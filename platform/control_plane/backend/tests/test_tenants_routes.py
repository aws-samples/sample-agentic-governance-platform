"""Tenant admin route tests.

The real ``require_role`` / ``current_principal`` path runs against a mocked ``verify_entra_token``;
a fake ``TenantService`` is patched onto the router module's ``_svc``, and the ``AppError``
handler renders ``TenantError`` and ``CatalogError`` from their tables. Every tenant endpoint is
admin-gated, so viewer and operator are forbidden everywhere.

The DELETE reference check runs in the route over the module's ``_list_agents`` /
``_list_mcp_servers`` / ``_list_projects`` hooks, which the tests patch directly: a referencing
resource or a source that cannot be read answers 409.
"""

import inspect
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from models.tenant import DatabricksStageConfig, Tenant, TenantPlatform, TenantStageConfig, TenantView
from services.runtime_catalog import DiscoveredAgents
from services.tenant_service import TenantError

pytestmark = pytest.mark.usefixtures("reset_modules")


def _build_client(fake_svc):
    import api.routes.tenants as tenants_module
    from core.errors import install_error_handlers

    tenants_module._svc = fake_svc
    tenants_module._list_agents = lambda: []
    tenants_module._list_mcp_servers = lambda: []
    tenants_module._list_projects = lambda: []

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


def _tenant(id="ten-abc123"):
    return Tenant(
        id=id,
        name="Fraud LoB",
        line_of_business="Fraud",
        entra_group_ids=["g1"],
        stages={
            "dev": TenantStageConfig(account_id="111111111111"),
            "prod": TenantStageConfig(account_id="222222222222"),
        },
        description="fraud tenant",
        created_by="admin@x.com",
        created_at="2026-07-13T00:00:00+00:00",
        updated_at="2026-07-13T00:00:00+00:00",
    )


def _create_body():
    return {
        "name": "Fraud LoB",
        "line_of_business": "Fraud",
        "entra_group_ids": ["g1"],
        "stages": {
            "dev": {"account_id": "111111111111"},
            "prod": {"account_id": "222222222222"},
        },
        "description": "fraud tenant",
    }


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_list_tenants_forbidden(entra_settings, role):
    s = MagicMock()
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for(role)):
        resp = client.get("/api/v1/admin/tenants", headers=_headers())
    assert resp.status_code == 403
    s.list.assert_not_called()


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_create_tenant_forbidden(entra_settings, role):
    s = MagicMock()
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for(role)):
        resp = client.post("/api/v1/admin/tenants", json=_create_body(), headers=_headers())
    assert resp.status_code == 403
    s.create.assert_not_called()


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_get_tenant_forbidden(entra_settings, role):
    s = MagicMock()
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for(role)):
        resp = client.get("/api/v1/admin/tenants/ten-abc123", headers=_headers())
    assert resp.status_code == 403
    s.get.assert_not_called()


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_update_tenant_forbidden(entra_settings, role):
    s = MagicMock()
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for(role)):
        resp = client.put("/api/v1/admin/tenants/ten-abc123", json={"name": "x"}, headers=_headers())
    assert resp.status_code == 403
    s.update.assert_not_called()


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_delete_tenant_forbidden(entra_settings, role):
    s = MagicMock()
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for(role)):
        resp = client.delete("/api/v1/admin/tenants/ten-abc123", headers=_headers())
    assert resp.status_code == 403
    s.delete.assert_not_called()


def test_tenant_write_handlers_are_sync_so_the_probe_may_own_the_event_loop(entra_settings):
    """The service drives the Databricks capability probe through ``asyncio.run``, which raises
    under a running loop; FastAPI runs a sync handler in a threadpool, where there is none."""
    import api.routes.tenants as tenants_module

    for handler in (tenants_module.create_tenant, tenants_module.update_tenant, tenants_module.probe_tenant):
        assert not inspect.iscoroutinefunction(handler), handler.__name__


def test_list_tenants_admin_ok(entra_settings):
    s = MagicMock()
    s.list.return_value = [_tenant()]
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/tenants", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()[0]["id"] == "ten-abc123"


