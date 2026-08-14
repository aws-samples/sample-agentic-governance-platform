"""User → Agent grants (Entra app-role assignments) + principal search (Epic 6, T-ROUTES).

Grants are **live Microsoft Graph reads/writes — NO DynamoDB**: ``appRoleAssignedTo``
on the agent's service principal is the single source of truth. These routes are a
thin pass-through over :class:`GraphService` (T-GRAPH), mirroring the lazy
``get_service()`` singleton + RBAC + error→HTTP idiom of ``routes/agents.py``.

Two routers live here:
  - ``router`` (``prefix="/agents"``, tag ``"grants"``) — grants nested under the agent:
    ``GET/POST /agents/{id}/grants`` + ``DELETE /agents/{id}/grants/{assignment_id}``,
    plus (E29/T13, Databricks-governed agents only) ``GET /agents/{id}/grants/drift`` and
    ``POST /agents/{id}/grants/reassert``.
  - ``entra_router`` (``prefix="/entra"``, tag ``"entra"``) — the Graph-backed principal
    picker: ``GET /entra/principals/search?q=``.

The lazy ``get_graph_service()`` singleton defined here is the ONE source of the
``GraphService`` instance; ``routes/agents.py`` imports it (for ``/invoke`` + the
provisioning hook) rather than building its own, so the whole app shares one client.

E29/T13 (design §3A) adds ONE platform-specific behavior, entirely behind
``is_databricks_governed_agent``: on a Databricks-governed agent each USER assignment is
mirrored to a per-user ``CAN_USE`` entry on the agent's Databricks app, because the apps
proxy enforces that list itself. The Entra assignment remains the single source of truth and
the ACL is a one-way copy — never read back into Entra. AgentCore agents are unchanged.

Role mapping: Graph returns an ``appRoleId`` GUID; the route maps it to
``"Invoker"``/``"Admin"`` via the agent's stored ``invoker_role_id``/``admin_role_id``
(an unknown id → ``"Unknown"``). Conversely a create maps the requested role name back
to the agent's stored GUID (an unknown role name → 400).
"""

import logging
from typing import List, NamedTuple, Optional

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi import Depends as RBACDepends
from pydantic import BaseModel

from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from models.agent import is_databricks_governed_agent
from services.graph_service import GraphError, GraphService
from services.tenant_resolver import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["grants"])
entra_router = APIRouter(prefix="/entra", tags=["entra"])

# Minimum trimmed query length for the principal picker — a 1-char $search is noise
# (and Graph $search needs >= a couple chars to be useful). Below this → [].
_MIN_SEARCH_LEN = 2

# Lazy GraphService singleton — the ONE source of the instance, imported by
# routes/agents.py too (so /invoke + the provisioning hook share one client).
_graph_svc: Optional[GraphService] = None


def get_graph_service() -> GraphService:
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


async def get_tenant_ctx(
    principal: Principal = RBACDepends(current_principal),
) -> TenantContext:
    """Delegate to the ONE resolver-singleton accessor (E24 follow-up, L1).

    ``users.py`` owns the lazy ``TenantResolver`` singleton; this thin re-export
    gives grants.py its own per-request ``tenant_ctx`` dependency WITHOUT a second
    resolver copy — tests patch ``api.routes.users._tenant_resolver`` and the
    grant-list read gate observes it. Imported lazily to avoid an import cycle at
    module load (mirrors ``agents.get_tenant_ctx``).
    """
    from api.routes.users import get_tenant_ctx as _users_get_tenant_ctx

    return await _users_get_tenant_ctx(principal)


# --- models ------------------------------------------------------------------

class GrantCreate(BaseModel):
    principal_id: str        # Entra user/group object id
    principal_type: str      # "user" | "group" (display only; Graph assigns identically)
    role: str                # "Invoker" | "Admin"


class GrantRead(BaseModel):
    assignment_id: str       # appRoleAssignment id (for revoke)
    principal_id: str
    principal_display: str
    principal_type: str      # "User" | "Group" (Graph's principalType)
    role: str                # mapped from appRoleId via invoker_role_id/admin_role_id


class DriftEntry(BaseModel):
    """One disagreement between the Entra grant list and the Databricks app's ACL."""

    principal: str           # the Databricks user_name (or the oid AGP could not resolve)
    kind: str                # "user" | "group" | "service_principal" (see _diff_acl)
    level: str               # the platform's own permission word, passed through
    direction: str           # "unauthorized_acl" | "missing_acl"


class DriftRead(BaseModel):
    entries: List[DriftEntry]


class PrincipalHit(BaseModel):
    id: str
    display_name: str
    type: str                # "user" | "group"
    mail: Optional[str] = None


# --- helpers -----------------------------------------------------------------

def _role_for_app_role_id(agent, app_role_id: Optional[str]) -> str:
    """Map a Graph appRoleId GUID → "Invoker"/"Admin" via the agent's stored ids.

    An id matching neither (or a None id) → "Unknown" (tolerant — a role minted by
    another tool, or a stale/legacy assignment, must not break the list view).
    """
    if app_role_id and app_role_id == agent.invoker_role_id:
        return "Invoker"
    if app_role_id and app_role_id == agent.admin_role_id:
        return "Admin"
    return "Unknown"


def _app_role_id_for_role(agent, role: str) -> str:
    """Map a requested role name → the agent's stored appRoleId GUID.

    Raises ``HTTPException(400)`` for anything other than "Invoker"/"Admin".
    """
    if role == "Invoker":
        return agent.invoker_role_id
    if role == "Admin":
        return agent.admin_role_id
    raise HTTPException(status_code=400, detail="role must be 'Invoker' or 'Admin'")


