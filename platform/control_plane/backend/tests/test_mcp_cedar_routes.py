"""Cedar gateway-policy route tests (Epic 8, Task T4).

Exercise the REAL ``require_role`` + ``current_principal`` dependency path
(``AUTH_PROVIDER=entra``) against a mocked ``verify_entra_token`` (no live Entra) and a
mocked ``McpCedarService`` / ``McpServerRegistryService`` (no live AWS / boto3). Mirrors
the RBAC-test idiom of ``test_mcp_server_grants_routes.py``: reset cached modules, build a
minimal app with ONLY the mcp_cedar + mcp_servers routers, patch the lazy singletons to
return mocks.

The 4 routes (``routes/mcp_cedar.py``, prefix ``/mcp-servers``):
  - GET  ``/{id}/policies``            VIEWER   → CedarPolicySet
  - POST ``/{id}/policies``            OPERATOR → CedarPolicyRow (201)
  - DELETE ``/{id}/policies/{pid}``    OPERATOR → 204
  - PUT  ``/{id}/policy-enforcement``  OPERATOR → {enforcement_mode}

The cedar service methods are async, so the double is an ``AsyncMock``. ``McpCedarError``
maps to 422 (add) / 404 (delete-missing) / 502 (enforcement). The ``mode`` literal is
validated in the route → 400 BEFORE the service is called.

Tenant scoping (E34/T6) is pinned in the last section, in the idiom of
``test_registry_tenant_scoping.py``: a ``_FakeResolver`` seeded onto
``api.routes.users._tenant_resolver`` (the ONE resolver singleton ``mcp_cedar.get_tenant_ctx``
delegates to) drives the caller's ``TenantContext``, and a foreign tenant's gateway must 404
with a body byte-identical to a truly-missing id — on ALL FOUR routes, before the cedar
service is touched and before the 409 gateway-kind branch.
"""

from datetime import datetime, timezone
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
        "api.routes.mcp_servers",
        "api.routes.mcp_cedar",
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


def _make_mcp(**overrides):
    """Build a real provisioned GATEWAY MCP server for mock return values."""
    from models.mcp_server import Kind, LifecycleState, McpServer

    now = datetime.now(timezone.utc)
    base = dict(
        id="mcp-123",
        name="claims-mcp-de",
        description="Claims MCP server",
        kind=Kind.GATEWAY,
        lifecycle_state=LifecycleState.APPROVED,
        gateway_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/gw-abc",
        gateway_id="gw-abc",
        entra_sp_id="mcp-sp-obj-id",
        identity_status="provisioned",
        created_at=now,
        updated_at=now,
        created_by="maria.bauer@example.com",
        tenant_id="ten-1",
    )
    base.update(overrides)
    return McpServer(**base)


def _context(*, is_global=False, tenant_ids=()):
    from services.tenant_resolver import TenantContext

    return TenantContext(is_global=is_global, tenant_ids=frozenset(tenant_ids), tenants=())


class _FakeResolver:
    """Async ``resolve`` stub returning a fixed context regardless of principal."""

    def __init__(self, ctx):
        self._ctx = ctx

    async def resolve(self, principal):
        return self._ctx


def _build_client(mock_cedar=None, mock_registry=None, ctx=None):
    """Build a minimal app with the mcp_cedar + mcp_servers routers.

    Must be called AFTER the entra env fixture so the route modules import with the
    right settings. Patches the lazy singletons so no real AWS / boto3 is touched:
    ``mcp_cedar._cedar_svc`` (the cedar service) and ``mcp_servers._svc`` (the MCP
    registry, resolved by the route via ``mcp_servers.get_service()``).

    Also seeds the E24 tenant-resolver singleton ``api.routes.users._tenant_resolver`` —
    the ONE resolver accessor ``mcp_cedar.get_tenant_ctx`` delegates to. ``ctx`` defaults
    to an always-GLOBAL context so the tests that predate tenant scoping keep exercising
    the same paths (a global admin bypasses all filtering); left unseeded, every route
    would build a REAL ``TenantResolver`` and reach live AWS / Graph. Tenant scoping
    itself is pinned by the E34/T6 cases at the bottom of this file, which pass an
    explicit scoped ``ctx``.
    """
    import api.routes.mcp_cedar as mcp_cedar_module
    import api.routes.mcp_servers as mcp_servers_module
    import api.routes.users as users_module

    if mock_cedar is not None:
        mcp_cedar_module._cedar_svc = mock_cedar
    if mock_registry is not None:
        mcp_servers_module._svc = mock_registry
    users_module._tenant_resolver = _FakeResolver(ctx or _context(is_global=True))

    app = FastAPI()
    app.include_router(mcp_cedar_module.router, prefix="/api/v1")
    return TestClient(app), mcp_cedar_module


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


