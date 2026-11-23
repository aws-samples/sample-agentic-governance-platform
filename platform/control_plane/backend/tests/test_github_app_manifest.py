"""The App-via-Manifest flow over ``httpx.MockTransport``: manifest shape, the conversion
exchange, the installation lookup and the client-id read; a throwaway RSA key per test."""

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from services.github_app_manifest import (
    MANIFEST_PERMISSIONS,
    GitHubManifestError,
    build_app_name,
    build_manifest,
    convert_manifest_code,
    fetch_app_client_id,
    register_url,
    resolve_installation_id,
)

NOW = 1_700_000_000


def _gen_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_build_manifest_shape():
    m = build_manifest("acme", "https://app.example.com/ops/connections/callback")
    assert m["name"] == "agp-acme"
    assert m["redirect_url"] == "https://app.example.com/ops/connections/callback"
    assert m["public"] is False
    assert m["url"] == "https://app.example.com/ops/connections/callback"
    assert m["default_permissions"] == MANIFEST_PERMISSIONS


def test_build_manifest_omits_hook_attributes_because_github_requires_a_url_inside_it():
    m = build_manifest("acme", "https://app.example.com/ops/connections/callback")
    assert "hook_attributes" not in m


def test_the_permission_set_carries_githubs_machine_keys():
    """``workflows`` is what lets the scaffold push a ``.github/workflows`` file and
    ``actions_variables`` is the repository Variables permission; ``variables`` is a spelling
    GitHub's manifest validator rejects."""
    assert MANIFEST_PERMISSIONS["administration"] == "write"
    assert MANIFEST_PERMISSIONS["metadata"] == "read"
    assert MANIFEST_PERMISSIONS["workflows"] == "write"
    assert MANIFEST_PERMISSIONS["actions_variables"] == "write"
    assert "variables" not in MANIFEST_PERMISSIONS


def test_app_name_within_github_34_char_cap():
    assert build_app_name("AgenticOps-Platform") == "agp-AgenticOps-Platform"
    assert len(build_app_name("AgenticOps-Platform")) <= 34
    assert len(build_app_name("x" * 60)) == 34
    m = build_manifest("AgenticOps-Platform", "https://app.example.com/ops/connections/callback")
    assert m["name"] == "agp-AgenticOps-Platform" and len(m["name"]) <= 34


def test_register_url_is_org_scoped_and_encodes_state():
    url = register_url("acme", "st/a te+1")
    assert url.startswith("https://github.com/organizations/acme/settings/apps/new?state=")
    assert " " not in url and "/a te" not in url


def test_convert_manifest_code_returns_captured_fields():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/app-manifests/tempcode/conversions"
        assert req.method == "POST"
        return httpx.Response(
            201,
            json={
                "id": 424242,
                "slug": "agp-acme-provisioning",
                "pem": _gen_pem(),
                "webhook_secret": "whsec_abc",
                "client_id": "Iv1.x",
            },
        )

    out = convert_manifest_code("tempcode", client=_client(handler), base_url=None)
    assert out["app_id"] == "424242"
    assert out["slug"] == "agp-acme-provisioning"
    assert out["webhook_secret"] == "whsec_abc"
    assert out["pem"].startswith("-----BEGIN")


def test_convert_manifest_code_non_201_is_safe():
    def handler(req):
        return httpx.Response(422, json={"message": "sensitive detail"})

    with pytest.raises(GitHubManifestError) as ei:
        convert_manifest_code("bad", client=_client(handler), base_url=None)
    assert "422" in str(ei.value)
    assert "sensitive" not in str(ei.value)


def test_convert_manifest_code_incomplete_is_safe():
    def handler(req):
        return httpx.Response(201, json={"id": 1})

    with pytest.raises(GitHubManifestError) as ei:
        convert_manifest_code("c", client=_client(handler), base_url=None)
    assert "incomplete" in str(ei.value)


def test_resolve_installation_id_matches_org_case_insensitive():
    pem = _gen_pem()

    def handler(req):
        assert req.url.path == "/app/installations"
        assert req.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(
            200,
            json=[
                {"id": 111, "account": {"login": "other"}},
                {"id": 222, "account": {"login": "ACME"}},
            ],
        )

    got = resolve_installation_id(
        "424242", pem, "acme", client=_client(handler), base_url=None, now_epoch=NOW
    )
    assert got == "222"


def test_resolve_installation_id_not_installed_returns_none():
    pem = _gen_pem()

    def handler(req):
        return httpx.Response(200, json=[])

    assert (
        resolve_installation_id("1", pem, "acme", client=_client(handler), base_url=None, now_epoch=NOW)
        is None
    )


def test_resolve_installation_id_uses_custom_base_url():
    pem = _gen_pem()
    seen = {}

    def handler(req):
        seen["host"] = req.url.host
        return httpx.Response(200, json=[])

    resolve_installation_id(
        "1",
        pem,
        "acme",
        client=_client(handler),
        base_url="https://ghe.example.com/api/v3",
        now_epoch=NOW,
    )
    assert seen["host"] == "ghe.example.com"


def test_resolve_installation_id_incomplete_response_raises():
    pem = _gen_pem()

    def handler(req):
        return httpx.Response(200, json=[{"account": {"login": "acme"}}])

    with pytest.raises(GitHubManifestError) as ei:
        resolve_installation_id("1", pem, "acme", client=_client(handler), base_url=None, now_epoch=NOW)
    assert "no id" in str(ei.value)


