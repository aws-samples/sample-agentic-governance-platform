"""Entra-related settings are declared, default to Entra and load from env vars."""

import sys

import pytest


def _fresh_settings_class():
    """The class imported fresh, past the module-level singleton."""
    sys.modules.pop("core.config", None)
    from core.config import Settings

    return Settings


def test_settings_has_auth_provider_default():
    """AUTH_PROVIDER defaults to 'entra'; `_env_file=None` keeps a developer's .env out of the default."""
    monkey = pytest.MonkeyPatch()
    monkey.delenv("AUTH_PROVIDER", raising=False)
    try:
        s = _fresh_settings_class()(_env_file=None)
        assert s.AUTH_PROVIDER == "entra"
    finally:
        monkey.undo()


def test_settings_ignores_env_file_keys_that_belong_to_sibling_tools(tmp_path):
    """The shared `.env` carries the frontend's and the infra scripts' variables too; an unknown key
    must not break backend startup."""
    env_file = tmp_path / ".env"
    env_file.write_text("VITE_API_BASE=http://localhost\nAPP_NAME=From file\n")

    s = _fresh_settings_class()(_env_file=env_file)

    assert s.APP_NAME == "From file"
    assert not hasattr(s, "VITE_API_BASE")


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

    s = _fresh_settings_class()()
    assert s.AUTH_PROVIDER == "entra"
    assert s.ENTRA_TENANT_ID == "tenant-uuid"
    assert s.ENTRA_AUDIENCE == "api://agp"
    assert s.ENTRA_BACKEND_CLIENT_ID == "backend-client-uuid"
    assert s.ENTRA_BACKEND_CLIENT_SECRET == "literal-secret"
    assert s.ENTRA_BACKEND_CLIENT_SECRET_ARN.startswith("arn:aws:secretsmanager:")
    assert s.ENTRA_ROLE_ADMIN == "Platform.Admin"
    assert s.ENTRA_ROLE_OPERATOR == "Platform.Operator"
    assert s.ENTRA_ROLE_VIEWER == "Platform.Viewer"
