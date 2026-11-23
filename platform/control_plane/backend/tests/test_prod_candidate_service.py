"""The prod-candidate store, ``ProjectService.record_prod_candidate``.

Prod deploys the artifact that was reviewed and merged to ``main``, not whatever last landed on dev. The
candidate is registered out-of-band by a GitHub-OIDC workflow (``POST /builds/prod-candidate``) that has
no project context, only an ``agent_id``, so the resolution here is by ``agent_id``, not by
``(project_id, repo_id)``.

Two invariants this file pins. A newer merge overwrites every candidate field: ``main``'s HEAD is the
only prod candidate, with no queue and no history. The backend cannot clobber a candidate:
``prod_candidate_*`` is written by this one method alone, and every other ``_save_repo`` caller leaves
it untouched even when saving a record it read before the candidate arrived, because the CodeBuild
write-back, a materialize step save and the candidate route all write the same DDB row.

The real ``ProjectService`` with ``table_name=""`` (the in-memory store), an injected clock
and id source so timestamps are asserted exactly, and every collaborator a ``MagicMock``. Assertions are
against a re-read (``get_repo``), never the returned object: the returned record is mutated in memory
before the save, so it cannot tell a persisted write from a dropped one. Every test runs under
``passing_eval_gate`` so the eval gate (``test_promote_eval_gate.py``) stays out of the way.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from seed import seed

from core.state_store import StateStore
from models.project import Project
from models.repository import CicdStatus, Repository
from services.project_errors import ProjectError
from services.project_service import ProjectService
from services.project_store import _PROD_CANDIDATE_FIELDS, _PROJECT_PK, _REPOSITORY_PK, _to_item

pytestmark = pytest.mark.usefixtures("passing_eval_gate")

FIXED = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
FIXED_TS = FIXED.isoformat()

AGENT_ID = "a-1"
CANDIDATE_TAG = "a-1-tree111"
CANDIDATE_SHA = "1" * 40
CANDIDATE_ACTOR = "merger-login"
_CANDIDATE_FIELDS = _PROD_CANDIDATE_FIELDS
CANDIDATE_DIGEST = "sha256:" + "ab" * 32


def test_the_candidate_block_read_from_the_service_is_the_six_fields_the_loops_below_iterate():
    """The field list is imported, not restated, so a field added to the block is covered by every
    membership loop; pinning its members keeps those loops from going vacuous on an empty tuple."""
    assert set(_CANDIDATE_FIELDS) == {
        "prod_candidate_image_tag",
        "prod_candidate_digest",
        "prod_candidate_sha",
        "prod_candidate_actor",
        "prod_candidate_at",
        "prod_candidate_status",
    }


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


def _repo(id="r-1", project_id="proj-1", agent_id=AGENT_ID, cicd_status: CicdStatus = "deployed"):
    """A materialized repo with no candidate yet (the post-promote state)."""
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
    """The real in-memory ``ProjectService``: the contract under test is what it resolves and persists,
    which a ``MagicMock`` would answer for; every collaborator is mocked."""
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
    seed(service._store, _to_item(_PROJECT_PK, _project()))
    seed(service._store, _to_item(_REPOSITORY_PK, _repo()))
    return service


def test_record_prod_candidate_stamps_all_six_fields(svc):
    """All six fields land, ``status="pending"``, and ``prod_candidate_at`` comes from the
    INJECTED clock — a service that read the wall clock inline would be untestable and would
    also drift from every other timestamp on the record."""
    svc.record_prod_candidate(
        AGENT_ID,
        image_tag=CANDIDATE_TAG,
        sha=CANDIDATE_SHA,
        actor=CANDIDATE_ACTOR,
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
    """A digest-less registration (an agent repo registered before digests existed, or the
    ``/prod-candidate`` route, which has no digest in scope) stores ``None``, never ``""``: ``promote_repo``
    tests the field for truthiness to decide whether it has an approved digest, so an empty string would
    flow through as the reference ``<ecr_repo>@``, an image the build cannot name."""
    svc.record_prod_candidate(
        AGENT_ID,
        image_tag=CANDIDATE_TAG,
        sha=CANDIDATE_SHA,
        actor=CANDIDATE_ACTOR,
        image_digest="",
    )
    stored = svc.get_repo("r-1")
    assert stored.prod_candidate_digest is None
    assert stored.prod_candidate_image_tag == CANDIDATE_TAG


def test_record_prod_candidate_resolves_by_agent_id_not_repo_id(svc):
    """The OIDC path has no project/repo context — only the ``agent_id`` its token is bound
    to. A second repo under a DIFFERENT agent must be left alone."""
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo(id="r-2", agent_id="a-OTHER")))
    svc.record_prod_candidate(AGENT_ID, image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR)
    assert svc.get_repo("r-1").prod_candidate_image_tag == CANDIDATE_TAG
    assert svc.get_repo("r-2").prod_candidate_image_tag is None


def test_a_newer_merge_overwrites_the_whole_candidate(svc):
    """``main``'s HEAD is the only prod candidate: a second merge replaces every candidate field rather
    than queueing behind the first, so there is no decline-versus-supersede semantics to model."""
    svc.record_prod_candidate(AGENT_ID, image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR)
    svc.record_prod_candidate(AGENT_ID, image_tag="a-1-tree222", sha="2" * 40, actor="second-merger")
    stored = svc.get_repo("r-1")
    assert stored.prod_candidate_image_tag == "a-1-tree222"
    assert stored.prod_candidate_sha == "2" * 40
    assert stored.prod_candidate_actor == "second-merger"
    assert stored.prod_candidate_status == "pending"


def test_record_prod_candidate_leaves_last_dev_image_tag_alone(svc):
    """The two tags are independent facts: ``last_dev_image_tag`` is what dev is RUNNING
    (CodeBuild-exclusive), the candidate is what ``main`` OFFERS. The FE shows both so an
    OWNER can see whether prod is about to match dev or move past it."""
    svc.record_prod_candidate(AGENT_ID, image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR)
    assert svc.get_repo("r-1").last_dev_image_tag == "a-1-tree000"


def test_record_prod_candidate_does_not_transition_cicd_status(svc):
    """Registering a candidate is NOT a deployment: the badge must keep reading ``deployed``.
    A candidate save that carried ``include_cicd_status`` would make a merge to ``main`` look
    like a dev deploy in the UI."""
    svc.record_prod_candidate(AGENT_ID, image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR)
    assert svc.get_repo("r-1").cicd_status == "deployed"


def test_unknown_agent_id_is_not_found_and_persists_nothing(svc):
    """A token proving a repo whose agent AGP does not know is a 404 at the route, and nothing may be
    written: an unknown agent must not create or touch a row."""
    with pytest.raises(ProjectError) as ei:
        svc.record_prod_candidate("a-NOPE", image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR)
    assert ei.value.kind == "agent_not_found"
    stored = svc.get_repo("r-1")
    for field in _CANDIDATE_FIELDS:
        assert getattr(stored, field) is None
    assert [r.id for r in svc._load_all_repos()] == ["r-1"]


def test_an_unrelated_backend_save_does_not_wipe_a_stored_candidate(svc):
    """``prod_candidate_*`` is candidate-route-exclusive: a whole-item write of a record read before the
    candidate arrived would reset the fields to the stale ``None`` it read, and the promote route cannot
    distinguish a wiped candidate from "nothing has merged to main", so the Promote button would stop
    working with no error anywhere. Staged as the genuine race: read a snapshot, register a candidate,
    then save the stale snapshot with an unrelated field mutated, as a step or status save does."""
    stale = svc.get_repo("r-1")
    svc.record_prod_candidate(AGENT_ID, image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR)

    stale.repo_url = "https://github.com/acme/renamed"
    svc._save_repo(stale)

    stored = svc.get_repo("r-1")
    assert stored.repo_url == "https://github.com/acme/renamed"
    assert stored.prod_candidate_image_tag == CANDIDATE_TAG
    assert stored.prod_candidate_sha == CANDIDATE_SHA
    assert stored.prod_candidate_actor == CANDIDATE_ACTOR
    assert stored.prod_candidate_at == FIXED_TS
    assert stored.prod_candidate_status == "pending"


def test_a_cicd_status_save_also_does_not_wipe_a_stored_candidate(svc):
    """``include_cicd_status=True`` opts into ONE co-owned attribute, not into the candidate.
    Every in-flight transition (create / retry / finalize / mark-failed / the promote failure
    path) takes this branch, so a leak here would wipe the candidate on the most common saves
    in the service."""
    svc.record_prod_candidate(AGENT_ID, image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR)
    stale = _repo(cicd_status="failed")
    svc._save_repo(stale, include_cicd_status=True)

    stored = svc.get_repo("r-1")
    assert stored.cicd_status == "failed"
    assert stored.prod_candidate_image_tag == CANDIDATE_TAG
    assert stored.prod_candidate_status == "pending"


def _on_table(svc, table):
    svc._store = StateStore("projects", table=table)


def test_the_update_expression_omits_the_candidate_fields_by_default(svc):
    """The guard is pinned on the write itself: a default ``_save_repo`` names neither the candidate
    fields nor ``last_dev_image_tag`` in its ``SET`` expression (an unnamed attribute is what "survives"
    means there), and the ``include_prod_candidate`` opt-in names the candidate without widening the
    CodeBuild guard."""
    table = MagicMock()
    _on_table(svc, table)

    svc._save_repo(_repo())
    named = table.update_item.call_args.kwargs["ExpressionAttributeNames"].values()
    for field in _CANDIDATE_FIELDS:
        assert field not in named
    assert "last_dev_image_tag" not in named
    assert "cicd_status" not in named

    table.reset_mock()
    svc._save_repo(_repo(), include_prod_candidate=True)
    named = table.update_item.call_args.kwargs["ExpressionAttributeNames"].values()
    for field in _CANDIDATE_FIELDS:
        assert field in named
    assert "last_dev_image_tag" not in named


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
    stale = svc.find_repository_by_agent_id(AGENT_ID)
    seed(svc._store, _to_item(_REPOSITORY_PK, _promoted_repo()))
    svc.find_repository_by_agent_id = MagicMock(return_value=stale)


def test_registering_a_candidate_preserves_the_promotion_audit(svc):
    """A merge to ``main`` landing during a promotion does not revert its audit stamp: a whole-record save
    from the stale snapshot would re-set all four ``last_promoted_*`` attributes back to ``None``,
    destroying the audit record on the product's highest-consequence verb."""
    _stage_the_promotion_during_the_scan(svc)

    svc.record_prod_candidate(AGENT_ID, image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR)

    stored = svc.get_repo("r-1")
    assert stored.prod_candidate_image_tag == CANDIDATE_TAG
    for field, value in _PROMOTION_AUDIT.items():
        assert getattr(stored, field) == value, field


