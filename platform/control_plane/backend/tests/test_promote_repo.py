"""Governed prod promotion — ``POST /projects/{id}/repos/{repo_id}/promote`` (Epic 27, T8).

The epic's headline feature: an OWNER clicks Promote and the BACKEND resolves which image
goes to prod. **No image tag is ever entered by a user** — it is read off the record the
dev buildspec stamped (``last_dev_image_tag``, T7), so the promote surface has no
tag-injection input at all. The tests below therefore pin two things that are easy to lose:
the tag comes from the RECORD (and a client-supplied one is ignored), and the OWNER gate
refuses BEFORE any deploy is started.

Harness idiom is ``test_projects_role_gating.py``: the autouse module reset, the entra env
fixture, FAKE resolvers on the ``users`` module globals (the ONE resolver singletons), and
NO ``dependency_overrides`` — the REAL ``require_role`` / ``current_principal`` /
``get_tenant_ctx`` / ``get_project_ctx`` chain runs against a mocked ``verify_entra_token``.

Unlike that file, the ``ProjectService`` here is the REAL in-memory one (``table_name=""``,
the ``test_repo_status_retry.py`` precedent) with its collaborators mocked — because the
whole contract under test is what the SERVICE resolves and persists, which a MagicMock
would answer for. Only the single-repo READ is a mock, so a test can vary the stored
``last_dev_image_tag`` without hand-seeding a row. The runtime-build service is a mock:
nothing here may reach CodeBuild, Secrets Manager, GitHub or Entra.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from models.deployment import DeploymentOutcome
from models.project import (  # noqa: F401 — ProjectDetail via get_project
    Project,
    ProjectDetail,
)
from models.project_role import ROLE_NAMES, ProjectRoleRecord
from models.repository import Repository
from services.project_service import (
    _PROD_CANDIDATE_FIELDS,
    ProjectError,
    ProjectService,
)

FIXED = datetime(2026, 7, 27, tzinfo=timezone.utc)
FIXED_TS = FIXED.isoformat()

# The tag the dev buildspec stamped on the record — what DEV is currently running. E27A
# narrowed promote OFF this tag; it is kept on the record (and asserted untouched) because it
# is still the FE's "what dev runs" fact.
DEV_TAG = "a-1-abc1234"
# E27A: the PROD CANDIDATE — the tree-sha tag registered when something merged to `main`, and
# the only tag promote may ever deploy. DELIBERATELY DIFFERENT from DEV_TAG: were they equal,
# every assertion below would pass whichever field the service read, and the narrowing this
# epic exists for would not be pinned by a single test.
CANDIDATE_TAG = "a-1-tree777"
CANDIDATE_SHA = "f" * 40
CANDIDATE_ACTOR = "merger-login"
# E28B/T4: read FROM THE SERVICE, not restated. A local copy stopped matching the moment the
# production tuple grew ``prod_candidate_digest``, and the "clears the whole block" loop below would
# have kept passing while the digest silently survived a promote — a leftover digest being exactly
# what lets a second approval deploy an already-consumed candidate.
_CANDIDATE_FIELDS = _PROD_CANDIDATE_FIELDS
assert "prod_candidate_digest" in _CANDIDATE_FIELDS, _CANDIDATE_FIELDS  # non-vacuity
INSUFFICIENT = "insufficient project role"


@pytest.fixture(autouse=True)
def reset_modules():
    import sys

    for mod in [
        "core.rbac",
        "core.security_entra",
        "core.config",
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
    monkeypatch.setenv("ENTRA_TENANT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("ENTRA_AUDIENCE", "api://agp")
    monkeypatch.setenv("ENTRA_BACKEND_CLIENT_ID", "backend-client-id")
    monkeypatch.setenv("ENTRA_SPA_CLIENT_ID", "spa-client-id")
    monkeypatch.setenv("ENTRA_ROLE_ADMIN", "Platform.Admin")
    monkeypatch.setenv("ENTRA_ROLE_OPERATOR", "Platform.Operator")
    monkeypatch.setenv("ENTRA_ROLE_VIEWER", "Platform.Viewer")


# --- record / model helpers --------------------------------------------------


def _governed_row(pid="proj-1", principal="someone-else-oid", role="owner"):
    """A role row on ``pid`` — its mere EXISTENCE makes the project 'governed'."""
    return ProjectRoleRecord(
        project_id=pid, principal_id=principal, principal_type="user",
        principal_display="Alex", role=role, granted_by="seed", granted_at=FIXED_TS,
    )


def _project(id="proj-1", tenant_id="default"):
    return Project(
        id=id,
        name="Fraud Ops",
        connection_id="conn-1",
        tenant_id=tenant_id,
        description="a container",
        created_by="operator@x.com",
        created_at=FIXED_TS,
        updated_at=FIXED_TS,
    )


def _repo(
    id="r-1",
    project_id="proj-1",
    last_dev_image_tag=DEV_TAG,
    cicd_status="deployed",
    last_promoted_at=None,
    prod_candidate_image_tag=CANDIDATE_TAG,
    prod_candidate_digest=None,
):
    """A repo with a PENDING prod candidate — so it HAS something promotable (E27A).

    It also carries ``last_dev_image_tag``, because the two coexist on a real record: dev is
    running one image while ``main`` offers another. Promote must read the CANDIDATE.

    ``cicd_status`` / ``last_promoted_at`` are overridable so a test can stage the
    in-flight and the STUCK-at-``promoting`` states the bounded guard distinguishes;
    ``prod_candidate_image_tag`` so a test can stage the no-candidate refusal."""
    candidate = (
        {
            "prod_candidate_image_tag": prod_candidate_image_tag,
            # E28B/T4: defaults to None so every PRE-EXISTING test in this file keeps exercising the
            # legacy tag-only candidate — that path must not regress, since a pre-E28B row and a
            # rollback both still travel it. The digest cases pass it explicitly.
            "prod_candidate_digest": prod_candidate_digest,
            "prod_candidate_sha": CANDIDATE_SHA,
            "prod_candidate_actor": CANDIDATE_ACTOR,
            "prod_candidate_at": FIXED_TS,
            "prod_candidate_status": "pending",
        }
        # A repo with no candidate tag has NO candidate at all — the five fields are written
        # and cleared as one unit, so staging a half-populated candidate would test a state
        # the service cannot produce.
        if prod_candidate_image_tag
        else {}
    )
    return Repository(
        id=id,
        project_id=project_id,
        name="fraud-agent",
        repo_url="https://github.com/acme/fraud-agent",
        agent_id="a-1",
        template_name="strands-agentcore",
        cicd_status=cicd_status,
        status="provisioning",
        created_by="operator@x.com",
        created_at=FIXED_TS,
        updated_at=FIXED_TS,
        last_dev_image_tag=last_dev_image_tag,
        last_promoted_at=last_promoted_at,
        **candidate,
    )


# --- context + resolver fakes ------------------------------------------------


def _tenant_context(*, is_global=False, tenant_ids=("default",)):
    from services.tenant_resolver import TenantContext

    return TenantContext(is_global=is_global, tenant_ids=frozenset(tenant_ids), tenants=())


def _as_role(value):
    return ROLE_NAMES[value] if isinstance(value, str) else value


def _project_context(*, is_global=False, role=None, project_id="proj-1", roles=None):
    from services.project_resolver import ProjectContext

    if roles is not None:
        mapped = {pid: _as_role(r) for pid, r in roles.items()}
    elif role is None:
        mapped = {}
    else:
        mapped = {project_id: _as_role(role)}
    return ProjectContext(is_global=is_global, roles=mapped)


class _FakeTenantResolver:
    def __init__(self, ctx):
        self._ctx = ctx

    async def resolve(self, principal):
        return self._ctx


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


def _real_service(build_svc, *, tenant_id="default"):
    """The REAL in-memory ``ProjectService`` with every collaborator mocked.

    ``table_name=""`` ⇒ the local dict fallback (no boto3, no DDB). The parent project is
    seeded so ``_load_visible_project`` and the connection lookup inside ``promote_repo``
    both resolve for real — the tenant gate is therefore exercised, not stubbed out."""
    svc = ProjectService(
        table_name="",
        registry=MagicMock(),
        identity=MagicMock(),
        connection_service=MagicMock(),
        github_repo_service=MagicMock(),
        runtime_build_service=build_svc,
        ecr_repository="platform-fallback-ecr",
        now=lambda: FIXED,
    )
    svc._local_projects["proj-1"] = _project(tenant_id=tenant_id)
    return svc


@pytest.fixture
def build_svc():
    """The ``RuntimeBuildService`` mock the service promotes through. Nothing in this file
    may reach CodeBuild/Secrets Manager, and ``assert_not_called`` on it is how every gate
    test proves the refusal happened BEFORE the deploy."""
    svc = MagicMock()
    svc.start_runtime_build.return_value = "build-1"
    return svc


@pytest.fixture
def role_svc():
    """The ``ProjectRoleService`` MagicMock on the projects module global.

    Promote uses the STRICT gate, so this store is never read to decide authority — the
    fixture exists so a regression to the ungoverned-fallback helper is VISIBLE (as a call
    on ``has_role_rows``) rather than silent."""
    import api.routes.projects as projects_module

    svc = MagicMock()
    svc.has_role_rows.return_value = False
    svc.list_all_strict.return_value = []
    svc.list_for_project.return_value = []
    projects_module._role_svc = svc
    return svc


@pytest.fixture
def project_svc(build_svc):
    """The real ProjectService on the projects module global, with ONLY the single-repo
    read mocked so a test can vary the stored ``last_dev_image_tag`` in one line.

    ``get_repo`` and ``_get_repo`` are the SAME mock: the route reads through the public
    one, the service through the private one, and a test setting ``get_repo.return_value``
    must reach both."""
    import api.routes.projects as projects_module

    svc = _real_service(build_svc)
    repo_read = MagicMock(return_value=_repo())
    svc.get_repo = repo_read
    svc._get_repo = repo_read
    projects_module._svc = svc
    return svc


@pytest.fixture
def project_service_local(build_svc):
    """Service-level fixture: NOTHING mocked on the read path, so the persisted outcome of
    a promote (success or failure) is asserted against real stored state."""
    svc = _real_service(build_svc)
    svc._local_repos["r-1"] = _repo()
    return svc


@pytest.fixture
def client_factory(entra_settings, role_svc, project_svc):
    """Build a TestClient for a caller with a given PLATFORM role + PROJECT role."""
    built = []

    def _make(*, platform_role="operator", project_role=None, roles=None,
              project_ctx=None, tenant_ctx=None):
        import api.routes.projects as projects_module
        import api.routes.users as users_module

        users_module._tenant_resolver = _FakeTenantResolver(
            tenant_ctx if tenant_ctx is not None else _tenant_context()
        )
        users_module._project_resolver = _FakeProjectResolver(
            project_ctx
            if project_ctx is not None
            else _project_context(
                is_global=platform_role == "admin", role=project_role, roles=roles
            )
        )

        app = FastAPI()
        app.include_router(projects_module.router, prefix="/api/v1")
        client = TestClient(app, headers={"Authorization": "Bearer fake-token"})
        patcher = patch(
            "core.security_entra.verify_entra_token", return_value=_claims_for(platform_role)
        )
        patcher.start()
        client._entra_patcher = patcher
        built.append(client)
        return client

    yield _make
    for client in built:
        client._entra_patcher.stop()


@pytest.fixture
def client_foreign_tenant(client_factory):
    """A caller whose tenant does NOT include the project's, holding NO project role.

    Both conditions at once is what makes the gate ORDER observable: the two gates DISAGREE
    (tenant-first says 404, role-first says 403), so the observed status distinguishes the
    order rather than merely happening to be 404. A 403 here would CONFIRM that a foreign
    tenant's project exists."""
    return client_factory(
        project_role=None, tenant_ctx=_tenant_context(tenant_ids=["ten-OTHER"])
    )


