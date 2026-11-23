"""The per-user GitHub link routes: the real ``require_role``/``current_principal`` path against a
mocked ``verify_entra_token``, a fake link service on the router's module-level ``_svc``, and a fake
``ConnectionService`` on ``api.routes.connections._svc`` for the ``org`` field. DynamoDB and Secrets
Manager are never faked because they are never reached.

Two wiring gates are pinned because nothing fails loudly at runtime without them: the secret prefix
from settings (an empty one would write bare-UUID secrets outside every IAM condition) and a
non-empty ``allowed_origins`` (the service fails open without it)."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from models.connection import Connection, ConnectionAuthType, ConnStatus, Provider
from models.github_link import GitHubUserLink, LinkStatus
from services.connection_service import ConnectionError
from services.github_user_link import GitHubLinkError, GitHubUserLinkService

pytestmark = pytest.mark.usefixtures("reset_modules")

_OID = "viewer-oid"
_CALLBACK = "https://console.example.com/ops/github-link/callback"
_LINK_ROWS = [
    ("not_found", 404, "GitHub link not found"),
    ("connection_missing", 404, "GitHub link not found"),
    ("bad_request", 400, "Invalid request"),
    ("conflict", 409, "That GitHub account is already linked to another user"),
    ("oauth_client_missing", 409, "This org connection has no GitHub OAuth client — ask an admin to add one"),
    ("refresh_in_progress", 409, "GitHub token refresh in progress — retry"),
    ("link_revoked", 409, "Your GitHub authorization was revoked — reconnect your account"),
    ("provider_error", 502, "GitHub request failed"),
    ("secret_error", 502, "Secret store operation failed"),
]


def _headers():
    return {"Authorization": "Bearer fake-token"}


def _claims_for(role: str, oid: str = _OID):
    role_app = {
        "viewer": "Platform.Viewer",
        "operator": "Platform.Operator",
        "admin": "Platform.Admin",
    }[role]
    return {"oid": oid, "preferred_username": f"{role}@x.com", "roles": [role_app]}


def _connection(**overrides) -> Connection:
    data: dict[str, Any] = {
        "id": "conn-1",
        "provider": Provider.GITHUB,
        "org": "acme",
        "base_url": None,
        "auth_type": ConnectionAuthType.GITHUB_APP,
        "status": ConnStatus.CONNECTED,
        "app_id": "12345",
        "client_id": "Iv1.abc",
        "has_oauth_client": True,
        "secret_arn": "arn:aws:secretsmanager:us-east-1:redacted:secret:agp-dev/git/conn-1",
        "has_secret": True,
        "created_by": "admin@x.com",
        "created_at": "2026-07-28T10:00:00+00:00",
        "updated_at": "2026-07-28T10:00:00+00:00",
    }
    data.update(overrides)
    return Connection(**data)


def _link(**overrides) -> GitHubUserLink:
    data: dict[str, Any] = {
        "id": "link-1",
        "principal_oid": _OID,
        "connection_id": "conn-1",
        "github_id": 4242,
        "github_login": "octocat",
        "status": LinkStatus.LINKED,
        "secret_arn": "arn:aws:secretsmanager:us-east-1:redacted:secret:agp-dev/ghu/link-1",
        "token_version": 0,
        "last_verified_at": "2026-07-28T11:00:00+00:00",
        "created_at": "2026-07-28T10:00:00+00:00",
        "updated_at": "2026-07-28T11:00:00+00:00",
    }
    data.update(overrides)
    return GitHubUserLink(**data)


def _real_connection_org(connections_module):
    """The real service's ``connection_org``, reading through whatever ``connections_module._svc``
    is at call time, so the fake link service translates a connection failure exactly as the wired
    one does."""
    real = GitHubUserLinkService(
        table_name="",
        secret_prefix="agp-test/github-user-link/",
        client_credentials_loader=lambda connection_id: ("", ""),
        connection_org_loader=lambda connection_id: connections_module._svc.get_connection(connection_id).org,
        secrets_client=MagicMock(),
    )
    return real.connection_org


def _build_client(link_svc, connections=None, get_connection=None):
    import api.routes.connections as connections_module
    import api.routes.github_link as github_link_module
    from core.errors import install_error_handlers

    github_link_module._svc = link_svc
    link_svc.connection_org = _real_connection_org(connections_module)

    fake_conn_svc = MagicMock()
    fake_conn_svc.list_connections.return_value = list(
        connections if connections is not None else [_connection()]
    )
    if get_connection is not None:
        fake_conn_svc.get_connection = get_connection
    else:
        fake_conn_svc.get_connection.return_value = _connection()
    connections_module._svc = fake_conn_svc

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(github_link_module.router, prefix="/api/v1")
    return TestClient(app), fake_conn_svc


def _svc_with(**methods):
    s = MagicMock()
    for name, val in methods.items():
        setattr(s, name, MagicMock(**val))
    return s


def test_get_view_joins_links_and_connections(entra_settings):
    s = _svc_with(list_for_principal={"return_value": [_link()]})
    client, _ = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/me/github-link", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["connections"] == [{"connection_id": "conn-1", "org": "acme", "oauth_client_ready": True}]
    assert body["links"] == [
        {
            "connection_id": "conn-1",
            "org": "acme",
            "linked": True,
            "status": "linked",
            "github_login": "octocat",
            "last_verified_at": "2026-07-28T11:00:00+00:00",
        }
    ]
    s.list_for_principal.assert_called_once_with(_OID)


def test_get_view_skips_a_link_whose_connection_vanished(entra_settings):
    s = _svc_with(list_for_principal={"return_value": [_link(connection_id="conn-gone")]})
    client, _ = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/me/github-link", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["links"] == []


def test_get_view_only_lists_github_connections(entra_settings):
    s = _svc_with(list_for_principal={"return_value": []})
    client, _ = _build_client(
        s,
        connections=[
            _connection(),
            _connection(id="conn-2", provider=Provider.GITLAB, org="other"),
        ],
    )
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/me/github-link", headers=_headers())
    assert resp.status_code == 200
    assert [c["connection_id"] for c in resp.json()["connections"]] == ["conn-1"]


def test_get_view_reports_a_refreshing_link_as_still_linked(entra_settings):
    """The console renders ``linked === false`` as "reconnect"; an in-flight refresh must not tell
    the human to re-authorize."""
    s = _svc_with(list_for_principal={"return_value": [_link(status=LinkStatus.REFRESHING)]})
    client, _ = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/me/github-link", headers=_headers())
    row = resp.json()["links"][0]
    assert row["status"] == "refreshing"
    assert row["linked"] is True


def test_start_returns_the_authorize_url_and_state(entra_settings):
    s = _svc_with(begin_link={"return_value": ("https://github.com/login/oauth/authorize?x=1", "st-1")})
    client, _ = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/me/github-link/start",
            json={"connection_id": "conn-1", "redirect_uri": _CALLBACK},
            headers=_headers(),
        )
    assert resp.status_code == 200
    assert resp.json() == {
        "authorize_url": "https://github.com/login/oauth/authorize?x=1",
        "state": "st-1",
    }


def test_start_passes_the_principals_oid_and_ignores_any_body_oid(entra_settings):
    s = _svc_with(begin_link={"return_value": ("https://github.com/x", "st-1")})
    client, _ = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/me/github-link/start",
            json={
                "connection_id": "conn-1",
                "redirect_uri": _CALLBACK,
                "principal_oid": "somebody-elses-oid",
            },
            headers=_headers(),
        )
    assert resp.status_code == 200
    args, kwargs = s.begin_link.call_args
    assert "somebody-elses-oid" not in list(args) + list(kwargs.values())
    assert args[0] == _OID


def test_start_rejects_a_principal_with_no_oid(entra_settings):
    s = _svc_with(begin_link={"return_value": ("https://github.com/x", "st-1")})
    client, _ = _build_client(s)
    claims = _claims_for("viewer")
    claims.pop("oid")
    with patch("core.security_entra.verify_entra_token", return_value=claims):
        resp = client.post(
            "/api/v1/me/github-link/start",
            json={"connection_id": "conn-1", "redirect_uri": _CALLBACK},
            headers=_headers(),
        )
    assert resp.status_code == 400
    assert resp.json() == {"detail": "Invalid request", "code": "github_link.bad_request"}
    s.begin_link.assert_not_called()


def test_callback_returns_the_link_status(entra_settings):
    s = _svc_with(complete_link={"return_value": _link()})
    client, _ = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/me/github-link/callback",
            json={"code": "gho_code", "state": "st-1"},
            headers=_headers(),
        )
    assert resp.status_code == 200
    assert resp.json() == {
        "connection_id": "conn-1",
        "org": "acme",
        "linked": True,
        "status": "linked",
        "github_login": "octocat",
        "last_verified_at": "2026-07-28T11:00:00+00:00",
    }
    s.complete_link.assert_called_once_with(_OID, "gho_code", "st-1")


@pytest.mark.parametrize(
    "kind", ["not_found", "secret_error"], ids=["connection_deleted_mid_flow", "store_fault"]
)
def test_callback_reports_a_COMMITTED_link_as_SUCCESS_when_the_org_lookup_fails(entra_settings, kind):
    """``complete_link`` has already written the secret and the row by the time the route resolves
    ``org``, so the post-commit lookup degrades to an empty ``org`` instead of a 404 the callback
    page renders as terminal."""
    s = _svc_with(complete_link={"return_value": _link()})
    client, _ = _build_client(s, get_connection=MagicMock(side_effect=ConnectionError("gone", kind=kind)))
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/me/github-link/callback",
            json={"code": "gho_code", "state": "st-1"},
            headers=_headers(),
        )
    assert resp.status_code == 200, "a committed link was reported as a failure"
    body = resp.json()
    assert body["linked"] is True
    assert body["status"] == "linked"
    assert body["github_login"] == "octocat"
    assert body["connection_id"] == "conn-1"
    assert body["org"] == ""


def test_callback_never_raises_on_a_boto_fault_in_the_composed_connection_service(entra_settings):
    """Driven through the real ``ConnectionService`` over a faulting table, so a narrowed guard next
    door fails this test too."""
    import api.routes.connections as connections_module

    s = _svc_with(complete_link={"return_value": _link()})
    client, _ = _build_client(s)
    connections_module._svc = _real_conn_svc_over(
        EndpointConnectionError(endpoint_url="https://dynamodb.example.invalid/")
    )
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/me/github-link/callback",
            json={"code": "gho_code", "state": "st-1"},
            headers=_headers(),
        )
    assert resp.status_code == 200, "a committed link was reported as a failure"
    assert resp.json()["linked"] is True
    for forbidden in ("dynamodb.example.invalid", "Traceback"):
        assert forbidden not in resp.text, forbidden


def test_verify_returns_the_refreshed_login(entra_settings):
    s = _svc_with(verify_link={"return_value": _link(github_login="octocat-renamed")})
    client, _ = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post("/api/v1/me/github-link/conn-1/verify", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["github_login"] == "octocat-renamed"
    assert resp.json()["org"] == "acme"
    s.verify_link.assert_called_once_with(_OID, "conn-1")


def test_verify_resolves_the_org_BEFORE_it_mutates(entra_settings):
    """``verify_link`` writes, so the ``org`` lookup runs first and a vanished connection answers
    404 "GitHub link not found" (the link service's ``connection_missing``) with nothing mutated."""
    s = _svc_with(verify_link={"return_value": _link()})
    client, _ = _build_client(
        s,
        get_connection=MagicMock(side_effect=ConnectionError("unknown connection", kind="not_found")),
    )
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post("/api/v1/me/github-link/conn-1/verify", headers=_headers())
    assert resp.status_code == 404
    assert resp.json() == {"detail": "GitHub link not found", "code": "github_link.connection_missing"}
    assert "unknown connection" not in resp.text
    s.verify_link.assert_not_called()


def test_a_connection_store_fault_on_verify_renders_the_link_table(entra_settings):
    s = _svc_with(verify_link={"return_value": _link()})
    client, _ = _build_client(
        s,
        get_connection=MagicMock(side_effect=ConnectionError("store down", kind="secret_error")),
    )
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post("/api/v1/me/github-link/conn-1/verify", headers=_headers())
    assert resp.status_code == 502
    assert resp.json() == {"detail": "Secret store operation failed", "code": "github_link.secret_error"}
    s.verify_link.assert_not_called()


def test_any_other_connection_failure_on_verify_is_the_link_tables_provider_error(entra_settings):
    s = _svc_with(verify_link={"return_value": _link()})
    client, _ = _build_client(
        s,
        get_connection=MagicMock(side_effect=ConnectionError("Invalid request", kind="bad_request")),
    )
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post("/api/v1/me/github-link/conn-1/verify", headers=_headers())
    assert resp.status_code == 502
    assert resp.json() == {"detail": "GitHub request failed", "code": "github_link.provider_error"}
    s.verify_link.assert_not_called()


def test_a_connection_store_fault_on_the_view_renders_the_connection_table(entra_settings):
    s = _svc_with(list_for_principal={"return_value": [_link()]})
    client, fake_conn_svc = _build_client(s)
    fake_conn_svc.list_connections.side_effect = ConnectionError("store down", kind="secret_error")
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/me/github-link", headers=_headers())
    assert resp.status_code == 502
    assert resp.json() == {"detail": "Secret store operation failed", "code": "connection.secret_error"}
    s.list_for_principal.assert_not_called()


def test_unlink_returns_204_with_no_body(entra_settings):
    s = _svc_with(unlink={"return_value": None})
    client, _ = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.delete("/api/v1/me/github-link/conn-1", headers=_headers())
    assert resp.status_code == 204
    assert resp.content == b""
    s.unlink.assert_called_once_with(_OID, "conn-1")


@pytest.mark.parametrize("kind,status,detail", _LINK_ROWS, ids=[row[0] for row in _LINK_ROWS])
def test_every_error_kind_renders_its_fixed_detail(entra_settings, kind, status, detail):
    marker = "ghu_leaked_token_and_a_ClientError_message"
    s = _svc_with(verify_link={"side_effect": GitHubLinkError(marker, kind=kind)})
    client, _ = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post("/api/v1/me/github-link/conn-1/verify", headers=_headers())
    assert resp.status_code == status, f"kind={kind}"
    assert resp.json() == {"detail": detail, "code": f"github_link.{kind}"}
    assert marker not in resp.text, f"kind={kind} leaked the error message"


@pytest.mark.parametrize(
    "route,method,path",
    [
        ("list_for_principal", "get", "/api/v1/me/github-link"),
        ("begin_link", "post", "/api/v1/me/github-link/start"),
        ("complete_link", "post", "/api/v1/me/github-link/callback"),
        ("unlink", "delete", "/api/v1/me/github-link/conn-1"),
    ],
)
def test_every_route_renders_a_link_error_through_the_table(entra_settings, route, method, path):
    s = _svc_with(**{route: {"side_effect": GitHubLinkError("row gone", kind="not_found")}})
    client, _ = _build_client(s)
    bodies = {
        "post": {"connection_id": "conn-1", "redirect_uri": _CALLBACK, "code": "c", "state": "s"},
    }
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = getattr(client, method)(
            path, headers=_headers(), **({"json": bodies["post"]} if method == "post" else {})
        )
    assert resp.status_code == 404
    assert resp.json() == {"detail": "GitHub link not found", "code": "github_link.not_found"}
    assert "row gone" not in resp.text


def test_refresh_in_progress_and_secret_error_stay_retryable():
    """Both occur under ordinary two-task contention or a transient AWS blip; collapsing either
    into a terminal kind would tell the human to re-link over a race."""
    assert GitHubLinkError.kinds["refresh_in_progress"].status == 409
    assert GitHubLinkError.kinds["secret_error"].status == 502
    assert "retry" in str(GitHubLinkError.kinds["refresh_in_progress"].detail).lower()


def test_no_route_ever_returns_401():
    """The SPA's 401 interceptor drops the auth token and reloads, logging the human out; every
    table a link route renders through stays inside the pinned set."""
    statuses = {o.status for o in GitHubLinkError.kinds.values()} | {
        o.status for o in ConnectionError.kinds.values()
    }
    assert statuses <= {400, 404, 409, 502}


def test_a_response_body_never_contains_a_token_or_a_secret(entra_settings):
    s = _svc_with(complete_link={"return_value": _link()})
    client, _ = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/me/github-link/callback",
            json={"code": "gho_the_authorization_code", "state": "st-1"},
            headers=_headers(),
        )
    assert resp.status_code == 200
    text = resp.text
    for forbidden in ("gho_", "ghu_", "ghr_", "access_token", "refresh_token", "secret_arn"):
        assert forbidden not in text, forbidden


