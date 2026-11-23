"""The containment routes (``routes/containment.py``, prefix ``/agents``).

Real ``require_role`` + ``current_principal`` path (AUTH_PROVIDER=entra, mocked token verify),
a real ``ContainmentService`` over the in-memory stores, and a mocked agents registry. The
background jobs (``run_contain`` / ``run_restore``) are replaced by AsyncMocks so the tests
assert they are scheduled without running any AWS lever.
"""

import time
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("reset_modules")

REASON = "tool misuse on the claims MCP"


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
        agent_arn="arn:aws:bedrock-agentcore:us-east-1:ACCOUNT:runtime/agent-abc123",
        identity_status="provisioned",
        created_at=now,
        updated_at=now,
        created_by="maria.bauer@example.com",
        tenant_id="ten-1",
    )
    base.update(overrides)
    return Agent(**base)


class _Resolver:
    def __init__(self, ctx):
        self._ctx = ctx

    async def resolve(self, principal):
        return self._ctx


def _ctx(*, is_global=True, tenant_ids=()):
    from services.tenant_resolver import TenantContext

    return TenantContext(is_global=is_global, tenant_ids=frozenset(tenant_ids), tenants=())


@pytest.fixture
def env(entra_settings):
    """App with the containment router; real service over fresh in-memory stores."""
    import api.routes.agents as agents_module
    import api.routes.users as users_module
    import services.containment_service as svc_module
    import services.containment_store as store_module
    from services.containment_service import ContainmentService
    from services.containment_store import AuditStore, ContainmentStore

    agent = _make_agent()
    registry = MagicMock()
    registry.get.side_effect = lambda aid: agent if aid == agent.id else None
    agents_module._svc = registry
    users_module._tenant_resolver = _Resolver(_ctx())

    store, audit = ContainmentStore(table_name=""), AuditStore(table_name="")
    service = ContainmentService(
        store=store,
        audit=audit,
        cedar=MagicMock(),
        levers=MagicMock(),
        agents_loader=registry.get,
        mcp_loader=lambda _id: None,
    )

    def _finished(agent_id):
        state = store.get_state(agent_id)
        assert state is not None
        state.job = "contained"
        store.put_state(state, {"job": {"running"}})

    service.run_contain = AsyncMock(side_effect=_finished)
    service.run_restore = AsyncMock()
    patches = [
        patch.object(svc_module, "_service", service),
        patch.object(store_module, "_containment_store", store),
        patch.object(store_module, "_audit_store", audit),
    ]
    for p in patches:
        p.start()

    import api.routes.containment as containment_module
    from core.errors import install_error_handlers

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(containment_module.router, prefix="/api/v1")
    yield TestClient(app), service, users_module, registry, store
    for p in patches:
        p.stop()


def _as(role: str):
    role_app = {"viewer": "Platform.Viewer", "operator": "Platform.Operator", "admin": "Platform.Admin"}[role]
    claims = {"oid": f"{role}-oid", "preferred_username": f"{role}.user@example.com", "roles": [role_app]}
    return patch("core.security_entra.verify_entra_token", return_value=claims)


H = {"Authorization": "Bearer fake-token"}
BASE = "/api/v1/agents/rec-123/containment"


def test_get_never_contained(env):
    client, *_ = env
    with _as("viewer"):
        resp = client.get(BASE, headers=H)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_id"] == "rec-123"
    assert body["state"] is None and body["job"] is None
    assert body["steps"] == [] and body["signals"] == [] and body["audit"] == []
    assert body["monitor"] == {"enabled": False}


def test_put_monitor_needs_operator(env):
    client, *_ = env
    with _as("viewer"):
        resp = client.put(f"{BASE}/monitor", json={"enabled": True}, headers=H)
    assert resp.status_code == 403


def test_put_monitor_toggles_and_audits(env):
    client, _service, *_ = env
    with _as("operator"):
        resp = client.put(f"{BASE}/monitor", json={"enabled": True}, headers=H)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"enabled": True}
        body = client.get(BASE, headers=H).json()
    assert body["monitor"] == {"enabled": True}
    assert [a["action"] for a in body["audit"]] == ["monitor-on"]
    with _as("operator"):
        resp = client.put(f"{BASE}/monitor", json={"enabled": False}, headers=H)
    assert resp.json() == {"enabled": False}


