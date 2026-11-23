"""The eval runner: repo file read/commit, run start, run execute.

Every collaborator is a fake: the repo provider (read_repo / read_tree / commit_files), the
connection service, the project service, the registry, the platform adapter, the Graph OBO
exchange, the span source and the judge. The store is the real in-memory ``EvalStore``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks

from core.errors import AppError, Outcome
from core.state_store import StateStore
from models.eval import DatasetItem, EvalConfig, EvalRun, ExpectedTool, RunStatus, Score
from services import eval_runner as runner_module
from services.eval_runner import EvalError, EvalRunner
from services.eval_store import EvalStore
from services.platform_adapter import InvokeError

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
HEAD = "a" * 40
DIGEST = "sha256:" + "b" * 64
DEV_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/fraud_agent-AbC"
HARNESS_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:harness/fraud_harness-XyZ"
PRINCIPAL = SimpleNamespace(oid="op-oid", email="op@x.com")


def _line(**kw) -> str:
    return json.dumps(kw)


class FakeRepos:
    """read_repo / read_file / commit_files_on_head over an in-memory tree (overlay semantics)."""

    def __init__(self, files=None, head=HEAD, default_branch="main"):
        self.files = dict(files or {"README.md": b"hi", "src/main.py": b"print(1)"})
        self.head = head
        self.default_branch = default_branch
        self.commits = []
        self.reads = []
        self.move_before_commit = False

    def read_repo(self, org, repo, *, token, base_url=None):
        return SimpleNamespace(default_branch=self.default_branch, head_sha=self.head)

    def read_file(self, org, repo, path, *, ref, token, base_url=None):
        self.reads.append((path, ref))
        return self.files.get(path)

    def read_tree(self, *a, **k):
        raise AssertionError("read_tree costs a call per file; the runner reads two files")

    def commit_files(self, *a, **k):
        raise AssertionError("commit_files replaces the tree; the runner overlays")

    def commit_files_on_head(
        self, org, repo, files, *, branch, expected_head_sha, message, token, base_url=None
    ):
        from services.github_repo_errors import GitHubRepoConflictError

        if self.move_before_commit or expected_head_sha != self.head:
            raise GitHubRepoConflictError("moved")
        self.commits.append(
            {"files": dict(files), "branch": branch, "message": message, "expected": expected_head_sha}
        )
        self.files.update(files)
        self.head = "c" * 40
        return self.head


def _repo(
    agent_id: str | None = "a-1",
    digest: str | None = DIGEST,
    tag: str | None = "t-1",
    project_id="proj-1",
    prod_digest=None,
    prod_tag=None,
):
    return SimpleNamespace(
        id="r-1",
        project_id=project_id,
        name="fraud-agent",
        agent_id=agent_id,
        last_dev_digest=digest,
        last_dev_image_tag=tag,
        last_promoted_digest=prod_digest,
        last_promoted_image_tag=prod_tag,
    )


def _agent(arns=None):
    arns = {"dev": DEV_ARN} if arns is None else arns
    return SimpleNamespace(
        id="a-1",
        name="fraud_agent",
        tenant_id="default",
        entra_app_audience="api://a-1",
        identity_status="provisioned",
        langfuse_key_secret_name=None,
        platform="agentcore",
        runtime_arns=lambda: dict(arns),
    )


class FakeAdapter:
    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []
        self.actor_ids = []

    async def invoke(self, agent, req, principal, obo_token, stage=None, actor_id=None):
        self.calls.append((req.prompt, obo_token, stage))
        self.actor_ids.append(actor_id)
        answer = self.answers.get(
            req.prompt, {"response": "ok", "session_id": "s-" + req.prompt, "tool_calls": []}
        )
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeGraph:
    def __init__(self):
        self.calls = []

    async def obo_exchange(self, user_token, audience):
        self.calls.append((user_token, audience))
        return "obo-token"


@pytest.fixture
def env():
    repos = FakeRepos()
    projects = MagicMock()
    projects.get_repo.return_value = _repo()
    projects.get_project.return_value = SimpleNamespace(
        project=SimpleNamespace(id="proj-1", connection_id="conn-1", tenant_id="default", trunk_branch="main")
    )
    connections = MagicMock()
    connections.get_connection.return_value = SimpleNamespace(org="acme", base_url=None)
    connections.get_bearer_token.return_value = "gh-token"
    registry = MagicMock()
    registry.get.return_value = _agent()
    adapter = FakeAdapter()
    graph = FakeGraph()
    store = EvalStore(store=StateStore("", clock=lambda: runner._now().timestamp()))
    mirror = MagicMock(return_value=True)
    runner = EvalRunner(
        projects=projects,
        repos=repos,
        connections=connections,
        registry=registry,
        adapter_for=lambda agent: adapter,
        graph=graph,
        store=store,
        client=lambda service, cfg, *, session_suffix: MagicMock(name=service),
        mirror=mirror,
        now=lambda: NOW,
        new_id=lambda: "run-1",
        sleep=lambda s: None,
    )
    return SimpleNamespace(
        runner=runner,
        repos=repos,
        projects=projects,
        registry=registry,
        adapter=adapter,
        graph=graph,
        store=store,
        mirror=mirror,
    )


def _with_dataset(env, *lines, config=None):
    env.repos.files["evals/dataset.jsonl"] = ("\n".join(lines) + "\n").encode()
    if config is not None:
        env.repos.files["evals/evals.yaml"] = config.encode()


def test_read_without_eval_files_returns_defaults(env):
    out = env.runner.read("proj-1", "r-1")
    assert out["configured"] is False
    assert out["config"] == EvalConfig()
    assert out["items"] == []
    assert out["errors"] == []
    assert out["head_sha"] == HEAD
    assert env.repos.reads == [("evals/evals.yaml", HEAD), ("evals/dataset.jsonl", HEAD)]


def test_read_surfaces_parse_errors_instead_of_raising(env):
    _with_dataset(env, "{not json", config="version: 2\n")
    out = env.runner.read("proj-1", "r-1")
    assert out["configured"] is True
    assert out["config"] == EvalConfig()
    assert any(e.startswith("evals/evals.yaml:") for e in out["errors"])
    assert any(e.startswith("evals/dataset.jsonl:1:") for e in out["errors"])


def test_read_unknown_repo_is_not_found(env):
    env.projects.get_repo.return_value = None
    with pytest.raises(EvalError) as exc:
        env.runner.read("proj-1", "r-1")
    assert exc.value.kind == "not_found"


def test_validate_reports_errors_and_counts():
    ok = EvalRunner.validate(config_text="judges: []\n", dataset_text=_line(id="a", input="hi") + "\n")
    assert ok == {"ok": True, "errors": [], "items": 1}
    bad = EvalRunner.validate(config_text="nope: 1\n", dataset_text="[]\n")
    assert bad["ok"] is False and len(bad["errors"]) == 2 and bad["items"] == 0


def test_put_config_overlays_evals_yaml_on_the_head(env):
    head = env.runner.put_config("proj-1", "r-1", EvalConfig(judges=[]))
    commit = env.repos.commits[-1]
    assert head == "c" * 40
    assert set(commit["files"]) == {"evals/evals.yaml"}
    assert b"judges: []" in commit["files"]["evals/evals.yaml"]
    assert commit["branch"] == "main" and commit["expected"] == HEAD
    assert "[skip ci]" not in commit["message"] and "(via AGP)" in commit["message"]
    assert env.repos.files["README.md"] == b"hi"


def test_a_push_since_the_read_is_a_conflict(env):
    env.repos.move_before_commit = True
    with pytest.raises(EvalError) as exc:
        env.runner.put_config("proj-1", "r-1", EvalConfig())
    assert exc.value.kind == "conflict"


def test_default_branch_other_than_trunk_is_refused(env):
    env.repos.default_branch = "develop"
    with pytest.raises(EvalError) as exc:
        env.runner.put_config("proj-1", "r-1", EvalConfig())
    assert exc.value.kind == "trunk_mismatch" and env.repos.commits == []


def test_put_dataset_append_merges_with_the_current_file(env):
    _with_dataset(env, _line(id="a", input="one"))
    head, count = env.runner.put_dataset("proj-1", "r-1", [DatasetItem(id="b", input="two")], mode="append")
    assert count == 2 and head == "c" * 40
    body = env.repos.commits[-1]["files"]["evals/dataset.jsonl"].decode()
    assert [json.loads(x)["id"] for x in body.splitlines()] == ["a", "b"]
    assert "[skip ci]" not in env.repos.commits[-1]["message"]
    assert set(env.repos.commits[-1]["files"]) == {"evals/dataset.jsonl"}


def test_put_dataset_replace_drops_the_old_items(env):
    _with_dataset(env, _line(id="a", input="one"))
    _, count = env.runner.put_dataset("proj-1", "r-1", [DatasetItem(id="z", input="new")], mode="replace")
    body = env.repos.commits[-1]["files"]["evals/dataset.jsonl"].decode()
    assert count == 1 and json.loads(body)["id"] == "z"


def test_put_dataset_append_refuses_a_duplicate_id(env):
    _with_dataset(env, _line(id="a", input="one"))
    with pytest.raises(EvalError) as exc:
        env.runner.put_dataset("proj-1", "r-1", [DatasetItem(id="a", input="again")], mode="append")
    assert exc.value.kind == "invalid"
    assert env.repos.commits == []


def test_put_dataset_writes_to_the_configured_path(env):
    env.repos.files["evals/evals.yaml"] = b"dataset: evals/golden.jsonl\n"
    env.runner.put_dataset("proj-1", "r-1", [DatasetItem(id="a", input="x")], mode="append")
    assert "evals/golden.jsonl" in env.repos.commits[-1]["files"]


def test_start_saves_a_running_run_and_schedules_execute(env):
    _with_dataset(env, _line(id="a", input="one"))
    tasks = BackgroundTasks()
    run = env.runner.start(
        "proj-1", "r-1", trigger="manual", principal=PRINCIPAL, user_token="user-tok", background_tasks=tasks
    )
    assert run.status == "running" and run.digest == DIGEST and run.git_sha == HEAD
    assert run.started_by == "op@x.com" and run.started_at.tzinfo is not None
    assert env.store.list_runs("r-1")[0].run_id == "run-1"
    assert env.graph.calls == [("user-tok", "api://a-1")]
    (task,) = tasks.tasks
    assert task.func == env.runner.execute
    assert isinstance(task.args[0], EvalRun)
    assert task.args[0].run_id == "run-1"
    assert isinstance(task.args[1], list)
    assert [i.id for i in task.args[1]] == ["a"]
    assert task.args[2:] == ("obo-token", PRINCIPAL)


def test_start_refuses_while_a_run_is_running(env):
    _with_dataset(env, _line(id="a", input="one"))
    env.store.save_run(
        EvalRun(
            run_id="old",
            repo_id="r-1",
            agent_id="a-1",
            project_id="proj-1",
            trigger="manual",
            started_by="x",
            started_at=NOW - timedelta(minutes=5),
            config=EvalConfig(),
        )
    )
    with pytest.raises(EvalError) as exc:
        env.runner.start(
            "proj-1",
            "r-1",
            trigger="manual",
            principal=PRINCIPAL,
            user_token="t",
            background_tasks=BackgroundTasks(),
        )
    assert exc.value.kind == "running"


def test_start_ignores_a_stale_running_run(env):
    _with_dataset(env, _line(id="a", input="one"))
    env.store.save_run(
        EvalRun(
            run_id="old",
            repo_id="r-1",
            agent_id="a-1",
            project_id="proj-1",
            trigger="manual",
            started_by="x",
            started_at=NOW - timedelta(hours=3),
            config=EvalConfig(),
        )
    )
    run = env.runner.start(
        "proj-1",
        "r-1",
        trigger="manual",
        principal=PRINCIPAL,
        user_token="t",
        background_tasks=BackgroundTasks(),
    )
    assert run.run_id == "run-1"


def test_start_refuses_an_empty_dataset(env):
    with pytest.raises(EvalError) as exc:
        env.runner.start(
            "proj-1",
            "r-1",
            trigger="manual",
            principal=PRINCIPAL,
            user_token="t",
            background_tasks=BackgroundTasks(),
        )
    assert exc.value.kind == "empty"
    assert env.graph.calls == []


def test_start_refuses_an_invalid_dataset(env):
    _with_dataset(env, "{bad")
    with pytest.raises(EvalError) as exc:
        env.runner.start(
            "proj-1",
            "r-1",
            trigger="manual",
            principal=PRINCIPAL,
            user_token="t",
            background_tasks=BackgroundTasks(),
        )
    assert exc.value.kind == "invalid" and exc.value.errors


def test_start_refuses_a_repo_without_an_agent(env):
    env.projects.get_repo.return_value = _repo(agent_id=None)
    with pytest.raises(EvalError) as exc:
        env.runner.start(
            "proj-1",
            "r-1",
            trigger="manual",
            principal=PRINCIPAL,
            user_token="t",
            background_tasks=BackgroundTasks(),
        )
    assert exc.value.kind == "no_agent"


def test_start_refuses_an_agent_without_a_dev_runtime(env):
    _with_dataset(env, _line(id="a", input="one"))
    env.registry.get.return_value = _agent(arns={"prod": DEV_ARN})
    with pytest.raises(EvalError) as exc:
        env.runner.start(
            "proj-1",
            "r-1",
            trigger="manual",
            principal=PRINCIPAL,
            user_token="t",
            background_tasks=BackgroundTasks(),
        )
    assert exc.value.kind == "no_dev"


def _saved(
    env,
    run_id,
    *,
    digest: str | None = DIGEST,
    tag=None,
    status: RunStatus = "passed",
    age=timedelta(minutes=1),
):
    run = EvalRun(
        run_id=run_id,
        repo_id="r-1",
        agent_id="a-1",
        project_id="proj-1",
        trigger="auto",
        started_by="x",
        started_at=NOW - age,
        config=EvalConfig(),
        digest=digest,
        build_tag=tag,
        status=status,
        finished_at=None if status == "running" else NOW,
    )
    env.store.save_run(run)
    return run


def test_reusable_run_returns_the_run_for_the_current_dev_digest(env):
    _saved(env, "done")
    handled, run = env.runner.reusable_run("proj-1", "r-1")
    assert handled and run.run_id == "done"
    env.projects.get_repo.return_value = _repo(digest="sha256:" + "f" * 64)
    assert env.runner.reusable_run("proj-1", "r-1") == (False, None)


def test_reusable_run_uses_the_tag_when_the_build_has_no_digest(env):
    env.projects.get_repo.return_value = _repo(digest=None, tag="t-1")
    _saved(env, "by-tag", digest=None, tag="t-1")
    handled, run = env.runner.reusable_run("proj-1", "r-1")
    assert handled and run.run_id == "by-tag"
    env.projects.get_repo.return_value = _repo(digest=None, tag="t-2")
    assert env.runner.reusable_run("proj-1", "r-1") == (False, None)


def test_reusable_run_without_build_identity_starts_nothing(env):
    env.projects.get_repo.return_value = _repo(digest=None, tag=None)
    assert env.runner.reusable_run("proj-1", "r-1") == (True, None)
    _saved(env, "latest", digest=None)
    handled, run = env.runner.reusable_run("proj-1", "r-1")
    assert handled and run.run_id == "latest"


def test_reusable_run_ignores_a_stale_running_run(env):
    _saved(env, "dead", status="running", age=timedelta(hours=3))
    assert env.runner.reusable_run("proj-1", "r-1") == (False, None)
    _saved(env, "live", status="running", age=timedelta(minutes=5))
    assert env.runner.reusable_run("proj-1", "r-1")[1].run_id == "live"


def test_the_stale_cutoff_scales_with_the_runs_item_count(env):
    """min(2 h, 10 min + 3 min x items): a 3-item run is dead after 19 min, not 2 h."""
    _with_dataset(env, _line(id="a", input="one"), _line(id="b", input="two"))
    started = env.runner.start(
        "proj-1",
        "r-1",
        trigger="manual",
        principal=PRINCIPAL,
        user_token="t",
        background_tasks=BackgroundTasks(),
    )
    assert started.item_count == 2
    env.store = EvalStore()
    env.runner._store = env.store

    def running(run_id, age, count):
        env.store.save_run(
            EvalRun(
                run_id=run_id,
                repo_id="r-1",
                agent_id="a-1",
                project_id="proj-1",
                trigger="auto",
                started_by="x",
                started_at=NOW - age,
                config=EvalConfig(),
                digest=DIGEST,
                item_count=count,
            )
        )

    running("young", timedelta(minutes=18), 3)
    assert env.runner.reusable_run("proj-1", "r-1")[1].run_id == "young"
    with pytest.raises(EvalError):
        env.runner.start(
            "proj-1",
            "r-1",
            trigger="manual",
            principal=PRINCIPAL,
            user_token="t",
            background_tasks=BackgroundTasks(),
        )
    env.store = EvalStore()
    env.runner._store = env.store
    running("dead", timedelta(minutes=20), 3)
    assert env.runner.reusable_run("proj-1", "r-1") == (False, None)
    assert (
        env.runner.start(
            "proj-1",
            "r-1",
            trigger="manual",
            principal=PRINCIPAL,
            user_token="t",
            background_tasks=BackgroundTasks(),
        ).run_id
        == "run-1"
    )
    env.store = EvalStore()
    env.runner._store = env.store
    running("big", timedelta(minutes=119), 50)
    assert env.runner.reusable_run("proj-1", "r-1")[1].run_id == "big"


def test_reusable_run_without_an_agent_falls_through_to_start(env):
    env.projects.get_repo.return_value = _repo(agent_id=None)
    assert env.runner.reusable_run("proj-1", "r-1") == (False, None)


def test_start_stamps_the_build_tag(env):
    _with_dataset(env, _line(id="a", input="one"))
    run = env.runner.start(
        "proj-1",
        "r-1",
        trigger="manual",
        principal=PRINCIPAL,
        user_token="t",
        background_tasks=BackgroundTasks(),
    )
    assert run.build_tag == "t-1" and run.digest == DIGEST


def test_start_refuses_an_unprovisioned_agent(env):
    _with_dataset(env, _line(id="a", input="one"))
    agent = _agent()
    agent.identity_status = "pending"
    env.registry.get.return_value = agent
    with pytest.raises(EvalError) as exc:
        env.runner.start(
            "proj-1",
            "r-1",
            trigger="manual",
            principal=PRINCIPAL,
            user_token="t",
            background_tasks=BackgroundTasks(),
        )
    assert exc.value.kind == "not_provisioned" and env.graph.calls == []


def test_start_maps_a_graph_error(env):
    from services.graph_errors import GraphError

    _with_dataset(env, _line(id="a", input="one"))

    async def boom(user_token, audience):
        raise GraphError(500, "unknown")

    env.graph.obo_exchange = boom
    with pytest.raises(EvalError) as exc:
        env.runner.start(
            "proj-1",
            "r-1",
            trigger="manual",
            principal=PRINCIPAL,
            user_token="t",
            background_tasks=BackgroundTasks(),
        )
    assert exc.value.kind == "obo_failed"
    assert env.store.list_runs("r-1") == []


def test_a_repo_of_another_project_is_not_found(env):
    env.projects.get_repo.return_value = _repo(project_id="proj-2")
    for call in (
        lambda: env.runner.read("proj-1", "r-1"),
        lambda: env.runner.put_config("proj-1", "r-1", EvalConfig()),
        lambda: env.runner.get_run("proj-1", "r-1", "x"),
        lambda: env.runner.reusable_run("proj-1", "r-1"),
    ):
        with pytest.raises(EvalError) as exc:
            call()
        assert exc.value.kind == "not_found"
    assert env.repos.reads == [] and env.repos.commits == []


def test_get_run_returns_the_dataset_as_run(env):
    run = _run()
    items = [DatasetItem(id="a", input="one", expected_tools=[ExpectedTool(name="t1")])]
    env.store.save_run(run, [], items)
    got_run, results, dataset = env.runner.get_run("proj-1", "r-1", "run-1")
    assert got_run.run_id == "run-1" and results == [] and dataset == items


@pytest.mark.parametrize(
    "path",
    [
        "../dataset.jsonl",
        "evals/../x.jsonl",
        ".github/workflows/x.yml",
        "/evals/x.jsonl",
        "evals/evals.yaml",
        "evals/sub/x.jsonl",
        "evals/x.json",
        "deploy.sh",
    ],
)
def test_dataset_path_outside_evals_jsonl_is_refused_by_the_model(path):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EvalConfig(dataset=path)


@pytest.mark.parametrize(
    "path", ["../dataset.jsonl", ".github/workflows/x.yml", "/abs.jsonl", "evals/evals.yaml"]
)
def test_runner_rechecks_the_path_before_any_commit(env, path):
    with pytest.raises(EvalError) as exc:
        env.runner.put_config(
            "proj-1", "r-1", EvalConfig.model_construct(**{**EvalConfig().__dict__, "dataset": path})
        )
    assert exc.value.kind == "invalid" and env.repos.commits == []


def test_an_evals_yaml_naming_a_workflow_is_an_error_and_never_written(env):
    env.repos.files["evals/evals.yaml"] = b"dataset: .github/workflows/build.yml\n"
    out = env.runner.read("proj-1", "r-1")
    assert out["errors"] and ".github/workflows/build.yml" not in [p for p, _ in env.repos.reads]
    with pytest.raises(EvalError) as exc:
        env.runner.put_dataset("proj-1", "r-1", [DatasetItem(id="a", input="x")], mode="append")
    assert exc.value.kind == "invalid" and env.repos.commits == []


def test_deeply_nested_files_are_errors_not_crashes(env):
    env.repos.files["evals/evals.yaml"] = ("judges: " + "[" * 5000).encode()
    env.repos.files["evals/dataset.jsonl"] = ('{"id":"a","input":"x","tags":' + "[" * 100000 + "\n").encode()
    out = env.runner.read("proj-1", "r-1")
    assert len(out["errors"]) >= 2
    res = EvalRunner.validate(config_text="judges: " + "[" * 5000, dataset_text="[" * 100000)
    assert res["ok"] is False


def test_oversized_files_are_errors(env):
    env.repos.files["evals/dataset.jsonl"] = b"x" * (runner_module.MAX_FILE_BYTES + 1)
    out = env.runner.read("proj-1", "r-1")
    assert any("larger than" in e for e in out["errors"])


def _run(cfg=None, run_id="run-1"):
    return EvalRun(
        run_id=run_id,
        repo_id="r-1",
        agent_id="a-1",
        project_id="proj-1",
        trigger="manual",
        started_by="op@x.com",
        started_at=NOW,
        config=cfg or EvalConfig(judges=[]),
        digest=DIGEST,
    )


def _execute(env, run, items):
    """The background task over a run ``start`` saved: the running row is the task's precondition."""
    env.store.save_run(run)
    env.runner.execute(run, items, "obo", PRINCIPAL)


