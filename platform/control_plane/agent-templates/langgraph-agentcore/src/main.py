"""LangGraph Agent Template — Bedrock AgentCore Entry Point.

A barebones tool-using graph: an LLM node and a tool node with a conditional edge, which is the
smallest shape that is recognisably LangGraph rather than a single LLM call.
Deploys to Bedrock AgentCore via BedrockAgentCoreApp.

Local: python -m src.main (starts on http://localhost:8080)
Docker: docker build -t my-agent . && docker run -p 8080:8080 my-agent
Test: curl -X POST http://localhost:8080/invocations -d '{"prompt": "hello"}'

RESPONSE CONTRACT — ``{"prompt": "..."}`` in, ``{"result": "<answer>"}`` out, ONE JSON object,
not a stream. This differs from the Strands template in this catalog, which streams SSE, and the
difference is deliberate: LangGraph's ``invoke`` is request/response, so a streaming wrapper here
would be a translation layer with nothing to translate.

**What the platform actually returns to its caller**, because the two branches do NOT agree and an
earlier version of this docstring got it wrong: ``POST /agents/{id}/invoke`` always wraps in
``{"response": ...}``, and only the SSE branch unwraps the payload. This template takes the JSON
branch (``application/json``), where the body is forwarded VERBATIM — so a client sees

    {"response": {"result": "<answer>"}}

i.e. NESTED, one level deeper than the Strands template's ``{"response": "<answer>"}``. Nothing
unwraps ``result`` for a non-streaming agent. That is a real difference between the two templates,
not an inconsistency to paper over here — the fix, if it is ever wanted, belongs in the platform's
route, not in this file.

Keep the key named ``result`` regardless: it is what the SSE extractor looks for, so renaming it
would break a future streaming variant of this template, and the governance UI keys on it too.
"""

import base64
import contextlib
import json
import logging
import os
from datetime import UTC, datetime
from typing import Annotated, TypedDict

import boto3

from src.config import config

logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# --- Langfuse observability -----------------------------------------------------------------
# EVERY agent provisioned from this template is observable with zero manual wiring: the platform
# provisions one Langfuse project + key PER AGENT and injects the NON-SECRET host + secret NAME
# into the runtime env (LANGFUSE_HOST / LANGFUSE_SECRET_NAME). This block reads the per-agent
# {public_key, secret_key} pair from AWS Secrets Manager and points the OTLP exporter at that
# project — attribution is STRUCTURAL (each agent authenticates with its own project key, so
# traces land in its own project). The project key is NEVER a hardcoded literal; the secret VALUE
# lives ONLY in Secrets Manager and is never logged (the base64 auth IS the secret).
#
# Runs BEFORE any langchain/langgraph import below, which is why those imports are not at the top
# of this module: LangChain reads the OTEL env at import time, and `LANGSMITH_OTEL_ENABLED` has to
# be set before it is read or the instrumentation never attaches. Graceful degrade throughout: if
# the env/secret is absent or unreadable, telemetry is simply not wired and the agent still runs.


# The TracerProvider this module installs, or None when telemetry is not wired. Held at module
# level ONLY so the entrypoint can force-flush THIS provider — see the flush in `handler`.
_tracer_provider = None


