"""Service layer for per-project roles (E27/T1) — the project→principal→role store.

A **project role** grants one Entra principal (user or group object id) authority over
ONE project (``viewer``/``maintainer``/``owner``). This service is pure persistence +
write validation; it answers "which principals hold which role on this project?" and
nothing else. It does NOT decide whether a caller may act — that is the resolver's job
(T2), which is the only seam routes are allowed to call.

Persistence is DDB-or-local, cloned verbatim from ``tenant_service`` / ``project_service``:
the ``_has_ddb`` guard, ``boto3.resource("dynamodb")`` + ``.Table(name)``, a local dict +
``threading.Lock``, serialize via ``{"pk":..., "sk":..., **json.loads(model.model_dump_json())}``
and deserialize via ``ProjectRoleRecord.model_validate(clean)``. Roles are a THIRD
partition in the EXISTING ``projects`` table (``settings.PROJECTS_TABLE_NAME`` — no new
table, no new env var), the same multi-partition-in-one-table shape
``connection_service`` uses for ``connection``/``conn_state``: ``pk="project_role"`` with
a COMPOSITE ``sk=f"{project_id}#{principal_id}"``, so one project's rows are a contiguous
``begins_with`` range — no GSI needed. The local fallback dict is keyed by that same
composite ``sk``. No secrets — a role row carries only metadata — so there is NO Secrets
Manager path.

Determinism: the clock (``now``) is injectable; tests pass a fixed clock. There is no id
source because the key is derived (project + principal), which is also what makes
``grant`` an UPSERT: re-granting a principal changes their role, it never duplicates.

``ProjectRoleError`` carries a SAFE ``.message`` + a ``.kind`` hint
(``{"not_found","validation","last_owner","ownership_unverified"}``) the route maps to a
fixed HTTP status + fixed detail literal (never ``str(exc)``).

Reads DEGRADE (log + ``[]``) on a DDB ``ClientError`` so the read routes keep serving. The
exceptions are the reads whose answer is a DECISION rather than data, which use the strict
loaders and fail CLOSED — an unreadable partition must never be mistaken for an empty one:
the last-owner guard (``_refuse_owner_downgrade``, a WRITE decision), the two
AUTHORIZATION reads ``has_role_rows`` / ``list_all_strict`` (E27/T4's governed-or-not gate
input), and ``list_for_project_strict`` — the ROSTER read, whose ``[]`` the browser turns
into a decision (E27/T11). All four raise ``kind="ownership_unverified"``.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, List

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from models.project_role import (
    PRINCIPAL_TYPES,
    ROLE_NAMES,
    ProjectRoleCreate,
    ProjectRoleRecord,
)

logger = logging.getLogger(__name__)

_PARTITION_KEY = "project_role"  # a THIRD partition in the projects table (never in a project list)

# The sk is composite so one project's grants are a contiguous begins_with range.
_SK_SEPARATOR = "#"

# The wire name of the role that must never be left empty on a project.
_OWNER = "owner"


class ProjectRoleError(Exception):
    """A project-role operation failed. Carries a SAFE message (never internal store detail) and a
    ``.kind`` hint the route maps to a fixed HTTP status + fixed detail literal:
    ``{"not_found","validation","last_owner","ownership_unverified"}``."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


