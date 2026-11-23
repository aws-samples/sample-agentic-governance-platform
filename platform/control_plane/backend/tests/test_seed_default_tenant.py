"""Offline tests for ``scripts/seed_default_tenant.py``.

Pure-logic tests via injected in-memory fakes (no moto, no boto3): the script's
``run()`` takes the four services as parameters, so no AWS client is ever constructed
here. Every test injects an explicit ``account_id``/``region`` so STS is never called
(there is no hardcoded account anywhere — the real run derives it from the caller's
credentials). The backend dir goes on ``sys.path`` so ``import scripts.<mod>`` resolves,
as in ``test_demo_use_cases.py``.

Coverage:
  1. Seeding creates the ``default`` tenant exactly once, with the contract fields
     (name/LoB/groups/injected accounts/region); the CLI account override wins
     over STS.
  2. Records missing ``tenant_id`` (None/absent) get stamped ``"default"`` via each
     service's existing write path (``persist_identity`` / ``_save_project``).
  3. Records that already carry a ``tenant_id`` are untouched.
  4. A second run performs ZERO writes (idempotent).
  5. ``--dry-run`` performs ZERO writes and mutates nothing (pure listing +
     would-stamp counts).
"""

from __future__ import annotations

import importlib
import os
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import NoCredentialsError, ProfileNotFound

_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

seed = importlib.import_module("scripts.seed_default_tenant")

GROUP_ID = "11111111-2222-3333-4444-555555555555"
ACCOUNT_ID = "111122223333"
REGION = "eu-central-1"


class FakeTenantService:
    """Mimics TenantService.list()/upsert_seed() over a dict; counts writes."""

    def __init__(self, tenants=None) -> None:
        self.store = {t.id: t for t in (tenants or [])}
        self.upsert_calls = 0

    def list(self):
        return list(self.store.values())

    def upsert_seed(self, tenant):
        self.upsert_calls += 1
        self.store[tenant.id] = tenant
        return tenant


class FakeRegistry:
    """Mimics the agent/MCP registry surface the script uses: list() + persist_identity()."""

    def __init__(self, records=None) -> None:
        self.records = list(records or [])
        self.persist_calls = []

    def list(self):
        return list(self.records)

    def persist_identity(self, record):
        self.persist_calls.append(record)
        return record


class FakeProjectService:
    """Mimics ProjectService.list_projects()/_save_project(); counts writes."""

    def __init__(self, projects=None) -> None:
        self.projects = list(projects or [])
        self.save_calls = []

    def list_projects(self):
        return list(self.projects)

    def _save_project(self, record):
        self.save_calls.append(record)


def _record(rid: str, tenant_id=None) -> SimpleNamespace:
    return SimpleNamespace(id=rid, name=f"name-{rid}", tenant_id=tenant_id)


def _services(agents=(), mcps=(), projects=(), tenants=()):
    return {
        "tenant_service": FakeTenantService(list(tenants)),
        "agent_service": FakeRegistry(list(agents)),
        "mcp_server_service": FakeRegistry(list(mcps)),
        "project_service": FakeProjectService(list(projects)),
    }


def _run(svcs, *, dry_run=False, account_id: str | None = ACCOUNT_ID, region: str | None = REGION, **wiring):
    """Drive ``run()`` with an injected account/region (the ``--account-id`` /
    ``--region`` CLI path) so tests never resolve anything via STS or env. Extra
    ``**wiring`` kwargs (``dev_ecr_uri``/``*_role_arn`` …) pass through to ``run()``."""
    return seed.run(
        group_id=GROUP_ID,
        dry_run=dry_run,
        account_id=account_id,
        region=region,
        **wiring,
        **svcs,
    )


def test_seed_creates_default_tenant_with_contract_fields():
    svcs = _services()
    rc = _run(svcs)

    assert rc == 0
    tenant = svcs["tenant_service"].store["default"]
    assert tenant.id == "default"
    assert tenant.name == "Default-Platform"
    assert tenant.line_of_business == "Platform"
    assert tenant.entra_group_ids == [GROUP_ID]
    assert tenant.stages["dev"].account_id == ACCOUNT_ID
    assert tenant.stages["prod"].account_id == ACCOUNT_ID
    assert tenant.stages["dev"].region == REGION
    assert tenant.stages["prod"].region == REGION
    assert svcs["tenant_service"].upsert_calls == 1