def test_list_tenants_carries_derived_readiness(entra_settings):
    """The list carries the same ``readiness`` the probe route does; a tenant that names no
    deploy role was never probed, so ``deploy`` renders ready."""
    s = MagicMock()
    s.list.return_value = [_tenant()]
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/tenants", headers=_headers())
    assert resp.status_code == 200
    readiness = resp.json()[0]["readiness"]
    assert readiness["deploy"] == {"verb": "deploy", "state": "ready", "reason": "", "stage": None}
    assert all(entry["verb"] == verb for verb, entry in readiness.items())


def _response_model_of(tenants_module, handler_name: str):
    (route,) = [r for r in tenants_module.router.routes if r.endpoint.__name__ == handler_name]
    return route.response_model


def test_list_and_probe_declare_the_tenant_view_and_discovery_its_envelope(entra_settings):
    tenants_module, _ = _build_client(MagicMock())
    assert _response_model_of(tenants_module, "list_tenants") == list[TenantView]
    assert _response_model_of(tenants_module, "probe_tenant") is TenantView
    assert _response_model_of(tenants_module, "list_discovered_agents") is DiscoveredAgents


def test_list_tenants_body_round_trips_through_the_tenant_view(entra_settings):
    s = MagicMock()
    s.list.return_value = [_tenant(), _dbx_tenant()]
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/tenants", headers=_headers())
    assert resp.status_code == 200
    for body in resp.json():
        assert TenantView.model_validate(body).model_dump(mode="json") == body


def test_create_tenant_admin_201(entra_settings):
    s = MagicMock()
    s.create.return_value = _tenant()
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/tenants", json=_create_body(), headers=_headers())
    assert resp.status_code == 201
    assert resp.json()["id"] == "ten-abc123"
    _, kwargs = s.create.call_args
    assert kwargs["created_by"] == "admin@x.com"


def test_create_tenant_name_taken_409(entra_settings):
    s = MagicMock()
    s.create.side_effect = TenantError("secret internals", kind="name_taken")
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/tenants", json=_create_body(), headers=_headers())
    assert resp.status_code == 409
    assert resp.json()["detail"] == "tenant name already exists"
    assert "secret internals" not in resp.text


def test_create_tenant_validation_400(entra_settings):
    s = MagicMock()
    s.create.side_effect = TenantError("secret internals", kind="validation")
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/tenants", json=_create_body(), headers=_headers())
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid tenant"
    assert "secret internals" not in resp.text


def test_get_tenant_admin_200(entra_settings):
    s = MagicMock()
    s.get.return_value = _tenant()
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/tenants/ten-abc123", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["id"] == "ten-abc123"


def test_tenants_not_found_is_404(entra_settings):
    """The domain route's wire for ``not_found``: the row every other route that resolves a
    tenant renders through the same table."""
    s = MagicMock()
    s.get.side_effect = TenantError("secret internals", kind="not_found")
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/tenants/nope", headers=_headers())
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Tenant not found", "code": "tenant.not_found"}
    assert "secret internals" not in resp.text


def test_update_tenant_admin_200(entra_settings):
    s = MagicMock()
    s.update.return_value = _tenant()
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.put(
            "/api/v1/admin/tenants/ten-abc123",
            json={"description": "updated"},
            headers=_headers(),
        )
    assert resp.status_code == 200
    assert resp.json()["id"] == "ten-abc123"
    args, _ = s.update.call_args
    assert args[0] == "ten-abc123"


def test_update_tenant_404(entra_settings):
    s = MagicMock()
    s.update.side_effect = TenantError("secret internals", kind="not_found")
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.put("/api/v1/admin/tenants/nope", json={"name": "x"}, headers=_headers())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Tenant not found"


def test_update_tenant_validation_400(entra_settings):
    s = MagicMock()
    s.update.side_effect = TenantError("secret internals", kind="validation")
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.put(
            "/api/v1/admin/tenants/ten-abc123",
            json={"stages": {"dev": {"account_id": "bad"}}},
            headers=_headers(),
        )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid tenant"


def test_delete_tenant_admin_204(entra_settings):
    s = MagicMock()
    s.get.return_value = _tenant()
    s.delete.return_value = None
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.delete("/api/v1/admin/tenants/ten-abc123", headers=_headers())
    assert resp.status_code == 204
    s.delete.assert_called_once_with("ten-abc123")


