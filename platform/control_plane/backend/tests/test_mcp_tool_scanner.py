"""MCP wire-protocol tool-scanner tests (Epic 7, Task T-SCAN).

``scan_mcp_tools`` hand-rolls the MCP "Streamable HTTP" JSON-RPC handshake
(``initialize`` → ``notifications/initialized`` → ``tools/list``) over async
``httpx`` to read a server's tools (research §5). NO live network — every test
injects an ``httpx.AsyncClient`` backed by an ``httpx.MockTransport`` whose
handler records each request and returns a canned response (the repo's pure-
service mocking idiom — see ``test_graph_service.py``).

The MockTransport handler captures the ordered list of (method, body) JSON-RPC
frames + the request headers, so the tests can assert the 3-POST order, the
header shape (Accept lists BOTH content types, MCP-Protocol-Version present /
negotiated, Authorization present only with a bearer, echoed Mcp-Session-Id),
the JSON-vs-SSE fork, pagination on ``nextCursor``, and the
``inputSchema``→``input_schema`` mapping. Error paths (JSON-RPC error frame,
4xx/5xx, timeout) raise ``McpScanError``.
"""

from __future__ import annotations

import json

import httpx
import pytest

from models.mcp_server import McpTool
from services.mcp_tool_scanner import McpScanError, scan_mcp_tools

ENDPOINT = "https://gw-abc123.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"


# ---------------------------------------------------------------------------
# Mock-transport handler builder
# ---------------------------------------------------------------------------

def _sse(frame: dict) -> httpx.Response:
    """A ``text/event-stream`` response carrying ``frame`` as a single ``data:`` event."""
    body = f'data: {json.dumps(frame)}\n\n'
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, text=body
    )


def _jsonr(frame: dict, *, headers: dict | None = None) -> httpx.Response:
    """An ``application/json`` JSON-RPC response carrying ``frame``."""
    hdrs = {"content-type": "application/json"}
    if headers:
        hdrs.update(headers)
    return httpx.Response(200, headers=hdrs, json=frame)


def _make_client(responder):
    """Build an ``httpx.AsyncClient`` over a MockTransport + a shared `calls` log.

    ``responder(call_index, body, request)`` returns the ``httpx.Response`` for each
    request. Each call is appended to ``calls`` as a dict with ``method``/``body``/
    ``headers`` so tests can assert order + header shape.
    """
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else {}
        calls.append(
            {
                "method": body.get("method"),
                "body": body,
                "headers": dict(request.headers),
                "url": str(request.url),
            }
        )
        return responder(len(calls) - 1, body, request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, calls


# A canonical initialize result; protocolVersion echoes the sent version unless a
# test overrides it.
def _init_result(protocol_version: str = "2025-06-18") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": protocol_version,
            "capabilities": {},
            "serverInfo": {"name": "test-mcp", "version": "1.0.0"},
        },
    }


def _tools_result(tools: list[dict], *, next_cursor: str | None = None) -> dict:
    result: dict = {"tools": tools}
    if next_cursor is not None:
        result["nextCursor"] = next_cursor
    return {"jsonrpc": "2.0", "id": 2, "result": result}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_does_handshake_then_tools_list():
    """The 3 POSTs happen IN ORDER (initialize, notifications/initialized,
    tools/list); Accept lists BOTH content types; MCP-Protocol-Version is sent."""

    def responder(i, body, request):
        if body.get("method") == "initialize":
            return _jsonr(_init_result())
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        if body.get("method") == "tools/list":
            return _jsonr(_tools_result([]))
        raise AssertionError(f"unexpected method {body.get('method')}")

    client, calls = _make_client(responder)
    async with client:
        await scan_mcp_tools(ENDPOINT, http_client=client)

    assert [c["method"] for c in calls] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    # All three POST to the same endpoint.
    assert all(c["url"] == ENDPOINT for c in calls)
    # Accept lists BOTH content types on every POST.
    for c in calls:
        accept = c["headers"]["accept"]
        assert "application/json" in accept
        assert "text/event-stream" in accept
        assert c["headers"]["mcp-protocol-version"]  # present + truthy
        assert c["headers"]["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_scan_maps_inputSchema_to_input_schema():
    """A tool ``{name, description, inputSchema}`` → ``McpTool`` with the schema in the
    real ``input_schema`` field."""
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}

    def responder(i, body, request):
        if body.get("method") == "initialize":
            return _jsonr(_init_result())
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return _jsonr(
            _tools_result(
                [{"name": "search", "description": "Search the web", "inputSchema": schema}]
            )
        )

    client, _ = _make_client(responder)
    async with client:
        tools = await scan_mcp_tools(ENDPOINT, http_client=client)

    assert len(tools) == 1
    tool = tools[0]
    assert isinstance(tool, McpTool)
    assert tool.name == "search"
    assert tool.description == "Search the web"
    assert tool.input_schema == schema


