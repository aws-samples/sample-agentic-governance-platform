"""Promote, build and rollback wired to the append-only ``Deployment`` partition.

The one invariant the wiring turns on: two promotes leave two rows, and the first is not erased.
Promotion state used to live only as singular scalars on ``Repository`` (``last_promoted_*``), which are
overwritten wholesale by the next promote, so a second release erased the evidence of the first and
rollback had no artifact to roll back to.

Four more things are pinned here because each is a way the wiring could be silently wrong:

1. **A failed build appends a ``FAILED`` row rather than vanishing.** A promote that could not
   start is exactly the event an operator needs in the history; with no row it looks like nobody
   ever tried.
2. **Rollback refuses a tag that never SUCCEEDED for that stage.** Without that validation the
   route is a deploy-anything primitive: an arbitrary caller-supplied tag would be applied to
   production. Refusal is asserted together with ``start_runtime_build.assert_not_called()``;
   "returned 4xx" is not the same claim as "did not deploy".
3. **Rollback refuses a tag belonging to ANOTHER repo.** Same class, different axis: two repos
   share one tenant ECR registry, so repo B's tag is a resolvable image. The validation must be
   scoped to *this* repo's own history.
4. **Rollback is gated exactly like promote**: OWNER, through the STRICT gate. It is a write to
   production, so a looser gate on the newer verb would bypass the older one.

Actor currency is pinned too: a promote or rollback actor is an **Entra oid** with
``actor_kind="entra"``, while the OIDC dev-build actor is a **GitHub login** with
``actor_kind="github"``. They are two different currencies and must never be stored as one.

The buildspec-written terminal row must round-trip. The buildspec (``modules/codebuild/buildspec.yml``,
helper ``_dep``) is the only writer of a terminal ``succeeded`` or ``failed`` row: the backend appends
``started`` when a build is requested, and only the build itself knows how it ended. That row is written
by a different producer, in a different language, from a shell context, so "does the reader see it?" is
implied by no other test in this suite; a drift in the sk shape does not raise, the row simply lands
where ``list_deployments`` does not look and the symptom is "history is empty" hours later. The
``_BUILDSPEC_ROW`` fixtures are a literal transcript of executing the helper with ``REPO_SK=r-1
STAGE=dev AGENT_ID=a-1 IMAGE_TAG=a-1-abc1234 CODEBUILD_BUILD_ID=p:1234-5678``; if the buildspec changes
shape, re-capture them the same way rather than hand-edit them into agreement, because agreeing with a
hand-edit is not evidence about the buildspec.

A rollback cannot name its target's digest: a rollback target is validated against succeeded
``Deployment`` rows, which record a tag only, so on a rollback nothing knows which bytes the older tag
resolves to, and the honest answer is "unknown", not "whatever the last release used".

Harness idiom is ``test_promote_repo.py``'s: the real in-memory ``ProjectService`` (``table_name=""``
selects the in-memory store), an injected fixed clock, every collaborator a ``MagicMock`` so nothing
reaches CodeBuild, Secrets Manager, GitHub or Entra, and for the route tests the real ``require_role``,
``current_principal``, ``get_tenant_ctx`` and ``get_project_ctx`` chain against a mocked
``verify_entra_token`` (no ``dependency_overrides``). Account ids, where they appear at all, are
obviously-fake 12-digit values. Every test runs under ``passing_eval_gate`` so the eval gate
(``test_promote_eval_gate.py``) stays out of the way.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from seed import seed

from models.deployment import Deployment, DeploymentOutcome
from models.project import Project
from models.project_role import ROLE_NAMES, ProjectRoleRecord
from models.repository import CicdStatus, Repository
from services.project_errors import ProjectError
from services.project_service import ProjectService
from services.project_store import _PROJECT_PK, _REPOSITORY_PK, _parse_rows, _to_item
from services.runtime_build_service import RuntimeBuildError

pytestmark = pytest.mark.usefixtures("reset_modules", "passing_eval_gate")

FIXED = datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc)
FIXED_TS = FIXED.isoformat()

AGENT_ID = "a-1"
TAG_ONE = "a-1-tree001"
TAG_TWO = "a-1-tree002"
SHA_ONE = "1" * 40
SHA_TWO = "2" * 40
OWNER_OID = "owner-oid"
INSUFFICIENT = "insufficient project role"


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
    cicd_status: CicdStatus = "deployed",
    last_promoted_at=None,
    prod_candidate_image_tag: str | None = TAG_ONE,
    prod_candidate_sha=SHA_ONE,
    agent_id=AGENT_ID,
    name="fraud-agent",
):
    """A repo with a pending prod candidate, so it has something promotable."""
    candidate: dict[str, Any] = (
        {
            "prod_candidate_image_tag": prod_candidate_image_tag,
            "prod_candidate_sha": prod_candidate_sha,
            "prod_candidate_actor": "merger-login",
            "prod_candidate_at": FIXED_TS,
            "prod_candidate_status": "pending",
        }
        if prod_candidate_image_tag
        else {}
    )
    return Repository(
        id=id,
        project_id=project_id,
        name=name,
        repo_url=f"https://github.com/acme/{name}",
        agent_id=agent_id,
        template_name="strands-agentcore",
        cicd_status=cicd_status,
        status="provisioning",
        created_by="operator@x.com",
        created_at=FIXED_TS,
        updated_at=FIXED_TS,
        last_promoted_at=last_promoted_at,
        **candidate,
    )


def _service(build_svc, *, now=None, tenant_id="default"):
    """The real in-memory ``ProjectService``: ``table_name=""`` selects the in-memory store."""
    svc = ProjectService(
        table_name="",
        registry=MagicMock(),
        identity=MagicMock(),
        connection_service=MagicMock(),
        github_repo_service=MagicMock(),
        runtime_build_service=build_svc,
        ecr_repository="platform-fallback-ecr",
        now=now or (lambda: FIXED),
    )
    seed(svc._store, _to_item(_PROJECT_PK, _project(tenant_id=tenant_id)))
    return svc


@pytest.fixture
def svc(build_svc):
    s = _service(build_svc)
    seed(s._store, _to_item(_REPOSITORY_PK, _repo()))
    return s


class _Clock:
    """A SETTABLE clock, not a sequence — every append and every stamp reads ``_now()``, so a
    fixed list of moments would couple the test to the exact number of internal reads."""

    def __init__(self, at=FIXED):
        self.at = at

    def __call__(self):
        return self.at


def _succeeded(svc, *, repo_id="r-1", stage="prod", image_tag=TAG_ONE, **over):
    """Seed a SUCCEEDED row — the only kind rollback may target."""
    return svc.append_deployment(
        repo_id=repo_id,
        agent_id=AGENT_ID,
        stage=stage,
        image_tag=image_tag,
        outcome=DeploymentOutcome.SUCCEEDED,
        completed_at=FIXED_TS,
        **over,
    )


def test_two_promotes_leave_two_rows_and_the_first_is_not_erased(build_svc):
    """The ``last_promoted_*`` scalars can only ever remember the second promote; the append-only
    partition must still hold the first, newest first, with two distinct tags so the erasure would be
    assertable. A newer merge registers the second candidate once the first promote's in-flight window
    is over. The denormalized cache remembers none of the tags until a delivery actually succeeds
    (neither promote got past ``StartBuild``), so it claims nothing about what prod serves while both
    attempts survive in the partition and the attribution stays."""
    clock = _Clock()
    svc = _service(build_svc, now=clock)
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo()))

    svc.promote_repo("proj-1", "r-1", promoted_by=OWNER_OID)
    clock.at = FIXED + timedelta(hours=2)
    svc.record_prod_candidate(AGENT_ID, image_tag=TAG_TWO, sha=SHA_TWO, actor="merger-login")
    svc.promote_repo("proj-1", "r-1", promoted_by=OWNER_OID)

    rows = svc.list_deployments("r-1", stage="prod")
    assert len(rows) == 2, "the first promote's row was erased"
    assert [r.image_tag for r in rows] == [TAG_TWO, TAG_ONE]
    repo = svc._get_repo("r-1")
    assert repo is not None
    assert repo.last_promoted_image_tag is None
    assert repo.last_promotion_build_id == "build-1"


def test_a_promote_row_carries_the_promote_facts(svc, build_svc):
    """Attribution, build id, sha and stage all land on the row; the Deployments tab reads this, and a
    row without a build id or actor is not an audit record. The actor is an Entra oid, never a GitHub
    login."""
    svc.promote_repo("proj-1", "r-1", promoted_by=OWNER_OID)

    (row,) = svc.list_deployments("r-1")
    assert row.repo_id == "r-1"
    assert row.agent_id == AGENT_ID
    assert row.stage == "prod"
    assert row.image_tag == TAG_ONE
    assert row.source_sha == SHA_ONE
    assert row.build_id == "build-1"
    assert row.outcome is DeploymentOutcome.STARTED
    assert row.actor == OWNER_OID
    assert row.actor_kind == "entra"
    assert row.error is None


def test_a_failed_promote_appends_a_FAILED_row(svc, build_svc):
    """A build that could not start must not vanish from the history: with no row, "the deploy failed"
    and "nobody ever tried" are the same observation. The row is appended already closed, and its error
    is a safe hint; the build service's own message never reaches the record."""
    build_svc.start_runtime_build.side_effect = RuntimeBuildError("boom", kind="start_failed")

    with pytest.raises(ProjectError) as err:
        svc.promote_repo("proj-1", "r-1", promoted_by=OWNER_OID)
    assert err.value.kind == "promote_failed"

    (row,) = svc.list_deployments("r-1")
    assert row.outcome is DeploymentOutcome.FAILED
    assert row.image_tag == TAG_ONE
    assert row.build_id is None
    assert row.completed_at == FIXED_TS
    assert row.actor == OWNER_OID and row.actor_kind == "entra"
    assert row.error and "boom" not in row.error


