"""Unit tests for RuntimeBuildService (E22/T6, hardened in E22/T6b).

A fake codebuild client records `start_build` kwargs; a fake connection service returns a
Connection and a ready bearer token (PAT: the stored token; App: a freshly minted
installation token); a fake secretsmanager client records the scratch-token secret writes.

The fix (T6b): the build MUST NOT pass the connection's own secret_arn — for a GitHub App
connection that secret is `{"private_key": ...}` (no `.token`) and the buildspec's
`jq -r '.token'` clone would silently fail. Instead the service resolves a ready token via
`get_bearer_token` (uniform for PAT + App), writes `{"token": <token>}` to a short-lived
scratch secret, and passes THAT scratch ARN as GIT_SECRET_ARN. Asserts the env contract,
the scratch-secret body, and that the raw token never appears in the StartBuild payload.
"""

import json
from types import SimpleNamespace

import pytest

from models.connection import AuthType, Connection, ConnStatus, Provider
from models.tenant import TenantStageConfig
from services.runtime_build_service import RuntimeBuildError, RuntimeBuildService
from services.tenant_service import TenantError

_SCRATCH_PREFIX = "agp-test/runtime-build-token/"


class FakeCodeBuild:
    def __init__(self, *, build_id="build-123:abc", raise_exc=None):
        self.build_id = build_id
        self.raise_exc = raise_exc
        self.calls = []

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
    # E36/T9: the best-effort scratch delete names this class in an `except` clause, so the
    # fake must expose it — Python evaluates the clause expression whenever delete_secret
    # raises, and a missing attribute would surface as an AttributeError instead of the
    # RuntimeBuildError the caller is asserting on.
    ResourceNotFoundException = _ResourceNotFoundException


class FakeSecretsManager:
    """Records create_secret / put_secret_value writes and delete_secret calls; returns a
    deterministic ARN."""

    def __init__(self, *, raise_exc=None, already_exists=False, delete_raise_exc=None):
        self.raise_exc = raise_exc
        self.already_exists = already_exists  # create_secret raises ResourceExistsException
        self.delete_raise_exc = delete_raise_exc  # delete_secret raises this
        self.writes = []  # list of (name, secret_string)
        self.deletes = []  # list of delete_secret kwargs
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
    def __init__(self, connection, *, bearer_token="tok-ready"):
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


def _agent(tenant_id="ten-x"):
    return SimpleNamespace(tenant_id=tenant_id)


def _tenant(stages=None):
    if stages is None:
        stages = {
            "dev": TenantStageConfig(
                account_id="111111111111", region="us-east-1",
                ecr_repo_uri="", deploy_role_arn="agp-deployment-dev",
            ),
            "prod": TenantStageConfig(
                account_id="222222222222", region="eu-central-1",
                ecr_repo_uri="ecr-prod-uri", deploy_role_arn="agp-deployment-prod",
            ),
        }
    return SimpleNamespace(stages=stages)


def _conn(**over):
    base = dict(
        id="conn-1", provider=Provider.GITHUB, org="acme-org",
        base_url=None, auth_type=AuthType.PAT, status=ConnStatus.CONNECTED,
        secret_arn="arn:aws:secretsmanager:us-east-1:111:secret:agp/git/conn-1",
        has_secret=True, created_by="a@b.com", created_at="t", updated_at="t",
    )
    base.update(over)
    return Connection(**base)


def _svc(cb, conn_svc, sm, *, tenant_service=None, agent_registry=None):
    return RuntimeBuildService(
        cb, conn_svc, sm,
        tenant_service=tenant_service or FakeTenantService(_tenant()),
        agent_registry=agent_registry or FakeAgentRegistry(_agent()),
        codebuild_project_name="p", scratch_secret_prefix=_SCRATCH_PREFIX,
    )


def _env_map(kwargs):
    return {e["name"]: e["value"] for e in kwargs["environmentVariablesOverride"]}


