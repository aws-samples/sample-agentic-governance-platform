"""The append-only deployment store: ``ProjectService.append_deployment`` and ``list_deployments``.

The singular scalars on ``Repository`` (``last_promoted_*``, ``prod_candidate_*``) are overwritten
wholesale by a newer merge and cleared on a successful promote, so on their own one promote erased the
evidence of the previous one and rollback had no artifact to roll back to. The store is the history.

Five invariants this file pins:

1. **An append never overwrites.** Every call adds a row; N appends ⇒ N rows. This is the whole
   point of the partition, and the failure it replaces was silent (a lost row looks exactly like
   "nothing ever deployed").
2. **Reads are newest-first.** The sk carries ``started_at``, so time-sort needs no counter and
   no append-time coordination (no read-modify-write, so no append race).
3. **Two appends in the SAME millisecond both survive.** The 4-char id suffix on the sk is what
   makes that true — without it the two rows share a key and the second is refused rather than
   silently replacing the first. Pinned with a FIXED clock, which is the same-millisecond case by
   construction.
4. **A malformed row is skipped, not fatal.** Matches the existing ``_parse_rows`` tolerance
   (``project_store.py``): a single pre-rename or partially written item must never 500 a whole
   history list.
5. **The cross-stage read is bounded without the caller's ``limit`` as a DDB ``Limit``.** It
   cannot let DynamoDB truncate (a ``Limit`` applies in sk order, which is stage order there), so
   it reads the ``{repo_id}#`` prefix whole, newest-first within each stage, and keeps ``limit``
   rows per stage: the global newest-``limit`` set holds at most ``limit`` rows from any one stage,
   so every later row of a stage is provably outranked and dropped. Retained rows are ``limit``
   times the distinct stages, and the answer equals the unbounded read. The one ``Limit`` it does
   carry is the row ceiling ``_MAX_DEPLOYMENT_ROWS``, the liveness valve pinned last in this file.

The real ``ProjectService`` with ``table_name=""`` (the in-memory store), an injected clock so
timestamps are asserted exactly, and every collaborator a ``MagicMock`` so nothing can reach
GitHub, CodeBuild, Secrets Manager or Entra. The memory store carries DynamoDB's semantics, so key
shape and sort direction are read back from it; the query kwargs, the paging and a store fault are
pinned on a ``MagicMock`` table injected through ``StateStore(table=...)``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from seed import seed

from core.state_store import StateStore
from models.deployment import Deployment, DeploymentOutcome
from services.project_service import ProjectService
from services.project_store import _DEPLOYMENT_PK, _MAX_DEPLOYMENT_ROWS

FIXED = datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc)
FIXED_TS = FIXED.isoformat()

REPO_ID = "repo-1"
AGENT_ID = "a-1"


@pytest.fixture
def svc():
    """The real in-memory ``ProjectService``: ``table_name=""`` selects the in-memory store."""
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
    return svc.append_deployment(repo_id=REPO_ID, agent_id=AGENT_ID, stage=stage, image_tag=image_tag, **over)


def _on_table(svc, table):
    """Put the service's store on ``table`` so the real query and put kwargs can be asserted."""
    svc._store = StateStore("projects", table=table)
    return table


def test_appends_accumulate_and_never_overwrite(svc):
    """The invariant the singular ``last_promoted_*`` scalars broke: three promotes of the SAME
    repo+stage leave three rows, each remembering its own artifact. Without this, rollback has
    no candidate to roll back TO."""
    for tag in ("a-1-tree001", "a-1-tree002", "a-1-tree003"):
        _append(svc, image_tag=tag)

    rows = svc.list_deployments(REPO_ID)
    assert len(rows) == 3
    assert {r.image_tag for r in rows} == {"a-1-tree001", "a-1-tree002", "a-1-tree003"}
    assert len({r.id for r in rows}) == 3
    assert len({r.seq_key for r in rows}) == 3


