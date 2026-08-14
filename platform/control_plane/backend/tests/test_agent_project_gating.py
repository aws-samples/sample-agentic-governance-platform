"""Project-gated agent mutation (Epic 27, Task T5).

Closes the agent-mutation bypass: before T5, ``PUT /agents/{id}`` and
``POST /agents/{id}/reprovision`` were only TENANT-gated, so any tenant member could
mutate an agent belonging to someone else's project — routing straight around the
per-project role T4 enforces on the project routes.

Mirrors ``test_project_roles_routes.py``: the autouse module reset, the entra env
fixture, FAKE resolvers patched onto the ``users`` module globals (the ONE resolver
singletons — an UNSEEDED ``_project_resolver`` would build a REAL ``ProjectResolver`` +
``GraphService`` and attempt live ``login.microsoftonline.com`` calls), and the services
as ``MagicMock``s on the route-module globals. NO ``dependency_overrides``.

What this pins:
  - ``Agent.project_id`` is ADDITIVE and optional — an old record (``None``) keeps its
    pre-T5 behaviour (tenant-gated only) and round-trips through the envelope;
  - ``project_id`` is SERVER-STAMPED ONLY — it is not a field on ``AgentCreate``, so a
    ``POST /agents`` body cannot plant a new agent into someone else's project;
  - an agent WITH a ``project_id`` needs MAINTAINER on that project for all FOUR
    envelope mutations — ``PUT`` / ``reprovision`` / ``DELETE`` / ``publish`` — with the
    T4 403 literal;
  - the gate runs AFTER ``_load_visible_agent`` and BEFORE any service mutation, so a
    PRESENT-but-foreign-tenant agent 404s byte-identically to a missing one (never a 403,
    which would be an existence oracle);
  - the ``project_id is None`` short-circuit fires AT THE SEAM — the role store is never
    consulted for an unparented agent;
  - the registry LIFECYCLE routes (``/submit``, ``/transitions``) are NOT project-gated
    (design §6 non-overlap — a lifecycle decision is a platform-ADMIN act owned by the
    AWS Agent Registry, not a project act);
  - T4's fail direction is inherited verbatim: the design-§3 ungoverned-project fallback
    applies at MAINTAINER level, and an UNREADABLE role partition fails CLOSED.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

FIXED_TS = "2026-07-27T00:00:00+00:00"
INSUFFICIENT = "insufficient project role"


@pytest.fixture(autouse=True)
def reset_modules():
    import sys

    for mod in [
        "core.rbac",
        "core.security_entra",
        "core.config",
        "api.routes.agents",
        "api.routes.projects",
        "api.routes.users",
        "api.routes.tenants",
    ]:
        sys.modules.pop(mod, None)
    yield


@pytest.fixture
def entra_settings(monkeypatch):
    monkeypatch.setenv("AUTH_PROVIDER", "entra")
    monkeypatch.setenv("USE_DEV_AUTH", "False")
    monkeypatch.setenv("DEBUG", "False")
    monkeypatch.setenv("ENTRA_TENANT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("ENTRA_AUDIENCE", "api://agp")
    monkeypatch.setenv("ENTRA_ROLE_ADMIN", "Platform.Admin")
    monkeypatch.setenv("ENTRA_ROLE_OPERATOR", "Platform.Operator")
    monkeypatch.setenv("ENTRA_ROLE_VIEWER", "Platform.Viewer")


# --- record / model helpers --------------------------------------------------


def _agent(project_id=None, **overrides):
    from models.agent import Agent, LifecycleState, Origin

    now = datetime.now(timezone.utc)
    base = dict(
        id="a-1",
        name="claims-triage-de",
        purpose="Triage claims",
        lifecycle_state=LifecycleState.PROPOSED,
        origin=Origin.REGISTERED,
        created_at=now,
        updated_at=now,
        created_by="maria.bauer@example.com",
        tenant_id="ten-1",
        project_id=project_id,
    )
    base.update(overrides)
    return Agent(**base)


def _valid_agent_update():
    return {"purpose": "Triage claims faster"}


# Every route that carries the T5 project gate, as (method, path, json-body). Used by the
# ordering + short-circuit tests so a FIFTH gated route can never be added without
# appearing here.
GATED_ROUTES = [
    ("put", "/api/v1/agents/a-1", {"purpose": "Triage claims faster"}),
    ("delete", "/api/v1/agents/a-1", None),
    ("post", "/api/v1/agents/a-1/reprovision", None),
    ("put", "/api/v1/agents/a-1/publish", {"published": True}),
]


def _call(client, method, path, body):
    return getattr(client, method)(path, **({} if body is None else {"json": body}))


def _governed_row(pid="proj-1", principal="someone-else-oid", role="owner"):
    from models.project_role import ProjectRoleRecord

    return ProjectRoleRecord(
        project_id=pid, principal_id=principal, principal_type="user",
        principal_display="Alex", role=role, granted_by="seed", granted_at=FIXED_TS,
    )


# --- context + resolver fakes ------------------------------------------------


class _FakeTenantResolver:
    """Global TenantContext by DEFAULT: the tenant gate is NOT what most of these tests
    exercise (``test_registry_tenant_scoping.py`` owns it), so it must never be the reason
    for a 404 here.

    ``tenant_ids`` makes it SCOPED instead, which the ordering tests need: proving the
    project gate runs after the tenant gate requires a caller who genuinely cannot see the
    target agent's tenant."""

    def __init__(self, tenant_ids=None):
        self._tenant_ids = tenant_ids

    async def resolve(self, principal):
        from services.tenant_resolver import TenantContext

        if self._tenant_ids is None:
            return TenantContext(is_global=True, tenant_ids=frozenset(), tenants=())
        return TenantContext(
            is_global=False, tenant_ids=frozenset(self._tenant_ids), tenants=()
        )


