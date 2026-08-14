"""Promote / build / rollback wired to the append-only ``Deployment`` partition (E28/T4, D7).

T3 built the partition and deliberately wired NO call sites. This file pins the wiring, and the
one invariant the whole epic turns on:

> **Two promotes leave two rows.** The first is NOT erased.

That is the entire point of D7. Promotion state used to live only as singular scalars on
``Repository`` (``last_promoted_*``), which are overwritten wholesale by the next promote — so a
second release erased the evidence of the first, and rollback had no artifact to roll back TO.

Four more things are pinned here because each is a way the wiring could be silently wrong:

1. **A failed build appends a ``FAILED`` row rather than vanishing.** A promote that could not
   start is exactly the event an operator needs in the history; with no row it looks like nobody
   ever tried.
2. **Rollback refuses a tag that never SUCCEEDED for that stage.** Without that validation the
   route is a deploy-anything primitive: an arbitrary caller-supplied tag would be applied to
   production. Refusal is asserted together with ``start_runtime_build.assert_not_called()`` —
   "returned 4xx" is not the same claim as "did not deploy".
3. **Rollback refuses a tag belonging to ANOTHER repo.** Same class, different axis: two repos
   share one tenant ECR registry, so repo B's tag is a resolvable image. The validation must be
   scoped to *this* repo's own history.
4. **Rollback is gated exactly like promote** — OWNER, through the STRICT gate. It is a write to
   production, so a looser gate on the newer verb would bypass the older one.

Actor currency (C1) is pinned too: a promote/rollback actor is an **Entra oid** with
``actor_kind="entra"``, while the OIDC dev-build actor is a **GitHub login** with
``actor_kind="github"``. They are two different currencies and must never be stored as one.

Harness idiom is ``test_promote_repo.py``'s: the REAL in-memory ``ProjectService``
(``table_name=""`` ⇒ the local dict fallback, no boto3/no moto), an injected fixed clock, every
collaborator a ``MagicMock`` so nothing reaches CodeBuild / Secrets Manager / GitHub / Entra, and
for the route tests the REAL ``require_role`` / ``current_principal`` / ``get_tenant_ctx`` /
``get_project_ctx`` chain against a mocked ``verify_entra_token`` (no ``dependency_overrides``).
Account ids, where they appear at all, are obviously-fake 12-digit values.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from models.deployment import Deployment, DeploymentOutcome
from models.project import Project, ProjectDetail  # noqa: F401 — ProjectDetail via get_project
from models.project_role import ROLE_NAMES, ProjectRoleRecord
from models.repository import Repository
from services.project_service import ProjectError, ProjectService, _parse_rows
from services.runtime_build_service import RuntimeBuildError

FIXED = datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc)
FIXED_TS = FIXED.isoformat()

AGENT_ID = "a-1"
# Two DISTINCT candidate tags: the whole point of test 1 is that promoting the second does not
# erase the first, which is unassertable if the two tags are equal.
TAG_ONE = "a-1-tree001"
TAG_TWO = "a-1-tree002"
SHA_ONE = "1" * 40
SHA_TWO = "2" * 40
# The Entra oid a promote/rollback is attributed to — NOT a GitHub login (C1 actor_kind).
OWNER_OID = "owner-oid"
INSUFFICIENT = "insufficient project role"


# --------------------------------------------------------------------------- #
# record helpers (test_promote_repo.py's idiom)
# --------------------------------------------------------------------------- #


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
    cicd_status="deployed",
    last_promoted_at=None,
    prod_candidate_image_tag=TAG_ONE,
    prod_candidate_sha=SHA_ONE,
    agent_id=AGENT_ID,
    name="fraud-agent",
):
    """A repo with a PENDING prod candidate, so it has something promotable (E27A)."""
    candidate = (
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


@pytest.fixture
def build_svc():
    """The ``RuntimeBuildService`` mock every deploy goes through. ``assert_not_called`` on it is
    how a refusal test proves nothing was deployed, not merely that a 4xx was returned."""
    svc = MagicMock()
    svc.start_runtime_build.return_value = "build-1"
    return svc


def _service(build_svc, *, now=None, tenant_id="default"):
    """The REAL in-memory ``ProjectService`` — ``table_name=""`` ⇒ the local dict fallback."""
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
    svc._local_projects["proj-1"] = _project(tenant_id=tenant_id)
    return svc


@pytest.fixture
def svc(build_svc):
    s = _service(build_svc)
    s._local_repos["r-1"] = _repo()
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


# =========================================================================== #
# 1) THE invariant: two promotes leave TWO rows (D7)
# =========================================================================== #


def test_two_promotes_leave_two_rows_and_the_first_is_not_erased(build_svc):
    """The entire point of D7. The ``last_promoted_*`` scalars can only ever remember the
    SECOND promote; the append-only partition must still hold the first."""
    clock = _Clock()
    svc = _service(build_svc, now=clock)
    svc._local_repos["r-1"] = _repo()

    svc.promote_repo("proj-1", "r-1", promoted_by=OWNER_OID)
    # A newer merge registers the SECOND candidate, and the first promote's in-flight window
    # is over (the clock has advanced past _PROMOTE_IN_FLIGHT_MINUTES).
    clock.at = FIXED + timedelta(hours=2)
    svc.record_prod_candidate(AGENT_ID, image_tag=TAG_TWO, sha=SHA_TWO, actor="merger-login")
    svc.promote_repo("proj-1", "r-1", promoted_by=OWNER_OID)

    rows = svc.list_deployments("r-1", stage="prod")
    assert len(rows) == 2, "the first promote's row was erased — D7's whole premise"
    # Newest first, and BOTH artifacts are remembered.
    assert [r.image_tag for r in rows] == [TAG_TWO, TAG_ONE]
    # The denormalized cache (D7) can remember at most ONE tag — which is exactly why the rows
    # have to exist. Since E28A/T6 it remembers NONE of them until a delivery actually succeeds:
    # neither promote got past `StartBuild` here, so the cache honestly claims nothing about what
    # prod serves, while both attempts survive in the partition.
    assert svc._local_repos["r-1"].last_promoted_image_tag is None
    assert svc._local_repos["r-1"].last_promotion_build_id == "build-1"  # …still attributed


def test_a_promote_row_carries_the_promote_facts(svc, build_svc):
    """Attribution, build id, sha and stage all land on the row — the Deployments tab reads
    this, and a row without a build id or actor is not an audit record."""
    svc.promote_repo("proj-1", "r-1", promoted_by=OWNER_OID)

    (row,) = svc.list_deployments("r-1")
    assert row.repo_id == "r-1"
    assert row.agent_id == AGENT_ID
    assert row.stage == "prod"
    assert row.image_tag == TAG_ONE
    assert row.source_sha == SHA_ONE
    assert row.build_id == "build-1"
    assert row.outcome is DeploymentOutcome.STARTED
    # C1: a promote's actor is an ENTRA oid, never a GitHub login.
    assert row.actor == OWNER_OID
    assert row.actor_kind == "entra"
    assert row.error is None


def test_a_failed_promote_appends_a_FAILED_row(svc, build_svc):
    """A build that could not start must not vanish from the history. With no row, "the deploy
    failed" and "nobody ever tried" are the same observation."""
    build_svc.start_runtime_build.side_effect = RuntimeBuildError("boom")

    with pytest.raises(ProjectError) as err:
        svc.promote_repo("proj-1", "r-1", promoted_by=OWNER_OID)
    assert err.value.kind == "promote_failed"

    (row,) = svc.list_deployments("r-1")
    assert row.outcome is DeploymentOutcome.FAILED
    assert row.image_tag == TAG_ONE
    assert row.build_id is None
    assert row.completed_at == FIXED_TS  # terminal: appended already closed
    assert row.actor == OWNER_OID and row.actor_kind == "entra"
    # SAFE hint only — the build service's own message must never reach the record.
    assert row.error and "boom" not in row.error


