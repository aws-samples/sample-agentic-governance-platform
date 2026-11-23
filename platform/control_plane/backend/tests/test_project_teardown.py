"""Service tests for the teardown cascade: ``delete_repo`` and its read-only ``preview_delete``, the guarded
``delete_project``, and the runtime, state, exec-role and identity steps they drive in each stage's account.

Every collaborator is faked (``MagicMock``, ``SimpleNamespace`` or a small class here); no live AWS,
Entra or GitHub.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from botocore.exceptions import ClientError
from conftest import FIXED, TEMPLATE_NAME
from seed import seed

from models.project import Project
from models.repository import (
    RepoDeleteItemResult,
    RepoDeletePreview,
    RepoDeleteSelection,
    Repository,
)
from models.tenant import TenantStageConfig
from services import project_materialize, project_runtime_teardown
from services.connection_service import ConnectionError
from services.github_repo_errors import GitHubRepoError
from services.github_repo_service import GitHubRepoService
from services.project_errors import ProjectError
from services.project_service import ProjectService
from services.project_store import _REPOSITORY_PK, _to_item
from services.tenant_credentials import StageUnresolvedError, TenantCredentialsError

P = "proj-1"


R = "repo-1"


AGENT_ID = "agent-1"


AGENT_NAME = "my_platform_agent"


AGENT_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/agent-1"


class _FakeDeleteRegistry:
    """Registry double for the teardown cascade — serves an Agent and captures deletes."""

    def __init__(self, agent):
        self.get_return = agent
        self.deleted = None
        self.delete_raises = None

    def get(self, agent_id):
        return self.get_return

    def delete(self, agent_id):
        if self.delete_raises:
            raise self.delete_raises
        self.deleted = agent_id
        return self.get_return


class _FakeDeleteIdentity:
    """Identity double — ``delete_runtime`` is sync; ``delete_identity`` is async (driven
    via the service's asyncio.run guard)."""

    def __init__(self):
        self.deleted_runtime_arns = []
        self.deleted_identity = False
        self.delete_runtime_raises = None
        self.delete_identity_raises = None
        self.runtime_exists_return = True
        self.runtime_exists_raises = None
        self.delete_runtime_clients = []
        self.exists_clients = []
        self.probed_runtime_arns = []

    @property
    def deleted_runtime_arn(self):
        """The LAST ARN deleted, or None."""
        return self.deleted_runtime_arns[-1] if self.deleted_runtime_arns else None

    def delete_runtime(self, agent_arn, *, control_client=None):
        self.delete_runtime_clients.append(control_client)
        if self.delete_runtime_raises:
            raise self.delete_runtime_raises
        try:
            gone = not self.runtime_exists(agent_arn, control_client=control_client)
        except (RuntimeError, ClientError):
            gone = False
        if gone:
            return
        self.deleted_runtime_arns.append(agent_arn)

    async def delete_identity(self, agent):
        if self.delete_identity_raises:
            raise self.delete_identity_raises
        self.deleted_identity = True

    def runtime_exists(self, agent_arn, *, control_client=None):
        self.exists_clients.append(control_client)
        self.probed_runtime_arns.append(agent_arn)
        if self.runtime_exists_raises:
            raise self.runtime_exists_raises
        return bool(agent_arn) and self.runtime_exists_return


class _FakeEcr:
    def __init__(self):
        self.delete_images_raises = None
        self.deleted_agent_id = None
        self.count_images_return = 1
        self.count_images_raises = None

    def delete_images(self, agent_id):
        if self.delete_images_raises:
            raise self.delete_images_raises
        self.deleted_agent_id = agent_id
        return 1

    def count_images(self, agent_id):
        if self.count_images_raises:
            raise self.count_images_raises
        return self.count_images_return


class _FakeS3:
    """Capture double for the runtime TF-state teardown."""

    def __init__(self):
        self.keys = [f"agentcore-runtime/{AGENT_ID}/dev/terraform.tfstate"]
        self.deleted_keys = []
        self.listed_prefix = None
        self.list_calls = 0
        self.raises = None
        self.delete_raises_for = {}
        self.endless_pages = False

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        if self.raises:
            raise self.raises
        self.listed_prefix = Prefix
        self.list_calls += 1
        if self.endless_pages:
            return {
                "Contents": [{"Key": f"{Prefix}p{self.list_calls}/terraform.tfstate"}],
                "NextContinuationToken": f"tok-{self.list_calls}",
            }
        matched = [k for k in self.keys if k.startswith(Prefix)]
        return {"Contents": [{"Key": k} for k in matched]} if matched else {}

    def delete_object(self, Bucket, Key):
        if self.raises:
            raise self.raises
        if Key in self.delete_raises_for:
            raise self.delete_raises_for[Key]
        self.deleted_keys.append(Key)


def _seed_repo(svc):
    """Persist a project + a materialized repo into the local stores for the cascade."""
    svc._save_project(
        Project(
            id=P,
            name="p1",
            connection_id="c1",
            tenant_id="default",
            description="d",
            created_by="op@x",
            created_at="2026-07-02T00:00:00+00:00",
            updated_at="2026-07-02T00:00:00+00:00",
        )
    )
    svc._save_repo(
        Repository(
            id=R,
            project_id=P,
            name="my-agent",
            repo_url="https://github.com/acme/my-agent",
            agent_id=AGENT_ID,
            template_name=TEMPLATE_NAME,
            cicd_status="provisioning",
            status="provisioning",
            created_by="op@x",
            created_at="2026-07-02T00:00:00+00:00",
            updated_at="2026-07-02T00:00:00+00:00",
        )
    )


@pytest.fixture
def delete_fakes():
    """A ProjectService wired with teardown fakes + a seeded project/repo."""
    agent = SimpleNamespace(
        id=AGENT_ID,
        name=AGENT_NAME,
        agent_arn=AGENT_ARN,
        agent_arns={},
        entra_app_id="app-1",
        entra_sp_id="sp-1",
        langfuse_project_id="clx-proj-1",
        langfuse_key_secret_name="langfuse-agent-agent-1-keys",
    )
    registry = _FakeDeleteRegistry(agent)
    identity = _FakeDeleteIdentity()
    github = MagicMock(spec=GitHubRepoService)
    github.repo_exists.return_value = True
    ecr = _FakeEcr()
    s3 = _FakeS3()
    langfuse = MagicMock(name="LangfuseProvisioningService")

    conn = MagicMock()
    conn.get_connection.return_value = SimpleNamespace(org="acme", base_url=None)
    conn.get_bearer_token.return_value = "ghp_secret"

    svc = ProjectService(
        table_name="",
        registry=registry,
        identity=identity,
        connection_service=conn,
        github_repo_service=github,
        ecr_image_service=ecr,
        langfuse_provisioning=langfuse,
        runtime_state_bucket="agp-tf-state",
        now=lambda: FIXED,
    )
    svc._s3 = s3
    iam = MagicMock(name="iam")
    svc._iam = iam
    _seed_repo(svc)
    return SimpleNamespace(
        svc=svc,
        registry=registry,
        identity=identity,
        github=github,
        ecr=ecr,
        s3=s3,
        iam=iam,
        langfuse=langfuse,
        agent=agent,
    )


def test_delete_repo_full_cascade_order_and_removes_record(delete_fakes):
    f = delete_fakes
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert [i.item for i in res.items if i.outcome == "deleted"] == [
        "github",
        "image",
        "runtime",
        "exec_role",
        "identity",
        "langfuse",
        "record",
    ]
    assert res.record_removed is True
    assert f.registry.deleted == AGENT_ID
    assert f.svc._get_repo(R) is None
    assert f.identity.deleted_runtime_arn == AGENT_ARN
    assert f.identity.deleted_identity is True
    assert f.ecr.deleted_agent_id == AGENT_ID


def test_delete_repo_keeps_record_when_a_selected_step_fails(delete_fakes):
    f = delete_fakes
    f.github.delete_repo.side_effect = GitHubRepoError("boom")
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert next(i for i in res.items if i.item == "github").outcome == "failed"
    assert "boom" not in (next(i for i in res.items if i.item == "github").reason or "")
    assert next(i for i in res.items if i.item == "record").outcome == "skipped"
    assert res.record_removed is False
    assert f.svc._get_repo(R) is not None


def test_delete_repo_respects_unchecked_items(delete_fakes):
    f = delete_fakes
    sel = RepoDeleteSelection(github=False, image=False, runtime=False, identity=False, record=True)
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=sel)

    outcomes = {i.item: i.outcome for i in res.items}
    assert outcomes["github"] == "skipped"
    assert outcomes["image"] == "skipped"
    assert outcomes["runtime"] == "skipped"
    assert outcomes["identity"] == "skipped"
    assert res.record_removed is True
    assert f.svc._get_repo(R) is None


def test_delete_repo_on_an_mcp_repo_runs_none_of_the_agent_teardown(delete_fakes, monkeypatch):
    f = delete_fakes
    mcp = SimpleNamespace(id="mcp-1", entra_app_id="app-9", entra_sp_id="sp-9")
    f.svc._mcp_registry = MagicMock()
    f.svc._mcp_registry.get.return_value = mcp
    f.svc._mcp_registry.delete.return_value = mcp
    torn = []

    async def fake_teardown(m):
        torn.append(m)
        return [RepoDeleteItemResult(item="identity", outcome="deleted")]

    monkeypatch.setattr("services.project_teardown.teardown_mcp_identity", fake_teardown)
    repo = f.svc._get_repo(R)
    seed(
        f.svc._store,
        _to_item(
            _REPOSITORY_PK,
            repo.model_copy(
                update={"artifact_kind": "mcp_server", "agent_id": None, "mcp_server_id": "mcp-1"}
            ),
        ),
    )

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection(image=False))

    assert [i.item for i in res.items if i.outcome == "deleted"] == ["github", "identity", "record"]
    assert torn == [mcp]
    assert f.ecr.deleted_agent_id is None
    assert f.identity.deleted_runtime_arns == [] and f.identity.deleted_identity is False
    assert f.s3.deleted_keys == []
    assert f.iam.mock_calls == []
    f.langfuse.delete_agent_project.assert_not_called()
    assert f.registry.deleted is None


def test_delete_repo_record_unchecked_never_removes_row(delete_fakes):
    f = delete_fakes
    sel = RepoDeleteSelection(record=False)
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=sel)

    assert res.record_removed is False
    assert f.svc._get_repo(R) is not None
    assert next(i for i in res.items if i.item == "record").outcome == "skipped"


