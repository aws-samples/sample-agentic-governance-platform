"""Tests for _extract_role under AUTH_PROVIDER=entra."""

from unittest.mock import patch

import pytest
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def reset_modules():
    import sys
    for mod in ["core.rbac", "core.security_entra", "core.config"]:
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


def _fake_request(authorization_header: str = ""):
    """Build a minimal Request-like object with the headers we care about."""
    class FakeRequest:
        headers = {}
    req = FakeRequest()
    req.headers = {"Authorization": authorization_header} if authorization_header else {}
    return req


def test_admin_role_claim_maps_to_admin(entra_settings):
    from core.rbac import Role, _extract_role

    with patch("core.security_entra.verify_entra_token", return_value={
        "oid": "lars-oid",
        "roles": ["Platform.Admin"],
    }):
        role = _extract_role(_fake_request("Bearer fake-token"))

    assert role == Role.ADMIN


def test_operator_role_claim_maps_to_operator(entra_settings):
    from core.rbac import Role, _extract_role

    with patch("core.security_entra.verify_entra_token", return_value={
        "oid": "maria-oid",
        "roles": ["Platform.Operator"],
    }):
        role = _extract_role(_fake_request("Bearer fake-token"))

    assert role == Role.OPERATOR


def test_viewer_role_claim_maps_to_viewer(entra_settings):
    from core.rbac import Role, _extract_role

    with patch("core.security_entra.verify_entra_token", return_value={
        "oid": "hans-oid",
        "roles": ["Platform.Viewer"],
    }):
        role = _extract_role(_fake_request("Bearer fake-token"))

    assert role == Role.VIEWER


def test_no_role_claim_defaults_to_viewer(entra_settings):
    """Token validates but has no `roles` claim → least-privilege default."""
    from core.rbac import Role, _extract_role

    with patch("core.security_entra.verify_entra_token", return_value={
        "oid": "stranger-oid",
    }):
        role = _extract_role(_fake_request("Bearer fake-token"))

    assert role == Role.VIEWER


def test_multiple_roles_picks_highest(entra_settings):
    """If a user has multiple Platform.* role claims, return the highest."""
    from core.rbac import Role, _extract_role

    with patch("core.security_entra.verify_entra_token", return_value={
        "oid": "multi-oid",
        "roles": ["Platform.Viewer", "Platform.Admin", "Platform.Operator"],
    }):
        role = _extract_role(_fake_request("Bearer fake-token"))

    assert role == Role.ADMIN


def test_missing_authorization_header_returns_401(entra_settings):
    """No Bearer header under Entra → 401 (no admin fallback)."""
    from core.rbac import _extract_role

    with pytest.raises(HTTPException) as exc:
        _extract_role(_fake_request(""))

    assert exc.value.status_code == 401


def test_invalid_token_under_entra_returns_401(entra_settings):
    """Invalid token under Entra → 401 (no admin fallback even if USE_DEV_AUTH=True)."""
    from core.rbac import _extract_role

    def fail(*args, **kwargs):
        raise HTTPException(status_code=401, detail="boom")

    with patch("core.security_entra.verify_entra_token", side_effect=fail):
        with pytest.raises(HTTPException) as exc:
            _extract_role(_fake_request("Bearer broken-token"))

    assert exc.value.status_code == 401


def test_use_dev_auth_does_not_apply_under_entra(monkeypatch):
    """With dev-auth OFF, the Entra path validates tokens and never falls back.

    Entra is the sole real provider. When USE_DEV_AUTH/DEBUG are off (the
    default), a missing token returns 401 and an invalid token returns 401 —
    the dev flag being off grants no admin bypass.
    """
    monkeypatch.setenv("AUTH_PROVIDER", "entra")
    monkeypatch.setenv("USE_DEV_AUTH", "False")  # dev-auth off → real Entra validation
    monkeypatch.setenv("DEBUG", "False")
    monkeypatch.setenv("ENTRA_TENANT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("ENTRA_AUDIENCE", "api://agp")

    import sys
    sys.modules.pop("core.rbac", None)
    sys.modules.pop("core.config", None)
    from core.rbac import _extract_role

    # No auth header → 401 (no dev-auth fallback).
    with pytest.raises(HTTPException) as exc:
        _extract_role(_fake_request(""))
    assert exc.value.status_code == 401

    # Invalid token → 401 (verify_entra_token rejects it; no bypass).
    def fail(*args, **kwargs):
        raise HTTPException(status_code=401, detail="boom")

    with patch("core.security_entra.verify_entra_token", side_effect=fail):
        with pytest.raises(HTTPException) as exc2:
            _extract_role(_fake_request("Bearer broken-token"))
    assert exc2.value.status_code == 401


# ===========================================================================
# E36/T18 — `principal_email`: ONE precedence order, shared by both live sites
# ===========================================================================
# The defect was two orders: `Principal.email` (→ `created_by`) read
# `preferred_username` first, while `/users/me` (→ what the UI shows) read the
# `email` claim first, so a token carrying both with different values attributed
# a caller's writes to one address and showed them another. The spec decision is
# `preferred_username` → `email` → `upn`, in one helper both sites call.


def test_principal_email_prefers_preferred_username_over_every_other_claim():
    """Rung 1: with all three present, `preferred_username` wins."""
    from core.rbac import principal_email

    assert principal_email({
        "preferred_username": "pu@example.com",
        "email": "mail@example.com",
        "upn": "upn@example.com",
    }) == "pu@example.com"


def test_principal_email_falls_back_to_the_email_claim():
    """Rung 2: no `preferred_username` → the `email` claim, ahead of `upn`."""
    from core.rbac import principal_email

    assert principal_email({
        "email": "mail@example.com",
        "upn": "upn@example.com",
    }) == "mail@example.com"


def test_principal_email_falls_back_to_upn_last():
    """Rung 3: neither of the first two → `upn`."""
    from core.rbac import principal_email

    assert principal_email({"upn": "upn@example.com"}) == "upn@example.com"


def test_principal_email_is_none_when_the_token_carries_none_of_the_three():
    """All absent → None, never a fabricated literal: a `"unknown"` here would land in
    `created_by` as an invented identity. The display fallback lives at the /users/me
    call site instead (see `test_users_me_*`), and route code pairs the None with the
    caller's `oid` (the `created_by=principal.email or principal.oid` idiom)."""
    from core.rbac import principal_email

    assert principal_email({"oid": "sp-oid", "roles": ["Platform.Operator"]}) is None
    assert principal_email({}) is None


def test_principal_email_skips_empty_string_claims():
    """An empty claim is absent, not an address — the `or` chain must fall through it."""
    from core.rbac import principal_email

    assert principal_email({"preferred_username": "", "email": "mail@example.com"}) == "mail@example.com"