def test_a_candidate_registration_does_not_defeat_the_in_flight_guard(svc):
    """The CONSEQUENCE of the mirror clobber, end-to-end: with ``last_promoted_at`` reverted to
    ``None``, ``_promotion_in_flight`` fails open (it cannot date the attempt, so it does not
    block) and a SECOND promote starts a SECOND CodeBuild run against the same Terraform state
    key. This is the test that proves the guard still holds after a candidate registration."""
    _stage_the_promotion_during_the_scan(svc)

    svc.record_prod_candidate(AGENT_ID, image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR)

    with pytest.raises(ProjectError) as ei:
        svc.promote_repo("proj-1", "r-1", promoted_by="oid-second")
    assert ei.value.kind == "promote_in_flight"
    svc._builds.start_runtime_build.assert_not_called()


def test_the_candidate_write_names_only_the_six_fields_it_owns(svc):
    """On the write itself: the candidate's ``SET`` names exactly the candidate fields
    plus ``updated_at``; anything else named is an attribute a stale read could revert, ``last_promoted_*``
    being the dangerous one."""
    table = MagicMock()
    table.query.return_value = {"Items": [{"pk": "repository", "sk": "r-1", **_promoted_repo().model_dump()}]}
    _on_table(svc, table)

    svc.record_prod_candidate(AGENT_ID, image_tag=CANDIDATE_TAG, sha=CANDIDATE_SHA, actor=CANDIDATE_ACTOR)

    named = set(table.update_item.call_args.kwargs["ExpressionAttributeNames"].values())
    assert named == set(_CANDIDATE_FIELDS) | {"updated_at"}
    for field in _PROMOTION_AUDIT:
        assert field not in named
