"""Governed prod promotion: ``POST /projects/{id}/repos/{repo_id}/promote``.

An OWNER clicks Promote and the backend resolves which image goes to prod. No image tag is ever entered
by a user: promote deploys the prod candidate registered when something merged to ``main``, so the
promote surface has no tag-injection input at all. The tests pin two things that are easy to lose: the
tag comes from the record (a client-supplied one is ignored), and the OWNER gate refuses before any
deploy is started.

Promotion approves and deploys a digest. The tag is the git tree sha and the image build is not
reproducible (a floating base image, ranged deps, no lockfile), so rebuilding an unchanged tree produces
the same tag over different bytes; naming the approved artifact by digest is what makes "the bytes an
owner approved" and "the bytes prod runs" one fact. ``dev_outcome_for_candidate`` is the same dev-only
fact ``promote_repo`` gates on, read back onto the response so the frontend's own gate cannot drift from
the backend's; it is pinned on ``get_repo``, ``list_repositories`` and ``get_project``, the three read
paths the repo list and detail surfaces poll.

Harness idiom is ``test_projects_role_gating.py``: the module reset, the Entra env fixture, fake
resolvers on the ``users`` module globals (the one resolver singletons), and no ``dependency_overrides``;
the real ``require_role``, ``current_principal``, ``get_tenant_ctx`` and ``get_project_ctx`` chain runs
against a mocked ``verify_entra_token``.

Unlike that file, the ``ProjectService`` here is the real in-memory one (``table_name=""``) with its
collaborators mocked, because the whole contract under test is what the service resolves and persists,
which a ``MagicMock`` would answer for. Only the single-repo read is a mock in the route tests, so a test
can vary the stored record without hand-seeding a row. The runtime-build service is a mock: nothing here
may reach CodeBuild, Secrets Manager, GitHub or Entra. Every test runs under ``passing_eval_gate`` so the
eval gate (``test_promote_eval_gate.py``) stays out of the way.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from seed import seed

from models.deployment import Deployment, DeploymentOutcome
from models.project import Project
from models.project_role import ROLE_NAMES, ProjectRoleRecord
from models.repository import CicdStatus, Repository
from services.project_errors import ProjectError
from services.project_service import ProjectService
from services.project_store import _PROD_CANDIDATE_FIELDS, _PROJECT_PK, _REPOSITORY_PK, _to_item

pytestmark = pytest.mark.usefixtures("reset_modules", "passing_eval_gate")

FIXED = datetime(2026, 7, 27, tzinfo=timezone.utc)
FIXED_TS = FIXED.isoformat()

DEV_TAG = "a-1-abc1234"
CANDIDATE_TAG = "a-1-tree777"
CANDIDATE_SHA = "f" * 40
CANDIDATE_ACTOR = "merger-login"
_CANDIDATE_FIELDS = _PROD_CANDIDATE_FIELDS
INSUFFICIENT = "insufficient project role"


def test_the_candidate_block_read_from_the_service_carries_the_digest():
    """The field list is imported, not restated: a local copy stopped matching when the block grew
    ``prod_candidate_digest``, and the clears-the-whole-block loop would have kept passing while a
    leftover digest let a second approval deploy an already-consumed candidate."""
    assert "prod_candidate_digest" in _CANDIDATE_FIELDS


def _governed_row(pid="proj-1", principal="someone-else-oid", role="owner"):
    """A role row on ``pid`` — its mere EXISTENCE makes the project 'governed'."""
    return ProjectRoleRecord(
        project_id=pid,
        principal_id=principal,
        principal_type="user",
        principal_display="Alex",
        role=role,
        granted_by="seed",
        granted_at=FIXED_TS,
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
    last_dev_image_tag: str | None = DEV_TAG,
    cicd_status: CicdStatus = "deployed",
    last_promoted_at=None,
    prod_candidate_image_tag: str | None = CANDIDATE_TAG,
    prod_candidate_digest=None,
):
    """A repo with a pending prod candidate, so it has something promotable.

    It also carries ``last_dev_image_tag``, because the two coexist on a real record: dev is running one
    image while ``main`` offers another, and promote must read the candidate. ``cicd_status`` and
    ``last_promoted_at`` are overridable so a test can stage the in-flight and the stuck-at-``promoting``
    states the bounded guard distinguishes; ``prod_candidate_image_tag`` so a test can stage the
    no-candidate refusal. ``prod_candidate_digest`` defaults to ``None`` so the tag-only candidate path,
    which a pre-digest row and a rollback still travel, stays exercised; the digest cases pass it
    explicitly. A repo with no candidate tag has no candidate at all: the fields are written and cleared
    as one unit, so a half-populated candidate is a state the service cannot produce."""
    candidate: dict[str, Any] = (
        {
            "prod_candidate_image_tag": prod_candidate_image_tag,
            "prod_candidate_digest": prod_candidate_digest,
            "prod_candidate_sha": CANDIDATE_SHA,
            "prod_candidate_actor": CANDIDATE_ACTOR,
            "prod_candidate_at": FIXED_TS,
            "prod_candidate_status": "pending",
        }
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

        assert projects_module._role_svc is not None
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


def _real_service(build_svc, *, tenant_id="default"):
    """The REAL in-memory ``ProjectService`` with every collaborator mocked.

    ``table_name=""`` selects the in-memory store. The parent project is
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
    seed(svc._store, _to_item(_PROJECT_PK, _project(tenant_id=tenant_id)))
    return svc


