"""The agent→repo lookup and the agent-scoped eval runs read the Governance agent
Evaluations tab uses, so a reader below Operator can see an agent's gate and run history.

Both carry the ``/deployments`` gate verbatim (tenant context + VIEWER, then the shared
``_load_visible_agent``) and resolve the repo SERVER-SIDE from the agent id. Harness reused from
``test_deployments_route``."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.routing import APIRoute
from test_deployments_route import (
    AGENT_ID,
    REPO_ID,
    _build_client,
    _claims_for,
    _context,
    _headers,
    _make_agent,
    _repo,
)

pytestmark = pytest.mark.usefixtures("reset_modules")


def _client():
    mock_svc = MagicMock()
    mock_svc.get.side_effect = lambda agent_id: (
        _make_agent()
        if agent_id == AGENT_ID
        else _make_agent(agent_id="foreign", tenant_id="ten-2")
        if agent_id == "foreign"
        else None
    )
    return _build_client(mock_svc, _context(tenant_ids=["ten-1"]))


def _project_svc(repo):
    svc = MagicMock()
    svc.find_repository_by_agent_id.return_value = repo
    svc.get_repo.return_value = (
        repo.model_copy(update={"eval_gate": "failed", "eval_regressions": ["x"]}) if repo else None
    )
    return svc


def _get(url, project_svc, runner=None):
    client = _client()
    with (
        patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")),
        patch("api.routes.projects.get_project_service", return_value=project_svc),
        patch("api.routes.evals.get_eval_runner", return_value=runner or MagicMock()),
    ):
        return client.get(url, headers=_headers())


def test_a_viewer_finds_the_agents_repository(entra_settings):
    resp = _get(f"/api/v1/agents/{AGENT_ID}/repository", _project_svc(_repo()))
    assert resp.status_code == 200
    assert resp.json() == {
        "project_id": "prj-1",
        "repo_id": REPO_ID,
        "name": "claims-triage-de",
        "prod_candidate_status": None,
        "eval_gate": "failed",
        "eval_regressions": ["x"],
    }


def test_an_agent_without_a_repository_is_404_with_its_own_detail(entra_settings):
    resp = _get(f"/api/v1/agents/{AGENT_ID}/repository", _project_svc(None))
    assert resp.status_code == 404
    assert resp.json() == {"detail": "No repository for this agent", "code": "project.no_repository"}


def test_a_foreign_agent_looks_absent_and_no_repo_is_read(entra_settings):
    svc = _project_svc(_repo())
    for path in ("repository", "evals/runs"):
        resp = _get(f"/api/v1/agents/foreign/{path}", svc)
        assert resp.status_code == 404 and resp.json()["detail"] == "Agent not found"
    svc.find_repository_by_agent_id.assert_not_called()


def test_a_viewer_reads_the_agents_eval_runs_for_the_repo_resolved_server_side(entra_settings):
    runner = MagicMock()
    runner.runs.return_value = {
        "runs": [],
        "overrides": [],
        "current_digest": "sha256:d",
        "current_build_tag": "t",
        "current_run": None,
    }
    resp = _get(f"/api/v1/agents/{AGENT_ID}/evals/runs", _project_svc(_repo()), runner)
    assert resp.status_code == 200
    assert resp.json() == {
        "runs": [],
        "overrides": [],
        "current_digest": "sha256:d",
        "current_build_tag": "t",
        "current_run": None,
    }
    runner.runs.assert_called_once_with("prj-1", REPO_ID)


def test_eval_runs_for_an_agent_without_a_repository_is_404(entra_settings):
    resp = _get(f"/api/v1/agents/{AGENT_ID}/evals/runs", _project_svc(None))
    assert resp.status_code == 404
    assert resp.json() == {"detail": "No repository for this agent", "code": "project.no_repository"}


def test_agent_eval_runs_not_found_is_404(entra_settings):
    """An ``EvalError`` from the runner renders its table through the one handler: ``not_found``
    is a 404 with the runner's message as the detail."""
    from services.eval_runner import EvalError

    runner = MagicMock()
    runner.runs.side_effect = EvalError("Project not found", "not_found")
    resp = _get(f"/api/v1/agents/{AGENT_ID}/evals/runs", _project_svc(_repo()), runner)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Project not found", "code": "eval.not_found"}


def test_both_reads_carry_the_deployments_gate(entra_settings):
    import api.routes.agents as agents_module

    def deps(suffix):
        route = next(r for r in agents_module.router.routes if getattr(r, "path", "").endswith(suffix))
        assert isinstance(route, APIRoute)
        out = []
        for d in route.dependant.dependencies:
            assert d.call is not None
            out.append(
                (d.name, d.call.__qualname__, tuple(c.cell_contents for c in (d.call.__closure__ or ())))
            )
        return out

    gate = deps("/{agent_id}/deployments")
    assert deps("/{agent_id}/repository") == gate[:2] and deps("/{agent_id}/evals/runs") == gate[:2]


def test_the_agent_scoped_runs_read_redacts_per_item_check_detail(entra_settings):
    """A failed args check quotes the expected and actual tool arguments
    (account numbers, customer ids). Tenant viewers get the verdict, never the data."""
    from datetime import datetime, timezone

    from models.eval import EvalConfig, EvalRun, GateResult

    leaky = "item-3: args failed — expected args {'iban': 'DE89370400440532013000'}, got {'iban': 'DE00'}"
    run = EvalRun(
        run_id="run-1",
        repo_id=REPO_ID,
        agent_id=AGENT_ID,
        project_id="prj-1",
        trigger="manual",
        started_by="x",
        started_at=datetime(2026, 10, 7, tzinfo=timezone.utc),
        status="failed",
        config=EvalConfig(),
        gate=GateResult(passed=False, reasons=[leaky, "Builtin.Correctness mean 0.50 < 0.8"]),
    )
    runner = MagicMock()
    runner.runs.return_value = {
        "runs": [run],
        "overrides": [],
        "current_digest": None,
        "current_build_tag": None,
        "current_run": run,
    }
    resp = _get(f"/api/v1/agents/{AGENT_ID}/evals/runs", _project_svc(_repo()), runner)
    assert resp.status_code == 200
    body = resp.json()
    for r in (body["runs"][0], body["current_run"]):
        assert r["gate"]["reasons"] == ["item-3: args failed", "Builtin.Correctness mean 0.50 < 0.8"]
    assert "DE89" not in resp.text and "expected args" not in resp.text and "got" not in resp.text