@pytest.fixture
def spans(monkeypatch):
    """A fake SpanSource class: records constructor args and session reads."""
    record = SimpleNamespace(
        reads=[], min_tool_spans=[], log_groups=None, spans=[{"span": 1}], tool_calls=None
    )

    class FakeSpanSource:
        def __init__(self, logs, *, log_groups=None, sleep=None):
            record.log_groups = log_groups

        def session_spans(self, session_id, *, since, wait_s=180, poll_s=15, min_tool_spans=0):
            record.reads.append((session_id, poll_s, since))
            record.min_tool_spans.append(min_tool_spans)
            return record.spans

        @staticmethod
        def tool_calls_from_spans(s):
            return record.tool_calls

    monkeypatch.setattr(runner_module, "SpanSource", FakeSpanSource)
    return record


def _stored(env):
    run, results = env.store.get_run("r-1", "run-1")
    return run, results


def test_execute_passes_the_gate(env, spans):
    items = [DatasetItem(id="a", input="one", expected_tools=[ExpectedTool(name="t1")])]
    env.adapter.answers["one"] = {
        "response": "done",
        "session_id": "s-1",
        "tool_calls": [{"name": "t1", "args": {}}],
    }
    _execute(env, _run(), items)
    run, results = _stored(env)
    assert run.status == "passed" and run.gate.passed and run.finished_at is not None
    assert run.summary.items == 1 and run.summary.checks_failed == 0 and run.summary.checks_passed == 2
    assert results[0].output == "done" and results[0].session_id == "s-1"
    assert env.adapter.calls == [("one", "obo", "dev")]
    assert spans.reads == []