def test_append_returns_the_pinned_record_shape(svc):
    """The pinned record shape: a ``dep-<8 hex>`` id, an ISO-8601 ``started_at``, the ``started`` default
    outcome, and the sk mirrored onto the record for round-tripping."""
    record = _append(
        svc,
        source_sha="1" * 40,
        build_id="build-1",
        actor="merger-login",
        actor_kind="github",
    )

    assert record.id.startswith("dep-") and len(record.id) == len("dep-") + 8
    assert int(record.id[4:], 16) >= 0
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
    """``stage`` is whatever the API returns. A tenant with a ``staging`` stage must persist,
    not raise — hardcoding a dev/prod literal here is the drift this contract forbids."""
    record = _append(svc, stage="staging")

    assert record.stage == "staging"
    assert svc.list_deployments(REPO_ID, stage="staging")[0].id == record.id
    assert svc.list_deployments(REPO_ID, stage="prod") == []


def test_the_append_writes_the_pinned_partition_and_sort_key(svc):
    """The key shape, read back from the stored row: ``pk="deployment"`` and
    ``sk = "{repo_id}#{stage}#{started_at}#{id[-4:]}"``, the sk mirrored onto the record so a row
    round-trips."""
    record = _append(svc)

    item = svc._store.get((_DEPLOYMENT_PK, record.seq_key))
    assert item["pk"] == _DEPLOYMENT_PK == "deployment"
    assert item["sk"] == f"{REPO_ID}#prod#{FIXED_TS}#{record.id[-4:]}"
    assert item["sk"] == record.seq_key
    assert item["image_tag"] == "a-1-tree000"


def test_an_append_is_a_create_and_never_an_update(svc):
    """Nothing ever updates a row: the append is one conditional put and no ``update_item``."""
    table = _on_table(svc, MagicMock())

    record = _append(svc)

    kwargs = table.put_item.call_args.kwargs
    assert kwargs["Item"]["sk"] == record.seq_key
    assert "ConditionExpression" in kwargs
    table.update_item.assert_not_called()


def test_a_repeated_sort_key_is_refused_not_overwritten(svc):
    """The write is ``put_new``: a second write under an existing sk is refused, so a lost row can
    never look like "nothing ever deployed"."""
    record = _append(svc)

    with pytest.raises(RuntimeError):
        svc._save_deployment(record)
    assert len(svc.list_deployments(REPO_ID)) == 1


def test_an_empty_table_name_selects_the_in_memory_store(svc):
    """The service is constructed with ``table_name=""`` in tests and in any deployment without a
    table; the one store then lives in memory, and the append and the read go through it."""
    assert svc._store.in_memory
    record = _append(svc)
    assert svc.list_deployments(REPO_ID) == [record]


def test_list_is_newest_first(svc):
    """History reads newest-first because that is the only order a "what is deployed now / what
    can I roll back to" surface can use without re-sorting client-side."""
    clock = [
        datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 31, 11, 0, tzinfo=timezone.utc),
    ]
    for moment, tag in zip(clock, ("oldest", "middle", "newest"), strict=True):
        svc._now = lambda moment=moment: moment
        _append(svc, image_tag=tag)

    rows = svc.list_deployments(REPO_ID)
    assert [r.image_tag for r in rows] == ["newest", "middle", "oldest"]
    assert [r.started_at for r in rows] == [m.isoformat() for m in reversed(clock)]


def _at(svc, moment: datetime):
    """Point the injected clock at one instant (so appends carry asserted timestamps)."""
    svc._now = lambda moment=moment: moment