def test_delete_tenant_404(entra_settings):
    s = MagicMock()
    s.get.side_effect = TenantError("secret internals", kind="not_found")
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.delete("/api/v1/admin/tenants/nope", headers=_headers())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Tenant not found"
    s.delete.assert_not_called()


def test_delete_tenant_referenced_409(entra_settings, monkeypatch):
    s = MagicMock()
    s.get.return_value = _tenant("ten-abc123")
    tenants_module, client = _build_client(s)
    referencing = MagicMock()
    referencing.tenant_id = "ten-abc123"
    monkeypatch.setattr(tenants_module, "_list_projects", lambda: [referencing])
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.delete("/api/v1/admin/tenants/ten-abc123", headers=_headers())
    assert resp.status_code == 409
    assert resp.json() == {
        "detail": "tenant is referenced by existing resources",
        "code": "tenant.referenced",
    }
    s.delete.assert_not_called()


def test_delete_tenant_cannot_verify_409(entra_settings, monkeypatch):
    s = MagicMock()
    s.get.return_value = _tenant("ten-abc123")
    tenants_module, client = _build_client(s)

    def _boom():
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(tenants_module, "_list_agents", _boom)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.delete("/api/v1/admin/tenants/ten-abc123", headers=_headers())
    assert resp.status_code == 409
    assert resp.json() == {
        "detail": "tenant is referenced by existing resources",
        "code": "tenant.referenced",
    }
    assert "registry unavailable" not in resp.text
    s.delete.assert_not_called()


WS_URL = "https://dbc-test.cloud.databricks.com"


def _dbx_tenant(id="ten-dbx001"):
    return Tenant(
        id=id,
        name="Analytics LoB",
        line_of_business="Analytics",
        entra_group_ids=["g1"],
        platform=TenantPlatform.DATABRICKS,
        stages={
            "dev": DatabricksStageConfig(
                workspace_url=WS_URL, workspace_id="1234567890123456", sp_client_id="sp-abc"
            )
        },
        capabilities={"can_discover": True, "account_admin": False},
        binding_mode="sp_secret",
        description="dbx tenant",
        created_by="admin@x.com",
        created_at="2026-08-06T00:00:00+00:00",
        updated_at="2026-08-06T00:00:00+00:00",
    )


def _dbx_create_body():
    return {
        "name": "Analytics LoB",
        "line_of_business": "Analytics",
        "entra_group_ids": ["g1"],
        "platform": "databricks",
        "stages": {
            "dev": {"workspace_url": WS_URL, "workspace_id": "1234567890123456", "sp_client_id": "sp-abc"}
        },
        "description": "dbx tenant",
    }


def test_create_databricks_tenant_admin_201(entra_settings):
    """A Databricks body reaches the service with its platform and Databricks-shaped stage intact."""
    s = MagicMock()
    s.create.return_value = _dbx_tenant()
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/tenants", json=_dbx_create_body(), headers=_headers())
    assert resp.status_code == 201
    body = resp.json()
    assert body["platform"] == "databricks"
    assert body["stages"]["dev"]["workspace_url"] == WS_URL
    args, _ = s.create.call_args
    assert args[0].platform == "databricks"
    assert isinstance(args[0].stages["dev"], DatabricksStageConfig)


def test_list_tenants_exposes_platform_and_capability_fields(entra_settings):
    """An AWS tenant stored before platforms existed serializes with the defaults, not with keys
    missing."""
    s = MagicMock()
    s.list.return_value = [_tenant(), _dbx_tenant()]
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/tenants", headers=_headers())
    assert resp.status_code == 200
    aws, dbx = resp.json()
    assert aws["platform"] == "aws"
    assert aws["capabilities"] == {} and aws["binding_mode"] == ""
    assert dbx["platform"] == "databricks"
    assert dbx["capabilities"] == {"can_discover": True, "account_admin": False}
    assert dbx["binding_mode"] == "sp_secret"


def test_create_tenant_rejects_unknown_platform_422(entra_settings):
    """An unknown platform is refused by the model at the boundary and never reaches the service."""
    s = MagicMock()
    _, client = _build_client(s)
    body = _dbx_create_body() | {"platform": "gcp-vertex"}
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/tenants", json=body, headers=_headers())
    assert resp.status_code == 422
    s.create.assert_not_called()


