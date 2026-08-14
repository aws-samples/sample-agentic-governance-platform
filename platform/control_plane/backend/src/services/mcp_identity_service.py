"""MCP Entra identity provisioning orchestrator (Epic 7, Task T-IDENTITY).

``McpIdentityService`` is the heart of "registering an MCP makes the platform wire it
up." For a manually-created AgentCore **Gateway** (or **Runtime-MCP**) it runs the same
shape of background provisioning E6 ran for agents — create the per-MCP Entra app reg +
SP (roles assignable **to applications**), require assignment — but the load-bearing
middle step is configuring the resource's OWN inbound ``CUSTOM_JWT`` authorizer to trust
our Entra tenant (gateway → ``UpdateGateway``; runtime → the E6 ``UpdateAgentRuntime``
path, verbatim). It also live-scans the tools BEFORE locking the gateway down (so the
scan needs no token) and persists everything onto the registry record.

It is a CLONE of ``agent_identity_service`` in mechanics (persist-immediately after each
Entra write, off-loop boto3 dispatch, gateway-id/RID-from-ARN, poll-to-READY, the
failure→``identity_status='failed'``+raise contract) but with THREE deliberate,
load-bearing structural DEVIATIONS from the agent template (see CRITIC-C2 on ``provision``):
  (a) there is **NO ``grant_*_obo_consent`` step** — the agent→MCP delegated consent
      moves to GRANT time (T-ROUTES), so ``provision`` NEVER calls it;
  (b) a **NEW pre-lockdown tool-scan** precedes app creation (the gateway isn't locked
      down yet, so the scan needs no token); and therefore
  (c) create is NOT step 1.

It is **idempotent + resumable** (the "Re-provision" button): step (3) is skipped when
the MCP already carries an ``entra_sp_id`` (a re-provision), the Graph PATCH +
authorizer GET→Update are idempotent, and the pre-lockdown scan is skipped once the
gateway is already ``CUSTOM_JWT``. On ANY failure (except the swallowed best-effort scan)
it persists ``identity_status='failed'`` and raises :class:`McpProvisioningError`.

Mechanics source: research §1.1 (gateway GetGateway→replay(``name``/``roleArn``/
``protocolType``/``protocolConfiguration``)→``UpdateGateway``→poll-to-READY;
gatewayId-from-ARN; the optional-field replay loop), §1.2 (runtime = E6 verbatim),
§1.3 (``gatewayUrl`` stored verbatim), §1.4 (audience-form — feed BOTH forms),
§3 design decision (pre-lockdown scan). ``agent_identity_service`` is the template.
"""

from __future__ import annotations

import logging
import time

import anyio
import anyio.to_thread
import boto3

from models.mcp_server import IdentityStatus, Kind, McpServer, McpTool
from services.graph_service import GraphError, GraphService
from services.mcp_server_service import McpServerRegistryService
from services.mcp_tool_scanner import McpScanError, scan_mcp_tools

logger = logging.getLogger(__name__)

# Poll-to-READY loop bounds (research §1.1 — UpdateGateway/UpdateAgentRuntime are async;
# the resource advances UPDATING→READY, ~minute+). Bounded so a stuck/never-READY
# resource fails loudly instead of hanging the background task forever. Same values as
# agent_identity_service.
_POLL_MAX_ATTEMPTS = 60
_POLL_INTERVAL_SECONDS = 5.0

# Status that means success. A ``*_FAILED`` / ``*_UNSUCCESSFUL`` (or exhausting the loop)
# is a provisioning failure. Covers BOTH the gateway status enum
# (``CREATING|UPDATING|UPDATE_UNSUCCESSFUL|DELETING|READY|FAILED``, research §1.1) and the
# runtime enum (``CREATE_FAILED|UPDATE_FAILED|…``, E6).
_READY_STATUS = "READY"
_FAILED_STATUSES = frozenset(
    {
        "CREATE_FAILED",
        "UPDATE_FAILED",
        "DELETE_FAILED",
        "FAILED",
        "UPDATE_UNSUCCESSFUL",
    }
)

# Gateway authorizer types that mean "not yet locked down" — the pre-lockdown wire scan
# needs no token only while the gateway is open (research §3 design decision). Once it is
# ``CUSTOM_JWT`` the scan would need a bearer, so we skip it (keep existing tools).
_OPEN_AUTHORIZER_TYPES = frozenset({"NONE", "AWS_IAM"})

