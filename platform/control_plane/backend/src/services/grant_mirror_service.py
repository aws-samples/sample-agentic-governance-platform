"""User-to-agent grants: the Entra assignment, and its mirror onto a Databricks app's access list.

The routes keep RBAC, the tenant gate and the wire shapes; everything about how a grant becomes real
lives here. ``assign_agent_role`` and ``revoke_agent_role`` are the Entra writes every agent shares
and the point where a Graph refusal gets its meaning. The rest is the platform-ACL mirror, every
step of which is behind ``is_databricks_governed_agent``.

The doctrine, stated once:

* **Entra is the single source of truth.** The agent's ``appRoleAssignedTo`` list answers "may X
  invoke this agent". The Databricks app's per-user ``CAN_USE`` entry is its one-way mirror and is
  never read back into Entra. AgentCore agents have no mirror at all.
* **Why the mirror exists.** The Databricks apps proxy enforces its own door: a federated token for
  a user without ``CAN_USE`` is refused before the app is reached. An Entra grant that is not
  mirrored buys the user nothing, and an ACL entry nobody granted is access around the platform.
* **A grant exists in both places or in neither.** On a fresh assignment the ACL write follows the
  Entra write, and a failure rolls the assignment back. The MCP grants next door are fail-loud and
  retry-forward instead: a half-done MCP grant leaves an authorization that is real but
  undelivered, while a half-done Databricks grant leaves the Access tab claiming access the
  platform's own door refuses, which is the lie this product exists to stop.
* **Revoke is Entra first, ACL second, and never a resurrection.** The governed invoke path dies
  with the assignment even if the ACL removal fails; that failure leaves the agent drifted and
  retryable, which the drift read reports and Re-assert repairs.
* **Detection is automatic, repair is a human's button.** The drift read never writes: silently
  re-asserting would hide that someone edits the app's ACL around the platform.
* **Group grants are refused, not skipped.** A group has no ``user_name``, so a group grant could
  never be mirrored; recording one would claim access the door refuses for every member.
* **Every failure answers with a fixed literal.** Never ``str(err)``, never an upstream body, never
  a workspace URL. The logs, kind only, are where correlation happens.

Collaborators are reached through the route-level lazy accessors (``api.routes.grants``'s Graph
singleton, ``api.routes.agents``'s two Databricks accessors, ``api.routes.tenants``'s tenant
service), imported inside the methods: that keeps this module free of import cycles and keeps one
instance of each client per process, the same singletons the routes and the tests seed.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import ClassVar, List, Literal, NamedTuple, Optional, get_args

from pydantic import Field

from core.errors import AppError, Outcome
from models.agent import is_databricks_governed_agent
from models.base import WireModel
from services.databricks_workspace_service import AccessDriftKind, DatabricksWorkspaceService
from services.graph_errors import GraphError
from services.graph_service import GraphService

logger = logging.getLogger(__name__)

AccessDriftDirection = Literal["unauthorized_acl", "missing_acl"]


class DriftEntry(WireModel):
    """One disagreement between the Entra grant list and the Databricks app's ACL."""

    principal: str = Field(description="The Databricks user_name, or the oid AGP could not resolve.")
    kind: AccessDriftKind = Field(
        description="'user', 'group' or 'service_principal', as the platform reports it."
    )
    level: str = Field(description="The platform's own permission word, passed through.")
    direction: AccessDriftDirection = Field(description="'unauthorized_acl' or 'missing_acl'.")


class DriftRead(WireModel):
    entries: List[DriftEntry]


ACL_CAN_USE = "CAN_USE"
ACL_CAN_MANAGE = "CAN_MANAGE"
ACL_KIND_USER: AccessDriftKind = "user"
ACL_KIND_GROUP: AccessDriftKind = "group"
ACL_KIND_SERVICE_PRINCIPAL: AccessDriftKind = "service_principal"
_ACL_KINDS: tuple[AccessDriftKind, ...] = get_args(AccessDriftKind)
ACL_ADMINS_ENTRY = {"principal": "admins", "kind": "group", "level": ACL_CAN_MANAGE}

