"""Pydantic model for Deployments — the APPEND-ONLY delivery record (E28/T3, contract C1).

A ``Deployment`` is one attempt to put one image tag onto one stage of one repo. The
partition is append-only: nothing ever UPDATES a row, and a new attempt is a new row.

That is the whole point. Promotion state on :class:`models.repository.Repository`
(``last_promoted_*`` / ``prod_candidate_*``) is a set of SINGULAR scalars — "overwritten
wholesale by a newer merge, and cleared on a successful promote" — so a promote erased the
evidence of the previous one. With no record of what was deployed before, build history has
nothing to list and rollback has no artifact to roll back TO. This record is the history;
the scalars stay on ``Repository`` as a denormalized "latest" cache so a list row needs no
extra query.

``stage`` is FREE-FORM and is NEVER validated against a ``dev``/``prod`` literal (D8) — the
stages come from the tenant's config, and a hardcoded literal here would be exactly the
drift the contract forbids.

``actor_kind`` exists because a GitHub login and an Entra oid are two different currencies
and must never be silently rendered as one: a build promoted over the OIDC path is proven by
GitHub, an AGP promote is proven by Entra, and "who approved this" cannot mix them.

Timestamps are ISO-8601 UTC ``str`` (repo-wide convention — not ``datetime``).

No boto3, no I/O — persistence lives in ``project_service`` (``append_deployment`` /
``list_deployments``).
"""

from __future__ import annotations

from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel


class DeploymentOutcome(str, Enum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def new_deployment_id() -> str:
    """A ``dep-<8 hex>`` id (mirrors the ``mkt-``/``ten-`` prefixed id helpers).

    The last 4 characters are also the sk's collision-breaking suffix, so the id must be
    minted per row — two rows sharing an id in the same millisecond share a sort key, and in
    DynamoDB an equal key is an overwrite, i.e. a silently lost deployment."""
    return "dep-" + uuid4().hex[:8]


def deployment_seq_key(repo_id: str, stage: str, started_at: str, id: str) -> str:
    """The DDB sort key: ``{repo_id}#{stage}#{started_at}#{id[-4:]}``.

    The ``started_at`` prefix makes the partition time-sortable without a counter, so an
    append needs no read-modify-write and therefore has no race; the 4-char id suffix removes
    same-millisecond collisions. Queried newest-first with
    ``begins_with(sk, f"{repo_id}#{stage}#")`` + ``ScanIndexForward=False``."""
    return f"{repo_id}#{stage}#{started_at}#{id[-4:]}"


class Deployment(BaseModel):
    """Read-model — one persisted delivery attempt. Append-only; carries NO token, ever."""

    id: str  # "dep-<8 hex>"
    repo_id: str
    agent_id: str
    stage: str  # free-form; NEVER validated against a dev/prod literal (D8)
    seq_key: str  # the DDB sk, mirrored for round-tripping
    image_tag: str
    source_sha: Optional[str] = None
    build_id: Optional[str] = None  # CodeBuild id
    outcome: DeploymentOutcome = DeploymentOutcome.STARTED
    actor: Optional[str] = None  # OIDC-proven login, or Entra oid for an AGP promote
    actor_kind: Optional[str] = None  # "github" | "entra"
    started_at: str  # ISO-8601 UTC
    completed_at: Optional[str] = None
    error: Optional[str] = None  # SAFE short hint only — never a token or body
