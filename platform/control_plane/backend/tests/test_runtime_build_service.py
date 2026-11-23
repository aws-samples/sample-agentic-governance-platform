"""Unit tests for RuntimeBuildService.

A fake codebuild client records ``start_build`` kwargs; a fake connection service returns a
Connection and a ready bearer token (PAT: the stored token; App: a freshly minted installation
token); a fake secretsmanager client records the scratch-token secret writes and deletes.

The build must never pass the connection's own secret ARN: for a GitHub App connection that secret
is ``{"private_key": ...}`` and the buildspec's ``jq -r '.token'`` clone would silently fail. The
service resolves a ready token through ``get_bearer_token``, writes ``{"token": <token>}`` to a
short-lived scratch secret and passes that scratch ARN as ``GIT_SECRET_ARN``. Asserted here: the env
contract, the scratch-secret body, and that the raw token never appears in the StartBuild payload.
"""

import json
from types import SimpleNamespace
from typing import Any

import pytest

from models.connection import Connection, ConnectionAuthType, ConnStatus, Provider
from models.tenant import TenantStageConfig
from services.runtime_build_service import RuntimeBuildError, RuntimeBuildService
from services.tenant_service import TenantError

_SCRATCH_PREFIX = "agp-test/runtime-build-token/"


class FakeCodeBuild:
    def __init__(self, *, build_id="build-123:abc", raise_exc=None, in_flight_env=None):
        self.build_id = build_id
        self.raise_exc = raise_exc
        self.calls = []
        self._in_flight_env = in_flight_env

    def list_builds_for_project(self, **kwargs):
        return {"ids": ["build-inflight:xyz"]} if self._in_flight_env is not None else {"ids": []}

    def batch_get_builds(self, **kwargs):
        if self._in_flight_env is None:
            return {"builds": []}
        return {
            "builds": [
                {
                    "buildStatus": "IN_PROGRESS",
                    "environment": {
                        "environmentVariables": [
                            {"name": k, "value": v} for k, v in self._in_flight_env.items()
                        ]
                    },
                }
            ]
        }

    def start_build(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc:
            raise self.raise_exc
        return {"build": {"id": self.build_id}}


class _ResourceExistsException(Exception):
    pass


class _ResourceNotFoundException(Exception):
    pass


class _FakeSMExceptions:
    ResourceExistsException = _ResourceExistsException
    ResourceNotFoundException = _ResourceNotFoundException


class FakeSecretsManager:
    """Records create_secret / put_secret_value writes and delete_secret calls; returns a
    deterministic ARN."""

    def __init__(self, *, raise_exc=None, already_exists=False, delete_raise_exc=None):
        self.raise_exc = raise_exc
        self.already_exists = already_exists
        self.delete_raise_exc = delete_raise_exc
        self.writes = []
        self.deletes = []
        self.exceptions = _FakeSMExceptions()

    def _arn(self, name):
        return f"arn:aws:secretsmanager:us-east-1:111:secret:{name}-AbCdEf"

    def create_secret(self, *, Name, SecretString, Tags=None):
        if self.raise_exc:
            raise self.raise_exc
        if self.already_exists:
            raise _ResourceExistsException(Name)
        self.writes.append((Name, SecretString))
        return {"ARN": self._arn(Name)}

    def put_secret_value(self, *, SecretId, SecretString):
        self.writes.append((SecretId, SecretString))
        return {"ARN": self._arn(SecretId)}

    def delete_secret(self, **kwargs):
        self.deletes.append(kwargs)
        if self.delete_raise_exc:
            raise self.delete_raise_exc
        return {"ARN": kwargs["SecretId"]}


class FakeConnectionService:
    def __init__(self, connection, *, bearer_token="tok-ready"):  # noqa: S107 — a fake connection's placeholder bearer, not a credential; expires 2027-10-23; added E49A-50A/T14
        self._conn = connection
        self._bearer = bearer_token
        self.bearer_calls = []

    def get_connection(self, id):
        return self._conn

    def get_bearer_token(self, connection_id):
        self.bearer_calls.append(connection_id)
        return self._bearer


class FakeAgentRegistry:
    """Returns a configured agent (SimpleNamespace with `.tenant_id`); `agent=None`
    simulates an unknown agent.

    `registry_id` is part of the interface `RuntimeBuildService` consumes, not decoration: the
    backend resolves the registry by NAME and passes AGENT_REGISTRY_ID to CodeBuild as a
    per-build override, so the id the build uses comes from THIS service rather than from a
    Terraform-baked project env var."""

    def __init__(self, agent, registry_id="REGFROMSERVICE"):
        self.agent = agent
        self.registry_id = registry_id
        self.calls = []

    def get(self, agent_id):
        self.calls.append(agent_id)
        return self.agent


class FakeTenantService:
    """Returns a configured tenant (SimpleNamespace with `.stages`); `raise_exc`
    simulates an unknown tenant (`TenantError`) from `get`."""

    def __init__(self, tenant, *, raise_exc=None):
        self.tenant = tenant
        self.raise_exc = raise_exc
        self.calls = []

    def get(self, tenant_id):
        self.calls.append(tenant_id)
        if self.raise_exc:
            raise self.raise_exc
        return self.tenant


def _agent(tenant_id: str | None = "ten-x"):
    return SimpleNamespace(tenant_id=tenant_id)


def _tenant(stages=None):
    if stages is None:
        stages = {
            "dev": TenantStageConfig(
                account_id="111111111111",
                region="us-east-1",
                ecr_repo_uri="",
                deploy_role_arn="agp-deployment-dev",
            ),
            "prod": TenantStageConfig(
                account_id="222222222222",
                region="eu-central-1",
                ecr_repo_uri="ecr-prod-uri",
                deploy_role_arn="agp-deployment-prod",
            ),
        }
    return SimpleNamespace(stages=stages)


def _conn(**over):
    base: dict[str, Any] = dict(
        id="conn-1",
        provider=Provider.GITHUB,
        org="acme-org",
        base_url=None,
        auth_type=ConnectionAuthType.PAT,
        status=ConnStatus.CONNECTED,
        secret_arn="arn:aws:secretsmanager:us-east-1:111:secret:agp/git/conn-1",
        has_secret=True,
        created_by="a@b.com",
        created_at="t",
        updated_at="t",
    )
    base.update(over)
    return Connection(**base)


def _svc(cb, conn_svc, sm, *, tenant_service=None, agent_registry=None):
    return RuntimeBuildService(
        cb,
        conn_svc,
        sm,
        tenant_service=tenant_service or FakeTenantService(_tenant()),
        agent_registry=agent_registry or FakeAgentRegistry(_agent()),
        codebuild_project_name="p",
        scratch_secret_prefix=_SCRATCH_PREFIX,
    )


def _env_map(kwargs):
    return {e["name"]: e["value"] for e in kwargs["environmentVariablesOverride"]}


def test_start_build_env_contract_and_build_id():
    cb = FakeCodeBuild(build_id="rt-build:xyz")
    sm = FakeSecretsManager()
    conn_svc = FakeConnectionService(_conn(base_url="https://ghe.acme.internal"))
    svc = RuntimeBuildService(
        cb,
        conn_svc,
        sm,
        tenant_service=FakeTenantService(_tenant()),
        agent_registry=FakeAgentRegistry(_agent()),
        codebuild_project_name="agp-runtime-provision",
        scratch_secret_prefix=_SCRATCH_PREFIX,
    )

    build_id = svc.start_runtime_build(
        agent_id="agent-42",
        image_tag="agent-42-abc123",
        ecr_repo="123.dkr.ecr.us-east-1.amazonaws.com/agp-agents",
        connection_id="conn-1",
        stage="dev",
    )

    assert build_id == "rt-build:xyz"
    assert len(cb.calls) == 1
    assert cb.calls[0]["projectName"] == "agp-runtime-provision"
    env = _env_map(cb.calls[0])
    assert env["IAC_TYPE"] == "agentcore_runtime"
    assert env["DEPLOYMENT_ID"] == "agentcore-runtime-agent-42-abc123"
    assert env["IMAGE_TAG"] == "agent-42-abc123"
    assert env["ECR_REPO"] == "123.dkr.ecr.us-east-1.amazonaws.com/agp-agents"
    assert env["GIT_INFRA_ORG"] == "acme-org"
    assert env["GIT_INFRA_REPO"] == "agp-runtime-infra"
    assert env["GIT_BASE_URL"] == "https://ghe.acme.internal"
    scratch_name = f"{_SCRATCH_PREFIX}agent-42-abc123"
    assert env["GIT_SECRET_ARN"] == sm._arn(scratch_name)
    assert env["GIT_SECRET_ARN"] != _conn().secret_arn
    assert "ARCHIVE_BUCKET" not in env and "ARCHIVE_KEY" not in env
    assert env["IMAGE_DIGEST"] == ""


def test_the_registry_id_is_supplied_by_the_BACKEND_not_by_terraform():
    """AGENT_REGISTRY_ID must reach the build as a per-build override, from the registry service.

    THE PROBLEM THIS CLOSES. The buildspec's `agentcore_runtime` branch calls
    `agent-registry-control get-registry-record` / `update-registry-record`, both of which need
    an ID — AWS mints registry ids and `RegistryIdentifier` never accepts a name. Terraform used
    to bake that id into the CodeBuild project, which is what forced a from-zero deploy to
    `terraform apply` TWICE: there is no Terraform resource for the `agent-registry` namespace,
    so the id round-tripped through a capture file read during the PLAN walk, before the
    provisioner that writes it had run.

    The backend resolves the registry by NAME and is the build's ONLY trigger, so it can supply
    the id per build — and the override takes
    precedence over any project-level value, meaning the build always reads the registry the
    control plane is actually using. A stale or empty id here does NOT fail loudly: the record
    read 404s, or worse the write-back lands on the wrong registry, and the buildspec treats a
    failed write-back as a LIVE-but-untracked runtime the delete cascade can never reclaim."""
    cb = FakeCodeBuild()
    svc = _svc(
        cb,
        FakeConnectionService(_conn()),
        FakeSecretsManager(),
        agent_registry=FakeAgentRegistry(_agent(), registry_id="REGRESOLVED01"),
    )
    svc.start_runtime_build(
        agent_id="agent-42",
        image_tag="agent-42-abc123",
        ecr_repo="123.dkr.ecr.us-east-1.amazonaws.com/agp-agents",
        connection_id="conn-1",
        stage="dev",
    )
    entry = next(e for e in cb.calls[0]["environmentVariablesOverride"] if e["name"] == "AGENT_REGISTRY_ID")
    assert entry["value"] == "REGRESOLVED01"
    assert entry["type"] == "PLAINTEXT"


def test_the_image_digest_reaches_the_build_environment():
    """A digest passed in MUST arrive in the CodeBuild env, byte for byte.

    Asserted on the exact value, not merely presence: a truncated or re-derived digest would deploy
    bytes nobody approved while looking correct."""
    digest = "sha256:" + "ab" * 32
    cb = FakeCodeBuild(build_id="rt-build:xyz")
    svc = _svc(cb, FakeConnectionService(_conn()), FakeSecretsManager())
    svc.start_runtime_build(
        agent_id="agent-42",
        image_tag="agent-42-abc123",
        ecr_repo="123.dkr.ecr.us-east-1.amazonaws.com/agp-agents",
        connection_id="conn-1",
        stage="dev",
        image_digest=digest,
    )
    env = _env_map(cb.calls[0])
    assert env["IMAGE_DIGEST"] == digest, (
        "the approved digest must reach the buildspec — without it the deploy falls back to a "
        "MUTABLE tag, which is the exact hole D-B3 exists to close"
    )
    entry = next(e for e in cb.calls[0]["environmentVariablesOverride"] if e["name"] == "IMAGE_DIGEST")
    assert entry["type"] == "PLAINTEXT"
    assert env["IMAGE_TAG"] == "agent-42-abc123"


def test_an_absent_digest_is_sent_as_an_explicit_empty_value():
    """The legacy path must send the key EMPTY rather than omit it.

    Omitting it would make the buildspec's emptiness branch depend on an undefined shell variable —
    the two are indistinguishable to `sh`, so the key's PRESENCE is what makes the legacy case an
    explicit state instead of a missing one."""
    cb = FakeCodeBuild(build_id="rt-build:xyz")
    svc = _svc(cb, FakeConnectionService(_conn()), FakeSecretsManager())
    svc.start_runtime_build(
        agent_id="agent-42",
        image_tag="agent-42-abc123",
        ecr_repo="123.dkr.ecr.us-east-1.amazonaws.com/agp-agents",
        connection_id="conn-1",
        stage="dev",
    )
    names = [e["name"] for e in cb.calls[0]["environmentVariablesOverride"]]
    assert "IMAGE_DIGEST" in names, "the key must be PRESENT even when there is no digest"
    assert _env_map(cb.calls[0])["IMAGE_DIGEST"] == ""
    assert names.count("IMAGE_DIGEST") == 1


def test_app_connection_writes_scratch_token_and_uses_scratch_arn():
    """App connection: get_bearer_token mints a token; a scratch `{"token": ...}` secret is
    written and StartBuild's GIT_SECRET_ARN is that scratch ARN (never the connection's
    private-key secret_arn). The raw token appears in NO env override value."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    minted = "ghs_minted_installation_token"
    conn_svc = FakeConnectionService(
        _conn(auth_type=ConnectionAuthType.GITHUB_APP, app_id="123", installation_id="456"),
        bearer_token=minted,
    )
    svc = _svc(cb, conn_svc, sm)

    svc.start_runtime_build(
        agent_id="agent-9",
        image_tag="agent-9-deadbee",
        ecr_repo="r",
        connection_id="conn-1",
        stage="dev",
    )

    assert conn_svc.bearer_calls == ["conn-1"]
    assert len(sm.writes) == 1
    name, body = sm.writes[0]
    assert name == f"{_SCRATCH_PREFIX}agent-9-deadbee"
    assert json.loads(body) == {"token": minted}
    env = _env_map(cb.calls[0])
    assert env["GIT_SECRET_ARN"] == sm._arn(name)
    assert env["GIT_SECRET_ARN"] != _conn().secret_arn
    assert all(minted not in e["value"] for e in cb.calls[0]["environmentVariablesOverride"])


def test_pat_connection_uses_same_scratch_path():
    """PAT connection: identical path — get_bearer_token returns the stored PAT, a scratch
    `{"token": ...}` secret is written, GIT_SECRET_ARN is the scratch ARN. Token not in env."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    pat = "ghp_stored_pat"
    conn_svc = FakeConnectionService(_conn(), bearer_token=pat)
    svc = _svc(cb, conn_svc, sm)

    svc.start_runtime_build(
        agent_id="a",
        image_tag="a-sha",
        ecr_repo="r",
        connection_id="conn-1",
        stage="dev",
    )

    assert len(sm.writes) == 1
    name, body = sm.writes[0]
    assert json.loads(body) == {"token": pat}
    env = _env_map(cb.calls[0])
    assert env["GIT_SECRET_ARN"] == sm._arn(name)
    assert all(pat not in e["value"] for e in cb.calls[0]["environmentVariablesOverride"])


def test_scratch_secret_already_exists_falls_back_to_put_secret_value():
    """Re-running a build for an existing image tag hits a pre-existing scratch secret:
    create_secret raises ResourceExistsException (the real botocore name) and the service
    MUST fall back to put_secret_value rather than letting the except clause blow up. Guards
    the live bug where the except caught an exception name that botocore does not expose."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager(already_exists=True)
    pat = "ghp_stored_pat"
    conn_svc = FakeConnectionService(_conn(), bearer_token=pat)
    svc = _svc(cb, conn_svc, sm)

    build_id = svc.start_runtime_build(
        agent_id="a",
        image_tag="a-sha",
        ecr_repo="r",
        connection_id="conn-1",
        stage="dev",
    )

    assert build_id == cb.build_id
    assert len(sm.writes) == 1
    name, body = sm.writes[0]
    assert name == f"{_SCRATCH_PREFIX}a-sha"
    assert json.loads(body) == {"token": pat}
    assert _env_map(cb.calls[0])["GIT_SECRET_ARN"] == sm._arn(name)


def test_secret_arn_passed_not_token():
    """The scratch Secrets Manager ARN is passed; no raw token/credential is ever in the payload."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    svc = _svc(cb, FakeConnectionService(_conn(), bearer_token="ghp_x"), sm)
    svc.start_runtime_build(
        agent_id="a",
        image_tag="t",
        ecr_repo="r",
        connection_id="conn-1",
        stage="dev",
    )
    env = _env_map(cb.calls[0])
    assert env["GIT_SECRET_ARN"].startswith("arn:aws:secretsmanager")
    assert not any(k in env for k in ("TOKEN", "GIT_TOKEN", "PRIVATE_KEY", "SECRET"))


def test_empty_base_url_becomes_empty_string():
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    svc = _svc(cb, FakeConnectionService(_conn(base_url=None)), sm)
    svc.start_runtime_build(agent_id="a", image_tag="t", ecr_repo="r", connection_id="conn-1", stage="dev")
    assert _env_map(cb.calls[0])["GIT_BASE_URL"] == ""


def test_start_build_failure_raises_runtime_build_error():
    cb = FakeCodeBuild(raise_exc=RuntimeError("boom"))
    sm = FakeSecretsManager()
    svc = _svc(cb, FakeConnectionService(_conn()), sm)
    with pytest.raises(RuntimeBuildError):
        svc.start_runtime_build(
            agent_id="a", image_tag="t", ecr_repo="r", connection_id="conn-1", stage="dev"
        )


def test_scratch_secret_write_failure_raises_runtime_build_error():
    """A Secrets Manager write failure surfaces as RuntimeBuildError (SAFE) — StartBuild is
    never reached and the token never leaks."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager(raise_exc=RuntimeError("sm down"))
    svc = _svc(cb, FakeConnectionService(_conn(), bearer_token="ghp_secret"), sm)
    with pytest.raises(RuntimeBuildError):
        svc.start_runtime_build(
            agent_id="a", image_tag="t", ecr_repo="r", connection_id="conn-1", stage="dev"
        )
    assert cb.calls == []


def test_a_failed_start_build_deletes_the_scratch_secret_it_just_wrote():
    """The leak: `_write_scratch_token` runs first, so a StartBuild fault must force-delete
    the scratch secret before re-raising. `ForceDeleteWithoutRecovery=True` is required —
    the default 30-day recovery window would keep the token readable and billing."""
    cb = FakeCodeBuild(raise_exc=RuntimeError("boom"))
    sm = FakeSecretsManager()
    svc = _svc(cb, FakeConnectionService(_conn(), bearer_token="ghp_leaky"), sm)

    with pytest.raises(RuntimeBuildError):
        svc.start_runtime_build(
            agent_id="a",
            image_tag="a-sha",
            ecr_repo="r",
            connection_id="conn-1",
            stage="dev",
        )

    name, _body = sm.writes[0]
    assert sm.deletes == [{"SecretId": sm._arn(name), "ForceDeleteWithoutRecovery": True}], (
        "the just-written scratch secret must be force-deleted on the StartBuild failure path"
    )


def test_a_SUCCESSFUL_start_build_leaves_the_scratch_secret_alone():
    """The control plane must NOT delete on the happy path: CodeBuild reads the secret later,
    under its own role, and deleting here would break every clone. The buildspec's post_build
    purge owns that side (pinned in tests/test_buildspec_contract.py)."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    svc = _svc(cb, FakeConnectionService(_conn()), sm)

    svc.start_runtime_build(
        agent_id="a",
        image_tag="a-sha",
        ecr_repo="r",
        connection_id="conn-1",
        stage="dev",
    )

    assert sm.deletes == []


@pytest.mark.parametrize(
    "fault",
    [RuntimeError("sm down"), _ResourceNotFoundException("gone")],
    ids=["generic_fault", "already_gone"],
)
def test_a_failing_scratch_delete_never_masks_the_start_build_failure(fault):
    """Cleanup is BEST-EFFORT (mirrors `connection_service._delete_secret_best_effort`): both
    fault families are swallowed, so the caller still sees the SAFE RuntimeBuildError for the
    real cause (the StartBuild failure), never a delete_secret exception."""
    cb = FakeCodeBuild(raise_exc=RuntimeError("boom"))
    sm = FakeSecretsManager(delete_raise_exc=fault)
    svc = _svc(cb, FakeConnectionService(_conn()), sm)

    with pytest.raises(RuntimeBuildError) as ei:
        svc.start_runtime_build(
            agent_id="a",
            image_tag="a-sha",
            ecr_repo="r",
            connection_id="conn-1",
            stage="dev",
        )

    assert str(ei.value) == "Failed to start runtime build"
    assert len(sm.deletes) == 1


def test_start_runtime_build_passes_target_role_for_stage():
    """The stage's tenant config drives TARGET_ROLE_ARN/TARGET_ACCOUNT_ID/STAGE/TENANT_ID —
    the tenant is derived from the agent, never the request body."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    svc = _svc(
        cb,
        FakeConnectionService(_conn()),
        sm,
        tenant_service=FakeTenantService(_tenant()),
        agent_registry=FakeAgentRegistry(_agent()),
    )
    svc.start_runtime_build(
        agent_id="ag-1",
        image_tag="ag-1-abc",
        ecr_repo="req-ecr",
        connection_id="conn-1",
        stage="prod",
    )
    env = _env_map(cb.calls[0])
    assert env["TARGET_ROLE_ARN"] == "agp-deployment-prod"
    assert env["TARGET_ACCOUNT_ID"] == "222222222222"
    assert env["AWS_TARGET_REGION"] == "eu-central-1"
    assert env["STAGE"] == "prod"
    assert env["TENANT_ID"] == "ten-x"
    assert "EXEC_ROLE_ARN" not in env


def test_start_runtime_build_prefers_tenant_ecr_over_request():
    """When the tenant stage carries an ecr_repo_uri it is authoritative over the request's."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    svc = _svc(
        cb,
        FakeConnectionService(_conn()),
        sm,
        tenant_service=FakeTenantService(_tenant()),
        agent_registry=FakeAgentRegistry(_agent()),
    )
    svc.start_runtime_build(
        agent_id="ag-1",
        image_tag="t",
        ecr_repo="req-ecr",
        connection_id="conn-1",
        stage="prod",
    )
    assert _env_map(cb.calls[0])["ECR_REPO"] == "ecr-prod-uri"