def test_list_is_newest_first_ACROSS_STAGES(svc):
    """The ordering invariant where it can actually break: across stages.

    ``test_list_is_newest_first`` appends only to ``prod``, so the sk's stage component is
    constant there and the assertion passes for the wrong reason. With ``stage=None`` the query
    prefix widens to ``f"{repo_id}#"`` and the sk is
    ``{repo_id}#{stage}#{started_at}#{suffix}`` — so DynamoDB (and any naive sk sort) orders by
    STAGE NAME FIRST and only then by time. Appending prod@08h, dev@09h, prod@10h, dev@11h
    returned ``[p-10h, p-08h, d-11h, d-09h]``: two stage-grouped runs, not a history.

    The sk format is a pinned contract other modules code against, so this is fixed on the read side:
    the cross-stage path merges by ``started_at``, and each single-stage view stays newest-first through
    the pinned ``begins_with`` path."""
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

    assert [r.image_tag for r in rows] == ["d-11h", "p-10h", "d-09h", "p-08h"]
    assert [r.stage for r in rows] == ["dev", "prod", "dev", "prod"]
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


def test_the_cross_stage_query_does_not_truncate_before_the_merge(svc):
    """Pinned on the real read path: the cross-stage query never passes the caller's ``limit`` as
    the DDB ``Limit``; the one ``Limit`` it carries is the row ceiling, one past
    ``_MAX_DEPLOYMENT_ROWS``, and the caller's ``limit`` is applied per stage and after the merge.

    A ``Limit`` is applied by DynamoDB in sk order — i.e. in STAGE order here — so the caller's
    ``limit`` would truncate to the alphabetically-first stage before the merge could see the
    newest row. The single-stage path is the opposite: its sk order IS time order, so ``Limit`` is
    correct and cheap there. The two paths must therefore differ, and that difference is the fix."""
    table = _on_table(svc, MagicMock())
    prod = [
        _row(
            f"{REPO_ID}#prod#2026-07-31T0{h}:00:00+00:00#000{h}",
            started_at=f"2026-07-31T0{h}:00:00+00:00",
            image_tag=f"p-0{h}h",
        )
        for h in (3, 2, 1)
    ]
    dev_newest = _row(
        f"{REPO_ID}#dev#2026-07-31T23:00:00+00:00#0009",
        stage="dev",
        started_at="2026-07-31T23:00:00+00:00",
        image_tag="d-23h",
    )
    table.query.return_value = {"Items": [*prod, dev_newest]}

    rows = svc.list_deployments(REPO_ID, limit=2)
    assert table.query.call_args.kwargs["Limit"] == _MAX_DEPLOYMENT_ROWS + 1
    assert [r.image_tag for r in rows] == ["d-23h", "p-03h"]

    table.reset_mock()
    table.query.return_value = {"Items": []}
    svc.list_deployments(REPO_ID, stage="prod", limit=2)
    assert table.query.call_args.kwargs["Limit"] == 2


def test_the_cross_stage_read_follows_pagination(svc):
    """Because the cross-stage read cannot let DDB truncate, the store pages the prefix for it —
    otherwise "newest" is only the newest of whatever DynamoDB's first 1 MB page happened to
    contain, which is a silent wrong answer rather than a missing one. The newest row on the second
    page still comes first."""
    table = _on_table(svc, MagicMock())
    page_1 = _row(
        f"{REPO_ID}#prod#2026-07-31T08:00:00+00:00#0001",
        image_tag="p-08h",
        started_at="2026-07-31T08:00:00+00:00",
    )
    page_2 = _row(
        f"{REPO_ID}#dev#2026-07-31T11:00:00+00:00#0002",
        image_tag="d-11h",
        stage="dev",
        started_at="2026-07-31T11:00:00+00:00",
    )
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
    assert [r.image_tag for r in rows] == ["d-11h", "p-08h"]


def test_list_filters_by_repo_and_stage_and_honours_limit(svc):
    """``stage=None`` is every stage for the repo; a named stage is that stage only. Another
    repo's rows are never visible — they live in the same partition, separated by the sk prefix."""
    _append(svc, stage="dev", image_tag="d-1")
    _append(svc, stage="prod", image_tag="p-1")
    svc.append_deployment(repo_id="repo-2", agent_id="a-2", stage="prod", image_tag="other-repo")

    assert len(svc.list_deployments(REPO_ID)) == 2
    assert [r.image_tag for r in svc.list_deployments(REPO_ID, stage="prod")] == ["p-1"]
    assert [r.image_tag for r in svc.list_deployments(REPO_ID, stage="dev")] == ["d-1"]
    assert [r.image_tag for r in svc.list_deployments("repo-2")] == ["other-repo"]
    assert len(svc.list_deployments(REPO_ID, limit=1)) == 1


