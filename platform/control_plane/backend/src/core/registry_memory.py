"""The agent and MCP registries served from this process's memory, selected by ``REGISTRY_IN_MEMORY=true``
inside the registry provider of ``core.aws_clients`` and by nothing else.

``MemoryRegistryClient(registries=..., region=..., clock=..., auto_approve=False)`` answers the eight
members the registry writer calls (``MEMBERS``) and raises ``AttributeError`` naming
them and the switch on any other public attribute. ``get_paginator("list_registries").paginate()`` yields
one page naming every configured registry as ``READY``; the seven record operations keep each record in the
flat ``GetRegistryRecord`` shape under ``_records[registryId][recordId]``, deep-copied on every read, with
timezone-aware ``datetime`` timestamps. Every operation first runs botocore's ``ParamValidator`` over its
keyword arguments against the ``agent-registry-control`` model in the venv and raises
``ParamValidationError`` as boto3 does; an unknown registry or record raises ``ClientError``
``ResourceNotFoundException``; ``APPROVED`` requested from any status but ``PENDING_APPROVAL``, a status
the API does not transition to, or a filter the API does not offer raises ``ClientError``
``ValidationException``. ``ConflictException`` is never raised: no mutation is refused for timing.

A write answers its transient status and the settled one applies on the next read of that record,
through ``get_registry_record`` or ``list_registry_records``: a create answers ``CREATING`` and settles
to ``DRAFT``; an update answers ``UPDATING`` and settles to ``DRAFT`` whatever the status was, the reset
the platform's re-approval re-asserts from, storing the PATCH envelope flat (``optionalValue`` levels
removed, ``data`` strings re-serialised compact and sorted) and leaving every key the request omits.
Each settle or mutation moves ``updatedAt`` strictly later than the record's last timestamp. A submit
lands on ``PENDING_APPROVAL``, or ``APPROVED`` under ``auto_approve``; ``REJECTED`` and ``DEPRECATED`` are
accepted from any status; a submit, a status change or a delete clears a pending settle. A repeated
``clientToken`` in a registry answers the existing record and writes nothing. The pending settles live in
``_pending``, keyed ``(registryId, recordId)``, never inside a record. One ``RLock`` guards every member.

``memory_registry_client(region)`` builds one instance over the distinct values of
``AGENT_REGISTRY_NAME`` and ``MCP_REGISTRY_NAME`` on the first call, under a module lock, and answers it
ever after, so every service built in the process shares one registry state.

Decision: docs/adr/backend-registry-in-memory-behind-the-provider.md
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timedelta, timezone
from secrets import token_hex
from typing import Any, Final

import botocore.session
from botocore.exceptions import ClientError, ParamValidationError
from botocore.validate import ParamValidator

from core.config import settings

MEMBERS: Final = (
    "get_paginator",
    "list_registry_records",
    "get_registry_record",
    "create_registry_record",
    "update_registry_record",
    "delete_registry_record",
    "submit_registry_record_for_approval",
    "update_registry_record_status",
)

_SERVICE: Final = "agent-registry-control"
_ACCOUNT: Final = "000000000000"
_MICROSECOND: Final = timedelta(microseconds=1)
_FILTERS: Final = frozenset({"name", "recordType", "status"})
_REQUESTABLE: Final = frozenset({"APPROVED", "REJECTED", "DEPRECATED"})
_SUMMARY_KEYS: Final = (
    "registryArn",
    "recordArn",
    "recordId",
    "name",
    "displayName",
    "description",
    "recordType",
    "recordVersion",
    "status",
    "createdAt",
    "updatedAt",
)
_SUBMIT_KEYS: Final = ("registryArn", "recordArn", "recordId", "status", "updatedAt")
_STATUS_KEYS: Final = ("registryArn", "recordArn", "recordId", "status", "statusReason", "updatedAt")

_MODEL: Any = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _model() -> Any:
    global _MODEL
    if _MODEL is None:
        _MODEL = botocore.session.get_session().get_service_model(_SERVICE)
    return _MODEL


def _validate(operation: str, params: dict[str, Any]) -> None:
    report = ParamValidator().validate(params, _model().operation_model(operation).input_shape)
    if report.has_errors():
        raise ParamValidationError(report=report.generate_report())


def _not_found(operation: str, message: str) -> ClientError:
    return ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": message}}, operation)


def _validation(operation: str, message: str) -> ClientError:
    return ClientError({"Error": {"Code": "ValidationException", "Message": message}}, operation)


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"optionalValue"}:
            return _unwrap(value["optionalValue"])
        return {key: _unwrap(inner) for key, inner in value.items()}
    return value


def _reformat(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                json.dumps(json.loads(inner), sort_keys=True, separators=(",", ":"))
                if key == "data" and isinstance(inner, str)
                else _reformat(inner)
            )
            for key, inner in value.items()
        }
    return value


class _ListRegistriesPaginator:
    def __init__(self, registries: list[dict[str, Any]]) -> None:
        self._registries = registries

    def paginate(self) -> Iterator[dict[str, Any]]:
        yield {"registries": copy.deepcopy(self._registries)}


class MemoryRegistryClient:
    def __init__(
        self,
        *,
        registries: Sequence[str],
        region: str,
        clock: Callable[[], datetime] = _utcnow,
        auto_approve: bool = False,
    ) -> None:
        self._clock = clock
        self._auto_approve = auto_approve
        self._lock = threading.RLock()
        self._registries: dict[str, dict[str, Any]] = {}
        self._records: dict[str, dict[str, dict[str, Any]]] = {}
        self._tokens: dict[tuple[str, str], str] = {}
        self._pending: dict[tuple[str, str], str] = {}
        for name in registries:
            registry_id = hashlib.sha256(name.encode()).hexdigest()[:12]
            now = clock()
            self._registries[registry_id] = {
                "name": name,
                "registryId": registry_id,
                "registryArn": f"arn:aws:agent-registry:{region}:{_ACCOUNT}:registry/{registry_id}",
                "status": "READY",
                "createdAt": now,
                "updatedAt": now,
            }
            self._records[registry_id] = {}

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        raise AttributeError(
            f"{name} is not one of the eight registry operations the in-memory registry answers "
            f"({', '.join(MEMBERS)}); it serves REGISTRY_IN_MEMORY=true and nothing else"
        )

    def get_paginator(self, operation_name: str) -> _ListRegistriesPaginator:
        if operation_name != "list_registries":
            raise ValueError("only list_registries is pageable here")
        with self._lock:
            return _ListRegistriesPaginator(list(self._registries.values()))

    def _registry(self, operation: str, registry_id: str) -> dict[str, Any]:
        registry = self._registries.get(registry_id)
        if registry is None:
            raise _not_found(operation, f"Registry {registry_id} not found")
        return registry

    def _record(self, operation: str, registry_id: str, record_id: str) -> dict[str, Any]:
        self._registry(operation, registry_id)
        record = self._records[registry_id].get(record_id)
        if record is None:
            raise _not_found(operation, f"Record {record_id} not found in registry {registry_id}")
        return record

    def _touch(self, record: dict[str, Any]) -> None:
        record["updatedAt"] = max(self._clock(), record["updatedAt"] + _MICROSECOND)

    def _settle(self, registry_id: str, record: dict[str, Any]) -> None:
        pending = self._pending.pop((registry_id, record["recordId"]), None)
        if pending is not None:
            record["status"] = pending
            self._touch(record)

    def list_registry_records(self, **params: Any) -> dict[str, Any]:
        operation = "ListRegistryRecords"
        _validate(operation, params)
        with self._lock:
            registry_id = params["registryId"]
            self._registry(operation, registry_id)
            records = self._records[registry_id]
            for record in records.values():
                self._settle(registry_id, record)
            wanted = {item["name"]: item["values"][0] for item in params.get("filters", [])}
            unknown = sorted(set(wanted) - _FILTERS)
            if unknown:
                raise _validation(
                    operation, f"Unknown filter {', '.join(unknown)}; filters are {sorted(_FILTERS)}"
                )
            matching = sorted(
                (r for r in records.values() if all(r.get(key) == value for key, value in wanted.items())),
                key=lambda r: (r["createdAt"], r["recordId"]),
            )
            try:
                start = int(params.get("nextToken", "0"))
            except ValueError as err:
                raise _validation(operation, "Invalid nextToken") from err
            end = start + params.get("maxResults", 100)
            answer: dict[str, Any] = {
                "registryRecords": [
                    copy.deepcopy({k: r[k] for k in _SUMMARY_KEYS if k in r}) for r in matching[start:end]
                ]
            }
            if end < len(matching):
                answer["nextToken"] = str(end)
            return answer

    def get_registry_record(self, **params: Any) -> dict[str, Any]:
        operation = "GetRegistryRecord"
        _validate(operation, params)
        with self._lock:
            record = self._record(operation, params["registryId"], params["recordId"])
            self._settle(params["registryId"], record)
            return copy.deepcopy(record)

    def create_registry_record(self, **params: Any) -> dict[str, Any]:
        operation = "CreateRegistryRecord"
        _validate(operation, params)
        with self._lock:
            registry = self._registry(operation, params["registryId"])
            registry_id = registry["registryId"]
            token = params.get("clientToken")
            existing = self._tokens.get((registry_id, token)) if token else None
            if existing is not None:
                record = self._records[registry_id][existing]
                return {"recordArn": record["recordArn"], "status": record["status"]}
            record_id = token_hex(6)
            now = self._clock()
            record = {
                "registryArn": registry["registryArn"],
                "recordArn": f"{registry['registryArn']}/record/{record_id}",
                "recordId": record_id,
                "name": params["name"],
                "recordType": params["recordType"],
                "descriptors": copy.deepcopy(params["descriptors"]),
                "status": "CREATING",
                "createdAt": now,
                "updatedAt": now,
                "statusReason": "",
            }
            for key in ("displayName", "description", "recordVersion"):
                if key in params:
                    record[key] = params[key]
            self._records[registry_id][record_id] = record
            if token:
                self._tokens[(registry_id, token)] = record_id
            self._pending[(registry_id, record_id)] = "DRAFT"
            return {"recordArn": record["recordArn"], "status": "CREATING"}

    def update_registry_record(self, **params: Any) -> dict[str, Any]:
        operation = "UpdateRegistryRecord"
        _validate(operation, params)
        with self._lock:
            registry_id = params["registryId"]
            record = self._record(operation, registry_id, params["recordId"])
            for key in ("name", "recordVersion"):
                if key in params:
                    record[key] = params[key]
            for key in ("displayName", "description"):
                if key in params:
                    record[key] = params[key]["optionalValue"]
            if "descriptors" in params:
                record["descriptors"] = _reformat(_unwrap(params["descriptors"]))
            record["status"] = "UPDATING"
            self._touch(record)
            self._pending[(registry_id, record["recordId"])] = "DRAFT"
            return copy.deepcopy(record)

    def delete_registry_record(self, **params: Any) -> dict[str, Any]:
        operation = "DeleteRegistryRecord"
        _validate(operation, params)
        with self._lock:
            registry_id = params["registryId"]
            record = self._record(operation, registry_id, params["recordId"])
            record_id = record["recordId"]
            del self._records[registry_id][record_id]
            self._pending.pop((registry_id, record_id), None)
            self._tokens = {
                key: held
                for key, held in self._tokens.items()
                if not (key[0] == registry_id and held == record_id)
            }
            return {}

    def submit_registry_record_for_approval(self, **params: Any) -> dict[str, Any]:
        operation = "SubmitRegistryRecordForApproval"
        _validate(operation, params)
        with self._lock:
            registry_id = params["registryId"]
            record = self._record(operation, registry_id, params["recordId"])
            self._pending.pop((registry_id, record["recordId"]), None)
            record["status"] = "APPROVED" if self._auto_approve else "PENDING_APPROVAL"
            self._touch(record)
            return {key: record[key] for key in _SUBMIT_KEYS}

    def update_registry_record_status(self, **params: Any) -> dict[str, Any]:
        operation = "UpdateRegistryRecordStatus"
        _validate(operation, params)
        with self._lock:
            registry_id = params["registryId"]
            record = self._record(operation, registry_id, params["recordId"])
            requested = params["status"]
            refused = requested not in _REQUESTABLE or (
                requested == "APPROVED" and record["status"] != "PENDING_APPROVAL"
            )
            if refused:
                raise _validation(
                    operation, f"Invalid status transition from {record['status']} to {requested}"
                )
            self._pending.pop((registry_id, record["recordId"]), None)
            record["status"] = requested
            record["statusReason"] = params["statusReason"]
            self._touch(record)
            return {key: record[key] for key in _STATUS_KEYS}


_INSTANCE: MemoryRegistryClient | None = None
_INSTANCE_LOCK = threading.Lock()


def memory_registry_client(region: str) -> MemoryRegistryClient:
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            names = list(dict.fromkeys((settings.AGENT_REGISTRY_NAME, settings.MCP_REGISTRY_NAME)))
            _INSTANCE = MemoryRegistryClient(registries=names, region=region)
        return _INSTANCE
