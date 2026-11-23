"""Strands Agent Template — Bedrock AgentCore Entry Point.

A barebones single agent with tools and conversation memory, plus on-behalf-of MCP wiring that
activates only when the platform grants this agent an MCP server (see the MCP section below).
Deploys to Bedrock AgentCore via BedrockAgentCoreApp.

Local: python -m src.main (starts on http://localhost:8080)
Docker: docker build -t my-agent . && docker run -p 8080:8080 my-agent
Test: curl -X POST http://localhost:8080/invocations -d '{"prompt": "hello"}'
"""

import ast
import base64
import contextlib
import json
import logging
import math
import operator
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import BedrockAgentCoreContext
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent, tool
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.models import BedrockModel
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool
from strands.tools.mcp.mcp_client import MCPClient
from strands.types.exceptions import ConcurrencyException
from strands.vended_plugins.skills import AgentSkills

from src.config import config
from src.mcp_runtime import namespace_tool_name, parse_mcp_servers

logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# --- Langfuse observability (E26/T10) ------------------------------------------------------
# EVERY agent provisioned from this template is observable with zero manual wiring: the platform
# provisions one Langfuse project + key PER AGENT and injects the NON-SECRET host + secret NAME
# into the runtime env (LANGFUSE_HOST / LANGFUSE_SECRET_NAME). This block reads the per-agent
# {public_key, secret_key} pair from AWS Secrets Manager and wires the Strands OTLP exporter from
# it — attribution is STRUCTURAL (each agent authenticates with its own project key, so traces
# land in its own project). The project key is NEVER a hardcoded literal; the secret VALUE lives
# ONLY in Secrets Manager and is never logged (the base64 auth IS the secret). Wired ONCE here,
# BEFORE the module-level Agent() below is constructed (research §1). Graceful degrade: if the
# env/secret is absent or unreadable, telemetry is simply not wired and the agent still runs.


# The SDK TracerProvider Strands wired, once telemetry is configured — the one the entrypoint's
# shutdown flush must target. Stays None on every degraded path, which is also the signal that
# there is nothing of ours to flush.
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

    # #1 TRAP: AgentCore auto-instruments with ADOT and owns the global OTEL TracerProvider first
    # (set-once) — disabling ADOT lets the Langfuse exporter win. Set only once Langfuse is configured.
    os.environ["DISABLE_ADOT_OBSERVABILITY"] = "true"
    # BASE endpoint form: the HTTP exporter auto-appends /v1/traces — do NOT add it here.
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"{host}/api/public/otel"
    os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Basic {lf_auth}"
    # Langfuse supports http/protobuf + http/json; gRPC is NOT supported.
    os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
    os.environ.setdefault(
        "OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental,gen_ai_tool_definitions"
    )
    os.environ.setdefault("OTEL_SERVICE_NAME", os.environ.get("AGENT_NAME", "strands-agentcore-agent"))
    # Strands' is_langfuse heuristic is a literal "langfuse" substring check on the OTEL endpoint /
    # LANGFUSE_BASE_URL; set LANGFUSE_BASE_URL so Input/Output render even behind a CloudFront alias.
    os.environ.setdefault("LANGFUSE_BASE_URL", f"{host}/langfuse")

    # Imported HERE, not at module top: Strands reads the OTEL env above at import time, so a
    # top-level import would capture the un-configured values.
    #
    # WRAPPED, because this tail runs at MODULE IMPORT and an exception here does not degrade
    # telemetry — it kills the agent before `app.run()` and the runtime crash-loops. That is not
    # hypothetical: the exporter dependency was missing from pyproject.toml, so this exact line
    # raised ModuleNotFoundError in every materialized agent whose Langfuse secret existed. The
    # missing dependency is fixed; this wrap is why the NEXT such fault costs a warning line
    # instead of an outage, because the failure mode is entirely one-sided — an agent with no
    # traces is degraded, an agent that will not start is down.
    # Logged WITHOUT the auth header/keys: `exc` is the import/exporter fault, and the secret only
    # ever reaches os.environ above — keep it that way, a traceback is not a place for a credential.
    global _tracer_provider
    try:
        from strands.telemetry import StrandsTelemetry

        # KEEP the StrandsTelemetry instance rather than constructing-and-discarding it: it OWNS
        # the SDK TracerProvider it just wired, exposed as `.tracer_provider`, and that object is
        # what the entrypoint's shutdown flush must target. `get_tracer_provider()` is NOT a
        # substitute — see the flush in the entrypoint for why.
        telemetry = StrandsTelemetry()
        telemetry.setup_otlp_exporter()
        _tracer_provider = telemetry.tracer_provider
    except Exception as exc:
        logger.warning("Langfuse telemetry disabled: OTLP exporter setup failed (%r)", exc, exc_info=True)


