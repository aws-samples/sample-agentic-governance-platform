"""Shared user → agent grant application (Epic 33, Task T3).

The E6-shaped twin of ``services/agent_mcp_grant.py``. Where that module grants an AGENT
access to an MCP, this one grants a HUMAN USER access to an AGENT — the same Entra
app-role assignment the live E6 route ``POST /agents/{id}/grants``
(``api.routes.grants.create_grant``) writes, lifted into a reusable service function so the
marketplace agent-subscription approval applies REAL access instead of only flipping a
status.

It is deliberately much SMALLER than the MCP twin: there is no OBO consent to wire, no
per-agent credential provider, and no runtime env to rebuild. A user→agent grant is exactly
ONE Graph write:

  ``assign_app_role(agent.entra_sp_id, user_oid, agent.invoker_role_id)``

⚠️ ARGUMENT ORDER / DIRECTION (the inverse of the MCP grant, and the easy bug): here the
agent SP is the **RESOURCE** and the user oid is the **PRINCIPAL**. In the agent→MCP grant
the agent SP is the principal and the MCP SP is the resource. ``GraphService``'s first
parameter is named ``agent_sp_id`` in both cases but always means the RESOURCE SP whose
``appRoleAssignedTo`` collection is written.

Role: **Invoker only**. A marketplace subscription grants the ability to INVOKE the product,
never to administer it, so ``admin_role_id`` is never assigned on this path (contrast the E6
route, which lets an operator pick a role).

Returns the Entra app-role assignment id, which the caller PERSISTS (the marketplace stores
it as ``grant_assignment_id``) — it is the only handle by which the grant can later be
revoked.

IDEMPOTENT: a Graph "already assigned" (a ``GraphError`` with status 400/409 from
``assign_app_role``) is recovered — the function re-lists the agent's assignments and returns
the EXISTING one matching this ``principalId`` + ``appRoleId``. That matters because a user
may already hold Invoker via a direct E6 grant, and a re-approval (or a retry after a
partial failure) must not fail just because the access already exists. If the recovery cannot
resolve an id (no match, a match carrying no ``id``, OR the re-list itself fails) it raises
``UserGrantError`` rather
than returning ``""`` — an empty id would be persisted as a grant that revoke could never
tear down. The SAME invariant governs the happy path: an assign response carrying no
``id`` raises ``UserGrantError`` too, rather than handing the caller an unrevokable grant.

SECURITY (T-GRAPH carry-forward): this module NEVER logs a token or secret, and every raised
exception carries a FIXED literal message — never ``str(graph_err)`` and never the Graph
``error.message``, because a ``GraphError`` from a resource endpoint carries a server-authored
message that callers may surface as an HTTP detail. The ``logger.exception`` seams emit a
traceback only (Python tracebacks never print local VALUES, so the user oid / assignment ids
in scope cannot leak through them). HTTP concerns stay OUT of this module: it raises the two
domain exceptions below and the caller maps them (the marketplace service → ``grant_failed``
→ 502).

The Graph accessor is resolved via ``api.routes.grants.get_graph_service`` — the ONE
``GraphService`` singleton the whole app shares (``grants.py`` owns it; ``routes/agents.py``
imports it too) — imported AT CALL TIME. That deferral is the ``agent_mcp_grant``
non-circular idiom: the ROUTE imports this module at module level, so a module-level import
of the route here would couple import order.
"""

import logging

from services.graph_service import GraphError

logger = logging.getLogger(__name__)

# FIXED safe literals (the T-GRAPH no-leak guard). The caller may surface these; a Graph
# ``error.message`` must never reach one.
_ASSIGN_FAILED_MSG = "failed to grant the user access to the agent"
_RECOVERY_FAILED_MSG = (
    "the user already holds access to the agent but the existing grant could not be read"
)
_REVOKE_FAILED_MSG = "failed to revoke the user's access to the agent"
_REVOKE_NOT_FOUND_MSG = "grant not found"


# --- domain exceptions --------------------------------------------------------
# SIBLINGS, deliberately (C4): ``UserGrantNotFoundError`` is NOT a subclass of
# ``UserGrantError``. A revoke-time not-found means the assignment is ALREADY gone, which is
# SUCCESS for callers — making it a subclass would let a caller's ``except UserGrantError``
# failure branch swallow a success, or an ``except UserGrantNotFoundError`` success branch
# swallow a real failure, depending on clause order. Keeping them siblings makes the
# marketplace service's two except-clauses order-independent.

class UserGrantError(Exception):
    """A non-idempotent Graph failure applying or revoking the user→agent grant.

    Carries a SAFE, FIXED message. The caller maps it (the marketplace service → a
    ``grant_failed`` MarketplaceError → 502, with the subscription persisted FAILED so the
    row offers Retry)."""


class UserGrantNotFoundError(Exception):
    """The app-role assignment is already gone (a Graph 404 on ``revoke_app_role`` — the
    stale record / double-click race). Callers treat this as SUCCESS: the desired end state
    (no access) already holds, so there is nothing to retry."""


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
    # Deferred import of the singleton accessor (the SAME one the E6 route uses). Deferred
    # to call time so this module does NOT import the route module at load — the route
    # imports THIS module (the correct, non-circular direction).
    from api.routes.grants import get_graph_service

    graph = get_graph_service()
    app_role_id = agent.invoker_role_id

    try:
        assignment = await graph.assign_app_role(
            # RESOURCE = the agent SP, PRINCIPAL = the human user (see the module docstring).
            agent.entra_sp_id, user_oid, app_role_id
        )
        assignment_id = assignment.get("id") or ""
        if not assignment_id:
            # Graph accepted the write but named no assignment. Do NOT return "": the
            # caller persists it as ``grant_assignment_id`` and its revoke guard treats a
            # falsy id as "nothing to revoke", so the row would later flip to REVOKED
            # while the user KEPT the Invoker role — silently. Failing closed is strictly
            # better: a FAILED row offers Retry, and the retry's 400/409 recovery resolves
            # the real id of whatever assignment Graph did create. (Same invariant the
            # recovery path below enforces.)
            logger.warning(
                "[user_grant] assign returned no assignment id for agent %s (user %s)",
                getattr(agent, "id", "?"),
                user_oid,
            )
            raise UserGrantError(_ASSIGN_FAILED_MSG)
        return assignment_id
    except GraphError as err:
        if err.status not in (400, 409):
            # A real (non-idempotent) Graph failure. Log the traceback only — never the
            # exception VALUE, whose Graph message could carry a resource detail.
            logger.exception(
                "[user_grant] assign failed for agent %s (user %s)",
                getattr(agent, "id", "?"),
                user_oid,
            )
            raise UserGrantError(_ASSIGN_FAILED_MSG) from err

        # Already assigned (or a duplicate) → recover the EXISTING assignment id from Graph
        # rather than failing, so a re-approval / retry is idempotent.
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
                # A MATCH that carries no id is not a resolution: returning "" here would
                # break the very invariant this recovery exists to protect. Fall through to
                # the raise below (a later entry may still name the assignment).

        # No usable match: Graph said "already assigned" but we cannot name the assignment
        # (no matching entry, or a match with no ``id``). Do NOT return "" — an empty id
        # would persist as an unrevokable grant.
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
            # Already revoked (stale record / double-click) — the assignment is gone.
            raise UserGrantNotFoundError(_REVOKE_NOT_FOUND_MSG) from err
        logger.exception(
            "[user_grant] revoke failed for agent %s (assignment %s)",
            getattr(agent, "id", "?"),
            assignment_id,
        )
        raise UserGrantError(_REVOKE_FAILED_MSG) from err
    return None