def test_start_runtime_build_empty_deploy_role_is_deploy_in_place():
    """An empty deploy_role_arn ⇒ TARGET_ROLE_ARN=="" (buildspec skips assume; deploy-in-place)
    and the request's ecr_repo is used when the tenant stage's is unset."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    stages = {
        "dev": TenantStageConfig(account_id="333333333333", region="us-east-1"),
    }
    svc = _svc(
        cb,
        FakeConnectionService(_conn()),
        sm,
        tenant_service=FakeTenantService(_tenant(stages)),
        agent_registry=FakeAgentRegistry(_agent()),
    )
    svc.start_runtime_build(
        agent_id="ag-1",
        image_tag="t",
        ecr_repo="req-ecr",
        connection_id="conn-1",
        stage="dev",
    )
    env = _env_map(cb.calls[0])
    assert env["TARGET_ROLE_ARN"] == ""
    assert "EXEC_ROLE_ARN" not in env
    assert env["ECR_REPO"] == "req-ecr"


def test_start_runtime_build_unknown_agent_raises():
    """An unknown agent (registry returns None) raises RuntimeBuildError before StartBuild."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    svc = _svc(
        cb,
        FakeConnectionService(_conn()),
        sm,
        agent_registry=FakeAgentRegistry(None),
    )
    with pytest.raises(RuntimeBuildError):
        svc.start_runtime_build(
            agent_id="nope",
            image_tag="t",
            ecr_repo="e",
            connection_id="conn-1",
            stage="dev",
        )
    assert cb.calls == []


