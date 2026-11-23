"""The platform role-holder functions: the users-admin surface's Graph sequences over a ``GraphService``
passed as ``graph``.

``platform_role_holders`` and its three siblings each turn a ``GraphError`` into the ``PlatformUsersError``
kind that names what the sequence was doing.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING, ClassVar

from core.errors import AppError, Outcome
from services.graph_errors import GraphError

if TYPE_CHECKING:
    from services.graph_service import GraphService


class PlatformUsersError(AppError):
    """A platform role-holder operation failed: a Graph call refused, or the role is not on the app.

    Every detail is a fixed literal; the message is the same literal, so logs and the wire agree.
    """

    domain: ClassVar[str] = "platform_users"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "read_failed": Outcome(502, "Failed to read platform users"),
        "role_not_configured": Outcome(502, "Platform role is not configured"),
        "already_has_role": Outcome(409, "principal already has a platform role"),
        "assign_failed": Outcome(502, "Failed to assign the platform role"),
        "change_failed": Outcome(502, "Failed to change the platform role"),
        "no_platform_role": Outcome(404, "user has no platform role to remove"),
        "remove_failed": Outcome(502, "Failed to remove platform access"),
    }

    def __init__(self, *, kind: str) -> None:
        detail = type(self).kinds[kind].detail
        super().__init__(detail if isinstance(detail, str) else kind, kind)


def platform_assignment_ids_for(
    assignments: list,
    principal_id: str,
    platform_role_ids: Collection[str],
    exclude_app_role_id: str | None = None,
) -> list:
    """The ids of ``principal_id``'s assignments whose appRoleId is a platform role.

    Foreign roles are skipped. ``exclude_app_role_id`` keeps one role out of the list: the role a
    change just assigned, so the revoke of the principal's other platform roles never touches it.
    """
    return [
        a["id"]
        for a in assignments
        if a.get("principalId") == principal_id
        and a.get("appRoleId") in platform_role_ids
        and a.get("appRoleId") != exclude_app_role_id
        and a.get("id")
    ]


def _platform_role_ids(
    role_id_by_value: Mapping[str, str], platform_role_values: Collection[str]
) -> set[str]:
    return {rid for value, rid in role_id_by_value.items() if value in platform_role_values}


async def platform_role_holders(graph: GraphService, client_id: str) -> tuple[dict[str, str], list[dict]]:
    """The platform app's appRole value-to-id map and every assignment on its SP.

    Any ``GraphError`` is ``read_failed``.
    """
    try:
        sp_id, role_id_by_value = await graph.resolve_platform_sp(client_id)
        assignments = await graph.list_assignments(sp_id)
    except GraphError as err:
        raise PlatformUsersError(kind="read_failed") from err
    return role_id_by_value, assignments


async def assign_platform_role(
    graph: GraphService, client_id: str, principal_id: str, role_value: str
) -> tuple[dict, dict[str, str]]:
    """Assign the platform appRole whose value is ``role_value`` to the principal.

    Returns the new assignment and the role value-to-id map. A role the app does not define is
    ``role_not_configured``; a Graph 400 or 409 on the assign is ``already_has_role``; any other
    ``GraphError`` is ``assign_failed``.
    """
    try:
        sp_id, role_id_by_value = await graph.resolve_platform_sp(client_id)
        app_role_id = role_id_by_value.get(role_value)
        if not app_role_id:
            raise PlatformUsersError(kind="role_not_configured")
        assignment = await graph.assign_app_role(sp_id, principal_id, app_role_id)
    except GraphError as err:
        if err.status in (400, 409):
            raise PlatformUsersError(kind="already_has_role") from err
        raise PlatformUsersError(kind="assign_failed") from err
    return assignment, role_id_by_value


async def replace_platform_role(
    graph: GraphService,
    client_id: str,
    principal_id: str,
    role_value: str,
    platform_role_values: Collection[str],
) -> tuple[dict, dict[str, str]]:
    """Leave the principal with exactly one platform role: assign the new one, then revoke the others.

    Assign-first is the safer order: a failure after the assign leaves the principal with the new
    role plus possibly a stale one, which ``group_platform_users`` resolves to the highest and a
    repeated change converges, never with no access. ``platform_role_values`` names the appRole
    values that count as platform roles, so a foreign role is never revoked. A role the app does
    not define is ``role_not_configured``; a Graph 400 or 409 on the assign is ``already_has_role``
    (the principal already holds the target role); any other ``GraphError`` is ``change_failed``.
    """
    try:
        sp_id, role_id_by_value = await graph.resolve_platform_sp(client_id)
        app_role_id = role_id_by_value.get(role_value)
        if not app_role_id:
            raise PlatformUsersError(kind="role_not_configured")
        assignment = await graph.assign_app_role(sp_id, principal_id, app_role_id)
        assignments = await graph.list_assignments(sp_id)
        others = platform_assignment_ids_for(
            assignments,
            principal_id,
            _platform_role_ids(role_id_by_value, platform_role_values),
            exclude_app_role_id=app_role_id,
        )
        for assignment_id in others:
            await graph.revoke_app_role(sp_id, assignment_id)
    except GraphError as err:
        if err.status in (400, 409):
            raise PlatformUsersError(kind="already_has_role") from err
        raise PlatformUsersError(kind="change_failed") from err
    return assignment, role_id_by_value


async def revoke_platform_roles(
    graph: GraphService, client_id: str, principal_id: str, platform_role_values: Collection[str]
) -> None:
    """Revoke every platform role the principal holds on the platform app.

    A principal holding none is ``no_platform_role``; any ``GraphError`` is ``remove_failed``.
    """
    try:
        sp_id, role_id_by_value = await graph.resolve_platform_sp(client_id)
        assignments = await graph.list_assignments(sp_id)
        held = platform_assignment_ids_for(
            assignments, principal_id, _platform_role_ids(role_id_by_value, platform_role_values)
        )
        if not held:
            raise PlatformUsersError(kind="no_platform_role")
        for assignment_id in held:
            await graph.revoke_app_role(sp_id, assignment_id)
    except GraphError as err:
        raise PlatformUsersError(kind="remove_failed") from err