@pytest.fixture
def project_service_local(build_svc):
    """Service-level fixture: NOTHING mocked on the read path, so the persisted outcome of
    a promote (success or failure) is asserted against real stored state."""
    svc = _real_service(build_svc)
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo()))
    return svc


@pytest.fixture
def client_foreign_tenant(client_factory):
    """A caller whose tenant does NOT include the project's, holding NO project role.

    Both conditions at once is what makes the gate ORDER observable: the two gates DISAGREE
    (tenant-first says 404, role-first says 403), so the observed status distinguishes the
    order rather than merely happening to be 404. A 403 here would CONFIRM that a foreign
    tenant's project exists."""
    return client_factory(project_role=None, tenant_ctx=_tenant_context(tenant_ids=["ten-OTHER"]))


def test_owner_promotes_with_backend_resolved_tag(client_factory, project_svc, build_svc, role_svc):
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory(project_role="owner", roles={"proj-1": "owner"})
    r = client.post("/api/v1/projects/proj-1/repos/r-1/promote")
    assert r.status_code == 202
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
    build_svc.start_runtime_build.assert_not_called()


def test_ungoverned_project_still_requires_a_real_owner(client_factory, build_svc, role_svc):
    """Promote does not ride the ungoverned-project allowance: an unowned project cannot be promoted to
    prod by a mere tenant member. The store is never even consulted, which is what pins the strict gate
    rather than the permissive one."""
    role_svc.list_for_project.return_value = []
    client = client_factory(project_role=None)
    assert client.post("/api/v1/projects/proj-1/repos/r-1/promote").status_code == 403
    build_svc.start_runtime_build.assert_not_called()
    role_svc.has_role_rows.assert_not_called()


def test_a_missing_dev_tag_no_longer_blocks_promote(client_factory, project_svc, build_svc, role_svc):
    """Promote depends on the candidate alone: the record of what dev is *running* is not a precondition
    for shipping what ``main`` offers, so a repo whose dev deploy was never recorded (a swallowed
    buildspec write, a prod-only history) is still promotable once something has merged to ``main``."""
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


def test_promote_deploys_the_candidate_not_the_dev_tag(project_service_local, build_svc):
    """With dev running one image and ``main`` offering another, prod gets ``main``'s: reading
    ``last_dev_image_tag`` would ship an artifact that was never reviewed or merged. The fixture keeps
    the two tags distinguishable so the assertion can tell which field the service read."""
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert build_svc.start_runtime_build.call_args.kwargs["image_tag"] == CANDIDATE_TAG
    assert CANDIDATE_TAG != DEV_TAG


def test_no_candidate_is_no_prod_candidate(project_service_local, build_svc):
    """Nothing has merged to ``main`` since the last promotion ⇒ there is nothing to approve.
    Refused before the deploy, with the kind the route maps to 409."""
    seed(project_service_local._store, _to_item(_REPOSITORY_PK, _repo(prod_candidate_image_tag=None)))
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert ei.value.kind == "no_prod_candidate"
    build_svc.start_runtime_build.assert_not_called()


def test_an_empty_candidate_tag_is_also_no_prod_candidate(project_service_local, build_svc):
    """Kept falsy-or-missing, never ``is None``: an empty tag would deploy ``<ecr_repo>:`` to production,
    so the guard must not narrow to a ``None`` check just because a different field is now read."""
    seed(project_service_local._store, _to_item(_REPOSITORY_PK, _repo(prod_candidate_image_tag="")))
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert ei.value.kind == "no_prod_candidate"
    build_svc.start_runtime_build.assert_not_called()


def test_an_unpromotable_repo_on_an_unwired_platform_is_told_the_409_not_the_502():
    svc = _real_service(None)
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo(prod_candidate_image_tag="")))
    with pytest.raises(ProjectError) as ei:
        svc.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert ei.value.kind == "no_prod_candidate"
    assert ProjectError.kinds[ei.value.kind].status == 409


def test_a_candidate_whose_dev_deploy_failed_is_refused(project_service_local, build_svc):
    """A trunk push registers its candidate the instant the build starts, before terraform apply has a
    result, so a repo can offer "Promote to prod" for a candidate whose dev deploy failed; promoting it
    would ship bytes nobody has seen run anywhere. Refused with ``no_dev_build``, the same kind (and 409)
    as "nothing has deployed to dev yet". The gate reads the dev-stage ``Deployment`` row for this
    candidate's own tag, not the repo-wide ``cicd_status``, which is left at its default ``deployed``
    here to prove the refusal rests on the dev-scoped fact."""
    seed(project_service_local._store, _to_item(_REPOSITORY_PK, _repo()))
    project_service_local.append_deployment(
        repo_id="r-1",
        agent_id="a-1",
        stage="dev",
        image_tag=CANDIDATE_TAG,
        outcome=DeploymentOutcome.FAILED,
        completed_at=FIXED_TS,
    )
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert ei.value.kind == "no_dev_build"
    build_svc.start_runtime_build.assert_not_called()


