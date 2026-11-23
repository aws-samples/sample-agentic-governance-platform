"""Grant or revoke a human user's Invoker role on an agent's service principal.

The user-to-agent twin of ``agent_mcp_grant``: one Graph write,
``assign_app_role(agent.entra_sp_id, user_oid, agent.invoker_role_id)``, where the agent's
service principal is the resource and the user is the principal, the inverse of the agent-to-MCP
grant. Only the Invoker role is ever assigned. The assignment id returned is the caller's only
handle to revoke.

Idempotent: a Graph "already assigned" (400 or 409) is recovered by re-listing the agent's
assignments and returning the existing id; a recovery that cannot name an id raises
``UserGrantError`` rather than returning an empty id the caller would persist as an unrevokable
grant, and so does an assign response naming no id. Every raised message is a fixed literal,
never a Graph message. The Graph singleton is imported at call time from ``api.routes.grants``,
which imports this module.
"""

import logging

from services.graph_errors import GraphError

logger = logging.getLogger(__name__)

_ASSIGN_FAILED_MSG = "failed to grant the user access to the agent"
_RECOVERY_FAILED_MSG = "the user already holds access to the agent but the existing grant could not be read"
_REVOKE_FAILED_MSG = "failed to revoke the user's access to the agent"
_REVOKE_NOT_FOUND_MSG = "grant not found"


class UserGrantError(Exception):
    """A non-idempotent Graph failure applying or revoking the grant; the message is a fixed literal.
    Deliberately not the parent of ``UserGrantNotFoundError``, so a caller's two except clauses are
    order-independent (``test_agent_user_grant`` pins it).
    """


class UserGrantNotFoundError(Exception):
    """The app-role assignment is already gone (a Graph 404 on ``revoke_app_role``); callers treat it
    as success, since the desired end state already holds.
    """


async def apply_user_agent_grant(agent, user_oid: str) -> str:
    """Grant ``user_oid`` the Invoker app role on ``agent``'s service principal and return
    the Entra app-role assignment id.

    See the module docstring for the direction/argument-order warning, the idempotency
    contract, and the security rules. Raises ``UserGrantError`` on any failure the caller
    must surface (including a 400/409 whose existing-assignment recovery could not resolve
    an id). The caller is responsible for checking that the agent is provisioned
    (``identity_status == "provisioned"`` + ``entra_sp_id`` + ``invoker_role_id``) BEFORE
    calling — this function does not re-validate the record.
    """
    from api.routes.grants import get_graph_service

    graph = get_graph_service()
    app_role_id = agent.invoker_role_id

    try:
        assignment = await graph.assign_app_role(agent.entra_sp_id, user_oid, app_role_id)
        assignment_id = assignment.get("id") or ""
        if not assignment_id:
            logger.warning(
                "[user_grant] assign returned no assignment id for agent %s (user %s)",
                getattr(agent, "id", "?"),
                user_oid,
            )
            raise UserGrantError(_ASSIGN_FAILED_MSG)
        return assignment_id
    except GraphError as err:
        if err.status not in (400, 409):
            logger.exception(
                "[user_grant] assign failed for agent %s (user %s)",
                getattr(agent, "id", "?"),
                user_oid,
            )
            raise UserGrantError(_ASSIGN_FAILED_MSG) from err

        try:
            existing = await graph.list_assignments(agent.entra_sp_id)
        except GraphError as read_err:
            logger.exception(
                "[user_grant] already-assigned recovery read failed for agent %s (user %s)",
                getattr(agent, "id", "?"),
                user_oid,
            )
            raise UserGrantError(_RECOVERY_FAILED_MSG) from read_err

        for a in existing:
            if a.get("principalId") == user_oid and a.get("appRoleId") == app_role_id:
                aid = a.get("id")
                if aid:
                    return aid

        logger.warning(
            "[user_grant] agent %s reported an existing grant for user %s but no matching "
            "assignment with an id was found",
            getattr(agent, "id", "?"),
            user_oid,
        )
        raise UserGrantError(_RECOVERY_FAILED_MSG) from err


async def revoke_user_agent_grant(agent, assignment_id: str) -> None:
    """Revoke app-role assignment ``assignment_id`` on ``agent``'s service principal.

    The kill switch for a marketplace agent subscription. A Graph 404 raises
    ``UserGrantNotFoundError`` (the assignment is already gone — SUCCESS for callers); any
    other ``GraphError`` raises ``UserGrantError`` so the caller can leave the subscription
    APPROVED and let the admin retry. Returns None on success.

    Unlike the MCP twin there is NO multiplicity guard and no consent teardown: a user→agent
    grant is a single assignment with nothing shared behind it, so deleting it fully removes
    the access it conferred.
    """
    from api.routes.grants import get_graph_service

    graph = get_graph_service()
    try:
        await graph.revoke_app_role(agent.entra_sp_id, assignment_id)
    except GraphError as err:
        if err.status == 404:
            raise UserGrantNotFoundError(_REVOKE_NOT_FOUND_MSG) from err
        logger.exception(
            "[user_grant] revoke failed for agent %s (assignment %s)",
            getattr(agent, "id", "?"),
            assignment_id,
        )
        raise UserGrantError(_REVOKE_FAILED_MSG) from err
    return None
