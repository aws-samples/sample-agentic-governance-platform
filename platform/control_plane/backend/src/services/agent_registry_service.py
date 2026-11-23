"""AWS Agent Registry-backed CRUD and lifecycle for agents.

One registry holds one CUSTOM record per agent; the governance model is serialized into the
record's ``descriptors.custom.data`` JSON (``Agent.to_envelope()`` / ``Agent.from_record()``),
and ``lifecycle_state`` derives from the native ``status``. The registry lives on the
``agent-registry`` namespace; Identity, Gateway and Policy stay on ``bedrock-agentcore``. Every
registry call goes through ``services.registry_writer.RegistryWriter``. No HTTP vocabulary here:
the errors are ``core.errors.AppError`` tables.
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
from models.agent import (
    TRANSITION_TO_STATUS,
    Agent,
    AgentCreate,
    AgentUpdate,
    IdentityStatus,
    LifecycleState,
    Platform,
    lifecycle_for_status,
)
from models.marketplace import MarketplacePublication
from services.registry_writer import RecordContent, RegistryWriter, stable_status

logger = logging.getLogger(__name__)

_RECORD_TYPE_AGENT = "CUSTOM"

_LIFECYCLE_TO_STATUS: dict[LifecycleState, str] = {
    LifecycleState.PENDING_APPROVAL: "PENDING_APPROVAL",
    LifecycleState.APPROVED: "APPROVED",
    LifecycleState.REJECTED: "REJECTED",
    LifecycleState.DEPRECATED: "DEPRECATED",
}


def apply_creator_sponsor(create: AgentCreate, principal) -> AgentCreate:
    """The creator sponsors by default: fill ``sponsor_email`` / ``sponsor_oid`` from ``principal``
    when the create payload leaves them blank. Pure; shared by ``POST /agents`` and
    ``ProjectService.add_repo`` so both flows default the owner identically.
    """
    if not create.sponsor_email:
        create.sponsor_email = principal.email
    if not create.sponsor_oid:
        create.sponsor_oid = principal.oid
    return create


def _record_content(agent: Agent) -> RecordContent:
    """The record's governed content: ``description`` falls back to the name because the registry
    requires one character."""
    return RecordContent(
        name=agent.name,
        description=agent.purpose or agent.name,
        descriptors={"custom": {"data": json.dumps(agent.to_envelope())}},
    )


class NameTakenError(AppError):
    """A registry record with the requested name already exists.

    The message names the requested name and nothing else; the registry's response never
    reaches it. The pre-check is racy (the AWS Agent Registry has no conditional put), so two
    concurrent creates can both pass it.
    """

    domain: ClassVar[str] = "agent"
    kinds: ClassVar[Mapping[str, Outcome]] = {"name_taken": Outcome(409, None)}


class AgentNotFoundError(AppError):
    """An agent id resolves to no record, or to one the caller's tenant does not see.

    The CRUD methods return ``Optional[Agent]`` and the agents routes raise this for a
    caller-supplied id, so a missing and a foreign agent answer the same 404.
    ``persist_marketplace`` raises it too, because its caller is a service holding a decision it
    is about to persist: a silent ``None`` would let an approve mark a publish request APPROVED
    while the registry write never landed. The detail is fixed; the message names the id.
    """

    domain: ClassVar[str] = "agent"
    kinds: ClassVar[Mapping[str, Outcome]] = {"not_found": Outcome(404, "Agent not found")}


class AgentRequestError(AppError):
    """An agent write the agents routes refuse; every kind has a fixed detail.
    ``platform_tenant_mismatch``: a governable platform label contradicts the tenant's platform.
    ``not_governed``: a reprovision of an agent with no runtime to bind.
    """

    domain: ClassVar[str] = "agent"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "platform_tenant_mismatch": Outcome(409, "agent platform does not match the tenant's platform"),
        "not_governed": Outcome(409, "not a governed agent"),
    }


class MalformedAgentRecordError(AppError, ValueError):
    """A CUSTOM registry record lacks a well-formed governance envelope.

    The ``descriptors.custom.data`` blob is missing, is not JSON, or holds a structurally
    invalid sub-model. ``get()`` raises it; ``list()`` skips the record and warns, so one bad
    record cannot abort the whole fan-out. The one kind has a fixed detail for a route that lets
    it escape; the message, which quotes the parse error, is for the log. Also a ``ValueError``,
    because the build-start path maps every ``ValueError`` from its registry read to its own
    ``build_failed`` outcome.
    """

    domain: ClassVar[str] = "agent"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "malformed_record": Outcome(502, "the agent's registry record could not be read"),
    }


class IllegalTransitionError(AppError):
    """A lifecycle transition was refused.

    ``illegal_transition``: the AWS Agent Registry rejected the status edge (``DRAFT ->
    APPROVED`` needs a submit first); the message is composed here from the validated action,
    never from the registry's response, which is logged instead. ``unknown_action``: the action
    is not one of ``TRANSITION_TO_STATUS``; the message echoes the caller's own action and the
    valid ones. ``missing_reason``: the registry requires a non-empty ``statusReason``; fixed
    detail.
    """

    domain: ClassVar[str] = "agent"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "illegal_transition": Outcome(409, None),
        "unknown_action": Outcome(400, None),
        "missing_reason": Outcome(400, "A non-empty reason is required for a status transition"),
    }

    def __init__(self, message: str, kind: str = "illegal_transition") -> None:
        super().__init__(message, kind)


def _is_not_found(err: ClientError) -> bool:
    return err.response.get("Error", {}).get("Code") == "ResourceNotFoundException"


class AgentRegistryService:
    """CRUD + lifecycle over a single AWS Agent Registry (Custom records)."""

    def __init__(
        self,
        registry_id: str = "",
        region: str | None = None,
        control_client=None,
        registry_name: str = "",
    ):
        """``registry_name`` is the normal path, resolved to the id on first use; ``registry_id`` is an
        explicit override that skips the lookup. Passing neither is a configuration error reported on
        first use. ``control_client`` reaches the writer, which obtains one when it is None.
        """
        self._writer = RegistryWriter(
            region=region,
            registry_id=registry_id,
            registry_name=registry_name,
            kind="agent",
            control_client=control_client,
        )

    @property
    def registry_id(self) -> str:
        return self._writer.registry_id

    def _hydrate(self, record_id: str) -> Optional[Agent]:
        """``GetRegistryRecord`` parsed into an ``Agent``; None when not found. A missing or non-JSON
        envelope and a structurally invalid sub-model both raise ``MalformedAgentRecordError``,
        translated here rather than in ``models.agent`` because that import edge would be a cycle.
        """
        resp = self._writer.get(record_id)
        if resp is None:
            return None
        try:
            inline = resp["descriptors"]["custom"]["data"]
            envelope = json.loads(inline)
        except (KeyError, TypeError, json.JSONDecodeError) as err:
            raise MalformedAgentRecordError(
                f"Registry record {record_id!r} has no well-formed governance envelope: {err}"
            ) from err
        try:
            return Agent.from_record(resp, envelope)
        except ValidationError as err:
            raise MalformedAgentRecordError(
                f"Registry record {record_id!r} has a structurally invalid governance envelope: {err}"
            ) from err

    def create(
        self,
        req: AgentCreate,
        created_by: Optional[str] = None,
        *,
        project_id: Optional[str] = None,
    ) -> Agent:
        """Register an agent. ``project_id`` is a server-side stamp, a keyword rather than a field on
        ``AgentCreate``, so only a caller that already resolved and authorized the project can set it.
        An AgentCore agent is stamped ``identity_status='pending'`` into the create envelope; the route
        schedules ``provision`` after the 201. ``description`` falls back to the name because the API
        requires one character, and ``clientToken`` is a uuid5 of the name so a retried create is
        idempotent. The writer polls the record to DRAFT before returning so the following writes do not
        hit ``ConflictException``, and the lifecycle derives from the observed status.
        """
        if next(self._writer.iter_summaries(record_type=_RECORD_TYPE_AGENT, name=req.name), None) is not None:
            raise NameTakenError(f"Agent name '{req.name}' is already in use")

        now = datetime.now(timezone.utc)
        agent = Agent(
            **req.model_dump(exclude_none=False),
            id="",
            lifecycle_state=LifecycleState.PROPOSED,
            project_id=project_id,
            created_at=now,
            updated_at=now,
            created_by=created_by,
        )

        if agent.is_agentcore:
            agent.identity_status = IdentityStatus.PENDING

        agent.id, status = self._writer.create(
            _record_content(agent),
            record_type=_RECORD_TYPE_AGENT,
            client_token=str(uuid.uuid5(uuid.NAMESPACE_DNS, f"agp-agent:{req.name}")),
        )
        agent.lifecycle_state = lifecycle_for_status(status)
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
        status_filter = _LIFECYCLE_TO_STATUS.get(lifecycle_state) if lifecycle_state else None

        agents: List[Agent] = []
        for summary in self._writer.iter_summaries(record_type=_RECORD_TYPE_AGENT, status=status_filter):
            try:
                hydrated = self._hydrate(summary["recordId"])
            except MalformedAgentRecordError as err:
                logger.warning("Skipping malformed registry record: %s", err)
                continue
            if hydrated is not None:
                agents.append(hydrated)

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
        """Read-modify-write through the writer's governed edit; None when the record is gone. A user
        content edit leaves an APPROVED record DRAFT, so the response reports PROPOSED."""
        existing = self.get(agent_id)
        if existing is None:
            return None

        for field, value in req.model_dump(exclude_none=True).items():
            setattr(existing, field, value)
        existing.updated_at = datetime.now(timezone.utc)

        if self._writer.write_content(agent_id, _record_content(existing)) is None:
            return None
        if existing.lifecycle_state == LifecycleState.APPROVED:
            existing.lifecycle_state = LifecycleState.PROPOSED
        return existing

    def persist_identity(self, agent: Agent) -> Agent:
        """Envelope-write an in-hand ``Agent``: the fields ``AgentUpdate`` omits (identity, marketplace,
        the publish flag). The writer's platform write: no call when the record already holds this
        content, and an APPROVED record is put back to APPROVED afterwards; the returned agent
        carries the lifecycle of the status the writer saw.
        """
        outcome = self._writer.write(agent.id, _record_content(agent))
        if outcome.written:
            agent.updated_at = datetime.now(timezone.utc)
        agent.lifecycle_state = lifecycle_for_status(stable_status(outcome.status))
        return agent

    def set_published(self, agent_id: str, published: bool) -> Optional[Agent]:
        """Flip the cross-tenant publish flag as a platform write, through ``persist_identity`` so
        APPROVED is re-asserted; None when the record is gone.
        """
        agent = self.get(agent_id)
        if agent is None:
            return None
        agent.published = published
        try:
            return self.persist_identity(agent)
        except ClientError as err:
            if _is_not_found(err):
                return None
            raise

    def persist_marketplace(self, agent_id: str, publication: Optional[MarketplacePublication]) -> Agent:
        """Service-only write of the marketplace block; ``None`` clears it, an unpublish passes
        ``published=False`` to keep the declared history. Read-modify-write through
        ``persist_identity``. Raises ``AgentNotFoundError`` when the id no longer resolves, so an
        approve cannot report success on a write that never happened.
        """
        agent = self.get(agent_id)
        if agent is None:
            raise AgentNotFoundError(f"Agent {agent_id!r} not found")
        agent.marketplace = publication
        return self.persist_identity(agent)

    def delete(self, agent_id: str) -> Optional[Agent]:
        prior = self.get(agent_id)
        if prior is None or not self._writer.delete(agent_id):
            return None
        return prior

    def submit_for_approval(self, agent_id: str) -> Optional[Agent]:
        if not self._writer.submit(agent_id):
            return None
        return self.get(agent_id)

    def transition(self, agent_id: str, action: str, reason: str) -> Optional[Agent]:
        """Apply a lifecycle action; None when the record is gone. Deprecating a published
        marketplace product unlists it (``published=False`` keeps the declared history), and an
        unlist failure propagates rather than leaving a deprecated record flagged published."""
        if action not in TRANSITION_TO_STATUS:
            raise IllegalTransitionError(
                f"Unknown transition action {action!r}; valid actions: {sorted(TRANSITION_TO_STATUS)}",
                kind="unknown_action",
            )
        if not reason or not reason.strip():
            raise IllegalTransitionError(
                "A non-empty reason is required for a status transition", kind="missing_reason"
            )

        try:
            landed = self._writer.set_status(agent_id, TRANSITION_TO_STATUS[action], reason)
        except ClientError as err:
            error = err.response.get("Error", {})
            message = error.get("Message", "") or ""
            if error.get("Code") == "ValidationException" or "Invalid status transition" in message:
                logger.warning(
                    "[registry] the registry refused %r on record %s: %s", action, agent_id, message
                )
                raise IllegalTransitionError(
                    f"Invalid status transition: {action!r} is not allowed from the agent's current state",
                    kind="illegal_transition",
                ) from err
            raise
        if not landed:
            return None
        agent = self.get(agent_id)
        if (
            agent is not None
            and TRANSITION_TO_STATUS[action] == _LIFECYCLE_TO_STATUS[LifecycleState.DEPRECATED]
            and agent.marketplace is not None
            and agent.marketplace.published
        ):
            return self.persist_marketplace(
                agent_id, agent.marketplace.model_copy(update={"published": False})
            )
        return agent
