"""One way to reach a DynamoDB table. Every table in the platform is keyed ``(pk, sk)``, and every
write but ``set_owned`` carries its condition, so a caller writes under one or names what it owns.

``StateStore(table_name, *, region=None, table=None, clock=time.time)`` selects its table once: an
injected ``table`` is used as given; an empty ``table_name`` builds the private memory double; any other
name is ``core.aws_clients.dynamodb_table(table_name, region=region)``, built at construction
on the constructing thread and on every other thread's first call, never shared across
threads, because a boto3 resource is not thread-safe: its condition-expression builder
resets its placeholder counters at the start of every call, so one resource under concurrent
callers names one attribute twice in a condition. A configured name means DynamoDB, with no
try/except: a fault surfaces where the store is built, or at another thread's first call, like
every other client's.
Conditions are ``boto3.dynamodb.conditions`` builder objects, never strings. ``put_new`` writes under
``attribute_not_exists(sk)``. ``put_if_version`` writes under ``<version_attr> = :expected`` for an int
and, for ``None`` (the row read carries no version yet), under ``attribute_exists(sk) AND
attribute_not_exists(<version_attr>)``, so an absent or deleted row is never recreated; it raises
``VersionMismatch`` when refused. ``transition`` writes the whole
item under, per expected attribute, ``IN (values)`` or'd with ``attribute_not_exists`` when ``ABSENT`` is
in the set, the attributes and'd. ``acquire_lease`` writes ``holder``, ``expires_at`` and ``ttl`` under
``attribute_not_exists(pk) OR expires_at < :now OR holder = :holder``; ``release_lease`` deletes under
``holder = :holder`` and swallows the conditional failure. ``delete`` deletes under ``attribute_exists(sk)``
and returns the old row or ``None``. ``update_existing`` is a ``SET`` of the named attributes under
``attribute_exists(sk)`` and returns the merged row or ``None``; a ``SET`` that names ``pk`` or ``sk``
raises ``ValueError`` before any table call. ``set_owned``, the same ``SET`` as an upsert that returns the
written row, is the one unconditional write, named in ``BY_DESIGN_UNCONDITIONAL`` for the check that reads
this module.
Every ``ClientError`` other than the conditional failure propagates unchanged; ``STORE_FAULTS`` is the
pair the services catch. ``query`` and ``scan_prefix`` page over ``LastEvaluatedKey``.

The memory double evaluates the same builder objects, parses only the store's ``SET`` grammar, raises the
real ``ConditionalCheckFailedException``, stores every value through boto3's ``TypeSerializer`` and
``TypeDeserializer`` (a float raises ``TypeError``, an empty set the ``ValidationException`` DynamoDB
answers, numbers read back as ``Decimal``), deep-copies rows out, pages, and accepts no keyword the store
does not send, so a new idiom fails in memory before it reaches DynamoDB. The conformance test runs every
primitive against the double and against moto.

Decision: docs/adr/backend-state-store-conditional-writes.md
"""

from __future__ import annotations

import copy
import functools
import operator
import re
import threading
import time
from collections.abc import Callable, Collection, Mapping
from typing import Any, Final

from boto3.dynamodb.conditions import Attr, ConditionBase
from boto3.dynamodb.conditions import Key as KeyAttr
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from core import aws_clients

STORE_FAULTS: Final = (ClientError, BotoCoreError)
BY_DESIGN_UNCONDITIONAL: Final = frozenset({"set_owned"})
Key = tuple[str, str]

_CONDITIONAL_FAILURE = "ConditionalCheckFailedException"
_PAGE = 100
_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()
_SET_GRAMMAR = re.compile(r"^SET #\w+ = :\w+(, #\w+ = :\w+)*$")
_ASSIGNMENT = re.compile(r"(#\w+) = (:\w+)")


class _Absent:
    def __repr__(self) -> str:
        return "ABSENT"


ABSENT: Final = _Absent()


class VersionMismatch(Exception):
    """The row is not at the version the caller read."""