_setup_langfuse_telemetry()
# --- end Langfuse observability ------------------------------------------------------------


# --- CloudWatch traces (E42) ---------------------------------------------------------------
# A SECOND span processor on the same provider sends every span to CloudWatch as well, because
# AgentCore Evaluations judges this agent from its CloudWatch spans (README → "Evaluations"). This
# is NOT ADOT auto-instrumentation: it is one explicit OTLP exporter pointed at the X-Ray OTLP
# endpoint, signed with SigV4 from the runtime role. The signing is the same mechanism as
# aws-opentelemetry-distro's `OTLPAwsSpanExporter`
# (amazon/opentelemetry/distro/exporter/otlp/aws/traces/otlp_aws_span_exporter.py, whose
# AwsAuthSession in .../aws/common/aws_auth_session.py is a requests.Session that SigV4-signs each
# POST for service "xray"), written out here because the distro pins the OTEL SDK to one exact
# version plus ~50 instrumentations, which would fight the range this template's framework pins.
# On by default; AGP_CLOUDWATCH_TRACES=false turns it off. Any setup fault is logged and the agent
# runs without it, the same one-sided rule as the Langfuse block.


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
            # Langfuse is not wired, so nothing has built a provider yet: let Strands build one
            # (it becomes the global). Same ADOT rule as the Langfuse block — this module owns it.
            os.environ["DISABLE_ADOT_OBSERVABILITY"] = "true"
            os.environ.setdefault(
                "OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental,gen_ai_tool_definitions"
            )
            from strands.telemetry import StrandsTelemetry

            _tracer_provider = StrandsTelemetry().tracer_provider
        _tracer_provider.add_span_processor(_SessionIdProcessor())
        _tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
        logger.info("CloudWatch traces enabled (X-Ray OTLP endpoint, %s)", config.AWS_REGION)
    except Exception as exc:
        logger.warning(
            "CloudWatch traces disabled: X-Ray span exporter setup failed (%r)", exc, exc_info=True
        )


_setup_cloudwatch_traces()
# --- end CloudWatch traces -----------------------------------------------------------------

# --- Tools ---

_SAFE_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
}
_SAFE_FUNCTIONS = {"sqrt": math.sqrt, "abs": abs, "round": round}
_SAFE_CONSTANTS = {"pi": math.pi, "e": math.e}


