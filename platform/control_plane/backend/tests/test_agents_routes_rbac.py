"""RBAC + behavior tests for the Agent Registry routes.

These exercise the REAL `require_role` + `current_principal` dependency path
(`AUTH_PROVIDER=entra`) against a mocked `verify_entra_token` (so no live Entra)
and a mocked `AgentRegistryService` (so no live AWS / registry).

A minimal FastAPI app including ONLY the agents router is built per test (after
the entra env is set + the auth/config modules are reset) to avoid importing the
whole app's import chain.
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("reset_modules")

_FAKE_TOKEN = "ghs_" + "a" * 36


def _make_agent(**overrides):
    """Build a real Agent model instance for mock return values."""
    from models.agent import Agent, LifecycleState, Origin

    now = datetime.now(timezone.utc)
    base: dict[str, Any] = dict(
        id="rec-123",
        name="claims-triage-de",
        purpose="Triage claims",
        lifecycle_state=LifecycleState.PROPOSED,
        origin=Origin.REGISTERED,
        created_at=now,
        updated_at=now,
        created_by="maria.bauer@example.com",
    )
    base.update(overrides)
    return Agent(**base)


def _build_client(mock_svc):
    """Build a minimal app with ONLY the agents router + a mocked service.

    Must be called AFTER the entra env fixture so the route module imports with
    the right settings. Patches the route module's `get_service` to return the
    mock so no real AWS is touched. Also pre-seeds the tenant-resolver
    singleton (via `api.routes.users._tenant_resolver`) with an always-global
    stub, and `api.routes.tenants._svc` with a fake `.get` that accepts ANY
    tenant_id — this file predates tenant scoping and its fixtures carry a
    fixed "default" tenant_id with no real Tenant record backing it; global
    admin (bypasses all filtering) + an always-known tenant preserve the
    earlier behavior. Tenant-scoping itself is covered by
    `test_registry_tenant_scoping.py`.
    """
    import api.routes.agents as agents_module
    import api.routes.tenants as tenants_module
    import api.routes.users as users_module
    from core.errors import install_error_handlers
    from services.tenant_resolver import TenantContext

    agents_module._svc = mock_svc

    class _GlobalResolver:
        async def resolve(self, principal):
            return TenantContext(is_global=True, tenant_ids=frozenset(), tenants=())

    users_module._tenant_resolver = _GlobalResolver()

    class _AnyTenantService:
        def get(self, tenant_id):
            from unittest.mock import MagicMock

            return MagicMock(id=tenant_id)

    tenants_module._svc = _AnyTenantService()

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(agents_module.router, prefix="/api/v1")
    return TestClient(app), agents_module


def _real_registry(ctl):
    """The real ``AgentRegistryService`` over a MagicMock control client, for tests that pin what the
    service's own errors put on the wire."""
    from services.agent_registry_service import AgentRegistryService

    return AgentRegistryService(registry_id="reg-test", control_client=ctl)


def _transition(client, body):
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        return client.post("/api/v1/agents/rec-123/transitions", json=body, headers=_headers())


def _claims_for(role: str):
    """Return entra token claims for a given platform role."""
    role_app = {
        "viewer": "Platform.Viewer",
        "operator": "Platform.Operator",
        "admin": "Platform.Admin",
    }[role]
    return {
        "oid": f"{role}-oid",
        "preferred_username": f"{role}.user@example.com",
        "roles": [role_app],
    }


def _headers():
    return {"Authorization": "Bearer fake-token"}


def test_viewer_can_list(entra_settings):
    mock_svc = MagicMock()
    mock_svc.list.return_value = [_make_agent()]
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list) and body[0]["id"] == "rec-123"


def test_viewer_cannot_create(entra_settings):
    mock_svc = MagicMock()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post("/api/v1/agents", json={"name": "x"}, headers=_headers())

    assert resp.status_code == 403


def test_viewer_cannot_transition(entra_settings):
    mock_svc = MagicMock()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/agents/rec-123/transitions",
            json={"action": "approve", "reason": "ok"},
            headers=_headers(),
        )

    assert resp.status_code == 403


