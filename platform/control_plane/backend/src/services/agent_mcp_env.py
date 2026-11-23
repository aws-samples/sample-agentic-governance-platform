"""The agent's runtime MCP environment, rebuilt from its desired-state set and the MCP registry.

``MCP_SERVERS`` is a JSON list carrying every MCP the agent is wired to (granted or declared), so
one governed agent reaches more than one MCP; the legacy single-valued ``MCP_AUDIENCE`` /
``MCP_GATEWAY_URL`` keys are neutralised to ``""`` so a not-yet-redeployed runtime cannot keep a
stale value beside the list. The registry is desired state (intent and connection details), never
the authorization decision: Entra's ``oauth2PermissionGrant`` is the enforcement boundary, and a
stale entry (registry yes, Entra no) can only fail closed, which is what makes a
rebuild-from-registry safe and lets a missing record be skipped with a warning.

``build_runtime_mcp_env`` is pure; ``rebuild_runtime_mcp_env`` resolves the ids through the
registry and pushes the env to every per-stage runtime off the event loop;
``reconcile_runtime_mcp_env`` is the read-path backstop that re-applies the env when a successful
read of a runtime shows ``MCP_SERVERS`` gone; ``ungranted_tool_mcps`` is the agent-detail hint for
harness tools with no grant behind them. Nothing here logs an env value, a token or a secret: ids,
counts and key names only. The singleton accessors are module-level functions with deferred imports
so tests can patch them and so the route modules keep importing the services, never the reverse.
Decision: docs/adr/backend-mcp-grant-entra-first-then-runtime-env.md
"""

import functools
import json
import logging
import re
from typing import Mapping, Optional

import anyio.to_thread

from models.agent import is_harness_arn, resolve_runtime_arns

logger = logging.getLogger(__name__)

_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _slug(name: str) -> str:
    """A deterministic tool-namespace label from an MCP name: lowercase, any run of characters
    outside ``[a-z0-9]`` collapsed to one ``_``, the ends stripped. An empty or symbol-only name
    becomes ``"mcp"``, because a blank prefix would break the runtime's ``{label}__{tool}``
    namespacing."""
    slug = _SLUG_NON_ALNUM.sub("_", (name or "").lower()).strip("_")
    return slug or "mcp"


def _get_mcp_registry():
    """The MCP registry singleton (per-id detail lookup for desired-state connection info)."""
    from api.routes.mcp_servers import get_service

    return get_service()


def _get_agent_identity_service():
    """The AgentIdentityService singleton (owns ``set_runtime_environment``)."""
    from api.routes.mcp_server_grants import get_agent_identity_service

    return get_agent_identity_service()


def ungranted_tool_mcps(agent, harness_arns) -> list:
    """Sync: the MCP records whose gateway a harness of ``agent`` lists in ``tools`` while the agent
    holds no grant for it. Such a harness fails every invoke, even a prompt that uses no tool,
    because its token exchange to that gateway is refused.

    Declared is ``GetHarness`` ``tools[].config.agentCoreGateway.gatewayArn`` (only harness ARNs are
    read; a runtime has no declared tools); governed is a same-tenant or shared ``kind=gateway``
    record carrying that ARN; granted is ``agent.mcp_server_ids``. A read failure (a harness in a
    tenant account is unreadable with the ambient client) reports nothing and is logged: this feeds
    a hint, never a gate, so it never raises.
    """
    declared: set[str] = set()
    for arn in harness_arns:
        if not is_harness_arn(arn):
            continue
        try:
            harness = _get_agent_identity_service()._control.get_harness(harnessId=arn.rsplit("/", 1)[-1])[
                "harness"
            ]
        except Exception:
            logger.warning("[mcp_env] could not read the harness tools of agent %s", agent.id, exc_info=True)
            continue
        for tool in harness.get("tools") or []:
            gw = ((tool.get("config") or {}).get("agentCoreGateway") or {}).get("gatewayArn")
            if gw:
                declared.add(gw)
    if not declared:
        return []
    from models.mcp_server import Kind

    try:
        mcps = _get_mcp_registry().list(kind=Kind.GATEWAY)
    except Exception:
        logger.warning("[mcp_env] could not list MCP records for agent %s", agent.id, exc_info=True)
        return []
    granted = set(agent.mcp_server_ids or [])
    return [
        m
        for m in mcps
        if m.kind == Kind.GATEWAY
        and m.gateway_arn in declared
        and (m.tenant_id == getattr(agent, "tenant_id", None) or getattr(m, "shared", False))
        and m.id not in granted
    ]


def runtime_mcp_ids(agent) -> list[str]:
    """The MCP ids the runtime is wired to: granted (``mcp_server_ids``) first, then declared
    (``declared_mcp_server_ids``) not already present, deduplicated. Developers declare, governance
    authorizes: a declared-but-ungranted server is wired and refused by Entra (no consent), never
    silently absent. ``getattr`` because callers also pass bare namespaces."""
    ids: list[str] = []
    for mcp_id in list(agent.mcp_server_ids or []) + list(
        getattr(agent, "declared_mcp_server_ids", None) or []
    ):
        if mcp_id not in ids:
            ids.append(mcp_id)
    return ids


