"""Agent → MCP grants (Entra app-role assignments on the MCP SP) — Epic 7, T-ROUTES.

Like E6's user→agent grants (``routes/grants.py``), these are **live Microsoft Graph
reads/writes — NO DynamoDB**: ``appRoleAssignedTo`` on the MCP's service principal is the
single source of truth. They are a thin pass-through over :class:`GraphService`, mirroring
the lazy ``get_*_graph_service()`` singleton + RBAC + error→HTTP idiom of ``routes/grants.py``,
but with the **MCP SP as the resource** and an **agent SP as the principal**.

Two routers live here:
  - ``router`` (``prefix="/mcp-servers"``, tag ``"mcp-grants"``) — grants nested under the MCP:
    ``GET/POST /mcp-servers/{id}/grants`` + ``DELETE /mcp-servers/{id}/grants/{assignment_id}``.
  - ``agent_mcp_router`` (``prefix="/agents"``, tag ``"agent-mcp-grants"``) — the AGENT-direction
    reverse read: ``GET /agents/{id}/mcp-grants`` ("what MCPs can this agent reach"). It MUST
    live on a ``/agents``-prefixed router (CRITIC-M2): a route on the ``/mcp-servers``-prefixed
    ``router`` could not serve an ``/agents/...`` path (prefix mismatch → unreachable).

The POST does **both** the app-role assignment AND ``grant_agent_obo_consent`` — the
delegated-consent precondition for the Tier-2 agent→MCP OBO invoke (research §2.4).

SECURITY (T-GRAPH carry-forward): the ``GraphError``→HTTP mapping uses FIXED ``detail=``
literals, NEVER ``str(err)`` / ``err.message`` — a Graph resource ``error.message`` could
otherwise surface to clients. The resolved internal GUIDs (``mcp.entra_sp_id``,
``principal_id``) flow into the graph service; no raw path segment is echoed into a Graph URL.
"""

import logging
from types import SimpleNamespace
from typing import List, Optional

import anyio.to_thread
from fastapi import APIRouter, HTTPException, Response
from fastapi import Depends as RBACDepends
from pydantic import BaseModel

from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from services.agent_credential_service import AgentCredentialService
from services.agent_identity_service import AgentIdentityService
from services.agent_mcp_grant import (
    ConsentRevokeError,
    GrantNotFoundError,
    GrantReadError,
    GrantRevokeError,
    GrantRevokeFailedError,
    apply_agent_mcp_grant,
    revoke_agent_mcp_grant,
)
from services.graph_service import GraphError, GraphService
from services.tenant_resolver import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp-servers", tags=["mcp-grants"])
agent_mcp_router = APIRouter(prefix="/agents", tags=["agent-mcp-grants"])

# Lazy GraphService singleton — same pattern as ``routes/grants.py`` (a per-router
# singleton; tests patch ``mcp_server_grants._graph_svc`` directly).
_graph_svc: Optional[GraphService] = None

# Lazy AgentCredentialService singleton (T-GRANT-CRED-WIRING) — mirrors the GraphService
# singleton; tests patch ``mcp_server_grants._cred_svc`` directly.
_cred_svc: Optional[AgentCredentialService] = None

# Lazy AgentIdentityService singleton (T-GRANT-ENV-INJECT) — used at grant time to inject
# the reference agent's runtime env (set_runtime_environment). Mirrors the other singletons;
# tests patch ``mcp_server_grants._identity_svc`` directly.
_identity_svc: Optional[AgentIdentityService] = None


def get_mcp_graph_service() -> GraphService:
    global _graph_svc
    if _graph_svc is None:
        _graph_svc = GraphService(
            tenant_id=settings.ENTRA_TENANT_ID,
            backend_client_id=settings.ENTRA_BACKEND_CLIENT_ID,
            login_base=settings.ENTRA_LOGIN_BASE,
            graph_base=settings.GRAPH_API_BASE,
            audience_prefix=settings.AGENT_APP_AUDIENCE_PREFIX,
        )
    return _graph_svc


def get_agent_credential_service() -> AgentCredentialService:
    """Lazy AgentCredentialService singleton (the per-agent OBO credential provider).

    Wires the shared GraphService; region defaults to the agent registry region (same as
    ``agents.get_identity_service()`` — the agent-side services are co-located there).
    """
    global _cred_svc
    if _cred_svc is None:
        _cred_svc = AgentCredentialService(
            graph=get_mcp_graph_service(),
            region=getattr(settings, "AGENT_REGISTRY_REGION", "") or "us-east-1",
        )
    return _cred_svc


