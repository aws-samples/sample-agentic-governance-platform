"""The cross-account credential seam, ``services.tenant_credentials``.

``stage_client`` is the one place that decides which account a call lands in: no stage config,
or a stage with an empty ``deploy_role_arn``, is the ambient client (deploy-in-place); a stage
carrying a ``deploy_role_arn`` is ``sts:AssumeRole`` into the tenant account and a client built
from the temporary credentials; a failed assume raises :class:`TenantCredentialsError`, never a
silently ambient client, since that would land the call in the control-plane account and read
NotFound as a successful teardown.

Fully offline: ``core.aws_clients.client`` is monkeypatched, the only injection point the pinned
signature leaves.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import BotoCoreError, ClientError

from core import aws_clients
from core.aws_clients import AssumedCredentials
from models.tenant import TenantStageConfig
from services import tenant_credentials
from services.tenant_credentials import TenantCredentialsError, stage_client

ROLE_ARN = "arn:aws:iam::210987654321:role/agp-deployment-acme-default"
ROLE_NAME = "agp-deployment-acme-default"


class _FakeClient:
    """A client stand-in that remembers how the factory was asked to build it; the seam is about
    the kwargs."""

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
        self.assume_role_raises: Exception | None = None

    def assume_role(self, **kwargs):
        self.assume_role_calls.append(kwargs)
        if self.assume_role_raises:
            raise self.assume_role_raises
        return self.assume_role_result


class _FakeFactory:
    """Records every ``client(...)`` call in order: an assume path builds two clients (sts, then
    the target service), and the sts fake is pre-seeded so a test can arm ``assume_role`` first."""

    def __init__(self):
        self.calls = []
        self.clients = []
        self.sts = None

    def client(self, service_name, **kwargs):
        self.calls.append((service_name, kwargs))
        made = _FakeClient(service_name, **kwargs)
        if service_name == "sts":
            made = self.sts or made
            self.sts = made
        self.clients.append(made)
        return made


@pytest.fixture
def fake_factory(monkeypatch):
    fake = _FakeFactory()
    monkeypatch.setattr(aws_clients, "client", fake.client)
    return fake


def _client_error(code: str = "AccessDenied") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"{code}: not allowed"}}, "AssumeRole")


def _cfg(**over) -> TenantStageConfig:
    base: dict[str, Any] = {"account_id": "210987654321", "region": "eu-west-1"}
    base.update(over)
    return TenantStageConfig(**base)


def test_no_stage_config_yields_an_ambient_client(fake_factory):
    """``cfg=None`` is an ambient client with no STS call: an assume the caller did not ask for
    would need a grant the task role does not hold."""
    client = stage_client("iam", None, session_suffix="teardown-agent-1")

    assert client.service_name == "iam"
    assert fake_factory.calls == [("iam", {})]
    assert fake_factory.sts is None


def test_empty_deploy_role_arn_yields_an_ambient_client_in_the_stage_region(fake_factory):
    """Deploy-in-place: ambient credentials, the stage's own region."""
    client = stage_client("iam", _cfg(deploy_role_arn=""), session_suffix="teardown-a")

    assert client.service_name == "iam"
    assert fake_factory.calls == [("iam", {"region": "eu-west-1"})]
    assert fake_factory.sts is None
    assert "credentials" not in client.kwargs


def test_deploy_role_arn_assumes_and_builds_the_client_from_TEMP_credentials(fake_factory):
    """An sts client in the stage's region first, then the target service client."""
    client = stage_client(
        "bedrock-agentcore-control",
        _cfg(deploy_role_arn=ROLE_ARN),
        session_suffix="teardown-agent-1",
    )

    assert [name for name, _ in fake_factory.calls] == ["sts", "bedrock-agentcore-control"]
    assert fake_factory.calls[0][1] == {"region": "eu-west-1"}
    assert fake_factory.sts.assume_role_calls == [
        {"RoleArn": ROLE_ARN, "RoleSessionName": "agp-teardown-agent-1"}
    ]
    assert client.kwargs == {
        "region": "eu-west-1",
        "credentials": AssumedCredentials(
            access_key_id="ASIATEMP", secret_access_key="temp-secret", session_token="temp-token"
        ),
    }


