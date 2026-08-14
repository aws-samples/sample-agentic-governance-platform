"""Offline tests for the E32 re-registration driver (injected services, no AWS/Graph).

The script under test WRITES TO LIVE SYSTEMS when run for real (it creates registry
records), so the property these tests exist to pin down is MECHANICAL, not cosmetic: in
``--dry-run`` **no method is called on any injected collaborator at all**. The zero-writes
tests therefore assert on the mocks (``mock_calls == []`` / ``assert_not_called``), never on
log text — a log line proves nothing about whether a call happened — and one goes further
still, injecting collaborators that raise on ANY attribute access.

The script does NOT provision Entra identity, apply grants, or delete Entra apps: it drives
the SERVICE layer, and ``create()`` only stamps ``identity_status="pending"`` (provisioning
is the route-level background task at ``api/routes/agents.py:322``). ``--apply-grants`` and
``--delete-old-apps`` therefore exit 2 with the manual procedure, and a matching group of
tests below pins that refusal.

Both registry services are injected. No boto3 client and no httpx client is ever
constructed here, so the conftest non-loopback socket guard has nothing to trip on.

Mirrors ``tests/test_backfill_langfuse_projects.py``: the backend dir goes on ``sys.path``
so ``import scripts.<mod>`` resolves, and the script itself is import-safe (stdlib-only at
module top; models/services/boto3 are imported lazily inside functions).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# The backend dir on sys.path makes `scripts` importable as a namespace package
# (same shim as tests/test_backfill_langfuse_projects.py).
_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from scripts.reregister_records import (  # noqa: E402
    build_plan,
    parse_args,
    read_old_records,
    reregister,
)

# The AWS documentation example account id (the same placeholder tests/conftest.py uses).
# The live account id is NEVER baked into the repo — it arrives via CLI args / settings.
_EXAMPLE_ACCOUNT = "123456789012"
_GATEWAY_ARN = (
    f"arn:aws:bedrock-agentcore:us-east-1:{_EXAMPLE_ACCOUNT}:gateway/agp-support-mcp-abc123"
)
_RUNTIME_ARN = (
    f"arn:aws:bedrock-agentcore:us-east-1:{_EXAMPLE_ACCOUNT}:runtime/agp-fnol-agent-xyz789"
)


# ===========================================================================
# The three tests from the task brief (verbatim contract)
# ===========================================================================

def test_dry_run_makes_no_writes():
    agents, mcps = MagicMock(), MagicMock()
    plan = [{"old_id": "OLD1", "name": "fnol-agent", "kind": "agent", "envelope": {}}]
    out = reregister(plan, dry_run=True, agent_service=agents, mcp_service=mcps)
    agents.create.assert_not_called()
    mcps.create.assert_not_called()
    assert out[0]["status"] == "dry-run"
    assert out[0]["new_id"] is None


def test_build_plan_selects_only_wired_records():
    """Only records with real wiring are re-registered; DRAFT seed data is skipped."""
    records = [
        {"recordId": "W1", "displayName": "wired", "_envelope": {"entra_app_id": "x", "agent_arn": "y"}},
        {"recordId": "D1", "displayName": "draft", "_envelope": {}},
    ]
    plan = build_plan(records, kind="agent")
    assert [p["old_id"] for p in plan] == ["W1"]


def test_live_run_creates_and_reports_new_id():
    agents, mcps = MagicMock(), MagicMock()
    agents.create.return_value = MagicMock(id="NEW1")
    plan = [{"old_id": "OLD1", "name": "fnol-agent", "kind": "agent", "envelope": {}}]
    out = reregister(plan, dry_run=False, agent_service=agents, mcp_service=mcps)
    agents.create.assert_called_once()
    assert out[0] == {"old_id": "OLD1", "new_id": "NEW1", "name": "fnol-agent",
                      "kind": "agent", "status": "created"}


# ===========================================================================
# Zero-writes property — the mechanical proof, asserted on the mocks
# ===========================================================================

def test_dry_run_calls_nothing_at_all_on_any_collaborator():
    """--dry-run performs ZERO writes: no create, and in fact NOT ONE call of any name on
    either registry service double.

    ``mock_calls == []`` is deliberately stronger than ``create.assert_not_called()``: it
    also catches a read (``get``/``list``) that a future edit might slip into the dry-run
    path, which on the live account would be an unintended API call against the registry.
    The envelopes below carry the full wiring (old Entra ids, ``mcp_server_ids``) precisely
    so that a re-introduced grant/delete preview would have something to act on.
    """
    agents, mcps = MagicMock(), MagicMock()
    plan = [
        {
            "old_id": "OLDA",
            "name": "fnol-agent",
            "kind": "agent",
            "envelope": {
                "entra_app_id": "app-old",
                "entra_sp_id": "sp-old",
                "agent_arn": _RUNTIME_ARN,
                "mcp_server_ids": ["MCP-OLD-1", "MCP-OLD-2"],
            },
        },
        {
            "old_id": "OLDM",
            "name": "support-mcp",
            "kind": "mcp",
            "envelope": {"entra_app_id": "app-old-mcp", "gateway_arn": _GATEWAY_ARN, "kind": "gateway"},
            "server_json": {"description": "IT support", "version": "1.0.0"},
            "tools": [],
        },
    ]

    out = reregister(
        plan,
        dry_run=True,
        agent_service=agents,
        mcp_service=mcps,
    )

    # Not one call — not create, not get, not list — on either service double.
    assert agents.mock_calls == []
    assert mcps.mock_calls == []
    agents.create.assert_not_called()
    mcps.create.assert_not_called()

    assert [row["status"] for row in out] == ["dry-run", "dry-run"]
    assert [row["new_id"] for row in out] == [None, None]


def test_dry_run_does_not_even_dereference_a_collaborator():
    """Stronger than ``mock_calls == []``: the collaborators raise on ANY attribute access,
    so this passes only if the dry-run branch never so much as LOOKS at them. That rules out
    a lazily-triggered property or a `getattr` probe, which a call-recording mock would
    happily absorb. (The reviewer's landmine probe, pinned as a test.)"""

    class _Landmine:
        def __getattr__(self, name):
            raise AssertionError(f"dry-run touched the collaborator: .{name}")

    plan = [
        {
            "old_id": "OLDA",
            "name": "fnol-agent",
            "kind": "agent",
            "envelope": {
                "entra_app_id": "app-old",
                "entra_sp_id": "sp-old",
                "agent_arn": _RUNTIME_ARN,
                "mcp_server_ids": ["MCP-OLD-1"],
            },
        },
        {
            "old_id": "OLDM",
            "name": "support-mcp",
            "kind": "mcp",
            "envelope": {"entra_app_id": "app-old-mcp", "gateway_arn": _GATEWAY_ARN,
                         "kind": "gateway"},
            "server_json": {"description": "IT support", "version": "1.0.0"},
            "tools": [],
        },
    ]
    out = reregister(plan, dry_run=True, agent_service=_Landmine(), mcp_service=_Landmine())
    assert [row["status"] for row in out] == ["dry-run", "dry-run"]
    assert [row["new_id"] for row in out] == [None, None]


def test_dry_run_is_the_default_mode():
    """A bare invocation (no mode flag) must not mutate anything: dry_run stays on."""
    args = parse_args([])
    assert args.live is False
    args_live = parse_args(["--live"])
    assert args_live.live is True


def test_live_and_dry_run_together_is_refused():
    """An ambiguous instruction about WRITING is refused rather than silently resolved."""
    with pytest.raises(SystemExit):
        parse_args(["--live", "--dry-run"])


# ===========================================================================
# build_plan gating
# ===========================================================================

def test_build_plan_keeps_gateway_wired_mcp_and_skips_unwired():
    records = [
        {
            "recordId": "M1",
            "displayName": "support-mcp",
            "status": "APPROVED",
            "_envelope": {"entra_app_id": "x", "gateway_arn": _GATEWAY_ARN},
        },
        {"recordId": "M2", "displayName": "draft-mcp", "status": "DRAFT", "_envelope": {}},
    ]
    plan = build_plan(records, kind="mcp")
    assert [p["old_id"] for p in plan] == ["M1"]
    assert plan[0]["kind"] == "mcp"
    assert plan[0]["envelope"]["gateway_arn"] == _GATEWAY_ARN


def test_build_plan_skips_deprecated_records_even_when_wired():
    """The superseded pre-rebrand MCP records are DEPRECATED and deliberately dropped —
    they are not re-registered even though their envelopes still carry real wiring."""
    records = [
        {
            "recordId": "DEP1",
            "displayName": "legacy-support-mcp",
            "status": "DEPRECATED",
            "_envelope": {"entra_app_id": "x", "gateway_arn": _GATEWAY_ARN},
        },
        {
            "recordId": "OK1",
            "displayName": "support-mcp",
            "status": "APPROVED",
            "_envelope": {"entra_app_id": "y", "gateway_arn": _GATEWAY_ARN},
        },
    ]
    plan = build_plan(records, kind="mcp")
    assert [p["old_id"] for p in plan] == ["OK1"]


def test_build_plan_falls_back_to_name_when_display_name_absent():
    records = [{"recordId": "W1", "name": "fnol-agent", "_envelope": {"entra_app_id": "x"}}]
    plan = build_plan(records, kind="agent")
    assert plan[0]["name"] == "fnol-agent"


def test_build_plan_keeps_runtime_arn_only_record():
    records = [{"recordId": "R1", "displayName": "r", "_envelope": {"runtime_arn": _RUNTIME_ARN}}]
    assert [p["old_id"] for p in build_plan(records, kind="mcp")] == ["R1"]


# ===========================================================================
# OLD-namespace read path (old schema keys)
# ===========================================================================

def test_read_old_records_reads_the_old_agent_schema():
    """Agents live at ``descriptors.custom.inlineContent`` in the OLD namespace (the ported
    services now WRITE ``descriptors.custom.data``), and the OLD ListRegistryRecords takes
    ``descriptorType`` as a discrete kwarg, not a structured ``filters`` list."""
    envelope = {"entra_app_id": "app-1", "agent_arn": _RUNTIME_ARN}
    ctl = MagicMock()
    ctl.list_registry_records.return_value = {
        "registryRecords": [{"recordId": "A1"}],
        "nextToken": None,
    }
    ctl.get_registry_record.return_value = {
        "recordId": "A1",
        "name": "fnol-agent",
        "displayName": "fnol-agent",
        "status": "DRAFT",
        "descriptors": {"custom": {"inlineContent": json.dumps(envelope)}},
    }

    records = read_old_records("OLD-AGENT-REG", "agent", control_client=ctl)

    assert ctl.list_registry_records.call_args.kwargs["descriptorType"] == "CUSTOM"
    assert ctl.list_registry_records.call_args.kwargs["registryId"] == "OLD-AGENT-REG"
    assert [r["_envelope"] for r in records] == [envelope]


def test_read_old_records_reads_the_old_mcp_schema():
    """MCPs live at ``descriptors.mcp.server.inlineContent`` (+ ``mcp.tools.inlineContent``)
    in the OLD namespace, with governance under ``_meta``."""
    envelope = {"entra_app_id": "app-2", "gateway_arn": _GATEWAY_ARN, "kind": "gateway"}
    server_json = {
        "name": "agp/support-mcp",
        "description": "IT support",
        "version": "1.2.0",
        "remotes": [{"type": "streamable-http", "url": "https://gw.example.test/mcp"}],
        "_meta": {"com.agp/governance": envelope},
    }
    tools = [{"name": "ticket_lookup", "description": "Look up", "inputSchema": {"type": "object"}}]
    ctl = MagicMock()
    ctl.list_registry_records.return_value = {
        "registryRecords": [{"recordId": "M1"}],
        "nextToken": None,
    }
    ctl.get_registry_record.return_value = {
        "recordId": "M1",
        "name": "support-mcp",
        "displayName": "support-mcp",
        "status": "APPROVED",
        "descriptors": {
            "mcp": {
                "server": {"inlineContent": json.dumps(server_json)},
                "tools": {"inlineContent": json.dumps({"tools": tools})},
            }
        },
    }

    records = read_old_records("OLD-MCP-REG", "mcp", control_client=ctl)

    assert ctl.list_registry_records.call_args.kwargs["descriptorType"] == "MCP"
    assert records[0]["_envelope"] == envelope
    assert records[0]["_server_json"] == server_json
    assert records[0]["_tools"] == tools


def test_read_old_records_paginates():
    ctl = MagicMock()
    ctl.list_registry_records.side_effect = [
        {"registryRecords": [{"recordId": "A1"}], "nextToken": "t1"},
        {"registryRecords": [{"recordId": "A2"}]},
    ]
    ctl.get_registry_record.side_effect = lambda registryId, recordId: {
        "recordId": recordId,
        "name": recordId,
        "status": "DRAFT",
        "descriptors": {"custom": {"inlineContent": json.dumps({"entra_app_id": "x"})}},
    }
    records = read_old_records("REG", "agent", control_client=ctl)
    assert [r["recordId"] for r in records] == ["A1", "A2"]


def test_read_old_records_skips_a_malformed_record_without_aborting():
    ctl = MagicMock()
    ctl.list_registry_records.return_value = {"registryRecords": [{"recordId": "BAD"}, {"recordId": "OK"}]}

    def _get(registryId, recordId):
        if recordId == "BAD":
            return {"recordId": "BAD", "name": "bad", "descriptors": {}}
        return {
            "recordId": "OK",
            "name": "ok",
            "descriptors": {"custom": {"inlineContent": json.dumps({"entra_app_id": "x"})}},
        }

    ctl.get_registry_record.side_effect = _get
    records = read_old_records("REG", "agent", control_client=ctl)
    assert [r["recordId"] for r in records] == ["OK"]


# ===========================================================================
# Live re-registration — payload fidelity
# ===========================================================================

def test_live_run_carries_gateway_arn_verbatim_into_the_new_mcp_record():
    """Registering an MCP only REFERENCES its gateway by ARN — it never creates one — so
    the new record must point at the SAME live gateway (whose Cedar policy engine stayed
    behind in the bedrock-agentcore namespace and keeps enforcing)."""
    agents, mcps = MagicMock(), MagicMock()
    mcps.create.return_value = MagicMock(id="NEWM")
    plan = [
        {
            "old_id": "OLDM",
            "name": "support-mcp",
            "kind": "mcp",
            "envelope": {
                "entra_app_id": "app-old",
                "gateway_arn": _GATEWAY_ARN,
                "kind": "gateway",
                "owner_email": "ops@example.test",
                "business_unit": "Customer Service",
                "region": "EU",
                "data_classification": "Confidential",
                "tenant_id": "default",
            },
            "server_json": {
                "description": "IT support desk tools",
                "version": "1.2.0",
                "remotes": [{"type": "streamable-http", "url": "https://gw.example.test/mcp"}],
            },
            "tools": [
                {"name": "ticket_lookup", "description": "Look up", "inputSchema": {"type": "object"}}
            ],
        }
    ]

    out = reregister(plan, dry_run=False, agent_service=agents, mcp_service=mcps)

    req = mcps.create.call_args.args[0]
    assert req.gateway_arn == _GATEWAY_ARN  # verbatim, byte-identical
    assert req.name == "support-mcp"
    assert req.kind.value == "gateway"
    assert req.description == "IT support desk tools"
    assert req.version == "1.2.0"
    assert req.endpoint_url == "https://gw.example.test/mcp"
    assert [t.name for t in req.available_tools] == ["ticket_lookup"]
    assert req.available_tools[0].input_schema == {"type": "object"}
    assert req.tenant_id == "default"
    assert out[0]["new_id"] == "NEWM"
    agents.create.assert_not_called()


def test_live_run_does_not_carry_the_old_entra_identity_onto_the_new_record():
    """The re-registered record gets a FRESH, self-consistent identity (the locked E32
    decision — no id crosswalk). The old ``entra_app_id`` must NOT be replayed onto the
    new payload: ``AgentCreate`` has no such field, and copying it would re-point the new
    record at the superseded app registration this run is about to delete."""
    agents, mcps = MagicMock(), MagicMock()
    agents.create.return_value = MagicMock(id="NEW1")
    plan = [
        {
            "old_id": "OLD1",
            "name": "fnol-agent",
            "kind": "agent",
            "envelope": {
                "entra_app_id": "app-old",
                "entra_sp_id": "sp-old",
                "agent_arn": _RUNTIME_ARN,
                "sponsor_email": "ops@example.test",
                "business_unit": "Claims",
                "platform": "aws_bedrock",
                "auth_type": "entra",
                "tenant_id": "default",
                "purpose": "First notice of loss intake",
            },
        }
    ]

    reregister(plan, dry_run=False, agent_service=agents, mcp_service=mcps)

    req = agents.create.call_args.args[0]
    assert not hasattr(req, "entra_app_id")
    assert req.agent_arn == _RUNTIME_ARN  # the runtime handle DOES carry through
    assert req.name == "fnol-agent"
    assert req.purpose == "First notice of loss intake"
    assert req.platform.value == "aws_bedrock"
    assert req.auth_type.value == "entra"
    assert req.tenant_id == "default"


def test_live_run_defaults_tenant_id_when_the_old_envelope_predates_multi_tenancy():
    agents, mcps = MagicMock(), MagicMock()
    agents.create.return_value = MagicMock(id="NEW1")
    plan = [{"old_id": "OLD1", "name": "a", "kind": "agent", "envelope": {}}]
    reregister(plan, dry_run=False, agent_service=agents, mcp_service=mcps)
    assert agents.create.call_args.args[0].tenant_id == "default"


# ===========================================================================
# Idempotency + resilience
# ===========================================================================

def test_name_taken_is_logged_and_skipped_not_fatal():
    """A name that already exists raises NameTakenError → log + skip; the run continues
    (mirrors scripts/seed_agents.py)."""
    from services.agent_registry_service import NameTakenError

    agents, mcps = MagicMock(), MagicMock()
    agents.create.side_effect = [NameTakenError("taken"), MagicMock(id="NEW2")]
    plan = [
        {"old_id": "OLD1", "name": "dup", "kind": "agent", "envelope": {}},
        {"old_id": "OLD2", "name": "fresh", "kind": "agent", "envelope": {}},
    ]
    out = reregister(plan, dry_run=False, agent_service=agents, mcp_service=mcps)
    assert [row["status"] for row in out] == ["skipped-exists", "created"]
    assert [row["new_id"] for row in out] == [None, "NEW2"]


def test_a_create_failure_does_not_abort_the_remaining_records():
    agents, mcps = MagicMock(), MagicMock()
    agents.create.side_effect = [RuntimeError("boom"), MagicMock(id="NEW2")]
    plan = [
        {"old_id": "OLD1", "name": "bad", "kind": "agent", "envelope": {}},
        {"old_id": "OLD2", "name": "good", "kind": "agent", "envelope": {}},
    ]
    out = reregister(plan, dry_run=False, agent_service=agents, mcp_service=mcps)
    assert [row["status"] for row in out] == ["failed", "created"]


def test_unknown_kind_is_reported_not_raised():
    agents, mcps = MagicMock(), MagicMock()
    plan = [{"old_id": "X", "name": "x", "kind": "banana", "envelope": {}}]
    out = reregister(plan, dry_run=False, agent_service=agents, mcp_service=mcps)
    assert out[0]["status"] == "failed"
    agents.create.assert_not_called()
    mcps.create.assert_not_called()


# ===========================================================================
# Grants + Entra cleanup are NOT done by this script — the flags refuse
#
# `service.create()` provisions NO Entra identity: it stamps identity_status="pending",
# and the real provisioning hook is the route-level background task at
# `api/routes/agents.py:322` (`mcp_servers.py:237`), which a service-layer script cannot
# reach. Two consequences these tests pin down:
#   * a created record can never carry `entra_sp_id` (not a field on `AgentCreate`), so
#     `apply_agent_mcp_grant` — whose Graph principal IS `entra_sp_id` — could never run;
#   * the new record has no identity at all, so deleting the superseded Entra app would
#     destroy the only working one.
# The earlier tests here asserted a grant happy path on `MagicMock(entra_sp_id="sp-new")`
# — a shape production cannot produce — which is exactly what hid a 100% grant skip. They
# are gone: `reregister()` no longer takes `apply_grants`/`delete_old_apps`/`grant_fn`/
# `delete_app_fn` at all, and the flags exit 2 with the manual procedure instead.
# ===========================================================================

def test_reregister_does_not_accept_the_grant_or_delete_parameters_at_all():
    """The strongest available guard against the defect coming back: there is no parameter
    to pass, so no caller can re-enable a grant or an Entra delete from this script."""
    import inspect as _inspect

    params = set(_inspect.signature(reregister).parameters)
    assert params == {"plan", "dry_run", "agent_service", "mcp_service"}


def test_the_script_contains_no_call_to_the_grant_or_entra_delete_helpers():
    """No code path may invoke ``apply_agent_mcp_grant`` or ``delete_agent_app`` on the
    false premise that this script's records have an identity. Asserted on the module's own
    source so a re-introduction anywhere in the file trips it (prose mentions live in the
    docstring, which is stripped from the parsed body)."""
    import ast

    import scripts.reregister_records as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    called = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
    }
    assert "apply_agent_mcp_grant" not in called
    assert "delete_agent_app" not in called
    # Nor may they be imported for use elsewhere.
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "apply_agent_mcp_grant" not in imported


