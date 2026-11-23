"""The ``set_repo_vars`` step branches on the chain's build kind.

The step resolves the chain (``template.infra_template_id`` to an ``InfraTemplateRecord``) and branches
on ``build_kind``. ``"container"`` writes the var dict every existing agent deploys through, pinned as an
exact dict equality rather than a set of ``in`` assertions because a var that quietly changed shape
breaks every build in the org. ``"config"`` (the harness: a template with no image, no registry and
nothing to push) drops the three stage-scoped ECR keys, the push role among them a real credential
handed to CI for no reason, and writes ``AGP_REGION`` and ``AGP_DEPLOY_ROLE_ARN`` instead, with the
step's label saying so. Both branches source from the tenant's ``dev`` stage, the one stage whose
credentials reach CI, and differ in which of its fields they forward.

The config branch is a subtraction, not a two-key dict: the keys are layered (platform defaults, then
project, then repo overrides, then identity) and the platform defaults themselves carry the three ECR
keys, so writing only two would also drop ``AGENT_ID``, ``CONNECTION_ID`` and ``AGP_API_URL``, which the
harness scaffold's ``deploy.yml`` trigger job reads. The ECR three are subtracted wherever they came from.

The write client drops empty values, so a blank setting once left the step ``done`` with a partial var
set on the repository and the build failing with no signal in AGP; the step refuses before the write,
and the persisted hint names each missing key and where its value comes from, so the operator fixes
the source rather than retrying into the same hole.

The service is driven directly (``run_materialize``) with mocked collaborators and a real in-memory
``ProjectService``; no FastAPI, no AWS.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.infra_template_registry import (
    InfraTemplateRecord,
    InfraTemplateRegistry,
    InfraTemplateRegistryError,
)
from services.project_service import ProjectService
from services.template_registry import TemplateRecord, TemplateRegistry, template_id_for

FIXED = datetime(2026, 8, 6, tzinfo=timezone.utc)

CONTAINER_TEMPLATE = "strands-agentcore"
CONFIG_TEMPLATE = "harness-agentcore"
RUNTIME_INFRA_ID = "agentcore-runtime"
HARNESS_INFRA_ID = "agentcore-harness"

DEV_STAGE = SimpleNamespace(
    account_id="acct-dev",
    region="eu-central-1",
    ecr_repo_uri="dev.ecr.example/agents",
    push_role_arn="arn:aws:iam::acct-dev:role/agp-push-dev",
    deploy_role_arn="arn:aws:iam::acct-dev:role/agp-deploy-dev",
)

PLATFORM_REPO_VARS = {
    "AWS_REGION": "us-east-1",
    "AWS_ECR_PUSH_ROLE_ARN": "arn:aws:iam::acct-platform:role/platform-push",
    "ECR_REPOSITORY": "platform.ecr.example/fallback",
    "PLATFORM_BUILD_DONE_URL": "https://agp.example/builds/done",
    "AGP_API_URL": "https://agp.example/api/v1",
}


class _FakeTenantService:
    def get(self, tenant_id):
        return SimpleNamespace(id=tenant_id, stages={"dev": DEV_STAGE})


def _template_registry(*, framework="strands"):
    """A real in-memory ``TemplateRegistry`` holding both templates, the container one on the default
    chain and the harness one on the config chain; neither has a source pair, so materialize reads the
    on-disk seed the scaffold fixture provides (this file is about vars, not bytes)."""
    reg = TemplateRegistry()
    for name, infra_id in (
        (CONTAINER_TEMPLATE, RUNTIME_INFRA_ID),
        (CONFIG_TEMPLATE, HARNESS_INFRA_ID),
    ):
        reg.create(
            TemplateRecord(
                id=template_id_for(name),
                name=name,
                connection_id="c1",
                created_at="2026-08-01T00:00:00+00:00",
                created_by="op@x",
                framework=framework,
                infra_template_id=infra_id,
            )
        )
    return reg


def _infra_registry(*, ids=(RUNTIME_INFRA_ID, HARNESS_INFRA_ID)):
    reg = InfraTemplateRegistry()
    shapes = {
        RUNTIME_INFRA_ID: ("container", "agentcore_runtime", "agp-runtime-infra"),
        HARNESS_INFRA_ID: ("config", "agentcore_harness", "agp-harness-infra"),
    }
    for infra_id in ids:
        build_kind, iac_type, repo_name = shapes[infra_id]
        reg.create(
            InfraTemplateRecord(
                id=infra_id,
                name=infra_id,
                platform="aws",
                iac_type=iac_type,
                build_kind=build_kind,
                repo_name=repo_name,
                module_source=f"{infra_id}/module.zip",
                connection_id="c1",
                created_at="2026-08-01T00:00:00+00:00",
            )
        )
    return reg


@pytest.fixture
def container_and_config_scaffold_dir(tmp_path_factory):
    root = tmp_path_factory.mktemp("agent-templates")
    for name in (CONTAINER_TEMPLATE, CONFIG_TEMPLATE):
        (root / name).mkdir()
        (root / name / "README.md").write_bytes(b"# agent\n")
    return root


@pytest.fixture
def mocks():
    agent = SimpleNamespace(
        id="agent-1",
        agent_arn=None,
        name="p1_agent",
        framework="strands",
        model_id="us.anthropic.claude-sonnet-4-6",
    )
    registry = MagicMock()
    registry.create.return_value = agent
    registry.get.return_value = agent

    identity = MagicMock()
    identity.provision_identity.return_value = None

    conn = MagicMock()
    conn.get_connection.return_value = SimpleNamespace(org="acme", base_url=None)
    conn.get_bearer_token.return_value = "ghp_secret"

    rollout = MagicMock()
    rollout.create_repo.return_value = "https://github.com/acme/my-agent"
    return SimpleNamespace(registry=registry, identity=identity, conn=conn, rollout=rollout)


def _service(mocks, scaffold_dir, *, templates=None, infra=None):
    ids = iter([f"id-{i}" for i in range(200)])
    return ProjectService(
        table_name="",
        agent_templates_dir=str(scaffold_dir),
        registry=mocks.registry,
        identity=mocks.identity,
        connection_service=mocks.conn,
        github_repo_service=mocks.rollout,
        tenant_service=_FakeTenantService(),
        template_registry=templates if templates is not None else _template_registry(),
        infra_template_registry=infra if infra is not None else _infra_registry(),
        repo_vars=dict(PLATFORM_REPO_VARS),
        new_id=lambda: next(ids),
        now=lambda: FIXED,
    )


def _materialize(svc, template_name):
    """Drive a full materialize and return the persisted repo record."""
    project = svc.create_project(
        name="p1",
        connection_id="c1",
        tenant_id="ten-1",
        description="d",
        created_by="op@x",
    )
    repo = svc.add_repo(
        project_id=project.id,
        name="my-agent",
        template_name=template_name,
        agent_config={
            "agent_name": "p1_agent",
            "framework": "strands",
            "model_id": "us.anthropic.claude-sonnet-4-6",
        },
        created_by="op@x",
        principal=SimpleNamespace(oid="O1", email="e@x"),
    )
    svc.run_materialize(repo.id)
    persisted = svc.get_repo(repo.id)
    assert persisted is not None
    return persisted


def _written_vars(mocks):
    """The var dict ``set_repo_vars`` handed the write client, the third positional arg; the scope is
    repository-wide on both branches."""
    assert mocks.rollout.set_ci_vars.call_count == 1
    args, kwargs = mocks.rollout.set_ci_vars.call_args
    assert kwargs["scope"] is None
    return args[2]


def _step(repo, key):
    return next(s for s in repo.steps if s.key == key)


def test_a_container_chain_writes_exactly_todays_var_dict(mocks, container_and_config_scaffold_dir):
    """THE EXACT DICT, not a subset. Every agent in every org deploys through this branch, so the
    test is an equality: an added key, a dropped key or a re-sourced value all fail here rather
    than in a customer's build.

    Layering: platform defaults < project < repo overrides, with the tenant's DEV stage overriding the
    three stage-scoped keys and ``AGENT_ID``/``CONNECTION_ID`` stamped last
    and never overridable."""
    svc = _service(mocks, container_and_config_scaffold_dir)

    _materialize(svc, CONTAINER_TEMPLATE)

    assert _written_vars(mocks) == {
        "PLATFORM_BUILD_DONE_URL": "https://agp.example/builds/done",
        "AGP_API_URL": "https://agp.example/api/v1",
        "ECR_REPOSITORY": "dev.ecr.example/agents",
        "AWS_REGION": "eu-central-1",
        "AWS_ECR_PUSH_ROLE_ARN": "arn:aws:iam::acct-dev:role/agp-push-dev",
        "AGENT_ID": "agent-1",
        "CONNECTION_ID": "c1",
    }


def test_a_container_chain_keeps_the_steps_own_label(mocks, container_and_config_scaffold_dir):
    """No skip happened, so nothing may claim one. The label is what the console renders verbatim
    (no key-to-label map), so a container build must keep reading exactly what
    ``MATERIALIZE_STEPS`` declares."""
    svc = _service(mocks, container_and_config_scaffold_dir)

    repo = _materialize(svc, CONTAINER_TEMPLATE)

    assert _step(repo, "set_repo_vars").label == "Set repository CI variables"


@pytest.mark.parametrize(
    "templates,infra",
    [(None, "unwired"), ("real", "empty")],
    ids=["no-infra-registry", "dangling-chain"],
)
def test_an_unresolvable_chain_falls_back_to_the_container_vars(
    mocks, container_and_config_scaffold_dir, templates, infra
):
    """Fail closed on the regression, not on the new path.

    ``no-infra-registry`` is the construction with no catalog wired at all; ``dangling-chain`` is a
    catalog with no infra row for the chain the template names, a connection never rolled out.
    ``TemplateRecord.infra_template_id`` hydrates to ``"agentcore-runtime"`` for every record that
    predates the field, so "no infra record found" is overwhelmingly a deployment that has not seeded
    the infra partition yet, and the honest answer for it is the container behaviour. Treating an
    unresolvable chain as "not a container" would strip ECR_REPOSITORY, AWS_REGION and the push role
    from a build that needs all three, and the step would still report ``done``."""
    svc = _service(
        mocks,
        container_and_config_scaffold_dir,
        templates=_template_registry() if templates == "real" else None,
        infra=None if infra == "unwired" else _infra_registry(ids=()),
    )

    repo = _materialize(svc, CONTAINER_TEMPLATE)

    written = _written_vars(mocks)
    assert written["ECR_REPOSITORY"] == "dev.ecr.example/agents"
    assert written["AWS_ECR_PUSH_ROLE_ARN"] == "arn:aws:iam::acct-dev:role/agp-push-dev"
    assert "AGP_DEPLOY_ROLE_ARN" not in written
    assert _step(repo, "set_repo_vars").label == "Set repository CI variables"


def test_a_config_chain_drops_the_ecr_vars_and_writes_the_two_config_ones(
    mocks, container_and_config_scaffold_dir
):
    svc = _service(mocks, container_and_config_scaffold_dir)

    _materialize(svc, CONFIG_TEMPLATE)

    assert _written_vars(mocks) == {
        "PLATFORM_BUILD_DONE_URL": "https://agp.example/builds/done",
        "AGP_API_URL": "https://agp.example/api/v1",
        "AGP_REGION": "eu-central-1",
        "AGP_DEPLOY_ROLE_ARN": "arn:aws:iam::acct-dev:role/agp-deploy-dev",
        "AGENT_ID": "agent-1",
        "CONNECTION_ID": "c1",
    }


def test_a_config_chain_writes_no_ecr_key_from_any_layer(mocks, container_and_config_scaffold_dir):
    """THE SUBTRACTION IS WHAT MAKES THIS TRUE, and this is the test that proves it: all three
    keys are present in the PLATFORM DEFAULTS, so an implementation that merely skipped
    ``_build_stage_ci_vars`` would still hand a harness repo a push role — the credential with the
    real consequence."""
    svc = _service(mocks, container_and_config_scaffold_dir)

    _materialize(svc, CONFIG_TEMPLATE)

    written = _written_vars(mocks)
    for key in ("ECR_REPOSITORY", "AWS_REGION", "AWS_ECR_PUSH_ROLE_ARN"):
        assert key not in written


def test_a_config_chain_keeps_the_vars_its_deploy_workflow_reads(mocks, container_and_config_scaffold_dir):
    """The harness scaffold's ``deploy.yml`` trigger job reads ``vars.AGENT_ID``,
    ``vars.CONNECTION_ID`` and ``vars.AGP_API_URL``. "Only the two config vars" is about the
    stage-sourced ECR layer — a literal two-key dict would leave the workflow unable to name its
    own agent or reach the platform."""
    svc = _service(mocks, container_and_config_scaffold_dir)

    _materialize(svc, CONFIG_TEMPLATE)

    written = _written_vars(mocks)
    assert written["AGENT_ID"] == "agent-1"
    assert written["CONNECTION_ID"] == "c1"
    assert written["AGP_API_URL"] == "https://agp.example/api/v1"


def test_a_config_chain_records_the_skip_on_the_step(mocks, container_and_config_scaffold_dir):
    """THE SKIP IS STATED, NOT INFERRED. A config build's CI vars look INCOMPLETE next to a
    container one's, and an operator comparing the two has no other way to tell "the platform
    skipped these on purpose" from "the step half-failed". The string is pinned by the plan."""
    svc = _service(mocks, container_and_config_scaffold_dir)

    repo = _materialize(svc, CONFIG_TEMPLATE)

    step = _step(repo, "set_repo_vars")
    assert step.label == "config build: ECR vars skipped"
    assert step.status.value == "done"