def test_a_promote_refused_before_the_build_appends_NOTHING(svc, build_svc):
    """A refusal is not a deployment. ``no_prod_candidate`` never reached CodeBuild, so a row
    would be a phantom attempt in the history."""
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo(prod_candidate_image_tag=None)))

    with pytest.raises(ProjectError) as err:
        svc.promote_repo("proj-1", "r-1", promoted_by=OWNER_OID)
    assert err.value.kind == "no_prod_candidate"
    assert svc.list_deployments("r-1") == []
    build_svc.start_runtime_build.assert_not_called()


def test_rollback_deploys_a_previously_succeeded_tag(svc, build_svc):
    _succeeded(svc, image_tag=TAG_ONE)

    repo = svc.rollback_repo("proj-1", "r-1", image_tag=TAG_ONE, rolled_back_by=OWNER_OID)

    kwargs = build_svc.start_runtime_build.call_args.kwargs
    assert kwargs["image_tag"] == TAG_ONE
    assert kwargs["stage"] == "prod"
    assert kwargs["agent_id"] == AGENT_ID
    assert repo.cicd_status == "promoting"


def test_rollback_appends_its_own_row(svc, build_svc):
    """A rollback is a deployment: it must be in the history, attributed, and distinguishable from the
    promote it reverts. Its row is selected by outcome, not by index, because under the fixed clock both
    rows share ``started_at`` and "newest" is a tie."""
    _succeeded(svc, image_tag=TAG_ONE)

    svc.rollback_repo("proj-1", "r-1", image_tag=TAG_ONE, rolled_back_by=OWNER_OID)

    rows = svc.list_deployments("r-1", stage="prod")
    assert len(rows) == 2
    (row,) = [d for d in rows if d.outcome is DeploymentOutcome.STARTED]
    assert row.image_tag == TAG_ONE
    assert row.outcome is DeploymentOutcome.STARTED
    assert row.build_id == "build-1"
    assert row.actor == OWNER_OID and row.actor_kind == "entra"


def test_rollback_refuses_a_tag_that_never_succeeded_for_that_stage(svc, build_svc):
    """The whole point of the validation. A tag with no SUCCEEDED row in this stage is not a
    known-good artifact, and applying an arbitrary caller-supplied tag to production would be a
    deploy-anything primitive."""
    _succeeded(svc, image_tag=TAG_ONE)

    with pytest.raises(ProjectError) as err:
        svc.rollback_repo("proj-1", "r-1", image_tag="a-1-neverbuilt", rolled_back_by=OWNER_OID)
    assert err.value.kind == "unknown_rollback_target"
    build_svc.start_runtime_build.assert_not_called()
    assert len(svc.list_deployments("r-1")) == 1


