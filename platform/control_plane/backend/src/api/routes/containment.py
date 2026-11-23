"""The kill-switch routes, nested under ``/agents``: GET the containment view (viewer), PUT the
monitor flag (operator), POST contain (operator, 202) and POST restore (admin, 202).

Every route resolves the agent through ``agents._load_visible_agent`` first, so a foreign tenant's
agent is the same 404 as a missing one. Contain and restore write the state synchronously and run
the native levers as a background task; a ``ContainmentConflict`` renders through the ``AppError``
handler. Contain is AgentCore-only; the view, the monitor flag and restore are not gated on it.
"""

import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks
from fastapi import Depends as RBACDepends
from pydantic import StringConstraints

from api.routes.agents import _load_visible_agent, get_tenant_ctx
from core.rbac import Principal, Role, current_principal, require_role
from models.base import WireModel
from models.containment import AuditEvent, ContainmentView, MonitorState
from services.agent_identity_service import is_agentcore_agent
from services.containment_service import MIN_REASON, ContainmentConflict, get_containment_service
from services.containment_store import get_audit_store, get_containment_store
from services.tenant_resolver import TenantContext

router = APIRouter(prefix="/agents", tags=["containment"])


class MonitorRequest(WireModel):
    enabled: bool


class ReasonRequest(WireModel):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=MIN_REASON)]


@router.get("/{agent_id}/containment", response_model=ContainmentView)
async def get_containment(
    agent_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    agent = await _load_visible_agent(agent_id, ctx)
    return get_containment_service().view(agent.id)


@router.put("/{agent_id}/containment/monitor", response_model=MonitorState)
async def set_monitor(
    agent_id: str,
    req: MonitorRequest,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    agent = await _load_visible_agent(agent_id, ctx)
    store = get_containment_store()
    if store.get_monitor(agent.id)["enabled"] == req.enabled:
        return {"enabled": req.enabled}
    cursor = int(time.time() * 1000) if req.enabled else None
    store.set_monitor(agent.id, enabled=req.enabled, cursor_ms=cursor)
    get_audit_store().append(
        AuditEvent(
            agent_id=agent.id,
            at=datetime.now(UTC).isoformat(timespec="microseconds"),
            actor_oid=principal.oid or principal.email or "",
            actor_name=principal.email or principal.oid or "unknown",
            action="monitor-on" if req.enabled else "monitor-off",
            result="ok",
        )
    )
    return {"enabled": req.enabled}


async def _run(
    verb: str,
    agent_id: str,
    req: ReasonRequest,
    principal: Principal,
    ctx: TenantContext,
    background: BackgroundTasks,
) -> dict:
    agent = await _load_visible_agent(agent_id, ctx)
    if verb == "contain" and not is_agentcore_agent(agent):
        raise ContainmentConflict(f"agent {agent.id} is not an AgentCore agent", kind="not_agentcore")
    service = get_containment_service()
    await getattr(service, verb)(agent, principal, req.reason)
    background.add_task(getattr(service, f"run_{verb}"), agent.id)
    return service.view(agent.id)


@router.post("/{agent_id}/containment/contain", response_model=ContainmentView, status_code=202)
async def contain_agent(
    agent_id: str,
    req: ReasonRequest,
    background: BackgroundTasks,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    return await _run("contain", agent_id, req, principal, ctx, background)


@router.post("/{agent_id}/containment/restore", response_model=ContainmentView, status_code=202)
async def restore_agent(
    agent_id: str,
    req: ReasonRequest,
    background: BackgroundTasks,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    return await _run("restore", agent_id, req, principal, ctx, background)
