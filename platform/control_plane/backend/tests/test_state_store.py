"""Conformance of ``core.state_store``: every primitive runs the same sequence against the memory
double and against a moto DynamoDB table, so the double is proven against the service it stands in
for, exception codes included. The tests that take no ``store`` fixture pin the selection rule
and the module's import-time behaviour.

Two sequences run on the memory double alone, each with its moto reason in its own docstring: the
``Limit``-after-ordering pin and the twenty-thread race for one conditional ``put_new``. One runs on
moto alone, with its reason in its docstring: a call from another thread while a condition is being
built, which exercises boto3's expression builder on the client path the double never runs.
"""

import importlib.util
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from boto3.dynamodb.conditions import ConditionBase
from botocore.exceptions import BotoCoreError, ClientError
from moto.core.decorator import mock_aws
from seed import seed

from core import aws_clients, state_store
from core.state_store import ABSENT, BY_DESIGN_UNCONDITIONAL, STORE_FAULTS, StateStore, VersionMismatch

REGION = "us-east-1"
TABLE = "conformance"
NOW = 1_800_000_000.0


class _Clock:
    def __init__(self, t: float = NOW):
        self.t = t

    def __call__(self) -> float:
        return self.t


class _Unreachable:
    """A table double whose every call fails like a missing table: the fault path of a configured name."""

    def __getattr__(self, name):
        def _raise(**kwargs):
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "Requested resource not found"}},
                name,
            )

        return _raise


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture(params=["memory", "moto"])
def store(request, clock):
    if request.param == "memory":
        yield StateStore("", clock=clock)
        return
    with mock_aws():
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
        yield StateStore(TABLE, region=REGION, clock=clock)


def test_put_new_writes_once(store):
    item = {"pk": "P", "sk": "S", "v": 1, "name": "first"}
    assert store.put_new(item) is True
    assert store.put_new({**item, "name": "second"}) is False
    assert store.get(("P", "S")) == item
    assert store.get(("P", "S"), consistent=True) == item
    assert store.get(("P", "other")) is None


def test_put_if_version_matches_or_raises(store):
    assert store.put_new({"pk": "P", "sk": "S", "version": 0, "name": "a"}) is True
    bumped = {"pk": "P", "sk": "S", "version": 1, "name": "b"}
    assert store.put_if_version(bumped, "version", 0) is None
    assert store.get(("P", "S")) == bumped
    with pytest.raises(VersionMismatch):
        store.put_if_version({**bumped, "version": 2, "name": "stale"}, "version", 0)
    assert store.get(("P", "S")) == bumped
    with pytest.raises(VersionMismatch):
        store.put_if_version({"pk": "P", "sk": "absent", "version": 1}, "version", 0)
    assert store.get(("P", "absent")) is None


def test_a_legacy_row_takes_one_unversioned_write_and_refuses_a_second(store):
    seed(store, {"pk": "T", "sk": "META", "name": "legacy"})
    first = {"pk": "T", "sk": "META", "name": "first", "row_version": 1}
    assert store.put_if_version(first, "row_version", None) is None
    assert store.get(("T", "META")) == first
    with pytest.raises(VersionMismatch):
        store.put_if_version({**first, "name": "second"}, "row_version", None)
    assert store.get(("T", "META")) == first


def test_an_int_after_the_legacy_write_is_strict(store):
    seed(store, {"pk": "T", "sk": "META", "name": "legacy"})
    store.put_if_version({"pk": "T", "sk": "META", "name": "first", "row_version": 1}, "row_version", None)
    second = {"pk": "T", "sk": "META", "name": "second", "row_version": 2}
    assert store.put_if_version(second, "row_version", 1) is None
    assert store.get(("T", "META")) == second
    for stale in (0, 1, 3):
        with pytest.raises(VersionMismatch):
            store.put_if_version({**second, "name": "stale", "row_version": stale + 1}, "row_version", stale)
    assert store.get(("T", "META")) == second


def test_expected_none_on_an_absent_row_raises_and_creates_nothing(store):
    with pytest.raises(VersionMismatch, match="T/absent is absent or already carries row_version"):
        store.put_if_version({"pk": "T", "sk": "absent", "row_version": 1}, "row_version", None)
    assert store.get(("T", "absent")) is None


