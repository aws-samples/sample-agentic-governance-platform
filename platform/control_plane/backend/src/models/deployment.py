"""Pydantic model for Deployments: the append-only delivery record.

A ``Deployment`` is one attempt to put one image tag onto one stage of one repo. Nothing ever updates a
row; a new attempt is a new row. The promotion scalars on ``models.repository.Repository`` are
overwritten by every newer merge and cleared by every promote, so this record is the history that
build history lists and rollback rolls back to; the scalars stay as a denormalized "latest" cache.

``stage`` is free-form and never validated against a ``dev`` / ``prod`` literal: the stages come from
the tenant's config. ``actor_kind`` exists because a GitHub login and an Entra oid are two currencies
that must never be rendered as one. Timestamps are ISO-8601 UTC strings, the repo-wide convention.

No boto3, no I/O: persistence lives in ``project_service`` (``append_deployment``, ``list_deployments``).
"""

from __future__ import annotations

from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import Field

from models.base import WireModel


class DeploymentOutcome(str, Enum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def new_deployment_id() -> str:
    """A ``dep-<8 hex>`` id, minted per row: its last four characters are the sort key's
    collision-breaking suffix, and two rows sharing an id in the same millisecond would share a key,
    which in DynamoDB is an overwrite, a silently lost deployment."""
    return "dep-" + uuid4().hex[:8]


def deployment_seq_key(repo_id: str, stage: str, started_at: str, id: str) -> str:
    """The DynamoDB sort key ``{repo_id}#{stage}#{started_at}#{id[-4:]}``.

    The ``started_at`` prefix makes the partition time-sortable without a counter, so an append needs
    no read-modify-write and has no race; the id suffix removes same-millisecond collisions. Queried
    newest-first with ``begins_with(sk, f"{repo_id}#{stage}#")`` and ``ScanIndexForward=False``.
    """
    return f"{repo_id}#{stage}#{started_at}#{id[-4:]}"


class Deployment(WireModel):
    """Read model: one persisted delivery attempt. Append-only; carries no token, ever."""

    id: str = Field(description='"dep-<8 hex>"')
    repo_id: str
    agent_id: Optional[str] = Field(
        default=None, description="The agent delivered; every row written before MCP repos existed has one"
    )
    mcp_server_id: Optional[str] = Field(default=None, description="The MCP server delivered")
    stage: str = Field(description="Free-form; never validated against a dev or prod literal")
    seq_key: str = Field(description="The DynamoDB sort key, mirrored for round-tripping")
    image_tag: str
    source_sha: Optional[str] = None
    build_id: Optional[str] = Field(default=None, description="The CodeBuild id")
    outcome: DeploymentOutcome = DeploymentOutcome.STARTED
    actor: Optional[str] = Field(
        default=None, description="The OIDC-proven login, or the Entra oid of an AGP promote"
    )
    actor_kind: Optional[str] = Field(default=None, description="github or entra")
    started_at: str = Field(description="ISO-8601 UTC")
    completed_at: Optional[str] = None
    error: Optional[str] = Field(default=None, description="A safe short hint; never a token or a body")
