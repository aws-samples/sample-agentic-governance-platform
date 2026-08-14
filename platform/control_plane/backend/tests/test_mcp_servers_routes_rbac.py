"""RBAC + behavior tests for the MCP Server Registry routes (Epic 5, Task 4).

Structural clone of `test_agents_routes_rbac.py`. These exercise the REAL
`require_role` + `current_principal` dependency path (`AUTH_PROVIDER=entra`)
against a mocked `verify_entra_token` (so no live Entra) and a mocked
`McpServerRegistryService` (so no live AWS / registry).

A minimal FastAPI app including ONLY the mcp_servers router is built per test
(after the entra env is set + the auth/config modules are reset) to avoid
importing the whole app's import chain.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

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
        "api.routes.mcp_servers",
    ]:
        sys.modules.pop(mod, None)
    yield


@pytest.fixture
def entra_settings(monkeypatch):
    monkeypatch.setenv("AUTH_PROVIDER", "entra")
    monkeypatch.setenv("USE_DEV_AUTH", "False")
    monkeypatch.setenv("ENTRA_TENANT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("ENTRA_AUDIENCE", "api://agp")
    monkeypatch.setenv("ENTRA_ROLE_ADMIN", "Platform.Admin")
    monkeypatch.setenv("ENTRA_ROLE_OPERATOR", "Platform.Operator")
    monkeypatch.setenv("ENTRA_ROLE_VIEWER", "Platform.Viewer")


def _make_mcp_server(**overrides):
    """Build a real McpServer model instance for mock return values."""
    from models.mcp_server import Kind, LifecycleState, McpServer

    now = datetime.now(timezone.utc)
    base = dict(
        id="rec-123",
        name="claims-mcp-de",
        description="Claims MCP server",
        kind=Kind.STANDARD,
        lifecycle_state=LifecycleState.PROPOSED,
        created_at=now,
        updated_at=now,
        created_by="maria.bauer@example.com",
    )
    base.update(overrides)
    return McpServer(**base)


def _build_client(mock_svc, mock_identity=None):
    """Build a minimal app with ONLY the mcp_servers router + a mocked service.

    Must be called AFTER the entra env fixture so the route module imports with
    the right settings. Injects the mock as the module singleton so no real AWS
    is touched. Optionally injects the lazy McpIdentityService singleton (the E7
    provisioning-hook + reprovision tests) so no real Graph / boto3 is touched.
    Also pre-seeds the E24 tenant-resolver singleton with an always-global stub,
    and `api.routes.tenants._svc` with a fake `.get` that accepts ANY tenant_id —
    this file predates tenant scoping; global admin bypasses all filtering and an
    always-known tenant preserves the pre-E24 fixtures' fixed "default" tenant_id
    (no real Tenant record backs it). Tenant scoping itself is covered by
    `test_registry_tenant_scoping.py`.
    """
    import api.routes.mcp_servers as mcp_servers_module
    import api.routes.tenants as tenants_module
    import api.routes.users as users_module
    from services.tenant_resolver import TenantContext

    mcp_servers_module._svc = mock_svc
    if mock_identity is not None:
        mcp_servers_module._identity_svc = mock_identity

    class _GlobalResolver:
        async def resolve(self, principal):
            return TenantContext(is_global=True, tenant_ids=frozenset(), tenants=())

    users_module._tenant_resolver = _GlobalResolver()

    class _AnyTenantService:
        def get(self, tenant_id):
            from unittest.mock import MagicMock

            return MagicMock(id=tenant_id)

    tenants_module._svc = _AnyTenantService()

    app = FastAPI()
    app.include_router(mcp_servers_module.router, prefix="/api/v1")
    return TestClient(app), mcp_servers_module


def _claims_for(role: str):
    """Return entra token claims for a given platform role."""
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


# --- VIEWER ---------------------------------------------------------------

def test_viewer_can_list(entra_settings):
    mock_svc = MagicMock()
    mock_svc.list.return_value = [_make_mcp_server()]
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/mcp-servers", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list) and body[0]["id"] == "rec-123"


def test_viewer_cannot_create(entra_settings):
    mock_svc = MagicMock()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post("/api/v1/mcp-servers", json={"name": "x"}, headers=_headers())

    assert resp.status_code == 403


def test_viewer_cannot_transition(entra_settings):
    mock_svc = MagicMock()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/mcp-servers/rec-123/transitions",
            json={"action": "approve", "reason": "ok"},
            headers=_headers(),
        )

    assert resp.status_code == 403


# --- OPERATOR -------------------------------------------------------------

def test_operator_can_create(entra_settings):
    mock_svc = MagicMock()
    mock_svc.create.return_value = _make_mcp_server()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers",
            json={"name": "claims-mcp-de", "description": "Claims MCP server", "tenant_id": "default"},
            headers=_headers(),
        )

    assert resp.status_code == 201
    assert resp.json()["id"] == "rec-123"


def test_operator_can_submit(entra_settings):
    from models.mcp_server import LifecycleState

    mock_svc = MagicMock()
    mock_svc.submit_for_approval.return_value = _make_mcp_server(
        lifecycle_state=LifecycleState.PENDING_APPROVAL
    )
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/mcp-servers/rec-123/submit", headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["lifecycle_state"] == "pending_approval"


def test_operator_cannot_transition(entra_settings):
    mock_svc = MagicMock()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers/rec-123/transitions",
            json={"action": "approve", "reason": "ok"},
            headers=_headers(),
        )

    assert resp.status_code == 403


# --- ADMIN ----------------------------------------------------------------

def test_admin_can_transition(entra_settings):
    from models.mcp_server import LifecycleState

    mock_svc = MagicMock()
    mock_svc.transition.return_value = _make_mcp_server(
        lifecycle_state=LifecycleState.APPROVED
    )
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/mcp-servers/rec-123/transitions",
            json={"action": "approve", "reason": "looks good"},
            headers=_headers(),
        )

    assert resp.status_code == 200
    assert resp.json()["lifecycle_state"] == "approved"
    mock_svc.transition.assert_called_once_with("rec-123", "approve", "looks good")


def test_admin_bad_action_returns_400(entra_settings):
    mock_svc = MagicMock()
    mock_svc.transition.side_effect = ValueError("Unknown transition action 'frobnicate'")
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/mcp-servers/rec-123/transitions",
            json={"action": "frobnicate", "reason": "x"},
            headers=_headers(),
        )

    assert resp.status_code == 400


def test_admin_illegal_transition_returns_409(entra_settings):
    """An illegal status edge maps to 409, not 400/500.

    IllegalTransitionError subclasses ValueError, so the 409 handler must be
    ordered BEFORE the generic ValueError->400 handler."""
    from services.mcp_server_service import IllegalTransitionError

    msg = (
        "Invalid status transition from DRAFT to APPROVED. Valid transitions from "
        "DRAFT: PENDING_APPROVAL, DEPRECATED, DRAFT, UPDATING"
    )
    mock_svc = MagicMock()
    mock_svc.transition.side_effect = IllegalTransitionError(msg)
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/mcp-servers/rec-123/transitions",
            json={"action": "approve", "reason": "ok"},
            headers=_headers(),
        )

    assert resp.status_code == 409
    assert "Invalid status transition" in resp.json()["detail"]


# --- Error mapping --------------------------------------------------------

def test_create_duplicate_name_returns_409(entra_settings):
    from services.mcp_server_service import NameTakenError

    mock_svc = MagicMock()
    mock_svc.create.side_effect = NameTakenError("MCP server name 'dup' is already in use")
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers", json={"name": "dup", "tenant_id": "default"}, headers=_headers()
        )

    assert resp.status_code == 409


def test_create_schema_invalid_returns_422(entra_settings):
    """A registry schema-validation rejection on create maps to 422."""
    from services.mcp_server_service import McpValidationError

    mock_svc = MagicMock()
    mock_svc.create.side_effect = McpValidationError("Schema validation failed: bad server.json")
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers",
            json={"name": "bad-mcp"},
            headers=_headers(),
        )

    assert resp.status_code == 422


def test_update_schema_invalid_returns_422(entra_settings):
    """A registry schema-validation rejection on update maps to 422."""
    from services.mcp_server_service import McpValidationError

    mock_svc = MagicMock()
    mock_svc.update.side_effect = McpValidationError("Schema validation failed: bad server.json")
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.put(
            "/api/v1/mcp-servers/rec-123",
            json={"description": "new"},
            headers=_headers(),
        )

    assert resp.status_code == 422


def test_get_missing_returns_404(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = None
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/mcp-servers/rec-missing", headers=_headers())

    assert resp.status_code == 404


# --- created_by / owner from principal -----------------------------------

def test_create_uses_principal_as_created_by(entra_settings):
    """POST /mcp-servers as operator must pass created_by == the principal's
    email, NOT a hardcoded 'user'."""
    maria_claims = {
        "oid": "maria-oid",
        "preferred_username": "maria.bauer@example.com",
        "roles": ["Platform.Operator"],
    }
    mock_svc = MagicMock()
    mock_svc.create.return_value = _make_mcp_server()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=maria_claims):
        resp = client.post(
            "/api/v1/mcp-servers",
            json={"name": "claims-mcp-de", "description": "Claims MCP server", "tenant_id": "default"},
            headers=_headers(),
        )

    assert resp.status_code == 201
    _, kwargs = mock_svc.create.call_args
    assert kwargs.get("created_by") == "maria.bauer@example.com"


def test_create_defaults_owner_to_principal(entra_settings):
    """When owner_* is blank, the create payload should default to the creator."""
    maria_claims = {
        "oid": "maria-oid",
        "preferred_username": "maria.bauer@example.com",
        "roles": ["Platform.Operator"],
    }
    mock_svc = MagicMock()
    mock_svc.create.return_value = _make_mcp_server()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=maria_claims):
        resp = client.post(
            "/api/v1/mcp-servers",
            json={"name": "claims-mcp-de", "tenant_id": "default"},
            headers=_headers(),
        )

    assert resp.status_code == 201
    args, _ = mock_svc.create.call_args
    req = args[0]
    assert req.owner_email == "maria.bauer@example.com"
    assert req.owner_oid == "maria-oid"


def test_list_passes_kind_filter(entra_settings):
    """GET /mcp-servers?kind=gateway must coerce + pass kind to svc.list."""
    from models.mcp_server import Kind

    mock_svc = MagicMock()
    mock_svc.list.return_value = []
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/mcp-servers?kind=gateway", headers=_headers())

    assert resp.status_code == 200
    _, kwargs = mock_svc.list.call_args
    assert kwargs.get("kind") == Kind.GATEWAY


def test_list_bad_kind_returns_400(entra_settings):
    """A bogus kind enum is rejected by _coerce_kind -> HTTP 400 (svc.list untouched)."""
    mock_svc = MagicMock()
    mock_svc.list.return_value = []
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/mcp-servers?kind=bogus", headers=_headers())

    assert resp.status_code == 400
    mock_svc.list.assert_not_called()


def test_list_bad_lifecycle_returns_400(entra_settings):
    """A bogus lifecycle_state enum is rejected by _coerce_lifecycle -> HTTP 400."""
    mock_svc = MagicMock()
    mock_svc.list.return_value = []
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/mcp-servers?lifecycle_state=bogus", headers=_headers())

    assert resp.status_code == 400


# --- create provisioning hook (Epic 7, T-ROUTES) -------------------------

def _make_agentcore_mcp(**overrides):
    """An AgentCore Gateway/Runtime MCP that should_provision_mcp() gates ON."""
    from models.mcp_server import Kind

    base = dict(
        kind=Kind.GATEWAY,
        gateway_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/gw-abc123",
    )
    base.update(overrides)
    return _make_mcp_server(**base)


def test_create_mcp_schedules_provisioning_for_agentcore_kinds(entra_settings):
    """A gateway (or runtime) MCP → set identity_status='pending' + persist_identity +
    schedule a background provision; the 201 returns immediately. A standard MCP →
    NOT scheduled and no persist_identity."""
    from unittest.mock import AsyncMock

    from models.mcp_server import IdentityStatus, Kind

    # --- gateway path (should provision) ---
    created_gateway = _make_agentcore_mcp(identity_status="none")
    mock_svc = MagicMock()
    mock_svc.create.return_value = created_gateway
    mock_svc.persist_identity.side_effect = lambda m: m
    mock_identity = MagicMock()
    mock_identity.provision = AsyncMock(return_value=created_gateway)

    client, _ = _build_client(mock_svc, mock_identity=mock_identity)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers",
            json={
                "name": "claims-mcp-de",
                "kind": "gateway",
                "gateway_arn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/gw-abc123",
                "tenant_id": "default",
            },
            headers=_headers(),
        )

    assert resp.status_code == 201
    # persist_identity was called with identity_status='pending' (before the bg task).
    assert mock_svc.persist_identity.called
    persisted = mock_svc.persist_identity.call_args[0][0]
    assert persisted.identity_status == "pending"
    # ...as the MEMBER (Epic 36/T20): a bare string would trip pydantic's serializer
    # warning on the 201's own response serialization.
    assert persisted.identity_status is IdentityStatus.PENDING
    # The background task scheduled provision (TestClient runs bg tasks after response).
    mock_identity.provision.assert_awaited_once()

    # --- runtime path (should also provision) ---
    created_runtime = _make_mcp_server(
        kind=Kind.RUNTIME,
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/rt-abc123",
        identity_status="none",
    )
    mock_svc_rt = MagicMock()
    mock_svc_rt.create.return_value = created_runtime
    mock_svc_rt.persist_identity.side_effect = lambda m: m
    mock_identity_rt = MagicMock()
    mock_identity_rt.provision = AsyncMock(return_value=created_runtime)
    client_rt, _ = _build_client(mock_svc_rt, mock_identity=mock_identity_rt)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp_rt = client_rt.post(
            "/api/v1/mcp-servers",
            json={
                "name": "runtime-mcp-de",
                "kind": "runtime",
                "runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/rt-abc123",
                "tenant_id": "default",
            },
            headers=_headers(),
        )

    assert resp_rt.status_code == 201
    mock_identity_rt.provision.assert_awaited_once()

    # --- standard path (should NOT provision) ---
    created_standard = _make_mcp_server(kind=Kind.STANDARD, identity_status="none")
    mock_svc2 = MagicMock()
    mock_svc2.create.return_value = created_standard
    mock_identity2 = MagicMock()
    mock_identity2.provision = AsyncMock()
    client2, _ = _build_client(mock_svc2, mock_identity=mock_identity2)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp2 = client2.post(
            "/api/v1/mcp-servers",
            json={"name": "standard-mcp-de", "kind": "standard", "tenant_id": "default"},
            headers=_headers(),
        )

    assert resp2.status_code == 201
    mock_svc2.persist_identity.assert_not_called()
    mock_identity2.provision.assert_not_called()


# --- POST /mcp-servers/{id}/reprovision (Epic 7, T-ROUTES) ---------------

def test_reprovision_sets_pending_and_schedules(entra_settings):
    """OPERATOR reprovision: first persist identity_status='pending', then schedule a
    background provision; returns 202."""
    from unittest.mock import AsyncMock

    from models.mcp_server import IdentityStatus, Kind

    mcp = _make_mcp_server(
        kind=Kind.GATEWAY,
        gateway_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/gw-abc123",
        identity_status="failed",
    )
    mock_svc = MagicMock()
    mock_svc.get.return_value = mcp
    mock_svc.persist_identity.side_effect = lambda m: m
    mock_identity = MagicMock()
    mock_identity.provision = AsyncMock(return_value=mcp)
    client, _ = _build_client(mock_svc, mock_identity=mock_identity)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/mcp-servers/rec-123/reprovision", headers=_headers())

    assert resp.status_code == 202
    assert mock_svc.persist_identity.called
    persisted = mock_svc.persist_identity.call_args[0][0]
    assert persisted.identity_status == "pending"
    # ...as the MEMBER (Epic 36/T20) — see the create-hook test above.
    assert persisted.identity_status is IdentityStatus.PENDING
    mock_identity.provision.assert_awaited_once()


def test_reprovision_viewer_forbidden(entra_settings):
    from unittest.mock import AsyncMock

    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp_server()
    mock_identity = MagicMock()
    mock_identity.provision = AsyncMock()
    client, _ = _build_client(mock_svc, mock_identity=mock_identity)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post("/api/v1/mcp-servers/rec-123/reprovision", headers=_headers())

    assert resp.status_code == 403
    mock_identity.provision.assert_not_called()


def test_reprovision_missing_mcp_404(entra_settings):
    from unittest.mock import AsyncMock

    mock_svc = MagicMock()
    mock_svc.get.return_value = None
    mock_identity = MagicMock()
    mock_identity.provision = AsyncMock()
    client, _ = _build_client(mock_svc, mock_identity=mock_identity)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/mcp-servers/rec-missing/reprovision", headers=_headers())

    assert resp.status_code == 404
    mock_identity.provision.assert_not_called()


def test_reprovision_non_provisionable_kind_409(entra_settings):
    """A standard (non-AgentCore) MCP cannot be (re)provisioned → 409."""
    from unittest.mock import AsyncMock

    from models.mcp_server import Kind

    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp_server(kind=Kind.STANDARD)
    mock_identity = MagicMock()
    mock_identity.provision = AsyncMock()
    client, _ = _build_client(mock_svc, mock_identity=mock_identity)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/mcp-servers/rec-123/reprovision", headers=_headers())

    assert resp.status_code == 409
    mock_identity.provision.assert_not_called()


# --- POST /mcp-servers/{id}/refresh-tools (Epic 7, T-REFRESH-TOOLS-BE) ----
# Synchronous, tools-ONLY native re-read (NOT a 202 background re-provision):
# 200 + the updated McpServer so the UI shows fresh tools on click. Gateway-only
# (409 for runtime/standard — no native target read). OPERATOR-gated (a mutation).

def test_refresh_tools_operator_ok(entra_settings):
    """OPERATOR refresh-tools on a gateway → 200 + the updated McpServer (synchronous).
    refresh_tools is awaited once; it's NOT the background-task / 202 reprovision path."""
    from unittest.mock import AsyncMock

    from models.mcp_server import Kind

    mcp = _make_mcp_server(
        kind=Kind.GATEWAY,
        gateway_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/gw-abc123",
        identity_status="provisioned",
    )
    updated = _make_mcp_server(
        kind=Kind.GATEWAY,
        gateway_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/gw-abc123",
        identity_status="provisioned",
        available_tools=[{"name": "claims___echo", "description": "echo", "input_schema": {}}],
    )
    mock_svc = MagicMock()
    mock_svc.get.return_value = mcp
    mock_identity = MagicMock()
    mock_identity.refresh_tools = AsyncMock(return_value=updated)
    client, _ = _build_client(mock_svc, mock_identity=mock_identity)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/mcp-servers/rec-123/refresh-tools", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert [t["name"] for t in body["available_tools"]] == ["claims___echo"]
    mock_identity.refresh_tools.assert_awaited_once_with(mcp)


