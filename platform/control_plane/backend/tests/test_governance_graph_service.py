"""GovernanceGraphService aggregation tests (Epic 11, Task 4).

The service composes the registry lists (``agent_service.list()`` /
``mcp_service.list()`` — sync boto3) + the async ``GraphService`` reads
(``list_assignments`` / ``list_agent_mcp_grants`` / ``get_principal`` /
``list_member_group_ids``) into a ``GovernanceGraph`` read-DTO. It is READ-ONLY:
it must NEVER call a write/provisioning method, NEVER ``list_policies`` /
``add_policy`` / any Cedar mutation. ``has_policy`` is derived ONLY from the
already-listed ``McpServer.cedar_enforcement_mode`` field.

These tests inject ``MagicMock`` registry services (``.list()`` returns REAL
``Agent`` / ``McpServer`` instances so the enum→string metadata coercion is
exercised) + an ``AsyncMock`` graph_service (so NO live Graph is touched). The
repo is NOT in pytest-asyncio ``auto`` mode (no config), so every async test is
decorated ``@pytest.mark.asyncio`` explicitly (same as ``test_graph_service.py``
/ ``test_agent_mcp_grant.py``).
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from models.agent import Agent, LifecycleState, Origin, Platform
from models.mcp_server import Kind
from models.mcp_server import LifecycleState as McpLifecycleState
from models.mcp_server import McpServer

_NOW = datetime.now(timezone.utc)


# --- builders (real model instances so enum .value coercion is exercised) ----

def _agent(
    *,
    id="agent-1",
    name="claims-triage-de",
    provisioned=True,
    entra_sp_id="agent-sp-1",
    invoker_role_id="agent-inv-guid",
    admin_role_id="agent-adm-guid",
    tenant_id=None,
) -> Agent:
    return Agent(
        id=id,
        name=name,
        lifecycle_state=LifecycleState.APPROVED,
        platform=Platform.AWS_BEDROCK,
        origin=Origin.REGISTERED,
        identity_status="provisioned" if provisioned else "none",
        entra_sp_id=entra_sp_id if provisioned else None,
        invoker_role_id=invoker_role_id,
        admin_role_id=admin_role_id,
        tenant_id=tenant_id,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _mcp(
    *,
    id="mcp-1",
    name="internal-claims-mcp",
    entra_sp_id="mcp-sp-1",
    invoker_role_id="mcp-inv-guid",
    admin_role_id="mcp-adm-guid",
    cedar_enforcement_mode="enforce",
    tenant_id=None,
    published=False,
    shared=False,
) -> McpServer:
    return McpServer(
        id=id,
        name=name,
        lifecycle_state=McpLifecycleState.APPROVED,
        kind=Kind.GATEWAY,
        entra_sp_id=entra_sp_id,
        invoker_role_id=invoker_role_id,
        admin_role_id=admin_role_id,
        cedar_enforcement_mode=cedar_enforcement_mode,
        identity_status="provisioned",
        tenant_id=tenant_id,
        published=published,
        shared=shared,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _make_service(*, agents, mcps, graph):
    """Wire MagicMock registry services + an AsyncMock graph_service."""
    from services.governance_graph_service import GovernanceGraphService

    agent_service = MagicMock()
    agent_service.list = MagicMock(return_value=agents)
    mcp_service = MagicMock()
    mcp_service.list = MagicMock(return_value=mcps)
    return GovernanceGraphService(
        agent_service=agent_service,
        mcp_service=mcp_service,
        graph_service=graph,
    )


def _empty_graph():
    """AsyncMock graph_service whose reads return nothing (no edges)."""
    graph = AsyncMock()
    graph.list_assignments = AsyncMock(return_value=[])
    graph.list_agent_mcp_grants = AsyncMock(return_value=[])
    return graph


def _node_by_id(graph, node_id):
    for n in graph.nodes:
        if n.id == node_id:
            return n
    return None


def _edges_by_type(graph, edge_type):
    return [e for e in graph.edges if e.type == edge_type]


# --- node assembly -----------------------------------------------------------

@pytest.mark.asyncio
async def test_build_node_assembly_includes_all_entities_and_skips_unprovisioned():
    """2 agents (1 provisioned, 1 not) + 2 MCPs → 4 entity nodes with prefixed
    ids + metadata; the UNPROVISIONED agent's sp is NEVER passed to Graph."""
    prov = _agent(id="a-prov", name="prov-agent", provisioned=True, entra_sp_id="sp-prov")
    unprov = _agent(id="a-unprov", name="unprov-agent", provisioned=False)
    m1 = _mcp(id="m-1", name="mcp-one")
    m2 = _mcp(id="m-2", name="mcp-two", entra_sp_id="mcp-sp-2", cedar_enforcement_mode="none")
    graph = _empty_graph()

    svc = _make_service(agents=[prov, unprov], mcps=[m1, m2], graph=graph)
    out = await svc.build()

    # 4 entity nodes (no principals — empty assignments)
    assert _node_by_id(out, "agent:a-prov") is not None
    assert _node_by_id(out, "agent:a-unprov") is not None
    assert _node_by_id(out, "mcp:m-1") is not None
    assert _node_by_id(out, "mcp:m-2") is not None
    assert len([n for n in out.nodes if n.type in ("agent", "mcp")]) == 4

    # ref_id is the bare id; metadata enums coerced to plain strings
    agent_node = _node_by_id(out, "agent:a-prov")
    assert agent_node.ref_id == "a-prov"
    assert agent_node.label == "prov-agent"
    assert agent_node.metadata == {
        "origin": "Registered",
        "lifecycle_state": "approved",
        "identity_status": "provisioned",
        "platform": "aws_bedrock",
    }
    mcp_node = _node_by_id(out, "mcp:m-1")
    assert mcp_node.ref_id == "m-1"
    assert mcp_node.metadata == {
        "kind": "gateway",
        "cedar_enforcement_mode": "enforce",
        "identity_status": "provisioned",
    }

    # Provisioned agent's sp WAS read; the unprovisioned agent's sp NEVER was.
    called_sps = {c.args[0] for c in graph.list_assignments.call_args_list}
    assert "sp-prov" in called_sps
    assert "a-unprov" not in called_sps
    assert None not in called_sps
    # the unprovisioned agent has no sp at all → exactly one provisioned agent read
    assert graph.list_assignments.await_count == 1
    assert graph.list_agent_mcp_grants.await_count == 1


