"""Tests for ``services.agent_mcp_grant``, the grant body shared by the grants route and the
marketplace.

They pin the contract of ``apply_agent_mcp_grant``: call order (``assign_app_role`` ->
``grant_agent_obo_consent`` -> ``ensure_agent_credential_provider`` -> the runtime env rebuild);
the Entra assignment id as the return value; idempotency (a Graph "already assigned" recovers the
existing assignment id and still completes the consent); role ``Admin`` using the MCP's
``admin_role_id``; a non-idempotent Graph failure raising ``GrantRevokeError.assign_failed``,
never swallowed. The revoke tests pin the kill-switch sequence, the multiplicity guard and the
typed-exception contract. The singletons are patched so no live Graph or AWS is touched.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.agent_mcp_grant as agent_mcp_grant
from services.agent_mcp_grant import GrantRevokeError


def _make_mcp(**overrides):
    """A provisioned gateway MCP with the fields the grant body reads."""
    base: dict[str, Any] = dict(
        id="mcp-123",
        name="claims-mcp-de",
        entra_sp_id="mcp-sp-obj-id",
        invoker_role_id="role-invoker-guid",
        admin_role_id="role-admin-guid",
        gateway_url="https://gw-abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
        entra_app_audience="api://agp-mcp-mcp-123",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_agent(**overrides):
    """A provisioned agent with the fields the grant body reads."""
    base: dict[str, Any] = dict(
        id="agent-123",
        name="claims-triage-de",
        entra_sp_id="agent-sp-obj-id",
        entra_app_id="agent-app-id",
        agent_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/agent-abc123",
        oauth2_credential_provider_name=None,
        mcp_server_ids=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _patch_singletons(*, graph, cred_svc, identity_svc, agent_registry):
    """Patch the four accessors the grant body reads.

    ``agent_mcp_grant`` reuses the SAME singleton accessors the route used — it imports them
    (deferred, at call time, to avoid a circular import) from ``api.routes.mcp_server_grants``:
    ``get_mcp_graph_service`` / ``get_agent_credential_service`` / ``get_agent_identity_service``.
    Because the import is deferred, patch them at THAT source module (so the function picks up
    the doubles when it imports). It resolves the agent registry via ``api.routes.agents.get_service``
    only to persist a changed provider name (same as the route).
    """
    return [
        patch("api.routes.mcp_server_grants.get_mcp_graph_service", return_value=graph),
        patch(
            "api.routes.mcp_server_grants.get_agent_credential_service",
            return_value=cred_svc,
        ),
        patch(
            "api.routes.mcp_server_grants.get_agent_identity_service",
            return_value=identity_svc,
        ),
        patch("api.routes.agents.get_service", return_value=agent_registry),
    ]


def _enter(patches):
    return [p.__enter__() for p in patches]


def _exit(patches):
    for p in reversed(patches):
        p.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_apply_grant_calls_in_order():
    """Provisioned agent + mcp → the grant seams fire in the pinned order with the pinned
    args; the function returns the assignment id from ``assign_app_role``.

    The final seam is ``rebuild_runtime_mcp_env(agent)`` (rebuild the full
    runtime env from the desired-state set), NOT the old single-MCP
    ``set_runtime_environment`` call — this service no longer constructs the env dict (that
    moved to ``agent_mcp_env.rebuild_runtime_mcp_env``)."""
    from services.agent_mcp_grant import apply_agent_mcp_grant

    agent = _make_agent()
    mcp = _make_mcp()

    graph = MagicMock()
    graph.assign_app_role = AsyncMock(return_value={"id": "assign-new", "principalId": "agent-sp-obj-id"})
    graph.grant_agent_obo_consent = AsyncMock(return_value=None)
    cred_svc = MagicMock()
    cred_svc._region = "us-east-1"
    cred_svc.ensure_agent_credential_provider = AsyncMock(return_value="agp-agent-obo-agent-123")
    identity_svc = MagicMock()
    agent_registry = MagicMock()
    agent_registry.list.return_value = [agent]
    agent_registry.persist_identity.return_value = None

    rebuild = AsyncMock()

    manager = MagicMock()
    manager.attach_mock(graph.assign_app_role, "assign")
    manager.attach_mock(graph.grant_agent_obo_consent, "consent")
    manager.attach_mock(cred_svc.ensure_agent_credential_provider, "ensure_provider")
    manager.attach_mock(rebuild, "rebuild_env")

    patches = _patch_singletons(
        graph=graph, cred_svc=cred_svc, identity_svc=identity_svc, agent_registry=agent_registry
    )
    _enter(patches)
    try:
        with patch.object(agent_mcp_grant, "rebuild_runtime_mcp_env", rebuild):
            assignment_id = await apply_agent_mcp_grant(agent, mcp, role="Invoker")
    finally:
        _exit(patches)

    assert assignment_id == "assign-new"
    graph.assign_app_role.assert_awaited_once_with("mcp-sp-obj-id", "agent-sp-obj-id", "role-invoker-guid")
    graph.grant_agent_obo_consent.assert_awaited_once_with("agent-sp-obj-id", "mcp-sp-obj-id")
    cred_svc.ensure_agent_credential_provider.assert_awaited_once_with(agent)
    rebuild.assert_awaited_once_with(agent)
    identity_svc.set_runtime_environment.assert_not_called()
    order = [
        c[0] for c in manager.mock_calls if c[0] in ("assign", "consent", "ensure_provider", "rebuild_env")
    ]
    assert order == ["assign", "consent", "ensure_provider", "rebuild_env"]


@pytest.mark.asyncio
async def test_apply_grant_idempotent_already_assigned():
    """``assign_app_role`` raises the Graph already-assigned error (400/409) → the function
    recovers the EXISTING assignment id (via ``list_assignments``, mirroring the route's
    recovery intent) and STILL completes ``grant_agent_obo_consent``."""
    from services.agent_mcp_grant import apply_agent_mcp_grant
    from services.graph_errors import GraphError

    agent = _make_agent()
    mcp = _make_mcp()

    graph = MagicMock()
    graph.assign_app_role = AsyncMock(side_effect=GraphError(409, "Request_BadRequest"))
    graph.list_assignments = AsyncMock(
        return_value=[
            {
                "id": "assign-existing",
                "principalId": "agent-sp-obj-id",
                "appRoleId": "role-invoker-guid",
            },
            {
                "id": "assign-other",
                "principalId": "some-other-sp",
                "appRoleId": "role-invoker-guid",
            },
        ]
    )
    graph.grant_agent_obo_consent = AsyncMock(return_value=None)
    cred_svc = MagicMock()
    cred_svc._region = "us-east-1"
    cred_svc.ensure_agent_credential_provider = AsyncMock(return_value="prov-x")
    identity_svc = MagicMock()
    agent_registry = MagicMock()
    agent_registry.list.return_value = [agent]
    agent_registry.persist_identity.return_value = None

    rebuild = AsyncMock()

    patches = _patch_singletons(
        graph=graph, cred_svc=cred_svc, identity_svc=identity_svc, agent_registry=agent_registry
    )
    _enter(patches)
    try:
        with patch.object(agent_mcp_grant, "rebuild_runtime_mcp_env", rebuild):
            assignment_id = await apply_agent_mcp_grant(agent, mcp, role="Invoker")
    finally:
        _exit(patches)

    assert assignment_id == "assign-existing"
    graph.grant_agent_obo_consent.assert_awaited_once_with("agent-sp-obj-id", "mcp-sp-obj-id")


@pytest.mark.asyncio
async def test_apply_grant_role_admin_uses_admin_role_id():
    """role="Admin" → the app role id passed to ``assign_app_role`` is ``mcp.admin_role_id``."""
    from services.agent_mcp_grant import apply_agent_mcp_grant

    agent = _make_agent()
    mcp = _make_mcp()

    graph = MagicMock()
    graph.assign_app_role = AsyncMock(return_value={"id": "assign-admin", "principalId": "agent-sp-obj-id"})
    graph.grant_agent_obo_consent = AsyncMock(return_value=None)
    cred_svc = MagicMock()
    cred_svc._region = "us-east-1"
    cred_svc.ensure_agent_credential_provider = AsyncMock(return_value="prov-x")
    identity_svc = MagicMock()
    agent_registry = MagicMock()
    agent_registry.list.return_value = [agent]
    agent_registry.persist_identity.return_value = None

    rebuild = AsyncMock()

    patches = _patch_singletons(
        graph=graph, cred_svc=cred_svc, identity_svc=identity_svc, agent_registry=agent_registry
    )
    _enter(patches)
    try:
        with patch.object(agent_mcp_grant, "rebuild_runtime_mcp_env", rebuild):
            await apply_agent_mcp_grant(agent, mcp, role="Admin")
    finally:
        _exit(patches)

    graph.assign_app_role.assert_awaited_once_with("mcp-sp-obj-id", "agent-sp-obj-id", "role-admin-guid")


@pytest.mark.asyncio
async def test_apply_grant_propagates_graph_failure():
    """A non-idempotent Graph failure (5xx) on ``assign_app_role`` → the function RAISES
    (it does NOT swallow real failures; the caller maps it to 502)."""
    from services.agent_mcp_grant import apply_agent_mcp_grant
    from services.graph_errors import GraphError

    agent = _make_agent()
    mcp = _make_mcp()

    graph = MagicMock()
    graph.assign_app_role = AsyncMock(side_effect=GraphError(500, "Internal"))
    graph.grant_agent_obo_consent = AsyncMock(return_value=None)
    cred_svc = MagicMock()
    cred_svc.ensure_agent_credential_provider = AsyncMock(return_value="prov-x")
    identity_svc = MagicMock()
    agent_registry = MagicMock()

    patches = _patch_singletons(
        graph=graph, cred_svc=cred_svc, identity_svc=identity_svc, agent_registry=agent_registry
    )
    _enter(patches)
    try:
        with pytest.raises(GrantRevokeError) as exc:
            await apply_agent_mcp_grant(agent, mcp, role="Invoker")
    finally:
        _exit(patches)

    assert (exc.value.kind, exc.value.status_code) == ("assign_failed", 502)
    assert exc.value.detail == "failed to assign the role"
    graph.grant_agent_obo_consent.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_grant_adds_mcp_id_and_rebuilds_env():
    """A successful grant adds ``mcp.id`` to ``agent.mcp_server_ids``, persists the agent,
    and awaits ``rebuild_runtime_mcp_env(agent)`` exactly once."""
    from services.agent_mcp_grant import apply_agent_mcp_grant

    agent = _make_agent(mcp_server_ids=[])
    mcp = _make_mcp()

    graph = MagicMock()
    graph.assign_app_role = AsyncMock(return_value={"id": "assign-new", "principalId": "agent-sp-obj-id"})
    graph.grant_agent_obo_consent = AsyncMock(return_value=None)
    cred_svc = MagicMock()
    cred_svc._region = "us-east-1"
    cred_svc.ensure_agent_credential_provider = AsyncMock(return_value="agp-agent-obo-agent-123")
    identity_svc = MagicMock()
    agent_registry = MagicMock()
    agent_registry.list.return_value = [agent]
    agent_registry.persist_identity.return_value = None

    rebuild = AsyncMock()

    patches = _patch_singletons(
        graph=graph, cred_svc=cred_svc, identity_svc=identity_svc, agent_registry=agent_registry
    )
    _enter(patches)
    try:
        with patch.object(agent_mcp_grant, "rebuild_runtime_mcp_env", rebuild):
            assignment_id = await apply_agent_mcp_grant(agent, mcp, role="Invoker")
    finally:
        _exit(patches)

    assert assignment_id == "assign-new"
    assert mcp.id in agent.mcp_server_ids
    agent_registry.persist_identity.assert_called()
    rebuild.assert_awaited_once_with(agent)


@pytest.mark.asyncio
async def test_apply_grant_dedupes_existing_mcp_id():
    """If ``mcp.id`` is already in the agent's set, the grant does NOT duplicate it (the set
    stays a single entry), and the env is still rebuilt."""
    from services.agent_mcp_grant import apply_agent_mcp_grant

    agent = _make_agent(mcp_server_ids=["mcp-123"])
    mcp = _make_mcp(id="mcp-123")

    graph = MagicMock()
    graph.assign_app_role = AsyncMock(return_value={"id": "assign-new", "principalId": "agent-sp-obj-id"})
    graph.grant_agent_obo_consent = AsyncMock(return_value=None)
    cred_svc = MagicMock()
    cred_svc._region = "us-east-1"
    cred_svc.ensure_agent_credential_provider = AsyncMock(return_value="prov-x")
    identity_svc = MagicMock()
    agent_registry = MagicMock()
    agent_registry.list.return_value = [agent]
    agent_registry.persist_identity.return_value = None

    rebuild = AsyncMock()

    patches = _patch_singletons(
        graph=graph, cred_svc=cred_svc, identity_svc=identity_svc, agent_registry=agent_registry
    )
    _enter(patches)
    try:
        with patch.object(agent_mcp_grant, "rebuild_runtime_mcp_env", rebuild):
            await apply_agent_mcp_grant(agent, mcp, role="Invoker")
    finally:
        _exit(patches)

    assert agent.mcp_server_ids == ["mcp-123"]
    rebuild.assert_awaited_once_with(agent)


@pytest.mark.asyncio
async def test_apply_grant_env_rebuild_failure_raises_split_state_message():
    """If ``rebuild_runtime_mcp_env`` raises (the env push failed), the grant RE-RAISES a
    ``RuntimeError`` whose message states the split state verbatim — the Entra permission is
    already updated; the operator must KNOW the runtime is not yet wired and retry fixes it."""
    from services.agent_mcp_grant import apply_agent_mcp_grant

    agent = _make_agent(mcp_server_ids=[])
    mcp = _make_mcp()

    graph = MagicMock()
    graph.assign_app_role = AsyncMock(return_value={"id": "assign-new", "principalId": "agent-sp-obj-id"})
    graph.grant_agent_obo_consent = AsyncMock(return_value=None)
    cred_svc = MagicMock()
    cred_svc._region = "us-east-1"
    cred_svc.ensure_agent_credential_provider = AsyncMock(return_value="prov-x")
    identity_svc = MagicMock()
    agent_registry = MagicMock()
    agent_registry.list.return_value = [agent]
    agent_registry.persist_identity.return_value = None

    rebuild = AsyncMock(side_effect=RuntimeError("boto blew up"))

    patches = _patch_singletons(
        graph=graph, cred_svc=cred_svc, identity_svc=identity_svc, agent_registry=agent_registry
    )
    _enter(patches)
    try:
        with patch.object(agent_mcp_grant, "rebuild_runtime_mcp_env", rebuild):
            with pytest.raises(GrantRevokeError) as exc:
                await apply_agent_mcp_grant(agent, mcp, role="Invoker")
    finally:
        _exit(patches)

    assert "cannot reach this MCP until the grant is retried" in str(exc.value)
    assert (exc.value.kind, exc.value.status_code) == ("wiring_failed", 502)
    assert "boto blew up" not in exc.value.detail


def _patch_graph(graph):
    """Patch only the graph singleton (the revoke body uses ``get_mcp_graph_service``).

    ``revoke_agent_mcp_grant`` reuses the SAME deferred accessor the route used —
    ``api.routes.mcp_server_grants.get_mcp_graph_service`` — so patch it at THAT source
    module (mirroring ``_patch_singletons`` for the grant side). The revoke body touches no
    credential / identity service or agent registry.
    """
    return [
        patch("api.routes.mcp_server_grants.get_mcp_graph_service", return_value=graph),
    ]


@pytest.mark.asyncio
async def test_revoke_calls_in_order():
    """Single-assignment agent → pre-revoke list_assignments(mcp.entra_sp_id) resolves the
    principalId, revoke_app_role(mcp.entra_sp_id, assignment_id) deletes the assignment, a
    re-list confirms no sibling remains, and revoke_agent_obo_consent(agent_sp, mcp_sp) tears
    down the OBO kill switch — in that exact order."""
    from services.agent_mcp_grant import revoke_agent_mcp_grant

    mcp = _make_mcp()

    graph = MagicMock()
    graph.list_assignments = AsyncMock(
        side_effect=[
            [
                {
                    "id": "assign-1",
                    "principalId": "agent-sp-obj-id",
                    "principalType": "ServicePrincipal",
                    "appRoleId": "role-invoker-guid",
                },
            ],
            [],
        ]
    )
    graph.revoke_app_role = AsyncMock(return_value=None)
    graph.revoke_agent_obo_consent = AsyncMock(return_value=None)

    manager = MagicMock()
    manager.attach_mock(graph.list_assignments, "list_assignments")
    manager.attach_mock(graph.revoke_app_role, "revoke_app_role")
    manager.attach_mock(graph.revoke_agent_obo_consent, "revoke_consent")

    agent_registry = MagicMock()
    agent_registry.list.return_value = [_make_agent(entra_sp_id="agent-sp-obj-id", mcp_server_ids=[mcp.id])]
    agent_registry.persist_identity.return_value = None

    patches = [
        *_patch_graph(graph),
        patch("api.routes.agents.get_service", return_value=agent_registry),
    ]
    _enter(patches)
    try:
        with patch.object(agent_mcp_grant, "rebuild_runtime_mcp_env", AsyncMock()):
            result = await revoke_agent_mcp_grant(mcp, "assign-1")
    finally:
        _exit(patches)

    assert result is None
    graph.revoke_app_role.assert_awaited_once_with("mcp-sp-obj-id", "assign-1")
    graph.revoke_agent_obo_consent.assert_awaited_once_with("agent-sp-obj-id", "mcp-sp-obj-id")
    assert graph.list_assignments.await_args_list[0].args[0] == "mcp-sp-obj-id"
    order = [
        c[0] for c in manager.mock_calls if c[0] in ("list_assignments", "revoke_app_role", "revoke_consent")
    ]
    assert order == ["list_assignments", "revoke_app_role", "list_assignments", "revoke_consent"]


@pytest.mark.asyncio
async def test_revoke_keeps_consent_when_sibling_assignment_remains():
    """Multiplicity guard: the agent holds BOTH Invoker (assign-1) AND Admin (assign-2) on
    the same MCP, sharing ONE consent. Revoking assign-1 → the assignment is revoked but the
    Admin assignment SURVIVES the re-list, so the shared consent is NOT torn down."""
    from services.agent_mcp_grant import revoke_agent_mcp_grant

    mcp = _make_mcp()

    graph = MagicMock()
    graph.list_assignments = AsyncMock(
        side_effect=[
            [
                {"id": "assign-1", "principalId": "agent-sp-obj-id", "appRoleId": "role-invoker-guid"},
                {"id": "assign-2", "principalId": "agent-sp-obj-id", "appRoleId": "role-admin-guid"},
            ],
            [
                {"id": "assign-2", "principalId": "agent-sp-obj-id", "appRoleId": "role-admin-guid"},
            ],
        ]
    )
    graph.revoke_app_role = AsyncMock(return_value=None)
    graph.revoke_agent_obo_consent = AsyncMock(return_value=None)

    agent_registry = MagicMock()
    agent_registry.list.return_value = [_make_agent(entra_sp_id="agent-sp-obj-id", mcp_server_ids=[mcp.id])]
    agent_registry.persist_identity.return_value = None

    patches = [
        *_patch_graph(graph),
        patch("api.routes.agents.get_service", return_value=agent_registry),
    ]
    _enter(patches)
    try:
        with patch.object(agent_mcp_grant, "rebuild_runtime_mcp_env", AsyncMock()):
            await revoke_agent_mcp_grant(mcp, "assign-1")
    finally:
        _exit(patches)

    graph.revoke_app_role.assert_awaited_once_with("mcp-sp-obj-id", "assign-1")
    graph.revoke_agent_obo_consent.assert_not_called()


@pytest.mark.asyncio
async def test_revoke_tears_down_consent_when_relist_returns_stale_deleted_assignment():
    """Read-after-delete race (verified live in Entra): ``appRoleAssignedTo`` is eventually
    consistent, so the post-revoke re-list fires milliseconds after the delete and almost
    always returns a STALE replica that STILL contains the assignment we JUST deleted
    (assign-1). The multiplicity guard must EXCLUDE our own just-deleted ``assignment_id``
    (a stale replica shares the same id; a genuine sibling has a different id), so the shared
    consent IS still torn down despite the stale re-list."""
    from services.agent_mcp_grant import revoke_agent_mcp_grant

    mcp = _make_mcp()

    graph = MagicMock()
    graph.list_assignments = AsyncMock(
        side_effect=[
            [
                {"id": "assign-1", "principalId": "agent-sp-obj-id", "appRoleId": "role-invoker-guid"},
            ],
            [
                {"id": "assign-1", "principalId": "agent-sp-obj-id", "appRoleId": "role-invoker-guid"},
            ],
        ]
    )
    graph.revoke_app_role = AsyncMock(return_value=None)
    graph.revoke_agent_obo_consent = AsyncMock(return_value=None)

    agent_registry = MagicMock()
    agent_registry.list.return_value = [_make_agent(entra_sp_id="agent-sp-obj-id", mcp_server_ids=[mcp.id])]
    agent_registry.persist_identity.return_value = None

    patches = [
        *_patch_graph(graph),
        patch("api.routes.agents.get_service", return_value=agent_registry),
    ]
    _enter(patches)
    try:
        with patch.object(agent_mcp_grant, "rebuild_runtime_mcp_env", AsyncMock()):
            await revoke_agent_mcp_grant(mcp, "assign-1")
    finally:
        _exit(patches)

    graph.revoke_app_role.assert_awaited_once_with("mcp-sp-obj-id", "assign-1")
    graph.revoke_agent_obo_consent.assert_awaited_once_with("agent-sp-obj-id", "mcp-sp-obj-id")


@pytest.mark.asyncio
async def test_revoke_stale_assignment_raises_not_found():
    """The assignment is no longer in the pre-revoke list (already gone — the FE double-click
    race) → agent SP unresolved; the function still attempts revoke_app_role, whose Graph 404
    raises ``GrantNotFoundError`` (and NO consent teardown is attempted)."""
    from services.agent_mcp_grant import GrantNotFoundError, revoke_agent_mcp_grant
    from services.graph_errors import GraphError

    mcp = _make_mcp()

    graph = MagicMock()
    graph.list_assignments = AsyncMock(return_value=[])
    graph.revoke_app_role = AsyncMock(side_effect=GraphError(404, "Request_ResourceNotFound"))
    graph.revoke_agent_obo_consent = AsyncMock(return_value=None)

    patches = _patch_graph(graph)
    _enter(patches)
    try:
        with pytest.raises(GrantNotFoundError):
            await revoke_agent_mcp_grant(mcp, "assign-stale")
    finally:
        _exit(patches)

    graph.revoke_agent_obo_consent.assert_not_called()


@pytest.mark.asyncio
async def test_revoke_list_failure_raises_read_error():
    """The pre-revoke list_assignments fails → ``GrantReadError`` (we will not blindly revoke
    and lose the chance to also clear the consent). revoke_app_role is never reached."""
    from services.agent_mcp_grant import GrantReadError, revoke_agent_mcp_grant
    from services.graph_errors import GraphError

    mcp = _make_mcp()

    graph = MagicMock()
    graph.list_assignments = AsyncMock(side_effect=GraphError(500, "Internal"))
    graph.revoke_app_role = AsyncMock(return_value=None)
    graph.revoke_agent_obo_consent = AsyncMock(return_value=None)

    patches = _patch_graph(graph)
    _enter(patches)
    try:
        with pytest.raises(GrantReadError):
            await revoke_agent_mcp_grant(mcp, "assign-1")
    finally:
        _exit(patches)

    graph.revoke_app_role.assert_not_called()
    graph.revoke_agent_obo_consent.assert_not_called()


@pytest.mark.asyncio
async def test_revoke_graph_failure_raises_revoke_failed():
    """A non-404 GraphError from revoke_app_role → ``GrantRevokeFailedError`` (the route maps
    it to the SAME 409 as today)."""
    from services.agent_mcp_grant import GrantRevokeFailedError, revoke_agent_mcp_grant
    from services.graph_errors import GraphError

    mcp = _make_mcp()

    graph = MagicMock()
    graph.list_assignments = AsyncMock(
        return_value=[
            {"id": "assign-1", "principalId": "agent-sp-obj-id", "appRoleId": "role-invoker-guid"},
        ]
    )
    graph.revoke_app_role = AsyncMock(side_effect=GraphError(500, "Internal"))
    graph.revoke_agent_obo_consent = AsyncMock(return_value=None)

    patches = _patch_graph(graph)
    _enter(patches)
    try:
        with pytest.raises(GrantRevokeFailedError):
            await revoke_agent_mcp_grant(mcp, "assign-1")
    finally:
        _exit(patches)

    graph.revoke_agent_obo_consent.assert_not_called()


@pytest.mark.asyncio
async def test_revoke_consent_failure_raises_consent_error():
    """A non-404 failure from revoke_agent_obo_consent (the assignment is ALREADY deleted at
    that point) → ``ConsentRevokeError``; the underlying GraphError message NEVER leaks into
    the raised exception's message (FIXED safe literal only)."""
    from services.agent_mcp_grant import ConsentRevokeError, revoke_agent_mcp_grant
    from services.graph_errors import GraphError

    secret_marker = "LEAKED-CONSENT-REVOKE-INTERNAL-DETAIL"
    mcp = _make_mcp()

    graph = MagicMock()
    graph.list_assignments = AsyncMock(
        side_effect=[
            [
                {"id": "assign-1", "principalId": "agent-sp-obj-id", "appRoleId": "role-invoker-guid"},
            ],
            [],
        ]
    )
    graph.revoke_app_role = AsyncMock(return_value=None)
    graph.revoke_agent_obo_consent = AsyncMock(
        side_effect=GraphError(403, "Authorization_RequestDenied", message=secret_marker)
    )

    patches = _patch_graph(graph)
    _enter(patches)
    try:
        with pytest.raises(ConsentRevokeError) as exc_info:
            await revoke_agent_mcp_grant(mcp, "assign-1")
    finally:
        _exit(patches)

    graph.revoke_app_role.assert_awaited_once_with("mcp-sp-obj-id", "assign-1")
    assert secret_marker not in str(exc_info.value)