def test_a_failed_prod_promote_then_retry_is_allowed(project_service_local, build_svc):
    """``cicd_status`` is one field shared by every stage: a failed prod promote (or a failed rollback)
    persists ``cicd_status="failed"`` on the record, and a gate reading that field would refuse the very
    next promote of a candidate whose dev deploy was fine. The dev-scoped gate is not fooled by a
    prod-stage failure."""
    seed(project_service_local._store, _to_item(_REPOSITORY_PK, _repo(cicd_status="failed")))
    project_service_local.append_deployment(
        repo_id="r-1",
        agent_id="a-1",
        stage="dev",
        image_tag=CANDIDATE_TAG,
        outcome=DeploymentOutcome.SUCCEEDED,
        completed_at=FIXED_TS,
    )
    repo = project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    build_svc.start_runtime_build.assert_called_once()
    assert repo.cicd_status == "promoting"


def test_an_old_dev_failure_then_a_newer_dev_success_is_allowed(project_service_local, build_svc):
    """The gate reads the NEWEST dev row for this tag, not "any dev row ever". A retried dev
    build for the same candidate (STARTED → FAILED → re-pushed → STARTED → SUCCEEDED) must
    read as promotable once the newer attempt lands, not be stuck on its own first failure."""
    from models.deployment import deployment_seq_key

    seed(project_service_local._store, _to_item(_REPOSITORY_PK, _repo()))
    old_ts, new_ts = "2026-07-26T00:00:00+00:00", "2026-07-27T12:00:00+00:00"
    for started_at, outcome, suffix in (
        (old_ts, DeploymentOutcome.FAILED, "aaaa"),
        (new_ts, DeploymentOutcome.SUCCEEDED, "bbbb"),
    ):
        dep_id = f"dep-{suffix}"
        project_service_local._save_deployment(
            Deployment(
                id=dep_id,
                repo_id="r-1",
                agent_id="a-1",
                stage="dev",
                seq_key=deployment_seq_key("r-1", "dev", started_at, dep_id),
                image_tag=CANDIDATE_TAG,
                outcome=outcome,
                started_at=started_at,
                completed_at=started_at,
            )
        )
    repo = project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    build_svc.start_runtime_build.assert_called_once()
    assert repo.cicd_status == "promoting"


def test_a_record_with_neither_candidate_nor_dev_tag_refuses_without_leaking_a_500(
    project_service_local, build_svc
):
    """A row with neither a candidate nor a dev tag refuses cleanly with a mapped kind rather than raise
    on a missing attribute."""
    seed(
        project_service_local._store,
        _to_item(_REPOSITORY_PK, _repo(prod_candidate_image_tag=None, last_dev_image_tag=None)),
    )
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert ei.value.kind == "no_prod_candidate"
    build_svc.start_runtime_build.assert_not_called()


def test_a_successful_promote_clears_all_six_candidate_fields(project_service_local, build_svc):
    """The candidate is CONSUMED by the approval, so a second Promote on the same merge is
    refused rather than re-deploying the same image (and rather than leaving a permanently
    'pending' badge in the UI). Asserted against a RE-READ: clearing the in-memory record
    without a save that OPTS IN to the candidate fields would leave the stored row pending. What was
    shipped is not lost by the clearing: ``last_promoted_image_tag`` means "what prod serves" and is
    written only once a delivery has succeeded, so the artifact offered is asserted on the append-only
    delivery row."""
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    stored = project_service_local.get_repo("r-1")
    for field in _CANDIDATE_FIELDS:
        assert getattr(stored, field) is None, field
    assert stored.last_promotion_build_id == "build-1"
    (row,) = project_service_local.list_deployments("r-1", stage="prod")
    assert row.image_tag == CANDIDATE_TAG


def test_a_failed_promote_leaves_the_candidate_intact(project_service_local, build_svc):
    """A failed promote must stay RETRYABLE. Clearing the candidate on the failure path would
    turn a transient CodeBuild/registry fault into a permanent one: the record would 409
    ``no_prod_candidate`` forever and the only recovery would be pushing a new commit to
    ``main`` — the operator would have to fake a merge to retry a deploy."""
    from services.runtime_build_service import RuntimeBuildError

    build_svc.start_runtime_build.side_effect = RuntimeBuildError("nope", kind="start_failed")
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert ei.value.kind == "promote_failed"
    stored = project_service_local.get_repo("r-1")
    assert stored.cicd_status == "failed"
    assert stored.prod_candidate_image_tag == CANDIDATE_TAG
    assert stored.prod_candidate_status == "pending"
    assert stored.prod_candidate_sha == CANDIDATE_SHA
    assert stored.prod_candidate_actor == CANDIDATE_ACTOR


def test_a_refused_promote_leaves_the_candidate_intact(project_service_local, build_svc):
    """A refusal before the deploy (here the in-flight guard) is not a consumption either; the first
    promote consumed the candidate, so it is re-armed to isolate the guard."""
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    seed(
        project_service_local._store,
        _to_item(_REPOSITORY_PK, _repo(cicd_status="promoting", last_promoted_at=FIXED_TS)),
    )
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-2")
    assert ei.value.kind == "promote_in_flight"
    assert project_service_local.get_repo("r-1").prod_candidate_image_tag == CANDIDATE_TAG