# ===========================================================================
# The route — the tag is resolved by the BACKEND, and OWNER gates it
# ===========================================================================


def test_owner_promotes_with_backend_resolved_tag(client_factory, project_svc, build_svc, role_svc):
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory(project_role="owner", roles={"proj-1": "owner"})
    r = client.post("/api/v1/projects/proj-1/repos/r-1/promote")
    assert r.status_code == 202
    # the tag came from the record's CANDIDATE, and NO tag was accepted from the caller
    assert build_svc.start_runtime_build.call_args.kwargs["image_tag"] == CANDIDATE_TAG
    assert build_svc.start_runtime_build.call_args.kwargs["stage"] == "prod"


def test_promote_ignores_any_client_supplied_tag(client_factory, project_svc, build_svc, role_svc):
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory(project_role="owner", roles={"proj-1": "owner"})
    r = client.post("/api/v1/projects/proj-1/repos/r-1/promote", json={"image_tag": "evil-tag"})
    assert r.status_code == 202
    assert build_svc.start_runtime_build.call_args.kwargs["image_tag"] == CANDIDATE_TAG


def test_maintainer_cannot_promote(client_factory, build_svc, role_svc):
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory(project_role="maintainer", roles={"proj-1": "maintainer"})
    r = client.post("/api/v1/projects/proj-1/repos/r-1/promote")
    assert r.status_code == 403
    assert r.json()["detail"] == INSUFFICIENT
    build_svc.start_runtime_build.assert_not_called()      # guard BEFORE the deploy