# --- user/group edges + dedup across agents ----------------------------------

@pytest.mark.asyncio
async def test_build_user_group_dedup_two_agents_one_user_node_two_edges():
    """A User + a Group on agent A and the SAME user on agent B →
    ONE user node, ONE group node, TWO access edges; edge id = assignment id;
    ServicePrincipal principals are skipped (agent→agent out of scope)."""
    a = _agent(id="a-A", entra_sp_id="sp-A", invoker_role_id="A-inv", admin_role_id="A-adm")
    b = _agent(id="a-B", entra_sp_id="sp-B", invoker_role_id="B-inv", admin_role_id="B-adm")
    graph = _empty_graph()

    async def assignments(sp):
        if sp == "sp-A":
            return [
                {
                    "id": "assign-1",
                    "principalId": "user-oid",
                    "principalDisplayName": "Maria Bauer",
                    "principalType": "User",
                    "appRoleId": "A-inv",
                },
                {
                    "id": "assign-2",
                    "principalId": "group-oid",
                    "principalDisplayName": "Contoso-Claims-Officers",
                    "principalType": "Group",
                    "appRoleId": "A-adm",
                },
                {
                    "id": "assign-sp",
                    "principalId": "other-agent-sp",
                    "principalDisplayName": "peer-agent",
                    "principalType": "ServicePrincipal",
                    "appRoleId": "A-inv",
                },
            ]
        if sp == "sp-B":
            return [
                {
                    "id": "assign-3",
                    "principalId": "user-oid",
                    "principalDisplayName": "Maria Bauer",
                    "principalType": "User",
                    "appRoleId": "B-inv",
                }
            ]
        return []

    graph.list_assignments.side_effect = assignments

    svc = _make_service(agents=[a, b], mcps=[], graph=graph)
    out = await svc.build()

    # ONE user node, ONE group node (deduped by principalId across agents)
    user_nodes = [n for n in out.nodes if n.type == "user"]
    group_nodes = [n for n in out.nodes if n.type == "group"]
    assert len(user_nodes) == 1
    assert len(group_nodes) == 1
    assert user_nodes[0].id == "user:user-oid"
    assert user_nodes[0].ref_id == "user-oid"
    assert user_nodes[0].label == "Maria Bauer"
    assert user_nodes[0].metadata == {"principal_type": "User"}
    assert group_nodes[0].id == "group:group-oid"
    assert group_nodes[0].metadata == {"principal_type": "Group"}

    access = _edges_by_type(out, "access")
    # user→A, group→A, user→B  (the ServicePrincipal assignment is skipped)
    assert len(access) == 3
    by_id = {e.id: e for e in access}
    assert by_id["assign-1"].source == "user:user-oid"
    assert by_id["assign-1"].target == "agent:a-A"
    assert by_id["assign-1"].role == "Invoker"
    assert by_id["assign-2"].source == "group:group-oid"
    assert by_id["assign-2"].target == "agent:a-A"
    assert by_id["assign-2"].role == "Admin"
    assert by_id["assign-3"].source == "user:user-oid"
    assert by_id["assign-3"].target == "agent:a-B"
    assert by_id["assign-3"].role == "Invoker"
    # ServicePrincipal principal produced NO edge and NO node
    assert "assign-sp" not in by_id
    assert not any("other-agent-sp" in n.id for n in out.nodes)