class _FakeProjectResolver:
    """Async ``resolve`` stub + the real ``refresh_project`` semantics.

    ``refresh_project`` is NOT stubbed out: the gates call it on every DENY, so a fake without
    it would make the route's defensive except-clause swallow an AttributeError and silently
    stop exercising the fresh-read path. It therefore does exactly what the real resolver does —
    a strict single-project read through the SAME ``_role_svc`` the test seeds, folded by the
    real pure ``context_from_rows`` — so a stale-cache test can seed rows the cached ctx omits."""

    def __init__(self, ctx):
        self._ctx = ctx
        self.invalidate = MagicMock()

    async def resolve(self, principal):
        return self._ctx

    def refresh_project(self, principal, project_id):
        import api.routes.projects as projects_module
        from services.project_resolver import context_from_rows

        rows = projects_module._role_svc.list_for_project_strict(project_id)
        return context_from_rows(principal, rows)


_CLAIMS = {
    "viewer": "Platform.Viewer",
    "operator": "Platform.Operator",
    "admin": "Platform.Admin",
}


def _claims_for(platform_role: str):
    return {
        "oid": f"{platform_role}-oid",
        "preferred_username": f"{platform_role}@x.com",
        "roles": [_CLAIMS[platform_role]],
    }


# --- the shared service / client fixtures ------------------------------------


@pytest.fixture
def agent_svc():
    """The ``AgentRegistryService`` MagicMock on the agents module global."""
    import api.routes.agents as agents_module

    svc = MagicMock()
    svc.get.return_value = _agent()
    svc.update.return_value = _agent()
    svc.delete.return_value = _agent()
    svc.transition.return_value = _agent()
    svc.submit_for_approval.return_value = _agent()
    svc.persist_identity.return_value = None
    agents_module._svc = svc
    return svc


