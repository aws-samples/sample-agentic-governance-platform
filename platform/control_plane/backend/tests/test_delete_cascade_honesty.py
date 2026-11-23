"""The repo delete cascade must not report a teardown it did not perform.

When the repo row points at a record the configured registry returns NotFound for (``get()`` is
``None``), the preview must not grey runtime and identity as already deleted, and the cascade must not
say ``deleted`` for runtime, identity and record while all of them survive. These tests drive the real
``AgentRegistryService`` over a mocked control client, so the registry's own ``delete()`` return
contract (the prior Agent on success, ``None`` when nothing was removed) is what the cascade reads.

A harness agent's teardown is not automated and is never claimed. An MCP-server repo's cascade reports
the Terraform-managed gateway as manual with its state key (it is never offered as a checkbox), a
never-built gateway as absent, and previews the Cedar engine, which rides identity.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from conftest import RECORD_ID, REGISTRY_ID, _sample_record

from core.state_store import StateStore
from models.project import Project
from models.repository import RepoDeleteItemResult, RepoDeleteSelection, Repository
from services import project_teardown
from services.agent_registry_service import AgentRegistryService
from services.github_repo_service import GitHubRepoService
from services.mcp_server_service import MalformedMcpRecordError
from services.project_errors import ProjectError
from services.project_service import ProjectService

P = "proj-1"
R = "repo-1"
_TS = "2026-07-02T00:00:00+00:00"
RECORD_ONLY = RepoDeleteSelection(github=False, image=False, runtime=False, identity=False)


def _not_found(op: str) -> ClientError:
    return ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, op)


@pytest.fixture
def ctl():
    return MagicMock(name="agent-registry-control")


@pytest.fixture
def svc(ctl):
    github = MagicMock(spec=GitHubRepoService)
    github.repo_exists.return_value = False
    conn = MagicMock()
    conn.get_connection.return_value = SimpleNamespace(org="acme", base_url=None)
    conn.get_bearer_token.return_value = "ghp_secret"
    s = ProjectService(
        table_name="",
        registry=AgentRegistryService(registry_id=REGISTRY_ID, control_client=ctl),
        identity=MagicMock(),
        connection_service=conn,
        github_repo_service=github,
    )
    s._save_project(
        Project(
            id=P,
            name="p1",
            connection_id="c1",
            tenant_id="default",
            description="d",
            created_by="op@x",
            created_at=_TS,
            updated_at=_TS,
        )
    )
    s._save_repo(
        Repository(
            id=R,
            project_id=P,
            name="my-agent",
            repo_url="https://github.com/acme/my-agent",
            agent_id=RECORD_ID,
            template_name="strands-agentcore",
            cicd_status="provisioning",
            status="provisioning",
            created_by="op@x",
            created_at=_TS,
            updated_at=_TS,
        )
    )
    return s


def _record_item(result):
    return next(i for i in result.items if i.item == "record")


def test_a_delete_that_removed_nothing_is_failed_and_keeps_the_row(svc, ctl):
    """The record was read up front, but the registry delete found nothing to remove."""
    ctl.get_registry_record.return_value = _sample_record()
    ctl.delete_registry_record.side_effect = _not_found("DeleteRegistryRecord")
    res = svc.delete_repo(project_id=P, repo_id=R, selection=RECORD_ONLY)
    item = _record_item(res)
    assert (item.outcome, item.reason) == (
        "failed",
        "record not found at delete time — retry to remove the row",
    )
    assert res.record_removed is False
    assert svc._get_repo(R) is not None


NO_RECORD = ("skipped", "no registry record — nothing known to tear down")


def test_a_record_the_registry_does_not_know_reports_nothing_as_deleted(svc, ctl):
    """``get()`` is ``None``: the configured registry does not know the record, and AGP cannot verify
    "already gone", so nothing agent-derived may read as torn down; the operator asked to delete, so the
    row is still reclaimed."""
    ctl.get_registry_record.side_effect = _not_found("GetRegistryRecord")
    res = svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection(github=False, image=False))
    by_item = {i.item: (i.outcome, i.reason) for i in res.items}
    assert by_item["runtime"] == NO_RECORD
    assert by_item["identity"] == NO_RECORD
    assert by_item["langfuse"] == NO_RECORD
    assert by_item["record"] == ("skipped", "not found in the registry")
    assert "deleted" not in {outcome for outcome, _ in by_item.values()}
    assert res.record_removed is True
    assert svc._get_repo(R) is None


def test_a_record_the_registry_does_not_know_previews_unknown_not_gone(svc, ctl):
    ctl.get_registry_record.side_effect = _not_found("GetRegistryRecord")
    states = {i.item: i.state for i in svc.preview_delete(project_id=P, repo_id=R).items}
    assert states["runtime"] == "unknown"
    assert states["identity"] == "unknown"


def test_a_malformed_record_previews_unknown_instead_of_500(svc, ctl):
    ctl.get_registry_record.return_value = _sample_record(descriptors={})
    states = {i.item: i.state for i in svc.preview_delete(project_id=P, repo_id=R).items}
    assert states["runtime"] == "unknown"
    assert states["identity"] == "unknown"


def test_deleting_a_malformed_record_is_refused_before_any_step(svc, ctl):
    ctl.get_registry_record.return_value = _sample_record(descriptors={})
    with pytest.raises(ProjectError) as e:
        svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())
    assert e.value.kind == "record_unreadable"
    ctl.delete_registry_record.assert_not_called()
    assert svc._get_repo(R) is not None


HARNESS_ARN = "arn:aws:bedrock-agentcore:us-east-1:ACCOUNT:harness/hrn-abc"
HARNESS_RUNTIME_SKIP = (
    "skipped",
    "harness teardown isn't automated yet — delete the harness in the AgentCore console "
    "(its Terraform state and managed memory are left too)",
)
HARNESS_EXEC_ROLE_SKIP = (
    "skipped",
    "not automated for harnesses yet — delete the my-agent-dev-harness-exec role in IAM",
)


@pytest.fixture
def harness_svc(svc):
    agent = SimpleNamespace(
        id=RECORD_ID,
        name="my-agent",
        agent_arn=HARNESS_ARN,
        agent_arns={"dev": HARNESS_ARN},
        entra_app_id=None,
        entra_sp_id=None,
    )
    svc._registry = MagicMock()
    svc._registry.get.return_value = agent
    return svc


def test_a_harness_runtime_previews_unknown_not_gone(harness_svc):
    states = {i.item: i.state for i in harness_svc.preview_delete(project_id=P, repo_id=R).items}
    assert states["runtime"] == "unknown"
    harness_svc._identity.runtime_exists.assert_not_called()


def test_a_harness_runtime_and_exec_role_are_skipped_never_deleted(harness_svc):
    """The exec role is an IAM fact: its reason names the role, not the AgentCore console."""
    res = harness_svc.delete_repo(
        project_id=P,
        repo_id=R,
        selection=RepoDeleteSelection(github=False, image=False, identity=False, record=False),
    )
    by_item = {i.item: (i.outcome, i.reason) for i in res.items}
    assert by_item["runtime"] == HARNESS_RUNTIME_SKIP
    assert by_item["exec_role"] == HARNESS_EXEC_ROLE_SKIP
    assert "AgentCore" not in by_item["exec_role"][1]
    harness_svc._identity.delete_runtime.assert_not_called()


MCP_ID = "mcp-1"
GATEWAY_MANUAL = (
    "manual",
    (
        "Not removed by AGP — Terraform-managed. Delete it with terraform destroy on state key "
        f"agentcore-gateway/{MCP_ID}/dev/, or in the AgentCore console"
    ),
)
GATEWAY_MAYBE = (
    "manual",
    (
        "Not removed by AGP — a build ran but recorded no gateway, so it may have left a partial "
        f"one. Check Terraform state key agentcore-gateway/{MCP_ID}/dev/ and destroy it there"
    ),
)
GATEWAY_ABSENT = ("absent", "never built — nothing to remove")
NOT_USED = ("skipped", "not used by MCP servers")


@pytest.fixture
def mcp_svc(svc, monkeypatch):
    mcp = SimpleNamespace(
        id=MCP_ID,
        name="claims-api",
        entra_app_id="app-1",
        entra_sp_id="sp-1",
        cedar_policy_engine_id="pe-1",
    )
    svc._mcp_registry = MagicMock()
    svc._mcp_registry.get.return_value = mcp
    svc._mcp_registry.delete.return_value = mcp
    svc._registry = MagicMock()
    svc._delete_repo(R)
    svc._save_repo(
        Repository(
            id=R,
            project_id=P,
            name="claims-api",
            repo_url="https://github.com/acme/claims-api",
            artifact_kind="mcp_server",
            mcp_server_id=MCP_ID,
            template_name="gateway-agentcore",
            cicd_status="provisioning",
            status="provisioning",
            created_by="op@x",
            created_at=_TS,
            updated_at=_TS,
        )
    )
    calls = []

    async def fake_teardown(m):
        calls.append(m)
        return list(svc.teardown_items)

    svc.teardown_items = [
        RepoDeleteItemResult(item="identity", outcome="deleted"),
        RepoDeleteItemResult(item="policy_engine", outcome="skipped"),
    ]
    svc.teardown_calls = calls
    monkeypatch.setattr("services.project_teardown.teardown_mcp_identity", fake_teardown)
    return svc


def test_an_mcp_repo_delete_runs_github_identity_then_the_mcp_record(mcp_svc):
    """The agent registry is never touched for an MCP repo."""
    res = mcp_svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection(image=False))
    assert [i.item for i in res.items] == [
        "github",
        "image",
        "runtime",
        "identity",
        "policy_engine",
        "record",
    ]
    by_item = {i.item: (i.outcome, i.reason) for i in res.items}
    assert by_item["identity"] == ("deleted", None)
    assert by_item["record"] == ("deleted", None)
    assert res.record_removed is True
    assert mcp_svc.teardown_calls == [mcp_svc._mcp_registry.get.return_value]
    mcp_svc._mcp_registry.delete.assert_called_once_with(MCP_ID)
    assert mcp_svc._get_repo(R) is None
    mcp_svc._registry.get.assert_not_called()
    mcp_svc._registry.delete.assert_not_called()


def _set_repo(svc, **fields):
    """The buildspec's write path: ``_save_repo`` deliberately skips ``gateway_*`` and ``cicd_status``."""
    svc._write_repo(svc._get_repo(R).model_copy(update=fields), set())