def test_the_query_uses_begins_with_and_scans_backwards(svc):
    """The read contract, on the real read path: ``begins_with(sk, "{repo_id}#{stage}#")`` plus
    ``ScanIndexForward=False``. The sort direction is DynamoDB's job (a local re-sort would silently
    only order the first page), so the query itself is pinned; ``stage=None`` widens the prefix to the
    whole repo, never to the whole partition."""
    from boto3.dynamodb.conditions import Key

    table = _on_table(svc, MagicMock())
    table.query.return_value = {"Items": []}

    svc.list_deployments(REPO_ID, stage="prod", limit=7)

    kwargs = table.query.call_args.kwargs
    assert kwargs["KeyConditionExpression"] == (
        Key("pk").eq(_DEPLOYMENT_PK) & Key("sk").begins_with(f"{REPO_ID}#prod#")
    )
    assert kwargs["ScanIndexForward"] is False
    assert kwargs["Limit"] == 7

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

    table = _on_table(svc, MagicMock())
    table.query.side_effect = ClientError({"Error": {"Code": "Throttling"}}, "Query")

    assert svc.list_deployments(REPO_ID) == []


def test_two_same_millisecond_appends_both_survive(svc):
    """The clock is FIXED, so both rows carry an identical ``started_at`` — the exact collision
    the 4-char id suffix on the sk exists to break. Without the suffix the two rows share a key
    and the second append is refused: a promote that could not be recorded."""
    first = _append(svc, image_tag="same-ms-1")
    second = _append(svc, image_tag="same-ms-2")

    assert first.started_at == second.started_at == FIXED_TS
    assert first.id != second.id
    assert first.seq_key != second.seq_key

    rows = svc.list_deployments(REPO_ID)
    assert len(rows) == 2
    assert {r.image_tag for r in rows} == {"same-ms-1", "same-ms-2"}


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
    seed(svc._store, _row(f"{REPO_ID}#prod#{FIXED_TS}#0002", image_tag="readable"))
    seed(svc._store, {"pk": _DEPLOYMENT_PK, "sk": f"{REPO_ID}#prod#{FIXED_TS}#0001"})

    rows = svc.list_deployments(REPO_ID, stage="prod")

    assert [r.image_tag for r in rows] == ["readable"]


def test_a_malformed_outcome_value_is_skipped_not_fatal(svc):
    """The other malformed shape: a row whose enum value is not in ``DeploymentOutcome`` (an
    older writer, or a hand-edited row). Skipped like any other validation failure."""
    seed(svc._store, _row(f"{REPO_ID}#prod#{FIXED_TS}#0002", image_tag="readable"))
    seed(svc._store, _row(f"{REPO_ID}#prod#{FIXED_TS}#0001", outcome="exploded"))

    assert [r.image_tag for r in svc.list_deployments(REPO_ID, stage="prod")] == ["readable"]


def test_the_cross_stage_read_drops_rows_it_can_PROVE_are_outranked(svc):
    """The bound: 3 rows per stage across 2 stages with ``limit=2`` retain 2 per stage (4), not all 6,
    and the bounded read still returns exactly the right answer. Dropping the 3rd row of a stage is
    safe because two newer rows of that same stage are already held, so it cannot reach the global
    top 2."""
    for stage in ("prod", "dev"):
        for hour in (12, 11, 10):
            ts = f"2026-07-31T{hour}:00:00+00:00"
            seed(
                svc._store,
                _row(
                    f"{REPO_ID}#{stage}#{ts}#{hour:04d}",
                    stage=stage,
                    started_at=ts,
                    image_tag=f"{stage}-{hour}h",
                ),
            )

    retained = svc._all_stage_deployments(REPO_ID, 2)

    assert len(retained) == 4
    assert {r.image_tag for r in retained} == {"prod-12h", "prod-11h", "dev-12h", "dev-11h"}
    assert [r.image_tag for r in svc.list_deployments(REPO_ID, limit=2)] == [
        "prod-12h",
        "dev-12h",
    ]


