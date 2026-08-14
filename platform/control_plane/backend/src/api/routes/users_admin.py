"""Platform Users admin (Epic 16) — list/add/change-role/remove platform access.

Entra/Graph is the system of record: the platform role-holders are the
``appRoleAssignedTo`` entries on the PLATFORM application's service principal
(the BACKEND app reg, which defines Platform.Admin/Operator/Viewer and is the
``aud`` of inbound user tokens — see ``_platform_app_client_id`` for why). This
module is a thin admin-gated pass-through over GraphService, mirroring grants.py's
RBAC + GraphError→HTTP idiom and reusing the ONE GraphService singleton.

Role vocabulary: the API speaks the short token "admin"/"operator"/"viewer";
Entra speaks the appRole `value` (settings.ENTRA_ROLE_*). We map between them via
config (never hardcode "Platform.*"). We only ever read/assign/revoke the three
Platform.* roles — assignments with any other appRoleId are left untouched.
"""

import logging
from typing import List

from fastapi import APIRouter, HTTPException, Response
from fastapi import Depends as RBACDepends
from pydantic import BaseModel

from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from services.graph_service import GraphError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/users", tags=["users-admin"])

# admin > operator > viewer — used to surface the highest role a principal holds.
_ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}


# --- models ------------------------------------------------------------------

class PlatformUser(BaseModel):
    principal_id: str
    display_name: str
    principal_type: str   # Graph's principalType: "User" | "Group" | "ServicePrincipal"
    role: str             # "admin" | "operator" | "viewer"


class RoleAssignmentCreate(BaseModel):
    principal_id: str
    role: str             # "admin" | "operator" | "viewer"


class RoleChange(BaseModel):
    role: str             # "admin" | "operator" | "viewer"


# --- config-derived maps + helpers (pure) ------------------------------------

def _platform_app_client_id() -> str:
    """The app reg client id whose SP holds the Platform.* assignments.

    Resolution: explicit override (ENTRA_PLATFORM_APP_CLIENT_ID) first, then the
    BACKEND app, then the SPA app as a last resort.

    WHY backend before SPA: the inbound user JWT's ``aud`` is the BACKEND app, and
    Entra sources the token's ``roles`` claim from the app-role assignments on the
    SP of the token-audience app. So a Platform.* role only reaches the user's token
    if it is assigned on the BACKEND app's service principal — assigning it on the
    SPA app's SP is a silent no-op (the SPA app is merely the OAuth client / ``azp``).
    The panel must therefore manage assignments on the backend app.
    """
    return (
        settings.ENTRA_PLATFORM_APP_CLIENT_ID
        or settings.ENTRA_BACKEND_CLIENT_ID
        or settings.ENTRA_SPA_CLIENT_ID
    )


def _platform_role_maps() -> tuple[dict, dict]:
    """(token_to_value, value_to_token) from settings.ENTRA_ROLE_*.

    e.g. token_to_value = {"admin": "Platform.Admin", ...};
         value_to_token = {"Platform.Admin": "admin", ...}.
    """
    token_to_value = {
        "admin": settings.ENTRA_ROLE_ADMIN,
        "operator": settings.ENTRA_ROLE_OPERATOR,
        "viewer": settings.ENTRA_ROLE_VIEWER,
    }
    return token_to_value, {v: k for k, v in token_to_value.items()}


def group_platform_users(assignments: list, role_id_to_token: dict) -> list:
    """Group appRoleAssignedTo entries by principal, keep only those whose appRoleId
    maps to a known platform role token, surface the HIGHEST role, sort by name.

    Returns [{principal_id, display_name, principal_type, role}].
    """
    best: dict[str, dict] = {}
    for a in assignments:
        token = role_id_to_token.get(a.get("appRoleId"))
        if token is None:
            continue  # foreign / default-access role — not a platform role
        pid = a.get("principalId") or ""
        cur = best.get(pid)
        if cur is None or _ROLE_RANK[token] > _ROLE_RANK[cur["role"]]:
            best[pid] = {
                "principal_id": pid,
                "display_name": a.get("principalDisplayName") or "",
                "principal_type": a.get("principalType") or "",
                "role": token,
            }
    return sorted(best.values(), key=lambda u: u["display_name"].lower())


def platform_assignment_ids_for(
    assignments: list,
    principal_id: str,
    role_id_to_token: dict,
    exclude_app_role_id: str | None = None,
) -> list:
    """All Platform.* appRoleAssignment ids held by ``principal_id`` (foreign roles excluded).

    When ``exclude_app_role_id`` is given, assignments for that appRoleId are skipped — used
    by change-role to revoke the principal's OTHER platform roles while keeping the one just
    assigned.
    """
    return [
        a["id"]
        for a in assignments
        if a.get("principalId") == principal_id
        and a.get("appRoleId") in role_id_to_token
        and a.get("appRoleId") != exclude_app_role_id
        and a.get("id")
    ]


def _role_id_to_token(role_id_by_value: dict, value_to_token: dict) -> dict:
    """Invert resolve_platform_sp's {value: id} into {id: token} for known roles."""
    return {
        rid: value_to_token[value]
        for value, rid in role_id_by_value.items()
        if value in value_to_token
    }


