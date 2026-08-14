"""Pydantic models for the per-user GitHub account link (E27B).

Plain ``BaseModel``s with ``str, Enum`` enums — the ``connection`` idiom. A
``GitHubUserLink`` is the persisted record of "this human authorized AGP to act on GitHub
as *them*": the Entra ``oid`` on our side, GitHub's numeric user id on theirs. Platform
actions taken through the link are attributed to the human, not to AGP's App.

SECRET SEAM: no token ever lives on a read model. ``GitHubUserLink`` carries only
``secret_arn`` — a POINTER to the Secrets Manager secret holding
``{"access_token", "refresh_token"}`` — plus NON-secret metadata (expiries, a
``token_version`` counter, the denormalized login). The tokens themselves never cross
this boundary, exactly as ``Connection`` exposes ``secret_arn``/``has_secret`` and never
the credential. There is deliberately no write-only "link create" input either: the
authorization ``code`` and the PKCE ``code_verifier`` are exchanged server-side and are
not model fields.

JOIN KEY: ``github_id`` is GitHub's numeric user id and is the STABLE identifier — a
login can be renamed, and ``email`` is ``null`` unless the user published one, so neither
is usable as a key. ``github_login`` is a display label only, refreshed on verify.

DynamoDB serialization and Secrets Manager I/O live in ``services/github_user_link.py``,
NOT here. No boto3, no I/O, no FastAPI — pure models.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class LinkStatus(str, Enum):
    LINKED = "linked"
    REFRESHING = "refreshing"  # a refresh is claimed in flight (claim-then-rotate guard)
    UNLINKED = "unlinked"


class GitHubUserLink(BaseModel):          # read model — NO token field, ever
    id: str
    principal_oid: str
    connection_id: str
    github_id: int                        # GitHub's numeric id — the STABLE join key
    github_login: str                     # denormalized display label; refreshed on verify
    status: LinkStatus
    secret_arn: str
    token_version: int = 0
    access_token_expires_at: Optional[str] = None    # ISO-8601 UTC; None ⇒ non-expiring token
    refresh_token_expires_at: Optional[str] = None
    refresh_claimed_at: Optional[str] = None
    last_verified_at: Optional[str] = None
    created_at: str
    updated_at: str


class LinkStartRequest(BaseModel):
    connection_id: str
    redirect_uri: str


class LinkStartResponse(BaseModel):
    authorize_url: str
    state: str


class LinkCallbackRequest(BaseModel):
    code: str
    state: str


class GitHubLinkStatus(BaseModel):        # one row of the UI's list
    connection_id: str
    org: str
    linked: bool
    status: str                           # "linked" | "refreshing" | "unlinked"
    github_login: Optional[str] = None
    last_verified_at: Optional[str] = None


class LinkableConnection(BaseModel):
    connection_id: str
    org: str
    oauth_client_ready: bool


class GitHubLinkView(BaseModel):
    links: list[GitHubLinkStatus] = Field(default_factory=list)
    connections: list[LinkableConnection] = Field(default_factory=list)
