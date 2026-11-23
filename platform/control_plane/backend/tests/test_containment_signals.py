"""AgentCoreEvalSource: aws/spans via FilterLogEvents -> custom evaluator -> FAIL signals.

Span fixtures are real fraud_triage_dev sessions from the 2026-10-13 live spike (account ids removed).
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from models.containment import Signal
from services.containment_signals import (
    EVALUATOR_CONFIG,
    EVALUATOR_NAME,
    JUDGE_MODEL_ID,
    TOOL_MISUSE_INSTRUCTIONS,
    AgentCoreEvalSource,
    SignalSource,
)

FIXTURES = Path(__file__).parent / "fixtures"
RT = "arn:aws:bedrock-agentcore:us-east-1:<acct>:runtime/fraud_triage_dev-AbC123xYz0"
HARNESS = "arn:aws:bedrock-agentcore:us-east-1:<acct>:harness/fnol_e40_dev-XyZ789"


def _spans(name):
    return json.loads((FIXTURES / f"e41_spans_{name}.json").read_text())


def _sid(spans):
    return spans[0]["attributes"]["session.id"]


def _tool_spans(spans):
    return [s for s in spans if s.get("attributes", {}).get("gen_ai.operation.name") == "execute_tool"]


def _ms(span):
    return int(span["endTimeUnixNano"]) // 1_000_000


class _FakeLogs:
    """filter_log_events double that behaves like CloudWatch on aws/spans (live probe 2026-10-13):
    a JSON selector on the dotted ``service.name`` key matches nothing; a quoted term matches any
    event whose message contains it."""

    def __init__(self, spans, page_size=3):
        self.events = [{"timestamp": _ms(s), "message": json.dumps(s)} for s in spans]
        self.page_size = page_size
        self.calls = []

    def add(self, spans):
        self.events += [{"timestamp": _ms(s), "message": json.dumps(s)} for s in spans]

    def filter_log_events(self, **kw):
        self.calls.append(kw)
        pattern = kw["filterPattern"]
        hits = [e for e in self.events if e["timestamp"] >= kw["startTime"]]
        if pattern.startswith("{"):
            hits = []
        else:
            assert pattern.startswith('"') and pattern.endswith('"')
            hits = [e for e in hits if pattern[1:-1] in e["message"]]
        start = int(kw.get("nextToken") or 0)
        page = hits[start : start + self.page_size]
        out: dict[str, Any] = {"events": page}
        if start + self.page_size < len(hits):
            out["nextToken"] = str(start + self.page_size)
        return out


class _FakeAgentCore:
    """evaluate double: one TOOL_CALL result per execute_tool span; label from ``labels`` (default PASS)."""

    def __init__(self, labels=None, value=None):
        self.labels = labels or {}
        self.value = value
        self.calls = []

    def evaluate(self, **kw):
        self.calls.append(kw)
        out = []
        target = set(kw["evaluationTarget"]["spanIds"])
        assert 1 <= len(target) <= 10
        for s in _tool_spans(kw["evaluationInput"]["sessionSpans"]):
            if s["spanId"] not in target:
                continue
            label = self.labels.get(s["spanId"], "PASS")
            out.append(
                {
                    "evaluatorId": kw["evaluatorId"],
                    "label": label,
                    "value": self.value,
                    "explanation": f"{label} because reasons",
                    "context": {
                        "spanContext": {
                            "sessionId": s["attributes"]["session.id"],
                            "traceId": s["traceId"],
                            "spanId": s["spanId"],
                        }
                    },
                }
            )
        return {"evaluationResults": out}


class _FakeControl:
    def __init__(self, existing=None):
        self.existing = existing or []
        self.calls = []

    def list_evaluators(self, **kw):
        self.calls.append(("list", kw))
        if not kw.get("nextToken"):
            return {
                "evaluators": [
                    {"evaluatorId": "Builtin.Helpfulness", "evaluatorName": "Builtin.Helpfulness"}
                ],
                "nextToken": "p2",
            }
        return {"evaluators": self.existing}

    def create_evaluator(self, **kw):
        self.calls.append(("create", kw))
        return {"evaluatorId": "agp_tool_misuse-New123", "status": "ACTIVE"}


def _agent(arns=None):
    arns = {"dev": RT} if arns is None else arns
    return SimpleNamespace(id="agent1", runtime_arns=lambda: arns)


def _source(logs, agentcore=None, control=None, clock=None):
    agentcore = agentcore or _FakeAgentCore()
    control = control or _FakeControl(
        existing=[{"evaluatorId": "agp_tool_misuse-Old999", "evaluatorName": EVALUATOR_NAME}]
    )
    built = []

    def factory(client):
        def make(service, agent):
            built.append((service, agent.id))
            return client

        return make

    src = AgentCoreEvalSource(
        logs_client_factory=factory(logs),
        agentcore_client_factory=factory(agentcore),
        control_client_factory=factory(control),
        **({"clock": clock} if clock else {}),
    )
    return src, agentcore, control, built


def test_ensure_evaluator_finds_existing_by_name_without_creating():
    src, _, control, built = _source(_FakeLogs([]))
    assert src.ensure_evaluator(_agent()) == "agp_tool_misuse-Old999"
    assert [c[0] for c in control.calls] == ["list", "list"]
    assert control.calls[1][1]["nextToken"] == "p2"
    assert ("bedrock-agentcore-control", "agent1") in built


def test_ensure_evaluator_creates_when_missing_with_exact_config():
    control = _FakeControl(existing=[])
    src, _, _, _ = _source(_FakeLogs([]), control=control)
    assert src.ensure_evaluator(_agent()) == "agp_tool_misuse-New123"
    create = [c[1] for c in control.calls if c[0] == "create"]
    assert len(create) == 1
    kw = create[0]
    assert kw["evaluatorName"] == "agp_tool_misuse_v2" == EVALUATOR_NAME
    assert kw["level"] == "TOOL_CALL"
    judge = kw["evaluatorConfig"]["llmAsAJudge"]
    assert judge["instructions"] == TOOL_MISUSE_INSTRUCTIONS
    assert [c["label"] for c in judge["ratingScale"]["categorical"]] == ["PASS", "FAIL"]
    assert all(c["definition"] for c in judge["ratingScale"]["categorical"])
    model = judge["modelConfig"]["bedrockEvaluatorModelConfig"]
    assert model["modelId"] == JUDGE_MODEL_ID == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert model["inferenceConfig"]["temperature"] == 0


def test_ensure_evaluator_is_cached_after_the_first_call():
    src, _, control, _ = _source(_FakeLogs([]))
    src.ensure_evaluator(_agent())
    src.ensure_evaluator(_agent())
    assert len(control.calls) == 2  # one paginated list, once


def test_rubric_is_verbatim():
    assert TOOL_MISUSE_INSTRUCTIONS.startswith(
        "You audit one tool call made by an AI agent for an insurance company.\n"
    )
    for placeholder in ("{available_tools}", "{context}", "{tool_turn}"):
        assert placeholder in TOOL_MISUSE_INSTRUCTIONS
    assert TOOL_MISUSE_INSTRUCTIONS.endswith("quotes the offending text when you answer FAIL.")


def test_rubric_weighs_what_the_tool_received_and_returned():
    """A hook can rewrite the arguments after the model emits them: the span's arguments look clean
    and only the tool's echoed record carries the note, so the judge must read the result."""
    text = TOOL_MISUSE_INSTRUCTIONS
    assert "with its arguments and its result: {tool_turn}" in text
    assert "not only the arguments the model wrote" in text
    assert "data this call wrote that the tool echoes back" in text
    assert "release a payout or approve without review" in text
    assert "Answer PASS for everything else" in text and "lookups" in text
    fail = EVALUATOR_CONFIG["llmAsAJudge"]["ratingScale"]["categorical"][1]
    assert fail["label"] == "FAIL" and "echoed back" in fail["definition"]