async def get_tenant_ctx(
    principal: Principal = RBACDepends(current_principal),
) -> TenantContext:
    """Delegate to the ONE resolver-singleton accessor (E24 follow-up, L1).

    ``users.py`` owns the lazy ``TenantResolver`` singleton; this thin re-export
    gives mcp_server_grants.py its own per-request ``tenant_ctx`` dependency
    WITHOUT a second resolver copy — tests patch
    ``api.routes.users._tenant_resolver`` and the grant-list read gates observe
    it. Imported lazily to avoid an import cycle at module load (mirrors
    ``mcp_servers.get_tenant_ctx``).
    """
    from api.routes.users import get_tenant_ctx as _users_get_tenant_ctx

    return await _users_get_tenant_ctx(principal)


def get_agent_identity_service() -> AgentIdentityService:
    """Lazy AgentIdentityService singleton (T-GRANT-ENV-INJECT — the runtime env writer).

    Used by the grant route to inject the reference agent's runtime env via
    ``set_runtime_environment``. Mirrors ``agents.get_identity_service()`` (shared
    GraphService + the agent registry singleton; region co-located with the registry).
    """
    global _identity_svc
    if _identity_svc is None:
        # Local import avoids import-order coupling with the agents route module (same
        # idiom the create-grant handler uses to resolve the agent registry).
        from api.routes.agents import get_service as get_agent_service

        _identity_svc = AgentIdentityService(
            graph=get_mcp_graph_service(),
            registry=get_agent_service(),
            tenant_id=settings.ENTRA_TENANT_ID,
            login_base=settings.ENTRA_LOGIN_BASE,
            region=getattr(settings, "AGENT_REGISTRY_REGION", "") or "us-east-1",
        )
    return _identity_svc


# --- models ------------------------------------------------------------------
# Reuse E6's GrantCreate/GrantRead shapes verbatim (re-declared identically; the FE
# reuses the same TS types). principal_type is "agent" here (vs "user"/"group" in E6).

class GrantCreate(BaseModel):
    principal_id: str        # the agent's Entra SP object id (the grant principal)
    principal_type: str      # "agent" (display only; Graph assigns identically)
    role: str                # "Invoker" | "Admin"


class GrantRead(BaseModel):
    assignment_id: str       # appRoleAssignment id (for revoke)
    principal_id: str
    principal_display: str
    principal_type: str      # Graph's principalType (e.g. "ServicePrincipal")
    role: str                # mapped from appRoleId via invoker_role_id/admin_role_id


class AgentMcpGrant(BaseModel):
    """One row of the agent-direction reverse read (the agent's outbound MCP grants)."""

    mcp_id: str          # our McpServer.id (reverse-joined from resourceId via entra_sp_id)
    mcp_name: str        # resourceDisplayName (inline from Graph)
    role: str            # mapped from appRoleId via the MCP's invoker_role_id/admin_role_id
    assignment_id: str   # for revoke (DELETE on /mcp-servers/{mcp_id}/grants/{assignment_id})


# --- helpers -----------------------------------------------------------------

def _role_for_app_role_id(mcp, app_role_id: Optional[str]) -> str:
    """Map a Graph appRoleId GUID → "Invoker"/"Admin" via the MCP's stored ids.

    An id matching neither (or a None id) → "Unknown" (tolerant — a role minted by
    another tool, or a stale/legacy assignment, must not break the list view).
    """
    if app_role_id and app_role_id == mcp.invoker_role_id:
        return "Invoker"
    if app_role_id and app_role_id == mcp.admin_role_id:
        return "Admin"
    return "Unknown"


def _app_role_id_for_role(mcp, role: str) -> str:
    """Map a requested role name → the MCP's stored appRoleId GUID.

    Raises ``HTTPException(400)`` for anything other than "Invoker"/"Admin".
    """
    if role == "Invoker":
        return mcp.invoker_role_id
    if role == "Admin":
        return mcp.admin_role_id
    raise HTTPException(status_code=400, detail="role must be 'Invoker' or 'Admin'")


def _is_provisioned(resource) -> bool:
    """An MCP/agent is grant-capable iff its identity is provisioned + it has an SP id."""
    return resource.identity_status == "provisioned" and bool(resource.entra_sp_id)


