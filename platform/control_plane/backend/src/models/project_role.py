"""Pydantic models for per-project roles: the project to principal to role edge.

A project role grants one Entra principal (a user or a group, by object id) authority over one project:
``VIEWER`` < ``MAINTAINER`` < ``OWNER``. It is checked in addition to tenant visibility, never instead
of it; a foreign-tenant project is invisible before any role logic runs.

``ProjectRole`` is an ``IntEnum`` so the resolver can compare with ``>=`` (an owner satisfies a
maintainer requirement). The int is an implementation detail: the API and DynamoDB store the lowercase
name through ``role_name``, so a future reordering cannot re-interpret stored rows. ``ROLE_NAMES`` maps
the wire name to the role and is the allow-list the service validates a granted role against;
``PRINCIPAL_TYPES`` names the Entra principal kinds a role may be granted to, a group grant covering
its members.

Persistence and validation orchestration live in ``project_role_service``; no boto3, no I/O here.
"""

from __future__ import annotations

from enum import IntEnum

from pydantic import Field, PrivateAttr

from models.base import WireModel


class ProjectRole(IntEnum):
    """Per-project authority; ordered, so ``>=`` is meaningful."""

    VIEWER = 0
    MAINTAINER = 1
    OWNER = 2


ROLE_NAMES: dict[str, ProjectRole] = {
    "viewer": ProjectRole.VIEWER,
    "maintainer": ProjectRole.MAINTAINER,
    "owner": ProjectRole.OWNER,
}

PRINCIPAL_TYPES: set[str] = {"user", "group"}


def role_name(role: ProjectRole) -> str:
    """The wire form: the API and DynamoDB store the lowercase name, never the int."""
    return role.name.lower()


class ProjectRoleRecord(WireModel):
    """Read model: one project to principal to role edge. Metadata only, no credential."""

    project_id: str
    principal_id: str = Field(description="The Entra oid of a user or a group")
    principal_type: str = Field(description="user or group")
    principal_display: str = Field(description="The display name at grant time; best-effort, for the UI")
    role: str = Field(description="One of the ROLE_NAMES keys")
    granted_by: str = Field(description="principal.oid or principal.email; never a body value")
    granted_at: str = Field(description="ISO-8601")
    _row_version: int | None = PrivateAttr(default=None)


class ProjectRoleCreate(WireModel):
    """Write-only input: the ``POST /projects/{id}/roles`` body. ``granted_by`` is not here: the grantor
    comes from the validated ``Principal``, never from the request body."""

    principal_id: str
    principal_type: str = Field(description="user or group")
    principal_display: str = ""
    role: str = Field(description="One of the ROLE_NAMES keys")