def _is_provisioned(agent) -> bool:
    """An agent is grant-capable iff its identity is provisioned + it has an SP id."""
    return agent.identity_status == "provisioned" and bool(agent.entra_sp_id)


def _to_grant_read(agent, assignment: dict) -> GrantRead:
    return GrantRead(
        assignment_id=assignment.get("id", ""),
        principal_id=assignment.get("principalId", ""),
        principal_display=assignment.get("principalDisplayName") or "",
        principal_type=assignment.get("principalType") or "",
        role=_role_for_app_role_id(agent, assignment.get("appRoleId")),
    )


# --- Databricks platform-ACL mirror (E29/T13c+d, design §3A) -----------------
#
# On a Databricks-governed agent the Entra app-role assignment stays the SINGLE SOURCE OF
# TRUTH for "may X invoke this agent", and the app's per-user ``CAN_USE`` entry is its
# ONE-WAY MIRROR. The mirror exists because the Databricks apps proxy enforces its own door:
# a federated token for a user without ``CAN_USE`` is refused (401) BEFORE the app is
# reached, so an Entra grant that is not mirrored buys the user nothing, and an ACL entry
# nobody granted is access around the platform. AgentCore agents are untouched here, byte
# for byte — every step below is behind ``is_databricks_governed_agent``.

_ACL_CAN_USE = "CAN_USE"
_ACL_CAN_MANAGE = "CAN_MANAGE"
_ACL_KIND_USER = "user"
_ACL_KIND_GROUP = "group"
_ACL_KIND_SERVICE_PRINCIPAL = "service_principal"
# The asserted baseline's first entry (T13b's ``_ADMINS_ENTRY``). Restated from fixed text
# rather than imported from the identity service's privates — the workspace client REFUSES a
# PUT that omits it (``acl_missing_admins``), so a drift between the two spellings surfaces
# as a loud failure, never as a silent lockout.
_ACL_ADMINS_ENTRY = {"principal": "admins", "kind": "group", "level": _ACL_CAN_MANAGE}

# Graph's ``principalType`` for a user assignment — the only assignments §3A mirrors.
_PRINCIPAL_TYPE_USER = "User"

_DIRECTION_UNAUTHORIZED = "unauthorized_acl"
_DIRECTION_MISSING = "missing_acl"

# Every ACL failure answers with one of these FIXED literals. Never ``str(err)``, never an
# upstream body, never a workspace URL — the T-GRAPH no-leak convention this module already
# follows for Graph errors. The logs (kind-only) are where correlation happens.
_ACL_TARGET_UNRESOLVED = (
    "the agent's Databricks app could not be resolved, so its platform access list "
    "cannot be reached"
)
_ACL_READ_FAILED = "the agent's platform access list could not be read"
_ACL_WRITE_FAILED = "the agent's platform access list could not be written"
_GRANTS_READ_FAILED = "the agent's grant list could not be read"
_GRANT_NOT_APPLIED = "grant not applied; the platform ACL write failed"
_GRANT_NOT_APPLIED_NO_EMAIL = (
    "grant not applied; the grantee has no email or user principal name to mirror onto "
    "the platform access list"
)
_GRANT_ROLLBACK_FAILED = (
    "grant not applied on the platform AND the Entra assignment could not be rolled back "
    "— the agent is drifted; re-assert from the Access tab"
)
_REVOKE_ACL_FAILED = (
    "grant revoked; platform ACL removal failed — re-assert from the Access tab"
)
_NOT_DATABRICKS = "not a Databricks-governed agent"
# A group has no ``user_name``, so a group grant could never be mirrored onto the app's ACL —
# recording one would claim access the platform's own door refuses for every member.
_GROUP_GRANT_UNSUPPORTED = (
    "group grants are not enforceable on Databricks agents yet; grant individual users"
)