def test_operator_can_create(entra_settings):
    mock_svc = MagicMock()
    mock_svc.create.return_value = _make_agent()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents",
            json={"name": "claims-triage-de", "purpose": "Triage claims", "tenant_id": "default"},
            headers=_headers(),
        )

    assert resp.status_code == 201
    assert resp.json()["id"] == "rec-123"


def test_operator_can_submit(entra_settings):
    from models.agent import LifecycleState

    mock_svc = MagicMock()
    mock_svc.submit_for_approval.return_value = _make_agent(lifecycle_state=LifecycleState.PENDING_APPROVAL)
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/agents/rec-123/submit", headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["lifecycle_state"] == "pending_approval"


def test_operator_cannot_transition(entra_settings):
    mock_svc = MagicMock()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents/rec-123/transitions",
            json={"action": "approve", "reason": "ok"},
            headers=_headers(),
        )

    assert resp.status_code == 403


def test_admin_can_transition(entra_settings):
    from models.agent import LifecycleState

    mock_svc = MagicMock()
    mock_svc.transition.return_value = _make_agent(lifecycle_state=LifecycleState.APPROVED)
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/agents/rec-123/transitions",
            json={"action": "approve", "reason": "looks good"},
            headers=_headers(),
        )

    assert resp.status_code == 200
    assert resp.json()["lifecycle_state"] == "approved"
    mock_svc.transition.assert_called_once_with("rec-123", "approve", "looks good")


def test_agents_unknown_action_is_400(entra_settings):
    """The service refuses an action outside ``TRANSITION_TO_STATUS`` before any registry call;
    the detail names the caller's own action and the valid ones."""
    ctl = MagicMock()
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent()
    mock_svc.transition.side_effect = _real_registry(ctl).transition
    client, _ = _build_client(mock_svc)

    resp = _transition(client, {"action": "frobnicate", "reason": "x"})

    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "Unknown transition action 'frobnicate'; valid actions: ['approve', 'deprecate', 'reject']"
    )
    ctl.update_registry_record_status.assert_not_called()


def test_agents_missing_reason_is_400(entra_settings):
    ctl = MagicMock()
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent()
    mock_svc.transition.side_effect = _real_registry(ctl).transition
    client, _ = _build_client(mock_svc)

    resp = _transition(client, {"action": "approve", "reason": "  "})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "A non-empty reason is required for a status transition"
    ctl.update_registry_record_status.assert_not_called()


def test_agents_illegal_transition_is_409(entra_settings):
    from services.agent_registry_service import IllegalTransitionError

    msg = (
        "Invalid status transition from DRAFT to APPROVED. Valid transitions from "
        "DRAFT: PENDING_APPROVAL, DEPRECATED, DRAFT, UPDATING"
    )
    mock_svc = MagicMock()
    mock_svc.transition.side_effect = IllegalTransitionError(msg)
    client, _ = _build_client(mock_svc)

    resp = _transition(client, {"action": "approve", "reason": "ok"})

    assert resp.status_code == 409
    assert resp.json()["detail"] == msg


def test_agent_error_detail_for_illegal_transition_carries_no_upstream_body(entra_settings):
    """The registry's ValidationException message is logged, never sent: the detail is composed
    from the validated action alone."""
    ctl = MagicMock()
    ctl.update_registry_record_status.side_effect = ClientError(
        {
            "Error": {
                "Code": "ValidationException",
                "Message": f"Invalid status transition from DRAFT to APPROVED by {_FAKE_TOKEN}",
            }
        },
        "UpdateRegistryRecordStatus",
    )
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent()
    mock_svc.transition.side_effect = _real_registry(ctl).transition
    client, _ = _build_client(mock_svc)

    resp = _transition(client, {"action": "approve", "reason": "ok"})

    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "Invalid status transition: 'approve' is not allowed from the agent's current state"
    )
    assert _FAKE_TOKEN not in resp.text
    assert "DRAFT" not in resp.text


def test_agents_name_taken_is_409(entra_settings):
    from services.agent_registry_service import NameTakenError

    mock_svc = MagicMock()
    mock_svc.create.side_effect = NameTakenError("Agent name 'dup' is already in use")
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/agents", json={"name": "dup", "tenant_id": "default"}, headers=_headers())

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Agent name 'dup' is already in use"