def test_cli_account_override_wins_over_sts(monkeypatch):
    """An explicit account (the --account-id path) is used verbatim — STS is never
    consulted. Proven by poisoning boto3: any attempt to build an STS client blows
    up, yet the run succeeds with the injected account."""

    class _PoisonedBoto3:
        def client(self, *args, **kwargs):
            raise AssertionError("STS must not be called when --account-id is given")

        def __getattr__(self, name):
            raise AssertionError("boto3 must not be touched when --account-id is given")

    monkeypatch.setitem(sys.modules, "boto3", _PoisonedBoto3())
    svcs = _services()
    rc = _run(svcs)

    assert rc == 0
    tenant = svcs["tenant_service"].store["default"]
    assert tenant.stages["dev"].account_id == ACCOUNT_ID


def test_explicit_account_id_reaches_both_stages():
    """The injected account lands on BOTH the dev and prod stage configs."""
    other = "999988887777"
    svcs = _services()
    rc = _run(svcs, account_id=other)

    assert rc == 0
    tenant = svcs["tenant_service"].store["default"]
    assert tenant.stages["dev"].account_id == other
    assert tenant.stages["prod"].account_id == other


def test_wiring_values_land_on_stage_configs():
    """The ECR/role-ARN wiring kwargs land on the matching per-stage config."""
    svcs = _services()
    rc = _run(
        svcs,
        dev_ecr_uri="dev-ecr",
        prod_ecr_uri="prod-ecr",
        dev_deploy_role_arn="arn:dev-deploy",
        prod_deploy_role_arn="arn:prod-deploy",
        dev_push_role_arn="arn:dev-push",
        prod_push_role_arn="arn:prod-push",
    )

    assert rc == 0
    tenant = svcs["tenant_service"].store["default"]
    assert tenant.stages["dev"].ecr_repo_uri == "dev-ecr"
    assert tenant.stages["prod"].ecr_repo_uri == "prod-ecr"
    assert tenant.stages["dev"].deploy_role_arn == "arn:dev-deploy"
    assert tenant.stages["prod"].deploy_role_arn == "arn:prod-deploy"
    assert tenant.stages["dev"].push_role_arn == "arn:dev-push"
    assert tenant.stages["prod"].push_role_arn == "arn:prod-push"


def test_build_default_tenant_yields_nested_stages():
    """Unit-level: ``_build_default_tenant`` returns nested stages with the passed
    ecr/role values."""
    tenant = seed._build_default_tenant(
        GROUP_ID,
        account_id=ACCOUNT_ID,
        region=REGION,
        dev_ecr_uri="u",
        prod_ecr_uri="v",
        dev_deploy_role_arn="d",
        prod_deploy_role_arn="p",
    )
    assert set(tenant.stages) == {"dev", "prod"}
    assert tenant.stages["dev"].account_id == ACCOUNT_ID
    assert tenant.stages["dev"].ecr_repo_uri == "u"
    assert tenant.stages["prod"].ecr_repo_uri == "v"
    assert tenant.stages["dev"].deploy_role_arn == "d"
    assert tenant.stages["prod"].deploy_role_arn == "p"


def test_second_run_does_not_recreate_tenant():
    svcs = _services()
    _run(svcs)
    rc = _run(svcs)

    assert rc == 0
    assert svcs["tenant_service"].upsert_calls == 1
    assert list(svcs["tenant_service"].store) == ["default"]


def test_pre_existing_default_tenant_is_not_rewritten():
    existing = SimpleNamespace(id="default", name="Default")
    svcs = _services(tenants=[existing])
    rc = _run(svcs)

    assert rc == 0
    assert svcs["tenant_service"].upsert_calls == 0
    assert svcs["tenant_service"].store["default"] is existing