def test_contain_returns_202_and_schedules_run_contain(env):
    client, service, *_ = env
    with _as("operator"):
        resp = client.post(f"{BASE}/contain", json={"reason": REASON}, headers=H)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["state"] == "CONTAINED" and body["job"] == "running"
    assert body["by_name"] == "operator.user@example.com"
    assert [s["id"] for s in body["steps"]] == ["refuse-invokes", "cedar-forbid", "runtime-deny"]
    service.run_contain.assert_awaited_once_with("rec-123")


def test_contain_viewer_forbidden(env):
    client, service, *_ = env
    with _as("viewer"):
        resp = client.post(f"{BASE}/contain", json={"reason": REASON}, headers=H)
    assert resp.status_code == 403
    service.run_contain.assert_not_awaited()


@pytest.mark.parametrize("reason", ["7 chars", "        ", "  short  "])
def test_contain_short_reason_422(env, reason):
    client, service, *_ = env
    with _as("operator"):
        resp = client.post(f"{BASE}/contain", json={"reason": reason}, headers=H)
    assert resp.status_code == 422
    service.run_contain.assert_not_awaited()


def test_contain_twice_409(env):
    client, *_ = env
    with _as("operator"):
        client.post(f"{BASE}/contain", json={"reason": REASON}, headers=H)
        resp = client.post(f"{BASE}/contain", json={"reason": REASON}, headers=H)
    assert resp.status_code == 409
    assert resp.json() == {"detail": "agent is already contained", "code": "containment.already_contained"}


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_restore_needs_admin(env, role):
    client, service, *_ = env
    with _as(role):
        resp = client.post(f"{BASE}/restore", json={"reason": REASON}, headers=H)
    assert resp.status_code == 403
    service.run_restore.assert_not_awaited()


def test_restore_not_contained_409(env):
    client, *_ = env
    with _as("admin"):
        resp = client.post(f"{BASE}/restore", json={"reason": REASON}, headers=H)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "agent is not contained"


def test_restore_same_person_409(env):
    client, service, *_ = env
    with _as("admin"):
        client.post(f"{BASE}/contain", json={"reason": REASON}, headers=H)
        resp = client.post(f"{BASE}/restore", json={"reason": REASON}, headers=H)
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "restore requires a different person than the one who contained the agent"
    )
    service.run_restore.assert_not_awaited()


def test_restore_by_second_person_202(env):
    client, service, *_ = env
    with _as("operator"):
        client.post(f"{BASE}/contain", json={"reason": REASON}, headers=H)
    with _as("admin"):
        resp = client.post(f"{BASE}/restore", json={"reason": "false positive, verified"}, headers=H)
    assert resp.status_code == 202, resp.text
    assert resp.json()["job"] == "restoring"
    service.run_restore.assert_awaited_once_with("rec-123")


def test_restore_short_reason_422(env):
    client, *_ = env
    with _as("admin"):
        resp = client.post(f"{BASE}/restore", json={"reason": "short"}, headers=H)
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "method, path, role, body",
    [
        ("get", "", "viewer", None),
        ("put", "/monitor", "operator", {"enabled": True}),
        ("post", "/contain", "operator", {"reason": REASON}),
        ("post", "/restore", "admin", {"reason": REASON}),
    ],
)
def test_foreign_tenant_agent_404(env, method, path, role, body):
    client, service, users_module, *_ = env
    users_module._tenant_resolver = _Resolver(_ctx(is_global=False, tenant_ids={"ten-2"}))
    with _as(role):
        kwargs: dict[str, Any] = {"headers": H} if body is None else {"headers": H, "json": body}
        resp = getattr(client, method)(f"{BASE}{path}", **kwargs)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Agent not found", "code": "agent.not_found"}
    service.run_contain.assert_not_awaited()
    service.run_restore.assert_not_awaited()


def test_missing_agent_404(env):
    client, *_ = env
    with _as("viewer"):
        resp = client.get("/api/v1/agents/nope/containment", headers=H)
    assert resp.status_code == 404


def test_put_monitor_reenable_keeps_cursor(env):
    client, *_, store = env
    with _as("operator"):
        client.put(f"{BASE}/monitor", json={"enabled": True}, headers=H)
        store.save_cursor("rec-123", 12345)
        resp = client.put(f"{BASE}/monitor", json={"enabled": True}, headers=H)
        audit = client.get(BASE, headers=H).json()["audit"]
    assert resp.json() == {"enabled": True}
    assert store.get_monitor("rec-123") == {"enabled": True, "cursor_ms": 12345}
    assert [a["action"] for a in audit] == ["monitor-on"]