def test_execute_gives_every_item_its_own_memory_actor(env, spans):
    """Harness memory is actor-scoped: with one shared actor an item answers from an earlier item's
    facts without calling the tool."""
    items = [DatasetItem(id="a", input="one"), DatasetItem(id="b", input="two")]
    _execute(env, _run(), items)
    _execute(env, _run(run_id="run-2"), items)
    ids = env.adapter.actor_ids
    assert len(ids) == 4 and all(i and i.startswith("agp-eval-") for i in ids) and len(set(ids)) == 4


def test_execute_check_failure_fails_with_reasons(env, spans):
    items = [DatasetItem(id="a", input="one", expected_tools=[ExpectedTool(name="t2")])]
    _execute(env, _run(), items)
    run, _ = _stored(env)
    assert run.status == "failed" and not run.gate.passed
    assert any("a" in r and "tools" in r for r in run.gate.reasons)


def test_execute_judge_mean_below_threshold_fails(env, spans, monkeypatch):
    values = iter([0.9, 0.5])
    monkeypatch.setattr(
        runner_module,
        "judge_item",
        lambda client, judge, s, item: Score(evaluator=judge, kind="judge", value=next(values), passed=None),
    )
    cfg = EvalConfig(judges=["Builtin.Correctness"])
    items = [DatasetItem(id="a", input="one"), DatasetItem(id="b", input="two")]
    _execute(env, _run(cfg), items)
    run, results = _stored(env)
    assert run.summary.judge_means == {"Builtin.Correctness": pytest.approx(0.7)}
    assert run.status == "failed"
    assert any("Builtin.Correctness" in r for r in run.gate.reasons)
    assert len(spans.reads) == 2 and spans.reads[0][1] == 5
    assert results[0].scores[-1].kind == "judge"


