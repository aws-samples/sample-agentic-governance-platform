"""Tests for ``services.mcp_cedar_service``, the Cedar Policy Engine orchestrator.

A CLONE of ``test_mcp_identity_service`` in idiom: an injected ``MagicMock``
``bedrock-agentcore-control`` client (NO live AWS), a ``persist_identity``-recording
registry double, the poll ``time.sleep`` patched to a no-op, and the call-ORDER
assertions via a shared ``manager.attach_mock(...)`` mock. The repo is NOT in
pytest-asyncio ``auto`` mode → every async test is decorated ``@pytest.mark.asyncio``.

The load-bearing security property under test is the **authorizer replay** on BOTH
attach (``update_gateway`` with ``policyEngineConfiguration``) and detach (``update_gateway``
WITHOUT it): the inbound Entra ``CUSTOM_JWT`` gate must never be stripped. The
engine/policy waiters are exercised via the injected client's
``get_waiter(...).wait(...)`` no-op MagicMock (assert called, don't poll). ``get_policy``
statements are generated with the REAL ``build_cedar_policy`` so ``parse_cedar_policy``
round-trips back to the friendly row.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from models.mcp_server import CedarEnforcementMode, Kind, LifecycleState, McpServer, McpTool
from services.cedar_policy_text import build_cedar_policy
from services.mcp_cedar_service import McpCedarError, McpCedarService

REGION = "us-east-1"

GATEWAY_ID = "demo-claims-gw-aBcDeFgHiJ"
GATEWAY_ARN = f"arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/{GATEWAY_ID}"
ENGINE_ID = "pe-1"
ENGINE_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:policy-engine/agpcedar_mcp_1_aaaaaaaaaa"

AUTHORIZER_CONFIG = {
    "customJWTAuthorizer": {
        "discoveryUrl": ("https://login.microsoftonline.com/tenant-x/v2.0/.well-known/openid-configuration"),
        "allowedAudience": ["api://agp-mcp-mcp-rec-123", "app-client-guid"],
    }
}


def _make_mcp(
    *,
    mcp_id: str = "mcp-rec-123",
    name: str = "Internal Claims MCP",
    kind: Kind = Kind.GATEWAY,
    gateway_arn: str | None = GATEWAY_ARN,
    gateway_id: str | None = GATEWAY_ID,
    cedar_policy_engine_id: str | None = None,
    cedar_policy_engine_arn: str | None = None,
    cedar_enforcement_mode: CedarEnforcementMode = "none",
) -> McpServer:
    now = datetime.now(timezone.utc)
    return McpServer(
        id=mcp_id,
        name=name,
        kind=kind,
        gateway_arn=gateway_arn,
        gateway_id=gateway_id,
        lifecycle_state=LifecycleState.APPROVED,
        cedar_policy_engine_id=cedar_policy_engine_id,
        cedar_policy_engine_arn=cedar_policy_engine_arn,
        cedar_enforcement_mode=cedar_enforcement_mode,
        created_at=now,
        updated_at=now,
    )


def _make_mcp_with_tools(
    *,
    cedar_policy_engine_id: str | None = ENGINE_ID,
    cedar_policy_engine_arn: str | None = ENGINE_ARN,
    cedar_enforcement_mode: CedarEnforcementMode = "enforce",
) -> McpServer:
    """An mcp whose ``available_tools`` carry typed input schemas — a ``transfer`` tool
    with a numeric ``amount`` param and a ``get_client`` tool with a string ``client_id``
    param. Defaults to an engine-already-attached gateway so the add_policy tests focus on
    the new validation/threading rather than the (untouched) ensure/attach flow."""
    mcp = _make_mcp(
        cedar_policy_engine_id=cedar_policy_engine_id,
        cedar_policy_engine_arn=cedar_policy_engine_arn,
        cedar_enforcement_mode=cedar_enforcement_mode,
    )
    mcp.available_tools = [
        McpTool(
            name="transfer",
            description="Move funds",
            input_schema={"properties": {"amount": {"type": "number"}}},
        ),
        McpTool(
            name="get_client",
            description="Lookup a client",
            input_schema={"properties": {"client_id": {"type": "string"}}},
        ),
    ]
    return mcp


def _cedar_control_client(
    *,
    gateway_statuses: list[str] | None = None,
    summaries_pages: list[dict] | None = None,
    get_policy_statement: str | None = None,
) -> MagicMock:
    """A MagicMock ``bedrock-agentcore-control`` client for the Cedar path.

    Clones ``test_mcp_identity_service._gateway_control_client`` (``get_gateway`` returns a
    replayable base dict incl. ``authorizerType``/``authorizerConfiguration`` + a sequenceable
    ``status``; ``update_gateway`` records the call) and EXTENDS it with the Policy Engine
    surface (``create_policy_engine`` / ``create_policy`` / ``list_policy_summaries`` /
    ``get_policy`` / ``delete_policy``) + a ``get_waiter`` returning a no-op MagicMock.
    """
    control = MagicMock(name="bedrock-agentcore-control")
    base = {
        "gatewayId": GATEWAY_ID,
        "name": "demo-claims-gw",
        "roleArn": "arn:aws:iam::123456789012:role/gateway-service-role",
        "protocolType": "MCP",
        "protocolConfiguration": {"mcp": {"searchType": "SEMANTIC"}},
        "gatewayUrl": f"https://{GATEWAY_ID}.example/mcp",
        "authorizerType": "CUSTOM_JWT",
        "authorizerConfiguration": AUTHORIZER_CONFIG,
    }
    if gateway_statuses is None:
        control.get_gateway.return_value = {**base, "status": "READY"}
    else:
        control.get_gateway.side_effect = [{**base, "status": s} for s in gateway_statuses]
    control.update_gateway.return_value = {"status": "UPDATING"}

    control.create_policy_engine.return_value = {
        "policyEngineId": ENGINE_ID,
        "policyEngineArn": ENGINE_ARN,
        "status": "ACTIVE",
    }
    control.get_policy_engine.return_value = {"status": "ACTIVE"}
    control.create_policy.return_value = {
        "policyId": "pol-1",
        "status": "ACTIVE",
        "definition": {"cedar": {"statement": "..."}},
    }
    if summaries_pages is None:
        control.list_policy_summaries.return_value = {
            "policies": [{"policyId": "pol-1", "name": "agppolabc", "status": "ACTIVE"}],
            "nextToken": None,
        }
    else:
        control.list_policy_summaries.side_effect = list(summaries_pages)

    if get_policy_statement is None:
        get_policy_statement = build_cedar_policy(
            principal_oid="eb3da",
            principal_label="lars@example.com",
            gateway_arn=GATEWAY_ARN,
            tool_name="ClaimsTarget___get_claim",
        )
    control.get_policy.return_value = {
        "definition": {"cedar": {"statement": get_policy_statement}},
        "status": "ACTIVE",
    }
    control.delete_policy.return_value = {}

    control.get_waiter.return_value = MagicMock(name="waiter")
    return control


def _registry_double() -> MagicMock:
    """A MagicMock McpServerRegistryService whose ``persist_identity`` returns its arg."""
    registry = MagicMock(name="McpServerRegistryService")
    registry.persist_identity.side_effect = lambda mcp: mcp
    return registry


def _make_service(*, registry: MagicMock, control: MagicMock) -> McpCedarService:
    return McpCedarService(registry=registry, control_client=control, region=REGION)


@pytest.fixture(autouse=True)
def _instant_sleep(monkeypatch):
    """Patch the poll loop's sleep + the conflict-retry sleep so no test blocks."""
    import services.mcp_cedar_service as mod

    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)


