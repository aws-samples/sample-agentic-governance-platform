"""Model tests for the Project and Repository surface.

Pure Pydantic, no I/O, no AWS. Pins the 1:N project-to-repos design: ``Project`` is an empty
container (no ``template_id`` / ``repo_url`` / ``agent_id`` / ``status``; those live on
``Repository``), ``ProjectCreate`` accepts neither ``template_id`` nor ``trunk_branch``, and
``ProjectDetail`` nests a project with its repositories. The ``AGENT_NAME_RE`` arithmetic and the
artifact-kind split are pinned here too; the framework arm of ``validate_agent_config`` is pinned in
``tests/test_project_validation.py``, which needs a catalog and a route.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from models.deployment import Deployment
from models.project import Project, ProjectCreate, ProjectDetail
from models.repository import Repository, RepositoryCreate


def test_project_roundtrips():
    project = Project(
        id="p1",
        name="Payments",
        connection_id="c1",
        tenant_id="default",
        description="the payments org container",
        created_by="op@example.com",
        created_at="2026-07-08T00:00:00Z",
        updated_at="2026-07-08T00:00:00Z",
    )
    assert Project.model_validate_json(project.model_dump_json()) == project
    for gone in ("template_id", "repo_url", "agent_id", "status"):
        assert gone not in Project.model_fields
    assert Project.model_fields["description"].default == ""
    assert Project.model_fields["tenant_id"].is_required()
    assert ProjectCreate.model_fields["tenant_id"].is_required()


def test_project_create_drops_trunk_branch_and_project_defaults_it_to_main():
    """`trunk_branch` is internal and effectively constant: the create API has no such field, a stale
    client's value is dropped rather than refused, and every project reads `main`, including a stored
    project written before the field existed."""
    assert "trunk_branch" not in ProjectCreate.model_fields
    body = ProjectCreate.model_validate(
        {"name": "Payments", "connection_id": "c1", "tenant_id": "default", "trunk_branch": "develop"}
    )
    assert not hasattr(body, "trunk_branch")
    stored = {
        "id": "p1",
        "name": "Payments",
        "connection_id": "c1",
        "tenant_id": "default",
        "created_by": "op@example.com",
        "created_at": "2026-07-08T00:00:00Z",
        "updated_at": "2026-07-08T00:00:00Z",
    }
    assert Project.model_validate(stored).trunk_branch == "main"


def test_repository_roundtrips():
    repo = Repository(
        id="r1",
        project_id="p1",
        name="checkout-agent",
        repo_url="https://github.com/acme/checkout-agent",
        agent_id="a1",
        template_name="strands-agentcore",
        status="provisioning",
        created_by="op@example.com",
        created_at="2026-07-08T00:00:00Z",
        updated_at="2026-07-08T00:00:00Z",
    )
    assert Repository.model_validate_json(repo.model_dump_json()) == repo
    assert repo.cicd_status == "provisioning"
    bare = Repository(
        id="r2",
        project_id="p1",
        name="a",
        agent_id="a2",
        template_name="strands-agentcore",
        status="failed",
        created_by="op@example.com",
        created_at="2026-07-08T00:00:00Z",
        updated_at="2026-07-08T00:00:00Z",
    )
    assert bare.repo_url is None


def test_repository_create_carries_agent_config():
    body = RepositoryCreate.model_validate(
        {
            "name": "checkout-agent",
            "template_name": "strands-agentcore",
            "agent_config": {"framework": "strands", "agent_name": "checkout"},
        }
    )
    assert body.agent_config.framework == "strands"
    assert body.agent_config.agent_name == "checkout"


def test_project_create_no_longer_accepts_template_id():
    body = ProjectCreate.model_validate(
        {"name": "Payments", "connection_id": "c1", "tenant_id": "default", "template_id": "x"}
    )
    assert "template_id" not in ProjectCreate.model_fields
    assert not hasattr(body, "template_id")
    assert body.description == ""


def test_project_detail_nests_project_and_repositories():
    project = Project(
        id="p1",
        name="Payments",
        connection_id="c1",
        tenant_id="default",
        created_by="op@example.com",
        created_at="2026-07-08T00:00:00Z",
        updated_at="2026-07-08T00:00:00Z",
    )
    repo = Repository(
        id="r1",
        project_id="p1",
        name="checkout-agent",
        agent_id="a1",
        template_name="strands-agentcore",
        status="provisioning",
        created_by="op@example.com",
        created_at="2026-07-08T00:00:00Z",
        updated_at="2026-07-08T00:00:00Z",
    )
    detail = ProjectDetail(project=project, repositories=[repo])
    assert ProjectDetail.model_validate_json(detail.model_dump_json()) == detail
    assert detail.project.id == "p1"
    assert detail.repositories[0].project_id == "p1"
    assert detail.effective_role is None
    assert detail.ungoverned is False


def _minimal_repo_kwargs() -> dict:
    """The required-only Repository kwargs — mirrors the ``bare`` repo above."""
    return {
        "id": "r1",
        "project_id": "p1",
        "name": "checkout-agent",
        "agent_id": "a1",
        "template_name": "strands-agentcore",
        "status": "provisioning",
        "created_by": "op@example.com",
        "created_at": "2026-07-28T00:00:00Z",
        "updated_at": "2026-07-28T00:00:00Z",
    }


def test_prod_candidate_fields_default_to_none():
    repo = Repository(**_minimal_repo_kwargs())
    assert repo.prod_candidate_image_tag is None
    assert repo.prod_candidate_status is None


def test_prod_candidate_fields_round_trip():
    repo = Repository(
        **_minimal_repo_kwargs(),
        prod_candidate_image_tag="a-1-3f9a1c2",
        prod_candidate_sha="3f9a1c2" * 5 + "abcde",
        prod_candidate_actor="jorge",
        prod_candidate_at="2026-07-28T10:00:00Z",
        prod_candidate_status="pending",
    )
    assert Repository(**repo.model_dump()).prod_candidate_actor == "jorge"


def test_legacy_record_without_prod_candidate_fields_validates():
    assert Repository(**_minimal_repo_kwargs()).prod_candidate_at is None


def test_agent_name_re_caps_at_32_so_BOTH_derived_names_fit():
    """The cap is asserted THROUGH the arithmetic it exists to satisfy, not as a bare `32`.

    `agent_name` is the string the buildspec feeds the runtime module, which derives two stage-scoped
    account-global resource names from it: `{agent_name}_{stage}` under the 48-character runtime cap
    and `{agent_name}-{stage}-agentcore-exec` under IAM's 64. At 48 both overflowed, and truncating
    was rejected because two long names sharing a prefix would collide silently on an account-global
    name. A restated literal would still pass if someone widened the regex and "updated the test";
    this version fails unless the longest name the regex ACCEPTS fits both ceilings for a 15-char
    stage, the budgeted headroom (`dev` / `prod` are 3 / 4 today)."""
    from models.project import AGENT_NAME_RE

    longest = "a" + "b" * 31
    assert len(longest) == 32
    assert AGENT_NAME_RE.match(longest), "a 32-char name must be accepted"
    assert not AGENT_NAME_RE.match(longest + "c"), "33 chars must be refused"

    stage = "s" * 15
    assert len(f"{longest}_{stage}") <= 48, "agent_runtime_name would overflow its 48-char cap"
    assert len(f"{longest}-{stage}-agentcore-exec") <= 64, "the IAM role name would overflow 64"


def test_the_32_cap_fits_the_role_name_the_BACKEND_ACTUALLY_DERIVES():
    """The sibling above pins the cap against a RESTATED arithmetic; this pins it against the real
    producer, which is what the account will actually be asked to create.

    Both are needed and neither subsumes the other. The restated version documents the budget
    (`{name}-{stage}-agentcore-exec`, 15-char stage) and would keep passing if the backend's
    derivation grew a segment; this one imports `agentcore_exec_role_name` — the single Python
    producer, pinned byte-for-byte to the terraform module by
    `test_project_service.py::test_exec_role_name_matches_the_terraform_module_that_creates_it` —
    so the ceiling is measured on the string that reaches IAM. A drift between the two shows up
    as one of these two tests failing rather than as a `CreateRole` failure mid-deploy.

    The LEGACY un-scoped name is checked too: the delete cascade still attempts it (orphans from
    before stage scoping exist live), so it must also be a name IAM would accept as an argument.

    THE BINDING CEILING IS 15, AND IT IS THE RUNTIME NAME THAT SETS IT, not the role name this test
    measures: `32 + 1 + S + 1 + len("agentcore-exec") <= 64` gives `S <= 16` for the role name in
    isolation, but at a 32-char agent name a 16-char stage makes `agent_runtime_name` 49 against
    AWS's 48, and the module's `precondition` refuses it at `plan`. Both ceilings are pinned here,
    so the role name's extra character can never be read as usable headroom."""
    from models.project import AGENT_NAME_RE
    from services.project_runtime_teardown import (
        agentcore_exec_role_name,
        legacy_agentcore_exec_role_name,
    )

    longest = "a" + "b" * 31
    assert AGENT_NAME_RE.match(longest), "guard's premise: 32 chars is the widest accepted name"

    budgeted = agentcore_exec_role_name(longest, "s" * 15)
    assert len(budgeted) <= 64, f"{budgeted!r} is {len(budgeted)} chars; IAM allows 64"

    assert len(f"{longest}_{'s' * 15}") <= 48, "15 is the widest stage: the RUNTIME name binds first, at 48"
    assert len(f"{longest}_{'s' * 16}") > 48, (
        "16 overflows agent_runtime_name (48) even though the role name still fits — the runtime "
        "precondition refuses it at plan"
    )

    assert len(agentcore_exec_role_name(longest, "s" * 16)) <= 64, (
        "16 is the widest stage the ROLE NAME alone tolerates; the binding ceiling is still 15"
    )
    assert len(agentcore_exec_role_name(longest, "s" * 17)) > 64, "17 must overflow"

    assert len(legacy_agentcore_exec_role_name(longest)) <= 64


