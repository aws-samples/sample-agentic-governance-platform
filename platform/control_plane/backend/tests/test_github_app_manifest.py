import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from services.github_app_manifest import (
    build_manifest, build_app_name, register_url, convert_manifest_code,
    resolve_installation_id, fetch_app_client_id, GitHubManifestError,
    MANIFEST_PERMISSIONS,
)


def _gen_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def test_build_manifest_shape():
    m = build_manifest("acme", "https://app.example.com/ops/connections/callback")
    assert m["name"] == "agp-acme"
    assert m["redirect_url"] == "https://app.example.com/ops/connections/callback"
    assert m["public"] is False
    # The App homepage `url` is REQUIRED by GitHub's manifest schema.
    assert m["url"] == "https://app.example.com/ops/connections/callback"
    # `hook_attributes` must be OMITTED: GitHub makes hook_attributes.url mandatory whenever
    # the object is present and otherwise rejects the manifest with `"url" wasn't supplied`.
    # AGP consumes no App webhooks, so the key must not appear at all.
    assert "hook_attributes" not in m
    assert m["default_permissions"] == MANIFEST_PERMISSIONS
    assert MANIFEST_PERMISSIONS["administration"] == "write"
    assert MANIFEST_PERMISSIONS["metadata"] == "read"
    # workflows=write is REQUIRED to push the scaffold's .github/workflows/build.yml — a
    # git/trees call containing a workflow path 403s without it. ("workflows" IS a valid
    # manifest key, unlike "variables".)
    assert MANIFEST_PERMISSIONS["workflows"] == "write"
    # The repo "Variables" permission (POST /repos/.../actions/variables, used by
    # github_repo_service.set_repo_variables) has the machine key "actions_variables", NOT
    # "variables" — confirmed via GET /app on the live app that has Variables granted (2026-07-08).
    # The earlier "variables" spelling was rejected by GitHub's manifest validator.
    assert MANIFEST_PERMISSIONS["actions_variables"] == "write"
    assert "variables" not in MANIFEST_PERMISSIONS


def test_app_name_within_github_34_char_cap():
    # GitHub rejects the manifest if the App name exceeds 34 chars. The real org that surfaced
    # this, "AgenticOps-Platform", made the old "agp-{org}-provisioning" name 36 chars.
    assert build_app_name("AgenticOps-Platform") == "agp-AgenticOps-Platform"
    assert len(build_app_name("AgenticOps-Platform")) <= 34
    # A pathologically long org is truncated to the cap, never over.
    long_org = "x" * 60
    assert len(build_app_name(long_org)) == 34
    # Name still flows through build_manifest.
    m = build_manifest("AgenticOps-Platform", "https://app.example.com/ops/connections/callback")
    assert m["name"] == "agp-AgenticOps-Platform" and len(m["name"]) <= 34


def test_register_url_is_org_scoped_and_encodes_state():
    url = register_url("acme", "st/a te+1")
    assert url.startswith("https://github.com/organizations/acme/settings/apps/new?state=")
    assert " " not in url and "/a te" not in url  # state is percent-encoded


def test_convert_manifest_code_returns_captured_fields():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/app-manifests/tempcode/conversions"
        assert req.method == "POST"
        return httpx.Response(201, json={
            "id": 424242, "slug": "agp-acme-provisioning",
            "pem": _gen_pem(), "webhook_secret": "whsec_abc", "client_id": "Iv1.x",
        })
    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = convert_manifest_code("tempcode", client=client, base_url=None)
    assert out["app_id"] == "424242"
    assert out["slug"] == "agp-acme-provisioning"
    assert out["webhook_secret"] == "whsec_abc"
    assert out["pem"].startswith("-----BEGIN")  # generated at runtime, not a literal


def test_convert_manifest_code_non_201_is_safe():
    def handler(req):
        return httpx.Response(422, json={"message": "sensitive detail"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GitHubManifestError) as ei:
        convert_manifest_code("bad", client=client, base_url=None)
    assert "422" in str(ei.value)
    assert "sensitive" not in str(ei.value)


def test_convert_manifest_code_incomplete_is_safe():
    def handler(req):
        return httpx.Response(201, json={"id": 1})  # no pem
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GitHubManifestError) as ei:
        convert_manifest_code("c", client=client, base_url=None)
    assert "incomplete" in str(ei.value)


def test_resolve_installation_id_matches_org_case_insensitive():
    pem = _gen_pem()
    def handler(req):
        assert req.url.path == "/app/installations"
        assert req.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(200, json=[
            {"id": 111, "account": {"login": "other"}},
            {"id": 222, "account": {"login": "ACME"}},
        ])
    client = httpx.Client(transport=httpx.MockTransport(handler))
    got = resolve_installation_id("424242", pem, "acme", client=client, base_url=None, now_epoch=1_700_000_000)
    assert got == "222"


def test_resolve_installation_id_not_installed_returns_none():
    pem = _gen_pem()
    def handler(req):
        return httpx.Response(200, json=[])
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert resolve_installation_id("1", pem, "acme", client=client, base_url=None, now_epoch=1_700_000_000) is None


def test_resolve_installation_id_uses_custom_base_url():
    pem = _gen_pem()
    seen = {}
    def handler(req):
        seen["host"] = req.url.host
        return httpx.Response(200, json=[])
    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolve_installation_id("1", pem, "acme", client=client, base_url="https://ghe.example.com/api/v3", now_epoch=1_700_000_000)
    assert seen["host"] == "ghe.example.com"


