"""Projects, their repositories and the deliveries made from them.

A ``Project`` is an empty container scoped to one org (``connection_id``). ``add_repo`` validates
the request, pre-registers the governed agent (or MCP record) and persists a pending
``Repository``; ``run_materialize`` then runs the provider steps in the background, one commit
pushed over the ``RepoProvider`` seam with ``[skip ci]``, and writes per-step status onto the
record. ``promote_repo``, ``rollback_repo`` and ``build_repo`` start deliveries through the
runtime build service and append to the append-only deployment history; ``delete_repo`` is the
ordered best-effort teardown. Materialize, delivery and teardown are module functions over the service in
:mod:`services.project_materialize`, :mod:`services.project_delivery`, :mod:`services.project_teardown`
and :mod:`services.project_runtime_teardown`, and the methods here delegate to them.

Persistence is :class:`services.project_store.ProjectStore` over one ``core.state_store.StateStore``
(DynamoDB, or in memory with ``table_name=""``). Every failure a route can see is a
:class:`services.project_errors.ProjectError` kind. Connection credentials flow only into the provider
client: never logged, never returned, never stored on a read model.
Decision: docs/adr/backend-feature-modules-over-the-service.md
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import ValidationError

from core import aws_clients
from core.state_store import StateStore
from models.agent import (
    GOVERNABLE_PLATFORMS,
    AgentAuthType,
    AgentCreate,
    DataClassification,
    IdentityStatus,
    Platform,
)
from models.deployment import (
    Deployment,
    DeploymentOutcome,
    deployment_seq_key,
    new_deployment_id,
)
from models.mcp_server import MAX_SERVER_JSON_DESCRIPTION, Kind, McpServerCreate
from models.project import FrameworkMismatchError, Project, ProjectDetail, validate_agent_config
from models.repository import (
    EvalEvidence,
    RepoDeletePreview,
    RepoDeleteResult,
    RepoDeleteSelection,
    Repository,
    default_steps,
)
from services import project_delivery, project_materialize, project_runtime_teardown, project_teardown
from services.agent_registry_service import apply_creator_sponsor
from services.eval_store import get_eval_store
from services.github_repo_service import GitHubRepoService
from services.github_template_service import _NAME_RE as _TEMPLATE_NAME_RE
from services.infra_template_registry import InfraTemplateRegistry
from services.project_delivery import _eval_run_passed
from services.project_errors import ProjectError
from services.project_materialize import _CI_STAGE, _BuildKind
from services.project_store import ProjectStore
from services.runtime_build_service import RuntimeBuildService
from services.tenant_credentials import stage_client

logger = logging.getLogger(__name__)

_OPS_MCP_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_MCP_PLATFORM_ACCOUNT_ONLY = "MCP servers can only be built in the platform account for now"

_EvalGate = Literal["passed", "failed", "running", "none"]


def _data_classification(value: Optional[str]) -> Optional[DataClassification]:
    """The enum for a body-supplied classification; an unknown value is ``invalid_data_classification``
    with the enum's own sentence."""
    if not value:
        return None
    try:
        return DataClassification(value)
    except ValueError as err:
        raise ProjectError(str(err), kind="invalid_data_classification") from err


