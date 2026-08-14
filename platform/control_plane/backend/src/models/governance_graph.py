"""Governance-graph read-models (Epic 11, Task 3).

Plain read-DTOs backing the read-only `GET /governance-graph` aggregation
endpoint + the lazy `GET /governance-graph/principals/{oid}` Entra lookup. Unlike
the registry models (`agent.py`, `mcp_server.py`), these carry NO envelope
(de)serialization — the aggregation service composes them from already-hydrated
registry + Graph reads. The shapes follow the Epic 11 governance-graph plan's "Backend
wire contract" + "Pydantic models" blocks.

Mutable-default note: the plain literals `metadata: dict = {}` / `group_names:
list[str] = []` are safe and idiomatic in Pydantic v2 — it deep-copies defaults
per instance (verified by tests/test_governance_graph_model.py), so two instances
never share the same default object. Pinned verbatim from the plan contract.
"""

from typing import Literal, Optional

from pydantic import BaseModel


class GraphNode(BaseModel):
    type: Literal["user", "group", "agent", "mcp"]
    id: str            # "<type>:<entity-id>"
    label: str
    ref_id: str        # bare entity id for detail links
    metadata: dict = {}   # type-specific; see wire contract


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    type: Literal["access", "can_call"]
    role: str
    has_policy: bool = False


class GovernanceGraph(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class PrincipalDetail(BaseModel):
    id: str
    display_name: str
    kind: Literal["user", "group"]
    user_principal_name: Optional[str] = None
    mail: Optional[str] = None
    job_title: Optional[str] = None
    group_names: list[str] = []
