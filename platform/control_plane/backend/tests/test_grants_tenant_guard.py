"""Cross-tenant grant guard tests.

Contract, the CALLER's tenant (all four writes and the reads): every grant handler loads its
agent or MCP server through the parent route's visibility helper (``_load_visible_agent``,
``_load_visible_mcp_server``), so a foreign tenant's record answers the parent's literal 404
("Agent not found" / "MCP server not found"), byte-identical to a missing record, before the
provisioning 409, before the grantee resolve and before any Graph call. The MCP writes pass
``for_write=True``: a platform-shared MCP stays readable across tenants but is 404 as a foreign
grant target or revoke target. A global ctx (the real resolver's ADMIN) sees every record.

Contract, the GRANTEE's tenant (grant CREATION only, after the caller gate):
  - user→agent (``grants.create_grant``): resolve the GRANTEE user's tenant set via
    ``TenantResolver.resolve_oid_tenants(body.principal_id)``. If ``agent.tenant_id``
    is one of the grantee's tenants → OPERATOR suffices (today's rule). Else →
    require ADMIN; a non-admin gets 403 with the FIXED literal
    "cross-tenant grant requires admin".
  - agent→MCP (``mcp_server_grants.create_mcp_grant``): if
    ``agent.tenant_id == mcp.tenant_id`` (both non-None) or ``mcp.shared`` →
    OPERATOR. Else ADMIN, same 403 literal.
  - Fail-closed: a grantee-tenant resolution failure (GraphError inside
    ``resolve_oid_tenants`` → empty frozenset) is treated as cross-tenant → ADMIN
    required. A record with ``tenant_id=None`` (legacy/unstamped) is NOT
    same-tenant → ADMIN required.
  - The guard runs BEFORE the Graph call that creates the assignment — on a 403 the
    create-assignment mock must never have been called.

Mirrors the RBAC-test idiom of ``test_grants_routes.py`` /
``test_mcp_server_grants_routes.py``: reset cached modules, build a minimal app with
ONLY the router under test, patch the lazy singletons (no live AWS / Graph / Entra),
and seed ``api.routes.users._tenant_resolver`` (the ONE resolver-singleton accessor).
"""

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("reset_modules")

CROSS_TENANT_403 = "cross-tenant grant requires admin"


def _make_agent(**overrides):
    from models.agent import Agent, AgentAuthType, LifecycleState, Origin, Platform

    now = datetime.now(timezone.utc)
    base: dict[str, Any] = dict(
        id="rec-123",
        name="claims-triage-de",
        purpose="Triage claims",
        lifecycle_state=LifecycleState.APPROVED,
        origin=Origin.REGISTERED,
        platform=Platform.AWS_BEDROCK,
        auth_type=AgentAuthType.ENTRA,
        agent_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/agent-abc123",
        entra_sp_id="sp-obj-id",
        entra_app_audience="api://agp-agent-rec-123",
        invoker_role_id="role-invoker-guid",
        admin_role_id="role-admin-guid",
        identity_status="provisioned",
        created_at=now,
        updated_at=now,
        created_by="maria.bauer@example.com",
        tenant_id="ten-1",
    )
    base.update(overrides)
    return Agent(**base)


def _make_mcp(**overrides):
    from models.mcp_server import Kind, LifecycleState, McpServer

    now = datetime.now(timezone.utc)
    base: dict[str, Any] = dict(
        id="mcp-123",
        name="claims-mcp-de",
        description="Claims MCP server",
        kind=Kind.GATEWAY,
        lifecycle_state=LifecycleState.APPROVED,
        gateway_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/gw-abc123",
        entra_sp_id="mcp-sp-obj-id",
        entra_app_audience="api://agp-mcp-mcp-123",
        invoker_role_id="role-invoker-guid",
        admin_role_id="role-admin-guid",
        identity_status="provisioned",
        created_at=now,
        updated_at=now,
        created_by="maria.bauer@example.com",
        tenant_id="ten-1",
        shared=False,
    )
    base.update(overrides)
    return McpServer(**base)


class _FakeResolver:
    """Resolver double: fixed grantee tenant set for ``resolve_oid_tenants`` (records
    the oids it was asked about); ``resolve`` returns ``ctx`` (default: a global ctx —
    the pre-L1 tests don't exercise the read gate; the L1 read-gate tests pass a
    scoped ctx)."""

    def __init__(self, grantee_tenants: Iterable[str] = frozenset(), ctx=None):
        self._grantee_tenants = frozenset(grantee_tenants)
        self._ctx = ctx
        self.seen_oids = []

    async def resolve(self, principal):
        from services.tenant_resolver import TenantContext

        if self._ctx is not None:
            return self._ctx
        return TenantContext(is_global=True, tenant_ids=frozenset(), tenants=())

    async def resolve_oid_tenants(self, oid):
        self.seen_oids.append(oid)
        return self._grantee_tenants


