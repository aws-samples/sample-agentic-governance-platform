"""Eval runner: the repo's eval files, run start, and run execution.

Ties the eval pieces together:

- **Files.** ``evals/evals.yaml`` and the dataset are read one file each (``read_file``) at the
  trunk's head sha. Edits are committed to the trunk WITHOUT ``[skip ci]`` so a dev build starts,
  through ``commit_files_on_head``: an overlay on the tip that only fast-forwards, so a push
  that landed since the read is a 409 ``conflict``, never silently reverted. The dataset path is
  ``evals/<name>.jsonl`` only (the model's pattern, re-checked before every read and write).
- **Build identity.** A run is tied to the dev build it ran against: its digest when present,
  else its image tag (``build_tag``; harness/config builds have no digest).
- **Start** (request path): resolve repo + agent, read and validate the files, take the dev
  digest, take the repo's run lock (atomic), exchange the caller's token OBO once, save a
  ``running`` run and schedule :meth:`EvalRunner.execute` as a background task.
- **Execute** (background, never raises): phase A invokes every item against the dev runtime
  (the only OBO-bound work); after one ingestion wait, phase B fills missing tool calls from
  spans and runs the checks and the judges. Then the gate, save, emit and mirror.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, ClassVar, Literal, Optional
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError

from core.errors import MESSAGE, AppError, Outcome
from models.agent import is_harness_arn
from models.eval import (
    DATASET_PATH_PATTERN,
    DatasetItem,
    EvalConfig,
    EvalRun,
    GateResult,
    ItemResult,
    RunBaseline,
    RunSummary,
    Score,
    ToolCall,
)
from services.eval_checks import run_checks
from services.eval_config import (
    CONFIG_FILE,
    EvalFileError,
    dump_config,
    dump_dataset,
    parse_config,
    parse_dataset,
)
from services.eval_judges import SpanSource, judge_item
from services.eval_langfuse import mirror_run
from services.graph_errors import GraphError, NotAssignedError, OboConfigError
from services.repo_provider import RepoConflictError
from services.tenant_credentials import stage_client

logger = logging.getLogger(__name__)

STAGE = "dev"
INGEST_WAIT_S = 60
SPAN_WAIT_S = 120
SPAN_POLL_S = 5
SPAN_LOOKBACK = timedelta(minutes=1)  # clock skew between AGP and the span timestamps
TRACES_LOOKBACK = timedelta(days=7)
STALE_RUNNING = timedelta(hours=2)
STALE_BASE = timedelta(minutes=10)
STALE_PER_ITEM = timedelta(minutes=3)
AWAIT_BUILD = timedelta(minutes=30)
HARNESS_LOG_GROUP_PREFIX = "/aws/bedrock-agentcore/runtimes/"
SPANS_LOG_GROUP = "aws/spans"
MAX_FILE_BYTES = 1_000_000
REGRESSION_TOLERANCE = 0.05


class EvalError(AppError):
    """An eval operation was refused or failed; ``kind`` selects the wire.

    The message is client copy: a fixed sentence of this module, at most naming the caller
    (``not_assigned``), never a provider, store or Graph response. ``invalid`` carries the file
    errors (``"<file>:<line>: <message>"`` each) beside the message; every other kind sends the
    message alone.
    """

    domain: ClassVar[str] = "eval"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "not_found": Outcome(404, None),
        "no_agent": Outcome(409, None),
        "no_dev": Outcome(409, None),
        "running": Outcome(409, None),
        "no_commit": Outcome(409, None),
        "conflict": Outcome(409, None),
        "trunk_mismatch": Outcome(409, None),
        "not_provisioned": Outcome(409, None),
        "invalid": Outcome(422, {"message": MESSAGE, "errors": []}),
        "empty": Outcome(422, None),
        "not_assigned": Outcome(403, None),
        "obo_config": Outcome(500, None),
        "provider": Outcome(502, None),
        "obo_failed": Outcome(502, None),
    }

    def __init__(self, message: str, kind: str, errors: Optional[list[str]] = None):
        super().__init__(message, kind, extra={"errors": list(errors)} if errors else None)
        self.errors = list(errors or [])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _decode(raw: Optional[bytes], path: str, errors: list[str]) -> Optional[str]:
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        errors.append(f"{path}:1: not UTF-8 text")
        return None


def _mean(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _short_build(run: EvalRun) -> str:
    """The run's build in short form, as the UI labels it: the tag's commit, else the digest's hex."""
    tag = (run.build_tag or "").strip()
    if tag:
        if re.fullmatch(r"[0-9a-f]{40}", tag):
            return tag[:7]
        return tag.rsplit("-", 1)[-1] or tag
    return (run.digest or "").removeprefix("sha256:")[:7]


