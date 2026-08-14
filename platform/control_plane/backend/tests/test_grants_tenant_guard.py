"""Cross-tenant grant guard tests (E24 multi-tenancy, Task 7).

Contract (spec §5 Grants — grant CREATION only; revoke + list unchanged):
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

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

CROSS_TENANT_403 = "cross-tenant grant requires admin"


@pytest.fixture(autouse=True)
def reset_modules():
    """Drop cached auth/config/route modules so monkeypatched env is honored."""
    import sys

    for mod in [
        "core.rbac",
        "core.security_entra",
        "core.config",
        "api.routes.agents",
        "api.routes.grants",
        "api.routes.mcp_servers",
        "api.routes.mcp_server_grants",
        "api.routes.users",
    ]:
        sys.modules.pop(mod, None)
    yield


@pytest.fixture
def entra_settings(monkeypatch):
    monkeypatch.setenv("AUTH_PROVIDER", "entra")
    monkeypatch.setenv("USE_DEV_AUTH", "False")
    monkeypatch.setenv("DEBUG", "False")
    monkeypatch.setenv("ENTRA_TENANT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("ENTRA_AUDIENCE", "api://agp")
    monkeypatch.setenv("ENTRA_BACKEND_CLIENT_ID", "backend-client-id")
    monkeypatch.setenv("ENTRA_ROLE_ADMIN", "Platform.Admin")
    monkeypatch.setenv("ENTRA_ROLE_OPERATOR", "Platform.Operator")
    monkeypatch.setenv("ENTRA_ROLE_VIEWER", "Platform.Viewer")


# --- fixtures ------------------------------------------------------------------


def _make_agent(**overrides):
    from models.agent import Agent, AuthType, LifecycleState, Origin, Platform

    now = datetime.now(timezone.utc)
    base = dict(
        id="rec-123",
        name="claims-triage-de",
        purpose="Triage claims",
        lifecycle_state=LifecycleState.APPROVED,
        origin=Origin.REGISTERED,
        platform=Platform.AWS_BEDROCK,
        auth_type=AuthType.ENTRA,
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
    base = dict(
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

    def __init__(self, grantee_tenants=frozenset(), ctx=None):
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

    return TenantContext(
        is_global=False, tenant_ids=frozenset(tenant_ids), tenants=()
    )


def _claims_for(role: str):
    role_app = {
        "viewer": "Platform.Viewer",
        "operator": "Platform.Operator",
        "admin": "Platform.Admin",
    }[role]
    return {
        "oid": f"{role}-oid",
        "preferred_username": f"{role}.user@example.com",
        "roles": [role_app],
    }


def _headers():
    return {"Authorization": "Bearer fake-token"}


# --- app builders --------------------------------------------------------------


def _build_grants_client(agent, resolver, mock_graph=None):
    """Minimal app with ONLY the user→agent grants router + mocked singletons."""
    import api.routes.agents as agents_module
    import api.routes.grants as grants_module
    import api.routes.users as users_module

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

    mock_mcp_registry = MagicMock()
    mock_mcp_registry.get.return_value = mcp
    mcp_servers_module._svc = mock_mcp_registry

    mock_agent_registry = MagicMock()
    mock_agent_registry.list.return_value = list(registry_agents)
    mock_agent_registry.get.return_value = (
        list(registry_agents)[0] if registry_agents else None
    )
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
    mock_cred_svc.ensure_agent_credential_provider = AsyncMock(
        return_value="agp-agent-obo-agent-123"
    )
    mcp_grants_module._cred_svc = mock_cred_svc
    mcp_grants_module._identity_svc = MagicMock()

    users_module._tenant_resolver = resolver if resolver is not None else _FakeResolver()

    app = FastAPI()
    app.include_router(mcp_grants_module.router, prefix="/api/v1")
    app.include_router(mcp_grants_module.agent_mcp_router, prefix="/api/v1")
    return TestClient(app), mock_graph


def _post_agent_grant(client, role, *, principal_id="user-1", principal_type="user"):
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for(role)):
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


# --- user→agent grant creation (grants.create_grant) ----------------------------


def test_user_agent_same_tenant_operator_passes(entra_settings):
    """Grantee's tenant set contains the agent's tenant → OPERATOR suffices (today's
    rule); the assignment fires and the grantee oid was the one resolved."""
    resolver = _FakeResolver(grantee_tenants={"ten-1"})
    client, mock_graph = _build_grants_client(_make_agent(tenant_id="ten-1"), resolver)

    resp = _post_agent_grant(client, "operator")

    assert resp.status_code == 201
    mock_graph.assign_app_role.assert_awaited_once_with(
        "sp-obj-id", "user-1", "role-invoker-guid"
    )
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
    route exercises ``resolve_oid_tenants`` end-to-end."""
    from services.graph_service import GraphError
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

    resp = _post_agent_grant(client, "operator")

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


# --- user→agent GROUP grantees (E6 grants groups too — T7-fix) -------------------