def test_ungoverned_project_still_requires_a_real_owner(client_factory, build_svc, role_svc):
    """Promote does NOT ride the design-§3 ungoverned fallback: an unowned project cannot
    be promoted to prod by a mere tenant member. The store is never even consulted, which
    is what pins the STRICT gate rather than the fallback-bearing one."""
    role_svc.list_for_project.return_value = []
    client = client_factory(project_role=None)
    assert client.post("/api/v1/projects/proj-1/repos/r-1/promote").status_code == 403
    build_svc.start_runtime_build.assert_not_called()
    role_svc.has_role_rows.assert_not_called()


def test_a_missing_dev_tag_no_longer_blocks_promote(
    client_factory, project_svc, build_svc, role_svc
):
    """E27A's narrowing, from the other side: promote depends on the CANDIDATE alone.

    Pre-E27A this repo 409'd ``no dev deployment to promote``. It must now promote: the record
    of what dev is *running* is not a precondition for shipping what ``main`` OFFERS — a repo
    whose dev deploy was never recorded (a swallowed buildspec write, a prod-only history) is
    still promotable once something has merged to ``main``."""
    project_svc.get_repo.return_value = _repo(last_dev_image_tag=None)
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory(project_role="owner", roles={"proj-1": "owner"})
    r = client.post("/api/v1/projects/proj-1/repos/r-1/promote")
    assert r.status_code == 202
    assert build_svc.start_runtime_build.call_args.kwargs["image_tag"] == CANDIDATE_TAG


def test_foreign_tenant_404s_before_promote(client_foreign_tenant, build_svc):
    r = client_foreign_tenant.post("/api/v1/projects/proj-1/repos/r-1/promote")
    assert r.status_code == 404
    assert r.json()["detail"] == "Project not found"
    build_svc.start_runtime_build.assert_not_called()


def test_platform_viewer_cannot_promote_even_as_project_owner(
    client_factory, project_svc, build_svc, role_svc
):
    """The PLATFORM-role gate (``require_role(Role.OPERATOR)``) is independent of the project
    role: a project OWNER who is only a Platform.Viewer still cannot deploy to prod. Without
    this, deleting the ``require_role`` dependency from the route signature would be a silent
    false-permissive change on the highest-consequence route in the epic."""
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory(platform_role="viewer", roles={"proj-1": "owner"})
    r = client.post("/api/v1/projects/proj-1/repos/r-1/promote")
    assert r.status_code == 403
    build_svc.start_runtime_build.assert_not_called()


# ===========================================================================
# E27A — the narrowing: promote deploys the CANDIDATE, and consumes it
# ===========================================================================


def test_promote_deploys_the_candidate_not_the_dev_tag(project_service_local, build_svc):
    """The epic in one assertion: with dev running one image and ``main`` offering another,
    prod gets ``main``'s. Reading ``last_dev_image_tag`` here would ship an artifact that was
    never reviewed or merged — the whole problem E27A exists to fix."""
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert build_svc.start_runtime_build.call_args.kwargs["image_tag"] == CANDIDATE_TAG
    assert CANDIDATE_TAG != DEV_TAG  # the fixture must keep them distinguishable


def test_no_candidate_is_no_prod_candidate(project_service_local, build_svc):
    """Nothing has merged to ``main`` since the last promotion ⇒ there is nothing to approve.
    Refused BEFORE the deploy, with the kind T7 maps to 409."""
    project_service_local._local_repos["r-1"] = _repo(prod_candidate_image_tag=None)
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert ei.value.kind == "no_prod_candidate"
    build_svc.start_runtime_build.assert_not_called()


def test_an_empty_candidate_tag_is_also_no_prod_candidate(project_service_local, build_svc):
    """Kept FALSY-or-missing, never ``is None`` — E27/T7's reasoning carried over intact. An
    empty tag would deploy ``<ecr_repo>:`` to PRODUCTION, so the guard must not narrow to a
    None check just because a different field is now read."""
    project_service_local._local_repos["r-1"] = _repo(prod_candidate_image_tag="")
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert ei.value.kind == "no_prod_candidate"
    build_svc.start_runtime_build.assert_not_called()