def _judge_mean(summary: RunSummary) -> Optional[float]:
    return _mean([m for m in summary.judge_means.values() if m is not None])


def _judge_reasons(
    means: dict[str, Optional[float]], unscored: dict[str, int], threshold: float, item_count: int
) -> list[str]:
    """Why the judges fail the gate: no scores, items left unscored, or a mean under the threshold."""
    reasons: list[str] = []
    for judge, mean in means.items():
        if mean is None:
            reasons.append(f"{judge}: no scores")
            continue
        if unscored.get(judge):
            reasons.append(f"{judge}: {unscored[judge]}/{item_count} items unscored")
        if mean < threshold:
            reasons.append(f"{judge} mean {mean:.2f} < {threshold}")
    return reasons


def _needs_spans(result: ItemResult, config: EvalConfig) -> bool:
    return bool(result.session_id and not result.error and (config.judges or result.tool_calls is None))


class _TenantClients:
    """The tenant-stage AWS clients one run scores with, built on first need, once. A client that
    cannot be built is remembered as its error, so every score that needed it carries the error
    instead of the run failing."""

    def __init__(self, build: Callable[[str], Any]):
        self._build = build
        self._built: dict[str, tuple[Any, Optional[str]]] = {}

    def get(self, service: str) -> tuple[Any, Optional[str]]:
        if service not in self._built:
            try:
                self._built[service] = (self._build(service), None)
            except Exception as exc:
                logger.exception("[evals] could not build the %s client", service)
                self._built[service] = (None, f"{service} client unavailable ({type(exc).__name__})")
        return self._built[service]