def test_delete_repo_idempotent_when_agent_already_gone(delete_fakes):
    f = delete_fakes
    f.registry.get_return = None
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    by_item = {i.item: (i.outcome, i.reason) for i in res.items}
    no_record = ("skipped", "no registry record — nothing known to tear down")
    assert by_item["runtime"] == no_record
    assert by_item["identity"] == no_record
    assert by_item["langfuse"] == no_record
    assert by_item["record"] == ("skipped", "not found in the registry")
    assert res.record_removed is True
    assert f.svc._get_repo(R) is None


def test_delete_repo_unknown_repo_raises_not_found(delete_fakes):
    f = delete_fakes
    with pytest.raises(ProjectError) as e:
        f.svc.delete_repo(project_id=P, repo_id="nope", selection=RepoDeleteSelection())
    assert e.value.kind == "repo_not_found"


def test_delete_repo_wrong_project_raises_not_found(delete_fakes):
    f = delete_fakes
    with pytest.raises(ProjectError) as e:
        f.svc.delete_repo(project_id="other", repo_id=R, selection=RepoDeleteSelection())
    assert e.value.kind == "repo_not_found"


def test_delete_repo_deletes_tf_state_object(delete_fakes):
    f = delete_fakes
    f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())
    assert f.s3.deleted_keys == [f"agentcore-runtime/{AGENT_ID}/dev/terraform.tfstate"]


def test_delete_repo_state_delete_failure_does_not_fail_runtime(delete_fakes):
    f = delete_fakes
    f.s3.raises = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "DeleteObject")
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"
    assert res.record_removed is True


def test_delete_repo_record_delete_failure_keeps_row(delete_fakes):
    f = delete_fakes
    f.registry.delete_raises = RuntimeError("ddb down")
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert next(i for i in res.items if i.item == "record").outcome == "failed"
    assert res.record_removed is False
    assert f.svc._get_repo(R) is not None


def test_delete_repo_github_skipped_when_connection_missing(delete_fakes):
    f = delete_fakes
    f.svc._conn.get_connection.side_effect = ConnectionError("Unknown connection", kind="not_found")
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    github = next(i for i in res.items if i.item == "github")
    assert github.outcome == "skipped"
    assert "connection removed" in (github.reason or "")
    assert next(i for i in res.items if i.item == "record").outcome == "deleted"
    assert res.record_removed is True
    assert f.svc._get_repo(R) is None


def test_delete_repo_github_still_fails_on_other_connection_error(delete_fakes):
    f = delete_fakes
    f.svc._conn.get_connection.side_effect = ConnectionError("boom", kind="secret_error")
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert next(i for i in res.items if i.item == "github").outcome == "failed"
    assert res.record_removed is False
    assert f.svc._get_repo(R) is not None


def test_delete_cascade_removes_langfuse_project(delete_fakes):
    f = delete_fakes
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    f.langfuse.delete_agent_project.assert_called_once_with(f.agent)
    langfuse = next(i for i in res.items if i.item == "langfuse")
    assert langfuse.outcome == "deleted"


def test_delete_cascade_langfuse_absent_is_success(delete_fakes):
    f = delete_fakes
    f.langfuse.delete_agent_project.side_effect = RuntimeError("langfuse unreachable")
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    langfuse = next(i for i in res.items if i.item == "langfuse")
    assert langfuse.outcome == "failed"
    assert langfuse.reason == "RuntimeError"
    assert "unreachable" not in (langfuse.reason or "")
    assert next(i for i in res.items if i.item == "identity").outcome == "deleted"
    assert next(i for i in res.items if i.item == "record").outcome == "deleted"
    assert res.record_removed is True
    assert f.svc._get_repo(R) is None


def test_delete_cascade_langfuse_reported_failed_does_not_block_record(delete_fakes):
    """A Langfuse teardown failure is REPORTED (its own failed item) yet NON-BLOCKING:
    it must not feed the record-gating predicate, so the record is still deleted and the
    DDB row is gone. This is the reported-but-non-blocking guarantee."""
    f = delete_fakes
    f.langfuse.delete_agent_project.side_effect = RuntimeError("boom")
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert next(i for i in res.items if i.item == "langfuse").outcome == "failed"
    assert next(i for i in res.items if i.item == "record").outcome == "deleted"
    assert res.record_removed is True
    assert f.registry.deleted == AGENT_ID
    assert f.svc._get_repo(R) is None


def test_delete_cascade_identity_deselected_marks_langfuse_skipped(delete_fakes):
    """Langfuse is grouped with identity (the agent's own resource). Deselecting identity
    skips the Langfuse teardown too — but it is emitted as an explicit "skipped" item (never
    silently absent) so the orphaned Langfuse project stays VISIBLE in the result, while the
    record still deletes."""
    f = delete_fakes
    sel = RepoDeleteSelection(identity=False)
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=sel)

    assert next(i for i in res.items if i.item == "identity").outcome == "skipped"
    langfuse = next(i for i in res.items if i.item == "langfuse")
    assert langfuse.outcome == "skipped"
    f.langfuse.delete_agent_project.assert_not_called()
    assert next(i for i in res.items if i.item == "record").outcome == "deleted"
    assert res.record_removed is True
    assert f.svc._get_repo(R) is None


def test_delete_preview_unaffected(delete_fakes):
    f = delete_fakes
    preview = f.svc.preview_delete(project_id=P, repo_id=R)

    assert [i.item for i in preview.items] == [
        "github",
        "image",
        "runtime",
        "identity",
        "record",
    ]
    f.langfuse.delete_agent_project.assert_not_called()


def _preview_states(preview: RepoDeletePreview) -> dict:
    return {i.item: i.state for i in preview.items}


def test_preview_delete_all_present(delete_fakes):
    f = delete_fakes
    preview = f.svc.preview_delete(project_id=P, repo_id=R)

    states = _preview_states(preview)
    assert states["github"] == "present"
    assert states["image"] == "present"
    assert states["runtime"] == "present"
    assert states["identity"] == "present"
    assert states["record"] == "present"
    assert f.registry.deleted is None
    assert f.identity.deleted_runtime_arn is None
    assert f.ecr.deleted_agent_id is None
    f.github.delete_repo.assert_not_called()


def test_preview_delete_github_gone_when_connection_missing(delete_fakes):
    f = delete_fakes
    f.svc._conn.get_connection.side_effect = ConnectionError("Unknown connection", kind="not_found")
    states = _preview_states(f.svc.preview_delete(project_id=P, repo_id=R))
    assert states["github"] == "gone"


def test_preview_delete_github_gone_when_repo_absent(delete_fakes):
    f = delete_fakes
    f.github.repo_exists.return_value = False
    states = _preview_states(f.svc.preview_delete(project_id=P, repo_id=R))
    assert states["github"] == "gone"


def test_preview_delete_github_unknown_on_probe_error(delete_fakes):
    f = delete_fakes
    f.github.repo_exists.side_effect = GitHubRepoError("timeout")
    states = _preview_states(f.svc.preview_delete(project_id=P, repo_id=R))
    assert states["github"] == "unknown"


def test_preview_delete_image_gone_when_none(delete_fakes):
    f = delete_fakes
    f.ecr.count_images_return = 0
    states = _preview_states(f.svc.preview_delete(project_id=P, repo_id=R))
    assert states["image"] == "gone"


def test_preview_delete_image_unknown_on_probe_error(delete_fakes):
    f = delete_fakes
    f.ecr.count_images_raises = RuntimeError("boom")
    states = _preview_states(f.svc.preview_delete(project_id=P, repo_id=R))
    assert states["image"] == "unknown"


def test_preview_delete_runtime_gone_when_probe_says_gone(delete_fakes):
    f = delete_fakes
    f.identity.runtime_exists_return = False
    states = _preview_states(f.svc.preview_delete(project_id=P, repo_id=R))
    assert states["runtime"] == "gone"


def test_preview_delete_runtime_unknown_on_probe_error(delete_fakes):
    f = delete_fakes
    f.identity.runtime_exists_raises = RuntimeError("boom")
    states = _preview_states(f.svc.preview_delete(project_id=P, repo_id=R))
    assert states["runtime"] == "unknown"


def test_preview_delete_runtime_unknown_on_access_denied(delete_fakes):
    f = delete_fakes
    f.identity.runtime_exists_raises = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
        "GetAgentRuntime",
    )
    states = _preview_states(f.svc.preview_delete(project_id=P, repo_id=R))
    assert states["runtime"] == "unknown"


def test_preview_delete_runtime_unknown_when_agent_missing(delete_fakes):
    f = delete_fakes
    f.registry.get_return = None
    states = _preview_states(f.svc.preview_delete(project_id=P, repo_id=R))
    assert states["runtime"] == "unknown"
    assert states["identity"] == "unknown"


def test_preview_delete_identity_present_from_stored_ids(delete_fakes):
    f = delete_fakes
    states = _preview_states(f.svc.preview_delete(project_id=P, repo_id=R))
    assert states["identity"] == "present"


def test_preview_delete_identity_gone_when_no_ids(delete_fakes):
    f = delete_fakes
    f.registry.get_return = SimpleNamespace(id=AGENT_ID, agent_arn=None, entra_app_id=None, entra_sp_id=None)
    states = _preview_states(f.svc.preview_delete(project_id=P, repo_id=R))
    assert states["identity"] == "gone"


def test_preview_delete_unknown_repo_raises_not_found(delete_fakes):
    f = delete_fakes
    with pytest.raises(ProjectError) as e:
        f.svc.preview_delete(project_id=P, repo_id="nope")
    assert e.value.kind == "repo_not_found"


def test_preview_delete_wrong_project_raises_not_found(delete_fakes):
    f = delete_fakes
    with pytest.raises(ProjectError) as e:
        f.svc.preview_delete(project_id="other", repo_id=R)
    assert e.value.kind == "repo_not_found"


P_EMPTY = "proj-empty"


P_WITH_REPO = "proj-with-repo"


def _make_project_with(svc, project_id):
    """Persist an empty project container into the local store under ``project_id``."""
    svc._save_project(
        Project(
            id=project_id,
            name="p",
            connection_id="c1",
            tenant_id="default",
            description="d",
            created_by="op@x",
            created_at="2026-07-02T00:00:00+00:00",
            updated_at="2026-07-02T00:00:00+00:00",
        )
    )