def test_a_promote_refused_before_the_build_appends_NOTHING(svc, build_svc):
    """A refusal is not a deployment. ``no_prod_candidate`` never reached CodeBuild, so a row
    would be a phantom attempt in the history."""
    svc._local_repos["r-1"] = _repo(prod_candidate_image_tag=None)

    with pytest.raises(ProjectError) as err:
        svc.promote_repo("proj-1", "r-1", promoted_by=OWNER_OID)
    assert err.value.kind == "no_prod_candidate"
    assert svc.list_deployments("r-1") == []
    build_svc.start_runtime_build.assert_not_called()


# =========================================================================== #
# 2) Rollback — the validation IS the feature
# =========================================================================== #


def test_rollback_deploys_a_previously_succeeded_tag(svc, build_svc):
    _succeeded(svc, image_tag=TAG_ONE)

    repo = svc.rollback_repo("proj-1", "r-1", image_tag=TAG_ONE, rolled_back_by=OWNER_OID)

    kwargs = build_svc.start_runtime_build.call_args.kwargs
    assert kwargs["image_tag"] == TAG_ONE
    assert kwargs["stage"] == "prod"
    assert kwargs["agent_id"] == AGENT_ID
    assert repo.cicd_status == "promoting"


def test_rollback_appends_its_own_row(svc, build_svc):
    """A rollback IS a deployment (D7) — it must be in the history, attributed, and
    distinguishable from the promote it reverts."""
    _succeeded(svc, image_tag=TAG_ONE)

    svc.rollback_repo("proj-1", "r-1", image_tag=TAG_ONE, rolled_back_by=OWNER_OID)

    rows = svc.list_deployments("r-1", stage="prod")
    assert len(rows) == 2  # the seeded SUCCEEDED row + the rollback's own
    # Selected by outcome, not by index: under the fixed clock both rows share `started_at`, so
    # "newest" is a tie and an index would silently assert on the seeded row.
    (row,) = [d for d in rows if d.outcome is DeploymentOutcome.STARTED]
    assert row.image_tag == TAG_ONE
    assert row.outcome is DeploymentOutcome.STARTED
    assert row.build_id == "build-1"
    assert row.actor == OWNER_OID and row.actor_kind == "entra"


def test_rollback_refuses_a_tag_that_never_succeeded_for_that_stage(svc, build_svc):
    """The whole point of the validation. A tag with no SUCCEEDED row in this stage is not a
    known-good artifact, and applying an arbitrary caller-supplied tag to production would be a
    deploy-anything primitive."""
    _succeeded(svc, image_tag=TAG_ONE)  # a DIFFERENT tag did succeed

    with pytest.raises(ProjectError) as err:
        svc.rollback_repo("proj-1", "r-1", image_tag="a-1-neverbuilt", rolled_back_by=OWNER_OID)
    assert err.value.kind == "unknown_rollback_target"
    build_svc.start_runtime_build.assert_not_called()  # refused BEFORE any deploy
    assert len(svc.list_deployments("r-1")) == 1  # nothing appended for a refusal