def _scoped_ctx(*tenant_ids):
    """A NON-global TenantContext scoped to the given tenant ids."""
    from services.tenant_resolver import TenantContext

    return TenantContext(is_global=False, tenant_ids=frozenset(tenant_ids), tenants=())


def _claims_for(role: str, groups=None):
    """Token claims for ``role``; ``groups`` adds the caller's own group memberships, which the
    real ``TenantResolver`` reads from the token before it asks Graph."""
    role_app = {
        "viewer": "Platform.Viewer",
        "operator": "Platform.Operator",
        "admin": "Platform.Admin",
    }[role]
    claims = {
        "oid": f"{role}-oid",
        "preferred_username": f"{role}.user@example.com",
        "roles": [role_app],
    }
    if groups is not None:
        claims["groups"] = list(groups)
    return claims


def _headers():
    return {"Authorization": "Bearer fake-token"}


def _build_grants_client(agent, resolver, mock_graph=None):
    """Minimal app with ONLY the user→agent grants router + mocked singletons."""
    import api.routes.agents as agents_module
    import api.routes.grants as grants_module
    import api.routes.users as users_module
    from core.errors import install_error_handlers

    mock_registry = MagicMock()
    mock_registry.get.return_value = agent
    agents_module._svc = mock_registry

    if mock_graph is None:
        mock_graph = MagicMock()
        mock_graph.assign_app_role = AsyncMock(
            return_value={
                "id": "assign-new",
                "principalId": "user-1",
                "principalDisplayName": "Maria Bauer",
                "principalType": "User",
                "appRoleId": "role-invoker-guid",
            }
        )
        mock_graph.list_assignments = AsyncMock(return_value=[])
    grants_module._graph_svc = mock_graph

    users_module._tenant_resolver = resolver

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(grants_module.router, prefix="/api/v1")
    return TestClient(app), mock_graph


def _build_mcp_grants_client(mcp, registry_agents, resolver=None):
    """Minimal app with ONLY the agent→MCP grants routers + mocked singletons.

    ``resolver`` seeds the ONE tenant-resolver singleton (default: always-global —
    the pre-L1 guard tests only exercise the write guard, which reads
    ``principal.role`` directly; the L1 read-gate tests pass a scoped resolver).
    Includes BOTH routers so the agent-direction reverse read is testable too.
    """
    import api.routes.agents as agents_module
    import api.routes.mcp_server_grants as mcp_grants_module
    import api.routes.mcp_servers as mcp_servers_module
    import api.routes.users as users_module
    from core.errors import install_error_handlers

    mock_mcp_registry = MagicMock()
    mock_mcp_registry.get.return_value = mcp
    mcp_servers_module._svc = mock_mcp_registry

    mock_agent_registry = MagicMock()
    mock_agent_registry.list.return_value = list(registry_agents)
    mock_agent_registry.get.return_value = next(iter(registry_agents)) if registry_agents else None
    mock_agent_registry.persist_identity.return_value = None
    agents_module._svc = mock_agent_registry

    mock_graph = MagicMock()
    mock_graph.assign_app_role = AsyncMock(
        return_value={
            "id": "assign-new",
            "principalId": "agent-sp-obj-id",
            "principalDisplayName": "claims-triage-de",
            "principalType": "ServicePrincipal",
            "appRoleId": "role-invoker-guid",
        }
    )
    mock_graph.grant_agent_obo_consent = AsyncMock(return_value=None)
    mock_graph.list_assignments = AsyncMock(return_value=[])
    mock_graph.list_agent_mcp_grants = AsyncMock(return_value=[])
    mcp_grants_module._graph_svc = mock_graph

    mock_cred_svc = MagicMock()
    mock_cred_svc.ensure_agent_credential_provider = AsyncMock(return_value="agp-agent-obo-agent-123")
    mcp_grants_module._cred_svc = mock_cred_svc
    mcp_grants_module._identity_svc = MagicMock()

    users_module._tenant_resolver = resolver if resolver is not None else _FakeResolver()

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(mcp_grants_module.router, prefix="/api/v1")
    app.include_router(mcp_grants_module.agent_mcp_router, prefix="/api/v1")
    return TestClient(app), mock_graph


def _post_agent_grant(client, role, *, principal_id="user-1", principal_type="user", groups=None):
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for(role, groups)):
        return client.post(
            "/api/v1/agents/rec-123/grants",
            json={
                "principal_id": principal_id,
                "principal_type": principal_type,
                "role": "Invoker",
            },
            headers=_headers(),
        )


def _post_mcp_grant(client, role):
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for(role)):
        return client.post(
            "/api/v1/mcp-servers/mcp-123/grants",
            json={"principal_id": "agent-sp-obj-id", "principal_type": "agent", "role": "Invoker"},
            headers=_headers(),
        )