def test_start_runtime_build_unknown_tenant_raises_build_error():
    """An agent whose tenant_id resolves to no tenant (tenants.get raises TenantError) must
    surface as RuntimeBuildError (not_found), NOT an unhandled TenantError escaping to a 500.
    StartBuild is never reached."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    svc = _svc(
        cb,
        FakeConnectionService(_conn()),
        sm,
        tenant_service=FakeTenantService(None, raise_exc=TenantError("Unknown tenant", kind="not_found")),
        agent_registry=FakeAgentRegistry(_agent()),
    )
    with pytest.raises(RuntimeBuildError) as ei:
        svc.start_runtime_build(
            agent_id="ag-1",
            image_tag="t",
            ecr_repo="e",
            connection_id="conn-1",
            stage="dev",
        )
    assert ei.value.kind == "not_found"
    assert cb.calls == []


def test_start_runtime_build_none_tenant_id_raises_build_error():
    """A legacy/un-stamped agent (tenant_id is None) must surface as RuntimeBuildError
    (not_found), not an unhandled error. StartBuild is never reached."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    svc = _svc(
        cb,
        FakeConnectionService(_conn()),
        sm,
        tenant_service=FakeTenantService(None, raise_exc=TenantError("Unknown tenant", kind="not_found")),
        agent_registry=FakeAgentRegistry(_agent(tenant_id=None)),
    )
    with pytest.raises(RuntimeBuildError) as ei:
        svc.start_runtime_build(
            agent_id="ag-1",
            image_tag="t",
            ecr_repo="e",
            connection_id="conn-1",
            stage="dev",
        )
    assert ei.value.kind == "not_found"
    assert cb.calls == []


