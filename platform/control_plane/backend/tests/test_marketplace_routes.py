"""Marketplace route tests (Epic 9, Task T5).

Exercise the REAL `require_role` + `current_principal` dependency path
(`AUTH_PROVIDER=entra`) against a mocked `verify_entra_token` (no live Entra) and a
mocked `MarketplaceService` singleton (no live AWS / Graph / registry). Mirrors the
RBAC-test idiom of `test_mcp_server_grants_routes.py`: reset cached modules, build a
minimal app with ONLY the marketplace router, patch the lazy `_svc` singleton.

RBAC: browse/subscribe/list-own = VIEWER; admin list-all/approve/reject/retry/
metrics/set-listing = ADMIN. Identity is threaded from the validated principal
(`requester_oid`/`decided_by`/`caller_oid`), NEVER from the body. The
`MarketplaceError.kind`→HTTP mapping uses FIXED `detail=` literals (not `str(err)`).
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
        "api.routes.marketplace",
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


def _now():
    return datetime.now(timezone.utc)


def _make_product_card(**overrides):
    from models.marketplace import ProductCard, ProductType

    base = dict(
        product_type=ProductType.AGENT,
        product_id="bp-fnol",
        name="FNOL Agent",
        pitch="Start claims.",
        capabilities=["start claim"],
    )
    base.update(overrides)
    return ProductCard(**base)


def _make_subscription(**overrides):
    from models.marketplace import ProductType, Subscription, SubscriptionStatus

    now = _now()
    base = dict(
        id="mkt-abc1234567",
        product_type=ProductType.AGENT,
        product_id="bp-fnol",
        product_name="FNOL Agent",
        requester_oid="viewer-oid",
        requester_email="viewer.user@example.com",
        status=SubscriptionStatus.PENDING,
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return Subscription(**base)


def _make_metrics(**overrides):
    from models.marketplace import MarketplaceMetrics

    base = dict(
        total=0,
        pending=0,
        approved=0,
        rejected=0,
        failed=0,
        approval_rate=0.0,
        by_type={"agent": 0, "mcp": 0},
        top_products=[],
    )
    base.update(overrides)
    return MarketplaceMetrics(**base)


def _make_listing(**overrides):
    from models.marketplace import Listing, ProductType

    base = dict(product_type=ProductType.MCP, product_id="m1", available=True, auto_approve=True)
    base.update(overrides)
    return Listing(**base)


def _build_client(mock_svc, ctx=None):
    """Build a minimal app with ONLY the marketplace router, with `_svc` patched.

    Must be called AFTER the entra env fixture so the route module imports with the
    right settings. The marketplace routes resolve the service via `get_marketplace_service()`
    which returns the lazy `_svc` singleton — we set it to a mock directly so no real
    AWS / Graph / registry is touched.

    E24/T8: also seeds the ONE tenant-resolver singleton
    (``api.routes.users._tenant_resolver``) with a stub returning ``ctx`` (default:
    a global-admin context, preserving pre-E24 behavior for the existing tests) —
    the mcp-products route now resolves a TenantContext per request (the Task 5
    fixup pattern).
    """
    import api.routes.marketplace as marketplace_module
    import api.routes.users as users_module
    from services.tenant_resolver import TenantContext

    marketplace_module._svc = mock_svc

    if ctx is None:
        ctx = TenantContext(is_global=True, tenant_ids=frozenset(), tenants=())

    class _FakeResolver:
        async def resolve(self, principal):
            return ctx

    users_module._tenant_resolver = _FakeResolver()

    app = FastAPI()
    app.include_router(marketplace_module.router, prefix="/api/v1")
    return TestClient(app), marketplace_module


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


# --- GET /marketplace/agent-products + /mcp-products (VIEWER) -----------------

def test_viewer_can_list_agent_products(entra_settings):
    mock_svc = MagicMock()
    mock_svc.list_agent_products.return_value = [_make_product_card()]
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/marketplace/agent-products", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert body[0]["product_id"] == "bp-fnol"
    mock_svc.list_agent_products.assert_called_once_with(caller_oid="viewer-oid")


def test_viewer_can_list_mcp_products(entra_settings):
    from models.marketplace import ProductType

    mock_svc = MagicMock()
    mock_svc.list_mcp_products.return_value = [
        _make_product_card(product_type=ProductType.MCP, product_id="m1", name="Claims MCP", kind="gateway")
    ]
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/marketplace/mcp-products", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert body[0]["product_id"] == "m1"
    # E24/T8: the route threads the resolved TenantContext into the catalog call.
    mock_svc.list_mcp_products.assert_called_once()
    kwargs = mock_svc.list_mcp_products.call_args.kwargs
    assert kwargs["caller_oid"] == "viewer-oid"
    assert kwargs["ctx"] is not None


def test_mcp_products_threads_scoped_tenant_ctx(entra_settings):
    """E24/T8: the route resolves the caller's TenantContext (via the ONE resolver
    singleton) and passes it to list_mcp_products — the service does the
    visible-or-published filtering."""
    from services.tenant_resolver import TenantContext

    scoped = TenantContext(is_global=False, tenant_ids=frozenset({"ten-1"}), tenants=())
    mock_svc = MagicMock()
    mock_svc.list_mcp_products.return_value = []
    client, _ = _build_client(mock_svc, ctx=scoped)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/marketplace/mcp-products", headers=_headers())

    assert resp.status_code == 200
    kwargs = mock_svc.list_mcp_products.call_args.kwargs
    assert kwargs["ctx"] is scoped


def test_mcp_product_card_serializes_tenant_badge_fields(entra_settings):
    """The ProductCard response carries the E24 badge fields end-to-end."""
    from models.marketplace import ProductType

    mock_svc = MagicMock()
    mock_svc.list_mcp_products.return_value = [
        _make_product_card(
            product_type=ProductType.MCP, product_id="m-pub", name="Foreign Published",
            kind="gateway", tenant_id="ten-2", tenant_name="Marketing EU",
            published=True, shared=False,
        )
    ]
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/marketplace/mcp-products", headers=_headers())

    assert resp.status_code == 200
    card = resp.json()[0]
    assert card["tenant_id"] == "ten-2"
    assert card["tenant_name"] == "Marketing EU"
    assert card["published"] is True
    assert card["shared"] is False


# --- POST /marketplace/subscriptions (VIEWER) --------------------------------

def test_viewer_can_subscribe(entra_settings):
    mock_svc = MagicMock()
    mock_svc.create_subscription = AsyncMock(return_value=_make_subscription())
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/marketplace/subscriptions",
            json={"product_type": "agent", "product_id": "bp-fnol", "requester_oid": "SPOOFED"},
            headers=_headers(),
        )

    assert resp.status_code == 201
    mock_svc.create_subscription.assert_awaited_once()
    kwargs = mock_svc.create_subscription.await_args.kwargs
    # Identity comes from the TOKEN, not the body.
    assert kwargs["requester_oid"] == "viewer-oid"
    assert kwargs["requester_email"] == "viewer.user@example.com"
    assert kwargs["product_type"] == "agent"
    assert kwargs["product_id"] == "bp-fnol"


def test_subscribe_mcp_without_agent_id_422(entra_settings):
    mock_svc = MagicMock()
    mock_svc.create_subscription = AsyncMock(return_value=_make_subscription())
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/marketplace/subscriptions",
            json={"product_type": "mcp", "product_id": "m1"},
            headers=_headers(),
        )

    assert resp.status_code == 422
    mock_svc.create_subscription.assert_not_called()


# --- GET /marketplace/eligible-agents (VIEWER) — F1 --------------------------

def _claims_for_with_groups(role: str, groups: list[str]):
    claims = _claims_for(role)
    claims["groups"] = groups
    return claims


def _agent_ns(**over):
    import types

    base = dict(
        id="agent-1", name="Provisioned Agent",
        identity_status="provisioned", entra_sp_id="sp-a", sponsor_oid="viewer-oid",
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def test_eligible_agents_endpoint_viewer_ok(entra_settings):
    mock_svc = MagicMock()
    mock_svc.eligible_agents = AsyncMock(return_value=[_agent_ns()])
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/marketplace/eligible-agents", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert body[0]["name"] == "Provisioned Agent"
    mock_svc.eligible_agents.assert_awaited_once()
    kwargs = mock_svc.eligible_agents.await_args.kwargs
    # caller_oid from the token; no groups claim → []
    assert kwargs["caller_oid"] == "viewer-oid"
    assert kwargs["caller_group_ids"] == []


def test_eligible_agents_uses_group_claim(entra_settings):
    mock_svc = MagicMock()
    mock_svc.eligible_agents = AsyncMock(
        return_value=[_agent_ns(id="agent-g", name="Granted Agent", entra_sp_id="sp-g")]
    )
    client, _ = _build_client(mock_svc)

    claims = _claims_for_with_groups("viewer", ["grp-claims", "grp-marketing"])
    with patch("core.security_entra.verify_entra_token", return_value=claims):
        resp = client.get("/api/v1/marketplace/eligible-agents", headers=_headers())

    assert resp.status_code == 200
    kwargs = mock_svc.eligible_agents.await_args.kwargs
    # The token's `groups` claim is passed straight through (no Graph fallback).
    assert kwargs["caller_oid"] == "viewer-oid"
    assert kwargs["caller_group_ids"] == ["grp-claims", "grp-marketing"]


def test_subscribe_mcp_ineligible_agent_409(entra_settings):
    from services.marketplace_service import MarketplaceError

    mock_svc = MagicMock()
    mock_svc.create_subscription = AsyncMock(
        side_effect=MarketplaceError("you may only subscribe on behalf of …", kind="conflict")
    )
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/marketplace/subscriptions",
            json={"product_type": "mcp", "product_id": "m1", "agent_id": "agent-x"},
            headers=_headers(),
        )

    assert resp.status_code == 409
    mock_svc.create_subscription.assert_awaited_once()
    # caller_group_ids threaded into the subscribe call (no groups claim → []).
    kwargs = mock_svc.create_subscription.await_args.kwargs
    assert kwargs["caller_group_ids"] == []
    assert kwargs["requester_oid"] == "viewer-oid"


# --- GET /marketplace/subscriptions (VIEWER, caller-scoped) ------------------

def test_viewer_can_list_own_subscriptions(entra_settings):
    mock_svc = MagicMock()
    mock_svc.list_subscriptions.return_value = [_make_subscription()]
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/marketplace/subscriptions", headers=_headers())

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    mock_svc.list_subscriptions.assert_called_once()
    kwargs = mock_svc.list_subscriptions.call_args.kwargs
    assert kwargs["caller_oid"] == "viewer-oid"


# --- GET /marketplace/admin/subscriptions (ADMIN) ---------------------------

def test_viewer_cannot_list_admin_subscriptions(entra_settings):
    mock_svc = MagicMock()
    mock_svc.list_subscriptions.return_value = []
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/marketplace/admin/subscriptions", headers=_headers())

    assert resp.status_code == 403
    mock_svc.list_subscriptions.assert_not_called()


def test_admin_can_list_admin_subscriptions(entra_settings):
    mock_svc = MagicMock()
    mock_svc.list_subscriptions.return_value = [_make_subscription()]
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/marketplace/admin/subscriptions", headers=_headers())

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    mock_svc.list_subscriptions.assert_called_once()
    # Admin list-all → no caller scoping.
    kwargs = mock_svc.list_subscriptions.call_args.kwargs
    assert kwargs.get("caller_oid") is None


# --- ADMIN write routes forbidden to VIEWER ---------------------------------

def test_viewer_cannot_approve(entra_settings):
    mock_svc = MagicMock()
    mock_svc.approve = AsyncMock(return_value=_make_subscription())
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post("/api/v1/marketplace/subscriptions/mkt-abc1234567/approve", headers=_headers())

    assert resp.status_code == 403
    mock_svc.approve.assert_not_called()


def test_viewer_cannot_reject(entra_settings):
    mock_svc = MagicMock()
    mock_svc.reject.return_value = _make_subscription()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/marketplace/subscriptions/mkt-abc1234567/reject",
            json={"reason": "x"},
            headers=_headers(),
        )

    assert resp.status_code == 403
    mock_svc.reject.assert_not_called()


def test_viewer_cannot_retry(entra_settings):
    mock_svc = MagicMock()
    mock_svc.retry_grant = AsyncMock(return_value=_make_subscription())
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post("/api/v1/marketplace/subscriptions/mkt-abc1234567/retry", headers=_headers())

    assert resp.status_code == 403
    mock_svc.retry_grant.assert_not_called()


# --- ADMIN approve / reject / retry (happy paths) ---------------------------

def test_admin_can_approve(entra_settings):
    from models.marketplace import SubscriptionStatus

    mock_svc = MagicMock()
    mock_svc.approve = AsyncMock(return_value=_make_subscription(status=SubscriptionStatus.APPROVED))
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/marketplace/subscriptions/mkt-abc1234567/approve", headers=_headers())

    assert resp.status_code == 200
    mock_svc.approve.assert_awaited_once()
    call = mock_svc.approve.await_args
    assert call.args[0] == "mkt-abc1234567"
    # decided_by comes from the token (oid or email).
    assert call.kwargs["decided_by"] == "admin-oid"


def test_admin_can_reject_with_reason(entra_settings):
    from models.marketplace import SubscriptionStatus

    mock_svc = MagicMock()
    mock_svc.reject.return_value = _make_subscription(status=SubscriptionStatus.REJECTED)
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/marketplace/subscriptions/mkt-abc1234567/reject",
            json={"reason": "x"},
            headers=_headers(),
        )

    assert resp.status_code == 200
    mock_svc.reject.assert_called_once()
    call = mock_svc.reject.call_args
    assert call.args[0] == "mkt-abc1234567"
    assert call.kwargs["reason"] == "x"
    assert call.kwargs["decided_by"] == "admin-oid"


def test_admin_can_retry(entra_settings):
    from models.marketplace import SubscriptionStatus

    mock_svc = MagicMock()
    mock_svc.retry_grant = AsyncMock(return_value=_make_subscription(status=SubscriptionStatus.APPROVED))
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post("/api/v1/marketplace/subscriptions/mkt-abc1234567/retry", headers=_headers())

    assert resp.status_code == 200
    mock_svc.retry_grant.assert_awaited_once()
    call = mock_svc.retry_grant.await_args
    assert call.args[0] == "mkt-abc1234567"
    assert call.kwargs["decided_by"] == "admin-oid"


# --- POST /marketplace/subscriptions/{id}/revoke (ADMIN) — E9R --------------

def test_viewer_cannot_revoke(entra_settings):
    mock_svc = MagicMock()
    mock_svc.revoke_subscription = AsyncMock(return_value=_make_subscription())
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/marketplace/subscriptions/mkt-abc1234567/revoke",
            json={"reason": "offboarding"},
            headers=_headers(),
        )

    assert resp.status_code == 403
    mock_svc.revoke_subscription.assert_not_called()


def test_admin_can_revoke(entra_settings):
    from models.marketplace import SubscriptionStatus

    mock_svc = MagicMock()
    mock_svc.revoke_subscription = AsyncMock(
        return_value=_make_subscription(status=SubscriptionStatus.REVOKED)
    )
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/marketplace/subscriptions/mkt-abc1234567/revoke",
            json={"reason": "offboarding"},
            headers=_headers(),
        )

    assert resp.status_code == 200
    mock_svc.revoke_subscription.assert_awaited_once()
    call = mock_svc.revoke_subscription.await_args
    assert call.args[0] == "mkt-abc1234567"
    # decided_by comes from the TOKEN, not the body; reason from the body.
    assert call.kwargs["decided_by"] == "admin-oid"
    assert call.kwargs["reason"] == "offboarding"


def test_admin_can_revoke_empty_body(entra_settings):
    from models.marketplace import SubscriptionStatus

    mock_svc = MagicMock()
    mock_svc.revoke_subscription = AsyncMock(
        return_value=_make_subscription(status=SubscriptionStatus.REVOKED)
    )
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/marketplace/subscriptions/mkt-abc1234567/revoke",
            json={},
            headers=_headers(),
        )

    assert resp.status_code == 200
    mock_svc.revoke_subscription.assert_awaited_once()
    call = mock_svc.revoke_subscription.await_args
    assert call.args[0] == "mkt-abc1234567"
    assert call.kwargs["decided_by"] == "admin-oid"
    # Empty body → reason None (same as reject).
    assert call.kwargs["reason"] is None


def test_revoke_error_mapping(entra_settings):
    from services.marketplace_service import MarketplaceError

    secret_marker = "LEAKED-MARKETPLACE-INTERNAL-DETAIL"
    cases = [
        ("not_found", 404, "Marketplace product or subscription not found"),
        ("conflict", 409, "Marketplace operation conflicts with the current state"),
        ("grant_failed", 502, "Failed to apply the agent grant; see backend logs and retry"),
    ]
    for kind, expected_status, expected_detail in cases:
        mock_svc = MagicMock()
        mock_svc.revoke_subscription = AsyncMock(
            side_effect=MarketplaceError(secret_marker, kind=kind)
        )
        client, _ = _build_client(mock_svc)

        with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
            resp = client.post(
                "/api/v1/marketplace/subscriptions/mkt-abc1234567/revoke",
                json={"reason": "x"},
                headers=_headers(),
            )

        assert resp.status_code == expected_status, f"kind={kind}"
        # FIXED detail literal — a constant, never str(err); the error message must NOT leak.
        assert resp.json()["detail"] == expected_detail, f"kind={kind}"
        assert secret_marker not in resp.text, f"kind={kind} leaked the error message"


# --- GET /marketplace/admin/metrics (ADMIN) ---------------------------------

def test_admin_can_get_metrics(entra_settings):
    mock_svc = MagicMock()
    mock_svc.metrics.return_value = _make_metrics(total=3, pending=1, approved=2, approval_rate=1.0)
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/marketplace/admin/metrics", headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["total"] == 3
    mock_svc.metrics.assert_called_once()


def test_viewer_cannot_get_metrics(entra_settings):
    mock_svc = MagicMock()
    mock_svc.metrics.return_value = _make_metrics()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/marketplace/admin/metrics", headers=_headers())

    assert resp.status_code == 403
    mock_svc.metrics.assert_not_called()


# --- PUT /marketplace/listings/{product_type}/{product_id} (ADMIN) ----------

def test_admin_can_set_listing(entra_settings):
    mock_svc = MagicMock()
    mock_svc.set_listing.return_value = _make_listing()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.put(
            "/api/v1/marketplace/listings/mcp/m1",
            json={"auto_approve": True},
            headers=_headers(),
        )

    assert resp.status_code == 200
    mock_svc.set_listing.assert_called_once()
    kwargs = mock_svc.set_listing.call_args.kwargs
    assert kwargs["product_type"] == "mcp"
    assert kwargs["product_id"] == "m1"
    assert kwargs["auto_approve"] is True


def test_viewer_cannot_set_listing(entra_settings):
    mock_svc = MagicMock()
    mock_svc.set_listing.return_value = _make_listing()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.put(
            "/api/v1/marketplace/listings/mcp/m1",
            json={"auto_approve": True},
            headers=_headers(),
        )

    assert resp.status_code == 403
    mock_svc.set_listing.assert_not_called()


# --- Publish requests (E33) --------------------------------------------------
#
# Same idiom as the listing pair above: every route gets the positive half (status +
# `call_args.kwargs` asserted) AND the negative-RBAC twin (403 + `assert_not_called()`).
# RBAC: create + read-own-product = OPERATOR (a publisher acts on their own product);
# list-all / approve / reject / unpublish = ADMIN (the attestation is approved, not
# self-asserted). `decided_by` ALWAYS comes from the validated principal, never the body.
#
# Amendment 1 / contract C9 generalized the flow from agents to BOTH product types: the
# body carries `product_type` + `product_id`, and the two agent-only paths were replaced by
# `(product_type, product_id)`-keyed ones. The route passes the plain enum VALUE (the
# `set_listing` / `create_subscription` idiom) so the service's `f"publish#{pt}#{pid}"` key
# builder never sees a repr; a path `product_type` outside the enum is a 422 with the SAME
# fixed literal the listings route uses.

def _datasheet_dict(**overrides):
    base = dict(
        owner_team="Claims Automation",
        support_contact="claims-team@example.com",
        data_classification="Confidential",
    )
    base.update(overrides)
    return base


def _make_publish_request(**overrides):
    from models.marketplace import (
        Datasheet,
        ProductType,
        PublishRequest,
        PublishRequestStatus,
    )

    now = _now()
    base = dict(
        id="pub-abc1234567",
        product_type=ProductType.AGENT,
        product_id="agent-1",
        product_name="FNOL Agent",
        tenant_id="ten-1",
        datasheet=Datasheet(**_datasheet_dict()),
        status=PublishRequestStatus.PENDING,
        requested_by="operator-oid",
        requested_by_email="operator.user@example.com",
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return PublishRequest(**base)


def _publish_body(**overrides):
    base = dict(product_type="agent", product_id="agent-1", datasheet=_datasheet_dict())
    base.update(overrides)
    return base


def test_operator_can_create_publish_request(entra_settings):
    mock_svc = MagicMock()
    mock_svc.create_publish_request.return_value = _make_publish_request()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/marketplace/publish-requests",
            json=_publish_body(requested_by="SPOOFED"),
            headers=_headers(),
        )

    assert resp.status_code == 201
    assert resp.json()["id"] == "pub-abc1234567"
    assert resp.json()["product_type"] == "agent"
    assert resp.json()["product_id"] == "agent-1"
    mock_svc.create_publish_request.assert_called_once()
    kwargs = mock_svc.create_publish_request.call_args.kwargs
    # C9: the (product_type, product_id) PAIR identifies the product; the enum is
    # unwrapped to its plain value so the service can key on it directly.
    assert kwargs["product_type"] == "agent"
    assert kwargs["product_id"] == "agent-1"
    # Identity comes from the TOKEN, not the body.
    assert kwargs["requester_oid"] == "operator-oid"
    assert kwargs["requester_email"] == "operator.user@example.com"
    # The declared datasheet is threaded through as the validated model.
    assert kwargs["datasheet"].owner_team == "Claims Automation"
    assert kwargs["datasheet"].support_contact == "claims-team@example.com"
    # The resolved TenantContext scopes the product lookup (foreign-tenant → not_found).
    assert kwargs["ctx"] is not None


def test_operator_can_create_mcp_publish_request(entra_settings):
    """Amendment 1: publish is the only door for MCP servers too — the SAME route."""
    from models.marketplace import ProductType

    mock_svc = MagicMock()
    mock_svc.create_publish_request.return_value = _make_publish_request(
        product_type=ProductType.MCP, product_id="m1", product_name="Claims MCP"
    )
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/marketplace/publish-requests",
            json=_publish_body(product_type="mcp", product_id="m1"),
            headers=_headers(),
        )

    assert resp.status_code == 201
    assert resp.json()["product_type"] == "mcp"
    kwargs = mock_svc.create_publish_request.call_args.kwargs
    assert kwargs["product_type"] == "mcp"
    assert kwargs["product_id"] == "m1"


def test_create_publish_request_requires_product_type(entra_settings):
    """`product_type` has no default (C9) — the registry is never guessed, so a body
    without it is a 422 from the model, BEFORE the service is reached."""
    mock_svc = MagicMock()
    mock_svc.create_publish_request.return_value = _make_publish_request()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/marketplace/publish-requests",
            json={"product_id": "agent-1", "datasheet": _datasheet_dict()},
            headers=_headers(),
        )

    assert resp.status_code == 422
    mock_svc.create_publish_request.assert_not_called()


def test_create_publish_request_rejects_bogus_product_type(entra_settings):
    mock_svc = MagicMock()
    mock_svc.create_publish_request.return_value = _make_publish_request()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/marketplace/publish-requests",
            json=_publish_body(product_type="blueprint"),
            headers=_headers(),
        )

    assert resp.status_code == 422
    mock_svc.create_publish_request.assert_not_called()


def test_create_publish_request_threads_scoped_tenant_ctx(entra_settings):
    from services.tenant_resolver import TenantContext

    scoped = TenantContext(is_global=False, tenant_ids=frozenset({"ten-1"}), tenants=())
    mock_svc = MagicMock()
    mock_svc.create_publish_request.return_value = _make_publish_request()
    client, _ = _build_client(mock_svc, ctx=scoped)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/marketplace/publish-requests",
            json=_publish_body(),
            headers=_headers(),
        )

    assert resp.status_code == 201
    assert mock_svc.create_publish_request.call_args.kwargs["ctx"] is scoped


def test_create_publish_request_falls_back_to_email_when_no_oid(entra_settings):
    """`requester_oid=(oid or email)` — the sibling-mutation fallback idiom.

    A principal with no `oid` claim (the dev-auth shape) must still produce a valid
    `PublishRequest.requested_by`, which is a non-Optional str."""
    mock_svc = MagicMock()
    mock_svc.create_publish_request.return_value = _make_publish_request()
    client, _ = _build_client(mock_svc)

    claims = _claims_for("operator")
    del claims["oid"]
    with patch("core.security_entra.verify_entra_token", return_value=claims):
        resp = client.post(
            "/api/v1/marketplace/publish-requests",
            json=_publish_body(),
            headers=_headers(),
        )

    assert resp.status_code == 201
    kwargs = mock_svc.create_publish_request.call_args.kwargs
    assert kwargs["requester_oid"] == "operator.user@example.com"
    assert kwargs["requester_email"] == "operator.user@example.com"


def test_viewer_cannot_create_publish_request(entra_settings):
    mock_svc = MagicMock()
    mock_svc.create_publish_request.return_value = _make_publish_request()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/marketplace/publish-requests",
            json=_publish_body(),
            headers=_headers(),
        )

    assert resp.status_code == 403
    mock_svc.create_publish_request.assert_not_called()


def test_create_publish_request_rejects_thin_datasheet(entra_settings):
    """The three mandatory declared fields are enforced by the body model (422)."""
    mock_svc = MagicMock()
    mock_svc.create_publish_request.return_value = _make_publish_request()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/marketplace/publish-requests",
            json=_publish_body(datasheet={"owner_team": "Claims Automation"}),
            headers=_headers(),
        )

    assert resp.status_code == 422
    mock_svc.create_publish_request.assert_not_called()


def test_admin_can_list_publish_requests(entra_settings):
    mock_svc = MagicMock()
    mock_svc.list_publish_requests.return_value = [_make_publish_request()]
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get(
            "/api/v1/marketplace/publish-requests?status=pending", headers=_headers()
        )

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert body[0]["product_id"] == "agent-1"
    assert body[0]["product_type"] == "agent"
    assert body[0]["product_name"] == "FNOL Agent"
    mock_svc.list_publish_requests.assert_called_once()
    # The enum-typed query param is unwrapped to the plain str the service pin takes.
    assert mock_svc.list_publish_requests.call_args.kwargs["status"] == "pending"


def test_admin_can_list_publish_requests_unfiltered(entra_settings):
    """No status param → the whole queue (status=None), not an empty filter."""
    mock_svc = MagicMock()
    mock_svc.list_publish_requests.return_value = [_make_publish_request()]
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/marketplace/publish-requests", headers=_headers())

    assert resp.status_code == 200
    assert len(resp.json()) == 1
    mock_svc.list_publish_requests.assert_called_once()
    assert mock_svc.list_publish_requests.call_args.kwargs["status"] is None


def test_list_publish_requests_rejects_bogus_status(entra_settings):
    """An unknown status is a 422 from the enum-typed param, NOT a 500 from the service."""
    mock_svc = MagicMock()
    mock_svc.list_publish_requests.return_value = []
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get(
            "/api/v1/marketplace/publish-requests?status=bogus", headers=_headers()
        )

    assert resp.status_code == 422
    mock_svc.list_publish_requests.assert_not_called()


def test_operator_cannot_list_publish_requests(entra_settings):
    mock_svc = MagicMock()
    mock_svc.list_publish_requests.return_value = []
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/marketplace/publish-requests", headers=_headers())

    assert resp.status_code == 403
    mock_svc.list_publish_requests.assert_not_called()


def test_operator_can_get_publish_request_for_product(entra_settings):
    from services.tenant_resolver import TenantContext

    scoped = TenantContext(is_global=False, tenant_ids=frozenset({"ten-1"}), tenants=())
    mock_svc = MagicMock()
    mock_svc.get_publish_request_for_product.return_value = _make_publish_request()
    client, _ = _build_client(mock_svc, ctx=scoped)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get(
            "/api/v1/marketplace/publish-requests/product/agent/agent-1", headers=_headers()
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    mock_svc.get_publish_request_for_product.assert_called_once()
    call = mock_svc.get_publish_request_for_product.call_args
    # C9: the PAIR is positional, the enum unwrapped to its plain value.
    assert call.args[0] == "agent"
    assert call.args[1] == "agent-1"
    # The resolved TenantContext is threaded so the SERVICE scopes the read — without it a
    # foreign-tenant product_id would leak the declared datasheet + requester email.
    assert call.kwargs["ctx"] is scoped


def test_operator_can_get_publish_request_for_mcp_product(entra_settings):
    from models.marketplace import ProductType

    mock_svc = MagicMock()
    mock_svc.get_publish_request_for_product.return_value = _make_publish_request(
        product_type=ProductType.MCP, product_id="m1", product_name="Claims MCP"
    )
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get(
            "/api/v1/marketplace/publish-requests/product/mcp/m1", headers=_headers()
        )

    assert resp.status_code == 200
    assert resp.json()["product_type"] == "mcp"
    call = mock_svc.get_publish_request_for_product.call_args
    assert call.args[0] == "mcp"
    assert call.args[1] == "m1"


def test_viewer_cannot_get_publish_request_for_product(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get_publish_request_for_product.return_value = _make_publish_request()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get(
            "/api/v1/marketplace/publish-requests/product/agent/agent-1", headers=_headers()
        )

    assert resp.status_code == 403
    mock_svc.get_publish_request_for_product.assert_not_called()


def test_get_publish_request_for_product_404_when_none(entra_settings):
    """No record AND foreign-tenant collapse to the SAME 404 body (no existence oracle)."""
    mock_svc = MagicMock()
    mock_svc.get_publish_request_for_product.return_value = None
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get(
            "/api/v1/marketplace/publish-requests/product/agent/agent-x", headers=_headers()
        )

    assert resp.status_code == 404
    # Byte-identical to the mapped `not_found` literal.
    assert resp.json()["detail"] == "Marketplace product or subscription not found"


def test_get_publish_request_for_product_bogus_type_422(entra_settings):
    """A product_type outside the enum is a 422 with the FIXED listings literal — the
    service is never consulted (and so cannot be probed with a made-up registry name)."""
    mock_svc = MagicMock()
    mock_svc.get_publish_request_for_product.return_value = _make_publish_request()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get(
            "/api/v1/marketplace/publish-requests/product/blueprint/agent-1", headers=_headers()
        )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "Invalid marketplace product type"
    mock_svc.get_publish_request_for_product.assert_not_called()


def test_admin_can_approve_publish(entra_settings):
    from models.marketplace import PublishRequestStatus

    mock_svc = MagicMock()
    mock_svc.approve_publish = AsyncMock(
        return_value=_make_publish_request(
            status=PublishRequestStatus.APPROVED, decided_by="admin-oid", decided_at=_now()
        )
    )
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/marketplace/publish-requests/pub-abc1234567/approve",
            json={"decided_by": "SPOOFED"},
            headers=_headers(),
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    mock_svc.approve_publish.assert_awaited_once()
    call = mock_svc.approve_publish.await_args
    assert call.args[0] == "pub-abc1234567"
    # decided_by comes from the TOKEN, never the body.
    assert call.kwargs["decided_by"] == "admin-oid"


def test_operator_cannot_approve_publish(entra_settings):
    mock_svc = MagicMock()
    mock_svc.approve_publish = AsyncMock(return_value=_make_publish_request())
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/marketplace/publish-requests/pub-abc1234567/approve", headers=_headers()
        )

    assert resp.status_code == 403
    mock_svc.approve_publish.assert_not_called()


def test_admin_can_reject_publish_with_reason(entra_settings):
    from models.marketplace import PublishRequestStatus

    mock_svc = MagicMock()
    mock_svc.reject_publish.return_value = _make_publish_request(
        status=PublishRequestStatus.REJECTED, decided_by="admin-oid", decision_reason="thin datasheet"
    )
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/marketplace/publish-requests/pub-abc1234567/reject",
            json={"reason": "thin datasheet", "decided_by": "SPOOFED"},
            headers=_headers(),
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    mock_svc.reject_publish.assert_called_once()
    call = mock_svc.reject_publish.call_args
    assert call.args[0] == "pub-abc1234567"
    # reason from the body; decided_by from the TOKEN.
    assert call.kwargs["reason"] == "thin datasheet"
    assert call.kwargs["decided_by"] == "admin-oid"


def test_admin_can_reject_publish_empty_body(entra_settings):
    mock_svc = MagicMock()
    mock_svc.reject_publish.return_value = _make_publish_request()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/marketplace/publish-requests/pub-abc1234567/reject",
            json={},
            headers=_headers(),
        )

    assert resp.status_code == 200
    call = mock_svc.reject_publish.call_args
    assert call.kwargs["reason"] is None
    assert call.kwargs["decided_by"] == "admin-oid"


def test_operator_cannot_reject_publish(entra_settings):
    mock_svc = MagicMock()
    mock_svc.reject_publish.return_value = _make_publish_request()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/marketplace/publish-requests/pub-abc1234567/reject",
            json={"reason": "x"},
            headers=_headers(),
        )

    assert resp.status_code == 403
    mock_svc.reject_publish.assert_not_called()


def test_admin_can_unpublish_agent_product(entra_settings):
    mock_svc = MagicMock()
    mock_svc.unpublish = AsyncMock(return_value=None)
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/marketplace/products/agent/agent-1/unpublish", headers=_headers()
        )

    assert resp.status_code == 204
    mock_svc.unpublish.assert_awaited_once()
    call = mock_svc.unpublish.await_args
    # C9: the (product_type, product_id) PAIR is positional; decided_by from the TOKEN.
    assert call.args[0] == "agent"
    assert call.args[1] == "agent-1"
    assert call.kwargs["decided_by"] == "admin-oid"


def test_admin_can_unpublish_mcp_product(entra_settings):
    """The generalized path delists an MCP server through the SAME route."""
    mock_svc = MagicMock()
    mock_svc.unpublish = AsyncMock(return_value=None)
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/marketplace/products/mcp/m1/unpublish", headers=_headers()
        )

    assert resp.status_code == 204
    call = mock_svc.unpublish.await_args
    assert call.args[0] == "mcp"
    assert call.args[1] == "m1"
    assert call.kwargs["decided_by"] == "admin-oid"


def test_operator_cannot_unpublish_agent_product(entra_settings):
    mock_svc = MagicMock()
    mock_svc.unpublish = AsyncMock(return_value=None)
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/marketplace/products/agent/agent-1/unpublish", headers=_headers()
        )

    assert resp.status_code == 403
    mock_svc.unpublish.assert_not_called()


def test_unpublish_bogus_product_type_422(entra_settings):
    mock_svc = MagicMock()
    mock_svc.unpublish = AsyncMock(return_value=None)
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/marketplace/products/blueprint/agent-1/unpublish", headers=_headers()
        )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "Invalid marketplace product type"
    mock_svc.unpublish.assert_not_called()


def test_bogus_product_type_does_not_outrank_rbac(entra_settings):
    """RBAC BEFORE the 422: `_product_type_value()` runs in the handler body, so the role
    dependency has already answered by then.

    Ordering is the security property, not a detail. If the path guard ran first, an
    unauthorized caller could tell a REAL product_type ("agent" → 403) from a bogus one
    ("blueprint" → 422) and read the enum off the error codes; worse, the 422 would confirm
    the route exists to someone who may not use it. Both generalized paths are pinned — the
    OPERATOR read and the ADMIN unpublish — because they each call the guard."""
    mock_svc = MagicMock()
    mock_svc.get_publish_request_for_product.return_value = _make_publish_request()
    mock_svc.unpublish = AsyncMock(return_value=None)
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        read = client.get(
            "/api/v1/marketplace/publish-requests/product/blueprint/agent-1", headers=_headers()
        )
        unpub = client.post(
            "/api/v1/marketplace/products/blueprint/agent-1/unpublish", headers=_headers()
        )

    assert read.status_code == 403
    assert unpub.status_code == 403
    mock_svc.get_publish_request_for_product.assert_not_called()
    mock_svc.unpublish.assert_not_called()


def test_generalized_publish_paths_replace_the_agent_only_ones(entra_settings):
    """C9 says REPLACES, not "adds": leaving the agent-only paths registered would keep a
    second, product-type-blind door into the same records."""
    import api.routes.marketplace as marketplace_module

    paths = {getattr(r, "path", None) for r in marketplace_module.router.routes}
    assert "/marketplace/publish-requests/product/{product_type}/{product_id}" in paths
    assert "/marketplace/products/{product_type}/{product_id}/unpublish" in paths
    assert "/marketplace/publish-requests/agent/{agent_id}" not in paths
    assert "/marketplace/products/agent/{agent_id}/unpublish" not in paths


def test_create_publish_request_error_mapping(entra_settings):
    """E33 kinds on the create path → status + FIXED detail (never str(err))."""
    from services.marketplace_service import MarketplaceError

    secret_marker = "LEAKED-MARKETPLACE-INTERNAL-DETAIL"
    cases = [
        ("not_found", 404, "Marketplace product or subscription not found"),
        ("agent_not_approved", 409, None),
        ("publish_conflict", 409, None),
        # Amendment 1 / C10: identity is a publish precondition for BOTH product types.
        ("identity_not_provisioned", 409, "identity is not provisioned"),
        # E29×E33 seam: agent-specific by design — only agents can be Databricks-governed.
        ("databricks_publish_unsupported", 409,
         "Databricks-governed agents cannot be published to the marketplace yet"),
    ]
    for kind, expected_status, expected_detail in cases:
        mock_svc = MagicMock()
        mock_svc.create_publish_request.side_effect = MarketplaceError(
            secret_marker, kind=kind
        )
        client, _ = _build_client(mock_svc)

        with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
            resp = client.post(
                "/api/v1/marketplace/publish-requests",
                json=_publish_body(),
                headers=_headers(),
            )

        assert resp.status_code == expected_status, f"kind={kind}"
        assert secret_marker not in resp.text, f"kind={kind} leaked the error message"
        if expected_detail is not None:
            assert resp.json()["detail"] == expected_detail, f"kind={kind}"


def test_publish_error_details_are_product_neutral(entra_settings):
    """C9: the kind NAMES are unchanged, but the literals must not say "agent" — the same
    codes now answer MCP publish attempts."""
    import api.routes.marketplace as marketplace_module

    for kind in ("agent_not_approved", "publish_conflict", "identity_not_provisioned"):
        detail = marketplace_module._ERROR_DETAIL[kind]
        assert "agent" not in detail.lower(), f"{kind} detail is agent-specific: {detail}"


def test_approve_publish_error_mapping(entra_settings):
    """A failed registry envelope write surfaces as 502 with a FIXED detail."""
    from services.marketplace_service import MarketplaceError

    secret_marker = "LEAKED-MARKETPLACE-INTERNAL-DETAIL"
    cases = [
        ("not_found", 404),
        ("illegal_publish_state", 409),
        ("publish_write_failed", 502),
        # C9: approve RE-ASSERTS the lifecycle + identity preconditions.
        ("agent_not_approved", 409),
        ("identity_not_provisioned", 409),
    ]
    for kind, expected_status in cases:
        mock_svc = MagicMock()
        mock_svc.approve_publish = AsyncMock(
            side_effect=MarketplaceError(secret_marker, kind=kind)
        )
        client, _ = _build_client(mock_svc)

        with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
            resp = client.post(
                "/api/v1/marketplace/publish-requests/pub-abc1234567/approve",
                headers=_headers(),
            )

        assert resp.status_code == expected_status, f"kind={kind}"
        assert secret_marker not in resp.text, f"kind={kind} leaked the error message"


def test_unpublish_not_found_maps_to_404(entra_settings):
    from services.marketplace_service import MarketplaceError

    secret_marker = "LEAKED-MARKETPLACE-INTERNAL-DETAIL"
    mock_svc = MagicMock()
    mock_svc.unpublish = AsyncMock(side_effect=MarketplaceError(secret_marker, kind="not_found"))
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/marketplace/products/agent/agent-x/unpublish", headers=_headers()
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Marketplace product or subscription not found"
    assert secret_marker not in resp.text


def test_reject_publish_error_mapping_illegal_state(entra_settings):
    from services.marketplace_service import MarketplaceError

    secret_marker = "LEAKED-MARKETPLACE-INTERNAL-DETAIL"
    mock_svc = MagicMock()
    mock_svc.reject_publish.side_effect = MarketplaceError(
        secret_marker, kind="illegal_publish_state"
    )
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/marketplace/publish-requests/pub-abc1234567/reject",
            json={"reason": "x"},
            headers=_headers(),
        )

    assert resp.status_code == 409
    assert secret_marker not in resp.text


# --- MarketplaceError.kind → HTTP status mapping (FIXED detail) -------------

def test_marketplace_error_status_mapping(entra_settings):
    from services.marketplace_service import MarketplaceError

    secret_marker = "LEAKED-MARKETPLACE-INTERNAL-DETAIL"
    cases = [
        ("not_found", 404),
        ("conflict", 409),
        ("bad_request", 422),
        ("grant_failed", 502),
    ]
    for kind, expected_status in cases:
        mock_svc = MagicMock()
        mock_svc.create_subscription = AsyncMock(
            side_effect=MarketplaceError(secret_marker, kind=kind)
        )
        client, _ = _build_client(mock_svc)

        with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
            resp = client.post(
                "/api/v1/marketplace/subscriptions",
                json={"product_type": "agent", "product_id": "bp-fnol"},
                headers=_headers(),
            )

        assert resp.status_code == expected_status, f"kind={kind}"
        # FIXED detail literal — the MarketplaceError message must NOT leak.
        assert secret_marker not in resp.text, f"kind={kind} leaked the error message"


# --- both-block app wiring guard --------------------------------------------

def test_routes_registered_in_app(entra_settings):
    import main

    from conftest import app_route_paths

    paths = app_route_paths(main.app)
    assert "/api/v1/marketplace/agent-products" in paths
    assert "/api/v1/marketplace/admin/metrics" in paths
