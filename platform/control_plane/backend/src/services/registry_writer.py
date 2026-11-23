"""Under ``src/`` every ``agent-registry-control`` operation goes through :class:`RegistryWriter`, which
alone obtains the client; the bootstrap script paginates its own client and passes the pages.

The two registry services each hold one writer (``kind`` ``"agent"`` or ``"mcp"``) and hand it their
injectable ``control_client``; the writer calls ``core.aws_clients.registry_control_client`` when none
is given. :class:`RecordContent` is a record's governed content in the flat create shape and owns both
wire shapes: ``create_params`` for ``CreateRegistryRecord``, ``update_params`` for the PATCH envelope of
``UpdateRegistryRecord``, where every mutable field and every descriptor level is wrapped in
``{"optionalValue": ...}``.

The registry resets a record APPROVED -> DRAFT on any ``UpdateRegistryRecord``. :meth:`RegistryWriter.write`
is the platform write (identity, Cedar, marketplace, the publish flag): it reads the settled record,
makes no call when the content already equals it, writes, and puts a record that was APPROVED back to
APPROVED through ``SubmitRegistryRecordForApproval`` then ``UpdateRegistryRecordStatus(APPROVED)``,
keeping the human approver's reason. Once the content write landed it never raises: a failed
re-approval is logged, naming the record, and the true status is returned.
:meth:`RegistryWriter.write_content` is a user's governed edit: it writes and leaves the record DRAFT.

Race guard: before each re-assert step the writer re-reads the record, waits until it is out of
UPDATING/CREATING, and checks the content is still what it wrote (PATCH envelopes stripped, ``data``
blobs compared as parsed JSON). Different means someone else wrote in between, and the writer stops
rather than approve content nobody reviewed. ``updatedAt`` only skips a read older than the writer's
last call, because the registry's asynchronous settle stamps a newer one than the write's response. A
write landing between that read and the next call is not caught.

One deadline, ``REAPPROVAL_BUDGET_S`` from the start of each write, bounds its ``ConflictException``
retry, its pre-read and its re-assert, so two platform writes in one request add at most twice that,
under the 30 s API Gateway cap. The calls are sync boto3 plus sleeps; async callers off-load with
``anyio.to_thread.run_sync``.
Decision: docs/adr/backend-registry-writer-owner.md
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, NamedTuple

from botocore.exceptions import ClientError

from core.aws_clients import registry_control_client
from core.registry_resolver import (
    RegistryNotConfiguredError,
    RegistryNotFoundError,
    find_registry_by_name,
)

logger = logging.getLogger(__name__)

APPROVED: Final = "APPROVED"
REAPPROVAL_BUDGET_S: Final = 6.0
REASSERT_REASON: Final = (
    "Re-approved by AGP after a platform write (identity, policy, marketplace or build metadata)"
)
_REASON_SUFFIX: Final = " [re-approved by AGP after a platform write]"
_STATUS_REASON_MAX: Final = 255
_TRANSIENT: Final = frozenset({"CREATING", "UPDATING"})
_STABLE: Final = frozenset({"DRAFT", "PENDING_APPROVAL", APPROVED, "REJECTED", "DEPRECATED"})
_CONTENT_KEYS: Final = ("name", "displayName", "description", "descriptors")
_DELAY: Final = 0.5
_DRAFT_POLL_S: Final = 5.0

_monotonic = time.monotonic
_sleep = time.sleep


class _Kind(NamedTuple):
    service: str
    setting: str
    label: str


_KINDS: Final = {
    "agent": _Kind("AgentRegistryService", "AGENT", "agent"),
    "mcp": _Kind("McpServerRegistryService", "MCP", "MCP"),
}


@dataclass(frozen=True)
class RecordContent:
    """A record's governed content in the flat create shape. ``descriptors`` is
    ``{"custom": {"data": ...}}`` or ``{"mcpServer": {...}}``; ``displayName`` is always ``name``,
    because reads prefer ``displayName`` and a rename that patched ``name`` alone would revert."""

    name: str
    description: str
    descriptors: Mapping[str, Any]
    record_version: str = "1.0.0"

    def create_params(self) -> dict:
        return {
            "name": self.name,
            "displayName": self.name,
            "description": self.description,
            "descriptors": dict(self.descriptors),
            "recordVersion": self.record_version,
        }

    def update_params(self) -> dict:
        """The PATCH envelope ``tests/test_registry_update_param_shape.py`` validates against the
        botocore model: ``name`` and ``recordVersion`` plain, everything else wrapped at every level."""
        return {
            "name": self.name,
            "displayName": {"optionalValue": self.name},
            "description": {"optionalValue": self.description},
            "descriptors": _wrap(self.descriptors),
            "recordVersion": self.record_version,
        }


class WriteOutcome(NamedTuple):
    """What one write did. ``status`` is the record's native status: the final read after a
    re-assert, the write's own response when the pre-read failed, and otherwise the settled status
    the pre-read saw. ``written`` is False when the content already matched and no call was made;
    ``reset_and_recovered`` when the write reset an APPROVED record and the re-assert brought it back;
    ``superseded_by_other_writer`` when the race guard stopped the re-assert."""

    status: str | None
    written: bool
    reset_and_recovered: bool
    superseded_by_other_writer: bool


class _Superseded(Exception):
    """The record's content is no longer what AGP wrote: someone else wrote it."""

    def __init__(self, status: str, key: str | None = None):
        super().__init__(f"record changed by another write at {key!r} (now {status!r})")
        self.status = status
        self.key = key


