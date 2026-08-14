"""Offline tests for ``scripts/backfill_langfuse_projects.py`` (E26/T12).

Pure-logic tests via injected in-memory fakes (NO moto, NO boto3, NO live Langfuse):
the script's ``run()`` takes the registry + provisioner as parameters, so no AWS client
and no HTTP session is ever constructed here. Mirrors the
``tests/test_seed_default_tenant.py`` idiom of putting the backend dir on ``sys.path``
so ``import scripts.<mod>`` resolves.

Coverage (task brief):
  1. ``--dry-run`` lists ONLY the unprovisioned agents and never calls the provisioner.
  2. Apply mode provisions exactly the agents missing ``langfuse_project_id``.
  3. One agent's failure does not abort the others; the summary counts it and the exit
     code is non-zero.
  4. ``--agent-id`` restricts the backfill to that agent; an UNKNOWN id is a failure
     (non-zero exit) — a typo must never produce a green run that did nothing.
  5. The secret VALUE never reaches stdout — only the secret NAME + project id do, and
     ``--verbose`` never enables the AWS SDK / HTTP wire loggers (which would dump the
     ``CreateSecret`` request body, secret key included, to stderr).
  6. Infra config resolution: ``terraform.tfvars`` parsing, the documented precedence
     (CLI flag > ``terraform.tfvars`` scalars > ambient environment), the
     ``--langfuse-host`` override, the derived admin secret name, and the fail-fast
     when ``LANGFUSE_HOST`` is unresolvable (provisions NOTHING). A stray
     ``infrastructure/.env`` is IGNORED — the ``.env`` leg was removed in E34/T10.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

# The backend dir on sys.path makes `scripts` importable as a namespace package
# (same shim as tests/test_seed_default_tenant.py). The script itself is import-safe:
# stdlib-only at module top, everything else lazily imported.
_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from models.agent import Agent, AuthType, LifecycleState, Platform  # noqa: E402
import scripts.backfill_langfuse_projects as backfill  # noqa: E402

# The secret VALUE a real provisioner would mint — must NEVER be printed (test 5).
SECRET_KEY_VALUE = "sk-lf-NEVER-PRINT-THIS-VALUE"
PUBLIC_KEY_VALUE = "pk-lf-NEVER-PRINT-THIS-EITHER"


def _agent(
    *,
    agent_id: str,
    name: str,
    langfuse_project_id: str | None = None,
    langfuse_key_secret_name: str | None = None,
) -> Agent:
    now = datetime.now(timezone.utc)
    return Agent(
        id=agent_id,
        name=name,
        purpose="Pre-E26 agent",
        lifecycle_state=LifecycleState.APPROVED,
        platform=Platform.AWS_BEDROCK,
        auth_type=AuthType.ENTRA,
        langfuse_project_id=langfuse_project_id,
        langfuse_key_secret_name=langfuse_key_secret_name,
        created_at=now,
        updated_at=now,
    )


class FakeRegistry:
    """Mimics AgentRegistryService.list() over a list; counts envelope writes."""

    def __init__(self, agents) -> None:
        self._agents = list(agents)
        self.persist_calls = []

    def list(self):
        return list(self._agents)

    def persist_identity(self, agent):
        self.persist_calls.append(agent.id)
        return agent


class FakeProvisioner:
    """Mimics LangfuseProvisioningService.provision_agent_project (C2 contract).

    Writes the C1 join onto the envelope + persists it (like the real one) and returns
    the non-secret dict. ``fail_ids`` makes it raise for specific agents.
    """

    def __init__(self, registry=None, fail_ids=()) -> None:
        self.registry = registry
        self.fail_ids = set(fail_ids)
        self.calls = []

    def provision_agent_project(self, agent):
        self.calls.append(agent.id)
        if agent.id in self.fail_ids:
            raise RuntimeError(f"Langfuse tRPC failed for {agent.id}")
        secret_name = f"langfuse-agent-{agent.id}-keys"
        project_id = f"proj-{agent.id}"
        agent.langfuse_project_id = project_id
        agent.langfuse_key_secret_name = secret_name
        if self.registry is not None:
            self.registry.persist_identity(agent)
        # The real provisioner returns the public key (non-secret) — the script must
        # still never print it, and it never returns the secret key at all.
        return {
            "project_id": project_id,
            "secret_name": secret_name,
            "public_key": PUBLIC_KEY_VALUE,
        }


def _fixture_agents():
    """Three agents: #1 + #3 unprovisioned (pre-E26), #2 already provisioned."""
    return [
        _agent(agent_id="rec-001", name="Contact Center"),
        _agent(
            agent_id="rec-002",
            name="FNOL Intake",
            langfuse_project_id="proj-existing",
            langfuse_key_secret_name="langfuse-agent-rec-002-keys",
        ),
        _agent(agent_id="rec-003", name="Onboarding"),
    ]


