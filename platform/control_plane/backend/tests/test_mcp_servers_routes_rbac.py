"""RBAC + behavior tests for the MCP Server Registry routes.

Structural clone of `test_agents_routes_rbac.py`. These exercise the REAL
`require_role` + `current_principal` dependency path (`AUTH_PROVIDER=entra`)
against a mocked `verify_entra_token` (so no live Entra) and a mocked
`McpServerRegistryService` (so no live AWS / registry).

A minimal FastAPI app including ONLY the mcp_servers router is built per test
(after the entra env is set + the auth/config modules are reset) to avoid
importing the whole app's import chain.
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("reset_modules")


def _make_mcp_server(**overrides):
    """Build a real McpServer model instance for mock return values."""
    from models.mcp_server import Kind, LifecycleState, McpServer

    now = datetime.now(timezone.utc)
    base: dict[str, Any] = dict(
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
    is touched. Optionally injects the lazy McpIdentityService singleton (the
    provisioning-hook + reprovision tests) so no real Graph / boto3 is touched.
    Also pre-seeds the tenant-resolver singleton with an always-global stub,
    and `api.routes.tenants._svc` with a fake `.get` that accepts ANY tenant_id —
    this file predates tenant scoping; global admin bypasses all filtering and an
    always-known tenant preserves the older fixtures' fixed "default" tenant_id
    (no real Tenant record backs it). Tenant scoping itself is covered by
    `test_registry_tenant_scoping.py`.
    """
    import api.routes.mcp_servers as mcp_servers_module
    import api.routes.tenants as tenants_module
    import api.routes.users as users_module
    from core.errors import install_error_handlers
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
    install_error_handlers(app)
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


NOT_FOUND_BODY = {"detail": "MCP server not found", "code": "mcp_server.not_found"}


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
    assert resp.json() == {"detail": "Requires operator role or higher", "code": "auth.role_required"}


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
    assert resp.json() == {"detail": "Requires admin role or higher", "code": "auth.role_required"}


def test_admin_can_transition(entra_settings):
    from models.mcp_server import LifecycleState

    mock_svc = MagicMock()
    mock_svc.transition.return_value = _make_mcp_server(lifecycle_state=LifecycleState.APPROVED)
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


def test_mcp_servers_invalid_request_is_400(entra_settings):
    """An unknown action or an empty reason renders the service's own sentence verbatim."""
    from services.mcp_server_service import IllegalTransitionError

    msg = "Unknown transition action 'frobnicate'; valid actions: ['approve', 'deprecate', 'reject']"
    mock_svc = MagicMock()
    mock_svc.transition.side_effect = IllegalTransitionError(msg, "invalid_request")
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/mcp-servers/rec-123/transitions",
            json={"action": "frobnicate", "reason": "x"},
            headers=_headers(),
        )

    assert resp.status_code == 400
    assert resp.json() == {"detail": msg, "code": "mcp_server.invalid_request"}


def test_mcp_servers_illegal_edge_is_409(entra_settings):
    """An illegal status edge renders the registry's explanation of the legal edges."""
    from services.mcp_server_service import IllegalTransitionError

    msg = (
        "Invalid status transition from DRAFT to APPROVED. Valid transitions from "
        "DRAFT: PENDING_APPROVAL, DEPRECATED, DRAFT, UPDATING"
    )
    mock_svc = MagicMock()
    mock_svc.transition.side_effect = IllegalTransitionError(msg, "illegal_edge")
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/mcp-servers/rec-123/transitions",
            json={"action": "approve", "reason": "ok"},
            headers=_headers(),
        )

    assert resp.status_code == 409
    assert resp.json() == {"detail": msg, "code": "mcp_server.illegal_edge"}


