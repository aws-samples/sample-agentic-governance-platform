"""The append-only deployment store — ``ProjectService.append_deployment`` / ``list_deployments``
(E28/T3, contract C1).

The problem this store exists to fix: promotion state used to live ONLY as singular scalars on
``Repository`` (``last_promoted_*`` / ``prod_candidate_*``), whose own docstring says they are
"overwritten wholesale by a newer merge, and cleared on a successful promote". One promote
therefore ERASED the evidence of the previous one, which makes build history and rollback
structurally impossible — you cannot roll back to an artifact the record no longer remembers.

Four invariants this file exists to pin:

1. **An append never overwrites.** Every call adds a row; N appends ⇒ N rows. This is the whole
   point of the partition, and the failure it replaces was silent (a lost row looks exactly like
   "nothing ever deployed").
2. **Reads are newest-first.** The sk carries ``started_at``, so time-sort needs no counter and
   no append-time coordination (no read-modify-write, so no append race).
3. **Two appends in the SAME millisecond both survive.** The 4-char id suffix on the sk is what
   makes that true — without it the two rows share a key and the second silently replaces the
   first. Pinned with a FIXED clock, which is the same-millisecond case by construction.
4. **A malformed row is skipped, not fatal.** Matches the existing ``_parse_rows`` /
   ``_scan_partition`` tolerance (``project_service.py``): a single pre-rename or partially
   written item must never 500 a whole history list.

Harness is the seam ``test_prod_candidate_service.py`` established: the REAL ``ProjectService``
with ``table_name=""`` (the local dict fallback — no boto3, NO moto), an injected clock so
timestamps are asserted exactly, and every collaborator a ``MagicMock`` so nothing can reach
GitHub, CodeBuild, Secrets Manager or Entra. The DDB branch is pinned separately by setting
``svc._table`` to a ``MagicMock`` and asserting on the real ``query``/``put_item`` kwargs — the
local dict only MIRRORS those semantics, so key shape and sort direction must be pinned where
the write and read actually happen.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from models.deployment import Deployment, DeploymentOutcome
from services.project_service import _DEPLOYMENT_PK, ProjectService

FIXED = datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc)
FIXED_TS = FIXED.isoformat()

REPO_ID = "repo-1"
AGENT_ID = "a-1"


@pytest.fixture
def svc():
    """The REAL in-memory ``ProjectService`` — ``table_name=""`` ⇒ the local dict fallback."""
    return ProjectService(
        table_name="",
        registry=MagicMock(),
        identity=MagicMock(),
        connection_service=MagicMock(),
        github_repo_service=MagicMock(),
        runtime_build_service=MagicMock(),
        now=lambda: FIXED,
    )


def _append(svc, *, stage="prod", image_tag="a-1-tree000", **over):
    return svc.append_deployment(
        repo_id=REPO_ID, agent_id=AGENT_ID, stage=stage, image_tag=image_tag, **over
    )


def _ddb(svc, table):
    """Flip the service onto its DDB branch (mirrors test_prod_candidate_service.py)."""
    svc._table = table
    svc.table_name = "projects"  # with _table set, this flips _has_ddb
    return table


# --------------------------------------------------------------------------- #
# 1) An append NEVER overwrites
# --------------------------------------------------------------------------- #


def test_appends_accumulate_and_never_overwrite(svc):
    """The invariant the singular ``last_promoted_*`` scalars broke: three promotes of the SAME
    repo+stage leave three rows, each remembering its own artifact. Without this, rollback has
    no candidate to roll back TO."""
    for tag in ("a-1-tree001", "a-1-tree002", "a-1-tree003"):
        _append(svc, image_tag=tag)

    rows = svc.list_deployments(REPO_ID)
    assert len(rows) == 3
    assert {r.image_tag for r in rows} == {"a-1-tree001", "a-1-tree002", "a-1-tree003"}
    assert len({r.id for r in rows}) == 3  # distinct ids
    assert len({r.seq_key for r in rows}) == 3  # …and distinct DDB sort keys


def test_append_returns_the_pinned_record_shape(svc):
    """C1, verbatim: a ``dep-<8 hex>`` id, an ISO-8601 ``started_at``, the ``started`` default
    outcome, and the sk mirrored onto the record for round-tripping."""
    record = _append(
        svc,
        source_sha="1" * 40,
        build_id="build-1",
        actor="merger-login",
        actor_kind="github",
    )

    assert record.id.startswith("dep-") and len(record.id) == len("dep-") + 8
    assert int(record.id[4:], 16) >= 0  # the suffix is hex
    assert record.repo_id == REPO_ID
    assert record.agent_id == AGENT_ID
    assert record.stage == "prod"
    assert record.started_at == FIXED_TS
    assert record.completed_at is None
    assert record.outcome is DeploymentOutcome.STARTED
    assert record.actor == "merger-login"
    assert record.actor_kind == "github"
    assert record.error is None
    assert record.seq_key == f"{REPO_ID}#prod#{FIXED_TS}#{record.id[-4:]}"


def test_stage_is_free_form_and_never_validated_against_dev_or_prod(svc):
    """D8: ``stage`` is whatever the API returns. A tenant with a ``staging`` stage must persist,
    not raise — hardcoding a dev/prod literal here is the drift this contract forbids."""
    record = _append(svc, stage="staging")

    assert record.stage == "staging"
    assert svc.list_deployments(REPO_ID, stage="staging")[0].id == record.id
    assert svc.list_deployments(REPO_ID, stage="prod") == []


def test_the_ddb_append_writes_the_pinned_partition_and_sort_key(svc):
    """The local dict only mirrors the semantics, so the key shape is pinned on the real write
    path: ``pk="deployment"`` and ``sk = "{repo_id}#{stage}#{started_at}#{id[-4:]}"``."""
    table = _ddb(svc, MagicMock())

    record = _append(svc)

    item = table.put_item.call_args.kwargs["Item"]
    assert item["pk"] == _DEPLOYMENT_PK == "deployment"
    assert item["sk"] == f"{REPO_ID}#prod#{FIXED_TS}#{record.id[-4:]}"
    assert item["sk"] == record.seq_key  # the mirror is what makes a row round-trip
    assert item["image_tag"] == "a-1-tree000"
    table.update_item.assert_not_called()  # append-only: nothing ever UPDATES a row


def test_append_degrades_when_no_table_is_configured(svc):
    """The service is constructed with ``table_name=""`` in tests and in any deployment without a
    table; the neighbouring methods degrade to the local dict rather than crash, and so must
    this one."""
    assert svc._has_ddb is False
    record = _append(svc)
    assert svc.list_deployments(REPO_ID) == [record]


# --------------------------------------------------------------------------- #
# 2) Ordering is newest-first
# --------------------------------------------------------------------------- #


def test_list_is_newest_first(svc):
    """History reads newest-first because that is the only order a "what is deployed now / what
    can I roll back to" surface can use without re-sorting client-side."""
    clock = [
        datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 31, 11, 0, tzinfo=timezone.utc),
    ]
    for moment, tag in zip(clock, ("oldest", "middle", "newest")):
        svc._now = lambda moment=moment: moment
        _append(svc, image_tag=tag)

    rows = svc.list_deployments(REPO_ID)
    assert [r.image_tag for r in rows] == ["newest", "middle", "oldest"]
    assert [r.started_at for r in rows] == [m.isoformat() for m in reversed(clock)]