def test_user_agent_same_tenant_operator_passes(entra_settings):
    """Grantee's tenant set contains the agent's tenant → OPERATOR suffices (today's
    rule); the assignment fires and the grantee oid was the one resolved."""
    resolver = _FakeResolver(grantee_tenants={"ten-1"})
    client, mock_graph = _build_grants_client(_make_agent(tenant_id="ten-1"), resolver)

    resp = _post_agent_grant(client, "operator")

    assert resp.status_code == 201
    mock_graph.assign_app_role.assert_awaited_once_with("sp-obj-id", "user-1", "role-invoker-guid")
    assert resolver.seen_oids == ["user-1"]


def test_user_agent_cross_tenant_operator_403_literal_no_graph_write(entra_settings):
    """Grantee belongs to a DIFFERENT tenant → OPERATOR is rejected with the FIXED
    403 literal and the create-assignment Graph call is NEVER made."""
    resolver = _FakeResolver(grantee_tenants={"ten-2"})
    client, mock_graph = _build_grants_client(_make_agent(tenant_id="ten-1"), resolver)

    resp = _post_agent_grant(client, "operator")

    assert resp.status_code == 403
    assert resp.json()["detail"] == CROSS_TENANT_403
    mock_graph.assign_app_role.assert_not_called()


def test_user_agent_cross_tenant_admin_passes(entra_settings):
    """ADMIN may create cross-tenant grants — the guard does not even need to
    resolve the grantee (no Graph read on the admin path)."""
    resolver = _FakeResolver(grantee_tenants=frozenset())
    client, mock_graph = _build_grants_client(_make_agent(tenant_id="ten-1"), resolver)

    resp = _post_agent_grant(client, "admin")

    assert resp.status_code == 201
    mock_graph.assign_app_role.assert_awaited_once()
    assert resolver.seen_oids == []


def test_user_agent_grantee_resolution_failure_fail_closed_403(entra_settings):
    """A GraphError while resolving the grantee's tenants → empty set (fail-closed)
    → treated as cross-tenant → OPERATOR gets the 403 literal, no Graph write.

    Wires a REAL TenantResolver (raising graph mock) into the users singleton so the
    route exercises ``resolve_oid_tenants`` end-to-end; the caller's own membership rides in
    the token's ``groups`` claim so the caller gate passes and the grantee path is what fails."""
    from services.graph_errors import GraphError
    from services.tenant_resolver import TenantResolver

    tenant = MagicMock()
    tenant.id = "ten-1"
    tenant.entra_group_ids = ["grp-1"]
    tenant_service = MagicMock()
    tenant_service.list.return_value = [tenant]
    graph = AsyncMock()
    graph.list_member_group_ids = AsyncMock(side_effect=GraphError(502, "graph_down"))
    resolver = TenantResolver(tenant_service, graph)

    client, mock_graph = _build_grants_client(_make_agent(tenant_id="ten-1"), resolver)

    resp = _post_agent_grant(client, "operator", groups=["grp-1"])

    assert resp.status_code == 403
    assert resp.json()["detail"] == CROSS_TENANT_403
    mock_graph.assign_app_role.assert_not_called()


def test_user_agent_none_tenant_id_requires_admin(entra_settings):
    """A legacy/unstamped agent (tenant_id=None) is NEVER same-tenant (fail-closed):
    OPERATOR → 403 literal even when the grantee resolves to tenants."""
    resolver = _FakeResolver(grantee_tenants={"ten-1"})
    client, mock_graph = _build_grants_client(_make_agent(tenant_id=None), resolver)

    resp = _post_agent_grant(client, "operator")

    assert resp.status_code == 403
    assert resp.json()["detail"] == CROSS_TENANT_403
    mock_graph.assign_app_role.assert_not_called()


def test_user_agent_none_tenant_id_admin_passes(entra_settings):
    """...but ADMIN can still grant on a legacy/unstamped agent."""
    resolver = _FakeResolver(grantee_tenants=frozenset())
    client, mock_graph = _build_grants_client(_make_agent(tenant_id=None), resolver)

    resp = _post_agent_grant(client, "admin")

    assert resp.status_code == 201
    mock_graph.assign_app_role.assert_awaited_once()


def _real_resolver_with_tenant(*, tenant_id="ten-1", entra_group_ids=(), graph_raises=True):
    """A REAL TenantResolver over one tenant record + a graph mock. The default
    RAISING graph mirrors production for a GROUP oid: /users/{oid}/transitiveMemberOf
    404s → GraphError → the user path degrades; only a DIRECT entra_group_ids match
    can resolve the tenant."""
    from services.graph_errors import GraphError
    from services.tenant_resolver import TenantResolver

    tenant = MagicMock()
    tenant.id = tenant_id
    tenant.entra_group_ids = list(entra_group_ids)
    tenant_service = MagicMock()
    tenant_service.list.return_value = [tenant]
    graph = AsyncMock()
    if graph_raises:
        graph.list_member_group_ids = AsyncMock(
            side_effect=GraphError(404, "groups have no /users/{oid} memberships")
        )
    else:
        graph.list_member_group_ids = AsyncMock(return_value=[])
    return TenantResolver(tenant_service, graph)


