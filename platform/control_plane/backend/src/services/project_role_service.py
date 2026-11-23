"""Per-project roles: which Entra principals hold which role on which project.

A role row grants one principal (a user or group object id) ``viewer``, ``maintainer`` or ``owner`` on
one project. This service is persistence plus write validation and never decides whether a caller may
act; ``services.project_resolver`` folds the rows into that answer and is the only seam routes call.

Rows are a third partition of the projects table, ``pk="project_role"`` with the composite
``sk="<project_id>#<principal_id>"``, so one project's rows are one ``begins_with`` range and
``grant`` is an upsert keyed on (project, principal). A ``#`` in either id would make the key
non-injective, so every entry point refuses one. Persistence is ``core.state_store.StateStore``; an
empty table name selects its memory double. The clock is injectable.

Reads degrade to ``[]`` on a store fault so the read routes keep serving; the reads whose answer is a
decision fail closed instead and raise ``ownership_unverified``: the last-owner guard (a write
decision), ``has_role_rows`` and ``list_all_strict`` (the ungoverned-project gate's inputs), and
``list_for_project_strict`` (the roster the console turns into a decision).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import ClassVar, List

from botocore.exceptions import ClientError

from core.errors import AppError, Outcome
from core.state_store import StateStore, VersionMismatch
from models.project_role import (
    PRINCIPAL_TYPES,
    ROLE_NAMES,
    ProjectRoleCreate,
    ProjectRoleRecord,
)

logger = logging.getLogger(__name__)

_PARTITION_KEY = "project_role"
_SK_SEPARATOR = "#"
_OWNER = "owner"


class ProjectRoleError(AppError):
    """A project-role operation was refused or failed.

    Every kind has a fixed detail, so the message never reaches a client; it names the refused rule
    for the log and for callers that branch on ``.kind``. ``insufficient`` is the project routes'
    gate refusal over the resolver's answer; this service, which never decides whether a caller may
    act, does not raise it. ``changed`` is a grant that lost to a concurrent write of its row.
    """

    domain: ClassVar[str] = "project_role"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "not_found": Outcome(404, "project role not found"),
        "insufficient": Outcome(403, "insufficient project role"),
        "validation": Outcome(400, "invalid project role"),
        "last_owner": Outcome(409, "project must keep at least one owner"),
        "ownership_unverified": Outcome(503, "could not verify project ownership"),
        "changed": Outcome(409, "Project role changed while this request ran; reload and retry"),
    }

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message, kind)


class ProjectRoleService:
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

    def list_for_project(self, project_id: str) -> List[ProjectRoleRecord]:
        """Every role row on one project; a store fault degrades to ``[]``."""
        self._validate_ids(project_id)
        return self._load_for_project(project_id)

    def list_for_project_strict(self, project_id: str) -> List[ProjectRoleRecord]:
        """The roster read: a store fault raises ``ownership_unverified`` instead of reading as an
        empty roster, because the console turns emptiness into a decision (a falsely empty roster
        would let a Viewer grant downgrade an existing owner through the upsert)."""
        self._validate_ids(project_id)
        try:
            return self._load_for_project_strict(project_id)
        except ClientError:
            logger.exception(
                "Could not read the role roster for project %s, so an empty list would be "
                "indistinguishable from an unreadable partition",
                project_id,
            )
            raise ProjectRoleError(
                "Could not verify project ownership", kind="ownership_unverified"
            ) from None

    def list_all(self) -> List[ProjectRoleRecord]:
        """Every role row across all projects, the resolver's cache load; a store fault degrades to ``[]``."""
        return self._load_all()

    def has_role_rows(self, project_id: str) -> bool:
        """Whether the project holds at least one role row, the ungoverned-project gate's input.

        A store fault raises ``ownership_unverified``: "the partition is empty" and "the partition is
        unreadable" are the same value to a degrading read but opposite answers to an authorization
        question, since "ungoverned" hands out the maintainer-level rule to every tenant member."""
        self._validate_ids(project_id)
        try:
            return bool(self._load_for_project_strict(project_id))
        except ClientError:
            logger.exception(
                "Could not read the role partition for project %s, so whether it is governed is unknown",
                project_id,
            )
            raise ProjectRoleError(
                "Could not verify project ownership", kind="ownership_unverified"
            ) from None

    def list_all_strict(self) -> List[ProjectRoleRecord]:
        """``list_all`` for an authorization decision: the list routes build the governed-project set
        from it, so a store fault raises ``ownership_unverified`` instead of reading as "nothing is
        governed"."""
        try:
            return self._load_all_strict()
        except ClientError:
            logger.exception(
                "Could not read the project_role partition, so which projects are governed is unknown"
            )
            raise ProjectRoleError(
                "Could not verify project ownership", kind="ownership_unverified"
            ) from None

    def grant(self, project_id: str, data: ProjectRoleCreate, granted_by: str) -> ProjectRoleRecord:
        """Upsert one principal's role on a project; re-granting changes the role, never duplicates.

        ``granted_by`` is the validated caller's identity, never a request-body value. The last-owner
        guard applies here as on ``revoke``: an upsert that downgrades the only remaining owner is
        refused with ``last_owner`` and persists nothing."""
        self._validate(data)
        self._validate_ids(project_id, data.principal_id)

        self._refuse_owner_downgrade(project_id, data)
        item = self._store.get((_PARTITION_KEY, _sort_key(project_id, data.principal_id)), consistent=True)
        record = ProjectRoleRecord(
            project_id=project_id,
            principal_id=data.principal_id,
            principal_type=data.principal_type,
            principal_display=data.principal_display,
            role=data.role,
            granted_by=granted_by,
            granted_at=self._now().isoformat(),
        )
        if item is None:
            self._create(record)
        else:
            record._row_version = self._from_item(item)._row_version
            self._save(record)
        return record

    def _refuse_owner_downgrade(self, project_id: str, data: ProjectRoleCreate) -> None:
        """Refuse an upsert that would take the project's owner count to zero.

        Only a downgrade of an existing ``owner`` row can, so granting ``owner``, granting a principal
        who holds no row or a non-owner row, and the first grant on a project pass untouched. One strict
        read drives both the existing-row lookup and the owner count: a swallowed store fault would read
        as "no owners to protect", the legitimate first-grant state, and let the downgrade through."""
        if data.role == _OWNER:
            return
        try:
            rows = self._load_for_project_strict(project_id)
        except ClientError:
            logger.exception(
                "Refusing the role change on project %s: the role partition is unreadable, "
                "so a last-owner downgrade cannot be ruled out",
                project_id,
            )
            raise ProjectRoleError(
                "Could not verify project ownership", kind="ownership_unverified"
            ) from None
        existing = next((r for r in rows if r.principal_id == data.principal_id), None)
        if existing is None or existing.role != _OWNER:
            return
        if sum(1 for r in rows if r.role == _OWNER) <= 1:
            raise ProjectRoleError("A project must keep at least one owner", kind="last_owner")

    def revoke(self, project_id: str, principal_id: str) -> None:
        """Remove one principal's role, refusing to strip a project of its last owner.

        One read drives both the not-found check and the owner count; the check and the delete are
        two store calls, so interleaved revokes stay last-writer-wins across requests."""
        self._validate_ids(project_id, principal_id)
        rows = self._load_for_project(project_id)
        record = next((r for r in rows if r.principal_id == principal_id), None)
        if record is None:
            raise ProjectRoleError("Unknown project role grant", kind="not_found")
        if record.role == _OWNER and sum(1 for r in rows if r.role == _OWNER) <= 1:
            raise ProjectRoleError("A project must keep at least one owner", kind="last_owner")
        self._delete(project_id, principal_id)

    def revoke_all(self, project_id: str) -> int:
        """Delete every role row on a project and return how many were removed.

        The project-deletion counterpart of ``revoke``, without the last-owner guard: once the project
        is gone there is nothing left to administer, and the guard would make the final owner row
        undeletable. Idempotent: a project with no rows returns 0. A store fault on the read raises
        ``ownership_unverified``, because silently deleting nothing is the orphaning this prevents:
        an orphan row would become a live grant again if its project id were ever reused, which
        uuid4 ids make unreachable today and a slug, import or restore path would not."""
        self._validate_ids(project_id)
        try:
            rows = self._load_for_project_strict(project_id)
        except ClientError:
            logger.exception(
                "Could not read the role rows for project %s, so they cannot be cleaned up",
                project_id,
            )
            raise ProjectRoleError(
                "Could not verify project ownership", kind="ownership_unverified"
            ) from None
        for record in rows:
            self._delete(project_id, record.principal_id)
        return len(rows)

    def owner_count(self, project_id: str) -> int:
        """How many ``owner`` rows a project has."""
        self._validate_ids(project_id)
        return sum(1 for r in self._load_for_project(project_id) if r.role == _OWNER)

    @staticmethod
    def _validate(data: ProjectRoleCreate) -> None:
        if data.role not in ROLE_NAMES:
            raise ProjectRoleError("role must be one of viewer, maintainer, owner", kind="validation")
        if data.principal_type not in PRINCIPAL_TYPES:
            raise ProjectRoleError("principal_type must be user or group", kind="validation")
        if not data.principal_id:
            raise ProjectRoleError("principal_id is required", kind="validation")

    @staticmethod
    def _validate_ids(project_id: str, principal_id: str | None = None) -> None:
        """Reject a ``#`` in either key component: ``("a", "b#c")`` and ``("a#b", "c")`` both encode to
        ``"a#b#c"``, so the second grant would overwrite the first and read back the wrong project."""
        if _SK_SEPARATOR in project_id:
            raise ProjectRoleError("project_id must not contain '#'", kind="validation")
        if principal_id is not None and _SK_SEPARATOR in principal_id:
            raise ProjectRoleError("principal_id must not contain '#'", kind="validation")

    def _get(self, project_id: str, principal_id: str) -> ProjectRoleRecord | None:
        sk = _sort_key(project_id, principal_id)
        try:
            item = self._store.get((_PARTITION_KEY, sk))
        except ClientError:
            logger.exception("Failed to fetch project role %s from DDB", sk)
            return None
        return self._from_item(item) if item else None

    def _load_for_project(self, project_id: str) -> List[ProjectRoleRecord]:
        try:
            return self._load_for_project_strict(project_id)
        except ClientError:
            logger.exception("Failed to load project roles for %s from DDB", project_id)
            return []

    def _load_for_project_strict(self, project_id: str) -> List[ProjectRoleRecord]:
        items = self._scan_partition(sk_prefix=f"{project_id}{_SK_SEPARATOR}")
        return [self._from_item(i) for i in items]

    def _load_all(self) -> List[ProjectRoleRecord]:
        try:
            return self._load_all_strict()
        except ClientError:
            logger.exception("Failed to load project roles from DDB")
            return []

    def _load_all_strict(self) -> List[ProjectRoleRecord]:
        return [self._from_item(i) for i in self._scan_partition()]

    def _create(self, record: ProjectRoleRecord) -> None:
        if not self._store.put_new({**self._to_item(record), "row_version": 1}):
            raise ProjectRoleError(
                f"Project role {_sort_key(record.project_id, record.principal_id)} already exists",
                kind="changed",
            )
        record._row_version = 1

    def _save(self, record: ProjectRoleRecord) -> None:
        written = (record._row_version or 0) + 1
        try:
            self._store.put_if_version(
                {**self._to_item(record), "row_version": written}, "row_version", record._row_version
            )
        except VersionMismatch:
            raise ProjectRoleError(
                f"Project role {_sort_key(record.project_id, record.principal_id)} changed", kind="changed"
            ) from None
        record._row_version = written

    def _delete(self, project_id: str, principal_id: str) -> None:
        self._store.delete((_PARTITION_KEY, _sort_key(project_id, principal_id)))

    def _scan_partition(self, sk_prefix: str = "") -> List[dict]:
        return self._store.query(_PARTITION_KEY, sk_prefix=sk_prefix)

    def _to_item(self, record: ProjectRoleRecord) -> dict:
        return {
            "pk": _PARTITION_KEY,
            "sk": _sort_key(record.project_id, record.principal_id),
            **json.loads(record.model_dump_json()),
        }

    def _from_item(self, item: dict) -> ProjectRoleRecord:
        clean = {k: v for k, v in item.items() if k not in ("pk", "sk")}
        record = ProjectRoleRecord.model_validate(clean)
        version = item.get("row_version")
        record._row_version = None if version is None else int(version)
        return record


def _sort_key(project_id: str, principal_id: str) -> str:
    return f"{project_id}{_SK_SEPARATOR}{principal_id}"
