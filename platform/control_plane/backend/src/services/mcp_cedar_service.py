"""Cedar policy engines for AgentCore gateways: per-tool authorization from a friendly form.

``McpCedarService`` turns a gateway MCP server's authorization form (an Entra user's ``oid``, a
tool or all tools, allow or deny, parameter conditions) into a native Policy Engine plus a Cedar
``permit`` or ``forbid`` policy on the gateway: the first policy creates the engine (idempotent)
and attaches it in ``ENFORCE`` mode through ``update_gateway``'s ``policyEngineConfiguration``,
after which the gateway default-denies; removing a policy leaves the engine; disabling detaches
the engine by omitting ``policyEngineConfiguration`` from the full-replace update.

Every gateway update is a GET -> replay -> ``update_gateway`` -> poll-to-READY that replays the
inbound ``authorizerType`` and ``authorizerConfiguration`` verbatim, so the Entra ``CUSTOM_JWT``
gate is never stripped. The sync boto3 work is dispatched off the event loop. The Cedar text is
generated and parsed by the pure ``cedar_policy_text`` module; the gateway evaluates it.

The engine and policy statuses (``CREATING|ACTIVE|UPDATING|DELETING|*_FAILED``, success
``ACTIVE``) are a different family from the gateway's (``CREATING|UPDATING|READY|FAILED``, success
``READY``): the installed boto3 waiters settle engines and policies, ``_poll_gateway_ready`` the
gateway.
Decision: docs/adr/backend-mcp-teardown-authentication-before-authorization.md
"""

from __future__ import annotations

import functools
import logging
import re
import time
from collections.abc import Mapping
from typing import ClassVar, Optional
from uuid import uuid4

import anyio
import anyio.to_thread
from botocore.exceptions import ClientError, WaiterError

from core import aws_clients
from core.errors import AppError, Outcome
from models.mcp_server import CedarEnforcementMode, McpServer
from services.cedar_policy_text import (
    CedarEffect,
    build_agent_forbid,
    build_cedar_policy,
    detect_effect,
    parse_cedar_policy,
    validate_conditions,
)
from services.mcp_server_service import McpServerRegistryService

logger = logging.getLogger(__name__)

_MODE_NONE: CedarEnforcementMode = "none"
_MODE_LOG_ONLY: CedarEnforcementMode = "log_only"
_MODE_ENFORCE: CedarEnforcementMode = "enforce"
_AWS_MODE: dict[CedarEnforcementMode, str] = {_MODE_LOG_ONLY: "LOG_ONLY", _MODE_ENFORCE: "ENFORCE"}
_MODE_FROM_AWS: dict[str, CedarEnforcementMode] = {aws: ours for ours, aws in _AWS_MODE.items()}

_EFFECT_ALLOW: CedarEffect = "allow"
_EFFECT_DENY: CedarEffect = "deny"

_ENGINE_ACTIVE = "ACTIVE"
_ENGINE_FAILED = frozenset({"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"})

_ENGINE_NOT_OWNED = "engine not owned by this record"

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

_CONFLICT_MAX_ATTEMPTS = 5
_CONFLICT_INTERVAL_SECONDS = 3.0

_MAX_POLICY_PAGES = 50

_OPTIONAL_GATEWAY_FIELDS = (
    "protocolType",
    "protocolConfiguration",
    "description",
    "kmsKeyArn",
    "interceptorConfigurations",
    "exceptionLevel",
)


class McpCedarError(AppError):
    """A Cedar policy-engine operation was refused or failed.

    Every kind has a fixed detail except ``invalid_mode``, whose message is this module's own
    sentence; the other messages name the MCP server, the gateway, the engine, the policy or the
    rejected input for the log and never carry an AWS response body. ``policy_not_found`` and
    ``policy_delete_failed`` are raised ``from`` the ``ClientError`` so callers can read its code
    (``containment_service`` reads ``ResourceNotFoundException`` off ``__cause__``).
    """

    domain: ClassVar[str] = "mcp_cedar"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "invalid_policy": Outcome(422, "failed to create policy (invalid Cedar or gateway error)"),
        "policy_create_failed": Outcome(502, "failed to create policy"),
        "policy_not_found": Outcome(404, "policy not found"),
        "policy_delete_failed": Outcome(404, "policy not found"),
        "gateway_update_failed": Outcome(502, "failed to update enforcement"),
        "unrecognised_engine_mode": Outcome(
            502, "the gateway reports a policy-engine mode this platform does not recognise"
        ),
        "invalid_mode": Outcome(400, None),
        "gateway_not_provisioned": Outcome(409, "gateway identity is not provisioned"),
        "engine_delete_failed": Outcome(502, "failed to delete policy engine"),
        "not_gateway": Outcome(409, "policies are only available for gateway MCP servers"),
        "containment_policy": Outcome(
            409, "this policy belongs to an agent containment; restore the agent instead"
        ),
    }