def test_a_merge_landing_during_the_deploy_is_not_erased(project_service_local, build_svc):
    """Compare-and-clear. ``start_runtime_build`` blocks for seconds; a merge to ``main`` inside that
    window registers a newer candidate. Clearing off the record read before the build would erase it
    with no error anywhere (the Promote button goes quiet and nobody is told a merge is waiting), so
    every candidate field must still hold the new values. The interleaving is simulated where it really
    happens: from the build service's own ``side_effect``, mid-flight between promote's read and save.

    The promotion that did run is still fully attributed, since only the clearing is conditional, never
    the attribution stamp; the image tag records what prod serves, which this promote has not yet
    proven, and the shipped artifact is on the delivery row, which the compare-and-clear cannot touch."""
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
    assert stored.last_promoted_by == "oid-1"
    assert stored.last_promotion_build_id == "build-1"
    assert stored.cicd_status == "promoting"
    (row,) = project_service_local.list_deployments("r-1", stage="prod")
    assert row.image_tag == CANDIDATE_TAG


def test_promote_does_not_rewrite_the_dev_tag_when_clearing_the_candidate(project_service_local, build_svc):
    """The candidate opt-in must not widen into the CodeBuild-owned attribute: the save that
    CLEARS the candidate still leaves ``last_dev_image_tag`` (and thus the FE's 'what dev
    runs') alone."""
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert project_service_local.get_repo("r-1").last_dev_image_tag == DEV_TAG


def test_a_promote_whose_build_never_succeeds_does_not_claim_prod_serves_the_tag(
    project_service_local, build_svc
):
    """``last_promoted_image_tag`` means "what prod serves": the promote starts a build and nothing ever
    reports success, so nothing may assert that production is running that image. ``_stamp_promotion``
    runs the moment ``start_runtime_build`` returns, which only means CodeBuild accepted the build, so
    only the tag is provisional; ``by``, ``at`` and ``build_id`` attribute the authorization, true at that
    instant, and are written.

    The before value is captured rather than assumed: asserting ``is None`` alone would also pass if the
    field had simply never been populated, which is not the claim. The claim is that this promote did
    not change it, and that the attempt is recoverable from the history, where "what we tried" lives."""
    before = project_service_local.get_repo("r-1").last_promoted_image_tag

    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")

    stored = project_service_local.get_repo("r-1")
    assert stored.last_promoted_image_tag == before
    assert stored.last_promoted_by == "oid-1"
    assert stored.last_promotion_build_id == "build-1"
    assert stored.last_promoted_at
    (row,) = project_service_local.list_deployments("r-1", stage="prod")
    assert row.image_tag == CANDIDATE_TAG and row.build_id == "build-1"