PRINCIPAL_TYPE_USER = "User"

DIRECTION_UNAUTHORIZED: AccessDriftDirection = "unauthorized_acl"
DIRECTION_MISSING: AccessDriftDirection = "missing_acl"

_ALREADY_ASSIGNED = "principal is already assigned to this agent"
_ASSIGN_FAILED = "failed to assign the role"
_ALREADY_REVOKED = "grant not found"
_REVOKE_FAILED = "failed to revoke the grant"
_ACL_TARGET_UNRESOLVED = (
    "the agent's Databricks app could not be resolved, so its platform access list cannot be reached"
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
REVOKE_ACL_FAILED = "grant revoked; platform ACL removal failed — re-assert from the Access tab"
_NOT_DATABRICKS = "not a Databricks-governed agent"
_NOT_PROVISIONED = "agent identity is not provisioned"
GRANT_ROLE_INVALID = "role must be 'Invoker' or 'Admin'"
CROSS_TENANT_GRANT_REQUIRES_ADMIN = "cross-tenant grant requires admin"
GROUP_GRANT_UNSUPPORTED = "group grants are not enforceable on Databricks agents yet; grant individual users"


class GrantMirrorError(AppError):
    """A grant step was refused or failed: the Entra assignment, or its mirror onto the app's ACL.

    Every detail is a fixed literal of this module and the message is that same literal, so the
    wire never carries an upstream failure's text, a response body or a workspace URL. A domain
    error rather than a response because the grant path rolls back on failure: a step that raised
    the response could not be wrapped, and the rollback would be skipped.
    """

    domain: ClassVar[str] = "grant"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "already_assigned": Outcome(409, _ALREADY_ASSIGNED),
        "assign_failed": Outcome(502, _ASSIGN_FAILED),
        "already_revoked": Outcome(404, _ALREADY_REVOKED),
        "revoke_failed": Outcome(409, _REVOKE_FAILED),
        "not_databricks": Outcome(409, _NOT_DATABRICKS),
        "not_provisioned": Outcome(409, _NOT_PROVISIONED),
        "invalid_role": Outcome(400, GRANT_ROLE_INVALID),
        "cross_tenant": Outcome(403, CROSS_TENANT_GRANT_REQUIRES_ADMIN),
        "group_unsupported": Outcome(422, GROUP_GRANT_UNSUPPORTED),
        "target_unresolved": Outcome(502, _ACL_TARGET_UNRESOLVED),
        "grants_read_failed": Outcome(502, _GRANTS_READ_FAILED),
        "acl_read_failed": Outcome(502, _ACL_READ_FAILED),
        "acl_write_failed": Outcome(502, _ACL_WRITE_FAILED),
        "not_applied": Outcome(502, _GRANT_NOT_APPLIED),
        "grantee_unnamed": Outcome(502, _GRANT_NOT_APPLIED_NO_EMAIL),
        "rollback_failed": Outcome(502, _GRANT_ROLLBACK_FAILED),
        "revoke_acl_failed": Outcome(502, REVOKE_ACL_FAILED),
    }

    def __init__(self, *, kind: str) -> None:
        detail = type(self).kinds[kind].detail
        super().__init__(detail if isinstance(detail, str) else kind, kind)


class AclTarget(NamedTuple):
    """Everything an app-ACL call needs: the client, the workspace, and the app's name.

    The permissions API is keyed on the app name, not a URL or an id, and the name only exists in
    the workspace's own app listing; ``DatabricksIdentityService._resolve_stage_and_app`` produces it.
    """

    databricks: DatabricksWorkspaceService
    stage: object
    token: str
    app_name: str

    @property
    def workspace_url(self) -> str:
        return getattr(self.stage, "workspace_url", "") or ""


