"""E36/T8 — the cross-account credential seam (`services.tenant_credentials`).

The defect this seam exists for (research item 1): every teardown path built its boto3
client from the backend's AMBIENT ECS-task credentials, so for a tenant whose stage
deploys into its OWN account the resource was never even addressed — the probe answered
``ResourceNotFoundException`` in the CONTROL-PLANE account, the cascade read that as
"already gone" and reported ``deleted`` while the runtime kept billing.

`stage_client` is the one place that decides WHICH account a teardown call lands in:

* no stage config, or a stage with an empty ``deploy_role_arn`` → the AMBIENT client
  (deploy-in-place; exactly today's behaviour, so a single-account tenant is untouched);
* a stage carrying a ``deploy_role_arn`` → ``sts:AssumeRole`` into the tenant account and
  a client built from the TEMPORARY credentials;
* an AssumeRole that fails → :class:`TenantCredentialsError`, never a silently-ambient
  client. Falling back to ambient on failure would reproduce the original defect exactly:
  the call would land in the control-plane account, answer NotFound, and be reported as a
  successful teardown.

FULLY OFFLINE. ``boto3`` is monkeypatched at the MODULE attribute — the pinned signature
carries no ``sts_client`` parameter (T13 consumes it as written), so the module attribute
is the only injection point. Nothing here touches the network.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import BotoCoreError, ClientError

from models.tenant import TenantStageConfig
from services import tenant_credentials
from services.tenant_credentials import TenantCredentialsError, stage_client

ROLE_ARN = "arn:aws:iam::210987654321:role/agp-deployment-acme-default"
ROLE_NAME = "agp-deployment-acme-default"


# --------------------------------------------------------------------------- #
# boto3 doubles
# --------------------------------------------------------------------------- #


class _FakeClient:
    """A boto3 client stand-in that REMEMBERS how it was built — the whole point of the
    seam is the kwargs, not the calls."""

    def __init__(self, service_name, **kwargs):
        self.service_name = service_name
        self.kwargs = kwargs
        self.assume_role_calls = []
        self.assume_role_result = {
            "Credentials": {
                "AccessKeyId": "ASIATEMP",
                "SecretAccessKey": "temp-secret",
                "SessionToken": "temp-token",
            }
        }
        self.assume_role_raises = None

    def assume_role(self, **kwargs):
        self.assume_role_calls.append(kwargs)
        if self.assume_role_raises:
            raise self.assume_role_raises
        return self.assume_role_result


class _FakeBoto3:
    """Records every ``client(...)`` call, in order, and hands back a ``_FakeClient``.

    Records EVERY call rather than the last: an assume path builds TWO clients (sts, then
    the target service) and a fake that overwrote one field could not tell a seam that
    assumed from one that quietly returned the ambient client.
    """

    def __init__(self):
        self.calls = []
        self.clients = []
        self.sts = None

    def client(self, service_name, **kwargs):
        self.calls.append((service_name, kwargs))
        made = _FakeClient(service_name, **kwargs)
        if service_name == "sts":
            # Pre-seeded so a test can arm assume_role BEFORE stage_client runs.
            made = self.sts or made
            self.sts = made
        self.clients.append(made)
        return made


@pytest.fixture
def fake_boto3(monkeypatch):
    fake = _FakeBoto3()
    monkeypatch.setattr(tenant_credentials, "boto3", fake)
    return fake


def _client_error(code: str = "AccessDenied") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": f"{code}: not allowed"}}, "AssumeRole"
    )


def _cfg(**over) -> TenantStageConfig:
    base = {"account_id": "210987654321", "region": "eu-west-1"}
    base.update(over)
    return TenantStageConfig(**base)


# --------------------------------------------------------------------------- #
# Ambient passthrough — deploy-in-place must be byte-for-byte today's behaviour
# --------------------------------------------------------------------------- #


def test_no_stage_config_yields_an_ambient_client(fake_boto3):
    """``cfg=None`` (no tenant service, legacy record, unknown stage) ⇒ ambient client.

    No STS call at all: an assume the caller did not ask for would need a grant the ECS
    task role does not hold, turning every single-account teardown into a failure."""
    client = stage_client("iam", None, session_suffix="teardown-agent-1")

    assert client.service_name == "iam"
    assert fake_boto3.calls == [("iam", {})]
    assert fake_boto3.sts is None


def test_empty_deploy_role_arn_yields_an_ambient_client_in_the_stage_region(fake_boto3):
    """A stage with NO deploy role is deploy-in-place: ambient CREDENTIALS, but the
    stage's own region — the seam knows the region, so using it is strictly more correct
    than the caller's default, and credentials are what "ambient" is about."""
    client = stage_client("iam", _cfg(deploy_role_arn=""), session_suffix="teardown-a")

    assert client.service_name == "iam"
    assert fake_boto3.calls == [("iam", {"region_name": "eu-west-1"})]
    assert fake_boto3.sts is None
    assert "aws_access_key_id" not in client.kwargs


