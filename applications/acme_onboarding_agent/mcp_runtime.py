"""Stdlib-only helpers that read the MCP server list the platform writes into an agent's runtime
environment and name the discovered tools for the model.

``MCP_SERVERS`` is a JSON list of ``{"id", "audience", "gateway_url", "label"}`` entries, one per
granted MCP server; a runtime deployed before the list existed still carries one
``MCP_AUDIENCE``/``MCP_GATEWAY_URL`` pair. A value that cannot be read yields an empty list and the
agent runs prompt-only: nothing here raises out of an invoke.
"""

import hashlib
import json
import logging
from typing import Mapping

# Bedrock's ToolSpecification name is at most 64 characters:
# https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ToolSpecification.html
_MAX_TOOL_NAME = 64

logger = logging.getLogger(__name__)


def parse_mcp_servers(environ: Mapping[str, str]) -> list[dict]:
    """The agent's MCP servers: ``MCP_SERVERS`` when set, else the legacy single pair, else ``[]``.

    A ``MCP_SERVERS`` value that is not JSON or not a list logs a warning and yields ``[]``. The
    legacy pair counts only when both values are non-empty, which is how the platform neutralises
    them once it writes ``MCP_SERVERS``.
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
    """The model-facing name of an MCP tool: ``{label}__{name}``, unique across servers and
    traceable to its source; the gateway still receives its own tool name.

    A gateway advertises every tool as ``<target>___<tool>``, so a name over Bedrock's limit first
    drops the target, then is cut and suffixed with a short hash of the full name, stable across
    restarts.
    """
    full = f"{label}__{name}"
    if len(full) <= _MAX_TOOL_NAME:
        return full
    short = f"{label}__{name.split('___', 1)[-1]}"
    if len(short) <= _MAX_TOOL_NAME:
        return short
    digest = hashlib.sha1(full.encode(), usedforsecurity=False).hexdigest()[:6]
    return f"{short[: _MAX_TOOL_NAME - 7]}_{digest}"
