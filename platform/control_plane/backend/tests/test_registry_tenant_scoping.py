"""Tenant scoping of the registry routes: agents + MCP servers, + publish.

The enforcement heart of the epic. Every test here builds a MINIMAL app (ONLY the
router under test), patches the router's registry-service singleton (mocked, no
real AWS) and `api.routes.users._tenant_resolver` (the ONE resolver-singleton
accessor both agents.py and mcp_servers.py delegate to via their own
`get_tenant_ctx` — patching it here on `users_module` is observed everywhere).

Contract under test (brief):
  - List: post-filter with `visible(ctx, r.tenant_id)` (agents) /
    `visible(ctx, r.tenant_id, shared=r.shared)` (MCPs).
  - Detail/mutation/lifecycle/invoke routes: not visible -> 404 with the EXISTING
    not-found literal — BYTE-IDENTICAL to a truly-missing id (the 404-not-403
    contract; a foreign tenant's resource must look absent).
  - Create: unknown tenant -> 404 "Tenant not found"; foreign tenant (non-global)
    -> 403 "tenant not permitted".
  - Publish: PUT .../publish, body {"published": bool}, OPERATOR+,
    visibility-gated 404, flips the flag via the existing update path.
  - MCP `shared` settable only by ADMIN (create AND update) -> non-admin 403.
    ANY explicit `shared` in an update payload (True OR False) requires ADMIN
    (un-sharing is as privileged as sharing).
  - MCP `shared` grants cross-tenant READ visibility ONLY: GET detail/list keep the shared bypass, but every
    mutation/lifecycle/identity route treats a foreign shared MCP as 404
    (write requires own-tenant membership or global admin).
  - ADMIN bypasses all filtering (is_global=True).
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("reset_modules")


def _make_agent(**overrides):
    from models.agent import Agent, LifecycleState, Origin

    now = datetime.now(timezone.utc)
    base: dict[str, Any] = dict(
        id="rec-123",
        name="claims-triage-de",
        purpose="Triage claims",
        lifecycle_state=LifecycleState.PROPOSED,
        origin=Origin.REGISTERED,
        created_at=now,
        updated_at=now,
        created_by="maria.bauer@example.com",
        tenant_id="ten-1",
    )
    base.update(overrides)
    return Agent(**base)


def _make_mcp(**overrides):
    from models.mcp_server import Kind, LifecycleState, McpServer

    now = datetime.now(timezone.utc)
    base: dict[str, Any] = dict(
        id="mcp-123",
        name="claims-mcp-de",
        description="Claims MCP server",
        kind=Kind.STANDARD,
        lifecycle_state=LifecycleState.PROPOSED,
        created_at=now,
        updated_at=now,
        created_by="maria.bauer@example.com",
        tenant_id="ten-1",
        shared=False,
    )
    base.update(overrides)
    return McpServer(**base)


def _context(*, is_global=False, tenant_ids=()):
    from services.tenant_resolver import TenantContext

    return TenantContext(is_global=is_global, tenant_ids=frozenset(tenant_ids), tenants=())


def _global_project_context():
    from services.project_resolver import ProjectContext

    return ProjectContext(is_global=True, roles={})


class _FakeResolver:
    """Async ``resolve`` stub returning a fixed context regardless of principal."""

    def __init__(self, ctx):
        self._ctx = ctx

    async def resolve(self, principal):
        return self._ctx


class _FakeTenantService:
    """Minimal ``TenantService``-shaped fake: only ``.get`` is exercised by create()."""

    def __init__(self, known_ids):
        self._known = set(known_ids)

    def get(self, tenant_id):
        from services.tenant_service import TenantError

        if tenant_id not in self._known:
            raise TenantError("Unknown tenant", kind="not_found")
        return MagicMock(id=tenant_id)


def _seed_resolver(ctx):
    import api.routes.users as users_module

    users_module._tenant_resolver = _FakeResolver(ctx)
    users_module._project_resolver = _FakeResolver(_global_project_context())


def _seed_tenant_service(known_ids):
    """Patch ``tenants.get_tenant_service`` (imported lazily by create routes)."""
    import api.routes.tenants as tenants_module

    tenants_module._svc = _FakeTenantService(known_ids)


def _app():
    from core.errors import install_error_handlers

    app = FastAPI()
    install_error_handlers(app)
    return app


def _build_agents_client(mock_svc, ctx):
    import api.routes.agents as agents_module

    agents_module._svc = mock_svc
    _seed_resolver(ctx)

    app = _app()
    app.include_router(agents_module.router, prefix="/api/v1")
    return TestClient(app), agents_module


def _build_mcp_client(mock_svc, ctx):
    import api.routes.mcp_servers as mcp_servers_module

    mcp_servers_module._svc = mock_svc
    _seed_resolver(ctx)

    app = _app()
    app.include_router(mcp_servers_module.router, prefix="/api/v1")
    return TestClient(app), mcp_servers_module


def _claims_for(role: str):
    role_app = {
        "viewer": "Platform.Viewer",
        "operator": "Platform.Operator",
        "admin": "Platform.Admin",
    }[role]
    return {
        "oid": f"{role}-oid",
        "preferred_username": f"{role}.user@example.com",
        "roles": [role_app],
    }


def _headers():
    return {"Authorization": "Bearer fake-token"}


def test_agents_list_non_admin_sees_only_own_tenant(entra_settings):
    mock_svc = MagicMock()
    mock_svc.list.return_value = [
        _make_agent(id="own", tenant_id="ten-1"),
        _make_agent(id="foreign", tenant_id="ten-2"),
        _make_agent(id="untagged", tenant_id=None),
    ]
    client, _ = _build_agents_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents", headers=_headers())

    assert resp.status_code == 200
    ids = [a["id"] for a in resp.json()]
    assert ids == ["own"]


def test_agents_list_admin_sees_all(entra_settings):
    mock_svc = MagicMock()
    mock_svc.list.return_value = [
        _make_agent(id="a1", tenant_id="ten-1"),
        _make_agent(id="a2", tenant_id="ten-2"),
        _make_agent(id="a3", tenant_id=None),
    ]
    client, _ = _build_agents_client(mock_svc, _context(is_global=True))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/agents", headers=_headers())

    assert resp.status_code == 200
    ids = {a["id"] for a in resp.json()}
    assert ids == {"a1", "a2", "a3"}


def test_agent_detail_foreign_tenant_404_matches_truly_missing_body(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.side_effect = lambda agent_id: (
        _make_agent(id="foreign", tenant_id="ten-2") if agent_id == "foreign" else None
    )
    client, _ = _build_agents_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        foreign_resp = client.get("/api/v1/agents/foreign", headers=_headers())
        missing_resp = client.get("/api/v1/agents/truly-missing", headers=_headers())

    assert foreign_resp.status_code == 404
    assert missing_resp.status_code == 404
    assert foreign_resp.json() == missing_resp.json()


def test_agent_detail_own_tenant_visible(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(id="own", tenant_id="ten-1")
    client, _ = _build_agents_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/own", headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["id"] == "own"


def test_agent_delete_foreign_tenant_404_before_service_delete_called(entra_settings):
    """The visibility gate runs BEFORE any side effect: svc.delete is never called."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(id="foreign", tenant_id="ten-2")
    client, _ = _build_agents_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete("/api/v1/agents/foreign", headers=_headers())

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Agent not found"
    mock_svc.delete.assert_not_called()