# ---------------------------------------------------------------------------
# 1) dry-run lists the unprovisioned agents and calls NOTHING
# ---------------------------------------------------------------------------


def test_dry_run_lists_unprovisioned_and_calls_nothing(capsys):
    registry = FakeRegistry(_fixture_agents())
    provisioner = FakeProvisioner(registry=registry)

    exit_code = backfill.run(registry=registry, provisioner=provisioner, dry_run=True)
    out = capsys.readouterr().out

    assert exit_code == 0
    # Nothing was called and nothing was written.
    assert provisioner.calls == []
    assert registry.persist_calls == []
    # Only the unprovisioned agents appear in the plan.
    assert "rec-001" in out
    assert "rec-003" in out
    assert "rec-002" not in out
    assert "Contact Center" in out and "Onboarding" in out
    assert "dry run" in out.lower()


# ---------------------------------------------------------------------------
# 2) apply mode provisions exactly the unprovisioned agents
# ---------------------------------------------------------------------------


def test_backfill_provisions_only_unprovisioned(capsys):
    registry = FakeRegistry(_fixture_agents())
    provisioner = FakeProvisioner(registry=registry)

    exit_code = backfill.run(registry=registry, provisioner=provisioner, dry_run=False)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert provisioner.calls == ["rec-001", "rec-003"]  # rec-002 skipped
    assert registry.persist_calls == ["rec-001", "rec-003"]
    # The operator gets the project id + the Secrets Manager NAME per provisioned agent.
    assert "proj-rec-001" in out
    assert "langfuse-agent-rec-001-keys" in out
    assert "proj-rec-003" in out
    assert "langfuse-agent-rec-003-keys" in out
    assert "provisioned=2" in out and "skipped=1" in out and "failed=0" in out


# ---------------------------------------------------------------------------
# 3) one failure does not abort the rest; exit code is non-zero
# ---------------------------------------------------------------------------


def test_one_failure_does_not_abort_others(capsys):
    agents = [
        _agent(agent_id="rec-001", name="Contact Center"),
        _agent(agent_id="rec-002", name="FNOL Intake"),
        _agent(agent_id="rec-003", name="Onboarding"),
    ]
    registry = FakeRegistry(agents)
    provisioner = FakeProvisioner(registry=registry, fail_ids={"rec-002"})

    exit_code = backfill.run(registry=registry, provisioner=provisioner, dry_run=False)
    out = capsys.readouterr().out

    # All three were attempted; #1 and #3 succeeded despite #2 blowing up.
    assert provisioner.calls == ["rec-001", "rec-002", "rec-003"]
    assert registry.persist_calls == ["rec-001", "rec-003"]
    assert agents[0].langfuse_project_id == "proj-rec-001"
    assert agents[2].langfuse_project_id == "proj-rec-003"
    assert agents[1].langfuse_project_id is None
    # Summary reports the failure and the exit status is non-zero.
    assert "provisioned=2" in out and "failed=1" in out
    assert "rec-002" in out
    assert exit_code != 0


# ---------------------------------------------------------------------------
# 4) --agent-id restricts the set
# ---------------------------------------------------------------------------


def test_agent_id_filter(capsys):
    registry = FakeRegistry(_fixture_agents())
    provisioner = FakeProvisioner(registry=registry)

    exit_code = backfill.run(
        registry=registry,
        provisioner=provisioner,
        dry_run=False,
        agent_ids=["rec-003"],
    )
    out = capsys.readouterr().out

    assert exit_code == 0
    assert provisioner.calls == ["rec-003"]
    assert registry.persist_calls == ["rec-003"]
    assert "rec-001" not in out

    # The CLI accepts the flag repeatedly AND comma-separated.
    args = backfill.parse_args(["--agent-id", "a,b", "--agent-id", "c"])
    assert backfill._selected_agent_ids(args) == ["a", "b", "c"]


