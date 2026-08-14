"""Tests for current_principal dependency under AUTH_PROVIDER=entra."""

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


def test_entra_principal_operator(entra_settings):
    from core.rbac import Role, current_principal

    with patch("core.security_entra.verify_entra_token", return_value={
        "oid": "maria-oid",
        "preferred_username": "maria.bauer@contoso.onmicrosoft.com",
        "roles": ["Platform.Operator"],
    }):
        principal = current_principal(_fake_request("Bearer fake-token"))

    assert principal.oid == "maria-oid"
    assert principal.email == "maria.bauer@contoso.onmicrosoft.com"
    assert principal.role == Role.OPERATOR
    assert principal.raw_claims["oid"] == "maria-oid"


def test_entra_missing_authorization_returns_401(entra_settings):
    from core.rbac import current_principal

    with pytest.raises(HTTPException) as exc:
        current_principal(_fake_request(""))

    assert exc.value.status_code == 401


def test_entra_no_roles_claim_defaults_viewer_but_oid_populated(entra_settings):
    from core.rbac import Role, current_principal

    with patch("core.security_entra.verify_entra_token", return_value={
        "oid": "stranger-oid",
        "preferred_username": "stranger@example.com",
    }):
        principal = current_principal(_fake_request("Bearer fake-token"))

    assert principal.role == Role.VIEWER
    assert principal.oid == "stranger-oid"
    assert principal.email == "stranger@example.com"


def test_entra_multiple_roles_picks_highest(entra_settings):
    from core.rbac import Role, current_principal

    with patch("core.security_entra.verify_entra_token", return_value={
        "oid": "multi-oid",
        "preferred_username": "multi@example.com",
        "roles": ["Platform.Viewer", "Platform.Admin", "Platform.Operator"],
    }):
        principal = current_principal(_fake_request("Bearer fake-token"))

    assert principal.role == Role.ADMIN


def test_entra_email_fallback_to_email_claim(entra_settings):
    """No preferred_username, but an email claim → email from email."""
    from core.rbac import current_principal

    with patch("core.security_entra.verify_entra_token", return_value={
        "oid": "e-oid",
        "email": "from-email@example.com",
        "roles": ["Platform.Viewer"],
    }):
        principal = current_principal(_fake_request("Bearer fake-token"))

    assert principal.email == "from-email@example.com"


def test_entra_email_fallback_to_upn_claim(entra_settings):
    """No preferred_username or email, but a upn claim → email from upn."""
    from core.rbac import current_principal

    with patch("core.security_entra.verify_entra_token", return_value={
        "oid": "u-oid",
        "upn": "from-upn@example.com",
        "roles": ["Platform.Viewer"],
    }):
        principal = current_principal(_fake_request("Bearer fake-token"))

    assert principal.email == "from-upn@example.com"
