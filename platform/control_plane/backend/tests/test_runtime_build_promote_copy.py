"""Unit tests for the E25C/T6b cross-account image-copy env injection.

T6 made a prod promote reuse the SAME image tag with no rebuild — but the image was
pushed only to the DEV-stage ECR. For a cross-account tenant the prod ECR is a different
repo in a different account, so `start_runtime_build` must inject SOURCE_* overrides that
tell the buildspec to copy `dev_ecr:tag` -> `prod_ecr:tag` before provisioning. Single-
account (dev.account_id == prod.account_id) and any dev build must inject NONE of them.

Asserts the injection matrix over a mocked codebuild.start_build (mirrors the fakes in
test_runtime_build_service.py):
  - cross-account prod promote -> SOURCE_ECR_REPO/SOURCE_DEPLOY_ROLE_ARN/SOURCE_REGION
    present with the DEV-stage values;
  - single-account prod promote (dev.account_id == prod.account_id) -> SOURCE_* absent;
  - stage=dev -> SOURCE_* absent.
"""

from types import SimpleNamespace

from models.tenant import TenantStageConfig
from services.runtime_build_service import RuntimeBuildService

_SCRATCH_PREFIX = "agp-test/runtime-build-token/"
_SOURCE_KEYS = ("SOURCE_ECR_REQUIRED", "SOURCE_ECR_REPO", "SOURCE_DEPLOY_ROLE_ARN", "SOURCE_REGION")


class FakeCodeBuild:
    def __init__(self, *, build_id="build-123:abc"):
        self.build_id = build_id
        self.calls = []

    def start_build(self, **kwargs):
        self.calls.append(kwargs)
        return {"build": {"id": self.build_id}}


class _ResourceExistsException(Exception):
    pass


class _FakeSMExceptions:
    ResourceExistsException = _ResourceExistsException


class FakeSecretsManager:
    def __init__(self):
        self.writes = []
        self.exceptions = _FakeSMExceptions()

    def _arn(self, name):
        return f"arn:aws:secretsmanager:us-east-1:111:secret:{name}-AbCdEf"

    def create_secret(self, *, Name, SecretString, Tags=None):
        self.writes.append((Name, SecretString))
        return {"ARN": self._arn(Name)}

    def put_secret_value(self, *, SecretId, SecretString):
        self.writes.append((SecretId, SecretString))
        return {"ARN": self._arn(SecretId)}


class FakeConnectionService:
    def __init__(self, *, bearer_token="tok-ready"):
        self._bearer = bearer_token

    def get_connection(self, id):
        return SimpleNamespace(org="acme-org", base_url=None)

    def get_bearer_token(self, connection_id):
        return self._bearer


class FakeAgentRegistry:
    """`registry_id` is part of the collaborator interface, not decoration: the backend now
    supplies AGENT_REGISTRY_ID to CodeBuild as a per-build override (it resolves the registry
    by NAME, so Terraform no longer publishes the id at all)."""

    def __init__(self, agent, registry_id="REGFROMSERVICE"):
        self.agent = agent
        self.registry_id = registry_id

    def get(self, agent_id):
        return self.agent


class FakeTenantService:
    def __init__(self, tenant):
        self.tenant = tenant

    def get(self, tenant_id):
        return self.tenant


def _agent(tenant_id="ten-x"):
    return SimpleNamespace(tenant_id=tenant_id)


def _tenant(stages):
    return SimpleNamespace(stages=stages)


def _svc(tenant):
    cb = FakeCodeBuild()
    svc = RuntimeBuildService(
        cb, FakeConnectionService(), FakeSecretsManager(),
        tenant_service=FakeTenantService(tenant), agent_registry=FakeAgentRegistry(_agent()),
        codebuild_project_name="p", scratch_secret_prefix=_SCRATCH_PREFIX,
    )
    return svc, cb


def _env_map(kwargs):
    return {e["name"]: e["value"] for e in kwargs["environmentVariablesOverride"]}


def _cross_account_stages():
    return {
        "dev": TenantStageConfig(
            account_id="111111111111", region="us-east-1",
            ecr_repo_uri="111.dkr.ecr.us-east-1.amazonaws.com/agp-agents",
            deploy_role_arn="agp-deployment-dev",
        ),
        "prod": TenantStageConfig(
            account_id="222222222222", region="eu-central-1",
            ecr_repo_uri="222.dkr.ecr.eu-central-1.amazonaws.com/agp-agents",
            deploy_role_arn="agp-deployment-prod",
        ),
    }


