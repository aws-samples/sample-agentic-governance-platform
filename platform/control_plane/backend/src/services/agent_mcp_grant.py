"""Shared agent → MCP grant application (Epic 9, Task T3).

``apply_agent_mcp_grant`` is the grant body LIFTED VERBATIM from
``api.routes.mcp_server_grants.create_mcp_grant`` (the live, E7-verified POST handler,
its inline body at L228-363) into a reusable service function so BOTH callers share ONE
implementation:

  - the existing E7 route ``POST /mcp-servers/{id}/grants`` (``create_mcp_grant``), and
  - the E9 marketplace approve / auto-approve flow (``marketplace_service._apply_grant``).

It does FOUR things, in this exact order (the load-bearing E7 sequence — research §2.4 /
§4.2 / §12.5):

  1. ``assign_app_role(mcp.entra_sp_id, agent.entra_sp_id, app_role_id)`` — the Entra
     app-role assignment on the MCP SP (Invoker/Admin → the MCP's stored role id);
  2. ``grant_agent_obo_consent(agent.entra_sp_id, mcp.entra_sp_id)`` — the agent→MCP
     delegated-consent precondition for the Tier-2 OBO invoke;
  3. ``ensure_agent_credential_provider(agent)`` — the per-agent AgentCore Identity OBO
     credential provider (get-or-create); persists the (non-secret) provider name on the
     agent record if it changed;
  4. Record this MCP in the agent's authoritative desired-state set
     (``agent.mcp_server_ids``, dedup + persist) and rebuild the FULL runtime env from that
     set via ``rebuild_runtime_mcp_env(agent)`` (E12). The env build now lives in
     ``services/agent_mcp_env.py``: it resolves each granted MCP via the registry and writes
     a multi-MCP ``MCP_SERVERS`` JSON list (+ ``CREDENTIAL_PROVIDER_NAME``, with the legacy
     ``MCP_AUDIENCE`` / ``MCP_GATEWAY_URL`` keys neutralized) so granting a SECOND MCP no
     longer overwrites the first. The underlying ``set_runtime_environment`` set is still
     dispatched OFF the event loop (the sync boto3 set is wrapped in
     ``anyio.to_thread.run_sync`` — research §12.3, never block the loop).

Returns the Entra app-role assignment id (for revoke / record-keeping).

IDEMPOTENT: a Graph "already assigned" (a ``GraphError`` with status 400/409 from
``assign_app_role``) is recovered — the function re-lists the MCP's assignments, finds the
existing one for this agent + role, and proceeds (the OBO consent grant is itself idempotent
in the graph service). A non-idempotent Graph failure (anything else, e.g. a 5xx) is
RE-RAISED so the caller can map it (the E7 route → 502; marketplace → ``failed`` + Retry).

SECURITY (T-GRAPH carry-forward): this service NEVER logs a token or secret; the runtime-env
values here are non-secrets (provider name / url / audience / region), and the
``logger.exception`` failure seams emit a traceback only (Python tracebacks never print local
VALUES). FIXED-``detail`` HTTP literals + the GraphError→HTTPException mapping stay in the
ROUTE (HTTP concerns are not the service's job) — this function raises domain/Graph
exceptions; the credential-provider / runtime-env failure seams re-raise the underlying
exception (the route still maps both to a 502 with its fixed literals).

The singleton accessors are imported from ``api.routes.mcp_server_grants`` (the same
``get_mcp_graph_service`` / ``get_agent_credential_service`` / ``get_agent_identity_service``
the route used) — a non-circular direction: the route imports THIS module, and this module's
imports of the route's accessors are deferred to call time (module-level import would couple
import order with the route module). The agent registry (for persisting a changed provider
name) is resolved via ``api.routes.agents.get_service``, exactly as the route did.
"""

import logging
from typing import Optional

from services.agent_mcp_env import rebuild_runtime_mcp_env
from services.graph_service import GraphError

logger = logging.getLogger(__name__)

