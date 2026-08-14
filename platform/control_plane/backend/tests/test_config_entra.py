"""Test that Entra-related settings are declared and load from env vars."""

import os

import pytest


def test_settings_has_auth_provider_default():
    """AUTH_PROVIDER defaults to 'entra' — Entra is the sole auth provider."""
    # Force fresh import to avoid cached singleton
    if "core.config" in __import__("sys").modules:
        del __import__("sys").modules["core.config"]
    # Clear any AUTH_PROVIDER from environment for this test
    monkey = pytest.MonkeyPatch()
    monkey.delenv("AUTH_PROVIDER", raising=False)
    try:
        from core.config import Settings

        # _env_file=None disables loading from .env / .env.local so the test
        # exercises the pure code default, not whatever a developer's local
        # env file says.
        s = Settings(_env_file=None)
        assert s.AUTH_PROVIDER == "entra"
    finally:
        monkey.undo()


def test_settings_loads_entra_fields_from_env(monkeypatch):
    """All nine Entra fields (AUTH_PROVIDER + 8 ENTRA_*) load from environment variables."""
    monkeypatch.setenv("AUTH_PROVIDER", "entra")
    monkeypatch.setenv("ENTRA_TENANT_ID", "tenant-uuid")
    monkeypatch.setenv("ENTRA_AUDIENCE", "api://agp")
    monkeypatch.setenv("ENTRA_BACKEND_CLIENT_ID", "backend-client-uuid")
    monkeypatch.setenv("ENTRA_BACKEND_CLIENT_SECRET", "literal-secret")
    monkeypatch.setenv(
        "ENTRA_BACKEND_CLIENT_SECRET_ARN",
        "arn:aws:secretsmanager:eu-west-1:123:secret:test-XXX",
    )
    monkeypatch.setenv("ENTRA_ROLE_ADMIN", "Platform.Admin")
    monkeypatch.setenv("ENTRA_ROLE_OPERATOR", "Platform.Operator")
    monkeypatch.setenv("ENTRA_ROLE_VIEWER", "Platform.Viewer")

    # Force fresh import
    if "core.config" in __import__("sys").modules:
        del __import__("sys").modules["core.config"]
    from core.config import Settings

    s = Settings()
    assert s.AUTH_PROVIDER == "entra"
    assert s.ENTRA_TENANT_ID == "tenant-uuid"
    assert s.ENTRA_AUDIENCE == "api://agp"
    assert s.ENTRA_BACKEND_CLIENT_ID == "backend-client-uuid"
    assert s.ENTRA_BACKEND_CLIENT_SECRET == "literal-secret"
    assert s.ENTRA_BACKEND_CLIENT_SECRET_ARN.startswith("arn:aws:secretsmanager:")
    assert s.ENTRA_ROLE_ADMIN == "Platform.Admin"
    assert s.ENTRA_ROLE_OPERATOR == "Platform.Operator"
    assert s.ENTRA_ROLE_VIEWER == "Platform.Viewer"