def _at(svc, moment: datetime):
    """Point the injected clock at one instant (so appends carry asserted timestamps)."""
    svc._now = lambda moment=moment: moment


def test_list_is_newest_first_ACROSS_STAGES(svc):
    """The ordering invariant where it can actually BREAK — across stages (FIX round 1).

    ``test_list_is_newest_first`` appends only to ``prod``, so the sk's stage component is
    constant there and the assertion passes for the wrong reason. With ``stage=None`` the query
    prefix widens to ``f"{repo_id}#"`` and the sk is
    ``{repo_id}#{stage}#{started_at}#{suffix}`` — so DynamoDB (and any naive sk sort) orders by
    STAGE NAME FIRST and only then by time. Appending prod@08h, dev@09h, prod@10h, dev@11h
    returned ``[p-10h, p-08h, d-11h, d-09h]``: two stage-grouped runs, not a history.

    The sk format is a pinned contract (C1) that T4/T12 code against, so this is fixed on the
    READ side — the cross-stage path merges by ``started_at``."""
    moments = {
        "p-08h": ("prod", datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)),
        "d-09h": ("dev", datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc)),
        "p-10h": ("prod", datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)),
        "d-11h": ("dev", datetime(2026, 7, 31, 11, 0, tzinfo=timezone.utc)),
    }
    for tag, (stage, moment) in moments.items():
        _at(svc, moment)
        _append(svc, stage=stage, image_tag=tag)

    rows = svc.list_deployments(REPO_ID)

    # Interleaved by TIME, not grouped by stage.
    assert [r.image_tag for r in rows] == ["d-11h", "p-10h", "d-09h", "p-08h"]
    assert [r.stage for r in rows] == ["dev", "prod", "dev", "prod"]
    # Each single-stage view stays newest-first too (the pinned begins_with path).
    assert [r.image_tag for r in svc.list_deployments(REPO_ID, stage="prod")] == [
        "p-10h",
        "p-08h",
    ]
    assert [r.image_tag for r in svc.list_deployments(REPO_ID, stage="dev")] == [
        "d-11h",
        "d-09h",
    ]


