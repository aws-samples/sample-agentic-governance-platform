"""Cedar Policy Engine orchestrator for AgentCore Gateways (Epic 8, Task T3).

``McpCedarService`` turns a gateway MCP server's friendly per-tool authorization form
(pick an Entra user's ``oid`` → pick a tool / "All tools" → Allow) into a native
AgentCore **Policy Engine** + Cedar ``permit`` policy and applies it to the gateway:

  - on the FIRST policy we ``create_policy_engine`` (idempotent — skip when the record
    already carries one), ASSOCIATE it to the gateway via ``update_gateway``'s
    ``policyEngineConfiguration`` field, then ``create_policy`` the Cedar text;
  - default-deny kicks in once the engine is attached in ``ENFORCE`` mode;
  - remove = ``delete_policy`` (the engine survives);
  - disable = detach the engine (``update_gateway`` OMITTING ``policyEngineConfiguration``).

It is a CLONE of ``mcp_identity_service`` in mechanics: the GET→replay→``update_gateway``
→poll-to-READY gateway pattern (replaying ``authorizerType`` + ``authorizerConfiguration``
+ the optional fields so the E7 inbound Entra ``CUSTOM_JWT`` gate is NEVER stripped —
research §3.1, the highest-risk seam), the off-loop ``anyio.to_thread.run_sync`` dispatch
(never block the uvicorn loop), and the bounded poll constants. The Cedar text is
generated/parsed by the pure ``cedar_policy_text`` helper (no ``cedar-policy`` binding —
the gateway evaluates).

Status vocabulary note: the Policy ENGINE + POLICY statuses
(``CREATING|ACTIVE|UPDATING|DELETING|*_FAILED``, success = ``ACTIVE``) are DISTINCT from
the GATEWAY status family (``CREATING|UPDATING|READY|FAILED``, success = ``READY``) — do
not cross them. Engine/policy settle is handled by the installed boto3 waiters
(``policy_engine_active`` / ``policy_active`` / ``policy_deleted``); the gateway settle by
the cloned ``_poll_gateway_ready``.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional
from uuid import uuid4

import anyio
import anyio.to_thread
import boto3
from botocore.exceptions import ClientError

from models.mcp_server import McpServer
from services.cedar_policy_text import (
    build_cedar_policy,
    detect_effect,
    parse_cedar_policy,
    validate_conditions,
)
from services.mcp_server_service import McpServerRegistryService

logger = logging.getLogger(__name__)

# Enforcement-mode constants (the McpServer.cedar_enforcement_mode values ↔ the AWS mode).
_MODE_NONE = "none"
_MODE_LOG_ONLY = "log_only"
_MODE_ENFORCE = "enforce"
_AWS_MODE = {_MODE_LOG_ONLY: "LOG_ONLY", _MODE_ENFORCE: "ENFORCE"}  # our str → GatewayPolicyEngineMode
# The reverse translation, for ADOPTING the mode a live gateway reports (E32). Derived from
# _AWS_MODE rather than spelled out so the two can never drift; total over the AWS
# GatewayPolicyEngineMode enum, which is exactly ['LOG_ONLY', 'ENFORCE'].
_MODE_FROM_AWS = {aws: ours for ours, aws in _AWS_MODE.items()}

# Effect vocabulary (API/service side; cedar_policy_text maps these to permit/forbid).
_EFFECT_ALLOW = "allow"
_EFFECT_DENY = "deny"

# Policy Engine + policy status vocabulary (NOT the gateway READY family).
_ENGINE_ACTIVE = "ACTIVE"
_ENGINE_FAILED = frozenset({"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"})

# Teardown skip reason (E36/T16 fix round 1): the live gateway carries an engine the record
# does not name, so it is not provably ours and is never deleted. SAFE by construction — a
# fixed string, no ids. See `delete_policy_engine`.
_ENGINE_NOT_OWNED = "engine not owned by this record"

# Gateway poll-to-READY loop bounds (cloned from mcp_identity_service — UpdateGateway is
# async; the gateway advances UPDATING→READY). Bounded so a stuck gateway fails loudly.
_POLL_MAX_ATTEMPTS = 60
_POLL_INTERVAL_SECONDS = 5.0
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

# The gateway is briefly UPDATING after any update_gateway; the attach/detach may
# ConflictException. Bounded retry (mirror the E7 retry stance); the poll-to-READY
# handles the async settle.
_CONFLICT_MAX_ATTEMPTS = 5
_CONFLICT_INTERVAL_SECONDS = 3.0

# list_policy_summaries pagination guard — cap pages so a misbehaving control plane can
# never spin the request forever (mirrors mcp_identity_service's page caps).
_MAX_POLICY_PAGES = 50

# The optional gateway fields replayed on the full-replace update_gateway PUT (cloned from
# mcp_identity_service._configure_gateway_authorizer's replay loop) so we never drop them.
_OPTIONAL_GATEWAY_FIELDS = (
    "protocolType",
    "protocolConfiguration",
    "description",
    "kmsKeyArn",
    "interceptorConfigurations",
    "exceptionLevel",
)


class McpCedarError(Exception):
    """A Cedar policy-engine operation failed (engine/policy create/attach/delete, or a
    bad request). Carries a SAFE message (no token/secret); routes map it to 4xx/502."""


class McpCedarService:
    """Orchestrates the AgentCore Gateway native Policy Engine + Cedar policies."""

    def __init__(
        self,
        *,
        registry: McpServerRegistryService,
        control_client=None,
        region: str = "us-east-1",
        engine_name_prefix: str = "agp-cedar-",
        tenant_id: Optional[str] = None,
        login_base: Optional[str] = None,
    ) -> None:
        """``registry`` (for persist_identity + get). ``control_client`` is the
        ``bedrock-agentcore-control`` boto3 client (injected MagicMock in tests).

        ``tenant_id`` / ``login_base`` are accepted for parity with
        ``mcp_identity_service`` but are NOT used: the attach REPLAYS the existing
        ``authorizerConfiguration`` from ``get_gateway``, so we never re-derive the
        discovery URL (the E7 inbound gate is preserved verbatim)."""
        self._registry = registry
        self._region = region
        self._engine_name_prefix = engine_name_prefix
        self._tenant_id = tenant_id
        self._login_base = login_base
        self._control = control_client or boto3.client(
            "bedrock-agentcore-control", region_name=region
        )

    # -- public (async; sync boto3 internals off-loaded) -------------------

    async def add_policy(
        self,
        mcp: McpServer,
        *,
        principal_oid: Optional[str],
        principal_label: str,
        tool_name: Optional[str],
        effect: str = "allow",
        conditions: Optional[list[dict]] = None,
    ) -> dict:
        """Ensure engine + attach (ENFORCE on the first policy), generate Cedar, create the
        policy, wait ``policy_active``. Returns the friendly row
        ``{policy_id, user_oid, user_label, tool, effect, conditions, managed, cedar_text}``.
        ``tool_name=None`` → all-tools (the action clause is omitted).

        ``effect`` (``allow``/``deny``) + parameter ``conditions`` (each
        ``{param, op, value, type}``, validated against the tool's ``input_schema``) extend
        E8 (defaults preserve the E8 behavior). Every request-shape check happens BEFORE any
        AWS call (raising ``McpCedarError`` → route 422) so an invalid request makes no calls."""
        conds = conditions or []

        # -- Validate the request BEFORE touching AWS (no engine ensure/attach on a bad
        #    request). Each failure is a safe McpCedarError → route 422.
        if effect not in (_EFFECT_ALLOW, _EFFECT_DENY):
            raise McpCedarError(f"invalid effect {effect!r}: must be 'allow' or 'deny'")
        if conds and tool_name is None:
            raise McpCedarError("conditions require a specific tool")
        if effect == _EFFECT_ALLOW and principal_oid is None:
            raise McpCedarError("an allow policy requires a user")
        if effect == _EFFECT_DENY and principal_oid is None and not conds:
            raise McpCedarError("an all-users deny needs at least one condition")
        if conds:
            tool = next(
                (t for t in mcp.available_tools if t.name == tool_name), None
            )
            if tool is None:
                raise McpCedarError(f"unknown tool {tool_name!r}")
            try:
                conds = validate_conditions(conds, tool.input_schema)
            except ValueError as err:
                raise McpCedarError(f"invalid condition: {err}") from err

        # Ensure the engine exists (idempotent) — sync boto3 off the loop.
        await anyio.to_thread.run_sync(self._ensure_engine, mcp)

        # First policy ever → attach ENFORCE (default-deny, spec §2) + persist the mode.
        if mcp.cedar_enforcement_mode == _MODE_NONE:
            await anyio.to_thread.run_sync(self._attach_engine, mcp, _MODE_ENFORCE)
            mcp.cedar_enforcement_mode = _MODE_ENFORCE
            self._registry.persist_identity(mcp)

        # Generate the Cedar text (pure helper). build_cedar_policy raises ValueError on a
        # bad oid/tool/condition — surface as a safe McpCedarError (route → 422).
        try:
            statement = build_cedar_policy(
                principal_oid=principal_oid,
                principal_label=principal_label,
                gateway_arn=mcp.gateway_arn or "",
                tool_name=tool_name,
                effect=effect,
                conditions=conds,
            )
        except ValueError as err:
            raise McpCedarError(f"invalid policy input: {err}") from err

        resp = await anyio.to_thread.run_sync(
            self._create_policy, mcp.cedar_policy_engine_id, statement
        )

        # Our own statement always carries a v2 header → parse_cedar_policy never None here.
        parsed = parse_cedar_policy(statement)
        return {
            "policy_id": resp["policyId"],
            "user_oid": parsed["user_oid"],
            "user_label": parsed["user_label"],
            "tool": parsed["tool"],
            "effect": parsed["effect"],
            "conditions": parsed["conditions"],
            "managed": True,
            "cedar_text": statement,
        }

    async def list_policies(self, mcp: McpServer) -> dict:
        """``{enforcement_mode, engine_id, policies:[friendly rows]}``. No engine anywhere →
        empty. Else ``list_policy_summaries`` (paginated) → ``get_policy`` per summary →
        ``parse_cedar_policy``; a foreign/headerless policy yields a row with null user
        fields + the raw Cedar text.

        E32: an EMPTY envelope engine id is no longer sufficient to return empty — a
        re-registered record starts empty while its gateway is still enforcing, and
        reporting ``{mode: "none", policies: []}`` for that gateway is actively misleading
        (the UI renders "none" as *open*, while every unmatched call is being default-denied
        by policies the operator can neither see nor get a ``policy_id`` for). So probe the
        live gateway first, exactly as the mutating paths do. ``_adopt_gateway_engine`` is
        sync boto3 → dispatched via ``anyio.to_thread.run_sync``, never called directly on
        the event loop. It short-circuits with NO AWS call when ``gateway_id`` is unset, so a
        non-gateway MCP still never touches the control client. The adoption is in-memory
        only: this is a read path, and persisting from a GET is left to the mutating
        callers."""
        if not mcp.cedar_policy_engine_id:
            await anyio.to_thread.run_sync(self._adopt_gateway_engine, mcp)
        if not mcp.cedar_policy_engine_id:
            return {
                "enforcement_mode": mcp.cedar_enforcement_mode or _MODE_NONE,
                "engine_id": None,
                "policies": [],
            }
        rows = await anyio.to_thread.run_sync(
            self._list_policies_sync, mcp.cedar_policy_engine_id
        )
        return {
            "enforcement_mode": mcp.cedar_enforcement_mode,
            "engine_id": mcp.cedar_policy_engine_id,
            "policies": rows,
        }

    async def delete_policy(self, mcp: McpServer, policy_id: str) -> None:
        """``delete_policy`` + wait ``policy_deleted``. Engine unset → ``McpCedarError``
        (route → 404). Engine + attachment are untouched."""
        if not mcp.cedar_policy_engine_id:
            raise McpCedarError("no policy engine on this gateway")
        await anyio.to_thread.run_sync(
            self._delete_policy_sync, mcp.cedar_policy_engine_id, policy_id
        )

    async def set_enforcement(self, mcp: McpServer, mode: str) -> McpServer:
        """``mode ∈ {log_only, enforce, disabled}`` (else ``McpCedarError`` → route 400).
        ``enforce``/``log_only``: ensure engine → attach → persist the mode.
        ``disabled``: detach (omit ``policyEngineConfiguration``, replay authorizer) →
        persist ``"none"``. Returns the updated mcp."""
        if mode in (_MODE_LOG_ONLY, _MODE_ENFORCE):
            await anyio.to_thread.run_sync(self._ensure_engine, mcp)
            await anyio.to_thread.run_sync(self._attach_engine, mcp, mode)
            mcp.cedar_enforcement_mode = mode
            self._registry.persist_identity(mcp)
            return mcp
        if mode == "disabled":
            await anyio.to_thread.run_sync(self._detach_engine, mcp)
            mcp.cedar_enforcement_mode = _MODE_NONE
            self._registry.persist_identity(mcp)
            return mcp
        raise McpCedarError(f"unknown enforcement mode: {mode}")

    async def delete_policy_engine(self, mcp: McpServer) -> Optional[str]:
        """DETACH the engine from its gateway, then DELETE it — the teardown twin of
        ``_ensure_engine``'s create (E36/T16, research item 5A).

        The half of the engine lifecycle that never existed: ``DELETE /mcp-servers/{id}``
        deleted the registry record and nothing else, so the LIVE gateway stayed attached to
        an ENFORCE-mode policy engine that nothing in the platform pointed at any more. Cedar
        kept default-denying every call and the only way back was
        :meth:`_adopt_gateway_engine`'s re-registration path.

        Returns ``None`` when the engine was deleted or there was nothing to delete, and a
        SAFE skip REASON string when an engine exists that this record does not OWN — the
        caller turns that into a ``skipped`` line-item rather than a false ``deleted``.

        ONLY AN ENGINE ID STORED ON THE RECORD IS EVER DELETED (E36/T16 fix round 1). The
        first cut adopted whatever engine the LIVE gateway reported when the record's engine
        id was empty (E32's re-registered record starts that way) and then deleted it. But
        ``gateway_id`` descends from ``gateway_arn``, a CALLER-SETTABLE field on
        ``McpServerCreate``/``McpServerUpdate``, so "the engine that gateway reports" is not
        provably this record's: a record pointed — by typo or deliberately — at another team's
        gateway made a plain record delete destroy that gateway's engine, every policy in it
        and its default-deny. Deleting a policy engine is irreversible, so the live gateway is
        still ASKED (a pure read, so an operator learns the engine survives) and NEVER
        deleted, and it is not detached either: detaching is precisely the fail-open act.
        The cost is the E32 orphan this method was written for — a re-registered record's
        engine keeps default-denying until it is reclaimed by hand — which is the fail-CLOSED
        side of the trade.

        Idempotent, and best-effort in the same directions the create path is exact:

          - EMPTY envelope engine id ⇒ nothing is deleted. The gateway is read; if it still
            carries an engine the skip reason names that, and an unreachable/unreadable
            gateway is a logged no-op.
          - no engine anywhere ⇒ return without a single mutating call.
          - the DETACH fires only when the LIVE gateway reports THIS record's engine ARN
            (E36/T16 fix round 2). A detach is a full-replace ``update_gateway`` that strips
            Cedar from whatever gateway it is pointed at, and ``gateway_id`` descends from the
            caller-settable ``gateway_arn`` — so a record pointed at another team's gateway
            must not strip ITS engine either. Anything else (a different engine, or none) is
            left attached and logged; the record's own engine is still deleted.
          - a DETACH that fails (typically: the gateway was deleted first, so ``get_gateway``
            answers ``ResourceNotFoundException``) is logged and the engine is deleted ANYWAY.
            The engine is the resource that keeps denying; refusing to delete it because its
            gateway is gone would leak the very thing the cascade is for.
          - ``ResourceNotFoundException`` on the delete == success (the desired end state).
            Any other ``ClientError`` becomes :class:`McpCedarError` so the CALLER reports it
            as a failed cascade line-item rather than swallowing it.

        FAIL-OPEN ORDER (E36/T16 fix round 1): nothing deletes the GATEWAY — the platform did
        not create it — so this method strips AUTHORIZATION from a resource that stays live.
        Its caller therefore tears the Entra application (AUTHENTICATION) down FIRST, so a
        sequence that dies in the middle leaves the gateway unable to mint a token rather than
        serving every tool call with Cedar removed. See ``routes/mcp_servers.py``'s
        ``_teardown_mcp_identity``.

        The record's ``cedar_policy_engine_id`` / ``_arn`` / ``_enforcement_mode`` are NOT
        cleared+persisted: the only caller deletes the record immediately afterwards, so a
        ``persist_identity`` here would be an ``UpdateRegistryRecord`` write against a row on
        its way out (and, if it failed, would turn a clean teardown into a 500).
        """
        return await anyio.to_thread.run_sync(self._delete_policy_engine_sync, mcp)

    # -- private sync boto3 (dispatched off-loop) --------------------------

    def _live_engine_config(self, gateway_id: str) -> dict:
        """The ``policyEngineConfiguration`` the LIVE gateway reports, or ``{}``. A pure READ
        (``get_gateway`` only, never ``update_gateway``). ONE derivation shared by
        :meth:`_adopt_gateway_engine` and the teardown's ownership check, so both ask the same
        question of the same shape."""
        return (self._control.get_gateway(gatewayIdentifier=gateway_id) or {}).get(
            "policyEngineConfiguration"
        ) or {}

    def _adopt_gateway_engine(self, mcp: McpServer) -> bool:
        """ADOPT (onto ``mcp``, in memory) the policy engine the LIVE gateway already
        reports. Returns True when something was adopted, False otherwise. A pure READ —
        ``get_gateway`` only, never ``update_gateway`` — so it cannot disturb the E7 inbound
        authorizer (research §3.1). Sync (boto3); callers dispatch it off-loop.

        E32: a re-registered MCP record starts with an EMPTY envelope engine id while the
        live gateway still has its engine attached (record ids change on re-registration;
        gateways and policy engines do not — they stay in the bedrock-agentcore namespace).
        Creating a second engine would silently orphan the first one and every policy in it.
        Verified live before this fix: gateway agp-contact-center-mcp-lvpg04fpeh carries
        policyEngineConfiguration in ENFORCE mode, so a re-registration would have
        duplicated an ENFORCING engine.

        The adopted engine's NAME keeps the OLD record id (agp_cedar_<oldRecordId>-...).
        That is accepted drift (spec D8): renaming/recreating would churn live ENFORCE-mode
        authorization for cosmetics, so the id is taken verbatim from the ARN tail.

        The reported ``mode`` is adopted ALONGSIDE the ARN, and that is security-relevant,
        not cosmetic. Without it the envelope stays ``"none"``, which has two verified
        consequences: (1) ``add_policy``'s ``== _MODE_NONE`` branch would re-attach
        ENFORCE, silently ESCALATING a gateway deliberately left in LOG_ONLY observe-only
        staging (``log_only`` is fully reachable — the route accepts it and the UI offers
        it); and (2) the read path would report ``enforcement_mode: "none"`` — rendered as
        "open" — for a gateway that is actually default-denying.

        An unrecognised or absent mode is NEVER guessed at: ``_MODE_FROM_AWS`` is total over
        today's enum (``['LOG_ONLY', 'ENFORCE']``, and ``mode`` is required on the
        ``GetGateway`` output shape), so a miss means AWS added a value whose semantics we do
        not know, and inventing one could either escalate or weaken live enforcement. What we
        do instead depends on whether the RECORD has a trustworthy mode to fall back on:

          - it already records ``log_only``/``enforce`` → keep that value untouched and warn.
            Safe: ``add_policy``'s ``== _MODE_NONE`` attach branch is already unreachable.
          - it records ``"none"`` (or nothing) — the state a freshly re-registered E32 record
            starts in → ``McpCedarError``. Keeping ``"none"`` is NOT neutral here: verified
            that ``mode='AUDIT'``, an absent ``mode`` and ``mode=''`` all end up taking
            ``add_policy``'s ENFORCE attach, i.e. an unknown gateway mode would silently
            switch on default-deny. Fail loud so an operator sees the unknown value."""
        if not mcp.gateway_id:
            return False
        existing = self._live_engine_config(mcp.gateway_id)
        existing_arn = existing.get("arn")
        if not existing_arn:
            return False
        mcp.cedar_policy_engine_arn = existing_arn
        mcp.cedar_policy_engine_id = existing_arn.rsplit("/", 1)[-1]
        adopted_mode = _MODE_FROM_AWS.get(existing.get("mode"))
        if adopted_mode is not None:
            mcp.cedar_enforcement_mode = adopted_mode
        elif mcp.cedar_enforcement_mode in _AWS_MODE:
            logger.warning(
                "[mcp_cedar] gateway %s reports policy-engine mode %r, which we do not "
                "recognise; keeping the recorded mode %r",
                mcp.gateway_id,
                existing.get("mode"),
                mcp.cedar_enforcement_mode,
            )
        else:
            # No recorded mode to fall back on → refuse rather than let the caller attach
            # ENFORCE off the back of a mode we cannot interpret.
            raise McpCedarError(
                f"gateway {mcp.gateway_id} reports unrecognised policy-engine mode "
                f"{existing.get('mode')!r}"
            )
        return True

    def _ensure_engine(self, mcp: McpServer) -> None:
        """Create the Policy Engine + wait ``policy_engine_active`` + persist its ids.

        Idempotent on TWO levels: the record's ``cedar_policy_engine_id`` (fast path), and —
        when that envelope field is empty — whatever the LIVE gateway already reports
        (``_adopt_gateway_engine``, which is strictly upstream of the only
        ``create_policy_engine`` call in this file).

        A successful adoption is PERSISTED here, and deliberately here rather than inside
        ``_adopt_gateway_engine``: the probe is shared with ``list_policies``, whose route is
        gated at ``Role.VIEWER`` (``routes/mcp_cedar.py:136-140``), so persisting inside it
        would let a VIEWER GET issue an ``UpdateRegistryRecord`` write. ``_ensure_engine`` is
        reached only from the OPERATOR-gated mutating callers (``add_policy`` /
        ``set_enforcement``), so the write belongs on this side of the split.

        Without the persist the adoption dies with the request: verified that every HTTP
        request re-hydrates a FRESH ``McpServer`` from the registry record
        (``routes/mcp_cedar.py:121`` → ``mcp_server_service._hydrate`` → ``get_registry_record``;
        nothing caches the record). So after Task 8's re-registration an operator could POST a
        policy (201, adopted in memory) and then get **404** on the DELETE of that very row,
        because the next request's fresh object still had the empty envelope and
        ``delete_policy`` raises on it. Persisting on first mutating use heals the envelope so
        the following request hydrates the adopted engine."""
        if mcp.cedar_policy_engine_id:
            return

        if self._adopt_gateway_engine(mcp):
            self._registry.persist_identity(mcp)
            return

        resp = self._control.create_policy_engine(
            name=self._engine_name(mcp),
            description=f"Cedar policy engine for gateway {mcp.name}",
            # str(uuid4())=36 chars: AWS ClientToken requires min length 33 (uuid4().hex is only 32)
            clientToken=str(uuid4()),
        )
        engine_id = resp["policyEngineId"]
        self._control.get_waiter("policy_engine_active").wait(policyEngineId=engine_id)
        mcp.cedar_policy_engine_id = engine_id
        mcp.cedar_policy_engine_arn = resp["policyEngineArn"]
        self._registry.persist_identity(mcp)

    def _engine_name(self, mcp: McpServer) -> str:
        """A Policy Engine name matching ``^[A-Za-z][A-Za-z0-9_]*$`` (the AWS pattern).

        The default prefix ``agp-cedar-`` carries a hyphen, so sanitize the WHOLE
        concatenation (replace any char not ``[A-Za-z0-9_]`` with ``_``), guarantee a
        leading alpha, and truncate to ≤63."""
        name = re.sub(r"[^A-Za-z0-9_]", "_", f"{self._engine_name_prefix}{mcp.id}")
        if not name or not name[0].isalpha():
            name = "e" + name
        return name[:63]

    def _create_policy(self, engine_id: str, statement: str) -> dict:
        """``create_policy`` + wait ``policy_active``. The policy name matches
        ``^[A-Za-z][A-Za-z0-9_]*$`` (≤48 chars) — ``agppol`` + a short uuid (no hyphens)."""
        resp = self._control.create_policy(
            policyEngineId=engine_id,
            name="agppol" + uuid4().hex[:16],
            definition={"cedar": {"statement": statement}},
        )
        policy_id = resp["policyId"]
        self._control.get_waiter("policy_active").wait(
            policyEngineId=engine_id, policyId=policy_id
        )
        return resp

    def _list_policies_sync(self, engine_id: str) -> list[dict]:
        """``list_policy_summaries`` (paginate on ``nextToken``, bounded) → ``get_policy``
        per summary → ``parse_cedar_policy`` → a friendly row. ``list_policy_summaries``
        does NOT carry the Cedar text, so ``get_policy`` is required per policy."""
        rows: list[dict] = []
        next_token: Optional[str] = None
        for _ in range(_MAX_POLICY_PAGES):
            kwargs: dict = {"policyEngineId": engine_id}
            if next_token:
                kwargs["nextToken"] = next_token
            page = self._control.list_policy_summaries(**kwargs)
            for summary in page.get("policies") or []:
                policy_id = summary.get("policyId")
                if not policy_id:
                    continue
                detail = self._control.get_policy(
                    policyEngineId=engine_id, policyId=policy_id
                )
                statement = (
                    (detail.get("definition") or {}).get("cedar") or {}
                ).get("statement") or ""
                parsed = parse_cedar_policy(statement)
                if parsed is None:
                    # Foreign / headerless policy — show raw Cedar, no friendly fields; the
                    # effect is sniffed from the keyword (detect_effect) for the badge.
                    rows.append(
                        {
                            "policy_id": policy_id,
                            "user_oid": None,
                            "user_label": None,
                            "tool": None,
                            "effect": detect_effect(statement),
                            "conditions": [],
                            "managed": False,
                            "cedar_text": statement,
                        }
                    )
                else:
                    rows.append(
                        {
                            "policy_id": policy_id,
                            "user_oid": parsed.get("user_oid"),
                            "user_label": parsed.get("user_label"),
                            "tool": parsed.get("tool"),
                            "effect": parsed.get("effect"),
                            "conditions": parsed.get("conditions"),
                            "managed": True,
                            "cedar_text": statement,
                        }
                    )
            next_token = page.get("nextToken")
            if not next_token:
                break
        return rows

    def _delete_policy_sync(self, engine_id: str, policy_id: str) -> None:
        """``delete_policy`` + wait ``policy_deleted``. A control-plane ``ClientError`` (e.g.
        the policy is gone) surfaces as a safe ``McpCedarError`` (route → 404)."""
        try:
            self._control.delete_policy(policyEngineId=engine_id, policyId=policy_id)
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            logger.warning(
                "[mcp_cedar] delete_policy failed for policy %s on engine %s: %s",
                policy_id,
                engine_id,
                code,
            )
            raise McpCedarError("policy delete failed") from err
        self._control.get_waiter("policy_deleted").wait(
            policyEngineId=engine_id, policyId=policy_id
        )

    def _delete_policy_engine_sync(self, mcp: McpServer) -> Optional[str]:
        """``_detach_engine`` → ``delete_policy_engine`` + wait ``policy_engine_deleted``.

        The sync body of :meth:`delete_policy_engine` — see that docstring for the ownership
        rule and every idempotency direction. Deleting the ENGINE cascades its policies, so
        there is no per-policy loop here. Returns ``None``, or the skip reason when an engine
        exists that this record does not own."""
        engine_id = mcp.cedar_policy_engine_id
        if not engine_id:
            if not mcp.gateway_id:
                logger.info(
                    "[mcp_cedar] no policy engine on record for MCP %s — nothing to tear down",
                    mcp.id,
                )
                return None
            # The record names a GATEWAY but no engine, so nothing here is provably ours:
            # `gateway_id` descends from the caller-settable `gateway_arn`. READ the gateway
            # (so the report can say the engine survives) and delete nothing — a policy-engine
            # delete is irreversible and a detach is the fail-open act.
            try:
                live_arn = self._live_engine_config(mcp.gateway_id).get("arn")
            except ClientError:
                logger.warning(
                    "[mcp_cedar] could not read gateway %s while looking for a policy "
                    "engine to tear down; nothing to delete",
                    mcp.gateway_id,
                )
                return None
            if not live_arn:
                return None
            logger.warning(
                "[mcp_cedar] gateway %s still carries a policy engine, but MCP record %s "
                "does not own it (empty cedar_policy_engine_id) — NOT deleting it; reclaim "
                "by hand once it is confirmed an orphan",
                mcp.gateway_id,
                mcp.id,
            )
            return _ENGINE_NOT_OWNED

        if mcp.gateway_id:
            try:
                # DETACH ONLY WHAT WE OWN (E36/T16 fix round 2): the detach is a full-replace
                # `update_gateway` that omits `policyEngineConfiguration`, i.e. it strips Cedar
                # from whatever gateway `gateway_id` names — and that id descends from the
                # caller-settable `gateway_arn`. The same READ the empty-id branch uses above
                # answers whether this gateway carries THIS engine; a mismatch (or nothing
                # attached) is left alone, and the engine we do own is still deleted below.
                live_arn = self._live_engine_config(mcp.gateway_id).get("arn")
                if live_arn == mcp.cedar_policy_engine_arn:
                    self._detach_engine(mcp)
                else:
                    logger.warning(
                        "[mcp_cedar] gateway %s does not carry policy engine %s (it reports "
                        "%s) — NOT detaching it; deleting only this record's engine",
                        mcp.gateway_id,
                        engine_id,
                        live_arn or "no engine",
                    )
            except (ClientError, McpCedarError):
                # Typically the gateway is already gone. Delete the engine anyway — it is
                # what keeps default-denying.
                logger.warning(
                    "[mcp_cedar] detach failed for gateway %s (already gone or busy); "
                    "deleting policy engine %s anyway",
                    mcp.gateway_id,
                    engine_id,
                )

        try:
            self._control.delete_policy_engine(policyEngineId=engine_id)
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            if code == "ResourceNotFoundException":
                logger.info(
                    "[mcp_cedar] policy engine %s already gone — treating as success",
                    engine_id,
                )
                return None
            logger.warning(
                "[mcp_cedar] delete_policy_engine failed for engine %s: %s",
                engine_id,
                code,
            )
            raise McpCedarError("policy engine delete failed") from err
        self._control.get_waiter("policy_engine_deleted").wait(policyEngineId=engine_id)
        logger.info("[mcp_cedar] deleted policy engine %s", engine_id)
        return None

    # -- gateway attach/detach (sync, off-loop) ----------------------------

    def _attach_engine(self, mcp: McpServer, mode: str) -> None:
        """ASSOCIATE the engine to the gateway: GET→replay→``update_gateway`` (with
        ``policyEngineConfiguration``)→poll READY. Replays ``authorizerType`` +
        ``authorizerConfiguration`` + optional fields so the E7 inbound gate is preserved
        (research §3.1)."""
        kwargs = self._replay_kwargs(mcp.gateway_id)
        kwargs["policyEngineConfiguration"] = {
            "arn": mcp.cedar_policy_engine_arn,
            "mode": _AWS_MODE[mode],
        }
        self._update_gateway_with_conflict_retry(kwargs)
        self._poll_gateway_ready(mcp.gateway_id)

    def _detach_engine(self, mcp: McpServer) -> None:
        """DETACH the engine: same GET→replay→``update_gateway``→poll but OMIT
        ``policyEngineConfiguration`` (the full-replace PUT drops it = detach). Still
        replays ``authorizerType`` + ``authorizerConfiguration`` (research §3.1)."""
        kwargs = self._replay_kwargs(mcp.gateway_id)
        self._update_gateway_with_conflict_retry(kwargs)
        self._poll_gateway_ready(mcp.gateway_id)

    def _replay_kwargs(self, gateway_id: str) -> dict:
        """Build the full-replace ``update_gateway`` kwargs from a fresh ``get_gateway``:
        the required fields + the inbound authorizer (replayed verbatim) + the optional
        fields if present. Cloned from
        ``mcp_identity_service._configure_gateway_authorizer``'s replay set; the authorizer
        replay is the load-bearing security property (never strip the E7 gate)."""
        gw = self._control.get_gateway(gatewayIdentifier=gateway_id)
        kwargs: dict = {
            "gatewayIdentifier": gw["gatewayId"],
            "name": gw["name"],
            "roleArn": gw["roleArn"],
            "authorizerType": gw["authorizerType"],
            "authorizerConfiguration": gw["authorizerConfiguration"],
        }
        for key in _OPTIONAL_GATEWAY_FIELDS:
            if gw.get(key):
                kwargs[key] = gw[key]
        return kwargs

    def _update_gateway_with_conflict_retry(self, kwargs: dict) -> None:
        """``update_gateway`` with a bounded ``ConflictException`` retry — the gateway is
        briefly UPDATING after any prior update, so the call can conflict; retry then
        re-raise. Any other ``ClientError`` raises immediately."""
        last_err: Optional[ClientError] = None
        for _ in range(_CONFLICT_MAX_ATTEMPTS):
            try:
                self._control.update_gateway(**kwargs)
                return
            except ClientError as err:
                code = err.response.get("Error", {}).get("Code", "")
                if code != "ConflictException":
                    raise
                last_err = err
                logger.info(
                    "[mcp_cedar] update_gateway conflict (gateway busy); retrying"
                )
                time.sleep(_CONFLICT_INTERVAL_SECONDS)
        # Exhausted the retries on a persistent conflict.
        raise McpCedarError("gateway update conflicted (still updating)") from last_err

    def _poll_gateway_ready(self, gateway_id: str) -> None:
        """Poll ``get_gateway`` until status READY (bounded loop + sleep). A
        ``*_FAILED``/``UPDATE_UNSUCCESSFUL`` status, or exhausting the loop, raises.
        Cloned from ``mcp_identity_service._poll_gateway_ready``. ``time.sleep`` is patched
        to a no-op in tests."""
        for _ in range(_POLL_MAX_ATTEMPTS):
            resp = self._control.get_gateway(gatewayIdentifier=gateway_id)
            status = resp.get("status")
            if status == _READY_STATUS:
                return
            if status in _FAILED_STATUSES:
                raise McpCedarError(
                    f"gateway {gateway_id} reached terminal status {status}"
                )
            time.sleep(_POLL_INTERVAL_SECONDS)
        raise McpCedarError(
            f"gateway {gateway_id} did not reach READY within "
            f"{_POLL_MAX_ATTEMPTS} polls"
        )
