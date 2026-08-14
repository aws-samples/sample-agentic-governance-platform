"""Tests for services/github_app_auth.py — App JWT signing + installation-token mint.

Offline: a throwaway RSA keypair is generated per test via ``cryptography`` (no fixed
key material committed), and the token-mint POST is served by an ``httpx.MockTransport``
(NO live GitHub). Determinism: ``now_epoch`` is passed explicitly — no wall clock.
"""

import jwt
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from services.github_app_auth import (
    GITHUB_DEFAULT_BASE,
    GitHubAppAuthError,
    build_app_jwt,
    mint_installation_token,
)

FIXED_EPOCH = 1_700_000_000  # arbitrary fixed instant — determinism, not "now"


def _rsa_pem():
    """Generate a throwaway RSA keypair; return (private_pem_str, public_key)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return private_pem, key.public_key()


def test_build_app_jwt_is_decodable_rs256_with_right_claims():
    private_pem, public_key = _rsa_pem()

    token = build_app_jwt("123456", private_pem, now_epoch=FIXED_EPOCH)

    # Verifies the RS256 signature against the public key (skip exp so the fixed
    # epoch doesn't trip expiry) and asserts the clock-skew-safe iss/iat/exp claims.
    claims = jwt.decode(
        token, public_key, algorithms=["RS256"], options={"verify_exp": False}
    )
    assert claims["iss"] == "123456"
    assert claims["iat"] == FIXED_EPOCH - 60
    assert claims["exp"] == FIXED_EPOCH + 540
    # exp window is ≤ 10 minutes (GitHub's hard cap).
    assert claims["exp"] - claims["iat"] <= 600


def test_mint_installation_token_posts_to_right_url_and_returns_token():
    private_pem, _ = _rsa_pem()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["accept"] = request.headers.get("Accept")
        return httpx.Response(201, json={"token": "ghs_minted_abc", "expires_at": "2026-01-01T00:00:00Z"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        token = mint_installation_token(
            "123456", "78910", private_pem, client=client, base_url=None, now_epoch=FIXED_EPOCH
        )
    finally:
        client.close()

    assert token == "ghs_minted_abc"
    assert seen["url"] == f"{GITHUB_DEFAULT_BASE}/app/installations/78910/access_tokens"
    assert seen["auth"].startswith("Bearer ")
    assert seen["accept"] == "application/vnd.github+json"


def test_mint_uses_custom_base_url_for_ghe():
    private_pem, _ = _rsa_pem()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(201, json={"token": "ghs_ghe"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        token = mint_installation_token(
            "1", "2", private_pem,
            client=client, base_url="https://ghe.example.com/api/v3/", now_epoch=FIXED_EPOCH,
        )
    finally:
        client.close()

    assert token == "ghs_ghe"
    assert seen["url"] == "https://ghe.example.com/api/v3/app/installations/2/access_tokens"


def test_mint_non_201_raises_safe_error():
    private_pem, _ = _rsa_pem()

    def handler(request: httpx.Request) -> httpx.Response:
        # A body that echoes secret-looking content — must NEVER leak into the message.
        return httpx.Response(404, json={"message": "Not Found", "secret_leak": private_pem})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(GitHubAppAuthError) as ei:
            mint_installation_token(
                "1", "2", private_pem, client=client, base_url=None, now_epoch=FIXED_EPOCH
            )
    finally:
        client.close()

    msg = str(ei.value)
    assert "404" in msg
    assert private_pem not in msg
    assert "BEGIN" not in msg  # no PEM material


def test_mint_transport_error_raises_safe_error():
    private_pem, _ = _rsa_pem()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(GitHubAppAuthError) as ei:
            mint_installation_token(
                "1", "2", private_pem, client=client, base_url=None, now_epoch=FIXED_EPOCH
            )
    finally:
        client.close()

    assert private_pem not in str(ei.value)