def test_a_pre_e26a_record_still_refuses_without_leaking_a_500(project_service_local, build_svc):
    """A row written before E27A has neither a candidate nor (perhaps) a dev tag. It must
    refuse cleanly with a mapped kind rather than raise on a missing attribute."""
    project_service_local._local_repos["r-1"] = _repo(
        prod_candidate_image_tag=None, last_dev_image_tag=None
    )
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert ei.value.kind == "no_prod_candidate"
    build_svc.start_runtime_build.assert_not_called()


def test_a_successful_promote_clears_all_six_candidate_fields(project_service_local, build_svc):
    """The candidate is CONSUMED by the approval, so a second Promote on the same merge is
    refused rather than re-deploying the same image (and rather than leaving a permanently
    'pending' badge in the UI). Asserted against a RE-READ: clearing the in-memory record
    without a save that OPTS IN to the candidate fields would leave the stored row pending."""
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    stored = project_service_local.get_repo("r-1")
    for field in _CANDIDATE_FIELDS:
        assert getattr(stored, field) is None, field
    # …and WHAT was shipped is not lost by the clearing. E28A/T6 moved that fact off the
    # `last_promoted_image_tag` scalar (which now means "what prod SERVES", written only once a
    # delivery has actually succeeded) onto the append-only delivery row, so it is asserted
    # THERE — consuming the candidate must not erase the record of the artifact it offered.
    assert stored.last_promotion_build_id == "build-1"
    (row,) = project_service_local.list_deployments("r-1", stage="prod")
    assert row.image_tag == CANDIDATE_TAG


def test_a_failed_promote_leaves_the_candidate_intact(project_service_local, build_svc):
    """A failed promote must stay RETRYABLE. Clearing the candidate on the failure path would
    turn a transient CodeBuild/registry fault into a permanent one: the record would 409
    ``no_prod_candidate`` forever and the only recovery would be pushing a new commit to
    ``main`` — the operator would have to fake a merge to retry a deploy."""
    from services.runtime_build_service import RuntimeBuildError

    build_svc.start_runtime_build.side_effect = RuntimeBuildError("nope", kind="codebuild")
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert ei.value.kind == "promote_failed"
    stored = project_service_local.get_repo("r-1")
    assert stored.cicd_status == "failed"                     # the attribution still persisted
    assert stored.prod_candidate_image_tag == CANDIDATE_TAG    # …and the candidate survived
    assert stored.prod_candidate_status == "pending"
    assert stored.prod_candidate_sha == CANDIDATE_SHA
    assert stored.prod_candidate_actor == CANDIDATE_ACTOR


def test_a_refused_promote_leaves_the_candidate_intact(project_service_local, build_svc):
    """A refusal BEFORE the deploy (here: the in-flight guard) is not a consumption either."""
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    # The first promote consumed the candidate, so re-arm it to isolate the guard.
    project_service_local._local_repos["r-1"] = _repo(
        cicd_status="promoting", last_promoted_at=FIXED_TS
    )
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-2")
    assert ei.value.kind == "promote_in_flight"
    assert project_service_local.get_repo("r-1").prod_candidate_image_tag == CANDIDATE_TAG


def test_a_merge_landing_during_the_deploy_is_not_erased(project_service_local, build_svc):
    """COMPARE-AND-CLEAR (E27A/T5 FIX 3). ``start_runtime_build`` blocks for seconds; a merge to
    ``main`` inside that window registers a NEWER candidate. Clearing off the record read BEFORE
    the build would erase it with no error anywhere — the Promote button goes quiet and nobody is
    told a merge is waiting. So all five fields must still hold the NEW values.

    The interleaving is simulated where it really happens: from the build service's own
    ``side_effect``, i.e. mid-flight between promote's read and promote's save."""
    NEWER_TAG = "a-1-tree999"
    NEWER_SHA = "9" * 40

    def _merge_lands_mid_build(**kwargs):
        project_service_local.record_prod_candidate(
            "a-1", image_tag=NEWER_TAG, sha=NEWER_SHA, actor="later-merger"
        )
        return "build-1"

    build_svc.start_runtime_build.side_effect = _merge_lands_mid_build
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    stored = project_service_local.get_repo("r-1")
    assert stored.prod_candidate_image_tag == NEWER_TAG
    assert stored.prod_candidate_sha == NEWER_SHA
    assert stored.prod_candidate_actor == "later-merger"
    assert stored.prod_candidate_status == "pending"
    assert stored.prod_candidate_at
    # …and the promotion that DID run is still fully ATTRIBUTED — it genuinely happened, so only
    # the CLEARING is conditional, never the attribution stamp. The image tag is a separate
    # question (E28A/T6): it records what prod SERVES, which this promote has not yet proven.
    assert stored.last_promoted_by == "oid-1"
    assert stored.last_promotion_build_id == "build-1"
    assert stored.cicd_status == "promoting"
    # The shipped artifact is on the delivery row, which the compare-and-clear cannot touch.
    (row,) = project_service_local.list_deployments("r-1", stage="prod")
    assert row.image_tag == CANDIDATE_TAG


def test_promote_does_not_rewrite_the_dev_tag_when_clearing_the_candidate(
    project_service_local, build_svc
):
    """The candidate opt-in must not widen into the CodeBuild-owned attribute: the save that
    CLEARS the candidate still leaves ``last_dev_image_tag`` (and thus the FE's 'what dev
    runs') alone."""
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert project_service_local.get_repo("r-1").last_dev_image_tag == DEV_TAG


# --- E28A/T6: `last_promoted_image_tag` means "what prod SERVES" --------------
#
# OBSERVED LIVE: a promote failed at `terraform apply`, yet the record claimed prod served that
# tag. The Deployments tab said "Nothing is serving here" — the store and the UI disagreed, and
# the store was the one lying. `_stamp_promotion` ran the moment `start_runtime_build` returned,
# which only means CodeBuild ACCEPTED the build.
#
# The fix makes ONLY the tag provisional. `by`/`at`/`build_id` keep being stamped where they
# were: they attribute the AUTHORIZATION (true at that instant), and `last_promoted_at` is
# `_promotion_in_flight`'s clock — deferring it would disarm the concurrency guard for the whole
# build duration, which is why moving the whole stamp was rejected.


