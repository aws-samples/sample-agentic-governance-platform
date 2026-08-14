"""Tenant admin route tests (Epic 24, Task T2).

Exercises the REAL require_role/current_principal path against a mocked
verify_entra_token (no live Entra) and a FAKE TenantService patched onto the
router module's shared ``_svc`` (no live AWS). Mirrors test_projects_routes.py
for the app/client + role auth-override idiom.

ALL tenant endpoints are ADMIN-gated (spec §2 — members get their tenants from
/users/me, Task 4), so VIEWER *and* OPERATOR are forbidden on every endpoint.

The DELETE reference check runs IN THE ROUTE: it counts ``tenant_id`` matches
across the agent registry, MCP registry, and project services. Those three
sources are reached via the tenants module's ``_list_agents``/``_list_mcp_servers``/
``_list_projects`` wrappers, which the tests patch directly. A referencing
resource OR any error reading a source ("cannot verify") ⇒ 409.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from models.tenant import DatabricksStageConfig, Tenant, TenantStageConfig
from services.tenant_service import TenantError


@pytest.fixture(autouse=True)
def reset_modules():
    import sys

    for mod in [
        "core.rbac",
        "core.security_entra",
        "core.config",
        "api.routes.tenants",
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
    import api.routes.tenants as tenants_module

    # Patch the lazy TenantService singleton so no live AWS is ever touched.
    # reset_modules re-imports tenants fresh each test.
    tenants_module._svc = fake_svc
    # Default the reference-check sources to "no references" so DELETE tests that
    # do not care about references don't reach the real registry/project services.
    tenants_module._list_agents = lambda: []
    tenants_module._list_mcp_servers = lambda: []
    tenants_module._list_projects = lambda: []

    app = FastAPI()
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


# --- RBAC: every endpoint is ADMIN-only (403 for viewer AND operator) --------

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
        resp = client.put(
            "/api/v1/admin/tenants/ten-abc123", json={"name": "x"}, headers=_headers()
        )
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


# --- GET /admin/tenants list ------------------------------------------------

def test_list_tenants_admin_ok(entra_settings):
    s = MagicMock()
    s.list.return_value = [_tenant()]
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/tenants", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()[0]["id"] == "ten-abc123"


# --- POST /admin/tenants: 201, created_by from principal --------------------

def test_create_tenant_admin_201(entra_settings):
    s = MagicMock()
    s.create.return_value = _tenant()
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/tenants", json=_create_body(), headers=_headers())
    assert resp.status_code == 201
    assert resp.json()["id"] == "ten-abc123"
    # created_by is taken from the principal, never a body field.
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


# --- GET /admin/tenants/{id}: 200 present, 404 absent (FIXED literal) -------

def test_get_tenant_admin_200(entra_settings):
    s = MagicMock()
    s.get.return_value = _tenant()
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/tenants/ten-abc123", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["id"] == "ten-abc123"


def test_get_tenant_404(entra_settings):
    s = MagicMock()
    s.get.side_effect = TenantError("secret internals", kind="not_found")
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/tenants/nope", headers=_headers())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Tenant not found"
    assert "secret internals" not in resp.text


# --- PUT /admin/tenants/{id}: 200, 404, 400 ---------------------------------

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
        resp = client.put(
            "/api/v1/admin/tenants/nope", json={"name": "x"}, headers=_headers()
        )
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


# --- DELETE /admin/tenants/{id}: 204, 404, 409 (referenced / cannot verify) --

def test_delete_tenant_admin_204(entra_settings):
    s = MagicMock()
    s.get.return_value = _tenant()
    s.delete.return_value = None
    tenants_module, client = _build_client(s)
    # No referencing resources anywhere (defaults set in _build_client).
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


def test_delete_tenant_referenced_409(entra_settings):
    s = MagicMock()
    s.get.return_value = _tenant("ten-abc123")
    tenants_module, client = _build_client(s)
    # A project references this tenant (defensive getattr reads tenant_id).
    referencing = MagicMock()
    referencing.tenant_id = "ten-abc123"
    tenants_module._list_projects = lambda: [referencing]
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.delete("/api/v1/admin/tenants/ten-abc123", headers=_headers())
    assert resp.status_code == 409
    assert resp.json()["detail"] == "tenant is referenced by existing resources"
    s.delete.assert_not_called()


def test_delete_tenant_cannot_verify_409(entra_settings):
    s = MagicMock()
    s.get.return_value = _tenant("ten-abc123")
    tenants_module, client = _build_client(s)

    def _boom():
        raise RuntimeError("registry unavailable")

    # One source errors → "cannot verify" → fail-closed 409.
    tenants_module._list_agents = _boom
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.delete("/api/v1/admin/tenants/ten-abc123", headers=_headers())
    assert resp.status_code == 409
    assert resp.json()["detail"] == "tenant is referenced by existing resources"
    assert "registry unavailable" not in resp.text
    s.delete.assert_not_called()


# --- E29/T1: platform-typed tenants over the wire ---------------------------

WS_URL = "https://dbc-test.cloud.databricks.com"


def _dbx_tenant(id="ten-dbx001"):
    return Tenant(
        id=id,
        name="Analytics LoB",
        line_of_business="Analytics",
        entra_group_ids=["g1"],
        platform="databricks",
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
        "stages": {"dev": {"workspace_url": WS_URL, "workspace_id": "1234567890123456",
                           "sp_client_id": "sp-abc"}},
        "description": "dbx tenant",
    }


def test_create_databricks_tenant_admin_201(entra_settings):
    """A Databricks body parses into ``TenantCreate`` and reaches the service with its
    platform + Databricks-shaped stage intact (no shape coercion at the route boundary)."""
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
    s = MagicMock()
    s.list.return_value = [_tenant(), _dbx_tenant()]
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/admin/tenants", headers=_headers())
    assert resp.status_code == 200
    aws, dbx = resp.json()
    # A pre-E29-shaped AWS tenant serializes with the defaults, not with keys missing.
    assert aws["platform"] == "aws"
    assert aws["capabilities"] == {} and aws["binding_mode"] == ""
    assert dbx["platform"] == "databricks"
    assert dbx["capabilities"] == {"can_discover": True, "account_admin": False}
    assert dbx["binding_mode"] == "sp_secret"


def test_create_tenant_rejects_unknown_platform_422(entra_settings):
    """An unknown platform is refused by the model at the boundary — it never reaches the
    service, so no tenant can be created on a platform AGP cannot govern."""
    s = MagicMock()
    _, client = _build_client(s)
    body = _dbx_create_body() | {"platform": "gcp-vertex"}
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/tenants", json=body, headers=_headers())
    assert resp.status_code == 422
    s.create.assert_not_called()


def test_update_tenant_ignores_platform_in_body(entra_settings):
    """``platform`` is immutable: the PUT model has no such field, so the key is dropped
    and the service is never asked to re-type a live tenant."""
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
    """A rejected workspace_url surfaces the FIXED literal — the offending value and the
    service's own message never reach the client."""
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