def test_agent_name_re_still_refuses_the_shapes_AWS_refuses():
    """Non-vacuity: the tightening must not be the ONLY thing pinned, or a regex loosened to
    `.{0,32}` would pass the length test above. `agent_runtime_name` is
    `[a-zA-Z][a-zA-Z0-9_]{0,47}` — a LETTER first, and underscores only. A hyphen is the trap
    worth naming: it is legal in the IAM role name and illegal in the runtime name, so a hyphenated
    agent_name fails at `CreateAgentRuntime` after the role was already created."""
    from models.project import AGENT_NAME_RE

    for ok in ("a", "A1", "my_agent", "platform_agent"):
        assert AGENT_NAME_RE.match(ok), ok
    for bad in ("", "1agent", "_agent", "my-agent", "my agent", "my.agent", "my/agent"):
        assert not AGENT_NAME_RE.match(bad), bad


def test_validate_agent_config_rejects_an_over_long_agent_name():
    """The regex is only load-bearing if the validator actually applies it — this is the API-facing
    path (`project_service.add_repo` step 0), so a 33-char name must be refused at repo-create
    rather than surfacing as a terraform `precondition` failure mid-build (or, worse, an
    `EntityAlreadyExists` at IAM).

    The second argument is the ``framework`` recorded on the REGISTERED TEMPLATE, which the requested
    one must equal; it is passed as ``"strands"`` here to MATCH, so the framework arm stays satisfied
    and this test keeps asserting only what it is about, the name."""
    import pytest

    from models.project import validate_agent_config

    good = {"framework": "strands", "agent_name": "a" * 32}
    validate_agent_config(good, "strands")  # must not raise

    with pytest.raises(ValueError, match="invalid agent_config agent_name"):
        validate_agent_config({"framework": "strands", "agent_name": "a" * 33}, "strands")