class _AclError(Exception):
    """A Databricks ACL step failed, carrying a SAFE fixed ``detail`` literal.

    An internal type rather than a raw ``HTTPException`` because the grant path has to
    ROLL BACK on failure: a step that raised the response directly could not be wrapped
    (the rollback would be skipped), and catching ``HTTPException`` to un-raise it is how
    a control-flow bug gets written.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class _AclTarget(NamedTuple):
    """Everything an app-ACL call needs: the client, the workspace, and the app's NAME.

    The permissions API is keyed on the app NAME (not a URL, not an id), and the name only
    exists in the workspace's own app listing — see
    ``DatabricksIdentityService._resolve_stage_and_app``, which is what produces this.
    """

    databricks: object
    stage: object
    token: str
    app_name: str

    @property
    def workspace_url(self) -> str:
        return getattr(self.stage, "workspace_url", "") or ""


async def _acl_target(agent) -> _AclTarget:
    """Resolve the agent's app + a workspace token, or raise :class:`_AclError`.

    REUSES ``DatabricksIdentityService._resolve_stage_and_app``: it picks the stage by
    LISTING each workspace's apps (evidence, not a parse of an Apps hostname — which carries
    no workspace identity), mints the workspace SP token from the stored credential, and
    returns the app record whose ``url`` matches the agent's handle. The private reach is the
    same bounded, documented idiom ``routes/agents.py::_databricks_agent_secret`` uses, and
    for the same reason: re-implementing stage resolution (or the secret read behind it) in a
    route would mean two rules about which workspace an agent lives in, and the looser one
    would be the bug. Nothing here reads a secret directly.
    """
    from api.routes.agents import (
        get_databricks_identity_service,
        get_databricks_workspace_service,
    )

    identity = get_databricks_identity_service()
    if identity is None:
        logger.warning(
            "[grants] no Databricks identity service is configured, so agent %s's platform "
            "access list cannot be reached",
            agent.id,
        )
        raise _AclError(_ACL_TARGET_UNRESOLVED)

    tenant_id = (agent.tenant_id or "").strip()
    if not tenant_id:
        raise _AclError(_ACL_TARGET_UNRESOLVED)
    # The tenants-route singleton — the SAME instance the admin routes write, so a tenant
    # connected a moment ago is visible here. Lazily imported (a module-level import is a
    # cycle), exactly as this module already imports from ``api.routes.agents``.
    from api.routes.tenants import get_tenant_service

    try:
        tenant = get_tenant_service().get(tenant_id)
    except Exception:  # noqa: BLE001 — TenantError, a store fault, anything: one safe answer
        tenant = None
    if tenant is None:
        logger.warning("[grants] could not resolve the tenant for agent %s", agent.id)
        raise _AclError(_ACL_TARGET_UNRESOLVED)

    try:
        stage, token, app = await identity._resolve_stage_and_app(agent, tenant)
    except Exception as err:  # noqa: BLE001 — ProvisioningError/DatabricksError: one code
        logger.warning(
            "[grants] could not resolve agent %s's Databricks app (%s)",
            agent.id,
            getattr(err, "kind", None) or type(err).__name__,
        )
        raise _AclError(_ACL_TARGET_UNRESOLVED) from None

    app_name = str((app or {}).get("name") or "")
    if not app_name:
        raise _AclError(_ACL_TARGET_UNRESOLVED)
    return _AclTarget(
        databricks=get_databricks_workspace_service(),
        stage=stage,
        token=token,
        app_name=app_name,
    )


async def _grantee_user_name(oid: str) -> str:
    """The Databricks ``user_name`` for an Entra user oid — ``mail``, else ``userPrincipalName``.

    Those are the two values a Databricks username actually carries (§3A). An oid resolving
    to neither returns ``""``: the caller decides what that means, because it means different
    things on a grant (refuse + roll back) and on a drift read (report the oid as
    ``missing_acl`` — an assignment AGP cannot mirror is exactly the state the operator needs
    to see).
    """
    if not oid:
        return ""
    try:
        principal = await get_graph_service().get_principal(oid, "user")
    except (GraphError, ValueError):
        return ""
    if not isinstance(principal, dict):
        return ""
    mail = str(principal.get("mail") or "").strip()
    return mail or str(principal.get("userPrincipalName") or "").strip()


class _AssignedUsers(NamedTuple):
    """The agent's USER assignments, split by whether AGP can name them on the platform.

    ``names`` is ``{casefolded user_name: user_name}`` — DEDUPED, because
    ``set_app_permissions`` refuses a list naming the same ``user_name`` twice
    (``acl_entry_invalid``, users compared casefolded) and one person holding both Invoker and
    Admin is two assignments but one ACL entry.

    ``unresolved`` holds the OIDS Entra reported neither a ``mail`` nor a UPN for. They are
    kept SEPARATE rather than folded in under their oid, because the two consumers need
    opposite things from them: drift must REPORT them (an assignment AGP cannot mirror is
    real, un-enforced access), while the re-assert must NOT WRITE them — an oid in the
    ``user_name`` slot is a grant to a principal nobody named, and it would make the next
    drift read see a matching entry and report convergence that never happened.
    """

    names: "dict[str, str]"
    unresolved: List[str]


async def _assigned_user_names(agent) -> _AssignedUsers:
    """Read the agent's assignments and split their USER half (see :class:`_AssignedUsers`).

    GROUP assignments are absent by design. §3A's mirror is per-USER — a group name has no
    ``user_name`` to write — which is exactly why ``create_grant`` REFUSES a group principal on
    a Databricks-governed agent (:data:`_GROUP_GRANT_UNSUPPORTED`): a group grant would
    authorize nobody at the app's door. The filter here stays as defence in depth (Graph
    reports the real ``principalType``; a request body only claims one) and covers any group
    assignment made before that refusal existed, or made outside AGP.
    """
    try:
        assignments = await get_graph_service().list_assignments(agent.entra_sp_id)
    except GraphError as err:
        logger.warning(
            "[grants] could not read agent %s's assignments for the ACL mirror (%s)",
            agent.id,
            err.status,
        )
        raise _AclError(_GRANTS_READ_FAILED) from None

    names: dict[str, str] = {}
    unresolved: List[str] = []
    for assignment in assignments:
        if str(assignment.get("principalType") or "") != _PRINCIPAL_TYPE_USER:
            continue
        oid = str(assignment.get("principalId") or "")
        if not oid:
            continue
        value = await _grantee_user_name(oid)
        if not value:
            if oid not in unresolved:
                unresolved.append(oid)
            continue
        names.setdefault(value.casefold(), value)
    return _AssignedUsers(names=names, unresolved=unresolved)


async def _read_acl(agent, target: _AclTarget) -> List[dict]:
    try:
        return await target.databricks.get_app_permissions(
            target.workspace_url, target.token, target.app_name
        )
    except Exception as err:  # noqa: BLE001 — DatabricksError or transport: one safe code
        logger.warning(
            "[grants] could not read agent %s's app access list (%s)",
            agent.id,
            getattr(err, "kind", None) or type(err).__name__,
        )
        raise _AclError(_ACL_READ_FAILED) from None


def _log_stripped_acl_entries(
    agent, app_name: str, current: List[dict], desired: List[dict]
) -> None:
    """Name every entry the re-assert PUT is about to remove, then count them.

    A LOCAL TWIN of ``DatabricksIdentityService._log_stripped_acl_entries``, same vocabulary
    and same grounds: ``set_app_permissions`` is a REPLACING PUT, and a takeover whose record
    omits WHO lost access is not a record. T13b logs it on the provisioning path; this is the
    same takeover on a live app, triggered by an operator's button, so it cannot log less.
    Principal names are directory identities, not secrets — the module's stated rule.

    (The duplication is deliberate for now: the service's helper is private and the service
    files are outside this task's manifest. Review M-2's proposed public ``resolve_app_target``
    is where both this and the target resolution should eventually live.)

    INHERITED entries are counted apart: a PUT cannot remove them, so calling one "stripped"
    would claim a takeover that did not happen. They survive and show up as drift instead.
    """
    wanted = {
        (str(e.get("principal") or ""), str(e.get("kind") or ""), str(e.get("level") or ""))
        for e in desired
    }
    unwanted = [
        e
        for e in current
        if (
            str(e.get("principal") or ""),
            str(e.get("kind") or ""),
            str(e.get("level") or ""),
        )
        not in wanted
    ]
    stripped = [e for e in unwanted if not e.get("inherited")]
    inherited = len(unwanted) - len(stripped)
    if inherited:
        logger.warning(
            "[grants] re-asserted ACL on app %s for agent %s: %d inherited entries survive "
            "the assert — AGP cannot remove them",
            app_name,
            agent.id,
            inherited,
        )
    if not stripped:
        return
    for entry in stripped:
        logger.warning(
            "[grants] agent %s: stripping app %s ACL entry — %s (%s) held %s",
            agent.id,
            app_name,
            entry.get("principal"),
            entry.get("kind"),
            entry.get("level"),
        )
    logger.warning(
        "[grants] re-asserted ACL on app %s for agent %s: stripped %d pre-existing entries",
        app_name,
        agent.id,
        len(stripped),
    )


def _baseline_principals(target: _AclTarget) -> set:
    """The ``(principal, kind, level)`` triples the assert OWNS — the only non-drift entries.

    Restated from the same two facts :func:`reassert_grants_acl` composes from — workspace
    ``admins`` at ``CAN_MANAGE`` and the tenant's workspace SP at ``CAN_MANAGE`` — and nothing
    else: with federation the only binding (§3B), the invoke identity is the CALLING USER, so no
    service principal but AGP's own writing credential belongs on the app.

    It is a triple, not a bare name, because both halves carry weight: an ACL *user* entry that
    happens to spell "admins" is a stranger at the door, not the workspace admins group, and the
    LEVEL is part of the match too — the workspace SP demoted to ``CAN_USE`` would make that the
    LAST ACL write AGP could perform, so it falls through to ``unauthorized_acl``, which the
    re-assert repairs since it composes the level back. Names are casefolded, matching the
    comparison the rest of the diff makes.

    If the dormant ``DATABRICKS_ALLOW_SP_SECRET_BINDING`` gate is ever re-enabled, the agent SP
    that ``_assert_app_acl`` writes at ``CAN_USE`` for that leg is NOT in this baseline: drift
    would report it as ``unauthorized_acl`` and Re-assert would STRIP it (breaking invoke until
    re-provision), so re-enabling the gate means restoring that entry here too.
    """
    triples = {
        (
            str(_ACL_ADMINS_ENTRY["principal"]).casefold(),
            _ACL_KIND_GROUP,
            _ACL_CAN_MANAGE,
        )
    }
    name = str(getattr(target.stage, "sp_client_id", "") or "").strip()
    if name:
        triples.add((name.casefold(), _ACL_KIND_SERVICE_PRINCIPAL, _ACL_CAN_MANAGE))
    return triples


def _diff_acl(
    acl: List[dict], assigned: _AssignedUsers, baseline: set
) -> List["DriftEntry"]:
    """The two-directional drift diff (design §3A). Compared casefolded.

    * ``unauthorized_acl`` — ANY direct, non-baseline entry: a user with no matching
      assignment, but also a hand-granted GROUP or SERVICE_PRINCIPAL. Access around the
      platform fails OPEN, so this is the loud direction and it must not have a blind spot —
      a group ``CAN_USE`` is the single widest way around AGP, and this diff is its only
      detector. Re-assert already strips them, so the repair and the detection now match.
    * ``missing_acl`` — an assignment with no ACL entry: a half-completed grant/revoke, or an
      admin removed the entry. USERS ONLY, because the mirror is per-user: there is no group
      or service-principal entry AGP is ever waiting to see (§3A, and ``create_grant``
      refuses a group principal outright).

    The two directions therefore have DIFFERENT SCOPES BY DESIGN: the diff answers "does the
    door list match the truth" (every entry counts), while the mirror answers "how grants are
    written" (per user). Anything wider than the mirror can only ever appear on the platform
    side, which is precisely the direction that must be reported.

    THE BASELINE IS NOT DRIFT — ``admins`` and the tenant workspace SP, and nothing else
    (§3B: federation is the only binding, so the invoke identity is the calling user). It is an
    EXPLICIT predicate (:func:`_baseline_principals`), because the kind filter no longer
    excludes it implicitly — and it matches on LEVEL too, so a baseline principal held at a
    level the assert never writes IS drift.

    INHERITED entries are not drift EITHER, but only in one direction, and the asymmetry is
    the point: a PUT cannot remove an inherited grant, so reporting one as
    ``unauthorized_acl`` would offer a Re-assert that cannot fix it — a lie. It still grants
    real access, so it DOES satisfy an assignment and therefore suppresses ``missing_acl``.
    """
    direct = {
        str(e.get("principal") or "").casefold()
        for e in acl
        if e.get("kind") == _ACL_KIND_USER and not e.get("inherited")
    }
    present = {
        str(e.get("principal") or "").casefold()
        for e in acl
        if e.get("kind") == _ACL_KIND_USER
    }

    granted = assigned.names
    entries: List[DriftEntry] = []
    seen: set = set()
    for entry in acl:
        if entry.get("inherited"):
            continue
        name = str(entry.get("principal") or "")
        kind = str(entry.get("kind") or "")
        level = str(entry.get("level") or "")
        # The LEVEL is part of the key: a baseline principal held at ANY level other than the
        # one the assert writes is not the baseline entry. Both baseline members sit at
        # CAN_MANAGE, so the reachable case is a DEMOTION — the workspace SP dropped to
        # CAN_USE, which costs AGP its ability to write this ACL at all (see
        # :func:`_baseline_principals`).
        key = (name.casefold(), kind, level)
        if not name or key in baseline or key in seen:
            continue
        # A USER entry is authorized by a matching assignment; nothing else can be — no
        # assignment ever produces a group or service-principal entry on this list. And it is
        # only authorized AT THE LEVEL the mirror writes: a granted user promoted to
        # CAN_MANAGE holds app-ACL-rewrite power the grant never conferred, caught by the same
        # level-is-part-of-the-match rule :func:`_baseline_principals` relies on. Re-assert repairs
        # it (it composes CAN_USE), so the detection must match the repair.
        if (
            kind == _ACL_KIND_USER
            and name.casefold() in granted
            and level == _ACL_CAN_USE
        ):
            continue
        seen.add(key)
        entries.append(
            DriftEntry(
                principal=name,
                kind=kind,
                level=level,
                direction=_DIRECTION_UNAUTHORIZED,
            )
        )
    # An assignment AGP cannot even NAME on the platform is reported under its oid: it is
    # access the platform is not enforcing, and the operator cannot act on what is not shown.
    for value in list(granted.values()) + assigned.unresolved:
        key = value.casefold()
        if key in present or key in direct:
            continue
        entries.append(
            DriftEntry(
                principal=value,
                kind=_ACL_KIND_USER,
                # The level AGP would have written — the entry is missing, so there is no
                # platform level to report, and inventing another word would teach a
                # vocabulary the product does not own.
                level=_ACL_CAN_USE,
                direction=_DIRECTION_MISSING,
            )
        )
    return entries


async def _drift(
    agent, target: _AclTarget, assigned: Optional[_AssignedUsers] = None
) -> "DriftRead":
    """Read both sides and diff them. Raises :class:`_AclError` on either read.

    ``assigned`` lets a caller that ALREADY resolved the Entra side pass it in instead of
    paying for it twice: :func:`_assigned_user_names` is one ``list_assignments`` plus one
    UNCACHED ``get_principal`` per assignment, so the re-assert path would otherwise cost
    ``2 + 2N`` Graph round-trips on a write that sits under the API Gateway 30s ceiling.
    Reusing it is safe in exactly this case — AGP's own ACL PUT cannot change Entra state.
    The ACL side is ALWAYS re-read, because that is the side the PUT just changed.
    """
    if assigned is None:
        assigned = await _assigned_user_names(agent)
    acl = await _read_acl(agent, target)
    return DriftRead(entries=_diff_acl(acl, assigned, _baseline_principals(target)))


async def _mirror_grant(agent, assignment: dict) -> None:
    """Write the per-user ``CAN_USE`` that makes the fresh assignment real, or ROLL IT BACK.

    §3A: a grant exists in BOTH places or in NEITHER. That is a DELIBERATE DIVERGENCE from
    the multi-write idiom next door — ``mcp_server_grants`` / ``agent_mcp_grant`` are
    fail-loud + RETRY-FORWARD ("role assigned but … wiring failed; re-grant to retry") and
    keep the Entra assignment. The difference is what a half-done write means: a half-done
    MCP grant leaves an authorization that is REAL but undelivered, while a half-done
    Databricks grant leaves AGP's Access tab claiming access the platform's own door still
    refuses — the platform would be lying about who can reach the agent, which is precisely
    what this epic exists to stop. So the assignment goes back.

    A NON-USER assignment is REFUSED here, not skipped. This is the real enforcement point for
    :data:`_GROUP_GRANT_UNSUPPORTED`: ``body.principal_type`` is display-only and never reaches
    Graph (``assign_app_role`` sends only ids — Entra resolves the type from the oid), so a
    group oid labelled ``"user"`` sails past ``create_grant``'s fast-path check. Graph's
    ``principalType`` on the assignment it just created is the only truthful answer, and it is
    in hand right here — so the assignment goes back and the 422 is raised from here.

    If the rollback itself fails the response names BOTH failures: the record is then genuinely
    drifted, and the drift read is what surfaces it.
    """
    if str(assignment.get("principalType") or "") != _PRINCIPAL_TYPE_USER:
        try:
            await get_graph_service().revoke_app_role(
                agent.entra_sp_id, str(assignment.get("id") or "")
            )
        except Exception as err:  # noqa: BLE001 — same drifted state as any failed rollback
            logger.warning(
                "[grants] agent %s: a non-user assignment cannot be mirrored AND could not "
                "be rolled back (%s)",
                agent.id,
                getattr(err, "status", None) or type(err).__name__,
            )
            raise HTTPException(status_code=502, detail=_GRANT_ROLLBACK_FAILED) from None
        raise HTTPException(status_code=422, detail=_GROUP_GRANT_UNSUPPORTED)

    user_name = await _grantee_user_name(str(assignment.get("principalId") or ""))
    detail = _GRANT_NOT_APPLIED_NO_EMAIL if not user_name else ""
    if user_name:
        try:
            target = await _acl_target(agent)
            await target.databricks.grant_app_can_use(
                target.workspace_url,
                target.token,
                target.app_name,
                user_name,
                kind=_ACL_KIND_USER,
            )
        except _AclError:
            detail = _GRANT_NOT_APPLIED
        except Exception as err:  # noqa: BLE001 — DatabricksError or transport: one code
            logger.warning(
                "[grants] the platform ACL grant failed for agent %s (%s)",
                agent.id,
                getattr(err, "kind", None) or type(err).__name__,
            )
            detail = _GRANT_NOT_APPLIED
    if not detail:
        return

    try:
        await get_graph_service().revoke_app_role(
            agent.entra_sp_id, str(assignment.get("id") or "")
        )
    except Exception as err:  # noqa: BLE001 — GraphError or transport: same drifted state
        logger.warning(
            "[grants] agent %s: the platform ACL write failed AND the Entra assignment "
            "could not be rolled back (%s)",
            agent.id,
            getattr(err, "status", None) or type(err).__name__,
        )
        raise HTTPException(status_code=502, detail=_GRANT_ROLLBACK_FAILED) from None
    raise HTTPException(status_code=502, detail=detail)


async def _revoke_mirror_target(agent, assignment_id: str) -> Optional[str]:
    """The ``user_name`` whose ACL entry a pending revoke must remove.

    Resolved BEFORE the destructive Entra call — the ordering ``agent_mcp_grant``'s revoke
    already establishes: once the assignment is deleted, the principal behind it is no longer
    readable from Graph, so a mirror resolved afterwards would be guesswork.

    ``None`` = nothing to mirror (a group assignment, or an id the assignment list does not
    carry — a stale/double-click revoke, which the Entra call answers with its own 404).
    ``""`` = a user whose name AGP could not resolve, INCLUDING the case where the assignment
    list itself was unreadable: the revoke still proceeds (§3A never resurrects an
    assignment), and the caller reports the ACL half as failed, which is honest — the entry
    may well still be there.
    """
    try:
        assignments = await get_graph_service().list_assignments(agent.entra_sp_id)
    except GraphError:
        logger.warning(
            "[grants] agent %s: the assignment list was unreadable, so the revoke's ACL "
            "mirror target is unknown",
            agent.id,
        )
        return ""
    match = next(
        (a for a in assignments if str(a.get("id") or "") == assignment_id), None
    )
    if match is None:
        return None
    if str(match.get("principalType") or "") != _PRINCIPAL_TYPE_USER:
        return None
    return await _grantee_user_name(str(match.get("principalId") or ""))


async def _mirror_revoke(agent, target_user: str) -> None:
    """Remove the user's ACL entry after the Entra assignment is already gone."""
    try:
        target = await _acl_target(agent)
        await target.databricks.revoke_app_can_use(
            target.workspace_url,
            target.token,
            target.app_name,
            target_user,
            _ACL_KIND_USER,
        )
    except _AclError:
        raise HTTPException(status_code=502, detail=_REVOKE_ACL_FAILED) from None
    except Exception as err:  # noqa: BLE001 — DatabricksError or transport: one safe code
        logger.warning(
            "[grants] the platform ACL removal failed for agent %s (%s)",
            agent.id,
            getattr(err, "kind", None) or type(err).__name__,
        )
        raise HTTPException(status_code=502, detail=_REVOKE_ACL_FAILED) from None


