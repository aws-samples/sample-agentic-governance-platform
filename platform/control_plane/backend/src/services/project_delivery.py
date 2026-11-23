"""The delivery workflow: promotion to prod, rollback to a succeeded tag, and the dev build of a config repo.

Functions over a ``ProjectService`` passed as ``svc``, whose collaborators are read at call time.
``ProjectService`` delegates ``promote_repo``, ``rollback_repo``, ``build_repo`` and ``record_prod_candidate``
here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional

from botocore.exceptions import BotoCoreError, ClientError

from models.deployment import DeploymentOutcome
from models.eval import PromoteOverride
from models.project import Project
from models.repository import Repository
from services.connection_service import ConnectionError
from services.github_repo_errors import GitHubRepoError
from services.infra_template_registry import InfraTemplateRegistryError
from services.project_errors import ProjectError
from services.project_store import _PROD_CANDIDATE_FIELDS
from services.runtime_build_service import RuntimeBuildError, RuntimeBuildService
from services.template_registry import TemplateRegistryError

if TYPE_CHECKING:
    from services.project_materialize import _BuildKind
    from services.project_service import ProjectService

logger = logging.getLogger(__name__)

_MCP_NO_PROMOTION = "MCP servers deploy a single gateway; promotion is not available yet"


_PROMOTE_IN_FLIGHT_MINUTES = 60


def _eval_run_passed(run) -> bool:
    """A run satisfies the promote gate only when it FINISHED as ``passed`` with a passing gate;
    an error/failed/running run never does, whatever its ``gate`` says."""
    return bool(
        run is not None
        and run.status == "passed"
        and run.finished_at is not None
        and run.gate is not None
        and run.gate.passed
    )


def promote_repo(
    svc: ProjectService,
    project_id: str,
    repo_id: str,
    *,
    promoted_by: str,
    override_reason: Optional[str] = None,
    promoted_by_name: Optional[str] = None,
) -> Repository:
    """Promote the repo's PROD CANDIDATE to prod.

    The image tag is resolved SERVER-SIDE from ``repo.prod_candidate_image_tag``, the artifact
    that merged to ``main``; no caller supplies a tag. The deploy is
    :meth:`RuntimeBuildService.start_runtime_build` with ``stage="prod"`` and the approved
    digest read off the record, never re-derived from the tag. ``promoted_by`` is the
    validated principal, never a body value: the Entra oid, the stable identity a role row is
    keyed by, with the email as the fallback only under dev auth.

    Every input is resolved before anything is mutated. A build failure still attributes the
    attempt and persists ``cicd_status="failed"`` before raising, with ``from None`` keeping
    the build service's message out of the chain; a CodeBuild in-flight conflict is its own
    ``conflict`` kind and leaves the record alone. The candidate is consumed by a
    COMPARE-AND-CLEAR after the build started: the record is re-read and the candidate block
    cleared only while the stored tag, sha and digest are still the ones promoted, so a merge
    landing during the deploy is not erased. The in-flight guard is bounded by
    ``_PROMOTE_IN_FLIGHT_MINUTES`` (see :func:`_promotion_in_flight`).

    Raises ``ProjectError`` with kind ``repo_not_found``, ``mcp_not_promotable``,
    ``no_prod_candidate``, ``no_dev_build`` (the candidate's dev deploy failed), ``eval_gate``,
    ``promote_in_flight``, ``conflict`` or ``promote_failed``."""
    repo = _resolve_delivery_repo(svc, project_id, repo_id)
    if repo.artifact_kind == "mcp_server":
        raise ProjectError(_MCP_NO_PROMOTION, kind="mcp_not_promotable")

    tag = repo.prod_candidate_image_tag
    if not tag:
        raise ProjectError("no prod candidate to promote", kind="no_prod_candidate")

    if svc._latest_dev_outcome_for_tag(repo.id, tag) is DeploymentOutcome.FAILED:
        raise ProjectError("the prod candidate has not deployed to dev", kind="no_dev_build")
    candidate_digest = repo.prod_candidate_digest
    candidate_sha = repo.prod_candidate_sha

    override = _eval_override(
        svc,
        repo,
        digest=candidate_digest,
        tag=tag,
        promoted_by=promoted_by,
        override_reason=override_reason,
        promoted_by_name=promoted_by_name,
    )

    if _promotion_in_flight(svc, repo):
        raise ProjectError("a promotion is already in flight", kind="promote_in_flight")

    project, builds = _resolve_delivery_context(
        svc, project_id, repo_id, verb="promote", noun="promotion", kind="promote_failed"
    )

    build_id = _start_delivery(
        svc,
        builds,
        repo,
        noun="promotion",
        stage="prod",
        image_tag=tag,
        actor=promoted_by,
        stamp=True,
        failure=ProjectError("promotion failed", kind="promote_failed"),
        agent_id=repo.agent_id,
        connection_id=project.connection_id,
        image_digest=candidate_digest or "",
    )

    if override is not None:
        _record_override_best_effort(svc, override, repo)

    _stamp_promotion(
        svc,
        repo,
        promoted_by=promoted_by,
        tag=tag,
        build_id=build_id,
        serving=svc._has_succeeded(repo_id, "prod", tag),
        digest=candidate_digest,
    )
    repo.cicd_status = "promoting"
    _consume_candidate(svc, repo, svc._get_repo(repo_id), tag, candidate_digest)
    svc._save_repo(repo, include_cicd_status=True, include_prod_candidate=True)
    _append_delivery(
        svc,
        repo,
        stage="prod",
        image_tag=tag,
        build_id=build_id,
        actor=promoted_by,
        source_sha=candidate_sha,
    )
    return svc._with_build_kinds([repo])[0]


def _eval_override(
    svc: ProjectService,
    repo: Repository,
    *,
    digest: Optional[str],
    tag: str,
    promoted_by: str,
    override_reason: Optional[str],
    promoted_by_name: Optional[str],
) -> Optional[PromoteOverride]:
    run = svc._evals.latest_run_for_build(repo.id, digest=digest, tag=tag)
    if _eval_run_passed(run):
        return None
    reason = (override_reason or "").strip()
    if not reason or len(reason) > 500 or not promoted_by:
        raise ProjectError(
            "Prod promotion needs a passing evaluation for this build (or an override with a reason).",
            kind="eval_gate",
        )
    return PromoteOverride(
        repo_id=repo.id,
        digest=digest,
        by=promoted_by,
        reason=reason,
        at=svc._now(),
        by_name=(promoted_by_name.strip() if isinstance(promoted_by_name, str) else "")[:200] or None,
        build_tag=tag,
    )


def _record_override_best_effort(svc: ProjectService, override: PromoteOverride, repo: Repository) -> None:
    try:
        svc._evals.record_override(override)
    except Exception:
        logger.exception(
            "[project] could not record the eval-gate override for repo %s"
            " (digest=%s build_tag=%s by=%s by_name=%s)",
            repo.id,
            override.digest,
            override.build_tag,
            override.by,
            override.by_name,
        )


def _consume_candidate(
    svc: ProjectService, repo: Repository, fresh: Optional[Repository], tag: str, digest: Optional[str]
) -> None:
    superseded = fresh is not None and (
        fresh.prod_candidate_image_tag != tag
        or fresh.prod_candidate_digest != digest
        or fresh.prod_candidate_sha != repo.prod_candidate_sha
    )
    if superseded:
        for field in _PROD_CANDIDATE_FIELDS:
            setattr(repo, field, getattr(fresh, field))
    else:
        _clear_prod_candidate(repo)


def _promotion_in_flight(svc: ProjectService, repo: Repository) -> bool:
    """Is a promotion GENUINELY still running for ``repo``?

    ``cicd_status == "promoting"`` alone is not enough: nothing guarantees the status is
    ever cleared (the buildspec's write is best-effort), so treating it as a permanent
    block would strand the repo. The status counts as in-flight only for
    ``_PROMOTE_IN_FLIGHT_MINUTES`` after its start clock — ``last_promoted_at`` (prod
    promote / prod rollback) or ``last_build_started_at`` (Build now / non-prod rollback),
    either one — the CodeBuild project's own build timeout, past which the build is
    necessarily over. An unparseable, absent or future clock is treated as NOT in flight: the
    guard fails OPEN, because a double promote (a spurious ``failed``, same image tag either
    way) costs far less than a repo that can never be promoted again."""
    if repo.cicd_status != "promoting":
        return False
    for field in ("last_promoted_at", "last_build_started_at"):
        started_at = getattr(repo, field)
        if not started_at:
            continue
        try:
            started = datetime.fromisoformat(started_at)
        except ValueError:
            logger.warning("[project] repo %s has an unparseable %s; not blocking promote", repo.id, field)
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        delta = svc._now() - started
        if timedelta(0) <= delta < timedelta(minutes=_PROMOTE_IN_FLIGHT_MINUTES):
            return True
    return False


def _stamp_promotion(
    svc: ProjectService,
    repo: Repository,
    *,
    promoted_by: str,
    tag: str,
    build_id: Optional[str],
    serving: bool = True,
    digest: Optional[str] = None,
) -> None:
    """Write the promotion-audit fields onto ``repo``. Applied on BOTH the success and the
    failure path so an attempted promotion is always attributed.

    Three of the four are ALWAYS written, because all three are true the instant an OWNER's
    authorization is accepted: WHO authorized it, WHEN, and WHICH build carries it. In
    particular ``last_promoted_at`` is :func:`_promotion_in_flight`'s own clock — deferring it
    to the build's terminal path would disarm the concurrency guard for the whole build
    duration and let a second delivery race the same Terraform state key.

    ``serving=False`` skips ONLY ``last_promoted_image_tag``. That field means "the tag prod
    SERVES", not "the tag we last tried": :func:`promote_repo` stamps the moment
    ``start_runtime_build`` returns, which proves only that CodeBuild ACCEPTED the build — the
    ``terraform apply`` can fail minutes later, and it did live, leaving the record claiming
    prod served an image it had never run while the Deployments tab correctly showed nothing
    serving. What was ATTEMPTED is not lost: it is the delivery row appended by the same
    caller. Callers that pass ``serving=True`` are asserting the tag is one prod has
    demonstrably run — a prod ROLLBACK (whose target :meth:`ProjectService._has_succeeded` already proved) is
    the case that must keep repointing this cache, since prod genuinely serves the older image
    and leaving the cache on the newer tag would make every list row lie."""
    ts = svc._now().isoformat()
    repo.last_promoted_by = promoted_by
    repo.last_promoted_at = ts
    if serving:
        repo.last_promoted_image_tag = tag
        if digest:
            repo.last_promoted_digest = digest
    repo.last_promotion_build_id = build_id
    repo.updated_at = ts


def record_prod_candidate(
    svc: ProjectService,
    agent_id: str,
    *,
    image_tag: str,
    sha: str,
    actor: str,
    image_digest: str = "",
) -> Repository:
    """Register/overwrite the repo's single prod candidate.

    Called by ``POST /builds/prod-candidate`` after a merge to ``main``; every argument comes
    from the validated GitHub-OIDC token, never from the request body. Resolved by
    ``agent_id`` because the OIDC path has no project context; an unknown agent raises
    ``agent_not_found`` and writes nothing. ``image_digest`` is the approvable value a later
    promote deploys; ``""`` registers a tag-only candidate, stored as ``None`` because the
    promote path and the eval lookup test the digest for truthiness. All six fields are
    overwritten as one unit and ``prod_candidate_status`` becomes ``"pending"``: ``main``'s
    HEAD is the only candidate, so a newer merge replaces the older one.

    The save is TARGETED (:meth:`ProjectService._save_prod_candidate`): this function's read is a
    partition scan and therefore stale, and a whole-record save would revert a promotion stamped in
    the meantime. ``cicd_status`` is left alone."""
    repo = svc.find_repository_by_agent_id(agent_id)
    if repo is None:
        raise ProjectError("Agent not found", kind="agent_not_found")
    ts = svc._now().isoformat()
    repo.prod_candidate_image_tag = image_tag
    repo.prod_candidate_digest = image_digest or None
    repo.prod_candidate_sha = sha
    repo.prod_candidate_actor = actor
    repo.prod_candidate_at = ts
    repo.prod_candidate_status = "pending"
    repo.updated_at = ts
    svc._save_prod_candidate(repo)
    logger.info(
        "[project] prod candidate registered for repo %s (agent %s, tag %s)",
        repo.id,
        agent_id,
        image_tag,
    )
    return repo


def _clear_prod_candidate(repo: Repository) -> None:
    """Consume the prod candidate on ``repo``: every field back to ``None``.

    Cleared as ONE unit, because a half-cleared candidate is a state nothing can act on:
    the promote guard reads only the tag, so a leftover ``pending`` status or actor would
    show a candidate in the UI that the route refuses. Applied ONLY on a successful promote
    (the approval consumed it); the caller must pair it with a save that opts in via
    ``include_prod_candidate``, or the cleared values never reach the store."""
    for field in _PROD_CANDIDATE_FIELDS:
        setattr(repo, field, None)


def _append_delivery(
    svc: ProjectService,
    repo: Repository,
    *,
    stage: str,
    image_tag: str,
    build_id: Optional[str],
    actor: str,
    outcome: DeploymentOutcome = DeploymentOutcome.STARTED,
    source_sha: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Append ONE ``Deployment`` row for an AGP-initiated delivery.

    BEST-EFFORT by design: the deploy has already been decided (started, or definitively
    refused by the build service) by the time this runs, so a store fault here must not
    convert a 202 into a 500 or replace the promotion's own curated 502 with an unrelated
    one. A missing history row is a gap in a list; a raised error here would be a lie about
    what happened to production.

    ``actor_kind`` is pinned to ``"entra"`` because every caller of this helper is an
    AGP-principal action (promote / rollback) attributed to an Entra oid. The OIDC build path
    appends its own row with ``actor_kind="github"``: a GitHub login and an Entra oid are two
    different currencies and this helper must never be reused to launder one as the other.

    A terminal ``outcome`` is appended already CLOSED (``completed_at``). The partition is
    append-only: nothing ever updates a row, so a terminal state is its own complete row."""
    ts = svc._now().isoformat()
    try:
        svc.append_deployment(
            repo_id=repo.id,
            agent_id=repo.agent_id,
            mcp_server_id=repo.mcp_server_id,
            stage=stage,
            image_tag=image_tag,
            source_sha=source_sha,
            build_id=build_id,
            outcome=outcome,
            actor=actor,
            actor_kind="entra",
            completed_at=ts if outcome is not DeploymentOutcome.STARTED else None,
            error=error,
        )
    except (ClientError, BotoCoreError):
        logger.exception(
            "[project] could not append the deployment record for repo %s (stage %s, tag %s)",
            repo.id,
            stage,
            image_tag,
        )


def _resolve_delivery_repo(svc: ProjectService, project_id: str, repo_id: str) -> Repository:
    """Resolve the repo a delivery names.

    The project-ownership check is the load-bearing half, not the lookup: without it an OWNER
    of project A could promote or roll back a repo that lives under project B. Both facts
    collapse to the SAME ``not_found`` — a repo under another project must not be
    distinguishable from one that does not exist, or the 404 becomes an enumeration oracle
    for other tenants' repo ids."""
    repo = svc._get_repo(repo_id)
    if repo is None or repo.project_id != project_id:
        raise ProjectError("Repository not found", kind="repo_not_found")
    return repo


def _resolve_delivery_context(
    svc: ProjectService, project_id: str, repo_id: str, *, verb: str, noun: str, kind: str
) -> tuple[Project, RuntimeBuildService]:
    """Resolve the parent project (for its ``connection_id``) and the wired build service,
    the last resolve move of promote, rollback and build.

    Kept separate from :func:`_resolve_delivery_repo` because each caller interposes its own
    validation between the two, and that order is observable: an unpromotable repo on a
    misconfigured platform is still told the ordinary 409, not the 502. A missing build
    service is a misconfiguration refused before any state change; ``kind`` is the caller's
    failure kind (``promote_failed``, ``rollback_failed`` or ``build_failed``)."""
    project = svc._get_project(project_id)
    if project is None:
        raise ProjectError("Repository not found", kind="repo_not_found")
    if svc._builds is None:
        logger.error("[project] %s refused for repo %s: no runtime build service", verb, repo_id)
        raise ProjectError(f"{noun} failed", kind=kind)
    return project, svc._builds


def _record_failed_delivery(
    svc: ProjectService,
    repo: Repository,
    *,
    noun: str,
    stage: str,
    image_tag: str,
    actor: str,
    stamp: bool,
) -> None:
    """Record a delivery the build service refused to start, before the caller raises.

    Logs the traceback only (never the exception value), attributes the attempt through
    :func:`_stamp_promotion` when ``stamp`` (promote always; rollback only for ``prod``, so a
    non-prod rollback never repoints the prod cache), persists ``cicd_status="failed"`` for
    every stage so the in-flight guard does not refuse retries over a build that does not
    exist, tolerates a store fault on that save, and appends a terminal ``FAILED`` row so the
    history can tell "the deploy failed" from "nobody ever tried". The caller raises its own
    kind with ``from None`` at the throw."""
    logger.exception(
        "[project] %s build failed for repo %s (agent %s, tag %s, stage %s)",
        noun,
        repo.id,
        repo.agent_id,
        image_tag,
        stage,
    )
    if stamp:
        _stamp_promotion(svc, repo, promoted_by=actor, tag=image_tag, build_id=None)
    repo.cicd_status = "failed"
    try:
        svc._save_repo(repo, include_cicd_status=True)
    except (ClientError, BotoCoreError):
        logger.exception("[project] could not persist the failed %s for repo %s", noun, repo.id)
    _append_delivery(
        svc,
        repo,
        stage=stage,
        image_tag=image_tag,
        build_id=None,
        actor=actor,
        outcome=DeploymentOutcome.FAILED,
        error=f"failed to start the {noun} build",
    )


def _start_delivery(
    svc: ProjectService,
    builds: RuntimeBuildService,
    repo: Repository,
    *,
    noun: str,
    stage: str,
    image_tag: str,
    actor: str,
    stamp: bool,
    failure: ProjectError,
    unrecorded_kinds: frozenset[str] = frozenset(),
    **build_args: Any,
) -> str:
    try:
        return builds.start_runtime_build(
            image_tag=image_tag, ecr_repo=svc._ecr_repository, stage=stage, **build_args
        )
    except (RuntimeBuildError, ConnectionError, ValueError, ClientError, BotoCoreError) as exc:
        if isinstance(exc, RuntimeBuildError) and exc.kind == "conflict":
            raise ProjectError(
                "a build is already running for this agent/stage, try again shortly",
                kind="conflict",
            ) from None
        if isinstance(exc, RuntimeBuildError) and exc.kind in unrecorded_kinds:
            raise failure from None
        _record_failed_delivery(
            svc, repo, noun=noun, stage=stage, image_tag=image_tag, actor=actor, stamp=stamp
        )
        raise failure from None


def rollback_repo(
    svc: ProjectService,
    project_id: str,
    repo_id: str,
    *,
    image_tag: str,
    rolled_back_by: str,
    stage: str = "prod",
) -> Repository:
    """Redeploy a PREVIOUSLY-SUCCEEDED image tag to ``stage``.

    THE VALIDATION IS THE FEATURE: ``image_tag`` is accepted only if a ``SUCCEEDED``
    :class:`~models.deployment.Deployment` row exists for this repo, in this stage. Another
    repo's tag names a real image in the shared registry; a dev success is not a prod
    approval; ``started`` and ``failed`` are not evidence the image ever served. ``stage`` is
    free-form and defaults to ``"prod"``; a blank stage is refused because it would disable
    the stage scoping. ``rolled_back_by`` is the validated principal, never a body value.

    A prod rollback repoints the ``last_promoted_*`` cache at the rolled-back tag (prod
    genuinely serves the older image) and clears ``last_promoted_digest``, which the rows
    cannot name. A non-prod rollback leaves the prod cache alone but still arms the repo-wide
    in-flight guard. The prod candidate is never consumed. Failure discipline mirrors
    promote: inputs resolved before any mutation, a failed start attributed and recorded,
    the exception set enumerated.

    Raises ``ProjectError`` with kind ``repo_not_found``, ``mcp_not_promotable``,
    ``unknown_rollback_target``, ``promote_in_flight``, ``conflict`` or ``rollback_failed``."""
    repo = _resolve_delivery_repo(svc, project_id, repo_id)
    if repo.artifact_kind == "mcp_server":
        raise ProjectError(_MCP_NO_PROMOTION, kind="mcp_not_promotable")

    if not stage or not stage.strip():
        raise ProjectError("stage must not be blank", kind="unknown_rollback_target")
    if not image_tag or not svc._has_succeeded(repo_id, stage, image_tag):
        logger.warning(
            "[project] rollback refused for repo %s: no succeeded %s deployment for tag %s",
            repo_id,
            stage,
            image_tag,
        )
        raise ProjectError("no such succeeded deployment", kind="unknown_rollback_target")

    if _promotion_in_flight(svc, repo):
        raise ProjectError("a promotion is already in flight", kind="promote_in_flight")

    project, builds = _resolve_delivery_context(
        svc, project_id, repo_id, verb="rollback", noun="rollback", kind="rollback_failed"
    )

    build_id = _start_delivery(
        svc,
        builds,
        repo,
        noun="rollback",
        stage=stage,
        image_tag=image_tag,
        actor=rolled_back_by,
        stamp=stage == "prod",
        failure=ProjectError("rollback failed", kind="rollback_failed"),
        agent_id=repo.agent_id,
        connection_id=project.connection_id,
    )

    if stage == "prod":
        _stamp_promotion(svc, repo, promoted_by=rolled_back_by, tag=image_tag, build_id=build_id)
        repo.last_promoted_digest = None
    else:
        ts = svc._now().isoformat()
        repo.last_build_started_at = ts
        repo.updated_at = ts
    repo.cicd_status = "promoting"
    svc._save_repo(repo, include_cicd_status=True)
    _append_delivery(
        svc,
        repo,
        stage=stage,
        image_tag=image_tag,
        build_id=build_id,
        actor=rolled_back_by,
    )
    logger.info(
        "[project] rollback started for repo %s (stage %s, tag %s, build %s)",
        repo_id,
        stage,
        image_tag,
        build_id,
    )
    return svc._with_build_kinds([repo])[0]


def build_repo(svc: ProjectService, project_id: str, repo_id: str, *, requested_by: str) -> Repository:
    """Start a DEV build of a CONFIG repo at its default branch's current HEAD.

    The one-click first build of a freshly materialized config repo. The tag is the HEAD
    commit sha, resolved server-side from the provider, never taken from the caller. A
    container repo is refused: its build needs an image only its own CI produces, and the
    chain is resolved with the strict :func:`services.project_materialize._resolve_build_kind`.
    Everything else is :func:`rollback_repo`'s non-prod path: the same in-flight guard, deploy call,
    failure epilogue (no prod-cache stamp) and in-flight stamp.

    Raises ``ProjectError`` with kind ``repo_not_found``, ``container_build``, ``not_ready``,
    ``materialize_failed``, ``no_commit``, ``promote_in_flight``, ``conflict`` or
    ``build_failed``."""
    repo = _resolve_delivery_repo(svc, project_id, repo_id)
    project, builds = _resolve_delivery_context(
        svc, project_id, repo_id, verb="build", noun="build", kind="build_failed"
    )
    build_kind = _buildable_kind(svc, project, repo)
    if _promotion_in_flight(svc, repo):
        raise ProjectError("a promotion is already in flight", kind="promote_in_flight")
    head_sha = _head_sha(svc, project, repo)

    artifact: Dict[str, Any] = (
        {"mcp_server_id": repo.mcp_server_id}
        if repo.artifact_kind == "mcp_server"
        else {"agent_id": repo.agent_id}
    )
    build_id = _start_delivery(
        svc,
        builds,
        repo,
        noun="build",
        stage="dev",
        image_tag=head_sha,
        actor=requested_by,
        stamp=False,
        failure=ProjectError("build failed", kind="build_failed"),
        unrecorded_kinds=frozenset({"build_kind_mismatch"}),
        **artifact,
        connection_id=project.connection_id,
        expected_build_kind=build_kind,
    )

    ts = svc._now().isoformat()
    repo.last_build_started_at = ts
    repo.updated_at = ts
    repo.cicd_status = "promoting"
    svc._save_repo(repo, include_cicd_status=True)
    _append_delivery(
        svc,
        repo,
        stage="dev",
        image_tag=head_sha,
        build_id=build_id,
        actor=requested_by,
        source_sha=head_sha,
    )
    logger.info(
        "[project] build started for repo %s (stage dev, sha %s, build %s)",
        repo_id,
        head_sha,
        build_id,
    )
    repo.build_kind = build_kind
    return repo


def _buildable_kind(svc: ProjectService, project: Project, repo: Repository) -> _BuildKind:
    try:
        build_kind = svc._resolve_build_kind(project.connection_id, repo.template_name)
    except (ClientError, BotoCoreError, TemplateRegistryError, InfraTemplateRegistryError):
        logger.exception("[project] build refused for repo %s: build kind unreadable", repo.id)
        raise ProjectError("build failed", kind="build_failed") from None
    if build_kind != "config":
        raise ProjectError("container repos build from CI on push", kind="container_build")
    if repo.status == "failed":
        raise ProjectError(
            "this repo's setup failed — retry the setup before building",
            kind="materialize_failed",
        )
    if repo.status != "ready":
        raise ProjectError(
            "this repo is still being created — try again when materialize finishes",
            kind="not_ready",
        )
    return build_kind


def _head_sha(svc: ProjectService, project: Project, repo: Repository) -> str:
    try:
        connection = svc._conn.get_connection(project.connection_id)
        token = svc._conn.get_bearer_token(project.connection_id)
        view = svc._rollout.read_repo(connection.org, repo.name, token=token, base_url=connection.base_url)
    except (ConnectionError, GitHubRepoError):
        logger.exception("[project] build refused for repo %s: HEAD unreadable", repo.id)
        raise ProjectError("build failed", kind="build_failed") from None
    if view is None or not view.head_sha:
        raise ProjectError("repo has no commit to build yet", kind="no_commit")
    return view.head_sha
