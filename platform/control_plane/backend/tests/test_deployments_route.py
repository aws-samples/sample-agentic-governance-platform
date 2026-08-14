"""Tests for ``GET /agents/{agent_id}/deployments`` (E28/T11, contract C2 + D7).

WHY THIS ROUTE EXISTS AT ALL. T3 built the append-only store
(``ProjectService.append_deployment`` / ``list_deployments``) and T4 built the appends, but
no task ever mounted the read. C2 pins the route, and the frontend client call T11 adds
would otherwise have been a call to a 404.

The load-bearing invariants, and why each is here rather than assumed:

1. **The gate is BYTE-IDENTICAL to ``/runtime``'s.** Two sibling reads on the same resource
   must not carry different guards — whichever is looser becomes the bypass (D3). So a
   foreign tenant's agent and a truly-missing id both produce the same "Agent not found",
   and the store is never touched before the gate has run.
2. **``stage`` is FREE-FORM (D8).** A tenant's stage set is open, so the route must forward
   ``uat`` (or a region name) to the store unvalidated. A dev/prod allowlist here would be
   exactly the hardcode the design forbids.
3. **An agent with no repository returns ``[]``, not a 500.** The store is keyed by
   ``repo_id`` and the caller supplies an ``agent_id``; an agent that owns no repo has no
   deployment history, which is a STATE, not an error.
4. **Newest-first ordering survives the route.** ``list_deployments`` does the ordering (its
   own tests pin that); this asserts the route is a pass-through that does not re-sort or
   reverse — a history list whose "current" row is not actually current is the failure class
   the whole deployment partition exists to remove.
5. **``limit`` is bounded**, mirroring ``/traces``' ``Query(default=50, ge=1, le=100)``.

Harness mirrors ``test_runtime_status.py`` layer 2 (itself the ``test_observability_routes``
idiom): a minimal FastAPI app with ONLY the agents router, the REAL ``require_role`` +
``current_principal`` path against a mocked ``verify_entra_token``, a mocked
``AgentRegistryService`` and a seeded tenant-resolver stub. The project service is patched at
``api.routes.projects.get_project_service`` — the handler resolves it lazily (an import cycle
otherwise: ``projects`` imports ``agents``), so patching the accessor is what the route sees.
No boto3, no live AWS.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

AGENT_ID = "own"
REPO_ID = "repo-1"


@pytest.fixture(autouse=True)
def reset_modules():
    """Drop cached auth/config/route modules so monkeypatched env is honored."""
    import sys

    for mod in [
        "core.rbac",
        "core.security_entra",
        "core.config",
        "api.routes.agents",
        "api.routes.users",
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


def _make_agent(*, agent_id: str = AGENT_ID, tenant_id: str = "ten-1"):
    from models.agent import Agent, AuthType, LifecycleState, Platform

    now = datetime.now(timezone.utc)
    return Agent(
        id=agent_id,
        name="Claims Triage DE",
        purpose="Triage inbound motor claims",
        lifecycle_state=LifecycleState.APPROVED,
        platform=Platform.AWS_BEDROCK,
        auth_type=AuthType.ENTRA,
        tenant_id=tenant_id,
        created_at=now,
        updated_at=now,
    )


def _deployment(*, stage: str, image_tag: str, started_at: str, **over):
    from models.deployment import Deployment, DeploymentOutcome, deployment_seq_key

    dep_id = over.pop("id", "dep-abcd1234")
    return Deployment(
        id=dep_id,
        repo_id=REPO_ID,
        agent_id=AGENT_ID,
        stage=stage,
        seq_key=deployment_seq_key(REPO_ID, stage, started_at, dep_id),
        image_tag=image_tag,
        outcome=over.pop("outcome", DeploymentOutcome.SUCCEEDED),
        started_at=started_at,
        **over,
    )


def _repo():
    from models.repository import Repository

    return Repository(
        id=REPO_ID,
        project_id="prj-1",
        name="claims-triage-de",
        agent_id=AGENT_ID,
        template_name="strands-agent",
        status="ready",
        created_by="lars.svensson@example.com",
        created_at="2026-07-01T00:00:00+00:00",
        updated_at="2026-07-01T00:00:00+00:00",
    )


def _context(*, is_global=False, tenant_ids=()):
    from services.tenant_resolver import TenantContext

    return TenantContext(is_global=is_global, tenant_ids=frozenset(tenant_ids), tenants=())


class _FakeResolver:
    def __init__(self, ctx):
        self._ctx = ctx

    async def resolve(self, principal):
        return self._ctx


def _build_client(mock_svc, ctx):
    """Minimal app with ONLY the agents router + mocked registry/resolver."""
    import api.routes.agents as agents_module
    import api.routes.users as users_module

    agents_module._svc = mock_svc
    users_module._tenant_resolver = _FakeResolver(ctx)

    app = FastAPI()
    app.include_router(agents_module.router, prefix="/api/v1")
    return TestClient(app)


def _claims_for(role: str):
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


def _project_svc(*, repo=None, rows=None):
    """A project-service double. ``repo=None`` models an agent that owns no repository."""
    svc = MagicMock()
    svc.find_repository_by_agent_id.return_value = repo
    svc.list_deployments.return_value = list(rows or [])
    return svc


def _get(client, project_svc, url):
    with patch(
        "core.security_entra.verify_entra_token", return_value=_claims_for("viewer")
    ), patch("api.routes.projects.get_project_service", return_value=project_svc):
        return client.get(url, headers=_headers())


# ---------------------------------------------------------------------------
# The gate — byte-identical to /runtime's
# ---------------------------------------------------------------------------

def test_foreign_tenant_404_is_byte_identical_to_missing(entra_settings):
    """A foreign tenant's agent must look ABSENT, never leak a 403 that confirms it exists —
    and the deployment store must not be read before the gate has run."""
    foreign = _make_agent(agent_id="foreign", tenant_id="ten-2")
    mock_svc = MagicMock()
    mock_svc.get.side_effect = lambda agent_id: (
        foreign if agent_id == "foreign" else None
    )
    client = _build_client(mock_svc, _context(tenant_ids=["ten-1"]))
    project_svc = _project_svc(repo=_repo())

    foreign_resp = _get(client, project_svc, "/api/v1/agents/foreign/deployments")
    missing_resp = _get(client, project_svc, "/api/v1/agents/truly-missing/deployments")

    assert foreign_resp.status_code == 404
    assert missing_resp.status_code == 404
    assert foreign_resp.json() == missing_resp.json()
    assert foreign_resp.json()["detail"] == "Agent not found"
    project_svc.list_deployments.assert_not_called()
    project_svc.find_repository_by_agent_id.assert_not_called()


def test_gate_matches_the_runtime_route_exactly(entra_settings):
    """Mechanical, not by eye: the two handlers' RBAC dependencies must be the same objects
    in the same order. A guard that differs between two sibling reads on one resource means
    the looser one is a bypass (D3), and a hand-copied gate is exactly how that happens."""
    import api.routes.agents as agents_module

    def deps(suffix: str):
        route = next(
            r
            for r in agents_module.router.routes
            if getattr(r, "path", "").endswith(suffix)
        )
        # The gate, in DECLARATION ORDER (order matters — the tenant context must resolve
        # before the role check). ``require_role`` returns a fresh closure per call, so the
        # callables are never identical objects; the comparable identity is the qualified name
        # plus the captured ``min_role``, which is exactly what makes two gates the same gate.
        out = []
        for d in route.dependant.dependencies:
            captured = tuple(c.cell_contents for c in (d.call.__closure__ or ()))
            out.append((d.name, d.call.__qualname__, captured))
        return out

    runtime_gate = deps("/{agent_id}/runtime")
    # Guards this assertion against a vacuous pass: /runtime really does carry both halves of
    # the gate, and the role half really did capture VIEWER.
    from core.rbac import Role

    assert len(runtime_gate) == 2
    assert runtime_gate[0][:2] == ("ctx", "get_tenant_ctx")
    assert runtime_gate[1][0] == "_"
    assert runtime_gate[1][2] == (Role.VIEWER,)

    assert deps("/{agent_id}/deployments") == runtime_gate


def test_requires_a_validated_token(entra_settings):
    """No token → 401, and the store is never read."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent()
    client = _build_client(mock_svc, _context(tenant_ids=["ten-1"]))
    project_svc = _project_svc(repo=_repo())

    with patch("api.routes.projects.get_project_service", return_value=project_svc):
        resp = client.get(f"/api/v1/agents/{AGENT_ID}/deployments")

    assert resp.status_code == 401
    project_svc.list_deployments.assert_not_called()