class AssignedUsers(NamedTuple):
    """The agent's user assignments, split by whether AGP can name them on the platform.

    ``names`` is ``{casefolded user_name: user_name}``, deduplicated because ``set_app_permissions``
    refuses a list naming the same ``user_name`` twice and one person holding both Invoker and
    Admin is two assignments but one ACL entry. ``unresolved`` holds the oids Entra reported neither
    a ``mail`` nor a UPN for, kept apart because the two consumers need opposite things: drift
    must report them (an assignment AGP cannot mirror is real, unenforced access) while the
    re-assert must not write them (an oid in the ``user_name`` slot would grant a principal nobody
    named and make the next drift read report a convergence that never happened).
    """

    names: dict[str, str]
    unresolved: List[str]


def is_provisioned(agent) -> bool:
    """An agent is grant-capable iff its identity is provisioned and it has an SP id."""
    return agent.identity_status == "provisioned" and bool(agent.entra_sp_id)


async def assign_agent_role(graph: GraphService, agent, principal_id: str, app_role_id: str) -> dict:
    """Assign the app role on the agent's SP and return the new assignment.

    A Graph 400 or 409 means the principal already holds a role on this agent; any other
    ``GraphError`` is ``assign_failed``.
    """
    try:
        return await graph.assign_app_role(agent.entra_sp_id, principal_id, app_role_id)
    except GraphError as err:
        if err.status in (400, 409):
            raise GrantMirrorError(kind="already_assigned") from err
        raise GrantMirrorError(kind="assign_failed") from err


async def revoke_agent_role(graph: GraphService, agent, assignment_id: str) -> None:
    """Delete the assignment on the agent's SP.

    A Graph 404 means it is already gone (a stale id, or the second click of a double click); any
    other ``GraphError`` is ``revoke_failed``.
    """
    try:
        await graph.revoke_app_role(agent.entra_sp_id, assignment_id)
    except GraphError as err:
        if err.status == 404:
            raise GrantMirrorError(kind="already_revoked") from err
        raise GrantMirrorError(kind="revoke_failed") from err


def _baseline_principals(target: AclTarget) -> set:
    """The ``(principal, kind, level)`` triples the assert owns, the only non-drift entries.

    Workspace ``admins`` at ``CAN_MANAGE`` and the tenant's workspace SP at ``CAN_MANAGE``, nothing
    else: with federation the only binding, the invoke identity is the calling user, so no service
    principal but AGP's own writing credential belongs on the app. One function for both sides,
    because the drift read and the re-assert write must agree about what is owned, or a repair
    that writes an entry the detector calls drift makes every re-assert immediately self-drift.
    A triple, not a bare name: an ACL user entry spelled "admins" is a stranger at the door, and the
    workspace SP demoted to ``CAN_USE`` would make that the last ACL write AGP could perform, so it
    falls through to ``unauthorized_acl`` and the re-assert composes the level back. Names are
    casefolded. If the ``DATABRICKS_ALLOW_SP_SECRET_BINDING`` gate is ever re-enabled, the agent SP
    that leg writes at ``CAN_USE`` must be added here too, or drift would report it and Re-assert
    would strip it.
    """
    triples = {
        (
            str(ACL_ADMINS_ENTRY["principal"]).casefold(),
            ACL_KIND_GROUP,
            ACL_CAN_MANAGE,
        )
    }
    name = str(getattr(target.stage, "sp_client_id", "") or "").strip()
    if name:
        triples.add((name.casefold(), ACL_KIND_SERVICE_PRINCIPAL, ACL_CAN_MANAGE))
    return triples


