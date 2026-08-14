"""Governance-graph route tests (Epic 11, Task 5).

Exercise the REAL `require_role` + `current_principal` dependency path
(`AUTH_PROVIDER=entra`) against a mocked `verify_entra_token` (no live Entra) and a
patched `_build_service` (no live AWS / Graph / HTTP). Mirrors the RBAC-test idiom
of `test_grants_routes.py`: reset cached modules, build a minimal app with ONLY the
governance-graph router, patch the lazy service factory so no real registry / Graph
is touched.

Covers (plan Task 5):
  - `GET /api/v1/governance-graph` as VIEWER → 200, body validates against
    `GovernanceGraph`.
  - missing / insufficient credential → 401 / 403 (the require_role VIEWER path).
  - `GET /api/v1/governance-graph/principals/{oid}?kind=user` → 200 PrincipalDetail.
  - service raising a 404-class GraphError → 404; other GraphError → 502; bad kind
    (service ValueError) → 400; invalid query kind → 422.
  - registration smoke: the route is mounted under the API_PREFIX app.
"""

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
        "api.routes.mcp_servers",
        "api.routes.grants",
        "api.routes.governance_graph",
        "api.routes.users",
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
    monkeypatch.setenv("ENTRA_ROLE_ADMIN", "Platform.Admin")
    monkeypatch.setenv("ENTRA_ROLE_OPERATOR", "Platform.Operator")
    monkeypatch.setenv("ENTRA_ROLE_VIEWER", "Platform.Viewer")


def _sample_graph():
    """A known GovernanceGraph the mocked service returns from .build()."""
    from models.governance_graph import GovernanceGraph, GraphEdge, GraphNode

    return GovernanceGraph(
        nodes=[
            GraphNode(
                type="agent",
                id="agent:rec-123",
                label="claims-triage-de",
                ref_id="rec-123",
                metadata={"identity_status": "provisioned"},
            ),
            GraphNode(
                type="mcp",
                id="mcp:mcp-1",
                label="internal-claims-mcp",
                ref_id="mcp-1",
                metadata={"cedar_enforcement_mode": "enforce"},
            ),
            GraphNode(
                type="user",
                id="user:user-1",
                label="Maria Bauer",
                ref_id="user-1",
                metadata={"principal_type": "User"},
            ),
        ],
        edges=[
            GraphEdge(
                id="assign-1",
                source="user:user-1",
                target="agent:rec-123",
                type="access",
                role="Invoker",
            ),
            GraphEdge(
                id="grant-1",
                source="agent:rec-123",
                target="mcp:mcp-1",
                type="can_call",
                role="Invoker",
                has_policy=True,
            ),
        ],
    )


def _sample_principal():
    from models.governance_graph import PrincipalDetail

    return PrincipalDetail(
        id="user-1",
        display_name="Maria Bauer",
        kind="user",
        user_principal_name="maria.bauer@example.onmicrosoft.com",
        mail="maria.bauer@example.com",
        job_title="Claims Officer",
        group_names=["Contoso-Claims-Officers"],
    )


def _build_client(mock_service, ctx=None):
    """Build a minimal app with ONLY the governance-graph router + a patched service.

    Must be called AFTER the entra env fixture so the route module imports with the
    right settings. Patches the module's lazy `_build_service` factory so no real
    registry / Graph is touched — every handler resolves to `mock_service`.

    E24/T8: also seeds the ONE tenant-resolver singleton
    (``api.routes.users._tenant_resolver``) with a stub returning ``ctx`` (default
    global-admin, preserving pre-E24 behavior for the existing tests) — the graph
    route now resolves a TenantContext per request (the Task 5 fixup pattern).
    """
    import api.routes.governance_graph as gg_module
    import api.routes.users as users_module
    from services.tenant_resolver import TenantContext

    gg_module._build_service = lambda: mock_service

    if ctx is None:
        ctx = TenantContext(is_global=True, tenant_ids=frozenset(), tenants=())

    class _FakeResolver:
        async def resolve(self, principal):
            return ctx

    users_module._tenant_resolver = _FakeResolver()

    app = FastAPI()
    app.include_router(gg_module.router, prefix="/api/v1")
    return TestClient(app), gg_module


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


# --- GET /governance-graph ---------------------------------------------------

def test_get_graph_viewer_ok(entra_settings):
    """VIEWER can read the graph; the body validates against GovernanceGraph."""
    from models.governance_graph import GovernanceGraph

    mock_service = MagicMock()
    mock_service.build = AsyncMock(return_value=_sample_graph())
    client, _ = _build_client(mock_service)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/governance-graph", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    # Round-trips into the model (registration + response_model wiring proven).
    GovernanceGraph(**body)
    assert {n["id"] for n in body["nodes"]} == {"agent:rec-123", "mcp:mcp-1", "user:user-1"}
    assert any(e["has_policy"] for e in body["edges"])
    mock_service.build.assert_awaited_once()


