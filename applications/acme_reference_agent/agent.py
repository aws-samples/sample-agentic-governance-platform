"""Acme reference agent.

The Strands agent on AgentCore Runtime that proves agent-to-MCP access uses the same mechanism
as user-to-agent access.

The platform invokes the agent with the user's identity; for every granted MCP server the agent asks
AgentCore Identity for a delegated (On-Behalf-Of) token for that server's Entra audience and calls the
gateway's tools over MCP Streamable HTTP. The agent holds no secret: the agent app's client secret
lives in AgentCore's Token Vault as the credential provider the platform creates at grant time.

The token call uses ``oauth2Flow="ON_BEHALF_OF_TOKEN_EXCHANGE"`` and nothing else: no M2M branch, no
flow switch. ``@requires_access_token`` and ``IdentityClient.get_token`` type only ``M2M`` and
``USER_FEDERATION``, so the delegated flow is reachable only through the raw ``bedrock-agentcore``
data-plane client. Autonomous operation would be a one-line change of ``oauth2Flow`` to ``"M2M"``.

A separate deployable: ``requirements.txt`` pins ``strands-agents``, ``bedrock-agentcore`` and ``mcp``,
nothing is imported from the control plane, and ``deploy.sh`` with ``README.md`` carry the runbook.
"""

import base64
import contextlib
import json
import logging
import os

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import BedrockAgentCoreContext
from mcp.client.streamable_http import streamablehttp_client
from opentelemetry import trace as otel_trace
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool
from strands.tools.mcp.mcp_client import MCPClient

from mcp_runtime import namespace_tool_name, parse_mcp_servers

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

# botocore logs every response body at DEBUG (botocore/parsers.py), and the GetSecretValue body read
# below is the Langfuse key; INFO is a floor for the wire libraries, a quieter LOG_LEVEL stands.
for _wire_logger_name in ("boto3", "botocore", "urllib3", "httpx", "httpcore", "s3transfer"):
    _wire_logger = logging.getLogger(_wire_logger_name)
    if _wire_logger.getEffectiveLevel() < logging.INFO:
        _wire_logger.setLevel(logging.INFO)

logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"


def get_mcp_obo_token(*, provider_name: str, scopes: list[str], region: str, wat: str | None = None) -> str:
    """A delegated (On-Behalf-Of) token for an MCP server's audience, as the inbound user.

    The runtime's workload access token binds the inbound user; AgentCore Identity performs the Entra
    OBO exchange as the agent app, whose secret stays in the Token Vault, and returns an MCP-audience
    token that keeps the user's ``oid``/``sub``/``upn``. ``wat`` lets one workload access token serve
    several exchanges in one invoke; when omitted it is fetched here.
    """
    dp = boto3.client("bedrock-agentcore", region_name=region)
    if wat is None:
        wat = BedrockAgentCoreContext.get_workload_access_token()
    return dp.get_resource_oauth2_token(
        workloadIdentityToken=wat,
        resourceCredentialProviderName=provider_name,
        oauth2Flow="ON_BEHALF_OF_TOKEN_EXCHANGE",
        scopes=scopes,
    )["accessToken"]


def _namespaced_tool(raw: MCPAgentTool, label: str) -> MCPAgentTool:
    """The discovered tool under its server-namespaced name, ``{label}__{name}``.

    ``MCPAgentTool.name_override`` changes only the name the model sees: ``stream()`` still calls the
    gateway with ``mcp_tool.name``, and the tool keeps ``raw.mcp_client``, the live client in the stack.
    """
    return MCPAgentTool(
        raw.mcp_tool,
        raw.mcp_client,
        name_override=namespace_tool_name(label, raw.tool_name),
    )


_LF_SERVICE_NAME = "acme-reference-agent"


def _setup_langfuse_telemetry() -> None:
    """Wire the Langfuse OTLP exporter from this agent's own Langfuse project key.

    ``LANGFUSE_HOST`` and ``LANGFUSE_SECRET_NAME`` name the host and the Secrets Manager secret holding
    ``{public_key, secret_key}``; the platform injects both when it provisions the agent's project. The
    OTEL_* environment the Strands exporter reads is set here, once, before any Agent is built. When
    either variable is unset or the secret cannot be read, telemetry stays off and the agent runs; the
    key values and the Authorization header are never logged.
    """
    host = os.environ.get("LANGFUSE_HOST", "").rstrip("/")
    secret_name = os.environ.get("LANGFUSE_SECRET_NAME", "")
    if not host or not secret_name:
        logger.info("Langfuse telemetry disabled: LANGFUSE_HOST / LANGFUSE_SECRET_NAME not set.")
        return

    try:
        region = os.environ.get("AWS_REGION", "us-east-1")
        sm = boto3.client("secretsmanager", region_name=region)
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

    lf_auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()

    # AgentCore's ADOT auto-instrumentation owns the global TracerProvider (set once), so a later
    # exporter is ignored unless ADOT is off; set only once Langfuse is known to be configured
    # (AgentCore developer guide, Observability:
    # https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/).
    os.environ["DISABLE_ADOT_OBSERVABILITY"] = "true"
    # The OTLP/HTTP exporter appends /v1/traces to the base endpoint itself:
    # https://opentelemetry.io/docs/specs/otel/protocol/exporter/#endpoint-urls-for-otlphttp
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"{host}/api/public/otel"
    os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Basic {lf_auth}"
    # Langfuse takes http/protobuf and http/json, not gRPC: https://langfuse.com/docs/opentelemetry/get-started
    os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
    os.environ.setdefault(
        "OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental,gen_ai_tool_definitions"
    )
    os.environ.setdefault("OTEL_SERVICE_NAME", _LF_SERVICE_NAME)
    # Strands promotes prompt and completion content onto span attributes only when its is_langfuse
    # check finds "langfuse" in an OTEL endpoint or in LANGFUSE_BASE_URL (strands/telemetry/tracer.py);
    # the CloudFront alias host carries no such substring, and this variable serves that check alone.
    os.environ.setdefault("LANGFUSE_BASE_URL", f"{host}/langfuse")

    from strands.telemetry import StrandsTelemetry

    StrandsTelemetry().setup_otlp_exporter()


