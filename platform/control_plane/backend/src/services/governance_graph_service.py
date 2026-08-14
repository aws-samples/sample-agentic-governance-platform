"""Governance-graph aggregation service (Epic 11, Task 4).

The read-only aggregator behind ``GET /governance-graph`` (+ the lazy
``GET /governance-graph/principals/{oid}``). It composes the existing registry
lists (``agent_service.list()`` / ``mcp_service.list()`` — sync boto3) with the
async ``GraphService`` reads (``list_assignments`` / ``list_agent_mcp_grants`` /
``get_principal`` / ``list_member_group_ids``) into a ``GovernanceGraph`` of
nodes + edges, and resolves a single user/group principal's Entra detail.

READ-ONLY BY DESIGN (research §5): this service calls ONLY GraphService READ
methods. It NEVER provisions/assigns/revokes, and NEVER touches Cedar
(``list_policies`` / ``add_policy`` / enforcement). ``has_policy`` is derived
purely from the already-listed ``McpServer.cedar_enforcement_mode`` field — no
extra AWS/Cedar round-trip.

Composition + concurrency (research §3):
  - ``agent_service.list()`` + ``mcp_service.list()`` once each (sync up front).
  - Build the ``entra_sp_id → McpServer`` reverse-join map once.
  - For each PROVISIONED agent (``identity_status == "provisioned" and entra_sp_id``,
    mirroring ``routes/grants._is_provisioned``) fan ``list_assignments`` +
    ``list_agent_mcp_grants`` out concurrently via ``asyncio.gather``. Each
    agent's Graph work is wrapped in try/except: a per-agent Graph error degrades
    that agent to "no edges" (logged) and aggregation continues — a single agent's
    failure must NOT 500 the whole graph (mirror the listing endpoints' posture).
  - Unprovisioned agents appear as isolated nodes; NO Graph call is made for them.

Role mapping mirrors ``routes/grants._role_for_app_role_id`` — the same
Invoker/Admin/Unknown logic is replicated locally (``_role_for_app_role_id``
below) rather than imported, to keep this service free of an ``api.routes``
dependency (the route layer imports the service, not the reverse) and to avoid a
circular import. Behavior is identical to the route helper.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from models.governance_graph import (
    GovernanceGraph,
    GraphEdge,
    GraphNode,
    PrincipalDetail,
)
from services.tenant_resolver import visible

logger = logging.getLogger(__name__)


def _role_for_app_role_id(entity, app_role_id: Optional[str]) -> str:
    """Map a Graph appRoleId GUID → "Invoker"/"Admin" via the entity's stored ids.

    Replicates ``api.routes.grants._role_for_app_role_id`` verbatim (same shape,
    same tolerant fallback): an id matching neither (or a None id) → "Unknown".
    ``entity`` is an Agent (for access edges) or an McpServer (for can_call edges)
    — both carry ``invoker_role_id`` / ``admin_role_id``.
    """
    if app_role_id and app_role_id == entity.invoker_role_id:
        return "Invoker"
    if app_role_id and app_role_id == entity.admin_role_id:
        return "Admin"
    return "Unknown"


def _is_provisioned(agent) -> bool:
    """An agent is Graph-readable iff its identity is provisioned + it has an SP id.

    Mirrors ``api.routes.grants._is_provisioned``.
    """
    return agent.identity_status == "provisioned" and bool(agent.entra_sp_id)


def _enum_value(value):
    """Coerce a (possibly Enum) field to its plain JSON-safe string value."""
    return getattr(value, "value", value)


class GovernanceGraphService:
    """Composes the registry + Graph reads into a ``GovernanceGraph``.

    Dependencies are INJECTED (the route constructs the service from the shared
    DI singletons); this service never constructs an ``AgentRegistryService`` /
    ``McpServerRegistryService`` / ``GraphService`` itself.
    """

    def __init__(self, agent_service, mcp_service, graph_service):
        self._agents = agent_service
        self._mcps = mcp_service
        self._graph = graph_service

    # -- the aggregation ----------------------------------------------------

    async def build(self, *, ctx=None) -> GovernanceGraph:
        """Fan out over the registries + Graph and return the governance graph.

        Nodes: ALL agents + ALL MCPs (even isolated) + the deduped set of
        user/group principals discovered across all provisioned agents'
        assignments. Edges: User/Group→Agent ("access") + Agent→MCP ("can_call",
        carrying ``has_policy``). See the module docstring for the contract.

        Multi-tenancy (E24/T8): a non-global ``ctx`` restricts the graph to the
        subgraph INDUCED by the caller's visible set — agents with
        ``visible(ctx, tenant_id)`` and MCPs with ``visible(ctx, tenant_id,
        shared=shared)`` (the exact Task 5 list-filtering semantics; ``published``
        grants marketplace discovery only, NOT graph visibility). Filtering the
        two input lists up front induces the subgraph automatically: principal
        nodes + access edges derive only from the kept agents' fan-out, and a
        can_call grant to a dropped MCP misses the ``by_sp`` reverse-join and is
        skipped — no dangling edges. ``ctx=None`` or ``ctx.is_global`` keeps the
        full graph (admin unchanged).
        """
        agents = self._agents.list()
        mcps = self._mcps.list()

        if ctx is not None and not ctx.is_global:
            agents = [a for a in agents if visible(ctx, getattr(a, "tenant_id", None))]
            mcps = [
                m
                for m in mcps
                if visible(
                    ctx,
                    getattr(m, "tenant_id", None),
                    shared=bool(getattr(m, "shared", False)),
                )
            ]

        # entra_sp_id → McpServer reverse-join map (only provisioned MCPs carry an
        # SP id). Built once; used to resolve each agent→MCP grant's resourceId.
        by_sp = {m.entra_sp_id: m for m in mcps if m.entra_sp_id}

        nodes: list[GraphNode] = []

        # Entity nodes first (deterministic insertion order: agents, then MCPs).
        for a in agents:
            nodes.append(
                GraphNode(
                    type="agent",
                    id=f"agent:{a.id}",
                    label=a.name,
                    ref_id=a.id,
                    metadata={
                        "origin": _enum_value(a.origin),
                        "lifecycle_state": _enum_value(a.lifecycle_state),
                        "identity_status": _enum_value(a.identity_status),
                        "platform": _enum_value(a.platform),
                    },
                )
            )
        for m in mcps:
            nodes.append(
                GraphNode(
                    type="mcp",
                    id=f"mcp:{m.id}",
                    label=m.name,
                    ref_id=m.id,
                    metadata={
                        "kind": _enum_value(m.kind),
                        "cedar_enforcement_mode": _enum_value(m.cedar_enforcement_mode),
                        "identity_status": _enum_value(m.identity_status),
                    },
                )
            )

        # Fan the per-agent Graph reads out concurrently. Only provisioned agents
        # get Graph calls; unprovisioned agents stay isolated nodes.
        provisioned = [a for a in agents if _is_provisioned(a)]
        per_agent = await asyncio.gather(
            *(self._edges_for_agent(a, by_sp) for a in provisioned)
        )

        # Dedup principal nodes by their prefixed node id across ALL agents.
        principal_nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []
        for principals, agent_edges in per_agent:
            for node in principals:
                principal_nodes.setdefault(node.id, node)
            edges.extend(agent_edges)

        # Append the deduped principals after the entity nodes (deterministic).
        nodes.extend(principal_nodes.values())

        return GovernanceGraph(nodes=nodes, edges=edges)

    async def _edges_for_agent(
        self, agent, by_sp: dict
    ) -> tuple[list[GraphNode], list[GraphEdge]]:
        """Resolve ONE provisioned agent's principal nodes + its access/can_call
        edges. A per-agent Graph error is caught here: the agent degrades to no
        edges (logged) so ``build()`` continues — it must never 500 the graph.
        """
        sp = agent.entra_sp_id
        try:
            assignments, grants = await asyncio.gather(
                self._graph.list_assignments(sp),
                self._graph.list_agent_mcp_grants(sp),
            )
        except Exception:  # noqa: BLE001 — degrade-not-fail per the contract.
            logger.warning(
                "governance-graph: Graph read failed for agent %s (sp=%s); "
                "degrading to no edges",
                agent.id,
                sp,
                exc_info=True,
            )
            return [], []

        principal_nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []

        # User/Group → Agent ("access"). ServicePrincipal principals (agent→agent)
        # are out of scope and skipped.
        for a in assignments:
            ptype = a.get("principalType")
            if ptype not in ("User", "Group"):
                continue
            principal_id = a.get("principalId") or ""
            node_prefix = "user" if ptype == "User" else "group"
            principal_nodes.append(
                GraphNode(
                    type=node_prefix,
                    id=f"{node_prefix}:{principal_id}",
                    label=a.get("principalDisplayName") or "",
                    ref_id=principal_id,
                    metadata={"principal_type": ptype},
                )
            )
            edges.append(
                GraphEdge(
                    id=a.get("id", ""),
                    source=f"{node_prefix}:{principal_id}",
                    target=f"agent:{agent.id}",
                    type="access",
                    role=_role_for_app_role_id(agent, a.get("appRoleId")),
                )
            )

        # Agent → MCP ("can_call"). Reverse-join resourceId → our McpServer; drop
        # grants whose resourceId is not a known MCP SP (e.g. Microsoft Graph).
        for g in grants:
            mcp = by_sp.get(g.get("resourceId"))
            if mcp is None:
                continue
            edges.append(
                GraphEdge(
                    id=g.get("id", ""),
                    source=f"agent:{agent.id}",
                    target=f"mcp:{mcp.id}",
                    type="can_call",
                    role=_role_for_app_role_id(mcp, g.get("appRoleId")),
                    has_policy=mcp.cedar_enforcement_mode != "none",
                )
            )

        return principal_nodes, edges

    # -- lazy principal detail ----------------------------------------------

    async def get_principal(self, oid: str, kind: str) -> PrincipalDetail:
        """Resolve ONE user/group principal's Entra detail (lazy, on node click).

        ``kind`` must be "user" or "group" (else ``ValueError`` — before any Graph
        call). The PRIMARY ``graph_service.get_principal(oid, kind)`` call's errors
        PROPAGATE (the route maps 404→404, other→502). For ``kind=="user"`` the
        group names are resolved best-effort via ``list_member_group_ids`` +
        per-id ``get_principal(gid, "group")`` — a per-group resolve failure is
        skipped, not fatal. Groups get ``group_names == []``.
        """
        if kind not in ("user", "group"):
            raise ValueError(f"unsupported principal kind: {kind!r}")

        raw = await self._graph.get_principal(oid, kind)

        group_names: list[str] = []
        if kind == "user":
            group_names = await self._resolve_group_names(oid)

        return PrincipalDetail(
            id=oid,
            display_name=raw.get("displayName") or "",
            kind=kind,
            user_principal_name=raw.get("userPrincipalName"),
            mail=raw.get("mail"),
            job_title=raw.get("jobTitle"),
            group_names=group_names,
        )

    async def _resolve_group_names(self, user_oid: str) -> list[str]:
        """Resolve a user's transitive group display names (best-effort).

        ``list_member_group_ids`` errors propagate (it is the membership read);
        each per-group name resolve is wrapped so one failing group is skipped,
        not fatal — the demo-quality contract.
        """
        group_ids = await self._graph.list_member_group_ids(user_oid)
        names: list[str] = []
        for gid in group_ids:
            try:
                grp = await self._graph.get_principal(gid, "group")
            except Exception:  # noqa: BLE001 — skip a group we cannot resolve.
                logger.warning(
                    "governance-graph: failed to resolve group %s for user %s; "
                    "skipping",
                    gid,
                    user_oid,
                    exc_info=True,
                )
                continue
            name = grp.get("displayName")
            if name:
                names.append(name)
        return names