@pytest.fixture
def delete_project_svc(project_service_fakes):
    """A service seeded with an empty project and one that holds a repository."""
    svc = project_service_fakes
    _make_project_with(svc, P_EMPTY)
    _make_project_with(svc, P_WITH_REPO)
    svc._save_repo(
        Repository(
            id="repo-in-p",
            project_id=P_WITH_REPO,
            name="my-agent",
            repo_url="https://github.com/acme/my-agent",
            agent_id="agent-1",
            template_name=TEMPLATE_NAME,
            cicd_status="provisioning",
            status="provisioning",
            created_by="op@x",
            created_at="2026-07-02T00:00:00+00:00",
            updated_at="2026-07-02T00:00:00+00:00",
        )
    )
    return svc


def test_delete_project_removes_empty_container(delete_project_svc):
    svc = delete_project_svc
    svc.delete_project(P_EMPTY)
    assert svc._get_project(P_EMPTY) is None


def test_delete_project_blocked_when_repos_exist(delete_project_svc):
    svc = delete_project_svc
    with pytest.raises(ProjectError) as e:
        svc.delete_project(P_WITH_REPO)
    assert e.value.kind == "has_repositories"
    assert svc._get_project(P_WITH_REPO) is not None


def test_delete_project_unknown_raises_not_found(delete_project_svc):
    svc = delete_project_svc
    with pytest.raises(ProjectError) as e:
        svc.delete_project("nope")
    assert e.value.kind == "not_found"


RUNTIME_STATE_BUCKET = "agp-tf-state"


def test_runtime_state_key_carries_the_stage_segment():
    """The key is `agentcore-runtime/{agent_id}/{stage}/terraform.tfstate`. Without the
    stage segment two stages share one state file and one runtime ARN."""
    from services.project_runtime_teardown import runtime_state_key

    assert runtime_state_key("a-1", "dev") == "agentcore-runtime/a-1/dev/terraform.tfstate"


@pytest.mark.parametrize("stage", ["dev", "prod", "uat", "staging", "eu-west-1"])
def test_runtime_state_key_is_distinct_per_stage_for_ANY_stage_name(stage):
    """The stage set is OPEN — a tenant may carry `uat` only, or per-region stages. The
    key must scope by whatever the stage is CALLED, never by a dev/prod literal."""
    from services.project_runtime_teardown import runtime_state_key

    key = runtime_state_key("a-1", stage)
    assert key == f"agentcore-runtime/a-1/{stage}/terraform.tfstate"
    assert f"/{stage}/" in key
    assert key != runtime_state_key("a-1", "other")


def test_runtime_state_key_agrees_with_the_buildspec_that_actually_writes_it():
    """The two producers must agree. The buildspec is the WRITER (`terraform init
    -backend-config="key=…"`); Python only deletes/inspects. A drifted literal means the
    delete path targets an object that does not exist and state leaks silently — so this
    asserts against the REAL buildspec.yml rather than a copied string."""
    import re
    from pathlib import Path

    from services.project_runtime_teardown import runtime_state_key

    buildspec = (
        Path(__file__).resolve().parents[2] / "infrastructure" / "modules" / "codebuild" / "buildspec.yml"
    )
    assert buildspec.is_file(), buildspec

    keys = re.findall(r'-backend-config="key=(agentcore-runtime/[^"]+)"', buildspec.read_text())
    assert len(keys) == 1, f"expected exactly one runtime state key, got {keys}"
    resolved = keys[0].replace("$AGENT_ID", "a-1").replace("$STAGE", "dev")
    assert resolved == runtime_state_key("a-1", "dev")


def test_delete_repo_deletes_the_tf_state_of_EVERY_stage(delete_fakes):
    """The consequence of stage-scoping the key: there is no longer ONE state object per
    agent, there are N (one per stage ever deployed). A single-key delete would leave every
    other stage's state behind, so the teardown deletes the whole
    `agentcore-runtime/{agent_id}/` prefix — which also covers a stage the tenant has since
    dropped, whose state a `tenant.stages` loop would never see."""
    f = delete_fakes
    f.s3.keys = [
        f"agentcore-runtime/{AGENT_ID}/dev/terraform.tfstate",
        f"agentcore-runtime/{AGENT_ID}/prod/terraform.tfstate",
        f"agentcore-runtime/{AGENT_ID}/uat/terraform.tfstate",
    ]

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert f.s3.listed_prefix == f"agentcore-runtime/{AGENT_ID}/"
    assert sorted(f.s3.deleted_keys) == sorted(f.s3.keys)
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"


def test_delete_repo_tolerates_an_agent_with_no_stored_state(delete_fakes):
    """Nothing ever deployed (or the state was already reclaimed) ⇒ an empty listing. That is
    an already-done success, not a failure — and no delete call is made."""
    f = delete_fakes
    f.s3.keys = []

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert f.s3.deleted_keys == []
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"


def test_state_listing_stops_at_the_page_ceiling_instead_of_paging_forever(delete_fakes):
    """A store that always returns a continuation token would hang the whole teardown. The
    listing stops at ``_MAX_STATE_LIST_PAGES``; truncating is allowed by this path's
    best-effort contract, but it IS a leak, so it must be bounded rather than infinite."""
    from services.project_runtime_teardown import _MAX_STATE_LIST_PAGES

    f = delete_fakes
    f.s3.endless_pages = True

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert f.s3.list_calls == _MAX_STATE_LIST_PAGES
    assert len(f.s3.deleted_keys) == _MAX_STATE_LIST_PAGES
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"


def test_one_failing_state_delete_does_not_strand_the_others(delete_fakes):
    """Per-key tolerance. The objects are independent, so a ``ClientError`` on the SECOND of
    three must not abandon the third — a partial reclaim beats leaking two objects because one
    was denied. Still never raises: the runtime item stays ``deleted``."""
    f = delete_fakes
    dev, prod, uat = (
        f"agentcore-runtime/{AGENT_ID}/dev/terraform.tfstate",
        f"agentcore-runtime/{AGENT_ID}/prod/terraform.tfstate",
        f"agentcore-runtime/{AGENT_ID}/uat/terraform.tfstate",
    )
    f.s3.keys = [dev, prod, uat]
    f.s3.delete_raises_for = {
        prod: ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "DeleteObject")
    }

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert f.s3.deleted_keys == [dev, uat]
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"


def test_a_failed_state_listing_is_swallowed_and_deletes_nothing(delete_fakes):
    """If the LISTING itself fails there is no inventory to act on — log and leave the runtime
    item successful (the runtime is gone; its state is a best-effort extra)."""
    f = delete_fakes
    f.s3.raises = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "ListObjectsV2")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert f.s3.deleted_keys == []
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"


LEGACY_EXEC_ROLE = f"{AGENT_NAME}-agentcore-exec"


def _exec_role(stage):
    """The per-stage exec-role name, spelled out here rather than imported from the producer.

    Deliberately a restated literal: importing `agentcore_exec_role_name` would make every
    assertion below a tautology (`f(x) == f(x)`) that a broken producer satisfies. The literal is
    what pins the producer, and the producer is separately pinned against the real main.tf by
    `test_exec_role_name_matches_the_terraform_module_that_creates_it`."""
    return f"{AGENT_NAME}-{stage}-agentcore-exec"


def _iam_client_error(code, op):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, op)


def _deleted_roles(f):
    """The set of RoleNames `delete_role` was called with — order-free by construction.

    A `set`, deliberately: the reclaim iterates `agent_arns`, so an assertion on a LIST would be
    an assertion about dict iteration order, which is the defect this epic already had to repair
    once in `test_preview_reports_the_runtime_PRESENT_…`. Ordering that IS load-bearing (the
    policy before its own role) is asserted separately, per role, off `mock_calls`."""
    return {c.kwargs["RoleName"] for c in f.iam.delete_role.call_args_list}


def _deleted_role_policies(f):
    """The set of RoleNames `delete_role_policy` was called with. See :func:`_deleted_roles`."""
    return {c.kwargs["RoleName"] for c in f.iam.delete_role_policy.call_args_list}


def _agentcore_main_tf():
    from pathlib import Path

    main_tf = (
        Path(__file__).resolve().parents[2] / "infrastructure" / "modules" / "agentcore_runtime" / "main.tf"
    )
    assert main_tf.is_file(), main_tf
    return main_tf.read_text()


def _tf_exec_role_name(body, agent_name, stage):
    """The exec-role name the REAL module would create for ``agent_name`` at ``stage``."""
    import re

    code = body
    refs = re.findall(
        r'^\s*resource\s+"aws_iam_role"\s+"exec"\s*\{\s*\n\s*name\s*=\s*local\.(\w+)\s*$',
        code,
        re.M,
    )
    assert len(refs) == 1, f"expected exactly one exec role name reference, got {refs}"
    templates = re.findall(rf'^\s*{re.escape(refs[0])}\s*=\s*"([^"]+)"', code, re.M)
    assert len(templates) == 1, f"expected exactly one {refs[0]} definition, got {templates}"
    rendered = templates[0].replace("${var.agent_name}", agent_name).replace("${var.stage}", stage)
    assert "${" not in rendered, f"unresolved interpolation in {rendered!r}"
    return rendered


def test_exec_role_name_matches_the_terraform_module_that_creates_it(delete_fakes):
    """The name is pinned against the REAL module, not a copied string."""
    from services.project_runtime_teardown import agentcore_exec_role_name

    body = _agentcore_main_tf()

    for stage in ("dev", "prod"):
        assert _tf_exec_role_name(body, AGENT_NAME, stage) == agentcore_exec_role_name(AGENT_NAME, stage)
    assert agentcore_exec_role_name(AGENT_NAME, "dev") != agentcore_exec_role_name(AGENT_NAME, "prod")
    assert agentcore_exec_role_name(AGENT_NAME, "dev") == _exec_role("dev")
    assert AGENT_ID not in agentcore_exec_role_name(AGENT_NAME, "dev")
    from services.project_runtime_teardown import legacy_agentcore_exec_role_name

    assert legacy_agentcore_exec_role_name(AGENT_NAME) == LEGACY_EXEC_ROLE
    assert legacy_agentcore_exec_role_name(AGENT_NAME) != agentcore_exec_role_name(AGENT_NAME, "dev")