class ProjectRoleService:
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
            except Exception:  # pragma: no cover — degrade to local fallback.
                self._table = None

        # Local fallback cache (used when no DDB table is configured), keyed by the composite sk.
        self._local: Dict[str, ProjectRoleRecord] = {}
        # Re-entrant so ``revoke`` can hold the lock across its check-AND-delete while the
        # persistence helpers it calls still take the lock themselves (see ``revoke``).
        self._local_lock = threading.RLock()

    # -- mode helper --------------------------------------------------------

    @property
    def _has_ddb(self) -> bool:
        return bool(self.table_name) and self._table is not None

    # ===================================================================== #
    # Public API
    # ===================================================================== #

    def list_for_project(self, project_id: str) -> List[ProjectRoleRecord]:
        """Every role row on ONE project (a begins_with range read, not a whole-partition scan)."""
        self._validate_ids(project_id)
        return self._load_for_project(project_id)

    def list_for_project_strict(self, project_id: str) -> List[ProjectRoleRecord]:
        """``list_for_project`` for a caller who must tell "empty" from "unreadable" —
        the ROSTER read the roles LIST route serves to the console (E27/T11).

        Same reasoning as ``has_role_rows`` / ``list_all_strict``, one layer up: the
        degrading sibling answers a store fault with HTTP 200 + ``[]``, and the browser
        cannot distinguish that from a genuinely empty roster. It then USES the emptiness
        as a decision — the Access tab's ``existingIds`` is what makes its Grant an ADD
        rather than an upsert-downgrade, so a falsely-empty roster lets "grant Viewer"
        silently demote a principal who already holds Owner. Unreadable ⇒ the roster is
        UNKNOWN ⇒ ``kind="ownership_unverified"`` (the same kind the last-owner guard
        raises for the same condition), which the route maps to the same retryable 503.

        The degrading ``list_for_project`` stays for callers that legitimately want
        degradation (the migration script's inventory read)."""
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
        """Every role row across all projects — the resolver's warm-load (T2)."""
        return self._load_all()

    def has_role_rows(self, project_id: str) -> bool:
        """Is this project GOVERNED — does it hold at least one role row? (E27/T4's gate input.)

        STRICT, unlike ``list_for_project``: a DDB ``ClientError`` does NOT degrade to "no
        rows". "The partition is empty" and "the partition is unreadable" are the same value
        to a degrading read, but they are OPPOSITE answers to an AUTHORIZATION question —
        "ungoverned" hands out the design-§3 fallback, so a swallowed fault would grant a
        role-less caller MAINTAINER on somebody else's governed project. Unreadable ⇒
        governance is UNKNOWN ⇒ ``kind="ownership_unverified"`` (the same kind
        ``_refuse_owner_downgrade`` raises for the same condition), which the route maps to a
        fail-CLOSED decision."""
        self._validate_ids(project_id)
        try:
            return bool(self._load_for_project_strict(project_id))
        except ClientError:
            logger.exception(
                "Could not read the role partition for project %s, so whether it is "
                "governed is unknown",
                project_id,
            )
            raise ProjectRoleError(
                "Could not verify project ownership", kind="ownership_unverified"
            ) from None

    def list_all_strict(self) -> List[ProjectRoleRecord]:
        """``list_all`` for an AUTHORIZATION decision — the whole-partition counterpart of
        ``has_role_rows``, used by the two LIST routes to build the governed-project set in
        one read.

        Same reasoning: a DDB ``ClientError`` must not read as "nothing is governed", so it
        surfaces as ``kind="ownership_unverified"`` instead of ``[]``. The degrading
        ``list_all`` stays for the resolver's warm-load, which fails closed on an empty
        roles map anyway."""
        try:
            return self._load_all_strict()
        except ClientError:
            logger.exception(
                "Could not read the project_role partition, so which projects are governed "
                "is unknown"
            )
            raise ProjectRoleError(
                "Could not verify project ownership", kind="ownership_unverified"
            ) from None

    def grant(
        self, project_id: str, data: ProjectRoleCreate, granted_by: str
    ) -> ProjectRoleRecord:
        """UPSERT one principal's role on a project — re-granting changes the role, never duplicates.

        ``granted_by`` is the validated caller's identity (oid/email); it is never taken
        from the request body.

        Carries the SAME last-owner guard as ``revoke``: an upsert that DOWNGRADES the only
        remaining ``owner`` row would strand the project with zero owners, which is the exact
        lockout the guard exists to prevent — reachable one HTTP verb over (``PUT`` instead of
        ``DELETE``) if only ``revoke`` were guarded. See ``_refuse_owner_downgrade``."""
        self._validate(data)
        self._validate_ids(project_id, data.principal_id)

        with self._local_lock:
            self._refuse_owner_downgrade(project_id, data)
            record = ProjectRoleRecord(
                project_id=project_id,
                principal_id=data.principal_id,
                principal_type=data.principal_type,
                principal_display=data.principal_display,
                role=data.role,
                granted_by=granted_by,
                granted_at=self._now().isoformat(),
            )
            self._save(record)
        return record

    def _refuse_owner_downgrade(self, project_id: str, data: ProjectRoleCreate) -> None:
        """Refuse an upsert that would take a project's owner count to ZERO, persisting NOTHING.

        Only a downgrade of an EXISTING ``owner`` row can do that, so three shapes pass
        untouched: granting ``owner`` (never lowers the count — including an idempotent
        re-grant of the only owner AS owner), granting a principal who holds no row or a
        non-owner row (the owner count is unchanged), and the FIRST grant on a project with
        no owners at all (otherwise a new project could never be seeded).

        ONE read of the partition drives both the existing-row lookup and the owner count so
        the two can never disagree; ``grant`` holds the re-entrant local lock across this
        check AND the write. DDB mode stays last-writer-wins across requests — same
        deliberate limitation as ``revoke``.

        The read is the STRICT loader, not the degrading one, because this is a WRITE
        decision: the read routes may safely treat a failed partition read as ``[]``, but
        the guard cannot — an empty list is also the legitimate FIRST-grant state, so a
        swallowed ``ClientError`` would read as "no owners to protect" and let the very
        downgrade this guard exists to refuse through. Unreadable ⇒ ownership is UNKNOWN ⇒
        refuse (``kind="ownership_unverified"`` → 503), which is what ``revoke`` already
        does by luck on the same blip."""
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
            raise ProjectRoleError(
                "A project must keep at least one owner", kind="last_owner"
            )

    def revoke(self, project_id: str, principal_id: str) -> None:
        """Remove one principal's role, refusing to strip a project of its LAST owner.

        A project with no owner is unadministerable (nobody could grant the role back), so
        revoking the only ``owner`` row raises ``kind="last_owner"`` and persists NOTHING.

        ONE read of the partition drives BOTH the not-found check and the owner count, so the
        two can never disagree, and the local lock is held across the whole check-AND-delete so
        interleaved revokes cannot each observe two owners and both pass the guard. DDB mode
        stays last-writer-wins across requests: closing that needs a conditional/transactional
        write, which is a deliberate follow-up, not something to fake with a lock here."""
        self._validate_ids(project_id, principal_id)
        with self._local_lock:
            rows = self._load_for_project(project_id)
            record = next((r for r in rows if r.principal_id == principal_id), None)
            if record is None:
                raise ProjectRoleError("Unknown project role grant", kind="not_found")
            if record.role == _OWNER and sum(1 for r in rows if r.role == _OWNER) <= 1:
                raise ProjectRoleError(
                    "A project must keep at least one owner", kind="last_owner"
                )
            self._delete(project_id, principal_id)

    def revoke_all(self, project_id: str) -> int:
        """Delete EVERY role row on a project. Returns how many were removed.

        The project-deletion counterpart of ``revoke`` — and deliberately NOT guarded by the
        last-owner rule. That guard exists to stop a project being stranded with nobody able to
        administer it; once the project itself is gone there is nothing left to administer, and
        applying the guard here would make the rows undeletable by construction (the final
        OWNER row could never be removed).

        Without this, ``pk="project_role"`` accumulates rows for projects that no longer
        exist. They are indistinguishable from live grants to ``list_all``/``list_all_strict``,
        so they inflate the partition the resolver scans on every cache miss (an authz hot
        path), and they would become LIVE grants again if a deleted id were ever reused —
        ``uuid4`` makes that unreachable today, but a slug/import/restore path would not.

        E23 cascade idiom: IDEMPOTENT and "already gone" = success. A project with no rows
        returns 0 rather than raising, so a retried or partially-completed delete is safe to
        run again. Read failures PROPAGATE (strict) — silently deleting nothing because the
        partition was briefly unreadable is the exact orphaning this exists to prevent, and the
        caller (which has already deleted the project) logs and continues."""
        self._validate_ids(project_id)
        with self._local_lock:
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
        """How many ``owner`` rows a project has — the last-owner guard's input."""
        self._validate_ids(project_id)
        return sum(1 for r in self._load_for_project(project_id) if r.role == _OWNER)

    # ===================================================================== #
    # Validation
    # ===================================================================== #

    @staticmethod
    def _validate(data: ProjectRoleCreate) -> None:
        if data.role not in ROLE_NAMES:
            raise ProjectRoleError(
                "role must be one of viewer, maintainer, owner", kind="validation"
            )
        if data.principal_type not in PRINCIPAL_TYPES:
            raise ProjectRoleError(
                "principal_type must be user or group", kind="validation"
            )
        if not data.principal_id:
            raise ProjectRoleError("principal_id is required", kind="validation")

    @staticmethod
    def _validate_ids(project_id: str, principal_id: str | None = None) -> None:
        """Reject a ``#`` in either key component — the composite sk is only injective while
        neither half contains the separator. ``("a", "b#c")`` and ``("a#b", "c")`` both encode
        to ``"a#b#c"``, so without this the second grant would silently OVERWRITE the first
        (a lost authorization grant) and the survivor would read back the wrong
        ``project_id``. Rejected at every entry point rather than by changing the pinned key
        format."""
        if _SK_SEPARATOR in project_id:
            raise ProjectRoleError(
                "project_id must not contain '#'", kind="validation"
            )
        if principal_id is not None and _SK_SEPARATOR in principal_id:
            raise ProjectRoleError(
                "principal_id must not contain '#'", kind="validation"
            )

    # ===================================================================== #
    # Persistence (DDB-or-local, mirror tenant_service.py)
    # ===================================================================== #

    def _get(self, project_id: str, principal_id: str) -> ProjectRoleRecord | None:
        sk = _sort_key(project_id, principal_id)
        if self._has_ddb:
            try:
                resp = self._table.get_item(Key={"pk": _PARTITION_KEY, "sk": sk})
                item = resp.get("Item")
                return self._from_item(item) if item else None
            except ClientError:
                logger.exception("Failed to fetch project role %s from DDB", sk)
                return None
        with self._local_lock:
            record = self._local.get(sk)
            return record.model_copy(deep=True) if record else None

    def _load_for_project(self, project_id: str) -> List[ProjectRoleRecord]:
        """READ path: degrades to ``[]`` on a DDB ``ClientError`` (the store's documented
        posture — the read routes keep serving). Write DECISIONS must use the strict variant."""
        try:
            return self._load_for_project_strict(project_id)
        except ClientError:
            logger.exception("Failed to load project roles for %s from DDB", project_id)
            return []

    def _load_for_project_strict(self, project_id: str) -> List[ProjectRoleRecord]:
        """Same read, but a DDB ``ClientError`` PROPAGATES so a caller whose decision depends
        on the rows can tell "partition empty" from "partition unreadable"."""
        prefix = f"{project_id}{_SK_SEPARATOR}"
        if self._has_ddb:
            items = self._scan_partition(sk_prefix=prefix)
            return [self._from_item(i) for i in items]
        with self._local_lock:
            return [
                r.model_copy(deep=True)
                for sk, r in self._local.items()
                if sk.startswith(prefix)
            ]

    def _load_all(self) -> List[ProjectRoleRecord]:
        """READ path: degrades to ``[]`` on a DDB ``ClientError`` (same posture as
        ``_load_for_project``). AUTHORIZATION decisions must use the strict variant."""
        try:
            return self._load_all_strict()
        except ClientError:
            logger.exception("Failed to load project roles from DDB")
            return []

    def _load_all_strict(self) -> List[ProjectRoleRecord]:
        """Same read, but a DDB ``ClientError`` PROPAGATES so a caller whose decision depends
        on the rows can tell "partition empty" from "partition unreadable"."""
        if self._has_ddb:
            items = self._scan_partition()
            return [self._from_item(i) for i in items]
        with self._local_lock:
            return [r.model_copy(deep=True) for r in self._local.values()]

    def _save(self, record: ProjectRoleRecord) -> None:
        if self._has_ddb:
            self._table.put_item(Item=self._to_item(record))
            return
        with self._local_lock:
            self._local[_sort_key(record.project_id, record.principal_id)] = (
                record.model_copy(deep=True)
            )

    def _delete(self, project_id: str, principal_id: str) -> None:
        sk = _sort_key(project_id, principal_id)
        if self._has_ddb:
            self._table.delete_item(Key={"pk": _PARTITION_KEY, "sk": sk})
            return
        with self._local_lock:
            self._local.pop(sk, None)

    def _scan_partition(self, sk_prefix: str = "") -> List[dict]:
        condition = Key("pk").eq(_PARTITION_KEY)
        if sk_prefix:
            condition = condition & Key("sk").begins_with(sk_prefix)
        items: List[dict] = []
        kwargs = {"KeyConditionExpression": condition}
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        return items

    def _to_item(self, record: ProjectRoleRecord) -> dict:
        return {
            "pk": _PARTITION_KEY,
            "sk": _sort_key(record.project_id, record.principal_id),
            **json.loads(record.model_dump_json()),
        }

    def _from_item(self, item: dict) -> ProjectRoleRecord:
        clean = {k: v for k, v in item.items() if k not in ("pk", "sk")}
        return ProjectRoleRecord.model_validate(clean)


def _sort_key(project_id: str, principal_id: str) -> str:
    """The composite sk — ``<project_id>#<principal_id>``. Derived (not generated), which is
    what makes ``grant`` an upsert."""
    return f"{project_id}{_SK_SEPARATOR}{principal_id}"
