"""Connections admin route tests.

The real ``require_role`` / ``current_principal`` path runs against a mocked ``verify_entra_token``;
a fake ``ConnectionService`` is patched onto the router module's ``_svc`` and the ``AppError``
handler renders ``ConnectionError`` from its table. The list is OPERATOR-read; every mutation is
ADMIN. No read model carries a credential.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from models.connection import Connection, ConnectionAuthType, ConnStatus, Provider
from services.connection_service import ConnectionError, ConnectionService
from services.github_app_auth import GitHubAppAuthError
from services.github_repo_errors import GitHubRepoError
from services.template_rollout_service import RolloutError

pytestmark = pytest.mark.usefixtures("reset_modules")


def _gen_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _build_client(fake_svc, projects=None, rollout_svc=None):
    """The delete route composes the project service for its referential check, so a fake with
    ``list_projects()`` is patched in too; the reconcile routes read ``_rollout_svc``."""
    import api.routes.connections as connections_module
    import api.routes.projects as projects_module
    from core.errors import install_error_handlers

    connections_module._svc = fake_svc
    connections_module._rollout_svc = rollout_svc

    fake_project_svc = MagicMock()
    fake_project_svc.list_projects.return_value = list(projects or [])
    projects_module._svc = fake_project_svc

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(connections_module.router, prefix="/api/v1")
    return TestClient(app)


def _svc_with(**methods):
    """A fake ConnectionService whose named methods return or raise as configured."""
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
    data: dict[str, Any] = {
        "id": "conn-1",
        "provider": Provider.GITHUB,
        "org": "acme",
        "base_url": None,
        "auth_type": ConnectionAuthType.PAT,
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


def test_list_viewer_forbidden(entra_settings):
    s = _svc_with(list_connections={"return_value": []})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/admin/connections", headers=_headers())
    assert resp.status_code == 403
    s.list_connections.assert_not_called()


def test_list_operator_allowed(entra_settings):
    """Operators read connections to pick one when provisioning a project; the read model carries
    no secret, so the list is OPERATOR-gated."""
    s = _svc_with(list_connections={"return_value": [_sample_connection()]})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/admin/connections", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["id"] == "conn-1"
    assert "token" not in body[0]
    s.list_connections.assert_called_once()


def test_post_operator_forbidden(entra_settings):
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
    assert "token" not in body
    assert body["id"] == "conn-1"
    assert body["status"] == "connected"
    _, kwargs = s.create_connection.call_args
    assert kwargs["created_by"] == "admin@x.com"


def test_put_key_replaces_key_200(entra_settings):
    updated = _sample_connection(
        auth_type=ConnectionAuthType.GITHUB_APP,
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
    assert "private_key" not in body
    assert body["id"] == "conn-1"
    assert body["status"] == "connected"
    s.replace_key.assert_called_once_with("conn-1", new_pem)


def test_post_verify_failed_400_surfaces_reason(entra_settings):
    s = _svc_with(
        create_connection={"side_effect": ConnectionError("token did not authenticate", kind="verify_failed")}
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections",
            json={"provider": "github", "org": "acme", "token": "ghp_bad"},
            headers=_headers(),
        )
    assert resp.status_code == 400
    assert resp.json() == {"detail": "token did not authenticate", "code": "connection.verify_failed"}


_FAKE_TOKEN = "ghs_" + "a" * 36
_FAKE_PAT = "ghp_" + "a" * 36
_UPSTREAM_BODY = f'{{"message": "Bad credentials", "token": "{_FAKE_TOKEN}"}}'
_MALFORMED_BASE_URLS = [
    pytest.param("https://ghe.acme.dev\x00/api/v3", "InvalidURL", id="nul-byte"),
    pytest.param("https://xn--zzz-.acme.dev/api/v3", "IDNAError", id="bad-idna-label"),
]


def test_connection_error_detail_for_verify_failed_carries_no_upstream_body(entra_settings):
    """``verify_failed`` sends the message to the client, so the message must be safe by
    construction: through the real service, a minter that saw a provider body raises its fixed
    text, and neither the body nor a token-shaped string reaches the detail."""

    def _minter(*args, **kwargs):
        assert "ghs_" in _UPSTREAM_BODY
        raise GitHubAppAuthError("GitHub declined the installation token request (HTTP 401)")

    svc = ConnectionService(secrets_client=MagicMock(), mint_installation_token=_minter)
    client = _build_client(svc)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections",
            json={
                "provider": "github",
                "org": "acme",
                "auth_type": "github_app",
                "app_id": "555",
                "installation_id": "222",
                "private_key": _gen_pem(),
            },
            headers=_headers(),
        )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "GitHub declined the installation token request (HTTP 401)"
    assert "ghs_" not in resp.text
    assert "Bad credentials" not in resp.text


@pytest.mark.parametrize("base_url, failure", _MALFORMED_BASE_URLS)
def test_connection_error_detail_for_verify_failed_on_a_malformed_base_url_pat(
    entra_settings, base_url, failure
):
    """A ``base_url`` httpx refuses to send to (a NUL byte, a label IDNA refuses to encode) is
    ``verify_failed`` on the PAT path, not a 500: the real service and verifier over a real client that
    never reaches the network, and the detail is the verifier's unreachable sentence, never the token."""
    svc = ConnectionService(secrets_client=MagicMock())
    client = _build_client(svc)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections",
            json={"provider": "github", "org": "acme", "token": _FAKE_PAT, "base_url": base_url},
            headers=_headers(),
        )
    assert resp.status_code == 400
    assert resp.json()["code"] == "connection.verify_failed"
    assert resp.json()["detail"].startswith("could not reach github")
    assert "ghp_" not in resp.text


