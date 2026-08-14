"""Integration test for /users/me under AUTH_PROVIDER=entra."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def reset_modules(monkeypatch):
    """Configure Entra mode and reload all modules so settings.AUTH_PROVIDER='entra' takes effect."""
    monkeypatch.setenv("AUTH_PROVIDER", "entra")
    monkeypatch.setenv("USE_DEV_AUTH", "False")
    monkeypatch.setenv("DEBUG", "False")
    monkeypatch.setenv("ENTRA_TENANT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("ENTRA_AUDIENCE", "api://agp")
    monkeypatch.setenv("ENTRA_ROLE_ADMIN", "Platform.Admin")
    monkeypatch.setenv("ENTRA_ROLE_OPERATOR", "Platform.Operator")
    monkeypatch.setenv("ENTRA_ROLE_VIEWER", "Platform.Viewer")

    import sys
    for mod in list(sys.modules):
        if mod.startswith("core.") or mod.startswith("api.") or mod == "main":
            sys.modules.pop(mod, None)
    yield


def _build_app():
    """Spin up the FastAPI app fresh after env is set."""
    from main import app

    # E24/T4: /users/me now resolves tenants. Stub the resolver singleton so these
    # identity tests stay hermetic (no live Graph/token HTTP) — tenant behavior is
    # covered in test_users_me_tenants.py.
    import api.routes.users as users_module
    from services.tenant_resolver import TenantContext

    class _EmptyResolver:
        async def resolve(self, principal):
            return TenantContext(is_global=False, tenant_ids=frozenset(), tenants=())

    users_module._tenant_resolver = _EmptyResolver()
    return app


def test_users_me_returns_maria_as_operator():
    """Maria signed in: /users/me returns email + role=operator + can_deploy=True."""
    app = _build_app()
    client = TestClient(app)

    fake_claims = {
        "oid": "maria-oid",
        "preferred_username": "maria.bauer@contoso.onmicrosoft.com",
        "name": "Maria Bauer",
        "roles": ["Platform.Operator"],
    }

    with patch("core.security_entra.verify_entra_token", return_value=fake_claims):
        response = client.get(
            "/api/v1/users/me",
            headers={"Authorization": "Bearer fake-token-maria"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email"] == "maria.bauer@contoso.onmicrosoft.com"
    assert body["role"] == "operator"
    assert body["role_level"] == 1
    assert body["can_deploy"] is True
    # The sidebar footer shows the human name from the token's `name` claim.
    assert body["name"] == "Maria Bauer"


def test_users_me_returns_name_none_when_claim_absent():
    """Token without a `name` claim → response `name` is None (FE falls back to
    the email alias)."""
    app = _build_app()
    client = TestClient(app)

    fake_claims = {
        "oid": "noname-oid",
        "preferred_username": "no.name@contoso.onmicrosoft.com",
        "roles": ["Platform.Viewer"],
    }

    with patch("core.security_entra.verify_entra_token", return_value=fake_claims):
        response = client.get(
            "/api/v1/users/me",
            headers={"Authorization": "Bearer fake-token-noname"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] is None


def test_users_me_returns_oid():
    """Entra-authenticated caller: /users/me echoes the token's `oid` claim
    (the stable principal id the FE compares against app-role-assignment
    principals for E6 user→agent grants)."""
    app = _build_app()
    client = TestClient(app)

    fake_claims = {
        "oid": "maria-oid",
        "preferred_username": "maria.bauer@contoso.onmicrosoft.com",
        "name": "Maria Bauer",
        "roles": ["Platform.Operator"],
    }

    with patch("core.security_entra.verify_entra_token", return_value=fake_claims):
        response = client.get(
            "/api/v1/users/me",
            headers={"Authorization": "Bearer fake-token-maria"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["oid"] == "maria-oid"


def test_users_me_returns_lars_as_admin():
    app = _build_app()
    client = TestClient(app)

    fake_claims = {
        "oid": "lars-oid",
        "preferred_username": "lars.svensson@contoso.onmicrosoft.com",
        "name": "Lars Svensson",
        "roles": ["Platform.Admin"],
    }

    with patch("core.security_entra.verify_entra_token", return_value=fake_claims):
        response = client.get(
            "/api/v1/users/me",
            headers={"Authorization": "Bearer fake-token-lars"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email"] == "lars.svensson@contoso.onmicrosoft.com"
    assert body["role"] == "admin"
    assert body["can_deploy"] is True


def test_users_me_returns_hans_as_viewer():
    app = _build_app()
    client = TestClient(app)

    fake_claims = {
        "oid": "hans-oid",
        "preferred_username": "hans.muller@contoso.onmicrosoft.com",
        "name": "Hans Muller",
        "roles": ["Platform.Viewer"],
    }

    with patch("core.security_entra.verify_entra_token", return_value=fake_claims):
        response = client.get(
            "/api/v1/users/me",
            headers={"Authorization": "Bearer fake-token-hans"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["role"] == "viewer"
    assert body["can_deploy"] is False


def test_users_me_returns_401_without_token():
    """No Authorization header → 401."""
    app = _build_app()
    client = TestClient(app)

    response = client.get("/api/v1/users/me")
    assert response.status_code == 401


def test_users_me_returns_401_with_invalid_token():
    """Bad token → verify_entra_token raises HTTPException(401) → 401 to client."""
    from fastapi import HTTPException

    app = _build_app()
    client = TestClient(app)

    def fail(*args, **kwargs):
        raise HTTPException(status_code=401, detail="Invalid token: signature mismatch")

    with patch("core.security_entra.verify_entra_token", side_effect=fail):
        response = client.get(
            "/api/v1/users/me",
            headers={"Authorization": "Bearer garbage"},
        )

    assert response.status_code == 401
    assert "signature" in response.json()["detail"].lower() or "invalid" in response.json()["detail"].lower()


# ===========================================================================
# E36/T18 — /users/me and current_principal read ONE precedence
# ===========================================================================


def test_users_me_and_current_principal_agree_on_the_same_claims():
    """The defect was their disagreement, so pin the agreement itself.

    This fixture is the only shape that could ever expose it: a token carrying BOTH
    `email` and `preferred_username` with *different* values. /users/me used to read
    `email` first while `current_principal` (→ `created_by`) read `preferred_username`
    first, so the caller was shown one address and attributed the other. Both now read
    `rbac.principal_email`, and `preferred_username` is the single winning rung.
    """
    from core.rbac import current_principal

    app = _build_app()
    client = TestClient(app)

    fake_claims = {
        "oid": "twoclaims-oid",
        "preferred_username": "shown.and.attributed@contoso.onmicrosoft.com",
        "email": "different.mailbox@contoso.com",
        "name": "Two Claims",
        "roles": ["Platform.Operator"],
    }

    class _FakeRequest:
        headers = {"Authorization": "Bearer fake-token-twoclaims"}

    with patch("core.security_entra.verify_entra_token", return_value=fake_claims):
        response = client.get(
            "/api/v1/users/me",
            headers={"Authorization": "Bearer fake-token-twoclaims"},
        )
        principal = current_principal(_FakeRequest())

    assert response.status_code == 200, response.text
    displayed = response.json()["email"]
    assert displayed == principal.email, (
        "the address shown to the caller must be the address attributed to their writes"
    )
    assert displayed == "shown.and.attributed@contoso.onmicrosoft.com"


def test_users_me_shows_unknown_when_the_token_carries_no_email_claim():
    """The `"unknown"` fallback lives at THIS call site, not in the helper: a service
    principal's `Principal.email` stays None (so `created_by` falls back to the oid)
    while the interface still has a string to render."""
    app = _build_app()
    client = TestClient(app)

    fake_claims = {"oid": "sp-oid", "roles": ["Platform.Viewer"]}

    with patch("core.security_entra.verify_entra_token", return_value=fake_claims):
        response = client.get(
            "/api/v1/users/me",
            headers={"Authorization": "Bearer fake-token-sp"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["email"] == "unknown"