@pytest.mark.asyncio
async def test_scan_handles_json_response():
    """An ``application/json`` body is parsed via ``resp.json()``."""

    def responder(i, body, request):
        if body.get("method") == "initialize":
            return _jsonr(_init_result())
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return _jsonr(_tools_result([{"name": "a", "inputSchema": {}}]))

    client, _ = _make_client(responder)
    async with client:
        tools = await scan_mcp_tools(ENDPOINT, http_client=client)

    assert [t.name for t in tools] == ["a"]


@pytest.mark.asyncio
async def test_scan_handles_sse_response():
    """A ``text/event-stream`` body with ``data:`` frames is buffered + parsed."""

    def responder(i, body, request):
        if body.get("method") == "initialize":
            return _sse(_init_result())
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return _sse(_tools_result([{"name": "sse-tool", "inputSchema": {}}]))

    client, _ = _make_client(responder)
    async with client:
        tools = await scan_mcp_tools(ENDPOINT, http_client=client)

    assert [t.name for t in tools] == ["sse-tool"]


@pytest.mark.asyncio
async def test_scan_echoes_session_id():
    """``initialize`` returns an ``Mcp-Session-Id`` response header → echoed on the
    later POSTs."""

    def responder(i, body, request):
        if body.get("method") == "initialize":
            return _jsonr(_init_result(), headers={"Mcp-Session-Id": "sess-xyz"})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return _jsonr(_tools_result([]))

    client, calls = _make_client(responder)
    async with client:
        await scan_mcp_tools(ENDPOINT, http_client=client)

    # initialize had no session id to send; the later two echo it.
    assert "mcp-session-id" not in {k.lower() for k in calls[0]["headers"]}
    assert calls[1]["headers"]["mcp-session-id"] == "sess-xyz"
    assert calls[2]["headers"]["mcp-session-id"] == "sess-xyz"


@pytest.mark.asyncio
async def test_scan_uses_negotiated_protocol_version():
    """``result.protocolVersion`` ≠ the sent version → the header switches to the
    negotiated value on the subsequent POSTs."""

    def responder(i, body, request):
        if body.get("method") == "initialize":
            return _jsonr(_init_result(protocol_version="2025-03-26"))
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return _jsonr(_tools_result([]))

    client, calls = _make_client(responder)
    async with client:
        await scan_mcp_tools(ENDPOINT, http_client=client)

    # initialize sends the default version; the later POSTs send the negotiated one.
    assert calls[0]["headers"]["mcp-protocol-version"] == "2025-06-18"
    assert calls[1]["headers"]["mcp-protocol-version"] == "2025-03-26"
    assert calls[2]["headers"]["mcp-protocol-version"] == "2025-03-26"


@pytest.mark.asyncio
async def test_scan_paginates_on_next_cursor():
    """A first ``tools/list`` returning ``nextCursor`` → a second page is fetched with
    ``params.cursor``; tools from both pages are returned."""

    def responder(i, body, request):
        if body.get("method") == "initialize":
            return _jsonr(_init_result())
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        # tools/list — page 1 has a cursor, page 2 does not.
        if body["params"].get("cursor") is None:
            return _jsonr(
                _tools_result([{"name": "p1", "inputSchema": {}}], next_cursor="CUR1")
            )
        return _jsonr(_tools_result([{"name": "p2", "inputSchema": {}}]))

    client, calls = _make_client(responder)
    async with client:
        tools = await scan_mcp_tools(ENDPOINT, http_client=client)

    assert [t.name for t in tools] == ["p1", "p2"]
    # Two tools/list POSTs; the second carried the cursor.
    tools_list_calls = [c for c in calls if c["method"] == "tools/list"]
    assert len(tools_list_calls) == 2
    assert tools_list_calls[1]["body"]["params"]["cursor"] == "CUR1"


