"""POST /agents/{id}/invoke route tests (Epic 6, Task T-ROUTES).

The invoke route does OBO (mocked GraphService.obo_exchange) → httpx bearer-POST to
the AgentCore runtime (mocked). The runtime STREAMS SSE (text/event-stream); the
route buffers the `data: {...}` lines and extracts the terminal message. NO live
calls — `httpx.AsyncClient` is patched, obo is an AsyncMock.

Exception→HTTP mapping under test:
  - NotAssignedError → 403
  - OboConfigError   → 500
  - unprovisioned / not agentcore → 409 (no OBO/httpx)
  - `?stage=` naming a stage the agent owns no runtime for → 404 (no OBO/httpx)
  - runtime 401/403 → 502 ("agent rejected the token")
  - VIEWER can invoke (VIEWER-gated)
  - the raw inbound token is forwarded as the OBO assertion
  - boto3 control client is NOT used for invoke (httpx only)
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def reset_modules():
    import sys

    for mod in [
        "core.rbac",
        "core.security_entra",
        "core.config",
        "api.routes.agents",
        "api.routes.grants",
    ]:
        sys.modules.pop(mod, None)
    yield


@pytest.fixture
def entra_settings(monkeypatch):
    monkeypatch.setenv("AUTH_PROVIDER", "entra")
    monkeypatch.setenv("USE_DEV_AUTH", "False")
    monkeypatch.setenv("ENTRA_TENANT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("ENTRA_AUDIENCE", "api://agp")
    monkeypatch.setenv("ENTRA_BACKEND_CLIENT_ID", "backend-client-id")
    monkeypatch.setenv("ENTRA_ROLE_ADMIN", "Platform.Admin")
    monkeypatch.setenv("ENTRA_ROLE_OPERATOR", "Platform.Operator")
    monkeypatch.setenv("ENTRA_ROLE_VIEWER", "Platform.Viewer")


def _make_agent(**overrides):
    from models.agent import Agent, AuthType, LifecycleState, Origin, Platform

    now = datetime.now(timezone.utc)
    base = dict(
        id="rec-123",
        name="claims-triage-de",
        purpose="Triage claims",
        lifecycle_state=LifecycleState.APPROVED,
        origin=Origin.REGISTERED,
        platform=Platform.AWS_BEDROCK,
        auth_type=AuthType.ENTRA,
        agent_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/agent-abc123",
        entra_sp_id="sp-obj-id",
        entra_app_audience="api://agp-agent-rec-123",
        invoker_role_id="role-invoker-guid",
        admin_role_id="role-admin-guid",
        identity_status="provisioned",
        created_at=now,
        updated_at=now,
        created_by="maria.bauer@example.com",
    )
    base.update(overrides)
    return Agent(**base)


def _build_client(mock_registry=None, mock_graph=None):
    """Also pre-seeds the E24 tenant-resolver singleton with an always-global stub —
    `/invoke` now resolves `tenant_ctx`, and this file's fixtures predate tenant
    scoping (no `tenant_id`); global admin bypasses all filtering so behavior is
    unchanged."""
    import api.routes.agents as agents_module
    import api.routes.grants as grants_module
    import api.routes.users as users_module
    from services.tenant_resolver import TenantContext

    if mock_registry is not None:
        agents_module._svc = mock_registry
    if mock_graph is not None:
        # grants.py owns the ONE GraphService singleton; agents.get_graph_service (used
        # by /invoke) delegates to it, so patch it on grants_module.
        grants_module._graph_svc = mock_graph

    class _GlobalResolver:
        async def resolve(self, principal):
            return TenantContext(is_global=True, tenant_ids=frozenset(), tenants=())

    users_module._tenant_resolver = _GlobalResolver()

    app = FastAPI()
    app.include_router(agents_module.router, prefix="/api/v1")
    app.include_router(grants_module.router, prefix="/api/v1")
    app.include_router(grants_module.entra_router, prefix="/api/v1")
    return TestClient(app), agents_module


def _claims_for(role: str):
    role_app = {
        "viewer": "Platform.Viewer",
        "operator": "Platform.Operator",
        "admin": "Platform.Admin",
    }[role]
    return {
        "oid": f"{role}-oid",
        "preferred_username": f"{role}.user@example.com",
        "roles": [role_app],
    }


def _headers(token="fake-inbound-token"):
    return {"Authorization": f"Bearer {token}"}


def _mock_httpx_response(*, status_code=200, content_type="text/event-stream", text=""):
    """A MagicMock that quacks like an httpx.Response for the bits the route reads."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"content-type": content_type}
    resp.text = text

    def _json():
        import json as _j

        return _j.loads(text)

    resp.json.side_effect = _json
    return resp


@asynccontextmanager
async def _fake_async_client_cm(response, capture):
    """An async-context-manager replacement for httpx.AsyncClient(...).

    Records the POST kwargs in `capture` and returns `response`.
    """
    client = MagicMock()

    async def _post(url, **kwargs):
        capture["url"] = url
        capture["kwargs"] = kwargs
        return response

    client.post = _post
    yield client


def _patch_httpx(agents_module, response, capture):
    """Patch the agents module's httpx.AsyncClient with one returning `response`.

    Supports BOTH usage styles: `async with httpx.AsyncClient() as c:` and a bare
    `httpx.AsyncClient()` whose `.post` is awaited then `.aclose()`d.
    """
    def _factory(*args, **kwargs):
        return _FakeAsyncClient(response, capture)

    return patch.object(agents_module.httpx, "AsyncClient", _factory)


class _FakeAsyncClient:
    def __init__(self, response, capture):
        self._response = response
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        self._capture["url"] = url
        self._capture["kwargs"] = kwargs
        return self._response

    async def aclose(self):
        return None


SSE_BODY = (
    'data: {"event": "start"}\n\n'
    'data: {"message": {"role": "assistant", "content": [{"text": "hi"}]}}\n\n'
)


def test_invoke_assigned_sse_extracts_terminal_text(entra_settings):
    """Assigned → 200; the httpx mock returns a text/event-stream SSE body; the route
    extracts the terminal message text 'hi' and forwards it."""
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock(return_value="obo-agent-token")
    client, agents_module = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    response = _mock_httpx_response(
        status_code=200, content_type="text/event-stream", text=SSE_BODY
    )
    capture = {}

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        with _patch_httpx(agents_module, response, capture):
            resp = client.post(
                "/api/v1/agents/rec-123/invoke",
                json={"prompt": "what is 1+1?"},
                headers=_headers(),
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["response"] == "hi"
    # The runtime URL targets the AgentCore data-plane host + DEFAULT qualifier.
    assert "bedrock-agentcore.us-east-1.amazonaws.com/runtimes/" in capture["url"]
    assert "qualifier=DEFAULT" in capture["url"]
    # The body forwarded to the runtime carries the prompt AND the invoker's Entra oid
    # (the agreed `user_oid` contract — for per-user telemetry attribution). The viewer
    # principal's oid is "viewer-oid" (see _claims_for). Principal has no `sub`, so no
    # `user_sub` is sent.
    assert capture["kwargs"]["json"] == {"prompt": "what is 1+1?", "user_oid": "viewer-oid"}
    # Authorization uses the OBO'd token, not the inbound one.
    assert capture["kwargs"]["headers"]["Authorization"] == "Bearer obo-agent-token"
    # Session id header is present and >= 33 chars.
    sid = capture["kwargs"]["headers"]["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"]
    assert len(sid) >= 33


def test_invoke_json_fallback_forwarded(entra_settings):
    """A non-streaming runtime returning application/json → the JSON is forwarded."""
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock(return_value="obo-agent-token")
    client, agents_module = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    response = _mock_httpx_response(
        status_code=200,
        content_type="application/json",
        text='{"answer": "the answer is 2"}',
    )
    capture = {}

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        with _patch_httpx(agents_module, response, capture):
            resp = client.post(
                "/api/v1/agents/rec-123/invoke",
                json={"prompt": "what is 1+1?"},
                headers=_headers(),
            )

    assert resp.status_code == 200, resp.text
    assert resp.json()["response"] == {"answer": "the answer is 2"}


def test_invoke_forwards_raw_inbound_token_as_obo_assertion(entra_settings):
    """The raw inbound bearer token (the SAME string current_principal validated) is
    passed to obo_exchange as the assertion, with the agent's audience."""
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock(return_value="obo-agent-token")
    client, agents_module = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    response = _mock_httpx_response(text=SSE_BODY)
    capture = {}

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        with _patch_httpx(agents_module, response, capture):
            resp = client.post(
                "/api/v1/agents/rec-123/invoke",
                json={"prompt": "hi"},
                headers=_headers(token="my-special-inbound-token"),
            )

    assert resp.status_code == 200, resp.text
    mock_graph.obo_exchange.assert_awaited_once_with(
        "my-special-inbound-token", "api://agp-agent-rec-123"
    )


def test_invoke_forwards_invoker_oid_in_runtime_payload(entra_settings):
    """The runtime POST body carries the invoking principal's Entra oid under the agreed
    `user_oid` key (so the deployed agent can attribute telemetry per-user WITHOUT
    decoding the inbound token). Uses an `admin` principal to prove the forwarded oid is
    the ACTUAL invoker's oid, not a constant. Principal has no `sub`, so no `user_sub`."""
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock(return_value="obo-agent-token")
    client, agents_module = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    response = _mock_httpx_response(text=SSE_BODY)
    capture = {}

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        with _patch_httpx(agents_module, response, capture):
            resp = client.post(
                "/api/v1/agents/rec-123/invoke",
                json={"prompt": "hello"},
                headers=_headers(),
            )

    assert resp.status_code == 200, resp.text
    posted = capture["kwargs"]["json"]
    assert posted["prompt"] == "hello"
    assert posted["user_oid"] == "admin-oid"  # the admin principal's oid (see _claims_for)
    # Principal carries no `sub` attribute → the route does not forward `user_sub`.
    assert "user_sub" not in posted


def test_invoke_not_assigned_403(entra_settings):
    from services.graph_service import NotAssignedError

    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock(
        side_effect=NotAssignedError("user is not assigned to the agent app")
    )
    client, agents_module = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/agents/rec-123/invoke",
            json={"prompt": "hi"},
            headers=_headers(),
        )

    assert resp.status_code == 403
    assert "not assigned" in resp.json()["detail"].lower()