def test_the_bound_is_per_stage_so_a_newer_row_on_a_LATER_page_still_wins(svc):
    """The bound must not become the truncation it replaced. `dev` sorts after `prod` in sk
    order, so its rows arrive LAST — if the cap were global rather than per-stage, filling it on
    the prod rows would drop the newest deployment, which is exactly the truncation bug the merge fixed."""
    table = _on_table(svc, MagicMock())
    prod = [
        _row(
            f"{REPO_ID}#prod#2026-07-31T0{h}:00:00+00:00#000{h}",
            stage="prod",
            started_at=f"2026-07-31T0{h}:00:00+00:00",
            image_tag=f"p-0{h}h",
        )
        for h in (3, 2, 1)
    ]
    dev_newest = _row(
        f"{REPO_ID}#dev#2026-07-31T23:00:00+00:00#0009",
        stage="dev",
        started_at="2026-07-31T23:00:00+00:00",
        image_tag="d-23h",
    )
    table.query.side_effect = [
        {"Items": prod, "LastEvaluatedKey": {"pk": _DEPLOYMENT_PK, "sk": prod[-1]["sk"]}},
        {"Items": [dev_newest]},
    ]

    assert [r.image_tag for r in svc.list_deployments(REPO_ID, limit=1)] == ["d-23h"]


def test_the_cross_stage_read_stops_at_the_row_cap_instead_of_paging_forever(svc, caplog):
    """The hard valve. The per-stage bound caps what is RETAINED, not what is READ: a repo with
    thousands of stages would still have the store page the whole prefix and hang the request. So
    the read asks the store for one row past ``_MAX_DEPLOYMENT_ROWS``; when that row arrives the
    paging stops, the row is dropped and a warning is LOGGED — a short answer the log admits to,
    rather than a request that never returns. The cut is stage-major, so the row dropped here is
    the newest deployment of a later-arriving stage: the warning is the only signal, which is the
    trade the decision record names."""
    table = _on_table(svc, MagicMock())
    prod = _row(f"{REPO_ID}#prod#{FIXED_TS}#00000")
    at_the_ceiling = [
        {**prod, "sk": f"{REPO_ID}#prod#{FIXED_TS}#{i:05d}", "seq_key": f"{REPO_ID}#prod#{FIXED_TS}#{i:05d}"}
        for i in range(_MAX_DEPLOYMENT_ROWS)
    ]
    newest = _row(
        f"{REPO_ID}#dev#2026-07-31T23:00:00+00:00#99999",
        stage="dev",
        started_at="2026-07-31T23:00:00+00:00",
        image_tag="d-23h",
    )
    older = _row(
        f"{REPO_ID}#dev#2026-07-31T22:00:00+00:00#99998",
        stage="dev",
        started_at="2026-07-31T22:00:00+00:00",
        image_tag="d-22h",
    )
    table.query.side_effect = [
        {"Items": [*at_the_ceiling, newest], "LastEvaluatedKey": {"pk": _DEPLOYMENT_PK, "sk": newest["sk"]}},
        {"Items": [older]},
    ]

    with caplog.at_level(logging.WARNING, logger="services.project_service"):
        rows = svc.list_deployments(REPO_ID)

    assert table.query.call_count == 1
    assert table.query.call_args.kwargs["Limit"] == _MAX_DEPLOYMENT_ROWS + 1
    assert len(rows) == 50
    assert {r.stage for r in rows} == {"prod"}
    ceiling_hits = [r for r in caplog.records if "read ceiling" in r.getMessage()]
    assert len(ceiling_hits) == 1
    assert "MAY OMIT THE NEWEST DEPLOYMENT" in ceiling_hits[0].getMessage()