@pytest.mark.asyncio
async def test_add_policy_first_time_creates_engine_attaches_enforce_then_creates_policy():
    mcp = _make_mcp(cedar_policy_engine_id=None, cedar_enforcement_mode="none")
    registry = _registry_double()
    control = _cedar_control_client()

    manager = MagicMock()
    manager.attach_mock(control.create_policy_engine, "create_policy_engine")
    manager.attach_mock(control.get_waiter, "get_waiter")
    manager.attach_mock(control.get_gateway, "get_gateway")
    manager.attach_mock(control.update_gateway, "update_gateway")
    manager.attach_mock(control.create_policy, "create_policy")

    svc = _make_service(registry=registry, control=control)
    row = await svc.add_policy(
        mcp,
        principal_oid="eb3da",
        principal_label="lars@example.com",
        tool_name="ClaimsTarget___get_claim",
    )

    order = [c[0] for c in manager.mock_calls]
    assert order.index("create_policy_engine") < order.index("update_gateway")
    assert order.index("update_gateway") < order.index("create_policy")
    assert order.index("get_waiter") < order.index("update_gateway")

    control.get_waiter.assert_any_call("policy_engine_active")

    _, kwargs = control.update_gateway.call_args
    assert kwargs["policyEngineConfiguration"] == {"arn": ENGINE_ARN, "mode": "ENFORCE"}
    assert kwargs["authorizerType"] == "CUSTOM_JWT"
    assert kwargs["authorizerConfiguration"] == AUTHORIZER_CONFIG

    _, cp_kwargs = control.create_policy.call_args
    assert cp_kwargs["policyEngineId"] == ENGINE_ID
    statement = cp_kwargs["definition"]["cedar"]["statement"]
    assert 'AgentCore::Action::"ClaimsTarget___get_claim"' in statement

    control.get_waiter.assert_any_call("policy_active")

    assert row["policy_id"] == "pol-1"
    assert row["tool"] == "ClaimsTarget___get_claim"
    assert row["user_oid"] == "eb3da"
    assert row["effect"] == "allow"
    assert row["cedar_text"] == statement


@pytest.mark.asyncio
async def test_create_policy_engine_client_token_passes_botocore_param_validation():
    """Guard the mock↔live seam: the MagicMock control client never runs botocore's
    real ParamValidator, so a too-short clientToken (uuid4().hex = 32 < AWS min 33)
    passed unit tests but 500'd live. Capture the kwargs our service passes to
    create_policy_engine and validate them against the REAL service input shape."""
    import botocore.session
    from botocore.validate import ParamValidator

    mcp = _make_mcp(cedar_policy_engine_id=None, cedar_enforcement_mode="none")
    registry = _registry_double()
    control = _cedar_control_client()

    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return {
            "policyEngineId": ENGINE_ID,
            "policyEngineArn": ENGINE_ARN,
            "status": "ACTIVE",
        }

    control.create_policy_engine.side_effect = _capture

    svc = _make_service(registry=registry, control=control)
    await svc.add_policy(
        mcp,
        principal_oid="eb3da",
        principal_label="lars@example.com",
        tool_name="ClaimsTarget___get_claim",
    )

    model = botocore.session.get_session().get_service_model("bedrock-agentcore-control")
    shape = model.operation_model("CreatePolicyEngine").input_shape
    errors = ParamValidator().validate(captured, shape)
    assert not errors.has_errors(), errors.generate_report()
    assert len(captured["clientToken"]) >= 33


@pytest.mark.asyncio
async def test_add_policy_replays_authorizer_on_attach():
    mcp = _make_mcp(cedar_policy_engine_id=None, cedar_enforcement_mode="none")
    registry = _registry_double()
    control = _cedar_control_client()
    read = control.get_gateway.return_value

    svc = _make_service(registry=registry, control=control)
    await svc.add_policy(mcp, principal_oid="eb3da", principal_label="lars@x", tool_name=None)

    _, kwargs = control.update_gateway.call_args
    assert kwargs["authorizerType"] == read["authorizerType"]
    assert kwargs["authorizerConfiguration"] == read["authorizerConfiguration"]
    assert kwargs["protocolType"] == read["protocolType"]
    assert kwargs["protocolConfiguration"] == read["protocolConfiguration"]
    assert kwargs["gatewayIdentifier"] == GATEWAY_ID


