"""The registry writer: under ``src/`` the one registry caller, and AGP's re-approval after its writes.

The AWS Agent Registry resets a record's status APPROVED -> DRAFT on ANY UpdateRegistryRecord. AGP puts
an APPROVED record back to APPROVED after its own writes (submit -> approve, since DRAFT -> APPROVED is
an illegal edge); a user's content edit stays DRAFT. ``RegistryWriter.write`` is the platform write,
``RegistryWriter.write_content`` the user's edit, and one deadline, ``REAPPROVAL_BUDGET_S``, bounds each
write's conflict retry, pre-read and re-assert.

The registry behind the writer is ``core.registry_memory.MemoryRegistryClient``, the in-memory
double that settles a write's transient status on the next read; a ``MagicMock(wraps=...)`` spy
stands between the writer and the double so call records and per-operation overrides work, and a
test that needs a registry behaviour the double does not offer writes the double's state directly.
The ``ConflictException`` paths, which the double never raises, run over a plain ``MagicMock``. The
writer's clock is faked: its ``_sleep`` advances its ``_monotonic``.
"""

from __future__ import annotations

import copy
import itertools
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from conftest import MCP_RECORD_ID, RECORD_ARN, RECORD_ID, _mcp_sample_record, _sample_record
from test_mcp_server_service import _publication

from core.registry_memory import MemoryRegistryClient
from core.registry_resolver import (
    RegistryNotConfiguredError,
    RegistryNotFoundError,
    find_registry_by_name,
)
from models.agent import AgentUpdate, LifecycleState
from models.mcp_server import McpServerUpdate
from services.registry_writer import (
    REAPPROVAL_BUDGET_S,
    RecordContent,
    RegistryWriter,
    WriteOutcome,
    stable_status,
)

REGISTRY_NAME = "agp-reapproval"
REGION = "us-east-1"
GONE = "abcdefabcdef"
_CREATE_KEYS = ("name", "displayName", "description", "recordType", "recordVersion", "descriptors")
_FACTORIES = {"agent": (_sample_record, RECORD_ID), "mcp": (_mcp_sample_record, MCP_RECORD_ID)}


def _registry_id(registry: MemoryRegistryClient) -> str:
    page = next(iter(registry.get_paginator("list_registries").paginate()))
    return page["registries"][0]["registryId"]


REGISTRY_ID = _registry_id(MemoryRegistryClient(registries=[REGISTRY_NAME], region=REGION))