def test_agent_error_detail_for_name_taken_carries_no_upstream_body(entra_settings):
    """The name pre-check's registry listing never reaches the detail; only the requested name does."""
    ctl = MagicMock()
    ctl.list_registry_records.return_value = {
        "registryRecords": [{"recordId": "rec-dup", "name": "dup", "description": _FAKE_TOKEN}]
    }
    client, _ = _build_client(_real_registry(ctl))

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/agents", json={"name": "dup", "tenant_id": "default"}, headers=_headers())

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Agent name 'dup' is already in use"
    assert _FAKE_TOKEN not in resp.text
    ctl.create_registry_record.assert_not_called()


def test_agents_malformed_record_is_502(entra_settings):
    """A record whose envelope cannot be parsed is a fixed 502, never the parse error and never a
    generic 500."""
    from services.agent_registry_service import MalformedAgentRecordError

    mock_svc = MagicMock()
    mock_svc.get.side_effect = MalformedAgentRecordError(
        f"Registry record 'rec-123' has no well-formed governance envelope: {_FAKE_TOKEN}"
    )
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/rec-123", headers=_headers())

    assert resp.status_code == 502
    assert resp.json() == {
        "detail": "the agent's registry record could not be read",
        "code": "agent.malformed_record",
    }
    assert _FAKE_TOKEN not in resp.text