# ListGatewayTargets pagination guard (T-NATIVE-TOOLSCAN). The native tool read pages on
# ``nextToken``; cap the page count so a misbehaving control plane can never spin the
# background task forever (mirrors the scanner's ``_MAX_PAGES`` stance).
_MAX_TARGET_PAGES = 20


def should_provision_mcp(mcp: McpServer) -> bool:
    """The provisioning gate: which MCP records get an Entra identity (research §6.4).

    CRITIC-I2: research §6.4's gate is "kind ∈ {gateway, runtime} AND handle AND
    auth_type=entra". ``McpServer`` has NO ``auth_type`` field (MCP records don't carry
    one) — so ``kind + handle`` IS the sufficient gate here; the §6.4 ``auth_type`` clause
    does not apply to MCP records. (Documented divergence, intentional.)

    True only for an AgentCore Gateway carrying a ``gateway_arn`` OR an AgentCore
    Runtime-MCP carrying a ``runtime_arn``. ``standard`` (external/metadata-only) records
    and handle-less records are skipped.
    """
    return (mcp.kind == Kind.GATEWAY and bool(mcp.gateway_arn)) or (
        mcp.kind == Kind.RUNTIME and bool(mcp.runtime_arn)
    )


class McpProvisioningError(Exception):
    """A step in the (non-atomic) Entra+AWS MCP provisioning sequence failed.

    The MCP's ``identity_status`` is persisted as ``'failed'`` before this is raised, so a
    later re-provision (idempotent + resumable) can retry.
    """