def test_a_built_mcp_gateway_is_reported_manual_with_its_state_key(mcp_svc):
    """The gateway is not a deletable item: the result says how to remove it by hand, whatever the
    selection said, since it is never offered as a checkbox. ``exec_role`` and ``langfuse`` are never
    emitted, because the modal ties them to runtime and identity and would flag them amber."""
    _set_repo(mcp_svc, gateway_arn="arn:gw", gateway_id="gw-1")
    res = mcp_svc.delete_repo(
        project_id=P, repo_id=R, selection=RepoDeleteSelection(image=False, runtime=False)
    )
    by_item = {i.item: (i.outcome, i.reason) for i in res.items}
    assert by_item["runtime"] == GATEWAY_MANUAL
    assert by_item["image"] == NOT_USED
    assert "exec_role" not in by_item
    assert "langfuse" not in by_item
    mcp_svc._identity.delete_runtime.assert_not_called()


def test_an_mcp_identity_failure_keeps_the_record_for_retry(mcp_svc):
    mcp_svc.teardown_items = [
        RepoDeleteItemResult(item="identity", outcome="failed", reason="HTTPStatusError"),
        RepoDeleteItemResult(item="policy_engine", outcome="deleted"),
    ]
    res = mcp_svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection(image=False))
    record = _record_item(res)
    assert (record.outcome, record.reason) == ("skipped", "kept for retry — an earlier step failed")
    assert res.record_removed is False
    mcp_svc._mcp_registry.delete.assert_not_called()
    assert mcp_svc._get_repo(R) is not None


