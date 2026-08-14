"""Offline tests for ``scripts/migrate_to_e25b.py`` (E25B migration orchestrator).

Pure-logic tests via injected in-memory fakes (NO moto, NO boto3, NO live GitHub/AWS):
``run()`` takes the six services as parameters, so no AWS/GitHub client is ever
constructed here. Every test injects an explicit ``account_id``/``region`` so STS is
NEVER called (there is no hardcoded account anywhere — the real run derives it from the
caller's credentials). Mirrors ``test_seed_default_tenant.py``'s fakes/idioms and the
``sys.path`` shim that makes ``scripts.<mod>`` importable.

Coverage (task brief):
  1. ``--dry-run`` makes ZERO mutating calls (no repo overwrite, no tenant/agent writes).
  2. Step 2: the per-org overwrite loop calls ``rollout(template_names=[], overwrite=True,
     overwrite_infra=True)`` for each connection and counts an org as done ONLY when the
     infra item's action is ``overwritten``/``created`` (E28D — ``overwrite_infra`` is what
     forces the infra repo, and a ``skipped`` must not be reported as a success). The fake's
     signature is pinned to the real service's so the same drift cannot hide again.
  3. Step 3: deployed runtimes (non-empty ``agent_arn``) are detected + reported;
     no build is triggered; runtimes without an ARN are ignored.
  4. Idempotency: a second real run performs the same (delete+recreate) overwrite and
     the tenant step writes nothing new.
  5. Config resolution: the E25B-only values (connections table, secret prefix,
     runtime-module bucket/key) resolve the seed way (CLI > infra files > derivation).
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

# Backend dir on sys.path makes `scripts` importable as a namespace package (same shim
# as test_seed_default_tenant.py). Both scripts are import-safe (stdlib-only at top,
# everything else lazily imported).
_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import scripts.migrate_to_e25b as mig  # noqa: E402 — after sys.path shim
import scripts.seed_default_tenant as seed  # noqa: E402

GROUP_ID = "11111111-2222-3333-4444-555555555555"
# Injected account/region — arbitrary test values, NOT any real environment's.
ACCOUNT_ID = "111122223333"
REGION = "eu-central-1"


# ---------------------------------------------------------------------------
# In-memory fakes (no AWS, no GitHub) — seed fakes reused for the tenant step.
# ---------------------------------------------------------------------------


class FakeTenantService:
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
    """agent/MCP registry surface: list() + persist_identity()."""

    def __init__(self, records=None) -> None:
        self.records = list(records or [])
        self.persist_calls = []

    def list(self):
        return list(self.records)

    def persist_identity(self, record):
        self.persist_calls.append(record)
        return record


class FakeProjectService:
    def __init__(self, projects=None) -> None:
        self.projects = list(projects or [])
        self.save_calls = []

    def list_projects(self):
        return list(self.projects)

    def _save_project(self, record):
        self.save_calls.append(record)


class FakeConnectionService:
    """Mimics ConnectionService.list_connections()."""

    def __init__(self, connections=None) -> None:
        self.connections = list(connections or [])

    def list_connections(self):
        return list(self.connections)


class _RolloutResult:
    def __init__(self, items):
        self.items = items


class FakeRolloutService:
    """Records every rollout() call; returns a RolloutResult carrying the forced infra
    item so Step 2 can read its action. Never touches GitHub/S3.

    Its ``rollout`` signature is PINNED to the real ``RolloutService.rollout`` by
    ``test_the_fake_rollout_signature_matches_the_real_service`` below — see that test for
    why a bare hand-rolled fake (or a ``MagicMock(spec=…)``) is not enough here.
    """

    def __init__(self, *, action="overwritten", explode_on=()):
        self.calls = []
        self._action = action
        self._explode_on = set(explode_on)

    def rollout(self, connection_id, *, template_names, overwrite, overwrite_infra=False):
        self.calls.append(
            SimpleNamespace(
                connection_id=connection_id,
                template_names=list(template_names),
                overwrite=overwrite,
                overwrite_infra=overwrite_infra,
            )
        )
        if connection_id in self._explode_on:
            raise RuntimeError("boom")
        return _RolloutResult(
            [SimpleNamespace(name=mig.INFRA_REPO_NAME, action=self._action, reason=None)]
        )


def _agent(rid, *, name=None, agent_arn=None, tenant_id="default"):
    # tenant_id defaults to "default" so the chained seed step does NOT stamp it —
    # keeping the Step-3 runtime-report tests focused on report-only behavior.
    return SimpleNamespace(
        id=rid, name=name or f"name-{rid}", agent_arn=agent_arn, tenant_id=tenant_id
    )


def _conn(cid, org):
    return SimpleNamespace(id=cid, org=org)


def _record(rid, tenant_id=None):
    return SimpleNamespace(id=rid, name=f"name-{rid}", tenant_id=tenant_id)


def _services(*, agents=(), conns=(), rollout=None, projects=(), tenants=()):
    return {
        "tenant_service": FakeTenantService(list(tenants)),
        "agent_service": FakeRegistry(list(agents)),
        "mcp_server_service": FakeRegistry(),
        "project_service": FakeProjectService(list(projects)),
        "connection_service": FakeConnectionService(list(conns)),
        "rollout_service": rollout if rollout is not None else FakeRolloutService(),
    }


def _run(svcs, *, dry_run=False, account_id=ACCOUNT_ID, region=REGION):
    return mig.run(
        group_id=GROUP_ID,
        dry_run=dry_run,
        account_id=account_id,
        region=region,
        **svcs,
    )


# ---------------------------------------------------------------------------
# 1. --dry-run — zero mutating calls
# ---------------------------------------------------------------------------


def test_dry_run_makes_no_mutating_calls(capsys):
    rollout = FakeRolloutService()
    svcs = _services(
        agents=[_agent("a1", agent_arn="arn:aws:...:runtime/a1"), _record("a2")],
        conns=[_conn("c1", "org-one"), _conn("c2", "org-two")],
        rollout=rollout,
        projects=[_record("p1")],
    )

    rc = _run(svcs, dry_run=True)

    assert rc == 0
    # Step 1 (tenant/seed): nothing written.
    assert svcs["tenant_service"].upsert_calls == 0
    assert "default" not in svcs["tenant_service"].store
    assert svcs["project_service"].save_calls == []
    # Agent-registry stamp path stayed silent (a2 has tenant_id=None, so the seed's
    # would-stamp path is exercised; dry-run must persist nothing).
    assert svcs["agent_service"].persist_calls == []
    # Step 2: rollout NEVER called in dry-run (it deletes+recreates a real repo).
    assert rollout.calls == []
    out = capsys.readouterr().out
    assert "org-one -> would-overwrite" in out
    assert "org-two -> would-overwrite" in out
    # Step 3 still reports (read-only in both modes).
    assert "MUST be RE-PUSHED" in out
    # Operator ordering is unmissable.
    assert "terraform apply FIRST" in out


# ---------------------------------------------------------------------------
# 2. Step 2 — per-org overwrite loop
# ---------------------------------------------------------------------------


def test_real_run_overwrites_infra_repo_per_org(capsys):
    rollout = FakeRolloutService(action="overwritten")
    svcs = _services(conns=[_conn("c1", "org-one"), _conn("c2", "org-two")], rollout=rollout)

    rc = _run(svcs)

    assert rc == 0
    # One rollout per connection, each forcing ONLY the infra repo.
    assert [c.connection_id for c in rollout.calls] == ["c1", "c2"]
    assert all(c.template_names == [] for c in rollout.calls)
    assert all(c.overwrite is True for c in rollout.calls)
    # E28D — ``overwrite_infra`` is the flag that actually forces the infra repo; ``overwrite``
    # governs TEMPLATE repos only and no longer reaches it. Without this the script's CORE job
    # is a silent no-op (every existing agp-runtime-infra comes back "skipped").
    assert all(c.overwrite_infra is True for c in rollout.calls)
    out = capsys.readouterr().out
    assert "org-one -> overwritten" in out
    assert "org-two -> overwritten" in out
    assert "overwrote 2/2 org repo(s) (0 failure(s))" in out


def test_the_fake_rollout_signature_matches_the_real_service():
    """The fake above is signature-PINNED to ``RolloutService.rollout``.

    This is the E28D/T7 ``spec=`` lesson generalized — but one step further, because ``spec=``
    would not have caught THIS bug: ``MagicMock(spec=RolloutService)`` checks attribute NAMES
    only, so a fake that accepted the old ``(template_names, overwrite)`` shape while the real
    service grew ``overwrite_infra`` still passes a ``spec=`` mock. Only comparing the
    signatures makes a drift red. Parameter names, kinds and defaults are compared; annotations
    are not (the fake writes none, and a missing annotation is not a divergence in behavior).

    If this goes red: the real service's signature moved — update the FAKE and the caller in
    ``scripts/migrate_to_e25b.py``, never this assertion.
    """
    import inspect

    from services.template_rollout_service import RolloutService

    want = list(inspect.signature(RolloutService.rollout).parameters.values())
    got = list(inspect.signature(FakeRolloutService.rollout).parameters.values())
    assert [p.name for p in got] == [p.name for p in want]
    assert [p.kind for p in got] == [p.kind for p in want]
    assert [p.default for p in got] == [p.default for p in want]


def test_a_skipped_infra_item_is_NOT_counted_as_a_success(capsys):
    """The accounting is derived from the infra item's ACTION, not from "the call did not raise".

    ``skipped`` is exactly what a rollout that forgot ``overwrite_infra=True`` returns for an
    existing repo — the failure mode this script must never report as done. It exits nonzero and
    says so, instead of printing "overwrote 1/1" over an org whose module is still pre-E25B.
    """
    rollout = FakeRolloutService(action="skipped")
    svcs = _services(conns=[_conn("c1", "org-one")], rollout=rollout)

    rc = _run(svcs)

    assert rc == 1
    out = capsys.readouterr().out
    assert "org-one -> skipped" in out
    assert "overwrote 0/1 org repo(s) (1 failure(s))" in out


def test_a_missing_infra_item_is_NOT_counted_as_a_success(capsys):
    """A result with no ``agp-runtime-infra`` item at all is also not a success."""

    class _NoInfraItemRollout(FakeRolloutService):
        def rollout(self, connection_id, *, template_names, overwrite, overwrite_infra=False):
            super().rollout(
                connection_id,
                template_names=template_names,
                overwrite=overwrite,
                overwrite_infra=overwrite_infra,
            )
            return _RolloutResult([])

    svcs = _services(conns=[_conn("c1", "org-one")], rollout=_NoInfraItemRollout())

    rc = _run(svcs)

    assert rc == 1
    assert "org-one -> unknown" in capsys.readouterr().out


def test_a_created_infra_item_counts_as_done(capsys):
    """``created`` (the repo was absent) is as valid an outcome as ``overwritten``."""
    svcs = _services(
        conns=[_conn("c1", "org-one")], rollout=FakeRolloutService(action="created")
    )
    rc = _run(svcs)
    assert rc == 0
    out = capsys.readouterr().out
    assert "org-one -> created" in out
    assert "overwrote 1/1 org repo(s) (0 failure(s))" in out


def test_step2_no_connections_is_noop(capsys):
    svcs = _services(conns=[])
    rc = _run(svcs)
    assert rc == 0
    assert svcs["rollout_service"].calls == []
    assert "overwrote 0/0 org repo(s)" in capsys.readouterr().out


def test_step2_one_org_failure_continues_and_returns_nonzero(capsys):
    rollout = FakeRolloutService(explode_on={"c1"})
    svcs = _services(conns=[_conn("c1", "bad-org"), _conn("c2", "good-org")], rollout=rollout)

    rc = _run(svcs)

    assert rc == 1  # failure surfaced in the exit code
    # Both orgs were attempted (log-and-continue).
    assert [c.connection_id for c in rollout.calls] == ["c1", "c2"]
    out = capsys.readouterr().out
    assert "bad-org -> failed" in out
    assert "good-org -> overwritten" in out
    assert "(1 failure(s))" in out


# ---------------------------------------------------------------------------
# 3. Step 3 — detect + report deployed runtimes (report-only)
# ---------------------------------------------------------------------------


def test_deployed_runtimes_reported_no_build_triggered(capsys):
    svcs = _services(
        agents=[
            _agent("a1", name="orders", agent_arn="arn:aws:...:runtime/a1"),
            _agent("a2", name="undeployed", agent_arn=None),
            _agent("a3", name="fnol", agent_arn="arn:aws:...:runtime/a3"),
        ],
    )

    rc = _run(svcs)

    assert rc == 0
    out = capsys.readouterr().out
    # Only the two with a non-empty agent_arn are reported.
    assert "2 deployed runtime(s)" in out
    assert "agent_id='a1'" in out and "arn:aws:...:runtime/a1" in out
    assert "agent_id='a3'" in out
    assert "'a2'" not in out  # undeployed agent is not reported
    assert "MUST be RE-PUSHED" in out
    # No agent write path was invoked by the runtime report (persist_identity only from
    # the chained seed stamp — these agents already have no missing tenant_id anyway).
    assert svcs["agent_service"].persist_calls == []


def test_no_deployed_runtimes_reports_nothing(capsys):
    svcs = _services(agents=[_agent("a1", agent_arn=None)])
    rc = _run(svcs)
    assert rc == 0
    assert "no deployed runtimes found" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 4. Idempotency — a second real run re-overwrites (same result), no new writes
# ---------------------------------------------------------------------------


def test_second_run_is_idempotent(capsys):
    rollout = FakeRolloutService()
    svcs = _services(conns=[_conn("c1", "org-one")], rollout=rollout)

    assert _run(svcs) == 0
    assert _run(svcs) == 0
    # Tenant created exactly once; the overwrite ran on both passes (delete+recreate is
    # idempotent — same end state), which is the intended repeatable behavior.
    assert svcs["tenant_service"].upsert_calls == 1
    assert [c.connection_id for c in rollout.calls] == ["c1", "c1"]


def test_run_chains_seed_tenant_step():
    """Step 1 actually seeds the default tenant via the reused seed.run()."""
    svcs = _services()
    rc = _run(svcs)
    assert rc == 0
    assert svcs["tenant_service"].upsert_calls == 1
    tenant = svcs["tenant_service"].store["default"]
    assert tenant.id == "default"
    assert tenant.entra_group_ids == [GROUP_ID]


# ---------------------------------------------------------------------------
# 5. Config resolution — E25B-only values (seed precedence rules reused)
# ---------------------------------------------------------------------------

# ``terraform.tfvars`` is the only config file the seed's resolver reads — the ``.env``
# leg was removed in E34/T10, so a stray ``infrastructure/.env`` is ignored entirely.
_FULL_TFVARS = (
    'aws_region   = "us-east-1"\n'
    'project_name = "agp"\n'
    'environment  = "dev"\n'
    'agent_registry_id = "RegAgents"\n'
)


def _write_infra(tmp_path, tfvars_text=None, stray_env_text=None):
    infra = tmp_path / "infrastructure"
    infra.mkdir()
    if tfvars_text is not None:
        (infra / "terraform.tfvars").write_text(tfvars_text, encoding="utf-8")
    if stray_env_text is not None:  # written only to prove it has no effect
        (infra / ".env").write_text(stray_env_text, encoding="utf-8")
    return infra


def _fake_sts(monkeypatch, account: str) -> None:
    """Make the caller's live STS identity ``account`` for one test. Needed because the
    seed's account resolver — which this migration reuses verbatim — takes the account from
    the caller's credentials, never from the infra folder, and every derived name here is
    built from it. No credentials, no network (conftest's guard fails any real STS call)."""

    class _Boto3:
        def client(self, service):
            assert service == "sts"
            return SimpleNamespace(get_caller_identity=lambda: {"Account": account})

    monkeypatch.setitem(sys.modules, "boto3", _Boto3())


def _args(infra_dir, **overrides):
    base = dict(
        infra_dir=str(infra_dir),
        region=None,
        account_id=None,
        agent_registry_id=None,
        mcp_registry_id=None,
        tenants_table=None,
        projects_table=None,
        connections_table=None,
        connections_secret_prefix=None,
        runtime_module_bucket=None,
        runtime_module_key=None,
        agent_templates_dir=None,
        dry_run=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_e25b_config_derives_connections_and_state_bucket(tmp_path, monkeypatch):
    """No CLI overrides ⇒ connections table + state bucket derive from Terraform's rule
    (last-6 of the CALLER's account, which the seed's resolver takes from STS)."""
    _fake_sts(monkeypatch, "111122223333")
    infra = _write_infra(
        tmp_path,
        tfvars_text=_FULL_TFVARS,
        # A phantom infrastructure/.env cannot move a single resolved value (E34/T10).
        stray_env_text="AWS_REGION=eu-west-1\nRUNTIME_MODULE_BUCKET=stray-bucket\n",
    )

    cfg = mig._resolve_e25b_config(_args(infra))

    # Shared (seed) values still present.
    assert cfg["region"] == "us-east-1"
    assert cfg["account_id"] == "111122223333"
    assert cfg["tenants_table"] == "agp-cp-dev-223333-tenants"
    # E25B-only derived values (Terraform naming rule + backend/default fallbacks).
    assert cfg["connections_table"] == "agp-cp-dev-223333-connections"
    assert cfg["connections_secret_prefix"] == mig.DEFAULT_CONNECTIONS_SECRET_PREFIX
    assert cfg["runtime_module_bucket"] == "agp-cp-dev-223333-tf-state"
    assert cfg["runtime_module_key"] == mig.DEFAULT_RUNTIME_MODULE_KEY


def test_e25b_config_tfvars_values_win_over_derivation(tmp_path, monkeypatch):
    """runtime_module_bucket / _key / connections_secret_prefix in terraform.tfvars are
    used verbatim (they beat the derived defaults)."""
    _fake_sts(monkeypatch, "111122223333")
    infra = _write_infra(
        tmp_path,
        tfvars_text=_FULL_TFVARS
        + 'runtime_module_bucket = "my-state-bucket"\n'
        + 'runtime_module_key    = "custom/key.zip"\n'
        + 'connections_secret_prefix = "agp-prod/git/"\n',
    )

    cfg = mig._resolve_e25b_config(_args(infra))
    assert cfg["runtime_module_bucket"] == "my-state-bucket"
    assert cfg["runtime_module_key"] == "custom/key.zip"
    assert cfg["connections_secret_prefix"] == "agp-prod/git/"


def test_e25b_config_cli_beats_the_tfvars(tmp_path, monkeypatch):
    _fake_sts(monkeypatch, "111122223333")
    infra = _write_infra(
        tmp_path, tfvars_text=_FULL_TFVARS + 'runtime_module_bucket = "file-bucket"\n'
    )

    cfg = mig._resolve_e25b_config(
        _args(
            infra,
            connections_table="cli-connections",
            runtime_module_bucket="cli-bucket",
            runtime_module_key="cli/key.zip",
            connections_secret_prefix="cli/prefix/",
        )
    )
    assert cfg["connections_table"] == "cli-connections"
    assert cfg["runtime_module_bucket"] == "cli-bucket"
    assert cfg["runtime_module_key"] == "cli/key.zip"
    assert cfg["connections_secret_prefix"] == "cli/prefix/"


# ---------------------------------------------------------------------------
# 6. main() guardrails
# ---------------------------------------------------------------------------


def test_main_requires_group_id(monkeypatch):
    """No --group-id and no DEFAULT_TENANT_GROUP_ID env ⇒ exit 2 BEFORE any service
    construction (the lazy-import contract)."""
    monkeypatch.delenv(seed.GROUP_ID_ENV_VAR, raising=False)
    assert mig.main(["--dry-run"]) == 2
