"""Grants + principal-search route tests (Epic 6, Task T-ROUTES).

Exercise the REAL `require_role` + `current_principal` dependency path
(`AUTH_PROVIDER=entra`) against a mocked `verify_entra_token` (no live Entra) and a
mocked `GraphService` / `AgentRegistryService` / `AgentIdentityService` (no live AWS
/ Graph / HTTP). Mirrors the RBAC-test idiom of `test_agents_routes_rbac.py`:
reset cached modules, build a minimal app with ONLY the grants + entra + agents
routers, patch the lazy singletons to return mocks.

Also covers the `create_agent` background provisioning hook (agentcore-only gate).
"""

import logging
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


def _make_agent(**overrides):
    """Build a real provisioned AgentCore Agent for mock return values."""
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
        # E24/T7: the cross-tenant grant guard fails closed on tenant_id=None, and
        # this file's fixtures predate tenant scoping — stamp the fixed tenant the
        # _build_client resolver stub reports for every grantee, so the pre-guard
        # (same-tenant OPERATOR) behavior these tests exercise is preserved.
        tenant_id="ten-1",
    )
    base.update(overrides)
    return Agent(**base)


def _build_client(
    mock_registry=None,
    mock_graph=None,
    mock_identity=None,
    mock_databricks=None,
    mock_databricks_identity=None,
):
    """Build a minimal app with the grants + entra + agents routers + mocked singletons.

    Must be called AFTER the entra env fixture so the route modules import with the
    right settings. Patches the route modules' lazy singletons so no real AWS / Graph
    is touched. Also pre-seeds the E24 tenant-resolver singleton with an
    always-global stub, and `api.routes.tenants._svc` with a fake `.get` that
    accepts ANY tenant_id — `agents.py`'s `/reprovision` and create routes now
    resolve `tenant_ctx`/validate `tenant_id`, and this file's fixtures predate
    tenant scoping (a fixed "default" tenant_id with no real Tenant record);
    global admin + an always-known tenant preserve unchanged behavior.
    """
    import api.routes.agents as agents_module
    import api.routes.grants as grants_module
    import api.routes.tenants as tenants_module
    import api.routes.users as users_module
    from services.project_resolver import ProjectContext
    from services.tenant_resolver import TenantContext

    if mock_registry is not None:
        agents_module._svc = mock_registry
    if mock_graph is not None:
        # grants.py owns the ONE GraphService singleton; agents.get_graph_service
        # delegates to it, so patching it here makes both the grants routes and the
        # /invoke route observe the mock.
        grants_module._graph_svc = mock_graph
    if mock_identity is not None:
        agents_module._identity_svc = mock_identity
    # E29/T13c+d — the ACL mirror reaches the Databricks halves through `agents.py`'s two
    # accessors, so seeding those singletons is what keeps the mirror offline.
    if mock_databricks is not None:
        agents_module._databricks_workspace_svc = mock_databricks
    if mock_databricks_identity is not None:
        agents_module._databricks_identity_svc = mock_databricks_identity

    class _GlobalResolver:
        async def resolve(self, principal):
            return TenantContext(is_global=True, tenant_ids=frozenset(), tenants=())

        async def resolve_oid_tenants(self, oid):
            # E24/T7: every grantee resolves to the fixture tenant ("ten-1") so the
            # cross-tenant grant guard sees same-tenant and pre-guard behavior holds.
            return frozenset({"ten-1"})

    users_module._tenant_resolver = _GlobalResolver()

    class _GlobalProjectResolver:
        async def resolve(self, principal):
            return ProjectContext(is_global=True, roles={})

    # E27/T5 — seed the ONE project-resolver singleton too. `/reprovision` (and the other
    # gated agent mutations) now resolve `get_project_ctx`; unseeded that builds a REAL
    # ProjectResolver + GraphService and reaches login.microsoftonline.com once per request
    # (it degrades to an empty roles map, so these tests still passed — silently online).
    # GLOBAL, because this file's fixtures predate per-project ownership (no `project_id`),
    # so `may()` short-circuits True and the pre-E27 grant/reprovision behavior these cases
    # pin is preserved; the project thresholds live in `test_agent_project_gating.py`.
    users_module._project_resolver = _GlobalProjectResolver()

    class _AnyTenantService:
        def get(self, tenant_id):
            from unittest.mock import MagicMock

            return MagicMock(id=tenant_id)

    tenants_module._svc = _AnyTenantService()

    app = FastAPI()
    app.include_router(agents_module.router, prefix="/api/v1")
    app.include_router(grants_module.router, prefix="/api/v1")
    app.include_router(grants_module.entra_router, prefix="/api/v1")
    return TestClient(app), agents_module, grants_module


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


# --- GET /agents/{id}/grants (list) --------------------------------------

def test_list_grants_viewer_ok_maps_roles(entra_settings):
    """VIEWER can list; appRoleId is mapped to Invoker/Admin via the agent's role ids;
    an unknown appRoleId maps to 'Unknown'."""
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()

    mock_graph = MagicMock()
    mock_graph.list_assignments = AsyncMock(
        return_value=[
            {
                "id": "assign-1",
                "principalId": "user-1",
                "principalDisplayName": "Maria Bauer",
                "principalType": "User",
                "appRoleId": "role-invoker-guid",
            },
            {
                "id": "assign-2",
                "principalId": "group-1",
                "principalDisplayName": "Claims Team",
                "principalType": "Group",
                "appRoleId": "role-admin-guid",
            },
            {
                "id": "assign-3",
                "principalId": "user-3",
                "principalDisplayName": "Mystery",
                "principalType": "User",
                "appRoleId": "some-other-guid",
            },
        ]
    )
    client, _, _ = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/rec-123/grants", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 3
    assert body[0]["role"] == "Invoker"
    assert body[0]["assignment_id"] == "assign-1"
    assert body[0]["principal_display"] == "Maria Bauer"
    assert body[0]["principal_type"] == "User"
    assert body[1]["role"] == "Admin"
    assert body[2]["role"] == "Unknown"
    mock_graph.list_assignments.assert_awaited_once_with("sp-obj-id")


def test_list_grants_unprovisioned_returns_empty_not_409(entra_settings):
    """An unprovisioned agent (no sp / status != provisioned) → [] (NOT 409); the FE
    handles the banner. No graph call is made."""
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent(
        identity_status="pending", entra_sp_id=None
    )
    mock_graph = MagicMock()
    mock_graph.list_assignments = AsyncMock(return_value=[])
    client, _, _ = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/rec-123/grants", headers=_headers())

    assert resp.status_code == 200
    assert resp.json() == []
    mock_graph.list_assignments.assert_not_called()


