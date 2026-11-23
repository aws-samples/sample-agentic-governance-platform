"""User-to-agent grants (Entra app-role assignments) and the principal picker.

Grants are live Microsoft Graph reads and writes, never a table: ``appRoleAssignedTo`` on the agent's
service principal is the single source of truth. Two routers live here. ``router`` (``/agents``)
nests the grants under the agent: ``GET/POST /agents/{id}/grants``, ``DELETE
/agents/{id}/grants/{assignment_id}`` and, for Databricks-governed agents only, ``GET
/agents/{id}/grants/drift`` and ``POST /agents/{id}/grants/reassert``. ``entra_router``
(``/entra``) is the Graph-backed principal picker, ``GET /entra/principals/search?q=``.

``get_graph_service()`` here is the one source of the ``GraphService`` instance; ``routes/agents.py``
imports it for ``/invoke`` and the provisioning hook, so the whole app shares one client. The routes
own RBAC, the tenant gate and the wire shapes; how a grant becomes real, including the Databricks
ACL mirror and its doctrine, is :mod:`services.grant_mirror_service`, whose errors the one
``AppError`` handler renders.

Role mapping: Graph returns an ``appRoleId`` GUID, mapped to "Invoker" or "Admin" through the agent's
stored ``invoker_role_id`` and ``admin_role_id`` (an unknown id is "Unknown"); a create maps the
requested role name back to the stored GUID (an unknown role name is a 400).
"""

from typing import List, Optional

from fastapi import APIRouter, Query, Response
from fastapi import Depends as RBACDepends
from pydantic import Field

from core.rbac import Principal, Role, current_principal, require_role
from models.agent import is_databricks_governed_agent
from models.base import WireModel
from services.grant_mirror_service import (
    ACL_KIND_GROUP,
    DriftRead,
    GrantMirrorError,
    assign_agent_role,
    get_grant_mirror_service,
    is_provisioned,
    revoke_agent_role,
)
from services.graph_service import GraphService, PrincipalHitType, graph_service_from_settings
from services.tenant_resolver import TenantContext

router = APIRouter(prefix="/agents", tags=["grants"])
entra_router = APIRouter(prefix="/entra", tags=["entra"])

_MIN_SEARCH_LEN = 2

_graph_svc: Optional[GraphService] = None


def get_graph_service() -> GraphService:
    global _graph_svc
    if _graph_svc is None:
        _graph_svc = graph_service_from_settings()
    return _graph_svc


async def get_tenant_ctx(
    principal: Principal = RBACDepends(current_principal),
) -> TenantContext:
    """The per-request ``TenantContext`` through the one resolver singleton ``users.py`` owns.

    Tests patch ``api.routes.users._tenant_resolver`` and the grant-list read gate observes it.
    Imported lazily because a module-level import is a cycle.
    """
    from api.routes.users import get_tenant_ctx as _users_get_tenant_ctx

    return await _users_get_tenant_ctx(principal)


class AgentGrantCreate(WireModel):
    principal_id: str = Field(description="The Entra object id of the user or group.")
    principal_type: str = Field(
        description="'user' or 'group'. Display only: Graph resolves the real type from the oid."
    )
    role: str = Field(description="'Invoker' or 'Admin'.")


class AgentGrantRead(WireModel):
    assignment_id: str = Field(description="The appRoleAssignment id; the handle a revoke takes.")
    principal_id: str
    principal_display: str
    principal_type: str = Field(description="Graph's principalType: 'User' or 'Group'.")
    role: str = Field(
        description="'Invoker', 'Admin', or 'Unknown' for an appRoleId the agent does not define."
    )


class PrincipalHit(WireModel):
    id: str
    display_name: str
    type: PrincipalHitType = Field(description="'user' or 'group'.")
    mail: Optional[str] = None


def _role_for_app_role_id(agent, app_role_id: Optional[str]) -> str:
    """Map a Graph appRoleId GUID to "Invoker" or "Admin" through the agent's stored ids.

    An id matching neither, or no id, is "Unknown": a role minted by another tool or a stale
    assignment never breaks the list view.
    """
    if app_role_id and app_role_id == agent.invoker_role_id:
        return "Invoker"
    if app_role_id and app_role_id == agent.admin_role_id:
        return "Admin"
    return "Unknown"


