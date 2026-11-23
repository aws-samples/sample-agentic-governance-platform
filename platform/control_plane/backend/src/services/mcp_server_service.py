"""MCP server registry: CRUD and lifecycle over one AWS Agent Registry of ``MCP`` records.

An MCP server is an ``MCP``-type record whose ``descriptors.mcpServer.data`` is a stringified
``server.json`` (governance rides inside ``_meta["com.agp/governance"]``) and whose optional
``descriptors.mcpServer.additionalData.tools.data`` is a stringified ``{"tools": [...]}``. Both
legs are schema-validated by the registry, so a non-compliant payload is a ``ValidationException``
on create or update, surfaced here as :class:`McpValidationError`. ``lifecycle_state`` derives
from the native ``status`` and is never stored in the envelope. A freshly created record is
``CREATING`` and is polled to ``DRAFT`` before it is exposed. Every registry call goes through
``services.registry_writer.RegistryWriter``.

The CRUD methods return ``None`` for an id that no longer resolves; the service-only writes
(:meth:`McpServerRegistryService.persist_marketplace`) raise instead, because their caller holds
a decision it is about to record. The gateway and the Cedar policy engines are not registry
objects; ``mcp_identity_service`` and ``mcp_cedar_service`` own them on ``bedrock-agentcore``.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import ClassVar, List, Optional

from botocore.exceptions import ClientError
from pydantic import ValidationError

from core.errors import AppError, Outcome
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
from services.registry_writer import RecordContent, RegistryWriter, stable_status

logger = logging.getLogger(__name__)

_RECORD_TYPE_MCP = "MCP"
_SERVER_SCHEMA_VERSION = "2025-12-11"
_TOOLS_SCHEMA_VERSION = "2025-11-25"

_LIFECYCLE_TO_STATUS: dict[LifecycleState, str] = {
    LifecycleState.PENDING_APPROVAL: "PENDING_APPROVAL",
    LifecycleState.APPROVED: "APPROVED",
    LifecycleState.REJECTED: "REJECTED",
    LifecycleState.DEPRECATED: "DEPRECATED",
}


class NameTakenError(AppError):
    """A registry record with the same name already exists.

    The name pre-check is a list-by-name read, so two concurrent creates can both pass it: the
    registry has no conditional put. The message is this module's own sentence naming the taken
    name and is sent to the client verbatim.
    """

    domain: ClassVar[str] = "mcp_server"
    kinds: ClassVar[Mapping[str, Outcome]] = {"name_taken": Outcome(409, None)}


class McpServerNotFoundError(AppError):
    """An MCP server id resolves to no record the caller may see.

    The routes raise it for a missing id and for a foreign tenant's record alike, one body for
    both. :meth:`McpServerRegistryService.persist_marketplace` raises it because its caller holds a
    decision it is about to record: a silent ``None`` would let an approve mark a publish request
    APPROVED while the registry write never landed. The detail is fixed; the message names the id.
    """

    domain: ClassVar[str] = "mcp_server"
    kinds: ClassVar[Mapping[str, Outcome]] = {"not_found": Outcome(404, "MCP server not found")}


class McpServerRequestError(AppError):
    """A refusal an MCP server route decides itself: a non-admin setting ``shared`` on create or
    update, a change the owning repository makes instead, or a verb the record's kind does not
    support. Every kind has a fixed detail; the message names the record or the role for the log."""

    domain: ClassVar[str] = "mcp_server"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "shared_admin_only": Outcome(403, "only ADMIN may set shared"),
        "repo_managed": Outcome(409, "managed by repository"),
        "delete_through_repository": Outcome(409, "delete the repository that owns this MCP server"),
        "rebuild_through_repository": Outcome(409, "rebuild the repository instead"),
        "not_provisionable": Outcome(409, "not an AgentCore gateway or runtime MCP"),
        "tools_gateway_only": Outcome(409, "tool refresh is only supported for gateway MCP servers"),
    }


class MalformedMcpRecordError(AppError, ValueError):
    """An MCP record lacks a well-formed ``descriptors.mcpServer`` payload.

    The ``data`` blob is missing or non-JSON, or a well-formed payload holds a structurally
    invalid governance sub-model (the ``marketplace`` block). ``get()`` surfaces it; ``list()``
    skips the record with a warning so one bad record cannot abort the catalog. The detail is
    fixed; the message carries the record id and the parser's text for the log.
    """

    domain: ClassVar[str] = "mcp_server"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "malformed_record": Outcome(502, "MCP server record is malformed")
    }


class IllegalTransitionError(AppError, ValueError):
    """A requested lifecycle transition is refused.

    ``illegal_edge``: the registry rejected the status edge (``DRAFT -> APPROVED``) with a
    ``ValidationException`` on ``UpdateRegistryRecordStatus``; the message is the registry's
    explanation of the legal edges. ``invalid_request``: the action is not one of
    ``TRANSITION_TO_STATUS`` or the reason is empty; the message is this module's sentence. Both
    messages are sent to the client verbatim: the registry composes the ``illegal_edge`` text from
    its status vocabulary and the request's own values, this module composes the
    ``invalid_request`` text from the action and the valid actions, and a transition request
    carries no credential, so neither message can.
    """

    domain: ClassVar[str] = "mcp_server"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "illegal_edge": Outcome(409, None),
        "invalid_request": Outcome(400, None),
    }


class McpValidationError(AppError, ValueError):
    """The registry rejected the MCP payload at create or update time.

    The registry validates ``descriptors.mcpServer`` and its ``additionalData.tools`` against the
    MCP schemas; a non-compliant payload fails with a ``ValidationException`` whose message
    ("Schema validation failed: ...") describes the caller's own payload and is sent to the client
    verbatim. Distinguished from :class:`IllegalTransitionError` by call site: a schema failure
    happens on ``create_registry_record`` / ``update_registry_record``, a transition failure on
    ``update_registry_record_status``. The ``schema`` message is that registry text, which AWS
    composes from the schema's field names and the offending values of the caller's own payload;
    a record payload holds no credential (the identity fields are platform-written ids and no
    token or secret is ever written to a record), so none can reach the client through it.
    """

    domain: ClassVar[str] = "mcp_server"
    kinds: ClassVar[Mapping[str, Outcome]] = {"schema": Outcome(422, None)}


def _is_not_found(err: ClientError) -> bool:
    return err.response.get("Error", {}).get("Code") == "ResourceNotFoundException"


def _is_validation(err: ClientError) -> bool:
    return err.response.get("Error", {}).get("Code") == "ValidationException"


def _validation_message(err: ClientError) -> str:
    return err.response.get("Error", {}).get("Message", "") or str(err)


def _mcp_descriptors(mcp: McpServer) -> dict:
    """The flat create-style ``mcpServer`` descriptors for ``mcp``; the tools branch only when
    the record declares tools."""
    descriptors: dict = {
        "mcpServer": {
            "data": json.dumps(mcp.to_server_json()),
            "dataSchemaVersion": _SERVER_SCHEMA_VERSION,
        }
    }
    if mcp.available_tools:
        descriptors["mcpServer"]["additionalData"] = {
            "tools": {
                "data": json.dumps({"tools": mcp.tools_as_mcp()}),
                "dataSchemaVersion": _TOOLS_SCHEMA_VERSION,
            }
        }
    return descriptors


def _record_content(mcp: McpServer) -> RecordContent:
    """The record's governed content: ``description`` falls back to the name because the registry
    requires a non-empty one."""
    return RecordContent(
        name=mcp.name,
        description=mcp.description or mcp.name,
        descriptors=_mcp_descriptors(mcp),
    )


class McpServerRegistryService:
    """CRUD + lifecycle over a single AWS Agent Registry (MCP records)."""

    def __init__(
        self,
        registry_id: str = "",
        region: str | None = None,
        control_client=None,
        registry_name: str = "",
    ):
        """Accept the registry name, the registry id, or both.

        The id is resolved from the name at first use (AWS mints the id, so Terraform cannot
        publish it before the first apply); ``registry_id`` is an explicit override that
        short-circuits resolution. ``control_client`` is injectable for tests and reaches the
        writer, which obtains one when it is None.
        """
        self._writer = RegistryWriter(
            region=region,
            registry_id=registry_id,
            registry_name=registry_name,
            kind="mcp",
            control_client=control_client,
        )

    @property
    def registry_id(self) -> str:
        return self._writer.registry_id

    def _hydrate(self, record_id: str) -> Optional[McpServer]:
        """``GetRegistryRecord`` + parse the MCP payload into an :class:`McpServer`; ``None`` when
        not found.

        Raises :class:`MalformedMcpRecordError` when the record exists but its
        ``descriptors.mcpServer.data`` is missing or non-JSON, or when ``McpServer.from_record``
        rejects the governance sub-model (pydantic's ``ValidationError``, translated here because
        this module imports ``models.mcp_server`` and the reverse edge would be a cycle).
        """
        resp = self._writer.get(record_id)
        if resp is None:
            return None
        try:
            server_branch = resp["descriptors"]["mcpServer"]
            server_json = json.loads(server_branch["data"])
            tools_node = (server_branch.get("additionalData") or {}).get("tools")
            tools = json.loads(tools_node["data"]).get("tools", []) if tools_node else []
        except (KeyError, TypeError, json.JSONDecodeError) as err:
            raise MalformedMcpRecordError(
                f"Registry record {record_id!r} has no well-formed MCP server payload: {err}"
            ) from err
        try:
            return McpServer.from_record(resp, server_json, tools)
        except ValidationError as err:
            raise MalformedMcpRecordError(
                f"Registry record {record_id!r} has a structurally invalid governance envelope: {err}"
            ) from err

    def create(
        self,
        req: McpServerCreate,
        created_by: Optional[str] = None,
        *,
        project_id: Optional[str] = None,
        repo_id: Optional[str] = None,
    ) -> McpServer:
        """Create the record; the writer polls it to ``DRAFT``.

        ``project_id`` / ``repo_id`` are server-written ownership: only the repository materialize
        path passes them, marking the record as managed by that repository. The ``McpServer`` is
        built with a placeholder id so ``to_server_json()`` can run; the native recordId from the
        create response is authoritative. ``description`` falls back to the name because the
        registry requires a non-empty one. The create is not transactional across the poll: a
        poll failure leaves an orphaned ``CREATING`` record.
        """
        if next(self._writer.iter_summaries(record_type=_RECORD_TYPE_MCP, name=req.name), None) is not None:
            raise NameTakenError(f"MCP server name '{req.name}' is already in use")

        now = datetime.now(timezone.utc)
        mcp = McpServer(
            **req.model_dump(exclude_none=False),
            id="",
            lifecycle_state=LifecycleState.PROPOSED,
            created_at=now,
            updated_at=now,
            created_by=created_by,
            project_id=project_id,
            repo_id=repo_id,
        )
        try:
            mcp.id, status = self._writer.create(
                _record_content(mcp),
                record_type=_RECORD_TYPE_MCP,
                client_token=str(uuid.uuid5(uuid.NAMESPACE_DNS, f"agp-mcp:{req.name}")),
            )
        except ClientError as err:
            if _is_validation(err):
                raise McpValidationError(_validation_message(err)) from err
            raise
        mcp.lifecycle_state = lifecycle_for_status(status)
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
        """List and hydrate every MCP record, newest first.

        The server-side status filter applies only where the lifecycle maps 1:1 to a native status
        (``PROPOSED`` covers several native states and is filtered client-side). List summaries
        omit descriptors, so each record is read individually; a malformed record is skipped with a
        warning.
        """
        status_filter = _LIFECYCLE_TO_STATUS.get(lifecycle_state) if lifecycle_state else None

        servers: List[McpServer] = []
        for summary in self._writer.iter_summaries(record_type=_RECORD_TYPE_MCP, status=status_filter):
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
            return region is None or m.region == region

        result = [m for m in servers if _keep(m)]
        result.sort(key=lambda m: m.updated_at, reverse=True)
        return result

    def update(self, mcp_server_id: str, req: McpServerUpdate) -> Optional[McpServer]:
        """Read-modify-write the user-editable fields; ``None`` when the record is gone.

        A user content edit leaves the record ``DRAFT`` (the registry resets an ``APPROVED``
        record on any write), so an ``APPROVED`` lifecycle is reported as ``PROPOSED``.
        """
        existing = self.get(mcp_server_id)
        if existing is None:
            return None

        for field, value in req.model_dump(exclude_none=True).items():
            setattr(existing, field, value)
        existing.updated_at = datetime.now(timezone.utc)

        try:
            written = self._writer.write_content(mcp_server_id, _record_content(existing))
        except ClientError as err:
            if _is_validation(err):
                raise McpValidationError(_validation_message(err)) from err
            raise
        if written is None:
            return None
        if existing.lifecycle_state == LifecycleState.APPROVED:
            existing.lifecycle_state = LifecycleState.PROPOSED
        return existing

    def persist_identity(self, mcp: McpServer) -> McpServer:
        """Envelope-write an in-hand :class:`McpServer`, the platform's own write path.

        ``McpServerUpdate`` deliberately omits the identity fields (``entra_sp_id``,
        ``entra_app_audience``, the role ids, ``gateway_id`` / ``gateway_url``,
        ``identity_status``), so ``update()`` cannot persist them; they ride in the governance
        envelope inside ``server.json``. Writes the ``mcpServer`` descriptor (never the agent side's
        ``CUSTOM`` one) through the writer's platform write: no call when the record already holds
        this content, and an ``APPROVED`` record is re-asserted ``APPROVED`` afterwards, because the
        registry resets any write to ``DRAFT``; the returned record carries the lifecycle of the
        status the writer saw.
        """
        outcome = self._writer.write(mcp.id, _record_content(mcp))
        if outcome.written:
            mcp.updated_at = datetime.now(timezone.utc)
        mcp.lifecycle_state = lifecycle_for_status(stable_status(outcome.status))
        return mcp

    def set_published(self, mcp_server_id: str, published: bool) -> Optional[McpServer]:
        """Flip the cross-tenant publish flag as a platform write.

        ``published`` is an exposure flag, not governed content, so it goes through
        ``persist_identity`` (which re-asserts ``APPROVED``) rather than ``update()``. ``None`` when
        the record is gone, also when it vanishes between the read and the write; a
        ``ValidationException`` is the same :class:`McpValidationError` as ``update()``.
        """
        mcp = self.get(mcp_server_id)
        if mcp is None:
            return None
        mcp.published = published
        try:
            return self.persist_identity(mcp)
        except ClientError as err:
            if _is_not_found(err):
                return None
            if _is_validation(err):
                raise McpValidationError(_validation_message(err)) from err
            raise

    def persist_marketplace(
        self, mcp_server_id: str, publication: Optional[MarketplacePublication]
    ) -> McpServer:
        """Service-only write of the marketplace block; ``None`` clears it.

        ``McpServerUpdate`` omits ``marketplace`` because a declared datasheet is an attestation a
        request body must never forge. Takes an id rather than a record because the caller holds
        only the id from its publish request, and re-reading lands the block on the current
        record. Unpublish is the caller's: it passes ``published=False``, keeping the declared
        history. Raises :class:`McpServerNotFoundError` when the id no longer resolves.
        """
        mcp = self.get(mcp_server_id)
        if mcp is None:
            raise McpServerNotFoundError(f"MCP server {mcp_server_id!r} not found")
        mcp.marketplace = publication
        return self.persist_identity(mcp)

    def delete(self, mcp_server_id: str) -> Optional[McpServer]:
        """Delete the registry record and return the prior state (``None`` if it was gone).

        Record-only: the identity cascade (the Cedar policy engine, the Entra app and its
        consents) runs in ``services.mcp_teardown`` before this call, because it needs the ids
        this delete removes and because ``McpCedarService`` imports this module.
        """
        prior = self.get(mcp_server_id)
        if prior is None or not self._writer.delete(mcp_server_id):
            return None
        return prior

    def submit_for_approval(self, mcp_server_id: str) -> Optional[McpServer]:
        if not self._writer.submit(mcp_server_id):
            return None
        return self.get(mcp_server_id)

    def transition(self, mcp_server_id: str, action: str, reason: str) -> Optional[McpServer]:
        """Apply a lifecycle transition; ``None`` when the record is gone.

        Deprecating a record whose marketplace product is published unlists it (``published=False``
        keeps the declared datasheet as history); approve and reject never touch the block, which
        is what makes a deprecation stick. The lifecycle write comes first and an unlist failure
        propagates, so a deprecated record is never left silently flagged published.
        """
        if action not in TRANSITION_TO_STATUS:
            raise IllegalTransitionError(
                f"Unknown transition action {action!r}; valid actions: {sorted(TRANSITION_TO_STATUS)}",
                "invalid_request",
            )
        if not reason or not reason.strip():
            raise IllegalTransitionError(
                "A non-empty reason is required for a status transition", "invalid_request"
            )

        try:
            landed = self._writer.set_status(mcp_server_id, TRANSITION_TO_STATUS[action], reason)
        except ClientError as err:
            error = err.response.get("Error", {})
            message = error.get("Message", "") or ""
            if error.get("Code") == "ValidationException" or "Invalid status transition" in message:
                raise IllegalTransitionError(message or str(err), "illegal_edge") from err
            raise
        if not landed:
            return None
        mcp = self.get(mcp_server_id)
        if (
            mcp is not None
            and TRANSITION_TO_STATUS[action] == _LIFECYCLE_TO_STATUS[LifecycleState.DEPRECATED]
            and mcp.marketplace is not None
            and mcp.marketplace.published
        ):
            return self.persist_marketplace(
                mcp_server_id, mcp.marketplace.model_copy(update={"published": False})
            )
        return mcp
