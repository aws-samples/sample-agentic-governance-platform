"""E32 guard: the ECS task role must grant agent-registry:* and RETAIN workload identity.

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
    """Runtime/Identity/Gateway/Cedar stayed in `bedrock-agentcore` — E32 must not have
    turned the ADD into a replace."""
    assert '"bedrock-agentcore:*"' in ecs_tf


@pytest.mark.parametrize("tf_path", [CODEBUILD_TF, DEFAULT_TENANT_TF])
def test_provisioning_roles_grant_both_namespaces(tf_path):
    """The CodeBuild role and the tenant deploy role both touch registry records
    (buildspec.yml get-registry-record / update-registry-record), so each needs the new
    namespace beside the retained one."""
    text = tf_path.read_text()
    assert '"bedrock-agentcore:*"' in text, tf_path.name
    assert '"agent-registry:*"' in text, tf_path.name


# ---------------------------------------------------------------------------
# E36/T21: the `agp-deployment-*` deploy-role naming contract, three sides.
#
# The role NAME (modules/default_tenant) and the sts:AssumeRole RESOURCE wildcards
# (modules/codebuild, and modules/ecs since E36/T11) are ONE contract — the name pattern
# IS the authorization boundary. Rename one side alone and the first symptom is a live
# AccessDenied inside a CodeBuild run (provisioning) or a failed teardown, both invisible
# to the whole offline suite. So the sides are EXTRACTED and compared to each other, not
# spot-checked independently.
#
# The E34/T13b rename that CREATED this contract (the retired resource prefix -> `agp-`) needs
# no separate `not in` assertion here, and deliberately gets none: pinning the extracted prefix
# to `DEPLOY_ROLE_PREFIX` below already fails on a revert of ANY side, and
# `tests/test_no_legacy_vocabulary.py` independently forbids the retired prefix across the whole
# tracked tree — these three `.tf` files included, which is strictly stronger than a local
# substring check. Writing the old literal here would only trip that guard.
# ---------------------------------------------------------------------------

DEPLOY_ROLE_PREFIX = "agp-deployment-"

# `name = "agp-deployment-${var.name_prefix}-default"` — the shape side. The interpolation
# is matched loosely (`${...}`) because WHICH variable carries the per-tenant suffix is not
# part of this contract; only the prefix in front of it is.
DEPLOY_ROLE_NAME_RE = re.compile(
    r'name\s*=\s*"(?P<prefix>[a-z0-9-]+?-)\$\{[^}]+\}-default"'
)
# `"arn:aws:iam::*:role/agp-deployment-*"` — the grant side.
ASSUMABLE_ROLE_ARN_RE = re.compile(r'"arn:aws:iam::\*:role/(?P<prefix>[a-z0-9-]+?-)\*"')
# A commented-out ARN grants nothing, so it must not satisfy the grant side: widening the
# real `Resource` to `role/*` while leaving the old value as a `#` note would otherwise pass.
HCL_COMMENT = re.compile(r"^\s*(#|//).*$", re.MULTILINE)


@pytest.fixture(scope="module")
def codebuild_tf() -> str:
    return CODEBUILD_TF.read_text()


@pytest.fixture(scope="module")
def default_tenant_tf() -> str:
    return DEFAULT_TENANT_TF.read_text()


def test_deploy_role_name_matches_every_assume_grant(default_tenant_tf, codebuild_tf, ecs_tf):
    """The prefix the deploy-role is NAMED with must be the prefix each assuming role is
    GRANTED — compared side-to-side, so a one-sided rename cannot pass."""
    named = DEPLOY_ROLE_NAME_RE.findall(default_tenant_tf)
    # Exactly one match: the list-equality also pins uniqueness, so a SECOND `…-default`
    # role name would fail here rather than silently widening the boundary.
    assert named == [DEPLOY_ROLE_PREFIX], (
        str(DEFAULT_TENANT_TF.relative_to(INFRA)),
        DEPLOY_ROLE_NAME_RE.pattern,
        named,
    )
    for label, text in (("codebuild", codebuild_tf), ("ecs", ecs_tf)):
        granted = ASSUMABLE_ROLE_ARN_RE.findall(HCL_COMMENT.sub("", text))
        assert named[0] in granted, (label, named[0], granted)