def test_v2_name_ignores_an_existing_v1_evaluator():
    """ensure_evaluator never updates by name, so the new rubric only reaches AgentCore under a new name."""
    control = _FakeControl(
        existing=[{"evaluatorId": "agp_tool_misuse-Old999", "evaluatorName": "agp_tool_misuse"}]
    )
    src, _, _, _ = _source(_FakeLogs([]), control=control)
    assert src.ensure_evaluator(_agent()) == "agp_tool_misuse-New123"
    assert [c[1]["evaluatorName"] for c in control.calls if c[0] == "create"] == ["agp_tool_misuse_v2"]


def test_source_satisfies_protocol_and_name():
    src, _, _, _ = _source(_FakeLogs([]))
    assert isinstance(src, SignalSource)
    assert src.name == "agentcore-evaluations"


def test_scan_pages_aws_spans_from_since_and_returns_max_timestamp_cursor():
    spans = _spans("benign1") + _spans("benign2")
    logs = _FakeLogs(spans, page_size=3)
    src, _, _, _ = _source(logs)
    signals, sessions, cursor = src.scan(_agent(), since_ms=0)
    first = [c for c in logs.calls if c["filterPattern"] == '"fraud_triage_dev"']
    assert len(first) > 1 and all(c["logGroupName"] == "aws/spans" and c["startTime"] == 0 for c in first)
    assert cursor == max(_ms(s) for s in spans)
    assert dict(sessions) == {
        _sid(_spans("benign1")): max(_ms(s) for s in _spans("benign1")),
        _sid(_spans("benign2")): max(_ms(s) for s in _spans("benign2")),
    }
    assert signals == []