@pytest.mark.asyncio
async def test_scan_no_bearer_omits_auth_header():
    """``bearer=None`` (the pre-lockdown path) → NO Authorization header on any POST."""

    def responder(i, body, request):
        if body.get("method") == "initialize":
            return _jsonr(_init_result())
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return _jsonr(_tools_result([]))

    client, calls = _make_client(responder)
    async with client:
        await scan_mcp_tools(ENDPOINT, bearer=None, http_client=client)

    for c in calls:
        assert "authorization" not in {k.lower() for k in c["headers"]}


@pytest.mark.asyncio
async def test_scan_with_bearer_sets_auth_header():
    """``bearer="tok"`` → ``Authorization: Bearer tok`` present on every POST."""

    def responder(i, body, request):
        if body.get("method") == "initialize":
            return _jsonr(_init_result())
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return _jsonr(_tools_result([]))

    client, calls = _make_client(responder)
    async with client:
        await scan_mcp_tools(ENDPOINT, bearer="tok", http_client=client)

    for c in calls:
        assert c["headers"]["authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_scan_raises_on_jsonrpc_error_frame():
    """An ``{"error": {...}}`` JSON-RPC frame → ``McpScanError``."""

    def responder(i, body, request):
        if body.get("method") == "initialize":
            return _jsonr(_init_result())
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return _jsonr(
            {"jsonrpc": "2.0", "id": 2, "error": {"code": -32601, "message": "Method not found"}}
        )

    client, _ = _make_client(responder)
    with pytest.raises(McpScanError):
        async with client:
            await scan_mcp_tools(ENDPOINT, http_client=client)


@pytest.mark.asyncio
async def test_scan_raises_on_timeout():
    """A transport raising ``httpx.TimeoutException`` → ``McpScanError``."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(McpScanError):
        async with client:
            await scan_mcp_tools(ENDPOINT, http_client=client)


@pytest.mark.asyncio
async def test_scan_raises_on_4xx():
    """A 4xx/5xx response (here 401) → ``McpScanError``."""

    def responder(i, body, request):
        return httpx.Response(401, json={"error": "unauthorized"})

    client, _ = _make_client(responder)
    with pytest.raises(McpScanError):
        async with client:
            await scan_mcp_tools(ENDPOINT, http_client=client)


@pytest.mark.asyncio
async def test_scan_caps_pagination_at_max_pages():
    """A server returning a self-referential ``nextCursor`` forever → the scan
    stops at the page cap (_MAX_PAGES == 50) without hanging, and the number of
    ``tools/list`` POSTs equals exactly 50."""
    from services.mcp_tool_scanner import _MAX_PAGES

    def responder(i, body, request):
        if body.get("method") == "initialize":
            return _jsonr(_init_result())
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        # Always return the same cursor — an infinite-loop server.
        return _jsonr(_tools_result([{"name": "t", "inputSchema": {}}], next_cursor="FOREVER"))

    client, calls = _make_client(responder)
    async with client:
        tools = await scan_mcp_tools(ENDPOINT, http_client=client)

    tools_list_calls = [c for c in calls if c["method"] == "tools/list"]
    assert len(tools_list_calls) == _MAX_PAGES
    # Each page contributed one tool.
    assert len(tools) == _MAX_PAGES


@pytest.mark.asyncio
async def test_scan_raises_on_non_dict_jsonrpc_body():
    """An ``initialize`` response whose JSON body is a list/scalar (not an object)
    → ``McpScanError`` (I-1 fix: the dict-ness chokepoint in _frame_or_raise
    covers both JSON and SSE transports)."""

    def responder_list(i, body, request):
        # Return a JSON array instead of an object — malformed remote.
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json=[1, 2, 3],
        )

    client, _ = _make_client(responder_list)
    with pytest.raises(McpScanError):
        async with client:
            await scan_mcp_tools(ENDPOINT, http_client=client)


@pytest.mark.asyncio
async def test_scan_raises_on_nameless_tool():
    """A ``tools/list`` result containing a tool with no ``name`` key → ``McpScanError``
    (I-2 fix: ``ValidationError`` from ``McpTool(name="")`` is converted cleanly)."""

    def responder(i, body, request):
        if body.get("method") == "initialize":
            return _jsonr(_init_result())
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        # Tool is missing the "name" key entirely.
        return _jsonr(_tools_result([{"description": "no name here", "inputSchema": {}}]))

    client, _ = _make_client(responder)
    with pytest.raises(McpScanError):
        async with client:
            await scan_mcp_tools(ENDPOINT, http_client=client)
