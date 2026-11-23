"""EvalStore: S3 run records + DynamoDB index + CloudWatch EMF scores.

The behaviour tests run in memory: the index on the state store's memory double, the S3 half on
the store's own object dictionary. The layout tests run the production constructor under moto,
S3 and DynamoDB both, and read the object keys and the row attributes back through the real
clients. The logs client is a MagicMock: only its call shape matters.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from moto.core.decorator import mock_aws

from core import aws_clients
from core.state_store import StateStore
from models.eval import (
    DatasetItem,
    EvalConfig,
    EvalRun,
    GateResult,
    ItemResult,
    PromoteOverride,
    RunStatus,
    RunSummary,
    Score,
    ToolCall,
)
from services import eval_store as eval_store_module
from services.eval_store import EvalStore

T0 = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
REGION = "us-east-1"
BUCKET = "evals-bucket"
TABLE = "evals-table"
LOG_GROUP = "/agp/evals/results/test"
MINUTE = 60


class _Clock:
    def __init__(self, at: datetime):
        self.t = at.timestamp()

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: timedelta) -> None:
        self.t += delta.total_seconds()


class _Unreachable:
    """A table double whose every call fails like a missing table."""

    def __getattr__(self, name):
        def _raise(**kwargs):
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "Requested resource not found"}},
                name,
            )

        return _raise


@pytest.fixture
def store():
    return EvalStore()


@pytest.fixture
def aws():
    with mock_aws():
        aws_clients.client("s3", region=REGION).create_bucket(Bucket=BUCKET)
        aws_clients.client("dynamodb", region=REGION).create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield EvalStore(bucket=BUCKET, table=TABLE, log_group=LOG_GROUP, logs=MagicMock(), region=REGION)


def _objects() -> dict[str, bytes]:
    s3 = aws_clients.client("s3", region=REGION)
    listed = s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])
    return {o["Key"]: s3.get_object(Bucket=BUCKET, Key=o["Key"])["Body"].read() for o in listed}


def _rows() -> list[dict]:
    return aws_clients.dynamodb_table(TABLE, region=REGION).scan()["Items"]


def _logging_store():
    return EvalStore(log_group=LOG_GROUP, logs=MagicMock())


def _run(
    run_id="r1",
    *,
    started=T0,
    digest: str | None = "sha256:aaa",
    status: RunStatus = "running",
    repo_id="repo-1",
    **kw,
):
    return EvalRun(
        run_id=run_id,
        repo_id=repo_id,
        agent_id="agent-1",
        project_id="proj-1",
        git_sha="abc123",
        digest=digest,
        trigger=kw.pop("trigger", "manual"),
        started_by="user-1",
        started_at=started,
        status=status,
        config=EvalConfig(),
        **kw,
    )


def _results():
    return [
        ItemResult(
            item_id="i1",
            session_id="sess-1",
            output="ok",
            tool_calls=[ToolCall(name="get_policy", args={"id": "p1"})],
            latency_ms=120,
            scores=[
                Score(evaluator="tools", kind="check", value=1.0, passed=True),
                Score(
                    evaluator="Builtin.Correctness",
                    kind="judge",
                    value=0.9,
                    passed=None,
                    label="Correct",
                    explanation="matches",
                ),
                Score(
                    evaluator="Builtin.GoalSuccessRate", kind="judge", value=None, passed=None, error="boom"
                ),
            ],
        ),
        ItemResult(
            item_id="i2",
            error="timeout",
            scores=[Score(evaluator="no_error", kind="check", value=0.0, passed=False)],
        ),
    ]


def test_list_runs_newest_first_and_limit(store):
    for i in range(4):
        store.save_run(_run(f"r{i}", started=T0 + timedelta(minutes=i)))
    store.save_run(_run("other", repo_id="repo-2"))

    runs = store.list_runs("repo-1")
    assert [r.run_id for r in runs] == ["r3", "r2", "r1", "r0"]
    assert [r.run_id for r in store.list_runs("repo-1", limit=2)] == ["r3", "r2"]
    assert [r.run_id for r in store.list_runs("repo-2")] == ["other"]
    assert store.list_runs("nobody") == []


def test_save_run_again_updates_in_place(store):
    store.save_run(_run("r1"))
    done = _run(
        "r1",
        status="passed",
        finished_at=T0 + timedelta(minutes=3),
        summary=RunSummary(
            items=2, checks_passed=2, checks_failed=0, judge_means={"Builtin.Correctness": 0.9}
        ),
        gate=GateResult(passed=True),
    )
    store.save_run(done, _results(), from_status="running")

    runs = store.list_runs("repo-1")
    assert len(runs) == 1
    assert runs[0] == done
    assert store.get_run("repo-1", "r1") == (done, _results())


def test_a_finished_run_closes_only_a_running_row(store):
    store.save_run(_run("r1", status="passed"))
    with pytest.raises(RuntimeError):
        store.save_run(_run("r1", status="failed"), from_status="running")
    with pytest.raises(RuntimeError):
        store.save_run(_run("r1"))
    assert [r.status for r in store.list_runs("repo-1")] == ["passed"]


def test_get_run_round_trip_with_results(store):
    run = _run("r1", status="failed", gate=GateResult(passed=False, reasons=["no_error failed on i2"]))
    results = _results()
    store.save_run(run, results, [DatasetItem(id="i1", input="hi"), DatasetItem(id="i2", input="yo")])

    got = store.get_run("repo-1", "r1")
    assert got is not None
    got_run, got_results = got
    assert got_run == run
    assert got_results == results


def test_get_run_without_results_and_missing(store):
    store.save_run(_run("r1"))
    assert store.get_run("repo-1", "r1") == (_run("r1"), [])
    assert store.get_run("repo-1", "nope") is None
    assert store.get_run("repo-2", "r1") is None


def test_latest_run_for_build_matches_digest_first(store):
    store.save_run(_run("old", started=T0, digest="sha256:aaa", status="passed"))
    store.save_run(_run("new", started=T0 + timedelta(minutes=5), digest="sha256:aaa", status="failed"))
    store.save_run(_run("newest-other", started=T0 + timedelta(minutes=9), digest="sha256:bbb"))
    store.save_run(_run("nodigest", started=T0 + timedelta(minutes=10), digest=None))

    assert store.latest_run_for_build("repo-1", digest="sha256:aaa", tag=None).run_id == "new"
    assert store.latest_run_for_build("repo-1", digest="sha256:bbb", tag="ignored").run_id == "newest-other"
    assert store.latest_run_for_build("repo-1", digest="sha256:zzz", tag=None) is None
    assert store.latest_run_for_build("repo-2", digest="sha256:aaa", tag=None) is None


def test_latest_run_for_build_falls_back_to_the_tag(store):
    store.save_run(_run("t-old", started=T0, digest=None, build_tag="abc"))
    store.save_run(_run("t-new", started=T0 + timedelta(minutes=1), digest=None, build_tag="abc"))
    store.save_run(_run("other", started=T0 + timedelta(minutes=2), digest=None, build_tag="def"))

    assert store.latest_run_for_build("repo-1", digest=None, tag="abc").run_id == "t-new"
    assert store.latest_run_for_build("repo-1", digest="", tag="def").run_id == "other"
    assert store.latest_run_for_build("repo-1", digest=None, tag="zzz") is None
    assert store.latest_run_for_build("repo-1", digest=None, tag=None) is None


def test_latest_run_for_build_reads_one_page_before_the_whole_partition(store):
    page = eval_store_module.LATEST_RUN_FIRST_PAGE
    for i in range(page + 5):
        store.save_run(_run(f"r{i}", started=T0 + timedelta(minutes=i), digest=f"sha256:{i}"))
    store.save_run(_run("lone", repo_id="repo-2"))

    with patch.object(store._store, "query", wraps=store._store.query) as query:
        inside = store.latest_run_for_build("repo-1", digest=f"sha256:{page + 2}", tag=None)
        assert inside.run_id == f"r{page + 2}"
        assert [c.kwargs["limit"] for c in query.call_args_list] == [page]

        query.reset_mock()
        assert store.latest_run_for_build("repo-1", digest="sha256:1", tag=None).run_id == "r1"
        assert [c.kwargs["limit"] for c in query.call_args_list] == [page, None]

        query.reset_mock()
        assert store.latest_run_for_build("repo-2", digest="sha256:zzz", tag=None) is None
        assert [c.kwargs["limit"] for c in query.call_args_list] == [page]


def test_get_run_dataset_returns_the_dataset_as_run(store):
    from models.eval import DatasetItem, ExpectedTool

    items = [DatasetItem(id="i1", input="hi", expected_tools=[ExpectedTool(name="t1", args={"x": 1})])]
    store.save_run(_run("r1"))
    assert store.get_run_dataset("repo-1", "r1") == []
    store.save_run(_run("r1"), _results(), items, from_status="running")
    assert store.get_run_dataset("repo-1", "r1") == items
    assert store.get_run_dataset("repo-1", "nope") == []


def test_overrides_newest_first(store):
    store.save_run(_run("r1"))
    for i in range(3):
        store.record_override(
            PromoteOverride(
                repo_id="repo-1",
                digest=None if i == 1 else f"sha256:{i}",
                by="u",
                reason=f"why {i}",
                at=T0 + timedelta(minutes=i),
            )
        )
    store.record_override(PromoteOverride(repo_id="repo-2", digest="d", by="u", reason="x", at=T0))

    got = store.list_overrides("repo-1")
    assert [o.reason for o in got] == ["why 2", "why 1", "why 0"]
    assert got[1].digest is None
    assert [o.reason for o in store.list_overrides("repo-1", limit=1)] == ["why 2"]
    assert [r.run_id for r in store.list_runs("repo-1")] == ["r1"]


def test_two_overrides_in_the_same_second_for_the_same_digest_both_land(store):
    override = PromoteOverride(repo_id="repo-1", digest="sha256:aaa", by="u", reason="twice", at=T0)
    store.record_override(override)
    store.record_override(override)
    assert [o.reason for o in store.list_overrides("repo-1")] == ["twice", "twice"]


def test_aws_layout_keys_and_row(aws):
    run = _run(
        "r1", summary=RunSummary(items=1, checks_passed=1, checks_failed=0), gate=GateResult(passed=True)
    )
    items = [DatasetItem(id="i1", input="hi")]
    aws.save_run(run, _results(), items)

    objects = _objects()
    assert set(objects) == {
        "runs/repo-1/r1/run.json",
        "runs/repo-1/r1/results.jsonl",
        "runs/repo-1/r1/dataset.jsonl",
    }
    lines = objects["runs/repo-1/r1/results.jsonl"].decode().splitlines()
    assert [json.loads(line)["item_id"] for line in lines] == ["i1", "i2"]
    dataset = objects["runs/repo-1/r1/dataset.jsonl"].decode()
    assert json.loads(dataset.splitlines()[0]) == {
        "id": "i1",
        "input": "hi",
        "assertions": [],
        "expected_tools": [],
        "tags": [],
    }

    (row,) = _rows()
    assert row["pk"] == "REPO#repo-1"
    assert row["sk"] == f"RUN#{T0.isoformat()}#r1"
    assert row["run_id"] == "r1" and row["status"] == "running" and row["digest"] == "sha256:aaa"
    assert row["git_sha"] == "abc123" and row["trigger"] == "manual" and row["started_by"] == "user-1"
    assert json.loads(row["summary"])["items"] == 1
    assert json.loads(row["gate"]) == {"passed": True, "reasons": []}
    assert row["s3_prefix"] == "runs/repo-1/r1/"

    assert aws.list_runs("repo-1") == [run]
    assert aws.get_run("repo-1", "r1") == (run, _results())
    assert aws.get_run_dataset("repo-1", "r1") == items
    assert aws.get_run("repo-1", "nope") is None


def test_aws_save_run_without_results_writes_only_run_json(aws):
    aws.save_run(_run("r1"))
    assert set(_objects()) == {"runs/repo-1/r1/run.json"}


def test_aws_override_row_key(aws):
    aws.record_override(PromoteOverride(repo_id="repo-1", digest=None, by="u", reason="hotfix", at=T0))
    (row,) = _rows()
    assert row["pk"] == "REPO#repo-1"
    assert row["sk"].startswith(f"OVERRIDE#{T0.isoformat()}#none#")
    assert "by_name" not in row and "build_tag" not in row


def test_aws_override_round_trips_attribution(aws):
    """The display name and build tag survive the DDB row."""
    aws.record_override(
        PromoteOverride(
            repo_id="repo-1",
            digest=None,
            by="oid-1",
            reason="hotfix",
            at=T0,
            by_name="Lars",
            build_tag="a-1-abc1234",
        )
    )
    (row,) = _rows()
    assert (row["by_name"], row["build_tag"]) == ("Lars", "a-1-abc1234")
    [o] = aws.list_overrides("repo-1")
    assert (o.by, o.by_name, o.build_tag) == ("oid-1", "Lars", "a-1-abc1234")


def _emitted(store):
    call = store._logs.put_log_events.call_args
    assert call is not None
    kwargs = call.kwargs
    assert kwargs["logGroupName"] == LOG_GROUP
    return kwargs, [json.loads(e["message"]) for e in kwargs["logEvents"]]


def test_emit_scores_one_batch_with_emf_shape():
    store = _logging_store()
    run = _run("r1", trigger="auto")
    store.emit_scores(run, _results())

    assert store._logs.put_log_events.call_count == 1
    kwargs, events = _emitted(store)
    assert kwargs["logStreamName"] == "r1"
    assert len(events) == 4

    tools, judge, judge_err, no_error = events
    directive = tools["_aws"]["CloudWatchMetrics"][0]
    assert directive["Namespace"] == "AGP/Evals"
    assert directive["Dimensions"] == [["RepoId", "Evaluator", "Source"]]
    assert {m["Name"] for m in directive["Metrics"]} == {"Score", "Pass"}
    assert tools["RepoId"] == "repo-1" and tools["Evaluator"] == "tools" and tools["Source"] == "auto"
    assert tools["Score"] == 1.0 and tools["Pass"] == 1
    assert tools["run_id"] == "r1" and tools["item_id"] == "i1" and tools["session_id"] == "sess-1"
    assert tools["agent_id"] == "agent-1"
    assert tools["gen_ai.evaluation.name"] == "tools"
    assert tools["gen_ai.evaluation.score.value"] == 1.0

    assert [m["Name"] for m in judge["_aws"]["CloudWatchMetrics"][0]["Metrics"]] == ["Score"]
    assert "Pass" not in judge
    assert judge["gen_ai.evaluation.score.label"] == "Correct"
    assert judge["gen_ai.evaluation.explanation"] == "matches"

    assert "_aws" not in judge_err and "Score" not in judge_err and "Pass" not in judge_err

    assert no_error["Pass"] == 0 and no_error["Score"] == 0.0 and no_error["session_id"] is None

    for e in events:
        if "_aws" in e:
            dims = e["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
            assert all(d not in ("RunId", "ItemId", "SessionId") for group in dims for d in group)


def test_emit_scores_creates_stream_and_tolerates_existing():
    store = _logging_store()
    store._logs.exceptions.ResourceAlreadyExistsException = type("Exists", (Exception,), {})
    store._logs.create_log_stream.side_effect = store._logs.exceptions.ResourceAlreadyExistsException()
    store.emit_scores(_run("r1"), _results())
    store._logs.create_log_stream.assert_called_once_with(logGroupName=LOG_GROUP, logStreamName="r1")
    assert store._logs.put_log_events.call_count == 1


def test_a_table_without_a_bucket_keeps_the_whole_store_in_memory(aws):
    table_only = EvalStore(table=TABLE)
    assert table_only._store.in_memory
    table_only.save_run(_run("r1"))
    assert [r.run_id for r in table_only.list_runs("repo-1")] == ["r1"]
    assert table_only.get_run("repo-1", "r1") == (_run("r1"), [])
    assert EvalStore(bucket=BUCKET, s3=MagicMock())._store.in_memory
    assert not aws._store.in_memory


def test_a_table_fault_surfaces_unchanged():
    store = EvalStore(store=StateStore("", table=_Unreachable()))
    with pytest.raises(ClientError) as exc:
        store.acquire_run_lock("repo-1", "a", ttl_s=MINUTE)
    assert exc.value.response["Error"]["Code"] == "ResourceNotFoundException"
    with pytest.raises(ClientError):
        store.record_override(PromoteOverride(repo_id="repo-1", digest=None, by="u", reason="x", at=T0))
    with pytest.raises(ClientError):
        store.list_runs("repo-1")


def test_emit_scores_never_raises(caplog):
    store = _logging_store()
    store._logs.put_log_events.side_effect = RuntimeError("throttled")
    store.emit_scores(_run("r1"), _results())
    assert "throttled" in caplog.text


def test_emit_scores_noop_without_scores_or_log_group():
    store = _logging_store()
    store.emit_scores(_run("r1"), [ItemResult(item_id="i1")])
    store._logs.put_log_events.assert_not_called()
    EvalStore().emit_scores(_run("r1"), _results())


def test_get_eval_store_is_a_memory_singleton_by_default(monkeypatch):
    monkeypatch.setattr(eval_store_module, "_store", None)
    a = eval_store_module.get_eval_store()
    assert a is eval_store_module.get_eval_store()
    assert a._store.in_memory and a._s3 is None and a._logs is None


def test_latest_run_for_build_finished_skips_running_and_errored_runs(store):
    store.save_run(_run("done", started=T0, digest="sha256:aaa", status="failed"))
    store.save_run(_run("errored", started=T0 + timedelta(minutes=1), digest="sha256:aaa", status="error"))
    store.save_run(_run("live", started=T0 + timedelta(minutes=2), digest="sha256:aaa", status="running"))

    assert store.latest_run_for_build("repo-1", digest="sha256:aaa", tag=None).run_id == "live"
    assert store.latest_run_for_build("repo-1", digest="sha256:aaa", tag=None, finished=True).run_id == "done"


def test_a_run_saved_before_baseline_existed_still_loads(store):
    legacy = json.loads(_run("legacy", status="passed").model_dump_json())
    legacy.pop("baseline")
    legacy.pop("regressions")
    run = EvalRun.model_validate(legacy)
    assert run.baseline is None and run.regressions == []
    store.save_run(run)
    assert store.get_run("repo-1", "legacy")[0].baseline is None


def test_the_run_lock_admits_one_holder_until_it_expires_or_is_released():
    clock = _Clock(T0)
    store = EvalStore(store=StateStore("", clock=clock))
    assert store.acquire_run_lock("repo-1", "a", ttl_s=19 * MINUTE)
    clock.advance(timedelta(minutes=5))
    assert not store.acquire_run_lock("repo-1", "b", ttl_s=19 * MINUTE)
    assert store.acquire_run_lock("repo-2", "c", ttl_s=19 * MINUTE)
    clock.advance(timedelta(minutes=14, seconds=1))
    assert store.acquire_run_lock("repo-1", "d", ttl_s=60 * MINUTE)
    store.release_run_lock("repo-1", "a")
    clock.advance(timedelta(minutes=2))
    assert not store.acquire_run_lock("repo-1", "e", ttl_s=60 * MINUTE)
    store.release_run_lock("repo-1", "d")
    assert store.acquire_run_lock("repo-1", "e", ttl_s=60 * MINUTE)


def test_the_run_lock_row_is_not_a_run(store):
    store.acquire_run_lock("repo-1", "a", ttl_s=19 * MINUTE)
    assert store.list_runs("repo-1") == [] and store.list_overrides("repo-1") == []


def test_concurrent_acquires_have_exactly_one_winner():
    store = EvalStore()
    with ThreadPoolExecutor(8) as pool:
        won = list(
            pool.map(lambda i: store.acquire_run_lock("repo-1", f"r{i}", ttl_s=60 * MINUTE), range(32))
        )
    assert won.count(True) == 1


def test_awaiting_build_holds_the_old_build_until_it_expires(store):
    assert store.awaiting_build("repo-1", now=T0) is None
    store.mark_awaiting_build("repo-1", "sha256:old", expires_at=T0 + timedelta(minutes=30))
    assert store.awaiting_build("repo-1", now=T0 + timedelta(minutes=29)) == "sha256:old"
    assert store.awaiting_build("repo-2", now=T0) is None
    assert store.awaiting_build("repo-1", now=T0 + timedelta(minutes=30)) is None
    store.mark_awaiting_build("repo-1", "", expires_at=T0 + timedelta(hours=1))
    assert store.awaiting_build("repo-1", now=T0) == ""
    assert store.list_runs("repo-1") == [] and store.list_overrides("repo-1") == []


def test_aws_awaiting_build_reads_the_newer_build_after_two_commits(aws):
    aws.mark_awaiting_build("repo-1", "sha256:old", expires_at=T0 + timedelta(minutes=30))
    aws.mark_awaiting_build("repo-1", "sha256:new", expires_at=T0 + timedelta(minutes=45))

    assert aws.awaiting_build("repo-1", now=T0 + timedelta(minutes=40)) == "sha256:new"
    assert _rows() == [
        {
            "pk": "REPO#repo-1",
            "sk": "AWAIT_BUILD",
            "build": "sha256:new",
            "expires_at": int((T0 + timedelta(minutes=45)).timestamp()),
        }
    ]