# --- role mapping ------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_role_mapping_invoker_admin_unknown():
    """appRoleId matching invoker/admin → Invoker/Admin; anything else → Unknown."""
    a = _agent(id="a-1", entra_sp_id="sp-1", invoker_role_id="inv-guid", admin_role_id="adm-guid")
    graph = _empty_graph()
    graph.list_assignments.side_effect = lambda sp: [
        {"id": "e-inv", "principalId": "u1", "principalDisplayName": "U1",
         "principalType": "User", "appRoleId": "inv-guid"},
        {"id": "e-adm", "principalId": "u2", "principalDisplayName": "U2",
         "principalType": "User", "appRoleId": "adm-guid"},
        {"id": "e-unk", "principalId": "u3", "principalDisplayName": "U3",
         "principalType": "User", "appRoleId": "legacy-guid"},
    ]

    svc = _make_service(agents=[a], mcps=[], graph=graph)
    out = await svc.build()

    roles = {e.id: e.role for e in _edges_by_type(out, "access")}
    assert roles == {"e-inv": "Invoker", "e-adm": "Admin", "e-unk": "Unknown"}


# --- agent→mcp reverse-join filter -------------------------------------------

@pytest.mark.asyncio
async def test_build_agent_mcp_reverse_join_drops_unknown_resource():
    """One grant to a known MCP sp → a can_call edge; one grant to an unknown
    resource (e.g. Microsoft Graph) → DROPPED."""
    a = _agent(id="a-1", entra_sp_id="sp-1", invoker_role_id="x", admin_role_id="y")
    m = _mcp(id="m-1", entra_sp_id="mcp-sp-1", invoker_role_id="mcp-inv", admin_role_id="mcp-adm")
    graph = _empty_graph()
    graph.list_agent_mcp_grants.side_effect = lambda sp: [
        {"id": "grant-known", "resourceId": "mcp-sp-1",
         "resourceDisplayName": "internal-claims-mcp", "appRoleId": "mcp-inv"},
        {"id": "grant-graph", "resourceId": "graph-sp-id",
         "resourceDisplayName": "Microsoft Graph", "appRoleId": "some-graph-role"},
    ]

    svc = _make_service(agents=[a], mcps=[m], graph=graph)
    out = await svc.build()

    can_call = _edges_by_type(out, "can_call")
    assert len(can_call) == 1
    edge = can_call[0]
    assert edge.id == "grant-known"
    assert edge.source == "agent:a-1"
    assert edge.target == "mcp:m-1"
    assert edge.role == "Invoker"


# --- has_policy (no Cedar/AWS call) ------------------------------------------

@pytest.mark.asyncio
async def test_build_has_policy_from_enforcement_mode_no_cedar_call():
    """has_policy True for an enforce MCP, False for a none MCP; NO list_policies /
    extra AWS call is made (has_policy comes only from the listed field)."""
    a = _agent(id="a-1", entra_sp_id="sp-1", invoker_role_id="x", admin_role_id="y")
    m_enforce = _mcp(id="m-enf", entra_sp_id="sp-enf", invoker_role_id="enf-inv",
                     admin_role_id="enf-adm", cedar_enforcement_mode="enforce")
    m_none = _mcp(id="m-none", entra_sp_id="sp-none", invoker_role_id="none-inv",
                  admin_role_id="none-adm", cedar_enforcement_mode="none")
    graph = _empty_graph()
    graph.list_agent_mcp_grants.side_effect = lambda sp: [
        {"id": "g-enf", "resourceId": "sp-enf", "resourceDisplayName": "enf",
         "appRoleId": "enf-inv"},
        {"id": "g-none", "resourceId": "sp-none", "resourceDisplayName": "none",
         "appRoleId": "none-inv"},
    ]

    svc = _make_service(agents=[a], mcps=[m_enforce, m_none], graph=graph)
    out = await svc.build()

    policy = {e.id: e.has_policy for e in _edges_by_type(out, "can_call")}
    assert policy == {"g-enf": True, "g-none": False}

    # No Cedar / extra AWS call: the graph_service AsyncMock has NO list_policies
    # attribute access, and the registry services were only ``.list()``-ed.
    assert not hasattr(graph, "list_policies") or not graph.list_policies.called
    # the graph_service was only used for the read methods we expect
    assert graph.list_assignments.await_count == 1
    assert graph.list_agent_mcp_grants.await_count == 1