def test_group_grantee_in_agent_tenant_operator_passes(entra_settings):
    """A GROUP grantee whose oid sits DIRECTLY in the agent tenant's
    ``entra_group_ids`` is same-tenant → OPERATOR suffices. The Graph user-resolve
    is NOT required: it RAISES here (a group oid
    404s on transitiveMemberOf) and the direct match still succeeds — no 403, no
    raise, assignment fires. The caller's own membership rides in the token's ``groups``
    claim."""
    resolver = _real_resolver_with_tenant(tenant_id="ten-1", entra_group_ids=["group-1"], graph_raises=True)
    client, mock_graph = _build_grants_client(_make_agent(tenant_id="ten-1"), resolver)

    resp = _post_agent_grant(
        client, "operator", principal_id="group-1", principal_type="group", groups=["group-1"]
    )

    assert resp.status_code == 201
    mock_graph.assign_app_role.assert_awaited_once_with("sp-obj-id", "group-1", "role-invoker-guid")


def test_group_grantee_unlinked_operator_403_admin_passes(entra_settings):
    """A GROUP grantee linked to NO tenant resolves to the empty set → fail-closed
    cross-tenant: OPERATOR gets the FIXED 403 literal (no Graph write); ADMIN
    passes (the guard is skipped entirely on the admin path). The caller's own membership
    rides in the token's ``groups`` claim."""
    resolver = _real_resolver_with_tenant(
        tenant_id="ten-1", entra_group_ids=["some-other-group"], graph_raises=True
    )
    client, mock_graph = _build_grants_client(_make_agent(tenant_id="ten-1"), resolver)

    resp = _post_agent_grant(
        client,
        "operator",
        principal_id="group-unlinked",
        principal_type="group",
        groups=["some-other-group"],
    )

    assert resp.status_code == 403
    assert resp.json()["detail"] == CROSS_TENANT_403
    mock_graph.assign_app_role.assert_not_called()

    resp = _post_agent_grant(client, "admin", principal_id="group-unlinked", principal_type="group")

    assert resp.status_code == 201
    mock_graph.assign_app_role.assert_awaited_once()


def test_agent_mcp_same_tenant_operator_passes(entra_settings):
    """agent.tenant_id == mcp.tenant_id (both non-None) → OPERATOR suffices."""
    agent = _make_agent(id="agent-123", entra_sp_id="agent-sp-obj-id", tenant_id="ten-1")
    client, mock_graph = _build_mcp_grants_client(_make_mcp(tenant_id="ten-1"), [agent])

    resp = _post_mcp_grant(client, "operator")

    assert resp.status_code == 201
    mock_graph.assign_app_role.assert_awaited_once_with(
        "mcp-sp-obj-id", "agent-sp-obj-id", "role-invoker-guid"
    )


def test_agent_mcp_cross_tenant_operator_403_literal_no_graph_write(entra_settings):
    """Different tenants, MCP not shared → OPERATOR gets the FIXED 403 literal and
    NEITHER assign_app_role NOR grant_agent_obo_consent is called."""
    agent = _make_agent(id="agent-123", entra_sp_id="agent-sp-obj-id", tenant_id="ten-2")
    client, mock_graph = _build_mcp_grants_client(_make_mcp(tenant_id="ten-1"), [agent])

    resp = _post_mcp_grant(client, "operator")

    assert resp.status_code == 403
    assert resp.json()["detail"] == CROSS_TENANT_403
    mock_graph.assign_app_role.assert_not_called()
    mock_graph.grant_agent_obo_consent.assert_not_called()


def test_agent_mcp_cross_tenant_admin_passes(entra_settings):
    """ADMIN may create the cross-tenant agent→MCP grant."""
    agent = _make_agent(id="agent-123", entra_sp_id="agent-sp-obj-id", tenant_id="ten-2")
    client, mock_graph = _build_mcp_grants_client(_make_mcp(tenant_id="ten-1"), [agent])

    resp = _post_mcp_grant(client, "admin")

    assert resp.status_code == 201
    mock_graph.assign_app_role.assert_awaited_once()


def test_agent_mcp_shared_mcp_cross_tenant_operator_passes(entra_settings):
    """mcp.shared=True → the grantee guard is skipped: a caller who can see the MCP for write may
    grant a foreign-tenant agent onto it at OPERATOR (shared = cross-tenant READ, and grant-target
    for a foreign grantee, never for a foreign caller)."""
    agent = _make_agent(id="agent-123", entra_sp_id="agent-sp-obj-id", tenant_id="ten-2")
    client, mock_graph = _build_mcp_grants_client(_make_mcp(tenant_id="ten-1", shared=True), [agent])

    resp = _post_mcp_grant(client, "operator")

    assert resp.status_code == 201
    mock_graph.assign_app_role.assert_awaited_once()