def _databricks_acl_gate(agent) -> None:
    """The two 409s the drift + re-assert routes share.

    Non-Databricks → 409: an AgentCore agent has no platform ACL at all, and answering with
    an empty drift list would claim "checked, all clean" about a surface that does not exist.
    Not provisioned → 409 on the SAME gate the mutations use: there is no asserted list to
    diff yet, so a diff would report every assignment as ``missing_acl``.
    """
    if not is_databricks_governed_agent(agent):
        raise HTTPException(status_code=409, detail=_NOT_DATABRICKS)
    if not _is_provisioned(agent):
        raise HTTPException(status_code=409, detail="agent identity is not provisioned")


# --- grants routes -----------------------------------------------------------

@router.get("/{agent_id}/grants", response_model=List[GrantRead])
async def list_grants(
    agent_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """List the agent's app-role assignments (the access list).

    Unprovisioned (no SP / status != provisioned) → ``[]`` (NOT 409 — the FE shows a
    banner). Otherwise read live from Graph + map each appRoleId → Invoker/Admin.

    Tenant gate (E24 follow-up, review L1): the read is gated on the SAME
    ``_load_visible_agent`` helper as the parent ``GET /agents/{id}`` — a foreign
    tenant's agent 404s with the byte-identical "Agent not found" literal, so the
    sub-resource read can never enumerate an agent the detail route says is absent.
    """
    # Local import avoids a circular import at module load (agents.py imports this
    # module's get_graph_service; we read its helpers lazily inside the handler).
    from api.routes.agents import _load_visible_agent

    agent = await _load_visible_agent(agent_id, ctx)

    if not _is_provisioned(agent):
        return []

    assignments = await get_graph_service().list_assignments(agent.entra_sp_id)
    return [_to_grant_read(agent, a) for a in assignments]


@router.post("/{agent_id}/grants", response_model=GrantRead, status_code=201)
async def create_grant(
    agent_id: str,
    body: GrantCreate,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Grant a user/group an Invoker/Admin app role on the agent.

    409 if the agent's identity isn't provisioned (no SP to assign on). 400 if the
    role isn't Invoker/Admin. A Graph already-exists/409 → 409. 422 for a GROUP
    principal on a Databricks-governed agent (E29/T13c, §3A — see below).

    Cross-tenant grant guard (E24/T7): a NON-admin may only grant when the GRANTEE
    (``body.principal_id`` — a USER or a GROUP, per the E6 Access tab) belongs to
    the agent's tenant. The grantee's tenant set is resolved via
    ``TenantResolver.resolve_oid_tenants``: the user path (Graph group memberships)
    UNIONED with the direct-group path (the oid itself in a tenant's
    ``entra_group_ids`` — so a same-tenant GROUP grantee passes at OPERATOR without
    a Graph resolve). Unresolvable → empty set (GraphError degrades, fail-closed).
    Anything else (foreign-tenant grantee, unresolvable grantee, OR a legacy agent
    with ``tenant_id=None``) is a cross-tenant grant → ADMIN only, 403 with a FIXED
    literal. The guard runs BEFORE the Graph assign; revoke + list are untouched.
    """
    from api.routes.agents import get_service

    agent = get_service().get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not _is_provisioned(agent):
        raise HTTPException(
            status_code=409, detail="agent identity is not provisioned"
        )

    app_role_id = _app_role_id_for_role(agent, body.role)

    # E29/T13c (§3A) — REFUSE a group principal on a Databricks-governed agent, BEFORE any
    # write: a group has no `user_name`, so the grant could never be mirrored onto the app's
    # ACL, and recording it would have the Access tab claim access the platform's own door
    # refuses for every member of that group. A 4xx and not a rollback — nothing is written.
    # This is only the FAST PATH: `principal_type` is display-only and never reaches Graph, so
    # a mislabelled group oid is caught in `_mirror_grant` against Graph's truthful answer.
    if (
        is_databricks_governed_agent(agent)
        and body.principal_type.strip().casefold() == _ACL_KIND_GROUP
    ):
        raise HTTPException(status_code=422, detail=_GROUP_GRANT_UNSUPPORTED)

    if principal.role < Role.ADMIN:
        # An unstamped agent (tenant_id=None, pre-E24 legacy) can never be
        # "same-tenant" — fail closed WITHOUT the Graph read (nothing to match).
        if agent.tenant_id is None:
            raise HTTPException(
                status_code=403, detail="cross-tenant grant requires admin"
            )
        # Lazy import: users.py owns the ONE TenantResolver singleton (E24/T5) —
        # tests patch ``api.routes.users._tenant_resolver`` and this guard sees it.
        from api.routes.users import get_tenant_resolver

        grantee_tenants = await get_tenant_resolver().resolve_oid_tenants(
            body.principal_id
        )
        if agent.tenant_id not in grantee_tenants:
            raise HTTPException(
                status_code=403, detail="cross-tenant grant requires admin"
            )

    try:
        assignment = await get_graph_service().assign_app_role(
            agent.entra_sp_id, body.principal_id, app_role_id
        )
    except GraphError as err:
        if err.status in (400, 409):
            # Already-assigned (or a duplicate) — surface as a conflict.
            raise HTTPException(
                status_code=409, detail="principal is already assigned to this agent"
            )
        raise HTTPException(status_code=502, detail="failed to assign the role")

    # E29/T13c — MIRROR the assignment onto the Databricks app's ACL (§3A). AgentCore agents
    # skip this entirely; a Databricks-governed agent's grant is not finished until the
    # platform's own door lists the user, and _mirror_grant rolls the assignment back if it
    # cannot be. Deliberately AFTER the assignment: Entra stays the source of truth, so the
    # mirror is derived from a fact that already exists.
    if is_databricks_governed_agent(agent):
        await _mirror_grant(agent, assignment)
    return _to_grant_read(agent, assignment)


@router.delete("/{agent_id}/grants/{assignment_id}", status_code=204)
async def delete_grant(
    agent_id: str,
    assignment_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Revoke an app-role assignment by id → 204.

    Guards (mirroring create_grant): 409 if the agent isn't provisioned (no SP id —
    otherwise we'd hit a malformed ``/servicePrincipals/None/...`` path), and a
    ``GraphError`` from revoke maps 404→404 (a stale/already-deleted assignment_id, the
    FE double-click race) / other→409, so neither surfaces as a raw 500.
    """
    from api.routes.agents import get_service

    agent = get_service().get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not _is_provisioned(agent):
        raise HTTPException(
            status_code=409, detail="agent identity is not provisioned"
        )

    # E29/T13c — resolve the ACL mirror target BEFORE the destructive call (§3A ordering):
    # after the assignment is deleted the principal behind it is no longer readable.
    mirror_user = (
        await _revoke_mirror_target(agent, assignment_id)
        if is_databricks_governed_agent(agent)
        else None
    )

    try:
        await get_graph_service().revoke_app_role(agent.entra_sp_id, assignment_id)
    except GraphError as err:
        if err.status == 404:
            # Already revoked (FE double-click race) — the assignment is gone.
            raise HTTPException(status_code=404, detail="grant not found")
        raise HTTPException(status_code=409, detail="failed to revoke the grant")

    # ENTRA FIRST, ACL SECOND, and NEVER a resurrection (§3A): the governed invoke path dies
    # with the assignment even if this second write fails. A failure here leaves the agent
    # drifted and retryable — which is what the drift read reports and Re-assert repairs —
    # so it answers 502 with that instruction instead of undoing the revoke.
    if mirror_user is not None:
        if not mirror_user:
            raise HTTPException(status_code=502, detail=_REVOKE_ACL_FAILED)
        await _mirror_revoke(agent, mirror_user)
    return Response(status_code=204)


# --- platform-ACL drift + re-assert (E29/T13d, design §3A) -------------------

@router.get("/{agent_id}/grants/drift", response_model=DriftRead)
async def get_grants_drift(
    agent_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """Diff the agent's Entra grants against its Databricks app ACL, both directions.

    A READ, gated exactly like ``list_grants``: VIEWER + the shared ``_load_visible_agent``,
    so a foreign tenant's agent 404s with the byte-identical "Agent not found" and this
    sub-resource can never enumerate an agent the detail route says is absent.

    DETECTION IS AUTOMATIC, REPAIR IS A HUMAN'S BUTTON (§3A). This route never writes:
    silently re-asserting would hide the fact that someone is editing the app's ACL around
    the platform, which is the one fact an FSI operator needs to see.
    """
    from api.routes.agents import _load_visible_agent

    agent = await _load_visible_agent(agent_id, ctx)
    _databricks_acl_gate(agent)

    try:
        return await _drift(agent, await _acl_target(agent))
    except _AclError as err:
        raise HTTPException(status_code=502, detail=err.detail) from None


@router.post("/{agent_id}/grants/reassert", response_model=DriftRead)
async def reassert_grants_acl(
    agent_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Rewrite the app's ACL from AGP's grants, then answer with FRESH drift.

    RBAC is the SAME threshold as the grant mutations (OPERATOR), not admin-only: this
    rewrites derived state from grants an operator may already create and delete, and the
    Access tab gates the button on that same ``canManage``. A stricter gate here would put a
    403 behind a button the tab shows.

    The desired list is COMPOSED, never read-modified — the assert's whole value is that it
    is a function of Entra state plus the tenant, so a re-assert after a half-completed write
    converges instead of preserving whatever it finds:

      * ``admins`` at ``CAN_MANAGE`` — workspace admins stay above the governance layer, and
        the client refuses a PUT without it.
      * the tenant's workspace SP at ``CAN_MANAGE`` — the credential AGP itself writes with.
        Dropping it would make this the LAST ACL write AGP could ever perform on the app.
      * one ``CAN_USE`` per ASSIGNED USER whose username Entra could name, casefold-deduped by
        :func:`_assigned_user_names`.

    NO service principal beyond that one: federation is the only binding (§3B), so the invoke
    identity is the CALLING USER and an agent-SP entry would be a standing non-user path to the
    app that no grant confers. The composed list is exactly what :func:`_baseline_principals`
    treats as owned — anything else here would make every re-assert immediately self-drift.

    ``_to_write_shape`` is deliberately NOT used (the same call T13b made): it collapses a
    READ into a writable shape, and nothing here is read-fed — and it cannot casefold
    ``user_name``, which is the one duplicate this composer can actually produce.

    The PUT REPLACES the list, so the current ACL is read first and every entry the write will
    remove is logged by name (:func:`_log_stripped_acl_entries`) — the same record T13b makes
    for the same takeover.

    The answer is a fresh drift read rather than an ack, so the caller sees the state that
    resulted. Non-empty entries after a successful PUT are honest, not a failure: an
    inherited grant or an assignment with no resolvable username cannot be written away.
    """
    from api.routes.agents import _load_visible_agent

    agent = await _load_visible_agent(agent_id, ctx)
    _databricks_acl_gate(agent)

    try:
        target = await _acl_target(agent)
        assigned = await _assigned_user_names(agent)

        # The stage's SP client id is non-empty by construction: `_resolve_stage_and_app`
        # reached this stage by MINTING a token from it. One rule about it, stated where
        # T13b states it — not a second guard.
        workspace_sp = str(getattr(target.stage, "sp_client_id", "") or "").strip()
        desired: List[dict] = [
            dict(_ACL_ADMINS_ENTRY),
            {
                "principal": workspace_sp,
                "kind": _ACL_KIND_SERVICE_PRINCIPAL,
                "level": _ACL_CAN_MANAGE,
            },
        ]
        # RESOLVED usernames only. An assignment whose oid resolves to neither a mail nor a
        # UPN is left OFF the list and comes back as `missing_acl` below — writing the oid
        # into the `user_name` slot would grant a principal nobody named AND make the next
        # drift read report convergence that never happened.
        for value in assigned.names.values():
            desired.append(
                {"principal": value, "kind": _ACL_KIND_USER, "level": _ACL_CAN_USE}
            )

        # READ BEFORE THE WRITE, purely for the record: the PUT replaces the list, so after it
        # the entries it removed are unrecoverable and nobody can answer "who lost access at
        # 14:07?". The post-write drift read below cannot serve as that record — it runs after
        # the fact. One extra GET on a rare, human-triggered write.
        current = await _read_acl(agent, target)
        _log_stripped_acl_entries(agent, target.app_name, current, desired)

        try:
            await target.databricks.set_app_permissions(
                target.workspace_url, target.token, target.app_name, desired
            )
        except Exception as err:  # noqa: BLE001 — DatabricksError or transport: one code
            logger.warning(
                "[grants] the platform ACL re-assert failed for agent %s (%s)",
                agent.id,
                getattr(err, "kind", None) or type(err).__name__,
            )
            raise _AclError(_ACL_WRITE_FAILED) from None

        # The Entra side is passed BACK IN rather than resolved a second time (see `_drift`):
        # AGP's own PUT cannot have changed it, and re-resolving it would double the Graph
        # fan-out on the one path with a write already on the clock.
        return await _drift(agent, target, assigned)
    except _AclError as err:
        raise HTTPException(status_code=502, detail=err.detail) from None


# --- principal search (separate /entra router) -------------------------------

@entra_router.get("/principals/search", response_model=List[PrincipalHit])
async def search_principals(
    q: str = Query(default=""),
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """Graph ``$search`` users + groups for the picker. A too-short query → ``[]``."""
    if len(q.strip()) < _MIN_SEARCH_LEN:
        return []

    hits = await get_graph_service().search_principals(q.strip())
    return [
        PrincipalHit(
            id=h.get("id", ""),
            display_name=h.get("displayName") or "",
            type=h.get("type") or "",
            mail=h.get("mail"),
        )
        for h in hits
    ]