def test_the_terraform_name_extraction_cannot_be_satisfied_by_a_COMMENT(delete_fakes):
    """Non-vacuity for the guard above: it must read CODE, never the module's prose.

    main.tf's comments state the role name's contract in words AND quote the template verbatim, so
    a source-as-text assertion is one careless regex away from passing against a comment while the
    resource says something else. Fed synthetic modules whose ONLY mention of the name is a
    comment, the extraction must refuse (assert) rather than return a name — which is what makes
    the real pin load-bearing instead of decorative. This is also the test that justifies dropping
    the comment-stripping pre-pass: the ANCHOR is the defense, and here it is exercised directly."""
    import pytest

    commented_out = """
# resource "aws_iam_role" "exec" {
#   name = local.exec_role_name
# }
locals {
  exec_role_name = "${var.agent_name}-${var.stage}-agentcore-exec"
}
"""
    template_only_in_prose = """
resource "aws_iam_role" "exec" {
  name = local.exec_role_name
}
locals {
  # exec_role_name = "${var.agent_name}-${var.stage}-agentcore-exec"
}
"""
    for source in (commented_out, template_only_in_prose):
        with pytest.raises(AssertionError):
            _tf_exec_role_name(source, AGENT_NAME, "dev")

    assert _tf_exec_role_name(_agentcore_main_tf(), AGENT_NAME, "dev") == _exec_role("dev")


def test_exec_role_inline_policy_name_matches_the_terraform_module(delete_fakes):
    """The inline policy name is pinned to the module too. IAM refuses to delete a role that
    still carries an inline policy, so a drifted policy name doesn't just leak the policy —
    the ROLE delete then fails with DeleteConflict and nothing is reclaimed."""
    import re
    from pathlib import Path

    from services.project_runtime_teardown import _AGENTCORE_EXEC_POLICY_NAME

    main_tf = (
        Path(__file__).resolve().parents[2] / "infrastructure" / "modules" / "agentcore_runtime" / "main.tf"
    )
    body = main_tf.read_text()
    names = re.findall(r'resource\s+"aws_iam_role_policy"\s+"exec"\s*\{\s*\n\s*name\s*=\s*"([^"]+)"', body)
    assert len(names) == 1, f"expected exactly one exec inline policy name, got {names}"
    assert names[0] == _AGENTCORE_EXEC_POLICY_NAME


def test_delete_cascade_reclaims_the_exec_role_policy_first_then_the_role(delete_fakes):
    """ORDER is load-bearing: IAM rejects `DeleteRole` on a role that still has an inline
    policy (`DeleteConflict`). Deleting the policy first is what makes the role deletable."""
    f = delete_fakes
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert f.iam.mock_calls == [
        call.delete_role_policy(RoleName=LEGACY_EXEC_ROLE, PolicyName="runtime-exec"),
        call.delete_role(RoleName=LEGACY_EXEC_ROLE),
    ]
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"
    assert res.record_removed is True


def test_delete_cascade_treats_an_ALREADY_GONE_exec_role_as_success(delete_fakes):
    """Idempotence. A repo predating the module, or a second delete attempt, has no role —
    `NoSuchEntity` on EITHER call is the already-done state, not a failure. It must not flip
    the runtime item, and the policy's NoSuchEntity must not stop the role delete from being
    attempted (they are reported independently by IAM)."""
    f = delete_fakes
    f.iam.delete_role_policy.side_effect = _iam_client_error("NoSuchEntity", "DeleteRolePolicy")
    f.iam.delete_role.side_effect = _iam_client_error("NoSuchEntity", "DeleteRole")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    f.iam.delete_role.assert_called_once_with(RoleName=LEGACY_EXEC_ROLE)
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"
    assert res.record_removed is True


def test_an_iam_failure_does_NOT_abort_the_cascade(delete_fakes):
    """Same contract as `_delete_runtime_state`: the runtime being gone matters more than the role."""
    f = delete_fakes
    f.iam.delete_role.side_effect = _iam_client_error("AccessDenied", "DeleteRole")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"
    assert f.identity.deleted_identity is True
    f.langfuse.delete_agent_project.assert_called_once_with(f.agent)
    assert res.record_removed is True
    assert f.svc._get_repo(R) is None
    assert {i.item: i.outcome for i in res.items} == {
        "github": "deleted",
        "image": "deleted",
        "runtime": "deleted",
        "exec_role": "failed",
        "identity": "deleted",
        "langfuse": "deleted",
        "record": "deleted",
    }


def test_an_iam_policy_delete_failure_still_attempts_the_role(delete_fakes):
    """A non-NoSuchEntity failure on the POLICY must not skip the role delete. The two are
    independent reclaims and a partial one beats leaking both — same per-key tolerance the
    state-object teardown has."""
    f = delete_fakes
    f.iam.delete_role_policy.side_effect = _iam_client_error("AccessDenied", "DeleteRolePolicy")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    f.iam.delete_role.assert_called_once_with(RoleName=LEGACY_EXEC_ROLE)
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"


def test_deselecting_the_runtime_leaves_the_exec_role_ALONE(delete_fakes):
    """The reclaim rides the runtime selection. An operator who unchecks `runtime` is keeping
    the runtime, which still NEEDS its exec role to pull images and write logs — deleting the
    role would silently break a runtime the operator deliberately kept."""
    f = delete_fakes
    sel = RepoDeleteSelection(runtime=False)

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=sel)

    assert f.iam.mock_calls == []
    assert next(i for i in res.items if i.item == "runtime").outcome == "skipped"


def test_the_exec_role_is_reclaimed_even_when_the_runtime_arn_is_already_gone(delete_fakes):
    """The role outlives the runtime: `_delete_runtime` no-ops when there is no ARN, but the
    ROLE is what leaks account-globally and blocks a re-materialize under the same name. The
    reclaim only needs the agent's name, so a cleared/never-written ARN must not strand it."""
    f = delete_fakes
    f.agent.agent_arn = None

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    f.iam.delete_role.assert_called_once_with(RoleName=LEGACY_EXEC_ROLE)
    assert f.identity.deleted_runtime_arn is None
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"


def test_an_agent_with_no_name_reclaims_nothing_rather_than_guessing(delete_fakes):
    """No name ⇒ no derivable role name. Calling IAM with an empty/None `RoleName` would at
    best be a wasted denied call and at worst target something else — so it is skipped."""
    f = delete_fakes
    f.agent.name = None

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert f.iam.mock_calls == []
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"


def test_delete_cascade_reclaims_EVERY_STAGE_exec_role(delete_fakes):
    """N stages ⇒ N per-stage roles reclaimed (plus the legacy name, see the sibling below).

    The stages come from `agent_arns`' KEYS — the only evidence on the record of what was ever
    deployed. Deleting one name for an agent with three stage roles leaks two account-global
    names, silently, because every miss reads as `NoSuchEntity`/success."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN, "uat": UAT_ARN}
    f.agent.agent_arn = UAT_ARN

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert _deleted_roles(f) == {
        _exec_role("dev"),
        _exec_role("prod"),
        _exec_role("uat"),
        LEGACY_EXEC_ROLE,
    }
    assert _deleted_role_policies(f) == _deleted_roles(f)
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"
    assert res.record_removed is True


def test_delete_cascade_ALSO_reclaims_the_pre_T1b_UN_STAGE_SCOPED_name(delete_fakes):
    """THE MIGRATION CASE."""
    f = delete_fakes
    f.agent.agent_arns = {"prod": PROD_ARN}

    f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert LEGACY_EXEC_ROLE in _deleted_roles(f)
    assert _deleted_roles(f) == {_exec_role("prod"), LEGACY_EXEC_ROLE}


def test_a_legacy_scalar_only_record_does_NOT_derive_an_unknown_stage_role(delete_fakes):
    """`resolve_runtime_arns` keys a scalar-only record under the `UNKNOWN_STAGE` PLACEHOLDER,
    which is not a stage anyone deployed to. Interpolating it would target
    `{name}-unknown-agentcore-exec` — a name Terraform never created — producing a guaranteed
    `NoSuchEntity` that the cascade reads as success while the role that DOES exist (the legacy
    un-scoped one) went untouched. This is the whole registry's shape today, so it is the case a
    naive "iterate the keys" would break on."""
    from models.agent import UNKNOWN_STAGE

    f = delete_fakes
    f.agent.agent_arns = {}

    f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert _deleted_roles(f) == {LEGACY_EXEC_ROLE}
    assert all(UNKNOWN_STAGE not in name for name in _deleted_roles(f))


def test_the_reclaim_deletes_each_roles_POLICY_before_that_ROLE(delete_fakes):
    """Order is load-bearing PER ROLE, and asserting it globally would be order-dependent noise.

    IAM rejects `DeleteRole` while the role still carries an inline policy (`DeleteConflict`), so
    what must hold is: for every role, its own policy delete precedes its own role delete. The
    order the ROLES are visited in is dict-iteration order and deliberately NOT asserted."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}

    f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    seen = [(c[0], c.kwargs["RoleName"]) for c in f.iam.mock_calls]
    for role in (_exec_role("dev"), _exec_role("prod"), LEGACY_EXEC_ROLE):
        assert seen.index(("delete_role_policy", role)) < seen.index(("delete_role", role)), role
    assert len(seen) == 6


def test_one_stage_exec_role_failing_does_NOT_strand_the_others(delete_fakes):
    """Per-ROLE tolerance, mirroring `_delete_runtime_state`'s per-KEY tolerance. An
    `AccessDenied` on one role (the live shape while the task-role grant is missing) must not
    abandon the rest — a partial reclaim strictly beats leaking every remaining name, and the
    cascade still must not fail."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    boom = _iam_client_error("AccessDenied", "DeleteRole")
    f.iam.delete_role.side_effect = [boom, None, None]

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert _deleted_roles(f) == {_exec_role("dev"), _exec_role("prod"), LEGACY_EXEC_ROLE}
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"
    assert res.record_removed is True


def test_the_reclaim_name_list_is_deduped_and_holds_no_unresolved_placeholder(delete_fakes):
    """The list handed to IAM must contain no duplicate and no fabricated segment.

    Asserted on the derivation directly (rather than through a cascade run) because the pathology
    it guards is a name-SHAPE one: a duplicate wastes a call and makes the count unassertable,
    and `None`/`unknown` reaching a `RoleName` is the "delete something that never existed" bug
    in miniature."""

    agent = SimpleNamespace(
        agent_arns={"dev": DEV_ARN, "prod": PROD_ARN}, agent_arn=PROD_ARN, name=AGENT_NAME
    )
    names = [name for _, name in project_runtime_teardown._exec_role_targets_to_delete(agent, AGENT_NAME)]

    assert len(names) == len(set(names))
    assert sorted(names) == sorted([_exec_role("dev"), _exec_role("prod"), LEGACY_EXEC_ROLE])
    for name in names:
        assert "None" not in name and "unknown" not in name
        assert name.startswith(f"{AGENT_NAME}-") and name.endswith("-agentcore-exec")
        assert len(name) <= 64


def test_an_empty_or_None_stage_key_is_SKIPPED_rather_than_interpolated(delete_fakes):
    """A malformed map (an empty-string or None key, e.g. a half-written envelope) must not
    produce `{name}--agentcore-exec` or `{name}-None-agentcore-exec`. Those are legal-LOOKING
    names that exist nowhere, so every one of them is a silent no-op that displaces nothing."""

    agent = SimpleNamespace(
        agent_arns={"": DEV_ARN, None: PROD_ARN, "dev": UAT_ARN}, agent_arn=None, name=AGENT_NAME
    )
    names = [name for _, name in project_runtime_teardown._exec_role_targets_to_delete(agent, AGENT_NAME)]

    assert sorted(names) == sorted([_exec_role("dev"), LEGACY_EXEC_ROLE])


DEV_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/agent-1_dev-aaa"


PROD_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/agent-1_prod-bbb"


UAT_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/agent-1_uat-ccc"


def test_delete_cascade_deletes_EVERY_runtime_in_the_per_stage_map(delete_fakes):
    """N runtimes ⇒ N deletes."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN, "uat": UAT_ARN}
    f.agent.agent_arn = UAT_ARN

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert sorted(f.identity.deleted_runtime_arns) == sorted([DEV_ARN, PROD_ARN, UAT_ARN])
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"
    assert res.record_removed is True


