"""Projects (containers) + Repositories API (Epic 20 / Task 10).

Operator-gated CRUD over 1:N template-materialized projects. Mirrors ``connections.py`` /
``ops_templates.py``: the lazy ``_svc`` singleton (tests patch ``_svc`` directly so this
never runs against live AWS/Entra/GitHub), ``require_role(Role.OPERATOR)`` on every
endpoint, and a FIXED-``detail`` error map (never ``str(err)``, so a raw store/GitHub
message can't leak — the lone exception is the curated ``agent_config`` ValueError literal,
a §safe message with no secret). ``created_by`` comes from the validated ``principal``,
never the body.

A **Project** is now an EMPTY CONTAINER scoped to one org. Materializing a repo+agent from a
template happens on ``POST /projects/{id}/repos`` (``ProjectService.add_repo``); the flat
``GET /repositories`` list has its own ``repositories_router`` in this module. Both routers
share the single ``_svc`` singleton.

The connection PAT never crosses any boundary here — it flows only through the T6 GitHub
write client inside ``ProjectService``.

Multi-tenancy (E24/T6): every project belongs to exactly one tenant. The list is
post-filtered with ``visible(ctx, p.tenant_id)``; project detail + repo-create gate on
visibility → 404 with the EXISTING "Project not found" literal (byte-identical to a
truly-missing id — the 404-not-403 contract); create validates the tenant exists (400
"unknown tenant") + membership for a non-global caller (403 "tenant not permitted").
**Repositories carry NO tenant field** — they inherit through their parent project, so
the flat ``/repositories`` list filters each repo by its project's tenant.

Per-project roles (E27/T4): every route here now ALSO gates on the caller's authority over
the specific project — VIEWER to read, MAINTAINER to materialize/retry, OWNER to destroy
(``_require_project_role_or_ungoverned``); the two LIST routes FILTER instead of refusing.
Checked IN ADDITION to tenant visibility and always AFTER it, so a foreign tenant still gets
the byte-identical 404 and a 403 can only ever confirm a project the caller's own tenant
already exposes. ``may()`` is the only authority seam — the role STORE is read for exactly
one thing: whether a project has any role rows at all, which drives the design-§3 fallback
that keeps pre-migration (ungoverned) projects working at MAINTAINER level. That fallback
never reaches OWNER, so the E23 delete cascade always needs a real OWNER row. Both governed-
or-not reads are the store's STRICT variants and fail CLOSED, because "the partition is
empty" and "the partition is unreadable" are the same value to a degrading read but opposite
answers to an authorization question.

The detail read ALSO reports the caller's own standing as two additive UI HINTS —
``effective_role`` + ``ungoverned`` (E27/T11, see ``_effective_role_hint``). They are NEVER an
authority: the browser cannot evaluate an Entra GROUP grant, so without them a group-derived
OWNER is indistinguishable from a role-less caller and the UI must guess. Enforcement stays
exactly where it was, in ``may()`` and the gates above.

Governed promotion (E27/T8): ``POST /{id}/repos/{repo_id}/promote`` is the epic's headline
action — an OWNER asks for prod and the BACKEND resolves which image (no tag is ever accepted
from a caller). It is the ONE non-role-CRUD route on the STRICT gate: shipping to production
must not ride the §3 ungoverned fallback, so it needs a real OWNER row even on a
pre-migration project.
"""

import functools
import logging
from typing import List, Optional

import anyio.to_thread
from fastapi import APIRouter, BackgroundTasks, HTTPException, Response
from fastapi import Depends as RBACDepends

from api.routes.agents import (
    get_identity_service,
    get_langfuse_service,
    get_service as get_agent_registry,
)
from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from models.project import Project, ProjectCreate, ProjectDetail
from models.project_role import ProjectRole, ProjectRoleCreate, ProjectRoleRecord, role_name
from models.repository import (
    PullRequestCreate,
    PullRequestView,
    RepoDeletePreview,
    RepoDeleteResult,
    RepoDeleteSelection,
    Repository,
    RepositoryCreate,
    RepoRollbackRequest,
)
from services.agent_registry_service import NameTakenError
# ``ConnectionError`` here is the SERVICE's class, which subclasses ``Exception`` and is NOT
# Python's builtin of the same name. Importing it shadows the builtin for this module, which is
# deliberate and matches ``connections.py`` / ``github_link.py`` / ``project_service.py`` — and
# is why it is imported at all: an ``except ConnectionError`` relying on the builtin would
# silently never fire against a connection-service failure.
from services.connection_service import ConnectionError, ConnectionService
from services.ecr_image_service import EcrImageService
from services.github_pr_service import GitHubPrError, GitHubPrService
from services.project_resolver import ProjectContext, context_from_rows, may, widen
from services.project_role_service import ProjectRoleError, ProjectRoleService
from services.project_service import ProjectError, ProjectService
from services.template_registry import TemplateRegistry
from services.tenant_resolver import TenantContext, visible

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects", tags=["projects"])
repositories_router = APIRouter(prefix="/repositories", tags=["repositories"])

_svc: Optional[ProjectService] = None
# E27/T3 — the per-project role store (a THIRD partition in the projects table). Routes
# NEVER read it to decide authority (that is ``may(pctx, ...)``); this is only the
# write/read surface the role-CRUD routes below manage. Tests patch ``_role_svc`` directly.
_role_svc: Optional[ProjectRoleService] = None
# E28/T14 — the pull-request client. STATELESS and credential-free by construction (it takes
# its token as a parameter and can reach no credential seam — see the module), so the
# singleton holds nothing but one pooled ``httpx.Client``. Tests patch ``get_pr_service``.
_pr_svc: Optional[GitHubPrService] = None


class ProjectRoleRead(ProjectRoleRecord):
    """Response model for the role routes — ``ProjectRoleRecord``'s fields verbatim.

    A role row carries only metadata (no credential, no secret), so the stored record is
    safe to return as-is; subclassing keeps the two from drifting apart."""


def get_project_role_service() -> ProjectRoleService:
    """Lazy ``ProjectRoleService`` singleton (E27/T3) — roles live in a THIRD partition of
    the EXISTING projects table, so there is no new table and no new env var. Also the
    collaborator ``users.get_project_resolver()`` builds the ONE ``ProjectResolver`` from,
    so the role-CRUD writes here and the authz reads there share one store. Tests patch
    ``_role_svc`` directly so this never runs live."""
    global _role_svc
    if _role_svc is None:
        _role_svc = ProjectRoleService(
            table_name=settings.PROJECTS_TABLE_NAME, region=settings.AWS_REGION
        )
    return _role_svc


async def get_tenant_ctx(
    principal: Principal = RBACDepends(current_principal),
) -> TenantContext:
    """Delegate to the ONE resolver-singleton accessor (E24/T6).

    ``users.py`` owns the lazy ``TenantResolver`` singleton (``_tenant_resolver`` /
    ``get_tenant_resolver()``); this is a thin re-export so projects.py has its own
    per-request ``tenant_ctx`` dependency WITHOUT keeping a second resolver copy —
    tests patch ``api.routes.users._tenant_resolver`` and both /users/me and every
    route here observe it. Imported lazily to avoid an import cycle at module load.
    """
    from api.routes.users import get_tenant_ctx as _users_get_tenant_ctx

    return await _users_get_tenant_ctx(principal)


async def get_project_ctx(
    principal: Principal = RBACDepends(current_principal),
) -> ProjectContext:
    """Delegate to the ONE project-resolver-singleton accessor (E27/T3).

    ``users.py`` owns the lazy ``ProjectResolver`` singleton (``_project_resolver`` /
    ``get_project_resolver()``); this is a thin re-export so projects.py has its own
    per-request ``project_ctx`` dependency WITHOUT keeping a second resolver copy —
    tests patch ``api.routes.users._project_resolver`` and every gated route here
    observes it. Imported lazily to avoid an import cycle at module load (mirrors
    ``get_tenant_ctx`` above).
    """
    from api.routes.users import get_project_ctx as _users_get_project_ctx

    return await _users_get_project_ctx(principal)


def _require_project_role(
    pctx: ProjectContext, project_id: str, required: ProjectRole, *, principal: Principal
) -> ProjectContext:
    """Gate an action on the caller's per-project authority. ``may()`` is the ONLY seam —
    this never reads ``ProjectRoleService`` to decide. Runs AFTER ``_load_visible_project``,
    so a foreign-tenant project has already 404'd and a 403 here can only ever confirm a
    project the caller's own tenant already exposes.

    STRICT — no ungoverned-project fallback. Used by the role-CRUD routes above (the roster
    IS the governance surface, so "this project has no roles yet" must never be the reason
    someone gets to read or rewrite it) AND by ``promote_repo`` (E27/T8 — a prod release with
    nobody accountable for it must be refused, not defaulted). Every OTHER project route uses
    ``_require_project_role_or_ungoverned`` below.

    A refusal is confirmed against a FRESH read before it is served (``_refreshed_pctx``):
    the resolver's cache is process-local, so on a multi-task deployment a cached snapshot may
    be pre-grant and denying on it alone would 403 a caller on a role they genuinely hold.

    Returns the context it actually DECIDED on, so a caller that also needs to report the
    caller's standing (the detail route's UI hints) reuses that one answer instead of paying
    for a second read — and cannot disagree with the gate that just ran."""
    if may(pctx, project_id, required):
        return pctx
    fresh = _refreshed_pctx(pctx, principal, project_id)
    if may(fresh, project_id, required):
        return fresh
    raise HTTPException(status_code=403, detail="insufficient project role")


