"""Build-trigger endpoints: GitHub OIDC to CodeBuild StartBuild.

A GitHub Action, after pushing the agent image to ECR, calls ``POST /builds/runtime`` with a
GitHub-minted OIDC token to ask AGP to provision the runtime. This is a separate trust boundary from
the Entra-governed surface: auth is ``Depends(verify_github_oidc)``, never ``require_role`` or
``current_principal``. The validator proves the token is a genuine, current, correctly-audienced
GitHub Actions token; the routes then bind the token's ``repository_owner`` to the resolved
connection's org, the token's ``repository`` to the repository that owns the posted artifact, and the
image tag to that artifact, each a 403 on mismatch and every refusal before any side effect.

The connection credential is never read here: the build service passes only a scratch-secret ARN to
CodeBuild. The lazy service singletons are module attributes tests patch directly, so nothing here
runs against live AWS in a test. ``ConnectionError``, ``ProjectError`` and ``RuntimeBuildError``
render through their tables.
"""

import functools
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import anyio.to_thread
from fastapi import APIRouter, Depends
from pydantic import Field, field_validator, model_validator

from core import aws_clients
from core.config import settings
from core.security_github_oidc import GitHubActionsClaims, verify_github_oidc
from models.base import WireModel
from models.deployment import DeploymentOutcome
from services.connection_service import ConnectionError, ConnectionService
from services.runtime_build_service import RuntimeBuildError, RuntimeBuildService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/builds", tags=["builds"])

_conn_svc: Optional[ConnectionService] = None
_build_svc: Optional[RuntimeBuildService] = None
_project_svc = None

# Docker's tag grammar, which ECR applies: [A-Za-z0-9._-], at most 128 characters, no leading dash
# (https://docs.docker.com/reference/cli/docker/image/tag/).
_IMAGE_TAG_RE = re.compile(r"\A[A-Za-z0-9._][A-Za-z0-9._-]{0,127}\Z")
_IMAGE_DIGEST_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_COMMIT_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_BUILD_KIND_CONFIG = "config"
_REFUSALS_WITHOUT_A_DELIVERY_ROW = frozenset({"conflict", "invalid_name"})


class RuntimeBuildRequest(WireModel):
    """The body a repository's workflow posts after a push. Exactly one of ``agent_id`` and
    ``mcp_server_id`` names the artifact; the deploy tenant is never taken from the body, it is
    derived server-side from that artifact's record."""

    agent_id: Optional[str] = Field(
        None,
        description="The agent whose image was pushed; an agent workflow posts this, not mcp_server_id.",
    )
    mcp_server_id: Optional[str] = Field(
        None,
        description="The MCP server whose repo was pushed; its workflow posts this, not agent_id.",
    )
    image_tag: str = Field(
        description="<agent_id>-<short_sha> for a container build; a bare 40-hex commit sha for a config one"
    )
    ecr_repo: str
    connection_id: str
    stage: str = Field(
        description="The branch-derived deploy stage, dev or prod; the only trusted body field."
    )
    image_digest: Optional[str] = Field(
        None,
        description=(
            "The sha256: digest of the image the workflow just pushed. Optional because a materialized repo "
            "carries a committed copy of its workflow and older copies post none; absent means a tag-only "
            "deploy."
        ),
    )

    @field_validator("image_digest")
    @classmethod
    def _digest_shape(cls, v: Optional[str]) -> Optional[str]:
        """A present but malformed digest is a 422: it comes from the workflow's own build step, so
        it is a template or CI defect the author must see. Empty collapses to ``None`` (the tag-only
        path) because a job output legitimately arrives empty from a skipped upstream step."""
        if v is None or not v.strip():
            return None
        if not _IMAGE_DIGEST_RE.match(v):
            raise ValueError("image_digest must be 'sha256:' followed by 64 lowercase hex chars")
        return v

    @model_validator(mode="after")
    def _exactly_one_artifact(self) -> "RuntimeBuildRequest":
        if (self.agent_id is None) == (self.mcp_server_id is None):
            raise ValueError("exactly one of agent_id or mcp_server_id is required")
        return self


class RuntimeBuildResponse(WireModel):
    build_id: str
    status: str


class ProdCandidateRequest(WireModel):
    agent_id: str
    image_tag: str
    ecr_repo: str = Field(
        description="Accepted for symmetry with the runtime body; neither trusted nor stored, never read"
    )
    connection_id: str


class ProdCandidateResponse(WireModel):
    status: str


