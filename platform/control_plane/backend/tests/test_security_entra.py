"""Unit tests for Microsoft Entra JWT validation."""

import datetime as dt
from unittest.mock import patch

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

# Reset module cache between tests so settings monkeypatches take effect.
@pytest.fixture(autouse=True)
def reset_module():
    import sys
    for mod in ["core.security_entra", "core.config"]:
        sys.modules.pop(mod, None)
    yield


# === Test fixtures ===

@pytest.fixture
def tenant_id():
    return "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def audience():
    return "api://agp"


@pytest.fixture
def spa_client_id():
    return "00000000-0000-0000-0000-000000000002"


@pytest.fixture
def backend_client_id():
    return "00000000-0000-0000-0000-000000000003"


@pytest.fixture
def rsa_key_pair():
    """Generate an RSA keypair for signing test tokens."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_key = private_key.public_key()
    return {
        "private_pem": private_pem,
        "public_key": public_key,
        "kid": "test-kid",
    }


@pytest.fixture
def make_token(rsa_key_pair, tenant_id, audience):
    """Factory: build a signed JWT with overridable claims."""
    def _make(**overrides):
        now = int(dt.datetime.now(dt.timezone.utc).timestamp())
        claims = {
            "iss": f"https://login.microsoftonline.com/{tenant_id}/v2.0",
            "aud": audience,
            "exp": now + 3600,
            "iat": now,
            "nbf": now,
            "oid": "user-oid-maria",
            "name": "Maria Bauer",
            "preferred_username": "maria.bauer@contoso.onmicrosoft.com",
            "roles": ["Platform.Operator"],
            "tid": tenant_id,
        }
        claims.update(overrides)
        # Strip None values so callers can drop claims with overrides={"roles": None}
        claims = {k: v for k, v in claims.items() if v is not None}
        return pyjwt.encode(
            claims,
            rsa_key_pair["private_pem"],
            algorithm="RS256",
            headers={"kid": rsa_key_pair["kid"]},
        )
    return _make


@pytest.fixture
def patched_jwks(rsa_key_pair):
    """Patch PyJWKClient so the validator uses our test public key."""
    # Build a fake signing key object with `.key` attribute matching what PyJWT's
    # signing_key.key would return.
    class FakeSigningKey:
        def __init__(self, key):
            self.key = key

    fake_key = FakeSigningKey(rsa_key_pair["public_key"])

    with patch("jwt.PyJWKClient.get_signing_key_from_jwt", return_value=fake_key):
        yield


@pytest.fixture
def configured_settings(monkeypatch, tenant_id, audience, spa_client_id, backend_client_id):
    """Set the env vars the validator reads."""
    monkeypatch.setenv("AUTH_PROVIDER", "entra")
    monkeypatch.setenv("ENTRA_TENANT_ID", tenant_id)
    monkeypatch.setenv("ENTRA_AUDIENCE", audience)
    monkeypatch.setenv("ENTRA_SPA_CLIENT_ID", spa_client_id)
    monkeypatch.setenv("ENTRA_BACKEND_CLIENT_ID", backend_client_id)


# === Tests ===

def test_valid_token_returns_claims(make_token, patched_jwks, configured_settings):
    """Happy path: a properly signed Entra token returns the claims dict."""
    from core.security_entra import verify_entra_token

    token = make_token()
    claims = verify_entra_token(token)

    assert claims["oid"] == "user-oid-maria"
    assert claims["name"] == "Maria Bauer"
    assert claims["preferred_username"] == "maria.bauer@contoso.onmicrosoft.com"
    assert claims["roles"] == ["Platform.Operator"]


def test_wrong_issuer_returns_401(make_token, patched_jwks, configured_settings):
    """Issuer mismatch (e.g., wrong tenant) is rejected."""
    from core.security_entra import verify_entra_token

    token = make_token(iss="https://login.microsoftonline.com/wrong-tenant/v2.0")

    with pytest.raises(HTTPException) as exc:
        verify_entra_token(token)
    assert exc.value.status_code == 401
    assert "issuer" in exc.value.detail.lower() or "iss" in exc.value.detail.lower()


def test_wrong_audience_returns_401(make_token, patched_jwks, configured_settings):
    """Audience mismatch is rejected."""
    from core.security_entra import verify_entra_token

    token = make_token(aud="api://different-app")

    with pytest.raises(HTTPException) as exc:
        verify_entra_token(token)
    assert exc.value.status_code == 401
    assert "audience" in exc.value.detail.lower() or "aud" in exc.value.detail.lower()


def test_aud_spa_client_id_is_accepted(
    make_token, patched_jwks, configured_settings, spa_client_id,
):
    """Legacy SPA-scope tokens carry aud=<SPA client-id GUID> — still accepted."""
    from core.security_entra import verify_entra_token

    token = make_token(aud=spa_client_id)
    claims = verify_entra_token(token)
    assert claims["aud"] == spa_client_id


def test_aud_backend_client_id_is_accepted(
    make_token, patched_jwks, configured_settings, backend_client_id,
):
    """v2.0 tokens for the backend app's own exposed scope carry
    aud=<backend client-id GUID> (the current frontend path) — must be accepted."""
    from core.security_entra import verify_entra_token

    token = make_token(aud=backend_client_id)
    claims = verify_entra_token(token)
    assert claims["aud"] == backend_client_id


def test_expired_token_returns_401(make_token, patched_jwks, configured_settings):
    """An expired token is rejected."""
    from core.security_entra import verify_entra_token

    past = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).timestamp())
    token = make_token(exp=past, iat=past - 60, nbf=past - 60)

    with pytest.raises(HTTPException) as exc:
        verify_entra_token(token)
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail.lower()


def test_token_expired_within_leeway_is_accepted(make_token, patched_jwks, configured_settings):
    """E36/T17: a token that expired 30 s ago is still accepted — clock-skew leeway.

    The default JWT_LEEWAY_SECONDS is 60, so a container whose clock runs up to a
    minute fast no longer answers every request with a blanket 401. This pins the
    default: no env override is set, so the setting's own value is what passes.
    """
    from core.security_entra import verify_entra_token

    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    token = make_token(exp=now - 30, iat=now - 3600, nbf=now - 3600)

    claims = verify_entra_token(token)
    assert claims["oid"] == "user-oid-maria"


def test_token_expired_beyond_leeway_is_rejected(make_token, patched_jwks, configured_settings):
    """E36/T17: leeway is a 60 s grace, not an amnesty — 120 s past expiry still 401s."""
    from core.security_entra import verify_entra_token

    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    token = make_token(exp=now - 120, iat=now - 3600, nbf=now - 3600)

    with pytest.raises(HTTPException) as exc:
        verify_entra_token(token)
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail.lower()


def test_missing_role_claim_does_not_fail_validation(
    make_token, patched_jwks, configured_settings,
):
    """Missing `roles` claim is allowed at validation; role mapping (rbac.py) handles default-to-viewer."""
    from core.security_entra import verify_entra_token

    token = make_token(roles=None)  # remove the roles claim (None is stripped by make_token)

    claims = verify_entra_token(token)
    assert "roles" not in claims or claims.get("roles") is None


def test_garbage_token_returns_401(configured_settings):
    """A non-JWT string fails validation cleanly."""
    from core.security_entra import verify_entra_token

    with pytest.raises(HTTPException) as exc:
        verify_entra_token("not-a-jwt")
    assert exc.value.status_code == 401


def test_missing_tenant_id_raises_runtime_error(monkeypatch):
    """If ENTRA_TENANT_ID is blank, validation can't even start — fail loud."""
    monkeypatch.setenv("AUTH_PROVIDER", "entra")
    monkeypatch.setenv("ENTRA_TENANT_ID", "")
    monkeypatch.setenv("ENTRA_AUDIENCE", "api://agp")

    from core.security_entra import verify_entra_token

    with pytest.raises(RuntimeError, match="ENTRA_TENANT_ID"):
        verify_entra_token("any-token")
