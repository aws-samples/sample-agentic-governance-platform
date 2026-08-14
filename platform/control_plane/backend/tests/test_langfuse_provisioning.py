"""Tests for the per-agent Langfuse provisioner (Epic 26, Task 4).

Executable spec for the C2 methods on ``LangfuseProvisioningService``:
``provision_agent_project`` (create project + mint key + store in Secrets Manager +
write the C1 join onto the agent envelope; idempotent) and ``delete_agent_project``
(best-effort/idempotent teardown), plus the best-effort register-time hook.

ALL external side effects are mocked — there are NO live Langfuse / AWS calls:
  - the internal tRPC HTTP → the ``requests.Session`` returned by ``_get_session`` is a
    ``MagicMock`` whose ``.post`` is sequenced via ``side_effect`` (one response per
    tRPC call), so no CSRF/auto-login round-trip runs.
  - AWS Secrets Manager → moto's ``@mock_aws`` (mirrors ``test_connection_service.py``).

SECRET-SAFETY: the secret VALUES (public/secret key) live ONLY in Secrets Manager —
the tests assert the key values never land on the agent envelope (only the secret NAME
+ project id do).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from models.agent import Agent, AuthType, LifecycleState, Platform
from services.langfuse_provisioning import LangfuseProvisioningService

REGION = "us-east-1"
HOST = "https://langfuse.example.com"
ADMIN_SECRET = "agp-dev/langfuse-admin"


def _agent(
    *,
    agent_id: str = "rec-abc123",
    name: str = "Claims Triage DE",
    langfuse_project_id: str | None = None,
    langfuse_key_secret_name: str | None = None,
) -> Agent:
    now = datetime.now(timezone.utc)
    return Agent(
        id=agent_id,
        name=name,
        purpose="Triage inbound motor claims",
        lifecycle_state=LifecycleState.APPROVED,
        platform=Platform.AWS_BEDROCK,
        auth_type=AuthType.ENTRA,
        agent_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/x-y",
        langfuse_project_id=langfuse_project_id,
        langfuse_key_secret_name=langfuse_key_secret_name,
        created_at=now,
        updated_at=now,
    )


def _resp(payload: dict) -> MagicMock:
    """A mock tRPC HTTP response (200; ``raise_for_status`` is a no-op)."""
    m = MagicMock(name="tRPC-response")
    m.status_code = 200
    m.json.return_value = payload
    m.raise_for_status = MagicMock()
    return m


def _svc(*, registry=None, tenants=None) -> LangfuseProvisioningService:
    return LangfuseProvisioningService(
        langfuse_host=HOST,
        langfuse_secret_name=ADMIN_SECRET,
        region=REGION,
        registry=registry,
        agp_project_name="agp",
        tenants=tenants,
    )


# ===========================================================================
# 1) create → project + key + SM store + envelope join
# ===========================================================================
@mock_aws
def test_provision_creates_project_and_writes_envelope():
    agent = _agent()
    registry = MagicMock(name="AgentRegistryService")
    registry.persist_identity.side_effect = lambda a: a

    svc = _svc(registry=registry)
    session = MagicMock(name="requests.Session")
    session.post.side_effect = [
        _resp({"result": {"data": {"json": {"id": "clx-proj-1"}}}}),  # projects.create
        _resp({"result": {"data": {"json": {"publicKey": "pk-lf-abc", "secretKey": "sk-lf-xyz"}}}}),  # projectApiKeys.create
    ]
    svc._get_session = lambda: session  # bypass the CSRF/auto-login round-trip

    result = svc.provision_agent_project(agent)

    expected_secret = f"langfuse-agent-{agent.id}-keys"
    assert result == {
        "project_id": "clx-proj-1",
        "secret_name": expected_secret,
        "public_key": "pk-lf-abc",
    }

    # tRPC: create project THEN mint key (two calls).
    assert session.post.call_count == 2
    create_call = session.post.call_args_list[0]
    assert create_call.args[0].endswith("/api/trpc/projects.create")
    assert create_call.kwargs["json"] == {"json": {"name": "agp-Claims Triage DE", "orgId": "seed-org"}}

    # Secrets Manager holds the 4-key JSON (public_key, secret_key, project_name, project_id).
    sm = boto3.client("secretsmanager", region_name=REGION)
    body = json.loads(sm.get_secret_value(SecretId=expected_secret)["SecretString"])
    assert body == {
        "public_key": "pk-lf-abc",
        "secret_key": "sk-lf-xyz",
        "project_name": "agp-Claims Triage DE",
        "project_id": "clx-proj-1",
    }

    # C1 join is on the agent envelope + persisted via the registry.
    assert agent.langfuse_project_id == "clx-proj-1"
    assert agent.langfuse_key_secret_name == expected_secret
    registry.persist_identity.assert_called_once_with(agent)

    # SECRET-SAFETY: the secret VALUE never leaks onto the envelope.
    envelope = agent.to_envelope()
    assert "sk-lf-xyz" not in json.dumps(envelope)
    assert "pk-lf-abc" not in json.dumps(envelope)
    assert envelope["langfuse_key_secret_name"] == expected_secret
    assert envelope["langfuse_project_id"] == "clx-proj-1"


# ===========================================================================
# 2) idempotent — already provisioned ⇒ no tRPC create, returns existing id
# ===========================================================================
@mock_aws
def test_provision_is_idempotent():
    secret_name = "langfuse-agent-rec-abc123-keys"
    sm = boto3.client("secretsmanager", region_name=REGION)
    sm.create_secret(
        Name=secret_name,
        SecretString=json.dumps({
            "public_key": "pk-lf-old",
            "secret_key": "sk-lf-old",
            "project_name": "agp-Claims Triage DE",
            "project_id": "clx-existing",
        }),
    )
    agent = _agent(langfuse_project_id="clx-existing", langfuse_key_secret_name=secret_name)
    registry = MagicMock(name="AgentRegistryService")

    svc = _svc(registry=registry)
    session = MagicMock(name="requests.Session")
    svc._get_session = lambda: session

    result = svc.provision_agent_project(agent)

    assert result["project_id"] == "clx-existing"
    assert result["secret_name"] == secret_name
    assert result["public_key"] == "pk-lf-old"
    # NO tRPC create, NO new project, NO re-persist.
    session.post.assert_not_called()
    registry.persist_identity.assert_not_called()


# ===========================================================================
# 2b) already provisioned BUT the stored secret was deleted out-of-band ⇒
#     re-mint instead of dead-locking (the Important finding).
# ===========================================================================
@mock_aws
def test_provision_remints_when_secret_missing():
    # Envelope carries a project id, but NO secret exists in Secrets Manager.
    secret_name = "langfuse-agent-rec-abc123-keys"
    agent = _agent(langfuse_project_id="clx-existing", langfuse_key_secret_name=secret_name)
    registry = MagicMock(name="AgentRegistryService")
    registry.persist_identity.side_effect = lambda a: a

    svc = _svc(registry=registry)
    session = MagicMock(name="requests.Session")
    session.post.side_effect = [
        _resp({"result": {"data": {"json": {"id": "clx-existing"}}}}),  # projects.create (reuses existing)
        _resp({"result": {"data": {"json": {"publicKey": "pk-lf-new", "secretKey": "sk-lf-new"}}}}),  # mint
    ]
    svc._get_session = lambda: session

    # MUST recover (re-mint) rather than raise on the missing secret.
    result = svc.provision_agent_project(agent)

    assert result == {
        "project_id": "clx-existing",
        "secret_name": secret_name,
        "public_key": "pk-lf-new",
    }
    # Fell through to the create+mint path (two tRPC calls) instead of dead-locking.
    assert session.post.call_count == 2
    # The SM secret was (re-)created with the freshly minted key pair.
    sm = boto3.client("secretsmanager", region_name=REGION)
    body = json.loads(sm.get_secret_value(SecretId=secret_name)["SecretString"])
    assert body["public_key"] == "pk-lf-new"
    assert body["secret_key"] == "sk-lf-new"
    assert body["project_id"] == "clx-existing"


# ===========================================================================
# 3) register-time hook is best-effort — failure swallowed, fields stay None
# ===========================================================================
def test_provision_failure_is_non_blocking(monkeypatch):
    import api.routes.agents as agents_module
    from api.routes.agents import provision_langfuse_best_effort

    # Langfuse configured (else the hook is a no-op by design).
    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", HOST)

    agent = _agent()
    service = MagicMock(name="LangfuseProvisioningService")
    service.provision_agent_project.side_effect = RuntimeError("tRPC create failed")

    # The register-time path MUST NOT raise (registration is not aborted).
    provision_langfuse_best_effort(agent, service)

    service.provision_agent_project.assert_called_once_with(agent)
    assert agent.langfuse_project_id is None
    assert agent.langfuse_key_secret_name is None


# ===========================================================================
# 4) delete — already-gone (NotFound) ⇒ success, no raise
# ===========================================================================
@mock_aws
def test_delete_agent_project_idempotent():
    # project + secret both already gone: SM has no secret; the tRPC delete errors.
    agent = _agent(
        langfuse_project_id="clx-gone",
        langfuse_key_secret_name="langfuse-agent-rec-abc123-keys",
    )
    svc = _svc()
    session = MagicMock(name="requests.Session")
    session.post.side_effect = RuntimeError("404 project not found")
    svc._get_session = lambda: session

    # Best-effort/idempotent: already-gone == success, returns None, no raise.
    assert svc.delete_agent_project(agent) is None
    # It still ATTEMPTED the tRPC project delete (the error was swallowed, not skipped).
    session.post.assert_called_once()
    assert session.post.call_args.args[0].endswith("/api/trpc/projects.delete")


# ===========================================================================
# 4b) delete happy path — project + secret EXIST ⇒ both are actually deleted
# ===========================================================================
@mock_aws
def test_delete_agent_project_deletes_project_and_secret():
    secret_name = "langfuse-agent-rec-abc123-keys"
    sm = boto3.client("secretsmanager", region_name=REGION)
    sm.create_secret(
        Name=secret_name,
        SecretString=json.dumps({
            "public_key": "pk-lf-x",
            "secret_key": "sk-lf-x",
            "project_name": "agp-Claims Triage DE",
            "project_id": "clx-live",
        }),
    )
    agent = _agent(langfuse_project_id="clx-live", langfuse_key_secret_name=secret_name)

    svc = _svc()
    session = MagicMock(name="requests.Session")
    session.post.return_value = _resp({"result": {"data": {"json": {"success": True}}}})
    svc._get_session = lambda: session

    assert svc.delete_agent_project(agent) is None

    # The tRPC project delete was ATTEMPTED with the right endpoint + project id.
    session.post.assert_called_once()
    delete_call = session.post.call_args
    assert delete_call.args[0].endswith("/api/trpc/projects.delete")
    assert delete_call.kwargs["json"] == {"json": {"projectId": "clx-live"}}

    # The SM secret was actually deleted (force-deleted, no recovery window).
    with pytest.raises(sm.exceptions.ResourceNotFoundException):
        sm.get_secret_value(SecretId=secret_name)


# ===========================================================================
# E36/T13 — the cross-account + runtime-wiring legs (item 26).
#
# Four legs, all offline (moto for the AMBIENT/control-plane account, a MagicMock with REAL
# botocore exception classes for the TENANT account — moto cannot serve two accounts):
#   (1) the account is PARSED from ``agent.agent_arn``; == the platform account ⇒ the ambient
#       path, byte for byte what shipped (``stage_client`` is never even reached, and neither
#       is the tenant lookup);
#   (2) != ⇒ the per-agent secret is created through ``stage_client`` with the MATCHING stage's
#       ``TenantStageConfig``; NO matching stage (or a failed assume) ⇒ platform-account
#       creation + a warning, and registration is NEVER blocked;
#   (3) after provisioning succeeds, ``LANGFUSE_HOST`` + ``LANGFUSE_SECRET_NAME`` are injected
#       onto the runtime via ``set_runtime_environment``, under the account-aware client;
#   (4) an SM resource policy on THAT ONE secret grants ``secretsmanager:GetSecretValue`` to the
#       runtime's exec ``roleArn`` (the value ``set_runtime_environment`` now returns).
#
# ``123456789012`` is moto's ambient account — the same one ``_agent``'s ``agent_arn`` names, so
# the same-account leg needs no extra setup. ``111122223333`` is the tenant account.
# ===========================================================================

PLATFORM_ACCOUNT = "123456789012"
TENANT_ACCOUNT = "111122223333"
TENANT_ROLE = f"arn:aws:iam::{TENANT_ACCOUNT}:role/agp-deployment-acme-prod"
PLATFORM_ROLE = f"arn:aws:iam::{PLATFORM_ACCOUNT}:role/agp-deployment-agp-default"


def _cross_agent(**kw):
    """An agent whose runtime lives in the TENANT account."""
    agent = _agent(**kw)
    agent.agent_arn = f"arn:aws:bedrock-agentcore:{REGION}:{TENANT_ACCOUNT}:runtime/x-y"
    agent.tenant_id = "ten-acme"
    return agent


def _stage(*, account_id: str, deploy_role_arn: str):
    from models.tenant import TenantStageConfig

    return TenantStageConfig(
        account_id=account_id, region=REGION, deploy_role_arn=deploy_role_arn
    )


def _tenants(stages: dict):
    """A duck-typed TenantService whose ``get`` answers a record carrying ``stages``."""
    from types import SimpleNamespace

    svc = MagicMock(name="TenantService")
    svc.get.return_value = SimpleNamespace(id="ten-acme", stages=stages)
    return svc


def _tenant_sm():
    """A MagicMock secretsmanager client for the tenant account, carrying the REAL botocore
    exception classes (an ``except MagicMock().exceptions.X`` would be a TypeError)."""
    ambient = boto3.client("secretsmanager", region_name=REGION)
    fake = MagicMock(name="tenant-account-secretsmanager")
    fake.exceptions.ResourceNotFoundException = (
        ambient.exceptions.ResourceNotFoundException
    )
    fake.exceptions.ResourceExistsException = ambient.exceptions.ResourceExistsException
    return fake


def _wired_session() -> MagicMock:
    session = MagicMock(name="requests.Session")
    session.post.side_effect = [
        _resp({"result": {"data": {"json": {"id": "clx-proj-x"}}}}),
        _resp({"result": {"data": {"json": {"publicKey": "pk-x", "secretKey": "sk-x"}}}}),
    ]
    return session


# --- leg 1: same account ⇒ the ambient path, unchanged ---------------------
@mock_aws
def test_same_account_agent_keeps_the_ambient_client():
    """The account is parsed from ``agent_arn``; equal to the platform account ⇒ today's path.

    The tenant lookup is asserted NOT to happen: the short-circuit must precede it, so a tenant
    store outage cannot change how a single-account agent is provisioned.
    """
    agent = _agent()  # agent_arn account == PLATFORM_ACCOUNT
    agent.tenant_id = "ten-acme"
    # The default tenant's deploy role lives in the PLATFORM account (the shipped single-account
    # shape) — so a naive "does any stage carry a deploy_role_arn" test would assume into it.
    tenants = _tenants({"dev": _stage(account_id=PLATFORM_ACCOUNT, deploy_role_arn=PLATFORM_ROLE)})
    svc = _svc(tenants=tenants)
    svc._stage_client = MagicMock(name="stage_client", side_effect=AssertionError("assumed"))
    svc._get_session = lambda: _wired_session()

    result = svc.provision_agent_project(agent)

    assert result["secret_name"] == f"langfuse-agent-{agent.id}-keys"
    svc._stage_client.assert_not_called()
    tenants.get.assert_not_called()
    # The secret landed in the AMBIENT (platform) account.
    sm = boto3.client("secretsmanager", region_name=REGION)
    assert json.loads(sm.get_secret_value(SecretId=result["secret_name"])["SecretString"])[
        "public_key"
    ] == "pk-x"


# --- leg 2: cross account ⇒ stage_client with the MATCHING stage -----------
@mock_aws
def test_cross_account_agent_creates_the_secret_through_stage_client():
    agent = _cross_agent()
    tenants = _tenants(
        {
            "dev": _stage(account_id=PLATFORM_ACCOUNT, deploy_role_arn=PLATFORM_ROLE),
            "prod": _stage(account_id=TENANT_ACCOUNT, deploy_role_arn=TENANT_ROLE),
        }
    )
    tenant_sm = _tenant_sm()
    svc = _svc(tenants=tenants)
    svc._stage_client = MagicMock(name="stage_client", return_value=tenant_sm)
    svc._get_session = lambda: _wired_session()

    result = svc.provision_agent_project(agent)

    # stage_client("secretsmanager", <the prod cfg>, session_suffix="lf-<id[:8]>") — the pinned
    # signature: cfg positional, session_suffix keyword-only.
    svc._stage_client.assert_called_once()
    call = svc._stage_client.call_args
    assert call.args[0] == "secretsmanager"
    assert call.args[1].deploy_role_arn == TENANT_ROLE  # the MATCHING stage, not "dev"
    assert call.kwargs == {"session_suffix": f"lf-{agent.id[:8]}"}

    # The secret was created in the TENANT account, and NOT in the platform account.
    tenant_sm.create_secret.assert_called_once()
    assert tenant_sm.create_secret.call_args.kwargs["Name"] == result["secret_name"]
    sm = boto3.client("secretsmanager", region_name=REGION)
    with pytest.raises(sm.exceptions.ResourceNotFoundException):
        sm.get_secret_value(SecretId=result["secret_name"])


@mock_aws
def test_cross_account_no_matching_stage_falls_back_to_the_platform_account(caplog):
    """No stage's ``deploy_role_arn`` names the agent's account ⇒ platform-account creation +
    a warning. Best-effort by design: registration is never blocked."""
    agent = _cross_agent()
    agent.agent_arn = f"arn:aws:bedrock-agentcore:{REGION}:999988887777:runtime/x-y"
    tenants = _tenants({"prod": _stage(account_id=TENANT_ACCOUNT, deploy_role_arn=TENANT_ROLE)})
    svc = _svc(tenants=tenants)
    svc._stage_client = MagicMock(name="stage_client", side_effect=AssertionError("assumed"))
    svc._get_session = lambda: _wired_session()

    with caplog.at_level(logging.WARNING):
        result = svc.provision_agent_project(agent)

    svc._stage_client.assert_not_called()
    assert any("[langfuse]" in r.message and r.levelname == "WARNING" for r in caplog.records)
    # NO ACCOUNT ID IN ANY MESSAGE (the hard project rule).
    for record in caplog.records:
        assert "999988887777" not in record.getMessage()
    sm = boto3.client("secretsmanager", region_name=REGION)
    assert sm.get_secret_value(SecretId=result["secret_name"])["SecretString"]


@mock_aws
def test_cross_account_assume_failure_falls_back_and_never_raises(caplog):
    """A failed AssumeRole is a warning + the platform-account fallback, NOT a raise: this flow
    is best-effort and a minted Langfuse key that is never stored is the worst outcome."""
    from services.tenant_credentials import TenantCredentialsError

    agent = _cross_agent()
    tenants = _tenants({"prod": _stage(account_id=TENANT_ACCOUNT, deploy_role_arn=TENANT_ROLE)})
    svc = _svc(tenants=tenants)
    svc._stage_client = MagicMock(
        name="stage_client",
        side_effect=TenantCredentialsError("agp-deployment-acme-prod (ClientError)"),
    )
    svc._get_session = lambda: _wired_session()

    with caplog.at_level(logging.WARNING):
        result = svc.provision_agent_project(agent)

    sm = boto3.client("secretsmanager", region_name=REGION)
    assert sm.get_secret_value(SecretId=result["secret_name"])["SecretString"]
    assert any("[langfuse]" in r.message for r in caplog.records)


@mock_aws
def test_a_failed_platform_account_probe_is_retried_for_the_next_agent(caplog, monkeypatch):
    """E36/T13 fix round 1 — one transient ``sts:GetCallerIdentity`` blip must NOT pin the process.

    The probe flag used to be set BEFORE the call, so a single failure cached "platform account
    unknown" on the singleton for its whole lifetime and every cross-account agent registered
    afterwards silently got its secret in the platform account, where its runtime cannot read it —
    with ONE warning at process start rather than one per affected agent. Two registrations here:

      1. the probe fails ⇒ a per-agent WARNING that NAMES the degraded agent + the ambient path;
      2. the next agent probes AGAIN (2 STS attempts total), succeeds, and the cross-account path
         works — the secret is created through ``stage_client`` in the tenant account.
    """
    import services.langfuse_provisioning as module

    tenants = _tenants({"prod": _stage(account_id=TENANT_ACCOUNT, deploy_role_arn=TENANT_ROLE)})
    tenant_sm = _tenant_sm()
    svc = _svc(tenants=tenants)
    svc._stage_client = MagicMock(name="stage_client", return_value=tenant_sm)
    svc._get_session = lambda: _wired_session()

    real_client = module.boto3.client
    sts_attempts = []

    def _client(service_name, *args, **kwargs):
        if service_name == "sts":
            sts_attempts.append(service_name)
            if len(sts_attempts) == 1:
                raise RuntimeError("transient STS blip")
        return real_client(service_name, *args, **kwargs)

    monkeypatch.setattr(module.boto3, "client", _client)

    first = _cross_agent(agent_id="rec-probe-fail")
    with caplog.at_level(logging.WARNING):
        result_one = svc.provision_agent_project(first)

    # (1) degraded to ambient, and LOUDLY — the warning names the agent it degraded, with no
    # account id in it (the hard project rule).
    svc._stage_client.assert_not_called()
    assert any(
        "[langfuse]" in r.getMessage() and first.id in r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING"
    )
    for record in caplog.records:
        assert TENANT_ACCOUNT not in record.getMessage()
    sm = boto3.client("secretsmanager", region_name=REGION)
    assert sm.get_secret_value(SecretId=result_one["secret_name"])["SecretString"]

    # (2) the NEXT agent re-probes and gets the cross-account path.
    second = _cross_agent(agent_id="rec-probe-ok")
    result_two = svc.provision_agent_project(second)

    assert len(sts_attempts) == 2  # the failure was NOT cached
    svc._stage_client.assert_called_once()
    assert svc._stage_client.call_args.args[1].deploy_role_arn == TENANT_ROLE
    assert svc._stage_client.call_args.kwargs == {"session_suffix": f"lf-{second.id[:8]}"}
    tenant_sm.create_secret.assert_called_once()
    with pytest.raises(sm.exceptions.ResourceNotFoundException):
        sm.get_secret_value(SecretId=result_two["secret_name"])


@mock_aws
def test_no_tenant_service_stays_ambient():
    """The legacy wiring (no tenant service injected) is untouched — no lookup, no assume."""
    agent = _cross_agent()
    svc = _svc()  # tenants=None
    svc._stage_client = MagicMock(name="stage_client", side_effect=AssertionError("assumed"))
    svc._get_session = lambda: _wired_session()

    result = svc.provision_agent_project(agent)

    svc._stage_client.assert_not_called()
    sm = boto3.client("secretsmanager", region_name=REGION)
    assert sm.get_secret_value(SecretId=result["secret_name"])["SecretString"]


# --- legs 3+4: env injection + the read grant -----------------------------
@mock_aws
def test_wire_agent_runtime_injects_env_and_grants_read():
    secret_name = "langfuse-agent-rec-abc123-keys"
    sm = boto3.client("secretsmanager", region_name=REGION)
    sm.create_secret(Name=secret_name, SecretString=json.dumps({"public_key": "pk"}))
    agent = _agent()
    exec_role = f"arn:aws:iam::{PLATFORM_ACCOUNT}:role/claims-dev-agentcore-exec"
    identity = MagicMock(name="AgentIdentityService")
    identity.set_runtime_environment.return_value = exec_role

    svc = _svc()
    svc.wire_agent_runtime(agent, secret_name, identity)

    # (3) the injection payload — exactly the two keys, on the agent's runtime ARN, under the
    # account-aware control client (None ⇒ ambient for a same-account agent). ``wait_ready=False``
    # (fix round 1): the poll buys leg 4 nothing (its roleArn comes from the pre-poll GET and it
    # writes to Secrets Manager, not the runtime) and would park a shared threadpool worker for up
    # to 300 s on an UNCONDITIONAL per-registration path.
    identity.set_runtime_environment.assert_called_once_with(
        agent.agent_arn,
        {"LANGFUSE_HOST": HOST, "LANGFUSE_SECRET_NAME": secret_name},
        wait_ready=False,
        control_client=None,
    )

    # (4) the resource policy on THAT ONE secret names the runtime's exec role.
    policy = json.loads(sm.get_resource_policy(SecretId=secret_name)["ResourcePolicy"])
    assert policy["Version"] == "2012-10-17"
    assert len(policy["Statement"]) == 1
    statement = policy["Statement"][0]
    assert statement["Effect"] == "Allow"
    assert statement["Principal"] == {"AWS": exec_role}
    assert statement["Action"] == "secretsmanager:GetSecretValue"
    assert statement["Resource"] == "*"


@mock_aws
def test_wire_agent_runtime_uses_the_stage_control_client_cross_account():
    agent = _cross_agent()
    tenants = _tenants({"prod": _stage(account_id=TENANT_ACCOUNT, deploy_role_arn=TENANT_ROLE)})
    tenant_sm = _tenant_sm()
    tenant_control = MagicMock(name="tenant-bedrock-agentcore-control")
    exec_role = f"arn:aws:iam::{TENANT_ACCOUNT}:role/claims-prod-agentcore-exec"
    identity = MagicMock(name="AgentIdentityService")
    identity.set_runtime_environment.return_value = exec_role

    def _client(service_name, cfg, *, session_suffix):
        assert session_suffix == f"lf-{agent.id[:8]}"
        return tenant_control if service_name == "bedrock-agentcore-control" else tenant_sm

    svc = _svc(tenants=tenants)
    svc._stage_client = MagicMock(name="stage_client", side_effect=_client)
    svc.wire_agent_runtime(agent, "langfuse-agent-rec-abc123-keys", identity)

    assert identity.set_runtime_environment.call_args.kwargs["control_client"] is tenant_control
    # The policy was attached in the TENANT account, where the secret is.
    tenant_sm.put_resource_policy.assert_called_once()
    policy = json.loads(tenant_sm.put_resource_policy.call_args.kwargs["ResourcePolicy"])
    assert policy["Statement"][0]["Principal"] == {"AWS": exec_role}


@mock_aws
def test_wire_agent_runtime_is_a_no_op_without_a_runtime():
    agent = _agent()
    agent.agent_arn = ""
    identity = MagicMock(name="AgentIdentityService")

    _svc().wire_agent_runtime(agent, "langfuse-agent-rec-abc123-keys", identity)

    identity.set_runtime_environment.assert_not_called()


@mock_aws
def test_wire_agent_runtime_swallows_an_injection_failure(caplog):
    """Every new leg degrades to a logged no-op — never a registration failure."""
    agent = _agent()
    identity = MagicMock(name="AgentIdentityService")
    identity.set_runtime_environment.side_effect = RuntimeError("did not reach READY")

    with caplog.at_level(logging.WARNING):
        assert _svc().wire_agent_runtime(agent, "langfuse-agent-rec-abc123-keys", identity) is None

    assert any("[langfuse]" in r.message for r in caplog.records)


@mock_aws
def test_wire_agent_runtime_swallows_a_policy_failure(caplog):
    """The secret does not exist ⇒ the grant is a logged no-op; the injection still happened."""
    agent = _agent()
    identity = MagicMock(name="AgentIdentityService")
    identity.set_runtime_environment.return_value = f"arn:aws:iam::{PLATFORM_ACCOUNT}:role/x"

    with caplog.at_level(logging.WARNING):
        assert _svc().wire_agent_runtime(agent, "langfuse-agent-nope-keys", identity) is None

    identity.set_runtime_environment.assert_called_once()
    assert any("[langfuse]" in r.message for r in caplog.records)


# --- the registration hook wires the runtime after provisioning succeeds ---
def test_register_hook_wires_the_runtime_after_provisioning(monkeypatch):
    import api.routes.agents as agents_module
    from api.routes.agents import provision_langfuse_best_effort

    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", HOST)
    identity = MagicMock(name="AgentIdentityService")
    monkeypatch.setattr(agents_module, "get_identity_service", lambda: identity)

    agent = _agent()
    service = MagicMock(name="LangfuseProvisioningService")
    service.provision_agent_project.return_value = {
        "project_id": "clx-1",
        "secret_name": "langfuse-agent-rec-abc123-keys",
        "public_key": "pk",
    }

    provision_langfuse_best_effort(agent, service)

    service.wire_agent_runtime.assert_called_once_with(
        agent, "langfuse-agent-rec-abc123-keys", identity
    )


def test_register_hook_does_not_wire_when_provisioning_failed(monkeypatch):
    import api.routes.agents as agents_module
    from api.routes.agents import provision_langfuse_best_effort

    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", HOST)
    monkeypatch.setattr(agents_module, "get_identity_service", lambda: MagicMock())

    agent = _agent()
    service = MagicMock(name="LangfuseProvisioningService")
    service.provision_agent_project.side_effect = RuntimeError("tRPC create failed")

    provision_langfuse_best_effort(agent, service)  # MUST NOT raise

    service.wire_agent_runtime.assert_not_called()


@mock_aws
def test_register_hook_end_to_end_with_the_real_service(monkeypatch):
    """The hook against the REAL provisioner — the one test that pins the hook↔service contract.

    Every other hook test uses a MagicMock service, which would happily absorb a renamed method
    or a changed signature. This one runs the whole registration-time path (project → key → secret
    → env injection → read grant) with only Langfuse's HTTP and the identity service doubled.
    """
    import api.routes.agents as agents_module
    from api.routes.agents import provision_langfuse_best_effort

    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", HOST)
    exec_role = f"arn:aws:iam::{PLATFORM_ACCOUNT}:role/claims-dev-agentcore-exec"
    identity = MagicMock(name="AgentIdentityService")
    identity.set_runtime_environment.return_value = exec_role
    monkeypatch.setattr(agents_module, "get_identity_service", lambda: identity)

    agent = _agent()
    registry = MagicMock(name="AgentRegistryService")
    registry.persist_identity.side_effect = lambda a: a
    svc = _svc(registry=registry)
    svc._get_session = lambda: _wired_session()

    provision_langfuse_best_effort(agent, svc)

    secret_name = f"langfuse-agent-{agent.id}-keys"
    assert agent.langfuse_project_id == "clx-proj-x"
    assert agent.langfuse_key_secret_name == secret_name
    identity.set_runtime_environment.assert_called_once_with(
        agent.agent_arn,
        {"LANGFUSE_HOST": HOST, "LANGFUSE_SECRET_NAME": secret_name},
        wait_ready=False,
        control_client=None,
    )
    sm = boto3.client("secretsmanager", region_name=REGION)
    policy = json.loads(sm.get_resource_policy(SecretId=secret_name)["ResourcePolicy"])
    assert policy["Statement"][0]["Principal"] == {"AWS": exec_role}
    assert policy["Statement"][0]["Action"] == "secretsmanager:GetSecretValue"
    # SECRET-SAFETY: no key value anywhere on the envelope, still.
    assert "sk-x" not in json.dumps(agent.to_envelope())


# ===========================================================================
# E36/T16 (routed here from T13's review) — the TEARDOWN half of the account seam.
#
# `delete_agent_project` deleted the per-agent secret with the AMBIENT client, i.e. always in
# the CONTROL-PLANE account. For a cross-account agent that is a truthful answer to the wrong
# question: Secrets Manager says "not found" here, the swallow reads it as already-gone, and
# the cascade reports `deleted` while the tenant account keeps the secret. Same defect class
# as the runtimes and exec roles T8 fixed.
#
# The contract these pin:
#   - a resolvable cross-account stage ⇒ the delete goes through `stage_client`, and an
#     AssumeRole failure PROPAGATES (teardown must never fall back to ambient — the
#     `tenant_credentials` module rule);
#   - an UNRESOLVABLE account ⇒ the ambient delete is still attempted, but a "not found"
#     there proves nothing, so it is reported honestly instead of as `deleted`;
#   - a real ambient delete still counts as deleted even when the account was unresolvable —
#     the secret demonstrably went.
# ===========================================================================
@mock_aws
def test_delete_uses_the_stage_client_for_a_cross_account_agent():
    agent = _cross_agent(
        langfuse_project_id="clx-live",
        langfuse_key_secret_name="langfuse-agent-rec-abc123-keys",
    )
    tenants = _tenants(
        {
            "dev": _stage(account_id=PLATFORM_ACCOUNT, deploy_role_arn=PLATFORM_ROLE),
            "prod": _stage(account_id=TENANT_ACCOUNT, deploy_role_arn=TENANT_ROLE),
        }
    )
    tenant_sm = _tenant_sm()
    svc = _svc(tenants=tenants)
    svc._stage_client = MagicMock(name="stage_client", return_value=tenant_sm)
    session = MagicMock(name="requests.Session")
    session.post.return_value = _resp({"result": {"data": {"json": {"success": True}}}})
    svc._get_session = lambda: session

    assert svc.delete_agent_project(agent) is None

    call = svc._stage_client.call_args
    assert call.args[0] == "secretsmanager"
    assert call.args[1].deploy_role_arn == TENANT_ROLE
    assert call.kwargs == {"session_suffix": f"lf-{agent.id[:8]}"}
    tenant_sm.delete_secret.assert_called_once_with(
        SecretId="langfuse-agent-rec-abc123-keys", ForceDeleteWithoutRecovery=True
    )


@mock_aws
def test_delete_propagates_an_assume_role_failure_instead_of_going_ambient():
    """A failed assume means we never addressed the account that owns the secret. Falling
    back to ambient here is what produced the false ``deleted`` in the first place."""
    from services.tenant_credentials import TenantCredentialsError

    agent = _cross_agent(
        langfuse_project_id="clx-live",
        langfuse_key_secret_name="langfuse-agent-rec-abc123-keys",
    )
    tenants = _tenants({"prod": _stage(account_id=TENANT_ACCOUNT, deploy_role_arn=TENANT_ROLE)})
    svc = _svc(tenants=tenants)
    svc._stage_client = MagicMock(
        side_effect=TenantCredentialsError("agp-deployment-acme-prod (ClientError)")
    )
    session = MagicMock(name="requests.Session")
    session.post.return_value = _resp({"result": {"data": {"json": {"success": True}}}})
    svc._get_session = lambda: session

    with pytest.raises(TenantCredentialsError):
        svc.delete_agent_project(agent)

    # The Langfuse PROJECT delete was still attempted first (it is account-agnostic).
    session.post.assert_called_once()


@mock_aws
def test_delete_reports_honestly_when_the_owning_account_is_unresolvable():
    """No tenant stage owns the agent's runtime account ⇒ the ambient NotFound is not
    evidence of anything. Raise so the cascade line-item is ``failed``, never ``deleted``."""
    from services.langfuse_provisioning import LangfuseAccountUnresolvedError

    agent = _cross_agent(
        langfuse_project_id="clx-live",
        langfuse_key_secret_name="langfuse-agent-rec-abc123-keys",
    )
    tenants = _tenants({})  # no stages at all ⇒ nothing owns that account
    svc = _svc(tenants=tenants)
    svc._stage_client = MagicMock(side_effect=AssertionError("must not assume"))
    # A MagicMock ambient client carrying the REAL exception classes: moto's delete_secret
    # SUCCEEDS on a missing secret when ForceDeleteWithoutRecovery is set, so it cannot serve
    # the not-found shape real Secrets Manager answers with.
    ambient = _tenant_sm()
    ambient.delete_secret.side_effect = ambient.exceptions.ResourceNotFoundException(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, "DeleteSecret"
    )
    svc._sm = ambient
    session = MagicMock(name="requests.Session")
    session.post.return_value = _resp({"result": {"data": {"json": {"success": True}}}})
    svc._get_session = lambda: session

    with pytest.raises(LangfuseAccountUnresolvedError):
        svc.delete_agent_project(agent)

    # The ambient delete WAS attempted (it is the only account we can reach) ...
    ambient.delete_secret.assert_called_once()
    # ... and the Langfuse PROJECT delete happened first, account-agnostic.
    session.post.assert_called_once()


@mock_aws
def test_delete_counts_as_deleted_when_an_unresolved_secret_is_actually_here():
    """The account could not be resolved, but the ambient delete DID remove a secret — that
    is proof, so it is a success and not a reported failure."""
    secret_name = "langfuse-agent-rec-abc123-keys"
    sm = boto3.client("secretsmanager", region_name=REGION)
    sm.create_secret(Name=secret_name, SecretString=json.dumps({"public_key": "pk"}))

    agent = _cross_agent(langfuse_project_id="clx-live", langfuse_key_secret_name=secret_name)
    svc = _svc(tenants=None)  # no tenant service ⇒ unresolvable
    session = MagicMock(name="requests.Session")
    session.post.return_value = _resp({"result": {"data": {"json": {"success": True}}}})
    svc._get_session = lambda: session

    assert svc.delete_agent_project(agent) is None

    with pytest.raises(sm.exceptions.ResourceNotFoundException):
        sm.get_secret_value(SecretId=secret_name)


@mock_aws
def test_delete_creation_path_resolution_is_unchanged_for_a_same_account_agent():
    """Regression guard on the refactor: ``_stage_cfg_for`` (the CREATION helper) still
    answers None for a same-account agent without consulting the tenant store."""
    agent = _agent()  # arn account == moto's ambient account
    agent.tenant_id = "ten-acme"
    tenants = _tenants({"dev": _stage(account_id=PLATFORM_ACCOUNT, deploy_role_arn=PLATFORM_ROLE)})
    svc = _svc(tenants=tenants)

    assert svc._stage_cfg_for(agent) is None
    tenants.get.assert_not_called()