@pytest.fixture
def role_svc():
    """The ``ProjectRoleService`` MagicMock on the PROJECTS module global — the T5 gate
    reuses T4's ``_require_project_role_or_ungoverned``, so the governed-or-not read goes
    through the same singleton. ``has_role_rows`` is derived from ``list_for_project`` so a
    test seeding rows makes the project genuinely GOVERNED."""
    import api.routes.projects as projects_module

    svc = MagicMock()
    svc.list_for_project.return_value = []
    svc.has_role_rows.side_effect = lambda pid: bool(svc.list_for_project(pid))
    projects_module._role_svc = svc
    return svc


@pytest.fixture
def client_factory(entra_settings, agent_svc, role_svc):
    """Build a minimal app with ONLY the agents router + both resolver singletons seeded.

    ``roles`` is the caller's project_id -> role-name map; ``project_role`` is the
    shorthand for "this role on proj-1". ``platform_role="admin"`` makes the project
    context global (``may()`` short-circuits True), mirroring the real resolver.
    ``tenant_ids`` scopes the TENANT context (default: global) — only the gate-ordering
    tests need it.
    """
    built = []

    def _make(
        *, platform_role="operator", project_role=None, roles=None, tenant_ids=None
    ):
        import api.routes.agents as agents_module
        import api.routes.tenants as tenants_module
        import api.routes.users as users_module
        from models.project_role import ROLE_NAMES
        from services.project_resolver import ProjectContext

        if roles is None:
            roles = {} if project_role is None else {"proj-1": project_role}
        ctx = ProjectContext(
            is_global=platform_role == "admin",
            roles={pid: ROLE_NAMES[name] for pid, name in roles.items()},
        )

        users_module._tenant_resolver = _FakeTenantResolver(tenant_ids)
        users_module._project_resolver = _FakeProjectResolver(ctx)
        tenants_module._svc = MagicMock()

        app = FastAPI()
        app.include_router(agents_module.router, prefix="/api/v1")
        client = TestClient(app, headers={"Authorization": "Bearer fake-token"})
        patcher = patch(
            "core.security_entra.verify_entra_token", return_value=_claims_for(platform_role)
        )
        patcher.start()
        built.append(patcher)
        return client

    yield _make
    for patcher in built:
        patcher.stop()


# ===========================================================================
# The contract
# ===========================================================================

def test_agent_without_project_id_is_tenant_gated_only(client_factory, agent_svc):
    agent_svc.get.return_value = _agent(project_id=None)
    client = client_factory(project_role=None)
    r = client.put("/api/v1/agents/a-1", json=_valid_agent_update())
    assert r.status_code == 200


def test_agent_with_project_id_requires_maintainer(client_factory, agent_svc, role_svc):
    agent_svc.get.return_value = _agent(project_id="proj-1")
    role_svc.list_for_project.return_value = [_governed_row(pid="proj-1")]
    client = client_factory(project_role="viewer", roles={"proj-1": "viewer"})
    r = client.put("/api/v1/agents/a-1", json=_valid_agent_update())
    assert r.status_code == 403
    assert r.json()["detail"] == INSUFFICIENT
    agent_svc.update.assert_not_called()


def test_reprovision_is_project_gated(client_factory, agent_svc, role_svc):
    agent_svc.get.return_value = _agent(project_id="proj-1")
    role_svc.list_for_project.return_value = [_governed_row(pid="proj-1")]
    client = client_factory(project_role="viewer", roles={"proj-1": "viewer"})
    assert client.post("/api/v1/agents/a-1/reprovision").status_code == 403
    agent_svc.persist_identity.assert_not_called()


def test_delete_is_project_gated(client_factory, agent_svc, role_svc):
    agent_svc.get.return_value = _agent(project_id="proj-1")
    role_svc.list_for_project.return_value = [_governed_row(pid="proj-1")]
    client = client_factory(project_role="viewer", roles={"proj-1": "viewer"})
    r = client.delete("/api/v1/agents/a-1")
    assert r.status_code == 403
    assert r.json()["detail"] == INSUFFICIENT
    agent_svc.delete.assert_not_called()


