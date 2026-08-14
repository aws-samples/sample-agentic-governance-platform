"""Tests for the governance-graph Pydantic read-models (Epic 11, Task 3).

Spec: the Epic 11 governance-graph plan (Task 3 + Cross-task contracts -> Pydantic
      models / Backend wire contract) — a design artifact kept outside this repository.

These are pure-python read-DTO models: no envelope, no boto3, no FastAPI. They
back the read-only GET /governance-graph aggregation endpoint.
"""

import pytest
from pydantic import ValidationError

from models.governance_graph import (
    GovernanceGraph,
    GraphEdge,
    GraphNode,
    PrincipalDetail,
)


# ---------------------------------------------------------------------------
# Construction + model_dump key coverage (matches the wire contract)
# ---------------------------------------------------------------------------

def test_graph_node_constructs_and_dumps_wire_keys():
    node = GraphNode(
        type="agent",
        id="agent:rec-1",
        label="claims-triage-de",
        ref_id="rec-1",
        metadata={"origin": "Registered", "lifecycle_state": "approved"},
    )
    dumped = node.model_dump()
    assert set(dumped) == {"type", "id", "label", "ref_id", "metadata"}
    assert dumped["type"] == "agent"
    assert dumped["id"] == "agent:rec-1"
    assert dumped["label"] == "claims-triage-de"
    assert dumped["ref_id"] == "rec-1"
    assert dumped["metadata"] == {"origin": "Registered", "lifecycle_state": "approved"}


def test_graph_edge_constructs_and_dumps_wire_keys():
    edge = GraphEdge(
        id="asg-1",
        source="agent:rec-1",
        target="mcp:rec-2",
        type="can_call",
        role="Invoker",
        has_policy=True,
    )
    dumped = edge.model_dump()
    assert set(dumped) == {"id", "source", "target", "type", "role", "has_policy"}
    assert dumped["id"] == "asg-1"
    assert dumped["source"] == "agent:rec-1"
    assert dumped["target"] == "mcp:rec-2"
    assert dumped["type"] == "can_call"
    assert dumped["role"] == "Invoker"
    assert dumped["has_policy"] is True


def test_governance_graph_constructs_and_dumps_wire_keys():
    graph = GovernanceGraph(
        nodes=[
            GraphNode(type="user", id="user:oid-1", label="Maria Bauer", ref_id="oid-1"),
            GraphNode(type="agent", id="agent:rec-1", label="claims-triage-de", ref_id="rec-1"),
        ],
        edges=[
            GraphEdge(
                id="asg-1",
                source="user:oid-1",
                target="agent:rec-1",
                type="access",
                role="Invoker",
            )
        ],
    )
    dumped = graph.model_dump()
    assert set(dumped) == {"nodes", "edges"}
    assert len(dumped["nodes"]) == 2
    assert len(dumped["edges"]) == 1
    assert dumped["nodes"][0]["type"] == "user"
    assert dumped["edges"][0]["type"] == "access"


def test_principal_detail_constructs_and_dumps_wire_keys():
    detail = PrincipalDetail(
        id="oid-1",
        display_name="Maria Bauer",
        kind="user",
        user_principal_name="maria.bauer@example.onmicrosoft.com",
        mail="maria.bauer@example.com",
        job_title="Claims Officer",
        group_names=["Contoso-Claims-Officers"],
    )
    dumped = detail.model_dump()
    assert set(dumped) == {
        "id",
        "display_name",
        "kind",
        "user_principal_name",
        "mail",
        "job_title",
        "group_names",
    }
    assert dumped["id"] == "oid-1"
    assert dumped["display_name"] == "Maria Bauer"
    assert dumped["kind"] == "user"
    assert dumped["user_principal_name"] == "maria.bauer@example.onmicrosoft.com"
    assert dumped["mail"] == "maria.bauer@example.com"
    assert dumped["job_title"] == "Claims Officer"
    assert dumped["group_names"] == ["Contoso-Claims-Officers"]


# ---------------------------------------------------------------------------
# Literal validation: bad type / kind values raise ValidationError
# ---------------------------------------------------------------------------

def test_graph_node_rejects_bad_type_literal():
    with pytest.raises(ValidationError):
        GraphNode(type="robot", id="robot:x", label="X", ref_id="x")


def test_graph_edge_rejects_bad_type_literal():
    with pytest.raises(ValidationError):
        GraphEdge(id="e1", source="a", target="b", type="invokes", role="Invoker")


def test_principal_detail_rejects_bad_kind_literal():
    with pytest.raises(ValidationError):
        PrincipalDetail(id="oid-1", display_name="X", kind="service_principal")


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def test_graph_edge_has_policy_defaults_false():
    edge = GraphEdge(
        id="asg-1",
        source="user:oid-1",
        target="agent:rec-1",
        type="access",
        role="Invoker",
    )
    assert edge.has_policy is False


def test_graph_node_metadata_defaults_empty_dict():
    node = GraphNode(type="user", id="user:oid-1", label="Maria", ref_id="oid-1")
    assert node.metadata == {}


def test_principal_detail_optional_defaults():
    detail = PrincipalDetail(id="gid-1", display_name="Contoso-Claims-Officers", kind="group")
    assert detail.user_principal_name is None
    assert detail.mail is None
    assert detail.job_title is None
    assert detail.group_names == []


# ---------------------------------------------------------------------------
# Mutable-default isolation: two instances must NOT share the same default object
# (guards the default-factory question — Pydantic v2 deep-copies defaults).
# ---------------------------------------------------------------------------

def test_graph_node_metadata_default_not_shared_between_instances():
    a = GraphNode(type="user", id="user:a", label="A", ref_id="a")
    b = GraphNode(type="user", id="user:b", label="B", ref_id="b")
    a.metadata["mutated"] = True
    assert a.metadata == {"mutated": True}
    assert b.metadata == {}  # unaffected
    assert a.metadata is not b.metadata


def test_principal_detail_group_names_default_not_shared_between_instances():
    a = PrincipalDetail(id="a", display_name="A", kind="user")
    b = PrincipalDetail(id="b", display_name="B", kind="user")
    a.group_names.append("grp-1")
    assert a.group_names == ["grp-1"]
    assert b.group_names == []  # unaffected
    assert a.group_names is not b.group_names