def test_start_runtime_build_missing_stage_raises_build_error():
    """A tenant missing the requested stage (KeyError on the subscript) must surface as
    RuntimeBuildError (not_found), not an unhandled 500. StartBuild is never reached."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    stages = {"dev": TenantStageConfig(account_id="333333333333", region="us-east-1")}
    svc = _svc(
        cb,
        FakeConnectionService(_conn()),
        sm,
        tenant_service=FakeTenantService(_tenant(stages)),
        agent_registry=FakeAgentRegistry(_agent()),
    )
    with pytest.raises(RuntimeBuildError) as ei:
        svc.start_runtime_build(
            agent_id="ag-1",
            image_tag="t",
            ecr_repo="e",
            connection_id="conn-1",
            stage="prod",
        )
    assert ei.value.kind == "not_found"
    assert cb.calls == []


def test_start_build_refuses_a_databricks_tenant():
    """A Databricks tenant is refused BEFORE the AWS-only stage reads (`ecr_repo_uri`,
    `deploy_role_arn`, `account_id`, `region`) — none of which exist on a `DatabricksStageConfig`.

    Without the gate this method would raise `AttributeError` out of a pydantic model and surface
    as a 500. There is no AgentCore runtime to build for a Databricks agent, so the honest answer is
    an explicit unsupported-operation error, and no StartBuild (or scratch-secret write) may happen.
    """
    from models.tenant import DatabricksStageConfig, TenantPlatform

    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    conn_svc = FakeConnectionService(_conn())
    tenant = SimpleNamespace(
        platform=TenantPlatform.DATABRICKS,
        stages={
            "dev": DatabricksStageConfig(
                workspace_url="https://dbc-test.cloud.databricks.com",
                sp_client_id="sp-1",
            )
        },
    )
    svc = _svc(cb, conn_svc, sm, tenant_service=FakeTenantService(tenant))

    with pytest.raises(RuntimeBuildError) as exc:
        svc.start_runtime_build(
            agent_id="agent-42",
            image_tag="agent-42-abc123",
            ecr_repo="repo",
            connection_id="conn-1",
            stage="dev",
        )

    assert exc.value.kind == "unsupported"
    assert cb.calls == []
    assert sm.writes == []


def test_start_build_allows_an_explicitly_aws_tenant():
    """The gate reads `platform` when it is present and defaults to AWS when it is not, so an
    explicitly-AWS tenant (and every legacy record, which carries no `platform` at all) builds
    exactly as before — the fence above proves the absent case."""
    from models.tenant import TenantPlatform

    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    conn_svc = FakeConnectionService(_conn())
    tenant = SimpleNamespace(platform=TenantPlatform.AWS, stages=_tenant().stages)
    svc = _svc(cb, conn_svc, sm, tenant_service=FakeTenantService(tenant))

    build_id = svc.start_runtime_build(
        agent_id="agent-42",
        image_tag="agent-42-abc123",
        ecr_repo="repo",
        connection_id="conn-1",
        stage="dev",
    )

    assert build_id == cb.build_id
    assert len(cb.calls) == 1


class FakeProjectService:
    """Resolves agent_id → Repository-shaped object (`.name`, `.template_name`). `repo=None`
    simulates an agent with no owning repo (a pre-project agent)."""

    def __init__(self, repo):
        self.repo = repo
        self.calls = []

    def find_repository_by_agent_id(self, agent_id):
        self.calls.append(agent_id)
        return self.repo


class FakeTemplateRegistry:
    """Resolves (connection_id, template_id) → a record carrying `.infra_template_id`."""

    def __init__(self, record):
        self.record = record
        self.calls = []

    def get(self, connection_id, template_id):
        self.calls.append((connection_id, template_id))
        return self.record


class FakeInfraTemplateRegistry:
    """Resolves (connection_id, infra_template_id) → an InfraTemplateRecord-shaped object."""

    def __init__(self, record, *, raise_exc=None):
        self.record = record
        self.raise_exc = raise_exc
        self.calls = []

    def get(self, connection_id, infra_template_id):
        self.calls.append((connection_id, infra_template_id))
        if self.raise_exc:
            raise self.raise_exc
        return self.record


def _repo(name="acme-harness-agent", template_name="harness-agentcore"):
    return SimpleNamespace(name=name, template_name=template_name, agent_id="ag-1")


def _infra(
    *,
    id="agentcore-harness",
    iac_type="agentcore_harness",
    build_kind="config",
    repo_name="agp-harness-infra",
):
    return SimpleNamespace(
        id=id,
        iac_type=iac_type,
        build_kind=build_kind,
        repo_name=repo_name,
    )


def _chain_svc(cb, sm, *, infra, conn=None, repo=None, template_infra_id=None):
    """A service wired with the full chain: project → template → infra template."""
    if template_infra_id is None:
        template_infra_id = infra.id if infra is not None else "agentcore-runtime"
    return RuntimeBuildService(
        cb,
        FakeConnectionService(conn or _conn()),
        sm,
        tenant_service=FakeTenantService(_tenant()),
        agent_registry=FakeAgentRegistry(_agent()),
        codebuild_project_name="p",
        scratch_secret_prefix=_SCRATCH_PREFIX,
        project_service=FakeProjectService(repo if repo is not None else _repo()),
        template_registry=FakeTemplateRegistry(SimpleNamespace(infra_template_id=template_infra_id)),
        infra_template_registry=FakeInfraTemplateRegistry(infra),
    )


_SHA = "a" * 40


def test_config_chain_agent_gets_the_harness_env_contract():
    """A `build_kind="config"` chain produces the harness env — and OMITS every image key.

    The pinned contract: `IAC_TYPE=agentcore_harness`, `GIT_AGENT_REPO` (the harness module
    reads `harness.yaml` out of the AGENT repo, so the build must clone a SECOND repo — the
    container path clones only the infra repo), `DEPLOYMENT_ID` keyed on the harness iac_type,
    and NO `ECR_REPO`/`ECR_REPOSITORY`/`IMAGE_DIGEST`. The omission is the substantive half:
    there is no image on this path, and an empty `ECR_REPO` would send the buildspec's
    `ECR_REPO_ARN` derivation down a branch whose inputs do not exist.
    """
    cb = FakeCodeBuild(build_id="cfg-build:1")
    sm = FakeSecretsManager()
    svc = _chain_svc(cb, sm, infra=_infra())

    build_id = svc.start_runtime_build(
        agent_id="ag-1",
        image_tag=_SHA,
        ecr_repo="",
        connection_id="conn-1",
        stage="dev",
    )

    assert build_id == "cfg-build:1"
    env = _env_map(cb.calls[0])
    assert env["IAC_TYPE"] == "agentcore_harness"
    assert env["DEPLOYMENT_ID"] == f"agentcore-harness-{_SHA}"
    assert env["IMAGE_TAG"] == _SHA
    assert env["GIT_AGENT_REPO"] == "acme-harness-agent"
    assert env["GIT_INFRA_REPO"] == "agp-harness-infra"
    assert env["GIT_INFRA_ORG"] == "acme-org"
    assert env["AGENT_ID"] == "ag-1"
    assert env["STAGE"] == "dev"
    assert env["TENANT_ID"] == "ten-x"
    for absent in ("ECR_REPO", "ECR_REPOSITORY", "IMAGE_DIGEST"):
        assert absent not in env, f"{absent} must not be sent on the config path"


def test_config_chain_omits_the_cross_account_image_copy_block():
    """A prod config build sends NO `SOURCE_ECR_*` overrides even across accounts.

    The copy block exists to move image BYTES between two accounts' registries. A harness has
    no bytes to move, so `SOURCE_ECR_REQUIRED=true` would make the buildspec fail LOUD on a
    missing `SOURCE_ECR_REPO` — turning every cross-account harness promote into a hard error.
    """
    cb = FakeCodeBuild()
    svc = _chain_svc(cb, FakeSecretsManager(), infra=_infra())

    svc.start_runtime_build(
        agent_id="ag-1",
        image_tag=_SHA,
        ecr_repo="",
        connection_id="conn-1",
        stage="prod",
    )

    env = _env_map(cb.calls[0])
    for absent in ("SOURCE_ECR_REQUIRED", "SOURCE_ECR_REPO", "SOURCE_DEPLOY_ROLE_ARN"):
        assert absent not in env


def test_container_chain_env_is_byte_identical_to_the_pre_e30_contract():
    """REGRESSION PIN. A `build_kind="container"` chain must send EXACTLY the container env set.

    The whole risk of this task is that generalising one path perturbs the other. This pins the
    NAME SET, not just a few values: an added key (e.g. a `GIT_AGENT_REPO` leaking onto the
    container path) fails here, which a per-key assertion would not catch.

    `AGENT_REGISTRY_ID` is in the set on BOTH paths: the buildspec's project-level variable is
    declared empty, so the id arrives as a per-build override or the build cannot read the record it
    writes back to. `AGENT_ID` is sent explicitly on both paths so `_has_in_flight_build` can read it
    off a running build's own environment; the buildspec's `${AGENT_ID:-...}` default makes this a
    no-op for its own derivation.
    """
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    svc = _chain_svc(
        cb,
        sm,
        infra=_infra(
            id="agentcore-runtime",
            iac_type="agentcore_runtime",
            build_kind="container",
            repo_name="agp-runtime-infra",
        ),
        repo=_repo(template_name="strands-agentcore"),
    )

    svc.start_runtime_build(
        agent_id="ag-1",
        image_tag="ag-1-abc1234",
        ecr_repo="repo-uri",
        connection_id="conn-1",
        stage="dev",
    )

    env = _env_map(cb.calls[0])
    assert set(env) == {
        "IAC_TYPE",
        "DEPLOYMENT_ID",
        "IMAGE_TAG",
        "AGENT_ID",
        "IMAGE_DIGEST",
        "ECR_REPO",
        "GIT_INFRA_ORG",
        "GIT_INFRA_REPO",
        "GIT_BASE_URL",
        "GIT_SECRET_ARN",
        "TARGET_ROLE_ARN",
        "TARGET_ACCOUNT_ID",
        "AWS_TARGET_REGION",
        "TENANT_ID",
        "STAGE",
        "AGENT_REGISTRY_ID",
    }
    assert env["IAC_TYPE"] == "agentcore_runtime"
    assert env["DEPLOYMENT_ID"] == "agentcore-runtime-ag-1-abc1234"
    assert env["GIT_INFRA_REPO"] == "agp-runtime-infra"
    assert "GIT_AGENT_REPO" not in env


def test_iac_type_from_the_caller_is_ignored():
    """`iac_type` is a CHAIN fact, never a caller fact.

    `start_runtime_build` accepts no such parameter, so passing one is a `TypeError` rather than
    a silently-honoured override. That is the assertion: the value cannot get in AT ALL, which is
    stronger than "it gets in and is overwritten". Paired with the route-level test
    (test_builds_routes) that a body carrying `iac_type` is dropped by pydantic.
    """
    cb = FakeCodeBuild()
    svc = _chain_svc(
        cb,
        FakeSecretsManager(),
        infra=_infra(
            id="agentcore-runtime",
            iac_type="agentcore_runtime",
            build_kind="container",
            repo_name="agp-runtime-infra",
        ),
    )

    untyped: Any = svc.start_runtime_build
    with pytest.raises(TypeError):
        untyped(
            agent_id="ag-1",
            image_tag="ag-1-abc1234",
            ecr_repo="r",
            connection_id="conn-1",
            stage="dev",
            iac_type="agentcore_harness",
        )
    assert cb.calls == []
    svc.start_runtime_build(
        agent_id="ag-1",
        image_tag="ag-1-abc1234",
        ecr_repo="r",
        connection_id="conn-1",
        stage="dev",
    )
    assert _env_map(cb.calls[0])["IAC_TYPE"] == "agentcore_runtime"


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({}, id="no_chain_services_wired"),
        pytest.param({"project_service": FakeProjectService(None)}, id="agent_has_no_repo"),
    ],
)
def test_an_unresolvable_chain_folds_to_the_container_path(kwargs):
    cb = FakeCodeBuild()
    svc = RuntimeBuildService(
        cb,
        FakeConnectionService(_conn()),
        FakeSecretsManager(),
        tenant_service=FakeTenantService(_tenant()),
        agent_registry=FakeAgentRegistry(_agent()),
        codebuild_project_name="p",
        scratch_secret_prefix=_SCRATCH_PREFIX,
        **kwargs,
    )

    svc.start_runtime_build(
        agent_id="ag-1",
        image_tag="ag-1-abc1234",
        ecr_repo="repo-uri",
        connection_id="conn-1",
        stage="dev",
    )

    env = _env_map(cb.calls[0])
    assert env["IAC_TYPE"] == "agentcore_runtime"
    assert env["GIT_INFRA_REPO"] == "agp-runtime-infra"
    assert env["ECR_REPO"] == "repo-uri"


def test_an_infra_registry_fault_folds_to_the_container_path():
    """A STORE fault is not a build failure. The registry reads STRICT (it raises rather than
    returning None), and this is the one call site where that strictness must not propagate: the
    build it would fail is a container build that needs no catalog row to succeed."""
    from services.infra_template_registry import InfraTemplateRegistryError

    cb = FakeCodeBuild()
    svc = RuntimeBuildService(
        cb,
        FakeConnectionService(_conn()),
        FakeSecretsManager(),
        tenant_service=FakeTenantService(_tenant()),
        agent_registry=FakeAgentRegistry(_agent()),
        codebuild_project_name="p",
        scratch_secret_prefix=_SCRATCH_PREFIX,
        project_service=FakeProjectService(_repo()),
        template_registry=FakeTemplateRegistry(SimpleNamespace(infra_template_id="agentcore-harness")),
        infra_template_registry=FakeInfraTemplateRegistry(
            None, raise_exc=InfraTemplateRegistryError("store down")
        ),
    )

    svc.start_runtime_build(
        agent_id="ag-1",
        image_tag="ag-1-abc1234",
        ecr_repo="repo-uri",
        connection_id="conn-1",
        stage="dev",
    )

    assert _env_map(cb.calls[0])["IAC_TYPE"] == "agentcore_runtime"


@pytest.mark.parametrize(
    "infra",
    [pytest.param(None, id="chain_unresolved"), pytest.param(_infra(build_kind="container"), id="container")],
)
def test_an_expected_config_build_never_folds_to_the_container_path(infra):
    """Build now resolved `config` STRICTLY; this lenient walk must not turn a
    catalog hiccup into a container build of a harness agent. Refused before any secret write."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    svc = _chain_svc(cb, sm, infra=infra, template_infra_id="agentcore-harness")

    with pytest.raises(RuntimeBuildError) as err:
        svc.start_runtime_build(
            agent_id="ag-1",
            image_tag=_SHA,
            ecr_repo="",
            connection_id="conn-1",
            stage="dev",
            expected_build_kind="config",
        )
    assert err.value.kind == "build_kind_mismatch"
    assert cb.calls == []
    assert sm.writes == []


