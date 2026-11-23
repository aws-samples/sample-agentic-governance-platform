"""The kill switch's job runner: contain, restore, and the view.

``contain`` / ``restore`` validate and write the state synchronously; the route schedules
``run_contain`` / ``run_restore`` as a BackgroundTask. Steps run in ``STEP_ORDER``; each one
catches its own AWS errors, records ``ok | skipped | failed`` and writes one audit row, and one
failed step never stops the next. The state stays ``CONTAINED`` until a restore finishes, so AGP
keeps refusing invokes even when every native lever failed. Every state write is a transition from
the ``(state, job)`` the caller requires (``CONTAINABLE``, ``RESTORABLE``, the action's running
job), so two contain calls racing the same instant land one and refuse the other.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Final, Literal, get_args

import anyio.to_thread
from botocore.exceptions import ClientError

from core.config import settings
from core.errors import AppError, Outcome
from core.state_store import ABSENT
from models.containment import STEP_ORDER, AuditEvent, ContainmentState, StepId, StepResult
from models.mcp_server import Kind
from services.containment_native import RuntimeLevers, make_client_factory
from services.containment_store import get_audit_store, get_containment_store

logger = logging.getLogger(__name__)

MIN_REASON = 8
ALREADY_CONTAINED = "agent is already contained"
NOT_CONTAINED = "agent is not contained"
SAME_PERSON = "restore requires a different person than the one who contained the agent"
JOB_RUNNING = "agent containment is still running"
NO_IDENTITY = "contain requires a signed-in identity"
NOT_AGENTCORE = "containment is available for AgentCore agents only"
BUSY_JOBS = {"running", "restoring"}
IDLE_JOBS = frozenset(get_args(ContainmentState.model_fields["job"].annotation)) - BUSY_JOBS
CONTAINABLE: Final = {"state": {ABSENT, "RESTORED"}, "job": {ABSENT, *IDLE_JOBS}}
RESTORABLE: Final = {"state": {"CONTAINED"}, "job": {ABSENT, *IDLE_JOBS}}

StepStatus = Literal["ok", "skipped", "failed"]
StepOutcome = tuple[StepStatus, str, str | None]
JobAction = Literal["contain", "restore"]
AuditResult = Literal["ok", "skipped", "failed", "requested"]
RUNNING_JOB: Mapping[JobAction, str] = {"contain": "running", "restore": "restoring"}


class ContainmentConflict(AppError):
    """A contain or restore was refused by the agent's platform, its containment state or the
    two-person rule. Every kind has a fixed detail, one of this module's constants."""

    domain: ClassVar[str] = "containment"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "no_identity": Outcome(409, NO_IDENTITY),
        "job_running": Outcome(409, JOB_RUNNING),
        "already_contained": Outcome(409, ALREADY_CONTAINED),
        "not_contained": Outcome(409, NOT_CONTAINED),
        "same_person": Outcome(409, SAME_PERSON),
        "not_agentcore": Outcome(409, NOT_AGENTCORE),
    }


def _check_reason(reason: str) -> None:
    if len((reason or "").strip()) < MIN_REASON:
        raise ValueError(f"reason must be at least {MIN_REASON} characters")


def _ids(*values: str | None) -> set[str]:
    """Non-empty identifiers, case-folded (emails differ in case across IdPs)."""
    return {v.strip().casefold() for v in values if v and v.strip()}


def _policy_gone(exc: Exception) -> bool:
    """The cedar service wraps every ClientError in McpCedarError ``from`` the original."""
    cause = exc.__cause__
    return (
        isinstance(cause, ClientError)
        and cause.response.get("Error", {}).get("Code") == "ResourceNotFoundException"
    )


def _undo_status(statuses: list[StepStatus]) -> StepStatus:
    """An undo step is failed when ANY line failed (a partial undo is not done)."""
    return "failed" if "failed" in statuses else _aggregate(statuses)


def _error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}".strip()


def _aggregate(results: list[StepStatus]) -> StepStatus:
    """ok if any ok, skipped if all skipped (or nothing to do), else failed."""
    if not results or all(r == "skipped" for r in results):
        return "skipped"
    return "ok" if "ok" in results else "failed"


def agent_stage_cfg(agent):
    """The tenant stage owning the agent's (first) runtime; None means ambient credentials. The
    project service is reached through its route accessor because a service importing a route at
    load is a cycle."""
    from api.routes import projects

    stage = next(iter(agent.runtime_arns()), None)
    return projects.get_project_service()._stage_cfg(agent.tenant_id, stage)


