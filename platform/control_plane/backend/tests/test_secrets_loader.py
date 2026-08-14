"""Test the Entra backend client-secret loader."""

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_loader_cache():
    """Clear the module-level cache between tests."""
    import sys
    if "core.secrets_loader" in sys.modules:
        del sys.modules["core.secrets_loader"]
    yield
    if "core.secrets_loader" in sys.modules:
        del sys.modules["core.secrets_loader"]


def test_returns_literal_secret_when_set(monkeypatch):
    """Local dev path: ENTRA_BACKEND_CLIENT_SECRET is read directly."""
    monkeypatch.setenv("ENTRA_BACKEND_CLIENT_SECRET", "literal-from-env-local")
    monkeypatch.setenv("ENTRA_BACKEND_CLIENT_SECRET_ARN", "")  # ARN ignored when literal present

    from core.secrets_loader import load_entra_backend_client_secret

    result = load_entra_backend_client_secret()
    assert result == "literal-from-env-local"


def test_fetches_from_secrets_manager_when_only_arn_set(monkeypatch):
    """Cloud path: literal blank, ARN set, boto3 returns secret."""
    monkeypatch.setenv("ENTRA_BACKEND_CLIENT_SECRET", "")
    monkeypatch.setenv(
        "ENTRA_BACKEND_CLIENT_SECRET_ARN",
        "arn:aws:secretsmanager:eu-west-1:123:secret:agp/graph-client-secret-XXXXXX",
    )
    monkeypatch.setenv("AWS_REGION", "eu-west-1")

    fake_client = MagicMock()
    fake_client.get_secret_value.return_value = {
        "SecretString": "secret-from-aws"
    }

    with patch("boto3.client", return_value=fake_client):
        from core.secrets_loader import load_entra_backend_client_secret

        result = load_entra_backend_client_secret()

    assert result == "secret-from-aws"
    fake_client.get_secret_value.assert_called_once_with(
        SecretId="arn:aws:secretsmanager:eu-west-1:123:secret:agp/graph-client-secret-XXXXXX"
    )


def test_secrets_manager_with_json_secret(monkeypatch):
    """If Secrets Manager returns JSON, extract the 'secret' or 'value' key (graceful)."""
    monkeypatch.setenv("ENTRA_BACKEND_CLIENT_SECRET", "")
    monkeypatch.setenv(
        "ENTRA_BACKEND_CLIENT_SECRET_ARN",
        "arn:aws:secretsmanager:eu-west-1:123:secret:test-XXX",
    )

    fake_client = MagicMock()
    fake_client.get_secret_value.return_value = {
        "SecretString": json.dumps({"secret": "json-wrapped-secret"})
    }

    with patch("boto3.client", return_value=fake_client):
        from core.secrets_loader import load_entra_backend_client_secret

        result = load_entra_backend_client_secret()

    assert result == "json-wrapped-secret"


def test_raises_when_neither_set(monkeypatch):
    """If both are blank, raise RuntimeError with a clear message."""
    monkeypatch.setenv("ENTRA_BACKEND_CLIENT_SECRET", "")
    monkeypatch.setenv("ENTRA_BACKEND_CLIENT_SECRET_ARN", "")

    from core.secrets_loader import load_entra_backend_client_secret

    with pytest.raises(RuntimeError, match="ENTRA_BACKEND_CLIENT_SECRET"):
        load_entra_backend_client_secret()


def test_caches_result(monkeypatch):
    """Second call returns cached value without re-fetching."""
    monkeypatch.setenv("ENTRA_BACKEND_CLIENT_SECRET", "")
    monkeypatch.setenv(
        "ENTRA_BACKEND_CLIENT_SECRET_ARN",
        "arn:aws:secretsmanager:eu-west-1:123:secret:test-XXX",
    )

    fake_client = MagicMock()
    fake_client.get_secret_value.return_value = {"SecretString": "fresh"}

    with patch("boto3.client", return_value=fake_client):
        from core.secrets_loader import load_entra_backend_client_secret

        result1 = load_entra_backend_client_secret()
        result2 = load_entra_backend_client_secret()

    assert result1 == "fresh"
    assert result2 == "fresh"
    # Boto3 client called twice (cache is on the secret value, not the client),
    # but get_secret_value should be called exactly once.
    assert fake_client.get_secret_value.call_count == 1