def test_an_expected_config_build_on_a_config_chain_proceeds():
    cb = FakeCodeBuild()
    svc = _chain_svc(cb, FakeSecretsManager(), infra=_infra())
    svc.start_runtime_build(
        agent_id="ag-1",
        image_tag=_SHA,
        ecr_repo="",
        connection_id="conn-1",
        stage="dev",
        expected_build_kind="config",
    )
    assert _env_map(cb.calls[0])["IAC_TYPE"] == "agentcore_harness"


def test_resolve_infra_template_is_public_for_the_route_tag_gate():
    """The route needs the resolved BUILD KIND before it validates the image tag (the harness
    workflow posts a bare sha, which the container prefix rule 403s). It reads the SAME resolver
    the env assembly uses — a second copy of the chain walk in `builds.py` could disagree with
    the service about which mechanism a build is."""
    svc = _chain_svc(FakeCodeBuild(), FakeSecretsManager(), infra=_infra())

    resolved = svc.resolve_infra_template(agent_id="ag-1", connection_id="conn-1")

    assert resolved is not None
    assert resolved.build_kind == "config"
    assert resolved.iac_type == "agentcore_harness"


def test_the_chain_is_walked_ONCE_per_build():
    """`GIT_AGENT_REPO` comes from the SAME walk that resolved the mechanism, not a second read.

    Two independent reads could see different answers — a repo re-pointed in between would clone one
    agent's config under another agent's chain — and the config path needs both halves of the walk,
    so they must be one atomic answer rather than two lookups that agree by luck.
    """
    cb = FakeCodeBuild()
    projects = FakeProjectService(_repo())
    svc = RuntimeBuildService(
        cb,
        FakeConnectionService(_conn()),
        FakeSecretsManager(),
        tenant_service=FakeTenantService(_tenant()),
        agent_registry=FakeAgentRegistry(_agent()),
        codebuild_project_name="p",
        scratch_secret_prefix=_SCRATCH_PREFIX,
        project_service=projects,
        template_registry=FakeTemplateRegistry(SimpleNamespace(infra_template_id="agentcore-harness")),
        infra_template_registry=FakeInfraTemplateRegistry(_infra()),
    )

    svc.start_runtime_build(
        agent_id="ag-1",
        image_tag=_SHA,
        ecr_repo="",
        connection_id="conn-1",
        stage="dev",
    )

    assert projects.calls == ["ag-1"]
    assert _env_map(cb.calls[0])["GIT_AGENT_REPO"] == "acme-harness-agent"


