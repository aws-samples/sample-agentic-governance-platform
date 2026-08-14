"""Acme First Notification of Loss (FNOL) agent — the C-3 consumer (Epic 7, Tier 2, Task T-REF-AGENT).

This is a Strands-on-AgentCore-Runtime agent the user deploys to close the
**C-3 loop**: "agent->MCP uses the same access mechanism as user->agent." When the
platform invokes this agent with a real user's identity, the agent asks **AgentCore
Identity** for a *delegated* (On-Behalf-Of, user-preserving) token for the MCP server's
Entra audience, then uses Strands' built-in MCP client to list and call the gateway's
tools over the MCP Streamable-HTTP protocol. The agent never holds a printable secret —
the agent app's client secret lives in AgentCore's Token Vault (provisioned by the
control-plane at grant time as a ``MicrosoftOauth2`` credential provider); the agent only
calls a data-plane token util.

THE OBO DECISION (research §3, plan §5 / Tier-2 header — read this before changing it):
  The token call hardcodes ``oauth2Flow="ON_BEHALF_OF_TOKEN_EXCHANGE"`` (delegated/OBO).
  There is DELIBERATELY **no** env-var flow switch, **no** M2M branch, **no** dual-mode
  anything — that would be complexity built to hedge a bet not yet lost. The
  ``@requires_access_token`` decorator + ``IdentityClient.get_token`` are typed
  ``Literal["M2M","USER_FEDERATION"]`` and CANNOT do OBO (research §3.3, the key
  correction) — so the OBO path is reachable only via the **raw boto3**
  ``get_resource_oauth2_token(...)`` call below (the ``bedrock-agentcore`` data-plane
  client accepts the ``ON_BEHALF_OF_TOKEN_EXCHANGE`` string). If, at live bring-up, the
  Preview OBO path turns out not to preserve the user's identity, switching to autonomous
  is a deliberate ONE-LINE pivot made THEN (change ``oauth2Flow`` to ``"M2M"``) — it is
  documented in the README but intentionally NOT pre-built here.

SEPARATE DEPLOYABLE (research Decision 7, plan §5):
  This package has its OWN ``requirements.txt`` (``strands-agents`` / ``bedrock-agentcore``
  / ``mcp``) and imports NOTHING from the control-plane backend — those deps are NOT in the
  backend image. It is deliberately kept OUT of the AGP control plane; the user deploys it
  standalone via ``deploy.sh`` (see ``README.md`` for the runbook).

Mechanics source: research §3.3 (the raw-boto3 OBO helper), §4.2 (the
``BedrockAgentCoreApp`` + Strands ``MCPClient`` skeleton).
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
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool
from strands.tools.mcp.mcp_client import MCPClient

from mcp_runtime import namespace_tool_name, parse_mcp_servers

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

# SECURITY — DO NOT REMOVE. `basicConfig` above configures the ROOT logger, so the README's
# documented `LOG_LEVEL=DEBUG` would otherwise switch DEBUG on for every library in the process.
# `botocore.parsers` then logs each response verbatim (`LOG.debug("Response body:\n%r", ...)`),
# and `_setup_langfuse_telemetry()` below reads this agent's Langfuse public/secret key with
# boto3 `get_secret_value` — a `GetSecretValue` response body IS the secret, so the key would land
# in CloudWatch. Flooring the library loggers at INFO keeps `LOG_LEVEL=DEBUG` usable for OUR code
# without enabling anyone else's payload logging. A FLOOR: a quieter LOG_LEVEL is left alone.
for _wire_logger_name in ("boto3", "botocore", "urllib3", "httpx", "httpcore", "s3transfer"):
    _wire_logger = logging.getLogger(_wire_logger_name)
    if _wire_logger.getEffectiveLevel() < logging.INFO:
        _wire_logger.setLevel(logging.INFO)

logger = logging.getLogger(__name__)

# The Bedrock model the agent reasons with. Cross-region inference profile id — the repo
# idiom (53 uses across templates). Swappable for any current Anthropic Claude model id
# valid on Bedrock via the MODEL_ID env var.
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"

# The agent's persona. This is the ONLY thing that distinguishes this agent from the
# reference clone — the OBO/MCP wiring below is identical, so granting this agent an MCP in
# the platform auto-wires its tools with zero code change.
SYSTEM_PROMPT = """You are the Acme First Notification of Loss (FNOL) Assistant. You are \
the very first point of contact when an Acme customer reports a loss, incident, or \
accident — a car crash, a home break-in or flood, a travel mishap, damaged property. The \
customer may be shaken, upset, or in shock. Your job is to look after them and to capture \
an accurate first report of what happened.

