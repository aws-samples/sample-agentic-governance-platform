"""The AGP-owned template registry (E28B/T2, design D-B4) — model + store.

E22 made the template catalog stateless by asking GitHub "which of your repos have
``is_template == true``?". That flag exists ONLY on GitHub (GitLab paywalls it, Bitbucket
and Azure DevOps have no template concept at all), so *discovery* could not port. This
module is the replacement: the platform stores the catalog and discovery becomes a read of
AGP's own store, identical on every provider.

A record is a **POINTER, never a payload** — no template contents are stored (tenet 2,
light on data). ``source_url`` is any git URL, so a registered template may live outside
the connected org: another org, a public repo, a mirror. That is the thing ``is_template``
could not express.

Persistence is the DDB-or-local idiom cloned from ``project_role_service``: a FOURTH
partition in the EXISTING projects table (``settings.PROJECTS_TABLE_NAME`` — no new table,
no new env var), ``pk="template"`` with a COMPOSITE ``sk=f"{connection_id}#{template_id}"``
so one connection's catalog is a contiguous ``begins_with`` range (no GSI). ``#`` is
rejected in both key halves for the same reason it is there: the composite key is only
injective while neither half contains the separator, and a collision would silently
OVERWRITE a catalog entry.

``template_id`` is DERIVED from the template name rather than generated, which is what makes
``put`` an UPSERT: re-uploading a template of the same name produces a new *version* of one
catalog entry instead of a duplicate. The routes address templates by name, so a generated
id would need a name→id lookup that could disagree with itself.

Reads are STRICT — a DDB ``ClientError`` PROPAGATES as :class:`TemplateRegistryError`
rather than degrading to ``[]``. An empty catalog and an unreadable partition are the same
value to a degrading read but opposite answers to the operator: "you have no templates"
invites a re-upload or a re-rollout of templates that already exist.

No secrets — a record carries only catalog metadata — so there is NO Secrets Manager path,
and the connection token never reaches this module.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_PARTITION_KEY = "template"  # a FOURTH partition in the projects table

# The sk is composite so one connection's catalog is a contiguous begins_with range.
_SK_SEPARATOR = "#"


class TemplateRegistryError(Exception):
    """A registry operation failed for a reason the CALLER cannot fix by retrying — the
    store is unavailable. Carries a SAFE message, never a raw store value.

    Deliberately distinct from :class:`TemplateRegistryValidationError`: the two failures map
    to opposite HTTP semantics. A store fault is transient (retry may succeed → 503); malformed
    input is permanent (retrying an empty ``connection_id`` can never succeed → 422). One
    exception type for both told the console to retry a request that could not work.
    """


class TemplateRegistryValidationError(TemplateRegistryError):
    """The CALLER's input is malformed — an empty or ``#``-bearing key component.

    Subclasses :class:`TemplateRegistryError` so a caller that only cares "the registry
    refused" still catches it, while callers that map to HTTP can distinguish 422 from 503.
    Order matters at every call site: catch this BEFORE its parent.
    """


class TemplateRecord(BaseModel):
    """One catalog entry: a POINTER to a template, never its contents.

    The pinned D-B4 fields (``id``/``name``/``description``/``source_url``/``version``/
    ``connection_id``/``created_at``/``created_by``) plus the three CATALOG-METADATA fields
    the operator sets and the console renders (``framework``/``aws_services``/``tags``).
    Those three used to ride on the template repo's GitHub *topics*; topics are as
    unportable as ``is_template``, so they move onto the record. They describe the catalog
    entry, not the template's contents, so this stays a pointer.

    E28C (design D-C1) adds ``source_org`` + ``source_repo``: the STRUCTURAL source, which is
    what makes the pointer *dereferenceable*. E28C reads a template's bytes from its repo at
    use-time, and the seam's ``read_repo``/``read_tree`` take ``(org, repo)`` positionally —
    so the pair is STORED, never parsed back out of ``source_url``. It could not be: that
    field is contractually "any git URL" (see above), and a URL shape that decomposes for
    github.com does not for a self-hosted GitLab subgroup or an Azure DevOps project.
    """

    id: str
    name: str
    description: str = ""
    source_url: str = ""  # ANY git URL — the template need not live in the connected org
    version: str = "1"
    connection_id: str
    created_at: str
    created_by: str = ""
    updated_at: str = ""
    framework: str = ""
    aws_services: List[str] = []
    tags: List[str] = []

    # The structural source, written by rollout / adopt / upload-into-org. Both ``None`` means
    # NOT DEREFERENCEABLE (a pre-E28C record, or an upload pointing outside the org) and the
    # materialize path falls back to the on-disk seed, loudly. There is NO migration and no
    # backfill from ``source_url``: ``None`` is a truthful "AGP does not know which repo this
    # is", where a guessed org would name a repo that may not exist.
    source_org: Optional[str] = None
    source_repo: Optional[str] = None


def template_id_for(name: str) -> str:
    """Derive the template id from its name, ENFORCING that the result is a legal key half.

    The id is derived rather than generated, which is what makes :meth:`TemplateRegistry.put`
    an upsert: a re-upload versions ONE catalog entry instead of creating a second one with
    the same name. The routes address templates by name, so a generated id would require a
    name→id index that could disagree with the record.

    Because the derivation is identity, this function's job is the CHECK, not the transform:
    it is the one place that guarantees a name can be used as an ``sk`` component. That
    matters most on the rollout path, where names are on-disk directory names that never pass
    through the service layer's stricter ``_NAME_RE`` — an ``agent-templates/`` subdirectory
    called ``foo#bar`` would otherwise mint a key that collides across connections.
    """
    if not name:
        raise TemplateRegistryValidationError("template name is required")
    if _SK_SEPARATOR in name:
        raise TemplateRegistryValidationError("template name must not contain '#'")
    return name


class TemplateRegistry:
    """DDB-or-local store for :class:`TemplateRecord` (``pk="template"``)."""

    def __init__(
        self,
        table_name: str = "",
        region: str = "us-east-1",
        *,
        now=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.table_name = table_name
        self.region = region
        self._now = now

        self._ddb = None
        self._table = None
        if table_name:
            try:
                self._ddb = boto3.resource("dynamodb", region_name=region)
                self._table = self._ddb.Table(table_name)
            except Exception:  # pragma: no cover — degrade to the local fallback.
                self._table = None

        # Local fallback cache (no DDB table configured), keyed by the composite sk.
        self._local: Dict[str, TemplateRecord] = {}
        self._local_lock = threading.RLock()

    @property
    def _has_ddb(self) -> bool:
        return bool(self.table_name) and self._table is not None

    # ===================================================================== #
    # Public API
    # ===================================================================== #

    def list_for_connection(self, connection_id: str) -> List[TemplateRecord]:
        """One connection's catalog — a ``begins_with`` range read, not a partition scan.

        STRICT: a store failure raises rather than returning ``[]``, because an empty
        catalog and an unreadable one are opposite answers to the operator."""
        self._validate_ids(connection_id)
        prefix = f"{connection_id}{_SK_SEPARATOR}"
        if self._has_ddb:
            return [self._from_item(i) for i in self._query(sk_prefix=prefix)]
        with self._local_lock:
            return [
                r.model_copy(deep=True)
                for sk, r in sorted(self._local.items())
                if sk.startswith(prefix)
            ]

    def get(self, connection_id: str, template_id: str) -> Optional[TemplateRecord]:
        """One catalog entry, or None. STRICT on a store failure (see
        :meth:`list_for_connection`) — "absent" must not be a swallowed fault, or a
        not-found would be indistinguishable from an unreadable partition."""
        self._validate_ids(connection_id, template_id)
        sk = _sort_key(connection_id, template_id)
        if self._has_ddb:
            try:
                item = self._table.get_item(
                    Key={"pk": _PARTITION_KEY, "sk": sk}
                ).get("Item")
            except ClientError:
                logger.exception("[template-registry] failed to read template %s", sk)
                raise TemplateRegistryError(
                    "Could not read the template catalog"
                ) from None
            return self._from_item(item) if item else None
        with self._local_lock:
            record = self._local.get(sk)
            return record.model_copy(deep=True) if record else None

    def put(self, record: TemplateRecord) -> TemplateRecord:
        """UPSERT one catalog entry, stamping ``updated_at``. Re-putting the same
        (connection, id) replaces the entry — it never duplicates it."""
        self._validate_ids(record.connection_id, record.id)
        stamped = record.model_copy(update={"updated_at": self._now().isoformat()})
        if self._has_ddb:
            try:
                self._table.put_item(Item=self._to_item(stamped))
            except ClientError:
                logger.exception(
                    "[template-registry] failed to write template %s",
                    _sort_key(stamped.connection_id, stamped.id),
                )
                raise TemplateRegistryError(
                    "Could not write the template catalog"
                ) from None
            return stamped
        with self._local_lock:
            self._local[_sort_key(stamped.connection_id, stamped.id)] = (
                stamped.model_copy(deep=True)
            )
        return stamped

    def delete(self, connection_id: str, template_id: str) -> None:
        """DEREGISTER one catalog entry. Deletes the POINTER only — never the repo behind
        ``source_url``, which may be a public repo or a mirror AGP does not own.
        Idempotent: "already gone" is success (the E23 cascade idiom)."""
        self._validate_ids(connection_id, template_id)
        sk = _sort_key(connection_id, template_id)
        if self._has_ddb:
            try:
                self._table.delete_item(Key={"pk": _PARTITION_KEY, "sk": sk})
            except ClientError:
                logger.exception("[template-registry] failed to delete template %s", sk)
                raise TemplateRegistryError(
                    "Could not update the template catalog"
                ) from None
            return
        with self._local_lock:
            self._local.pop(sk, None)

    # ===================================================================== #
    # Internals
    # ===================================================================== #

    @staticmethod
    def _validate_ids(connection_id: str, template_id: Optional[str] = None) -> None:
        """Reject a ``#`` in either key half — the composite sk is only injective while
        neither contains the separator. ``("a", "b#c")`` and ``("a#b", "c")`` both encode to
        ``"a#b#c"``, so without this the second write would silently OVERWRITE the first and
        the survivor would read back the wrong ``connection_id``.

        Raises the VALIDATION subclass, never the store-fault parent: this is bad input, and a
        retry cannot fix it."""
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
        condition = Key("pk").eq(_PARTITION_KEY) & Key("sk").begins_with(sk_prefix)
        items: List[dict] = []
        kwargs = {"KeyConditionExpression": condition}
        try:
            while True:
                resp = self._table.query(**kwargs)
                items.extend(resp.get("Items", []))
                lek = resp.get("LastEvaluatedKey")
                if not lek:
                    break
                kwargs["ExclusiveStartKey"] = lek
        except ClientError:
            logger.exception(
                "[template-registry] failed to list the template partition for %s",
                sk_prefix,
            )
            raise TemplateRegistryError("Could not read the template catalog") from None
        return items

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
        return TemplateRecord.model_validate(clean)


def _sort_key(connection_id: str, template_id: str) -> str:
    """The composite sk — ``<connection_id>#<template_id>``."""
    return f"{connection_id}{_SK_SEPARATOR}{template_id}"
