"""Agent Registry service — AWS Agent Registry-backed CRUD + lifecycle (Epic 4, Task 2).

Wraps one boto3 client behind a store-agnostic interface (research §6):
  - ``agent-registry-control`` (``self._ctl``) — CRUD + lifecycle (registry records)

E32: the Registry moved off the ``bedrock-agentcore`` namespace onto its own
``agent-registry`` one (AWS, 2026-08-06; the old namespace shuts down 2026-09-17 and brand-new
accounts cannot use it at all). Only the Registry moved — Identity, Gateway, and Policy (the
``agent_identity_service`` / ``agent_credential_service`` / ``mcp_identity_service`` /
``mcp_cedar_service`` clients) all stay on ``bedrock-agentcore``. The move also dropped the
reserved data-plane client this service used to hold but never called: the new namespace's data
plane is a different API surface (``SearchDiscoverableRegistryRecords``), so it will be wired
deliberately if/when discovery is actually needed, not carried as dead state.

One AWS Agent Registry holds one **Custom record per agent**. The governance model
is serialized into the record's ``descriptors.custom.data`` JSON (the
"envelope", research §4) — built/parsed via ``Agent.to_envelope()`` /
``Agent.from_record()``. ``lifecycle_state`` is derived from the native ``status``
(research §5) and never stored in the envelope.

Mirrors ``services.operating_model_service`` structure: module logger,
``NameTakenError``, config-injected constructor, ``create/get/list/update/delete``
returning ``Optional[...]``, read-modify-write ``update``. No FastAPI/HTTP concerns
live here (those are in the T3 routes).
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
from models.agent import (
    TRANSITION_TO_STATUS,
    Agent,
    AgentCreate,
    AgentUpdate,
    IdentityStatus, LifecycleState,
    Platform,
    lifecycle_for_status,
)
from models.marketplace import MarketplacePublication

logger = logging.getLogger(__name__)

# Registry record constants (single-source so a future bump is one edit).
_RECORD_TYPE_AGENT = "CUSTOM"  # agents are CUSTOM records (E32: recordType, not descriptorType)
_RECORD_VERSION = "1.0.0"

# Native registry status used as a server-side ListRegistryRecords filter for a
# given LifecycleState, ONLY where the mapping is 1:1 (PROPOSED also covers the
# transient CREATING/*_FAILED states, so we omit a server filter for it and
# filter client-side instead). Everything else stays client-side too — see list().
_LIFECYCLE_TO_STATUS: dict[LifecycleState, str] = {
    LifecycleState.PENDING_APPROVAL: "PENDING_APPROVAL",
    LifecycleState.APPROVED: "APPROVED",
    LifecycleState.REJECTED: "REJECTED",
    LifecycleState.DEPRECATED: "DEPRECATED",
}


def apply_creator_sponsor(create: AgentCreate, principal) -> AgentCreate:
    """The creator sponsors by default: back-fill ``sponsor_*`` from ``principal``.

    Sets ``sponsor_email``/``sponsor_oid`` from ``principal.email``/``principal.oid``
    ONLY when the create payload leaves them blank; an explicitly-supplied sponsor is
    left untouched. Pure, no I/O — mutates and returns the same ``AgentCreate`` so both
    the ``POST /agents`` route and the repo-creation path (``project_service.add_repo``)
    can share one owner-defaulting rule and avoid "ownerless" agents (research §4).
    ``principal`` is any object exposing ``.oid`` / ``.email`` (``core.rbac.Principal``).
    """
    if not create.sponsor_email:
        create.sponsor_email = principal.email
    if not create.sponsor_oid:
        create.sponsor_oid = principal.oid
    return create


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

    Must be sent on EVERY update that touches the name, because ``Agent.from_record``
    PREFERS ``displayName`` over ``name``: patching ``name`` alone leaves the registry
    holding the old ``displayName``, so a rename returns 200 with the new name and then
    silently reverts on the very next read, in both the list and detail views.

    Exactly ONE level of wrapping — ``UpdateRegistryRecord.displayName`` is an
    ``UpdatedDisplayName`` structure whose only member is ``optionalValue``, the same
    shape as ``description`` and NOT the three levels ``descriptors`` needs. (Confirmed
    against the agent-registry-control botocore model — see
    test_registry_update_param_shape.) Note the sibling ``name`` on the same call stays a
    PLAIN string: it is shape ``RegistryRecordName``, unwrapped.
    """
    return {"optionalValue": text}


