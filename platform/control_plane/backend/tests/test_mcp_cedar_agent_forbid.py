"""Tests for ``McpCedarService.add_agent_forbid`` , the containment agent-scoped forbid.

The forbid is only written to a gateway that is ALREADY enforcing Cedar. Attaching an engine
to a mode-``none`` gateway would flip it to ENFORCE and default-deny every caller, so the
method must never ensure/attach an engine or change the mode: on ``none``/``log_only`` it
returns ``skipped`` and makes NO control-plane call at all.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, WaiterError

from models.mcp_server import CedarEnforcementMode, Kind, LifecycleState, McpServer
from services.cedar_policy_text import build_agent_forbid, build_cedar_policy
from services.mcp_cedar_service import McpCedarError, McpCedarService

GATEWAY_ARN = "arn:aws:bedrock-agentcore:us-east-1:acct:gateway/gw-test"
ENGINE_ID = "pe-1"
APP_ID = "11111111-2222-3333-4444-555555555555"


def _make_mcp(*, mode: CedarEnforcementMode, engine_id: str | None = ENGINE_ID) -> McpServer:
    now = datetime.now(timezone.utc)
    return McpServer(
        id="mcp-rec-1",
        name="Claims MCP",
        kind=Kind.GATEWAY,
        gateway_arn=GATEWAY_ARN,
        gateway_id="gw-test",
        lifecycle_state=LifecycleState.APPROVED,
        cedar_policy_engine_id=engine_id,
        cedar_enforcement_mode=mode,
        created_at=now,
        updated_at=now,
    )


def _service() -> tuple[McpCedarService, MagicMock, MagicMock]:
    control = MagicMock(name="bedrock-agentcore-control")
    control.create_policy.return_value = {"policyId": "pol-c1", "status": "ACTIVE"}
    registry = MagicMock(name="registry")
    return McpCedarService(registry=registry, control_client=control), control, registry


@pytest.mark.asyncio
async def test_enforce_creates_the_forbid_on_the_record_engine():
    svc, control, registry = _service()
    mcp = _make_mcp(mode="enforce")

    out = await svc.add_agent_forbid(mcp, agent_app_id=APP_ID, agent_label="Fraud triage")

    assert out == {"status": "ok", "policy_id": "pol-c1"}
    control.create_policy.assert_called_once()
    kwargs = control.create_policy.call_args.kwargs
    assert kwargs["policyEngineId"] == ENGINE_ID
    assert kwargs["validationMode"] == "IGNORE_ALL_FINDINGS"
    assert kwargs["definition"] == {
        "cedar": {
            "statement": build_agent_forbid(
                agent_app_id=APP_ID, agent_label="Fraud triage", gateway_arn=GATEWAY_ARN
            )
        }
    }
    control.create_policy_engine.assert_not_called()
    control.update_gateway.assert_not_called()
    control.get_gateway.assert_not_called()
    registry.persist_identity.assert_not_called()
    assert mcp.cedar_enforcement_mode == "enforce"


@pytest.mark.asyncio
async def test_enforce_calls_create_policy_with_engine_and_text(monkeypatch):
    svc, _control, _registry = _service()
    seen: list[tuple[str, str, str | None]] = []

    def fake_create(engine_id, statement, *, validation_mode=None):
        seen.append((engine_id, statement, validation_mode))
        return {"policyId": "pol-x"}

    monkeypatch.setattr(svc, "_create_policy", fake_create)
    out = await svc.add_agent_forbid(_make_mcp(mode="enforce"), agent_app_id=APP_ID, agent_label="A")

    assert out == {"status": "ok", "policy_id": "pol-x"}
    assert seen == [
        (
            ENGINE_ID,
            build_agent_forbid(agent_app_id=APP_ID, agent_label="A", gateway_arn=GATEWAY_ARN),
            "IGNORE_ALL_FINDINGS",
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["none", "log_only"])
async def test_not_enforcing_is_skipped_with_no_control_call(mode):
    svc, control, registry = _service()
    mcp = _make_mcp(mode=mode)

    out = await svc.add_agent_forbid(mcp, agent_app_id=APP_ID, agent_label="A")

    assert out == {"status": "skipped", "detail": "gateway not enforcing Cedar"}
    assert control.method_calls == []
    assert registry.method_calls == []
    assert mcp.cedar_enforcement_mode == mode


@pytest.mark.asyncio
async def test_enforce_without_engine_id_is_skipped_with_no_control_call():
    svc, control, _registry = _service()

    out = await svc.add_agent_forbid(
        _make_mcp(mode="enforce", engine_id=None), agent_app_id=APP_ID, agent_label="A"
    )

    assert out == {"status": "skipped", "detail": "gateway not enforcing Cedar"}
    assert control.method_calls == []


@pytest.mark.asyncio
async def test_bad_app_id_raises_safe_error_before_any_call():
    svc, control, _registry = _service()

    with pytest.raises(McpCedarError):
        await svc.add_agent_forbid(_make_mcp(mode="enforce"), agent_app_id='x" || true', agent_label="A")
    assert control.method_calls == []


def _with_policy(control: MagicMock, statement: str) -> None:
    control.list_policy_summaries.return_value = {
        "policies": [{"policyId": "pol-c1"}],
        "nextToken": None,
    }
    control.get_policy.return_value = {"definition": {"cedar": {"statement": statement}}}


@pytest.mark.asyncio
async def test_list_policies_carries_kind_and_agent_app_id():
    svc, control, _registry = _service()
    _with_policy(
        control,
        build_agent_forbid(agent_app_id=APP_ID, agent_label="A", gateway_arn=GATEWAY_ARN),
    )

    row = (await svc.list_policies(_make_mcp(mode="enforce")))["policies"][0]

    assert (row["kind"], row["agent_app_id"]) == ("contain", APP_ID)


@pytest.mark.asyncio
async def test_list_policies_foreign_row_has_null_kind():
    svc, control, _registry = _service()
    _with_policy(control, "forbid(principal, action, resource);\n")

    row = (await svc.list_policies(_make_mcp(mode="enforce")))["policies"][0]

    assert (row["kind"], row["agent_app_id"]) == (None, None)


@pytest.mark.asyncio
async def test_policy_kind_reads_contain_and_user_rows():
    svc, control, _registry = _service()
    mcp = _make_mcp(mode="enforce")
    _with_policy(
        control,
        build_agent_forbid(agent_app_id=APP_ID, agent_label="A", gateway_arn=GATEWAY_ARN),
    )
    assert await svc.policy_kind(mcp, "pol-c1") == "contain"
    control.get_policy.assert_called_once_with(policyEngineId=ENGINE_ID, policyId="pol-c1")

    _with_policy(
        control,
        build_cedar_policy(principal_oid="u1", principal_label="x", gateway_arn=GATEWAY_ARN, tool_name=None),
    )
    assert await svc.policy_kind(mcp, "pol-1") is None


@pytest.mark.asyncio
async def test_policy_kind_none_on_lookup_error_or_no_engine():
    svc, control, _registry = _service()
    control.get_policy.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}}, "GetPolicy"
    )
    assert await svc.policy_kind(_make_mcp(mode="enforce"), "gone") is None
    assert await svc.policy_kind(_make_mcp(mode="enforce", engine_id=None), "x") is None


@pytest.mark.asyncio
async def test_create_failed_raises_not_hangs():
    """policy_active's waiter fails on CREATE_FAILED → a safe McpCedarError (containment records
    the step as failed)."""
    svc, control, _registry = _service()
    control.get_waiter.return_value.wait.side_effect = WaiterError(
        name="PolicyActive",
        reason="Waiter encountered a terminal failure state",
        last_response={"status": "CREATE_FAILED"},
    )

    with pytest.raises(McpCedarError, match="agent forbid create failed"):
        await svc.add_agent_forbid(_make_mcp(mode="enforce"), agent_app_id=APP_ID, agent_label="A")


@pytest.mark.asyncio
async def test_create_policy_client_error_raises():
    svc, control, _registry = _service()
    control.create_policy.side_effect = ClientError(
        {"Error": {"Code": "ValidationException"}}, "CreatePolicy"
    )

    with pytest.raises(McpCedarError):
        await svc.add_agent_forbid(_make_mcp(mode="enforce"), agent_app_id=APP_ID, agent_label="A")


def test_create_policy_default_sends_no_validation_mode():
    """add_policy's path (no validation_mode) keeps the default service validation."""
    svc, control, _registry = _service()
    svc._create_policy(ENGINE_ID, "stmt")
    assert "validationMode" not in control.create_policy.call_args.kwargs