def test_a_promote_whose_build_never_succeeds_does_not_claim_prod_serves_the_tag(
    project_service_local, build_svc
):
    """THE finding. The promote starts a build and nothing ever reports success, so nothing may
    assert that production is running that image.

    The BEFORE value is captured rather than assumed: asserting ``is None`` alone would also pass
    if the field had simply never been populated, which is not the claim. The claim is that this
    promote did not CHANGE it — while the three attribution fields, which are true the instant the
    authorization happens, ARE written."""
    before = project_service_local.get_repo("r-1").last_promoted_image_tag

    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")

    stored = project_service_local.get_repo("r-1")
    assert stored.last_promoted_image_tag == before  # unchanged — the apply has not succeeded
    assert stored.last_promoted_by == "oid-1"
    assert stored.last_promotion_build_id == "build-1"
    assert stored.last_promoted_at
    # …and the attempt is fully recoverable from the history, which is where "what we tried"
    # lives now.
    (row,) = project_service_local.list_deployments("r-1", stage="prod")
    assert row.image_tag == CANDIDATE_TAG and row.build_id == "build-1"


def test_a_promote_of_an_ALREADY_SUCCEEDED_tag_does_claim_prod_serves_it(
    project_service_local, build_svc
):
    """The other side of the gate, so the flag is not merely "never write the tag".

    The write is gated on the EXISTING ``_has_succeeded`` helper (already the rollback gate),
    not on a second mechanism. A tag with a SUCCEEDED prod row for this repo is one production
    has demonstrably served, so re-promoting it may repoint the cache."""
    project_service_local.append_deployment(
        repo_id="r-1",
        agent_id="a-1",
        stage="prod",
        image_tag=CANDIDATE_TAG,
        outcome=DeploymentOutcome.SUCCEEDED,
        completed_at=FIXED_TS,
    )

    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")

    assert project_service_local.get_repo("r-1").last_promoted_image_tag == CANDIDATE_TAG


def test_the_in_flight_guard_still_arms_after_a_promote(project_service_local, build_svc):
    """The reason the whole stamp could not be deferred. ``_promotion_in_flight`` is measured
    FROM ``last_promoted_at``, so if T6 had moved that field to the build's terminal path the
    guard would read "not in flight" for the entire build and a second promote could race the
    same Terraform state key.

    Staged the way the guard is really met: promote, then re-arm the candidate the promote
    consumed, so the SECOND call is refused by the guard rather than by ``no_prod_candidate``
    (which would pass whether or not the guard armed at all)."""
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    stored = project_service_local.get_repo("r-1")
    assert stored.cicd_status == "promoting" and stored.last_promoted_at  # both halves armed

    project_service_local.record_prod_candidate(
        "a-1", image_tag="a-1-tree888", sha="a" * 40, actor="merger-login"
    )
    build_svc.start_runtime_build.reset_mock()
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-2")
    assert ei.value.kind == "promote_in_flight"
    build_svc.start_runtime_build.assert_not_called()


def test_the_provisional_tag_gate_reads_the_repo_and_stage_scoped_history(
    project_service_local, build_svc
):
    """The gate must not be satisfied by a SUCCEEDED row belonging to a different repo or a
    non-prod stage. Both are realistic: two repos share one tenant ECR registry (so another
    repo's tag is a resolvable image), and the same artifact routinely succeeds on dev first —
    dev succeeding proves nothing about what PRODUCTION serves."""
    for over in ({"repo_id": "r-OTHER"}, {"stage": "dev"}):
        project_service_local._local_repos["r-1"] = _repo()  # reset the consumed candidate
        project_service_local.append_deployment(
            **{
                "repo_id": "r-1",
                "agent_id": "a-1",
                "stage": "prod",
                "image_tag": CANDIDATE_TAG,
                "outcome": DeploymentOutcome.SUCCEEDED,
                "completed_at": FIXED_TS,
                **over,
            }
        )

        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")

        assert project_service_local.get_repo("r-1").last_promoted_image_tag is None, over


# --- service-level failure discipline ---------------------------------------


def test_success_persists_audit_fields(project_service_local, build_svc):
    """Asserted against a RE-READ, never the returned object.

    The returned record is mutated in memory before the save, so asserting it cannot tell a
    persisted transition from a dropped one: with ``include_cicd_status=True`` removed the
    return value still says ``promoting`` while the STORED record stays ``deployed`` — exactly
    T7's clobber class of bug, and the FE polls the stored record."""
    build_svc.start_runtime_build.return_value = "build-123"
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    stored = project_service_local.get_repo("r-1")
    assert stored.cicd_status == "promoting"
    assert stored.last_promoted_by == "oid-1"
    assert stored.last_promotion_build_id == "build-123"
    assert stored.last_promoted_at
    # E28A/T6: the TAG is NOT stamped here. `start_runtime_build` returning only proves
    # CodeBuild ACCEPTED the build; the terraform apply may fail minutes later, and
    # `last_promoted_image_tag` means "what prod SERVES". The other three are true at this
    # instant (who authorized, when, which build) and are stamped unconditionally.
    assert stored.last_promoted_image_tag is None
    # T7's split, pinned from the promote side: the tag CodeBuild owns is never rewritten.
    assert stored.last_dev_image_tag == DEV_TAG


def test_repo_from_another_project_cannot_be_promoted(project_service_local, build_svc):
    """The cross-project (IDOR) guard: an OWNER of proj-1 cannot promote a repo that lives
    under a different project. This guard is the only thing standing between a project-scoped
    OWNER role and a prod deploy of someone else's repo."""
    project_service_local._local_repos["r-2"] = _repo(id="r-2", project_id="proj-OTHER")
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-2", promoted_by="oid-1")
    assert ei.value.kind == "not_found"
    build_svc.start_runtime_build.assert_not_called()


