"""RBAC + behavior tests for the Agent Registry routes (Epic 4, Task 3).

These exercise the REAL `require_role` + `current_principal` dependency path
(`AUTH_PROVIDER=entra`) against a mocked `verify_entra_token` (so no live Entra)
and a mocked `AgentRegistryService` (so no live AWS / registry).

A minimal FastAPI app including ONLY the agents router is built per test (after
the entra env is set + the auth/config modules are reset) to avoid importing the
whole app's import chain.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def reset_modules():
    """Drop cached auth/config/route modules so monkeypatched env is honored."""
    import sys
    for mod in [
        "core.rbac",
        "core.security_entra",
        "core.config",
        "api.routes.agents",
    ]:
        sys.modules.pop(mod, None)
    yield


@pytest.fixture
def entra_settings(monkeypatch):
    monkeypatch.setenv("AUTH_PROVIDER", "entra")
    monkeypatch.setenv("USE_DEV_AUTH", "False")
    monkeypatch.setenv("ENTRA_TENANT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("ENTRA_AUDIENCE", "api://agp")
    monkeypatch.setenv("ENTRA_ROLE_ADMIN", "Platform.Admin")
    monkeypatch.setenv("ENTRA_ROLE_OPERATOR", "Platform.Operator")
    monkeypatch.setenv("ENTRA_ROLE_VIEWER", "Platform.Viewer")


def _make_agent(**overrides):
    """Build a real Agent model instance for mock return values."""
    from models.agent import Agent, LifecycleState, Origin

    now = datetime.now(timezone.utc)
    base = dict(
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
    mock so no real AWS is touched. Also pre-seeds the E24 tenant-resolver
    singleton (via `api.routes.users._tenant_resolver`) with an always-global
    stub, and `api.routes.tenants._svc` with a fake `.get` that accepts ANY
    tenant_id — this file predates tenant scoping and its fixtures carry a
    fixed "default" tenant_id with no real Tenant record backing it; global
    admin (bypasses all filtering) + an always-known tenant preserve the
    pre-E24 behavior. Tenant-scoping itself is covered by
    `test_registry_tenant_scoping.py`.
    """
    import api.routes.agents as agents_module
    import api.routes.tenants as tenants_module
    import api.routes.users as users_module
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
    app.include_router(agents_module.router, prefix="/api/v1")
    return TestClient(app), agents_module


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


# --- VIEWER ---------------------------------------------------------------

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


# --- OPERATOR -------------------------------------------------------------

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
    mock_svc.submit_for_approval.return_value = _make_agent(
        lifecycle_state=LifecycleState.PENDING_APPROVAL
    )
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


# --- ADMIN ----------------------------------------------------------------

def test_admin_can_transition(entra_settings):
    from models.agent import LifecycleState

    mock_svc = MagicMock()
    mock_svc.transition.return_value = _make_agent(
        lifecycle_state=LifecycleState.APPROVED
    )
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


def test_admin_bad_action_returns_400(entra_settings):
    mock_svc = MagicMock()
    mock_svc.transition.side_effect = ValueError("Unknown transition action 'frobnicate'")
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/agents/rec-123/transitions",
            json={"action": "frobnicate", "reason": "x"},
            headers=_headers(),
        )

    assert resp.status_code == 400


def test_admin_illegal_transition_returns_409(entra_settings):
    """An illegal status edge (the live DRAFT->APPROVED bug) maps to 409, not 400/500.

    IllegalTransitionError subclasses ValueError, so the 409 handler must be
    ordered BEFORE the generic ValueError->400 handler."""
    from services.agent_registry_service import IllegalTransitionError

    msg = (
        "Invalid status transition from DRAFT to APPROVED. Valid transitions from "
        "DRAFT: PENDING_APPROVAL, DEPRECATED, DRAFT, UPDATING"
    )
    mock_svc = MagicMock()
    mock_svc.transition.side_effect = IllegalTransitionError(msg)
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/agents/rec-123/transitions",
            json={"action": "approve", "reason": "ok"},
            headers=_headers(),
        )

    assert resp.status_code == 409
    assert "Invalid status transition" in resp.json()["detail"]


# --- Error mapping --------------------------------------------------------

def test_create_duplicate_name_returns_409(entra_settings):
    from services.agent_registry_service import NameTakenError

    mock_svc = MagicMock()
    mock_svc.create.side_effect = NameTakenError("Agent name 'dup' is already in use")
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/agents", json={"name": "dup", "tenant_id": "default"}, headers=_headers()
        )

    assert resp.status_code == 409