def _platform_user_from_assignment(assignment: dict, role_id_to_token: dict) -> PlatformUser:
    return PlatformUser(
        principal_id=assignment.get("principalId") or "",
        display_name=assignment.get("principalDisplayName") or "",
        principal_type=assignment.get("principalType") or "",
        role=role_id_to_token.get(assignment.get("appRoleId"), ""),
    )


# --- routes ------------------------------------------------------------------

@router.get("", response_model=List[PlatformUser])
async def list_platform_users(
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """List the platform's role-holders (Platform.* assignments on the platform SP)."""
    from api.routes.grants import get_graph_service

    _, value_to_token = _platform_role_maps()
    try:
        sp_id, role_id_by_value = await get_graph_service().resolve_platform_sp(
            _platform_app_client_id()
        )
        assignments = await get_graph_service().list_assignments(sp_id)
    except GraphError:
        raise HTTPException(status_code=502, detail="Failed to read platform users")

    role_id_to_token = _role_id_to_token(role_id_by_value, value_to_token)
    return group_platform_users(assignments, role_id_to_token)


@router.post("", response_model=PlatformUser, status_code=201)
async def add_platform_user(
    body: RoleAssignmentCreate,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Onboard an existing directory principal by assigning a platform role."""
    from api.routes.grants import get_graph_service

    token_to_value, value_to_token = _platform_role_maps()
    if body.role not in token_to_value:
        raise HTTPException(status_code=400, detail="role must be 'admin', 'operator', or 'viewer'")

    try:
        sp_id, role_id_by_value = await get_graph_service().resolve_platform_sp(
            _platform_app_client_id()
        )
        app_role_id = role_id_by_value.get(token_to_value[body.role])
        if not app_role_id:
            raise HTTPException(status_code=502, detail="Platform role is not configured")
        assignment = await get_graph_service().assign_app_role(
            sp_id, body.principal_id, app_role_id
        )
    except GraphError as err:
        if err.status in (400, 409):
            raise HTTPException(status_code=409, detail="principal already has a platform role")
        raise HTTPException(status_code=502, detail="Failed to assign the platform role")

    return _platform_user_from_assignment(assignment, _role_id_to_token(role_id_by_value, value_to_token))


@router.put("/{principal_id}/role", response_model=PlatformUser)
async def change_platform_role(
    principal_id: str,
    body: RoleChange,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Set a principal's platform role to EXACTLY one: ASSIGN the new role first, THEN revoke
    the principal's OTHER Platform.* assignments.

    Assign-first is the strictly-safer ordering: a failure AFTER the assign leaves the principal
    with the new role plus possibly a stale older role (harmless — ``group_platform_users``
    surfaces the highest; re-issuing the change converges), but NEVER zero access. If the
    principal already holds the target role, the assign surfaces a 409 (same fixed-detail idiom
    as onboarding) — a clean signal for an already-set role.
    """
    from api.routes.grants import get_graph_service

    token_to_value, value_to_token = _platform_role_maps()
    if body.role not in token_to_value:
        raise HTTPException(status_code=400, detail="role must be 'admin', 'operator', or 'viewer'")

    try:
        sp_id, role_id_by_value = await get_graph_service().resolve_platform_sp(
            _platform_app_client_id()
        )
        role_id_to_token = _role_id_to_token(role_id_by_value, value_to_token)
        app_role_id = role_id_by_value.get(token_to_value[body.role])
        if not app_role_id:
            raise HTTPException(status_code=502, detail="Platform role is not configured")
        # Assign FIRST so a mid-sequence failure never strips all access.
        assignment = await get_graph_service().assign_app_role(sp_id, principal_id, app_role_id)
        # Then revoke the principal's OTHER Platform.* roles (never the one just assigned).
        assignments = await get_graph_service().list_assignments(sp_id)
        for aid in platform_assignment_ids_for(
            assignments, principal_id, role_id_to_token, exclude_app_role_id=app_role_id
        ):
            await get_graph_service().revoke_app_role(sp_id, aid)
    except GraphError as err:
        if err.status in (400, 409):
            raise HTTPException(status_code=409, detail="principal already has a platform role")
        raise HTTPException(status_code=502, detail="Failed to change the platform role")

    return _platform_user_from_assignment(assignment, role_id_to_token)


@router.delete("/{principal_id}", status_code=204)
async def remove_platform_user(
    principal_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Revoke ALL of a principal's Platform.* assignments → 204. None held → 404."""
    from api.routes.grants import get_graph_service

    _, value_to_token = _platform_role_maps()
    try:
        sp_id, role_id_by_value = await get_graph_service().resolve_platform_sp(
            _platform_app_client_id()
        )
        role_id_to_token = _role_id_to_token(role_id_by_value, value_to_token)
        assignments = await get_graph_service().list_assignments(sp_id)
        ids = platform_assignment_ids_for(assignments, principal_id, role_id_to_token)
        if not ids:
            raise HTTPException(status_code=404, detail="user has no platform role to remove")
        for aid in ids:
            await get_graph_service().revoke_app_role(sp_id, aid)
    except GraphError:
        raise HTTPException(status_code=502, detail="Failed to remove platform access")

    return Response(status_code=204)