class StateStore:
    def __init__(
        self,
        table_name: str,
        *,
        region: str | None = None,
        table: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._name = table_name
        self._region = region
        self._threads = threading.local()
        self._shared: Any | None = table
        if table is None and table_name == "":
            self._shared = _MemoryTable()
        if self._shared is None:
            self._bind()

    def _bind(self) -> None:
        self._threads.table = aws_clients.dynamodb_table(self._name, region=self._region)

    @property
    def _table(self) -> Any:
        if self._shared is not None:
            return self._shared
        if not hasattr(self._threads, "table"):
            self._bind()
        return self._threads.table

    @property
    def in_memory(self) -> bool:
        return isinstance(self._shared, _MemoryTable)

    def get(self, key: Key, *, consistent: bool = False) -> dict | None:
        return self._table.get_item(Key=_key_item(key), ConsistentRead=consistent).get("Item")

    def query(
        self,
        pk: str,
        *,
        sk_prefix: str | None = None,
        forward: bool = True,
        limit: int | None = None,
        consistent: bool = False,
    ) -> list[dict]:
        condition = KeyAttr("pk").eq(pk)
        if sk_prefix:
            condition = condition & KeyAttr("sk").begins_with(sk_prefix)
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": condition,
            "ScanIndexForward": forward,
            "ConsistentRead": consistent,
        }
        return _pages(self._table.query, kwargs, limit)

    def scan_prefix(self, pk_prefix: str) -> list[dict]:
        kwargs: dict[str, Any] = {"FilterExpression": Attr("pk").begins_with(pk_prefix)} if pk_prefix else {}
        return _pages(self._table.scan, kwargs, None)

    def put_new(self, item: dict) -> bool:
        return self._put_if(item, Attr("sk").not_exists())

    def put_if_version(self, item: dict, version_attr: str, expected: int | None) -> None:
        if expected is None:
            condition = Attr("sk").exists() & Attr(version_attr).not_exists()
            expectation = f"absent or already carries {version_attr}"
        else:
            condition = Attr(version_attr).eq(expected)
            expectation = f"not at {version_attr} {expected}"
        if not self._put_if(item, condition):
            raise VersionMismatch(f"{item.get('pk')}/{item.get('sk')} is {expectation}")

    def transition(self, item: dict, expected: Mapping[str, Collection[str | _Absent]]) -> bool:
        return self._put_if(item, _transition_condition(expected))

    def set_owned(self, key: Key, attrs: Mapping[str, Any]) -> dict:
        """Unconditional by the caller's declaration: it owns ``attrs`` alone on a row other writers share
        (disjoint ownership), or the row is one whose single writer owns every attribute."""
        response = self._table.update_item(
            Key=_key_item(key),
            ReturnValues="ALL_NEW",
            **_set_expression(attrs),
        )
        return response["Attributes"]

    def update_existing(self, key: Key, attrs: Mapping[str, Any]) -> dict | None:
        try:
            response = self._table.update_item(
                Key=_key_item(key),
                ConditionExpression=Attr("sk").exists(),
                ReturnValues="ALL_NEW",
                **_set_expression(attrs),
            )
        except ClientError as exc:
            if _conditional_failed(exc):
                return None
            raise
        return response.get("Attributes")

    def acquire_lease(self, key: Key, holder: str, ttl_s: int) -> bool:
        now = int(self._clock())
        expires_at = now + ttl_s
        item = {**_key_item(key), "holder": holder, "expires_at": expires_at, "ttl": expires_at}
        free = Attr("pk").not_exists() | Attr("expires_at").lt(now) | Attr("holder").eq(holder)
        return self._put_if(item, free)

    def release_lease(self, key: Key, holder: str) -> None:
        try:
            self._table.delete_item(Key=_key_item(key), ConditionExpression=Attr("holder").eq(holder))
        except ClientError as exc:
            if not _conditional_failed(exc):
                raise

    def delete(self, key: Key) -> dict | None:
        try:
            response = self._table.delete_item(
                Key=_key_item(key), ConditionExpression=Attr("sk").exists(), ReturnValues="ALL_OLD"
            )
        except ClientError as exc:
            if _conditional_failed(exc):
                return None
            raise
        return response.get("Attributes")

    def _put_if(self, item: dict, condition: ConditionBase) -> bool:
        try:
            self._table.put_item(Item=item, ConditionExpression=condition)
        except ClientError as exc:
            if _conditional_failed(exc):
                return False
            raise
        return True