class _Clock:
    """Fake monotonic clock: ``sleep`` advances it, so a budget overrun is observable."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _tick_clock():
    base = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)
    ticks = itertools.count(1)
    return lambda: base + timedelta(seconds=next(ticks))


def _memory_ctl(record_factory, status: str, *, auto_approve: bool = False):
    """The in-memory registry holding one record in ``status``, behind a recording spy.

    The record is created from the fixture record's content, read once so it settles to DRAFT,
    then re-keyed to the fixture's id and set to ``status`` with the human approver's reason.
    ``ctl.double`` is the registry itself (for a test that calls an operation around an override
    or writes the stored record), ``ctl.settled()`` the record's status after any pending settle,
    ``ctl.user_edit()`` a user's content PUT landing: new content, reset to DRAFT, newer
    ``updatedAt``, no pending settle."""
    double = MemoryRegistryClient(
        registries=[REGISTRY_NAME], region=REGION, clock=_tick_clock(), auto_approve=auto_approve
    )
    seed = record_factory(status=status)
    created = double.create_registry_record(
        registryId=REGISTRY_ID, **{key: seed[key] for key in _CREATE_KEYS}
    )
    minted = created["recordArn"].rsplit("/", 1)[-1]
    double.get_registry_record(registryId=REGISTRY_ID, recordId=minted)
    record_id = seed["recordId"]
    records = double._records[REGISTRY_ID]
    record = records[record_id] = records.pop(minted)
    record.update(
        recordId=record_id,
        recordArn=f"{record['registryArn']}/record/{record_id}",
        status=status,
        statusReason="Approved by alice: reviewed",
    )

    def _settled():
        return double.get_registry_record(registryId=REGISTRY_ID, recordId=record_id)["status"]

    def _user_edit():
        record["description"] = "edited by a user, never reviewed"
        record["status"] = "DRAFT"
        record["updatedAt"] += timedelta(seconds=1)
        double._pending.pop((REGISTRY_ID, record_id), None)

    ctl = MagicMock(name="agent-registry-control", wraps=double)
    ctl.double = double
    ctl.settled = _settled
    ctl.user_edit = _user_edit
    return ctl


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    import services.registry_writer as mod

    c = _Clock()
    monkeypatch.setattr(mod, "_monotonic", c.now)
    monkeypatch.setattr(mod, "_sleep", c.sleep)
    return c


@pytest.fixture(params=["agent", "mcp"])
def kind(request):
    return request.param


def _writer_over(kind: Literal["agent", "mcp"], status: str, **knobs):
    factory, record_id = _FACTORIES[kind]
    ctl = _memory_ctl(factory, status, **knobs)
    return RegistryWriter(registry_id=REGISTRY_ID, kind=kind, control_client=ctl), ctl, record_id


def _stored(ctl, record_id: str) -> dict:
    return ctl.double._records[REGISTRY_ID][record_id]


def _content(record: dict, **changes) -> RecordContent:
    fields = {
        "name": record["name"],
        "description": record["description"],
        "descriptors": copy.deepcopy(record["descriptors"]),
    }
    return RecordContent(**{**fields, **changes})


def _edited(record: dict) -> RecordContent:
    return _content(record, description=f"{record['description']} (identity provisioned)")


def _error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


def _mock_writer(record: dict, kind: Literal["agent", "mcp"] = "agent"):
    ctl = MagicMock(name="agent-registry-control")
    ctl.get_registry_record.return_value = record
    ctl.update_registry_record.return_value = {"recordId": record["recordId"], "status": "UPDATING"}
    return RegistryWriter(registry_id=REGISTRY_ID, kind=kind, control_client=ctl), ctl


def test_write_reasserts_approved_when_it_was_approved(kind):
    writer, ctl, record_id = _writer_over(kind, "APPROVED")

    outcome = writer.write(record_id, _edited(_stored(ctl, record_id)))

    assert outcome == WriteOutcome(
        status="APPROVED", written=True, reset_and_recovered=True, superseded_by_other_writer=False
    )
    assert ctl.settled() == "APPROVED"
    ctl.submit_registry_record_for_approval.assert_called_once_with(
        registryId=REGISTRY_ID, recordId=record_id
    )
    kwargs = ctl.update_registry_record_status.call_args.kwargs
    assert kwargs["status"] == "APPROVED"
    assert kwargs["statusReason"]


def test_write_makes_no_call_and_resets_nothing_when_the_content_already_matches(kind):
    writer, ctl, record_id = _writer_over(kind, "APPROVED")

    outcome = writer.write(record_id, _content(_stored(ctl, record_id)))

    assert outcome == WriteOutcome(
        status="APPROVED", written=False, reset_and_recovered=False, superseded_by_other_writer=False
    )
    ctl.update_registry_record.assert_not_called()
    ctl.submit_registry_record_for_approval.assert_not_called()
    ctl.update_registry_record_status.assert_not_called()
    assert ctl.settled() == "APPROVED"


def test_a_write_that_drops_a_key_from_the_data_blob_is_written():
    writer, ctl, record_id = _writer_over("agent", "DRAFT")
    stored = _stored(ctl, record_id)
    both = {"agent_arns": {"dev": "runtime-dev", "prod": "runtime-prod"}, "model_id": "m"}
    dev_only = {"agent_arns": {"dev": "runtime-dev"}, "model_id": "m"}
    stored["descriptors"] = {"custom": {"data": json.dumps(both)}}

    outcome = writer.write(
        record_id, _content(stored, descriptors={"custom": {"data": json.dumps(dev_only)}})
    )

    assert outcome.written
    ctl.update_registry_record.assert_called_once()
    assert json.loads(_stored(ctl, record_id)["descriptors"]["custom"]["data"]) == dev_only


def test_write_does_not_approve_a_draft_record(kind):
    writer, ctl, record_id = _writer_over(kind, "DRAFT")

    outcome = writer.write(record_id, _edited(_stored(ctl, record_id)))

    assert outcome == WriteOutcome(
        status="DRAFT", written=True, reset_and_recovered=False, superseded_by_other_writer=False
    )
    ctl.submit_registry_record_for_approval.assert_not_called()
    ctl.update_registry_record_status.assert_not_called()
    assert ctl.settled() == "DRAFT"


def test_write_content_never_reasserts_and_reports_the_reset(kind):
    writer, ctl, record_id = _writer_over(kind, "APPROVED")

    outcome = writer.write_content(record_id, _edited(_stored(ctl, record_id)))

    assert outcome == WriteOutcome(
        status="UPDATING", written=True, reset_and_recovered=False, superseded_by_other_writer=False
    )
    ctl.submit_registry_record_for_approval.assert_not_called()
    ctl.update_registry_record_status.assert_not_called()
    assert ctl.settled() == "DRAFT"


def test_write_content_answers_none_when_the_record_is_gone(kind):
    writer, ctl, record_id = _writer_over(kind, "DRAFT")

    assert writer.write_content(GONE, _edited(_stored(ctl, record_id))) is None


def test_a_reassert_failure_logs_the_record_and_returns_the_true_state(kind, caplog):
    writer, ctl, record_id = _writer_over(kind, "APPROVED")
    ctl.submit_registry_record_for_approval.side_effect = _error(
        "AccessDeniedException", "SubmitRegistryRecordForApproval"
    )

    with caplog.at_level(logging.WARNING):
        outcome = writer.write(record_id, _edited(_stored(ctl, record_id)))

    assert ctl.settled() == "DRAFT"
    assert outcome.status == "DRAFT"
    assert not outcome.reset_and_recovered
    assert any(record_id in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


def test_a_write_that_did_not_reset_costs_no_wait(kind, clock):
    """the read showing AGP's own updatedAt is authoritative at once — no fixed wait."""
    writer, ctl, record_id = _writer_over(kind, "APPROVED")

    def _keeps_the_status(**kw):
        was = ctl.double._records[REGISTRY_ID][record_id]["status"]
        resp = ctl.double.update_registry_record(**kw)
        ctl.double._pending[(REGISTRY_ID, record_id)] = was
        return resp

    ctl.update_registry_record.side_effect = _keeps_the_status

    outcome = writer.write(record_id, _edited(_stored(ctl, record_id)))

    ctl.submit_registry_record_for_approval.assert_not_called()
    assert outcome.status == "APPROVED"
    assert not outcome.reset_and_recovered
    assert clock.t == 0


