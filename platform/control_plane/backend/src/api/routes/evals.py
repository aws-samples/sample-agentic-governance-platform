"""Evaluations API: ``/projects/{id}/repos/{repo_id}/evals``.

The repo's eval files (read, validate, commit), eval runs (start, list, detail) and the recent
traces a dataset is seeded from. Same gates as the repo routes in ``projects.py``: OPERATOR
platform role, the project's tenant visibility (404 first), then project VIEWER to read and
MAINTAINER to write. All logic is in ``services.eval_runner``; its ``EvalError`` kinds table is
the wire. Tests patch ``_runner`` directly.

Handlers are SYNC (threadpool): the runner makes blocking provider calls and runs the OBO
exchange with ``asyncio.run``, which needs a thread without a running loop.
"""

import logging
from typing import List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response
from fastapi import Depends as RBACDepends
from pydantic import Field

import api.routes.projects as projects
from core.rbac import Principal, Role, current_principal, require_role
from models.base import WireModel
from models.eval import (
    DatasetAdded,
    DatasetItem,
    EvalConfig,
    EvalFiles,
    EvalRun,
    EvalRuns,
    HeadSha,
    RunDetail,
    ValidationResult,
)
from models.project_role import ProjectRole
from services.eval_judges import TraceSummary
from services.eval_runner import MAX_FILE_BYTES, EvalRunner
from services.eval_store import get_eval_store
from services.project_resolver import ProjectContext
from services.tenant_resolver import TenantContext

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects", tags=["evals"])

_runner: Optional[EvalRunner] = None


def get_eval_runner() -> EvalRunner:
    """Lazy runner over the project service's own collaborators (one GitHub client, one
    connection service, one tenant-stage resolver) and the agents route's singletons."""
    global _runner
    if _runner is None:
        from api.routes.agents import get_adapters, get_graph_service, get_service
        from models.agent import GOVERNABLE_PLATFORMS
        from services.platform_adapter_factory import adapter_for

        svc = projects.get_project_service()
        _runner = EvalRunner(
            projects=svc,
            repos=svc._rollout,
            connections=svc._conn,
            registry=get_service(),
            adapter_for=lambda agent: adapter_for(get_adapters(), GOVERNABLE_PLATFORMS.get(agent.platform)),
            graph=get_graph_service(),
            store=get_eval_store(),
            stage_cfg=svc._stage_cfg,
        )
    return _runner


def _gate(id: str, role: ProjectRole, ctx: TenantContext, pctx: ProjectContext, principal: Principal) -> None:
    projects._load_visible_project(id, ctx)
    projects._require_project_role_or_ungoverned(pctx, id, role, principal=principal)


class ValidateRequest(WireModel):
    config_text: Optional[str] = Field(default=None, max_length=MAX_FILE_BYTES)
    dataset_text: Optional[str] = Field(default=None, max_length=MAX_FILE_BYTES)


class DatasetRequest(WireModel):
    items: List[DatasetItem]
    mode: Literal["append", "replace"] = "append"


class StartRunRequest(WireModel):
    trigger: Literal["manual", "auto"] = "manual"


class TraceList(WireModel):
    traces: List[TraceSummary]
    source: str


@router.get("/{id}/repos/{repo_id}/evals", response_model=EvalFiles)
def get_evals(
    id: str,
    repo_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    _gate(id, ProjectRole.VIEWER, ctx, pctx, principal)
    out = get_eval_runner().read(id, repo_id)
    return {
        **out,
        "config": out["config"].model_dump(mode="json"),
        "items": [i.model_dump(mode="json") for i in out["items"]],
    }


@router.post("/{id}/repos/{repo_id}/evals/validate", response_model=ValidationResult)
def validate_evals(
    id: str,
    repo_id: str,
    body: ValidateRequest,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    _gate(id, ProjectRole.VIEWER, ctx, pctx, principal)
    return EvalRunner.validate(config_text=body.config_text, dataset_text=body.dataset_text)


@router.put("/{id}/repos/{repo_id}/evals/config", response_model=HeadSha)
def put_eval_config(
    id: str,
    repo_id: str,
    body: EvalConfig,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    _gate(id, ProjectRole.MAINTAINER, ctx, pctx, principal)
    return {"head_sha": get_eval_runner().put_config(id, repo_id, body)}


@router.post("/{id}/repos/{repo_id}/evals/dataset", response_model=DatasetAdded)
def post_eval_dataset(
    id: str,
    repo_id: str,
    body: DatasetRequest,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    _gate(id, ProjectRole.MAINTAINER, ctx, pctx, principal)
    head, count = get_eval_runner().put_dataset(id, repo_id, body.items, mode=body.mode)
    return {"head_sha": head, "items": count}


@router.get("/{id}/repos/{repo_id}/evals/runs", response_model=EvalRuns)
def list_eval_runs(
    id: str,
    repo_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    _gate(id, ProjectRole.VIEWER, ctx, pctx, principal)
    out = get_eval_runner().runs(id, repo_id)
    return runs_payload(out)


def runs_payload(out: dict) -> dict:
    """The runs-list body; also served agent-scoped by ``GET /agents/{agent_id}/evals/runs``."""
    return {
        "runs": [r.model_dump(mode="json") for r in out["runs"]],
        "overrides": [o.model_dump(mode="json") for o in out["overrides"]],
        "current_digest": out["current_digest"],
        "current_build_tag": out["current_build_tag"],
        "current_run": out["current_run"].model_dump(mode="json") if out["current_run"] else None,
    }


@router.get("/{id}/repos/{repo_id}/evals/runs/{run_id}", response_model=RunDetail)
def get_eval_run(
    id: str,
    repo_id: str,
    run_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    _gate(id, ProjectRole.VIEWER, ctx, pctx, principal)
    run, results, dataset = get_eval_runner().get_run(id, repo_id, run_id)
    return {
        "run": run.model_dump(mode="json"),
        "results": [r.model_dump(mode="json") for r in results],
        "dataset": [d.model_dump(mode="json") for d in dataset],
    }


@router.post("/{id}/repos/{repo_id}/evals/runs", response_model=Optional[EvalRun], status_code=202)
def start_eval_run(
    id: str,
    repo_id: str,
    body: StartRunRequest,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """202 and a background ``execute``. ``auto`` answers 200 instead, starting nothing, when a run
    already covers the current dev build (digest, else tag), or when the build has no identity at
    all, with the latest run or null, so the tab can request a run on every visit."""
    _gate(id, ProjectRole.MAINTAINER, ctx, pctx, principal)
    runner = get_eval_runner()
    auth_header = request.headers.get("authorization", "")
    user_token = auth_header.split(" ", 1)[1] if " " in auth_header else ""
    if body.trigger == "auto":
        handled, existing = runner.reusable_run(id, repo_id)
        if handled:
            response.status_code = 200
            return existing.model_dump(mode="json") if existing is not None else None
    run = runner.start(
        id,
        repo_id,
        trigger=body.trigger,
        principal=principal,
        user_token=user_token,
        background_tasks=background_tasks,
    )
    return run.model_dump(mode="json")


@router.get("/{id}/repos/{repo_id}/evals/traces", response_model=TraceList)
def list_eval_traces(
    id: str,
    repo_id: str,
    stage: Literal["dev", "prod"] = Query(default="dev"),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    _gate(id, ProjectRole.VIEWER, ctx, pctx, principal)
    traces, source = get_eval_runner().traces(id, repo_id, stage=stage, limit=limit)
    return {"traces": [t.model_dump(mode="json") for t in traces], "source": source}
