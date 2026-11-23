"""Containment models: the kill-switch state, its steps, signals and audit rows.

No row for an agent means it was never contained. Times are ISO-8601 UTC strings.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from models.base import WireModel

StepId = Literal["refuse-invokes", "cedar-forbid", "runtime-deny"]
STEP_ORDER: tuple[StepId, ...] = ("refuse-invokes", "cedar-forbid", "runtime-deny")
ContainmentStateValue = Literal["CONTAINED", "RESTORED"]
ContainmentJob = Literal["running", "contained", "failed", "restoring", "restored"]


class StepResult(WireModel):
    id: StepId
    status: Literal["pending", "running", "ok", "skipped", "failed"] = "pending"
    detail: str = ""
    native_ref: str | None = Field(
        default=None, description="Policy id, runtime ARN or session ids, for CloudTrail matching"
    )
    at: str | None = Field(default=None, description="When the step finished")


class ContainmentState(WireModel):
    agent_id: str
    state: ContainmentStateValue
    job: ContainmentJob
    since: str = Field(description="When contain was clicked")
    by_oid: str = Field(description="principal.oid or principal.email")
    by_name: str
    by_email: str | None = Field(
        default=None, description="The container's other identifier, for the same-person check"
    )
    reason: str
    signal_at: str | None = Field(
        default=None, description="detected_at of the newest signal at contain time"
    )
    steps: list[StepResult]
    policies: list[dict] = Field(default=[], description='[{"mcp_id": str, "policy_id": str}]')
    runtime_policy_before: dict[str, str | None] = Field(
        default={}, description="Runtime ARN to the previous policy JSON, or None"
    )
    restored_by_name: str | None = None
    restored_by_oid: str | None = None
    restored_at: str | None = None


class SignalView(WireModel):
    """A signal as the containment view serves it: the stored ``Signal`` without its agent id."""

    session_id: str
    trace_id: str | None
    span_id: str
    tool: str | None
    label: str = Field(description='The judge\'s verdict on the span, always "FAIL": a PASS writes no signal')
    explanation: str
    span_end: str = Field(description="End time of the judged span")
    detected_at: str = Field(description="When AGP wrote the signal")
    source: str = "agentcore-evaluations"


class Signal(SignalView):
    agent_id: str


class AuditRow(WireModel):
    """An audit event as the containment view serves it: the stored ``AuditEvent`` without its agent
    id and actor oid."""

    at: str
    actor_name: str
    action: Literal["contain", "restore", "monitor-on", "monitor-off", "signal"]
    step: StepId | None = None
    result: Literal["ok", "skipped", "failed", "requested"]
    detail: str = ""
    native_ref: str | None = None


class AuditEvent(AuditRow):
    agent_id: str
    actor_oid: str


class MonitorState(WireModel):
    enabled: bool


class ContainmentView(WireModel):
    """The containment tab's body: the state flattened (every field ``None`` when the agent was never
    contained), the steps, the recent signals, the monitor flag and the audit trail."""

    agent_id: str
    state: ContainmentStateValue | None
    job: ContainmentJob | None
    since: str | None
    by_name: str | None
    by_oid: str | None
    reason: str | None
    signal_at: str | None
    steps: list[StepResult]
    signals: list[SignalView]
    monitor: MonitorState
    audit: list[AuditRow]
    cloudtrail_url: str = Field(description="CloudTrail event history filtered on EventName=CreatePolicy")