@pytest.mark.asyncio
async def test_add_policy_engine_exists_skips_create_and_attach():
    mcp = _make_mcp(
        cedar_policy_engine_id=ENGINE_ID,
        cedar_policy_engine_arn=ENGINE_ARN,
        cedar_enforcement_mode="enforce",
    )
    registry = _registry_double()
    control = _cedar_control_client()

    svc = _make_service(registry=registry, control=control)
    await svc.add_policy(
        mcp,
        principal_oid="eb3da",
        principal_label="lars@x",
        tool_name="ClaimsTarget___get_claim",
    )

    control.create_policy_engine.assert_not_called()
    control.update_gateway.assert_not_called()
    control.create_policy.assert_called_once()


@pytest.mark.asyncio
async def test_add_policy_all_tools_generates_action_unconstrained_cedar():
    mcp = _make_mcp(
        cedar_policy_engine_id=ENGINE_ID,
        cedar_policy_engine_arn=ENGINE_ARN,
        cedar_enforcement_mode="enforce",
    )
    registry = _registry_double()
    control = _cedar_control_client()

    svc = _make_service(registry=registry, control=control)
    await svc.add_policy(mcp, principal_oid="eb3da", principal_label="lars@x", tool_name=None)

    _, kwargs = control.create_policy.call_args
    statement = kwargs["definition"]["cedar"]["statement"]
    assert "AgentCore::Action::" not in statement
    assert re.search(r"^\s*action,\s*$", statement, re.MULTILINE)


@pytest.mark.asyncio
async def test_add_policy_engine_name_sanitized():
    mcp = _make_mcp(mcp_id="mcp-123", cedar_policy_engine_id=None, cedar_enforcement_mode="none")
    registry = _registry_double()
    control = _cedar_control_client()

    svc = _make_service(registry=registry, control=control)
    await svc.add_policy(mcp, principal_oid="eb3da", principal_label="lars@x", tool_name="t1")

    _, kwargs = control.create_policy_engine.call_args
    name = kwargs["name"]
    assert re.match(r"^[A-Za-z][A-Za-z0-9_]*$", name), name
    assert "-" not in name


@pytest.mark.asyncio
async def test_add_policy_persists_engine_ids_and_mode():
    mcp = _make_mcp(cedar_policy_engine_id=None, cedar_enforcement_mode="none")
    registry = _registry_double()
    control = _cedar_control_client()

    svc = _make_service(registry=registry, control=control)
    await svc.add_policy(mcp, principal_oid="eb3da", principal_label="lars@x", tool_name="t1")

    registry.persist_identity.assert_called()
    assert mcp.cedar_policy_engine_id == ENGINE_ID
    assert mcp.cedar_policy_engine_arn == ENGINE_ARN
    assert mcp.cedar_enforcement_mode == "enforce"


@pytest.mark.asyncio
async def test_list_policies_engine_unset_returns_empty():
    mcp = _make_mcp(cedar_policy_engine_id=None, cedar_enforcement_mode="none")
    registry = _registry_double()
    control = _cedar_control_client()

    svc = _make_service(registry=registry, control=control)
    result = await svc.list_policies(mcp)

    assert result == {"enforcement_mode": "none", "engine_id": None, "policies": []}
    control.list_policy_summaries.assert_not_called()
    control.get_policy.assert_not_called()


@pytest.mark.asyncio
async def test_list_policies_fetches_get_policy_per_summary_and_parses():
    mcp = _make_mcp(
        cedar_policy_engine_id=ENGINE_ID,
        cedar_policy_engine_arn=ENGINE_ARN,
        cedar_enforcement_mode="enforce",
    )
    registry = _registry_double()
    control = _cedar_control_client()

    svc = _make_service(registry=registry, control=control)
    result = await svc.list_policies(mcp)

    control.list_policy_summaries.assert_called_once()
    control.get_policy.assert_called_once_with(policyEngineId=ENGINE_ID, policyId="pol-1")

    assert result["enforcement_mode"] == "enforce"
    assert result["engine_id"] == ENGINE_ID
    assert len(result["policies"]) == 1
    row = result["policies"][0]
    assert row["policy_id"] == "pol-1"
    assert row["user_oid"] == "eb3da"
    assert row["user_label"] == "lars@example.com"
    assert row["tool"] == "ClaimsTarget___get_claim"
    assert row["effect"] == "allow"
    assert "// agp:v2" in row["cedar_text"]


@pytest.mark.asyncio
async def test_list_policies_foreign_policy_row_has_null_user():
    foreign = "permit(principal, action, resource);\n"
    mcp = _make_mcp(
        cedar_policy_engine_id=ENGINE_ID,
        cedar_policy_engine_arn=ENGINE_ARN,
        cedar_enforcement_mode="log_only",
    )
    registry = _registry_double()
    control = _cedar_control_client(get_policy_statement=foreign)

    svc = _make_service(registry=registry, control=control)
    result = await svc.list_policies(mcp)

    row = result["policies"][0]
    assert row["user_oid"] is None
    assert row["user_label"] is None
    assert row["tool"] is None
    assert row["effect"] == "allow"
    assert row["cedar_text"] == foreign


@pytest.mark.asyncio
async def test_list_policies_paginates():
    page1 = {
        "policies": [{"policyId": "pol-1", "name": "a", "status": "ACTIVE"}],
        "nextToken": "tok",
    }
    page2 = {
        "policies": [{"policyId": "pol-2", "name": "b", "status": "ACTIVE"}],
        "nextToken": None,
    }
    mcp = _make_mcp(
        cedar_policy_engine_id=ENGINE_ID,
        cedar_policy_engine_arn=ENGINE_ARN,
        cedar_enforcement_mode="enforce",
    )
    registry = _registry_double()
    control = _cedar_control_client(summaries_pages=[page1, page2])

    svc = _make_service(registry=registry, control=control)
    result = await svc.list_policies(mcp)

    assert control.list_policy_summaries.call_count == 2
    _, kwargs = control.list_policy_summaries.call_args_list[1]
    assert kwargs["nextToken"] == "tok"
    assert len(result["policies"]) == 2
    assert {r["policy_id"] for r in result["policies"]} == {"pol-1", "pol-2"}