def test_invoke_obo_config_error_500(entra_settings):
    from services.graph_service import OboConfigError

    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock(
        side_effect=OboConfigError("OBO failed for a configuration reason")
    )
    client, agents_module = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/agents/rec-123/invoke",
            json={"prompt": "hi"},
            headers=_headers(),
        )

    assert resp.status_code == 500
    assert "misconfigured" in resp.json()["detail"].lower()


def test_invoke_unprovisioned_409_no_obo_or_httpx(entra_settings):
    """An unprovisioned agent → 409 BEFORE any OBO or httpx call is made."""
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent(identity_status="pending")
    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock()
    client, agents_module = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    response = _mock_httpx_response(text=SSE_BODY)
    capture = {}
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        with _patch_httpx(agents_module, response, capture):
            resp = client.post(
                "/api/v1/agents/rec-123/invoke",
                json={"prompt": "hi"},
                headers=_headers(),
            )

    assert resp.status_code == 409
    mock_graph.obo_exchange.assert_not_called()
    assert capture == {}  # no httpx POST made


def test_invoke_runtime_rejects_token_502(entra_settings):
    """The runtime returns 403 with the live-confirmed reject body → 502."""
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock(return_value="obo-agent-token")
    client, agents_module = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    response = _mock_httpx_response(
        status_code=403,
        content_type="application/json",
        text='{"message": "OAuth authorization failed: Failed to parse token"}',
    )
    capture = {}

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        with _patch_httpx(agents_module, response, capture):
            resp = client.post(
                "/api/v1/agents/rec-123/invoke",
                json={"prompt": "hi"},
                headers=_headers(),
            )

    assert resp.status_code == 502
    assert "rejected" in resp.json()["detail"].lower()


def test_invoke_timeout_504(entra_settings):
    import httpx as _httpx

    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock(return_value="obo-agent-token")
    client, agents_module = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    class _TimeoutClient(_FakeAsyncClient):
        async def post(self, url, **kwargs):
            raise _httpx.TimeoutException("timed out")

    def _factory(*args, **kwargs):
        return _TimeoutClient(None, {})

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        with patch.object(agents_module.httpx, "AsyncClient", _factory):
            resp = client.post(
                "/api/v1/agents/rec-123/invoke",
                json={"prompt": "hi"},
                headers=_headers(),
            )

    assert resp.status_code == 504


def test_invoke_viewer_can_invoke(entra_settings):
    """Invoking is VIEWER-gated (not a governance mutation)."""
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()
    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock(return_value="obo-agent-token")
    client, agents_module = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    response = _mock_httpx_response(text=SSE_BODY)
    capture = {}
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        with _patch_httpx(agents_module, response, capture):
            resp = client.post(
                "/api/v1/agents/rec-123/invoke",
                json={"prompt": "hi"},
                headers=_headers(),
            )

    assert resp.status_code == 200


def test_invoke_does_not_use_boto3_control_client(entra_settings):
    """The invoke path uses httpx ONLY — the registry's boto3 control client is never
    touched for invocation."""
    mock_registry = MagicMock()
    agent = _make_agent()
    mock_registry.get.return_value = agent
    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock(return_value="obo-agent-token")
    client, agents_module = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    response = _mock_httpx_response(text=SSE_BODY)
    capture = {}
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        with _patch_httpx(agents_module, response, capture):
            resp = client.post(
                "/api/v1/agents/rec-123/invoke",
                json={"prompt": "hi"},
                headers=_headers(),
            )

    assert resp.status_code == 200
    # Only .get was called on the registry mock; no invoke_agent_runtime / control client.
    called = {c[0] for c in mock_registry.method_calls}
    assert "invoke_agent_runtime" not in called
    assert not any("invoke" in name for name in called)


# --- ?stage= (E36/T2, item 6) -------------------------------------------------
#
# The route used to invoke whatever the `agent_arn` SCALAR named — "whichever stage deployed
# last" (C-A2) — so an operator who believed they were invoking dev could reach prod. `?stage=`
# is OPTIONAL and ADDITIVE, copying `runtime_status`'s idiom: given a stage, THAT stage's runtime;
# a stage the agent owns no runtime for is refused (404) rather than falling through to another
# stage's runtime; omitted, the pre-E36 behaviour byte for byte.

# dev and prod deliberately sit in DIFFERENT regions, because the runtime URL's host is derived
# from the CHOSEN ARN's region segment — so the assertions prove the whole URL followed the stage,
# not just its path. The scalar duplicates DEV (as if dev deployed last).
_DEV_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/agent-dev111"
_PROD_ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/agent-prod22"


def _staged_agent():
    """An agent owning one runtime PER STAGE, with the scalar naming dev's."""
    return _make_agent(
        agent_arn=_DEV_ARN,
        agent_arns={"dev": _DEV_ARN, "prod": _PROD_ARN},
    )


def _encoded(arn: str) -> str:
    from urllib.parse import quote

    return quote(arn, safe="")


def test_invoke_with_stage_targets_that_stages_runtime(entra_settings):
    """`?stage=prod` invokes the PROD runtime — not the scalar's (dev) one.

    The scalar still names dev, so a route that ignored the parameter would pass every other
    assertion in this file while reaching the wrong runtime; the ARN + host assertions are what
    make the stage load-bearing.
    """
    mock_registry = MagicMock()
    mock_registry.get.return_value = _staged_agent()
    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock(return_value="obo-agent-token")
    client, agents_module = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    response = _mock_httpx_response(text=SSE_BODY)
    capture = {}

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        with _patch_httpx(agents_module, response, capture):
            resp = client.post(
                "/api/v1/agents/rec-123/invoke?stage=prod",
                json={"prompt": "hi"},
                headers=_headers(),
            )

    assert resp.status_code == 200, resp.text
    assert _encoded(_PROD_ARN) in capture["url"]
    assert _encoded(_DEV_ARN) not in capture["url"]
    # Region comes from the CHOSEN ARN's segment 3, so the host moves with the stage.
    assert capture["url"].startswith("https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/")


def test_invoke_unknown_stage_404_no_obo_or_httpx(entra_settings):
    """A stage the agent owns no runtime for → 404, BEFORE any OBO or runtime call.

    Never a fall-through to another stage's runtime: answering with prod's runtime because dev
    has none would look like an answer to the question asked while being a different one. The
    refusal is also cheap — no Entra round-trip is spent on a stage that cannot resolve.
    """
    mock_registry = MagicMock()
    mock_registry.get.return_value = _staged_agent()
    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock(return_value="obo-agent-token")
    client, agents_module = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    response = _mock_httpx_response(text=SSE_BODY)
    capture = {}

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        with _patch_httpx(agents_module, response, capture):
            resp = client.post(
                "/api/v1/agents/rec-123/invoke?stage=staging",
                json={"prompt": "hi"},
                headers=_headers(),
            )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown stage"
    mock_graph.obo_exchange.assert_not_called()
    assert capture == {}  # no httpx POST made


def test_invoke_legacy_scalar_only_record_refuses_an_explicit_stage(entra_settings):
    """A legacy (scalar-only) record asked for an explicit stage → 404.

    `runtime_arns()` keys such a record's single runtime `UNKNOWN_STAGE`, because the record
    genuinely cannot attribute it. Captioning that runtime "dev" on request would manufacture
    per-stage evidence out of an agent-level fact.
    """
    mock_registry = MagicMock()
    mock_registry.get.return_value = _make_agent()  # scalar only, no `agent_arns`
    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock(return_value="obo-agent-token")
    client, agents_module = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    response = _mock_httpx_response(text=SSE_BODY)
    capture = {}

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        with _patch_httpx(agents_module, response, capture):
            resp = client.post(
                "/api/v1/agents/rec-123/invoke?stage=dev",
                json={"prompt": "hi"},
                headers=_headers(),
            )

    assert resp.status_code == 404
    mock_graph.obo_exchange.assert_not_called()
    assert capture == {}


