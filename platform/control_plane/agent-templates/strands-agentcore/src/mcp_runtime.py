"""Pure, stdlib-only multi-MCP runtime helpers.

The platform can wire ONE governed agent to MORE THAN ONE MCP server at a time, so it writes an
``MCP_SERVERS`` JSON list into this runtime's environment (produced by the control plane's
``build_runtime_mcp_env``). This module is the RUNTIME-SIDE consumer of exactly that contract, and
it is what makes a scaffolded agent able to use the MCP grants it is given — see ``src/main.py``
for the OBO token exchange and the ``MCPClient`` wiring that consume what this returns.

It is deliberately stdlib-only (``json``, ``logging``, ``typing`` — NO boto3/strands/OTEL) so the
parsing logic is unit-testable in isolation, without the heavy agent dependencies.

Degrade, never crash: a malformed ``MCP_SERVERS`` value or a missing env yields an empty list (the
agent then runs prompt-only) — it MUST NOT raise out of an invoke. An agent that crash-loops
because one env var is malformed is strictly worse than one that answers without tools.
"""

import json
import logging
from typing import Mapping

logger = logging.getLogger(__name__)


def parse_mcp_servers(environ: Mapping[str, str]) -> list[dict]:
    """Parse the agent's MCP server list from the runtime environment.

    Resolution order:
      1. ``MCP_SERVERS`` — a JSON list of ``{"id","audience","gateway_url","label"}`` written
         by the platform's ``build_runtime_mcp_env``. Used when present and non-empty.
      2. Legacy fallback — when ``MCP_SERVERS`` is absent/empty but BOTH legacy
         ``MCP_AUDIENCE`` and ``MCP_GATEWAY_URL`` are set, build a one-element list
         (``label="mcp"``) so a not-yet-redeployed agent keeps working.
      3. Otherwise ``[]`` (the agent runs prompt-only).

    DEGRADE, NEVER CRASH: a malformed ``MCP_SERVERS`` value (not JSON, or not a list) logs a
    warning and returns ``[]`` — it must never raise out of an invoke.

    Note: the platform NEUTRALIZES the legacy keys to ``""`` once it writes ``MCP_SERVERS``, so
    the legacy fallback only fires on a genuinely single-MCP-era runtime (both legacy keys
    non-empty and no ``MCP_SERVERS``) — empty-string legacy values are correctly treated as unset.
    """
    raw = environ.get("MCP_SERVERS")
    if raw:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError) as exc:
            logger.warning(
                "MCP_SERVERS is not valid JSON — running with no MCP servers (degrade): %r",
                exc,
            )
            return []
        if not isinstance(parsed, list):
            logger.warning(
                "MCP_SERVERS is not a JSON list (got %s) — running with no MCP servers (degrade).",
                type(parsed).__name__,
            )
            return []
        return parsed

    # Legacy single-MCP fallback. Both keys must be truthy.
    audience = environ.get("MCP_AUDIENCE")
    gateway_url = environ.get("MCP_GATEWAY_URL")
    if audience and gateway_url:
        return [
            {
                "id": "mcp",
                "audience": audience,
                "gateway_url": gateway_url,
                "label": "mcp",
            }
        ]

    return []


def namespace_tool_name(label: str, name: str) -> str:
    """Prefix an MCP tool's advertised name with its server label.

    Tools from every wired MCP are advertised to the model as ``{label}__{name}`` so names can
    never collide across servers and a tool call is traceable to its source MCP. The label is the
    deterministic slug the platform assigned to the MCP record.
    """
    return f"{label}__{name}"