def _safe_eval(node):
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.Name) and node.id in _SAFE_CONSTANTS:
        return _SAFE_CONSTANTS[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_OPERATORS:
        return _SAFE_OPERATORS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_OPERATORS:
        return _SAFE_OPERATORS[type(node.op)](_safe_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _SAFE_FUNCTIONS:
        return _SAFE_FUNCTIONS[node.func.id](*[_safe_eval(a) for a in node.args])
    raise ValueError(f"Unsupported: {ast.dump(node)}")


@tool
def calculator(expression: str) -> str:
    """Evaluate a mathematical expression safely.

    Args:
        expression: Math expression (e.g., 'sqrt(144) + 2 * 3')
    """
    try:
        result = _safe_eval(ast.parse(expression, mode="eval"))
        return str(result)
    except (ValueError, SyntaxError, ZeroDivisionError) as e:
        return f"Error: {e}"


@tool
def get_current_datetime() -> str:
    """Get the current date and time in UTC."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


# --- Agent ---

# The repo root (`/app` in the image). `prompt/` and `skills/` sit next to `src/` there, so a
# prompt or skill change is a file edit, never a handler change.
APP_ROOT = Path(__file__).resolve().parent.parent

# Used only when `prompt/system.md` is absent (e.g. a repo created before the file existed).
DEFAULT_SYSTEM_PROMPT = """You are a helpful assistant. You can:
- Answer questions conversationally
- Perform math calculations using the calculator tool
- Tell the current date and time

Be concise and helpful. Use tools when appropriate."""


def load_system_prompt(root: Path) -> str:
    """The whole of ``<root>/prompt/system.md`` when present, else the built-in default."""
    path = root / "prompt" / "system.md"
    return path.read_text(encoding="utf-8") if path.is_file() else DEFAULT_SYSTEM_PROMPT


def refusal_note(refused: list[str]) -> str:
    """One sentence telling the model which configured MCP servers refused it. ``""`` when none.

    Without it the model answers as if the tools never existed and guesses; with it, it says it
    cannot access the data, which is the honest answer before access is granted.
    """
    if not refused:
        return ""
    return (
        f"These MCP servers are configured for you but refused access on this request: "
        f"{', '.join(refused)}; if a question needs them, say you cannot access them instead of guessing."
    )


SYSTEM_PROMPT = load_system_prompt(APP_ROOT)

BUILTIN_TOOLS = [calculator, get_current_datetime]


def _build_agent(
    extra_tools: list | tuple = (),
    messages: list | None = None,
    trace_attributes: dict | None = None,
    model: BedrockModel | None = None,
    system_prompt: str | None = None,
) -> Agent:
    """Build the agent — once at import, and again per invoke only when MCP tools were wired.

    An MCP tool is a live object bound to a gateway connection that is opened and closed WITHIN a
    single invoke (see ``_mcp_tools``), so unlike the built-ins its tool list cannot be fixed at
    import time; handing the model MCP tools means rebuilding the agent inside the invoke that
    owns those connections.

    ``messages`` is passed the module-level agent's OWN list — strands stores the reference and
    appends to it in place — so a rebuilt agent CONTINUES the same conversation instead of starting
    a blank one. This template advertises conversation memory, and an agent that forgot the
    previous turn the moment an MCP was granted would be a silent regression.

    ``model`` is the module-level agent's own model on a rebuild, so an invoke never constructs a
    second Bedrock client. ``trace_attributes`` carries the per-caller Langfuse user id (see
    ``_caller_oid``); Strands applies it to every span the agent creates.

    ``system_prompt`` defaults to ``SYSTEM_PROMPT``; a rebuild passes it with a refusal note added.
    Skills come from ``skills/<name>/SKILL.md`` via Strands' ``AgentSkills`` plugin, which lists
    each skill's name + description in the prompt and adds a ``skills`` tool that loads the body on
    demand. With no skill folders the plugin is left off, so the model is not shown an empty tool.
    """
    skills_dir = APP_ROOT / "skills"
    plugins = [AgentSkills(skills=[str(skills_dir)])] if any(skills_dir.glob("*/SKILL.md")) else []
    return Agent(
        model=model
        or BedrockModel(
            model_id=config.MODEL_ID,
            region_name=config.AWS_REGION,
            temperature=0.3,
            max_tokens=4096,
        ),
        messages=messages,
        system_prompt=system_prompt or SYSTEM_PROMPT,
        tools=[*BUILTIN_TOOLS, *extra_tools],
        plugins=plugins,
        conversation_manager=SlidingWindowConversationManager(window_size=20),
        trace_attributes=trace_attributes,
    )


agent = _build_agent()

# One conversation (`agent.messages`) is shared by the module agent and every per-invoke rebuild,
# but strands' concurrency guard (THROW) only covers one Agent instance. This lock extends the
# same rule across all of them: a second concurrent invoke fails fast instead of interleaving turns.
_conversation_lock = threading.Lock()


# --- MCP tools (granted MCP servers) -------------------------------------------------------
# The platform can grant this agent access to one or more governed MCP servers. When it does, it
# injects MCP_SERVERS (a JSON list) + CREDENTIAL_PROVIDER_NAME into this runtime's environment;
# the code below turns those grants into real tools. WITH NO MCP GRANTED, NOTHING HERE RUNS AND
# THE AGENT IS UNAFFECTED — that is the normal state of a freshly scaffolded agent and it must
# stay a clean skip, not a startup requirement.
#
# The identity story is the point: the agent never holds a printable secret. The runtime injects a
# Workload Access Token that binds the INBOUND USER, and AgentCore Identity performs the Entra
# On-Behalf-Of exchange as the agent app using a client secret that stays in its Token Vault. The
# resulting MCP-audience token PRESERVES the user, so the MCP server authorizes the human who
# asked, not a service principal — the same access mechanism end to end.


def get_mcp_obo_token(*, provider_name: str, scopes: list[str], region: str, wat: str | None = None) -> str:
    """Obtain a DELEGATED (On-Behalf-Of, user-preserving) token for an MCP server's audience.

    ``oauth2Flow`` is hardcoded to ``ON_BEHALF_OF_TOKEN_EXCHANGE`` — the delegated flow. This uses
    the raw boto3 ``bedrock-agentcore`` data-plane client on purpose: the decorator-based path
    (``@requires_access_token``) and ``IdentityClient.get_token`` are typed
    ``Literal["M2M","USER_FEDERATION"]`` and CANNOT express OBO, so they would silently give you an
    APPLICATION identity instead of the caller's. If you ever genuinely want autonomous (non-user)
    access to an MCP, that is a deliberate one-line change to ``"M2M"`` here, not a default.

    ``wat`` lets one Workload Access Token be REUSED across several OBO exchanges: the WAT binds the
    same inbound user for every MCP, so it is fetched once per invoke and only the outbound
    MCP-audience token is per-MCP. When omitted it is fetched here.
    """
    dp = boto3.client("bedrock-agentcore", region_name=region)
    # Runtime-injected; binds the inbound USER (present on a JWT-inbound runtime).
    if wat is None:
        wat = BedrockAgentCoreContext.get_workload_access_token()
    return dp.get_resource_oauth2_token(
        workloadIdentityToken=wat,
        resourceCredentialProviderName=provider_name,
        oauth2Flow="ON_BEHALF_OF_TOKEN_EXCHANGE",
        scopes=scopes,
    )["accessToken"]


def _namespaced_tool(raw: MCPAgentTool, label: str) -> MCPAgentTool:
    """Re-wrap a discovered MCP tool so the model sees a server-namespaced name.

    Strands' ``MCPAgentTool`` decouples the agent-facing name from the wire name by design:
    ``name_override`` sets what the model is shown, while ``stream()`` always calls the gateway with
    the original ``mcp_tool.name``. So namespacing costs nothing on the wire, and it buys two
    things: two MCP servers can expose a ``search`` tool without colliding, and every tool call in
    the traces names the server it went to. Uses the platform's ``{label}__{name}`` form (DOUBLE
    underscore) — not the SDK's blanket ``prefix=``, which joins with a single one.
    """
    return MCPAgentTool(
        raw.mcp_tool,
        raw.mcp_client,
        name_override=namespace_tool_name(label, raw.tool_name),
    )


def _mcp_environ() -> dict[str, str]:
    """The env keys ``parse_mcp_servers`` resolves, read through ``config`` like everything else."""
    return {
        "MCP_SERVERS": config.MCP_SERVERS,
        "MCP_AUDIENCE": config.MCP_AUDIENCE,
        "MCP_GATEWAY_URL": config.MCP_GATEWAY_URL,
    }


def _mcp_tools(stack: contextlib.ExitStack, refused: list | None = None) -> list:
    """Connect to every granted MCP server and return its tools, namespaced. ``[]`` when none.

    A server whose token exchange or connection fails is dropped AND its label appended to
    ``refused``, so the handler can tell the model (see ``refusal_note``).

    DEGRADE, NEVER CRASH — every failure mode here returns fewer tools, never an exception:
    unconfigured, misconfigured, an unreachable gateway, a refused token. A tool the model cannot
    reach makes an answer worse; an invoke that raises makes it absent, and a startup that raises
    makes the whole agent a crash-loop. Weigh any change here on that asymmetry.

    Each ``MCPClient`` is entered on the CALLER's ``ExitStack`` because the connections must stay
    open for as long as the agent might call a tool — i.e. for the whole stream — and the number of
    them is only known at runtime, so hand-nested ``with`` blocks are not an option.
    """
    servers = parse_mcp_servers(_mcp_environ())
    if not servers:
        logger.info("No MCP configured (MCP_SERVERS empty/absent) — running prompt-only.")
        return []

    provider_name = config.CREDENTIAL_PROVIDER_NAME
    if not provider_name:
        # Servers granted but no credential provider to exchange a token with: there is no
        # user-preserving token to be had, so drop the tools rather than fall back to some other
        # identity. Loud, because this combination means the platform's provisioning is incomplete.
        logger.warning(
            "%d MCP server(s) configured but CREDENTIAL_PROVIDER_NAME is unset — "
            "cannot mint an on-behalf-of token; running prompt-only (degrade).",
            len(servers),
        )
        if refused is not None:
            refused += [s.get("label") or s.get("id") for s in servers]
        return []

    # One WAT binds the inbound user for every MCP, so fetch it once. On failure leave it None:
    # each per-server call then re-fetches its own, so the loop still degrades per server.
    try:
        wat = BedrockAgentCoreContext.get_workload_access_token()
    except Exception as wat_exc:
        logger.warning(
            "Workload access token fetch failed — per-MCP fetch will retry: %r", wat_exc, exc_info=True
        )
        wat = None

    tools: list = []
    for server in servers:
        try:
            token = get_mcp_obo_token(
                provider_name=provider_name,
                scopes=[server["audience"] + "/.default"],
                region=config.AWS_REGION,
                wat=wat,
            )
            # Bound as lambda defaults, not closure reads: the factory is called later, by which
            # point a closure over the loop variable would name the LAST server for every client.
            client = MCPClient(
                lambda t=token, u=server["gateway_url"]: streamablehttp_client(
                    u, headers={"Authorization": f"Bearer {t}"}
                )
            )
            stack.enter_context(client)
            tools += [_namespaced_tool(t, server["label"]) for t in client.list_tools_sync()]
        except Exception as exc:
            logger.warning(
                "MCP %s unavailable this invoke — dropping (degrade): %r",
                server.get("id"),
                exc,
                exc_info=True,
            )
            if refused is not None:
                refused.append(server.get("label") or server.get("id"))
            continue

    return tools


# --- end MCP tools -------------------------------------------------------------------------


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


# --- AgentCore App ---

app = BedrockAgentCoreApp()


@app.entrypoint
async def handler(payload: dict, context=None):
    """Handle agent invocations.

    Payload: ``{"prompt": "user message"}``

    Response: an SSE stream of two main event shapes, because it has two kinds of reader — a streaming
    UI consumes the incremental tokens, a buffering client reads the terminal message:

    * ``{"type": "content", "data": "<token>"}``
    * ``{"message": {"role": "assistant", "content": [{"text": "<full answer>"}]}}`` — terminal,
      TEXT-BEARING ONLY. Errors: ``{"type": "error", "data": "<message>"}``.
    * ``{"tool_calls": [{"name": ..., "args": {...}}]}`` — last, on success only: the tools this
      invoke called, in order, for evaluations (README → "Evaluations").

    **The contract, with the reasoning for both rules, is documented in README.md → "Response
    contract".** That is its home: the README ships into every materialized repository, so it is
    what a customer editing this handler actually reads. Read it before changing what is yielded.
    In short: always emit the terminal message (chunks alone make a buffering client show one
    token as the answer), and never forward a textless ``toolUse``/``toolResult`` message (it
    becomes the client's "last message" and masks a later error).

    MCP: if the platform has granted this agent access to governed MCP servers, they are connected
    at the top of each invoke and their tools are added to the built-ins for that invoke (see the
    MCP section above). This is per-invoke and not at import because the tokens are user-preserving:
    they are minted from the identity of whoever is calling right now. With no grant it is a clean
    skip and the module-level agent answers, which is why this template runs as-is.

    Also not forwarded: the ``{"result": AgentResult}`` event Strands yields after the terminal
    message. It is result-shaped, which a buffering client also treats as terminal, so it would
    win the last-match and replace the answer with a stop_reason/metrics blob. (Today it happens
    to serialize to a Python ``repr`` string — AgentCore falls back to ``json.dumps(str(obj))``
    because ``EventLoopMetrics`` holds a ``Trace`` — so it merely puts garbage on the wire rather
    than breaking extraction. Do not rely on that: it is an accident of what is unserializable
    this release, not a guarantee.)
    """
    prompt = payload.get("prompt", "")
    if not prompt:
        yield {"type": "error", "data": "'prompt' field is required"}
        return

    logger.info("Invocation received: %s...", prompt[:50])
    try:
        # The ExitStack holds every granted MCP server's connection open for exactly as long as the
        # stream can call a tool, and closes them all on the way out — including on error. With no
        # MCP granted it holds nothing, `_mcp_tools` returns `[]`, and the module-level `agent`
        # (built once at import) answers; only its trace attributes are set per invoke.
        # A refused server forces a rebuild too: the refusal note goes into THIS invoke's system
        # prompt only, and the module agent's prompt is never edited in place.
        with contextlib.ExitStack() as stack:
            if not _conversation_lock.acquire(blocking=False):
                raise ConcurrencyException(
                    "Agent is already processing a request. Concurrent invocations are not supported."
                )
            stack.callback(_conversation_lock.release)
            refused: list[str] = []
            mcp_tools = _mcp_tools(stack, refused)
            oid = _caller_oid(payload)
            # Keys are the Langfuse OTEL mapping spelling (`langfuse.user.id`, not `user.id`).
            trace_attributes = {"langfuse.user.id": oid} if oid else {}
            if mcp_tools or refused:
                active_agent = _build_agent(
                    mcp_tools,
                    messages=agent.messages,
                    trace_attributes=trace_attributes,
                    model=agent.model,
                    system_prompt="\n\n".join(filter(None, [SYSTEM_PROMPT, refusal_note(refused)])),
                )
            else:
                # Strands reads `trace_attributes` at every span, so setting it per invoke tags this
                # call without a rebuild — and resetting it to {} keeps the previous caller's id off
                # an anonymous call. The module agent's concurrency guard (THROW) still applies.
                agent.trace_attributes = trace_attributes
                active_agent = agent
            tool_calls: list[dict] = []
            async for event in active_agent.stream_async(prompt):
                if "data" in event:
                    yield {"type": "content", "data": event["data"]}
                elif "message" in event:
                    # Text-bearing only — a toolUse/toolResult message forwarded here would become
                    # the buffering client's "last message" and mask a later error (see docstring).
                    content = event["message"].get("content", [])
                    # The toolUse blocks are this invoke's tool calls, which evaluations check. A
                    # block without a name is skipped: the backend's ToolCall needs one.
                    tool_calls += [
                        {"name": p["toolUse"]["name"], "args": p["toolUse"].get("input") or {}}
                        for p in content
                        if isinstance(p, dict)
                        and isinstance(p.get("toolUse"), dict)
                        and isinstance(p["toolUse"].get("name"), str)
                    ]
                    if any(isinstance(p, dict) and "text" in p for p in content):
                        yield {"message": event["message"]}
            # After the terminal message, success only. Neither message- nor result-shaped, so a
            # buffering client's last-match still lands on the answer.
            yield {"tool_calls": tool_calls}
    except Exception as e:
        logger.exception("Agent error")
        yield {"type": "error", "data": str(e)}
    finally:
        # Force-flush OTEL spans before the AgentCore microVM freezes (research §0.5): the
        # background BatchSpanProcessor would otherwise lose buffered Langfuse spans. Best-effort.
        #
        # Flushes THE PROVIDER STRANDS WIRED (captured above), not `get_tracer_provider()`. The
        # global is a `ProxyTracerProvider` whenever telemetry is unwired — and it has no
        # `force_flush` attribute at all, so the old unconditional call raised AttributeError on
        # EVERY invocation and logged a scary warning about lost spans on the path where there were
        # simply no spans to lose. It can also legitimately be someone else's provider, since
        # `set_tracer_provider` is set-once. `None` means telemetry was never wired, so there is
        # nothing to flush and nothing to warn about — hence the guard is OUTSIDE the try, and
        # there is deliberately no fallback to the global.
        if _tracer_provider is not None:
            try:
                _tracer_provider.force_flush()
            except Exception:
                # Best-effort: a flush fault must never fail the turn. LOGGED, not swallowed —
                # losing spans is invisible otherwise, and "traces are empty" would be
                # indistinguishable from "the agent was never invoked".
                logger.warning("OTEL span flush failed; buffered spans may be lost", exc_info=True)


if __name__ == "__main__":
    app.run()