def test_a_promote_of_an_ALREADY_SUCCEEDED_tag_does_claim_prod_serves_it(project_service_local, build_svc):
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
    FROM ``last_promoted_at``, so had that field moved to the build's terminal path the
    guard would read "not in flight" for the entire build and a second promote could race the
    same Terraform state key.

    Staged the way the guard is really met: promote, then re-arm the candidate the promote
    consumed, so the SECOND call is refused by the guard rather than by ``no_prod_candidate``
    (which would pass whether or not the guard armed at all)."""
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    stored = project_service_local.get_repo("r-1")
    assert stored.cicd_status == "promoting" and stored.last_promoted_at

    project_service_local.record_prod_candidate(
        "a-1", image_tag="a-1-tree888", sha="a" * 40, actor="merger-login"
    )
    build_svc.start_runtime_build.reset_mock()
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-2")
    assert ei.value.kind == "promote_in_flight"
    build_svc.start_runtime_build.assert_not_called()


def test_the_provisional_tag_gate_reads_the_repo_and_stage_scoped_history(project_service_local, build_svc):
    """The gate must not be satisfied by a SUCCEEDED row belonging to a different repo or a
    non-prod stage. Both are realistic: two repos share one tenant ECR registry (so another
    repo's tag is a resolvable image), and the same artifact routinely succeeds on dev first —
    dev succeeding proves nothing about what PRODUCTION serves."""
    for over in ({"repo_id": "r-OTHER"}, {"stage": "dev"}):
        seed(project_service_local._store, _to_item(_REPOSITORY_PK, _repo()))
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


def test_success_persists_audit_fields(project_service_local, build_svc):
    """Asserted against a RE-READ, never the returned object.

    The returned record is mutated in memory before the save, so asserting it cannot tell a
    persisted transition from a dropped one: with ``include_cicd_status=True`` removed the
    return value still says ``promoting`` while the STORED record stays ``deployed``, exactly the
    clobber class of bug, and the FE polls the stored record.

    The tag is not stamped here: ``start_runtime_build`` returning only proves CodeBuild accepted the
    build, the terraform apply may fail minutes later, and ``last_promoted_image_tag`` means "what prod
    serves". The other three (who authorized, when, which build) are true at this instant and stamped
    unconditionally, and the tag CodeBuild owns is never rewritten."""
    build_svc.start_runtime_build.return_value = "build-123"
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    stored = project_service_local.get_repo("r-1")
    assert stored.cicd_status == "promoting"
    assert stored.last_promoted_by == "oid-1"
    assert stored.last_promotion_build_id == "build-123"
    assert stored.last_promoted_at
    assert stored.last_promoted_image_tag is None
    assert stored.last_dev_image_tag == DEV_TAG


def test_repo_from_another_project_cannot_be_promoted(project_service_local, build_svc):
    """The cross-project (IDOR) guard: an OWNER of proj-1 cannot promote a repo that lives
    under a different project. This guard is the only thing standing between a project-scoped
    OWNER role and a prod deploy of someone else's repo."""
    seed(project_service_local._store, _to_item(_REPOSITORY_PK, _repo(id="r-2", project_id="proj-OTHER")))
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-2", promoted_by="oid-1")
    assert ei.value.kind == "repo_not_found"
    build_svc.start_runtime_build.assert_not_called()


def test_build_failure_stamps_actor_and_persists_failed_before_raising(project_service_local, build_svc):
    """A failed build start is attributed to the actor and persists ``failed`` before the error is raised."""
    from services.runtime_build_service import RuntimeBuildError

    build_svc.start_runtime_build.side_effect = RuntimeBuildError("nope", kind="start_failed")
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert ei.value.kind == "promote_failed"
    stored = project_service_local.get_repo("r-1")
    assert stored.cicd_status == "failed"
    assert stored.last_promoted_by == "oid-1"
    assert stored.last_promoted_image_tag == CANDIDATE_TAG


def test_a_409_in_flight_conflict_is_not_recorded_as_a_failed_promotion(project_service_local, build_svc):
    """`RuntimeBuildError(kind="conflict")` — the CodeBuild in-flight check
    INSIDE `start_runtime_build`, a different guard from this record's own
    `_promotion_in_flight` — means nothing was attempted. It must map to its own `ProjectError`
    kind ("conflict") rather than going through `_record_failed_delivery`: the record must stay
    exactly as it was, so the caller's "try again shortly" is actually true."""
    from services.runtime_build_service import RuntimeBuildError

    build_svc.start_runtime_build.side_effect = RuntimeBuildError(
        "a build for agent_id=a-1 stage=prod is already in progress", kind="conflict"
    )
    before = project_service_local.get_repo("r-1")
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert ei.value.kind == "conflict"
    stored = project_service_local.get_repo("r-1")
    assert stored.cicd_status == before.cicd_status != "failed"
    assert stored.prod_candidate_image_tag == before.prod_candidate_image_tag
    assert stored.last_promoted_by is None


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
    every ``ClientError`` whose code is not ``ResourceNotFoundException``, so a ``ThrottlingException`` on
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
        Agent.model_validate({"id": "a-1", "name": "n", "purpose": "p", "platform": "NOT_A_PLATFORM"})
    except ValidationError as err:
        return err
    raise AssertionError("expected a pydantic ValidationError")


_BUILD_START_FAILURES = [
    _connection_error,
    _malformed_agent_error,
    _registry_client_error,
    _botocore_transport_error,
    _unknown_registry_status_error,
    _pydantic_validation_error,
]


@pytest.mark.parametrize("make_exc", _BUILD_START_FAILURES)
def test_non_runtime_build_failures_also_stamp_and_persist_failed(project_service_local, build_svc, make_exc):
    """Every failure to start the deploy shares one discipline.

    ``start_runtime_build`` resolves the connection, the agent (through the registry) and the
    tenant before it ever reaches its own ``RuntimeBuildError``, so a whole family of
    dependency faults precedes it. Any catch that misses one lets it escape as a 500 AND skips
    the stamp entirely — leaving the record still reading ``deployed`` with
    ``last_promoted_by=None``, i.e. an ATTEMPTED PROD PROMOTION WITH NO ATTRIBUTION.

    Parametrized over every type confirmed reachable by probing the real call graph (real
    ``RuntimeBuildService``, ``AgentRegistryService`` and ``TenantService``, only the boto3 clients
    faked), so the catch tuple cannot be narrowed back one term at a time without a failure."""
    build_svc.start_runtime_build.side_effect = make_exc()
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert ei.value.kind == "promote_failed"
    stored = project_service_local.get_repo("r-1")
    assert stored.cicd_status == "failed"
    assert stored.last_promoted_by == "oid-1"
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


def test_a_409_in_flight_conflict_is_409_over_the_route_not_502(
    client_factory, project_svc, build_svc, role_svc
):
    """The route's twin of `test_a_409_in_flight_conflict_is_not_recorded_as_a_failed_promotion`:
    a `RuntimeBuildError(kind="conflict")` must map to 409 ("try again shortly"), not the
    generic 502 every OTHER `RuntimeBuildError` gets."""
    from services.runtime_build_service import RuntimeBuildError

    build_svc.start_runtime_build.side_effect = RuntimeBuildError(
        "a build for agent_id=a-1 stage=prod is already in progress", kind="conflict"
    )
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory(project_role="owner", roles={"proj-1": "owner"})
    r = client.post("/api/v1/projects/proj-1/repos/r-1/promote")
    assert r.status_code == 409
    assert r.json()["detail"] == "a build is already running for this agent/stage, try again shortly"


def test_programming_errors_are_NOT_recorded_as_a_failed_promotion(project_service_local, build_svc):
    """The catch is ENUMERATED, not ``except Exception``: an AGP bug must surface as a bug
    rather than be laundered into a plausible-looking ``failed`` promotion record."""
    build_svc.start_runtime_build.side_effect = TypeError("start_runtime_build() got …")
    with pytest.raises(TypeError):
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")


def test_second_promote_while_in_flight_is_409(project_service_local, build_svc):
    """Two concurrent promotes would start two CodeBuild runs racing the same Terraform state
    key. The first promote stamps ``promoting`` + ``last_promoted_at``, so the second refuses.

    Staging note: the second promote needs a candidate to even reach this guard, since the first
    consumed the one it shipped. A new merge to ``main`` landing mid-promotion is exactly how that
    happens in practice, and it is why the guard is still needed: without it, a merge arriving during a
    promotion would let a second deploy start against the same state key. The newer candidate is still
    there to promote once the first deploy settles."""
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    build_svc.start_runtime_build.reset_mock()
    project_service_local.record_prod_candidate(
        "a-1", image_tag="a-1-tree888", sha="8" * 40, actor="second-merger"
    )
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-2")
    assert ei.value.kind == "promote_in_flight"
    build_svc.start_runtime_build.assert_not_called()
    assert project_service_local.get_repo("r-1").prod_candidate_image_tag == "a-1-tree888"


def test_in_flight_promote_is_409_over_the_route(client_factory, project_svc, build_svc, role_svc):
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
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo(cicd_status="promoting", last_promoted_at=stale)))
    repo = svc.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert repo.cicd_status == "promoting"
    assert build_svc.start_runtime_build.call_count == 1


