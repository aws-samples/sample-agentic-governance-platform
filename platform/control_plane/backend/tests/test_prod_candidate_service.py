"""The prod-candidate store — ``ProjectService.record_prod_candidate`` (E27A/T5).

E27A's premise: prod deploys the artifact that was REVIEWED AND MERGED TO ``main``, not
whatever last landed on dev. The candidate is registered out-of-band by a GitHub-OIDC
workflow (``POST /builds/prod-candidate``, T6) which has NO project context — only an
``agent_id`` — so the resolution here is by ``agent_id``, not by ``(project_id, repo_id)``.

Two invariants this file exists to pin:

1. **A newer merge OVERWRITES all five fields.** ``main``'s HEAD is the only prod candidate.
   No queue, no history, no "behind by N" — precisely the complexity E27 §7 rejected.
2. **The backend cannot CLOBBER a candidate.** ``prod_candidate_*`` is written by this one
   method alone; every other ``_save_repo`` caller must leave it untouched even when saving a
   record it read BEFORE the candidate arrived. That is the load-bearing test below, and it is
   the mirror image of the ``last_dev_image_tag`` clobber class E27/T7 fixed: the CodeBuild
   write-back and a materialize/step save race the candidate route on the same DDB row.

Harness is the service seam ``test_promote_repo.py`` established: the REAL ``ProjectService``
with ``table_name=""`` (the local dict fallback — no boto3, NO moto), an injected clock and
id source so timestamps are asserted exactly, and every collaborator a ``MagicMock`` so
nothing can reach GitHub, CodeBuild, Secrets Manager or Entra. Assertions are against a
RE-READ (``get_repo``), never the returned object: the returned record is mutated in memory
before the save, so it cannot tell a persisted write from a dropped one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from models.project import Project
from models.repository import Repository
from services.project_service import (
    _PROD_CANDIDATE_FIELDS,
    ProjectError,
    ProjectService,
)

FIXED = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
FIXED_TS = FIXED.isoformat()

AGENT_ID = "a-1"
# The content-addressed tag shape E27A pins: {AGENT_ID}-{tree_sha[:7]} (design §2).
CANDIDATE_TAG = "a-1-tree111"
CANDIDATE_SHA = "1" * 40  # main's merge commit, full sha
CANDIDATE_ACTOR = "merger-login"

# E28B/T4 (D-B3): the digest joined the candidate block, so this list is now read FROM THE
# SERVICE rather than restated here. A local copy silently diverged the moment the production
# tuple grew a sixth member — and it would have kept asserting the old five while the real write
# named six, which is the "test that cannot fail" shape this epic has hit repeatedly. Importing it
# means a field added to (or dropped from) the block is covered by every loop below automatically.
_CANDIDATE_FIELDS = _PROD_CANDIDATE_FIELDS
# Pinned explicitly so the import above cannot silently become an empty/partial tuple and make
# every membership loop below vacuous.
assert set(_CANDIDATE_FIELDS) == {
    "prod_candidate_image_tag",
    "prod_candidate_digest",
    "prod_candidate_sha",
    "prod_candidate_actor",
    "prod_candidate_at",
    "prod_candidate_status",
}, _CANDIDATE_FIELDS
CANDIDATE_DIGEST = "sha256:" + "ab" * 32


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


def _repo(id="r-1", project_id="proj-1", agent_id=AGENT_ID, cicd_status="deployed"):
    """A materialized repo with NO candidate yet (the pre-E27A / post-promote state)."""
    return Repository(
        id=id,
        project_id=project_id,
        name="fraud-agent",
        repo_url="https://github.com/acme/fraud-agent",
        agent_id=agent_id,
        template_name="strands-agentcore",
        cicd_status=cicd_status,
        status="provisioning",
        created_by="operator@x.com",
        created_at=FIXED_TS,
        updated_at=FIXED_TS,
        last_dev_image_tag="a-1-tree000",
    )


@pytest.fixture
def svc():
    """The REAL in-memory ``ProjectService`` — ``table_name=""`` ⇒ the local dict fallback.

    The service is real because the whole contract under test is what it RESOLVES and
    PERSISTS, which a MagicMock would answer for. Every collaborator is mocked, so no test
    here can reach a network dependency."""
    service = ProjectService(
        table_name="",
        registry=MagicMock(),
        identity=MagicMock(),
        connection_service=MagicMock(),
        github_repo_service=MagicMock(),
        runtime_build_service=MagicMock(),
        ecr_repository="platform-fallback-ecr",
        now=lambda: FIXED,
    )
    service._local_projects["proj-1"] = _project()
    service._local_repos["r-1"] = _repo()
    return service


# --- registering a candidate --------------------------------------------------


def test_record_prod_candidate_stamps_all_six_fields(svc):
    """All six fields land, ``status="pending"``, and ``prod_candidate_at`` comes from the
    INJECTED clock — a service that read the wall clock inline would be untestable and would
    also drift from every other timestamp on the record."""
    svc.record_prod_candidate(
        AGENT_ID, image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR,
        image_digest=CANDIDATE_DIGEST,
    )
    stored = svc.get_repo("r-1")
    assert stored.prod_candidate_image_tag == CANDIDATE_TAG
    assert stored.prod_candidate_digest == CANDIDATE_DIGEST
    assert stored.prod_candidate_sha == CANDIDATE_SHA
    assert stored.prod_candidate_actor == CANDIDATE_ACTOR
    assert stored.prod_candidate_at == FIXED_TS
    assert stored.prod_candidate_status == "pending"


def test_a_candidate_with_no_digest_stores_None_not_empty_string(svc):
    """E28B/T4. A digest-less registration (a pre-E28B agent repo, or the legacy
    ``/prod-candidate`` route which has no digest in scope) must store ``None``, never ``""``.

    ``promote_repo`` tests this field for TRUTHINESS to decide whether it has an approved digest
    to deploy, so an empty string is a present-but-unusable value that would flow through as the
    reference ``<ecr_repo>@`` — deploying an image the build cannot name, which is the exact
    failure E28A/T1b's rule exists to prevent."""
    svc.record_prod_candidate(
        AGENT_ID, image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR,
        image_digest="",
    )
    stored = svc.get_repo("r-1")
    assert stored.prod_candidate_digest is None
    assert stored.prod_candidate_image_tag == CANDIDATE_TAG  # the tag-only path still registers