def test_build_failure_stamps_actor_and_persists_failed_before_raising(
    project_service_local, build_svc
):
    from services.runtime_build_service import RuntimeBuildError

    build_svc.start_runtime_build.side_effect = RuntimeBuildError("nope", kind="codebuild")  # kind is keyword-only
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert ei.value.kind == "promote_failed"
    stored = project_service_local.get_repo("r-1")
    assert stored.cicd_status == "failed"
    assert stored.last_promoted_by == "oid-1"          # attributed even on failure
    assert stored.last_promoted_image_tag == CANDIDATE_TAG


def _connection_error():
    """The connection service's OWN error type — ``start_runtime_build``'s FIRST statement is
    ``self._connections.get_connection(...)`` and its docstring explicitly delegates this
    mapping to the caller. Reachable without a deleted connection: ``ConnectionService._get``
    swallows a ``ClientError`` and returns ``None``, which ``get_connection`` turns into
    ``not_found`` — so a transient DynamoDB throttle takes this path."""
    from services.connection_service import ConnectionError as ConnError

    return ConnError("Unknown connection", kind="not_found")


def _malformed_agent_error():
    """``self._registry.get(agent_id)`` — the SECOND statement of ``start_runtime_build``."""
    from services.agent_registry_service import MalformedAgentRecordError

    return MalformedAgentRecordError("agent record has no inlineContent")


def _registry_client_error():
    """A throttled / IAM-denied registry read. ``AgentRegistryService._hydrate`` RE-RAISES
    every ``ClientError`` whose code is not ``ResourceNotFoundException``
    (``agent_registry_service.py:199-202``), so a ``ThrottlingException`` on
    ``GetRegistryRecord`` — the single most likely runtime fault on this route — escapes
    ``start_runtime_build`` as a raw ``ClientError``. Verified against the REAL call graph
    (real registry service, only the boto client faked)."""
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
        "GetRegistryRecord",
    )


def _botocore_transport_error():
    """A transport fault needs no store fault at all — a network blip inside the ECS task
    surfaces as a ``BotoCoreError`` subclass, which nothing in the registry filters."""
    from botocore.exceptions import EndpointConnectionError

    return EndpointConnectionError(endpoint_url="https://bedrock-agentcore-control")


def _unknown_registry_status_error():
    """The Agent Registry is a PREVIEW service: a status value outside
    ``STATUS_TO_LIFECYCLE`` raises this from ``Agent.from_record`` inside ``_hydrate``. It is
    a SIBLING of ``MalformedAgentRecordError`` under ``ValueError``, not a subclass, so the
    old enumerated tuple missed it. Its own docstring says drift should "fail loudly with
    context" — on this route that must not mean an opaque 500 with a lost audit record."""
    from models.agent import UnknownRegistryStatusError

    return UnknownRegistryStatusError("Unknown registry status 'ACTIVE'")


def _pydantic_validation_error():
    """Raised by ``Agent.from_record``'s ``cls(...)`` on any envelope value the model rejects,
    and by ``TenantService._from_item``'s ``Tenant.model_validate`` on a poisoned stored item
    (that one is raised INSIDE the build service's try but is not in its ``(TenantError,
    KeyError)`` tuple, so it escapes the service's own wrapper). A ``ValueError`` subclass."""
    from models.agent import Agent

    try:
        Agent(id="a-1", name="n", purpose="p", platform="NOT_A_PLATFORM")
    except ValidationError as err:
        return err
    raise AssertionError("expected a pydantic ValidationError")  # pragma: no cover


# Every type below was confirmed REACHABLE by probing the real `start_runtime_build` call
# graph (real RuntimeBuildService + real AgentRegistryService + real TenantService, only the
# boto3 clients faked); each previously escaped as an unmapped 500 with the record still
# reading `deployed` and the prod-promotion attempt UNATTRIBUTED.
_BUILD_START_FAILURES = [
    _connection_error,
    _malformed_agent_error,
    _registry_client_error,
    _botocore_transport_error,
    _unknown_registry_status_error,
    _pydantic_validation_error,
]


@pytest.mark.parametrize("make_exc", _BUILD_START_FAILURES)
def test_non_runtime_build_failures_also_stamp_and_persist_failed(
    project_service_local, build_svc, make_exc
):
    """EVERY failure to start the deploy shares one discipline (E27/T8 fix).

    ``start_runtime_build`` resolves the connection, the agent (through the registry) and the
    tenant before it ever reaches its own ``RuntimeBuildError``, so a whole family of
    dependency faults precedes it. Any catch that misses one lets it escape as a 500 AND skips
    the stamp entirely — leaving the record still reading ``deployed`` with
    ``last_promoted_by=None``, i.e. an ATTEMPTED PROD PROMOTION WITH NO ATTRIBUTION.

    Parametrized over every type confirmed reachable from that call graph, so the catch tuple
    cannot be narrowed back one term at a time without a failure."""
    build_svc.start_runtime_build.side_effect = make_exc()
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert ei.value.kind == "promote_failed"          # ⇒ 502, not an unmapped 500
    stored = project_service_local.get_repo("r-1")
    assert stored.cicd_status == "failed"
    assert stored.last_promoted_by == "oid-1"          # attributed even on failure
    assert stored.last_promoted_image_tag == CANDIDATE_TAG


@pytest.mark.parametrize("make_exc", _BUILD_START_FAILURES)
def test_non_runtime_build_failures_map_to_502_not_500(
    client_factory, project_svc, build_svc, role_svc, make_exc
):
    """The route twin of the above: the contract's 502 literal, never a 500."""
    build_svc.start_runtime_build.side_effect = make_exc()
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory(project_role="owner", roles={"proj-1": "owner"})
    r = client.post("/api/v1/projects/proj-1/repos/r-1/promote")
    assert r.status_code == 502
    assert r.json()["detail"] == "failed to start the promotion build"