def get_connection_service() -> ConnectionService:
    """Lazy ConnectionService singleton (tests patch ``_conn_svc`` directly)."""
    global _conn_svc
    if _conn_svc is None:
        _conn_svc = ConnectionService(
            table_name=settings.CONNECTIONS_TABLE_NAME,
            secret_prefix=settings.CONNECTIONS_SECRET_PREFIX,
            region=settings.AWS_REGION,
        )
    return _conn_svc


def get_build_service() -> RuntimeBuildService:
    """Lazy RuntimeBuildService singleton (tests patch ``_build_svc`` directly).

    The ProjectService is attached after construction rather than passed in: its own factory
    resolves this singleton, and resolving both eagerly recursed. It is attached inside the guard,
    because an unattached service folds every config agent to the container path."""
    global _build_svc
    if _build_svc is None:
        from api.routes.agents import get_service as get_agent_registry
        from api.routes.mcp_server_grants import get_agent_credential_service
        from api.routes.mcp_servers import get_service as get_mcp_registry
        from services.infra_template_registry import InfraTemplateRegistry
        from services.template_registry import TemplateRegistry
        from services.tenant_service import TenantService

        _build_svc = RuntimeBuildService(
            aws_clients.client("codebuild", region=settings.AWS_REGION),
            get_connection_service(),
            aws_clients.client("secretsmanager", region=settings.AWS_REGION),
            tenant_service=TenantService(table_name=settings.TENANTS_TABLE_NAME, region=settings.AWS_REGION),
            agent_registry=get_agent_registry(),
            codebuild_project_name=settings.CODEBUILD_PROJECT_NAME,
            scratch_secret_prefix=settings.RUNTIME_BUILD_TOKEN_PREFIX,
            project_service=None,
            template_registry=TemplateRegistry(
                table_name=settings.PROJECTS_TABLE_NAME, region=settings.AWS_REGION
            ),
            infra_template_registry=InfraTemplateRegistry(
                table_name=settings.PROJECTS_TABLE_NAME, region=settings.AWS_REGION
            ),
            mcp_registry=get_mcp_registry(),
            credential_service=get_agent_credential_service(),
        )
        _build_svc.attach_project_service(get_project_service())
    return _build_svc


def get_project_service():
    """The shared ProjectService, reused from the projects route and resolved lazily so importing
    the builds route never drags the project stack in (tests patch ``_project_svc`` directly)."""
    global _project_svc
    if _project_svc is None:
        from api.routes.projects import get_project_service as _get

        _project_svc = _get()
    return _project_svc


def _resolve_connection_for_token(gh: GitHubActionsClaims, connection_id: str):
    """Resolve the posted connection (an unknown one renders the connection table's 404) and
    assert the token was minted in its org (403). GitHub logins are case-insensitive while the
    stored org may diverge in case, so both trusted operands are case-folded."""
    conn = get_connection_service().get_connection(connection_id)
    if gh.repository_owner.lower() != conn.org.lower():
        logger.warning(
            "[builds] org mismatch: token owner=%s connection org=%s",
            gh.repository_owner,
            conn.org,
        )
        raise RuntimeBuildError("the token's owner is not the connection's org", kind="token_org_mismatch")
    return conn


def _assert_token_owns_agent(gh: GitHubActionsClaims, agent_id: str) -> None:
    """Bind agent_id to its repository to the token's ``repository`` claim. The org match proves
    the token belongs to some repo in the org, not that the token's repo owns the posted agent;
    without this binding repo A's workflow could build repo B's agent and assume B's cross-account
    deploy role. The repository that owns the agent is resolved, its org read through the repo's
    project connection, and the proven claim must equal ``<org>/<repo.name>`` case-folded. An
    unknown agent or any mismatch is a 403."""
    conn_svc = get_connection_service()
    project_svc = get_project_service()
    repo = project_svc.find_repository_by_agent_id(agent_id)
    repo_org = None
    if repo is not None:
        detail = project_svc.get_project(repo.project_id)
        if detail is not None:
            try:
                repo_conn = conn_svc.get_connection(detail.project.connection_id)
                repo_org = repo_conn.org
            except ConnectionError:
                repo_org = None
    if repo is None or repo_org is None or gh.repository.lower() != f"{repo_org}/{repo.name}".lower():
        logger.warning(
            "[builds] agent/repo binding failed: token repo=%s agent_id=%s",
            gh.repository,
            agent_id,
        )
        raise RuntimeBuildError(
            f"the token's repository does not own agent {agent_id}", kind="token_not_agent_owner"
        )