def test_the_config_branch_survives_a_repo_override_of_an_ecr_key(mocks, container_and_config_scaffold_dir):
    """A repo-level override is a LAYER, and the subtraction runs after it. Otherwise an operator
    could re-introduce a push role into a config build through ``repo_overrides`` — the same
    "checkbox as authority" shape refused for destructive rollouts."""
    svc = _service(mocks, container_and_config_scaffold_dir)
    project = svc.create_project(
        name="p1", connection_id="c1", tenant_id="ten-1", description="d", created_by="op@x"
    )
    repo = svc.add_repo(
        project_id=project.id,
        name="my-agent",
        template_name=CONFIG_TEMPLATE,
        agent_config={
            "agent_name": "p1_agent",
            "framework": "strands",
            "model_id": "us.anthropic.claude-sonnet-4-6",
        },
        created_by="op@x",
        principal=SimpleNamespace(oid="O1", email="e@x"),
        repo_overrides={"AWS_ECR_PUSH_ROLE_ARN": "arn:aws:iam::attacker:role/push"},
    )
    svc.run_materialize(repo.id)

    assert "AWS_ECR_PUSH_ROLE_ARN" not in _written_vars(mocks)


def test_an_empty_deploy_role_is_single_account_mode_not_a_failure(mocks, container_and_config_scaffold_dir):
    """Empty is a value here, and materialize must not refuse it.

    ``modules/agentcore_harness/variables.tf`` documents ``deploy_role_arn`` as "empty =
    deploy-in-place (single-account)" and ``versions.tf`` gates its ``assume_role`` on ``!= ""``, so a
    blank role is the single-account shape, not a missing value; refusing it would make a harness agent
    unmaterializable on every single-account deployment. Nothing consumes the var either way: the
    harness ``deploy.yml`` reads only ``AGENT_ID``, ``CONNECTION_ID`` and ``AGP_API_URL``, and the
    harness buildspec resolves the role and region server-side from the tenant stage.

    ``AGP_DEPLOY_ROLE_ARN`` is written as empty rather than omitted; the write client drops empties, so
    a single-account repo ends up with no such var, which is the module's contract. The ECR subtraction
    is unaffected by the single-account shape."""
    svc = _service(mocks, container_and_config_scaffold_dir)
    svc._tenants = SimpleNamespace(
        get=lambda tenant_id: SimpleNamespace(
            id=tenant_id,
            stages={"dev": SimpleNamespace(region="eu-central-1", deploy_role_arn="")},
        )
    )

    repo = _materialize(svc, CONFIG_TEMPLATE)

    step = _step(repo, "set_repo_vars")
    assert step.status.value == "done"
    assert step.label == "config build: ECR vars skipped"
    written = _written_vars(mocks)
    assert written["AGP_REGION"] == "eu-central-1"
    assert written["AGP_DEPLOY_ROLE_ARN"] == ""
    assert "AWS_ECR_PUSH_ROLE_ARN" not in written


