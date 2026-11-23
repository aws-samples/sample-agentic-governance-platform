"""Harness invoke through AGP and the runtime-only guards.

A config agent's AgentCore HARNESS ARN lands in the same ``agent_arn`` / ``agent_arns`` slots a
runtime's does, so the ARN's resource type is the discriminator:

  - ``AwsPlatformAdapter.invoke`` POSTs ``/harnesses/invoke?harnessArn=…`` with the OBO bearer and
    decodes the AWS binary event stream (NOT SSE) into one ``{"response": …}``.
  - Every runtime-only AgentCore call (``GetAgentRuntime`` / ``UpdateAgentRuntime`` /
    ``DeleteAgentRuntime``) skips a harness ARN with NO AWS call.

The event-stream frames are REAL bytes (prelude + CRCs), decoded by the production
``botocore.eventstream.EventStreamBuffer`` — botocore ships a decoder but no encoder, so
``_frame`` builds them by hand. No live calls: httpx is patched, the control client is a mock.
"""

import asyncio
import binascii
import contextlib
import json
import struct
from collections.abc import AsyncIterable
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import httpx
import pytest
from test_invoke_route import (
    _build_client,
    _claims_for,
    _headers,
    _make_agent,
)

from services.platform_adapter import InvokeError

if TYPE_CHECKING:
    from api.routes.agents import InvokeRequest
    from core.rbac import Principal

pytestmark = pytest.mark.usefixtures("reset_modules")

HARNESS_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:harness/hrn_abc-XyZ123"
PROD_HARNESS_ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:harness/hrn_prod-Q1"


def _frame(headers: dict, payload: bytes) -> bytes:
    """One AWS event-stream message: prelude (total, headers len, CRC) + headers + payload + CRC.

    Header values are type 7 (string) — the only type AgentCore sends on these frames.
    """
    hbytes = b""
    for name, value in headers.items():
        n, v = name.encode(), value.encode()
        hbytes += struct.pack("!B", len(n)) + n + b"\x07" + struct.pack("!H", len(v)) + v
    total = 12 + len(hbytes) + len(payload) + 4
    prelude = struct.pack("!II", total, len(hbytes))
    prelude += struct.pack("!I", binascii.crc32(prelude) & 0xFFFFFFFF)
    body = prelude + hbytes + payload
    return body + struct.pack("!I", binascii.crc32(body) & 0xFFFFFFFF)


def _event(event_type: str, payload: dict) -> bytes:
    return _frame(
        {":message-type": "event", ":event-type": event_type, ":content-type": "application/json"},
        json.dumps(payload).encode(),
    )


def _stream(*texts: str) -> bytes:
    """The live-observed shape (spike §3): messageStart, N deltas, stop events, metadata."""
    out = _event("messageStart", {"role": "assistant"})
    for t in texts:
        out += _event("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": t}})
    out += _event("contentBlockStop", {"contentBlockIndex": 0})
    out += _event("messageStop", {"stopReason": "end_turn"})
    out += _event("metadata", {"usage": {"inputTokens": 3, "outputTokens": 4}})
    return out


class _FakeAsyncClient:
    def __init__(self, response, capture, **kwargs):
        self._response = response
        self._capture = capture
        capture["client_kwargs"] = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @contextlib.asynccontextmanager
    async def stream(self, method, url, **kwargs):
        self._capture["method"] = method
        self._capture["url"] = url
        self._capture["kwargs"] = kwargs
        yield self._response


def _patch_httpx(response, capture):
    return patch.object(httpx, "AsyncClient", lambda *a, **kw: _FakeAsyncClient(response, capture, **kw))


def _response(status_code=200, content: bytes | AsyncIterable[bytes] = b"", headers=None):
    return httpx.Response(
        status_code,
        headers={"content-type": "application/vnd.amazon.eventstream", **(headers or {})},
        content=content,
    )


def _chunked(data: bytes, size: int):
    """A body delivered in ``size``-byte network chunks (frames split across chunk borders)."""

    async def gen():
        for i in range(0, len(data), size):
            yield data[i : i + size]

    return gen()


def _adapter():
    from services.agent_identity_service import AgentIdentityService
    from services.platform_adapters.aws import AwsPlatformAdapter

    return AwsPlatformAdapter(identity=MagicMock(spec=AgentIdentityService))