def test_the_session_name_is_prefixed_and_truncated_to_64(fake_factory):
    """STS caps ``RoleSessionName`` at 64 characters; a longer name is a hard ValidationError."""
    stage_client("iam", _cfg(deploy_role_arn=ROLE_ARN), session_suffix="x" * 200)

    name = fake_factory.sts.assume_role_calls[0]["RoleSessionName"]
    assert len(name) == 64
    assert name == ("agp-" + "x" * 200)[:64]
    assert name.startswith("agp-")


def test_each_call_assumes_afresh_no_cached_credentials(fake_factory):
    """Two teardown steps are two assumes: a cached session would outlive the 15-minute floor on
    some roles and nothing here refreshes it."""
    stage_client("iam", _cfg(deploy_role_arn=ROLE_ARN), session_suffix="a")
    stage_client("iam", _cfg(deploy_role_arn=ROLE_ARN), session_suffix="b")

    assert [n for n, _ in fake_factory.calls] == ["sts", "iam", "sts", "iam"]
    assert [c["RoleSessionName"] for c in fake_factory.sts.assume_role_calls] == [
        "agp-a",
        "agp-b",
    ]


def test_assume_role_ACCESS_DENIED_raises_TenantCredentialsError(fake_factory):
    fake_factory.sts = _FakeClient("sts")
    fake_factory.sts.assume_role_raises = _client_error("AccessDenied")

    with pytest.raises(TenantCredentialsError) as excinfo:
        stage_client("iam", _cfg(deploy_role_arn=ROLE_ARN), session_suffix="teardown-a")

    assert excinfo.value.kind == "assume_role_failed"


def test_a_failed_assume_NEVER_returns_an_ambient_client(fake_factory):
    """An ambient fallback lands the delete in the control-plane account, where the tenant's
    resource is NotFound, which the cascade reads as idempotent success."""
    fake_factory.sts = _FakeClient("sts")
    fake_factory.sts.assume_role_raises = _client_error("AccessDenied")

    with pytest.raises(TenantCredentialsError):
        stage_client("iam", _cfg(deploy_role_arn=ROLE_ARN), session_suffix="teardown-a")

    assert [name for name, _ in fake_factory.calls] == ["sts"]


def test_a_BotoCoreError_on_assume_also_raises_TenantCredentialsError(fake_factory):
    fake_factory.sts = _FakeClient("sts")
    fake_factory.sts.assume_role_raises = BotoCoreError()

    with pytest.raises(TenantCredentialsError):
        stage_client("iam", _cfg(deploy_role_arn=ROLE_ARN), session_suffix="teardown-a")


def test_a_MALFORMED_assume_response_raises_TenantCredentialsError(fake_factory):
    """A response with no ``Credentials`` must not KeyError out of the seam."""
    fake_factory.sts = _FakeClient("sts")
    fake_factory.sts.assume_role_result = {}

    with pytest.raises(TenantCredentialsError):
        stage_client("iam", _cfg(deploy_role_arn=ROLE_ARN), session_suffix="teardown-a")


def test_the_error_names_the_ROLE_but_never_the_ACCOUNT_ID(fake_factory):
    """``deploy_role_arn`` contains the tenant's account id and the message reaches a read model
    and the logs, so it carries the role name and the exception type, nothing from the provider."""
    fake_factory.sts = _FakeClient("sts")
    fake_factory.sts.assume_role_raises = _client_error("AccessDenied")

    with pytest.raises(TenantCredentialsError) as excinfo:
        stage_client("iam", _cfg(deploy_role_arn=ROLE_ARN), session_suffix="teardown-a")

    message = str(excinfo.value)
    assert ROLE_NAME in message
    assert "210987654321" not in message
    assert ROLE_ARN not in message
    assert "ClientError" in message
    assert "not allowed" not in message


