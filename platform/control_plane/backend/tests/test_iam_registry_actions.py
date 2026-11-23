"""The ECS task role must grant agent-registry:* and retain workload identity.

AWS moved ONLY Registry to the `agent-registry` namespace. bedrock-agentcore:* no longer
authorizes registry calls, but workload identity + OAuth credential providers deliberately
stay in the old namespace — so this is an ADD, never a replace.

These are static assertions over the Terraform SOURCE TEXT, in the spirit of
`test_buildspec_contract.py`, because the defect they guard is invisible to every
behavioural test: both new registry clients sign as `agent-registry`, so a policy that
grants only `bedrock-agentcore*` AccessDenies every registry call at runtime while the
whole offline suite stays green. No mock can catch that; only the IAM text can.
"""

import re
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parents[2] / "infrastructure"

ECS_TF = INFRA / "modules" / "ecs" / "main.tf"
CODEBUILD_TF = INFRA / "modules" / "codebuild" / "main.tf"
DEFAULT_TENANT_TF = INFRA / "modules" / "default_tenant" / "main.tf"


@pytest.fixture(scope="module")
def ecs_tf() -> str:
    return ECS_TF.read_text()


def test_grants_agent_registry_namespace(ecs_tf):
    assert '"agent-registry:*"' in ecs_tf


def test_retains_workload_identity_in_old_namespace(ecs_tf):
    for action in (
        "bedrock-agentcore:CreateWorkloadIdentity",
        "bedrock-agentcore:GetWorkloadIdentity",
        "bedrock-agentcore:DeleteWorkloadIdentity",
    ):
        assert action in ecs_tf, action


def test_drops_dead_control_plane_prefix(ecs_tf):
    """`bedrock-agentcore-control:` was never a real IAM prefix; registry authz is
    now `agent-registry:`, so the belt-and-suspenders block is dead weight."""
    assert "bedrock-agentcore-control:CreateRegistry" not in ecs_tf


def test_drops_renamed_search_action(ecs_tf):
    """`SearchRegistryRecords` was renamed `SearchDiscoverableRegistryRecords` and moved
    to the data plane, so the old action name grants nothing under either namespace."""
    assert "SearchRegistryRecords" not in ecs_tf


def test_retains_broad_agentcore_wildcard(ecs_tf):
    """Runtime/Identity/Gateway/Cedar stayed in `bedrock-agentcore`; the registry move is an add,
    never a replace."""
    assert '"bedrock-agentcore:*"' in ecs_tf


@pytest.mark.parametrize("tf_path", [CODEBUILD_TF, DEFAULT_TENANT_TF])
def test_provisioning_roles_grant_both_namespaces(tf_path):
    """The CodeBuild role and the tenant deploy role both touch registry records
    (buildspec.yml get-registry-record / update-registry-record), so each needs the new
    namespace beside the retained one."""
    text = tf_path.read_text()
    assert '"bedrock-agentcore:*"' in text, tf_path.name
    assert '"agent-registry:*"' in text, tf_path.name


DEPLOY_ROLE_PREFIX = "agp-deployment-"
DEPLOY_ROLE_NAME_RE = re.compile(r'name\s*=\s*"(?P<prefix>[a-z0-9-]+?-)\$\{[^}]+\}-default"')
ASSUMABLE_ROLE_ARN_RE = re.compile(r'"arn:aws:iam::\*:role/(?P<prefix>[a-z0-9-]+?-)\*"')
HCL_COMMENT = re.compile(r"^\s*(#|//).*$", re.MULTILINE)


@pytest.fixture(scope="module")
def codebuild_tf() -> str:
    return CODEBUILD_TF.read_text()


@pytest.fixture(scope="module")
def default_tenant_tf() -> str:
    return DEFAULT_TENANT_TF.read_text()


def test_deploy_role_name_matches_every_assume_grant(default_tenant_tf, codebuild_tf, ecs_tf):
    """The `agp-deployment-*` deploy-role naming contract, three sides compared to each other.

    The role name (modules/default_tenant) and the sts:AssumeRole resource wildcards (modules/codebuild,
    modules/ecs) are one contract: the name pattern is the authorization boundary, and a one-sided
    rename first shows as a live AccessDenied inside a CodeBuild run or a failed teardown, invisible to
    the offline suite. So the sides are extracted and compared, never spot-checked. `DEPLOY_ROLE_NAME_RE`
    reads the name side, `name = "<prefix>${...}-default"`, with the interpolated variable left loose
    because which one carries the per-tenant suffix is not the contract; `ASSUMABLE_ROLE_ARN_RE` reads the
    grant side, `"arn:aws:iam::*:role/<prefix>*"`, after `HCL_COMMENT` strips commented-out lines, since
    a `#` note of the old ARN beside a widened `role/*` grants nothing. The list equality against
    `DEPLOY_ROLE_PREFIX` also pins uniqueness, so a second `…-default` role name fails instead of
    widening the boundary, and a revert of any side fails here without a `not in` on the retired prefix.
    """
    named = DEPLOY_ROLE_NAME_RE.findall(default_tenant_tf)
    assert named == [DEPLOY_ROLE_PREFIX], (
        str(DEFAULT_TENANT_TF.relative_to(INFRA)),
        DEPLOY_ROLE_NAME_RE.pattern,
        named,
    )
    for label, text in (("codebuild", codebuild_tf), ("ecs", ecs_tf)):
        granted = ASSUMABLE_ROLE_ARN_RE.findall(HCL_COMMENT.sub("", text))
        assert named[0] in granted, (label, named[0], granted)


MCP_SECRET_RESOURCE = (
    '"arn:${data.aws_partition.current.partition}:secretsmanager:'
    "${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}"
    ':secret:agp-mcp/*"'
)


def test_deploy_role_reads_only_the_mcp_secret_namespace(default_tenant_tf):
    """CreateApiKeyCredentialProvider (external secret) reads the customer's secret with the caller's
    credentials, the deploy role the gateway module's provider assumes, so that role needs read on the
    reserved `agp-mcp/` namespace and only that namespace: never a wildcard action, never an inlined
    account id. Invisible offline; the first symptom would be an AccessDenied mid-apply."""
    text = HCL_COMMENT.sub("", default_tenant_tf)
    assert '"secretsmanager:GetSecretValue"' in text
    assert '"secretsmanager:DescribeSecret"' in text
    assert f"Resource = {MCP_SECRET_RESOURCE}" in text
    assert '"secretsmanager:*"' not in text
    assert not re.search(r"secretsmanager:[a-z0-9-]*:\d{12}:", text)
    for source in ("aws_caller_identity", "aws_region", "aws_partition"):
        assert f'data "{source}" "current"' in text, source
