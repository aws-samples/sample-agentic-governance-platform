"""The agent-to-MCP grant, apply and revoke, shared by the grants route and the marketplace.

``apply_agent_mcp_grant`` does four things in this order: the Entra app-role assignment on the MCP
service principal (``Invoker`` or ``Admin``, the MCP's stored role id); the agent-to-MCP delegated
consent, the precondition for the agent's on-behalf-of invoke; the agent's AgentCore Identity
credential provider (get-or-create, its non-secret name persisted on the agent record); and the
agent's desired-state set ``mcp_server_ids`` plus the runtime env rebuilt from it
(``services.agent_mcp_env``). Entra first, then the set, then the env: the enforcement change is
what matters for security, and the env is recoverable by re-running the grant, which is
idempotent (a Graph "already assigned" is recovered by re-reading the existing assignment id).

``revoke_agent_mcp_grant`` is the kill switch. The MCP app has ``appRoleAssignmentRequired=false``,
so the admission gate for the delegated path is the consent grant, not the app-role assignment:
deleting only the assignment would remove the UI row and leave the agent able to reach the MCP.
The revoke resolves the agent SP from the assignment before deleting it (unrecoverable afterwards),
revokes the assignment, tears the consent down only when the agent holds no other assignment on
this MCP (Invoker and Admin share one consent), then syncs the set and the env.

Both raise :class:`GrantRevokeError` kinds whose client details are fixed or whose messages this
module sets; a Graph response body never reaches a message, and the failure seams log a traceback
(no local values, so no secret). The route and marketplace singletons are imported at call time,
the non-circular direction. ``list_assignments`` and ``revoke_app_role`` name their first argument
``agent_sp_id``, but for an agent-to-MCP grant it is the resource (MCP) SP.
Decision: docs/adr/backend-mcp-grant-entra-first-then-runtime-env.md
"""

import logging
from collections.abc import Mapping
from typing import ClassVar, Optional

import anyio.to_thread

from core.errors import AppError, Outcome
from services.agent_mcp_env import rebuild_runtime_mcp_env
from services.grant_mirror_service import CROSS_TENANT_GRANT_REQUIRES_ADMIN, GRANT_ROLE_INVALID
from services.graph_errors import GraphError

logger = logging.getLogger(__name__)

_ENV_REBUILD_FAILED_MSG = (
    "Entra permission updated but the runtime environment was NOT updated — the agent "
    "cannot reach this MCP until the grant is retried."
)


class GrantRevokeError(AppError):
    """An agent-to-MCP grant could not be applied or revoked.

    The base carries the apply kinds, each with a fixed detail: ``already_assigned`` (Graph refused
    the assignment as a duplicate and the existing assignment id could not be recovered, or refused
    the consent request with a 400, the wire the route has always given that case),
    ``assign_failed`` (any other Graph failure on the assignment or the consent) and
    ``wiring_failed`` (the Entra permission is in place but the credential provider, the registry
    write or the runtime env could not be wired; a retried grant is idempotent and fixes it). The
    subclasses are the revoke kinds, one row each, whose messages are fixed literals this module
    sets and sends to the client verbatim. Every ``None``-detail row's message is a fixed literal
    written by this module and never Graph's response body.
    """

    domain: ClassVar[str] = "mcp_grant"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "already_assigned": Outcome(409, "principal is already assigned to this MCP"),
        "assign_failed": Outcome(502, "failed to assign the role"),
        "wiring_failed": Outcome(
            502,
            "role assigned but credential-provider or runtime env wiring failed; re-grant to retry",
        ),
    }


class McpGrantRefusedError(AppError):
    """The MCP grants routes refused a grant or a revoke before any Graph call; every kind has a
    fixed detail. ``invalid_role``: a role other than ``Invoker`` or ``Admin``.
    ``not_provisioned``: the MCP has no service principal to assign on. ``cross_tenant``: a
    non-admin granted an agent outside the MCP's tenant, or a principal that is not a governed
    agent.
    """

    domain: ClassVar[str] = "mcp_grant"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "invalid_role": Outcome(400, GRANT_ROLE_INVALID),
        "not_provisioned": Outcome(409, "MCP identity is not provisioned"),
        "cross_tenant": Outcome(403, CROSS_TENANT_GRANT_REQUIRES_ADMIN),
    }