def test_a_cross_stage_limit_keeps_the_NEWEST_rows(svc):
    """The consequence of the ordering bug, and the sharper failure: with stage-major ordering,
    ``limit=2`` returned ``[p-10h, p-08h]`` — it DROPPED the newest deployment entirely.

    A "what is deployed now / what can I roll back to" surface that pages this way shows a stale
    artifact as current, which on the product's highest-consequence verb is worse than an error.
    So ``limit`` must be applied AFTER the cross-stage merge, never as a DDB ``Limit`` before it."""
    for stage, moment, tag in (
        ("prod", datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc), "p-08h"),
        ("dev", datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc), "d-09h"),
        ("prod", datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc), "p-10h"),
        ("dev", datetime(2026, 7, 31, 11, 0, tzinfo=timezone.utc), "d-11h"),
    ):
        _at(svc, moment)
        _append(svc, stage=stage, image_tag=tag)

    assert [r.image_tag for r in svc.list_deployments(REPO_ID, limit=2)] == ["d-11h", "p-10h"]
    assert [r.image_tag for r in svc.list_deployments(REPO_ID, limit=1)] == ["d-11h"]


def test_the_cross_stage_ddb_query_does_not_truncate_before_the_merge(svc):
    """Pinned on the real read path: the cross-stage query must NOT pass a DDB ``Limit``.

    A ``Limit`` is applied by DynamoDB in sk order — i.e. in STAGE order here — so it would
    truncate to the alphabetically-first stage before the merge could see the newest row. The
    single-stage path is the opposite: its sk order IS time order, so ``Limit`` is correct and
    cheap there. The two paths must therefore differ, and that difference is the fix."""
    table = _ddb(svc, MagicMock())
    table.query.return_value = {"Items": []}

    svc.list_deployments(REPO_ID, limit=2)  # cross-stage
    assert "Limit" not in table.query.call_args.kwargs

    table.reset_mock()
    table.query.return_value = {"Items": []}
    svc.list_deployments(REPO_ID, stage="prod", limit=2)  # single-stage
    assert table.query.call_args.kwargs["Limit"] == 2