def _assert_token_owns_mcp(gh: GitHubActionsClaims, mcp_server_id: str) -> None:
    """:func:`_assert_token_owns_agent`'s twin for an MCP-server repo: without it repo A's workflow
    could rebuild repo B's gateway. An unknown MCP server or any mismatch is a 403."""
    conn_svc = get_connection_service()
    project_svc = get_project_service()
    repo = project_svc.find_repository_by_mcp_server_id(mcp_server_id)
    repo_org = None
    if repo is not None:
        detail = project_svc.get_project(repo.project_id)
        if detail is not None:
            try:
                repo_org = conn_svc.get_connection(detail.project.connection_id).org
            except ConnectionError:
                repo_org = None
    if repo is None or repo_org is None or gh.repository.lower() != f"{repo_org}/{repo.name}".lower():
        logger.warning(
            "[builds] mcp/repo binding failed: token repo=%s mcp_server_id=%s",
            gh.repository,
            mcp_server_id,
        )
        raise RuntimeBuildError(
            f"the token's repository does not own MCP server {mcp_server_id}", kind="token_not_mcp_owner"
        )


def _resolve_build_kind(agent_id: str, connection_id: str) -> str:
    """The resolved build kind for this agent's infra template, ``"container"`` when unknown.

    Read from the build service's own chain resolver rather than walked here, so the rule that
    gates the tag and the mechanism the service deploys by cannot disagree. Degrades to
    ``"container"`` on any fault, the strict direction: a config build refused during a store blip
    is a retryable 403, an unbound tag accepted during one is a cross-agent deploy. The chain is
    resolved again inside the service; why that is safe is recorded in the decision below.
    """
    # Decision: docs/adr/backend-build-kind-resolved-at-the-route-and-in-the-service.md
    try:
        resolved = get_build_service().resolve_infra_template(agent_id=agent_id, connection_id=connection_id)
    except Exception:
        logger.exception(
            "[builds] could not resolve the build kind for agent %s; applying the container tag rule",
            agent_id,
        )
        return "container"
    kind = getattr(resolved, "build_kind", "") if resolved is not None else ""
    return kind or "container"


def _assert_tag_belongs_to_agent(
    gh: GitHubActionsClaims,
    agent_id: str,
    image_tag: str,
    build_kind: str = "container",
) -> None:
    """Bind the candidate image tag to the agent. The binding above proves the token owns the
    agent, not that the tag names that agent's artifact, and the tag is the value an owner's
    production approval attests to.

    On the container path the tenant ECR repository is shared by every materialized agent, so the
    ``<agent_id>-`` prefix is the only agent-identity boundary inside the registry (the buildspec
    re-derives the agent id from it), and the whole tag must fit the tag grammar so no whitespace
    or metacharacter reaches the buildspec's shell. A config agent has no image: its workflow posts
    a bare 40-hex commit sha, so that path requires exactly that shape instead, not as a union with
    the container rule, which would make the gate the weaker of the two everywhere. The default is
    ``"container"``, which keeps ``/prod-candidate`` unchanged: a config agent can never have a
    prod candidate. The detail never echoes the offending tag.
    """
    if build_kind == _BUILD_KIND_CONFIG:
        tag_is_bound = bool(_COMMIT_SHA_RE.match(image_tag))
    else:
        tag_is_bound = bool(image_tag.startswith(f"{agent_id}-") and _IMAGE_TAG_RE.match(image_tag))
    if not tag_is_bound:
        logger.warning(
            "[builds] candidate tag/agent binding failed: token repo=%s agent_id=%s",
            gh.repository,
            agent_id,
        )
        raise RuntimeBuildError(f"the image tag is not bound to {agent_id}", kind="tag_not_owned")


@router.post("/runtime", response_model=RuntimeBuildResponse, status_code=202)
async def start_runtime_build(
    body: RuntimeBuildRequest,
    gh: GitHubActionsClaims = Depends(verify_github_oidc),
):
    """Start the runtime-provision CodeBuild for a pushed agent image or MCP-server repo.

    Auth is GitHub OIDC. Only ``stage`` is trusted from the body, and only ``dev`` is accepted over
    this path: prod is an AGP-owned decision the promote route gates on an owner, and refusing it
    here, not in the service, is what makes that gate real, because the promote route starts prod
    builds through the same service call. Every refusal precedes every side effect.
    """
    _resolve_connection_for_token(gh, body.connection_id)
    if body.stage not in ("dev", "prod"):
        raise RuntimeBuildError("the stage is neither dev nor prod", kind="invalid_stage")
    if body.mcp_server_id is not None:
        return await _start_mcp_build(body, gh, body.mcp_server_id)
    return await _start_agent_build(body, gh, body.agent_id or "")