def test_promoting_without_a_timestamp_does_not_block(build_svc):
    """The guard fails open on a missing or unparseable ``last_promoted_at`` (a record written before the
    stamp existed, or a partial write): being unable to date the attempt must not strand the repo."""
    svc = _real_service(build_svc)
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo(cicd_status="promoting", last_promoted_at=None)))
    assert svc.promote_repo("proj-1", "r-1", promoted_by="oid-1").cicd_status == "promoting"
    seed(
        svc._store,
        _to_item(_REPOSITORY_PK, _repo(id="r-2", cicd_status="promoting", last_promoted_at="not-a-date")),
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
        seed(
            svc._store,
            _to_item(
                _REPOSITORY_PK,
                _repo(id=repo_id, cicd_status="promoting", last_promoted_at=(FIXED + skew).isoformat()),
            ),
        )
        assert svc.promote_repo("proj-1", repo_id, promoted_by="oid-1").cicd_status == "promoting"
    assert build_svc.start_runtime_build.call_count == 2


def test_a_store_fault_on_the_failure_persist_still_yields_the_curated_error(
    project_service_local, build_svc
):
    """The failure path's audit write is BEST-EFFORT and must never downgrade the outcome.

    ``_save_repo`` propagates in DDB mode by design, and on the failure path it runs BEFORE
    the ``raise`` — so a throttled projects table replaced the contract's curated 502 with an
    unmapped ``ClientError`` → 500. The build failing and the table throttling is a correlated
    pair during a regional event, not two coincidences. The operator must still get the
    promotion's error, not the audit write's, and the write is still attempted: swallowing it must not
    mean skipping it."""
    from botocore.exceptions import ClientError

    from services.runtime_build_service import RuntimeBuildError

    build_svc.start_runtime_build.side_effect = RuntimeBuildError("nope", kind="start_failed")
    project_service_local._save_repo = MagicMock(
        side_effect=ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "UpdateItem",
        )
    )
    with pytest.raises(ProjectError) as ei:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert ei.value.kind == "promote_failed"
    assert ei.value.message == "promotion failed"
    project_service_local._save_repo.assert_called_once()


def test_promote_error_detail_never_leaks_internals(client_factory, build_svc, role_svc, project_svc):
    from services.runtime_build_service import RuntimeBuildError

    build_svc.start_runtime_build.side_effect = RuntimeBuildError("arn:aws:secret:leak", kind="start_failed")
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory(project_role="owner", roles={"proj-1": "owner"})
    r = client.post("/api/v1/projects/proj-1/repos/r-1/promote")
    assert r.status_code == 502
    assert r.json()["detail"] == "failed to start the promotion build"
    assert "secret" not in r.text


CANDIDATE_DIGEST = "sha256:" + "ab" * 32


def test_promote_deploys_the_APPROVED_DIGEST(project_service_local, build_svc):
    """The digest recorded on the candidate is what reaches the deploy, passed through verbatim and never
    re-resolved from the tag; the tag still travels alongside it, because the buildspec derives the agent
    id, the deployment id and the scratch-secret name from the tag."""
    seed(
        project_service_local._store, _to_item(_REPOSITORY_PK, _repo(prod_candidate_digest=CANDIDATE_DIGEST))
    )
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    kwargs = build_svc.start_runtime_build.call_args.kwargs
    assert kwargs["image_digest"] == CANDIDATE_DIGEST
    assert kwargs["image_tag"] == CANDIDATE_TAG


