"""Tests for ``services.graph_service`` — the Microsoft Entra / Graph / OBO adapter (E6, T-GRAPH).

ALL HTTP is mocked — there are NO live calls. The pattern: build an
``httpx.AsyncClient`` over an ``httpx.MockTransport`` whose handler routes on
``request.method`` + ``request.url.path`` and returns ``httpx.Response(...)``,
then inject that client into ``GraphService(http_client=...)``. The backend
secret is injected via ``client_secret_loader=lambda: "SENTINEL_SECRET"`` so no
Secrets Manager / boto3 is touched.

The repo is NOT in pytest-asyncio ``auto`` mode (no pytest.ini / pyproject
config), so every async test is decorated with ``@pytest.mark.asyncio``
explicitly.

Mechanics under test come from research §2 (OBO request shape + AADSTS codes;
provisioning Graph calls; ``appRoleAssignedTo`` read/assign/revoke; ``$search``
picker). Contract is pinned in the E6 plan, Task T-GRAPH.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Optional
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest

from services.graph_service import (
    GraphError,
    GraphService,
    NotAssignedError,
    OboConfigError,
)

# ---------------------------------------------------------------------------
# Realistic-but-fake tenant fixtures (NOT hardcoded into the service — passed
# through the constructor). Values mirror the live tenant shapes from the plan.
# ---------------------------------------------------------------------------
TENANT_ID = "00000000-0000-0000-0000-000000000001"
BACKEND_CLIENT_ID = "00000000-0000-0000-0000-000000000003"
LOGIN_BASE = "https://login.microsoftonline.com"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
AUDIENCE_PREFIX = "api://agp-agent-"
TOKEN_PATH = f"/{TENANT_ID}/oauth2/v2.0/token"

SENTINEL_SECRET = "SENTINEL_SECRET"  # the (fake) backend client secret


def _build(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    secret_loader: Optional[Callable[[], str]] = None,
) -> GraphService:
    """Construct a GraphService whose HTTP goes through a MockTransport handler."""
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return GraphService(
        tenant_id=TENANT_ID,
        backend_client_id=BACKEND_CLIENT_ID,
        login_base=LOGIN_BASE,
        graph_base=GRAPH_BASE,
        audience_prefix=AUDIENCE_PREFIX,
        client_secret_loader=secret_loader or (lambda: SENTINEL_SECRET),
        http_client=client,
    )


def _app_token_response() -> httpx.Response:
    """The client-credentials token response that ``_app_token`` consumes."""
    return httpx.Response(
        200,
        json={"access_token": "APP_TOKEN_FAKE", "expires_in": 3600, "token_type": "Bearer"},
    )


def _form(request: httpx.Request) -> dict:
    """Parse a urlencoded request body into a flat {key: value} dict."""
    raw = request.content.decode()
    return {k: v[0] for k, v in parse_qs(raw).items()}


# ===========================================================================
# create_agent_app
# ===========================================================================
@pytest.mark.asyncio
async def test_create_agent_app_posts_application_then_sp():
    agent_id = "rec-abc123"
    calls: list[tuple[str, str]] = []
    captured: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "POST" and path.endswith("/applications"):
            captured["application"] = json.loads(request.content.decode())
            return httpx.Response(
                201,
                json={"id": "app-obj-id", "appId": "app-client-guid"},
            )
        if request.method == "POST" and path.endswith("/servicePrincipals"):
            captured["sp"] = json.loads(request.content.decode())
            return httpx.Response(201, json={"id": "sp-obj-id", "appId": "app-client-guid"})
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    result = await svc.create_agent_app(agent_id, "Claims Triage DE")

    # /applications then /servicePrincipals were both POSTed (order: app before sp).
    graph_calls = [c for c in calls if not c[1].endswith("/oauth2/v2.0/token")]
    assert ("POST", "/v1.0/applications") in graph_calls
    assert ("POST", "/v1.0/servicePrincipals") in graph_calls
    assert graph_calls.index(("POST", "/v1.0/applications")) < graph_calls.index(
        ("POST", "/v1.0/servicePrincipals")
    )

    app_body = captured["application"]
    # CRITIQUE-FIX-C: assert the EXACT identifierUris value (id-derived), not just presence.
    assert app_body["identifierUris"] == [f"api://agp-agent-{agent_id}"]
    assert app_body["api"]["requestedAccessTokenVersion"] == 2
    assert app_body["signInAudience"] == "AzureADMyOrg"
    # E29 livefix-6 (proven live 2026-08-12): Databricks federation policies match on
    # subject_claim=email, and Entra emits `email` in an ACCESS token only when the
    # RESOURCE app opts in via optionalClaims — without this every federated exchange
    # of a per-agent token dies with invalid_grant.
    assert app_body["optionalClaims"] == {
        "accessToken": [{"name": "email", "essential": False, "additionalProperties": []}]
    }
    # exactly one Invoke oauth2 scope.
    scopes = app_body["api"]["oauth2PermissionScopes"]
    assert any(s["value"] == "Invoke" for s in scopes)
    # BOTH Invoker + Admin appRoles present.
    role_values = {r["value"] for r in app_body["appRoles"]}
    assert {"Invoker", "Admin"} <= role_values

    # the SP is created from the app's client GUID.
    assert captured["sp"]["appId"] == "app-client-guid"

    # returns the 6 keys.
    assert set(result.keys()) == {
        "app_id",
        "sp_id",
        "app_uri",
        "invoke_scope_id",
        "invoker_role_id",
        "admin_role_id",
    }
    assert result["app_id"] == "app-client-guid"
    assert result["sp_id"] == "sp-obj-id"
    assert result["app_uri"] == f"api://agp-agent-{agent_id}"
    # the returned ids are the GUIDs minted into the bodies.
    assert result["invoke_scope_id"] == next(
        s["id"] for s in scopes if s["value"] == "Invoke"
    )
    assert result["invoker_role_id"] == next(
        r["id"] for r in app_body["appRoles"] if r["value"] == "Invoker"
    )
    assert result["admin_role_id"] == next(
        r["id"] for r in app_body["appRoles"] if r["value"] == "Admin"
    )


@pytest.mark.asyncio
async def test_create_agent_app_get_or_create_on_duplicate_identifier():
    """CRITIQUE-FIX-A: a duplicate-identifierUris error → look up the existing app + SP."""
    agent_id = "rec-dupe"
    app_uri = f"api://agp-agent-{agent_id}"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if method == "POST" and path.endswith("/applications"):
            # Graph rejects the duplicate identifierUri.
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "Request_BadRequest",
                        "message": (
                            "Another object with the same value for property "
                            "identifierUris already exists."
                        ),
                    }
                },
            )
        if method == "GET" and path.endswith("/applications"):
            # the $filter lookup of the existing app.
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "existing-app-obj",
                            "appId": "existing-app-guid",
                            "identifierUris": [app_uri],
                            "api": {
                                "oauth2PermissionScopes": [
                                    {"id": "existing-scope-id", "value": "Invoke"}
                                ]
                            },
                            "appRoles": [
                                {"id": "existing-invoker", "value": "Invoker"},
                                {"id": "existing-admin", "value": "Admin"},
                            ],
                        }
                    ]
                },
            )
        if method == "GET" and path.endswith("/servicePrincipals"):
            # the $filter lookup of the existing SP by appId.
            return httpx.Response(
                200,
                json={"value": [{"id": "existing-sp-obj", "appId": "existing-app-guid"}]},
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    result = await svc.create_agent_app(agent_id, "Claims Triage DE")

    # Did NOT raise; returned the EXISTING ids.
    assert result["app_id"] == "existing-app-guid"
    assert result["sp_id"] == "existing-sp-obj"
    assert result["app_uri"] == app_uri
    assert result["invoke_scope_id"] == "existing-scope-id"
    assert result["invoker_role_id"] == "existing-invoker"
    assert result["admin_role_id"] == "existing-admin"


# ===========================================================================
# create_agent_app — SP-create retry on Entra replication lag (Part A)
# ===========================================================================
def _sp_replication_lag_400() -> httpx.Response:
    """The transient 400 Graph returns while the just-created app replicates.

    Body shape mirrors the live failure: ``Request_BadRequest`` whose message says
    the appId does not (yet) reference a valid application object.
    """
    return httpx.Response(
        400,
        json={
            "error": {
                "code": "Request_BadRequest",
                "message": (
                    "The appId 'app-client-guid' of the service principal does not "
                    "reference a valid application object."
                ),
            }
        },
    )


def _sp_replication_lag_404() -> httpx.Response:
    """The transient 404 shape (the application object not yet visible)."""
    return httpx.Response(
        404,
        json={
            "error": {
                "code": "Request_ResourceNotFound",
                "message": "Resource 'app-client-guid' does not exist or one of its "
                "queried reference-property objects are not present.",
            }
        },
    )


def _sp_created_201() -> httpx.Response:
    return httpx.Response(201, json={"id": "sp-obj-id", "appId": "app-client-guid"})


def _app_created_201() -> httpx.Response:
    return httpx.Response(201, json={"id": "app-obj-id", "appId": "app-client-guid"})


@pytest.mark.asyncio
async def test_create_agent_app_retries_sp_create_on_transient_400_then_succeeds(
    monkeypatch,
):
    """Entra replication-lag race: /applications→201, /servicePrincipals→400 once
    (replication lag) then 201 → create_agent_app succeeds, returns the 6 ids, and
    the SP endpoint was called >=2 times. The async sleep is patched to instant."""
    monkeypatch.setattr(
        "services.graph_service.asyncio.sleep", AsyncMock(return_value=None)
    )
    sp_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "POST" and path.endswith("/applications"):
            return _app_created_201()
        if request.method == "POST" and path.endswith("/servicePrincipals"):
            sp_calls["n"] += 1
            if sp_calls["n"] == 1:
                return _sp_replication_lag_400()
            return _sp_created_201()
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    result = await svc.create_agent_app("rec-lag", "Claims Triage DE")

    assert sp_calls["n"] >= 2
    assert set(result.keys()) == {
        "app_id",
        "sp_id",
        "app_uri",
        "invoke_scope_id",
        "invoker_role_id",
        "admin_role_id",
    }
    assert result["app_id"] == "app-client-guid"
    assert result["sp_id"] == "sp-obj-id"


@pytest.mark.asyncio
async def test_create_agent_app_retries_sp_create_on_404_then_succeeds(monkeypatch):
    """Same race, 404 shape first → retried, then 201 → success."""
    monkeypatch.setattr(
        "services.graph_service.asyncio.sleep", AsyncMock(return_value=None)
    )
    sp_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "POST" and path.endswith("/applications"):
            return _app_created_201()
        if request.method == "POST" and path.endswith("/servicePrincipals"):
            sp_calls["n"] += 1
            if sp_calls["n"] == 1:
                return _sp_replication_lag_404()
            return _sp_created_201()
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    result = await svc.create_agent_app("rec-lag-404", "Claims Triage DE")

    assert sp_calls["n"] >= 2
    assert result["sp_id"] == "sp-obj-id"


@pytest.mark.asyncio
async def test_create_agent_app_sp_create_gives_up_after_max_attempts(monkeypatch):
    """/applications→201, /servicePrincipals ALWAYS 400 → raises GraphError after the
    bounded attempts; the SP endpoint was called EXACTLY `attempts` times."""
    monkeypatch.setattr(
        "services.graph_service.asyncio.sleep", AsyncMock(return_value=None)
    )
    sp_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "POST" and path.endswith("/applications"):
            return _app_created_201()
        if request.method == "POST" and path.endswith("/servicePrincipals"):
            sp_calls["n"] += 1
            return _sp_replication_lag_400()
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(GraphError) as exc_info:
        await svc.create_agent_app("rec-stuck", "Claims Triage DE")

    from services.graph_service import _REPLICATION_RETRY_ATTEMPTS

    assert sp_calls["n"] == _REPLICATION_RETRY_ATTEMPTS
    assert exc_info.value.status == 400


@pytest.mark.asyncio
async def test_create_agent_app_sp_create_403_fails_fast_no_retry(monkeypatch):
    """A 403 on /servicePrincipals is a PERMISSIONS error, NOT replication lag —
    it must fail fast: raise immediately, SP endpoint called EXACTLY once."""
    sleep_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("services.graph_service.asyncio.sleep", sleep_mock)
    sp_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "POST" and path.endswith("/applications"):
            return _app_created_201()
        if request.method == "POST" and path.endswith("/servicePrincipals"):
            sp_calls["n"] += 1
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": "Authorization_RequestDenied",
                        "message": "Insufficient privileges to complete the operation.",
                    }
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(GraphError) as exc_info:
        await svc.create_agent_app("rec-403", "Claims Triage DE")

    assert sp_calls["n"] == 1  # NO retry on a perms error.
    assert exc_info.value.status == 403
    sleep_mock.assert_not_awaited()  # never slept — failed fast.


# ===========================================================================
# GraphError.message — safe resource-error detail (Part B)
# ===========================================================================
@pytest.mark.asyncio
async def test_graph_error_carries_safe_message_for_resource_errors():
    """A resource-endpoint error (here assign_app_role) surfaces a SAFE detail: the
    raised GraphError has .status, .code, AND a populated .message (from
    error.message). The message carries NO token/secret (benign resource text)."""
    benign_message = "The roleId is not valid for this service principal."

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "POST" and path.endswith("/appRoleAssignedTo"):
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "Request_BadRequest",
                        "message": benign_message,
                    }
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(GraphError) as exc_info:
        await svc.assign_app_role("agent-sp-obj", "maria-oid", "bad-role-id")

    err = exc_info.value
    assert err.status == 400
    assert err.code == "Request_BadRequest"
    assert err.message == benign_message
    # The safe message is in str() so the failure log can show it.
    assert benign_message in str(err)


@pytest.mark.asyncio
async def test_token_error_still_has_no_message():
    """The /token path stays status+code ONLY — NO error_description / message
    leakage. A failed obo_exchange yields a GraphError whose .message is None and
    whose str() does NOT carry the error_description text."""
    description = "AADSTS7000215: Invalid client secret provided LEAKY_DESCRIPTION."

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == TOKEN_PATH:
            return httpx.Response(
                400,
                json={
                    "error": "invalid_client",
                    "error_description": description,
                    "error_codes": [7000215],
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(GraphError) as exc_info:
        await svc.obo_exchange("USER_TOKEN_FAKE", "api://agp-agent-rec-1")

    err = exc_info.value
    assert not isinstance(err, (NotAssignedError, OboConfigError))
    assert getattr(err, "message", None) is None
    assert "LEAKY_DESCRIPTION" not in str(err)
    assert description not in str(err)


# ===========================================================================
# set_assignment_required
# ===========================================================================
@pytest.mark.asyncio
async def test_set_assignment_required_patches_sp():
    sp_id = "sp-obj-id"
    captured: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "PATCH" and path == f"/v1.0/servicePrincipals/{sp_id}":
            captured["patch"] = json.loads(request.content.decode())
            return httpx.Response(204)
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    result = await svc.set_assignment_required(sp_id)

    assert result is None
    # DEFAULT call (no `required` arg) PATCHes True — the E6 agent→user gate, unchanged.
    assert captured["patch"] == {"appRoleAssignmentRequired": True}


@pytest.mark.asyncio
async def test_set_assignment_required_false_patches_false():
    """E7: the MCP path passes required=False — the PATCH body must carry
    appRoleAssignmentRequired == False (the delegated/OBO user is gated by the
    agent→MCP consent grant, NOT by assignment — research §2.4/§2.5). This must FAIL
    if someone restores the hardcoded True."""
    sp_id = "mcp-sp-obj-id"
    captured: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "PATCH" and path == f"/v1.0/servicePrincipals/{sp_id}":
            captured["patch"] = json.loads(request.content.decode())
            return httpx.Response(204)
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    result = await svc.set_assignment_required(sp_id, required=False)

    assert result is None
    # SPECIFIC to False — a body of {"appRoleAssignmentRequired": True} must fail here.
    assert captured["patch"] == {"appRoleAssignmentRequired": False}
    assert captured["patch"]["appRoleAssignmentRequired"] is False


@pytest.mark.asyncio
async def test_set_assignment_required_retries_on_transient_404_then_succeeds(
    monkeypatch,
):
    """The PATCH references the JUST-created SP, which may not have replicated yet.
    A transient 404 once, then 204 → succeeds, and the SP PATCH endpoint was called
    >=2 times. Async sleep patched to instant."""
    monkeypatch.setattr(
        "services.graph_service.asyncio.sleep", AsyncMock(return_value=None)
    )
    sp_id = "sp-obj-id"
    patch_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "PATCH" and path == f"/v1.0/servicePrincipals/{sp_id}":
            patch_calls["n"] += 1
            if patch_calls["n"] == 1:
                return _sp_replication_lag_404()
            return httpx.Response(204)
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    result = await svc.set_assignment_required(sp_id)

    assert result is None
    assert patch_calls["n"] >= 2


@pytest.mark.asyncio
async def test_set_assignment_required_403_fails_fast(monkeypatch):
    """A 403 on the PATCH is a PERMISSIONS error, NOT replication lag — it must fail
    fast: raise immediately, endpoint called EXACTLY once, sleep never awaited."""
    sleep_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("services.graph_service.asyncio.sleep", sleep_mock)
    sp_id = "sp-obj-id"
    patch_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "PATCH" and path == f"/v1.0/servicePrincipals/{sp_id}":
            patch_calls["n"] += 1
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": "Authorization_RequestDenied",
                        "message": "Insufficient privileges to complete the operation.",
                    }
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(GraphError) as exc_info:
        await svc.set_assignment_required(sp_id)

    assert patch_calls["n"] == 1  # NO retry on a perms error.
    assert exc_info.value.status == 403
    sleep_mock.assert_not_awaited()


# ===========================================================================
# grant_backend_obo_consent
# ===========================================================================
@pytest.mark.asyncio
async def test_grant_backend_obo_consent_resolves_backend_sp_and_posts_grant():
    agent_sp_id = "agent-sp-obj"
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path.endswith("/servicePrincipals"):
            captured["filter"] = request.url.params.get("$filter")
            return httpx.Response(200, json={"value": [{"id": "backend-sp-obj"}]})
        if request.method == "POST" and path.endswith("/oauth2PermissionGrants"):
            captured["grant"] = json.loads(request.content.decode())
            return httpx.Response(201, json={"id": "grant-id"})
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    await svc.grant_backend_obo_consent(agent_sp_id)

    # the $filter resolves the backend SP by its appId.
    assert captured["filter"] == f"appId eq '{BACKEND_CLIENT_ID}'"
    grant = captured["grant"]
    assert grant["clientId"] == "backend-sp-obj"
    assert grant["resourceId"] == agent_sp_id
    assert grant["consentType"] == "AllPrincipals"
    assert grant["scope"] == "Invoke"


@pytest.mark.asyncio
async def test_grant_backend_obo_consent_ignores_already_exists():
    """Idempotent: a duplicate-grant error is swallowed (no raise)."""
    agent_sp_id = "agent-sp-obj"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path.endswith("/servicePrincipals"):
            return httpx.Response(200, json={"value": [{"id": "backend-sp-obj"}]})
        if request.method == "POST" and path.endswith("/oauth2PermissionGrants"):
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "Request_BadRequest",
                        "message": "Permission entry already exists.",
                    }
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    # Must NOT raise.
    await svc.grant_backend_obo_consent(agent_sp_id)


@pytest.mark.asyncio
async def test_grant_backend_obo_consent_retries_grant_on_transient_404_then_succeeds(
    monkeypatch,
):
    """The grant POST references the just-created agent SP as resourceId, which may
    not have replicated yet. The backend-SP GET resolves normally; the grant POST
    404s once (replication lag) then 201 → succeeds, grant endpoint called >=2x."""
    monkeypatch.setattr(
        "services.graph_service.asyncio.sleep", AsyncMock(return_value=None)
    )
    agent_sp_id = "agent-sp-obj"
    grant_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path.endswith("/servicePrincipals"):
            return httpx.Response(200, json={"value": [{"id": "backend-sp-obj"}]})
        if request.method == "POST" and path.endswith("/oauth2PermissionGrants"):
            grant_calls["n"] += 1
            if grant_calls["n"] == 1:
                return _sp_replication_lag_404()
            return httpx.Response(201, json={"id": "grant-id"})
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    await svc.grant_backend_obo_consent(agent_sp_id)

    assert grant_calls["n"] >= 2


@pytest.mark.asyncio
async def test_grant_backend_obo_consent_already_exists_not_retried(monkeypatch):
    """Idempotency PRECEDES the transient retry: an already-exists error on the grant
    POST is swallowed as success on the FIRST call and is NOT retried — the POST is
    called EXACTLY once and sleep is never awaited (proves already-exists wins over
    the 400-transient-retry path, since the marker is itself a 400)."""
    sleep_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("services.graph_service.asyncio.sleep", sleep_mock)
    agent_sp_id = "agent-sp-obj"
    grant_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path.endswith("/servicePrincipals"):
            return httpx.Response(200, json={"value": [{"id": "backend-sp-obj"}]})
        if request.method == "POST" and path.endswith("/oauth2PermissionGrants"):
            grant_calls["n"] += 1
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "Request_BadRequest",
                        "message": "Permission entry already exists.",
                    }
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    # Must NOT raise.
    await svc.grant_backend_obo_consent(agent_sp_id)

    assert grant_calls["n"] == 1  # swallowed on the first call, NOT retried.
    sleep_mock.assert_not_awaited()  # already-exists short-circuits before any sleep.


@pytest.mark.asyncio
async def test_grant_backend_obo_consent_grant_403_fails_fast(monkeypatch):
    """A 403 on the grant POST is a PERMISSIONS error, NOT replication lag — it must
    fail fast: raise immediately, grant endpoint called EXACTLY once, never slept."""
    sleep_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("services.graph_service.asyncio.sleep", sleep_mock)
    agent_sp_id = "agent-sp-obj"
    grant_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path.endswith("/servicePrincipals"):
            return httpx.Response(200, json={"value": [{"id": "backend-sp-obj"}]})
        if request.method == "POST" and path.endswith("/oauth2PermissionGrants"):
            grant_calls["n"] += 1
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": "Authorization_RequestDenied",
                        "message": "Insufficient privileges to complete the operation.",
                    }
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(GraphError) as exc_info:
        await svc.grant_backend_obo_consent(agent_sp_id)

    assert grant_calls["n"] == 1  # NO retry on a perms error.
    assert exc_info.value.status == 403
    sleep_mock.assert_not_awaited()


# ===========================================================================
# replication-retry window (Part A — widened to ~45-60s)
# ===========================================================================
@pytest.mark.asyncio
async def test_replication_retry_window_is_at_least_45s(monkeypatch):
    """The widened replication-retry window must total >= 45s of (async) backoff so a
    re-provision self-heals without a manual retry. We capture the actual per-attempt
    delays the helper sleeps by driving a call that ALWAYS returns a transient 404 and
    summing the awaited sleep arguments (sleep itself is patched to instant)."""
    slept: list[float] = []

    async def _record_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr("services.graph_service.asyncio.sleep", _record_sleep)
    sp_id = "sp-obj-id"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "PATCH" and path == f"/v1.0/servicePrincipals/{sp_id}":
            return _sp_replication_lag_404()
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(GraphError):
        await svc.set_assignment_required(sp_id)

    # Worst case: a transient failure on every attempt → (attempts - 1) sleeps.
    from services.graph_service import _REPLICATION_RETRY_ATTEMPTS

    assert len(slept) == _REPLICATION_RETRY_ATTEMPTS - 1
    assert sum(slept) >= 45, f"replication window only {sum(slept)}s (delays={slept})"


# ===========================================================================
# list_assignments
# ===========================================================================
@pytest.mark.asyncio
async def test_list_assignments_returns_inline_display_and_type():
    agent_sp_id = "agent-sp-obj"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path == (
            f"/v1.0/servicePrincipals/{agent_sp_id}/appRoleAssignedTo"
        ):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "assign-1",
                            "principalId": "maria-oid",
                            "principalDisplayName": "Maria Bauer",
                            "principalType": "User",
                            "appRoleId": "invoker-role-id",
                        },
                        {
                            "id": "assign-2",
                            "principalId": "claims-group-oid",
                            "principalDisplayName": "Claims Team",
                            "principalType": "Group",
                            "appRoleId": "admin-role-id",
                        },
                    ]
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    assignments = await svc.list_assignments(agent_sp_id)

    assert len(assignments) == 2
    first = assignments[0]
    # display name + type come INLINE (no extra resolve call).
    assert first["principalDisplayName"] == "Maria Bauer"
    assert first["principalType"] == "User"
    assert first["appRoleId"] == "invoker-role-id"
    assert assignments[1]["principalType"] == "Group"


# ===========================================================================
# assign_app_role
# ===========================================================================
@pytest.mark.asyncio
async def test_assign_app_role_posts_assignment():
    agent_sp_id = "agent-sp-obj"
    captured: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "POST" and path == (
            f"/v1.0/servicePrincipals/{agent_sp_id}/appRoleAssignedTo"
        ):
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(
                201,
                json={
                    "id": "new-assign-id",
                    "principalId": "maria-oid",
                    "principalDisplayName": "Maria Bauer",
                    "principalType": "User",
                    "appRoleId": "invoker-role-id",
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    result = await svc.assign_app_role(agent_sp_id, "maria-oid", "invoker-role-id")

    body = captured["body"]
    assert body["principalId"] == "maria-oid"
    assert body["resourceId"] == agent_sp_id
    assert body["appRoleId"] == "invoker-role-id"
    assert result["id"] == "new-assign-id"


# ===========================================================================
# revoke_app_role
# ===========================================================================
@pytest.mark.asyncio
async def test_revoke_app_role_deletes():
    agent_sp_id = "agent-sp-obj"
    assignment_id = "assign-1"
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "DELETE" and path == (
            f"/v1.0/servicePrincipals/{agent_sp_id}/appRoleAssignedTo/{assignment_id}"
        ):
            return httpx.Response(204)
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    result = await svc.revoke_app_role(agent_sp_id, assignment_id)

    assert result is None
    assert (
        "DELETE",
        f"/v1.0/servicePrincipals/{agent_sp_id}/appRoleAssignedTo/{assignment_id}",
    ) in calls


# ===========================================================================
# search_principals
# ===========================================================================
@pytest.mark.asyncio
async def test_search_principals_sets_consistencylevel_header_and_merges_users_groups():
    query = "mar"
    headers_seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path.endswith("/users"):
            headers_seen["users"] = request.headers.get("ConsistencyLevel", "")
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "maria-oid",
                            "displayName": "Maria Bauer",
                            "mail": "maria.bauer@example.com",
                        }
                    ]
                },
            )
        if request.method == "GET" and path.endswith("/groups"):
            headers_seen["groups"] = request.headers.get("ConsistencyLevel", "")
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "claims-group-oid", "displayName": "Claims Marketing"}
                    ]
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    hits = await svc.search_principals(query)

    # ConsistencyLevel: eventual on BOTH calls (required for $search).
    assert headers_seen["users"] == "eventual"
    assert headers_seen["groups"] == "eventual"

    by_id = {h["id"]: h for h in hits}
    assert by_id["maria-oid"]["type"] == "user"
    assert by_id["maria-oid"]["displayName"] == "Maria Bauer"
    assert by_id["maria-oid"]["mail"] == "maria.bauer@example.com"
    assert by_id["claims-group-oid"]["type"] == "group"
    assert by_id["claims-group-oid"]["displayName"] == "Claims Marketing"


# ===========================================================================
# obo_exchange
# ===========================================================================
@pytest.mark.asyncio
async def test_obo_exchange_returns_access_token_on_success():
    captured: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == TOKEN_PATH:
            captured["form"] = _form(request)
            return httpx.Response(
                200, json={"access_token": "OBO_AGENT_TOKEN", "expires_in": 3600}
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    token = await svc.obo_exchange("USER_TOKEN_FAKE", "api://agp-agent-rec-1")

    assert token == "OBO_AGENT_TOKEN"
    form = captured["form"]
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    assert form["client_id"] == BACKEND_CLIENT_ID
    assert form["assertion"] == "USER_TOKEN_FAKE"
    assert form["scope"] == "api://agp-agent-rec-1/.default"
    assert form["requested_token_use"] == "on_behalf_of"
    assert form["client_secret"] == SENTINEL_SECRET


@pytest.mark.asyncio
async def test_obo_exchange_raises_not_assigned_on_50105():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == TOKEN_PATH:
            return httpx.Response(
                400,
                json={
                    "error": "interaction_required",
                    "error_description": "AADSTS50105: The user is not assigned to a role...",
                    "error_codes": [50105],
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(NotAssignedError):
        await svc.obo_exchange("USER_TOKEN_FAKE", "api://agp-agent-rec-1")


@pytest.mark.asyncio
async def test_obo_exchange_raises_not_assigned_on_50105_string_codes():
    """Fix 1: if a proxy re-serializes error_codes as STRINGS, 50105 must still
    map to NotAssignedError (not silently collapse into a generic GraphError)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == TOKEN_PATH:
            return httpx.Response(
                400,
                json={
                    "error": "interaction_required",
                    "error_description": "AADSTS50105: The user is not assigned to a role...",
                    "error_codes": ["50105"],  # STRING, not int.
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(NotAssignedError):
        await svc.obo_exchange("USER_TOKEN_FAKE", "api://agp-agent-rec-1")


@pytest.mark.asyncio
async def test_obo_exchange_raises_obo_config_error_on_65001_or_500011():
    """CRITIQUE-FIX-D: 65001 (missing delegated grant) OR 500011 (resource unresolved)
    → a DISTINCT OboConfigError (not NotAssigned, not generic GraphError)."""
    for code in (65001, 500011):

        def handler(request: httpx.Request, _code=code) -> httpx.Response:
            if request.method == "POST" and request.url.path == TOKEN_PATH:
                return httpx.Response(
                    400,
                    json={
                        "error": "invalid_grant",
                        "error_description": f"AADSTS{_code}: ...",
                        "error_codes": [_code],
                    },
                )
            return httpx.Response(500, json={"error": "unexpected"})

        svc = _build(handler)
        with pytest.raises(OboConfigError):
            await svc.obo_exchange("USER_TOKEN_FAKE", "api://agp-agent-rec-1")


@pytest.mark.asyncio
async def test_obo_exchange_raises_graph_error_on_other_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == TOKEN_PATH:
            return httpx.Response(
                400,
                json={
                    "error": "invalid_client",
                    "error_description": "AADSTS7000215: Invalid client secret provided.",
                    "error_codes": [7000215],
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(GraphError) as exc_info:
        await svc.obo_exchange("USER_TOKEN_FAKE", "api://agp-agent-rec-1")
    # GraphError must NOT be one of the typed subclasses by accident.
    assert not isinstance(exc_info.value, (NotAssignedError, OboConfigError))


@pytest.mark.asyncio
async def test_obo_exchange_raises_graph_error_when_no_access_token():
    """Fix 2: a malformed 200 lacking access_token → GraphError, not a bare KeyError."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == TOKEN_PATH:
            # 200 OK but no access_token in the body.
            return httpx.Response(200, json={"token_type": "Bearer", "expires_in": 3600})
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(GraphError) as exc_info:
        await svc.obo_exchange("USER_TOKEN_FAKE", "api://agp-agent-rec-1")
    assert exc_info.value.code == "no_access_token_in_response"


# ===========================================================================
# search_principals — query escaping
# ===========================================================================
@pytest.mark.asyncio
async def test_search_principals_escapes_double_quotes_in_query():
    """Fix 3: a double-quote in the caller-supplied query must NOT malform the
    outgoing $search OData clause (it would close the quoted clause early)."""
    search_values: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path.endswith("/users"):
            search_values["users"] = request.url.params.get("$search", "")
            return httpx.Response(200, json={"value": []})
        if request.method == "GET" and path.endswith("/groups"):
            search_values["groups"] = request.url.params.get("$search", "")
            return httpx.Response(200, json={"value": []})
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    # A malicious/typo'd query carrying embedded double-quotes.
    await svc.search_principals('mar"ia" OR 1')

    # The injected quotes are stripped → the clause stays well-formed
    # (`"displayName:..."` with exactly the wrapping pair, no inner quotes).
    for who in ("users", "groups"):
        value = search_values[who]
        # exactly the two wrapping quotes remain.
        assert value.count('"') == 2
        assert value.startswith('"displayName:')
        assert value.endswith('"')
        # the inner quotes from the raw query are gone.
        assert value == '"displayName:maria OR 1"'


# ===========================================================================
# resolve_user_by_email — the EXACT $filter counterpart to the fuzzy $search
# ===========================================================================
def _users_filter_handler(
    captured: dict, value: list[dict], *, status: int = 200
) -> Callable[[httpx.Request], httpx.Response]:
    """Route the app token + a single ``GET /users`` and record its ``$filter``."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path.endswith("/users"):
            captured["filter"] = request.url.params.get("$filter", "")
            captured["select"] = request.url.params.get("$select", "")
            captured["consistency"] = request.headers.get("ConsistencyLevel", "")
            if status != 200:
                return httpx.Response(
                    status, json={"error": {"code": "Request_BadRequest"}}
                )
            return httpx.Response(200, json={"value": value})
        return httpx.Response(500, json={"error": "unexpected"})

    return handler


@pytest.mark.asyncio
async def test_resolve_user_by_email_filters_on_mail_and_upn_and_returns_the_hit():
    """The address is resolved by an EXACT $filter on BOTH address attributes.

    ``search_principals`` only ``$search``es ``displayName``, so it can never resolve an
    address — this is the method callers use when they hold an address and need the oid.
    The hit shape matches ``search_principals``' so both share one matcher.
    """
    captured: dict[str, str] = {}
    svc = _build(
        _users_filter_handler(
            captured,
            [
                {
                    "id": "maria-oid",
                    "displayName": "Maria Bauer",
                    "mail": "maria.bauer@example.com",
                    "userPrincipalName": "maria.bauer@example.com",
                }
            ],
        )
    )

    hit = await svc.resolve_user_by_email("maria.bauer@example.com")

    assert (
        captured["filter"]
        == "mail eq 'maria.bauer@example.com' or userPrincipalName eq 'maria.bauer@example.com'"
    )
    assert captured["consistency"] == "eventual"
    assert hit == {
        "id": "maria-oid",
        "displayName": "Maria Bauer",
        "type": "user",
        "mail": "maria.bauer@example.com",
        "userPrincipalName": "maria.bauer@example.com",
    }


@pytest.mark.asyncio
async def test_resolve_user_by_email_returns_none_when_no_user_matches():
    captured: dict[str, str] = {}
    svc = _build(_users_filter_handler(captured, []))

    assert await svc.resolve_user_by_email("nobody@example.com") is None


@pytest.mark.asyncio
async def test_resolve_user_by_email_returns_none_when_several_users_match():
    """An ambiguous address must NOT be narrowed to the first hit — the caller decides."""
    captured: dict[str, str] = {}
    svc = _build(
        _users_filter_handler(
            captured,
            [
                {"id": "oid-1", "mail": "shared@example.com"},
                {"id": "oid-2", "mail": "shared@example.com"},
            ],
        )
    )

    assert await svc.resolve_user_by_email("shared@example.com") is None


@pytest.mark.asyncio
async def test_resolve_user_by_email_doubles_single_quotes_in_the_address():
    """A single-quote is LEGAL in an address' local part and is OData's own string
    delimiter, so it must be DOUBLED — otherwise the $filter clause closes early
    (malformed at best, injectable at worst)."""
    captured: dict[str, str] = {}
    svc = _build(_users_filter_handler(captured, []))

    await svc.resolve_user_by_email("o'brien@example.com")

    assert (
        captured["filter"]
        == "mail eq 'o''brien@example.com' or userPrincipalName eq 'o''brien@example.com'"
    )


@pytest.mark.asyncio
async def test_resolve_user_by_email_injection_attempt_stays_inside_the_literal():
    """An address crafted to close the clause and append OData must stay quoted."""
    captured: dict[str, str] = {}
    svc = _build(_users_filter_handler(captured, []))

    await svc.resolve_user_by_email("x' or startsWith(mail,'a")

    # Every injected quote is doubled, so no lone quote survives to end the literal.
    assert "'' or startsWith(mail,''a'" in captured["filter"]
    # Balanced quote count ⇒ the literals are still closed (no dangling delimiter).
    assert captured["filter"].count("'") % 2 == 0


@pytest.mark.asyncio
async def test_resolve_user_by_email_raises_graph_error_on_non_2xx():
    captured: dict[str, str] = {}
    svc = _build(_users_filter_handler(captured, [], status=400))

    with pytest.raises(GraphError) as err:
        await svc.resolve_user_by_email("maria.bauer@example.com")
    assert err.value.status == 400


# ===========================================================================
# _app_token caching
# ===========================================================================
@pytest.mark.asyncio
async def test_app_token_caches_and_refreshes():
    token_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == TOKEN_PATH:
            token_calls["n"] += 1
            return httpx.Response(
                200,
                json={"access_token": f"APP_TOKEN_{token_calls['n']}", "expires_in": 3600},
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)

    t1 = await svc._app_token()
    t2 = await svc._app_token()
    # Two calls within validity reuse the cached token → exactly ONE HTTP call.
    assert t1 == t2
    assert token_calls["n"] == 1

    # Force expiry → next call refreshes (a second HTTP call).
    svc._app_token_expiry = 0.0
    t3 = await svc._app_token()
    assert token_calls["n"] == 2
    assert t3 != t1


@pytest.mark.asyncio
async def test_app_token_wraps_transport_error_in_graph_error():
    """E24/T5 review Finding 3: a TRANSPORT failure (timeout/connect error) on
    the Entra /token POST itself (cold token cache) must surface as ``GraphError``
    — not a raw ``httpx.HTTPError`` — so callers' ``except GraphError`` (e.g.
    ``TenantResolver._group_ids``) catches it uniformly. The message stays a
    fixed literal (no exception detail leaks outward)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == TOKEN_PATH:
            raise httpx.ConnectTimeout("connection timed out")
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(GraphError) as exc_info:
        await svc._app_token()

    assert exc_info.value.status == 502
    assert exc_info.value.code == "transport_error"
    # Fixed literal only — the httpx detail must not leak into the message.
    assert "connection timed out" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_list_member_group_ids_token_transport_failure_is_graph_error():
    """Resolver-facing shape of Finding 3: a cold-cache token transport failure
    reached TRANSITIVELY through ``list_member_group_ids`` also surfaces as
    ``GraphError`` — so ``TenantResolver._group_ids``'s ``except GraphError:
    return []`` degrades instead of 500ing the resolve."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/oauth2/v2.0/token"):
            raise httpx.ConnectError("dns failure")
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(GraphError):
        await svc.list_member_group_ids("frank-oid")


# ===========================================================================
# Secret / token redaction (CRITIQUE-FIX-E)
# ===========================================================================
@pytest.mark.asyncio
async def test_secret_and_tokens_never_logged(caplog):
    """Distinct sentinels for the secret, the inbound user token, and the OBO'd
    token. Drive a path through obo_exchange (success first, to mint an OBO'd
    token; then a forced error to inspect the exception message). Assert NONE of
    the three sentinels appears in any captured log record OR in any raised
    exception's str()."""
    secret_sentinel = "SECRET_SENTINEL_DO_NOT_LEAK"
    user_token_sentinel = "USER_TOKEN_SENTINEL_DO_NOT_LEAK"
    obo_token_sentinel = "OBO_TOKEN_SENTINEL_DO_NOT_LEAK"

    state = {"mode": "ok"}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == TOKEN_PATH:
            if state["mode"] == "ok":
                return httpx.Response(
                    200, json={"access_token": obo_token_sentinel, "expires_in": 3600}
                )
            # error mode: an Entra error whose body echoes the assertion (we must
            # NOT propagate the body into the exception).
            return httpx.Response(
                400,
                json={
                    "error": "invalid_grant",
                    "error_description": (
                        f"AADSTS7000218: assertion={user_token_sentinel} "
                        f"secret={secret_sentinel}"
                    ),
                    "error_codes": [7000218],
                },
            )
        # a Graph call (Bearer app token) to also exercise that path.
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path.endswith("/servicePrincipals"):
            return httpx.Response(200, json={"value": [{"id": "backend-sp"}]})
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler, secret_loader=lambda: secret_sentinel)

    with caplog.at_level(logging.DEBUG):
        # success path: mints the OBO'd token sentinel.
        returned = await svc.obo_exchange(user_token_sentinel, "api://agp-agent-rec-1")
        assert returned == obo_token_sentinel

        # error path: capture the raised exception's message.
        state["mode"] = "error"
        with pytest.raises(GraphError) as exc_info:
            await svc.obo_exchange(user_token_sentinel, "api://agp-agent-rec-1")
        exc_text = str(exc_info.value)

    # The exception message must NOT carry any of the three sentinels (only
    # status + Graph/Entra error code are allowed).
    assert secret_sentinel not in exc_text
    assert user_token_sentinel not in exc_text
    assert obo_token_sentinel not in exc_text

    # No captured log record may contain any of the three sentinels.
    all_logs = "\n".join(rec.getMessage() for rec in caplog.records)
    assert secret_sentinel not in all_logs
    assert user_token_sentinel not in all_logs
    assert obo_token_sentinel not in all_logs


# ===========================================================================
# AsyncClient lifecycle (Fix 5)
# ===========================================================================
@pytest.mark.asyncio
async def test_aclose_does_not_close_an_injected_client():
    """When an http_client is INJECTED, the caller owns its lifecycle —
    aclose() must NOT close it."""
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    client = httpx.AsyncClient(transport=transport)
    svc = GraphService(
        tenant_id=TENANT_ID,
        backend_client_id=BACKEND_CLIENT_ID,
        login_base=LOGIN_BASE,
        graph_base=GRAPH_BASE,
        audience_prefix=AUDIENCE_PREFIX,
        client_secret_loader=lambda: SENTINEL_SECRET,
        http_client=client,
    )
    await svc.aclose()
    assert client.is_closed is False
    await client.aclose()  # caller cleans up its own client.


class _LoopBoundClient:
    """Fake httpx.AsyncClient that models httpx's real loop-affinity.

    A default (self-created) ``httpx.AsyncClient`` binds its connection-pool /
    asyncio transport to the event loop of the FIRST request. When that loop is
    closed (each ``asyncio.run`` closes its loop), reusing the same client from a
    NEW loop raises ``RuntimeError: unable to perform operation on <TCPTransport
    closed=True ...>; the handler is closed`` — the live-test bug. This fake
    reproduces exactly that, deterministically and without any network I/O.
    """

    def __init__(self, *args, **kwargs) -> None:  # noqa: D401 - mimic httpx ctor
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None  # constructed outside a loop (lazy bind on 1st use)
        self.is_closed = False

    def _bind_or_raise(self) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if self._loop is None:
            self._loop = running  # lazy bind to the first loop that uses it
        elif running is not self._loop:
            raise RuntimeError(
                "unable to perform operation on <TCPTransport closed=True "
                "reading=False>; the handler is closed"
            )

    async def post(self, *args, **kwargs) -> httpx.Response:
        self._bind_or_raise()
        return httpx.Response(
            200, json={"access_token": "APP_TOKEN_FAKE", "expires_in": 3600}
        )

    async def request(self, *args, **kwargs) -> httpx.Response:
        self._bind_or_raise()
        return httpx.Response(200, json={})

    async def aclose(self) -> None:
        self.is_closed = True


async def _run_app_token(svc: GraphService) -> str:
    """Coroutine helper: force a token fetch (exercises ``self._http.post``)."""
    svc._app_token_value = None  # bust the cache so HTTP is actually attempted
    svc._app_token_expiry = 0.0
    return await svc._app_token()


def test_self_created_client_survives_cross_loop_reuse(monkeypatch):
    """REPRO (E23 live-test bug): a SELF-CREATED client driven via two separate
    ``asyncio.run`` calls (two loops) must NOT raise the closed-transport
    RuntimeError on the 2nd call. Before the per-loop-client fix the singleton's
    transport is bound to the (now-closed) first loop → RuntimeError on reuse.

    httpx's loop-affinity is modelled by ``_LoopBoundClient`` so the repro is
    deterministic and network-free.
    """
    monkeypatch.setattr(httpx, "AsyncClient", _LoopBoundClient)
    svc = GraphService(
        tenant_id=TENANT_ID,
        backend_client_id=BACKEND_CLIENT_ID,
        login_base=LOGIN_BASE,
        graph_base=GRAPH_BASE,
        audience_prefix=AUDIENCE_PREFIX,
        client_secret_loader=lambda: SENTINEL_SECRET,
        # No http_client → self-created (the production singleton shape).
    )

    # Loop #1 — binds the client's transport to this (soon-closed) loop.
    assert asyncio.run(_run_app_token(svc)) == "APP_TOKEN_FAKE"
    # Loop #2 — a fresh loop. The buggy code reuses the loop-1-bound transport
    # here and raises the closed-transport RuntimeError; the fix rebuilds a
    # per-loop client so this succeeds.
    assert asyncio.run(_run_app_token(svc)) == "APP_TOKEN_FAKE"


def test_injected_client_is_never_rebuilt_across_loops():
    """An INJECTED client (tests / DI) is caller-owned — the loop-safety guard
    must NEVER swap it out, even across ``asyncio.run`` loops. Guards the
    MockTransport-injected test path from the per-loop-client fix."""
    svc = _build(
        lambda req: httpx.Response(
            200, json={"access_token": "APP_TOKEN_FAKE", "expires_in": 3600}
        )
    )
    same = svc._http
    assert asyncio.run(_run_app_token(svc)) == "APP_TOKEN_FAKE"
    # Injected client object is identical after a loop cycle (never rebuilt).
    assert svc._http is same
    # The injected client is NEVER placed in the per-loop map (caller-owned).
    assert len(svc._clients_by_loop) == 0


def test_self_created_clients_tracked_per_loop_not_silently_orphaned(monkeypatch):
    """ANTI-LEAK (review follow-up): with a SELF-created client driven on TWO
    different live loops, EACH loop keeps its OWN tracked client — the earlier
    loop's client is NOT silently dropped on the loop change (the pre-fix
    single-slot reassignment orphaned a still-live-loop client → fd/connection
    leak). We drive two explicit loops (kept alive so the WeakKeyDictionary keeps
    both keys), assert the map holds a DISTINCT client per loop, and assert
    aclose() closes BOTH (not just the latest)."""
    monkeypatch.setattr(httpx, "AsyncClient", _LoopBoundClient)
    svc = GraphService(
        tenant_id=TENANT_ID,
        backend_client_id=BACKEND_CLIENT_ID,
        login_base=LOGIN_BASE,
        graph_base=GRAPH_BASE,
        audience_prefix=AUDIENCE_PREFIX,
        client_secret_loader=lambda: SENTINEL_SECRET,
        # No http_client → self-created (the production singleton shape).
    )

    loops: list[asyncio.AbstractEventLoop] = []
    clients_seen: list[_LoopBoundClient] = []
    for _ in range(2):
        loop = asyncio.new_event_loop()
        loops.append(loop)  # keep the loop object alive → weak key stays live
        try:
            assert loop.run_until_complete(_run_app_token(svc)) == "APP_TOKEN_FAKE"
        finally:
            # Do NOT close the loop yet — we need it alive to read the map below.
            pass
        clients_seen.append(svc._clients_by_loop[loop])

    # Each loop got its OWN client — the first was NOT displaced by the second.
    assert len(svc._clients_by_loop) == 2
    assert clients_seen[0] is not clients_seen[1]
    assert all(c.is_closed is False for c in clients_seen)

    # aclose() closes ALL self-created clients it still holds (both loops), never
    # just the most-recent one.
    asyncio.run(svc.aclose())
    assert all(c.is_closed is True for c in clients_seen)

    for loop in loops:
        loop.close()


@pytest.mark.asyncio
async def test_aclose_closes_a_self_created_client():
    """When NO client is injected, the service owns the default client and
    aclose() closes it."""
    svc = GraphService(
        tenant_id=TENANT_ID,
        backend_client_id=BACKEND_CLIENT_ID,
        login_base=LOGIN_BASE,
        graph_base=GRAPH_BASE,
        audience_prefix=AUDIENCE_PREFIX,
        client_secret_loader=lambda: SENTINEL_SECRET,
    )
    assert svc._http.is_closed is False
    await svc.aclose()
    assert svc._http.is_closed is True


# ===========================================================================
# create_mcp_app (E7, T-GRAPH) — MCP app reg whose roles are assignable to
# APPLICATIONS (an agent is a ServicePrincipal and cannot take a ["User"]-only
# role). The shared _create_app_reg builder is parameterised on the
# identifierUris prefix + the appRoles allowedMemberTypes; the agent path stays
# byte-identical (locked by test_create_agent_app_unchanged_user_member_types).
# ===========================================================================
@pytest.mark.asyncio
async def test_create_mcp_app_uses_mcp_prefix_and_application_member_types():
    """The MCP app uses the api://agp-mcp- prefix, sets appRoles
    allowedMemberTypes == ["User","Application"] (the safe superset — a human can
    be assigned for testing AND an agent SP), defines an Invoke oauth2 scope, and
    a /servicePrincipals create follows the /applications create."""
    mcp_id = "mcp-weather-1"
    calls: list[tuple[str, str]] = []
    captured: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "POST" and path.endswith("/applications"):
            captured["application"] = json.loads(request.content.decode())
            return httpx.Response(201, json={"id": "mcp-app-obj", "appId": "mcp-app-guid"})
        if request.method == "POST" and path.endswith("/servicePrincipals"):
            captured["sp"] = json.loads(request.content.decode())
            return httpx.Response(201, json={"id": "mcp-sp-obj", "appId": "mcp-app-guid"})
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    result = await svc.create_mcp_app(mcp_id, "Weather MCP")

    # /applications POSTed before /servicePrincipals.
    graph_calls = [c for c in calls if not c[1].endswith("/oauth2/v2.0/token")]
    assert ("POST", "/v1.0/applications") in graph_calls
    assert ("POST", "/v1.0/servicePrincipals") in graph_calls
    assert graph_calls.index(("POST", "/v1.0/applications")) < graph_calls.index(
        ("POST", "/v1.0/servicePrincipals")
    )

    app_body = captured["application"]
    # MCP prefix (NOT the agent prefix the service was constructed with).
    assert app_body["identifierUris"] == [f"api://agp-mcp-{mcp_id}"]
    assert app_body["api"]["requestedAccessTokenVersion"] == 2
    assert app_body["signInAudience"] == "AzureADMyOrg"
    # an Invoke oauth2 scope is defined.
    scopes = app_body["api"]["oauth2PermissionScopes"]
    assert any(s["value"] == "Invoke" for s in scopes)
    # THE load-bearing fact (research §2.1): every appRole allows Application
    # member types — both Invoker and Admin set ["User","Application"].
    assert {r["value"] for r in app_body["appRoles"]} >= {"Invoker", "Admin"}
    for role in app_body["appRoles"]:
        assert role["allowedMemberTypes"] == ["User", "Application"]

    # the SP is created from the app's client GUID.
    assert captured["sp"]["appId"] == "mcp-app-guid"

    # returns the SAME 6 keys as create_agent_app.
    assert set(result.keys()) == {
        "app_id",
        "sp_id",
        "app_uri",
        "invoke_scope_id",
        "invoker_role_id",
        "admin_role_id",
    }
    assert result["app_id"] == "mcp-app-guid"
    assert result["sp_id"] == "mcp-sp-obj"
    assert result["app_uri"] == f"api://agp-mcp-{mcp_id}"


@pytest.mark.asyncio
async def test_create_agent_app_unchanged_user_member_types():
    """Locks the refactor: the agent path STILL emits allowedMemberTypes == ["User"]
    and the api://agp-agent- prefix — the _create_app_reg extraction is invisible to
    the E6 user→agent path."""
    agent_id = "rec-refactor-lock"
    captured: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "POST" and path.endswith("/applications"):
            captured["application"] = json.loads(request.content.decode())
            return httpx.Response(201, json={"id": "app-obj-id", "appId": "app-client-guid"})
        if request.method == "POST" and path.endswith("/servicePrincipals"):
            return httpx.Response(201, json={"id": "sp-obj-id", "appId": "app-client-guid"})
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    await svc.create_agent_app(agent_id, "Claims Triage DE")

    app_body = captured["application"]
    assert app_body["identifierUris"] == [f"api://agp-agent-{agent_id}"]
    # every appRole stays ["User"]-only (user-grants depend on it).
    assert {r["value"] for r in app_body["appRoles"]} >= {"Invoker", "Admin"}
    for role in app_body["appRoles"]:
        assert role["allowedMemberTypes"] == ["User"]


@pytest.mark.asyncio
async def test_create_mcp_app_get_or_create_on_duplicate():
    """A duplicate-identifierUris error on the MCP app create → look up the existing
    app + SP and return their ids (mirrors create_agent_app's CRITIQUE-FIX-A path)."""
    mcp_id = "mcp-dupe"
    app_uri = f"api://agp-mcp-{mcp_id}"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if method == "POST" and path.endswith("/applications"):
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "Request_BadRequest",
                        "message": (
                            "Another object with the same value for property "
                            "identifierUris already exists."
                        ),
                    }
                },
            )
        if method == "GET" and path.endswith("/applications"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "existing-mcp-app-obj",
                            "appId": "existing-mcp-app-guid",
                            "identifierUris": [app_uri],
                            "api": {
                                "oauth2PermissionScopes": [
                                    {"id": "existing-mcp-scope", "value": "Invoke"}
                                ]
                            },
                            "appRoles": [
                                {"id": "existing-mcp-invoker", "value": "Invoker"},
                                {"id": "existing-mcp-admin", "value": "Admin"},
                            ],
                        }
                    ]
                },
            )
        if method == "GET" and path.endswith("/servicePrincipals"):
            return httpx.Response(
                200,
                json={"value": [{"id": "existing-mcp-sp", "appId": "existing-mcp-app-guid"}]},
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    result = await svc.create_mcp_app(mcp_id, "Weather MCP")

    # Did NOT raise; returned the EXISTING ids resolved by the api://agp-mcp- uri.
    assert result["app_id"] == "existing-mcp-app-guid"
    assert result["sp_id"] == "existing-mcp-sp"
    assert result["app_uri"] == app_uri
    assert result["invoke_scope_id"] == "existing-mcp-scope"
    assert result["invoker_role_id"] == "existing-mcp-invoker"
    assert result["admin_role_id"] == "existing-mcp-admin"


# ===========================================================================
# get_application_object_id (E7, T-CRED-PROVIDER) — resolves the application
# directory object id from the stored appId/clientId GUID via a $filter lookup.
# Required before add_agent_password (which needs /applications/{objId}, not the
# GUID). Fails CLOSED on an empty result so we never mint on the wrong app.
# ===========================================================================
@pytest.mark.asyncio
async def test_get_application_object_id_filters_by_app_id():
    """GET /applications?$filter=appId eq '{client_id}'&$select=id → returns
    value[0]['id'] (the directory object id, not the appId GUID).
    Asserts the exact OData $filter string and the $select=id param."""
    client_id = "00000000-0000-0000-0000-000000000003"
    object_id = "app-directory-object-id-1234"
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path.endswith("/applications"):
            captured["filter"] = request.url.params.get("$filter", "")
            captured["select"] = request.url.params.get("$select", "")
            return httpx.Response(200, json={"value": [{"id": object_id}]})
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    result = await svc.get_application_object_id(client_id)

    # Returns the directory object id (not the appId GUID).
    assert result == object_id
    # The exact $filter string uses the appId GUID, single-quoted for OData.
    assert captured["filter"] == f"appId eq '{client_id}'"
    # $select=id limits the response body to just the id field.
    assert captured["select"] == "id"


@pytest.mark.asyncio
async def test_get_application_object_id_raises_when_not_found():
    """An empty value list (app not in this tenant) → GraphError (fails CLOSED —
    never proceeds to add_agent_password on a missing app)."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path.endswith("/applications"):
            return httpx.Response(200, json={"value": []})
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(GraphError) as exc_info:
        await svc.get_application_object_id("no-such-app-guid")

    err = exc_info.value
    assert err.status == 404
    assert err.code == "Request_ResourceNotFound"


# ===========================================================================
# add_agent_password (E7, T-CRED-PROVIDER) — mints a client secret for the AGENT
# app via Graph addPassword. Returns the secretText, which goes STRAIGHT into the
# AgentCore Token Vault (a MicrosoftOauth2 credential provider) and is NEVER
# persisted/logged by us. Takes the application OBJECT id (the /applications/{objId}
# directory id), NOT the appId/clientId GUID.
# ===========================================================================
@pytest.mark.asyncio
async def test_add_agent_password_returns_secret():
    """POST /applications/{objId}/addPassword → returns the response's secretText.
    Asserts the endpoint, the displayName in the passwordCredential body, and that the
    returned value is exactly the Graph-minted secretText."""
    app_object_id = "agent-app-object-id"
    secret_text = "the-minted-client-secret-value"
    captured: dict[str, dict] = {}
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "POST" and path == (
            f"/v1.0/applications/{app_object_id}/addPassword"
        ):
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "keyId": "key-guid-1",
                    "displayName": "agp-obo",
                    "secretText": secret_text,
                    "endDateTime": "2027-06-03T00:00:00Z",
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    returned = await svc.add_agent_password(app_object_id, display_name="agp-obo")

    # Hit the addPassword endpoint for the given application OBJECT id.
    assert (
        "POST",
        f"/v1.0/applications/{app_object_id}/addPassword",
    ) in calls
    # The passwordCredential body carries the displayName.
    body = captured["body"]
    assert body["passwordCredential"]["displayName"] == "agp-obo"
    # Returns exactly the Graph-minted secretText (the vaulted secret).
    assert returned == secret_text


@pytest.mark.asyncio
async def test_add_agent_password_secret_never_logged(caplog):
    """SECURITY (extends CRITIQUE-FIX-E to add_agent_password): the minted secretText
    must NEVER reach a log record. We drive the success path (which mints the secret
    sentinel) with logging captured at DEBUG and assert the sentinel is absent."""
    app_object_id = "agent-app-object-id"
    secret_sentinel = "ADD_PASSWORD_SECRET_SENTINEL_DO_NOT_LEAK"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "POST" and path == (
            f"/v1.0/applications/{app_object_id}/addPassword"
        ):
            return httpx.Response(
                200,
                json={"keyId": "k1", "secretText": secret_sentinel},
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with caplog.at_level(logging.DEBUG):
        returned = await svc.add_agent_password(app_object_id)
        assert returned == secret_sentinel

    # The minted secret never appears in any captured log record.
    all_logs = "\n".join(rec.getMessage() for rec in caplog.records)
    assert secret_sentinel not in all_logs


# ===========================================================================
# grant_agent_obo_consent (E7, T-GRAPH) — the agent-as-client analog of
# grant_backend_obo_consent. The ONLY difference: clientId is the AGENT SP
# (passed in), not the backend SP (resolved). Reuses the already-exists swallow
# + replication-retry loop verbatim.
# ===========================================================================
@pytest.mark.asyncio
async def test_grant_agent_obo_consent_posts_grant_with_agent_client():
    """POST /oauth2PermissionGrants with the AGENT SP as clientId, the MCP SP as
    resourceId, consentType AllPrincipals, scope Invoke. No backend-SP GET lookup
    (the client is passed in, not resolved)."""
    agent_sp_id = "agent-sp-obj"
    mcp_sp_id = "mcp-sp-obj"
    captured: dict[str, object] = {}
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "POST" and path.endswith("/oauth2PermissionGrants"):
            captured["grant"] = json.loads(request.content.decode())
            return httpx.Response(201, json={"id": "grant-id"})
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    await svc.grant_agent_obo_consent(agent_sp_id, mcp_sp_id)

    grant = captured["grant"]
    assert grant["clientId"] == agent_sp_id  # the AGENT SP (passed in).
    assert grant["resourceId"] == mcp_sp_id  # the MCP SP.
    assert grant["consentType"] == "AllPrincipals"
    assert grant["scope"] == "Invoke"
    # No backend-SP resolution GET (unlike grant_backend_obo_consent): the agent
    # SP is supplied directly, so there is no /servicePrincipals $filter lookup.
    assert not any(
        m == "GET" and p.endswith("/servicePrincipals") for (m, p) in calls
    )


@pytest.mark.asyncio
async def test_grant_agent_obo_consent_ignores_already_exists():
    """Idempotent: a duplicate-grant error is swallowed (no raise)."""
    agent_sp_id = "agent-sp-obj"
    mcp_sp_id = "mcp-sp-obj"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "POST" and path.endswith("/oauth2PermissionGrants"):
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "Request_BadRequest",
                        "message": "Permission entry already exists.",
                    }
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    # Must NOT raise.
    await svc.grant_agent_obo_consent(agent_sp_id, mcp_sp_id)


# ===========================================================================
# revoke_agent_obo_consent (E7 security-fix) — the DELETE twin of
# grant_agent_obo_consent. Resolves the agent→MCP oauth2PermissionGrant via a
# $filter on clientId+resourceId, then DELETEs each match. IDEMPOTENT: no match,
# or a 404 on the DELETE (already gone), is the DESIRED end state (swallow it).
# This makes the UI "revoke" a REAL OBO kill switch — without it, deleting the
# app-role assignment leaves the consent grant, so the agent can still OBO-reach
# the MCP (AADSTS65001 only fires when the consent is ALSO gone).
# ===========================================================================
@pytest.mark.asyncio
async def test_revoke_agent_obo_consent_deletes_matching_grant():
    """GET /servicePrincipals/{agent_sp}/oauth2PermissionGrants (the NAVIGATION
    property — NOT a top-level $filter, which Graph evaluates unreliably) returns the
    agent's grants; the one whose resourceId == the MCP SP is matched IN-MEMORY and a
    DELETE to /oauth2PermissionGrants/{id} is issued. A grant to a DIFFERENT resource
    is left alone (so the delete is scoped to exactly this (agent, MCP) pair)."""
    agent_sp_id = "agent-sp-obj"
    mcp_sp_id = "mcp-sp-obj"
    grant_id = "grant-id-1"
    other_grant_id = "grant-id-other-mcp"
    captured: dict[str, object] = {}
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path.endswith("/oauth2PermissionGrants"):
            captured["get_path"] = path
            # The nav read returns ALL of the agent's grants; only the one whose
            # resourceId matches this MCP SP must be deleted (in-memory filter).
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": grant_id, "resourceId": mcp_sp_id, "scope": "Invoke"},
                        {"id": other_grant_id, "resourceId": "some-other-mcp-sp"},
                    ]
                },
            )
        if request.method == "DELETE" and path == (
            f"/v1.0/oauth2PermissionGrants/{grant_id}"
        ):
            captured["deleted"] = True
            return httpx.Response(204)
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    result = await svc.revoke_agent_obo_consent(agent_sp_id, mcp_sp_id)

    assert result is None
    # The DELETE was issued against the resolved (matching) grant id.
    assert captured.get("deleted") is True
    assert (
        "DELETE",
        f"/v1.0/oauth2PermissionGrants/{grant_id}",
    ) in calls
    # The grant for a DIFFERENT resource was NOT deleted (in-memory resourceId scope).
    assert (
        "DELETE",
        f"/v1.0/oauth2PermissionGrants/{other_grant_id}",
    ) not in calls
    # Resolved via the agent SP's navigation property — NOT a top-level collection $filter.
    assert (
        captured["get_path"]
        == f"/v1.0/servicePrincipals/{agent_sp_id}/oauth2PermissionGrants"
    )


@pytest.mark.asyncio
async def test_revoke_agent_obo_consent_no_match_is_noop():
    """GET returns an empty value list (the grant is already gone / never existed)
    → NO DELETE is issued and the call does NOT raise (idempotent: the desired end
    state — no agent→MCP consent — already holds)."""
    agent_sp_id = "agent-sp-obj"
    mcp_sp_id = "mcp-sp-obj"
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path.endswith("/oauth2PermissionGrants"):
            return httpx.Response(200, json={"value": []})
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    # Must NOT raise.
    result = await svc.revoke_agent_obo_consent(agent_sp_id, mcp_sp_id)

    assert result is None
    # NO DELETE issued (nothing to revoke).
    assert not any(m == "DELETE" for (m, _p) in calls)


@pytest.mark.asyncio
async def test_revoke_agent_obo_consent_swallows_404_on_delete():
    """A 404 on the DELETE (a concurrent revoke already removed the grant between
    the GET and the DELETE) is the DESIRED end state — swallow it, never raise."""
    agent_sp_id = "agent-sp-obj"
    mcp_sp_id = "mcp-sp-obj"
    grant_id = "grant-id-1"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path.endswith("/oauth2PermissionGrants"):
            return httpx.Response(
                200, json={"value": [{"id": grant_id, "resourceId": mcp_sp_id}]}
            )
        if request.method == "DELETE" and path == (
            f"/v1.0/oauth2PermissionGrants/{grant_id}"
        ):
            return httpx.Response(
                404,
                json={
                    "error": {
                        "code": "Request_ResourceNotFound",
                        "message": "Resource not found for the segment.",
                    }
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    # Must NOT raise — already-gone is the desired end state.
    result = await svc.revoke_agent_obo_consent(agent_sp_id, mcp_sp_id)

    assert result is None


@pytest.mark.asyncio
async def test_revoke_agent_obo_consent_reraises_non_404():
    """A non-404 GraphError on the DELETE (e.g. 403 permissions, 500) must RAISE —
    the revoke genuinely failed and the operator must be able to act on it (re-revoke
    is idempotent). Only 404 (already gone) is swallowed."""
    agent_sp_id = "agent-sp-obj"
    mcp_sp_id = "mcp-sp-obj"
    grant_id = "grant-id-1"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path.endswith("/oauth2PermissionGrants"):
            return httpx.Response(
                200, json={"value": [{"id": grant_id, "resourceId": mcp_sp_id}]}
            )
        if request.method == "DELETE" and path == (
            f"/v1.0/oauth2PermissionGrants/{grant_id}"
        ):
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": "Authorization_RequestDenied",
                        "message": "Insufficient privileges to complete the operation.",
                    }
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(GraphError) as exc_info:
        await svc.revoke_agent_obo_consent(agent_sp_id, mcp_sp_id)

    assert exc_info.value.status == 403


@pytest.mark.asyncio
async def test_revoke_agent_obo_consent_guards_filter_delimiter():
    """Defensive (mirrors get_application_object_id's single-quote guard at
    graph_service.py:836): a single-quote in an id would break the OData $filter
    string literal, so the ids are guarded before interpolation. A single-quote in
    either id raises GraphError(400) and issues NO Graph call."""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(GraphError) as exc_info:
        await svc.revoke_agent_obo_consent("agent'; DROP", "mcp-sp-obj")
    assert exc_info.value.status == 400
    # No /oauth2PermissionGrants call was made (guarded before interpolation).
    assert not any(p.endswith("/oauth2PermissionGrants") for (_m, p) in calls)


@pytest.mark.asyncio
async def test_revoke_agent_obo_consent_deletes_all_matches():
    """Defensive: if the GET returns MULTIPLE grants for the same (agent, MCP) pair
    (a directory anomaly — normally there is exactly one), EVERY match is DELETEd so
    no stale consent survives the revoke."""
    agent_sp_id = "agent-sp-obj"
    mcp_sp_id = "mcp-sp-obj"
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path.endswith("/oauth2PermissionGrants"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "grant-a", "resourceId": mcp_sp_id},
                        {"id": "grant-b", "resourceId": mcp_sp_id},
                    ]
                },
            )
        if request.method == "DELETE" and "/oauth2PermissionGrants/" in path:
            deleted.append(path.rsplit("/", 1)[-1])
            return httpx.Response(204)
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    await svc.revoke_agent_obo_consent(agent_sp_id, mcp_sp_id)

    assert set(deleted) == {"grant-a", "grant-b"}


# ===========================================================================
# list_agent_mcp_grants (E7, T-GRAPH) — principal-side reverse read. Distinct
# endpoint from appRoleAssignedTo: appRoleAssignments == "what this agent is
# assigned TO" (research §2.3).
# ===========================================================================
@pytest.mark.asyncio
async def test_list_agent_mcp_grants_returns_inline_resource_fields():
    """GET /servicePrincipals/{agent_sp}/appRoleAssignments returns the inline list
    carrying resourceId / resourceDisplayName / appRoleId (the principal-side read,
    distinct from the resource-side appRoleAssignedTo)."""
    agent_sp_id = "agent-sp-obj"
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path == (
            f"/v1.0/servicePrincipals/{agent_sp_id}/appRoleAssignments"
        ):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "grant-assign-1",
                            "principalId": agent_sp_id,
                            "appRoleId": "mcp-invoker-role-id",
                            "resourceId": "mcp-sp-obj",
                            "resourceDisplayName": "Weather MCP",
                        },
                        {
                            "id": "grant-assign-2",
                            "principalId": agent_sp_id,
                            "appRoleId": "mcp2-invoker-role-id",
                            "resourceId": "mcp2-sp-obj",
                            "resourceDisplayName": "Finance MCP",
                        },
                    ]
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    grants = await svc.list_agent_mcp_grants(agent_sp_id)

    # Hit the principal-side endpoint, NOT the resource-side appRoleAssignedTo.
    assert (
        "GET",
        f"/v1.0/servicePrincipals/{agent_sp_id}/appRoleAssignments",
    ) in calls
    assert not any(p.endswith("/appRoleAssignedTo") for (_m, p) in calls)

    assert len(grants) == 2
    first = grants[0]
    # resource fields come INLINE (no extra resolve call).
    assert first["resourceId"] == "mcp-sp-obj"
    assert first["resourceDisplayName"] == "Weather MCP"
    assert first["appRoleId"] == "mcp-invoker-role-id"
    assert grants[1]["resourceId"] == "mcp2-sp-obj"


# ===========================================================================
# assign_app_role — REUSED UNCHANGED for an SP principal (research §2.2). The
# body carries NO principalType — Graph infers it from the principal object, so
# assigning an agent SP to an MCP role needs ZERO code change.
# ===========================================================================
@pytest.mark.asyncio
async def test_assign_app_role_unchanged_for_sp_principal():
    """Calling assign_app_role for an SP principal (agent SP → MCP Invoker role)
    emits the SAME body shape as the user case: principalId / resourceId / appRoleId
    and NO principalType (Graph infers it). Proves reuse-unchanged for E7."""
    mcp_sp_id = "mcp-sp-obj"
    agent_sp_id = "agent-sp-obj"
    captured: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "POST" and path == (
            f"/v1.0/servicePrincipals/{mcp_sp_id}/appRoleAssignedTo"
        ):
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(
                201,
                json={
                    "id": "new-agent-assign-id",
                    "principalId": agent_sp_id,
                    "principalType": "ServicePrincipal",
                    "appRoleId": "mcp-invoker-role-id",
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    result = await svc.assign_app_role(mcp_sp_id, agent_sp_id, "mcp-invoker-role-id")

    body = captured["body"]
    assert body["principalId"] == agent_sp_id
    assert body["resourceId"] == mcp_sp_id
    assert body["appRoleId"] == "mcp-invoker-role-id"
    # CRITICAL: NO principalType in the body — Graph infers it from the principal.
    assert "principalType" not in body
    assert result["id"] == "new-agent-assign-id"


# ===========================================================================
# Secret redaction for the NEW E7 methods (extends CRITIQUE-FIX-E to
# create_mcp_app + grant_agent_obo_consent). Both new methods mint the app
# Bearer token from the client secret (via _app_token) on every Graph call;
# this asserts that the secret — supplied by the loader — never reaches a log
# record. The forced errors use REALISTIC benign Graph resource bodies (Graph
# never echoes the client secret in a resource error body — the redaction
# boundary is the /token path, already pinned by test_secret_and_tokens_never_
# logged). We assert the loader's secret is not constructed into the raised
# error's str() either (these methods never put the secret into a message).
# ===========================================================================
@pytest.mark.asyncio
async def test_secret_never_logged_by_new_e7_methods(caplog):
    secret_sentinel = "E7_SECRET_SENTINEL_DO_NOT_LEAK"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        # Benign resource errors (the realistic shape — a Graph resource body
        # never carries our client secret). 403 → both new methods fail fast.
        if request.method == "POST" and path.endswith("/applications"):
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": "Authorization_RequestDenied",
                        "message": "Insufficient privileges to complete the operation.",
                    }
                },
            )
        if request.method == "POST" and path.endswith("/oauth2PermissionGrants"):
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": "Authorization_RequestDenied",
                        "message": "Insufficient privileges to complete the operation.",
                    }
                },
            )
        # add_agent_password (Tier-2) on a 403 also fails fast with a benign body.
        if request.method == "POST" and path.endswith("/addPassword"):
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": "Authorization_RequestDenied",
                        "message": "Insufficient privileges to complete the operation.",
                    }
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler, secret_loader=lambda: secret_sentinel)

    with caplog.at_level(logging.DEBUG):
        # create_mcp_app on a 403 (non-replication) fails fast — capture the error.
        with pytest.raises(GraphError) as mcp_exc:
            await svc.create_mcp_app("mcp-leak", "Weather MCP")
        # grant_agent_obo_consent on a 403 fails fast — capture the error.
        with pytest.raises(GraphError) as grant_exc:
            await svc.grant_agent_obo_consent("agent-sp-obj", "mcp-sp-obj")
        # add_agent_password on a 403 fails fast — capture the error.
        with pytest.raises(GraphError) as pwd_exc:
            await svc.add_agent_password("agent-app-object-id")

    # The client secret (resolved via the loader) is never placed into any
    # raised exception's str() — these methods carry only status + Graph code
    # (+ the benign resource message), never the secret.
    assert secret_sentinel not in str(mcp_exc.value)
    assert secret_sentinel not in str(grant_exc.value)
    assert secret_sentinel not in str(pwd_exc.value)

    # No captured log record may contain the client secret.
    all_logs = "\n".join(rec.getMessage() for rec in caplog.records)
    assert secret_sentinel not in all_logs


# ===========================================================================
# list_member_group_ids (E9/F1 — the groups-overage fallback)
# ===========================================================================
@pytest.mark.asyncio
async def test_list_member_group_ids_returns_group_ids():
    """Happy path: GET /users/{oid}/transitiveMemberOf/microsoft.graph.group
    → returns the group object-ids from the response ``value``."""
    user_oid = "alice-oid"
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path == (
            f"/v1.0/users/{user_oid}/transitiveMemberOf/microsoft.graph.group"
        ):
            seen_paths.append(path)
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "group-claims"},
                        {"id": "group-marketing"},
                    ]
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    ids = await svc.list_member_group_ids(user_oid)

    assert ids == ["group-claims", "group-marketing"]
    assert seen_paths  # the transitiveMemberOf/group endpoint was hit


@pytest.mark.asyncio
async def test_list_member_group_ids_paginates_odata_nextlink():
    """Pagination: follows ``@odata.nextLink`` across pages, concatenating ids."""
    user_oid = "bob-oid"
    base = f"/v1.0/users/{user_oid}/transitiveMemberOf/microsoft.graph.group"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = request.url.query.decode()  # httpx.URL.query is bytes
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path == base and "page2" not in query:
            return httpx.Response(
                200,
                json={
                    # nextLink is an absolute URL; `base` already carries the /v1.0
                    # path prefix, so the host (sans the version segment) is prepended.
                    "value": [{"id": "g1"}, {"id": "g2"}],
                    "@odata.nextLink": f"https://graph.microsoft.com{base}?$skiptoken=page2",
                },
            )
        if request.method == "GET" and path == base and "page2" in query:
            return httpx.Response(200, json={"value": [{"id": "g3"}]})
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    ids = await svc.list_member_group_ids(user_oid)

    assert ids == ["g1", "g2", "g3"]


@pytest.mark.asyncio
async def test_list_member_group_ids_empty_when_no_groups():
    """No group memberships → empty list (not an error)."""
    user_oid = "carol-oid"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path.endswith(
            "/transitiveMemberOf/microsoft.graph.group"
        ):
            return httpx.Response(200, json={"value": []})
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    ids = await svc.list_member_group_ids(user_oid)

    assert ids == []


@pytest.mark.asyncio
async def test_list_member_group_ids_raises_on_graph_error():
    """Best-effort fail-closed: on a Graph error the method RAISES (the caller
    decides fail-closed) — it does NOT silently return []."""
    user_oid = "dave-oid"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path.endswith(
            "/transitiveMemberOf/microsoft.graph.group"
        ):
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": "Authorization_RequestDenied",
                        "message": "Insufficient privileges.",
                    }
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(GraphError):
        await svc.list_member_group_ids(user_oid)


@pytest.mark.asyncio
async def test_list_member_group_ids_wraps_transport_error_in_graph_error():
    """E24/T5 carry-forward: a TRANSPORT failure (timeout, connect error — raised on
    the request itself, not returned as a response) must ALSO surface as
    ``GraphError`` so ``TenantResolver._group_ids``'s ``except GraphError: return []``
    catches it uniformly, instead of leaking a raw ``httpx.HTTPError`` that would
    crash the resolve."""
    user_oid = "erin-oid"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path.endswith(
            "/transitiveMemberOf/microsoft.graph.group"
        ):
            raise httpx.ConnectTimeout("connection timed out")
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(GraphError):
        await svc.list_member_group_ids(user_oid)


# ===========================================================================
# get_principal (E11/T2 — read-only Entra by-oid lookup for a USER or GROUP
# node detail). kind="user" → GET /users/{oid}?$select=displayName,
# userPrincipalName,mail,jobTitle; kind="group" → GET /groups/{oid}?$select=
# displayName,mail. Returns the raw Graph object dict. RAISES GraphError on a
# non-2xx (no body leak). A bad ``kind`` raises ValueError (defensive).
# ===========================================================================
@pytest.mark.asyncio
async def test_get_principal_user_selects_user_fields_and_returns_dict():
    """kind="user" issues GET /users/{oid} with the user $select and returns the
    parsed Graph object dict verbatim."""
    user_oid = "oid-1"
    captured: dict[str, str] = {}
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path == f"/v1.0/users/{user_oid}":
            captured["select"] = request.url.params.get("$select", "")
            return httpx.Response(
                200,
                json={
                    "id": user_oid,
                    "displayName": "Maria Bauer",
                    "userPrincipalName": "maria.bauer@example.onmicrosoft.com",
                    "mail": "maria.bauer@example.com",
                    "jobTitle": "Claims Officer",
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    result = await svc.get_principal(user_oid, "user")

    # Hit the by-oid /users endpoint (NOT the $search /users picker).
    assert ("GET", f"/v1.0/users/{user_oid}") in calls
    # The user $select carries exactly the four documented fields.
    assert captured["select"] == "displayName,userPrincipalName,mail,jobTitle"
    # The raw Graph object dict is returned verbatim (no post-processing here).
    assert result["id"] == user_oid
    assert result["displayName"] == "Maria Bauer"
    assert result["userPrincipalName"] == "maria.bauer@example.onmicrosoft.com"
    assert result["mail"] == "maria.bauer@example.com"
    assert result["jobTitle"] == "Claims Officer"


@pytest.mark.asyncio
async def test_get_principal_user_tolerates_absent_optional_fields():
    """Graph may omit mail/jobTitle — get_principal returns whatever the dict has
    (no shaping, no KeyError)."""
    user_oid = "oid-min"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path == f"/v1.0/users/{user_oid}":
            # mail + jobTitle omitted entirely.
            return httpx.Response(
                200,
                json={
                    "id": user_oid,
                    "displayName": "Hans Mueller",
                    "userPrincipalName": "hans@example.onmicrosoft.com",
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    result = await svc.get_principal(user_oid, "user")

    assert result["displayName"] == "Hans Mueller"
    # Absent optional fields are simply not in the dict (tolerated, not raised).
    assert "mail" not in result
    assert "jobTitle" not in result


@pytest.mark.asyncio
async def test_get_principal_group_selects_group_fields():
    """kind="group" issues GET /groups/{oid} with the group $select (displayName,
    mail) and returns the parsed dict."""
    group_oid = "gid-1"
    captured: dict[str, str] = {}
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path == f"/v1.0/groups/{group_oid}":
            captured["select"] = request.url.params.get("$select", "")
            return httpx.Response(
                200,
                json={
                    "id": group_oid,
                    "displayName": "Contoso-Claims-Officers",
                    "mail": "claims-officers@example.com",
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    result = await svc.get_principal(group_oid, "group")

    # Hit the by-oid /groups endpoint.
    assert ("GET", f"/v1.0/groups/{group_oid}") in calls
    # The group $select carries exactly the two documented fields.
    assert captured["select"] == "displayName,mail"
    assert result["id"] == group_oid
    assert result["displayName"] == "Contoso-Claims-Officers"
    assert result["mail"] == "claims-officers@example.com"


@pytest.mark.asyncio
async def test_get_principal_raises_graph_error_on_non_2xx_no_body_leak():
    """A non-2xx (here 404) → GraphError carrying ONLY status + code (mirrors the
    repo's GraphError discipline). The raw resource body is never propagated."""
    user_oid = "oid-missing"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path == f"/v1.0/users/{user_oid}":
            return httpx.Response(
                404,
                json={
                    "error": {
                        "code": "Request_ResourceNotFound",
                        "message": "Resource does not exist.",
                    }
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(GraphError) as exc_info:
        await svc.get_principal(user_oid, "user")

    err = exc_info.value
    assert err.status == 404
    assert err.code == "Request_ResourceNotFound"


@pytest.mark.asyncio
async def test_get_principal_rejects_bad_kind_with_value_error():
    """Defensive: a kind other than "user"/"group" raises ValueError and issues NO
    Graph call (the route layer validates, but the method guards too)."""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(ValueError):
        await svc.get_principal("oid-x", "servicePrincipal")

    # No /users or /groups by-oid call was made (guarded before any request).
    assert not any(
        p.startswith("/v1.0/users/") or p.startswith("/v1.0/groups/")
        for (_m, p) in calls
    )


# ===========================================================================
# resolve_platform_sp (E16)
# ===========================================================================
PLATFORM_CLIENT_ID = "spa-client-guid"


def _platform_sp_response() -> httpx.Response:
    """A $filter=appId eq '...'&$select=id,appRoles servicePrincipals response."""
    return httpx.Response(
        200,
        json={
            "value": [
                {
                    "id": "platform-sp-obj-id",
                    "appRoles": [
                        {"id": "admin-guid", "value": "Platform.Admin", "isEnabled": True},
                        {"id": "operator-guid", "value": "Platform.Operator", "isEnabled": True},
                        {"id": "viewer-guid", "value": "Platform.Viewer", "isEnabled": True},
                        {"id": "disabled-guid", "value": "Platform.Old", "isEnabled": False},
                    ],
                }
            ]
        },
    )


@pytest.mark.asyncio
async def test_resolve_platform_sp_maps_value_to_role_id():
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and request.url.path == "/v1.0/servicePrincipals":
            # httpx percent-encodes the query string, so assert against the
            # DECODED $filter param (matches the existing idiom at :606).
            assert "appId eq" in (request.url.params.get("$filter") or "")
            return _platform_sp_response()
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    sp_id, role_map = await svc.resolve_platform_sp(PLATFORM_CLIENT_ID)

    assert sp_id == "platform-sp-obj-id"
    # disabled appRole is excluded; enabled ones are mapped value -> id.
    assert role_map == {
        "Platform.Admin": "admin-guid",
        "Platform.Operator": "operator-guid",
        "Platform.Viewer": "viewer-guid",
    }


@pytest.mark.asyncio
async def test_resolve_platform_sp_is_cached():
    sp_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.url.path == "/v1.0/servicePrincipals":
            sp_calls["n"] += 1
            return _platform_sp_response()
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    await svc.resolve_platform_sp(PLATFORM_CLIENT_ID)
    await svc.resolve_platform_sp(PLATFORM_CLIENT_ID)
    assert sp_calls["n"] == 1  # second call served from cache


@pytest.mark.asyncio
async def test_resolve_platform_sp_no_match_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.url.path == "/v1.0/servicePrincipals":
            return httpx.Response(200, json={"value": []})
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(GraphError):
        await svc.resolve_platform_sp(PLATFORM_CLIENT_ID)


@pytest.mark.asyncio
async def test_resolve_platform_sp_empty_roles_not_cached_refetches():
    """SP exists but its Platform.* roles aren't defined yet → empty map, NOT cached.

    Models the first-run order (create app reg → define roles): a successful resolve
    with no enabled appRoles must NOT be cached, so the next call re-fetches and would
    pick up the roles once an operator defines them.
    """
    sp_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.url.path == "/v1.0/servicePrincipals":
            sp_calls["n"] += 1
            # SP found, but no appRoles defined/enabled yet.
            return httpx.Response(
                200, json={"value": [{"id": "platform-sp-obj-id", "appRoles": []}]}
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    sp_id, role_map = await svc.resolve_platform_sp(PLATFORM_CLIENT_ID)
    assert sp_id == "platform-sp-obj-id"
    assert role_map == {}

    await svc.resolve_platform_sp(PLATFORM_CLIENT_ID)
    assert sp_calls["n"] == 2  # empty result was not cached, so it re-fetched


@pytest.mark.asyncio
async def test_resolve_platform_sp_rejects_single_quote_without_http():
    """A client id containing the OData $filter delimiter raises 400 before any HTTP."""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(GraphError) as exc:
        await svc.resolve_platform_sp("evil' or 1 eq 1")
    assert exc.value.status == 400
    # No Graph call (and no token fetch) was issued — guarded before the request.
    assert calls == []


# ===========================================================================
# delete_agent_app (E23/T1) — the DELETE twin of create_agent_app: tears down
# the agent's Entra application registration (which cascades its service
# principal) so a repo-teardown leaves no orphaned identity. Resolves the app
# OBJECT id from the stored client GUID via get_application_object_id, then
# DELETEs /applications/{objId}; if there is no app id it falls back to
# DELETEing the SP directly. IDEMPOTENT — the desired end state is "no agent
# app/SP", so a None/404 resolve, a 404 on the DELETE, or both ids blank is a
# no-op success (mirrors revoke_agent_obo_consent's 404-swallow idiom).
# ===========================================================================
@pytest.mark.asyncio
async def test_delete_agent_app_deletes_application_by_resolved_object_id():
    """entra_app_id (the client GUID) resolves to the directory OBJECT id via
    get_application_object_id, and a DELETE is issued against
    /applications/{objId} — NOT against the client GUID and NOT the SP."""
    client_guid = "client-guid"
    object_id = "obj-123"
    sp_id = "sp-1"
    recorded_calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        recorded_calls.append((request.method, path))
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        # get_application_object_id resolves the client GUID → object id.
        if request.method == "GET" and path.endswith("/applications"):
            return httpx.Response(200, json={"value": [{"id": object_id}]})
        if request.method == "DELETE" and path == f"/v1.0/applications/{object_id}":
            return httpx.Response(204)
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    result = await svc.delete_agent_app(entra_app_id=client_guid, entra_sp_id=sp_id)

    assert result is None
    assert ("DELETE", f"/v1.0/applications/{object_id}") in recorded_calls
    # The app delete cascades the SP in Entra — we do NOT also DELETE the SP.
    assert not any(
        m == "DELETE" and p.endswith(f"/servicePrincipals/{sp_id}")
        for (m, p) in recorded_calls
    )


@pytest.mark.asyncio
async def test_delete_agent_app_falls_back_to_sp_when_no_app_id():
    """When entra_app_id is falsy but entra_sp_id is set, delete the SP directly
    (DELETE /servicePrincipals/{sp_id}) — no application resolve is attempted."""
    sp_id = "sp-1"
    recorded_calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        recorded_calls.append((request.method, path))
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "DELETE" and path == f"/v1.0/servicePrincipals/{sp_id}":
            return httpx.Response(204)
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    result = await svc.delete_agent_app(entra_app_id=None, entra_sp_id=sp_id)

    assert result is None
    assert ("DELETE", f"/v1.0/servicePrincipals/{sp_id}") in recorded_calls
    # No application resolve when there is no app id to resolve.
    assert not any(m == "GET" and p.endswith("/applications") for (m, p) in recorded_calls)


@pytest.mark.asyncio
async def test_delete_agent_app_is_idempotent_when_app_not_found():
    """The app id no longer resolves (get_application_object_id would raise a 404
    on an empty value list) → the desired end state (no app) already holds, so
    delete_agent_app swallows it and does NOT raise."""
    recorded_calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        recorded_calls.append((request.method, path))
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        # Empty value → get_application_object_id raises GraphError(404).
        if request.method == "GET" and path.endswith("/applications"):
            return httpx.Response(200, json={"value": []})
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    # Must NOT raise — already-gone is the desired end state.
    result = await svc.delete_agent_app(entra_app_id="gone", entra_sp_id=None)

    assert result is None
    # No DELETE issued (there was nothing to delete).
    assert not any(m == "DELETE" for (m, _p) in recorded_calls)


@pytest.mark.asyncio
async def test_delete_agent_app_swallows_404_on_delete():
    """A 404 on the DELETE itself (the app was resolved but removed concurrently)
    is the DESIRED end state — swallow it, never raise."""
    object_id = "obj-123"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path.endswith("/applications"):
            return httpx.Response(200, json={"value": [{"id": object_id}]})
        if request.method == "DELETE" and path == f"/v1.0/applications/{object_id}":
            return httpx.Response(
                404,
                json={
                    "error": {
                        "code": "Request_ResourceNotFound",
                        "message": "Resource not found for the segment.",
                    }
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    # Must NOT raise — already-gone is the desired end state.
    result = await svc.delete_agent_app(entra_app_id="client-guid", entra_sp_id=None)

    assert result is None


@pytest.mark.asyncio
async def test_delete_agent_app_reraises_non_404_on_delete():
    """A non-404 GraphError on the DELETE (e.g. 403 permissions) must RAISE — the
    teardown genuinely failed and the operator must be able to act on it (re-delete
    is idempotent). Only 404 (already gone) is swallowed."""
    object_id = "obj-123"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "GET" and path.endswith("/applications"):
            return httpx.Response(200, json={"value": [{"id": object_id}]})
        if request.method == "DELETE" and path == f"/v1.0/applications/{object_id}":
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": "Authorization_RequestDenied",
                        "message": "Insufficient privileges to complete the operation.",
                    }
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    with pytest.raises(GraphError) as exc_info:
        await svc.delete_agent_app(entra_app_id="client-guid", entra_sp_id=None)

    assert exc_info.value.status == 403


@pytest.mark.asyncio
async def test_delete_agent_app_swallows_404_on_sp_fallback():
    """A 404 on the SP-fallback DELETE (SP already gone) is the desired end state —
    swallow it, never raise."""
    sp_id = "sp-1"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        if request.method == "DELETE" and path == f"/v1.0/servicePrincipals/{sp_id}":
            return httpx.Response(
                404,
                json={
                    "error": {
                        "code": "Request_ResourceNotFound",
                        "message": "Resource not found for the segment.",
                    }
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    result = await svc.delete_agent_app(entra_app_id=None, entra_sp_id=sp_id)

    assert result is None


@pytest.mark.asyncio
async def test_delete_agent_app_noop_when_both_ids_blank():
    """Both ids blank → nothing to tear down. No Graph call (not even a token
    fetch) and no DELETE; must not raise."""
    recorded_calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded_calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/oauth2/v2.0/token"):
            return _app_token_response()
        return httpx.Response(500, json={"error": "unexpected"})

    svc = _build(handler)
    result = await svc.delete_agent_app(entra_app_id=None, entra_sp_id=None)

    assert result is None
    # Nothing to do — no DELETE, and no Graph call at all.
    assert not any(m == "DELETE" for (m, _p) in recorded_calls)
    assert recorded_calls == []
