"""GitHub-backed template catalog route tests (E22/T2).

Exercises the REAL require_role/current_principal path against a mocked
verify_entra_token (no live Entra) and a FAKE GitHubTemplateService patched onto
the router's module-level ``_svc`` (no live GitHub / AWS). Mirrors
test_ops_templates_routes.py for the app/client + role auth-override idiom, and
the ``.kind`` → HTTP-status / FIXED-detail mapping.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.github_template_service import GitHubTemplateError, TemplateView


@pytest.fixture(autouse=True)
def reset_modules():
    import sys

    for mod in [
        "core.rbac",
        "core.security_entra",
        "core.config",
        "api.routes.github_templates",
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


def _build_client(fake_svc):
    import api.routes.github_templates as gh_module

    gh_module._svc = fake_svc  # the lazy GitHubTemplateService singleton

    app = FastAPI()
    app.include_router(gh_module.router, prefix="/api/v1")
    return TestClient(app)


def _claims_for(role: str):
    role_app = {
        "viewer": "Platform.Viewer",
        "operator": "Platform.Operator",
        "admin": "Platform.Admin",
    }[role]
    return {"oid": f"{role}-oid", "preferred_username": f"{role}@x.com", "roles": [role_app]}


def _headers():
    return {"Authorization": "Bearer fake-token"}


def _sample_view(name="strands-agentcore"):
    return TemplateView(
        name=name,
        description="A scaffold",
        framework="strands",
        aws_services=["lambda"],
        tags=["fsi"],
        html_url=f"https://github.com/acme/{name}",
        updated_at="2026-07-08T00:00:00+00:00",
    )


# --- RBAC: upload + delete require ADMIN (403 for a viewer) ------------------

def test_upload_viewer_forbidden(entra_settings):
    s = MagicMock()
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/github-templates",
            files={"file": ("t.zip", b"zipbytes", "application/zip")},
            data={"connection_id": "conn1", "name": "t", "framework": "strands"},
            headers=_headers(),
        )
    assert resp.status_code == 403
    s.upload_template.assert_not_called()


def test_delete_viewer_forbidden(entra_settings):
    s = MagicMock()
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.delete(
            "/api/v1/github-templates/strands-agentcore?connection_id=conn1", headers=_headers()
        )
    assert resp.status_code == 403
    s.delete_template.assert_not_called()


# --- GET: operator can read the catalog -------------------------------------

def test_list_templates_operator_ok(entra_settings):
    s = MagicMock()
    s.list_templates.return_value = [_sample_view()]
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/github-templates?connection_id=conn1", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["name"] == "strands-agentcore"
    assert body[0]["framework"] == "strands"
    s.list_templates.assert_called_once_with("conn1")


# --- POST happy path: admin upload → 201 ------------------------------------

def test_upload_admin_201(entra_settings):
    s = MagicMock()
    s.upload_template.return_value = _sample_view(name="my-tpl")
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/github-templates",
            files={"file": ("t.zip", b"zipbytes", "application/zip")},
            data={
                "connection_id": "conn1",
                "name": "my-tpl",
                "description": "d",
                "framework": "strands",
                "aws_services": ["lambda"],
                "tags": ["fsi"],
            },
            headers=_headers(),
        )
    assert resp.status_code == 201
    assert resp.json()["name"] == "my-tpl"
    _, kwargs = s.upload_template.call_args
    assert s.upload_template.call_args[0][0] == "conn1"
    assert kwargs["zip_bytes"] == b"zipbytes"
    assert kwargs["name"] == "my-tpl"
    assert kwargs["framework"] == "strands"
    assert kwargs["aws_services"] == ["lambda"]
    assert kwargs["tags"] == ["fsi"]


# --- POST invalid zip → 422 with the FIXED detail (never str(err)) ----------

def test_upload_invalid_zip_422(entra_settings):
    s = MagicMock()
    s.upload_template.side_effect = GitHubTemplateError("secret internals", kind="invalid_zip")
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/github-templates",
            files={"file": ("t.zip", b"notazip", "application/zip")},
            data={"connection_id": "conn1", "name": "t", "framework": "strands"},
            headers=_headers(),
        )
    assert resp.status_code == 422
    assert "secret internals" not in resp.text


# --- POST invalid input → 422 -----------------------------------------------

def test_upload_invalid_input_422(entra_settings):
    s = MagicMock()
    s.upload_template.side_effect = GitHubTemplateError("bad fw", kind="invalid_input")
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/github-templates",
            files={"file": ("t.zip", b"zipbytes", "application/zip")},
            data={"connection_id": "conn1", "name": "t", "framework": "langgraph"},
            headers=_headers(),
        )
    assert resp.status_code == 422
    assert "bad fw" not in resp.text


# --- github_error → 502 -----------------------------------------------------

def test_list_github_error_502(entra_settings):
    s = MagicMock()
    s.list_templates.side_effect = GitHubTemplateError("raw gh body", kind="github_error")
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/github-templates?connection_id=conn1", headers=_headers())
    assert resp.status_code == 502
    assert "raw gh body" not in resp.text


# --- DELETE unknown → 404 with the FIXED detail -----------------------------

def test_delete_unknown_404(entra_settings):
    s = MagicMock()
    s.delete_template.side_effect = GitHubTemplateError("Unknown template", kind="not_found")
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.delete(
            "/api/v1/github-templates/nope?connection_id=conn1", headers=_headers()
        )
    assert resp.status_code == 404


# --- DELETE happy path → 204 ------------------------------------------------

def test_delete_admin_204(entra_settings):
    s = MagicMock()
    s.delete_template.return_value = None
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.delete(
            "/api/v1/github-templates/my-tpl?connection_id=conn1", headers=_headers()
        )
    assert resp.status_code == 204
    s.delete_template.assert_called_once_with("conn1", "my-tpl")


# --- the 422/503 split reaches the client -----------------------------------
# Both kinds used to come back as 503 "temporarily unavailable", which told the console to
# retry a malformed request forever. These pin the two halves at the HTTP boundary.

def test_invalid_input_is_422_not_503(entra_settings):
    """A malformed name/connection id is PERMANENT — 422, never a retry prompt."""
    s = MagicMock()
    s.delete_template.side_effect = GitHubTemplateError(
        "template id must not contain '#'", kind="invalid_input"
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.delete(
            "/api/v1/github-templates/a%23b?connection_id=conn1", headers=_headers()
        )
    assert resp.status_code == 422
    assert resp.status_code != 503


def test_store_error_is_503_retryable(entra_settings):
    """A genuine store fault IS transient — 503, so the console retries rather than
    rendering an empty catalog."""
    s = MagicMock()
    s.list_templates.side_effect = GitHubTemplateError(
        "Could not read the template catalog", kind="store_error"
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/github-templates?connection_id=conn1", headers=_headers())
    assert resp.status_code == 503


def test_store_error_detail_never_leaks_the_store_message(entra_settings):
    s = MagicMock()
    s.list_templates.side_effect = GitHubTemplateError(
        "Table agp-projects-xyz throughput exceeded", kind="store_error"
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/github-templates?connection_id=conn1", headers=_headers())
    assert resp.status_code == 503
    assert "agp-projects-xyz" not in resp.text
    assert "throughput" not in resp.text


def test_upload_passes_the_principal_as_created_by(entra_settings):
    """The audit field comes from the VALIDATED principal, never a form value."""
    s = MagicMock()
    s.upload_template.return_value = _sample_view(name="my-tpl")
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/github-templates",
            files={"file": ("t.zip", b"zipbytes", "application/zip")},
            data={
                "connection_id": "conn1", "name": "my-tpl", "framework": "strands",
                # A hostile form field must NOT become the audit value.
                "created_by": "attacker@evil.com",
            },
            headers=_headers(),
        )
    assert resp.status_code == 201
    _, kwargs = s.upload_template.call_args
    assert kwargs["created_by"] == "admin@x.com"


# --- PATCH unknown → 404 ----------------------------------------------------

def test_patch_unknown_404(entra_settings):
    s = MagicMock()
    s.patch_template.side_effect = GitHubTemplateError("Unknown template", kind="not_found")
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.patch(
            "/api/v1/github-templates/nope?connection_id=conn1",
            json={"tags": ["x"]},
            headers=_headers(),
        )
    assert resp.status_code == 404