@pytest.mark.asyncio
async def test_build_does_not_call_any_cedar_or_write_method():
    """Defensive: build() touches ONLY the read methods. Assert that no
    write/provisioning/Cedar attribute on the graph mock was ever accessed-as-called."""
    a = _agent(id="a-1", entra_sp_id="sp-1", invoker_role_id="x", admin_role_id="y")
    m = _mcp(id="m-1", entra_sp_id="sp-1-mcp")
    graph = _empty_graph()

    svc = _make_service(agents=[a], mcps=[m], graph=graph)
    await svc.build()

    forbidden = [
        "assign_app_role", "revoke_app_role", "grant_agent_obo_consent",
        "create_agent_app", "set_assignment_required", "add_agent_password",
        "list_policies", "add_policy", "delete_policy", "set_enforcement",
    ]
    for name in forbidden:
        attr = getattr(graph, name, None)
        # An AsyncMock auto-creates attributes on access; assert none was CALLED.
        assert attr is None or not getattr(attr, "called", False), f"{name} was called"


# --- per-agent Graph error tolerance -----------------------------------------

@pytest.mark.asyncio
async def test_build_per_agent_graph_error_is_tolerated():
    """One agent's list_assignments raises → that agent yields NO edges, the rest
    of the graph still builds, and NO exception escapes build()."""
    good = _agent(id="a-good", entra_sp_id="sp-good", invoker_role_id="g-inv", admin_role_id="g-adm")
    bad = _agent(id="a-bad", entra_sp_id="sp-bad", invoker_role_id="b-inv", admin_role_id="b-adm")
    m = _mcp(id="m-1", entra_sp_id="mcp-sp-1", invoker_role_id="mcp-inv", admin_role_id="mcp-adm")
    graph = _empty_graph()

    async def assignments(sp):
        if sp == "sp-bad":
            raise RuntimeError("Graph blew up for this agent")
        if sp == "sp-good":
            return [{"id": "ok-1", "principalId": "u-1", "principalDisplayName": "U1",
                     "principalType": "User", "appRoleId": "g-inv"}]
        return []

    async def grants(sp):
        if sp == "sp-good":
            return [{"id": "cc-1", "resourceId": "mcp-sp-1",
                     "resourceDisplayName": "m", "appRoleId": "mcp-inv"}]
        return []

    graph.list_assignments.side_effect = assignments
    graph.list_agent_mcp_grants.side_effect = grants

    svc = _make_service(agents=[good, bad], mcps=[m], graph=graph)
    out = await svc.build()  # must NOT raise

    # both agents are nodes
    assert _node_by_id(out, "agent:a-good") is not None
    assert _node_by_id(out, "agent:a-bad") is not None
    # good agent's edges present
    access = _edges_by_type(out, "access")
    can_call = _edges_by_type(out, "can_call")
    assert [e.id for e in access] == ["ok-1"]
    assert [e.id for e in can_call] == ["cc-1"]
    assert access[0].target == "agent:a-good"
    # the bad agent contributed NO edges
    assert all(e.target != "agent:a-bad" for e in out.edges)
    assert all(e.source != "agent:a-bad" for e in out.edges)


# --- E24/T8 — tenant-scoped induced subgraph ----------------------------------

def _ctx(*, is_global=False, tenant_ids=()):
    from services.tenant_resolver import TenantContext

    return TenantContext(is_global=is_global, tenant_ids=frozenset(tenant_ids), tenants=())


