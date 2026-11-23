"""The ``/projects/{id}/repos/{repo_id}/evals`` routes.

Fixture idiom is ``test_projects_role_gating.py``: the real ``require_role`` /
``current_principal`` / tenant + project ctx chain against a mocked ``verify_entra_token``,
fake resolvers on the ``users`` module, ``MagicMock`` services on the ``projects`` module. The
runner on ``evals._runner`` is a REAL ``EvalRunner`` over the fakes of ``test_eval_runner``, so
a started run's background task actually executes against the fake adapter.
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from test_eval_runner import DIGEST, HEAD, NOW, FakeAdapter, FakeGraph, FakeRepos, _agent, _repo
from test_projects_role_gating import (
    _claims_for,
    _detail,
    _FakeProjectResolver,
    _FakeTenantResolver,
    _FakeTenantService,
    _project_context,
    _tenant_context,
)

from models.eval import EvalConfig, EvalRun
from services.eval_store import EvalStore

pytestmark = pytest.mark.usefixtures("reset_modules")

BASE = "/api/v1/projects/proj-1/repos/r-1/evals"
_FAKE_TOKEN = "ghp_" + "0" * 36


@pytest.fixture
def env(entra_settings):
    from services.eval_runner import EvalRunner

    repos = FakeRepos()
    projects = MagicMock()
    projects.get_project.return_value = SimpleNamespace(
        project=SimpleNamespace(id="proj-1", connection_id="conn-1", tenant_id="default", trunk_branch="main")
    )
    projects.get_repo.return_value = _repo()
    connections = MagicMock()
    connections.get_connection.return_value = SimpleNamespace(org="acme", base_url=None)
    connections.get_bearer_token.return_value = "gh-token"
    registry = MagicMock()
    registry.get.return_value = _agent()
    adapter = FakeAdapter()
    graph = FakeGraph()
    store = EvalStore()
    ids = iter(f"run-{n}" for n in range(1, 100))
    runner = EvalRunner(
        projects=projects,
        repos=repos,
        connections=connections,
        registry=registry,
        adapter_for=lambda agent: adapter,
        graph=graph,
        store=store,
        client=lambda service, cfg, *, session_suffix: MagicMock(),
        mirror=MagicMock(return_value=False),
        now=lambda: NOW,
        new_id=lambda: next(ids),
        sleep=lambda s: None,
    )
    role_svc = MagicMock()
    role_svc.has_role_rows.return_value = False
    role_svc.list_for_project_strict.return_value = []
    project_svc = MagicMock()
    project_svc.get_project.return_value = _detail()
    return SimpleNamespace(
        runner=runner,
        repos=repos,
        projects=projects,
        registry=registry,
        adapter=adapter,
        graph=graph,
        store=store,
        role_svc=role_svc,
        project_svc=project_svc,
        patchers=[],
    )


@pytest.fixture
def client_for(env):
    def _make(*, platform_role="operator", project_role="maintainer"):
        import api.routes.evals as evals_module
        import api.routes.projects as projects_module
        import api.routes.tenants as tenants_module
        import api.routes.users as users_module
        from core.errors import install_error_handlers

        users_module._tenant_resolver = _FakeTenantResolver(_tenant_context())
        users_module._project_resolver = _FakeProjectResolver(
            _project_context(is_global=platform_role == "admin", role=project_role)
        )
        tenants_module._svc = _FakeTenantService({"default"})
        projects_module._svc = env.project_svc
        projects_module._role_svc = env.role_svc
        evals_module._runner = env.runner

        app = FastAPI()
        install_error_handlers(app)
        app.include_router(evals_module.router, prefix="/api/v1")
        patcher = patch("core.security_entra.verify_entra_token", return_value=_claims_for(platform_role))
        patcher.start()
        env.patchers.append(patcher)
        return TestClient(app, headers={"Authorization": "Bearer user-token"})

    yield _make
    for p in env.patchers:
        p.stop()


def _dataset(env, *items):
    env.repos.files["evals/evals.yaml"] = b"judges: []\n"
    env.repos.files["evals/dataset.jsonl"] = "".join(json.dumps(i) + "\n" for i in items).encode()


def test_get_without_eval_files_is_unconfigured_defaults(client_for):
    r = client_for().get(BASE)
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False and body["items"] == [] and body["errors"] == []
    assert body["head_sha"] == HEAD
    assert body["config"] == EvalConfig().model_dump(mode="json")


def test_get_surfaces_parse_errors_not_500(client_for, env):
    env.repos.files["evals/evals.yaml"] = b"version: 9\n"
    env.repos.files["evals/dataset.jsonl"] = b"{nope\n"
    body = client_for().get(BASE).json()
    assert body["configured"] is True and len(body["errors"]) == 2


def test_validate(client_for):
    r = client_for().post(f"{BASE}/validate", json={"dataset_text": '{"id":"a","input":"x"}\n'})
    assert r.json() == {"ok": True, "errors": [], "items": 1}
    r = client_for().post(f"{BASE}/validate", json={"config_text": "bogus: 1\n"})
    assert r.json()["ok"] is False


def test_unknown_repo_is_404(client_for, env):
    env.projects.get_repo.return_value = None
    assert client_for().get(BASE).status_code == 404


def test_put_config_commits_an_overlay_on_the_head(client_for, env):
    r = client_for().put(f"{BASE}/config", json={"judges": []})
    assert r.status_code == 200 and r.json() == {"head_sha": "c" * 40}
    commit = env.repos.commits[-1]
    assert set(commit["files"]) == {"evals/evals.yaml"} and commit["expected"] == HEAD
    assert "[skip ci]" not in commit["message"] and commit["branch"] == "main"
    assert {"README.md", "src/main.py", "evals/evals.yaml"} <= set(env.repos.files)


def test_a_branch_that_moved_is_409(client_for, env):
    env.repos.move_before_commit = True
    r = client_for().put(f"{BASE}/config", json={})
    assert r.status_code == 409 and r.json()["detail"] == "the branch moved; reload and retry"


@pytest.mark.parametrize(
    "path", ["../x.jsonl", ".github/workflows/build.yml", "/abs.jsonl", "evals/evals.yaml"]
)
def test_put_config_refuses_a_dataset_path_outside_evals(client_for, env, path):
    assert client_for().put(f"{BASE}/config", json={"dataset": path}).status_code == 422
    assert env.repos.commits == []


def test_validate_refuses_oversized_text(client_for):
    r = client_for().post(f"{BASE}/validate", json={"dataset_text": "x" * 1_000_001})
    assert r.status_code == 422


def test_deeply_nested_yaml_is_not_a_500(client_for, env):
    env.repos.files["evals/evals.yaml"] = ("judges: " + "[" * 5000).encode()
    r = client_for().get(BASE)
    assert r.status_code == 200 and r.json()["errors"]


def test_put_config_rejects_an_unknown_key(client_for, env):
    assert client_for().put(f"{BASE}/config", json={"judgez": []}).status_code == 422
    assert env.repos.commits == []


def test_post_dataset_append_and_replace(client_for, env):
    _dataset(env, {"id": "a", "input": "one"})
    c = client_for()
    r = c.post(f"{BASE}/dataset", json={"items": [{"id": "b", "input": "two"}], "mode": "append"})
    assert r.status_code == 200 and r.json() == {"head_sha": "c" * 40, "items": 2}
    r = c.post(f"{BASE}/dataset", json={"items": [{"id": "z", "input": "z"}], "mode": "replace"})
    assert r.json()["items"] == 1


def test_post_dataset_duplicate_id_is_422(client_for, env):
    _dataset(env, {"id": "a", "input": "one"})
    r = client_for().post(f"{BASE}/dataset", json={"items": [{"id": "a", "input": "x"}], "mode": "append"})
    assert r.status_code == 422
    assert any("duplicate id 'a'" in e for e in r.json()["detail"]["errors"])
    assert env.repos.commits == []


def test_start_run_is_202_and_the_background_task_runs(client_for, env):
    _dataset(env, {"id": "a", "input": "one"})
    r = client_for().post(f"{BASE}/runs", json={"trigger": "manual"})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "running" and body["digest"] == DIGEST
    assert env.graph.calls == [("user-token", "api://a-1")]
    # TestClient runs background tasks after the response: the run finished against the fake.
    run, results = env.store.get_run("r-1", body["run_id"])
    assert run.status == "passed" and len(results) == 1
    assert env.adapter.calls == [("one", "obo-token", "dev")]


def test_auto_run_returns_the_existing_run_for_the_digest(client_for, env):
    _dataset(env, {"id": "a", "input": "one"})
    env.store.save_run(
        EvalRun(
            run_id="prior",
            repo_id="r-1",
            agent_id="a-1",
            project_id="proj-1",
            trigger="auto",
            started_by="x",
            started_at=NOW - timedelta(minutes=3),
            config=EvalConfig(),
            digest=DIGEST,
            status="passed",
            finished_at=NOW,
        )
    )
    r = client_for().post(f"{BASE}/runs", json={"trigger": "auto"})
    assert r.status_code == 200 and r.json()["run_id"] == "prior"
    assert len(env.store.list_runs("r-1")) == 1 and env.graph.calls == []


def test_auto_run_without_build_identity_is_200_null_and_starts_nothing(client_for, env):
    _dataset(env, {"id": "a", "input": "one"})
    env.projects.get_repo.return_value = _repo(digest=None, tag=None)
    r = client_for().post(f"{BASE}/runs", json={"trigger": "auto"})
    assert r.status_code == 200 and r.json() is None
    assert env.store.list_runs("r-1") == [] and env.graph.calls == []


def test_auto_run_dedupes_on_the_tag_without_a_digest(client_for, env):
    _dataset(env, {"id": "a", "input": "one"})
    env.projects.get_repo.return_value = _repo(digest=None, tag="t-1")
    c = client_for()
    first = c.post(f"{BASE}/runs", json={"trigger": "auto"})
    assert first.status_code == 202 and first.json()["build_tag"] == "t-1"
    again = c.post(f"{BASE}/runs", json={"trigger": "auto"})
    assert again.status_code == 200 and again.json()["run_id"] == first.json()["run_id"]


def test_a_repo_of_another_project_is_404(client_for, env):
    env.projects.get_repo.return_value = _repo(project_id="proj-2")
    c = client_for()
    assert c.get(BASE).status_code == 404
    assert c.post(f"{BASE}/runs", json={"trigger": "manual"}).status_code == 404
    assert c.get(f"{BASE}/runs/x").status_code == 404


def test_start_run_409_while_running(client_for, env):
    _dataset(env, {"id": "a", "input": "one"})
    env.store.save_run(
        EvalRun(
            run_id="busy",
            repo_id="r-1",
            agent_id="a-1",
            project_id="proj-1",
            trigger="manual",
            started_by="x",
            started_at=NOW - timedelta(minutes=1),
            config=EvalConfig(),
        )
    )
    assert client_for().post(f"{BASE}/runs", json={"trigger": "manual"}).status_code == 409


def test_start_run_422_on_an_empty_dataset(client_for):
    assert client_for().post(f"{BASE}/runs", json={"trigger": "manual"}).status_code == 422


def test_mcp_repo_without_agent_409_on_start_but_get_works(client_for, env):
    env.projects.get_repo.return_value = _repo(agent_id=None)
    c = client_for()
    assert c.post(f"{BASE}/runs", json={"trigger": "manual"}).status_code == 409
    assert c.get(BASE).status_code == 200
    assert c.get(f"{BASE}/runs").status_code == 200


def test_list_and_get_runs(client_for, env):
    _dataset(env, {"id": "a", "input": "one"})
    c = client_for()
    run_id = c.post(f"{BASE}/runs", json={"trigger": "manual"}).json()["run_id"]
    body = c.get(f"{BASE}/runs").json()
    assert [r["run_id"] for r in body["runs"]] == [run_id]
    assert body["current_digest"] == DIGEST and body["current_run"]["run_id"] == run_id
    assert body["overrides"] == []
    assert body["current_build_tag"] == "t-1"
    detail = c.get(f"{BASE}/runs/{run_id}").json()
    assert detail["run"]["status"] == "passed" and detail["results"][0]["item_id"] == "a"
    assert detail["dataset"] == [
        {
            "id": "a",
            "input": "one",
            "expected_output": None,
            "assertions": [],
            "expected_tools": [],
            "source_trace_id": None,
            "tags": [],
        }
    ]
    assert c.get(f"{BASE}/runs/nope").status_code == 404


def test_traces_without_a_stage_runtime_is_source_none(client_for):
    r = client_for().get(f"{BASE}/traces", params={"stage": "prod"})
    assert r.status_code == 200 and r.json() == {"traces": [], "source": "none"}


def test_project_viewer_reads_but_cannot_write(client_for, env):
    env.role_svc.has_role_rows.return_value = True
    _dataset(env, {"id": "a", "input": "one"})
    c = client_for(project_role="viewer")
    assert c.get(BASE).status_code == 200
    assert c.get(f"{BASE}/runs").status_code == 200
    assert c.put(f"{BASE}/config", json={}).status_code == 403
    assert c.post(f"{BASE}/dataset", json={"items": [], "mode": "append"}).status_code == 403
    assert c.post(f"{BASE}/runs", json={"trigger": "manual"}).status_code == 403
    assert env.repos.commits == [] and env.store.list_runs("r-1") == []


def test_no_project_role_cannot_read_a_governed_project(client_for, env):
    env.role_svc.has_role_rows.return_value = True
    assert client_for(project_role=None).get(BASE).status_code == 403


EVAL_KINDS = {
    "not_found": 404,
    "no_agent": 409,
    "no_dev": 409,
    "running": 409,
    "no_commit": 409,
    "conflict": 409,
    "trunk_mismatch": 409,
    "not_provisioned": 409,
    "empty": 422,
    "not_assigned": 403,
    "obo_config": 500,
    "provider": 502,
    "obo_failed": 502,
}


@pytest.mark.parametrize("kind, status", sorted(EVAL_KINDS.items()))
def test_eval_error_kind_is_its_status_with_the_message_as_detail(client_for, env, kind, status):
    from services.eval_runner import EvalError

    with patch.object(env.runner, "read", side_effect=EvalError("the service's own sentence", kind)):
        r = client_for().get(BASE)
    assert (r.status_code, r.json()["detail"]) == (status, "the service's own sentence")
    assert r.json()["code"] == f"eval.{kind}"


def test_eval_error_invalid_is_422_with_message_and_errors(client_for, env):
    from services.eval_runner import EvalError

    err = EvalError("the eval files are invalid", "invalid", errors=["evals/evals.yaml:1: nope"])
    with patch.object(env.runner, "read", side_effect=err):
        r = client_for().get(BASE)
    assert r.status_code == 422
    assert r.json()["detail"] == {
        "message": "the eval files are invalid",
        "errors": ["evals/evals.yaml:1: nope"],
    }
    assert r.json()["code"] == "eval.invalid"


def test_eval_error_detail_for_provider_carries_no_upstream_body(client_for, env):
    def boom(*a, **k):
        raise RuntimeError(f"401 Bad credentials for token {_FAKE_TOKEN}")

    env.repos.read_repo = boom
    r = client_for().get(BASE)
    assert r.status_code == 502 and r.json()["detail"] == "the repository could not be read"
    assert "ghp_" not in r.text and "Bad credentials" not in r.text


def test_eval_error_detail_for_obo_failed_carries_no_upstream_body(client_for, env):
    from services.graph_errors import GraphError

    _dataset(env, {"id": "a", "input": "one"})

    async def boom(user_token, audience):
        raise GraphError(500, "invalid_grant", "AADSTS70000 assertion eyJhbGciOiJSUzI1NiIsImtpZCI6IjEifQ")

    env.graph.obo_exchange = boom
    r = client_for().post(f"{BASE}/runs", json={"trigger": "manual"})
    assert r.status_code == 502
    assert r.json()["detail"] == "the caller's token could not be exchanged for the agent"
    assert "eyJ" not in r.text and "AADSTS" not in r.text


def test_eval_error_detail_for_obo_config_carries_no_upstream_body(client_for, env):
    from services.graph_errors import OboConfigError

    _dataset(env, {"id": "a", "input": "one"})

    async def boom(user_token, audience):
        raise OboConfigError("AADSTS65001 the user or administrator has not consented; client_secret=s3cr3t")

    env.graph.obo_exchange = boom
    r = client_for().post(f"{BASE}/runs", json={"trigger": "manual"})
    assert r.status_code == 500
    assert r.json()["detail"] == "agent identity is misconfigured — re-provision"
    assert "AADSTS" not in r.text and "s3cr3t" not in r.text


def test_eval_error_detail_for_not_assigned_carries_no_upstream_body(client_for, env):
    """``not_assigned`` sends the message verbatim, so the message must be safe by construction: the
    runner composes it from the caller's identity, never from Graph's text, which here carries a
    token-shaped string."""
    from services.graph_errors import NotAssignedError

    _dataset(env, {"id": "a", "input": "one"})

    async def boom(user_token, audience):
        raise NotAssignedError(
            f"AADSTS50105 the signed in user is not assigned to a role; assertion {_FAKE_TOKEN}"
        )

    env.graph.obo_exchange = boom
    r = client_for().post(f"{BASE}/runs", json={"trigger": "manual"})
    assert r.status_code == 403
    assert r.json() == {"detail": "operator@x.com is not assigned to this agent", "code": "eval.not_assigned"}
    assert "AADSTS" not in r.text and "ghp_" not in r.text


ROUTE = "/projects/{id}/repos/{repo_id}/evals"
EVAL_RUN_KEYS = frozenset(
    {
        "run_id",
        "repo_id",
        "agent_id",
        "project_id",
        "stage",
        "git_sha",
        "digest",
        "build_tag",
        "trigger",
        "started_by",
        "started_at",
        "finished_at",
        "status",
        "config",
        "item_count",
        "summary",
        "gate",
        "baseline",
        "regressions",
    }
)
DATASET_ITEM_KEYS = frozenset(
    {"id", "input", "expected_output", "assertions", "expected_tools", "source_trace_id", "tags"}
)


def _route(router, method: str, path: str) -> APIRoute:
    route = next(r for r in router.routes if r.path == path and method in r.methods)
    assert isinstance(route, APIRoute)
    return route


def test_every_route_declares_its_envelope_and_keeps_every_key(client_for, env):
    from typing import Optional

    from models.eval import DatasetAdded, EvalFiles, EvalRuns, HeadSha, RunDetail, ValidationResult

    _dataset(env, {"id": "a", "input": "one"})
    c = client_for()
    import api.routes.evals as evals_module
    from api.routes.evals import TraceList

    files = c.get(BASE)
    validated = c.post(f"{BASE}/validate", json={"dataset_text": '{"id":"a","input":"x"}\n'})
    started = c.post(f"{BASE}/runs", json={"trigger": "manual"})
    runs = c.get(f"{BASE}/runs")
    detail = c.get(f"{BASE}/runs/{started.json()['run_id']}")
    traces = c.get(f"{BASE}/traces", params={"stage": "prod"})
    config = c.put(f"{BASE}/config", json={"judges": []})
    added = c.post(f"{BASE}/dataset", json={"items": [{"id": "b", "input": "two"}], "mode": "append"})
    responses = (files, validated, started, runs, detail, traces, config, added)
    assert [r.status_code for r in responses] == [200, 200, 202, 200, 200, 200, 200, 200]
    assert set(files.json()) == {"config", "items", "head_sha", "errors", "configured"}
    assert set(files.json()["config"]) == {"version", "dataset", "checks", "judges", "gate"}
    assert {k for i in files.json()["items"] for k in i} == DATASET_ITEM_KEYS
    assert set(validated.json()) == {"ok", "errors", "items"}
    assert set(started.json()) == EVAL_RUN_KEYS
    assert set(runs.json()) == {"runs", "overrides", "current_digest", "current_build_tag", "current_run"}
    assert {k for r in runs.json()["runs"] for k in r} == EVAL_RUN_KEYS
    assert set(runs.json()["current_run"]) == EVAL_RUN_KEYS
    assert set(detail.json()) == {"run", "results", "dataset"}
    assert set(detail.json()["run"]) == EVAL_RUN_KEYS
    assert {k for r in detail.json()["results"] for k in r} == {
        "item_id",
        "session_id",
        "output",
        "tool_calls",
        "latency_ms",
        "error",
        "scores",
    }
    assert {k for d in detail.json()["dataset"] for k in d} == DATASET_ITEM_KEYS
    assert set(traces.json()) == {"traces", "source"}
    assert set(config.json()) == {"head_sha"}
    assert set(added.json()) == {"head_sha", "items"}
    declared = {
        ("GET", ROUTE): EvalFiles,
        ("POST", f"{ROUTE}/validate"): ValidationResult,
        ("PUT", f"{ROUTE}/config"): HeadSha,
        ("POST", f"{ROUTE}/dataset"): DatasetAdded,
        ("GET", f"{ROUTE}/runs"): EvalRuns,
        ("GET", f"{ROUTE}/runs/{{run_id}}"): RunDetail,
        ("POST", f"{ROUTE}/runs"): Optional[EvalRun],
        ("GET", f"{ROUTE}/traces"): TraceList,
    }
    for (method, path), model in declared.items():
        assert _route(evals_module.router, method, path).response_model == model, (method, path)