def test_get_missing_returns_404(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = None
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/rec-missing", headers=_headers())

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Agent not found", "code": "agent.not_found"}


@pytest.mark.parametrize(
    ("method", "path", "body", "role", "write"),
    [
        ("put", "/api/v1/agents/rec-123", {"purpose": "x"}, "operator", "update"),
        ("post", "/api/v1/agents/rec-123/submit", None, "operator", "submit_for_approval"),
        (
            "post",
            "/api/v1/agents/rec-123/transitions",
            {"action": "approve", "reason": "ok"},
            "admin",
            "transition",
        ),
        ("put", "/api/v1/agents/rec-123/publish", {"published": True}, "operator", "set_published"),
    ],
)
def test_a_write_that_finds_no_record_is_the_agent_404(entra_settings, method, path, body, role, write):
    """The record vanished between the visibility load and the write: the missing agent's 404."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent()
    getattr(mock_svc, write).return_value = None
    client, _ = _build_client(mock_svc)
    _seed_project_resolver()

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for(role)):
        resp = client.request(method, path, json=body, headers=_headers())

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Agent not found", "code": "agent.not_found"}


@pytest.mark.parametrize("query", ["lifecycle_state=bogus", "platform=bogus"])
def test_an_unknown_list_filter_is_fastapis_422(entra_settings, query):
    mock_svc = MagicMock()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get(f"/api/v1/agents?{query}", headers=_headers())

    assert resp.status_code == 422
    body = resp.json()
    assert set(body) == {"detail"}
    assert body["detail"][0]["loc"] == ["query", query.partition("=")[0]]
    assert body["detail"][0]["type"] == "enum"
    mock_svc.list.assert_not_called()


def test_a_known_list_filter_reaches_the_service_as_its_enum(entra_settings):
    from models.agent import LifecycleState, Platform

    mock_svc = MagicMock()
    mock_svc.list.return_value = []
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents?lifecycle_state=approved&platform=aws_bedrock", headers=_headers())

    assert resp.status_code == 200
    mock_svc.list.assert_called_once_with(
        lifecycle_state=LifecycleState.APPROVED,
        sponsor_oid=None,
        business_unit=None,
        region=None,
        platform=Platform.AWS_BEDROCK,
    )


def test_create_under_a_tenant_outside_the_callers_memberships_is_403(entra_settings):
    import api.routes.users as users_module
    from services.tenant_resolver import TenantContext

    mock_svc = MagicMock()
    client, _ = _build_client(mock_svc)

    class _MemberResolver:
        async def resolve(self, principal):
            return TenantContext(is_global=False, tenant_ids=frozenset({"ten-mine"}), tenants=())

    users_module._tenant_resolver = _MemberResolver()

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/agents", json={"name": "x", "tenant_id": "ten-other"}, headers=_headers())

    assert resp.status_code == 403
    assert resp.json() == {"detail": "tenant not permitted", "code": "tenant.not_permitted"}
    mock_svc.create.assert_not_called()


def test_create_with_a_platform_its_tenant_does_not_run_is_409(entra_settings):
    import api.routes.tenants as tenants_module
    from models.tenant import TenantPlatform

    mock_svc = MagicMock()
    client, _ = _build_client(mock_svc)
    tenants_module._svc = MagicMock(
        get=MagicMock(return_value=MagicMock(id="ten-dbx", platform=TenantPlatform.DATABRICKS))
    )

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents",
            json={"name": "x", "tenant_id": "ten-dbx", "platform": "aws_bedrock"},
            headers=_headers(),
        )

    assert resp.status_code == 409
    assert resp.json() == {
        "detail": "agent platform does not match the tenant's platform",
        "code": "agent.platform_tenant_mismatch",
    }
    mock_svc.create.assert_not_called()


def test_reprovision_of_an_ungoverned_agent_is_409(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent()
    client, _ = _build_client(mock_svc)
    _seed_project_resolver()

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post("/api/v1/agents/rec-123/reprovision", headers=_headers())

    assert resp.status_code == 409
    assert resp.json() == {"detail": "not a governed agent", "code": "agent.not_governed"}
    mock_svc.persist_identity.assert_not_called()


def test_create_uses_principal_as_created_by(entra_settings):
    """POST /agents as operator must pass created_by == the principal's email,
    NOT a hardcoded 'user'."""
    maria_claims = {
        "oid": "maria-oid",
        "preferred_username": "maria.bauer@example.com",
        "roles": ["Platform.Operator"],
    }
    mock_svc = MagicMock()
    mock_svc.create.return_value = _make_agent()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=maria_claims):
        resp = client.post(
            "/api/v1/agents",
            json={"name": "claims-triage-de", "purpose": "Triage claims", "tenant_id": "default"},
            headers=_headers(),
        )

    assert resp.status_code == 201
    _, kwargs = mock_svc.create.call_args
    assert kwargs.get("created_by") == "maria.bauer@example.com"


def test_create_defaults_sponsor_to_principal(entra_settings):
    """When sponsor_* is blank, the create payload should default to the creator."""
    maria_claims = {
        "oid": "maria-oid",
        "preferred_username": "maria.bauer@example.com",
        "roles": ["Platform.Operator"],
    }
    mock_svc = MagicMock()
    mock_svc.create.return_value = _make_agent()
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=maria_claims):
        resp = client.post(
            "/api/v1/agents",
            json={"name": "claims-triage-de", "tenant_id": "default"},
            headers=_headers(),
        )

    assert resp.status_code == 201
    args, _ = mock_svc.create.call_args
    req = args[0]
    assert req.sponsor_email == "maria.bauer@example.com"
    assert req.sponsor_oid == "maria-oid"


def _no_repo_owner():
    """Patch context: no repository owns the agent ⇒ it really is registered-external.

     Every path through ``_teardown_registered_agent`` asks ``find_repository_by_agent_id``
    , so an unpatched test would build a live ``ProjectService`` (DynamoDB)."""
    import api.routes.projects as projects_module

    project_svc = MagicMock(name="ProjectService")
    project_svc.find_repository_by_agent_id.return_value = None
    return patch.object(projects_module, "get_project_service", return_value=project_svc)


def _seed_project_resolver():
    """Seed the ONE project-resolver singleton with a global stub.

    The DELETE route resolves ``project_ctx`` per request; unseeded it would build a real
    resolver (DynamoDB + Graph). ``is_global`` keeps the project-role gate a pass so these tests
    exercise the cascade, not the gate (gating has its own suite)."""
    import api.routes.users as users_module
    from services.project_resolver import ProjectContext

    class _GlobalProjectResolver:
        async def resolve(self, principal):
            return ProjectContext(is_global=True, roles={})

    users_module._project_resolver = _GlobalProjectResolver()


def _registered_agent(**overrides):
    """A registered-external agent: no ``project_id`` (``AgentCreate`` carries none), with
    every artifact the orphan table names."""
    from models.agent import AgentAuthType, Platform

    base: dict[str, Any] = dict(
        platform=Platform.AWS_BEDROCK,
        auth_type=AgentAuthType.ENTRA,
        agent_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/rec-123",
        entra_app_id="agent-app-guid",
        entra_sp_id="agent-sp-objid",
        oauth2_credential_provider_name="agp-agent-obo-rec-123",
        langfuse_project_id="clx-live",
        langfuse_key_secret_name="langfuse-agent-rec-123-keys",
    )
    base.update(overrides)
    return _make_agent(**base)


def _cascade_doubles():
    """(identity, langfuse) doubles. ``delete_identity`` / ``delete_obo_provider`` are async
    (the real ones await Graph and the control plane); ``delete_agent_project`` is SYNC (it is
    off-loaded to a worker thread by the route)."""
    from unittest.mock import AsyncMock

    identity = MagicMock(name="AgentIdentityService")
    identity.delete_identity = AsyncMock(return_value=None)
    identity.delete_obo_provider = AsyncMock(return_value=None)
    langfuse = MagicMock(name="LangfuseProvisioningService")
    langfuse.delete_agent_project.return_value = None
    return identity, langfuse


def test_delete_registered_agent_tears_down_identity_and_langfuse(entra_settings, monkeypatch):
    agent = _registered_agent()
    mock_svc = MagicMock()
    mock_svc.get.return_value = agent
    mock_svc.delete.return_value = agent
    client, agents_module = _build_client(mock_svc)
    _seed_project_resolver()
    identity, langfuse = _cascade_doubles()
    monkeypatch.setattr(agents_module, "_identity_svc", identity)
    monkeypatch.setattr(agents_module, "_langfuse_svc", langfuse)
    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", "https://lf.example.com")

    manager = MagicMock()
    manager.attach_mock(identity.delete_obo_provider, "delete_obo_provider")
    manager.attach_mock(identity.delete_identity, "delete_identity")
    manager.attach_mock(mock_svc.delete, "delete_record")

    with (
        patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")),
        _no_repo_owner(),
    ):
        resp = client.delete("/api/v1/agents/rec-123", headers=_headers())

    assert resp.status_code == 200
    identity.delete_obo_provider.assert_awaited_once_with("rec-123")
    identity.delete_identity.assert_awaited_once_with(agent, include_obo_provider=False)
    assert [c[0] for c in manager.mock_calls] == [
        "delete_obo_provider",
        "delete_identity",
        "delete_record",
    ]
    langfuse.delete_agent_project.assert_called_once_with(agent)
    mock_svc.delete.assert_called_once_with("rec-123")


def test_delete_project_owned_agent_keeps_todays_record_only_behaviour(entra_settings, monkeypatch):
    """A materialized agent's teardown is the OWNER-gated repo cascade's job — it reclaims
    the runtime, image, exec role and TF state too, from an operator-selected item set.
    Running this cascade as well would double-delete and split the report."""
    import asyncio

    _client, agents_module = _build_client(MagicMock())
    identity, langfuse = _cascade_doubles()
    monkeypatch.setattr(agents_module, "_identity_svc", identity)
    monkeypatch.setattr(agents_module, "_langfuse_svc", langfuse)
    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", "https://lf.example.com")

    agent = _registered_agent(project_id="proj-1")
    with _no_repo_owner():
        items = asyncio.run(agents_module._teardown_registered_agent(agent))

    assert [(i.item, i.outcome) for i in items] == [
        ("obo_provider", "skipped"),
        ("identity", "skipped"),
        ("langfuse", "skipped"),
    ]
    identity.delete_obo_provider.assert_not_awaited()
    identity.delete_identity.assert_not_awaited()
    langfuse.delete_agent_project.assert_not_called()


def test_teardown_skips_an_agent_a_REPOSITORY_owns_even_with_no_project_id(entra_settings, monkeypatch):
    """``project_id is None`` is NOT "registered-external": it also matches every
    project-less envelope, whose repo, runtime and image are alive. Tearing that agent's identity and
    observability down from this MAINTAINER-gated route would leave a live agent without an
    Entra app while bypassing the OWNER gate its other resources sit behind. So the repository
    partition is asked, and it — not the envelope field — decides."""
    import asyncio

    import api.routes.projects as projects_module

    _client, agents_module = _build_client(MagicMock())
    identity, langfuse = _cascade_doubles()
    monkeypatch.setattr(agents_module, "_identity_svc", identity)
    monkeypatch.setattr(agents_module, "_langfuse_svc", langfuse)
    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", "https://lf.example.com")

    project_svc = MagicMock(name="ProjectService")
    project_svc.find_repository_by_agent_id.return_value = MagicMock(name="Repository")

    agent = _registered_agent()
    with patch.object(projects_module, "get_project_service", return_value=project_svc):
        items = asyncio.run(agents_module._teardown_registered_agent(agent))

    assert [(i.item, i.outcome) for i in items] == [
        ("obo_provider", "skipped"),
        ("identity", "skipped"),
        ("langfuse", "skipped"),
    ]
    project_svc.find_repository_by_agent_id.assert_called_once_with("rec-123")
    identity.delete_obo_provider.assert_not_awaited()
    identity.delete_identity.assert_not_awaited()
    langfuse.delete_agent_project.assert_not_called()


def test_teardown_skips_every_leg_when_repository_ownership_cannot_be_established(
    entra_settings, monkeypatch
):
    """An unanswerable ownership question must not authorize an irreversible teardown — and
    must not 500 the delete either. Fails CLOSED: nothing is torn down, the record still goes,
    and re-running the delete reclaims a genuinely registered agent's orphans."""
    import asyncio

    import api.routes.projects as projects_module

    _client, agents_module = _build_client(MagicMock())
    identity, langfuse = _cascade_doubles()
    monkeypatch.setattr(agents_module, "_identity_svc", identity)
    monkeypatch.setattr(agents_module, "_langfuse_svc", langfuse)
    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", "https://lf.example.com")

    project_svc = MagicMock(name="ProjectService")
    project_svc.find_repository_by_agent_id.side_effect = RuntimeError("dynamodb throttled")

    with patch.object(projects_module, "get_project_service", return_value=project_svc):
        items = asyncio.run(agents_module._teardown_registered_agent(_registered_agent()))

    assert [i.outcome for i in items] == ["skipped", "skipped", "skipped"]
    identity.delete_obo_provider.assert_not_awaited()
    identity.delete_identity.assert_not_awaited()
    langfuse.delete_agent_project.assert_not_called()


def test_delete_registered_agent_reports_each_leg_and_never_blocks_the_record(entra_settings, monkeypatch):
    """A failed leg is REPORTED with a SAFE reason (the exception type) — and the record is
    still deleted: it is the only pointer an operator has, both legs are idempotent, and a
    500 here would trap the row behind the orphans it was trying to reclaim."""
    agent = _registered_agent()
    mock_svc = MagicMock()
    mock_svc.get.return_value = agent
    mock_svc.delete.return_value = agent
    client, agents_module = _build_client(mock_svc)
    _seed_project_resolver()
    identity, langfuse = _cascade_doubles()
    identity.delete_obo_provider.side_effect = RuntimeError("agentcore 500")
    identity.delete_identity.side_effect = RuntimeError("graph 403")
    langfuse.delete_agent_project.side_effect = ValueError("langfuse unreachable")
    monkeypatch.setattr(agents_module, "_identity_svc", identity)
    monkeypatch.setattr(agents_module, "_langfuse_svc", langfuse)
    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", "https://lf.example.com")

    with (
        patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")),
        _no_repo_owner(),
    ):
        resp = client.delete("/api/v1/agents/rec-123", headers=_headers())

    assert resp.status_code == 200
    identity.delete_obo_provider.assert_awaited_once()
    identity.delete_identity.assert_awaited_once()
    langfuse.delete_agent_project.assert_called_once()
    mock_svc.delete.assert_called_once_with("rec-123")


def test_teardown_report_uses_the_cascade_vocabulary(entra_settings, monkeypatch):
    import asyncio

    from services.langfuse_provisioning import LangfuseAccountUnresolvedError

    _client, agents_module = _build_client(MagicMock())
    identity, langfuse = _cascade_doubles()
    langfuse.delete_agent_project.side_effect = LangfuseAccountUnresolvedError(
        "no tenant stage owns the account holding the runtime"
    )
    monkeypatch.setattr(agents_module, "_identity_svc", identity)
    monkeypatch.setattr(agents_module, "_langfuse_svc", langfuse)
    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", "https://lf.example.com")

    with _no_repo_owner():
        items = asyncio.run(agents_module._teardown_registered_agent(_registered_agent()))

    assert [(i.item, i.outcome, i.reason) for i in items] == [
        ("obo_provider", "deleted", None),
        ("identity", "deleted", None),
        (
            "langfuse",
            "failed",
            "stage_unresolved: no tenant stage owns the account holding the runtime",
        ),
    ]


def test_teardown_reports_the_vault_entry_as_its_own_item(entra_settings, monkeypatch):
    """A surviving Token Vault entry can no longer hide behind ``identity: deleted``.

    The provider deletion used to ride the ``identity`` leg and be swallowed there (correct for
    the repo cascade, whose ``identity`` item is BLOCKING), so this route could report the Entra
    app AND the vault entry as deleted while the entry — a dangling clientId/clientSecret —
    survived. Nothing is blocking here, so it gets its own honest line-item."""
    import asyncio

    _client, agents_module = _build_client(MagicMock())
    identity, langfuse = _cascade_doubles()
    identity.delete_obo_provider.side_effect = RuntimeError("agentcore 500")
    monkeypatch.setattr(agents_module, "_identity_svc", identity)
    monkeypatch.setattr(agents_module, "_langfuse_svc", langfuse)
    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", "")

    with _no_repo_owner():
        items = asyncio.run(agents_module._teardown_registered_agent(_registered_agent()))

    assert [(i.item, i.outcome, i.reason) for i in items] == [
        ("obo_provider", "failed", "RuntimeError"),
        ("identity", "deleted", None),
        ("langfuse", "skipped", None),
    ]


def test_teardown_reports_a_cross_account_assume_failure_with_T8s_prefix(entra_settings, monkeypatch):
    """The langfuse leg PROPAGATES `TenantCredentialsError` by design (falling back to ambient
    is the defect), so the identical failure must read `assume_role_failed: <role>` here and on
    the repo path — the bare type name names neither the role nor the hop."""
    import asyncio

    from services.tenant_credentials import TenantCredentialsError

    _client, agents_module = _build_client(MagicMock())
    identity, langfuse = _cascade_doubles()
    langfuse.delete_agent_project.side_effect = TenantCredentialsError("agp-deployment-acme-prod")
    monkeypatch.setattr(agents_module, "_identity_svc", identity)
    monkeypatch.setattr(agents_module, "_langfuse_svc", langfuse)
    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", "https://lf.example.com")

    with _no_repo_owner():
        items = asyncio.run(agents_module._teardown_registered_agent(_registered_agent()))

    assert items[-1].item == "langfuse"
    assert (items[-1].outcome, items[-1].reason) == (
        "failed",
        "assume_role_failed: agp-deployment-acme-prod",
    )


def test_teardown_skips_every_leg_for_a_metadata_only_agent(entra_settings, monkeypatch):
    """Nothing was ever minted (no Entra ids, no provider name) and Langfuse is unconfigured
    ⇒ three explicit ``skipped`` items and not a single AWS/Graph call."""
    import asyncio

    _client, agents_module = _build_client(MagicMock())
    identity, langfuse = _cascade_doubles()
    monkeypatch.setattr(agents_module, "_identity_svc", identity)
    monkeypatch.setattr(agents_module, "_langfuse_svc", langfuse)
    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", "")

    agent = _make_agent()
    with _no_repo_owner():
        items = asyncio.run(agents_module._teardown_registered_agent(agent))

    assert [(i.item, i.outcome) for i in items] == [
        ("obo_provider", "skipped"),
        ("identity", "skipped"),
        ("langfuse", "skipped"),
    ]
    identity.delete_obo_provider.assert_not_awaited()
    identity.delete_identity.assert_not_awaited()
    langfuse.delete_agent_project.assert_not_called()


def test_delete_refuses_a_blocking_artifact_no_adapter_can_remove_500(entra_settings, monkeypatch):
    """A Databricks record naming a federation audience on a deployment with no Databricks
    collaborator: nothing can withdraw the audience, so the delete is refused and the record,
    the only pointer to that audience, survives."""
    from models.agent import AgentAuthType, Platform

    agent = _make_agent(
        platform=Platform.DATABRICKS,
        auth_type=AgentAuthType.ENTRA,
        binding_mode="federation",
        runtime_handle="https://claims.example.databricksapps.com",
        runtime_kind="app",
        entra_app_id="agent-app-guid",
        entra_app_audience="api://agp-agent-rec-123",
    )
    mock_svc = MagicMock()
    mock_svc.get.return_value = agent
    client, agents_module = _build_client(mock_svc)
    _seed_project_resolver()
    monkeypatch.setattr(agents_module, "_identity_svc", MagicMock())
    monkeypatch.setattr(agents_module.settings, "TENANTS_TABLE_NAME", "")

    with (
        patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")),
        _no_repo_owner(),
    ):
        resp = client.delete("/api/v1/agents/rec-123", headers=_headers())

    assert resp.status_code == 500
    assert resp.json()["detail"] == (
        "the agent's Databricks bindings cannot be removed on this deployment, so it was not deleted"
    )
    mock_svc.delete.assert_not_called()


def test_delete_missing_agent_has_no_teardown_side_effects(entra_settings, monkeypatch):
    """The visibility gate runs FIRST: a missing (or foreign-tenant) agent 404s without
    touching Graph, the Token Vault or Langfuse."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = None
    client, agents_module = _build_client(mock_svc)
    _seed_project_resolver()
    identity, langfuse = _cascade_doubles()
    monkeypatch.setattr(agents_module, "_identity_svc", identity)
    monkeypatch.setattr(agents_module, "_langfuse_svc", langfuse)
    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", "https://lf.example.com")

    with (
        patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")),
        _no_repo_owner(),
    ):
        resp = client.delete("/api/v1/agents/rec-missing", headers=_headers())

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Agent not found", "code": "agent.not_found"}
    identity.delete_obo_provider.assert_not_awaited()
    identity.delete_identity.assert_not_awaited()
    langfuse.delete_agent_project.assert_not_called()
    mock_svc.delete.assert_not_called()


def test_delete_whose_record_is_gone_after_teardown_is_the_agent_404(entra_settings, monkeypatch):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent()
    mock_svc.delete.return_value = None
    client, agents_module = _build_client(mock_svc)
    _seed_project_resolver()
    identity, langfuse = _cascade_doubles()
    monkeypatch.setattr(agents_module, "_identity_svc", identity)
    monkeypatch.setattr(agents_module, "_langfuse_svc", langfuse)
    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", "")

    with (
        patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")),
        _no_repo_owner(),
    ):
        resp = client.delete("/api/v1/agents/rec-123", headers=_headers())

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Agent not found", "code": "agent.not_found"}
    mock_svc.delete.assert_called_once_with("rec-123")


def test_the_five_per_agent_reads_declare_their_response_models(entra_settings):
    import api.routes.agents as agents_module
    from models.eval import EvalRuns
    from models.repository import AgentRepositoryView
    from services.langfuse_metrics_service import AgentMetrics, TracePage

    def declared(suffix):
        route = next(r for r in agents_module.router.routes if getattr(r, "path", "").endswith(suffix))
        assert isinstance(route, APIRoute)
        return route.response_model

    assert declared("/{agent_id}/evals/runs") is EvalRuns
    assert declared("/{agent_id}/invoke") is agents_module.InvokeResult
    assert declared("/{agent_id}/metrics") is AgentMetrics
    assert declared("/{agent_id}/repository") is AgentRepositoryView
    assert declared("/{agent_id}/traces") is TracePage


def test_the_route_bodies_reach_the_document_under_their_own_names(entra_settings):
    from main import app

    schemas = app.openapi()["components"]["schemas"]
    assert "api__routes__agents__TransitionRequest" not in schemas
    assert "models__transition__TransitionRequest" not in schemas
    assert sorted(schemas["TransitionRequest"]["properties"]) == ["action", "reason"]
    assert "PublishRequest-Input" not in schemas and "PublishRequest-Output" not in schemas
    assert sorted(schemas["PublishToggle"]["properties"]) == ["published"]
    assert "PublishRequest" in schemas
    assert [name for name in schemas if "__" in name] == []