def _setup_langfuse_telemetry() -> None:
    host = config.LANGFUSE_HOST.rstrip("/")
    secret_name = config.LANGFUSE_SECRET_NAME
    if not host or not secret_name:
        logger.info("Langfuse telemetry disabled: LANGFUSE_HOST / LANGFUSE_SECRET_NAME not set.")
        return

    try:
        sm = boto3.client("secretsmanager", region_name=config.AWS_REGION)
        secret = json.loads(sm.get_secret_value(SecretId=secret_name)["SecretString"])
        public_key = secret.get("public_key", "")
        secret_key = secret.get("secret_key", "")
    except Exception as exc:
        logger.warning(
            "Langfuse telemetry disabled: could not read secret %s (%r)", secret_name, exc, exc_info=True
        )
        return

    if not public_key or not secret_key:
        logger.warning("Langfuse telemetry disabled: secret %s missing public_key/secret_key.", secret_name)
        return

    # Basic auth base64("{public}:{secret}"). NEVER log this value / the Authorization header.
    lf_auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()

    # AgentCore auto-instruments with ADOT and owns the global OTEL TracerProvider first
    # (set-once) — disabling ADOT lets the Langfuse exporter win. Set only once Langfuse is configured.
    os.environ["DISABLE_ADOT_OBSERVABILITY"] = "true"
    # BASE endpoint form: the HTTP exporter auto-appends /v1/traces — do NOT add it here.
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"{host}/api/public/otel"
    os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Basic {lf_auth}"
    # Langfuse supports http/protobuf + http/json; gRPC is NOT supported.
    os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
    os.environ.setdefault("OTEL_SERVICE_NAME", os.environ.get("AGENT_NAME", "langgraph-agentcore-agent"))
    # LangChain's own OTEL bridge, which is what emits the graph/LLM spans this exporter ships.
    os.environ["LANGSMITH_OTEL_ENABLED"] = "true"

    # WRAPPED, because this tail runs at MODULE IMPORT and an exception here does not degrade
    # telemetry — it kills the agent before `app.run()` and the runtime crash-loops. An agent with
    # no traces is degraded; an agent that will not start is down. Logged WITHOUT the auth
    # header/keys: `exc` is the import/exporter fault, and the secret only ever reaches os.environ
    # above — a traceback is not a place for a credential.
    #
    # THE PROVIDER IS BUILT EXPLICITLY, and that is the whole reason this block is more than one
    # line. Unlike Strands — whose `[otel]` extra installs a real TracerProvider for us —
    # `LangchainInstrumentor().instrument()` installs NONE: it patches LangChain to EMIT spans but
    # never sets a provider. Combined with `DISABLE_ADOT_OBSERVABILITY` above (which removes the
    # one provider AgentCore would have installed), the global provider stays the no-op
    # `ProxyTracerProvider`, every span comes back `is_recording() == False`, and ALL telemetry is
    # dropped — silently, with the Langfuse env correctly set and nothing in the logs to say so.
    # Probed, not assumed: bare `instrument()` leaves `ProxyTracerProvider`/`is_recording() False`;
    # with the provider below it is `TracerProvider`/`True`.
    global _tracer_provider
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.langchain import LangchainInstrumentor
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        # The exporter reads OTEL_EXPORTER_OTLP_* from the env set above — no argument needed, and
        # no endpoint/credential is ever passed as a literal here.
        provider = TracerProvider()
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        # Passed EXPLICITLY as well as set globally. `set_tracer_provider` is set-once: if anything
        # in the runtime got there first, the global silently stays theirs, and handing the
        # instrumentor our provider directly means LangChain's spans reach Langfuse either way.
        LangchainInstrumentor().instrument(tracer_provider=provider)
        # Kept for the shutdown flush. `get_tracer_provider()` is NOT a substitute: if the
        # set-once above lost, it returns someone else's provider (or the Proxy, which has no
        # `force_flush` at all) and the flush becomes a no-op that logs a warning every turn.
        _tracer_provider = provider
        logger.info("Langfuse telemetry enabled for host %s", host)
    except Exception as exc:
        logger.warning(
            "Langfuse telemetry disabled: langchain instrumentation failed (%r)", exc, exc_info=True
        )


_setup_langfuse_telemetry()
# --- end Langfuse observability -------------------------------------------------------------


