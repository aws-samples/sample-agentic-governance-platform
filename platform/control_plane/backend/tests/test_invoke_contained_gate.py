"""The invoke gate. A contained agent is refused inside ``AwsPlatformAdapter.invoke``
before any HTTP call (harness branch included), the invoke route answers 409
``agent is contained``, and the eval runner records it as the item's error.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("reset_modules")

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:ACCOUNT:runtime/agent-abc123"
HARNESS_ARN = "arn:aws:bedrock-agentcore:us-east-1:ACCOUNT:harness/claims_helper-abc123"


@pytest.fixture
def store():
    """A fresh in-memory containment store behind the singleton the gate reads."""
    import services.containment_store as store_module
    from services.containment_store import ContainmentStore

    s = ContainmentStore(table_name="")
    with patch.object(store_module, "_containment_store", s):
        yield s


def _contain(store, agent_id="rec-123"):
    from core.state_store import ABSENT
    from models.containment import STEP_ORDER, ContainmentState, StepResult

    store.put_state(
        ContainmentState(
            agent_id=agent_id,
            state="CONTAINED",
            job="contained",
            since="2026-10-13T00:00:00+00:00",
            by_oid="op-oid",
            by_name="op@example.com",
            reason="tool misuse",
            steps=[StepResult(id=s) for s in STEP_ORDER],
        ),
        {"state": {ABSENT}},
    )


def _make_agent(**overrides):
    from models.agent import Agent, AgentAuthType, LifecycleState, Origin, Platform

    now = datetime.now(timezone.utc)
    base: dict[str, Any] = dict(
        id="rec-123",
        name="claims-triage-de",
        purpose="Triage claims",
        lifecycle_state=LifecycleState.APPROVED,
        origin=Origin.REGISTERED,
        platform=Platform.AWS_BEDROCK,
        auth_type=AgentAuthType.ENTRA,
        agent_arn=RUNTIME_ARN,
        entra_sp_id="sp-obj-id",
        entra_app_audience="api://agp-agent-rec-123",
        invoker_role_id="role-invoker-guid",
        admin_role_id="role-admin-guid",
        identity_status="provisioned",
        created_at=now,
        updated_at=now,
        created_by="maria.bauer@example.com",
    )
    base.update(overrides)
    return Agent(**base)


def _no_http():
    """Any httpx client construction fails the test: the gate must precede all HTTP."""

    def _boom(*a, **k):
        raise AssertionError("HTTP attempted for a contained agent")

    return patch.object(httpx, "AsyncClient", _boom)


@pytest.mark.parametrize("arn", [RUNTIME_ARN, HARNESS_ARN])
def test_adapter_refuses_contained_agent_before_http(store, arn):
    from api.routes.agents import InvokeRequest
    from services.platform_adapters.aws import AgentContainedError, AwsPlatformAdapter

    _contain(store)
    adapter = AwsPlatformAdapter(identity=MagicMock())
    with _no_http(), pytest.raises(AgentContainedError) as err:
        asyncio.run(
            adapter.invoke(
                _make_agent(agent_arn=arn), InvokeRequest(prompt="hi"), MagicMock(oid="u"), "obo-token"
            )
        )
    assert err.value.status_code == 409
    assert err.value.detail == "agent is contained"


def _build_client(agent):
    import api.routes.agents as agents_module
    import api.routes.grants as grants_module
    import api.routes.users as users_module
    from core.errors import install_error_handlers
    from services.tenant_resolver import TenantContext

    registry = MagicMock()
    registry.get.return_value = agent
    agents_module._svc = registry
    graph = MagicMock()
    graph.obo_exchange = AsyncMock(return_value="obo-agent-token")
    grants_module._graph_svc = graph

    class _GlobalResolver:
        async def resolve(self, principal):
            return TenantContext(is_global=True, tenant_ids=frozenset(), tenants=())

    users_module._tenant_resolver = _GlobalResolver()
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(agents_module.router, prefix="/api/v1")
    return TestClient(app)


CLAIMS = {"oid": "viewer-oid", "preferred_username": "viewer@example.com", "roles": ["Platform.Viewer"]}
H = {"Authorization": "Bearer fake-inbound-token"}


def test_route_returns_409_agent_is_contained(entra_settings, store):
    _contain(store)
    client = _build_client(_make_agent())
    with patch("core.security_entra.verify_entra_token", return_value=CLAIMS), _no_http():
        resp = client.post("/api/v1/agents/rec-123/invoke", json={"prompt": "hi"}, headers=H)
    assert resp.status_code == 409
    assert resp.json() == {"detail": "agent is contained", "code": "invoke.contained"}


def test_uncontained_agent_is_unaffected(entra_settings, store):
    _contain(store, agent_id="some-other-agent")
    client = _build_client(_make_agent())
    sse = 'data: {"message": {"role": "assistant", "content": [{"text": "hi"}]}}\n\n'
    resp_obj = MagicMock(status_code=200, headers={"content-type": "text/event-stream"}, text=sse)

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            return resp_obj

        async def aclose(self):
            return None

    with (
        patch("core.security_entra.verify_entra_token", return_value=CLAIMS),
        patch.object(httpx, "AsyncClient", _Client),
    ):
        resp = client.post("/api/v1/agents/rec-123/invoke", json={"prompt": "hi"}, headers=H)
    assert resp.status_code == 200, resp.text
    assert resp.json()["response"] == "hi"


def test_eval_runner_records_contained_as_item_error(store):
    from models.eval import DatasetItem
    from services.eval_runner import EvalRunner
    from services.platform_adapters.aws import AwsPlatformAdapter

    _contain(store)
    adapter = AwsPlatformAdapter(identity=MagicMock())
    item = DatasetItem(id="i1", input="hi")
    with _no_http():
        result = EvalRunner._invoke(
            MagicMock(), adapter, _make_agent(), item, "obo-token", MagicMock(oid="u")
        )
    assert result.error == "agent is contained"
