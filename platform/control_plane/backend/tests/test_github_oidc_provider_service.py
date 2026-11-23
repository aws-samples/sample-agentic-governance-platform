"""The account-global GitHub OIDC provider bootstrap: moto's mocked IAM so get-or-create runs
against real IAM semantics, plus stubs for the two paths moto cannot produce (a create-create race
and AccessDenied), and the shared push role that moved out of Terraform with the provider."""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError
from moto.core.decorator import mock_aws

from core import aws_clients
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
        iam_client=iam or aws_clients.client("iam", region="us-east-1"),
    )


def _iam_client_error(code: str, op: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


class _StubIam:
    """Scripted responses for the calls the service makes, stateful across the race: after
    ``EntityAlreadyExists`` the GET answers the winner's object with ``race_client_ids``;
    ``get_client_ids`` is the audience on a pre-existing (adoption-path) object."""

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


def test_provider_arn_is_deterministic_from_the_account_id():
    """Terraform passes the ARN as a plain string because an OIDC-provider ARN has no random
    component, so it is knowable before the object exists."""
    assert github_oidc_provider_arn(ACCOUNT) == PROVIDER_ARN
    assert github_oidc_provider_arn(ACCOUNT).endswith(f"/{GITHUB_OIDC_PROVIDER_HOST}")


def test_inert_when_unconfigured():
    svc = _svc(iam=object(), provider_arn="")
    assert svc.enabled is False
    assert svc.ensure_provider() is None


def test_resolve_prefers_the_configured_env_value():
    configured = "arn:aws:iam::999999999999:oidc-provider/token.actions.githubusercontent.com"
    assert resolve_github_oidc_provider_arn(configured, sts_client=object()) == configured


def test_resolve_derives_from_sts_when_env_is_empty():
    class _Sts:
        def get_caller_identity(self):
            return {"Account": ACCOUNT}

    assert resolve_github_oidc_provider_arn("", sts_client=_Sts()) == PROVIDER_ARN


def test_resolve_returns_empty_when_sts_fails_rather_than_raising():
    """A partially configured env is allowed to permit connections; an STS failure leaves the
    dependent services inert instead of breaking wiring."""

    class _Sts:
        def get_caller_identity(self):
            raise _iam_client_error("AccessDenied", "GetCallerIdentity")

    assert resolve_github_oidc_provider_arn("", sts_client=_Sts()) == ""


@mock_aws
def test_ensure_provider_creates_it_when_missing():
    iam = aws_clients.client("iam", region="us-east-1")
    svc = _svc(iam)
    arn = svc.ensure_provider()

    assert arn == PROVIDER_ARN
    got = iam.get_open_id_connect_provider(OpenIDConnectProviderArn=PROVIDER_ARN)
    assert got["Url"] in (GITHUB_OIDC_PROVIDER_HOST, GITHUB_OIDC_PROVIDER_URL)
    assert got["ClientIDList"] == ["sts.amazonaws.com"]
    assert got["ThumbprintList"]


@mock_aws
def test_ensure_provider_is_a_noop_when_it_already_exists():
    iam = aws_clients.client("iam", region="us-east-1")
    iam.create_open_id_connect_provider(
        Url=GITHUB_OIDC_PROVIDER_URL, ClientIDList=["sts.amazonaws.com"], ThumbprintList=["ab" * 20]
    )
    svc = _svc(iam)
    assert svc.ensure_provider() == PROVIDER_ARN
    assert len(iam.list_open_id_connect_providers()["OpenIDConnectProviderList"]) == 1


@mock_aws
def test_ensure_provider_is_idempotent_across_calls():
    iam = aws_clients.client("iam", region="us-east-1")
    svc = _svc(iam)
    assert svc.ensure_provider() == svc.ensure_provider() == PROVIDER_ARN
    assert len(iam.list_open_id_connect_providers()["OpenIDConnectProviderList"]) == 1


def test_ensure_provider_treats_a_create_create_race_as_success():
    iam = _StubIam(
        get_error=_iam_client_error("NoSuchEntity", "GetOpenIDConnectProvider"),
        create_error=_iam_client_error("EntityAlreadyExists", "CreateOpenIDConnectProvider"),
    )
    assert _svc(iam).ensure_provider() == PROVIDER_ARN
    assert iam.creates == 1


@mock_aws
def test_an_adopted_provider_missing_the_sts_audience_is_REFUSED_not_reported_ready():
    """The message names the provider ARN and the missing audience, the operator action AGP
    cannot perform, and the provider is left as found."""
    iam = aws_clients.client("iam", region="us-east-1")
    iam.create_open_id_connect_provider(
        Url=GITHUB_OIDC_PROVIDER_URL,
        ClientIDList=["some-other-audience"],
        ThumbprintList=["ab" * 20],
    )
    with pytest.raises(GitHubOidcProviderError) as exc:
        _svc(iam).ensure_provider()
    assert PROVIDER_ARN in exc.value.message
    assert "sts.amazonaws.com" in exc.value.message
    got = iam.get_open_id_connect_provider(OpenIDConnectProviderArn=PROVIDER_ARN)
    assert got["ClientIDList"] == ["some-other-audience"], "the provider must be left as found"


def test_a_RACED_provider_missing_the_sts_audience_is_ALSO_refused():
    """Losing the race means adopting the winner's object, so its audience is re-read: GET
    (NoSuchEntity), CREATE (race), GET."""
    iam = _StubIam(
        get_error=_iam_client_error("NoSuchEntity", "GetOpenIDConnectProvider"),
        create_error=_iam_client_error("EntityAlreadyExists", "CreateOpenIDConnectProvider"),
        race_client_ids=["some-other-audience"],
    )
    with pytest.raises(GitHubOidcProviderError) as exc:
        _svc(iam).ensure_provider()
    assert PROVIDER_ARN in exc.value.message
    assert "sts.amazonaws.com" in exc.value.message
    assert iam.gets == 2, iam.gets
    assert iam.creates == 1


def test_a_post_race_read_failure_reports_the_READ_failure_not_a_missing_audience():
    class _RaceThenDeniedGet(_StubIam):
        def get_open_id_connect_provider(self, **kwargs):
            self.gets += 1
            if self._raced:
                raise _iam_client_error("AccessDenied", "GetOpenIDConnectProvider")
            assert self._get_error is not None
            raise self._get_error

    iam = _RaceThenDeniedGet(
        get_error=_iam_client_error("NoSuchEntity", "GetOpenIDConnectProvider"),
        create_error=_iam_client_error("EntityAlreadyExists", "CreateOpenIDConnectProvider"),
    )
    with pytest.raises(GitHubOidcProviderError) as exc:
        _svc(iam).ensure_provider()
    assert "read" in exc.value.message.lower()
    assert "audience" not in exc.value.message.lower()
    assert "AccessDenied" not in exc.value.message


@mock_aws
def test_the_provider_AGP_creates_itself_needs_no_audience_reproach():
    iam = aws_clients.client("iam", region="us-east-1")
    assert _svc(iam).ensure_provider() == PROVIDER_ARN
    assert iam.get_open_id_connect_provider(OpenIDConnectProviderArn=PROVIDER_ARN)["ClientIDList"] == [
        "sts.amazonaws.com"
    ]


@mock_aws
def test_an_adopted_provider_with_EXTRA_audiences_is_accepted():
    """Only the presence of ``sts.amazonaws.com`` is required; an account that also federates
    GitHub Actions to another audience is not AGP's business to refuse."""
    iam = aws_clients.client("iam", region="us-east-1")
    iam.create_open_id_connect_provider(
        Url=GITHUB_OIDC_PROVIDER_URL,
        ClientIDList=["another-audience", "sts.amazonaws.com"],
        ThumbprintList=["ab" * 20],
    )
    assert _svc(iam).ensure_provider() == PROVIDER_ARN


def test_ensure_provider_surfaces_a_clear_error_when_create_is_denied():
    """A missing IAM grant is loud, not swallowed into "no provider", and the message carries no
    ``ClientError`` text."""
    iam = _StubIam(
        get_error=_iam_client_error("NoSuchEntity", "GetOpenIDConnectProvider"),
        create_error=_iam_client_error("AccessDenied", "CreateOpenIDConnectProvider"),
    )
    with pytest.raises(GitHubOidcProviderError) as exc:
        _svc(iam).ensure_provider()
    assert exc.value.kind == "iam_error"
    assert "AccessDenied" not in exc.value.message


def test_ensure_provider_surfaces_a_clear_error_when_the_read_is_denied():
    """A denied GET is not "absent": treating it as absent would drive a create that is also
    denied and report the wrong cause."""
    iam = _StubIam(get_error=_iam_client_error("AccessDenied", "GetOpenIDConnectProvider"))
    with pytest.raises(GitHubOidcProviderError):
        _svc(iam).ensure_provider()
    assert iam.creates == 0


def test_the_service_has_no_delete_path_for_the_account_global_singleton():
    """The provider is account-global; one AGP disconnect must not be able to remove it."""
    assert not [n for n in dir(GitHubOidcProviderService) if "delete" in n.lower()]


def _role_svc(iam=None):
    return EcrPushRoleService(
        role_name_prefix=PREFIX,
        oidc_provider_arn=PROVIDER_ARN,
        ecr_repository_arn=ECR_ARN,
        region="us-east-1",
        iam_client=iam or aws_clients.client("iam", region="us-east-1"),
    )


def test_shared_role_name_matches_the_terraform_resource_it_adopts():
    """``modules/agent_ecr`` created ``"${var.name_prefix}-agent-ecr-push"``; a drift here gives
    an already-deployed account a second role while the live one is orphaned."""
    assert _role_svc(iam=object()).shared_role_name() == f"{PREFIX}-agent-ecr-push"
    assert not _role_svc(iam=object()).shared_role_name().startswith(f"{PREFIX}-ecr-push-")


@mock_aws
def test_ensure_shared_role_creates_it_with_the_ported_terraform_policy():
    """Policy parity with the retired Terraform resource: ``GetAuthorizationToken`` on ``*`` plus
    the seven push actions scoped to the shared agent-images repo, and a trust naming the OIDC
    provider."""
    iam = aws_clients.client("iam", region="us-east-1")
    arn = _role_svc(iam).ensure_shared_role("AWS-AIOPS")

    assert arn is not None
    assert arn.endswith(f":role/{PREFIX}-agent-ecr-push")
    doc = iam.get_role_policy(RoleName=f"{PREFIX}-agent-ecr-push", PolicyName="ecr-push")["PolicyDocument"]
    push = next(s for s in doc["Statement"] if s["Action"] != "ecr:GetAuthorizationToken")
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
    auth = next(s for s in doc["Statement"] if s["Action"] == "ecr:GetAuthorizationToken")
    assert auth["Resource"] == "*"
    trust = iam.get_role(RoleName=f"{PREFIX}-agent-ecr-push")["Role"]["AssumeRolePolicyDocument"]
    assert trust["Statement"][0]["Principal"]["Federated"] == PROVIDER_ARN


@mock_aws
def test_ensure_shared_role_adopts_an_existing_role_without_retrusting_it():
    """The role is shared across orgs but a trust names one org, so refreshing it per connection
    would make the last connect win; only the org-independent inline policy converges."""
    iam = aws_clients.client("iam", region="us-east-1")
    svc = _role_svc(iam)
    svc.ensure_shared_role("AgenticOps-Platform")
    before = iam.get_role(RoleName=f"{PREFIX}-agent-ecr-push")["Role"]["AssumeRolePolicyDocument"]

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