def test_agent_invoke_foreign_tenant_404_before_any_obo_call(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(
        id="foreign",
        tenant_id="ten-2",
        identity_status="provisioned",
        platform="aws_bedrock",
        auth_type="entra",
        agent_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/agent-abc",
    )
    client, _agents_module = _build_agents_client(mock_svc, _context(tenant_ids=["ten-1"]))

    mock_graph = MagicMock()
    with patch("api.routes.grants.get_graph_service", return_value=mock_graph):
        with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
            resp = client.post(
                "/api/v1/agents/foreign/invoke",
                json={"prompt": "hi"},
                headers=_headers(),
            )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Agent not found"
    mock_graph.obo_exchange.assert_not_called()


def test_agent_reprovision_foreign_tenant_404(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(id="foreign", tenant_id="ten-2")
    client, _ = _build_agents_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/agents/foreign/reprovision", headers=_headers())

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Agent not found"
    mock_svc.persist_identity.assert_not_called()


def test_agents_tenant_not_found_is_404(entra_settings):
    """Create into an unknown tenant renders ``TenantError``'s ``not_found`` row, the tenant
    domain's wire; before the error layer this route answered 400 "unknown tenant"."""
    mock_svc = MagicMock()
    client, _ = _build_agents_client(mock_svc, _context(tenant_ids=["ten-1"]))
    _seed_tenant_service(known_ids={"ten-1"})

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents",
            json={"name": "new-agent", "tenant_id": "ten-unknown"},
            headers=_headers(),
        )

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Tenant not found", "code": "tenant.not_found"}
    mock_svc.create.assert_not_called()


def test_agent_create_foreign_tenant_403(entra_settings):
    mock_svc = MagicMock()
    client, _ = _build_agents_client(mock_svc, _context(tenant_ids=["ten-1"]))
    _seed_tenant_service(known_ids={"ten-1", "ten-2"})

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents",
            json={"name": "new-agent", "tenant_id": "ten-2"},
            headers=_headers(),
        )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "tenant not permitted"
    mock_svc.create.assert_not_called()