def test_rollback_refuses_a_tag_that_only_succeeded_in_ANOTHER_stage(svc, build_svc):
    """Stage-scoped, per D8. A tag proven good in ``dev`` is not thereby approved for ``prod`` —
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
    svc._local_repos["r-2"] = _repo(id="r-2", agent_id="a-2", name="other-agent")
    _succeeded(svc, repo_id="r-2", image_tag="a-2-tree001")

    with pytest.raises(ProjectError) as err:
        svc.rollback_repo("proj-1", "r-1", image_tag="a-2-tree001", rolled_back_by=OWNER_OID)
    assert err.value.kind == "unknown_rollback_target"
    build_svc.start_runtime_build.assert_not_called()


def test_rollback_refuses_a_tag_whose_only_row_FAILED(svc, build_svc):
    """``started`` and ``failed`` are not evidence an artifact ever ran. Only SUCCEEDED is."""
    svc.append_deployment(
        repo_id="r-1", agent_id=AGENT_ID, stage="prod", image_tag=TAG_TWO,
        outcome=DeploymentOutcome.FAILED, completed_at=FIXED_TS, error="build failed",
    )
    svc.append_deployment(
        repo_id="r-1", agent_id=AGENT_ID, stage="prod", image_tag=TAG_TWO,
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
    """Same ownership check ``promote_repo`` makes: without it an OWNER of project A could roll
    back a repo living under project B.

    The foreign project must EXIST for this to test the intended branch. An earlier version passed
    ``proj-OTHER``, which no fixture creates — so ``_get_project`` returned None and the 404 came
    from the missing-project path instead. Deleting the ``repo.project_id != project_id`` check
    kept that version green, i.e. it asserted nothing about ownership. With a real second project
    seeded, only the ownership check can produce this refusal."""
    svc._local_projects["proj-2"] = _project(id="proj-2")
    _succeeded(svc, image_tag=TAG_ONE)

    with pytest.raises(ProjectError) as err:
        svc.rollback_repo("proj-2", "r-1", image_tag=TAG_ONE, rolled_back_by=OWNER_OID)
    assert err.value.kind == "not_found"
    build_svc.start_runtime_build.assert_not_called()
    # The repo genuinely belongs to proj-1, and proj-2 genuinely exists — so the refusal is the
    # ownership check, not a missing parent.
    assert svc._local_repos["r-1"].project_id == "proj-1"
    assert svc._get_project("proj-2") is not None


# =========================================================================== #
# 3) The race: a rollback while a promote is in flight
# =========================================================================== #


def test_rollback_refuses_while_a_promote_is_in_flight(svc, build_svc):
    """Two builds against the SAME stage-scoped Terraform state key would race. Follows
    promote's own precedent — the bounded ``promoting`` guard — rather than a second mechanism."""
    svc._local_repos["r-1"] = _repo(cicd_status="promoting", last_promoted_at=FIXED_TS)
    _succeeded(svc, image_tag=TAG_ONE)

    with pytest.raises(ProjectError) as err:
        svc.rollback_repo("proj-1", "r-1", image_tag=TAG_ONE, rolled_back_by=OWNER_OID)
    assert err.value.kind == "promote_in_flight"
    build_svc.start_runtime_build.assert_not_called()


def test_a_promote_refuses_while_a_rollback_is_in_flight(build_svc):
    """The guard must work in BOTH directions, or the pair is not actually serialized: a
    rollback stamps the same in-flight state a promote refuses on."""
    svc = _service(build_svc)
    svc._local_repos["r-1"] = _repo()
    _succeeded(svc, image_tag=TAG_ONE)

    svc.rollback_repo("proj-1", "r-1", image_tag=TAG_ONE, rolled_back_by=OWNER_OID)
    build_svc.start_runtime_build.reset_mock()

    with pytest.raises(ProjectError) as err:
        svc.promote_repo("proj-1", "r-1", promoted_by=OWNER_OID)
    assert err.value.kind == "promote_in_flight"
    build_svc.start_runtime_build.assert_not_called()


def test_a_blank_stage_cannot_bypass_the_stage_scoped_validation(svc, build_svc):
    """A BLANK stage does not name an unknown stage — it DISABLES the scoping.

    `list_deployments` branches on `if stage:`, so `""` falls through to the CROSS-stage read and
    `_has_succeeded` would accept a tag that only ever succeeded in `dev` as a valid PROD rollback
    target. Before the fix this returned True and the rollback proceeded; it failed closed only by
    luck, via an unrelated KeyError on `tenant.stages[""]` deeper in the build service."""
    _succeeded(svc, stage="dev", image_tag="a-1-devonly")

    for blank in ("", "   ", "\t"):
        with pytest.raises(ProjectError) as err:
            svc.rollback_repo(
                "proj-1", "r-1", image_tag="a-1-devonly", stage=blank, rolled_back_by=OWNER_OID
            )
        assert err.value.kind == "unknown_rollback_target"
    build_svc.start_runtime_build.assert_not_called()
    # The bypass itself is gone at the helper level too, not merely refused one layer up.
    assert svc._has_succeeded("r-1", "", "a-1-devonly") is False


def test_the_rollback_request_model_rejects_a_blank_stage():
    """Rejected at the MODEL, so the route answers 422 before any handler logic runs."""
    from models.repository import RepoRollbackRequest

    assert RepoRollbackRequest(image_tag="t").stage == "prod"  # the default still works
    assert RepoRollbackRequest(image_tag="t", stage="uat").stage == "uat"  # free-form (D8)
    for blank in ("", " ", "\t"):
        with pytest.raises(ValidationError):
            RepoRollbackRequest(image_tag="t", stage=blank)