def test_rollback_refuses_a_tag_that_only_succeeded_in_ANOTHER_stage(svc, build_svc):
    """Stage-scoped. A tag proven good in ``dev`` is not thereby approved for ``prod`` —
    accepting it would let a dev-only artifact reach production through the rollback door."""
    _succeeded(svc, stage="dev", image_tag="a-1-devonly")

    with pytest.raises(ProjectError) as err:
        svc.rollback_repo("proj-1", "r-1", image_tag="a-1-devonly", rolled_back_by=OWNER_OID)
    assert err.value.kind == "unknown_rollback_target"
    build_svc.start_runtime_build.assert_not_called()


def test_rollback_refuses_a_tag_belonging_to_ANOTHER_repo(svc, build_svc):
    """Two repos share one tenant ECR registry, so repo B's tag names a real, pullable image.
    The validation must be scoped to THIS repo's own history or a cross-repo tag becomes a
    deployable artifact."""
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo(id="r-2", agent_id="a-2", name="other-agent")))
    _succeeded(svc, repo_id="r-2", image_tag="a-2-tree001")

    with pytest.raises(ProjectError) as err:
        svc.rollback_repo("proj-1", "r-1", image_tag="a-2-tree001", rolled_back_by=OWNER_OID)
    assert err.value.kind == "unknown_rollback_target"
    build_svc.start_runtime_build.assert_not_called()


def test_rollback_refuses_a_tag_whose_only_row_FAILED(svc, build_svc):
    """``started`` and ``failed`` are not evidence an artifact ever ran. Only SUCCEEDED is."""
    svc.append_deployment(
        repo_id="r-1",
        agent_id=AGENT_ID,
        stage="prod",
        image_tag=TAG_TWO,
        outcome=DeploymentOutcome.FAILED,
        completed_at=FIXED_TS,
        error="build failed",
    )
    svc.append_deployment(
        repo_id="r-1",
        agent_id=AGENT_ID,
        stage="prod",
        image_tag=TAG_TWO,
        outcome=DeploymentOutcome.STARTED,
    )

    with pytest.raises(ProjectError) as err:
        svc.rollback_repo("proj-1", "r-1", image_tag=TAG_TWO, rolled_back_by=OWNER_OID)
    assert err.value.kind == "unknown_rollback_target"
    build_svc.start_runtime_build.assert_not_called()


def test_rollback_refuses_an_empty_tag(svc, build_svc):
    """An empty tag would deploy ``<ecr_repo>:`` to production (``promote_repo``'s own
    falsy-not-None reasoning, carried over)."""
    with pytest.raises(ProjectError) as err:
        svc.rollback_repo("proj-1", "r-1", image_tag="", rolled_back_by=OWNER_OID)
    assert err.value.kind == "unknown_rollback_target"
    build_svc.start_runtime_build.assert_not_called()


def test_rollback_refuses_a_repo_in_another_project(svc, build_svc):
    """Same ownership check ``promote_repo`` makes: without it an OWNER of project A could roll back a repo
    living under project B. The foreign project is seeded so only the ownership check can produce this
    refusal: with no such project ``_get_project`` would return ``None`` and the 404 would come from the
    missing-project path, asserting nothing about ownership."""
    seed(svc._store, _to_item(_PROJECT_PK, _project(id="proj-2")))
    _succeeded(svc, image_tag=TAG_ONE)

    with pytest.raises(ProjectError) as err:
        svc.rollback_repo("proj-2", "r-1", image_tag=TAG_ONE, rolled_back_by=OWNER_OID)
    assert err.value.kind == "repo_not_found"
    build_svc.start_runtime_build.assert_not_called()
    assert svc._get_repo("r-1").project_id == "proj-1"
    assert svc._get_project("proj-2") is not None


def test_rollback_refuses_while_a_promote_is_in_flight(svc, build_svc):
    """Two builds against the SAME stage-scoped Terraform state key would race. Follows
    promote's own precedent — the bounded ``promoting`` guard — rather than a second mechanism."""
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo(cicd_status="promoting", last_promoted_at=FIXED_TS)))
    _succeeded(svc, image_tag=TAG_ONE)

    with pytest.raises(ProjectError) as err:
        svc.rollback_repo("proj-1", "r-1", image_tag=TAG_ONE, rolled_back_by=OWNER_OID)
    assert err.value.kind == "promote_in_flight"
    build_svc.start_runtime_build.assert_not_called()


def test_a_promote_refuses_while_a_rollback_is_in_flight(build_svc):
    """The guard must work in BOTH directions, or the pair is not actually serialized: a
    rollback stamps the same in-flight state a promote refuses on."""
    svc = _service(build_svc)
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo()))
    _succeeded(svc, image_tag=TAG_ONE)

    svc.rollback_repo("proj-1", "r-1", image_tag=TAG_ONE, rolled_back_by=OWNER_OID)
    build_svc.start_runtime_build.reset_mock()

    with pytest.raises(ProjectError) as err:
        svc.promote_repo("proj-1", "r-1", promoted_by=OWNER_OID)
    assert err.value.kind == "promote_in_flight"
    build_svc.start_runtime_build.assert_not_called()


def test_a_blank_stage_cannot_bypass_the_stage_scoped_validation(svc, build_svc):
    """A blank stage does not name an unknown stage, it disables the scoping: ``list_deployments``
    branches on ``if stage:``, so ``""`` would fall through to the cross-stage read and ``_has_succeeded``
    would accept a tag that only ever succeeded in ``dev`` as a prod rollback target. The bypass is gone
    at the helper level too, not merely refused one layer up."""
    _succeeded(svc, stage="dev", image_tag="a-1-devonly")

    for blank in ("", "   ", "\t"):
        with pytest.raises(ProjectError) as err:
            svc.rollback_repo("proj-1", "r-1", image_tag="a-1-devonly", stage=blank, rolled_back_by=OWNER_OID)
        assert err.value.kind == "unknown_rollback_target"
    build_svc.start_runtime_build.assert_not_called()
    assert svc._has_succeeded("r-1", "", "a-1-devonly") is False


