"""Shared multi-MCP runtime env rebuild helper (Epic 12, Task T1).

This module is the E12 single place that rebuilds an agent's runtime MCP environment
from its DESIRED-STATE set (``agent.mcp_server_ids``) + the MCP registry. It replaces the
flat, single-valued ``MCP_AUDIENCE`` / ``MCP_GATEWAY_URL`` keys (one effective MCP per
agent — the prototype bottleneck) with an ``MCP_SERVERS`` JSON list that carries every
granted MCP, so one governed agent can be wired to MORE THAN ONE MCP at a time.

DESIRED STATE vs. ENFORCED STATE (the core E12 principle): the registry is desired state —
intent + connection details, NEVER the authorization decision. Entra
(``oauth2PermissionGrant``) is the non-bypassable enforcement boundary. The asymmetry that
makes a rebuild-from-registry safe: a stale-extra entry (registry says YES, Entra says NO)
can only FAIL CLOSED (the OBO token mint fails — no access). So this helper SKIPS a
missing/unprovisioned MCP record with a warning and NEVER aborts the rebuild — a stale id
can only fail closed, never open.

``reconcile_runtime_mcp_env`` (E36/T12) is the READ-PATH backstop over the same rebuild: a runtime
REPLACEMENT drops the injected env with no signal to the platform, so a SUCCESSFUL read of a
runtime that lacks ``MCP_SERVERS`` while its record holds grants re-applies the desired state. It
runs on a read surface, so it is deliberately cheap and self-limiting: it heals only on EVIDENCE
(a read that reached the runtime), never waits for the runtime to come back READY, and drops a
trigger for an agent whose heal is already in flight.

``build_runtime_mcp_env`` is PURE (no IO — trivially unit-testable). ``rebuild_runtime_mcp_env``
is the IO twin: it resolves each id via the MCP registry singleton, builds the env, and
(only if the agent has a runtime handle) dispatches ``set_runtime_environment`` OFF the
event loop via ``anyio.to_thread.run_sync`` (the sync boto3 set must not block the uvicorn
loop — mirrors ``agent_mcp_grant.py``).

SECURITY (T-GRAPH carry-forward): NEVER logs a token/secret or any env VALUE — only key
names / ids / counts. The ``[mcp_env]`` log prefix mirrors ``agent_mcp_grant.py``'s
``[mcp_grant]`` style. The singleton accessors are module-level functions with deferred
imports inside (the non-circular direction, as in ``agent_mcp_grant.py``); kept at module
level so they are individually patchable.
"""

import functools
import json
import logging
import re
from typing import Mapping, Optional

import anyio.to_thread

from models.agent import resolve_runtime_arns

logger = logging.getLogger(__name__)

# Slug: collapse any run of non-[a-z0-9] into a single underscore, strip the ends.
_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _slug(name: str) -> str:
    """Deterministic, stable-across-rebuilds tool-namespace label from an MCP name.

    Lowercase, replace any run of non-``[a-z0-9]`` with a single ``_``, strip leading/
    trailing ``_``. An empty/symbol-only name falls back to ``"mcp"`` so the label is never
    blank (a blank prefix would break the runtime's ``{label}__{tool}`` namespacing).
    """
    slug = _SLUG_NON_ALNUM.sub("_", (name or "").lower()).strip("_")
    return slug or "mcp"


# --- singleton accessors (module-level so tests can monkeypatch them) --------
# Deferred imports inside (the non-circular direction — the route modules import service
# modules at load time, never the reverse; mirrors agent_mcp_grant.py).

def _get_mcp_registry():
    """The MCP registry singleton (per-id detail lookup — desired-state connection info)."""
    from api.routes.mcp_servers import get_service

    return get_service()


def _get_agent_identity_service():
    """The AgentIdentityService singleton (owns ``set_runtime_environment``)."""
    from api.routes.mcp_server_grants import get_agent_identity_service

    return get_agent_identity_service()


