"""Template rollout + reconcile route tests (E22/T5, rebuilt in E28C/T3).

Exercises the REAL require_role/current_principal path against a mocked
verify_entra_token (no live Entra) and a FAKE RolloutService patched onto the
connections router's module-level ``_rollout_svc`` (no live GitHub / AWS). Mirrors
test_connections_routes.py for the app/client + admin/non-admin auth-override idiom.

E28C/T3 adds the ADOPT route and closes two honesty holes in the mapping: ``kind="validation"``
used to be FLATTENED to 502 on the reconcile route (a permanent caller error reported as a
retryable platform fault), and there was no 409 kind at all — "already registered" had nowhere
to land.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.github_template_service import TemplateView
from services.template_rollout_service import (
    INFRA_REPO_NAME,
    ReconcileItem,
    ReconcileView,
    RolloutError,
    RolloutResult,
    RolloutResultItem,
)


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


def _build_client(fake_svc):
    import api.routes.connections as connections_module

    connections_module._rollout_svc = fake_svc  # the lazy RolloutService singleton
    app = FastAPI()
    app.include_router(connections_module.router, prefix="/api/v1")
    return TestClient(app)


def _svc_with(**methods):
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


# --- reconcile happy path: 200, three-state rows + the forced infra row ------

def _view():
    return ReconcileView(
        templates=[
            ReconcileItem(
                name="strands-agentcore",
                origin="seed",
                state="registered_present",
                default_branch="main",
                head_sha="abc123",
            ),
            ReconcileItem(
                name="their-template",
                origin="org",
                state="unregistered_present",
                default_branch=None,
                head_sha=None,
            ),
        ],
        infra_repo=ReconcileItem(
            name=INFRA_REPO_NAME,
            origin="seed",
            state="seed_absent",
            default_branch=None,
            head_sha=None,
        ),
    )


def test_reconcile_returns_view_admin(entra_settings):
    s = _svc_with(reconcile={"return_value": _view()})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/connections/conn-1/rollout/reconcile", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    rows = {r["name"]: r for r in body["templates"]}
    assert rows["strands-agentcore"]["state"] == "registered_present"
    assert rows["strands-agentcore"]["origin"] == "seed"
    assert rows["strands-agentcore"]["head_sha"] == "abc123"
    assert rows["their-template"]["state"] == "unregistered_present"
    assert rows["their-template"]["head_sha"] is None
    assert body["infra_repo"]["name"] == INFRA_REPO_NAME
    assert body["infra_repo"]["state"] == "seed_absent"
    # The DDB-only boolean is gone from the wire shape, not renamed alongside it.
    assert "exists_in_org" not in rows["strands-agentcore"]
    assert "selectable" not in rows["strands-agentcore"]
    s.reconcile.assert_called_once_with("conn-1")


# --- rollout happy path: 200, per-item actions -------------------------------

def test_rollout_returns_result_admin(entra_settings):
    result = RolloutResult(
        items=[
            RolloutResultItem("strands-agentcore", "created"),
            RolloutResultItem(INFRA_REPO_NAME, "created"),
        ]
    )
    s = _svc_with(rollout={"return_value": result})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections/conn-1/rollout",
            json={"template_names": ["strands-agentcore"], "overwrite": False},
            headers=_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()
    names = {i["name"]: i["action"] for i in body["items"]}
    assert names["strands-agentcore"] == "created"
    assert names[INFRA_REPO_NAME] == "created"
    s.rollout.assert_called_once_with(
        "conn-1",
        template_names=["strands-agentcore"],
        overwrite=False,
        overwrite_infra=False,
    )


def test_rollout_body_carries_the_two_consents_separately(entra_settings):
    """E28D wire split: ``overwrite`` is the TEMPLATE consent, ``overwrite_infra`` the infra
    repo's own. A body that sets only ``overwrite`` must reach the service with
    ``overwrite_infra=False`` — the narrowing is on the wire, not just in the service."""
    s = _svc_with(rollout={"return_value": RolloutResult()})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections/conn-1/rollout",
            json={"template_names": ["strands-agentcore"], "overwrite": True},
            headers=_headers(),
        )
    assert resp.status_code == 200
    s.rollout.assert_called_once_with(
        "conn-1",
        template_names=["strands-agentcore"],
        overwrite=True,
        overwrite_infra=False,   # NOT inherited from ``overwrite``
    )


def test_rollout_infra_consent_is_settable_on_its_own(entra_settings):
    """Ticking only the infra re-push is a submittable run: no template names, no template
    overwrite, and the infra consent still arrives as ``True``."""
    s = _svc_with(rollout={"return_value": RolloutResult()})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections/conn-1/rollout",
            json={"template_names": [], "overwrite_infra": True},
            headers=_headers(),
        )
    assert resp.status_code == 200
    s.rollout.assert_called_once_with(
        "conn-1", template_names=[], overwrite=False, overwrite_infra=True
    )


# --- RBAC: both routes require ADMIN (403 for a viewer) ----------------------

def test_reconcile_viewer_forbidden(entra_settings):
    s = _svc_with(reconcile={"return_value": _view()})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/admin/connections/conn-1/rollout/reconcile", headers=_headers())
    assert resp.status_code == 403
    s.reconcile.assert_not_called()


def test_rollout_operator_forbidden(entra_settings):
    s = _svc_with(rollout={"return_value": RolloutResult()})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/admin/connections/conn-1/rollout",
            json={"template_names": [], "overwrite": False},
            headers=_headers(),
        )
    assert resp.status_code == 403
    s.rollout.assert_not_called()


# --- error mapping: unknown template → 404 fixed literal ---------------------

def test_rollout_unknown_template_404(entra_settings):
    s = _svc_with(
        rollout={"side_effect": RolloutError("Unknown base template 'x'", kind="not_found")}
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections/conn-1/rollout",
            json={"template_names": ["x"], "overwrite": False},
            headers=_headers(),
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Unknown base template"


def test_rollout_validation_is_422_not_502(entra_settings):
    s = _svc_with(
        rollout={"side_effect": RolloutError("Invalid template name", kind="validation")}
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections/conn-1/rollout",
            json={"template_names": ["Bad Name"], "overwrite": False},
            headers=_headers(),
        )
    assert resp.status_code == 422


# --- reconcile error honesty: validation is 422 here TOO ----------------------


def test_reconcile_validation_is_422_not_flattened_to_502(entra_settings):
    """The route used to catch ``(RolloutError, GitHubRepoError)`` and answer 502 for BOTH, so a
    malformed connection id — permanent, unretryable — told the console to retry."""
    s = _svc_with(
        reconcile={"side_effect": RolloutError("Invalid connection id", kind="validation")}
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/connections/conn%231/rollout/reconcile", headers=_headers())
    assert resp.status_code == 422


def test_reconcile_store_fault_is_still_502(entra_settings):
    """The guard on the test above: a genuine fault must NOT become a 422, or the console would
    stop retrying something a retry could fix."""
    s = _svc_with(
        reconcile={"side_effect": RolloutError("Could not read the catalog", kind="rollout_error")}
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/connections/conn-1/rollout/reconcile", headers=_headers())
    assert resp.status_code == 502


# --- adopt: 200 TemplateView / 404 / 409 / 422 --------------------------------


def test_adopt_returns_the_template_view_admin(entra_settings):
    s = _svc_with(
        adopt={
            "return_value": TemplateView(
                name="their-template",
                description="ours now",
                framework="strands",
                html_url="https://github.com/acme/their-template",
            )
        }
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections/conn-1/templates/adopt",
            json={"repo_name": "their-template", "description": "ours now"},
            headers=_headers(),
        )
    assert resp.status_code == 200
    assert resp.json()["name"] == "their-template"
    assert resp.json()["description"] == "ours now"
    # ``created_by`` comes from the VALIDATED principal, never from the body.
    s.adopt.assert_called_once_with(
        "conn-1",
        repo_name="their-template",
        description="ours now",
        created_by="admin@x.com",
    )


def test_adopt_repo_not_found_is_404(entra_settings):
    s = _svc_with(
        adopt={"side_effect": RolloutError("Repository not found", kind="repo_not_found")}
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections/conn-1/templates/adopt",
            json={"repo_name": "ghost"},
            headers=_headers(),
        )
    assert resp.status_code == 404
    # NOT the rollout path's "Unknown base template" — a different fact needs a different literal.
    assert resp.json()["detail"] == "Repository not found in the org"


def test_adopt_already_registered_is_409(entra_settings):
    s = _svc_with(
        adopt={"side_effect": RolloutError("Already registered", kind="conflict")}
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections/conn-1/templates/adopt",
            json={"repo_name": "strands-agentcore"},
            headers=_headers(),
        )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Template already registered"


def test_adopt_invalid_name_is_422(entra_settings):
    s = _svc_with(
        adopt={"side_effect": RolloutError("Invalid repository name", kind="validation")}
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/admin/connections/conn-1/templates/adopt",
            json={"repo_name": "Bad Name"},
            headers=_headers(),
        )
    assert resp.status_code == 422


def test_adopt_operator_forbidden(entra_settings):
    s = _svc_with(adopt={"return_value": TemplateView(name="x")})
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/admin/connections/conn-1/templates/adopt",
            json={"repo_name": "their-template"},
            headers=_headers(),
        )
    assert resp.status_code == 403
    s.adopt.assert_not_called()