def test_unknown_agent_id_is_a_failure_not_a_green_run(capsys):
    """A TYPO'd --agent-id must be loud: nothing is provisioned AND the exit code is
    non-zero, so the operator never mistakes a no-op for success."""
    registry = FakeRegistry(_fixture_agents())
    provisioner = FakeProvisioner(registry=registry)

    exit_code = backfill.run(
        registry=registry,
        provisioner=provisioner,
        dry_run=False,
        agent_ids=["rec-00l"],  # typo: lowercase L instead of 1
    )
    out = capsys.readouterr().out

    assert exit_code != 0
    assert provisioner.calls == []
    assert registry.persist_calls == []
    assert "provisioned=0" in out and "failed=1" in out
    assert "rec-00l" in out


def test_unknown_agent_id_alongside_a_valid_one_still_provisions_and_fails(capsys):
    """A mix of a good id and a typo: the good one IS provisioned, but the run still
    exits non-zero and names the unknown id."""
    registry = FakeRegistry(_fixture_agents())
    provisioner = FakeProvisioner(registry=registry)

    exit_code = backfill.run(
        registry=registry,
        provisioner=provisioner,
        dry_run=False,
        agent_ids=["rec-003", "nope"],
    )
    out = capsys.readouterr().out

    assert exit_code != 0
    assert provisioner.calls == ["rec-003"]
    assert "provisioned=1" in out and "failed=1" in out
    assert "nope" in out


def test_unknown_agent_id_in_dry_run_is_also_non_zero(capsys):
    registry = FakeRegistry(_fixture_agents())
    provisioner = FakeProvisioner(registry=registry)

    exit_code = backfill.run(
        registry=registry,
        provisioner=provisioner,
        dry_run=True,
        agent_ids=["ghost"],
    )
    out = capsys.readouterr().out

    assert exit_code != 0
    assert provisioner.calls == []
    assert "ghost" in out


# ---------------------------------------------------------------------------
# 5) no secret VALUE can ever be printed
# ---------------------------------------------------------------------------


def test_no_secret_value_printed(capsys):
    registry = FakeRegistry(_fixture_agents())
    provisioner = FakeProvisioner(registry=registry)

    backfill.run(registry=registry, provisioner=provisioner, dry_run=False)
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert SECRET_KEY_VALUE not in combined
    assert PUBLIC_KEY_VALUE not in combined
    assert "secret_key" not in combined
    assert "public_key" not in combined
    # ...while the NON-secret join IS surfaced (that's what operators need).
    assert "langfuse-agent-rec-001-keys" in captured.out
    assert "proj-rec-001" in captured.out


# ---------------------------------------------------------------------------
# 5b) --verbose can never turn ON the wire loggers that would leak the secret
#
# WHY this is a separate test from test_no_secret_value_printed: that test drives
# ``run()`` with FakeProvisioner, which never touches boto3/httpx, so it structurally
# cannot observe a wire-log leak. The real leak vector is the LOGGING SETUP itself: if
# ``--verbose`` ever put ``botocore`` (or httpx/httpcore, for the Langfuse tRPC calls)
# at DEBUG, botocore.endpoint/botocore.parsers would write the full request BODY of
# every AWS call to stderr — and the provisioner's CreateSecret/PutSecretValue body
# CONTAINS the Langfuse secret key VALUE (``"secret_key": "sk-lf-…"``). So the thing
# worth asserting is a property of ``_configure_logging`` itself: no logger in
# WIRE_LOGGERS is ever at DEBUG, even in verbose mode.
# ---------------------------------------------------------------------------


@pytest.fixture
def logging_levels_restored():
    """Snapshot/restore global logging state so these tests cannot leak level changes
    (``_configure_logging`` mutates process-wide loggers) into any other test."""
    names = ("", "backfill_langfuse_projects", *backfill.WIRE_LOGGERS)
    saved_levels = {name: logging.getLogger(name).level for name in names}
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    try:
        yield
    finally:
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)
        root.handlers[:] = saved_handlers