def _may_project(
    pctx: ProjectContext, project_id: str, required: ProjectRole, *, has_rows: bool
) -> bool:
    """Project-role gate with the ungoverned-project fallback (design §3).

    A project with NO role rows is 'not yet governed': tenant-visible callers act as
    MAINTAINER so pre-migration projects keep working. OWNER verbs always need a real row."""
    if may(pctx, project_id, required):
        return True
    if not has_rows and required <= ProjectRole.MAINTAINER:
        return True
    return False


def _require_project_role_or_ungoverned(
    pctx: ProjectContext, project_id: str, required: ProjectRole, *, principal: Principal
) -> ProjectContext:
    """The E27/T4 gate on the pre-existing project routes: ``_may_project`` plus the §3
    fallback, raising the SAME 403 literal the strict gate uses.

    ``may()`` is checked FIRST and returns early, which is what makes the ``has_rows`` store
    read happen ONLY on the deny path — a caller who genuinely holds the role pays nothing.
    Runs AFTER ``_load_visible_project``, so a foreign-tenant project has already 404'd.

    The §3 fallback does NOT cover the stale-cache case, which is why the refresh below is
    needed on this gate too: ``has_role_rows`` is a LIVE read, so the moment the creator-OWNER
    row exists the project reads as GOVERNED on every task — including one whose cached
    snapshot predates that row. The fallback is then correctly withheld and the caller is hard
    403'd on the project they just created. Confirming the refusal against a fresh read is what
    closes it.

    Returns the context it DECIDED on, same contract as the strict gate above."""
    if may(pctx, project_id, required):
        return pctx
    if _may_project(
        pctx, project_id, required, has_rows=_project_has_role_rows(project_id)
    ):
        return pctx
    fresh = _refreshed_pctx(pctx, principal, project_id)
    if may(fresh, project_id, required):
        return fresh
    raise HTTPException(status_code=403, detail="insufficient project role")


def _refreshed_pctx(
    pctx: ProjectContext, principal: Principal, project_id: str
) -> ProjectContext:
    """Re-resolve ONE project from a FRESH store read — the DENY path's second opinion.

    The gates above have already refused on the per-request ``ProjectContext``, which is folded
    from the resolver's process-local cache. On a multi-task deployment
    (``ecs_desired_count = 2``) that snapshot can be PRE-GRANT: ``invalidate()`` only clears the
    cache of the task that served the write. The headline flow hits this every time — an
    operator creates a project (task A grants them OWNER and invalidates task A), then the very
    next request load-balances to task B and is refused on their own brand-new project.

    The result is only ever ORed onto a refusal, never substituted for a grant, so this can
    only RESTORE authority the store already says the caller holds. It cannot widen anything,
    and it leaves the documented revoke-direction staleness window exactly as it was (still
    fail-safe, still bounded by the TTL). No cross-process invalidation, no lock service, no
    reconciler.

    Cost is one ``begins_with`` range read, on the DENY path only — a caller who holds the role
    is served from cache and pays nothing.

    Fails CLOSED on anything unexpected: a store fault (``ProjectRoleError``) or a resolver
    failure means no extra authority could be established, so the ORIGINAL context is handed
    back unchanged and the refusal stands. Never raises — a clean 403 must not become a 500."""
    from api.routes.users import get_project_resolver

    try:
        return get_project_resolver().refresh_project(principal, project_id)
    except ProjectRoleError:
        logger.warning(
            "Could not re-read the role rows for project %s while confirming a refusal; "
            "the refusal stands",
            project_id,
        )
        return pctx
    except Exception:  # noqa: BLE001 — a confirmation read must never 500 a clean 403.
        logger.exception(
            "Failed to refresh project roles for %s while confirming a refusal", project_id
        )
        return pctx


def _project_has_role_rows(project_id: str) -> bool:
    """Is this project GOVERNED — does it hold at least one role row? The §3 fallback's
    only input, read for ONE project on the deny path of a single route.

    Reached through the role service rather than the resolver because a ``ProjectContext``
    carries only the CALLER's matching rows, so it cannot tell "nobody governs this project"
    from "somebody else does".

    ``has_role_rows`` is the STRICT read, not the degrading ``list_for_project``: a swallowed
    DDB ``ClientError`` would return ``[]``, which is indistinguishable from a genuinely
    ungoverned project — so one transient store fault would make every governed project look
    ungoverned and hand a role-less caller the §3 MAINTAINER fallback on someone else's
    project. ANY ``ProjectRoleError`` (``ownership_unverified`` from an unreadable partition,
    or ``validation`` when the store refuses a '#'-bearing id whose composite sk would be
    non-injective) means we could NOT establish that the project is ungoverned — so the
    fallback must not be handed out and only the caller's real role decides: treat it as
    governed and fail CLOSED. A 503 here would instead turn one blip into a hard outage for
    every pre-migration project, and fail-closed already denies exactly what the fallback
    would have granted."""
    try:
        return get_project_role_service().has_role_rows(project_id)
    except ProjectRoleError:
        logger.warning(
            "Could not read the role rows for project %s; treating it as governed so the "
            "ungoverned-project fallback cannot apply",
            project_id,
        )
        return True


def _governed_rows() -> List[ProjectRoleRecord]:
    """Every role row in the partition — the two LIST routes' single read.

    Serves BOTH list-route inputs from one read: the §3 governed-project set
    (``_governed_project_ids``) and the caller's own FRESH per-project roles (folded via
    ``ProjectResolver.context_from_rows``). Reading them separately is what made the list and
    the detail page disagree for up to a TTL about the same brand-new project — the list
    filtered on the store while the detail hint reported the cache.

    ``list_all_strict``, not the degrading ``list_all``: an unreadable partition would come
    back as ``[]`` — an EMPTY governed set, i.e. "nothing is governed", which is the maximally
    permissive answer and would leak every governed project in the tenant to a caller holding
    no role on any of them. There is no fail-closed *set* to substitute (a list has to be
    computed to be filtered), so an unverifiable read surfaces as the SAME 503 +
    ``ownership_unverified`` literal the role-write guard uses — retryable, and it never
    serves a silently-widened inventory."""
    try:
        return get_project_role_service().list_all_strict()
    except ProjectRoleError:
        logger.warning(
            "Could not read the project_role partition; refusing to serve a list filtered "
            "against an unverifiable governed set"
        )
        raise HTTPException(
            status_code=503, detail="could not verify project ownership"
        ) from None


def _list_pctx(
    pctx: ProjectContext, principal: Principal, rows: List[ProjectRoleRecord]
) -> ProjectContext:
    """Fold the list routes' ALREADY-READ rows into a fresh context for the filter.

    Zero extra store reads (``_governed_rows`` has just read the partition), and it makes the
    two surfaces consistent: the list now filters on the same freshly-read rows the detail
    route's gate falls back to, so a project whose grant landed on another ECS task appears in
    the list AND keeps its affordances on the detail page.

    ``context_from_rows`` is a module-level PURE fold (no I/O, no cache, no resolver state) —
    the same ``may()``-style seam, and the same fold ``resolve()`` uses, so the two paths cannot
    disagree about what a row means. ``widen`` (max per project) is what keeps this ADDITIVE:
    the freshly-read rows can only ADD a role the cache had not seen yet, never drop or lower
    one the cached context already carries (a group-derived grant that only ``resolve()``'s
    Graph lookup can see must survive this)."""
    return widen(pctx, context_from_rows(principal, rows))


def _governed_project_ids(rows: List[ProjectRoleRecord]) -> frozenset[str]:
    """The ids of every project that holds at least one role row — the §3 fallback's input
    for the two LIST routes. A pure derivation from ``_governed_rows()``' single read.

    ONE whole-partition read per request, NOT one per row: the lists are page-shaped, so a
    per-project ``has_role_rows`` would be an N+1 against DDB. Only called when the
    caller is non-global (an admin's ``may()`` short-circuits True, so the set is never
    needed)."""
    return frozenset(r.project_id for r in rows)


def _effective_role_hint(pctx: ProjectContext, project_id: str) -> Optional[str]:
    """The caller's EFFECTIVE role on ``project_id`` as a wire name — a UI HINT, NEVER an
    authority (E27/T11).

    **Nothing may ever gate on this.** ``may(pctx, ...)`` and the ``_require_project_role*``
    helpers above stay the only enforcement; this exists purely so the frontend can decide
    which affordances to RENDER. The browser cannot compute the answer itself — a role may be
    granted to an Entra GROUP and no client-side signal evaluates group membership — so
    without this a group-derived OWNER (the design-§9 recommended shape) is indistinguishable
    from a role-less caller and loses their own buttons.

    Derived from the ALREADY-RESOLVED ``ProjectContext``: no second store read, no second
    source of truth. A global admin reports ``"owner"``, mirroring ``may()``'s ``is_global``
    short-circuit — they may do everything, so anything weaker would be a lie in the other
    direction."""
    if pctx.is_global:
        return role_name(ProjectRole.OWNER)
    held = pctx.roles.get(project_id)
    return None if held is None else role_name(held)