def _diff_acl(acl: List[dict], assigned: AssignedUsers, baseline: set) -> List[DriftEntry]:
    """The two-directional drift diff, compared casefolded.

    ``unauthorized_acl`` is any direct, non-baseline entry: a user with no matching assignment, but
    also a hand-granted group or service principal, because access around the platform fails open
    and a group ``CAN_USE`` is the widest way around AGP. A granted user is authorized only at the
    ``CAN_USE`` the mirror writes; promoted to ``CAN_MANAGE`` the entry is drift, which the re-assert
    repairs. ``missing_acl`` is an assignment with no ACL entry, users only, because the mirror is
    per user. The baseline is not drift, and it matches on level too. Inherited entries are not
    ``unauthorized_acl``, because a PUT cannot remove them, but they do satisfy an assignment and so
    suppress ``missing_acl``. An assignment AGP cannot name is reported under its oid at the level
    AGP would have written.
    """
    direct = {
        str(e.get("principal") or "").casefold()
        for e in acl
        if e.get("kind") == ACL_KIND_USER and not e.get("inherited")
    }
    present = {str(e.get("principal") or "").casefold() for e in acl if e.get("kind") == ACL_KIND_USER}

    granted = assigned.names
    entries: List[DriftEntry] = []
    seen: set = set()
    for entry in acl:
        if entry.get("inherited"):
            continue
        name = str(entry.get("principal") or "")
        kind = str(entry.get("kind") or "")
        level = str(entry.get("level") or "")
        key = (name.casefold(), kind, level)
        if not name or kind not in _ACL_KINDS or key in baseline or key in seen:
            continue
        if kind == ACL_KIND_USER and name.casefold() in granted and level == ACL_CAN_USE:
            continue
        seen.add(key)
        entries.append(
            DriftEntry(
                principal=name,
                kind=kind,
                level=level,
                direction=DIRECTION_UNAUTHORIZED,
            )
        )
    for value in list(granted.values()) + assigned.unresolved:
        key = value.casefold()
        if key in present or key in direct:
            continue
        entries.append(
            DriftEntry(
                principal=value,
                kind=ACL_KIND_USER,
                level=ACL_CAN_USE,
                direction=DIRECTION_MISSING,
            )
        )
    return entries