async def _start_agent_build(
    body: RuntimeBuildRequest, gh: GitHubActionsClaims, agent_id: str
) -> RuntimeBuildResponse:
    """The agent half of ``POST /builds/runtime``: the binding and tag checks, then the prod
    refusal, each before anything is started, so a refusal never describes a build already in
    motion. The tag rule is resolved from the infra chain, never from the body, because a caller
    who could name their own build kind could name the weaker rule. The build call is sync and
    blocking, so it runs off the event loop."""
    _assert_token_owns_agent(gh, agent_id)
    _assert_tag_belongs_to_agent(
        gh, agent_id, body.image_tag, _resolve_build_kind(agent_id, body.connection_id)
    )
    if body.stage == "prod":
        raise RuntimeBuildError("a prod build was requested over OIDC", kind="prod_from_agp_only")

    build_svc = get_build_service()
    try:
        build_id = await anyio.to_thread.run_sync(
            functools.partial(
                build_svc.start_runtime_build,
                agent_id=agent_id,
                image_tag=body.image_tag,
                ecr_repo=body.ecr_repo,
                connection_id=body.connection_id,
                stage=body.stage,
                image_digest=body.image_digest or "",
            )
        )
    except RuntimeBuildError as exc:
        _record_failed_start(gh, body, exc)
        raise

    _append_build_record(gh, body, build_id=build_id, failed=False)
    _register_candidate_from_build(gh, body, agent_id)
    return RuntimeBuildResponse(build_id=build_id, status="started")


async def _start_mcp_build(
    body: RuntimeBuildRequest, gh: GitHubActionsClaims, mcp_server_id: str
) -> RuntimeBuildResponse:
    """The MCP-server half of ``POST /builds/runtime``: the same refusal order as the agent half.
    The tag rule is the config rule unconditionally, since an MCP server only has a config build;
    no prod candidate is registered, MCP servers have no promotion yet."""
    _assert_token_owns_mcp(gh, mcp_server_id)
    _assert_tag_belongs_to_agent(gh, mcp_server_id, body.image_tag, _BUILD_KIND_CONFIG)
    if body.stage == "prod":
        raise RuntimeBuildError("a prod build was requested over OIDC", kind="prod_from_agp_only")

    try:
        build_id = await anyio.to_thread.run_sync(
            functools.partial(
                get_build_service().start_runtime_build,
                mcp_server_id=mcp_server_id,
                image_tag=body.image_tag,
                ecr_repo=body.ecr_repo,
                connection_id=body.connection_id,
                stage=body.stage,
                image_digest="",
                expected_build_kind=_BUILD_KIND_CONFIG,
            )
        )
    except RuntimeBuildError as exc:
        _record_failed_start(gh, body, exc)
        raise
    _append_build_record(gh, body, build_id=build_id, failed=False)
    return RuntimeBuildResponse(build_id=build_id, status="started")


def _record_failed_start(gh: GitHubActionsClaims, body: RuntimeBuildRequest, exc: RuntimeBuildError) -> None:
    """A build that could not start is still a delivery attempt and gets a FAILED history row, so
    "the build failed" and "nobody ever pushed" do not look identical. A build already in progress
    gets no row (the push is waiting its turn, not failing) and neither does an unbuildable MCP name
    (a defect in the record, not a delivery)."""
    if exc.kind not in _REFUSALS_WITHOUT_A_DELIVERY_ROW:
        _append_build_record(gh, body, build_id=None, failed=True)