def test_a_blank_stage_is_a_422_at_the_route(client_factory, build_svc, role_svc):
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory(project_role="owner", roles={"proj-1": "owner"})
    r = client.post(_ROLLBACK, json={"image_tag": TAG_ONE, "stage": ""})
    assert r.status_code == 422
    build_svc.start_runtime_build.assert_not_called()


def test_the_in_flight_guard_arms_for_a_NON_prod_stage(build_svc):
    """The guard must arm for EVERY stage, not just prod.

    Both stamps used to sit under `if stage == "prod":`, so `_promotion_in_flight` (which reads
    `cicd_status` AND `last_promoted_at`) never armed for non-prod — two sequential `uat` rollbacks
    both reached `start_runtime_build` and raced the same stage-scoped Terraform state key."""
    svc = _service(build_svc)
    svc._local_repos["r-1"] = _repo()
    _succeeded(svc, stage="uat", image_tag="a-1-uat001")

    svc.rollback_repo(
        "proj-1", "r-1", image_tag="a-1-uat001", stage="uat", rolled_back_by=OWNER_OID
    )
    repo = svc._local_repos["r-1"]
    assert repo.cicd_status == "promoting"
    assert repo.last_promoted_at == FIXED_TS  # the guard's clock is stamped
    # …but the PROD delivery cache is NOT repointed by a uat rollback.
    assert repo.last_promoted_image_tag is None
    assert repo.last_promoted_by is None
    assert repo.last_promotion_build_id is None

    build_svc.start_runtime_build.reset_mock()
    with pytest.raises(ProjectError) as err:
        svc.rollback_repo(
            "proj-1", "r-1", image_tag="a-1-uat001", stage="uat", rolled_back_by=OWNER_OID
        )
    assert err.value.kind == "promote_in_flight"
    build_svc.start_runtime_build.assert_not_called()


def test_a_failed_non_prod_rollback_does_not_strand_the_record_at_promoting(build_svc):
    """A non-prod rollback that never started must land on `failed`, not linger at `promoting` —
    otherwise the bounded guard refuses retries for its whole window over a build that
    does not exist.

    ALSO pins the prod-stamp gate on the FAILURE path (E28D/IMP-1). The success path's gate is
    covered at `test_the_in_flight_guard_arms_for_a_NON_prod_stage`; this arm was unpinned, so
    stamping the prod cache from a failed `uat` rollback left the suite green. The gate is a
    `stamp=` keyword on the shared failure epilogue now, which is easier to fat-finger than the
    inline `if stage == "prod":` it replaced — so the assertion belongs here rather than in the
    reviewer's memory."""
    svc = _service(build_svc)
    svc._local_repos["r-1"] = _repo()
    _succeeded(svc, stage="uat", image_tag="a-1-uat001")
    build_svc.start_runtime_build.side_effect = RuntimeBuildError("boom")

    with pytest.raises(ProjectError) as err:
        svc.rollback_repo(
            "proj-1", "r-1", image_tag="a-1-uat001", stage="uat", rolled_back_by=OWNER_OID
        )
    assert err.value.kind == "rollback_failed"
    repo = svc._local_repos["r-1"]
    assert repo.cicd_status == "failed"
    # The PROD delivery cache is UNTOUCHED — a `uat` rollback must not overwrite a prod fact with
    # non-prod data, failed or not. All three start as None on this fixture, and each of these
    # three reddens if the epilogue's `stamp` is flipped to True (verified by mutation).
    assert repo.last_promoted_image_tag is None
    assert repo.last_promoted_by is None
    assert repo.last_promoted_at is None
    # Stated for the contract, NOT as the mutation's tripwire: the epilogue always passes
    # `build_id=None` (nothing started), so this one holds on both sides of that mutation. A
    # delivery that never began must never name a build id.
    assert repo.last_promotion_build_id is None
    (row,) = [
        d for d in svc.list_deployments("r-1", stage="uat")
        if d.outcome is DeploymentOutcome.FAILED
    ]
    assert row.image_tag == "a-1-uat001"


def test_a_stuck_rollback_is_recoverable(build_svc):
    """The in-flight refusal is BOUNDED, exactly as promote's is: past the CodeBuild build
    timeout the record is STUCK rather than in flight, and a permanently un-rollbackable repo is
    a worse failure than a double deploy."""
    svc = _service(build_svc)
    svc._local_repos["r-1"] = _repo(
        cicd_status="promoting", last_promoted_at=(FIXED - timedelta(hours=3)).isoformat()
    )
    _succeeded(svc, image_tag=TAG_ONE)

    svc.rollback_repo("proj-1", "r-1", image_tag=TAG_ONE, rolled_back_by=OWNER_OID)
    build_svc.start_runtime_build.assert_called_once()


# =========================================================================== #
# 4) The `last_promoted_*` cache after a rollback
# =========================================================================== #