def _app_role_id_for_role(agent, role: str) -> str:
    """Map a requested role name to the agent's stored appRoleId GUID; anything else is a 400."""
    if role == "Invoker":
        return agent.invoker_role_id
    if role == "Admin":
        return agent.admin_role_id
    raise GrantMirrorError(kind="invalid_role")


_is_provisioned = is_provisioned


def _to_grant_read(agent, assignment: dict) -> AgentGrantRead:
    return AgentGrantRead(
        assignment_id=assignment.get("id", ""),
        principal_id=assignment.get("principalId", ""),
        principal_display=assignment.get("principalDisplayName") or "",
        principal_type=assignment.get("principalType") or "",
        role=_role_for_app_role_id(agent, assignment.get("appRoleId")),
    )


async def _refuse_cross_tenant_grant(agent, principal: Principal, grantee_id: str) -> None:
    """A non-admin may only grant when the grantee belongs to the agent's tenant; otherwise 403.

    The grantee's tenant set comes from ``TenantResolver.resolve_oid_tenants``: a user's Graph
    group memberships, unioned with the oid itself appearing in a tenant's ``entra_group_ids`` so a
    same-tenant group grantee passes without a Graph resolve. An unresolvable grantee is an empty
    set, and an agent with no ``tenant_id`` can never be same-tenant, so both fail closed to the
    fixed 403 without a Graph read. Runs before the Graph assign.
    """
    if principal.role >= Role.ADMIN:
        return
    if agent.tenant_id is None:
        raise GrantMirrorError(kind="cross_tenant")
    from api.routes.users import get_tenant_resolver

    grantee_tenants = await get_tenant_resolver().resolve_oid_tenants(grantee_id)
    if agent.tenant_id not in grantee_tenants:
        raise GrantMirrorError(kind="cross_tenant")