def test_promote_never_re_derives_the_digest_from_the_registry(project_service_local, build_svc):
    """The point of the digest, asserted as a property of the data rather than of the call graph.

    A digest that does not match the tag's current registry content must still be the one deployed:
    the approval attests to BYTES, and re-resolving the tag at deploy time would ship whatever the
    (mutable) tag points at now. So the value the service passes must be exactly the stored one,
    even when it is unrelated to anything the tag would resolve to."""
    unrelated = "sha256:" + "de" * 32
    seed(project_service_local._store, _to_item(_REPOSITORY_PK, _repo(prod_candidate_digest=unrelated)))
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    assert build_svc.start_runtime_build.call_args.kwargs["image_digest"] == unrelated


def test_a_legacy_tag_only_candidate_still_promotes(project_service_local, build_svc):
    """A candidate registered before this epic (or by the still-live ``/prod-candidate`` route,
    which has no digest in scope) carries NO digest. It must still promote over the tag path —
    refusing would strand every candidate registered before digests with no way to release it."""
    seed(project_service_local._store, _to_item(_REPOSITORY_PK, _repo(prod_candidate_digest=None)))
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    kwargs = build_svc.start_runtime_build.call_args.kwargs
    assert kwargs["image_digest"] == ""
    assert kwargs["image_tag"] == CANDIDATE_TAG


def test_a_promote_clears_the_candidate_digest_with_the_rest_of_the_block(project_service_local, build_svc):
    """The digest is consumed as part of the ONE unit. A leftover digest after a successful promote
    would let a second approval deploy bytes whose candidate had already been consumed."""
    seed(
        project_service_local._store, _to_item(_REPOSITORY_PK, _repo(prod_candidate_digest=CANDIDATE_DIGEST))
    )
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    stored = project_service_local.get_repo("r-1")
    assert stored.prod_candidate_digest is None
    assert stored.prod_candidate_image_tag is None