def test_publish_is_project_gated(client_factory, agent_svc, role_svc):
    """``PUT /{id}/publish`` writes through the SAME ``svc.update`` read-modify-write as the
    gated ``PUT /{id}``, and flips the CROSS-TENANT exposure flag — a strictly wider blast
    radius than a ``purpose`` edit. A caller refused on the latter must not be able to
    publish that agent instead."""
    agent_svc.get.return_value = _agent(project_id="proj-1")
    role_svc.list_for_project.return_value = [_governed_row(pid="proj-1")]
    client = client_factory(project_role="viewer", roles={"proj-1": "viewer"})
    r = client.put("/api/v1/agents/a-1/publish", json={"published": True})
    assert r.status_code == 403
    assert r.json()["detail"] == INSUFFICIENT
    agent_svc.update.assert_not_called()


def test_maintainer_may_publish_its_project_agent(client_factory, agent_svc, role_svc):
    agent_svc.get.return_value = _agent(project_id="proj-1")
    agent_svc.update.return_value = _agent(project_id="proj-1", published=True)
    role_svc.list_for_project.return_value = [_governed_row(pid="proj-1")]
    client = client_factory(project_role="maintainer", roles={"proj-1": "maintainer"})
    r = client.put("/api/v1/agents/a-1/publish", json={"published": True})
    assert r.status_code == 200
    assert r.json()["published"] is True


def test_maintainer_may_mutate_its_project_agent(client_factory, agent_svc, role_svc):
    agent_svc.get.return_value = _agent(project_id="proj-1")
    agent_svc.update.return_value = _agent(project_id="proj-1")
    role_svc.list_for_project.return_value = [_governed_row(pid="proj-1")]
    client = client_factory(project_role="maintainer", roles={"proj-1": "maintainer"})
    assert client.put("/api/v1/agents/a-1", json=_valid_agent_update()).status_code == 200


def test_registry_transition_is_not_project_gated(client_factory, agent_svc, role_svc):
    """Design §6 non-overlap: lifecycle approval belongs to the registry/platform admin."""
    agent_svc.get.return_value = _agent(project_id="proj-1")
    client = client_factory(platform_role="admin", project_role=None)
    r = client.post("/api/v1/agents/a-1/transitions", json={"action": "approve", "reason": "ok"})
    assert r.status_code != 403


# --- project_id is SERVER-STAMPED ONLY (never a request body) ----------------

def test_create_body_cannot_supply_project_id(client_factory, agent_svc):
    """``POST /agents`` must NOT let a body plant a new agent into someone else's project.

    ``project_id`` is not a field on ``AgentCreate`` at all, so pydantic's default
    ``extra="ignore"`` drops the key — the route never sees it and the service is called
    with no server-side stamp, so the persisted envelope carries ``None``. Global
    Constraint: identity comes only from the validated Principal, never a request body."""
    agent_svc.create.return_value = _agent(project_id=None)
    client = client_factory()
    r = client.post(
        "/api/v1/agents",
        json={"name": "planted", "tenant_id": "ten-1", "project_id": "victim-proj"},
    )
    assert r.status_code == 201
    # The model dropped it — nothing downstream can read it back off the payload.
    req = agent_svc.create.call_args.args[0]
    assert not hasattr(req, "project_id")
    assert "project_id" not in req.model_dump()
    # And the route passed no server-side stamp either.
    assert agent_svc.create.call_args.kwargs.get("project_id") is None
    assert r.json()["project_id"] is None


def test_body_supplied_project_id_is_not_persisted_in_the_envelope(
    service, mock_registry_clients
):
    """The persistence-boundary half of the above: even if a body key survived validation
    somewhere, the CREATE envelope the registry receives must carry ``project_id: None``
    unless the SERVER passed the keyword. Asserted against the real
    ``AgentRegistryService`` rather than a mock, because the envelope is what an
    authorization gate later reads."""
    import json as _json

    from models.agent import AgentCreate

    ctl, _ = mock_registry_clients
    service.create(
        AgentCreate.model_validate(
            {"name": "planted", "tenant_id": "ten-1", "project_id": "victim-proj"}
        )
    )
    inline = ctl.create_registry_record.call_args.kwargs["descriptors"]["custom"]["data"]
    assert _json.loads(inline)["project_id"] is None