def test_mcp_servers_name_taken_is_409(entra_settings):
    from services.mcp_server_service import NameTakenError

    mock_svc = MagicMock()
    mock_svc.create.side_effect = NameTakenError("MCP server name 'dup' is already in use")
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers", json={"name": "dup", "tenant_id": "default"}, headers=_headers()
        )

    assert resp.status_code == 409
    assert resp.json() == {
        "detail": "MCP server name 'dup' is already in use",
        "code": "mcp_server.name_taken",
    }


def test_mcp_server_error_detail_for_name_taken_carries_no_upstream_body(entra_settings):
    """The real service over a registry whose list response carries a token-shaped string: the
    detail is the service's sentence about the name, never the response body."""
    from services.mcp_server_service import McpServerRegistryService

    marker = "ghs_" + "a" * 36
    ctl = MagicMock()
    ctl.list_registry_records.return_value = {
        "registryRecords": [
            {"recordId": "mcp-existing", "name": "dup", "status": "DRAFT", "description": marker}
        ]
    }
    svc = McpServerRegistryService(registry_id="reg-1", control_client=ctl)
    client, _ = _build_client(svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers", json={"name": "dup", "tenant_id": "default"}, headers=_headers()
        )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "MCP server name 'dup' is already in use"
    assert marker not in resp.text
    ctl.create_registry_record.assert_not_called()


@pytest.mark.parametrize(
    "body, detail",
    [
        (
            {"action": "frobnicate", "reason": "x"},
            "Unknown transition action 'frobnicate'; valid actions: ['approve', 'deprecate', 'reject']",
        ),
        ({"action": "approve", "reason": "   "}, "A non-empty reason is required for a status transition"),
    ],
    ids=["unknown-action", "empty-reason"],
)
def test_mcp_server_error_detail_for_invalid_request_carries_no_upstream_body(entra_settings, body, detail):
    """``invalid_request`` sends the message verbatim, so the message must be safe by construction:
    the real service refuses before any registry write, over a record whose description carries a
    token-shaped string, and the detail is the service's sentence about the request."""
    from conftest import _mcp_sample_record

    from services.mcp_server_service import McpServerRegistryService

    marker = "ghs_" + "a" * 36
    ctl = MagicMock()
    ctl.get_registry_record.return_value = _mcp_sample_record(status="DRAFT", description=marker)
    svc = McpServerRegistryService(registry_id="reg-1", control_client=ctl)
    client, _ = _build_client(svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            f"/api/v1/mcp-servers/{_mcp_sample_record()['recordId']}/transitions",
            json=body,
            headers=_headers(),
        )

    assert resp.status_code == 400
    assert resp.json() == {"detail": detail, "code": "mcp_server.invalid_request"}
    assert marker not in resp.text
    ctl.update_registry_record_status.assert_not_called()


def test_mcp_servers_schema_is_422_with_the_registry_message(entra_settings):
    """A registry schema rejection on create renders the registry's message about the payload."""
    from services.mcp_server_service import McpValidationError

    mock_svc = MagicMock()
    mock_svc.create.side_effect = McpValidationError("Schema validation failed: bad server.json")
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers",
            json={"name": "bad-mcp", "tenant_id": "default"},
            headers=_headers(),
        )

    assert resp.status_code == 422
    assert resp.json() == {
        "detail": "Schema validation failed: bad server.json",
        "code": "mcp_server.schema",
    }


def test_update_schema_invalid_returns_422(entra_settings):
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
    assert resp.json()["code"] == "mcp_server.schema"


def test_mcp_servers_create_unknown_tenant_is_404(entra_settings):
    """The tenant service's own not-found renders, with its code, before any registry call."""
    import api.routes.tenants as tenants_module
    from services.tenant_service import TenantError

    mock_svc = MagicMock()
    client, _ = _build_client(mock_svc)
    tenants_module._svc = MagicMock(
        get=MagicMock(side_effect=TenantError("tenant ten-unknown not found", "not_found"))
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers",
            json={"name": "new-mcp", "tenant_id": "ten-unknown"},
            headers=_headers(),
        )

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Tenant not found", "code": "tenant.not_found"}
    mock_svc.create.assert_not_called()