def test_unauthenticated_request_is_rejected(entra_settings):
    s = _svc_with(list_for_principal={"return_value": []})
    client, _ = _build_client(s)
    resp = client.get("/api/v1/me/github-link")
    assert resp.status_code in (401, 403)
    s.list_for_principal.assert_not_called()


def test_every_route_is_viewer_reachable(entra_settings):
    s = _svc_with(
        list_for_principal={"return_value": []},
        begin_link={"return_value": ("https://github.com/x", "st")},
        complete_link={"return_value": _link()},
        verify_link={"return_value": _link()},
        unlink={"return_value": None},
    )
    client, _ = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        assert client.get("/api/v1/me/github-link", headers=_headers()).status_code == 200
        start = client.post(
            "/api/v1/me/github-link/start",
            json={"connection_id": "conn-1", "redirect_uri": _CALLBACK},
            headers=_headers(),
        )
        assert start.status_code == 200
        callback = client.post(
            "/api/v1/me/github-link/callback", json={"code": "c", "state": "s"}, headers=_headers()
        )
        assert callback.status_code == 200
        assert client.post("/api/v1/me/github-link/conn-1/verify", headers=_headers()).status_code == 200
        assert client.delete("/api/v1/me/github-link/conn-1", headers=_headers()).status_code == 204


