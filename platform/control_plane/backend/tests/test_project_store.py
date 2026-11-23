"""``ProjectStore``: the row codecs and the deployment-history reads, over the in-memory ``StateStore``."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from core.state_store import StateStore
from models.deployment import Deployment, DeploymentOutcome, deployment_seq_key
from models.project import Project
from services import project_store
from services.project_store import (
    _DEPLOYMENT_PK,
    _PROJECT_PK,
    _ROLLBACK_HISTORY_LIMIT,
    ProjectStore,
    _bound_per_stage,
    _parse_rows,
    _strip_keys,
    _to_item,
)

REPO_ID = "repo-1"
T0 = datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def rows() -> ProjectStore:
    return ProjectStore(StateStore(""))


def _project(**over) -> Project:
    fields = {
        "id": "proj-1",
        "name": "Claims",
        "connection_id": "conn-1",
        "tenant_id": "default",
        "created_by": "oid-1",
        "created_at": "2026-07-31T09:00:00+00:00",
        "updated_at": "2026-07-31T09:00:00+00:00",
    }
    return Project(**{**fields, **over})


def _deployment(n: int, *, stage: str = "prod", image_tag: str | None = None, outcome=None) -> Deployment:
    started_at = (T0 + timedelta(minutes=n)).isoformat()
    id = f"dep-{n:08x}"
    return Deployment(
        id=id,
        repo_id=REPO_ID,
        agent_id="a-1",
        stage=stage,
        seq_key=deployment_seq_key(REPO_ID, stage, started_at, id),
        image_tag=image_tag or f"tag-{n}",
        outcome=outcome or DeploymentOutcome.SUCCEEDED,
        started_at=started_at,
    )


def test_a_record_round_trips_through_to_item_and_parse_rows():
    record = _project()

    item = _to_item(_PROJECT_PK, record)

    assert item["pk"] == "project"
    assert item["sk"] == "proj-1"
    assert _parse_rows(Project, [item]) == [record]


def test_to_item_takes_an_explicit_sort_key_over_the_id():
    record = _deployment(1)

    item = _to_item(_DEPLOYMENT_PK, record, sk=record.seq_key)

    assert (item["pk"], item["sk"]) == ("deployment", record.seq_key)


def test_parse_rows_skips_a_malformed_row_and_logs_its_sort_key(caplog):
    good = _to_item(_PROJECT_PK, _project())
    bad = {"pk": "project", "sk": "proj-broken", "name": "no id"}

    with caplog.at_level(logging.WARNING, logger="services.project_store"):
        parsed = _parse_rows(Project, [bad, good])

    assert [p.id for p in parsed] == ["proj-1"]
    assert "proj-broken" in caplog.text


def test_strip_keys_drops_only_the_partition_and_sort_keys():
    assert _strip_keys({"pk": "project", "sk": "p", "id": "p", "name": "n"}) == {"id": "p", "name": "n"}


def test_bound_per_stage_keeps_the_first_rows_of_each_stage_in_order():
    stages = ["prod", "prod", "dev", "prod", "dev", "dev"]
    deployments = [_deployment(i, stage=s) for i, s in enumerate(stages)]

    kept = _bound_per_stage(deployments, 2)

    assert [d.id for d in kept] == [deployments[i].id for i in (0, 1, 2, 4)]


def test_one_stage_reads_newest_first_and_the_other_stage_never_answers(rows):
    for n in (1, 3, 2):
        rows.save_deployment(_deployment(n, stage="prod"))
    rows.save_deployment(_deployment(4, stage="dev"))

    assert [d.image_tag for d in rows.list_deployments(REPO_ID, stage="prod")] == ["tag-3", "tag-2", "tag-1"]
    assert [d.image_tag for d in rows.list_deployments(REPO_ID, stage="dev")] == ["tag-4"]
    assert [d.image_tag for d in rows.list_deployments(REPO_ID, stage="prod", limit=1)] == ["tag-3"]


def test_the_cross_stage_read_merges_newest_first_and_limits_after_the_merge(rows):
    for n, stage in ((1, "prod"), (2, "dev"), (3, "prod"), (4, "dev")):
        rows.save_deployment(_deployment(n, stage=stage))

    assert [d.image_tag for d in rows.list_deployments(REPO_ID)] == ["tag-4", "tag-3", "tag-2", "tag-1"]
    assert [d.image_tag for d in rows.list_deployments(REPO_ID, limit=2)] == ["tag-4", "tag-3"]


def test_the_cross_stage_read_stops_at_the_row_ceiling_and_logs_it(rows, monkeypatch, caplog):
    monkeypatch.setattr(project_store, "_MAX_DEPLOYMENT_ROWS", 3)
    for n in range(1, 6):
        rows.save_deployment(_deployment(n))

    with caplog.at_level(logging.WARNING, logger="services.project_store"):
        listed = rows.list_deployments(REPO_ID)

    assert [d.image_tag for d in listed] == ["tag-5", "tag-4", "tag-3"]
    assert "read ceiling" in caplog.text


def test_a_repeated_sort_key_is_refused(rows):
    rows.save_deployment(_deployment(1))

    with pytest.raises(RuntimeError):
        rows.save_deployment(_deployment(1))


def test_a_repo_without_deployment_rows_has_no_deployments(rows):
    assert not rows.has_deployments(REPO_ID)


def test_one_appended_deployment_is_a_deployment_history(rows):
    rows.save_deployment(_deployment(1))

    assert rows.has_deployments(REPO_ID)


def test_has_succeeded_needs_a_succeeded_row_for_the_exact_tag_in_that_stage(rows):
    rows.save_deployment(_deployment(1, stage="prod", image_tag="good"))
    rows.save_deployment(_deployment(2, stage="prod", image_tag="failed", outcome=DeploymentOutcome.FAILED))
    rows.save_deployment(_deployment(3, stage="dev", image_tag="dev-only"))

    assert rows.has_succeeded(REPO_ID, "prod", "good")
    assert not rows.has_succeeded(REPO_ID, "prod", "failed")
    assert not rows.has_succeeded(REPO_ID, "prod", "dev-only")
    assert not rows.has_succeeded(REPO_ID, "prod", "never-built")


@pytest.mark.parametrize("stage", ["", "  "])
def test_has_succeeded_refuses_a_blank_stage_rather_than_reading_across_stages(rows, stage):
    rows.save_deployment(_deployment(1, stage="dev", image_tag="good"))

    assert not rows.has_succeeded(REPO_ID, stage, "good")


def test_has_succeeded_reads_no_further_back_than_the_rollback_history_limit(rows):
    rows.save_deployment(_deployment(0, image_tag="old"))
    for n in range(1, _ROLLBACK_HISTORY_LIMIT + 1):
        rows.save_deployment(_deployment(n))

    assert not rows.has_succeeded(REPO_ID, "prod", "old")
    assert rows.has_succeeded(REPO_ID, "prod", "tag-1")


def test_the_newest_dev_row_for_the_tag_is_the_dev_outcome(rows):
    rows.save_deployment(_deployment(1, stage="dev", image_tag="cand", outcome=DeploymentOutcome.STARTED))
    rows.save_deployment(_deployment(2, stage="dev", image_tag="cand", outcome=DeploymentOutcome.FAILED))
    rows.save_deployment(_deployment(3, stage="prod", image_tag="cand", outcome=DeploymentOutcome.SUCCEEDED))

    assert rows.latest_dev_outcome_for_tag(REPO_ID, "cand") is DeploymentOutcome.FAILED


def test_no_dev_row_for_the_tag_is_an_unknown_dev_outcome(rows):
    rows.save_deployment(_deployment(1, stage="prod", image_tag="cand"))

    assert rows.latest_dev_outcome_for_tag(REPO_ID, "cand") is None