def test_delete_cascade_deletes_EXACTLY_ONE_runtime_for_a_legacy_record(delete_fakes):
    """The other half of the same contract: a LEGACY record (no `agent_arns` key at all) must
    still delete exactly its ONE scalar runtime — not zero, and not a fabricated per-stage set.
    Every agent in the registry today is this shape, so a regression here breaks every delete."""
    f = delete_fakes
    f.agent.agent_arns = {}

    f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert f.identity.deleted_runtime_arns == [AGENT_ARN]


def test_delete_cascade_ignores_the_scalar_when_the_map_names_it_too(delete_fakes):
    """The buildspec writes BOTH the map entry and the scalar, so the scalar is a
    DUPLICATE of whichever stage deployed last. Unioning them would call `DeleteAgentRuntime`
    on that runtime twice — harmless but dishonest in the logs, and it would make the N-delete
    count unassertable. A populated map is the complete inventory."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    f.agent.agent_arn = PROD_ARN

    f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert sorted(f.identity.deleted_runtime_arns) == sorted([DEV_ARN, PROD_ARN])


def test_delete_cascade_still_deletes_a_scalar_the_map_does_NOT_name(delete_fakes):
    """The migration shape: an agent deployed under pre-stage-scoping code (scalar written, no map), then
    redeployed once under stage scoping (map gains `prod` only)."""
    f = delete_fakes
    f.agent.agent_arns = {"prod": PROD_ARN}
    f.agent.agent_arn = AGENT_ARN

    f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert sorted(f.identity.deleted_runtime_arns) == sorted([AGENT_ARN, PROD_ARN])


def test_one_runtime_delete_failing_does_NOT_strand_the_others(delete_fakes):
    """Per-ARN tolerance, mirroring `_delete_runtime_state`'s per-KEY tolerance and for the
    same reason: the runtimes are independent, so a partial reclaim strictly beats abandoning
    the rest. The step still reports FAILED (unlike the best-effort state/IAM reclaims — a
    runtime that survives is a live, billing resource, and the row must stay for retry), but
    only AFTER every other runtime was attempted."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN, "uat": UAT_ARN}
    f.agent.agent_arn = UAT_ARN
    boom = RuntimeError("AccessDenied")

    real_delete = f.identity.delete_runtime

    def flaky(agent_arn, *, control_client=None):
        if agent_arn == PROD_ARN:
            raise boom
        real_delete(agent_arn, control_client=control_client)

    f.identity.delete_runtime = flaky

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert sorted(f.identity.deleted_runtime_arns) == sorted([DEV_ARN, UAT_ARN])
    assert next(i for i in res.items if i.item == "runtime").outcome == "failed"
    assert res.record_removed is False


def test_a_failed_runtime_delete_logs_the_RID_and_NOT_the_account_bearing_ARN(delete_fakes, caplog):
    """The per-runtime failure log must name the runtime WITHOUT its AWS account id.

    A full runtime ARN is `arn:aws:bedrock-agentcore:<region>:<ACCOUNT>:runtime/<rid>`, and a hard
    project rule bans an account id anywhere — logs included. The RID alone still identifies the
    runtime uniquely, so the line stays chaseable; the sibling reclaims are careful the same way
    (the state delete logs the S3 key, the exec-role reclaim logs the role name).

    Asserted STRUCTURALLY on the emitted log records, not on the source text: the account id is
    read off the ARN constant by position rather than written as a literal, so this guard cannot
    be defeated by prose (there is no forbidden string in this file for a comment to quote), and
    it stays true if the fixture ARNs change."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    f.agent.agent_arn = PROD_ARN
    f.identity.delete_runtime_raises = RuntimeError("AccessDenied")

    with caplog.at_level(logging.ERROR, logger="services.project_runtime_teardown"):
        f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    failures = [
        r.getMessage()
        for r in caplog.records
        if r.name == "services.project_runtime_teardown" and "runtime delete failed" in r.getMessage()
    ]
    assert len(failures) == 2
    dev_rid, prod_rid = DEV_ARN.rsplit("/", 1)[-1], PROD_ARN.rsplit("/", 1)[-1]
    assert {dev_rid, prod_rid} == {m.rsplit(" ", 1)[-1] for m in failures}
    account_id = DEV_ARN.split(":")[4]
    region = DEV_ARN.split(":")[3]
    for message in failures:
        assert account_id not in message
        assert region not in message
        assert "arn:" not in message


def test_the_exec_role_is_still_reclaimed_when_a_runtime_delete_fails(delete_fakes):
    """The IAM reclaim must not be skipped by a runtime failure."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.identity.delete_runtime_raises = RuntimeError("AccessDenied")

    f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert _deleted_roles(f) == {_exec_role("dev"), LEGACY_EXEC_ROLE}


def test_preview_reports_the_runtime_PRESENT_when_ANY_stage_runtime_survives(delete_fakes):
    """The preview offers ONE `runtime` line-item for what may be N runtimes. Aggregating
    honestly means `present` if ANY of them is still there — a
    `gone` while prod's runtime is live would let an operator uncheck the box and leak it.

    ORDER-INDEPENDENT ON PURPOSE, and the setup is chosen so it stays that way while still
    proving the MAP is what gets probed. The first cut left the fixture scalar naming a THIRD
    runtime the map does not, so the real probe list was three ARNs and
    `sorted(probed) == [DEV, PROD]` held only because `dict` iteration happened to reach the live
    runtime second and short-circuit before the scalar — reversing the literal map order alone
    reddened it. Now the scalar mirrors a stage the map already names (the buildspec's dual write, as the
    sibling GONE test does) so the candidate set is exactly the two mapped ARNs in any order, and
    the LIVE one is the stage the scalar does NOT name — so an implementation that probed only
    the scalar could never reach it, whichever order the map yields.

    Every assertion below holds under BOTH map orders. In particular there is deliberately NO
    assertion on the probe COUNT or the LAST ARN probed here: those hold only if the loop
    short-circuits, and under a non-short-circuiting implementation they flip with the dict order
    — the very dependency this test is being repaired for. The short-circuit is pinned
    order-freely by the sibling test below instead."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    f.agent.agent_arn = PROD_ARN
    probed = []

    def probe(arn, *, control_client=None):
        probed.append(arn)
        return arn == DEV_ARN

    f.identity.runtime_exists = probe

    prev = f.svc.preview_delete(project_id=P, repo_id=R)

    assert next(i for i in prev.items if i.item == "runtime").state == "present"
    assert DEV_ARN in probed
    assert set(probed) <= {DEV_ARN, PROD_ARN}


def test_the_preview_STOPS_probing_once_one_runtime_is_proved_present(delete_fakes):
    """The short-circuit, pinned WITHOUT depending on dict order.

    `present` is settled by the first live runtime, so the probe must not keep calling the
    control plane once it has its answer. Making EVERY runtime live is what makes this
    order-free: a short-circuiting loop probes exactly ONE whichever key comes first, while one
    that probes them all makes three calls in every order. Asserting the count on a map with one
    live entry could not distinguish the two without also fixing which key is visited first."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN, "uat": UAT_ARN}
    f.agent.agent_arn = UAT_ARN
    probed = []

    def probe(arn, *, control_client=None):
        probed.append(arn)
        return True

    f.identity.runtime_exists = probe

    prev = f.svc.preview_delete(project_id=P, repo_id=R)

    assert next(i for i in prev.items if i.item == "runtime").state == "present"
    assert len(probed) == 1
    assert probed[0] in {DEV_ARN, PROD_ARN, UAT_ARN}


def test_preview_reports_the_runtime_GONE_only_when_EVERY_stage_runtime_is_gone(delete_fakes):
    """The converse. `gone` means "no runtime left to delete", so it must require ALL of them."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    f.agent.agent_arn = PROD_ARN
    probed = []

    def probe(arn, *, control_client=None):
        probed.append(arn)
        return False

    f.identity.runtime_exists = probe

    prev = f.svc.preview_delete(project_id=P, repo_id=R)

    assert sorted(probed) == sorted([DEV_ARN, PROD_ARN])
    assert next(i for i in prev.items if i.item == "runtime").state == "gone"


def test_preview_reports_UNKNOWN_when_a_probe_raises_and_none_proved_present(delete_fakes, caplog):
    """An AccessDenied probe is AMBIGUOUS — a live runtime behind an IAM/SCP/region misconfig
    returns it too. It must never be read as `gone`. But a raise must not HIDE a runtime another
    probe proved present either: `present` outranks `unknown` outranks `gone`.

    Also pins that the ambiguous-probe log names the RID and NOT the account-bearing ARN, the
    same rule as the delete-failure log — asserted on the emitted record with the account id
    derived from the ARN rather than written as a literal."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    f.agent.agent_arn = PROD_ARN

    def probe(arn, *, control_client=None):
        if arn == PROD_ARN:
            raise RuntimeError("AccessDenied")
        return False

    f.identity.runtime_exists = probe

    with caplog.at_level(logging.ERROR, logger="services.project_runtime_teardown"):
        prev = f.svc.preview_delete(project_id=P, repo_id=R)

    assert next(i for i in prev.items if i.item == "runtime").state == "unknown"

    probe_failures = [
        r.getMessage()
        for r in caplog.records
        if r.name == "services.project_runtime_teardown" and "runtime probe failed" in r.getMessage()
    ]
    assert len(probe_failures) == 1
    assert probe_failures[0].endswith(PROD_ARN.rsplit("/", 1)[-1])
    assert DEV_ARN.split(":")[4] not in probe_failures[0]
    assert "arn:" not in probe_failures[0]