def test_run_wires_existing_unwired_tenant(capsys):
    """Exists + UNWIRED (empty ecr/deploy-role on both stages) + this run carries
    wiring ⇒ RE-WIRE in place; status 'wired'. Identity is preserved: with
    account_id/region left None the existing tenant's account/region are reused
    (so STS is never consulted — the create branch would have failed on None)."""
    existing = seed._build_default_tenant(GROUP_ID, account_id=ACCOUNT_ID, region=REGION)
    svcs = _services(tenants=[existing])

    rc = _run(
        svcs,
        account_id=None,
        region=None,
        dev_ecr_uri="dev-ecr",
        prod_ecr_uri="prod-ecr",
        dev_deploy_role_arn="arn:dev-deploy",
        prod_deploy_role_arn="arn:prod-deploy",
        dev_push_role_arn="arn:dev-push",
        prod_push_role_arn="arn:prod-push",
    )

    assert rc == 0
    assert svcs["tenant_service"].upsert_calls == 1
    tenant = svcs["tenant_service"].store["default"]
    assert tenant.stages["dev"].ecr_repo_uri == "dev-ecr"
    assert tenant.stages["prod"].ecr_repo_uri == "prod-ecr"
    assert tenant.stages["dev"].deploy_role_arn == "arn:dev-deploy"
    assert tenant.stages["prod"].deploy_role_arn == "arn:prod-deploy"
    assert tenant.stages["dev"].push_role_arn == "arn:dev-push"
    assert tenant.stages["prod"].push_role_arn == "arn:prod-push"
    assert tenant.stages["dev"].account_id == ACCOUNT_ID
    assert tenant.stages["dev"].region == REGION
    assert tenant.entra_group_ids == [GROUP_ID]
    assert "tenant=wired" in capsys.readouterr().out


def test_rewiring_a_tenant_with_no_identity_to_preserve_is_a_clear_error():
    """An unwired tenant whose dev stage carries no account or region, with no overrides on
    the command line, cannot be re-wired: the run names the flags instead of handing
    ``None`` to the tenant model."""
    existing = SimpleNamespace(
        id="default",
        stages={"dev": SimpleNamespace(deploy_role_arn="", ecr_repo_uri=""), "prod": SimpleNamespace()},
        entra_group_ids=[],
    )
    svcs = _services(tenants=[existing])

    with pytest.raises(RuntimeError) as exc:
        seed.run(group_id="", dry_run=False, account_id=None, region=None, dev_ecr_uri="dev-ecr", **svcs)

    assert "--account-id" in str(exc.value)
    assert svcs["tenant_service"].upsert_calls == 0


def test_run_leaves_wired_tenant_untouched(capsys):
    """Exists + ALREADY WIRED (operator-set deploy-role/ecr) ⇒ leave untouched even
    when the run supplies DIFFERENT wiring; status 'exists'. This preserves operator
    modifications — the seed must never clobber a hand-wired tenant."""
    existing = seed._build_default_tenant(
        GROUP_ID,
        account_id=ACCOUNT_ID,
        region=REGION,
        dev_ecr_uri="orig-dev-ecr",
        prod_ecr_uri="orig-prod-ecr",
        dev_deploy_role_arn="arn:orig-dev-deploy",
        prod_deploy_role_arn="arn:orig-prod-deploy",
    )
    svcs = _services(tenants=[existing])

    rc = _run(
        svcs,
        dev_ecr_uri="new-dev-ecr",
        prod_ecr_uri="new-prod-ecr",
        dev_deploy_role_arn="arn:new-dev-deploy",
        prod_deploy_role_arn="arn:new-prod-deploy",
    )

    assert rc == 0
    assert svcs["tenant_service"].upsert_calls == 0
    tenant = svcs["tenant_service"].store["default"]
    assert tenant is existing
    assert tenant.stages["dev"].ecr_repo_uri == "orig-dev-ecr"
    assert tenant.stages["dev"].deploy_role_arn == "arn:orig-dev-deploy"
    assert "tenant=exists" in capsys.readouterr().out


def test_run_no_wiring_values_is_noop_on_existing(capsys):
    """Exists + UNWIRED but the run carries NO wiring values ⇒ no-op; status 'exists'.
    A bare re-run (e.g. Terraform apply with no ECR/role wiring yet) must not write."""
    existing = seed._build_default_tenant(GROUP_ID, account_id=ACCOUNT_ID, region=REGION)
    svcs = _services(tenants=[existing])

    rc = _run(svcs)

    assert rc == 0
    assert svcs["tenant_service"].upsert_calls == 0
    tenant = svcs["tenant_service"].store["default"]
    assert tenant is existing
    assert tenant.stages["dev"].ecr_repo_uri == ""
    assert "tenant=exists" in capsys.readouterr().out


def test_unstamped_records_get_stamped_default():
    agents = [_record("a1"), _record("a2", tenant_id="ten-x")]
    mcps = [_record("m1")]
    projects = [_record("p1"), _record("p2")]
    svcs = _services(agents=agents, mcps=mcps, projects=projects)

    rc = _run(svcs)

    assert rc == 0
    assert [r.id for r in svcs["agent_service"].persist_calls] == ["a1"]
    assert agents[0].tenant_id == "default"
    assert [r.id for r in svcs["mcp_server_service"].persist_calls] == ["m1"]
    assert mcps[0].tenant_id == "default"
    assert [r.id for r in svcs["project_service"].save_calls] == ["p1", "p2"]
    assert all(p.tenant_id == "default" for p in projects)


