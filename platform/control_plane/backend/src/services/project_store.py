"""The project family's rows: one ``core.state_store.StateStore`` table, three partitions.

``project`` and ``repository`` rows are keyed by the record's id; ``deployment`` is append-only, keyed
``{repo_id}#{stage}#{started_at}#{suffix}``.
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

from botocore.exceptions import ClientError
from pydantic import ValidationError

from core.state_store import StateStore
from models.deployment import Deployment, DeploymentOutcome
from models.project import Project
from models.repository import Repository

logger = logging.getLogger(__name__)

_PROJECT_PK = "project"
_REPOSITORY_PK = "repository"
_DEPLOYMENT_PK = "deployment"
# Decision: docs/adr/backend-deployment-history-paging-ceiling.md
_MAX_DEPLOYMENT_ROWS = 60_000
_ROLLBACK_HISTORY_LIMIT = 100
_PROD_CANDIDATE_FIELDS = (
    "prod_candidate_image_tag",
    "prod_candidate_digest",
    "prod_candidate_sha",
    "prod_candidate_actor",
    "prod_candidate_at",
    "prod_candidate_status",
)
_CANDIDATE_WRITE_FIELDS = frozenset(_PROD_CANDIDATE_FIELDS) | {"updated_at"}


class ProjectStore:
    def __init__(self, state_store: StateStore) -> None:
        self._store = state_store

    def get_project(self, id: str) -> Optional[Project]:
        try:
            item = self._store.get((_PROJECT_PK, id))
        except ClientError:
            logger.exception("Failed to fetch project %s from DDB", id)
            return None
        return self.hydrate_project(item) if item else None

    def load_all_projects(self) -> List[Project]:
        try:
            items = self.scan_partition(_PROJECT_PK)
        except ClientError:
            logger.exception("Failed to load projects from DDB")
            return []
        projects: List[Project] = []
        for item in items:
            try:
                projects.append(self.hydrate_project(item))
            except (ValidationError, ValueError):
                logger.warning("Skipping malformed Project row sk=%s (failed validation)", item.get("sk"))
        return projects

    @staticmethod
    def hydrate_project(item: dict) -> Project:
        """Validate a stored project item; a legacy record WITHOUT ``tenant_id``
        hydrates as ``tenant_id="default"`` (the seed tenant) so the required
        field never breaks reads of pre-existing data."""
        data = _strip_keys(item)
        data.setdefault("tenant_id", "default")
        return Project.model_validate(data)

    def save_project(self, record: Project) -> None:
        if not self._store.put_new(_to_item(_PROJECT_PK, record)):
            raise RuntimeError(f"project {record.id} already exists")

    def load_repos_for(self, project_id: str) -> List[Repository]:
        return [r for r in self.load_all_repos() if r.project_id == project_id]

    def load_all_repos(self) -> List[Repository]:
        try:
            items = self.scan_partition(_REPOSITORY_PK)
        except ClientError:
            logger.exception("Failed to load repositories from DDB")
            return []
        return _parse_rows(Repository, items)

    def save_repo(
        self,
        record: Repository,
        *,
        include_cicd_status: bool = False,
        include_prod_candidate: bool = False,
    ) -> None:
        """Persist a repository record WITHOUT clobbering the attributes another writer owns.

        A whole-item ``put_item`` would wipe fields written out-of-band back to a stale read, so
        this is a targeted ``SET`` over the attributes the backend owns. ``last_dev_image_tag``,
        ``last_dev_digest`` and the ``gateway_*`` fields are CodeBuild-exclusive and never written
        here; the computed read-time fields (``build_kind``, ``dev_outcome_for_candidate``, the
        eval gate trio) are never written either. :data:`_PROD_CANDIDATE_FIELDS` are stamped only
        by :meth:`ProjectService.record_prod_candidate` and cleared only by a successful
        :meth:`ProjectService.promote_repo`, which opts in via ``include_prod_candidate``;
        ``cicd_status`` is co-owned and a caller that means to transition it opts in via
        ``include_cicd_status``. The two opt-ins are independent."""
        skip = {
            "last_dev_image_tag",
            "last_dev_digest",
            "dev_outcome_for_candidate",
            "build_kind",
            "gateway_arn",
            "gateway_id",
            "gateway_url",
            "eval_gate",
            "eval_regressions",
            "eval_evidence",
        }
        if not include_cicd_status:
            skip.add("cicd_status")
        if not include_prod_candidate:
            skip.update(_PROD_CANDIDATE_FIELDS)
        self.write_repo(record, skip)

    def save_prod_candidate(self, record: Repository) -> None:
        """Persist ONLY the prod-candidate block + ``updated_at``.

        The inverse of :meth:`save_repo`: instead of SETting everything except another
        writer's attributes, this names exactly the attributes this writer OWNS
        (:data:`_CANDIDATE_WRITE_FIELDS`) and skips the entire rest of the record."""
        skip = set(Repository.model_fields) - _CANDIDATE_WRITE_FIELDS
        self.write_repo(record, skip)

    def write_repo(self, record: Repository, skip: set) -> None:
        """The single repository write path: ``set_owned`` over every serialized attribute of
        ``record`` EXCEPT ``skip`` (an attribute the ``SET`` does not NAME survives), an upsert
        exactly like a put for a new row that never removes or resets an attribute it does not
        name.

        A ``None`` ``agent_id`` (an MCP repo) is NEVER named — it is the ``agent_id-index``
        GSI hash key, and DynamoDB rejects a NULL index key; omitting it also keeps the row out of
        that sparse index."""
        if record.agent_id is None:
            skip = skip | {"agent_id"}
        attrs = {k: v for k, v in json.loads(record.model_dump_json()).items() if k not in skip}
        self._store.set_owned((_REPOSITORY_PK, record.id), attrs)

    def get_repo(self, repo_id: str) -> Optional[Repository]:
        try:
            item = self._store.get((_REPOSITORY_PK, repo_id))
        except ClientError:
            logger.exception("Failed to fetch repository %s from DDB", repo_id)
            return None
        return Repository.model_validate(_strip_keys(item)) if item else None

    def delete_repo(self, repo_id: str) -> None:
        self._store.delete((_REPOSITORY_PK, repo_id))

    def delete_project(self, project_id: str) -> None:
        self._store.delete((_PROJECT_PK, project_id))

    def scan_partition(self, pk: str) -> List[dict]:
        return self._store.query(pk)

    def list_deployments(
        self, repo_id: str, stage: Optional[str] = None, limit: int = 50
    ) -> List[Deployment]:
        """The repo's delivery history, NEWEST FIRST — the whole repo, or one ``stage`` of it.

        Two read paths, because the sk is ``{repo_id}#{stage}#{started_at}#{suffix}`` and the
        stage component sits BEFORE the timestamp. One stage: the sk order within the prefix IS
        time order, so DynamoDB sorts and limits server-side. All stages: sk order is stage-major,
        and a DynamoDB ``Limit`` would truncate to the alphabetically-first stage and could drop
        the newest deployment, so this path pages the ``repo_id`` prefix itself, keeps at most
        ``limit`` rows per stage, merges by ``started_at`` descending and applies ``limit`` after
        the merge. ``limit`` means "the newest N rows" on both paths. A malformed row is
        skipped and logged; an unreachable store returns ``[]``."""
        if stage:
            return self.query_stage_deployments(repo_id, stage, limit)
        rows = self.all_stage_deployments(repo_id, limit)
        rows.sort(key=lambda d: (d.started_at, d.seq_key), reverse=True)
        return rows[:limit]

    def query_stage_deployments(self, repo_id: str, stage: str, limit: int) -> List[Deployment]:
        """One stage, newest-first, sorted and truncated by DynamoDB (its sk order is time order
        inside a single ``{repo_id}#{stage}#`` prefix)."""
        prefix = f"{repo_id}#{stage}#"
        try:
            rows = self._store.query(_DEPLOYMENT_PK, sk_prefix=prefix, forward=False, limit=limit)
        except ClientError:
            logger.exception("Failed to load deployments for repo %s from DDB", repo_id)
            return []
        return _parse_rows(Deployment, rows)

    def all_stage_deployments(self, repo_id: str, limit: int) -> List[Deployment]:
        """The repo's rows that can still make the newest-``limit`` set, unordered — the input to
        the cross-stage merge.

        Reads the ``f"{repo_id}#"`` prefix whole (the store follows ``LastEvaluatedKey``), never
        with the caller's ``limit`` as a DDB ``Limit``: DynamoDB would truncate in stage order, so
        "newest" computed off a truncated page is a silently WRONG answer, not a short one. The one
        ``Limit`` the read carries is the row ceiling ``_MAX_DEPLOYMENT_ROWS``, asked for as one
        row more so a hit is unambiguous; it is a real ceiling and is logged when hit. Rows arrive
        newest-first within each stage (``ScanIndexForward=False`` orders by time inside a stage),
        so once a stage has contributed ``limit`` rows its remaining, strictly older rows are
        dropped (:func:`_bound_per_stage`); see :meth:`list_deployments` for the argument."""
        try:
            rows = self._store.query(
                _DEPLOYMENT_PK, sk_prefix=f"{repo_id}#", forward=False, limit=_MAX_DEPLOYMENT_ROWS + 1
            )
        except ClientError:
            logger.exception("Failed to load deployments for repo %s from DDB", repo_id)
            return []
        if len(rows) > _MAX_DEPLOYMENT_ROWS:
            logger.warning(
                "[project] deployment history for repo %s exceeded the %d-row read ceiling — this "
                "result MAY OMIT THE NEWEST DEPLOYMENT: paging is stage-major, so an early-sorting "
                "stage can exhaust the budget and a later stage go unread. Treat any 'current' "
                "artifact derived from it as unverified",
                repo_id,
                _MAX_DEPLOYMENT_ROWS,
            )
            rows = rows[:_MAX_DEPLOYMENT_ROWS]
        return _bound_per_stage(_parse_rows(Deployment, rows), limit)

    def save_deployment(self, record: Deployment) -> None:
        """Create the row under its composite sk. The sk is unique per append, so the write is a
        ``put_new`` and a repeated sk is refused rather than replacing a row.

        An MCP row's ``agent_id`` is OMITTED, never written as NULL — the same
        ``agent_id-index`` GSI-key rule :meth:`write_repo` applies to repository rows."""
        item = _to_item(_DEPLOYMENT_PK, record, sk=record.seq_key)
        if item.get("agent_id") is None:
            item.pop("agent_id", None)
        if not self._store.put_new(item):
            raise RuntimeError(f"deployment row {record.seq_key} already exists")

    def has_deployments(self, repo_id: str) -> bool:
        return bool(self._store.query(_DEPLOYMENT_PK, sk_prefix=f"{repo_id}#", limit=1))

    def has_succeeded(self, repo_id: str, stage: str, image_tag: str) -> bool:
        """Has ``image_tag`` ever SUCCEEDED for this repo in this stage?

        The rollback target check. Reads the repo+stage slice of the append-only partition —
        which is scoped by the sk prefix, so another repo's or another stage's succeeded row can
        never satisfy it — and looks for a ``SUCCEEDED`` outcome on the exact tag.

        ``_ROLLBACK_HISTORY_LIMIT`` bounds the read. It is a REAL limit: an artifact that last
        succeeded further back than that many deployments ago is not offered as a rollback
        target. That direction is the safe one (a refusal, not an unverified deploy), and the
        UI's own history list is bounded the same way — a target the operator cannot see listed
        is not one they are asking for.

        Fails CLOSED: :meth:`list_deployments` degrades to ``[]`` on an unreachable store, and
        an empty history here means REFUSE. "I cannot read the history" and "this tag never
        succeeded" are the same value to a degrading read but opposite answers to a
        deploy-to-production question, so the ambiguity must resolve to no deploy.

        A BLANK ``stage`` is refused rather than passed through, because
        :meth:`list_deployments` branches on ``if stage:`` — an empty string would read ACROSS
        stages and let a ``dev``-only tag answer True for a ``prod`` rollback. The caller checks
        this too; it is repeated here so the PRIMITIVE cannot be misused by a future caller."""
        if not stage or not stage.strip():
            return False
        return any(
            d.image_tag == image_tag and d.outcome is DeploymentOutcome.SUCCEEDED
            for d in self.list_deployments(repo_id, stage=stage, limit=_ROLLBACK_HISTORY_LIMIT)
        )

    def latest_dev_outcome_for_tag(self, repo_id: str, image_tag: str) -> Optional[DeploymentOutcome]:
        """The DEV-ONLY fact backing the promote gate.

        Reads the ``dev``-stage slice of the append-only ``Deployment`` partition — scoped by
        stage so a prod (or any other stage) outcome can never answer this — and returns the
        OUTCOME of the newest row whose ``image_tag`` matches this exact candidate.
        :meth:`list_deployments` returns newest-first, so the first match is the last word on
        this artifact: a build starts a row (``STARTED``) and the buildspec appends a SECOND,
        terminal row (``SUCCEEDED``/``FAILED``) rather than updating it in place (the partition
        is append-only) — the terminal row is newer and so is returned first.

        Returns ``None`` — treated as "not a known dev failure", i.e. does not block — when no
        ``dev`` row names this tag at all: a record predating the Deployment partition, or a
        narrow race where this read runs before the triggering build's own append has landed.
        Only an explicit ``FAILED`` row refuses.

        Bounded by ``_ROLLBACK_HISTORY_LIMIT``, same reasoning as :meth:`has_succeeded`: an
        artifact whose dev attempt is further back than that many dev deployments is treated as
        unknown rather than scanned for unbounded."""
        for d in self.list_deployments(repo_id, stage="dev", limit=_ROLLBACK_HISTORY_LIMIT):
            if d.image_tag == image_tag:
                return d.outcome
        return None


def _to_item(pk: str, record, sk: Optional[str] = None) -> dict:
    """Serialize a record into a DDB item. ``sk`` defaults to the record's id (projects,
    repositories — one row per entity); the append-only deployment partition passes its
    composite sort key instead."""
    return {"pk": pk, "sk": sk or record.id, **json.loads(record.model_dump_json())}


def _strip_keys(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in ("pk", "sk")}


def _bound_per_stage(rows, limit: int):
    """Keep at most ``limit`` rows per stage from ``rows``, which MUST already be newest-first
    within each stage: the bound of the cross-stage read."""
    seen: dict = {}
    kept = []
    for row in rows:
        if seen.get(row.stage, 0) >= limit:
            continue
        seen[row.stage] = seen.get(row.stage, 0) + 1
        kept.append(row)
    return kept


def _parse_rows(model, items):
    """Validate DDB rows into ``model`` records, SKIPPING (not raising on) any malformed
    row so a single pre-rename / partial-write item never 500s the whole list. Logs the
    row's ``sk`` (never the full row — could be large) on skip."""
    records = []
    for item in items:
        try:
            records.append(model.model_validate(_strip_keys(item)))
        except (ValidationError, ValueError):
            logger.warning(
                "Skipping malformed %s row sk=%s (failed validation)",
                model.__name__,
                item.get("sk"),
            )
    return records