def test_record_prod_candidate_resolves_by_agent_id_not_repo_id(svc):
    """The OIDC path has no project/repo context — only the ``agent_id`` its token is bound
    to. A second repo under a DIFFERENT agent must be left alone."""
    svc._local_repos["r-2"] = _repo(id="r-2", agent_id="a-OTHER")
    svc.record_prod_candidate(
        AGENT_ID, image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR
    )
    assert svc.get_repo("r-1").prod_candidate_image_tag == CANDIDATE_TAG
    assert svc.get_repo("r-2").prod_candidate_image_tag is None


def test_a_newer_merge_overwrites_the_whole_candidate(svc):
    """``main``'s HEAD is the ONLY prod candidate (design §4): a second merge REPLACES all
    five fields rather than queueing behind the first. No accumulation, no history, so there
    is no decline-vs-supersede semantics to model."""
    svc.record_prod_candidate(
        AGENT_ID, image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR
    )
    svc.record_prod_candidate(
        AGENT_ID, image_tag="a-1-tree222", sha="2" * 40, actor="second-merger"
    )
    stored = svc.get_repo("r-1")
    assert stored.prod_candidate_image_tag == "a-1-tree222"
    assert stored.prod_candidate_sha == "2" * 40
    assert stored.prod_candidate_actor == "second-merger"
    assert stored.prod_candidate_status == "pending"


def test_record_prod_candidate_leaves_last_dev_image_tag_alone(svc):
    """The two tags are independent facts: ``last_dev_image_tag`` is what dev is RUNNING
    (CodeBuild-exclusive), the candidate is what ``main`` OFFERS. The FE shows both so an
    OWNER can see whether prod is about to match dev or move past it (design §5)."""
    svc.record_prod_candidate(
        AGENT_ID, image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR
    )
    assert svc.get_repo("r-1").last_dev_image_tag == "a-1-tree000"


def test_record_prod_candidate_does_not_transition_cicd_status(svc):
    """Registering a candidate is NOT a deployment: the badge must keep reading ``deployed``.
    A candidate save that carried ``include_cicd_status`` would make a merge to ``main`` look
    like a dev deploy in the UI."""
    svc.record_prod_candidate(
        AGENT_ID, image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR
    )
    assert svc.get_repo("r-1").cicd_status == "deployed"