def _conditional_failed(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == _CONDITIONAL_FAILURE


def _key_item(key: Key) -> dict[str, str]:
    pk, sk = key
    return {"pk": pk, "sk": sk}


def _as_key(item: Mapping[str, Any]) -> Key:
    return item["pk"], item["sk"]


def _transition_condition(expected: Mapping[str, Collection[str | _Absent]]) -> ConditionBase:
    clauses = [_from_set(attr, values) for attr, values in expected.items()]
    if not clauses:
        raise ValueError("transition needs at least one expected attribute")
    return functools.reduce(operator.and_, clauses)


def _from_set(attr: str, values: Collection[str | _Absent]) -> ConditionBase:
    present = [value for value in values if value is not ABSENT]
    absent = any(value is ABSENT for value in values)
    if present and absent:
        return Attr(attr).is_in(present) | Attr(attr).not_exists()
    if present:
        return Attr(attr).is_in(present)
    if absent:
        return Attr(attr).not_exists()
    raise ValueError(f"transition from no {attr} value can never write")


def _set_expression(attrs: Mapping[str, Any]) -> dict[str, Any]:
    if not attrs:
        raise ValueError("a SET needs at least one attribute")
    if "pk" in attrs or "sk" in attrs:
        raise ValueError("a SET never names pk or sk; the key is the row's identity")
    return {
        "UpdateExpression": "SET " + ", ".join(f"#a{i} = :a{i}" for i in range(len(attrs))),
        "ExpressionAttributeNames": {f"#a{i}": name for i, name in enumerate(attrs)},
        "ExpressionAttributeValues": {f":a{i}": value for i, value in enumerate(attrs.values())},
    }


def _pages(call: Callable[..., dict], kwargs: dict[str, Any], limit: int | None) -> list[dict]:
    rows: list[dict] = []
    while True:
        if limit is not None:
            kwargs["Limit"] = limit - len(rows)
        response = call(**kwargs)
        rows.extend(response.get("Items", []))
        last = response.get("LastEvaluatedKey")
        if not last or (limit is not None and len(rows) >= limit):
            return rows
        kwargs["ExclusiveStartKey"] = last


_COMPARISONS: dict[str, Callable[[Any, Any], bool]] = {
    "=": operator.eq,
    "<": operator.lt,
    "IN": lambda actual, values: actual in values,
    "begins_with": lambda actual, prefix: isinstance(actual, str) and actual.startswith(prefix),
}


def _holds(condition: Any, row: Mapping[str, Any]) -> bool:
    expression = condition.get_expression()
    op, values = expression["operator"], expression["values"]
    if op == "AND":
        return _holds(values[0], row) and _holds(values[1], row)
    if op == "OR":
        return _holds(values[0], row) or _holds(values[1], row)
    name = values[0].name
    if op == "attribute_exists":
        return name in row
    if op == "attribute_not_exists":
        return name not in row
    compare = _COMPARISONS.get(op)
    if compare is None:
        raise TypeError(f"the memory table does not evaluate {op}")
    return name in row and compare(row[name], values[1])


def _check(condition: Any, current: Mapping[str, Any] | None, operation: str) -> None:
    if condition is not None and not _holds(condition, current or {}):
        raise ClientError(
            {"Error": {"Code": _CONDITIONAL_FAILURE, "Message": "The conditional request failed"}}, operation
        )


def _assignments(expression: str, names: Mapping[str, str], values: Mapping[str, Any]) -> dict[str, Any]:
    if not _SET_GRAMMAR.match(expression):
        raise TypeError(f"the memory table parses only the store's SET grammar, not {expression!r}")
    return {names[name]: values[value] for name, value in _ASSIGNMENT.findall(expression)}


def _stored(item: Mapping[str, Any], operation: str) -> dict[str, Any]:
    wire = {name: _SERIALIZER.serialize(value) for name, value in item.items()}
    for value in wire.values():
        _refuse_empty_sets(value, operation)
    return {name: _DESERIALIZER.deserialize(value) for name, value in wire.items()}


def _refuse_empty_sets(wire: Mapping[str, Any], operation: str) -> None:
    kind, inner = next(iter(wire.items()))
    if kind in ("SS", "NS", "BS") and not inner:
        raise ClientError(
            {"Error": {"Code": "ValidationException", "Message": "a set may not be empty"}}, operation
        )
    for nested in inner.values() if kind == "M" else inner if kind == "L" else ():
        _refuse_empty_sets(nested, operation)


def _page(rows: list[dict], limit: int | None, start: Mapping[str, Any] | None) -> dict[str, Any]:
    if start is not None:
        rows = rows[[_as_key(row) for row in rows].index(_as_key(start)) + 1 :]
    size = min(x for x in (limit, _PAGE) if x is not None)
    page, rest = rows[:size], rows[size:]
    response: dict[str, Any] = {"Items": [copy.deepcopy(row) for row in page]}
    if rest:
        response["LastEvaluatedKey"] = _key_item(_as_key(page[-1]))
    return response


class _MemoryTable:
    def __init__(self) -> None:
        self._rows: dict[Key, dict] = {}
        self._lock = threading.Lock()

    def get_item(self, Key: Mapping[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:
        with self._lock:
            row = self._rows.get(_as_key(Key))
        return {"Item": copy.deepcopy(row)} if row is not None else {}

    def put_item(self, Item: Mapping[str, Any], ConditionExpression: Any = None) -> dict[str, Any]:
        stored = _stored(Item, "PutItem")
        with self._lock:
            key = _as_key(stored)
            _check(ConditionExpression, self._rows.get(key), "PutItem")
            self._rows[key] = stored
        return {}

    def update_item(
        self,
        Key: Mapping[str, Any],
        UpdateExpression: str,
        ExpressionAttributeNames: Mapping[str, str],
        ExpressionAttributeValues: Mapping[str, Any],
        ConditionExpression: Any = None,
        ReturnValues: str = "NONE",
    ) -> dict[str, Any]:
        assigned = _stored(
            _assignments(UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues), "UpdateItem"
        )
        with self._lock:
            key = _as_key(Key)
            current = self._rows.get(key)
            _check(ConditionExpression, current, "UpdateItem")
            row = {**(copy.deepcopy(current) if current is not None else dict(Key)), **assigned}
            self._rows[key] = row
            return {"Attributes": copy.deepcopy(row)} if ReturnValues == "ALL_NEW" else {}

    def delete_item(
        self, Key: Mapping[str, Any], ConditionExpression: Any = None, ReturnValues: str = "NONE"
    ) -> dict[str, Any]:
        with self._lock:
            key = _as_key(Key)
            current = self._rows.get(key)
            _check(ConditionExpression, current, "DeleteItem")
            self._rows.pop(key, None)
        if ReturnValues == "ALL_OLD" and current is not None:
            return {"Attributes": copy.deepcopy(current)}
        return {}

    def query(
        self,
        KeyConditionExpression: Any,
        ScanIndexForward: bool = True,
        Limit: int | None = None,
        ExclusiveStartKey: Mapping[str, Any] | None = None,
        ConsistentRead: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            rows = sorted(
                (row for row in self._rows.values() if _holds(KeyConditionExpression, row)),
                key=lambda row: row["sk"],
                reverse=not ScanIndexForward,
            )
        return _page(rows, Limit, ExclusiveStartKey)

    def scan(
        self, FilterExpression: Any = None, ExclusiveStartKey: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        with self._lock:
            rows = [
                row
                for _key, row in sorted(self._rows.items())
                if FilterExpression is None or _holds(FilterExpression, row)
            ]
        return _page(rows, None, ExclusiveStartKey)