def test_agent_mcp_none_tenant_ids_require_admin(entra_settings):
    """tenant_id=None on BOTH records (legacy/unstamped) is NOT same-tenant
    (None == None must NOT satisfy the guard — fail-closed) → OPERATOR 403."""
    agent = _make_agent(id="agent-123", entra_sp_id="agent-sp-obj-id", tenant_id=None)
    client, mock_graph = _build_mcp_grants_client(_make_mcp(tenant_id=None, shared=False), [agent])

    resp = _post_mcp_grant(client, "operator")

    assert resp.status_code == 403
    assert resp.json()["detail"] == CROSS_TENANT_403
    mock_graph.assign_app_role.assert_not_called()


def test_agent_mcp_unresolvable_principal_requires_admin(entra_settings):
    """The grant principal SP does not resolve to a governed agent → the agent's
    tenant is unknowable → fail-closed → OPERATOR 403; ADMIN still passes."""
    other = _make_agent(id="agent-other", entra_sp_id="other-sp", tenant_id="ten-1")
    client, mock_graph = _build_mcp_grants_client(_make_mcp(tenant_id="ten-1"), [other])

    resp = _post_mcp_grant(client, "operator")

    assert resp.status_code == 403
    assert resp.json()["detail"] == CROSS_TENANT_403
    mock_graph.assign_app_role.assert_not_called()


def _get(client, role, path):
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for(role)):
        return client.get(f"/api/v1{path}", headers=_headers())


def _delete(client, role, path):
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for(role)):
        return client.delete(f"/api/v1{path}", headers=_headers())


def test_list_agent_grants_foreign_tenant_viewer_404_same_body_no_graph_read(
    entra_settings,
):
    """GET /agents/{id}/grants on a FOREIGN agent → 404 with the parent route's
    "Agent not found" literal (byte-identical to a missing agent) and NO Graph read."""
    resolver = _FakeResolver(ctx=_scoped_ctx("ten-2"))
    client, mock_graph = _build_grants_client(_make_agent(tenant_id="ten-1"), resolver)

    resp = _get(client, "viewer", "/agents/rec-123/grants")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Agent not found"
    mock_graph.list_assignments.assert_not_called()


def test_list_agent_grants_own_tenant_viewer_200(entra_settings):
    """GET /agents/{id}/grants on an OWN-tenant agent → 200 (the read still works)."""
    resolver = _FakeResolver(ctx=_scoped_ctx("ten-1"))
    client, mock_graph = _build_grants_client(_make_agent(tenant_id="ten-1"), resolver)

    resp = _get(client, "viewer", "/agents/rec-123/grants")

    assert resp.status_code == 200
    assert resp.json() == []
    mock_graph.list_assignments.assert_awaited_once_with("sp-obj-id")


def test_list_agent_grants_foreign_tenant_admin_200(entra_settings):
    """A global admin still reads any agent's grant list (gate unchanged for admin)."""
    from services.tenant_resolver import TenantContext

    resolver = _FakeResolver(ctx=TenantContext(is_global=True, tenant_ids=frozenset(), tenants=()))
    client, mock_graph = _build_grants_client(_make_agent(tenant_id="ten-1"), resolver)

    resp = _get(client, "admin", "/agents/rec-123/grants")

    assert resp.status_code == 200
    mock_graph.list_assignments.assert_awaited_once()


def test_list_mcp_grants_foreign_tenant_viewer_404_same_body_no_graph_read(
    entra_settings,
):
    """GET /mcp-servers/{id}/grants on a FOREIGN non-shared MCP → 404 with the parent
    route's "MCP server not found" literal and NO Graph read."""
    resolver = _FakeResolver(ctx=_scoped_ctx("ten-2"))
    client, mock_graph = _build_mcp_grants_client(
        _make_mcp(tenant_id="ten-1", shared=False), [], resolver=resolver
    )

    resp = _get(client, "viewer", "/mcp-servers/mcp-123/grants")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "MCP server not found"
    mock_graph.list_assignments.assert_not_called()


def test_list_mcp_grants_own_tenant_viewer_200(entra_settings):
    """GET /mcp-servers/{id}/grants on an OWN-tenant MCP → 200."""
    resolver = _FakeResolver(ctx=_scoped_ctx("ten-1"))
    client, mock_graph = _build_mcp_grants_client(
        _make_mcp(tenant_id="ten-1", shared=False), [], resolver=resolver
    )

    resp = _get(client, "viewer", "/mcp-servers/mcp-123/grants")

    assert resp.status_code == 200
    assert resp.json() == []
    mock_graph.list_assignments.assert_awaited_once_with("mcp-sp-obj-id")


def test_list_mcp_grants_shared_mcp_foreign_operator_200_read_bypass(entra_settings):
    """A platform-SHARED MCP stays readable cross-tenant (the for_write=False READ
    bypass — same semantics as the parent GET /mcp-servers/{id})."""
    resolver = _FakeResolver(ctx=_scoped_ctx("ten-2"))
    client, mock_graph = _build_mcp_grants_client(
        _make_mcp(tenant_id="ten-1", shared=True), [], resolver=resolver
    )

    resp = _get(client, "operator", "/mcp-servers/mcp-123/grants")

    assert resp.status_code == 200
    mock_graph.list_assignments.assert_awaited_once_with("mcp-sp-obj-id")