def test_execute_judge_with_no_values_fails_the_gate(env, spans, monkeypatch):
    monkeypatch.setattr(
        runner_module,
        "judge_item",
        lambda client, judge, s, item: Score(
            evaluator=judge, kind="judge", value=None, passed=None, error="x"
        ),
    )
    _execute(env, _run(EvalConfig(judges=["Builtin.Correctness"])), [DatasetItem(id="a", input="one")])
    run, _ = _stored(env)
    assert run.summary.judge_means == {"Builtin.Correctness": None}
    assert run.status == "failed"


def test_execute_judge_with_any_unscored_item_fails_the_gate(env, spans, monkeypatch):
    values = iter([1.0, None, None])
    monkeypatch.setattr(
        runner_module,
        "judge_item",
        lambda client, judge, s, item: (
            lambda v: Score(
                evaluator=judge,
                kind="judge",
                value=v,
                passed=None,
                error=None if v is not None else "no spans",
            )
        )(next(values)),
    )
    items = [DatasetItem(id=i, input=i) for i in ("a", "b", "c")]
    _execute(env, _run(EvalConfig(judges=["Builtin.Correctness"])), items)
    run, _ = _stored(env)
    assert run.summary.judge_means == {"Builtin.Correctness": 1.0}
    assert run.status == "failed"
    assert "Builtin.Correctness: 2/3 items unscored" in run.gate.reasons