class McpIdentityService:
    """Orchestrates Entra identity provisioning for an AgentCore Gateway / Runtime-MCP."""

    def __init__(
        self,
        *,
        graph: GraphService,
        registry: McpServerRegistryService,
        control_client=None,
        region: str = "us-east-1",
        tenant_id: str,
        login_base: str,
    ) -> None:
        self._graph = graph
        self._registry = registry
        self._region = region
        self._tenant_id = tenant_id
        self._login_base = login_base.rstrip("/")
        # boto3 ``bedrock-agentcore-control`` client. Injectable for tests (a MagicMock);
        # built by default (no live AWS in unit tests, which always inject). Control-plane
        # signing name is ``bedrock-agentcore-control``. (Same as agent_identity_service.)
        self._control = control_client or boto3.client(
            "bedrock-agentcore-control", region_name=region
        )

    # -- public ------------------------------------------------------------

    async def provision(self, mcp: McpServer) -> McpServer:
        """Idempotent, resumable provisioning. CANONICAL ORDER (NOT the agent clone's
        order — see CRITIC-C2 in the module docstring).

        (1) derive gateway_id (from gateway_arn) / runtime id (from runtime_arn);
            GetGateway / GetAgentRuntime; capture gateway_url VERBATIM (gateway path)
            → set mcp.gateway_url + mcp.gateway_id; persist (still 'pending').
        (2) Tool discovery (best-effort, CRITIC-I1): gateway only; runtime → skip.
            PRIMARY: native control-plane read — ListGatewayTargets → GetGatewayTarget
            → inlinePayload (token-less; works on a locked CUSTOM_JWT gateway). FALLBACK:
            token-less wire-scan (scan_mcp_tools(gateway_url, bearer=None)) ONLY when the
            native read found nothing AND the authorizer is still NONE/AWS_IAM. Any
            read/scan error is swallowed (never strand identity as 'failed'); overwrite
            available_tools ONLY on a NON-EMPTY result (never wipe E5 seeds).
        (3) if NOT mcp.entra_sp_id: create_mcp_app → set ALL FIVE ids (CRITIC-I4) →
            persist IMMEDIATELY (status stays 'pending') [E6 CRITIQUE-FIX-A]. Re-provision
            (entra_sp_id already set) → skip create.
        (4) set_assignment_required(mcp.entra_sp_id, required=False) — EXPLICIT False:
            the delegated/OBO user is gated by the agent→MCP consent grant, NOT by an MCP
            app-role assignment (which Entra enforces against the USER → AADSTS50105). See
            research §2.4/§2.5 + the inline comment at the call site.
        (5) configure the inbound authorizer OFF-LOOP (anyio.to_thread.run_sync — never
            block the uvicorn loop): GATEWAY → _configure_gateway_authorizer ; RUNTIME →
            _configure_runtime_authorizer.
        (6) persist identity_status='provisioned'.

        On ANY failure (except the swallowed step-2 scan): persist
        identity_status='failed' + raise McpProvisioningError. Steps individually
        idempotent → re-provision resumes cleanly.
        """
        try:
            logger.info(
                "[mcp_identity] provisioning MCP %s (kind=%s)", mcp.id, mcp.kind
            )
            # (1) Read the resource + capture its identifiers. For a gateway, the
            # gatewayUrl is stored VERBATIM (research §1.3 — never hand-construct it) and
            # the short gatewayId is parsed from the arn (research §1.1). The current
            # authorizerType decides whether the pre-lockdown scan can run token-less.
            current_authorizer_type: str | None = None
            if mcp.kind == Kind.GATEWAY:
                gateway_id = self._gateway_id_from_arn(mcp.gateway_arn)
                mcp.gateway_id = gateway_id
                gw = self._control.get_gateway(gatewayIdentifier=gateway_id)
                # gatewayUrl VERBATIM from the API (research §1.3).
                if gw.get("gatewayUrl"):
                    mcp.gateway_url = gw["gatewayUrl"]
                current_authorizer_type = gw.get("authorizerType")
                # The authorizer type is a key DECISION input — it explains whether the
                # token-less wire-scan fallback is even eligible (it isn't once CUSTOM_JWT).
                logger.info(
                    "[mcp_identity] MCP %s gateway_id=%s authorizer=%s",
                    mcp.id,
                    gateway_id,
                    current_authorizer_type,
                )
            else:
                # Runtime-MCP: read it so a stuck/failed read surfaces early + parity with
                # the gateway path; nothing to capture (no gatewayUrl analogue).
                runtime_id = mcp.runtime_arn.rsplit("/", 1)[-1]
                self._control.get_agent_runtime(agentRuntimeId=runtime_id)
            # Capture what we learned (gateway_url / gateway_id) IN MEMORY only — still
            # 'pending'. The earliest write is intentionally dropped (T-PERSIST-RETRY):
            # the route already stamps 'pending' + persists before scheduling provision,
            # so a write here is redundant (and an extra UPDATING-state race surface). The
            # single persist after step (3) carries gateway_url/gateway_id + the entra ids
            # in ONE write. (Re-provision: that persist runs in both branches — see below.)
            mcp.identity_status = IdentityStatus.PENDING

            # (2) Tool discovery (best-effort, CRITIC-I1). For a GATEWAY the PRIMARY path
            # is the NATIVE control-plane read (ListGatewayTargets → GetGatewayTarget):
            # it needs NO token and works on a locked CUSTOM_JWT gateway (the now-common
            # case — gateways are born CUSTOM_JWT, T-AUTHZ-AT-CREATE, so the wire-scan is
            # never eligible). The pre-lockdown wire-scan (research §3) stays ONLY as a
            # fallback for the rare still-open gateway whose native read found nothing.
            # A flaky read/scan must NOT strand identity as 'failed', and a transient
            # empty/error result must NOT wipe E5-seeded tools — so we overwrite
            # available_tools ONLY on a successful NON-EMPTY result. runtime → skip.
            if mcp.kind == Kind.GATEWAY and mcp.gateway_id:
                tools: list[McpTool] = []
                # Track WHICH discovery path produced the tools so the outcome line can
                # answer "why are there N tools" without a code dig (T-OBSERVABILITY).
                source = "none"
                # NATIVE read first — SYNC boto3 control calls, dispatched OFF the loop
                # (like the authorizer config) so the uvicorn loop never blocks.
                try:
                    tools = await anyio.to_thread.run_sync(
                        self._read_gateway_tools_native, mcp.gateway_id
                    )
                    if tools:
                        source = "native"
                except Exception as native_err:  # noqa: BLE001 — best-effort (CRITIC-I1)
                    logger.warning(
                        "[mcp_identity] native tool read failed for MCP %s (continuing): %s",
                        mcp.id,
                        native_err,
                    )
                # Fallback: only if the native read found nothing AND the gateway is still
                # open (so a token-less wire-scan is still possible — research §3).
                if (
                    not tools
                    and mcp.gateway_url
                    and (current_authorizer_type in _OPEN_AUTHORIZER_TYPES)
                ):
                    try:
                        tools = await scan_mcp_tools(mcp.gateway_url, bearer=None)
                        if tools:
                            source = "wire-scan"
                    except McpScanError as scan_err:
                        # Tolerant marker: log + continue. Do NOT fail provisioning, do
                        # NOT wipe seeded tools (CRITIC-I1).
                        logger.warning(
                            "[mcp_identity] pre-lockdown wire-scan failed for MCP %s "
                            "(continuing, seeded tools kept): %s",
                            mcp.id,
                            scan_err,
                        )
                # CRITIC-I1: overwrite ONLY on a non-empty result; never wipe E5-seeded
                # tools on an empty/error discovery.
                if tools:
                    mcp.available_tools = tools
                # ONE outcome line — makes "why are there 0 tools" answerable from logs.
                # source ∈ {native, wire-scan, none}; count is what discovery returned
                # this pass (the seeded tools are kept on a 0/error result, see above).
                logger.info(
                    "[mcp_identity] MCP %s tool discovery: %d tools via %s",
                    mcp.id,
                    len(tools),
                    source,
                )
            elif mcp.kind != Kind.GATEWAY:
                # Runtime-MCP: tool discovery is gateway-only — say so explicitly so a
                # "0 tools on a runtime" is a logged DECISION, not a silent gap.
                logger.info(
                    "[mcp_identity] MCP %s tool discovery skipped (kind=%s, gateway-only)",
                    mcp.id,
                    mcp.kind,
                )

            # (3) Create the per-MCP Entra app reg + SP — UNLESS the MCP already carries an
            # entra_sp_id (a re-provision: skip create). CRITIC-I4: set ALL FIVE ids.
            if not mcp.entra_sp_id:
                created = await self._graph.create_mcp_app(mcp.id, mcp.name)
                mcp.entra_app_id = created["app_id"]
                mcp.entra_sp_id = created["sp_id"]
                # AUDIENCE-FORM (research §1.4): set entra_app_audience to the app URI
                # form; the authorizer step feeds BOTH this and the client GUID
                # (entra_app_id) to allowedAudience so whichever form the real token
                # carries validates.
                mcp.entra_app_audience = created["app_uri"]
                mcp.invoker_role_id = created["invoker_role_id"]
                mcp.admin_role_id = created["admin_role_id"]
                # FIX (mirror E6): guard the get-or-create branch — _resolve_existing_app
                # can return sp_id=None (app exists from a prior partial run, SP not yet
                # created). Persisting entra_sp_id=None then calling set_assignment_required
                # on a /servicePrincipals/None path would malform, and the falsy
                # entra_sp_id would make the next re-provision re-enter create_mcp_app →
                # 409 on the duplicate identifierUri. The APP ids we DID get are set above
                # so the duplicate-resolve works next time; raise here to route through the
                # failure path WITHOUT advancing to steps 4-5.
                if not mcp.entra_sp_id:
                    raise McpProvisioningError(
                        f"create_mcp_app returned no sp_id for MCP {mcp.id}"
                    )
            # ONE persist after the Entra-app block — runs in BOTH branches (freshly
            # created OR re-provision). This is the SINGLE 'pending' write of the sequence
            # (the redundant step-1 write was dropped, T-PERSIST-RETRY): it carries
            # gateway_url/gateway_id + available_tools + the five Entra ids in one write.
            # E6 CRITIQUE-FIX-A preserved: a freshly-created app reg is persisted
            # IMMEDIATELY, before steps 4-5. The sequence is non-atomic — if a later step
            # fails and we hadn't persisted first, the failure-persist would write
            # entra_sp_id=None, the re-provision skip-guard would re-create the app, and
            # the duplicate identifierUri would 409 forever. On re-provision it persists
            # the gateway_url/gateway_id learned this pass even though create was skipped.
            self._registry.persist_identity(mcp)
            # CRITIC-C2 (a): there is intentionally NO grant_*_obo_consent step here — the
            # agent→MCP delegated consent fires at GRANT time (T-ROUTES), not provision.

            # (4) Set the MCP SP's appRoleAssignmentRequired to FALSE — EXPLICITLY
            # (research §2.4/§2.5). In the agent→MCP DELEGATED/OBO flow Entra enforces the
            # resource app's assignment-required against the USER, not the calling agent SP;
            # our locked design NEVER assigns users to MCP servers (the user is granted the
            # AGENT, the AGENT is granted the MCP via a consent grant), so a True flag here
            # would block the delegated user with AADSTS50105 by design (live-confirmed). The
            # OBO admission gate that REMAINS is the per-agent agent→MCP oauth2PermissionGrant
            # (grant_agent_obo_consent, fired at grant time); revoke it → OBO fails AADSTS65001.
            # This deliberately drops the agent-SP app-role *assignment* as an admission gate —
            # it is only consulted in an app-only/M2M token, a path we do NOT use (OBO works).
            # EXPLICIT False (not "skip the call") so a re-provision of an MCP whose SP is
            # currently True (e.g. flipped manually, or any prior-true record) converges to
            # False — idempotent + self-healing.
            await self._graph.set_assignment_required(mcp.entra_sp_id, required=False)

            # (5) Configure the resource's inbound CUSTOM_JWT authorizer. SYNC blocking
            # boto3 with a ~minute+ poll-to-READY; provision() runs on the single uvicorn
            # event loop (via BackgroundTasks), so dispatch it OFF the loop
            # (CRITIQUE-FIX-B) to avoid freezing health checks. Branch on kind.
            if mcp.kind == Kind.GATEWAY:
                await anyio.to_thread.run_sync(
                    self._configure_gateway_authorizer, mcp.gateway_id, mcp
                )
            else:
                await anyio.to_thread.run_sync(
                    self._configure_runtime_authorizer, mcp.runtime_arn, mcp
                )
            logger.info(
                "[mcp_identity] MCP %s inbound CUSTOM_JWT authorizer configured", mcp.id
            )

            # (6) Done.
            mcp.identity_status = IdentityStatus.PROVISIONED
            self._registry.persist_identity(mcp)
            logger.info("[mcp_identity] MCP %s provisioned", mcp.id)
            return mcp

        except Exception as err:  # noqa: BLE001 — any failure → failed status + re-raise
            mcp.identity_status = IdentityStatus.FAILED
            try:
                self._registry.persist_identity(mcp)
            except Exception:  # noqa: BLE001 — don't mask the original failure
                logger.exception(
                    "[mcp_identity] failed to persist 'failed' status for MCP %s",
                    mcp.id,
                )
            # Surface the failure (mirror E6 CRITIQUE-FIX-E) WITHOUT leaking secrets.
            # A GraphError's str() carries status + Graph error code + (for
            # RESOURCE-endpoint errors) a safe message — none of which contain a token or
            # the client secret — so a plain WARNING with its str() is safe. For ANY OTHER
            # (unexpected) error we log the FULL TRACEBACK via logger.exception so the next
            # silent failure is diagnosable from logs instead of a code dig (T-OBSERVABILITY).
            # Python tracebacks print the stack, NOT local-variable values, and this flow
            # holds no secret locals (agent_credential_service is out of scope here), so the
            # traceback is safe to emit.
            if isinstance(err, GraphError):
                logger.warning(
                    "[mcp_identity] provisioning failed for MCP %s: %s", mcp.id, err
                )
            else:
                logger.exception(
                    "[mcp_identity] provisioning failed for MCP %s (unexpected error)",
                    mcp.id,
                )
            raise McpProvisioningError(
                f"provisioning failed for MCP {mcp.id}"
            ) from err

    async def refresh_tools(self, mcp: McpServer) -> McpServer:
        """Re-read the gateway's tools natively + persist — WITHOUT touching identity/authorizer.

        The fast, SYNCHRONOUS twin of the step-2 tool discovery inside :meth:`provision`,
        for the FE "refresh tools" button: read the tools natively (``ListGatewayTargets`` →
        ``GetGatewayTarget`` → inline lambda toolSchema, token-less, works on a locked
        CUSTOM_JWT gateway) + persist + return the fresh record immediately. It does NOT
        re-provision: ``identity_status``, the inbound authorizer, and every ``entra_*``
        field are left untouched.

        Gateway-only (the native read is gateway target-based). Derives the gatewayId
        (prefer ``mcp.gateway_id``; fall back to ``_gateway_id_from_arn(mcp.gateway_arn)``);
        if neither handle is present (not a gateway / no handle) it returns the mcp
        unchanged — the route already guards this, but be defensive.

        Off-loops the sync boto3 read (``anyio.to_thread.run_sync``), like ``provision`` does.
        Best-effort + overwrite-only-on-non-empty (CRITIC-I1): a read error is logged + the
        mcp is returned UNCHANGED (never raise — a flaky read must not 500 the refresh
        button), and ``available_tools`` is overwritten + persisted ONLY on a non-empty
        result (never wipe existing tools on an empty/error read, and avoid a needless
        write / UPDATING-race when nothing changed).
        """
        gateway_id = mcp.gateway_id or (
            self._gateway_id_from_arn(mcp.gateway_arn) if mcp.gateway_arn else None
        )
        if not gateway_id:
            # Not a gateway / no handle to read — nothing to refresh (route guards this too).
            return mcp

        tools: list[McpTool] = []
        try:
            # SYNC boto3 control calls dispatched OFF the loop (like provision's native read).
            tools = await anyio.to_thread.run_sync(
                self._read_gateway_tools_native, gateway_id
            )
        except Exception as err:  # noqa: BLE001 — best-effort (CRITIC-I1): never 500/raise
            logger.warning(
                "[mcp_identity] refresh tool read failed for MCP %s (returning unchanged): %s",
                mcp.id,
                err,
            )
            return mcp

        # Overwrite-only-on-non-empty (CRITIC-I1): never wipe existing tools on an empty
        # read; persist ONLY when something actually came back (avoid a needless write).
        if tools:
            mcp.available_tools = tools
            self._registry.persist_identity(mcp)
        # Outcome line — makes "the refresh button returned 0 tools" answerable from logs.
        logger.info(
            "[mcp_identity] refresh_tools MCP %s: %d tools", mcp.id, len(tools)
        )
        return mcp

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _gateway_id_from_arn(gateway_arn: str) -> str:
        """Parse the short gatewayId from a gatewayArn (research §1.1 — control calls take
        the gatewayId, NOT the ARN; the "RID-not-ARN" lesson repeats). ARN tail is
        ``.../gateway/{gatewayId}``."""
        return gateway_arn.rsplit("/", 1)[-1]

    def _read_gateway_tools_native(self, gateway_id: str) -> list[McpTool]:
        """Read a gateway's tools from the control plane (no token; works on a locked
        CUSTOM_JWT gateway — T-NATIVE-TOOLSCAN / research §5.3).

        ``ListGatewayTargets`` → ``GetGatewayTarget`` → for each LAMBDA target with an
        inline ``toolSchema``, map ``inlinePayload`` → :class:`McpTool` whose name is the
        INVOCABLE ``<targetName>___<toolName>`` (triple underscore, stored verbatim).
        Targets that are not natively-readable (``openApiSchema`` / ``smithyModel`` /
        ``mcpServer`` / ``apiGateway``, or a lambda using an s3 ``toolSchema`` instead of
        ``inlinePayload``) are SKIPPED — they need the wire scan / a token (logged at
        debug). ``ListGatewayTargets`` is paginated on ``nextToken`` (bounded by
        ``_MAX_TARGET_PAGES``). Best-effort: a boto error reading ONE target is logged +
        skipped, not fatal, so one bad target doesn't lose the rest. Returns the
        aggregated tool list (possibly empty). SYNC boto3 — dispatched off-loop by the
        caller (``anyio.to_thread.run_sync``), like the authorizer config.
        """
        tools: list[McpTool] = []
        # Counters for the single end-of-read summary line (T-OBSERVABILITY) — makes
        # "the gateway has tools but we read 0" diagnosable (target seen but skipped).
        n_targets = 0
        n_skipped = 0
        next_token: str | None = None
        for _ in range(_MAX_TARGET_PAGES):
            kwargs: dict = {"gatewayIdentifier": gateway_id}
            if next_token:
                kwargs["nextToken"] = next_token
            page = self._control.list_gateway_targets(**kwargs)
            for item in page.get("items") or []:
                target_id = item.get("targetId")
                if not target_id:
                    continue
                n_targets += 1
                try:
                    gt = self._control.get_gateway_target(
                        gatewayIdentifier=gateway_id, targetId=target_id
                    )
                except Exception as err:  # noqa: BLE001 — best-effort per-target
                    n_skipped += 1
                    logger.debug(
                        "[mcp_identity] GetGatewayTarget failed for target %s on gateway "
                        "%s (skipping): %s",
                        target_id,
                        gateway_id,
                        err,
                    )
                    continue

                tc = gt.get("targetConfiguration") or {}
                lam = (tc.get("mcp") or {}).get("lambda") or {}
                inline_payload = (lam.get("toolSchema") or {}).get("inlinePayload")
                if not inline_payload:
                    # Not a lambda inline-schema target — needs the wire scan / a token.
                    n_skipped += 1
                    logger.debug(
                        "[mcp_identity] gateway %s target %s has no inline lambda "
                        "toolSchema (skipping native read)",
                        gateway_id,
                        target_id,
                    )
                    continue

                # The invocable name prefix is the TARGET name (research §5.3): prefer the
                # GetGatewayTarget name, fall back to the list item's name.
                target_name = gt.get("name") or item.get("name") or ""
                for tool in inline_payload:
                    tool_name = tool.get("name")
                    if not tool_name:
                        continue  # defensive: skip nameless entries, don't crash
                    tools.append(
                        McpTool(
                            name=f"{target_name}___{tool_name}",
                            description=tool.get("description") or "",
                            input_schema=tool.get("inputSchema") or {},
                        )
                    )

            next_token = page.get("nextToken")
            if not next_token:
                break
        logger.info(
            "[mcp_identity] native tool read for gateway %s: %d tools across %d targets "
            "(%d skipped)",
            gateway_id,
            len(tools),
            n_targets,
            n_skipped,
        )
        return tools

    def _discovery_url(self) -> str:
        """The Entra v2 OIDC well-known URL for our tenant (matches the runtime authorizer
        discoveryUrl agent_identity_service builds)."""
        return (
            f"{self._login_base}/{self._tenant_id}"
            "/v2.0/.well-known/openid-configuration"
        )

    @staticmethod
    def _allowed_audience(mcp: McpServer) -> list[str]:
        """Both audience forms (CRITIC-M3 / research §1.4): the api:// URI AND the app
        client GUID, in that order, dropping any that's unset. The real OBO'd token's
        ``aud`` can be either form, so the authorizer accepts both (each is per-MCP-unique
        → cross-MCP-replay safe)."""
        return [a for a in (mcp.entra_app_audience, mcp.entra_app_id) if a]

    # -- gateway authorizer (sync, off-loop) -------------------------------

    def _configure_gateway_authorizer(self, gateway_id: str, mcp: McpServer) -> None:
        """SYNC blocking boto3: set the gateway's inbound CUSTOM_JWT authorizer (research §1.1).

        ``UpdateGateway`` is a **full-replace PUT** (NOT the ``{"optionalValue":…}`` PATCH
        ``UpdateRegistryRecord`` uses). Required input: ``gatewayIdentifier``, ``name``
        (must equal the create-time name), ``roleArn``, ``authorizerType``. So this
        ``get_gateway`` → replays ``name``/``roleArn``/``protocolType``/
        ``protocolConfiguration`` (+ optional ``description``/``kmsKeyArn``/
        ``interceptorConfigurations``/``policyEngineConfiguration``/``exceptionLevel`` only
        if present, to avoid dropping them — esp. ``protocolConfiguration`` which carries
        ``searchType=SEMANTIC`` that can only be set at create) + adds
        ``authorizerConfiguration`` + ``authorizerType="CUSTOM_JWT"`` → ``update_gateway``
        → polls ``get_gateway`` to READY. ``gatewayIdentifier`` takes the short gatewayId,
        NOT the ARN.
        """
        gw = self._control.get_gateway(gatewayIdentifier=gateway_id)

        # Audience guard (#2 fix): fail loud before the Update rather than silently
        # configuring an open-to-nobody authorizer (empty allowedAudience rejects all tokens
        # with no clear error). Routes through the caller's failure path (persist 'failed').
        audience = self._allowed_audience(mcp)
        if not audience:
            raise McpProvisioningError(
                f"refusing to configure CUSTOM_JWT with empty allowedAudience for MCP {mcp.id}"
            )

        authorizer_configuration = {
            "customJWTAuthorizer": {
                "discoveryUrl": self._discovery_url(),
                # Audience ONLY (E6 Decision 3) — no allowedClients / customClaims.
                # BOTH forms (CRITIC-M3 / research §1.4).
                "allowedAudience": audience,
            }
        }

        # Full-replace: the required fields + the new authorizer.
        kwargs: dict = {
            "gatewayIdentifier": gw["gatewayId"],
            "name": gw["name"],
            "roleArn": gw["roleArn"],
            "authorizerType": "CUSTOM_JWT",
            "authorizerConfiguration": authorizer_configuration,
        }
        # Replay optional fields ONLY if present (research §1.1) so we never drop them.
        # protocolType/protocolConfiguration carry the create-time MCP + semantic-search
        # config; the rest are optional gateway settings.
        for key in (
            "protocolType",
            "protocolConfiguration",
            "description",
            "kmsKeyArn",
            "interceptorConfigurations",
            "policyEngineConfiguration",
            "exceptionLevel",
        ):
            if gw.get(key):
                kwargs[key] = gw[key]

        self._control.update_gateway(**kwargs)

        self._poll_gateway_ready(gateway_id)

    def _poll_gateway_ready(self, gateway_id: str) -> None:
        """Poll ``get_gateway`` until status READY (bounded loop + sleep).

        A ``*_FAILED`` / ``UPDATE_UNSUCCESSFUL`` status, or exhausting
        ``_POLL_MAX_ATTEMPTS``, raises so the caller marks the MCP ``failed``.
        ``time.sleep`` is patched to a no-op in tests.
        """
        for _ in range(_POLL_MAX_ATTEMPTS):
            resp = self._control.get_gateway(gatewayIdentifier=gateway_id)
            status = resp.get("status")
            if status == _READY_STATUS:
                return
            if status in _FAILED_STATUSES:
                raise RuntimeError(
                    f"gateway {gateway_id} reached terminal status {status}"
                )
            time.sleep(_POLL_INTERVAL_SECONDS)
        raise RuntimeError(
            f"gateway {gateway_id} did not reach READY within "
            f"{_POLL_MAX_ATTEMPTS} polls"
        )

    # -- runtime authorizer (sync, off-loop) — E6 verbatim -----------------

    def _configure_runtime_authorizer(self, runtime_arn: str, mcp: McpServer) -> None:
        """SYNC blocking boto3: set a Runtime-MCP's inbound JWT authorizer.

        Research §1.2: a runtime with ``serverProtocol=MCP`` uses the IDENTICAL
        ``customJWTAuthorizer`` shape and the IDENTICAL ``GetAgentRuntime →
        replay(agentRuntimeArtifact/roleArn/networkConfiguration) → UpdateAgentRuntime →
        poll-to-READY`` path E6 built and live-confirmed — reused verbatim, swapping the
        audience (BOTH forms, CRITIC-M3). ``RID``-not-ARN: the control calls take
        ``agentRuntimeId`` (the ARN's last ``/``-segment).
        """
        runtime_id = runtime_arn.rsplit("/", 1)[-1]

        current = self._control.get_agent_runtime(agentRuntimeId=runtime_id)

        # Audience guard (mirror of the gateway path): fail loud before the Update.
        audience = self._allowed_audience(mcp)
        if not audience:
            raise McpProvisioningError(
                f"refusing to configure CUSTOM_JWT with empty allowedAudience for MCP {mcp.id}"
            )

        authorizer_configuration = {
            "customJWTAuthorizer": {
                "discoveryUrl": self._discovery_url(),
                # Audience ONLY — no allowedClients / customClaims. BOTH forms (CRITIC-M3).
                "allowedAudience": audience,
            }
        }

        # Full-replace: replay the required fields from the GET + add the authorizer.
        # Also replay optional fields ONLY if present, to avoid silently dropping
        # environmentVariables, protocolConfiguration, requestHeaderConfiguration,
        # lifecycleConfiguration, metadataConfiguration, filesystemConfigurations, or
        # description that were set at create time — the runtime twin of the gateway
        # optional-field replay loop above.
        kwargs: dict = {
            "agentRuntimeId": runtime_id,
            "agentRuntimeArtifact": current["agentRuntimeArtifact"],
            "roleArn": current["roleArn"],
            "networkConfiguration": current["networkConfiguration"],
            "authorizerConfiguration": authorizer_configuration,
        }
        for key in (
            "protocolConfiguration",
            "requestHeaderConfiguration",
            "environmentVariables",
            "lifecycleConfiguration",
            "metadataConfiguration",
            "filesystemConfigurations",
            "description",
        ):
            if current.get(key):
                kwargs[key] = current[key]

        self._control.update_agent_runtime(**kwargs)

        self._poll_runtime_ready(runtime_id)

    def _poll_runtime_ready(self, runtime_id: str) -> None:
        """Poll ``get_agent_runtime`` until status READY (bounded loop + sleep).

        A ``*_FAILED`` status, or exhausting ``_POLL_MAX_ATTEMPTS``, raises so the caller
        marks the MCP ``failed``. ``time.sleep`` is patched to a no-op in tests.
        """
        for _ in range(_POLL_MAX_ATTEMPTS):
            resp = self._control.get_agent_runtime(agentRuntimeId=runtime_id)
            status = resp.get("status")
            if status == _READY_STATUS:
                return
            if status in _FAILED_STATUSES:
                raise RuntimeError(
                    f"runtime {runtime_id} reached terminal status {status}"
                )
            time.sleep(_POLL_INTERVAL_SECONDS)
        raise RuntimeError(
            f"runtime {runtime_id} did not reach READY within "
            f"{_POLL_MAX_ATTEMPTS} polls"
        )