def test_resolve_installation_id_wraps_an_unparseable_private_key_before_any_request():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json=[])

    with pytest.raises(GitHubManifestError) as ei:
        resolve_installation_id(
            "1", "not-a-pem", "acme", client=_client(handler), base_url=None, now_epoch=NOW
        )
    assert str(ei.value) == "the App private key could not be parsed"
    assert ei.value.__cause__ is None
    assert calls == []


def test_manifest_does_not_request_pull_requests():
    assert "pull_requests" not in MANIFEST_PERMISSIONS


def test_build_manifest_omits_callback_urls_when_not_supplied():
    assert "callback_urls" not in build_manifest("acme", "https://x/cb")


def test_build_manifest_includes_callback_urls_when_supplied():
    """``redirect_url`` (the admin's landing) and ``callback_urls`` (the user's) are different
    slots, and the manifest invariants survive the addition."""
    m = build_manifest("acme", "https://x/cb", callback_urls=["https://x/ops/github-link/callback"])
    assert m["callback_urls"] == ["https://x/ops/github-link/callback"]
    assert m["public"] is False and "hook_attributes" not in m
    assert m["redirect_url"] == "https://x/cb"


def test_convert_captures_client_id_and_secret():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "id": 424242,
                "slug": "agp-acme",
                "pem": _gen_pem(),
                "webhook_secret": "whsec_abc",
                "client_id": "Iv23liAbCdEf",
                "client_secret": "cs_deadbeef",
            },
        )

    out = convert_manifest_code("tempcode", client=_client(handler), base_url=None)
    assert out["client_id"] == "Iv23liAbCdEf"
    assert out["client_secret"] == "cs_deadbeef"
    assert out["app_id"] == "424242" and out["webhook_secret"] == "whsec_abc"


def test_convert_without_client_fields_still_succeeds():
    """GitHub's prose and schema disagree on the OAuth pair, so its absence degrades to "user
    linking unavailable" rather than failing the onboarding."""

    def handler(req):
        return httpx.Response(201, json={"id": 7, "pem": _gen_pem(), "webhook_secret": "w"})

    out = convert_manifest_code("c", client=_client(handler), base_url=None)
    assert out["app_id"] == "7"
    assert out["client_id"] is None and out["client_secret"] is None


def test_convert_error_never_leaks_the_client_secret_the_body_or_the_code():
    def handler(req):
        return httpx.Response(
            500,
            json={
                "client_secret": "cs_supersecret",
                "message": "sensitive detail",
            },
        )

    with pytest.raises(GitHubManifestError) as ei:
        convert_manifest_code("thecode", client=_client(handler), base_url=None)
    msg = str(ei.value)
    assert "500" in msg
    assert "cs_supersecret" not in msg and "sensitive" not in msg
    assert "thecode" not in msg


def test_fetch_app_client_id_returns_the_client_id():
    pem = _gen_pem()

    def handler(req):
        assert req.url.path == "/app"
        assert req.method == "GET"
        assert req.headers["Authorization"].startswith("Bearer ")
        assert req.headers["Accept"] == "application/vnd.github+json"
        return httpx.Response(200, json={"id": 424242, "client_id": "Iv23liAbCdEf"})

    got = fetch_app_client_id("424242", pem, client=_client(handler), base_url=None, now_epoch=NOW)
    assert got == "Iv23liAbCdEf"


def test_fetch_app_client_id_uses_custom_base_url():
    pem = _gen_pem()
    seen = {}

    def handler(req):
        seen["host"] = req.url.host
        return httpx.Response(200, json={"client_id": "Iv1.x"})

    fetch_app_client_id(
        "1", pem, client=_client(handler), base_url="https://ghe.example.com/api/v3", now_epoch=NOW
    )
    assert seen["host"] == "ghe.example.com"


def test_fetch_app_client_id_missing_field_raises_safely():
    pem = _gen_pem()

    def handler(req):
        return httpx.Response(200, json={"id": 424242})

    with pytest.raises(GitHubManifestError) as ei:
        fetch_app_client_id("424242", pem, client=_client(handler), base_url=None, now_epoch=NOW)
    assert "client id" in str(ei.value).lower()


def test_fetch_app_client_id_403_message_has_the_status_but_not_the_body():
    pem = _gen_pem()

    def handler(req):
        return httpx.Response(403, json={"message": "sensitive detail", "token": "ghs_secret"})

    with pytest.raises(GitHubManifestError) as ei:
        fetch_app_client_id("1", pem, client=_client(handler), base_url=None, now_epoch=NOW)
    msg = str(ei.value)
    assert "403" in msg
    assert "sensitive" not in msg and "ghs_secret" not in msg


def test_fetch_app_client_id_transport_failure_is_safe():
    pem = _gen_pem()

    def handler(req):
        raise httpx.ConnectError("connect to https://api.github.com failed")

    with pytest.raises(GitHubManifestError) as ei:
        fetch_app_client_id("1", pem, client=_client(handler), base_url=None, now_epoch=NOW)
    msg = str(ei.value)
    assert "ConnectError" in msg
    assert "api.github.com" not in msg


def test_fetch_app_client_id_wraps_an_unparseable_private_key_before_any_request():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={"client_id": "Iv1.x"})

    with pytest.raises(GitHubManifestError) as ei:
        fetch_app_client_id("1", "not-a-pem", client=_client(handler), base_url=None, now_epoch=NOW)
    assert str(ei.value) == "the App private key could not be parsed"
    assert calls == []
