"""The template catalog routes: the real ``require_role``/``current_principal`` path against a
mocked ``verify_entra_token`` and a fake ``GitHubTemplateService`` on the router's module-level
``_svc``; no live GitHub or AWS."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.github_template_service import GitHubTemplateError, TemplateView
from services.infra_template_registry import (
    InfraTemplateRegistryError,
    InfraTemplateRegistryValidationError,
)

pytestmark = pytest.mark.usefixtures("reset_modules")

_TEMPLATE_ROWS = [
    ("invalid_zip", 422, "Invalid template zip"),
    ("invalid_input", 422, "Invalid template metadata"),
    ("not_found", 404, "Template not found"),
    ("github_error", 502, "GitHub template operation failed"),
    ("store_error", 503, "Template catalog is temporarily unavailable"),
]


def _build_client(fake_svc, infra_registry=None):
    import api.routes.github_templates as gh_module
    from core.errors import install_error_handlers

    gh_module._svc = fake_svc
    if infra_registry is not None:
        gh_module._infra_registry = infra_registry

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(gh_module.router, prefix="/api/v1")
    app.include_router(gh_module.infra_templates_router, prefix="/api/v1")
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


def test_list_returns_the_artifact_kind_through_the_infra_join(entra_settings):
    """The join rewrites ``platform`` and ``build_kind`` only; the record-owned kind survives it."""
    s = MagicMock()
    s.list_templates.return_value = [
        _sample_view(),
        _sample_view("gateway-agentcore").model_copy(update={"artifact_kind": "mcp_server"}),
    ]
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/github-templates?connection_id=conn1", headers=_headers())
    assert resp.status_code == 200
    kinds = {v["name"]: v["artifact_kind"] for v in resp.json()}
    assert kinds == {"strands-agentcore": "agent", "gateway-agentcore": "mcp_server"}


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


def test_list_github_error_502(entra_settings):
    s = MagicMock()
    s.list_templates.side_effect = GitHubTemplateError("raw gh body", kind="github_error")
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/github-templates?connection_id=conn1", headers=_headers())
    assert resp.status_code == 502
    assert "raw gh body" not in resp.text


def test_delete_unknown_404(entra_settings):
    s = MagicMock()
    s.delete_template.side_effect = GitHubTemplateError("Unknown template", kind="not_found")
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.delete("/api/v1/github-templates/nope?connection_id=conn1", headers=_headers())
    assert resp.status_code == 404


def test_delete_admin_204(entra_settings):
    s = MagicMock()
    s.delete_template.return_value = None
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.delete("/api/v1/github-templates/my-tpl?connection_id=conn1", headers=_headers())
    assert resp.status_code == 204
    s.delete_template.assert_called_once_with("conn1", "my-tpl")


def test_invalid_input_is_422_not_503(entra_settings):
    """A malformed name or connection id is permanent: 422, never a retry prompt."""
    s = MagicMock()
    s.delete_template.side_effect = GitHubTemplateError(
        "template id must not contain '#'", kind="invalid_input"
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.delete("/api/v1/github-templates/a%23b?connection_id=conn1", headers=_headers())
    assert resp.status_code == 422
    assert resp.status_code != 503


def test_store_error_is_503_retryable(entra_settings):
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


@pytest.mark.parametrize("kind,status,detail", _TEMPLATE_ROWS, ids=[row[0] for row in _TEMPLATE_ROWS])
def test_every_template_error_kind_renders_its_fixed_detail(entra_settings, kind, status, detail):
    s = MagicMock()
    s.list_templates.side_effect = GitHubTemplateError(
        "Table agp-projects-xyz throughput exceeded", kind=kind
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/github-templates?connection_id=conn1", headers=_headers())
    assert resp.status_code == status
    assert resp.json() == {"detail": detail, "code": f"github_template.{kind}"}
    assert "agp-projects-xyz" not in resp.text


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("patch", "/api/v1/github-templates/nope?connection_id=conn1", {"json": {"tags": ["x"]}}),
        ("delete", "/api/v1/github-templates/nope?connection_id=conn1", {}),
    ],
)
def test_the_write_routes_render_a_template_error_through_the_table(entra_settings, method, path, kwargs):
    s = MagicMock()
    s.patch_template.side_effect = GitHubTemplateError("Unknown template", kind="not_found")
    s.delete_template.side_effect = GitHubTemplateError("Unknown template", kind="not_found")
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = getattr(client, method)(path, headers=_headers(), **kwargs)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Template not found", "code": "github_template.not_found"}


def test_upload_passes_the_principal_as_created_by(entra_settings):
    """The audit field comes from the validated principal; a hostile form field is ignored."""
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
                "framework": "strands",
                "created_by": "attacker@evil.com",
            },
            headers=_headers(),
        )
    assert resp.status_code == 201
    _, kwargs = s.upload_template.call_args
    assert kwargs["created_by"] == "admin@x.com"


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


def test_infra_list_renders_a_malformed_request_as_422_with_the_fixed_detail(entra_settings):
    registry = MagicMock()
    registry.list.side_effect = InfraTemplateRegistryValidationError("connection id must not contain '#'")
    client = _build_client(MagicMock(), infra_registry=registry)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/projects/templates/infra?connection_id=conn%231", headers=_headers())
    assert resp.status_code == 422
    assert resp.json() == {"detail": "Invalid infra template request", "code": "infra_template.validation"}


def test_infra_list_renders_a_store_fault_as_503_with_the_fixed_detail(entra_settings):
    registry = MagicMock()
    registry.list.side_effect = InfraTemplateRegistryError("Table agp-projects-xyz throughput exceeded")
    client = _build_client(MagicMock(), infra_registry=registry)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/projects/templates/infra?connection_id=conn1", headers=_headers())
    assert resp.status_code == 503
    assert resp.json() == {
        "detail": "Infra template catalog is temporarily unavailable",
        "code": "infra_template.store_error",
    }
    assert "agp-projects-xyz" not in resp.text


def test_upload_renders_a_corrupt_connection_secret_as_github_error_502(entra_settings):
    """The real template service over the real connection service whose stored secret no longer
    decodes: the connection's ``secret_error`` is the catalog's ``github_error`` row, never a 500,
    and neither the secret body nor the store's text reaches the client."""
    import io
    import zipfile

    from models.connection import ConnectionCreate, Provider
    from services.connection_service import ConnectionService
    from services.connection_verify import VerifyResult
    from services.github_template_service import GitHubTemplateService
    from services.template_registry import TemplateRegistry

    sm = MagicMock()
    sm.create_secret.return_value = {"ARN": "secret-arn-1"}
    connections = ConnectionService(
        secrets_client=sm,
        verify=lambda *a, **k: VerifyResult(ok=True, account_login="acme", reason=None),
        new_id=lambda: "conn1",
    )
    connections.create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"), created_by="op@x.com"
    )
    sm.get_secret_value.return_value = {"SecretString": "not json at all"}
    svc = GitHubTemplateService(
        github_repo_service=MagicMock(), connection_service=connections, template_registry=TemplateRegistry()
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("app.py", "print('hi')\n")
    client = _build_client(svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/github-templates",
            files={"file": ("t.zip", buf.getvalue(), "application/zip")},
            data={"connection_id": "conn1", "name": "my-tpl", "framework": "strands"},
            headers=_headers(),
        )

    assert resp.status_code == 502
    assert resp.json() == {
        "detail": "GitHub template operation failed",
        "code": "github_template.github_error",
    }
    assert "not json" not in resp.text