def test_resolve_installation_id_incomplete_response_raises():
    pem = _gen_pem()
    def handler(req):
        return httpx.Response(200, json=[{"account": {"login": "acme"}}])  # no id
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GitHubManifestError) as ei:
        resolve_installation_id("1", pem, "acme", client=client, base_url=None, now_epoch=1_700_000_000)
    assert "no id" in str(ei.value)


# ---------------------------------------------------------------- E27B additions


def test_manifest_does_not_request_pull_requests_yet():
    # E27B decision D7: pull_requests belongs to E27C, which is the first epic to exercise a PR
    # verb. It is NOT requested here because a wrong machine key makes GitHub reject the WHOLE
    # manifest and break org onboarding (the "variables" vs "actions_variables" lesson above), the
    # spelling cannot be verified offline, and GitHub does not retro-apply a permission change to
    # already-created Apps — so E27C needs a manual grant on existing installs either way.
    # E27C adds it only after confirming the key against a live GET /app.
    assert "pull_requests" not in MANIFEST_PERMISSIONS


def test_build_manifest_omits_callback_urls_when_not_supplied():
    # Unsupplied ⇒ the key must not appear at all (an empty array is a distinct GitHub input).
    assert "callback_urls" not in build_manifest("acme", "https://x/cb")


def test_build_manifest_includes_callback_urls_when_supplied():
    m = build_manifest("acme", "https://x/cb", callback_urls=["https://x/ops/github-link/callback"])
    assert m["callback_urls"] == ["https://x/ops/github-link/callback"]
    # The two existing manifest invariants must survive the addition.
    assert m["public"] is False and "hook_attributes" not in m
    # redirect_url (admin lands here) and callback_urls (user lands there) are different slots.
    assert m["redirect_url"] == "https://x/cb"


def test_convert_captures_client_id_and_secret():
    # GitHub's 201 schema is an allOf whose SECOND block marks client_id + client_secret required.
    # AGP discarded both until E27B — that is what made per-user OAuth impossible.
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={
            "id": 424242, "slug": "agp-acme", "pem": _gen_pem(),
            "webhook_secret": "whsec_abc",
            "client_id": "Iv23liAbCdEf", "client_secret": "cs_deadbeef",
        })
    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = convert_manifest_code("tempcode", client=client, base_url=None)
    assert out["client_id"] == "Iv23liAbCdEf"
    assert out["client_secret"] == "cs_deadbeef"
    assert out["app_id"] == "424242" and out["webhook_secret"] == "whsec_abc"


def test_convert_without_client_fields_still_succeeds():
    # GitHub's narrative docs contradict its own schema (prose mentions only id/pem/webhook_secret)
    # and publishes NO example body — so a missing pair must DEGRADE (user linking unavailable for
    # that connection), never break App onboarding, which works today.
    def handler(req):
        return httpx.Response(201, json={"id": 7, "pem": _gen_pem(), "webhook_secret": "w"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = convert_manifest_code("c", client=client, base_url=None)
    assert out["app_id"] == "7"
    assert out["client_id"] is None and out["client_secret"] is None


def test_convert_error_never_leaks_the_client_secret_or_the_body():
    def handler(req):
        return httpx.Response(500, json={
            "client_secret": "cs_supersecret", "message": "sensitive detail",
        })
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GitHubManifestError) as ei:
        convert_manifest_code("thecode", client=client, base_url=None)
    msg = str(ei.value)
    assert "500" in msg
    assert "cs_supersecret" not in msg and "sensitive" not in msg
    # The one-time manifest code is credential material too — never echoed.
    assert "thecode" not in msg


def test_fetch_app_client_id_returns_the_client_id():
    pem = _gen_pem()
    def handler(req):
        assert req.url.path == "/app"
        assert req.method == "GET"
        assert req.headers["Authorization"].startswith("Bearer ")
        assert req.headers["Accept"] == "application/vnd.github+json"
        return httpx.Response(200, json={"id": 424242, "client_id": "Iv23liAbCdEf"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    got = fetch_app_client_id("424242", pem, client=client, base_url=None, now_epoch=1_700_000_000)
    assert got == "Iv23liAbCdEf"


def test_fetch_app_client_id_uses_custom_base_url():
    pem = _gen_pem()
    seen = {}
    def handler(req):
        seen["host"] = req.url.host
        return httpx.Response(200, json={"client_id": "Iv1.x"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetch_app_client_id("1", pem, client=client, base_url="https://ghe.example.com/api/v3",
                        now_epoch=1_700_000_000)
    assert seen["host"] == "ghe.example.com"


def test_fetch_app_client_id_missing_field_raises_safely():
    pem = _gen_pem()
    def handler(req):
        return httpx.Response(200, json={"id": 424242})  # no client_id
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GitHubManifestError) as ei:
        fetch_app_client_id("424242", pem, client=client, base_url=None, now_epoch=1_700_000_000)
    assert "client id" in str(ei.value).lower()


def test_fetch_app_client_id_403_message_has_the_status_but_not_the_body():
    pem = _gen_pem()
    def handler(req):
        return httpx.Response(403, json={"message": "sensitive detail", "token": "ghs_secret"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GitHubManifestError) as ei:
        fetch_app_client_id("1", pem, client=client, base_url=None, now_epoch=1_700_000_000)
    msg = str(ei.value)
    assert "403" in msg
    assert "sensitive" not in msg and "ghs_secret" not in msg


def test_fetch_app_client_id_transport_failure_is_safe():
    pem = _gen_pem()
    def handler(req):
        raise httpx.ConnectError("connect to https://api.github.com failed")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GitHubManifestError) as ei:
        fetch_app_client_id("1", pem, client=client, base_url=None, now_epoch=1_700_000_000)
    msg = str(ei.value)
    assert "ConnectError" in msg
    assert "api.github.com" not in msg