def test_refresh_tools_viewer_forbidden(entra_settings):
    from unittest.mock import AsyncMock

    from models.mcp_server import Kind

    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp_server(
        kind=Kind.GATEWAY,
        gateway_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/gw-abc123",
    )
    mock_identity = MagicMock()
    mock_identity.refresh_tools = AsyncMock()
    client, _ = _build_client(mock_svc, mock_identity=mock_identity)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post("/api/v1/mcp-servers/rec-123/refresh-tools", headers=_headers())

    assert resp.status_code == 403
    mock_identity.refresh_tools.assert_not_called()


def test_refresh_tools_404_when_missing(entra_settings):
    from unittest.mock import AsyncMock

    mock_svc = MagicMock()
    mock_svc.get.return_value = None
    mock_identity = MagicMock()
    mock_identity.refresh_tools = AsyncMock()
    client, _ = _build_client(mock_svc, mock_identity=mock_identity)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/mcp-servers/rec-missing/refresh-tools", headers=_headers())

    assert resp.status_code == 404
    mock_identity.refresh_tools.assert_not_called()


def test_refresh_tools_409_when_not_gateway(entra_settings):
    """A non-gateway (standard/runtime) MCP has no native target read → 409."""
    from unittest.mock import AsyncMock

    from models.mcp_server import Kind

    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp_server(kind=Kind.STANDARD)
    mock_identity = MagicMock()
    mock_identity.refresh_tools = AsyncMock()
    client, _ = _build_client(mock_svc, mock_identity=mock_identity)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/mcp-servers/rec-123/refresh-tools", headers=_headers())

    assert resp.status_code == 409
    mock_identity.refresh_tools.assert_not_called()


