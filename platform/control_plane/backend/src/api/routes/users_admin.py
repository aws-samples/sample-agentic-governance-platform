"""Platform users admin: list, add, change the role of, and remove the platform's role-holders.

Entra is the system of record: the role-holders are the ``appRoleAssignedTo`` entries on the
platform application's service principal, the backend app registration that defines
Platform.Admin, Platform.Operator and Platform.Viewer and is the ``aud`` of inbound user tokens
(``_platform_app_client_id`` says why). The routes own RBAC and the wire shapes, whose ``Literal``
role fields make an unknown token a 422; the Graph sequences and their failure meanings are the
``platform_role_holders`` family in :mod:`services.graph_platform_users`, whose ``PlatformUsersError``
kinds the one ``AppError`` handler renders.

Role vocabulary: the API speaks the short tokens "admin", "operator" and "viewer"; Entra speaks the
appRole value from ``settings.ENTRA_ROLE_*``. The mapping comes from configuration, never a
hardcoded "Platform.*", and only those three roles are ever read, assigned or revoked: an
assignment with any other appRoleId is left untouched.
"""

from typing import List, Literal

from fastapi import APIRouter, Response
from fastapi import Depends as RBACDepends
from pydantic import Field

from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from models.base import WireModel
from services.graph_platform_users import (
    assign_platform_role,
    platform_role_holders,
    replace_platform_role,
    revoke_platform_roles,
)

router = APIRouter(prefix="/admin/users", tags=["users-admin"])

_ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}

PlatformRole = Literal["admin", "operator", "viewer", ""]


class PlatformUser(WireModel):
    principal_id: str
    display_name: str
    principal_type: str = Field(description="Graph's principalType: 'User', 'Group' or 'ServicePrincipal'.")
    role: PlatformRole = Field(description="The highest platform role held: 'admin', 'operator' or 'viewer'.")


class RoleAssignmentCreate(WireModel):
    principal_id: str
    role: Literal["admin", "operator", "viewer"] = Field(description="'admin', 'operator' or 'viewer'.")


class RoleChange(WireModel):
    role: Literal["admin", "operator", "viewer"] = Field(description="'admin', 'operator' or 'viewer'.")


def _platform_app_client_id() -> str:
    """The app registration client id whose SP holds the Platform.* assignments.

    The explicit ``ENTRA_PLATFORM_APP_CLIENT_ID`` first, then the backend app, then the SPA app as
    a last resort. Backend before SPA because the inbound user JWT's ``aud`` is the backend app and
    Entra sources the token's ``roles`` claim from the assignments on the token-audience app's SP:
    a Platform.* role assigned on the SPA app's SP never reaches the user's token, the SPA app being
    only the OAuth client.
    """
    return (
        settings.ENTRA_PLATFORM_APP_CLIENT_ID
        or settings.ENTRA_BACKEND_CLIENT_ID
        or settings.ENTRA_SPA_CLIENT_ID
    )


def _platform_role_maps() -> tuple[dict, dict]:
    """``(token_to_value, value_to_token)`` from ``settings.ENTRA_ROLE_*``."""
    token_to_value = {
        "admin": settings.ENTRA_ROLE_ADMIN,
        "operator": settings.ENTRA_ROLE_OPERATOR,
        "viewer": settings.ENTRA_ROLE_VIEWER,
    }
    return token_to_value, {v: k for k, v in token_to_value.items()}


def group_platform_users(assignments: list, role_id_to_token: dict) -> list:
    """Group assignments by principal, keep the platform roles, surface the highest, sort by name.

    Returns ``[{principal_id, display_name, principal_type, role}]``; an assignment whose appRoleId
    maps to no platform role token is left out.
    """
    best: dict[str, dict] = {}
    for a in assignments:
        token = role_id_to_token.get(a.get("appRoleId"))
        if token is None:
            continue
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


def _role_id_to_token(role_id_by_value: dict, value_to_token: dict) -> dict:
    """Invert ``resolve_platform_sp``'s ``{value: id}`` into ``{id: token}`` for the known roles."""
    return {rid: value_to_token[value] for value, rid in role_id_by_value.items() if value in value_to_token}


def _platform_user_from_assignment(assignment: dict, role_id_to_token: dict) -> PlatformUser:
    return PlatformUser(
        principal_id=assignment.get("principalId") or "",
        display_name=assignment.get("principalDisplayName") or "",
        principal_type=assignment.get("principalType") or "",
        role=role_id_to_token.get(assignment.get("appRoleId"), ""),
    )


@router.get("", response_model=List[PlatformUser])
async def list_platform_users(
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """List the platform's role-holders: the Platform.* assignments on the platform SP."""
    from api.routes.grants import get_graph_service

    _, value_to_token = _platform_role_maps()
    role_id_by_value, assignments = await platform_role_holders(
        get_graph_service(), _platform_app_client_id()
    )
    return group_platform_users(assignments, _role_id_to_token(role_id_by_value, value_to_token))


@router.post("", response_model=PlatformUser, status_code=201)
async def add_platform_user(
    body: RoleAssignmentCreate,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Onboard an existing directory principal by assigning a platform role."""
    from api.routes.grants import get_graph_service

    token_to_value, value_to_token = _platform_role_maps()
    assignment, role_id_by_value = await assign_platform_role(
        get_graph_service(), _platform_app_client_id(), body.principal_id, token_to_value[body.role]
    )
    return _platform_user_from_assignment(assignment, _role_id_to_token(role_id_by_value, value_to_token))


@router.put("/{principal_id}/role", response_model=PlatformUser)
async def change_platform_role(
    principal_id: str,
    body: RoleChange,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Set a principal's platform role to exactly one: assign the new role, then revoke the others.

    The ordering and its failure meanings are ``replace_platform_role``'s; a principal that already
    holds the target role answers the same 409 as onboarding.
    """
    from api.routes.grants import get_graph_service

    token_to_value, value_to_token = _platform_role_maps()
    assignment, role_id_by_value = await replace_platform_role(
        get_graph_service(),
        _platform_app_client_id(),
        principal_id,
        token_to_value[body.role],
        value_to_token,
    )
    return _platform_user_from_assignment(assignment, _role_id_to_token(role_id_by_value, value_to_token))


@router.delete("/{principal_id}", status_code=204)
async def remove_platform_user(
    principal_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Revoke every Platform.* assignment the principal holds, answering 204; none held is a 404."""
    from api.routes.grants import get_graph_service

    _, value_to_token = _platform_role_maps()
    await revoke_platform_roles(get_graph_service(), _platform_app_client_id(), principal_id, value_to_token)
    return Response(status_code=204)