def test_a_prod_rollback_repoints_the_last_promoted_cache(svc, build_svc):
    """DECISION: prod is now serving the OLDER tag, so the denormalized cache must follow it.
    Leaving it on the newer tag would make the list row (which reads the cache, not the history)
    claim production runs an image it does not."""
    svc.promote_repo("proj-1", "r-1", promoted_by=OWNER_OID)
    _succeeded(svc, image_tag=TAG_TWO)  # an older artifact that once ran in prod
    svc._local_repos["r-1"].cicd_status = "deployed"  # the promote settled
    # Prod now serves TAG_ONE, so the cache says so. Staged BY HAND because since E28A/T6
    # promote no longer writes this scalar (a started build is not a served image) — the
    # settled state is what a succeeded delivery leaves. It is staged as a NON-None older tag
    # on purpose: with the cache empty, "repointed" and "written for the first time" would be
    # the same observation and this test would stop fencing anything.
    svc._local_repos["r-1"].last_promoted_image_tag = TAG_ONE

    svc.rollback_repo("proj-1", "r-1", image_tag=TAG_TWO, rolled_back_by="rollback-oid")

    repo = svc._local_repos["r-1"]
    assert repo.last_promoted_image_tag == TAG_TWO
    assert repo.last_promoted_by == "rollback-oid"
    assert repo.last_promotion_build_id == "build-1"


def test_a_rollback_does_not_consume_the_prod_candidate(svc, build_svc):
    """A rollback is not an approval of ``main``. The pending candidate must survive so the
    OWNER can still promote it once the incident is over — clearing it would make the fix
    unreachable without a fresh commit."""
    _succeeded(svc, image_tag="a-1-older")

    svc.rollback_repo("proj-1", "r-1", image_tag="a-1-older", rolled_back_by=OWNER_OID)

    repo = svc._local_repos["r-1"]
    assert repo.prod_candidate_image_tag == TAG_ONE
    assert repo.prod_candidate_status == "pending"


def test_a_failed_rollback_appends_FAILED_and_stays_retryable(svc, build_svc):
    """Mirrors promote's failure discipline: attribute the attempt, persist ``failed``, leave
    the target rollbackable."""
    _succeeded(svc, image_tag=TAG_ONE)
    build_svc.start_runtime_build.side_effect = RuntimeBuildError("boom")

    with pytest.raises(ProjectError) as err:
        svc.rollback_repo("proj-1", "r-1", image_tag=TAG_ONE, rolled_back_by=OWNER_OID)
    assert err.value.kind == "rollback_failed"

    (failed,) = [
        d for d in svc.list_deployments("r-1", stage="prod")
        if d.outcome is DeploymentOutcome.FAILED
    ]
    assert failed.image_tag == TAG_ONE
    assert failed.error and "boom" not in failed.error
    assert svc._local_repos["r-1"].cicd_status == "failed"


# =========================================================================== #
# 5) The OIDC dev-build path — a build appends too, with a GITHUB actor
# =========================================================================== #