def build_runtime_mcp_env(mcps, provider_name: Optional[str]) -> dict[str, str]:
    """PURE: build the runtime env dict for a set of granted MCPs.

    Produces:
      - ``MCP_SERVERS``: a JSON list with one entry per MCP (in input order), each
        ``{"id", "audience", "gateway_url", "label"}``. ``audience`` is
        ``mcp.entra_app_audience`` (the OBO scope base); ``gateway_url`` is
        ``mcp.gateway_url`` (the streamable-http endpoint); ``label`` is a deterministic
        slug of ``mcp.name`` — the tool-namespace prefix.
      - ``CREDENTIAL_PROVIDER_NAME``: ONLY when ``provider_name`` is truthy (one OBO
        provider per agent — the SAME for every MCP, not per-MCP).
      - ``MCP_AUDIENCE`` / ``MCP_GATEWAY_URL``: always set to ``""`` to NEUTRALIZE the
        legacy single-MCP keys, so a not-yet-redeployed runtime cannot keep using a stale
        real value alongside the new list (avoids dual state — design §3.2/§5).

    LABEL COLLISION: two MCPs whose names slug to the same label would make tool namespacing
    ambiguous. On a repeat label, suffix ``_{mcp.id}`` (the FULL registry id — unique by
    construction in the registry, so the first suffixed candidate is always unique). A
    belt-and-suspenders loop handles the pathological case of a repeated full id by
    appending ``_2``, ``_3``, … (the first occurrence keeps the bare slug; only later
    collisions are suffixed — deterministic + stable across rebuilds for a fixed id ordering).
    """
    entries: list[dict[str, str]] = []
    seen_labels: set[str] = set()
    for mcp in mcps:
        base = _slug(mcp.name)
        label = base
        if label in seen_labels:
            label = f"{base}_{mcp.id}"   # full id — unique by construction in the registry
            # belt-and-suspenders: if somehow still seen (repeated full id), keep disambiguating
            n = 2
            while label in seen_labels:
                label = f"{base}_{mcp.id}_{n}"
                n += 1
        seen_labels.add(label)
        entries.append(
            {
                "id": mcp.id,
                "audience": mcp.entra_app_audience,
                "gateway_url": mcp.gateway_url,
                "label": label,
            }
        )

    env: dict[str, str] = {
        "MCP_SERVERS": json.dumps(entries),
        # Neutralize the legacy single-MCP keys (never leave a stale real value behind).
        "MCP_AUDIENCE": "",
        "MCP_GATEWAY_URL": "",
    }
    if provider_name:
        env["CREDENTIAL_PROVIDER_NAME"] = provider_name
    return env