def test_list_agent_mcp_grants_foreign_tenant_viewer_404_same_body_no_graph_read(
    entra_settings,
):
    """GET /agents/{id}/mcp-grants gates on the AGENT's tenant: a FOREIGN agent →
    404 with the "Agent not found" literal and NO Graph read."""
    resolver = _FakeResolver(ctx=_scoped_ctx("ten-2"))
    agent = _make_agent(id="agent-123", tenant_id="ten-1")
    client, mock_graph = _build_mcp_grants_client(_make_mcp(tenant_id="ten-1"), [agent], resolver=resolver)

    resp = _get(client, "viewer", "/agents/agent-123/mcp-grants")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Agent not found"
    mock_graph.list_agent_mcp_grants.assert_not_called()


def test_list_agent_mcp_grants_own_tenant_viewer_200(entra_settings):
    """GET /agents/{id}/mcp-grants on an OWN-tenant agent → 200."""
    resolver = _FakeResolver(ctx=_scoped_ctx("ten-1"))
    agent = _make_agent(id="agent-123", tenant_id="ten-1")
    client, mock_graph = _build_mcp_grants_client(_make_mcp(tenant_id="ten-1"), [agent], resolver=resolver)

    resp = _get(client, "viewer", "/agents/agent-123/mcp-grants")

    assert resp.status_code == 200
    assert resp.json() == []
    mock_graph.list_agent_mcp_grants.assert_awaited_once_with("sp-obj-id")


def test_create_grant_foreign_tenant_operator_404_same_body_no_graph_write(entra_settings):
    """POST /agents/{id}/grants on a FOREIGN agent → 404 with the parent route's "Agent not
    found" literal, no Graph write, and the grantee is never resolved: the caller gate precedes
    the grantee guard, so a same-tenant grantee cannot open a foreign agent."""
    resolver = _FakeResolver(grantee_tenants={"ten-1"}, ctx=_scoped_ctx("ten-2"))
    client, mock_graph = _build_grants_client(_make_agent(tenant_id="ten-1"), resolver)

    resp = _post_agent_grant(client, "operator")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Agent not found"
    mock_graph.assign_app_role.assert_not_called()
    assert resolver.seen_oids == []


def test_create_grant_foreign_unprovisioned_agent_is_404_not_409(entra_settings):
    """A FOREIGN unprovisioned agent is 404, not the 409 that would confirm it exists."""
    resolver = _FakeResolver(grantee_tenants={"ten-1"}, ctx=_scoped_ctx("ten-2"))
    client, mock_graph = _build_grants_client(
        _make_agent(tenant_id="ten-1", identity_status="pending", entra_sp_id=None), resolver
    )

    resp = _post_agent_grant(client, "operator")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Agent not found"
    mock_graph.assign_app_role.assert_not_called()


def test_create_grant_own_tenant_scoped_operator_201(entra_settings):
    """A scoped (non-global) OPERATOR of the agent's own tenant still grants: 201, one assign."""
    resolver = _FakeResolver(grantee_tenants={"ten-1"}, ctx=_scoped_ctx("ten-1"))
    client, mock_graph = _build_grants_client(_make_agent(tenant_id="ten-1"), resolver)

    resp = _post_agent_grant(client, "operator")

    assert resp.status_code == 201
    mock_graph.assign_app_role.assert_awaited_once_with("sp-obj-id", "user-1", "role-invoker-guid")


def test_delete_grant_foreign_tenant_operator_404_same_body_no_graph_write(entra_settings):
    """DELETE /agents/{id}/grants/{assignment_id} on a FOREIGN agent → 404 "Agent not found",
    and the Graph revoke is never made."""
    resolver = _FakeResolver(ctx=_scoped_ctx("ten-2"))
    client, mock_graph = _build_grants_client(_make_agent(tenant_id="ten-1"), resolver)
    mock_graph.revoke_app_role = AsyncMock(return_value=None)

    resp = _delete(client, "operator", "/agents/rec-123/grants/assign-1")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Agent not found"
    mock_graph.revoke_app_role.assert_not_called()


def test_delete_grant_foreign_unprovisioned_agent_is_404_not_409(entra_settings):
    """A FOREIGN unprovisioned agent is 404 on revoke too, not the 409 that would confirm it exists."""
    resolver = _FakeResolver(ctx=_scoped_ctx("ten-2"))
    client, mock_graph = _build_grants_client(
        _make_agent(tenant_id="ten-1", identity_status="pending", entra_sp_id=None), resolver
    )
    mock_graph.revoke_app_role = AsyncMock(return_value=None)

    resp = _delete(client, "operator", "/agents/rec-123/grants/assign-1")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Agent not found"
    mock_graph.revoke_app_role.assert_not_called()