def test_viewer_suffices(entra_settings):
    """VIEWER-gated, like ``/runtime`` and ``/metrics`` — reads are tenant-wide by design."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent()
    client = _build_client(mock_svc, _context(tenant_ids=["ten-1"]))

    resp = _get(
        client,
        _project_svc(repo=_repo(), rows=[]),
        f"/api/v1/agents/{AGENT_ID}/deployments",
    )

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# The read
# ---------------------------------------------------------------------------

def test_agent_with_no_repository_is_empty(entra_settings):
    """The store is keyed by ``repo_id``; the caller supplies an ``agent_id``. An agent that
    owns no repository has NO deployment history, which is a state and not an error."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent()
    client = _build_client(mock_svc, _context(tenant_ids=["ten-1"]))
    project_svc = _project_svc(repo=None, rows=[_deployment(
        stage="uat", image_tag="own-tree001", started_at="2026-07-30T10:00:00+00:00"
    )])

    resp = _get(client, project_svc, f"/api/v1/agents/{AGENT_ID}/deployments")

    assert resp.status_code == 200
    assert resp.json() == []
    # And it did NOT fall through to an unscoped read — that would return another repo's rows.
    project_svc.list_deployments.assert_not_called()


def test_repo_id_is_resolved_server_side_from_the_agent(entra_settings):
    """No ``repo_id`` query param exists, deliberately: one would let a caller read another
    repo's history under an agent they happen to be able to see."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent()
    client = _build_client(mock_svc, _context(tenant_ids=["ten-1"]))
    project_svc = _project_svc(repo=_repo(), rows=[])

    resp = _get(
        client,
        project_svc,
        f"/api/v1/agents/{AGENT_ID}/deployments?repo_id=someone-elses-repo",
    )

    assert resp.status_code == 200
    project_svc.find_repository_by_agent_id.assert_called_once_with(AGENT_ID)
    assert project_svc.list_deployments.call_args.args[0] == REPO_ID
    assert "someone-elses-repo" not in str(project_svc.list_deployments.call_args)


def test_a_uat_stage_passes_through_unvalidated(entra_settings):
    """``stage`` is FREE-FORM (D8). A tenant's stage set is open, so validating against a
    dev/prod literal here would refuse a legitimate tenant's only stage."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent()
    client = _build_client(mock_svc, _context(tenant_ids=["ten-1"]))
    row = _deployment(
        stage="uat", image_tag="own-tree001", started_at="2026-07-30T10:00:00+00:00"
    )
    project_svc = _project_svc(repo=_repo(), rows=[row])

    resp = _get(client, project_svc, f"/api/v1/agents/{AGENT_ID}/deployments?stage=uat")

    assert resp.status_code == 200
    assert project_svc.list_deployments.call_args.kwargs["stage"] == "uat"
    assert [r["stage"] for r in resp.json()] == ["uat"]