def _invoke(agent, response, capture, *, stage=None, prompt="hello", **kw):
    with _patch_httpx(response, capture):
        return asyncio.run(
            _adapter().invoke(
                agent,
                cast("InvokeRequest", SimpleNamespace(prompt=prompt)),
                cast("Principal", SimpleNamespace(oid="user-oid")),
                "obo-agent-token",
                stage,
                **kw,
            )
        )


def _harness_agent(**over):
    return _make_agent(agent_arn=HARNESS_ARN, framework="harness", **over)


def test_harness_invoke_request_shape(entra_settings):
    capture = {}
    out = _invoke(_harness_agent(), _response(content=_stream("hi")), capture, prompt="what is 1+1?")

    assert out["response"] == "hi"
    assert capture["method"] == "POST"
    assert capture["url"] == (
        "https://bedrock-agentcore.us-east-1.amazonaws.com/harnesses/invoke"
        f"?harnessArn={quote(HARNESS_ARN, safe='')}"
    )
    # The FULL ARN is encoded into the query (colons and the slash included).
    assert "harness%2Fhrn_abc-XyZ123" in capture["url"]
    headers = capture["kwargs"]["headers"]
    assert headers["Authorization"] == "Bearer obo-agent-token"
    assert headers["Content-Type"] == "application/json"
    assert len(headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"]) >= 33
    assert capture["kwargs"]["json"] == {
        "messages": [{"role": "user", "content": [{"text": "what is 1+1?"}]}]
    }
    assert capture["client_kwargs"]["timeout"] == 120.0


def test_harness_invoke_sends_the_given_memory_actor(entra_settings):
    capture = {}
    _invoke(_harness_agent(), _response(content=_stream("hi")), capture, actor_id="agp-eval-x")
    assert capture["kwargs"]["json"]["actorId"] == "agp-eval-x"


@pytest.mark.parametrize("actor_id", [None, "", "a b", "../x", "x" * 300])
def test_harness_invoke_sends_no_actor_when_missing_or_unsafe(entra_settings, actor_id):
    capture = {}
    _invoke(_harness_agent(), _response(content=_stream("hi")), capture, actor_id=actor_id)
    assert "actorId" not in capture["kwargs"]["json"]


def test_harness_invoke_uses_the_stages_harness(entra_settings):
    capture = {}
    agent = _harness_agent(agent_arns={"dev": HARNESS_ARN, "prod": PROD_HARNESS_ARN})
    _invoke(agent, _response(content=_stream("ok")), capture, stage="prod")
    assert capture["url"].startswith(
        "https://bedrock-agentcore.us-west-2.amazonaws.com/harnesses/invoke?harnessArn="
    )
    assert quote(PROD_HARNESS_ARN, safe="") in capture["url"]


def test_harness_multi_delta_stream_joins_text_in_order(entra_settings):
    out = _invoke(_harness_agent(), _response(content=_stream("Hello", " there,", " Lars!")), {})
    assert out["response"] == "Hello there, Lars!"


def test_harness_non_text_deltas_are_ignored(entra_settings):
    body = _event("contentBlockDelta", {"delta": {"toolUse": {"input": "{}"}}}) + _event(
        "contentBlockDelta", {"delta": {"text": "done"}}
    )
    assert _invoke(_harness_agent(), _response(content=body), {})["response"] == "done"


def test_harness_exception_frame_maps_to_upstream_error_with_its_type(entra_settings):
    body = _event("contentBlockDelta", {"delta": {"text": "par"}}) + _frame(
        {
            ":message-type": "exception",
            ":exception-type": "validationException",
            ":content-type": "application/json",
        },
        json.dumps({"message": "secret upstream detail 111122223333"}).encode(),
    )
    with pytest.raises(InvokeError) as exc:
        _invoke(_harness_agent(), _response(content=body), {})
    assert exc.value.status_code == 502
    assert exc.value.detail == "agent returned an error response [validationException]"
    assert "111122223333" not in exc.value.detail


def test_harness_corrupt_stream_maps_to_upstream_error(entra_settings):
    with pytest.raises(InvokeError) as exc:
        _invoke(_harness_agent(), _response(content=b"\x00\x00\x00\x20garbage-not-a-frame...."), {})
    assert exc.value.status_code == 502
    assert exc.value.detail == "agent returned an error response"


def test_harness_stream_split_across_chunks_decodes(entra_settings):
    data = _stream("Hello", " there,", " Lars!")
    out = _invoke(_harness_agent(), _response(content=_chunked(data, 7)), {})
    assert out["response"] == "Hello there, Lars!"


def test_harness_reply_over_the_byte_cap_maps_to_upstream_error(entra_settings, monkeypatch):
    from services.platform_adapters import aws

    monkeypatch.setattr(aws, "_MAX_HARNESS_REPLY_BYTES", 1000)
    data = _stream(*["x" * 50] * 20)  # well over 1000 bytes of valid frames
    with pytest.raises(InvokeError) as exc:
        _invoke(_harness_agent(), _response(content=_chunked(data, 200)), {})
    assert (exc.value.status_code, exc.value.detail) == (502, "agent returned an error response")


def test_harness_error_frame_maps_to_upstream_error_with_its_code(entra_settings):
    body = _event("contentBlockDelta", {"delta": {"text": "part"}}) + _frame(
        {
            ":message-type": "error",
            ":error-code": "InternalFailure",
            ":error-message": "secret upstream detail 111122223333",
        },
        b"",
    )
    with pytest.raises(InvokeError) as exc:
        _invoke(_harness_agent(), _response(content=body), {})
    assert (exc.value.status_code, exc.value.detail) == (
        502,
        "agent returned an error response [InternalFailure]",
    )
    assert "111122223333" not in exc.value.detail


def test_harness_unknown_message_type_with_free_text_code_has_no_suffix(entra_settings):
    body = _frame({":message-type": "error", ":error-code": "bad code: 111122223333"}, b"")
    with pytest.raises(InvokeError) as exc:
        _invoke(_harness_agent(), _response(content=body), {})
    assert exc.value.detail == "agent returned an error response"


def test_harness_truncated_final_frame_maps_to_upstream_error(entra_settings):
    data = _stream("A", "B")[:-5]
    with pytest.raises(InvokeError) as exc:
        _invoke(_harness_agent(), _response(content=data), {})
    assert (exc.value.status_code, exc.value.detail) == (502, "agent returned an error response")


@pytest.mark.parametrize("content", [b"", b'{"x":1}', _event("messageStop", {"stopReason": "end_turn"})])
def test_harness_reply_with_no_text_maps_to_upstream_error(entra_settings, content):
    with pytest.raises(InvokeError) as exc:
        _invoke(_harness_agent(), _response(content=content), {})
    assert (exc.value.status_code, exc.value.detail) == (502, "agent returned an error response")


@pytest.mark.parametrize("status", [401, 403])
def test_harness_401_403_map_to_the_auth_kind(entra_settings, status):
    with pytest.raises(InvokeError) as exc:
        _invoke(_harness_agent(), _response(status, b'{"message":"audience mismatch"}'), {})
    assert (exc.value.status_code, exc.value.detail) == (502, "agent rejected the token")


def test_harness_500_unrecognized_client_header_maps_to_the_auth_kind(entra_settings):
    resp = _response(
        500,
        b'{"message":"Invalid Signature for bearerToken"}',
        {"x-amzn-errortype": "UnrecognizedClientException:http://internal.amazon.com/"},
    )
    with pytest.raises(InvokeError) as exc:
        _invoke(_harness_agent(), resp, {})
    assert (exc.value.status_code, exc.value.detail) == (502, "agent rejected the token")


def test_harness_500_unrecognized_client_body_maps_to_the_auth_kind(entra_settings):
    resp = _response(500, b'{"__type":"UnrecognizedClientException","message":"Invalid Signature"}')
    with pytest.raises(InvokeError) as exc:
        _invoke(_harness_agent(), resp, {})
    assert (exc.value.status_code, exc.value.detail) == (502, "agent rejected the token")


def test_harness_other_500_maps_to_the_upstream_kind(entra_settings):
    resp = _response(500, b'{"message":"internal"}', {"x-amzn-errortype": "InternalServerException"})
    with pytest.raises(InvokeError) as exc:
        _invoke(_harness_agent(), resp, {})
    assert (exc.value.status_code, exc.value.detail) == (502, "agent returned an error response")


def test_harness_bad_region_refused_before_any_request(entra_settings):
    capture = {}
    agent = _harness_agent()
    # Bypass the write-time validator — the invoke-time guard must hold on its own.
    object.__setattr__(
        agent, "agent_arn", "arn:aws:bedrock-agentcore:evil.example.com/x:111122223333:harness/h"
    )
    with pytest.raises(InvokeError) as exc:
        _invoke(agent, _response(content=_stream("x")), capture)
    assert exc.value.detail == "the agent could not be invoked [agent_arn_invalid]"
    assert "url" not in capture


def test_route_invokes_a_harness_agent_end_to_end(entra_settings):
    mock_registry = MagicMock()
    mock_registry.get.return_value = _harness_agent()
    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock(return_value="obo-agent-token")
    client, _ = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)
    capture = {}

    with (
        patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")),
        _patch_httpx(_response(content=_stream("Hello", " Lars")), capture),
    ):
        resp = client.post("/api/v1/agents/rec-123/invoke", json={"prompt": "who am I?"}, headers=_headers())

    assert resp.status_code == 200, resp.text
    assert resp.json()["response"] == "Hello Lars"
    assert "/harnesses/invoke?harnessArn=" in capture["url"]
    assert capture["kwargs"]["headers"]["Authorization"] == "Bearer obo-agent-token"
    # The caller's oid is the harness memory actor, so one user's facts never reach another.
    assert capture["kwargs"]["json"]["actorId"] == "viewer-oid"
    mock_graph.obo_exchange.assert_awaited_once()


