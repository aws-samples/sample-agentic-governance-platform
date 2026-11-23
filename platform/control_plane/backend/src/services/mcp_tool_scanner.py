"""MCP Streamable HTTP tool scanner: read a server's declared tools over its own protocol.

The transport is a small JSON-RPC handshake, ``initialize`` -> ``notifications/initialized`` ->
``tools/list``, as POSTs to one endpoint, hand-rolled over ``httpx`` rather than the ``mcp`` SDK.
A response arrives as a JSON body or as a ``text/event-stream`` whose ``data:`` lines carry the
frames. One function serves a gateway's ``gatewayUrl`` and a standalone runtime's or external
server's ``endpoint_url``; it is pure (no boto3, no persistence) and takes an injected
``httpx.AsyncClient`` in tests. ``bearer`` is optional: the pre-lockdown registration scan sends
no Authorization header.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import ClassVar, Optional

import httpx
from pydantic import ValidationError

from core.errors import AppError, Outcome
from models.mcp_server import McpTool

_DEFAULT_PROTOCOL_VERSION = "2025-06-18"
_MAX_PAGES = 50
_CLIENT_INFO = {"name": "agp-scanner", "version": "1.0.0"}


class McpScanError(AppError):
    """The MCP tool scan failed: a JSON-RPC ``error`` frame, a non-2xx HTTP response, a transport
    or timeout failure, or an unparseable response.

    The provisioning hook treats it as best-effort and persists no partial tools. The detail is
    fixed; the message may quote the server's error frame for the log.
    """

    domain: ClassVar[str] = "mcp_scan"
    kinds: ClassVar[Mapping[str, Outcome]] = {"scan_failed": Outcome(502, "MCP tool scan failed")}


def _extract_jsonrpc(resp: httpx.Response) -> object:
    """The JSON-RPC frame of an MCP response, by Content-Type.

    A ``text/event-stream`` body is read as ``data:`` lines, each parsed as JSON, returning the
    last frame carrying ``result`` or ``error`` (else the last parseable payload); any other body is
    ``resp.json()``. May return a non-dict for a malformed body; ``_frame_or_raise`` rejects those.
    """
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" not in content_type:
        return resp.json()

    last_frame = None
    last_payload = None
    for raw_line in resp.text.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except (ValueError, TypeError):
            continue
        last_payload = payload
        if isinstance(payload, dict) and ("result" in payload or "error" in payload):
            last_frame = payload

    frame = last_frame if last_frame is not None else last_payload
    if not isinstance(frame, dict):
        raise McpScanError("MCP SSE response carried no parseable JSON-RPC frame")
    return frame


def _frame_or_raise(resp: httpx.Response) -> dict:
    """The JSON-RPC frame of a 2xx response; a non-2xx status, a non-object frame and an ``error``
    frame each raise."""
    if not (200 <= resp.status_code < 300):
        raise McpScanError(f"MCP server returned HTTP {resp.status_code}")
    frame = _extract_jsonrpc(resp)
    if not isinstance(frame, dict):
        raise McpScanError("MCP response was not a JSON-RPC object")
    if frame.get("error"):
        raise McpScanError(f"MCP JSON-RPC error: {frame['error']}")
    return frame


def _headers(protocol_version: str, session_id: Optional[str], bearer: Optional[str]) -> dict:
    """The POST headers: both response formats accepted (the server picks), the negotiated
    protocol version, the session id once the server has issued one, and the bearer only when
    given."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": protocol_version,
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return headers


def _tool_from_entry(entry: dict) -> McpTool:
    try:
        return McpTool(
            name=entry.get("name", ""),
            description=entry.get("description", "") or "",
            input_schema=entry.get("inputSchema") or {},
        )
    except ValidationError as exc:
        raise McpScanError(f"MCP tool entry failed validation: {exc}") from exc


async def _list_tools(post: Callable[[dict], Awaitable[httpx.Response]]) -> list[McpTool]:
    """``tools/list``, following ``result.nextCursor`` for at most ``_MAX_PAGES`` pages so a
    misbehaving server can never spin the scan forever."""
    tools: list[McpTool] = []
    cursor: Optional[str] = None
    for _ in range(_MAX_PAGES):
        params: dict = {"cursor": cursor} if cursor else {}
        list_resp = await post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": params})
        result = _frame_or_raise(list_resp).get("result") or {}
        tools.extend(_tool_from_entry(t) for t in result.get("tools") or [])
        cursor = result.get("nextCursor")
        if not cursor:
            break
    return tools


async def scan_mcp_tools(
    endpoint_url: str,
    bearer: Optional[str] = None,
    *,
    timeout: float = 30.0,
    http_client: Optional[httpx.AsyncClient] = None,
) -> list[McpTool]:
    """Scan an MCP server and return its declared tools.

    Runs ``initialize`` -> ``notifications/initialized`` -> ``tools/list`` as POSTs to the same
    ``endpoint_url``. The ``initialize`` result's ``protocolVersion`` is echoed on the
    ``MCP-Protocol-Version`` header from then on, and the ``Mcp-Session-Id`` response header, if
    any, on the later POSTs. ``Authorization: Bearer`` is sent only when ``bearer`` is truthy. A
    JSON-RPC ``error`` frame, any non-2xx status, or a transport failure raises
    :class:`McpScanError` with no partial result. ``http_client`` is injected for tests; when
    omitted, a client is created for the call.
    """
    if http_client is not None:
        return await _run_scan(endpoint_url, bearer, http_client)
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await _run_scan(endpoint_url, bearer, client)


async def _run_scan(endpoint_url: str, bearer: Optional[str], client: httpx.AsyncClient) -> list[McpTool]:
    """The handshake over one client. ``notifications/initialized`` is a notification (no id) the
    protocol requires before ``tools/list``; it answers with an empty 2xx and no frame is parsed
    from it."""
    protocol_version = _DEFAULT_PROTOCOL_VERSION
    session_id: Optional[str] = None

    async def _post(body: dict) -> httpx.Response:
        headers = _headers(protocol_version, session_id, bearer)
        try:
            return await client.post(endpoint_url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise McpScanError(f"MCP transport error: {type(exc).__name__}") from exc

    init_resp = await _post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": _DEFAULT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
        }
    )
    init_frame = _frame_or_raise(init_resp)
    negotiated = (init_frame.get("result") or {}).get("protocolVersion")
    if negotiated:
        protocol_version = negotiated
    session_id = init_resp.headers.get("mcp-session-id") or session_id

    notif_resp = await _post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    if not (200 <= notif_resp.status_code < 300):
        raise McpScanError(f"MCP notifications/initialized returned HTTP {notif_resp.status_code}")

    return await _list_tools(_post)