def test_programming_errors_are_NOT_recorded_as_a_failed_promotion(
    project_service_local, build_svc
):
    """The catch is ENUMERATED, not ``except Exception``: an AGP bug must surface as a bug
    rather than be laundered into a plausible-looking ``failed`` promotion record."""
    build_svc.start_runtime_build.side_effect = TypeError("start_runtime_build() got …")
    with pytest.raises(TypeError):
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")


# --- the bounded in-flight guard (E27/T8 — I4 + I5 answered together) --------


def test_second_promote_while_in_flight_is_409(project_service_local, build_svc):
    """Two concurrent promotes would start two CodeBuild runs racing the same Terraform state
    key. The first promote stamps ``promoting`` + ``last_promoted_at``, so the second refuses.

    E27A staging note — the second promote needs a candidate to even reach this guard, since
    the first CONSUMED the one it shipped. A NEW merge to ``main`` landing mid-promotion is
    exactly how that happens in practice, and it is why the guard is still load-bearing after
    the narrowing: without it, a merge arriving during a promotion would let a second deploy
    start against the same state key."""
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    build_svc.start_runtime_build.reset_mock()
    # a newer merge to `main` while the first promotion is still running
    project_service_local.record_prod_candidate(
        "a-1", image_tag="a-1-tree888", sha="8" * 40, actor="second-merger"
    )
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-2")
    assert ei.value.kind == "promote_in_flight"
    build_svc.start_runtime_build.assert_not_called()
    # …and the newer candidate is still there to promote once the first deploy settles.
    assert project_service_local.get_repo("r-1").prod_candidate_image_tag == "a-1-tree888"


def test_in_flight_promote_is_409_over_the_route(
    client_factory, project_svc, build_svc, role_svc
):
    project_svc.get_repo.return_value = _repo(cicd_status="promoting", last_promoted_at=FIXED_TS)
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory(project_role="owner", roles={"proj-1": "owner"})
    r = client.post("/api/v1/projects/proj-1/repos/r-1/promote")
    assert r.status_code == 409
    assert r.json()["detail"] == "a promotion is already in flight"
    build_svc.start_runtime_build.assert_not_called()


def test_a_stuck_promoting_record_is_promotable_again_after_the_window(build_svc):
    """The guard is BOUNDED, and this is why it may exist at all.

    A record can stick at ``promoting`` forever (the buildspec's status write is
    ``2>/dev/null || true`` and no-ops on an unset ``REPO_SK``, and there is no reconciler).
    An unconditional guard would make such a repo PERMANENTLY unpromotable — strictly worse
    than the double deploy it prevents. Past the CodeBuild build timeout the build cannot
    still be running, so a retry is allowed."""
    stale = (FIXED - timedelta(minutes=61)).isoformat()
    svc = _real_service(build_svc)
    svc._local_repos["r-1"] = _repo(cicd_status="promoting", last_promoted_at=stale)
    repo = svc.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert repo.cicd_status == "promoting"
    assert build_svc.start_runtime_build.call_count == 1


def test_promoting_without_a_timestamp_does_not_block(build_svc):
    """The guard fails OPEN on a missing/unparseable ``last_promoted_at`` (a pre-T8 record, or
    a partial write): being unable to date the attempt must not strand the repo."""
    svc = _real_service(build_svc)
    svc._local_repos["r-1"] = _repo(cicd_status="promoting", last_promoted_at=None)
    assert svc.promote_repo("proj-1", "r-1", promoted_by="oid-1").cicd_status == "promoting"
    svc._local_repos["r-2"] = _repo(
        id="r-2", cicd_status="promoting", last_promoted_at="not-a-date"
    )
    assert svc.promote_repo("proj-1", "r-2", promoted_by="oid-1").cicd_status == "promoting"


def test_a_future_dated_last_promoted_at_does_not_block_forever(build_svc):
    """The window is ``[0, 60min)`` — a NEGATIVE delta must not read as in-flight.

    ``now - started`` is negative for a future stamp, and a negative delta is always
    ``< 60min``, so an unclamped comparison refuses the promote until wall-clock catches up.
    A clock skew on the writing task, a restored/migrated row or a hand edit is enough to make
    a production repo PERMANENTLY unpromotable — the precise outcome the bounded guard exists
    to prevent, arriving through the one input the bound did not range-check."""
    svc = _real_service(build_svc)
    for repo_id, skew in (("r-1", timedelta(days=1)), ("r-2", timedelta(days=3650))):
        svc._local_repos[repo_id] = _repo(
            id=repo_id, cicd_status="promoting", last_promoted_at=(FIXED + skew).isoformat()
        )
        assert (
            svc.promote_repo("proj-1", repo_id, promoted_by="oid-1").cicd_status == "promoting"
        )
    assert build_svc.start_runtime_build.call_count == 2


def test_a_store_fault_on_the_failure_persist_still_yields_the_curated_error(
    project_service_local, build_svc
):
    """The failure path's audit write is BEST-EFFORT and must never downgrade the outcome.

    ``_save_repo`` propagates in DDB mode by design, and on the failure path it runs BEFORE
    the ``raise`` — so a throttled projects table replaced the contract's curated 502 with an
    unmapped ``ClientError`` → 500. The build failing and the table throttling is a correlated
    pair during a regional event, not two coincidences. The operator must still get the
    promotion's error, not the audit write's."""
    from botocore.exceptions import ClientError

    from services.runtime_build_service import RuntimeBuildError

    build_svc.start_runtime_build.side_effect = RuntimeBuildError("nope", kind="codebuild")
    project_service_local._save_repo = MagicMock(
        side_effect=ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "UpdateItem",
        )
    )
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert ei.value.kind == "promote_failed"          # ⇒ the mapped 502, never a 500
    assert ei.value.message == "promotion failed"
    # The write was still ATTEMPTED — swallowing it must not mean skipping it.
    project_service_local._save_repo.assert_called_once()