def test_a_config_chain_still_refuses_a_tenant_with_no_dev_stage(mocks, container_and_config_scaffold_dir):
    """The shared refusal survives the empty-deploy-role relaxation. Dropping that hard-fail must not
    also drop the missing-``dev`` one: that stage names the ACCOUNT a config build deploys into,
    and substituting another would land a harness agent somewhere nobody chose. This is the same
    rule ``_build_stage_ci_vars`` applies to a container build."""
    svc = _service(mocks, container_and_config_scaffold_dir)
    svc._tenants = SimpleNamespace(
        get=lambda tenant_id: SimpleNamespace(id=tenant_id, stages={"staging": DEV_STAGE})
    )

    repo = _materialize(svc, CONFIG_TEMPLATE)

    assert _step(repo, "set_repo_vars").status.value == "failed"
    assert repo.status == "failed"
    mocks.rollout.set_ci_vars.assert_not_called()


def test_an_unreadable_infra_catalog_fails_the_step_rather_than_guessing(
    mocks, container_and_config_scaffold_dir
):
    """The registries are STRICT by design and this does not soften them. Folding "AGP could not
    read its infra catalog" into "container" would write ECR vars for a harness repo on every
    store hiccup — an intermittent wrong answer, strictly worse than a loud failure a retry
    fixes (the rule ``_resolve_template_bytes`` already follows)."""

    class _Exploding:
        def get(self, connection_id, infra_template_id):
            raise InfraTemplateRegistryError("store down")

    svc = _service(mocks, container_and_config_scaffold_dir, infra=_Exploding())

    repo = _materialize(svc, CONFIG_TEMPLATE)

    assert _step(repo, "set_repo_vars").status.value == "failed"
    mocks.rollout.set_ci_vars.assert_not_called()


