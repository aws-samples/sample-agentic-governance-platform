"""Pydantic models for Projects: containers of template-materialized repositories.

A ``Project`` is an empty container scoped to one org (``connection_id``) and one tenant; it holds no
template, repo, agent or status of its own, those live on ``models.repository.Repository`` records
materialized under ``POST /projects/{id}/repos``. ``validate_agent_config`` and ``AGENT_NAME_RE`` live
here because repo-create enforces the two governance-critical ``agent_config`` fields through them.

No boto3, no I/O: persistence and orchestration live in ``services.project_service``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, ClassVar, Dict, List, Optional

from pydantic import Field

from core.errors import AppError, Outcome
from models.base import WireModel
from models.repository import Repository

AGENT_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,31}$")


class FrameworkMismatchError(AppError, ValueError):
    """The requested ``framework`` disagrees with the registered template's.

    Rendered 422 with the message verbatim: well-formed input that is unprocessable against this
    template and valid against the matching one, where a malformed ``agent_name`` (the other refusal
    ``validate_agent_config`` raises) is a 400. The message is this module's own sentence naming the
    two frameworks, never a store or provider value. Still a ``ValueError`` so a caller that catches the
    base keeps catching it.
    """

    domain: ClassVar[str] = "project"
    kinds: ClassVar[Mapping[str, Outcome]] = {"framework_mismatch": Outcome(422, None)}


def validate_agent_config(agent_config: Dict[str, Any], template_framework: str) -> None:
    """Validate the governance-critical ``agent_config`` fields.

    ``framework`` must equal ``template_framework``, the framework recorded on the registered template
    being materialized, else ``FrameworkMismatchError``; it is a relation against AGP's own catalog,
    not a list, because ``add_repo`` stamps the value onto the governed Agent record and a record
    disagreeing with its template would lie about its own contents. Both values are named in the
    message because the operator cannot see the template's. An empty ``template_framework`` means
    unknown (a legacy row, or the on-disk seed path) and folds; a store fault propagates at the call
    site instead.

    ``agent_name`` must match ``AGENT_NAME_RE`` else ``ValueError``: it is the stem of two stage-scoped,
    account-global AWS names (``{agent_name}_{stage}`` under the 48-character runtime cap and
    ``{agent_name}-{stage}-agentcore-exec`` under IAM's 64), so 32 leaves room for a 15-character stage
    under both; ``tests/test_project_repository_models.py`` pins the arithmetic and the frontend mirror.
    An unknown framework never becomes a hole in this check.
    """
    framework = agent_config.get("framework")
    if template_framework and framework != template_framework:
        raise FrameworkMismatchError(
            f"agent_config framework {framework!r} does not match the registered template's "
            f"framework {template_framework!r}"
        )
    agent_name = agent_config.get("agent_name") or ""
    if not AGENT_NAME_RE.match(agent_name):
        raise ValueError(
            f"invalid agent_config agent_name {agent_name!r}; must match {AGENT_NAME_RE.pattern}"
        )


class Project(WireModel):
    """Read model: an empty container scoped to one org and one tenant. Carries no token, ever.

    ``tenant_id`` is required; the project's repositories and their agents inherit it. Legacy stored
    records without the key hydrate as ``"default"`` in the service read path. ``trunk_branch`` is
    defaulted rather than required because every project stored before the field existed lacks the
    key.
    """

    id: str
    name: str
    connection_id: str
    tenant_id: str
    description: str = ""
    created_by: str
    created_at: str
    updated_at: str
    trunk_branch: str = Field(
        default="main",
        description=(
            "The one branch materialize creates and the repository builds from. Always main: the create "
            "API does not accept it and the shipped agent template's workflow builds on main only"
        ),
    )


class ProjectCreate(WireModel):
    """Write-only input: the ``POST /projects`` body. ``tenant_id`` is required. Deliberately without
    ``trunk_branch``: a field whose only legal value is its default is API noise, and a stale client
    that still sends one is dropped by pydantic's default ``extra="ignore"`` rather than refused."""

    name: str
    connection_id: str
    tenant_id: str
    description: str = ""


class ProjectDetail(WireModel):
    """Read model: the ``GET /projects/{id}`` response, a project with its repositories.

    ``effective_role`` and ``ungoverned`` are UI hints, never an authority. The browser cannot compute
    the caller's project role because a role may be granted to an Entra group, so without them the
    frontend either hides Grant and Promote from a group-derived owner or renders them optimistically
    and relies on a 403. Nothing server-side reads them to decide an action: ``may(ctx, ...)`` and the
    route gates in ``api/routes/projects.py`` remain the only enforcement. Both are computed per
    request in ``get_project`` and defaulted here, so every other producer reports "no hint".
    """

    project: Project
    repositories: List[Repository]
    effective_role: Optional[str] = Field(
        default=None,
        description=(
            "The caller's effective role on this project as the resolver computed it: viewer, "
            "maintainer or owner, None when they hold none; a global admin reads as owner"
        ),
    )
    ungoverned: bool = Field(
        default=False,
        description=(
            "Whether the ungoverned-project rule applies: the project holds no role rows, so any "
            "tenant-visible caller acts as maintainer for maintainer-level verbs. Separate from "
            "effective_role because the strict gates (role CRUD, promote) ignore this rule; False "
            "whenever the governed bit could not be established"
        ),
    )