def test_the_rollback_request_model_rejects_a_blank_stage():
    """Rejected at the MODEL, so the route answers 422 before any handler logic runs."""
    from models.repository import RepoRollbackRequest

    assert RepoRollbackRequest(image_tag="t").stage == "prod"
    assert RepoRollbackRequest(image_tag="t", stage="uat").stage == "uat"
    for blank in ("", " ", "\t"):
        with pytest.raises(ValidationError):
            RepoRollbackRequest(image_tag="t", stage=blank)


def test_a_blank_stage_is_a_422_at_the_route(client_factory_over_route_svc, build_svc, role_svc):
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory_over_route_svc(project_role="owner", roles={"proj-1": "owner"})
    r = client.post(_ROLLBACK, json={"image_tag": TAG_ONE, "stage": ""})
    assert r.status_code == 422
    build_svc.start_runtime_build.assert_not_called()


def test_the_in_flight_guard_arms_for_a_NON_prod_stage(build_svc):
    """The guard arms for every stage, not just prod: ``_promotion_in_flight`` reads ``cicd_status`` and a
    clock, so two sequential ``uat`` rollbacks would otherwise both reach ``start_runtime_build`` and race
    the same stage-scoped Terraform state key. The prod delivery cache is not repointed by a ``uat``
    rollback, ``last_promoted_at`` included, or a non-prod delivery reads as a prod promotion attempt."""
    svc = _service(build_svc)
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo()))
    _succeeded(svc, stage="uat", image_tag="a-1-uat001")

    svc.rollback_repo("proj-1", "r-1", image_tag="a-1-uat001", stage="uat", rolled_back_by=OWNER_OID)
    repo = svc._get_repo("r-1")
    assert repo is not None
    assert repo.cicd_status == "promoting"
    assert repo.last_build_started_at == FIXED_TS
    assert repo.last_promoted_at is None
    assert repo.last_promoted_image_tag is None
    assert repo.last_promoted_by is None
    assert repo.last_promotion_build_id is None

    build_svc.start_runtime_build.reset_mock()
    with pytest.raises(ProjectError) as err:
        svc.rollback_repo("proj-1", "r-1", image_tag="a-1-uat001", stage="uat", rolled_back_by=OWNER_OID)
    assert err.value.kind == "promote_in_flight"
    build_svc.start_runtime_build.assert_not_called()


def test_a_failed_non_prod_rollback_does_not_strand_the_record_at_promoting(build_svc):
    """A non-prod rollback that never started lands on ``failed``, not ``promoting``, otherwise the bounded
    guard refuses retries for its whole window over a build that does not exist. The prod delivery cache
    is untouched: a ``uat`` rollback must not overwrite a prod fact with non-prod data, failed or not,
    and the failure epilogue's ``stamp=`` keyword is the gate that holds it. A delivery that never began
    never names a build id."""
    svc = _service(build_svc)
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo()))
    _succeeded(svc, stage="uat", image_tag="a-1-uat001")
    build_svc.start_runtime_build.side_effect = RuntimeBuildError("boom", kind="start_failed")

    with pytest.raises(ProjectError) as err:
        svc.rollback_repo("proj-1", "r-1", image_tag="a-1-uat001", stage="uat", rolled_back_by=OWNER_OID)
    assert err.value.kind == "rollback_failed"
    repo = svc._get_repo("r-1")
    assert repo is not None
    assert repo.cicd_status == "failed"
    assert repo.last_promoted_image_tag is None
    assert repo.last_promoted_by is None
    assert repo.last_promoted_at is None
    assert repo.last_promotion_build_id is None
    (row,) = [d for d in svc.list_deployments("r-1", stage="uat") if d.outcome is DeploymentOutcome.FAILED]
    assert row.image_tag == "a-1-uat001"


def test_a_409_in_flight_conflict_rollback_is_not_recorded_as_failed(build_svc):
    """Rollback's half of the gap promote's test pins:
    `RuntimeBuildError(kind="conflict")` — the CodeBuild in-flight check inside
    `start_runtime_build`, not this record's own `_promotion_in_flight` guard — means nothing
    was attempted. It must map to `ProjectError(kind="conflict")`, not go through
    `_record_failed_delivery`, so the record is left exactly as it was."""
    svc = _service(build_svc)
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo()))
    _succeeded(svc, stage="uat", image_tag="a-1-uat001")
    build_svc.start_runtime_build.side_effect = RuntimeBuildError(
        "a build for agent_id=a-1 stage=uat is already in progress", kind="conflict"
    )
    repo = svc._get_repo("r-1")
    assert repo is not None
    before = repo.model_copy(deep=True)

    with pytest.raises(ProjectError) as err:
        svc.rollback_repo("proj-1", "r-1", image_tag="a-1-uat001", stage="uat", rolled_back_by=OWNER_OID)
    assert err.value.kind == "conflict"
    repo = svc._get_repo("r-1")
    assert repo is not None
    assert repo.cicd_status == before.cicd_status != "failed"
    assert repo.last_promoted_at == before.last_promoted_at


def test_a_stuck_rollback_is_recoverable(build_svc):
    """The in-flight refusal is BOUNDED, exactly as promote's is: past the CodeBuild build
    timeout the record is STUCK rather than in flight, and a permanently un-rollbackable repo is
    a worse failure than a double deploy."""
    svc = _service(build_svc)
    seed(
        svc._store,
        _to_item(
            _REPOSITORY_PK,
            _repo(cicd_status="promoting", last_promoted_at=(FIXED - timedelta(hours=3)).isoformat()),
        ),
    )
    _succeeded(svc, image_tag=TAG_ONE)

    svc.rollback_repo("proj-1", "r-1", image_tag=TAG_ONE, rolled_back_by=OWNER_OID)
    build_svc.start_runtime_build.assert_called_once()