def test_an_arbitrary_stage_name_is_not_refused(entra_settings):
    """Belt to the braces above: a region-shaped stage name is just as legitimate."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent()
    client = _build_client(mock_svc, _context(tenant_ids=["ten-1"]))
    project_svc = _project_svc(repo=_repo(), rows=[])

    resp = _get(
        client, project_svc, f"/api/v1/agents/{AGENT_ID}/deployments?stage=eu-central-1"
    )

    assert resp.status_code == 200
    assert project_svc.list_deployments.call_args.kwargs["stage"] == "eu-central-1"


def test_no_stage_reads_across_stages(entra_settings):
    """Omitting ``stage`` must forward ``None``, not a default literal — the store branches on
    ``if stage:`` and a blank/omitted value is what makes it read the whole repo."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent()
    client = _build_client(mock_svc, _context(tenant_ids=["ten-1"]))
    project_svc = _project_svc(repo=_repo(), rows=[])

    resp = _get(client, project_svc, f"/api/v1/agents/{AGENT_ID}/deployments")

    assert resp.status_code == 200
    assert project_svc.list_deployments.call_args.kwargs["stage"] is None


def test_newest_first_ordering_survives_the_route(entra_settings):
    """The store orders; the route must be a pass-through. A history list whose top row is
    not the current one is the exact failure the deployment partition exists to remove."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent()
    client = _build_client(mock_svc, _context(tenant_ids=["ten-1"]))
    rows = [
        _deployment(
            stage="uat", image_tag="own-newest", started_at="2026-07-31T10:00:00+00:00",
            id="dep-11111111",
        ),
        _deployment(
            stage="uat", image_tag="own-middle", started_at="2026-07-30T10:00:00+00:00",
            id="dep-22222222",
        ),
        _deployment(
            stage="uat", image_tag="own-oldest", started_at="2026-07-29T10:00:00+00:00",
            id="dep-33333333",
        ),
    ]
    project_svc = _project_svc(repo=_repo(), rows=rows)

    resp = _get(client, project_svc, f"/api/v1/agents/{AGENT_ID}/deployments")

    assert [r["image_tag"] for r in resp.json()] == [
        "own-newest", "own-middle", "own-oldest",
    ]


def test_actor_kind_round_trips(entra_settings):
    """A GitHub login and an Entra oid are two different currencies and must never be
    rendered as one (E27A §6) — so the discriminator has to survive the wire."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent()
    client = _build_client(mock_svc, _context(tenant_ids=["ten-1"]))
    rows = [
        _deployment(
            stage="uat", image_tag="own-a", started_at="2026-07-31T10:00:00+00:00",
            actor="jorge", actor_kind="github", id="dep-aaaaaaaa",
        ),
        _deployment(
            stage="uat", image_tag="own-b", started_at="2026-07-30T10:00:00+00:00",
            actor="00000000-0000-0000-0000-000000000009", actor_kind="entra",
            id="dep-bbbbbbbb",
        ),
        # A build-written terminal row carries NO actor at all, by design (C1).
        _deployment(
            stage="uat", image_tag="own-c", started_at="2026-07-29T10:00:00+00:00",
            id="dep-cccccccc",
        ),
    ]
    project_svc = _project_svc(repo=_repo(), rows=rows)

    body = _get(client, project_svc, f"/api/v1/agents/{AGENT_ID}/deployments").json()

    assert [r["actor_kind"] for r in body] == ["github", "entra", None]
    assert [r["actor"] for r in body] == [
        "jorge", "00000000-0000-0000-0000-000000000009", None,
    ]


