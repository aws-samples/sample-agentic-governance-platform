"""Connection-scoped template catalog: a read of AGP's own ``template`` registry partition.

Discovery is a store read, so every provider answers it identically and a registered template may
live outside the connected org (``source_url`` is any git URL). ``list`` reads the connection's
records; ``upload`` creates the repository from the uploaded zip and then registers it; ``patch``
edits the record's catalog metadata; ``delete`` deregisters and leaves the repository in place,
since the pointer may name a public repo or a mirror AGP does not own (so a repo created by
``upload`` outlives its entry). The connection's token authorizes the delegated GitHub write only
and never reaches a log or an exception. :class:`GitHubTemplateError` renders through its table.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
import zlib
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import ClassVar, List, Literal, Optional, Tuple

from pydantic import Field

from core.errors import AppError, Outcome
from core.state_store import VersionMismatch
from models.base import WireModel
from services.connection_service import ConnectionError
from services.github_repo_errors import GitHubRepoError
from services.template_registry import (
    TemplateRecord,
    TemplateRegistryError,
    TemplateRegistryValidationError,
    template_id_for,
)

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class GitHubTemplateError(AppError):
    """A template catalog operation failed. The message is safe (never a token or a raw GitHub or
    store body); the client sees the fixed detail of the kind. ``store_error`` is retryable as
    sent: an unreadable partition is a transient fault, not an empty catalog. ``changed`` is a save
    that lost to a concurrent upload, patch or delete of the same entry, retryable after a reload."""

    domain: ClassVar[str] = "github_template"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "invalid_zip": Outcome(422, "Invalid template zip"),
        "invalid_input": Outcome(422, "Invalid template metadata"),
        "not_found": Outcome(404, "Template not found"),
        "github_error": Outcome(502, "GitHub template operation failed"),
        "store_error": Outcome(503, "Template catalog is temporarily unavailable"),
        "changed": Outcome(409, "GitHub template changed while this request ran; reload and retry"),
    }

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message, kind)


class TemplateView(WireModel):
    """Read model derived from a registry record: ``html_url`` is the record's ``source_url`` and
    ``updated_at`` the record's ``updated_at``."""

    name: str
    description: str = ""
    framework: str = ""
    aws_services: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    html_url: str = ""
    updated_at: str = ""
    infra_template_id: str = Field(
        default="agentcore-runtime",
        description="The infra template this template deploys through; the record's own field.",
    )
    platform: str = Field(
        default="aws",
        description=(
            "The infra template's platform, joined by the route; the default is the pre-catalog "
            "truth, an AgentCore container template on AWS."
        ),
    )
    build_kind: str = Field(
        default="container",
        description="The infra template's build kind, joined by the route like platform.",
    )
    preview: bool = False
    artifact_kind: Literal["agent", "mcp_server"] = Field(
        default="agent",
        description="What the template's repository becomes; the record's own field, never joined.",
    )