def test_the_frontend_mirrors_the_backend_regex_EXACTLY():
    """The two regexes are edited independently and neither imports the other. A frontend still
    accepting 48 lets an operator type a name the modal calls valid and the backend then rejects
    with a 502 — and if the backend were ever the looser one, the name would reach terraform and
    fail at `plan`. Read out of the .ts source, so this cannot pass by restating the pattern.

    `AGENT_NAME_RE` is an exported const of `studioModel.ts`, so the frontend's own vitest suite
    could import it and compare. The backend owns the pattern, though, so the agreement is
    asserted here, where the Python regex lives."""
    import re as _re
    from pathlib import Path

    from models.project import AGENT_NAME_RE

    modal = Path(__file__).resolve().parents[2] / "frontend/src/components/operations/studioModel.ts"
    assert modal.is_file(), modal
    (mirrored,) = _re.findall(r"const AGENT_NAME_RE = /\^(.+)\$/;", modal.read_text())
    assert f"^{mirrored}$" == AGENT_NAME_RE.pattern


def _mcp_repo_kwargs() -> dict:
    """A repository builds EITHER an agent OR an MCP server: ``artifact_kind`` says which, and exactly
    one of ``agent_id`` / ``mcp_server_id`` is set to match, never both, never the wrong one."""
    kw = _minimal_repo_kwargs()
    del kw["agent_id"]
    kw.update(artifact_kind="mcp_server", mcp_server_id="mcp-1")
    return kw


def test_a_repo_dict_from_before_mcp_repos_validates_as_an_agent_repo():
    """Such an item carries agent_id only: no kind, no mcp id, no gateway fields."""
    repo = Repository(**_minimal_repo_kwargs())
    assert repo.artifact_kind == "agent"
    assert repo.agent_id == "a1"
    assert repo.mcp_server_id is None
    assert (repo.gateway_arn, repo.gateway_id, repo.gateway_url) == (None, None, None)


def test_mcp_server_repo_validates_without_an_agent_id():
    repo = Repository(
        **_mcp_repo_kwargs(), gateway_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/g-1"
    )
    assert repo.artifact_kind == "mcp_server"
    assert repo.agent_id is None
    assert repo.mcp_server_id == "mcp-1"
    assert Repository.model_validate_json(repo.model_dump_json()) == repo