def test_already_stamped_records_are_untouched():
    agents = [_record("a1", tenant_id="ten-claims"), _record("a2", tenant_id="default")]
    mcps = [_record("m1", tenant_id="ten-uw")]
    projects = [_record("p1", tenant_id="default")]
    svcs = _services(agents=agents, mcps=mcps, projects=projects)

    rc = _run(svcs)

    assert rc == 0
    assert svcs["agent_service"].persist_calls == []
    assert svcs["mcp_server_service"].persist_calls == []
    assert svcs["project_service"].save_calls == []
    assert agents[0].tenant_id == "ten-claims"
    assert mcps[0].tenant_id == "ten-uw"


def test_second_run_performs_zero_writes():
    svcs = _services(agents=[_record("a1")], mcps=[_record("m1")], projects=[_record("p1")])

    assert _run(svcs) == 0
    first = (
        svcs["tenant_service"].upsert_calls,
        len(svcs["agent_service"].persist_calls),
        len(svcs["mcp_server_service"].persist_calls),
        len(svcs["project_service"].save_calls),
    )
    assert first == (1, 1, 1, 1)

    assert _run(svcs) == 0
    second = (
        svcs["tenant_service"].upsert_calls,
        len(svcs["agent_service"].persist_calls),
        len(svcs["mcp_server_service"].persist_calls),
        len(svcs["project_service"].save_calls),
    )
    assert second == first


def test_dry_run_writes_nothing_and_mutates_nothing(capsys):
    agents = [_record("a1"), _record("a2", tenant_id="ten-x")]
    mcps = [_record("m1")]
    projects = [_record("p1")]
    svcs = _services(agents=agents, mcps=mcps, projects=projects)

    rc = _run(svcs, dry_run=True)

    assert rc == 0
    assert svcs["tenant_service"].upsert_calls == 0
    assert "default" not in svcs["tenant_service"].store
    assert svcs["agent_service"].persist_calls == []
    assert svcs["mcp_server_service"].persist_calls == []
    assert svcs["project_service"].save_calls == []
    assert agents[0].tenant_id is None
    assert mcps[0].tenant_id is None
    assert projects[0].tenant_id is None
    out = capsys.readouterr().out
    assert "agents=1/2" in out
    assert "mcp_servers=1/1" in out
    assert "projects=1/1" in out


def test_summary_counts_printed(capsys):
    svcs = _services(agents=[_record("a1"), _record("a2")], mcps=[], projects=[_record("p1", tenant_id="t")])

    assert _run(svcs) == 0
    out = capsys.readouterr().out
    assert "tenant=created" in out
    assert "agents=2/2" in out
    assert "mcp_servers=0/0" in out
    assert "projects=0/1" in out


def test_unconfigured_service_is_skipped():
    """A None service (registry not configured) is skipped — the rest still runs."""
    svcs = _services(projects=[_record("p1")])
    svcs["agent_service"] = None
    svcs["mcp_server_service"] = None

    rc = _run(svcs)

    assert rc == 0
    assert svcs["tenant_service"].upsert_calls == 1
    assert [r.id for r in svcs["project_service"].save_calls] == ["p1"]


def test_stamp_failure_continues_and_returns_nonzero():
    class ExplodingRegistry(FakeRegistry):
        def persist_identity(self, record):
            if record.id == "a1":
                raise RuntimeError("boom")
            return super().persist_identity(record)

    svcs = _services(agents=[_record("a1"), _record("a2")])
    svcs["agent_service"] = ExplodingRegistry(svcs["agent_service"].records)

    rc = _run(svcs)

    assert rc == 1
    assert [r.id for r in svcs["agent_service"].persist_calls] == ["a2"]


def test_main_requires_group_id(monkeypatch):
    """No --group-id and no DEFAULT_TENANT_GROUP_ID env ⇒ exit 2 BEFORE any settings
    import (the lazy-import contract — the script must not need full env)."""
    monkeypatch.delenv("DEFAULT_TENANT_GROUP_ID", raising=False)
    assert seed.main(["--dry-run"]) == 2