def test_scan_with_no_events_keeps_the_cursor():
    src, agentcore, _, _ = _source(_FakeLogs([]))
    assert src.scan(_agent(), since_ms=1234) == ([], [], 1234)
    assert agentcore.calls == []


def test_scan_emits_signal_only_for_fail_and_reads_label():
    spans = _spans("benign2")  # get_current_datetime + calculator
    _dt, calc = _tool_spans(spans)
    agentcore = _FakeAgentCore(labels={calc["spanId"]: "FAIL"}, value=1.0)  # value is ignored
    src, _, _, _ = _source(_FakeLogs(spans), agentcore=agentcore)
    signals, _, _ = src.scan(_agent(), since_ms=0)
    assert len(agentcore.calls) == 1
    call = agentcore.calls[0]
    assert call["evaluatorId"] == "agp_tool_misuse-Old999"
    assert len(call["evaluationInput"]["sessionSpans"]) == len(spans)
    assert len(signals) == 1
    sig = signals[0]
    assert isinstance(sig, Signal)
    assert sig.agent_id == "agent1"
    assert sig.session_id == _sid(spans)
    assert sig.span_id == calc["spanId"]
    assert sig.trace_id == calc["traceId"]
    assert sig.tool == "calculator"
    assert sig.label == "FAIL"
    assert sig.explanation == "FAIL because reasons"
    assert sig.span_end.startswith("2026-")
    assert sig.detected_at
    assert sig.source == "agentcore-evaluations"


def test_scan_ignores_numeric_value_without_fail_label():
    spans = _spans("benign1")
    agentcore = _FakeAgentCore(value=0.0)  # PASS label, value 0 must not become a signal
    src, _, _, _ = _source(_FakeLogs(spans), agentcore=agentcore)
    assert src.scan(_agent(), since_ms=0)[0] == []


def test_scan_judges_each_tool_span_once():
    spans = _spans("misuse")
    agentcore = _FakeAgentCore(labels={_tool_spans(spans)[0]["spanId"]: "FAIL"})
    src, _, _, _ = _source(_FakeLogs(spans), agentcore=agentcore)
    first, _, cursor = src.scan(_agent(), since_ms=0)
    again, sessions, cursor2 = src.scan(_agent(), since_ms=cursor)  # boundary event re-read
    assert len(first) == 1 and again == []
    assert len(agentcore.calls) == 1
    assert sessions and cursor2 == cursor


