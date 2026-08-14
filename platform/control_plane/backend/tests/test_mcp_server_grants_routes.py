"""Agent → MCP grants route tests (Epic 7, Task T-ROUTES).

Exercise the REAL `require_role` + `current_principal` dependency path
(`AUTH_PROVIDER=entra`) against a mocked `verify_entra_token` (no live Entra) and a
mocked `GraphService` / `McpServerRegistryService` / `AgentRegistryService` (no live
AWS / Graph / HTTP). Mirrors the RBAC-test idiom of `test_grants_routes.py`: reset
cached modules, build a minimal app with ONLY the mcp_server_grants + mcp_servers +
agents routers, patch the lazy singletons to return mocks.

Two routers live in `mcp_server_grants.py`:
  - `router` (prefix `/mcp-servers`): GET/POST `/{id}/grants` + DELETE `/{id}/grants/{aid}`.
  - `agent_mcp_router` (prefix `/agents`): GET `/{id}/mcp-grants` (the reverse join — the
    CRITIC-M2 prefix-mismatch guard).

The POST does BOTH `assign_app_role` AND `grant_agent_obo_consent` (delegated-consent
precondition for Tier-2 invoke). The GraphError→HTTP mapping uses FIXED `detail=`
literals (security carry-forward from T-GRAPH), NOT `str(err)`.
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


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
    monkeypatch.setenv("ENTRA_TENANT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("ENTRA_AUDIENCE", "api://agp")
    monkeypatch.setenv("ENTRA_BACKEND_CLIENT_ID", "backend-client-id")
    monkeypatch.setenv("ENTRA_ROLE_ADMIN", "Platform.Admin")
    monkeypatch.setenv("ENTRA_ROLE_OPERATOR", "Platform.Operator")
    monkeypatch.setenv("ENTRA_ROLE_VIEWER", "Platform.Viewer")


def _make_mcp(**overrides):
    """Build a real provisioned MCP server (gateway) for mock return values."""
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
        # E24/T7: the cross-tenant grant guard fails closed on tenant_id=None; this
        # file's fixtures predate tenant scoping — stamp the same fixed tenant as
        # _make_agent so the pre-guard (same-tenant OPERATOR) behavior is preserved.
        tenant_id="ten-1",
    )
    base.update(overrides)
    return McpServer(**base)


def _make_agent(**overrides):
    """Build a real provisioned AgentCore Agent (the grant principal / reverse-join subject)."""
    from models.agent import Agent, AuthType, LifecycleState, Origin, Platform

    now = datetime.now(timezone.utc)
    base = dict(
        id="agent-123",
        name="claims-triage-de",
        purpose="Triage claims",
        lifecycle_state=LifecycleState.APPROVED,
        origin=Origin.REGISTERED,
        platform=Platform.AWS_BEDROCK,
        auth_type=AuthType.ENTRA,
        agent_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/agent-abc123",
        entra_sp_id="agent-sp-obj-id",
        entra_app_id="agent-app-id",
        entra_app_audience="api://agp-agent-agent-123",
        invoker_role_id="agent-role-invoker-guid",
        admin_role_id="agent-role-admin-guid",
        identity_status="provisioned",
        created_at=now,
        updated_at=now,
        created_by="maria.bauer@example.com",
        # E24/T7: same-tenant as _make_mcp (see comment there).
        tenant_id="ten-1",
    )
    base.update(overrides)
    return Agent(**base)


def _build_client(
    mock_mcp_registry=None,
    mock_graph=None,
    mock_agent_registry=None,
    mock_cred_svc=None,
    mock_identity_svc=None,
):
    """Build a minimal app with the mcp_server_grants + mcp_servers + agents routers.

    Must be called AFTER the entra env fixture so the route modules import with the
    right settings. Patches the lazy singletons so no real AWS / Graph is touched.

    `mcp_server_grants.py` owns the GraphService singleton (`_graph_svc`) — patch it
    there. The MCP-grants routes resolve the MCP via `mcp_servers.get_service()` (its
    `_svc` singleton) and the agent via `agents.get_service()` (its `_svc` singleton).
    The POST also creates the agent's credential provider via the credential-service
    singleton (`mcp_grants_module._cred_svc`) — patch it there too — and (T-GRANT-ENV-INJECT)
    injects the reference agent's runtime env via the identity-service singleton
    (`mcp_grants_module._identity_svc`).
    """
    import api.routes.agents as agents_module
    import api.routes.mcp_server_grants as mcp_grants_module
    import api.routes.mcp_servers as mcp_servers_module
    import api.routes.users as users_module
    from services.tenant_resolver import TenantContext

    # E24 L1 follow-up: the grant-list reads now resolve a TenantContext via the ONE
    # resolver singleton. This file's fixtures predate tenant scoping — seed an
    # always-global stub so pre-gate behavior is preserved for every existing test.
    class _GlobalResolver:
        async def resolve(self, principal):
            return TenantContext(is_global=True, tenant_ids=frozenset(), tenants=())

    users_module._tenant_resolver = _GlobalResolver()

    if mock_mcp_registry is not None:
        mcp_servers_module._svc = mock_mcp_registry
    if mock_agent_registry is not None:
        agents_module._svc = mock_agent_registry
    if mock_graph is not None:
        mcp_grants_module._graph_svc = mock_graph
    if mock_cred_svc is not None:
        mcp_grants_module._cred_svc = mock_cred_svc
    # T-GRANT-ENV-INJECT: the POST also injects runtime env via the identity-service
    # singleton. Default it to a benign no-op double (set_runtime_environment is SYNC —
    # the route off-loads it via anyio.to_thread.run_sync) so existing create-grant tests
    # that reach the injection point never touch real AWS. Tests that assert on injection
    # pass their own ``mock_identity_svc``.
    if mock_identity_svc is not None:
        mcp_grants_module._identity_svc = mock_identity_svc
    else:
        mcp_grants_module._identity_svc = MagicMock()

    app = FastAPI()
    app.include_router(mcp_grants_module.router, prefix="/api/v1")
    app.include_router(mcp_grants_module.agent_mcp_router, prefix="/api/v1")
    return TestClient(app), mcp_grants_module


def _default_create_grant_mocks(agent=None, provider_name="agp-agent-obo-agent-123"):
    """Default agent-registry + credential-service mocks for the POST create-grant path.

    The create handler resolves the grant principal (an agent SP id) to one of OUR
    registry agents via ``get_agent_service().list()`` and then ensures that agent's
    credential provider. Existing create-grant tests don't care about that step, so
    this gives them a registry whose ``list()`` returns an agent matching the default
    ``principal_id`` ("agent-sp-obj-id") + a credential service that returns a name.
    """
    mock_agent_registry = MagicMock()
    mock_agent_registry.list.return_value = [agent if agent is not None else _make_agent()]
    mock_agent_registry.persist_identity.return_value = None
    mock_cred_svc = MagicMock()
    # ensure_agent_credential_provider is ASYNC (T-CRED-ASYNC-FIX) — the route awaits it
    # directly (no run_sync), so the double must be an AsyncMock.
    mock_cred_svc.ensure_agent_credential_provider = AsyncMock(return_value=provider_name)
    return mock_agent_registry, mock_cred_svc


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


# --- GET /mcp-servers/{id}/grants (list) ---------------------------------

def test_list_mcp_grants_viewer_ok_maps_roles(entra_settings):
    """VIEWER can list; appRoleId is mapped to Invoker/Admin via the MCP's role ids;
    the MCP's entra_sp_id is resolved + used for the graph read."""
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    mock_graph.list_assignments = AsyncMock(
        return_value=[
            {
                "id": "assign-1",
                "principalId": "agent-sp-obj-id",
                "principalDisplayName": "claims-triage-de",
                "principalType": "ServicePrincipal",
                "appRoleId": "role-invoker-guid",
            },
            {
                "id": "assign-2",
                "principalId": "agent-sp-2",
                "principalDisplayName": "another-agent",
                "principalType": "ServicePrincipal",
                "appRoleId": "role-admin-guid",
            },
            {
                "id": "assign-3",
                "principalId": "agent-sp-3",
                "principalDisplayName": "mystery",
                "principalType": "ServicePrincipal",
                "appRoleId": "some-other-guid",
            },
        ]
    )
    client, _ = _build_client(mock_mcp_registry=mock_mcp, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/mcp-servers/mcp-123/grants", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 3
    assert body[0]["role"] == "Invoker"
    assert body[0]["assignment_id"] == "assign-1"
    assert body[1]["role"] == "Admin"
    assert body[2]["role"] == "Unknown"
    mock_graph.list_assignments.assert_awaited_once_with("mcp-sp-obj-id")


def test_list_mcp_grants_unprovisioned_returns_empty(entra_settings):
    """An unprovisioned MCP (no sp / status != provisioned) → [] (NOT 409). No graph call."""
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp(identity_status="pending", entra_sp_id=None)
    mock_graph = MagicMock()
    mock_graph.list_assignments = AsyncMock(return_value=[])
    client, _ = _build_client(mock_mcp_registry=mock_mcp, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/mcp-servers/mcp-123/grants", headers=_headers())

    assert resp.status_code == 200
    assert resp.json() == []
    mock_graph.list_assignments.assert_not_called()


def test_list_mcp_grants_missing_mcp_404(entra_settings):
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = None
    mock_graph = MagicMock()
    client, _ = _build_client(mock_mcp_registry=mock_mcp, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/mcp-servers/mcp-missing/grants", headers=_headers())

    assert resp.status_code == 404


# --- POST /mcp-servers/{id}/grants (create) ------------------------------

def test_create_mcp_grant_operator_assigns_and_consents_invoker(entra_settings):
    """OPERATOR can create; role 'Invoker' → the MCP's invoker_role_id; the route does
    BOTH assign_app_role AND grant_agent_obo_consent(principal_id, mcp.entra_sp_id)."""
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
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
    mock_agent_registry, mock_cred_svc = _default_create_grant_mocks()
    client, _ = _build_client(
        mock_mcp_registry=mock_mcp,
        mock_graph=mock_graph,
        mock_agent_registry=mock_agent_registry,
        mock_cred_svc=mock_cred_svc,
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers/mcp-123/grants",
            json={"principal_id": "agent-sp-obj-id", "principal_type": "agent", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code in (200, 201)
    body = resp.json()
    assert body["role"] == "Invoker"
    assert body["assignment_id"] == "assign-new"
    mock_graph.assign_app_role.assert_awaited_once_with(
        "mcp-sp-obj-id", "agent-sp-obj-id", "role-invoker-guid"
    )
    # The delegated-consent precondition: grant_agent_obo_consent(agent_sp, mcp_sp).
    mock_graph.grant_agent_obo_consent.assert_awaited_once_with(
        "agent-sp-obj-id", "mcp-sp-obj-id"
    )


def test_create_mcp_grant_maps_admin_role(entra_settings):
    """role 'Admin' → the MCP's admin_role_id (and consent still fires)."""
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    mock_graph.assign_app_role = AsyncMock(
        return_value={
            "id": "assign-new",
            "principalId": "agent-sp-obj-id",
            "principalDisplayName": "claims-triage-de",
            "principalType": "ServicePrincipal",
            "appRoleId": "role-admin-guid",
        }
    )
    mock_graph.grant_agent_obo_consent = AsyncMock(return_value=None)
    mock_agent_registry, mock_cred_svc = _default_create_grant_mocks()
    client, _ = _build_client(
        mock_mcp_registry=mock_mcp,
        mock_graph=mock_graph,
        mock_agent_registry=mock_agent_registry,
        mock_cred_svc=mock_cred_svc,
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers/mcp-123/grants",
            json={"principal_id": "agent-sp-obj-id", "principal_type": "agent", "role": "Admin"},
            headers=_headers(),
        )

    assert resp.status_code in (200, 201)
    assert resp.json()["role"] == "Admin"
    mock_graph.assign_app_role.assert_awaited_once_with(
        "mcp-sp-obj-id", "agent-sp-obj-id", "role-admin-guid"
    )
    mock_graph.grant_agent_obo_consent.assert_awaited_once_with(
        "agent-sp-obj-id", "mcp-sp-obj-id"
    )


def test_create_mcp_grant_viewer_forbidden(entra_settings):
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    mock_graph.assign_app_role = AsyncMock()
    mock_graph.grant_agent_obo_consent = AsyncMock()
    client, _ = _build_client(mock_mcp_registry=mock_mcp, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/mcp-servers/mcp-123/grants",
            json={"principal_id": "agent-sp-obj-id", "principal_type": "agent", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code == 403
    mock_graph.assign_app_role.assert_not_called()
    mock_graph.grant_agent_obo_consent.assert_not_called()


def test_create_mcp_grant_bad_role_400(entra_settings):
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    mock_graph.assign_app_role = AsyncMock()
    mock_graph.grant_agent_obo_consent = AsyncMock()
    client, _ = _build_client(mock_mcp_registry=mock_mcp, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers/mcp-123/grants",
            json={"principal_id": "agent-sp-obj-id", "principal_type": "agent", "role": "Superuser"},
            headers=_headers(),
        )

    assert resp.status_code == 400
    mock_graph.assign_app_role.assert_not_called()


def test_create_mcp_grant_unprovisioned_409(entra_settings):
    """No sp / not provisioned → 409 on create (unlike list, which returns [])."""
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp(identity_status="pending", entra_sp_id=None)
    mock_graph = MagicMock()
    mock_graph.assign_app_role = AsyncMock()
    mock_graph.grant_agent_obo_consent = AsyncMock()
    client, _ = _build_client(mock_mcp_registry=mock_mcp, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers/mcp-123/grants",
            json={"principal_id": "agent-sp-obj-id", "principal_type": "agent", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code == 409
    mock_graph.assign_app_role.assert_not_called()


def test_create_mcp_grant_already_assigned_409(entra_settings):
    """A Graph 409/already-exists on assign → 409 to the caller."""
    from services.graph_service import GraphError

    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    mock_graph.assign_app_role = AsyncMock(side_effect=GraphError(409, "Request_BadRequest"))
    mock_graph.grant_agent_obo_consent = AsyncMock()
    # E24/T7: the create path now resolves the grant principal to a governed agent
    # (cross-tenant guard) BEFORE the Graph write — give it a same-tenant registry.
    mock_agent_registry, _ = _default_create_grant_mocks()
    client, _ = _build_client(
        mock_mcp_registry=mock_mcp,
        mock_graph=mock_graph,
        mock_agent_registry=mock_agent_registry,
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers/mcp-123/grants",
            json={"principal_id": "agent-sp-obj-id", "principal_type": "agent", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code == 409


def test_create_mcp_grant_graph_error_uses_fixed_literal_not_str_err(entra_settings):
    """A non-400/409 GraphError → 502 with a FIXED detail literal, NOT str(err) (which
    could leak a Graph resource message). Security carry-forward from T-GRAPH."""
    from services.graph_service import GraphError

    secret_marker = "LEAKED-GRAPH-RESOURCE-DETAIL"
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    mock_graph.assign_app_role = AsyncMock(
        side_effect=GraphError(500, "Internal", message=secret_marker)
    )
    mock_graph.grant_agent_obo_consent = AsyncMock()
    # E24/T7: same-tenant registry so the cross-tenant guard passes (see above).
    mock_agent_registry, _ = _default_create_grant_mocks()
    client, _ = _build_client(
        mock_mcp_registry=mock_mcp,
        mock_graph=mock_graph,
        mock_agent_registry=mock_agent_registry,
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers/mcp-123/grants",
            json={"principal_id": "agent-sp-obj-id", "principal_type": "agent", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code == 502
    assert secret_marker not in resp.text


def test_create_mcp_grant_missing_mcp_404(entra_settings):
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = None
    mock_graph = MagicMock()
    mock_graph.assign_app_role = AsyncMock()
    mock_graph.grant_agent_obo_consent = AsyncMock()
    client, _ = _build_client(mock_mcp_registry=mock_mcp, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers/mcp-missing/grants",
            json={"principal_id": "agent-sp-obj-id", "principal_type": "agent", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code == 404


# --- POST create-grant: agent credential-provider wiring (T-GRANT-CRED-WIRING) ---

def test_create_grant_ensures_credential_provider(entra_settings):
    """A successful POST ALSO ensures the agent's AgentCore credential provider:
    ``ensure_agent_credential_provider`` is called with the resolved Agent (the one whose
    ``entra_sp_id == body.principal_id``), AND ``persist_identity`` is called for BOTH the
    provider-name persist and the E12 mcp_server_ids set-add. Assign + consent still fire
    (no regression)."""
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
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

    # Two registry agents; only the second matches the grant principal id.
    other = _make_agent(id="agent-other", entra_sp_id="other-sp")
    target = _make_agent(id="agent-123", entra_sp_id="agent-sp-obj-id")
    mock_agent_registry = MagicMock()
    mock_agent_registry.list.return_value = [other, target]
    mock_cred_svc = MagicMock()
    # ASYNC (T-CRED-ASYNC-FIX): the route awaits it directly → AsyncMock.
    mock_cred_svc.ensure_agent_credential_provider = AsyncMock(
        return_value="agp-agent-obo-agent-123"
    )

    client, _ = _build_client(
        mock_mcp_registry=mock_mcp,
        mock_graph=mock_graph,
        mock_agent_registry=mock_agent_registry,
        mock_cred_svc=mock_cred_svc,
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers/mcp-123/grants",
            json={"principal_id": "agent-sp-obj-id", "principal_type": "agent", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code in (200, 201)
    # Assign + consent unchanged.
    mock_graph.assign_app_role.assert_awaited_once_with(
        "mcp-sp-obj-id", "agent-sp-obj-id", "role-invoker-guid"
    )
    mock_graph.grant_agent_obo_consent.assert_awaited_once_with(
        "agent-sp-obj-id", "mcp-sp-obj-id"
    )
    # The credential provider was ensured for the RESOLVED agent (matched by SP id).
    mock_cred_svc.ensure_agent_credential_provider.assert_called_once_with(target)
    # E12 (Task T3): persist_identity is now called TWICE per grant — once for the
    # provider-name (unchanged) and once for the mcp_server_ids set-add. Both calls carry
    # the same resolved agent record (``target``).
    assert mock_agent_registry.persist_identity.call_count == 2
    persisted_agents = [c.args[0] for c in mock_agent_registry.persist_identity.call_args_list]
    assert all(a is target for a in persisted_agents)
    assert target.oauth2_credential_provider_name == "agp-agent-obo-agent-123"
    # The MCP id was added to the agent's desired-state set.
    assert "mcp-123" in target.mcp_server_ids


def test_create_grant_reuses_existing_provider(entra_settings):
    """When the provider already exists, ``ensure_agent_credential_provider`` returns the
    existing name (idempotent get-or-create lives in the service). The route still calls
    it exactly once and the grant succeeds (201). With the name already on the agent, no
    provider-name re-persist fires — but E12 (Task T3) persists once for the mcp_server_ids
    set-add (the MCP id is added to the desired-state set on every successful grant)."""
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
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

    # Agent already carries the provider name (a prior grant created it).
    target = _make_agent(oauth2_credential_provider_name="agp-agent-obo-agent-123")
    mock_agent_registry = MagicMock()
    mock_agent_registry.list.return_value = [target]
    mock_cred_svc = MagicMock()
    # ASYNC (T-CRED-ASYNC-FIX): the route awaits it directly → AsyncMock.
    mock_cred_svc.ensure_agent_credential_provider = AsyncMock(
        return_value="agp-agent-obo-agent-123"
    )

    client, _ = _build_client(
        mock_mcp_registry=mock_mcp,
        mock_graph=mock_graph,
        mock_agent_registry=mock_agent_registry,
        mock_cred_svc=mock_cred_svc,
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers/mcp-123/grants",
            json={"principal_id": "agent-sp-obj-id", "principal_type": "agent", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code in (200, 201)
    mock_cred_svc.ensure_agent_credential_provider.assert_called_once_with(target)
    # Name unchanged → no provider-name re-persist. E12 (Task T3): the mcp_server_ids
    # set-add still persists exactly once (the MCP id is added to the desired-state set).
    mock_agent_registry.persist_identity.assert_called_once_with(target)


def test_create_grant_credential_failure_returns_502(entra_settings, caplog):
    """A failure inside ``ensure_agent_credential_provider`` → 502 with a FIXED detail
    literal; the detail MUST NOT contain any exception string (no secret/token leak —
    matches the file's GraphError→HTTP fixed-literal convention). The REAL exception is
    logged server-side via ``logger.exception`` (T-CRED-ASYNC-FIX: the failure used to be
    invisible) — assert an ERROR record was emitted while the 502 detail stays a literal."""
    import logging

    secret_marker = "LEAKED-CRED-PROVIDER-INTERNAL-DETAIL"
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
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

    target = _make_agent()
    mock_agent_registry = MagicMock()
    mock_agent_registry.list.return_value = [target]
    mock_cred_svc = MagicMock()
    # ASYNC (T-CRED-ASYNC-FIX): the route awaits it directly → AsyncMock that raises.
    mock_cred_svc.ensure_agent_credential_provider = AsyncMock(
        side_effect=RuntimeError(secret_marker)
    )

    client, _ = _build_client(
        mock_mcp_registry=mock_mcp,
        mock_graph=mock_graph,
        mock_agent_registry=mock_agent_registry,
        mock_cred_svc=mock_cred_svc,
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        with caplog.at_level(logging.ERROR):
            resp = client.post(
                "/api/v1/mcp-servers/mcp-123/grants",
                json={"principal_id": "agent-sp-obj-id", "principal_type": "agent", "role": "Invoker"},
                headers=_headers(),
            )

    assert resp.status_code == 502
    # No leak: the exception string must not surface in the response.
    assert secret_marker not in resp.text
    # The real failure was logged server-side (an ERROR record with traceback) so the 502
    # is no longer invisible. logger.exception emits at ERROR with exc_info attached.
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "expected logger.exception to emit an ERROR record"
    assert any(r.exc_info is not None for r in error_records), (
        "expected the logged record to carry the exception traceback"
    )


def test_create_grant_skips_provider_when_principal_not_governed_agent(entra_settings):
    """If the grant principal SP isn't one of OUR registry agents, the binary SP
    assignment is still valid → grant returns 201, but the provider step is SKIPPED
    (``ensure_agent_credential_provider`` NOT called) — defensive, not a 500.

    E24/T7: a non-governed principal's tenant is unknowable, so the cross-tenant
    guard fails closed at OPERATOR (covered in test_grants_tenant_guard.py) — this
    defensive path is now exercised as ADMIN."""
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    mock_graph.assign_app_role = AsyncMock(
        return_value={
            "id": "assign-new",
            "principalId": "agent-sp-obj-id",
            "principalDisplayName": "external-sp",
            "principalType": "ServicePrincipal",
            "appRoleId": "role-invoker-guid",
        }
    )
    mock_graph.grant_agent_obo_consent = AsyncMock(return_value=None)

    # No registry agent matches the grant principal id ("agent-sp-obj-id").
    mock_agent_registry = MagicMock()
    mock_agent_registry.list.return_value = [_make_agent(id="agent-other", entra_sp_id="other-sp")]
    mock_cred_svc = MagicMock()
    mock_cred_svc.ensure_agent_credential_provider.return_value = "should-not-be-called"

    client, _ = _build_client(
        mock_mcp_registry=mock_mcp,
        mock_graph=mock_graph,
        mock_agent_registry=mock_agent_registry,
        mock_cred_svc=mock_cred_svc,
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/mcp-servers/mcp-123/grants",
            json={"principal_id": "agent-sp-obj-id", "principal_type": "agent", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code in (200, 201)
    mock_cred_svc.ensure_agent_credential_provider.assert_not_called()
    mock_agent_registry.persist_identity.assert_not_called()


# --- POST create-grant: reference-agent runtime env injection (T-GRANT-ENV-INJECT) ---
# After the credential provider is ensured, the route injects the 4 env vars the reference
# agent reads (agent.py: CREDENTIAL_PROVIDER_NAME / MCP_GATEWAY_URL / MCP_AUDIENCE /
# AWS_REGION) onto the agent's AgentCore Runtime via the identity service's
# set_runtime_environment, dispatched OFF the loop (sync boto3, research §12.3). A failure
# fails loud (502 + logger.exception, research §12.5) — a successful grant must imply a
# fully-wired, invokable agent.

def test_grant_injects_runtime_env_after_credential_provider(entra_settings):
    """A successful POST rebuilds the full runtime env AFTER ensuring the credential
    provider: ``set_runtime_environment`` is called with the agent's runtime handle
    (``agent_arn``) and the E12 MCP_SERVERS env shape (a JSON list with one entry per
    granted MCP + CREDENTIAL_PROVIDER_NAME; the legacy MCP_AUDIENCE / MCP_GATEWAY_URL
    keys are neutralized to ""), dispatched off-loop via anyio.to_thread.run_sync,
    AFTER ``ensure_agent_credential_provider``."""
    mock_mcp = MagicMock()
    # The MCP carries the verbatim gateway_url + audience the agent needs.
    mock_mcp.get.return_value = _make_mcp(
        gateway_url="https://gw-abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
        entra_app_audience="api://agp-mcp-mcp-123",
    )
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

    target = _make_agent(id="agent-123", entra_sp_id="agent-sp-obj-id")
    mock_agent_registry = MagicMock()
    mock_agent_registry.list.return_value = [target]
    mock_agent_registry.persist_identity.return_value = None
    mock_cred_svc = MagicMock()
    mock_cred_svc._region = "us-east-1"  # the region the agent's AWS_REGION must match
    mock_cred_svc.ensure_agent_credential_provider = AsyncMock(
        return_value="agp-agent-obo-agent-123"
    )
    # set_runtime_environment is SYNC (agent_mcp_env off-loads it via anyio.to_thread.run_sync).
    mock_identity_svc = MagicMock()

    client, mcp_grants_module = _build_client(
        mock_mcp_registry=mock_mcp,
        mock_graph=mock_graph,
        mock_agent_registry=mock_agent_registry,
        mock_cred_svc=mock_cred_svc,
        mock_identity_svc=mock_identity_svc,
    )

    # Record the relative order of provider-ensure vs env-inject + prove the env-inject runs
    # OFF the loop (agent_mcp_env.rebuild_runtime_mcp_env wraps set_runtime_environment in
    # anyio.to_thread.run_sync, sourced from the agent_mcp_env module).
    manager = MagicMock()
    manager.attach_mock(mock_cred_svc.ensure_agent_credential_provider, "ensure_provider")
    manager.attach_mock(mock_identity_svc.set_runtime_environment, "set_env")

    import services.agent_mcp_env as agent_mcp_env_module
    real_run_sync = agent_mcp_env_module.anyio.to_thread.run_sync
    run_sync_recorder = AsyncMock(side_effect=real_run_sync)
    with patch.object(
        agent_mcp_env_module.anyio.to_thread, "run_sync", run_sync_recorder
    ):
        with patch(
            "core.security_entra.verify_entra_token",
            return_value=_claims_for("operator"),
        ):
            resp = client.post(
                "/api/v1/mcp-servers/mcp-123/grants",
                json={"principal_id": "agent-sp-obj-id", "principal_type": "agent", "role": "Invoker"},
                headers=_headers(),
            )

    assert resp.status_code in (200, 201)
    # set_runtime_environment called once with the agent's runtime handle + the E12 env shape.
    mock_identity_svc.set_runtime_environment.assert_called_once()
    call = mock_identity_svc.set_runtime_environment.call_args
    # First positional arg = the agent's AgentCore Runtime handle (agent_arn).
    assert call.args[0] == target.agent_arn
    env = call.args[1] if len(call.args) > 1 else call.kwargs["env"]
    # E12 (Task T3): the env now carries an MCP_SERVERS JSON list (one entry per granted MCP)
    # + CREDENTIAL_PROVIDER_NAME; the legacy single-MCP keys are neutralized to "" so a
    # not-yet-redeployed runtime can no longer read a stale real value alongside the list.
    assert "MCP_SERVERS" in env
    servers = json.loads(env["MCP_SERVERS"])
    assert len(servers) == 1
    entry = servers[0]
    assert entry["id"] == "mcp-123"
    assert entry["audience"] == "api://agp-mcp-mcp-123"
    assert entry["gateway_url"] == "https://gw-abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
    assert "label" in entry                                   # deterministic slug, not asserted verbatim
    assert env["CREDENTIAL_PROVIDER_NAME"] == "agp-agent-obo-agent-123"
    assert env["MCP_AUDIENCE"] == ""                          # legacy key neutralized
    assert env["MCP_GATEWAY_URL"] == ""                       # legacy key neutralized
    # Dispatched OFF the loop: run_sync was awaited with the bound set_runtime_environment.
    assert run_sync_recorder.await_count >= 1
    assert any(
        c.args and c.args[0] == mock_identity_svc.set_runtime_environment
        for c in run_sync_recorder.await_args_list
    )
    # Order: provider ensured BEFORE env injected.
    order = [c[0] for c in manager.mock_calls]
    assert order.index("ensure_provider") < order.index("set_env")


def test_grant_skips_env_injection_when_agent_has_no_runtime_handle(entra_settings):
    """A governed agent WITHOUT a runtime handle (metadata-only / external — agent_arn is
    None) → the grant + provider step still run, but env injection is SKIPPED (nothing to
    update; do NOT error). Grant returns 201."""
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp(
        gateway_url="https://gw-abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
        entra_app_audience="api://agp-mcp-mcp-123",
    )
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

    # The agent has an Entra app (so the provider step runs) but NO agent_arn.
    target = _make_agent(id="agent-123", entra_sp_id="agent-sp-obj-id", agent_arn=None)
    mock_agent_registry = MagicMock()
    mock_agent_registry.list.return_value = [target]
    mock_agent_registry.persist_identity.return_value = None
    mock_cred_svc = MagicMock()
    mock_cred_svc._region = "us-east-1"
    mock_cred_svc.ensure_agent_credential_provider = AsyncMock(
        return_value="agp-agent-obo-agent-123"
    )
    mock_identity_svc = MagicMock()

    client, _ = _build_client(
        mock_mcp_registry=mock_mcp,
        mock_graph=mock_graph,
        mock_agent_registry=mock_agent_registry,
        mock_cred_svc=mock_cred_svc,
        mock_identity_svc=mock_identity_svc,
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers/mcp-123/grants",
            json={"principal_id": "agent-sp-obj-id", "principal_type": "agent", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code in (200, 201)
    # The provider was still ensured (it doesn't need a runtime handle).
    mock_cred_svc.ensure_agent_credential_provider.assert_called_once_with(target)
    # But env injection is SKIPPED — no runtime to update.
    mock_identity_svc.set_runtime_environment.assert_not_called()


def test_grant_fails_loud_when_env_injection_raises(entra_settings, caplog):
    """A failure inside ``set_runtime_environment`` → 502 with a FIXED detail literal (no
    exception string leaks) AND ``logger.exception`` is invoked server-side (an ERROR record
    with the traceback), matching the credential-provider-failure path (research §12.5/§12.9).
    A successful grant must imply a fully-wired agent, so a wiring failure must NOT silently
    succeed."""
    import logging

    secret_marker = "LEAKED-ENV-INJECTION-INTERNAL-DETAIL"
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp(
        gateway_url="https://gw-abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
        entra_app_audience="api://agp-mcp-mcp-123",
    )
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

    target = _make_agent(id="agent-123", entra_sp_id="agent-sp-obj-id")
    mock_agent_registry = MagicMock()
    mock_agent_registry.list.return_value = [target]
    mock_agent_registry.persist_identity.return_value = None
    mock_cred_svc = MagicMock()
    mock_cred_svc._region = "us-east-1"
    mock_cred_svc.ensure_agent_credential_provider = AsyncMock(
        return_value="agp-agent-obo-agent-123"
    )
    mock_identity_svc = MagicMock()
    # The sync set_runtime_environment raises (e.g. a RuntimeError from the poll-to-READY).
    mock_identity_svc.set_runtime_environment.side_effect = RuntimeError(secret_marker)

    client, _ = _build_client(
        mock_mcp_registry=mock_mcp,
        mock_graph=mock_graph,
        mock_agent_registry=mock_agent_registry,
        mock_cred_svc=mock_cred_svc,
        mock_identity_svc=mock_identity_svc,
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        with caplog.at_level(logging.ERROR):
            resp = client.post(
                "/api/v1/mcp-servers/mcp-123/grants",
                json={"principal_id": "agent-sp-obj-id", "principal_type": "agent", "role": "Invoker"},
                headers=_headers(),
            )

    assert resp.status_code == 502
    # No leak: the exception string must not surface in the response.
    assert secret_marker not in resp.text
    # The real failure was logged server-side (logger.exception → ERROR record with traceback).
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "expected logger.exception to emit an ERROR record"
    assert any(r.exc_info is not None for r in error_records), (
        "expected the logged record to carry the exception traceback"
    )


# --- DELETE /mcp-servers/{id}/grants/{assignment_id} ---------------------

def test_delete_mcp_grant_operator_ok_204(entra_settings):
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    # The route resolves the agent SP from the assignment BEFORE revoking, then re-lists
    # post-revoke to apply the multiplicity guard. Single assignment → empty after revoke.
    mock_graph.list_assignments = AsyncMock(
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
    mock_graph.revoke_app_role = AsyncMock(return_value=None)
    mock_graph.revoke_agent_obo_consent = AsyncMock(return_value=None)
    # E12 (Task T4): after the kill switch, revoke_agent_mcp_grant resolves the governed agent
    # by SP and rebuilds the runtime env without the revoked MCP. Inject a registry so the
    # agent resolves deterministically (no real registry / AWS); set_runtime_environment is
    # the benign no-op identity double _build_client installs by default.
    mock_agent_registry = MagicMock()
    mock_agent_registry.list.return_value = [_make_agent(mcp_server_ids=["mcp-123"])]
    mock_agent_registry.persist_identity.return_value = None
    client, _ = _build_client(
        mock_mcp_registry=mock_mcp,
        mock_graph=mock_graph,
        mock_agent_registry=mock_agent_registry,
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete(
            "/api/v1/mcp-servers/mcp-123/grants/assign-1", headers=_headers()
        )

    assert resp.status_code == 204
    mock_graph.revoke_app_role.assert_awaited_once_with("mcp-sp-obj-id", "assign-1")


def test_delete_mcp_grant_viewer_forbidden(entra_settings):
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    mock_graph.revoke_app_role = AsyncMock()
    client, _ = _build_client(mock_mcp_registry=mock_mcp, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.delete(
            "/api/v1/mcp-servers/mcp-123/grants/assign-1", headers=_headers()
        )

    assert resp.status_code == 403
    mock_graph.revoke_app_role.assert_not_called()


def test_delete_mcp_grant_missing_mcp_404(entra_settings):
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = None
    mock_graph = MagicMock()
    mock_graph.revoke_app_role = AsyncMock()
    client, _ = _build_client(mock_mcp_registry=mock_mcp, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete(
            "/api/v1/mcp-servers/mcp-missing/grants/assign-1", headers=_headers()
        )

    assert resp.status_code == 404


def test_delete_mcp_grant_stale_assignment_404(entra_settings):
    """A GraphError(404) from revoke (already-deleted assignment) → 404, NOT a raw 500."""
    from services.graph_service import GraphError

    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    # The stale assignment_id is no longer in the list (already gone) → agent SP can't be
    # resolved; the route still attempts revoke_app_role, whose 404 maps to 404.
    mock_graph.list_assignments = AsyncMock(return_value=[])
    mock_graph.revoke_app_role = AsyncMock(
        side_effect=GraphError(404, "Request_ResourceNotFound")
    )
    mock_graph.revoke_agent_obo_consent = AsyncMock(return_value=None)
    client, _ = _build_client(mock_mcp_registry=mock_mcp, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete(
            "/api/v1/mcp-servers/mcp-123/grants/assign-stale", headers=_headers()
        )

    assert resp.status_code == 404
    # No consent teardown for a stale/missing assignment (no agent SP to scope it).
    mock_graph.revoke_agent_obo_consent.assert_not_called()


# --- DELETE: revoke ALSO deletes the OBO consent grant (E7 security-fix) ---
# Revoking only the app-role assignment is COSMETIC for the delegated path: the MCP app
# is appRoleAssignmentRequired=false, so the real agent→MCP admission gate is the consent
# grant (oauth2PermissionGrant). The DELETE must capture the agent SP (principalId) from
# the assignment BEFORE revoke_app_role removes it, then delete the consent — UNLESS the
# agent still holds ANOTHER app-role assignment on this MCP (Invoker+Admin share ONE
# consent), in which case the consent is kept.

def test_delete_mcp_grant_revokes_assignment_and_consent(entra_settings):
    """A single-assignment agent: the DELETE resolves the agent SP (principalId) from the
    target assignment, calls revoke_app_role(mcp_sp, assignment_id), then — because no
    OTHER assignment remains for that agent SP — calls revoke_agent_obo_consent(agent_sp,
    mcp_sp). principalId is resolved BEFORE the revoke (it is unrecoverable afterward).
    Returns 204."""
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    # First list_assignments (pre-revoke): resolves principalId for the target assignment.
    # Second list_assignments (post-revoke): the agent has NO remaining assignment.
    mock_graph.list_assignments = AsyncMock(
        side_effect=[
            [
                {
                    "id": "assign-1",
                    "principalId": "agent-sp-obj-id",
                    "principalDisplayName": "claims-triage-de",
                    "principalType": "ServicePrincipal",
                    "appRoleId": "role-invoker-guid",
                },
            ],
            [],  # post-revoke: nothing left for this agent.
        ]
    )
    mock_graph.revoke_app_role = AsyncMock(return_value=None)
    mock_graph.revoke_agent_obo_consent = AsyncMock(return_value=None)

    # Record relative call order: principalId must be resolved (list_assignments) BEFORE
    # the assignment is revoked (revoke_app_role).
    manager = MagicMock()
    manager.attach_mock(mock_graph.list_assignments, "list_assignments")
    manager.attach_mock(mock_graph.revoke_app_role, "revoke_app_role")
    manager.attach_mock(mock_graph.revoke_agent_obo_consent, "revoke_consent")

    # E12 (Task T4): after the kill switch, revoke resolves the governed agent by SP and
    # rebuilds the runtime env — inject a registry so the agent resolves deterministically
    # (no real registry / AWS).
    mock_agent_registry = MagicMock()
    mock_agent_registry.list.return_value = [_make_agent(mcp_server_ids=["mcp-123"])]
    mock_agent_registry.persist_identity.return_value = None

    client, _ = _build_client(
        mock_mcp_registry=mock_mcp,
        mock_graph=mock_graph,
        mock_agent_registry=mock_agent_registry,
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete(
            "/api/v1/mcp-servers/mcp-123/grants/assign-1", headers=_headers()
        )

    assert resp.status_code == 204
    # The app-role assignment was revoked.
    mock_graph.revoke_app_role.assert_awaited_once_with("mcp-sp-obj-id", "assign-1")
    # The OBO consent was ALSO revoked, for the agent SP resolved from the assignment.
    mock_graph.revoke_agent_obo_consent.assert_awaited_once_with(
        "agent-sp-obj-id", "mcp-sp-obj-id"
    )
    # Ordering: principalId resolved (a list_assignments) BEFORE the assignment revoke.
    order = [c[0] for c in manager.mock_calls]
    assert order.index("list_assignments") < order.index("revoke_app_role")


def test_delete_mcp_grant_keeps_consent_when_other_role_assignment_remains(entra_settings):
    """The Invoker+Admin multiplicity (load-bearing): an agent holds BOTH an Invoker AND
    an Admin assignment on the SAME MCP, sharing ONE consent grant. Revoking ONE → the
    assignment is revoked, but revoke_agent_obo_consent is NOT called (the surviving
    assignment still needs the consent for OBO)."""
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    # Pre-revoke: the agent holds Invoker (assign-1) AND Admin (assign-2). Post-revoke of
    # assign-1: the Admin assignment (assign-2) STILL remains for that agent SP.
    mock_graph.list_assignments = AsyncMock(
        side_effect=[
            [
                {
                    "id": "assign-1",
                    "principalId": "agent-sp-obj-id",
                    "principalType": "ServicePrincipal",
                    "appRoleId": "role-invoker-guid",
                },
                {
                    "id": "assign-2",
                    "principalId": "agent-sp-obj-id",
                    "principalType": "ServicePrincipal",
                    "appRoleId": "role-admin-guid",
                },
            ],
            [
                {
                    "id": "assign-2",
                    "principalId": "agent-sp-obj-id",
                    "principalType": "ServicePrincipal",
                    "appRoleId": "role-admin-guid",
                },
            ],
        ]
    )
    mock_graph.revoke_app_role = AsyncMock(return_value=None)
    mock_graph.revoke_agent_obo_consent = AsyncMock(return_value=None)

    # E12 (Task T4): the env sync runs after the kill switch regardless of the consent
    # decision — inject a registry so the agent resolves deterministically (no real AWS).
    mock_agent_registry = MagicMock()
    mock_agent_registry.list.return_value = [_make_agent(mcp_server_ids=["mcp-123"])]
    mock_agent_registry.persist_identity.return_value = None

    client, _ = _build_client(
        mock_mcp_registry=mock_mcp,
        mock_graph=mock_graph,
        mock_agent_registry=mock_agent_registry,
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete(
            "/api/v1/mcp-servers/mcp-123/grants/assign-1", headers=_headers()
        )

    assert resp.status_code == 204
    mock_graph.revoke_app_role.assert_awaited_once_with("mcp-sp-obj-id", "assign-1")
    # A surviving Admin assignment still needs the consent → it must NOT be revoked.
    mock_graph.revoke_agent_obo_consent.assert_not_called()


def test_delete_mcp_grant_consent_revoke_failure_surfaces_502(entra_settings, caplog):
    """A non-404 failure from revoke_agent_obo_consent (the consent revoke genuinely
    failed; the assignment is ALREADY deleted at that point) surfaces a 502 with a FIXED
    detail literal (no exception string leaks), and logger.exception records the real
    cause server-side. Re-revoke is idempotent, so the operator can retry."""
    import logging

    from services.graph_service import GraphError

    secret_marker = "LEAKED-CONSENT-REVOKE-INTERNAL-DETAIL"
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    mock_graph.list_assignments = AsyncMock(
        side_effect=[
            [
                {
                    "id": "assign-1",
                    "principalId": "agent-sp-obj-id",
                    "principalType": "ServicePrincipal",
                    "appRoleId": "role-invoker-guid",
                },
            ],
            [],  # post-revoke: nothing left → consent revoke is attempted.
        ]
    )
    mock_graph.revoke_app_role = AsyncMock(return_value=None)
    mock_graph.revoke_agent_obo_consent = AsyncMock(
        side_effect=GraphError(403, "Authorization_RequestDenied", message=secret_marker)
    )

    client, _ = _build_client(mock_mcp_registry=mock_mcp, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        with caplog.at_level(logging.ERROR):
            resp = client.delete(
                "/api/v1/mcp-servers/mcp-123/grants/assign-1", headers=_headers()
            )

    assert resp.status_code == 502
    # No leak: the GraphError message must not surface in the response.
    assert secret_marker not in resp.text
    # The real failure was logged server-side (logger.exception → ERROR record w/ traceback).
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "expected logger.exception to emit an ERROR record"
    assert any(r.exc_info is not None for r in error_records), (
        "expected the logged record to carry the exception traceback"
    )


def test_delete_mcp_grant_missing_assignment_still_revokes(entra_settings):
    """If the assignment_id is NOT in the pre-revoke list (already gone — the FE
    double-click race), the agent SP can't be resolved → keep today's behavior: still call
    revoke_app_role (its 404 maps to 404), and do NOT attempt a consent revoke (no agent SP
    to scope it to). The existing GraphError(404)→404 path is preserved."""
    from services.graph_service import GraphError

    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    # The target assignment_id is NOT present (a DIFFERENT agent's assignment is).
    mock_graph.list_assignments = AsyncMock(
        return_value=[
            {
                "id": "some-other-assignment",
                "principalId": "other-agent-sp",
                "principalType": "ServicePrincipal",
                "appRoleId": "role-invoker-guid",
            },
        ]
    )
    mock_graph.revoke_app_role = AsyncMock(
        side_effect=GraphError(404, "Request_ResourceNotFound")
    )
    mock_graph.revoke_agent_obo_consent = AsyncMock(return_value=None)

    client, _ = _build_client(mock_mcp_registry=mock_mcp, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete(
            "/api/v1/mcp-servers/mcp-123/grants/assign-gone", headers=_headers()
        )

    # The stale-assignment 404 path is preserved (revoke_app_role's 404 → 404).
    assert resp.status_code == 404
    # No consent revoke attempted (no agent SP resolved for the missing assignment).
    mock_graph.revoke_agent_obo_consent.assert_not_called()


# --- GET /agents/{id}/mcp-grants (the reverse join — agent_mcp_router) ----

def test_list_agent_mcp_grants_returns_reverse_joined_mcps(entra_settings):
    """The agent_mcp_router route: list_agent_mcp_grants returns raw appRoleAssignments
    with resourceId/resourceDisplayName/appRoleId/id; the route reverse-joins each
    resourceId → our McpServer (via entra_sp_id) → AgentMcpGrant[]. Assignments whose
    resourceId is NOT a known MCP SP are filtered out; unmatched appRoleId → 'Unknown'."""
    mock_agent = MagicMock()
    mock_agent.get.return_value = _make_agent()

    # Two known MCPs (by entra_sp_id) + their role ids for the appRoleId interpretation.
    mcp_a = _make_mcp(
        id="mcp-aaa",
        name="claims-mcp-de",
        entra_sp_id="mcp-sp-a",
        invoker_role_id="a-invoker",
        admin_role_id="a-admin",
    )
    mcp_b = _make_mcp(
        id="mcp-bbb",
        name="fraud-mcp-de",
        entra_sp_id="mcp-sp-b",
        invoker_role_id="b-invoker",
        admin_role_id="b-admin",
    )
    mock_mcp = MagicMock()
    mock_mcp.list.return_value = [mcp_a, mcp_b]

    mock_graph = MagicMock()
    mock_graph.list_agent_mcp_grants = AsyncMock(
        return_value=[
            {
                "id": "assign-a",
                "resourceId": "mcp-sp-a",
                "resourceDisplayName": "claims-mcp-de",
                "appRoleId": "a-invoker",
                "principalId": "agent-sp-obj-id",
            },
            {
                "id": "assign-b",
                "resourceId": "mcp-sp-b",
                "resourceDisplayName": "fraud-mcp-de",
                "appRoleId": "b-admin",
                "principalId": "agent-sp-obj-id",
            },
            {
                # NOT a known MCP SP → filtered out.
                "id": "assign-x",
                "resourceId": "some-non-mcp-sp",
                "resourceDisplayName": "Microsoft Graph",
                "appRoleId": "graph-role",
                "principalId": "agent-sp-obj-id",
            },
        ]
    )

    client, _ = _build_client(
        mock_mcp_registry=mock_mcp, mock_graph=mock_graph, mock_agent_registry=mock_agent
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/agent-123/mcp-grants", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    # The non-MCP assignment is filtered out → only 2 remain.
    assert len(body) == 2
    by_mcp = {row["mcp_id"]: row for row in body}
    assert by_mcp["mcp-aaa"]["mcp_name"] == "claims-mcp-de"
    assert by_mcp["mcp-aaa"]["role"] == "Invoker"
    assert by_mcp["mcp-aaa"]["assignment_id"] == "assign-a"
    assert by_mcp["mcp-bbb"]["role"] == "Admin"
    assert by_mcp["mcp-bbb"]["assignment_id"] == "assign-b"
    mock_graph.list_agent_mcp_grants.assert_awaited_once_with("agent-sp-obj-id")


def test_list_agent_mcp_grants_unprovisioned_agent_returns_empty(entra_settings):
    """An unprovisioned agent (no sp / status != provisioned) → [] (no graph call)."""
    mock_agent = MagicMock()
    mock_agent.get.return_value = _make_agent(identity_status="pending", entra_sp_id=None)
    mock_mcp = MagicMock()
    mock_mcp.list.return_value = []
    mock_graph = MagicMock()
    mock_graph.list_agent_mcp_grants = AsyncMock(return_value=[])

    client, _ = _build_client(
        mock_mcp_registry=mock_mcp, mock_graph=mock_graph, mock_agent_registry=mock_agent
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/agent-123/mcp-grants", headers=_headers())

    assert resp.status_code == 200
    assert resp.json() == []
    mock_graph.list_agent_mcp_grants.assert_not_called()


def test_list_agent_mcp_grants_missing_agent_404(entra_settings):
    mock_agent = MagicMock()
    mock_agent.get.return_value = None
    mock_mcp = MagicMock()
    mock_graph = MagicMock()
    client, _ = _build_client(
        mock_mcp_registry=mock_mcp, mock_graph=mock_graph, mock_agent_registry=mock_agent
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/agent-missing/mcp-grants", headers=_headers())

    assert resp.status_code == 404


def test_agent_mcp_grants_route_reachable_at_agents_prefix(entra_settings):
    """CRITIC-M2 guard: the agent_mcp_router route MUST resolve at
    /api/v1/agents/{id}/mcp-grants (a route under the /mcp-servers-prefixed router could
    NOT). A reachable-but-empty (or 404-from-handler) response proves the path is
    registered; a 404 for an UNKNOWN route would carry FastAPI's "Not Found" detail."""
    # The route is registered iff it appears on the app's route table at the expected path.
    mock_agent = MagicMock()
    mock_agent.get.return_value = _make_agent()
    mock_mcp = MagicMock()
    mock_mcp.list.return_value = []
    mock_graph = MagicMock()
    mock_graph.list_agent_mcp_grants = AsyncMock(return_value=[])
    client, mcp_grants_module = _build_client(
        mock_mcp_registry=mock_mcp, mock_graph=mock_graph, mock_agent_registry=mock_agent
    )

    # 1) The path is on the app route table (the strongest reachability proof).
    from conftest import app_route_paths

    paths = app_route_paths(client.app)
    assert "/api/v1/agents/{agent_id}/mcp-grants" in paths

    # 2) A real request resolves to the handler (200), NOT an unknown-route 404.
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/agent-123/mcp-grants", headers=_headers())
    assert resp.status_code == 200
