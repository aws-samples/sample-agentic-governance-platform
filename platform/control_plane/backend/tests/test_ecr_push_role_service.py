"""Tests for the per-org GitHub-OIDC ECR-push role lifecycle (E22 multi-org bugfix).

Uses moto's mocked IAM (like test_connection_service mocks Secrets Manager) so create/
update/delete are exercised against real IAM semantics — idempotency, trust-policy sub
scoping, and the inline ECR policy — without touching live AWS.
"""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

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
        iam_client=iam or boto3.client("iam", region_name="us-east-1"),
    )


# --------------------------------------------------------------------------- #
# naming + config gate
# --------------------------------------------------------------------------- #


def test_role_name_is_prefixed_and_sanitized():
    svc = _svc(iam=object())  # no IAM call for pure naming
    assert svc.role_name("AWS-AIOPS") == "agp-cp-dev-ecr-push-AWS-AIOPS"
    # A stray unsupported char (space/slash) is replaced with '-'.
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
    svc.delete_role("AWS-AIOPS")  # must not raise / not call IAM


# --------------------------------------------------------------------------- #
# ensure_role — create + idempotent update
# --------------------------------------------------------------------------- #


@mock_aws
def test_ensure_role_creates_role_with_scoped_trust_and_ecr_policy():
    iam = boto3.client("iam", region_name="us-east-1")
    svc = _svc(iam)
    arn = svc.ensure_role("AWS-AIOPS")

    assert arn.endswith(":role/agp-cp-dev-ecr-push-AWS-AIOPS")
    role = iam.get_role(RoleName="agp-cp-dev-ecr-push-AWS-AIOPS")["Role"]
    trust = role["AssumeRolePolicyDocument"]
    cond = trust["Statement"][0]["Condition"]
    # Trust pins the OIDC sub to THIS org's repos only + the standard aud. The sub is a
    # LIST accepting both the standard shape and the immutable-ID subject-customization
    # shape (repo:<org>@<orgId>/...), so orgs with that customization can still push.
    assert cond["StringLike"]["token.actions.githubusercontent.com:sub"] == [
        "repo:AWS-AIOPS/*:*",
        "repo:AWS-AIOPS@*/*:*",
    ]
    assert cond["StringEquals"]["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"

    pol = iam.get_role_policy(RoleName="agp-cp-dev-ecr-push-AWS-AIOPS", PolicyName="ecr-push")
    doc = pol["PolicyDocument"]
    # Push actions are scoped to the shared agent-images repo ARN.
    push_stmt = [s for s in doc["Statement"] if s["Action"] != "ecr:GetAuthorizationToken"][0]
    assert push_stmt["Resource"] == ECR_ARN
    assert "ecr:PutImage" in push_stmt["Action"]


@mock_aws
def test_ensure_role_is_idempotent():
    """A second ensure for the same org converges (no EntityAlreadyExists error)."""
    iam = boto3.client("iam", region_name="us-east-1")
    svc = _svc(iam)
    arn1 = svc.ensure_role("AWS-AIOPS")
    arn2 = svc.ensure_role("AWS-AIOPS")  # must not raise
    assert arn1 == arn2
    # Exactly one role exists.
    names = [r["RoleName"] for r in iam.list_roles()["Roles"]]
    assert names.count("agp-cp-dev-ecr-push-AWS-AIOPS") == 1


@mock_aws
def test_two_orgs_get_distinct_roles():
    iam = boto3.client("iam", region_name="us-east-1")
    svc = _svc(iam)
    a = svc.ensure_role("AgenticOps-Platform")
    b = svc.ensure_role("AWS-AIOPS")
    assert a != b
    # Each trust is scoped to its own org.
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

    iam = boto3.client("iam", region_name="us-east-1")
    svc = _svc(iam)
    org = "AgenticOps-Platform"
    svc.ensure_role(org)
    role = iam.get_role(RoleName=f"agp-cp-dev-ecr-push-{org}")["Role"]
    patterns = role["AssumeRolePolicyDocument"]["Statement"][0]["Condition"]["StringLike"][
        "token.actions.githubusercontent.com:sub"
    ]

    # The immutable-ID sub (org@orgId, repo@repoId) is matched by at least one pattern.
    immutable_sub = f"repo:{org}@296866902/my-test-agent@1302865558:ref:refs/heads/main"
    assert any(fnmatch.fnmatch(immutable_sub, p) for p in patterns)
    # The standard sub still matches.
    standard_sub = f"repo:{org}/my-test-agent:ref:refs/heads/main"
    assert any(fnmatch.fnmatch(standard_sub, p) for p in patterns)
    # A SIBLING org (prefix collision) is NOT matched by any pattern — boundary stays tight.
    sibling_sub = f"repo:{org}-evil/my-test-agent:ref:refs/heads/main"
    assert not any(fnmatch.fnmatch(sibling_sub, p) for p in patterns)


# --------------------------------------------------------------------------- #
# delete_role — teardown + idempotent
# --------------------------------------------------------------------------- #


@mock_aws
def test_delete_role_removes_role_and_policy():
    iam = boto3.client("iam", region_name="us-east-1")
    svc = _svc(iam)
    svc.ensure_role("AWS-AIOPS")
    svc.delete_role("AWS-AIOPS")
    with pytest.raises(iam.exceptions.NoSuchEntityException):
        iam.get_role(RoleName="agp-cp-dev-ecr-push-AWS-AIOPS")


@mock_aws
def test_delete_role_is_idempotent_when_absent():
    """Deleting a never-provisioned org's role is a no-op, not an error."""
    iam = boto3.client("iam", region_name="us-east-1")
    svc = _svc(iam)
    svc.delete_role("never-existed")  # must not raise


@mock_aws
def test_ensure_then_delete_then_reensure_roundtrips():
    iam = boto3.client("iam", region_name="us-east-1")
    svc = _svc(iam)
    svc.ensure_role("AWS-AIOPS")
    svc.delete_role("AWS-AIOPS")
    arn = svc.ensure_role("AWS-AIOPS")  # re-connect after disconnect
    assert arn.endswith(":role/agp-cp-dev-ecr-push-AWS-AIOPS")


# --------------------------------------------------------------------------- #
# C1 — THE ORG NEVER REACHES A TRUST POLICY UNVALIDATED (defense in depth).
#
# `org` lands INSIDE the trust policy's `sub` StringLike PATTERN, where `*` and `?` are
# WILDCARD METACHARACTERS. It used to be a Terraform literal (`var.github_org`); it is now
# API request input. `org="*"` therefore mints `repo:*/*:*` — a role ANY GitHub repo on the
# internet may assume — and that role's ARN is PUBLISHED to third-party repos by design as
# the `AWS_ECR_PUSH_ROLE_ARN` Actions var. It is the write path into the ECR repo that is
# the supply-chain root for every materialized agent image.
#
# TWO LAYERS, and these tests cover the SERVICE one. The model validator
# (`ConnectionCreate.org` / `ManifestStart.org`) is the primary gate; this layer exists so a
# caller that never built a model — a script, a migration, a new route, a test helper —
# cannot bypass it. Both must be present: removing either one alone must still redden a test.
#
# IT FAILS LOUD RATHER THAN SANITIZING, unlike `_SANITIZE` on the role NAME: quietly turning
# `*` into `-` would mint a role trusting an org the operator never named, which is a
# different wrong answer rather than a safe one.
# --------------------------------------------------------------------------- #


_WILDCARD_ORGS = [
    "*",                # the attack: yields repo:*/*:* — trust-anyone
    "a/*",              # escapes the org segment
    "a:b",              # escapes into the sub's ref segment
    "?",                # IAM's single-char wildcard
    "*/*",
    "acme*",            # prefix-widens to sibling orgs (acme-evil)
    "-acme",            # leading hyphen — not a legal GitHub login
    "acme-",            # trailing hyphen
    "",                 # empty
    "a" * 40,           # over GitHub's 39-char login limit
    "acme\n",           # trailing newline — match()'s `$` tolerates it, fullmatch doesn't;
                        # would land verbatim in the trust sub and brick the shared role
]


@pytest.mark.parametrize("org", _WILDCARD_ORGS)
@mock_aws
def test_the_SERVICE_refuses_a_trust_widening_org_and_creates_NOTHING(org):
    """Asserted on BOTH halves: it raises, AND no role exists afterwards. "Raises" alone
    would pass for an implementation that created the role and then failed."""
    iam = boto3.client("iam", region_name="us-east-1")
    svc = _svc(iam)
    with pytest.raises(EcrPushRoleError) as exc:
        svc.ensure_role(org)
    # `bad_request`, not `iam_error`: nothing is wrong with IAM — the CALLER passed a value
    # that must never reach a trust document. `_ensure_ecr_push_role` swallows this on the
    # wired path (an IAM bootstrap must not fail a credential handshake), so the kind is what
    # a log reader gets; mislabelling it would send someone auditing IAM grants.
    assert exc.value.kind == "bad_request"
    assert iam.list_roles()["Roles"] == [], "a rejected org must leave NO role behind"


@pytest.mark.parametrize("org", _WILDCARD_ORGS)
@mock_aws
def test_the_SHARED_role_path_refuses_the_same_orgs(org):
    """`ensure_shared_role` is the other door to `_trust_policy`, and it is the one the
    reviewer proved reachable: the shared role's trust is NEVER rewritten once created, so a
    wildcard that lands there is PERMANENT — no later connect corrects it and there is no
    recovery path in the code. Both doors, one check."""
    iam = boto3.client("iam", region_name="us-east-1")
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
    iam = boto3.client("iam", region_name="us-east-1")
    svc = _svc(iam)
    for org in ("*", "*/*", "?", "a/*"):
        with pytest.raises(EcrPushRoleError):
            svc._trust_policy(org)
    # And the accepted path still produces a tightly-scoped pattern with no bare wildcard org.
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
    iam = boto3.client("iam", region_name="us-east-1")
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