def test_start_build_env_contract_and_build_id():
    cb = FakeCodeBuild(build_id="rt-build:xyz")
    sm = FakeSecretsManager()
    conn_svc = FakeConnectionService(_conn(base_url="https://ghe.acme.internal"))
    svc = RuntimeBuildService(
        cb, conn_svc, sm,
        tenant_service=FakeTenantService(_tenant()), agent_registry=FakeAgentRegistry(_agent()),
        codebuild_project_name="agp-runtime-provision", scratch_secret_prefix=_SCRATCH_PREFIX,
    )

    build_id = svc.start_runtime_build(
        agent_id="agent-42", image_tag="agent-42-abc123",
        ecr_repo="123.dkr.ecr.us-east-1.amazonaws.com/agp-agents", connection_id="conn-1",
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
    # GIT_SECRET_ARN points at the scratch secret, NOT the connection's own secret_arn.
    scratch_name = f"{_SCRATCH_PREFIX}agent-42-abc123"
    assert env["GIT_SECRET_ARN"] == sm._arn(scratch_name)
    assert env["GIT_SECRET_ARN"] != _conn().secret_arn
    # No ARCHIVE_* leftovers from the old EventBridge contract.
    assert "ARCHIVE_BUCKET" not in env and "ARCHIVE_KEY" not in env
    # E28B/T4 (D-B3): the LEGACY tag-only path sends the key with an EMPTY value — always present,
    # never absent. The buildspec branches on emptiness, and an absent override is indistinguishable
    # from an empty one to the shell, so "" is an explicit state rather than a missing variable.
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

    The backend resolves the registry by NAME and is the build's ONLY trigger (the EventBridge
    trigger was deleted in E22/T7), so it can supply the id per build — and the override takes
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
        agent_id="agent-42", image_tag="agent-42-abc123",
        ecr_repo="123.dkr.ecr.us-east-1.amazonaws.com/agp-agents", connection_id="conn-1",
        stage="dev",
    )
    entry = next(
        e for e in cb.calls[0]["environmentVariablesOverride"] if e["name"] == "AGENT_REGISTRY_ID"
    )
    # The value comes from the registry service — i.e. from the name resolution — not a constant.
    assert entry["value"] == "REGRESOLVED01"
    assert entry["type"] == "PLAINTEXT"


# --- E28B/T4 (D-B3): IMAGE_DIGEST is THE epic's headline link ----------------
#
# THE GAP THESE CLOSE (E28B final review, #2). The test above asserted nine env keys and not this
# one, so DELETING the `IMAGE_DIGEST` entry from `runtime_build_service.py` left the entire suite
# GREEN at 2433 passed — on the single link the epic's own ledger says it must not close without.
# The digest is the whole promotion contract: it names the exact bytes an Owner approved, and a
# mutable tag is precisely the hole it exists to close.


def test_the_image_digest_reaches_the_build_environment():
    """A digest passed in MUST arrive in the CodeBuild env, byte for byte.

    Asserted on the exact value, not merely presence: a truncated or re-derived digest would deploy
    bytes nobody approved while looking correct."""
    digest = "sha256:" + "ab" * 32
    cb = FakeCodeBuild(build_id="rt-build:xyz")
    svc = _svc(cb, FakeConnectionService(_conn()), FakeSecretsManager())
    svc.start_runtime_build(
        agent_id="agent-42", image_tag="agent-42-abc123",
        ecr_repo="123.dkr.ecr.us-east-1.amazonaws.com/agp-agents", connection_id="conn-1",
        stage="dev", image_digest=digest,
    )
    env = _env_map(cb.calls[0])
    assert env["IMAGE_DIGEST"] == digest, (
        "the approved digest must reach the buildspec — without it the deploy falls back to a "
        "MUTABLE tag, which is the exact hole D-B3 exists to close"
    )
    # It is sent as PLAINTEXT like its siblings (a digest is not a secret, and a mismatched type
    # would make CodeBuild look for a parameter/secret that does not exist).
    entry = next(e for e in cb.calls[0]["environmentVariablesOverride"] if e["name"] == "IMAGE_DIGEST")
    assert entry["type"] == "PLAINTEXT"
    # The tag still rides along — D-B3 says the digest fields sit ALONGSIDE the tag, not replacing
    # it, so a rollback to pre-E28B code can still find one.
    assert env["IMAGE_TAG"] == "agent-42-abc123"


def test_an_absent_digest_is_sent_as_an_explicit_empty_value():
    """The legacy path must send the key EMPTY rather than omit it.

    Omitting it would make the buildspec's emptiness branch depend on an undefined shell variable —
    the two are indistinguishable to `sh`, so the key's PRESENCE is what makes the legacy case an
    explicit state instead of a missing one."""
    cb = FakeCodeBuild(build_id="rt-build:xyz")
    svc = _svc(cb, FakeConnectionService(_conn()), FakeSecretsManager())
    svc.start_runtime_build(
        agent_id="agent-42", image_tag="agent-42-abc123",
        ecr_repo="123.dkr.ecr.us-east-1.amazonaws.com/agp-agents", connection_id="conn-1",
        stage="dev",
    )
    names = [e["name"] for e in cb.calls[0]["environmentVariablesOverride"]]
    assert "IMAGE_DIGEST" in names, "the key must be PRESENT even when there is no digest"
    assert _env_map(cb.calls[0])["IMAGE_DIGEST"] == ""
    assert names.count("IMAGE_DIGEST") == 1  # exactly one entry — a duplicate is ambiguous