def test_scan_session_with_no_tool_span_is_not_judged():
    spans = [s for s in _spans("benign1") if s["attributes"].get("gen_ai.operation.name") != "execute_tool"]
    src, agentcore, _, _ = _source(_FakeLogs(spans))
    signals, sessions, _ = src.scan(_agent(), since_ms=0)
    assert signals == [] and agentcore.calls == [] and len(sessions) == 1


def test_scan_fetches_the_whole_session_for_context():
    spans = _spans("benign2")
    tool_end = max(_ms(s) for s in _tool_spans(spans))
    logs = _FakeLogs(spans)
    src, agentcore, _, _ = _source(logs)
    src.scan(_agent(), since_ms=tool_end)  # the earlier spans of the turn are before the cursor
    by_sid = [c for c in logs.calls if c["filterPattern"] == f'"{_sid(spans)}"']
    assert by_sid and by_sid[0]["startTime"] < tool_end
    assert len(agentcore.calls[0]["evaluationInput"]["sessionSpans"]) == len(spans)


def test_scan_evaluate_error_holds_the_cursor_and_retries_from_it():
    spans = _spans("benign1")
    tool = _tool_spans(spans)[0]

    class Flaky(_FakeAgentCore):
        def evaluate(self, **kw):
            if not self.calls:
                self.calls.append(kw)
                raise RuntimeError("throttled")
            return super().evaluate(**kw)

    agentcore = Flaky(labels={tool["spanId"]: "FAIL"})
    src, _, _, _ = _source(_FakeLogs(spans), agentcore=agentcore)
    signals, _, cursor = src.scan(_agent(), since_ms=0)
    assert signals == [] and cursor == _ms(tool)  # not past the unjudged tool span
    again, _, cursor2 = src.scan(_agent(), since_ms=cursor)
    assert len(again) == 1 and cursor2 == max(_ms(s) for s in spans)


def test_scan_no_result_for_a_span_is_not_judged_and_holds_the_cursor():
    spans = _spans("benign1")
    tool = _tool_spans(spans)[0]

    class Empty(_FakeAgentCore):
        def evaluate(self, **kw):
            self.calls.append(kw)
            return {
                "evaluationResults": [
                    {"errorCode": "Incomplete", "context": {"spanContext": {"spanId": tool["spanId"]}}}
                ]
            }

    agentcore = Empty()

    def young():
        return _ms(tool) / 1000 + 60

    src, _, _, _ = _source(_FakeLogs(spans), agentcore=agentcore, clock=young)
    _, _, cursor = src.scan(_agent(), since_ms=0)
    assert cursor == _ms(tool)
    src.scan(_agent(), since_ms=cursor)
    assert len(agentcore.calls) == 2


def test_scan_ten_failed_evaluates_then_success_still_signals():
    spans = _spans("benign1")
    tool = _tool_spans(spans)[0]

    class Down(_FakeAgentCore):
        def evaluate(self, **kw):
            self.calls.append(kw)
            if len(self.calls) <= 10:
                raise RuntimeError("ThrottlingException")
            return super().evaluate(**kw)

    agentcore = Down(labels={tool["spanId"]: "FAIL"})

    def an_hour_later():
        return _ms(tool) / 1000 + 3600

    src, _, _, _ = _source(_FakeLogs(spans), agentcore=agentcore, clock=an_hour_later)
    cursor = 0
    for _ in range(10):
        signals, _, cursor = src.scan(_agent(), since_ms=cursor)
        assert signals == [] and cursor == _ms(tool)
    signals, _, _ = src.scan(_agent(), since_ms=cursor)
    assert [s.span_id for s in signals] == [tool["spanId"]]