def test_agent_create_admin_bypasses_tenant_membership(entra_settings):
    mock_svc = MagicMock()
    mock_svc.create.return_value = _make_agent(id="new", tenant_id="ten-2")
    client, _ = _build_agents_client(mock_svc, _context(is_global=True))
    _seed_tenant_service(known_ids={"ten-2"})

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/agents",
            json={"name": "new-agent", "tenant_id": "ten-2"},
            headers=_headers(),
        )

    assert resp.status_code == 201
    mock_svc.create.assert_called_once()


def test_agent_create_own_tenant_ok(entra_settings):
    mock_svc = MagicMock()
    mock_svc.create.return_value = _make_agent(id="new", tenant_id="ten-1")
    client, _ = _build_agents_client(mock_svc, _context(tenant_ids=["ten-1"]))
    _seed_tenant_service(known_ids={"ten-1"})

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents",
            json={"name": "new-agent", "tenant_id": "ten-1"},
            headers=_headers(),
        )

    assert resp.status_code == 201
    mock_svc.create.assert_called_once()


def test_agent_publish_flips_flag(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(id="own", tenant_id="ten-1", published=False)
    mock_svc.set_published.return_value = _make_agent(id="own", tenant_id="ten-1", published=True)
    client, _ = _build_agents_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.put("/api/v1/agents/own/publish", json={"published": True}, headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["published"] is True
    mock_svc.set_published.assert_called_once_with("own", True)
    mock_svc.update.assert_not_called()


def test_agent_publish_runs_the_platform_write_off_the_event_loop(entra_settings):
    """``set_published`` polls the registry while it re-asserts APPROVED,
    so the route off-loads it (``anyio.to_thread``) — the single uvicorn worker's loop
    (and its /ping health check) never blocks on it."""
    import asyncio

    seen = {}

    def _probe(*_a):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return _make_agent(id="own", tenant_id="ten-1", published=True)

    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(id="own", tenant_id="ten-1", published=False)
    mock_svc.set_published.side_effect = _probe
    client, _ = _build_agents_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.put("/api/v1/agents/own/publish", json={"published": True}, headers=_headers())

    assert resp.status_code == 200
    assert seen["on_loop"] is False


def test_agent_publish_foreign_tenant_404(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(id="foreign", tenant_id="ten-2")
    client, _ = _build_agents_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.put("/api/v1/agents/foreign/publish", json={"published": True}, headers=_headers())

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Agent not found"
    mock_svc.update.assert_not_called()
    mock_svc.set_published.assert_not_called()


def test_agent_publish_viewer_forbidden(entra_settings):
    mock_svc = MagicMock()
    client, _ = _build_agents_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.put("/api/v1/agents/own/publish", json={"published": True}, headers=_headers())

    assert resp.status_code == 403
    mock_svc.get.assert_not_called()


def test_mcp_list_shared_visible_to_everyone(entra_settings):
    mock_svc = MagicMock()
    mock_svc.list.return_value = [
        _make_mcp(id="own", tenant_id="ten-1", shared=False),
        _make_mcp(id="shared-foreign", tenant_id="ten-2", shared=True),
        _make_mcp(id="foreign", tenant_id="ten-2", shared=False),
    ]
    client, _ = _build_mcp_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/mcp-servers", headers=_headers())

    assert resp.status_code == 200
    ids = {m["id"] for m in resp.json()}
    assert ids == {"own", "shared-foreign"}


def test_mcp_detail_foreign_non_shared_404_matches_truly_missing_body(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.side_effect = lambda mcp_id: (
        _make_mcp(id="foreign", tenant_id="ten-2", shared=False) if mcp_id == "foreign" else None
    )
    client, _ = _build_mcp_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        foreign_resp = client.get("/api/v1/mcp-servers/foreign", headers=_headers())
        missing_resp = client.get("/api/v1/mcp-servers/truly-missing", headers=_headers())

    assert foreign_resp.status_code == 404
    assert missing_resp.status_code == 404
    assert foreign_resp.json() == missing_resp.json()


def test_mcp_detail_foreign_shared_visible(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp(id="shared", tenant_id="ten-2", shared=True)
    client, _ = _build_mcp_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/mcp-servers/shared", headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["id"] == "shared"


def test_mcp_reprovision_foreign_tenant_404_before_persist(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp(id="foreign", tenant_id="ten-2", shared=False)
    client, _ = _build_mcp_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/mcp-servers/foreign/reprovision", headers=_headers())

    assert resp.status_code == 404
    assert resp.json()["detail"] == "MCP server not found"
    mock_svc.persist_identity.assert_not_called()


def test_mcp_refresh_tools_foreign_tenant_404(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp(id="foreign", tenant_id="ten-2", shared=False)
    client, _ = _build_mcp_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/mcp-servers/foreign/refresh-tools", headers=_headers())

    assert resp.status_code == 404
    assert resp.json()["detail"] == "MCP server not found"


def test_mcp_delete_foreign_tenant_404_before_service_delete(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp(id="foreign", tenant_id="ten-2", shared=False)
    client, _ = _build_mcp_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete("/api/v1/mcp-servers/foreign", headers=_headers())

    assert resp.status_code == 404
    assert resp.json()["detail"] == "MCP server not found"
    mock_svc.delete.assert_not_called()


def test_mcp_update_foreign_shared_404_before_service_update(entra_settings):
    """A foreign SHARED MCP is readable (see detail test above) but NOT mutable:
    update -> 404 byte-identical to missing, svc.update never called."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp(id="shared", tenant_id="ten-2", shared=True)
    client, _ = _build_mcp_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.put("/api/v1/mcp-servers/shared", json={"description": "tamper"}, headers=_headers())

    assert resp.status_code == 404
    assert resp.json()["detail"] == "MCP server not found"
    mock_svc.update.assert_not_called()


def test_mcp_delete_foreign_shared_404_before_service_delete(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp(id="shared", tenant_id="ten-2", shared=True)
    client, _ = _build_mcp_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete("/api/v1/mcp-servers/shared", headers=_headers())

    assert resp.status_code == 404
    assert resp.json()["detail"] == "MCP server not found"
    mock_svc.delete.assert_not_called()


def test_mcp_publish_foreign_shared_404_before_service_update(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp(id="shared", tenant_id="ten-2", shared=True)
    client, _ = _build_mcp_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.put("/api/v1/mcp-servers/shared/publish", json={"published": True}, headers=_headers())

    assert resp.status_code == 404
    assert resp.json()["detail"] == "MCP server not found"
    mock_svc.update.assert_not_called()


def test_mcp_submit_foreign_shared_404_before_lifecycle_call(entra_settings):
    """Lifecycle route (submit) also drops the shared bypass on write."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp(id="shared", tenant_id="ten-2", shared=True)
    client, _ = _build_mcp_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/mcp-servers/shared/submit", headers=_headers())

    assert resp.status_code == 404
    assert resp.json()["detail"] == "MCP server not found"
    mock_svc.submit_for_approval.assert_not_called()


def test_mcp_update_own_tenant_shared_mcp_non_shared_field_ok(entra_settings):
    """The write gate still honors OWN-tenant membership: an operator of the
    OWNING tenant may update (a non-`shared` field of) their own shared MCP."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp(id="own-shared", tenant_id="ten-1", shared=True)
    mock_svc.update.return_value = _make_mcp(
        id="own-shared", tenant_id="ten-1", shared=True, description="updated"
    )
    client, _ = _build_mcp_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.put(
            "/api/v1/mcp-servers/own-shared", json={"description": "updated"}, headers=_headers()
        )

    assert resp.status_code == 200
    mock_svc.update.assert_called_once()


def test_mcp_delete_foreign_shared_admin_ok(entra_settings):
    """Global admin retains write access to any shared MCP (is_global bypass)."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp(id="shared", tenant_id="ten-2", shared=True)
    mock_svc.delete.return_value = _make_mcp(id="shared", tenant_id="ten-2", shared=True)
    client, _ = _build_mcp_client(mock_svc, _context(is_global=True))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.delete("/api/v1/mcp-servers/shared", headers=_headers())

    assert resp.status_code == 200
    mock_svc.delete.assert_called_once()


def test_mcp_servers_tenant_not_found_is_404(entra_settings):
    """The MCP registry's create answers the same tenant-domain row as the agents' create."""
    mock_svc = MagicMock()
    client, _ = _build_mcp_client(mock_svc, _context(tenant_ids=["ten-1"]))
    _seed_tenant_service(known_ids={"ten-1"})

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers",
            json={"name": "new-mcp", "tenant_id": "ten-unknown"},
            headers=_headers(),
        )

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Tenant not found", "code": "tenant.not_found"}
    mock_svc.create.assert_not_called()


def test_mcp_create_foreign_tenant_403(entra_settings):
    mock_svc = MagicMock()
    client, _ = _build_mcp_client(mock_svc, _context(tenant_ids=["ten-1"]))
    _seed_tenant_service(known_ids={"ten-1", "ten-2"})

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers",
            json={"name": "new-mcp", "tenant_id": "ten-2"},
            headers=_headers(),
        )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "tenant not permitted"
    mock_svc.create.assert_not_called()


def test_mcp_create_shared_non_admin_403(entra_settings):
    mock_svc = MagicMock()
    client, _ = _build_mcp_client(mock_svc, _context(tenant_ids=["ten-1"]))
    _seed_tenant_service(known_ids={"ten-1"})

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers",
            json={"name": "new-mcp", "tenant_id": "ten-1", "shared": True},
            headers=_headers(),
        )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "only ADMIN may set shared"
    mock_svc.create.assert_not_called()


def test_mcp_create_shared_admin_ok(entra_settings):
    mock_svc = MagicMock()
    mock_svc.create.return_value = _make_mcp(id="new", tenant_id="ten-1", shared=True)
    client, _ = _build_mcp_client(mock_svc, _context(is_global=True))
    _seed_tenant_service(known_ids={"ten-1"})

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/mcp-servers",
            json={"name": "new-mcp", "tenant_id": "ten-1", "shared": True},
            headers=_headers(),
        )

    assert resp.status_code == 201
    mock_svc.create.assert_called_once()


def test_mcp_update_shared_non_admin_403(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp(id="own", tenant_id="ten-1", shared=False)
    client, _ = _build_mcp_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.put("/api/v1/mcp-servers/own", json={"shared": True}, headers=_headers())

    assert resp.status_code == 403
    assert resp.json()["detail"] == "only ADMIN may set shared"
    mock_svc.update.assert_not_called()


def test_mcp_update_shared_admin_ok(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp(id="own", tenant_id="ten-1", shared=False)
    mock_svc.update.return_value = _make_mcp(id="own", tenant_id="ten-1", shared=True)
    client, _ = _build_mcp_client(mock_svc, _context(is_global=True))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.put("/api/v1/mcp-servers/own", json={"shared": True}, headers=_headers())

    assert resp.status_code == 200
    mock_svc.update.assert_called_once()


def test_mcp_update_unshare_non_admin_403(entra_settings):
    """Un-SETTING shared (`{"shared": false}`) is as privileged
    as setting it — a same-tenant OPERATOR gets 403, the update never runs."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp(id="own", tenant_id="ten-1", shared=True)
    client, _ = _build_mcp_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.put("/api/v1/mcp-servers/own", json={"shared": False}, headers=_headers())

    assert resp.status_code == 403
    assert resp.json()["detail"] == "only ADMIN may set shared"
    mock_svc.update.assert_not_called()


def test_mcp_update_unshare_admin_ok(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp(id="own", tenant_id="ten-1", shared=True)
    mock_svc.update.return_value = _make_mcp(id="own", tenant_id="ten-1", shared=False)
    client, _ = _build_mcp_client(mock_svc, _context(is_global=True))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.put("/api/v1/mcp-servers/own", json={"shared": False}, headers=_headers())

    assert resp.status_code == 200
    mock_svc.update.assert_called_once()


def test_mcp_update_non_shared_field_non_admin_ok(entra_settings):
    """A non-admin OPERATOR may still update OTHER fields — only an explicit
    `shared` (any value) is rejected."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp(id="own", tenant_id="ten-1", shared=False)
    mock_svc.update.return_value = _make_mcp(id="own", tenant_id="ten-1", shared=False, description="updated")
    client, _ = _build_mcp_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.put("/api/v1/mcp-servers/own", json={"description": "updated"}, headers=_headers())

    assert resp.status_code == 200
    mock_svc.update.assert_called_once()


def test_mcp_publish_flips_flag(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp(id="own", tenant_id="ten-1", published=False)
    mock_svc.set_published.return_value = _make_mcp(id="own", tenant_id="ten-1", published=True)
    client, _ = _build_mcp_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.put("/api/v1/mcp-servers/own/publish", json={"published": True}, headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["published"] is True
    mock_svc.set_published.assert_called_once_with("own", True)
    mock_svc.update.assert_not_called()


def test_mcp_publish_runs_the_platform_write_off_the_event_loop(entra_settings):
    """``set_published`` polls the registry while it re-asserts APPROVED,
    so the route off-loads it (``anyio.to_thread``) — the single uvicorn worker's loop
    (and its /ping health check) never blocks on it."""
    import asyncio

    seen = {}

    def _probe(*_a):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return _make_mcp(id="own", tenant_id="ten-1", published=True)

    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp(id="own", tenant_id="ten-1", published=False)
    mock_svc.set_published.side_effect = _probe
    client, _ = _build_mcp_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.put("/api/v1/mcp-servers/own/publish", json={"published": True}, headers=_headers())

    assert resp.status_code == 200
    assert seen["on_loop"] is False


def test_mcp_publish_foreign_tenant_404(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp(id="foreign", tenant_id="ten-2", shared=False)
    client, _ = _build_mcp_client(mock_svc, _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.put("/api/v1/mcp-servers/foreign/publish", json={"published": True}, headers=_headers())

    assert resp.status_code == 404
    assert resp.json()["detail"] == "MCP server not found"
    mock_svc.update.assert_not_called()