# ===========================================================================
# E36/T16 (research item 5A) — DELETE /mcp-servers/{id} tears identity down.
#
# It used to delete the registry record and nothing else: the MCP's Entra app/SP (and every
# consent granted on it) were orphaned, and — the consequential half — the LIVE gateway kept
# an ENFORCE-mode Cedar policy engine nothing in the platform pointed at any more.
#
# Both legs are best-effort with a per-resource report line-item, so these pin: order
# (Entra FIRST, then the engine, then the record — fix round 1: the gateway is never deleted,
# so authentication must go before authorization or a half-done teardown leaves it serving with
# Cedar stripped), the exact call shapes, "skipped" for a record that owns neither, "skipped"
# for an engine the record does not own, "the record still goes when a leg fails", and "a 404
# has no side effects".
# ===========================================================================
def _mcp_with_identity():
    from models.mcp_server import Kind

    return _make_mcp_server(
        kind=Kind.GATEWAY,
        gateway_id="demo-claims-gw-aBcDeFgHiJ",
        gateway_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/demo-claims-gw-aBcDeFgHiJ",
        entra_app_id="mcp-app-guid",
        entra_sp_id="mcp-sp-objid",
        cedar_policy_engine_id="pe-1",
        cedar_policy_engine_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:policy-engine/pe-1",
        cedar_enforcement_mode="enforce",
    )