def build_runtime_mcp_env(mcps, provider_name: Optional[str]) -> dict[str, str]:
    """Pure: the runtime env for a list of MCP records.

    ``MCP_SERVERS`` is a JSON list, one ``{"id", "audience", "gateway_url", "label"}`` per MCP in
    input order (``audience`` is the OBO scope base ``entra_app_audience``, ``gateway_url`` the
    streamable-http endpoint, ``label`` the slug of the name); ``CREDENTIAL_PROVIDER_NAME`` is set
    only when ``provider_name`` is truthy (one OBO provider per agent, the same for every MCP);
    ``MCP_AUDIENCE`` and ``MCP_GATEWAY_URL`` are always ``""``.

    Two MCPs whose names slug to the same label would make tool namespacing ambiguous, so a repeat
    label is suffixed with the full registry id (unique by construction) and, should that repeat
    too, with ``_2``, ``_3``...; the first occurrence keeps the bare slug, which is deterministic
    across rebuilds for a fixed ordering.
    """
    entries: list[dict[str, str]] = []
    seen_labels: set[str] = set()
    for mcp in mcps:
        base = _slug(mcp.name)
        label = base
        if label in seen_labels:
            label = f"{base}_{mcp.id}"
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
        "MCP_AUDIENCE": "",
        "MCP_GATEWAY_URL": "",
    }
    if provider_name:
        env["CREDENTIAL_PROVIDER_NAME"] = provider_name
    return env


async def rebuild_runtime_mcp_env(agent, *, wait_ready: bool = True) -> None:
    """Rebuild and push the agent's runtime MCP env from its desired-state set.

    Each id of :func:`runtime_mcp_ids` is resolved through the registry; a missing record (a stale
    id) is skipped with a warning, never fatal. The env is pushed to every per-stage runtime the
    agent owns through the sync ``set_runtime_environment``, one ``run_sync`` per runtime so no
    poll-to-READY runs on the event loop; a grant is a fact about the agent, not a stage. Every
    runtime is attempted and the failures are raised together afterwards (``RuntimeError``), because
    the runtimes are independent but a successful grant must mean a fully wired agent. An agent
    with no runtime handle is a no-op.

    ``wait_ready`` is passed straight through: ``True`` (grant and revoke) polls each runtime to
    READY so a governance mutation reports success only once the wiring converged; ``False``
    (reconcile-on-read) returns once the update is accepted, because a read surface must not hold a
    worker thread for the poll's minutes. The bound method itself is passed to ``run_sync`` on the
    default path, which the grant-route tests assert on.
    """
    registry = _get_mcp_registry()
    mcps = []
    for mcp_id in runtime_mcp_ids(agent):
        mcp = registry.get(mcp_id)
        if mcp is None:
            logger.warning("[mcp_env] agent %s references unknown MCP %s — skipping", agent.id, mcp_id)
            continue
        mcps.append(mcp)

    env = build_runtime_mcp_env(mcps, agent.oauth2_credential_provider_name)

    runtimes = resolve_runtime_arns(agent)
    if not runtimes:
        logger.info("[mcp_env] agent %s has no runtime handle — skipping env rebuild", agent.id)
        return
    logger.info(
        "[mcp_env] rebuilding runtime env (%d MCPs, %d keys) across %d runtime(s) for agent %s",
        len(mcps),
        len(env),
        len(runtimes),
        agent.id,
    )
    set_env = _get_agent_identity_service().set_runtime_environment
    if not wait_ready:
        set_env = functools.partial(set_env, wait_ready=False)
    failed: list[str] = []
    for stage, arn in runtimes.items():
        try:
            await anyio.to_thread.run_sync(set_env, arn, env)
        except Exception:
            logger.exception("[mcp_env] runtime env injection failed for agent %s stage %s", agent.id, stage)
            failed.append(stage)
    if failed:
        raise RuntimeError(
            f"the runtime env could not be injected into {len(failed)} of "
            f"{len(runtimes)} runtimes of agent {agent.id}: {sorted(failed)}"
        )


_RECONCILE_IN_FLIGHT: set[str] = set()


async def reconcile_runtime_mcp_env(agent, live_env: Optional[Mapping[str, str]]) -> bool:
    """Heal a runtime that lost its MCP env, from a read of that runtime. Returns whether a rebuild
    was applied.

    A runtime replacement drops the backend-injected env with no signal to the platform (our own
    pipeline replaces the resource on a rename; a customer's ``agentcore launch`` does the same for
    a registered-external agent), so the record can say "wired" while the runtime carries nothing.
    The trigger is evidence, not its absence: :func:`runtime_mcp_ids` non-empty, ``live_env`` not
    ``None`` (a read that reached the runtime) and ``MCP_SERVERS`` not among its keys. Key presence,
    not value: the contents are desired state the grant flow owns. ``live_env is None`` (nothing
    deployed, runtime gone, throttled, cross-account) is not a trigger, because every such heal
    was either unhealable or a no-op and a throttled read must not answer with an extra write.

    Not lane-scoped: it heals platform-deployed and registered-external agents alike. One heal per
    agent at a time: the repository page fires the agent-level probe and one probe per stage
    concurrently, each seeing the same missing key, so the duplicates are dropped rather than
    queued (``_RECONCILE_IN_FLIGHT`` is a bare set because the check-then-add holds no ``await``,
    and it is released in ``finally`` so a failing heal cannot wedge an agent shut). It does not
    wait for READY and it never raises: the caller is a read surface, and a propagating failure
    would blank the fleet view over a heal nobody asked for.
    """
    wired = runtime_mcp_ids(agent)
    if not wired:
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
        "[mcp_env] agent %s has %d wired MCP(s) but the runtime it just reported (%d env key(s)) "
        "has no MCP_SERVERS — reconciling on read",
        agent.id,
        len(wired),
        len(live_env),
    )
    _RECONCILE_IN_FLIGHT.add(agent.id)
    try:
        await rebuild_runtime_mcp_env(agent, wait_ready=False)
    except Exception:
        logger.exception(
            "[mcp_env] reconcile-on-read failed for agent %s — leaving the runtime as read", agent.id
        )
        return False
    finally:
        _RECONCILE_IN_FLIGHT.discard(agent.id)
    return True
