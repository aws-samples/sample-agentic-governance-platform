"""Build-trigger endpoint (E22/T6): GitHub OIDC → CodeBuild StartBuild.

A GitHub Action, after pushing the agent image to ECR, calls `POST /builds/runtime` with
a GitHub-minted OIDC token to ask AGP to provision the runtime. This is a SEPARATE trust
boundary from the Entra-governed surface: auth is `Depends(verify_github_oidc)` — NOT
`require_role`/`current_principal`.

Defense-in-depth: the validator proves the token is a genuine, current, correctly-audienced
GitHub Actions token; this route additionally asserts the token's `repository_owner` claim
matches the resolved connection's org (so a valid token from org A cannot start a build
against org B's connection) → 403 on mismatch.

The connection credential is never read here; the build service passes only the Secrets
Manager ARN (+ org/repo/base_url) to CodeBuild, which reads the secret under its own role.

Mirrors `connections.py` for the lazy `_svc`/`_build_svc` singletons (tests patch these
module attributes directly so nothing runs against live AWS).
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from core.config import settings
from core.security_github_oidc import GitHubActionsClaims, verify_github_oidc
from models.deployment import DeploymentOutcome
from services.connection_service import ConnectionError, ConnectionService
from services.runtime_build_service import RuntimeBuildError, RuntimeBuildService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/builds", tags=["builds"])

_conn_svc: Optional[ConnectionService] = None
_build_svc: Optional[RuntimeBuildService] = None
_project_svc = None  # ProjectService — set lazily via get_project_service() (tests patch directly)

# ECR's own tag grammar: [A-Za-z0-9._-], 1-128 chars, and a tag may not begin with `-`.
# Applied to the WHOLE tag (which also pins the total length) on top of the required
# `<agent_id>-` prefix — see _assert_tag_belongs_to_agent for why that prefix is an
# identity rather than a label.
_IMAGE_TAG_RE = re.compile(r"\A[A-Za-z0-9._][A-Za-z0-9._-]{0,127}\Z")

# An OCI image digest: the `sha256:` algorithm prefix + exactly 64 LOWERCASE hex chars.
# Anchored and exact-length on purpose — this value is interpolated into the buildspec's
# `<repo>@<digest>` reference, so a loose pattern is both a deploy of an unnamed image and a
# shell-metacharacter seam. Lowercase only: registries emit lowercase and a case-variant digest
# would not resolve, so accepting one would produce a build that fails at pull time instead of a
# 422 the workflow can act on.
_IMAGE_DIGEST_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")


class RuntimeBuildRequest(BaseModel):
    agent_id: str
    image_tag: str
    ecr_repo: str
    connection_id: str
    # The (branch-derived) deploy stage. The TENANT is NOT taken from the body — it is
    # derived server-side from the agent (see the route + service). Only `stage` is trusted
    # from the caller, and it is validated against ("dev","prod").
    stage: str
    # E28B/T4 (D-B3) — the digest of the image the workflow just pushed.
    #
    # THIS FIELD'S ABSENCE WAS A SILENT DATA LOSS. The template workflow already POSTs
    # `image_digest`, and pydantic ignores unknown keys by default, so the digest was accepted with
    # a 202 and DISCARDED — no error anywhere, and the entire digest contract was a no-op
    # end-to-end while looking healthy from both sides.
    #
    # OPTIONAL rather than required, deliberately: a materialized repo carries a COMMITTED copy of
    # the workflow, so agent repos created before this epic keep POSTing a digest-less body. Making
    # it required would 422 every one of them — turning a missing optimization into a total outage
    # of the dev deploy path. Absent ⇒ the legacy tag-only deploy, which still works.
    image_digest: Optional[str] = None

    @field_validator("image_digest")
    @classmethod
    def _digest_shape(cls, v: Optional[str]) -> Optional[str]:
        """Reject a present-but-malformed digest; normalize empty to ``None``.

        A 422 here is the RIGHT answer where the tag path's fixed-detail 403 is not: this value
        comes from the workflow's own build step, not from an attacker probing identity, so a
        malformed one is a template/CI bug the workflow author must see and fix. Empty collapses to
        `None` (the legacy path) rather than 422, because a job output can legitimately arrive empty
        from a skipped upstream step and that is indistinguishable from "no digest sent"."""
        if v is None or not v.strip():
            return None
        if not _IMAGE_DIGEST_RE.match(v):
            raise ValueError("image_digest must be 'sha256:' followed by 64 lowercase hex chars")
        return v


class RuntimeBuildResponse(BaseModel):
    build_id: str
    status: str


class ProdCandidateRequest(BaseModel):
    agent_id: str
    image_tag: str
    # Accepted for symmetry with /runtime's body (the workflow posts one body shape) but
    # NEITHER trusted NOR stored — nothing downstream reads a caller-supplied registry.
    ecr_repo: str
    connection_id: str


class ProdCandidateResponse(BaseModel):
    status: str


def get_connection_service() -> ConnectionService:
    """Lazy ConnectionService singleton (tests patch `_conn_svc` directly)."""
    global _conn_svc
    if _conn_svc is None:
        _conn_svc = ConnectionService(
            table_name=settings.CONNECTIONS_TABLE_NAME,
            secret_prefix=settings.CONNECTIONS_SECRET_PREFIX,
            region=settings.AWS_REGION,
        )
    return _conn_svc


def get_build_service() -> RuntimeBuildService:
    """Lazy RuntimeBuildService singleton (tests patch `_build_svc` directly)."""
    global _build_svc
    if _build_svc is None:
        import boto3

        from api.routes.agents import get_service as get_agent_registry
        from services.tenant_service import TenantService

        _build_svc = RuntimeBuildService(
            boto3.client("codebuild", region_name=settings.AWS_REGION),
            get_connection_service(),
            boto3.client("secretsmanager", region_name=settings.AWS_REGION),
            tenant_service=TenantService(
                table_name=settings.TENANTS_TABLE_NAME, region=settings.AWS_REGION
            ),
            agent_registry=get_agent_registry(),
            codebuild_project_name=settings.CODEBUILD_PROJECT_NAME,
            scratch_secret_prefix=settings.RUNTIME_BUILD_TOKEN_PREFIX,
        )
    return _build_svc


def get_project_service():
    """The shared ProjectService (reused from the projects route) — used to resolve the
    Repository that owns the POSTed agent_id for the E25/I1 token↔agent binding. Tests patch
    the module-level `_project_svc` directly so this never runs live."""
    global _project_svc
    if _project_svc is None:
        from api.routes.projects import get_project_service as _get
        _project_svc = _get()
    return _project_svc


def _resolve_connection_for_token(gh: GitHubActionsClaims, connection_id: str):
    """Resolve the POSTed connection (404) and assert the token was minted in its org (403).

    Check 1+2 of the OIDC trust boundary, shared VERBATIM by every route on this boundary
    (`/runtime` and `/prod-candidate`) so the two can never drift apart.
    """
    conn_svc = get_connection_service()
    try:
        conn = conn_svc.get_connection(connection_id)
    except ConnectionError:
        # Fixed detail — never echoes a store/exception value.
        raise HTTPException(status_code=404, detail="Connection not found")

    # GitHub org/repo logins are case-insensitive; the OIDC claim carries canonical case
    # while our stored org may diverge. Case-fold both trusted operands (never a
    # false-accept — both come from our store / the proven token).
    if gh.repository_owner.lower() != conn.org.lower():
        logger.warning(
            "[builds] org mismatch: token owner=%s connection org=%s",
            gh.repository_owner, conn.org,
        )
        raise HTTPException(
            status_code=403,
            detail="Token repository_owner does not match connection org",
        )
    return conn


def _assert_token_owns_agent(gh: GitHubActionsClaims, agent_id: str) -> None:
    """BIND agent_id → repo → token.repository (E25/I1). The org-match only proves the
    token belongs to SOME repo in the connected org — it does NOT prove the token's repo
    owns the caller-supplied agent_id. Without this, repo A's workflow could POST repo B's
    agent_id and trigger a build that derives B's tenant + assumes B's cross-account deploy
    role (a cross-tenant redeploy lever), or register a prod candidate against B's repo. So we
    resolve the Repository that OWNS this agent_id, resolve its org via the repo's project
    connection, and require the OIDC-proven `repository` claim to equal `<org>/<repo.name>`.
    The proven repo identity is authoritative over the body's agent_id; an unknown agent or
    any mismatch → 403.
    """
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
    # Case-fold the binding comparison for the same reason as the org-match above: GitHub
    # identity is case-insensitive, so a case-divergent (but equal) repo must not false-403.
    if (
        repo is None
        or repo_org is None
        or gh.repository.lower() != f"{repo_org}/{repo.name}".lower()
    ):
        logger.warning(
            "[builds] agent/repo binding failed: token repo=%s agent_id=%s",
            gh.repository, agent_id,
        )
        raise HTTPException(
            status_code=403, detail="Token repository does not own this agent"
        )


def _assert_tag_belongs_to_agent(gh: GitHubActionsClaims, agent_id: str, image_tag: str) -> None:
    """BIND the candidate image_tag → agent_id. The binding above proves the token owns the
    agent; it does NOT prove the TAG names that agent's artifact — and the tag is the single
    value a project OWNER's production approval attests to.

    The tenant ECR repository is SHARED by every materialized agent
    (`modules/agent_ecr/main.tf`), so the tag PREFIX is the only agent-identity boundary
    inside the registry: AGP already relies on it to enumerate an agent's images
    (`ecr_image_service.py` builds `prefix = f"{agent_id}-"`), and the runtime buildspec
    re-derives `AGENT_ID="${IMAGE_TAG%%-*}"` — that derived id then drives the registry
    read/write and the Terraform state key, and it is NOT passed in the StartBuild env. So an
    unbound prefix is an IDENTITY, not a label: `agent_id=<A>` with
    `image_tag="<B>-deadbee"` would make A's owner promote B's image over B's state.

    Two conditions, both required: the proven `<agent_id>-` prefix (the convention the
    committed workflow already produces as `{AGENT_ID}-{tree_sha[:7]}`) and ECR's own tag
    charset (which also keeps a whitespace/metacharacter tag out of the buildspec's shell).
    """
    if not image_tag.startswith(f"{agent_id}-") or not _IMAGE_TAG_RE.match(image_tag):
        logger.warning(
            "[builds] candidate tag/agent binding failed: token repo=%s agent_id=%s",
            gh.repository, agent_id,
        )
        # Fixed detail — never echoes the offending tag back to the caller.
        raise HTTPException(
            status_code=403, detail="Image tag does not belong to this agent"
        )


@router.post("/runtime", response_model=RuntimeBuildResponse, status_code=202)
async def start_runtime_build(
    body: RuntimeBuildRequest,
    gh: GitHubActionsClaims = Depends(verify_github_oidc),
):
    """Start the runtime-provision CodeBuild for a pushed agent image.

    Auth: GitHub OIDC (separate trust boundary). Defense-in-depth: the token's
    `repository_owner` MUST match the resolved connection's org (403 otherwise).
    """
    _resolve_connection_for_token(gh, body.connection_id)

    # Only `stage` is trusted from the body (the tenant is derived server-side from the
    # agent inside the service). Reject anything outside the known stages.
    if body.stage not in ("dev", "prod"):
        raise HTTPException(status_code=422, detail="Invalid stage")

    _assert_token_owns_agent(gh, body.agent_id)

    # E28B/T4 — BIND THE TAG TO THE AGENT, *BEFORE* anything is started.
    #
    # The binding above proves the token owns the agent; it does NOT prove the TAG names that
    # agent's artifact. That matters more on this route than it used to, because a trunk push now
    # registers a PROD CANDIDATE from this same body — so an unbound tag would become the artifact
    # an OWNER later approves for production (see `_assert_tag_belongs_to_agent` for why the tag
    # PREFIX is an identity rather than a label on a shared tenant registry).
    #
    # It runs HERE rather than inside the registrar deliberately. Placed after `StartBuild` it
    # produced a 403 describing a build that was ALREADY RUNNING, with a delivery row appended for
    # it — the route answering "refused" about work it had just set in motion. Every refusal on
    # this route must precede every side effect, which is the same rule the stage/prod checks
    # below already follow.
    _assert_tag_belongs_to_agent(gh, body.agent_id, body.image_tag)

    # E27 — prod is an AGP-owned decision (design §5). The OIDC path has no human
    # principal, so an OWNER check is impossible here; refusing prod is what makes the
    # promote route's gate real rather than advisory. Dev is unchanged.
    #
    # Placed AFTER the binding checks on purpose: a probe with a foreign-repo token still
    # gets the binding 403, so the ordering leaks nothing about stage handling. Also note
    # this refusal lives in the ROUTE, not the service — the promote route calls
    # `RuntimeBuildService.start_runtime_build(..., stage="prod")` directly, so refusing in
    # the service would break the one legitimate prod path.
    if body.stage == "prod":
        raise HTTPException(
            status_code=403,
            detail="prod deploys must be initiated from AGP",
        )

    build_svc = get_build_service()
    try:
        build_id = build_svc.start_runtime_build(
            agent_id=body.agent_id,
            image_tag=body.image_tag,
            ecr_repo=body.ecr_repo,
            connection_id=body.connection_id,
            stage=body.stage,
            image_digest=body.image_digest or "",
        )
    except RuntimeBuildError:
        # E28/T4 — a build that could not start is still a delivery ATTEMPT and belongs in the
        # history; with no row, "the build failed" and "nobody ever pushed" look identical.
        _append_build_record(gh, body, build_id=None, failed=True)
        raise HTTPException(status_code=502, detail={"status": "failed_to_start"})

    # E28/T4 (D7) — APPEND the delivery record. Placed AFTER every refusal above, so a rejected
    # stage or a failed binding check leaves no phantom row in the history.
    _append_build_record(gh, body, build_id=build_id, failed=False)
    # E28B/T4 (D-B3) — REGISTER THE PROD CANDIDATE. This route is now the registrar.
    _register_candidate_from_build(gh, body)
    return RuntimeBuildResponse(build_id=build_id, status="started")


def _register_candidate_from_build(gh: GitHubActionsClaims, body: RuntimeBuildRequest) -> None:
    """Register this build's artifact as the prod candidate, IF it came from the project's trunk.

    WHY THIS LIVES HERE NOW (E28B/T4). E28B collapsed the template workflow to one path and deleted
    its ``candidate`` job, which was the only caller of ``POST /builds/prod-candidate`` — leaving
    NOTHING writing the candidate block, so ``promote_repo`` refused every promotion with
    ``no_prod_candidate`` permanently. Registration moved to the build trigger because this route
    already proves, from the same validated OIDC token, all three facts a candidate needs: WHICH
    artifact (tag + digest), WHO merged (``actor``) and WHICH commit (``sha``). The
    ``/prod-candidate`` route is left in place and working for any repo still running the old
    two-job workflow.

    **THE TRUNK GATE IS THE GOVERNANCE CONTROL, and it is why this is not simply "register on every
    build".** A candidate is what an OWNER's production approval attests to, so only a ref that
    went through the provider's review may produce one. The gate compares against the owning
    project's ``trunk_branch`` (D-B5) rather than a ``main`` literal: branch names became project
    config this epic, so a hardcoded ``main`` would silently register nothing for a project whose
    trunk is ``release`` — a promote surface that goes quiet with no error, which is this epic's
    signature defect. A live probe already found the inverse hole (a bare template tree on ``main``
    arriving promotable); this gate is what keeps a non-trunk push from reopening it.

    BEST-EFFORT, exactly like ``_append_build_record``: the build has already started by the time
    this runs, so a store fault must not turn a live deploy into a 500 the workflow retries. An
    unregistered candidate is a Promote button that stays quiet until the next merge; a wrong HTTP
    answer here would misreport what happened to a runtime.
    """
    try:
        project_svc = get_project_service()
        repo = project_svc.find_repository_by_agent_id(body.agent_id)
        if repo is None:  # pragma: no cover — the binding check already proved it exists
            return
        detail = project_svc.get_project(repo.project_id)
        if detail is None:  # pragma: no cover — the repo's parent should still exist
            return
        # Default to "main" ONLY if the project record predates D-B5 and carries no trunk at all.
        # `Project` has never validated `trunk_branch` (the non-blank and `"main"`-only validators
        # were `ProjectCreate`'s, and E36/T15 deleted them), so the `or "main"` below — not a model
        # rule — is the guard: it covers both a pre-E28B row with no key and a blank stored value.
        trunk = getattr(detail.project, "trunk_branch", None) or "main"
        if gh.ref != f"refs/heads/{trunk}":
            logger.info(
                "[builds] not registering a prod candidate for agent %s: ref is not the trunk",
                body.agent_id,
            )
            return
        # NOTE the tag↔agent binding is NOT re-run here: the caller asserts it BEFORE `StartBuild`,
        # so a foreign tag never reaches this function at all. Running it here instead was a defect
        # — the 403 arrived after the build had started and a delivery row had been appended.
        project_svc.record_prod_candidate(
            body.agent_id,
            image_tag=body.image_tag,
            image_digest=body.image_digest or "",
            # OIDC-proven, never body-asserted — the attribution rule E27 established: a
            # body-supplied actor would let any holder of the build credential claim someone
            # else's merge.
            sha=gh.sha or "",
            actor=gh.actor or "",
        )
    except Exception:
        # Deliberately broad (the `_append_build_record` argument): the build is already running, so
        # nothing this can raise may change the response the workflow sees. The traceback is logged
        # (never an exception value / any secret) so the bug stays visible.
        #
        # This catch is TOTAL by design — there is deliberately no `except HTTPException: raise`
        # ahead of it. An earlier revision had one, to let the tag-binding 403 escape from here; that
        # was the defect, because by this point the build has started and a 403 would describe work
        # already in motion. Every refusal now happens BEFORE `StartBuild`, so nothing raised here
        # is a refusal — it can only be a fault, and a fault must not change the answer.
        logger.exception(
            "[builds] could not register the prod candidate for agent %s (tag %s)",
            body.agent_id, body.image_tag,
        )


def _append_build_record(
    gh: GitHubActionsClaims, body: RuntimeBuildRequest, *, build_id: Optional[str], failed: bool
) -> None:
    """Append one ``Deployment`` row for this OIDC-triggered build (E28/T4, D7).

    ``actor``/``source_sha`` come from the VALIDATED OIDC token, never the body — the same
    attribution rule ``/prod-candidate`` follows, and the reason ``actor_kind`` is ``"github"``
    here: this actor is an OIDC-proven GitHub login, a fundamentally different currency from the
    Entra oid a promote/rollback records (C1). Storing or rendering one as the other would
    misattribute a deployment to a person who never authorized it.

    The row is resolved to a repo via ``find_repository_by_agent_id`` — which the binding check
    above has ALREADY proven owns this agent — so no unowned agent can invent history.

    BEST-EFFORT: the build has already been started (or definitively refused) by the time this
    runs, so a store fault must not turn a live deploy into a 500 the workflow retries, nor
    replace the curated 502. A missing history row is a gap in a list; a wrong HTTP answer here
    would misreport what happened to a runtime.
    """
    try:
        project_svc = get_project_service()
        repo = project_svc.find_repository_by_agent_id(body.agent_id)
        if repo is None:  # pragma: no cover — the binding check above already proved it exists
            return
        # A terminal row is appended already CLOSED — the partition is append-only (C1), so
        # there is no `started` row to close in place later.
        completed_at = datetime.now(timezone.utc).isoformat() if failed else None
        project_svc.append_deployment(
            repo_id=repo.id,
            agent_id=body.agent_id,
            stage=body.stage,
            image_tag=body.image_tag,
            source_sha=gh.sha or None,
            build_id=build_id,
            outcome=DeploymentOutcome.FAILED if failed else DeploymentOutcome.STARTED,
            actor=gh.actor or None,
            actor_kind="github",
            completed_at=completed_at,
            # SAFE fixed hint — the build service's own message never reaches the record.
            error="failed to start the runtime build" if failed else None,
        )
    except Exception:
        # Deliberately broad, unlike the service's enumerated sets: this is a pure side-channel
        # write on a path whose primary outcome is already decided, so NOTHING it can raise —
        # including an AGP bug — may change the response the workflow sees. The traceback is
        # logged (never the exception value / any secret) so the bug is still visible.
        logger.exception(
            "[builds] could not append the deployment record for agent %s (stage %s, tag %s)",
            body.agent_id, body.stage, body.image_tag,
        )


@router.post("/prod-candidate", response_model=ProdCandidateResponse, status_code=202)
async def register_prod_candidate(
    body: ProdCandidateRequest,
    gh: GitHubActionsClaims = Depends(verify_github_oidc),
):
    """Register `main`'s HEAD as THE prod candidate (E27A/T6). No build is started.

    This is the trust boundary where AGP learns, provably, WHICH image and WHICH human: the
    workflow's `candidate` job calls this after a merge to `main` has pushed the tree-sha
    image. `actor` and `sha` are read ONLY from the validated OIDC token — never from the
    body, which is why the recorded "who merged" survives as attribution a promote can be
    audited against (design §4). `image_tag` does come from the body (the workflow computed
    it from the same tree the token proves) but is NOT taken on trust — it must name the
    agent the token was just proven to own (see `_assert_tag_belongs_to_agent`);
    `ecr_repo` is accepted and DISCARDED.

    Auth: GitHub OIDC — the same separate trust boundary as `/runtime`, running the same
    checks in the same order via the shared helpers above.
    """
    _resolve_connection_for_token(gh, body.connection_id)
    _assert_token_owns_agent(gh, body.agent_id)

    # main IS the prod candidate (design §1). Checked AFTER the binding checks so a
    # foreign-repo token still gets the binding 403 first — the ordering leaks nothing
    # about branch handling (the E27/T9 rule at the /runtime route above).
    if gh.ref != "refs/heads/main":
        raise HTTPException(status_code=403, detail="prod candidates come from main only")

    # LAST of the five checks: the four above are about the TOKEN, this one is about the BODY.
    # Keeping it last preserves the established refusal ordering (which leaks nothing) while
    # still guarding BEFORE the write — nothing is persisted for a rejected tag.
    _assert_tag_belongs_to_agent(gh, body.agent_id, body.image_tag)

    # Local import: this module keeps the ProjectService dependency lazy (see
    # get_project_service) so importing the builds route never drags the project stack in.
    from services.project_service import ProjectError

    project_svc = get_project_service()
    try:
        project_svc.record_prod_candidate(
            body.agent_id,
            image_tag=body.image_tag,
            # OIDC-proven, never body-asserted. Both claims are Optional on the model
            # (GitHub always mints them on an Actions token, but the validator does not
            # require them), so coerce to "" rather than writing None into the record.
            sha=gh.sha or "",
            actor=gh.actor or "",
        )
    except ProjectError:
        # The binding above already proved an owning repo existed; a `not_found` here means it
        # vanished in between. Fixed detail — never `str(err)`, which could echo the store.
        raise HTTPException(status_code=404, detail="Agent not found")

    return ProdCandidateResponse(status="registered")
