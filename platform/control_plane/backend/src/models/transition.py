"""Both ``POST /{id}/transitions`` routes take the same two fields, so they import this model and the
OpenAPI document carries one ``TransitionRequest`` instead of a module-qualified name per route. The
action vocabulary and the non-empty reason are checked by the registry service the route calls; the
body only carries them.
"""

from __future__ import annotations

from pydantic import Field

from models.base import WireModel


class TransitionRequest(WireModel):
    """The admin lifecycle transition body, one schema for the agent and the MCP server routes."""

    action: str = Field(description="One of approve, reject, deprecate.")
    reason: str = Field(description="Required by the registry; recorded as the status reason.")