class GitHubTemplateService:
    """Registry-backed, connection-scoped template catalog. Collaborators are injected."""

    def __init__(
        self,
        *,
        github_repo_service,
        connection_service,
        template_registry,
        now=lambda: datetime.now(timezone.utc).isoformat(),
    ) -> None:
        self._gh = github_repo_service
        self._connections = connection_service
        self._registry = template_registry
        self._now = now

    def list_templates(self, connection_id: str) -> List[TemplateView]:
        """The connection's registered templates: a store read, no provider call. A store
        failure is ``store_error`` rather than an empty catalog, which would invite a re-upload."""
        return [self._view_from_record(r) for r in self._records(connection_id)]

    def upload_template(
        self,
        connection_id: str,
        *,
        zip_bytes: bytes,
        name: str,
        description: str,
        framework: str,
        aws_services: List[str],
        tags: List[str],
        created_by: str = "",
    ) -> TemplateView:
        """Create a repo from the uploaded zip, then register it in the catalog.

        ``name``, ``framework`` and the zip are validated up front. The repo is created before
        the record is written, so a failed provider write leaves no entry pointing at a repo that
        does not exist. A re-upload versions the one entry and keeps the original registrant and
        ``created_at``; ``created_by`` is the validated caller's identity and takes effect on a
        first registration only. The structural ``source_org``/``source_repo`` pair is recorded
        here because this verb knows the exact pair it created."""
        self._require_valid_name(name)
        framework = self._require_valid_framework(framework)
        self._require_valid_zip(zip_bytes)

        previous = self._find(connection_id, name, consistent=True)

        org, base_url, token = self._resolve(connection_id)
        try:
            html_url = self._gh.create_repo_from_zip(org, name, zip_bytes, token, base_url=base_url)
        except GitHubRepoError as exc:
            raise GitHubTemplateError(str(exc), kind="github_error") from None

        record = TemplateRecord(
            id=template_id_for(name),
            name=name,
            description=description,
            source_url=html_url,
            version=_next_version(previous),
            connection_id=connection_id,
            created_at=(previous.created_at if previous is not None else self._now()),
            created_by=(previous.created_by if previous is not None else created_by),
            framework=framework,
            aws_services=list(aws_services),
            tags=list(tags),
            source_org=org,
            source_repo=name,
        )
        if previous is None:
            return self._view_from_record(self._create(record))
        return self._view_from_record(self._save(previous.model_copy(update=record.model_dump())))

    def patch_template(
        self,
        connection_id: str,
        name: str,
        *,
        description: Optional[str] = None,
        aws_services: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        framework: Optional[str] = None,
    ) -> TemplateView:
        """Update a registered template's catalog metadata; unspecified fields keep their recorded
        value. Unknown template is ``not_found``; a bad ``name`` or ``framework`` is
        ``invalid_input``. No provider call.

        ``framework`` is validated only when supplied: an explicit ``""`` blanks a load-bearing
        field and is refused, while ``None`` means "not patching it", so a legacy row with a
        blank framework stays patchable in its other fields. The structural source pair is
        immutable here: there is no parameter that could repoint the record, because a moved
        template is a new registration an auditor can see."""
        self._require_valid_name(name)
        current = self._find(connection_id, name, consistent=True)
        if current is None:
            raise GitHubTemplateError("Unknown template", kind="not_found")

        if framework is not None:
            merged_framework = self._require_valid_framework(framework)
        else:
            merged_framework = current.framework

        updated = current.model_copy(
            update={
                "description": (description if description is not None else current.description),
                "framework": merged_framework,
                "aws_services": (
                    list(aws_services) if aws_services is not None else list(current.aws_services)
                ),
                "tags": list(tags) if tags is not None else list(current.tags),
            }
        )
        return self._view_from_record(self._save(updated))

    def delete_template(self, connection_id: str, name: str) -> None:
        """Deregister a template: remove the catalog entry and leave the repository alone. An
        unregistered name is ``not_found``; a malformed one is ``invalid_input``."""
        self._require_valid_name(name)
        if self._find(connection_id, name) is None:
            raise GitHubTemplateError("Unknown template", kind="not_found")
        try:
            self._registry.delete(connection_id, template_id_for(name))
        except TemplateRegistryValidationError as exc:
            raise GitHubTemplateError(str(exc), kind="invalid_input") from None
        except TemplateRegistryError as exc:
            raise GitHubTemplateError(str(exc), kind="store_error") from None

    def _view_from_record(self, record: TemplateRecord) -> TemplateView:
        return TemplateView(
            name=record.name,
            description=record.description,
            framework=record.framework,
            aws_services=list(record.aws_services),
            tags=list(record.tags),
            html_url=record.source_url,
            updated_at=record.updated_at or record.created_at,
            infra_template_id=record.infra_template_id,
            preview=record.preview,
            artifact_kind=record.artifact_kind,
        )

    def _records(self, connection_id: str) -> List[TemplateRecord]:
        try:
            return self._registry.list_for_connection(connection_id)
        except TemplateRegistryValidationError as exc:
            raise GitHubTemplateError(str(exc), kind="invalid_input") from None
        except TemplateRegistryError as exc:
            raise GitHubTemplateError(str(exc), kind="store_error") from None

    def _find(self, connection_id: str, name: str, *, consistent: bool = False) -> Optional[TemplateRecord]:
        """The current record for a named template, or None if it is not registered."""
        try:
            return self._registry.get(connection_id, template_id_for(name), consistent=consistent)
        except TemplateRegistryValidationError as exc:
            raise GitHubTemplateError(str(exc), kind="invalid_input") from None
        except TemplateRegistryError as exc:
            raise GitHubTemplateError(str(exc), kind="store_error") from None

    def _create(self, record: TemplateRecord) -> TemplateRecord:
        try:
            created = self._registry.create(record)
        except TemplateRegistryValidationError as exc:
            raise GitHubTemplateError(str(exc), kind="invalid_input") from None
        except TemplateRegistryError as exc:
            raise GitHubTemplateError(str(exc), kind="store_error") from None
        if not created:
            raise GitHubTemplateError("Template changed", kind="changed")
        return record

    def _save(self, record: TemplateRecord) -> TemplateRecord:
        try:
            return self._registry.save(record)
        except VersionMismatch:
            raise GitHubTemplateError("Template changed", kind="changed") from None
        except TemplateRegistryValidationError as exc:
            raise GitHubTemplateError(str(exc), kind="invalid_input") from None
        except TemplateRegistryError as exc:
            raise GitHubTemplateError(str(exc), kind="store_error") from None

    def _resolve(self, connection_id: str) -> Tuple[str, Optional[str], str]:
        """``(org, base_url, token)`` for a connection. An unknown connection is ``not_found``;
        any other connection failure is ``github_error``. The token is transient and never
        logged."""
        try:
            conn = self._connections.get_connection(connection_id)
            token = self._connections.get_bearer_token(connection_id)
        except ConnectionError as exc:
            mapped = "not_found" if exc.kind == "not_found" else "github_error"
            raise GitHubTemplateError(str(exc), kind=mapped) from None
        return conn.org, conn.base_url, token

    @staticmethod
    def _require_valid_name(name: str) -> None:
        if not _NAME_RE.match(name or ""):
            raise GitHubTemplateError("Template name must match ^[a-z][a-z0-9-]{0,63}$", kind="invalid_input")

    @staticmethod
    def _require_valid_framework(framework: str) -> str:
        """Check ``framework`` is non-empty and return it stripped; the caller stores the stripped
        value, so what is validated and what is recorded are one string.

        Free-form: the catalog is the authority on which frameworks exist, so it is not also
        their gate. Non-empty because ``validate_agent_config`` compares an add_repo request
        against the recorded value and treats a blank as unknown, which would disable the
        governance gate for every repo materialized from the row. Guards ``upload_template`` and
        an explicitly supplied ``patch_template`` value; the rollout's seeds take their framework
        from ``template.json`` and an adopted template registers with none."""
        stripped = (framework or "").strip()
        if not stripped:
            raise GitHubTemplateError("framework must not be empty", kind="invalid_input")
        return stripped

    @staticmethod
    def _require_valid_zip(zip_bytes: bytes) -> None:
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r"):
                pass
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError):
            raise GitHubTemplateError("Uploaded file is not a valid zip", kind="invalid_zip") from None


def _next_version(previous: Optional[TemplateRecord]) -> str:
    """Re-uploading a template versions its one catalog entry: ``"1"`` for a first upload, then
    ``"2"``, ``"3"``; a non-numeric recorded version is left alone rather than guessed at."""
    if previous is None:
        return "1"
    try:
        return str(int(previous.version) + 1)
    except (TypeError, ValueError):
        return previous.version