_FULL_TFVARS = (
    'aws_region   = "us-east-1"\n'
    'project_name = "agp"\n'
    'environment  = "test-fixture"\n'
    'agent_registry_id = "RegAgents"\n'
    'mcp_registry_id   = "RegMcp"\n'
)

_STRAY_ENV = (
    "AWS_REGION=eu-west-1\n"
    "PROJECT_NAME=stray\n"
    "ENVIRONMENT=prod\n"
    "AGENT_REGISTRY_ID=RegFromStrayEnv\n"
    "MCP_REGISTRY_ID=McpFromStrayEnv\n"
)


def _write_infra(tmp_path, tfvars_text=None, stray_env_text=None):
    """Build a fake infra dir under tmp_path with an optional ``terraform.tfvars``.

    ``stray_env_text`` writes an ``infrastructure/.env``. NOTHING reads that file any
    more; it is written here only to prove it has no effect on the resolved config."""
    infra = tmp_path / "infrastructure"
    infra.mkdir()
    if tfvars_text is not None:
        (infra / "terraform.tfvars").write_text(tfvars_text, encoding="utf-8")
    if stray_env_text is not None:
        (infra / ".env").write_text(stray_env_text, encoding="utf-8")
    return infra


def test_parse_tfvars_scalars_skip_blocks_and_inline_comments(tmp_path):
    infra = _write_infra(
        tmp_path,
        tfvars_text=(
            "# AWS Configuration\n"
            'aws_region  = "us-east-1"\n'
            'environment = "dev"\n'
            'project_name = "agp"\n'
            'hosted_zone_id = "" # Leave empty if not using custom domain\n'
            "ecs_task_cpu = 512 # inline comment after a number\n"
            'availability_zones = ["us-east-1a", "us-east-1b"]\n'
            "tags = {\n"
            '  Project   = "agp"\n'
            '  ManagedBy = "terraform"\n'
            "}\n"
            'agent_registry_id = "RegBBBB"\n'
            'mcp_registry_id   = "RegCCCC"\n'
        ),
    )
    cfg = seed._load_infra_config(infra)
    assert cfg["AWS_REGION"] == "us-east-1"
    assert cfg["ENVIRONMENT"] == "dev"
    assert cfg["PROJECT_NAME"] == "agp"
    assert cfg["AGENT_REGISTRY_ID"] == "RegBBBB"
    assert cfg["MCP_REGISTRY_ID"] == "RegCCCC"
    assert cfg["ECS_TASK_CPU"] == "512"
    assert "HOSTED_ZONE_ID" not in cfg
    assert "AVAILABILITY_ZONES" not in cfg
    assert "TAGS" not in cfg
    assert "PROJECT" not in cfg
    assert "MANAGEDBY" not in cfg


def test_a_stray_env_file_is_ignored_entirely(tmp_path):
    """``infrastructure/.env`` is a phantom config surface: no adopter should create
    one, nothing in the repo writes one, and this script no longer reads one. A stray
    file on disk — even one that sets every key, with values that conflict with the
    tfvars — contributes NOTHING to the resolved config."""
    infra = _write_infra(
        tmp_path,
        tfvars_text=_FULL_TFVARS,
        stray_env_text=_STRAY_ENV + "VPC_ID=vpc-from-stray-env\n",
    )
    cfg = seed._load_infra_config(infra)
    assert cfg["AWS_REGION"] == "us-east-1"
    assert cfg["PROJECT_NAME"] == "agp"
    assert cfg["ENVIRONMENT"] == "test-fixture"
    assert cfg["AGENT_REGISTRY_ID"] == "RegAgents"
    assert cfg["MCP_REGISTRY_ID"] == "RegMcp"
    assert "VPC_ID" not in cfg


def test_a_stray_env_file_alone_yields_an_empty_config(tmp_path):
    """No ``terraform.tfvars`` + a fully-populated stray ``.env`` ⇒ empty config. The
    ``.env`` is not a fallback either; it is simply not a source."""
    infra = _write_infra(tmp_path, stray_env_text=_STRAY_ENV)
    assert seed._load_infra_config(infra) == {}


def test_the_env_parser_no_longer_exists():
    """Guard against the ``.env`` leg being reintroduced by a future copy-paste: the
    parser itself is gone from both clone scripts, and with a single config file there is
    no per-key precedence left to encode."""
    assert not hasattr(seed, "_parse_env_file")
    assert not hasattr(seed, "DERIVATION_KEYS")


def test_missing_infra_files_yield_empty_config(tmp_path):
    infra = tmp_path / "infrastructure"
    infra.mkdir()
    assert seed._load_infra_config(infra) == {}