def _runtime_client_error_stream() -> bytes:
    """The live L16 failure: the harness refuses the whole invoke with an exception frame."""
    return _frame(
        {
            ":message-type": "exception",
            ":exception-type": "runtimeClientError",
            ":content-type": "application/json",
        },
        json.dumps({"message": "upstream detail eyJhbGciOiJSUzI1NiJ9.payload.sig"}).encode(),
    )


def _invoke_route(ungranted):
    mock_registry = MagicMock()
    mock_registry.get.return_value = _harness_agent()
    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock(return_value="obo-agent-token")
    client, _ = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)
    seen = []

    def _ungranted(agent, arns):
        seen.append(list(arns))
        return ungranted

    with (
        patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")),
        _patch_httpx(_response(content=_runtime_client_error_stream()), {}),
        patch("services.platform_adapters.aws.ungranted_tool_mcps", _ungranted),
    ):
        resp = client.post("/api/v1/agents/rec-123/invoke", json={"prompt": "Hello"}, headers=_headers())
    return resp, seen


def test_route_names_the_missing_mcp_grant_when_a_harness_refuses_the_invoke(entra_settings):
    """An ungranted gateway in harness.yaml ``tools`` fails every invoke with a bare
    runtimeClientError; when AGP sees an ungranted declared gateway, the 502 names it."""
    resp, seen = _invoke_route([SimpleNamespace(id="mcp-a", name="claims-api")])

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "claims-api" in detail and "grant" in detail
    assert detail.endswith("then invoke again [runtimeClientError]")
    assert seen == [[HARNESS_ARN]]
    assert resp.json()["code"] == "invoke.tools_ungranted"