def test_settings_carries_the_pinned_link_secret_prefix(entra_settings):
    from core.config import Settings

    assert Settings().GITHUB_USER_LINK_SECRET_PREFIX == "agp-dev/github-user-link/"


def test_service_is_built_with_the_settings_secret_prefix(entra_settings):
    import api.routes.connections as connections_module
    import api.routes.github_link as m
    from core.config import settings

    connections_module._svc = MagicMock()
    m._svc = None
    svc = m.get_github_link_service()
    assert svc.secret_prefix == settings.GITHUB_USER_LINK_SECRET_PREFIX
    assert svc.secret_prefix
    assert svc._secret_name("link-1") == f"{settings.GITHUB_USER_LINK_SECRET_PREFIX}link-1"
    m._svc = None


def test_an_empty_secret_prefix_is_loud_not_silent(entra_settings, monkeypatch):
    import api.routes.connections as connections_module
    import api.routes.github_link as m
    from core.config import settings

    connections_module._svc = MagicMock()
    monkeypatch.setattr(settings, "GITHUB_USER_LINK_SECRET_PREFIX", "")
    m._svc = None
    with pytest.raises(GitHubLinkError) as exc:
        m.get_github_link_service()
    assert exc.value.kind == "secret_error"
    m._svc = None


def test_an_empty_secret_prefix_surfaces_as_502_not_a_500(entra_settings, monkeypatch):
    import api.routes.github_link as m
    from core.config import settings

    client, _ = _build_client(_svc_with(list_for_principal={"return_value": []}))
    monkeypatch.setattr(settings, "GITHUB_USER_LINK_SECRET_PREFIX", "")
    m._svc = None
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/me/github-link", headers=_headers())
    assert resp.status_code == 502
    assert resp.json() == {"detail": "Secret store operation failed", "code": "github_link.secret_error"}
    m._svc = None


