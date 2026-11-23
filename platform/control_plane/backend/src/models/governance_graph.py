"""Governance-graph read models backing ``GET /governance-graph`` and the lazy
``GET /governance-graph/principals/{oid}`` Entra lookup.

Plain read DTOs: unlike the registry models they carry no envelope (de)serialization; the aggregation
service composes them from already-hydrated registry and Graph reads. The bare ``= {}`` and ``= []``
defaults are safe in pydantic v2, which deep-copies a default per instance;
``tests/test_governance_graph_model.py`` pins the isolation.
"""

from typing import Literal, Optional

from pydantic import Field

from models.base import WireModel


class GraphNode(WireModel):
    type: Literal["user", "group", "agent", "mcp"]
    id: str = Field(description="<type>:<entity-id>")
    label: str
    ref_id: str = Field(description="The bare entity id, for detail links")
    metadata: dict = Field(default={}, description="Type-specific")


class GraphEdge(WireModel):
    id: str
    source: str
    target: str
    type: Literal["access", "can_call"]
    role: str
    has_policy: bool = False


class GovernanceGraph(WireModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class PrincipalDetail(WireModel):
    id: str
    display_name: str
    kind: Literal["user", "group"]
    user_principal_name: Optional[str] = None
    mail: Optional[str] = None
    job_title: Optional[str] = None
    group_names: list[str] = Field(default_factory=list)