@pytest.mark.asyncio
async def test_revoke_removes_mcp_id_and_rebuilds_env():
    """After the Entra revoke + consent teardown succeed, the revoked ``mcp.id`` is removed
    from the resolved agent's ``mcp_server_ids`` (only the revoked one), the agent is
    persisted, and the runtime env is rebuilt without that MCP — ``rebuild_runtime_mcp_env``
    is awaited exactly once. The kill-switch graph calls still fire."""
    from services.agent_mcp_grant import revoke_agent_mcp_grant

    mcp = _make_mcp(id="CZR2", entra_sp_id="mcp-sp-obj-id")
    agent = _make_agent(entra_sp_id="agent-sp-obj-id", mcp_server_ids=["CZR2", "FN01"])

    graph = MagicMock()
    graph.list_assignments = AsyncMock(
        side_effect=[
            [
                {
                    "id": "assign-1",
                    "principalId": "agent-sp-obj-id",
                    "principalType": "ServicePrincipal",
                    "appRoleId": "role-invoker-guid",
                },
            ],
            [],
        ]
    )
    graph.revoke_app_role = AsyncMock(return_value=None)
    graph.revoke_agent_obo_consent = AsyncMock(return_value=None)

    agent_registry = MagicMock()
    agent_registry.list.return_value = [agent]
    agent_registry.persist_identity.return_value = None

    rebuild = AsyncMock()

    patches = [
        *_patch_graph(graph),
        patch("api.routes.agents.get_service", return_value=agent_registry),
    ]
    _enter(patches)
    try:
        with patch.object(agent_mcp_grant, "rebuild_runtime_mcp_env", rebuild):
            await revoke_agent_mcp_grant(mcp, assignment_id="assign-1")
    finally:
        _exit(patches)

    assert agent.mcp_server_ids == ["FN01"]
    agent_registry.persist_identity.assert_called_once_with(agent)
    rebuild.assert_awaited_once_with(agent)
    graph.revoke_app_role.assert_awaited_once_with("mcp-sp-obj-id", "assign-1")
    graph.revoke_agent_obo_consent.assert_awaited_once_with("agent-sp-obj-id", "mcp-sp-obj-id")