def test_get_missing_returns_404(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = None
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/mcp-servers/rec-missing", headers=_headers())

    assert resp.status_code == 404
    assert resp.json() == NOT_FOUND_BODY


@pytest.mark.parametrize(
    ("method", "path", "role", "body", "service_call"),
    [
        ("put", "/api/v1/mcp-servers/rec-123", "operator", {"description": "x"}, "update"),
        ("post", "/api/v1/mcp-servers/rec-123/submit", "operator", None, "submit_for_approval"),
        (
            "post",
            "/api/v1/mcp-servers/rec-123/transitions",
            "admin",
            {"action": "approve", "reason": "ok"},
            "transition",
        ),
        ("put", "/api/v1/mcp-servers/rec-123/publish", "operator", {"published": True}, "set_published"),
    ],
)
def test_a_record_gone_between_the_gate_and_the_write_is_404(
    entra_settings, method, path, role, body, service_call
):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp_server()
    getattr(mock_svc, service_call).return_value = None
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for(role)):
        resp = client.request(method, path, json=body, headers=_headers())

    assert resp.status_code == 404
    assert resp.json() == NOT_FOUND_BODY
    getattr(mock_svc, service_call).assert_called_once()


def test_a_record_gone_before_the_delete_is_404(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp_server()
    mock_svc.delete.return_value = None
    client, _ = _build_client(mock_svc)
    cedar, graph = _teardown_doubles()

    with (
        patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")),
        patch("api.routes.mcp_cedar._cedar_svc", cedar),
        patch("api.routes.mcp_server_grants._graph_svc", graph),
    ):
        resp = client.delete("/api/v1/mcp-servers/rec-123", headers=_headers())

    assert resp.status_code == 404
    assert resp.json() == NOT_FOUND_BODY
    mock_svc.delete.assert_called_once()


def test_an_operator_setting_shared_on_create_is_403(entra_settings):
    mock_svc = MagicMock()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers",
            json={"name": "claims-mcp-de", "tenant_id": "default", "shared": True},
            headers=_headers(),
        )

    assert resp.status_code == 403
    assert resp.json() == {"detail": "only ADMIN may set shared", "code": "mcp_server.shared_admin_only"}
    mock_svc.create.assert_not_called()


def test_create_in_a_tenant_the_caller_is_not_a_member_of_is_403(entra_settings):
    import api.routes.users as users_module
    from services.tenant_resolver import TenantContext

    class _ScopedResolver:
        async def resolve(self, principal):
            return TenantContext(is_global=False, tenant_ids=frozenset({"ten-1"}), tenants=())

    mock_svc = MagicMock()
    client, _ = _build_client(mock_svc)
    users_module._tenant_resolver = _ScopedResolver()

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers", json={"name": "claims-mcp-de", "tenant_id": "ten-2"}, headers=_headers()
        )

    assert resp.status_code == 403
    assert resp.json() == {"detail": "tenant not permitted", "code": "tenant.not_permitted"}
    mock_svc.create.assert_not_called()


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


@pytest.mark.parametrize("name", ["kind", "lifecycle_state"])
def test_list_with_an_unknown_filter_value_is_fastapi_422(entra_settings, name):
    mock_svc = MagicMock()
    mock_svc.list.return_value = []
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get(f"/api/v1/mcp-servers?{name}=bogus", headers=_headers())

    assert resp.status_code == 422
    body = resp.json()
    assert "code" not in body
    assert [error["loc"] for error in body["detail"]] == [["query", name]]
    mock_svc.list.assert_not_called()


def test_list_passes_the_lifecycle_filter_as_its_enum(entra_settings):
    from models.mcp_server import LifecycleState

    mock_svc = MagicMock()
    mock_svc.list.return_value = []
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/mcp-servers?lifecycle_state=approved", headers=_headers())

    assert resp.status_code == 200
    _, kwargs = mock_svc.list.call_args
    assert kwargs.get("lifecycle_state") is LifecycleState.APPROVED


