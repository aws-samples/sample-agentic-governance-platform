"""Per-user GitHub link route tests (E27B/T8) — the wiring hub.

Same idiom as ``test_connections_routes.py``: the REAL ``require_role``/``current_principal``
path against a mocked ``verify_entra_token`` (no live Entra), a FAKE link service patched onto
the router's module-level ``_svc``, and a fake ``ConnectionService`` on
``api.routes.connections._svc`` (the ``GET``/``callback``/``verify`` routes compose it for the
``org`` field). DynamoDB and Secrets Manager are never faked because they are never reached.

Two of these tests exist to pin the wiring GATES both reviewers flagged as blocking:

* ``test_service_is_built_with_the_settings_secret_prefix`` — without
  ``secret_prefix=settings.GITHUB_USER_LINK_SECRET_PREFIX`` every per-user secret lands at a
  bare UUID name, OUTSIDE every ``agp-dev/*`` IAM condition and lifecycle rule, with dev and
  prod sharing one namespace. Nothing fails loudly at runtime (the ECS grant is
  ``Resource = "*"``), so a test is the only thing that can catch it.
* ``test_service_is_built_with_non_empty_allowed_origins`` — ``allowed_origins`` fails OPEN
  when empty (deliberate, and warned about at construction), which means a wiring miss silently
  removes the server-side origin check on the OAuth ``redirect_uri``.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from models.connection import AuthType, Connection, ConnStatus, Provider
from models.github_link import GitHubUserLink, LinkStatus
from services.connection_service import ConnectionError
from services.github_user_link import GitHubLinkError

_OID = "viewer-oid"


@pytest.fixture(autouse=True)
def reset_modules():
    for mod in [
        "core.rbac",
        "core.security_entra",
        "core.config",
        "api.routes.connections",
        "api.routes.github_link",
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
    data = {
        "id": "conn-1",
        "provider": Provider.GITHUB,
        "org": "acme",
        "base_url": None,
        "auth_type": AuthType.GITHUB_APP,
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
    data = {
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


def _build_client(link_svc, connections=None, get_connection=None):
    import api.routes.connections as connections_module
    import api.routes.github_link as github_link_module

    github_link_module._svc = link_svc

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
    app.include_router(github_link_module.router, prefix="/api/v1")
    return TestClient(app), fake_conn_svc


def _svc_with(**methods):
    s = MagicMock()
    for name, val in methods.items():
        setattr(s, name, MagicMock(**val))
    return s


# --- GET "" : the joined view ------------------------------------------------

def test_get_view_joins_links_and_connections(entra_settings):
    s = _svc_with(list_for_principal={"return_value": [_link()]})
    client, _ = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/me/github-link", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["connections"] == [
        {"connection_id": "conn-1", "org": "acme", "oauth_client_ready": True}
    ]
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
    # the oid comes from the validated principal, never a param
    s.list_for_principal.assert_called_once_with(_OID)


def test_get_view_skips_a_link_whose_connection_vanished(entra_settings):
    s = _svc_with(list_for_principal={"return_value": [_link(connection_id="conn-gone")]})
    client, _ = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/me/github-link", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["links"] == []  # skipped, not an error


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
    # T5's deriveLinkCardState renders `linked === false` as 'revoked' → "Reconnect". A
    # transient in-flight refresh must NOT tell the human to re-authorize.
    s = _svc_with(list_for_principal={"return_value": [_link(status=LinkStatus.REFRESHING)]})
    client, _ = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/me/github-link", headers=_headers())
    row = resp.json()["links"][0]
    assert row["status"] == "refreshing"
    assert row["linked"] is True


# --- POST /start ------------------------------------------------------------

def test_start_returns_the_authorize_url_and_state(entra_settings):
    s = _svc_with(
        begin_link={"return_value": ("https://github.com/login/oauth/authorize?x=1", "st-1")}
    )
    client, _ = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/me/github-link/start",
            json={
                "connection_id": "conn-1",
                "redirect_uri": "https://console.example.com/ops/github-link/callback",
            },
            headers=_headers(),
        )
    assert resp.status_code == 200
    assert resp.json() == {
        "authorize_url": "https://github.com/login/oauth/authorize?x=1",
        "state": "st-1",
    }


def test_start_passes_the_principals_oid_and_ignores_any_body_oid(entra_settings):
    """The forgery guard at the route boundary."""
    s = _svc_with(begin_link={"return_value": ("https://github.com/x", "st-1")})
    client, _ = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/me/github-link/start",
            json={
                "connection_id": "conn-1",
                "redirect_uri": "https://console.example.com/ops/github-link/callback",
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
            json={
                "connection_id": "conn-1",
                "redirect_uri": "https://console.example.com/ops/github-link/callback",
            },
            headers=_headers(),
        )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid request"
    s.begin_link.assert_not_called()


# --- POST /callback ---------------------------------------------------------

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
        "org": "acme",  # the ROUTE owns org; GitHubUserLink carries none
        "linked": True,
        "status": "linked",
        "github_login": "octocat",
        "last_verified_at": "2026-07-28T11:00:00+00:00",
    }
    s.complete_link.assert_called_once_with(_OID, "gho_code", "st-1")


@pytest.mark.parametrize(
    "kind",
    ["not_found", "secret_error", "provider_error"],
    ids=["connection_deleted_mid_flow", "ddb_blip", "other_store_fault"],
)
def test_callback_reports_a_COMMITTED_link_as_SUCCESS_when_the_org_lookup_fails(
    entra_settings, kind
):
    """A SUCCESSFUL link must never be reported as a failure.

    ``complete_link`` has already created the Secrets Manager secret and written the row by
    the time the route resolves ``org`` for the response — the ``connection_id`` only exists
    once the stored state is consumed, so that lookup cannot be hoisted. It used to RAISE:
    an admin deleting the connection in the ~200 ms window, or a DDB blip (which
    ``ConnectionService._get`` collapses to ``not_found``), answered 404 "GitHub link not
    found", which the callback page renders as a TERMINAL rose error whose copy says
    "Nothing was changed" — false, the link is live at GitHub. The human then starts over and
    burns a second authorization.

    So the post-commit lookup DEGRADES: 200 with an empty ``org``. Nothing on the callback
    page consumes ``org`` (its success copy is keyed off ``github_login``), and the next visit
    to the link page re-joins ``list_connections()`` and shows the real org."""
    s = _svc_with(complete_link={"return_value": _link()})
    client, _ = _build_client(
        s,
        get_connection=MagicMock(side_effect=ConnectionError("gone", kind=kind)),
    )
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
    assert body["connection_id"] == "conn-1"  # the row's own id still answers
    assert body["org"] == ""  # the one field that could not be resolved


def test_callback_never_raises_on_a_boto_fault_in_the_composed_connection_service(
    entra_settings,
):
    """Driven through the REAL ``ConnectionService`` over a faulting table, so a narrowed
    guard next door fails THIS test too: a store fault after the write is still a 200."""
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


# --- POST /{id}/verify ------------------------------------------------------

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
    """RESOLVE BEFORE MUTATING. ``verify_link`` writes — it may rotate the token and it
    persists either the refreshed login or an UNLINKED row. The ``org`` lookup used to run
    after it, so a vanished connection or a DDB blip turned a completed write into a terminal
    404. The ``connection_id`` is a path param here, so the lookup is hoisted ahead of the
    mutation: the error is then honest, because nothing was written."""
    s = _svc_with(verify_link={"return_value": _link()})
    client, _ = _build_client(
        s,
        get_connection=MagicMock(
            side_effect=ConnectionError("unknown connection", kind="not_found")
        ),
    )
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post("/api/v1/me/github-link/conn-1/verify", headers=_headers())
    assert resp.status_code == 404
    # THE POINT: nothing was mutated, so "not found" is true rather than a lie about a write.
    s.verify_link.assert_not_called()


# --- DELETE /{id} ----------------------------------------------------------

def test_unlink_returns_204_with_no_body(entra_settings):
    s = _svc_with(unlink={"return_value": None})
    client, _ = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.delete("/api/v1/me/github-link/conn-1", headers=_headers())
    assert resp.status_code == 204
    assert resp.content == b""
    s.unlink.assert_called_once_with(_OID, "conn-1")


# --- error mapping ---------------------------------------------------------

@pytest.mark.parametrize(
    "kind,status",
    [
        ("not_found", 404),
        ("bad_request", 400),
        ("conflict", 409),
        ("oauth_client_missing", 409),
        ("refresh_in_progress", 409),
        ("link_revoked", 409),
        ("provider_error", 502),
        ("secret_error", 502),
    ],
)
def test_every_error_kind_maps_to_a_fixed_detail(entra_settings, kind, status):
    marker = "ghu_leaked_token_and_a_ClientError_message"
    s = _svc_with(verify_link={"side_effect": GitHubLinkError(marker, kind=kind)})
    client, _ = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post("/api/v1/me/github-link/conn-1/verify", headers=_headers())
    assert resp.status_code == status, f"kind={kind}"
    assert marker not in resp.text, f"kind={kind} leaked the error message"

    import api.routes.github_link as m

    assert resp.json()["detail"] == m._ERROR_DETAIL[kind]


def test_refresh_in_progress_and_secret_error_stay_retryable(entra_settings):
    """RETRYABLE, not terminal: both occur under ordinary 2-task contention or a transient
    AWS blip, and the client is expected to retry after a delay. Collapsing either into a
    terminal kind would tell the human to re-link over a 200ms race."""
    import api.routes.github_link as m

    assert m._ERROR_STATUS["refresh_in_progress"] == 409
    assert m._ERROR_STATUS["secret_error"] == 502
    assert "retry" in m._ERROR_DETAIL["refresh_in_progress"].lower()


def test_no_route_ever_returns_401(entra_settings):
    # B11: the SPA's 401 interceptor drops the auth token and reloads, logging the human out.
    import api.routes.github_link as m

    assert set(m._ERROR_STATUS.values()) <= {400, 404, 409, 502}


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
    """A per-user action on the human's OWN link — any authenticated human may take it."""
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
        assert (
            client.post(
                "/api/v1/me/github-link/start",
                json={
                    "connection_id": "conn-1",
                    "redirect_uri": "https://console.example.com/ops/github-link/callback",
                },
                headers=_headers(),
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/v1/me/github-link/callback",
                json={"code": "c", "state": "s"},
                headers=_headers(),
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/v1/me/github-link/conn-1/verify", headers=_headers()
            ).status_code
            == 200
        )
        assert (
            client.delete("/api/v1/me/github-link/conn-1", headers=_headers()).status_code
            == 204
        )