@pytest.mark.asyncio
async def test_revoke_env_rebuild_failure_is_swallowed_not_raised():
    """If ``rebuild_runtime_mcp_env`` raises on revoke, the revoke does NOT re-raise — the
    Entra kill switch ALREADY succeeded (access is revoked); a stale env entry is harmless
    (fails closed → agent gets AADSTS65001 → degrade-drops it; self-heals on next
    grant/revoke). The revoke returns None, the kill-switch graph calls still happened, and
    ``persist_identity`` was called (the desired-state set WAS edited before the rebuild)."""
    from services.agent_mcp_grant import revoke_agent_mcp_grant

    mcp = _make_mcp(id="CZR2", entra_sp_id="mcp-sp-obj-id")
    agent = _make_agent(entra_sp_id="agent-sp-obj-id", mcp_server_ids=["CZR2"])

    graph = MagicMock()
    graph.list_assignments = AsyncMock(
        side_effect=[
            [
                {
                    "id": "assign-1",
                    "principalId": "agent-sp-obj-id",
                    "appRoleId": "role-invoker-guid",
                },
            ],
            [],
        ]
    )
    graph.revoke_app_role = AsyncMock(return_value=None)
    graph.revoke_agent_obo_consent = AsyncMock(return_value=None)

    agent_registry = MagicMock()
    agent_registry.list.return_value = [agent]
    agent_registry.persist_identity.return_value = None

    rebuild = AsyncMock(side_effect=RuntimeError("boto blew up"))

    patches = [
        *_patch_graph(graph),
        patch("api.routes.agents.get_service", return_value=agent_registry),
    ]
    _enter(patches)
    try:
        with patch.object(agent_mcp_grant, "rebuild_runtime_mcp_env", rebuild):
            result = await revoke_agent_mcp_grant(mcp, "assign-1")
    finally:
        _exit(patches)

    assert result is None
    graph.revoke_app_role.assert_awaited_once_with("mcp-sp-obj-id", "assign-1")
    graph.revoke_agent_obo_consent.assert_awaited_once_with("agent-sp-obj-id", "mcp-sp-obj-id")
    agent_registry.persist_identity.assert_called_once_with(agent)
    rebuild.assert_awaited_once_with(agent)


