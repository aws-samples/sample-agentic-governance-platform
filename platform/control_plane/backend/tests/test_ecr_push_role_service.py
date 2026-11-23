"""Tests for the per-org GitHub-OIDC ECR-push role lifecycle.

Uses moto's mocked IAM (like test_connection_service mocks Secrets Manager) so create/
update/delete are exercised against real IAM semantics — idempotency, trust-policy sub
scoping, and the inline ECR policy — without touching live AWS.
"""

from __future__ import annotations

import json

import pytest
from moto.core.decorator import mock_aws

from core import aws_clients
from services.ecr_push_role_service import EcrPushRoleError, EcrPushRoleService

OIDC_ARN = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
ECR_ARN = "arn:aws:ecr:us-east-1:123456789012:repository/agp-agent-images"
PREFIX = "agp-cp-dev"


def _svc(iam=None):
    return EcrPushRoleService(
        role_name_prefix=PREFIX,
        oidc_provider_arn=OIDC_ARN,
        ecr_repository_arn=ECR_ARN,
        region="us-east-1",
        iam_client=iam or aws_clients.client("iam", region="us-east-1"),
    )


def test_role_name_is_prefixed_and_sanitized():
    svc = _svc(iam=object())
    assert svc.role_name("AWS-AIOPS") == "agp-cp-dev-ecr-push-AWS-AIOPS"
    assert svc.role_name("acme corp/x") == "agp-cp-dev-ecr-push-acme-corp-x"


def test_role_name_bounded_to_64_chars():
    svc = _svc(iam=object())
    long_org = "o" * 100
    assert len(svc.role_name(long_org)) == 64


def test_inert_when_unconfigured():
    """Missing any wiring value ⇒ inert: ensure returns None, delete no-ops (no IAM call)."""
    svc = EcrPushRoleService(
        role_name_prefix="", oidc_provider_arn="", ecr_repository_arn="", iam_client=object()
    )
    assert svc.enabled is False
    assert svc.ensure_role("AWS-AIOPS") is None
    svc.delete_role("AWS-AIOPS")