def _registry_returning(mcp):
    reg = MagicMock()
    reg.get.return_value = mcp
    return reg


def _cedar_double():
    """An AsyncMock cedar service with canned return values for the happy paths."""
    svc = MagicMock()
    svc.list_policies = AsyncMock(
        return_value={"enforcement_mode": "enforce", "engine_id": "pe-1", "policies": []}
    )
    svc.add_policy = AsyncMock(
        return_value={
            "policy_id": "pol-1",
            "user_oid": "user-oid-1",
            "user_label": "lars@example.com",
            "tool": "T___get_claim",
            "effect": "allow",
            "cedar_text": "// agp:v1 ...\npermit(...);",
        }
    )
    svc.delete_policy = AsyncMock(return_value=None)
    svc.set_enforcement = AsyncMock(
        return_value=MagicMock(cedar_enforcement_mode="log_only")
    )
    return svc


# --- GET /{id}/policies (list) -------------------------------------------

def test_viewer_can_list_policies(entra_settings):
    cedar = _cedar_double()
    client, _ = _build_client(mock_cedar=cedar, mock_registry=_registry_returning(_make_mcp()))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/mcp-servers/mcp-123/policies", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["enforcement_mode"] == "enforce"
    assert body["engine_id"] == "pe-1"
    assert body["policies"] == []


# --- POST /{id}/policies (add) -------------------------------------------

def test_viewer_cannot_add_policy(entra_settings):
    cedar = _cedar_double()
    client, _ = _build_client(mock_cedar=cedar, mock_registry=_registry_returning(_make_mcp()))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/mcp-servers/mcp-123/policies",
            json={"principal_oid": "o", "principal_label": "l"},
            headers=_headers(),
        )

    assert resp.status_code == 403
    cedar.add_policy.assert_not_called()


def test_operator_can_add_policy(entra_settings):
    cedar = _cedar_double()
    client, _ = _build_client(mock_cedar=cedar, mock_registry=_registry_returning(_make_mcp()))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers/mcp-123/policies",
            json={
                "principal_oid": "user-oid-1",
                "principal_label": "lars@example.com",
                "tool_name": "T___get_claim",
            },
            headers=_headers(),
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["policy_id"] == "pol-1"
    assert body["tool"] == "T___get_claim"
    cedar.add_policy.assert_awaited_once()
    _, kwargs = cedar.add_policy.call_args
    assert kwargs["principal_oid"] == "user-oid-1"
    assert kwargs["principal_label"] == "lars@example.com"
    assert kwargs["tool_name"] == "T___get_claim"


def test_add_policy_all_tools_passes_none_tool(entra_settings):
    cedar = _cedar_double()
    client, _ = _build_client(mock_cedar=cedar, mock_registry=_registry_returning(_make_mcp()))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers/mcp-123/policies",
            json={
                "principal_oid": "user-oid-1",
                "principal_label": "lars@example.com",
                "tool_name": "ignored",
                "all_tools": True,
            },
            headers=_headers(),
        )

    assert resp.status_code == 201
    _, kwargs = cedar.add_policy.call_args
    assert kwargs["tool_name"] is None


# --- DELETE /{id}/policies/{pid} -----------------------------------------

def test_operator_can_delete_policy(entra_settings):
    cedar = _cedar_double()
    client, _ = _build_client(mock_cedar=cedar, mock_registry=_registry_returning(_make_mcp()))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete(
            "/api/v1/mcp-servers/mcp-123/policies/pol-1", headers=_headers()
        )

    assert resp.status_code == 204
    cedar.delete_policy.assert_awaited_once()
    args, _ = cedar.delete_policy.call_args
    assert "pol-1" in args


