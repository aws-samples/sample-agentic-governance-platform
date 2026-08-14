"""Connection-scoped template catalog service (E22/T2, re-based on the registry in E28B/T2).

E22 made this service stateless by asking GitHub "which of your repos have ``is_template ==
true``?". That question has no meaning on three of four providers (design §The template
lifecycle), so **discovery is now a read of AGP's own store** — the ``template`` partition
owned by ``template_registry``. Every provider answers it identically, and a registered
template may live outside the connected org (``source_url`` is any git URL).

What each operation does now:
  - **list** — read the connection's registry records. No GitHub call at all.
  - **upload** — create the repo from the uploaded zip (still a provider write) and then
    REGISTER it. The ``is_template`` flip is gone; nothing marks the repo on the provider.
  - **patch** — edit the record's catalog metadata. Formerly a ``set_repo_metadata`` call
    that re-encoded metadata into GitHub *topics*; topics are as unportable as
    ``is_template``, so ``framework``/``aws_services``/``tags`` live on the record and the
    topic round-trip is gone.
  - **delete** — DEREGISTER: remove the catalog entry, leaving the repository in place.
    ``source_url`` may point at a public repo or a mirror AGP does not own, so deleting the
    repo behind a pointer is both unsafe and provider-specific. Known consequence: a repo
    created by ``upload_template`` outlives its catalog entry.

:class:`TemplateView` keeps its E22 shape (the frontend contract does not move in this
task): ``html_url`` is served from the record's ``source_url`` and ``updated_at`` from the
record's ``updated_at``.

SECURITY: the connection's token is used only to authorize the delegated GitHub write. It is
never logged and never folded into an exception. ``GitHubTemplateError`` carries a SAFE
``.message`` + a ``.kind`` the route maps to a fixed HTTP status + fixed detail literal
(never ``str(exc)``); a wrapped ``GitHubRepoError`` (already SAFE) is re-wrapped by kind, and
a registry failure surfaces as ``store_error``.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
import zlib
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from pydantic import BaseModel

from models.project import SUPPORTED_FRAMEWORK
from services.github_repo_service import GitHubRepoError
from services.template_registry import (
    TemplateRecord,
    TemplateRegistryError,
    TemplateRegistryValidationError,
    template_id_for,
)

logger = logging.getLogger(__name__)

# Repo/template name: a leading lowercase letter, then up to 63 more of [a-z0-9-]
# (DNS-safe, repo-name-able — mirrors ops_template's OPS_TEMPLATE_ID_RE).
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class GitHubTemplateError(Exception):
    """A template catalog operation failed. Carries a SAFE message (never a token / raw
    GitHub or store body) and a ``.kind`` the route maps to a fixed HTTP status + fixed detail
    literal: ``{"invalid_zip","not_found","github_error","invalid_input","store_error"}``."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