def _real_resolver_with_tenant(*, tenant_id="ten-1", entra_group_ids=(), graph_raises=True):
    """A REAL TenantResolver over one tenant record + a graph mock. The default
    RAISING graph mirrors production for a GROUP oid: /users/{oid}/transitiveMemberOf
    404s → GraphError → the user path degrades; only a DIRECT entra_group_ids match
    can resolve the tenant."""
    from services.graph_service import GraphError
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
    ``entra_group_ids`` is same-tenant → OPERATOR suffices (pre-E24 behavior
    restored). The Graph user-resolve is NOT required: it RAISES here (a group oid
    404s on transitiveMemberOf) and the direct match still succeeds — no 403, no
    raise, assignment fires."""
    resolver = _real_resolver_with_tenant(
        tenant_id="ten-1", entra_group_ids=["group-1"], graph_raises=True
    )
    client, mock_graph = _build_grants_client(_make_agent(tenant_id="ten-1"), resolver)

    resp = _post_agent_grant(
        client, "operator", principal_id="group-1", principal_type="group"
    )

    assert resp.status_code == 201
    mock_graph.assign_app_role.assert_awaited_once_with(
        "sp-obj-id", "group-1", "role-invoker-guid"
    )


def test_group_grantee_unlinked_operator_403_admin_passes(entra_settings):
    """A GROUP grantee linked to NO tenant resolves to the empty set → fail-closed
    cross-tenant: OPERATOR gets the FIXED 403 literal (no Graph write); ADMIN
    passes (the guard is skipped entirely on the admin path)."""
    resolver = _real_resolver_with_tenant(
        tenant_id="ten-1", entra_group_ids=["some-other-group"], graph_raises=True
    )
    client, mock_graph = _build_grants_client(_make_agent(tenant_id="ten-1"), resolver)

    resp = _post_agent_grant(
        client, "operator", principal_id="group-unlinked", principal_type="group"
    )

    assert resp.status_code == 403
    assert resp.json()["detail"] == CROSS_TENANT_403
    mock_graph.assign_app_role.assert_not_called()

    resp = _post_agent_grant(
        client, "admin", principal_id="group-unlinked", principal_type="group"
    )

    assert resp.status_code == 201
    mock_graph.assign_app_role.assert_awaited_once()


# --- agent→MCP grant creation (mcp_server_grants.create_mcp_grant) --------------


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
    """mcp.shared=True → being a grant target is allowed at OPERATOR even
    cross-tenant (Task-5 policy: shared = cross-tenant READ + grant-target)."""
    agent = _make_agent(id="agent-123", entra_sp_id="agent-sp-obj-id", tenant_id="ten-2")
    client, mock_graph = _build_mcp_grants_client(
        _make_mcp(tenant_id="ten-1", shared=True), [agent]
    )

    resp = _post_mcp_grant(client, "operator")

    assert resp.status_code == 201
    mock_graph.assign_app_role.assert_awaited_once()


def test_agent_mcp_none_tenant_ids_require_admin(entra_settings):
    """tenant_id=None on BOTH records (legacy/unstamped) is NOT same-tenant
    (None == None must NOT satisfy the guard — fail-closed) → OPERATOR 403."""
    agent = _make_agent(id="agent-123", entra_sp_id="agent-sp-obj-id", tenant_id=None)
    client, mock_graph = _build_mcp_grants_client(
        _make_mcp(tenant_id=None, shared=False), [agent]
    )

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


# --- grant sub-resource READ gates (E24 follow-up, review Finding L1) ------------
# The three GET routes must gate on the SAME visibility as their parent detail
# route: a foreign-tenant resource 404s with the byte-identical not-found literal
# (no Graph read fires — the assignment list must never leak), own-tenant is 200,
# and the shared-MCP READ bypass (for_write=False) is preserved.


def _get(client, role, path):
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for(role)):
        return client.get(f"/api/v1{path}", headers=_headers())


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

    resolver = _FakeResolver(
        ctx=TenantContext(is_global=True, tenant_ids=frozenset(), tenants=())
    )
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
    client, mock_graph = _build_mcp_grants_client(
        _make_mcp(tenant_id="ten-1"), [agent], resolver=resolver
    )

    resp = _get(client, "viewer", "/agents/agent-123/mcp-grants")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Agent not found"
    mock_graph.list_agent_mcp_grants.assert_not_called()


def test_list_agent_mcp_grants_own_tenant_viewer_200(entra_settings):
    """GET /agents/{id}/mcp-grants on an OWN-tenant agent → 200."""
    resolver = _FakeResolver(ctx=_scoped_ctx("ten-1"))
    agent = _make_agent(id="agent-123", tenant_id="ten-1")
    client, mock_graph = _build_mcp_grants_client(
        _make_mcp(tenant_id="ten-1"), [agent], resolver=resolver
    )

    resp = _get(client, "viewer", "/agents/agent-123/mcp-grants")

    assert resp.status_code == 200
    assert resp.json() == []
    mock_graph.list_agent_mcp_grants.assert_awaited_once_with("sp-obj-id")