def test_expected_none_on_a_deleted_row_raises_and_creates_nothing(store):
    legacy = {"pk": "T", "sk": "META", "name": "legacy"}
    seed(store, legacy)
    assert store.delete(("T", "META")) == legacy
    with pytest.raises(VersionMismatch):
        store.put_if_version({**legacy, "name": "resurrected", "row_version": 1}, "row_version", None)
    assert store.get(("T", "META")) is None


def test_a_row_carrying_token_version_refuses_expected_none(store):
    link = {"pk": "USER#u1", "sk": "GITHUB_LINK", "secret_id": "s1", "token_version": 0}
    seed(store, link)
    with pytest.raises(VersionMismatch):
        store.put_if_version({**link, "secret_id": "s2", "token_version": 1}, "token_version", None)
    assert store.get(("USER#u1", "GITHUB_LINK")) == link


def test_transition_from_a_set_including_absent(store):
    pending = {"pk": "M", "sk": "pub", "status": "pending", "by": "alice"}
    assert store.transition(pending, {"status": {ABSENT, "approved", "rejected"}}) is True
    assert store.get(("M", "pub")) == pending
    assert store.transition({**pending, "status": "pending", "by": "bob"}, {"status": {ABSENT}}) is False
    assert store.get(("M", "pub")) == pending
    approved = {"pk": "M", "sk": "pub", "status": "approved", "by": "alice", "decided_by": "carol"}
    assert store.transition(approved, {"status": ["pending"]}) is True
    assert store.get(("M", "pub")) == approved
    assert store.transition({**approved, "status": "rejected"}, {"status": ["pending"]}) is False
    assert store.get(("M", "pub")) == approved
    assert store.transition({**pending, "by": "dave"}, {"status": {ABSENT, "approved", "rejected"}}) is True
    assert store.get(("M", "pub")) == {**pending, "by": "dave"}


def test_transition_over_two_attributes(store):
    row = {"pk": "AGENT#1", "sk": "STATE", "state": "CONTAINED", "job": "running", "doc": "{}"}
    expected = {"state": {ABSENT, "RESTORED"}, "job": {ABSENT, "idle", "failed"}}
    assert store.transition(row, expected) is True
    assert store.transition({**row, "doc": "again"}, expected) is False
    assert store.get(("AGENT#1", "STATE")) == row
    done = {**row, "job": "contained", "doc": "{...}"}
    assert store.transition(done, {"state": {"CONTAINED"}, "job": {"running"}}) is True
    assert store.get(("AGENT#1", "STATE")) == done
    assert store.transition({**done, "job": "failed"}, {"state": {"CONTAINED"}, "job": {"running"}}) is False
    assert store.get(("AGENT#1", "STATE")) == done


def test_set_owned_upserts_only_the_named_attributes(store):
    key = ("AGENT#1", "MONITOR")
    store.set_owned(key, {"enabled": True, "cursor_ms": 5})
    assert store.get(key) == {"pk": "AGENT#1", "sk": "MONITOR", "enabled": True, "cursor_ms": 5}
    store.set_owned(key, {"cursor_ms": 9})
    assert store.get(key) == {"pk": "AGENT#1", "sk": "MONITOR", "enabled": True, "cursor_ms": 9}
    store.set_owned(key, {"status": "ready", "name": "reserved words pass through names"})
    assert store.get(key) == {
        "pk": "AGENT#1",
        "sk": "MONITOR",
        "enabled": True,
        "cursor_ms": 9,
        "status": "ready",
        "name": "reserved words pass through names",
    }


def test_set_owned_returns_the_written_row(store):
    key = ("LISTING", "agent#a1")
    assert store.put_new({"pk": "LISTING", "sk": "agent#a1", "title": "kept", "tags": ["x"]}) is True
    written = store.set_owned(key, {"price": 3, "status": "published"})
    assert written == {
        "pk": "LISTING",
        "sk": "agent#a1",
        "title": "kept",
        "tags": ["x"],
        "price": 3,
        "status": "published",
    }
    assert written == store.get(key)
    fresh = store.set_owned(("LISTING", "agent#a2"), {"title": "new"})
    assert fresh == {"pk": "LISTING", "sk": "agent#a2", "title": "new"} == store.get(("LISTING", "agent#a2"))