def test_service_is_built_with_non_empty_allowed_origins(entra_settings):
    import api.routes.connections as connections_module
    import api.routes.github_link as m
    from core.config import settings

    connections_module._svc = MagicMock()
    m._svc = None
    svc = m.get_github_link_service()
    assert svc._allowed_origins, "allowed_origins must be wired from real configuration"
    for origin in settings.CORS_ORIGINS:
        assert origin.lower().rstrip("/") in svc._allowed_origins
    m._svc = None


def test_the_wired_origins_actually_reject_a_foreign_redirect_uri(entra_settings):
    import api.routes.connections as connections_module
    import api.routes.github_link as m

    connections_module._svc = MagicMock()
    m._svc = None
    svc = m.get_github_link_service()
    with pytest.raises(GitHubLinkError) as exc:
        svc._validate_redirect_uri("https://evil.example.com/ops/github-link/callback")
    assert exc.value.kind == "bad_request"
    svc._validate_redirect_uri("http://localhost:5173/ops/github-link/callback")
    m._svc = None


def test_router_is_exported_from_the_routes_package(entra_settings):
    import api.routes as routes

    assert "github_link_router" in routes.__all__
    assert routes.github_link_router is not None


def _real_conn_svc_over(fault):
    """A real ``ConnectionService`` over a table whose every operation raises ``fault``."""
    from core.state_store import StateStore
    from services.connection_service import ConnectionService

    table = MagicMock()
    for op in ("get_item", "put_item", "delete_item", "query"):
        getattr(table, op).side_effect = fault
    svc = ConnectionService(
        table_name="",
        secret_prefix="agp-test/git-connections/",
        region="us-east-1",
        secrets_client=MagicMock(),
    )
    svc._store = StateStore("connections", table=table)
    assert svc._store.in_memory is False
    return svc


