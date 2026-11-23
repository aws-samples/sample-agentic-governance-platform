"""The materialize workflow: the steps that turn a pending repository into a ready one.

Functions over a ``ProjectService`` passed as ``svc``, whose collaborators are read at call time.
``ProjectService`` delegates ``run_materialize`` and ``retry_materialize`` here; ``run_materialize`` drives
:data:`models.repository.MATERIALIZE_STEPS` by key, and the CI variables a repository receives are built and
checked here.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Literal, Optional

from models.repository import MATERIALIZE_STEPS, Repository, StepStatus
from services.github_template_service import _NAME_RE as _TEMPLATE_NAME_RE
from services.project_errors import ProjectError
from services.scaffold_files import collect_scaffold_files
from services.template_registry import template_id_for

if TYPE_CHECKING:
    from services.project_service import ProjectService

logger = logging.getLogger(__name__)

_TEMPLATE_PUSH_MESSAGE = "chore: initialize from template [skip ci]"


_CI_STAGE = "dev"


_AUTO_INIT_BRANCH = "main"


_STAGE_SCOPED_VAR_KEYS = ("ECR_REPOSITORY", "AWS_REGION", "AWS_ECR_PUSH_ROLE_ARN")


_CONFIG_REGION_VAR = "AGP_REGION"


_CONFIG_DEPLOY_ROLE_VAR = "AGP_DEPLOY_ROLE_ARN"


_CONFIG_SKIP_LABEL = "config build: ECR vars skipped"


_MCP_LANGFUSE_SKIP_LABEL = "Langfuse skipped: not used by MCP servers"


_BuildKind = Literal["container", "config"]


_CONTAINER_BUILD_KIND: _BuildKind = "container"


_CONFIG_BUILD_KIND: _BuildKind = "config"


async def _await(awaitable: Awaitable[Any]) -> Any:
    return await awaitable


def _get_agent_credential_service():
    """The AgentCredentialService singleton the grant path uses. Deferred import (the route
    module imports services at load, never the reverse); module-level so tests patch it."""
    from api.routes.mcp_server_grants import get_agent_credential_service

    return get_agent_credential_service()


def _step_error_hint(err: Exception) -> str:
    """The SAFE short hint persisted on a failed materialize step, and shown to the operator.

    ``type(err).__name__`` alone was the rule, and for arbitrary exceptions it still is: a
    provider/Graph/boto exception message can carry a token, a URL or a response body, and this
    string lands on a read model the console renders. Fail closed on anything unrecognised.

    :class:`ProjectError` is the ONE exception this module raises deliberately, and every one of its
    messages is a CURATED LITERAL written here (grep the constructor calls — no interpolated token,
    no provider body, no ``str(exc)`` of a foreign error). Those messages are the only place the
    operator can learn WHAT to fix — "tenant 'x' carries no 'dev' stage (stages present: [...])"
    tells them to change tenant config, whereas ``"ProjectError"`` tells them nothing and invites a
    pointless retry.

    So the type name is kept as the fail-closed default, and the message is surfaced only for the
    type whose messages this module authors.
    """
    if isinstance(err, ProjectError):
        return err.message
    return type(err).__name__


_BEST_EFFORT_STEPS = frozenset({"provision_langfuse"})


def _build_stage_ci_vars(tenant, tenant_id: str) -> Dict[str, str]:
    """The three stage-sourced CI vars for :data:`_CI_STAGE`, or raise naming what the tenant has.

    Looked up BY NAME. A tenant carrying stages but no ``dev`` cannot satisfy the template's
    ``environment: dev`` jobs, so this refuses instead of substituting another stage: substituting
    would hand a workflow labelled `dev` some other stage's ECR repository and push role, and the
    only evidence would be a deploy landing in the wrong account. The message names the stages the
    tenant ACTUALLY carries, because the fix is a tenant config change and the operator needs to
    see what they have.

    Raising here fails the ``set_repo_vars`` STEP (``run_materialize`` records it and stops), which
    is the correct blast radius: the repo and its template exist, and a retry after fixing the
    tenant resumes from this step."""
    stages = getattr(tenant, "stages", None) or {}
    cfg = stages.get(_CI_STAGE)
    if cfg is None:
        raise ProjectError(
            f"tenant {tenant_id!r} carries no {_CI_STAGE!r} stage, so the repository's CI "
            f"variables cannot be sourced (stages present: {sorted(stages)})",
            kind="materialize_error",
        )
    stage_vars = {
        "ECR_REPOSITORY": cfg.ecr_repo_uri,
        "AWS_REGION": cfg.region,
        "AWS_ECR_PUSH_ROLE_ARN": cfg.push_role_arn,
    }
    return stage_vars


def _build_config_ci_vars(tenant, tenant_id: str) -> Dict[str, str]:
    """The two stage-sourced CI vars a CONFIG build gets.

    The same ``dev`` stage, looked up the same way and refused the same way as
    :func:`_build_stage_ci_vars`. The keys are AGP-prefixed rather than ``AWS_REGION`` so a config
    repository carrying ``AWS_REGION`` without ``ECR_REPOSITORY`` is never read as a half-configured
    container build. An empty ``deploy_role_arn`` is legal: the harness module reads
    empty as deploy-in-place (single-account), and the write client drops empty values, so a
    single-account tenant simply has no such repo var."""
    stages = getattr(tenant, "stages", None) or {}
    cfg = stages.get(_CI_STAGE)
    if cfg is None:
        raise ProjectError(
            f"tenant {tenant_id!r} carries no {_CI_STAGE!r} stage, so the repository's CI "
            f"variables cannot be sourced (stages present: {sorted(stages)})",
            kind="materialize_error",
        )
    return {
        _CONFIG_REGION_VAR: getattr(cfg, "region", "") or "",
        _CONFIG_DEPLOY_ROLE_VAR: getattr(cfg, "deploy_role_arn", "") or "",
    }


_SETTINGS_CI_VAR_SOURCES = {
    "ECR_REPOSITORY": "backend setting PROJECT_ECR_REPOSITORY is empty",
    "AWS_REGION": "backend setting AWS_REGION is empty",
    "AWS_ECR_PUSH_ROLE_ARN": (
        "connection has no ecr_push_role_arn and backend setting PROJECT_ECR_PUSH_ROLE_ARN is empty"
    ),
}


_AGP_API_URL_SOURCE = "backend setting AGP_API_URL is empty (root tfvar agp_api_url, then terraform apply)"


def _tenant_ci_var_sources(tenant_id: str) -> Dict[str, str]:
    stage = f"tenant {tenant_id!r} {_CI_STAGE} stage field"
    reseed = "(re-run scripts/seed_default_tenant.py with the wiring flags)"
    return {
        "ECR_REPOSITORY": f"{stage} ecr_repo_uri is empty {reseed}",
        "AWS_REGION": f"{stage} region is empty {reseed}",
        "AWS_ECR_PUSH_ROLE_ARN": (
            f"connection has no ecr_push_role_arn and the {stage} push_role_arn is empty"
        ),
    }


def _require_ci_vars(
    effective_vars: Dict[str, str], is_mcp: bool, build_kind: str, tenant_id: str, *, tenants_configured: bool
) -> None:
    required = ["MCP_SERVER_ID" if is_mcp else "AGENT_ID", "CONNECTION_ID", "AGP_API_URL"]
    if build_kind != _CONFIG_BUILD_KIND:
        required.extend(_STAGE_SCOPED_VAR_KEYS)
    missing = [key for key in required if not effective_vars.get(key)]
    if not missing:
        return
    sources = {
        **(_tenant_ci_var_sources(tenant_id) if tenants_configured else _SETTINGS_CI_VAR_SOURCES),
        "AGP_API_URL": _AGP_API_URL_SOURCE,
    }
    raise ProjectError(
        "repository CI variables incomplete — "
        + "; ".join(f"{key}: {sources.get(key, 'no value resolved')}" for key in missing),
        kind="materialize_error",
    )


def run_materialize(svc: ProjectService, repo_id: str) -> None:
    """Run the background materialize steps for a persisted-pending repo.

    The BackgroundTask target scheduled by ``POST /projects/{id}/repos`` AFTER the 202
    response. Runs each step in :data:`MATERIALIZE_STEPS` order, wrapping it
    ``_save_repo_step(RUNNING)`` → do the work → ``_save_repo_step(DONE)``. A step
    already ``done`` is SKIPPED (resume support for retry). A step that raises is marked
    ``failed`` with a SAFE short hint (:func:`_step_error_hint`); for a step in
    :data:`_BEST_EFFORT_STEPS` (``provision_langfuse``) the run CONTINUES and the record is
    left alone, for every other step the record flips to ``cicd_status="failed"`` /
    ``status="failed"`` and the run STOPS. A step is best-effort only when it provisions a
    resource the repository does not depend on; a step is never made best-effort because it
    is flaky. The exception is always swallowed: this runs after
    the response, so it must NEVER raise. Terminal success: the ``finalize`` step flips
    ``cicd_status`` → ``"ready"``.

    The pending inputs come from ``_pending_materialize`` (stashed by ``add_repo``); an
    absent entry (e.g. a stale/duplicate schedule) is a no-op."""
    pending = svc._pending_materialize.get(repo_id)
    if pending is None:
        logger.warning("[project] run_materialize called for unknown/stale repo %s", repo_id)
        return

    repo = svc._get_repo(repo_id)
    if repo is None:
        return
    done_keys = {s.key for s in repo.steps if s.status == StepStatus.DONE}

    runners: Optional[Dict[str, Callable[[], None]]] = None
    for step in MATERIALIZE_STEPS:
        key = step["key"]
        if key in done_keys:
            continue
        _save_repo_step(svc, repo_id, key, StepStatus.RUNNING)
        try:
            if runners is None:
                runners = _materialize_runners(svc, repo_id, pending)
            runner = runners.get(key)
            if runner is None:
                raise KeyError(f"no materialize runner for step '{key}'")
            runner()
        except Exception as err:
            logger.exception("[project] materialize step '%s' failed for %s", key, repo_id)
            _save_repo_step(svc, repo_id, key, StepStatus.FAILED, error=_step_error_hint(err))
            if key in _BEST_EFFORT_STEPS:
                continue
            _mark_repo_failed(svc, repo_id)
            svc._pending_materialize.pop(repo_id, None)
            return
        _save_repo_step(svc, repo_id, key, StepStatus.DONE)

    svc._pending_materialize.pop(repo_id, None)


def retry_materialize(svc: ProjectService, repo_id: str) -> Repository:
    """Reset the failed materialize and RE-DERIVE the inputs, so a scheduled
    ``run_materialize`` resumes from the failed step.

    Every non-``done`` step goes back to ``pending``, plus ``finalize`` even when ``done``: it
    is the only writer of ``"ready"`` and this function flips the record to ``"provisioning"``,
    so a best-effort step failing after ``finalize`` completed would otherwise strand the repo.
    The inputs are rebuilt from durable state (the stash is popped on a terminal run): the
    registered agent or MCP record, the project's connection and tenant. A repo whose steps
    are ALL ``done`` raises ``nothing_to_retry`` and changes nothing."""
    repo = svc._get_repo(repo_id)
    if repo is None:
        raise ProjectError("Repository not found", kind="repo_not_found")

    if all(step.status == StepStatus.DONE for step in repo.steps):
        raise ProjectError("Nothing to retry", kind="nothing_to_retry")

    agent = mcp = None
    if repo.artifact_kind == "mcp_server":
        if svc._mcp_registry is not None:
            mcp = svc._mcp_registry.get(repo.mcp_server_id)
        if mcp is None:
            raise ProjectError("Repository not found", kind="repo_not_found")
    else:
        agent = svc._registry.get(repo.agent_id)
        if agent is None:
            raise ProjectError("Repository not found", kind="repo_not_found")
    project = svc._get_project(repo.project_id)
    if project is None:
        raise ProjectError("Repository not found", kind="repo_not_found")
    agent_config = (
        {
            "agent_name": agent.name,
            "framework": agent.framework,
            "model_id": agent.model_id,
        }
        if agent is not None
        else {}
    )
    svc._pending_materialize[repo_id] = {
        "agent": agent,
        "mcp": mcp,
        "name": repo.name,
        "connection_id": project.connection_id,
        "template_name": repo.template_name,
        "agent_config": agent_config,
        "repo_overrides": None,
        "tenant_id": project.tenant_id,
        "trunk_branch": project.trunk_branch,
    }

    ts = svc._now().isoformat()
    for step in repo.steps:
        if step.status != StepStatus.DONE or step.key == "finalize":
            step.status = StepStatus.PENDING
            step.error = None
            step.started_at = None
            step.completed_at = None
    repo.cicd_status = "provisioning"
    repo.status = "provisioning"
    repo.updated_at = ts
    svc._save_repo(repo, include_cicd_status=True)
    return svc._with_build_kinds([repo])[0]


def _materialize_runners(svc: ProjectService, repo_id: str, pending: dict) -> Dict[str, Callable[[], None]]:
    """Build the per-step work closures for :func:`run_materialize`.

    One closure per :data:`MATERIALIZE_STEPS` key: ``mint_identity`` → ``create_repo`` →
    ``push_template`` → ``set_repo_vars`` → ``provision_langfuse`` → ``finalize``. The
    connection token is read FRESH here (never stashed) and forwarded straight to the write
    client. Materialize makes two provider writes and only one touches the tree: create the
    repo, then push the whole template in ONE commit whose message carries ``[skip ci]``, so
    there is no second writer to race. An MCP repo's stash carries ``"mcp"`` (``"agent"`` is
    None): ``mint_identity`` mints the MCP's Entra app, ``set_repo_vars`` stamps
    ``MCP_SERVER_ID`` instead of ``AGENT_ID``, and ``provision_langfuse`` is skipped with the
    reason on its label."""
    agent = pending["agent"]
    mcp = pending.get("mcp")
    name = pending["name"]
    connection_id = pending["connection_id"]
    template_name = pending["template_name"]
    trunk = pending["trunk_branch"]

    connection = svc._conn.get_connection(connection_id)
    token = svc._conn.get_bearer_token(connection_id)
    state: Dict[str, str] = {}

    def mint_identity() -> None:
        if mcp is not None:
            if svc._mcp_identity is None:
                raise ProjectError("no MCP identity service", kind="materialize_error")
            svc._mcp_identity.mint_app(mcp)
            return
        _provision_identity(svc, agent)
        _ensure_credential_provider(svc, agent)

    def create_repo() -> None:
        repo_url = svc._rollout.create_repo(
            connection.org, name, private=True, token=token, base_url=connection.base_url
        )
        state["repo_url"] = repo_url
        _persist_repo_url(svc, repo_id, repo_url)

    def push_template() -> None:
        files, label = _resolve_template_bytes(svc, connection_id, connection, token, template_name)
        _save_repo_step_label(svc, repo_id, "push_template", label)
        svc._rollout.commit_files(
            connection.org,
            name,
            files,
            branch=trunk,
            message=_TEMPLATE_PUSH_MESSAGE,
            token=token,
            base_url=connection.base_url,
        )
        _adopt_trunk(svc, connection, name, trunk, token)

    def set_repo_vars() -> None:
        _set_repo_vars(svc, repo_id, pending, connection, token)

    def provision_langfuse() -> None:
        _provision_langfuse(svc, repo_id, agent, mcp)

    def finalize() -> None:
        _finalize_repo(svc, repo_id, state.get("repo_url"))

    return {
        "mint_identity": mint_identity,
        "create_repo": create_repo,
        "push_template": push_template,
        "set_repo_vars": set_repo_vars,
        "provision_langfuse": provision_langfuse,
        "finalize": finalize,
    }


def _set_repo_vars(svc: ProjectService, repo_id: str, pending: dict, connection, token: str) -> None:
    agent = pending["agent"]
    mcp = pending.get("mcp")
    connection_id = pending["connection_id"]
    tenant_id = pending["tenant_id"]
    build_kind = _resolve_build_kind(svc, connection_id, pending["template_name"])
    effective_vars = {
        **svc._repo_vars,
        **(pending["repo_overrides"] or {}),
        **({"MCP_SERVER_ID": mcp.id} if mcp is not None else {"AGENT_ID": agent.id}),
        "CONNECTION_ID": connection_id,
    }
    if build_kind == _CONFIG_BUILD_KIND:
        for key in _STAGE_SCOPED_VAR_KEYS:
            effective_vars.pop(key, None)
        if svc._tenants is not None:
            effective_vars.update(_build_config_ci_vars(svc._tenants.get(tenant_id), tenant_id))
        _save_repo_step_label(svc, repo_id, "set_repo_vars", _CONFIG_SKIP_LABEL)
    elif svc._tenants is not None:
        effective_vars.update(_build_stage_ci_vars(svc._tenants.get(tenant_id), tenant_id))
    if getattr(connection, "ecr_push_role_arn", None):
        effective_vars["AWS_ECR_PUSH_ROLE_ARN"] = connection.ecr_push_role_arn
    _require_ci_vars(
        effective_vars,
        mcp is not None,
        build_kind,
        tenant_id,
        tenants_configured=svc._tenants is not None,
    )
    svc._rollout.set_ci_vars(
        connection.org,
        pending["name"],
        effective_vars,
        scope=None,
        token=token,
        base_url=connection.base_url,
    )


def _provision_langfuse(svc: ProjectService, repo_id: str, agent, mcp) -> None:
    if mcp is not None:
        _save_repo_step_label(svc, repo_id, "provision_langfuse", _MCP_LANGFUSE_SKIP_LABEL)
        return
    if svc._langfuse is None or not getattr(svc._langfuse, "langfuse_host", ""):
        return
    svc._langfuse.provision_agent_project(agent)


def _resolve_template_bytes(
    svc: ProjectService, connection_id: str, connection, token: str, template_name: str
) -> tuple[Dict[str, bytes], str]:
    """The bytes ``push_template`` commits, and the LABEL naming where they came from.

    Three arms. A record carrying both halves of its source pair (``source_org`` and
    ``source_repo``, judged on usability, not assignment) is dereferenced: ``read_repo``
    resolves the tip once and ``read_tree`` reads AT that sha, never at a branch name. A record
    with no usable pair, an unregistered name or no catalog at all falls back to the on-disk
    seed, and the label says so. A pair that does not dereference (the repo is gone, or exists
    with no commit) FAILS with no disk fallback: shipping starter bytes where the customer
    expected their own template is the defect this closes. A store fault raises rather than
    degrading to the seed."""
    record = _get_template_record(svc, connection_id, template_name)
    source_org = (record.source_org or "") if record is not None else ""
    source_repo = (record.source_repo or "") if record is not None else ""

    if not (source_org and source_repo):
        scaffold_dir = _resolve_scaffold_dir(svc, template_name)
        files = collect_scaffold_files(scaffold_dir)
        if not files:
            raise ProjectError(
                f"template '{template_name}' has no files on disk — nothing to push",
                kind="materialize_error",
            )
        return files, f"Fetch template (seed: {template_name})"

    view = svc._rollout.read_repo(source_org, source_repo, token=token, base_url=connection.base_url)
    if view is None or not view.head_sha:
        fault = (
            f"template repo {source_repo} missing"
            if view is None
            else f"template repo {source_repo} exists but has no commits — nothing to ship"
        )
        raise ProjectError(
            f"{fault} — re-seed or deregister on the Templates page",
            kind="materialize_error",
        )
    files = svc._rollout.read_tree(
        source_org, source_repo, ref=view.head_sha, token=token, base_url=connection.base_url
    )
    if not files:
        raise ProjectError(
            f"template repo {source_repo} has no files at {view.head_sha[:7]} — nothing to push",
            kind="materialize_error",
        )
    return files, f"Fetch template ({source_repo}@{view.head_sha[:7]})"


def _get_template_record(svc: ProjectService, connection_id: str, template_name: str):
    """The catalog row for ``template_name`` under ``connection_id``, or None.

    None means "no such row" OR "no catalog wired" — both are arm 2, and both are truthful
    absences of a pointer. A STORE FAULT PROPAGATES (the registry is strict by design): see
    :func:`_resolve_template_bytes` for why an unreadable catalog must not read as "no source".

    Scoped by connection, and keyed with the registry's OWN key derivation
    (``template_id_for``) rather than passing the name straight through — the id is derived
    from the name, and re-deriving it here by hand is how the two could drift.
    """
    if svc._templates is None:
        return None
    return svc._templates.get(connection_id, template_id_for(template_name))


def _resolve_build_kind(svc: ProjectService, connection_id: str, template_name: str) -> _BuildKind:
    """The chain's build kind: ``template.infra_template_id`` → ``InfraTemplateRecord`` →
    ``build_kind``.

    FAILS CLOSED TOWARD ``"container"``: no infra catalog wired, no template record or a blank
    chain, or an infra id with no record all read as container, which keeps every existing
    agent's image build intact; a record answers its own ``build_kind``. A STORE FAULT
    PROPAGATES rather than degrading to container (an intermittent wrong answer is worse than
    a loud failure a retry fixes); it lands on the ``set_repo_vars`` step, whose failure a
    retry resumes from.
    """
    if svc._infra is None:
        return _CONTAINER_BUILD_KIND
    record = _get_template_record(svc, connection_id, template_name)
    infra_id = getattr(record, "infra_template_id", "") if record is not None else ""
    if not infra_id:
        return _CONTAINER_BUILD_KIND
    infra = svc._infra.get(connection_id, infra_id)
    if infra is None:
        logger.info(
            "[project] template %r names infra template %r with no catalog record — treating the build as %s",
            template_name,
            infra_id,
            _CONTAINER_BUILD_KIND,
        )
        return _CONTAINER_BUILD_KIND
    return _CONFIG_BUILD_KIND if infra.build_kind == _CONFIG_BUILD_KIND else _CONTAINER_BUILD_KIND


def _save_repo_step_label(svc: ProjectService, repo_id: str, step_key: str, label: str) -> None:
    """Persist a step's LABEL, leaving its status/timestamps alone.

    Separate from :func:`_save_repo_step` because the two carry different facts: status is a
    transition the runner loop drives, while this is evidence the runner DISCOVERED (which
    bytes it resolved). Keeping them apart is what lets the label survive the later DONE write
    — ``_save_repo_step`` never touches ``label``.

    A record's stored labels are what the console renders (no client-side key→label map), so a
    historical repo keeps reading the label it was materialized with. A vanished record is a
    no-op, mirroring :func:`_save_repo_step`."""
    repo = svc._get_repo(repo_id)
    if repo is None:
        return
    for step in repo.steps:
        if step.key == step_key:
            step.label = label
            break
    repo.updated_at = svc._now().isoformat()
    svc._save_repo(repo)


def _resolve_scaffold_dir(svc: ProjectService, template_name: str) -> Path:
    """The on-disk scaffold dir for ``template_name``, validated and CONTAINED.

    ``template_name`` comes from the request body and becomes a filesystem path here, so two
    layers guard it: the name must match the catalog's own ``_NAME_RE`` (imported, so the
    catalog and the pusher cannot disagree), and containment is asserted AFTER resolution,
    which also covers a symlink inside the templates dir pointing out of it. The refusal
    echoes the rejected name (caller-supplied) and never a resolved absolute path.
    """
    if not _TEMPLATE_NAME_RE.match(template_name or ""):
        raise ProjectError(
            f"invalid template name {template_name!r} — a template name must match "
            f"{_TEMPLATE_NAME_RE.pattern}",
            kind="materialize_error",
        )
    base = svc._agent_templates_dir.resolve()
    candidate = (svc._agent_templates_dir / template_name).resolve()
    if candidate != base and not candidate.is_relative_to(base):
        logger.error(
            "[project] refusing template %r — it resolves outside the templates directory",
            template_name,
        )
        raise ProjectError(
            f"invalid template name {template_name!r} — it resolves outside the templates directory",
            kind="materialize_error",
        )
    return candidate


def _adopt_trunk(svc: ProjectService, connection, name: str, trunk: str, token: str) -> None:
    """Make ``trunk`` the repository's ONLY branch and its default.

    Called once, immediately after the template push. Two provider calls, both no-ops when the
    trunk is already the branch ``create_repo``'s ``auto_init`` produced:

      1. point ``default_branch`` at ``trunk`` — otherwise PRs open against the seeded branch
         and the template sits somewhere the provider does not serve as HEAD;
      2. delete the seeded branch — otherwise it lingers with the auto-init README, Actions
         watches it, and ``build.yml``'s ``branches:`` filter can fire on a branch that never
         held the agent's code.

    The ORDER is required, not stylistic: GitHub refuses to delete the default branch, so a
    delete-first implementation fails on every non-default trunk.

    The seeded branch's name is the ORG's default-branch setting, which AGP cannot read from
    the connection record — so it is inferred as :data:`_AUTO_INIT_BRANCH`, GitHub's own
    default. A DIFFERENT org setting leaves one stray ref: undesirable, but strictly better
    than the pre-fix state, and it cannot leave the trunk non-default (step 1 is
    unconditional). Deliberately NOT probed with an extra round trip.
    """
    if trunk == _AUTO_INIT_BRANCH:
        return
    svc._rollout.set_default_branch(connection.org, name, trunk, token=token, base_url=connection.base_url)
    svc._rollout.delete_branch(
        connection.org, name, _AUTO_INIT_BRANCH, token=token, base_url=connection.base_url
    )


def _provision_identity(svc: ProjectService, agent) -> None:
    """Drive ``identity.provision_identity(agent)`` (async in production; a plain Mock in
    tests). Any awaitable (a coroutine, a Future, an object with ``__await__``) is run to
    completion on a fresh loop, so the sync orchestration needs no running event loop; a plain
    return value is left as is."""
    result = svc._identity.provision_identity(agent)
    if inspect.isawaitable(result):
        asyncio.run(_await(result))


def _ensure_credential_provider(svc: ProjectService, agent) -> None:
    """BEST EFFORT: create the agent's OAuth credential provider right after its
    Entra app exists, so a declared MCP server is reachable the moment it is granted. It is the
    agent's OWN credential and grants nothing (consent is the gate). The SAME get-or-create the
    grant path calls, so a failure here is logged and swallowed — the grant creates it later."""
    if not getattr(agent, "entra_app_id", None):
        return
    try:
        name = asyncio.run(_get_agent_credential_service().ensure_agent_credential_provider(agent))
        if getattr(agent, "oauth2_credential_provider_name", None) != name:
            agent.oauth2_credential_provider_name = name
            svc._registry.persist_identity(agent)
    except Exception:
        logger.warning(
            "[project] credential provider creation failed for agent %s — the grant will create it",
            getattr(agent, "id", "?"),
            exc_info=True,
        )


def _save_repo_step(
    svc: ProjectService, repo_id: str, step_key: str, status: StepStatus, error: Optional[str] = None
) -> None:
    """Persist ONE StepState transition on a repo's timeline.

    The ``error`` is a SHORT SAFE hint only (``type(err).__name__`` / a curated string) —
    NEVER a token or GitHub/Graph body. A vanished record is a no-op."""
    repo = svc._get_repo(repo_id)
    if repo is None:
        return
    ts = svc._now().isoformat()
    for step in repo.steps:
        if step.key == step_key:
            step.status = status
            if status == StepStatus.RUNNING:
                step.started_at = ts
                step.error = None
            elif status == StepStatus.DONE:
                step.completed_at = ts
                step.error = None
            elif status == StepStatus.FAILED:
                step.completed_at = ts
                step.error = error
            break
    repo.updated_at = ts
    svc._save_repo(repo)


def _mark_repo_failed(svc: ProjectService, repo_id: str) -> None:
    """Flip the record's ``cicd_status``/``status`` to ``"failed"``. Called
    after a step is marked failed so the badge + the failed-record query reflect it."""
    repo = svc._get_repo(repo_id)
    if repo is None:
        return
    repo.cicd_status = "failed"
    repo.status = "failed"
    repo.updated_at = svc._now().isoformat()
    svc._save_repo(repo, include_cicd_status=True)


def _persist_repo_url(svc: ProjectService, repo_id: str, repo_url: Optional[str]) -> None:
    """Persist the generated ``repo_url`` onto the record the moment ``generate_repo``
    completes, so a resumed run that skips the done ``generate_repo``
    step can still read the url off the record in ``_finalize_repo``. A vanished record
    is a no-op."""
    repo = svc._get_repo(repo_id)
    if repo is None:
        return
    repo.repo_url = repo_url
    repo.updated_at = svc._now().isoformat()
    svc._save_repo(repo)


def _finalize_repo(svc: ProjectService, repo_id: str, repo_url: Optional[str]) -> None:
    """Terminal success: stamp the generated ``repo_url`` and flip BOTH ``cicd_status`` and
    ``status`` to ``"ready"``, closing the ``provisioning`` → ``ready`` | ``failed`` cycle.

    On a RESUMED run where ``generate_repo`` was already done, ``repo_url`` arrives as None and
    the already-persisted url is kept. ``status`` rides the ``include_cicd_status`` save, which
    still skips the CodeBuild-owned ``last_dev_image_tag``."""
    repo = svc._get_repo(repo_id)
    if repo is None:
        return
    if repo_url is not None:
        repo.repo_url = repo_url
    repo.cicd_status = "ready"
    repo.status = "ready"
    repo.updated_at = svc._now().isoformat()
    svc._save_repo(repo, include_cicd_status=True)