def _to_grant_read(mcp, assignment: dict) -> GrantRead:
    return GrantRead(
        assignment_id=assignment.get("id", ""),
        principal_id=assignment.get("principalId", ""),
        principal_display=assignment.get("principalDisplayName") or "",
        principal_type=assignment.get("principalType") or "",
        role=_role_for_app_role_id(mcp, assignment.get("appRoleId")),
    )


# --- grants routes (resource-side: the MCP SP) -------------------------------

@router.get("/{mcp_id}/grants", response_model=List[GrantRead])
async def list_mcp_grants(
    mcp_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """List the MCP's app-role assignments (which agents can reach it).

    Unprovisioned (no SP / status != provisioned) → ``[]`` (NOT 409 — the FE shows a
    banner). Otherwise read live from Graph + map each appRoleId → Invoker/Admin.

    Tenant gate (E24 follow-up, review L1): the read is gated on the SAME
    ``_load_visible_mcp_server`` helper as the parent ``GET /mcp-servers/{id}``
    (READ semantics — ``for_write=False`` keeps the shared-MCP bypass) — a foreign
    non-shared MCP 404s with the byte-identical "MCP server not found" literal.
    """
    # Local import avoids any import-order coupling with the mcp_servers route module.
    from api.routes.mcp_servers import _load_visible_mcp_server

    mcp = await _load_visible_mcp_server(mcp_id, ctx)

    if not _is_provisioned(mcp):
        return []

    assignments = await get_mcp_graph_service().list_assignments(mcp.entra_sp_id)
    return [_to_grant_read(mcp, a) for a in assignments]


@router.post("/{mcp_id}/grants", response_model=GrantRead, status_code=201)
async def create_mcp_grant(
    mcp_id: str,
    body: GrantCreate,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Grant an agent an Invoker/Admin app role on the MCP SP, AND wire the agent→MCP
    delegated consent (the precondition for the Tier-2 agent→MCP OBO invoke, §2.4).

    409 if the MCP's identity isn't provisioned (no SP to assign on). 400 if the role
    isn't Invoker/Admin. A Graph already-exists/409 → 409. The consent grant is
    idempotent (an already-exists is swallowed by the graph service).
    """
    from api.routes.mcp_servers import get_service

    mcp = get_service().get(mcp_id)
    if not mcp:
        raise HTTPException(status_code=404, detail="MCP server not found")
    if not _is_provisioned(mcp):
        raise HTTPException(status_code=409, detail="MCP identity is not provisioned")

    # Validate the role → 400 BEFORE any Graph write (route-side HTTP concern). The shared
    # grant function re-maps the role internally; this guard preserves the route's 400.
    app_role_id = _app_role_id_for_role(mcp, body.role)

    # Cross-tenant grant guard (E24/T7) — runs BEFORE any Graph write, and BEFORE the
    # ``principal`` rebinding below (it needs the CALLER's role). Same-tenant
    # (``agent.tenant_id == mcp.tenant_id``, both non-None) or a platform-shared MCP
    # (``mcp.shared`` — being a grant target is allowed at OPERATOR, Task-5 policy) →
    # OPERATOR suffices. Anything else — foreign tenant, a legacy/unstamped record
    # (``tenant_id=None`` is NEVER same-tenant, so ``None == None`` does NOT pass), or a
    # grant principal that doesn't resolve to a governed agent (tenant unknowable) — is
    # cross-tenant, FAIL-CLOSED → ADMIN only, 403 with a FIXED literal.
    if principal.role < Role.ADMIN and not mcp.shared:
        from api.routes.agents import get_service as get_agent_service

        grant_agent = next(
            (
                a
                for a in get_agent_service().list()
                if a.entra_sp_id == body.principal_id
            ),
            None,
        )
        if (
            grant_agent is None
            or grant_agent.tenant_id is None
            or mcp.tenant_id is None
            or grant_agent.tenant_id != mcp.tenant_id
        ):
            raise HTTPException(
                status_code=403, detail="cross-tenant grant requires admin"
            )

    # The grant principal is the agent's SP object id (``body.principal_id``). Pass a minimal
    # principal carrier into the shared grant function: it does ``assign_app_role`` +
    # ``grant_agent_obo_consent`` against this SP id, and ONLY AFTER those succeed resolves
    # the governed agent record from the registry (for the credential-provider / runtime-env
    # steps) — so the registry is never read before a successful assign, matching the original
    # handler order exactly (an already-assigned / Graph failure short-circuits untouched).
    principal = SimpleNamespace(entra_sp_id=body.principal_id)

    # Apply the full grant via the shared, behavior-preserving function (the body factored
    # out of this handler in E9/T3): assign_app_role → grant_agent_obo_consent →
    # ensure_agent_credential_provider → set_runtime_environment. It returns the Entra
    # assignment id and re-raises the underlying exceptions on failure; this route maps each
    # failure to its EXISTING HTTP status + FIXED detail literal (the secret-leak guard —
    # never str(err) — stays here, the service never raises HTTP).
    try:
        assignment_id = await apply_agent_mcp_grant(principal, mcp, role=body.role)
    except GraphError as err:
        # assign_app_role / grant_agent_obo_consent surfaced a Graph failure. An
        # already-assigned (400/409) that could not be idempotently recovered → 409 (the
        # original behavior); any other → 502. FIXED detail literals (T-GRAPH convention).
        if err.status in (400, 409):
            raise HTTPException(
                status_code=409, detail="principal is already assigned to this MCP"
            )
        raise HTTPException(status_code=502, detail="failed to assign the role")
    except HTTPException:
        # A role-mapping or other already-HTTP error from the shared body → re-raise as-is.
        raise
    except Exception:  # noqa: BLE001 — credential-provider / runtime-env failure (fail loud).
        # The shared body already logged the REAL cause server-side via logger.exception
        # (traceback only — no secret VALUE leaks). Surface the route's FIXED literal — never
        # str(err): the credential service handles a secret; a leaked message could surface it.
        raise HTTPException(
            status_code=502,
            detail="role assigned but credential-provider or runtime env wiring failed; re-grant to retry",
        )

    # Shape the GrantRead response (route concern). The assignment id comes from the shared
    # body; the principal is the grant principal; the role is mapped back from the app role
    # id; principalType is ServicePrincipal (an agent SP), matching the live Graph response.
    return GrantRead(
        assignment_id=assignment_id,
        principal_id=body.principal_id,
        principal_display="",
        principal_type="ServicePrincipal",
        role=_role_for_app_role_id(mcp, app_role_id),
    )


@router.delete("/{mcp_id}/grants/{assignment_id}", status_code=204)
async def delete_mcp_grant(
    mcp_id: str,
    assignment_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Revoke an app-role assignment by id → 204, AND tear down the agent→MCP OBO consent
    so the revoke is a REAL kill switch (E7 security-fix).

    Under our delegated/OBO design the MCP app is ``appRoleAssignmentRequired=false``
    (research §2.4/§2.5), so the agent→MCP admission gate is the **consent grant**
    (``oauth2PermissionGrant``, written by ``grant_agent_obo_consent`` at grant time), NOT
    the app-role assignment (which OBO does not consult — it is only read by app-only/M2M
    tokens, a path we do not use). So deleting only the assignment is COSMETIC for the
    delegated path: it removes the "Connected Agents" UI row but leaves the agent able to
    OBO-reach the MCP. This handler therefore ALSO deletes the consent.

    Sequence (ordering is load-bearing):
      1. Resolve the agent SP (``principalId``) for THIS ``assignment_id`` from
         ``list_assignments`` BEFORE revoking — after ``revoke_app_role`` runs the
         assignment is gone and the ``principalId`` is unrecoverable. If the assignment
         isn't found (already gone — the FE double-click race), keep today's behavior: we
         can't scope a consent revoke, so just attempt the assignment revoke (its 404 →
         404) and skip the consent.
      2. ``revoke_app_role(mcp_sp, assignment_id)`` (as before; 404→404, other→409).
      3. MULTIPLICITY GUARD: an agent may hold BOTH an Invoker AND an Admin assignment on
         the SAME MCP, and those two assignments share ONE consent grant (the consent is
         per-(agent, MCP) scope="Invoke", not per-role — the POST's ``grant_agent_obo_consent``
         swallows already-exists, so a 2nd role reuses the 1st consent). So delete the
         consent ONLY IF the agent has NO OTHER app-role assignment remaining on this MCP
         (re-list, filter to the same ``principalId``); a surviving role still needs it.
      4. Fail-loud: a consent-revoke error (non-404 surfaces from the graph service)
         becomes a 502 with a FIXED detail literal (never ``str(err)`` — T-GRAPH
         convention) and ``logger.exception`` server-side (the file's observability idiom,
         research §12.9). The assignment IS already deleted at that point, but re-revoke is
         idempotent so the operator can retry.

    Guards (mirroring create): 409 if the MCP isn't provisioned (no SP id — otherwise we'd
    hit a malformed ``/servicePrincipals/None/...`` path).
    """
    from api.routes.mcp_servers import get_service

    mcp = get_service().get(mcp_id)
    if not mcp:
        raise HTTPException(status_code=404, detail="MCP server not found")
    if not _is_provisioned(mcp):
        raise HTTPException(status_code=409, detail="MCP identity is not provisioned")

    # The full kill-switch sequence (resolve principalId BEFORE revoke → revoke_app_role →
    # multiplicity-guard re-list → revoke_agent_obo_consent) lives in the shared, behavior-
    # preserving ``revoke_agent_mcp_grant`` (the body factored out of this handler in E9R/T1)
    # so BOTH this route and the marketplace admin-revoke share ONE implementation. It raises
    # typed domain exceptions; this route maps each to its EXISTING HTTP status, surfacing the
    # exception's FIXED safe message as the detail (a literal set by the shared body — never
    # ``str(graph_err)``, so the T-GRAPH no-leak guard holds; the shared body already logged
    # the REAL cause server-side via ``logger.exception``, traceback only). Mapping:
    #   GrantNotFoundError     → 404 (stale / already-gone assignment)
    #   GrantRevokeFailedError → 409 (non-404 GraphError on the assignment revoke)
    #   GrantReadError         → 502 (pre- or post-revoke list_assignments failed)
    #   ConsentRevokeError     → 502 (consent teardown failed; re-revoke to retry)
    try:
        await revoke_agent_mcp_grant(mcp, assignment_id)
    except GrantNotFoundError as err:
        raise HTTPException(status_code=404, detail=str(err))
    except GrantRevokeFailedError as err:
        raise HTTPException(status_code=409, detail=str(err))
    except (GrantReadError, ConsentRevokeError) as err:
        raise HTTPException(status_code=502, detail=str(err))
    except GrantRevokeError as err:  # defensive: any future revoke error → recoverable 502.
        raise HTTPException(status_code=502, detail=str(err))

    return Response(status_code=204)


# --- agent-direction reverse read (separate /agents router — CRITIC-M2) ------

@agent_mcp_router.get("/{agent_id}/mcp-grants", response_model=List[AgentMcpGrant])
async def list_agent_mcp_grants(
    agent_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """Reverse read: which MCPs the agent can reach (its outbound app-role grants).

    Resolve the agent's ``entra_sp_id`` (via the agent registry); unprovisioned → ``[]``.
    ``graph.list_agent_mcp_grants(agent_sp_id)`` returns raw appRoleAssignments with
    ``resourceId`` (= an MCP's SP object id) / ``resourceDisplayName`` / ``appRoleId`` /
    ``id``. Reverse-join each ``resourceId`` back to OUR ``McpServer`` (match on
    ``entra_sp_id``) to get ``mcp_id`` + to interpret ``appRoleId`` against THAT MCP's
    role ids. Assignments whose ``resourceId`` is NOT a known MCP SP (the agent assigned
    to a non-MCP resource, e.g. Microsoft Graph) are FILTERED OUT.

    Tenant gate (E24 follow-up, review L1): the read is gated on the AGENT's tenant
    via the SAME ``_load_visible_agent`` helper as ``GET /agents/{id}`` — a foreign
    tenant's agent 404s with the byte-identical "Agent not found" literal.
    """
    from api.routes.agents import _load_visible_agent
    from api.routes.mcp_servers import get_service as get_mcp_service

    agent = await _load_visible_agent(agent_id, ctx)
    if not _is_provisioned(agent):
        return []

    assignments = await get_mcp_graph_service().list_agent_mcp_grants(agent.entra_sp_id)

    # Build the entra_sp_id → McpServer map (only provisioned MCPs carry an SP id).
    mcps = get_mcp_service().list()
    by_sp = {m.entra_sp_id: m for m in mcps if m.entra_sp_id}

    rows: List[AgentMcpGrant] = []
    for a in assignments:
        resource_sp_id = a.get("resourceId")
        mcp = by_sp.get(resource_sp_id)
        if mcp is None:
            # Not one of OUR MCP SPs (e.g. a Graph/non-MCP grant) — filter out.
            continue
        rows.append(
            AgentMcpGrant(
                mcp_id=mcp.id,
                mcp_name=a.get("resourceDisplayName") or mcp.name,
                role=_role_for_app_role_id(mcp, a.get("appRoleId")),
                assignment_id=a.get("id", ""),
            )
        )
    return rows
