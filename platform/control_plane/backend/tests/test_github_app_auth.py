"""The App JWT and the installation-token mint, offline: a throwaway RSA keypair per test and an
``httpx.MockTransport`` for the mint; ``now_epoch`` is passed explicitly."""

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from services.github_app_auth import (
    GITHUB_DEFAULT_BASE,
    GitHubAppAuthError,
    build_app_jwt,
    mint_installation_token,
)

FIXED_EPOCH = 1_700_000_000


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

    claims = jwt.decode(token, public_key, algorithms=["RS256"], options={"verify_exp": False})
    assert claims["iss"] == "123456"
    assert claims["iat"] == FIXED_EPOCH - 60
    assert claims["exp"] == FIXED_EPOCH + 540


def test_build_app_jwt_stays_inside_githubs_ten_minute_cap():
    private_pem, public_key = _rsa_pem()

    token = build_app_jwt("123456", private_pem, now_epoch=FIXED_EPOCH)

    claims = jwt.decode(token, public_key, algorithms=["RS256"], options={"verify_exp": False})
    assert claims["exp"] - claims["iat"] <= 600


def test_build_app_jwt_wraps_an_unparseable_private_key_in_the_documented_error():
    with pytest.raises(GitHubAppAuthError) as ei:
        build_app_jwt("123456", "not-a-pem", now_epoch=FIXED_EPOCH)

    assert str(ei.value) == "the App private key could not be parsed"
    assert ei.value.__cause__ is None


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
            "1",
            "2",
            private_pem,
            client=client,
            base_url="https://ghe.example.com/api/v3/",
            now_epoch=FIXED_EPOCH,
        )
    finally:
        client.close()

    assert token == "ghs_ghe"
    assert seen["url"] == "https://ghe.example.com/api/v3/app/installations/2/access_tokens"


def test_mint_non_201_raises_safe_error():
    """A body that echoes key material never reaches the message; only the status does."""
    private_pem, _ = _rsa_pem()

    def handler(request: httpx.Request) -> httpx.Response:
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
    assert "BEGIN" not in msg


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