def test_execute_invoke_error_fails_the_no_error_check(env, spans):
    env.adapter.answers["one"] = InvokeError("agent invocation timed out", "timed_out")
    _execute(env, _run(), [DatasetItem(id="a", input="one")])
    run, results = _stored(env)
    assert results[0].error == "agent invocation timed out"
    no_error = next(s for s in results[0].scores if s.evaluator == "no_error")
    assert no_error.passed is False
    assert run.status == "failed"


def test_execute_adapter_refusal_detail_is_the_item_error(env, spans):
    class _Refused(AppError):
        domain = "scratch"
        kinds: ClassVar = {"contained": Outcome(409, "agent is contained")}

    env.adapter.answers["one"] = _Refused("agent is contained", "contained")
    _execute(env, _run(), [DatasetItem(id="a", input="one")])
    run, results = _stored(env)
    assert results[0].error == "agent is contained" and run.status == "failed"


def test_execute_falls_back_to_spans_for_missing_tool_calls(env, spans):
    from models.eval import ToolCall

    spans.tool_calls = [ToolCall(name="t1", args={"x": 1})]
    env.adapter.answers["one"] = {"response": "r", "session_id": "s-1", "tool_calls": None}
    items = [DatasetItem(id="a", input="one", expected_tools=[ExpectedTool(name="t1", args={"x": 1})])]
    _execute(env, _run(), items)
    run, results = _stored(env)
    assert [s for s, _, _ in spans.reads] == ["s-1"]
    assert results[0].tool_calls[0].name == "t1" and results[0].tool_calls[0].args == {"x": 1}
    assert run.status == "passed"


def test_execute_span_read_waits_for_one_tool_span_per_reported_tool_call(env, spans, monkeypatch):
    """Live 2026-10-07: judged before the execute_tool span landed — the read must wait for it."""
    monkeypatch.setattr(
        runner_module,
        "judge_item",
        lambda client, judge, s, item: Score(evaluator=judge, kind="judge", value=1.0, passed=None),
    )
    env.adapter.answers["one"] = {
        "response": "391",
        "session_id": "s-1",
        "tool_calls": [{"name": "calculator", "args": {"expression": "17 * 23"}}],
    }
    env.adapter.answers["two"] = {"response": "r", "session_id": "s-2", "tool_calls": None}
    items = [DatasetItem(id="a", input="one"), DatasetItem(id="b", input="two")]
    _execute(env, _run(EvalConfig(judges=["Builtin.GoalSuccessRate"])), items)
    assert spans.min_tool_spans == [1, 0]