class ProjectService:
    def __init__(
        self,
        *,
        table_name: str = "",
        registry,
        identity,
        connection_service,
        github_repo_service: Optional[GitHubRepoService] = None,
        tenant_service=None,
        ecr_image_service=None,
        runtime_build_service=None,
        ecr_repository: str = "",
        langfuse_provisioning=None,
        runtime_state_bucket: str = "",
        agent_templates_dir: str = "",
        template_registry=None,
        infra_template_registry=None,
        repo_vars: Optional[Dict[str, str]] = None,
        eval_store=None,
        region: str | None = None,
        mcp_registry=None,
        mcp_identity=None,
        new_id=lambda: str(uuid4()),
        now=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.table_name = table_name
        self.region = region
        self._registry = registry
        self._identity = identity
        self._conn = connection_service
        self._ecr = ecr_image_service
        self._langfuse = langfuse_provisioning
        self._runtime_state_bucket = runtime_state_bucket
        self._builds: Optional[RuntimeBuildService] = runtime_build_service
        self._ecr_repository = ecr_repository
        self._rollout = github_repo_service or GitHubRepoService()
        self._agent_templates_dir = Path(agent_templates_dir or ".")
        self._templates = template_registry
        self._infra = infra_template_registry
        if self._infra is None and table_name:
            self._infra = InfraTemplateRegistry(table_name=table_name, region=region)
        self._tenants = tenant_service
        self._repo_vars = dict(repo_vars or {})
        self._mcp_registry = mcp_registry
        self._mcp_identity = mcp_identity
        self._platform_account: Optional[str] = None
        self._evals = eval_store or get_eval_store()
        self._new_id = new_id
        self._now = now

        self._store = StateStore(table_name, region=region)
        self._s3: Any = None
        if runtime_state_bucket:
            self._s3 = aws_clients.client("s3", region=region)
        self._iam: Any = aws_clients.client("iam", region=region)
        self._stage_client = stage_client
        self._pending_materialize: Dict[str, dict] = {}

    @property
    def _store(self) -> StateStore:
        return self._rows._store

    @_store.setter
    def _store(self, state_store: StateStore) -> None:
        self._rows = ProjectStore(state_store)

    def attach_runtime_build_service(self, runtime_build_service) -> None:
        """Wire the RuntimeBuildService AFTER construction.

        The projects route resolves the builds route's singleton and the build service resolves
        ProjectService back (it walks the infra chain), and each factory publishes its global only
        after its constructor returns, so resolving both eagerly recursed until ``RecursionError``.
        The route publishes its singleton first and then calls this; the ctor arg stays for every
        caller that already holds a build service. A regression test calls the factories for real,
        in both orders.
        """
        self._builds = runtime_build_service

    def list_projects(self) -> List[Project]:
        return self._load_all_projects()

    def get_project(self, id: str) -> Optional[ProjectDetail]:
        """A project + its repositories (filtered by ``project_id``); None if absent."""
        project = self._get_project(id)
        if project is None:
            return None
        repos = self._with_build_kinds([self._with_dev_outcome(r) for r in self._load_repos_for(id)])
        return ProjectDetail(project=project, repositories=repos)

    def create_project(
        self,
        *,
        name: str,
        connection_id: str,
        tenant_id: str,
        description: str,
        created_by: str,
    ) -> Project:
        """Persist an EMPTY container in the ``project`` partition. No agent, no repo.

        No ``trunk_branch`` parameter: every project takes ``Project.trunk_branch``'s default, the
        single branch the shipped template's workflow can build. Stored records without the key
        hydrate as ``"main"``."""
        ts = self._now().isoformat()
        record = Project(
            id=self._new_id(),
            name=name,
            connection_id=connection_id,
            tenant_id=tenant_id,
            description=description,
            created_by=created_by,
            created_at=ts,
            updated_at=ts,
        )
        self._save_project(record)
        return record

    def list_repositories(self) -> List[Repository]:
        """Flat scan of the ``repository`` partition (for the /repositories page)."""
        repos = self._load_all_repos()
        for repo in repos:
            self._reconcile_built_gateway(repo)
        return self._with_build_kinds([self._with_dev_outcome(r) for r in repos])

    def find_repository_by_agent_id(self, agent_id: str) -> Optional[Repository]:
        """The Repository that owns ``agent_id`` (its 1:1 pre-registered agent), or None.

        Scans the ``repository`` partition (mirrors :meth:`list_repositories`). Used by the
        builds route to bind a GitHub-OIDC token's proven ``repository`` to the agent_id it
        claims to build; the OIDC repo identity must own the agent."""
        return next((r for r in self._load_all_repos() if r.agent_id == agent_id), None)

    def find_repository_by_mcp_server_id(self, mcp_server_id: str) -> Optional[Repository]:
        """The Repository that owns ``mcp_server_id`` (its 1:1 pre-registered MCP record), or
        None. The MCP twin of :meth:`find_repository_by_agent_id`, same scan."""
        return next((r for r in self._load_all_repos() if r.mcp_server_id == mcp_server_id), None)

    def _platform_account_id(self) -> Optional[str]:
        """The control-plane account id, or None when it could not be determined.

        The ``langfuse_provisioning`` probe, reused: ONLY A SUCCESS IS CACHED, so one STS blip
        does not pin the process to "unknown" — and unknown REFUSES the MCP path (see
        :meth:`_in_platform_account`), so a cached failure would refuse every MCP repo until a
        restart."""
        if self._platform_account is None:
            try:
                self._platform_account = aws_clients.client("sts", region=self.region).get_caller_identity()[
                    "Account"
                ]
            except Exception:
                logger.warning("[project] could not resolve the platform account", exc_info=True)
                return None
        return self._platform_account

    def _in_platform_account(self, tenant_id: Optional[str]) -> bool:
        """True only when the tenant's DEV stage is PROVABLY the platform account.

        The MCP services address the gateway with the AMBIENT platform-account client (nothing goes
        through ``stage_client`` there), so a gateway built in another account would be invisible
        to the reconcile and to every governance flow. Every unknown — no tenant service, an
        unreadable tenant, no ``dev`` stage or account, an STS failure — answers False: the
        refusal is the safe direction."""
        if self._tenants is None or not tenant_id:
            return False
        try:
            tenant = self._tenants.get(tenant_id)
        except Exception:
            logger.warning(
                "[project] tenant %s is unreadable; refusing the MCP path", tenant_id, exc_info=True
            )
            return False
        cfg = (getattr(tenant, "stages", None) or {}).get(_CI_STAGE) if tenant else None
        account = getattr(cfg, "account_id", "") if cfg is not None else ""
        if not account:
            return False
        return account == self._platform_account_id()

    def _agent_platform_for_tenant(self, tenant_id: Optional[str]) -> Platform:
        """The agent platform label the OWNING TENANT's platform implies.

        The pre-registered record's platform is derived from :data:`GOVERNABLE_PLATFORMS`
        (inverted: the tenant platform names the agent label) so a record can never contradict
        its own tenant. The legacy/unwired shapes answer AWS: no tenant service injected, no
        ``tenant_id``, an unreadable tenant, or a tenant record with no ``platform`` field.
        """
        wire = "aws"
        if self._tenants is not None and tenant_id:
            try:
                tenant = self._tenants.get(tenant_id)
            except Exception:
                logger.warning("[project] tenant %s is unreadable; assuming aws", tenant_id, exc_info=True)
                tenant = None
            if tenant is not None:
                wire = getattr(tenant, "platform", "") or "aws"
        for agent_platform, tenant_platform in GOVERNABLE_PLATFORMS.items():
            if tenant_platform == wire:
                return agent_platform
        raise ProjectError(
            "the owning tenant's platform cannot host a governed agent",
            kind="unsupported",
        )

    def add_repo(
        self,
        *,
        project_id: str,
        name: str,
        template_name: str,
        agent_config: Dict,
        created_by: str,
        principal,
        business_unit: Optional[str] = None,
        region: Optional[str] = None,
        data_classification: Optional[str] = None,
        purpose: Optional[str] = None,
        repo_overrides: Optional[Dict[str, str]] = None,
        mcp_name: Optional[str] = None,
    ) -> Repository:
        """Pre-register the agent and persist a PENDING repo record; the route returns 202.

        Only the sync work happens here, and every refusal comes before the first side effect:
        the template name is validated at the boundary (the deeper containment check in
        ``_resolve_scaffold_dir`` stays), the project is resolved, then ``agent.config`` is checked
        against the registered template's framework. The governed Agent record is created with
        ``agent_arn=None`` and the project's tenant and ``project_id``; the ``Repository`` is
        persisted with a full ``pending`` timeline; the materialize inputs are stashed in
        ``_pending_materialize`` for :meth:`run_materialize`. The connection token is never
        stashed."""
        if not _TEMPLATE_NAME_RE.match(template_name or ""):
            raise ProjectError(
                f"invalid template name {template_name!r} — a template name must match "
                f"{_TEMPLATE_NAME_RE.pattern}",
                kind="invalid_template_name",
            )

        project = self._get_project(project_id)
        if project is None:
            raise ProjectError("Project not found", kind="not_found")

        record = self._get_template_record(project.connection_id, template_name)
        if record is not None and getattr(record, "artifact_kind", "agent") == "mcp_server":
            return self._add_mcp_repo(
                project=project,
                record=record,
                name=name,
                template_name=template_name,
                mcp_name=mcp_name or name,
                created_by=created_by,
                principal=principal,
                business_unit=business_unit,
                region=region,
                data_classification=data_classification,
                purpose=purpose,
                repo_overrides=repo_overrides,
            )
        try:
            validate_agent_config(agent_config, record.framework if record is not None else "")
        except FrameworkMismatchError:
            raise
        except ValueError as err:
            raise ProjectError(str(err), kind="invalid_agent_name") from err

        try:
            spec = AgentCreate(
                name=agent_config["agent_name"],
                purpose=(purpose or "").strip()
                or f"Agent for repository '{name}' (template '{template_name}')",
                auth_type=AgentAuthType.ENTRA,
                platform=self._agent_platform_for_tenant(project.tenant_id),
                framework=agent_config.get("framework"),
                model_id=agent_config.get("model_id"),
                business_unit=business_unit,
                region=region,
                data_classification=_data_classification(data_classification),
                agent_arn=None,
                tenant_id=project.tenant_id,
            )
        except ValidationError as err:
            raise ProjectError(str(err), kind="invalid_agent_config") from err

        agent = self._registry.create(
            apply_creator_sponsor(spec, principal),
            created_by=created_by,
            project_id=project_id,
        )

        ts = self._now().isoformat()
        repo_id = self._new_id()

        record = Repository(
            id=repo_id,
            project_id=project_id,
            name=name,
            repo_url=None,
            agent_id=agent.id,
            template_name=template_name,
            cicd_status="provisioning",
            status="provisioning",
            created_by=created_by,
            created_at=ts,
            updated_at=ts,
            steps=default_steps(),
        )
        self._save_repo(record, include_cicd_status=True)

        self._pending_materialize[repo_id] = {
            "agent": agent,
            "name": name,
            "connection_id": project.connection_id,
            "template_name": template_name,
            "agent_config": agent_config,
            "repo_overrides": repo_overrides,
            "tenant_id": project.tenant_id,
            "trunk_branch": project.trunk_branch,
        }
        return record

    def _add_mcp_repo(
        self,
        *,
        project: Project,
        record,
        name: str,
        template_name: str,
        mcp_name: str,
        created_by: str,
        principal,
        business_unit: Optional[str],
        region: Optional[str],
        data_classification: Optional[str],
        purpose: Optional[str],
        repo_overrides: Optional[Dict[str, str]],
    ) -> Repository:
        """:meth:`add_repo` for an ``mcp_server`` template.

        Every refusal comes BEFORE the first side effect (the MCP record create), as
        ``ProjectError(kind="mcp_refused")`` whose message is a fixed AGP literal:

        * the template's INFRA does not declare ``mcp_server`` in ``artifact_kinds`` — a template
          pairing ``mcp_server`` with the runtime or harness infra would build the wrong thing;
        * ``mcp_name`` fails :data:`_OPS_MCP_NAME_RE` — the gateway name rule (no underscores) and
          the 64-char IAM role-name budget of ``agp-<name>-<stage>-gateway-role``;
        * the tenant's dev account is not provably the platform account — see
          :meth:`_in_platform_account`.

        The repo id is generated FIRST so the MCP record is born owning it (``repo_id`` +
        ``project_id``, server-side keywords no request body can supply). Then the repo row is
        persisted with ``agent_id=None`` and the inputs are stashed under ``"mcp"`` (``"agent"`` is
        None) for :meth:`run_materialize`."""
        if self._mcp_registry is None:
            raise ProjectError("MCP servers are not available on this platform", kind="mcp_refused")
        infra = (
            self._infra.get(project.connection_id, getattr(record, "infra_template_id", ""))
            if self._infra is not None
            else None
        )
        if infra is None or "mcp_server" not in (getattr(infra, "artifact_kinds", None) or []):
            raise ProjectError(
                "this template's infrastructure cannot build an MCP server", kind="mcp_refused"
            )
        if not _OPS_MCP_NAME_RE.match(mcp_name):
            raise ProjectError(f"MCP server name must match {_OPS_MCP_NAME_RE.pattern}", kind="mcp_refused")
        if not self._in_platform_account(project.tenant_id):
            raise ProjectError(_MCP_PLATFORM_ACCOUNT_ONLY, kind="mcp_refused")

        repo_id = self._new_id()
        try:
            spec = McpServerCreate(
                name=mcp_name,
                description=((purpose or "").strip() or f"MCP server {mcp_name}")[
                    :MAX_SERVER_JSON_DESCRIPTION
                ],
                kind=Kind.GATEWAY,
                tenant_id=project.tenant_id,
                owner_oid=getattr(principal, "oid", None),
                owner_email=getattr(principal, "email", None),
                business_unit=business_unit,
                region=region,
                data_classification=_data_classification(data_classification),
            )
        except ValidationError as err:
            raise ProjectError(str(err), kind="invalid_agent_config") from err
        mcp = self._mcp_registry.create(spec, created_by=created_by, project_id=project.id, repo_id=repo_id)

        ts = self._now().isoformat()
        repo = Repository(
            id=repo_id,
            project_id=project.id,
            name=name,
            repo_url=None,
            artifact_kind="mcp_server",
            mcp_server_id=mcp.id,
            template_name=template_name,
            cicd_status="provisioning",
            status="provisioning",
            created_by=created_by,
            created_at=ts,
            updated_at=ts,
            steps=default_steps(),
        )
        self._save_repo(repo, include_cicd_status=True)
        self._pending_materialize[repo_id] = {
            "agent": None,
            "mcp": mcp,
            "name": name,
            "connection_id": project.connection_id,
            "template_name": template_name,
            "agent_config": {},
            "repo_overrides": repo_overrides,
            "tenant_id": project.tenant_id,
            "trunk_branch": project.trunk_branch,
        }
        return repo

    def get_repo(self, repo_id: str) -> Optional[Repository]:
        """Public read of a single Repository record with its live ``steps`` timeline; None when
        absent."""
        repo = self._get_repo(repo_id)
        if repo is None:
            return None
        self._reconcile_built_gateway(repo)
        return self._with_build_kinds([self._with_dev_outcome(repo)])[0]

    def _reconcile_built_gateway(self, repo: Repository) -> None:
        """Adopt a Terraform-built gateway onto the repo's MCP record.

        Nothing calls the backend after a build: the buildspec's gateway arm writes only the repo
        row's ``gateway_*``. So a repo READ is the trigger — an MCP repo whose ``gateway_arn`` is
        set hands it to :meth:`McpIdentityService.adopt_built_gateway`, which records the gateway,
        its tools and ``identity_status=provisioned`` on the MCP record in ONE write, or makes no
        write when all of that already matches.

        NEVER RAISES. A read must not fail over a reconcile: any error (registry read, GetGateway,
        the write) is logged with its traceback and swallowed, and the next read retries."""
        if repo.artifact_kind != "mcp_server" or not repo.gateway_arn:
            return
        if self._mcp_registry is None or self._mcp_identity is None:
            return
        try:
            mcp = self._mcp_registry.get(repo.mcp_server_id)
            if mcp is None:
                logger.warning(
                    "[project] repo %s names MCP record %s, which is gone",
                    repo.id,
                    repo.mcp_server_id,
                )
                return
            if (
                mcp.gateway_arn == repo.gateway_arn
                and getattr(mcp, "gateway_id", None) == repo.gateway_id
                and getattr(mcp, "gateway_url", None) == repo.gateway_url
                and getattr(mcp, "identity_status", None) == IdentityStatus.PROVISIONED
            ):
                return
            self._mcp_identity.adopt_built_gateway(
                mcp,
                gateway_arn=repo.gateway_arn,
                gateway_id=repo.gateway_id,
                gateway_url=repo.gateway_url,
            )
        except Exception:
            logger.exception("[project] could not reconcile the built gateway for repo %s", repo.id)

    def _with_build_kinds(self, repos: List[Repository]) -> List[Repository]:
        """Stamp :attr:`Repository.build_kind` onto READ responses, never on a record about to be
        written; see ``_save_repo``'s skip set.

        Cost: no provider call — :meth:`_resolve_build_kind` is two catalog ``get_item`` reads
        (template, infra template), plus one project read for its connection. Memoized per call
        so a list pays that once per distinct project / (connection, template), not per repo.

        NEVER RAISES. :meth:`_resolve_build_kind` deliberately propagates store faults (the
        materialize step must fail loudly), but a read is not a build: an unreadable chain reads
        as ``None`` (unknown) so the repo still renders — the UI simply offers no Build now."""
        connections: Dict[str, Optional[str]] = {}
        kinds: Dict[tuple, Optional[_BuildKind]] = {}
        for repo in repos:
            key = None
            try:
                if repo.project_id not in connections:
                    connections[repo.project_id] = None
                    project = self._get_project(repo.project_id)
                    connections[repo.project_id] = project.connection_id if project else None
                connection_id = connections[repo.project_id]
                key = (connection_id, repo.template_name)
                if key not in kinds:
                    kinds[key] = (
                        self._resolve_build_kind(connection_id, repo.template_name) if connection_id else None
                    )
                repo.build_kind = kinds[key]
            except Exception:
                logger.exception("[project] could not resolve the build kind for repo %s", repo.id)
                if key is not None:
                    kinds[key] = None
                repo.build_kind = None
        return repos

    def _with_dev_outcome(self, repo: Repository) -> Repository:
        """Stamp :attr:`Repository.dev_outcome_for_candidate` onto a READ response, never on a
        record about to be written; see ``_save_repo``'s skip set.

        Only queried when there IS a pending candidate: no candidate means nothing for the
        frontend gate to check, so this skips the ``list_deployments`` read entirely for the
        common case (a repo with nothing awaiting approval)."""
        if repo.prod_candidate_status == "pending" and repo.prod_candidate_image_tag:
            outcome = self._latest_dev_outcome_for_tag(repo.id, repo.prod_candidate_image_tag)
            repo.dev_outcome_for_candidate = outcome.value if outcome is not None else None
        else:
            repo.dev_outcome_for_candidate = None
        repo.eval_gate, repo.eval_regressions, repo.eval_evidence = self._eval_gate_for_candidate(repo)
        return repo

    def _eval_gate_for_candidate(
        self, repo: Repository
    ) -> tuple[Optional[_EvalGate], Optional[list[str]], Optional[EvalEvidence]]:
        """The promote eval gate's state for the candidate's digest — the SAME lookup
        :meth:`promote_repo` refuses on — and that run's regressions vs prod (flags only). State
        ``None`` for a repo without an agent (no evals) or when the store cannot be read: a read
        must not fail over the eval index. Regressions and evidence ``None`` when there is no
        candidate run."""
        if not repo.agent_id:
            return None, None, None
        if not (repo.prod_candidate_status == "pending" and repo.prod_candidate_image_tag):
            return "none", None, None
        try:
            run = self._evals.latest_run_for_build(
                repo.id, digest=repo.prod_candidate_digest, tag=repo.prod_candidate_image_tag
            )
        except Exception:
            logger.exception("[project] could not read the eval gate for repo %s", repo.id)
            return None, None, None
        if run is None:
            return "none", None, None
        sm = run.summary
        evidence = EvalEvidence(
            run_id=run.run_id,
            status=run.status,
            checks_passed=sm.checks_passed if sm else None,
            checks_total=(sm.checks_passed + sm.checks_failed) if sm else None,
            judge_means=dict(sm.judge_means) if sm else {},
            judge_min_mean=run.config.gate.judge_min_mean,
        )
        if run.status == "running":
            return "running", None, evidence
        b = run.baseline
        still_prod = b is not None and (
            b.digest == repo.last_promoted_digest
            if repo.last_promoted_digest
            else bool(b.build_tag) and b.build_tag == repo.last_promoted_image_tag
        )
        return (
            ("passed" if _eval_run_passed(run) else "failed"),
            (list(run.regressions) if still_prod else None),
            evidence,
        )

    def run_materialize(self, repo_id: str) -> None:
        return project_materialize.run_materialize(self, repo_id)

    def retry_materialize(self, repo_id: str) -> Repository:
        return project_materialize.retry_materialize(self, repo_id)

    def promote_repo(
        self,
        project_id: str,
        repo_id: str,
        *,
        promoted_by: str,
        override_reason: Optional[str] = None,
        promoted_by_name: Optional[str] = None,
    ) -> Repository:
        return project_delivery.promote_repo(
            self,
            project_id,
            repo_id,
            promoted_by=promoted_by,
            override_reason=override_reason,
            promoted_by_name=promoted_by_name,
        )

    def record_prod_candidate(
        self,
        agent_id: str,
        *,
        image_tag: str,
        sha: str,
        actor: str,
        image_digest: str = "",
    ) -> Repository:
        return project_delivery.record_prod_candidate(
            self, agent_id, image_tag=image_tag, sha=sha, actor=actor, image_digest=image_digest
        )

    def _resolve_delivery_repo(self, project_id: str, repo_id: str) -> Repository:
        return project_delivery._resolve_delivery_repo(self, project_id, repo_id)

    def rollback_repo(
        self,
        project_id: str,
        repo_id: str,
        *,
        image_tag: str,
        rolled_back_by: str,
        stage: str = "prod",
    ) -> Repository:
        return project_delivery.rollback_repo(
            self, project_id, repo_id, image_tag=image_tag, rolled_back_by=rolled_back_by, stage=stage
        )

    def build_repo(self, project_id: str, repo_id: str, *, requested_by: str) -> Repository:
        return project_delivery.build_repo(self, project_id, repo_id, requested_by=requested_by)

    def _has_succeeded(self, repo_id: str, stage: str, image_tag: str) -> bool:
        return self._rows.has_succeeded(repo_id, stage, image_tag)

    def _latest_dev_outcome_for_tag(self, repo_id: str, image_tag: str) -> Optional[DeploymentOutcome]:
        return self._rows.latest_dev_outcome_for_tag(repo_id, image_tag)

    def _get_template_record(self, connection_id: str, template_name: str):
        return project_materialize._get_template_record(self, connection_id, template_name)

    def _resolve_build_kind(self, connection_id: str, template_name: str) -> _BuildKind:
        return project_materialize._resolve_build_kind(self, connection_id, template_name)

    def delete_repo(
        self, *, project_id: str, repo_id: str, selection: RepoDeleteSelection
    ) -> RepoDeleteResult:
        return project_teardown.delete_repo(self, project_id=project_id, repo_id=repo_id, selection=selection)

    def preview_delete(self, *, project_id: str, repo_id: str) -> RepoDeletePreview:
        return project_teardown.preview_delete(self, project_id=project_id, repo_id=repo_id)

    def delete_project(self, project_id: str) -> None:
        return project_teardown.delete_project(self, project_id)

    def _stage_cfg(self, tenant_id: Optional[str], stage: Optional[str]):
        return project_runtime_teardown._stage_cfg(self, tenant_id, stage)

    def append_deployment(
        self,
        *,
        repo_id: str,
        agent_id: Optional[str] = None,
        stage: str,
        image_tag: str,
        mcp_server_id: Optional[str] = None,
        source_sha: Optional[str] = None,
        build_id: Optional[str] = None,
        outcome: DeploymentOutcome = DeploymentOutcome.STARTED,
        actor: Optional[str] = None,
        actor_kind: Optional[str] = None,
        completed_at: Optional[str] = None,
        error: Optional[str] = None,
    ) -> Deployment:
        """Append ONE delivery record to the ``deployment`` partition and return it.

        Append-only by construction: the sk carries ``started_at`` + a per-row id suffix, so a
        new attempt is always a NEW row and nothing is ever read-modify-written. That removes
        the whole clobber class the repository row suffers from (see :meth:`_save_repo`) and it
        is why history and rollback are possible at all — the ``last_promoted_*`` scalars are
        overwritten by the next promote, so they cannot be the record of what happened.

        ``stage`` is free-form and NOT validated against a dev/prod literal. ``error`` is
        a SAFE short hint only — never a token or a response body. ``actor_kind``
        (``"github"``/``"entra"``) exists so an OIDC-proven login and an Entra oid are never
        rendered as the same currency.

        With no table configured the record lands in the local list instead (the same
        degrade-don't-crash fallback every other write here has).

        The delivered artifact is EXACTLY ONE of ``agent_id`` / ``mcp_server_id``; anything
        else is an AGP bug and raises ``ValueError``."""
        if bool(agent_id) == bool(mcp_server_id):
            raise ValueError("a deployment names exactly one of agent_id / mcp_server_id")
        started_at = self._now().isoformat()
        id = new_deployment_id()
        record = Deployment(
            id=id,
            repo_id=repo_id,
            agent_id=agent_id,
            mcp_server_id=mcp_server_id,
            stage=stage,
            seq_key=deployment_seq_key(repo_id, stage, started_at, id),
            image_tag=image_tag,
            source_sha=source_sha,
            build_id=build_id,
            outcome=outcome,
            actor=actor,
            actor_kind=actor_kind,
            started_at=started_at,
            completed_at=completed_at,
            error=error,
        )
        self._save_deployment(record)
        return record

    def list_deployments(
        self, repo_id: str, stage: Optional[str] = None, limit: int = 50
    ) -> List[Deployment]:
        return self._rows.list_deployments(repo_id, stage, limit)

    def _all_stage_deployments(self, repo_id: str, limit: int) -> List[Deployment]:
        return self._rows.all_stage_deployments(repo_id, limit)

    def _save_deployment(self, record: Deployment) -> None:
        return self._rows.save_deployment(record)

    def _get_project(self, id: str) -> Optional[Project]:
        return self._rows.get_project(id)

    def _load_all_projects(self) -> List[Project]:
        return self._rows.load_all_projects()

    def _save_project(self, record: Project) -> None:
        return self._rows.save_project(record)

    def _load_repos_for(self, project_id: str) -> List[Repository]:
        return self._rows.load_repos_for(project_id)

    def _load_all_repos(self) -> List[Repository]:
        return self._rows.load_all_repos()

    def _save_repo(
        self,
        record: Repository,
        *,
        include_cicd_status: bool = False,
        include_prod_candidate: bool = False,
    ) -> None:
        return self._rows.save_repo(
            record, include_cicd_status=include_cicd_status, include_prod_candidate=include_prod_candidate
        )

    def _save_prod_candidate(self, record: Repository) -> None:
        return self._rows.save_prod_candidate(record)

    def _write_repo(self, record: Repository, skip: set) -> None:
        return self._rows.write_repo(record, skip)

    def _get_repo(self, repo_id: str) -> Optional[Repository]:
        return self._rows.get_repo(repo_id)

    def _delete_repo(self, repo_id: str) -> None:
        return self._rows.delete_repo(repo_id)

    def _delete_project(self, project_id: str) -> None:
        return self._rows.delete_project(project_id)