def test_the_cross_stage_read_follows_pagination(svc):
    """Because the cross-stage read cannot let DDB truncate, it must page the prefix itself —
    otherwise "newest" is only the newest of whatever DynamoDB's first 1 MB page happened to
    contain, which is a silent wrong answer rather than a missing one. Mirrors the
    ``LastEvaluatedKey`` loop ``_scan_partition`` already uses."""
    table = _ddb(svc, MagicMock())
    page_1 = _row(f"{REPO_ID}#prod#2026-07-31T08:00:00+00:00#0001", image_tag="p-08h",
                  started_at="2026-07-31T08:00:00+00:00")
    page_2 = _row(f"{REPO_ID}#dev#2026-07-31T11:00:00+00:00#0002", image_tag="d-11h",
                  stage="dev", started_at="2026-07-31T11:00:00+00:00")
    table.query.side_effect = [
        {"Items": [page_1], "LastEvaluatedKey": {"pk": _DEPLOYMENT_PK, "sk": page_1["sk"]}},
        {"Items": [page_2]},
    ]

    rows = svc.list_deployments(REPO_ID)

    assert table.query.call_count == 2
    assert table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {
        "pk": _DEPLOYMENT_PK,
        "sk": page_1["sk"],
    }
    # The newest row lived on the SECOND page — it must still come first.
    assert [r.image_tag for r in rows] == ["d-11h", "p-08h"]


def test_list_filters_by_repo_and_stage_and_honours_limit(svc):
    """``stage=None`` is every stage for the repo; a named stage is that stage only. Another
    repo's rows are never visible — they live in the same partition, separated by the sk prefix."""
    _append(svc, stage="dev", image_tag="d-1")
    _append(svc, stage="prod", image_tag="p-1")
    svc.append_deployment(
        repo_id="repo-2", agent_id="a-2", stage="prod", image_tag="other-repo"
    )

    assert len(svc.list_deployments(REPO_ID)) == 2
    assert [r.image_tag for r in svc.list_deployments(REPO_ID, stage="prod")] == ["p-1"]
    assert [r.image_tag for r in svc.list_deployments(REPO_ID, stage="dev")] == ["d-1"]
    assert [r.image_tag for r in svc.list_deployments("repo-2")] == ["other-repo"]
    assert len(svc.list_deployments(REPO_ID, limit=1)) == 1


def test_the_ddb_query_uses_begins_with_and_scans_backwards(svc):
    """C1's read contract, on the real read path: ``begins_with(sk, "{repo_id}#{stage}#")`` plus
    ``ScanIndexForward=False``. The sort direction is DynamoDB's job — a local re-sort would
    silently only order the FIRST page, so the query itself must be pinned."""
    from boto3.dynamodb.conditions import Key

    table = _ddb(svc, MagicMock())
    table.query.return_value = {"Items": []}

    svc.list_deployments(REPO_ID, stage="prod", limit=7)

    kwargs = table.query.call_args.kwargs
    assert kwargs["KeyConditionExpression"] == (
        Key("pk").eq(_DEPLOYMENT_PK) & Key("sk").begins_with(f"{REPO_ID}#prod#")
    )
    assert kwargs["ScanIndexForward"] is False
    assert kwargs["Limit"] == 7

    # stage=None widens the prefix to the whole repo, never to the whole partition.
    table.reset_mock()
    table.query.return_value = {"Items": []}
    svc.list_deployments(REPO_ID)
    assert table.query.call_args.kwargs["KeyConditionExpression"] == (
        Key("pk").eq(_DEPLOYMENT_PK) & Key("sk").begins_with(f"{REPO_ID}#")
    )


def test_list_degrades_to_empty_when_the_store_is_unreachable(svc):
    """A read that cannot reach DDB returns ``[]`` and logs — the same "degrade, don't crash"
    behaviour ``_load_all_repos`` has. An empty history is a UI state; a 500 is not."""
    from botocore.exceptions import ClientError

    table = _ddb(svc, MagicMock())
    table.query.side_effect = ClientError({"Error": {"Code": "Throttling"}}, "Query")

    assert svc.list_deployments(REPO_ID) == []


