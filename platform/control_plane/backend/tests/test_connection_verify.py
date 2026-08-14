import httpx
from models.connection import Provider
from services.connection_verify import verify_connection, VerifyResult


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_github_success_returns_login():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/user":
            return httpx.Response(200, json={"login": "octocat"})
        if req.url.path == "/orgs/acme":
            return httpx.Response(200, json={"login": "acme"})
        return httpx.Response(404)
    r = verify_connection(Provider.GITHUB, "acme", None, "ghp_x", client=_client(handler))
    assert r == VerifyResult(ok=True, account_login="octocat", reason=None)


def test_github_bad_token():
    r = verify_connection(Provider.GITHUB, "acme", None, "bad",
                          client=_client(lambda req: httpx.Response(401)))
    assert r.ok is False and "authenticate" in r.reason.lower()


def test_github_org_not_visible():
    def handler(req):
        return httpx.Response(200, json={"login": "octocat"}) if req.url.path == "/user" \
            else httpx.Response(404)
    r = verify_connection(Provider.GITHUB, "ghost", None, "ghp_x", client=_client(handler))
    assert r.ok is False and "ghost" in r.reason and "visible" in r.reason.lower()


def test_github_app_verifies_via_installation_repositories_not_user():
    # Regression: a GitHub App INSTALLATION token has no user identity — GET /user returns
    # 403 "Resource not accessible by integration". The App path must probe
    # /installation/repositories instead. If this ever calls /user, the test fails.
    seen = {"paths": []}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["paths"].append(req.url.path)
        if req.url.path == "/user":
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        if req.url.path == "/installation/repositories":
            return httpx.Response(200, json={"total_count": 3, "repositories": []})
        return httpx.Response(404)

    r = verify_connection(
        Provider.GITHUB, "acme", None, "ghs_installation", client=_client(handler), is_app=True
    )
    assert r == VerifyResult(ok=True, account_login="acme", reason=None)
    assert seen["paths"] == ["/installation/repositories"]  # /user never called


def test_github_app_not_installed_is_not_success():
    # 403/404 on the installation probe → App not installed / lost access, not "connected".
    r = verify_connection(
        Provider.GITHUB, "acme", None, "ghs_x",
        client=_client(lambda req: httpx.Response(404)), is_app=True,
    )
    assert r.ok is False and "acme" in r.reason and "installation" in r.reason.lower()


def test_github_app_bad_token_is_auth_failure():
    r = verify_connection(
        Provider.GITHUB, "acme", None, "ghs_bad",
        client=_client(lambda req: httpx.Response(401)), is_app=True,
    )
    assert r.ok is False and "authenticate" in r.reason.lower()


def test_gitlab_success_uses_v4_and_username():
    seen = []
    def handler(req):
        seen.append(req.url.path)
        if req.url.path == "/api/v4/user":
            return httpx.Response(200, json={"username": "alice"})
        if req.url.path == "/api/v4/groups/my-group":
            return httpx.Response(200, json={"full_path": "my-group"})
        return httpx.Response(404)
    r = verify_connection(Provider.GITLAB, "my-group", None, "glpat", client=_client(handler))
    assert r.ok is True and r.account_login == "alice"
    assert "/api/v4/user" in seen


def test_network_error_is_unreachable_reason():
    def handler(req):
        raise httpx.ConnectError("boom")
    r = verify_connection(Provider.GITHUB, "acme", None, "ghp_x", client=_client(handler))
    assert r.ok is False and "reach" in r.reason.lower()


def test_reason_never_contains_response_body():
    def handler(req):
        return httpx.Response(403, json={"secret_echo": "should-not-leak"})
    r = verify_connection(Provider.GITHUB, "acme", None, "ghp_x", client=_client(handler))
    assert "secret_echo" not in (r.reason or "")


def test_identity_5xx_is_unreachable_not_success():
    def handler(req):
        return httpx.Response(500)
    r = verify_connection(Provider.GITHUB, "acme", None, "ghp_x", client=_client(handler))
    assert r.ok is False and "reach" in r.reason.lower()


def test_non_json_identity_body_does_not_crash():
    def handler(req):
        if req.url.path == "/user":
            return httpx.Response(200, text="<html>boom</html>")
        return httpx.Response(404)
    r = verify_connection(Provider.GITHUB, "acme", None, "ghp_x", client=_client(handler))
    assert isinstance(r, VerifyResult)
    assert r.ok is False
    assert "boom" not in (r.reason or "")


def test_org_5xx_is_unreachable_not_success():
    def handler(req):
        if req.url.path == "/user":
            return httpx.Response(200, json={"login": "octocat"})
        if req.url.path == "/orgs/acme":
            return httpx.Response(500)
        return httpx.Response(404)
    r = verify_connection(Provider.GITHUB, "acme", None, "ghp_x", client=_client(handler))
    assert r.ok is False and "reach" in r.reason.lower()