def test_update_existing_refuses_a_missing_row(store):
    key = ("U", "1")
    assert store.update_existing(key, {"a": 1}) is None
    assert store.get(key) is None
    assert store.put_new({"pk": "U", "sk": "1", "a": 0, "b": "kept"}) is True
    assert store.update_existing(key, {"a": 1, "c": "new"}) == {
        "pk": "U",
        "sk": "1",
        "a": 1,
        "b": "kept",
        "c": "new",
    }
    assert store.get(key) == {"pk": "U", "sk": "1", "a": 1, "b": "kept", "c": "new"}


def test_a_set_never_names_a_key_attribute(store):
    key = ("K", "1")
    row = {"pk": "K", "sk": "1", "a": 1}
    assert store.put_new(row) is True
    for attrs in ({"pk": "ZZZ", "a": 2}, {"sk": "2"}, {"a": 3, "pk": "K"}):
        with pytest.raises(ValueError):
            store.set_owned(key, attrs)
        with pytest.raises(ValueError):
            store.update_existing(key, attrs)
    assert store.get(key) == row
    assert store.query("K") == [row]


def test_acquire_lease_admits_one_holder_renews_it_and_frees_on_expiry(store, clock):
    key = ("LEASE", "monitor")
    clock.t = 1000.0
    assert store.acquire_lease(key, "h1", 30) is True
    assert store.get(key) == {"pk": "LEASE", "sk": "monitor", "holder": "h1", "expires_at": 1030, "ttl": 1030}
    assert store.acquire_lease(key, "h2", 30) is False
    clock.t = 1010.0
    assert store.acquire_lease(key, "h1", 30) is True
    assert store.get(key) == {"pk": "LEASE", "sk": "monitor", "holder": "h1", "expires_at": 1040, "ttl": 1040}
    clock.t = 1035.0
    assert store.acquire_lease(key, "h2", 30) is False
    clock.t = 1040.0
    assert store.acquire_lease(key, "h2", 30) is False
    clock.t = 1041.0
    assert store.acquire_lease(key, "h2", 30) is True
    assert store.get(key) == {"pk": "LEASE", "sk": "monitor", "holder": "h2", "expires_at": 1071, "ttl": 1071}


def test_release_lease_by_the_holder_only(store):
    key = ("LEASE", "monitor")
    assert store.acquire_lease(key, "h1", 30) is True
    assert store.release_lease(key, "h2") is None
    assert store.get(key)["holder"] == "h1"
    assert store.release_lease(key, "h1") is None
    assert store.get(key) is None
    assert store.release_lease(key, "h1") is None
    assert store.acquire_lease(key, "h2", 30) is True


def test_delete_returns_the_old_row_or_none(store):
    key = ("D", "1")
    assert store.delete(key) is None
    row = {"pk": "D", "sk": "1", "state": "nonce", "exp": 42}
    assert store.put_new(row) is True
    assert store.delete(key) == row
    assert store.get(key) is None
    assert store.delete(key) is None


def test_query_pages_and_orders(store):
    runs = [{"pk": "Q", "sk": f"RUN#{i:03d}", "i": i} for i in range(120)]
    for row in reversed(runs):
        assert store.put_new(row) is True
    assert store.put_new({"pk": "Q", "sk": "LOCK", "holder": "x"}) is True
    assert store.put_new({"pk": "other", "sk": "RUN#000", "i": -1}) is True
    everything = store.query("Q")
    assert [r["sk"] for r in everything] == ["LOCK", *(r["sk"] for r in runs)]
    assert store.query("Q", sk_prefix="RUN#") == runs
    assert store.query("Q", sk_prefix="RUN#", consistent=True) == runs
    newest = store.query("Q", sk_prefix="RUN#", forward=False, limit=50)
    assert [r["sk"] for r in newest] == [f"RUN#{i:03d}" for i in range(119, 69, -1)]
    assert store.query("Q", sk_prefix="RUN#", forward=False) == list(reversed(runs))
    assert store.query("Q", sk_prefix="NOPE") == []
    assert store.query("nobody") == []