@pytest.mark.asyncio
async def test_first_grant_writes_the_agent_record_once(service, mock_registry_clients, monkeypatch):
    """A harness build creates the OBO provider but never persists
    its NAME, so the first grant has two changes (provider name + MCP id set). They go out in
    ONE UpdateRegistryRecord: two back-to-back writes hit the registry's UPDATING window
    (ConflictException → the live first-grant 502) and doubled the grant's latency. Real
    AgentRegistryService over a mocked control client."""
    from conftest import RECORD_ID

    from services.agent_mcp_grant import apply_agent_mcp_grant

    ctl, _ = mock_registry_clients
    agent = service.get(RECORD_ID)
    agent.entra_sp_id = "agent-sp-obj-id"
    agent.entra_app_id = "agent-app-id"
    agent.oauth2_credential_provider_name = None
    agent.mcp_server_ids = []
    ctl.update_registry_record.return_value = {"recordArn": RECORD_ID, "status": "DRAFT"}
    monkeypatch.setattr(service, "list", lambda *a, **k: [agent])

    graph = MagicMock()
    graph.assign_app_role = AsyncMock(return_value={"id": "assign-new"})
    graph.grant_agent_obo_consent = AsyncMock(return_value=None)
    cred_svc = MagicMock()
    cred_svc.ensure_agent_credential_provider = AsyncMock(return_value=f"agp-agent-obo-{RECORD_ID}")
    patches = _patch_singletons(
        graph=graph, cred_svc=cred_svc, identity_svc=MagicMock(), agent_registry=service
    )
    _enter(patches)
    try:
        with patch.object(agent_mcp_grant, "rebuild_runtime_mcp_env", AsyncMock()):
            assert await apply_agent_mcp_grant(agent, _make_mcp()) == "assign-new"
    finally:
        _exit(patches)

    assert ctl.update_registry_record.call_count == 1
    written = str(ctl.update_registry_record.call_args.kwargs["descriptors"])
    assert "mcp-123" in written
    assert f"agp-agent-obo-{RECORD_ID}" in written