@pytest.mark.asyncio
async def test_delete_policy_calls_delete_and_waiter():
    mcp = _make_mcp(
        cedar_policy_engine_id=ENGINE_ID,
        cedar_policy_engine_arn=ENGINE_ARN,
        cedar_enforcement_mode="enforce",
    )
    registry = _registry_double()
    control = _cedar_control_client()

    svc = _make_service(registry=registry, control=control)
    await svc.delete_policy(mcp, "pol-1")

    control.delete_policy.assert_called_once_with(policyEngineId=ENGINE_ID, policyId="pol-1")
    control.get_waiter.assert_any_call("policy_deleted")
    control.update_gateway.assert_not_called()
    control.delete_policy_engine.assert_not_called()


@pytest.mark.asyncio
async def test_delete_policy_engine_unset_raises():
    mcp = _make_mcp(cedar_policy_engine_id=None, cedar_enforcement_mode="none")
    registry = _registry_double()
    control = _cedar_control_client()

    svc = _make_service(registry=registry, control=control)
    with pytest.raises(McpCedarError):
        await svc.delete_policy(mcp, "pol-1")
    control.delete_policy.assert_not_called()


@pytest.mark.asyncio
async def test_set_enforcement_enforce_attaches():
    mcp = _make_mcp(cedar_policy_engine_id=None, cedar_enforcement_mode="none")
    registry = _registry_double()
    control = _cedar_control_client()

    svc = _make_service(registry=registry, control=control)
    result = await svc.set_enforcement(mcp, "enforce")

    control.create_policy_engine.assert_called_once()
    _, kwargs = control.update_gateway.call_args
    assert kwargs["policyEngineConfiguration"]["mode"] == "ENFORCE"
    assert result.cedar_enforcement_mode == "enforce"
    registry.persist_identity.assert_called()


@pytest.mark.asyncio
async def test_set_enforcement_log_only_attaches_log_only():
    mcp = _make_mcp(
        cedar_policy_engine_id=ENGINE_ID,
        cedar_policy_engine_arn=ENGINE_ARN,
        cedar_enforcement_mode="none",
    )
    registry = _registry_double()
    control = _cedar_control_client()

    svc = _make_service(registry=registry, control=control)
    result = await svc.set_enforcement(mcp, "log_only")

    control.create_policy_engine.assert_not_called()
    _, kwargs = control.update_gateway.call_args
    assert kwargs["policyEngineConfiguration"]["mode"] == "LOG_ONLY"
    assert result.cedar_enforcement_mode == "log_only"


@pytest.mark.asyncio
async def test_set_enforcement_disabled_detaches():
    mcp = _make_mcp(
        cedar_policy_engine_id=ENGINE_ID,
        cedar_policy_engine_arn=ENGINE_ARN,
        cedar_enforcement_mode="enforce",
    )
    registry = _registry_double()
    control = _cedar_control_client()
    read = control.get_gateway.return_value

    svc = _make_service(registry=registry, control=control)
    result = await svc.set_enforcement(mcp, "disabled")

    control.update_gateway.assert_called()
    _, kwargs = control.update_gateway.call_args
    assert "policyEngineConfiguration" not in kwargs
    assert kwargs["authorizerType"] == read["authorizerType"]
    assert kwargs["authorizerConfiguration"] == read["authorizerConfiguration"]
    assert result.cedar_enforcement_mode == "none"
    control.delete_policy_engine.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "start_mode"), [("enforce", "none"), ("disabled", "enforce")])
async def test_attach_and_detach_keep_a_terraform_gateways_allowed_scopes(mode, start_mode):
    """A repo-built gateway is born with ``allowedScopes=["Invoke"]`` (the gate that
    403s app-only tokens). The full-replace PUT must carry it through on attach AND detach —
    dropping it silently re-admits app-only tokens for the MCP audience."""
    mcp = _make_mcp(
        cedar_policy_engine_id=ENGINE_ID,
        cedar_policy_engine_arn=ENGINE_ARN,
        cedar_enforcement_mode=start_mode,
    )
    control = _cedar_control_client()
    scoped = {
        "customJWTAuthorizer": {
            **AUTHORIZER_CONFIG["customJWTAuthorizer"],
            "allowedScopes": ["Invoke"],
        }
    }
    control.get_gateway.return_value = {
        **control.get_gateway.return_value,
        "authorizerConfiguration": scoped,
    }

    svc = _make_service(registry=_registry_double(), control=control)
    await svc.set_enforcement(mcp, mode)

    _, kwargs = control.update_gateway.call_args
    assert kwargs["authorizerConfiguration"]["customJWTAuthorizer"]["allowedScopes"] == ["Invoke"]


@pytest.mark.asyncio
async def test_mcp_cedar_error_detail_for_invalid_mode_carries_no_upstream_body():
    """The only kind whose message is the detail: the service's own sentence, built before any
    control-plane call, so no AWS text can reach it."""
    mcp = _make_mcp(
        cedar_policy_engine_id=ENGINE_ID,
        cedar_policy_engine_arn=ENGINE_ARN,
        cedar_enforcement_mode="enforce",
    )
    registry = _registry_double()
    control = _cedar_control_client()

    svc = _make_service(registry=registry, control=control)
    with pytest.raises(McpCedarError) as exc:
        await svc.set_enforcement(mcp, "bogus")

    assert (exc.value.kind, exc.value.status_code) == ("invalid_mode", 400)
    assert exc.value.detail == "unknown enforcement mode: bogus"
    assert control.method_calls == []


@pytest.mark.asyncio
async def test_conflict_exception_on_attach_is_retried():
    mcp = _make_mcp(
        cedar_policy_engine_id=ENGINE_ID,
        cedar_policy_engine_arn=ENGINE_ARN,
        cedar_enforcement_mode="none",
    )
    registry = _registry_double()
    control = _cedar_control_client()

    conflict = ClientError(
        {"Error": {"Code": "ConflictException", "Message": "gateway is UPDATING"}},
        "UpdateGateway",
    )
    control.update_gateway.side_effect = [conflict, {"status": "UPDATING"}]

    svc = _make_service(registry=registry, control=control)
    await svc.set_enforcement(mcp, "enforce")

    assert control.update_gateway.call_count >= 2
    assert control.get_gateway.called


def _created_statement(control: MagicMock) -> str:
    """The Cedar statement the service handed to create_policy."""
    _, kwargs = control.create_policy.call_args
    return kwargs["definition"]["cedar"]["statement"]