# =========================================================================== #
# E29/T3 — GET /admin/tenants/{id}/discovered-agents
#
# The discovery route. Everything platform-specific lives behind the ``runtime_catalog`` seam,
# so these tests patch the module's ``_catalog_for`` hook (the ``_list_agents`` idiom above) and
# assert only what the ROUTE owns: admin gating, the stage guard, the envelope, the
# ``already_registered`` join against the registry, and — the one that matters most — that a
# platform failure becomes a 502 carrying a SAFE CODE and nothing else.
# =========================================================================== #

def _found(name="fraud-agent", handle=f"{WS_URL}/apps/fraud-agent", kind="app",
           state="RUNNING", created_by="a@b.com"):
    from services.runtime_catalog import DiscoveredAgent

    return DiscoveredAgent(name=name, runtime_handle=handle, kind=kind, state=state,
                           created_by=created_by)


def _with_catalog(tenants_module, agents=None, error=None):
    """Patch the module's catalog hook with a fake whose ``list_agents`` returns/raises."""
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
        resp = client.get(
            "/api/v1/admin/tenants/ten-dbx001/discovered-agents?stage=dev", headers=_headers())
    assert resp.status_code == 403
    s.get.assert_not_called()


def test_discovered_agents_returns_the_agents_and_the_platform(entra_settings):
    s = MagicMock()
    s.get.return_value = _dbx_tenant()
    mod, client = _build_client(s)
    fake = _with_catalog(mod, [_found()])
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get(
            "/api/v1/admin/tenants/ten-dbx001/discovered-agents?stage=dev", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["platform"] == "databricks"
    assert body["agents"] == [{
        "name": "fraud-agent",
        "runtime_handle": f"{WS_URL}/apps/fraud-agent",
        "kind": "app",
        "state": "RUNNING",
        "created_by": "a@b.com",
        "already_registered": False,
    }]
    assert fake.calls == [("ten-dbx001", "dev")]


def test_discovered_agents_reports_an_aws_tenants_platform_too(entra_settings):
    """The envelope's ``platform`` is what T8 branches on to build a create body, so it must be
    right for BOTH platforms — not just the one this epic added."""
    s = MagicMock()
    s.get.return_value = _tenant()
    mod, client = _build_client(s)
    _with_catalog(mod, [_found(kind="agentcore_runtime",
                              handle="arn:aws:bedrock-agentcore:us-east-1:111111111111:runtime/x")])
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get(
            "/api/v1/admin/tenants/ten-abc123/discovered-agents?stage=dev", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["platform"] == "aws"


def test_discovered_agents_empty_list_is_a_200_not_an_error(entra_settings):
    """A workspace with no agents is an ordinary answer and must stay distinguishable from a
    failure — an operator seeing an error would go looking for a broken credential."""
    s = MagicMock()
    s.get.return_value = _dbx_tenant()
    mod, client = _build_client(s)
    _with_catalog(mod, [])
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get(
            "/api/v1/admin/tenants/ten-dbx001/discovered-agents?stage=dev", headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == {"agents": [], "platform": "databricks"}


def test_discovered_agents_400_for_an_unknown_stage(entra_settings):
    """The stage guard is the ROUTE's, ahead of the adapter: an unknown stage is the caller's
    mistake, not a platform failure, and 400 vs 502 is what tells those apart."""
    s = MagicMock()
    s.get.return_value = _dbx_tenant()
    mod, client = _build_client(s)
    fake = _with_catalog(mod, [_found()])
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get(
            "/api/v1/admin/tenants/ten-dbx001/discovered-agents?stage=nope", headers=_headers())
    assert resp.status_code == 400
    assert resp.json()["detail"] == "unknown stage"
    assert fake.calls == []  # no platform call was made for a stage that does not exist


def test_discovered_agents_404_for_an_unknown_tenant(entra_settings):
    s = MagicMock()
    s.get.side_effect = TenantError("Unknown tenant", kind="not_found")
    mod, client = _build_client(s)
    _with_catalog(mod, [_found()])
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get(
            "/api/v1/admin/tenants/ten-nope/discovered-agents?stage=dev", headers=_headers())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Tenant not found"


def test_discovered_agents_requires_the_stage_query_param(entra_settings):
    s = MagicMock()
    s.get.return_value = _dbx_tenant()
    mod, client = _build_client(s)
    _with_catalog(mod, [_found()])
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get(
            "/api/v1/admin/tenants/ten-dbx001/discovered-agents", headers=_headers())
    assert resp.status_code == 422


def test_discovered_agents_502_with_a_safe_code_when_the_platform_is_unreachable(entra_settings):
    """A CatalogError becomes a 502 whose detail carries the safe CODE and nothing else — the
    upstream message can name workspace paths and principal ids."""
    from services.runtime_catalog import CatalogError

    s = MagicMock()
    s.get.return_value = _dbx_tenant()
    mod, client = _build_client(s)
    _with_catalog(mod, error=CatalogError(
        "Databricks rejected the request (list apps, status 403) at /Workspace/secret",
        kind="PERMISSION_DENIED"))
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get(
            "/api/v1/admin/tenants/ten-dbx001/discovered-agents?stage=dev", headers=_headers())
    assert resp.status_code == 502
    assert "PERMISSION_DENIED" in resp.json()["detail"]
    assert "/Workspace/secret" not in resp.text
    assert "status 403" not in resp.text


def test_discovered_agents_502_never_echoes_an_unsafe_kind(entra_settings):
    """``CatalogError`` normalises a non-code ``kind`` to "unknown" — pinned from the route's
    side, because this is the value that reaches a browser."""
    from services.runtime_catalog import CatalogError

    s = MagicMock()
    s.get.return_value = _dbx_tenant()
    mod, client = _build_client(s)
    _with_catalog(mod, error=CatalogError("failed", kind="/Workspace/secret/path 403"))
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get(
            "/api/v1/admin/tenants/ten-dbx001/discovered-agents?stage=dev", headers=_headers())
    assert resp.status_code == 502
    assert "Workspace" not in resp.text


def test_discovered_agents_502_when_the_adapter_raises_something_unexpected(entra_settings):
    """Nothing from an adapter reaches a client raw. An unexpected exception is still a
    discovery failure, not a 500 with a traceback-shaped detail."""
    s = MagicMock()
    s.get.return_value = _dbx_tenant()
    mod, client = _build_client(s)
    _with_catalog(mod, error=RuntimeError("botocore: AccessDeniedException on arn:aws:iam::x"))
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get(
            "/api/v1/admin/tenants/ten-dbx001/discovered-agents?stage=dev", headers=_headers())
    assert resp.status_code == 502
    assert "AccessDenied" not in resp.text
    assert "arn:aws:iam" not in resp.text


# --- already_registered: the join against AGP's own registry ----------------

def _registry_agent(runtime_handle=None, agent_arn=None):
    a = MagicMock()
    a.runtime_handle = runtime_handle
    a.agent_arn = agent_arn
    return a


def test_discovered_agents_flags_an_already_registered_runtime_handle(entra_settings):
    s = MagicMock()
    s.get.return_value = _dbx_tenant()
    mod, client = _build_client(s)
    _with_catalog(mod, [_found(), _found(name="new", handle=f"{WS_URL}/apps/new")])
    mod._list_agents = lambda: [_registry_agent(runtime_handle=f"{WS_URL}/apps/fraud-agent")]
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get(
            "/api/v1/admin/tenants/ten-dbx001/discovered-agents?stage=dev", headers=_headers())
    assert resp.status_code == 200
    flags = {a["name"]: a["already_registered"] for a in resp.json()["agents"]}
    assert flags == {"fraud-agent": True, "new": False}


def test_discovered_agents_flags_an_already_registered_agent_arn(entra_settings):
    """The OTHER currency: an AgentCore agent's handle is stored as ``agent_arn``, so matching
    only ``runtime_handle`` would offer an operator a re-registration of every AWS agent."""
    arn = "arn:aws:bedrock-agentcore:us-east-1:111111111111:runtime/fraud"
    s = MagicMock()
    s.get.return_value = _tenant()
    mod, client = _build_client(s)
    _with_catalog(mod, [_found(name="fraud", handle=arn, kind="agentcore_runtime")])
    mod._list_agents = lambda: [_registry_agent(agent_arn=arn)]
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get(
            "/api/v1/admin/tenants/ten-abc123/discovered-agents?stage=dev", headers=_headers())
    assert resp.json()["agents"][0]["already_registered"] is True


def test_discovered_agents_still_lists_when_the_registry_cannot_be_read(entra_settings):
    """An unreadable registry means AGP cannot say what is already governed — so nothing is
    flagged and the LISTING still ships. Failing the whole discovery closed would be the wrong
    direction here: the operator's next step (register, which re-checks) is safe either way,
    and a duplicate registration is refused downstream."""
    s = MagicMock()
    s.get.return_value = _dbx_tenant()
    mod, client = _build_client(s)
    _with_catalog(mod, [_found()])

    def _boom():
        raise RuntimeError("registry unavailable")

    mod._list_agents = _boom
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get(
            "/api/v1/admin/tenants/ten-dbx001/discovered-agents?stage=dev", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["agents"][0]["already_registered"] is False


# =========================================================================== #
# FIX round 1 — secret_error mapping + the settings-driven secret prefix
# =========================================================================== #

def test_create_tenant_secret_fault_is_502_not_400(entra_settings):
    """A Secrets Manager fault is OURS, not the caller's. 400 "invalid tenant" sent an operator
    to edit a form that was already correct; 502 says "retry this unchanged" (connections.py's
    mapping for the same kind)."""
    s = MagicMock()
    s.create.side_effect = TenantError(
        "Failed to store the tenant credential", kind="secret_error")
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/tenants", json=_dbx_create_body(), headers=_headers())
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Secret store operation failed"


def test_update_tenant_secret_fault_is_502(entra_settings):
    """The rotation path takes the same mapping — a re-connect is where a secret fault is most
    likely, since the secret name already exists."""
    s = MagicMock()
    s.update.side_effect = TenantError(
        "Failed to store the tenant credential", kind="secret_error")
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.put("/api/v1/admin/tenants/ten-dbx001",
                          json={"description": "x"}, headers=_headers())
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Secret store operation failed"


def test_a_secret_fault_never_echoes_the_services_own_message(entra_settings):
    """FIXED literals throughout — a store message could name an ARN or a KMS key."""
    s = MagicMock()
    s.create.side_effect = TenantError(
        "kms key arn:aws:kms:us-east-1:111111111111:key/abc unavailable", kind="secret_error")
    _, client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/admin/tenants", json=_dbx_create_body(), headers=_headers())
    assert resp.status_code == 502
    assert "kms" not in resp.text.lower()
    assert "arn:aws:kms" not in resp.text


def test_the_tenant_service_is_built_with_the_settings_secret_prefix(monkeypatch):
    """The gap the T3 report flagged: without this wiring every environment writes tenant
    secrets under the `agp-dev/` prefix regardless of stage."""
    monkeypatch.setenv("DATABRICKS_TENANT_SECRET_PREFIX", "agp-prod/databricks-tenants/")
    import importlib
    import sys

    sys.modules.pop("core.config", None)
    sys.modules.pop("api.routes.tenants", None)
    import api.routes.tenants as fresh

    fresh = importlib.reload(fresh)
    fresh._svc = None
    svc = fresh.get_tenant_service()
    assert svc.secret_prefix == "agp-prod/databricks-tenants/"
    fresh._svc = None


def test_the_secret_prefix_defaults_without_an_env_override():
    import importlib
    import sys

    sys.modules.pop("core.config", None)
    sys.modules.pop("api.routes.tenants", None)
    import api.routes.tenants as fresh

    fresh = importlib.reload(fresh)
    fresh._svc = None
    assert fresh.get_tenant_service().secret_prefix == "agp-dev/databricks-tenants/"
    fresh._svc = None