class EvalRunner:
    def __init__(
        self,
        *,
        projects,
        repos,
        connections,
        registry,
        adapter_for: Callable[[Any], Any],
        graph,
        store,
        stage_cfg: Callable[[Optional[str], str], Any] = lambda tenant_id, stage: None,
        client=stage_client,
        mirror=mirror_run,
        now: Callable[[], datetime] = _utcnow,
        new_id: Callable[[], str] = lambda: uuid4().hex,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._projects = projects
        self._repos = repos
        self._conn = connections
        self._registry = registry
        self._adapter_for = adapter_for
        self._graph = graph
        self._store = store
        self._stage_cfg = stage_cfg
        self._client = client
        self._mirror = mirror
        self._now = now
        self._new_id = new_id
        self._sleep = sleep

    def _resolve(self, project_id: str, repo_id: str):
        repo = self._projects.get_repo(repo_id)
        if repo is None or repo.project_id != project_id:
            raise EvalError("Repository not found", kind="not_found")
        detail = self._projects.get_project(project_id)
        if detail is None:
            raise EvalError("Project not found", kind="not_found")
        return repo, detail.project

    def _agent(self, repo):
        agent = self._registry.get(repo.agent_id) if repo.agent_id else None
        if agent is None:
            raise EvalError("this repository has no agent to evaluate", kind="no_agent")
        return agent

    def _head(self, project, repo):
        """(head_sha, connection, token) of the trunk; head "" ⇒ no commit yet.

        Reads go through ``read_repo`` (the DEFAULT branch's tip) and writes go to the trunk, so
        the two must be one branch — otherwise an edit would be based on another branch's files."""
        try:
            connection = self._conn.get_connection(project.connection_id)
            token = self._conn.get_bearer_token(project.connection_id)
            view = self._repos.read_repo(connection.org, repo.name, token=token, base_url=connection.base_url)
        except Exception:
            logger.exception("[evals] could not read the head of repo %s", repo.id)
            raise EvalError("the repository could not be read", kind="provider") from None
        if view is None:
            raise EvalError("the repository could not be read", kind="provider")
        if view.head_sha and view.default_branch != project.trunk_branch:
            raise EvalError("the repository's default branch is not the project trunk", kind="trunk_mismatch")
        return view.head_sha or "", connection, token

    def _read_file(self, repo, connection, token, head: str, path: str) -> Optional[bytes]:
        if not head:
            return None
        try:
            return self._repos.read_file(
                connection.org, repo.name, path, ref=head, token=token, base_url=connection.base_url
            )
        except Exception:
            logger.exception("[evals] could not read %s of repo %s", path, repo.id)
            raise EvalError("the repository could not be read", kind="provider") from None

    @staticmethod
    def _parse_config(text: Optional[str], errors: list[str]) -> EvalConfig:
        if text is not None and len(text.encode("utf-8")) > MAX_FILE_BYTES:
            errors.append(f"{CONFIG_FILE}:1: larger than {MAX_FILE_BYTES} bytes")
            return EvalConfig()
        try:
            return parse_config(text)
        except EvalFileError as exc:
            errors.extend(exc.errors)
        except RecursionError:
            errors.append(f"{CONFIG_FILE}:1: nested too deeply")
        return EvalConfig()

    @staticmethod
    def _parse_dataset(text: Optional[str], path: str, errors: list[str]) -> list[DatasetItem]:
        if text is not None and len(text.encode("utf-8")) > MAX_FILE_BYTES:
            errors.append(f"{path}:1: larger than {MAX_FILE_BYTES} bytes")
            return []
        try:
            return parse_dataset(text)
        except EvalFileError as exc:
            errors.extend(exc.errors)
        except RecursionError:
            errors.append(f"{path}:1: nested too deeply")
        return []

    @staticmethod
    def _check_path(path: str) -> None:
        """The dataset path AGP reads and COMMITS to: ``evals/<name>.jsonl`` and nothing else."""
        if not re.fullmatch(DATASET_PATH_PATTERN, path or "") or path == CONFIG_FILE:
            raise EvalError(
                "invalid dataset path",
                kind="invalid",
                errors=[f"{CONFIG_FILE}: dataset must be evals/<name>.jsonl"],
            )

    def _load(self, project, repo) -> dict:
        """Config + dataset at the trunk's head, with every file problem in ``errors``."""
        head, connection, token = self._head(project, repo)
        errors: list[str] = []
        raw = self._read_file(repo, connection, token, head, CONFIG_FILE)
        config = self._parse_config(_decode(raw, CONFIG_FILE, errors), errors)
        self._check_path(config.dataset)
        items = self._parse_dataset(
            _decode(self._read_file(repo, connection, token, head, config.dataset), config.dataset, errors),
            config.dataset,
            errors,
        )
        return {
            "head": head,
            "connection": connection,
            "token": token,
            "config": config,
            "items": items,
            "errors": errors,
            "configured": raw is not None,
        }

    def read(self, project_id: str, repo_id: str) -> dict:
        repo, project = self._resolve(project_id, repo_id)
        st = self._load(project, repo)
        return {
            "config": st["config"],
            "items": st["items"],
            "head_sha": st["head"],
            "errors": st["errors"],
            "configured": st["configured"],
        }

    @classmethod
    def validate(cls, *, config_text: Optional[str] = None, dataset_text: Optional[str] = None) -> dict:
        errors: list[str] = []
        config = cls._parse_config(config_text, errors)
        items = cls._parse_dataset(dataset_text, config.dataset, errors)
        return {"ok": not errors, "errors": errors, "items": len(items)}

    def _commit(self, project, repo, head, connection, token, path: str, body: str, what: str) -> str:
        if not head:
            raise EvalError("the repository has no commit yet", kind="no_commit")
        if path != CONFIG_FILE:
            self._check_path(path)
        data = body.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise EvalError(
                "the file is too large",
                kind="invalid",
                errors=[f"{path}: larger than {MAX_FILE_BYTES} bytes"],
            )
        try:
            return self._repos.commit_files_on_head(
                connection.org,
                repo.name,
                {path: data},
                branch=project.trunk_branch,
                expected_head_sha=head,
                message=f"evals: {what} (via AGP)",
                token=token,
                base_url=connection.base_url,
            )
        except RepoConflictError:
            raise EvalError("the branch moved; reload and retry", kind="conflict") from None
        except Exception:
            logger.exception("[evals] commit to repo %s failed", repo.id)
            raise EvalError("the commit failed", kind="provider") from None

    def put_config(self, project_id: str, repo_id: str, config: EvalConfig) -> str:
        repo, project = self._resolve(project_id, repo_id)
        self._check_path(config.dataset)
        head, connection, token = self._head(project, repo)
        return self._commit(
            project, repo, head, connection, token, CONFIG_FILE, dump_config(config), "update config"
        )

    def put_dataset(
        self, project_id: str, repo_id: str, items: list[DatasetItem], *, mode: str
    ) -> tuple[str, int]:
        repo, project = self._resolve(project_id, repo_id)
        head, connection, token = self._head(project, repo)
        errors: list[str] = []
        config = self._parse_config(
            _decode(self._read_file(repo, connection, token, head, CONFIG_FILE), CONFIG_FILE, errors), errors
        )
        path = config.dataset
        self._check_path(path)
        current: list[DatasetItem] = []
        if mode == "append":
            current = self._parse_dataset(
                _decode(self._read_file(repo, connection, token, head, path), path, errors), path, errors
            )
            taken = {i.id for i in current}
            errors.extend(f"duplicate id '{i.id}' (already in {path})" for i in items if i.id in taken)
        if errors:
            raise EvalError("the dataset cannot be updated", kind="invalid", errors=errors)
        body = dump_dataset(current + list(items))
        merged = self._parse_dataset(body, path, errors)
        if errors:
            raise EvalError("the dataset is invalid", kind="invalid", errors=errors)
        what = f"add {len(items)} item(s)" if mode == "append" else f"replace dataset ({len(merged)} items)"
        head = self._commit(project, repo, head, connection, token, path, body, what)
        try:
            self._store.mark_awaiting_build(
                repo.id,
                repo.last_dev_digest or repo.last_dev_image_tag or "",
                expires_at=self._now() + AWAIT_BUILD,
            )
        except Exception:
            logger.exception("[evals] could not mark repo %s as awaiting its build", repo.id)
        return head, len(merged)

    @staticmethod
    def _stale_after(item_count: Optional[int]) -> timedelta:
        """The cutoff after which a ``running`` run is treated as dead: ``STALE_BASE`` plus
        ``STALE_PER_ITEM`` per item, capped at ``STALE_RUNNING``. The per-item term is the worst
        case of one invoke plus its two-minute span wait, so one item is about thirteen minutes,
        three about nineteen, and fifty the two-hour cap; a run with no item count gets the cap."""
        if item_count is None:
            return STALE_RUNNING
        return min(STALE_RUNNING, STALE_BASE + STALE_PER_ITEM * item_count)

    def _fresh(self, run: Optional[EvalRun]) -> Optional[EvalRun]:
        """``run`` unless it is a ``running`` run older than its stale cutoff (its task died)."""
        if run is None or run.status != "running":
            return run
        return None if run.started_at <= self._now() - self._stale_after(run.item_count) else run

    def runs(self, project_id: str, repo_id: str) -> dict:
        repo, _ = self._resolve(project_id, repo_id)
        current = self._store.latest_run_for_build(
            repo.id, digest=repo.last_dev_digest, tag=repo.last_dev_image_tag
        )
        return {
            "runs": self._store.list_runs(repo.id),
            "overrides": self._store.list_overrides(repo.id),
            "current_digest": repo.last_dev_digest,
            "current_build_tag": repo.last_dev_image_tag,
            "current_run": current,
        }

    def get_run(
        self, project_id: str, repo_id: str, run_id: str
    ) -> tuple[EvalRun, list[ItemResult], list[DatasetItem]]:
        repo, _ = self._resolve(project_id, repo_id)
        hit = self._store.get_run(repo.id, run_id)
        if hit is None:
            raise EvalError("Run not found", kind="not_found")
        run, results = hit
        return run, results, self._store.get_run_dataset(repo.id, run_id)

    def reusable_run(self, project_id: str, repo_id: str) -> tuple[bool, Optional[EvalRun]]:
        """The ``auto`` decision: ``(True, run)`` ⇒ answer with ``run`` and start nothing.

        A build is identified by its digest, else its tag. A fresh run for the current dev build
        is reused. With NO identity there is no build to attach an auto run to, so nothing starts
        and the latest run (or None) is returned — the tab then offers a manual run. The same
        answer while a dataset commit's build has not landed (the current build predates it). A repo
        without an agent falls through to ``start``, which refuses it."""
        repo, _ = self._resolve(project_id, repo_id)
        if not repo.agent_id:
            return False, None
        if not repo.last_dev_digest and not repo.last_dev_image_tag:
            latest = self._store.list_runs(repo.id, limit=1)
            return True, latest[0] if latest else None
        run = self._fresh(
            self._store.latest_run_for_build(
                repo.id, digest=repo.last_dev_digest, tag=repo.last_dev_image_tag
            )
        )
        if run is not None:
            return True, run
        if self._store.awaiting_build(repo.id, now=self._now()) == (
            repo.last_dev_digest or repo.last_dev_image_tag
        ):
            latest = self._store.list_runs(repo.id, limit=1)
            return True, latest[0] if latest else None
        return False, None

    def start(
        self,
        project_id: str,
        repo_id: str,
        *,
        trigger: Literal["manual", "auto"],
        principal,
        user_token: str,
        background_tasks,
    ) -> EvalRun:
        repo, project = self._resolve(project_id, repo_id)
        agent = self._agent(repo)
        self._refuse_unless_startable(repo, agent)
        st = self._load(project, repo)
        if st["errors"]:
            raise EvalError("the eval files are invalid", kind="invalid", errors=st["errors"])
        if not st["items"]:
            raise EvalError("the dataset is empty", kind="empty")
        run_id, started_at = self._new_id(), self._now()
        ttl_s = int(self._stale_after(len(st["items"])).total_seconds())
        if not self._store.acquire_run_lock(repo.id, run_id, ttl_s=ttl_s):
            raise EvalError("an evaluation run is already running for this repository", kind="running")
        try:
            obo_token = self._exchange_obo(agent, principal, user_token, repo)
            run = EvalRun(
                run_id=run_id,
                repo_id=repo.id,
                agent_id=agent.id,
                project_id=project.id,
                git_sha=st["head"] or None,
                digest=repo.last_dev_digest,
                build_tag=repo.last_dev_image_tag,
                trigger=trigger,
                started_by=principal.email or principal.oid,
                started_at=started_at,
                config=st["config"],
                item_count=len(st["items"]),
            )
            self._store.save_run(run)
        except Exception:
            self._release_lock_after_a_failed_start(repo.id, run_id)
            raise
        background_tasks.add_task(self.execute, run, st["items"], obo_token, principal)
        return run

    def _refuse_unless_startable(self, repo, agent) -> None:
        if agent.identity_status != "provisioned":
            raise EvalError("agent identity is not provisioned", kind="not_provisioned")
        if not agent.runtime_arns().get(STAGE):
            raise EvalError("the agent has no dev deployment to evaluate", kind="no_dev")
        if any(self._fresh(r) and r.status == "running" for r in self._store.list_runs(repo.id)):
            raise EvalError("an evaluation run is already running for this repository", kind="running")

    def _exchange_obo(self, agent, principal, user_token: str, repo) -> str:
        try:
            return asyncio.run(self._graph.obo_exchange(user_token, agent.entra_app_audience))
        except NotAssignedError:
            raise EvalError(
                f"{principal.email or principal.oid} is not assigned to this agent", kind="not_assigned"
            ) from None
        except OboConfigError:
            raise EvalError("agent identity is misconfigured — re-provision", kind="obo_config") from None
        except GraphError:
            logger.exception("[evals] OBO exchange failed for repo %s", repo.id)
            raise EvalError(
                "the caller's token could not be exchanged for the agent", kind="obo_failed"
            ) from None

    def _release_lock_after_a_failed_start(self, repo_id: str, run_id: str) -> None:
        try:
            self._store.release_run_lock(repo_id, run_id)
        except Exception:
            logger.exception("[evals] could not release the run lock for repo %s", repo_id)

    def _compare_with_prod(self, run: EvalRun) -> None:
        """Set ``baseline`` + ``regressions`` against the last finished run of the build serving
        prod (its digest, else its tag). Nothing to compare ⇒ both stay empty. Never raises: a
        comparison is a flag, never a reason for the run to fail."""
        try:
            repo = self._projects.get_repo(run.repo_id)
            digest, tag = repo.last_promoted_digest, repo.last_promoted_image_tag
            if not digest and not tag:
                return
            if (run.digest == digest) if digest else (run.build_tag == tag):
                return
            base = self._store.latest_run_for_build(run.repo_id, digest=digest, tag=tag, finished=True)
            if base is None or base.summary is None or run.summary is None:
                return
            build = _short_build(base)
            b_total = base.summary.checks_passed + base.summary.checks_failed
            r_total = run.summary.checks_passed + run.summary.checks_failed
            b_mean, r_mean = _judge_mean(base.summary), _judge_mean(run.summary)
            regressions = []
            if (
                b_total
                and r_total
                and run.summary.checks_passed / r_total < base.summary.checks_passed / b_total
            ):
                regressions.append(
                    f"Checks {run.summary.checks_passed}/{r_total} passed, "
                    f"prod build {build} passed {base.summary.checks_passed}/{b_total}"
                )
            if b_mean is not None and r_mean is not None and b_mean - r_mean > REGRESSION_TOLERANCE + 1e-9:
                regressions.append(f"Judges {r_mean:.2f}, down from {b_mean:.2f} on prod build {build}")
            run.baseline = RunBaseline(
                run_id=base.run_id,
                build=build,
                checks_passed=base.summary.checks_passed,
                checks_total=b_total,
                judge_mean=b_mean,
                digest=digest,
                build_tag=tag,
            )
            run.regressions = regressions
        except Exception:
            logger.exception("[evals] could not compare run %s with the prod build", run.run_id)

    @staticmethod
    def _harness_name(arn: str) -> str:
        """The harness name from its ARN: the id is ``<name>-<suffix>`` and a name has no hyphens."""
        return arn.rsplit("/", 1)[-1].rsplit("-", 1)[0]

    def _log_groups(self, logs, arn: str) -> list[str]:
        """Harness agents trace into their backing runtime's own log group as well; others: aws/spans.

        That runtime is ``harness_<name>-<id>`` under ``HARNESS_LOG_GROUP_PREFIX``, an id that need
        not match the harness's, so resolve by name prefix. The trailing ``-`` keeps ``harness_a``
        from matching ``harness_a_b`` (names have no hyphens). A lookup that fails reads as
        aws/spans only."""
        if not is_harness_arn(arn):
            return [SPANS_LOG_GROUP]
        prefix = f"{HARNESS_LOG_GROUP_PREFIX}harness_{self._harness_name(arn)}-"
        try:
            found = [
                g["logGroupName"]
                for g in logs.describe_log_groups(logGroupNamePrefix=prefix).get("logGroups", [])
            ]
        except (ClientError, BotoCoreError) as exc:
            logger.warning("[evals] could not resolve the harness log group: %s", exc)
            found = []
        return [*found, SPANS_LOG_GROUP]

    def _invoke(self, adapter, agent, item: DatasetItem, obo_token: str, principal) -> ItemResult:
        """Phase A for one item: the invoke against the dev runtime, under a memory actor of its own
        so no item answers from an earlier item's facts. A refusal the adapter answers with (an
        ``AppError`` kind) is the item's error text; any other failure is the item's error by type
        name."""
        from api.routes.agents import InvokeRequest

        started = time.monotonic()
        fresh_memory_actor = f"agp-eval-{uuid4().hex}"
        try:
            resp = asyncio.run(
                adapter.invoke(
                    agent,
                    InvokeRequest(prompt=item.input),
                    principal,
                    obo_token,
                    STAGE,
                    actor_id=fresh_memory_actor,
                )
            )
        except AppError as exc:
            return ItemResult(
                item_id=item.id, latency_ms=int((time.monotonic() - started) * 1000), error=str(exc.detail)
            )
        except Exception as exc:
            logger.exception("[evals] invoke failed for item %s", item.id)
            return ItemResult(
                item_id=item.id,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=f"invoke failed ({type(exc).__name__})",
            )
        raw = resp.get("tool_calls")
        calls = None
        if isinstance(raw, list):
            calls = [
                ToolCall.model_validate(c)
                for c in raw
                if isinstance(c, dict) and isinstance(c.get("name"), str)
            ]
        output = resp.get("response")
        return ItemResult(
            item_id=item.id,
            session_id=resp.get("session_id"),
            output=output if isinstance(output, str) else None,
            tool_calls=calls,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    @staticmethod
    def _gate(config: EvalConfig, results: list[ItemResult]) -> tuple[RunSummary, GateResult]:
        reasons: list[str] = []
        passed = failed = 0
        values: dict[str, list[float]] = {j: [] for j in config.judges}
        unscored: dict[str, int] = {}
        for r in results:
            for s in r.scores:
                if s.kind == "check":
                    if s.passed is False:
                        failed += 1
                        reasons.append(
                            f"{r.item_id}: {s.evaluator} failed — {s.explanation or s.error or 'no detail'}"
                        )
                    elif s.passed:
                        passed += 1
                elif s.value is not None:
                    values.setdefault(s.evaluator, []).append(s.value)
                else:
                    values.setdefault(s.evaluator, [])
                    unscored[s.evaluator] = unscored.get(s.evaluator, 0) + 1
        means = {j: _mean(v) for j, v in values.items()}
        reasons += _judge_reasons(means, unscored, config.gate.judge_min_mean, len(results))
        summary = RunSummary(
            items=len(results), checks_passed=passed, checks_failed=failed, judge_means=means
        )
        return summary, GateResult(passed=not reasons, reasons=reasons)

    def execute(self, run: EvalRun, items: list[DatasetItem], obo_token: str, principal) -> None:
        """Run every item, score it, save + emit + mirror. Never raises.

        Two phases, so only the invokes depend on the OBO token (about an hour of life):
        A invokes every item back to back; after ONE wait for span ingestion, B reads spans
        where needed and scores. A tenant client that cannot be built turns into errored
        judge scores (and errored tool checks that needed spans), not an errored run."""
        results: list[ItemResult] = []
        agent = None
        try:
            agent = self._registry.get(run.agent_id)
            if agent is None:
                raise EvalError("the agent no longer exists", kind="no_agent")
            arn = agent.runtime_arns().get(STAGE) or ""
            adapter = self._adapter_for(agent)
            config = run.config
            since = self._now() - SPAN_LOOKBACK
            results = [self._invoke(adapter, agent, item, obo_token, principal) for item in items]
            self._score(config, agent, arn, since, items, results)
            run.summary, run.gate = self._gate(run.config, results)
            run.status = "passed" if run.gate.passed else "failed"
            self._compare_with_prod(run)
        except Exception as exc:
            logger.exception("[evals] run %s failed", run.run_id)
            run.status = "error"
            run.gate = GateResult(passed=False, reasons=[f"the run failed ({type(exc).__name__})"])
        run.finished_at = self._now()
        self._persist(run, items, results, agent)

    def _score(
        self,
        config: EvalConfig,
        agent,
        arn: str,
        since: datetime,
        items: list[DatasetItem],
        results: list[ItemResult],
    ) -> None:
        """Phase B: one ingestion wait when any item needs spans, then per item the spans it needs
        (filling tool calls the invoke envelope did not carry), the checks and the judges. The
        tenant's clients are built on first need, once. ``since`` opens the span window and was taken
        before the first invoke, so the invokes' own duration never pushes an early item out of it."""
        if any(_needs_spans(r, config) for r in results):
            self._sleep(INGEST_WAIT_S)
        clients = _TenantClients(
            lambda service: self._client(
                service, self._stage_cfg(agent.tenant_id, STAGE), session_suffix=f"evals-{agent.id}"
            )
        )
        span_source = None
        for item, result in zip(items, results, strict=True):
            spans: list[dict] = []
            span_error = None
            if _needs_spans(result, config):
                logs, span_error = clients.get("logs")
                if logs is not None:
                    span_source = span_source or SpanSource(
                        logs, log_groups=self._log_groups(logs, arn), sleep=self._sleep
                    )
                    spans = self._session_spans(span_source, result, since)
            result.scores = self._scores(item, result, config, spans, span_error, clients)

    @staticmethod
    def _session_spans(source: SpanSource, result: ItemResult, since: datetime) -> list[dict]:
        spans = source.session_spans(
            result.session_id or "",
            since=since,
            wait_s=SPAN_WAIT_S,
            poll_s=SPAN_POLL_S,
            min_tool_spans=len(result.tool_calls or []),
        )
        if result.tool_calls is None:
            result.tool_calls = SpanSource.tool_calls_from_spans(spans)
        return spans

    @staticmethod
    def _scores(
        item: DatasetItem,
        result: ItemResult,
        config: EvalConfig,
        spans: list[dict],
        span_error: Optional[str],
        clients: _TenantClients,
    ) -> list[Score]:
        scores = list(run_checks(item, result, config.checks))
        for judge in config.judges:
            agentcore, judge_error = clients.get("bedrock-agentcore")
            error = judge_error or (span_error if not spans else None)
            if error:
                scores.append(Score(evaluator=judge, kind="judge", value=None, passed=None, error=error))
            else:
                scores.append(judge_item(agentcore, judge, spans, item))
        return scores

    def _persist(self, run: EvalRun, items: list[DatasetItem], results: list[ItemResult], agent) -> None:
        """Save, release the lock, emit and mirror; each leg is best effort and logs its own failure."""
        try:
            self._store.save_run(run, results, items, from_status="running")
        except Exception:
            logger.exception("[evals] could not save run %s", run.run_id)
        try:
            self._store.release_run_lock(run.repo_id, run.run_id)
        except Exception:
            logger.exception("[evals] could not release the run lock for run %s", run.run_id)
        try:
            self._store.emit_scores(run, results)
        except Exception:
            logger.exception("[evals] could not emit scores for run %s", run.run_id)
        if agent is not None:
            try:
                self._mirror(agent, run, items, results)
            except Exception:
                logger.exception("[evals] could not mirror run %s", run.run_id)

    def traces(self, project_id: str, repo_id: str, *, stage: str, limit: int) -> tuple[list, str]:
        """Recent sessions of the agent's ``stage`` runtime from CloudWatch spans.

        The ``service.name`` prefix is the stage-scoped runtime name ``<name>_<stage>`` for a
        container (the runtime module's ``AGENT_NAME``, which the templates use as
        ``OTEL_SERVICE_NAME``), so a single-account dev list holds no prod sessions;
        ``harness_<harness name>.DEFAULT`` for a harness (seen in its ADOT boot log; verified live on
        spans 2026-10-06/07, see ``eval_judges``)."""
        repo, _ = self._resolve(project_id, repo_id)
        agent = self._agent(repo)
        arn = agent.runtime_arns().get(stage)
        if not arn:
            return [], "none"
        prefix = (
            f"harness_{self._harness_name(arn)}.DEFAULT" if is_harness_arn(arn) else f"{agent.name}_{stage}"
        )
        try:
            logs = self._client(
                "logs", self._stage_cfg(agent.tenant_id, stage), session_suffix=f"evals-{agent.id}"
            )
            source = SpanSource(logs, log_groups=self._log_groups(logs, arn), sleep=self._sleep)
            return source.recent_sessions(
                prefix, since=self._now() - TRACES_LOOKBACK, limit=limit
            ), "cloudwatch"
        except Exception:
            logger.exception("[evals] could not read traces for repo %s", repo.id)
            return [], "none"
