"""Pydantic models for per-project roles (E27/T1) — the project→principal→role edge.

Plain ``BaseModel``s — the ``tenant``/``project`` idiom. A **project role** grants one
Entra principal (a user OR a group, by object id) authority over ONE project:
``VIEWER`` < ``MAINTAINER`` < ``OWNER``. It is checked IN ADDITION to tenant
visibility, never instead of it — a foreign-tenant project is invisible before any
role logic runs.

:class:`ProjectRole` is an ``IntEnum`` so the resolver can compare with ``>=`` (an
OWNER satisfies a MAINTAINER requirement). The int is an implementation detail: the
API and DDB both store the lowercase *name* via :func:`role_name`, so a future
reordering of the enum cannot silently re-interpret stored rows.

Persistence + validation orchestration live in ``project_role_service`` — no boto3,
no I/O here (pure models).
"""

from __future__ import annotations

from enum import IntEnum

from pydantic import BaseModel


class ProjectRole(IntEnum):
    """Per-project authority. Ordered — comparisons are meaningful (``>=``)."""

    VIEWER = 0
    MAINTAINER = 1
    OWNER = 2


# Wire name → role. Also the allow-list the service validates a granted role against.
ROLE_NAMES: dict[str, ProjectRole] = {
    "viewer": ProjectRole.VIEWER,
    "maintainer": ProjectRole.MAINTAINER,
    "owner": ProjectRole.OWNER,
}

# The Entra principal kinds a role may be granted to (a group grant covers its members).
PRINCIPAL_TYPES: set[str] = {"user", "group"}


def role_name(role: ProjectRole) -> str:
    """Wire form — the API and DDB store the lowercase name, never the int."""
    return role.name.lower()


class ProjectRoleRecord(BaseModel):
    """Read-model — one project→principal→role edge. Carries only metadata, no credential."""

    project_id: str
    principal_id: str  # Entra oid (user OR group object id)
    principal_type: str  # "user" | "group"
    principal_display: str  # display name at grant time (best-effort, for UI)
    role: str  # one of ROLE_NAMES keys
    granted_by: str  # principal.oid or principal.email — never a body value
    granted_at: str  # ISO8601


class ProjectRoleCreate(BaseModel):
    """Write-only input — the POST /projects/{id}/roles body. ``granted_by`` is NOT here:
    the grantor comes from the validated ``Principal``, never from the request body."""

    principal_id: str
    principal_type: str  # "user" | "group"
    principal_display: str = ""
    role: str  # one of ROLE_NAMES keys