def stable_status(status: str | None) -> str:
    """A settled status is reported as is; a transient, failed or unknown one (UPDATING, UPDATE_FAILED,
    None) reads as DRAFT."""
    return status if status in _STABLE else "DRAFT"


def _code(err: ClientError) -> str | None:
    return err.response.get("Error", {}).get("Code")


def _is_conflict(err: ClientError) -> bool:
    return _code(err) == "ConflictException"


def _is_not_found(err: ClientError) -> bool:
    return _code(err) == "ResourceNotFoundException"


def _status(resp: Any) -> str | None:
    return resp.get("status") if isinstance(resp, dict) else None


def _ts(value: Any) -> datetime | None:
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value if isinstance(value, datetime) else None


def _updated_at(resp: Any) -> datetime | None:
    return _ts(resp.get("updatedAt")) if isinstance(resp, dict) else None


def _wrap(value: Any) -> dict:
    if isinstance(value, Mapping):
        return {"optionalValue": {key: _wrap(inner) for key, inner in value.items()}}
    return {"optionalValue": value}


def _normal(value: Any, key: str | None = None) -> Any:
    """Strip ``optionalValue`` PATCH envelopes and parse ``data`` JSON blobs, so a sent
    UpdateRegistryRecord field and the GET's flat field compare by meaning, not bytes."""
    if isinstance(value, dict):
        if set(value) == {"optionalValue"}:
            return _normal(value["optionalValue"], key)
        return {k: _normal(v, k) for k, v in value.items()}
    if key == "data" and isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _mismatch(got: Any, want: Any, path: str) -> str | None:
    """The first key path where a field AGP wrote differs in ``got`` (fields the registry
    adds are ignored), or None. Only the key is ever logged, never the values."""
    if isinstance(want, dict):
        if not isinstance(got, dict):
            return path
        for k, v in want.items():
            found = _mismatch(got[k], v, f"{path}.{k}") if k in got else f"{path}.{k}"
            if found:
                return found
        return None
    return None if got == want else path


def _first_mismatch(got: dict, sent: dict) -> str | None:
    for k in _CONTENT_KEYS:
        if k in sent and k in got:
            found = _mismatch(_normal(got[k], k), _normal(sent[k], k), k)
            if found:
                return found
    return None


def _holds(got: Any, want: Any, key: str | None = None) -> bool:
    if isinstance(want, dict) and key != "data":
        return isinstance(got, dict) and all(k in got and _holds(got[k], v, k) for k, v in want.items())
    return got == want


def _reason(prior_reason: str | None) -> str:
    """Keep the human approver's reason (who approved and why); mark AGP's re-approval once."""
    if not prior_reason:
        return REASSERT_REASON
    if prior_reason.endswith(_REASON_SUFFIX):
        return prior_reason[:_STATUS_REASON_MAX]
    return prior_reason[: _STATUS_REASON_MAX - len(_REASON_SUFFIX)] + _REASON_SUFFIX


def _retry_on_conflict(call: Callable[[], Any], deadline: float) -> Any:
    """One registry mutation, retried on ConflictException (a record still UPDATING) until ``deadline``."""
    while True:
        try:
            return call()
        except ClientError as err:
            if not _is_conflict(err) or _monotonic() + _DELAY > deadline:
                raise
            _sleep(_DELAY)