def test_scan_old_span_with_no_label_answers_releases_the_cursor():
    spans = _spans("benign1")
    tool = _tool_spans(spans)[0]

    class Never(_FakeAgentCore):
        def evaluate(self, **kw):
            self.calls.append(kw)
            return {"evaluationResults": []}

    now = {"s": _ms(tool) / 1000 + 60}
    agentcore = Never()
    src, _, _, _ = _source(_FakeLogs(spans), agentcore=agentcore, clock=lambda: now["s"])
    _, _, cursor = src.scan(_agent(), since_ms=0)
    assert cursor == _ms(tool)  # young: still held
    now["s"] = _ms(tool) / 1000 + 16 * 60
    _, _, cursor = src.scan(_agent(), since_ms=cursor)
    assert cursor == max(_ms(s) for s in spans)  # older than 15 min: released
    calls = len(agentcore.calls)
    src.scan(_agent(), since_ms=0)
    assert len(agentcore.calls) == calls  # and not judged again


def test_scan_reads_a_minute_back_and_catches_a_late_span():
    spans = _spans("benign1")
    tool = _tool_spans(spans)[0]
    logs = _FakeLogs([s for s in spans if s is not tool])
    agentcore = _FakeAgentCore(labels={tool["spanId"]: "FAIL"})
    src, _, _, _ = _source(logs, agentcore=agentcore)
    _, _, cursor = src.scan(_agent(), since_ms=0)
    assert cursor > _ms(tool)
    logs.add([tool])  # ingested late, older than the cursor
    signals, _, _ = src.scan(_agent(), since_ms=cursor)
    assert [s.span_id for s in signals] == [tool["spanId"]]
    reads = [c["startTime"] for c in logs.calls if c["filterPattern"] == '"fraud_triage_dev"']
    assert reads[-1] == cursor - 60_000


def test_scan_post_filters_on_the_exact_service_name():
    spans = _spans("benign1")
    other = json.loads(json.dumps(spans))
    for s in other:
        s["resource"]["attributes"]["service.name"] = (
            "fraud_triage_dev_shadow"  # term matches, service doesn't
        )
        s["attributes"]["session.id"] = "other-session"
        s["spanId"] = "f" + s["spanId"][1:]
    src, agentcore, _, _ = _source(_FakeLogs(other))
    assert src.scan(_agent(), since_ms=0)[:2] == ([], [])
    assert agentcore.calls == []


def test_scan_targets_only_new_tool_spans_and_never_rejudges_pass():
    spans = _spans("benign2")
    dt, calc = _tool_spans(spans)
    logs = _FakeLogs([s for s in spans if s is not calc])
    agentcore = _FakeAgentCore(labels={calc["spanId"]: "FAIL"})
    src, _, _, _ = _source(logs, agentcore=agentcore)
    assert src.scan(_agent(), since_ms=0)[0] == []
    assert agentcore.calls[0]["evaluationTarget"] == {"spanIds": [dt["spanId"]]}
    logs.add([calc])
    signals, _, _ = src.scan(_agent(), since_ms=0)
    assert agentcore.calls[1]["evaluationTarget"] == {"spanIds": [calc["spanId"]]}
    assert len(agentcore.calls[1]["evaluationInput"]["sessionSpans"]) == len(
        spans
    )  # whole session as context
    assert [s.span_id for s in signals] == [calc["spanId"]]
    src.scan(_agent(), since_ms=0)
    assert len(agentcore.calls) == 2  # the PASS span is not judged again


def test_scan_batches_targets_by_ten():
    base = _spans("benign1")
    tool = _tool_spans(base)[0]
    extra = []
    for i in range(11):
        t = json.loads(json.dumps(tool))
        t["spanId"] = f"{i:016x}"
        extra.append(t)
    src, agentcore, _, _ = _source(_FakeLogs(base + extra))
    src.scan(_agent(), since_ms=0)
    assert [len(c["evaluationTarget"]["spanIds"]) for c in agentcore.calls] == [10, 2]


def test_scan_skips_harness_arns():
    src, _agentcore, _, _ = _source(_FakeLogs(_spans("benign1")))
    assert src.scan(_agent({"dev": HARNESS}), since_ms=7) == ([], [], 7)


def test_scan_logs_failure_returns_nothing_and_keeps_cursor():
    class Broken(_FakeLogs):
        def filter_log_events(self, **kw):
            raise RuntimeError("denied")

    src, _, _, _ = _source(Broken([]))
    assert src.scan(_agent(), since_ms=5) == ([], [], 5)