@pytest.mark.asyncio
async def test_add_policy_with_numeric_condition_passes_built_statement_to_create():
    mcp = _make_mcp_with_tools()
    registry = _registry_double()
    control = _cedar_control_client()

    svc = _make_service(registry=registry, control=control)
    row = await svc.add_policy(
        mcp,
        principal_oid="eb3da",
        principal_label="lars@x",
        tool_name="transfer",
        conditions=[{"param": "amount", "op": "<", "value": "1000", "type": "number"}],
    )

    statement = _created_statement(control)
    assert "context.input has amount && context.input.amount < 1000" in statement
    assert len(row["conditions"]) == 1
    assert row["conditions"][0] == {
        "param": "amount",
        "op": "<",
        "value": "1000",
        "type": "number",
    }
    assert row["effect"] == "allow"
    assert row["managed"] is True
    assert row["cedar_text"] == statement


@pytest.mark.asyncio
async def test_add_policy_deny_effect_builds_forbid():
    mcp = _make_mcp_with_tools()
    registry = _registry_double()
    control = _cedar_control_client()

    svc = _make_service(registry=registry, control=control)
    row = await svc.add_policy(
        mcp,
        principal_oid="eb3da",
        principal_label="lars@x",
        tool_name="transfer",
        effect="deny",
    )

    statement = _created_statement(control)
    assert "forbid(" in statement
    assert row["effect"] == "deny"
    assert row["managed"] is True


@pytest.mark.asyncio
async def test_add_policy_all_users_deny():
    mcp = _make_mcp_with_tools()
    registry = _registry_double()
    control = _cedar_control_client()

    svc = _make_service(registry=registry, control=control)
    row = await svc.add_policy(
        mcp,
        principal_oid=None,
        principal_label="Everyone",
        tool_name="transfer",
        effect="deny",
        conditions=[{"param": "amount", "op": ">", "value": "10000", "type": "number"}],
    )

    statement = _created_statement(control)
    assert "forbid(" in statement
    assert 'principal.getTag("oid")' not in statement
    assert "context.input has amount && context.input.amount > 10000" in statement
    assert row["user_oid"] is None
    assert row["effect"] == "deny"
    assert row["managed"] is True


@pytest.mark.asyncio
async def test_add_policy_allow_without_user_raises_cedar_error():
    mcp = _make_mcp_with_tools()
    registry = _registry_double()
    control = _cedar_control_client()

    svc = _make_service(registry=registry, control=control)
    with pytest.raises(McpCedarError):
        await svc.add_policy(
            mcp,
            principal_oid=None,
            principal_label="lars@x",
            tool_name="transfer",
            effect="allow",
        )
    control.create_policy.assert_not_called()
    control.create_policy_engine.assert_not_called()
    control.update_gateway.assert_not_called()


@pytest.mark.asyncio
async def test_add_policy_conditions_on_all_tools_raises():
    mcp = _make_mcp_with_tools()
    registry = _registry_double()
    control = _cedar_control_client()

    svc = _make_service(registry=registry, control=control)
    with pytest.raises(McpCedarError):
        await svc.add_policy(
            mcp,
            principal_oid="eb3da",
            principal_label="lars@x",
            tool_name=None,
            conditions=[{"param": "amount", "op": "<", "value": "1000", "type": "number"}],
        )
    control.create_policy.assert_not_called()
    control.create_policy_engine.assert_not_called()


@pytest.mark.asyncio
async def test_add_policy_unknown_param_raises_cedar_error():
    mcp = _make_mcp_with_tools()
    registry = _registry_double()
    control = _cedar_control_client()

    svc = _make_service(registry=registry, control=control)
    with pytest.raises(McpCedarError):
        await svc.add_policy(
            mcp,
            principal_oid="eb3da",
            principal_label="lars@x",
            tool_name="transfer",
            conditions=[{"param": "nope", "op": "<", "value": "1", "type": "number"}],
        )
    control.create_policy.assert_not_called()


@pytest.mark.asyncio
async def test_add_policy_type_mismatch_raises_cedar_error():
    mcp = _make_mcp_with_tools()
    registry = _registry_double()
    control = _cedar_control_client()

    svc = _make_service(registry=registry, control=control)
    with pytest.raises(McpCedarError):
        await svc.add_policy(
            mcp,
            principal_oid="eb3da",
            principal_label="lars@x",
            tool_name="get_client",
            conditions=[{"param": "client_id", "op": "<", "value": "id1", "type": "string"}],
        )
    control.create_policy.assert_not_called()


@pytest.mark.asyncio
async def test_add_policy_no_conditions_still_works():
    mcp = _make_mcp_with_tools()
    registry = _registry_double()
    control = _cedar_control_client()

    svc = _make_service(registry=registry, control=control)
    row = await svc.add_policy(
        mcp,
        principal_oid="eb3da",
        principal_label="lars@x",
        tool_name="transfer",
    )

    control.create_policy.assert_called_once()
    assert row["conditions"] == []
    assert row["effect"] == "allow"
    assert row["managed"] is True
    assert row["tool"] == "transfer"
    assert row["user_oid"] == "eb3da"


@pytest.mark.asyncio
async def test_list_policies_carries_effect_and_conditions():
    conditioned_forbid = build_cedar_policy(
        principal_oid=None,
        principal_label="Everyone",
        gateway_arn=GATEWAY_ARN,
        tool_name="transfer",
        effect="deny",
        conditions=[{"param": "amount", "op": ">", "value": "10000", "type": "number"}],
    )
    mcp = _make_mcp(
        cedar_policy_engine_id=ENGINE_ID,
        cedar_policy_engine_arn=ENGINE_ARN,
        cedar_enforcement_mode="enforce",
    )
    registry = _registry_double()
    control = _cedar_control_client(get_policy_statement=conditioned_forbid)

    svc = _make_service(registry=registry, control=control)
    result = await svc.list_policies(mcp)

    row = result["policies"][0]
    assert row["effect"] == "deny"
    assert row["managed"] is True
    assert len(row["conditions"]) == 1
    assert row["conditions"][0]["param"] == "amount"