def test_get_graph_threads_tenant_ctx_into_build(entra_settings):
    """E24/T8: the route resolves the caller's TenantContext (via the ONE resolver
    singleton) and passes it to service.build(ctx=...) — the service induces the
    subgraph. Admin (is_global) contexts are threaded identically."""
    from services.tenant_resolver import TenantContext

    scoped = TenantContext(is_global=False, tenant_ids=frozenset({"ten-1"}), tenants=())
    mock_service = MagicMock()
    mock_service.build = AsyncMock(return_value=_sample_graph())
    client, _ = _build_client(mock_service, ctx=scoped)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/governance-graph", headers=_headers())

    assert resp.status_code == 200
    mock_service.build.assert_awaited_once()
    assert mock_service.build.await_args.kwargs["ctx"] is scoped


def test_get_graph_no_credential_401(entra_settings):
    """No Authorization header → 401 (the require_role VIEWER path rejects it)."""
    mock_service = MagicMock()
    mock_service.build = AsyncMock(return_value=_sample_graph())
    client, _ = _build_client(mock_service)

    resp = client.get("/api/v1/governance-graph")

    assert resp.status_code == 401
    mock_service.build.assert_not_called()


# --- GET /governance-graph/principals/{oid} ----------------------------------

def test_get_principal_viewer_ok(entra_settings):
    """VIEWER can resolve a user principal; body validates against PrincipalDetail."""
    from models.governance_graph import PrincipalDetail

    mock_service = MagicMock()
    mock_service.get_principal = AsyncMock(return_value=_sample_principal())
    client, _ = _build_client(mock_service)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get(
            "/api/v1/governance-graph/principals/user-1",
            params={"kind": "user"},
            headers=_headers(),
        )

    assert resp.status_code == 200
    body = resp.json()
    PrincipalDetail(**body)
    assert body["id"] == "user-1"
    assert body["kind"] == "user"
    assert body["group_names"] == ["Contoso-Claims-Officers"]
    mock_service.get_principal.assert_awaited_once_with("user-1", "user")


def test_get_principal_group_ok(entra_settings):
    """kind=group is also accepted."""
    from models.governance_graph import PrincipalDetail

    mock_service = MagicMock()
    mock_service.get_principal = AsyncMock(
        return_value=PrincipalDetail(
            id="grp-1", display_name="Contoso-Claims-Officers", kind="group"
        )
    )
    client, _ = _build_client(mock_service)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get(
            "/api/v1/governance-graph/principals/grp-1",
            params={"kind": "group"},
            headers=_headers(),
        )

    assert resp.status_code == 200
    assert resp.json()["kind"] == "group"
    mock_service.get_principal.assert_awaited_once_with("grp-1", "group")


def test_get_principal_graph_404_maps_404(entra_settings):
    """A 404-class GraphError from the service → HTTP 404 (not a raw 500)."""
    from services.graph_service import GraphError

    mock_service = MagicMock()
    mock_service.get_principal = AsyncMock(
        side_effect=GraphError(404, "Request_ResourceNotFound")
    )
    client, _ = _build_client(mock_service)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get(
            "/api/v1/governance-graph/principals/missing-oid",
            params={"kind": "user"},
            headers=_headers(),
        )

    assert resp.status_code == 404


def test_get_principal_other_graph_error_maps_502(entra_settings):
    """A non-404 GraphError from the service → HTTP 502 (not a raw 500)."""
    from services.graph_service import GraphError

    mock_service = MagicMock()
    mock_service.get_principal = AsyncMock(
        side_effect=GraphError(403, "Authorization_RequestDenied")
    )
    client, _ = _build_client(mock_service)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get(
            "/api/v1/governance-graph/principals/user-1",
            params={"kind": "user"},
            headers=_headers(),
        )

    assert resp.status_code == 502


def test_get_principal_service_valueerror_maps_400(entra_settings):
    """The service raising ValueError (bad kind it rejects) → HTTP 400."""
    mock_service = MagicMock()
    mock_service.get_principal = AsyncMock(
        side_effect=ValueError("unsupported principal kind: 'device'")
    )
    client, _ = _build_client(mock_service)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        # `kind` must pass query validation to reach the service; the service then
        # rejects it. We use a value the route accepts at the query layer but the
        # service would reject — emulated here by the mock raising ValueError.
        resp = client.get(
            "/api/v1/governance-graph/principals/user-1",
            params={"kind": "user"},
            headers=_headers(),
        )

    assert resp.status_code == 400


def test_get_principal_bad_query_kind_422(entra_settings):
    """An invalid `kind` query value (not user/group) → 422 at the query layer."""
    mock_service = MagicMock()
    mock_service.get_principal = AsyncMock(return_value=_sample_principal())
    client, _ = _build_client(mock_service)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get(
            "/api/v1/governance-graph/principals/user-1",
            params={"kind": "device"},
            headers=_headers(),
        )

    assert resp.status_code == 422
    mock_service.get_principal.assert_not_called()