def test_execute_harness_reads_its_own_log_group_too(env, spans):
    """The backing runtime is ``harness_<name>-<its own id>``; the trailing ``-`` in the prefix keeps
    ``harness_<name>_<more>`` out."""
    env.registry.get.return_value = _agent(arns={"dev": HARNESS_ARN})
    logs = MagicMock()
    logs.describe_log_groups.return_value = {
        "logGroups": [{"logGroupName": "/aws/bedrock-agentcore/runtimes/harness_fraud_harness-Rt9-DEFAULT"}]
    }
    env.runner._client = lambda service, cfg, *, session_suffix: logs
    env.adapter.answers["one"] = {"response": "r", "session_id": "s-1", "tool_calls": None}
    _execute(env, _run(), [DatasetItem(id="a", input="one")])
    assert spans.log_groups == [
        "/aws/bedrock-agentcore/runtimes/harness_fraud_harness-Rt9-DEFAULT",
        "aws/spans",
    ]
    logs.describe_log_groups.assert_called_once_with(
        logGroupNamePrefix="/aws/bedrock-agentcore/runtimes/harness_fraud_harness-"
    )


def test_execute_emits_and_mirrors(env, spans):
    emitted = []
    env.store.emit_scores = lambda run, results: emitted.append((run.run_id, len(results)))
    _execute(env, _run(), [DatasetItem(id="a", input="one")])
    assert emitted == [("run-1", 1)]
    env.mirror.assert_called_once()
    agent, run, items, results = env.mirror.call_args.args
    assert agent.id == "a-1" and run.status == "passed" and items[0].id == "a" and len(results) == 1


