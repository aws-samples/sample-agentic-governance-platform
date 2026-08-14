"""Tests for the account-global GitHub OIDC provider bootstrap.

Mirrors ``test_ecr_push_role_service``: moto's mocked IAM so get-or-create runs against
real IAM semantics without touching live AWS, plus stubs for the two paths moto cannot
produce (a create-create race, and AccessDenied).
"""

from __future__ import annotations

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from services.ecr_push_role_service import EcrPushRoleError, EcrPushRoleService
from services.github_oidc_provider_service import (
    GITHUB_OIDC_PROVIDER_HOST,
    GITHUB_OIDC_PROVIDER_URL,
    GitHubOidcProviderError,
    GitHubOidcProviderService,
    github_oidc_provider_arn,
    resolve_github_oidc_provider_arn,
)

ACCOUNT = "123456789012"
PROVIDER_ARN = f"arn:aws:iam::{ACCOUNT}:oidc-provider/{GITHUB_OIDC_PROVIDER_HOST}"
ECR_ARN = "arn:aws:ecr:us-east-1:123456789012:repository/agp-agent-images"
PREFIX = "agp-cp-dev"


def _svc(iam=None, provider_arn: str = PROVIDER_ARN):
    return GitHubOidcProviderService(
        provider_arn=provider_arn,
        region="us-east-1",
        iam_client=iam or boto3.client("iam", region_name="us-east-1"),
    )