@pytest.mark.parametrize("flag", ["--apply-grants", "--delete-old-apps"])
def test_the_refused_flags_exit_2_with_guidance_and_build_no_client(flag, monkeypatch, caplog):
    """Both flags REFUSE rather than warn-and-continue, in dry-run AND in --live, and the
    refusal happens before any service/client is constructed — so no AWS or Graph call can
    precede it. The message must name the manual procedure."""
    from scripts import reregister_records as mod

    def _explode(*args, **kwargs):  # pragma: no cover — must never be reached
        raise AssertionError("a client was constructed before the refusal")

    monkeypatch.setattr(mod, "_build_services", _explode)
    monkeypatch.setattr(mod, "read_old_records", _explode)

    for argv in ([flag], [flag, "--live"]):
        caplog.clear()
        with caplog.at_level("ERROR"):
            # Registry ids ARE supplied, so the refusal cannot be confused with the
            # missing-id usage error — and it must fire before that check anyway.
            rc = mod.main(argv + ["--agent-registry-id", "OLD-A", "--mcp-registry-id", "OLD-M"])
        assert rc == 2, f"{argv} must exit 2 (the usage-error code), not run"
        message = "\n".join(record.getMessage() for record in caplog.records)
        assert "REFUSED" in message
        assert "reprovision" in message


