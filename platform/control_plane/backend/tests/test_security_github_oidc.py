"""Unit tests for GitHub Actions OIDC JWT validation (E22/T6).

Mirrors test_security_entra.py: a real RSA keypair signs test tokens, PyJWKClient is
patched to return our public key, settings come from monkeypatched env.
"""

import datetime as dt
from unittest.mock import patch

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def reset_module():
    import sys
    for mod in ["core.security_github_oidc", "core.config"]:
        sys.modules.pop(mod, None)
    yield


@pytest.fixture
def issuer():
    return "https://token.actions.githubusercontent.com"


@pytest.fixture
def audience():
    return "agp-runtime-build"


@pytest.fixture
def rsa_key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return {"private_pem": private_pem, "public_key": private_key.public_key(), "kid": "gh-kid"}


@pytest.fixture
def make_token(rsa_key_pair, issuer, audience):
    def _make(**overrides):
        now = int(dt.datetime.now(dt.timezone.utc).timestamp())
        claims = {
            "iss": issuer,
            "aud": audience,
            "exp": now + 3600,
            "iat": now,
            "nbf": now,
            "sub": "repo:acme-org/agp-runtime-infra:ref:refs/heads/main",
            "repository": "acme-org/some-agent",
            "repository_owner": "acme-org",
            "ref": "refs/heads/main",
            "sha": "deadbeef",
            "workflow": "build",
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return pyjwt.encode(
            claims, rsa_key_pair["private_pem"], algorithm="RS256",
            headers={"kid": rsa_key_pair["kid"]},
        )
    return _make


@pytest.fixture
def patched_jwks(rsa_key_pair):
    class FakeSigningKey:
        def __init__(self, key):
            self.key = key
    fake_key = FakeSigningKey(rsa_key_pair["public_key"])
    with patch("jwt.PyJWKClient.get_signing_key_from_jwt", return_value=fake_key):
        yield


@pytest.fixture
def configured_settings(monkeypatch, issuer, audience):
    monkeypatch.setenv("GITHUB_OIDC_ISSUER", issuer)
    monkeypatch.setenv("GITHUB_OIDC_AUDIENCE", audience)


def test_valid_token_returns_claims(make_token, patched_jwks, configured_settings):
    from core.security_github_oidc import verify_github_oidc_token

    claims = verify_github_oidc_token(make_token())
    assert claims.repository == "acme-org/some-agent"
    assert claims.repository_owner == "acme-org"
    assert claims.ref == "refs/heads/main"


def test_immutable_id_subject_customization_claim_shapes(
    make_token, patched_jwks, configured_settings
):
    """Orgs with immutable-ID subject customization mint tokens whose ``sub`` carries the
    ``@<orgId>``/``@<repoId>`` suffixes, but ``repository_owner`` remains the bare login
    (per GitHub OIDC docs the ``@id`` form appears only in ``sub``). This documents the
    claim shapes we accept and confirms the route's ``repository_owner``-vs-org check still
    sees the plain login, so it keeps working for these orgs (no route change needed)."""
    from core.security_github_oidc import verify_github_oidc_token

    token = make_token(
        sub="repo:AgenticOps-Platform@296866902/my-test-agent@1302865558:ref:refs/heads/main",
        repository="AgenticOps-Platform/my-test-agent",
        repository_owner="AgenticOps-Platform",  # login only — the @orgId lives in sub
    )
    claims = verify_github_oidc_token(token)
    # repository_owner is the bare login → the route's `gh.repository_owner != conn.org`
    # gate still matches a connection stored as "AgenticOps-Platform".
    assert claims.repository_owner == "AgenticOps-Platform"
    assert claims.repository == "AgenticOps-Platform/my-test-agent"
    # The immutable-ID markers are confined to the sub claim.
    assert claims.sub == (
        "repo:AgenticOps-Platform@296866902/my-test-agent@1302865558:ref:refs/heads/main"
    )
    assert "@296866902" not in claims.repository_owner


def test_wrong_issuer_returns_401(make_token, patched_jwks, configured_settings):
    from core.security_github_oidc import verify_github_oidc_token

    token = make_token(iss="https://evil.example.com")
    with pytest.raises(HTTPException) as exc:
        verify_github_oidc_token(token)
    assert exc.value.status_code == 401
    assert "issuer" in exc.value.detail.lower()


def test_wrong_audience_returns_401(make_token, patched_jwks, configured_settings):
    from core.security_github_oidc import verify_github_oidc_token

    token = make_token(aud="some-other-aud")
    with pytest.raises(HTTPException) as exc:
        verify_github_oidc_token(token)
    assert exc.value.status_code == 401
    assert "audience" in exc.value.detail.lower()


def test_expired_token_returns_401(make_token, patched_jwks, configured_settings):
    from core.security_github_oidc import verify_github_oidc_token

    past = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).timestamp())
    token = make_token(exp=past, iat=past - 60, nbf=past - 60)
    with pytest.raises(HTTPException) as exc:
        verify_github_oidc_token(token)
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail.lower()