def test_viewer_cannot_delete_policy(entra_settings):
    cedar = _cedar_double()
    client, _ = _build_client(mock_cedar=cedar, mock_registry=_registry_returning(_make_mcp()))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.delete(
            "/api/v1/mcp-servers/mcp-123/policies/pol-1", headers=_headers()
        )

    assert resp.status_code == 403
    cedar.delete_policy.assert_not_called()


# --- PUT /{id}/policy-enforcement ----------------------------------------

def test_operator_can_set_enforcement(entra_settings):
    cedar = _cedar_double()
    client, _ = _build_client(mock_cedar=cedar, mock_registry=_registry_returning(_make_mcp()))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.put(
            "/api/v1/mcp-servers/mcp-123/policy-enforcement",
            json={"mode": "log_only"},
            headers=_headers(),
        )

    assert resp.status_code == 200
    assert resp.json() == {"enforcement_mode": "log_only"}
    cedar.set_enforcement.assert_awaited_once()
    args, _ = cedar.set_enforcement.call_args
    assert "log_only" in args


def test_set_enforcement_bad_mode_returns_400(entra_settings):
    cedar = _cedar_double()
    client, _ = _build_client(mock_cedar=cedar, mock_registry=_registry_returning(_make_mcp()))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.put(
            "/api/v1/mcp-servers/mcp-123/policy-enforcement",
            json={"mode": "bogus"},
            headers=_headers(),
        )

    assert resp.status_code == 400
    cedar.set_enforcement.assert_not_called()


# --- guards: gateway-kind / missing / unprovisioned ----------------------

def test_non_gateway_returns_409(entra_settings):
    from models.mcp_server import Kind

    mcp = _make_mcp(kind=Kind.STANDARD)
    cedar = _cedar_double()
    client, _ = _build_client(mock_cedar=cedar, mock_registry=_registry_returning(mcp))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        get_resp = client.get("/api/v1/mcp-servers/mcp-123/policies", headers=_headers())
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        post_resp = client.post(
            "/api/v1/mcp-servers/mcp-123/policies",
            json={"principal_oid": "o", "principal_label": "l"},
            headers=_headers(),
        )

    assert get_resp.status_code == 409
    assert post_resp.status_code == 409


def test_missing_mcp_returns_404(entra_settings):
    cedar = _cedar_double()
    client, _ = _build_client(mock_cedar=cedar, mock_registry=_registry_returning(None))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/mcp-servers/mcp-missing/policies", headers=_headers())

    assert resp.status_code == 404


def test_add_policy_unprovisioned_gateway_returns_409(entra_settings):
    mcp = _make_mcp(gateway_id=None)
    cedar = _cedar_double()
    client, _ = _build_client(mock_cedar=cedar, mock_registry=_registry_returning(mcp))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers/mcp-123/policies",
            json={"principal_oid": "o", "principal_label": "l"},
            headers=_headers(),
        )

    assert resp.status_code == 409
    cedar.add_policy.assert_not_called()


# --- error mapping: McpCedarError ----------------------------------------

def test_add_policy_cedar_error_returns_422(entra_settings):
    from services.mcp_cedar_service import McpCedarError

    cedar = _cedar_double()
    cedar.add_policy = AsyncMock(side_effect=McpCedarError("invalid cedar"))
    client, _ = _build_client(mock_cedar=cedar, mock_registry=_registry_returning(_make_mcp()))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers/mcp-123/policies",
            json={"principal_oid": "o", "principal_label": "l"},
            headers=_headers(),
        )

    assert resp.status_code == 422


def test_delete_missing_policy_returns_404(entra_settings):
    from services.mcp_cedar_service import McpCedarError

    cedar = _cedar_double()
    cedar.delete_policy = AsyncMock(side_effect=McpCedarError("not found"))
    client, _ = _build_client(mock_cedar=cedar, mock_registry=_registry_returning(_make_mcp()))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete(
            "/api/v1/mcp-servers/mcp-123/policies/pol-missing", headers=_headers()
        )

    assert resp.status_code == 404


# --- E10: effect + conditions threading (add) ----------------------------