class TemplateView(BaseModel):
    """Read-model — derived from a registry record. Shape UNCHANGED from E22 so the frontend
    contract does not move in this task: ``html_url`` comes from the record's ``source_url``,
    ``updated_at`` from the record's ``updated_at``."""

    name: str
    description: str = ""
    framework: str = ""
    aws_services: List[str] = []
    tags: List[str] = []
    html_url: str = ""
    updated_at: str = ""


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

    # ===================================================================== #
    # Public API
    # ===================================================================== #

    def list_templates(self, connection_id: str) -> List[TemplateView]:
        """List the connection's REGISTERED templates — a store read, no provider call.

        A store failure surfaces as ``store_error`` rather than an empty catalog: "you have
        no templates" would invite a re-upload of templates that already exist."""
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
        """Create a repo from the uploaded zip, then REGISTER it in the catalog.

        ``created_by`` is the validated caller's identity, passed by the route from its
        ``Principal`` — never taken from a form field. A re-upload keeps the ORIGINAL
        registrant (see below), so this only takes effect on a first registration.

        Validates ``name`` (repo-name-safe) + ``framework`` (== the one supported scaffold) +
        the zip up front (``invalid_input`` / ``invalid_zip``). Nothing marks the repo on the
        provider any more — the catalog entry is what makes it a template. The repo is created
        BEFORE the record is written so a failed provider write leaves no entry pointing at a
        repo that does not exist. A GitHub failure surfaces as ``github_error``, a store
        failure as ``store_error``."""
        self._require_valid_name(name)
        self._require_valid_framework(framework)
        self._require_valid_zip(zip_bytes)

        # Read the existing entry ONCE: a re-upload versions that entry and keeps its
        # original registrant, and two reads could disagree with each other.
        previous = self._find(connection_id, name)

        org, base_url, token = self._resolve(connection_id)
        try:
            html_url = self._gh.create_repo_from_zip(org, name, zip_bytes, token, base_url=base_url)
        except GitHubRepoError as exc:
            raise GitHubTemplateError(str(exc), kind="github_error") from None

        record = self._put(
            TemplateRecord(
                id=template_id_for(name),
                name=name,
                description=description,
                source_url=html_url,
                version=_next_version(previous),
                connection_id=connection_id,
                created_at=(
                    previous.created_at if previous is not None else self._now()
                ),
                created_by=(
                    previous.created_by if previous is not None else created_by
                ),
                framework=framework,
                aws_services=list(aws_services),
                tags=list(tags),
                # The STRUCTURAL source (E28C, design D-C1). This verb creates the repo in the
                # connection's org, so it KNOWS the exact pair — it records it rather than
                # leaving materialize to parse ``source_url``. Set fresh on a re-upload too:
                # the target org + name are unchanged, so the write is idempotent (unlike
                # ``created_at``/``created_by``, which belong to the FIRST registration).
                source_org=org,
                source_repo=name,
            )
        )
        return self._view_from_record(record)

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
        """Update a registered template's catalog metadata. Unspecified fields keep their
        CURRENT recorded value (so a tags-only patch keeps framework/aws). Unknown template →
        ``not_found``; a bad ``framework`` or ``name`` → ``invalid_input``. No provider call —
        the metadata lives on the record, not on GitHub topics.

        ``name`` is validated HERE, not left to the registry's key guard: it arrives as a raw
        URL path segment, and all three write verbs must agree on what a legal template name
        is (``upload_template`` always validated it; this one did not).

        The STRUCTURAL SOURCE IS IMMUTABLE here (E28C, design D-C1): there is deliberately no
        ``source_org``/``source_repo``/``source_url`` parameter, and the ``model_copy`` below
        carries the recorded pair through untouched. A moved template is a NEW registration,
        because repointing a record silently changes which bytes every future materialize
        ships — and an auditor asking "which repo did this agent come from" must not get an
        answer that a metadata edit could have rewritten. No guard is needed to enforce this;
        the signature is the enforcement, and a test pins it."""
        self._require_valid_name(name)
        current = self._find(connection_id, name)
        if current is None:
            raise GitHubTemplateError("Unknown template", kind="not_found")

        merged_framework = framework if framework is not None else current.framework
        if merged_framework:
            self._require_valid_framework(merged_framework)

        updated = current.model_copy(
            update={
                "description": (
                    description if description is not None else current.description
                ),
                "framework": merged_framework,
                "aws_services": (
                    list(aws_services)
                    if aws_services is not None
                    else list(current.aws_services)
                ),
                "tags": list(tags) if tags is not None else list(current.tags),
            }
        )
        return self._view_from_record(self._put(updated))

    def delete_template(self, connection_id: str, name: str) -> None:
        """DEREGISTER a template — remove the catalog entry, leave the repository alone.

        The repo is deliberately NOT deleted: ``source_url`` may point at a public repo, a
        mirror, or a repo in another org, and deleting what a pointer names is unsafe and
        provider-specific. Consequence, accepted: a repo created by ``upload_template``
        outlives its catalog entry. An unregistered name → ``not_found``; a malformed one →
        ``invalid_input`` (validated here for the same reason as ``patch_template``: it is a raw
        URL path segment, and all three write verbs must agree on what a legal name is)."""
        self._require_valid_name(name)
        if self._find(connection_id, name) is None:
            raise GitHubTemplateError("Unknown template", kind="not_found")
        try:
            self._registry.delete(connection_id, template_id_for(name))
        except TemplateRegistryValidationError as exc:
            raise GitHubTemplateError(str(exc), kind="invalid_input") from None
        except TemplateRegistryError as exc:
            raise GitHubTemplateError(str(exc), kind="store_error") from None

    # ===================================================================== #
    # Internals
    # ===================================================================== #

    def _view_from_record(self, record: TemplateRecord) -> TemplateView:
        return TemplateView(
            name=record.name,
            description=record.description,
            framework=record.framework,
            aws_services=list(record.aws_services),
            tags=list(record.tags),
            html_url=record.source_url,
            updated_at=record.updated_at or record.created_at,
        )

    # Every registry call funnels its exceptions through these three wrappers, and each maps
    # the VALIDATION subclass to ``invalid_input`` (→ 422) BEFORE its store-fault parent
    # (→ 503). Order is load-bearing: the subclass would otherwise be swallowed by the parent
    # clause and a malformed ``connection_id`` would tell the console to retry a request that
    # can never succeed. ``connection_id`` reaches every route unvalidated (and ``list`` has no
    # name to validate at all), so the registry's key guard is the boundary that catches it.

    def _records(self, connection_id: str) -> List[TemplateRecord]:
        try:
            return self._registry.list_for_connection(connection_id)
        except TemplateRegistryValidationError as exc:
            raise GitHubTemplateError(str(exc), kind="invalid_input") from None
        except TemplateRegistryError as exc:
            raise GitHubTemplateError(str(exc), kind="store_error") from None

    def _find(self, connection_id: str, name: str) -> Optional[TemplateRecord]:
        """The current record for a named template, or None if it is not registered."""
        try:
            return self._registry.get(connection_id, template_id_for(name))
        except TemplateRegistryValidationError as exc:
            raise GitHubTemplateError(str(exc), kind="invalid_input") from None
        except TemplateRegistryError as exc:
            raise GitHubTemplateError(str(exc), kind="store_error") from None

    def _put(self, record: TemplateRecord) -> TemplateRecord:
        try:
            return self._registry.put(record)
        except TemplateRegistryValidationError as exc:
            raise GitHubTemplateError(str(exc), kind="invalid_input") from None
        except TemplateRegistryError as exc:
            raise GitHubTemplateError(str(exc), kind="store_error") from None

    def _resolve(self, connection_id: str) -> Tuple[str, Optional[str], str]:
        """Resolve ``(org, base_url, token)`` for a connection. An unknown connection surfaces
        as ``not_found``; the token is transient and never logged."""
        try:
            conn = self._connections.get_connection(connection_id)
            token = self._connections.get_bearer_token(connection_id)
        except Exception as exc:  # ConnectionError carries a SAFE message + a .kind
            kind = getattr(exc, "kind", None)
            mapped = "not_found" if kind == "not_found" else "github_error"
            raise GitHubTemplateError(str(exc), kind=mapped) from None
        return conn.org, conn.base_url, token

    @staticmethod
    def _require_valid_name(name: str) -> None:
        if not _NAME_RE.match(name or ""):
            raise GitHubTemplateError(
                "Template name must match ^[a-z][a-z0-9-]{0,63}$", kind="invalid_input"
            )

    @staticmethod
    def _require_valid_framework(framework: str) -> None:
        if framework != SUPPORTED_FRAMEWORK:
            raise GitHubTemplateError(
                f"unsupported framework; only {SUPPORTED_FRAMEWORK!r} is supported",
                kind="invalid_input",
            )

    @staticmethod
    def _require_valid_zip(zip_bytes: bytes) -> None:
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r"):
                pass
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError):
            raise GitHubTemplateError("Uploaded file is not a valid zip", kind="invalid_zip") from None


def _next_version(previous: Optional[TemplateRecord]) -> str:
    """Re-uploading a template VERSIONS its one catalog entry (the id is derived from the
    name, so the write is an upsert) — ``"1"`` for a first upload, then ``"2"``, ``"3"``…
    A non-numeric recorded version is left alone rather than guessed at."""
    if previous is None:
        return "1"
    try:
        return str(int(previous.version) + 1)
    except (TypeError, ValueError):
        return previous.version
