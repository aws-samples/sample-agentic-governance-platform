"""Eval run store, the system of record for evaluation runs.

Three sinks, each with one job:

- **S3** holds the full, portable record: ``runs/{repo_id}/{run_id}/run.json`` plus
  ``results.jsonl`` and ``dataset.jsonl`` (the dataset AS RUN, so a later edit to the repo's file
  cannot rewrite what a past run was scored against).
- **DynamoDB**, through ``core.state_store``, is the index: ``pk="REPO#{repo_id}"``,
  ``sk="RUN#{started_at}#{run_id}"`` so a descending query is newest-first, and
  ``sk="OVERRIDE#{at}#{digest}#{uuid}"`` for audited promote overrides in the same partition. The
  run row is written new once (``save_run`` from a start) and then only over a row still in the
  status the caller names (``from_status``), so two writers cannot both close one run.
  ``sk="LOCK"`` is the per-repo run lock, the store's lease: acquiring it makes starting a run
  atomic, and its ``expires_at`` (the run's stale cutoff) frees it if the run dies without
  releasing it. ``sk="AWAIT_BUILD"`` marks a dataset commit whose build has not landed yet: it
  holds the dev build that was current at the commit, until ``expires_at``. A run row also
  carries the whole ``EvalRun`` as JSON (``run``) so a list never needs S3; the flat attributes
  are there for a human reading the table.
- **CloudWatch** gets one EMF log line per score (namespace ``AGP/Evals``). Run, item and session
  ids are LOG FIELDS, never dimensions — a dimension per run would mint a metric per run.

The run record needs both the table and the bucket: with either name empty the whole store is
in-memory (the index on the state store's memory double, the objects in a dictionary), so writes
and reads never split across modes. An empty log-group name sends no metrics.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from botocore.exceptions import ClientError

from core import aws_clients
from core.config import settings
from core.state_store import StateStore
from models.eval import DatasetItem, EvalRun, ItemResult, PromoteOverride, RunStatus

logger = logging.getLogger(__name__)

EMF_NAMESPACE = "AGP/Evals"
EMF_DIMENSIONS = [["RepoId", "Evaluator", "Source"]]
LOCK_SK = "LOCK"
AWAIT_SK = "AWAIT_BUILD"
LATEST_RUN_FIRST_PAGE = 50


def _pk(repo_id: str) -> str:
    return f"REPO#{repo_id}"


def _prefix(repo_id: str, run_id: str) -> str:
    return f"runs/{repo_id}/{run_id}/"


def _jsonl(models, *, exclude_none: bool = False) -> str:
    return "".join(m.model_dump_json(exclude_none=exclude_none) + "\n" for m in models)


class EvalStore:
    def __init__(
        self,
        *,
        bucket: str = "",
        table: str = "",
        log_group: str = "",
        s3=None,
        store: StateStore | None = None,
        logs=None,
        region: str | None = None,
    ):
        """``store`` is the state store over the index table, ``s3`` and ``logs`` are clients; each
        is built only when its name is configured and nothing was injected. The store gets the
        table's name only when the bucket is named too, so a missing bucket keeps the index in
        memory beside the objects."""
        self._bucket = bucket
        self._log_group = log_group
        self._store = (
            store if store is not None else StateStore(table if (table and bucket) else "", region=region)
        )
        self._s3: Any = (
            s3 if s3 is not None else (aws_clients.client("s3", region=region) if bucket else None)
        )
        self._logs: Any = (
            logs if logs is not None else (aws_clients.client("logs", region=region) if log_group else None)
        )
        self._objects: dict[str, bytes] = {}

    def save_run(
        self,
        run: EvalRun,
        results: list[ItemResult] | None = None,
        dataset: list[DatasetItem] | None = None,
        *,
        from_status: RunStatus | None = None,
    ) -> None:
        """Write the record, then the row: new when ``from_status`` is None, else over the row
        still in ``from_status``. A refused row write raises, so a lost run is never silent."""
        prefix = _prefix(run.repo_id, run.run_id)
        self._put(prefix + "run.json", run.model_dump_json(), "application/json")
        if results is not None:
            self._put(prefix + "results.jsonl", _jsonl(results), "application/x-ndjson")
        if dataset is not None:
            self._put(prefix + "dataset.jsonl", _jsonl(dataset, exclude_none=True), "application/x-ndjson")

        item: dict[str, Any] = {
            "pk": _pk(run.repo_id),
            "sk": f"RUN#{run.started_at.isoformat()}#{run.run_id}",
            "run_id": run.run_id,
            "status": run.status,
            "trigger": run.trigger,
            "started_by": run.started_by,
            "s3_prefix": prefix,
            "run": run.model_dump_json(),
        }
        for name, value in (
            ("digest", run.digest),
            ("build_tag", run.build_tag),
            ("git_sha", run.git_sha),
            ("summary", run.summary.model_dump_json() if run.summary else None),
            ("gate", run.gate.model_dump_json() if run.gate else None),
        ):
            if value is not None:
                item[name] = value
        if from_status is None:
            written = self._store.put_new(item)
        else:
            written = self._store.transition(item, {"status": {from_status}})
        if not written:
            raise RuntimeError(f"the row of run {run.run_id} is not in the state this write expects")

    def list_runs(self, repo_id: str, limit: int = 20) -> list[EvalRun]:
        return [EvalRun.model_validate_json(r["run"]) for r in self._query(repo_id, "RUN#", limit=limit)]

    def get_run(self, repo_id: str, run_id: str) -> tuple[EvalRun, list[ItemResult]] | None:
        prefix = _prefix(repo_id, run_id)
        body = self._get(prefix + "run.json")
        if body is None:
            return None
        results_body = self._get(prefix + "results.jsonl") or ""
        results = [ItemResult.model_validate_json(line) for line in results_body.splitlines() if line.strip()]
        return EvalRun.model_validate_json(body), results

    def get_run_dataset(self, repo_id: str, run_id: str) -> list[DatasetItem]:
        """The dataset AS RUN (``dataset.jsonl``); ``[]`` when the run saved none (yet)."""
        body = self._get(_prefix(repo_id, run_id) + "dataset.jsonl") or ""
        return [DatasetItem.model_validate_json(line) for line in body.splitlines() if line.strip()]

    def latest_run_for_build(
        self, repo_id: str, *, digest: str | None, tag: str | None, finished: bool = False
    ) -> EvalRun | None:
        """Newest run for one build: matched on ``digest`` when given, else on ``tag``
        (``build_tag``); neither ⇒ None, since nothing identifies the build. ``finished`` keeps
        only runs that scored (``passed``/``failed``) — running and errored runs have no numbers.
        Reads the newest ``LATEST_RUN_FIRST_PAGE`` runs first and the whole history only when
        none of them is the build's and the page was full."""
        if not digest and not tag:
            return None

        def match(r: EvalRun) -> bool:
            same = r.digest == digest if digest else r.build_tag == tag
            return same and (not finished or r.status in ("passed", "failed"))

        def first_match(rows: list[dict]) -> EvalRun | None:
            runs = (EvalRun.model_validate_json(r["run"]) for r in rows)
            return next((r for r in runs if match(r)), None)

        page = self._query(repo_id, "RUN#", limit=LATEST_RUN_FIRST_PAGE)
        found = first_match(page)
        if found is not None or len(page) < LATEST_RUN_FIRST_PAGE:
            return found
        return first_match(self._query(repo_id, "RUN#"))

    def acquire_run_lock(self, repo_id: str, run_id: str, *, ttl_s: int) -> bool:
        """Take the repo's run lock for ``run_id`` for ``ttl_s`` seconds. False when another run
        holds it and it has not expired. One conditional put, so two starts cannot both win."""
        return self._store.acquire_lease((_pk(repo_id), LOCK_SK), holder=run_id, ttl_s=ttl_s)

    def release_run_lock(self, repo_id: str, run_id: str) -> None:
        """Free the lock if ``run_id`` still holds it; a lock taken over since is left alone."""
        self._store.release_lease((_pk(repo_id), LOCK_SK), holder=run_id)

    def mark_awaiting_build(self, repo_id: str, build: str, *, expires_at: datetime) -> None:
        """Remember that ``build`` (digest, else tag; "" = none) is now older than the trunk."""
        self._store.set_owned(
            (_pk(repo_id), AWAIT_SK), {"build": build, "expires_at": int(expires_at.timestamp())}
        )

    def awaiting_build(self, repo_id: str, *, now: datetime) -> Optional[str]:
        """The build a dataset commit left behind, or None when there is no unexpired mark."""
        row = self._store.get((_pk(repo_id), AWAIT_SK))
        if row is None or not int(now.timestamp()) < int(row["expires_at"]):
            return None
        return row.get("build", "")

    def record_override(self, o: PromoteOverride) -> None:
        item: dict[str, Any] = {
            "pk": _pk(o.repo_id),
            "sk": f"OVERRIDE#{o.at.isoformat()}#{o.digest or 'none'}#{uuid4().hex}",
            "repo_id": o.repo_id,
            "by": o.by,
            "reason": o.reason,
            "at": o.at.isoformat(),
        }
        if o.digest is not None:
            item["digest"] = o.digest
        if o.by_name is not None:
            item["by_name"] = o.by_name
        if o.build_tag is not None:
            item["build_tag"] = o.build_tag
        self._store.put_new(item)

    def list_overrides(self, repo_id: str, limit: int = 20) -> list[PromoteOverride]:
        return [
            PromoteOverride(
                repo_id=r["repo_id"],
                digest=r.get("digest"),
                by=r["by"],
                reason=r["reason"],
                at=r["at"],
                by_name=r.get("by_name"),
                build_tag=r.get("build_tag"),
            )
            for r in self._query(repo_id, "OVERRIDE#", limit=limit)
        ]

    def emit_scores(self, run: EvalRun, results: list[ItemResult]) -> None:
        """One PutLogEvents batch, one EMF line per score. Never raises: metrics are a mirror
        of the S3 record, so a CloudWatch failure must not fail the run."""
        if self._logs is None or not self._log_group:
            return
        try:
            now_ms = int(time.time() * 1000)
            events = [
                {"timestamp": now_ms, "message": json.dumps(self._emf(run, result, score, now_ms))}
                for result in results
                for score in result.scores
            ]
            if not events:
                return
            try:
                self._logs.create_log_stream(logGroupName=self._log_group, logStreamName=run.run_id)
            except self._logs.exceptions.ResourceAlreadyExistsException:
                pass
            self._logs.put_log_events(
                logGroupName=self._log_group, logStreamName=run.run_id, logEvents=events
            )
        except Exception:
            logger.exception("eval emit_scores failed for run %s", run.run_id)

    @staticmethod
    def _emf(run: EvalRun, result: ItemResult, score, now_ms: int) -> dict[str, Any]:
        event: dict[str, Any] = {
            "RepoId": run.repo_id,
            "Evaluator": score.evaluator,
            "Source": run.trigger,
            "gen_ai.evaluation.name": score.evaluator,
            "gen_ai.evaluation.score.value": score.value,
            "gen_ai.evaluation.score.label": score.label,
            "gen_ai.evaluation.explanation": score.explanation or score.error,
            "run_id": run.run_id,
            "item_id": result.item_id,
            "session_id": result.session_id,
            "agent_id": run.agent_id,
        }
        metrics = []
        if score.value is not None:
            event["Score"] = score.value
            metrics.append({"Name": "Score", "Unit": "None"})
        if score.passed is not None:
            event["Pass"] = 1 if score.passed else 0
            metrics.append({"Name": "Pass", "Unit": "Count"})
        # An EMF directive needs at least one metric: https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format_Specification.html
        if metrics:
            event["_aws"] = {
                "Timestamp": now_ms,
                "CloudWatchMetrics": [
                    {"Namespace": EMF_NAMESPACE, "Dimensions": EMF_DIMENSIONS, "Metrics": metrics}
                ],
            }
        return event

    def _query(self, repo_id: str, sk_prefix: str, limit: int | None = None) -> list[dict]:
        """Newest-first rows under ``sk_prefix``, up to ``limit``."""
        return self._store.query(_pk(repo_id), sk_prefix=sk_prefix, forward=False, limit=limit)

    def _put(self, key: str, body: str, content_type: str) -> None:
        if self._store.in_memory:
            self._objects[key] = body.encode()
            return
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=body.encode(), ContentType=content_type)

    def _get(self, key: str) -> Optional[str]:
        if self._store.in_memory:
            body = self._objects.get(key)
            return body.decode() if body is not None else None
        try:
            return self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read().decode()
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise


_store: Optional[EvalStore] = None


def get_eval_store() -> EvalStore:
    global _store
    if _store is None:
        _store = EvalStore(
            bucket=settings.EVALS_BUCKET_NAME,
            table=settings.EVALS_TABLE_NAME,
            log_group=settings.EVALS_LOG_GROUP,
        )
    return _store
