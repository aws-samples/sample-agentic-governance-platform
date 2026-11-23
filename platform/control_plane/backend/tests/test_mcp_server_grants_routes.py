"""Agent-to-MCP grants route tests.

Exercise the REAL `require_role` + `current_principal` dependency path (`AUTH_PROVIDER=entra`)
against a mocked `verify_entra_token` (no live Entra) and a mocked `GraphService` /
`McpServerRegistryService` / `AgentRegistryService` (no live AWS / Graph / HTTP): reset cached
modules, build a minimal app with ONLY the mcp_server_grants + mcp_servers + agents routers and
the `AppError` handler, patch the lazy singletons to return mocks.

Two routers live in `mcp_server_grants.py`: `router` (prefix `/mcp-servers`): GET/POST
`/{id}/grants` + DELETE `/{id}/grants/{aid}`; `agent_mcp_router` (prefix `/agents`): GET
`/{id}/mcp-grants`, the reverse join, which must resolve at the `/agents` prefix.

The POST does BOTH `assign_app_role` AND `grant_agent_obo_consent` (the delegated-consent
precondition for the agent's on-behalf-of invoke). Every failure renders a `GrantRevokeError`
kind: a fixed detail or a sentence the service set, never a Graph body.
"""

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("reset_modules")


def _make_mcp(**overrides):
    """Build a real provisioned MCP server (gateway) for mock return values."""
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
    )
    base.update(overrides)
    return McpServer(**base)