def test_add_policy_passes_effect_and_conditions_to_service(entra_settings):
    """A conditioned deny POST → 201; effect + conditions reach the service as plain
    dicts (the route is a thin pass-through; the service validates)."""
    cedar = _cedar_double()
    cedar.add_policy = AsyncMock(
        return_value={
            "policy_id": "pol-2",
            "user_oid": "o",
            "user_label": "l",
            "tool": "transfer",
            "effect": "deny",
            "conditions": [{"param": "amount", "op": ">", "value": "10000", "type": "number"}],
            "managed": True,
            "cedar_text": "// agp:v2 effect=forbid ...\nforbid(...);",
        }
    )
    client, _ = _build_client(mock_cedar=cedar, mock_registry=_registry_returning(_make_mcp()))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers/mcp-123/policies",
            json={
                "principal_oid": "o",
                "principal_label": "l",
                "tool_name": "transfer",
                "effect": "deny",
                "conditions": [
                    {"param": "amount", "op": ">", "value": "10000", "type": "number"}
                ],
            },
            headers=_headers(),
        )

    assert resp.status_code == 201
    cedar.add_policy.assert_awaited_once()
    _, kwargs = cedar.add_policy.call_args
    assert kwargs["effect"] == "deny"
    assert kwargs["conditions"] == [
        {"param": "amount", "op": ">", "value": "10000", "type": "number"}
    ]
    assert kwargs["principal_oid"] == "o"
    assert kwargs["tool_name"] == "transfer"


def test_add_policy_all_users_deny_null_principal(entra_settings):
    """No principal_oid in the body (all-users deny) → principal_oid=None reaches the
    service; CedarPolicyRow validates with a null user_oid."""
    cedar = _cedar_double()
    cedar.add_policy = AsyncMock(
        return_value={
            "policy_id": "pol-3",
            "user_oid": None,
            "user_label": "Everyone",
            "tool": "transfer",
            "effect": "deny",
            "conditions": [{"param": "amount", "op": ">", "value": "10000", "type": "number"}],
            "managed": True,
            "cedar_text": "// agp:v2 effect=forbid oid=- ...\nforbid(...);",
        }
    )
    client, _ = _build_client(mock_cedar=cedar, mock_registry=_registry_returning(_make_mcp()))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers/mcp-123/policies",
            json={
                "principal_label": "Everyone",
                "tool_name": "transfer",
                "effect": "deny",
                "conditions": [
                    {"param": "amount", "op": ">", "value": "10000", "type": "number"}
                ],
            },
            headers=_headers(),
        )

    assert resp.status_code == 201
    cedar.add_policy.assert_awaited_once()
    _, kwargs = cedar.add_policy.call_args
    assert kwargs["principal_oid"] is None
    body = resp.json()
    assert body["user_oid"] is None
    assert body["user_label"] == "Everyone"


def test_add_policy_defaults_effect_allow_empty_conditions(entra_settings):
    """An E8-style body (no effect/conditions) → the route defaults effect="allow" and
    conditions=[] when threading to the service."""
    cedar = _cedar_double()
    client, _ = _build_client(mock_cedar=cedar, mock_registry=_registry_returning(_make_mcp()))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers/mcp-123/policies",
            json={
                "principal_oid": "user-oid-1",
                "principal_label": "lars@example.com",
                "tool_name": "T___get_claim",
            },
            headers=_headers(),
        )

    assert resp.status_code == 201
    cedar.add_policy.assert_awaited_once()
    _, kwargs = cedar.add_policy.call_args
    assert kwargs["effect"] == "allow"
    assert kwargs["conditions"] == []


def test_list_returns_effect_and_conditions(entra_settings):
    """A policy set whose row carries effect/conditions/managed → the JSON row echoes
    them (CedarPolicyRow serializes the new fields)."""
    cedar = _cedar_double()
    cedar.list_policies = AsyncMock(
        return_value={
            "enforcement_mode": "enforce",
            "engine_id": "pe-1",
            "policies": [
                {
                    "policy_id": "pol-4",
                    "user_oid": None,
                    "user_label": None,
                    "tool": None,
                    "effect": "deny",
                    "conditions": [
                        {"param": "amount", "op": ">", "value": "10000", "type": "number"}
                    ],
                    "managed": False,
                    "cedar_text": "forbid(principal, action, resource);",
                }
            ],
        }
    )
    client, _ = _build_client(mock_cedar=cedar, mock_registry=_registry_returning(_make_mcp()))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/mcp-servers/mcp-123/policies", headers=_headers())

    assert resp.status_code == 200
    row = resp.json()["policies"][0]
    assert row["effect"] == "deny"
    assert row["conditions"] == [
        {"param": "amount", "op": ">", "value": "10000", "type": "number"}
    ]
    assert row["managed"] is False


