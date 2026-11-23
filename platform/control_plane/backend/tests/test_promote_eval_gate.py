"""The promote eval gate.

A prod promotion needs a PASSING eval run for the candidate's digest, or an override with a
reason, which is recorded (who, why, which digest). The gate sits after the candidate is read and
before the in-flight check, so a refusal never reaches the deploy. ``Repository.eval_gate`` is
the same fact read back onto the response, so the Promote dialog cannot drift from the refusal.

The service is the real in-memory one (``test_promote_repo.py``'s idiom) with a real in-memory
``EvalStore`` injected, so a "passing run" is a seeded record, not a mock answer.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from seed import seed
from test_promote_repo import (
    CANDIDATE_TAG,
    FIXED,
    _governed_row,
    _project,
    _repo,
)

from models.eval import EvalConfig, EvalRun, GateResult, RunStatus
from services.eval_store import EvalStore
from services.project_errors import ProjectError
from services.project_service import ProjectService
from services.project_store import _PROJECT_PK, _REPOSITORY_PK, _to_item
from services.runtime_build_service import RuntimeBuildError

pytestmark = pytest.mark.usefixtures("reset_modules")

DIGEST = "sha256:" + "d" * 64
GATE_DETAIL = "Prod promotion needs a passing evaluation for this build (or an override with a reason)."


def _run(
    status: RunStatus = "passed",
    digest: str | None = DIGEST,
    run_id="run-1",
    gate_passed=None,
    build_tag=None,
):
    finished = status != "running"
    if gate_passed is None:
        gate_passed = status == "passed"
    return EvalRun(
        run_id=run_id,
        repo_id="r-1",
        agent_id="a-1",
        project_id="proj-1",
        digest=digest,
        build_tag=build_tag,
        trigger="manual",
        started_by="dev-oid",
        started_at=FIXED,
        finished_at=FIXED if finished else None,
        status=status,
        config=EvalConfig(),
        gate=GateResult(passed=gate_passed, reasons=[] if gate_passed else ["check failed"])
        if finished
        else None,
    )


@pytest.fixture
def store():
    return EvalStore()


@pytest.fixture
def svc(build_svc, store):
    s = ProjectService(
        table_name="",
        registry=MagicMock(),
        identity=MagicMock(),
        connection_service=MagicMock(),
        github_repo_service=MagicMock(),
        runtime_build_service=build_svc,
        ecr_repository="platform-fallback-ecr",
        eval_store=store,
        now=lambda: FIXED,
    )
    seed(s._store, _to_item(_PROJECT_PK, _project()))
    seed(s._store, _to_item(_REPOSITORY_PK, _repo(prod_candidate_digest=DIGEST)))
    return s


def test_no_run_refuses_with_eval_gate(svc, build_svc):
    with pytest.raises(ProjectError) as exc:
        svc.promote_repo("proj-1", "r-1", promoted_by="owner-oid")
    assert exc.value.kind == "eval_gate"
    assert exc.value.message == GATE_DETAIL
    build_svc.start_runtime_build.assert_not_called()


@pytest.mark.parametrize("status", ["failed", "error", "running"])
def test_a_run_that_did_not_pass_refuses(svc, store, build_svc, status):
    store.save_run(_run(status=status))
    with pytest.raises(ProjectError) as exc:
        svc.promote_repo("proj-1", "r-1", promoted_by="owner-oid")
    assert exc.value.kind == "eval_gate"
    build_svc.start_runtime_build.assert_not_called()


def test_a_passing_run_for_another_digest_does_not_count(svc, store, build_svc):
    store.save_run(_run(digest="sha256:" + "e" * 64))
    with pytest.raises(ProjectError) as exc:
        svc.promote_repo("proj-1", "r-1", promoted_by="owner-oid")
    assert exc.value.kind == "eval_gate"


def test_a_passing_run_proceeds_and_records_no_override(svc, store, build_svc):
    store.save_run(_run(status="passed"))
    svc.promote_repo("proj-1", "r-1", promoted_by="owner-oid")
    assert build_svc.start_runtime_build.call_args.kwargs["image_digest"] == DIGEST
    assert store.list_overrides("r-1") == []


def test_the_gate_is_checked_before_the_in_flight_guard(svc, build_svc):
    """A promotion in flight with no passing run answers eval_gate, not promote_in_flight."""
    seed(
        svc._store,
        _to_item(
            _REPOSITORY_PK,
            _repo(prod_candidate_digest=DIGEST, cicd_status="promoting", last_promoted_at=FIXED.isoformat()),
        ),
    )
    with pytest.raises(ProjectError) as exc:
        svc.promote_repo("proj-1", "r-1", promoted_by="owner-oid")
    assert exc.value.kind == "eval_gate"


def test_an_override_proceeds_and_is_recorded(svc, store, build_svc):
    """The row keeps the candidate's tag; no display name was given, so none is invented."""
    store.save_run(_run(status="failed"))
    svc.promote_repo("proj-1", "r-1", promoted_by="owner-oid", override_reason="  hotfix for INC-42  ")
    build_svc.start_runtime_build.assert_called_once()
    [o] = store.list_overrides("r-1")
    assert (o.repo_id, o.digest, o.by, o.reason, o.at) == (
        "r-1",
        DIGEST,
        "owner-oid",
        "hotfix for INC-42",
        FIXED,
    )
    assert (o.build_tag, o.by_name) == (CANDIDATE_TAG, None)