def test_resolve_infra_template_returns_none_when_unresolvable():
    svc = RuntimeBuildService(
        FakeCodeBuild(),
        FakeConnectionService(_conn()),
        FakeSecretsManager(),
        tenant_service=FakeTenantService(_tenant()),
        agent_registry=FakeAgentRegistry(_agent()),
        codebuild_project_name="p",
        scratch_secret_prefix=_SCRATCH_PREFIX,
    )
    assert svc.resolve_infra_template(agent_id="ag-1", connection_id="conn-1") is None


def test_an_in_flight_build_for_the_same_agent_and_stage_is_refused():
    cb = FakeCodeBuild(in_flight_env={"AGENT_ID": "agent-42", "STAGE": "dev"})
    sm = FakeSecretsManager()
    conn_svc = FakeConnectionService(_conn())
    svc = _svc(cb, conn_svc, sm)

    with pytest.raises(RuntimeBuildError) as exc_info:
        svc.start_runtime_build(
            agent_id="agent-42",
            image_tag="agent-42-abc123",
            ecr_repo="repo-uri",
            connection_id="conn-1",
            stage="dev",
        )

    assert exc_info.value.kind == "conflict"
    assert cb.calls == []


def test_an_in_flight_build_for_a_DIFFERENT_agent_or_stage_does_not_block():
    sm = FakeSecretsManager()
    conn_svc = FakeConnectionService(_conn())

    for in_flight in (
        {"AGENT_ID": "some-other-agent", "STAGE": "dev"},
        {"AGENT_ID": "agent-42", "STAGE": "prod"},
    ):
        cb = FakeCodeBuild(build_id="rt-build:ok", in_flight_env=in_flight)
        svc = _svc(cb, conn_svc, sm)
        build_id = svc.start_runtime_build(
            agent_id="agent-42",
            image_tag="agent-42-abc123",
            ecr_repo="repo-uri",
            connection_id="conn-1",
            stage="dev",
        )
        assert build_id == "rt-build:ok"
        assert len(cb.calls) == 1


def test_a_codebuild_fault_while_checking_in_flight_builds_FAILS_OPEN(caplog):
    """Best-effort: a `list_builds_for_project`/`batch_get_builds` fault (throttling, a
    permissions gap, an older fake with neither method) must not block every deploy — it
    logs and lets StartBuild proceed. The fail-open path must never be SILENT — assert the
    warning actually lands, not just that the build was allowed through."""

    class _ExplodingCodeBuild(FakeCodeBuild):
        def list_builds_for_project(self, **kwargs):
            raise RuntimeError("boom")

    cb = _ExplodingCodeBuild(build_id="rt-build:ok")
    sm = FakeSecretsManager()
    conn_svc = FakeConnectionService(_conn())
    svc = _svc(cb, conn_svc, sm)

    import logging

    with caplog.at_level(logging.ERROR):
        build_id = svc.start_runtime_build(
            agent_id="agent-42",
            image_tag="agent-42-abc123",
            ecr_repo="repo-uri",
            connection_id="conn-1",
            stage="dev",
        )
    assert build_id == "rt-build:ok"
    assert any(
        "could not list in-flight builds" in r.message and "failing open" in r.message for r in caplog.records
    )


class FakeMcpProjectService(FakeProjectService):
    """Adds the lookup mcp_server_id → the MCP repo."""

    def __init__(self, repo, mcp_repo=None):
        super().__init__(repo)
        self.mcp_repo = mcp_repo
        self.mcp_calls = []

    def find_repository_by_mcp_server_id(self, mcp_server_id):
        self.mcp_calls.append(mcp_server_id)
        return self.mcp_repo


class FakeMcpRegistry:
    def __init__(self, servers=()):
        self.servers = {s.id: s for s in servers}
        self.list_calls = []

    def get(self, mcp_server_id):
        return self.servers.get(mcp_server_id)

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        kind = kwargs.get("kind")
        return [s for s in self.servers.values() if kind is None or s.kind == kind]


class _CredNotFound(Exception):
    pass


class FakeCredControl:
    class exceptions:
        ResourceNotFoundException = _CredNotFound

    def __init__(self, *, raise_exc=None):
        self.raise_exc = raise_exc
        self.calls = []

    def get_oauth2_credential_provider(self, *, name):
        self.calls.append(name)
        if self.raise_exc:
            raise self.raise_exc
        return {
            "credentialProviderArn": (
                f"arn:aws:bedrock-agentcore:us-east-1:111:token-vault/default/oauth2credentialprovider/{name}"
            )
        }


class FakeCredentialService:
    """`ensure_agent_credential_provider` is ASYNC in the real service; so is this fake."""

    def __init__(self, *, raise_exc=None, control=None):
        self.raise_exc = raise_exc
        self._control = control or FakeCredControl()
        self.ensured = []

    async def ensure_agent_credential_provider(self, agent):
        self.ensured.append(agent)
        if self.raise_exc:
            raise self.raise_exc
        return "agp-agent-obo-ag-1"


def _mcp(
    id="mcp-1",
    *,
    kind="gateway",
    gateway_arn=None,
    audience: str | None = "api://agp-mcp-mcp-1",
    name="claims-api",
    tenant_id="ten-m",
    entra_app_id="app-guid-1",
):
    from models.mcp_server import Kind

    return SimpleNamespace(
        id=id,
        name=name,
        kind=Kind(kind),
        gateway_arn=gateway_arn,
        entra_app_audience=audience,
        entra_app_id=entra_app_id,
        tenant_id=tenant_id,
    )


def _gw_infra():
    return _infra(
        id="agentcore-gateway",
        iac_type="agentcore_gateway",
        build_kind="config",
        repo_name="agp-gateway-infra",
    )


def _mcp_svc(
    cb,
    sm=None,
    *,
    infra=None,
    mcp_repo: SimpleNamespace | str | None = "default",
    mcps=None,
    cred=None,
    agent_repo=None,
    tenant_service=None,
):
    if mcp_repo == "default":
        mcp_repo = SimpleNamespace(
            id="repo-mcp-1",
            name="claims-mcp",
            template_name="agentcore-gateway",
            mcp_server_id="mcp-1",
            agent_id=None,
        )
    return RuntimeBuildService(
        cb,
        FakeConnectionService(_conn()),
        sm or FakeSecretsManager(),
        tenant_service=tenant_service or FakeTenantService(_tenant()),
        agent_registry=FakeAgentRegistry(_agent()),
        codebuild_project_name="p",
        scratch_secret_prefix=_SCRATCH_PREFIX,
        project_service=FakeMcpProjectService(agent_repo or _repo(), mcp_repo),
        template_registry=FakeTemplateRegistry(SimpleNamespace(infra_template_id=(infra or _gw_infra()).id)),
        infra_template_registry=FakeInfraTemplateRegistry(infra or _gw_infra()),
        mcp_registry=FakeMcpRegistry(mcps if mcps is not None else [_mcp()]),
        credential_service=cred,
    )


