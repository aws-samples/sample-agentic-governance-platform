"""Runtime-build orchestration: resolve a Git connection, mint a ready clone token into a
short-lived scratch secret, then StartBuild the runtime-provision CodeBuild project.

A GitHub Action, authenticated through GitHub OIDC at the route, or the project service's promote,
rollback and build-now paths call ``start_runtime_build``. The build's env carries the org's
connection metadata (org and base URL, never a credential) and a git-clone contract pointing at the
org's private infra repo.

The scratch secret exists because the buildspec clones with ``jq -r '.token'``: a PAT connection's
secret is ``{"token": ...}`` but a GitHub App connection's is ``{"private_key": ...}``, so the
connection's own secret cannot be passed. ``get_bearer_token`` resolves a ready token for both
types, it is written to a per-build scratch secret, and only that scratch ARN enters the StartBuild
payload. The token is never logged and never placed in the env; CodeBuild reads the secret under its
own role. Cleanup has three legs: the buildspec's ``post_build`` purges the secret of every build that
started, ``_delete_secret_best_effort`` covers a StartBuild that faulted after the write, and
``scripts/sweep_runtime_build_tokens.py`` is the operator backstop.

The deploy mechanism is resolved from the infra chain (agent, repository, template, infra template)
and never from the caller; an unresolvable chain folds to the container path, and the route resolves
the same chain once more to gate the image tag.
Decision: docs/adr/backend-build-kind-resolved-at-the-route-and-in-the-service.md

The boto3 clients and every collaborating service are injected, so tests pass fakes.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import re
from collections.abc import Mapping
from typing import ClassVar

from core.errors import MESSAGE, AppError, Outcome
from services.platform_registry import declaration_for
from services.tenant_service import TenantError

logger = logging.getLogger(__name__)

_INFRA_REPO_NAME = "agp-runtime-infra"
_DEFAULT_IAC_TYPE = "agentcore_runtime"
_BUILD_KIND_CONFIG = "config"
_MCP_NAME_RE = re.compile(r"\A[a-z][a-z0-9-]{1,30}\Z")
_FAILED_TO_START = Outcome(502, {"status": "failed_to_start"})


class RuntimeBuildError(AppError):
    """A runtime build could not be started. The message is a fixed literal of this module plus
    the ids the caller sent, never a token, a secret or an upstream response; ``conflict`` sends it
    to the client, every other kind has a fixed detail.

    ``conflict``: a build for the same artifact and stage is already running, the 409 the GitHub
    trigger job backs off and retries. ``not_found``: the agent, MCP server, tenant or stage could
    not be resolved. ``unsupported``: the tenant's platform offers no ops lane. ``build_kind_mismatch``:
    a config build was expected and the infra chain did not resolve to one. ``invalid_name``: the MCP
    server's name is outside the rule the buildspec's shell and tfvars can accept.
    ``credential_staging_failed`` and ``start_failed``: the scratch-secret write or StartBuild
    faulted. Each of these but ``conflict`` and ``invalid_name`` renders the ``failed_to_start`` body
    the workflow reads.

    The build-trigger routes raise the rest, each a fixed sentence and every one before a side
    effect: the GitHub OIDC token's org, repository or image tag is not bound to the posted
    artifact (``token_org_mismatch``, ``token_not_agent_owner``, ``token_not_mcp_owner``,
    ``tag_not_owned``), the stage is neither ``dev`` nor ``prod`` (``invalid_stage``), a prod build
    was asked for over OIDC (``prod_from_agp_only``), or a prod candidate came from a ref other than
    ``main`` (``prod_candidate_not_main``).
    """

    domain: ClassVar[str] = "runtime_build"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "conflict": Outcome(409, {"status": "build_in_progress", "message": MESSAGE}),
        "not_found": _FAILED_TO_START,
        "unsupported": _FAILED_TO_START,
        "build_kind_mismatch": _FAILED_TO_START,
        "credential_staging_failed": _FAILED_TO_START,
        "start_failed": _FAILED_TO_START,
        "invalid_name": Outcome(422, "MCP server name is not buildable"),
        "token_org_mismatch": Outcome(403, "Token repository_owner does not match connection org"),
        "token_not_agent_owner": Outcome(403, "Token repository does not own this agent"),
        "token_not_mcp_owner": Outcome(403, "Token repository does not own this MCP server"),
        "tag_not_owned": Outcome(403, "Image tag does not belong to this agent"),
        "invalid_stage": Outcome(422, "Invalid stage"),
        "prod_from_agp_only": Outcome(403, "prod deploys must be initiated from AGP"),
        "prod_candidate_not_main": Outcome(403, "prod candidates come from main only"),
    }

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message, kind)


class RuntimeBuildService:
    def __init__(
        self,
        codebuild_client,
        connection_service,
        secretsmanager_client,
        *,
        tenant_service,
        agent_registry,
        codebuild_project_name: str,
        scratch_secret_prefix: str,
        project_service=None,
        template_registry=None,
        infra_template_registry=None,
        mcp_registry=None,
        credential_service=None,
    ) -> None:
        """The infra chain services, the MCP registry and the credential service are optional: a
        service wired without the chain resolves nothing and takes the container path; unwired, an
        MCP build is refused and a harness build sends the two tool-auth variables empty."""
        self._codebuild = codebuild_client
        self._connections = connection_service
        self._sm = secretsmanager_client
        self._tenants = tenant_service
        self._registry = agent_registry
        self._project_name = codebuild_project_name
        self._scratch_prefix = scratch_secret_prefix
        self._projects = project_service
        self._templates = template_registry
        self._infra_templates = infra_template_registry
        self._mcp = mcp_registry
        self._credentials = credential_service

    def attach_project_service(self, project_service) -> None:
        """Wire the ProjectService after construction, the other half of the route-level
        cycle-breaker on ``ProjectService.attach_runtime_build_service``: each route factory
        publishes its singleton only after its constructor returns, so resolving both eagerly
        recursed. A service left unattached would fold every config agent to the container path,
        which is why the route attaches inside the same guard that builds the singleton."""
        self._projects = project_service

    def _resolve_chain(self, agent_id: str, connection_id: str):
        """Walk the infra chain once, returning ``(repo, infra_template)``, either of which may be
        ``None``. Both halves come back because the config path needs both: the infra record decides
        the mechanism and the repo's name is the agent repo the build must clone, and one walk keeps
        the two answers from being read at different moments."""
        if not (self._projects and self._templates and self._infra_templates):
            return None, None
        try:
            repo = self._projects.find_repository_by_agent_id(agent_id)
            if repo is None:
                return None, None
            template = self._templates.get(connection_id, repo.template_name)
            if template is None:
                return repo, None
            infra_id = getattr(template, "infra_template_id", "") or ""
            if not infra_id:
                return repo, None
            return repo, self._infra_templates.get(connection_id, infra_id)
        except Exception:
            logger.exception(
                "[runtime_build] could not resolve the infra chain for agent_id=%s; container path taken",
                agent_id,
            )
            return None, None

    def resolve_infra_template(self, *, agent_id: str, connection_id: str):
        """Walk the infra chain (agent, repository, template, infra template) or return ``None``.

        The chain is the only source of ``iac_type``: never the request body, for the same reason
        the deploy tenant is not, since a caller who can name the mechanism can ask to be deployed by
        a module its template was never built for. Public because the builds route gates the image
        tag on the resolved ``build_kind`` and must read the same resolver the env assembly uses.

        Returns ``None`` and never raises for every cannot-resolve case: chain services not wired,
        an agent with no owning repository, an absent template or infra template, or a store fault.
        The caller folds ``None`` to the container path; a transient store fault must not fail a
        container build that needs no catalog row to succeed.
        """
        return self._resolve_chain(agent_id, connection_id)[1]

    def start_runtime_build(
        self,
        *,
        agent_id: str | None = None,
        mcp_server_id: str | None = None,
        image_tag: str,
        ecr_repo: str,
        connection_id: str,
        stage: str,
        image_digest: str = "",
        expected_build_kind: str | None = None,
    ) -> str:
        """Resolve the connection, mint a ready clone token into a scratch secret, then StartBuild
        the runtime-provision project; returns the CodeBuild build id.

        Exactly one of ``agent_id`` and ``mcp_server_id`` (else ``ValueError``); an MCP server takes
        the gateway config build. ``image_digest`` is the ``sha256:`` reference of the exact bytes to
        deploy, threaded from the caller so the approved value is never re-derived from the registry
        at deploy time; empty on the tag-only path (a rollback, or a candidate recorded without one).
        ``expected_build_kind="config"`` refuses with ``build_kind_mismatch`` when the chain does not
        resolve to a config infra template, before any secret is written.

        Connection-resolution failures propagate as the connection service's ``ConnectionError``;
        every other refusal and fault is a ``RuntimeBuildError`` kind (see the class).
        """
        if mcp_server_id is not None:
            if agent_id is not None:
                raise ValueError("exactly one of agent_id or mcp_server_id is required")
            return self._start_mcp_build(
                mcp_server_id=mcp_server_id, image_tag=image_tag, connection_id=connection_id, stage=stage
            )
        if agent_id is None:
            raise ValueError("exactly one of agent_id or mcp_server_id is required")
        return self._start_agent_build(
            agent_id=agent_id,
            image_tag=image_tag,
            ecr_repo=ecr_repo,
            connection_id=connection_id,
            stage=stage,
            image_digest=image_digest,
            expected_build_kind=expected_build_kind,
        )

    def _start_agent_build(
        self,
        *,
        agent_id: str,
        image_tag: str,
        ecr_repo: str,
        connection_id: str,
        stage: str,
        image_digest: str,
        expected_build_kind: str | None,
    ) -> str:
        """The agent half of :meth:`start_runtime_build`. The in-flight check runs first, before the
        connection is resolved; the tenant is derived from the agent's record, never the body; the
        mechanism comes from the chain, and the scratch token is written only once every refusal has
        passed."""
        if self._has_in_flight_build(id_var="AGENT_ID", artifact_id=agent_id, stage=stage):
            raise RuntimeBuildError(
                f"a build for agent_id={agent_id} stage={stage} is already in progress",
                kind="conflict",
            )
        conn = self._connections.get_connection(connection_id)
        agent = self._registry.get(agent_id)
        if agent is None:
            raise RuntimeBuildError("unknown agent", kind="not_found")
        tenant, cfg = self._resolve_stage(agent.tenant_id, stage)

        repo, infra = self._resolve_chain(agent_id, connection_id)
        if expected_build_kind == _BUILD_KIND_CONFIG and (
            infra is None or infra.build_kind != _BUILD_KIND_CONFIG
        ):
            raise RuntimeBuildError(
                "config build expected but the infra chain did not resolve to one",
                kind="build_kind_mismatch",
            )
        scratch_arn = self._write_scratch_token(image_tag, connection_id)

        if repo is not None and infra is not None and infra.build_kind == _BUILD_KIND_CONFIG:
            return self._start_config_build(
                agent_id=agent_id,
                image_tag=image_tag,
                infra=infra,
                agent_repo_name=repo.name,
                conn=conn,
                cfg=cfg,
                scratch_arn=scratch_arn,
                tenant_id=agent.tenant_id,
                stage=stage,
                agent=agent,
            )
        env = self._container_env(
            agent_id=agent_id,
            image_tag=image_tag,
            image_digest=image_digest,
            ecr_repo=ecr_repo,
            conn=conn,
            tenant=tenant,
            tenant_id=agent.tenant_id,
            cfg=cfg,
            scratch_arn=scratch_arn,
            stage=stage,
        )
        return self._start(env, agent_id=agent_id, image_tag=image_tag, scratch_arn=scratch_arn)

    def _resolve_stage(self, tenant_id, stage: str):
        """The owning tenant and the stage's cross-account config, derived server-side from the
        artifact's record. An unknown tenant (a legacy record with no ``tenant_id`` included) and a
        stage the tenant does not carry are both ``not_found``; a tenant whose platform declares no
        ops lane, or declares nothing, is ``unsupported``, because the stage reads below are AWS-only
        and there is no AgentCore runtime to build for any other platform."""
        try:
            tenant = self._tenants.get(tenant_id)
            cfg = tenant.stages[stage]
        except (TenantError, KeyError) as exc:
            raise RuntimeBuildError("unknown tenant or stage", kind="not_found") from exc
        try:
            ops_lane = declaration_for(getattr(tenant, "platform", "aws") or "aws").ops_lane
        except ValueError:
            ops_lane = False
        if not ops_lane:
            raise RuntimeBuildError("runtime builds are only supported for AWS tenants", kind="unsupported")
        return tenant, cfg

    def _container_env(
        self,
        *,
        agent_id: str,
        image_tag: str,
        image_digest: str,
        ecr_repo: str,
        conn,
        tenant,
        tenant_id: str,
        cfg,
        scratch_arn: str,
        stage: str,
    ) -> list:
        """The StartBuild env of a container build.

        ``IMAGE_DIGEST`` is always sent, empty on the tag-only path, so the buildspec's emptiness
        branch reads a defined variable. ``ECR_REPO`` is the tenant stage's registry, the request's
        value only while the stage has none yet. ``AGENT_ID`` is sent explicitly so
        :meth:`_has_in_flight_build` can read it off a running build. ``AGENT_REGISTRY_ID`` is a
        per-build override because AWS mints registry ids and the buildspec's record read and
        write-back need the id the control plane is using. ``TARGET_ROLE_ARN`` empty means deploy in
        place; no exec-role variable is sent, the module self-provisions that role in the target
        account. On a prod build whose dev stage lives in another account the ``SOURCE_*`` block asks
        the buildspec to copy the image from the dev registry first: ``SOURCE_ECR_REQUIRED`` fires on
        the account id alone, so a dev stage that never built fails the build loud rather than
        provisioning prod against an image that is not there; a prod-only tenant copies nothing.
        """
        env = [
            {"name": "IAC_TYPE", "value": _DEFAULT_IAC_TYPE, "type": "PLAINTEXT"},
            {"name": "DEPLOYMENT_ID", "value": f"agentcore-runtime-{image_tag}", "type": "PLAINTEXT"},
            {"name": "IMAGE_TAG", "value": image_tag, "type": "PLAINTEXT"},
            {"name": "AGENT_ID", "value": agent_id, "type": "PLAINTEXT"},
            {"name": "IMAGE_DIGEST", "value": image_digest or "", "type": "PLAINTEXT"},
            {"name": "ECR_REPO", "value": cfg.ecr_repo_uri or ecr_repo, "type": "PLAINTEXT"},
            {"name": "GIT_INFRA_ORG", "value": conn.org, "type": "PLAINTEXT"},
            {"name": "GIT_INFRA_REPO", "value": _INFRA_REPO_NAME, "type": "PLAINTEXT"},
            {"name": "GIT_BASE_URL", "value": conn.base_url or "", "type": "PLAINTEXT"},
            {"name": "GIT_SECRET_ARN", "value": scratch_arn, "type": "PLAINTEXT"},
            {"name": "TARGET_ROLE_ARN", "value": cfg.deploy_role_arn, "type": "PLAINTEXT"},
            {"name": "TARGET_ACCOUNT_ID", "value": cfg.account_id, "type": "PLAINTEXT"},
            {"name": "AWS_TARGET_REGION", "value": cfg.region, "type": "PLAINTEXT"},
            {"name": "TENANT_ID", "value": tenant_id, "type": "PLAINTEXT"},
            {"name": "STAGE", "value": stage, "type": "PLAINTEXT"},
            {"name": "AGENT_REGISTRY_ID", "value": self._registry.registry_id, "type": "PLAINTEXT"},
        ]
        dev_cfg = tenant.stages.get("dev") if stage == "prod" else None
        if dev_cfg is not None and dev_cfg.account_id != cfg.account_id:
            env += [
                {"name": "SOURCE_ECR_REQUIRED", "value": "true", "type": "PLAINTEXT"},
                {"name": "SOURCE_ECR_REPO", "value": dev_cfg.ecr_repo_uri, "type": "PLAINTEXT"},
                {"name": "SOURCE_DEPLOY_ROLE_ARN", "value": dev_cfg.deploy_role_arn, "type": "PLAINTEXT"},
                {"name": "SOURCE_REGION", "value": dev_cfg.region, "type": "PLAINTEXT"},
            ]
        return env

    def _start_config_build(
        self,
        *,
        agent_id: str,
        image_tag: str,
        infra,
        agent_repo_name: str,
        conn,
        cfg,
        scratch_arn: str,
        tenant_id: str,
        stage: str,
        agent,
    ) -> str:
        """StartBuild for a ``build_kind="config"`` infra template.

        A config agent has no container: the repo holds a declarative definition the IaC module
        reads, so the env omits ``ECR_REPO``, ``IMAGE_DIGEST`` and the whole ``SOURCE_ECR_*`` block
        (an empty ``ECR_REPO`` would still send the buildspec's registry derivation down a branch
        whose inputs do not exist) and adds ``GIT_AGENT_REPO``, the developer's agent repo the module
        reads the config out of, resolved from the owning repository inside the chain walk and never
        from the body. ``AGENT_ID`` is explicit because the tag is a bare commit sha the container
        branch's prefix derivation would misread as a new state key per push. ``GIT_INFRA_REPO`` comes
        from the catalog record, and the two tool-auth variables are always sent, empty on failure.
        """
        env = [
            {"name": "IAC_TYPE", "value": infra.iac_type, "type": "PLAINTEXT"},
            {
                "name": "DEPLOYMENT_ID",
                "value": f"{infra.iac_type.replace('_', '-')}-{image_tag}",
                "type": "PLAINTEXT",
            },
            {"name": "IMAGE_TAG", "value": image_tag, "type": "PLAINTEXT"},
            {"name": "AGENT_ID", "value": agent_id, "type": "PLAINTEXT"},
            {"name": "GIT_INFRA_ORG", "value": conn.org, "type": "PLAINTEXT"},
            {"name": "GIT_INFRA_REPO", "value": infra.repo_name, "type": "PLAINTEXT"},
            {"name": "GIT_AGENT_REPO", "value": agent_repo_name, "type": "PLAINTEXT"},
            {"name": "GIT_BASE_URL", "value": conn.base_url or "", "type": "PLAINTEXT"},
            {"name": "GIT_SECRET_ARN", "value": scratch_arn, "type": "PLAINTEXT"},
            {"name": "TARGET_ROLE_ARN", "value": cfg.deploy_role_arn, "type": "PLAINTEXT"},
            {"name": "TARGET_ACCOUNT_ID", "value": cfg.account_id, "type": "PLAINTEXT"},
            {"name": "AWS_TARGET_REGION", "value": cfg.region, "type": "PLAINTEXT"},
            {"name": "TENANT_ID", "value": tenant_id, "type": "PLAINTEXT"},
            {"name": "STAGE", "value": stage, "type": "PLAINTEXT"},
            {"name": "AGENT_REGISTRY_ID", "value": self._registry.registry_id, "type": "PLAINTEXT"},
        ]
        tool_auth, obo_arn = self._harness_tool_auth(agent)
        env += [
            {"name": "GATEWAY_TOOL_AUTH", "value": tool_auth, "type": "PLAINTEXT"},
            {"name": "OBO_PROVIDER_ARN", "value": obo_arn, "type": "PLAINTEXT"},
        ]
        return self._start(env, agent_id=agent_id, image_tag=image_tag, scratch_arn=scratch_arn)

    def _harness_tool_auth(self, agent) -> tuple[str, str]:
        """``(GATEWAY_TOOL_AUTH, OBO_PROVIDER_ARN)`` for a harness agent build, or ``("", "")``.

        The map is compact JSON ``{gateway_arn: "<entra_app_audience>/.default"}`` over the agent's
        own tenant's ``kind=gateway`` MCP records that carry both an ARN and an audience; an ARN
        claimed by more than one record is dropped rather than guessed, and the warning names the MCP
        ids, never the ARN. With nothing to exchange for, the OBO provider (whose get-or-create mints
        an Entra secret) is not touched. The ensure coroutine runs on a fresh loop in a one-shot worker
        thread because this method is reached both on the event loop and from a threadpool.

        Any failure (services unwired, registry, Graph, control plane) sends both empty and logs a
        warning: the build proceeds and the tools stay on the module's default auth, since a scope
        with no provider cannot be exchanged.
        """
        if self._mcp is None or self._credentials is None:
            logger.warning(
                "[runtime_build] gateway tool auth not wired; harness build for agent %s sends it empty",
                getattr(agent, "id", ""),
            )
            return "", ""
        try:
            from models.mcp_server import Kind

            claims: dict = {}
            for m in self._mcp.list(kind=Kind.GATEWAY):
                if (
                    m.kind == Kind.GATEWAY
                    and m.gateway_arn
                    and m.entra_app_audience
                    and m.tenant_id == getattr(agent, "tenant_id", None)
                ):
                    claims.setdefault(m.gateway_arn, []).append(m)
            scopes = {}
            for gw_arn, records in claims.items():
                if len(records) > 1:
                    logger.warning(
                        "[runtime_build] gateway ARN claimed by several MCP records %s; dropped",
                        sorted(r.id for r in records),
                    )
                    continue
                scopes[gw_arn] = f"{records[0].entra_app_audience}/.default"
            if not scopes:
                return "", ""
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                name = pool.submit(
                    asyncio.run, self._credentials.ensure_agent_credential_provider(agent)
                ).result()
            arn = self._credentials._control.get_oauth2_credential_provider(name=name)[
                "credentialProviderArn"
            ]
        except Exception:
            logger.warning(
                "[runtime_build] could not resolve gateway tool auth for agent %s; harness build sends it "
                "empty (tools stay on the default auth)",
                getattr(agent, "id", ""),
                exc_info=True,
            )
            return "", ""
        return json.dumps(scopes, separators=(",", ":"), sort_keys=True), arn

    def _start_mcp_build(self, *, mcp_server_id: str, image_tag: str, connection_id: str, stage: str) -> str:
        """StartBuild for an MCP-server repo, the gateway config build.

        The harness config path's twin with the agent half swapped for the MCP half: the tenant and
        the Entra wiring come from the MCP record, never the body; the repo row key goes in as
        ``REPO_SK``; no ``AGENT_ID`` or ``AGENT_REGISTRY_ID`` is sent. There is no container fold: an
        MCP server has no image, so an unresolvable or non-config chain is a ``build_kind_mismatch``
        refusal before any secret is written. The record's name is editable after create, so it is
        re-checked against the rule here; ``invalid_name`` starts nothing.
        """
        if self._has_in_flight_build(id_var="MCP_SERVER_ID", artifact_id=mcp_server_id, stage=stage):
            raise RuntimeBuildError(
                f"a build for mcp_server_id={mcp_server_id} stage={stage} is already in progress",
                kind="conflict",
            )
        conn = self._connections.get_connection(connection_id)
        mcp = self._mcp.get(mcp_server_id) if self._mcp is not None else None
        if mcp is None:
            raise RuntimeBuildError("unknown MCP server", kind="not_found")
        if not _MCP_NAME_RE.match(mcp.name or ""):
            raise RuntimeBuildError(
                "MCP server name does not match ^[a-z][a-z0-9-]{1,30}$", kind="invalid_name"
            )
        _tenant, cfg = self._resolve_stage(mcp.tenant_id, stage)

        repo, infra = self._resolve_mcp_chain(mcp_server_id, connection_id)
        if repo is None or infra is None or infra.build_kind != _BUILD_KIND_CONFIG:
            raise RuntimeBuildError(
                "config build expected but the infra chain did not resolve to one",
                kind="build_kind_mismatch",
            )
        scratch_arn = self._write_scratch_token(image_tag, connection_id)

        env = [
            {"name": "IAC_TYPE", "value": infra.iac_type, "type": "PLAINTEXT"},
            {
                "name": "DEPLOYMENT_ID",
                "value": f"{infra.iac_type.replace('_', '-')}-{image_tag}",
                "type": "PLAINTEXT",
            },
            {"name": "IMAGE_TAG", "value": image_tag, "type": "PLAINTEXT"},
            {"name": "MCP_SERVER_ID", "value": mcp_server_id, "type": "PLAINTEXT"},
            {"name": "MCP_NAME", "value": mcp.name, "type": "PLAINTEXT"},
            {"name": "REPO_SK", "value": repo.id, "type": "PLAINTEXT"},
            {"name": "ENTRA_APP_AUDIENCE", "value": mcp.entra_app_audience or "", "type": "PLAINTEXT"},
            {"name": "ENTRA_APP_ID", "value": mcp.entra_app_id or "", "type": "PLAINTEXT"},
            {"name": "GIT_INFRA_ORG", "value": conn.org, "type": "PLAINTEXT"},
            {"name": "GIT_INFRA_REPO", "value": infra.repo_name, "type": "PLAINTEXT"},
            {"name": "GIT_AGENT_REPO", "value": repo.name, "type": "PLAINTEXT"},
            {"name": "GIT_BASE_URL", "value": conn.base_url or "", "type": "PLAINTEXT"},
            {"name": "GIT_SECRET_ARN", "value": scratch_arn, "type": "PLAINTEXT"},
            {"name": "TARGET_ROLE_ARN", "value": cfg.deploy_role_arn, "type": "PLAINTEXT"},
            {"name": "TARGET_ACCOUNT_ID", "value": cfg.account_id, "type": "PLAINTEXT"},
            {"name": "AWS_TARGET_REGION", "value": cfg.region, "type": "PLAINTEXT"},
            {"name": "TENANT_ID", "value": mcp.tenant_id, "type": "PLAINTEXT"},
            {"name": "STAGE", "value": stage, "type": "PLAINTEXT"},
        ]
        return self._start(env, agent_id=mcp_server_id, image_tag=image_tag, scratch_arn=scratch_arn)

    def _resolve_mcp_chain(self, mcp_server_id: str, connection_id: str):
        """``(repo, infra_template)`` for an MCP server, :meth:`_resolve_chain`'s twin keyed by the
        MCP id. Folds every fault to ``(None, None)``, which the caller refuses."""
        if not (self._projects and self._templates and self._infra_templates):
            return None, None
        try:
            repo = self._projects.find_repository_by_mcp_server_id(mcp_server_id)
            if repo is None:
                return None, None
            template = self._templates.get(connection_id, repo.template_name)
            infra_id = (getattr(template, "infra_template_id", "") or "") if template else ""
            if not infra_id:
                return repo, None
            return repo, self._infra_templates.get(connection_id, infra_id)
        except Exception:
            logger.exception(
                "[runtime_build] could not resolve the infra chain for mcp_server_id=%s",
                mcp_server_id,
            )
            return None, None

    def _has_in_flight_build(self, *, id_var: str, artifact_id: str, stage: str) -> bool:
        """True if a build whose env ``id_var`` equals ``artifact_id`` and whose ``STAGE`` matches
        is still ``IN_PROGRESS``. One shared CodeBuild project runs every build, so this walks the
        project's running builds (``list_builds_for_project`` then ``batch_get_builds``, the two-call
        CodeBuild shape) and reads each one's own environment overrides. Best-effort and fails open:
        any CodeBuild API fault logs and returns ``False`` rather than blocking every deploy."""
        try:
            ids = self._codebuild.list_builds_for_project(projectName=self._project_name).get("ids") or []
            if not ids:
                return False
            builds = self._codebuild.batch_get_builds(ids=ids).get("builds") or []
        except Exception:
            logger.exception(
                "[runtime_build] could not list in-flight builds for project=%s (failing open)",
                self._project_name,
            )
            return False

        for build in builds:
            if build.get("buildStatus") != "IN_PROGRESS":
                continue
            overrides = (build.get("environment") or {}).get("environmentVariables") or []
            env = {e.get("name"): e.get("value") for e in overrides}
            if env.get(id_var) == artifact_id and env.get("STAGE") == stage:
                return True
        return False

    def _start(self, env, *, agent_id: str, image_tag: str, scratch_arn: str) -> str:
        """StartBuild with the assembled env overrides, shared by every build kind so a failure is
        the same ``start_failed``. The scratch secret was written before this call, so a StartBuild
        that faults leaves a live clone token no build will purge: this is the one path that reclaims
        it, and the cleanup must not change the error the caller sees."""
        try:
            resp = self._codebuild.start_build(
                projectName=self._project_name,
                environmentVariablesOverride=env,
            )
        except Exception as exc:
            logger.exception(
                "[runtime_build] StartBuild failed for agent_id=%s image_tag=%s", agent_id, image_tag
            )
            self._delete_secret_best_effort(scratch_arn)
            raise RuntimeBuildError("Failed to start runtime build", kind="start_failed") from exc

        return resp["build"]["id"]

    def _delete_secret_best_effort(self, secret_id: str) -> None:
        """Force-delete a scratch secret; every failure is swallowed and logged, because this runs
        inside the handler that is about to raise the real error. ``ForceDeleteWithoutRecovery`` is
        required: the default recovery window would keep the clone token readable for a month.
        Called only when StartBuild failed, never on the happy path, where CodeBuild still has to
        read the secret and the buildspec's ``post_build`` owns the purge."""
        try:
            self._sm.delete_secret(SecretId=secret_id, ForceDeleteWithoutRecovery=True)
        except self._sm.exceptions.ResourceNotFoundException:
            pass
        except Exception:
            logger.exception("[runtime_build] scratch-secret delete failed for %s", secret_id)

    def _write_scratch_token(self, image_tag: str, connection_id: str) -> str:
        """Resolve a ready bearer token and write ``{"token": <token>}`` to a per-build scratch
        secret; return its ARN. Uniform for PAT and App connections through ``get_bearer_token``; a
        pre-existing scratch name (a retry of the same image tag) is overwritten with
        ``put_secret_value``. Any failure raises ``credential_staging_failed`` before StartBuild is
        reached; the token and the exception value are never logged, only the traceback."""
        name = f"{self._scratch_prefix}{image_tag}"
        try:
            token = self._connections.get_bearer_token(connection_id)
            secret_string = json.dumps({"token": token})
            try:
                resp = self._sm.create_secret(
                    Name=name,
                    SecretString=secret_string,
                    Tags=[
                        {"Key": "managed_by", "Value": "agp"},
                        {"Key": "purpose", "Value": "runtime-build-token"},
                    ],
                )
            except self._sm.exceptions.ResourceExistsException:
                resp = self._sm.put_secret_value(SecretId=name, SecretString=secret_string)
            return resp["ARN"]
        except RuntimeBuildError:
            raise
        except Exception as exc:
            logger.exception(
                "[runtime_build] scratch-token write failed for connection_id=%s image_tag=%s",
                connection_id,
                image_tag,
            )
            raise RuntimeBuildError(
                "Failed to stage runtime build credential", kind="credential_staging_failed"
            ) from exc