def _single_account_stages():
    return {
        "dev": TenantStageConfig(
            account_id="111111111111", region="us-east-1",
            ecr_repo_uri="111.dkr.ecr.us-east-1.amazonaws.com/agp-agents",
            deploy_role_arn="agp-deployment-dev",
        ),
        "prod": TenantStageConfig(
            account_id="111111111111", region="us-east-1",
            ecr_repo_uri="111.dkr.ecr.us-east-1.amazonaws.com/agp-agents",
            deploy_role_arn="agp-deployment-prod",
        ),
    }


def test_cross_account_prod_promote_injects_source_vars_with_dev_values():
    svc, cb = _svc(_tenant(_cross_account_stages()))
    svc.start_runtime_build(
        agent_id="ag-1", image_tag="ag-1-abc", ecr_repo="req-ecr",
        connection_id="conn-1", stage="prod",
    )
    env = _env_map(cb.calls[0])
    # REQUIRED is the signal-vs-action bridge: it fires on the cross-account promote.
    assert env["SOURCE_ECR_REQUIRED"] == "true"
    assert env["SOURCE_ECR_REPO"] == "111.dkr.ecr.us-east-1.amazonaws.com/agp-agents"
    assert env["SOURCE_DEPLOY_ROLE_ARN"] == "agp-deployment-dev"
    assert env["SOURCE_REGION"] == "us-east-1"
    # ECR_REPO stays the PROD-stage target (copy destination).
    assert env["ECR_REPO"] == "222.dkr.ecr.eu-central-1.amazonaws.com/agp-agents"


def test_cross_account_prod_promote_signals_required_even_when_dev_ecr_empty():
    """The whole point of SOURCE_ECR_REQUIRED: account_id is the signal, so the REQUIRED
    flag fires even when dev never built (ecr_repo_uri empty). SOURCE_ECR_REPO is injected
    empty → the buildspec sees REQUIRED=true + empty repo and fails LOUD at build time
    instead of silently provisioning prod against a nonexistent image."""
    stages = {
        "dev": TenantStageConfig(
            account_id="111111111111", region="us-east-1",
            ecr_repo_uri="",  # dev never built
            deploy_role_arn="agp-deployment-dev",
        ),
        "prod": TenantStageConfig(
            account_id="222222222222", region="eu-central-1",
            ecr_repo_uri="222.dkr.ecr.eu-central-1.amazonaws.com/agp-agents",
            deploy_role_arn="agp-deployment-prod",
        ),
    }
    svc, cb = _svc(_tenant(stages))
    svc.start_runtime_build(
        agent_id="ag-1", image_tag="ag-1-abc", ecr_repo="req-ecr",
        connection_id="conn-1", stage="prod",
    )
    env = _env_map(cb.calls[0])
    assert env["SOURCE_ECR_REQUIRED"] == "true"
    assert env["SOURCE_ECR_REPO"] == ""  # empty → buildspec fails loud, never provisions


def test_single_account_prod_promote_injects_no_source_vars():
    svc, cb = _svc(_tenant(_single_account_stages()))
    svc.start_runtime_build(
        agent_id="ag-1", image_tag="ag-1-abc", ecr_repo="req-ecr",
        connection_id="conn-1", stage="prod",
    )
    env = _env_map(cb.calls[0])
    assert all(k not in env for k in _SOURCE_KEYS)


def test_dev_build_injects_no_source_vars():
    svc, cb = _svc(_tenant(_cross_account_stages()))
    svc.start_runtime_build(
        agent_id="ag-1", image_tag="ag-1-abc", ecr_repo="req-ecr",
        connection_id="conn-1", stage="dev",
    )
    env = _env_map(cb.calls[0])
    assert all(k not in env for k in _SOURCE_KEYS)


def test_cross_account_prod_promote_missing_dev_stage_is_no_copy():
    """A prod-only tenant (no 'dev' key) must not crash the build request; treat as no-copy."""
    stages = {
        "prod": TenantStageConfig(
            account_id="222222222222", region="eu-central-1",
            ecr_repo_uri="222.dkr.ecr.eu-central-1.amazonaws.com/agp-agents",
            deploy_role_arn="agp-deployment-prod",
        ),
    }
    svc, cb = _svc(_tenant(stages))
    build_id = svc.start_runtime_build(
        agent_id="ag-1", image_tag="ag-1-abc", ecr_repo="req-ecr",
        connection_id="conn-1", stage="prod",
    )
    assert build_id == cb.build_id
    env = _env_map(cb.calls[0])
    assert all(k not in env for k in _SOURCE_KEYS)