@pytest.mark.parametrize("base_url, failure", _MALFORMED_BASE_URLS)
def test_connection_error_detail_for_verify_failed_on_a_malformed_base_url_app(
    entra_settings, base_url, failure
):
    """The same malformed ``base_url`` on the GitHub App path fails at the real minter with its fixed
    text plus the exception's type name, never the URL or the key."""
    svc = ConnectionService(secrets_client=MagicMock())
    client = _build_client(svc)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections",
            json={
                "provider": "github",
                "org": "acme",
                "auth_type": "github_app",
                "app_id": "555",
                "installation_id": "222",
                "private_key": _gen_pem(),
                "base_url": base_url,
            },
            headers=_headers(),
        )
    assert resp.status_code == 400
    assert resp.json() == {
        "detail": f"could not reach GitHub to mint installation token ({failure})",
        "code": "connection.verify_failed",
    }
    assert "ghe.acme.dev" not in resp.text


def test_connections_not_found_is_404(entra_settings):
    """The domain route's wire for ``not_found``, the row ``builds.py``, ``github_link.py`` and
    ``projects.py`` render through the same table."""
    s = _svc_with(test_connection={"side_effect": ConnectionError("Unknown connection", kind="not_found")})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/connections/conn-x/test", headers=_headers())
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Connection not found", "code": "connection.not_found"}


def test_delete_connection_204(entra_settings):
    s = _svc_with(delete_connection={"return_value": None})
    client = _build_client(s, projects=[])
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.delete("/api/v1/admin/connections/conn-1", headers=_headers())
    assert resp.status_code == 204
    assert resp.content == b""
    s.delete_connection.assert_called_once_with("conn-1")


def _project_ref(connection_id: str):
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
    assert resp.json() == {
        "detail": "Connection has projects; delete them first",
        "code": "connection.has_projects",
    }
    s.delete_connection.assert_not_called()


def test_delete_connection_succeeds_when_no_projects_reference_it(entra_settings):
    s = _svc_with(delete_connection={"return_value": None})
    client = _build_client(s, projects=[_project_ref("other-conn")])
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.delete("/api/v1/admin/connections/conn-1", headers=_headers())
    assert resp.status_code == 204
    s.delete_connection.assert_called_once_with("conn-1")


