"""MCP Server Registry CRUD + lifecycle API routes (Epic 5, Task 4).

Structural clone of `agents.py` (Epic 4). Backed by `McpServerRegistryService`
(AWS Agent Registry, MCP records). Mirrors the router + lazy `get_service()` +
error→HTTP mapping idiom, and the RBAC `Depends as RBACDepends` +
`require_role(Role.X)` trailing-param idiom.

RBAC: list/get = VIEWER; create/update/delete/submit = OPERATOR; transitions
(approve/reject/deprecate) = ADMIN.

`created_by` comes from the validated `current_principal`, never a hardcoded
"user"; when the create payload leaves owner_* blank, it defaults to the creator.

Two MCP-specific deltas vs E4's mapping: a schema `McpValidationError` on
create/update → HTTP 422 (research §2). Both `McpValidationError` and
`NameTakenError` must precede any generic `ValueError` catch (McpValidationError
subclasses ValueError), so create/update deliberately have NO bare ValueError
catch.

Multi-tenancy (Epic 24/T5): a `tenant_ctx` per-request dependency (delegating to
``users.get_tenant_ctx`` — the ONE resolver-singleton accessor) gates every
detail/mutation/lifecycle/identity route via ``_load_visible_mcp_server`` BEFORE
any side effect, and post-filters ``list`` results. ``shared`` grants
cross-tenant READ visibility ONLY (get + list): mutation/lifecycle/identity
routes pass ``for_write=True`` so a foreign shared MCP is 404 on write —
mutations require own-tenant membership or global admin (policy resolution of
review Finding 1). Setting OR clearing ``shared`` (create or update) is
ADMIN-only.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi import Depends as RBACDepends
from pydantic import BaseModel

from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from models.mcp_server import (
    IdentityStatus, Kind,
    LifecycleState,
    McpServer,
    McpServerCreate,
    McpServerUpdate,
)
from models.repository import RepoDeleteItemResult
from services.mcp_identity_service import McpIdentityService, should_provision_mcp
from services.mcp_server_service import (
    IllegalTransitionError,
    McpServerRegistryService,
    McpValidationError,
    NameTakenError,
)
from services.tenant_credentials import StageUnresolvedError, TenantCredentialsError
from services.tenant_resolver import TenantContext, visible

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/mcp-servers", tags=["mcp-servers"])

_svc: Optional[McpServerRegistryService] = None
_identity_svc: Optional[McpIdentityService] = None


def get_service() -> McpServerRegistryService:
    """Lazy McpServerRegistryService singleton.

    Addressed by NAME, with ``MCP_REGISTRY_ID`` kept as an explicit override — identical
    reasoning to ``routes/agents.get_service()``; see that docstring and
    ``core.registry_resolver``.
    """
    global _svc
    if _svc is None:
        _svc = McpServerRegistryService(
            registry_id=getattr(settings, "MCP_REGISTRY_ID", ""),
            registry_name=getattr(settings, "MCP_REGISTRY_NAME", ""),
            region=getattr(settings, "MCP_REGISTRY_REGION", "us-east-1"),
        )
    return _svc


def get_mcp_identity_service() -> McpIdentityService:
    """Lazy McpIdentityService singleton (the MCP provisioning orchestrator, T-IDENTITY).

    Wires the shared GraphService (owned by ``routes/mcp_server_grants``) + the MCP
    registry singleton; region defaults to the registry region. Mirrors the
    ``get_identity_service()`` idiom of ``routes/agents.py``.
    """
    global _identity_svc
    if _identity_svc is None:
        from api.routes.mcp_server_grants import get_mcp_graph_service

        _identity_svc = McpIdentityService(
            graph=get_mcp_graph_service(),
            registry=get_service(),
            tenant_id=settings.ENTRA_TENANT_ID,
            login_base=settings.ENTRA_LOGIN_BASE,
            region=getattr(settings, "MCP_REGISTRY_REGION", "") or "us-east-1",
        )
    return _identity_svc


async def get_tenant_ctx(
    principal: Principal = RBACDepends(current_principal),
) -> TenantContext:
    """Delegate to the ONE resolver-singleton accessor (E24/T5).

    ``users.py`` owns the lazy ``TenantResolver`` singleton (``_tenant_resolver`` /
    ``get_tenant_resolver()``); this is a thin re-export so mcp_servers.py has its
    own per-request ``tenant_ctx`` dependency WITHOUT keeping a second resolver
    copy — tests patch ``api.routes.users._tenant_resolver`` and both /users/me and
    every route here observe it. Imported lazily to avoid an import cycle at module
    load (mirrors ``agents.get_tenant_ctx``).
    """
    from api.routes.users import get_tenant_ctx as _users_get_tenant_ctx

    return await _users_get_tenant_ctx(principal)


async def _load_visible_mcp_server(
    mcp_server_id: str, ctx: TenantContext, *, for_write: bool = False
) -> McpServer:
    """Load an MCP server by id and gate it on tenant visibility BEFORE any side
    effect. ONE helper every detail/mutation/lifecycle/identity route calls
    (research brief — missing an endpoint is this task's failure mode). A missing
    OR not-visible MCP raises the SAME 404 literal ("MCP server not found") — the
    two cases must be byte-identical (spec's 404-not-403 contract).

    ``shared`` grants cross-tenant READ visibility ONLY (review Finding 1 policy
    resolution): the default READ gate honors ``shared`` (a platform-shared MCP
    is visible to every tenant), but ``for_write=True`` (every mutation/
    lifecycle/identity route) drops the shared bypass — a foreign shared MCP is
    404 on a write path; mutations require own-tenant membership or global
    admin."""
    mcp_server = get_service().get(mcp_server_id)
    shared = bool(mcp_server and mcp_server.shared and not for_write)
    if not mcp_server or not visible(ctx, mcp_server.tenant_id, shared=shared):
        raise HTTPException(status_code=404, detail="MCP server not found")
    return mcp_server


class TransitionRequest(BaseModel):
    """Body for the admin lifecycle transition endpoint."""

    action: str  # "approve" | "reject" | "deprecate"
    reason: str


class PublishRequest(BaseModel):
    """Body for the cross-tenant publish toggle (E24/T5)."""

    published: bool


def _coerce_lifecycle(value: Optional[str]) -> Optional[LifecycleState]:
    if value is None:
        return None
    try:
        return LifecycleState(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid lifecycle_state: {value}")


def _coerce_kind(value: Optional[str]) -> Optional[Kind]:
    if value is None:
        return None
    try:
        return Kind(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid kind: {value}")


def _reject_shared_if_not_admin(shared: Optional[bool], principal: Principal) -> None:
    """MCP ``shared`` is settable only by ADMIN (create AND update) — spec §5 +
    review Finding 2. ``None`` means "``shared`` absent from the payload" and is
    always allowed; ANY explicit value — ``True`` (sharing) OR ``False``
    (un-sharing) — from a non-admin caller is rejected, so an OPERATOR cannot
    silently un-share a platform-shared MCP any more than they can share one.
    The create path normalizes its non-Optional ``shared: bool = False`` default
    to ``None`` before calling (creating an unshared MCP is the default and
    carries no privilege)."""
    if shared is not None and principal.role < Role.ADMIN:
        raise HTTPException(status_code=403, detail="only ADMIN may set shared")


# --- CRUD --------------------------------------------------------------------

@router.post("", response_model=McpServer, status_code=201)
async def create_mcp_server(
    req: McpServerCreate,
    background_tasks: BackgroundTasks,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    # Multi-tenancy (E24/T5) — tenant_id must exist, then (for a non-global caller)
    # must be one of the caller's own memberships. No resource exists yet, so 403
    # (not 404) is correct here — unlike the visibility-gated routes below.
    from api.routes.tenants import get_tenant_service
    from services.tenant_service import TenantError

    try:
        get_tenant_service().get(req.tenant_id)
    except TenantError:
        raise HTTPException(status_code=400, detail="unknown tenant")
    if not ctx.is_global and req.tenant_id not in ctx.tenant_ids:
        raise HTTPException(status_code=403, detail="tenant not permitted")

    # shared is ADMIN-only (create or update) — spec §5. Normalize the create
    # model's non-Optional `shared: bool = False` default to None (= "not being
    # set"): only creating an ALREADY-shared MCP needs ADMIN.
    _reject_shared_if_not_admin(True if req.shared else None, principal)

    # The creator owns by default when the wizard leaves owner_* blank.
    if not req.owner_email:
        req.owner_email = principal.email
    if not req.owner_oid:
        req.owner_oid = principal.oid

    svc = get_service()
    try:
        mcp = svc.create(req, created_by=principal.email or principal.oid)
    except McpValidationError as e:
        # Registry schema-validation rejection (research §2) -> 422. Must precede
        # any generic ValueError catch (McpValidationError subclasses ValueError).
        raise HTTPException(status_code=422, detail=str(e))
    except NameTakenError as e:
        raise HTTPException(status_code=409, detail=str(e))

    # Provisioning-on-registration hook (Epic 7, clone of E6's create_agent) — AgentCore
    # Gateway / Runtime-MCP records only (should_provision_mcp gates kind+handle). Unlike
    # the agent path (where create() pre-stamps 'pending'), McpServerRegistryService.create()
    # does NOT stamp identity_status, so we set it 'pending' + persist_identity HERE (so the
    # 201 response + the FE banner reflect the in-flight provisioning), THEN schedule
    # provision() to run AFTER this 201 is sent. Errors inside provision() are swallowed into
    # identity_status='failed' by the service — they never crash the request. A standard
    # (external/metadata) MCP → no provisioning; identity_status stays 'none'.
    if should_provision_mcp(mcp):
        logger.info(
            "[mcp_servers] scheduling background provisioning for MCP %s (kind=%s)",
            mcp.id,
            mcp.kind,
        )
        mcp.identity_status = IdentityStatus.PENDING
        svc.persist_identity(mcp)
        background_tasks.add_task(get_mcp_identity_service().provision, mcp)
    else:
        logger.debug("[mcp_servers] no provisioning for standard MCP %s", mcp.id)

    return mcp


@router.get("", response_model=List[McpServer])
async def list_mcp_servers(
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
    lifecycle_state: Optional[str] = Query(default=None),
    kind: Optional[str] = Query(default=None),
    owner_oid: Optional[str] = Query(default=None),
    business_unit: Optional[str] = Query(default=None),
    region: Optional[str] = Query(default=None),
):
    svc = get_service()
    records = svc.list(
        lifecycle_state=_coerce_lifecycle(lifecycle_state),
        kind=_coerce_kind(kind),
        owner_oid=owner_oid,
        business_unit=business_unit,
        region=region,
    )
    # Tenant post-filter (E24/T5) — the AWS registry API has no tenant concept, so
    # filtering lands AFTER svc.list() returns (research §5). shared=True servers
    # pass for every caller regardless of tenant_id.
    return [r for r in records if visible(ctx, r.tenant_id, shared=r.shared)]


@router.get("/{mcp_server_id}", response_model=McpServer)
async def get_mcp_server(
    mcp_server_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    return await _load_visible_mcp_server(mcp_server_id, ctx)


@router.put("/{mcp_server_id}", response_model=McpServer)
async def update_mcp_server(
    mcp_server_id: str,
    req: McpServerUpdate,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    await _load_visible_mcp_server(mcp_server_id, ctx, for_write=True)
    # shared is ADMIN-only (create or update) — spec §5 + Finding 2: ANY explicit
    # `shared` in the payload (True OR False) requires ADMIN.
    _reject_shared_if_not_admin(req.shared, principal)

    svc = get_service()
    try:
        mcp_server = svc.update(mcp_server_id, req)
    except McpValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except NameTakenError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not mcp_server:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return mcp_server


async def _teardown_item(item: str, start) -> RepoDeleteItemResult:
    """Run one best-effort teardown leg and map success/failure to a report line-item.

    The async twin of ``ProjectService._run_step`` (E23/T4), and deliberately the SAME
    vocabulary + model: ``outcome`` ∈ ``deleted|failed|skipped`` and a SAFE ``reason`` — the
    two PREFIXED reasons T8 pinned (``assume_role_failed:`` = we know the account and could
    not get in; ``stage_unresolved:`` = we cannot tell which account owns it, two different
    operator actions) and ``type(err).__name__`` for everything else, never a token, a Graph
    body or an AWS message. Two report shapes for the same class of act would be free to
    drift. The identical helper lives in ``routes/agents.py`` — the two registry route
    modules are deliberate structural clones.

    ``start`` is a zero-arg CALLABLE returning the awaitable, not the awaitable itself, so
    that resolving the lazy service singleton happens INSIDE the guard too: a cascade whose
    contract is "nothing escapes" must not be able to 500 on building a collaborator.

    A leg may RETURN a non-empty ``str`` to mean "I deliberately did nothing, and this is the
    safe reason": that becomes ``skipped`` with that reason instead of a false ``deleted``
    (E36/T16 fix round 1 — a policy engine the record does not own is left alone).
    """
    try:
        note = await start()
    except TenantCredentialsError as err:
        logger.exception("[teardown] step '%s' could not assume the tenant role", item)
        return RepoDeleteItemResult(
            item=item, outcome="failed", reason=f"assume_role_failed: {err.message}"
        )
    except StageUnresolvedError as err:
        logger.exception("[teardown] step '%s' could not resolve the owning account", item)
        return RepoDeleteItemResult(
            item=item, outcome="failed", reason=f"stage_unresolved: {err.message}"
        )
    except Exception as err:  # noqa: BLE001 — best-effort; the report carries the failure
        logger.exception("[teardown] step '%s' failed", item)
        return RepoDeleteItemResult(item=item, outcome="failed", reason=type(err).__name__)
    if isinstance(note, str) and note:
        return RepoDeleteItemResult(item=item, outcome="skipped", reason=note)
    return RepoDeleteItemResult(item=item, outcome="deleted")


async def _teardown_mcp_identity(mcp: McpServer) -> List[RepoDeleteItemResult]:
    """Tear down the platform-managed identity an MCP record owns (E36/T16, item 5A).

    Two legs, in this order, each BEST-EFFORT and each its own report line-item:

      1. ``identity`` — delete the MCP's Entra application (``entra_app_id``), which
         CASCADES its service principal and every consent/app-role assignment granted on
         it, via the existing generic idempotent ``GraphService.delete_agent_app``. No new
         Graph code: that method already resolves appId→objectId, falls back to the SP id,
         no-ops on blank ids and swallows a 404.
      2. ``policy_engine`` — detach the native Cedar Policy Engine from the gateway and
         delete it (``McpCedarService.delete_policy_engine``).

    AUTHENTICATION BEFORE AUTHORIZATION, and that order is the security property (E36/T16 fix
    round 1 — it used to be the other way round). NOTHING deletes the GATEWAY: the platform
    did not create it, so it stays live and serving after both legs. Removing Cedar first left
    a window — and, if the identity leg then failed, a permanent state — in which that live
    gateway accepted every tool call with authorization stripped and its authenticator intact.
    Reversed, the app registration goes first, so a sequence that dies in the middle leaves a
    gateway that can no longer mint a token (fail-CLOSED) rather than one that authorizes
    everything (fail-open).

    THE RESIDUAL, stated because the record is about to be deleted and nobody can look it up
    afterwards: the legs are independent, so a FAILED identity leg does not stop the engine
    leg. A Graph 403 plus a successful engine delete still ends with a live gateway whose
    Cedar is gone and whose Entra app survives; the report line-items (logged under
    ``[teardown]``) are the only trace, and the reclaim is manual. Already-minted tokens also
    stay valid to their expiry on the happy path. Same limitation as
    ``docs/agentcore-registration.md`` §10 names for every leg here.

    Each leg is SKIPPED (an explicit line-item, never silently absent) when the record
    carries nothing for it — a standard MCP has no gateway and no engine, and an MCP whose
    provisioning never ran has no Entra ids. The engine leg also reports ``skipped`` when the
    live gateway carries an engine this record does not own; it is never deleted (see
    ``McpCedarService.delete_policy_engine``).

    Best-effort means best-effort: a failure is REPORTED (and logged with a stack trace) but
    never blocks the record delete. The alternative — 500 and keep the row — would leave the
    operator with a record they cannot delete and the orphans they were trying to reclaim.
    Both legs are idempotent, so the retryable path is simply "delete again" once the cause
    (a Graph permission, a busy gateway) is fixed; the ids stay on the record until it goes.
    """
    items: List[RepoDeleteItemResult] = []

    if mcp.entra_app_id or mcp.entra_sp_id:
        from api.routes.mcp_server_grants import get_mcp_graph_service

        items.append(
            await _teardown_item(
                "identity",
                lambda: get_mcp_graph_service().delete_agent_app(
                    entra_app_id=mcp.entra_app_id, entra_sp_id=mcp.entra_sp_id
                ),
            )
        )
    else:
        items.append(RepoDeleteItemResult(item="identity", outcome="skipped"))

    if mcp.cedar_policy_engine_id or mcp.gateway_id:
        from api.routes.mcp_cedar import get_cedar_service

        items.append(
            await _teardown_item(
                "policy_engine", lambda: get_cedar_service().delete_policy_engine(mcp)
            )
        )
    else:
        items.append(RepoDeleteItemResult(item="policy_engine", outcome="skipped"))

    # The report's ONLY consumer: `response_model=McpServer` is the pinned wire contract, so
    # this log line IS the audit trail for an operator who has to reclaim a failed leg by hand
    # after the record is gone. ONE stable prefix across both REGISTRY cascades so a single
    # CloudWatch filter finds every outcome they report (the repo cascade logs under
    # `[project] teardown step …` and returns its items on the wire — see `project_service.py`).
    for entry in items:
        logger.info(
            "[teardown] mcp_server=%s item=%s outcome=%s reason=%s",
            mcp.id,
            entry.item,
            entry.outcome,
            entry.reason,
        )
    return items


@router.delete("/{mcp_server_id}", response_model=McpServer)
async def delete_mcp_server(
    mcp_server_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Delete an MCP server: tear down its identity, THEN delete the registry record.

    E36/T16 (item 5A): this used to delete the record only, orphaning the MCP's Entra
    app/SP + consents and — worse — leaving the live gateway attached to an ENFORCE-mode
    Cedar policy engine nothing pointed at. The cascade runs BEFORE the record delete
    because every id it needs (``cedar_policy_engine_id``, ``entra_app_id``,
    ``entra_sp_id``) lives on the record.

    The GATEWAY is never deleted (the platform did not create it), so the cascade tears
    AUTHENTICATION down before AUTHORIZATION: a half-finished teardown must leave a gateway
    that cannot mint a token, not one still serving with Cedar stripped. Order, its residual
    fail-open case and the per-resource report all live in ``_teardown_mcp_identity``.

    It runs in the ROUTE rather than in ``McpServerRegistryService.delete`` for one hard
    reason: ``McpCedarService`` imports the registry service, so a cascade inside it would
    be an import cycle. That also matches the per-item cascade shape of ``projects.py``.
    """
    mcp = await _load_visible_mcp_server(mcp_server_id, ctx, for_write=True)
    await _teardown_mcp_identity(mcp)
    svc = get_service()
    mcp_server = svc.delete(mcp_server_id)
    if not mcp_server:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return mcp_server


# --- lifecycle ---------------------------------------------------------------

@router.post("/{mcp_server_id}/submit", response_model=McpServer)
async def submit_mcp_server(
    mcp_server_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    await _load_visible_mcp_server(mcp_server_id, ctx, for_write=True)
    svc = get_service()
    mcp_server = svc.submit_for_approval(mcp_server_id)
    if not mcp_server:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return mcp_server


@router.post("/{mcp_server_id}/transitions", response_model=McpServer)
async def transition_mcp_server(
    mcp_server_id: str,
    req: TransitionRequest,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    await _load_visible_mcp_server(mcp_server_id, ctx, for_write=True)
    svc = get_service()
    try:
        mcp_server = svc.transition(mcp_server_id, req.action, req.reason)
    except IllegalTransitionError as e:
        # Illegal status edge (e.g. DRAFT->APPROVED). IllegalTransitionError IS a
        # ValueError, so this MUST precede the generic ValueError->400 handler.
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not mcp_server:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return mcp_server


@router.put("/{mcp_server_id}/publish", response_model=McpServer)
async def publish_mcp_server(
    mcp_server_id: str,
    req: PublishRequest,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Flip the cross-tenant publish flag (E24/T5). Visibility-gated 404 (same as
    every other mutation), then a plain envelope update via the existing
    ``McpServerUpdate`` read-modify-write path."""
    await _load_visible_mcp_server(mcp_server_id, ctx, for_write=True)
    svc = get_service()
    mcp_server = svc.update(mcp_server_id, McpServerUpdate(published=req.published))
    if not mcp_server:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return mcp_server


# --- identity re-provisioning (Epic 7, clone of E6's reprovision_agent) -------

@router.post("/{mcp_server_id}/reprovision", response_model=McpServer, status_code=202)
async def reprovision_mcp_server(
    mcp_server_id: str,
    background_tasks: BackgroundTasks,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Re-run identity provisioning for an AgentCore Gateway / Runtime-MCP (any state).

    The provisioning sequence is non-atomic + idempotent + resumable, so this is the
    recovery affordance for a 'failed' (or stranded 'pending') MCP — and also re-configures
    a live 'provisioned' one. We FIRST set identity_status='pending' and persist (so the FE
    banner reflects the in-flight re-config), THEN schedule provision() as a background task.
    Returns 202 with the pending MCP. A standard (non-AgentCore) MCP → 409.
    """
    mcp = await _load_visible_mcp_server(mcp_server_id, ctx, for_write=True)
    svc = get_service()
    if not should_provision_mcp(mcp):
        raise HTTPException(status_code=409, detail="not an AgentCore gateway or runtime MCP")

    logger.info("[mcp_servers] re-provisioning MCP %s", mcp_server_id)
    mcp.identity_status = IdentityStatus.PENDING
    svc.persist_identity(mcp)
    background_tasks.add_task(get_mcp_identity_service().provision, mcp)
    return mcp


@router.post("/{mcp_server_id}/refresh-tools", response_model=McpServer, status_code=200)
async def refresh_mcp_tools(
    mcp_server_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Re-read the gateway's tools from the control plane + persist, SYNCHRONOUSLY.

    Returns the updated McpServer (200) so the UI can show the fresh tools immediately on
    click. Unlike ``/reprovision`` (202 + a full BACKGROUND re-provision that also
    reconfigures the inbound authorizer), this touches ONLY tools — the native read
    (``ListGatewayTargets`` → ``GetGatewayTarget`` → inline lambda toolSchema) is token-less
    and works on a locked CUSTOM_JWT gateway. A flaky read is best-effort inside the service
    (never wipes existing tools, never 500s). OPERATOR-gated (it can rewrite available_tools).

    404 if missing or not tenant-visible; 409 if not a gateway (runtime/standard MCPs
    have no native target read).
    """
    logger.info("[mcp_servers] refresh-tools requested for MCP %s", mcp_server_id)
    mcp = await _load_visible_mcp_server(mcp_server_id, ctx, for_write=True)
    if mcp.kind != Kind.GATEWAY:
        raise HTTPException(
            status_code=409,
            detail="tool refresh is only supported for gateway MCP servers",
        )
    updated = await get_mcp_identity_service().refresh_tools(mcp)
    return updated