@pytest.mark.parametrize(
    "ids",
    [{}, {"agent_id": "ag-1", "mcp_server_id": "mcp-1"}],
    ids=["neither", "both"],
)
def test_exactly_one_artifact_id_is_required(ids):
    cb = FakeCodeBuild()
    svc = _mcp_svc(cb)
    with pytest.raises(ValueError):
        svc.start_runtime_build(
            **ids,
            image_tag=_SHA,
            ecr_repo="",
            connection_id="conn-1",
            stage="dev",
        )
    assert cb.calls == []


def test_mcp_build_sends_the_gateway_env_contract_and_no_agent_vars():
    cb = FakeCodeBuild(build_id="gw-build:1")
    sm = FakeSecretsManager()
    svc = _mcp_svc(cb, sm)

    build_id = svc.start_runtime_build(
        mcp_server_id="mcp-1",
        image_tag=_SHA,
        ecr_repo="",
        connection_id="conn-1",
        stage="dev",
    )

    assert build_id == "gw-build:1"
    env = _env_map(cb.calls[0])
    assert set(env) == {
        "IAC_TYPE",
        "DEPLOYMENT_ID",
        "IMAGE_TAG",
        "MCP_SERVER_ID",
        "MCP_NAME",
        "REPO_SK",
        "ENTRA_APP_AUDIENCE",
        "ENTRA_APP_ID",
        "TENANT_ID",
        "STAGE",
        "GIT_AGENT_REPO",
        "GIT_INFRA_ORG",
        "GIT_INFRA_REPO",
        "GIT_BASE_URL",
        "GIT_SECRET_ARN",
        "TARGET_ROLE_ARN",
        "TARGET_ACCOUNT_ID",
        "AWS_TARGET_REGION",
    }
    assert env["IAC_TYPE"] == "agentcore_gateway"
    assert env["DEPLOYMENT_ID"] == f"agentcore-gateway-{_SHA}"
    assert env["IMAGE_TAG"] == _SHA
    assert env["MCP_SERVER_ID"] == "mcp-1"
    assert env["MCP_NAME"] == "claims-api"
    assert env["REPO_SK"] == "repo-mcp-1"
    assert env["ENTRA_APP_AUDIENCE"] == "api://agp-mcp-mcp-1"
    assert env["ENTRA_APP_ID"] == "app-guid-1"
    assert env["TENANT_ID"] == "ten-m"
    assert env["STAGE"] == "dev"
    assert env["GIT_AGENT_REPO"] == "claims-mcp"
    assert env["GIT_INFRA_REPO"] == "agp-gateway-infra"
    assert env["GIT_SECRET_ARN"] == sm._arn(f"{_SCRATCH_PREFIX}{_SHA}")
    for absent in ("AGENT_ID", "AGENT_REGISTRY_ID", "ECR_REPO", "IMAGE_DIGEST"):
        assert absent not in env


def test_mcp_build_for_an_unknown_mcp_is_not_found():
    cb = FakeCodeBuild()
    svc = _mcp_svc(cb, mcps=[])
    with pytest.raises(RuntimeBuildError) as exc_info:
        svc.start_runtime_build(
            mcp_server_id="mcp-1",
            image_tag=_SHA,
            ecr_repo="",
            connection_id="conn-1",
            stage="dev",
        )
    assert exc_info.value.kind == "not_found"
    assert cb.calls == []