def test_an_override_records_the_promoters_display_name(svc, store, build_svc):
    """``by`` is an oid only its owner can name; the row keeps the display name too."""
    svc.promote_repo(
        "proj-1",
        "r-1",
        promoted_by="owner-oid",
        override_reason="INC-42",
        promoted_by_name="  Lars Svensson  ",
    )
    [o] = store.list_overrides("r-1")
    assert (o.by, o.by_name) == ("owner-oid", "Lars Svensson")


@pytest.mark.parametrize("name", ["", "   ", None, 42])
def test_a_blank_or_odd_display_name_is_not_recorded(svc, store, build_svc, name):
    svc.promote_repo(
        "proj-1", "r-1", promoted_by="owner-oid", override_reason="INC-42", promoted_by_name=name
    )
    [o] = store.list_overrides("r-1")
    assert o.by_name is None


@pytest.mark.parametrize("reason", ["", "   ", None])
def test_an_empty_override_still_refuses(svc, store, build_svc, reason):
    with pytest.raises(ProjectError) as exc:
        svc.promote_repo("proj-1", "r-1", promoted_by="owner-oid", override_reason=reason)
    assert exc.value.kind == "eval_gate"
    assert store.list_overrides("r-1") == []
    build_svc.start_runtime_build.assert_not_called()


def test_an_override_over_500_chars_still_refuses(svc, store, build_svc):
    with pytest.raises(ProjectError) as exc:
        svc.promote_repo("proj-1", "r-1", promoted_by="owner-oid", override_reason="x" * 501)
    assert exc.value.kind == "eval_gate"
    assert store.list_overrides("r-1") == []


def test_a_failed_override_write_logs_who_and_which_build_but_not_the_reason(
    svc, store, build_svc, monkeypatch, caplog
):
    """The log line is the only record of that bypass: repo, digest, tag, by, by_name; no reason text."""

    def boom(_override):
        raise RuntimeError("ddb down")

    monkeypatch.setattr(store, "record_override", boom)
    with caplog.at_level("ERROR", logger="services.project_delivery"):
        svc.promote_repo(
            "proj-1",
            "r-1",
            promoted_by="owner-oid",
            override_reason="secret INC-42 detail",
            promoted_by_name="Lars Svensson",
        )
    build_svc.start_runtime_build.assert_called_once()
    [rec] = [r for r in caplog.records if "eval-gate override" in r.getMessage()]
    msg = rec.getMessage()
    for part in ("r-1", DIGEST, CANDIDATE_TAG, "owner-oid", "Lars Svensson"):
        assert part in msg
    assert "INC-42" not in msg


def test_an_override_on_a_passing_run_is_not_recorded(svc, store, build_svc):
    """The override is an audit of a BYPASS — a passing run needed none."""
    store.save_run(_run(status="passed"))
    svc.promote_repo("proj-1", "r-1", promoted_by="owner-oid", override_reason="belt and braces")
    assert store.list_overrides("r-1") == []


