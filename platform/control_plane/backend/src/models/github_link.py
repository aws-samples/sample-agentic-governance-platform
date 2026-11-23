"""Pydantic models for the per-user GitHub account link.

A ``GitHubUserLink`` is the persisted record of "this human authorized AGP to act on GitHub as them":
the Entra ``oid`` on our side, GitHub's numeric user id on theirs. Platform actions taken through the
link are attributed to the human, not to AGP's App.

No token ever lives on a read model. ``GitHubUserLink`` carries only ``secret_arn``, a pointer to the
Secrets Manager secret holding ``{"access_token", "refresh_token"}``, plus non-secret metadata; the
read models' inherited ``extra="ignore"`` discards an undeclared key. There is deliberately no write-only
"link create" input: the authorization ``code`` and the PKCE ``code_verifier`` are exchanged
server-side and are not model fields. ``github_id`` is the stable join key, since a login can be renamed
and ``email`` is null unless the user published one; ``github_login`` is a display label refreshed on
verify.

DynamoDB serialization and Secrets Manager I/O live in ``services/github_user_link.py``. No boto3, no
I/O, no FastAPI here.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import Field

from models.base import WireModel


class LinkStatus(str, Enum):
    LINKED = "linked"
    REFRESHING = "refreshing"
    UNLINKED = "unlinked"


class GitHubUserLink(WireModel):
    """Read model: no token field, ever. ``refreshing`` means a refresh is claimed in flight (the
    claim-then-rotate guard)."""

    id: str
    principal_oid: str
    connection_id: str
    github_id: int = Field(description="GitHub's numeric user id, the stable join key")
    github_login: str = Field(description="Denormalized display label, refreshed on verify")
    status: LinkStatus
    secret_arn: str
    token_version: int = 0
    access_token_expires_at: Optional[str] = Field(
        default=None, description="ISO-8601 UTC; None means a non-expiring token"
    )
    refresh_token_expires_at: Optional[str] = None
    refresh_claimed_at: Optional[str] = None
    last_verified_at: Optional[str] = None
    created_at: str
    updated_at: str


class LinkStartRequest(WireModel):
    connection_id: str
    redirect_uri: str


class LinkStartResponse(WireModel):
    authorize_url: str
    state: str


class LinkCallbackRequest(WireModel):
    code: str
    state: str


class GitHubLinkStatus(WireModel):
    """One row of the UI's list."""

    connection_id: str
    org: str
    linked: bool
    status: str = Field(
        description="linked, refreshing or unlinked; a plain string so a new value cannot 500 the read"
    )
    github_login: Optional[str] = None
    last_verified_at: Optional[str] = None


class LinkableConnection(WireModel):
    connection_id: str
    org: str
    oauth_client_ready: bool


class GitHubLinkView(WireModel):
    links: list[GitHubLinkStatus] = Field(default_factory=list)
    connections: list[LinkableConnection] = Field(default_factory=list)