def _load_visible_project(id: str, ctx: TenantContext) -> ProjectDetail:
    """Load a project (detail) by id and gate it on tenant visibility BEFORE any side
    effect. ONE helper the detail route AND repo-create call — a missing OR not-visible
    project raises the SAME 404 literal ("Project not found"), byte-identical to a
    truly-missing id (the 404-not-403 contract: a foreign tenant's project must look
    absent, never leak a 403 that would confirm it exists)."""
    detail = get_project_service().get_project(id)
    if detail is None or not visible(ctx, detail.project.tenant_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return detail


def get_project_service() -> ProjectService:
    """Lazy ``ProjectService`` singleton built from ``settings``.

    Wires the shared Agent Registry + identity singletons (from the agents route) and a
    ConnectionService (in-memory fallback when the table name is empty). The repo is
    generated directly from a GitHub template repo in the connection's org (no enrollment
    gate, no scaffold catalog). The build-only CI repo vars come from settings; empty
    values are dropped by the write client. Tests patch ``_svc`` directly with a fake so
    this never runs live."""
    global _svc
    if _svc is None:
        connection_service = ConnectionService(
            table_name=settings.CONNECTIONS_TABLE_NAME,
            secret_prefix=settings.CONNECTIONS_SECRET_PREFIX,
            region=settings.AWS_REGION,
        )
        repo_vars = {
            "AWS_REGION": settings.AWS_REGION,
            "AWS_ECR_PUSH_ROLE_ARN": settings.PROJECT_ECR_PUSH_ROLE_ARN,
            "ECR_REPOSITORY": settings.PROJECT_ECR_REPOSITORY,
            "PLATFORM_BUILD_DONE_URL": settings.PROJECT_BUILD_DONE_URL,
            # Where the scaffold build.yml `trigger` job POSTs (`${AGP_API_URL}/builds/runtime`).
            # Empty values are dropped by the write client (omitted ⇒ trigger cannot reach AGP).
            "AGP_API_URL": settings.AGP_API_URL,
        }
        # E25/T4: the per-STAGE ecr/region/push-role values come from the tenant's stage config,
        # so ProjectService needs a TenantService to resolve them. (E28B/T7: they are written as
        # REPOSITORY-level CI vars — there are no GitHub Environments any more. See
        # ``ProjectService.set_repo_vars`` for why that resolves and must not be reverted.)
        from services.tenant_service import TenantService

        tenant_service = TenantService(
            table_name=settings.TENANTS_TABLE_NAME, region=settings.AWS_REGION
        )
        # E27/T8: the governed promote action deploys through the EXISTING
        # RuntimeBuildService. Reuse the builds route's ONE lazy singleton rather than
        # constructing a second one, so AGP-triggered prod promotions and OIDC-triggered dev
        # builds share the same codebuild/secrets clients and configuration. Imported lazily
        # to avoid an import cycle (builds.py resolves ProjectService from this module).
        from api.routes.builds import get_build_service

        _svc = ProjectService(
            table_name=settings.PROJECTS_TABLE_NAME,
            registry=get_agent_registry(),
            identity=get_identity_service(),
            connection_service=connection_service,
            tenant_service=tenant_service,
            repo_vars=repo_vars,
            region=settings.AWS_REGION,
            ecr_image_service=EcrImageService(
                repository=settings.PROJECT_ECR_REPOSITORY, region=settings.AWS_REGION
            ),
            runtime_build_service=get_build_service(),
            # Only the platform FALLBACK — the build service prefers the tenant stage's own
            # ecr_repo_uri (same value the build-only CI repo var above carries).
            ecr_repository=settings.PROJECT_ECR_REPOSITORY,
            # E26/T7: the shared Langfuse provisioner singleton (from the agents route) so
            # the delete cascade tears down the agent's Langfuse project + SM secret. Its
            # C2 delete_agent_project is idempotent/best-effort (already-gone == success).
            langfuse_provisioning=get_langfuse_service(),
            runtime_state_bucket=settings.RUNTIME_MODULE_BUCKET,
            # E28B/T3: materialize pushes the template's BYTES itself (one commit, `[skip ci]`),
            # so the service needs the on-disk scaffold dir. Same wiring the template rollout
            # path already uses (`api/routes/connections.py`), from the same setting.
            #
            # E28C/T4 (D-C2): the dir is now the FALLBACK. The catalog below is what makes a
            # template record dereferenceable — materialize reads the named template's REPO at
            # use-time and only falls back to disk for a record with no structural source.
            agent_templates_dir=settings.AGENT_TEMPLATES_DIR,
            # The SAME `template` partition of the SAME projects table the templates surface
            # reads (`github_templates.py` / `connections.py` build one from these two settings).
            # A pointer store, not a content store — materialize dereferences the pointer.
            template_registry=TemplateRegistry(
                table_name=settings.PROJECTS_TABLE_NAME,
                region=settings.AWS_REGION,
            ),
        )
    return _svc


@router.post("", response_model=Project, status_code=201)
async def create_project(
    body: ProjectCreate,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    # An ASYNC handler is fine here: create_project only persists an empty container (no
    # asyncio.run, no blocking orchestration). add_repo is the one that drives the async
    # provision_identity via asyncio.run, so THAT handler is sync (threadpool) — see below.
    #
    # Multi-tenancy (E24/T6) — tenant_id must exist, then (for a non-global caller)
    # must be one of the caller's own memberships. No resource exists yet, so 403
    # (not 404) is correct here — unlike the visibility-gated routes below. Mirrors
    # agents.create_agent (T5).
    from api.routes.tenants import get_tenant_service
    from services.tenant_service import TenantError

    try:
        get_tenant_service().get(body.tenant_id)
    except TenantError:
        raise HTTPException(status_code=400, detail="unknown tenant")
    if not ctx.is_global and body.tenant_id not in ctx.tenant_ids:
        raise HTTPException(status_code=403, detail="tenant not permitted")

    svc = get_project_service()
    project = svc.create_project(
        name=body.name,
        connection_id=body.connection_id,
        tenant_id=body.tenant_id,
        description=body.description,
        created_by=principal.email or principal.oid,
        # NO trunk is forwarded (E36/T15, item 24 option B): the create API no longer carries one,
        # and `Project.trunk_branch` supplies the only value the agent template's workflow can
        # build. A `trunk_branch` a stale client still sends is dropped by ProjectCreate.
    )
    _grant_creator_owner(project.id, principal)
    return project


def _grant_creator_owner(project_id: str, principal: Principal) -> None:
    """Bootstrap the creator as the new project's first OWNER (E27/T3).

    Keyed on ``principal.oid``, NOT ``created_by``: ``created_by`` is an email, and a role
    row's ``principal_id`` must be an Entra object id for the resolver to match it against
    the caller's oid ∪ group ids. A dev-auth principal has ``oid=None`` and therefore no
    joinable identity — skip and log rather than fail the create.

    A store failure is LOGGED, never propagated: the project is already persisted by the
    time this runs, so raising would 500 a successful create and orphan the container. An
    ungoverned project (zero role rows) is the safe outcome — the gate's zero-rows state
    is fail-closed for non-admins, and an admin can still grant the role back.
    """
    if not principal.oid:
        logger.info(
            "Skipping creator-OWNER grant for project %s: no Entra oid on the principal "
            "(dev-auth); the project starts with no role rows.",
            project_id,
        )
        return
    try:
        get_project_role_service().grant(
            project_id,
            ProjectRoleCreate(
                principal_id=principal.oid,
                principal_type="user",
                principal_display=principal.email or principal.oid,
                role=role_name(ProjectRole.OWNER),
            ),
            granted_by=principal.oid,
        )
    except Exception:  # noqa: BLE001 — a grant failure must NEVER fail the create.
        logger.exception("Failed to grant creator-OWNER on project %s", project_id)
        return
    # The resolver caches role rows for up to its TTL — drop it so the creator's brand-new
    # OWNER role is effective on their very next request instead of up to 60s later.
    _invalidate_project_roles()


def _invalidate_project_roles() -> None:
    """Drop the ProjectResolver's cached role rows after a role write so the authz change
    applies to the NEXT request instead of up to one TTL later. Best-effort: a failure here
    only means the change lands a minute late, so it must never fail the write that
    succeeded. Nothing propagates across processes — a second ECS task's cache still
    carries the old rows for up to its own TTL (documented in ``project_resolver``)."""
    from api.routes.users import get_project_resolver

    try:
        get_project_resolver().invalidate()
    except Exception:  # noqa: BLE001 — a cache drop must never fail a successful write.
        logger.exception("Failed to invalidate the project-role cache")


@router.get("", response_model=List[Project])
async def list_projects(
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    svc = get_project_service()
    # Tenant post-filter (E24/T6) — the store has no tenant-aware query, so filtering
    # lands AFTER list_projects() returns (mirrors agents.list_agents).
    projects = [p for p in svc.list_projects() if visible(ctx, p.tenant_id)]
    # Project-role post-filter (E27/T4). A LIST filters rather than 403s — the caller asked
    # "what may I see?", not "may I see this?". ONE whole-partition read builds the governed
    # set for the whole page (never one read per project); an admin's may() short-circuits
    # True, so they never pay for it at all.
    if pctx.is_global:
        return projects
    # ONE read serves both inputs, and the caller's own roles are re-folded from THOSE rows so
    # this list cannot disagree with the detail page's gate about a just-granted project.
    rows = _governed_rows()
    pctx = _list_pctx(pctx, principal, rows)
    governed = _governed_project_ids(rows)
    return [
        p
        for p in projects
        if _may_project(pctx, p.id, ProjectRole.VIEWER, has_rows=p.id in governed)
    ]


@router.get("/{id}", response_model=ProjectDetail)
async def get_project(
    id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    detail = _load_visible_project(id, ctx)
    # The gate hands back the context it DECIDED on — the cached one when that already granted
    # VIEWER, otherwise the FRESH single-project re-read it fell back to. The hints below are
    # derived from THAT, so the affordances the UI renders can never contradict the gate that
    # just ran, and a creator whose OWNER row was written on another ECS task keeps their
    # buttons instead of losing them for a TTL (the D18 failure mode, via the cache).
    pctx = _require_project_role_or_ungoverned(
        pctx, id, ProjectRole.VIEWER, principal=principal
    )
    # UI HINTS ONLY (E27/T11) — see ``ProjectDetail`` / ``_effective_role_hint``. The gate
    # above already ran and is the authority; these two fields just let the frontend render
    # the same answer instead of guessing (it cannot evaluate an Entra GROUP grant).
    #
    # ``ungoverned`` costs NO extra store read: control only reaches here if the VIEWER gate
    # passed, so a non-global caller holding no role can ONLY have passed through the §3
    # fallback — which means ``_project_has_role_rows`` already returned False for this id.
    # (A caller who holds a role implies the project HAS rows, hence is governed; a global
    # admin short-circuits, so the bit is unknown for them and reported False — they never
    # need the fallback.)
    return detail.model_copy(
        update={
            "effective_role": _effective_role_hint(pctx, id),
            "ungoverned": not pctx.is_global and pctx.roles.get(id) is None,
        }
    )


# ===========================================================================
# E27/T3 — per-project role CRUD. Gated on BOTH tenant visibility (the existing
# `_load_visible_project`, which runs FIRST so a foreign tenant 404s before any role
# logic) AND the caller's own per-project role via `may()`. Reading the roster needs
# VIEWER; changing it needs OWNER. Every write drops the resolver cache so the authz
# change is effective on the next request, not up to one TTL later.
# ===========================================================================

@router.get("/{id}/roles", response_model=List[ProjectRoleRead])
async def list_project_roles(
    id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """The project's role roster. VIEWER-gated: anyone with any role on the project may see
    who else holds one (it is governance metadata, not a credential).

    ``list_for_project_strict``, not the degrading ``list_for_project``: the console USES
    this list's emptiness as a decision, so "empty" and "unreadable" must be
    distinguishable ON THE WIRE (E27/T11). The degrading read answers a store fault with
    200 + ``[]``, and the Access tab's Grant is only an ADD because the roster tells it who
    already holds a role — a falsely-empty roster would let "grant Viewer" reach the
    backend's upsert and silently DOWNGRADE an existing owner. A false 200 is therefore
    worse than a 503 here."""
    _load_visible_project(id, ctx)
    _require_project_role(pctx, id, ProjectRole.VIEWER, principal=principal)
    try:
        return get_project_role_service().list_for_project_strict(id)
    except ProjectRoleError as err:
        # FIXED detail per .kind — never str(err). "validation" is the store's '#'-in-id
        # rejection (the composite sk is only injective without it) → a malformed id is a 400.
        if err.kind == "validation":
            raise HTTPException(status_code=400, detail="invalid project role")
        # The role partition is unreadable, so the roster is UNKNOWN — the SAME 503 + literal
        # T3 already pins for an unverifiable-ownership read on the write paths. Retryable,
        # and the FE maps it to a sentence via `roleActionMessage`.
        if err.kind == "ownership_unverified":
            raise HTTPException(
                status_code=503, detail="could not verify project ownership"
            )
        raise


@router.post("/{id}/roles", response_model=ProjectRoleRead, status_code=201)
async def grant_project_role(
    id: str,
    body: ProjectRoleCreate,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Grant (or re-grant — the store upserts) one principal's role on the project.
    OWNER-gated: handing out authority over a project is an owner's act."""
    _load_visible_project(id, ctx)
    _require_project_role(pctx, id, ProjectRole.OWNER, principal=principal)
    try:
        # granted_by comes from the validated principal, NEVER the body.
        record = get_project_role_service().grant(
            id, body, granted_by=principal.oid or principal.email or ""
        )
    except ProjectRoleError as err:
        # FIXED detail per .kind — never str(err). ``grant`` is an upsert, so this verb can
        # ALSO downgrade the last owner — same guard, same 409, same literal as PUT/DELETE.
        if err.kind == "last_owner":
            raise HTTPException(
                status_code=409, detail="project must keep at least one owner"
            )
        # The store could not READ the role partition, so it refused rather than risk
        # stranding the project with zero owners — a transient store fault, not the
        # caller's fault: 503, retryable.
        if err.kind == "ownership_unverified":
            raise HTTPException(
                status_code=503, detail="could not verify project ownership"
            )
        if err.kind == "validation":
            raise HTTPException(status_code=400, detail="invalid project role")
        raise
    _invalidate_project_roles()
    return record


@router.put("/{id}/roles/{principal_id}", response_model=ProjectRoleRead)
async def update_project_role(
    id: str,
    principal_id: str,
    body: ProjectRoleCreate,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Change one principal's role. The store's ``grant`` is an UPSERT keyed on
    (project, principal), so a role change IS a re-grant — the PATH ``principal_id`` is
    authoritative and any body ``principal_id`` is ignored, so this can never
    accidentally target a different principal than the URL names.

    Because it IS a re-grant, the store's last-owner guard applies here too: downgrading the
    only remaining owner would strand the project with zero owners → 409, same literal DELETE
    uses."""
    _load_visible_project(id, ctx)
    _require_project_role(pctx, id, ProjectRole.OWNER, principal=principal)
    try:
        record = get_project_role_service().grant(
            id,
            body.model_copy(update={"principal_id": principal_id}),
            granted_by=principal.oid or principal.email or "",
        )
    except ProjectRoleError as err:
        # FIXED detail per .kind — never str(err).
        if err.kind == "last_owner":
            raise HTTPException(
                status_code=409, detail="project must keep at least one owner"
            )
        # Same as POST: the guard's partition read failed, so the downgrade was refused
        # rather than risk a zero-owner project. Transient → 503.
        if err.kind == "ownership_unverified":
            raise HTTPException(
                status_code=503, detail="could not verify project ownership"
            )
        if err.kind == "validation":
            raise HTTPException(status_code=400, detail="invalid project role")
        if err.kind == "not_found":
            raise HTTPException(status_code=404, detail="project role not found")
        raise
    _invalidate_project_roles()
    return record


@router.delete("/{id}/roles/{principal_id}", status_code=204)
async def revoke_project_role(
    id: str,
    principal_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Revoke one principal's role. The store refuses to strip a project of its LAST owner
    (an ownerless project is unadministerable) → 409."""
    _load_visible_project(id, ctx)
    _require_project_role(pctx, id, ProjectRole.OWNER, principal=principal)
    try:
        get_project_role_service().revoke(id, principal_id)
    except ProjectRoleError as err:
        # FIXED detail per .kind — never str(err).
        if err.kind == "last_owner":
            raise HTTPException(
                status_code=409, detail="project must keep at least one owner"
            )
        if err.kind == "validation":
            raise HTTPException(status_code=400, detail="invalid project role")
        if err.kind == "not_found":
            raise HTTPException(status_code=404, detail="project role not found")
        raise
    _invalidate_project_roles()
    return Response(status_code=204)


@router.post("/{id}/repos", response_model=Repository, status_code=202)
def add_repo(
    id: str,
    body: RepositoryCreate,
    background_tasks: BackgroundTasks,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    # A SYNC handler so FastAPI dispatches it to a threadpool: add_repo does the fast sync
    # work (validate → pre-register agent → persist a PENDING record) and returns 202, then
    # the scheduled run_materialize BackgroundTask drives the side-effecting steps
    # (including the async provision_identity via asyncio.run). BackgroundTasks for a sync
    # function also run in the threadpool, which has no running loop — so asyncio.run there
    # is safe (mirrors agents.reprovision's set-pending → persist → add_task → 202 shape).
    #
    # Tenant gate (E24/T6): the repo inherits the PARENT project's tenant, so this
    # mutation gates on the project's visibility BEFORE any side effect (no agent
    # pre-registered, no repo materialized) — 404 byte-identical to a truly-missing project.
    #
    # Project-role gate (E27/T4): materializing a repo+agent is a MAINTAINER act. The ctx
    # arrives via Depends (FastAPI resolves the async dependency before dispatching this sync
    # handler to the threadpool), so there is nothing to await here.
    _load_visible_project(id, ctx)
    _require_project_role_or_ungoverned(pctx, id, ProjectRole.MAINTAINER, principal=principal)
    svc = get_project_service()
    try:
        repo = svc.add_repo(
            project_id=id,
            name=body.name,
            template_name=body.template_name,
            agent_config=body.agent_config,
            purpose=body.purpose,
            business_unit=body.business_unit,
            region=body.region,
            data_classification=body.data_classification,
            repo_overrides=body.repo_overrides,
            created_by=principal.email or principal.oid,
            principal=principal,
        )
    except NameTakenError as err:
        # The agent_name is already registered (pre-check in registry.create, BEFORE any
        # side effect — no repo/identity minted). Map to 409 with the safe literal message,
        # mirroring the agents route. Without this, it escapes as a raw 500.
        raise HTTPException(status_code=409, detail=str(err))
    except ValueError as err:
        # agent.config validation (non-strands framework / bad agent_name) — the message is
        # a curated §safe literal (no secret), so surfacing str(err) is allowed here.
        raise HTTPException(status_code=400, detail=str(err))
    except ProjectError as err:
        # add_repo does ONLY sync work, and raises exactly two ProjectError kinds:
        # "not_found" (unknown project) → 404, and "invalid_template_name" (E28C/T4, P-B5) → 422.
        # Materialize failures no longer raise here — they surface in the background
        # run_materialize as a failed record. FIXED detail, never str(err) (which could carry a
        # store/GitHub message, or — for the template name — a resolved container path).
        if err.kind == "not_found":
            raise HTTPException(status_code=404, detail="Project not found")
        if err.kind == "invalid_template_name":
            # 422, not 400: the body is syntactically valid and one field's VALUE is
            # unprocessable. Refused BEFORE the identity mint and the repo creation, so nothing
            # needs cleaning up — which is the whole point of moving the check to the boundary.
            raise HTTPException(status_code=422, detail="invalid template name")
        raise
    # Sync work done + PENDING record persisted → schedule the background steps (they run
    # AFTER this 202 response; run_materialize writes per-step status and never raises).
    background_tasks.add_task(svc.run_materialize, repo.id)
    return repo


@router.get("/{id}/repos/{repo_id}/status", response_model=Repository)
async def repo_status(
    id: str,
    repo_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    # READ of the live per-step materialize timeline (E25C/T3). An ASYNC handler is fine:
    # get_repo is a plain record read (no asyncio.run, no blocking orchestration).
    # Tenant gate (E24/T6): the repo inherits the PARENT project's tenant — a foreign
    # project 404s ("Project not found", byte-identical) BEFORE the record is read.
    # Project-role gate (E27/T4): a repo inherits its PARENT project's role too, and reading
    # the timeline is a VIEWER act.
    _load_visible_project(id, ctx)
    _require_project_role_or_ungoverned(pctx, id, ProjectRole.VIEWER, principal=principal)
    svc = get_project_service()
    repo = svc.get_repo(repo_id)
    if repo is None or repo.project_id != id:
        raise HTTPException(status_code=404, detail="Repository not found")
    return repo


@router.post("/{id}/repos/{repo_id}/retry", response_model=Repository, status_code=202)
def retry_repo(
    id: str,
    repo_id: str,
    background_tasks: BackgroundTasks,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    # A SYNC handler (threadpool) mirroring add_repo: retry_materialize does the fast sync
    # prep (reset failed steps → pending, re-derive + re-stash the materialize inputs, flip
    # cicd_status → provisioning) and returns 202, then the scheduled run_materialize
    # BackgroundTask resumes the steps from the first failure (its done-skip loop skips the
    # already-done steps, including the async provision_identity via asyncio.run).
    # Tenant gate (E24/T6): the repo inherits the PARENT project's tenant, so this mutation
    # gates on the project's visibility BEFORE any state change — 404 byte-identical to a
    # truly-missing project.
    # Project-role gate (E27/T4): a retry re-drives the same 8 materialize steps add_repo
    # does, so it carries the same MAINTAINER threshold. Sync handler → the ctx arrives via
    # Depends, nothing to await.
    _load_visible_project(id, ctx)
    _require_project_role_or_ungoverned(pctx, id, ProjectRole.MAINTAINER, principal=principal)
    svc = get_project_service()
    repo = svc.get_repo(repo_id)
    if repo is None or repo.project_id != id:
        raise HTTPException(status_code=404, detail="Repository not found")
    try:
        repo = svc.retry_materialize(repo_id)
    except ProjectError as err:
        # FIXED detail per .kind — never str(err), which could carry a store/GitHub message.
        # "nothing_to_retry" (409): every step is already done — no-op, and crucially NOT
        # scheduling a pointless run_materialize (nothing would run, and the flip that would
        # strand the repo at "provisioning" is skipped in the service). "not_found" (404)
        # mirrors add_repo — inert today (the get_repo pre-check above catches it) but honest.
        if err.kind == "nothing_to_retry":
            raise HTTPException(status_code=409, detail="Nothing to retry")
        if err.kind == "not_found":
            raise HTTPException(status_code=404, detail="Repository not found")
        raise
    background_tasks.add_task(svc.run_materialize, repo_id)
    return repo


@router.post("/{id}/repos/{repo_id}/promote", response_model=Repository, status_code=202)
def promote_repo(
    id: str,
    repo_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Promote the repo's PROD CANDIDATE to PROD (E27/T8, narrowed by E27A/T7) — the epic's
    headline action, and the AGP-native replacement for the GitHub-specific approve surface.

    The candidate is the image that was registered when a merge to ``main`` landed — i.e. the
    artifact a reviewer approved — NOT "whatever last deployed to dev". E27A narrowed it off
    ``last_dev_image_tag`` precisely because promoting the latest dev artifact could ship code
    nobody reviewed. Nothing merged since the last release ⇒ nothing to promote ⇒ **409**, an
    ordinary state the UI must be able to tell apart from a server fault.

    NO image tag is accepted from the caller. Any body is ignored entirely (the signature
    takes none), and the tag is resolved SERVER-SIDE from ``repo.prod_candidate_image_tag``,
    which only the OIDC-authenticated candidate route writes — so there is no input through
    which an arbitrary image could be pushed to production. ``promoted_by`` likewise comes
    from the validated principal, never a body.

    A SYNC handler (threadpool) like the other mutating repo routes: the service call does
    blocking boto3 work (a Secrets Manager write + CodeBuild StartBuild inside the runtime
    build service), which must not run on the uvicorn event loop.

    Tenant gate (E24/T6): the repo inherits the PARENT project's tenant, so this gates on the
    project's visibility BEFORE any deploy — a foreign project 404s byte-identically to a
    truly-missing one.

    Project-role gate (E27/T8): **OWNER, via the STRICT gate** — deliberately NOT
    ``_require_project_role_or_ungoverned``. Deploying to production is the highest-consequence
    verb in the epic, so it must never ride the design-§3 ungoverned-project fallback: a
    project with no role rows has nobody accountable for a prod release, and "not yet governed"
    must not mean "anyone in the tenant may ship to prod". An OWNER row is required even for a
    pre-migration project (for NON-GLOBAL callers — a global admin passes ``may()``
    unconditionally, as on every other project-gated route). Refused BEFORE the build starts.

    A second promote while one is in flight is refused with 409 (the service bounds that guard
    so a stuck record stays recoverable — see :meth:`ProjectService.promote_repo`).
    """
    _load_visible_project(id, ctx)
    _require_project_role(pctx, id, ProjectRole.OWNER, principal=principal)
    svc = get_project_service()
    try:
        # promoted_by prefers the Entra oid (the stable, joinable identity a role row is
        # keyed by) and falls back to the email only for a dev-auth principal with no oid.
        return svc.promote_repo(id, repo_id, promoted_by=principal.oid or principal.email or "")
    except ProjectError as err:
        # FIXED detail per .kind — never str(err), which could carry a store/CodeBuild message.
        # "no_prod_candidate" (E27A/T7) is the NORMAL empty state — nothing has merged to
        # `main` since the last promotion — so it must read as a 409 the FE can render as
        # "nothing to promote", not as the 500 an unmapped kind would leak.
        if err.kind == "no_prod_candidate":
            raise HTTPException(status_code=409, detail="no prod candidate to promote")
        # Retained for pre-E27A records (the service no longer raises it) so stored
        # expectations and their tests behave unchanged.
        if err.kind == "no_dev_build":
            raise HTTPException(status_code=409, detail="no dev deployment to promote")
        if err.kind == "promote_in_flight":
            raise HTTPException(status_code=409, detail="a promotion is already in flight")
        if err.kind == "promote_failed":
            raise HTTPException(status_code=502, detail="failed to start the promotion build")
        if err.kind == "not_found":
            raise HTTPException(status_code=404, detail="Repository not found")
        raise


@router.post("/{id}/repos/{repo_id}/rollback", response_model=Repository, status_code=202)
def rollback_repo(
    id: str,
    repo_id: str,
    body: RepoRollbackRequest,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Redeploy a PREVIOUSLY-SUCCEEDED image tag (E28/T4, contract C2 — D5+D7).

    Promote's counterpart, and the verb that makes the append-only ``Deployment`` history
    actionable rather than merely readable.

    **Unlike promote, this route DOES take an image tag** — a rollback has to name a target — and
    that is exactly why the service validates it rather than trusting it: ``image_tag`` is
    accepted only if it has a ``succeeded`` deployment row **for this repo, in this stage**. An
    unvalidated tag here would be a deploy-anything-to-production primitive, since the tenant ECR
    registry is shared across agents and an arbitrary tag can name a real, pullable image. A
    rejected tag is a **409** with a FIXED detail that never echoes the tag back — an ordinary
    state the UI renders, not a server fault.

    ``stage`` is free-form (D8) and defaults to ``"prod"``; it needs no allowlist because an
    unknown stage has no succeeded rows and is refused by the same check. The
    ``("dev","prod")`` allowlist on the OIDC ``/builds/runtime`` route is a different trust
    boundary and is untouched.

    A SYNC handler (threadpool) for the same reason as promote: the service does blocking boto3
    work (a Secrets Manager write + CodeBuild StartBuild) that must not run on the event loop.

    Tenant gate (E24/T6): the repo inherits the PARENT project's tenant, so a foreign project
    404s byte-identically to a truly-missing one BEFORE any deploy.

    Project-role gate: **OWNER, via the STRICT gate** — the SAME helper and the SAME threshold as
    promote, deliberately not a second gate of its own. A rollback is a write to PRODUCTION, so
    anything looser here would be a bypass of promote's gate (whichever of a differently-behaving
    pair is looser becomes the real gate — the rationale at ``api/routes/agents.py:209-213``). It
    likewise does NOT ride the design-§3 ungoverned fallback: a project with nobody accountable
    must not be rollbackable by a mere tenant member.

    A rollback while a delivery is in flight is refused with 409 — the service reuses promote's
    own bounded ``promoting`` guard, so the two verbs are serialized in either order and cannot
    race the same stage-scoped Terraform state key.
    """
    _load_visible_project(id, ctx)
    _require_project_role(pctx, id, ProjectRole.OWNER, principal=principal)
    svc = get_project_service()
    try:
        # rolled_back_by prefers the Entra oid (the stable identity a role row is keyed by) and
        # falls back to the email only for a dev-auth principal with no oid — identical to
        # promote's `promoted_by`, and never a body value.
        return svc.rollback_repo(
            id,
            repo_id,
            image_tag=body.image_tag,
            stage=body.stage,
            rolled_back_by=principal.oid or principal.email or "",
        )
    except ProjectError as err:
        # FIXED detail per .kind — never str(err), and never the rejected tag (which would echo
        # caller input back into a log-visible response).
        if err.kind == "unknown_rollback_target":
            raise HTTPException(
                status_code=409, detail="no such succeeded deployment to roll back to"
            )
        if err.kind == "promote_in_flight":
            raise HTTPException(status_code=409, detail="a promotion is already in flight")
        if err.kind == "rollback_failed":
            raise HTTPException(status_code=502, detail="failed to start the rollback build")
        if err.kind == "not_found":
            raise HTTPException(status_code=404, detail="Repository not found")
        raise


@router.get("/{id}/repos/{repo_id}/delete-preview", response_model=RepoDeletePreview)
async def delete_preview(
    id: str,
    repo_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    # READ-ONLY reachability pre-check (E23/T11) so the delete modal only offers artifacts
    # that still exist. Deletes NOTHING.
    #
    # DISPATCHED OFF THE EVENT LOOP (E36/T8 fix 1). This used to call the service directly on
    # the grounds that "the service only probes — the runtime/image/github probes are quick
    # reads". T8 invalidated that: `_probe_runtime` now resolves a per-STAGE client, which
    # costs a DynamoDB tenant read, an sts:AssumeRole ROUND TRIP and two botocore service-model
    # loads per stage — all synchronous. On a black-holed STS endpoint botocore also retries
    # with backoff before the seam converts the error, and every stall is the whole uvicorn
    # worker's, not just this caller's. Same `anyio.to_thread.run_sync` idiom as
    # `agents.py`'s `/runtime` probe, and the same reason the sibling DELETE handler below is
    # sync-in-a-threadpool.
    # Tenant gate (E24 merge): the repo inherits the PARENT project's tenant — a foreign
    # project 404s before any probe runs.
    # Project-role gate (E27/T4): OWNER, matching the DELETE it precedes. The preview is
    # read-only, but it is the delete modal's own surface — a MAINTAINER who can never
    # complete the cascade has no reason to enumerate its artifacts, and pinning both at the
    # same threshold means the UI can gate the button on ONE answer.
    _load_visible_project(id, ctx)
    _require_project_role_or_ungoverned(pctx, id, ProjectRole.OWNER, principal=principal)
    svc = get_project_service()
    try:
        return await anyio.to_thread.run_sync(
            functools.partial(svc.preview_delete, project_id=id, repo_id=repo_id)
        )
    except ProjectError as err:
        # FIXED detail per .kind — never str(err), which could carry a store/GitHub message.
        if err.kind == "not_found":
            raise HTTPException(status_code=404, detail="Repository not found")
        raise HTTPException(status_code=502, detail="Failed to preview the repository delete")


@router.delete("/{id}/repos/{repo_id}", response_model=RepoDeleteResult)
def delete_repo(
    id: str,
    repo_id: str,
    selection: RepoDeleteSelection = RepoDeleteSelection(),
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    # A SYNC handler (threadpool) for the same reason as add_repo: the teardown cascade
    # drives the async delete_identity via asyncio.run inside the service, which must NOT
    # run on the uvicorn event loop. Partial failure is DATA in the result envelope
    # (per-item outcome), not an HTTP error — so a 200 is returned even when steps failed.
    # Tenant gate (E24 merge): mutation on a repo gates through the PARENT project's
    # visibility BEFORE any teardown side effect — foreign project → byte-identical 404.
    #
    # Project-role gate (E27/T4, design §1): OWNER — the highest bar in the epic and the one
    # this whole task exists for. This is the E23 5-item cascade: a live AgentCore runtime,
    # the Entra app registration, the ECR images, the runtime TF state and the registry
    # record. It is IRREVERSIBLE and it is NOT a repo-provider verb (GitHub authorizes its
    # own repo delete; nothing there governs the runtime or the identity), so an AGP gate here
    # is additive, not a duplicate. Refused BEFORE the first teardown step runs. The §3
    # fallback deliberately does NOT reach OWNER, so an ungoverned project cannot be
    # cascade-deleted by a mere tenant member. Sync handler → nothing to await.
    _load_visible_project(id, ctx)
    _require_project_role_or_ungoverned(pctx, id, ProjectRole.OWNER, principal=principal)
    svc = get_project_service()
    try:
        return svc.delete_repo(project_id=id, repo_id=repo_id, selection=selection)
    except ProjectError as err:
        # FIXED detail per .kind — never str(err), which could carry a store/GitHub message.
        if err.kind == "not_found":
            raise HTTPException(status_code=404, detail="Repository not found")
        raise HTTPException(status_code=502, detail="Failed to delete the repository")


@router.delete("/{id}", status_code=204)
async def delete_project(
    id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    # An ASYNC handler is fine: delete_project only removes an empty container (no
    # asyncio.run, no blocking orchestration) — it refuses when repos still exist.
    # Tenant gate (E24 merge): a foreign tenant's project 404s before any delete.
    # Project-role gate (E27/T4): OWNER — destroying the governance container itself (and its
    # role rows with it) is the owner's act, and it must not ride the §3 fallback.
    _load_visible_project(id, ctx)
    _require_project_role_or_ungoverned(pctx, id, ProjectRole.OWNER, principal=principal)
    svc = get_project_service()
    try:
        svc.delete_project(id)
    except ProjectError as err:
        # FIXED detail per .kind — never str(err).
        if err.kind == "not_found":
            raise HTTPException(status_code=404, detail="Project not found")
        if err.kind == "has_repositories":
            raise HTTPException(
                status_code=409, detail="Project has repositories; delete them first"
            )
        raise HTTPException(status_code=502, detail="Failed to delete the project")
    _cleanup_project_roles(id)
    return Response(status_code=204)


def _cleanup_project_roles(project_id: str) -> None:
    """Delete the project's role rows AFTER the project itself is gone (E27 fix pass).

    Ordered deliberately: the project record is removed FIRST, so a failure here can only ever
    leave orphan rows — never a live project whose owners were stripped, which would be an
    unadministerable project (exactly what the last-owner guard exists to prevent).

    Best-effort, E23 cascade idiom: the project is already deleted by the time this runs, so
    raising would 500 a delete that SUCCEEDED and tell the caller to retry something that
    cannot be retried. Log and continue instead — the residue is unreferenced rows, and
    ``revoke_all`` is idempotent so a later delete of the same id cleans them up.

    Also invalidates the resolver cache: the deleted project's grants must stop being folded
    into a caller's context on this process's very next request."""
    try:
        removed = get_project_role_service().revoke_all(project_id)
    except Exception:  # noqa: BLE001 — a cleanup failure must NEVER fail a completed delete.
        logger.exception(
            "Failed to delete the role rows for project %s; the project is gone but its "
            "role rows remain orphaned",
            project_id,
        )
        return
    if removed:
        logger.info("Deleted %d role row(s) for project %s", removed, project_id)
    _invalidate_project_roles()


@repositories_router.get("", response_model=List[Repository])
async def list_repositories(
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    svc = get_project_service()
    repos = svc.list_repositories()
    if not ctx.is_global:
        # Repositories carry NO tenant field (E24/T6) — visibility is inherited through
        # each repo's PARENT project. A repo whose project is not visible (or gone) is
        # filtered out, same 404-shaped absence semantics as the detail routes.
        project_tenant = {p.id: p.tenant_id for p in svc.list_projects()}
        repos = [
            r
            for r in repos
            if r.project_id in project_tenant and visible(ctx, project_tenant[r.project_id])
        ]
    # Project-role post-filter (E27/T4) — inherited through the parent project exactly like
    # tenant visibility, and read straight off ``r.project_id`` (no project lookup needed).
    # A LIST filters rather than 403s; ONE whole-partition read serves the whole page.
    if pctx.is_global:
        return repos
    rows = _governed_rows()
    pctx = _list_pctx(pctx, principal, rows)
    governed = _governed_project_ids(rows)
    return [
        r
        for r in repos
        if _may_project(
            pctx, r.project_id, ProjectRole.VIEWER, has_rows=r.project_id in governed
        )
    ]


# =========================================================================== #
# Pull requests on a repository (E28/T14 — design D14+D15, contract C2)
#
# THE FOUR ROUTES ``client.ts``'s ``pullRequestsApi`` (client.ts:1076-1094) ALREADY CALLS.
# They 404'd until this task mounted them, which is why no UI called them: T11 held the
# exclusive claim on that file and declared the calls there, so the PATHS AND THE RESPONSE
# SHAPE BELOW ARE THE PIN, not this module's choice. A path renamed here silently 404s the
# frontend, and a field renamed here silently reads as ``undefined`` — a test pins both.
#
# REPO-SCOPED, not agent-scoped (C2). Design D2 puts a fact on the agent whenever the agent
# has an analogue for it; a pull request has none. It belongs to a REPOSITORY, and routing it
# through an agent would invent a relationship the provider does not have.
#
# ---------------------------------------------------------------------------
# THE CREDENTIAL: THE LINKED HUMAN'S TOKEN, AND NEVER AGP'S OWN (D15)
#
# Every verb here runs under the E27B per-user token — ``GitHubUserLinkService`` —
# resolved from the VALIDATED Entra principal. AGP also holds an org-scoped App installation
# token, one ``ConnectionService.get_bearer_token`` away, and using it here would be a
# security defect rather than a convenience: AGP's App is never the author of anybody's pull
# request, so an App-token path would sail through the self-approval refusal below and let the
# platform approve a human's own PR on their behalf. That is the reviewer independence the
# whole gate exists to provide.
#
# So a human with no link, or a revoked one, is REFUSED (409) and nothing is retried. The
# service half of that invariant is structural — ``github_pr_service`` imports no credential
# seam at all and takes its token as a parameter — and the route half is asserted by call
# count on the connection service. Two halves, because either alone can be undone.
#
# ---------------------------------------------------------------------------
# THE GATE: THE ROUTER'S EXISTING ONE, VERBATIM
#
# All four carry the SAME four dependencies in the SAME order as ``GET /repositories``, the
# sibling already on this router — then the shared ``_load_visible_repo`` — because two reads
# of one resource must not carry different guards: whichever is looser IS the gate (D3; the
# rationale is spelled out at ``api/routes/agents.py:209-213``, and ``/runtime`` ↔
# ``/deployments`` is the house precedent with a test asserting the pair). A test compares the
# dependency lists mechanically rather than by eye.
#
# On top of that, the PROJECT-ROLE threshold: VIEWER to read, MAINTAINER to open/approve/merge
# — the same level ``retry`` carries, and deliberately NOT promote's strict OWNER. A merge to
# the default branch does not deploy anything: it registers a prod CANDIDATE, which still
# needs an OWNER's promote through the existing governed route. So this cannot be a way around
# the prod gate, and requiring OWNER here would instead mean nobody but an owner could open a
# pull request.
#
# ---------------------------------------------------------------------------
# WHY THIS IS NOT SHADOW GOVERNANCE
#
# Approving a PULL REQUEST is a repo-provider act on a provider object, explicitly in scope
# (D15). Approving an AGENT is governance and is forbidden on this surface. The two are not
# blurred here: nothing below touches a lifecycle state, a grant, or a role.
# =========================================================================== #


def get_pr_service() -> GitHubPrService:
    """Lazy :class:`GitHubPrService` singleton.

    Nothing to configure: the service holds no credential and no store — it takes a token per
    call — so this is only here to share one pooled ``httpx.Client`` across requests. Tests
    patch this accessor (rather than a module global) because the handlers resolve it lazily."""
    global _pr_svc
    if _pr_svc is None:
        _pr_svc = GitHubPrService()
    return _pr_svc


# ``GitHubPrError.kind`` → (status, FIXED detail). Never ``str(err)`` — the repo-wide rule —
# so no provider message, token or AWS account id can reach an HTTP body. Every kind the
# service can raise appears here, and a test asserts that exhaustively: an UNMAPPED kind would
# escape as an unhandled 500, and on the list read a 500 renders a BROKEN pull-requests tab,
# which is worse than the hidden tab a missing grant is supposed to produce.
_PR_ERROR = {
    # THE capability refusal (A3). The org's App is not granted ``pull_requests`` — a MANUAL
    # per-org grant that GitHub does not retro-apply to an already-created App, so this is an
    # ordinary state for any org onboarded before this feature. The frontend resolves this ONE
    # literal to a HIDDEN tab, which is why it must be a mapped 403 and never a 5xx.
    "capability_missing": (403, "pull requests are not enabled for this organization"),
    "not_found": (404, "pull request not found"),
    # D15's refusal, stated so the UI can render the reason rather than "failed".
    "self_approval": (409, "you cannot approve your own pull request"),
    "not_approvable": (409, "this pull request cannot be approved"),
    "not_mergeable": (409, "this pull request cannot be merged yet"),
    "conflict": (409, "GitHub declined the request for this pull request"),
    # Nothing to open a pull request FOR — the head branch is not ahead of the base. A
    # client-side refusal like its 409 neighbours, NOT a 5xx: GitHub answered fine, it simply
    # had no commits to compare. Its own literal because ``conflict``'s copy named two causes
    # this is not, and an operator read those as facts.
    "no_commits": (409, "there are no commits between these branches"),
    # The ONE 5xx, and it is honest: a real GitHub outage is not a missing grant, and dressing
    # it as a client-side state would tell the operator the tab is unavailable when GitHub is
    # simply down. 502 is the house status for an upstream failure (``github_link.py``).
    "provider_error": (502, "GitHub request failed"),
}


def _raise_pr_error(err: GitHubPrError):
    status, detail = _PR_ERROR.get(err.kind, _PR_ERROR["provider_error"])
    raise HTTPException(status_code=status, detail=detail)


def _load_visible_repo(repo_id: str, ctx: TenantContext) -> "tuple[Repository, ProjectDetail]":
    """Load a repository AND its parent project, gated on tenant visibility.

    ONE helper all four PR routes call, so the absence semantics cannot differ between them.
    Repositories carry NO tenant field (E24/T6) — visibility is inherited through the parent
    project — so this resolves the parent and gates on it.

    A missing repo, a repo whose project is gone, and a repo in a FOREIGN tenant all raise the
    SAME 404 literal, byte-identically: a 403 would confirm that a repository the caller may
    not see exists at all. Returns both records because every caller needs the repo's NAME and
    the project's CONNECTION to address the provider."""
    svc = get_project_service()
    repo = svc.get_repo(repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="Repository not found")
    detail = svc.get_project(repo.project_id)
    if detail is None or not visible(ctx, detail.project.tenant_id):
        raise HTTPException(status_code=404, detail="Repository not found")
    return repo, detail


def _provider_coordinates(detail: ProjectDetail) -> "tuple[str, Optional[str]]":
    """The org and API base for the repo's connection — resolved SERVER-SIDE, never accepted.

    The org comes from the project's connection record because accepting it from a caller would
    make these routes a write-to-any-repo primitive **under a human's own GitHub token**, which
    is a far worse primitive than the same bug under an App token: the human's account carries
    their personal authority across every org they belong to.

    A vanished or unreadable connection is a 502 with the same fixed literal as any other
    provider failure — the repository record is fine, but AGP cannot address the provider."""
    from api.routes.connections import get_connection_service

    try:
        connection = get_connection_service().get_connection(detail.project.connection_id)
    except ConnectionError:
        logger.warning(
            "[pr] could not resolve the connection for project %s", detail.project.id
        )
        raise HTTPException(status_code=502, detail="GitHub request failed")
    return connection.org, connection.base_url


def _user_token(principal: Principal, connection_id: str) -> str:
    """The linked human's GitHub token for this org — the ONLY credential these routes use.

    **There is deliberately no fallback.** A human with no link, or a revoked one, cannot act
    as themselves, so AGP does nothing rather than acting as ITSELF: the App-token path would
    let the platform approve a human's own pull request (its App is never the author), which
    silently defeats D15's refusal. So this raises, and no other credential is tried.

    409 rather than 401/403, for two reasons. The SPA's response interceptor turns a 401 into
    ``removeItem('auth_token') + reload()`` — logging the human out over a missing GitHub link
    (``github_link.py``'s "NO ROUTE HERE RETURNS 401" rule) — and 403 is already this router's
    authorization refusal, which this is not: the caller's AGP standing is fine, they simply
    have not connected a GitHub account yet. 409 is a state with a remedy the UI can offer."""
    from api.routes.github_link import get_github_link_service
    from services.github_user_link import GitHubLinkError

    oid = (principal.oid or "").strip()
    if not oid:
        # A dev-auth principal has no oid, so no link can be attributed to it. Refused for the
        # same reason a missing link is: AGP cannot establish whose account it would act as.
        raise HTTPException(
            status_code=409,
            detail="connect your GitHub account to act on pull requests",
        )
    try:
        return get_github_link_service().get_user_bearer_token(oid, connection_id)
    except GitHubLinkError as err:
        logger.info("[pr] no usable GitHub user link (%s) — refusing, never falling back", err.kind)
        raise HTTPException(
            status_code=409,
            detail="connect your GitHub account to act on pull requests",
        )


def _viewer_login(principal: Principal, connection_id: str) -> str:
    """The GitHub LOGIN the caller is acting as, from their link row for THIS connection.

    This is what decides ``can_approve``, so it must be a PROVIDER currency: a GitHub login is
    proven by GitHub, an Entra oid by Entra, and AGP holds no mapping between them (E27A §6).
    Deriving it from the Entra principal would compare two different currencies and call the
    result a self-approval check.

    SCOPED TO THIS CONNECTION. A human may link several orgs; reading another org's login here
    would compare the caller against a stranger, and could either offer a self-approval or
    suppress a legitimate one.

    Degrades to EMPTY rather than raising, and empty means the service refuses the approve
    (fail-closed). The token has already been resolved by this point, so the human's authority
    is established — only the label is missing, and a lost label must not fail a read."""
    from api.routes.github_link import get_github_link_service
    from services.github_user_link import GitHubLinkError

    oid = (principal.oid or "").strip()
    try:
        for link in get_github_link_service().list_for_principal(oid):
            if link.connection_id == connection_id:
                return link.github_login or ""
    except GitHubLinkError:
        logger.warning("[pr] could not read the link label; approvals will be refused")
    return ""


def _pr_context(
    repo_id: str,
    principal: Principal,
    ctx: TenantContext,
    pctx: ProjectContext,
    required: ProjectRole,
) -> "tuple[str, str, str, str, Optional[str]]":
    """Everything a PR verb needs, resolved in the ONE order that is safe.

    ``(org, repo_name, token, viewer_login, base_url)``.

    THE ORDER IS THE CONTRACT, and each step guards the next:

      1. **Tenant visibility** (``_load_visible_repo``) — a foreign tenant 404s byte-identically
         BEFORE anything else happens, so a 403 below can only ever concern a repository the
         caller's own tenant already exposes.
      2. **Project role** — refused BEFORE any credential is resolved and before GitHub is
         reached. A 403 that has already opened a pull request is not a refusal.
      3. **Provider coordinates**, from the records.
      4. **The linked human's token** — last, because it is the only step with a side effect
         worth avoiding (it can spend a refresh token).

    ``required`` is VIEWER for the read and MAINTAINER for the three writes; the gate itself is
    ``_require_project_role_or_ungoverned``, the same helper (and therefore the same design-§3
    ungoverned fallback) that ``retry`` uses. Deliberately NOT promote's strict gate: opening a
    pull request on a pre-migration project must not be impossible, and a merge deploys nothing
    on its own."""
    repo, detail = _load_visible_repo(repo_id, ctx)
    _require_project_role_or_ungoverned(
        pctx, repo.project_id, required, principal=principal
    )
    org, base_url = _provider_coordinates(detail)
    connection_id = detail.project.connection_id
    token = _user_token(principal, connection_id)
    return org, repo.name, token, _viewer_login(principal, connection_id), base_url


@repositories_router.get("/{repo_id}/pull-requests", response_model=List[PullRequestView])
def list_pull_requests(
    repo_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """This repository's pull requests, as the linked human sees them (D14).

    A SYNC handler (threadpool): the service does blocking ``httpx`` I/O and the link lookup
    does blocking boto3 work, neither of which may run on the uvicorn event loop.

    THIS READ IS ALSO THE CAPABILITY PROBE (A3). The frontend calls it to decide whether the
    Pull requests tab renders AT ALL, so its failure modes are load-bearing: a
    ``capability_missing`` 403 — the org's App lacking the ``pull_requests`` grant — resolves to
    a HIDDEN tab, and every other refusal is a mapped status the tab can state. Nothing here may
    escape as a 500, because a broken tab is exactly the outcome the requirement forbids.

    VIEWER-gated, matching every other read of a repository fact. The three writes below need
    MAINTAINER."""
    org, name, token, viewer, base_url = _pr_context(
        repo_id, principal, ctx, pctx, ProjectRole.VIEWER
    )
    try:
        return get_pr_service().list_pull_requests(
            org, name, token, viewer_login=viewer, base_url=base_url
        )
    except GitHubPrError as err:
        _raise_pr_error(err)


@repositories_router.post(
    "/{repo_id}/pull-requests", response_model=PullRequestView, status_code=201
)
def create_pull_request(
    repo_id: str,
    body: PullRequestCreate,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Open a pull request AS THE LINKED HUMAN — attributed to them, never to AGP's App.

    MAINTAINER-gated, the same threshold as ``retry``: this writes to the repository, but it
    deploys nothing.

    ``base`` rides through OPTIONAL and unvalidated (D8). A tenant's branch set is open, so an
    omitted base means the repository's own default branch — a fact the PROVIDER holds — and a
    literal default here would be the hardcode the design forbids. The AUTHOR is not a body
    field at all: it is whichever GitHub account the caller's link names."""
    org, name, token, viewer, base_url = _pr_context(
        repo_id, principal, ctx, pctx, ProjectRole.MAINTAINER
    )
    try:
        return get_pr_service().create_pull_request(
            org,
            name,
            token,
            viewer_login=viewer,
            title=body.title,
            head=body.head,
            base=body.base,
            body=body.body,
            base_url=base_url,
        )
    except GitHubPrError as err:
        _raise_pr_error(err)


@repositories_router.post(
    "/{repo_id}/pull-requests/{number}/approve", response_model=PullRequestView
)
def approve_pull_request(
    repo_id: str,
    number: int,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Approve a pull request AS THE LINKED HUMAN (D15) — a repo-provider act, in scope.

    A SELF-APPROVAL IS REFUSED WITH A REASON AND NOTHING IS RETRIED. The service reads the PR,
    refuses locally when the linked human is the author, and never reaches the review endpoint;
    this route holds no second credential to retry under, which is the whole design. The 409 it
    answers is a state the UI states calmly beside a suppressed button, not an error banner.

    This is NOT the governance approval. Approving a pull request is the provider's verb on the
    provider's object; approving an AGENT is governance and is forbidden on this surface. And it
    is not a way into production either: a merge registers a prod CANDIDATE that still needs an
    OWNER's promote through the existing governed route."""
    org, name, token, viewer, base_url = _pr_context(
        repo_id, principal, ctx, pctx, ProjectRole.MAINTAINER
    )
    try:
        return get_pr_service().approve_pull_request(
            org, name, number, token, viewer_login=viewer, base_url=base_url
        )
    except GitHubPrError as err:
        _raise_pr_error(err)


@repositories_router.post(
    "/{repo_id}/pull-requests/{number}/merge", response_model=PullRequestView
)
def merge_pull_request(
    repo_id: str,
    number: int,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Merge a pull request AS THE LINKED HUMAN.

    A MERGE FAILURE SURFACES A SAFE HINT ONLY — a fixed literal per ``.kind``, never the
    provider's message, which can echo attacker-influenced content (and this epic has already
    had to truncate one field for carrying an AWS account id). The two ordinary refusals,
    "cannot be merged yet" (checks or branch protection) and "conflicting state" (the branch
    moved), are 409 states the UI renders rather than faults.

    MAINTAINER, not OWNER: merging to the default branch registers a prod CANDIDATE, and
    shipping it still requires an OWNER's promote. Gating this at OWNER would put ordinary
    development behind the production gate without protecting production any further."""
    org, name, token, viewer, base_url = _pr_context(
        repo_id, principal, ctx, pctx, ProjectRole.MAINTAINER
    )
    try:
        return get_pr_service().merge_pull_request(
            org, name, number, token, viewer_login=viewer, base_url=base_url
        )
    except GitHubPrError as err:
        _raise_pr_error(err)