def test_a_tag_identified_candidate_passes_with_a_run_for_its_tag(svc, store, build_svc):
    """Harness and config builds have no digest; the build is then its tag."""
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo(prod_candidate_digest=None)))
    store.save_run(_run(status="passed", digest=None, build_tag=CANDIDATE_TAG))
    svc.promote_repo("proj-1", "r-1", promoted_by="owner-oid")
    build_svc.start_runtime_build.assert_called_once()
    assert store.list_overrides("r-1") == []


def test_a_tag_identified_candidate_refuses_without_a_run_for_its_tag(svc, store, build_svc):
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo(prod_candidate_digest=None)))
    store.save_run(_run(status="passed", digest=None, build_tag="some-other-tag"))
    store.save_run(_run(status="passed", digest=None, build_tag=None, run_id="run-2"))
    with pytest.raises(ProjectError) as exc:
        svc.promote_repo("proj-1", "r-1", promoted_by="owner-oid")
    assert exc.value.kind == "eval_gate"
    build_svc.start_runtime_build.assert_not_called()


def test_a_digest_candidate_ignores_a_run_matching_only_its_tag(svc, store, build_svc):
    """Digest wins: a tag-matching run of different bytes does not attest this build."""
    store.save_run(_run(status="passed", digest="sha256:" + "e" * 64, build_tag=CANDIDATE_TAG))
    with pytest.raises(ProjectError) as exc:
        svc.promote_repo("proj-1", "r-1", promoted_by="owner-oid")
    assert exc.value.kind == "eval_gate"


def test_a_tag_identified_candidate_promotes_with_an_override(svc, store, build_svc):
    """With no digest, the tag is the only thing naming the bypassed build."""
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo(prod_candidate_digest=None)))
    svc.promote_repo("proj-1", "r-1", promoted_by="owner-oid", override_reason="legacy build")
    build_svc.start_runtime_build.assert_called_once()
    [o] = store.list_overrides("r-1")
    assert (o.digest, o.reason) == (None, "legacy build")
    assert o.build_tag == CANDIDATE_TAG


@pytest.mark.parametrize(
    "run_status,expected",
    [(None, "none"), ("passed", "passed"), ("failed", "failed"), ("running", "running")],
)
def test_eval_gate_for_a_tag_identified_candidate(svc, store, run_status, expected):
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo(prod_candidate_digest=None)))
    if run_status:
        store.save_run(_run(status=run_status, digest=None, build_tag=CANDIDATE_TAG))
    assert svc.get_repo("r-1").eval_gate == expected


def test_an_override_refused_by_the_in_flight_guard_leaves_no_row(svc, store, build_svc):
    seed(
        svc._store,
        _to_item(
            _REPOSITORY_PK,
            _repo(prod_candidate_digest=DIGEST, cicd_status="promoting", last_promoted_at=FIXED.isoformat()),
        ),
    )
    with pytest.raises(ProjectError) as exc:
        svc.promote_repo("proj-1", "r-1", promoted_by="owner-oid", override_reason="INC-42")
    assert exc.value.kind == "promote_in_flight"
    assert store.list_overrides("r-1") == []


def test_an_override_whose_build_fails_to_start_leaves_no_row(svc, store, build_svc):
    build_svc.start_runtime_build.side_effect = RuntimeBuildError("nope", kind="start_failed")
    with pytest.raises(ProjectError) as exc:
        svc.promote_repo("proj-1", "r-1", promoted_by="owner-oid", override_reason="INC-42")
    assert exc.value.kind == "promote_failed"
    assert store.list_overrides("r-1") == []


def test_an_override_needs_an_identifiable_caller(svc, store, build_svc):
    with pytest.raises(ProjectError) as exc:
        svc.promote_repo("proj-1", "r-1", promoted_by="", override_reason="INC-42")
    assert exc.value.kind == "eval_gate"
    assert store.list_overrides("r-1") == []
    build_svc.start_runtime_build.assert_not_called()