def test_update_tenant_ignores_platform_in_body(entra_settings):
    """``platform`` is immutable: the PUT model has no such field, so the key is dropped."""
    s = MagicMock()
    s.update.return_value = _dbx_tenant()
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.put(
            "/api/v1/admin/tenants/ten-dbx001",
            json={"platform": "aws", "description": "updated"},
            headers=_headers(),
        )
    assert resp.status_code == 200
    args, _ = s.update.call_args
    assert not hasattr(args[1], "platform")
    assert "platform" not in args[1].model_dump(exclude_unset=True)
    assert resp.json()["platform"] == "databricks"


def test_create_databricks_tenant_validation_400_hides_detail(entra_settings):
    """A rejected workspace URL surfaces the fixed literal; the offending value and the service's
    message never reach the client."""
    s = MagicMock()
    s.create.side_effect = TenantError(
        "stage dev workspace_url must be an https workspace origin", kind="validation"
    )
    _, client = _build_client(s)
    body = _dbx_create_body()
    body["stages"]["dev"]["workspace_url"] = "javascript:alert(1)"
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/tenants", json=body, headers=_headers())
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid tenant"
    assert "workspace_url" not in resp.text
    assert "javascript" not in resp.text


def _found(
    name="fraud-agent", handle=f"{WS_URL}/apps/fraud-agent", kind="app", state="RUNNING", created_by="a@b.com"
):
    from services.runtime_catalog import DiscoveredAgent

    return DiscoveredAgent(name=name, runtime_handle=handle, kind=kind, state=state, created_by=created_by)


def _with_catalog(tenants_module, agents=None, error=None):
    """Patch the module's catalog hook with a fake whose ``list_agents`` returns or raises."""

    class _FakeCatalog:
        def __init__(self):
            self.calls = []

        async def list_agents(self, tenant, stage):
            self.calls.append((tenant.id, stage))
            if error:
                raise error
            return list(agents or [])

    fake = _FakeCatalog()
    tenants_module._catalog_for = lambda tenant: fake
    return fake


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_discovered_agents_forbidden(entra_settings, role):
    """Admin-gated like every sibling: discovery enumerates a customer's whole workspace."""
    s = MagicMock()
    s.get.return_value = _dbx_tenant()
    mod, client = _build_client(s)
    _with_catalog(mod, [_found()])
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for(role)):
        resp = client.get("/api/v1/admin/tenants/ten-dbx001/discovered-agents?stage=dev", headers=_headers())
    assert resp.status_code == 403
    s.get.assert_not_called()


def test_discovered_agents_returns_the_agents_and_the_platform(entra_settings):
    s = MagicMock()
    s.get.return_value = _dbx_tenant()
    mod, client = _build_client(s)
    fake = _with_catalog(mod, [_found()])
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/tenants/ten-dbx001/discovered-agents?stage=dev", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["platform"] == "databricks"
    assert body["agents"] == [
        {
            "name": "fraud-agent",
            "runtime_handle": f"{WS_URL}/apps/fraud-agent",
            "kind": "app",
            "state": "RUNNING",
            "created_by": "a@b.com",
            "already_registered": False,
        }
    ]
    assert fake.calls == [("ten-dbx001", "dev")]
    assert DiscoveredAgents.model_validate(body).model_dump(mode="json") == body


def test_discovered_agents_reports_an_aws_tenants_platform_too(entra_settings):
    """The registration wizard branches on the envelope's ``platform`` to build a create body, so
    it must be right for both platforms."""
    s = MagicMock()
    s.get.return_value = _tenant()
    mod, client = _build_client(s)
    _with_catalog(
        mod,
        [
            _found(
                kind="agentcore_runtime", handle="arn:aws:bedrock-agentcore:us-east-1:111111111111:runtime/x"
            )
        ],
    )
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/tenants/ten-abc123/discovered-agents?stage=dev", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["platform"] == "aws"


def test_discovered_agents_empty_list_is_a_200_not_an_error(entra_settings):
    """A workspace with no agents is an ordinary answer, distinguishable from a failure."""
    s = MagicMock()
    s.get.return_value = _dbx_tenant()
    mod, client = _build_client(s)
    _with_catalog(mod, [])
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/tenants/ten-dbx001/discovered-agents?stage=dev", headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == {"agents": [], "platform": "databricks"}


