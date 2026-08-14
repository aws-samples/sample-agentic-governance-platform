"""Route tests for the E26 observability read surface (contract C4, Task 6).

Covers the tenant-scoped, VIEWER-GET observability routes:
  - ``GET /observability/settings`` (config echo — configured = bool(LANGFUSE_HOST)),
  - ``GET /observability/metrics?scope=…`` (scope→agent-set + totals + by_agent[]),
  - ``GET /agents/{id}/metrics`` and ``GET /agents/{id}/traces`` (per-agent, on the
    agents router — visibility-gated via the SHARED ``_load_visible_agent``).

Mirrors ``test_registry_tenant_scoping.py``'s harness: a minimal FastAPI app with ONLY
the router under test, the REAL ``require_role`` + ``current_principal`` path against a
mocked ``verify_entra_token``, a mocked ``AgentRegistryService`` (no AWS), a seeded
tenant-resolver stub, and a MOCKED T5 metrics service (no live Langfuse). The 404 for a
foreign/unknown agent must be BYTE-IDENTICAL to the truly-missing 404.
"""

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def reset_modules():
    """Drop cached auth/config/route modules so monkeypatched env is honored."""
    import sys

    for mod in [
        "core.rbac",
        "core.security_entra",
        "core.config",
        "api.routes.agents",
        "api.routes.observability",
        "api.routes.users",
    ]:
        sys.modules.pop(mod, None)
    yield


