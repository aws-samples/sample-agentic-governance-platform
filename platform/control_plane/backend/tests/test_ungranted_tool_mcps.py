"""Which MCP servers a harness agent lists in ``tools`` but holds no grant for.

A harness whose ``tools`` names a gateway the agent was never granted fails EVERY invoke (the
TOKEN_EXCHANGE to that gateway fails, even for a prompt that uses no tool). AGP can see both
halves: the declared gateways via ``GetHarness`` (``tools[].config.agentCoreGateway.gatewayArn``)
and the granted set (``agent.mcp_server_ids``). No live AWS: the control client is a mock.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from models.mcp_server import Kind
from services import agent_mcp_env

HARNESS = "arn:aws:bedrock-agentcore:us-east-1:acct:harness/fnol_dev-SFM3qBuWSV"
GW_A = "arn:aws:bedrock-agentcore:us-east-1:acct:gateway/agp-claims-api-dev-a"
GW_B = "arn:aws:bedrock-agentcore:us-east-1:acct:gateway/agp-other-dev-b"


def _mcp(mid, arn, tenant="t1", name=None, shared=False):
    return SimpleNamespace(
        id=mid,
        name=name or mid,
        kind=Kind.GATEWAY,
        gateway_arn=arn,
        tenant_id=tenant,
        shared=shared,
    )


def _harness_tools(*arns):
    return {
        "harness": {
            "tools": [
                {"type": "agentcore_gateway", "config": {"agentCoreGateway": {"gatewayArn": a}}} for a in arns
            ]
            + [{"type": "inline_function", "config": {"inlineFunction": {}}}]
        }
    }


def _run(agent, mcps, control):
    registry = MagicMock()
    registry.list.return_value = mcps
    identity = SimpleNamespace(_control=control)
    with (
        patch.object(agent_mcp_env, "_get_mcp_registry", return_value=registry),
        patch.object(agent_mcp_env, "_get_agent_identity_service", return_value=identity),
    ):
        return agent_mcp_env.ungranted_tool_mcps(agent, [HARNESS])


def test_a_declared_gateway_without_a_grant_is_reported():
    control = MagicMock()
    control.get_harness.return_value = _harness_tools(GW_A, GW_B)
    agent = SimpleNamespace(id="a1", tenant_id="t1", mcp_server_ids=["mcp-b"])

    out = _run(agent, [_mcp("mcp-a", GW_A, name="claims-api"), _mcp("mcp-b", GW_B)], control)

    assert [m.id for m in out] == ["mcp-a"]
    control.get_harness.assert_called_once_with(harnessId="fnol_dev-SFM3qBuWSV")


def test_all_declared_gateways_granted_reports_nothing():
    control = MagicMock()
    control.get_harness.return_value = _harness_tools(GW_A)
    agent = SimpleNamespace(id="a1", tenant_id="t1", mcp_server_ids=["mcp-a"])
    assert _run(agent, [_mcp("mcp-a", GW_A)], control) == []


def test_an_ungoverned_or_foreign_tenant_gateway_is_not_ours_to_report():
    control = MagicMock()
    control.get_harness.return_value = _harness_tools(GW_A, GW_B)
    agent = SimpleNamespace(id="a1", tenant_id="t1", mcp_server_ids=[])
    assert _run(agent, [_mcp("mcp-b", GW_B, tenant="t2")], control) == []


def test_an_unreadable_harness_reports_nothing_and_does_not_raise():
    control = MagicMock()
    control.get_harness.side_effect = RuntimeError("AccessDenied")
    agent = SimpleNamespace(id="a1", tenant_id="t1", mcp_server_ids=[])
    assert _run(agent, [_mcp("mcp-a", GW_A)], control) == []


def test_a_runtime_arn_is_not_read():
    control = MagicMock()
    registry = MagicMock()
    with (
        patch.object(agent_mcp_env, "_get_mcp_registry", return_value=registry),
        patch.object(
            agent_mcp_env,
            "_get_agent_identity_service",
            return_value=SimpleNamespace(_control=control),
        ),
    ):
        out = agent_mcp_env.ungranted_tool_mcps(
            SimpleNamespace(id="a1", tenant_id="t1", mcp_server_ids=[]),
            ["arn:aws:bedrock-agentcore:us-east-1:acct:runtime/agent-abc"],
        )
    assert out == []
    control.get_harness.assert_not_called()


def test_a_shared_gateway_from_another_tenant_is_reported():
    control = MagicMock()
    control.get_harness.return_value = _harness_tools(GW_A)
    agent = SimpleNamespace(id="a1", tenant_id="t1", mcp_server_ids=[])
    out = _run(agent, [_mcp("mcp-a", GW_A, tenant="platform", shared=True)], control)
    assert [m.id for m in out] == ["mcp-a"]


def test_an_mcp_registry_error_reports_nothing_and_does_not_raise():
    """On the invoke-failure path a raise here would turn the mapped 502 into a 500."""
    control = MagicMock()
    control.get_harness.return_value = _harness_tools(GW_A)
    registry = MagicMock()
    registry.list.side_effect = RuntimeError("registry down")
    with (
        patch.object(agent_mcp_env, "_get_mcp_registry", return_value=registry),
        patch.object(
            agent_mcp_env,
            "_get_agent_identity_service",
            return_value=SimpleNamespace(_control=control),
        ),
    ):
        out = agent_mcp_env.ungranted_tool_mcps(
            SimpleNamespace(id="a1", tenant_id="t1", mcp_server_ids=[]), [HARNESS]
        )
    assert out == []