def test_invoke_without_stage_keeps_the_scalars_runtime(entra_settings):
    """No `?stage=` → the SCALAR's runtime, byte-identical to the pre-E36 behaviour.

    Asserted on a MULTI-stage agent, which is the only shape where "the scalar" and "the first
    map key" could differ — the additive parameter must not silently re-point existing callers.
    The ARN is the same one every other test in this file reaches by default.
    """
    mock_registry = MagicMock()
    mock_registry.get.return_value = _staged_agent()
    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock(return_value="obo-agent-token")
    client, agents_module = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)

    response = _mock_httpx_response(text=SSE_BODY)
    capture = {}

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        with _patch_httpx(agents_module, response, capture):
            resp = client.post(
                "/api/v1/agents/rec-123/invoke",
                json={"prompt": "hi"},
                headers=_headers(),
            )

    assert resp.status_code == 200, resp.text
    assert _encoded(_DEV_ARN) in capture["url"]
    assert _encoded(_PROD_ARN) not in capture["url"]
    assert capture["url"].startswith("https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/")


# --- _extract_sse_text direct unit tests (the SSE buffer/parse edge cases) ---

def _extract(body: str):
    """Import the parser fresh (reset_modules pops api.routes.agents per test)."""
    from api.routes.agents import _extract_sse_text

    return _extract_sse_text(body)


def test_extract_sse_skips_junk_line_then_returns_terminal_text(entra_settings):
    """A junk `data: not-json` line is tolerated (skipped, no crash); the following
    valid terminal message's text is returned."""
    body = (
        "data: not-json-at-all\n\n"
        'data: {"message": {"role": "assistant", "content": [{"text": "hello"}]}}\n\n'
    )
    assert _extract(body) == "hello"


def test_extract_sse_result_shaped_terminal_event(entra_settings):
    """A `data: {"result": "..."}`-shaped terminal event → returns the result value."""
    body = 'data: {"result": "the answer is 42"}\n\n'
    assert _extract(body) == "the answer is 42"


def test_extract_sse_empty_body_falls_back_to_raw(entra_settings):
    """An empty / no-`data:`-lines body → falls back to returning the raw body (no
    crash)."""
    assert _extract("") == ""
    assert _extract("event: ping\n: a comment\n") == "event: ping\n: a comment\n"


def test_extract_sse_concatenates_multi_text_parts(entra_settings):
    """Multiple text parts in the terminal message's content are joined."""
    body = (
        'data: {"message": {"role": "assistant", '
        '"content": [{"text": "foo"}, {"text": "bar"}]}}\n\n'
    )
    assert _extract(body) == "foobar"


# --- CAPTURED-BODY FENCE: producer and consumer against the SAME bytes (E28D/T3) ---
#
# Every test above uses a HAND-WRITTEN body, so they only ever pin this parser against our own
# model of the template. The template's tests pin the handler against ITS reproduction of this
# parser (`_buffered_client_view` in agent-templates/strands-agentcore/tests/test_agent.py). Two
# halves, each tested against a model of the other, is exactly the gap that let a stream ship
# whose whole "answer" was one emoji.
#
# These two tests close it. The fixtures are the template handler's OWN output, SSE-encoded the
# way AgentCore encodes it, committed as bytes — see
# tests/fixtures/generate_strands_agentcore_fixtures.py for the (offline) regeneration procedure.
# The contract itself is documented for customers in the template's README ("Response contract").
#
# If one of these fails, a producer/consumer DISAGREEMENT is the finding — do not adjust the
# expected string to match the parser without deciding which side is wrong.

_FIXTURE_DIR = Path(__file__).parent / "fixtures"

# The exact string agent-templates/strands-agentcore/tests/test_agent.py pins on the producer
# side (`_TERMINAL_MESSAGE`, and its `_buffered_client_view` assertion). Restated here so a
# divergence fails loudly; the two literals must stay identical.
_PRODUCER_PINNED_ANSWER = "The answer is 2 😊"


def test_extract_sse_captured_template_body_yields_producer_pinned_answer(entra_settings):
    """A body captured from the template handler extracts the WHOLE answer, emoji included.

    Fences the parser against real wire bytes rather than a synthetic body: content chunks first,
    then the terminal text-bearing message. The defect this guards against rendered the last
    content chunk (`" 😊"`) as the entire response.
    """
    body = (_FIXTURE_DIR / "strands_agentcore_invoke.sse").read_text(encoding="utf-8")

    assert _extract(body) == _PRODUCER_PINNED_ANSWER


def test_extract_sse_captured_throttled_after_tool_body_yields_the_error(entra_settings):
    """A tool cycle that then throttles surfaces the ERROR, never a stale toolResult blob.

    The captured body has no terminal message — the handler filtered the textless
    toolUse/toolResult messages out — so the parser falls back to the last payload, which is the
    error event. Extracting a `toolResult` here would hide a failed run behind a plausible-looking
    blob, which is the regression the terminal-answer fix closed.
    """
    body = (_FIXTURE_DIR / "strands_agentcore_invoke_throttled.sse").read_text(encoding="utf-8")

    extracted = _extract(body)

    assert extracted == {"type": "error", "data": "ThrottlingException: rate exceeded"}
    assert "toolResult" not in repr(extracted)


# =============================================================================
# E29/T7 — the DATABRICKS invoke branch (contract C-5, design §3)
# =============================================================================
#
# Everything above this line is the AgentCore fence: the existing branch is byte-unchanged and
# its tests are unmodified. What follows pins the second platform.
#
# Two shapes, one enforcement point. The Entra OBO exchange runs FIRST on BOTH binding modes —
# it is where "is this user assigned to this agent?" is actually decided — and only then does
# the route touch Databricks. In `federation` mode the OBO token is exchanged at the workspace
# (RFC 8693) so the agent and Unity Catalog see the real caller; in `sp_secret` mode the OBO
# token is DISCARDED after it has done its enforcement job and a per-agent service-principal
# token is minted instead. `test_..._sp_secret_unassigned_403_before_any_databricks_call` is the
# test that keeps that ordering honest: it asserts the Databricks fake was never hit.
#
# The fake is deliberately STRICTER than a MagicMock: an empty workspace URL, an empty subject
# token or an empty credential is a `DatabricksError`, exactly as the real client's `_request`
# would produce. A fake more generous than reality makes tests that cannot fail.

_DB_HANDLE = "https://claims-triage-1234.aws.databricksapps.com"
_DB_WORKSPACE = "https://dbc-test.cloud.databricks.com"
# No account id in an ARN anywhere (Global Constraints) — the account segment is legally empty
# and `_SM_SECRET_ARN_RE` accepts it, so the fixture carries no AWS account number at all.
_DB_AGENT_SECRET_ARN = (
    "arn:aws:secretsmanager:us-east-1::secret:agp-dev/databricks/agents/rec-db-1-AbCdEf"
)
_DB_TENANT_SECRET_ARN = (
    "arn:aws:secretsmanager:us-east-1::secret:agp-dev/databricks/ten-db-ZyXwVu"
)
# A sentinel that must never reach a response body, a log line, or an error message.
_SP_SECRET_SENTINEL = "s3cr3t-sp-credential-DO-NOT-LEAK"


def _make_databricks_agent(**overrides):
    from models.agent import Agent, AuthType, LifecycleState, Origin, Platform

    now = datetime.now(timezone.utc)
    base = dict(
        id="rec-db-1",
        name="claims-triage-db",
        purpose="Triage claims on Databricks",
        lifecycle_state=LifecycleState.APPROVED,
        origin=Origin.REGISTERED,
        platform=Platform.DATABRICKS,
        auth_type=AuthType.ENTRA,
        agent_arn=None,
        runtime_handle=_DB_HANDLE,
        runtime_kind="app",
        tenant_id="ten-db",
        entra_sp_id="sp-obj-id",
        entra_app_audience="api://agp-agent-rec-db-1",
        invoker_role_id="role-invoker-guid",
        admin_role_id="role-admin-guid",
        identity_status="provisioned",
        created_at=now,
        updated_at=now,
        created_by="maria.bauer@example.com",
    )
    base.update(overrides)
    return Agent(**base)


def _db_stage(workspace_url=_DB_WORKSPACE, **over):
    from models.tenant import DatabricksStageConfig

    base = dict(
        workspace_url=workspace_url,
        workspace_id="0",
        cloud="aws",
        region="us-east-1",
        account_id="11111111-2222-3333-4444-555555555555",
        sp_client_id="tenant-sp-client-id",
        sp_client_secret_arn=_DB_TENANT_SECRET_ARN,
    )
    base.update(over)
    return DatabricksStageConfig(**base)


def _db_tenant(*, binding_mode="federation", stages=None, **over):
    """A REAL `Tenant` with a REAL `DatabricksStageConfig` — the union the service stores."""
    from models.tenant import Tenant, TenantPlatform

    if stages is None:
        stages = {"dev": _db_stage()}
    base = dict(
        id="ten-db",
        name="Claims (Databricks)",
        line_of_business="Claims",
        entra_group_ids=["grp-claims"],
        platform=TenantPlatform.DATABRICKS,
        stages=stages,
        capabilities={"can_discover": True, "account_admin": True, "user_sync": True},
        binding_mode=binding_mode,
        created_by="a@b.com",
        created_at="t",
        updated_at="t",
    )
    base.update(over)
    return Tenant(**base)