def _github_claims(**over):
    from core.security_github_oidc import GitHubActionsClaims

    base = {
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
    project_svc._local_repos["r-1"] = _repo()

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
    """A ``dependency_overrides`` for ``verify_github_oidc`` (``test_builds_routes.py``'s idiom) —
    FastAPI resolves the dependency object at include time, so a ``patch`` of the module attribute
    would not reach it and every request would 403 on real JWKS validation."""
    import api.routes.builds as builds_module

    app = FastAPI()
    app.include_router(builds_module.router, prefix="/api/v1")
    app.dependency_overrides[builds_module.verify_github_oidc] = lambda: claims or _github_claims()
    return TestClient(app)


def _runtime_body(**over):
    body = {
        "agent_id": AGENT_ID,
        "image_tag": TAG_ONE,
        "ecr_repo": "111122223333.dkr.ecr.us-east-1.amazonaws.com/agp",  # obviously fake
        "connection_id": "conn-1",
        "stage": "dev",
    }
    body.update(over)
    return body


def test_a_dev_build_appends_a_row_with_a_GITHUB_actor(builds_env, build_svc):
    """C1's actor_kind distinction, load-bearing: this actor is an OIDC-proven GitHub login, not
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
    build_svc.start_runtime_build.side_effect = RuntimeBuildError("boom")
    r = _builds_client().post("/api/v1/builds/runtime", json=_runtime_body())
    assert r.status_code == 502

    (row,) = builds_env.list_deployments("r-1")
    assert row.outcome is DeploymentOutcome.FAILED
    assert row.actor_kind == "github"
    assert row.error and "boom" not in row.error


def test_a_refused_dev_build_appends_NOTHING(builds_env, build_svc):
    """A prod request over the OIDC path is refused (E27 §5) before any deploy — and must not
    leave a phantom row. Also pins that the ("dev","prod") allowlist was NOT widened."""
    client = _builds_client()
    assert client.post("/api/v1/builds/runtime", json=_runtime_body(stage="prod")).status_code == 403
    assert client.post("/api/v1/builds/runtime", json=_runtime_body(stage="uat")).status_code == 422
    assert builds_env.list_deployments("r-1") == []
    build_svc.start_runtime_build.assert_not_called()


# =========================================================================== #
# 6) The route — gated exactly like promote
# =========================================================================== #


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


def _governed_row(pid="proj-1", principal="someone-else-oid", role="owner"):
    return ProjectRoleRecord(
        project_id=pid, principal_id=principal, principal_type="user",
        principal_display="Alex", role=role, granted_by="seed", granted_at=FIXED_TS,
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
def role_svc():
    import api.routes.projects as projects_module

    svc = MagicMock()
    svc.has_role_rows.return_value = False
    svc.list_all_strict.return_value = []
    svc.list_for_project.return_value = []
    projects_module._role_svc = svc
    return svc


@pytest.fixture
def route_svc(build_svc):
    import api.routes.projects as projects_module

    svc = _service(build_svc)
    svc._local_repos["r-1"] = _repo()
    _succeeded(svc, image_tag=TAG_ONE)
    projects_module._svc = svc
    return svc


@pytest.fixture
def client_factory(entra_settings, role_svc, route_svc):
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


_ROLLBACK = "/api/v1/projects/proj-1/repos/r-1/rollback"


def test_owner_can_roll_back(client_factory, route_svc, build_svc, role_svc):
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory(project_role="owner", roles={"proj-1": "owner"})

    r = client.post(_ROLLBACK, json={"image_tag": TAG_ONE})
    assert r.status_code == 202
    assert build_svc.start_runtime_build.call_args.kwargs["image_tag"] == TAG_ONE
    # Attributed to the validated principal's ENTRA oid, never a body value. Selected by
    # outcome rather than by index: the seeded row shares the fixed clock's `started_at`, so
    # "newest" is a tie and an index would assert on whichever row won the tie-break.
    rows = route_svc.list_deployments("r-1", stage="prod")
    (rollback_row,) = [d for d in rows if d.outcome is DeploymentOutcome.STARTED]
    assert rollback_row.actor == OWNER_OID
    assert rollback_row.actor_kind == "entra"


def test_maintainer_cannot_roll_back(client_factory, build_svc, role_svc):
    """Rollback is gated at least as strictly as promote — it is a write to production. A looser
    gate on the newer verb would bypass the older one."""
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory(project_role="maintainer", roles={"proj-1": "maintainer"})

    r = client.post(_ROLLBACK, json={"image_tag": TAG_ONE})
    assert r.status_code == 403
    assert r.json()["detail"] == INSUFFICIENT
    build_svc.start_runtime_build.assert_not_called()


def test_viewer_cannot_roll_back(client_factory, build_svc, role_svc):
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory(project_role="viewer", roles={"proj-1": "viewer"})
    assert client.post(_ROLLBACK, json={"image_tag": TAG_ONE}).status_code == 403
    build_svc.start_runtime_build.assert_not_called()


def test_rollback_on_an_ungoverned_project_still_requires_a_real_owner(
    client_factory, build_svc, role_svc
):
    """The STRICT gate, matching promote: a project with nobody accountable must not be
    rollbackable by a mere tenant member. The role store is never consulted, which is what pins
    the strict helper rather than the fallback-bearing one."""
    role_svc.list_for_project.return_value = []
    client = client_factory(project_role=None)
    assert client.post(_ROLLBACK, json={"image_tag": TAG_ONE}).status_code == 403
    build_svc.start_runtime_build.assert_not_called()
    role_svc.has_role_rows.assert_not_called()


def test_a_foreign_tenant_gets_404_not_403(client_factory, build_svc, role_svc):
    """Tenant gate runs FIRST (byte-identical 404), so a 403 can never confirm that a foreign
    tenant's project exists."""
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory(
        project_role=None, tenant_ctx=_tenant_context(tenant_ids=["ten-OTHER"])
    )
    r = client.post(_ROLLBACK, json={"image_tag": TAG_ONE})
    assert r.status_code == 404
    assert r.json()["detail"] == "Project not found"
    build_svc.start_runtime_build.assert_not_called()


def test_an_unknown_rollback_target_is_a_409_with_a_fixed_detail(
    client_factory, route_svc, build_svc, role_svc
):
    """A rejected tag is an ordinary state the UI renders, not a server fault — and the detail is
    a FIXED literal that never echoes the offending tag back."""
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory(project_role="owner", roles={"proj-1": "owner"})

    r = client.post(_ROLLBACK, json={"image_tag": "a-1-neverbuilt"})
    assert r.status_code == 409
    assert r.json()["detail"] == "no such succeeded deployment to roll back to"
    assert "neverbuilt" not in r.text
    build_svc.start_runtime_build.assert_not_called()


def test_a_missing_image_tag_is_a_422(client_factory, build_svc, role_svc):
    """``image_tag`` is REQUIRED — a rollback with no target must not fall back to any
    server-chosen tag."""
    role_svc.list_for_project.return_value = [_governed_row()]
    client = client_factory(project_role="owner", roles={"proj-1": "owner"})
    assert client.post(_ROLLBACK, json={}).status_code == 422
    build_svc.start_runtime_build.assert_not_called()


# =========================================================================== #
# 7) T4b — the BUILDSPEC-written terminal row must round-trip (contract C1)
# =========================================================================== #
#
# The buildspec (`modules/codebuild/buildspec.yml`, helper `_dep`) is the ONLY writer of a
# terminal `succeeded`/`failed` row: the backend appends `started` when a build is REQUESTED,
# and only the build itself knows how it ended.
#
# That row is written by a DIFFERENT producer, in a different language, from a shell context —
# so "does the reader see it?" is not implied by any other test in this suite. A drift in the sk
# shape does not raise: the row simply lands where `list_deployments` does not look, and the
# symptom is "history is empty" hours later during the live test. Two real bugs were caught this
# way while building T4b (an 8-char sk suffix where the contract says `id[-4:]`, and a
# non-portable `date +%6N` that emitted a literal "6N"), which is exactly why these assert on a
# LITERAL transcript of the helper's output rather than on a re-derivation of it.
#
# `_BUILDSPEC_ROW` below is copy-pasted from actually EXECUTING the helper with
# REPO_SK=r-1 STAGE=dev AGENT_ID=a-1 IMAGE_TAG=a-1-abc1234 CODEBUILD_BUILD_ID=p:1234-5678.
# If the buildspec changes shape, this fixture must be re-captured the same way — do not hand-edit
# it into agreement, because agreeing with a hand-edit is not evidence about the buildspec.

# A `succeeded` row exactly as the buildspec emits it (note: `error` present as a DDB NULL, the
# sk suffix is the LAST FOUR characters of `id`, and the timestamp is a Python isoformat).
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

# The `failed` variant — same helper, `_dep failed`. `error` is a SAFE fixed hint.
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


def _ddb(svc, items):
    """Flip the service onto its DDB branch with `query` returning `items`
    (`test_deployment_store.py`'s idiom)."""
    table = MagicMock()
    table.query.return_value = {"Items": [_flatten(i) for i in items]}
    svc._table = table
    svc.table_name = "projects"  # with _table set, this flips _has_ddb
    return table


def test_a_buildspec_written_row_round_trips(build_svc):
    """THE T4b test: the shell-written terminal row parses back into a `Deployment` with every
    field intact. A key/field drift here is invisible in production — the row is simply never
    read — so this is the only cheap place to catch it."""
    svc = _service(build_svc)
    _ddb(svc, [_BUILDSPEC_ROW])

    (row,) = svc.list_deployments("r-1", stage="dev")
    assert row.id == "dep-a76eb62c"
    assert row.repo_id == "r-1"
    assert row.agent_id == "a-1"
    assert row.stage == "dev"
    assert row.image_tag == "a-1-abc1234"
    assert row.build_id == "p:1234-5678"
    assert row.outcome is DeploymentOutcome.SUCCEEDED
    assert row.started_at == "2026-07-31T12:20:17.180133+00:00"
    assert row.completed_at == row.started_at  # terminal: appended already closed
    # A build has NO human actor, and the buildspec must not invent one.
    assert row.actor is None
    assert row.actor_kind is None
    assert row.error is None


def test_the_buildspec_sk_suffix_is_the_last_four_chars_of_the_id(build_svc):
    """The exact bug the first shell draft shipped: `${D_ID##*-}` expands to all 8 hex chars, not
    `id[-4:]`. The suffix exists to break same-millisecond collisions, and a row written under a
    non-contract key is unreadable — so pin the arithmetic, not just the parse."""
    for item in (_BUILDSPEC_ROW, _BUILDSPEC_FAILED_ROW):
        sk, ident = item["sk"]["S"], item["id"]["S"]
        started_at = item["started_at"]["S"]
        repo_id, stage, ts, suffix = sk.split("#")
        assert suffix == ident[-4:], "sk suffix must be id[-4:] (contract C1)"
        assert len(suffix) == 4
        # …and the rest of the key is the pinned shape, in the pinned order.
        assert (repo_id, stage) == ("r-1", "dev")
        assert ts == started_at  # the sk's time component IS started_at, not a second clock
        assert item["seq_key"]["S"] == sk  # the sk is mirrored onto the record for round-tripping


def test_the_buildspec_timestamp_is_a_real_isoformat_not_a_literal_format_string(build_svc):
    """The second shell bug: `date -u +%...%6N` is not portable and emitted a literal `6N`, which
    is neither parseable nor sortable — so the whole history would order wrongly. Parsing it with
    `fromisoformat` is what makes this assertion have teeth."""
    for item in (_BUILDSPEC_ROW, _BUILDSPEC_FAILED_ROW):
        ts = item["started_at"]["S"]
        parsed = datetime.fromisoformat(ts)
        assert parsed.tzinfo is not None, "must be UTC-aware, like every Python writer here"
        assert parsed.utcoffset() == timedelta(0)
        assert "N" not in ts and "%" not in ts


def test_a_buildspec_failed_row_round_trips_with_a_safe_hint(build_svc):
    svc = _service(build_svc)
    _ddb(svc, [_BUILDSPEC_FAILED_ROW])

    (row,) = svc.list_deployments("r-1", stage="dev")
    assert row.outcome is DeploymentOutcome.FAILED
    assert row.error == "the runtime build failed"
    # SAFE short hint only — no ARN, no token, no raw upstream body (C1).
    for leak in ("arn:", "aws_", "token", "secret", "Traceback", "http"):
        assert leak.lower() not in (row.error or "").lower()


def test_shell_and_python_written_rows_interleave_correctly(build_svc):
    """The two producers must be INDISTINGUISHABLE to the reader — same partition, one ordering.

    This is the case the live test exercises: the backend appends `started` when the build is
    requested, then the buildspec appends the terminal row minutes later. Both must appear in one
    newest-first history, ordered by time and not by which producer wrote them."""
    svc = _service(build_svc)
    # A Python-written `started` row, timestamped BEFORE the buildspec's terminal row.
    started = svc.append_deployment(
        repo_id="r-1", agent_id="a-1", stage="dev", image_tag="a-1-abc1234",
        build_id="p:1234-5678", actor="jorge", actor_kind="github",
    )
    python_item = {
        "sk": {"S": started.seq_key}, "id": {"S": started.id}, "repo_id": {"S": "r-1"},
        "agent_id": {"S": "a-1"}, "stage": {"S": "dev"}, "seq_key": {"S": started.seq_key},
        "image_tag": {"S": "a-1-abc1234"}, "build_id": {"S": "p:1234-5678"},
        "outcome": {"S": "started"}, "actor": {"S": "jorge"}, "actor_kind": {"S": "github"},
        "started_at": {"S": started.started_at}, "pk": {"S": "deployment"},
    }
    # DDB returns them newest-first (ScanIndexForward=False); the fixed clock puts the Python row
    # at 09:00 and the buildspec row at 12:20, so the buildspec row is genuinely newer.
    _ddb(svc, [_BUILDSPEC_ROW, python_item])

    rows = svc.list_deployments("r-1", stage="dev")
    assert len(rows) == 2, "a producer mismatch drops a row silently — the T15 failure mode"
    assert [r.outcome for r in rows] == [
        DeploymentOutcome.SUCCEEDED,
        DeploymentOutcome.STARTED,
    ]
    # Same repo, same stage, same image tag, same build — ONE deployment, two rows.
    assert {r.image_tag for r in rows} == {"a-1-abc1234"}
    assert {r.build_id for r in rows} == {"p:1234-5678"}
    # …and the actor currencies stay distinct (C1): the REQUEST was made by a GitHub login, the
    # BUILD has no human actor at all. Neither is laundered into the other.
    assert rows[1].actor_kind == "github" and rows[0].actor_kind is None


def test_a_buildspec_row_makes_rollback_reachable_end_to_end(build_svc):
    """The point of T4b: rollback validates against `SUCCEEDED`, and until the buildspec wrote one
    that check could never pass (my T4 finding C1). With the terminal row present, the SAME
    validation now admits the tag — and still refuses one that has no succeeded row."""
    svc = _service(build_svc)
    svc._local_repos["r-1"] = _repo()
    prod_row = {
        **_BUILDSPEC_ROW,
        "sk": {"S": "r-1#prod#2026-07-31T12:20:17.180133+00:00#b62c"},
        "seq_key": {"S": "r-1#prod#2026-07-31T12:20:17.180133+00:00#b62c"},
        "stage": {"S": "prod"},
        "image_tag": {"S": TAG_ONE},
    }
    # Only the DEPLOYMENT read comes from DDB here — the repo/project reads stay on the local
    # fallback. Flipping the whole service onto a MagicMock table would serve deployment items to
    # `_get_repo` too, and the test would fail on a fixture artifact rather than on the contract.
    svc.list_deployments = lambda repo_id, stage=None, limit=50: (
        _parse_rows(Deployment, [_flatten(prod_row)])
        if repo_id == "r-1" and stage == "prod"
        else []
    )

    # The buildspec-written row is what makes this succeed.
    svc.rollback_repo("proj-1", "r-1", image_tag=TAG_ONE, rolled_back_by=OWNER_OID)
    assert build_svc.start_runtime_build.call_args.kwargs["image_tag"] == TAG_ONE

    # …and the validation still has teeth against a tag no buildspec ever reported.
    with pytest.raises(ProjectError) as err:
        svc.rollback_repo("proj-1", "r-1", image_tag="a-1-neverbuilt", rolled_back_by=OWNER_OID)
    assert err.value.kind == "unknown_rollback_target"


# --------------------------------------------------------------------------- #
# E28B/T4 (D-B3) — a ROLLBACK cannot name its target's digest
# --------------------------------------------------------------------------- #
#
# A rollback target is validated against succeeded ``Deployment`` rows, and those rows record a TAG
# only (contract C1 has no digest field). So on a rollback nothing knows which bytes the older tag
# resolves to — and the honest answer is "unknown", not "whatever the last release used".

_A_DIGEST = "sha256:" + "ab" * 32


def test_a_prod_rollback_CLEARS_the_promoted_digest(svc):
    """The two prod scalars must never DISAGREE.

    ``last_promoted_image_tag`` is repointed at the rolled-back tag (prod genuinely serves the older
    image, so leaving it on the newer one would make every list row lie). If the digest were left
    alone, the record would then read: tag = the rolled-back image, digest = the image we just
    rolled AWAY from. The digest is the field a later approval trusts, so that pair is worse than an
    absent digest. It is cleared and re-populated by the buildspec — the only writer that knows what
    the apply actually deployed."""
    svc._local_repos["r-1"] = _repo()
    # Stage a repo that HAS a promoted digest from an earlier release.
    repo = svc._local_repos["r-1"]
    repo.last_promoted_image_tag = TAG_TWO
    repo.last_promoted_digest = _A_DIGEST
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
    svc._local_repos["r-1"] = _repo()
    _succeeded(svc, image_tag=TAG_ONE)

    svc.rollback_repo("proj-1", "r-1", image_tag=TAG_ONE, rolled_back_by=OWNER_OID)

    kwargs = svc._builds.start_runtime_build.call_args.kwargs
    assert kwargs["image_tag"] == TAG_ONE
    assert not kwargs.get("image_digest"), kwargs


def test_a_NON_prod_rollback_leaves_the_prod_digest_alone(svc):
    """A ``uat`` rollback must not touch a PROD fact. The tag/actor/build-id trio is already scoped
    to prod for this reason, and the digest belongs to the same group — repointing (or clearing) it
    from a non-prod rollback would corrupt prod's record with non-prod data."""
    svc._local_repos["r-1"] = _repo()
    repo = svc._local_repos["r-1"]
    repo.last_promoted_image_tag = TAG_TWO
    repo.last_promoted_digest = _A_DIGEST
    _succeeded(svc, stage="uat", image_tag=TAG_ONE)

    svc.rollback_repo("proj-1", "r-1", image_tag=TAG_ONE, rolled_back_by=OWNER_OID, stage="uat")

    stored = svc.get_repo("r-1")
    assert stored.last_promoted_digest == _A_DIGEST
    assert stored.last_promoted_image_tag == TAG_TWO