def test_unselected_mcp_legs_are_skipped_not_run(mcp_svc):
    """The gateway line reports its real state, never "not selected": it is not selectable."""
    res = mcp_svc.delete_repo(
        project_id=P,
        repo_id=R,
        selection=RepoDeleteSelection(github=False, image=False, runtime=False, identity=False, record=False),
    )
    by_item = {i.item: (i.outcome, i.reason) for i in res.items}
    for item in ("github", "identity", "record"):
        assert by_item[item] == ("skipped", "not selected")
    assert by_item["runtime"] == GATEWAY_ABSENT
    assert mcp_svc.teardown_calls == []
    mcp_svc._mcp_registry.delete.assert_not_called()


def test_an_mcp_repo_whose_record_is_gone_tears_nothing_down_but_reclaims_the_row(mcp_svc):
    mcp_svc._mcp_registry.get.return_value = None
    mcp_svc._mcp_registry.delete.return_value = None
    res = mcp_svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection(image=False))
    by_item = {i.item: (i.outcome, i.reason) for i in res.items}
    assert by_item["identity"] == ("skipped", "no registry record — nothing known to tear down")
    assert by_item["record"] == ("skipped", "not found in the registry")
    assert mcp_svc.teardown_calls == []
    assert mcp_svc._get_repo(R) is None