# Split-state message (Epic 12, Task T3 — design §4). A FIXED literal raised when the Entra
# permission was applied but the runtime-env rebuild failed: enforcement is already correct,
# but the agent is not yet wired to reach the MCP. Re-running the grant is idempotent and
# fixes the wiring — the operator MUST know the system is in permission-✓ / wiring-✗ state.
# Defined here (not in the route) so the HTTP literal stays out of the service — the route
# maps this RuntimeError → 502 (marketplace → failed + Retry), matching the existing
# convention for the credential-provider / runtime-env failure seams.
_ENV_REBUILD_FAILED_MSG = (
    "Entra permission updated but the runtime environment was NOT updated — the agent "
    "cannot reach this MCP until the grant is retried."
)


# --- revoke exceptions (Epic 9R, Task T1) ------------------------------------
# Typed domain failures for ``revoke_agent_mcp_grant`` — the revoke twin of
# ``apply_agent_mcp_grant``. The shared revoke body raises these; HTTP concerns stay in the
# ROUTE (it maps each type → its EXISTING status + FIXED detail literal). Each carries a
# SAFE message (a FIXED literal set by the revoke body — NEVER ``str(graph_err)`` / a Graph
# ``error.message`` — the T-GRAPH no-leak guard); the route may surface that safe message as
# the response detail without leaking a Graph resource body.

class GrantRevokeError(Exception):
    """Base for revoke failures. Carries a SAFE message."""


class GrantNotFoundError(GrantRevokeError):
    """The app-role assignment is already gone (a Graph 404 on ``revoke_app_role`` — the
    stale/double-click race). The route maps this to 404."""


class GrantReadError(GrantRevokeError):
    """``list_assignments`` failed (pre-revoke read, OR the post-revoke multiplicity re-list).
    The route maps this to 502."""


class GrantRevokeFailedError(GrantRevokeError):
    """A non-404 GraphError on ``revoke_app_role`` (the assignment delete genuinely failed).
    The route maps this to 409."""


class ConsentRevokeError(GrantRevokeError):
    """``revoke_agent_obo_consent`` failed (the assignment is already deleted at that point).
    The route maps this to 502."""


def _app_role_id_for_role(mcp, role: str) -> str:
    """Map a requested role name → the MCP's stored appRoleId GUID.

    Pure (no HTTP). "Invoker" → ``mcp.invoker_role_id``; "Admin" → ``mcp.admin_role_id``;
    anything else → ``ValueError`` (the E7 route validates the role and raises its own
    ``HTTPException(400)`` BEFORE calling this function, so a bad role never reaches here on
    the route path; the marketplace only ever passes "Invoker").
    """
    if role == "Invoker":
        return mcp.invoker_role_id
    if role == "Admin":
        return mcp.admin_role_id
    raise ValueError("role must be 'Invoker' or 'Admin'")