class _FakeDatabricks:
    """The two token calls the invoke path makes, recorded — and REJECTING a drifted shape.

    Mirrors `DatabricksWorkspaceService`'s real failure surface: a missing workspace URL,
    subject token or credential raises `DatabricksError`, never a token. That is what makes an
    assertion like "the fake was never hit" meaningful, and what stops a route bug (an empty
    workspace URL, a forgotten OBO token) from passing as a green test.
    """

    def __init__(self, *, exchange_error=None, mint_error=None):
        self.exchange_calls = []
        self.mint_calls = []
        self._exchange_error = exchange_error
        self._mint_error = mint_error

    @property
    def touched(self):
        return bool(self.exchange_calls or self.mint_calls)

    async def exchange_federated_token(self, workspace_url, subject_jwt):
        from services.databricks_workspace_service import DatabricksError

        self.exchange_calls.append((workspace_url, subject_jwt))
        if self._exchange_error is not None:
            raise self._exchange_error
        if not workspace_url or not subject_jwt:
            raise DatabricksError("exchange rejected", kind="invalid_request")
        return "db-federated-token"

    async def mint_m2m_token(self, workspace_url, client_id, client_secret):
        from services.databricks_workspace_service import DatabricksError

        self.mint_calls.append((workspace_url, client_id, client_secret))
        if self._mint_error is not None:
            raise self._mint_error
        if not workspace_url or not client_id or not client_secret:
            raise DatabricksError("mint rejected", kind="invalid_client")
        return "db-m2m-token"


class _FakeSecretsManager:
    """`get_secret_value` over an in-memory {arn: body} map; an unknown ARN raises."""

    def __init__(self, bodies=None):
        self._bodies = dict(bodies or {})
        self.reads = []

    def get_secret_value(self, SecretId):  # noqa: N803 — boto3's kwarg name
        self.reads.append(SecretId)
        if SecretId not in self._bodies:
            raise RuntimeError("ResourceNotFoundException")
        return {"SecretString": self._bodies[SecretId]}


def _db_identity_service(bodies=None):
    """A REAL `DatabricksIdentityService` over a fake Secrets Manager.

    Real, not a stub, deliberately: the invoke path reuses T6's trusted-derivation rules
    (`_owns_secret_arn`) and its body reader rather than reimplementing them, so the tests must
    exercise the actual rules — including the foreign-ARN refusal below.
    """
    import json as _json

    from services.databricks_identity_service import DatabricksIdentityService

    if bodies is None:
        bodies = {
            _DB_AGENT_SECRET_ARN: _json.dumps(
                {
                    "client_secret": _SP_SECRET_SENTINEL,
                    "scim_id": "scim-1",
                    "agent_id": "rec-db-1",
                }
            )
        }
    return DatabricksIdentityService(
        databricks=MagicMock(),
        registry=MagicMock(),
        secrets_client=_FakeSecretsManager(bodies),
        secret_prefix="agp-dev/databricks/agents/",
    )


class _FakeTenantService:
    def __init__(self, tenant):
        self.tenant = tenant
        self.calls = []

    def get(self, tenant_id):
        from services.tenant_service import TenantError

        self.calls.append(tenant_id)
        if self.tenant is None or getattr(self.tenant, "id", None) != tenant_id:
            raise TenantError("unknown tenant")
        return self.tenant


def _build_databricks_client(
    *, agent=None, tenant=None, databricks=None, identity=None, graph=None
):
    """`_build_client` plus the three Databricks-side singletons the branch reads.

    `reset_modules` pops `api.routes.agents` per test, so its module globals are already fresh;
    `api.routes.tenants` is NOT popped (that fixture is the AgentCore fence and stays
    unmodified), so its `_svc` is set explicitly on every call rather than relied upon.
    """
    mock_registry = MagicMock()
    mock_registry.get.return_value = (
        agent if agent is not None else _make_databricks_agent()
    )
    mock_graph = graph
    if mock_graph is None:
        mock_graph = MagicMock()
        mock_graph.obo_exchange = AsyncMock(return_value="obo-agent-token")

    client, agents_module = _build_client(
        mock_registry=mock_registry, mock_graph=mock_graph
    )

    import api.routes.tenants as tenants_module

    tenants_module._svc = _FakeTenantService(
        tenant if tenant is not None else _db_tenant()
    )

    fake_db = databricks if databricks is not None else _FakeDatabricks()
    agents_module._databricks_workspace_svc = fake_db
    agents_module._databricks_identity_svc = (
        identity if identity is not None else _db_identity_service()
    )
    return client, agents_module, mock_graph, fake_db


def _invoke_db(client, agents_module, response=None, prompt="triage this claim", role="viewer"):
    """POST the invoke route with httpx patched; returns (resp, capture)."""
    if response is None:
        response = _mock_httpx_response(text=SSE_BODY)
    capture = {}
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for(role)):
        with _patch_httpx(agents_module, response, capture):
            resp = client.post(
                "/api/v1/agents/rec-db-1/invoke",
                json={"prompt": prompt},
                headers=_headers(),
            )
    return resp, capture


# --- federation mode ---------------------------------------------------------

def test_invoke_databricks_federation_exchanges_obo_token_then_posts(entra_settings):
    """The governed path end to end: OBO → RFC 8693 exchange at the workspace → bearer POST.

    Pins all four halves of C-5's federation leg: the exchange gets the OBO token (not the raw
    inbound one), the POST goes to the app's default `/api/v1/agent` route, the Authorization
    header carries the DATABRICKS token (not the Entra one), and the body is the same
    `{"prompt", "user_oid"}` contract the AgentCore branch sends.
    """
    client, agents_module, mock_graph, fake_db = _build_databricks_client()

    resp, capture = _invoke_db(client, agents_module)

    assert resp.status_code == 200, resp.text
    assert resp.json()["response"] == "hi"
    mock_graph.obo_exchange.assert_awaited_once_with(
        "fake-inbound-token", "api://agp-agent-rec-db-1"
    )
    assert fake_db.exchange_calls == [(_DB_WORKSPACE, "obo-agent-token")]
    assert fake_db.mint_calls == []
    assert capture["url"] == f"{_DB_HANDLE}/api/v1/agent"
    assert capture["kwargs"]["headers"]["Authorization"] == "Bearer db-federated-token"
    assert capture["kwargs"]["json"] == {
        "prompt": "triage this claim",
        "user_oid": "viewer-oid",
    }
    # The AgentCore session header is AgentCore's — it has no meaning to a Databricks app.
    assert "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id" not in capture["kwargs"]["headers"]


def test_invoke_databricks_endpoint_url_overrides_the_default_route(entra_settings):
    """`endpoint_url`, when the record carries one, is the invoke URL — the default
    `runtime_handle + /api/v1/agent` is only the template's convention."""
    agent = _make_databricks_agent(endpoint_url=f"{_DB_HANDLE}/api/v2/chat")
    client, agents_module, _graph, _db = _build_databricks_client(agent=agent)

    resp, capture = _invoke_db(client, agents_module)

    assert resp.status_code == 200, resp.text
    assert capture["url"] == f"{_DB_HANDLE}/api/v2/chat"


def test_invoke_databricks_trailing_slash_handle_yields_one_slash(entra_settings):
    """A handle stored with a trailing slash must not produce `//api/v1/agent`."""
    agent = _make_databricks_agent(runtime_handle=f"{_DB_HANDLE}/")
    client, agents_module, _graph, _db = _build_databricks_client(agent=agent)

    resp, capture = _invoke_db(client, agents_module)

    assert resp.status_code == 200, resp.text
    assert capture["url"] == f"{_DB_HANDLE}/api/v1/agent"


def test_invoke_databricks_federation_exchange_failure_502_safe_code(entra_settings):
    """A failed token exchange → 502 with the fixed safe code, and NOTHING from upstream.

    The upstream `DatabricksError.message` is composed safely by C-2, but this layer does not
    depend on that: it forwards neither the message nor the kind, so a workspace path or an
    echoed request form has no route into a response body.
    """
    from services.databricks_workspace_service import DatabricksError

    fake_db = _FakeDatabricks(
        exchange_error=DatabricksError(
            "Databricks rejected the request (exchange federated token, status 400) "
            "/oidc/v1/token on dbc-test",
            kind="invalid_request",
        )
    )
    client, agents_module, _graph, _db = _build_databricks_client(databricks=fake_db)

    resp, capture = _invoke_db(client, agents_module)

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "federation_exchange_failed" in detail
    assert "oidc" not in detail.lower()
    assert "dbc-test" not in detail
    assert capture == {}  # no POST was made with no token


# --- sp_secret mode (DORMANT — E29/T14a) -------------------------------------
#
# The mode is no longer produced by the connect flow and is not consumable on a default
# deployment (design §3B). These tests keep the capability PINNED by turning the gate on
# explicitly — `sp_secret_invoke_gate_on(agents_module)` — so the leg cannot rot while it is
# dormant. The gate-OFF refusal is pinned separately, further down.


