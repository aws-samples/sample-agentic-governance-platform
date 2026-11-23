"""Governance-graph aggregation: registries plus Graph reads into nodes and edges, read-only.

The aggregator behind ``GET /governance-graph`` and the lazy
``GET /governance-graph/principals/{oid}``. It composes the registry lists (``agent_service.list()``,
``mcp_service.list()``) with the ``GraphService`` reads (``list_assignments``,
``list_agent_mcp_grants``, ``get_principal``, ``list_member_group_ids``) into a ``GovernanceGraph``
and resolves one user or group principal's Entra detail. It calls only Graph read methods and never
touches Cedar: ``has_policy`` is derived from the already-listed ``McpServer.cedar_enforcement_mode``.

Every provisioned agent's two Graph reads fan out concurrently; a per-agent Graph failure degrades
that agent to no edges and the aggregation continues. Unprovisioned agents are isolated nodes and
cost no Graph call. The role mapping replicates ``api.routes.grants._role_for_app_role_id`` so this
module never imports from ``api.routes``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import ClassVar, Optional

from core.errors import AppError, Outcome
from models.governance_graph import (
    GovernanceGraph,
    GraphEdge,
    GraphNode,
    PrincipalDetail,
)
from services.graph_errors import GraphError
from services.tenant_resolver import TenantContext, visible

logger = logging.getLogger(__name__)


class GovernanceGraphError(AppError):
    """A principal lookup was refused: a bad kind, an unknown principal, or a Graph failure.

    Every detail is a fixed literal; the message is for logs.
    """

    domain: ClassVar[str] = "governance_graph"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "invalid_kind": Outcome(400, "kind must be 'user' or 'group'"),
        "principal_not_found": Outcome(404, "principal not found"),
        "principal_lookup_failed": Outcome(502, "failed to resolve the principal"),
    }

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message, kind)


def _role_for_app_role_id(entity, app_role_id: Optional[str]) -> str:
    """Map a Graph appRoleId GUID to "Invoker" or "Admin" through the entity's stored ids.

    An id matching neither, or no id, is "Unknown", so a role minted by another tool or a stale
    assignment never breaks the graph. ``entity`` is an Agent or an McpServer; both carry
    ``invoker_role_id`` and ``admin_role_id``.
    """
    if app_role_id and app_role_id == entity.invoker_role_id:
        return "Invoker"
    if app_role_id and app_role_id == entity.admin_role_id:
        return "Admin"
    return "Unknown"


def _is_provisioned(agent) -> bool:
    """An agent is Graph-readable iff its identity is provisioned and it has an SP id."""
    return agent.identity_status == "provisioned" and bool(agent.entra_sp_id)


def _enum_value(value):
    """Coerce a (possibly Enum) field to its plain JSON-safe string value."""
    return getattr(value, "value", value)


class GovernanceGraphService:
    """Composes the registry and Graph reads into a ``GovernanceGraph``.

    Dependencies are injected: the route constructs the service from the shared singletons, and
    this service never builds a registry or Graph client itself.
    """

    def __init__(self, agent_service, mcp_service, graph_service):
        self._agents = agent_service
        self._mcps = mcp_service
        self._graph = graph_service

    async def build(self, *, ctx: TenantContext) -> GovernanceGraph:
        """Fan out over the registries and Graph and return the governance graph.

        Nodes are every agent and MCP, isolated ones included, plus the deduplicated user and group
        principals found across the provisioned agents' assignments, entity nodes first. Edges are
        User/Group to Agent ("access") and Agent to MCP ("can_call", carrying ``has_policy``).
        A non-global ``ctx`` restricts the graph to the subgraph induced by the caller's visible
        set: agents with ``visible(ctx, tenant_id)`` and MCPs with ``visible(ctx, tenant_id,
        shared=shared)``, the registry list filters' semantics (``published`` grants marketplace
        discovery, not graph visibility). Filtering the two input lists up front induces the
        subgraph: principal nodes and access edges derive only from the kept agents, and a
        can_call grant to a dropped MCP misses the reverse join and is skipped, so no edge dangles.
        ``ctx.is_global`` keeps the full graph.
        """
        agents = self._agents.list()
        mcps = self._mcps.list()

        if not ctx.is_global:
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

        by_sp = {m.entra_sp_id: m for m in mcps if m.entra_sp_id}

        nodes: list[GraphNode] = []

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

        provisioned = [a for a in agents if _is_provisioned(a)]
        per_agent = await asyncio.gather(*(self._edges_for_agent(a, by_sp) for a in provisioned))

        principal_nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []
        for principals, agent_edges in per_agent:
            for node in principals:
                principal_nodes.setdefault(node.id, node)
            edges.extend(agent_edges)

        nodes.extend(principal_nodes.values())

        return GovernanceGraph(nodes=nodes, edges=edges)

    async def _edges_for_agent(self, agent, by_sp: dict) -> tuple[list[GraphNode], list[GraphEdge]]:
        """One provisioned agent's principal nodes and its access and can_call edges.

        A Graph failure for this agent is logged and degrades it to no edges so ``build()``
        continues. Service-principal assignments produce no node and no edge; a can_call grant
        whose ``resourceId`` is not a known MCP SP (Microsoft Graph itself, say) is dropped.
        """
        sp = agent.entra_sp_id
        try:
            assignments, grants = await asyncio.gather(
                self._graph.list_assignments(sp),
                self._graph.list_agent_mcp_grants(sp),
            )
        except Exception:
            logger.warning(
                "governance-graph: Graph read failed for agent %s (sp=%s); degrading to no edges",
                agent.id,
                sp,
                exc_info=True,
            )
            return [], []

        principal_nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []

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

    async def get_principal(self, oid: str, kind: str) -> PrincipalDetail:
        """Resolve one user or group principal's Entra detail, on node click.

        A ``kind`` other than "user" or "group" is ``invalid_kind`` before any Graph call. A Graph
        404 on the principal read or on a user's membership read is ``principal_not_found``; any
        other ``GraphError`` on those two reads is ``principal_lookup_failed``. For a user the group
        names come from ``list_member_group_ids`` plus one ``get_principal(gid, "group")`` each, a
        failing group skipped; a group's ``group_names`` is empty.
        """
        if kind not in ("user", "group"):
            raise GovernanceGraphError(f"unsupported principal kind: {kind!r}", "invalid_kind")

        try:
            raw = await self._graph.get_principal(oid, kind)
            group_names = await self._resolve_group_names(oid) if kind == "user" else []
        except GraphError as err:
            if err.status == 404:
                raise GovernanceGraphError(f"principal {oid!r} not found", "principal_not_found") from err
            raise GovernanceGraphError("the Graph read failed", "principal_lookup_failed") from err

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
        """A user's transitive group display names; the membership read raises, a failing group is skipped."""
        group_ids = await self._graph.list_member_group_ids(user_oid)
        names: list[str] = []
        for gid in group_ids:
            try:
                grp = await self._graph.get_principal(gid, "group")
            except Exception:
                logger.warning(
                    "governance-graph: failed to resolve group %s for user %s; skipping",
                    gid,
                    user_oid,
                    exc_info=True,
                )
                continue
            name = grp.get("displayName")
            if name:
                names.append(name)
        return names