def _required_gateway_id(mcp: McpServer) -> str:
    """The gateway id a gateway update needs; a record without one has no gateway to update."""
    if not mcp.gateway_id:
        raise McpCedarError(f"MCP {mcp.id} has no provisioned gateway", "gateway_not_provisioned")
    return mcp.gateway_id


class McpCedarService:
    """Orchestrates the AgentCore Gateway native Policy Engine + Cedar policies."""

    def __init__(
        self,
        *,
        registry: McpServerRegistryService,
        control_client=None,
        region: str | None = None,
        engine_name_prefix: str = "agp-cedar-",
        tenant_id: Optional[str] = None,
        login_base: Optional[str] = None,
    ) -> None:
        """``registry`` for ``persist_identity`` and ``get``; ``control_client`` is the
        ``bedrock-agentcore-control`` boto3 client, injected in tests. ``tenant_id`` and
        ``login_base`` are accepted for parity with ``mcp_identity_service`` and unused: the attach
        replays the existing ``authorizerConfiguration`` and never re-derives the discovery URL."""
        self._registry = registry
        self._region = region
        self._engine_name_prefix = engine_name_prefix
        self._tenant_id = tenant_id
        self._login_base = login_base
        self._control = control_client or aws_clients.client("bedrock-agentcore-control", region=region)

    async def add_policy(
        self,
        mcp: McpServer,
        *,
        principal_oid: Optional[str],
        principal_label: str,
        tool_name: Optional[str],
        effect: CedarEffect = "allow",
        conditions: Optional[list[dict]] = None,
    ) -> dict:
        """Ensure the engine, attach it in ``ENFORCE`` on the first policy, generate the Cedar text,
        create the policy and wait for it to be active. Returns the friendly row
        ``{policy_id, user_oid, user_label, tool, effect, conditions, managed, cedar_text}``;
        ``tool_name=None`` means all tools.

        Every request-shape check runs before any AWS call, so an invalid request makes none:
        ``effect`` must be ``allow`` or ``deny``, conditions need a specific tool the record
        declares, an allow needs a user, and an all-users deny needs at least one condition.
        """
        conds = conditions or []

        if effect not in (_EFFECT_ALLOW, _EFFECT_DENY):
            raise McpCedarError(f"invalid effect {effect!r}: must be 'allow' or 'deny'", "invalid_policy")
        if conds and tool_name is None:
            raise McpCedarError("conditions require a specific tool", "invalid_policy")
        if effect == _EFFECT_ALLOW and principal_oid is None:
            raise McpCedarError("an allow policy requires a user", "invalid_policy")
        if effect == _EFFECT_DENY and principal_oid is None and not conds:
            raise McpCedarError("an all-users deny needs at least one condition", "invalid_policy")
        if conds:
            tool = next((t for t in mcp.available_tools if t.name == tool_name), None)
            if tool is None:
                raise McpCedarError(f"unknown tool {tool_name!r}", "invalid_policy")
            try:
                conds = validate_conditions(conds, tool.input_schema)
            except ValueError as err:
                raise McpCedarError(f"invalid condition: {err}", "invalid_policy") from err

        engine_id = await anyio.to_thread.run_sync(self._ensure_engine, mcp)

        if mcp.cedar_enforcement_mode == _MODE_NONE:
            await anyio.to_thread.run_sync(self._attach_engine, mcp, _MODE_ENFORCE)
            mcp.cedar_enforcement_mode = _MODE_ENFORCE
            await anyio.to_thread.run_sync(self._registry.persist_identity, mcp)

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
            raise McpCedarError(f"invalid policy input: {err}", "invalid_policy") from err

        resp = await anyio.to_thread.run_sync(self._create_policy, engine_id, statement)

        parsed = parse_cedar_policy(statement) or {}
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

    async def add_agent_forbid(self, mcp: McpServer, *, agent_app_id: str, agent_label: str) -> dict:
        """Write an agent-scoped forbid on every tool of a gateway that is already enforcing Cedar.

        Returns ``{"status": "ok", "policy_id"}`` or ``{"status": "skipped", "detail": "gateway
        not enforcing Cedar"}``. Never ensures or attaches an engine or changes the mode: attaching
        to a mode-``none`` gateway would flip it to ``ENFORCE`` and default-deny every caller, and a
        ``log_only`` forbid enforces nothing. The skip path makes no AWS call. The policy is
        created with ``validationMode=IGNORE_ALL_FINDINGS``, because the validator flags an
        all-tools forbid as overly restrictive (which is the point) and the policy would end
        ``CREATE_FAILED``; only this path relaxes validation.
        """
        engine_id = mcp.cedar_policy_engine_id
        if mcp.cedar_enforcement_mode != _MODE_ENFORCE or not engine_id:
            return {"status": "skipped", "detail": "gateway not enforcing Cedar"}
        try:
            statement = build_agent_forbid(
                agent_app_id=agent_app_id,
                agent_label=agent_label,
                gateway_arn=mcp.gateway_arn or "",
            )
        except ValueError as err:
            raise McpCedarError(f"invalid policy input: {err}", "invalid_policy") from err
        try:
            resp = await anyio.to_thread.run_sync(
                functools.partial(
                    self._create_policy,
                    engine_id,
                    statement,
                    validation_mode="IGNORE_ALL_FINDINGS",
                )
            )
        except (ClientError, WaiterError) as err:
            raise McpCedarError("agent forbid create failed", "policy_create_failed") from err
        return {"status": "ok", "policy_id": resp["policyId"]}

    async def list_policies(self, mcp: McpServer) -> dict:
        """``{enforcement_mode, engine_id, policies: [friendly rows]}``.

        When the record carries no engine id the live gateway is probed first and its engine
        adopted in memory only (this is a read path; the mutating callers persist): a re-registered
        record starts empty while its gateway may still be enforcing, and reporting ``none`` for it
        would show an open gateway that is default-denying every unmatched call. No engine anywhere
        gives an empty set; a non-gateway record never touches the control client.
        """
        if not mcp.cedar_policy_engine_id:
            await anyio.to_thread.run_sync(self._adopt_gateway_engine, mcp)
        if not mcp.cedar_policy_engine_id:
            return {
                "enforcement_mode": mcp.cedar_enforcement_mode or _MODE_NONE,
                "engine_id": None,
                "policies": [],
            }
        rows = await anyio.to_thread.run_sync(self._list_policies_sync, mcp.cedar_policy_engine_id)
        return {
            "enforcement_mode": mcp.cedar_enforcement_mode,
            "engine_id": mcp.cedar_policy_engine_id,
            "policies": rows,
        }

    async def delete_policy(self, mcp: McpServer, policy_id: str) -> None:
        """``delete_policy`` and wait ``policy_deleted``; the engine and its attachment are
        untouched. A record without an engine has no policy to delete."""
        if not mcp.cedar_policy_engine_id:
            raise McpCedarError("no policy engine on this gateway", "policy_not_found")
        await anyio.to_thread.run_sync(self._delete_policy_sync, mcp.cedar_policy_engine_id, policy_id)

    async def policy_kind(self, mcp: McpServer, policy_id: str) -> Optional[str]:
        """The ``kind`` of one policy (``"contain"`` or ``None``) from a single ``get_policy``; an
        unset engine or a failed lookup is ``None`` (the delete that follows reports the 404)."""
        engine_id = mcp.cedar_policy_engine_id
        if not engine_id:
            return None

        def _get() -> Optional[str]:
            try:
                detail = self._control.get_policy(policyEngineId=engine_id, policyId=policy_id)
            except ClientError:
                return None
            statement = ((detail.get("definition") or {}).get("cedar") or {}).get("statement") or ""
            parsed = parse_cedar_policy(statement)
            return parsed.get("kind") if parsed else None

        return await anyio.to_thread.run_sync(_get)

    async def set_enforcement(self, mcp: McpServer, mode: str) -> McpServer:
        """``enforce`` or ``log_only``: ensure the engine, attach it in that mode, persist the mode.
        ``disabled``: detach (replaying the authorizer) and persist ``none``. Returns the updated
        record."""
        if mode in (_MODE_LOG_ONLY, _MODE_ENFORCE):
            await anyio.to_thread.run_sync(self._ensure_engine, mcp)
            await anyio.to_thread.run_sync(self._attach_engine, mcp, mode)
            mcp.cedar_enforcement_mode = mode
            await anyio.to_thread.run_sync(self._registry.persist_identity, mcp)
            return mcp
        if mode == "disabled":
            await anyio.to_thread.run_sync(self._detach_engine, mcp)
            mcp.cedar_enforcement_mode = _MODE_NONE
            await anyio.to_thread.run_sync(self._registry.persist_identity, mcp)
            return mcp
        raise McpCedarError(f"unknown enforcement mode: {mode}", "invalid_mode")

    async def delete_policy_engine(self, mcp: McpServer) -> Optional[str]:
        """Detach the record's engine from its gateway, then delete it: the teardown twin of
        ``_ensure_engine``.

        Only an engine id stored on the record is ever deleted or detached. ``gateway_id`` descends
        from the caller-settable ``gateway_arn``, so the engine the live gateway reports is not
        provably this record's: with an empty record id the gateway is read (so the report can say
        the engine survives) and nothing is deleted or detached, because a policy-engine delete is
        irreversible and a detach is the fail-open act. Returns ``None`` when the engine was deleted
        or there was nothing to delete, and a safe skip reason when an engine exists that this
        record does not own.

        Idempotent: the detach fires only when the live gateway reports this record's engine ARN;
        a detach that fails (typically the gateway is already gone) is logged and the engine is
        deleted anyway, because the engine is what keeps denying; ``ResourceNotFoundException`` on
        the delete is success; any other ``ClientError`` is ``engine_delete_failed`` so the caller
        reports a failed line-item. The record's engine fields are not cleared, because the only
        caller deletes the record next. The caller tears the Entra application down first
        (``services.mcp_teardown``), so a sequence that dies in the middle leaves a gateway that
        cannot mint a token rather than one serving with Cedar removed.
        """
        return await anyio.to_thread.run_sync(self._delete_policy_engine_sync, mcp)

    def _live_engine_config(self, gateway_id: str) -> dict:
        """The ``policyEngineConfiguration`` the live gateway reports, or ``{}``; a pure read shared
        by :meth:`_adopt_gateway_engine` and the teardown's ownership check."""
        return (self._control.get_gateway(gatewayIdentifier=gateway_id) or {}).get(
            "policyEngineConfiguration"
        ) or {}

    def _adopt_gateway_engine(self, mcp: McpServer) -> bool:
        """Adopt onto ``mcp``, in memory, the policy engine the live gateway already reports.

        A re-registered record starts with an empty engine id while its gateway keeps the engine
        (record ids change on re-registration, gateways and engines do not); creating a second
        engine would orphan the first and every policy in it. A pure ``get_gateway`` read, so the
        inbound authorizer is never disturbed. The engine id is the ARN tail, name drift accepted.

        The reported mode is adopted with the ARN, which is security-relevant: left at ``none``,
        ``add_policy`` would re-attach ``ENFORCE`` over a gateway deliberately in ``LOG_ONLY``, and
        the read path would show "open" for a gateway that is default-denying. A mode outside
        ``_MODE_FROM_AWS`` is never guessed at: the record's own ``log_only`` / ``enforce`` is kept
        with a warning, and a record with no trustworthy mode raises ``unrecognised_engine_mode``
        rather than let the caller attach ``ENFORCE`` off a value we cannot interpret. Returns
        whether something was adopted.
        """
        if not mcp.gateway_id:
            return False
        existing = self._live_engine_config(mcp.gateway_id)
        existing_arn = existing.get("arn")
        if not existing_arn:
            return False
        mcp.cedar_policy_engine_arn = existing_arn
        mcp.cedar_policy_engine_id = existing_arn.rsplit("/", 1)[-1]
        reported_mode = str(existing.get("mode") or "")
        adopted_mode = _MODE_FROM_AWS.get(reported_mode)
        if adopted_mode is not None:
            mcp.cedar_enforcement_mode = adopted_mode
        elif mcp.cedar_enforcement_mode in _AWS_MODE:
            logger.warning(
                "[mcp_cedar] gateway %s reports policy-engine mode %r, which we do not "
                "recognise; keeping the recorded mode %r",
                mcp.gateway_id,
                reported_mode,
                mcp.cedar_enforcement_mode,
            )
        else:
            raise McpCedarError(
                f"gateway {mcp.gateway_id} reports unrecognised policy-engine mode {reported_mode!r}",
                "unrecognised_engine_mode",
            )
        return True

    def _ensure_engine(self, mcp: McpServer) -> str:
        """Ensure the record has a policy engine and return its id.

        Idempotent on two levels: the record's ``cedar_policy_engine_id`` first, then whatever the
        live gateway already reports (:meth:`_adopt_gateway_engine`), and only then
        ``create_policy_engine`` plus the ``policy_engine_active`` waiter. A successful adoption is
        persisted here rather than inside the probe, because the probe is shared with the
        VIEWER-gated ``list_policies`` while this method is reached only from the OPERATOR-gated
        mutating callers; without the persist the adoption would die with the request and the next
        request's freshly hydrated record would still carry the empty id.
        """
        if mcp.cedar_policy_engine_id:
            return mcp.cedar_policy_engine_id

        if self._adopt_gateway_engine(mcp) and mcp.cedar_policy_engine_id:
            self._registry.persist_identity(mcp)
            return mcp.cedar_policy_engine_id

        resp = self._control.create_policy_engine(
            name=self._engine_name(mcp),
            description=f"Cedar policy engine for gateway {mcp.name}",
            clientToken=str(uuid4()),
        )
        engine_id = resp["policyEngineId"]
        self._control.get_waiter("policy_engine_active").wait(policyEngineId=engine_id)
        mcp.cedar_policy_engine_id = engine_id
        mcp.cedar_policy_engine_arn = resp["policyEngineArn"]
        self._registry.persist_identity(mcp)
        return engine_id

    def _engine_name(self, mcp: McpServer) -> str:
        """A Policy Engine name matching ``^[A-Za-z][A-Za-z0-9_]*$`` and at most 63 characters:
        the whole prefix-plus-id concatenation sanitised, with a leading alpha guaranteed."""
        name = re.sub(r"[^A-Za-z0-9_]", "_", f"{self._engine_name_prefix}{mcp.id}")
        if not name or not name[0].isalpha():
            name = "e" + name
        return name[:63]

    def _create_policy(
        self, engine_id: str, statement: str, *, validation_mode: Optional[str] = None
    ) -> dict:
        """``create_policy`` and wait ``policy_active``. The policy name matches
        ``^[A-Za-z][A-Za-z0-9_]*$``; ``validationMode`` is sent only when given; the waiter raises
        ``WaiterError`` on ``CREATE_FAILED``."""
        kwargs: dict = {}
        if validation_mode:
            kwargs["validationMode"] = validation_mode
        resp = self._control.create_policy(
            policyEngineId=engine_id,
            name="agppol" + uuid4().hex[:16],
            definition={"cedar": {"statement": statement}},
            **kwargs,
        )
        policy_id = resp["policyId"]
        self._control.get_waiter("policy_active").wait(policyEngineId=engine_id, policyId=policy_id)
        return resp

    def _list_policies_sync(self, engine_id: str) -> list[dict]:
        """``list_policy_summaries`` (paginated, bounded) then ``get_policy`` per summary, because
        the summaries carry no Cedar text; a foreign or headerless policy yields a row with null
        user fields, ``managed=False`` and the effect sniffed from the keyword."""
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
                detail = self._control.get_policy(policyEngineId=engine_id, policyId=policy_id)
                statement = ((detail.get("definition") or {}).get("cedar") or {}).get("statement") or ""
                parsed = parse_cedar_policy(statement)
                if parsed is None:
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
                            "kind": None,
                            "agent_app_id": None,
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
                            "kind": parsed.get("kind"),
                            "agent_app_id": parsed.get("agent_app_id"),
                        }
                    )
            next_token = page.get("nextToken")
            if not next_token:
                break
        return rows

    def _delete_policy_sync(self, engine_id: str, policy_id: str) -> None:
        """``delete_policy`` and wait ``policy_deleted``. A ``ResourceNotFoundException`` is
        ``policy_not_found``; any other ``ClientError`` is ``policy_delete_failed``, which renders the
        same 404 the route has always given and differs only in its code."""
        try:
            self._control.delete_policy(policyEngineId=engine_id, policyId=policy_id)
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            logger.warning(
                "[mcp_cedar] delete_policy failed for policy %s on engine %s: %s", policy_id, engine_id, code
            )
            if code == "ResourceNotFoundException":
                raise McpCedarError("policy not found", "policy_not_found") from err
            raise McpCedarError("policy delete failed", "policy_delete_failed") from err
        self._control.get_waiter("policy_deleted").wait(policyEngineId=engine_id, policyId=policy_id)

    def _delete_policy_engine_sync(self, mcp: McpServer) -> Optional[str]:
        """The sync body of :meth:`delete_policy_engine`; see that docstring for the ownership
        rule and every idempotency direction. Deleting the engine cascades its policies."""
        engine_id = mcp.cedar_policy_engine_id
        if not engine_id:
            if not mcp.gateway_id:
                logger.info(
                    "[mcp_cedar] no policy engine on record for MCP %s — nothing to tear down", mcp.id
                )
                return None
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
                logger.info("[mcp_cedar] policy engine %s already gone — treating as success", engine_id)
                return None
            logger.warning("[mcp_cedar] delete_policy_engine failed for engine %s: %s", engine_id, code)
            raise McpCedarError("policy engine delete failed", "engine_delete_failed") from err
        self._control.get_waiter("policy_engine_deleted").wait(policyEngineId=engine_id)
        logger.info("[mcp_cedar] deleted policy engine %s", engine_id)
        return None

    def _attach_engine(self, mcp: McpServer, mode: CedarEnforcementMode) -> None:
        """Associate the engine to the gateway: GET -> replay -> ``update_gateway`` with
        ``policyEngineConfiguration`` -> poll READY, replaying the inbound authorizer."""
        gateway_id = _required_gateway_id(mcp)
        kwargs = self._replay_kwargs(gateway_id)
        kwargs["policyEngineConfiguration"] = {
            "arn": mcp.cedar_policy_engine_arn,
            "mode": _AWS_MODE[mode],
        }
        self._update_gateway_with_conflict_retry(kwargs)
        self._poll_gateway_ready(gateway_id)

    def _detach_engine(self, mcp: McpServer) -> None:
        """Detach the engine: the same GET -> replay -> ``update_gateway`` -> poll, omitting
        ``policyEngineConfiguration`` (the full-replace PUT drops it), still replaying the inbound
        authorizer."""
        gateway_id = _required_gateway_id(mcp)
        kwargs = self._replay_kwargs(gateway_id)
        self._update_gateway_with_conflict_retry(kwargs)
        self._poll_gateway_ready(gateway_id)

    def _replay_kwargs(self, gateway_id: str) -> dict:
        """The full-replace ``update_gateway`` kwargs from a fresh ``get_gateway``: the required
        fields, the inbound authorizer replayed verbatim (the load-bearing security property) and
        the optional fields present."""
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
        """``update_gateway`` with a bounded ``ConflictException`` retry: the gateway is briefly
        UPDATING after any prior update. Any other ``ClientError`` raises immediately; a persistent
        conflict is ``gateway_update_failed``."""
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
                logger.info("[mcp_cedar] update_gateway conflict (gateway busy); retrying")
                time.sleep(_CONFLICT_INTERVAL_SECONDS)
        raise McpCedarError(
            "gateway update conflicted (still updating)", "gateway_update_failed"
        ) from last_err

    def _poll_gateway_ready(self, gateway_id: str) -> None:
        """Poll ``get_gateway`` until READY; a failed status or an exhausted budget is
        ``gateway_update_failed``."""
        for _ in range(_POLL_MAX_ATTEMPTS):
            resp = self._control.get_gateway(gatewayIdentifier=gateway_id)
            status = resp.get("status")
            if status == _READY_STATUS:
                return
            if status in _FAILED_STATUSES:
                raise McpCedarError(
                    f"gateway {gateway_id} reached terminal status {status}", "gateway_update_failed"
                )
            time.sleep(_POLL_INTERVAL_SECONDS)
        raise McpCedarError(
            f"gateway {gateway_id} did not reach READY within {_POLL_MAX_ATTEMPTS} polls",
            "gateway_update_failed",
        )