class _StsBoto3:
    """Minimal ``boto3`` stand-in: ``client("sts").get_caller_identity()`` returns the
    configured account. Injected into ``sys.modules`` so the script's lazy ``import boto3``
    picks it up — no credentials, no network (conftest's guard fails any real STS call)."""

    def __init__(self, account: str) -> None:
        self._account = account

    def client(self, service, **kwargs):
        assert service == "sts"
        return SimpleNamespace(get_caller_identity=lambda: {"Account": self._account})


class _UnreachableBoto3:
    """``boto3`` stand-in whose STS client cannot be built — models "no credentials / no
    network"; the script treats a botocore failure as "the account cannot be derived"."""

    def client(self, service, **kwargs):
        raise NoCredentialsError()


def _fake_sts(monkeypatch, account: str) -> None:
    """Make the caller's live STS identity ``account`` for one test."""
    monkeypatch.setitem(sys.modules, "boto3", _StsBoto3(account))


def _unreachable_sts(monkeypatch) -> None:
    """Make STS unreachable for one test (no credentials / offline)."""
    monkeypatch.setitem(sys.modules, "boto3", _UnreachableBoto3())


def _args(infra_dir, **overrides):
    """A parse_args-shaped namespace with every config flag defaulted to None."""
    base: dict[str, Any] = dict(
        infra_dir=str(infra_dir),
        region=None,
        account_id=None,
        agent_registry_id=None,
        mcp_registry_id=None,
        tenants_table=None,
        projects_table=None,
        dry_run=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_resolution_cli_beats_the_tfvars(tmp_path):
    infra = _write_infra(tmp_path, tfvars_text=_FULL_TFVARS, stray_env_text=_STRAY_ENV)
    cfg = seed._resolve_runtime_config(
        _args(
            infra,
            region="eu-central-1",
            account_id="999988887777",
            agent_registry_id="CliAgents",
            mcp_registry_id="CliMcp",
            tenants_table="cli-tenants",
            projects_table="cli-projects",
        )
    )
    assert cfg == {
        "region": "eu-central-1",
        "account_id": "999988887777",
        "agent_registry_id": "CliAgents",
        "mcp_registry_id": "CliMcp",
        "tenants_table": "cli-tenants",
        "projects_table": "cli-projects",
    }


def test_resolution_tfvars_beats_derivation(tmp_path, monkeypatch):
    """Region + registry ids come from ``terraform.tfvars``, and the tables are derived
    from its project_name/environment plus the caller's account (which is always STS's —
    the file is not an account source at all, see §7b)."""
    _fake_sts(monkeypatch, "111122223333")
    infra = _write_infra(tmp_path, tfvars_text=_FULL_TFVARS)
    cfg = seed._resolve_runtime_config(_args(infra))
    assert cfg["region"] == "us-east-1"
    assert cfg["account_id"] == "111122223333"
    assert cfg["agent_registry_id"] == "RegAgents"
    assert cfg["mcp_registry_id"] == "RegMcp"
    assert cfg["tenants_table"] == "agp-cp-test-fixture-223333-tenants"
    assert cfg["projects_table"] == "agp-cp-test-fixture-223333-projects"


def test_resolution_derived_tables_use_injected_sts_account(tmp_path, monkeypatch):
    """No --account-id ⇒ the STS-derived (faked) account feeds <acct6>."""
    _fake_sts(monkeypatch, "444455556666")
    infra = _write_infra(
        tmp_path,
        tfvars_text=(
            'aws_region   = "us-east-1"\n'
            'project_name = "agp"\n'
            'environment  = "test-fixture"\n'
            'agent_registry_id = "RegAgents"\n'
        ),
    )
    cfg = seed._resolve_runtime_config(_args(infra))
    assert cfg["account_id"] == "444455556666"
    assert cfg["tenants_table"] == "agp-cp-test-fixture-556666-tenants"
    assert cfg["projects_table"] == "agp-cp-test-fixture-556666-projects"
    assert cfg["mcp_registry_id"] is None


def test_resolution_derived_tables_ignore_a_stray_env_file(tmp_path, monkeypatch):
    """Terraform only ever read the TFVARS project_name/environment for name_prefix, so the
    REAL tables are
    agp-cp-test-fixture-…. A stray ``.env`` claiming PROJECT_NAME=stray/ENVIRONMENT=prod cannot
    skew the derivation onto tables that do not exist — nor can it move the region."""
    _fake_sts(monkeypatch, "111122223333")
    infra = _write_infra(
        tmp_path,
        tfvars_text=(
            'project_name = "agp"\n'
            'environment  = "test-fixture"\n'
            'aws_region   = "us-east-1"\n'
            'agent_registry_id = "RegAgents"\n'
        ),
        stray_env_text=_STRAY_ENV,
    )
    cfg = seed._resolve_runtime_config(_args(infra))
    assert cfg["region"] == "us-east-1"
    assert cfg["agent_registry_id"] == "RegAgents"
    assert cfg["tenants_table"] == "agp-cp-test-fixture-223333-tenants"
    assert cfg["projects_table"] == "agp-cp-test-fixture-223333-projects"


def test_account_comes_from_sts_and_ignores_the_infra_files(tmp_path, monkeypatch):
    """An account id left in the infra files has NO effect: the caller's STS account is
    what resolves and what the table names derive from. The credentials already say which
    account the script acts on, and that account both derives the table names and is
    stamped onto the tenant's stages, so a checked-in copy is a second answer that can
    silently disagree with the live one. This is the regression guard — that key used to
    outrank the live credentials, so a stale value derived table names for an account the
    caller cannot even reach and died on a bare ResourceNotFoundException."""
    _fake_sts(monkeypatch, "444455556666")
    infra = _write_infra(
        tmp_path,
        tfvars_text=_FULL_TFVARS + 'aws_account_id = "111122223333"\n',
        stray_env_text="AWS_ACCOUNT_ID=111122223333\n",
    )

    cfg = seed._resolve_runtime_config(_args(infra))

    assert cfg["account_id"] == "444455556666"
    assert cfg["tenants_table"] == "agp-cp-test-fixture-556666-tenants"
    assert cfg["projects_table"] == "agp-cp-test-fixture-556666-projects"


def test_cli_account_flag_beats_sts(tmp_path, monkeypatch):
    """--account-id is a DELIBERATE operator override (e.g. seeding a tenant that points at
    another account), so it wins and STS is never consulted. Proven by poisoning boto3: any
    STS touch blows up, yet the run resolves the flag's account."""

    class _PoisonedBoto3:
        def client(self, *args, **kwargs):
            raise AssertionError("STS must not be consulted when --account-id is given")

        def __getattr__(self, name):
            raise AssertionError("boto3 must not be touched when --account-id is given")

    monkeypatch.setitem(sys.modules, "boto3", _PoisonedBoto3())
    infra = _write_infra(tmp_path, tfvars_text=_FULL_TFVARS)

    cfg = seed._resolve_runtime_config(_args(infra, account_id="999988887777"))

    assert cfg["account_id"] == "999988887777"
    assert cfg["tenants_table"] == "agp-cp-test-fixture-887777-tenants"


def test_unreachable_sts_degrades_to_the_placeholder_in_dry_run(tmp_path, monkeypatch):
    """No flag + STS unreachable + --dry-run ⇒ the placeholder. Dry-run must keep working
    with no cloud access at all; it writes nothing, so a placeholder account can only show
    up in the printed plan."""
    _unreachable_sts(monkeypatch)
    infra = _write_infra(
        tmp_path,
        tfvars_text='aws_region = "us-east-1"\nagent_registry_id = "RegAgents"\n',
    )

    cfg = seed._resolve_runtime_config(_args(infra, dry_run=True, tenants_table="t", projects_table="p"))

    assert cfg["account_id"] == seed.ACCOUNT_PLACEHOLDER


def test_unreachable_sts_raises_in_a_real_run(tmp_path, monkeypatch):
    """No flag + STS unreachable + real run ⇒ raises, naming the escape hatch. The seed
    never writes a guessed account."""
    _unreachable_sts(monkeypatch)
    infra = _write_infra(
        tmp_path,
        tfvars_text='aws_region = "us-east-1"\nagent_registry_id = "RegAgents"\n',
    )

    with pytest.raises(RuntimeError) as exc:
        seed._resolve_runtime_config(_args(infra, tenants_table="t", projects_table="p"))

    assert "--account-id" in str(exc.value)


def test_malformed_account_flag_is_rejected(tmp_path, monkeypatch):
    """The 12-digit rule applies to the flag too, and in --dry-run as well: a malformed
    --account-id is a caller error, and previewing a plan built on it is pointless."""
    _unreachable_sts(monkeypatch)
    infra = _write_infra(tmp_path, tfvars_text=_FULL_TFVARS)

    with pytest.raises(RuntimeError) as exc:
        seed._resolve_runtime_config(_args(infra, account_id="12345", dry_run=True))

    assert "12-digit" in str(exc.value)


def test_malformed_sts_account_is_rejected(tmp_path, monkeypatch):
    """The 12-digit rule applies to the STS path too — nothing reaches the tenant model
    unvalidated, whatever the source."""
    _fake_sts(monkeypatch, "not-an-account")
    infra = _write_infra(tmp_path, tfvars_text=_FULL_TFVARS)

    with pytest.raises(RuntimeError) as exc:
        seed._resolve_runtime_config(_args(infra))

    assert "12-digit" in str(exc.value)


def test_resolve_region_without_any_source_degrades_only_in_dry_run(monkeypatch):
    """No --region, no AWS_REGION/AWS_DEFAULT_REGION and a boto3 session with no region:
    dry-run continues with the placeholder, a real run refuses to guess."""
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.setitem(
        sys.modules, "boto3", SimpleNamespace(Session=lambda: SimpleNamespace(region_name=None))
    )

    assert seed._resolve_region(None, dry_run=True) == seed.REGION_PLACEHOLDER
    with pytest.raises(RuntimeError) as exc:
        seed._resolve_region(None, dry_run=False)
    assert "--region" in str(exc.value)


def test_resolve_region_treats_a_broken_boto3_profile_as_unresolved(monkeypatch):
    """A botocore failure while building the session (a missing profile) is "no region
    configured", not a crash."""

    def _broken_session():
        raise ProfileNotFound(profile="missing")

    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(Session=_broken_session))

    assert seed._resolve_region(None, dry_run=True) == seed.REGION_PLACEHOLDER