def test_unknown_agent_id_is_not_found_and_persists_nothing(svc):
    """A token proving a repo whose agent AGP does not know is a 404 at the route (T6), and
    NOTHING may be written — an unknown agent must not create or touch a row."""
    with pytest.raises(ProjectError) as ei:
        svc.record_prod_candidate(
            "a-NOPE", image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR
        )
    assert ei.value.kind == "not_found"
    stored = svc.get_repo("r-1")
    for field in _CANDIDATE_FIELDS:
        assert getattr(stored, field) is None
    assert list(svc._local_repos) == ["r-1"]  # no row invented


# --- the clobber guard (the load-bearing one) ---------------------------------


def test_an_unrelated_backend_save_does_not_wipe_a_stored_candidate(svc):
    """THE load-bearing test: ``prod_candidate_*`` is candidate-route-EXCLUSIVE.

    A whole-item write of a record read BEFORE the candidate arrived would reset all five
    fields to the stale ``None`` it read — and the promote route cannot distinguish a wiped
    candidate from "nothing has merged to main", so the OWNER's Promote button would simply
    stop working with no error anywhere. Exactly the ``last_dev_image_tag`` clobber class
    E27/T7 fixed, and the race is real: the CodeBuild write-back, a materialize step save and
    the candidate route all write the same DDB row.

    Staged as the genuine race: read a snapshot, register a candidate, then save the STALE
    snapshot (mutating an unrelated field, as a step/status save does)."""
    stale = svc.get_repo("r-1")  # a snapshot with no candidate on it
    svc.record_prod_candidate(
        AGENT_ID, image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR
    )

    stale.repo_url = "https://github.com/acme/renamed"
    svc._save_repo(stale)  # the DEFAULT save — must not name the candidate fields

    stored = svc.get_repo("r-1")
    assert stored.repo_url == "https://github.com/acme/renamed"   # the intended write landed
    assert stored.prod_candidate_image_tag == CANDIDATE_TAG        # …and the candidate survived
    assert stored.prod_candidate_sha == CANDIDATE_SHA
    assert stored.prod_candidate_actor == CANDIDATE_ACTOR
    assert stored.prod_candidate_at == FIXED_TS
    assert stored.prod_candidate_status == "pending"


def test_a_cicd_status_save_also_does_not_wipe_a_stored_candidate(svc):
    """``include_cicd_status=True`` opts into ONE co-owned attribute, not into the candidate.
    Every in-flight transition (create / retry / finalize / mark-failed / the promote failure
    path) takes this branch, so a leak here would wipe the candidate on the most common saves
    in the service."""
    svc.record_prod_candidate(
        AGENT_ID, image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR
    )
    stale = _repo(cicd_status="failed")  # a stale read, no candidate on it
    svc._save_repo(stale, include_cicd_status=True)

    stored = svc.get_repo("r-1")
    assert stored.cicd_status == "failed"                          # the intended write landed
    assert stored.prod_candidate_image_tag == CANDIDATE_TAG
    assert stored.prod_candidate_status == "pending"


def test_the_ddb_update_expression_omits_the_candidate_fields_by_default(svc):
    """The local dict fallback only MIRRORS the DDB semantics, so the guard is also pinned on
    the real write path: a default ``_save_repo`` in DDB mode must not NAME the candidate
    fields in its ``SET`` expression (an unnamed attribute is what "survives" means there)."""
    table = MagicMock()
    svc._table = table
    svc.table_name = "projects"  # with _table set, this flips _has_ddb

    svc._save_repo(_repo())
    named = table.update_item.call_args.kwargs["ExpressionAttributeNames"].values()
    for field in _CANDIDATE_FIELDS:
        assert field not in named
    assert "last_dev_image_tag" not in named  # E27/T7's guard, still in force
    assert "cicd_status" not in named

    table.reset_mock()
    svc._save_repo(_repo(), include_prod_candidate=True)
    named = table.update_item.call_args.kwargs["ExpressionAttributeNames"].values()
    for field in _CANDIDATE_FIELDS:
        assert field in named  # the opt-in is what lets the candidate be written/cleared
    assert "last_dev_image_tag" not in named  # …and it never widens the CodeBuild guard