async def apply_agent_mcp_grant(agent, mcp, *, role: str = "Invoker") -> str:
    """Apply the full agent→MCP grant (assign role + OBO consent + credential provider +
    add the MCP to the agent's ``mcp_server_ids`` set and rebuild the multi-MCP runtime env)
    and return the Entra app-role assignment id.

    See the module docstring for the exact order, idempotency, and security contract. The
    assign/consent/provider steps are the body lifted verbatim from
    ``mcp_server_grants.create_mcp_grant`` (L228-363); the env step was switched to the E12
    set+rebuild path (``rebuild_runtime_mcp_env``).
    """
    # Deferred import of the singleton accessors (the SAME ones the route used). Deferred to
    # call time so this module does NOT import the route module at load — the route imports
    # THIS module at module level (the correct, non-circular direction).
    from api.routes.mcp_server_grants import (
        get_agent_credential_service,
        get_mcp_graph_service,
    )

    app_role_id = _app_role_id_for_role(mcp, role)

    graph = get_mcp_graph_service()
    try:
        assignment = await graph.assign_app_role(
            mcp.entra_sp_id, agent.entra_sp_id, app_role_id
        )
        assignment_id = assignment.get("id", "")
    except GraphError as err:
        if err.status in (400, 409):
            # Idempotent: an already-assigned (or duplicate) → recover the EXISTING
            # assignment id from Graph rather than failing. Re-list the MCP's assignments
            # and match this agent SP + this app role. If the recovery itself cannot
            # resolve the existing id (no match, OR the re-list fails), surface the ORIGINAL
            # already-assigned GraphError so the caller still gets a clean signal — the E7
            # route maps it to 409 (the original behavior), the marketplace treats an
            # already-assigned as success. NEVER let a recovery-time error mask the original.
            assignment_id = ""
            try:
                existing = await graph.list_assignments(mcp.entra_sp_id)
                for a in existing:
                    if (
                        a.get("principalId") == agent.entra_sp_id
                        and a.get("appRoleId") == app_role_id
                    ):
                        assignment_id = a.get("id", "")
                        break
            except Exception:  # noqa: BLE001 — recovery is best-effort; original error wins.
                raise err from None
            if not assignment_id:
                # Could not recover the existing assignment id — surface the original error.
                raise err from None
        else:
            # A real (non-idempotent) Graph failure → propagate (callers map to 502).
            raise

    # Wire AGENT→MCP delegated consent (research §2.4): agent SP = the grant principal,
    # MCP SP = the resource. Idempotent (an already-exists is swallowed in the service).
    await graph.grant_agent_obo_consent(agent.entra_sp_id, mcp.entra_sp_id)

    # Resolve the GOVERNED agent record for the credential-provider / runtime-env steps. This
    # mirrors the original route order EXACTLY (the registry is read AFTER assign + consent
    # succeed — never before, so an assign/consent failure short-circuits without touching the
    # registry). The grant principal is the agent's SP object id; match on ``entra_sp_id``. The
    # caller's ``agent`` may already be the governed record (the marketplace passes it) — the
    # registry lookup returns the same record then; for the route's defensive synthetic agent
    # (a non-governed principal SP) it returns None → the cred/env steps are skipped below.
    from api.routes.agents import get_service as get_agent_service

    governed_agent = next(
        (a for a in get_agent_service().list() if a.entra_sp_id == agent.entra_sp_id),
        None,
    )

    # Ensure the agent's AgentCore Identity credential provider exists (the "outbound auth"
    # the reference agent uses for agent→MCP OBO). Per-agent, get-or-create, created at grant
    # time so a successful grant guarantees the provider exists before the first invoke.
    if governed_agent is not None and governed_agent.entra_app_id:
        agent = governed_agent
        try:
            # ASYNC service (T-CRED-ASYNC-FIX): it awaits Graph on THIS (the uvicorn) loop —
            # reusing the loop-bound httpx client correctly — and off-loads its own blocking
            # boto3 internally. No run_sync wrapper.
            provider_name = await get_agent_credential_service().ensure_agent_credential_provider(
                agent
            )
        except Exception:  # noqa: BLE001 — surface a clear, recoverable error (re-grant to retry).
            # Log the REAL failure server-side (traceback) so this stops being invisible —
            # Python tracebacks never print local VALUES, so the agent's client secret cannot
            # leak via this log.
            logger.exception(
                "[mcp_grant] credential-provider setup failed for agent %s (MCP %s)",
                getattr(agent, "id", "?"),
                mcp.entra_sp_id,
            )
            # Re-raise so the caller maps it (the E7 route → 502 with its FIXED literal;
            # marketplace → failed + Retry). HTTP concerns stay in the route.
            raise

        # Persist the provider NAME on the agent record (a non-secret). Only when it changed.
        if agent.oauth2_credential_provider_name != provider_name:
            from api.routes.agents import get_service as get_agent_service

            agent.oauth2_credential_provider_name = provider_name
            get_agent_service().persist_identity(agent)

        # E12 (Task T3, design §3.2/§4): the Entra grant + consent succeeded above, so the
        # ENFORCEMENT change is already in place. Now record this MCP in the agent's
        # DESIRED-STATE set and rebuild the FULL runtime env from that set — replacing the old
        # single-MCP "latest grant wins" injection (one effective MCP per agent). The set is
        # the authoritative per-agent list of granted MCPs; the env is rebuilt from it +
        # the MCP registry, so granting a SECOND MCP no longer overwrites the first.
        #
        # Entra FIRST → persist the set → rebuild env (the load-bearing order, design §4): the
        # enforcement change is what matters for security; the env is recoverable by re-running
        # the grant (idempotent), so an env-rebuild failure must FAIL LOUD (re-raise) rather
        # than silently leave the system in a permission-✓ / wiring-✗ split state.
        if mcp.id not in agent.mcp_server_ids:
            # Dedup: a re-grant of an already-granted MCP must not duplicate the id.
            agent.mcp_server_ids.append(mcp.id)
            get_agent_service().persist_identity(agent)

        try:
            # rebuild_runtime_mcp_env reads agent.mcp_server_ids, resolves each MCP via the
            # registry, builds the MCP_SERVERS env, and dispatches set_runtime_environment OFF
            # the event loop (a no-op for an agent with no runtime handle). It owns the env
            # build now — this service no longer constructs the env dict itself.
            await rebuild_runtime_mcp_env(agent)
        except Exception as err:  # noqa: BLE001 — fail loud, mirroring the credential-provider path.
            # Log the REAL failure server-side (traceback) so this is not invisible. The env
            # values are NON-secrets (provider name / url / audience / region) and Python
            # tracebacks never print local VALUES — no secret can leak via this log.
            logger.exception(
                "[mcp_grant] runtime env rebuild failed for agent %s (MCP %s)",
                getattr(agent, "id", "?"),
                mcp.id,
            )
            # Re-raise with the explicit split-state message (design §4): the Entra permission
            # is updated but the runtime is not wired. The route maps RuntimeError → 502
            # (marketplace → failed + Retry); the HTTP literal stays in the route.
            raise RuntimeError(_ENV_REBUILD_FAILED_MSG) from err
    else:
        # The principal SP isn't one of our governed/provisioned agents (or it has no Entra
        # app). The binary SP app-role assignment is still valid, so don't error — log a
        # warning (visible, not silent) and skip provider creation. The UI only offers
        # governed agents, so the normal path always resolves; this branch is defensive.
        logger.warning(
            "[mcp_grant] grant principal %s did not resolve to a governed agent with an "
            "Entra app; skipping credential-provider creation (assignment still valid)",
            getattr(agent, "entra_sp_id", "?"),
        )

    return assignment_id