async def rebuild_runtime_mcp_env(agent, *, wait_ready: bool = True) -> None:
    """IO: rebuild + push the agent's runtime MCP env from its desired-state set.

    Resolves each id in ``agent.mcp_server_ids`` via the MCP registry singleton; a
    missing/None record (a stale id) is SKIPPED with a warning — never fatal, since a
    stale-extra entry can only fail closed (design §2). Builds the env via
    ``build_runtime_mcp_env`` (with the agent's single OBO ``provider_name``), then — ONLY
    if the agent carries at least one runtime handle — dispatches the SYNC boto3
    ``set_runtime_environment`` OFF the event loop via ``anyio.to_thread.run_sync`` (never
    block the uvicorn loop — mirrors ``agent_mcp_grant.py``), ONCE PER per-stage runtime the
    agent owns (E28A/T1). An agent with no runtime handle at all (metadata-only / external) is
    a no-op (info log).

    ``wait_ready`` (E36/T12, fix round 1) is passed straight through to
    ``set_runtime_environment``: True (the DEFAULT — grant and revoke) polls each runtime to
    READY, so a governance mutation only reports success once it has seen the wiring converge;
    False (reconcile-on-read only) returns as soon as the update is accepted, because a read
    surface must not hold an ``anyio`` worker for the poll's minutes and the next read observes
    the outcome anyway.

    SECURITY: logs ids / counts only — never an env VALUE, token, or secret.
    """
    registry = _get_mcp_registry()
    mcps = []
    for mcp_id in agent.mcp_server_ids:
        mcp = registry.get(mcp_id)
        if mcp is None:
            # A stale/unprovisioned id — skip + warn (never abort; it can only fail closed).
            logger.warning(
                "[mcp_env] agent %s references unknown MCP %s — skipping",
                agent.id,
                mcp_id,
            )
            continue
        mcps.append(mcp)

    env = build_runtime_mcp_env(mcps, agent.oauth2_credential_provider_name)

    runtimes = resolve_runtime_arns(agent)
    if runtimes:
        logger.info(
            "[mcp_env] rebuilding runtime env (%d MCPs, %d keys) across %d runtime(s) "
            "for agent %s",
            len(mcps),
            len(env),
            len(runtimes),
            agent.id,
        )
        # E28A/T1 (D-A4 defect 4): EVERY per-stage runtime, not just the stored scalar. Since
        # T1b an agent owns one runtime per stage, and injecting into one left the other acting
        # on a revoked or superseded MCP set — a grant is a governance fact about the AGENT, not
        # about a stage, so there is no per-stage question to answer here.
        #
        # The fan-out lives HERE rather than behind an agent-level identity-service method
        # because `set_runtime_environment` is deliberately env-AGNOSTIC (a generic per-runtime
        # MERGE primitive; WHAT it is called with is this module's business) — and because this
        # module already owns the off-loop dispatch, which must stay one `run_sync` per SYNC
        # boto3 call so no ~minute+ poll-to-READY ever runs on the uvicorn loop.
        #
        # ATTEMPT-ALL-THEN-RAISE, mirroring the authorizer fan-out: the runtimes are independent
        # so one unreachable runtime must not stop the others from being brought into line, but
        # the grant route turns a raise into a fail-loud 5xx and a successful grant must imply a
        # fully-wired agent — reporting success while a runtime holds the old env would state a
        # governance fact the platform had not established.
        set_env = _get_agent_identity_service().set_runtime_environment
        if not wait_ready:
            # Bound ONLY on the no-wait path, so the grant/revoke hand-off keeps passing the BOUND
            # METHOD ITSELF to `run_sync` (what the grant-route tests assert on) and its behaviour
            # is byte for byte unchanged.
            set_env = functools.partial(set_env, wait_ready=False)
        failed: list[str] = []
        for stage, arn in runtimes.items():
            try:
                # SYNC boto3 with a ~minute+ poll-to-READY → OFF the uvicorn loop, per runtime.
                await anyio.to_thread.run_sync(set_env, arn, env)
            except Exception:  # noqa: BLE001 — per-runtime tolerance; re-raised below
                # Logged per runtime so every stage left holding stale env is nameable. Keys and
                # counts only — never an env VALUE (the module's standing security rule).
                logger.exception(
                    "[mcp_env] runtime env injection failed for agent %s stage %s",
                    agent.id,
                    stage,
                )
                failed.append(stage)
        if failed:
            raise RuntimeError(
                f"the runtime env could not be injected into {len(failed)} of "
                f"{len(runtimes)} runtimes of agent {agent.id}: {sorted(failed)}"
            )
    else:
        logger.info(
            "[mcp_env] agent %s has no runtime handle — skipping env rebuild",
            agent.id,
        )


# Per-agent in-flight guard for the read-path heal (E36/T12, fix round 1). Process-local and
# deliberately not a lock: the repository page fires the agent-level probe AND one probe per stage
# CONCURRENTLY, so without this a single page load produced 1+N simultaneous reconciles for the
# same agent, each full-replace-PUTing every one of its runtimes. Serializing them would only
# queue the duplicates; the second trigger has nothing left to contribute (the first re-derives the
# same desired state from the registry), so it is DROPPED. A bare set is sufficient because the
# check-then-add below contains no `await` — nothing can interleave inside it on the event loop —
# and it is emptied in a `finally`, so a raising heal cannot wedge an agent shut.
_RECONCILE_IN_FLIGHT: set[str] = set()