class GrantNotFoundError(GrantRevokeError):
    """The app-role assignment is already gone (a Graph 404 on ``revoke_app_role``, the stale or
    double-click race); callers treat it as done. The message is a fixed literal of this module,
    never Graph's body (see ``GrantRevokeError``)."""

    kinds: ClassVar[Mapping[str, Outcome]] = {"not_found": Outcome(404, None)}


class GrantReadError(GrantRevokeError):
    """``list_assignments`` failed, before the revoke or on the re-list after it; the message says
    which and whether the assignment is already revoked, in a fixed literal of this module and never
    Graph's body (see ``GrantRevokeError``)."""

    kinds: ClassVar[Mapping[str, Outcome]] = {"read_failed": Outcome(502, None)}


class GrantRevokeFailedError(GrantRevokeError):
    """A non-404 Graph failure on ``revoke_app_role``: the assignment delete itself failed. The
    message is a fixed literal of this module, never Graph's body (see ``GrantRevokeError``)."""

    kinds: ClassVar[Mapping[str, Outcome]] = {"revoke_failed": Outcome(409, None)}


class ConsentRevokeError(GrantRevokeError):
    """``revoke_agent_obo_consent`` failed after the assignment was deleted; re-revoking is
    idempotent. The message is a fixed literal of this module, never Graph's body (see
    ``GrantRevokeError``)."""

    kinds: ClassVar[Mapping[str, Outcome]] = {"consent_revoke_failed": Outcome(502, None)}


def _app_role_id_for_role(mcp, role: str) -> str:
    """The MCP's stored app role id for ``Invoker`` or ``Admin``; anything else is a ``ValueError``
    (the route validates the role before calling, the marketplace only passes ``Invoker``)."""
    if role == "Invoker":
        return mcp.invoker_role_id
    if role == "Admin":
        return mcp.admin_role_id
    raise ValueError("role must be 'Invoker' or 'Admin'")


async def _assign_role(graph, mcp, agent, app_role_id: str) -> str:
    """Assign the app role on the MCP SP and return the assignment id.

    A Graph 400 or 409 means the assignment already exists: the MCP's assignments are re-read and
    the one for this agent and role is returned, which makes a re-grant idempotent. When the
    recovery finds nothing or itself fails, the original refusal wins as ``already_assigned``; a
    recovery-time error never masks it. Any other Graph failure is ``assign_failed``.
    """
    try:
        assignment = await graph.assign_app_role(mcp.entra_sp_id, agent.entra_sp_id, app_role_id)
        return assignment.get("id", "")
    except GraphError as err:
        if err.status not in (400, 409):
            raise GrantRevokeError(
                f"assigning the role on MCP {mcp.id} failed ({err.status})", "assign_failed"
            ) from err
        try:
            existing = await graph.list_assignments(mcp.entra_sp_id)
        except Exception:
            logger.warning(
                "[mcp_grant] could not re-read the assignments of MCP %s after an already-assigned refusal",
                mcp.id,
                exc_info=True,
            )
            raise GrantRevokeError(
                f"principal is already assigned to MCP {mcp.id}; the existing assignment could not be read",
                "already_assigned",
            ) from err
        for candidate in existing:
            matches = (
                candidate.get("principalId") == agent.entra_sp_id
                and candidate.get("appRoleId") == app_role_id
            )
            if matches and candidate.get("id", ""):
                return candidate.get("id", "")
        raise GrantRevokeError(
            f"principal is already assigned to MCP {mcp.id}; the existing assignment was not found",
            "already_assigned",
        ) from err


async def _grant_consent(graph, mcp, agent) -> None:
    """Grant the agent-to-MCP delegated consent. Graph swallows an already-existing grant itself,
    so a 400 here is a refusal, rendered as ``already_assigned`` (the wire the route has always
    given a Graph 400 on this path); any other Graph failure is ``assign_failed``."""
    try:
        await graph.grant_agent_obo_consent(agent.entra_sp_id, mcp.entra_sp_id)
    except GraphError as err:
        if err.status == 400:
            raise GrantRevokeError(
                f"granting the consent for MCP {mcp.id} was refused ({err.status})", "already_assigned"
            ) from err
        raise GrantRevokeError(
            f"granting the consent for MCP {mcp.id} failed ({err.status})", "assign_failed"
        ) from err