# --- the MIRROR clobber: a candidate must not revert the promotion audit ------
#
# `_save_repo` is a read-modify-write — it SETs every attribute except a skip set — and
# `last_promoted_*` is in NO skip set. So a candidate registered off a record read moments
# earlier used to re-SET the promotion stamp back to the stale value (typically None).
# That is not only lost audit history: `_promotion_in_flight` is measured FROM
# `last_promoted_at`, so a reverted stamp makes the guard FAIL OPEN and a second promote can
# start a second CodeBuild run against the SAME Terraform state key. The fix is that
# `record_prod_candidate` writes ONLY the six attributes it owns.

PROMOTED_AT = FIXED_TS
_PROMOTION_AUDIT = {
    "last_promoted_by": "oid-owner",
    "last_promoted_at": PROMOTED_AT,
    "last_promoted_image_tag": "a-1-tree000",
    "last_promotion_build_id": "build-1",
}


def _promoted_repo(**over):
    """A repo mid-promotion: the four audit fields stamped and ``cicd_status="promoting"``."""
    repo = _repo(cicd_status="promoting")
    for field, value in {**_PROMOTION_AUDIT, **over}.items():
        setattr(repo, field, value)
    return repo


def _stage_the_promotion_during_the_scan(svc):
    """Stage the genuine race, deterministically: the candidate route's resolution
    (``find_repository_by_agent_id`` — a full-partition SCAN + hydrate, a wide window)
    completes BEFORE the promotion stamps the row, so the record it goes on to write is stale.

    The staging has to pin the read this way round: with a fresh read the whole-record save
    re-writes the audit values it just read and the bug is invisible. Staleness IS the bug."""
    stale = svc.find_repository_by_agent_id(AGENT_ID)  # the scan, pre-promotion
    svc._local_repos["r-1"] = _promoted_repo()  # the promotion lands inside that window
    svc.find_repository_by_agent_id = MagicMock(return_value=stale)


def test_registering_a_candidate_preserves_the_promotion_audit(svc):
    """A merge to ``main`` landing DURING a promotion must not revert its audit stamp.

    Before the targeted write, this whole-record save re-SET all four ``last_promoted_*``
    attributes from the stale snapshot — i.e. back to ``None`` — destroying the audit record on
    the product's highest-consequence verb (an FSI requirement of this epic)."""
    _stage_the_promotion_during_the_scan(svc)

    svc.record_prod_candidate(
        AGENT_ID, image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR
    )

    stored = svc.get_repo("r-1")
    assert stored.prod_candidate_image_tag == CANDIDATE_TAG  # the intended write landed
    for field, value in _PROMOTION_AUDIT.items():
        assert getattr(stored, field) == value, field


def test_a_candidate_registration_does_not_defeat_the_in_flight_guard(svc):
    """The CONSEQUENCE of the mirror clobber, end-to-end: with ``last_promoted_at`` reverted to
    ``None``, ``_promotion_in_flight`` fails open (it cannot date the attempt, so it does not
    block) and a SECOND promote starts a SECOND CodeBuild run against the same Terraform state
    key. This is the test that proves the guard still holds after a candidate registration."""
    _stage_the_promotion_during_the_scan(svc)

    svc.record_prod_candidate(
        AGENT_ID, image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR
    )

    with pytest.raises(ProjectError) as ei:
        svc.promote_repo("proj-1", "r-1", promoted_by="oid-second")
    assert ei.value.kind == "promote_in_flight"
    svc._builds.start_runtime_build.assert_not_called()  # no second build against the state key


def test_the_candidate_write_names_only_the_six_fields_it_owns_in_ddb(svc):
    """The DDB branch, on the real write path: the candidate's ``SET`` must name EXACTLY the
    five candidate fields plus ``updated_at``. The local dict only mirrors these semantics, so
    the guard is pinned where the write actually happens — anything else named here is an
    attribute a stale read could revert (``last_promoted_*`` being the dangerous one)."""
    table = MagicMock()
    table.query.return_value = {  # find_repository_by_agent_id scans the partition
        "Items": [{"pk": "repository", "sk": "r-1", **_promoted_repo().model_dump()}]
    }
    svc._table = table
    svc.table_name = "projects"  # with _table set, this flips _has_ddb

    svc.record_prod_candidate(
        AGENT_ID, image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR
    )

    named = set(table.update_item.call_args.kwargs["ExpressionAttributeNames"].values())
    assert named == set(_CANDIDATE_FIELDS) | {"updated_at"}
    for field in _PROMOTION_AUDIT:
        assert field not in named  # an unnamed attribute is what "survives" means in DDB