# --------------------------------------------------------------------------- #
# The assume path
# --------------------------------------------------------------------------- #


def test_deploy_role_arn_assumes_and_builds_the_client_from_TEMP_credentials(fake_boto3):
    client = stage_client(
        "bedrock-agentcore-control",
        _cfg(deploy_role_arn=ROLE_ARN),
        session_suffix="teardown-agent-1",
    )

    # 1) an sts client in the STAGE's region, then 2) the target service client.
    assert [name for name, _ in fake_boto3.calls] == ["sts", "bedrock-agentcore-control"]
    assert fake_boto3.calls[0][1] == {"region_name": "eu-west-1"}
    assert fake_boto3.sts.assume_role_calls == [
        {"RoleArn": ROLE_ARN, "RoleSessionName": "agp-teardown-agent-1"}
    ]
    assert client.kwargs == {
        "region_name": "eu-west-1",
        "aws_access_key_id": "ASIATEMP",
        "aws_secret_access_key": "temp-secret",
        "aws_session_token": "temp-token",
    }


def test_the_session_name_is_prefixed_and_truncated_to_64(fake_boto3):
    """``RoleSessionName`` is capped at 64 chars by the STS API; a longer name is a hard
    ValidationError, i.e. a teardown that fails on the NAME rather than the permission."""
    stage_client("iam", _cfg(deploy_role_arn=ROLE_ARN), session_suffix="x" * 200)

    name = fake_boto3.sts.assume_role_calls[0]["RoleSessionName"]
    assert len(name) == 64
    assert name == ("agp-" + "x" * 200)[:64]
    assert name.startswith("agp-")


def test_each_call_assumes_afresh_no_cached_credentials(fake_boto3):
    """Two teardown steps ⇒ two assumes. Deliberate: a cached session would outlive the
    15-minute floor on some roles and there is nothing here to refresh it."""
    stage_client("iam", _cfg(deploy_role_arn=ROLE_ARN), session_suffix="a")
    stage_client("iam", _cfg(deploy_role_arn=ROLE_ARN), session_suffix="b")

    assert [n for n, _ in fake_boto3.calls] == ["sts", "iam", "sts", "iam"]
    assert [c["RoleSessionName"] for c in fake_boto3.sts.assume_role_calls] == [
        "agp-a",
        "agp-b",
    ]


# --------------------------------------------------------------------------- #
# Failure — TenantCredentialsError, and NEVER an ambient fallback
# --------------------------------------------------------------------------- #


def test_assume_role_ACCESS_DENIED_raises_TenantCredentialsError(fake_boto3):
    fake_boto3.sts = _FakeClient("sts")
    fake_boto3.sts.assume_role_raises = _client_error("AccessDenied")

    with pytest.raises(TenantCredentialsError) as excinfo:
        stage_client("iam", _cfg(deploy_role_arn=ROLE_ARN), session_suffix="teardown-a")

    assert excinfo.value.kind == "assume_role_failed"