def test_add_policy_service_validation_error_returns_422(entra_settings):
    """The service validates (allow-without-user etc.) and raises McpCedarError → the
    existing 422 mapping (the route adds no validation of its own)."""
    from services.mcp_cedar_service import McpCedarError

    cedar = _cedar_double()
    cedar.add_policy = AsyncMock(side_effect=McpCedarError("an allow policy requires a user"))
    client, _ = _build_client(mock_cedar=cedar, mock_registry=_registry_returning(_make_mcp()))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/mcp-servers/mcp-123/policies",
            json={"principal_label": "l", "tool_name": "transfer", "effect": "allow"},
            headers=_headers(),
        )

    assert resp.status_code == 422


# --- E34/T6: tenant scoping of all four routes ---------------------------
#
# Before this task none of the four routes applied the E24 tenant invariant: a
# tenant-A OPERATOR could PUT .../policy-enforcement on tenant B's gateway. Each case
# below runs against ALL FOUR routes via the table, so a future route added without a
# tenant gate fails here.


def _call_list(client, mcp_id):
    return client.get(f"/api/v1/mcp-servers/{mcp_id}/policies", headers=_headers())


def _call_add(client, mcp_id):
    return client.post(
        f"/api/v1/mcp-servers/{mcp_id}/policies",
        json={"principal_oid": "user-oid-1", "principal_label": "lars@example.com"},
        headers=_headers(),
    )


def _call_delete(client, mcp_id):
    return client.delete(
        f"/api/v1/mcp-servers/{mcp_id}/policies/pol-1", headers=_headers()
    )


def _call_set_enforcement(client, mcp_id):
    return client.put(
        f"/api/v1/mcp-servers/{mcp_id}/policy-enforcement",
        json={"mode": "log_only"},
        headers=_headers(),
    )


# (route caller, minimum role, cedar-service method, happy-path status)
_CEDAR_ROUTES = [
    pytest.param(_call_list, "viewer", "list_policies", 200, id="list"),
    pytest.param(_call_add, "operator", "add_policy", 201, id="add"),
    pytest.param(_call_delete, "operator", "delete_policy", 204, id="delete"),
    pytest.param(
        _call_set_enforcement, "operator", "set_enforcement", 200, id="enforcement"
    ),
]
# The three mutating routes (everything but the read) — they gate with for_write=True.
_CEDAR_MUTATING_ROUTES = _CEDAR_ROUTES[1:]


@pytest.mark.parametrize("call, role, svc_method, ok_status", _CEDAR_ROUTES)
def test_cedar_route_foreign_tenant_404_matches_missing_body(
    entra_settings, call, role, svc_method, ok_status
):
    """A tenant-A caller on tenant-B's gateway → 404 whose FULL body is byte-identical to
    a truly-missing id's 404 (E24's 404-not-403 contract: a foreign resource must look
    absent), and the cedar service is never reached — the gate precedes every side
    effect."""
    cedar = _cedar_double()
    registry = MagicMock()
    registry.get.side_effect = lambda mcp_id: (
        _make_mcp(id="foreign", tenant_id="ten-2") if mcp_id == "foreign" else None
    )
    client, _ = _build_client(
        mock_cedar=cedar,
        mock_registry=registry,
        ctx=_context(tenant_ids=["ten-1"]),
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for(role)):
        foreign_resp = call(client, "foreign")
        missing_resp = call(client, "truly-missing")

    assert foreign_resp.status_code == 404
    assert missing_resp.status_code == 404
    assert foreign_resp.json() == missing_resp.json()
    assert foreign_resp.json()["detail"] == "MCP server not found"
    getattr(cedar, svc_method).assert_not_called()