# --------------------------------------------------------------------------- #
# 3) Same-millisecond appends BOTH survive
# --------------------------------------------------------------------------- #


def test_two_same_millisecond_appends_both_survive(svc):
    """The clock is FIXED, so both rows carry an identical ``started_at`` — the exact collision
    the 4-char id suffix on the sk exists to break. Without the suffix the two rows share a key
    and DynamoDB's ``put_item`` silently replaces the first: a promote that vanished."""
    first = _append(svc, image_tag="same-ms-1")
    second = _append(svc, image_tag="same-ms-2")

    assert first.started_at == second.started_at == FIXED_TS
    assert first.id != second.id
    assert first.seq_key != second.seq_key

    rows = svc.list_deployments(REPO_ID)
    assert len(rows) == 2
    assert {r.image_tag for r in rows} == {"same-ms-1", "same-ms-2"}


def test_same_millisecond_appends_write_distinct_ddb_sort_keys(svc):
    """Pinned on the real write path too: the local dict keys rows however it likes, but in DDB
    an equal sk IS an overwrite."""
    table = _ddb(svc, MagicMock())

    _append(svc, image_tag="same-ms-1")
    _append(svc, image_tag="same-ms-2")

    sks = [c.kwargs["Item"]["sk"] for c in table.put_item.call_args_list]
    assert len(sks) == 2
    assert sks[0] != sks[1]


# --------------------------------------------------------------------------- #
# 4) A malformed row is SKIPPED, not fatal
# --------------------------------------------------------------------------- #


def _row(sk: str, **over) -> dict:
    record = Deployment(
        id="dep-abcd1234",
        repo_id=REPO_ID,
        agent_id=AGENT_ID,
        stage="prod",
        seq_key=sk,
        image_tag="a-1-tree000",
        started_at=FIXED_TS,
    )
    return {"pk": _DEPLOYMENT_PK, "sk": sk, **record.model_dump(mode="json"), **over}


def test_a_malformed_row_is_skipped_not_fatal(svc):
    """Matches the existing ``_parse_rows`` tolerance: a partially written or pre-rename row must
    not break the WHOLE list. A history surface that 500s because one old row lacks a field is
    strictly worse than one that shows the rows it can read."""
    table = _ddb(svc, MagicMock())
    good = _row(f"{REPO_ID}#prod#{FIXED_TS}#0002", image_tag="readable")
    malformed = {"pk": _DEPLOYMENT_PK, "sk": f"{REPO_ID}#prod#{FIXED_TS}#0001"}  # no fields
    table.query.return_value = {"Items": [good, malformed]}

    rows = svc.list_deployments(REPO_ID, stage="prod")

    assert [r.image_tag for r in rows] == ["readable"]


def test_a_malformed_outcome_value_is_skipped_not_fatal(svc):
    """The other malformed shape: a row whose enum value is not in ``DeploymentOutcome`` (an
    older writer, or a hand-edited row). Skipped like any other validation failure."""
    table = _ddb(svc, MagicMock())
    table.query.return_value = {
        "Items": [
            _row(f"{REPO_ID}#prod#{FIXED_TS}#0002", image_tag="readable"),
            _row(f"{REPO_ID}#prod#{FIXED_TS}#0001", outcome="exploded"),
        ]
    }

    assert [r.image_tag for r in svc.list_deployments(REPO_ID, stage="prod")] == ["readable"]


# --------------------------------------------------------------------------- #
# 5) The cross-stage read is BOUNDED (E28/T2, parked finding P3)
#
# The cross-stage path cannot let DynamoDB truncate (see
# `test_the_cross_stage_ddb_query_does_not_truncate_before_the_merge`), so it pages the whole
# `{repo_id}#` prefix itself. That was accepted while nothing called it — but T12 puts it on the
# repo detail page, where an unbounded read means one repo with a long history can page its
# ENTIRE (never-expiring, append-only) delivery history into memory on every page view.
#
# The bound must not cost correctness, so it is NOT a DDB `Limit`. It rests on an exact
# argument instead: the global newest-`limit` set can contain AT MOST `limit` rows from any one
# stage, and rows arrive newest-first WITHIN a stage — so after `limit` rows of a stage have
# been seen, every later row of that stage is provably outranked and can be dropped on the
# spot. Retained rows are therefore `limit × distinct stages`, not the whole history, and the
# answer is identical to the unbounded read.
# --------------------------------------------------------------------------- #