SAFETY FIRST — ALWAYS YOUR FIRST STEP
- Before anything else, check that everyone is safe. If anyone is injured, in danger, or if \
there is a fire, a crime in progress, or a medical emergency, tell the customer to contact \
the emergency services (call your local emergency number, e.g. 999/112) right away, and to \
come back to start the claim once everyone is safe. Never delay a safety need to gather \
claim details.

TONE & VOICE — EMPATHY FIRST
- Lead with genuine care. Acknowledge what has happened and reassure the customer they are \
in the right place and you will help them through the first steps. Stay calm, patient, and \
unhurried. Use plain, gentle language. Let them tell their story; do not interrogate.

KEY BEHAVIORS & WORKFLOW
1. Open with empathy and the safety check above.
2. Then capture the incident in a clear, structured way, one step at a time:
   - WHAT happened (a short description in the customer's own words);
   - WHEN it happened (date and approximate time);
   - WHERE it happened (location / address);
   - WHO and WHAT was involved (parties and their details where known, any vehicles, \
property, or items, registration/identifiers);
   - DAMAGE observed and whether there are any INJURIES;
   - any POLICE / fire / crime reference number or third-party or witness details.
3. Confirm the details back to the customer in a brief summary and correct anything they \
flag. Accuracy of this first record matters.
4. Set clear expectations: explain in general terms what happens next in the claims \
process, that a claims handler will take it forward, and roughly what they can expect — \
without committing to timelines or outcomes.
5. Tell the customer you are passing the report to a claims handler, and close with care.

INSURANCE GUARDRAILS
- NEVER admit or deny liability or fault — for the customer, for Acme, or for any third \
party. Record what the customer reports as their account; do not judge it.
- NEVER promise that a claim will be paid or covered, and never estimate, suggest, or hint \
at a payout figure. You do not make coverage determinations — a claims handler assesses the \
claim.
- Do not provide legal, medical, or financial advice. If the customer asks whether to see a \
doctor or a lawyer, encourage them to seek the appropriate professional; explain, do not \
advise.
- Escalate to a human colleague when the situation is complex, is a complaint, involves a \
vulnerable or highly distressed customer, a serious injury or fatality, suspected fraud, or \
anything outside taking a first report.
- Protect customer data: collect only what is needed to record the incident, never reveal \
another person's information, and handle personal and incident data carefully.
- Stay in-domain. You only take first reports of loss for Acme insurance; politely \
redirect anything else (e.g. general billing or sales) to the contact centre."""


def get_mcp_obo_token(
    *, provider_name: str, scopes: list[str], region: str, wat: str | None = None
) -> str:
    """Obtain a DELEGATED (On-Behalf-Of, user-preserving) MCP-audience token.

    The runtime injects a Workload Access Token (WAT) that binds the inbound USER's
    identity; AgentCore Identity then performs Entra OBO AS THE AGENT APP (using the
    agent app's secret, which stays vaulted) to mint an MCP-audience token that PRESERVES
    the user (``oid``/``sub``/``upn``). Returns the raw access token string.

    ``oauth2Flow`` is hardcoded to ``ON_BEHALF_OF_TOKEN_EXCHANGE`` — the delegated flow.
    The decorator-based path (``@requires_access_token``) cannot reach OBO (research §3.3),
    so this uses the raw boto3 ``bedrock-agentcore`` data-plane client directly.

    ``wat`` may be supplied to REUSE one Workload Access Token across several OBO exchanges
    (E12 multi-MCP: the WAT binds the same inbound user, so it is fetched ONCE per invoke and
    reused for every MCP — only the outbound MCP-audience token is per-MCP). When omitted, the
    WAT is fetched here (the original single-MCP behaviour, unchanged).
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
    """Re-wrap a discovered MCP tool so the model sees a server-namespaced name (E12, §3.3).

    Strands' ``MCPAgentTool`` (verified against strands-agents 1.44.0, the version pulled by
    ``requirements.txt`` ``strands-agents>=1.38.0,<2.0``) DECOUPLES the agent-facing name from
    the wire name by design:
      - ``__init__`` accepts ``name_override`` and stores ``name_override or mcp_tool.name``;
        ``tool_name`` / ``tool_spec["name"]`` return that agent-facing name (``# Use
        agent-facing name in spec``).
      - ``stream()`` ALWAYS calls the gateway with ``name=self.mcp_tool.name`` (``# Use original
        MCP name for server communication``) — the override never reaches the wire.

    So namespacing is just the SDK's native ``name_override``: we rebuild the tool with
    ``namespace_tool_name(label, raw.tool_name)`` (``{label}__{name}``, double underscore — our
    backend contract; NOT the SDK's blanket ``prefix=`` which uses a single ``_``). It shares
    ``raw.mcp_client``, so invocation still routes through the live client opened in the loop
    below, and the gateway receives the unchanged original tool name.
    """
    return MCPAgentTool(
        raw.mcp_tool,
        raw.mcp_client,
        name_override=namespace_tool_name(label, raw.tool_name),
    )


def _decode_jwt_claims(token: str) -> dict:
    """Base64url-decode a JWT's payload segment WITHOUT signature verification — for
    DEBUG/telemetry only. Returns ``{}`` on any failure (never raises).

    Mirrors the repo's existing no-verify decode idiom (control-plane ``core/rbac.py``
    lines 49-52: split on ``.``, take the payload segment, pad to a multiple of 4,
    base64-decode, ``json.loads``). Uses ``urlsafe_b64decode`` because JWT segments use
    the URL-safe base64 alphabet (``-``/``_``).
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _invoker_oid() -> str | None:
    """Best-effort: the invoking user's Entra ``oid`` from the inbound bearer (for Langfuse
    per-user attribution). Returns ``None`` on any failure — telemetry must never break invoke.

    The inbound agent-audience JWT (forwarded by the platform/AgentCore on a JWT-inbound
    runtime) carries the user's claims; we read it via the
    ``BedrockAgentCoreContext.get_request_headers()`` path, decode it with the existing
    no-verify ``_decode_jwt_claims`` helper, and surface ``oid`` (the opaque, stable Entra
    user GUID — research §4) or fall back to ``sub``.
    """
    try:
        get_headers = getattr(BedrockAgentCoreContext, "get_request_headers", None)
        if not get_headers:
            return None
        headers = get_headers() or {}
        auth = headers.get("Authorization") or headers.get("authorization") or ""
        if not auth.startswith("Bearer "):
            return None
        claims = _decode_jwt_claims(auth[len("Bearer "):])
        return claims.get("oid") or claims.get("sub")
    except Exception:
        return None


# --- Langfuse observability (research 2026-06-05-langfuse-strands-agent-observability;
#     key sourcing hardened in E26/T10) ------------------------------------------------------
# Pushes full Strands traces (prompts/completions/tool calls), token usage, and per-USER
# attribution (Entra ``oid``) to a self-hosted Langfuse. Wired ONCE at module import, AFTER
# the OTEL env vars are set and BEFORE the app/agents are created (research §1).
#
# E26/T10 (closes E12 release gate #2 — hardcoded Langfuse keys): the project key is NO LONGER
# a hardcoded Langfuse public/secret-key literal. The platform provisions ONE Langfuse project + key
# PER AGENT and stores the ``{public_key, secret_key}`` pair in Secrets Manager; the runtime is
# injected with the secret NAME (``LANGFUSE_SECRET_NAME``) + the host (``LANGFUSE_HOST``). We
# read the pair from that secret at import and build the OTLP Basic-auth header from it — so
# attribution is STRUCTURAL (each agent authenticates with its OWN project key → its traces land
# in its OWN Langfuse project; the ``acme-<agent>`` tags below are now optional drill-down only).
# The secret VALUE lives ONLY in Secrets Manager — never a literal in code, never logged (the
# base64 auth IS the secret).
_LF_SERVICE_NAME = "acme-fnol-agent"


def _setup_langfuse_telemetry() -> None:
    """Wire the Langfuse OTLP exporter from a Secrets-Manager-sourced per-agent project key.

    Reads ``LANGFUSE_HOST`` + ``LANGFUSE_SECRET_NAME`` from the runtime env (injected by the
    platform's per-agent provisioning), fetches the ``{public_key, secret_key}`` pair from
    Secrets Manager (ambient region), and sets the OTEL_* env the Strands exporter reads lazily.

    GRACEFUL DEGRADE (mirrors the pre-existing blank-key guard): if either env var is unset, or
    the secret is missing/unreadable/malformed, telemetry is simply NOT wired — the agent runs
    fine (no exporter, no broken auth header, no crash). Best-effort: NEVER raises. NEVER logs
    the key values or the Authorization header — the base64 auth IS the secret.
    """
    host = os.environ.get("LANGFUSE_HOST", "").rstrip("/")
    secret_name = os.environ.get("LANGFUSE_SECRET_NAME", "")
    if not host or not secret_name:
        logger.info(
            "Langfuse telemetry disabled: LANGFUSE_HOST / LANGFUSE_SECRET_NAME not set."
        )
        return

    try:
        region = os.environ.get("AWS_REGION", "us-east-1")
        sm = boto3.client("secretsmanager", region_name=region)
        secret = json.loads(sm.get_secret_value(SecretId=secret_name)["SecretString"])
        public_key = secret.get("public_key", "")
        secret_key = secret.get("secret_key", "")
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the agent
        logger.warning(
            "Langfuse telemetry disabled: could not read secret %s (%r)", secret_name, exc
        )
        return

    if not public_key or not secret_key:
        logger.warning(
            "Langfuse telemetry disabled: secret %s missing public_key/secret_key.", secret_name
        )
        return

    # Basic auth: base64("{public}:{secret}"). NEVER log this value / the Authorization header.
    lf_auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()

    # #1 TRAP (research §0.2 / §5): AgentCore auto-instruments with ADOT and OWNS the global OTEL
    # TracerProvider FIRST (set-once) — a later StrandsTelemetry() is silently no-op'd and Langfuse
    # gets NOTHING. Disabling ADOT lets the Langfuse exporter win. Best set as a DEPLOY-TIME runtime
    # env var (most reliable — see deploy.sh); set here too as a best-effort fallback, but ONLY once
    # we know Langfuse is actually configured (so an unconfigured agent keeps ADOT/CloudWatch on).
    os.environ["DISABLE_ADOT_OBSERVABILITY"] = "true"
    # BASE endpoint form (research §2): the HTTP exporter auto-appends /v1/traces — do NOT add
    # /v1/traces or a trailing slash here (→ path duplication → 404).
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"{host}/api/public/otel"
    # Basic auth: base64("{public}:{secret}"), header sep is "=" not ": " (OTEL header env
    # encoding — research §2). The exporter reads this lazily at construction.
    os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Basic {lf_auth}"
    # Langfuse accepts http/protobuf + http/json; gRPC is NOT supported — set explicitly so a gRPC
    # exporter does not default to :4317 (research §2).
    os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
    # Max capture (research §6): content promoted to span attributes + full tool schemas; Strands'
    # is_langfuse special-case renders input/output cleanly. setdefault so a deploy-time override wins.
    os.environ.setdefault(
        "OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental,gen_ai_tool_definitions"
    )
    os.environ.setdefault("OTEL_SERVICE_NAME", _LF_SERVICE_NAME)
    # Strands only promotes prompt/completion CONTENT onto span attributes (the form Langfuse's
    # Input/Output panel reads) when its is_langfuse heuristic is True — a literal substring check
    # for "langfuse" in OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_EXPORTER_OTLP_TRACES_ENDPOINT /
    # LANGFUSE_BASE_URL (strands/telemetry/tracer.py is_langfuse). Our CloudFront alias host has no
    # "langfuse" substring, so we set LANGFUSE_BASE_URL (used by Strands ONLY for this detection —
    # the actual OTLP export still uses OTEL_EXPORTER_OTLP_ENDPOINT above) to a value containing
    # "langfuse". Without this, Input/Output show null/undefined in Langfuse.
    os.environ.setdefault("LANGFUSE_BASE_URL", f"{host}/langfuse")

    from strands.telemetry import StrandsTelemetry  # noqa: E402 — must import AFTER the OTEL env above

    # Create a new SDKTracerProvider, set it global, and wrap an HTTP OTLPSpanExporter (reading the
    # OTEL_* env above) in a BatchSpanProcessor. ONCE at import, before any Agent is built (§1).
    StrandsTelemetry().setup_otlp_exporter()


_setup_langfuse_telemetry()
# --- end Langfuse observability ------------------------------------------------------------


app = BedrockAgentCoreApp()


@app.entrypoint
async def invoke(payload):
    """Handle an agent invocation: run the agent, wiring MCP tools only if configured.

    Payload: ``{"prompt": "..."}``. MCP is OPTIONAL: the platform injects the MCP env
    (``CREDENTIAL_PROVIDER_NAME`` / ``MCP_AUDIENCE`` / ``MCP_GATEWAY_URL``) at grant time.
    When it is present, the agent obtains a user-preserving (OBO) Bearer token, connects
    to the MCP gateway, discovers the gateway's tools, and streams the model's response.
    When it is absent (no MCP granted yet), the agent runs prompt-only with the same model
    and persona but no tools. Each event is yielded as an SSE frame by the AgentCore runtime.

    The whole body is wrapped in ``try/finally`` so the OTEL spans are force-flushed after the
    stream drains — a serverless MUST (research §0.5): ``setup_otlp_exporter`` uses a background
    ``BatchSpanProcessor`` and the AgentCore microVM freezes right after the response, so without
    the flush the Langfuse spans are lost.
    """
    region = os.environ.get("AWS_REGION", "us-east-1")
    prompt = payload.get("prompt", "")
    model = BedrockModel(
        model_id=os.environ.get("MODEL_ID", DEFAULT_MODEL_ID),
        region_name=region,
    )

    # Langfuse trace attributes — built per-invocation because the user (oid) differs per
    # request (research §4: trace_attributes are bound at Agent() construction, immutable, and
    # the agent is built fresh per call). Strands applies these to ALL spans it creates
    # (agent/cycle/model/tool), so the per-user attribution covers the whole trace. The keys are
    # the Langfuse OTEL mapping spelling — langfuse.user.id / langfuse.session.id /
    # langfuse.trace.tags (NOT the stale user.id / langfuse.tags).
    # The backend forwards the authenticated invoker oid in the payload (clean, authoritative —
    # the agent can't read the inbound Authorization header; AgentCore's edge authorizer consumes
    # it). Fall back to the inbound-header path if absent.
    oid = payload.get("user_oid") or _invoker_oid()
    trace_attributes: dict = {
        "langfuse.trace.tags": ["acme-fnol", "prototype"],
        "langfuse.trace.name": "fnol-agent-invocation",
    }
    session_id = payload.get("session_id") or payload.get("conversation_id")
    if session_id:
        trace_attributes["langfuse.session.id"] = session_id
    if oid:
        # Entra oid -> Langfuse Users dashboard (per-user cost/tokens/traces).
        trace_attributes["langfuse.user.id"] = oid

    try:
        # MCP is OPTIONAL and now MULTI (E12, design §3.3): the platform injects an MCP_SERVERS
        # JSON list (one entry per granted MCP) plus the single CREDENTIAL_PROVIDER_NAME. A
        # not-yet-redeployed runtime still carries the legacy MCP_AUDIENCE/MCP_GATEWAY_URL —
        # parse_mcp_servers falls back to a 1-element list for it (back-compat, §5). When the
        # list is empty (no MCP granted), run prompt-only — no token call, no gateway, no tools.
        servers = parse_mcp_servers(os.environ)

        if servers:
            # The WAT binds the inbound USER and is the SAME for every MCP, so fetch it ONCE and
            # reuse it across all OBO exchanges (design §3.3); only the outbound MCP-audience
            # token is per-MCP. A WAT-fetch failure leaves wat=None — each per-server
            # get_mcp_obo_token then re-fetches (its single-MCP fallback), so the loop still
            # degrades per-server rather than crashing the whole invoke here.
            try:
                wat = BedrockAgentCoreContext.get_workload_access_token()
            except Exception as wat_exc:  # noqa: BLE001 — degrade; per-server calls will re-fetch
                logger.warning("workload access token fetch failed — per-MCP fetch will retry: %r", wat_exc)
                wat = None

            provider_name = os.environ["CREDENTIAL_PROVIDER_NAME"]
            # Keep every MCPClient context open while the Agent runs (an Agent built from N live
            # clients) and close them all at the end. N hand-nested `with` blocks are impossible
            # for a runtime-length list, so use a sync ExitStack (design note on multi-client
            # lifetime).
            with contextlib.ExitStack() as stack:
                all_tools: list = []
                identity_claims: dict = {}
                for s in servers:
                    try:
                        token = get_mcp_obo_token(
                            provider_name=provider_name,
                            scopes=[s["audience"] + "/.default"],
                            region=region,
                            wat=wat,
                        )

                        client = MCPClient(
                            lambda t=token, u=s["gateway_url"]: streamablehttp_client(
                                u, headers={"Authorization": f"Bearer {t}"}
                            )
                        )
                        stack.enter_context(client)
                        # Namespace every tool {label}__{name} so names can never collide across
                        # MCPs and a call is traceable to its source server (design §3.3).
                        all_tools += [_namespaced_tool(t, s["label"]) for t in client.list_tools_sync()]
                        # Identity is the SAME inbound user across all MCPs; capture it from the
                        # FIRST successful token for the Langfuse label (as today).
                        if not identity_claims:
                            identity_claims = _decode_jwt_claims(token)
                    except Exception as exc:  # noqa: BLE001 — DEGRADE: drop this MCP, keep the rest
                        logger.warning(
                            "MCP %s unavailable this invoke — dropping (degrade): %r",
                            s.get("id"),
                            exc,
                        )
                        continue

                # Langfuse shows the user-id string as the dashboard label, so prefer the readable
                # preferred_username (local part, domain stripped: lars.svensson@contoso.com ->
                # "lars.svensson") from the first MCP OBO token. This is the telemetry LABEL only —
                # Cedar/governance reads the oid from the token, unaffected. Falls back to the oid
                # already set from the payload (and, last, the token's oid) when no username.
                _username = (
                    identity_claims.get("preferred_username") or identity_claims.get("upn") or ""
                ).split("@", 1)[0]
                if _username:
                    trace_attributes["langfuse.user.id"] = _username
                elif not trace_attributes.get("langfuse.user.id"):
                    _token_oid = identity_claims.get("oid") or identity_claims.get("sub")
                    if _token_oid:
                        trace_attributes["langfuse.user.id"] = _token_oid

                # Run with whatever wired successfully (all_tools may be empty if every MCP
                # degrade-dropped — the agent then runs prompt-only-equivalent, but still under
                # the MCP branch since servers WERE configured).
                agent = Agent(
                    model=model,
                    system_prompt=SYSTEM_PROMPT,
                    tools=all_tools,
                    trace_attributes=trace_attributes,
                )
                async for event in agent.stream_async(prompt):
                    yield event
        else:
            # Prompt-only: no MCP granted yet. Same model + persona, no tools.
            logger.info("No MCP configured (MCP_SERVERS empty/absent) — running prompt-only.")
            agent = Agent(
                model=model,
                system_prompt=SYSTEM_PROMPT,
                trace_attributes=trace_attributes,
            )
            async for event in agent.stream_async(prompt):
                yield event
    finally:
        # Force-flush OTEL spans before the AgentCore microVM freezes (research §0.5). Runs when
        # the async generator is fully consumed/closed — i.e. AFTER the stream drains, which is
        # correct. Best-effort: a flush failure must never surface to the caller.
        try:
            from opentelemetry import trace as _trace

            _trace.get_tracer_provider().force_flush()
        except Exception:
            pass


if __name__ == "__main__":
    app.run()