def test_server_side_keyword_is_the_only_way_to_stamp_project_id(
    service, mock_registry_clients
):
    """The positive half — ``ProjectService.add_repo``'s stamp still lands in the envelope,
    so moving the field off the client-input model did not break materialize."""
    import json as _json

    from models.agent import AgentCreate

    ctl, _ = mock_registry_clients
    agent = service.create(
        AgentCreate(name="materialized", tenant_id="ten-1"), project_id="proj-1"
    )
    inline = ctl.create_registry_record.call_args.kwargs["descriptors"]["custom"]["data"]
    assert _json.loads(inline)["project_id"] == "proj-1"
    assert agent.project_id == "proj-1"


def test_project_id_round_trips_through_the_envelope():
    a = _agent(project_id="proj-1")
    assert a.project_id == "proj-1"
    revived = type(a).model_validate(a.model_dump())
    assert revived.project_id == "proj-1"


def test_project_id_round_trips_through_the_registry_envelope():
    """The governance envelope is the real persistence path (to_envelope/from_record) —
    an additive field that only survives ``model_dump`` would be lost on the next read."""
    from models.agent import Agent

    a = _agent(project_id="proj-1")
    envelope = a.to_envelope()
    assert envelope["project_id"] == "proj-1"
    revived = Agent.from_record(
        {
            "recordId": a.id,
            "name": a.name,
            "description": a.purpose,
            "status": "DRAFT",
            "createdAt": a.created_at,
            "updatedAt": a.updated_at,
        },
        envelope,
    )
    assert revived.project_id == "proj-1"


def test_pre_t5_envelope_hydrates_project_id_as_none():
    """An OLD record's envelope lacks the key entirely — it must hydrate as None (and so
    stay tenant-gated only), never raise."""
    from models.agent import Agent

    a = _agent(project_id="proj-1")
    envelope = a.to_envelope()
    envelope.pop("project_id")
    revived = Agent.from_record(
        {
            "recordId": a.id,
            "name": a.name,
            "description": a.purpose,
            "status": "DRAFT",
            "createdAt": a.created_at,
            "updatedAt": a.updated_at,
        },
        envelope,
    )
    assert revived.project_id is None


# --- T4's fail directions, inherited verbatim --------------------------------

def test_ungoverned_project_agent_stays_reachable_at_maintainer(
    client_factory, agent_svc, role_svc
):
    """Design §3: an agent on a project with NO role rows is 'not yet governed', so a
    tenant-visible caller acts as MAINTAINER — pre-migration agents keep working."""
    agent_svc.get.return_value = _agent(project_id="proj-1")
    agent_svc.update.return_value = _agent(project_id="proj-1")
    role_svc.list_for_project.return_value = []  # ungoverned
    client = client_factory(project_role=None)
    assert client.put("/api/v1/agents/a-1", json=_valid_agent_update()).status_code == 200


def test_unreadable_role_partition_fails_closed(client_factory, agent_svc, role_svc):
    """A store fault must never hand out the §3 fallback: 'unreadable' and 'ungoverned'
    are the same value to a degrading read but opposite authorization answers."""
    from services.project_role_service import ProjectRoleError

    agent_svc.get.return_value = _agent(project_id="proj-1")
    role_svc.has_role_rows.side_effect = ProjectRoleError(
        "unreadable", kind="ownership_unverified"
    )
    client = client_factory(project_role=None)
    r = client.put("/api/v1/agents/a-1", json=_valid_agent_update())
    assert r.status_code == 403
    assert r.json()["detail"] == INSUFFICIENT
    agent_svc.update.assert_not_called()