def test_promote_error_detail_never_leaks_internals(
    client_factory, build_svc, role_svc, project_svc
):
    from services.runtime_build_service import RuntimeBuildError

    build_svc.start_runtime_build.side_effect = RuntimeBuildError("arn:aws:secret:leak", kind="codebuild")
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory(project_role="owner", roles={"proj-1": "owner"})
    r = client.post("/api/v1/projects/proj-1/repos/r-1/promote")
    assert r.status_code == 502
    assert r.json()["detail"] == "failed to start the promotion build"
    assert "secret" not in r.text


# --------------------------------------------------------------------------- #
# E28B/T4 (D-B3) — promotion approves and deploys a DIGEST
# --------------------------------------------------------------------------- #
#
# The tag is the git TREE sha, so it is not even unique to a set of bytes: the image build is not
# reproducible (a floating base image, ranged deps, no lockfile), so rebuilding an unchanged tree
# produces the SAME tag over DIFFERENT bytes. That is why the approved artifact has to be named by
# digest — "the bytes an owner approved" and "the bytes prod runs" are otherwise related only by
# inference.

CANDIDATE_DIGEST = "sha256:" + "ab" * 32


def test_promote_deploys_the_APPROVED_DIGEST(project_service_local, build_svc):
    """The digest recorded on the candidate is what reaches the deploy — passed through verbatim,
    never re-resolved from the tag."""
    project_service_local._local_repos["r-1"] = _repo(prod_candidate_digest=CANDIDATE_DIGEST)
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    kwargs = build_svc.start_runtime_build.call_args.kwargs
    assert kwargs["image_digest"] == CANDIDATE_DIGEST
    # The tag still travels alongside it — the buildspec derives the agent id, the deployment id
    # and the scratch-secret name from the tag, so dropping it would break the build outright.
    assert kwargs["image_tag"] == CANDIDATE_TAG


def test_promote_never_re_derives_the_digest_from_the_registry(project_service_local, build_svc):
    """THE point of D-B3, asserted as a property of the DATA rather than of the call graph.

    A digest that does not match the tag's current registry content must still be the one deployed:
    the approval attests to BYTES, and re-resolving the tag at deploy time would ship whatever the
    (mutable) tag points at now. So the value the service passes must be exactly the stored one,
    even when it is unrelated to anything the tag would resolve to."""
    unrelated = "sha256:" + "de" * 32
    project_service_local._local_repos["r-1"] = _repo(prod_candidate_digest=unrelated)
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert build_svc.start_runtime_build.call_args.kwargs["image_digest"] == unrelated


def test_a_legacy_tag_only_candidate_still_promotes(project_service_local, build_svc):
    """A candidate registered before this epic (or by the still-live ``/prod-candidate`` route,
    which has no digest in scope) carries NO digest. It must still promote over the tag path —
    refusing would strand every pre-E28B candidate with no way to release it."""
    project_service_local._local_repos["r-1"] = _repo(prod_candidate_digest=None)
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    kwargs = build_svc.start_runtime_build.call_args.kwargs
    assert kwargs["image_digest"] is None
    assert kwargs["image_tag"] == CANDIDATE_TAG


def test_a_promote_clears_the_candidate_digest_with_the_rest_of_the_block(
    project_service_local, build_svc
):
    """The digest is consumed as part of the ONE unit. A leftover digest after a successful promote
    would let a second approval deploy bytes whose candidate had already been consumed."""
    project_service_local._local_repos["r-1"] = _repo(prod_candidate_digest=CANDIDATE_DIGEST)
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    stored = project_service_local.get_repo("r-1")
    assert stored.prod_candidate_digest is None
    assert stored.prod_candidate_image_tag is None  # …and it really was the whole block


def test_a_REBUILD_of_the_same_tree_is_not_erased_by_the_compare_and_clear(
    project_service_local, build_svc
):
    """The compare-and-clear must key on the DIGEST too, and this is the case that proves why.

    A rebuild of an unchanged tree produces the SAME tag and the SAME commit sha over DIFFERENT
    bytes — not an edge case but the expected outcome of a non-reproducible build. Comparing only
    tag+sha would read such a rebuild as "still the candidate we shipped" and CLEAR it, silently
    discarding an artifact no owner ever approved. The newer digest must survive."""
    project_service_local._local_repos["r-1"] = _repo(prod_candidate_digest=CANDIDATE_DIGEST)
    rebuilt = "sha256:" + "cd" * 32

    def _rebuild_lands_mid_build(**kwargs):
        # Same tag, same sha, DIFFERENT bytes — the whole point.
        project_service_local.record_prod_candidate(
            "a-1", image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR,
            image_digest=rebuilt,
        )
        return "build-1"

    build_svc.start_runtime_build.side_effect = _rebuild_lands_mid_build
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")

    stored = project_service_local.get_repo("r-1")
    assert stored.prod_candidate_digest == rebuilt, (
        "the rebuild's candidate was erased — the compare-and-clear is not comparing the digest"
    )
    assert stored.prod_candidate_status == "pending"
    # …and the promotion that DID run is still attributed to the digest it actually shipped.
    assert stored.last_promoted_by == "oid-1"


def test_the_promoted_digest_is_recorded_only_for_an_image_prod_has_RUN(
    project_service_local, build_svc
):
    """``last_promoted_digest`` means "what prod SERVES", so it rides the same `serving` gate as
    the tag rather than being stamped the moment CodeBuild accepts the build.

    On a FIRST promote nothing has succeeded in prod yet, so neither scalar is written — the honest
    answer, and the same asymmetry E28A/T6 established for the tag. Splitting the gate would let
    the pair disagree, which is worse than both being absent."""
    project_service_local._local_repos["r-1"] = _repo(prod_candidate_digest=CANDIDATE_DIGEST)
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    stored = project_service_local.get_repo("r-1")
    assert stored.last_promoted_digest is None
    assert stored.last_promoted_image_tag is None  # the tag makes the same claim, so it agrees