# --- GATE 1: the settings secret prefix is actually applied ------------------

def test_settings_carries_the_pinned_link_secret_prefix(entra_settings):
    from core.config import Settings

    assert Settings().GITHUB_USER_LINK_SECRET_PREFIX == "agp-dev/github-user-link/"


def test_service_is_built_with_the_settings_secret_prefix(entra_settings):
    """GATE 1. A missed ``secret_prefix=`` puts every per-user secret at a bare UUID name —
    outside every ``agp-dev/*`` IAM condition and lifecycle rule, with dev and prod sharing
    one namespace — and NOTHING fails loudly, because the ECS grant is ``Resource = "*"``."""
    import api.routes.connections as connections_module
    import api.routes.github_link as m
    from core.config import settings

    connections_module._svc = MagicMock()
    m._svc = None
    svc = m.get_github_link_service()
    assert svc.secret_prefix == settings.GITHUB_USER_LINK_SECRET_PREFIX
    assert svc.secret_prefix  # never a bare UUID namespace
    assert svc._secret_name("link-1") == f"{settings.GITHUB_USER_LINK_SECRET_PREFIX}link-1"
    m._svc = None


def test_an_empty_secret_prefix_is_loud_not_silent(entra_settings, monkeypatch):
    """An operator who blanks the env var must get a loud, retryable failure — not secrets
    quietly written to a bare-UUID namespace."""
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
    assert resp.json()["detail"] == m._ERROR_DETAIL["secret_error"]
    m._svc = None