def _tenants_with_dev(**fields):
    stage = SimpleNamespace(**{**vars(DEV_STAGE), **fields})
    return SimpleNamespace(get=lambda tenant_id: SimpleNamespace(id=tenant_id, stages={"dev": stage}))


@pytest.mark.parametrize("template_name", [CONTAINER_TEMPLATE, CONFIG_TEMPLATE])
def test_an_empty_agp_api_url_fails_the_step_naming_the_setting_and_the_tfvar(
    mocks, container_and_config_scaffold_dir, template_name
):
    """THE LIVE DEFECT. ``build.yml``'s trigger job POSTs to ``${AGP_API_URL}/builds/runtime``; with
    the var absent it can never reach the platform, and nothing in AGP said why. The hint must
    name the KEY (what the workflow misses), the SETTING (what the backend read) and the TFVAR
    (what the operator edits) — all three, because each is a different place to look.

    BOTH build kinds: the config branch builds a SHORTER required list, and ``AGP_API_URL`` is
    one of the three keys that survive the subtraction — the harness ``deploy.yml`` reads it just
    as ``build.yml`` does. Pinned per kind so a change to either list is caught by name."""
    svc = _service(mocks, container_and_config_scaffold_dir)
    svc._repo_vars["AGP_API_URL"] = ""

    repo = _materialize(svc, template_name)

    step = _step(repo, "set_repo_vars")
    assert step.status.value == "failed"
    assert repo.status == "failed"
    assert step.error is not None
    assert "AGP_API_URL" in step.error, step.error
    assert "agp_api_url" in step.error, step.error
    mocks.rollout.set_ci_vars.assert_not_called()