def test_delete_grant_own_tenant_scoped_operator_204(entra_settings):
    """A scoped OPERATOR of the agent's own tenant still revokes: 204, one Graph revoke."""
    resolver = _FakeResolver(ctx=_scoped_ctx("ten-1"))
    client, mock_graph = _build_grants_client(_make_agent(tenant_id="ten-1"), resolver)
    mock_graph.revoke_app_role = AsyncMock(return_value=None)

    resp = _delete(client, "operator", "/agents/rec-123/grants/assign-1")

    assert resp.status_code == 204
    mock_graph.revoke_app_role.assert_awaited_once_with("sp-obj-id", "assign-1")


def test_create_mcp_grant_foreign_tenant_operator_404_same_body_no_graph_write(entra_settings):
    """POST /mcp-servers/{id}/grants on a FOREIGN non-shared MCP → 404 with the parent route's
    "MCP server not found" literal (not the grantee guard's 403, which would confirm the MCP
    exists); neither assign_app_role nor grant_agent_obo_consent is called."""
    agent = _make_agent(id="agent-123", entra_sp_id="agent-sp-obj-id", tenant_id="ten-2")
    client, mock_graph = _build_mcp_grants_client(
        _make_mcp(tenant_id="ten-1", shared=False), [agent], resolver=_FakeResolver(ctx=_scoped_ctx("ten-2"))
    )

    resp = _post_mcp_grant(client, "operator")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "MCP server not found"
    mock_graph.assign_app_role.assert_not_called()
    mock_graph.grant_agent_obo_consent.assert_not_called()


def test_create_mcp_grant_foreign_shared_mcp_is_404_on_write(entra_settings):
    """A platform-SHARED foreign MCP is readable but NOT a grant target for a foreign caller:
    the write loads with for_write=True, so it is the same 404."""
    agent = _make_agent(id="agent-123", entra_sp_id="agent-sp-obj-id", tenant_id="ten-2")
    client, mock_graph = _build_mcp_grants_client(
        _make_mcp(tenant_id="ten-1", shared=True), [agent], resolver=_FakeResolver(ctx=_scoped_ctx("ten-2"))
    )

    resp = _post_mcp_grant(client, "operator")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "MCP server not found"
    mock_graph.assign_app_role.assert_not_called()
    mock_graph.grant_agent_obo_consent.assert_not_called()


def test_create_mcp_grant_foreign_unprovisioned_mcp_is_404_not_409(entra_settings):
    """A FOREIGN unprovisioned MCP is 404, not the 409 that would confirm it exists."""
    agent = _make_agent(id="agent-123", entra_sp_id="agent-sp-obj-id", tenant_id="ten-2")
    client, mock_graph = _build_mcp_grants_client(
        _make_mcp(tenant_id="ten-1", shared=False, identity_status="pending", entra_sp_id=None),
        [agent],
        resolver=_FakeResolver(ctx=_scoped_ctx("ten-2")),
    )

    resp = _post_mcp_grant(client, "operator")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "MCP server not found"
    mock_graph.assign_app_role.assert_not_called()


def test_create_mcp_grant_own_tenant_scoped_operator_201(entra_settings):
    """A scoped OPERATOR granting an own-tenant agent onto an own-tenant MCP: 201, one assign."""
    agent = _make_agent(id="agent-123", entra_sp_id="agent-sp-obj-id", tenant_id="ten-1")
    client, mock_graph = _build_mcp_grants_client(
        _make_mcp(tenant_id="ten-1", shared=False), [agent], resolver=_FakeResolver(ctx=_scoped_ctx("ten-1"))
    )

    resp = _post_mcp_grant(client, "operator")

    assert resp.status_code == 201
    mock_graph.assign_app_role.assert_awaited_once_with(
        "mcp-sp-obj-id", "agent-sp-obj-id", "role-invoker-guid"
    )


def test_delete_mcp_grant_foreign_tenant_operator_404_same_body_no_graph_write(entra_settings):
    """DELETE /mcp-servers/{id}/grants/{assignment_id} on a FOREIGN non-shared MCP → 404 "MCP
    server not found", no Graph revoke."""
    client, mock_graph = _build_mcp_grants_client(
        _make_mcp(tenant_id="ten-1", shared=False), [], resolver=_FakeResolver(ctx=_scoped_ctx("ten-2"))
    )
    mock_graph.revoke_app_role = AsyncMock(return_value=None)

    resp = _delete(client, "operator", "/mcp-servers/mcp-123/grants/assign-1")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "MCP server not found"
    mock_graph.revoke_app_role.assert_not_called()


def test_delete_mcp_grant_foreign_unprovisioned_mcp_is_404_not_409(entra_settings):
    """A FOREIGN unprovisioned MCP is 404 on revoke too, not the 409 that would confirm it exists."""
    client, mock_graph = _build_mcp_grants_client(
        _make_mcp(tenant_id="ten-1", shared=False, identity_status="pending", entra_sp_id=None),
        [],
        resolver=_FakeResolver(ctx=_scoped_ctx("ten-2")),
    )
    mock_graph.revoke_app_role = AsyncMock(return_value=None)

    resp = _delete(client, "operator", "/mcp-servers/mcp-123/grants/assign-1")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "MCP server not found"
    mock_graph.revoke_app_role.assert_not_called()