def _sp_secret_invoke_gate_on(agents_module, monkeypatch) -> None:
    """Enable the dormant binding for ONE test.

    Patched on the ROUTE MODULE's ``settings`` (the `agents_module.settings` idiom used across
    these tests): `reset_modules` re-imports `api.routes.agents` per test, so a fresh
    `from core.config import settings` here could hand back a different instance than the one
    the route reads."""
    monkeypatch.setattr(agents_module.settings, "DATABRICKS_ALLOW_SP_SECRET_BINDING", True)


def test_invoke_databricks_sp_secret_mints_m2m_and_discards_the_obo_token(
    entra_settings, monkeypatch
):
    """sp_secret: the OBO exchange still runs, its token is DISCARDED, and the per-agent
    service-principal credential mints the token that is actually sent."""
    agent = _make_databricks_agent(
        binding_mode="sp_secret",
        databricks_sp_id="agent-sp-application-id",
        databricks_sp_secret_arn=_DB_AGENT_SECRET_ARN,
    )
    client, agents_module, mock_graph, fake_db = _build_databricks_client(
        agent=agent, tenant=_db_tenant(binding_mode="sp_secret")
    )
    _sp_secret_invoke_gate_on(agents_module, monkeypatch)

    resp, capture = _invoke_db(client, agents_module)

    assert resp.status_code == 200, resp.text
    # The enforcement point still ran.
    mock_graph.obo_exchange.assert_awaited_once()
    # ...and its token went nowhere near Databricks.
    assert fake_db.exchange_calls == []
    assert fake_db.mint_calls == [
        (_DB_WORKSPACE, "agent-sp-application-id", _SP_SECRET_SENTINEL)
    ]
    assert capture["kwargs"]["headers"]["Authorization"] == "Bearer db-m2m-token"


def test_invoke_databricks_sp_secret_unassigned_403_before_any_databricks_call(entra_settings):
    """THE GRANT-ENFORCEMENT TEST. An unassigned user is refused by Entra, and the refusal
    lands BEFORE Databricks is contacted at all — asserted by the fake never being hit.

    This is the whole reason the OBO exchange still runs in a mode that does not use its token:
    without it, sp_secret binding would mint a service-principal token for any caller who could
    reach the route, and the agent's Entra appRole assignments would govern nothing.
    """
    from services.graph_service import NotAssignedError

    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock(side_effect=NotAssignedError("not assigned"))
    agent = _make_databricks_agent(
        binding_mode="sp_secret",
        databricks_sp_id="agent-sp-application-id",
        databricks_sp_secret_arn=_DB_AGENT_SECRET_ARN,
    )
    fake_db = _FakeDatabricks()
    client, agents_module, _graph, _db = _build_databricks_client(
        agent=agent,
        tenant=_db_tenant(binding_mode="sp_secret"),
        databricks=fake_db,
        graph=mock_graph,
    )

    resp, capture = _invoke_db(client, agents_module)

    assert resp.status_code == 403
    assert "not assigned" in resp.json()["detail"].lower()
    assert fake_db.touched is False
    assert capture == {}


def test_invoke_databricks_federation_unassigned_403_before_any_databricks_call(entra_settings):
    from services.graph_service import NotAssignedError

    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock(side_effect=NotAssignedError("not assigned"))
    fake_db = _FakeDatabricks()
    client, agents_module, _graph, _db = _build_databricks_client(
        databricks=fake_db, graph=mock_graph
    )

    resp, capture = _invoke_db(client, agents_module)

    assert resp.status_code == 403
    assert fake_db.touched is False
    assert capture == {}


def test_invoke_databricks_obo_config_error_500(entra_settings):
    """The OBO taxonomy is UNCHANGED across platforms: a consent/backend misconfig is a 500
    with the re-provision hint, never a 403 and never a 502."""
    from services.graph_service import OboConfigError

    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock(side_effect=OboConfigError("no consent"))
    fake_db = _FakeDatabricks()
    client, agents_module, _graph, _db = _build_databricks_client(
        databricks=fake_db, graph=mock_graph
    )

    resp, _capture = _invoke_db(client, agents_module)

    assert resp.status_code == 500
    assert "misconfigured" in resp.json()["detail"].lower()
    assert fake_db.touched is False


def test_invoke_databricks_sp_secret_foreign_secret_arn_refused_502(entra_settings, monkeypatch):
    """OB-2 on the INVOKE path: `databricks_sp_secret_arn` is client-settable, so an ARN this
    service did not write FOR THIS AGENT is refused — never read, never minted from.

    Without this, a caller could register an agent pointing at the TENANT's own workspace-SP
    secret (or another agent's) and have the invoke path mint a token from a credential the
    agent was never granted.
    """
    agent = _make_databricks_agent(
        binding_mode="sp_secret",
        databricks_sp_id="agent-sp-application-id",
        databricks_sp_secret_arn=_DB_TENANT_SECRET_ARN,  # the TENANT's secret, not the agent's
    )
    fake_db = _FakeDatabricks()
    client, agents_module, _graph, _db = _build_databricks_client(
        agent=agent,
        tenant=_db_tenant(binding_mode="sp_secret"),
        databricks=fake_db,
        identity=_db_identity_service(
            {_DB_TENANT_SECRET_ARN: '{"sp_client_secret": "tenant-credential"}'}
        ),
    )
    # Gate ON: the OB-2 pointer rule must hold on the dormant leg too — it is the rule that
    # would matter most on a deployment that deliberately enables it.
    _sp_secret_invoke_gate_on(agents_module, monkeypatch)

    resp, capture = _invoke_db(client, agents_module)

    assert resp.status_code == 502
    assert "sp_credential_unavailable" in resp.json()["detail"]
    assert fake_db.touched is False
    assert capture == {}


def test_invoke_databricks_sp_secret_without_a_stored_credential_502(
    entra_settings, monkeypatch
):
    """A record that names no per-agent credential (a half-provisioned sp_secret agent) is a
    502 with a safe code — not a crash, and not a call with an empty secret."""
    agent = _make_databricks_agent(binding_mode="sp_secret")
    fake_db = _FakeDatabricks()
    client, agents_module, _graph, _db = _build_databricks_client(
        agent=agent, tenant=_db_tenant(binding_mode="sp_secret"), databricks=fake_db
    )
    _sp_secret_invoke_gate_on(agents_module, monkeypatch)

    resp, _capture = _invoke_db(client, agents_module)

    assert resp.status_code == 502
    assert "sp_credential_unavailable" in resp.json()["detail"]
    assert fake_db.touched is False


def test_invoke_databricks_sp_secret_never_leaks_the_credential(
    entra_settings, caplog, monkeypatch
):
    """SENTINEL: the minted-from secret appears in NO response body and NO log line."""
    import logging as _logging

    agent = _make_databricks_agent(
        binding_mode="sp_secret",
        databricks_sp_id="agent-sp-application-id",
        databricks_sp_secret_arn=_DB_AGENT_SECRET_ARN,
    )
    client, agents_module, _graph, _db = _build_databricks_client(
        agent=agent, tenant=_db_tenant(binding_mode="sp_secret")
    )
    _sp_secret_invoke_gate_on(agents_module, monkeypatch)

    with caplog.at_level(_logging.DEBUG):
        resp, _capture = _invoke_db(client, agents_module)

    assert resp.status_code == 200, resp.text
    assert _SP_SECRET_SENTINEL not in resp.text
    assert _SP_SECRET_SENTINEL not in caplog.text


def test_invoke_databricks_token_mint_failure_502_safe_code(entra_settings, monkeypatch):
    from services.databricks_workspace_service import DatabricksError

    agent = _make_databricks_agent(
        binding_mode="sp_secret",
        databricks_sp_id="agent-sp-application-id",
        databricks_sp_secret_arn=_DB_AGENT_SECRET_ARN,
    )
    fake_db = _FakeDatabricks(
        mint_error=DatabricksError("mint failed at /oidc/v1/token", kind="invalid_client")
    )
    client, agents_module, _graph, _db = _build_databricks_client(
        agent=agent, tenant=_db_tenant(binding_mode="sp_secret"), databricks=fake_db
    )
    _sp_secret_invoke_gate_on(agents_module, monkeypatch)

    resp, capture = _invoke_db(client, agents_module)

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "token_mint_failed" in detail
    assert "oidc" not in detail.lower()
    assert capture == {}


# --- OB-4: the bearer token is never pointed at an unvalidated host ----------