def test_the_memory_table_limits_and_pages_after_ordering():
    """moto 5.0.0 applies ``Limit`` in storage order before sorting, so this pin is memory-only."""
    store = StateStore("")
    runs = [{"pk": "Q", "sk": f"RUN#{i:03d}", "i": i} for i in range(120)]
    for i in range(120):
        assert store.put_new(runs[(i * 37) % 120]) is True
    newest_first = list(reversed(runs))
    assert store.query("Q", sk_prefix="RUN#", limit=50) == runs[:50]
    assert store.query("Q", sk_prefix="RUN#", forward=False, limit=50) == newest_first[:50]
    assert store.query("Q", sk_prefix="RUN#", limit=110) == runs[:110]
    assert store.query("Q", sk_prefix="RUN#", forward=False, limit=110) == newest_first[:110]
    assert store.query("Q") == runs
    assert store.query("Q", forward=False) == newest_first


def test_scan_prefix(store):
    rows = [
        {"pk": "GUARDRAIL#a", "sk": "META", "n": 1},
        {"pk": "GUARDRAIL#b", "sk": "META", "n": 2},
        {"pk": "OTHER#c", "sk": "META", "n": 3},
    ]
    for row in rows:
        assert store.put_new(row) is True
    found = sorted(store.scan_prefix("GUARDRAIL#"), key=lambda r: r["pk"])
    assert found == rows[:2]
    assert store.scan_prefix("ZZZ") == []
    assert sorted(store.scan_prefix(""), key=lambda r: r["pk"]) == rows


def test_numbers_read_back_as_decimal_on_every_read_path(store):
    row = {"pk": "N", "sk": "1", "n": 7, "doc": {"m": 3, "l": [1, "x"]}}
    assert store.put_new(row) is True
    read = store.get(("N", "1"))
    assert read == row
    assert isinstance(read["n"], Decimal)
    assert isinstance(read["doc"]["m"], Decimal)
    assert isinstance(read["doc"]["l"][0], Decimal)
    assert isinstance(store.query("N")[0]["n"], Decimal)
    assert isinstance(store.scan_prefix("N")[0]["n"], Decimal)
    assert isinstance(store.update_existing(("N", "1"), {"n": 8})["n"], Decimal)
    assert isinstance(store.get(("N", "1"))["n"], Decimal)
    store.set_owned(("N", "2"), {"n": 9})
    assert isinstance(store.get(("N", "2"))["n"], Decimal)
    assert isinstance(store.delete(("N", "1"))["n"], Decimal)


def test_a_float_or_an_empty_set_is_refused(store):
    key = ("F", "1")
    with pytest.raises(TypeError):
        store.put_new({"pk": "F", "sk": "1", "t": 1.5})
    assert store.get(key) is None
    row = {"pk": "F", "sk": "1", "t": 1, "row_version": 1}
    assert store.put_new(row) is True
    with pytest.raises(TypeError):
        store.put_if_version({**row, "doc": {"t": 1.5}, "row_version": 2}, "row_version", 1)
    with pytest.raises(TypeError):
        store.set_owned(key, {"t": 2.5})
    with pytest.raises(TypeError):
        store.update_existing(key, {"t": 2.5})
    assert store.get(key) == row
    for item in ({"pk": "F", "sk": "2", "tags": set()}, {"pk": "F", "sk": "2", "doc": {"tags": set()}}):
        with pytest.raises(ClientError) as info:
            store.put_new(item)
        assert info.value.response["Error"]["Code"] == "ValidationException"
    assert store.get(("F", "2")) is None


def test_twenty_threads_put_new_the_same_key_and_one_wins_on_the_memory_double():
    """moto 5.0.0's ``Table.put_item`` reads the current item, evaluates the condition and assigns the
    row with no lock between the three, so a race under moto has several winners and says nothing about
    our code; the memory double's one lock around the check and the write is what this proves, and
    ``test_put_new_writes_once`` proves moto's conditional single-threaded on both doubles."""
    store = StateStore("")
    threads = 20
    barrier = threading.Barrier(threads)

    def race(i: int) -> bool:
        barrier.wait()
        return store.put_new({"pk": "T", "sk": "1", "winner": i})

    with ThreadPoolExecutor(max_workers=threads) as pool:
        results = list(pool.map(race, range(threads)))
    winners = [i for i, won in enumerate(results) if won]
    assert len(winners) == 1
    assert store.get(("T", "1")) == {"pk": "T", "sk": "1", "winner": winners[0]}


class _Reentering(ConditionBase):
    def __init__(self, inner: ConditionBase, hook: Callable[[], None]) -> None:
        super().__init__(inner)
        self.has_grouped_values = inner.has_grouped_values
        self._inner = inner
        self._hook = hook

    def get_expression(self) -> dict:
        self._hook()
        return self._inner.get_expression()