def test_contain_non_agentcore_agent_409(env):
    client, service, _, registry, _ = env
    agent = _make_agent(agent_arn=None)
    registry.get.side_effect = lambda aid: agent
    with _as("operator"):
        resp = client.post(f"{BASE}/contain", json={"reason": REASON}, headers=H)
        view = client.get(BASE, headers=H)
    assert resp.status_code == 409
    assert resp.json() == {
        "detail": "containment is available for AgentCore agents only",
        "code": "containment.not_agentcore",
    }
    assert view.status_code == 200 and view.json()["state"] is None
    service.run_contain.assert_not_awaited()


def test_restore_while_restoring_409(env):
    client, service, *_ = env
    with _as("operator"):
        client.post(f"{BASE}/contain", json={"reason": REASON}, headers=H)
    with _as("admin"):
        client.post(f"{BASE}/restore", json={"reason": "false positive, verified"}, headers=H)
        resp = client.post(f"{BASE}/restore", json={"reason": "false positive, verified"}, headers=H)
    assert resp.status_code == 409
    assert resp.json() == {"detail": "agent containment is still running", "code": "containment.job_running"}
    service.run_restore.assert_awaited_once_with("rec-123")


def test_contain_without_identity_409(env):
    client, service, *_ = env
    with patch("core.security_entra.verify_entra_token", return_value={"roles": ["Platform.Operator"]}):
        resp = client.post(f"{BASE}/contain", json={"reason": REASON}, headers=H)
    assert resp.status_code == 409
    assert resp.json() == {
        "detail": "contain requires a signed-in identity",
        "code": "containment.no_identity",
    }
    service.run_contain.assert_not_awaited()


def test_put_monitor_enable_sets_the_cursor_to_now(env):
    client, *_, store = env
    before = int(time.time() * 1000)
    with _as("operator"):
        client.put(f"{BASE}/monitor", json={"enabled": True}, headers=H)
    assert before <= store.get_monitor("rec-123")["cursor_ms"] <= int(time.time() * 1000)


ROUTE = "/agents/{agent_id}/containment"


def _route(router, method: str, path: str) -> APIRoute:
    route = next(r for r in router.routes if r.path == path and method in r.methods)
    assert isinstance(route, APIRoute)
    return route


def test_every_route_declares_its_envelope_and_keeps_every_key(env):
    import api.routes.containment as containment_module
    from models.containment import ContainmentView, MonitorState, Signal

    client, *_, store = env
    store.put_signal(
        Signal(
            agent_id="rec-123",
            session_id="sess",
            trace_id=None,
            span_id="s1",
            tool="send_email",
            label="FAIL",
            explanation="leaked",
            span_end="2026-10-13T08:00:00+00:00",
            detected_at="2026-10-13T08:00:00+00:00",
        )
    )
    with _as("operator"):
        monitor = client.put(f"{BASE}/monitor", json={"enabled": True}, headers=H)
        contained = client.post(f"{BASE}/contain", json={"reason": REASON}, headers=H)
    with _as("admin"):
        restored = client.post(f"{BASE}/restore", json={"reason": "false positive, verified"}, headers=H)
    with _as("viewer"):
        view = client.get(BASE, headers=H)
    assert [r.status_code for r in (monitor, contained, restored, view)] == [200, 202, 202, 200]
    assert set(monitor.json()) == {"enabled"}
    for body in (contained.json(), restored.json(), view.json()):
        assert set(body) == {
            "agent_id",
            "state",
            "job",
            "since",
            "by_name",
            "by_oid",
            "reason",
            "signal_at",
            "steps",
            "signals",
            "monitor",
            "audit",
            "cloudtrail_url",
        }
        assert {k for s in body["steps"] for k in s} == {"id", "status", "detail", "native_ref", "at"}
        assert {k for s in body["signals"] for k in s} == {
            "session_id",
            "trace_id",
            "span_id",
            "tool",
            "label",
            "explanation",
            "span_end",
            "detected_at",
            "source",
        }
        assert {k for a in body["audit"] for k in a} == {
            "at",
            "actor_name",
            "action",
            "step",
            "result",
            "detail",
            "native_ref",
        }
        assert set(body["monitor"]) == {"enabled"}
    router = containment_module.router
    assert _route(router, "PUT", f"{ROUTE}/monitor").response_model is MonitorState
    for method, path in (("GET", ROUTE), ("POST", f"{ROUTE}/contain"), ("POST", f"{ROUTE}/restore")):
        assert _route(router, method, path).response_model is ContainmentView