def test_verbose_never_enables_the_wire_loggers(logging_levels_restored):
    backfill._configure_logging(verbose=True)

    # The whole point of --verbose: THIS script's logger goes to DEBUG...
    assert backfill.logger.getEffectiveLevel() == logging.DEBUG
    # ...while every AWS-SDK / HTTP wire logger stays ABOVE DEBUG, so no request body
    # (and therefore no Langfuse secret key value) can ever be written to stderr.
    for name in backfill.WIRE_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() > logging.DEBUG, name
    # The root logger must not be at DEBUG either: a DEBUG root would enable wire
    # logging for any third-party logger that is NOT in the pinned list.
    assert logging.getLogger().getEffectiveLevel() > logging.DEBUG


def test_wire_logger_list_covers_the_known_leak_vectors():
    """Regression guard on the list itself: botocore is what dumps the Secrets Manager
    CreateSecret body, httpx/httpcore what dumps the Langfuse tRPC bodies."""
    for name in ("boto3", "botocore", "httpx", "httpcore", "urllib3"):
        assert name in backfill.WIRE_LOGGERS


def test_non_verbose_logging_is_info_everywhere(logging_levels_restored):
    backfill._configure_logging(verbose=False)

    assert backfill.logger.getEffectiveLevel() == logging.INFO
    for name in backfill.WIRE_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() > logging.DEBUG, name


# ---------------------------------------------------------------------------
# 6) Infra config resolution — terraform.tfvars under tmp_path.
#    Never touches the REAL infrastructure dir, never calls STS/Langfuse
#    (boto3 is poisoned or faked in every test that could reach it).
#    Mirrors tests/test_seed_default_tenant.py §6-7.
# ---------------------------------------------------------------------------


def _write_infra(tmp_path, tfvars_text=None, stray_env_text=None):
    """Build a fake infra dir under tmp_path with an optional ``terraform.tfvars``.

    ``stray_env_text`` writes an ``infrastructure/.env``. NOTHING reads that file any
    more (E34/T10 removed the ``.env`` leg); it is written here only to prove it has
    no effect on the resolved config."""
    infra = tmp_path / "infrastructure"
    infra.mkdir()
    if tfvars_text is not None:
        (infra / "terraform.tfvars").write_text(tfvars_text, encoding="utf-8")
    if stray_env_text is not None:
        (infra / ".env").write_text(stray_env_text, encoding="utf-8")
    return infra