def test_route_keeps_the_upstream_error_when_every_declared_tool_is_granted(entra_settings):
    resp, _ = _invoke_route([])
    assert resp.status_code == 502
    assert resp.json()["detail"] == "agent returned an error response [runtimeClientError]"
    assert resp.json()["code"] == "invoke.upstream_error"


def test_invoke_error_detail_for_upstream_error_carries_no_upstream_body(entra_settings):
    body = _frame(
        {
            ":message-type": "exception",
            ":exception-type": "validationException",
            ":content-type": "application/json",
        },
        json.dumps(
            {"message": "secret upstream detail 111122223333 eyJhbGciOiJSUzI1NiJ9.payload.sig"}
        ).encode(),
    )
    with pytest.raises(InvokeError) as exc:
        _invoke(_harness_agent(), _response(content=body), {})
    assert exc.value.code == "invoke.upstream_error"
    assert exc.value.detail == "agent returned an error response [validationException]"
    for leaked in ("secret upstream detail", "111122223333", "eyJhbGciOi", "obo-agent-token"):
        assert leaked not in exc.value.detail


def test_invoke_error_detail_for_tools_ungranted_carries_no_upstream_body(entra_settings):
    resp, _ = _invoke_route([SimpleNamespace(id="mcp-a", name="claims-api")])
    detail = resp.json()["detail"]
    assert resp.status_code == 502 and "claims-api" in detail
    for leaked in ("upstream detail", "eyJhbGciOi", "obo-agent-token"):
        assert leaked not in detail