def test_auto_approve_registry_skips_the_approve_call(kind):
    writer, ctl, record_id = _writer_over(kind, "APPROVED", auto_approve=True)

    outcome = writer.write(record_id, _edited(_stored(ctl, record_id)))

    ctl.update_registry_record_status.assert_not_called()
    assert outcome.status == "APPROVED"
    assert outcome.reset_and_recovered


def test_reassert_is_bounded_by_one_overall_budget(kind, clock):
    writer, ctl, record_id = _writer_over(kind, "APPROVED")
    content = _edited(_stored(ctl, record_id))
    real_update = ctl.double.update_registry_record

    def _stuck(**kw):
        resp = real_update(**kw)
        ctl.double._pending.pop((REGISTRY_ID, record_id), None)
        return resp

    ctl.update_registry_record.side_effect = _stuck

    outcome = writer.write(record_id, content)

    assert clock.t <= REAPPROVAL_BUDGET_S
    assert stable_status(outcome.status) == "DRAFT"


def test_two_writes_in_one_request_stay_well_under_the_gateway_cap():
    assert 2 * REAPPROVAL_BUDGET_S <= 15


def test_a_concurrent_user_edit_is_never_approved(kind, caplog):
    """a user content PUT landing between AGP's write and AGP's submit must stay DRAFT
    (the user was told PROPOSED) — AGP must not approve content nobody reviewed."""
    writer, ctl, record_id = _writer_over(kind, "APPROVED")
    content = _edited(_stored(ctl, record_id))
    real_update = ctl.double.update_registry_record

    def _then_user_edit(**kw):
        resp = real_update(**kw)
        ctl.user_edit()
        return resp

    ctl.update_registry_record.side_effect = _then_user_edit

    with caplog.at_level(logging.WARNING):
        outcome = writer.write(record_id, content)

    ctl.submit_registry_record_for_approval.assert_not_called()
    ctl.update_registry_record_status.assert_not_called()
    assert ctl.settled() == "DRAFT"
    assert outcome == WriteOutcome(
        status="DRAFT", written=True, reset_and_recovered=False, superseded_by_other_writer=True
    )
    assert any(record_id in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    msgs = [r.getMessage() for r in caplog.records if "another write" in r.getMessage()]
    assert msgs and "first differing field: None" not in msgs[0]


def test_a_concurrent_edit_after_submit_is_not_approved(kind):
    writer, ctl, record_id = _writer_over(kind, "APPROVED")
    content = _edited(_stored(ctl, record_id))
    real_submit = ctl.double.submit_registry_record_for_approval

    def _then_user_edit(**kw):
        resp = real_submit(**kw)
        ctl.user_edit()
        return resp

    ctl.submit_registry_record_for_approval.side_effect = _then_user_edit

    outcome = writer.write(record_id, content)

    ctl.update_registry_record_status.assert_not_called()
    assert outcome.status == "DRAFT"
    assert outcome.superseded_by_other_writer


def test_the_settle_stamping_a_newer_updated_at_is_still_agps_own_write(kind, caplog):
    """The registry's async UPDATING -> DRAFT settle stamps a NEWER updatedAt than the write's
    response. The content is still exactly what AGP wrote, so AGP re-approves."""
    writer, ctl, record_id = _writer_over(kind, "APPROVED")

    with caplog.at_level(logging.WARNING):
        outcome = writer.write(record_id, _edited(_stored(ctl, record_id)))

    assert ctl.settled() == "APPROVED"
    assert outcome.status == "APPROVED"
    ctl.update_registry_record_status.assert_called_once()
    assert not [r for r in caplog.records if "another write" in r.getMessage()]


def test_a_stale_read_of_the_pre_write_record_is_not_trusted(kind):
    writer, ctl, record_id = _writer_over(kind, "APPROVED")
    content = _edited(_stored(ctl, record_id))
    real_get = ctl.double.get_registry_record
    calls = {"n": 0}

    def _first_read_after_write_is_stale(**kw):
        rec = real_get(**kw)
        if ctl.update_registry_record.called and calls["n"] == 0:
            calls["n"] += 1
            return {**rec, "status": "APPROVED", "updatedAt": datetime(2026, 1, 1, tzinfo=timezone.utc)}
        return rec

    ctl.get_registry_record.side_effect = _first_read_after_write_is_stale

    outcome = writer.write(record_id, content)

    ctl.submit_registry_record_for_approval.assert_called_once()
    assert outcome.status == "APPROVED"
    assert ctl.settled() == "APPROVED"


def test_the_human_approvers_reason_is_kept(kind):
    """the re-approval keeps who approved and why, and does not stack suffixes."""
    writer, ctl, record_id = _writer_over(kind, "APPROVED")
    stored = _stored(ctl, record_id)

    writer.write(record_id, _edited(stored))
    first = stored["statusReason"]
    writer.write(record_id, _content(stored, description="a second platform write"))

    assert first.startswith("Approved by alice: reviewed")
    assert "AGP" in first
    assert stored["statusReason"] == first
    assert len(first) <= 255
    assert ctl.update_registry_record.call_count == 2


def test_a_failed_pre_read_does_not_block_the_write(kind, caplog):
    writer, ctl, record_id = _writer_over(kind, "APPROVED")
    content = _edited(_stored(ctl, record_id))
    ctl.get_registry_record.side_effect = _error("ThrottlingException", "GetRegistryRecord")

    with caplog.at_level(logging.WARNING):
        outcome = writer.write(record_id, content)

    ctl.update_registry_record.assert_called_once()
    assert outcome.written
    assert stable_status(outcome.status) == "DRAFT"
    assert any(record_id in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


@pytest.mark.parametrize(
    "fault", [RuntimeError("stub control client"), KeyError("status")], ids=["runtime", "key"]
)
def test_a_pre_read_fault_of_any_type_does_not_block_the_write(kind, caplog, fault):
    """The pre-read never raises: a non-boto fault from the control client is logged with the
    record id and the write still happens, reporting the write's own status."""
    writer, ctl, record_id = _writer_over(kind, "APPROVED")
    content = _edited(_stored(ctl, record_id))
    ctl.get_registry_record.side_effect = fault

    with caplog.at_level(logging.WARNING):
        outcome = writer.write(record_id, content)

    ctl.update_registry_record.assert_called_once()
    assert stable_status(outcome.status) != "APPROVED"
    assert any(record_id in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


def test_a_registry_resolution_fault_raises_before_any_write(kind):
    factory, record_id = _FACTORIES[kind]
    ctl = _memory_ctl(factory, "APPROVED")
    ctl.get_paginator.side_effect = [
        _error("ThrottlingException", "ListRegistries"),
        ctl.double.get_paginator("list_registries"),
    ]
    writer = RegistryWriter(registry_name=REGISTRY_NAME, region=REGION, kind=kind, control_client=ctl)

    with pytest.raises(ClientError) as exc:
        writer.write(record_id, _edited(_stored(ctl, record_id)))

    assert exc.value.response["Error"]["Code"] == "ThrottlingException"
    ctl.update_registry_record.assert_not_called()
    assert ctl.settled() == "APPROVED"


@pytest.mark.parametrize(
    "fault", [RuntimeError("stub control client"), KeyError("status")], ids=["runtime", "key"]
)
def test_a_reassert_fault_of_any_type_is_logged_and_the_true_state_returned(kind, caplog, fault):
    """The re-assert never raises: a non-boto fault from the submit call is logged with the record
    id and the caller gets the record's true state."""
    writer, ctl, record_id = _writer_over(kind, "APPROVED")
    ctl.submit_registry_record_for_approval.side_effect = fault

    with caplog.at_level(logging.WARNING):
        outcome = writer.write(record_id, _edited(_stored(ctl, record_id)))

    assert ctl.settled() == "DRAFT"
    assert outcome.status == "DRAFT"
    assert any(record_id in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


def test_write_retries_a_conflict_inside_the_deadline(clock):
    record = _sample_record(status="DRAFT")
    writer, ctl = _mock_writer(record)
    ctl.update_registry_record.side_effect = [
        _error("ConflictException", "UpdateRegistryRecord"),
        {"recordId": RECORD_ID, "status": "UPDATING"},
    ]

    outcome = writer.write(RECORD_ID, _edited(record))

    assert outcome.written
    assert ctl.update_registry_record.call_count == 2
    assert 0 < clock.t <= REAPPROVAL_BUDGET_S


def test_write_gives_up_at_the_deadline(clock):
    record = _sample_record(status="DRAFT")
    writer, ctl = _mock_writer(record)
    ctl.update_registry_record.side_effect = _error("ConflictException", "UpdateRegistryRecord")

    with pytest.raises(ClientError) as exc:
        writer.write(RECORD_ID, _edited(record))

    assert exc.value.response["Error"]["Code"] == "ConflictException"
    assert ctl.update_registry_record.call_count > 1
    assert clock.t <= REAPPROVAL_BUDGET_S


def test_write_does_not_retry_a_non_conflict_error():
    record = _sample_record(status="DRAFT")
    writer, ctl = _mock_writer(record)
    ctl.update_registry_record.side_effect = _error("ValidationException", "UpdateRegistryRecord")

    with pytest.raises(ClientError):
        writer.write(RECORD_ID, _edited(record))
    assert ctl.update_registry_record.call_count == 1


def test_an_agent_update_meets_one_conflict_then_succeeds(clock):
    """The user edit of an agent retries a ConflictException inside the one deadline."""
    from services.agent_registry_service import AgentRegistryService

    ctl = MagicMock(name="agent-registry-control")
    ctl.get_registry_record.return_value = _sample_record(status="DRAFT")
    ctl.update_registry_record.side_effect = [
        _error("ConflictException", "UpdateRegistryRecord"),
        {"recordArn": RECORD_ARN, "status": "UPDATING"},
    ]
    svc = AgentRegistryService(registry_id=REGISTRY_ID, control_client=ctl)

    updated = svc.update(RECORD_ID, AgentUpdate(business_unit="Finance"))

    assert updated is not None
    assert updated.business_unit == "Finance"
    assert ctl.update_registry_record.call_count == 2
    assert clock.t <= REAPPROVAL_BUDGET_S


def test_an_mcp_write_that_never_leaves_updating_gives_up_at_the_one_deadline(clock):
    from services.mcp_server_service import McpServerRegistryService

    ctl = MagicMock(name="agent-registry-control")
    ctl.get_registry_record.return_value = _mcp_sample_record(status="DRAFT")
    ctl.update_registry_record.side_effect = _error("ConflictException", "UpdateRegistryRecord")
    svc = McpServerRegistryService(registry_id=REGISTRY_ID, control_client=ctl)

    with pytest.raises(ClientError) as exc:
        svc.update(MCP_RECORD_ID, McpServerUpdate(description="edited"))

    assert exc.value.response["Error"]["Code"] == "ConflictException"
    assert ctl.update_registry_record.call_count > 1
    assert clock.t <= REAPPROVAL_BUDGET_S < 12 * 2.0


def test_create_answers_the_record_id_and_the_settled_draft(kind):
    factory, _record_id = _FACTORIES[kind]
    double = MemoryRegistryClient(registries=[REGISTRY_NAME], region=REGION, clock=_tick_clock())
    ctl = MagicMock(name="agent-registry-control", wraps=double)
    writer = RegistryWriter(registry_id=REGISTRY_ID, kind=kind, control_client=ctl)
    seed = factory()
    token = str(uuid.uuid4())

    record_id, status = writer.create(_content(seed), record_type=seed["recordType"], client_token=token)

    assert status == "DRAFT"
    assert double.get_registry_record(registryId=REGISTRY_ID, recordId=record_id)["name"] == seed["name"]
    kwargs = ctl.create_registry_record.call_args.kwargs
    assert kwargs["recordType"] == seed["recordType"]
    assert kwargs["clientToken"] == token
    assert kwargs["displayName"] == seed["name"]


def test_create_keeps_polling_through_a_conflict_while_creating():
    ctl = MagicMock(name="agent-registry-control")
    ctl.create_registry_record.return_value = {"recordArn": RECORD_ARN, "status": "CREATING"}
    ctl.get_registry_record.side_effect = [
        _error("ConflictException", "GetRegistryRecord"),
        _sample_record(status="DRAFT"),
    ]
    writer = RegistryWriter(registry_id=REGISTRY_ID, kind="agent", control_client=ctl)

    created = writer.create(_content(_sample_record()), record_type="CUSTOM", client_token=str(uuid.uuid4()))

    assert created == (RECORD_ID, "DRAFT")
    assert ctl.get_registry_record.call_count == 2


def test_create_raises_when_the_record_never_reaches_draft():
    ctl = MagicMock(name="agent-registry-control")
    ctl.create_registry_record.return_value = {"recordArn": RECORD_ARN, "status": "CREATING"}
    ctl.get_registry_record.return_value = _sample_record(status="CREATING")
    writer = RegistryWriter(registry_id=REGISTRY_ID, kind="agent", control_client=ctl)

    with pytest.raises(RuntimeError):
        writer.create(_content(_sample_record()), record_type="CUSTOM", client_token=str(uuid.uuid4()))
    assert ctl.get_registry_record.call_count > 1


def test_get_answers_the_record_or_none(kind):
    writer, _ctl, record_id = _writer_over(kind, "DRAFT")

    got = writer.get(record_id)

    assert got is not None
    assert got["recordId"] == record_id
    assert writer.get(GONE) is None


def test_iter_summaries_filters_by_type_name_and_status(kind):
    writer, ctl, record_id = _writer_over(kind, "APPROVED")
    stored = _stored(ctl, record_id)
    record_type = stored["recordType"]

    assert [s["recordId"] for s in writer.iter_summaries(record_type=record_type)] == [record_id]
    assert [s["recordId"] for s in writer.iter_summaries(record_type=record_type, name=stored["name"])] == [
        record_id
    ]
    assert list(writer.iter_summaries(record_type=record_type, name="nobody")) == []
    assert list(writer.iter_summaries(record_type=record_type, status="DRAFT")) == []
    assert [s["recordId"] for s in writer.iter_summaries(record_type=record_type, status="APPROVED")] == [
        record_id
    ]
    assert ctl.list_registry_records.call_args.kwargs["filters"] == [
        {"name": "recordType", "values": [record_type]},
        {"name": "status", "values": ["APPROVED"]},
    ]


def test_iter_summaries_follows_the_next_token():
    ctl = MagicMock(name="agent-registry-control")
    ctl.list_registry_records.side_effect = [
        {"registryRecords": [{"recordId": "rec-a"}], "nextToken": "page-2"},
        {"registryRecords": [{"recordId": "rec-b"}]},
    ]
    writer = RegistryWriter(registry_id=REGISTRY_ID, kind="agent", control_client=ctl)

    assert [s["recordId"] for s in writer.iter_summaries(record_type="CUSTOM")] == ["rec-a", "rec-b"]
    assert ctl.list_registry_records.call_args_list[1].kwargs["nextToken"] == "page-2"


def test_delete_submit_and_set_status_answer_true_when_they_land(kind):
    writer, ctl, record_id = _writer_over(kind, "DRAFT")

    assert writer.submit(record_id) is True
    assert ctl.settled() == "PENDING_APPROVAL"
    assert writer.set_status(record_id, "REJECTED", "not this one") is True
    assert ctl.settled() == "REJECTED"
    assert writer.delete(record_id) is True
    assert writer.get(record_id) is None


def test_delete_submit_and_set_status_answer_false_for_a_record_that_is_gone(kind):
    writer, _ctl, _record_id = _writer_over(kind, "DRAFT")

    assert writer.delete(GONE) is False
    assert writer.submit(GONE) is False
    assert writer.set_status(GONE, "REJECTED", "gone already") is False


def test_set_status_lets_a_refused_edge_propagate(kind):
    writer, _ctl, record_id = _writer_over(kind, "DRAFT")

    with pytest.raises(ClientError) as exc:
        writer.set_status(record_id, "APPROVED", "skip the review")
    assert exc.value.response["Error"]["Code"] == "ValidationException"


def _paged(*registries):
    ctl = MagicMock(name="agent-registry-control")
    ctl.get_paginator.return_value.paginate.return_value = [{"registries": list(registries)}]
    return ctl


def test_the_registry_id_resolves_the_name_once_through_the_writers_paginator():
    ctl = _paged(
        {"name": "agp-agents", "registryId": "REGRESOLVED01", "registryArn": "arn:x", "status": "READY"}
    )
    writer = RegistryWriter(registry_name="agp-agents", region=REGION, kind="agent", control_client=ctl)

    ctl.get_paginator.assert_not_called()
    assert writer.registry_id == "REGRESOLVED01"
    assert writer.registry_id == "REGRESOLVED01"
    ctl.get_paginator.assert_called_once_with("list_registries")


def test_an_explicit_registry_id_skips_the_lookup():
    ctl = _paged()
    writer = RegistryWriter(
        registry_id="REGPINNED0001", registry_name="agp-agents", kind="agent", control_client=ctl
    )

    assert writer.registry_id == "REGPINNED0001"
    ctl.get_paginator.assert_not_called()


def test_an_absent_name_names_the_registry_and_the_region():
    writer = RegistryWriter(
        registry_name="agp-agents", region="eu-west-1", kind="agent", control_client=_paged()
    )

    with pytest.raises(RegistryNotFoundError) as exc:
        _ = writer.registry_id
    assert "agp-agents" in str(exc.value)
    assert "eu-west-1" in str(exc.value)


@pytest.mark.parametrize(
    "kind, service, setting",
    [("agent", "AgentRegistryService", "AGENT"), ("mcp", "McpServerRegistryService", "MCP")],
)
def test_neither_id_nor_name_names_the_kinds_settings(kind, service, setting):
    writer = RegistryWriter(kind=kind, control_client=_paged())

    with pytest.raises(RegistryNotConfiguredError) as exc:
        _ = writer.registry_id
    message = str(exc.value)
    assert service in message
    assert f"{setting}_REGISTRY_NAME" in message
    assert f"{setting}_REGISTRY_ID" in message


def test_find_registry_by_name_reads_the_pages_it_is_given():
    pages = [
        {
            "registries": [
                {"name": "noise", "registryId": "REGNOISE00001", "registryArn": "arn:1", "status": "READY"}
            ]
        },
        {
            "registries": [
                {
                    "name": "agp-agents",
                    "registryId": "REGAGENTS0001",
                    "registryArn": "arn:2",
                    "status": "READY",
                }
            ]
        },
    ]

    assert find_registry_by_name(pages, "agp-agents") == ("REGAGENTS0001", "arn:2")
    assert find_registry_by_name(pages, "nobody") == (None, None)


def _mcp_svc(status: str, **knobs):
    from services.mcp_server_service import McpServerRegistryService

    ctl = _memory_ctl(_mcp_sample_record, status, **knobs)
    return McpServerRegistryService(registry_id=REGISTRY_ID, control_client=ctl), ctl


def _agent_svc(status: str, **knobs):
    from services.agent_registry_service import AgentRegistryService

    ctl = _memory_ctl(_sample_record, status, **knobs)
    return AgentRegistryService(registry_id=REGISTRY_ID, control_client=ctl), ctl


@pytest.fixture(params=["mcp", "agent"])
def svc_factory(request):
    return {"mcp": (_mcp_svc, MCP_RECORD_ID), "agent": (_agent_svc, RECORD_ID)}[request.param]


def test_persist_identity_reports_the_reasserted_lifecycle(svc_factory):
    make, record_id = svc_factory
    svc, ctl = make("APPROVED")
    obj = svc.get(record_id)
    obj.business_unit = "Identity"

    returned = svc.persist_identity(obj)

    assert ctl.settled() == "APPROVED"
    ctl.submit_registry_record_for_approval.assert_called_once()
    assert returned.lifecycle_state == LifecycleState.APPROVED


def test_persist_identity_of_unchanged_content_makes_no_write(svc_factory):
    """The first platform write stores the envelope as the model writes it; persisting the record
    read back makes no call and resets nothing."""
    make, record_id = svc_factory
    svc, ctl = make("APPROVED")
    svc.persist_identity(svc.get(record_id))
    ctl.reset_mock()

    returned = svc.persist_identity(svc.get(record_id))

    ctl.update_registry_record.assert_not_called()
    ctl.submit_registry_record_for_approval.assert_not_called()
    assert ctl.settled() == "APPROVED"
    assert returned.lifecycle_state == LifecycleState.APPROVED


@pytest.mark.parametrize(
    "status, lifecycle",
    [("DRAFT", LifecycleState.PROPOSED), ("PENDING_APPROVAL", LifecycleState.PENDING_APPROVAL)],
)
def test_persist_identity_reports_a_non_approved_prior_from_the_settled_read(svc_factory, status, lifecycle):
    make, record_id = svc_factory
    svc, ctl = make(status)
    obj = svc.get(record_id)
    obj.lifecycle_state = LifecycleState.APPROVED
    obj.business_unit = "Identity"

    returned = svc.persist_identity(obj)

    ctl.update_registry_record.assert_called_once()
    ctl.submit_registry_record_for_approval.assert_not_called()
    assert returned.lifecycle_state == lifecycle


def test_persist_marketplace_reasserts_approved(svc_factory):
    make, record_id = svc_factory
    svc, ctl = make("APPROVED")

    returned = svc.persist_marketplace(record_id, _publication())

    assert ctl.settled() == "APPROVED"
    assert returned.lifecycle_state == LifecycleState.APPROVED


def test_set_published_is_an_internal_write_and_reasserts_approved(svc_factory):
    make, record_id = svc_factory
    svc, ctl = make("APPROVED")

    returned = svc.set_published(record_id, True)

    assert returned.published is True
    assert ctl.settled() == "APPROVED"
    assert returned.lifecycle_state == LifecycleState.APPROVED


def test_set_published_returns_none_when_missing(svc_factory):
    make, record_id = svc_factory
    svc, ctl = make("APPROVED")
    ctl.get_registry_record.side_effect = _error("ResourceNotFoundException", "GetRegistryRecord")

    assert svc.set_published(record_id, True) is None
    ctl.update_registry_record.assert_not_called()


def test_user_update_does_not_reassert_and_reports_the_reset(svc_factory):
    make, record_id = svc_factory
    svc, ctl = make("APPROVED")
    req = (
        McpServerUpdate(description="edited") if record_id == MCP_RECORD_ID else AgentUpdate(purpose="edited")
    )

    returned = svc.update(record_id, req)

    ctl.submit_registry_record_for_approval.assert_not_called()
    ctl.update_registry_record_status.assert_not_called()
    assert ctl.settled() == "DRAFT"
    assert returned.lifecycle_state == LifecycleState.PROPOSED


def test_a_failed_pre_read_never_reports_a_stale_approved(svc_factory):
    """Without a pre-read the re-assert is skipped, so the returned object must carry the write's
    (reset) status, never the in-hand APPROVED — otherwise approve_publish reports success while
    the card is delisted."""
    make, record_id = svc_factory
    svc, ctl = make("APPROVED")
    obj = svc.get(record_id)
    ctl.get_registry_record.side_effect = _error("ThrottlingException", "GetRegistryRecord")

    returned = svc.persist_identity(obj)

    ctl.update_registry_record.assert_called_once()
    assert returned.lifecycle_state != LifecycleState.APPROVED


def test_set_published_maps_a_vanished_record_to_none(svc_factory):
    """deleted between get and write -> None (404), not a raw 500."""
    make, record_id = svc_factory
    svc, ctl = make("DRAFT")
    ctl.update_registry_record.side_effect = _error("ResourceNotFoundException", "UpdateRegistryRecord")

    assert svc.set_published(record_id, True) is None


def test_mcp_set_published_maps_validation_to_422():
    from services.mcp_server_service import McpValidationError

    svc, ctl = _mcp_svc("DRAFT")
    ctl.update_registry_record.side_effect = _error("ValidationException", "UpdateRegistryRecord")

    with pytest.raises(McpValidationError):
        svc.set_published(MCP_RECORD_ID, True)