def _landed(call: Callable[[], Any]) -> bool:
    try:
        call()
    except ClientError as err:
        if _is_not_found(err):
            return False
        raise
    return True


def _any_status(status: str) -> bool:
    return True


class RegistryWriter:
    """Every call to one AWS Agent Registry, the agent or the MCP-server one by ``kind``."""

    def __init__(
        self,
        *,
        region: str | None = None,
        registry_id: str = "",
        registry_name: str = "",
        kind: Literal["agent", "mcp"],
        control_client: Any = None,
    ) -> None:
        """``registry_name`` is resolved to the id on first use; ``registry_id`` is an explicit override
        that skips the lookup. Passing neither is a configuration error reported on first use."""
        self._kind = _KINDS[kind]
        self._region = region
        self._registry_id = registry_id
        self._registry_name = registry_name
        self._ctl = control_client or registry_control_client(region)

    @property
    def registry_id(self) -> str:
        """The registry id, resolved from the name through ``ListRegistries`` on first use and memoised;
        only a success is cached, so a transient failure does not poison a process-lifetime singleton.
        No lock: resolution is an idempotent read. AWS-side failures propagate unchanged, because
        reporting them as "not found" would send an operator to create a registry that exists."""
        return self._resolve_registry()

    def _resolve_registry(self) -> str:
        if self._registry_id:
            return self._registry_id
        name = self._registry_name
        if not name:
            setting = self._kind.setting
            raise RegistryNotConfiguredError(
                f"{self._kind.service} was constructed with neither a registry id nor a "
                f"registry name, so it cannot address a registry. Set {setting}_REGISTRY_NAME "
                f"(the normal path — the id is then resolved by name) or {setting}_REGISTRY_ID "
                "(an explicit override)."
            )
        registry_id, _arn = find_registry_by_name(self._ctl.get_paginator("list_registries").paginate(), name)
        if not registry_id:
            region = self._region
            raise RegistryNotFoundError(
                f"No AWS Agent Registry named {name!r} exists in region {region!r}. The "
                "control plane resolves the registry by NAME at first use, so nothing can be "
                "read or written until it exists. Create it with `terraform apply` in "
                "platform/control_plane/infrastructure (it creates both registries), or "
                "directly with `PYTHONPATH=src venv/bin/python scripts/ensure_registry.py "
                f"--name {name} --region {region}`. If the registry lives elsewhere, set "
                "AGENT_REGISTRY_NAME/MCP_REGISTRY_NAME (or pin the id outright with "
                "AGENT_REGISTRY_ID/MCP_REGISTRY_ID)."
            )
        self._registry_id = registry_id
        logger.info(
            "resolved %s registry %r -> %s (region %s)", self._kind.label, name, registry_id, self._region
        )
        return registry_id

    def get(self, record_id: str) -> dict | None:
        """``GetRegistryRecord``; None when the record does not exist."""
        try:
            return self._ctl.get_registry_record(registryId=self.registry_id, recordId=record_id)
        except ClientError as err:
            if _is_not_found(err):
                return None
            raise

    def iter_summaries(
        self, *, record_type: str, status: str | None = None, name: str | None = None
    ) -> Iterator[dict]:
        """``ListRegistryRecords`` over every page, narrowed by the structured filters; summaries omit
        descriptors."""
        filters = [{"name": "name", "values": [name]}] if name else []
        filters.append({"name": "recordType", "values": [record_type]})
        if status:
            filters.append({"name": "status", "values": [status]})
        kwargs: dict[str, Any] = {"registryId": self.registry_id, "maxResults": 100, "filters": filters}
        while True:
            page = self._ctl.list_registry_records(**kwargs)
            yield from page.get("registryRecords", [])
            token = page.get("nextToken")
            if not token:
                return
            kwargs["nextToken"] = token

    def create(self, content: RecordContent, *, record_type: str, client_token: str) -> tuple[str, str]:
        """``CreateRegistryRecord``, then the fresh record polled out of CREATING until DRAFT, so the
        next write does not meet ``ConflictException``. Answers the record id (the ``recordArn`` tail:
        the create returns no id) and the settled status. A ``ValidationException`` propagates; a record
        that has not reached DRAFT within the poll raises ``RuntimeError`` and stays in the registry."""
        resp = self._ctl.create_registry_record(
            registryId=self.registry_id,
            recordType=record_type,
            clientToken=client_token,
            **content.create_params(),
        )
        record_id = resp["recordArn"].rsplit("/", 1)[-1]
        settled = self._read(record_id, _monotonic() + _DRAFT_POLL_S, None, None, lambda s: s == "DRAFT")
        if settled is None or settled["status"] != "DRAFT":
            raise RuntimeError(
                f"Registry record {record_id!r} did not reach DRAFT within {_DRAFT_POLL_S:g} s"
            )
        return record_id, settled["status"]

    def write(self, record_id: str, content: RecordContent) -> WriteOutcome:
        """The platform write: the settled pre-read, no call when the record already holds ``content``
        (each ``data`` blob equal, fields the registry adds elsewhere ignored), the write retried on
        ``ConflictException`` inside the deadline, and APPROVED re-asserted when the record was APPROVED.
        The registry id is resolved first, so a resolution failure raises before any read. Raises what
        the content write raises (for example ``ConflictException`` past the deadline,
        ``ValidationException``, ``ResourceNotFoundException``)."""
        self._resolve_registry()
        deadline = _monotonic() + REAPPROVAL_BUDGET_S
        prior = self._settled(record_id, deadline)
        params = content.update_params()
        sent = {key: params[key] for key in _CONTENT_KEYS}
        if prior is not None and _holds(_normal(prior), _normal(sent)):
            return WriteOutcome(
                status=prior["status"],
                written=False,
                reset_and_recovered=False,
                superseded_by_other_writer=False,
            )
        written = self._update(record_id, params, deadline)
        if prior is None:
            return WriteOutcome(
                status=_status(written),
                written=True,
                reset_and_recovered=False,
                superseded_by_other_writer=False,
            )
        if prior["status"] != APPROVED:
            return WriteOutcome(
                status=prior["status"],
                written=True,
                reset_and_recovered=False,
                superseded_by_other_writer=False,
            )
        return self._reassert(
            record_id, written=written, sent=sent, prior_reason=prior.get("statusReason"), deadline=deadline
        )

    def write_content(self, record_id: str, content: RecordContent) -> WriteOutcome | None:
        """A user's governed edit: the write retried on ``ConflictException`` inside the deadline and
        never re-asserted, so an APPROVED record stays DRAFT; None when the record is gone."""
        self._resolve_registry()
        try:
            written = self._update(record_id, content.update_params(), _monotonic() + REAPPROVAL_BUDGET_S)
        except ClientError as err:
            if _is_not_found(err):
                return None
            raise
        return WriteOutcome(
            status=_status(written), written=True, reset_and_recovered=False, superseded_by_other_writer=False
        )

    def delete(self, record_id: str) -> bool:
        """``DeleteRegistryRecord``; False when the record was already gone."""
        return _landed(
            lambda: self._ctl.delete_registry_record(registryId=self.registry_id, recordId=record_id)
        )

    def submit(self, record_id: str) -> bool:
        """``SubmitRegistryRecordForApproval``; False when the record is gone."""
        return _landed(
            lambda: self._ctl.submit_registry_record_for_approval(
                registryId=self.registry_id, recordId=record_id
            )
        )

    def set_status(self, record_id: str, status: str, reason: str) -> bool:
        """``UpdateRegistryRecordStatus``; False when the record is gone, and the registry's refusal of the
        edge (``ValidationException``) propagates for the caller to translate."""
        return _landed(
            lambda: self._ctl.update_registry_record_status(
                registryId=self.registry_id, recordId=record_id, status=status, statusReason=reason
            )
        )

    def _update(self, record_id: str, params: dict, deadline: float) -> dict:
        return _retry_on_conflict(
            lambda: self._ctl.update_registry_record(
                registryId=self.registry_id, recordId=record_id, **params
            ),
            deadline,
        )

    def _read(
        self,
        record_id: str,
        deadline: float,
        after: datetime | None,
        sent: dict | None,
        done: Callable[[str], bool],
    ) -> dict | None:
        """GET until the record is out of CREATING/UPDATING and ``done(status)``, skipping reads
        older than ``after`` (AGP's last call). With ``sent``, a read whose content is not what
        AGP wrote raises ``_Superseded``. Reads at least once, stops at ``deadline``. Returns the
        last settled record, or None."""
        last = None
        while True:
            try:
                got = self._ctl.get_registry_record(registryId=self.registry_id, recordId=record_id)
            except ClientError as err:
                if not _is_conflict(err):
                    raise
                got = None
            if got is not None and got.get("status") not in _TRANSIENT:
                at = _ts(got.get("updatedAt"))
                if after is None or at is None or at >= after:
                    key = _first_mismatch(got, sent) if sent is not None else None
                    if key:
                        raise _Superseded(got["status"], key)
                    last = got
                    if done(got["status"]):
                        return got
            if _monotonic() + _DELAY > deadline:
                return last
            _sleep(_DELAY)

    def _settled(self, record_id: str, deadline: float) -> dict | None:
        """The record once out of CREATING/UPDATING, or None. Never raises: a failed pre-read
        must not block the platform write itself; the re-assert is skipped, and logged."""
        try:
            got = self._read(record_id, deadline, None, None, _any_status)
        except Exception as err:
            logger.warning(
                "[registry] could not read record %s before a platform write; if it was APPROVED "
                "the write leaves it DRAFT and it needs re-approval: %s",
                record_id,
                err,
                exc_info=True,
            )
            return None
        if got is None:
            logger.warning(
                "[registry] record %s still transient at the deadline before a platform write; "
                "if it was APPROVED the write leaves it DRAFT and it needs re-approval",
                record_id,
            )
        return got

    def _reassert(
        self, record_id: str, *, written: Any, sent: dict, prior_reason: str | None, deadline: float
    ) -> WriteOutcome:
        """Put a record that was APPROVED before the write back to APPROVED. ``written`` is the write's
        response (its ``updatedAt`` only skips stale reads); ``sent`` is what AGP wrote (the race guard).
        A settled write that did not reset the record (still APPROVED) is accepted as-is."""
        status: str | None = None
        reset = False
        ours = _updated_at(written)
        try:
            got = self._read(record_id, deadline, ours, sent, _any_status)
            status = got["status"] if got else None
            if status == "DRAFT":
                reset = True
                resp = _retry_on_conflict(
                    lambda: self._ctl.submit_registry_record_for_approval(
                        registryId=self.registry_id, recordId=record_id
                    ),
                    deadline,
                )
                ours = _updated_at(resp)
                got = self._read(record_id, deadline, ours, sent, lambda s: s != "DRAFT")
                status = got["status"] if got else None
            if status == "PENDING_APPROVAL":
                reset = True
                resp = _retry_on_conflict(
                    lambda: self._ctl.update_registry_record_status(
                        registryId=self.registry_id,
                        recordId=record_id,
                        status=APPROVED,
                        statusReason=_reason(prior_reason),
                    ),
                    deadline,
                )
                ours = _updated_at(resp)
                got = self._read(record_id, deadline, ours, sent, lambda s: s == APPROVED)
                status = got["status"] if got else None
            if status != APPROVED:
                raise RuntimeError(f"record ended at {status!r}")
            return WriteOutcome(
                status=status, written=True, reset_and_recovered=reset, superseded_by_other_writer=False
            )
        except _Superseded as sup:
            if sup.status != APPROVED:
                logger.warning(
                    "[registry] record %s was changed by another write while AGP re-asserted APPROVED "
                    "(first differing field: %s); left at %s for review (AGP does not approve content "
                    "it did not write)",
                    record_id,
                    sup.key,
                    sup.status,
                )
            return WriteOutcome(
                status=sup.status, written=True, reset_and_recovered=False, superseded_by_other_writer=True
            )
        except Exception as err:
            status = self._final_status(record_id, ours, sent, status)
            if status != APPROVED:
                logger.warning(
                    "[registry] could not re-assert APPROVED on record %s after a platform write; "
                    "it is %s and needs re-approval: %s",
                    record_id,
                    status,
                    err,
                    exc_info=True,
                )
            return WriteOutcome(
                status=status,
                written=True,
                reset_and_recovered=reset and status == APPROVED,
                superseded_by_other_writer=False,
            )

    def _final_status(
        self, record_id: str, ours: datetime | None, sent: dict, status: str | None
    ) -> str | None:
        try:
            got = self._read(record_id, 0.0, ours, sent, _any_status)
        except _Superseded as sup:
            return sup.status
        except Exception:
            logger.warning(
                "[registry] the final read of record %s failed; reporting the last status seen",
                record_id,
                exc_info=True,
            )
            return status
        return got["status"] if got else status
