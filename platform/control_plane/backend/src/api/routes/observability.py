"""Observability read routes (Epic 26, Task 6 — contract C4).

The tenant-scoped, VIEWER-GET read surface the frontend renders its Langfuse-backed
dashboards from (E26 replaces the bare Langfuse iframe embed). Two endpoints live here:

  - ``GET /observability/settings`` — echoes whether Langfuse is configured
    (``configured = bool(LANGFUSE_HOST)``) + the host, so the FE can show a
    graceful "not configured" state. No secret VALUE is ever surfaced.
  - ``GET /observability/metrics?scope=platform|tenant|project[&project_id=…]`` —
    resolves the visible agent set for the scope, then calls the T5 metrics service
    for the merged ``totals``/``daily``/``by_model`` AND assembles ``by_agent[]``
    (per-agent totals) — the by_agent shape is the route's job (T5 returns merged only).

The per-agent ``GET /agents/{id}/metrics`` + ``GET /agents/{id}/traces`` routes live on
the agents router (with the other per-agent sub-resources) and share its
``_load_visible_agent`` gate.

TENANT SCOPING (E24): every scope is filtered through ``visible(ctx, agent.tenant_id)`` —
a scoped caller only ever sees their own tenant's agents; a global admin sees all. A
foreign/unknown agent is never enumerated for a caller who can't see it.

RBAC: VIEWER (GET is a read, not a governance mutation) — mirrors ``list_agents`` /
``get_agent``.
"""

import logging
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi import Depends as RBACDepends

from core.config import settings
from core.rbac import Role, require_role
from services.langfuse_metrics_service import LangfuseMetricsService
from services.tenant_resolver import TenantContext, visible

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/observability", tags=["observability"])

_metrics_svc: Optional[LangfuseMetricsService] = None


def get_metrics_service() -> LangfuseMetricsService:
    """Lazy ``LangfuseMetricsService`` singleton (E26/T5 read path).

    Wired from the E26 config (Langfuse host) + the registry region; the service reads
    each agent's project key from Secrets Manager on demand. Tests patch this module's
    ``_metrics_svc`` directly with a mock so no live Langfuse/AWS is touched.
    """
    global _metrics_svc
    if _metrics_svc is None:
        _metrics_svc = LangfuseMetricsService(
            langfuse_host=settings.LANGFUSE_HOST,
            region=getattr(settings, "AGENT_REGISTRY_REGION", "") or "us-east-1",
        )
    return _metrics_svc


# The per-request tenant_ctx dependency is the SAME one agents.py delegates to (the ONE
# resolver-singleton accessor on users.py) — re-exported here so this router resolves the
# caller identically and tests can patch ``api.routes.users._tenant_resolver`` once.
from api.routes.agents import get_tenant_ctx  # noqa: E402


@router.get("/settings")
async def get_observability_settings(
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """Echo whether Langfuse is configured + its host (never any key VALUE).

    ``configured = bool(LANGFUSE_HOST)`` (contract C4/C6); ``langfuse_host`` is the
    configured host or ``None`` when unset — the FE renders a graceful not-configured
    state off ``configured``."""
    return {
        "langfuse_host": settings.LANGFUSE_HOST or None,
        "configured": bool(settings.LANGFUSE_HOST),
    }


def _visible_scope_agents(scope: str, project_id: Optional[str], ctx: TenantContext):
    """Resolve the visible agent set for a metrics ``scope`` (all tenant-filtered).

    - ``platform`` — every agent, tenant-filtered (a global admin sees all; a scoped
      caller sees only their own tenant's).
    - ``tenant``   — the caller's visible agents (identical filter — ``visible()`` is the
      tenant boundary; the two differ only semantically for the FE label).
    - ``project``  — the agents materialized into ``project_id`` (via the project's
      repositories), STILL visibility-filtered so a foreign agent in a shared project id
      never leaks.

    Every path ends in the SAME ``visible(ctx, a.tenant_id)`` gate — the E24 boundary.
    """
    from api.routes.agents import get_service

    records = get_service().list()

    if scope == "project":
        if not project_id:
            raise HTTPException(
                status_code=400, detail="project_id is required for scope=project"
            )
        agent_ids = _project_agent_ids(project_id, ctx)
        records = [a for a in records if a.id in agent_ids]

    return [a for a in records if visible(ctx, a.tenant_id)]


def _project_agent_ids(project_id: str, ctx: TenantContext) -> set:
    """The agent ids materialized into a project (via its repositories).

    An agent's project membership lives on the ``Repository`` record (``project_id`` →
    ``agent_id``), not the agent envelope — so we read the project's repositories.

    Defense-in-depth (E24/T6): the project read is gated on the SAME
    ``visible(ctx, project.tenant_id)`` boundary the rest of the app uses (mirrors
    ``projects._load_visible_project``), so a foreign/invisible project is
    indistinguishable from a missing one — BOTH yield an empty set (⇒ zeroed metrics,
    never an error, no existence oracle). This reuses the read-only visibility check
    (not the OPERATOR mutate-gate), keeping the VIEWER-GET intent; the trailing
    agent-level ``visible()`` filter still backstops any per-agent leak."""
    from api.routes.projects import get_project_service

    detail = get_project_service().get_project(project_id)
    if detail is None or not visible(ctx, detail.project.tenant_id):
        return set()
    return {r.agent_id for r in detail.repositories if r.agent_id}


@router.get("/metrics")
async def get_scope_metrics(
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
    scope: str = Query(default="platform"),
    project_id: Optional[str] = Query(default=None),
    date_from: Optional[date] = Query(default=None),
    date_to: Optional[date] = Query(default=None),
):
    """Scope dashboard: merged totals/daily/by_model + a per-agent ``by_agent[]`` list.

    Resolves the visible agent set for ``scope`` (all tenant-filtered — a foreign agent is
    never included), calls the T5 ``get_scope_metrics`` for the merged headline, and
    assembles ``by_agent[]`` from a per-agent ``get_agent_metrics`` (T5 returns the merged
    aggregate only — by_agent is the route's job). ``date_from``/``date_to`` default to the
    trailing 30-day window when omitted."""
    if scope not in ("platform", "tenant", "project"):
        raise HTTPException(status_code=400, detail=f"invalid scope: {scope}")
    date_to = date_to or date.today()
    date_from = date_from or (date_to - timedelta(days=30))

    svc = get_metrics_service()
    agents = _visible_scope_agents(scope, project_id, ctx)

    merged = await svc.get_scope_metrics(agents, date_from, date_to)
    by_agent = []
    for agent in agents:
        agent_metrics = await svc.get_agent_metrics(agent, date_from, date_to)
        by_agent.append(
            {
                "agent_id": agent.id,
                "agent_name": agent.name,
                "tenant_id": agent.tenant_id,
                "totals": agent_metrics.totals.model_dump(),
            }
        )

    body = merged.model_dump()
    body["by_agent"] = by_agent
    return body