def test_missing_mcp_grants_read_lists_the_ungranted_declared_mcps(entra_settings):
    """The agent-detail hint's read: GET /agents/{id}/mcp-grants/missing → [{mcp_id, mcp_name}]
    over every harness the agent owns (no ARN in the response)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import api.routes.mcp_server_grants as grants_mod

    mock_registry = MagicMock()
    mock_registry.get.return_value = _harness_agent(agent_arns={"dev": HARNESS_ARN, "prod": PROD_HARNESS_ARN})
    _build_client(mock_registry=mock_registry)
    app = FastAPI()
    app.include_router(grants_mod.agent_mcp_router, prefix="/api/v1")
    seen = []

    def _ungranted(agent, arns):
        seen.append(sorted(arns))
        return [SimpleNamespace(id="mcp-a", name="claims-api")]

    with (
        patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")),
        patch("services.agent_mcp_env.ungranted_tool_mcps", _ungranted),
    ):
        resp = TestClient(app).get("/api/v1/agents/rec-123/mcp-grants/missing", headers=_headers())

    assert resp.status_code == 200, resp.text
    assert resp.json() == [{"mcp_id": "mcp-a", "mcp_name": "claims-api"}]
    assert seen == [sorted([HARNESS_ARN, PROD_HARNESS_ARN])]


@pytest.fixture(autouse=True)
def _instant_sleep(monkeypatch):
    """A guard that regressed would poll ``get_agent_runtime`` to READY; never really sleep."""
    import services.agent_identity_service as ais

    monkeypatch.setattr(ais.time, "sleep", lambda *_: None)


def _identity(control):
    from services.agent_identity_service import AgentIdentityService

    registry = MagicMock()
    registry.persist_identity.side_effect = lambda a: a
    return AgentIdentityService(
        graph=AsyncMock(),
        registry=registry,
        control_client=control,
        region="us-east-1",
        tenant_id="00000000-0000-0000-0000-000000000001",
        login_base="https://login.microsoftonline.com",
    )


def test_guard_runtime_status_does_not_probe_a_harness(entra_settings):
    control = MagicMock()
    status = _identity(control).runtime_status(_harness_agent(agent_arns={"dev": HARNESS_ARN}))
    assert status.status == "unknown"
    assert status.stage == "dev"
    assert status.detail == "status not tracked for config agents"
    assert status.environment_variables is None  # no evidence → reconcile heals nothing
    control.get_agent_runtime.assert_not_called()


def test_guard_provision_runtime_skips_the_harness_authorizer(entra_settings):
    """The harness module owns its authorizer; a reprovision must not call
    GetAgentRuntime on a harness id and flip the agent to failed."""
    control = MagicMock()
    agent = _harness_agent(identity_status="pending")
    out = asyncio.run(_identity(control).provision_runtime(agent))
    assert out.identity_status == "provisioned"
    control.get_agent_runtime.assert_not_called()
    control.update_agent_runtime.assert_not_called()


def test_guard_delete_runtime_and_runtime_exists_skip_a_harness(entra_settings):
    control = MagicMock()
    svc = _identity(control)
    svc.delete_runtime(HARNESS_ARN)
    assert svc.runtime_exists(HARNESS_ARN) is False
    control.get_agent_runtime.assert_not_called()
    control.delete_agent_runtime.assert_not_called()


def test_guard_set_runtime_environment_skips_a_harness(entra_settings):
    control = MagicMock()
    assert _identity(control).set_runtime_environment(HARNESS_ARN, {"K": "v"}) is None
    control.get_agent_runtime.assert_not_called()
    control.update_agent_runtime.assert_not_called()


# The toolUse frame shapes (``contentBlockStart.start.toolUse`` / ``contentBlockDelta.delta.toolUse
# .input``) are the Converse-stream ones and are unverified live against a harness, so the parse is
# tolerant: unknown frames and malformed tool payloads are ignored, never a 502.


def _tool_stream() -> bytes:
    """Two tool blocks (index 1 and 2), each input split across deltas, then the answer text."""
    out = _event("messageStart", {"role": "assistant"})
    out += _event(
        "contentBlockStart",
        {"contentBlockIndex": 1, "start": {"toolUse": {"toolUseId": "t1", "name": "get_policy"}}},
    )
    out += _event(
        "contentBlockDelta",
        {"contentBlockIndex": 1, "delta": {"toolUse": {"input": '{"policy_'}}},
    )
    out += _event(
        "contentBlockDelta",
        {"contentBlockIndex": 1, "delta": {"toolUse": {"input": 'id": "P-1"}'}}},
    )
    out += _event("contentBlockStop", {"contentBlockIndex": 1})
    out += _event(
        "contentBlockStart",
        {"contentBlockIndex": 2, "start": {"toolUse": {"toolUseId": "t2", "name": "open_claim"}}},
    )
    out += _event("contentBlockDelta", {"contentBlockIndex": 2, "delta": {"toolUse": {"input": '{"amount"'}}})
    out += _event("contentBlockDelta", {"contentBlockIndex": 2, "delta": {"toolUse": {"input": ": 5}"}}})
    out += _event("contentBlockStop", {"contentBlockIndex": 2})
    out += _event("someFutureFrame", {"anything": True})
    out += _event("contentBlockDelta", {"contentBlockIndex": 3, "delta": {"text": "Claim opened."}})
    out += _event("messageStop", {"stopReason": "end_turn"})
    return out


def test_harness_returns_tool_calls_from_the_stream(entra_settings):
    out = _invoke(_harness_agent(), _response(content=_tool_stream()), {})

    assert out["response"] == "Claim opened."
    assert out["tool_calls"] == [
        {"name": "get_policy", "args": {"policy_id": "P-1"}},
        {"name": "open_claim", "args": {"amount": 5}},
    ]


def test_harness_tool_calls_survive_frames_split_across_chunks(entra_settings):
    out = _invoke(_harness_agent(), _response(content=_chunked(_tool_stream(), 7)), {})
    assert [c["name"] for c in out["tool_calls"]] == ["get_policy", "open_claim"]


def test_harness_with_no_tool_blocks_returns_an_empty_list(entra_settings):
    out = _invoke(_harness_agent(), _response(content=_stream("hi")), {})
    assert out["tool_calls"] == []


def test_harness_bad_tool_json_gives_empty_args(entra_settings):
    body = _event(
        "contentBlockStart",
        {"contentBlockIndex": 0, "start": {"toolUse": {"name": "lookup"}}},
    )
    body += _event(
        "contentBlockDelta", {"contentBlockIndex": 0, "delta": {"toolUse": {"input": "{not json"}}}
    )
    # A tool block with no input at all, and one whose input decodes to a non-object.
    body += _event("contentBlockStart", {"contentBlockIndex": 1, "start": {"toolUse": {"name": "noop"}}})
    body += _event("contentBlockStart", {"contentBlockIndex": 2, "start": {"toolUse": {"name": "listy"}}})
    body += _event("contentBlockDelta", {"contentBlockIndex": 2, "delta": {"toolUse": {"input": "[1, 2]"}}})
    # Malformed tool frames are ignored, not a 502.
    body += _event("contentBlockStart", {"contentBlockIndex": 4, "start": {"toolUse": "x"}})
    body += _event("contentBlockDelta", {"contentBlockIndex": 9, "delta": {"toolUse": {"input": 1}}})
    body += _event("contentBlockDelta", {"contentBlockIndex": 3, "delta": {"text": "done"}})

    out = _invoke(_harness_agent(), _response(content=body), {})

    assert out["response"] == "done"
    assert out["tool_calls"] == [
        {"name": "lookup", "args": {}},
        {"name": "noop", "args": {}},
        {"name": "listy", "args": {}},
    ]


def test_harness_session_id_is_the_header_sent(entra_settings):
    capture = {}
    out = _invoke(_harness_agent(), _response(content=_stream("hi")), capture)

    sent = capture["kwargs"]["headers"]["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"]
    assert out["session_id"] == sent
    assert len(sent) >= 33


def test_route_passes_harness_session_id_and_tool_calls_through(entra_settings):
    mock_registry = MagicMock()
    mock_registry.get.return_value = _harness_agent()
    mock_graph = MagicMock()
    mock_graph.obo_exchange = AsyncMock(return_value="obo-agent-token")
    client, _ = _build_client(mock_registry=mock_registry, mock_graph=mock_graph)
    capture = {}

    with (
        patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")),
        _patch_httpx(_response(content=_tool_stream()), capture),
    ):
        resp = client.post("/api/v1/agents/rec-123/invoke", json={"prompt": "open"}, headers=_headers())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"] == capture["kwargs"]["headers"]["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"]
    assert body["tool_calls"][1] == {"name": "open_claim", "args": {"amount": 5}}


def test_harness_repeated_block_index_across_messages_keeps_both_calls(entra_settings):
    """Converse restarts ``contentBlockIndex`` at 0 per message; a multi-turn harness reply must
    not let message 2's block 0 overwrite message 1's."""

    def _message(name: str, args: str) -> bytes:
        out = _event("messageStart", {"role": "assistant"})
        out += _event("contentBlockStart", {"contentBlockIndex": 0, "start": {"toolUse": {"name": name}}})
        out += _event("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"toolUse": {"input": args}}})
        out += _event("contentBlockStop", {"contentBlockIndex": 0})
        return out + _event("messageStop", {"stopReason": "tool_use"})

    body = _message("get_policy", '{"policy_id": "P-1"}') + _message("open_claim", '{"amount": 5}')
    body += _event("messageStart", {"role": "assistant"})
    body += _event("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "Opened."}})

    out = _invoke(_harness_agent(), _response(content=body), {})

    assert out["response"] == "Opened."
    assert out["tool_calls"] == [
        {"name": "get_policy", "args": {"policy_id": "P-1"}},
        {"name": "open_claim", "args": {"amount": 5}},
    ]


def test_harness_malformed_block_start_frames_never_fail_the_invoke(entra_settings):
    def _raw_start(payload: bytes) -> bytes:
        return _frame(
            {
                ":message-type": "event",
                ":event-type": "contentBlockStart",
                ":content-type": "application/json",
            },
            payload,
        )

    body = _raw_start(b"{not json")
    body += _raw_start(b'["a list"]')
    body += _event("contentBlockStart", {"contentBlockIndex": 0, "start": "not a dict"})
    body += _event("contentBlockStart", {"contentBlockIndex": 1, "start": {"toolUse": ["x"]}})
    body += _event("contentBlockDelta", {"contentBlockIndex": 2, "delta": {"text": "fine"}})

    out = _invoke(_harness_agent(), _response(content=body), {})

    assert out["response"] == "fine"
    assert out["tool_calls"] == []


def test_harness_text_blocks_across_messages_get_a_paragraph_break(entra_settings):
    """Live 2026-10-08: "...right away!Here's" — message 1's text and message 2's (after a tool
    turn) were joined with "". Different blocks are separate paragraphs; same-block deltas join."""
    body = _event("messageStart", {"role": "assistant"})
    body += _event("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "On it,"}})
    body += _event("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": " right away!"}})
    body += _event("contentBlockStop", {"contentBlockIndex": 0})
    body += _event("contentBlockStart", {"contentBlockIndex": 1, "start": {"toolUse": {"name": "lookup"}}})
    body += _event("contentBlockDelta", {"contentBlockIndex": 1, "delta": {"toolUse": {"input": "{}"}}})
    body += _event("contentBlockStop", {"contentBlockIndex": 1})
    body += _event("messageStop", {"stopReason": "tool_use"})
    body += _event("messageStart", {"role": "assistant"})
    body += _event("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "Here's"}})
    body += _event("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": " the status."}})
    body += _event("messageStop", {"stopReason": "end_turn"})

    out = _invoke(_harness_agent(), _response(content=body), {})

    assert out["response"] == "On it, right away!\n\nHere's the status."


def test_harness_a_new_block_index_without_a_stop_frame_is_a_new_paragraph(entra_settings):
    body = _event("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "First."}})
    body += _event("contentBlockDelta", {"contentBlockIndex": 2, "delta": {"text": "Second."}})
    assert _invoke(_harness_agent(), _response(content=body), {})["response"] == "First.\n\nSecond."


def test_harness_a_hook_event_mid_block_does_not_split_the_text(entra_settings):
    body = _event("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "Hel"}})
    body += _event("hookEvent", {"name": "afterModel"})
    body += _event("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "lo."}})
    assert _invoke(_harness_agent(), _response(content=body), {})["response"] == "Hello."