def _tenant_fixture():
    """2 tenants: ten-1 owns agent a-1 + MCP m-1; ten-2 owns agent a-2 + MCP m-2;
    m-shared (ten-2, shared=True) and m-pub (ten-2, published=True, NOT shared).
    Graph wiring: a-1 → m-1 + m-2 + m-shared; a-2 → m-2. One user on each agent."""
    a1 = _agent(id="a-1", entra_sp_id="sp-a1", invoker_role_id="a1-inv",
                admin_role_id="a1-adm", tenant_id="ten-1")
    a2 = _agent(id="a-2", entra_sp_id="sp-a2", invoker_role_id="a2-inv",
                admin_role_id="a2-adm", tenant_id="ten-2")
    m1 = _mcp(id="m-1", entra_sp_id="msp-1", invoker_role_id="m1-inv",
              admin_role_id="m1-adm", tenant_id="ten-1")
    m2 = _mcp(id="m-2", entra_sp_id="msp-2", invoker_role_id="m2-inv",
              admin_role_id="m2-adm", tenant_id="ten-2")
    m_shared = _mcp(id="m-shared", entra_sp_id="msp-shared", invoker_role_id="ms-inv",
                    admin_role_id="ms-adm", tenant_id="ten-2", shared=True)
    m_pub = _mcp(id="m-pub", entra_sp_id="msp-pub", invoker_role_id="mp-inv",
                 admin_role_id="mp-adm", tenant_id="ten-2", published=True)
    graph = _empty_graph()

    async def assignments(sp):
        if sp == "sp-a1":
            return [{"id": "acc-1", "principalId": "u-1", "principalDisplayName": "U1",
                     "principalType": "User", "appRoleId": "a1-inv"}]
        if sp == "sp-a2":
            return [{"id": "acc-2", "principalId": "u-2", "principalDisplayName": "U2",
                     "principalType": "User", "appRoleId": "a2-inv"}]
        return []

    async def grants(sp):
        if sp == "sp-a1":
            return [
                {"id": "cc-1", "resourceId": "msp-1", "appRoleId": "m1-inv"},
                {"id": "cc-2", "resourceId": "msp-2", "appRoleId": "m2-inv"},
                {"id": "cc-shared", "resourceId": "msp-shared", "appRoleId": "ms-inv"},
            ]
        if sp == "sp-a2":
            return [{"id": "cc-3", "resourceId": "msp-2", "appRoleId": "m2-inv"}]
        return []

    graph.list_assignments.side_effect = assignments
    graph.list_agent_mcp_grants.side_effect = grants
    return [a1, a2], [m1, m2, m_shared, m_pub], graph


@pytest.mark.asyncio
async def test_build_scoped_caller_gets_induced_subgraph_no_dangling_edges():
    """A one-tenant (ten-1) caller sees ONLY: own agent a-1, own MCP m-1, the
    shared MCP m-shared, u-1's access edge, and a-1's can_call edges to m-1 +
    m-shared. Everything of ten-2 (a-2, m-2, u-2, cc-2/cc-3/acc-2) is absent —
    including the DANGLING edge cc-2 (a-1 → foreign m-2). `published` grants
    marketplace discovery only, NOT graph visibility: m-pub is absent too."""
    agents, mcps, graph = _tenant_fixture()
    svc = _make_service(agents=agents, mcps=mcps, graph=graph)

    out = await svc.build(ctx=_ctx(tenant_ids=["ten-1"]))

    node_ids = {n.id for n in out.nodes}
    assert node_ids == {"agent:a-1", "mcp:m-1", "mcp:m-shared", "user:u-1"}
    edge_ids = {e.id for e in out.edges}
    assert edge_ids == {"acc-1", "cc-1", "cc-shared"}
    # every edge endpoint is an included node (induced subgraph — no dangling)
    for e in out.edges:
        assert e.source in node_ids
        assert e.target in node_ids
    # the foreign agent's Graph reads were never fanned out
    called_sps = {c.args[0] for c in graph.list_assignments.call_args_list}
    assert called_sps == {"sp-a1"}


@pytest.mark.asyncio
async def test_build_admin_graph_unchanged():
    """is_global ctx (and ctx=None) → the FULL graph: all nodes, all edges."""
    agents, mcps, graph = _tenant_fixture()
    svc = _make_service(agents=agents, mcps=mcps, graph=graph)

    out = await svc.build(ctx=_ctx(is_global=True))

    node_ids = {n.id for n in out.nodes}
    assert node_ids == {
        "agent:a-1", "agent:a-2", "mcp:m-1", "mcp:m-2", "mcp:m-shared",
        "mcp:m-pub", "user:u-1", "user:u-2",
    }
    assert {e.id for e in out.edges} == {"acc-1", "acc-2", "cc-1", "cc-2", "cc-shared", "cc-3"}


