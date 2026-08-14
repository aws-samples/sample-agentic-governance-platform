"""Pydantic models for Projects — 1:N containers of template-materialized repos (E20/T8).

Plain ``BaseModel``s — the ``connection`` idiom. A ``Project`` is now an EMPTY
CONTAINER scoped to one org (``connection_id``): it holds no template, repo, agent,
or status of its own. Those live on :class:`~models.repository.Repository` records,
each materialized from a template under ``POST /projects/{id}/repos`` (T9/T10).

``ProjectCreate`` is the write-only input (name + org). :func:`validate_agent_config`
and the regex constants stay here because repo-create (T9) still enforces the two
governance-critical ``agent.config`` fields:
  - ``framework`` MUST be ``"strands"`` (the only supported scaffold), else ValueError;
  - ``agent_name`` MUST match ``^[a-zA-Z][a-zA-Z0-9_]{0,31}$`` — the STEM of two stage-scoped
    account-global AWS names, not a name itself (E28A/T1b; see AGENT_NAME_RE).

No boto3, no I/O — persistence + orchestration live in ``project_service``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from models.repository import Repository

# agent_name: a leading letter, then up to 31 more of [A-Za-z0-9_] (32 chars total).
#
# E28A/T1b (C-A3) tightened this from 48 to 32, and the 32 is ARITHMETIC, not taste.
# `agent_name` is not a resource name — it is the STEM the runtime Terraform module derives TWO
# stage-scoped, ACCOUNT-GLOBAL names from (modules/agentcore_runtime/main.tf `locals`):
#
#   agent_runtime_name = "{agent_name}_{stage}"                 AWS cap 48, underscores only
#   exec role name     = "{agent_name}-{stage}-agentcore-exec"  IAM cap 64
#
# At 48 BOTH overflow: the role name was already 48 + len("-agentcore-exec") = 63 of 64 before any
# stage suffix existed. 32 leaves room for a 15-char stage under both ceilings (32+1+15 = 48;
# 32+1+15+15 = 63). TRUNCATING to fit was rejected — two long names sharing a prefix would collide
# silently on an account-global name, which is the very class of bug (finding #9) that stage-scoping
# fixes. Max name observed live is 17 chars, so nothing needs migrating.
#
# Mirrored in frontend AddRepoModal.tsx (a stale mirror lets the modal call a name valid that the
# API then 502s) and in the module's own `var.agent_name` validation.
AGENT_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,31}$")

# The only framework the T1 scaffold (and therefore a project) supports.
SUPPORTED_FRAMEWORK = "strands"

def validate_agent_config(agent_config: Dict[str, Any]) -> None:
    """Validate the governance-critical ``agent.config`` fields; raise ValueError on a miss.

    Enforced (the rest of the schema is the scaffold's concern):
      - ``framework`` == ``"strands"`` (the only supported scaffold);
      - ``agent_name`` matches :data:`AGENT_NAME_RE`.
    """
    framework = agent_config.get("framework")
    if framework != SUPPORTED_FRAMEWORK:
        raise ValueError(
            f"unsupported agent_config framework {framework!r}; only "
            f"{SUPPORTED_FRAMEWORK!r} is supported"
        )
    agent_name = agent_config.get("agent_name") or ""
    if not AGENT_NAME_RE.match(agent_name):
        raise ValueError(
            f"invalid agent_config agent_name {agent_name!r}; must match "
            f"{AGENT_NAME_RE.pattern}"
        )


class Project(BaseModel):
    """Read-model — an empty container scoped to one org. Carries NO token, ever.

    ``tenant_id`` is REQUIRED (E24/T6): every project belongs to exactly one tenant,
    and its repositories (and their agents) inherit that tenant implicitly. Legacy
    stored records without the key hydrate as ``"default"`` in the service read path.
    """

    id: str
    name: str
    connection_id: str
    tenant_id: str
    description: str = ""
    created_by: str
    created_at: str
    updated_at: str
    # E28B/T3 (D-B5) — THE TRUNK IS PROJECT CONFIG, NOT A PLATFORM CONSTANT.
    #
    # Materialize used to hardcode two branch literals: it cut `dev` off `main` and made `dev` the
    # default. That encoded one team's methodology as a platform rule, and it also encoded a
    # PROVIDER convention — a provider whose default branch is not `main` had no way to say so.
    # E28B creates exactly ONE branch (a second branch is a second write, and a ref creation fires
    # a build), so the only branch name the platform needs is this one, and it belongs to the
    # project.
    #
    # Defaulted rather than required: every stored pre-E28B project lacks the key entirely, and a
    # required field would break reads of existing data (the ``tenant_id`` lesson — see
    # ``ProjectService._hydrate_project``). "main" is the value those projects effectively had, so
    # the default is the migration.
    #
    # E36/T15 (item 24, option B) — INTERNAL AND EFFECTIVELY CONSTANT. Nothing on the create API
    # sets this any more: ``ProjectCreate.trunk_branch`` and its two validators are deleted, so
    # every project gets this default. The field itself STAYS because the platform is genuinely
    # trunk-agnostic and 3 sites read the attribute (``add_repo`` and ``retry_materialize`` copying
    # it into their pending state, and the ``/builds/runtime`` prod-candidate gate; materialize's
    # push and ``_adopt_trunk`` read that pending copy rather than the field, and
    # ``github_repo_service`` only names it in a comment) — removing it would replace ONE default
    # with THREE ``"main"`` literals, which is worse. What was removed is the operator-facing
    # PROMISE: the shipped agent template's workflow pins ``on.push.branches: [main]``
    # (``build.yml``), so a project on any other trunk materializes, reports ``ready``, and then
    # never builds. A field whose only legal value is its default is not configuration. Re-expose it
    # on the API when the workflow's branch filter becomes template-generated — the plumbing below
    # it already works.
    trunk_branch: str = "main"


class ProjectCreate(BaseModel):
    """Write-only input — the POST /projects body. ``tenant_id`` is REQUIRED (E24/T6)."""

    name: str
    connection_id: str
    tenant_id: str
    description: str = ""
    # NO ``trunk_branch`` HERE, deliberately (E36/T15, item 24 option B). It was accepted and
    # validated on the wire, and the second validator then refused every value except ``"main"``
    # — the template's workflow pins ``on.push.branches: [main]``, so any other trunk produced a
    # repo that materialized, reported ``ready``, and never built. Declaring an operator field
    # whose only legal value is the default is API noise, so the field and both validators
    # (blank-guard + template-pin) are gone; ``Project.trunk_branch`` stays internal (see there).
    # A stale client that still sends ``trunk_branch`` is not an error: this is a plain
    # ``BaseModel``, so Pydantic's default ``extra="ignore"`` drops the key — a narrowing that
    # turns a former 422 into a no-op, never a 500.


class ProjectDetail(BaseModel):
    """Read-model — the GET /projects/{id} response: a project + its repositories.

    ``effective_role`` / ``ungoverned`` are **UI HINTS ONLY — never an authority** (E27/T11).
    They exist because the browser CANNOT compute the caller's project role: a role may be
    granted to an Entra GROUP, and no client-side signal evaluates group membership — so
    without them the frontend either hides Grant/Promote from a group-derived owner (the very
    path the design's groups-first §9 recommends) or renders them optimistically and relies on
    a 403. NOTHING server-side may ever read these to decide an action: ``may(ctx, ...)`` plus
    the route gates in ``api/routes/projects.py`` remain the ONLY enforcement, and a route that
    started trusting a field it just serialized would be trusting its own output.

    Both are computed per-request in ``get_project`` and defaulted here, so every OTHER
    producer of a ``ProjectDetail`` (the service read path, every other route that loads one)
    stays unchanged and simply reports "no hint".
    """

    project: Project
    repositories: List[Repository]
    # The caller's EFFECTIVE role on this project, as the resolver already computed it:
    # "viewer" | "maintainer" | "owner", or None when they hold none. A GLOBAL ADMIN is
    # reported as "owner" — they may do everything, mirroring ``may()``'s ``is_global``
    # short-circuit, so an admin does not lose their own buttons.
    effective_role: Optional[str] = None
    # Does the design-§3 ungoverned-project fallback apply here — i.e. the project holds NO
    # role rows, so any tenant-visible caller acts as MAINTAINER for maintainer-level verbs?
    # A SECOND field rather than folding "maintainer" into ``effective_role``, because the two
    # answer different questions and the strict gates (role CRUD, promote) deliberately IGNORE
    # this fallback: reporting a role the caller does not actually hold would both misstate
    # their standing on the roster and invite a consumer to expect an OWNER-gated action to
    # work. False whenever the governed bit could not be established (fail-closed, same stance
    # as the gate).
    ungoverned: bool = False