async def revoke_agent_mcp_grant(mcp, assignment_id: str) -> None:
    """Revoke app-role assignment ``assignment_id`` on the MCP SP; when the principal
    (agent SP) holds NO OTHER assignment on that MCP, tear down the agent→MCP OBO consent
    (the real kill switch).

    This is the kill-switch body LIFTED VERBATIM from
    ``api.routes.mcp_server_grants.delete_mcp_grant`` (the live, E7-verified DELETE handler,
    L283-406) into a reusable service function so BOTH callers share ONE implementation:

      - the existing E7 route ``DELETE /mcp-servers/{id}/grants/{assignment_id}``
        (``delete_mcp_grant``), and
      - the E9R marketplace admin-revoke flow (``marketplace_service.revoke_subscription``).

    Sequence (ordering is load-bearing — research §3 / the route docstring at L301-319):
      1. ``list_assignments(mcp.entra_sp_id)`` BEFORE the revoke → resolve the agent SP
         (``principalId``) for THIS ``assignment_id``; it is unrecoverable once
         ``revoke_app_role`` deletes the assignment. A missing assignment (already gone)
         leaves ``agent_sp_id`` None → revoke-then-404 with NO consent teardown (no agent SP
         to scope it to). A read failure raises ``GrantReadError`` (we will not blindly
         revoke and lose the chance to also clear the consent).
      2. ``revoke_app_role(mcp.entra_sp_id, assignment_id)`` — a Graph 404 (already gone)
         raises ``GrantNotFoundError``; any other GraphError raises ``GrantRevokeFailedError``.
      3. MULTIPLICITY GUARD: an agent may hold BOTH an Invoker AND an Admin assignment on the
         SAME MCP, and those two assignments share ONE consent grant (the consent is
         per-(agent, MCP), not per-role). So tear down the consent ONLY IF the agent has NO
         OTHER app-role assignment remaining on this MCP (re-list, filter to the same
         ``principalId``); a surviving role still needs it. A re-list failure raises
         ``GrantReadError``.
      4. ``revoke_agent_obo_consent(agent_sp_id, mcp.entra_sp_id)`` — the real OBO kill switch.
         A failure raises ``ConsentRevokeError`` (the assignment IS already deleted at that
         point, but re-revoke is idempotent so the caller can retry).

    ⚠️ NAMING QUIRK (research §7): ``list_assignments`` / ``revoke_app_role`` name their first
    arg ``agent_sp_id``, but for an agent→MCP grant it is the **RESOURCE (MCP) SP** — pass
    ``mcp.entra_sp_id`` (NOT the agent SP).

    Uses ``get_mcp_graph_service()`` (the SAME deferred accessor the route used). Raises the
    typed exceptions above; SECURITY: NEVER logs a token, and each raised exception carries a
    FIXED safe message (never ``str(graph_err)`` — a Graph ``error.message`` could otherwise
    surface to a client); the ``logger.exception`` failure seams emit a traceback only
    (Python tracebacks never print local VALUES). Returns None on success.
    """
    # Deferred import of the singleton accessor (the SAME one the route used). Deferred to
    # call time so this module does NOT import the route module at load (the route imports
    # THIS module at module level — the correct, non-circular direction).
    from api.routes.mcp_server_grants import get_mcp_graph_service

    graph = get_mcp_graph_service()

    # (1) Resolve the agent SP (principalId) for THIS assignment BEFORE the revoke — it is
    # unrecoverable once revoke_app_role deletes the assignment. A missing assignment
    # (already gone) leaves agent_sp_id None → revoke-then-404 with no consent teardown.
    agent_sp_id: Optional[str] = None
    try:
        assignments = await graph.list_assignments(mcp.entra_sp_id)
    except GraphError as err:
        # Couldn't read assignments — log server-side and surface a recoverable error
        # rather than blindly revoking (we'd lose the chance to also clear the consent).
        logger.exception(
            "[mcp_grant] revoke: failed to list assignments for MCP %s before revoke",
            getattr(mcp, "id", "?"),
        )
        raise GrantReadError("failed to read the MCP's grants") from err
    agent_sp_id = next(
        (a.get("principalId") for a in assignments if a.get("id") == assignment_id),
        None,
    )

    # (2) Revoke the app-role assignment (the UI row).
    try:
        await graph.revoke_app_role(mcp.entra_sp_id, assignment_id)
    except GraphError as err:
        if err.status == 404:
            # Already revoked (FE double-click race) — the assignment is gone.
            raise GrantNotFoundError("grant not found") from err
        raise GrantRevokeFailedError("failed to revoke the grant") from err

    # (3) MULTIPLICITY GUARD + (4) consent teardown. Only when we resolved the agent SP.
    if agent_sp_id:
        try:
            remaining = await graph.list_assignments(mcp.entra_sp_id)
        except GraphError as err:
            # The assignment IS revoked; we just can't confirm whether a sibling role
            # remains. Surface a recoverable error so the caller can re-revoke (which is
            # idempotent) rather than silently leaving the consent in an unknown state.
            logger.exception(
                "[mcp_grant] revoke: failed to re-list assignments for MCP %s after "
                "revoking %s (consent teardown skipped)",
                getattr(mcp, "id", "?"),
                assignment_id,
            )
            raise GrantReadError(
                "grant revoked but consent cleanup could not be verified; re-revoke to retry"
            ) from err

        # Exclude OUR OWN just-deleted assignment_id from the sibling check: appRoleAssignedTo
        # is EVENTUALLY CONSISTENT, so this re-list (fired ms after the revoke) almost always
        # returns a STALE replica that still contains the assignment we JUST deleted. A stale
        # replica of our delete shares THIS assignment_id; a genuine sibling (e.g. Admin while
        # we revoked Invoker) has a DIFFERENT id. Filtering by id makes the guard immune to the
        # read-after-delete staleness of our own delete while still detecting a real sibling.
        agent_still_assigned = any(
            a.get("principalId") == agent_sp_id and a.get("id") != assignment_id
            for a in remaining
        )
        if not agent_still_assigned:
            # No sibling role remains for this agent on this MCP → the shared consent is no
            # longer needed: tear it down so the OBO kill switch is real.
            try:
                await graph.revoke_agent_obo_consent(agent_sp_id, mcp.entra_sp_id)
            except GraphError as err:
                # The assignment is already deleted; the consent revoke genuinely failed.
                # Log the REAL cause server-side (traceback carries no secret values) and
                # raise a FIXED-message domain error (never str(err) — T-GRAPH convention).
                # Re-revoke is idempotent, so the caller can retry.
                logger.exception(
                    "[mcp_grant] revoke: assignment %s removed but consent teardown failed "
                    "for agent %s -> MCP %s",
                    assignment_id,
                    agent_sp_id,
                    getattr(mcp, "id", "?"),
                )
                raise ConsentRevokeError(
                    "grant revoked but consent cleanup failed; re-revoke to retry"
                ) from err

    # E12 (Task T4, design §4/§7-revoke-env-sync): the Entra kill switch above has already
    # torn down enforcement (the security-critical part is DONE). Now SYNC the desired-state
    # set + runtime env so the agent stops even TRYING the dead MCP (no per-invoke
    # AADSTS65001 noise / wasted round-trip) and the env matches desired state. Entra FIRST →
    # remove from the set → rebuild env (the SAME load-bearing order as the apply path).
    #
    # Resolve the GOVERNED agent record by the agent SP (principalId) ALREADY resolved in
    # step 1 — the same agent-by-SP idiom the apply path uses. Deferred import of the
    # registry accessor (the non-circular direction; mirrors apply).
    from api.routes.agents import get_service as get_agent_service

    agent = next(
        (a for a in get_agent_service().list() if a.entra_sp_id == agent_sp_id),
        None,
    )

    if agent is not None and mcp.id in agent.mcp_server_ids:
        agent.mcp_server_ids.remove(mcp.id)
        get_agent_service().persist_identity(agent)
        try:
            # rebuild_runtime_mcp_env reads agent.mcp_server_ids (now WITHOUT the revoked
            # MCP), rebuilds the MCP_SERVERS env, and dispatches set_runtime_environment OFF
            # the event loop (a no-op for an agent with no runtime handle).
            await rebuild_runtime_mcp_env(agent)
        except Exception:  # noqa: BLE001 — the Entra kill switch ALREADY succeeded (access revoked);
            # a failed env rebuild leaves a STALE-but-harmless MCP entry on the runtime: it
            # fails closed (the agent's OBO to that MCP now gets AADSTS65001 and is
            # degrade-dropped) and self-heals on the next grant/revoke. Re-running THIS
            # revoke cannot fix it (the Entra revoke would 404). Log loud, do NOT raise.
            logger.exception(
                "[mcp_grant] revoke: runtime env rebuild failed for agent %s (MCP %s) — "
                "Entra access IS revoked; runtime env left stale (harmless, self-heals on "
                "next grant/revoke)",
                getattr(agent, "id", "?"),
                mcp.id,
            )
    elif agent is None:
        # Defensive — the normal path always resolves a governed agent (the kill switch only
        # ran because we resolved its SP). If it somehow can't be resolved here, the Entra
        # revoke ALREADY succeeded (the security-critical part is done) — log a warning and
        # return; do NOT raise (the env sync is best-effort relative to the kill switch).
        logger.warning(
            "[mcp_grant] revoke: could not resolve governed agent for SP %s — Entra revoke "
            "succeeded, env not rebuilt",
            agent_sp_id,
        )

    return None