def test_app_connection_writes_scratch_token_and_uses_scratch_arn():
    """App connection: get_bearer_token mints a token; a scratch `{"token": ...}` secret is
    written and StartBuild's GIT_SECRET_ARN is that scratch ARN (never the connection's
    private-key secret_arn). The raw token appears in NO env override value."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    minted = "ghs_minted_installation_token"
    conn_svc = FakeConnectionService(
        _conn(auth_type=AuthType.GITHUB_APP, app_id="123", installation_id="456"),
        bearer_token=minted,
    )
    svc = _svc(cb, conn_svc, sm)

    svc.start_runtime_build(
        agent_id="agent-9", image_tag="agent-9-deadbee",
        ecr_repo="r", connection_id="conn-1", stage="dev",
    )

    # A ready token was resolved via the auth-type seam (works for App).
    assert conn_svc.bearer_calls == ["conn-1"]
    # Exactly one scratch secret written, body = {"token": <minted>}.
    assert len(sm.writes) == 1
    name, body = sm.writes[0]
    assert name == f"{_SCRATCH_PREFIX}agent-9-deadbee"
    assert json.loads(body) == {"token": minted}
    # StartBuild uses the scratch ARN, not the connection's private-key secret_arn.
    env = _env_map(cb.calls[0])
    assert env["GIT_SECRET_ARN"] == sm._arn(name)
    assert env["GIT_SECRET_ARN"] != _conn().secret_arn
    # The raw minted token is NEVER in any env override value.
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
        agent_id="a", image_tag="a-sha", ecr_repo="r", connection_id="conn-1", stage="dev",
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
        agent_id="a", image_tag="a-sha", ecr_repo="r", connection_id="conn-1", stage="dev",
    )

    # The build proceeded (no AttributeError from a bad except name).
    assert build_id == cb.build_id
    # Exactly one write, via the put_secret_value fallback, with the token body.
    assert len(sm.writes) == 1
    name, body = sm.writes[0]
    assert name == f"{_SCRATCH_PREFIX}a-sha"
    assert json.loads(body) == {"token": pat}
    # GIT_SECRET_ARN still resolves to the scratch ARN.
    assert _env_map(cb.calls[0])["GIT_SECRET_ARN"] == sm._arn(name)


def test_secret_arn_passed_not_token():
    """The scratch Secrets Manager ARN is passed; no raw token/credential is ever in the payload."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    svc = _svc(cb, FakeConnectionService(_conn(), bearer_token="ghp_x"), sm)
    svc.start_runtime_build(
        agent_id="a", image_tag="t", ecr_repo="r", connection_id="conn-1", stage="dev",
    )
    env = _env_map(cb.calls[0])
    assert env["GIT_SECRET_ARN"].startswith("arn:aws:secretsmanager")
    # No env var carries a token/private-key value.
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
        svc.start_runtime_build(agent_id="a", image_tag="t", ecr_repo="r", connection_id="conn-1", stage="dev")