def test_a_prod_rollback_repoints_the_last_promoted_cache(svc, build_svc):
    """Prod is now serving the older tag, so the denormalized cache must follow it; leaving it on the
    newer tag would make the list row (which reads the cache, not the history) claim production runs an
    image it does not. The settled state is staged by hand, because promote no longer writes this scalar
    (a started build is not a served image), and as a non-``None`` older tag on purpose: with the cache
    empty, "repointed" and "written for the first time" would be the same observation."""
    svc.promote_repo("proj-1", "r-1", promoted_by=OWNER_OID)
    _succeeded(svc, image_tag=TAG_TWO)
    svc._store.set_owned(
        (_REPOSITORY_PK, "r-1"), {"cicd_status": "deployed", "last_promoted_image_tag": TAG_ONE}
    )

    svc.rollback_repo("proj-1", "r-1", image_tag=TAG_TWO, rolled_back_by="rollback-oid")

    repo = svc._get_repo("r-1")
    assert repo.last_promoted_image_tag == TAG_TWO
    assert repo.last_promoted_by == "rollback-oid"
    assert repo.last_promotion_build_id == "build-1"


def test_a_rollback_does_not_consume_the_prod_candidate(svc, build_svc):
    """A rollback is not an approval of ``main``. The pending candidate must survive so the
    OWNER can still promote it once the incident is over — clearing it would make the fix
    unreachable without a fresh commit."""
    _succeeded(svc, image_tag="a-1-older")

    svc.rollback_repo("proj-1", "r-1", image_tag="a-1-older", rolled_back_by=OWNER_OID)

    repo = svc._get_repo("r-1")
    assert repo.prod_candidate_image_tag == TAG_ONE
    assert repo.prod_candidate_status == "pending"


def test_a_failed_rollback_appends_FAILED_and_stays_retryable(svc, build_svc):
    """Mirrors promote's failure discipline: attribute the attempt, persist ``failed``, leave
    the target rollbackable."""
    _succeeded(svc, image_tag=TAG_ONE)
    build_svc.start_runtime_build.side_effect = RuntimeBuildError("boom", kind="start_failed")

    with pytest.raises(ProjectError) as err:
        svc.rollback_repo("proj-1", "r-1", image_tag=TAG_ONE, rolled_back_by=OWNER_OID)
    assert err.value.kind == "rollback_failed"

    (failed,) = [
        d for d in svc.list_deployments("r-1", stage="prod") if d.outcome is DeploymentOutcome.FAILED
    ]
    assert failed.image_tag == TAG_ONE
    assert failed.error and "boom" not in failed.error
    assert svc._get_repo("r-1").cicd_status == "failed"


def _github_claims(**over):
    from core.security_github_oidc import GitHubActionsClaims

    base: dict[str, Any] = {
        "repository": "acme/fraud-agent",
        "repository_owner": "acme",
        "ref": "refs/heads/dev",
        "sha": SHA_ONE,
        "actor": "jorge",
        "workflow_ref": "acme/fraud-agent/.github/workflows/build.yml@refs/heads/dev",
    }
    base.update(over)
    return GitHubActionsClaims(**base)


@pytest.fixture
def builds_env(build_svc):
    """The builds route's three module globals, all fakes — nothing may reach GitHub/CodeBuild."""
    import api.routes.builds as builds_module

    project_svc = _service(build_svc)
    seed(project_svc._store, _to_item(_REPOSITORY_PK, _repo()))

    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = MagicMock(org="acme", base_url=None)

    builds_module._build_svc = build_svc
    builds_module._conn_svc = conn_svc
    builds_module._project_svc = project_svc
    yield project_svc
    builds_module._build_svc = None
    builds_module._conn_svc = None
    builds_module._project_svc = None


def _builds_client(claims=None):
    """A ``dependency_overrides`` for ``verify_github_oidc`` (``test_builds_routes.py``'s idiom): FastAPI
    resolves the dependency object at include time, so a ``patch`` of the module attribute would not reach
    it and every request would 403 on real JWKS validation. The app mounts the real ``AppError`` handler,
    as ``main.py`` does, so a service error renders its wire instead of escaping the client."""
    import api.routes.builds as builds_module
    from core.errors import install_error_handlers

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(builds_module.router, prefix="/api/v1")
    app.dependency_overrides[builds_module.verify_github_oidc] = lambda: claims or _github_claims()
    return TestClient(app)


def _runtime_body(**over):
    body = {
        "agent_id": AGENT_ID,
        "image_tag": TAG_ONE,
        "ecr_repo": "111122223333.dkr.ecr.us-east-1.amazonaws.com/agp",
        "connection_id": "conn-1",
        "stage": "dev",
    }
    body.update(over)
    return body


def test_a_dev_build_appends_a_row_with_a_GITHUB_actor(builds_env, build_svc):
    """The ``actor_kind`` distinction: this actor is an OIDC-proven GitHub login, not
    an Entra oid. Rendering one as the other would misattribute a deployment."""
    r = _builds_client().post("/api/v1/builds/runtime", json=_runtime_body())
    assert r.status_code == 202

    (row,) = builds_env.list_deployments("r-1")
    assert row.stage == "dev"
    assert row.image_tag == TAG_ONE
    assert row.build_id == "build-1"
    assert row.outcome is DeploymentOutcome.STARTED
    assert row.actor == "jorge"
    assert row.actor_kind == "github"
    assert row.source_sha == SHA_ONE


def test_a_failed_dev_build_appends_a_FAILED_row(builds_env, build_svc):
    build_svc.start_runtime_build.side_effect = RuntimeBuildError("boom", kind="start_failed")
    r = _builds_client().post("/api/v1/builds/runtime", json=_runtime_body())
    assert r.status_code == 502

    (row,) = builds_env.list_deployments("r-1")
    assert row.outcome is DeploymentOutcome.FAILED
    assert row.actor_kind == "github"
    assert row.error and "boom" not in row.error


def test_a_refused_dev_build_appends_NOTHING(builds_env, build_svc):
    """A prod request over the OIDC path is refused before any deploy, and must not
    leave a phantom row. Also pins that the ("dev","prod") allowlist was NOT widened."""
    client = _builds_client()
    assert client.post("/api/v1/builds/runtime", json=_runtime_body(stage="prod")).status_code == 403
    assert client.post("/api/v1/builds/runtime", json=_runtime_body(stage="uat")).status_code == 422
    assert builds_env.list_deployments("r-1") == []
    build_svc.start_runtime_build.assert_not_called()


