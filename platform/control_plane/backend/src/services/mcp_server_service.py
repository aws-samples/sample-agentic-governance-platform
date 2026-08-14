"""MCP Server Registry service — AWS Agent Registry-backed CRUD + lifecycle (Epic 5, Task 3).

Structural clone of ``services.agent_registry_service`` (research §0, §1). Wraps one
boto3 client:
  - ``agent-registry-control`` (``self._ctl``) — CRUD + lifecycle (registry records)

E32: the Registry moved off the ``bedrock-agentcore`` namespace onto its own
``agent-registry`` one (AWS, 2026-08-06; the old namespace shuts down 2026-09-17 and
brand-new accounts cannot use it at all). **Only the Registry moved.** The MCP Gateway and
the Cedar policy engines are NOT registry objects: ``mcp_identity_service`` and
``mcp_cedar_service`` (like the agent-side ``agent_identity_service`` /
``agent_credential_service``) all stay on ``bedrock-agentcore``. The move also dropped the
reserved data-plane client this service used to hold but never called: the new namespace's
data plane is a different API surface (``SearchDiscoverableRegistryRecords``), so it will be
wired deliberately if/when discovery is actually needed, not carried as dead state.

An MCP server is an ``MCP``-type record in the dedicated ``agp-mcp-servers``
registry. Unlike E4's CUSTOM record, the payload is **schema-validated**:
``descriptors.mcpServer.data`` is a stringified ``server.json`` and the optional
``descriptors.mcpServer.additionalData.tools.data`` is a stringified ``{"tools": [...]}``
(research §2). E32 renamed all three legs of that payload — the union arm ``mcp`` became
``mcpServer``, its ``server`` leaf collapsed INTO the arm itself, and ``tools`` moved one
level down under ``additionalData`` — and consolidated ``schemaVersion``/
``protocolVersion``/``inlineContent`` into ``dataSchemaVersion``/``data``. Governance
rides inside ``server.json _meta["com.agp/governance"]`` — built/parsed via
``McpServer.to_server_json()`` / ``McpServer.from_record()``. ``lifecycle_state`` is derived
from the native ``status`` (research §5) and never stored in the envelope.

Two deltas vs E4:
  - a schema ``ValidationException`` on create/update → ``McpValidationError`` (→ HTTP 422);
  - a freshly-created record returns ``CREATING`` and must be polled to ``DRAFT`` (research §6.2)
    before it is exposed/mutable.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import boto3
from botocore.exceptions import ClientError
from pydantic import ValidationError

from core.registry_resolver import RegistryNotConfiguredError, resolve_registry_id
from models.marketplace import MarketplacePublication
from models.mcp_server import (
    TRANSITION_TO_STATUS,
    Kind,
    LifecycleState,
    McpServer,
    McpServerCreate,
    McpServerUpdate,
    lifecycle_for_status,
)

logger = logging.getLogger(__name__)

# Registry record constants (single-source so a future bump is one edit).
_RECORD_TYPE_MCP = "MCP"  # MCP servers are MCP records (E32: recordType, not descriptorType)
_RECORD_VERSION = "1.0.0"
_SERVER_SCHEMA_VERSION = "2025-12-11"  # research §2 — pin for determinism
# E32: renamed from _TOOLS_PROTOCOL_VERSION (same value) — the tools descriptor's
# ``protocolVersion`` was consolidated into the generic ``dataSchemaVersion`` member that
# every descriptor now carries.
_TOOLS_SCHEMA_VERSION = "2025-11-25"  # research §2

# Native registry status used as a server-side ListRegistryRecords filter for a
# given LifecycleState, ONLY where the mapping is 1:1 (PROPOSED also covers the
# transient CREATING/*_FAILED states, so we omit a server filter for it and
# filter client-side instead). Identical to E4.
_LIFECYCLE_TO_STATUS: dict[LifecycleState, str] = {
    LifecycleState.PENDING_APPROVAL: "PENDING_APPROVAL",
    LifecycleState.APPROVED: "APPROVED",
    LifecycleState.REJECTED: "REJECTED",
    LifecycleState.DEPRECATED: "DEPRECATED",
}


class NameTakenError(Exception):
    """Raised when a registry record with the same name already exists.

    Racy pre-check (research §7) — the registry has no conditional-put on
    CreateRegistryRecord. Acceptable for the prototype. → route 409.
    """


class McpServerNotFoundError(Exception):
    """Raised when a write targets an MCP server id that no longer resolves to a record.

    The CRUD methods here return ``Optional[McpServer]`` and let the route map a ``None`` to
    a 404, which is right for a caller-supplied id. ``persist_marketplace`` raises instead,
    because its caller is a SERVICE holding a decision it is about to persist: a silent
    ``None`` there would let an approve mark a publish request APPROVED while the registry
    write never landed. An exception forces that path to fail loudly and keep the request
    retryable. (Mirror of ``agent_registry_service.AgentNotFoundError``.)
    """


class MalformedMcpRecordError(ValueError):
    """Raised when an MCP record lacks a well-formed ``descriptors.mcpServer`` payload.

    The ``data`` blob could be missing or non-JSON, or a well-formed payload could hold a
    structurally invalid governance SUB-MODEL (the E33 ``marketplace`` block). ``get()``
    surfaces this loudly; ``list()`` skips-and-warns so one bad record can't abort the whole
    fan-out.
    """


class IllegalTransitionError(ValueError):
    """Raised when a requested lifecycle transition is an illegal status edge.

    The registry enforces legal status edges (research §5): e.g. ``DRAFT -> APPROVED``
    is rejected with a ``ValidationException`` on ``UpdateRegistryRecordStatus``.
    ``transition()`` maps that to this domain error so the route returns a clean 409.
    Subclasses ``ValueError``.
    """


class McpValidationError(ValueError):
    """Raised when the registry rejects the MCP payload at create/update time (research §2).

    The registry validates ``descriptors.mcpServer`` (and its ``additionalData.tools``)
    against the official MCP
    schemas; a non-compliant payload fails with a ``ValidationException`` ("Schema
    validation failed: …"). Distinguished from ``IllegalTransitionError`` by CALL SITE:
    a schema ``ValidationException`` happens on ``create_registry_record`` /
    ``update_registry_record``; a transition one happens on
    ``update_registry_record_status``. → route 422.
    """


def _is_not_found(err: ClientError) -> bool:
    return err.response.get("Error", {}).get("Code") == "ResourceNotFoundException"


def _is_validation(err: ClientError) -> bool:
    return err.response.get("Error", {}).get("Code") == "ValidationException"


def _wrap_update_description(text: str) -> dict:
    """Wrap a record ``description`` in the UpdateRegistryRecord PATCH envelope.

    ``UpdateRegistryRecord`` is a PATCH-style API: its mutable fields are wrapped in
    an ``{"optionalValue": ...}`` envelope, unlike the FLAT ``CreateRegistryRecord``
    shape. On Create, ``description`` is a plain string; on Update it is
    ``{"optionalValue": <str>}``. (Confirmed against the agent-registry-control
    botocore model — see test_registry_update_param_shape.)
    """
    return {"optionalValue": text}


def _wrap_update_display_name(text: str) -> dict:
    """Wrap a record ``displayName`` in the UpdateRegistryRecord PATCH envelope.

    Must be sent on EVERY update that touches the name, because ``McpServer.from_record``
    PREFERS ``displayName`` over ``name``: patching ``name`` alone leaves the registry
    holding the old ``displayName``, so a rename returns 200 with the new name and then
    silently reverts on the very next read, in both the list and detail views. (The agent
    side hit exactly this in Task 2; both MCP write paths had the same hole.)

    Exactly ONE level of wrapping — ``UpdateRegistryRecord.displayName`` is an
    ``UpdatedDisplayName`` structure whose only member is ``optionalValue``, the same shape
    as ``description`` and NOT the three levels ``descriptors`` needs. (Confirmed against the
    agent-registry-control botocore model — see test_registry_update_param_shape.) Note the
    sibling ``name`` on the same call stays a PLAIN string: it is shape
    ``RegistryRecordName``, unwrapped.
    """
    return {"optionalValue": text}


def _wrap_update_descriptors(flat: dict) -> dict:
    """Re-wrap a flat create-style MCP descriptors dict into the Update PATCH envelope.

    ``flat`` is the ``{"mcpServer": {"data": ..., "dataSchemaVersion": ...,
    "additionalData"?: {"tools": {...}}}}`` dict the create path builds (and passes flat —
    correct for ``CreateRegistryRecord``). The ``additionalData``/``tools`` branch is
    preserved only when present (it is optional: server-only records are legal).

    E32 note, because this is the easy thing to get wrong: the PATCH-envelope *idea* survived
    the namespace move, but the DEPTH did not. Every leaf under an ``Updated*Fields``
    structure is itself an ``Updated*`` wrapper structure, never a bare scalar — so each
    ``data``/``dataSchemaVersion`` needs its own ``optionalValue`` on top of the one its
    parent arm already gets. Verified leaf-by-leaf against the released botocore model:

      * ``UpdatedDescriptors``  -> ``optionalValue`` -> ``UpdatedDescriptorsFields``
      * ``.mcpServer``          -> ``optionalValue`` -> ``UpdatedMcpServerDescriptorFields``
      * ``.data``               -> ``UpdatedDescriptorData{optionalValue: <str>}``
      * ``.dataSchemaVersion``  -> ``UpdatedDataSchemaVersion{optionalValue: <str>}``
      * ``.additionalData``     -> ``optionalValue`` -> ``UpdatedMcpServerAdditionalDataFields``
      * ``.tools``              -> ``optionalValue`` -> ``UpdatedMcpToolsDescriptorFields``
        (whose own ``data``/``dataSchemaVersion`` are then wrapped again, as above)

    So the server blob sits THREE ``optionalValue``s deep and the tools blob FIVE. This is
    WIDER than the agent side's CUSTOM descriptor (one ``data`` leaf): every field patched
    here needs its own leaf wrapper. A bare scalar at any leaf fails param validation with
    "valid types: dict". (See test_registry_update_param_shape, which validates these exact
    kwargs against the real model.)
    """
    server = flat["mcpServer"]
    inner: dict = {
        "data": {"optionalValue": server["data"]},
        "dataSchemaVersion": {"optionalValue": server["dataSchemaVersion"]},
    }
    tools = (server.get("additionalData") or {}).get("tools")
    if tools:
        inner["additionalData"] = {
            "optionalValue": {
                "tools": {
                    "optionalValue": {
                        "data": {"optionalValue": tools["data"]},
                        "dataSchemaVersion": {
                            "optionalValue": tools["dataSchemaVersion"]
                        },
                    }
                }
            }
        }
    return {"optionalValue": {"mcpServer": {"optionalValue": inner}}}


class McpServerRegistryService:
    """CRUD + lifecycle over a single AWS Agent Registry (MCP records)."""

    def __init__(
        self,
        registry_id: str = "",
        region: str = "us-east-1",
        control_client=None,
        registry_name: str = "",
    ):
        """Accept the registry NAME, the registry ID, or both.

        Same change, same reasoning as ``AgentRegistryService.__init__`` — see that docstring
        and ``core.registry_resolver`` for why the id is resolved from the name at first use
        (AWS mints the id, so Terraform could only publish it through a capture file read
        during the PLAN walk, which is what forced two applies from zero). ``registry_id``
        remains an explicit override that short-circuits resolution, because
        ``MCP_REGISTRY_ID`` and several scripts pass ids directly.
        """
        # Held privately: `registry_id` is now a lazily-resolving property (and this field
        # doubles as its cache). Every read inside this class still goes through the property.
        self._registry_id = registry_id or ""
        self.registry_name = registry_name
        self.region = region
        # Injectable for tests (research §10 — MagicMock, not moto).
        self._ctl = control_client or boto3.client(
            "agent-registry-control", region_name=region
        )

    @property
    def registry_id(self) -> str:
        """The registryId, resolved from ``registry_name`` on FIRST USE and memoised.

        Mirrors ``AgentRegistryService.registry_id`` exactly, including the two properties
        that matter most: resolution never happens at import time or inside ``Settings()``,
        and a FAILED lookup is never cached — only a success writes ``_registry_id``, so a
        transient AWS error does not wedge this singleton for the process lifetime.
        """
        if self._registry_id:
            return self._registry_id
        if not self.registry_name:
            raise RegistryNotConfiguredError(
                "McpServerRegistryService was constructed with neither a registry id nor a "
                "registry name, so it cannot address a registry. Set MCP_REGISTRY_NAME (the "
                "normal path — the id is then resolved by name) or MCP_REGISTRY_ID (an "
                "explicit override)."
            )
        self._registry_id = resolve_registry_id(
            self.registry_name, self.region, ctl=self._ctl
        )
        logger.info(
            "resolved MCP registry %r -> %s (region %s)",
            self.registry_name,
            self._registry_id,
            self.region,
        )
        return self._registry_id

    # --- helpers -----------------------------------------------------------

    @staticmethod
    def _record_id_from_arn(record_arn: str) -> str:
        """Parse the recordId from a recordArn. ARN tail is ``.../record/{recordId}``."""
        return record_arn.rsplit("/", 1)[-1]

    def _name_exists(self, name: str) -> bool:
        """Racy name pre-check via the structured ``name`` filter (research §7).

        E32: ListRegistryRecords is now POST-shaped — the discrete ``name`` /
        ``descriptorType`` / ``status`` query params were replaced by one structured
        ``filters=[{"name": ..., "values": [...]}]`` list (filterable names are exactly
        ``name`` / ``status`` / ``recordType``).
        """
        resp = self._ctl.list_registry_records(
            registryId=self.registry_id,
            filters=[
                {"name": "name", "values": [name]},
                {"name": "recordType", "values": [_RECORD_TYPE_MCP]},
            ],
        )
        return bool(resp.get("registryRecords"))

    def _iter_record_summaries(self, *, status: Optional[str] = None):
        """Paginate ListRegistryRecords (research §6). List items omit descriptors.

        E32: filtering moved from discrete query params to one structured ``filters``
        list (see ``_name_exists``).
        """
        kwargs = {
            "registryId": self.registry_id,
            "maxResults": 100,
            "filters": [{"name": "recordType", "values": [_RECORD_TYPE_MCP]}],
        }
        if status:
            kwargs["filters"] = kwargs["filters"] + [{"name": "status", "values": [status]}]
        while True:
            page = self._ctl.list_registry_records(**kwargs)
            for summary in page.get("registryRecords", []):
                yield summary
            token = page.get("nextToken")
            if not token:
                break
            kwargs["nextToken"] = token

    def _hydrate(self, record_id: str) -> Optional[McpServer]:
        """GetRegistryRecord + parse the MCP payload into an McpServer. None if not found.

        Raises MalformedMcpRecordError if the record exists but its
        ``descriptors.mcpServer.data`` is missing/non-JSON, or holds a structurally invalid
        governance sub-model.
        """
        try:
            resp = self._ctl.get_registry_record(
                registryId=self.registry_id, recordId=record_id
            )
        except ClientError as err:
            if _is_not_found(err):
                return None
            raise
        try:
            # E32: the `mcp` union arm became `mcpServer`, its `server` leaf collapsed into
            # the arm itself (`data`), and `tools` moved down under `additionalData`.
            server_branch = resp["descriptors"]["mcpServer"]
            server_json = json.loads(server_branch["data"])
            tools_node = (server_branch.get("additionalData") or {}).get("tools")
            if tools_node:
                tools = json.loads(tools_node["data"]).get("tools", [])
            else:
                tools = []
        except (KeyError, TypeError, json.JSONDecodeError) as err:
            raise MalformedMcpRecordError(
                f"Registry record {record_id!r} has no well-formed MCP server "
                f"payload: {err}"
            ) from err
        # A well-formed payload can still hold a structurally invalid SUB-MODEL: since E33
        # ``from_record`` parses the ``marketplace`` block via
        # ``MarketplacePublication.model_validate``, which raises pydantic's
        # ``ValidationError``. That is the same class of fault as a non-JSON payload, so it
        # must reach callers as ``MalformedMcpRecordError`` — otherwise ``list()`` (which
        # catches only that type) lets it escape and ONE bad record 500s the whole catalog,
        # defeating the skip-and-warn contract. Translated HERE, at ``from_record``'s caller,
        # rather than inside ``models.mcp_server``: ``MalformedMcpRecordError`` is defined in
        # this module and this module imports ``models.mcp_server``, so raising it from the
        # parse site would need the reverse import edge — an import cycle. (The agent side
        # fixed the identical hole at ``agent_registry_service._hydrate``.)
        try:
            return McpServer.from_record(resp, server_json, tools)
        except ValidationError as err:
            raise MalformedMcpRecordError(
                f"Registry record {record_id!r} has a structurally invalid governance "
                f"envelope: {err}"
            ) from err

    def _update_registry_record_with_retry(
        self,
        *,
        recordId: str,
        name: str,
        displayName,
        description,
        descriptors,
        recordVersion: str,
        attempts: int = 12,
        delay: float = 2.0,
    ) -> None:
        """UpdateRegistryRecord, retrying on ConflictException (research §6.2 / the E6
        live seam).

        A record is briefly un-modifiable (native ``UPDATING`` state) immediately after a
        prior write / fresh create: ``UpdateRegistryRecord`` then fails with
        ``ConflictException`` ("Registry record cannot be modified while in UPDATING
        state."). The transition is fast ("a few seconds"), so a bounded retry resolves
        it. Mirrors the ``_poll_record_to_draft`` ConflictException idiom: re-raise any
        NON-Conflict error IMMEDIATELY (never mask a real failure), sleep between attempts,
        and surface the last ConflictException if the budget is exhausted.

        ``displayName`` / ``description`` / ``descriptors`` are passed through verbatim — the
        caller has already built the PATCH ``optionalValue`` envelope (see ``_wrap_update_*``);
        only ``name`` is a plain string. ``displayName`` is a REQUIRED keyword rather than an
        optional one precisely so neither write path can forget it: reads prefer
        ``displayName``, so a name-only patch silently reverts (E32 — see
        ``_wrap_update_display_name``).
        ~12 attempts × 2.0s ≈ 24s of headroom (generous for the fast UPDATING transition).
        """
        last_err: Optional[ClientError] = None
        for attempt in range(attempts):
            try:
                self._ctl.update_registry_record(
                    registryId=self.registry_id,
                    recordId=recordId,
                    name=name,
                    displayName=displayName,
                    description=description,
                    descriptors=descriptors,
                    recordVersion=recordVersion,
                )
                return
            except ClientError as err:
                # Only ConflictException (the UPDATING-state race) is retried; anything
                # else (ValidationException, ResourceNotFoundException, throttling, …) must
                # propagate immediately so the caller's existing mapping/handling applies.
                if err.response.get("Error", {}).get("Code") != "ConflictException":
                    raise
                last_err = err
                # Sleep BETWEEN attempts only — skip on the final attempt so we don't
                # waste ~2s before surfacing the failure.
                if attempt < attempts - 1:
                    time.sleep(delay)
        # Exhausted. Raise the last ConflictException if we got one; fall back to a
        # RuntimeError for the degenerate attempts<=0 case (last_err would be None).
        if last_err is not None:
            raise last_err
        raise RuntimeError(
            f"UpdateRegistryRecord for {recordId!r} did not complete after {attempts} attempts"
        )

    def _poll_record_to_draft(
        self, record_id: str, attempts: int = 10, delay: float = 0.5
    ) -> str:
        """Poll GetRegistryRecord until the record reaches DRAFT (research §6.2).

        Returns the final observed native status string (``"DRAFT"``) so the caller can
        derive the lifecycle from the freshly-observed status rather than the stale
        create response (which is ``"CREATING"`` in the real world).

        A freshly-created record returns ``CREATING`` and is briefly un-modifiable; a
        ``ConflictException`` while ``CREATING`` is expected — keep polling. Bounded loop;
        raises ``RuntimeError`` on timeout. In tests the mock returns DRAFT immediately so
        it exits on the first poll.
        """
        for _ in range(attempts):
            try:
                resp = self._ctl.get_registry_record(
                    registryId=self.registry_id, recordId=record_id
                )
                if resp.get("status") == "DRAFT":
                    return "DRAFT"
            except ClientError as err:
                # ConflictException during CREATING is expected — keep polling.
                if err.response.get("Error", {}).get("Code") != "ConflictException":
                    raise
            time.sleep(delay)
        raise RuntimeError(
            f"Registry record {record_id!r} did not reach DRAFT after {attempts} polls"
        )

    # --- CRUD --------------------------------------------------------------

    def create(self, req: McpServerCreate, created_by: Optional[str] = None) -> McpServer:
        # Racy name pre-check (research §7) -> NameTakenError -> HTTP 409.
        if self._name_exists(req.name):
            raise NameTakenError(f"MCP server name '{req.name}' is already in use")

        now = datetime.now(timezone.utc)
        # The authoritative id is the recordId, only known after create. Build the
        # McpServer with a placeholder id so to_server_json() can run, then overwrite
        # id + lifecycle from the create response (mirror E4's ordering — the envelope's
        # mcp_server_id mirror is for debug only; the native recordId is authoritative).
        mcp = McpServer(
            **req.model_dump(exclude_none=False),
            id="",
            lifecycle_state=LifecycleState.PROPOSED,
            created_at=now,
            updated_at=now,
            created_by=created_by,
        )

        server_json = mcp.to_server_json()
        tools_json = {"tools": mcp.tools_as_mcp()}

        # Omit the tools branch entirely when there are no declared tools (research §2:
        # tools is optional; server-only records are legal).
        descriptors: dict = {
            "mcpServer": {
                "data": json.dumps(server_json),
                "dataSchemaVersion": _SERVER_SCHEMA_VERSION,
            }
        }
        if mcp.available_tools:
            descriptors["mcpServer"]["additionalData"] = {
                "tools": {
                    "data": json.dumps(tools_json),
                    "dataSchemaVersion": _TOOLS_SCHEMA_VERSION,
                }
            }

        try:
            resp = self._ctl.create_registry_record(
                registryId=self.registry_id,
                name=req.name,
                # E32: displayName is the new human-facing label; `name` stays the unique
                # identifier. We have exactly one name to offer, so both carry it — a distinct
                # display label would need a new McpServerCreate field and a UI to set it.
                displayName=req.name,
                # CreateRegistryRecord requires description min length 1 — fall back to the
                # (always-present) name so a description-less create can't send "" and trip a
                # ParamValidationError -> raw 500. (McpServerCreate.description defaults to "";
                # the agent side already guards this the same way.)
                description=req.description or req.name,
                # E32: recordType replaces the removed descriptorType, and is REQUIRED on create.
                recordType=_RECORD_TYPE_MCP,
                descriptors=descriptors,
                recordVersion=_RECORD_VERSION,
                # Stable idempotency token for retried creates (≥33 chars).
                clientToken=str(uuid.uuid5(uuid.NAMESPACE_DNS, f"agp-mcp:{req.name}")),
            )
        except ClientError as err:
            # Schema-validation rejection (research §2) -> McpValidationError -> 422.
            if _is_validation(err):
                message = err.response.get("Error", {}).get("Message", "") or str(err)
                raise McpValidationError(message) from err
            raise

        mcp.id = self._record_id_from_arn(resp["recordArn"])
        # A fresh record returns CREATING; poll it to DRAFT before exposing it (research §6.2).
        # NOTE: create is NOT transactional across the poll — if _poll_record_to_draft raises
        # (timeout / non-Conflict ClientError) the record already exists and is left as an
        # orphaned CREATING record. Accepted prototype trade-off (same philosophy as the racy
        # name pre-check). Derive lifecycle from the freshly-observed status, not the stale
        # create response (which is "CREATING" in the real world).
        final_status = self._poll_record_to_draft(mcp.id)
        mcp.lifecycle_state = lifecycle_for_status(final_status)
        return mcp

    def get(self, mcp_server_id: str) -> Optional[McpServer]:
        return self._hydrate(mcp_server_id)

    def list(
        self,
        *,
        lifecycle_state: Optional[LifecycleState] = None,
        kind: Optional[Kind] = None,
        owner_oid: Optional[str] = None,
        business_unit: Optional[str] = None,
        region: Optional[str] = None,
    ) -> List[McpServer]:
        # Server-side status filter only where the lifecycle->status mapping is 1:1
        # (PROPOSED maps from several native states, so it is filtered client-side).
        status_filter = _LIFECYCLE_TO_STATUS.get(lifecycle_state) if lifecycle_state else None

        servers: List[McpServer] = []
        # N+1 fan-out: list summaries omit descriptors, so GetRegistryRecord per id
        # to read the server.json + governance (research §6).
        for summary in self._iter_record_summaries(status=status_filter):
            try:
                hydrated = self._hydrate(summary["recordId"])
            except MalformedMcpRecordError as err:
                logger.warning("Skipping malformed MCP registry record: %s", err)
                continue
            if hydrated is not None:
                servers.append(hydrated)

        def _keep(m: McpServer) -> bool:
            if lifecycle_state is not None and m.lifecycle_state != lifecycle_state:
                return False
            if kind is not None and m.kind != kind:
                return False
            if owner_oid is not None and m.owner_oid != owner_oid:
                return False
            if business_unit is not None and m.business_unit != business_unit:
                return False
            if region is not None and m.region != region:
                return False
            return True

        result = [m for m in servers if _keep(m)]
        result.sort(key=lambda m: m.updated_at, reverse=True)
        return result

    def update(self, mcp_server_id: str, req: McpServerUpdate) -> Optional[McpServer]:
        existing = self.get(mcp_server_id)
        if existing is None:
            return None

        # Read-modify-write.
        for field, value in req.model_dump(exclude_none=True).items():
            setattr(existing, field, value)
        existing.updated_at = datetime.now(timezone.utc)

        server_json = existing.to_server_json()
        tools_json = {"tools": existing.tools_as_mcp()}
        descriptors: dict = {
            "mcpServer": {
                "data": json.dumps(server_json),
                "dataSchemaVersion": _SERVER_SCHEMA_VERSION,
            }
        }
        if existing.available_tools:
            descriptors["mcpServer"]["additionalData"] = {
                "tools": {
                    "data": json.dumps(tools_json),
                    "dataSchemaVersion": _TOOLS_SCHEMA_VERSION,
                }
            }

        try:
            # UpdateRegistryRecord is a PATCH API: description + descriptors are
            # wrapped in the optionalValue envelope. The `descriptors` built above is
            # the flat create-style dict; re-wrap it for update (create passes it flat).
            # Routed through the ConflictException-retry helper (same UPDATING-state race
            # surface as persist_identity). The helper retries ONLY ConflictException and
            # re-raises everything else immediately, so a ValidationException /
            # ResourceNotFoundException still propagates here and the except block below
            # keeps its existing 404→None / 422→McpValidationError mapping.
            self._update_registry_record_with_retry(
                recordId=mcp_server_id,
                name=existing.name,  # plain string — NOT wrapped
                # Both name fields, always: reads prefer displayName, so patching
                # `name` alone makes a rename revert on the next read (E32).
                displayName=_wrap_update_display_name(existing.name),
                # description min length is 1 — fall back to the name, exactly as create does,
                # so clearing the description can't turn into a raw 500.
                description=_wrap_update_description(
                    existing.description or existing.name
                ),
                descriptors=_wrap_update_descriptors(descriptors),
                recordVersion=_RECORD_VERSION,
            )
        except ClientError as err:
            if _is_not_found(err):
                return None
            if _is_validation(err):
                message = err.response.get("Error", {}).get("Message", "") or str(err)
                raise McpValidationError(message) from err
            raise
        return existing

    def persist_identity(self, mcp: McpServer) -> McpServer:
        """Envelope-write an in-hand ``McpServer`` (used to persist E7 identity fields).

        ``McpServerUpdate`` deliberately omits the identity fields (``entra_sp_id`` /
        ``entra_app_audience`` / ``invoker_role_id`` / ``admin_role_id`` / ``gateway_id``
        / ``gateway_url`` / ``identity_status``), so ``update()`` cannot persist them.
        This thin helper mirrors ``update()``'s descriptor rebuild but for an
        already-hydrated ``McpServer``: it bumps ``updated_at`` and writes the SAME
        ``mcpServer`` descriptor via ``UpdateRegistryRecord``.

        CRITIC-I3 (clone trap): this is NOT E6's ``AgentRegistryService.persist_identity``
        — that writes a **CUSTOM** descriptor. MCP records are schema-validated ``MCP``
        records whose descriptor is ``mcpServer`` (research §2); cloning
        the agent CUSTOM body verbatim would schema-422 (or silently drop the identity
        fields). The identity fields ride in the governance envelope inside
        ``server.json`` (``to_server_json`` → ``to_envelope``), so the T-MODEL round-trip
        carries them. Used by the T-IDENTITY provisioning orchestrator. Returns the mcp.
        """
        mcp.updated_at = datetime.now(timezone.utc)

        server_json = mcp.to_server_json()
        tools_json = {"tools": mcp.tools_as_mcp()}
        descriptors: dict = {
            "mcpServer": {
                "data": json.dumps(server_json),
                "dataSchemaVersion": _SERVER_SCHEMA_VERSION,
            }
        }
        if mcp.available_tools:
            descriptors["mcpServer"]["additionalData"] = {
                "tools": {
                    "data": json.dumps(tools_json),
                    "dataSchemaVersion": _TOOLS_SCHEMA_VERSION,
                }
            }

        # UpdateRegistryRecord is a PATCH API: description + descriptors are wrapped in
        # the optionalValue envelope. The `descriptors` built above is the flat
        # create-style dict; re-wrap it for update (mirrors update() exactly).
        # Routed through the ConflictException-retry helper: provision() fires several
        # persist_identity writes in quick succession and a later write can race a prior
        # write's UPDATING transition (the E6 live seam — research §6.2).
        self._update_registry_record_with_retry(
            recordId=mcp.id,
            name=mcp.name,  # plain string — NOT wrapped
            # Both name fields, always: reads prefer displayName, so patching
            # `name` alone makes a rename revert on the next read (E32).
            displayName=_wrap_update_display_name(mcp.name),
            # description min length is 1 — fall back to the name, exactly as create does.
            description=_wrap_update_description(mcp.description or mcp.name),
            descriptors=_wrap_update_descriptors(descriptors),
            recordVersion=_RECORD_VERSION,
        )
        return mcp

    def persist_marketplace(
        self, mcp_server_id: str, publication: Optional[MarketplacePublication]
    ) -> McpServer:
        """Service-only write of the marketplace block (E33 Amendment 1 / C8). ``None`` clears it.

        The marketplace counterpart of ``persist_identity``, and service-only for the same
        reason: ``McpServerUpdate`` deliberately omits ``marketplace`` (a declared datasheet
        is an ATTESTATION — a request body must never be able to forge one), so ``update()``
        cannot persist it. Read-modify-write: hydrate, set the block, and delegate the
        envelope write to ``persist_identity`` so the three-level ``mcpServer`` PATCH
        descriptors wrap (and the five-level tools branch) lives in exactly ONE place — see
        ``_wrap_update_descriptors`` and test_registry_update_param_shape.

        Takes an ``mcp_server_id`` rather than an ``McpServer`` because the caller (the
        marketplace approve/unpublish path) holds only the id from its publish request, and
        re-reading is what makes the block land on the CURRENT record rather than a stale copy.

        Unpublish semantics live with the CALLER: it passes a publication with
        ``published=False`` (retaining the declared history), not ``None``. This method just
        persists what it is given.

        Raises ``McpServerNotFoundError`` when the id no longer resolves — a silent ``None``
        would let an approve report success on a write that never happened.
        """
        mcp = self.get(mcp_server_id)
        if mcp is None:
            raise McpServerNotFoundError(f"MCP server {mcp_server_id!r} not found")
        mcp.marketplace = publication
        return self.persist_identity(mcp)

    def delete(self, mcp_server_id: str) -> Optional[McpServer]:
        """Delete the registry RECORD and return the prior state (None if it was gone).

        Record-only BY DESIGN, and that is not the whole delete: the identity cascade (the
        Cedar policy engine + the Entra app/SP and its consents — E36/T16, research item 5A)
        runs in ``routes/mcp_servers.delete_mcp_server`` BEFORE this call, because it needs
        the ids this delete removes. It lives there rather than here for a hard reason —
        ``McpCedarService`` imports THIS module, so a cascade in this method would be an
        import cycle — and it matches ``projects.py``'s per-item cascade shape.
        """
        prior = self.get(mcp_server_id)
        if prior is None:
            return None
        try:
            self._ctl.delete_registry_record(
                registryId=self.registry_id, recordId=mcp_server_id
            )
        except ClientError as err:
            if _is_not_found(err):
                return None
            raise
        return prior

    # --- lifecycle ---------------------------------------------------------

    def submit_for_approval(self, mcp_server_id: str) -> Optional[McpServer]:
        try:
            self._ctl.submit_registry_record_for_approval(
                registryId=self.registry_id, recordId=mcp_server_id
            )
        except ClientError as err:
            if _is_not_found(err):
                return None
            raise
        return self.get(mcp_server_id)

    def transition(self, mcp_server_id: str, action: str, reason: str) -> Optional[McpServer]:
        if action not in TRANSITION_TO_STATUS:
            raise ValueError(
                f"Unknown transition action {action!r}; "
                f"valid actions: {sorted(TRANSITION_TO_STATUS)}"
            )
        if not reason or not reason.strip():
            # statusReason is REQUIRED by UpdateRegistryRecordStatus (research §6).
            raise ValueError("A non-empty reason is required for a status transition")

        try:
            self._ctl.update_registry_record_status(
                registryId=self.registry_id,
                recordId=mcp_server_id,
                status=TRANSITION_TO_STATUS[action],
                statusReason=reason,
            )
        except ClientError as err:
            # Not-found first (mirror the other control-plane calls).
            if _is_not_found(err):
                return None
            # An illegal status edge surfaces as a ValidationException on the TRANSITION
            # call site -> IllegalTransitionError (409), distinct from the create/update
            # schema ValidationException (research §2, §5).
            error = err.response.get("Error", {})
            message = error.get("Message", "") or ""
            if error.get("Code") == "ValidationException" or "Invalid status transition" in message:
                raise IllegalTransitionError(message or str(err)) from err
            raise
        mcp = self.get(mcp_server_id)
        # E33 Amendment 2 / C12: deprecating a PUBLISHED marketplace product UNLISTS it.
        # Only the DEPRECATED target does this — approve/reject never touch the block, which
        # is what makes a deprecation STICK: a later lifecycle re-approve does not re-list the
        # product, only a fresh publish request can.
        #
        # Ordering is lifecycle write FIRST, unlist SECOND, and an unlist failure PROPAGATES
        # (the route error mapping surfaces it) rather than being swallowed: a silent failure
        # would leave a deprecated record still flagged published with nobody told. That state
        # is already refused by the read path's lifecycle gate (defense in depth), and the
        # retryable path is transition-again or an admin unpublish. Identical to E4's
        # agent-side clause (the two registry services stay structural clones).
        if (
            mcp is not None
            and TRANSITION_TO_STATUS[action] == _LIFECYCLE_TO_STATUS[LifecycleState.DEPRECATED]
            and mcp.marketplace is not None
            and mcp.marketplace.published
        ):
            # published=False (not None) keeps the declared datasheet as history — the C8
            # unpublish semantics, reused verbatim.
            return self.persist_marketplace(
                mcp_server_id, mcp.marketplace.model_copy(update={"published": False})
            )
        return mcp
