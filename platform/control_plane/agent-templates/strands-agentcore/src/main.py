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
from datetime import UTC, datetime

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import BedrockAgentCoreContext
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent, tool
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.models import BedrockModel
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool
from strands.tools.mcp.mcp_client import MCPClient

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
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the agent
        logger.warning("Langfuse telemetry disabled: could not read secret %s (%r)", secret_name, exc)
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
    # top-level import would capture the un-configured values. (No `noqa: E402` — that rule is
    # about module-level imports and never applied to a function-local one.)
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
    try:
        from strands.telemetry import StrandsTelemetry

        StrandsTelemetry().setup_otlp_exporter()
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the agent
        logger.warning("Langfuse telemetry disabled: OTLP exporter setup failed (%r)", exc)


_setup_langfuse_telemetry()
# --- end Langfuse observability ------------------------------------------------------------

# --- Tools ---

_SAFE_OPERATORS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Pow: operator.pow, ast.USub: operator.neg,
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

SYSTEM_PROMPT = """You are a helpful assistant. You can:
- Answer questions conversationally
- Perform math calculations using the calculator tool
- Tell the current date and time

Be concise and helpful. Use tools when appropriate."""

BUILTIN_TOOLS = [calculator, get_current_datetime]


def _build_agent(extra_tools: list | tuple = (), messages: list | None = None) -> Agent:
    """Build the agent — once at import, and again per invoke only when MCP tools were wired.

    An MCP tool is a live object bound to a gateway connection that is opened and closed WITHIN a
    single invoke (see ``_mcp_tools``), so unlike the built-ins its tool list cannot be fixed at
    import time; handing the model MCP tools means rebuilding the agent inside the invoke that
    owns those connections.

    ``messages`` is passed the module-level agent's OWN list — strands stores the reference and
    appends to it in place — so a rebuilt agent CONTINUES the same conversation instead of starting
    a blank one. This template advertises conversation memory, and an agent that forgot the
    previous turn the moment an MCP was granted would be a silent regression.
    """
    return Agent(
        model=BedrockModel(
            model_id=config.MODEL_ID,
            region_name=config.AWS_REGION,
            temperature=0.3,
            max_tokens=4096,
        ),
        messages=messages,
        system_prompt=SYSTEM_PROMPT,
        tools=[*BUILTIN_TOOLS, *extra_tools],
        conversation_manager=SlidingWindowConversationManager(window_size=20),
    )


agent = _build_agent()


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


def _mcp_tools(stack: contextlib.ExitStack) -> list:
    """Connect to every granted MCP server and return its tools, namespaced. ``[]`` when none.

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
        return []

    # One WAT binds the inbound user for every MCP, so fetch it once. On failure leave it None:
    # each per-server call then re-fetches its own, so the loop still degrades per server.
    try:
        wat = BedrockAgentCoreContext.get_workload_access_token()
    except Exception as wat_exc:  # noqa: BLE001 — degrade; the per-MCP fetch below will retry
        logger.warning("Workload access token fetch failed — per-MCP fetch will retry: %r", wat_exc)
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
        except Exception as exc:  # noqa: BLE001 — DEGRADE: drop this MCP, keep the rest
            logger.warning(
                "MCP %s unavailable this invoke — dropping (degrade): %r", server.get("id"), exc
            )
            continue

    return tools


# --- end MCP tools -------------------------------------------------------------------------

# --- AgentCore App ---

app = BedrockAgentCoreApp()


@app.entrypoint
async def handler(payload: dict, context=None):
    """Handle agent invocations.

    Payload: ``{"prompt": "user message"}``

    Response: an SSE stream of two event shapes, because it has two kinds of reader — a streaming
    UI consumes the incremental tokens, a buffering client reads the terminal message:

    * ``{"type": "content", "data": "<token>"}``
    * ``{"message": {"role": "assistant", "content": [{"text": "<full answer>"}]}}`` — terminal,
      TEXT-BEARING ONLY. Errors: ``{"type": "error", "data": "<message>"}``.

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
        # (built once at import) is used unchanged: the bare template's behaviour is untouched.
        with contextlib.ExitStack() as stack:
            mcp_tools = _mcp_tools(stack)
            active_agent = _build_agent(mcp_tools, messages=agent.messages) if mcp_tools else agent
            async for event in active_agent.stream_async(prompt):
                if "data" in event:
                    yield {"type": "content", "data": event["data"]}
                elif "message" in event:
                    # Text-bearing only — a toolUse/toolResult message forwarded here would become
                    # the buffering client's "last message" and mask a later error (see docstring).
                    content = event["message"].get("content", [])
                    if any(isinstance(p, dict) and "text" in p for p in content):
                        yield {"message": event["message"]}
    except Exception as e:
        logger.exception("Agent error")
        yield {"type": "error", "data": str(e)}
    finally:
        # Force-flush OTEL spans before the AgentCore microVM freezes (research §0.5): the
        # background BatchSpanProcessor would otherwise lose buffered Langfuse spans. Best-effort.
        try:
            from opentelemetry import trace as _trace

            _trace.get_tracer_provider().force_flush()
        except Exception:
            # Best-effort: a flush fault must never fail the turn. LOGGED, not swallowed — losing
            # spans is invisible otherwise, and "traces are empty" would be indistinguishable from
            # "the agent was never invoked". (No `noqa` needed: BLE001 accepts a blind except that
            # logs the exception, which is why the bare `pass` this replaced was the real defect.)
            logger.warning("OTEL span flush failed; buffered spans may be lost", exc_info=True)


if __name__ == "__main__":
    app.run()