@pytest.mark.parametrize(
    "fault",
    [
        ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}},
            "Query",
        ),
        EndpointConnectionError(endpoint_url="https://dynamodb.example.invalid/"),
    ],
    ids=["client_error", "boto_core_error"],
)
def test_a_connection_store_fault_never_answers_500(entra_settings, fault):
    """Driven through the real ``ConnectionService``, so a narrowed guard next door fails a route
    test and not only a service test; both boto3 shapes."""
    import api.routes.connections as connections_module

    s = _svc_with(
        list_for_principal={"return_value": []},
        verify_link={"return_value": _link()},
    )
    client, _ = _build_client(s)
    connections_module._svc = _real_conn_svc_over(fault)

    allowed = {o.status for o in GitHubLinkError.kinds.values()} | {
        o.status for o in ConnectionError.kinds.values()
    }
    allowed |= {200, 204}
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        view = client.get("/api/v1/me/github-link", headers=_headers())
        verify = client.post("/api/v1/me/github-link/conn-1/verify", headers=_headers())

    for label, resp in (("GET view", view), ("POST verify", verify)):
        assert resp.status_code in allowed, f"{label} -> {resp.status_code}"
        assert resp.status_code != 500, f"{label} escaped as a 500"
        for forbidden in ("ProvisionedThroughput", "dynamodb.example.invalid", "Traceback"):
            assert forbidden not in resp.text, f"{label}: {forbidden} leaked"