def test_an_empty_tenant_ecr_repo_uri_fails_the_step_naming_the_seed_script(
    mocks, container_and_config_scaffold_dir
):
    """A tenant seeded WITHOUT the wiring flags carries a ``dev`` stage whose ``ecr_repo_uri`` is
    blank. ``_build_stage_ci_vars`` forwards it verbatim, the client drops it, and the build has
    no registry to push to. The hint names the key, the tenant, the stage FIELD and the script
    that fixes it — and names ONLY what is missing, so a present key (``AWS_REGION``) is not
    reported as a second problem the operator has to rule out."""
    svc = _service(mocks, container_and_config_scaffold_dir)
    svc._tenants = _tenants_with_dev(ecr_repo_uri="")

    repo = _materialize(svc, CONTAINER_TEMPLATE)

    step = _step(repo, "set_repo_vars")
    assert step.status.value == "failed"
    assert step.error is not None
    assert "ECR_REPOSITORY" in step.error, step.error
    assert "ecr_repo_uri" in step.error, step.error
    assert "ten-1" in step.error, step.error
    assert "seed_default_tenant.py" in step.error, step.error
    assert "AWS_REGION" not in step.error, step.error
    mocks.rollout.set_ci_vars.assert_not_called()


def test_an_empty_push_role_on_both_sources_fails_the_step_naming_both(
    mocks, container_and_config_scaffold_dir
):
    """The push role has two sources: the connection's per-org role wins, the tenant stage role stands
    in when the connection has none. Only when both are blank is the key missing, and the hint must say so —
    an operator who sees only "tenant stage" would re-seed a tenant when connecting the org's
    push role is the actual fix."""
    svc = _service(mocks, container_and_config_scaffold_dir)
    svc._tenants = _tenants_with_dev(push_role_arn="")

    repo = _materialize(svc, CONTAINER_TEMPLATE)

    step = _step(repo, "set_repo_vars")
    assert step.status.value == "failed"
    assert step.error is not None
    assert "AWS_ECR_PUSH_ROLE_ARN" in step.error, step.error
    assert "ecr_push_role_arn" in step.error, step.error
    assert "push_role_arn" in step.error, step.error
    mocks.rollout.set_ci_vars.assert_not_called()