def _governed_row(pid="proj-1", principal="someone-else-oid", role="owner"):
    return ProjectRoleRecord(
        project_id=pid,
        principal_id=principal,
        principal_type="user",
        principal_display="Alex",
        role=role,
        granted_by="seed",
        granted_at=FIXED_TS,
    )


def _tenant_context(*, is_global=False, tenant_ids=("default",)):
    from services.tenant_resolver import TenantContext

    return TenantContext(is_global=is_global, tenant_ids=frozenset(tenant_ids), tenants=())


def _project_context(*, is_global=False, role=None, project_id="proj-1", roles=None):
    from services.project_resolver import ProjectContext

    def _as_role(v):
        return ROLE_NAMES[v] if isinstance(v, str) else v

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


_CLAIMS = {"viewer": "Platform.Viewer", "operator": "Platform.Operator", "admin": "Platform.Admin"}


def _claims_for(platform_role):
    return {
        "oid": OWNER_OID if platform_role == "operator" else f"{platform_role}-oid",
        "preferred_username": f"{platform_role}@x.com",
        "roles": [_CLAIMS[platform_role]],
    }


@pytest.fixture
def route_svc(build_svc):
    import api.routes.projects as projects_module

    svc = _service(build_svc)
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo()))
    _succeeded(svc, image_tag=TAG_ONE)
    projects_module._svc = svc
    return svc


@pytest.fixture
def client_factory_over_route_svc(entra_settings, role_svc, route_svc):
    built = []

    def _make(*, platform_role="operator", project_role=None, roles=None, project_ctx=None, tenant_ctx=None):
        import api.routes.project_repos as project_repos_module
        import api.routes.projects as projects_module
        import api.routes.users as users_module
        from core.errors import install_error_handlers

        users_module._tenant_resolver = _FakeTenantResolver(
            tenant_ctx if tenant_ctx is not None else _tenant_context()
        )
        users_module._project_resolver = _FakeProjectResolver(
            project_ctx
            if project_ctx is not None
            else _project_context(is_global=platform_role == "admin", role=project_role, roles=roles)
        )
        app = FastAPI()
        install_error_handlers(app)
        app.include_router(projects_module.router, prefix="/api/v1")
        app.include_router(project_repos_module.router, prefix="/api/v1")
        client = TestClient(app, headers={"Authorization": "Bearer fake-token"})
        patcher = patch("core.security_entra.verify_entra_token", return_value=_claims_for(platform_role))
        patcher.start()
        client._entra_patcher = patcher
        built.append(client)
        return client

    yield _make
    for client in built:
        client._entra_patcher.stop()


_ROLLBACK = "/api/v1/projects/proj-1/repos/r-1/rollback"


def test_owner_can_roll_back(client_factory_over_route_svc, route_svc, build_svc, role_svc):
    """The rollback row is attributed to the validated principal's Entra oid, never a body value; it is
    selected by outcome rather than by index, because the seeded row shares the fixed clock's
    ``started_at`` and "newest" is a tie."""
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory_over_route_svc(project_role="owner", roles={"proj-1": "owner"})

    r = client.post(_ROLLBACK, json={"image_tag": TAG_ONE})
    assert r.status_code == 202
    assert build_svc.start_runtime_build.call_args.kwargs["image_tag"] == TAG_ONE
    rows = route_svc.list_deployments("r-1", stage="prod")
    (rollback_row,) = [d for d in rows if d.outcome is DeploymentOutcome.STARTED]
    assert rollback_row.actor == OWNER_OID
    assert rollback_row.actor_kind == "entra"


def test_maintainer_cannot_roll_back(client_factory_over_route_svc, build_svc, role_svc):
    """Rollback is gated at least as strictly as promote — it is a write to production. A looser
    gate on the newer verb would bypass the older one."""
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory_over_route_svc(project_role="maintainer", roles={"proj-1": "maintainer"})

    r = client.post(_ROLLBACK, json={"image_tag": TAG_ONE})
    assert r.status_code == 403
    assert r.json()["detail"] == INSUFFICIENT
    build_svc.start_runtime_build.assert_not_called()


def test_viewer_cannot_roll_back(client_factory_over_route_svc, build_svc, role_svc):
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory_over_route_svc(project_role="viewer", roles={"proj-1": "viewer"})
    assert client.post(_ROLLBACK, json={"image_tag": TAG_ONE}).status_code == 403
    build_svc.start_runtime_build.assert_not_called()


def test_rollback_on_an_ungoverned_project_still_requires_a_real_owner(
    client_factory_over_route_svc, build_svc, role_svc
):
    """The STRICT gate, matching promote: a project with nobody accountable must not be
    rollbackable by a mere tenant member. The role store is never consulted, which is what pins
    the strict helper rather than the permissive one."""
    role_svc.list_for_project.return_value = []
    client = client_factory_over_route_svc(project_role=None)
    assert client.post(_ROLLBACK, json={"image_tag": TAG_ONE}).status_code == 403
    build_svc.start_runtime_build.assert_not_called()
    role_svc.has_role_rows.assert_not_called()


def test_a_foreign_tenant_gets_404_not_403(client_factory_over_route_svc, build_svc, role_svc):
    """Tenant gate runs FIRST (byte-identical 404), so a 403 can never confirm that a foreign
    tenant's project exists."""
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory_over_route_svc(
        project_role=None, tenant_ctx=_tenant_context(tenant_ids=["ten-OTHER"])
    )
    r = client.post(_ROLLBACK, json={"image_tag": TAG_ONE})
    assert r.status_code == 404
    assert r.json()["detail"] == "Project not found"
    build_svc.start_runtime_build.assert_not_called()


def test_an_unknown_rollback_target_is_a_409_with_a_fixed_detail(
    client_factory_over_route_svc, route_svc, build_svc, role_svc
):
    """A rejected tag is an ordinary state the UI renders, not a server fault — and the detail is
    a FIXED literal that never echoes the offending tag back."""
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory_over_route_svc(project_role="owner", roles={"proj-1": "owner"})

    r = client.post(_ROLLBACK, json={"image_tag": "a-1-neverbuilt"})
    assert r.status_code == 409
    assert r.json()["detail"] == "no such succeeded deployment to roll back to"
    assert "neverbuilt" not in r.text
    build_svc.start_runtime_build.assert_not_called()