async def _wire_governed_agent(agent, mcp) -> None:
    """The credential provider and the runtime env for a governed agent, after the Entra
    assignment and consent succeeded.

    The provider name (a non-secret) and the MCP id are persisted in one write, because two
    back-to-back writes race the registry's UPDATING window. A failure of either step is
    ``wiring_failed``: the operator must know the system is in a permission-yes, wiring-no state
    that a retried grant fixes.
    """
    from api.routes.agents import get_service as get_agent_service
    from api.routes.mcp_server_grants import get_agent_credential_service

    try:
        provider_name = await get_agent_credential_service().ensure_agent_credential_provider(agent)
    except Exception as err:
        logger.exception(
            "[mcp_grant] credential-provider setup failed for agent %s (MCP %s)",
            getattr(agent, "id", "?"),
            mcp.entra_sp_id,
        )
        raise GrantRevokeError(
            f"credential provider setup failed for agent {getattr(agent, 'id', '?')}", "wiring_failed"
        ) from err

    changed = agent.oauth2_credential_provider_name != provider_name
    agent.oauth2_credential_provider_name = provider_name
    if mcp.id not in agent.mcp_server_ids:
        agent.mcp_server_ids.append(mcp.id)
        changed = True
    if changed:
        await anyio.to_thread.run_sync(get_agent_service().persist_identity, agent)

    try:
        await rebuild_runtime_mcp_env(agent)
    except Exception as err:
        logger.exception(
            "[mcp_grant] runtime env rebuild failed for agent %s (MCP %s)",
            getattr(agent, "id", "?"),
            mcp.id,
        )
        raise GrantRevokeError(_ENV_REBUILD_FAILED_MSG, "wiring_failed") from err


async def apply_agent_mcp_grant(agent, mcp, *, role: str = "Invoker") -> str:
    """Apply the full grant (the module docstring has the order) and return the Entra assignment id.

    ``agent`` may be the governed record or a bare carrier of ``entra_sp_id``: the governed record
    is resolved from the registry by SP only after the assignment and the consent succeeded, so a
    Graph failure never touches the registry. A principal that is not a governed agent with an
    Entra app keeps its (valid) assignment and skips the wiring with a warning. Any fault that is
    neither a :class:`GrantRevokeError` kind nor a ``GraphError`` this module maps (a transport error
    on an Entra write, a registry read or write) is ``wiring_failed`` with a logged traceback, the
    wire the route has always given such a fault; a retried grant is idempotent.
    """
    from api.routes.agents import get_service as get_agent_service
    from api.routes.mcp_server_grants import get_mcp_graph_service

    app_role_id = _app_role_id_for_role(mcp, role)
    try:
        graph = get_mcp_graph_service()
        assignment_id = await _assign_role(graph, mcp, agent, app_role_id)
        await _grant_consent(graph, mcp, agent)
        governed_agent = next(
            (a for a in get_agent_service().list() if a.entra_sp_id == agent.entra_sp_id), None
        )
        if governed_agent is not None and governed_agent.entra_app_id:
            await _wire_governed_agent(governed_agent, mcp)
        else:
            logger.warning(
                "[mcp_grant] grant principal %s did not resolve to a governed agent with an "
                "Entra app; skipping credential-provider creation (assignment still valid)",
                getattr(agent, "entra_sp_id", "?"),
            )
    except GrantRevokeError:
        raise
    except Exception as err:
        logger.exception("[mcp_grant] grant failed for MCP %s outside the mapped Graph seams", mcp.id)
        raise GrantRevokeError(f"grant failed for MCP {mcp.id}", "wiring_failed") from err
    return assignment_id