def test_scratch_secret_write_failure_raises_runtime_build_error():
    """A Secrets Manager write failure surfaces as RuntimeBuildError (SAFE) — StartBuild is
    never reached and the token never leaks."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager(raise_exc=RuntimeError("sm down"))
    svc = _svc(cb, FakeConnectionService(_conn(), bearer_token="ghp_secret"), sm)
    with pytest.raises(RuntimeBuildError):
        svc.start_runtime_build(agent_id="a", image_tag="t", ecr_repo="r", connection_id="conn-1", stage="dev")
    assert cb.calls == []


# ---------------------------------------------------------------------------
# E36/T9: the scratch secret is written BEFORE StartBuild, so a StartBuild that fails
# leaves a LIVE clone token (a verbatim PAT, or a ~1h installation token) in Secrets
# Manager that nothing will ever delete — no build exists to run the buildspec's
# post_build purge. The service owns cleanup on exactly that path.
# ---------------------------------------------------------------------------


def test_a_failed_start_build_deletes_the_scratch_secret_it_just_wrote():
    """The leak: `_write_scratch_token` runs first, so a StartBuild fault must force-delete
    the scratch secret before re-raising. `ForceDeleteWithoutRecovery=True` is required —
    the default 30-day recovery window would keep the token readable and billing."""
    cb = FakeCodeBuild(raise_exc=RuntimeError("boom"))
    sm = FakeSecretsManager()
    svc = _svc(cb, FakeConnectionService(_conn(), bearer_token="ghp_leaky"), sm)

    with pytest.raises(RuntimeBuildError):
        svc.start_runtime_build(
            agent_id="a", image_tag="a-sha", ecr_repo="r", connection_id="conn-1", stage="dev",
        )

    name, _body = sm.writes[0]
    assert sm.deletes == [
        {"SecretId": sm._arn(name), "ForceDeleteWithoutRecovery": True}
    ], "the just-written scratch secret must be force-deleted on the StartBuild failure path"


def test_a_SUCCESSFUL_start_build_leaves_the_scratch_secret_alone():
    """The control plane must NOT delete on the happy path: CodeBuild reads the secret later,
    under its own role, and deleting here would break every clone. The buildspec's post_build
    purge owns that side (pinned in tests/test_buildspec_contract.py)."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    svc = _svc(cb, FakeConnectionService(_conn()), sm)

    svc.start_runtime_build(
        agent_id="a", image_tag="a-sha", ecr_repo="r", connection_id="conn-1", stage="dev",
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
            agent_id="a", image_tag="a-sha", ecr_repo="r", connection_id="conn-1", stage="dev",
        )

    assert str(ei.value) == "Failed to start runtime build"
    assert len(sm.deletes) == 1  # it was attempted


# ---------------------------------------------------------------------------
# E25/T5: server-side tenant derivation + cross-account overrides.
# ---------------------------------------------------------------------------


def test_start_runtime_build_passes_target_role_for_stage():
    """The stage's tenant config drives TARGET_ROLE_ARN/TARGET_ACCOUNT_ID/STAGE/TENANT_ID —
    the tenant is derived from the agent, never the request body."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    svc = _svc(
        cb, FakeConnectionService(_conn()), sm,
        tenant_service=FakeTenantService(_tenant()), agent_registry=FakeAgentRegistry(_agent()),
    )
    svc.start_runtime_build(
        agent_id="ag-1", image_tag="ag-1-abc", ecr_repo="req-ecr", connection_id="conn-1", stage="prod",
    )
    env = _env_map(cb.calls[0])
    assert env["TARGET_ROLE_ARN"] == "agp-deployment-prod"
    assert env["TARGET_ACCOUNT_ID"] == "222222222222"
    assert env["AWS_TARGET_REGION"] == "eu-central-1"
    assert env["STAGE"] == "prod"
    assert env["TENANT_ID"] == "ten-x"
    # EXEC_ROLE_ARN is intentionally NOT overridden — it is a different role kind than the
    # deploy role, so the CodeBuild project's baked default (the agentcore runtime exec role)
    # stays authoritative. Overriding it here would feed the wrong role to CreateAgentRuntime.
    assert "EXEC_ROLE_ARN" not in env


def test_start_runtime_build_prefers_tenant_ecr_over_request():
    """When the tenant stage carries an ecr_repo_uri it is authoritative over the request's."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    svc = _svc(
        cb, FakeConnectionService(_conn()), sm,
        tenant_service=FakeTenantService(_tenant()), agent_registry=FakeAgentRegistry(_agent()),
    )
    svc.start_runtime_build(
        agent_id="ag-1", image_tag="t", ecr_repo="req-ecr", connection_id="conn-1", stage="prod",
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
        cb, FakeConnectionService(_conn()), sm,
        tenant_service=FakeTenantService(_tenant(stages)), agent_registry=FakeAgentRegistry(_agent()),
    )
    svc.start_runtime_build(
        agent_id="ag-1", image_tag="t", ecr_repo="req-ecr", connection_id="conn-1", stage="dev",
    )
    env = _env_map(cb.calls[0])
    assert env["TARGET_ROLE_ARN"] == ""
    # EXEC_ROLE_ARN is NOT emitted — for an unwired tenant, overriding it with "" would clobber
    # the CodeBuild project's correct baked default (env overrides replace, not fall through).
    assert "EXEC_ROLE_ARN" not in env
    assert env["ECR_REPO"] == "req-ecr"  # tenant ecr unset ⇒ request value