def test_a_missing_image_tag_is_a_422(client_factory_over_route_svc, build_svc, role_svc):
    """``image_tag`` is REQUIRED — a rollback with no target must not fall back to any
    server-chosen tag."""
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory_over_route_svc(project_role="owner", roles={"proj-1": "owner"})
    assert client.post(_ROLLBACK, json={}).status_code == 422
    build_svc.start_runtime_build.assert_not_called()


_BUILDSPEC_ROW = {
    "pk": {"S": "deployment"},
    "sk": {"S": "r-1#dev#2026-07-31T12:20:17.180133+00:00#b62c"},
    "id": {"S": "dep-a76eb62c"},
    "repo_id": {"S": "r-1"},
    "agent_id": {"S": "a-1"},
    "stage": {"S": "dev"},
    "seq_key": {"S": "r-1#dev#2026-07-31T12:20:17.180133+00:00#b62c"},
    "image_tag": {"S": "a-1-abc1234"},
    "build_id": {"S": "p:1234-5678"},
    "outcome": {"S": "succeeded"},
    "started_at": {"S": "2026-07-31T12:20:17.180133+00:00"},
    "completed_at": {"S": "2026-07-31T12:20:17.180133+00:00"},
    "error": {"NULL": True},
}

_BUILDSPEC_FAILED_ROW = {
    **_BUILDSPEC_ROW,
    "sk": {"S": "r-1#dev#2026-07-31T12:20:17.223183+00:00#b2fe"},
    "seq_key": {"S": "r-1#dev#2026-07-31T12:20:17.223183+00:00#b2fe"},
    "id": {"S": "dep-2610b2fe"},
    "outcome": {"S": "failed"},
    "started_at": {"S": "2026-07-31T12:20:17.223183+00:00"},
    "completed_at": {"S": "2026-07-31T12:20:17.223183+00:00"},
    "error": {"S": "the runtime build failed"},
}


def _flatten(ddb_item):
    """Unwrap a low-level DDB item ({"S": v} / {"NULL": True}) into what boto3's RESOURCE
    interface hands `_parse_rows`. The buildspec uses the CLI (low-level, typed), the backend
    reads through the resource client (untyped) — this bridges the two so the fixture can stay a
    literal transcript of the CLI's own payload."""
    return {k: (None if "NULL" in v else v["S"]) for k, v in ddb_item.items()}


def _stored(svc, items):
    """Store the low-level items as the rows the backend reads back."""
    for item in items:
        seed(svc._store, _flatten(item))


def test_a_buildspec_written_row_round_trips(build_svc):
    """The shell-written terminal row parses back into a ``Deployment`` with every field intact, appended
    already closed and with no human actor, which the buildspec must not invent. A key or field drift
    here is invisible in production (the row is simply never read), so this is the only cheap place to
    catch it."""
    svc = _service(build_svc)
    _stored(svc, [_BUILDSPEC_ROW])

    (row,) = svc.list_deployments("r-1", stage="dev")
    assert row.id == "dep-a76eb62c"
    assert row.repo_id == "r-1"
    assert row.agent_id == "a-1"
    assert row.stage == "dev"
    assert row.image_tag == "a-1-abc1234"
    assert row.build_id == "p:1234-5678"
    assert row.outcome is DeploymentOutcome.SUCCEEDED
    assert row.started_at == "2026-07-31T12:20:17.180133+00:00"
    assert row.completed_at == row.started_at
    assert row.actor is None
    assert row.actor_kind is None
    assert row.error is None


def test_the_buildspec_sk_suffix_is_the_last_four_chars_of_the_id(build_svc):
    """A shell expansion such as ``${D_ID##*-}`` yields all 8 hex chars, not ``id[-4:]``. The suffix
    exists to break same-millisecond collisions, and a row written under a non-contract key is
    unreadable, so the arithmetic is pinned, not just the parse: the rest of the key is the pinned shape
    in the pinned order, the sk's time component is ``started_at`` itself, and the sk is mirrored onto
    the record for round-tripping."""
    for item in (_BUILDSPEC_ROW, _BUILDSPEC_FAILED_ROW):
        sk, ident = item["sk"]["S"], item["id"]["S"]
        started_at = item["started_at"]["S"]
        repo_id, stage, ts, suffix = sk.split("#")
        assert suffix == ident[-4:], "sk suffix must be id[-4:]"
        assert len(suffix) == 4
        assert (repo_id, stage) == ("r-1", "dev")
        assert ts == started_at
        assert item["seq_key"]["S"] == sk


def test_the_buildspec_timestamp_is_a_real_isoformat_not_a_literal_format_string(build_svc):
    """``date -u +%...%6N`` is not portable and can emit a literal ``6N``, which is neither parseable nor
    sortable, so the whole history would order wrongly. Parsing with ``fromisoformat`` is what gives
    this assertion teeth."""
    for item in (_BUILDSPEC_ROW, _BUILDSPEC_FAILED_ROW):
        ts = item["started_at"]["S"]
        parsed = datetime.fromisoformat(ts)
        assert parsed.tzinfo is not None, "must be UTC-aware, like every Python writer here"
        assert parsed.utcoffset() == timedelta(0)
        assert "N" not in ts and "%" not in ts


def test_a_buildspec_failed_row_round_trips_with_a_safe_hint(build_svc):
    """The error is a safe short hint: no ARN, no token, no raw upstream body."""
    svc = _service(build_svc)
    _stored(svc, [_BUILDSPEC_FAILED_ROW])

    (row,) = svc.list_deployments("r-1", stage="dev")
    assert row.outcome is DeploymentOutcome.FAILED
    assert row.error == "the runtime build failed"
    for leak in ("arn:", "aws_", "token", "secret", "Traceback", "http"):
        assert leak.lower() not in (row.error or "").lower()