@pytest.mark.parametrize("case", ["no_repo", "container_infra"])
def test_mcp_build_never_folds_to_the_container_path(case):
    """An MCP server has no image; an unresolvable or non-config chain is a refusal."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    if case == "no_repo":
        svc = _mcp_svc(cb, sm, mcp_repo=None)
    else:
        svc = _mcp_svc(cb, sm, infra=_infra(build_kind="container"))
    with pytest.raises(RuntimeBuildError) as exc_info:
        svc.start_runtime_build(
            mcp_server_id="mcp-1",
            image_tag=_SHA,
            ecr_repo="",
            connection_id="conn-1",
            stage="dev",
        )
    assert exc_info.value.kind == "build_kind_mismatch"
    assert cb.calls == [] and sm.writes == []


def test_an_in_flight_build_for_the_same_mcp_and_stage_is_refused():
    cb = FakeCodeBuild(in_flight_env={"MCP_SERVER_ID": "mcp-1", "STAGE": "dev"})
    svc = _mcp_svc(cb)
    with pytest.raises(RuntimeBuildError) as exc_info:
        svc.start_runtime_build(
            mcp_server_id="mcp-1",
            image_tag=_SHA,
            ecr_repo="",
            connection_id="conn-1",
            stage="dev",
        )
    assert exc_info.value.kind == "conflict"
    assert cb.calls == []


def test_an_in_flight_mcp_build_does_not_block_another_mcp_or_an_agent():
    for in_flight in (
        {"MCP_SERVER_ID": "mcp-2", "STAGE": "dev"},
        {"MCP_SERVER_ID": "mcp-1", "STAGE": "prod"},
    ):
        cb = FakeCodeBuild(build_id="gw-build:ok", in_flight_env=in_flight)
        assert (
            _mcp_svc(cb).start_runtime_build(
                mcp_server_id="mcp-1",
                image_tag=_SHA,
                ecr_repo="",
                connection_id="conn-1",
                stage="dev",
            )
            == "gw-build:ok"
        )
    cb = FakeCodeBuild(build_id="rt-build:ok", in_flight_env={"MCP_SERVER_ID": "mcp-1", "STAGE": "dev"})
    assert (
        _svc(cb, FakeConnectionService(_conn()), FakeSecretsManager()).start_runtime_build(
            agent_id="agent-42",
            image_tag="agent-42-abc123",
            ecr_repo="r",
            connection_id="conn-1",
            stage="dev",
        )
        == "rt-build:ok"
    )


_GW_A = "arn:aws:bedrock-agentcore:us-east-1:111:gateway/gw-a"
_GW_B = "arn:aws:bedrock-agentcore:us-east-1:111:gateway/gw-b"


def test_a_harness_agent_build_carries_the_gateway_tool_auth_and_obo_provider():
    cb = FakeCodeBuild()
    cred = FakeCredentialService()
    mcps = [
        _mcp("mcp-a", gateway_arn=_GW_A, audience="api://agp-mcp-mcp-a", tenant_id="ten-x"),
        _mcp("mcp-b", gateway_arn=_GW_B, audience="api://agp-mcp-mcp-b", tenant_id="ten-x"),
        _mcp("mcp-noarn", gateway_arn=None, audience="api://agp-mcp-mcp-noarn", tenant_id="ten-x"),
        _mcp("mcp-noaud", gateway_arn="arn:gw-noaud", audience=None, tenant_id="ten-x"),
        _mcp(
            "mcp-rt",
            kind="runtime",
            gateway_arn="arn:gw-rt",
            audience="api://agp-mcp-mcp-rt",
            tenant_id="ten-x",
        ),
        _mcp(
            "mcp-other", gateway_arn="arn:gw-other", audience="api://agp-mcp-mcp-other", tenant_id="ten-other"
        ),
    ]
    svc = _mcp_svc(cb, infra=_infra(), mcps=mcps, cred=cred)

    svc.start_runtime_build(
        agent_id="ag-1",
        image_tag=_SHA,
        ecr_repo="",
        connection_id="conn-1",
        stage="dev",
    )

    env = _env_map(cb.calls[0])
    assert json.loads(env["GATEWAY_TOOL_AUTH"]) == {
        _GW_A: "api://agp-mcp-mcp-a/.default",
        _GW_B: "api://agp-mcp-mcp-b/.default",
    }
    assert " " not in env["GATEWAY_TOOL_AUTH"]
    assert env["OBO_PROVIDER_ARN"].endswith("/oauth2credentialprovider/agp-agent-obo-ag-1")
    assert len(cred.ensured) == 1
    assert cred._control.calls == ["agp-agent-obo-ag-1"]
    assert env["IAC_TYPE"] == "agentcore_harness" and env["AGENT_ID"] == "ag-1"


@pytest.mark.parametrize("where", ["ensure", "arn_lookup"])
def test_a_provider_failure_sends_both_empty_and_the_build_still_starts(where, caplog):
    import logging

    cb = FakeCodeBuild(build_id="cfg-build:ok")
    if where == "ensure":
        cred = FakeCredentialService(raise_exc=RuntimeError("graph down"))
    else:
        cred = FakeCredentialService(control=FakeCredControl(raise_exc=RuntimeError("throttled")))
    svc = _mcp_svc(
        cb,
        infra=_infra(),
        mcps=[_mcp("mcp-a", gateway_arn=_GW_A, tenant_id="ten-x")],
        cred=cred,
    )

    with caplog.at_level(logging.WARNING):
        build_id = svc.start_runtime_build(
            agent_id="ag-1",
            image_tag=_SHA,
            ecr_repo="",
            connection_id="conn-1",
            stage="dev",
        )

    assert build_id == "cfg-build:ok"
    env = _env_map(cb.calls[0])
    assert env["GATEWAY_TOOL_AUTH"] == "" and env["OBO_PROVIDER_ARN"] == ""
    assert any("tool auth" in r.message for r in caplog.records)


def test_a_harness_build_without_the_services_wired_sends_both_empty():
    cb = FakeCodeBuild()
    svc = _chain_svc(cb, FakeSecretsManager(), infra=_infra())
    svc.start_runtime_build(
        agent_id="ag-1",
        image_tag=_SHA,
        ecr_repo="",
        connection_id="conn-1",
        stage="dev",
    )
    env = _env_map(cb.calls[0])
    assert env["GATEWAY_TOOL_AUTH"] == "" and env["OBO_PROVIDER_ARN"] == ""


def test_a_container_agent_build_env_is_unchanged_with_the_mcp_services_wired():
    cb = FakeCodeBuild()
    cred = FakeCredentialService()
    svc = _mcp_svc(
        cb,
        infra=_infra(
            id="agentcore-runtime",
            iac_type="agentcore_runtime",
            build_kind="container",
            repo_name="agp-runtime-infra",
        ),
        mcps=[_mcp("mcp-a", gateway_arn=_GW_A)],
        cred=cred,
    )
    svc.start_runtime_build(
        agent_id="ag-1",
        image_tag="ag-1-abc1234",
        ecr_repo="repo-uri",
        connection_id="conn-1",
        stage="dev",
    )
    env = _env_map(cb.calls[0])
    assert set(env) == {
        "IAC_TYPE",
        "DEPLOYMENT_ID",
        "IMAGE_TAG",
        "AGENT_ID",
        "IMAGE_DIGEST",
        "ECR_REPO",
        "GIT_INFRA_ORG",
        "GIT_INFRA_REPO",
        "GIT_BASE_URL",
        "GIT_SECRET_ARN",
        "TARGET_ROLE_ARN",
        "TARGET_ACCOUNT_ID",
        "AWS_TARGET_REGION",
        "TENANT_ID",
        "STAGE",
        "AGENT_REGISTRY_ID",
    }
    assert cred.ensured == []


def test_no_applicable_gateway_scopes_never_mints_an_obo_provider():
    """Get-or-create mints an Entra secret, so a harness build with nothing to
    exchange for (here: only another tenant's gateway) must not call it."""
    cb = FakeCodeBuild()
    cred = FakeCredentialService()
    svc = _mcp_svc(
        cb,
        infra=_infra(),
        cred=cred,
        mcps=[_mcp("mcp-o", gateway_arn=_GW_A, tenant_id="ten-other")],
    )
    svc.start_runtime_build(
        agent_id="ag-1",
        image_tag=_SHA,
        ecr_repo="",
        connection_id="conn-1",
        stage="dev",
    )
    env = _env_map(cb.calls[0])
    assert env["GATEWAY_TOOL_AUTH"] == "" and env["OBO_PROVIDER_ARN"] == ""
    assert cred.ensured == [] and cred._control.calls == []


def test_a_gateway_arn_claimed_by_two_records_is_dropped(caplog):
    import logging

    cb = FakeCodeBuild()
    svc = _mcp_svc(
        cb,
        infra=_infra(),
        cred=FakeCredentialService(),
        mcps=[
            _mcp("mcp-a", gateway_arn=_GW_A, audience="api://agp-mcp-mcp-a", tenant_id="ten-x"),
            _mcp("mcp-dup", gateway_arn=_GW_A, audience="api://agp-mcp-mcp-dup", tenant_id="ten-x"),
            _mcp("mcp-b", gateway_arn=_GW_B, audience="api://agp-mcp-mcp-b", tenant_id="ten-x"),
        ],
    )
    with caplog.at_level(logging.WARNING):
        svc.start_runtime_build(
            agent_id="ag-1",
            image_tag=_SHA,
            ecr_repo="",
            connection_id="conn-1",
            stage="dev",
        )
    env = _env_map(cb.calls[0])
    assert json.loads(env["GATEWAY_TOOL_AUTH"]) == {_GW_B: "api://agp-mcp-mcp-b/.default"}
    msgs = [r.getMessage() for r in caplog.records if "several MCP records" in r.getMessage()]
    assert msgs and "mcp-a" in msgs[0] and "mcp-dup" in msgs[0]
    assert "111" not in msgs[0]


@pytest.mark.parametrize("bad", ["Claims_API", "x", "claims api", "a" * 32, "1claims", 'c";rm'])
def test_an_mcp_name_outside_the_rule_is_refused_before_any_side_effect(bad):
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    svc = _mcp_svc(cb, sm, mcps=[_mcp(name=bad)])
    with pytest.raises(RuntimeBuildError) as exc_info:
        svc.start_runtime_build(
            mcp_server_id="mcp-1",
            image_tag=_SHA,
            ecr_repo="",
            connection_id="conn-1",
            stage="dev",
        )
    assert exc_info.value.kind == "invalid_name"
    assert cb.calls == [] and sm.writes == []


@pytest.mark.parametrize(
    "kind",
    ["not_found", "unsupported", "build_kind_mismatch", "credential_staging_failed", "start_failed"],
)
def test_every_refusal_but_conflict_and_the_name_rule_renders_the_failed_to_start_body(kind):
    err = RuntimeBuildError("store internals", kind=kind)

    assert (err.status_code, err.kind) == (502, kind)
    assert err.body() == {"detail": {"status": "failed_to_start"}, "code": f"runtime_build.{kind}"}


def test_a_conflict_renders_the_in_progress_body_carrying_the_message():
    err = RuntimeBuildError("a build for agent_id=ag-1 stage=dev is already in progress", kind="conflict")

    assert err.status_code == 409
    assert err.body() == {
        "detail": {
            "status": "build_in_progress",
            "message": "a build for agent_id=ag-1 stage=dev is already in progress",
        },
        "code": "runtime_build.conflict",
    }


def test_an_unbuildable_name_renders_the_fixed_422_detail():
    err = RuntimeBuildError("MCP server name does not match the rule", kind="invalid_name")

    assert err.status_code == 422
    assert err.body() == {"detail": "MCP server name is not buildable", "code": "runtime_build.invalid_name"}


@pytest.mark.parametrize("artifact", ["agent", "mcp"])
def test_a_conflict_message_names_only_the_artifact_and_the_stage(artifact):
    """``conflict`` sends its message to the client, so the message is built from the caller's own
    ids and nothing else: no CodeBuild payload, no token, no store value."""
    if artifact == "agent":
        cb = FakeCodeBuild(in_flight_env={"AGENT_ID": "agent-42", "STAGE": "dev"})
        svc = _svc(cb, FakeConnectionService(_conn()), FakeSecretsManager())
        kwargs: dict[str, Any] = {"agent_id": "agent-42", "image_tag": "agent-42-abc123"}
        expected = "a build for agent_id=agent-42 stage=dev is already in progress"
    else:
        cb = FakeCodeBuild(in_flight_env={"MCP_SERVER_ID": "mcp-1", "STAGE": "dev"})
        svc = _mcp_svc(cb)
        kwargs = {"mcp_server_id": "mcp-1", "image_tag": _SHA}
        expected = "a build for mcp_server_id=mcp-1 stage=dev is already in progress"

    with pytest.raises(RuntimeBuildError) as exc_info:
        svc.start_runtime_build(**kwargs, ecr_repo="", connection_id="conn-1", stage="dev")

    assert exc_info.value.message == expected
    assert exc_info.value.detail == {"status": "build_in_progress", "message": expected}
