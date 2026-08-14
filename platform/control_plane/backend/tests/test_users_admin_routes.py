"""Users-admin route tests (Epic 16).

Exercises the REAL require_role/current_principal path against a mocked
verify_entra_token and a mocked GraphService (no live Entra/Graph). Mirrors
test_grants_routes.py: reset cached modules, build a minimal app with only the
users_admin router, patch the lazy GraphService singleton (owned by grants.py).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def reset_modules():
    import sys

    for mod in [
        "core.rbac",
        "core.security_entra",
        "core.config",
        "api.routes.grants",
        "api.routes.users_admin",
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
    monkeypatch.setenv("ENTRA_SPA_CLIENT_ID", "spa-client-id")
    monkeypatch.setenv("ENTRA_ROLE_ADMIN", "Platform.Admin")
    monkeypatch.setenv("ENTRA_ROLE_OPERATOR", "Platform.Operator")
    monkeypatch.setenv("ENTRA_ROLE_VIEWER", "Platform.Viewer")


# resolve_platform_sp returns (sp_id, {value: id}); these are the three role ids.
ROLE_MAP = {
    "Platform.Admin": "admin-guid",
    "Platform.Operator": "operator-guid",
    "Platform.Viewer": "viewer-guid",
}


def _build_client(mock_graph):
    import api.routes.grants as grants_module
    import api.routes.users_admin as users_module

    grants_module._graph_svc = mock_graph  # the ONE GraphService singleton

    app = FastAPI()
    app.include_router(users_module.router, prefix="/api/v1")
    return TestClient(app)


def _graph_with(**methods):
    g = MagicMock()
    g.resolve_platform_sp = AsyncMock(return_value=("platform-sp-id", ROLE_MAP))
    for name, val in methods.items():
        setattr(g, name, AsyncMock(**val))
    return g


def _claims_for(role: str):
    role_app = {
        "viewer": "Platform.Viewer",
        "operator": "Platform.Operator",
        "admin": "Platform.Admin",
    }[role]
    return {"oid": f"{role}-oid", "preferred_username": f"{role}@x.com", "roles": [role_app]}


def _headers():
    return {"Authorization": "Bearer fake-token"}


# --- GET /admin/users ----------------------------------------------------

def test_list_users_admin_groups_and_maps_roles(entra_settings):
    assignments = [
        {"id": "a1", "principalId": "u1", "principalDisplayName": "Ana", "principalType": "User", "appRoleId": "admin-guid"},
        {"id": "a2", "principalId": "u2", "principalDisplayName": "Bo", "principalType": "User", "appRoleId": "viewer-guid"},
        # u3 has both operator + admin -> highest (admin) surfaces.
        {"id": "a3", "principalId": "u3", "principalDisplayName": "Cy", "principalType": "Group", "appRoleId": "operator-guid"},
        {"id": "a4", "principalId": "u3", "principalDisplayName": "Cy", "principalType": "Group", "appRoleId": "admin-guid"},
        # foreign appRoleId (not Platform.*) -> excluded entirely.
        {"id": "a5", "principalId": "u9", "principalDisplayName": "Zed", "principalType": "User", "appRoleId": "00000000-0000-0000-0000-000000000000"},
    ]
    g = _graph_with(list_assignments={"return_value": assignments})
    client = _build_client(g)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/users", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    by_id = {u["principal_id"]: u for u in body}
    assert set(by_id) == {"u1", "u2", "u3"}  # u9 (foreign role) excluded
    assert by_id["u1"]["role"] == "admin"
    assert by_id["u2"]["role"] == "viewer"
    assert by_id["u3"]["role"] == "admin"  # highest of operator+admin
    assert by_id["u3"]["principal_type"] == "Group"
    g.list_assignments.assert_awaited_once_with("platform-sp-id")


def test_list_users_viewer_forbidden(entra_settings):
    g = _graph_with(list_assignments={"return_value": []})
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/admin/users", headers=_headers())
    assert resp.status_code == 403
    g.list_assignments.assert_not_called()


def test_list_users_resolve_failure_502(entra_settings):
    from services.graph_service import GraphError

    g = MagicMock()
    g.resolve_platform_sp = AsyncMock(side_effect=GraphError(404, "platform_sp_not_found"))
    g.list_assignments = AsyncMock(return_value=[])
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/users", headers=_headers())
    assert resp.status_code == 502
    g.list_assignments.assert_not_called()  # resolve failure short-circuits before listing


# --- POST /admin/users (onboard) -----------------------------------------

def test_add_user_admin_assigns_role_201(entra_settings):
    g = _graph_with(
        assign_app_role={"return_value": {
            "id": "a-new", "principalId": "u1", "principalDisplayName": "Ana",
            "principalType": "User", "appRoleId": "operator-guid",
        }},
    )
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/users", json={"principal_id": "u1", "role": "operator"}, headers=_headers())
    assert resp.status_code == 201
    assert resp.json()["role"] == "operator"
    g.assign_app_role.assert_awaited_once_with("platform-sp-id", "u1", "operator-guid")


def test_add_user_bad_role_400(entra_settings):
    g = _graph_with(assign_app_role={})
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/users", json={"principal_id": "u1", "role": "superuser"}, headers=_headers())
    assert resp.status_code == 400
    g.assign_app_role.assert_not_called()


def test_add_user_viewer_forbidden(entra_settings):
    g = _graph_with(assign_app_role={})
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post("/api/v1/admin/users", json={"principal_id": "u1", "role": "operator"}, headers=_headers())
    assert resp.status_code == 403
    g.assign_app_role.assert_not_called()


def test_add_user_already_assigned_409(entra_settings):
    from services.graph_service import GraphError

    g = _graph_with(assign_app_role={"side_effect": GraphError(409, "Request_BadRequest")})
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/users", json={"principal_id": "u1", "role": "admin"}, headers=_headers())
    assert resp.status_code == 409


# --- PUT /admin/users/{id}/role (change) ---------------------------------

def test_change_role_assigns_then_revokes_others(entra_settings):
    # Assign-first ordering: the new (admin) role is assigned, THEN u1's OTHER Platform.*
    # roles (viewer + operator) are revoked. The just-assigned admin id (a-new) is NEVER
    # revoked, and u2's assignment is untouched. list_assignments returns the post-assign
    # state (includes the new admin assignment alongside the two stale ones).
    existing = [
        {"id": "old-1", "principalId": "u1", "appRoleId": "viewer-guid"},
        {"id": "old-2", "principalId": "u1", "appRoleId": "operator-guid"},
        {"id": "a-new", "principalId": "u1", "appRoleId": "admin-guid"},  # the just-assigned role
        {"id": "keep", "principalId": "u2", "appRoleId": "admin-guid"},  # different principal — untouched
    ]
    g = _graph_with(
        list_assignments={"return_value": existing},
        revoke_app_role={"return_value": None},
        assign_app_role={"return_value": {
            "id": "a-new", "principalId": "u1", "principalDisplayName": "Ana",
            "principalType": "User", "appRoleId": "admin-guid",
        }},
    )
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.put("/api/v1/admin/users/u1/role", json={"role": "admin"}, headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"
    # the new role is assigned first…
    g.assign_app_role.assert_awaited_once_with("platform-sp-id", "u1", "admin-guid")
    # …then ONLY u1's OTHER Platform.* assignments are revoked.
    revoked = {c.args[1] for c in g.revoke_app_role.await_args_list}
    assert revoked == {"old-1", "old-2"}
    assert "a-new" not in revoked   # the just-assigned role is never revoked
    assert "keep" not in revoked    # u2's assignment is untouched


def test_change_role_assign_fails_502_revoke_never_called(entra_settings):
    # Safety property: assign is FIRST and fails → 502 and NOTHING is revoked, so the
    # principal's existing roles are left intact (never stripped on a failed change).
    from services.graph_service import GraphError

    existing = [
        {"id": "old-1", "principalId": "u1", "appRoleId": "viewer-guid"},
        {"id": "old-2", "principalId": "u1", "appRoleId": "operator-guid"},
    ]
    g = _graph_with(
        list_assignments={"return_value": existing},
        revoke_app_role={"return_value": None},
        assign_app_role={"side_effect": GraphError(502, "graph_unavailable")},
    )
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.put("/api/v1/admin/users/u1/role", json={"role": "admin"}, headers=_headers())
    assert resp.status_code == 502
    g.revoke_app_role.assert_not_called()  # assign failed first → no role touched


def test_change_role_viewer_forbidden(entra_settings):
    g = _graph_with(list_assignments={"return_value": []}, revoke_app_role={}, assign_app_role={})
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.put("/api/v1/admin/users/u1/role", json={"role": "admin"}, headers=_headers())
    assert resp.status_code == 403
    g.assign_app_role.assert_not_called()


# --- DELETE /admin/users/{id} (remove access) ----------------------------

def test_remove_user_revokes_all_platform_roles_204(entra_settings):
    existing = [
        {"id": "r1", "principalId": "u1", "appRoleId": "admin-guid"},
        {"id": "r2", "principalId": "u1", "appRoleId": "viewer-guid"},
        {"id": "other", "principalId": "u2", "appRoleId": "admin-guid"},
    ]
    g = _graph_with(list_assignments={"return_value": existing}, revoke_app_role={"return_value": None})
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.delete("/api/v1/admin/users/u1", headers=_headers())
    assert resp.status_code == 204
    revoked = {c.args[1] for c in g.revoke_app_role.await_args_list}
    assert revoked == {"r1", "r2"}


def test_remove_user_no_platform_role_404(entra_settings):
    g = _graph_with(list_assignments={"return_value": []}, revoke_app_role={})
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.delete("/api/v1/admin/users/u1", headers=_headers())
    assert resp.status_code == 404
    g.revoke_app_role.assert_not_called()


def test_remove_user_viewer_forbidden(entra_settings):
    g = _graph_with(list_assignments={"return_value": []}, revoke_app_role={})
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.delete("/api/v1/admin/users/u1", headers=_headers())
    assert resp.status_code == 403


# --- pure helper: _platform_app_client_id resolution ---------------------

def _resolve_platform_app_client_id(monkeypatch, **env):
    """Set the given env vars, re-pop the cached config + route modules so the
    module-level `settings` singleton is rebuilt from the fresh env, then call
    the helper. `settings` is imported `from core.config import settings` at
    users_admin load time, so both modules must be re-popped after setenv."""
    import sys

    for key, val in env.items():
        monkeypatch.setenv(key, val)
    sys.modules.pop("core.config", None)
    sys.modules.pop("api.routes.users_admin", None)
    from api.routes.users_admin import _platform_app_client_id

    return _platform_app_client_id()


def test_platform_app_client_id_prefers_backend(monkeypatch):
    # Explicit override wins over everything.
    assert _resolve_platform_app_client_id(
        monkeypatch,
        ENTRA_PLATFORM_APP_CLIENT_ID="override-id",
        ENTRA_BACKEND_CLIENT_ID="backend-id",
        ENTRA_SPA_CLIENT_ID="spa-id",
    ) == "override-id"


def test_platform_app_client_id_falls_back_to_backend_not_spa(monkeypatch):
    # Override blank → BACKEND wins over SPA (the invariant: roles live on the
    # token-audience backend app, not the SPA OAuth client).
    assert _resolve_platform_app_client_id(
        monkeypatch,
        ENTRA_PLATFORM_APP_CLIENT_ID="",
        ENTRA_BACKEND_CLIENT_ID="backend-id",
        ENTRA_SPA_CLIENT_ID="spa-id",
    ) == "backend-id"


def test_platform_app_client_id_falls_back_to_spa_last(monkeypatch):
    # Only SPA set → SPA as the last resort.
    assert _resolve_platform_app_client_id(
        monkeypatch,
        ENTRA_PLATFORM_APP_CLIENT_ID="",
        ENTRA_BACKEND_CLIENT_ID="",
        ENTRA_SPA_CLIENT_ID="spa-id",
    ) == "spa-id"


# --- pure helper: platform_assignment_ids_for exclusion ------------------

def test_platform_assignment_ids_for_excludes_target_role():
    from api.routes.users_admin import platform_assignment_ids_for

    role_id_to_token = {"admin-guid": "admin", "operator-guid": "operator", "viewer-guid": "viewer"}
    assignments = [
        {"id": "keep-admin", "principalId": "u1", "appRoleId": "admin-guid"},
        {"id": "drop-viewer", "principalId": "u1", "appRoleId": "viewer-guid"},
        {"id": "foreign", "principalId": "u1", "appRoleId": "not-a-platform-guid"},  # foreign — excluded
        {"id": "other-principal", "principalId": "u2", "appRoleId": "operator-guid"},
    ]
    # Without exclusion: both of u1's Platform.* ids (foreign + u2 excluded).
    assert set(platform_assignment_ids_for(assignments, "u1", role_id_to_token)) == {"keep-admin", "drop-viewer"}
    # With exclude_app_role_id=admin-guid: the just-assigned admin role id is omitted.
    assert platform_assignment_ids_for(
        assignments, "u1", role_id_to_token, exclude_app_role_id="admin-guid"
    ) == ["drop-viewer"]