class GrantMirrorService:
    """The mirror's whole behavior: resolve the app, read both sides, write, diff, roll back.

    Stateless; see :func:`get_grant_mirror_service`. Every method raises :class:`GrantMirrorError`
    and every detail is one of this module's fixed literals.
    """

    def _graph(self):
        from api.routes.grants import get_graph_service

        return get_graph_service()

    async def acl_target(self, agent) -> AclTarget:
        """Resolve the agent's app and a workspace token, or raise ``target_unresolved``.

        Reuses ``DatabricksIdentityService._resolve_stage_and_app``, which picks the stage by
        listing each workspace's apps, mints the workspace SP token from the stored credential and
        returns the app record whose ``url`` matches the agent's handle. Re-implementing stage
        resolution here would mean two rules about which workspace an agent lives in, and the
        looser one would be the bug. Nothing here reads a secret directly.
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
            raise GrantMirrorError(kind="target_unresolved")

        tenant_id = (agent.tenant_id or "").strip()
        if not tenant_id:
            raise GrantMirrorError(kind="target_unresolved")
        from api.routes.tenants import get_tenant_service

        try:
            tenant = get_tenant_service().get(tenant_id)
        except Exception as err:
            logger.warning(
                "[grants] could not resolve the tenant for agent %s (%s)",
                agent.id,
                getattr(err, "kind", None) or type(err).__name__,
            )
            raise GrantMirrorError(kind="target_unresolved") from err
        if tenant is None:
            logger.warning("[grants] could not resolve the tenant for agent %s", agent.id)
            raise GrantMirrorError(kind="target_unresolved")

        try:
            stage, token, app = await identity._resolve_stage_and_app(agent, tenant)
        except Exception as err:
            logger.warning(
                "[grants] could not resolve agent %s's Databricks app (%s)",
                agent.id,
                getattr(err, "kind", None) or type(err).__name__,
            )
            raise GrantMirrorError(kind="target_unresolved") from err

        app_name = str((app or {}).get("name") or "")
        if not app_name:
            raise GrantMirrorError(kind="target_unresolved")
        return AclTarget(
            databricks=get_databricks_workspace_service(),
            stage=stage,
            token=token,
            app_name=app_name,
        )

    async def grantee_user_name(self, oid: str) -> str:
        """The Databricks ``user_name`` for an Entra user oid: ``mail``, else ``userPrincipalName``.

        Those are the two values a Databricks username carries. An oid resolving to neither returns
        ``""`` and the caller decides what that means: a grant refuses and rolls back, a drift read
        reports the oid as ``missing_acl``.
        """
        if not oid:
            return ""
        try:
            principal = await self._graph().get_principal(oid, "user")
        except (GraphError, ValueError):
            return ""
        if not isinstance(principal, dict):
            return ""
        mail = str(principal.get("mail") or "").strip()
        return mail or str(principal.get("userPrincipalName") or "").strip()

    async def assigned_user_names(self, agent) -> AssignedUsers:
        """Read the agent's assignments and split their user half (see :class:`AssignedUsers`).

        Group assignments are dropped: the mirror is per user, which is why the grant path refuses
        a group principal on a Databricks-governed agent. The filter stays as defence in depth,
        because Graph reports the real ``principalType`` while a request body only claims one, and
        it covers group assignments made before that refusal existed or outside AGP.
        """
        try:
            assignments = await self._graph().list_assignments(agent.entra_sp_id)
        except GraphError as err:
            logger.warning(
                "[grants] could not read agent %s's assignments for the ACL mirror (%s)",
                agent.id,
                err.status,
            )
            raise GrantMirrorError(kind="grants_read_failed") from err

        names: dict[str, str] = {}
        unresolved: List[str] = []
        for assignment in assignments:
            if str(assignment.get("principalType") or "") != PRINCIPAL_TYPE_USER:
                continue
            oid = str(assignment.get("principalId") or "")
            if not oid:
                continue
            value = await self.grantee_user_name(oid)
            if not value:
                if oid not in unresolved:
                    unresolved.append(oid)
                continue
            names.setdefault(value.casefold(), value)
        return AssignedUsers(names=names, unresolved=unresolved)

    async def _read_acl(self, agent, target: AclTarget) -> List[dict]:
        try:
            return await target.databricks.get_app_permissions(
                target.workspace_url, target.token, target.app_name
            )
        except Exception as err:
            logger.warning(
                "[grants] could not read agent %s's app access list (%s)",
                agent.id,
                getattr(err, "kind", None) or type(err).__name__,
            )
            raise GrantMirrorError(kind="acl_read_failed") from err

    def _log_stripped_acl_entries(
        self, agent, app_name: str, current: List[dict], desired: List[dict]
    ) -> None:
        """Name every entry the re-assert PUT is about to remove, then count them.

        ``set_app_permissions`` is a replacing PUT, and a takeover whose record omits who lost
        access is not a record; provisioning logs the same takeover through
        ``DatabricksIdentityService._log_stripped_acl_entries``, and an operator's button cannot
        log less. Principal names are directory identities, not secrets. Inherited entries are
        counted apart: a PUT cannot remove them, so calling one "stripped" would claim a takeover
        that did not happen; they survive and show up as drift.
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

    def gate(self, agent) -> None:
        """The entry condition of every public method on this service: two 409s.

        Called at the top of every public method rather than left to the caller, so "no caller
        reaches the mirror ungated" is an invariant of the boundary, not a convention the next
        caller is trusted to know; the routes gate too, and the duplicate call reads two fields and
        performs no I/O. A non-Databricks agent has no platform ACL at all, and an empty drift list
        would claim "checked, all clean" about a surface that does not exist. An unprovisioned agent
        has no asserted list to diff yet, so a diff would report every assignment as ``missing_acl``.
        """
        if not is_databricks_governed_agent(agent):
            raise GrantMirrorError(kind="not_databricks")
        if not is_provisioned(agent):
            raise GrantMirrorError(kind="not_provisioned")

    async def _drift(self, agent, target: AclTarget, assigned: Optional[AssignedUsers] = None) -> DriftRead:
        """Read both sides and diff them; either read raises :class:`GrantMirrorError`.

        ``assigned`` lets a caller that already resolved the Entra side pass it in: that side is one
        ``list_assignments`` plus one uncached ``get_principal`` per assignment, and AGP's own ACL
        PUT cannot change Entra state. The ACL side is always re-read, because that is the side the
        PUT just changed.
        """
        if assigned is None:
            assigned = await self.assigned_user_names(agent)
        acl = await self._read_acl(agent, target)
        return DriftRead(entries=_diff_acl(acl, assigned, _baseline_principals(target)))

    async def drift(self, agent) -> DriftRead:
        """The drift read: resolve the app, diff both sides, write nothing."""
        self.gate(agent)
        return await self._drift(agent, await self.acl_target(agent))

    async def _roll_back(self, agent, assignment: dict) -> None:
        """Revoke the assignment just made; a failure here is the drifted state ``rollback_failed`` names."""
        try:
            await self._graph().revoke_app_role(agent.entra_sp_id, str(assignment.get("id") or ""))
        except Exception as err:
            logger.warning(
                "[grants] agent %s: the grant could not be mirrored AND the Entra assignment could not "
                "be rolled back (%s)",
                agent.id,
                getattr(err, "status", None) or type(err).__name__,
            )
            raise GrantMirrorError(kind="rollback_failed") from err

    async def mirror_grant(self, agent, assignment: dict) -> None:
        """Write the per-user ``CAN_USE`` that makes the fresh assignment real, or roll it back.

        Both or neither: a failed ACL write sends the assignment back. A non-user assignment is
        refused here, not skipped: ``principal_type`` in the request body is display-only and never
        reaches Graph, so a group oid labelled "user" passes the route's check, and Graph's
        ``principalType`` on the assignment it just created is the only truthful answer. The gate
        runs before the mirror's own failure handling so its 409 is never relabelled as a failed
        write. If the rollback itself fails, the error names both failures and the drift read is
        what surfaces the record.
        """
        self.gate(agent)

        if str(assignment.get("principalType") or "") != PRINCIPAL_TYPE_USER:
            await self._roll_back(agent, assignment)
            raise GrantMirrorError(kind="group_unsupported")

        user_name = await self.grantee_user_name(str(assignment.get("principalId") or ""))
        if not user_name:
            await self._roll_back(agent, assignment)
            raise GrantMirrorError(kind="grantee_unnamed")
        try:
            target = await self.acl_target(agent)
            await target.databricks.grant_app_can_use(
                target.workspace_url,
                target.token,
                target.app_name,
                user_name,
                kind=ACL_KIND_USER,
            )
        except GrantMirrorError as err:
            await self._roll_back(agent, assignment)
            raise GrantMirrorError(kind="not_applied") from err
        except Exception as err:
            logger.warning(
                "[grants] the platform ACL grant failed for agent %s (%s)",
                agent.id,
                getattr(err, "kind", None) or type(err).__name__,
            )
            await self._roll_back(agent, assignment)
            raise GrantMirrorError(kind="not_applied") from err

    async def revoke_mirror_target(self, agent, assignment_id: str) -> Optional[str]:
        """The ``user_name`` whose ACL entry a pending revoke must remove.

        Resolved before the destructive Entra call: once the assignment is deleted, the principal
        behind it is no longer readable from Graph. ``None`` means nothing to mirror (a group
        assignment, or an id the assignment list does not carry, which the Entra call answers with
        its own 404). ``""`` means a user AGP could not name, including an unreadable assignment
        list: the revoke still proceeds, and the caller reports the ACL half as failed, which is
        honest because the entry may well still be there.
        """
        self.gate(agent)
        try:
            assignments = await self._graph().list_assignments(agent.entra_sp_id)
        except GraphError:
            logger.warning(
                "[grants] agent %s: the assignment list was unreadable, so the revoke's ACL "
                "mirror target is unknown",
                agent.id,
            )
            return ""
        match = next((a for a in assignments if str(a.get("id") or "") == assignment_id), None)
        if match is None:
            return None
        if str(match.get("principalType") or "") != PRINCIPAL_TYPE_USER:
            return None
        return await self.grantee_user_name(str(match.get("principalId") or ""))

    async def mirror_revoke(self, agent, target_user: str) -> None:
        """Remove the user's ACL entry after the Entra assignment is already gone."""
        self.gate(agent)
        try:
            target = await self.acl_target(agent)
            await target.databricks.revoke_app_can_use(
                target.workspace_url,
                target.token,
                target.app_name,
                target_user,
                ACL_KIND_USER,
            )
        except GrantMirrorError as err:
            raise GrantMirrorError(kind="revoke_acl_failed") from err
        except Exception as err:
            logger.warning(
                "[grants] the platform ACL removal failed for agent %s (%s)",
                agent.id,
                getattr(err, "kind", None) or type(err).__name__,
            )
            raise GrantMirrorError(kind="revoke_acl_failed") from err

    async def reassert(self, agent) -> DriftRead:
        """Rewrite the app's ACL from AGP's grants, then answer with fresh drift.

        The desired list is composed, never read-modified, so a re-assert after a half-completed
        write converges: ``admins`` at ``CAN_MANAGE`` (workspace admins stay above the governance
        layer, and the client refuses a PUT without it), the tenant's workspace SP at ``CAN_MANAGE``
        (the credential AGP writes with; dropping it would make this the last ACL write AGP could
        perform), and one ``CAN_USE`` per assigned user whose username Entra could name. No other
        service principal: federation makes the calling user the invoke identity. The composed list
        is exactly what :func:`_baseline_principals` treats as owned, so a re-assert never
        self-drifts. The current ACL is read first so every entry the PUT removes is logged by
        name. The answer is a fresh drift read, not an ack: non-empty entries after a successful PUT
        are honest, because an inherited grant or an assignment with no resolvable username cannot
        be written away. ``admins`` is restated from fixed text rather than imported from the
        identity service so a spelling drift fails loud in ``acl_missing_admins`` instead of locking
        the workspace admins out silently; the list is composed by hand rather than through
        ``_to_write_shape``, which collapses a read and cannot casefold ``user_name``.
        """
        self.gate(agent)
        target = await self.acl_target(agent)
        assigned = await self.assigned_user_names(agent)

        workspace_sp = str(getattr(target.stage, "sp_client_id", "") or "").strip()
        desired: List[dict] = [
            dict(ACL_ADMINS_ENTRY),
            {
                "principal": workspace_sp,
                "kind": ACL_KIND_SERVICE_PRINCIPAL,
                "level": ACL_CAN_MANAGE,
            },
        ]
        for value in assigned.names.values():
            desired.append({"principal": value, "kind": ACL_KIND_USER, "level": ACL_CAN_USE})

        current = await self._read_acl(agent, target)
        self._log_stripped_acl_entries(agent, target.app_name, current, desired)

        try:
            await target.databricks.set_app_permissions(
                target.workspace_url, target.token, target.app_name, desired
            )
        except Exception as err:
            logger.warning(
                "[grants] the platform ACL re-assert failed for agent %s (%s)",
                agent.id,
                getattr(err, "kind", None) or type(err).__name__,
            )
            raise GrantMirrorError(kind="acl_write_failed") from err

        return await self._drift(agent, target, assigned)


_svc: Optional[GrantMirrorService] = None


def get_grant_mirror_service() -> GrantMirrorService:
    global _svc
    if _svc is None:
        _svc = GrantMirrorService()
    return _svc