@mock_aws
def test_ensure_role_creates_role_with_scoped_trust_and_ecr_policy():
    iam = aws_clients.client("iam", region="us-east-1")
    svc = _svc(iam)
    arn = svc.ensure_role("AWS-AIOPS")

    assert arn is not None
    assert arn.endswith(":role/agp-cp-dev-ecr-push-AWS-AIOPS")
    role = iam.get_role(RoleName="agp-cp-dev-ecr-push-AWS-AIOPS")["Role"]
    trust = role["AssumeRolePolicyDocument"]
    cond = trust["Statement"][0]["Condition"]
    assert cond["StringLike"]["token.actions.githubusercontent.com:sub"] == [
        "repo:AWS-AIOPS/*:*",
        "repo:AWS-AIOPS@*/*:*",
    ]
    assert cond["StringEquals"]["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"

    pol = iam.get_role_policy(RoleName="agp-cp-dev-ecr-push-AWS-AIOPS", PolicyName="ecr-push")
    doc = pol["PolicyDocument"]
    push_stmt = next(s for s in doc["Statement"] if s["Action"] != "ecr:GetAuthorizationToken")
    assert push_stmt["Resource"] == ECR_ARN
    assert "ecr:PutImage" in push_stmt["Action"]


@mock_aws
def test_ensure_role_is_idempotent():
    """A second ensure for the same org converges (no EntityAlreadyExists error)."""
    iam = aws_clients.client("iam", region="us-east-1")
    svc = _svc(iam)
    arn1 = svc.ensure_role("AWS-AIOPS")
    arn2 = svc.ensure_role("AWS-AIOPS")
    assert arn1 == arn2
    names = [r["RoleName"] for r in iam.list_roles()["Roles"]]
    assert names.count("agp-cp-dev-ecr-push-AWS-AIOPS") == 1


@mock_aws
def test_two_orgs_get_distinct_roles():
    iam = aws_clients.client("iam", region="us-east-1")
    svc = _svc(iam)
    a = svc.ensure_role("AgenticOps-Platform")
    b = svc.ensure_role("AWS-AIOPS")
    assert a != b
    for org in ("AgenticOps-Platform", "AWS-AIOPS"):
        role = iam.get_role(RoleName=f"agp-cp-dev-ecr-push-{org}")["Role"]
        sub = role["AssumeRolePolicyDocument"]["Statement"][0]["Condition"]["StringLike"][
            "token.actions.githubusercontent.com:sub"
        ]
        assert sub == [f"repo:{org}/*:*", f"repo:{org}@*/*:*"]


@mock_aws
def test_trust_sub_covers_immutable_id_subject_customization():
    """Orgs with immutable-ID subject customization mint OIDC subs shaped
    ``repo:<org>@<orgId>/<repo>@<repoId>:...`` — the trust must accept that form too,
    while the ``@*`` sitting BETWEEN org and ``/`` keeps sibling orgs
    (``repo:<org>-evil/...``) out of the boundary."""
    import fnmatch

    iam = aws_clients.client("iam", region="us-east-1")
    svc = _svc(iam)
    org = "AgenticOps-Platform"
    svc.ensure_role(org)
    role = iam.get_role(RoleName=f"agp-cp-dev-ecr-push-{org}")["Role"]
    patterns = role["AssumeRolePolicyDocument"]["Statement"][0]["Condition"]["StringLike"][
        "token.actions.githubusercontent.com:sub"
    ]

    immutable_sub = f"repo:{org}@296866902/my-test-agent@1302865558:ref:refs/heads/main"
    assert any(fnmatch.fnmatch(immutable_sub, p) for p in patterns)
    standard_sub = f"repo:{org}/my-test-agent:ref:refs/heads/main"
    assert any(fnmatch.fnmatch(standard_sub, p) for p in patterns)
    sibling_sub = f"repo:{org}-evil/my-test-agent:ref:refs/heads/main"
    assert not any(fnmatch.fnmatch(sibling_sub, p) for p in patterns)


@mock_aws
def test_delete_role_removes_role_and_policy():
    iam = aws_clients.client("iam", region="us-east-1")
    svc = _svc(iam)
    svc.ensure_role("AWS-AIOPS")
    svc.delete_role("AWS-AIOPS")
    with pytest.raises(iam.exceptions.NoSuchEntityException):
        iam.get_role(RoleName="agp-cp-dev-ecr-push-AWS-AIOPS")


@mock_aws
def test_delete_role_is_idempotent_when_absent():
    """Deleting a never-provisioned org's role is a no-op, not an error."""
    iam = aws_clients.client("iam", region="us-east-1")
    svc = _svc(iam)
    svc.delete_role("never-existed")


@mock_aws
def test_ensure_then_delete_then_reensure_roundtrips():
    iam = aws_clients.client("iam", region="us-east-1")
    svc = _svc(iam)
    svc.ensure_role("AWS-AIOPS")
    svc.delete_role("AWS-AIOPS")
    arn = svc.ensure_role("AWS-AIOPS")
    assert arn is not None
    assert arn.endswith(":role/agp-cp-dev-ecr-push-AWS-AIOPS")


_WILDCARD_ORGS = [
    pytest.param("*", id="star_trusts_anyone"),
    pytest.param("a/*", id="escapes_the_org_segment"),
    pytest.param("a:b", id="escapes_into_the_ref_segment"),
    pytest.param("?", id="iam_single_char_wildcard"),
    pytest.param("*/*", id="star_slash_star"),
    pytest.param("acme*", id="prefix_widens_to_sibling_orgs"),
    pytest.param("-acme", id="leading_hyphen"),
    pytest.param("acme-", id="trailing_hyphen"),
    pytest.param("", id="empty"),
    pytest.param("a" * 40, id="over_the_39_char_login_limit"),
    pytest.param("acme\n", id="trailing_newline_only_fullmatch_rejects"),
]


@pytest.mark.parametrize("org", _WILDCARD_ORGS)
@mock_aws
def test_the_SERVICE_refuses_a_trust_widening_org_and_creates_NOTHING(org):
    """Asserted on BOTH halves: it raises, AND no role exists afterwards. "Raises" alone
    would pass for an implementation that created the role and then failed."""
    iam = aws_clients.client("iam", region="us-east-1")
    svc = _svc(iam)
    with pytest.raises(EcrPushRoleError) as exc:
        svc.ensure_role(org)
    assert exc.value.kind == "bad_request"
    assert iam.list_roles()["Roles"] == [], "a rejected org must leave NO role behind"


@pytest.mark.parametrize("org", _WILDCARD_ORGS)
@mock_aws
def test_the_SHARED_role_path_refuses_the_same_orgs(org):
    """`ensure_shared_role` is the other door to `_trust_policy`, and it is the one the
    reviewer proved reachable: the shared role's trust is NEVER rewritten once created, so a
    wildcard that lands there is PERMANENT — no later connect corrects it and there is no
    recovery path in the code. Both doors, one check."""
    iam = aws_clients.client("iam", region="us-east-1")
    svc = _svc(iam)
    with pytest.raises(EcrPushRoleError) as exc:
        svc.ensure_shared_role(org)
    assert exc.value.kind == "bad_request"
    assert iam.list_roles()["Roles"] == []


@mock_aws
def test_a_wildcard_org_can_never_produce_the_repo_star_trust_the_docstring_FORBIDS():
    """The finding stated as the property it protects, not as a call that raises.

    This module's docstring says a single `repo:*/*:*` trust "would let ANY GitHub repo that
    learns the ARN push to our ECR". This asserts that string can be produced by NO org this
    service will accept — a stronger claim than "org='*' raises", and one that survives a
    future refactor of where the check lives."""
    iam = aws_clients.client("iam", region="us-east-1")
    svc = _svc(iam)
    for org in ("*", "*/*", "?", "a/*"):
        with pytest.raises(EcrPushRoleError):
            svc._trust_policy(org)
    doc = svc._trust_policy("AgenticOps-Platform")
    subs = json.loads(doc)["Statement"][0]["Condition"]["StringLike"][
        "token.actions.githubusercontent.com:sub"
    ]
    assert subs == ["repo:AgenticOps-Platform/*:*", "repo:AgenticOps-Platform@*/*:*"]
    assert "repo:*/" not in doc


@mock_aws
def test_legitimate_orgs_still_pass_at_the_service_layer():
    """The fence must not be a wall. Real GitHub logins — hyphenated, mixed-case, numeric,
    single-character, and the 39-char maximum — all still provision."""
    iam = aws_clients.client("iam", region="us-east-1")
    svc = _svc(iam)
    for org in ("a", "A1", "AWS-AIOPS", "AgenticOps-Platform", "a" * 39):
        assert svc.ensure_role(org) is not None, org


@mock_aws
def test_an_inert_service_never_reaches_the_org_check():
    """Ordering, pinned: the config gate comes FIRST, so an unconfigured env keeps returning
    None instead of starting to raise `bad_request` on inputs it previously ignored. That
    would turn a partially-configured deployment's harmless no-op into a connection failure —
    the exact rule (`a partially-configured env never blocks a connection`) this service is
    built around."""
    svc = EcrPushRoleService(
        role_name_prefix="", oidc_provider_arn="", ecr_repository_arn="", iam_client=object()
    )
    assert svc.ensure_role("*") is None
    assert svc.ensure_shared_role("*") is None


def test_the_service_regex_IS_the_model_regex_so_the_two_layers_cannot_drift():
    """One pattern, two enforcement points — asserted by IDENTITY, not by behaviour.

    Two independently-maintained copies of a security regex diverge, and the divergence is
    silent: the model would keep rejecting `*` while the service quietly started accepting
    something. This is why the service IMPORTS the model's pattern."""
    from models.connection import ORG_LOGIN_RE
    from services import ecr_push_role_service

    assert ecr_push_role_service.ORG_LOGIN_RE is ORG_LOGIN_RE


def test_the_default_kind_is_iam_error_and_the_message_is_kept():
    err = EcrPushRoleError("Failed to ensure the shared ECR-push role")

    assert (err.kind, err.message, str(err)) == (
        "iam_error",
        "Failed to ensure the shared ECR-push role",
        "Failed to ensure the shared ECR-push role",
    )