def test_get_principal_no_credential_401(entra_settings):
    """No Authorization header on the principal route → 401."""
    mock_service = MagicMock()
    mock_service.get_principal = AsyncMock(return_value=_sample_principal())
    client, _ = _build_client(mock_service)

    resp = client.get(
        "/api/v1/governance-graph/principals/user-1", params={"kind": "user"}
    )

    assert resp.status_code == 401
    mock_service.get_principal.assert_not_called()


# --- principal-detail tenant gate (E24 follow-up, triage item 3) -------------
# A NON-global caller may only resolve principals appearing in THEIR induced graph
# (the same build(ctx=...) filtering as GET /governance-graph). A principal outside
# that subgraph 404s with the EXISTING "principal not found" literal — byte-identical
# to a genuinely missing oid. Admin (is_global) skips the gate entirely.

def _scoped_ctx(*tenant_ids):
    from services.tenant_resolver import TenantContext

    return TenantContext(
        is_global=False, tenant_ids=frozenset(tenant_ids), tenants=()
    )


def test_get_principal_foreign_to_induced_graph_404_non_global(entra_settings):
    """A principal NOT in the caller's induced graph → 404 with the existing
    "principal not found" literal; the Graph detail read never fires."""
    mock_service = MagicMock()
    mock_service.build = AsyncMock(return_value=_sample_graph())  # has user:user-1 only
    mock_service.get_principal = AsyncMock(return_value=_sample_principal())
    client, _ = _build_client(mock_service, ctx=_scoped_ctx("ten-2"))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get(
            "/api/v1/governance-graph/principals/foreign-user-9",
            params={"kind": "user"},
            headers=_headers(),
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "principal not found"
    mock_service.get_principal.assert_not_called()
    # The gate built the caller's induced graph with THEIR ctx.
    assert mock_service.build.await_args.kwargs["ctx"].is_global is False


def test_get_principal_in_induced_graph_200_non_global(entra_settings):
    """A principal that IS in the caller's induced graph resolves normally (200)."""
    mock_service = MagicMock()
    mock_service.build = AsyncMock(return_value=_sample_graph())  # contains user:user-1
    mock_service.get_principal = AsyncMock(return_value=_sample_principal())
    client, _ = _build_client(mock_service, ctx=_scoped_ctx("ten-1"))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get(
            "/api/v1/governance-graph/principals/user-1",
            params={"kind": "user"},
            headers=_headers(),
        )

    assert resp.status_code == 200
    assert resp.json()["id"] == "user-1"
    mock_service.get_principal.assert_awaited_once_with("user-1", "user")


def test_get_principal_admin_skips_gate_200(entra_settings):
    """A global (admin) caller resolves ANY principal — the gate never builds the
    graph (no per-call induced-graph cost for admins)."""
    mock_service = MagicMock()
    mock_service.build = AsyncMock(return_value=_sample_graph())
    mock_service.get_principal = AsyncMock(return_value=_sample_principal())
    client, _ = _build_client(mock_service)  # default ctx: is_global=True

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get(
            "/api/v1/governance-graph/principals/user-1",
            params={"kind": "user"},
            headers=_headers(),
        )

    assert resp.status_code == 200
    mock_service.get_principal.assert_awaited_once_with("user-1", "user")
    mock_service.build.assert_not_called()


def test_get_principal_gate_matches_kind_prefix(entra_settings):
    """The gate keys on the node id "{kind}:{oid}" — a user oid that only exists as
    a GROUP node in the induced graph must still 404 for kind=user."""
    mock_service = MagicMock()
    mock_service.build = AsyncMock(return_value=_sample_graph())  # user:user-1, no group:user-1
    mock_service.get_principal = AsyncMock(return_value=_sample_principal())
    client, _ = _build_client(mock_service, ctx=_scoped_ctx("ten-1"))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get(
            "/api/v1/governance-graph/principals/user-1",
            params={"kind": "group"},
            headers=_headers(),
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "principal not found"
    mock_service.get_principal.assert_not_called()


# --- registration smoke ------------------------------------------------------

def test_route_mounted_under_api_prefix(entra_settings):
    """Registration smoke: the collection route is reachable under /api/v1 (mounted
    in the test app exactly as main.py mounts it under API_PREFIX)."""
    mock_service = MagicMock()
    mock_service.build = AsyncMock(return_value=_sample_graph())
    client, gg_module = _build_client(mock_service)

    paths = {r.path for r in gg_module.router.routes}
    assert "/governance-graph" in paths
    assert "/governance-graph/principals/{oid}" in paths

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/governance-graph", headers=_headers())
    assert resp.status_code == 200