def test_missing_agent_404s_before_any_role_logic(client_factory, agent_svc, role_svc):
    """The gate runs AFTER ``_load_visible_agent``, so a missing agent 404s and the role
    store is never consulted (a 403 must never confirm an agent the caller cannot see)."""
    agent_svc.get.return_value = None
    client = client_factory(project_role=None)
    r = client.put("/api/v1/agents/a-1", json=_valid_agent_update())
    assert r.status_code == 404
    assert r.json()["detail"] == "Agent not found"
    role_svc.has_role_rows.assert_not_called()


# --- gate ORDERING: present-but-foreign-tenant is indistinguishable from absent ----


@pytest.mark.parametrize("method,path,body", GATED_ROUTES)
def test_present_foreign_tenant_agent_404s_identically_to_a_missing_one(
    client_factory, agent_svc, role_svc, method, path, body
):
    """The case the "missing agent" test above does NOT cover: the agent EXISTS but belongs
    to a tenant the caller cannot see, AND it carries a ``project_id`` the caller holds no
    role on. Both facts are true at once, so the ORDER of the two gates decides the status.

    ``_load_visible_agent`` must win: 404 with the same body as a truly-absent agent. A 403
    here would be an EXISTENCE ORACLE — it would confirm an agent the caller must not know
    exists — and it is exactly what hoisting the project gate above ``_load_visible_agent``
    (e.g. into a FastAPI dependency) produces. Asserted byte-identically against the
    missing-agent response, and with ``has_role_rows.assert_not_called()`` so the ordering
    is pinned at the SEAM rather than only at the status code."""
    role_svc.list_for_project.return_value = [_governed_row(pid="proj-1")]

    # Baseline: the truly-missing 404, from an otherwise identical caller.
    agent_svc.get.return_value = None
    missing = _call(
        client_factory(project_role=None, tenant_ids={"ten-mine"}), method, path, body
    )
    assert missing.status_code == 404

    # The present-but-foreign agent, in a tenant the caller is NOT a member of.
    agent_svc.get.return_value = _agent(project_id="proj-1", tenant_id="ten-OTHER")
    role_svc.has_role_rows.reset_mock()
    foreign = _call(
        client_factory(project_role=None, tenant_ids={"ten-mine"}), method, path, body
    )

    assert foreign.status_code == 404
    assert foreign.json() == missing.json() == {"detail": "Agent not found"}
    assert foreign.content == missing.content  # byte-identical, not merely equivalent
    role_svc.has_role_rows.assert_not_called()
    agent_svc.update.assert_not_called()
    agent_svc.delete.assert_not_called()
    agent_svc.persist_identity.assert_not_called()


# --- the `project_id is None` short-circuit, pinned at the seam --------------


@pytest.mark.parametrize("method,path,body", GATED_ROUTES)
def test_unparented_agent_never_consults_the_role_store(
    client_factory, agent_svc, role_svc, method, path, body
):
    """The ``if agent.project_id is None: return`` early-out is the single line that makes
    T5 ADDITIVE, and asserting only the 200 does NOT pin it: with the short-circuit removed,
    the design-§3 ungoverned fallback still allows the request, so an outcome-level test
    stays green either way.

    Pin it at the SEAM instead — for an unparented agent the role store must never be read
    at all. (In production the difference is real: ``has_role_rows(None)`` would hit
    ``_validate_ids`` and a DDB round-trip on every mutation of every pre-E27 agent.)"""
    agent_svc.get.return_value = _agent(project_id=None)
    agent_svc.update.return_value = _agent(project_id=None)
    client = client_factory(project_role=None)  # non-global caller, holds no role

    r = _call(client, method, path, body)

    assert r.status_code in (200, 202, 409)  # never 403 — the gate did not run
    role_svc.has_role_rows.assert_not_called()
    role_svc.list_for_project.assert_not_called()