def test_token_expired_within_leeway_is_accepted(make_token, patched_jwks, configured_settings):
    """E36/T17: a token that expired 30 s ago is still accepted — clock-skew leeway.

    Same shared JWT_LEEWAY_SECONDS=60 as the Entra path: skew here breaks deploys
    (the `POST /builds/runtime` trigger), not logins. No env override is set, so this
    pins the setting's default value.
    """
    from core.security_github_oidc import verify_github_oidc_token

    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    token = make_token(exp=now - 30, iat=now - 3600, nbf=now - 3600)

    claims = verify_github_oidc_token(token)
    assert claims.repository == "acme-org/some-agent"


def test_token_expired_beyond_leeway_is_rejected(make_token, patched_jwks, configured_settings):
    """E36/T17: leeway is a 60 s grace, not an amnesty — 120 s past expiry still 401s."""
    from core.security_github_oidc import verify_github_oidc_token

    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    token = make_token(exp=now - 120, iat=now - 3600, nbf=now - 3600)

    with pytest.raises(HTTPException) as exc:
        verify_github_oidc_token(token)
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail.lower()


def test_missing_repository_owner_returns_401(make_token, patched_jwks, configured_settings):
    from core.security_github_oidc import verify_github_oidc_token

    token = make_token(repository_owner=None)  # stripped by make_token
    with pytest.raises(HTTPException) as exc:
        verify_github_oidc_token(token)
    assert exc.value.status_code == 401


def test_garbage_token_returns_401(configured_settings):
    from core.security_github_oidc import verify_github_oidc_token

    with pytest.raises(HTTPException) as exc:
        verify_github_oidc_token("not-a-jwt")
    assert exc.value.status_code == 401


def test_captures_actor_and_event_name(make_token, patched_jwks, configured_settings):
    """E27A: "who merged to main" is OIDC-proven, not body-asserted — GitHub mints `actor`
    and `event_name` on every Actions token, so the validator must carry them through."""
    from core.security_github_oidc import verify_github_oidc_token

    token = make_token(actor="jorge", event_name="push")
    claims = verify_github_oidc_token(token)
    assert claims.actor == "jorge"
    assert claims.event_name == "push"


def test_actor_and_event_name_are_optional(make_token, patched_jwks, configured_settings):
    """Both stay optional — a token minted without them still verifies (a missing claim is
    not an auth failure), same as the existing ref/sha/workflow fields."""
    from core.security_github_oidc import verify_github_oidc_token

    token = make_token(actor=None, event_name=None)  # stripped by make_token
    claims = verify_github_oidc_token(token)
    assert claims.actor is None
    assert claims.event_name is None
    # ...and the token is still fully valid otherwise.
    assert claims.repository == "acme-org/some-agent"