def test_a_failed_assume_NEVER_returns_an_ambient_client(fake_boto3):
    """The defect in one line. An ambient fallback lands the delete in the CONTROL-PLANE
    account, where the tenant's resource is NotFound — which the cascade reads as
    idempotent success. So the failure must be loud, and no target client may be built."""
    fake_boto3.sts = _FakeClient("sts")
    fake_boto3.sts.assume_role_raises = _client_error("AccessDenied")

    with pytest.raises(TenantCredentialsError):
        stage_client("iam", _cfg(deploy_role_arn=ROLE_ARN), session_suffix="teardown-a")

    assert [name for name, _ in fake_boto3.calls] == ["sts"]  # no "iam" client built


def test_a_BotoCoreError_on_assume_also_raises_TenantCredentialsError(fake_boto3):
    """Not only ``ClientError``: a network/config failure is equally "we did not get into
    that account", and must not be allowed to escape as a bare botocore exception that a
    caller's ``except ClientError`` would miss."""
    fake_boto3.sts = _FakeClient("sts")
    fake_boto3.sts.assume_role_raises = BotoCoreError()

    with pytest.raises(TenantCredentialsError):
        stage_client("iam", _cfg(deploy_role_arn=ROLE_ARN), session_suffix="teardown-a")


def test_a_MALFORMED_assume_response_raises_TenantCredentialsError(fake_boto3):
    """A response with no ``Credentials`` must not KeyError out of the seam — the caller
    maps ``TenantCredentialsError`` to an honest report line and a bare KeyError would be
    reported as a generic step failure instead."""
    fake_boto3.sts = _FakeClient("sts")
    fake_boto3.sts.assume_role_result = {}

    with pytest.raises(TenantCredentialsError):
        stage_client("iam", _cfg(deploy_role_arn=ROLE_ARN), session_suffix="teardown-a")


# --------------------------------------------------------------------------- #
# The error message is SAFE — a hard project rule
# --------------------------------------------------------------------------- #


def test_the_error_names_the_ROLE_but_never_the_ACCOUNT_ID(fake_boto3):
    """``deploy_role_arn`` CONTAINS the tenant's 12-digit account id, and this string
    reaches a read model the console renders (and the logs). The project rule bans an
    account id anywhere, so the message carries the role NAME — the actionable fact, what
    an operator types into IAM — plus the exception TYPE, and nothing from the provider's
    body (which can hold a request id, an ARN, or a credential)."""
    fake_boto3.sts = _FakeClient("sts")
    fake_boto3.sts.assume_role_raises = _client_error("AccessDenied")

    with pytest.raises(TenantCredentialsError) as excinfo:
        stage_client("iam", _cfg(deploy_role_arn=ROLE_ARN), session_suffix="teardown-a")

    message = str(excinfo.value)
    assert ROLE_NAME in message
    assert "210987654321" not in message
    assert ROLE_ARN not in message
    assert "ClientError" in message
    assert "not allowed" not in message  # nothing from the provider body


@pytest.mark.parametrize(
    "arn",
    [
        "arn:aws:iam::210987654321:role-agp-deployment",  # an admin typo: '-' not '/'
        "210987654321",                                   # a bare account id
        "arn:aws:iam::210987654321:root",                 # no '/' anywhere
    ],
)
def test_a_MALFORMED_deploy_role_arn_never_leaks_the_ACCOUNT_ID(fake_boto3, arn):
    """The safety of every downstream string used to rest on an unenforced `/`.

    `deploy_role_arn` is free-form (`models/tenant.py` declares it `str = ""`; the tenant
    service validates `account_id` only), and `rsplit("/", 1)[-1]` on a slash-less value
    returns the WHOLE string — so a typo put the tenant's 12-digit account id into `reason`
    (which the delete modal renders), into `RepoDeleteResult`, and into CloudWatch. It fails
    SILENTLY into the leak, which is why a typo is the worst arrival path and why every
    well-formed-ARN test above was blind to it."""
    fake_boto3.sts = _FakeClient("sts")
    fake_boto3.sts.assume_role_raises = _client_error("AccessDenied")

    with pytest.raises(TenantCredentialsError) as excinfo:
        stage_client("iam", _cfg(deploy_role_arn=arn), session_suffix="teardown-a")

    message = str(excinfo.value)
    assert "210987654321" not in message
    assert "arn:" not in message
    assert excinfo.value.message == message  # the attribute callers surface, same string