def test_agent_repo_without_agent_id_is_refused():
    kw = _minimal_repo_kwargs()
    del kw["agent_id"]
    with pytest.raises(ValidationError, match="agent_id"):
        Repository(**kw)


def test_agent_repo_carrying_an_mcp_server_id_is_refused():
    with pytest.raises(ValidationError, match="mcp_server_id"):
        Repository(**_minimal_repo_kwargs(), mcp_server_id="mcp-1")


def test_mcp_server_repo_without_mcp_server_id_is_refused():
    kw = _mcp_repo_kwargs()
    del kw["mcp_server_id"]
    with pytest.raises(ValidationError, match="mcp_server_id"):
        Repository(**kw)


def test_mcp_server_repo_carrying_an_agent_id_is_refused():
    with pytest.raises(ValidationError, match="agent_id"):
        Repository(**_mcp_repo_kwargs(), agent_id="a1")


def test_unknown_artifact_kind_is_refused():
    with pytest.raises(ValidationError):
        Repository.model_validate({**_minimal_repo_kwargs(), "artifact_kind": "runtime"})


def test_repository_create_agent_config_defaults_empty_and_takes_mcp_name():
    body = RepositoryCreate(name="claims-api", template_name="openapi-gateway", mcp_name="claims-api")
    assert body.agent_config.model_dump(exclude_unset=True) == {}
    assert body.mcp_name == "claims-api"
    assert (
        RepositoryCreate.model_validate(
            {"name": "a", "template_name": "t", "agent_config": {"x": 1}}
        ).mcp_name
        is None
    )


def _deployment_kwargs() -> dict:
    return {
        "id": "dep-0000abcd",
        "repo_id": "r1",
        "stage": "dev",
        "seq_key": "r1#dev#2026-10-05T00:00:00Z#abcd",
        "image_tag": "t-1",
        "started_at": "2026-10-05T00:00:00Z",
    }


def test_a_deployment_row_with_only_an_agent_id_validates_unchanged():
    dep = Deployment(**_deployment_kwargs(), agent_id="a1")
    assert dep.agent_id == "a1"
    assert dep.mcp_server_id is None


def test_deployment_validates_without_an_agent_id():
    dep = Deployment(**_deployment_kwargs(), mcp_server_id="mcp-1")
    assert dep.agent_id is None
    assert dep.mcp_server_id == "mcp-1"


def test_a_stored_status_outside_the_set_folds_to_unknown_and_warns(caplog):
    """The buildspec writes ``cicd_status`` straight into the table, so a read must never raise on a
    value the union does not name; the fold is logged with the raw value and the record's key."""
    import logging

    with caplog.at_level(logging.WARNING, logger="models.repository"):
        repo = Repository.model_validate({**_minimal_repo_kwargs(), "cicd_status": "building"})
    assert repo.cicd_status == "unknown"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "'building'" in warnings[0].getMessage()
    assert "r1" in warnings[0].getMessage()


def test_a_member_status_hydrates_unchanged_without_a_warning(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="models.repository"):
        repo = Repository(**_minimal_repo_kwargs(), cicd_status="deployed")
    assert repo.cicd_status == "deployed"
    assert caplog.records == []


def test_repository_create_agent_config_is_a_model_that_keeps_extra_keys():
    """The service reads the config as the dict the client sent: the declared keys, any extra key,
    and nothing the client left out."""
    from models.repository import AgentConfig

    body = RepositoryCreate.model_validate(
        {
            "name": "checkout-agent",
            "template_name": "strands-agentcore",
            "agent_config": {
                "framework": "strands",
                "agent_name": "checkout",
                "model_id": "us.anthropic.claude-sonnet-4-6",
                "temperature": 0.2,
            },
        }
    )
    assert isinstance(body.agent_config, AgentConfig)
    assert list(AgentConfig.model_fields) == ["agent_name", "framework", "model_id"]
    assert body.agent_config.model_dump(exclude_unset=True) == {
        "framework": "strands",
        "agent_name": "checkout",
        "model_id": "us.anthropic.claude-sonnet-4-6",
        "temperature": 0.2,
    }
    assert RepositoryCreate(name="a", template_name="t").agent_config.model_dump(exclude_unset=True) == {}


def test_agent_repository_view_copies_six_repository_fields():
    from models.repository import AgentRepositoryView

    assert list(AgentRepositoryView.model_fields) == [
        "project_id",
        "repo_id",
        "name",
        "prod_candidate_status",
        "eval_gate",
        "eval_regressions",
    ]
    copied = {"repo_id": "id"}
    for name, field in AgentRepositoryView.model_fields.items():
        assert field.annotation == Repository.model_fields[copied.get(name, name)].annotation, name