def test_the_cross_stage_read_drops_rows_it_can_PROVE_are_outranked(svc):
    """The bound, on the real read path: 3 rows per stage across 2 stages with ``limit=2`` must
    retain 2 per stage (4), not all 6. Dropping the 3rd row of a stage is safe because two
    NEWER rows of that same stage are already held, so it cannot reach the global top 2."""
    table = _ddb(svc, MagicMock())
    rows = []
    for stage in ("prod", "dev"):  # descending sk order, as ScanIndexForward=False returns
        for hour in (12, 11, 10):
            ts = f"2026-07-31T{hour}:00:00+00:00"
            rows.append(
                _row(f"{REPO_ID}#{stage}#{ts}#{hour:04d}", stage=stage, started_at=ts,
                     image_tag=f"{stage}-{hour}h")
            )
    table.query.return_value = {"Items": rows}

    retained = svc._all_stage_deployments(REPO_ID, 2)

    assert len(retained) == 4  # 2 per stage — NOT all 6
    assert {r.image_tag for r in retained} == {"prod-12h", "prod-11h", "dev-12h", "dev-11h"}
    # …and the bounded read still returns exactly the right answer.
    table.query.return_value = {"Items": rows}
    assert [r.image_tag for r in svc.list_deployments(REPO_ID, limit=2)] == [
        "prod-12h",
        "dev-12h",
    ]


def test_the_bound_is_per_stage_so_a_newer_row_on_a_LATER_page_still_wins(svc):
    """The bound must not become the truncation it replaced. `dev` sorts after `prod` in sk
    order, so its rows arrive LAST — if the cap were global rather than per-stage, filling it on
    the prod rows would drop the newest deployment, which is exactly the P3-era bug."""
    table = _ddb(svc, MagicMock())
    prod = [
        _row(f"{REPO_ID}#prod#2026-07-31T0{h}:00:00+00:00#000{h}", stage="prod",
             started_at=f"2026-07-31T0{h}:00:00+00:00", image_tag=f"p-0{h}h")
        for h in (3, 2, 1)
    ]
    dev_newest = _row(f"{REPO_ID}#dev#2026-07-31T23:00:00+00:00#0009", stage="dev",
                      started_at="2026-07-31T23:00:00+00:00", image_tag="d-23h")
    table.query.side_effect = [
        {"Items": prod, "LastEvaluatedKey": {"pk": _DEPLOYMENT_PK, "sk": prod[-1]["sk"]}},
        {"Items": [dev_newest]},
    ]

    assert [r.image_tag for r in svc.list_deployments(REPO_ID, limit=1)] == ["d-23h"]


def test_the_cross_stage_read_stops_at_the_page_cap_instead_of_paging_forever(svc):
    """The hard valve. The per-stage bound caps MEMORY but not the number of round-trips: a repo
    with thousands of stages, or a store that keeps handing back a ``LastEvaluatedKey``, would
    still page without end and hang the request. So the loop stops at
    ``_MAX_DEPLOYMENT_PAGES`` and LOGS — a short answer the docstring admits to, rather than a
    request that never returns."""
    from services.project_service import _MAX_DEPLOYMENT_PAGES

    table = _ddb(svc, MagicMock())
    # Always another page: an unbounded loop never returns from this.
    table.query.return_value = {
        "Items": [],
        "LastEvaluatedKey": {"pk": _DEPLOYMENT_PK, "sk": f"{REPO_ID}#dev#x#0001"},
    }

    assert svc.list_deployments(REPO_ID) == []
    assert table.query.call_count == _MAX_DEPLOYMENT_PAGES