def test_shell_and_python_written_rows_interleave_correctly(build_svc):
    """The two producers must be INDISTINGUISHABLE to the reader — same partition, one ordering.

    This is the case the live test exercises: the backend appends `started` when the build is
    requested, then the buildspec appends the terminal row minutes later. Both must appear in one
    newest-first history, ordered by time and not by which producer wrote them: the fixed clock puts
    the Python-written ``started`` row at 09:00 and the buildspec row at 12:20, so the buildspec row is
    genuinely newer. Same repo, stage, image tag and build means one deployment in two rows, and the
    actor currencies stay distinct: the request was made by a GitHub login, the build has no human
    actor at all."""
    svc = _service(build_svc)
    started = svc.append_deployment(
        repo_id="r-1",
        agent_id="a-1",
        stage="dev",
        image_tag="a-1-abc1234",
        build_id="p:1234-5678",
        actor="jorge",
        actor_kind="github",
    )
    python_item = {
        "sk": {"S": started.seq_key},
        "id": {"S": started.id},
        "repo_id": {"S": "r-1"},
        "agent_id": {"S": "a-1"},
        "stage": {"S": "dev"},
        "seq_key": {"S": started.seq_key},
        "image_tag": {"S": "a-1-abc1234"},
        "build_id": {"S": "p:1234-5678"},
        "outcome": {"S": "started"},
        "actor": {"S": "jorge"},
        "actor_kind": {"S": "github"},
        "started_at": {"S": started.started_at},
        "pk": {"S": "deployment"},
    }
    _stored(svc, [_BUILDSPEC_ROW, python_item])

    rows = svc.list_deployments("r-1", stage="dev")
    assert len(rows) == 2, "a producer mismatch drops a row silently"
    assert [r.outcome for r in rows] == [
        DeploymentOutcome.SUCCEEDED,
        DeploymentOutcome.STARTED,
    ]
    assert {r.image_tag for r in rows} == {"a-1-abc1234"}
    assert {r.build_id for r in rows} == {"p:1234-5678"}
    assert rows[1].actor_kind == "github" and rows[0].actor_kind is None


def test_a_buildspec_row_makes_rollback_reachable_end_to_end(build_svc):
    """Rollback validates against ``SUCCEEDED``, and until the buildspec wrote one that check could never
    pass; with the terminal row present the same validation admits the tag and still refuses one with no
    succeeded row. Only the deployment read comes from DDB here, the repo and project reads stay local:
    flipping the whole service onto a ``MagicMock`` table would serve deployment items to ``_get_repo``
    too, and the test would fail on a fixture artifact rather than on the contract."""
    svc = _service(build_svc)
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo()))
    prod_row = {
        **_BUILDSPEC_ROW,
        "sk": {"S": "r-1#prod#2026-07-31T12:20:17.180133+00:00#b62c"},
        "seq_key": {"S": "r-1#prod#2026-07-31T12:20:17.180133+00:00#b62c"},
        "stage": {"S": "prod"},
        "image_tag": {"S": TAG_ONE},
    }
    svc._rows.list_deployments = lambda repo_id, stage=None, limit=50: (
        _parse_rows(Deployment, [_flatten(prod_row)]) if repo_id == "r-1" and stage == "prod" else []
    )

    svc.rollback_repo("proj-1", "r-1", image_tag=TAG_ONE, rolled_back_by=OWNER_OID)
    assert build_svc.start_runtime_build.call_args.kwargs["image_tag"] == TAG_ONE

    with pytest.raises(ProjectError) as err:
        svc.rollback_repo("proj-1", "r-1", image_tag="a-1-neverbuilt", rolled_back_by=OWNER_OID)
    assert err.value.kind == "unknown_rollback_target"


_A_DIGEST = "sha256:" + "ab" * 32


def test_a_prod_rollback_CLEARS_the_promoted_digest(svc):
    """The two prod scalars must never DISAGREE.

    ``last_promoted_image_tag`` is repointed at the rolled-back tag (prod genuinely serves the older
    image, so leaving it on the newer one would make every list row lie). If the digest were left
    alone, the record would then read: tag = the rolled-back image, digest = the image we just
    rolled AWAY from. The digest is the field a later approval trusts, so that pair is worse than an
    absent digest. It is cleared and re-populated by the buildspec — the only writer that knows what
    the apply actually deployed. The repo is staged with a promoted digest from an earlier release."""
    repo = _repo()
    repo.last_promoted_image_tag = TAG_TWO
    repo.last_promoted_digest = _A_DIGEST
    seed(svc._store, _to_item(_REPOSITORY_PK, repo))
    _succeeded(svc, image_tag=TAG_ONE)

    svc.rollback_repo("proj-1", "r-1", image_tag=TAG_ONE, rolled_back_by=OWNER_OID)

    stored = svc.get_repo("r-1")
    assert stored.last_promoted_image_tag == TAG_ONE, "the tag must follow the rollback"
    assert stored.last_promoted_digest is None, (
        "a stale digest beside a rolled-back tag is a record that contradicts itself"
    )


def test_a_rollback_passes_no_digest_to_the_deploy(svc):
    """It has none to pass. The deploy therefore travels the tag path — which still works, and is
    precisely why the tag fields were kept alongside the digest fields rather than replaced."""
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo()))
    _succeeded(svc, image_tag=TAG_ONE)

    svc.rollback_repo("proj-1", "r-1", image_tag=TAG_ONE, rolled_back_by=OWNER_OID)

    kwargs = svc._builds.start_runtime_build.call_args.kwargs
    assert kwargs["image_tag"] == TAG_ONE
    assert not kwargs.get("image_digest"), kwargs


def test_a_NON_prod_rollback_leaves_the_prod_digest_alone(svc):
    """A ``uat`` rollback must not touch a PROD fact. The tag/actor/build-id trio is already scoped
    to prod for this reason, and the digest belongs to the same group — repointing (or clearing) it
    from a non-prod rollback would corrupt prod's record with non-prod data."""
    repo = _repo()
    repo.last_promoted_image_tag = TAG_TWO
    repo.last_promoted_digest = _A_DIGEST
    seed(svc._store, _to_item(_REPOSITORY_PK, repo))
    _succeeded(svc, stage="uat", image_tag=TAG_ONE)

    svc.rollback_repo("proj-1", "r-1", image_tag=TAG_ONE, rolled_back_by=OWNER_OID, stage="uat")

    stored = svc.get_repo("r-1")
    assert stored.last_promoted_digest == _A_DIGEST
    assert stored.last_promoted_image_tag == TAG_TWO