@pytest.mark.parametrize("status", ["failed", "error", "running"])
def test_a_non_passed_status_never_satisfies_the_gate_even_with_a_passing_gate(svc, store, build_svc, status):
    run = _run(status="passed").model_copy(update={"status": status})
    store.save_run(run)
    with pytest.raises(ProjectError) as exc:
        svc.promote_repo("proj-1", "r-1", promoted_by="owner-oid")
    assert exc.value.kind == "eval_gate"
    build_svc.start_runtime_build.assert_not_called()
    assert svc.get_repo("r-1").eval_gate == ("running" if status == "running" else "failed")


def test_a_store_fault_at_the_gate_fails_closed(svc, store, build_svc):
    store.latest_run_for_build = MagicMock(side_effect=RuntimeError("ddb down"))
    with pytest.raises(RuntimeError):
        svc.promote_repo("proj-1", "r-1", promoted_by="owner-oid", override_reason="INC-42")
    build_svc.start_runtime_build.assert_not_called()
    assert store.list_overrides("r-1") == []


def test_a_store_fault_on_read_gives_eval_gate_none(svc, store):
    store.latest_run_for_build = MagicMock(side_effect=RuntimeError("ddb down"))
    assert svc.get_repo("r-1").eval_gate is None


@pytest.mark.parametrize(
    "run_status,expected",
    [
        (None, "none"),
        ("passed", "passed"),
        ("failed", "failed"),
        ("error", "failed"),
        ("running", "running"),
    ],
)
def test_eval_gate_on_the_read_model(svc, store, run_status, expected):
    if run_status:
        store.save_run(_run(status=run_status))
    assert svc.get_repo("r-1").eval_gate == expected
    assert svc.list_repositories()[0].eval_gate == expected
    assert svc.get_project("proj-1").repositories[0].eval_gate == expected


def test_eval_gate_is_none_for_a_repo_without_an_agent(svc, store):
    mcp_repo = _repo(prod_candidate_digest=DIGEST).model_copy(
        update={"agent_id": None, "artifact_kind": "mcp_server", "mcp_server_id": "mcp-1"}
    )
    seed(svc._store, _to_item(_REPOSITORY_PK, mcp_repo))
    store.save_run(_run(status="passed"))
    assert svc.get_repo("r-1").eval_gate is None


def test_eval_gate_is_none_state_without_a_candidate(svc, store):
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo(prod_candidate_image_tag=None)))
    assert svc.get_repo("r-1").eval_gate == "none"


def test_eval_gate_is_not_persisted(svc, store):
    store.save_run(_run(status="passed"))
    repo = svc.get_repo("r-1")
    svc._save_repo(repo)
    assert svc._get_repo("r-1").eval_gate is None


@pytest.fixture
def gated_route_svc(project_svc, store):
    """The route's service with the in-memory store injected and a digest-bearing candidate."""
    project_svc._evals = store
    repo = _repo(prod_candidate_digest=DIGEST)
    project_svc.get_repo.return_value = repo
    return project_svc


def _owner(client_factory, role_svc):
    role_svc.list_for_project.return_value = [_governed_row()]
    return client_factory(project_role="owner", roles={"proj-1": "owner"})


def test_route_maps_eval_gate_to_409(client_factory, role_svc, gated_route_svc, build_svc):
    r = _owner(client_factory, role_svc).post("/api/v1/projects/proj-1/repos/r-1/promote")
    assert r.status_code == 409
    assert r.json()["detail"] == GATE_DETAIL
    build_svc.start_runtime_build.assert_not_called()


def test_route_passes_the_override_reason(client_factory, role_svc, gated_route_svc, store, build_svc):
    """With no ``name`` claim in the token the display name is the email; the identity stays the oid."""
    r = _owner(client_factory, role_svc).post(
        "/api/v1/projects/proj-1/repos/r-1/promote", json={"override_reason": "INC-42"}
    )
    assert r.status_code == 202
    [o] = store.list_overrides("r-1")
    assert (o.reason, o.digest) == ("INC-42", DIGEST)
    assert (o.by, o.by_name, o.build_tag) == ("operator-oid", "operator@x.com", CANDIDATE_TAG)


def test_route_with_a_passing_run_needs_no_body(client_factory, role_svc, gated_route_svc, store, build_svc):
    store.save_run(_run(status="passed"))
    r = _owner(client_factory, role_svc).post("/api/v1/projects/proj-1/repos/r-1/promote")
    assert r.status_code == 202
    assert store.list_overrides("r-1") == []