@pytest.fixture
def entra_settings(monkeypatch):
    monkeypatch.setenv("AUTH_PROVIDER", "entra")
    monkeypatch.setenv("USE_DEV_AUTH", "False")
    monkeypatch.setenv("DEBUG", "False")
    monkeypatch.setenv("ENTRA_TENANT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("ENTRA_AUDIENCE", "api://agp")
    monkeypatch.setenv("ENTRA_ROLE_ADMIN", "Platform.Admin")
    monkeypatch.setenv("ENTRA_ROLE_OPERATOR", "Platform.Operator")
    monkeypatch.setenv("ENTRA_ROLE_VIEWER", "Platform.Viewer")


# --- fixtures ------------------------------------------------------------------


def _make_agent(**overrides):
    from models.agent import Agent, LifecycleState, Origin

    now = datetime.now(timezone.utc)
    base = dict(
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


def _metrics(traces=0, cost=0.0, tokens=0):
    """Build a real AgentMetrics (the T5 DTO) for the mocked service to return."""
    from services.langfuse_metrics_service import AgentMetrics, MetricTotals

    return AgentMetrics(
        totals=MetricTotals(traces=traces, cost_usd=cost, tokens=tokens),
        daily=[],
        by_model=[],
    )


def _context(*, is_global=False, tenant_ids=()):
    from services.tenant_resolver import TenantContext

    return TenantContext(is_global=is_global, tenant_ids=frozenset(tenant_ids), tenants=())


def _project_detail(*, tenant_id, agent_ids):
    """Build a real ProjectDetail (project + repositories) for a mocked ProjectService.

    Project→agent membership lives on the Repository record (project_id → agent_id), so
    the observability project scope reads the project's repositories. The project carries
    the tenant that the ``_project_agent_ids`` visibility gate keys on."""
    from models.project import Project, ProjectDetail
    from models.repository import Repository

    now = "2026-07-16T00:00:00Z"
    project = Project(
        id="proj-1",
        name="proj",
        connection_id="conn-1",
        tenant_id=tenant_id,
        created_by="maria.bauer@example.com",
        created_at=now,
        updated_at=now,
    )
    repos = [
        Repository(
            id=f"repo-{aid}",
            project_id="proj-1",
            name=f"repo-{aid}",
            agent_id=aid,
            template_name="tmpl",
            status="provisioning",
            created_by="maria.bauer@example.com",
            created_at=now,
            updated_at=now,
        )
        for aid in agent_ids
    ]
    return ProjectDetail(project=project, repositories=repos)


class _FakeResolver:
    def __init__(self, ctx):
        self._ctx = ctx

    async def resolve(self, principal):
        return self._ctx


def _seed_resolver(ctx):
    import api.routes.users as users_module

    users_module._tenant_resolver = _FakeResolver(ctx)


def _build_observability_client(mock_svc, ctx, mock_metrics=None):
    """Minimal app with ONLY the observability router + mocked registry/metrics/resolver."""
    import api.routes.agents as agents_module
    import api.routes.observability as obs_module

    agents_module._svc = mock_svc
    if mock_metrics is not None:
        obs_module._metrics_svc = mock_metrics
    _seed_resolver(ctx)

    app = FastAPI()
    app.include_router(obs_module.router, prefix="/api/v1")
    return TestClient(app), obs_module


def _build_agents_client(mock_svc, ctx, mock_metrics=None):
    """Minimal app with ONLY the agents router (for the per-agent metrics/traces routes)."""
    import api.routes.agents as agents_module
    import api.routes.observability as obs_module

    agents_module._svc = mock_svc
    if mock_metrics is not None:
        obs_module._metrics_svc = mock_metrics
    _seed_resolver(ctx)

    app = FastAPI()
    app.include_router(agents_module.router, prefix="/api/v1")
    return TestClient(app), agents_module


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


# ===========================================================================
# 1. /observability/settings — configured echo
# ===========================================================================

def test_settings_reports_configured(entra_settings, monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "https://langfuse.example.com")
    client, _ = _build_observability_client(MagicMock(), _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/observability/settings", headers=_headers())

    assert resp.status_code == 200
    assert resp.json() == {
        "langfuse_host": "https://langfuse.example.com",
        "configured": True,
    }


def test_settings_reports_unconfigured(entra_settings, monkeypatch):
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    client, _ = _build_observability_client(MagicMock(), _context(tenant_ids=["ten-1"]))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/observability/settings", headers=_headers())

    assert resp.status_code == 200
    assert resp.json() == {"langfuse_host": None, "configured": False}


# ===========================================================================
# 2. Per-agent metrics — foreign tenant 404 byte-identical to truly-missing
# ===========================================================================

def test_agent_metrics_foreign_tenant_404(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.side_effect = lambda agent_id: (
        _make_agent(id="foreign", tenant_id="ten-2") if agent_id == "foreign" else None
    )
    mock_metrics = MagicMock()
    mock_metrics.get_agent_metrics = AsyncMock(return_value=_metrics())
    client, _ = _build_agents_client(
        mock_svc, _context(tenant_ids=["ten-1"]), mock_metrics
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        foreign_resp = client.get("/api/v1/agents/foreign/metrics", headers=_headers())
        missing_resp = client.get("/api/v1/agents/truly-missing/metrics", headers=_headers())

    assert foreign_resp.status_code == 404
    assert missing_resp.status_code == 404
    # BYTE-IDENTICAL body: a foreign tenant's agent must look absent (no leak).
    assert foreign_resp.json() == missing_resp.json()
    assert foreign_resp.json()["detail"] == "Agent not found"
    # The visibility gate ran BEFORE the metrics read.
    mock_metrics.get_agent_metrics.assert_not_called()


# ===========================================================================
# 3. Per-agent metrics — visible agent returns AgentMetrics
# ===========================================================================

def test_agent_metrics_ok(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(id="own", tenant_id="ten-1")
    mock_metrics = MagicMock()
    mock_metrics.get_agent_metrics = AsyncMock(
        return_value=_metrics(traces=7, cost=1.25, tokens=900)
    )
    client, _ = _build_agents_client(
        mock_svc, _context(tenant_ids=["ten-1"]), mock_metrics
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get(
            "/api/v1/agents/own/metrics?date_from=2026-07-09&date_to=2026-07-16",
            headers=_headers(),
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["totals"] == {"traces": 7, "cost_usd": 1.25, "tokens": 900}
    assert body["daily"] == [] and body["by_model"] == []
    # The route passed the resolved agent + parsed dates to the T5 service.
    called_agent, d_from, d_to = mock_metrics.get_agent_metrics.call_args[0]
    assert called_agent.id == "own"
    assert d_from == date(2026, 7, 9) and d_to == date(2026, 7, 16)


def test_agent_traces_ok(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(id="own", tenant_id="ten-1")
    mock_metrics = MagicMock()
    mock_metrics.get_agent_traces = AsyncMock(
        return_value={"data": [{"id": "t1", "cost_usd": 0.0}], "total": 1}
    )
    client, _ = _build_agents_client(
        mock_svc, _context(tenant_ids=["ten-1"]), mock_metrics
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get(
            "/api/v1/agents/own/traces?page=2&limit=10", headers=_headers()
        )

    assert resp.status_code == 200
    assert resp.json() == {"data": [{"id": "t1", "cost_usd": 0.0}], "total": 1}
    _, kwargs = mock_metrics.get_agent_traces.call_args
    called_agent = mock_metrics.get_agent_traces.call_args[0][0]
    assert called_agent.id == "own"
    assert kwargs.get("page") == 2 and kwargs.get("limit") == 10


# ===========================================================================
# 4. Scope metrics — scope=tenant lists totals + by_agent[] for visible agents
# ===========================================================================

def test_scope_metrics_tenant_lists_by_agent(entra_settings):
    mock_svc = MagicMock()
    mock_svc.list.return_value = [
        _make_agent(id="own", name="own-agent", tenant_id="ten-1"),
        _make_agent(id="foreign", name="foreign-agent", tenant_id="ten-2"),
    ]
    mock_metrics = MagicMock()
    mock_metrics.get_scope_metrics = AsyncMock(
        return_value=_metrics(traces=5, cost=2.0, tokens=500)
    )
    mock_metrics.get_agent_metrics = AsyncMock(
        return_value=_metrics(traces=5, cost=2.0, tokens=500)
    )
    client, _ = _build_observability_client(
        mock_svc, _context(tenant_ids=["ten-1"]), mock_metrics
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get(
            "/api/v1/observability/metrics?scope=tenant", headers=_headers()
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["totals"] == {"traces": 5, "cost_usd": 2.0, "tokens": 500}
    # by_agent lists ONLY the caller's visible agent (foreign tenant filtered out).
    assert len(body["by_agent"]) == 1
    entry = body["by_agent"][0]
    assert entry["agent_id"] == "own"
    assert entry["agent_name"] == "own-agent"
    assert entry["tenant_id"] == "ten-1"
    assert entry["totals"] == {"traces": 5, "cost_usd": 2.0, "tokens": 500}
    # The fan-out summed ONLY the visible agent set (never the foreign agent).
    scope_agents = mock_metrics.get_scope_metrics.call_args[0][0]
    assert [a.id for a in scope_agents] == ["own"]


# ===========================================================================
# 4b. Scope=project — visibility-gated project read + foreign/missing collapse
# ===========================================================================

def test_scope_project_filters_foreign(entra_settings):
    """scope=project with a project in the caller's tenant returns ONLY the caller's
    visible agents in that project; a project in ANOTHER tenant (or nonexistent) yields
    zeroed metrics / empty by_agent — indistinguishable from a missing project (no
    existence oracle, no foreign-agent leak)."""
    mock_svc = MagicMock()
    mock_svc.list.return_value = [
        _make_agent(id="own", name="own-agent", tenant_id="ten-1"),
        _make_agent(id="foreign", name="foreign-agent", tenant_id="ten-2"),
    ]
    mock_metrics = MagicMock()
    mock_metrics.get_scope_metrics = AsyncMock(
        return_value=_metrics(traces=3, cost=1.0, tokens=300)
    )
    mock_metrics.get_agent_metrics = AsyncMock(
        return_value=_metrics(traces=3, cost=1.0, tokens=300)
    )
    client, _ = _build_observability_client(
        mock_svc, _context(tenant_ids=["ten-1"]), mock_metrics
    )

    import api.routes.projects as projects_module

    # (a) A project in the caller's OWN tenant that materialized both agents — the
    #     trailing visibility gate still drops the foreign-tenant agent.
    own_project = _project_detail(tenant_id="ten-1", agent_ids=["own", "foreign"])
    mock_ps = MagicMock()
    mock_ps.get_project.return_value = own_project

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        with patch.object(projects_module, "get_project_service", return_value=mock_ps):
            resp_own = client.get(
                "/api/v1/observability/metrics?scope=project&project_id=proj-1",
                headers=_headers(),
            )

    assert resp_own.status_code == 200
    body_own = resp_own.json()
    # by_agent lists ONLY the caller's visible agent, even though the project record
    # references a foreign-tenant agent id.
    assert [e["agent_id"] for e in body_own["by_agent"]] == ["own"]
    own_scope_agents = mock_metrics.get_scope_metrics.call_args[0][0]
    assert [a.id for a in own_scope_agents] == ["own"]

    # (b) A project belonging to ANOTHER tenant — the project read is visibility-gated,
    #     so it collapses to the empty set: zeroed merged metrics + empty by_agent, with
    #     NO agent leaked (byte-identical to a nonexistent project).
    mock_metrics.get_scope_metrics = AsyncMock(return_value=_metrics())
    foreign_project = _project_detail(tenant_id="ten-2", agent_ids=["own", "foreign"])
    mock_ps.get_project.return_value = foreign_project

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        with patch.object(projects_module, "get_project_service", return_value=mock_ps):
            resp_foreign = client.get(
                "/api/v1/observability/metrics?scope=project&project_id=proj-1",
                headers=_headers(),
            )

    assert resp_foreign.status_code == 200
    body_foreign = resp_foreign.json()
    assert body_foreign["by_agent"] == []
    assert body_foreign["totals"] == {"traces": 0, "cost_usd": 0.0, "tokens": 0}
    foreign_scope_agents = mock_metrics.get_scope_metrics.call_args[0][0]
    assert foreign_scope_agents == []

    # (c) A nonexistent project id yields the SAME empty shape as the foreign project —
    #     no distinguishable error revealing whether the project exists.
    mock_metrics.get_scope_metrics = AsyncMock(return_value=_metrics())
    mock_ps.get_project.return_value = None

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        with patch.object(projects_module, "get_project_service", return_value=mock_ps):
            resp_missing = client.get(
                "/api/v1/observability/metrics?scope=project&project_id=nope",
                headers=_headers(),
            )

    assert resp_missing.status_code == 200
    assert resp_missing.json() == body_foreign  # foreign == missing (no oracle)


def test_scope_project_missing_project_id_400(entra_settings):
    """scope=project with NO project_id → 400 (locks the current guard)."""
    mock_svc = MagicMock()
    mock_svc.list.return_value = [_make_agent(id="own", tenant_id="ten-1")]
    mock_metrics = MagicMock()
    mock_metrics.get_scope_metrics = AsyncMock(return_value=_metrics())
    mock_metrics.get_agent_metrics = AsyncMock(return_value=_metrics())
    client, _ = _build_observability_client(
        mock_svc, _context(tenant_ids=["ten-1"]), mock_metrics
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get(
            "/api/v1/observability/metrics?scope=project", headers=_headers()
        )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "project_id is required for scope=project"


# ===========================================================================
# 4c. Scope=platform — non-admin restricted to own tenant; admin sees all
# ===========================================================================

def test_scope_platform_non_admin_restricted_to_tenant(entra_settings):
    """A NON-admin caller with scope=platform sees ONLY their tenant's agents in totals
    + by_agent (NOT all agents across tenants); a global-admin sees all."""
    agents = [
        _make_agent(id="own", name="own-agent", tenant_id="ten-1"),
        _make_agent(id="foreign", name="foreign-agent", tenant_id="ten-2"),
    ]
    mock_svc = MagicMock()
    mock_svc.list.return_value = agents
    mock_metrics = MagicMock()
    mock_metrics.get_scope_metrics = AsyncMock(
        return_value=_metrics(traces=9, cost=4.0, tokens=900)
    )
    mock_metrics.get_agent_metrics = AsyncMock(
        return_value=_metrics(traces=9, cost=4.0, tokens=900)
    )

    # Non-admin scoped to ten-1 — scope=platform must NOT leak the ten-2 agent.
    client, _ = _build_observability_client(
        mock_svc, _context(tenant_ids=["ten-1"]), mock_metrics
    )
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get(
            "/api/v1/observability/metrics?scope=platform", headers=_headers()
        )

    assert resp.status_code == 200
    body = resp.json()
    assert [e["agent_id"] for e in body["by_agent"]] == ["own"]
    scope_agents = mock_metrics.get_scope_metrics.call_args[0][0]
    assert [a.id for a in scope_agents] == ["own"]

    # Global admin — scope=platform sees EVERY tenant's agents.
    admin_client, _ = _build_observability_client(
        mock_svc, _context(is_global=True), mock_metrics
    )
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        admin_resp = admin_client.get(
            "/api/v1/observability/metrics?scope=platform", headers=_headers()
        )

    assert admin_resp.status_code == 200
    admin_body = admin_resp.json()
    assert sorted(e["agent_id"] for e in admin_body["by_agent"]) == ["foreign", "own"]
    admin_scope_agents = mock_metrics.get_scope_metrics.call_args[0][0]
    assert sorted(a.id for a in admin_scope_agents) == ["foreign", "own"]


# ===========================================================================
# 5. VIEWER is allowed on these GETs (not OPERATOR-gated)
# ===========================================================================

def test_viewer_can_get(entra_settings, monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "https://langfuse.example.com")
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(id="own", tenant_id="ten-1")
    mock_svc.list.return_value = [_make_agent(id="own", tenant_id="ten-1")]
    mock_metrics = MagicMock()
    mock_metrics.get_agent_metrics = AsyncMock(return_value=_metrics(traces=1))
    mock_metrics.get_scope_metrics = AsyncMock(return_value=_metrics(traces=1))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        obs_client, _ = _build_observability_client(
            mock_svc, _context(tenant_ids=["ten-1"]), mock_metrics
        )
        settings_resp = obs_client.get(
            "/api/v1/observability/settings", headers=_headers()
        )
        scope_resp = obs_client.get(
            "/api/v1/observability/metrics?scope=tenant", headers=_headers()
        )

        agents_client, _ = _build_agents_client(
            mock_svc, _context(tenant_ids=["ten-1"]), mock_metrics
        )
        agent_metrics_resp = agents_client.get(
            "/api/v1/agents/own/metrics", headers=_headers()
        )

    assert settings_resp.status_code == 200
    assert scope_resp.status_code == 200
    assert agent_metrics_resp.status_code == 200