def test_list_grants_missing_agent_404(entra_settings):
    mock_registry = MagicMock()
    mock_registry.get.return_value = None
    mock_graph = MagicMock()
    client, _, _ = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/rec-missing/grants", headers=_headers())

    assert resp.status_code == 404


# --- POST /agents/{id}/grants (create) -----------------------------------

def test_create_grant_operator_maps_invoker_role(entra_settings):
    """OPERATOR can create; role 'Invoker' → the agent's invoker_role_id."""
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
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
    client, _, _ = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents/rec-123/grants",
            json={"principal_id": "user-1", "principal_type": "user", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code in (200, 201)
    body = resp.json()
    assert body["role"] == "Invoker"
    assert body["assignment_id"] == "assign-new"
    mock_graph.assign_app_role.assert_awaited_once_with(
        "sp-obj-id", "user-1", "role-invoker-guid"
    )


def test_create_grant_maps_admin_role(entra_settings):
    """role 'Admin' → the agent's admin_role_id."""
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    mock_graph = MagicMock()
    mock_graph.assign_app_role = AsyncMock(
        return_value={
            "id": "assign-new",
            "principalId": "group-1",
            "principalDisplayName": "Claims Team",
            "principalType": "Group",
            "appRoleId": "role-admin-guid",
        }
    )
    client, _, _ = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents/rec-123/grants",
            json={"principal_id": "group-1", "principal_type": "group", "role": "Admin"},
            headers=_headers(),
        )

    assert resp.status_code in (200, 201)
    assert resp.json()["role"] == "Admin"
    mock_graph.assign_app_role.assert_awaited_once_with(
        "sp-obj-id", "group-1", "role-admin-guid"
    )


def test_create_grant_viewer_forbidden(entra_settings):
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    mock_graph = MagicMock()
    mock_graph.assign_app_role = AsyncMock()
    client, _, _ = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/agents/rec-123/grants",
            json={"principal_id": "user-1", "principal_type": "user", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code == 403
    mock_graph.assign_app_role.assert_not_called()


def test_create_grant_bad_role_400(entra_settings):
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    mock_graph = MagicMock()
    mock_graph.assign_app_role = AsyncMock()
    client, _, _ = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents/rec-123/grants",
            json={"principal_id": "user-1", "principal_type": "user", "role": "Superuser"},
            headers=_headers(),
        )

    assert resp.status_code == 400
    mock_graph.assign_app_role.assert_not_called()