def test_start_runtime_build_unknown_agent_raises():
    """An unknown agent (registry returns None) raises RuntimeBuildError before StartBuild."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    svc = _svc(
        cb, FakeConnectionService(_conn()), sm,
        agent_registry=FakeAgentRegistry(None),
    )
    with pytest.raises(RuntimeBuildError):
        svc.start_runtime_build(
            agent_id="nope", image_tag="t", ecr_repo="e", connection_id="conn-1", stage="dev",
        )
    assert cb.calls == []


def test_start_runtime_build_unknown_tenant_raises_build_error():
    """An agent whose tenant_id resolves to no tenant (tenants.get raises TenantError) must
    surface as RuntimeBuildError (not_found), NOT an unhandled TenantError escaping to a 500.
    StartBuild is never reached."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    svc = _svc(
        cb, FakeConnectionService(_conn()), sm,
        tenant_service=FakeTenantService(None, raise_exc=TenantError("Unknown tenant", kind="not_found")),
        agent_registry=FakeAgentRegistry(_agent()),
    )
    with pytest.raises(RuntimeBuildError) as ei:
        svc.start_runtime_build(
            agent_id="ag-1", image_tag="t", ecr_repo="e", connection_id="conn-1", stage="dev",
        )
    assert ei.value.kind == "not_found"
    assert cb.calls == []


def test_start_runtime_build_none_tenant_id_raises_build_error():
    """A legacy/un-stamped agent (tenant_id is None) must surface as RuntimeBuildError
    (not_found), not an unhandled error. StartBuild is never reached."""
    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    svc = _svc(
        cb, FakeConnectionService(_conn()), sm,
        tenant_service=FakeTenantService(None, raise_exc=TenantError("Unknown tenant", kind="not_found")),
        agent_registry=FakeAgentRegistry(_agent(tenant_id=None)),
    )
    with pytest.raises(RuntimeBuildError) as ei:
        svc.start_runtime_build(
            agent_id="ag-1", image_tag="t", ecr_repo="e", connection_id="conn-1", stage="dev",
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
        cb, FakeConnectionService(_conn()), sm,
        tenant_service=FakeTenantService(_tenant(stages)), agent_registry=FakeAgentRegistry(_agent()),
    )
    with pytest.raises(RuntimeBuildError) as ei:
        svc.start_runtime_build(
            agent_id="ag-1", image_tag="t", ecr_repo="e", connection_id="conn-1", stage="prod",
        )
    assert ei.value.kind == "not_found"
    assert cb.calls == []


# --- E29/T7 (ledger OB-3): the AWS-only stage reads are platform-gated -------

def test_start_build_refuses_a_databricks_tenant():
    """A Databricks tenant is refused BEFORE the AWS-only stage reads (`ecr_repo_uri`,
    `deploy_role_arn`, `account_id`, `region`) — none of which exist on a `DatabricksStageConfig`.

    Unreachable until E29/T3 made `platform="databricks"` a real stored value; from then on this
    method would have raised `AttributeError` out of a pydantic model and surfaced as a 500. There
    is no AgentCore runtime to build for a Databricks agent, so the honest answer is an explicit
    unsupported-operation error, and no StartBuild (or scratch-secret write) may happen.
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
            agent_id="agent-42", image_tag="agent-42-abc123",
            ecr_repo="repo", connection_id="conn-1", stage="dev",
        )

    assert exc.value.kind == "unsupported"
    assert cb.calls == []
    assert sm.writes == []


def test_start_build_allows_an_explicitly_aws_tenant():
    """The gate reads `platform` when it is present and defaults to AWS when it is not, so an
    explicitly-AWS tenant (and every pre-E29 record, which carries no `platform` at all) builds
    exactly as before — the fence above proves the absent case."""
    from models.tenant import TenantPlatform

    cb = FakeCodeBuild()
    sm = FakeSecretsManager()
    conn_svc = FakeConnectionService(_conn())
    tenant = SimpleNamespace(platform=TenantPlatform.AWS, stages=_tenant().stages)
    svc = _svc(cb, conn_svc, sm, tenant_service=FakeTenantService(tenant))

    build_id = svc.start_runtime_build(
        agent_id="agent-42", image_tag="agent-42-abc123",
        ecr_repo="repo", connection_id="conn-1", stage="dev",
    )

    assert build_id == cb.build_id
    assert len(cb.calls) == 1
