"""Tests for the per-agent Langfuse provisioner.

Executable spec for the public methods on ``LangfuseProvisioningService``:
``provision_agent_project`` (create project + mint key + store in Secrets Manager +
write the join onto the agent envelope; idempotent) and ``delete_agent_project``
(best-effort/idempotent teardown), plus the best-effort register-time hook.

ALL external side effects are mocked — there are NO live Langfuse / AWS calls:
  - the internal tRPC HTTP → the ``httpx.Client`` returned by ``_get_session`` is a
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

import pytest
from moto.core.decorator import mock_aws

from core import aws_clients
from models.agent import Agent, AgentAuthType, LifecycleState, Platform
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
        auth_type=AgentAuthType.ENTRA,
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


@mock_aws
def test_provision_creates_project_and_writes_envelope():
    agent = _agent()
    registry = MagicMock(name="AgentRegistryService")
    registry.persist_identity.side_effect = lambda a: a

    svc = _svc(registry=registry)
    session = MagicMock(name="httpx.Client")
    session.post.side_effect = [
        _resp({"result": {"data": {"json": {"id": "clx-proj-1"}}}}),
        _resp({"result": {"data": {"json": {"publicKey": "pk-lf-abc", "secretKey": "sk-lf-xyz"}}}}),
    ]
    svc._get_session = lambda: session

    result = svc.provision_agent_project(agent)

    expected_secret = f"langfuse-agent-{agent.id}-keys"
    assert result == {
        "project_id": "clx-proj-1",
        "secret_name": expected_secret,
        "public_key": "pk-lf-abc",
    }

    assert session.post.call_count == 2
    create_call = session.post.call_args_list[0]
    assert create_call.args[0].endswith("/api/trpc/projects.create")
    assert create_call.kwargs["json"] == {"json": {"name": "agp-Claims Triage DE", "orgId": "seed-org"}}

    sm = aws_clients.client("secretsmanager", region=REGION)
    body = json.loads(sm.get_secret_value(SecretId=expected_secret)["SecretString"])
    assert body == {
        "public_key": "pk-lf-abc",
        "secret_key": "sk-lf-xyz",
        "project_name": "agp-Claims Triage DE",
        "project_id": "clx-proj-1",
    }

    assert agent.langfuse_project_id == "clx-proj-1"
    assert agent.langfuse_key_secret_name == expected_secret
    registry.persist_identity.assert_called_once_with(agent)

    envelope = agent.to_envelope()
    assert "sk-lf-xyz" not in json.dumps(envelope)
    assert "pk-lf-abc" not in json.dumps(envelope)
    assert envelope["langfuse_key_secret_name"] == expected_secret
    assert envelope["langfuse_project_id"] == "clx-proj-1"


@mock_aws
def test_provision_is_idempotent():
    secret_name = "langfuse-agent-rec-abc123-keys"
    sm = aws_clients.client("secretsmanager", region=REGION)
    sm.create_secret(
        Name=secret_name,
        SecretString=json.dumps(
            {
                "public_key": "pk-lf-old",
                "secret_key": "sk-lf-old",
                "project_name": "agp-Claims Triage DE",
                "project_id": "clx-existing",
            }
        ),
    )
    agent = _agent(langfuse_project_id="clx-existing", langfuse_key_secret_name=secret_name)
    registry = MagicMock(name="AgentRegistryService")

    svc = _svc(registry=registry)
    session = MagicMock(name="httpx.Client")
    svc._get_session = lambda: session

    result = svc.provision_agent_project(agent)

    assert result["project_id"] == "clx-existing"
    assert result["secret_name"] == secret_name
    assert result["public_key"] == "pk-lf-old"
    session.post.assert_not_called()
    registry.persist_identity.assert_not_called()


@mock_aws
def test_provision_remints_when_secret_missing():
    secret_name = "langfuse-agent-rec-abc123-keys"
    agent = _agent(langfuse_project_id="clx-existing", langfuse_key_secret_name=secret_name)
    registry = MagicMock(name="AgentRegistryService")
    registry.persist_identity.side_effect = lambda a: a

    svc = _svc(registry=registry)
    session = MagicMock(name="httpx.Client")
    session.post.side_effect = [
        _resp({"result": {"data": {"json": {"id": "clx-existing"}}}}),
        _resp({"result": {"data": {"json": {"publicKey": "pk-lf-new", "secretKey": "sk-lf-new"}}}}),
    ]
    svc._get_session = lambda: session

    result = svc.provision_agent_project(agent)

    assert result == {
        "project_id": "clx-existing",
        "secret_name": secret_name,
        "public_key": "pk-lf-new",
    }
    assert session.post.call_count == 2
    sm = aws_clients.client("secretsmanager", region=REGION)
    body = json.loads(sm.get_secret_value(SecretId=secret_name)["SecretString"])
    assert body["public_key"] == "pk-lf-new"
    assert body["secret_key"] == "sk-lf-new"
    assert body["project_id"] == "clx-existing"


def test_provision_failure_is_non_blocking(monkeypatch):
    import api.routes.agents as agents_module
    from api.routes.agents import provision_langfuse_best_effort

    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", HOST)

    agent = _agent()
    service = MagicMock(name="LangfuseProvisioningService")
    service.provision_agent_project.side_effect = RuntimeError("tRPC create failed")

    provision_langfuse_best_effort(agent, service)

    service.provision_agent_project.assert_called_once_with(agent)
    assert agent.langfuse_project_id is None
    assert agent.langfuse_key_secret_name is None


@mock_aws
def test_delete_agent_project_idempotent():
    agent = _agent(
        langfuse_project_id="clx-gone",
        langfuse_key_secret_name="langfuse-agent-rec-abc123-keys",
    )
    svc = _svc()
    session = MagicMock(name="httpx.Client")
    session.post.side_effect = RuntimeError("404 project not found")
    svc._get_session = lambda: session

    assert svc.delete_agent_project(agent) is None
    session.post.assert_called_once()
    assert session.post.call_args.args[0].endswith("/api/trpc/projects.delete")


@mock_aws
def test_delete_agent_project_deletes_project_and_secret():
    secret_name = "langfuse-agent-rec-abc123-keys"
    sm = aws_clients.client("secretsmanager", region=REGION)
    sm.create_secret(
        Name=secret_name,
        SecretString=json.dumps(
            {
                "public_key": "pk-lf-x",
                "secret_key": "sk-lf-x",
                "project_name": "agp-Claims Triage DE",
                "project_id": "clx-live",
            }
        ),
    )
    agent = _agent(langfuse_project_id="clx-live", langfuse_key_secret_name=secret_name)

    svc = _svc()
    session = MagicMock(name="httpx.Client")
    session.post.return_value = _resp({"result": {"data": {"json": {"success": True}}}})
    svc._get_session = lambda: session

    assert svc.delete_agent_project(agent) is None

    session.post.assert_called_once()
    delete_call = session.post.call_args
    assert delete_call.args[0].endswith("/api/trpc/projects.delete")
    assert delete_call.kwargs["json"] == {"json": {"projectId": "clx-live"}}

    with pytest.raises(sm.exceptions.ResourceNotFoundException):
        sm.get_secret_value(SecretId=secret_name)


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

    return TenantStageConfig(account_id=account_id, region=REGION, deploy_role_arn=deploy_role_arn)


def _tenants(stages: dict):
    """A duck-typed TenantService whose ``get`` answers a record carrying ``stages``."""
    from types import SimpleNamespace

    svc = MagicMock(name="TenantService")
    svc.get.return_value = SimpleNamespace(id="ten-acme", stages=stages)
    return svc


def _tenant_sm():
    """A MagicMock secretsmanager client for the tenant account, carrying the REAL botocore
    exception classes (an ``except MagicMock().exceptions.X`` would be a TypeError)."""
    ambient = aws_clients.client("secretsmanager", region=REGION)
    fake = MagicMock(name="tenant-account-secretsmanager")
    fake.exceptions.ResourceNotFoundException = ambient.exceptions.ResourceNotFoundException
    fake.exceptions.ResourceExistsException = ambient.exceptions.ResourceExistsException
    return fake


def _wired_session() -> MagicMock:
    session = MagicMock(name="httpx.Client")
    session.post.side_effect = [
        _resp({"result": {"data": {"json": {"id": "clx-proj-x"}}}}),
        _resp({"result": {"data": {"json": {"publicKey": "pk-x", "secretKey": "sk-x"}}}}),
    ]
    return session


@mock_aws
def test_same_account_agent_keeps_the_ambient_client():
    """The account is parsed from ``agent_arn``; equal to the platform account ⇒ today's path.

    The tenant lookup is asserted NOT to happen: the short-circuit must precede it, so a tenant
    store outage cannot change how a single-account agent is provisioned.
    """
    agent = _agent()
    agent.tenant_id = "ten-acme"
    tenants = _tenants({"dev": _stage(account_id=PLATFORM_ACCOUNT, deploy_role_arn=PLATFORM_ROLE)})
    svc = _svc(tenants=tenants)
    svc._stage_client = MagicMock(name="stage_client", side_effect=AssertionError("assumed"))
    svc._get_session = lambda: _wired_session()

    result = svc.provision_agent_project(agent)

    assert result["secret_name"] == f"langfuse-agent-{agent.id}-keys"
    svc._stage_client.assert_not_called()
    tenants.get.assert_not_called()
    sm = aws_clients.client("secretsmanager", region=REGION)
    assert (
        json.loads(sm.get_secret_value(SecretId=result["secret_name"])["SecretString"])["public_key"]
        == "pk-x"
    )


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

    svc._stage_client.assert_called_once()
    call = svc._stage_client.call_args
    assert call.args[0] == "secretsmanager"
    assert call.args[1].deploy_role_arn == TENANT_ROLE
    assert call.kwargs == {"session_suffix": f"lf-{agent.id[:8]}"}

    tenant_sm.create_secret.assert_called_once()
    assert tenant_sm.create_secret.call_args.kwargs["Name"] == result["secret_name"]
    sm = aws_clients.client("secretsmanager", region=REGION)
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
    for record in caplog.records:
        assert "999988887777" not in record.getMessage()
    sm = aws_clients.client("secretsmanager", region=REGION)
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

    sm = aws_clients.client("secretsmanager", region=REGION)
    assert sm.get_secret_value(SecretId=result["secret_name"])["SecretString"]
    assert any("[langfuse]" in r.message for r in caplog.records)


@mock_aws
def test_a_failed_platform_account_probe_is_retried_for_the_next_agent(caplog, monkeypatch):
    """One transient ``sts:GetCallerIdentity`` blip must NOT pin the process.

    The probe flag used to be set BEFORE the call, so a single failure cached "platform account
    unknown" on the singleton for its whole lifetime and every cross-account agent registered
    afterwards silently got its secret in the platform account, where its runtime cannot read it —
    with ONE warning at process start rather than one per affected agent. Two registrations here:

      1. the probe fails ⇒ a per-agent WARNING that NAMES the degraded agent + the ambient path;
      2. the next agent probes AGAIN (2 STS attempts total), succeeds, and the cross-account path
         works — the secret is created through ``stage_client`` in the tenant account.
    """
    tenants = _tenants({"prod": _stage(account_id=TENANT_ACCOUNT, deploy_role_arn=TENANT_ROLE)})
    tenant_sm = _tenant_sm()
    svc = _svc(tenants=tenants)
    svc._stage_client = MagicMock(name="stage_client", return_value=tenant_sm)
    svc._get_session = lambda: _wired_session()

    real_client = aws_clients.client
    sts_attempts = []

    def _client(service_name, *args, **kwargs):
        if service_name == "sts":
            sts_attempts.append(service_name)
            if len(sts_attempts) == 1:
                raise RuntimeError("transient STS blip")
        return real_client(service_name, *args, **kwargs)

    monkeypatch.setattr(aws_clients, "client", _client)

    first = _cross_agent(agent_id="rec-probe-fail")
    with caplog.at_level(logging.WARNING):
        result_one = svc.provision_agent_project(first)

    svc._stage_client.assert_not_called()
    assert any(
        "[langfuse]" in r.getMessage() and first.id in r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING"
    )
    for record in caplog.records:
        assert TENANT_ACCOUNT not in record.getMessage()
    sm = aws_clients.client("secretsmanager", region=REGION)
    assert sm.get_secret_value(SecretId=result_one["secret_name"])["SecretString"]

    second = _cross_agent(agent_id="rec-probe-ok")
    result_two = svc.provision_agent_project(second)

    assert len(sts_attempts) == 2
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
    svc = _svc()
    svc._stage_client = MagicMock(name="stage_client", side_effect=AssertionError("assumed"))
    svc._get_session = lambda: _wired_session()

    result = svc.provision_agent_project(agent)

    svc._stage_client.assert_not_called()
    sm = aws_clients.client("secretsmanager", region=REGION)
    assert sm.get_secret_value(SecretId=result["secret_name"])["SecretString"]


@mock_aws
def test_wire_agent_runtime_injects_env_and_grants_read():
    secret_name = "langfuse-agent-rec-abc123-keys"
    sm = aws_clients.client("secretsmanager", region=REGION)
    sm.create_secret(Name=secret_name, SecretString=json.dumps({"public_key": "pk"}))
    agent = _agent()
    exec_role = f"arn:aws:iam::{PLATFORM_ACCOUNT}:role/claims-dev-agentcore-exec"
    identity = MagicMock(name="AgentIdentityService")
    identity.set_runtime_environment.return_value = exec_role

    svc = _svc()
    svc.wire_agent_runtime(agent, secret_name, identity)

    identity.set_runtime_environment.assert_called_once_with(
        agent.agent_arn,
        {"LANGFUSE_HOST": HOST, "LANGFUSE_SECRET_NAME": secret_name},
        wait_ready=False,
        control_client=None,
    )

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

    service.wire_agent_runtime.assert_called_once_with(agent, "langfuse-agent-rec-abc123-keys", identity)


def test_register_hook_does_not_wire_when_provisioning_failed(monkeypatch):
    import api.routes.agents as agents_module
    from api.routes.agents import provision_langfuse_best_effort

    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", HOST)
    monkeypatch.setattr(agents_module, "get_identity_service", lambda: MagicMock())

    agent = _agent()
    service = MagicMock(name="LangfuseProvisioningService")
    service.provision_agent_project.side_effect = RuntimeError("tRPC create failed")

    provision_langfuse_best_effort(agent, service)

    service.wire_agent_runtime.assert_not_called()


def test_register_hook_provisions_but_skips_wiring_when_trace_delivery_manual(monkeypatch):
    """ "manual" gates ONLY the OTLP env-injection leg.

    The Langfuse project + key are still provisioned (``provision_agent_project`` called); only
    ``wire_agent_runtime`` — the leg that pushes the OTLP env onto the runtime — is skipped.
    """
    import api.routes.agents as agents_module
    from api.routes.agents import provision_langfuse_best_effort

    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", HOST)
    monkeypatch.setattr(agents_module, "get_identity_service", lambda: MagicMock())

    agent = _agent()
    agent.trace_delivery = "manual"
    service = MagicMock(name="LangfuseProvisioningService")
    service.provision_agent_project.return_value = {
        "project_id": "clx-1",
        "secret_name": "langfuse-agent-rec-abc123-keys",
        "public_key": "pk",
    }

    provision_langfuse_best_effort(agent, service)

    service.provision_agent_project.assert_called_once_with(agent)
    service.wire_agent_runtime.assert_not_called()


def test_register_hook_skips_wiring_on_a_non_auto_capable_platform(monkeypatch):
    """a platform whose declaration has ``trace_auto_delivery=False`` (Databricks)
    provisions the project but never gets the OTLP env injected — even with the default "auto"."""
    import api.routes.agents as agents_module
    from api.routes.agents import provision_langfuse_best_effort

    monkeypatch.setattr(agents_module.settings, "LANGFUSE_HOST", HOST)
    monkeypatch.setattr(agents_module, "get_identity_service", lambda: MagicMock())

    agent = _agent()
    agent.platform = Platform.DATABRICKS
    service = MagicMock(name="LangfuseProvisioningService")
    service.provision_agent_project.return_value = {
        "project_id": "clx-1",
        "secret_name": "langfuse-agent-rec-abc123-keys",
        "public_key": "pk",
    }

    provision_langfuse_best_effort(agent, service)

    service.provision_agent_project.assert_called_once_with(agent)
    service.wire_agent_runtime.assert_not_called()


def test_trace_delivery_round_trips_through_the_envelope():
    """the choice survives to_envelope → from_record; absent ⇒ None (⇒ auto)."""

    def _native(agent: Agent) -> dict:
        return {
            "recordId": agent.id,
            "name": agent.name,
            "description": agent.purpose or "x",
            "status": "APPROVED",
            "createdAt": agent.created_at,
            "updatedAt": agent.updated_at,
        }

    agent = _agent()
    assert agent.trace_delivery is None
    legacy = agent.to_envelope()
    legacy.pop("trace_delivery")
    assert Agent.from_record(_native(agent), legacy).trace_delivery is None

    agent.trace_delivery = "manual"
    assert agent.to_envelope()["trace_delivery"] == "manual"
    assert Agent.from_record(_native(agent), agent.to_envelope()).trace_delivery == "manual"


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
    sm = aws_clients.client("secretsmanager", region=REGION)
    policy = json.loads(sm.get_resource_policy(SecretId=secret_name)["ResourcePolicy"])
    assert policy["Statement"][0]["Principal"] == {"AWS": exec_role}
    assert policy["Statement"][0]["Action"] == "secretsmanager:GetSecretValue"
    assert "sk-x" not in json.dumps(agent.to_envelope())


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
    session = MagicMock(name="httpx.Client")
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
    session = MagicMock(name="httpx.Client")
    session.post.return_value = _resp({"result": {"data": {"json": {"success": True}}}})
    svc._get_session = lambda: session

    with pytest.raises(TenantCredentialsError):
        svc.delete_agent_project(agent)

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
    tenants = _tenants({})
    svc = _svc(tenants=tenants)
    svc._stage_client = MagicMock(side_effect=AssertionError("must not assume"))
    ambient = _tenant_sm()
    ambient.delete_secret.side_effect = ambient.exceptions.ResourceNotFoundException(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, "DeleteSecret"
    )
    svc._sm = ambient
    session = MagicMock(name="httpx.Client")
    session.post.return_value = _resp({"result": {"data": {"json": {"success": True}}}})
    svc._get_session = lambda: session

    with pytest.raises(LangfuseAccountUnresolvedError):
        svc.delete_agent_project(agent)

    ambient.delete_secret.assert_called_once()
    session.post.assert_called_once()


@mock_aws
def test_delete_counts_as_deleted_when_an_unresolved_secret_is_actually_here():
    """The account could not be resolved, but the ambient delete DID remove a secret — that
    is proof, so it is a success and not a reported failure."""
    secret_name = "langfuse-agent-rec-abc123-keys"
    sm = aws_clients.client("secretsmanager", region=REGION)
    sm.create_secret(Name=secret_name, SecretString=json.dumps({"public_key": "pk"}))

    agent = _cross_agent(langfuse_project_id="clx-live", langfuse_key_secret_name=secret_name)
    svc = _svc(tenants=None)
    session = MagicMock(name="httpx.Client")
    session.post.return_value = _resp({"result": {"data": {"json": {"success": True}}}})
    svc._get_session = lambda: session

    assert svc.delete_agent_project(agent) is None

    with pytest.raises(sm.exceptions.ResourceNotFoundException):
        sm.get_secret_value(SecretId=secret_name)


@mock_aws
def test_delete_creation_path_resolution_is_unchanged_for_a_same_account_agent():
    """Regression guard on the refactor: ``_stage_cfg_for`` (the CREATION helper) still
    answers None for a same-account agent without consulting the tenant store."""
    agent = _agent()
    agent.tenant_id = "ten-acme"
    tenants = _tenants({"dev": _stage(account_id=PLATFORM_ACCOUNT, deploy_role_arn=PLATFORM_ROLE)})
    svc = _svc(tenants=tenants)

    assert svc._stage_cfg_for(agent) is None
    tenants.get.assert_not_called()