def test_a_REBUILD_of_the_same_tree_is_not_erased_by_the_compare_and_clear(project_service_local, build_svc):
    """The compare-and-clear must key on the DIGEST too, and this is the case that proves why.

    A rebuild of an unchanged tree produces the SAME tag and the SAME commit sha over DIFFERENT
    bytes — not an edge case but the expected outcome of a non-reproducible build. Comparing only
    tag+sha would read such a rebuild as "still the candidate we shipped" and CLEAR it, silently
    discarding an artifact no owner ever approved. The newer digest must survive, and the promotion that
    did run is still attributed to the digest it actually shipped."""
    seed(
        project_service_local._store, _to_item(_REPOSITORY_PK, _repo(prod_candidate_digest=CANDIDATE_DIGEST))
    )
    rebuilt = "sha256:" + "cd" * 32

    def _rebuild_lands_mid_build(**kwargs):
        project_service_local.record_prod_candidate(
            "a-1",
            image_tag=CANDIDATE_TAG,
            sha=CANDIDATE_SHA,
            actor=CANDIDATE_ACTOR,
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
    assert stored.last_promoted_by == "oid-1"


def test_the_promoted_digest_is_recorded_only_for_an_image_prod_has_RUN(project_service_local, build_svc):
    """``last_promoted_digest`` means "what prod SERVES", so it rides the same `serving` gate as
    the tag rather than being stamped the moment CodeBuild accepts the build.

    On a FIRST promote nothing has succeeded in prod yet, so neither scalar is written — the honest
    answer, and the same asymmetry the tag has. Splitting the gate would let
    the pair disagree, which is worse than both being absent."""
    seed(
        project_service_local._store, _to_item(_REPOSITORY_PK, _repo(prod_candidate_digest=CANDIDATE_DIGEST))
    )
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    stored = project_service_local.get_repo("r-1")
    assert stored.last_promoted_digest is None
    assert stored.last_promoted_image_tag is None


def test_dev_outcome_for_candidate_is_null_with_no_pending_candidate(project_service_local):
    """Nothing to approve ⇒ nothing to check — and no `list_deployments` read is needed."""
    seed(project_service_local._store, _to_item(_REPOSITORY_PK, _repo(prod_candidate_image_tag=None)))
    assert project_service_local.get_repo("r-1").dev_outcome_for_candidate is None


def test_dev_outcome_for_candidate_is_failed_when_the_candidates_own_dev_deploy_failed(
    project_service_local,
):
    """A candidate offered as pending whose own dev deploy failed must say so on the read, not just refuse
    silently on click."""
    seed(project_service_local._store, _to_item(_REPOSITORY_PK, _repo()))
    project_service_local.append_deployment(
        repo_id="r-1",
        agent_id="a-1",
        stage="dev",
        image_tag=CANDIDATE_TAG,
        outcome=DeploymentOutcome.FAILED,
        completed_at=FIXED_TS,
    )
    assert project_service_local.get_repo("r-1").dev_outcome_for_candidate == "failed"


def test_dev_outcome_for_candidate_prefers_the_newest_row_for_the_tag(project_service_local):
    """An older failure followed by a newer success for the SAME tag reads as succeeded — the
    append-only partition's newest-first order, mirroring `promote_repo`'s own gate exactly."""
    from models.deployment import deployment_seq_key

    seed(project_service_local._store, _to_item(_REPOSITORY_PK, _repo()))
    old_ts, new_ts = "2026-07-26T00:00:00+00:00", "2026-07-27T12:00:00+00:00"
    for started_at, outcome, suffix in (
        (old_ts, DeploymentOutcome.FAILED, "aaaa"),
        (new_ts, DeploymentOutcome.SUCCEEDED, "bbbb"),
    ):
        dep_id = f"dep-{suffix}"
        project_service_local._save_deployment(
            Deployment(
                id=dep_id,
                repo_id="r-1",
                agent_id="a-1",
                stage="dev",
                seq_key=deployment_seq_key("r-1", "dev", started_at, dep_id),
                image_tag=CANDIDATE_TAG,
                outcome=outcome,
                started_at=started_at,
                completed_at=started_at,
            )
        )
    assert project_service_local.get_repo("r-1").dev_outcome_for_candidate == "succeeded"


def test_dev_outcome_for_candidate_is_null_when_no_dev_row_names_the_tag(project_service_local):
    """No matching dev row at all (a record predating dev rows, or a narrow race) is UNKNOWN, not a
    failure — same "absence does not block" stance as `promote_repo`'s own gate."""
    seed(project_service_local._store, _to_item(_REPOSITORY_PK, _repo()))
    assert project_service_local.get_repo("r-1").dev_outcome_for_candidate is None


def test_dev_outcome_for_candidate_is_never_persisted(project_service_local, build_svc):
    """Computed at READ time only. A promote (which re-saves the record) must not have
    written the computed value back — `_save_repo`'s skip set is what this pins."""
    seed(project_service_local._store, _to_item(_REPOSITORY_PK, _repo()))
    project_service_local.append_deployment(
        repo_id="r-1",
        agent_id="a-1",
        stage="dev",
        image_tag=CANDIDATE_TAG,
        outcome=DeploymentOutcome.SUCCEEDED,
        completed_at=FIXED_TS,
    )
    project_service_local.get_repo("r-1")
    project_service_local.promote_repo("proj-1", "r-1", promoted_by="oid-1")
    raw = project_service_local._get_repo("r-1")
    assert raw.dev_outcome_for_candidate is None


def test_list_repositories_and_get_project_also_hydrate_the_field(project_service_local):
    """The two OTHER read paths the repo list/detail pages poll — not just `get_repo`."""
    seed(project_service_local._store, _to_item(_REPOSITORY_PK, _repo()))
    project_service_local.append_deployment(
        repo_id="r-1",
        agent_id="a-1",
        stage="dev",
        image_tag=CANDIDATE_TAG,
        outcome=DeploymentOutcome.FAILED,
        completed_at=FIXED_TS,
    )
    [listed] = project_service_local.list_repositories()
    assert listed.dev_outcome_for_candidate == "failed"

    seed(project_service_local._store, _to_item(_PROJECT_PK, _project()))
    detail = project_service_local.get_project("proj-1")
    [from_detail] = detail.repositories
    assert from_detail.dev_outcome_for_candidate == "failed"


MCP_NO_PROMOTION = "MCP servers deploy a single gateway; promotion is not available yet"


def _mcp_repo():
    return Repository(
        id="r-1",
        project_id="proj-1",
        name="claims-api",
        artifact_kind="mcp_server",
        mcp_server_id="mcp-1",
        template_name="gateway-agentcore",
        cicd_status="deployed",
        status="ready",
        created_by="operator@x.com",
        created_at=FIXED_TS,
        updated_at=FIXED_TS,
        prod_candidate_image_tag=CANDIDATE_TAG,
        prod_candidate_status="pending",
    )


def test_promote_refuses_an_mcp_repo_before_any_build(project_service_local, build_svc):
    """An MCP repo has no prod artifact, so promote refuses it before any build."""
    seed(project_service_local._store, _to_item(_REPOSITORY_PK, _mcp_repo()))
    with pytest.raises(ProjectError) as err:
        project_service_local.promote_repo("proj-1", "r-1", promoted_by="owner-oid")
    assert err.value.kind == "mcp_not_promotable"
    assert str(err.value) == MCP_NO_PROMOTION
    build_svc.start_runtime_build.assert_not_called()
    assert project_service_local.list_deployments("r-1") == []


def test_rollback_refuses_an_mcp_repo_before_any_build(project_service_local, build_svc):
    """An MCP repo has no prod artifact, so rollback refuses it before any build."""
    seed(project_service_local._store, _to_item(_REPOSITORY_PK, _mcp_repo()))
    with pytest.raises(ProjectError) as err:
        project_service_local.rollback_repo(
            "proj-1", "r-1", image_tag="c" * 40, rolled_back_by="owner-oid", stage="dev"
        )
    assert err.value.kind == "mcp_not_promotable"
    build_svc.start_runtime_build.assert_not_called()


def test_promote_and_rollback_routes_answer_422_for_an_mcp_repo(
    client_factory, project_svc, build_svc, role_svc
):
    project_svc.get_repo.return_value = _mcp_repo()
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory(project_role="owner", roles={"proj-1": "owner"})

    promote = client.post("/api/v1/projects/proj-1/repos/r-1/promote")
    rollback = client.post("/api/v1/projects/proj-1/repos/r-1/rollback", json={"image_tag": "c" * 40})
    for r in (promote, rollback):
        assert r.status_code == 422
        assert r.json()["detail"] == MCP_NO_PROMOTION
    build_svc.start_runtime_build.assert_not_called()