def test_the_refusal_fires_even_without_the_registry_ids():
    """The refusal precedes every other validation, so the operator is told the real reason
    rather than being sent to fix a registry id for a run that could never work."""
    from scripts.reregister_records import main

    assert main(["--apply-grants"]) == 2
    assert main(["--delete-old-apps"]) == 2
    assert main(["--live", "--apply-grants", "--delete-old-apps"]) == 2


def test_a_created_record_is_reported_as_needing_manual_reprovisioning(capsys):
    """A live run is NOT finished when the script exits: every created record still has
    identity_status='pending'. The summary must say so where the operator is looking."""
    from scripts.reregister_records import print_summary

    print_summary(
        [{"old_id": "OLD1", "new_id": "NEW1", "name": "a", "kind": "agent", "status": "created"}],
        dry_run=False,
    )
    out = capsys.readouterr().out
    assert "reprovision" in out
    assert "pending" in out


def test_the_dry_run_summary_does_not_claim_a_reprovision_step():
    """Nothing was created, so there is nothing to re-provision — the dry-run footer stays
    the unambiguous 'nothing was written' line."""
    from scripts.reregister_records import print_summary
    import io
    import contextlib

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        print_summary(
            [{"old_id": "OLD1", "new_id": None, "name": "a", "kind": "agent", "status": "dry-run"}],
            dry_run=True,
        )
    out = buffer.getvalue()
    assert "DRY RUN — nothing was written" in out
    assert "reprovision" not in out


# ===========================================================================
# CLI surface
# ===========================================================================

def test_cli_kind_defaults_to_both_and_region_to_us_east_1():
    args = parse_args([])
    assert args.kind == "both"
    assert args.region == "us-east-1"
    assert args.apply_grants is False
    assert args.delete_old_apps is False


def test_main_dry_run_requires_the_old_registry_id_for_the_selected_kind():
    from scripts.reregister_records import main

    # Nothing to read from → a clear non-zero exit, and no client is ever constructed.
    assert main(["--kind", "agent"]) == 2