def _wrap_update_descriptors_custom(inline_content: str) -> dict:
    """Wrap a CUSTOM descriptor's ``data`` blob in the UpdateRegistryRecord envelope.

    On Create the CUSTOM descriptor is the flat ``{"custom": {"data": ...}}``; on Update
    EVERY level gets its own ``{"optionalValue": ...}`` wrapper — ``descriptors``, then
    ``custom``, then ``data`` itself — so the blob sits three levels deep.

    E32 note, because this is the easy thing to get wrong: the PATCH-envelope *idea*
    survived the namespace move, but the depth did NOT. On the old
    ``bedrock-agentcore-control`` model the leaf was a bare string
    (``{"custom": {"optionalValue": {"inlineContent": <str>}}}``); on
    ``agent-registry-control`` the renamed ``data`` leaf is itself an ``UpdatedDescriptorData``
    STRUCTURE, so it needs one more ``optionalValue``. Sending the old two-level shape fails
    param validation with "Invalid type for parameter ...custom.optionalValue.data, valid
    types: dict". (Confirmed against the botocore model — see test_registry_update_param_shape.)
    """
    return {
        "optionalValue": {
            "custom": {"optionalValue": {"data": {"optionalValue": inline_content}}}
        }
    }


class NameTakenError(Exception):
    """Raised when a registry record with the same name already exists.

    Note: the underlying check is a *racy* pre-check (research §7) — the AWS Agent
    Registry has no conditional-put on CreateRegistryRecord. Two concurrent creates
    could both pass the pre-check. Acceptable for the prototype.
    """


class AgentNotFoundError(Exception):
    """Raised when a write targets an agent id that no longer resolves to a record.

    The CRUD methods here return ``Optional[Agent]`` and let the route map a ``None`` to a
    404, which is right for a caller-supplied id. ``persist_marketplace`` raises instead,
    because its caller is a SERVICE holding a decision it is about to persist: a silent
    ``None`` there would let an approve mark a publish request APPROVED while the registry
    write never landed. An exception forces that path to fail loudly and keep the request
    retryable.
    """


class MalformedAgentRecordError(ValueError):
    """Raised when a CUSTOM registry record lacks a well-formed governance envelope.

    A record could be missing the ``descriptors.custom.data`` blob or carry
    non-JSON content (e.g. written by another tool, or a partially-synced Preview
    record). ``get()`` surfaces this loudly; ``list()`` skips-and-warns so one bad
    record can't abort the whole fan-out / demo list view.
    """


class IllegalTransitionError(ValueError):
    """Raised when a requested lifecycle transition is an illegal status edge.

    The AWS Agent Registry enforces legal status edges (research §5): e.g.
    ``DRAFT -> APPROVED`` is rejected with a ``ValidationException`` — a record
    must first be submitted (``-> PENDING_APPROVAL``) before it can be approved.
    ``transition()`` maps that AWS error to this domain error so the route layer
    can return a clean 4xx (409) carrying the AWS message, rather than letting a
    raw ``ValidationException`` surface as a 500. Subclasses ``ValueError`` so a
    route that only maps ``ValueError`` still returns a client error.
    """


def _is_not_found(err: ClientError) -> bool:
    return err.response.get("Error", {}).get("Code") == "ResourceNotFoundException"