@pytest.mark.asyncio
async def test_list_policies_foreign_row_detects_effect():
    foreign = "forbid(principal, action, resource);\n"
    mcp = _make_mcp(
        cedar_policy_engine_id=ENGINE_ID,
        cedar_policy_engine_arn=ENGINE_ARN,
        cedar_enforcement_mode="log_only",
    )
    registry = _registry_double()
    control = _cedar_control_client(get_policy_statement=foreign)

    svc = _make_service(registry=registry, control=control)
    result = await svc.list_policies(mcp)

    row = result["policies"][0]
    assert row["managed"] is False
    assert row["effect"] == "deny"
    assert row["conditions"] == []
    assert row["user_oid"] is None


ADOPTED_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:policy-engine/agp_cedar_OLDID-abc"


def test_ensure_engine_adopts_preattached_engine():
    """A gateway that already has a policy engine is ADOPTED, never duplicated.

    Regression guard for re-registration: the new record's envelope has NO
    cedar_policy_engine_id, but the live gateway still has its engine attached (gateways
    and policy engines stayed in the bedrock-agentcore namespace — only Registry moved).
    Creating a second engine here silently orphans the first one and every policy in it.

    The adopted engine NAME keeps the OLD record id (``agp_cedar_OLDID-abc``) — accepted
    drift. Renaming would churn live ENFORCE-mode enforcement for cosmetics, so
    the id is taken verbatim from the ARN tail.

    NOT an ``@pytest.mark.asyncio`` test: ``_ensure_engine`` is one of the private SYNC
    boto3 helpers (production dispatches it off-loop via ``anyio.to_thread.run_sync``), so
    it is called directly here.
    """
    mcp = _make_mcp(cedar_policy_engine_id=None)
    control = _cedar_control_client()
    control.get_gateway.return_value = {
        **control.get_gateway.return_value,
        "policyEngineConfiguration": {"arn": ADOPTED_ARN, "mode": "ENFORCE"},
    }
    service = _make_service(registry=_registry_double(), control=control)

    service._ensure_engine(mcp)

    control.create_policy_engine.assert_not_called()
    assert mcp.cedar_policy_engine_arn == ADOPTED_ARN
    assert mcp.cedar_policy_engine_id == "agp_cedar_OLDID-abc"
    assert control.get_gateway.return_value["authorizerConfiguration"] == AUTHORIZER_CONFIG


def _control_reporting_engine(mode: str) -> MagicMock:
    """A control client whose ``get_gateway`` reports an already-attached engine in ``mode``.

    The base dict is SPREAD, not replaced: it carries ``authorizerType`` /
    ``authorizerConfiguration``, and the adoption probe must never strip the inbound gate
    (guarded by ``test_ensure_engine_adopts_preattached_engine``).
    """
    control = _cedar_control_client()
    control.get_gateway.return_value = {
        **control.get_gateway.return_value,
        "policyEngineConfiguration": {"arn": ADOPTED_ARN, "mode": mode},
    }
    return control


def test_ensure_engine_adopts_enforce_mode():
    """Adoption takes the gateway's MODE too, not just the ARN.

    Without this the envelope stays ``"none"`` and every read path reports a hard-denying
    gateway as "open". Sync call — ``_ensure_engine`` is a private sync boto3 helper.
    """
    mcp = _make_mcp(cedar_policy_engine_id=None, cedar_enforcement_mode="none")
    control = _control_reporting_engine("ENFORCE")
    service = _make_service(registry=_registry_double(), control=control)

    service._ensure_engine(mcp)

    assert mcp.cedar_enforcement_mode == "enforce"


def test_ensure_engine_adopting_log_only_gateway_does_not_escalate_to_enforce():
    """A LOG_ONLY gateway must stay LOG_ONLY — the silent-escalation guard.

    ``add_policy`` attaches ENFORCE whenever ``cedar_enforcement_mode == "none"``, so an
    adoption that ignored the reported mode would turn a gateway deliberately left in
    observe-only staging into a hard-denying one, with no prompt and no log line.
    """
    mcp = _make_mcp(cedar_policy_engine_id=None, cedar_enforcement_mode="none")
    control = _control_reporting_engine("LOG_ONLY")
    service = _make_service(registry=_registry_double(), control=control)

    service._ensure_engine(mcp)

    assert mcp.cedar_enforcement_mode == "log_only"
    assert mcp.cedar_enforcement_mode != "enforce"
    assert control.get_gateway.return_value["authorizerConfiguration"] == AUTHORIZER_CONFIG


@pytest.mark.asyncio
async def test_list_policies_on_reregistered_mcp_adopts_engine_and_lists_real_policies():
    """A re-registered MCP (EMPTY envelope engine id) whose gateway still reports an engine
    must show the REAL policies + the REAL mode, never the misleading empty/"none" payload.

    That payload renders as "open with no policies" while the live gateway default-denies
    through policies the operator can neither see nor obtain a ``policy_id`` for — and the
    natural response ("re-add the missing policies") duplicates them against a live engine.
    """
    mcp = _make_mcp(cedar_policy_engine_id=None, cedar_enforcement_mode="none")
    control = _control_reporting_engine("ENFORCE")
    service = _make_service(registry=_registry_double(), control=control)

    result = await service.list_policies(mcp)

    adopted_id = ADOPTED_ARN.rsplit("/", 1)[-1]
    assert result["engine_id"] == adopted_id
    assert result["enforcement_mode"] == "enforce"
    control.list_policy_summaries.assert_called_once()
    control.get_policy.assert_called_once_with(policyEngineId=adopted_id, policyId="pol-1")
    assert len(result["policies"]) == 1
    assert result["policies"][0]["policy_id"] == "pol-1"
    assert result["policies"][0]["user_oid"] == "eb3da"