def test_create_grant_unprovisioned_409(entra_settings):
    """No sp / not provisioned → 409 on create (unlike list, which returns [])."""
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent(
        identity_status="pending", entra_sp_id=None
    )
    mock_graph = MagicMock()
    mock_graph.assign_app_role = AsyncMock()
    client, _, _ = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents/rec-123/grants",
            json={"principal_id": "user-1", "principal_type": "user", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code == 409
    mock_graph.assign_app_role.assert_not_called()


def test_create_grant_already_assigned_409(entra_settings):
    """A Graph 409/already-exists on assign → 409 to the caller."""
    from services.graph_service import GraphError

    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    mock_graph = MagicMock()
    mock_graph.assign_app_role = AsyncMock(
        side_effect=GraphError(409, "Request_BadRequest")
    )
    client, _, _ = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents/rec-123/grants",
            json={"principal_id": "user-1", "principal_type": "user", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code == 409


def test_create_grant_missing_agent_404(entra_settings):
    mock_registry = MagicMock()
    mock_registry.get.return_value = None
    mock_graph = MagicMock()
    mock_graph.assign_app_role = AsyncMock()
    client, _, _ = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents/rec-missing/grants",
            json={"principal_id": "user-1", "principal_type": "user", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code == 404


# --- DELETE /agents/{id}/grants/{assignment_id} --------------------------

def test_delete_grant_operator_ok_204(entra_settings):
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    mock_graph = MagicMock()
    mock_graph.revoke_app_role = AsyncMock(return_value=None)
    client, _, _ = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete(
            "/api/v1/agents/rec-123/grants/assign-1", headers=_headers()
        )

    assert resp.status_code == 204
    mock_graph.revoke_app_role.assert_awaited_once_with("sp-obj-id", "assign-1")


def test_delete_grant_viewer_forbidden(entra_settings):
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    mock_graph = MagicMock()
    mock_graph.revoke_app_role = AsyncMock()
    client, _, _ = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.delete(
            "/api/v1/agents/rec-123/grants/assign-1", headers=_headers()
        )

    assert resp.status_code == 403
    mock_graph.revoke_app_role.assert_not_called()


def test_delete_grant_missing_agent_404(entra_settings):
    mock_registry = MagicMock()
    mock_registry.get.return_value = None
    mock_graph = MagicMock()
    mock_graph.revoke_app_role = AsyncMock()
    client, _, _ = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete(
            "/api/v1/agents/rec-missing/grants/assign-1", headers=_headers()
        )

    assert resp.status_code == 404


def test_delete_grant_unprovisioned_409_no_graph_call(entra_settings):
    """An unprovisioned agent → 409 (no SP to revoke on); no graph call is made."""
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent(
        identity_status="pending", entra_sp_id=None
    )
    mock_graph = MagicMock()
    mock_graph.revoke_app_role = AsyncMock()
    client, _, _ = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete(
            "/api/v1/agents/rec-123/grants/assign-1", headers=_headers()
        )

    assert resp.status_code == 409
    mock_graph.revoke_app_role.assert_not_called()


def test_delete_grant_stale_assignment_404_not_500(entra_settings):
    """A GraphError(404) from revoke (already-deleted assignment, FE double-click race)
    → 404, NOT a raw 500."""
    from services.graph_service import GraphError

    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    mock_graph = MagicMock()
    mock_graph.revoke_app_role = AsyncMock(
        side_effect=GraphError(404, "Request_ResourceNotFound")
    )
    client, _, _ = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete(
            "/api/v1/agents/rec-123/grants/assign-stale", headers=_headers()
        )

    assert resp.status_code == 404


def test_delete_grant_other_graph_error_409_not_500(entra_settings):
    """A non-404 GraphError from revoke → 409, NOT a raw 500."""
    from services.graph_service import GraphError

    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    mock_graph = MagicMock()
    mock_graph.revoke_app_role = AsyncMock(
        side_effect=GraphError(400, "Request_BadRequest")
    )
    client, _, _ = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete(
            "/api/v1/agents/rec-123/grants/assign-1", headers=_headers()
        )

    assert resp.status_code == 409


# --- GET /entra/principals/search ----------------------------------------

def test_principal_search_viewer_ok(entra_settings):
    mock_graph = MagicMock()
    mock_graph.search_principals = AsyncMock(
        return_value=[
            {"id": "user-1", "displayName": "Maria Bauer", "type": "user", "mail": "maria@x.com"},
            {"id": "group-1", "displayName": "Claims Team", "type": "group", "mail": None},
        ]
    )
    client, _, _ = _build_client(mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/entra/principals/search", params={"q": "maria"}, headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["id"] == "user-1"
    assert body[0]["display_name"] == "Maria Bauer"
    assert body[0]["type"] == "user"
    assert body[0]["mail"] == "maria@x.com"
    assert body[1]["type"] == "group"
    mock_graph.search_principals.assert_awaited_once_with("maria")


def test_principal_search_min_length_guard_returns_empty(entra_settings):
    """A too-short query returns [] without hitting Graph."""
    mock_graph = MagicMock()
    mock_graph.search_principals = AsyncMock(return_value=[])
    client, _, _ = _build_client(mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/entra/principals/search", params={"q": "a"}, headers=_headers())

    assert resp.status_code == 200
    assert resp.json() == []
    mock_graph.search_principals.assert_not_called()


# --- create_agent provisioning hook (agentcore-only gate) ----------------

def test_create_agent_schedules_provisioning_for_agentcore_only(entra_settings):
    """An agentcore agent (arn+entra+aws_bedrock) → a background provision is scheduled
    and the 201 response carries identity_status='pending' (stamped by create() itself,
    NOT by a synchronous persist_identity in the route). A metadata agent → no
    provisioning, identity_status stays 'none'."""
    # --- agentcore agent path ---
    # create() now stamps identity_status='pending' INTO the create envelope, so the
    # service returns an agent already carrying 'pending'. The route does NOT persist.
    created_agentcore = _make_agent(identity_status="pending")
    mock_registry = MagicMock()
    mock_registry.create.return_value = created_agentcore

    mock_identity = MagicMock()
    mock_identity.provision = AsyncMock(return_value=created_agentcore)

    client, agents_module, _ = _build_client(
        mock_registry=mock_registry, mock_identity=mock_identity
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents",
            json={
                "name": "claims-triage-de",
                "purpose": "Triage claims",
                "platform": "aws_bedrock",
                "auth_type": "entra",
                "agent_arn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/agent-abc123",
                "tenant_id": "default",
            },
            headers=_headers(),
        )

    assert resp.status_code == 201
    # The 201 response carries 'pending' (stamped by create(), not the route).
    assert resp.json()["identity_status"] == "pending"
    # The route does NOT synchronously persist (no update-after-create — that was the bug).
    mock_registry.persist_identity.assert_not_called()
    # The background task scheduled provision (TestClient runs background tasks after
    # the response is sent).
    mock_identity.provision.assert_awaited_once()

    # --- metadata agent path (NOT agentcore) ---
    from models.agent import AuthType

    created_metadata = _make_agent(
        identity_status="none",
        platform=None,
        auth_type=AuthType.NONE,
        agent_arn=None,
        entra_sp_id=None,
    )
    mock_registry2 = MagicMock()
    mock_registry2.create.return_value = created_metadata
    mock_identity2 = MagicMock()
    mock_identity2.provision = AsyncMock()
    client2, _, _ = _build_client(
        mock_registry=mock_registry2, mock_identity=mock_identity2
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp2 = client2.post(
            "/api/v1/agents",
            json={"name": "metadata-only-agent", "purpose": "no identity", "tenant_id": "default"},
            headers=_headers(),
        )

    assert resp2.status_code == 201
    mock_registry2.persist_identity.assert_not_called()
    mock_identity2.provision.assert_not_called()


# --- POST /agents/{id}/reprovision ---------------------------------------

def test_reprovision_operator_sets_pending_and_schedules(entra_settings):
    """OPERATOR reprovision: first persist identity_status='pending', then schedule a
    background provision; returns 202."""
    agent = _make_agent(identity_status="failed")
    mock_registry = MagicMock()
    mock_registry.get.return_value = agent
    mock_registry.persist_identity.side_effect = lambda a: a
    mock_identity = MagicMock()
    mock_identity.provision = AsyncMock(return_value=agent)
    client, _, _ = _build_client(mock_registry=mock_registry, mock_identity=mock_identity)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/agents/rec-123/reprovision", headers=_headers())

    assert resp.status_code == 202
    assert mock_registry.persist_identity.called
    persisted = mock_registry.persist_identity.call_args[0][0]
    assert persisted.identity_status == "pending"
    mock_identity.provision.assert_awaited_once()


def test_reprovision_viewer_forbidden(entra_settings):
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    mock_identity = MagicMock()
    mock_identity.provision = AsyncMock()
    client, _, _ = _build_client(mock_registry=mock_registry, mock_identity=mock_identity)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post("/api/v1/agents/rec-123/reprovision", headers=_headers())

    assert resp.status_code == 403
    mock_identity.provision.assert_not_called()


def test_reprovision_missing_agent_404(entra_settings):
    mock_registry = MagicMock()
    mock_registry.get.return_value = None
    mock_identity = MagicMock()
    mock_identity.provision = AsyncMock()
    client, _, _ = _build_client(mock_registry=mock_registry, mock_identity=mock_identity)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/agents/rec-missing/reprovision", headers=_headers())

    assert resp.status_code == 404


def test_reprovision_non_agentcore_409(entra_settings):
    """A non-agentcore agent cannot be (re)provisioned → 409."""
    from models.agent import AuthType

    agent = _make_agent(
        platform=None, auth_type=AuthType.NONE, agent_arn=None, entra_sp_id=None,
        identity_status="none",
    )
    mock_registry = MagicMock()
    mock_registry.get.return_value = agent
    mock_identity = MagicMock()
    mock_identity.provision = AsyncMock()
    client, _, _ = _build_client(mock_registry=mock_registry, mock_identity=mock_identity)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/agents/rec-123/reprovision", headers=_headers())

    assert resp.status_code == 409
    mock_identity.provision.assert_not_called()


# --- E29/T13c+d — Databricks platform-ACL mirror + drift/re-assert -----------
#
# The mirror is the §3A claim that an Entra grant on a Databricks-governed agent means the
# platform's own door lists the user. Every fake below mirrors the REAL client's refusals, so
# a test cannot pass on a shape the workspace would reject.

_DB_HANDLE = "https://claims-triage-1234.aws.databricksapps.com"
_DB_WORKSPACE = "https://dbc-test.cloud.databricks.com"
_DB_APP = "claims-triage"
_WS_SP = "tenant-sp-client-id"


def _make_databricks_agent(**overrides):
    """A provisioned, Databricks-GOVERNED agent (handle + Entra + Databricks platform)."""
    from models.agent import AuthType, Platform

    base = dict(
        id="rec-db-1",
        platform=Platform.DATABRICKS,
        auth_type=AuthType.ENTRA,
        agent_arn=None,
        runtime_handle=_DB_HANDLE,
        runtime_kind="app",
        tenant_id="ten-1",
    )
    base.update(overrides)
    return _make_agent(**base)


class _FakeStage:
    def __init__(self, sp_client_id=_WS_SP):
        self.workspace_url = _DB_WORKSPACE
        self.sp_client_id = sp_client_id


class _FakeDatabricksIdentity:
    """Only the one private helper the route reuses: stage + token + app resolution."""

    def __init__(self, *, stage=None, error=None):
        self.stage = stage or _FakeStage()
        self.error = error
        self.calls = 0

    async def _resolve_stage_and_app(self, agent, tenant):
        self.calls += 1
        if self.error:
            raise self.error
        return self.stage, "ws-token", {"name": _DB_APP, "url": _DB_HANDLE}


def _acl(principal, kind, level="CAN_USE", inherited=False):
    return {
        "principal": principal,
        "kind": kind,
        "level": level,
        "inherited": inherited,
    }


def _baseline_acl(*extra):
    """What T13b's assert leaves on a freshly provisioned app."""
    return [
        _acl("admins", "group", "CAN_MANAGE"),
        _acl(_WS_SP, "service_principal", "CAN_MANAGE"),
        *extra,
    ]


class _FakeAclClient:
    """The app-permissions surface, recording writes and REFUSING what the real one refuses.

    Mirrors `DatabricksWorkspaceService`: the PUT rejects a list without `admins` at
    `CAN_MANAGE` (`acl_missing_admins`), a level outside the closed pair, and a list naming
    the same principal twice (users casefolded) — so a composer bug fails offline instead of
    live.
    """

    def __init__(self, acl=None, *, fail=()):
        self.acl = list(acl if acl is not None else _baseline_acl())
        self.fail = set(fail)
        self.granted = []
        self.revoked = []
        self.asserted = None

    def _boom(self, what):
        from services.databricks_workspace_service import DatabricksError

        raise DatabricksError(f"fake failure: {what}", kind="forbidden")

    async def get_app_permissions(self, workspace_url, token, app_name):
        assert workspace_url == _DB_WORKSPACE and token and app_name == _DB_APP
        if "get_app_permissions" in self.fail:
            self._boom("get")
        return [dict(e) for e in self.acl]

    async def grant_app_can_use(
        self, workspace_url, token, app_name, principal, kind="group"
    ):
        assert workspace_url == _DB_WORKSPACE and token and app_name == _DB_APP
        if "grant_app_can_use" in self.fail:
            self._boom("grant")
        self.granted.append((principal, kind))
        self.acl.append(_acl(principal, kind))

    async def revoke_app_can_use(self, workspace_url, token, app_name, principal, kind):
        assert workspace_url == _DB_WORKSPACE and token and app_name == _DB_APP
        if "revoke_app_can_use" in self.fail:
            self._boom("revoke")
        self.revoked.append((principal, kind))
        self.acl = [
            e
            for e in self.acl
            if not (
                e["kind"] == kind
                and (
                    e["principal"].casefold() == principal.casefold()
                    if kind == "user"
                    else e["principal"] == principal
                )
            )
        ]

    async def set_app_permissions(self, workspace_url, token, app_name, entries):
        from services.databricks_workspace_service import DatabricksError

        assert workspace_url == _DB_WORKSPACE and token and app_name == _DB_APP
        if "set_app_permissions" in self.fail:
            self._boom("assert")
        if not any(
            e.get("principal") == "admins"
            and e.get("kind") == "group"
            and e.get("level") == "CAN_MANAGE"
            for e in entries
        ):
            raise DatabricksError("no admins entry", kind="acl_missing_admins")
        named = set()
        for entry in entries:
            if entry.get("level") not in ("CAN_USE", "CAN_MANAGE"):
                raise DatabricksError("bad level", kind="acl_entry_invalid")
            if entry.get("inherited"):
                raise DatabricksError("inherited re-PUT", kind="acl_entry_invalid")
            principal = str(entry.get("principal") or "")
            kind = str(entry.get("kind") or "")
            if not principal or kind not in ("user", "group", "service_principal"):
                raise DatabricksError("bad entry", kind="acl_entry_invalid")
            key = (kind, principal.casefold() if kind == "user" else principal)
            if key in named:
                raise DatabricksError("duplicate principal", kind="acl_entry_invalid")
            named.add(key)
        self.asserted = [dict(e) for e in entries]
        self.acl = [_acl(e["principal"], e["kind"], e["level"]) for e in entries]


def _graph_for_mirror(
    *, assignments=None, principal=None, principals=None, get_principal_error=None
):
    """A Graph mock with the three calls the mirror uses."""
    mock_graph = MagicMock()
    mock_graph.list_assignments = AsyncMock(return_value=list(assignments or []))
    mock_graph.assign_app_role = AsyncMock(
        return_value={
            "id": "assign-new",
            "principalId": "user-1",
            "principalDisplayName": "Lars Svensson",
            "principalType": "User",
            "appRoleId": "role-invoker-guid",
        }
    )
    mock_graph.revoke_app_role = AsyncMock(return_value=None)

    async def _get_principal(oid, kind):
        if get_principal_error is not None:
            raise get_principal_error
        if principals is not None:
            return principals[oid]
        return principal if principal is not None else {
            "mail": "Lars.Svensson@example.com",
            "userPrincipalName": "lars.svensson@example.com",
        }

    mock_graph.get_principal = AsyncMock(side_effect=_get_principal)
    return mock_graph


def _databricks_client(agent=None, *, graph=None, acl=None, identity=None):
    """TestClient wired for a Databricks-governed agent + the ACL fakes."""
    mock_registry = MagicMock()
    mock_registry.get.return_value = agent if agent is not None else _make_databricks_agent()
    acl_client = acl if acl is not None else _FakeAclClient()
    identity_svc = identity if identity is not None else _FakeDatabricksIdentity()
    client, _, _ = _build_client(
        mock_registry=mock_registry,
        mock_graph=graph if graph is not None else _graph_for_mirror(),
        mock_databricks=acl_client,
        mock_databricks_identity=identity_svc,
    )
    return client, acl_client, identity_svc


# --- grant: both writes, or neither ------------------------------------------

def test_create_grant_on_databricks_agent_mirrors_a_per_user_can_use(entra_settings):
    """§3A: the Entra assignment is followed by a per-user CAN_USE keyed on `mail`."""
    graph = _graph_for_mirror()
    client, acl, _ = _databricks_client(graph=graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents/rec-db-1/grants",
            json={"principal_id": "user-1", "principal_type": "user", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code in (200, 201)
    assert acl.granted == [("Lars.Svensson@example.com", "user")]
    graph.revoke_app_role.assert_not_called()


def test_create_grant_falls_back_to_the_upn_when_mail_is_absent(entra_settings):
    graph = _graph_for_mirror(
        principal={"userPrincipalName": "lars.svensson@example.com"}
    )
    client, acl, _ = _databricks_client(graph=graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents/rec-db-1/grants",
            json={"principal_id": "user-1", "principal_type": "user", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code in (200, 201)
    assert acl.granted == [("lars.svensson@example.com", "user")]


def test_create_grant_group_principal_on_a_databricks_agent_is_refused(entra_settings):
    """A GROUP has no `user_name` to write, so the grant could never reach the app's door —
    recording it would have the Access tab claim access the platform refuses for every member.
    REFUSED before any write: no assignment, no ACL call, nothing to roll back."""
    graph = _graph_for_mirror()
    client, acl, _ = _databricks_client(graph=graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents/rec-db-1/grants",
            json={"principal_id": "group-1", "principal_type": "Group", "role": "Admin"},
            headers=_headers(),
        )

    assert resp.status_code == 422
    assert resp.json()["detail"] == (
        "group grants are not enforceable on Databricks agents yet; grant individual users"
    )
    graph.assign_app_role.assert_not_called()
    graph.revoke_app_role.assert_not_called()
    graph.get_principal.assert_not_called()
    assert acl.granted == []


def test_create_grant_group_oid_mislabeled_as_a_user_is_still_refused(entra_settings):
    """`principal_type` is display-only and never reaches Graph — Entra resolves the real type
    from the oid. So the body's claim cannot be the enforcement point: a GROUP oid labelled
    "user" is caught against Graph's TRUTHFUL `principalType` on the assignment it just made,
    the assignment is ROLLED BACK, and the same 422 is raised. No unenforceable grant is ever
    recorded."""
    graph = _graph_for_mirror()
    graph.assign_app_role = AsyncMock(
        return_value={
            "id": "assign-grp",
            "principalId": "group-1",
            "principalDisplayName": "Claims Team",
            "principalType": "Group",
            "appRoleId": "role-invoker-guid",
        }
    )
    client, acl, _ = _databricks_client(graph=graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents/rec-db-1/grants",
            json={"principal_id": "group-1", "principal_type": "user", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code == 422
    assert resp.json()["detail"] == (
        "group grants are not enforceable on Databricks agents yet; grant individual users"
    )
    graph.revoke_app_role.assert_awaited_once_with("sp-obj-id", "assign-grp")
    assert acl.granted == []


def test_create_grant_mislabeled_group_whose_rollback_fails_502s(entra_settings):
    """The rollback is the honest half of the refusal, so its failure is reported as the
    drifted state it is — not swallowed into the 422."""
    from services.graph_service import GraphError

    graph = _graph_for_mirror()
    graph.assign_app_role = AsyncMock(
        return_value={
            "id": "assign-grp",
            "principalId": "group-1",
            "principalType": "Group",
            "appRoleId": "role-invoker-guid",
        }
    )
    graph.revoke_app_role = AsyncMock(side_effect=GraphError(500, "InternalError"))
    client, acl, _ = _databricks_client(graph=graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents/rec-db-1/grants",
            json={"principal_id": "group-1", "principal_type": "user", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code == 502
    assert "could not be rolled back" in resp.json()["detail"]
    assert acl.granted == []


def test_create_grant_group_principal_on_an_agentcore_agent_is_still_accepted(entra_settings):
    """The refusal is Databricks-only — an AgentCore group grant is byte-for-byte unchanged."""
    graph = _graph_for_mirror()
    graph.assign_app_role = AsyncMock(
        return_value={
            "id": "assign-grp",
            "principalId": "group-1",
            "principalDisplayName": "Claims Team",
            "principalType": "Group",
            "appRoleId": "role-admin-guid",
        }
    )
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    acl = _FakeAclClient()
    client, _, _ = _build_client(
        mock_registry=mock_registry,
        mock_graph=graph,
        mock_databricks=acl,
        mock_databricks_identity=_FakeDatabricksIdentity(),
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents/rec-123/grants",
            json={"principal_id": "group-1", "principal_type": "group", "role": "Admin"},
            headers=_headers(),
        )

    assert resp.status_code in (200, 201)
    graph.assign_app_role.assert_awaited_once()
    assert acl.granted == []


def test_create_grant_acl_failure_rolls_the_assignment_back_and_502s(entra_settings):
    """The DELIBERATE divergence from `mcp_server_grants`' retry-forward idiom: the grant
    exists in both places or in neither."""
    graph = _graph_for_mirror()
    client, acl, _ = _databricks_client(
        graph=graph, acl=_FakeAclClient(fail={"grant_app_can_use"})
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents/rec-db-1/grants",
            json={"principal_id": "user-1", "principal_type": "user", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code == 502
    assert "grant not applied" in resp.json()["detail"]
    graph.revoke_app_role.assert_awaited_once_with("sp-obj-id", "assign-new")
    assert acl.granted == []


def test_create_grant_unresolvable_username_rolls_back_before_any_acl_write(entra_settings):
    """Entra reports neither a mail nor a UPN → nothing to mirror → roll back, don't pretend."""
    graph = _graph_for_mirror(principal={"displayName": "Nameless"})
    client, acl, identity = _databricks_client(graph=graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents/rec-db-1/grants",
            json={"principal_id": "user-1", "principal_type": "user", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "grant not applied" in detail and "user principal name" in detail
    graph.revoke_app_role.assert_awaited_once_with("sp-obj-id", "assign-new")
    assert acl.granted == []
    # The refusal never contacted Databricks at all.
    assert identity.calls == 0


def test_create_grant_rollback_failure_names_both_failures(entra_settings):
    from services.graph_service import GraphError

    graph = _graph_for_mirror()
    graph.revoke_app_role = AsyncMock(side_effect=GraphError(500, "internalError"))
    client, _, _ = _databricks_client(
        graph=graph, acl=_FakeAclClient(fail={"grant_app_can_use"})
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents/rec-db-1/grants",
            json={"principal_id": "user-1", "principal_type": "user", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "rolled back" in detail and "drifted" in detail


def test_create_grant_on_agentcore_agent_never_touches_the_acl(entra_settings):
    """AgentCore behavior is unchanged, byte for byte."""
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    graph = _graph_for_mirror()
    acl = _FakeAclClient()
    identity = _FakeDatabricksIdentity()
    client, _, _ = _build_client(
        mock_registry=mock_registry,
        mock_graph=graph,
        mock_databricks=acl,
        mock_databricks_identity=identity,
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents/rec-123/grants",
            json={"principal_id": "user-1", "principal_type": "user", "role": "Invoker"},
            headers=_headers(),
        )

    assert resp.status_code in (200, 201)
    assert acl.granted == [] and acl.asserted is None
    assert identity.calls == 0
    graph.get_principal.assert_not_called()


# --- revoke: Entra first, ACL second, never a resurrection -------------------

def _one_user_assignment(assignment_id="assign-1", principal_type="User"):
    return [
        {
            "id": assignment_id,
            "principalId": "user-1",
            "principalDisplayName": "Lars Svensson",
            "principalType": principal_type,
            "appRoleId": "role-invoker-guid",
        }
    ]


def test_delete_grant_on_databricks_agent_removes_both(entra_settings):
    graph = _graph_for_mirror(assignments=_one_user_assignment())
    acl = _FakeAclClient(_baseline_acl(_acl("lars.svensson@example.com", "user")))
    client, acl, _ = _databricks_client(graph=graph, acl=acl)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete("/api/v1/agents/rec-db-1/grants/assign-1", headers=_headers())

    assert resp.status_code == 204
    graph.revoke_app_role.assert_awaited_once_with("sp-obj-id", "assign-1")
    # Case-insensitively matched by the client — Entra's `mail` is commonly mixed case.
    assert acl.revoked == [("Lars.Svensson@example.com", "user")]
    assert all(e["kind"] != "user" for e in acl.acl)


def test_delete_grant_acl_failure_502s_and_never_restores_the_assignment(entra_settings):
    graph = _graph_for_mirror(assignments=_one_user_assignment())
    client, _, _ = _databricks_client(
        graph=graph, acl=_FakeAclClient(fail={"revoke_app_can_use"})
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete("/api/v1/agents/rec-db-1/grants/assign-1", headers=_headers())

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "grant revoked" in detail and "re-assert" in detail.lower()
    graph.revoke_app_role.assert_awaited_once_with("sp-obj-id", "assign-1")
    graph.assign_app_role.assert_not_called()


def test_delete_grant_group_assignment_skips_the_acl(entra_settings):
    graph = _graph_for_mirror(
        assignments=_one_user_assignment(principal_type="Group")
    )
    client, acl, _ = _databricks_client(graph=graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete("/api/v1/agents/rec-db-1/grants/assign-1", headers=_headers())

    assert resp.status_code == 204
    assert acl.revoked == []


def test_delete_grant_on_agentcore_agent_is_unchanged(entra_settings):
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    graph = _graph_for_mirror(assignments=_one_user_assignment())
    acl = _FakeAclClient()
    client, _, _ = _build_client(
        mock_registry=mock_registry,
        mock_graph=graph,
        mock_databricks=acl,
        mock_databricks_identity=_FakeDatabricksIdentity(),
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete("/api/v1/agents/rec-123/grants/assign-1", headers=_headers())

    assert resp.status_code == 204
    assert acl.revoked == []
    graph.list_assignments.assert_not_called()


# --- GET /grants/drift -------------------------------------------------------

def test_drift_reports_both_directions_and_ignores_the_baseline(entra_settings):
    """A hand-granted user is `unauthorized_acl`; an assignment with no entry is
    `missing_acl`; the asserted baseline (admins / tenant SP) is not drift; user comparison is
    casefolded."""
    agent = _make_databricks_agent()
    graph = _graph_for_mirror(
        assignments=[
            {"id": "a1", "principalId": "user-1", "principalType": "User"},
            {"id": "a2", "principalId": "user-2", "principalType": "User"},
            {"id": "a3", "principalId": "group-1", "principalType": "Group"},
        ],
        principals={
            "user-1": {"mail": "Lars.Svensson@example.com"},
            "user-2": {"mail": "mira.patel@example.com"},
        },
    )
    acl = _FakeAclClient(
        _baseline_acl(
            # granted, mixed case on the platform side → NOT drift
            _acl("lars.svensson@example.com", "user"),
            # hand-granted around AGP → unauthorized_acl
            _acl("rogue@example.com", "user", "CAN_MANAGE"),
        )
    )
    client, _, _ = _databricks_client(agent=agent, graph=graph, acl=acl)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/rec-db-1/grants/drift", headers=_headers())

    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert {(e["principal"], e["direction"], e["level"]) for e in entries} == {
        ("rogue@example.com", "unauthorized_acl", "CAN_MANAGE"),
        ("mira.patel@example.com", "missing_acl", "CAN_USE"),
    }
    assert all(e["kind"] == "user" for e in entries)


def test_drift_reports_a_hand_granted_group_and_service_principal_as_unauthorized(
    entra_settings,
):
    """The diff answers "does the door list match the truth", so it flags EVERY non-baseline
    direct entry — a group `CAN_USE` is the widest way around AGP and this is its only
    detector. The mirror stays per-user; only this direction is wider."""
    graph = _graph_for_mirror(assignments=[])
    acl = _FakeAclClient(
        _baseline_acl(
            _acl("Claims Team", "group", "CAN_USE"),
            _acl("stranger-sp-app-id", "service_principal", "CAN_MANAGE"),
        )
    )
    client, _, _ = _databricks_client(graph=graph, acl=acl)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/rec-db-1/grants/drift", headers=_headers())

    assert resp.status_code == 200
    assert {
        (e["principal"], e["kind"], e["level"], e["direction"])
        for e in resp.json()["entries"]
    } == {
        ("Claims Team", "group", "CAN_USE", "unauthorized_acl"),
        ("stranger-sp-app-id", "service_principal", "CAN_MANAGE", "unauthorized_acl"),
    }


def test_drift_never_reports_a_group_or_sp_as_missing(entra_settings):
    """`missing_acl` stays USERS ONLY: there is no group or service-principal entry AGP is
    ever waiting to see, so a group ASSIGNMENT produces no drift at all (it is refused at
    create, and an old one is simply not mirrorable)."""
    graph = _graph_for_mirror(
        assignments=[{"id": "a1", "principalId": "group-1", "principalType": "Group"}]
    )
    client, _, _ = _databricks_client(graph=graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/rec-db-1/grants/drift", headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["entries"] == []


def test_drift_flags_the_agents_own_service_principal(entra_settings):
    """T14b: federation is the only binding, so the invoke identity is the CALLING USER and the
    baseline is admins + tenant-SP only. An agent-SP entry on the app is therefore a standing
    non-user path to the app that no grant confers — a stranger at the door, even at CAN_USE."""
    agent = _make_databricks_agent(databricks_sp_id="agent-sp-app-id")
    graph = _graph_for_mirror(assignments=[])
    acl = _FakeAclClient(_baseline_acl(_acl("agent-sp-app-id", "service_principal")))
    client, _, _ = _databricks_client(agent=agent, graph=graph, acl=acl)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/rec-db-1/grants/drift", headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["entries"] == [
        {
            "principal": "agent-sp-app-id",
            "kind": "service_principal",
            "level": "CAN_USE",
            "direction": "unauthorized_acl",
        }
    ]


def test_drift_flags_a_baseline_principal_at_the_wrong_level(entra_settings):
    """The baseline matches on LEVEL too, so a baseline principal at a level the assert never
    writes is drift. Demoting the tenant's workspace SP to CAN_USE is the live case: it is the
    credential AGP writes ACLs with, and at CAN_USE this would be the LAST write AGP could make.
    The re-assert composes the level back and repairs it."""
    graph = _graph_for_mirror(assignments=[])
    acl = _FakeAclClient(
        [
            _acl("admins", "group", "CAN_MANAGE"),
            _acl(_WS_SP, "service_principal", "CAN_USE"),
        ]
    )
    client, _, _ = _databricks_client(graph=graph, acl=acl)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/rec-db-1/grants/drift", headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["entries"] == [
        {
            "principal": _WS_SP,
            "kind": "service_principal",
            "level": "CAN_USE",
            "direction": "unauthorized_acl",
        }
    ]


def test_drift_flags_a_granted_user_promoted_to_can_manage(entra_settings):
    """The level match extends to GRANTED USERS: a matching assignment authorizes the user at
    the CAN_USE the mirror writes, not at CAN_MANAGE. Promoting one hands app-ACL-rewrite power
    the grant never conferred — the same escalation the baseline legs catch, and the re-assert
    repairs it identically. Reported ONCE, as unauthorized_acl (the entry exists, so it is not
    also `missing_acl`)."""
    graph = _graph_for_mirror(
        assignments=[{"id": "a1", "principalId": "user-1", "principalType": "User"}]
    )
    acl = _FakeAclClient(
        _baseline_acl(_acl("lars.svensson@example.com", "user", "CAN_MANAGE"))
    )
    client, _, _ = _databricks_client(graph=graph, acl=acl)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/rec-db-1/grants/drift", headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["entries"] == [
        {
            "principal": "lars.svensson@example.com",
            "kind": "user",
            "level": "CAN_MANAGE",
            "direction": "unauthorized_acl",
        }
    ]


def test_drift_never_reports_an_inherited_entry_as_unauthorized(entra_settings):
    """A PUT cannot remove an inherited grant, so flagging it would offer a Re-assert that
    cannot fix it. It still grants real access, so it also SUPPRESSES `missing_acl`."""
    graph = _graph_for_mirror(
        assignments=[{"id": "a1", "principalId": "user-1", "principalType": "User"}],
        principals={"user-1": {"mail": "lars.svensson@example.com"}},
    )
    acl = _FakeAclClient(
        _baseline_acl(
            _acl("lars.svensson@example.com", "user", inherited=True),
            _acl("inherited-stranger@example.com", "user", inherited=True),
        )
    )
    client, _, _ = _databricks_client(graph=graph, acl=acl)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/rec-db-1/grants/drift", headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["entries"] == []


def test_drift_reports_the_oid_when_the_grantee_has_no_resolvable_username(entra_settings):
    from services.graph_service import GraphError

    graph = _graph_for_mirror(
        assignments=[{"id": "a1", "principalId": "user-ghost", "principalType": "User"}],
        get_principal_error=GraphError(404, "notFound"),
    )
    client, _, _ = _databricks_client(graph=graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/rec-db-1/grants/drift", headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["entries"] == [
        {
            "principal": "user-ghost",
            "kind": "user",
            "level": "CAN_USE",
            "direction": "missing_acl",
        }
    ]


def test_drift_dedupes_a_user_holding_two_assignments(entra_settings):
    graph = _graph_for_mirror(
        assignments=[
            {"id": "a1", "principalId": "user-1", "principalType": "User"},
            {"id": "a2", "principalId": "user-1", "principalType": "User"},
        ],
        principals={"user-1": {"mail": "lars.svensson@example.com"}},
    )
    client, _, _ = _databricks_client(graph=graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/rec-db-1/grants/drift", headers=_headers())

    assert resp.status_code == 200
    assert len(resp.json()["entries"]) == 1


def test_drift_on_an_agentcore_agent_409s(entra_settings):
    """An AgentCore agent has no platform ACL — an empty list would claim "checked, clean"."""
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    acl = _FakeAclClient()
    client, _, _ = _build_client(
        mock_registry=mock_registry,
        mock_graph=_graph_for_mirror(),
        mock_databricks=acl,
        mock_databricks_identity=_FakeDatabricksIdentity(),
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/rec-123/grants/drift", headers=_headers())

    assert resp.status_code == 409
    assert "Databricks" in resp.json()["detail"]


def test_drift_on_an_unprovisioned_databricks_agent_409s(entra_settings):
    agent = _make_databricks_agent(identity_status="pending", entra_sp_id=None)
    client, acl, identity = _databricks_client(agent=agent)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/rec-db-1/grants/drift", headers=_headers())

    assert resp.status_code == 409
    assert identity.calls == 0


def test_drift_read_failure_502s_with_a_fixed_literal(entra_settings):
    client, _, _ = _databricks_client(acl=_FakeAclClient(fail={"get_app_permissions"}))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/rec-db-1/grants/drift", headers=_headers())

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail == "the agent's platform access list could not be read"
    assert "fake failure" not in detail


def test_drift_missing_agent_404(entra_settings):
    mock_registry = MagicMock()
    mock_registry.get.return_value = None
    client, _, _ = _build_client(
        mock_registry=mock_registry, mock_graph=_graph_for_mirror()
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/rec-missing/grants/drift", headers=_headers())

    assert resp.status_code == 404


# --- POST /grants/reassert ---------------------------------------------------

def test_reassert_composes_baseline_plus_assignments_and_returns_fresh_drift(entra_settings):
    agent = _make_databricks_agent(databricks_sp_id="agent-sp-app-id")
    graph = _graph_for_mirror(
        assignments=[
            {"id": "a1", "principalId": "user-1", "principalType": "User"},
            # the SAME user again (Invoker + Admin) — one ACL entry, or the PUT is refused
            {"id": "a2", "principalId": "user-1", "principalType": "User"},
            {"id": "a3", "principalId": "user-2", "principalType": "User"},
            {"id": "a4", "principalId": "group-1", "principalType": "Group"},
        ],
        principals={
            "user-1": {"mail": "Lars.Svensson@example.com"},
            "user-2": {"userPrincipalName": "mira.patel@example.com"},
        },
    )
    acl = _FakeAclClient(_baseline_acl(_acl("rogue@example.com", "user")))
    client, acl, _ = _databricks_client(agent=agent, graph=graph, acl=acl)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/agents/rec-db-1/grants/reassert", headers=_headers())

    assert resp.status_code == 200
    assert {(e["principal"], e["kind"], e["level"]) for e in acl.asserted} == {
        ("admins", "group", "CAN_MANAGE"),
        (_WS_SP, "service_principal", "CAN_MANAGE"),
        ("Lars.Svensson@example.com", "user", "CAN_USE"),
        ("mira.patel@example.com", "user", "CAN_USE"),
    }
    # The hand-granted entry is gone, so the fresh drift the caller gets back is empty.
    assert resp.json() == {"entries": []}


def test_reassert_never_writes_the_agents_own_service_principal(entra_settings):
    """T14b: the composed list is admins + tenant-SP + assigned users, even when the record
    carries an agent SP — federation makes the calling user the invoke identity, so an agent-SP
    entry grants a path no grant confers. It is also the SELF-DRIFT regression: the write side
    must compose exactly what `_baseline_principals` treats as owned, so the fresh drift the
    re-assert answers with is clean."""
    agent = _make_databricks_agent(databricks_sp_id="agent-sp-app-id")
    graph = _graph_for_mirror(assignments=[])
    client, acl, _ = _databricks_client(agent=agent, graph=graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/agents/rec-db-1/grants/reassert", headers=_headers())

    assert resp.status_code == 200
    assert {e["kind"] for e in acl.asserted} == {"group", "service_principal"}
    assert [e["principal"] for e in acl.asserted if e["kind"] == "service_principal"] == [
        _WS_SP
    ]
    assert resp.json() == {"entries": []}


def test_reassert_reports_the_drift_it_cannot_write_away(entra_settings):
    """An assignment with no resolvable username stays `missing_acl` after a SUCCESSFUL
    re-assert — the answer is the resulting state, not an ack."""
    from services.graph_service import GraphError

    graph = _graph_for_mirror(
        assignments=[{"id": "a1", "principalId": "user-ghost", "principalType": "User"}],
        get_principal_error=GraphError(404, "notFound"),
    )
    client, acl, _ = _databricks_client(graph=graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/agents/rec-db-1/grants/reassert", headers=_headers())

    assert resp.status_code == 200
    # The oid was never written as a user_name.
    assert all(e["kind"] != "user" for e in acl.asserted)
    assert resp.json()["entries"] == [
        {
            "principal": "user-ghost",
            "kind": "user",
            "level": "CAN_USE",
            "direction": "missing_acl",
        }
    ]


def test_reassert_logs_every_entry_the_put_will_strip(entra_settings, caplog):
    """The PUT REPLACES the list: a takeover whose record omits WHO lost access is not a
    record (T13b's rule, same takeover). The pre-write read is what makes the log possible."""
    graph = _graph_for_mirror(assignments=[])
    acl = _FakeAclClient(
        _baseline_acl(
            _acl("rogue@example.com", "user", "CAN_MANAGE"),
            _acl("Claims Team", "group", "CAN_USE"),
            _acl("inherited-stranger@example.com", "user", inherited=True),
        )
    )
    client, acl, _ = _databricks_client(graph=graph, acl=acl)

    with caplog.at_level(logging.WARNING, logger="api.routes.grants"):
        with patch(
            "core.security_entra.verify_entra_token", return_value=_claims_for("operator")
        ):
            resp = client.post(
                "/api/v1/agents/rec-db-1/grants/reassert", headers=_headers()
            )

    assert resp.status_code == 200
    text = caplog.text
    assert "rogue@example.com (user) held CAN_MANAGE" in text
    assert "Claims Team (group) held CAN_USE" in text
    assert "stripped 2 pre-existing entries" in text
    # An inherited entry is NOT "stripped" — a PUT cannot remove it.
    assert "inherited-stranger@example.com (user) held" not in text
    assert "1 inherited entries survive" in text


def test_reassert_resolves_each_grantee_through_graph_once(entra_settings):
    """The drift answer REUSES the assignment resolution the composer already paid for: one
    `list_assignments` + one `get_principal` per user, not two of each (the API Gateway 30s
    ceiling is real, and `get_principal` is uncached)."""
    graph = _graph_for_mirror(
        assignments=[
            {"id": "a1", "principalId": "user-1", "principalType": "User"},
            {"id": "a2", "principalId": "user-2", "principalType": "User"},
        ],
        principals={
            "user-1": {"mail": "lars.svensson@example.com"},
            "user-2": {"mail": "mira.patel@example.com"},
        },
    )
    client, acl, _ = _databricks_client(graph=graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/agents/rec-db-1/grants/reassert", headers=_headers())

    assert resp.status_code == 200
    assert graph.list_assignments.await_count == 1
    assert graph.get_principal.await_count == 2
    # The ACL side IS re-read — before the write (for the strip log) and after it (the drift).
    assert resp.json() == {"entries": []}


def test_reassert_write_failure_502s_with_a_fixed_literal(entra_settings):
    client, _, _ = _databricks_client(acl=_FakeAclClient(fail={"set_app_permissions"}))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/agents/rec-db-1/grants/reassert", headers=_headers())

    assert resp.status_code == 502
    assert resp.json()["detail"] == "the agent's platform access list could not be written"


def test_reassert_requires_operator(entra_settings):
    """EXACTLY the grant-mutation threshold — the Access tab gates the button on the same
    `canManage`, so a stricter gate would put a 403 behind a visible button."""
    client, acl, _ = _databricks_client()

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post("/api/v1/agents/rec-db-1/grants/reassert", headers=_headers())

    assert resp.status_code == 403
    assert acl.asserted is None


def test_reassert_on_an_agentcore_agent_409s(entra_settings):
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    acl = _FakeAclClient()
    client, _, _ = _build_client(
        mock_registry=mock_registry,
        mock_graph=_graph_for_mirror(),
        mock_databricks=acl,
        mock_databricks_identity=_FakeDatabricksIdentity(),
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/agents/rec-123/grants/reassert", headers=_headers())

    assert resp.status_code == 409
    assert acl.asserted is None


def test_reassert_on_an_unprovisioned_databricks_agent_409s(entra_settings):
    agent = _make_databricks_agent(identity_status="pending", entra_sp_id=None)
    client, acl, _ = _databricks_client(agent=agent)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/agents/rec-db-1/grants/reassert", headers=_headers())

    assert resp.status_code == 409
    assert acl.asserted is None


def test_unresolvable_databricks_app_502s_without_touching_the_acl(entra_settings):
    """Stage/app resolution failure is a fixed 502 — never an upstream message."""
    from services.databricks_identity_service import ProvisioningError

    identity = _FakeDatabricksIdentity(error=ProvisioningError("no app listed"))
    client, acl, _ = _databricks_client(identity=identity)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/rec-db-1/grants/drift", headers=_headers())

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "Databricks app could not be resolved" in detail
    assert "no app listed" not in detail