# --- CloudWatch traces (E42) ----------------------------------------------------------------
# A SECOND span processor on the same provider sends every span to CloudWatch as well, because
# AgentCore Evaluations judges this agent from its CloudWatch spans (README → "Evaluations"). This
# is NOT ADOT auto-instrumentation: it is one explicit OTLP exporter pointed at the X-Ray OTLP
# endpoint, signed with SigV4 from the runtime role. The signing is the same mechanism as
# aws-opentelemetry-distro's `OTLPAwsSpanExporter`
# (amazon/opentelemetry/distro/exporter/otlp/aws/traces/otlp_aws_span_exporter.py, whose
# AwsAuthSession in .../aws/common/aws_auth_session.py is a requests.Session that SigV4-signs each
# POST for service "xray"), written out here because the distro pins the OTEL SDK to one exact
# version plus ~50 instrumentations, which would fight the ranges this template pins.
# On by default; AGP_CLOUDWATCH_TRACES=false turns it off. Any setup fault is logged and the agent
# runs without it, the same one-sided rule as the Langfuse block. Runs before the langchain imports
# below for the same reason that block does.


_XRAY_SENT_HEADERS = {"content-type", "content-encoding", "user-agent"}  # + SigV4's own


def _xray_span_exporter(region: str):
    """An OTLP/HTTP span exporter for ``https://xray.{region}.amazonaws.com/v1/traces``, SigV4-signed."""
    import botocore.session
    import requests
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from requests.structures import CaseInsensitiveDict

    credentials = botocore.session.get_session().get_credentials()
    if credentials is None:
        raise RuntimeError("no AWS credentials to sign X-Ray span exports with")

    class _SigV4Session(requests.Session):
        def request(self, method, url, *args, data=None, headers=None, **kwargs):
            # The exporter also picks up OTEL_EXPORTER_OTLP_HEADERS, which the Langfuse block sets
            # to its Basic auth. So AWS gets an ALLOWLIST: the payload headers plus what SigV4 adds.
            # Every other header, on the session or on the call, is dropped (None removes a session
            # header from requests' merge).
            given = CaseInsensitiveDict({**self.headers, **(headers or {})})
            kept = {k: v for k, v in given.items() if k.lower() in _XRAY_SENT_HEADERS}
            signed = AWSRequest(method=method, url=url, data=data, headers=kept)
            # Frozen once per request, so a refresh can't land between the key, secret and token reads.
            SigV4Auth(credentials.get_frozen_credentials(), "xray", region).add_auth(signed)
            sent = CaseInsensitiveDict({k: None for k in self.headers})
            sent.update(signed.headers.items())
            return super().request(method, url, *args, data=data, headers=sent, **kwargs)

    endpoint = f"https://xray.{region}.amazonaws.com/v1/traces"
    return OTLPSpanExporter(endpoint=endpoint, session=_SigV4Session())


def _setup_cloudwatch_traces() -> None:
    if os.environ.get("AGP_CLOUDWATCH_TRACES", "").strip().lower() == "false":
        logger.info("CloudWatch traces disabled: AGP_CLOUDWATCH_TRACES=false.")
        return

    global _tracer_provider
    try:
        from bedrock_agentcore.runtime.context import BedrockAgentCoreContext
        from opentelemetry.sdk.trace import SpanProcessor
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        class _SessionIdProcessor(SpanProcessor):
            """Tags each span with the AgentCore session id, which is how CloudWatch spans are
            found per invoke. The runtime sets it per request from its session-id header."""

            def on_start(self, span, parent_context=None):
                session_id = BedrockAgentCoreContext.get_session_id()
                if session_id:
                    span.set_attribute("session.id", session_id)

        exporter = _xray_span_exporter(config.AWS_REGION)
        if _tracer_provider is None:
            # Langfuse is not wired, so nothing has built a provider yet. Build and instrument one
            # exactly as the Langfuse block does (see there for why it must be explicit).
            from opentelemetry import trace
            from opentelemetry.instrumentation.langchain import LangchainInstrumentor
            from opentelemetry.sdk.trace import TracerProvider

            os.environ["DISABLE_ADOT_OBSERVABILITY"] = "true"
            os.environ["LANGSMITH_OTEL_ENABLED"] = "true"
            provider = TracerProvider()
            trace.set_tracer_provider(provider)
            LangchainInstrumentor().instrument(tracer_provider=provider)
            _tracer_provider = provider
        _tracer_provider.add_span_processor(_SessionIdProcessor())
        _tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
        logger.info("CloudWatch traces enabled (X-Ray OTLP endpoint, %s)", config.AWS_REGION)
    except Exception as exc:
        logger.warning(
            "CloudWatch traces disabled: X-Ray span exporter setup failed (%r)", exc, exc_info=True
        )