def test_delete_mcp_grant_foreign_shared_mcp_is_404_on_write(entra_settings):
    """A platform-SHARED foreign MCP is not a revoke target either (for_write=True): 404, no
    Graph revoke."""
    client, mock_graph = _build_mcp_grants_client(
        _make_mcp(tenant_id="ten-1", shared=True), [], resolver=_FakeResolver(ctx=_scoped_ctx("ten-2"))
    )
    mock_graph.revoke_app_role = AsyncMock(return_value=None)

    resp = _delete(client, "operator", "/mcp-servers/mcp-123/grants/assign-1")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "MCP server not found"
    mock_graph.revoke_app_role.assert_not_called()


def test_delete_mcp_grant_own_tenant_scoped_operator_204(entra_settings):
    """A scoped OPERATOR of the MCP's own tenant still revokes: 204, one Graph revoke."""
    client, mock_graph = _build_mcp_grants_client(
        _make_mcp(tenant_id="ten-1", shared=False), [], resolver=_FakeResolver(ctx=_scoped_ctx("ten-1"))
    )
    mock_graph.revoke_app_role = AsyncMock(return_value=None)

    resp = _delete(client, "operator", "/mcp-servers/mcp-123/grants/assign-1")

    assert resp.status_code == 204
    mock_graph.revoke_app_role.assert_awaited_once_with("mcp-sp-obj-id", "assign-1")


def _post(client, role, path):
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for(role)):
        return client.post(f"/api/v1{path}", headers=_headers())


def _mirror_double(monkeypatch):
    import api.routes.grants as grants_module

    mirror = MagicMock()
    mirror.drift = AsyncMock(return_value={"entries": []})
    mirror.reassert = AsyncMock(return_value={"entries": []})
    monkeypatch.setattr(grants_module, "get_grant_mirror_service", lambda: mirror)
    return mirror


def test_grants_drift_foreign_tenant_viewer_404_same_body_no_acl_read(entra_settings, monkeypatch):
    """GET /agents/{id}/grants/drift on a FOREIGN agent → 404 with the parent route's "Agent not
    found" literal and the app's ACL never read."""
    resolver = _FakeResolver(ctx=_scoped_ctx("ten-2"))
    client, _ = _build_grants_client(_make_agent(tenant_id="ten-1"), resolver)
    mirror = _mirror_double(monkeypatch)

    resp = _get(client, "viewer", "/agents/rec-123/grants/drift")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Agent not found"
    mirror.gate.assert_not_called()
    mirror.drift.assert_not_called()


def test_grants_reassert_foreign_tenant_operator_404_same_body_no_acl_write(entra_settings, monkeypatch):
    """POST /agents/{id}/grants/reassert on a FOREIGN agent → 404 "Agent not found" and the app's
    ACL never rewritten."""
    resolver = _FakeResolver(ctx=_scoped_ctx("ten-2"))
    client, _ = _build_grants_client(_make_agent(tenant_id="ten-1"), resolver)
    mirror = _mirror_double(monkeypatch)

    resp = _post(client, "operator", "/agents/rec-123/grants/reassert")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Agent not found"
    mirror.gate.assert_not_called()
    mirror.reassert.assert_not_called()


def test_grants_reassert_own_tenant_scoped_operator_reaches_the_mirror(entra_settings, monkeypatch):
    """A scoped OPERATOR of the agent's own tenant reaches the mirror once: 200."""
    resolver = _FakeResolver(ctx=_scoped_ctx("ten-1"))
    client, _ = _build_grants_client(_make_agent(tenant_id="ten-1"), resolver)
    mirror = _mirror_double(monkeypatch)

    resp = _post(client, "operator", "/agents/rec-123/grants/reassert")

    assert resp.status_code == 200, resp.text
    mirror.reassert.assert_awaited_once()


def test_list_missing_agent_mcp_grants_foreign_tenant_viewer_404_same_body(entra_settings, monkeypatch):
    """GET /agents/{id}/mcp-grants/missing on a FOREIGN agent → 404 "Agent not found" and the
    agent's harness tool configuration never read."""
    from services import agent_mcp_env

    agent = _make_agent(id="agent-123", tenant_id="ten-1")
    client, _ = _build_mcp_grants_client(
        _make_mcp(tenant_id="ten-1"), [agent], resolver=_FakeResolver(ctx=_scoped_ctx("ten-2"))
    )
    ungranted = MagicMock(return_value=[])
    monkeypatch.setattr(agent_mcp_env, "ungranted_tool_mcps", ungranted)

    resp = _get(client, "viewer", "/agents/agent-123/mcp-grants/missing")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Agent not found"
    ungranted.assert_not_called()