def test_an_mcp_record_that_vanished_mid_cascade_is_failed_and_keeps_the_row(mcp_svc):
    mcp_svc._mcp_registry.delete.return_value = None
    res = mcp_svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection(image=False))
    record = _record_item(res)
    assert (record.outcome, record.reason) == (
        "failed",
        "record not found at delete time — retry to remove the row",
    )
    assert mcp_svc._get_repo(R) is not None


def test_an_mcp_repo_preview_probes_identity_and_record_and_shows_the_gateway_as_manual(mcp_svc):
    states = {i.item: i.state for i in mcp_svc.preview_delete(project_id=P, repo_id=R).items}
    assert states == {
        "github": "gone",
        "runtime": "gone",
        "identity": "present",
        "policy_engine": "present",
        "record": "present",
    }
    mcp_svc._identity.runtime_exists.assert_not_called()
    mcp_svc._registry.get.assert_not_called()


def test_an_mcp_preview_identity_is_gone_without_ids_and_unknown_without_a_record(mcp_svc):
    mcp_svc._mcp_registry.get.return_value = SimpleNamespace(
        id=MCP_ID, entra_app_id=None, entra_sp_id=None, cedar_policy_engine_id=None
    )
    states = {i.item: i.state for i in mcp_svc.preview_delete(project_id=P, repo_id=R).items}
    assert states["identity"] == "gone"
    mcp_svc._mcp_registry.get.return_value = None
    states = {i.item: i.state for i in mcp_svc.preview_delete(project_id=P, repo_id=R).items}
    assert states["identity"] == "unknown"


def test_an_mcp_preview_shows_identity_present_when_only_a_cedar_engine_is_left(mcp_svc):
    """Greying identity as "gone" here would uncheck the only leg that deletes the engine."""
    mcp_svc._mcp_registry.get.return_value = SimpleNamespace(
        id=MCP_ID, entra_app_id=None, entra_sp_id=None, cedar_policy_engine_id="pe-1"
    )
    states = {i.item: i.state for i in mcp_svc.preview_delete(project_id=P, repo_id=R).items}
    assert states["identity"] == "present"


def test_the_policy_engine_line_is_reported_when_identity_does_not_run(mcp_svc):
    res = mcp_svc.delete_repo(
        project_id=P, repo_id=R, selection=RepoDeleteSelection(image=False, identity=False, record=False)
    )
    by_item = {i.item: (i.outcome, i.reason) for i in res.items}
    assert by_item["policy_engine"] == ("skipped", "left in place — identity not selected")
    mcp_svc._mcp_registry.get.return_value = None
    mcp_svc._mcp_registry.delete.return_value = None
    res = mcp_svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection(image=False))
    by_item = {i.item: (i.outcome, i.reason) for i in res.items}
    assert by_item["policy_engine"] == ("skipped", "no registry record — nothing known to tear down")


def test_deleting_an_mcp_repo_with_a_malformed_record_is_refused_before_any_step(mcp_svc):
    mcp_svc._mcp_registry.get.side_effect = MalformedMcpRecordError("bad payload")
    with pytest.raises(ProjectError) as exc:
        mcp_svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection(image=False))
    assert exc.value.kind == "record_unreadable"
    assert mcp_svc.teardown_calls == []
    mcp_svc._rollout.delete_repo.assert_not_called()
    mcp_svc._mcp_registry.delete.assert_not_called()
    assert mcp_svc._get_repo(R) is not None


def test_an_unreadable_mcp_record_previews_unknown_instead_of_500(mcp_svc):
    mcp_svc._mcp_registry.get.side_effect = MalformedMcpRecordError("bad payload")
    states = {i.item: i.state for i in mcp_svc.preview_delete(project_id=P, repo_id=R).items}
    assert states["identity"] == "unknown"


def test_a_never_built_mcp_gateway_is_absent_not_a_manual_cleanup(mcp_svc):
    """Nothing was built, so nothing may still exist: no console hint, no amber banner."""
    preview = {i.item: (i.state, i.note) for i in mcp_svc.preview_delete(project_id=P, repo_id=R).items}
    assert preview["runtime"] == ("gone", "never built — nothing to remove")
    res = mcp_svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection(image=False))
    by_item = {i.item: (i.outcome, i.reason) for i in res.items}
    assert by_item["runtime"] == GATEWAY_ABSENT


