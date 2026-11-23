"""The user-OAuth transport over ``httpx.MockTransport``: the PKCE and authorize URL seams, the two
token legs, the identity probe and the grant revocation, and the contract that every failure is a
``GitHubOAuthError`` carrying no body text."""

import base64
import hashlib
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from services.github_user_oauth import (
    GITHUB_API_BASE,
    GITHUB_WEB_BASE,
    GitHubOAuthError,
    build_authorize_url,
    build_pkce_challenge,
    exchange_code,
    fetch_user_identity,
    refresh_user_token,
    revoke_grant,
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _exchange(handler, **over):
    kwargs: dict[str, Any] = dict(
        client_id="Iv1.abc",
        client_secret="s3cr3t",
        code="c",
        redirect_uri="https://x/cb",
        code_verifier="v",
        client=_client(handler),
    )
    kwargs.update(over)
    return exchange_code(**kwargs)


def test_pkce_challenge_is_43_char_url_safe_s256():
    ch = build_pkce_challenge("a" * 43)
    assert len(ch) == 43
    assert "=" not in ch and "+" not in ch and "/" not in ch


def test_pkce_challenge_is_base64url_sha256_of_the_verifier():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    assert build_pkce_challenge(verifier) == expected


def test_authorize_url_has_the_five_params_and_no_scope():
    url = build_authorize_url(
        client_id="Iv1.abc",
        redirect_uri="https://x/ops/github-link/callback",
        state="st-1",
        code_challenge="c" * 43,
    )
    assert url.startswith("https://github.com/login/oauth/authorize?")
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == ["Iv1.abc"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["redirect_uri"] == ["https://x/ops/github-link/callback"]
    assert q["state"] == ["st-1"]
    assert q["code_challenge"] == ["c" * 43]
    assert "scope" not in q


def test_authorize_url_percent_encodes_every_value():
    url = build_authorize_url(
        client_id="Iv1.a b", redirect_uri="https://x/cb?a=1&b=2", state="st/a te+1", code_challenge="c/d+e="
    )
    assert " " not in url
    q = parse_qs(urlparse(url).query)
    assert q["redirect_uri"] == ["https://x/cb?a=1&b=2"]
    assert q["state"] == ["st/a te+1"]
    assert q["code_challenge"] == ["c/d+e="]


def test_exchange_code_normalizes_the_token_payload():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["method"] = req.method
        seen["accept"] = req.headers["Accept"]
        seen["form"] = parse_qs(req.content.decode())
        return httpx.Response(
            200,
            json={
                "access_token": "ghu_abc",
                "refresh_token": "ghr_abc",
                "expires_in": 28800,
                "refresh_token_expires_in": 15897600,
                "scope": "",
                "token_type": "bearer",
            },
        )

    out = _exchange(
        handler, code="tempcode", redirect_uri="https://x/ops/github-link/callback", code_verifier="v" * 43
    )
    assert out == {
        "access_token": "ghu_abc",
        "refresh_token": "ghr_abc",
        "expires_in": 28800,
        "refresh_token_expires_in": 15897600,
    }
    assert seen["url"] == f"{GITHUB_WEB_BASE}/login/oauth/access_token"
    assert seen["method"] == "POST"
    assert seen["accept"] == "application/json"
    assert seen["form"]["client_id"] == ["Iv1.abc"]
    assert seen["form"]["client_secret"] == ["s3cr3t"]
    assert seen["form"]["code"] == ["tempcode"]
    assert seen["form"]["redirect_uri"] == ["https://x/ops/github-link/callback"]
    assert seen["form"]["code_verifier"] == ["v" * 43]
    assert "scope" not in seen["form"]


def test_exchange_code_treats_http_200_with_error_as_failure():
    def handler(req):
        return httpx.Response(
            200,
            json={
                "error": "bad_verification_code",
                "error_description": "The code passed is incorrect or expired.",
            },
        )

    with pytest.raises(GitHubOAuthError) as ei:
        _exchange(handler, code="stale")
    assert ei.value.kind == "bad_grant"


def test_exchange_code_200_without_access_token_is_a_failure():
    def handler(req):
        return httpx.Response(200, json={"token_type": "bearer"})

    with pytest.raises(GitHubOAuthError) as ei:
        _exchange(handler)
    assert ei.value.kind == "provider_error"


def test_exchange_code_unknown_error_name_is_provider_error():
    """A documented error name may be surfaced; the body may not."""

    def handler(req):
        return httpx.Response(200, json={"error": "incorrect_client_credentials"})

    with pytest.raises(GitHubOAuthError) as ei:
        _exchange(handler)
    assert ei.value.kind == "provider_error"
    assert "incorrect_client_credentials" in str(ei.value)


def test_exchange_code_never_leaks_the_secret_or_the_body():
    def handler(req):
        return httpx.Response(
            200,
            json={
                "error": "bad_verification_code",
                "error_description": "leak-me",
                "error_uri": "https://leak-me.example.com",
            },
        )

    with pytest.raises(GitHubOAuthError) as ei:
        _exchange(handler, code="tempcode", code_verifier="v" * 43)
    msg = str(ei.value)
    assert "s3cr3t" not in msg and "leak-me" not in msg and "tempcode" not in msg


def test_exchange_code_non_200_is_safe_and_carries_only_the_status():
    def handler(req):
        return httpx.Response(503, json={"message": "sensitive detail"})

    with pytest.raises(GitHubOAuthError) as ei:
        _exchange(handler)
    assert ei.value.kind == "provider_error"
    assert "503" in str(ei.value)
    assert "sensitive" not in str(ei.value)


def test_exchange_code_transport_failure_drops_the_chained_exception():
    def handler(req):
        raise httpx.ConnectError("connecting to https://github.com/...?client_secret=s3cr3t")

    with pytest.raises(GitHubOAuthError) as ei:
        _exchange(handler)
    assert "ConnectError" in str(ei.value)
    assert "s3cr3t" not in str(ei.value)
    assert ei.value.__cause__ is None
    assert ei.value.kind == "provider_error"


def test_exchange_code_non_json_body_is_safe():
    def handler(req):
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(GitHubOAuthError) as ei:
        _exchange(handler)
    assert "html" not in str(ei.value)


def test_refresh_user_token_sends_the_refresh_grant():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["form"] = parse_qs(req.content.decode())
        return httpx.Response(
            200,
            json={
                "access_token": "ghu_new",
                "refresh_token": "ghr_new",
                "expires_in": 28800,
                "refresh_token_expires_in": 15897600,
            },
        )

    out = refresh_user_token(
        client_id="Iv1.abc", client_secret="s3cr3t", refresh_token="ghr_old", client=_client(handler)
    )
    assert out == {
        "access_token": "ghu_new",
        "refresh_token": "ghr_new",
        "expires_in": 28800,
        "refresh_token_expires_in": 15897600,
    }
    assert seen["url"] == f"{GITHUB_WEB_BASE}/login/oauth/access_token"
    assert seen["form"]["grant_type"] == ["refresh_token"]
    assert seen["form"]["refresh_token"] == ["ghr_old"]
    assert seen["form"]["client_id"] == ["Iv1.abc"]
    assert seen["form"]["client_secret"] == ["s3cr3t"]


def test_refresh_missing_refresh_token_fields_is_a_non_expiring_grant():
    """An App with "Expire user authorization tokens" deselected omits every expiry field."""

    def handler(req):
        return httpx.Response(200, json={"access_token": "ghu_forever", "token_type": "bearer"})

    out = refresh_user_token(
        client_id="Iv1.abc", client_secret="s3cr3t", refresh_token="ghr_old", client=_client(handler)
    )
    assert out["access_token"] == "ghu_forever"
    assert out["refresh_token"] is None and out["expires_in"] is None
    assert out["refresh_token_expires_in"] is None


def test_refresh_bad_refresh_token_is_bad_grant():
    def handler(req):
        return httpx.Response(200, json={"error": "bad_refresh_token"})

    with pytest.raises(GitHubOAuthError) as ei:
        refresh_user_token(
            client_id="Iv1.abc", client_secret="s3cr3t", refresh_token="ghr_dead", client=_client(handler)
        )
    assert ei.value.kind == "bad_grant"


def test_refresh_never_leaks_the_secret_or_the_refresh_token():
    def handler(req):
        return httpx.Response(200, json={"error": "bad_refresh_token", "error_description": "leak-me"})

    with pytest.raises(GitHubOAuthError) as ei:
        refresh_user_token(
            client_id="Iv1.abc", client_secret="s3cr3t", refresh_token="ghr_dead", client=_client(handler)
        )
    msg = str(ei.value)
    assert "s3cr3t" not in msg and "ghr_dead" not in msg and "leak-me" not in msg


def test_fetch_user_identity_returns_numeric_id_and_login():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["method"] = req.method
        seen["auth"] = req.headers["Authorization"]
        seen["accept"] = req.headers["Accept"]
        return httpx.Response(200, json={"id": 583231, "login": "octocat", "name": "The Octocat"})

    out = fetch_user_identity("ghu_abc", client=_client(handler))
    assert out == {"github_id": 583231, "github_login": "octocat"}
    assert seen["url"] == f"{GITHUB_API_BASE}/user"
    assert seen["method"] == "GET"
    assert seen["auth"] == "Bearer ghu_abc"
    assert seen["accept"] == "application/vnd.github+json"


def test_fetch_user_identity_401_is_revoked():
    def handler(req):
        return httpx.Response(401, json={"message": "Bad credentials"})

    with pytest.raises(GitHubOAuthError) as ei:
        fetch_user_identity("ghu_dead", client=_client(handler))
    assert ei.value.kind == "revoked"


def test_fetch_user_identity_ignores_email():
    def handler(req):
        return httpx.Response(200, json={"id": 1, "login": "octocat", "email": None})

    out = fetch_user_identity("ghu_abc", client=_client(handler))
    assert out == {"github_id": 1, "github_login": "octocat"}
    assert "email" not in out


def test_fetch_user_identity_incomplete_response_raises():
    def handler(req):
        return httpx.Response(200, json={"login": "octocat"})

    with pytest.raises(GitHubOAuthError) as ei:
        fetch_user_identity("ghu_abc", client=_client(handler))
    assert ei.value.kind == "provider_error"


def test_fetch_user_identity_non_200_is_safe():
    def handler(req):
        return httpx.Response(500, json={"message": "sensitive detail"})

    with pytest.raises(GitHubOAuthError) as ei:
        fetch_user_identity("ghu_abc", client=_client(handler))
    assert ei.value.kind == "provider_error"
    assert "500" in str(ei.value)
    assert "sensitive" not in str(ei.value)


def test_fetch_user_identity_never_leaks_the_token():
    def handler(req):
        raise httpx.ConnectError("connect to api.github.com?token=ghu_leakme failed")

    with pytest.raises(GitHubOAuthError) as ei:
        fetch_user_identity("ghu_leakme", client=_client(handler))
    assert "ghu_leakme" not in str(ei.value)
    assert "ConnectError" in str(ei.value)
    assert ei.value.__cause__ is None


@pytest.mark.parametrize("status", [204, 404, 422])
def test_revoke_grant_204_and_404_and_422_all_succeed(status):
    def handler(req):
        return httpx.Response(status)

    assert (
        revoke_grant(
            client_id="Iv1.abc", client_secret="s3cr3t", access_token="ghu_abc", client=_client(handler)
        )
        is None
    )


def test_revoke_grant_uses_basic_auth_with_client_id_and_secret():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["method"] = req.method
        seen["auth"] = req.headers["Authorization"]
        seen["body"] = req.content.decode()
        return httpx.Response(204)

    revoke_grant(client_id="Iv1.abc", client_secret="s3cr3t", access_token="ghu_abc", client=_client(handler))
    assert seen["url"] == f"{GITHUB_API_BASE}/applications/Iv1.abc/grant"
    assert seen["method"] == "DELETE"
    expected = base64.b64encode(b"Iv1.abc:s3cr3t").decode("ascii")
    assert seen["auth"] == f"Basic {expected}"
    assert '"access_token": "ghu_abc"' in seen["body"] or '"access_token":"ghu_abc"' in seen["body"]


def test_revoke_grant_percent_encodes_the_client_id_in_the_path():
    """``raw_path`` is what goes on the wire; ``path`` percent-decodes."""
    seen = {}

    def handler(req):
        seen["raw_path"] = req.url.raw_path
        return httpx.Response(204)

    revoke_grant(client_id="Iv1.a/b", client_secret="s", access_token="ghu_abc", client=_client(handler))
    assert seen["raw_path"] == b"/applications/Iv1.a%2Fb/grant"


def test_revoke_grant_500_raises_without_the_body():
    def handler(req):
        return httpx.Response(500, json={"message": "sensitive detail"})

    with pytest.raises(GitHubOAuthError) as ei:
        revoke_grant(
            client_id="Iv1.abc", client_secret="s3cr3t", access_token="ghu_abc", client=_client(handler)
        )
    assert ei.value.kind == "provider_error"
    assert "500" in str(ei.value)
    assert "sensitive" not in str(ei.value)
    assert "s3cr3t" not in str(ei.value) and "ghu_abc" not in str(ei.value)


def test_revoke_grant_transport_failure_is_safe():
    def handler(req):
        raise httpx.ConnectError("https://api.github.com/applications/Iv1.abc/grant s3cr3t")

    with pytest.raises(GitHubOAuthError) as ei:
        revoke_grant(
            client_id="Iv1.abc", client_secret="s3cr3t", access_token="ghu_abc", client=_client(handler)
        )
    assert "ConnectError" in str(ei.value)
    assert "s3cr3t" not in str(ei.value)
    assert ei.value.__cause__ is None


def test_fetch_user_identity_non_integer_id_raises_oauth_error_without_body_text():
    """A coerced ``int(github_id)`` would raise a bare ``ValueError`` quoting the body."""

    def handler(req):
        return httpx.Response(200, json={"id": "0xdeadbeef-SECRET-ISH", "login": "octocat"})

    with pytest.raises(GitHubOAuthError) as ei:
        fetch_user_identity("ghu_abc", client=_client(handler))
    assert not isinstance(ei.value, (ValueError, TypeError))
    assert ei.value.kind == "provider_error"
    assert "0xdeadbeef" not in str(ei.value) and "SECRET" not in str(ei.value)


def test_fetch_user_identity_rejects_a_string_digit_id():
    def handler(req):
        return httpx.Response(200, json={"id": "583231", "login": "octocat"})

    with pytest.raises(GitHubOAuthError):
        fetch_user_identity("ghu_abc", client=_client(handler))


def test_fetch_user_identity_non_string_login_raises_instead_of_stringifying():
    def handler(req):
        return httpx.Response(200, json={"id": 5, "login": {"nested": "PWN"}})

    with pytest.raises(GitHubOAuthError) as ei:
        fetch_user_identity("ghu_abc", client=_client(handler))
    assert not isinstance(ei.value, (ValueError, TypeError))
    assert "PWN" not in str(ei.value) and "nested" not in str(ei.value)


def test_fetch_user_identity_rejects_a_bool_id():
    def handler(req):
        return httpx.Response(200, json={"id": True, "login": "octocat"})

    with pytest.raises(GitHubOAuthError):
        fetch_user_identity("ghu_abc", client=_client(handler))


def test_token_error_field_that_is_not_a_string_raises_oauth_error():
    """A dict ``error`` would make the frozenset membership test raise an unhashable-type
    ``TypeError``, escaping the contract."""

    def handler(req):
        return httpx.Response(200, json={"error": {"leak": "body-detail"}})

    with pytest.raises(GitHubOAuthError) as ei:
        _exchange(handler)
    assert not isinstance(ei.value, (ValueError, TypeError))
    assert ei.value.kind == "provider_error"
    assert "body-detail" not in str(ei.value) and "leak" not in str(ei.value)


def test_token_error_field_that_is_a_list_raises_oauth_error():
    def handler(req):
        return httpx.Response(200, json={"error": ["bad_verification_code"]})

    with pytest.raises(GitHubOAuthError) as ei:
        _exchange(handler)
    assert not isinstance(ei.value, (ValueError, TypeError))
    assert ei.value.kind == "provider_error"


def test_unknown_error_name_is_never_echoed_and_carries_no_newline():
    """An interpolated raw ``error`` would let a newline-bearing value forge a log line."""
    forged = "bad_verification_code\nFAKE-LOG-LINE actor=admin promoted=true " + "A" * 200

    def handler(req):
        return httpx.Response(200, json={"error": forged})

    with pytest.raises(GitHubOAuthError) as ei:
        _exchange(handler)
    msg = str(ei.value)
    assert "\n" not in msg
    assert "FAKE-LOG-LINE" not in msg and "AAAA" not in msg
    assert "an unrecognized error" in msg
    assert ei.value.kind == "provider_error"


def test_a_documented_error_name_is_still_echoed():
    def handler(req):
        return httpx.Response(200, json={"error": "bad_verification_code"})

    with pytest.raises(GitHubOAuthError) as ei:
        _exchange(handler)
    assert "bad_verification_code" in str(ei.value)
    assert ei.value.kind == "bad_grant"


def test_non_string_access_token_raises_instead_of_reaching_the_secret_body():
    def handler(req):
        return httpx.Response(200, json={"access_token": {"pwn": "x"}, "token_type": "bearer"})

    with pytest.raises(GitHubOAuthError) as ei:
        _exchange(handler)
    assert not isinstance(ei.value, (ValueError, TypeError))
    assert "pwn" not in str(ei.value)


def test_non_integer_lifetimes_normalize_to_none():
    def handler(req):
        return httpx.Response(
            200,
            json={
                "access_token": "ghu_new",
                "expires_in": "not-an-int",
                "refresh_token": {"nope": 1},
                "refresh_token_expires_in": True,
            },
        )

    out = _exchange(handler)
    assert out == {
        "access_token": "ghu_new",
        "refresh_token": None,
        "expires_in": None,
        "refresh_token_expires_in": None,
    }


def test_oauth_error_defaults_to_provider_error():
    assert GitHubOAuthError("nope").kind == "provider_error"
    assert str(GitHubOAuthError("nope")) == "nope"


def test_module_bases_are_the_pinned_literals():
    assert GITHUB_WEB_BASE == "https://github.com"
    assert GITHUB_API_BASE == "https://api.github.com"
