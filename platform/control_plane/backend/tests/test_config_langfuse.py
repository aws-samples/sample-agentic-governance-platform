"""Test that Langfuse-related settings (E26/C6) are declared and load from env vars."""


def _fresh_settings(**kwargs):
    """Import Settings fresh to avoid the cached module singleton."""
    if "core.config" in __import__("sys").modules:
        del __import__("sys").modules["core.config"]
    from core.config import Settings

    return Settings(**kwargs)


def test_settings_has_langfuse_defaults():
    """LANGFUSE_HOST and LANGFUSE_ADMIN_SECRET_NAME default to '' (not configured)."""
    # _env_file=None disables loading from .env so the test exercises the pure
    # code default, not whatever a developer's local env file says.
    s = _fresh_settings(_env_file=None)
    assert s.LANGFUSE_HOST == ""
    assert s.LANGFUSE_ADMIN_SECRET_NAME == ""
    # configured (per C6) = bool(settings.LANGFUSE_HOST) — False when unset.
    assert bool(s.LANGFUSE_HOST) is False


def test_settings_loads_langfuse_fields_from_env(monkeypatch):
    """Both Langfuse fields load from environment variables; host makes configured True."""
    monkeypatch.setenv("LANGFUSE_HOST", "https://example.cloudfront.net")
    monkeypatch.setenv("LANGFUSE_ADMIN_SECRET_NAME", "agp/langfuse/admin")

    s = _fresh_settings()
    assert s.LANGFUSE_HOST == "https://example.cloudfront.net"
    assert s.LANGFUSE_ADMIN_SECRET_NAME == "agp/langfuse/admin"
    # configured (per C6) = bool(settings.LANGFUSE_HOST) — True when set.
    assert bool(s.LANGFUSE_HOST) is True