def _args(infra_dir, **overrides):
    """A parse_args-shaped namespace with every config flag defaulted to None."""
    base = dict(
        infra_dir=str(infra_dir),
        region=None,
        agent_registry_id=None,
        langfuse_host=None,
        langfuse_admin_secret_name=None,
        project_name=None,
        agent_id=None,
        dry_run=False,
        verbose=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _PoisonedBoto3:
    """Any attempt to build an AWS client fails the test — proves STS is never hit."""

    def client(self, *args, **kwargs):  # pragma: no cover — must not run
        raise AssertionError("boto3/STS must not be called by this test")

    def __getattr__(self, name):  # pragma: no cover — must not run
        raise AssertionError("boto3 must not be touched by this test")


def _fake_boto3(account_id):
    """A boto3 stand-in whose STS returns a fixed, fake account id."""

    class _FakeSts:
        def get_caller_identity(self):
            return {"Account": account_id}

    class _FakeBoto3:
        def client(self, service, *args, **kwargs):
            assert service == "sts"
            return _FakeSts()

    return _FakeBoto3()


@pytest.fixture
def no_langfuse_env(monkeypatch):
    """Clear the ambient env vars the resolver falls back to, so a test asserting a
    file/CLI/derivation outcome can never be tainted by the developer's own shell."""
    for name in ("LANGFUSE_HOST", "LANGFUSE_ADMIN_SECRET_NAME", "AWS_REGION"):
        monkeypatch.delenv(name, raising=False)


# Every key the resolver reads, in ``terraform.tfvars`` syntax — the only config file
# this script reads. ``project_name``/``environment``/``aws_region`` are declared root
# variables; the rest are read from this file by the script alone (Terraform has no
# variable for them), which is why the CLI flags and the ambient env vars below are the
# normal way to supply them.
_FULL_TFVARS = (
    'aws_region   = "us-east-1"\n'
    'project_name = "agp"\n'
    'environment  = "dev"\n'
    'agent_registry_id = "RegAgents"\n'
    'langfuse_host     = "https://lf.tfvars.internal"\n'
    'langfuse_admin_secret_name = "agp-cp-dev-223333-langfuse-secrets"\n'
)

# The same values as a phantom ``infrastructure/.env`` — nothing reads this shape any
# more, so writing it must never change an outcome.
_STRAY_ENV = (
    "AWS_REGION=eu-west-1\n"
    "PROJECT_NAME=stray\n"
    "ENVIRONMENT=prod\n"
    "AGENT_REGISTRY_ID=RegFromStrayEnv\n"
    "LANGFUSE_HOST=https://lf.stray.internal\n"
    "LANGFUSE_ADMIN_SECRET_NAME=stray-langfuse-secrets\n"
)


# --- file parsing -----------------------------------------------------------


def test_parse_tfvars_scalars_skip_blocks_and_inline_comments(tmp_path):
    infra = _write_infra(
        tmp_path,
        tfvars_text=(
            "# AWS Configuration\n"
            "// c-style comment\n"
            'aws_region   = "us-east-1"\n'
            'environment  = "dev"\n'
            'project_name = "agp"\n'
            'langfuse_host = "https://lf.tfvars.internal"\n'
            'hosted_zone_id = "" # Leave empty if not using a custom domain\n'
            "langfuse_enabled = true\n"
            "ecs_task_cpu = 512 # inline comment after a number\n"
            'availability_zones = ["us-east-1a", "us-east-1b"]\n'
            "tags = {\n"
            '  Project   = "agp"\n'
            '  ManagedBy = "terraform"\n'
            "}\n"
            'agent_registry_id = "RegBBBB"\n'
        ),
    )
    cfg = backfill._load_infra_config(infra)

    # Keys are normalized to UPPER_SNAKE.
    assert cfg["AWS_REGION"] == "us-east-1"
    assert cfg["ENVIRONMENT"] == "dev"
    assert cfg["PROJECT_NAME"] == "agp"
    assert cfg["LANGFUSE_HOST"] == "https://lf.tfvars.internal"
    assert cfg["AGENT_REGISTRY_ID"] == "RegBBBB"
    assert cfg["LANGFUSE_ENABLED"] == "true"  # bare bool scalar
    assert cfg["ECS_TASK_CPU"] == "512"  # inline comment after a number is dropped
    # Empty string, list and the multi-line tags block are skipped gracefully —
    # critically, the keys INSIDE the block (Project/ManagedBy) never leak out.
    assert "HOSTED_ZONE_ID" not in cfg
    assert "AVAILABILITY_ZONES" not in cfg
    assert "TAGS" not in cfg
    assert "PROJECT" not in cfg
    assert "MANAGEDBY" not in cfg


def test_missing_infra_files_yield_empty_config(tmp_path):
    infra = tmp_path / "infrastructure"
    infra.mkdir()
    assert backfill._load_infra_config(infra) == {}


# --- the .env leg is GONE (E34/T10) ----------------------------------------


def test_a_stray_env_file_is_ignored_entirely(tmp_path):
    """``infrastructure/.env`` is a phantom config surface: no adopter should create
    one, nothing in the repo writes one, and this script no longer reads one. A stray
    file on disk — even one that sets every key, with values that conflict with the
    tfvars — contributes NOTHING to the resolved config."""
    infra = _write_infra(
        tmp_path,
        tfvars_text=(
            'aws_region        = "us-east-1"\n'
            'langfuse_host     = "https://lf.tfvars.internal"\n'
            'agent_registry_id = "RegFromTfvars"\n'
            'project_name      = "agp"\n'
            'environment       = "dev"\n'
        ),
        stray_env_text=(
            "AWS_REGION=eu-west-1\n"
            "LANGFUSE_HOST=https://lf.stray.internal\n"
            "AGENT_REGISTRY_ID=RegFromStrayEnv\n"
            "PROJECT_NAME=stray\n"
            "ENVIRONMENT=prod\n"
            "VPC_ID=vpc-from-stray-env\n"
        ),
    )
    cfg = backfill._load_infra_config(infra)

    assert cfg["AWS_REGION"] == "us-east-1"
    assert cfg["LANGFUSE_HOST"] == "https://lf.tfvars.internal"
    assert cfg["AGENT_REGISTRY_ID"] == "RegFromTfvars"
    assert cfg["PROJECT_NAME"] == "agp"
    assert cfg["ENVIRONMENT"] == "dev"
    assert "VPC_ID" not in cfg  # a key ONLY the stray .env carries never appears


def test_a_stray_env_file_alone_yields_an_empty_config(tmp_path):
    """No ``terraform.tfvars`` + a fully-populated stray ``.env`` ⇒ empty config. The
    ``.env`` is not a fallback either; it is simply not a source."""
    infra = _write_infra(tmp_path, stray_env_text=_STRAY_ENV)
    assert backfill._load_infra_config(infra) == {}


def test_the_env_parser_no_longer_exists():
    """Guard against the ``.env`` leg being reintroduced by a future copy-paste: the
    parser itself is gone from both clone scripts (E34/T10)."""
    assert not hasattr(backfill, "_parse_env_file")
    assert not hasattr(backfill, "DERIVATION_KEYS")


# --- _derive_langfuse_secret_name ------------------------------------------


def test_derive_langfuse_secret_name_matches_terraform_rule():
    """Terraform's rule: ``<project>-cp-<env>-<last-6-of-account>-langfuse-secrets``."""
    name = backfill._derive_langfuse_secret_name(
        {"PROJECT_NAME": "agp", "ENVIRONMENT": "dev"}, "111122223333"
    )
    assert name == "agp-cp-dev-223333-langfuse-secrets"

    assert (
        backfill._derive_langfuse_secret_name(
            {"PROJECT_NAME": "agp", "ENVIRONMENT": "prod"}, "999988887777"
        )
        == "agp-cp-prod-887777-langfuse-secrets"
    )


@pytest.mark.parametrize(
    "infra,account_id",
    [
        ({"ENVIRONMENT": "dev"}, "111122223333"),  # no PROJECT_NAME
        ({"PROJECT_NAME": "agp"}, "111122223333"),  # no ENVIRONMENT
        ({"PROJECT_NAME": "agp", "ENVIRONMENT": "dev"}, None),  # STS unreachable
    ],
)
def test_derive_langfuse_secret_name_is_a_soft_miss(infra, account_id):
    """Unresolvable inputs yield "" (soft miss — the admin secret is only needed by the
    provisioner's list-and-match fallback), never a half-built name."""
    assert backfill._derive_langfuse_secret_name(infra, account_id) == ""


# --- _resolve_runtime_config ----------------------------------------------


def test_resolution_cli_beats_the_tfvars(tmp_path, monkeypatch, no_langfuse_env):
    monkeypatch.setitem(sys.modules, "boto3", _PoisonedBoto3())
    infra = _write_infra(tmp_path, tfvars_text=_FULL_TFVARS, stray_env_text=_STRAY_ENV)
    cfg = backfill._resolve_runtime_config(
        _args(
            infra,
            region="eu-central-1",
            agent_registry_id="CliAgents",
            langfuse_host="https://lf.cli.internal",
            langfuse_admin_secret_name="cli-langfuse-secrets",
            project_name="cli-agp",
        )
    )
    assert cfg == {
        "region": "eu-central-1",
        "agent_registry_id": "CliAgents",
        "langfuse_host": "https://lf.cli.internal",
        "langfuse_admin_secret_name": "cli-langfuse-secrets",
        "agp_project_name": "cli-agp",
    }


def test_resolution_reads_the_tfvars_without_touching_sts(
    tmp_path, monkeypatch, no_langfuse_env
):
    """Everything resolves from ``terraform.tfvars`` — STS is never consulted (boto3 is
    poisoned to prove it) because the admin secret name is already in the file."""
    monkeypatch.setitem(sys.modules, "boto3", _PoisonedBoto3())
    infra = _write_infra(tmp_path, tfvars_text=_FULL_TFVARS)

    cfg = backfill._resolve_runtime_config(_args(infra))

    assert cfg["region"] == "us-east-1"
    assert cfg["agent_registry_id"] == "RegAgents"
    assert cfg["langfuse_host"] == "https://lf.tfvars.internal"
    assert cfg["langfuse_admin_secret_name"] == "agp-cp-dev-223333-langfuse-secrets"
    assert cfg["agp_project_name"] == backfill.DEFAULT_AGP_PROJECT_NAME


def test_resolution_ignores_a_stray_env_file(tmp_path, monkeypatch, no_langfuse_env):
    """End-to-end version of the ignore contract: a stray ``infrastructure/.env`` that
    sets every key cannot move a single resolved value away from the tfvars'."""
    monkeypatch.setitem(sys.modules, "boto3", _PoisonedBoto3())
    infra = _write_infra(tmp_path, tfvars_text=_FULL_TFVARS, stray_env_text=_STRAY_ENV)

    cfg = backfill._resolve_runtime_config(_args(infra))

    assert cfg["region"] == "us-east-1"
    assert cfg["agent_registry_id"] == "RegAgents"
    assert cfg["langfuse_host"] == "https://lf.tfvars.internal"
    assert cfg["langfuse_admin_secret_name"] == "agp-cp-dev-223333-langfuse-secrets"


def test_langfuse_host_cli_override_beats_the_tfvars(
    tmp_path, monkeypatch, no_langfuse_env
):
    """--langfuse-host wins over ``terraform.tfvars``."""
    monkeypatch.setitem(sys.modules, "boto3", _PoisonedBoto3())
    infra = _write_infra(tmp_path, tfvars_text=_FULL_TFVARS)
    cfg = backfill._resolve_runtime_config(
        _args(infra, langfuse_host="https://lf.cli.internal")
    )
    assert cfg["langfuse_host"] == "https://lf.cli.internal"


def test_langfuse_host_falls_back_to_tfvars_when_no_flag_is_passed(
    tmp_path, monkeypatch
):
    """No ``--langfuse-host`` ⇒ tfvars' ``langfuse_host`` is used."""
    monkeypatch.setitem(sys.modules, "boto3", _PoisonedBoto3())
    monkeypatch.setenv("LANGFUSE_HOST", "https://lf.envvar.internal")
    infra = _write_infra(
        tmp_path,
        tfvars_text=(
            'agent_registry_id = "RegTf"\n'
            'langfuse_host     = "https://lf.tfvars.internal"\n'
            'langfuse_admin_secret_name = "tf-langfuse-secrets"\n'
        ),
    )
    cfg = backfill._resolve_runtime_config(_args(infra))
    # tfvars outranks the ambient env var — the env var is the LAST leg.
    assert cfg["langfuse_host"] == "https://lf.tfvars.internal"


def test_langfuse_host_falls_back_to_the_env_var_last(tmp_path, monkeypatch):
    """The tfvars does not carry the host ⇒ the ``LANGFUSE_HOST`` env var (what the ECS
    task itself is given, and what `terraform output langfuse_host` feeds) is the last
    source before the hard error."""
    monkeypatch.setitem(sys.modules, "boto3", _PoisonedBoto3())
    monkeypatch.setenv("LANGFUSE_HOST", "https://lf.envvar.internal")
    infra = _write_infra(
        tmp_path,
        tfvars_text=(
            'agent_registry_id = "RegTf"\n'
            'langfuse_admin_secret_name = "tf-langfuse-secrets"\n'
        ),
    )
    cfg = backfill._resolve_runtime_config(_args(infra))
    assert cfg["langfuse_host"] == "https://lf.envvar.internal"


def test_region_defaults_to_repo_convention(tmp_path, monkeypatch, no_langfuse_env):
    monkeypatch.setitem(sys.modules, "boto3", _PoisonedBoto3())
    infra = _write_infra(
        tmp_path,
        tfvars_text=(
            'agent_registry_id = "RegAgents"\n'
            'langfuse_host     = "https://lf.tfvars.internal"\n'
            'langfuse_admin_secret_name = "s"\n'
        ),
    )
    cfg = backfill._resolve_runtime_config(_args(infra))
    assert cfg["region"] == backfill.DEFAULT_REGION == "us-east-1"


def test_admin_secret_name_is_derived_from_the_sts_account(
    tmp_path, monkeypatch, no_langfuse_env
):
    """Nothing supplies the admin secret name ⇒ it is DERIVED from tfvars'
    project_name/environment plus the (faked) STS account's last 6 digits. A stray
    ``.env`` claiming a different project/environment cannot skew the derivation —
    tfvars is what Terraform actually named the deployed resources from."""
    monkeypatch.setitem(sys.modules, "boto3", _fake_boto3("444455556666"))
    infra = _write_infra(
        tmp_path,
        tfvars_text=(
            'agent_registry_id = "RegAgents"\n'
            'langfuse_host     = "https://lf.tfvars.internal"\n'
            'project_name      = "agp"\n'
            'environment       = "dev"\n'
        ),
        stray_env_text="PROJECT_NAME=stray\nENVIRONMENT=prod\n",
    )
    cfg = backfill._resolve_runtime_config(_args(infra))
    assert cfg["langfuse_admin_secret_name"] == "agp-cp-dev-556666-langfuse-secrets"


def test_unresolvable_admin_secret_name_warns_but_does_not_fail(
    tmp_path, monkeypatch, no_langfuse_env, caplog
):
    """The admin secret is a SOFT requirement: unresolvable ⇒ warn + empty, because the
    provisioner only needs it for its list-and-match fallback."""
    monkeypatch.setitem(sys.modules, "boto3", _PoisonedBoto3())
    infra = _write_infra(
        tmp_path,
        tfvars_text=(
            'agent_registry_id = "RegAgents"\n'
            'langfuse_host     = "https://lf.tfvars.internal"\n'
        ),
    )
    with caplog.at_level(logging.WARNING, logger="backfill_langfuse_projects"):
        cfg = backfill._resolve_runtime_config(_args(infra))

    assert cfg["langfuse_admin_secret_name"] == ""
    # The run can still go on.
    assert cfg["langfuse_host"] == "https://lf.tfvars.internal"
    assert "--langfuse-admin-secret-name" in caplog.text


# --- fail-fast when LANGFUSE_HOST is unresolvable --------------------------


def test_missing_langfuse_host_raises_with_every_source_named(
    tmp_path, monkeypatch, no_langfuse_env
):
    monkeypatch.setitem(sys.modules, "boto3", _PoisonedBoto3())
    infra = _write_infra(tmp_path, tfvars_text='agent_registry_id = "RegAgents"\n')

    with pytest.raises(RuntimeError) as exc:
        backfill._resolve_runtime_config(_args(infra))

    msg = str(exc.value)
    assert "LANGFUSE_HOST" in msg
    assert "--langfuse-host" in msg
    assert "terraform.tfvars" in msg
    assert "terraform output langfuse_host" in msg  # how to get it
    assert "Refusing to half-run" in msg
    # No source that does not exist is named — the phantom .env is gone.
    assert ".env" not in msg


def test_missing_agent_registry_id_raises_with_every_source_named(
    tmp_path, monkeypatch, no_langfuse_env
):
    monkeypatch.setitem(sys.modules, "boto3", _PoisonedBoto3())
    infra = _write_infra(
        tmp_path, tfvars_text='langfuse_host = "https://lf.tfvars.internal"\n'
    )

    with pytest.raises(RuntimeError) as exc:
        backfill._resolve_runtime_config(_args(infra))

    msg = str(exc.value)
    assert "--agent-registry-id" in msg
    assert "terraform.tfvars" in msg
    assert ".env" not in msg


def test_main_without_langfuse_host_exits_nonzero_and_provisions_nothing(
    tmp_path, monkeypatch, no_langfuse_env, logging_levels_restored, caplog
):
    """The end-to-end fail-fast contract: no LANGFUSE_HOST anywhere ⇒ main() returns a
    non-zero exit code and NOTHING is provisioned — no services are even built, so
    neither Langfuse nor Secrets Manager is touched."""
    monkeypatch.setitem(sys.modules, "boto3", _PoisonedBoto3())
    built = []

    def _poisoned_build_services(config):
        built.append(config)
        raise AssertionError("services must not be built without LANGFUSE_HOST")

    def _poisoned_run(**kwargs):
        raise AssertionError("run() must not be reached without LANGFUSE_HOST")

    monkeypatch.setattr(backfill, "_build_services", _poisoned_build_services)
    monkeypatch.setattr(backfill, "run", _poisoned_run)
    infra = _write_infra(
        tmp_path,
        tfvars_text='aws_region = "us-east-1"\nagent_registry_id = "RegAgents"\n',
        # Even with a phantom .env supplying LANGFUSE_HOST, the run must fail fast.
        stray_env_text="LANGFUSE_HOST=https://lf.stray.internal\n",
    )

    with caplog.at_level(logging.ERROR, logger="backfill_langfuse_projects"):
        exit_code = backfill.main(["--infra-dir", str(infra), "--dry-run"])

    assert exit_code == 1
    assert built == []  # nothing constructed, nothing provisioned
    assert "LANGFUSE_HOST" in caplog.text
    assert "Refusing to half-run" in caplog.text
