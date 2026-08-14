"""Connections admin route tests (Epic 19, Task T4).

Exercises the REAL require_role/current_principal path against a mocked
verify_entra_token (no live Entra) and a FAKE ConnectionService patched onto the
router's module-level ``_svc`` (no live AWS / Secrets Manager). Mirrors
test_users_admin_routes.py for the app/client + admin/non-admin auth-override
idiom (reset cached modules, build a minimal app with only the connections
router, patch verify_entra_token to inject the caller's role claim).
"""

from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from models.connection import (
    AuthType,
    Connection,
    ConnStatus,
    Provider,
)
from services.connection_service import ConnectionError


def _gen_pem() -> str:
    """Generate a throwaway RSA private-key PEM (no fixed key material committed)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


@pytest.fixture(autouse=True)
def reset_modules():
    import sys

    for mod in [
        "core.rbac",
        "core.security_entra",
        "core.config",
        "api.routes.connections",
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


def _build_client(fake_svc, projects=None):
    import api.routes.connections as connections_module
    import api.routes.projects as projects_module

    connections_module._svc = fake_svc  # the lazy ConnectionService singleton

    # The delete route composes the ProjectService singleton for its referential-integrity
    # guard (E23/T10). Patch it with a fake exposing list_projects() so the route never
    # builds a live ProjectService (which would touch AWS/GitHub).
    fake_project_svc = MagicMock()
    fake_project_svc.list_projects.return_value = list(projects or [])
    projects_module._svc = fake_project_svc

    app = FastAPI()
    app.include_router(connections_module.router, prefix="/api/v1")
    return TestClient(app)


def _svc_with(**methods):
    """A fake ConnectionService whose named methods return/raise as configured."""
    s = MagicMock()
    for name, val in methods.items():
        setattr(s, name, MagicMock(**val))
    return s


def _claims_for(role: str):
    role_app = {
        "viewer": "Platform.Viewer",
        "operator": "Platform.Operator",
        "admin": "Platform.Admin",
    }[role]
    return {"oid": f"{role}-oid", "preferred_username": f"{role}@x.com", "roles": [role_app]}


def _headers():
    return {"Authorization": "Bearer fake-token"}


def _sample_connection(**overrides) -> Connection:
    data = {
        "id": "conn-1",
        "provider": Provider.GITHUB,
        "org": "acme",
        "base_url": None,
        "auth_type": AuthType.PAT,
        "status": ConnStatus.CONNECTED,
        "status_detail": None,
        "account_login": "acme-bot",
        "secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:agp/git/conn-1",
        "has_secret": True,
        "last_verified_at": "2026-06-30T10:00:00+00:00",
        "created_by": "admin@x.com",
        "created_at": "2026-06-30T10:00:00+00:00",
        "updated_at": "2026-06-30T10:00:00+00:00",
    }
    data.update(overrides)
    return Connection(**data)


# --- RBAC: GET list is OPERATOR-read; every mutation stays ADMIN (viewer 403 on all) ---

def test_list_viewer_forbidden(entra_settings):
    s = _svc_with(list_connections={"return_value": []})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/admin/connections", headers=_headers())
    assert resp.status_code == 403
    s.list_connections.assert_not_called()


def test_list_operator_allowed(entra_settings):
    # Operators CONSUME the trust boundary — they read connections to pick one + resolve
    # org names when provisioning projects. The read model carries NO secret, so GET is
    # OPERATOR-gated (mutations stay ADMIN).
    s = _svc_with(list_connections={"return_value": [_sample_connection()]})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/admin/connections", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["id"] == "conn-1"
    assert "token" not in body[0]  # read model carries no secret
    s.list_connections.assert_called_once()


def test_post_operator_forbidden(entra_settings):
    # Create is a MUTATION — admin only.
    s = _svc_with(create_connection={"return_value": _sample_connection()})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/admin/connections",
            json={"provider": "github", "org": "acme", "token": "ghp_x"},
            headers=_headers(),
        )
    assert resp.status_code == 403
    s.create_connection.assert_not_called()


def test_post_viewer_forbidden(entra_settings):
    s = _svc_with(create_connection={"return_value": _sample_connection()})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/admin/connections",
            json={"provider": "github", "org": "acme", "token": "ghp_x"},
            headers=_headers(),
        )
    assert resp.status_code == 403
    s.create_connection.assert_not_called()


def test_put_token_viewer_forbidden(entra_settings):
    s = _svc_with(replace_token={"return_value": _sample_connection()})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.put(
            "/api/v1/admin/connections/conn-1/token",
            json={"token": "ghp_new"},
            headers=_headers(),
        )
    assert resp.status_code == 403
    s.replace_token.assert_not_called()


def test_put_key_viewer_forbidden(entra_settings):
    s = _svc_with(replace_key={"return_value": _sample_connection()})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.put(
            "/api/v1/admin/connections/conn-1/key",
            json={"private_key": _gen_pem()},
            headers=_headers(),
        )
    assert resp.status_code == 403
    s.replace_key.assert_not_called()


def test_delete_viewer_forbidden(entra_settings):
    s = _svc_with(delete_connection={"return_value": None})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.delete("/api/v1/admin/connections/conn-1", headers=_headers())
    assert resp.status_code == 403
    s.delete_connection.assert_not_called()


# --- POST happy path: 201 + NO token leaked ---------------------------------

def test_post_creates_connection_201_no_token_in_body(entra_settings):
    s = _svc_with(create_connection={"return_value": _sample_connection()})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections",
            json={"provider": "github", "org": "acme", "token": "ghp_secret"},
            headers=_headers(),
        )
    assert resp.status_code == 201
    body = resp.json()
    assert "token" not in body  # the read model NEVER carries the PAT
    assert body["id"] == "conn-1"
    assert body["status"] == "connected"
    # created_by is taken from the validated principal, never the body.
    _, kwargs = s.create_connection.call_args
    assert kwargs["created_by"] == "admin@x.com"


# --- PUT /{id}/key happy path: 200 + updated Connection, admin-gated ---------

def test_put_key_replaces_key_200(entra_settings):
    updated = _sample_connection(
        auth_type=AuthType.GITHUB_APP,
        app_id="123456",
        installation_id="78910",
        account_login="acme-app",
    )
    s = _svc_with(replace_key={"return_value": updated})
    client = _build_client(s)
    new_pem = _gen_pem()
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.put(
            "/api/v1/admin/connections/conn-1/key",
            json={"private_key": new_pem},
            headers=_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "private_key" not in body  # the read model NEVER carries the key
    assert body["id"] == "conn-1"
    assert body["status"] == "connected"
    s.replace_key.assert_called_once_with("conn-1", new_pem)


# --- error mapping: verify_failed → 400 with the curated reason --------------

def test_post_verify_failed_400_surfaces_reason(entra_settings):
    s = _svc_with(
        create_connection={
            "side_effect": ConnectionError(
                "token did not authenticate", kind="verify_failed"
            )
        }
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections",
            json={"provider": "github", "org": "acme", "token": "ghp_bad"},
            headers=_headers(),
        )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "token did not authenticate"


# --- error mapping: not_found → 404 fixed literal on /{id}/test --------------

def test_test_connection_not_found_404(entra_settings):
    s = _svc_with(
        test_connection={"side_effect": ConnectionError("Unknown connection", kind="not_found")}
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/connections/conn-x/test", headers=_headers())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Connection not found"


# --- DELETE happy path: 204 -------------------------------------------------

def test_delete_connection_204(entra_settings):
    s = _svc_with(delete_connection={"return_value": None})
    client = _build_client(s, projects=[])
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.delete("/api/v1/admin/connections/conn-1", headers=_headers())
    assert resp.status_code == 204
    assert resp.content == b""
    s.delete_connection.assert_called_once_with("conn-1")


# --- E23/T10: referential-integrity guard — block delete while a project refs it ---

def _project_ref(connection_id: str) -> "Project":
    from models.project import Project

    return Project(
        id="proj-1",
        name="stranded",
        connection_id=connection_id,
        tenant_id="default",
        created_by="admin@x.com",
        created_at="2026-07-14T10:00:00+00:00",
        updated_at="2026-07-14T10:00:00+00:00",
    )


def test_delete_connection_blocked_when_projects_reference_it(entra_settings):
    s = _svc_with(delete_connection={"return_value": None})
    client = _build_client(s, projects=[_project_ref("conn-1")])
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.delete("/api/v1/admin/connections/conn-1", headers=_headers())
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Connection has projects; delete them first"
    s.delete_connection.assert_not_called()  # service never reached


def test_delete_connection_succeeds_when_no_projects_reference_it(entra_settings):
    # A project exists but references a DIFFERENT connection — no block.
    s = _svc_with(delete_connection={"return_value": None})
    client = _build_client(s, projects=[_project_ref("other-conn")])
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.delete("/api/v1/admin/connections/conn-1", headers=_headers())
    assert resp.status_code == 204
    s.delete_connection.assert_called_once_with("conn-1")


# --------------------------------------------------------------------------- #
# App-via-Manifest routes (E20/U2)
# --------------------------------------------------------------------------- #

_REDIRECT = "https://app.example.com/ops/connections/callback"


# --- manifest/start: 200 with org-scoped post_url + manifest + state ---------

def test_manifest_start_returns_post_url_manifest_state(entra_settings):
    s = _svc_with(create_manifest_state={"return_value": "st-123"})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections/manifest/start",
            json={"org": "acme", "redirect_url": _REDIRECT},
            headers=_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "st-123"
    assert body["post_url"].startswith(
        "https://github.com/organizations/acme/settings/apps/new?state="
    )
    assert body["manifest"]["redirect_url"] == _REDIRECT
    assert body["manifest"]["name"] == "agp-acme"
    _, kwargs = s.create_manifest_state.call_args
    assert kwargs.get("created_by", "admin@x.com")


# --- manifest/start: non-github provider → 400 -------------------------------

def test_manifest_start_non_github_400(entra_settings):
    s = _svc_with(create_manifest_state={"return_value": "st-123"})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections/manifest/start",
            json={"org": "acme", "provider": "gitlab", "redirect_url": _REDIRECT},
            headers=_headers(),
        )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid request"
    s.create_manifest_state.assert_not_called()


# --- manifest/callback: resolved → 200 needs_install=false -------------------

def test_manifest_callback_resolved_200(entra_settings):
    connected = _sample_connection(
        auth_type=AuthType.GITHUB_APP, app_id="555", installation_id="222",
        account_login="acme-app",
    )
    s = _svc_with(complete_manifest={"return_value": (connected, False, None)})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections/manifest/callback",
            json={"code": "tempcode", "state": "st-123"},
            headers=_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["needs_install"] is False and body["install_url"] is None
    assert body["connection"]["status"] == "connected"
    assert body["connection"]["installation_id"] == "222"
    assert "private_key" not in body["connection"]
    s.complete_manifest.assert_called_once_with("tempcode", "st-123")


# --- manifest/callback: not installed → 200 needs_install=true + install_url -

def test_manifest_callback_not_installed_200(entra_settings):
    pending = _sample_connection(
        auth_type=AuthType.GITHUB_APP, app_id="555", installation_id=None,
        status=ConnStatus.PENDING, account_login=None,
    )
    install_url = "https://github.com/apps/agp-acme-provisioning/installations/new"
    s = _svc_with(complete_manifest={"return_value": (pending, True, install_url)})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections/manifest/callback",
            json={"code": "tempcode", "state": "st-123"},
            headers=_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["needs_install"] is True and body["install_url"] == install_url
    assert body["connection"]["status"] == "pending"


# --- {id}/finalize happy path: 200 connected ---------------------------------

def test_finalize_connects_200(entra_settings):
    connected = _sample_connection(
        auth_type=AuthType.GITHUB_APP, app_id="555", installation_id="222",
        account_login="acme-app",
    )
    s = _svc_with(finalize_app_connection={"return_value": connected})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections/conn-1/finalize",
            json={"installation_id": "222"},
            headers=_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "connected" and body["installation_id"] == "222"
    assert "private_key" not in body
    s.finalize_app_connection.assert_called_once_with("conn-1", "222")


def test_finalize_empty_body_auto_resolves(entra_settings):
    connected = _sample_connection(
        auth_type=AuthType.GITHUB_APP, app_id="555", installation_id="222",
    )
    s = _svc_with(finalize_app_connection={"return_value": connected})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections/conn-1/finalize",
            json={},
            headers=_headers(),
        )
    assert resp.status_code == 200
    s.finalize_app_connection.assert_called_once_with("conn-1", None)


# --- RBAC: the three new routes require ADMIN (403 for a viewer) -------------

def test_manifest_start_viewer_forbidden(entra_settings):
    s = _svc_with(create_manifest_state={"return_value": "st"})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/admin/connections/manifest/start",
            json={"org": "acme", "redirect_url": _REDIRECT},
            headers=_headers(),
        )
    assert resp.status_code == 403
    s.create_manifest_state.assert_not_called()


def test_manifest_callback_viewer_forbidden(entra_settings):
    s = _svc_with(complete_manifest={"return_value": (_sample_connection(), False, None)})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/admin/connections/manifest/callback",
            json={"code": "c", "state": "s"},
            headers=_headers(),
        )
    assert resp.status_code == 403
    s.complete_manifest.assert_not_called()


def test_finalize_viewer_forbidden(entra_settings):
    s = _svc_with(finalize_app_connection={"return_value": _sample_connection()})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/admin/connections/conn-1/finalize",
            json={},
            headers=_headers(),
        )
    assert resp.status_code == 403
    s.finalize_app_connection.assert_not_called()


# --------------------------------------------------------------------------- #
# The ADMIN OAuth-client paste (E27B/T9)
# --------------------------------------------------------------------------- #
#
# GitHub exposes an App's client SECRET through no API after creation, so for an org whose App
# already exists this one-time paste is the ONLY way to enable per-user linking. These tests pin
# the three things that make it safe: it is ADMIN-gated, the pasted secret never appears in the
# response, and a GitHub Enterprise connection is refused rather than stored into a dead end.

_PASTED_SECRET = "s3cr3t"  # noqa: S105 — a fake, asserted to be ABSENT from every response


def _app_connection(**overrides) -> Connection:
    data = {
        "auth_type": AuthType.GITHUB_APP,
        "app_id": "555",
        "installation_id": "222",
        "account_login": "acme-app",
    }
    data.update(overrides)
    return _sample_connection(**data)


def _oauth_svc_with(**methods):
    """``_svc_with`` plus a ``get_connection`` default, which the paste route reads for its
    github.com pre-check. A bare ``MagicMock`` would answer that read with a MagicMock whose
    ``.base_url`` is TRUTHY — i.e. it would model a GitHub Enterprise connection and every test
    below would trip the refusal instead of the behaviour it is actually asserting."""
    methods.setdefault("get_connection", {"return_value": _app_connection(base_url=None)})
    return _svc_with(**methods)


def test_set_oauth_client_returns_the_connection_and_never_the_secret(entra_settings):
    updated = _app_connection(client_id="Iv1.abc", has_oauth_client=True)
    s = _oauth_svc_with(set_oauth_client={"return_value": updated})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.put(
            "/api/v1/admin/connections/conn-1/oauth-client",
            json={"client_id": "Iv1.abc", "client_secret": _PASTED_SECRET},
            headers=_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "client_secret" not in body and _PASTED_SECRET not in resp.text
    assert body["has_oauth_client"] is True
    assert body["client_id"] == "Iv1.abc"
    s.set_oauth_client.assert_called_once_with("conn-1", "Iv1.abc", _PASTED_SECRET)


def test_set_oauth_client_requires_admin(entra_settings):
    # An OPERATOR may READ the connections list, but pasting a client secret is a
    # trust-boundary mutation — ADMIN only.
    s = _oauth_svc_with(set_oauth_client={"return_value": _app_connection()})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.put(
            "/api/v1/admin/connections/conn-1/oauth-client",
            json={"client_id": "Iv1.abc", "client_secret": _PASTED_SECRET},
            headers=_headers(),
        )
    assert resp.status_code == 403
    s.set_oauth_client.assert_not_called()


def test_set_oauth_client_viewer_forbidden(entra_settings):
    s = _oauth_svc_with(set_oauth_client={"return_value": _app_connection()})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.put(
            "/api/v1/admin/connections/conn-1/oauth-client",
            json={"client_id": "Iv1.abc", "client_secret": _PASTED_SECRET},
            headers=_headers(),
        )
    assert resp.status_code == 403
    s.set_oauth_client.assert_not_called()


def test_set_oauth_client_mismatch_is_400_with_a_fixed_detail(entra_settings):
    # The service verifies the pasted client_id against GET /app BEFORE storing; a pair from a
    # different App is verify_failed → 400 with the curated (secret-free) reason.
    s = _oauth_svc_with(
        set_oauth_client={
            "side_effect": ConnectionError(
                "client_id does not match the App", kind="verify_failed"
            )
        }
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.put(
            "/api/v1/admin/connections/conn-1/oauth-client",
            json={"client_id": "Iv1.wrong", "client_secret": _PASTED_SECRET},
            headers=_headers(),
        )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "client_id does not match the App"
    assert _PASTED_SECRET not in resp.text


def test_set_oauth_client_unknown_connection_is_404(entra_settings):
    s = _oauth_svc_with(
        set_oauth_client={
            "side_effect": ConnectionError("Unknown connection", kind="not_found")
        }
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.put(
            "/api/v1/admin/connections/conn-x/oauth-client",
            json={"client_id": "Iv1.abc", "client_secret": _PASTED_SECRET},
            headers=_headers(),
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Connection not found"


def test_set_oauth_client_refuses_a_github_enterprise_connection(entra_settings):
    # THE DEAD END THIS CLOSES: the service would store a base_url connection's pair happily,
    # but get_oauth_client_credentials refuses every base_url connection (the OAuth legs are
    # github.com-only), so the admin's one-shot paste would vanish into a surface that can never
    # use it — while has_oauth_client/oauth_client_ready both read TRUE and hint at nothing.
    # Refused here, at the only reachable writer, so the paste is never wasted.
    s = _svc_with(
        get_connection={"return_value": _app_connection(base_url="https://ghe.acme.dev/api/v3")},
        set_oauth_client={"return_value": _app_connection()},
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.put(
            "/api/v1/admin/connections/conn-1/oauth-client",
            json={"client_id": "Iv1.abc", "client_secret": _PASTED_SECRET},
            headers=_headers(),
        )
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "Per-user GitHub linking is available for github.com connections only"
    )
    assert _PASTED_SECRET not in resp.text
    s.set_oauth_client.assert_not_called()  # nothing stored — no partial state


def test_set_oauth_client_allows_a_github_dot_com_connection(entra_settings):
    # The mirror of the refusal: base_url None ⇒ the paste proceeds.
    s = _svc_with(
        get_connection={"return_value": _app_connection(base_url=None)},
        set_oauth_client={"return_value": _app_connection(client_id="Iv1.abc", has_oauth_client=True)},
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.put(
            "/api/v1/admin/connections/conn-1/oauth-client",
            json={"client_id": "Iv1.abc", "client_secret": _PASTED_SECRET},
            headers=_headers(),
        )
    assert resp.status_code == 200
    s.set_oauth_client.assert_called_once()


def test_set_oauth_client_secret_error_is_502(entra_settings):
    s = _oauth_svc_with(
        set_oauth_client={
            "side_effect": ConnectionError("Failed to rotate connection secret", kind="secret_error")
        }
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.put(
            "/api/v1/admin/connections/conn-1/oauth-client",
            json={"client_id": "Iv1.abc", "client_secret": _PASTED_SECRET},
            headers=_headers(),
        )
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Secret store operation failed"
    assert _PASTED_SECRET not in resp.text


# --- manifest/start now registers the USER callback slot too (E27B/T9) --------


def test_manifest_start_includes_the_link_callback_url(entra_settings):
    # Two DIFFERENT slots on one App: redirect_url is where the ADMIN lands after creating it,
    # callback_urls is where an END USER lands after authorizing it. callback_urls is settable at
    # creation time only, so the manifest must carry it or per-user linking is impossible without
    # org-admin UI work. Derived from the SPA-supplied redirect_url's origin.
    s = _svc_with(create_manifest_state={"return_value": "st-123"})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections/manifest/start",
            json={"org": "acme", "redirect_url": "https://agp.example/ops/connections/callback"},
            headers=_headers(),
        )
    assert resp.status_code == 200
    m = resp.json()["manifest"]
    assert m["callback_urls"] == ["https://agp.example/ops/github-link/callback"]
    assert m["redirect_url"] == "https://agp.example/ops/connections/callback"  # unchanged


def test_manifest_start_link_callback_matches_the_pinned_path(entra_settings):
    # The path is imported from the link service, never re-typed: GitHub matches a redirect_uri
    # against a registered callback BYTE-FOR-BYTE, so a drifted literal here silently breaks
    # every link at authorize time.
    from services.github_user_link import LINK_CALLBACK_PATH

    s = _svc_with(create_manifest_state={"return_value": "st-123"})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections/manifest/start",
            json={"org": "acme", "redirect_url": "https://agp.example:8443/ops/connections/callback"},
            headers=_headers(),
        )
    assert resp.status_code == 200
    # Port and scheme preserved; only the PATH is swapped.
    assert resp.json()["manifest"]["callback_urls"] == [
        f"https://agp.example:8443{LINK_CALLBACK_PATH}"
    ]


def test_manifest_start_link_callback_drops_query_and_fragment(entra_settings):
    # T7's _validate_redirect_uri refuses a redirect_uri carrying a query or fragment, so a
    # registered callback that carried one could never be matched by a real link attempt.
    s = _svc_with(create_manifest_state={"return_value": "st-123"})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections/manifest/start",
            json={
                "org": "acme",
                "redirect_url": "https://agp.example/ops/connections/callback?x=1#frag",
            },
            headers=_headers(),
        )
    assert resp.status_code == 200
    assert resp.json()["manifest"]["callback_urls"] == [
        "https://agp.example/ops/github-link/callback"
    ]