_setup_cloudwatch_traces()
# --- end CloudWatch traces ------------------------------------------------------------------

# Imported HERE, not at module top: the block above must set LANGSMITH_OTEL_ENABLED and the OTEL
# endpoint BEFORE langchain reads them at import time — a top-of-file import captures the
# un-configured values and the traces silently never appear. E402 (the rule that forbids exactly
# this ordering) is therefore ignored for this file in pyproject.toml, with the reason recorded
# there; do NOT "fix" the ordering to satisfy it. Keep them grouped so isort leaves them put.
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import BedrockAgentCoreContext
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AnyMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

# --- Tools ---


@tool
def get_current_datetime() -> str:
    """Get the current date and time in UTC."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


TOOLS = [get_current_datetime]

# --- Graph ---

SYSTEM_PROMPT = """You are a helpful assistant. You can:
- Answer questions conversationally
- Tell the current date and time

Be concise and helpful. Use tools when appropriate."""


class State(TypedDict):
    """`add_messages` APPENDS rather than replaces, which is what lets the tool node's output
    accumulate onto the conversation instead of overwriting the LLM's tool-call message."""

    messages: Annotated[list[AnyMessage], add_messages]


def build_graph():
    """Compile the graph.

    Called from the entrypoint on first invocation, NOT at import: the Bedrock client it builds
    would otherwise be constructed while the container is still starting, and a credential or
    region fault there kills the runtime at import instead of failing one invocation.
    """
    llm = ChatBedrockConverse(
        model=config.MODEL_ID,
        region_name=config.AWS_REGION,
        temperature=0.3,
        max_tokens=4096,
    ).bind_tools(TOOLS)

    def call_model(state: State) -> dict:
        return {"messages": [llm.invoke(state["messages"])]}

    graph = StateGraph(State)
    graph.add_node("agent", call_model)
    graph.add_node("tools", ToolNode(TOOLS))
    graph.add_edge(START, "agent")
    # `tools_condition` routes to "tools" when the model asked for one and to END otherwise —
    # the loop back to "agent" is what lets the model read the tool result and answer.
    graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()


def _answer_text(message) -> str:
    """Flatten a message's content to a plain string.

    ``ChatBedrockConverse`` returns ``content`` as EITHER a string or a list of content blocks
    (``[{"type": "text", "text": "…"}, …]``) depending on the model and whether tools were used.
    Returning the list verbatim keeps the response 200 but makes ``{"result": [...]}` a JSON array
    where every reader expects a string, so the governance UI renders a blob instead of the
    answer. Join the text blocks and drop the non-text ones (reasoning/tool-use blocks are not
    the answer).
    """
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    return str(content)


def _tool_calls(messages) -> list[dict]:
    """This invoke's tool calls, in order, as ``[{"name", "args"}]`` — read by evaluations.

    Every message in the state belongs to this invoke (the graph starts from a fresh list each
    time), and an ``AIMessage`` carries the calls the model asked for in ``tool_calls``. A call
    without a name is skipped: the backend's ToolCall needs one.
    """
    return [
        {"name": call["name"], "args": call.get("args") or {}}
        for message in messages
        for call in (getattr(message, "tool_calls", None) or [])
        if isinstance(call.get("name"), str)
    ]


_MAX_OID_LEN = 128  # an Entra oid is a 36-char GUID; generous headroom