def _make_agentcore_mcp(**overrides):
    """An AgentCore Gateway/Runtime MCP that should_provision_mcp() gates ON."""
    from models.mcp_server import Kind

    base: dict[str, Any] = dict(
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
    assert mock_svc.persist_identity.called
    persisted = mock_svc.persist_identity.call_args[0][0]
    assert persisted.identity_status == "pending"
    assert persisted.identity_status is IdentityStatus.PENDING
    mock_identity.provision.assert_awaited_once()

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
    assert resp.json() == NOT_FOUND_BODY
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
    assert resp.json() == {
        "detail": "not an AgentCore gateway or runtime MCP",
        "code": "mcp_server.not_provisionable",
    }
    mock_identity.provision.assert_not_called()


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
    assert resp.json() == NOT_FOUND_BODY
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
    assert resp.json() == {
        "detail": "tool refresh is only supported for gateway MCP servers",
        "code": "mcp_server.tools_gateway_only",
    }
    mock_identity.refresh_tools.assert_not_called()


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

    with (
        patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")),
        patch("api.routes.mcp_cedar._cedar_svc", cedar),
        patch("api.routes.mcp_server_grants._graph_svc", graph),
    ):
        resp = client.delete("/api/v1/mcp-servers/rec-123", headers=_headers())

    assert resp.status_code == 200
    assert [c[0] for c in manager.mock_calls] == [
        "delete_agent_app",
        "delete_policy_engine",
        "delete_record",
    ]
    cedar.delete_policy_engine.assert_awaited_once_with(mcp)
    graph.delete_agent_app.assert_awaited_once_with(entra_app_id="mcp-app-guid", entra_sp_id="mcp-sp-objid")
    mock_svc.delete.assert_called_once_with("rec-123")


def test_delete_skips_both_legs_for_a_standard_mcp(entra_settings):
    """A standard MCP owns no gateway, no engine and no Entra app ⇒ no AWS/Graph call at
    all, and the record still goes."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_mcp_server()
    mock_svc.delete.return_value = _make_mcp_server()
    client, _ = _build_client(mock_svc)
    cedar, graph = _teardown_doubles()

    with (
        patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")),
        patch("api.routes.mcp_cedar._cedar_svc", cedar),
        patch("api.routes.mcp_server_grants._graph_svc", graph),
    ):
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
    cedar.delete_policy_engine.side_effect = McpCedarError(
        "policy engine delete failed", "engine_delete_failed"
    )
    graph.delete_agent_app.side_effect = RuntimeError("graph 403")

    with (
        patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")),
        patch("api.routes.mcp_cedar._cedar_svc", cedar),
        patch("api.routes.mcp_server_grants._graph_svc", graph),
    ):
        resp = client.delete("/api/v1/mcp-servers/rec-123", headers=_headers())

    assert resp.status_code == 200
    graph.delete_agent_app.assert_awaited_once()
    cedar.delete_policy_engine.assert_not_awaited()
    mock_svc.delete.assert_called_once_with("rec-123")


def test_a_failed_identity_leg_keeps_the_policy_engine(entra_settings):
    """Fail CLOSED: the Entra app survived, so the live gateway still accepts its tokens —
    deleting Cedar now would leave it authorizing every call. The engine is kept, and says so."""
    import asyncio

    from services import mcp_teardown

    _build_client(MagicMock())
    cedar, graph = _teardown_doubles()
    graph.delete_agent_app.side_effect = RuntimeError("graph 403")

    with (
        patch("api.routes.mcp_cedar._cedar_svc", cedar),
        patch("api.routes.mcp_server_grants._graph_svc", graph),
    ):
        items = asyncio.run(mcp_teardown.teardown_mcp_identity(_mcp_with_identity()))

    assert [(i.item, i.outcome, i.reason) for i in items] == [
        ("identity", "failed", "RuntimeError"),
        (
            "policy_engine",
            "skipped",
            "kept because the identity teardown failed; the gateway stays policy-enforced until a retry",
        ),
    ]
    cedar.delete_policy_engine.assert_not_awaited()


def test_a_deleted_identity_then_a_failed_engine_reports_both(entra_settings):
    """The other order: identity gone (fail-closed already), the engine delete fails — that
    failure is reported, never hidden."""
    import asyncio

    from services import mcp_teardown
    from services.mcp_cedar_service import McpCedarError

    _build_client(MagicMock())
    cedar, graph = _teardown_doubles()
    cedar.delete_policy_engine.side_effect = McpCedarError(
        "policy engine delete failed", "engine_delete_failed"
    )

    with (
        patch("api.routes.mcp_cedar._cedar_svc", cedar),
        patch("api.routes.mcp_server_grants._graph_svc", graph),
    ):
        items = asyncio.run(mcp_teardown.teardown_mcp_identity(_mcp_with_identity()))

    assert [(i.item, i.outcome, i.reason) for i in items] == [
        ("identity", "deleted", None),
        ("policy_engine", "failed", "McpCedarError"),
    ]


def test_delete_reports_per_resource_items_with_the_cascade_vocabulary(entra_settings):
    """The report itself: one entry per resource, ``deleted``/``failed``/``skipped`` and a
    SAFE reason (the exception TYPE name, never a Graph body or an AWS message)."""
    import asyncio

    from services import mcp_teardown
    from services.mcp_cedar_service import McpCedarError

    _build_client(MagicMock())
    mcp = _mcp_with_identity()
    mcp.entra_app_id = None
    mcp.entra_sp_id = None
    cedar, graph = _teardown_doubles()
    cedar.delete_policy_engine.side_effect = McpCedarError(
        "policy engine delete failed", "engine_delete_failed"
    )

    with (
        patch("api.routes.mcp_cedar._cedar_svc", cedar),
        patch("api.routes.mcp_server_grants._graph_svc", graph),
    ):
        items = asyncio.run(mcp_teardown.teardown_mcp_identity(mcp))

    assert [(i.item, i.outcome, i.reason) for i in items] == [
        ("identity", "absent", "no Entra app on record — nothing to remove"),
        ("policy_engine", "failed", "McpCedarError"),
    ]


def test_delete_reports_skipped_for_an_engine_the_record_does_not_own(entra_settings):
    """The wrong-target guard, seen from the route: `delete_policy_engine` returns a skip
    REASON instead of deleting an engine only the live gateway names, and that becomes a
    ``skipped`` line-item — never the false ``deleted`` the old "no engine anywhere" branch
    produced."""
    import asyncio

    from services import mcp_teardown

    _build_client(MagicMock())
    mcp = _mcp_with_identity()
    mcp.cedar_policy_engine_id = None
    mcp.cedar_policy_engine_arn = None
    cedar, graph = _teardown_doubles()
    cedar.delete_policy_engine.return_value = "engine not owned by this record"

    with (
        patch("api.routes.mcp_cedar._cedar_svc", cedar),
        patch("api.routes.mcp_server_grants._graph_svc", graph),
    ):
        items = asyncio.run(mcp_teardown.teardown_mcp_identity(mcp))

    assert [(i.item, i.outcome, i.reason) for i in items] == [
        ("identity", "deleted", None),
        ("policy_engine", "skipped", "engine not owned by this record"),
    ]


def test_teardown_item_carries_T8s_prefixed_reason_vocabulary(entra_settings):
    """``_teardown_item`` is the async twin of ``services.project_runtime_teardown._run_step``, so the same
    two cross-account failures must read the same on both paths: "we know the account and cannot
    get in" (`assume_role_failed:`) and "we cannot tell which account owns it"
    (`stage_unresolved:`) are different operator actions, and the bare type name names
    neither."""
    import asyncio

    from services import mcp_teardown
    from services.tenant_credentials import StageUnresolvedError, TenantCredentialsError

    _build_client(MagicMock())

    async def _raise(err):
        raise err

    assume = asyncio.run(
        mcp_teardown._teardown_item(
            "identity", lambda: _raise(TenantCredentialsError("agp-deployment-acme-prod"))
        )
    )
    unresolved = asyncio.run(
        mcp_teardown._teardown_item(
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

    with (
        patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")),
        patch("api.routes.mcp_cedar._cedar_svc", cedar),
        patch("api.routes.mcp_server_grants._graph_svc", graph),
    ):
        resp = client.delete("/api/v1/mcp-servers/rec-missing", headers=_headers())

    assert resp.status_code == 404
    assert resp.json() == NOT_FOUND_BODY
    cedar.delete_policy_engine.assert_not_awaited()
    graph.delete_agent_app.assert_not_awaited()
    mock_svc.delete.assert_not_called()


_OWNED_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/claims-gw-aBcDeFgHiJ"


def _repo_owned_mcp():
    from models.mcp_server import Kind

    return _make_mcp_server(
        kind=Kind.GATEWAY,
        gateway_arn=_OWNED_ARN,
        endpoint_url="https://claims.example.com/mcp",
        project_id="proj-1",
        repo_id="repo-1",
    )


@pytest.mark.parametrize(
    "body",
    [
        {"name": "renamed-mcp"},
        {"kind": "standard"},
        {"gateway_arn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/other"},
        {"runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/rt"},
        {"endpoint_url": "https://elsewhere.example.com/mcp"},
    ],
)
def test_put_refuses_handle_changes_on_a_repo_owned_mcp(entra_settings, body):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _repo_owned_mcp()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.put("/api/v1/mcp-servers/rec-123", json=body, headers=_headers())

    assert resp.status_code == 409
    assert resp.json() == {"detail": "managed by repository", "code": "mcp_server.repo_managed"}
    mock_svc.update.assert_not_called()


def test_put_allows_governance_edits_and_unchanged_handles_on_a_repo_owned_mcp(entra_settings):
    mcp = _repo_owned_mcp()
    mock_svc = MagicMock()
    mock_svc.get.return_value = mcp
    mock_svc.update.return_value = mcp
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.put(
            "/api/v1/mcp-servers/rec-123",
            json={
                "name": "claims-mcp-de",
                "description": "Claims API",
                "kind": "gateway",
                "gateway_arn": _OWNED_ARN,
            },
            headers=_headers(),
        )

    assert resp.status_code == 200
    mock_svc.update.assert_called_once()


def test_delete_refuses_a_repo_owned_mcp(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _repo_owned_mcp()
    client, _ = _build_client(mock_svc)
    cedar, graph = _teardown_doubles()

    with (
        patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")),
        patch("api.routes.mcp_cedar._cedar_svc", cedar),
        patch("api.routes.mcp_server_grants._graph_svc", graph),
    ):
        resp = client.delete("/api/v1/mcp-servers/rec-123", headers=_headers())

    assert resp.status_code == 409
    assert resp.json() == {
        "detail": "delete the repository that owns this MCP server",
        "code": "mcp_server.delete_through_repository",
    }
    graph.delete_agent_app.assert_not_awaited()
    cedar.delete_policy_engine.assert_not_awaited()
    mock_svc.delete.assert_not_called()


def test_reprovision_refuses_a_repo_owned_mcp(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _repo_owned_mcp()
    mock_identity = MagicMock()
    client, _ = _build_client(mock_svc, mock_identity=mock_identity)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/mcp-servers/rec-123/reprovision", headers=_headers())

    assert resp.status_code == 409
    assert resp.json() == {
        "detail": "rebuild the repository instead",
        "code": "mcp_server.rebuild_through_repository",
    }
    mock_svc.persist_identity.assert_not_called()
    mock_identity.provision.assert_not_called()


def test_delete_route_uses_the_shared_teardown_service(entra_settings):
    """The cascade moved to ``services.mcp_teardown`` so the repo delete can share it; the
    route must call THAT function (patched here), not a private copy."""
    from unittest.mock import AsyncMock

    mcp = _mcp_with_identity()
    mock_svc = MagicMock()
    mock_svc.get.return_value = mcp
    mock_svc.delete.return_value = mcp
    client, mcp_servers_module = _build_client(mock_svc)
    teardown = AsyncMock(return_value=[])

    with (
        patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")),
        patch.object(mcp_servers_module, "teardown_mcp_identity", teardown),
    ):
        resp = client.delete("/api/v1/mcp-servers/rec-123", headers=_headers())

    assert resp.status_code == 200
    teardown.assert_awaited_once_with(mcp)
    from services.mcp_teardown import teardown_mcp_identity

    assert teardown_mcp_identity.__module__ == "services.mcp_teardown"


def test_delete_route_logs_the_identity_ids_when_teardown_fails(entra_settings, caplog):
    """The route delete has no retry (the record goes regardless), so a failed leg
    logs the ids the record carried — the only trail for a manual cleanup."""
    import logging
    from unittest.mock import AsyncMock

    from models.repository import RepoDeleteItemResult

    mcp = _mcp_with_identity()
    mock_svc = MagicMock()
    mock_svc.get.return_value = mcp
    mock_svc.delete.return_value = mcp
    client, mcp_servers_module = _build_client(mock_svc)
    teardown = AsyncMock(
        return_value=[
            RepoDeleteItemResult(item="identity", outcome="failed", reason="HTTPStatusError"),
            RepoDeleteItemResult(item="policy_engine", outcome="skipped", reason="kept"),
        ]
    )

    with (
        patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")),
        patch.object(mcp_servers_module, "teardown_mcp_identity", teardown),
        caplog.at_level(logging.WARNING, logger=mcp_servers_module.logger.name),
    ):
        resp = client.delete("/api/v1/mcp-servers/rec-123", headers=_headers())

    assert resp.status_code == 200
    mock_svc.delete.assert_called_once()
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[teardown]")]
    assert (
        "identity" in line and "entra_app_id=mcp-app-guid" in line and "cedar_policy_engine_id=pe-1" in line
    )


def test_a_stored_enforcement_mode_outside_the_set_folds_to_none_and_warns(caplog):
    """A record whose envelope carries a ``cedar_enforcement_mode`` no release wrote hydrates to
    ``none`` and logs one warning naming the raw value and the record id, so the read model never
    raises on a stored value and the writer bug leaves a trail."""
    import json
    import logging

    from conftest import _mcp_sample_envelope, _mcp_sample_record

    import models.mcp_server as mcp_server_module
    from models.mcp_server import McpServer

    envelope = _mcp_sample_envelope()
    envelope["cedar_enforcement_mode"] = "audit"
    record = _mcp_sample_record(_envelope=envelope)
    descriptor = record["descriptors"]["mcpServer"]
    server_json = json.loads(descriptor["data"])
    tools = json.loads(descriptor["additionalData"]["tools"]["data"])["tools"]

    with caplog.at_level(logging.WARNING, logger=mcp_server_module.logger.name):
        mcp = McpServer.from_record(record, server_json, tools)

    assert mcp.cedar_enforcement_mode == "none"
    (line,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "'audit'" in line and record["recordId"] in line and "'none'" in line


def test_mcp_server_body_schemas_are_unqualified_in_the_document():
    """The publish toggle has its own name and the transition body is the shared model, so no
    schema of this route carries the module-qualified ``mcp_servers__`` prefix FastAPI assigns to a
    name two routers both define."""
    from main import app

    schemas = app.openapi()["components"]["schemas"]

    assert "PublishToggle" in schemas
    assert [name for name in schemas if "mcp_servers__" in name] == []
