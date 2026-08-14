"""Shared user→agent grant function tests (Epic 33, Task T3).

``services.agent_user_grant`` is the E6-shaped twin of ``services.agent_mcp_grant``: it
applies the REAL user→agent Entra app-role assignment (the same write the E6 route
``POST /agents/{id}/grants`` performs) so the marketplace agent-subscription approval
grants actual access instead of only flipping a status. These tests pin its contract:

  - ``apply_user_agent_grant`` assigns the agent's ``invoker_role_id`` to the user oid on
    the agent's SP and returns the Entra assignment id;
  - idempotent: a Graph "already assigned" (400/409) ⇒ recover the EXISTING assignment id
    by re-listing and matching ``principalId`` + ``appRoleId``;
  - a recovery that cannot resolve the id (no match, or the re-list itself fails) ⇒
    ``UserGrantError`` (never a raw GraphError escaping the module);
  - a non-idempotent Graph failure (5xx) ⇒ ``UserGrantError`` (callers map to 502);
  - ``revoke_user_agent_grant`` deletes the assignment; a Graph 404 ⇒
    ``UserGrantNotFoundError`` (= success for callers), any other GraphError ⇒
    ``UserGrantError``.

The Graph singleton is resolved via ``api.routes.grants.get_graph_service`` AT CALL TIME
(the ``agent_mcp_grant`` non-circular idiom), so it is patched at THAT source module and
NO live Graph is touched.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.agent_user_grant import (
    UserGrantError,
    UserGrantNotFoundError,
    apply_user_agent_grant,
    revoke_user_agent_grant,
)
from services.graph_service import GraphError

# The repo is NOT in pytest-asyncio ``auto`` mode, so every async test is decorated
# explicitly (same as ``test_agent_mcp_grant.py``).

USER_OID = "user-oid-alice"


def _make_agent(**overrides):
    """A provisioned agent with the fields the grant body reads."""
    base = dict(
        id="agent-123",
        name="claims-triage-de",
        entra_sp_id="agent-sp-obj-id",
        invoker_role_id="role-invoker-guid",
        admin_role_id="role-admin-guid",
        identity_status="provisioned",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _patch_graph(graph):
    """Patch the ONE Graph accessor the module resolves at call time.

    ``agent_user_grant`` imports ``api.routes.grants.get_graph_service`` INSIDE the
    function (deferred, the non-circular direction), so the patch must target that
    source module — patching a module-level name in ``agent_user_grant`` would miss it.
    """
    return patch("api.routes.grants.get_graph_service", return_value=graph)


# --------------------------------------------------------------------------- #
# apply_user_agent_grant
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_apply_assigns_invoker_role_and_returns_assignment_id():
    """The happy path: assign the agent's INVOKER role to the user oid on the agent's SP
    and return the new assignment id.

    Note the E6 argument order (``graph_service.assign_app_role(agent_sp_id,
    principal_id, app_role_id)``): the RESOURCE is the agent SP and the PRINCIPAL is the
    human user — the inverse of the agent→MCP grant, where the principal is an agent SP.
    """
    agent = _make_agent()
    graph = MagicMock()
    graph.assign_app_role = AsyncMock(return_value={"id": "assign-new"})

    with _patch_graph(graph):
        assignment_id = await apply_user_agent_grant(agent, USER_OID)

    assert assignment_id == "assign-new"
    graph.assign_app_role.assert_awaited_once_with(
        "agent-sp-obj-id", USER_OID, "role-invoker-guid"
    )
    # The Admin role is NEVER assigned by this path — a marketplace subscription grants
    # invoke access only.
    assert graph.assign_app_role.await_args.args[2] != agent.admin_role_id


@pytest.mark.parametrize("status", [400, 409])
@pytest.mark.asyncio
async def test_apply_already_assigned_recovers_existing_assignment_id(status):
    """IDEMPOTENT: an already-assigned (Graph 400/409) recovers the EXISTING assignment
    id by re-listing the agent's assignments and matching principalId + appRoleId — a
    re-approval must not fail just because the access already exists."""
    agent = _make_agent()
    graph = MagicMock()
    graph.assign_app_role = AsyncMock(
        side_effect=GraphError(status, "Request_BadRequest")
    )
    graph.list_assignments = AsyncMock(
        return_value=[
            # A different principal holding the same role → must not match.
            {"id": "assign-other", "principalId": "someone-else",
             "appRoleId": "role-invoker-guid"},
            # The same principal holding a DIFFERENT role → must not match.
            {"id": "assign-admin", "principalId": USER_OID,
             "appRoleId": "role-admin-guid"},
            # The real one: same principal AND same role.
            {"id": "assign-existing", "principalId": USER_OID,
             "appRoleId": "role-invoker-guid"},
        ]
    )

    with _patch_graph(graph):
        assignment_id = await apply_user_agent_grant(agent, USER_OID)

    assert assignment_id == "assign-existing"
    graph.list_assignments.assert_awaited_once_with("agent-sp-obj-id")


@pytest.mark.asyncio
async def test_apply_recovery_without_a_match_raises_user_grant_error():
    """A 400/409 whose recovery finds NO matching assignment cannot return an id — it
    raises ``UserGrantError`` rather than returning "" (an empty id would be persisted as
    a grant that revoke could never tear down)."""
    agent = _make_agent()
    graph = MagicMock()
    graph.assign_app_role = AsyncMock(side_effect=GraphError(409, "Conflict"))
    graph.list_assignments = AsyncMock(
        return_value=[{"id": "assign-other", "principalId": "someone-else",
                       "appRoleId": "role-invoker-guid"}]
    )

    with _patch_graph(graph):
        with pytest.raises(UserGrantError):
            await apply_user_agent_grant(agent, USER_OID)


@pytest.mark.parametrize("matched", [
    {"principalId": USER_OID, "appRoleId": "role-invoker-guid"},              # no id key
    {"id": "", "principalId": USER_OID, "appRoleId": "role-invoker-guid"},    # empty id
])
@pytest.mark.asyncio
async def test_apply_recovery_match_without_an_id_raises_user_grant_error(matched):
    """A MATCHED assignment carrying no usable ``id`` is not a resolution: returning "" would
    persist as a grant revoke could never tear down (the caller's revoke guard reads a falsy id
    as "nothing to revoke", so the row would flip to REVOKED while the user KEPT Invoker).
    Fails closed with the same fixed literal as the no-match branch."""
    agent = _make_agent()
    graph = MagicMock()
    graph.assign_app_role = AsyncMock(side_effect=GraphError(409, "Conflict"))
    graph.list_assignments = AsyncMock(return_value=[matched])

    with _patch_graph(graph):
        with pytest.raises(UserGrantError):
            await apply_user_agent_grant(agent, USER_OID)


@pytest.mark.asyncio
async def test_apply_recovery_idless_match_does_not_hide_a_later_usable_match():
    """Falling through an id-less match must not abandon the recovery: a LATER entry naming
    the same (principal, role) still resolves, so the fix fails closed without becoming
    stricter than the idempotency contract."""
    agent = _make_agent()
    graph = MagicMock()
    graph.assign_app_role = AsyncMock(side_effect=GraphError(409, "Conflict"))
    graph.list_assignments = AsyncMock(return_value=[
        {"principalId": USER_OID, "appRoleId": "role-invoker-guid"},
        {"id": "assign-existing", "principalId": USER_OID, "appRoleId": "role-invoker-guid"},
    ])

    with _patch_graph(graph):
        assert await apply_user_agent_grant(agent, USER_OID) == "assign-existing"


@pytest.mark.asyncio
async def test_apply_recovery_read_failure_raises_user_grant_error():
    """When the recovery RE-LIST itself fails, the module still raises its own typed
    error — a raw GraphError must never escape (the caller maps typed errors only)."""
    agent = _make_agent()
    graph = MagicMock()
    graph.assign_app_role = AsyncMock(side_effect=GraphError(400, "Request_BadRequest"))
    graph.list_assignments = AsyncMock(side_effect=GraphError(503, "ServiceUnavailable"))

    with _patch_graph(graph):
        with pytest.raises(UserGrantError):
            await apply_user_agent_grant(agent, USER_OID)


@pytest.mark.asyncio
async def test_apply_other_graph_failure_raises_user_grant_error():
    """A non-idempotent Graph failure (5xx) is a real failure → ``UserGrantError`` so the
    caller persists FAILED + offers Retry (never silently "granted")."""
    agent = _make_agent()
    graph = MagicMock()
    graph.assign_app_role = AsyncMock(side_effect=GraphError(502, "BadGateway"))
    graph.list_assignments = AsyncMock(return_value=[])

    with _patch_graph(graph):
        with pytest.raises(UserGrantError):
            await apply_user_agent_grant(agent, USER_OID)

    # A 5xx is NOT an already-assigned, so no recovery read is attempted.
    graph.list_assignments.assert_not_awaited()


@pytest.mark.parametrize(
    "response",
    [
        pytest.param({}, id="no-id-key"),
        pytest.param({"id": ""}, id="empty-id"),
    ],
)
@pytest.mark.asyncio
async def test_apply_without_an_assignment_id_raises_user_grant_error(response):
    """An assign response that names no assignment must NOT yield "": the caller persists
    the returned value as ``grant_assignment_id`` and its revoke guard treats a falsy id as
    "nothing to revoke", so a later revoke would flip the row to REVOKED while the user KEPT
    the Invoker role — silently. Fail closed instead: a FAILED row offers Retry, whose
    400/409 recovery resolves the real id. Same invariant the recovery path enforces."""
    agent = _make_agent()
    graph = MagicMock()
    graph.assign_app_role = AsyncMock(return_value=response)

    with _patch_graph(graph):
        with pytest.raises(UserGrantError):
            await apply_user_agent_grant(agent, USER_OID)


@pytest.mark.asyncio
async def test_apply_error_message_never_leaks_the_graph_detail():
    """T-GRAPH no-leak guard: the raised message is a FIXED literal, never ``str(err)`` /
    the Graph ``error.message`` (which reaches the client as an error detail)."""
    agent = _make_agent()
    graph = MagicMock()
    graph.assign_app_role = AsyncMock(
        side_effect=GraphError(500, "InternalServerError", "secret-ish resource detail")
    )

    with _patch_graph(graph):
        with pytest.raises(UserGrantError) as ei:
            await apply_user_agent_grant(agent, USER_OID)

    assert "secret-ish resource detail" not in str(ei.value)
    assert "InternalServerError" not in str(ei.value)


# --------------------------------------------------------------------------- #
# revoke_user_agent_grant
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_revoke_deletes_the_assignment():
    agent = _make_agent()
    graph = MagicMock()
    graph.revoke_app_role = AsyncMock(return_value=None)

    with _patch_graph(graph):
        result = await revoke_user_agent_grant(agent, "assign-existing")

    assert result is None
    graph.revoke_app_role.assert_awaited_once_with("agent-sp-obj-id", "assign-existing")


@pytest.mark.asyncio
async def test_revoke_missing_assignment_raises_not_found():
    """A Graph 404 means the assignment is ALREADY gone (the stale / double-click race) →
    ``UserGrantNotFoundError``, which callers treat as success."""
    agent = _make_agent()
    graph = MagicMock()
    graph.revoke_app_role = AsyncMock(side_effect=GraphError(404, "ResourceNotFound"))

    with _patch_graph(graph):
        with pytest.raises(UserGrantNotFoundError):
            await revoke_user_agent_grant(agent, "assign-gone")


@pytest.mark.asyncio
async def test_revoke_other_graph_failure_raises_user_grant_error():
    """Any other GraphError is a REAL teardown failure → ``UserGrantError``, so the
    caller leaves the subscription APPROVED and the admin can retry."""
    agent = _make_agent()
    graph = MagicMock()
    graph.revoke_app_role = AsyncMock(side_effect=GraphError(502, "BadGateway"))

    with _patch_graph(graph):
        with pytest.raises(UserGrantError) as ei:
            await revoke_user_agent_grant(agent, "assign-existing")

    # Not the not-found subtype — a real failure must not be mistaken for success.
    assert not isinstance(ei.value, UserGrantNotFoundError)


@pytest.mark.asyncio
async def test_not_found_is_not_a_user_grant_error_subclass():
    """C4 pins the two exceptions as SIBLINGS: a caller that catches ``UserGrantError``
    for the failure path must NOT accidentally swallow the not-found (= success) case,
    and vice versa. This ordering-independence is what makes the service's
    except-clauses safe."""
    assert not issubclass(UserGrantNotFoundError, UserGrantError)
    assert not issubclass(UserGrantError, UserGrantNotFoundError)