async def reconcile_runtime_mcp_env(agent, live_env: Optional[Mapping[str, str]]) -> bool:
    """Heal a runtime that LOST its MCP env, from a read of that runtime (E36/T12).

    Returns True iff a rebuild was applied. TRIGGER: ``agent.mcp_server_ids`` non-empty AND
    ``live_env is not None`` AND ``MCP_SERVERS`` is not a key of it — i.e. a read that REACHED
    the runtime and found the wiring gone. Delegates to :func:`rebuild_runtime_mcp_env` (already
    idempotent — desired state re-derived from the registry every time), WITHOUT waiting for the
    runtime to return to READY. NEVER raises out: it logs and returns False.

    WHY THIS EXISTS. A runtime REPLACEMENT drops the backend-injected env, and the platform gets
    no signal at all when it happens: our own pipeline replaces the resource on a rename, and a
    customer's ``agentcore launch`` (the ``applications/acme_*_agent/deploy.sh`` shape, deployed
    outside our pipeline) does the same for a registered-external agent whose deploy path we do
    not own. So the record can say "wired to an MCP" while the runtime carries nothing —
    governance state asserting a fact the runtime no longer implements.

    DELIBERATELY NOT LANE-SCOPED (research §3): it heals platform-deployed AND
    registered-external agents. There is no cheap lane discriminator anyway (``origin`` is never
    set to DEPLOYED; the authoritative repo lookup would add a DynamoDB read to a read route),
    and scoping it would remove the safety net from the one lane where a pipeline-side fix can
    still silently fail.

    KEY PRESENCE, NOT VALUE. The trigger asks only whether ``MCP_SERVERS`` is THERE. Its
    contents are desired state the grant flow owns, and diffing them on a read would make this
    the arbiter of a governance decision (and would rebuild on every read for any agent whose
    registry ordering merely differs).

    EVIDENCE, NOT ABSENCE OF EVIDENCE. ``live_env is None`` means the read never got an answer
    about the env — nothing deployed, the runtime gone, a Throttling/AccessDenied, an unreachable
    endpoint — and it is NOT a trigger. A wipe is only DETECTABLE from a read that reached the
    runtime, where an env without ``MCP_SERVERS`` (including a wholly empty ``{}``) is the wipe
    itself. Healing off ``None`` looked free because the rebuild is idempotent, but it never
    converged: every read of a deleted or cross-account runtime attempted the same doomed write
    and logged two tracebacks, and a throttled read answered AWS's back-off request with an EXTRA
    write against the same throttled API. Nothing is lost by waiting for evidence — reconcile-on-
    read's own premise is that another read is coming, and every case that used to heal off
    ``None`` was either unhealable (runtime gone, cross-account) or a no-op (no runtime handle).

    ONE HEAL PER AGENT AT A TIME (see :data:`_RECONCILE_IN_FLIGHT`). The repository page reads
    this endpoint 1+N times concurrently (agent-level probe + one per stage), and each read sees
    the same missing key, so the duplicates are dropped rather than queued.

    IT DOES NOT WAIT FOR READY (``wait_ready=False``). The poll inside
    ``set_runtime_environment`` sleeps up to 300 s per runtime while holding a shared ``anyio``
    worker thread, which no read handler may do; the next read observes whether the heal landed,
    which is what this function is.

    NEVER RAISES, unlike the grant path. ``rebuild_runtime_mcp_env`` raises when a runtime cannot
    be written — right for a grant, which must fail loud rather than report a half-wired agent.
    Here the caller is a read surface that is contractually 200-or-a-status, and a read that
    succeeded is no guarantee the WRITE will (a runtime already ``UPDATING``, a throttled update,
    an unwritable multi-stage sibling), so a propagating failure would blank the fleet view over a
    heal nobody asked for.

    SECURITY: logs ids / counts / key names only — never an env VALUE (this module's rule).
    """
    if not agent.mcp_server_ids:
        return False
    if live_env is None or "MCP_SERVERS" in live_env:
        return False
    if agent.id in _RECONCILE_IN_FLIGHT:
        logger.info(
            "[mcp_env] a reconcile-on-read is already in flight for agent %s — dropping this "
            "trigger (the one in flight re-applies the same desired state)",
            agent.id,
        )
        return False

    logger.info(
        "[mcp_env] agent %s has %d MCP grant(s) but the runtime it just reported (%d env key(s)) "
        "has no MCP_SERVERS — reconciling on read",
        agent.id,
        len(agent.mcp_server_ids),
        len(live_env),
    )
    _RECONCILE_IN_FLIGHT.add(agent.id)
    try:
        # No poll-to-READY: a read handler must not hold an anyio worker for the poll's minutes.
        await rebuild_runtime_mcp_env(agent, wait_ready=False)
    except Exception:  # noqa: BLE001 — best-effort heal on a read path; never propagate
        logger.exception(
            "[mcp_env] reconcile-on-read failed for agent %s — leaving the runtime as read",
            agent.id,
        )
        return False
    finally:
        # Always released, so a failed heal cannot wedge the agent out of future reconciles.
        _RECONCILE_IN_FLIGHT.discard(agent.id)
    return True
