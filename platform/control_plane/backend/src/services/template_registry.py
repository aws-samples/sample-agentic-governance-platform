"""The AGP-owned template catalog: the record model and its store.

Template discovery is a read of AGP's own store, the same on every git provider. A record is a
pointer to a template, never its contents: ``source_url`` may name a repository outside the
connected org, the structural ``source_org``/``source_repo`` pair is what the materialize path
dereferences, and ``infra_template_id`` links the record into the infra catalog that deploys it.

Persistence is one partition of the projects table, ``pk="template"`` with the composite
``sk="<connection_id>#<template_id>"``, so one connection's catalog is a contiguous
``begins_with`` range. ``#`` is refused in both key halves because the composite key is only
injective while neither half contains the separator. ``template_id`` is derived from the name, so
one name is one catalog entry. Reads are strict: a store fault raises :class:`TemplateRegistryError`
rather than reading as an empty catalog. The module holds no secret.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import ClassVar, List, Literal, Optional

from botocore.exceptions import ClientError
from pydantic import Field, PrivateAttr

from core.errors import AppError, Outcome
from core.state_store import StateStore
from models.base import WireModel

logger = logging.getLogger(__name__)

_PARTITION_KEY = "template"
_SK_SEPARATOR = "#"


class TemplateRegistryError(AppError):
    """The template catalog could not be read or written: a store fault the caller cannot fix by
    retrying the same request, rendered as the retryable 503. The message is a fixed literal of
    this module, never a store value; the route shows its own fixed detail."""

    domain: ClassVar[str] = "template"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "store_error": Outcome(503, "could not read the template catalog"),
    }


class TemplateRegistryValidationError(TemplateRegistryError):
    """The caller's input is malformed: an empty or ``#``-bearing key component. Permanent, so
    it renders the 422 where the parent renders the retryable 503; a subclass so a caller that
    only cares that the registry refused still catches the parent."""

    kinds: ClassVar[Mapping[str, Outcome]] = {
        "validation": Outcome(422, "invalid template or connection identifier"),
    }


class TemplateRecord(WireModel):
    """One catalog entry: a pointer to a template, never its contents.

    ``framework``, ``aws_services`` and ``tags`` are catalog metadata the console renders; they
    describe the entry, not the template's contents.
    """

    id: str
    name: str
    description: str = ""
    source_url: str = Field(
        default="",
        description="Any git URL, display only and never parsed; the template need not live in the org.",
    )
    version: str = "1"
    connection_id: str
    created_at: str
    created_by: str = ""
    updated_at: str = ""
    framework: str = ""
    aws_services: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    source_org: Optional[str] = Field(
        default=None,
        description=(
            "With source_repo, the repository the materialize path reads the template from. None means "
            "not dereferenceable, so the on-disk seed is used instead; never backfilled from source_url."
        ),
    )
    source_repo: Optional[str] = Field(
        default=None,
        description="The repository half of the structural source, written with source_org or not at all.",
    )
    infra_template_id: str = Field(
        default="agentcore-runtime",
        description=(
            "The infra template that deploys this template's repo. Pinned rather than optional: every "
            "record written before the infra catalog existed was an AgentCore runtime container template."
        ),
    )
    preview: bool = Field(
        default=False, description="Registered and materializable, but not yet offered as a default choice."
    )
    artifact_kind: Literal["agent", "mcp_server"] = Field(
        default="agent",
        description="What this template's repo becomes; read from template.json's artifact_kind.",
    )
    _row_version: int | None = PrivateAttr(default=None)


def template_id_for(name: str) -> str:
    """The template id is the name; this function is the check that the name is a legal key half.

    Rollout names are on-disk directory names that never pass the service layer's name rule, so the
    check runs here.
    """
    if not name:
        raise TemplateRegistryValidationError("template name is required")
    if _SK_SEPARATOR in name:
        raise TemplateRegistryValidationError("template name must not contain '#'")
    return name


class TemplateRegistry:
    """The :class:`TemplateRecord` store: the ``pk="template"`` partition of the projects table,
    written through :class:`StateStore` (in memory when ``table_name`` is empty)."""

    def __init__(
        self,
        table_name: str = "",
        region: str | None = None,
        *,
        now=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.table_name = table_name
        self.region = region
        self._now = now
        self._store = StateStore(table_name, region=region)

    def list_for_connection(self, connection_id: str) -> List[TemplateRecord]:
        """One connection's catalog, a ``begins_with`` range read. A store failure raises rather
        than returning ``[]``: an empty catalog and an unreadable one are opposite answers."""
        self._validate_ids(connection_id)
        prefix = f"{connection_id}{_SK_SEPARATOR}"
        return [self._from_item(i) for i in self._query(sk_prefix=prefix)]

    def get(
        self, connection_id: str, template_id: str, *, consistent: bool = False
    ) -> Optional[TemplateRecord]:
        """One catalog entry, or ``None``. A store failure raises, so a not-found is never an
        unreadable partition in disguise."""
        self._validate_ids(connection_id, template_id)
        sk = _sort_key(connection_id, template_id)
        try:
            item = self._store.get((_PARTITION_KEY, sk), consistent=consistent)
        except ClientError:
            logger.exception("[template-registry] failed to read template %s", sk)
            raise TemplateRegistryError("Could not read the template catalog") from None
        return self._from_item(item) if item else None

    def create(self, record: TemplateRecord) -> bool:
        """Write ``record`` as a new catalog entry at ``row_version`` 1, stamping its ``updated_at``,
        and return whether it landed: ``False`` means the (connection, id) is already registered and
        nothing was written."""
        self._validate_ids(record.connection_id, record.id)
        record.updated_at = self._now().isoformat()
        try:
            created = self._store.put_new({**self._to_item(record), "row_version": 1})
        except ClientError:
            logger.exception(
                "[template-registry] failed to write template %s",
                _sort_key(record.connection_id, record.id),
            )
            raise TemplateRegistryError("Could not write the template catalog") from None
        if created:
            record._row_version = 1
        return created

    def save(self, record: TemplateRecord) -> TemplateRecord:
        """Replace the catalog entry ``record`` was read from, stamping ``updated_at``, and return it
        at the ``row_version`` it was written at. The write lands only while the row is at the
        ``row_version`` the record carries, ``None`` matching only a row that carries none; an absent
        row or one another write moved raises ``core.state_store.VersionMismatch`` and writes
        nothing."""
        self._validate_ids(record.connection_id, record.id)
        stamped = record.model_copy(update={"updated_at": self._now().isoformat()})
        expected = stamped._row_version
        written = (expected or 0) + 1
        try:
            self._store.put_if_version(
                {**self._to_item(stamped), "row_version": written}, "row_version", expected
            )
        except ClientError:
            logger.exception(
                "[template-registry] failed to write template %s",
                _sort_key(stamped.connection_id, stamped.id),
            )
            raise TemplateRegistryError("Could not write the template catalog") from None
        stamped._row_version = written
        return stamped

    def delete(self, connection_id: str, template_id: str) -> None:
        """Deregister one catalog entry: the pointer only, never the repository behind
        ``source_url``. Idempotent: already gone is success."""
        self._validate_ids(connection_id, template_id)
        sk = _sort_key(connection_id, template_id)
        try:
            self._store.delete((_PARTITION_KEY, sk))
        except ClientError:
            logger.exception("[template-registry] failed to delete template %s", sk)
            raise TemplateRegistryError("Could not update the template catalog") from None

    @staticmethod
    def _validate_ids(connection_id: str, template_id: Optional[str] = None) -> None:
        """Refuse an empty or ``#``-bearing key half: ``("a", "b#c")`` and ``("a#b", "c")`` both
        encode to ``"a#b#c"``, so the second write would silently overwrite the first. Raises the
        validation subclass, never the store-fault parent."""
        if not connection_id:
            raise TemplateRegistryValidationError("connection_id is required")
        if _SK_SEPARATOR in connection_id:
            raise TemplateRegistryValidationError("connection_id must not contain '#'")
        if template_id is not None:
            if not template_id:
                raise TemplateRegistryValidationError("template id is required")
            if _SK_SEPARATOR in template_id:
                raise TemplateRegistryValidationError("template id must not contain '#'")

    def _query(self, *, sk_prefix: str) -> List[dict]:
        try:
            return self._store.query(_PARTITION_KEY, sk_prefix=sk_prefix)
        except ClientError:
            logger.exception(
                "[template-registry] failed to list the template partition for %s",
                sk_prefix,
            )
            raise TemplateRegistryError("Could not read the template catalog") from None

    @staticmethod
    def _to_item(record: TemplateRecord) -> dict:
        return {
            "pk": _PARTITION_KEY,
            "sk": _sort_key(record.connection_id, record.id),
            **json.loads(record.model_dump_json()),
        }

    @staticmethod
    def _from_item(item: dict) -> TemplateRecord:
        clean = {k: v for k, v in item.items() if k not in ("pk", "sk")}
        record = TemplateRecord.model_validate(clean)
        version = item.get("row_version")
        record._row_version = None if version is None else int(version)
        return record


def _sort_key(connection_id: str, template_id: str) -> str:
    return f"{connection_id}{_SK_SEPARATOR}{template_id}"