def _gateway_outcome(svc):
    res = svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection(image=False))
    return next((i.outcome, i.reason) for i in res.items if i.item == "runtime")


def _started(svc):
    svc.append_deployment(repo_id=R, mcp_server_id=MCP_ID, stage="dev", image_tag="abc")


def test_a_failed_mcp_build_without_ids_may_have_left_a_gateway(mcp_svc):
    """A failed apply can leave a live gateway the repo row never recorded."""
    _started(mcp_svc)
    mcp_svc.append_deployment(repo_id=R, mcp_server_id=MCP_ID, stage="dev", image_tag="abc", outcome="failed")
    assert _gateway_outcome(mcp_svc) == GATEWAY_MAYBE


def test_an_in_flight_mcp_build_is_manual_not_never_built(mcp_svc):
    """A build in flight (or one that died mid-apply) has a ``started`` row and no terminal row;
    ``cicd_status`` is not touched by a push build, so only the history can tell."""
    _started(mcp_svc)
    _set_repo(mcp_svc, cicd_status="promoting")
    preview = {i.item: i.state for i in mcp_svc.preview_delete(project_id=P, repo_id=R).items}
    assert preview["runtime"] == "manual"
    assert _gateway_outcome(mcp_svc) == GATEWAY_MAYBE


def test_a_failed_status_without_any_build_row_is_still_never_built(mcp_svc):
    """A materialize failure sets ``cicd_status=failed`` for a repo that never built."""
    _set_repo(mcp_svc, cicd_status="failed")
    assert _gateway_outcome(mcp_svc) == GATEWAY_ABSENT


def test_an_unreadable_build_history_errs_on_manual(mcp_svc, monkeypatch):
    """The real store error (a ``ClientError``, which ``list_deployments`` would swallow into ``[]``, that
    is "never built") must count as built, so the gateway line stays manual."""
    table = MagicMock()
    table.query.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Query"
    )
    monkeypatch.setattr(mcp_svc, "_store", StateStore("agp-test", table=table))
    repo = SimpleNamespace(id=R)
    assert project_teardown._mcp_ever_built(mcp_svc, repo) is True


def test_the_mcp_preview_shows_the_built_gateway_as_manual_with_how_to_remove_it(mcp_svc):
    """State ``manual`` plus the how, so the modal renders a note instead of a ticked checkbox."""
    _set_repo(mcp_svc, gateway_arn="arn:gw", gateway_id="gw-1")
    preview = {i.item: (i.state, i.note) for i in mcp_svc.preview_delete(project_id=P, repo_id=R).items}
    assert preview["runtime"] == ("manual", GATEWAY_MANUAL[1])


def test_the_mcp_preview_reports_the_cedar_engine_with_its_real_plan(mcp_svc):
    """Previewed (it rides identity), gone without an engine id, unknown without a record; a
    ``gateway_id`` alone never deletes an engine, and the note says so rather than "removed"."""
    preview = {i.item: (i.state, i.note) for i in mcp_svc.preview_delete(project_id=P, repo_id=R).items}
    assert preview["policy_engine"] == ("present", "removed with the Entra MCP app")
    mcp_svc._mcp_registry.get.return_value = SimpleNamespace(
        id=MCP_ID, entra_app_id="app-1", entra_sp_id="sp-1", cedar_policy_engine_id=None
    )
    states = {i.item: i.state for i in mcp_svc.preview_delete(project_id=P, repo_id=R).items}
    assert states["policy_engine"] == "gone"
    mcp_svc._mcp_registry.get.return_value = SimpleNamespace(
        id=MCP_ID,
        entra_app_id="app-1",
        entra_sp_id="sp-1",
        cedar_policy_engine_id=None,
        gateway_id="gw-1",
    )
    preview = {i.item: (i.state, i.note) for i in mcp_svc.preview_delete(project_id=P, repo_id=R).items}
    assert preview["policy_engine"] == ("gone", "none on record — an engine on the gateway is left in place")
    mcp_svc._mcp_registry.get.return_value = None
    states = {i.item: i.state for i in mcp_svc.preview_delete(project_id=P, repo_id=R).items}
    assert states["policy_engine"] == "unknown"