def test_a_config_build_with_an_unwired_stage_still_passes_the_contract_check(
    mocks, container_and_config_scaffold_dir
):
    """The relaxation :func:`test_an_empty_deploy_role_is_single_account_mode_not_a_failure` pins does
    not weaken the contract check. A config build's contract is ``AGENT_ID``, ``CONNECTION_ID`` and
    ``AGP_API_URL`` only: the three image keys are subtracted, and an empty deploy role is single-account
    mode. So a tenant stage with a blank ``ecr_repo_uri``, ``push_role_arn`` and ``deploy_role_arn`` (the
    MCP and single-account shape) must still materialize a harness repo: none of those blanks is a key
    its ``deploy.yml`` reads."""
    svc = _service(mocks, container_and_config_scaffold_dir)
    svc._tenants = _tenants_with_dev(ecr_repo_uri="", push_role_arn="", deploy_role_arn="")

    repo = _materialize(svc, CONFIG_TEMPLATE)

    step = _step(repo, "set_repo_vars")
    assert step.status.value == "done", step.error
    written = _written_vars(mocks)
    assert written["AGENT_ID"] == "agent-1"
    assert written["CONNECTION_ID"] == "c1"
    assert written["AGP_API_URL"] == "https://agp.example/api/v1"
    assert written["AGP_DEPLOY_ROLE_ARN"] == ""


def test_with_no_tenant_service_the_hint_names_the_platform_setting_not_a_stage(
    mocks, container_and_config_scaffold_dir
):
    """THE HINT NAMES WHERE THE VALUE ACTUALLY CAME FROM. With no tenant service wired (the
    legacy construction) the image keys are the platform defaults ``api/routes/projects.py``
    builds from settings, so sending that operator to re-seed a tenant stage would be a wrong
    remediation — the same class of defect as a step that reads ``done`` over a half-written
    repo."""
    svc = _service(mocks, container_and_config_scaffold_dir)
    svc._tenants = None
    svc._repo_vars["ECR_REPOSITORY"] = ""

    repo = _materialize(svc, CONTAINER_TEMPLATE)

    step = _step(repo, "set_repo_vars")
    assert step.status.value == "failed"
    assert step.error is not None
    assert "ECR_REPOSITORY" in step.error, step.error
    assert "PROJECT_ECR_REPOSITORY" in step.error, step.error
    assert "tenant" not in step.error, step.error
    mocks.rollout.set_ci_vars.assert_not_called()


@pytest.mark.parametrize("template_name", [CONTAINER_TEMPLATE, CONFIG_TEMPLATE])
def test_a_complete_contract_is_written_and_the_step_reads_done(
    mocks, container_and_config_scaffold_dir, template_name
):
    """The check is a GATE, not a rewrite: with every required key non-empty the write happens
    exactly as before and the step completes; the exact dict each branch writes is pinned by the two
    exact-dict tests above."""
    svc = _service(mocks, container_and_config_scaffold_dir)

    repo = _materialize(svc, template_name)

    step = _step(repo, "set_repo_vars")
    assert step.status.value == "done", step.error
    written = _written_vars(mocks)
    required = {"AGENT_ID", "CONNECTION_ID", "AGP_API_URL"}
    if template_name == CONTAINER_TEMPLATE:
        required |= {"ECR_REPOSITORY", "AWS_REGION", "AWS_ECR_PUSH_ROLE_ARN"}
    assert all(written[key] for key in required), written