@pytest.mark.asyncio
async def test_add_policy_on_reregistered_mcp_persists_the_adopted_engine_id():
    """The adopted engine id is PERSISTED on the first mutating use — the DELETE-404 guard.

    Every HTTP request re-hydrates a FRESH ``McpServer`` from the registry record (no
    caching), so an adoption that lives only in memory dies with the request: the operator
    POSTs a policy (201) and then the DELETE of that very row hydrates the still-empty
    envelope and ``delete_policy`` raises → HTTP 404. Persisting here is what makes the next
    request hydrate the adopted engine.

    Recorded via a ``side_effect`` (not ``call_args``) so the assertion pins the value the
    envelope carried AT persist time, not whatever the shared object ends up holding.
    """
    persisted_engine_ids: list[str | None] = []
    registry = _registry_double()
    registry.persist_identity.side_effect = lambda m: (
        persisted_engine_ids.append(m.cedar_policy_engine_id) or m
    )
    mcp = _make_mcp(cedar_policy_engine_id=None, cedar_enforcement_mode="none")
    control = _control_reporting_engine("ENFORCE")
    service = _make_service(registry=registry, control=control)

    await service.add_policy(
        mcp,
        principal_oid="eb3da",
        principal_label="lars@example.com",
        tool_name="ClaimsTarget___get_claim",
    )

    assert persisted_engine_ids == [ADOPTED_ARN.rsplit("/", 1)[-1]]
    control.create_policy_engine.assert_not_called()
    control.update_gateway.assert_not_called()


@pytest.mark.asyncio
async def test_list_policies_never_persists_the_adopted_engine():
    """The VIEWER-reachable GET must stay a pure READ — the security half of the persist fix.

    ``GET /{mcp_id}/policies`` is gated at ``Role.VIEWER`` (``routes/mcp_cedar.py:136-140``)
    and calls the shared adoption probe directly. Persisting from inside the probe would let a
    VIEWER trigger an ``UpdateRegistryRecord`` write, so the persist lives in
    ``_ensure_engine`` (mutating callers only). Same MCP shape as the add_policy test above.
    """
    registry = _registry_double()
    mcp = _make_mcp(cedar_policy_engine_id=None, cedar_enforcement_mode="none")
    control = _control_reporting_engine("ENFORCE")
    service = _make_service(registry=registry, control=control)

    result = await service.list_policies(mcp)

    assert result["engine_id"] == ADOPTED_ARN.rsplit("/", 1)[-1]
    registry.persist_identity.assert_not_called()
    control.update_gateway.assert_not_called()
    control.create_policy_engine.assert_not_called()


def test_adopting_unrecognised_mode_on_a_fresh_record_raises_instead_of_enforcing():
    """An unknown gateway mode with NO recorded mode to fall back on FAILS LOUD.

    A freshly re-registered record's recorded mode is ``"none"``, and keeping ``"none"`` is
    not neutral: it takes ``add_policy``'s ``== _MODE_NONE`` branch and attaches **ENFORCE**,
    i.e. an unknown mode would silently switch on default-deny. Guessing ``_MODE_FROM_AWS``'s
    missing value would be worse, so raise and name the value instead. Reachable only if AWS
    extends ``GatewayPolicyEngineMode`` (today exactly ``['LOG_ONLY','ENFORCE']``, required on
    the ``GetGateway`` output) — a robustness guard, not a live bug.
    """
    mcp = _make_mcp(cedar_policy_engine_id=None, cedar_enforcement_mode="none")
    control = _control_reporting_engine("AUDIT")
    service = _make_service(registry=_registry_double(), control=control)

    with pytest.raises(McpCedarError, match="AUDIT"):
        service._ensure_engine(mcp)

    control.update_gateway.assert_not_called()
    control.create_policy_engine.assert_not_called()


def test_adopting_unrecognised_mode_keeps_an_ALREADY_RECORDED_mode_untouched():
    """The safe half of the same branch: a record that HAS a real mode keeps it, no raise.

    With ``log_only``/``enforce`` already recorded, ``add_policy``'s ENFORCE-attach branch is
    already unreachable, so there is nothing to fail loud about — warn and leave the recorded
    value alone rather than guessing the unknown AWS mode.
    """
    mcp = _make_mcp(cedar_policy_engine_id=None, cedar_enforcement_mode="log_only")
    control = _control_reporting_engine("AUDIT")
    service = _make_service(registry=_registry_double(), control=control)

    service._ensure_engine(mcp)

    assert mcp.cedar_enforcement_mode == "log_only"
    assert mcp.cedar_policy_engine_id == ADOPTED_ARN.rsplit("/", 1)[-1]
    control.create_policy_engine.assert_not_called()


@pytest.mark.asyncio
async def test_delete_policy_engine_detaches_then_deletes():
    mcp = _make_mcp(
        cedar_policy_engine_id=ENGINE_ID,
        cedar_policy_engine_arn=ENGINE_ARN,
        cedar_enforcement_mode="enforce",
    )
    registry = _registry_double()
    control = _cedar_control_client()
    control.get_gateway.return_value = {
        **control.get_gateway.return_value,
        "policyEngineConfiguration": {"arn": ENGINE_ARN, "mode": "ENFORCE"},
    }

    manager = MagicMock()
    manager.attach_mock(control.update_gateway, "update_gateway")
    manager.attach_mock(control.delete_policy_engine, "delete_policy_engine")

    svc = _make_service(registry=registry, control=control)
    await svc.delete_policy_engine(mcp)

    assert [c[0] for c in manager.mock_calls] == ["update_gateway", "delete_policy_engine"]

    kwargs = control.update_gateway.call_args.kwargs
    assert "policyEngineConfiguration" not in kwargs
    assert kwargs["authorizerType"] == "CUSTOM_JWT"
    assert kwargs["authorizerConfiguration"] == AUTHORIZER_CONFIG

    control.delete_policy_engine.assert_called_once_with(policyEngineId=ENGINE_ID)
    control.get_waiter.assert_any_call("policy_engine_deleted")


@pytest.mark.asyncio
async def test_delete_policy_engine_noop_when_no_engine_anywhere():
    """No engine on the record and none on the live gateway ⇒ no delete, no gateway write."""
    mcp = _make_mcp(cedar_policy_engine_id=None, cedar_enforcement_mode="none")
    control = _cedar_control_client()
    control.get_gateway.return_value = {
        "gatewayId": GATEWAY_ID,
        "name": "demo-claims-gw",
        "roleArn": "arn:aws:iam::123456789012:role/gateway-service-role",
        "authorizerType": "CUSTOM_JWT",
        "authorizerConfiguration": AUTHORIZER_CONFIG,
        "status": "READY",
    }

    svc = _make_service(registry=_registry_double(), control=control)
    await svc.delete_policy_engine(mcp)

    control.delete_policy_engine.assert_not_called()
    control.update_gateway.assert_not_called()