def test_a_DENIED_reclaim_is_REPORTED_as_a_failed_exec_role_item(delete_fakes):
    """The honesty half. A denied reclaim must be visible on the cascade result, because the
    operator is the only thing that can clean up an account-global name AGP could not delete —
    and they cannot act on a report that says `deleted`."""
    f = delete_fakes
    f.iam.delete_role.side_effect = _iam_client_error("AccessDenied", "DeleteRole")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome == "failed"
    assert LEGACY_EXEC_ROLE in item.reason
    assert "ClientError" in item.reason


def test_a_DENIED_reclaim_still_removes_the_RECORD(delete_fakes):
    """The non-blocking half, and it is the reason `exec_role` is a separate item rather than a
    raise inside the runtime step. `failed` normally BLOCKS the record delete (the row is kept for
    retry) — but a retry cannot fix a missing IAM grant, so gating the row on this would trap
    every row behind a leaked role while the runtime, repo, image and identity are all gone. So
    `exec_role` is excluded from the record-gating predicate, exactly like `langfuse`."""
    f = delete_fakes
    f.iam.delete_role.side_effect = _iam_client_error("AccessDenied", "DeleteRole")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert next(i for i in res.items if i.item == "exec_role").outcome == "failed"
    assert next(i for i in res.items if i.item == "record").outcome == "deleted"
    assert res.record_removed is True
    assert f.svc._get_repo(R) is None


def test_a_denied_POLICY_delete_alone_also_reports_the_exec_role_item(delete_fakes):
    """The policy delete is half the reclaim, and skipping it does not merely leak a policy — IAM
    then refuses `DeleteRole` with `DeleteConflict`, so it leaks the whole role. A denial there is
    the same operator-actionable fact and must report identically."""
    f = delete_fakes
    f.iam.delete_role_policy.side_effect = _iam_client_error("AccessDenied", "DeleteRolePolicy")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome == "failed"
    assert LEGACY_EXEC_ROLE in item.reason


def test_the_exec_role_reason_LEAKS_NOTHING_from_the_exception_BODY(delete_fakes):
    """THE TOKEN-LEAK FENCE, applied to a message this task newly SURFACES."""
    f = delete_fakes
    f.iam.delete_role.side_effect = ClientError(
        {
            "Error": {
                "Code": "AccessDenied",
                "Message": (
                    f"User: {AGENT_ARN} is not authorized to perform iam:DeleteRole — token ghp_SECRET123"
                ),
            },
            "ResponseMetadata": {"RequestId": "req-abc-123"},
        },
        "DeleteRole",
    )

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    reason = next(i for i in res.items if i.item == "exec_role").reason
    assert "ghp_SECRET123" not in reason
    assert "req-abc-123" not in reason
    assert AGENT_ARN.split(":")[4] not in reason
    assert "arn:" not in reason
    assert "ghp_SECRET123" not in res.model_dump_json()


def test_a_SUCCESSFUL_reclaim_reports_deleted_and_names_no_role(delete_fakes):
    """The success path is unchanged and must stay quiet. A `reason` on a succeeded item would put
    a role name on a green row for no reason, and `NoSuchEntity` — the idempotent already-gone
    state every second delete and every pre-module repo takes — is success, not a leak."""
    f = delete_fakes
    f.iam.delete_role_policy.side_effect = _iam_client_error("NoSuchEntity", "DeleteRolePolicy")
    f.iam.delete_role.side_effect = _iam_client_error("NoSuchEntity", "DeleteRole")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome == "deleted"
    assert item.reason is None


def test_ONE_denied_stage_role_is_reported_WITHOUT_hiding_the_reclaimed_ones(delete_fakes):
    """Per-role tolerance survives the honesty change, and the report names WHICH role survived.

    An agent owns one role per stage. A denial on one must still attempt the rest (a partial
    reclaim beats leaking all of them) AND must name the one that leaked — "exec_role: failed"
    with no name would send the operator hunting through IAM for which of three it was."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    f.iam.delete_role.side_effect = [
        _iam_client_error("AccessDenied", "DeleteRole"),
        None,
        None,
    ]

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert _deleted_roles(f) == {_exec_role("dev"), _exec_role("prod"), LEGACY_EXEC_ROLE}
    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome == "failed"
    assert _exec_role("dev") in item.reason
    assert _exec_role("prod") not in item.reason
    assert LEGACY_EXEC_ROLE not in item.reason
    assert res.record_removed is True


def test_DESELECTING_the_runtime_reports_exec_role_as_SKIPPED_not_deleted(delete_fakes):
    """The reclaim rides the runtime selection, so an operator keeping the runtime keeps its role
    (a runtime without its exec role cannot pull images or write logs). The item must then read
    `skipped` — never silently absent, and never `deleted`, because a role that is still there is
    exactly the thing this item exists to be honest about."""
    f = delete_fakes

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection(runtime=False))

    assert f.iam.mock_calls == []
    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome == "skipped"


def test_an_agent_with_NO_NAME_reports_exec_role_skipped_rather_than_deleted(delete_fakes):
    """No name ⇒ no derivable role name ⇒ nothing was attempted, so `deleted` would be a claim
    about a call that never happened. This is the gap `_delete_runtime`'s docstring names: a
    second delete attempt after the registry entry is gone leaves the role to be removed by hand,
    and the cascade must say so rather than report a teardown it did not perform."""
    f = delete_fakes
    f.agent.name = None

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert f.iam.mock_calls == []
    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome == "skipped"
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"
    assert res.record_removed is True


def test_the_exec_role_item_is_reported_even_when_the_RUNTIME_delete_FAILS(delete_fakes):
    """A runtime failure must not swallow the reclaim's own report. The runtime item legitimately
    fails (a surviving runtime is live and billing, so the row IS kept for retry) — but the
    exec_role line still has to state what happened to the role, which was reclaimed before the
    raise precisely because it leaks account-globally."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.identity.delete_runtime_raises = RuntimeError("AccessDenied")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert next(i for i in res.items if i.item == "runtime").outcome == "failed"
    assert next(i for i in res.items if i.item == "exec_role").outcome == "deleted"
    assert _deleted_roles(f) == {_exec_role("dev"), LEGACY_EXEC_ROLE}


CROSS_ACCOUNT_ROLE = "arn:aws:iam::210987654321:role/agp-deployment-acme-dev"


CROSS_ACCOUNT_ROLE_NAME = "agp-deployment-acme-dev"


PROD_ROLE = "arn:aws:iam::310987654321:role/agp-deployment-acme-prod"


class _FakeStageClientSeam:
    """The `stage_client` seam, recorded.

    Hands back a DISTINCT double per (service, deploy_role_arn) pair, which is what makes
    "ran under the assumed client" able to fail: a seam returning one shared mock could not
    tell a per-stage assume from a single ambient client used N times.
    """

    def __init__(self):
        self.calls = []
        self.clients = {}
        self.raise_for = set()

    def __call__(self, service_name, cfg, *, session_suffix):
        arn = getattr(cfg, "deploy_role_arn", "") if cfg is not None else ""
        self.calls.append((service_name, arn, session_suffix))
        if arn in self.raise_for:
            raise TenantCredentialsError(f"{arn.rsplit('/', 1)[-1]} (ClientError)")
        return self.clients.setdefault((service_name, arn), MagicMock(name=f"{service_name}"))

    def client(self, service_name, deploy_role_arn):
        """The double a given (service, role) pair was served — None if never asked for."""
        return self.clients.get((service_name, deploy_role_arn))


def _arm_tenant(f, stages):
    """Give the fixture's service a tenant whose ``stages`` map is ``stages`` + the recorded
    seam. ``stages`` is ``{stage: deploy_role_arn}`` ("" ⇒ deploy-in-place)."""
    tenant = SimpleNamespace(
        id="default",
        stages={
            stage: TenantStageConfig(account_id="210987654321", region="eu-west-1", deploy_role_arn=arn)
            for stage, arn in stages.items()
        },
    )
    f.svc._tenants = SimpleNamespace(get=lambda tenant_id: tenant)
    seam = _FakeStageClientSeam()
    f.svc._stage_client = seam
    return seam


def _exec_role_reason(res):
    return next(i for i in res.items if i.item == "exec_role").reason


def test_a_cross_account_stage_reclaims_its_role_under_the_ASSUMED_client(delete_fakes):
    """The fix. The role named `{agent}-dev-agentcore-exec` exists in the TENANT's account,
    so the two IAM calls must be made by a client built from that account's temporary
    credentials — not by the ambient control-plane client, which would answer NoSuchEntity
    about a role it cannot see."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert ("iam", CROSS_ACCOUNT_ROLE) in [(s, a) for s, a, _ in seam.calls]
    assumed = seam.client("iam", CROSS_ACCOUNT_ROLE)
    assert assumed is not None
    assumed.delete_role.assert_called_once_with(RoleName=_exec_role("dev"))
    assert _exec_role("dev") not in _deleted_roles(f)
    assert next(i for i in res.items if i.item == "exec_role").outcome == "deleted"


def test_the_assume_session_name_identifies_the_agent_being_torn_down(delete_fakes):
    """`RoleSessionName` is what a tenant's CloudTrail shows them, so it has to say which
    agent's teardown opened the session — an opaque session id makes a cross-account delete
    unattributable in the only account that can audit it."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})

    f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    suffixes = {suffix for _, _, suffix in seam.calls}
    assert suffixes, "the seam was never called"
    for suffix in suffixes:
        assert AGENT_ID in suffix
        assert "teardown" in suffix