def test_discovered_agents_400_for_an_unknown_stage(entra_settings):
    """The stage guard is the route's and runs before the adapter: an unknown stage is the
    caller's mistake, not a platform failure, and no platform call is made for it."""
    s = MagicMock()
    s.get.return_value = _dbx_tenant()
    mod, client = _build_client(s)
    fake = _with_catalog(mod, [_found()])
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/tenants/ten-dbx001/discovered-agents?stage=nope", headers=_headers())
    assert resp.status_code == 400
    assert resp.json() == {"detail": "unknown stage", "code": "tenant.unknown_stage"}
    assert fake.calls == []


def test_discovered_agents_404_for_an_unknown_tenant(entra_settings):
    s = MagicMock()
    s.get.side_effect = TenantError("Unknown tenant", kind="not_found")
    mod, client = _build_client(s)
    _with_catalog(mod, [_found()])
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/tenants/ten-nope/discovered-agents?stage=dev", headers=_headers())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Tenant not found"


def test_discovered_agents_requires_the_stage_query_param(entra_settings):
    s = MagicMock()
    s.get.return_value = _dbx_tenant()
    mod, client = _build_client(s)
    _with_catalog(mod, [_found()])
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/tenants/ten-dbx001/discovered-agents", headers=_headers())
    assert resp.status_code == 422


def test_discovered_agents_502_with_a_safe_code_when_the_platform_is_unreachable(entra_settings):
    """A Databricks ``CatalogError`` renders its composed message, the fixed text plus the
    platform's own safe code, through the ``AppError`` handler."""
    from services.runtime_catalog import CatalogError

    s = MagicMock()
    s.get.return_value = _dbx_tenant()
    mod, client = _build_client(s)
    _with_catalog(
        mod, error=CatalogError("platform discovery failed (PERMISSION_DENIED)", kind="databricks_error")
    )
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/tenants/ten-dbx001/discovered-agents?stage=dev", headers=_headers())
    assert resp.status_code == 502
    assert resp.json() == {
        "detail": "platform discovery failed (PERMISSION_DENIED)",
        "code": "catalog.databricks_error",
    }


def test_discovered_agents_502_never_echoes_an_unsafe_kind(entra_settings):
    """A fixed-detail ``CatalogError`` kind renders the table's text; the adapter's message, which
    can name workspace paths and principal ids, never crosses."""
    from services.runtime_catalog import CatalogError

    s = MagicMock()
    s.get.return_value = _dbx_tenant()
    mod, client = _build_client(s)
    _with_catalog(
        mod,
        error=CatalogError(
            "Databricks rejected the request (list apps, status 403) at /Workspace/secret/path",
            kind="listing_failed",
        ),
    )
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/tenants/ten-dbx001/discovered-agents?stage=dev", headers=_headers())
    assert resp.status_code == 502
    assert resp.json() == {
        "detail": "platform discovery failed (listing_failed)",
        "code": "catalog.listing_failed",
    }
    assert "Workspace" not in resp.text
    assert "status 403" not in resp.text


def test_discovered_agents_502_when_the_adapter_raises_something_unexpected(entra_settings):
    """An exception that is not a ``CatalogError`` is still a discovery failure with a fixed
    detail, not a 500 with a traceback-shaped body."""
    s = MagicMock()
    s.get.return_value = _dbx_tenant()
    mod, client = _build_client(s)
    _with_catalog(mod, error=RuntimeError("botocore: AccessDeniedException on arn:aws:iam::x"))
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/tenants/ten-dbx001/discovered-agents?stage=dev", headers=_headers())
    assert resp.status_code == 502
    assert resp.json() == {"detail": "platform discovery failed (unknown)", "code": "tenant.discovery_failed"}
    assert "AccessDenied" not in resp.text
    assert "arn:aws:iam" not in resp.text


def _registry_agent(runtime_handle=None, agent_arn=None):
    a = MagicMock()
    a.runtime_handle = runtime_handle
    a.agent_arn = agent_arn
    return a