def test_resolution_missing_region_is_clear_error(tmp_path):
    infra = _write_infra(
        tmp_path,
        tfvars_text='project_name = "agp"\nenvironment = "dev"\n',
        stray_env_text="AWS_REGION=eu-west-1\n",
    )
    with pytest.raises(RuntimeError) as exc:
        seed._resolve_runtime_config(_args(infra))
    msg = str(exc.value)
    assert "--region" in msg
    assert "terraform.tfvars" in msg
    assert ".env" not in msg


def test_resolution_missing_agent_registry_is_clear_error(tmp_path, monkeypatch):
    """The account resolves before the registry check, so the caller gets an STS identity.
    ``terraform.tfvars`` is not named as a source: the root variable no longer exists, so
    setting an id there only draws an undeclared-variable warning from Terraform."""
    _fake_sts(monkeypatch, "111122223333")
    infra = _write_infra(tmp_path, tfvars_text='aws_region = "us-east-1"\n')
    with pytest.raises(RuntimeError) as exc:
        seed._resolve_runtime_config(_args(infra))
    msg = str(exc.value)
    assert "--agent-registry-id" in msg
    assert "resolves the registry by NAME at runtime" in msg
    assert "terraform.tfvars" not in msg
    assert ".env" not in msg


def test_resolution_missing_table_derivation_inputs_is_clear_error(tmp_path, monkeypatch):
    """Region/account/registry present but no PROJECT_NAME/ENVIRONMENT and no
    --tenants-table ⇒ the derivation errors naming the sources tried."""
    _fake_sts(monkeypatch, "111122223333")
    infra = _write_infra(
        tmp_path,
        tfvars_text='aws_region = "us-east-1"\nagent_registry_id = "RegAgents"\n',
    )
    with pytest.raises(RuntimeError) as exc:
        seed._resolve_runtime_config(_args(infra))
    msg = str(exc.value)
    assert "--tenants-table" in msg
    assert "project_name/environment" in msg
    assert "terraform.tfvars" in msg
    assert ".env" not in msg


def test_explicit_table_overrides_skip_derivation(tmp_path, monkeypatch):
    """--tenants-table/--projects-table bypass the PROJECT_NAME/ENVIRONMENT need."""
    _fake_sts(monkeypatch, "111122223333")
    infra = _write_infra(
        tmp_path,
        tfvars_text='aws_region = "us-east-1"\nagent_registry_id = "RegAgents"\n',
    )
    cfg = seed._resolve_runtime_config(_args(infra, tenants_table="my-tenants", projects_table="my-projects"))
    assert cfg["tenants_table"] == "my-tenants"
    assert cfg["projects_table"] == "my-projects"