def _iam_client_error(code: str, op: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


class _StubIam:
    """Minimal IAM stub: scripted responses for the calls the service makes.

    STATEFUL ACROSS THE RACE, because the service now re-GETs after
    ``EntityAlreadyExists`` and a stub that kept answering ``NoSuchEntity`` there would be
    modelling an impossible account (a create that lost to an existing object, on an object
    that does not exist). ``race_client_ids`` is therefore the WINNER'S audience list — what
    the racing creator put on the singleton — and it is what the loser reads back.

    ``get_client_ids`` is the audience on the pre-existing (adoption-path) object.
    """

    def __init__(
        self,
        *,
        get_error=None,
        create_error=None,
        get_client_ids=("sts.amazonaws.com",),
        race_client_ids=("sts.amazonaws.com",),
    ):
        self._get_error = get_error
        self._create_error = create_error
        self._get_client_ids = list(get_client_ids)
        self._race_client_ids = list(race_client_ids)
        self.creates = 0
        self.gets = 0
        self._raced = False

    def get_open_id_connect_provider(self, **kwargs):
        self.gets += 1
        if self._raced:
            # Post-race: the WINNER's object, with the winner's audience list.
            return {"Url": GITHUB_OIDC_PROVIDER_HOST, "ClientIDList": self._race_client_ids}
        if self._get_error:
            raise self._get_error
        return {"Url": GITHUB_OIDC_PROVIDER_HOST, "ClientIDList": self._get_client_ids}

    def create_open_id_connect_provider(self, **kwargs):
        self.creates += 1
        if self._create_error:
            if self._create_error.response["Error"]["Code"] == "EntityAlreadyExists":
                self._raced = True
            raise self._create_error
        return {"OpenIDConnectProviderArn": PROVIDER_ARN}


# --------------------------------------------------------------------------- #
# deterministic ARN + config gate
# --------------------------------------------------------------------------- #


def test_provider_arn_is_deterministic_from_the_account_id():
    """The whole design rests on this: Terraform can pass the ARN as a plain STRING because
    an OIDC-provider ARN has no random component, so it is knowable before the object
    exists. If this ever stopped being derivable, the ECS env var would need a resource
    reference again and the fresh-apply guarantee would be gone."""
    assert github_oidc_provider_arn(ACCOUNT) == PROVIDER_ARN
    assert github_oidc_provider_arn(ACCOUNT).endswith(f"/{GITHUB_OIDC_PROVIDER_HOST}")


def test_inert_when_unconfigured():
    """No ARN ⇒ inert: ensure returns None without calling IAM."""
    svc = _svc(iam=object(), provider_arn="")
    assert svc.enabled is False
    assert svc.ensure_provider() is None


# --------------------------------------------------------------------------- #
# resolve_github_oidc_provider_arn — the config fallback
# --------------------------------------------------------------------------- #


def test_resolve_prefers_the_configured_env_value():
    """A configured ARN wins and STS is never called (passing a client that would explode
    proves the call is not made)."""
    configured = "arn:aws:iam::999999999999:oidc-provider/token.actions.githubusercontent.com"
    assert resolve_github_oidc_provider_arn(configured, sts_client=object()) == configured


def test_resolve_derives_from_sts_when_env_is_empty():
    class _Sts:
        def get_caller_identity(self):
            return {"Account": ACCOUNT}

    assert resolve_github_oidc_provider_arn("", sts_client=_Sts()) == PROVIDER_ARN


def test_resolve_returns_empty_when_sts_fails_rather_than_raising():
    """An STS failure must leave the dependent services INERT, not break wiring: a
    partially-configured env is allowed to permit connections (it just falls back to the
    platform-default push role), which raising here would turn into a hard failure."""

    class _Sts:
        def get_caller_identity(self):
            raise _iam_client_error("AccessDenied", "GetCallerIdentity")

    assert resolve_github_oidc_provider_arn("", sts_client=_Sts()) == ""


# --------------------------------------------------------------------------- #
# ensure_provider
# --------------------------------------------------------------------------- #


@mock_aws
def test_ensure_provider_creates_it_when_missing():
    iam = boto3.client("iam", region_name="us-east-1")
    svc = _svc(iam)
    arn = svc.ensure_provider()

    assert arn == PROVIDER_ARN
    got = iam.get_open_id_connect_provider(OpenIDConnectProviderArn=PROVIDER_ARN)
    # IAM stores the URL without the scheme; the create call must still send the https form.
    assert got["Url"] in (GITHUB_OIDC_PROVIDER_HOST, GITHUB_OIDC_PROVIDER_URL)
    assert got["ClientIDList"] == ["sts.amazonaws.com"]
    # Thumbprints are required by the API even though AWS ignores them (root-CA validation).
    assert got["ThumbprintList"]


@mock_aws
def test_ensure_provider_is_a_noop_when_it_already_exists():
    """The common case — an account that already onboarded GitHub Actions, or the second
    connection. One read, nothing created."""
    iam = boto3.client("iam", region_name="us-east-1")
    iam.create_open_id_connect_provider(
        Url=GITHUB_OIDC_PROVIDER_URL, ClientIDList=["sts.amazonaws.com"], ThumbprintList=["ab" * 20]
    )
    svc = _svc(iam)
    assert svc.ensure_provider() == PROVIDER_ARN
    # Still exactly one provider — no second one, and IAM would have rejected it anyway.
    assert len(iam.list_open_id_connect_providers()["OpenIDConnectProviderList"]) == 1


@mock_aws
def test_ensure_provider_is_idempotent_across_calls():
    iam = boto3.client("iam", region_name="us-east-1")
    svc = _svc(iam)
    assert svc.ensure_provider() == svc.ensure_provider() == PROVIDER_ARN
    assert len(iam.list_open_id_connect_providers()["OpenIDConnectProviderList"]) == 1


def test_ensure_provider_treats_a_create_create_race_as_success():
    """Two connections bootstrapping at once: both GET NoSuchEntity, both CREATE, one loses
    with EntityAlreadyExists. The singleton now exists — which is what was asked for — and
    its ARN is deterministic, so the loser already holds the right answer."""
    iam = _StubIam(
        get_error=_iam_client_error("NoSuchEntity", "GetOpenIDConnectProvider"),
        create_error=_iam_client_error("EntityAlreadyExists", "CreateOpenIDConnectProvider"),
    )
    assert _svc(iam).ensure_provider() == PROVIDER_ARN
    assert iam.creates == 1


# --------------------------------------------------------------------------- #
# I1 — the ADOPTED / RACED provider's audience is VERIFIED, never assumed.
#
# Not a trust hole (fail-closed: STS rejects a token whose `aud` is not in the provider's
# client-ID list, so nothing extra is trusted and pushes simply stop). It is the
# "record says success, reality disagrees" class: the bootstrap logged "provider ensured",
# the connection read CONNECTED, and every push then failed with an opaque
# `Not authorized to perform sts:AssumeRoleWithWebIdentity`.
#
# AGP cannot REPAIR it — no `iam:AddClientIDToOpenIDConnectProvider` in the grant, which is
# deliberate and is the tightest fence in the whole change (with it, a compromised backend
# could add an attacker-chosen `aud` to the account's GitHub trust anchor). So detection is
# the entire remedy, and it must NAME the object an operator has to go fix.
# --------------------------------------------------------------------------- #


@mock_aws
def test_an_adopted_provider_missing_the_sts_audience_is_REFUSED_not_reported_ready():
    """The account already has a GitHub provider some other stack created, with a
    ``ClientIDList`` that does NOT include ``sts.amazonaws.com``. Every AGP push role becomes
    unassumable. The bootstrap must refuse rather than log success."""
    iam = boto3.client("iam", region_name="us-east-1")
    iam.create_open_id_connect_provider(
        Url=GITHUB_OIDC_PROVIDER_URL,
        ClientIDList=["some-other-audience"],
        ThumbprintList=["ab" * 20],
    )
    with pytest.raises(GitHubOidcProviderError) as exc:
        _svc(iam).ensure_provider()
    # The message must be ACTIONABLE: the exact provider ARN (the object to fix) and the
    # audience that is missing. Without both, the operator gets the same opaque dead end the
    # STS denial already gave them.
    assert PROVIDER_ARN in exc.value.message
    assert "sts.amazonaws.com" in exc.value.message
    # And AGP must NOT have tried to repair it — it holds no AddClientID grant, so an attempt
    # would fail with AccessDenied and report the wrong cause.
    got = iam.get_open_id_connect_provider(OpenIDConnectProviderArn=PROVIDER_ARN)
    assert got["ClientIDList"] == ["some-other-audience"], "the provider must be left as found"


def test_a_RACED_provider_missing_the_sts_audience_is_ALSO_refused():
    """The other adoption door. Losing the create-create race means adopting the WINNER's
    object — and the winner may not be AGP, so its audience is re-read rather than assumed.
    ``ensure_provider`` used to return the ARN here without ever looking at the object."""
    iam = _StubIam(
        get_error=_iam_client_error("NoSuchEntity", "GetOpenIDConnectProvider"),
        create_error=_iam_client_error("EntityAlreadyExists", "CreateOpenIDConnectProvider"),
        race_client_ids=["some-other-audience"],
    )
    with pytest.raises(GitHubOidcProviderError) as exc:
        _svc(iam).ensure_provider()
    assert PROVIDER_ARN in exc.value.message
    assert "sts.amazonaws.com" in exc.value.message
    # The re-GET is what makes the check possible: GET (NoSuchEntity) → CREATE (race) → GET.
    assert iam.gets == 2, iam.gets
    assert iam.creates == 1


def test_a_post_race_read_failure_reports_the_READ_failure_not_a_missing_audience():
    """A denied re-GET must not be folded into "audience missing" — that names an
    unactionable cause for what is really an IAM read problem, on a path where the object
    demonstrably exists."""

    class _RaceThenDeniedGet(_StubIam):
        def get_open_id_connect_provider(self, **kwargs):
            self.gets += 1
            if self._raced:
                raise _iam_client_error("AccessDenied", "GetOpenIDConnectProvider")
            raise self._get_error

    iam = _RaceThenDeniedGet(
        get_error=_iam_client_error("NoSuchEntity", "GetOpenIDConnectProvider"),
        create_error=_iam_client_error("EntityAlreadyExists", "CreateOpenIDConnectProvider"),
    )
    with pytest.raises(GitHubOidcProviderError) as exc:
        _svc(iam).ensure_provider()
    assert "read" in exc.value.message.lower()
    assert "audience" not in exc.value.message.lower()
    assert "AccessDenied" not in exc.value.message  # no ClientError text leaks


@mock_aws
def test_the_provider_AGP_creates_itself_needs_no_audience_reproach():
    """The create path knows its own audience by construction, so the check must not turn a
    healthy fresh bootstrap into a failure."""
    iam = boto3.client("iam", region_name="us-east-1")
    assert _svc(iam).ensure_provider() == PROVIDER_ARN
    assert iam.get_open_id_connect_provider(OpenIDConnectProviderArn=PROVIDER_ARN)[
        "ClientIDList"
    ] == ["sts.amazonaws.com"]


@mock_aws
def test_an_adopted_provider_with_EXTRA_audiences_is_accepted():
    """Only the PRESENCE of ``sts.amazonaws.com`` is required. An account that also federates
    GitHub Actions to another audience for its own reasons is not AGP's business to refuse —
    and refusing would make AGP unusable on a perfectly working account."""
    iam = boto3.client("iam", region_name="us-east-1")
    iam.create_open_id_connect_provider(
        Url=GITHUB_OIDC_PROVIDER_URL,
        ClientIDList=["another-audience", "sts.amazonaws.com"],
        ThumbprintList=["ab" * 20],
    )
    assert _svc(iam).ensure_provider() == PROVIDER_ARN


def test_ensure_provider_surfaces_a_clear_error_when_create_is_denied():
    """A missing IAM grant must be LOUD, not silently swallowed into "no provider" — that is
    the shape that let earlier IAM gaps hide behind clean reports. The message is SAFE (no
    ClientError text)."""
    iam = _StubIam(
        get_error=_iam_client_error("NoSuchEntity", "GetOpenIDConnectProvider"),
        create_error=_iam_client_error("AccessDenied", "CreateOpenIDConnectProvider"),
    )
    with pytest.raises(GitHubOidcProviderError) as exc:
        _svc(iam).ensure_provider()
    assert exc.value.kind == "iam_error"
    assert "AccessDenied" not in exc.value.message


def test_ensure_provider_surfaces_a_clear_error_when_the_read_is_denied():
    """A denied GET is NOT "absent" — treating it as absent would drive a create that is
    also denied, reporting the wrong cause."""
    iam = _StubIam(get_error=_iam_client_error("AccessDenied", "GetOpenIDConnectProvider"))
    with pytest.raises(GitHubOidcProviderError):
        _svc(iam).ensure_provider()
    assert iam.creates == 0


def test_the_service_has_no_delete_path_for_the_account_global_singleton():
    """Deliberate asymmetry, asserted so it is not "fixed" into symmetry with the per-org
    roles. The provider is account-global: anything else in the account that trusts GitHub
    Actions breaks the moment it is deleted, so one AGP disconnect must not be able to
    remove it."""
    assert not [n for n in dir(GitHubOidcProviderService) if "delete" in n.lower()]


# --------------------------------------------------------------------------- #
# the shared push role that moved out of Terraform with the provider
# --------------------------------------------------------------------------- #


def _role_svc(iam=None):
    return EcrPushRoleService(
        role_name_prefix=PREFIX,
        oidc_provider_arn=PROVIDER_ARN,
        ecr_repository_arn=ECR_ARN,
        region="us-east-1",
        iam_client=iam or boto3.client("iam", region_name="us-east-1"),
    )


def test_shared_role_name_matches_the_terraform_resource_it_adopts():
    """The name is load-bearing, not cosmetic. `modules/agent_ecr` created
    `"${var.name_prefix}-agent-ecr-push"`, and `ECR_PUSH_ROLE_NAME_PREFIX` is that same
    `name_prefix`. If this drifts, an already-deployed account gets a SECOND role while the
    live one (forgotten by the `removed` block, so never destroyed) is orphaned — and the
    repos still pointing at the old ARN keep pushing through a role nothing maintains."""
    assert _role_svc(iam=object()).shared_role_name() == f"{PREFIX}-agent-ecr-push"
    # And it must NOT collide with the per-org pattern the ECS policy scopes separately.
    assert not _role_svc(iam=object()).shared_role_name().startswith(f"{PREFIX}-ecr-push-")


@mock_aws
def test_ensure_shared_role_creates_it_with_the_ported_terraform_policy():
    """Policy parity with the retired Terraform resource: GetAuthorizationToken on `*` plus
    the seven push actions scoped to the shared agent-images repo ARN."""
    iam = boto3.client("iam", region_name="us-east-1")
    arn = _role_svc(iam).ensure_shared_role("AWS-AIOPS")

    assert arn.endswith(f":role/{PREFIX}-agent-ecr-push")
    doc = iam.get_role_policy(RoleName=f"{PREFIX}-agent-ecr-push", PolicyName="ecr-push")[
        "PolicyDocument"
    ]
    push = [s for s in doc["Statement"] if s["Action"] != "ecr:GetAuthorizationToken"][0]
    assert push["Resource"] == ECR_ARN
    assert set(push["Action"]) == {
        "ecr:BatchCheckLayerAvailability",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
    }
    auth = [s for s in doc["Statement"] if s["Action"] == "ecr:GetAuthorizationToken"][0]
    assert auth["Resource"] == "*"
    # Trust names the OIDC provider — the create-time dependency that forced the move.
    trust = iam.get_role(RoleName=f"{PREFIX}-agent-ecr-push")["Role"]["AssumeRolePolicyDocument"]
    assert trust["Statement"][0]["Principal"]["Federated"] == PROVIDER_ARN


@mock_aws
def test_ensure_shared_role_adopts_an_existing_role_without_retrusting_it():
    """The adoption case (every already-deployed account). The live role's trust is left
    EXACTLY as found: this role is SHARED across orgs but a trust names ONE org, so
    refreshing it per connection would make the last connect win and silently revoke the
    previous org's fallback. Only the org-independent inline policy converges."""
    iam = boto3.client("iam", region_name="us-east-1")
    svc = _role_svc(iam)
    svc.ensure_shared_role("AgenticOps-Platform")
    before = iam.get_role(RoleName=f"{PREFIX}-agent-ecr-push")["Role"][
        "AssumeRolePolicyDocument"
    ]

    # A second connection, different org.
    svc.ensure_shared_role("AWS-AIOPS")
    after = iam.get_role(RoleName=f"{PREFIX}-agent-ecr-push")["Role"]["AssumeRolePolicyDocument"]
    assert after == before, "an existing shared role's trust must not be rewritten"
    assert len([r for r in iam.list_roles()["Roles"] if r["RoleName"].endswith("-agent-ecr-push")]) == 1


@mock_aws
def test_ensure_shared_role_is_inert_when_unconfigured():
    svc = EcrPushRoleService(
        role_name_prefix="", oidc_provider_arn="", ecr_repository_arn="", iam_client=object()
    )
    assert svc.ensure_shared_role("AWS-AIOPS") is None


def test_ensure_shared_role_surfaces_a_clear_error_when_iam_denies():
    class _Denied:
        def create_role(self, **kwargs):
            raise _iam_client_error("AccessDenied", "CreateRole")

    with pytest.raises(EcrPushRoleError) as exc:
        _role_svc(_Denied()).ensure_shared_role("AWS-AIOPS")
    assert exc.value.kind == "iam_error"
    assert "AccessDenied" not in exc.value.message
