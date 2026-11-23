"""Users-admin route tests.

Exercises the real require_role/current_principal path against a mocked verify_entra_token and a
mocked GraphService (no live Entra/Graph). Mirrors test_grants_routes.py: reset cached modules,
build a minimal app with only the users_admin router and the AppError handler, patch the lazy
GraphService singleton grants.py owns.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("reset_modules")


ROLE_MAP = {
    "Platform.Admin": "admin-guid",
    "Platform.Operator": "operator-guid",
    "Platform.Viewer": "viewer-guid",
}


def _build_client(mock_graph):
    import api.routes.grants as grants_module
    import api.routes.users_admin as users_module
    from core.errors import install_error_handlers

    grants_module._graph_svc = mock_graph

    app = FastAPI()
    install_error_handlers(app)
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


def test_list_users_admin_groups_and_maps_roles(entra_settings):
    assignments = [
        {
            "id": "a1",
            "principalId": "u1",
            "principalDisplayName": "Ana",
            "principalType": "User",
            "appRoleId": "admin-guid",
        },
        {
            "id": "a2",
            "principalId": "u2",
            "principalDisplayName": "Bo",
            "principalType": "User",
            "appRoleId": "viewer-guid",
        },
        {
            "id": "a3",
            "principalId": "u3",
            "principalDisplayName": "Cy",
            "principalType": "Group",
            "appRoleId": "operator-guid",
        },
        {
            "id": "a4",
            "principalId": "u3",
            "principalDisplayName": "Cy",
            "principalType": "Group",
            "appRoleId": "admin-guid",
        },
        {
            "id": "a5",
            "principalId": "u9",
            "principalDisplayName": "Zed",
            "principalType": "User",
            "appRoleId": "00000000-0000-0000-0000-000000000000",
        },
    ]
    g = _graph_with(list_assignments={"return_value": assignments})
    client = _build_client(g)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/users", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    by_id = {u["principal_id"]: u for u in body}
    assert set(by_id) == {"u1", "u2", "u3"}
    assert by_id["u1"]["role"] == "admin"
    assert by_id["u2"]["role"] == "viewer"
    assert by_id["u3"]["role"] == "admin"
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
    """A Graph failure on the platform SP resolve is the fixed 502 and short-circuits the listing."""
    from services.graph_errors import GraphError

    g = MagicMock()
    g.resolve_platform_sp = AsyncMock(side_effect=GraphError(404, "platform_sp_not_found"))
    g.list_assignments = AsyncMock(return_value=[])
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/users", headers=_headers())
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Failed to read platform users"
    g.list_assignments.assert_not_called()


def test_add_user_admin_assigns_role_201(entra_settings):
    g = _graph_with(
        assign_app_role={
            "return_value": {
                "id": "a-new",
                "principalId": "u1",
                "principalDisplayName": "Ana",
                "principalType": "User",
                "appRoleId": "operator-guid",
            }
        },
    )
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/users", json={"principal_id": "u1", "role": "operator"}, headers=_headers()
        )
    assert resp.status_code == 201
    assert resp.json()["role"] == "operator"
    g.assign_app_role.assert_awaited_once_with("platform-sp-id", "u1", "operator-guid")


def test_add_user_bad_role_422(entra_settings):
    g = _graph_with(assign_app_role={})
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/users", json={"principal_id": "u1", "role": "superuser"}, headers=_headers()
        )
    assert resp.status_code == 422
    g.assign_app_role.assert_not_called()


def test_change_role_bad_role_422(entra_settings):
    g = _graph_with(assign_app_role={}, list_assignments={}, revoke_app_role={})
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.put("/api/v1/admin/users/u1/role", json={"role": "superuser"}, headers=_headers())
    assert resp.status_code == 422
    g.assign_app_role.assert_not_called()
    g.revoke_app_role.assert_not_called()


def test_platform_role_is_closed_and_only_the_response_admits_the_unmapped_empty_token():
    from typing import get_args

    from pydantic import ValidationError

    from api.routes.users_admin import PlatformRole, PlatformUser, RoleAssignmentCreate, RoleChange

    assert get_args(PlatformRole) == ("admin", "operator", "viewer", "")
    user = {"principal_id": "u1", "display_name": "Ana", "principal_type": "User"}
    assert PlatformUser.model_validate({**user, "role": ""}).role == ""
    with pytest.raises(ValidationError):
        PlatformUser.model_validate({**user, "role": "superuser"})
    assert RoleAssignmentCreate.model_validate({"principal_id": "u1", "role": "admin"}).role == "admin"
    assert RoleChange.model_validate({"role": "viewer"}).role == "viewer"
    with pytest.raises(ValidationError):
        RoleAssignmentCreate.model_validate({"principal_id": "u1", "role": ""})
    with pytest.raises(ValidationError):
        RoleChange.model_validate({"role": ""})


def test_add_user_viewer_forbidden(entra_settings):
    g = _graph_with(assign_app_role={})
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/admin/users", json={"principal_id": "u1", "role": "operator"}, headers=_headers()
        )
    assert resp.status_code == 403
    g.assign_app_role.assert_not_called()


def test_add_user_already_assigned_409(entra_settings):
    from services.graph_errors import GraphError

    g = _graph_with(assign_app_role={"side_effect": GraphError(409, "Request_BadRequest")})
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/users", json={"principal_id": "u1", "role": "admin"}, headers=_headers()
        )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "principal already has a platform role"


def test_add_user_other_graph_error_502(entra_settings):
    from services.graph_errors import GraphError

    g = _graph_with(assign_app_role={"side_effect": GraphError(503, "ServiceUnavailable")})
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/users", json={"principal_id": "u1", "role": "admin"}, headers=_headers()
        )
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Failed to assign the platform role"


def test_add_user_role_missing_on_the_app_502(entra_settings):
    """The platform SP resolves but does not define the requested role: nothing is assigned."""
    g = _graph_with(assign_app_role={})
    g.resolve_platform_sp = AsyncMock(return_value=("platform-sp-id", {"Platform.Admin": "admin-guid"}))
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/users", json={"principal_id": "u1", "role": "viewer"}, headers=_headers()
        )
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Platform role is not configured"
    g.assign_app_role.assert_not_called()


def test_change_role_assigns_then_revokes_others(entra_settings):
    """Assign-first ordering: the new role is assigned, then only the principal's other platform
    roles are revoked; the just-assigned id and another principal's assignment are never revoked."""
    existing = [
        {"id": "old-1", "principalId": "u1", "appRoleId": "viewer-guid"},
        {"id": "old-2", "principalId": "u1", "appRoleId": "operator-guid"},
        {"id": "a-new", "principalId": "u1", "appRoleId": "admin-guid"},
        {"id": "keep", "principalId": "u2", "appRoleId": "admin-guid"},
    ]
    g = _graph_with(
        list_assignments={"return_value": existing},
        revoke_app_role={"return_value": None},
        assign_app_role={
            "return_value": {
                "id": "a-new",
                "principalId": "u1",
                "principalDisplayName": "Ana",
                "principalType": "User",
                "appRoleId": "admin-guid",
            }
        },
    )
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.put("/api/v1/admin/users/u1/role", json={"role": "admin"}, headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"
    g.assign_app_role.assert_awaited_once_with("platform-sp-id", "u1", "admin-guid")
    revoked = {c.args[1] for c in g.revoke_app_role.await_args_list}
    assert revoked == {"old-1", "old-2"}
    assert "a-new" not in revoked
    assert "keep" not in revoked


def test_change_role_assign_fails_502_revoke_never_called(entra_settings):
    """The assign comes first and fails: the fixed 502, and nothing is revoked, so the principal's
    existing roles are left intact."""
    from services.graph_errors import GraphError

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
    assert resp.json()["detail"] == "Failed to change the platform role"
    g.revoke_app_role.assert_not_called()


def test_change_role_already_held_409(entra_settings):
    from services.graph_errors import GraphError

    g = _graph_with(
        list_assignments={"return_value": []},
        revoke_app_role={"return_value": None},
        assign_app_role={"side_effect": GraphError(400, "Request_BadRequest")},
    )
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.put("/api/v1/admin/users/u1/role", json={"role": "admin"}, headers=_headers())
    assert resp.status_code == 409
    assert resp.json()["detail"] == "principal already has a platform role"
    g.revoke_app_role.assert_not_called()


def test_change_role_role_missing_on_the_app_502(entra_settings):
    g = _graph_with(list_assignments={"return_value": []}, revoke_app_role={}, assign_app_role={})
    g.resolve_platform_sp = AsyncMock(return_value=("platform-sp-id", {"Platform.Viewer": "viewer-guid"}))
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.put("/api/v1/admin/users/u1/role", json={"role": "admin"}, headers=_headers())
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Platform role is not configured"
    g.assign_app_role.assert_not_called()


def test_change_role_viewer_forbidden(entra_settings):
    g = _graph_with(list_assignments={"return_value": []}, revoke_app_role={}, assign_app_role={})
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.put("/api/v1/admin/users/u1/role", json={"role": "admin"}, headers=_headers())
    assert resp.status_code == 403
    g.assign_app_role.assert_not_called()


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
    assert resp.json()["detail"] == "user has no platform role to remove"
    g.revoke_app_role.assert_not_called()


def test_remove_user_graph_error_502(entra_settings):
    from services.graph_errors import GraphError

    existing = [{"id": "r1", "principalId": "u1", "appRoleId": "admin-guid"}]
    g = _graph_with(
        list_assignments={"return_value": existing},
        revoke_app_role={"side_effect": GraphError(403, "Authorization_RequestDenied")},
    )
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.delete("/api/v1/admin/users/u1", headers=_headers())
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Failed to remove platform access"


def test_remove_user_viewer_forbidden(entra_settings):
    g = _graph_with(list_assignments={"return_value": []}, revoke_app_role={})
    client = _build_client(g)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.delete("/api/v1/admin/users/u1", headers=_headers())
    assert resp.status_code == 403


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
    """The explicit override wins over everything."""
    assert (
        _resolve_platform_app_client_id(
            monkeypatch,
            ENTRA_PLATFORM_APP_CLIENT_ID="override-id",
            ENTRA_BACKEND_CLIENT_ID="backend-id",
            ENTRA_SPA_CLIENT_ID="spa-id",
        )
        == "override-id"
    )


def test_platform_app_client_id_falls_back_to_backend_not_spa(monkeypatch):
    """With the override blank the backend app wins over the SPA: roles live on the token-audience
    backend app, not on the SPA OAuth client."""
    assert (
        _resolve_platform_app_client_id(
            monkeypatch,
            ENTRA_PLATFORM_APP_CLIENT_ID="",
            ENTRA_BACKEND_CLIENT_ID="backend-id",
            ENTRA_SPA_CLIENT_ID="spa-id",
        )
        == "backend-id"
    )


def test_platform_app_client_id_falls_back_to_spa_last(monkeypatch):
    assert (
        _resolve_platform_app_client_id(
            monkeypatch,
            ENTRA_PLATFORM_APP_CLIENT_ID="",
            ENTRA_BACKEND_CLIENT_ID="",
            ENTRA_SPA_CLIENT_ID="spa-id",
        )
        == "spa-id"
    )


def test_platform_assignment_ids_for_excludes_target_role():
    """Foreign roles and other principals are left out; the excluded appRoleId is the role just assigned."""
    from services.graph_platform_users import platform_assignment_ids_for

    platform_role_ids = {"admin-guid", "operator-guid", "viewer-guid"}
    assignments = [
        {"id": "keep-admin", "principalId": "u1", "appRoleId": "admin-guid"},
        {"id": "drop-viewer", "principalId": "u1", "appRoleId": "viewer-guid"},
        {"id": "foreign", "principalId": "u1", "appRoleId": "not-a-platform-guid"},
        {"id": "other-principal", "principalId": "u2", "appRoleId": "operator-guid"},
    ]
    assert set(platform_assignment_ids_for(assignments, "u1", platform_role_ids)) == {
        "keep-admin",
        "drop-viewer",
    }
    assert platform_assignment_ids_for(
        assignments, "u1", platform_role_ids, exclude_app_role_id="admin-guid"
    ) == ["drop-viewer"]