@pytest.mark.parametrize("call, role, svc_method, ok_status", _CEDAR_ROUTES)
def test_cedar_route_own_tenant_still_succeeds(
    entra_settings, call, role, svc_method, ok_status
):
    """No happy-path regression: a caller who is a member of the gateway's tenant still
    gets through to the cedar service."""
    cedar = _cedar_double()
    client, _ = _build_client(
        mock_cedar=cedar,
        mock_registry=_registry_returning(_make_mcp(tenant_id="ten-1")),
        ctx=_context(tenant_ids=["ten-1"]),
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for(role)):
        resp = call(client, "mcp-123")

    assert resp.status_code == ok_status
    getattr(cedar, svc_method).assert_awaited_once()


@pytest.mark.parametrize("call, role, svc_method, ok_status", _CEDAR_ROUTES)
def test_cedar_route_global_admin_succeeds_across_tenants(
    entra_settings, call, role, svc_method, ok_status
):
    """A global (unscoped) caller — ``is_global=True``, i.e. Role.ADMIN — reaches ANOTHER
    tenant's gateway: the E24 admin bypass survives the new gate."""
    cedar = _cedar_double()
    client, _ = _build_client(
        mock_cedar=cedar,
        mock_registry=_registry_returning(_make_mcp(tenant_id="ten-2")),
        ctx=_context(is_global=True),
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = call(client, "mcp-123")

    assert resp.status_code == ok_status
    getattr(cedar, svc_method).assert_awaited_once()


@pytest.mark.parametrize("call, role, svc_method, ok_status", _CEDAR_ROUTES)
def test_cedar_route_foreign_non_gateway_404_not_409(
    entra_settings, call, role, svc_method, ok_status
):
    """Visibility precedes the gateway-kind check: a foreign STANDARD MCP is 404, NOT the
    409 a same-tenant STANDARD MCP gets (``test_non_gateway_returns_409``) — the 409 would
    itself be an existence oracle for another tenant's registry."""
    from models.mcp_server import Kind

    cedar = _cedar_double()
    client, _ = _build_client(
        mock_cedar=cedar,
        mock_registry=_registry_returning(_make_mcp(kind=Kind.STANDARD, tenant_id="ten-2")),
        ctx=_context(tenant_ids=["ten-1"]),
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for(role)):
        resp = call(client, "mcp-123")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "MCP server not found"
    getattr(cedar, svc_method).assert_not_called()


def test_cedar_list_foreign_shared_gateway_readable(entra_settings):
    """``shared`` grants cross-tenant READ visibility, so the read route gates as a read:
    another tenant's SHARED gateway's policy set is listable (mcp_servers' Finding-1
    policy, inherited by reusing ``_load_visible_mcp_server``)."""
    cedar = _cedar_double()
    client, _ = _build_client(
        mock_cedar=cedar,
        mock_registry=_registry_returning(_make_mcp(tenant_id="ten-2", shared=True)),
        ctx=_context(tenant_ids=["ten-1"]),
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = _call_list(client, "mcp-123")

    assert resp.status_code == 200
    cedar.list_policies.assert_awaited_once()


@pytest.mark.parametrize("call, role, svc_method, ok_status", _CEDAR_MUTATING_ROUTES)
def test_cedar_mutation_on_foreign_shared_gateway_404(
    entra_settings, call, role, svc_method, ok_status
):
    """...but ``shared`` is READ-only cross-tenant: the three mutating routes pass
    ``for_write=True``, so a tenant-A OPERATOR cannot edit policies (or flip enforcement)
    on tenant B's shared gateway."""
    cedar = _cedar_double()
    client, _ = _build_client(
        mock_cedar=cedar,
        mock_registry=_registry_returning(_make_mcp(tenant_id="ten-2", shared=True)),
        ctx=_context(tenant_ids=["ten-1"]),
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for(role)):
        resp = call(client, "mcp-123")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "MCP server not found"
    getattr(cedar, svc_method).assert_not_called()


# --- wiring guard --------------------------------------------------------

def test_routes_registered_in_app(entra_settings):
    """Import main; assert both Cedar route paths exist in app.routes (guards the
    both-blocks wiring in main.py)."""
    import main

    from conftest import app_route_paths

    paths = app_route_paths(main.app)
    assert "/api/v1/mcp-servers/{mcp_id}/policies" in paths
    assert "/api/v1/mcp-servers/{mcp_id}/policy-enforcement" in paths