class AgentRegistryService:
    """CRUD + lifecycle over a single AWS Agent Registry (Custom records)."""

    def __init__(
        self,
        registry_id: str = "",
        region: str = "us-east-1",
        control_client=None,
        registry_name: str = "",
    ):
        """Accept the registry NAME, the registry ID, or both.

        ``registry_name`` is the normal production path (E32 follow-up): AWS mints the
        registryId and it cannot be chosen, so Terraform could only hand it over through a
        capture file it read during the PLAN walk — before the provisioner that writes it had
        run — which is what forced a from-zero deploy to `terraform apply` TWICE. The NAME is
        a static config value everyone knows at plan time, and name -> id is one
        ``ListRegistries`` call, so the backend resolves it itself. See
        ``core.registry_resolver`` for the full rationale, including why a duplicate name is
        a hard error.

        ``registry_id`` stays supported as an explicit OVERRIDE that short-circuits
        resolution entirely (no AWS call), because six operational scripts pass ids directly
        and the ``AGENT_REGISTRY_ID`` setting must keep working. It is now OPTIONAL rather
        than required — passing neither is a configuration error reported on first use, not a
        silent empty ``registryId`` on every call.
        """
        # NOT `self.registry_id` — that name is now a lazily-resolving property, so the
        # supplied value (which may be empty) is held privately and doubles as the resolution
        # cache. Everything inside this class keeps reading `self.registry_id`.
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

        Lazy and instance-memoised, deliberately:

        * NOT at import time and NOT in ``Settings()`` — importing a module must stay
          side-effect-free (``tests/test_api_properties.py`` bypasses ``src/__init__.py``
          specifically to avoid triggering ``Settings()`` validation, and that property is
          worth preserving). The route-level ``get_service()`` singletons are lazy too, so
          the first resolution happens on the first request that actually needs the registry.
        * A FAILURE IS NEVER CACHED. Only a successful lookup writes ``_registry_id``, so a
          transient AWS error (expired credentials, throttling, a brief control-plane 5xx)
          does not poison this singleton for the lifetime of the process — the next call
          retries. Caching the failure would turn a 30-second outage into "restart the ECS
          task", which is the kind of fix nobody finds from the symptom.

        No lock: resolution is an idempotent read, so two threads racing here converge on the
        same value and the only cost is one redundant ``ListRegistries`` call.
        """
        if self._registry_id:
            return self._registry_id
        if not self.registry_name:
            raise RegistryNotConfiguredError(
                "AgentRegistryService was constructed with neither a registry id nor a "
                "registry name, so it cannot address a registry. Set AGENT_REGISTRY_NAME "
                "(the normal path — the id is then resolved by name) or AGENT_REGISTRY_ID "
                "(an explicit override)."
            )
        self._registry_id = resolve_registry_id(
            self.registry_name, self.region, ctl=self._ctl
        )
        logger.info(
            "resolved agent registry %r -> %s (region %s)",
            self.registry_name,
            self._registry_id,
            self.region,
        )
        return self._registry_id

    # --- helpers -----------------------------------------------------------

    @staticmethod
    def _record_id_from_arn(record_arn: str) -> str:
        """Parse the recordId from a recordArn (CreateRegistryRecord output has no
        recordId — research §6). ARN tail is ``.../record/{recordId}``."""
        return record_arn.rsplit("/", 1)[-1]

    def _name_exists(self, name: str) -> bool:
        """Racy name pre-check via the structured ``name`` filter (research §7).

        E32: ListRegistryRecords is now POST-shaped — the discrete ``name`` /
        ``descriptorType`` / ``status`` query params were replaced by one structured
        ``filters=[{"name": ..., "values": [...]}]`` list.
        """
        resp = self._ctl.list_registry_records(
            registryId=self.registry_id,
            filters=[{"name": "name", "values": [name]}],
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
            "filters": [{"name": "recordType", "values": [_RECORD_TYPE_AGENT]}],
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

    def _hydrate(self, record_id: str) -> Optional[Agent]:
        """GetRegistryRecord + parse the envelope into an Agent. None if not found.

        Raises MalformedAgentRecordError if the record exists but its CUSTOM
        envelope is missing/non-JSON, or holds a structurally invalid sub-model.
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
            inline = resp["descriptors"]["custom"]["data"]
            envelope = json.loads(inline)
        except (KeyError, TypeError, json.JSONDecodeError) as err:
            raise MalformedAgentRecordError(
                f"Registry record {record_id!r} has no well-formed governance "
                f"envelope: {err}"
            ) from err
        # A well-formed JSON envelope can still hold a structurally invalid SUB-MODEL: since
        # E33 ``from_record`` parses the ``marketplace`` block via
        # ``MarketplacePublication.model_validate``, which raises pydantic's
        # ``ValidationError``. That is the same class of fault as a non-JSON envelope, so it
        # must reach callers as ``MalformedAgentRecordError`` — otherwise ``list()`` (which
        # catches only that type) lets it escape and ONE bad record 500s the whole catalog,
        # defeating the skip-and-warn contract below. Translated HERE, at ``from_record``'s
        # caller, rather than inside ``models.agent``: ``MalformedAgentRecordError`` is defined
        # in this module and this module imports ``models.agent``, so raising it from the parse
        # site would need the reverse import edge — an import cycle.
        try:
            return Agent.from_record(resp, envelope)
        except ValidationError as err:
            raise MalformedAgentRecordError(
                f"Registry record {record_id!r} has a structurally invalid governance "
                f"envelope: {err}"
            ) from err

    def _poll_record_to_draft(
        self, record_id: str, attempts: int = 10, delay: float = 0.5
    ) -> str:
        """Poll GetRegistryRecord until the record reaches DRAFT (mirror MCP §6.2).

        Returns the final observed native status string (``"DRAFT"``) so the caller can
        derive the lifecycle from the freshly-observed status rather than the stale
        create response (which is ``"CREATING"`` in the real world).

        A freshly-created record returns ``CREATING`` and is briefly un-modifiable; a
        ``ConflictException`` while ``CREATING`` is expected — keep polling. Bounded loop;
        raises ``RuntimeError`` on timeout. In tests the mock returns DRAFT immediately so
        it exits on the first poll. (Cloned from ``McpServerRegistryService`` which already
        proves this pattern; an agent CUSTOM record has the same CREATING→DRAFT lag, and
        skipping the poll is exactly the E6 ConflictException bug this fixes.)
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

    def create(
        self,
        req: AgentCreate,
        created_by: Optional[str] = None,
        *,
        project_id: Optional[str] = None,
    ) -> Agent:
        """Register an agent. ``project_id`` (E27/T5) is a SERVER-SIDE stamp — a keyword
        here rather than a field on ``AgentCreate``, so the owning project can only ever be
        set by a caller that already resolved and authorized it (``ProjectService.add_repo``),
        never by a request body. ``POST /agents`` leaves it None → unparented, tenant-gated
        only."""
        # Racy name pre-check (research §7) -> NameTakenError -> HTTP 409.
        if self._name_exists(req.name):
            raise NameTakenError(f"Agent name '{req.name}' is already in use")

        now = datetime.now(timezone.utc)
        # The authoritative id is the recordId, only known after create. Build the
        # Agent with a placeholder id so to_envelope() can run, then overwrite id +
        # lifecycle from the create response. (The envelope's agent_id mirror is for
        # debug only; the native recordId is authoritative — Decision 3.)
        agent = Agent(
            **req.model_dump(exclude_none=False),
            id="",
            lifecycle_state=LifecycleState.PROPOSED,
            project_id=project_id,
            created_at=now,
            updated_at=now,
            created_by=created_by,
        )

        # Provisioning-on-registration (Epic 6): for an AgentCore agent (arn + entra +
        # aws_bedrock), stamp identity_status='pending' so it is serialized INTO the
        # create envelope (to_envelope() includes identity_status). This replaces the
        # redundant update-right-after-create the route used to do — which 500'd with a
        # ConflictException because the record was still CREATING. A metadata agent keeps
        # the default 'none'. The route schedules provision() (background) after the 201.
        if agent.is_agentcore:
            agent.identity_status = IdentityStatus.PENDING

        resp = self._ctl.create_registry_record(
            registryId=self.registry_id,
            name=req.name,
            # E32: displayName is the new human-facing label; `name` stays the unique
            # identifier. We have exactly one name to offer, so both carry it — a distinct
            # display label would need a new AgentCreate field and a UI to set it.
            displayName=req.name,
            # E32: recordType replaces the removed descriptorType, and is REQUIRED on create.
            recordType=_RECORD_TYPE_AGENT,
            # CreateRegistryRecord requires description min length 1 — fall back to the
            # (always-present) name so a purpose-less create (e.g. the add_repo template
            # flow) can't send "" and trip a ParamValidationError -> raw 500.
            description=req.purpose or req.name,
            descriptors={"custom": {"data": json.dumps(agent.to_envelope())}},
            recordVersion=_RECORD_VERSION,
            # Stable idempotency token for retried creates: deterministic uuid5 derived
            # from the (unique) name -> same name yields the same 36-char token, and the
            # AWS API requires clientToken min length 33.
            clientToken=str(uuid.uuid5(uuid.NAMESPACE_DNS, f"agp-agent:{req.name}")),
        )

        agent.id = self._record_id_from_arn(resp["recordArn"])
        # A fresh record returns CREATING and is briefly un-modifiable; poll it to DRAFT
        # before returning so the subsequent background provision() writes (and any later
        # update/submit) don't hit a ConflictException (mirror the MCP service). Derive
        # lifecycle from the freshly-observed status, not the stale create response (which
        # is "CREATING" in the real world).
        final_status = self._poll_record_to_draft(agent.id)
        agent.lifecycle_state = lifecycle_for_status(final_status)
        return agent

    def get(self, agent_id: str) -> Optional[Agent]:
        return self._hydrate(agent_id)

    def list(
        self,
        *,
        lifecycle_state: Optional[LifecycleState] = None,
        sponsor_oid: Optional[str] = None,
        business_unit: Optional[str] = None,
        region: Optional[str] = None,
        platform: Optional[Platform] = None,
    ) -> List[Agent]:
        # Server-side status filter only where the lifecycle->status mapping is 1:1
        # (PROPOSED maps from several native states, so we filter it client-side).
        status_filter = _LIFECYCLE_TO_STATUS.get(lifecycle_state) if lifecycle_state else None

        agents: List[Agent] = []
        # N+1 fan-out: list summaries omit descriptors, so GetRegistryRecord per id
        # to read the governance envelope (research §6; fine at demo density).
        for summary in self._iter_record_summaries(status=status_filter):
            try:
                hydrated = self._hydrate(summary["recordId"])
            except MalformedAgentRecordError as err:
                # Skip-and-warn: one bad record must not abort the whole list view.
                logger.warning("Skipping malformed registry record: %s", err)
                continue
            if hydrated is not None:
                agents.append(hydrated)

        # Client-side filters: lifecycle (when not server-filtered) + governance.
        def _keep(a: Agent) -> bool:
            if lifecycle_state is not None and a.lifecycle_state != lifecycle_state:
                return False
            if sponsor_oid is not None and a.sponsor_oid != sponsor_oid:
                return False
            if business_unit is not None and a.business_unit != business_unit:
                return False
            if region is not None and a.region != region:
                return False
            if platform is not None and a.platform != platform:
                return False
            return True

        result = [a for a in agents if _keep(a)]
        result.sort(key=lambda a: a.updated_at, reverse=True)
        return result

    def update(self, agent_id: str, req: AgentUpdate) -> Optional[Agent]:
        existing = self.get(agent_id)
        if existing is None:
            return None

        # Read-modify-write (mirror operating_model_service.update).
        for field, value in req.model_dump(exclude_none=True).items():
            setattr(existing, field, value)
        existing.updated_at = datetime.now(timezone.utc)

        try:
            # UpdateRegistryRecord is a PATCH API: description + descriptors are
            # wrapped in the optionalValue envelope (unlike the flat create shape).
            self._ctl.update_registry_record(
                registryId=self.registry_id,
                recordId=agent_id,
                name=existing.name,  # plain string — NOT wrapped
                # Both name fields, always: reads prefer displayName, so patching
                # `name` alone makes a rename revert on the next read (E32).
                displayName=_wrap_update_display_name(existing.name),
                # ``Description`` is min length 1 on the update shape too, so a
                # purpose-less agent must fall back to the (always-present) name rather
                # than patch "" — botocore rejects the zero-length string with a
                # ParamValidationError, which this method's ClientError-only handler
                # can't map, so it escapes as a raw 500. Same reason ``create()`` above
                # already sends ``req.purpose or req.name``. (Confirmed against the
                # agent-registry-control botocore model — see
                # test_registry_update_param_shape.)
                description=_wrap_update_description(existing.purpose or existing.name),
                descriptors=_wrap_update_descriptors_custom(
                    json.dumps(existing.to_envelope())
                ),
                recordVersion=_RECORD_VERSION,
            )
        except ClientError as err:
            if _is_not_found(err):
                return None
            raise
        return existing

    def persist_identity(self, agent: Agent) -> Agent:
        """Envelope-write an in-hand ``Agent`` (used to persist E6 identity fields).

        ``AgentUpdate`` deliberately omits the identity fields (``entra_sp_id`` /
        ``entra_app_audience`` / ``invoker_role_id`` / ``admin_role_id`` /
        ``identity_status``), so ``update()`` cannot persist them. This thin helper
        mirrors ``update()``'s envelope-write but for an already-hydrated ``Agent``:
        it bumps ``updated_at`` and writes ``to_envelope()`` via
        ``UpdateRegistryRecord``. Used by the T-IDENTITY provisioning orchestrator
        (and the T-ROUTES create hook). Returns the agent.
        """
        agent.updated_at = datetime.now(timezone.utc)
        # UpdateRegistryRecord is a PATCH API: description + descriptors are wrapped
        # in the optionalValue envelope (unlike the flat create shape).
        self._ctl.update_registry_record(
            registryId=self.registry_id,
            recordId=agent.id,
            name=agent.name,  # plain string — NOT wrapped
            # Both name fields, always: reads prefer displayName, so patching
            # `name` alone makes a rename revert on the next read (E32).
            displayName=_wrap_update_display_name(agent.name),
            # Same min-length-1 guard as update(): a purpose-less agent patches the name,
            # not "" — and this path matters MORE, because it runs unguarded on the
            # registration provisioning hook where nothing catches a ParamValidationError.
            description=_wrap_update_description(agent.purpose or agent.name),
            descriptors=_wrap_update_descriptors_custom(
                json.dumps(agent.to_envelope())
            ),
            recordVersion=_RECORD_VERSION,
        )
        return agent

    def persist_marketplace(
        self, agent_id: str, publication: Optional[MarketplacePublication]
    ) -> Agent:
        """Service-only write of the marketplace block (E33). ``None`` clears it.

        The marketplace counterpart of ``persist_identity``, and service-only for the same
        reason: ``AgentUpdate`` deliberately omits ``marketplace`` (a declared datasheet is an
        ATTESTATION — a request body must never be able to forge one), so ``update()`` cannot
        persist it. Read-modify-write: hydrate, set the block, and delegate the envelope write
        to ``persist_identity`` so the three-level PATCH descriptors wrap lives in exactly ONE
        place (see ``_wrap_update_descriptors_custom`` and test_registry_update_param_shape).

        Takes an ``agent_id`` rather than an ``Agent`` because the caller (the marketplace
        approve/unpublish path) holds only the id from its publish request, and re-reading is
        what makes the block land on the CURRENT record rather than a stale copy.

        Raises ``AgentNotFoundError`` when the id no longer resolves — a silent ``None`` would
        let an approve report success on a write that never happened.
        """
        agent = self.get(agent_id)
        if agent is None:
            raise AgentNotFoundError(f"Agent {agent_id!r} not found")
        # None CLEARS the block; an unpublish instead passes a publication with
        # published=False, which retains the declared history (E33 contract C2).
        agent.marketplace = publication
        return self.persist_identity(agent)

    def delete(self, agent_id: str) -> Optional[Agent]:
        prior = self.get(agent_id)
        if prior is None:
            return None
        try:
            self._ctl.delete_registry_record(
                registryId=self.registry_id, recordId=agent_id
            )
        except ClientError as err:
            if _is_not_found(err):
                return None
            raise
        return prior

    # --- lifecycle ---------------------------------------------------------

    def submit_for_approval(self, agent_id: str) -> Optional[Agent]:
        try:
            self._ctl.submit_registry_record_for_approval(
                registryId=self.registry_id, recordId=agent_id
            )
        except ClientError as err:
            if _is_not_found(err):
                return None
            raise
        return self.get(agent_id)

    def transition(self, agent_id: str, action: str, reason: str) -> Optional[Agent]:
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
                recordId=agent_id,
                status=TRANSITION_TO_STATUS[action],
                statusReason=reason,
            )
        except ClientError as err:
            # Not-found first (mirror the other control-plane calls).
            if _is_not_found(err):
                return None
            # The registry enforces legal status edges (research §5); an illegal
            # edge (e.g. DRAFT->APPROVED) surfaces as a ValidationException — map
            # it to a domain error so the route returns a clean 409, not a 500.
            error = err.response.get("Error", {})
            message = error.get("Message", "") or ""
            if error.get("Code") == "ValidationException" or "Invalid status transition" in message:
                raise IllegalTransitionError(message or str(err)) from err
            raise
        agent = self.get(agent_id)
        # E33 Amendment 2 / C12: deprecating a PUBLISHED marketplace product UNLISTS it.
        # Only the DEPRECATED target does this — approve/reject never touch the block, which
        # is what makes a deprecation STICK: a later lifecycle re-approve does not re-list the
        # product, only a fresh publish request can.
        #
        # Ordering is lifecycle write FIRST, unlist SECOND, and an unlist failure PROPAGATES
        # (the route error mapping surfaces it) rather than being swallowed: a silent failure
        # would leave a deprecated record still flagged published with nobody told. That state
        # is already refused by the read path's lifecycle gate (defense in depth), and the
        # retryable path is transition-again or an admin unpublish.
        if (
            agent is not None
            and TRANSITION_TO_STATUS[action] == _LIFECYCLE_TO_STATUS[LifecycleState.DEPRECATED]
            and agent.marketplace is not None
            and agent.marketplace.published
        ):
            # published=False (not None) keeps the declared datasheet as history — the C2
            # unpublish semantics, reused verbatim.
            return self.persist_marketplace(
                agent_id, agent.marketplace.model_copy(update={"published": False})
            )
        return agent