async def _revoke_consent_if_unused(graph, mcp, agent_sp_id: str, assignment_id: str) -> None:
    """Tear the agent-to-MCP consent down unless the agent still holds another assignment on this
    MCP (Invoker and Admin share one consent).

    The re-list excludes the just-deleted assignment id: ``appRoleAssignedTo`` is eventually
    consistent, so a re-list fired right after the delete usually still contains it, while a
    genuine sibling has a different id. A re-list failure is :class:`GrantReadError` (the
    assignment is revoked, the consent's state unknown, re-revoking idempotent); a consent failure
    is :class:`ConsentRevokeError`.
    """
    try:
        remaining = await graph.list_assignments(mcp.entra_sp_id)
    except GraphError as err:
        logger.exception(
            "[mcp_grant] revoke: failed to re-list assignments for MCP %s after "
            "revoking %s (consent teardown skipped)",
            getattr(mcp, "id", "?"),
            assignment_id,
        )
        raise GrantReadError(
            "grant revoked but consent cleanup could not be verified; re-revoke to retry"
        ) from err
    if any(a.get("principalId") == agent_sp_id and a.get("id") != assignment_id for a in remaining):
        return
    try:
        await graph.revoke_agent_obo_consent(agent_sp_id, mcp.entra_sp_id)
    except GraphError as err:
        logger.exception(
            "[mcp_grant] revoke: assignment %s removed but consent teardown failed for agent %s -> MCP %s",
            assignment_id,
            agent_sp_id,
            getattr(mcp, "id", "?"),
        )
        raise ConsentRevokeError("grant revoked but consent cleanup failed; re-revoke to retry") from err


async def _sync_agent_env_after_revoke(mcp, agent_sp_id: Optional[str]) -> None:
    """Remove the MCP from the governed agent's set and rebuild its runtime env, so the agent stops
    trying the revoked MCP.

    Best-effort relative to the kill switch, which already succeeded: a rebuild failure leaves a
    stale entry that fails closed (the agent's on-behalf-of mint is refused) and self-heals on the
    next grant or revoke, so it is logged, not raised; an agent that does not resolve is logged too.
    """
    from api.routes.agents import get_service as get_agent_service

    agent = next((a for a in get_agent_service().list() if a.entra_sp_id == agent_sp_id), None)
    if agent is None:
        logger.warning(
            "[mcp_grant] revoke: could not resolve governed agent for SP %s — Entra revoke "
            "succeeded, env not rebuilt",
            agent_sp_id,
        )
        return
    if mcp.id not in agent.mcp_server_ids:
        return
    agent.mcp_server_ids.remove(mcp.id)
    await anyio.to_thread.run_sync(get_agent_service().persist_identity, agent)
    try:
        await rebuild_runtime_mcp_env(agent)
    except Exception:
        logger.exception(
            "[mcp_grant] revoke: runtime env rebuild failed for agent %s (MCP %s) — "
            "Entra access IS revoked; runtime env left stale (harmless, self-heals on "
            "next grant/revoke)",
            getattr(agent, "id", "?"),
            mcp.id,
        )


async def revoke_agent_mcp_grant(mcp, assignment_id: str) -> None:
    """Revoke the assignment and, when the agent holds no other assignment on this MCP, its
    consent; then sync the agent's set and runtime env (the module docstring has the why).

    The agent SP is resolved from the pre-revoke assignment list because it is unrecoverable once
    the assignment is deleted. A missing assignment leaves it unresolved: the revoke still runs and
    its Graph 404 is :class:`GrantNotFoundError`, with no consent teardown (nothing to scope it to).
    A failed read is :class:`GrantReadError` rather than a blind revoke that loses the chance to
    clear the consent; a non-404 revoke failure is :class:`GrantRevokeFailedError`.
    """
    from api.routes.mcp_server_grants import get_mcp_graph_service

    graph = get_mcp_graph_service()
    try:
        assignments = await graph.list_assignments(mcp.entra_sp_id)
    except GraphError as err:
        logger.exception(
            "[mcp_grant] revoke: failed to list assignments for MCP %s before revoke",
            getattr(mcp, "id", "?"),
        )
        raise GrantReadError("failed to read the MCP's grants") from err
    agent_sp_id = next((a.get("principalId") for a in assignments if a.get("id") == assignment_id), None)

    try:
        await graph.revoke_app_role(mcp.entra_sp_id, assignment_id)
    except GraphError as err:
        if err.status == 404:
            raise GrantNotFoundError("grant not found") from err
        raise GrantRevokeFailedError("failed to revoke the grant") from err

    if agent_sp_id:
        await _revoke_consent_if_unused(graph, mcp, agent_sp_id, assignment_id)
    await _sync_agent_env_after_revoke(mcp, agent_sp_id)