@router.get("/{agent_id}/grants", response_model=List[AgentGrantRead])
async def list_grants(
    agent_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """List the agent's app-role assignments, the access list.

    An unprovisioned agent answers ``[]``, not a 409, so the frontend can show its banner. The read
    is gated on the same ``_load_visible_agent`` as the parent ``GET /agents/{id}``: a foreign
    tenant's agent 404s with the byte-identical "Agent not found", so this sub-resource can never
    enumerate an agent the detail route says is absent.
    """
    from api.routes.agents import _load_visible_agent

    agent = await _load_visible_agent(agent_id, ctx)

    if not _is_provisioned(agent) or agent.entra_sp_id is None:
        return []

    assignments = await get_graph_service().list_assignments(agent.entra_sp_id)
    return [_to_grant_read(agent, a) for a in assignments]


@router.post("/{agent_id}/grants", response_model=AgentGrantRead, status_code=201)
async def create_grant(
    agent_id: str,
    body: AgentGrantCreate,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Grant a user or group an Invoker or Admin app role on the agent.

    The agent loads through the same ``_load_visible_agent`` as ``list_grants``: a missing agent
    and a foreign tenant's agent answer the same 404 "Agent not found" before the provisioning
    409, the grantee resolve and any Graph call, so neither the 409 nor the grantee guard's 403
    can confirm an agent the caller's tenant does not expose. Then 409 when the agent's identity
    is not provisioned, 400 for a role other than Invoker or Admin, 409 when the principal is
    already assigned, 403 for a cross-tenant grantee by a non-admin. A group principal on a
    Databricks-governed agent is refused with a 422 before any write: a group has no
    ``user_name`` to mirror, and recording it would have the Access tab claim access the
    platform's door refuses. That check reads the body's display-only ``principal_type``; the mirror
    catches a mislabelled group oid against Graph's truthful answer and rolls the assignment back.
    On a Databricks-governed agent the assignment is then mirrored onto the app's access list, and
    a failed mirror rolls the assignment back; AgentCore agents skip the mirror.
    """
    from api.routes.agents import _load_visible_agent

    agent = await _load_visible_agent(agent_id, ctx)
    if not _is_provisioned(agent):
        raise GrantMirrorError(kind="not_provisioned")

    app_role_id = _app_role_id_for_role(agent, body.role)

    if is_databricks_governed_agent(agent) and body.principal_type.strip().casefold() == ACL_KIND_GROUP:
        raise GrantMirrorError(kind="group_unsupported")

    await _refuse_cross_tenant_grant(agent, principal, body.principal_id)

    assignment = await assign_agent_role(get_graph_service(), agent, body.principal_id, app_role_id)

    if is_databricks_governed_agent(agent):
        await get_grant_mirror_service().mirror_grant(agent, assignment)
    return _to_grant_read(agent, assignment)


@router.delete("/{agent_id}/grants/{assignment_id}", status_code=204)
async def delete_grant(
    agent_id: str,
    assignment_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Revoke an app-role assignment by id, answering 204.

    The agent loads through ``_load_visible_agent``: a missing agent and a foreign tenant's agent
    answer the same 404 "Agent not found" before the provisioning 409, the mirror target resolve
    and the Graph revoke. Then 409 when the agent is not provisioned (no SP id to address). A
    stale or already-deleted assignment id is a 404. On a Databricks-governed agent the ACL mirror
    target is resolved before the destructive Entra call, because afterwards the principal behind
    the assignment is no longer readable; the revoke is Entra first, ACL second, and never undone:
    a failed ACL removal answers 502 with the re-assert instruction and leaves the agent drifted.
    """
    from api.routes.agents import _load_visible_agent

    agent = await _load_visible_agent(agent_id, ctx)
    if not _is_provisioned(agent):
        raise GrantMirrorError(kind="not_provisioned")

    mirror_user = (
        await get_grant_mirror_service().revoke_mirror_target(agent, assignment_id)
        if is_databricks_governed_agent(agent)
        else None
    )

    await revoke_agent_role(get_graph_service(), agent, assignment_id)

    if mirror_user is not None:
        if not mirror_user:
            raise GrantMirrorError(kind="revoke_acl_failed")
        await get_grant_mirror_service().mirror_revoke(agent, mirror_user)
    return Response(status_code=204)


@router.get("/{agent_id}/grants/drift", response_model=DriftRead)
async def get_grants_drift(
    agent_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """Diff the agent's Entra grants against its Databricks app ACL, both directions.

    A read, gated exactly like ``list_grants``: VIEWER plus the shared ``_load_visible_agent``.
    Detection is automatic, repair is a human's button: this route never writes, because silently
    re-asserting would hide that someone edits the app's ACL around the platform.
    """
    from api.routes.agents import _load_visible_agent

    agent = await _load_visible_agent(agent_id, ctx)

    mirror = get_grant_mirror_service()
    mirror.gate(agent)
    return await mirror.drift(agent)


@router.post("/{agent_id}/grants/reassert", response_model=DriftRead)
async def reassert_grants_acl(
    agent_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Rewrite the app's ACL from AGP's grants, then answer with fresh drift.

    RBAC is the grant mutations' threshold, OPERATOR, not admin-only: this rewrites derived state
    from grants an operator may already create and delete, and the Access tab gates the button on
    that same ``canManage``. The desired list, the strip log and the fresh-drift answer are
    :meth:`GrantMirrorService.reassert`'s.
    """
    from api.routes.agents import _load_visible_agent

    agent = await _load_visible_agent(agent_id, ctx)

    mirror = get_grant_mirror_service()
    mirror.gate(agent)
    return await mirror.reassert(agent)


@entra_router.get("/principals/search", response_model=List[PrincipalHit])
async def search_principals(
    q: str = Query(default=""),
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """Graph ``$search`` over users and groups for the picker; a query under two characters is ``[]``."""
    if len(q.strip()) < _MIN_SEARCH_LEN:
        return []

    hits = await get_graph_service().search_principals(q.strip())
    return [
        PrincipalHit(
            id=h["id"],
            display_name=h.get("displayName") or "",
            type=h["type"],
            mail=h.get("mail"),
        )
        for h in hits
    ]