# --- GATE 2: allowed_origins is wired NON-EMPTY -----------------------------

def test_service_is_built_with_non_empty_allowed_origins(entra_settings):
    """GATE 2. ``allowed_origins`` fails OPEN when empty, so the server-side origin check on
    the OAuth ``redirect_uri`` would silently not be enforced."""
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
    """The gate is only meaningful if the wired list is the one the check consults."""
    import api.routes.connections as connections_module
    import api.routes.github_link as m

    connections_module._svc = MagicMock()
    m._svc = None
    svc = m.get_github_link_service()
    with pytest.raises(GitHubLinkError) as exc:
        svc._validate_redirect_uri("https://evil.example.com/ops/github-link/callback")
    assert exc.value.kind == "bad_request"
    # and the real console origin passes
    svc._validate_redirect_uri("http://localhost:5173/ops/github-link/callback")
    m._svc = None


# --- app wiring guard: BOTH include_router blocks ---------------------------

def test_routes_registered_at_both_prefixes(entra_settings, monkeypatch):
    """``main.py`` registers every router twice — at ``API_PREFIX`` and again at
    ``ROOT_PATH + API_PREFIX`` for the API Gateway stage. Missing the second block makes the
    routes 404 in cloud while passing every local test."""
    monkeypatch.setenv("ROOT_PATH", "/dev")
    doomed = [k for k in sys.modules if k == "main" or k.startswith(("api.", "core.config"))]
    for k in doomed:
        sys.modules.pop(k, None)
    try:
        import main

        from conftest import app_route_paths

        paths = app_route_paths(main.app)
        for base in ("/api/v1", "/dev/api/v1"):
            assert f"{base}/me/github-link" in paths
            assert f"{base}/me/github-link/start" in paths
            assert f"{base}/me/github-link/callback" in paths
            assert f"{base}/me/github-link/{{connection_id}}/verify" in paths
            assert f"{base}/me/github-link/{{connection_id}}" in paths
    finally:
        for k in [k for k in sys.modules if k == "main" or k.startswith(("api.", "core.config"))]:
            sys.modules.pop(k, None)