_setup_langfuse_telemetry()


def _workload_access_token() -> str | None:
    """The inbound user's workload access token, fetched once and reused for every OBO exchange.

    ``None`` when the fetch fails: ``get_mcp_obo_token`` then fetches its own per server, so a failure
    here degrades per server rather than ending the invoke.
    """
    try:
        return BedrockAgentCoreContext.get_workload_access_token()
    except Exception as wat_exc:
        logger.warning(
            "workload access token fetch failed — per-MCP fetch will retry: %r", wat_exc, exc_info=True
        )
        return None


def _wire_mcp_tools(stack: contextlib.ExitStack, servers: list[dict], *, region: str) -> list:
    """Open one MCP client per granted server inside ``stack`` and return the tools of every server
    that answered, advertised to the model as ``{label}__{name}``.

    A server whose token, connection or tool listing fails is dropped with a warning and the rest
    still load; the clients stay open until ``stack`` closes, after the agent has run.
    """
    wat = _workload_access_token()
    provider_name = os.environ["CREDENTIAL_PROVIDER_NAME"]
    all_tools: list = []
    for s in servers:
        try:
            token = get_mcp_obo_token(
                provider_name=provider_name, scopes=[s["audience"] + "/.default"], region=region, wat=wat
            )
            client = MCPClient(
                lambda t=token, u=s["gateway_url"]: streamablehttp_client(
                    u, headers={"Authorization": f"Bearer {t}"}
                )
            )
            stack.enter_context(client)
            all_tools += [_namespaced_tool(t, s["label"]) for t in client.list_tools_sync()]
        except Exception as exc:
            logger.warning(
                "MCP %s unavailable this invoke — dropping (degrade): %r", s.get("id"), exc, exc_info=True
            )
            continue
    return all_tools


def _flush_spans() -> None:
    """Export the buffered OTEL spans now: the AgentCore microVM freezes right after the response and
    the batch processor exports in the background, so an unflushed trace never reaches Langfuse.

    Best-effort: a provider without ``force_flush`` is the unconfigured state and is skipped; a flush
    that fails is logged and never turns an answered invoke into an error.
    """
    try:
        flush = getattr(otel_trace.get_tracer_provider(), "force_flush", None)
        if flush is not None:
            flush()
    except Exception:
        logger.warning("span flush failed", exc_info=True)


app = BedrockAgentCoreApp()


@app.entrypoint
async def invoke(payload):
    """Run the agent over ``{"prompt": "..."}`` with the tools of every granted MCP server.

    ``MCP_SERVERS`` lists the granted servers; for each the agent takes a delegated (OBO) token for the
    server's audience, connects to its gateway and discovers its tools,
    advertised to the model as ``{label}__{name}``.
    A server that fails to wire is dropped and the rest still load; with no server granted the agent runs
    prompt-only. Every event is streamed as an SSE frame by the runtime, and the spans are flushed once
    the stream has drained.
    """
    region = os.environ["AWS_REGION"]
    model = BedrockModel(
        model_id=os.environ.get("MODEL_ID", DEFAULT_MODEL_ID),
        region_name=region,
    )
    prompt = payload.get("prompt", "")
    trace_attributes: dict = {
        "langfuse.trace.tags": ["acme-reference", "prototype"],
        "langfuse.trace.name": "reference-agent-invocation",
    }

    try:
        servers = parse_mcp_servers(os.environ)
        if servers:
            with contextlib.ExitStack() as stack:
                all_tools = _wire_mcp_tools(stack, servers, region=region)
                agent = Agent(model=model, tools=all_tools, trace_attributes=trace_attributes)
                async for event in agent.stream_async(prompt):
                    yield event
        else:
            logger.info("No MCP configured (MCP_SERVERS empty/absent) — running prompt-only.")
            agent = Agent(model=model, trace_attributes=trace_attributes)
            async for event in agent.stream_async(prompt):
                yield event
    finally:
        _flush_spans()


if __name__ == "__main__":
    app.run()
