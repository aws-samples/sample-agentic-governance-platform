"""Studio API: ``POST /studio/draft`` and ``POST /projects/{id}/repos/{repo_id}/studio-proposal``.

Same gates as ``POST /projects/{id}/repos`` in ``project_repos.py``: OPERATOR platform role, the
project's tenant visibility (404 first), then project MAINTAINER. The draft names its project in
the body. All logic is in ``services.studio_draft`` / ``services.studio_proposal``; their error
classes' kinds tables are the wire. Tests patch ``_svc`` and ``draft_composition`` directly.

Handlers are SYNC (threadpool): the draft parses OpenAPI files (CPU) before a blocking Bedrock
call, and the proposal's runtime env rebuild runs via ``anyio.from_thread``, which needs a worker.
"""

import json
from typing import List, Optional

from fastapi import APIRouter
from fastapi import Depends as RBACDepends
from pydantic import Field, field_validator

import api.routes.projects as projects
import api.routes.repositories as repositories
from core import aws_clients
from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from models.base import WireModel
from models.project_role import ProjectRole
from models.studio import DraftResponse, ProposalRequest, ProposalResponse
from services.project_errors import ProjectError
from services.project_resolver import ProjectContext
from services.studio_draft import draft_composition
from services.studio_proposal import StudioProposalService, StudioValidationError
from services.tenant_resolver import TenantContext

router = APIRouter(tags=["studio"])

_svc: Optional[StudioProposalService] = None

MAX_FILE_BYTES = 1024 * 1024
MAX_PROPOSAL_BYTES = (
    10 * 1024 * 1024
)  # API Gateway's payload cap: https://docs.aws.amazon.com/apigateway/latest/developerguide/limits.html


def get_proposal_service() -> StudioProposalService:
    """Lazy service over the project service's own GitHub client + connection service (as
    ``evals.get_eval_runner``), the agents / MCP routes' registry singletons and the GitHub
    user-link service the PR routes use."""
    global _svc
    if _svc is None:
        from api.routes.agents import get_service as get_agent_registry
        from api.routes.github_link import get_github_link_service
        from api.routes.mcp_servers import get_service as get_mcp_registry

        svc = projects.get_project_service()
        _svc = StudioProposalService(
            repos=svc._rollout,
            connections=svc._conn,
            prs=repositories.get_pr_service(),
            registry=get_agent_registry(),
            mcp_registry=get_mcp_registry(),
            secrets=aws_clients.client("secretsmanager", region=settings.AWS_REGION),
            links=get_github_link_service(),
        )
    return _svc


def _gate(id: str, ctx: TenantContext, pctx: ProjectContext, principal: Principal):
    detail = projects._load_visible_project(id, ctx)
    projects._require_project_role_or_ungoverned(pctx, id, ProjectRole.MAINTAINER, principal=principal)
    return detail


class DraftFile(WireModel):
    name: str = Field(min_length=1, max_length=255)
    text: str

    @field_validator("text")
    @classmethod
    def _at_most_1_mb(cls, v: str) -> str:
        if len(v.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("a file is larger than 1 MB")
        return v


class DraftRequest(WireModel):
    project_id: str
    description: str = Field(min_length=1, max_length=4000)
    files: List[DraftFile] = Field(default_factory=list, max_length=10)


@router.post("/studio/draft", response_model=DraftResponse)
def draft(
    body: DraftRequest,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    _gate(body.project_id, ctx, pctx, principal)
    return draft_composition(
        body.description, [(f.name, f.text) for f in body.files], model_id=settings.STUDIO_MODEL_ID
    )


@router.post(
    "/projects/{id}/repos/{repo_id}/studio-proposal", status_code=201, response_model=ProposalResponse
)
def propose(
    id: str,
    repo_id: str,
    req: ProposalRequest,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    detail = _gate(id, ctx, pctx, principal)
    repo = projects.get_project_service().get_repo(repo_id)
    if repo is None or repo.project_id != id:
        raise ProjectError(f"repository {repo_id} is not in project {id}", kind="repo_not_found")
    body = req.root.model_dump(mode="json", exclude_unset=True)
    if len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_PROPOSAL_BYTES:
        raise StudioValidationError("The proposal is larger than 10 MB", kind="invalid")
    return get_proposal_service().propose(detail.project, repo, body, caller=principal)