_REDIRECT = "https://app.example.com/ops/connections/callback"


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
    assert body["post_url"].startswith("https://github.com/organizations/acme/settings/apps/new?state=")
    assert body["manifest"]["redirect_url"] == _REDIRECT
    assert body["manifest"]["name"] == "agp-acme"
    _, kwargs = s.create_manifest_state.call_args
    assert kwargs.get("created_by", "admin@x.com")


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
    assert resp.json() == {"detail": "Invalid request", "code": "connection.bad_request"}
    s.create_manifest_state.assert_not_called()


def test_manifest_callback_resolved_200(entra_settings):
    connected = _sample_connection(
        auth_type=ConnectionAuthType.GITHUB_APP,
        app_id="555",
        installation_id="222",
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


def test_manifest_callback_not_installed_200(entra_settings):
    pending = _sample_connection(
        auth_type=ConnectionAuthType.GITHUB_APP,
        app_id="555",
        installation_id=None,
        status=ConnStatus.PENDING,
        account_login=None,
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


def test_finalize_connects_200(entra_settings):
    connected = _sample_connection(
        auth_type=ConnectionAuthType.GITHUB_APP,
        app_id="555",
        installation_id="222",
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
        auth_type=ConnectionAuthType.GITHUB_APP,
        app_id="555",
        installation_id="222",
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


_PASTED_SECRET = "s3cr3t"


def _app_connection(**overrides) -> Connection:
    data: dict[str, Any] = {
        "auth_type": ConnectionAuthType.GITHUB_APP,
        "app_id": "555",
        "installation_id": "222",
        "account_login": "acme-app",
    }
    data.update(overrides)
    return _sample_connection(**data)


def _oauth_svc_with(**methods):
    """``_svc_with`` plus a ``get_connection`` default for the paste route's github.com pre-check:
    a bare ``MagicMock``'s ``.base_url`` is truthy and would model a GitHub Enterprise connection."""
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
    s = _oauth_svc_with(
        set_oauth_client={
            "side_effect": ConnectionError("client_id does not match the App", kind="verify_failed")
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
        set_oauth_client={"side_effect": ConnectionError("Unknown connection", kind="not_found")}
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
    """The service would store a ``base_url`` connection's pair, but the OAuth legs are
    github.com-only, so the one-shot paste is refused here before anything is written."""
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
    assert resp.json()["detail"] == ("Per-user GitHub linking is available for github.com connections only")
    assert _PASTED_SECRET not in resp.text
    s.set_oauth_client.assert_not_called()


def test_set_oauth_client_allows_a_github_dot_com_connection(entra_settings):
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
    assert resp.json() == {"detail": "Secret store operation failed", "code": "connection.secret_error"}
    assert _PASTED_SECRET not in resp.text


def test_manifest_start_includes_the_link_callback_url(entra_settings):
    """``redirect_url`` is where the admin lands after creating the App; ``callback_urls`` is where
    an end user lands after authorizing it, settable at creation only, derived from the same origin."""
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
    assert m["redirect_url"] == "https://agp.example/ops/connections/callback"


def test_manifest_start_link_callback_matches_the_pinned_path(entra_settings):
    """GitHub matches a redirect_uri against a registered callback byte for byte, so the path is
    the link service's constant; scheme and port are preserved and only the path is swapped."""
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
    assert resp.json()["manifest"]["callback_urls"] == [f"https://agp.example:8443{LINK_CALLBACK_PATH}"]


def test_manifest_start_link_callback_drops_query_and_fragment(entra_settings):
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
    assert resp.json()["manifest"]["callback_urls"] == ["https://agp.example/ops/github-link/callback"]


_RECONCILE_ROUTES = [
    ("reconcile", "get", "/api/v1/admin/connections/conn-1/rollout/reconcile", None),
    ("rollout", "post", "/api/v1/admin/connections/conn-1/rollout", {"template_names": []}),
    ("adopt", "post", "/api/v1/admin/connections/conn-1/templates/adopt", {"repo_name": "x"}),
]
_ROLLOUT_KINDS = [
    ("not_found", 404, "Unknown base template"),
    ("repo_not_found", 404, "Repository not found in the org"),
    ("conflict", 409, "Template already registered"),
    ("rollout_error", 502, "Template rollout failed"),
    ("validation", 422, "Invalid template name or connection id"),
]


def _reconcile_call(client, method, path, body):
    if body is None:
        return client.get(path, headers=_headers())
    return client.post(path, json=body, headers=_headers())


@pytest.mark.parametrize(
    "kind,status,detail", _ROLLOUT_KINDS, ids=[f"{kind} is {status}" for kind, status, _ in _ROLLOUT_KINDS]
)
@pytest.mark.parametrize(
    "svc_method,method,path,body", _RECONCILE_ROUTES, ids=[r[0] for r in _RECONCILE_ROUTES]
)
def test_connections_rollout_error_kinds_render_the_route_map(
    svc_method, method, path, body, kind, status, detail, entra_settings
):
    """``RolloutError`` renders through its table on every route that lets it escape: each kind's
    status and fixed detail, never the service's message."""
    rollout = MagicMock()
    getattr(rollout, svc_method).side_effect = RolloutError("secret internals", kind=kind)
    client = _build_client(_svc_with(), rollout_svc=rollout)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = _reconcile_call(client, method, path, body)
    assert resp.status_code == status
    assert resp.json() == {"detail": detail, "code": f"rollout.{kind}"}
    assert "secret internals" not in resp.text


@pytest.mark.parametrize(
    "svc_method,method,path,body", _RECONCILE_ROUTES, ids=[r[0] for r in _RECONCILE_ROUTES]
)
def test_connections_github_repo_error_is_502(svc_method, method, path, body, entra_settings):
    rollout = MagicMock()
    getattr(rollout, svc_method).side_effect = GitHubRepoError("secret internals")
    client = _build_client(_svc_with(), rollout_svc=rollout)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = _reconcile_call(client, method, path, body)
    assert resp.status_code == 502
    assert resp.json() == {"detail": "Template rollout failed", "code": "github_repo.provider_error"}
    assert "secret internals" not in resp.text


@pytest.mark.parametrize(
    "svc_method,method,path,body", _RECONCILE_ROUTES, ids=[r[0] for r in _RECONCILE_ROUTES]
)
def test_connections_reconcile_routes_render_connection_not_found(
    svc_method, method, path, body, entra_settings
):
    rollout = MagicMock()
    getattr(rollout, svc_method).side_effect = ConnectionError("Unknown connection", kind="not_found")
    client = _build_client(_svc_with(), rollout_svc=rollout)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = _reconcile_call(client, method, path, body)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Connection not found", "code": "connection.not_found"}


def test_the_reconcile_read_answers_every_origin_and_state_with_todays_bytes(entra_settings):
    from services.template_rollout_service import ReconcileItem, ReconcileView

    rollout = MagicMock()
    rollout.reconcile.return_value = ReconcileView(
        templates=[
            ReconcileItem(
                name="seed-a", origin="seed", state="registered_present", default_branch="main", head_sha="a1"
            ),
            ReconcileItem(name="uploaded", origin="registry", state="registered_missing"),
            ReconcileItem(name="theirs", origin="org", state="unregistered_present"),
        ],
        infra_repo=ReconcileItem(name="infra", origin="seed", state="seed_absent"),
    )
    client = _build_client(_svc_with(), rollout_svc=rollout)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/connections/conn-1/rollout/reconcile", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert [(row["origin"], row["state"]) for row in body["templates"]] == [
        ("seed", "registered_present"),
        ("registry", "registered_missing"),
        ("org", "unregistered_present"),
    ]
    assert body["infra_repo"] == {
        "name": "infra",
        "origin": "seed",
        "state": "seed_absent",
        "default_branch": None,
        "head_sha": None,
        "infra_template_id": None,
    }


def test_reconcile_item_view_origin_and_state_are_closed():
    from pydantic import ValidationError

    from api.routes.connections import ReconcileItemView

    row = {"name": "t", "origin": "seed", "state": "seed_absent"}
    with pytest.raises(ValidationError):
        ReconcileItemView.model_validate({**row, "state": "quarantined"})
    with pytest.raises(ValidationError):
        ReconcileItemView.model_validate({**row, "origin": "elsewhere"})