def test_discovered_agents_flags_an_already_registered_runtime_handle(entra_settings, monkeypatch):
    s = MagicMock()
    s.get.return_value = _dbx_tenant()
    mod, client = _build_client(s)
    _with_catalog(mod, [_found(), _found(name="new", handle=f"{WS_URL}/apps/new")])
    monkeypatch.setattr(
        mod, "_list_agents", lambda: [_registry_agent(runtime_handle=f"{WS_URL}/apps/fraud-agent")]
    )
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/tenants/ten-dbx001/discovered-agents?stage=dev", headers=_headers())
    assert resp.status_code == 200
    flags = {a["name"]: a["already_registered"] for a in resp.json()["agents"]}
    assert flags == {"fraud-agent": True, "new": False}


def test_discovered_agents_flags_an_already_registered_agent_arn(entra_settings, monkeypatch):
    """An AgentCore agent's handle is stored as ``agent_arn``, the other currency."""
    arn = "arn:aws:bedrock-agentcore:us-east-1:111111111111:runtime/fraud"
    s = MagicMock()
    s.get.return_value = _tenant()
    mod, client = _build_client(s)
    _with_catalog(mod, [_found(name="fraud", handle=arn, kind="agentcore_runtime")])
    monkeypatch.setattr(mod, "_list_agents", lambda: [_registry_agent(agent_arn=arn)])
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/tenants/ten-abc123/discovered-agents?stage=dev", headers=_headers())
    assert resp.json()["agents"][0]["already_registered"] is True


def test_discovered_agents_still_lists_when_the_registry_cannot_be_read(entra_settings, monkeypatch):
    """An unreadable registry drops the badge, not the listing: registration re-checks and
    refuses a duplicate downstream."""
    s = MagicMock()
    s.get.return_value = _dbx_tenant()
    mod, client = _build_client(s)
    _with_catalog(mod, [_found()])

    def _boom():
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(mod, "_list_agents", _boom)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/tenants/ten-dbx001/discovered-agents?stage=dev", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["agents"][0]["already_registered"] is False


def test_create_tenant_secret_fault_is_502_not_400(entra_settings):
    """A Secrets Manager fault is ours, not the caller's: 502 says retry unchanged, where 400
    would send an operator to edit a form that was already correct."""
    s = MagicMock()
    s.create.side_effect = TenantError("Failed to store the tenant credential", kind="secret_error")
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/tenants", json=_dbx_create_body(), headers=_headers())
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Secret store operation failed"


def test_update_tenant_secret_fault_is_502(entra_settings):
    s = MagicMock()
    s.update.side_effect = TenantError("Failed to store the tenant credential", kind="secret_error")
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.put("/api/v1/admin/tenants/ten-dbx001", json={"description": "x"}, headers=_headers())
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Secret store operation failed"


def test_a_secret_fault_never_echoes_the_services_own_message(entra_settings):
    s = MagicMock()
    s.create.side_effect = TenantError(
        "kms key arn:aws:kms:us-east-1:111111111111:key/abc unavailable", kind="secret_error"
    )
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/tenants", json=_dbx_create_body(), headers=_headers())
    assert resp.status_code == 502
    assert "kms" not in resp.text.lower()
    assert "arn:aws:kms" not in resp.text


def test_the_tenant_service_is_built_with_the_settings_secret_prefix(monkeypatch):
    """Without this wiring every environment writes tenant secrets under the module default."""
    monkeypatch.setenv("DATABRICKS_TENANT_SECRET_PREFIX", "agp-prod/databricks-tenants/")
    import importlib
    import sys

    sys.modules.pop("core.config", None)
    sys.modules.pop("api.routes.tenants", None)
    import api.routes.tenants as fresh

    fresh = importlib.reload(fresh)
    monkeypatch.setattr(fresh, "_svc", None)
    svc = fresh.get_tenant_service()
    assert svc.secret_prefix == "agp-prod/databricks-tenants/"


def test_the_secret_prefix_defaults_without_an_env_override(monkeypatch):
    import importlib
    import sys

    sys.modules.pop("core.config", None)
    sys.modules.pop("api.routes.tenants", None)
    import api.routes.tenants as fresh

    fresh = importlib.reload(fresh)
    monkeypatch.setattr(fresh, "_svc", None)
    assert fresh.get_tenant_service().secret_prefix == "agp-dev/databricks-tenants/"