def test_an_ASSUME_FAILURE_reports_the_exec_role_item_as_assume_role_failed(delete_fakes):
    """THE PINNED CONTRACT. A failed assume means the role was never even addressed, so the
    one thing the report may not say is `deleted`. `assume_role_failed:` is the prefix
    because the operator's next action is different from every other failure here: this is
    not "retry", it is "the platform cannot reach that account — grant the trust or reclaim
    by hand"."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})
    seam.raise_for.add(CROSS_ACCOUNT_ROLE)

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome != "deleted"
    assert item.outcome == "failed"
    assert item.reason.startswith("assume_role_failed: ")
    assert CROSS_ACCOUNT_ROLE_NAME in item.reason


def test_an_assume_failure_reason_carries_NO_ACCOUNT_ID_and_no_ARN(delete_fakes):
    """THE TOKEN-LEAK FENCE, applied to a string this task newly surfaces. A
    `deploy_role_arn` CONTAINS the tenant's 12-digit account id, and `reason` renders in the
    console and lands in the logs — where a hard project rule bans an account id outright."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})
    seam.raise_for.add(CROSS_ACCOUNT_ROLE)

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    reason = _exec_role_reason(res)
    assert "210987654321" not in reason
    assert "arn:" not in reason
    assert "210987654321" not in res.model_dump_json()


def test_a_NoSuchEntity_under_the_ASSUMED_account_stays_idempotent_deleted(delete_fakes):
    """The other half of honesty, and the reason this is not simply "report failure when a
    deploy role exists". NoSuchEntity from the account that OWNS the role is the genuine
    already-done state that a second delete attempt, a pre-module repo and a half-failed
    provision all take. Once the question is asked in the right account the answer is
    trustworthy again, so it stays `deleted`."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})
    assumed = seam(
        "iam",
        SimpleNamespace(deploy_role_arn=CROSS_ACCOUNT_ROLE, region="eu-west-1"),
        session_suffix="pre-arm",
    )
    assumed.delete_role_policy.side_effect = _iam_client_error("NoSuchEntity", "DeleteRolePolicy")
    assumed.delete_role.side_effect = _iam_client_error("NoSuchEntity", "DeleteRole")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome == "deleted"
    assert item.reason is None
    assumed.delete_role.assert_any_call(RoleName=_exec_role("dev"))
    assert _exec_role("dev") not in _deleted_roles(f)


def test_an_AccessDenied_under_the_assumed_account_is_still_a_plain_not_reclaimed(delete_fakes):
    """An assume that WORKED followed by a denied delete is a different fact from an assume
    that failed: the platform reached the account and IAM refused the call. It keeps the
    pre-existing `not reclaimed:` wording, so `assume_role_failed:` stays diagnostic of the
    credential hop alone."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})
    assumed = seam(
        "iam",
        SimpleNamespace(deploy_role_arn=CROSS_ACCOUNT_ROLE, region="eu-west-1"),
        session_suffix="pre-arm",
    )
    assumed.delete_role.side_effect = _iam_client_error("AccessDenied", "DeleteRole")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    reason = _exec_role_reason(res)
    assert reason.startswith("not reclaimed: ")
    assert "assume_role_failed" not in reason


def test_an_assume_failure_for_ONE_stage_still_reclaims_the_OTHER(delete_fakes):
    """PER-ROLE TOLERANCE, preserved across the new failure mode. The reclaim loop's
    documented invariant is that one stage's failure never strands the next; raising out of
    the loop on the first bad assume would abandon a role the platform CAN delete, leaking it
    for a reason unrelated to it."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    f.agent.agent_arn = PROD_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE, "prod": PROD_ROLE})
    seam.raise_for.add(CROSS_ACCOUNT_ROLE)

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    prod_iam = seam.client("iam", PROD_ROLE)
    assert prod_iam is not None
    prod_iam.delete_role.assert_any_call(RoleName=_exec_role("prod"))
    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome == "failed"
    assert item.reason.startswith("assume_role_failed: ")
    assert CROSS_ACCOUNT_ROLE_NAME in item.reason


def test_the_LEGACY_unscoped_role_is_reclaimed_AMBIENT_never_under_a_guessed_stage(delete_fakes):
    """The pre-stage-scoping un-scoped name belongs to no stage, so there is no `deploy_role_arn` to
    resolve for
    it."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})

    f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert LEGACY_EXEC_ROLE in _deleted_roles(f)
    assumed = seam.client("iam", CROSS_ACCOUNT_ROLE)
    assert assumed is not None
    assumed_names = [c.kwargs.get("RoleName") for c in assumed.delete_role.call_args_list]
    assert LEGACY_EXEC_ROLE not in assumed_names


def test_an_UNKNOWN_STAGE_legacy_record_stays_on_the_AMBIENT_client(delete_fakes):
    """A scalar-only record resolves to the `unknown` placeholder stage, which names no
    tenant stage. It must not be looked up (and must not assume): `unknown` is the record
    admitting it does not know its account."""
    f = delete_fakes
    f.agent.agent_arns = {}
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert seam.calls == []
    assert LEGACY_EXEC_ROLE in _deleted_roles(f)
    assert next(i for i in res.items if i.item == "exec_role").outcome == "deleted"


def test_a_stage_with_NO_deploy_role_keeps_TODAYS_ambient_behaviour(delete_fakes):
    """Deploy-in-place must be untouched — no assume, no new client, the ambient one the
    whole pre-existing suite injects. A seam that assumed here would need a grant the ECS
    task role does not hold and would turn every single-account teardown into a failure."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    seam = _arm_tenant(f, {"dev": ""})

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert seam.calls == []
    assert _deleted_roles(f) == {_exec_role("dev"), LEGACY_EXEC_ROLE}
    assert next(i for i in res.items if i.item == "exec_role").outcome == "deleted"


def test_a_cross_account_runtime_is_deleted_under_the_ASSUMED_client(delete_fakes):
    """The runtime half. The control client must be the tenant account's, and it must be the
    SAME client the existence probe uses — see the next test for why that is the whole
    defect."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert ("bedrock-agentcore-control", CROSS_ACCOUNT_ROLE) in [(s, a) for s, a, _ in seam.calls]
    assumed = seam.client("bedrock-agentcore-control", CROSS_ACCOUNT_ROLE)
    assert f.identity.delete_runtime_clients == [assumed]
    assert f.identity.deleted_runtime_arns == [DEV_ARN]
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"


def test_an_ASSUME_FAILURE_reports_the_runtime_item_as_assume_role_failed(delete_fakes):
    """A live, billing runtime reported as `deleted` is the most expensive line in this
    defect. On a failed assume the step must fail with the credential reason — and NOT with
    a generic `TenantCredentialsError` type name, which tells an operator nothing about which
    account they cannot reach."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})
    seam.raise_for.add(CROSS_ACCOUNT_ROLE)

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    item = next(i for i in res.items if i.item == "runtime")
    assert item.outcome != "deleted"
    assert item.outcome == "failed"
    assert item.reason.startswith("assume_role_failed: ")
    assert CROSS_ACCOUNT_ROLE_NAME in item.reason
    assert f.identity.deleted_runtime_arns == []


def test_an_assume_failure_on_the_runtime_KEEPS_the_record_for_retry(delete_fakes):
    """UNLIKE `exec_role`, the runtime step is record-BLOCKING, and it must stay that way
    here: a surviving runtime is live, billing, and about to become untracked. Dropping the
    row would delete the last thing that knows the ARN."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})
    seam.raise_for.add(CROSS_ACCOUNT_ROLE)

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert res.record_removed is False
    assert f.svc._get_repo(R) is not None


def test_a_NotFound_under_the_CORRECT_assumed_account_stays_idempotent_deleted(delete_fakes):
    """The precise inversion of the defect. Under AMBIENT credentials a NotFound meant "not in
    the control-plane account", which said nothing about the tenant's runtime and was
    nonetheless reported as `deleted`. Asked in the account that OWNS the runtime, NotFound is
    the real already-gone state — so it stays the idempotent success it always was."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})
    f.identity.runtime_exists_return = False

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    item = next(i for i in res.items if i.item == "runtime")
    assert item.outcome == "deleted"
    assert item.reason is None
    assert f.identity.deleted_runtime_arns == []
    assumed = seam.client("bedrock-agentcore-control", CROSS_ACCOUNT_ROLE)
    assert assumed is not None, "the runtime step never assumed the tenant role"
    assert f.identity.exists_clients == [assumed]


def test_the_TF_STATE_delete_stays_on_the_CONTROL_PLANE_client(delete_fakes):
    """Not everything the runtime step reclaims is a tenant-account resource. The Terraform
    state objects live in the CONTROL-PLANE bucket, so assuming the tenant role for them
    would fail against a bucket the tenant never had."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})

    f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert f.s3.listed_prefix is not None, "the control-plane state delete did not run"


def test_a_MIXED_failure_still_names_the_credential_hop(delete_fakes):
    """When one stage's assume fails and another's delete fails on its own, the report has one
    line for two facts. It leads with the credential failure: an ordinary delete failure is a
    retry, an unreachable account is not, and the count still covers both."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    f.agent.agent_arn = PROD_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE, "prod": PROD_ROLE})
    seam.raise_for.add(CROSS_ACCOUNT_ROLE)
    f.identity.delete_runtime_raises = RuntimeError("boom")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    item = next(i for i in res.items if i.item == "runtime")
    assert item.outcome == "failed"
    assert item.reason.startswith("assume_role_failed: ")
    assert res.record_removed is False


def test_the_PREVIEW_never_reports_a_cross_account_runtime_as_GONE(delete_fakes):
    """The preview is what DRIVES the cascade: a `gone` runtime is presented as
    already-deleted and UNCHECKED, so an ambient probe answering NotFound in the wrong
    account would have the operator skip the runtime step entirely — leaking it with no
    report at all, which is the defect wearing the modal's clothes. A credential failure
    takes the existing ambiguous branch: `unknown`, which the frontend treats as
    selectable+checked. `gone` is never inferred from a question asked in the wrong place."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})
    seam.raise_for.add(CROSS_ACCOUNT_ROLE)

    preview = f.svc.preview_delete(project_id=P, repo_id=R)

    assert next(i for i in preview.items if i.item == "runtime").state == "unknown"


def test_the_PREVIEW_probes_under_the_ASSUMED_client(delete_fakes):
    """And when the assume works, the answer is about the right account."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})

    preview = f.svc.preview_delete(project_id=P, repo_id=R)

    assert next(i for i in preview.items if i.item == "runtime").state == "present"
    assumed = seam.client("bedrock-agentcore-control", CROSS_ACCOUNT_ROLE)
    assert assumed is not None, "the preview never assumed the tenant role"
    assert f.identity.exists_clients == [assumed]


def test_a_NON_credential_seam_failure_never_ESCAPES_the_exec_role_item(delete_fakes):
    """`_delete_exec_role` NEVER RAISES, and that invariant is load-bearing: its caller sits
    OUTSIDE `_run_step`, so an escape would 500 the whole delete route — losing the github,
    image, runtime and identity line-items along with it, and leaving the operator with no
    report of a teardown that mostly SUCCEEDED. A seam failure that is not a credential
    failure (a bad region, a client-construction error) is reported as an ordinary surviving
    role instead of allowed out."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})

    def boom(service_name, cfg, *, session_suffix):
        raise ValueError("invalid region")

    f.svc._stage_client = boom

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome == "failed"
    assert item.reason == f"not reclaimed: {_exec_role('dev')} (ValueError)"
    assert {i.item for i in res.items} >= {"github", "image", "runtime", "identity", "record"}