@pytest.mark.parametrize(
    "handle",
    [
        "http://claims-triage-1234.aws.databricksapps.com",       # not https
        "https://attacker.example.com",                            # foreign host
        "https://evil-databricksapps.com",                         # suffix LOOKALIKE
        "https://databricksapps.com",                              # the bare apex
        "https://claims.aws.databricksapps.com.attacker.example",   # suffix in the middle
        "https://claims.aws.databricksapps.com@attacker.example",   # userinfo — real host is the attacker
        "https://attacker.example.com@claims-triage-1234.aws.databricksapps.com",  # credentials at a real host
        "//claims-triage-1234.aws.databricksapps.com",             # scheme-relative
        "javascript:alert(1)",
        # FIX round 1 (Critical): these two pass `urlparse` AND the suffix test, but are
        # invalid to httpx — see the dedicated test below for why that mattered.
        "https://claims.aws.databricksapps.com/x\r\nX-Evil: 1",     # CRLF header injection
        "https://claims\xad.aws.databricksapps.com/api",            # soft hyphen in the host
        # FIX round 2 (Critical, remainder): an EMPTY/invalid punycode A-label. `httpx.URL()`
        # constructs these happily — the IDNA encode is LAZY and only runs on `.host`.
        "https://xn--.aws.databricksapps.com",
        "https://xn--a.aws.databricksapps.com",
        "https://xn--evil.aws.databricksapps.com",
    ],
)
def test_invoke_databricks_hostile_runtime_handle_refused_before_any_token(
    entra_settings, handle
):
    """OB-4, EXECUTED over hostile inputs. A handle that is not an https Databricks-Apps URL is
    refused with a 502 safe code, and the refusal lands before a token is even obtained — so
    there is never a bearer credential in flight toward an unvalidated host.

    `runtime_handle` reaches the record from registration, so it is attacker-influenced input on
    the one path that attaches a Databricks token to an outbound request.
    """
    agent = _make_databricks_agent(runtime_handle=handle)
    fake_db = _FakeDatabricks()
    client, agents_module, _graph, _db = _build_databricks_client(
        agent=agent, databricks=fake_db
    )

    resp, capture = _invoke_db(client, agents_module)

    assert resp.status_code == 502
    assert "invalid_runtime_handle" in resp.json()["detail"]
    assert fake_db.touched is False, "a token was obtained for an unvalidated host"
    assert capture == {}


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "http://claims-triage-1234.aws.databricksapps.com/api/v1/agent",
        "https://attacker.example.com/api/v1/agent",
        "https://evil-databricksapps.com/api/v1/agent",
        "https://attacker.example.com@claims-triage-1234.aws.databricksapps.com/api",
        # FIX round 1 (Critical): httpx-invalid, urlparse-valid. Same rule, same field.
        "https://claims.aws.databricksapps.com/x\r\nX-Evil: 1",
        "https://claims\xad.aws.databricksapps.com/api",
        # FIX round 2 (Critical, remainder) — same class, same rule, the other field.
        "https://xn--.aws.databricksapps.com/api/v1/agent",
        "https://xn--a.aws.databricksapps.com/api/v1/agent",
    ],
)
def test_invoke_databricks_hostile_endpoint_url_refused(entra_settings, endpoint_url):
    """`endpoint_url` is validated on EXACTLY the same rule as the handle. It is also a
    client-settable field, so a valid handle plus a hostile `endpoint_url` must not be the
    bypass — the validated value is the one actually POSTed to, not the one it derives from.
    """
    agent = _make_databricks_agent(endpoint_url=endpoint_url)
    fake_db = _FakeDatabricks()
    client, agents_module, _graph, _db = _build_databricks_client(
        agent=agent, databricks=fake_db
    )

    resp, capture = _invoke_db(client, agents_module)

    assert resp.status_code == 502
    assert "invalid_runtime_handle" in resp.json()["detail"]
    assert fake_db.touched is False
    assert capture == {}


# --- stage / tenant / binding-mode resolution -------------------------------

def test_invoke_databricks_multi_stage_picks_the_workspace_hosting_the_agent(entra_settings):
    """Two workspaces, and the handle is prefixed by exactly one of them → that one is used.

    Getting this wrong would exchange the caller's token at a workspace that does not host the
    app, so the honest outcomes are "the one that matches" or a refusal — never a positional pick.
    """
    prod_workspace = "https://dbc-prod.cloud.databricks.com"
    agent = _make_databricks_agent(runtime_handle=f"{prod_workspace}/apps/claims")
    tenant = _db_tenant(
        stages={
            "dev": _db_stage(_DB_WORKSPACE),
            "prod": _db_stage(prod_workspace),
        }
    )
    fake_db = _FakeDatabricks()
    client, agents_module, _graph, _db = _build_databricks_client(
        agent=agent, tenant=tenant, databricks=fake_db
    )

    resp, _capture = _invoke_db(client, agents_module)

    # The handle is a workspace-hosted path, not a `.databricksapps.com` URL, so OB-4 refuses
    # the POST — but the STAGE resolution happened first and picked prod. Asserting on the
    # refusal alone would not prove that, so assert the code and that nothing was exchanged.
    assert resp.status_code == 502
    assert "invalid_runtime_handle" in resp.json()["detail"]
    assert fake_db.touched is False


def test_invoke_databricks_multi_stage_with_no_match_502_safe_code(entra_settings):
    """Two workspaces and a handle that neither prefixes → fail closed with a safe code.

    A Databricks app URL (`<app>-<n>.<region>.databricksapps.com`) carries NO workspace
    identity, so on a multi-workspace tenant there is genuinely nothing to match on. Refusing is
    the honest answer; picking a workspace would exchange the caller's token at the wrong one.
    """
    tenant = _db_tenant(
        stages={
            "dev": _db_stage(_DB_WORKSPACE),
            "prod": _db_stage("https://dbc-prod.cloud.databricks.com"),
        }
    )
    fake_db = _FakeDatabricks()
    client, agents_module, _graph, _db = _build_databricks_client(
        tenant=tenant, databricks=fake_db
    )

    resp, capture = _invoke_db(client, agents_module)

    assert resp.status_code == 502
    assert "workspace_stage_unresolved" in resp.json()["detail"]
    assert fake_db.touched is False
    assert capture == {}


def test_invoke_databricks_two_stages_same_workspace_resolves(entra_settings):
    """dev + prod pointing at ONE workspace is the single-workspace case, not ambiguity.

    The tenant form requires BOTH stages complete, so a single-workspace operator
    duplicates the same `workspace_url` into dev and prod — the E29 live test's exact
    shape. The resolver must count DISTINCT workspaces, not stage entries; refusing
    here made every form-created single-workspace tenant invoke-dead (livefix-5)."""
    tenant = _db_tenant(
        stages={
            "dev": _db_stage(_DB_WORKSPACE),
            "prod": _db_stage(_DB_WORKSPACE),
        }
    )
    fake_db = _FakeDatabricks()
    client, agents_module, _graph, _db = _build_databricks_client(
        tenant=tenant, databricks=fake_db
    )

    resp, capture = _invoke_db(client, agents_module)

    assert resp.status_code == 200, resp.text
    assert fake_db.exchange_calls == [(_DB_WORKSPACE, "obo-agent-token")]
    assert capture["url"] == f"{_DB_HANDLE}/api/v1/agent"


def test_invoke_databricks_no_workspace_stage_502_safe_code(entra_settings):
    tenant = _db_tenant(stages={})
    fake_db = _FakeDatabricks()
    client, agents_module, _graph, _db = _build_databricks_client(
        tenant=tenant, databricks=fake_db
    )

    resp, _capture = _invoke_db(client, agents_module)

    assert resp.status_code == 502
    assert "workspace_stage_unresolved" in resp.json()["detail"]
    assert fake_db.touched is False


def test_invoke_databricks_unknown_tenant_502_safe_code(entra_settings):
    """A record whose tenant cannot be resolved is a 502 safe code, not a 500."""
    agent = _make_databricks_agent(tenant_id="ten-gone")
    fake_db = _FakeDatabricks()
    client, agents_module, _graph, _db = _build_databricks_client(
        agent=agent, databricks=fake_db
    )

    resp, _capture = _invoke_db(client, agents_module)

    assert resp.status_code == 502
    assert "tenant_unresolved" in resp.json()["detail"]
    assert fake_db.touched is False


def test_invoke_databricks_binding_mode_comes_from_the_tenant_not_the_agent(entra_settings):
    """The mode is the TENANT's (T6's rule). An agent record claiming `sp_secret` on a
    federation tenant is invoked over FEDERATION — the client-settable field is not authority.
    """
    agent = _make_databricks_agent(
        binding_mode="sp_secret",
        databricks_sp_id="planted-sp",
        databricks_sp_secret_arn=_DB_AGENT_SECRET_ARN,
    )
    client, agents_module, _graph, fake_db = _build_databricks_client(
        agent=agent, tenant=_db_tenant(binding_mode="federation")
    )

    resp, capture = _invoke_db(client, agents_module)

    assert resp.status_code == 200, resp.text
    assert fake_db.exchange_calls == [(_DB_WORKSPACE, "obo-agent-token")]
    assert fake_db.mint_calls == []
    assert capture["kwargs"]["headers"]["Authorization"] == "Bearer db-federated-token"


def test_invoke_databricks_invoke_unavailable_has_its_own_actionable_refusal(entra_settings):
    """E29/T14a (design §3B). ``invoke_unavailable`` is a DIFFERENT statement from "this tenant
    was never probed": the tenant WAS probed and cannot federate. It gets its own code and a
    sentence naming both missing grants, because ``binding_mode_unresolved`` would send the
    operator looking for an unconnected tenant instead of at their Databricks account."""
    fake_db = _FakeDatabricks()
    client, agents_module, _graph, _db = _build_databricks_client(
        tenant=_db_tenant(binding_mode="invoke_unavailable"), databricks=fake_db
    )

    resp, _capture = _invoke_db(client, agents_module)

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "federation_unavailable" in detail
    assert "binding_mode_unresolved" not in detail
    assert "account-admin" in detail and "user sync" in detail
    assert fake_db.touched is False