def _register_candidate_from_build(gh: GitHubActionsClaims, body: RuntimeBuildRequest, agent_id: str) -> None:
    """Register this build's artifact as the prod candidate if it came from the project's trunk.

    This route is the registrar because it already proves, from the validated OIDC token, which
    artifact (tag and digest), who merged (``actor``) and which commit (``sha``). The trunk gate is
    the governance control: a candidate is what an owner's production approval attests to, so only a
    ref that went through the provider's review may produce one, compared against the owning
    project's ``trunk_branch`` (``main`` only for a project record that carries none) rather than a
    ``main`` literal. Best-effort, like the delivery row: the build has already started, so nothing
    this raises may change the response the workflow sees, and the catch is total by design, since
    every refusal happened before StartBuild and nothing raised here can be one.
    """
    try:
        project_svc = get_project_service()
        repo = project_svc.find_repository_by_agent_id(agent_id)
        if repo is None:
            return
        detail = project_svc.get_project(repo.project_id)
        if detail is None:
            return
        trunk = getattr(detail.project, "trunk_branch", None) or "main"
        if gh.ref != f"refs/heads/{trunk}":
            logger.info(
                "[builds] not registering a prod candidate for agent %s: ref is not the trunk",
                agent_id,
            )
            return
        project_svc.record_prod_candidate(
            agent_id,
            image_tag=body.image_tag,
            image_digest=body.image_digest or "",
            sha=gh.sha or "",
            actor=gh.actor or "",
        )
    except Exception:
        logger.exception(
            "[builds] could not register the prod candidate for agent %s (tag %s)",
            agent_id,
            body.image_tag,
        )


def _append_build_record(
    gh: GitHubActionsClaims, body: RuntimeBuildRequest, *, build_id: Optional[str], failed: bool
) -> None:
    """Append one ``Deployment`` row for this OIDC-triggered build.

    ``actor`` and ``source_sha`` come from the validated token, never the body, and ``actor_kind``
    is ``"github"``: an OIDC-proven GitHub login is a different currency from the Entra oid a
    promote records, and rendering one as the other would misattribute a deployment. The row is
    keyed by the repository the binding check already proved owns the artifact; a terminal row is
    appended already closed, the partition being append-only. Best-effort: the build has already
    started or been refused, so a store fault must not turn a live deploy into a 500 or replace the
    curated error. The error hint is a fixed literal; the build service's message never reaches the
    record.
    """
    try:
        project_svc = get_project_service()
        if body.mcp_server_id is not None:
            repo = project_svc.find_repository_by_mcp_server_id(body.mcp_server_id)
            artifact = {"mcp_server_id": body.mcp_server_id}
        else:
            agent_id = body.agent_id or ""
            repo = project_svc.find_repository_by_agent_id(agent_id)
            artifact = {"agent_id": agent_id}
        if repo is None:
            return
        completed_at = datetime.now(timezone.utc).isoformat() if failed else None
        project_svc.append_deployment(
            repo_id=repo.id,
            **artifact,
            stage=body.stage,
            image_tag=body.image_tag,
            source_sha=gh.sha or None,
            build_id=build_id,
            outcome=DeploymentOutcome.FAILED if failed else DeploymentOutcome.STARTED,
            actor=gh.actor or None,
            actor_kind="github",
            completed_at=completed_at,
            error="failed to start the runtime build" if failed else None,
        )
    except Exception:
        logger.exception(
            "[builds] could not append the deployment record for %s (stage %s, tag %s)",
            body.agent_id or body.mcp_server_id,
            body.stage,
            body.image_tag,
        )


@router.post("/prod-candidate", response_model=ProdCandidateResponse, status_code=202)
async def register_prod_candidate(
    body: ProdCandidateRequest,
    gh: GitHubActionsClaims = Depends(verify_github_oidc),
):
    """Register ``main``'s HEAD as the prod candidate. No build is started.

    The route for a repository still running the old two-job workflow; the build trigger is the
    registrar for every other one. ``actor`` and ``sha`` are read only from the validated OIDC
    token, so the recorded "who merged" survives as attribution a promote can be audited against.
    ``image_tag`` comes from the body but must name the agent the token was just proven to own;
    ``ecr_repo`` is discarded. The same checks as ``/runtime`` run in the same order, the branch
    check after the binding checks so a foreign-repo token learns nothing about branch handling,
    and the tag check last, before the write. A vanished agent renders the project table's 404.
    """
    _resolve_connection_for_token(gh, body.connection_id)
    _assert_token_owns_agent(gh, body.agent_id)
    if gh.ref != "refs/heads/main":
        raise RuntimeBuildError(f"a prod candidate came from {gh.ref}", kind="prod_candidate_not_main")
    _assert_tag_belongs_to_agent(gh, body.agent_id, body.image_tag)

    get_project_service().record_prod_candidate(
        body.agent_id,
        image_tag=body.image_tag,
        sha=gh.sha or "",
        actor=gh.actor or "",
    )
    return ProdCandidateResponse(status="registered")
