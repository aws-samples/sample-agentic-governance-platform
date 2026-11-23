"""Span source + AgentCore judges.

``SpanSource`` reads an agent's OTel spans back out of CloudWatch through Logs Insights — from the
agent's own log group (stream ``spans``) when the runner resolves one, and Transaction Search's
``aws/spans`` group otherwise: the spans of one session (for the judges and for tool calls the
invoke envelope didn't carry), and the recent sessions of a service (the traces list that seeds a
dataset). ``judge_item`` hands one session's spans to AgentCore Evaluations ``Evaluate`` with the
dataset item as reference input.

Every log-group and attribute name sits in the constants block below, every parse is defensive,
and every read returns ``[]`` / ``None`` instead of raising. The CloudWatch vocabulary: in split
mode spans land in ``aws/spans``; in unified mode they land in the agent's own log group, which
also holds the runtime logs and EMF, so there the read is filtered to the ``spans`` and
``otel-rt-logs`` streams. ``otel-rt-logs`` carries the gen_ai log events (prompt and response
bodies) that Evaluate needs next to each span. A span document carries ``startTimeUnixNano``; a
log event has no start, only ``timeUnixNano``. ``service.name`` is a resource attribute, e.g.
``harness_<Name>_<suffix>.DEFAULT``. ``ignoredReferenceInputFields`` on an Evaluate result names
the reference fields the evaluator dropped.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from models.base import WireModel
from models.eval import DatasetItem, Score, ToolCall

logger = logging.getLogger(__name__)

SPANS_LOG_GROUP = "aws/spans"
SPANS_STREAMS = ("spans", "otel-rt-logs")
ATTR_SESSION_ID = "session.id"
ATTR_SERVICE_NAME = "service.name"
ATTR_OPERATION = "gen_ai.operation.name"
OPERATION_TOOL = "execute_tool"
ATTR_TOOL_NAME = "gen_ai.tool.name"
ATTR_TOOL_ARGS = "gen_ai.tool.call.arguments"
ATTRS_INPUT = ("gen_ai.input.messages", "gen_ai.prompt", "input.value")
ATTRS_OUTPUT = ("gen_ai.output.messages", "gen_ai.completion", "output.value")
FIELD_MESSAGE = "@message"
FIELD_TRACE_ID = "traceId"
FIELD_START = "startTimeUnixNano"
FIELD_END = "endTimeUnixNano"
FIELD_LOG_TIME = "timeUnixNano"
FIELD_IGNORED_REFS = "ignoredReferenceInputFields"

SPAN_LIMIT = 1000  # Evaluate's sessionSpans cap: https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_Evaluate.html
RECENT_SPAN_LIMIT = 10000  # Logs Insights' result cap: https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/CWL_QuerySyntax.html
QUERY_POLL_S = 1
RECENT_WAIT_S = 20  # under API Gateway's integration timeout: https://docs.aws.amazon.com/apigateway/latest/developerguide/limits.html
TEXT_MAX = 2000

_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_SAFE_PREFIX = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class TraceSummary(WireModel):
    session_id: str
    trace_id: str | None
    started_at: datetime | None
    input: str | None
    output: str | None
    tool_calls: list[ToolCall] | None
    latency_ms: int | None


def _attrs(span: Any) -> dict:
    a = span.get("attributes") if isinstance(span, dict) else None
    return a if isinstance(a, dict) else {}


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _time(doc: dict) -> int:
    return _int(doc.get(FIELD_START)) or _int(doc.get(FIELD_LOG_TIME)) or 0


def _span_count(docs: list[dict]) -> int:
    return sum(1 for d in docs if _int(d.get(FIELD_START)) is not None)


SPAN_AGENT_PREFIX = "invoke_agent"


def _is_tool_span(d: dict) -> bool:
    return _attrs(d).get(ATTR_OPERATION) == OPERATION_TOOL or str(d.get("name", "")).startswith(
        OPERATION_TOOL
    )


def _complete(docs: list[dict], needs_events: bool, min_tool_spans: int = 0) -> bool:
    """An ``invoke_agent`` span is in, at least ``min_tool_spans`` ``execute_tool`` spans are in, and
    (when log events are read) each agent span has its log event."""
    spans = [d for d in docs if _int(d.get(FIELD_START)) is not None]
    agents = [d for d in spans if str(d.get("name", "")).startswith(SPAN_AGENT_PREFIX)]
    if not agents:
        return False
    if sum(1 for d in spans if _is_tool_span(d)) < min_tool_spans:
        return False
    if not needs_events:
        return True
    events = {d.get("spanId") for d in docs if _int(d.get(FIELD_START)) is None}
    return all(a.get("spanId") in events for a in agents)


def _text(v: Any) -> str | None:
    """A prompt/completion attribute as plain text: the first ``text`` in a JSON message list, else
    the raw string."""
    if v is None:
        return None
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
        except ValueError:
            return v[:TEXT_MAX]
    else:
        parsed = v

    def first_text(o: Any) -> str | None:
        if isinstance(o, dict):
            if isinstance(o.get("text"), str):
                return o["text"]
            o = list(o.values())
        if isinstance(o, list):
            for x in o:
                t = first_text(x)
                if t:
                    return t
        return None

    t = first_text(parsed) if isinstance(parsed, (dict, list)) else str(parsed)
    return t[:TEXT_MAX] if t else None


def _user_text(v: Any) -> str | None:
    """A trace's input for display: the last text the user wrote, i.e. the last direct text part
    of the last ``role: user`` message that has one (tool results ride in user messages too, but
    nested, and skill/system text comes earlier). Anything else falls back to :func:`_text`."""
    try:
        parsed = json.loads(v) if isinstance(v, str) else v
    except ValueError:
        parsed = None
    if isinstance(parsed, list):
        for msg in reversed(parsed):
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            parts = msg.get("content", msg.get("parts"))
            parts = [{"text": parts}] if isinstance(parts, str) else parts
            for part in reversed(parts if isinstance(parts, list) else []):
                if not isinstance(part, dict):
                    continue
                t = part.get("text")
                if not isinstance(t, str) and part.get("type") == "text":
                    t = part.get("content")
                if isinstance(t, str) and t.strip():
                    return t[:TEXT_MAX]
    return _text(v)


class SpanSource:
    def __init__(
        self,
        logs_client,
        *,
        log_groups: list[str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._logs = logs_client
        self._log_groups = list(log_groups) if log_groups else [SPANS_LOG_GROUP]
        self._sleep = sleep

    def _query(self, body: str, since: datetime, budget_s: int) -> tuple[list[dict], int]:
        """Run ``fields @message`` + ``body`` once per log group; returns (span documents, seconds slept).

        One query per group because an agent's own group also holds runtime logs and EMF, so it gets a
        ``@logStream`` filter that ``aws/spans`` must not. Never raises; a failed group reads as empty."""
        spans: list[dict] = []
        waited = 0
        for group in self._log_groups:
            streams = ", ".join(f'"{s}"' for s in SPANS_STREAMS)
            stream = "" if group == SPANS_LOG_GROUP else f" | filter @logStream in [{streams}]"
            got, w = self._query_group(
                group, f"fields {FIELD_MESSAGE}{stream}{body}", since, max(budget_s - waited, 0)
            )
            spans += got
            waited += w
        return spans, waited

    def _query_group(self, group: str, query: str, since: datetime, budget_s: int) -> tuple[list[dict], int]:
        waited = 0
        try:
            qid = self._logs.start_query(
                logGroupNames=[group],
                startTime=int(since.timestamp()),
                endTime=int(time.time()),
                queryString=query,
            )["queryId"]
            while True:
                res = self._logs.get_query_results(queryId=qid)
                status = res.get("status")
                if status == "Complete":
                    break
                if status not in ("Scheduled", "Running") or waited >= budget_s:
                    return [], waited
                self._sleep(QUERY_POLL_S)
                waited += QUERY_POLL_S
        except Exception:
            logger.exception("Logs Insights span query on %s failed", group)
            return [], waited
        spans = []
        for row in res.get("results") or []:
            for f in row or []:
                if isinstance(f, dict) and f.get("field") == FIELD_MESSAGE:
                    try:
                        doc = json.loads(f.get("value") or "")
                    except ValueError:
                        continue
                    if isinstance(doc, dict):
                        spans.append(doc)
        return spans, waited

    def session_spans(
        self,
        session_id: str,
        *,
        since: datetime,
        wait_s: int = 180,
        poll_s: int = 15,
        min_tool_spans: int = 0,
    ) -> list[dict]:
        """One session's spans and log events, oldest first.

        Spans and their log events land minutes after the invoke, in separate batches and in either
        order, and Evaluate rejects a session missing either (live 2026-10-06: "log events but no span
        documents", "span … invoke_agent … is missing a corresponding log event"). So poll until the
        read is complete (``_complete``) and its document count is the same on two consecutive reads,
        or until ``wait_s`` runs out (then return whatever arrived, possibly ``[]``). ``min_tool_spans``
        is the tool-call count the invoke reported: the invoke_agent span can land before its
        execute_tool spans (live 2026-10-07: judged with no tool span, scored 0.00), so wait for them too.
        Log events come from the agent's own group only, so with aws/spans alone none are waited for.
        """
        if not _SAFE_ID.fullmatch(session_id or ""):
            return []
        query = (
            f' | filter attributes.{ATTR_SESSION_ID} = "{session_id}"'
            f" | sort {FIELD_START} asc | limit {SPAN_LIMIT}"
        )
        needs_events = any(g != SPANS_LOG_GROUP for g in self._log_groups)
        elapsed = 0
        last: list[dict] = []
        while True:
            docs, waited = self._query(query, since, max(wait_s - elapsed, 0))
            docs.sort(key=_time)
            elapsed += waited
            if _complete(docs, needs_events, min_tool_spans) and len(docs) == len(last):
                return docs
            if len(docs) >= len(last):
                last = docs
            if elapsed + poll_s > wait_s:
                return last
            self._sleep(poll_s)
            elapsed += poll_s

    def recent_sessions(
        self, service_name_prefix: str, *, since: datetime, limit: int = 50
    ) -> list[TraceSummary]:
        """Newest sessions of services whose name starts with the prefix: one query per group, grouped
        here by session."""
        if not _SAFE_PREFIX.fullmatch(service_name_prefix or ""):
            return []
        query = (
            f" | filter resource.attributes.{ATTR_SERVICE_NAME} like /^{re.escape(service_name_prefix)}/"
            f" | sort {FIELD_START} desc | limit {RECENT_SPAN_LIMIT}"
        )
        spans, _ = self._query(query, since, RECENT_WAIT_S)
        by_session: dict[str, list[dict]] = {}
        for s in spans:
            sid = _attrs(s).get(ATTR_SESSION_ID)
            if isinstance(sid, str) and sid:
                by_session.setdefault(sid, []).append(s)
        out = [self._summary(sid, ss) for sid, ss in by_session.items()]
        out.sort(key=lambda t: t.started_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return out[: max(1, limit)]

    @staticmethod
    def _summary(session_id: str, spans: list[dict]) -> TraceSummary:
        spans = sorted(spans, key=lambda s: _int(s.get(FIELD_START)) or 0)
        starts = [v for v in (_int(s.get(FIELD_START)) for s in spans) if v is not None]
        ends = [v for v in (_int(s.get(FIELD_END)) for s in spans) if v is not None]
        start = min(starts) if starts else None
        end = max(ends) if ends else None

        def first(names: tuple[str, ...], reverse: bool = False, text=_text) -> str | None:
            for s in reversed(spans) if reverse else spans:
                for n in names:
                    t = text(_attrs(s).get(n))
                    if t:
                        return t
            return None

        trace_id = next((s[FIELD_TRACE_ID] for s in spans if isinstance(s.get(FIELD_TRACE_ID), str)), None)
        return TraceSummary(
            session_id=session_id,
            trace_id=trace_id,
            started_at=datetime.fromtimestamp(start / 1e9, tz=UTC) if start is not None else None,
            input=first(ATTRS_INPUT, text=_user_text),
            output=first(ATTRS_OUTPUT, reverse=True),
            tool_calls=SpanSource.tool_calls_from_spans(spans),
            latency_ms=(end - start) // 1_000_000 if start is not None and end is not None else None,
        )

    @staticmethod
    def tool_calls_from_spans(spans: list[dict]) -> list[ToolCall] | None:
        """gen_ai tool spans → calls in start order; None when no tool span is recognised."""
        tools = []
        for s in spans or []:
            a = _attrs(s)
            name = a.get(ATTR_TOOL_NAME)
            if not isinstance(name, str) or not name:
                continue
            if ATTR_OPERATION in a and a[ATTR_OPERATION] != OPERATION_TOOL:
                continue
            raw = a.get(ATTR_TOOL_ARGS)
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raw = {}
            tools.append(
                (
                    _int(s.get(FIELD_START)) or 0,
                    ToolCall(name=name, args=raw if isinstance(raw, dict) else {}),
                )
            )
        if not tools:
            return None
        tools.sort(key=lambda t: t[0])
        return [c for _, c in tools]


def judge_item(agentcore_client, evaluator_id: str, spans: list[dict], item: DatasetItem) -> Score:
    """Score one item's session with an AgentCore built-in evaluator. Never raises.

    Log events alone cannot be judged. The item's assertions reference the session (GoalSuccessRate);
    its expected output references the trace of the item's final span (Correctness). TRACE-level
    evaluators return one result per trace, and a single-turn item has one."""

    def error(msg: str) -> Score:
        return Score(evaluator=evaluator_id, kind="judge", value=None, passed=None, error=msg)

    if not _span_count(spans):
        return error("no spans for this session")
    kwargs: dict[str, Any] = {
        "evaluatorId": evaluator_id,
        "evaluationInput": {"sessionSpans": spans[:SPAN_LIMIT]},
    }
    session_id = next((_attrs(s).get(ATTR_SESSION_ID) for s in spans if _attrs(s).get(ATTR_SESSION_ID)), None)
    refs: list[dict[str, Any]] = []
    if session_id and item.assertions:
        refs.append(
            {
                "context": {"spanContext": {"sessionId": session_id}},
                "assertions": [{"text": a} for a in item.assertions],
            }
        )
    if session_id and item.expected_output:
        ctx: dict[str, Any] = {"sessionId": session_id}
        trace_id = next(
            (
                s[FIELD_TRACE_ID]
                for s in reversed(spans)
                if isinstance(s, dict) and isinstance(s.get(FIELD_TRACE_ID), str)
            ),
            None,
        )
        if trace_id:
            ctx["traceId"] = trace_id
        refs.append({"context": {"spanContext": ctx}, "expectedResponse": {"text": item.expected_output}})
    if refs:
        kwargs["evaluationReferenceInputs"] = refs
    try:
        results = agentcore_client.evaluate(**kwargs).get("evaluationResults") or []
    except Exception as exc:
        logger.exception("Evaluate %s failed", evaluator_id)
        return error(str(exc) or type(exc).__name__)
    ignored = sorted({f for r in results for f in (r.get(FIELD_IGNORED_REFS) or []) if isinstance(f, str)})
    note = f" (ignored reference inputs: {', '.join(ignored)})" if ignored else ""
    values = [float(r["value"]) for r in results if isinstance(r.get("value"), (int, float))]
    if not values:
        bad = next((r for r in results if r.get("errorCode") or r.get("errorMessage")), None)
        if bad:
            return error(f"{bad.get('errorCode') or 'error'}: {bad.get('errorMessage') or ''}".strip() + note)
        return error("Evaluate returned no score" + note)
    first = next(r for r in results if isinstance(r.get("value"), (int, float)))
    return Score(
        evaluator=evaluator_id,
        kind="judge",
        value=sum(values) / len(values),
        passed=None,
        label=first.get("label"),
        explanation=((first.get("explanation") or "") + note).strip() or None,
    )