def test_the_read_model_carries_the_candidate_runs_regressions(svc, store):
    """Regressions are computed at read time and never persisted."""
    store.save_run(_regressed_run(digest=None, build_tag="x-b1a0bca"))
    seed(
        svc._store,
        _to_item(
            _REPOSITORY_PK,
            _repo(prod_candidate_digest=DIGEST).model_copy(update={"last_promoted_image_tag": "x-b1a0bca"}),
        ),
    )
    repo = svc.get_repo("r-1")
    assert repo.eval_regressions == ["Judges 0.62, down from 0.86 on prod build b1a0bca"]
    svc._save_repo(repo)
    assert svc._get_repo("r-1").eval_regressions is None


def test_no_candidate_run_means_no_regressions(svc, store):
    assert svc.get_repo("r-1").eval_regressions is None


def _regressed_run(**baseline):
    from models.eval import RunBaseline

    run = _run(status="passed")
    run.baseline = RunBaseline(
        run_id="prod-run", build="b1a0bca", checks_passed=12, checks_total=12, judge_mean=0.86, **baseline
    )
    run.regressions = ["Judges 0.62, down from 0.86 on prod build b1a0bca"]
    return run


def test_regressions_against_a_build_no_longer_in_prod_are_dropped(svc, store):
    store.save_run(_regressed_run(digest="sha256:" + "1" * 64, build_tag="x-b1a0bca"))
    seed(
        svc._store,
        _to_item(
            _REPOSITORY_PK,
            _repo(prod_candidate_digest=DIGEST).model_copy(
                update={"last_promoted_digest": "sha256:" + "2" * 64}
            ),
        ),
    )
    assert svc.get_repo("r-1").eval_regressions is None


def test_regressions_against_the_build_still_in_prod_are_kept(svc, store):
    prod = "sha256:" + "1" * 64
    store.save_run(_regressed_run(digest=prod, build_tag="x-b1a0bca"))
    seed(
        svc._store,
        _to_item(
            _REPOSITORY_PK,
            _repo(prod_candidate_digest=DIGEST).model_copy(update={"last_promoted_digest": prod}),
        ),
    )
    assert svc.get_repo("r-1").eval_regressions == ["Judges 0.62, down from 0.86 on prod build b1a0bca"]


def test_regressions_match_the_prod_tag_when_prod_has_no_digest(svc, store):
    store.save_run(_regressed_run(digest=None, build_tag="x-b1a0bca"))
    seed(
        svc._store,
        _to_item(
            _REPOSITORY_PK,
            _repo(prod_candidate_digest=DIGEST).model_copy(update={"last_promoted_image_tag": "x-b1a0bca"}),
        ),
    )
    assert svc.get_repo("r-1").eval_regressions == ["Judges 0.62, down from 0.86 on prod build b1a0bca"]


def test_the_read_model_carries_the_candidate_runs_evidence(svc, store):
    """The evidence the Promote dialog shows is computed at read time and never persisted."""
    from models.eval import RunSummary

    run = _run(status="failed")
    run.summary = RunSummary(
        items=4, checks_passed=10, checks_failed=2, judge_means={"Builtin.Correctness": 0.74}
    )
    store.save_run(run)
    ev = svc.get_repo("r-1").eval_evidence
    assert ev is not None
    assert (ev.run_id, ev.status, ev.checks_passed, ev.checks_total) == ("run-1", "failed", 10, 12)
    assert ev.judge_means == {"Builtin.Correctness": 0.74}
    assert ev.judge_min_mean == EvalConfig().gate.judge_min_mean
    svc._save_repo(svc.get_repo("r-1"))
    assert svc._get_repo("r-1").eval_evidence is None


def test_a_run_without_a_summary_still_names_the_run(svc, store):
    store.save_run(_run(status="running"))
    ev = svc.get_repo("r-1").eval_evidence
    assert (ev.run_id, ev.status, ev.checks_total) == ("run-1", "running", None)


def test_no_candidate_run_means_no_evidence(svc, store):
    assert svc.get_repo("r-1").eval_evidence is None