def test_a_NON_credential_seam_failure_makes_the_PREVIEW_unknown_not_a_500(delete_fakes):
    """The read-only probe has the same duty: an unreachable delete modal is strictly worse
    than an `unknown` row, which the frontend already treats as selectable+checked."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})

    def boom(service_name, cfg, *, session_suffix):
        raise ValueError("invalid region")

    f.svc._stage_client = boom

    preview = f.svc.preview_delete(project_id=P, repo_id=R)

    assert next(i for i in preview.items if i.item == "runtime").state == "unknown"


def test_TWO_cross_account_stages_each_delete_under_their_OWN_assumed_client(delete_fakes):
    """Cross-account confusion is the one mistake here that is irreversible.

    Asserted PAIRWISE (arn ↔ client), not as a set of clients: "both clients were used" is
    satisfied by a loop that swapped them."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    f.agent.agent_arn = PROD_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE, "prod": PROD_ROLE})

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    dev = seam.client("bedrock-agentcore-control", CROSS_ACCOUNT_ROLE)
    prod = seam.client("bedrock-agentcore-control", PROD_ROLE)
    assert dev is not None and prod is not None, "a stage never assumed its tenant role"
    assert dev is not prod, "the two accounts' clients are the same object"
    assert dict(zip(f.identity.deleted_runtime_arns, f.identity.delete_runtime_clients, strict=True)) == {
        DEV_ARN: dev,
        PROD_ARN: prod,
    }
    assert dict(zip(f.identity.probed_runtime_arns, f.identity.exists_clients, strict=True)) == {
        DEV_ARN: dev,
        PROD_ARN: prod,
    }
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"


def test_TWO_cross_account_stages_each_reclaim_their_role_under_their_OWN_client(delete_fakes):
    """The IAM half of the same property. An exec role is ACCOUNT-GLOBAL, so a DeleteRole
    issued in the wrong tenant's account answers NoSuchEntity — which this cascade reads as the
    idempotent already-done state and reports `deleted`, while the real role keeps blocking
    re-materialization in the account that holds it."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    f.agent.agent_arn = PROD_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE, "prod": PROD_ROLE})

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    dev_iam = seam.client("iam", CROSS_ACCOUNT_ROLE)
    prod_iam = seam.client("iam", PROD_ROLE)
    assert dev_iam is not None and prod_iam is not None
    assert dev_iam is not prod_iam

    def deleted_by(client):
        return [c.kwargs.get("RoleName") for c in client.delete_role.call_args_list]

    assert deleted_by(dev_iam) == [_exec_role("dev")]
    assert deleted_by(prod_iam) == [_exec_role("prod")]
    assert LEGACY_EXEC_ROLE in _deleted_roles(f)
    assert _exec_role("dev") not in _deleted_roles(f)
    assert _exec_role("prod") not in _deleted_roles(f)
    assert next(i for i in res.items if i.item == "exec_role").outcome == "deleted"


def _arm_lookup(f, get):
    """Like `_arm_tenant`, but the caller supplies the tenant LOOKUP itself."""
    f.svc._tenants = SimpleNamespace(get=get)
    seam = _FakeStageClientSeam()
    f.svc._stage_client = seam
    return seam


def _raising_lookup(tenant_id):
    raise RuntimeError("ProvisionedThroughputExceededException")


def _no_tenant_record(tenant_id):
    return None


def _tenant_without_the_stage(tenant_id):
    return SimpleNamespace(
        id="default",
        stages={
            "prod": TenantStageConfig(
                account_id="310987654321", region="eu-west-1", deploy_role_arn=PROD_ROLE
            )
        },
    )


@pytest.mark.parametrize(
    "lookup",
    [
        pytest.param(_raising_lookup, id="the tenant lookup RAISES (a store throttle)"),
        pytest.param(_no_tenant_record, id="the tenant record is MISSING"),
        pytest.param(_tenant_without_the_stage, id="the tenant no longer LISTS the stage"),
    ],
)
def test_an_UNRESOLVABLE_stage_is_reported_failed_and_NEVER_degrades_to_ambient(delete_fakes, lookup):
    """All three states, one property: the item says `failed`, and no call is made with the
    control-plane client whose NotFound would be read as success."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    seam = _arm_lookup(f, lookup)

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    runtime = next(i for i in res.items if i.item == "runtime")
    assert runtime.outcome != "deleted"
    assert runtime.outcome == "failed"
    assert runtime.reason.startswith("stage_unresolved: ")
    assert "dev" in runtime.reason
    assert f.identity.deleted_runtime_arns == []
    assert f.identity.exists_clients == []
    assert seam.calls == []

    exec_role = next(i for i in res.items if i.item == "exec_role")
    assert exec_role.outcome == "failed"
    assert "stage_unresolved" in exec_role.reason
    assert _exec_role("dev") in exec_role.reason
    assert _exec_role("dev") not in _deleted_roles(f)
    assert LEGACY_EXEC_ROLE in _deleted_roles(f)

    assert res.record_removed is False
    assert f.svc._get_repo(R) is not None


def test_an_UNRESOLVABLE_stage_makes_the_PREVIEW_unknown_never_GONE(delete_fakes):
    """Same rule on the read-only path, and for the same reason the credential failure takes
    it: `gone` presents the runtime as already-deleted and UNCHECKED, so an operator would
    skip the step entirely. "I could not work out which account to ask" is not "it is not
    there"."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    _arm_lookup(f, _raising_lookup)

    preview = f.svc.preview_delete(project_id=P, repo_id=R)

    assert next(i for i in preview.items if i.item == "runtime").state == "unknown"
    assert f.identity.exists_clients == []


def test_StageUnresolvedError_is_distinct_from_a_credential_failure():
    """Two different operator actions — "the platform cannot get INTO that account" (grant the
    trust) vs "the platform cannot work out WHICH account" (fix the tenant/stage record) — so
    the report prefixes and the exception types are deliberately separate."""
    err = StageUnresolvedError("tenant lookup failed for stage 'dev'")

    assert isinstance(err, Exception)
    assert not isinstance(err, TenantCredentialsError)
    assert err.message == "tenant lookup failed for stage 'dev'"


def test_a_NON_credential_client_failure_keeps_per_runtime_tolerance_and_the_TF_STATE(
    delete_fakes,
):
    """`stage_client`'s FINAL `boto3.client(...)` sits outside its own try, so a bad region or
    an unknown service raises something that is neither `TenantCredentialsError` nor
    `StageUnresolvedError`. Left unguarded it escaped `_delete_runtime` mid-loop, which lost
    per-runtime tolerance AND skipped the TF-state reclaim that sits after the loop — leaking
    the state objects. The two sibling paths (`_delete_exec_role`, `_probe_runtime`) already
    carried this guard; the runtime delete did not."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    f.agent.agent_arn = PROD_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE, "prod": PROD_ROLE})

    def boom(service_name, cfg, *, session_suffix):
        arn = getattr(cfg, "deploy_role_arn", "") if cfg is not None else ""
        if service_name == "bedrock-agentcore-control" and arn == CROSS_ACCOUNT_ROLE:
            raise ValueError("invalid region")
        return seam(service_name, cfg, session_suffix=session_suffix)

    f.svc._stage_client = boom

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    runtime = next(i for i in res.items if i.item == "runtime")
    assert runtime.outcome != "deleted"
    assert runtime.outcome == "failed"
    assert f.identity.deleted_runtime_arns == [PROD_ARN]
    assert f.identity.delete_runtime_clients == [seam.client("bedrock-agentcore-control", PROD_ROLE)]
    assert f.s3.listed_prefix is not None, "the TF-state objects leaked"
    assert res.record_removed is False


def test_a_MISSING_control_plane_IAM_client_no_longer_SUPPRESSES_a_cross_account_reclaim(
    delete_fakes,
):
    """`svc._iam` is None only when a caller injects it so. Pre-seam a missing client
    was "we cannot do this at all"; post-seam it is only the CONTROL-PLANE client, and whether
    it exists says nothing about assuming into a tenant account. The old gate therefore skipped
    a reclaim that works — and blamed it on `no agent record to derive the role name`, a cause
    that was not the actual one."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})
    f.svc._iam = None

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    dev_iam = seam.client("iam", CROSS_ACCOUNT_ROLE)
    assert dev_iam is not None
    dev_iam.delete_role.assert_any_call(RoleName=_exec_role("dev"))
    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome == "failed"
    assert item.reason == f"not reclaimed: {LEGACY_EXEC_ROLE} (no IAM client)"
    assert "no agent record" not in item.reason
    assert item.outcome != "skipped"


def test_NO_AGENT_NAME_is_still_the_only_reason_the_exec_role_item_is_SKIPPED(delete_fakes):
    """The other half of the same fix: `skipped` + `no agent record to derive the role name` is
    now EXACT rather than conflated, so it must still be produced by the state it names."""
    f = delete_fakes
    f.registry.get_return = None

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome == "skipped"
    assert item.reason == "no agent record to derive the role name"


class _Awaited:
    """A non-coroutine awaitable: ``__await__`` records that it ran and yields its value."""

    def __init__(self, value=None):
        self.value = value
        self.awaited = False

    def __await__(self):
        self.awaited = True
        return self.value
        yield


def test_identity_steps_drive_any_awaitable_not_only_a_coroutine():
    identity = MagicMock()
    provisioned, deleted = _Awaited("provisioned"), _Awaited("deleted")
    identity.provision_identity.return_value = provisioned
    identity.delete_identity.return_value = deleted
    svc = ProjectService(registry=MagicMock(), identity=identity, connection_service=MagicMock())
    agent = SimpleNamespace(id="agent-1")

    project_materialize._provision_identity(svc, agent)
    project_runtime_teardown._delete_identity(svc, agent)

    assert (provisioned.awaited, deleted.awaited) == (True, True)
    identity.provision_identity.assert_called_once_with(agent)
    identity.delete_identity.assert_called_once_with(agent)
