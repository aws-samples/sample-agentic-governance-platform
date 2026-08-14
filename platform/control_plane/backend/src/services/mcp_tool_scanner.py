"""MCP "Streamable HTTP" wire-protocol tool scanner (Epic 7, Task T-SCAN).

At registration the backend talks to an MCP server in its own protocol and reads
back its declared tools so the catalog auto-populates (research §5). The MCP
"Streamable HTTP" transport is a tiny JSON-RPC handshake — ``initialize`` →
``notifications/initialized`` → ``tools/list`` — so we hand-roll it in ~50 lines
of async ``httpx`` rather than pull in the heavyweight ``mcp`` SDK (NOT installed;
E6's "prefer httpx" stance). Because MCP responses can come back as EITHER a JSON
body OR an SSE ``text/event-stream`` stream, ``_extract_jsonrpc`` reuses E6's
exact ``data:``-line buffering idiom (``api/routes/agents._extract_sse_text``).
Only the line-buffering is shared: MCP's own JSON-RPC framing is the authority
here, NOT the agent response contract (whose home is
``agent-templates/strands-agentcore/README.md`` → "Response contract").

The scanner is endpoint-agnostic: one function serves both a gateway's
``gatewayUrl`` and a standalone runtime/external MCP's ``endpoint_url``. It is
pure (no boto3, no FastAPI, no persistence) and fully unit-testable with a mocked
transport — like ``graph_service``, an ``http_client`` may be injected (the
caller then owns its lifecycle); otherwise one is created per call.

``bearer`` is OPTIONAL: the pre-lockdown registration scan (research §5.4 / the
design decision) passes ``None`` and sends no Authorization header.
"""

from __future__ import annotations

import json
from typing import Optional

import httpx
from pydantic import ValidationError

from models.mcp_server import McpTool

# The protocol version we advertise on ``initialize`` (research §5.1/§5.2). We
# switch the ``MCP-Protocol-Version`` header to the server's negotiated
# ``result.protocolVersion`` for every subsequent POST.
_DEFAULT_PROTOCOL_VERSION = "2025-06-18"

# Pagination guard — ``tools/list`` loops on ``result.nextCursor``; cap the page
# count so a misbehaving server can never spin us forever (research §5.1).
_MAX_PAGES = 50

_CLIENT_INFO = {"name": "agp-scanner", "version": "1.0.0"}


class McpScanError(Exception):
    """The MCP tool scan failed: a JSON-RPC ``error`` frame, a 4xx/5xx HTTP
    response, a transport/timeout failure, or an unparseable response. The caller
    (provisioning hook) treats this as best-effort/non-fatal and does NOT persist
    partial tools (research §5.4)."""


def _extract_jsonrpc(resp: httpx.Response) -> object:
    """Return the JSON-RPC frame from an MCP response, branching on Content-Type.

    ``text/event-stream`` → buffer the ``data:`` lines, ``json.loads`` each, and
    return the LAST frame carrying ``result``/``error`` (falling back to the last
    parseable payload) — the E6 SSE idiom (``_extract_sse_text``). Otherwise the
    body is a plain JSON object → ``resp.json()``. The frame keys below are
    JSON-RPC's; the agent-stream event shapes are a different contract, documented
    in ``agent-templates/strands-agentcore/README.md`` → "Response contract".

    May return a non-dict value (array, scalar) if the remote sends a malformed
    body; ``_frame_or_raise`` is the single chokepoint that rejects non-dicts.
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
        data = line[len("data:"):].strip()
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


async def scan_mcp_tools(
    endpoint_url: str,
    bearer: Optional[str] = None,
    *,
    timeout: float = 30.0,
    http_client: Optional[httpx.AsyncClient] = None,
) -> list[McpTool]:
    """Scan an MCP server and return its declared tools.

    Runs the handshake — ``initialize`` → ``notifications/initialized`` →
    ``tools/list`` (paginating on ``result.nextCursor``, capped at ~50 pages) — as
    POSTs to the SAME ``endpoint_url`` (research §5.1). The ``initialize`` result's
    ``protocolVersion`` is echoed on the ``MCP-Protocol-Version`` header from then
    on, and the ``Mcp-Session-Id`` response header (if any) is echoed on the later
    POSTs. ``Authorization: Bearer {bearer}`` is sent ONLY when ``bearer`` is
    truthy (the pre-lockdown scan passes ``None``).

    A JSON-RPC ``error`` frame, any 4xx/5xx, or a timeout raises ``McpScanError``
    (no partial persistence). ``http_client`` is injected for tests; when omitted,
    a client is created in an ``async with`` for the call.
    """
    if http_client is not None:
        return await _run_scan(endpoint_url, bearer, http_client)
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await _run_scan(endpoint_url, bearer, client)


async def _run_scan(
    endpoint_url: str, bearer: Optional[str], client: httpx.AsyncClient
) -> list[McpTool]:
    # Mutable header state: protocol_version flips to the negotiated value after
    # ``initialize``; session_id fills in if the server returns one.
    protocol_version = _DEFAULT_PROTOCOL_VERSION
    session_id: Optional[str] = None

    def _headers() -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",  # BOTH — server picks
            "MCP-Protocol-Version": protocol_version,
        }
        if bearer:
            h["Authorization"] = f"Bearer {bearer}"
        if session_id:
            h["Mcp-Session-Id"] = session_id
        return h

    async def _post(body: dict) -> httpx.Response:
        try:
            return await client.post(endpoint_url, headers=_headers(), json=body)
        except httpx.HTTPError as exc:  # TimeoutException is an HTTPError subclass
            raise McpScanError(f"MCP transport error: {type(exc).__name__}") from exc

    def _frame_or_raise(resp: httpx.Response) -> dict:
        if not (200 <= resp.status_code < 300):
            raise McpScanError(f"MCP server returned HTTP {resp.status_code}")
        frame = _extract_jsonrpc(resp)
        if not isinstance(frame, dict):
            raise McpScanError("MCP response was not a JSON-RPC object")
        if frame.get("error"):
            raise McpScanError(f"MCP JSON-RPC error: {frame['error']}")
        return frame

    # (1) initialize — capture the negotiated protocolVersion + the session id.
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

    # (2) notifications/initialized — a notification (no id): HTTP 202, no body.
    # Spec-MANDATORY before tools/list; do NOT parse a JSON-RPC frame from it.
    notif_resp = await _post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    if not (200 <= notif_resp.status_code < 300):
        raise McpScanError(
            f"MCP notifications/initialized returned HTTP {notif_resp.status_code}"
        )

    # (3) tools/list — loop on result.nextCursor, capped at _MAX_PAGES.
    tools: list[McpTool] = []
    cursor: Optional[str] = None
    for _ in range(_MAX_PAGES):
        params: dict = {"cursor": cursor} if cursor else {}
        list_resp = await _post(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": params}
        )
        result = _frame_or_raise(list_resp).get("result") or {}
        for t in result.get("tools") or []:
            try:
                tools.append(
                    McpTool(
                        name=t.get("name", ""),
                        description=t.get("description", "") or "",
                        input_schema=t.get("inputSchema") or {},
                    )
                )
            except ValidationError as exc:
                raise McpScanError(f"MCP tool entry failed validation: {exc}") from exc
        cursor = result.get("nextCursor")
        if not cursor:
            break

    return tools