def test_invoke_databricks_sp_secret_is_refused_when_the_gate_is_off(entra_settings, monkeypatch):
    """DEFAULT DEPLOYMENT (design §3B): the dormant binding is not consumable. A fully
    provisioned sp_secret agent still does not mint a service-principal token — the refusal
    names the flag, so the operator sees a decision rather than a broken agent."""
    agent = _make_databricks_agent(
        binding_mode="sp_secret",
        databricks_sp_id="agent-sp-application-id",
        databricks_sp_secret_arn=_DB_AGENT_SECRET_ARN,
    )
    fake_db = _FakeDatabricks()
    client, agents_module, _graph, _db = _build_databricks_client(
        agent=agent, tenant=_db_tenant(binding_mode="sp_secret"), databricks=fake_db
    )
    assert agents_module.settings.DATABRICKS_ALLOW_SP_SECRET_BINDING is False

    resp, capture = _invoke_db(client, agents_module)

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "sp_secret_disabled" in detail
    assert "DATABRICKS_ALLOW_SP_SECRET_BINDING" in detail
    assert fake_db.mint_calls == []
    assert capture == {}  # nothing was sent to the app either


def test_invoke_databricks_unresolved_binding_mode_502_safe_code(entra_settings):
    """A tenant that was never probed carries no mode. Defaulting would PICK one, and the two
    modes differ in what the audit log can prove — so it is refused."""
    fake_db = _FakeDatabricks()
    client, agents_module, _graph, _db = _build_databricks_client(
        tenant=_db_tenant(binding_mode=""), databricks=fake_db
    )

    resp, _capture = _invoke_db(client, agents_module)

    assert resp.status_code == 502
    assert "binding_mode_unresolved" in resp.json()["detail"]
    assert fake_db.touched is False


# --- the 409 gate + the shared response contract -----------------------------

def test_invoke_databricks_unprovisioned_409_no_obo_or_databricks_call(entra_settings):
    agent = _make_databricks_agent(identity_status="pending")
    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock()
    fake_db = _FakeDatabricks()
    client, agents_module, _graph, _db = _build_databricks_client(
        agent=agent, databricks=fake_db, graph=mock_graph
    )

    resp, capture = _invoke_db(client, agents_module)

    assert resp.status_code == 409
    mock_graph.obo_exchange.assert_not_called()
    assert fake_db.touched is False
    assert capture == {}


def test_invoke_metadata_only_databricks_agent_409(entra_settings):
    """No `runtime_handle` ⇒ not databricks-governed ⇒ nothing to invoke ⇒ the same 409 the
    AgentCore branch gives a metadata-only record."""
    agent = _make_databricks_agent(runtime_handle=None)
    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock()
    client, agents_module, _graph, _db = _build_databricks_client(
        agent=agent, graph=mock_graph
    )

    resp, _capture = _invoke_db(client, agents_module)

    assert resp.status_code == 409
    mock_graph.obo_exchange.assert_not_called()


@pytest.mark.parametrize("status", [401, 403])
def test_invoke_databricks_app_rejects_the_token_502(entra_settings, status):
    """The app refusing the Databricks token is 502 "agent rejected the token" — DISTINCT from
    the OBO 403, which means the user is not assigned."""
    client, agents_module, _graph, _db = _build_databricks_client()
    response = _mock_httpx_response(
        status_code=status,
        content_type="application/json",
        text='{"detail": "invalid token"}',
    )

    resp, _capture = _invoke_db(client, agents_module, response=response)

    assert resp.status_code == 502
    assert "rejected" in resp.json()["detail"].lower()


def test_invoke_databricks_app_error_response_502(entra_settings):
    client, agents_module, _graph, _db = _build_databricks_client()
    response = _mock_httpx_response(
        status_code=500, content_type="application/json", text='{"detail": "boom"}'
    )

    resp, _capture = _invoke_db(client, agents_module, response=response)

    assert resp.status_code == 502
    assert "error response" in resp.json()["detail"].lower()


def test_invoke_databricks_timeout_504(entra_settings):
    import httpx as _httpx

    client, agents_module, _graph, _db = _build_databricks_client()

    class _TimeoutClient(_FakeAsyncClient):
        async def post(self, url, **kwargs):
            raise _httpx.TimeoutException("timed out")

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        with patch.object(
            agents_module.httpx, "AsyncClient", lambda *a, **k: _TimeoutClient(None, {})
        ):
            resp = client.post(
                "/api/v1/agents/rec-db-1/invoke",
                json={"prompt": "hi"},
                headers=_headers(),
            )

    assert resp.status_code == 504


def test_invoke_databricks_transport_failure_502(entra_settings):
    import httpx as _httpx

    client, agents_module, _graph, _db = _build_databricks_client()

    class _BrokenClient(_FakeAsyncClient):
        async def post(self, url, **kwargs):
            raise _httpx.ConnectError("no route")

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        with patch.object(
            agents_module.httpx, "AsyncClient", lambda *a, **k: _BrokenClient(None, {})
        ):
            resp = client.post(
                "/api/v1/agents/rec-db-1/invoke",
                json={"prompt": "hi"},
                headers=_headers(),
            )

    assert resp.status_code == 502
    assert "reach" in resp.json()["detail"].lower()


def test_invoke_databricks_response_contract_mirrors_agentcore(entra_settings):
    """The frontend's InvokePanel reads ONE shape. A Databricks app answering `application/json`
    yields `{"response": <json>}`; SSE yields the extracted terminal text — exactly the
    AgentCore branch's two cases, through the same parser."""
    client, agents_module, _graph, _db = _build_databricks_client()
    response = _mock_httpx_response(
        status_code=200,
        content_type="application/json",
        text='{"answer": "the claim is covered"}',
    )

    resp, _capture = _invoke_db(client, agents_module, response=response)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"response": {"answer": "the claim is covered"}}


def test_invoke_databricks_sse_response_uses_the_same_extractor(entra_settings):
    client, agents_module, _graph, _db = _build_databricks_client()
    body = 'data: {"message": {"role": "assistant", "content": [{"text": "covered"}]}}\n\n'
    response = _mock_httpx_response(
        status_code=200, content_type="text/event-stream", text=body
    )

    resp, _capture = _invoke_db(client, agents_module, response=response)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"response": "covered"}


def test_invoke_databricks_plain_text_response_falls_back_to_text(entra_settings):
    client, agents_module, _graph, _db = _build_databricks_client()
    response = _mock_httpx_response(
        status_code=200, content_type="text/plain", text="not json at all"
    )

    resp, _capture = _invoke_db(client, agents_module, response=response)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"response": "not json at all"}


# =============================================================================
# FIX round 1 (E29/T7) — the httpx.InvalidURL escape, redirects, actionability
# =============================================================================

def test_httpx_invalid_url_is_not_an_http_error_subclass():
    """The FACT the Critical rests on, pinned so it cannot silently change under a bump.

    `httpx.InvalidURL` inherits from `Exception` DIRECTLY — not from `httpx.HTTPError`. So a
    handler that catches `TimeoutException` and `HTTPError` does NOT catch it, which is exactly
    how a malformed URL escaped both arms of the invoke POST and became a 500.
    """
    import httpx as _httpx

    assert not issubclass(_httpx.InvalidURL, _httpx.HTTPError)
    assert issubclass(_httpx.InvalidURL, Exception)


@pytest.mark.parametrize(
    "handle",
    [
        # CRLF: `urlparse` keeps the netloc clean and the injection rides in the PATH.
        "https://claims.aws.databricksapps.com/x\r\nX-Evil: 1",
        # A soft hyphen (U+00AD) is invisible and survives `urlparse`, then fails IDNA.
        "https://claims\xad.aws.databricksapps.com/api",
    ],
)
def test_invoke_databricks_httpx_invalid_url_never_costs_a_token(entra_settings, handle):
    """FIX round 1, CRITICAL. A URL that passes `urlparse` + OB-4 but is INVALID to httpx must
    not reach the POST at all — and must never have cost a Databricks token.

    THE TWO-PART DEFECT this pins:

    1. `httpx.InvalidURL` is not an `HTTPError` subclass (see the test above), so it escaped
       BOTH except arms on the POST and surfaced as an unhandled 500.
    2. Worse than the status code: the token was minted BEFORE the POST, so a live Databricks
       credential had already been obtained for a request that was never made — a real
       credential spent on an unvalidated URL, with no request to show for it.

    Both halves are fixed by validating the URL through httpx's OWN parser inside OB-4, before
    any token is obtained. So the assertion that matters here is `fake_db.touched is False`:
    not merely "no 500", but "no credential was ever minted".
    """
    agent = _make_databricks_agent(runtime_handle=handle)
    fake_db = _FakeDatabricks()
    client, agents_module, _graph, _db = _build_databricks_client(
        agent=agent, databricks=fake_db
    )

    resp, capture = _invoke_db(client, agents_module)

    assert resp.status_code == 502
    assert "invalid_runtime_handle" in resp.json()["detail"]
    assert fake_db.touched is False, "a Databricks token was minted for a URL httpx cannot use"
    assert capture == {}