def test_router_is_exported_from_the_routes_package(entra_settings):
    import api.routes as routes

    assert "github_link_router" in routes.__all__
    assert routes.github_link_router is not None


# --- store-fault contract: a boto3 fault in the COMPOSED ConnectionService ---
#
# The two routes below compose the REAL `ConnectionService` for the `org` field. It guarded
# only `ClientError`, so a `BotoCoreError` (VPC-endpoint blip, DNS stall — ordinary from ECS)
# propagated raw and both routes answered HTTP 500 — outside the pinned {400,404,409,502}
# `test_no_route_ever_returns_401` asserts. Driven here through the real service so a
# narrowed guard next door fails a ROUTE test, not only a service test.

class _ConnFaultTable:
    """Every DDB op raises the given fault. Only ever read by `ConnectionService`."""

    def __init__(self, fault):
        self._fault = fault

    def get_item(self, **kwargs):
        raise self._fault

    def put_item(self, **kwargs):
        raise self._fault

    def delete_item(self, **kwargs):
        raise self._fault

    def query(self, **kwargs):
        raise self._fault


def _real_conn_svc_over(fault):
    from services.connection_service import ConnectionService

    svc = ConnectionService(
        table_name="connections",
        secret_prefix="agp-test/git-connections/",
        region="us-east-1",
        secrets_client=MagicMock(),
    )
    svc._table = _ConnFaultTable(fault)
    assert svc._has_ddb is True
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
    import api.routes.connections as connections_module
    import api.routes.github_link as m

    s = _svc_with(
        list_for_principal={"return_value": []},
        verify_link={"return_value": _link()},
    )
    client, _ = _build_client(s)
    connections_module._svc = _real_conn_svc_over(fault)

    allowed = set(m._ERROR_STATUS.values()) | {200, 204}
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        view = client.get("/api/v1/me/github-link", headers=_headers())
        verify = client.post("/api/v1/me/github-link/conn-1/verify", headers=_headers())

    for label, resp in (("GET view", view), ("POST verify", verify)):
        assert resp.status_code in allowed, f"{label} -> {resp.status_code}"
        assert resp.status_code != 500, f"{label} escaped as a 500"
        # and nothing boto3-shaped reached the body
        for forbidden in ("ProvisionedThroughput", "dynamodb.example.invalid", "Traceback"):
            assert forbidden not in resp.text, f"{label}: {forbidden} leaked"