def _caller_oid(payload: dict) -> str | None:
    """The calling user's Entra ``oid``, for the Langfuse Traces USER column. ``None`` if unknown.

    Prefers the inbound bearer, which the runtime's JWT authorizer has already verified (read here
    without re-verifying; only the ``oid`` claim leaves this function — never the token). Falls back
    to ``user_oid`` in the payload, which the platform sets from the caller it authenticated: the
    inbound Authorization header usually does not reach the container. Telemetry only, so this
    never raises.
    """
    token_oid = None
    try:
        headers = BedrockAgentCoreContext.get_request_headers() or {}
        auth = headers.get("Authorization") or headers.get("authorization") or ""
        if auth.startswith("Bearer "):
            seg = auth[len("Bearer ") :].split(".")[1]
            token_oid = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))).get("oid")
    except Exception as exc:
        logger.debug(
            "Caller oid not readable from the inbound bearer (%s)", type(exc).__name__, exc_info=True
        )
    for oid in (token_oid, payload.get("user_oid")):
        # A direct caller can put anything in the payload: a non-string or an implausibly long
        # value is ignored (not truncated), since a mangled id would mislabel the trace.
        if isinstance(oid, str) and 0 < len(oid) <= _MAX_OID_LEN:
            return oid
    return None


def _user_span(payload: dict):
    """The root ``invoke_agent`` span, tagged with the caller when there is one; no-op if unwired.

    Opened for every invoke, oid or not, so every trace from this agent has the same root name.

    The LangChain instrumentation's spans nest under the current span, so one attribute here
    reaches the whole trace. Key is the Langfuse OTEL mapping spelling (`langfuse.user.id`).
    """
    if _tracer_provider is None:
        return contextlib.nullcontext()
    oid = _caller_oid(payload)
    return _tracer_provider.get_tracer(__name__).start_as_current_span(
        "invoke_agent", attributes={"langfuse.user.id": oid} if oid else None
    )


# --- AgentCore App ---

app = BedrockAgentCoreApp()

_graph = None


@app.entrypoint
def handler(payload: dict, context=None) -> dict:
    """Handle agent invocations.

    Payload: ``{"prompt": "user message"}`` → Response: ``{"result": "<answer>", "tool_calls":
    [{"name": ..., "args": {...}}]}``. ``tool_calls`` is present on success only (README →
    "Evaluations").

    An error is returned in the SAME shape (``{"result": "Error: ..."}``) rather than raised: a
    raise becomes a 500 with no body the platform can read, so the governance UI would show a
    failed invocation with no reason. The message is what a caller can act on.
    """
    prompt = payload.get("prompt", "")
    if not prompt:
        return {"result": "Error: 'prompt' field is required"}

    logger.info("Invocation received: %s...", prompt[:50])
    global _graph
    try:
        with _user_span(payload):
            if _graph is None:
                _graph = build_graph()
            state = _graph.invoke({"messages": [("system", SYSTEM_PROMPT), ("human", prompt)]})
        return {"result": _answer_text(state["messages"][-1]), "tool_calls": _tool_calls(state["messages"])}
    # Blind by design — ANY graph fault must reach the caller as a message rather than crash the
    # runtime.
    except Exception as e:
        logger.exception("Agent error")
        return {"result": f"Error: {e}"}
    finally:
        # Force-flush OTEL spans before the AgentCore microVM freezes: the background
        # BatchSpanProcessor would otherwise lose buffered Langfuse spans. Best-effort.
        #
        # Flushes THE PROVIDER THIS MODULE BUILT, not `get_tracer_provider()`. The global is a
        # `ProxyTracerProvider` whenever telemetry is unwired — and it has no `force_flush`
        # attribute at all, so the old call raised AttributeError on EVERY invocation and logged a
        # scary warning about lost spans on the path where there were simply no spans to lose. It
        # can also legitimately be someone else's provider, since `set_tracer_provider` is
        # set-once. `None` means telemetry was never wired, so there is nothing to flush and
        # nothing to warn about.
        if _tracer_provider is not None:
            try:
                _tracer_provider.force_flush()
            except Exception:
                # LOGGED, not swallowed — losing spans is invisible otherwise, and "traces are
                # empty" would be indistinguishable from "the agent was never invoked". A flush
                # fault must never fail the turn.
                logger.warning("OTEL span flush failed; buffered spans may be lost", exc_info=True)


if __name__ == "__main__":
    app.run()