@pytest.mark.parametrize(
    "arn",
    [
        "arn:aws:iam::210987654321:role-agp-deployment",
        "210987654321",
        "arn:aws:iam::210987654321:root",
    ],
)
def test_a_MALFORMED_deploy_role_arn_never_leaks_the_ACCOUNT_ID(fake_factory, arn):
    """A typo, a bare account id or a ``:root`` ARN: slicing on a ``/`` that nothing enforces
    returned the whole string, so the label is parsed and fails closed to a generic name."""
    fake_factory.sts = _FakeClient("sts")
    fake_factory.sts.assume_role_raises = _client_error("AccessDenied")

    with pytest.raises(TenantCredentialsError) as excinfo:
        stage_client("iam", _cfg(deploy_role_arn=arn), session_suffix="teardown-a")

    message = str(excinfo.value)
    assert "210987654321" not in message
    assert "arn:" not in message
    assert excinfo.value.message == message


def test_a_malformed_ARN_is_still_ATTEMPTED_rather_than_refused_on_our_own_regex(fake_factory):
    """The label is a message fix, not a validator: STS is the authority on what it accepts."""
    arn = "arn:aws-cn:iam::210987654321:role-weird"
    stage_client("iam", _cfg(deploy_role_arn=arn), session_suffix="teardown-a")

    assert fake_factory.sts.assume_role_calls[0]["RoleArn"] == arn


def test_an_IAM_PATH_in_the_role_arn_yields_the_role_NAME_not_the_path(fake_factory):
    fake_factory.sts = _FakeClient("sts")
    fake_factory.sts.assume_role_raises = _client_error("AccessDenied")
    arn = "arn:aws:iam::210987654321:role/agp/platform/agp-deployment-acme-default"

    with pytest.raises(TenantCredentialsError) as excinfo:
        stage_client("iam", _cfg(deploy_role_arn=arn), session_suffix="teardown-a")

    assert ROLE_NAME in str(excinfo.value)
    assert "platform" not in str(excinfo.value)
    assert "210987654321" not in str(excinfo.value)


def test_the_error_chains_the_original_cause_for_the_logs(fake_factory):
    original = _client_error("AccessDenied")
    fake_factory.sts = _FakeClient("sts")
    fake_factory.sts.assume_role_raises = original

    with pytest.raises(TenantCredentialsError) as excinfo:
        stage_client("iam", _cfg(deploy_role_arn=ROLE_ARN), session_suffix="teardown-a")

    assert excinfo.value.__cause__ is original


def test_TenantCredentialsError_is_an_Exception_with_message_and_kind():
    err = TenantCredentialsError("agp-deployment-x (ClientError)")

    assert isinstance(err, Exception)
    assert err.message == "agp-deployment-x (ClientError)"
    assert err.kind == "assume_role_failed"


def test_TenantCredentialsError_renders_a_fixed_502_detail():
    """The one row never sends the message to a client, so a role name stays in the log."""
    err = TenantCredentialsError("agp-deployment-x (ClientError)")

    assert err.status_code == 502
    assert err.body() == {
        "detail": "could not assume the tenant deploy role",
        "code": "tenant.assume_role_failed",
    }


def test_StageUnresolvedError_renders_a_fixed_502_detail():
    err = tenant_credentials.StageUnresolvedError("no tenant record for stage 'prod'")

    assert err.message == "no tenant record for stage 'prod'"
    assert err.status_code == 502
    assert err.body() == {
        "detail": "could not resolve the account that owns the stage",
        "code": "tenant.stage_unresolved",
    }


def test_session_suffix_is_KEYWORD_ONLY_and_cfg_is_POSITIONAL(fake_factory):
    untyped: Any = stage_client
    with pytest.raises(TypeError):
        untyped("iam", None, "teardown-a")

    assert stage_client("iam", None, session_suffix="teardown-a").service_name == "iam"


def test_the_service_name_is_passed_through_verbatim(fake_factory):
    for service in ("iam", "bedrock-agentcore-control", "secretsmanager"):
        assert stage_client(service, None, session_suffix="s").service_name == service