def _teardown_doubles():
    """(cedar, graph) doubles whose teardown methods are async no-ops (already-gone == the
    same shape: both real implementations swallow not-found and return None)."""
    from unittest.mock import AsyncMock

    cedar = MagicMock(name="McpCedarService")
    cedar.delete_policy_engine = AsyncMock(return_value=None)
    graph = MagicMock(name="GraphService")
    graph.delete_agent_app = AsyncMock(return_value=None)
    return cedar, graph


def test_delete_tears_down_entra_app_then_engine_before_the_record(entra_settings):
    mcp = _mcp_with_identity()
    mock_svc = MagicMock()
    mock_svc.get.return_value = mcp
    mock_svc.delete.return_value = mcp
    client, _ = _build_client(mock_svc)
    cedar, graph = _teardown_doubles()

    manager = MagicMock()
    manager.attach_mock(graph.delete_agent_app, "delete_agent_app")
    manager.attach_mock(cedar.delete_policy_engine, "delete_policy_engine")
    manager.attach_mock(mock_svc.delete, "delete_record")

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")), \
         patch("api.routes.mcp_cedar._cedar_svc", cedar), \
         patch("api.routes.mcp_server_grants._graph_svc", graph):
        resp = client.delete("/api/v1/mcp-servers/rec-123", headers=_headers())

    assert resp.status_code == 200
    # AUTHENTICATION BEFORE AUTHORIZATION, then the record. Nothing deletes the GATEWAY, so it
    # is live throughout: stripping Cedar first left a window (and, on a failed identity leg, a
    # permanent state) in which it authorized every tool call with its authenticator intact.
    assert [c[0] for c in manager.mock_calls] == [
        "delete_agent_app",
        "delete_policy_engine",
        "delete_record",
    ]
    # The engine is detached+deleted, and the Entra app is deleted with the STORED ids (the
    # generic idempotent teardown — deleting the app cascades the SP and its consents).
    cedar.delete_policy_engine.assert_awaited_once_with(mcp)
    graph.delete_agent_app.assert_awaited_once_with(
        entra_app_id="mcp-app-guid", entra_sp_id="mcp-sp-objid"
    )
    # ...and only THEN is the record deleted (every id the cascade needs lives on it).
    mock_svc.delete.assert_called_once_with("rec-123")