def test_get_missing_returns_404(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = None
    client, _ = _build_client(mock_svc)

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/agents/rec-missing", headers=_headers())

    assert resp.status_code == 404


# --- created_by from principal -------------------------------------------

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


# ===========================================================================
# E36/T16 — DELETE /agents/{id} tears down what a REGISTERED-EXTERNAL agent owns.
#
# Every teardown in the platform hung off the repo-delete cascade, which an agent registered
# through POST /agents never has — so deleting its record orphaned its Entra app/SP, its
# `agp-agent-obo-{id}` Token Vault entry and its Langfuse project + secret, permanently
# (followup-lanes research Q2 §3, the registered-external orphan table).
#
# The contract pinned here: the cascade runs BEFORE the record delete (the ids live on the
# record), an agent a REPOSITORY owns is untouched (the repo cascade owns it — asked of the
# repository partition, because `project_id is None` also matches a pre-E27 envelope), every
# leg is best-effort and reported per resource, and no leg can block the record delete.
# ===========================================================================
def _no_repo_owner():
    """Patch context: no repository owns the agent ⇒ it really is registered-external.

    Every path through ``_teardown_registered_agent`` asks ``find_repository_by_agent_id``
    (fix round 1), so an unpatched test would build a live ``ProjectService`` (DynamoDB)."""
    import api.routes.projects as projects_module

    project_svc = MagicMock(name="ProjectService")
    project_svc.find_repository_by_agent_id.return_value = None
    return patch.object(projects_module, "get_project_service", return_value=project_svc)
def _seed_project_resolver():
    """Seed the ONE project-resolver singleton with a global stub.

    The DELETE route resolves ``project_ctx`` per request; unseeded it would build a real
    resolver (DynamoDB + Graph). ``is_global`` keeps the E27/T5 gate a pass so these tests
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
    from models.agent import AuthType, Platform

    base = dict(
        platform=Platform.AWS_BEDROCK,
        auth_type=AuthType.ENTRA,
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
    agents_module._identity_svc = identity
    agents_module._langfuse_svc = langfuse
    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", "https://lf.example.com")

    manager = MagicMock()
    manager.attach_mock(identity.delete_obo_provider, "delete_obo_provider")
    manager.attach_mock(identity.delete_identity, "delete_identity")
    manager.attach_mock(mock_svc.delete, "delete_record")

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")), \
         _no_repo_owner():
        resp = client.delete("/api/v1/agents/rec-123", headers=_headers())

    assert resp.status_code == 200
    # The Token Vault entry is its OWN leg and goes FIRST — the Entra app is the thing whose
    # disappearance makes it dangling — and `include_obo_provider=False` stops the identity
    # leg issuing a second delete for the resource this leg already reported.
    identity.delete_obo_provider.assert_awaited_once_with("rec-123")
    identity.delete_identity.assert_awaited_once_with(agent, include_obo_provider=False)
    assert [c[0] for c in manager.mock_calls] == [
        "delete_obo_provider",
        "delete_identity",
        "delete_record",
    ]
    # Langfuse: project + the per-agent secret, account-resolved inside the provisioner.
    langfuse.delete_agent_project.assert_called_once_with(agent)
    # ...and the record still goes, last.
    mock_svc.delete.assert_called_once_with("rec-123")


def test_delete_project_owned_agent_keeps_todays_record_only_behaviour(entra_settings, monkeypatch):
    """A materialized agent's teardown is the OWNER-gated repo cascade's job — it reclaims
    the runtime, image, exec role and TF state too, from an operator-selected item set.
    Running this cascade as well would double-delete and split the report."""
    import asyncio

    client, agents_module = _build_client(MagicMock())
    identity, langfuse = _cascade_doubles()
    agents_module._identity_svc = identity
    agents_module._langfuse_svc = langfuse
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


def test_teardown_skips_an_agent_a_REPOSITORY_owns_even_with_no_project_id(
    entra_settings, monkeypatch
):
    """``project_id is None`` is NOT "registered-external" (fix round 1): it also matches every
    pre-E27 envelope, whose repo, runtime and image are alive. Tearing that agent's identity and
    observability down from this MAINTAINER-gated route would leave a live agent without an
    Entra app while bypassing the OWNER gate its other resources sit behind. So the repository
    partition is asked, and it — not the envelope field — decides."""
    import asyncio

    import api.routes.projects as projects_module

    client, agents_module = _build_client(MagicMock())
    identity, langfuse = _cascade_doubles()
    agents_module._identity_svc = identity
    agents_module._langfuse_svc = langfuse
    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", "https://lf.example.com")

    project_svc = MagicMock(name="ProjectService")
    project_svc.find_repository_by_agent_id.return_value = MagicMock(name="Repository")

    agent = _registered_agent()  # project_id is None — the pre-E27 shape
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

    client, agents_module = _build_client(MagicMock())
    identity, langfuse = _cascade_doubles()
    agents_module._identity_svc = identity
    agents_module._langfuse_svc = langfuse
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
    agents_module._identity_svc = identity
    agents_module._langfuse_svc = langfuse
    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", "https://lf.example.com")

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")), \
         _no_repo_owner():
        resp = client.delete("/api/v1/agents/rec-123", headers=_headers())

    assert resp.status_code == 200
    # EVERY leg was attempted — one failure must not skip the next resource.
    identity.delete_obo_provider.assert_awaited_once()
    identity.delete_identity.assert_awaited_once()
    langfuse.delete_agent_project.assert_called_once()
    mock_svc.delete.assert_called_once_with("rec-123")


def test_teardown_report_uses_the_cascade_vocabulary(entra_settings, monkeypatch):
    import asyncio

    from services.langfuse_provisioning import LangfuseAccountUnresolvedError

    client, agents_module = _build_client(MagicMock())
    identity, langfuse = _cascade_doubles()
    langfuse.delete_agent_project.side_effect = LangfuseAccountUnresolvedError(
        "no tenant stage owns the account holding the runtime"
    )
    agents_module._identity_svc = identity
    agents_module._langfuse_svc = langfuse
    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", "https://lf.example.com")

    with _no_repo_owner():
        items = asyncio.run(agents_module._teardown_registered_agent(_registered_agent()))

    # T8's honest vocabulary: an unresolvable owning account is reported FAILED, never
    # "deleted", and it carries the `stage_unresolved:` PREFIX plus the safe reason the
    # provisioner built — `LangfuseAccountUnresolvedError` IS a `StageUnresolvedError`, so one
    # `except` arm renders it identically here and in the repo cascade. Never an account id.
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

    client, agents_module = _build_client(MagicMock())
    identity, langfuse = _cascade_doubles()
    identity.delete_obo_provider.side_effect = RuntimeError("agentcore 500")
    agents_module._identity_svc = identity
    agents_module._langfuse_svc = langfuse
    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", "")

    with _no_repo_owner():
        items = asyncio.run(agents_module._teardown_registered_agent(_registered_agent()))

    assert [(i.item, i.outcome, i.reason) for i in items] == [
        ("obo_provider", "failed", "RuntimeError"),
        ("identity", "deleted", None),
        ("langfuse", "skipped", None),
    ]


def test_teardown_reports_a_cross_account_assume_failure_with_T8s_prefix(
    entra_settings, monkeypatch
):
    """The langfuse leg PROPAGATES `TenantCredentialsError` by design (falling back to ambient
    is the defect), so the identical failure must read `assume_role_failed: <role>` here and on
    the repo path — the bare type name names neither the role nor the hop."""
    import asyncio

    from services.tenant_credentials import TenantCredentialsError

    client, agents_module = _build_client(MagicMock())
    identity, langfuse = _cascade_doubles()
    langfuse.delete_agent_project.side_effect = TenantCredentialsError(
        "agp-deployment-acme-prod"
    )
    agents_module._identity_svc = identity
    agents_module._langfuse_svc = langfuse
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

    client, agents_module = _build_client(MagicMock())
    identity, langfuse = _cascade_doubles()
    agents_module._identity_svc = identity
    agents_module._langfuse_svc = langfuse
    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", "")

    agent = _make_agent()  # no identity fields at all
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


def test_delete_missing_agent_has_no_teardown_side_effects(entra_settings, monkeypatch):
    """The visibility gate runs FIRST: a missing (or foreign-tenant) agent 404s without
    touching Graph, the Token Vault or Langfuse."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = None
    client, agents_module = _build_client(mock_svc)
    _seed_project_resolver()
    identity, langfuse = _cascade_doubles()
    agents_module._identity_svc = identity
    agents_module._langfuse_svc = langfuse
    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", "https://lf.example.com")

    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")), \
         _no_repo_owner():
        resp = client.delete("/api/v1/agents/rec-missing", headers=_headers())

    assert resp.status_code == 404
    identity.delete_obo_provider.assert_not_awaited()
    identity.delete_identity.assert_not_awaited()
    langfuse.delete_agent_project.assert_not_called()
    mock_svc.delete.assert_not_called()
