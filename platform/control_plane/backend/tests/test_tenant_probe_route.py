"""``POST /admin/tenants/{id}/probe``: re-runs the connect-time probe against the stored config,
persists the result and returns the tenant projection with the derived ``readiness`` map.

The real ``require_role`` / ``current_principal`` path runs against a mocked ``verify_entra_token``;
a fake ``TenantService`` is patched onto the router module's ``_svc``. The projection's
``readiness`` is not mocked: the fake returns a real ``Tenant`` and the route derives it.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from models.tenant import DatabricksStageConfig, Tenant, TenantBindingMode, TenantPlatform, TenantStageConfig
from services.tenant_service import TenantError

pytestmark = pytest.mark.usefixtures("reset_modules")


def _build_client(fake_svc):
    import api.routes.tenants as tenants_module
    from core.errors import install_error_handlers

    tenants_module._svc = fake_svc
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(tenants_module.router, prefix="/api/v1")
    return tenants_module, TestClient(app)


def _claims_for(role: str):
    role_app = {
        "viewer": "Platform.Viewer",
        "operator": "Platform.Operator",
        "admin": "Platform.Admin",
    }[role]
    return {"oid": f"{role}-oid", "preferred_username": f"{role}@x.com", "roles": [role_app]}


def _headers():
    return {"Authorization": "Bearer fake-token"}


def _aws_tenant(id="ten-aws1"):
    """An AWS tenant whose one deploy role the probe found unreachable."""
    return Tenant(
        id=id,
        name="Fraud LoB",
        line_of_business="Fraud",
        entra_group_ids=["g1"],
        stages={
            "prod": TenantStageConfig(
                account_id="222222222222",
                deploy_role_arn="arn:aws:iam::222222222222:role/agp-deployment-prod",
            ),
        },
        capabilities={"can_deploy_reach:prod": False},
        binding_mode="",
        description="fraud tenant",
        created_by="admin@x.com",
        created_at="2026-07-13T00:00:00+00:00",
        updated_at="2026-07-14T00:00:00+00:00",
    )


def _dbx_tenant(id="ten-dbx1", binding_mode: TenantBindingMode = "federation"):
    return Tenant(
        id=id,
        name="Analytics LoB",
        line_of_business="Analytics",
        entra_group_ids=["g1"],
        platform=TenantPlatform.DATABRICKS,
        stages={
            "dev": DatabricksStageConfig(
                workspace_url="https://dbc-test.cloud.databricks.com",
                workspace_id="123",
                sp_client_id="sp-abc",
            )
        },
        capabilities={"can_discover": True, "account_admin": True, "user_sync": True},
        binding_mode=binding_mode,
        description="analytics tenant",
        created_by="admin@x.com",
        created_at="2026-07-13T00:00:00+00:00",
        updated_at="2026-07-14T00:00:00+00:00",
    )


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_probe_tenant_forbidden(entra_settings, role):
    s = MagicMock()
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for(role)):
        resp = client.post("/api/v1/admin/tenants/ten-aws1/probe", headers=_headers())
    assert resp.status_code == 403
    s.reprobe.assert_not_called()


def test_probe_tenant_admin_returns_projection_with_readiness(entra_settings):
    """Every stored field plus the derived ``readiness`` map keyed by verb; the failed deploy
    reach renders as unavailable on the ``deploy`` verb, scoped to its stage."""
    s = MagicMock()
    s.reprobe.return_value = _aws_tenant()
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/tenants/ten-aws1/probe", headers=_headers())

    assert resp.status_code == 200
    s.reprobe.assert_called_once_with("ten-aws1")
    body = resp.json()
    assert body["id"] == "ten-aws1"
    assert body["capabilities"] == {"can_deploy_reach:prod": False}
    assert "readiness" in body
    assert body["readiness"]["deploy"]["state"] == "unavailable"
    assert body["readiness"]["deploy"]["reason"] == "deploy_role_not_assumable"
    assert body["readiness"]["deploy"]["stage"] == "prod"


def test_probe_readiness_entries_carry_their_own_verb(entra_settings):
    s = MagicMock()
    s.reprobe.return_value = _aws_tenant()
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/tenants/ten-aws1/probe", headers=_headers())

    assert resp.status_code == 200
    readiness = resp.json()["readiness"]
    assert readiness["deploy"]["verb"] == "deploy"
    assert {verb: entry["verb"] for verb, entry in readiness.items()} == {verb: verb for verb in readiness}
    assert readiness["invoke"]["stage"] is None


def test_probe_tenant_readiness_reflects_databricks_binding(entra_settings):
    s = MagicMock()
    s.reprobe.return_value = _dbx_tenant(binding_mode="federation")
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/tenants/ten-dbx1/probe", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["platform"] == "databricks"
    assert body["readiness"]["invoke"]["state"] == "ready"


def test_probe_tenant_invoke_unavailable_binding_renders_unavailable(entra_settings):
    s = MagicMock()
    s.reprobe.return_value = _dbx_tenant(binding_mode="invoke_unavailable")
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/tenants/ten-dbx1/probe", headers=_headers())

    body = resp.json()
    assert body["readiness"]["invoke"]["state"] == "unavailable"
    assert body["readiness"]["invoke"]["reason"] == "invoke_unavailable"


def test_probe_tenant_not_found_404(entra_settings):
    s = MagicMock()
    s.reprobe.side_effect = TenantError("secret internals", kind="not_found")
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/tenants/ten-missing/probe", headers=_headers())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Tenant not found"
    assert "secret internals" not in resp.text


def test_probe_tenant_secret_error_502(entra_settings):
    s = MagicMock()
    s.reprobe.side_effect = TenantError("raw boto message", kind="secret_error")
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/tenants/ten-aws1/probe", headers=_headers())
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Secret store operation failed"
    assert "raw boto message" not in resp.text


def test_probe_tenant_secret_never_leaks_in_projection(entra_settings):
    """The write-only ``sp_client_secret`` is excluded from every dump, so the projection cannot
    carry it even for a Databricks tenant."""
    s = MagicMock()
    s.reprobe.return_value = _dbx_tenant()
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/tenants/ten-dbx1/probe", headers=_headers())
    assert "sp_client_secret" not in resp.json()["stages"]["dev"]