class ContainmentService:
    def __init__(
        self,
        *,
        store,
        audit,
        cedar,
        levers,
        agents_loader: Callable[[str], Any],
        mcp_loader: Callable[[str], Any],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._store = store
        self._audit = audit
        self._cedar = cedar
        self._levers = levers
        self._agents = agents_loader
        self._mcp = mcp_loader
        self._now = now
        self._last: datetime | None = None

    def _stamp(self) -> str:
        """A strictly increasing microsecond ISO time, so audit rows order stably."""
        t = self._now()
        if self._last is not None and t <= self._last:
            t = self._last + timedelta(microseconds=1)
        self._last = t
        return t.isoformat(timespec="microseconds")

    def _log(
        self,
        agent_id: str,
        action: JobAction,
        actor: tuple[str, str],
        result: AuditResult,
        *,
        step: StepId | None = None,
        detail: str = "",
        native_ref: str | None = None,
    ) -> None:
        self._audit.append(
            AuditEvent(
                agent_id=agent_id,
                at=self._stamp(),
                actor_oid=actor[0],
                actor_name=actor[1],
                action=action,
                step=step,
                result=result,
                detail=detail,
                native_ref=native_ref,
            )
        )

    def _finish(
        self,
        state: ContainmentState,
        step_id: StepId,
        outcome: StepOutcome,
        action: JobAction,
        actor: tuple[str, str],
    ) -> None:
        status, detail, native_ref = outcome
        step = next(s for s in state.steps if s.id == step_id)
        step.status, step.detail, step.native_ref, step.at = status, detail, native_ref, self._stamp()
        self._advance(state, action)
        self._log(state.agent_id, action, actor, status, step=step_id, detail=detail, native_ref=native_ref)

    def _advance(self, state: ContainmentState, action: JobAction) -> None:
        """Write the job's progress while the row still carries this action's running job."""
        if not self._store.put_state(state, {"job": {RUNNING_JOB[action]}}):
            raise RuntimeError(f"the {action} job for agent {state.agent_id} no longer holds the row")

    async def contain(self, agent, principal, reason: str) -> ContainmentState:
        _check_reason(reason)
        if not _ids(principal.oid, principal.email):
            raise ContainmentConflict(NO_IDENTITY, "no_identity")
        newest = self._store.list_signals(agent.id, limit=1)
        state = ContainmentState(
            agent_id=agent.id,
            state="CONTAINED",
            job="running",
            since=self._stamp(),
            by_oid=principal.oid or principal.email,
            by_email=principal.email,
            by_name=principal.email or principal.oid,
            reason=reason.strip(),
            signal_at=newest[0].detected_at if newest else None,
            steps=[StepResult(id=s) for s in STEP_ORDER],
        )
        if not self._store.put_state(state, CONTAINABLE):
            current = self._store.get_state(agent.id)
            if current is not None and current.job == "restoring":
                raise ContainmentConflict(JOB_RUNNING, "job_running")
            raise ContainmentConflict(ALREADY_CONTAINED, "already_contained")
        self._log(agent.id, "contain", (state.by_oid, state.by_name), "requested", detail=state.reason)
        return state

    async def run_contain(self, agent_id: str) -> None:
        state = self._store.get_state(agent_id)
        if state is None or state.job != "running":
            return
        actor = (state.by_oid, state.by_name)
        try:
            await self._contain_steps(agent_id, state, actor)
        except Exception as exc:
            logger.exception("[contain] job failed for agent %s", agent_id)
            self._fail_job(state, "contain", actor, exc)

    async def _contain_steps(self, agent_id: str, state: ContainmentState, actor: tuple[str, str]) -> None:
        agent = self._agents(agent_id)
        self._finish(
            state, "refuse-invokes", ("ok", "AGP refuses invokes while contained", None), "contain", actor
        )
        if agent is None:
            cedar = runtime = ("failed", "agent not found", None)
        else:
            cedar = await self._forbid_gateways(agent, state)
        self._finish(state, "cedar-forbid", cedar, "contain", actor)
        if agent is not None:
            runtime = await self._deny_runtimes(agent, state)
        self._finish(state, "runtime-deny", runtime, "contain", actor)
        state.job = "contained" if "ok" in (cedar[0], runtime[0]) else "failed"
        self._advance(state, "contain")

    def _fail_job(
        self, state: ContainmentState, action: JobAction, actor: tuple[str, str], exc: Exception
    ) -> None:
        """Unexpected error: unfinished steps failed, job failed, state CONTAINED (AGP keeps refusing)."""
        detail = _error(exc)
        for step in state.steps:
            if step.status in ("pending", "running"):
                step.status, step.detail, step.at = "failed", detail, self._stamp()
        state.state, state.job = "CONTAINED", "failed"
        try:
            if self._store.put_state(state, {"job": {RUNNING_JOB[action]}}):
                self._log(state.agent_id, action, actor, "failed", detail=detail)
        except Exception:
            logger.exception("[%s] could not record the failure for agent %s", action, state.agent_id)

    async def _forbid_gateways(self, agent, state: ContainmentState) -> StepOutcome:
        if not agent.entra_app_id:
            return "failed", "agent has no Entra app id", None
        statuses: list[StepStatus] = []
        lines: list[str] = []
        policies: list[dict] = []
        for mcp_id in agent.mcp_server_ids or []:
            try:
                mcp = await anyio.to_thread.run_sync(self._mcp, mcp_id)
                if mcp is None or mcp.kind != Kind.GATEWAY:
                    continue
                res = await self._cedar.add_agent_forbid(
                    mcp, agent_app_id=agent.entra_app_id, agent_label=agent.name
                )
                policy_id = res.get("policy_id") if res["status"] == "ok" else None
            except Exception as exc:
                logger.warning(
                    "[contain] cedar forbid failed for agent %s on %s: %s",
                    agent.id,
                    mcp_id,
                    _error(exc),
                    exc_info=True,
                )
                statuses.append("failed")
                lines.append(f"{mcp_id}: failed ({_error(exc)})")
                continue
            statuses.append(res["status"])
            if res["status"] == "ok":
                policies.append({"mcp_id": mcp_id, "policy_id": policy_id})
                lines.append(f"{mcp_id}: ok ({policy_id})")
            else:
                lines.append(f"{mcp_id}: {res['status']} ({res.get('detail', '')})")
        state.policies = policies
        detail = "; ".join(lines) or "no Cedar gateways granted"
        return _aggregate(statuses), detail, ",".join(p["policy_id"] for p in policies) or None

    async def _deny_runtimes(self, agent, state: ContainmentState) -> StepOutcome:
        statuses: list[StepStatus] = []
        lines: list[str] = []
        ok_arns: list[str] = []
        for arn in dict.fromkeys(agent.runtime_arns().values()):
            res = await anyio.to_thread.run_sync(self._levers.deny_invokes, agent, arn)
            statuses.append(res["status"])
            if res["status"] == "ok":
                state.runtime_policy_before[arn] = res.get("before")
                ok_arns.append(arn)
                lines.append(f"{arn}: ok")
            else:
                lines.append(f"{arn}: {res['status']} ({res.get('detail', '')})")
        return _aggregate(statuses), "; ".join(lines) or "no runtimes", ",".join(ok_arns) or None

    async def restore(self, agent, principal, reason: str) -> ContainmentState:
        _check_reason(reason)
        state = self._store.get_state(agent.id)
        if state is None or state.state != "CONTAINED":
            raise ContainmentConflict(NOT_CONTAINED, "not_contained")
        if state.job in BUSY_JOBS:
            raise ContainmentConflict(JOB_RUNNING, "job_running")
        caller = _ids(principal.oid, principal.email)
        if not caller or caller & _ids(state.by_oid, state.by_email):
            raise ContainmentConflict(SAME_PERSON, "same_person")
        actor = principal.oid or principal.email
        state.job = "restoring"
        state.steps = [StepResult(id=s) for s in STEP_ORDER]
        state.restored_by_name = principal.email or principal.oid
        state.restored_by_oid = actor
        if not self._store.put_state(state, RESTORABLE):
            current = self._store.get_state(agent.id)
            if current is None or current.state != "CONTAINED":
                raise ContainmentConflict(NOT_CONTAINED, "not_contained")
            raise ContainmentConflict(JOB_RUNNING, "job_running")
        self._log(agent.id, "restore", (actor, state.restored_by_name), "requested", detail=reason.strip())
        return state

    async def run_restore(self, agent_id: str) -> None:
        state = self._store.get_state(agent_id)
        if state is None or state.job != "restoring":
            return
        actor = (state.restored_by_oid or "", state.restored_by_name or "")
        try:
            await self._restore_steps(agent_id, state, actor)
        except Exception as exc:
            logger.exception("[restore] job failed for agent %s", agent_id)
            self._fail_job(state, "restore", actor, exc)

    async def _restore_steps(self, agent_id: str, state: ContainmentState, actor: tuple[str, str]) -> None:
        """Native levers first; AGP stops refusing only once they are all undone. Anything left to
        undo keeps the state CONTAINED with the refs, so restore can be retried."""
        agent = self._agents(agent_id)
        cedar = await self._delete_policies(state)
        self._finish(state, "cedar-forbid", cedar, "restore", actor)
        runtime = await self._restore_runtimes(agent, state)
        self._finish(state, "runtime-deny", runtime, "restore", actor)
        if state.policies or state.runtime_policy_before:
            state.job = "failed"
            self._finish(
                state,
                "refuse-invokes",
                ("skipped", "native undo incomplete; retry restore", None),
                "restore",
                actor,
            )
            return
        state.state, state.job, state.restored_at = "RESTORED", "restored", self._stamp()
        self._finish(state, "refuse-invokes", ("ok", "AGP accepts invokes again", None), "restore", actor)

    async def _delete_policies(self, state: ContainmentState) -> StepOutcome:
        statuses: list[StepStatus] = []
        lines: list[str] = []
        kept: list[dict] = []
        deleted: list[str] = []
        for p in state.policies:
            try:
                mcp = await anyio.to_thread.run_sync(self._mcp, p["mcp_id"])
                if mcp is None:
                    statuses.append("ok")
                    lines.append(
                        f"{p['mcp_id']}: mcp server record gone; policy {p['policy_id']} not verified"
                    )
                    continue
                await self._cedar.delete_policy(mcp, p["policy_id"])
            except Exception as exc:
                if _policy_gone(exc):
                    statuses.append("ok")
                    deleted.append(p["policy_id"])
                    lines.append(f"{p['mcp_id']}: already gone ({p['policy_id']})")
                    continue
                logger.warning(
                    "[restore] policy delete failed for %s: %s", p["policy_id"], _error(exc), exc_info=True
                )
                statuses.append("failed")
                kept.append(p)
                lines.append(f"{p['mcp_id']}: failed ({_error(exc)})")
                continue
            statuses.append("ok")
            deleted.append(p["policy_id"])
            lines.append(f"{p['mcp_id']}: deleted ({p['policy_id']})")
        state.policies = kept
        return _undo_status(statuses), "; ".join(lines) or "no policies to delete", ",".join(deleted) or None

    async def _restore_runtimes(self, agent, state: ContainmentState) -> StepOutcome:
        if not state.runtime_policy_before:
            return "skipped", "no runtimes to restore", None
        if agent is None:
            return "failed", "agent not found", None
        statuses: list[StepStatus] = []
        lines: list[str] = []
        ok_arns: list[str] = []
        kept: dict[str, str | None] = {}
        for arn, before in state.runtime_policy_before.items():
            res = await anyio.to_thread.run_sync(self._levers.restore_invokes, agent, arn, before)
            statuses.append(res["status"])
            if res["status"] == "ok":
                ok_arns.append(arn)
            elif res["status"] == "failed":
                kept[arn] = before
            lines.append(f"{arn}: {res['status']}" + (f" ({res['detail']})" if res.get("detail") else ""))
        state.runtime_policy_before = kept
        return _undo_status(statuses), "; ".join(lines), ",".join(ok_arns) or None

    def view(self, agent_id: str) -> dict:
        state = self._store.get_state(agent_id)
        region = settings.AWS_REGION
        return {
            "agent_id": agent_id,
            "state": state.state if state else None,
            "job": state.job if state else None,
            "since": state.since if state else None,
            "by_name": state.by_name if state else None,
            "by_oid": state.by_oid if state else None,
            "reason": state.reason if state else None,
            "signal_at": state.signal_at if state else None,
            "steps": [s.model_dump() for s in state.steps] if state else [],
            "signals": [s.model_dump(exclude={"agent_id"}) for s in self._store.list_signals(agent_id)],
            "monitor": {"enabled": self._store.get_monitor(agent_id)["enabled"]},
            "audit": [e.model_dump(exclude={"agent_id", "actor_oid"}) for e in self._audit.list(agent_id)],
            "cloudtrail_url": (
                f"https://{region}.console.aws.amazon.com/cloudtrailv2/home"
                f"?region={region}#/events?EventName=CreatePolicy"
            ),
        }


_service: ContainmentService | None = None


def get_containment_service() -> ContainmentService:
    global _service
    if _service is None:
        from api.routes.agents import get_service as get_agents
        from api.routes.mcp_cedar import get_cedar_service
        from api.routes.mcp_servers import get_service as get_mcps

        _service = ContainmentService(
            store=get_containment_store(),
            audit=get_audit_store(),
            cedar=get_cedar_service(),
            levers=RuntimeLevers(client_factory=make_client_factory(agent_stage_cfg)),
            agents_loader=get_agents().get,
            mcp_loader=get_mcps().get,
        )
    return _service