@pytest.mark.parametrize("store", ["moto"], indirect=True)
def test_a_call_from_another_thread_mid_build_leaves_a_two_attribute_condition_intact(store):
    """boto3 builds a condition's placeholders on the ``Table`` resource, resetting its counters at the
    start of every call, so one resource shared by threads can name one attribute twice; the memory
    double evaluates the builder objects itself under its row lock, where a re-entrant read from another
    thread would wait forever, so the pin runs on moto alone."""
    seed(store, {"pk": "T", "sk": "1", "state": "X", "job": "Y"})
    seed(store, {"pk": "T", "sk": "2", "n": 1})
    reads: list[dict | None] = []

    def elsewhere() -> None:
        reader = threading.Thread(target=lambda: reads.append(store.get(("T", "2"))))
        reader.start()
        reader.join()

    original = state_store._from_set

    def from_set(attr, values):
        clause = original(attr, values)
        return _Reentering(clause, elsewhere) if attr == "job" else clause

    with patch.object(state_store, "_from_set", from_set):
        written = store.transition(
            {"pk": "T", "sk": "1", "state": "A", "job": "Y"}, {"state": {"A"}, "job": {"Y"}}
        )
    assert written is False
    assert store.get(("T", "1")) == {"pk": "T", "sk": "1", "state": "X", "job": "Y"}
    assert reads == [{"pk": "T", "sk": "2", "n": 1}]


def test_a_configured_name_never_selects_the_memory_table():
    store = StateStore("x", table=_Unreachable())
    assert store.in_memory is False
    with pytest.raises(ClientError) as info:
        store.get(("a", "b"))
    assert info.value.response["Error"]["Code"] == "ResourceNotFoundException"
    assert isinstance(info.value, STORE_FAULTS)
    assert STORE_FAULTS == (ClientError, BotoCoreError)
    with pytest.raises(ClientError):
        store.put_new({"pk": "a", "sk": "b"})
    with pytest.raises(ClientError):
        store.put_if_version({"pk": "a", "sk": "b", "row_version": 1}, "row_version", None)
    with pytest.raises(ClientError):
        store.set_owned(("a", "b"), {"owner": "o"})
    assert StateStore("", table=_Unreachable()).in_memory is False


def test_an_empty_name_builds_no_boto3_client():
    with (
        patch("boto3.resource", side_effect=AssertionError("boto3.resource must not be called")),
        patch("boto3.client", side_effect=AssertionError("boto3.client must not be called")),
    ):
        store = StateStore("")
        assert store.in_memory is True
        assert store.put_new({"pk": "a", "sk": "b"}) is True
        assert store.get(("a", "b")) == {"pk": "a", "sk": "b"}


def test_the_memory_table_rejects_an_unknown_keyword():
    table = StateStore("")._table
    with pytest.raises(TypeError):
        table.put_item(Item={"pk": "a", "sk": "b"}, ReturnValues="ALL_OLD")
    with pytest.raises(TypeError):
        table.get_item(Key={"pk": "a", "sk": "b"}, ProjectionExpression="pk")
    with pytest.raises(TypeError):
        table.delete_item(Key={"pk": "a", "sk": "b"}, Expected={})
    with pytest.raises(TypeError):
        table.update_item(Key={"pk": "a", "sk": "b"}, AttributeUpdates={})
    with pytest.raises(TypeError):
        table.query(IndexName="gsi")
    with pytest.raises(TypeError):
        table.scan(Limit=1)
    with pytest.raises(AttributeError):
        table.batch_writer()


def test_importing_the_module_constructs_nothing():
    spec = importlib.util.spec_from_file_location("state_store_fresh_import", Path(state_store.__file__))
    assert spec is not None and spec.loader is not None
    fresh = importlib.util.module_from_spec(spec)
    with (
        patch("boto3.resource", side_effect=AssertionError("boto3.resource must not be called")),
        patch("boto3.client", side_effect=AssertionError("boto3.client must not be called")),
    ):
        spec.loader.exec_module(fresh)
    assert fresh.BY_DESIGN_UNCONDITIONAL == BY_DESIGN_UNCONDITIONAL == frozenset({"set_owned"})
    assert repr(fresh.ABSENT) == "ABSENT"