def test_a_malformed_ARN_is_still_ATTEMPTED_rather_than_refused_on_our_own_regex(fake_boto3):
    """The label is a MESSAGE fix, not a validator. STS is the authority on what it accepts,
    so the assume is still made with the raw ARN — refusing locally would turn a working
    teardown into a failure over an ARN shape we simply failed to anticipate."""
    arn = "arn:aws-cn:iam::210987654321:role-weird"
    stage_client("iam", _cfg(deploy_role_arn=arn), session_suffix="teardown-a")

    assert fake_boto3.sts.assume_role_calls[0]["RoleArn"] == arn


def test_an_IAM_PATH_in_the_role_arn_yields_the_role_NAME_not_the_path(fake_boto3):
    """A path'd role ARN (`:role/team/dev/name`) is well-formed; the actionable fact is the
    NAME the operator types into IAM, so the path is discarded rather than echoed."""
    fake_boto3.sts = _FakeClient("sts")
    fake_boto3.sts.assume_role_raises = _client_error("AccessDenied")
    arn = "arn:aws:iam::210987654321:role/agp/platform/agp-deployment-acme-default"

    with pytest.raises(TenantCredentialsError) as excinfo:
        stage_client("iam", _cfg(deploy_role_arn=arn), session_suffix="teardown-a")

    assert ROLE_NAME in str(excinfo.value)
    assert "platform" not in str(excinfo.value)
    assert "210987654321" not in str(excinfo.value)


def test_the_error_chains_the_original_cause_for_the_logs(fake_boto3):
    """The safe message is for the operator; the real exception must still be reachable via
    ``__cause__`` so ``logger.exception`` at the call site records the true cause."""
    original = _client_error("AccessDenied")
    fake_boto3.sts = _FakeClient("sts")
    fake_boto3.sts.assume_role_raises = original

    with pytest.raises(TenantCredentialsError) as excinfo:
        stage_client("iam", _cfg(deploy_role_arn=ROLE_ARN), session_suffix="teardown-a")

    assert excinfo.value.__cause__ is original


# --------------------------------------------------------------------------- #
# Shape guarantees T13 consumes
# --------------------------------------------------------------------------- #


def test_TenantCredentialsError_is_an_Exception_with_message_and_kind():
    err = TenantCredentialsError("agp-deployment-x (ClientError)")

    assert isinstance(err, Exception)
    assert err.message == "agp-deployment-x (ClientError)"
    assert err.kind == "assume_role_failed"


def test_session_suffix_is_KEYWORD_ONLY_and_cfg_is_POSITIONAL(fake_boto3):
    """The signature is pinned (T13 consumes it as written): two positionals then a
    keyword-only ``session_suffix``. Passing it positionally must be a TypeError."""
    with pytest.raises(TypeError):
        stage_client("iam", None, "teardown-a")  # type: ignore[misc]

    # ...and the keyword form is the only one that works.
    assert stage_client("iam", None, session_suffix="teardown-a").service_name == "iam"


def test_the_service_name_is_passed_through_verbatim(fake_boto3):
    """No mapping table: the caller names the boto3 service (``iam``,
    ``bedrock-agentcore-control``, ``secretsmanager`` for T13) and the seam only decides
    the CREDENTIALS."""
    for service in ("iam", "bedrock-agentcore-control", "secretsmanager"):
        assert stage_client(service, None, session_suffix="s").service_name == service