def test_execute_unexpected_exception_saves_an_error_run(env, spans, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(runner_module, "run_checks", boom)
    run = _run()
    env.store.save_run(run)
    saved = []
    real = env.store.save_run
    env.store.save_run = lambda run, results=None, dataset=None, **kw: (
        saved.append(run.status),
        real(run, results, dataset, **kw),
    )
    env.runner.execute(run, [DatasetItem(id="a", input="one")], "obo", PRINCIPAL)
    run, _ = _stored(env)
    assert saved == ["error"] and run.status == "error" and run.finished_at is not None
    assert run.gate is not None and run.gate.passed is False


def test_execute_never_raises_when_the_agent_is_gone(env, spans):
    env.registry.get.return_value = None
    _execute(env, _run(), [DatasetItem(id="a", input="one")])
    run, _ = _stored(env)
    assert run.status == "error"


def test_execute_invokes_every_item_before_any_span_read(env, spans, monkeypatch):
    order = []
    real_invoke = env.adapter.invoke

    async def invoke(agent, req, principal, obo_token, stage=None, actor_id=None):
        order.append(("invoke", req.prompt))
        return await real_invoke(agent, req, principal, obo_token, stage, actor_id)

    env.adapter.invoke = invoke
    monkeypatch.setattr(spans, "reads", SimpleNamespace(append=lambda r: order.append(("spans", r[0]))))
    slept = []
    env.runner._sleep = slept.append
    monkeypatch.setattr(
        runner_module,
        "judge_item",
        lambda client, judge, s, item: Score(evaluator=judge, kind="judge", value=1.0, passed=None),
    )
    items = [DatasetItem(id="a", input="one"), DatasetItem(id="b", input="two")]
    _execute(env, _run(EvalConfig(judges=["Builtin.Correctness"])), items)
    assert order == [("invoke", "one"), ("invoke", "two"), ("spans", "s-one"), ("spans", "s-two")]
    assert slept == [runner_module.INGEST_WAIT_S]
    assert _stored(env)[0].status == "passed"


def test_span_reads_open_their_window_before_the_first_invoke(env, spans, monkeypatch):
    """With a real clock the invokes take minutes; the Logs Insights window must start before the
    first of them, or the early items' spans fall outside it."""
    clock = [NOW]
    env.runner._now = lambda: clock[0]
    real_invoke = env.adapter.invoke
    invoked_at = []

    async def invoke(agent, req, principal, obo_token, stage=None, actor_id=None):
        invoked_at.append(clock[0])
        clock[0] += timedelta(minutes=5)
        return await real_invoke(agent, req, principal, obo_token, stage, actor_id)

    env.adapter.invoke = invoke
    monkeypatch.setattr(
        runner_module,
        "judge_item",
        lambda client, judge, s, item: Score(evaluator=judge, kind="judge", value=1.0, passed=None),
    )
    items = [DatasetItem(id=i, input=i) for i in ("a", "b", "c")]
    _execute(env, _run(EvalConfig(judges=["Builtin.Correctness"])), items)
    assert len(spans.reads) == 3 and len(invoked_at) == 3
    assert all(since < invoked_at[0] for _, _, since in spans.reads)


def test_execute_no_wait_when_nothing_needs_spans(env, spans):
    slept = []
    env.runner._sleep = slept.append
    _execute(env, _run(), [DatasetItem(id="a", input="one")])
    assert slept == [] and spans.reads == []


def test_execute_client_failure_scores_errors_not_a_run_error(env, spans):
    def no_client(service, cfg, *, session_suffix):
        raise RuntimeError("assume role denied")

    env.runner._client = no_client
    env.adapter.answers["one"] = {
        "response": "r",
        "session_id": "s-1",
        "tool_calls": [{"name": "t1", "args": {}}],
    }
    env.adapter.answers["two"] = {"response": "r", "session_id": "s-2", "tool_calls": None}
    items = [
        DatasetItem(id="a", input="one", expected_tools=[ExpectedTool(name="t1")]),
        DatasetItem(id="b", input="two", expected_tools=[ExpectedTool(name="t1")]),
    ]
    _execute(env, _run(EvalConfig(judges=["Builtin.Correctness"])), items)
    run, results = _stored(env)
    assert run.status == "failed"
    judge = next(s for s in results[0].scores if s.kind == "judge")
    assert judge.value is None and "client unavailable" in judge.error
    tools_a = next(s for s in results[0].scores if s.evaluator == "tools")
    assert tools_a.passed is True
    tools_b = next(s for s in results[1].scores if s.evaluator == "tools")
    assert tools_b.passed is False and tools_b.error == "no tool-call data"
    assert spans.reads == []


@pytest.mark.parametrize(
    "arn,stage,prefix",
    [
        (DEV_ARN, "dev", "fraud_agent_dev"),
        (DEV_ARN, "prod", "fraud_agent_prod"),
        (HARNESS_ARN, "dev", "harness_fraud_harness.DEFAULT"),
    ],
)
def test_traces_filter_on_the_stage_scoped_service_name(env, monkeypatch, arn, stage, prefix):
    """A container's service.name is the runtime name ``<agent>_<stage>`` (agentcore_runtime AGENT_NAME)."""
    seen = []

    class FakeSpanSource:
        def __init__(self, logs, *, log_groups=None, sleep=None):
            pass

        def recent_sessions(self, service_name_prefix, *, since, limit=50):
            seen.append(service_name_prefix)
            return []

    monkeypatch.setattr(runner_module, "SpanSource", FakeSpanSource)
    env.registry.get.return_value = _agent(arns={stage: arn})
    assert env.runner.traces("proj-1", "r-1", stage=stage, limit=10) == ([], "cloudwatch")
    assert seen == [prefix]


PROD_DIGEST = "sha256:" + "e" * 64
PROD_TAG = "fraud_agent-b1a0bca"


def _prod_run(
    env,
    *,
    digest: str | None = PROD_DIGEST,
    tag=PROD_TAG,
    passed=12,
    failed=0,
    means=None,
    status: RunStatus = "passed",
    run_id="prod-run",
):
    from models.eval import GateResult, RunSummary

    run = EvalRun(
        run_id=run_id,
        repo_id="r-1",
        agent_id="a-1",
        project_id="proj-1",
        trigger="manual",
        started_by="x",
        started_at=NOW - timedelta(days=1),
        finished_at=NOW - timedelta(days=1),
        config=EvalConfig(),
        digest=digest,
        build_tag=tag,
        status=status,
        summary=RunSummary(
            items=6,
            checks_passed=passed,
            checks_failed=failed,
            judge_means=means if means is not None else {"Builtin.Correctness": 0.86},
        ),
        gate=GateResult(passed=status == "passed"),
    )
    env.store.save_run(run)
    return run


def _judging(monkeypatch, value):
    monkeypatch.setattr(
        runner_module,
        "judge_item",
        lambda client, judge, s, item: Score(evaluator=judge, kind="judge", value=value, passed=None),
    )


def _execute_judged(env, value, monkeypatch, *, expected_tool="t1"):
    _judging(monkeypatch, value)
    items = [DatasetItem(id="a", input="one", expected_tools=[ExpectedTool(name=expected_tool)])]
    env.adapter.answers["one"] = {
        "response": "done",
        "session_id": "s-1",
        "tool_calls": [{"name": "t1", "args": {}}],
    }
    _execute(env, _run(EvalConfig(judges=["Builtin.Correctness"])), items)
    return _stored(env)[0]


def test_a_run_records_the_prod_builds_last_run_as_its_baseline(env, spans, monkeypatch):
    env.projects.get_repo.return_value = _repo(prod_digest=PROD_DIGEST, prod_tag=PROD_TAG)
    _prod_run(env, passed=12, failed=0)
    run = _execute_judged(env, 0.9, monkeypatch)
    assert run.baseline is not None
    assert (run.baseline.run_id, run.baseline.build) == ("prod-run", "b1a0bca")
    assert (run.baseline.checks_passed, run.baseline.checks_total, run.baseline.judge_mean) == (12, 12, 0.86)
    assert run.regressions == []


def test_a_lower_check_ratio_is_a_regression(env, spans, monkeypatch):
    env.projects.get_repo.return_value = _repo(prod_digest=PROD_DIGEST, prod_tag=PROD_TAG)
    _prod_run(env, passed=12, failed=0)
    run = _execute_judged(env, 0.9, monkeypatch, expected_tool="t2")
    assert run.regressions == ["Checks 1/2 passed, prod build b1a0bca passed 12/12"]


def test_a_judge_drop_beyond_the_tolerance_flags_but_does_not_block(env, spans, monkeypatch):
    env.projects.get_repo.return_value = _repo(prod_digest=PROD_DIGEST)
    _prod_run(env, means={"Builtin.Correctness": 0.95, "Builtin.GoalSuccessRate": None})
    run = _execute_judged(env, 0.82, monkeypatch)
    assert run.regressions == ["Judges 0.82, down from 0.95 on prod build b1a0bca"]
    assert run.status == "passed" and run.gate.passed


def test_a_judge_drop_within_the_tolerance_is_not_a_regression(env, spans, monkeypatch):
    env.projects.get_repo.return_value = _repo(prod_digest=PROD_DIGEST)
    _prod_run(env, means={"Builtin.Correctness": 0.86})
    run = _execute_judged(env, 0.82, monkeypatch)
    assert run.baseline is not None and run.regressions == []


def test_the_baseline_matches_the_prod_tag_when_prod_has_no_digest(env, spans, monkeypatch):
    env.projects.get_repo.return_value = _repo(prod_tag=PROD_TAG)
    _prod_run(env, digest=None, run_id="by-tag")
    _prod_run(env, digest=PROD_DIGEST, tag="other-tag", run_id="other")
    run = _execute_judged(env, 0.9, monkeypatch)
    assert run.baseline.run_id == "by-tag"


def test_the_baseline_skips_an_errored_prod_run(env, spans, monkeypatch):
    env.projects.get_repo.return_value = _repo(prod_digest=PROD_DIGEST)
    _prod_run(env, status="error", run_id="errored")
    run = _execute_judged(env, 0.9, monkeypatch)
    assert run.baseline is None and run.regressions == []


@pytest.mark.parametrize("prod", [{}, {"prod_digest": DIGEST}])
def test_no_baseline_without_a_prod_build_or_for_the_prod_build_itself(env, spans, monkeypatch, prod):
    env.projects.get_repo.return_value = _repo(**prod)
    _prod_run(env, digest=DIGEST)
    run = _execute_judged(env, 0.5, monkeypatch)
    assert run.baseline is None and run.regressions == []


def test_a_baseline_lookup_failure_never_fails_the_run(env, spans, monkeypatch):
    env.projects.get_repo.return_value = _repo(prod_digest=PROD_DIGEST)
    env.store.latest_run_for_build = MagicMock(side_effect=RuntimeError("ddb down"))
    run = _execute_judged(env, 0.9, monkeypatch)
    assert run.status == "passed" and run.baseline is None and run.regressions == []


def _start(env, **kw):
    return env.runner.start(
        "proj-1",
        "r-1",
        trigger="manual",
        principal=PRINCIPAL,
        user_token="t",
        background_tasks=kw.get("tasks") or BackgroundTasks(),
    )


def test_two_starts_racing_past_the_running_check_cannot_both_run(env, monkeypatch):
    _with_dataset(env, _line(id="a", input="one"))
    monkeypatch.setattr(env.store, "list_runs", lambda repo_id, limit=20: [])
    ids = iter(["run-1", "run-2"])
    env.runner._new_id = lambda: next(ids)
    assert _start(env).run_id == "run-1"
    with pytest.raises(EvalError) as exc:
        _start(env)
    assert exc.value.kind == "running"
    assert env.store.get_run("r-1", "run-2") is None


def test_the_lock_expires_on_the_stale_cutoff(env, monkeypatch):
    _with_dataset(env, _line(id="a", input="one"))
    monkeypatch.setattr(env.store, "list_runs", lambda repo_id, limit=20: [])
    ids = iter(["run-1", "run-2", "run-3"])
    env.runner._new_id = lambda: next(ids)
    _start(env)
    env.runner._now = lambda: NOW + timedelta(minutes=12)
    with pytest.raises(EvalError):
        _start(env)
    env.runner._now = lambda: NOW + timedelta(minutes=14)
    assert _start(env).run_id == "run-3"


def test_a_finished_run_releases_the_lock(env, spans, monkeypatch):
    _with_dataset(env, _line(id="a", input="one"))
    monkeypatch.setattr(env.store, "list_runs", lambda repo_id, limit=20: [])
    ids = iter(["run-1", "run-2"])
    env.runner._new_id = lambda: next(ids)
    tasks = BackgroundTasks()
    _start(env, tasks=tasks)
    (task,) = tasks.tasks
    task.func(*task.args, **task.kwargs)
    assert _start(env).run_id == "run-2"


def test_a_failed_obo_exchange_releases_the_lock(env, monkeypatch):
    from services.graph_errors import GraphError

    _with_dataset(env, _line(id="a", input="one"))

    async def fail(user_token, audience):
        raise GraphError(500, "unknown")

    env.graph.obo_exchange = fail
    with pytest.raises(EvalError) as exc:
        _start(env)
    assert exc.value.kind == "obo_failed"
    assert env.store.acquire_run_lock("r-1", "probe", ttl_s=60)


def test_a_judge_drop_of_exactly_the_tolerance_is_not_a_regression(env, spans, monkeypatch):
    env.projects.get_repo.return_value = _repo(prod_digest=PROD_DIGEST)
    _prod_run(env, means={"Builtin.Correctness": 0.20})
    run = _execute_judged(env, 0.15, monkeypatch)
    assert run.baseline is not None and run.regressions == []


def test_the_baseline_records_the_prod_build_identity(env, spans, monkeypatch):
    env.projects.get_repo.return_value = _repo(prod_digest=PROD_DIGEST, prod_tag=PROD_TAG)
    _prod_run(env)
    run = _execute_judged(env, 0.9, monkeypatch)
    assert (run.baseline.digest, run.baseline.build_tag) == (PROD_DIGEST, PROD_TAG)


def test_a_failing_lock_release_does_not_mask_the_start_error(env, monkeypatch, caplog):
    from services.graph_errors import GraphError

    _with_dataset(env, _line(id="a", input="one"))

    async def fail(user_token, audience):
        raise GraphError(500, "unknown")

    env.graph.obo_exchange = fail
    monkeypatch.setattr(env.store, "release_run_lock", MagicMock(side_effect=RuntimeError("ddb down")))
    with pytest.raises(EvalError) as exc:
        _start(env)
    assert exc.value.kind == "obo_failed"
    assert "could not release the run lock" in caplog.text


def test_a_dataset_commit_starts_no_auto_run_on_the_old_build(env):
    _with_dataset(env, _line(id="a", input="one"))
    env.runner.put_dataset("proj-1", "r-1", [DatasetItem(id="b", input="two")], mode="append")
    assert env.runner.reusable_run("proj-1", "r-1") == (True, None)
    env.projects.get_repo.return_value = _repo(digest="sha256:" + "f" * 64)
    assert env.runner.reusable_run("proj-1", "r-1") == (False, None)


def test_a_dataset_commit_on_a_tag_only_build_waits_too(env):
    env.projects.get_repo.return_value = _repo(digest=None, tag="t-1")
    _with_dataset(env, _line(id="a", input="one"))
    env.runner.put_dataset("proj-1", "r-1", [DatasetItem(id="b", input="two")], mode="append")
    assert env.runner.reusable_run("proj-1", "r-1") == (True, None)
    env.projects.get_repo.return_value = _repo(digest=None, tag="t-2")
    assert env.runner.reusable_run("proj-1", "r-1") == (False, None)


def test_the_wait_for_the_build_expires(env):
    from services.eval_runner import AWAIT_BUILD

    _with_dataset(env, _line(id="a", input="one"))
    env.runner.put_dataset("proj-1", "r-1", [DatasetItem(id="b", input="two")], mode="append")
    env.runner._now = lambda: NOW + AWAIT_BUILD
    assert env.runner.reusable_run("proj-1", "r-1") == (False, None)


def test_while_waiting_the_latest_run_is_returned(env):
    old = _saved(env, "old", digest="sha256:" + "0" * 64)
    _with_dataset(env, _line(id="a", input="one"))
    env.runner.put_dataset("proj-1", "r-1", [DatasetItem(id="b", input="two")], mode="append")
    handled, run = env.runner.reusable_run("proj-1", "r-1")
    assert handled and run.run_id == old.run_id