@pytest.mark.asyncio
async def test_delete_policy_engine_never_deletes_an_engine_the_record_does_not_own():
    """The teardown deletes ONLY an engine id stored ON THE RECORD.

    `gateway_id` descends from `gateway_arn`, a CALLER-SETTABLE field, so the engine a LIVE
    gateway reports is not provably this record's — and a policy-engine delete is irreversible,
    taking every policy in it and that gateway's default-deny with it. So a record with an
    empty engine id and a gateway that IS enforcing makes zero mutating calls: no delete (the
    wrong-target hazard) and no detach either (detaching is the fail-open act). It is reported
    `skipped` with a safe reason, so the log names the engine that survived."""
    mcp = _make_mcp(cedar_policy_engine_id=None, cedar_enforcement_mode="none")
    control = _cedar_control_client()
    control.get_gateway.return_value = {
        "gatewayId": GATEWAY_ID,
        "name": "demo-claims-gw",
        "roleArn": "arn:aws:iam::123456789012:role/gateway-service-role",
        "authorizerType": "CUSTOM_JWT",
        "authorizerConfiguration": AUTHORIZER_CONFIG,
        "policyEngineConfiguration": {"arn": ENGINE_ARN, "mode": "ENFORCE"},
        "status": "READY",
    }

    svc = _make_service(registry=_registry_double(), control=control)
    note = await svc.delete_policy_engine(mcp)

    control.delete_policy_engine.assert_not_called()
    control.update_gateway.assert_not_called()
    assert note == "engine not owned by this record"
    assert not mcp.cedar_policy_engine_id


@pytest.mark.asyncio
async def test_delete_policy_engine_never_detaches_an_engine_the_record_does_not_own():
    """The DETACH is ownership-guarded too.

    A detach is a full-replace `update_gateway` that omits `policyEngineConfiguration`, i.e. it
    strips Cedar (and its default-deny) from whatever gateway `gateway_id` names, and
    `gateway_id` descends from the caller-settable `gateway_arn`, so a record legitimately
    owning engine E but repointed at another team's gateway G2 could otherwise strip G2's
    engine. So: a live gateway reporting a DIFFERENT engine ⇒ zero `update_gateway`, while the
    record's OWN engine is still deleted."""
    mcp = _make_mcp(
        cedar_policy_engine_id=ENGINE_ID,
        cedar_policy_engine_arn=ENGINE_ARN,
        cedar_enforcement_mode="enforce",
    )
    control = _cedar_control_client()
    other_engine_arn = "arn:aws:bedrock-agentcore:us-east-1:123456789012:policy-engine/agpcedar_other_team"
    control.get_gateway.return_value = {
        **control.get_gateway.return_value,
        "policyEngineConfiguration": {"arn": other_engine_arn, "mode": "ENFORCE"},
    }

    svc = _make_service(registry=_registry_double(), control=control)
    assert await svc.delete_policy_engine(mcp) is None

    control.update_gateway.assert_not_called()
    control.delete_policy_engine.assert_called_once_with(policyEngineId=ENGINE_ID)


@pytest.mark.asyncio
async def test_delete_policy_engine_returns_no_skip_note_when_it_deleted_the_engine():
    """The `None` half of the same contract: a record that DOES own its engine reports
    `deleted`, so the note must be falsy on the happy path."""
    mcp = _make_mcp(
        cedar_policy_engine_id=ENGINE_ID,
        cedar_policy_engine_arn=ENGINE_ARN,
        cedar_enforcement_mode="enforce",
    )
    control = _cedar_control_client()

    svc = _make_service(registry=_registry_double(), control=control)
    assert await svc.delete_policy_engine(mcp) is None
    control.delete_policy_engine.assert_called_once_with(policyEngineId=ENGINE_ID)


@pytest.mark.asyncio
async def test_delete_policy_engine_idempotent_on_already_gone():
    mcp = _make_mcp(
        cedar_policy_engine_id=ENGINE_ID,
        cedar_policy_engine_arn=ENGINE_ARN,
        cedar_enforcement_mode="enforce",
    )
    control = _cedar_control_client()
    control.delete_policy_engine.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
        "DeletePolicyEngine",
    )

    svc = _make_service(registry=_registry_double(), control=control)
    await svc.delete_policy_engine(mcp)

    control.delete_policy_engine.assert_called_once_with(policyEngineId=ENGINE_ID)


@pytest.mark.asyncio
async def test_delete_policy_engine_still_deletes_when_the_gateway_is_already_gone():
    """A detach that cannot reach the gateway must not strand the engine: the gateway may
    have been deleted first, and the engine is the resource that keeps default-denying."""
    mcp = _make_mcp(
        cedar_policy_engine_id=ENGINE_ID,
        cedar_policy_engine_arn=ENGINE_ARN,
        cedar_enforcement_mode="enforce",
    )
    control = _cedar_control_client()
    control.get_gateway.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, "GetGateway"
    )

    svc = _make_service(registry=_registry_double(), control=control)
    await svc.delete_policy_engine(mcp)

    control.update_gateway.assert_not_called()
    control.delete_policy_engine.assert_called_once_with(policyEngineId=ENGINE_ID)


@pytest.mark.asyncio
async def test_delete_policy_engine_reraises_other_client_error_as_cedar_error():
    mcp = _make_mcp(
        cedar_policy_engine_id=ENGINE_ID,
        cedar_policy_engine_arn=ENGINE_ARN,
        cedar_enforcement_mode="enforce",
    )
    control = _cedar_control_client()
    control.delete_policy_engine.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
        "DeletePolicyEngine",
    )

    svc = _make_service(registry=_registry_double(), control=control)
    with pytest.raises(McpCedarError):
        await svc.delete_policy_engine(mcp)