def test_delete_skips_both_legs_for_a_standard_mcp(entra_settings):
    """A standard MCP owns no gateway, no engine and no Entra app ⇒ no AWS/Graph call at
    all, and the record still goes."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp_server()
    mock_svc.delete.return_value = _make_mcp_server()
    client, _ = _build_client(mock_svc)
    cedar, graph = _teardown_doubles()

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")), \
         patch("api.routes.mcp_cedar._cedar_svc", cedar), \
         patch("api.routes.mcp_server_grants._graph_svc", graph):
        resp = client.delete("/api/v1/mcp-servers/rec-123", headers=_headers())

    assert resp.status_code == 200
    cedar.delete_policy_engine.assert_not_awaited()
    graph.delete_agent_app.assert_not_awaited()
    mock_svc.delete.assert_called_once_with("rec-123")


def test_delete_reports_a_failed_leg_but_still_deletes_the_record(entra_settings):
    """Best-effort means best-effort: a Graph 403 / a busy gateway must not leave the
    operator with a record they cannot delete. Both legs are idempotent, so the retryable
    path is 'fix the cause, delete again' — never 'the row is trapped'."""
    from services.mcp_cedar_service import McpCedarError

    mcp = _mcp_with_identity()
    mock_svc = MagicMock()
    mock_svc.get.return_value = mcp
    mock_svc.delete.return_value = mcp
    client, _ = _build_client(mock_svc)
    cedar, graph = _teardown_doubles()
    cedar.delete_policy_engine.side_effect = McpCedarError("policy engine delete failed")
    graph.delete_agent_app.side_effect = RuntimeError("graph 403")

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")), \
         patch("api.routes.mcp_cedar._cedar_svc", cedar), \
         patch("api.routes.mcp_server_grants._graph_svc", graph):
        resp = client.delete("/api/v1/mcp-servers/rec-123", headers=_headers())

    assert resp.status_code == 200
    # BOTH legs were attempted — one failure must not skip the next resource.
    cedar.delete_policy_engine.assert_awaited_once()
    graph.delete_agent_app.assert_awaited_once()
    mock_svc.delete.assert_called_once_with("rec-123")


def test_delete_reports_per_resource_items_with_the_cascade_vocabulary(entra_settings):
    """The report itself: one entry per resource, ``deleted``/``failed``/``skipped`` and a
    SAFE reason (the exception TYPE name, never a Graph body or an AWS message)."""
    import asyncio

    from services.mcp_cedar_service import McpCedarError

    client, mcp_servers_module = _build_client(MagicMock())
    mcp = _mcp_with_identity()
    mcp.entra_app_id = None
    mcp.entra_sp_id = None
    cedar, graph = _teardown_doubles()
    cedar.delete_policy_engine.side_effect = McpCedarError("policy engine delete failed")

    with patch("api.routes.mcp_cedar._cedar_svc", cedar), \
         patch("api.routes.mcp_server_grants._graph_svc", graph):
        items = asyncio.run(mcp_servers_module._teardown_mcp_identity(mcp))

    assert [(i.item, i.outcome, i.reason) for i in items] == [
        ("identity", "skipped", None),
        ("policy_engine", "failed", "McpCedarError"),
    ]


def test_delete_reports_skipped_for_an_engine_the_record_does_not_own(entra_settings):
    """The wrong-target guard, seen from the route: `delete_policy_engine` returns a skip
    REASON instead of deleting an engine only the live gateway names, and that becomes a
    ``skipped`` line-item — never the false ``deleted`` the old "no engine anywhere" branch
    produced."""
    import asyncio

    client, mcp_servers_module = _build_client(MagicMock())
    mcp = _mcp_with_identity()
    mcp.cedar_policy_engine_id = None
    mcp.cedar_policy_engine_arn = None
    cedar, graph = _teardown_doubles()
    cedar.delete_policy_engine.return_value = "engine not owned by this record"

    with patch("api.routes.mcp_cedar._cedar_svc", cedar), \
         patch("api.routes.mcp_server_grants._graph_svc", graph):
        items = asyncio.run(mcp_servers_module._teardown_mcp_identity(mcp))

    assert [(i.item, i.outcome, i.reason) for i in items] == [
        ("identity", "deleted", None),
        ("policy_engine", "skipped", "engine not owned by this record"),
    ]


def test_teardown_item_carries_T8s_prefixed_reason_vocabulary(entra_settings):
    """``_teardown_item`` is the async twin of ``ProjectService._run_step``, so the same two
    cross-account failures must read the same on both paths: "we know the account and cannot
    get in" (`assume_role_failed:`) and "we cannot tell which account owns it"
    (`stage_unresolved:`) are different operator actions, and the bare type name names
    neither."""
    import asyncio

    from services.tenant_credentials import StageUnresolvedError, TenantCredentialsError

    client, mcp_servers_module = _build_client(MagicMock())

    async def _raise(err):
        raise err

    assume = asyncio.run(
        mcp_servers_module._teardown_item(
            "identity", lambda: _raise(TenantCredentialsError("agp-deployment-acme-prod"))
        )
    )
    unresolved = asyncio.run(
        mcp_servers_module._teardown_item(
            "identity", lambda: _raise(StageUnresolvedError("no tenant record for stage 'prod'"))
        )
    )

    assert (assume.outcome, assume.reason) == (
        "failed",
        "assume_role_failed: agp-deployment-acme-prod",
    )
    assert (unresolved.outcome, unresolved.reason) == (
        "failed",
        "stage_unresolved: no tenant record for stage 'prod'",
    )


def test_delete_missing_mcp_has_no_teardown_side_effects(entra_settings):
    """The visibility gate runs FIRST: a missing (or foreign-tenant) record 404s without
    touching AWS or Graph."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = None
    client, _ = _build_client(mock_svc)
    cedar, graph = _teardown_doubles()

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")), \
         patch("api.routes.mcp_cedar._cedar_svc", cedar), \
         patch("api.routes.mcp_server_grants._graph_svc", graph):
        resp = client.delete("/api/v1/mcp-servers/rec-missing", headers=_headers())

    assert resp.status_code == 404
    cedar.delete_policy_engine.assert_not_awaited()
    graph.delete_agent_app.assert_not_awaited()
    mock_svc.delete.assert_not_called()