def _make_agent(**overrides):
    """Build a real provisioned AgentCore Agent (the grant principal / reverse-join subject)."""
    from models.agent import Agent, AgentAuthType, LifecycleState, Origin, Platform

    now = datetime.now(timezone.utc)
    base: dict[str, Any] = dict(
        id="agent-123",
        name="claims-triage-de",
        purpose="Triage claims",
        lifecycle_state=LifecycleState.APPROVED,
        origin=Origin.REGISTERED,
        platform=Platform.AWS_BEDROCK,
        auth_type=AgentAuthType.ENTRA,
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
    singleton (`mcp_grants_module._cred_svc`) — patch it there too — and
    injects the reference agent's runtime env via the identity-service singleton
    (`mcp_grants_module._identity_svc`).
    """
    import api.routes.agents as agents_module
    import api.routes.mcp_server_grants as mcp_grants_module
    import api.routes.mcp_servers as mcp_servers_module
    import api.routes.users as users_module
    from core.errors import install_error_handlers
    from services.tenant_resolver import TenantContext

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
    if mock_identity_svc is not None:
        mcp_grants_module._identity_svc = mock_identity_svc
    else:
        mcp_grants_module._identity_svc = MagicMock()

    app = FastAPI()
    install_error_handlers(app)
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
    mock_graph.grant_agent_obo_consent.assert_awaited_once_with("agent-sp-obj-id", "mcp-sp-obj-id")


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
    mock_graph.assign_app_role.assert_awaited_once_with("mcp-sp-obj-id", "agent-sp-obj-id", "role-admin-guid")
    mock_graph.grant_agent_obo_consent.assert_awaited_once_with("agent-sp-obj-id", "mcp-sp-obj-id")


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
    assert resp.json() == {"detail": "role must be 'Invoker' or 'Admin'", "code": "mcp_grant.invalid_role"}
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
    assert resp.json() == {"detail": "MCP identity is not provisioned", "code": "mcp_grant.not_provisioned"}
    mock_graph.assign_app_role.assert_not_called()


def test_create_mcp_grant_for_a_principal_that_is_no_governed_agent_403(entra_settings):
    """At OPERATOR on a non-shared MCP, a grantee that is not one of the registry's agents is
    cross-tenant by construction, refused before any Graph write."""
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    mock_graph.assign_app_role = AsyncMock()
    mock_graph.grant_agent_obo_consent = AsyncMock()
    mock_agent_registry, _ = _default_create_grant_mocks()
    mock_agent_registry.list.return_value = []
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

    assert resp.status_code == 403
    assert resp.json() == {"detail": "cross-tenant grant requires admin", "code": "mcp_grant.cross_tenant"}
    mock_graph.assign_app_role.assert_not_called()


def test_create_mcp_grant_already_assigned_409(entra_settings):
    """A Graph 409/already-exists on assign → 409 to the caller."""
    from services.graph_errors import GraphError

    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    mock_graph.assign_app_role = AsyncMock(side_effect=GraphError(409, "Request_BadRequest"))
    mock_graph.grant_agent_obo_consent = AsyncMock()
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
    assert resp.json() == {
        "detail": "principal is already assigned to this MCP",
        "code": "mcp_grant.already_assigned",
    }


def test_create_mcp_grant_graph_error_uses_fixed_literal_not_str_err(entra_settings):
    """A non-400/409 GraphError → 502 with a FIXED detail literal, NOT str(err) (which
    could leak a Graph resource message)."""
    from services.graph_errors import GraphError

    secret_marker = "LEAKED-GRAPH-RESOURCE-DETAIL"
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    mock_graph.assign_app_role = AsyncMock(side_effect=GraphError(500, "Internal", message=secret_marker))
    mock_graph.grant_agent_obo_consent = AsyncMock()
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
    assert resp.json() == {"detail": "failed to assign the role", "code": "mcp_grant.assign_failed"}


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


def test_create_grant_ensures_credential_provider(entra_settings):
    """A successful POST ALSO ensures the agent's AgentCore credential provider:
    ``ensure_agent_credential_provider`` is called with the resolved Agent (the one whose
    ``entra_sp_id == body.principal_id``), AND ONE ``persist_identity`` carries BOTH the
    provider name and the mcp_server_ids set-add. Assign + consent still fire
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

    other = _make_agent(id="agent-other", entra_sp_id="other-sp")
    target = _make_agent(id="agent-123", entra_sp_id="agent-sp-obj-id")
    mock_agent_registry = MagicMock()
    mock_agent_registry.list.return_value = [other, target]
    mock_cred_svc = MagicMock()
    mock_cred_svc.ensure_agent_credential_provider = AsyncMock(return_value="agp-agent-obo-agent-123")

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
    mock_graph.assign_app_role.assert_awaited_once_with(
        "mcp-sp-obj-id", "agent-sp-obj-id", "role-invoker-guid"
    )
    mock_graph.grant_agent_obo_consent.assert_awaited_once_with("agent-sp-obj-id", "mcp-sp-obj-id")
    mock_cred_svc.ensure_agent_credential_provider.assert_called_once_with(target)
    mock_agent_registry.persist_identity.assert_called_once_with(target)
    assert target.oauth2_credential_provider_name == "agp-agent-obo-agent-123"
    assert "mcp-123" in target.mcp_server_ids


def test_create_grant_reuses_existing_provider(entra_settings):
    """When the provider already exists, ``ensure_agent_credential_provider`` returns the
    existing name (idempotent get-or-create lives in the service). The route still calls
    it exactly once and the grant succeeds (201). With the name already on the agent, no
    provider-name re-persist fires — but the grant persists once for the mcp_server_ids
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

    target = _make_agent(oauth2_credential_provider_name="agp-agent-obo-agent-123")
    mock_agent_registry = MagicMock()
    mock_agent_registry.list.return_value = [target]
    mock_cred_svc = MagicMock()
    mock_cred_svc.ensure_agent_credential_provider = AsyncMock(return_value="agp-agent-obo-agent-123")

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
    mock_agent_registry.persist_identity.assert_called_once_with(target)


def test_create_grant_credential_failure_returns_502(entra_settings, caplog):
    """A failure inside ``ensure_agent_credential_provider`` → 502 with a FIXED detail
    literal; the detail MUST NOT contain any exception string (no secret/token leak). The REAL
    exception is logged server-side via ``logger.exception``: assert an ERROR record was emitted
    while the 502 detail stays a literal."""
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
    mock_cred_svc.ensure_agent_credential_provider = AsyncMock(side_effect=RuntimeError(secret_marker))

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
    assert secret_marker not in resp.text
    assert resp.json() == {
        "detail": "role assigned but credential-provider or runtime env wiring failed; re-grant to retry",
        "code": "mcp_grant.wiring_failed",
    }
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "expected logger.exception to emit an ERROR record"
    assert any(r.exc_info is not None for r in error_records), (
        "expected the logged record to carry the exception traceback"
    )


def test_create_grant_skips_provider_when_principal_not_governed_agent(entra_settings):
    """If the grant principal SP isn't one of OUR registry agents, the binary SP
    assignment is still valid → grant returns 201, but the provider step is SKIPPED
    (``ensure_agent_credential_provider`` NOT called) — defensive, not a 500.

    A non-governed principal's tenant is unknowable, so the cross-tenant
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


def test_grant_injects_runtime_env_after_credential_provider(entra_settings):
    """A successful POST rebuilds the full runtime env AFTER ensuring the credential
    provider: ``set_runtime_environment`` is called with the agent's runtime handle
    (``agent_arn``) and the MCP_SERVERS env shape (a JSON list with one entry per
    granted MCP + CREDENTIAL_PROVIDER_NAME; the legacy MCP_AUDIENCE / MCP_GATEWAY_URL
    keys are neutralized to ""), dispatched off-loop via anyio.to_thread.run_sync,
    AFTER ``ensure_agent_credential_provider``."""
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
    mock_cred_svc.ensure_agent_credential_provider = AsyncMock(return_value="agp-agent-obo-agent-123")
    mock_identity_svc = MagicMock()

    client, _ = _build_client(
        mock_mcp_registry=mock_mcp,
        mock_graph=mock_graph,
        mock_agent_registry=mock_agent_registry,
        mock_cred_svc=mock_cred_svc,
        mock_identity_svc=mock_identity_svc,
    )

    manager = MagicMock()
    manager.attach_mock(mock_cred_svc.ensure_agent_credential_provider, "ensure_provider")
    manager.attach_mock(mock_identity_svc.set_runtime_environment, "set_env")

    import services.agent_mcp_env as agent_mcp_env_module

    real_run_sync = agent_mcp_env_module.anyio.to_thread.run_sync
    run_sync_recorder = AsyncMock(side_effect=real_run_sync)
    with patch.object(agent_mcp_env_module.anyio.to_thread, "run_sync", run_sync_recorder):
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
    mock_identity_svc.set_runtime_environment.assert_called_once()
    call = mock_identity_svc.set_runtime_environment.call_args
    assert call.args[0] == target.agent_arn
    env = call.args[1] if len(call.args) > 1 else call.kwargs["env"]
    assert "MCP_SERVERS" in env
    servers = json.loads(env["MCP_SERVERS"])
    assert len(servers) == 1
    entry = servers[0]
    assert entry["id"] == "mcp-123"
    assert entry["audience"] == "api://agp-mcp-mcp-123"
    assert entry["gateway_url"] == "https://gw-abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
    assert "label" in entry
    assert env["CREDENTIAL_PROVIDER_NAME"] == "agp-agent-obo-agent-123"
    assert env["MCP_AUDIENCE"] == ""
    assert env["MCP_GATEWAY_URL"] == ""
    assert run_sync_recorder.await_count >= 1
    assert any(
        c.args and c.args[0] == mock_identity_svc.set_runtime_environment
        for c in run_sync_recorder.await_args_list
    )
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

    target = _make_agent(id="agent-123", entra_sp_id="agent-sp-obj-id", agent_arn=None)
    mock_agent_registry = MagicMock()
    mock_agent_registry.list.return_value = [target]
    mock_agent_registry.persist_identity.return_value = None
    mock_cred_svc = MagicMock()
    mock_cred_svc._region = "us-east-1"
    mock_cred_svc.ensure_agent_credential_provider = AsyncMock(return_value="agp-agent-obo-agent-123")
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
    mock_cred_svc.ensure_agent_credential_provider.assert_called_once_with(target)
    mock_identity_svc.set_runtime_environment.assert_not_called()


def test_grant_fails_loud_when_env_injection_raises(entra_settings, caplog):
    """A failure inside ``set_runtime_environment`` → 502 with a FIXED detail literal (no
    exception string leaks) AND ``logger.exception`` is invoked server-side (an ERROR record
    with the traceback), matching the credential-provider-failure path .
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
    mock_cred_svc.ensure_agent_credential_provider = AsyncMock(return_value="agp-agent-obo-agent-123")
    mock_identity_svc = MagicMock()
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
    assert secret_marker not in resp.text
    assert resp.json() == {
        "detail": "role assigned but credential-provider or runtime env wiring failed; re-grant to retry",
        "code": "mcp_grant.wiring_failed",
    }
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "expected logger.exception to emit an ERROR record"
    assert any(r.exc_info is not None for r in error_records), (
        "expected the logged record to carry the exception traceback"
    )


def test_delete_mcp_grant_operator_ok_204(entra_settings):
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
            [],
        ]
    )
    mock_graph.revoke_app_role = AsyncMock(return_value=None)
    mock_graph.revoke_agent_obo_consent = AsyncMock(return_value=None)
    mock_agent_registry = MagicMock()
    mock_agent_registry.list.return_value = [_make_agent(mcp_server_ids=["mcp-123"])]
    mock_agent_registry.persist_identity.return_value = None
    client, _ = _build_client(
        mock_mcp_registry=mock_mcp,
        mock_graph=mock_graph,
        mock_agent_registry=mock_agent_registry,
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete("/api/v1/mcp-servers/mcp-123/grants/assign-1", headers=_headers())

    assert resp.status_code == 204
    mock_graph.revoke_app_role.assert_awaited_once_with("mcp-sp-obj-id", "assign-1")


def test_delete_mcp_grant_viewer_forbidden(entra_settings):
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    mock_graph.revoke_app_role = AsyncMock()
    client, _ = _build_client(mock_mcp_registry=mock_mcp, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.delete("/api/v1/mcp-servers/mcp-123/grants/assign-1", headers=_headers())

    assert resp.status_code == 403
    mock_graph.revoke_app_role.assert_not_called()


def test_delete_mcp_grant_unprovisioned_409_no_graph_call(entra_settings):
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp(identity_status="pending", entra_sp_id=None)
    mock_graph = MagicMock()
    mock_graph.revoke_app_role = AsyncMock()
    client, _ = _build_client(mock_mcp_registry=mock_mcp, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete("/api/v1/mcp-servers/mcp-123/grants/assign-1", headers=_headers())

    assert resp.status_code == 409
    assert resp.json() == {"detail": "MCP identity is not provisioned", "code": "mcp_grant.not_provisioned"}
    mock_graph.revoke_app_role.assert_not_called()


def test_delete_mcp_grant_missing_mcp_404(entra_settings):
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = None
    mock_graph = MagicMock()
    mock_graph.revoke_app_role = AsyncMock()
    client, _ = _build_client(mock_mcp_registry=mock_mcp, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete("/api/v1/mcp-servers/mcp-missing/grants/assign-1", headers=_headers())

    assert resp.status_code == 404


def test_delete_mcp_grant_stale_assignment_404(entra_settings):
    """A GraphError(404) from revoke (already-deleted assignment) → 404, NOT a raw 500."""
    from services.graph_errors import GraphError

    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    mock_graph.list_assignments = AsyncMock(return_value=[])
    mock_graph.revoke_app_role = AsyncMock(side_effect=GraphError(404, "Request_ResourceNotFound"))
    mock_graph.revoke_agent_obo_consent = AsyncMock(return_value=None)
    client, _ = _build_client(mock_mcp_registry=mock_mcp, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete("/api/v1/mcp-servers/mcp-123/grants/assign-stale", headers=_headers())

    assert resp.status_code == 404
    mock_graph.revoke_agent_obo_consent.assert_not_called()


def test_delete_mcp_grant_revokes_assignment_and_consent(entra_settings):
    """A single-assignment agent: the DELETE resolves the agent SP (principalId) from the
    target assignment, calls revoke_app_role(mcp_sp, assignment_id), then — because no
    OTHER assignment remains for that agent SP — calls revoke_agent_obo_consent(agent_sp,
    mcp_sp). principalId is resolved BEFORE the revoke (it is unrecoverable afterward).
    Returns 204."""
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
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
            [],
        ]
    )
    mock_graph.revoke_app_role = AsyncMock(return_value=None)
    mock_graph.revoke_agent_obo_consent = AsyncMock(return_value=None)

    manager = MagicMock()
    manager.attach_mock(mock_graph.list_assignments, "list_assignments")
    manager.attach_mock(mock_graph.revoke_app_role, "revoke_app_role")
    manager.attach_mock(mock_graph.revoke_agent_obo_consent, "revoke_consent")

    mock_agent_registry = MagicMock()
    mock_agent_registry.list.return_value = [_make_agent(mcp_server_ids=["mcp-123"])]
    mock_agent_registry.persist_identity.return_value = None

    client, _ = _build_client(
        mock_mcp_registry=mock_mcp,
        mock_graph=mock_graph,
        mock_agent_registry=mock_agent_registry,
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete("/api/v1/mcp-servers/mcp-123/grants/assign-1", headers=_headers())

    assert resp.status_code == 204
    mock_graph.revoke_app_role.assert_awaited_once_with("mcp-sp-obj-id", "assign-1")
    mock_graph.revoke_agent_obo_consent.assert_awaited_once_with("agent-sp-obj-id", "mcp-sp-obj-id")
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

    mock_agent_registry = MagicMock()
    mock_agent_registry.list.return_value = [_make_agent(mcp_server_ids=["mcp-123"])]
    mock_agent_registry.persist_identity.return_value = None

    client, _ = _build_client(
        mock_mcp_registry=mock_mcp,
        mock_graph=mock_graph,
        mock_agent_registry=mock_agent_registry,
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete("/api/v1/mcp-servers/mcp-123/grants/assign-1", headers=_headers())

    assert resp.status_code == 204
    mock_graph.revoke_app_role.assert_awaited_once_with("mcp-sp-obj-id", "assign-1")
    mock_graph.revoke_agent_obo_consent.assert_not_called()


def test_delete_mcp_grant_consent_revoke_failure_surfaces_502(entra_settings, caplog):
    """A non-404 failure from revoke_agent_obo_consent (the consent revoke genuinely
    failed; the assignment is ALREADY deleted at that point) surfaces a 502 with a FIXED
    detail literal (no exception string leaks), and logger.exception records the real
    cause server-side. Re-revoke is idempotent, so the operator can retry."""
    import logging

    from services.graph_errors import GraphError

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
            [],
        ]
    )
    mock_graph.revoke_app_role = AsyncMock(return_value=None)
    mock_graph.revoke_agent_obo_consent = AsyncMock(
        side_effect=GraphError(403, "Authorization_RequestDenied", message=secret_marker)
    )

    client, _ = _build_client(mock_mcp_registry=mock_mcp, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        with caplog.at_level(logging.ERROR):
            resp = client.delete("/api/v1/mcp-servers/mcp-123/grants/assign-1", headers=_headers())

    assert resp.status_code == 502
    assert secret_marker not in resp.text
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
    from services.graph_errors import GraphError

    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
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
    mock_graph.revoke_app_role = AsyncMock(side_effect=GraphError(404, "Request_ResourceNotFound"))
    mock_graph.revoke_agent_obo_consent = AsyncMock(return_value=None)

    client, _ = _build_client(mock_mcp_registry=mock_mcp, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete("/api/v1/mcp-servers/mcp-123/grants/assign-gone", headers=_headers())

    assert resp.status_code == 404
    mock_graph.revoke_agent_obo_consent.assert_not_called()


def test_list_agent_mcp_grants_returns_reverse_joined_mcps(entra_settings):
    """The agent_mcp_router route: list_agent_mcp_grants returns raw appRoleAssignments
    with resourceId/resourceDisplayName/appRoleId/id; the route reverse-joins each
    resourceId → our McpServer (via entra_sp_id) → AgentMcpGrant[]. Assignments whose
    resourceId is NOT a known MCP SP are filtered out; unmatched appRoleId → 'Unknown'."""
    mock_agent = MagicMock()
    mock_agent.get.return_value = _make_agent()

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
    """The agent_mcp_router route MUST resolve at
    /api/v1/agents/{id}/mcp-grants (a route under the /mcp-servers-prefixed router could
    NOT). A reachable-but-empty (or 404-from-handler) response proves the path is
    registered; a 404 for an UNKNOWN route would carry FastAPI's "Not Found" detail."""
    mock_agent = MagicMock()
    mock_agent.get.return_value = _make_agent()
    mock_mcp = MagicMock()
    mock_mcp.list.return_value = []
    mock_graph = MagicMock()
    mock_graph.list_agent_mcp_grants = AsyncMock(return_value=[])
    client, _ = _build_client(
        mock_mcp_registry=mock_mcp, mock_graph=mock_graph, mock_agent_registry=mock_agent
    )

    from conftest import app_route_paths

    paths = app_route_paths(client.app)
    assert "/api/v1/agents/{agent_id}/mcp-grants" in paths

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/agent-123/mcp-grants", headers=_headers())
    assert resp.status_code == 200


_FAKE_TOKEN = "ghs_" + "a" * 36


@pytest.mark.parametrize(
    ("failing_call", "graph_status", "expected_status", "detail", "code"),
    [
        ("list_before", 500, 502, "failed to read the MCP's grants", "mcp_grant.read_failed"),
        (
            "list_after",
            500,
            502,
            "grant revoked but consent cleanup could not be verified; re-revoke to retry",
            "mcp_grant.read_failed",
        ),
        ("revoke", 404, 404, "grant not found", "mcp_grant.not_found"),
        ("revoke", 500, 409, "failed to revoke the grant", "mcp_grant.revoke_failed"),
        (
            "consent",
            403,
            502,
            "grant revoked but consent cleanup failed; re-revoke to retry",
            "mcp_grant.consent_revoke_failed",
        ),
    ],
    ids=["read_failed", "read_failed_after_revoke", "not_found", "revoke_failed", "consent_revoke_failed"],
)
def test_mcp_grant_error_detail_for_revoke_kinds_carries_no_upstream_body(
    entra_settings, failing_call, graph_status, expected_status, detail, code
):
    """The real revoke over a Graph whose failure message carries a token-shaped string: the
    detail is the sentence the service set and the code names the kind."""
    from services.graph_errors import GraphError

    failure = GraphError(graph_status, "Graph_Error", message=f"resource body with {_FAKE_TOKEN}")
    assignment = {"id": "assign-1", "principalId": "agent-sp-obj-id", "appRoleId": "role-invoker-guid"}
    list_effects = {
        "list_before": failure,
        "list_after": [[assignment], failure],
    }.get(failing_call, [[assignment], []])
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    mock_graph.list_assignments = AsyncMock(side_effect=list_effects)
    mock_graph.revoke_app_role = AsyncMock(side_effect=failure if failing_call == "revoke" else None)
    mock_graph.revoke_agent_obo_consent = AsyncMock(
        side_effect=failure if failing_call == "consent" else None
    )
    mock_agent_registry = MagicMock()
    mock_agent_registry.list.return_value = []
    client, _ = _build_client(
        mock_mcp_registry=mock_mcp, mock_graph=mock_graph, mock_agent_registry=mock_agent_registry
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete("/api/v1/mcp-servers/mcp-123/grants/assign-1", headers=_headers())

    assert resp.status_code == expected_status
    assert resp.json() == {"detail": detail, "code": code}
    assert _FAKE_TOKEN not in resp.text


def _registry_fault(operation: str):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": "ThrottlingException", "Message": _FAKE_TOKEN}}, operation)


def test_create_grant_registry_persist_failure_returns_502(entra_settings, caplog):
    """A registry write failing after the Entra writes is the fixed wiring 502, logged with a
    traceback, never a 500 and never the store's message."""
    import logging

    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    mock_graph.assign_app_role = AsyncMock(
        return_value={"id": "assign-new", "principalId": "agent-sp-obj-id"}
    )
    mock_graph.grant_agent_obo_consent = AsyncMock(return_value=None)
    mock_agent_registry, mock_cred_svc = _default_create_grant_mocks()
    mock_agent_registry.persist_identity.side_effect = _registry_fault("UpdateRegistryRecord")
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
    assert resp.json() == {
        "detail": "role assigned but credential-provider or runtime env wiring failed; re-grant to retry",
        "code": "mcp_grant.wiring_failed",
    }
    assert _FAKE_TOKEN not in resp.text
    assert any(r.levelno >= logging.ERROR and r.exc_info is not None for r in caplog.records)


def test_create_grant_registry_list_failure_returns_502(entra_settings):
    """The governed-agent lookup failing after the Entra writes is the same fixed wiring 502."""
    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    mock_graph.assign_app_role = AsyncMock(
        return_value={"id": "assign-new", "principalId": "agent-sp-obj-id"}
    )
    mock_graph.grant_agent_obo_consent = AsyncMock(return_value=None)
    mock_agent_registry, mock_cred_svc = _default_create_grant_mocks()
    mock_agent_registry.list.side_effect = _registry_fault("ListRegistryRecords")
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

    assert resp.status_code == 502
    assert resp.json()["code"] == "mcp_grant.wiring_failed"
    assert _FAKE_TOKEN not in resp.text
    mock_graph.grant_agent_obo_consent.assert_awaited_once()


@pytest.mark.parametrize(
    ("graph_status", "expected_status", "detail", "code"),
    [
        (400, 409, "principal is already assigned to this MCP", "mcp_grant.already_assigned"),
        (503, 502, "failed to assign the role", "mcp_grant.assign_failed"),
    ],
    ids=["consent_graph_400_is_409", "consent_graph_failure_is_502"],
)
def test_mcp_server_grants_consent_graph_error_renders_the_table(
    entra_settings, graph_status, expected_status, detail, code
):
    """A Graph refusal of the consent (400) keeps the route's historical 409; any other Graph
    failure on the consent is the fixed 502. Neither echoes Graph's message."""
    from services.graph_errors import GraphError

    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    mock_graph.assign_app_role = AsyncMock(
        return_value={"id": "assign-new", "principalId": "agent-sp-obj-id"}
    )
    mock_graph.grant_agent_obo_consent = AsyncMock(
        side_effect=GraphError(graph_status, "Graph_Error", message=f"body with {_FAKE_TOKEN}")
    )
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

    assert resp.status_code == expected_status
    assert resp.json() == {"detail": detail, "code": code}
    assert _FAKE_TOKEN not in resp.text
    mock_cred_svc.ensure_agent_credential_provider.assert_not_awaited()


def test_create_grant_transport_error_during_entra_write_returns_502(entra_settings, caplog):
    """A transport fault on the assignment itself (no Graph response at all) is the fixed wiring 502
    with a logged traceback, never a 500 and never the fault's text."""
    import logging

    import httpx

    mock_mcp = MagicMock()
    mock_mcp.get.return_value = _make_mcp()
    mock_graph = MagicMock()
    mock_graph.assign_app_role = AsyncMock(side_effect=httpx.ConnectError(f"refused {_FAKE_TOKEN}"))
    mock_graph.grant_agent_obo_consent = AsyncMock(return_value=None)
    mock_agent_registry, mock_cred_svc = _default_create_grant_mocks()
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
    assert resp.json() == {
        "detail": "role assigned but credential-provider or runtime env wiring failed; re-grant to retry",
        "code": "mcp_grant.wiring_failed",
    }
    assert _FAKE_TOKEN not in resp.text
    assert any(r.levelno >= logging.ERROR and r.exc_info is not None for r in caplog.records)
    mock_graph.grant_agent_obo_consent.assert_not_awaited()


def test_mcp_grant_schemas_are_unqualified_in_the_document():
    """The MCP grant bodies carry their own names, so the document has no schema FastAPI qualified
    with the ``mcp_server_grants__`` module path because the user-grant router defined the same
    name."""
    from main import app

    schemas = app.openapi()["components"]["schemas"]

    assert "McpGrantCreate" in schemas and "McpGrantRead" in schemas
    assert [name for name in schemas if "mcp_server_grants__" in name] == []