@pytest.mark.asyncio
async def test_build_no_ctx_is_full_graph():
    """ctx=None (default) keeps the pre-E24 behavior — full graph."""
    agents, mcps, graph = _tenant_fixture()
    svc = _make_service(agents=agents, mcps=mcps, graph=graph)

    out = await svc.build()

    assert {n.id for n in out.nodes} >= {"agent:a-1", "agent:a-2", "mcp:m-2", "mcp:m-pub"}
    assert {e.id for e in out.edges} == {"acc-1", "acc-2", "cc-1", "cc-2", "cc-shared", "cc-3"}


@pytest.mark.asyncio
async def test_build_scoped_caller_with_no_tenants_gets_empty_graph():
    """A non-global caller with NO memberships sees only shared MCPs (here: the
    shared node, isolated — its owning agent is foreign)."""
    agents, mcps, graph = _tenant_fixture()
    svc = _make_service(agents=agents, mcps=mcps, graph=graph)

    out = await svc.build(ctx=_ctx(tenant_ids=[]))

    assert {n.id for n in out.nodes} == {"mcp:m-shared"}
    assert out.edges == []


# --- get_principal -----------------------------------------------------------

@pytest.mark.asyncio
async def test_get_principal_user_resolves_group_names_skipping_failures():
    """user kind → PrincipalDetail mapped from the raw dict; group_names resolved
    via list_member_group_ids + per-id get_principal(group); a failing per-group
    resolve is skipped, not fatal."""
    graph = AsyncMock()

    async def get_principal(oid, kind):
        if oid == "user-oid" and kind == "user":
            return {
                "id": "user-oid",
                "displayName": "Maria Bauer",
                "userPrincipalName": "maria.bauer@example.onmicrosoft.com",
                "mail": "maria.bauer@example.com",
                "jobTitle": "Claims Officer",
            }
        if oid == "g-ok" and kind == "group":
            return {"id": "g-ok", "displayName": "Contoso-Claims-Officers"}
        if oid == "g-bad" and kind == "group":
            raise RuntimeError("group resolve failed")
        raise AssertionError(f"unexpected get_principal({oid!r}, {kind!r})")

    graph.get_principal.side_effect = get_principal
    graph.list_member_group_ids = AsyncMock(return_value=["g-ok", "g-bad"])

    svc = _make_service(agents=[], mcps=[], graph=graph)
    detail = await svc.get_principal("user-oid", "user")

    assert detail.id == "user-oid"
    assert detail.display_name == "Maria Bauer"
    assert detail.kind == "user"
    assert detail.user_principal_name == "maria.bauer@example.onmicrosoft.com"
    assert detail.mail == "maria.bauer@example.com"
    assert detail.job_title == "Claims Officer"
    # g-ok resolved; g-bad failure skipped (not fatal)
    assert detail.group_names == ["Contoso-Claims-Officers"]


@pytest.mark.asyncio
async def test_get_principal_group_kind_no_group_names():
    """group kind → group_names == [] (no membership resolve for groups)."""
    graph = AsyncMock()
    graph.get_principal = AsyncMock(
        return_value={"id": "grp-oid", "displayName": "Contoso-Claims-Officers",
                      "mail": "claims@example.com"}
    )
    graph.list_member_group_ids = AsyncMock(return_value=["should-not-be-used"])

    svc = _make_service(agents=[], mcps=[], graph=graph)
    detail = await svc.get_principal("grp-oid", "group")

    assert detail.kind == "group"
    assert detail.display_name == "Contoso-Claims-Officers"
    assert detail.mail == "claims@example.com"
    assert detail.user_principal_name is None
    assert detail.job_title is None
    assert detail.group_names == []
    graph.list_member_group_ids.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_principal_bad_kind_raises_value_error():
    """A kind other than user/group → ValueError (the primary call is never made)."""
    graph = AsyncMock()
    graph.get_principal = AsyncMock(return_value={})
    svc = _make_service(agents=[], mcps=[], graph=graph)

    with pytest.raises(ValueError):
        await svc.get_principal("oid", "servicePrincipal")
    graph.get_principal.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_principal_primary_error_propagates():
    """A GraphError from the PRIMARY get_principal call propagates (the route maps
    404→404 / other→502); it is NOT swallowed like the per-group resolves."""
    graph = AsyncMock()
    graph.get_principal = AsyncMock(side_effect=RuntimeError("graph 404"))
    svc = _make_service(agents=[], mcps=[], graph=graph)

    with pytest.raises(RuntimeError):
        await svc.get_principal("missing-oid", "user")