# ---------------------------------------------------------------------------
# limit
# ---------------------------------------------------------------------------

def test_limit_defaults_to_50(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent()
    client = _build_client(mock_svc, _context(tenant_ids=["ten-1"]))
    project_svc = _project_svc(repo=_repo(), rows=[])

    _get(client, project_svc, f"/api/v1/agents/{AGENT_ID}/deployments")

    assert project_svc.list_deployments.call_args.kwargs["limit"] == 50


@pytest.mark.parametrize("bad", ["0", "101", "-1"])
def test_limit_is_bounded(entra_settings, bad):
    """``Query(default=50, ge=1, le=100)`` — the ``/traces`` idiom. An unbounded limit on an
    append-only partition is an unbounded read."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent()
    client = _build_client(mock_svc, _context(tenant_ids=["ten-1"]))
    project_svc = _project_svc(repo=_repo(), rows=[])

    resp = _get(client, project_svc, f"/api/v1/agents/{AGENT_ID}/deployments?limit={bad}")

    assert resp.status_code == 422
    project_svc.list_deployments.assert_not_called()


def test_limit_is_forwarded(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent()
    client = _build_client(mock_svc, _context(tenant_ids=["ten-1"]))
    project_svc = _project_svc(repo=_repo(), rows=[])

    _get(client, project_svc, f"/api/v1/agents/{AGENT_ID}/deployments?limit=7")

    assert project_svc.list_deployments.call_args.kwargs["limit"] == 7


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------

def test_an_unreachable_store_is_an_empty_history_not_a_5xx(entra_settings):
    """``list_deployments`` degrades to ``[]`` and NEVER raises (T3/T2, hardened over three
    rounds). The route must not add error mapping that turns that into a 5xx — a 500 here
    would blank a repo page over a DynamoDB throttle."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent()
    client = _build_client(mock_svc, _context(tenant_ids=["ten-1"]))
    project_svc = _project_svc(repo=_repo(), rows=[])

    resp = _get(client, project_svc, f"/api/v1/agents/{AGENT_ID}/deployments")

    assert resp.status_code == 200
    assert resp.json() == []


def test_response_carries_no_account_id(entra_settings):
    """A hard project rule: no AWS account id anywhere. Nothing on a Deployment row should
    ever carry one, and this pins that the route does not enrich the row with one."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent()
    client = _build_client(mock_svc, _context(tenant_ids=["ten-1"]))
    rows = [
        _deployment(
            stage="uat", image_tag="own-a", started_at="2026-07-31T10:00:00+00:00",
            source_sha="3f9a1c2b" * 5, build_id="agp-runtime:abc-123",
        )
    ]
    project_svc = _project_svc(repo=_repo(), rows=rows)

    resp = _get(client, project_svc, f"/api/v1/agents/{AGENT_ID}/deployments")

    # Assert the 200 FIRST. Without it a 404 body ("Agent not found") satisfies every
    # "not in" below and the test passes vacuously — the exact class of guard this epic
    # got burned by twice.
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    raw = resp.text
    assert "dkr.ecr" not in raw
    assert "arn:aws" not in raw