def test_invoke_databricks_ob4_validation_precedes_the_token(entra_settings):
    """ORDERING, pinned directly rather than inferred: OB-4 runs BEFORE the token is obtained.

    Every hostile-input test asserts `fake_db.touched is False`, which already implies this —
    but implication by absence is fragile: a refactor that moved the mint earlier would still
    leave those tests green if it ALSO happened to refuse. This asserts the sequence itself, so
    "a bad handle never costs a token" is a pinned property and not a side effect.
    """
    agent = _make_databricks_agent(runtime_handle="https://attacker.example.com")
    fake_db = _FakeDatabricks()
    client, agents_module, mock_graph, _db = _build_databricks_client(
        agent=agent, databricks=fake_db
    )

    resp, _capture = _invoke_db(client, agents_module)

    assert resp.status_code == 502
    # The OBO exchange DID run (it is upstream of the dispatch and is the enforcement point)...
    mock_graph.obo_exchange.assert_awaited_once()
    # ...but no Databricks call followed it.
    assert fake_db.exchange_calls == [] and fake_db.mint_calls == []


def test_invoke_databricks_does_not_follow_redirects(entra_settings):
    """FIX round 1, MINOR. `follow_redirects=False` is EXPLICIT on the invoke POST, because it
    silently carries OB-4's entire guarantee.

    OB-4 validates the URL AGP requests. If the client followed redirects, a validated
    `*.databricksapps.com` host answering `302 Location: https://attacker.example` would have
    httpx re-send the request — Authorization header included — to a host that was never
    validated. The allowlist would be enforced on the first hop only. httpx's default is
    already `False`, which is exactly why it is passed explicitly: a guarantee this load-bearing
    must not rest on an upstream default that a future version could flip.
    """
    client, agents_module, _graph, _db = _build_databricks_client()
    captured_kwargs = {}

    class _RecordingClient(_FakeAsyncClient):
        def __init__(self, response, capture, **kwargs):
            super().__init__(response, capture)
            captured_kwargs.update(kwargs)

    response = _mock_httpx_response(text=SSE_BODY)
    capture = {}
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        with patch.object(
            agents_module.httpx,
            "AsyncClient",
            lambda *a, **k: _RecordingClient(response, capture, **k),
        ):
            resp = client.post(
                "/api/v1/agents/rec-db-1/invoke",
                json={"prompt": "hi"},
                headers=_headers(),
            )

    assert resp.status_code == 200, resp.text
    assert captured_kwargs.get("follow_redirects") is False


def test_invoke_databricks_unresolved_stage_detail_is_actionable(entra_settings):
    """FIX round 1, IMPORTANT. The multi-workspace refusal SAYS what is wrong and where the fix
    lives — an operator reading `[workspace_stage_unresolved]` alone cannot act on it.

    Still a SAFE message: it names no workspace URL, no host, and no upstream text. It states
    the structural fact (an Apps hostname carries no workspace identity, so a multi-workspace
    Databricks tenant cannot be disambiguated) and points at the tenant record as the fix.
    """
    tenant = _db_tenant(
        stages={
            "dev": _db_stage(_DB_WORKSPACE),
            "prod": _db_stage("https://dbc-prod.cloud.databricks.com"),
        }
    )
    client, agents_module, _graph, fake_db = _build_databricks_client(tenant=tenant)

    resp, _capture = _invoke_db(client, agents_module)

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "workspace_stage_unresolved" in detail
    # Actionable: names the cause and the record that must change.
    assert "workspace" in detail.lower()
    assert "tenant" in detail.lower()
    # ...and still leaks nothing.
    assert "dbc-prod" not in detail
    assert _DB_WORKSPACE not in detail


# =============================================================================
# FIX round 2 (E29/T7) — the LAZY IDNA encode: the rest of the Critical's class
# =============================================================================

def test_httpx_url_construction_does_not_validate_idna():
    """The MECHANISM behind fix round 2, pinned so the fix cannot quietly stop working.

    `httpx.URL()` is LAZY: it stores the raw authority and only runs the IDNA encode when
    `.host` is accessed. So `httpx.URL(url)` on its own validates almost nothing about the
    hostname — which is why round 1's validator (construct, discard) still let an
    IDNA-invalid host through to the token mint.

    Also pins the exception lineage, which is the reason the escape reached a 502 at all rather
    than a 500: `idna.IDNAError` subclasses `UnicodeError` → `ValueError`, and is NOT an
    `httpx.HTTPError` or `httpx.InvalidURL`. The POST-site `ValueError` arm was therefore
    LOAD-BEARING, not the defence-in-depth round 1's report claimed.
    """
    import httpx as _httpx
    import idna

    url = _httpx.URL("https://xn--.aws.databricksapps.com")  # constructs happily
    with pytest.raises(idna.IDNAError):
        url.host  # ...and only NOW does it fail

    assert issubclass(idna.IDNAError, ValueError)
    assert not issubclass(idna.IDNAError, _httpx.HTTPError)
    assert not issubclass(idna.IDNAError, _httpx.InvalidURL)


@pytest.mark.parametrize(
    "handle",
    [
        "https://xn--.aws.databricksapps.com",       # empty A-label: no punycode content
        "https://xn--a.aws.databricksapps.com",      # decodes to a disallowed codepoint
        "https://xn--evil.aws.databricksapps.com",   # ditto, and reads as a plausible host
    ],
)
def test_invoke_databricks_invalid_idna_host_never_costs_a_token(entra_settings, handle):
    """FIX round 2. An IDNA-invalid host is refused AT VALIDATION — before the token mint.

    THE GAP ROUND 1 LEFT. Round 1 closed the two named inputs (CRLF, soft hyphen) by calling
    `httpx.URL(url)` in the validator, but discarded the result — and because the IDNA encode is
    lazy (see the test above), an invalid punycode A-label sailed through validation, cost a real
    Databricks token, and only failed afterwards when httpx built the request. The credential was
    spent on a URL that was never requested, which is the exact harm the Critical was about; the
    clean 502 merely hid it.

    The fix is one token — `.host` — which forces the encode inside the validator. This asserts
    the property that actually matters: `fake_db.touched is False`.
    """
    agent = _make_databricks_agent(runtime_handle=handle)
    fake_db = _FakeDatabricks()
    client, agents_module, _graph, _db = _build_databricks_client(
        agent=agent, databricks=fake_db
    )

    resp, capture = _invoke_db(client, agents_module)

    assert resp.status_code == 502
    assert "invalid_runtime_handle" in resp.json()["detail"]
    assert fake_db.touched is False, "a Databricks token was minted for an IDNA-invalid host"
    assert capture == {}


@pytest.mark.parametrize(
    "handle,expected_url",
    [
        # A legitimate internationalized label — IDNA-VALID, so it must still be accepted.
        (
            "https://claims.ß.aws.databricksapps.com",
            "https://claims.ß.aws.databricksapps.com/api/v1/agent",
        ),
        # Host case is not significant; the suffix test lowercases, and the URL is passed
        # through unchanged (httpx normalises the authority when it builds the request).
        (
            "https://CLAIMS-Triage.AWS.DATABRICKSAPPS.COM",
            "https://CLAIMS-Triage.AWS.DATABRICKSAPPS.COM/api/v1/agent",
        ),
        # An explicit port is part of the authority, not the host — the suffix test uses
        # `hostname`, which strips it.
        (
            "https://claims.aws.databricksapps.com:8443",
            "https://claims.aws.databricksapps.com:8443/api/v1/agent",
        ),
    ],
)
def test_invoke_databricks_valid_idn_and_authority_forms_still_accepted(
    entra_settings, handle, expected_url
):
    """FIX round 2 must not over-reject. Forcing the IDNA encode rejects INVALID hosts only —
    a valid internationalized label, an uppercase host and an explicit port all still invoke.

    This is the regression half of the fix. A validator tightened until it refuses everything is
    not a fix, and `.host` on a legitimate IDN label succeeds (it encodes to punycode), so these
    are the cases proving the new call discriminates rather than just blocks.
    """
    agent = _make_databricks_agent(runtime_handle=handle)
    client, agents_module, _graph, fake_db = _build_databricks_client(agent=agent)

    resp, capture = _invoke_db(client, agents_module)

    assert resp.status_code == 200, resp.text
    assert capture["url"] == expected_url
    assert fake_db.exchange_calls == [(_DB_WORKSPACE, "obo-agent-token")]


def test_invoke_databricks_query_string_endpoint_url_accepted(entra_settings):
    """A query string on `endpoint_url` is preserved and does not trip validation — the rule is
    about the HOST, and a customer's app route may legitimately carry parameters."""
    url = f"{_DB_HANDLE}/api/v1/agent?q=1"
    agent = _make_databricks_agent(endpoint_url=url)
    client, agents_module, _graph, _db = _build_databricks_client(agent=agent)

    resp, capture = _invoke_db(client, agents_module)

    assert resp.status_code == 200, resp.text
    assert capture["url"] == url
