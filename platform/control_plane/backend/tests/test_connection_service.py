import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from moto.core.decorator import mock_aws
from seed import seed

from core import aws_clients
from core.state_store import StateStore
from models.connection import ConnectionAuthType, ConnectionCreate, ConnStatus, Provider
from services.connection_service import ConnectionError, ConnectionService
from services.connection_verify import VerifyResult
from services.github_app_auth import mint_installation_token

FIXED = datetime(2026, 6, 30, tzinfo=timezone.utc)


def _gen_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _svc(verify, ids=None, app_minter=None, resolver=None, converter=None):
    """An in-memory service with a moto Secrets Manager client, a fixed clock and deterministic ids."""
    ids = iter(ids or ["id-1", "id-2", "id-3"])
    kwargs: dict[str, Any] = {}
    if resolver is not None:
        kwargs["resolve_installation_id"] = resolver
    if converter is not None:
        kwargs["convert_manifest_code"] = converter
    return ConnectionService(
        table_name="",
        secret_prefix="agp-test/git-connections/",
        region="us-east-1",
        verify=verify,
        secrets_client=aws_clients.client("secretsmanager", region="us-east-1"),
        new_id=lambda: next(ids),
        now=lambda: FIXED,
        mint_installation_token=app_minter or (lambda *a, **k: "ghs_minted"),
        **kwargs,
    )


def _svc_with_ecr(verify, ecr, ids=None, oidc=None):
    """``_svc`` plus a fake per-org ECR-push role provisioner and, when given, a fake GitHub OIDC
    provider bootstrap."""
    ids = iter(ids or ["id-1", "id-2", "id-3"])
    return ConnectionService(
        table_name="",
        secret_prefix="agp-test/git-connections/",
        region="us-east-1",
        verify=verify,
        secrets_client=aws_clients.client("secretsmanager", region="us-east-1"),
        new_id=lambda: next(ids),
        now=lambda: FIXED,
        ecr_push_role_service=ecr,
        github_oidc_provider_service=oidc,
    )


def _ok(req=None, *a, **k):
    return VerifyResult(ok=True, account_login="octocat", reason=None)


def _fail(*a, **k):
    return VerifyResult(ok=False, account_login=None, reason="token did not authenticate")


@mock_aws
def test_create_verifies_then_stores_secret_and_record():
    svc = _svc(_ok)
    c = svc.create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"), created_by="admin@example.com"
    )
    assert c.id == "id-1" and c.status == ConnStatus.CONNECTED and c.account_login == "octocat"
    assert c.created_by == "admin@example.com" and c.has_secret is True
    sm = aws_clients.client("secretsmanager", region="us-east-1")
    body = json.loads(sm.get_secret_value(SecretId=c.secret_arn)["SecretString"])
    assert body == {"token": "ghp_x"}
    assert "token" not in json.loads(c.model_dump_json())


@mock_aws
def test_create_with_bad_token_stores_nothing():
    svc = _svc(_fail)
    with pytest.raises(ConnectionError) as ei:
        svc.create_connection(
            ConnectionCreate(provider=Provider.GITHUB, org="acme", token="bad"), created_by="a@b.com"
        )
    assert ei.value.kind == "verify_failed"
    assert svc.list_connections() == []
    sm = aws_clients.client("secretsmanager", region="us-east-1")
    assert sm.list_secrets()["SecretList"] == []


@mock_aws
def test_test_connection_flips_status_on_later_failure():
    flips = iter([_ok(), _fail()])
    svc = _svc(lambda *a, **k: next(flips))
    c = svc.create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"), created_by="a@b.com"
    )
    re = svc.test_connection(c.id)
    assert re.status_detail is not None
    assert re.status == ConnStatus.ERROR and "authenticate" in re.status_detail


@mock_aws
def test_replace_token_updates_secret_only_on_success():
    svc = _svc(_ok)
    c = svc.create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"), created_by="a@b.com"
    )
    svc.replace_token(c.id, "ghp_new")
    sm = aws_clients.client("secretsmanager", region="us-east-1")
    assert json.loads(sm.get_secret_value(SecretId=c.secret_arn)["SecretString"]) == {"token": "ghp_new"}


@mock_aws
def test_delete_removes_record_and_secret():
    svc = _svc(_ok)
    c = svc.create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"), created_by="a@b.com"
    )
    svc.delete_connection(c.id)
    assert svc.list_connections() == []
    sm = aws_clients.client("secretsmanager", region="us-east-1")
    assert sm.list_secrets()["SecretList"] == []


@mock_aws
def test_get_unknown_id_raises_not_found():
    svc = _svc(_ok)
    with pytest.raises(ConnectionError) as ei:
        svc.get_connection("nope")
    assert ei.value.kind == "not_found"


_PEM = _gen_pem()


def _app_body():
    return ConnectionCreate(
        provider=Provider.GITHUB,
        org="acme",
        auth_type=ConnectionAuthType.GITHUB_APP,
        app_id="123456",
        installation_id="78910",
        private_key=_PEM,
    )


@mock_aws
def test_create_app_connection_mints_token_verifies_then_stores_private_key():
    """The verify is handed the minted token, with the App probe, and the secret body holds the
    private key, never a token."""
    minted = {}

    def minter(app_id, installation_id, private_key_pem, **kwargs):
        minted["args"] = (app_id, installation_id, private_key_pem)
        return "ghs_installation_token"

    seen_token = {}

    def verify(provider, org, base_url, token, *, client, is_app=False):
        seen_token["token"] = token
        seen_token["is_app"] = is_app
        return VerifyResult(ok=True, account_login="acme-app", reason=None)

    svc = _svc(verify, app_minter=minter)
    c = svc.create_connection(_app_body(), created_by="admin@example.com")

    assert c.auth_type == ConnectionAuthType.GITHUB_APP
    assert c.app_id == "123456" and c.installation_id == "78910"
    assert c.status == ConnStatus.CONNECTED and c.account_login == "acme-app"
    assert minted["args"] == ("123456", "78910", _PEM)
    assert seen_token["token"] == "ghs_installation_token"
    assert seen_token["is_app"] is True

    sm = aws_clients.client("secretsmanager", region="us-east-1")
    body = json.loads(sm.get_secret_value(SecretId=c.secret_arn)["SecretString"])
    assert body == {"private_key": _PEM}
    dumped = json.loads(c.model_dump_json())
    assert "private_key" not in dumped and "token" not in dumped


@mock_aws
def test_get_bearer_token_returns_pat_for_pat_and_minted_for_app():
    def minter(app_id, installation_id, private_key_pem, **kwargs):
        return "ghs_fresh"

    svc = _svc(_ok, app_minter=minter)

    pat = svc.create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"),
        created_by="a@b.com",
    )
    app = svc.create_connection(_app_body(), created_by="a@b.com")

    assert svc.get_bearer_token(pat.id) == "ghp_x"
    assert svc.get_bearer_token(app.id) == "ghs_fresh"


@mock_aws
def test_app_verify_failure_stores_nothing():
    svc = _svc(_fail, app_minter=lambda *a, **k: "ghs_x")
    with pytest.raises(ConnectionError) as ei:
        svc.create_connection(_app_body(), created_by="a@b.com")
    assert ei.value.kind == "verify_failed"
    assert svc.list_connections() == []
    sm = aws_clients.client("secretsmanager", region="us-east-1")
    assert sm.list_secrets()["SecretList"] == []


@mock_aws
def test_replace_key_verifies_new_key_then_replaces_stored_secret():
    minted = []

    def minter(app_id, installation_id, private_key_pem, **kwargs):
        minted.append(private_key_pem)
        return "ghs_minted"

    svc = _svc(_ok, app_minter=minter)
    c = svc.create_connection(_app_body(), created_by="admin@example.com")

    new_pem = _gen_pem()
    re = svc.replace_key(c.id, new_pem)

    assert re.status == ConnStatus.CONNECTED
    assert minted[-1] == new_pem
    sm = aws_clients.client("secretsmanager", region="us-east-1")
    body = json.loads(sm.get_secret_value(SecretId=c.secret_arn)["SecretString"])
    assert body == {"private_key": new_pem}


@mock_aws
def test_replace_key_verify_failure_leaves_stored_key_unchanged():
    flips = iter([_ok(), _fail()])
    svc = _svc(lambda *a, **k: next(flips), app_minter=lambda *a, **k: "ghs_x")
    c = svc.create_connection(_app_body(), created_by="a@b.com")

    with pytest.raises(ConnectionError) as ei:
        svc.replace_key(c.id, _gen_pem())
    assert ei.value.kind == "verify_failed"
    sm = aws_clients.client("secretsmanager", region="us-east-1")
    body = json.loads(sm.get_secret_value(SecretId=c.secret_arn)["SecretString"])
    assert body == {"private_key": _PEM}


@mock_aws
def test_a_malformed_private_key_is_verify_failed_and_the_detail_carries_no_key_material():
    """PyJWT refuses a key it cannot parse before any GitHub call; the real minter surfaces it as
    ``verify_failed`` with a fixed message, nothing is stored, and neither the pasted text nor
    PyJWT's wording reaches the detail."""
    pasted = "not a pem at all"
    svc = _svc(_ok, app_minter=mint_installation_token)

    with pytest.raises(ConnectionError) as ei:
        svc.create_connection(
            ConnectionCreate(
                provider=Provider.GITHUB,
                org="acme",
                auth_type=ConnectionAuthType.GITHUB_APP,
                app_id="123456",
                installation_id="78910",
                private_key=pasted,
            ),
            created_by="a@b.com",
        )

    assert ei.value.kind == "verify_failed"
    assert ei.value.status_code == 400
    assert ei.value.detail == "the App private key could not be parsed"
    assert pasted not in ei.value.detail
    assert "parse the provided" not in ei.value.detail
    assert svc.list_connections() == []
    assert aws_clients.client("secretsmanager", region="us-east-1").list_secrets()["SecretList"] == []


@mock_aws
def test_replace_key_rejects_non_app_connection():
    svc = _svc(_ok)
    c = svc.create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"),
        created_by="a@b.com",
    )
    with pytest.raises(ConnectionError) as ei:
        svc.replace_key(c.id, _gen_pem())
    assert ei.value.kind == "verify_failed"


def test_create_validator_rejects_pat_without_token():
    with pytest.raises(ValueError):
        ConnectionCreate(provider=Provider.GITHUB, org="acme")


def test_create_validator_rejects_app_missing_fields():
    with pytest.raises(ValueError):
        ConnectionCreate(
            provider=Provider.GITHUB, org="acme", auth_type=ConnectionAuthType.GITHUB_APP, app_id="1"
        )


@pytest.mark.parametrize(
    "org",
    [
        "*",
        "a/*",
        "a:b",
        "?",
        "acme*",
        "-acme",
        "acme-",
        "a--b",
        "",
        "a" * 40,
        "acme corp",
        "acme\n",
    ],
)
def test_ConnectionCreate_REJECTS_an_org_that_could_widen_an_iam_trust_policy(org):
    """``org`` is interpolated into the ECR-push role's trust-policy ``sub`` pattern, where ``*``
    and ``?`` are wildcards, so ``org="*"`` would mint a role assumable by any GitHub repo. The
    model refuses it before any IAM call; a trailing newline is refused too, since ``match()``'s
    ``$`` would tolerate it."""
    with pytest.raises(ValueError):
        ConnectionCreate(provider=Provider.GITHUB, org=org, token="ghp_x")


@pytest.mark.parametrize("org", ["a", "A1", "AWS-AIOPS", "AgenticOps-Platform", "a" * 39])
def test_ConnectionCreate_still_accepts_every_legal_github_login(org):
    assert ConnectionCreate(provider=Provider.GITHUB, org=org, token="ghp_x").org == org


def test_the_validator_also_guards_the_GITLAB_provider():
    """``ensure_role(org)`` runs for every provider, so a GitLab group name reaches the same trust
    document; GitLab paths with ``/``, ``.`` or ``_`` are refused, which AGP's GitLab support
    today can afford."""
    with pytest.raises(ValueError):
        ConnectionCreate(provider=Provider.GITLAB, org="*", token="glpat_x")
    with pytest.raises(ValueError):
        ConnectionCreate(provider=Provider.GITLAB, org="group/subgroup", token="glpat_x")


def test_the_manifest_start_org_is_validated_too():
    """``ManifestStart.org`` travels through the CSRF state into ``create_pending_app_connection``
    and reaches ``_ensure_ecr_push_role`` on finalize without passing ``ConnectionCreate``."""
    from models.connection import ManifestStart

    with pytest.raises(ValueError):
        ManifestStart(org="*", redirect_url="https://agp.example/ops/connections/callback")
    with pytest.raises(ValueError):
        ManifestStart(org="acme\n", redirect_url="https://agp.example/ops/connections/callback")
    ok = ManifestStart(org="AgenticOps-Platform", redirect_url="https://agp.example/ops/connections/callback")
    assert ok.org == "AgenticOps-Platform"


@mock_aws
def test_a_wildcard_org_never_reaches_IAM_at_all():
    svc = _svc(_ok)
    with pytest.raises(ValueError):
        svc.create_connection(ConnectionCreate(provider=Provider.GITHUB, org="*", token="ghp_x"), "op@x")
    assert svc.list_connections() == []


@mock_aws
def test_create_pending_app_connection_stores_key_and_webhook_secret():
    svc = _svc(_ok)
    pem = _gen_pem()
    c = svc.create_pending_app_connection(
        org="acme",
        base_url=None,
        app_id="424242",
        private_key=pem,
        webhook_secret="whsec_x",
        created_by="a@b.com",
    )
    assert c.status == ConnStatus.PENDING and c.app_id == "424242" and c.installation_id is None
    assert c.auth_type == ConnectionAuthType.GITHUB_APP
    assert c.account_login is None and c.last_verified_at is None
    sm = aws_clients.client("secretsmanager", region="us-east-1")
    body = json.loads(sm.get_secret_value(SecretId=c.secret_arn)["SecretString"])
    assert body == {"private_key": pem, "webhook_secret": "whsec_x"}
    assert "private_key" not in json.loads(c.model_dump_json())


@mock_aws
def test_finalize_with_explicit_installation_id_connects():
    svc = _svc(_ok)
    pem = _gen_pem()
    c = svc.create_pending_app_connection(
        org="acme", base_url=None, app_id="1", private_key=pem, webhook_secret=None, created_by="a@b.com"
    )
    done = svc.finalize_app_connection(c.id, installation_id="999")
    assert done.status == ConnStatus.CONNECTED and done.installation_id == "999"
    assert done.account_login == "octocat" and done.last_verified_at is not None


@mock_aws
def test_finalize_auto_resolves_installation_and_connects():
    svc = _svc(_ok, resolver=lambda *a, **k: "222")
    pem = _gen_pem()
    c = svc.create_pending_app_connection(
        org="acme", base_url=None, app_id="1", private_key=pem, webhook_secret=None, created_by="a@b.com"
    )
    done = svc.finalize_app_connection(c.id)
    assert done.status == ConnStatus.CONNECTED and done.installation_id == "222"


@mock_aws
def test_finalize_not_installed_stays_pending():
    svc = _svc(_ok, resolver=lambda *a, **k: None)
    pem = _gen_pem()
    c = svc.create_pending_app_connection(
        org="acme", base_url=None, app_id="1", private_key=pem, webhook_secret=None, created_by="a@b.com"
    )
    with pytest.raises(ConnectionError) as ei:
        svc.finalize_app_connection(c.id)
    assert ei.value.kind == "verify_failed"
    assert svc.get_connection(c.id).status == ConnStatus.PENDING
    assert svc.get_connection(c.id).installation_id is None


@mock_aws
def test_finalize_verify_failure_stays_pending_no_installation_id():
    svc = _svc(_fail)
    pem = _gen_pem()
    c = svc.create_pending_app_connection(
        org="acme", base_url=None, app_id="1", private_key=pem, webhook_secret=None, created_by="a@b.com"
    )
    with pytest.raises(ConnectionError) as ei:
        svc.finalize_app_connection(c.id, installation_id="999")
    assert ei.value.kind == "verify_failed"
    got = svc.get_connection(c.id)
    assert got.status == ConnStatus.PENDING and got.installation_id is None


@mock_aws
def test_finalize_rejects_non_app_connection():
    svc = _svc(_ok)
    c = svc.create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"),
        created_by="a@b.com",
    )
    with pytest.raises(ConnectionError) as ei:
        svc.finalize_app_connection(c.id, installation_id="1")
    assert ei.value.kind == "verify_failed"


@mock_aws
def test_manifest_state_is_single_use_and_expires():
    svc = _svc(_ok)
    st = svc.create_manifest_state("acme", None, "a@b.com")
    got = svc.consume_manifest_state(st)
    assert got["org"] == "acme"
    assert got["base_url"] is None and got["created_by"] == "a@b.com"
    with pytest.raises(ConnectionError) as ei:
        svc.consume_manifest_state(st)
    assert ei.value.kind == "bad_request"


@mock_aws
def test_consume_unknown_manifest_state_is_bad_request():
    svc = _svc(_ok)
    with pytest.raises(ConnectionError) as ei:
        svc.consume_manifest_state("never-issued")
    assert ei.value.kind == "bad_request"


@mock_aws
def test_manifest_state_expired_is_bad_request():
    """The clock jumps 20 minutes between create and consume, past the 15-minute expiry."""
    clock = iter(
        [
            datetime(2026, 6, 30, 0, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 6, 30, 0, 20, 0, tzinfo=timezone.utc),
        ]
    )
    svc = ConnectionService(
        table_name="",
        secret_prefix="agp-test/git-connections/",
        region="us-east-1",
        verify=_ok,
        secrets_client=aws_clients.client("secretsmanager", region="us-east-1"),
        new_id=lambda: "state-1",
        now=lambda: next(clock),
        mint_installation_token=lambda *a, **k: "ghs_minted",
    )
    st = svc.create_manifest_state("acme", None, "a@b.com")
    with pytest.raises(ConnectionError) as ei:
        svc.consume_manifest_state(st)
    assert ei.value.kind == "bad_request"


@mock_aws
def test_manifest_state_never_appears_in_list_connections():
    svc = _svc(_ok)
    svc.create_manifest_state("acme", None, "a@b.com")
    assert svc.list_connections() == []


def _converter(pem):
    def _conv(code, **kwargs):
        return {"app_id": "555", "pem": pem, "webhook_secret": "whsec_z", "slug": "agp-acme-provisioning"}

    return _conv


@mock_aws
def test_complete_manifest_resolved_returns_connected():
    pem = _gen_pem()
    svc = _svc(_ok, resolver=lambda *a, **k: "222", converter=_converter(pem))
    st = svc.create_manifest_state("acme", None, "a@b.com")
    conn, needs_install, install_url = svc.complete_manifest("tempcode", st)
    assert needs_install is False and install_url is None
    assert conn.status == ConnStatus.CONNECTED and conn.installation_id == "222"
    assert conn.app_id == "555"
    sm = aws_clients.client("secretsmanager", region="us-east-1")
    body = json.loads(sm.get_secret_value(SecretId=conn.secret_arn)["SecretString"])
    assert body == {"private_key": pem, "webhook_secret": "whsec_z"}


@mock_aws
def test_complete_manifest_not_installed_returns_pending_and_install_url():
    pem = _gen_pem()
    svc = _svc(_ok, resolver=lambda *a, **k: None, converter=_converter(pem))
    st = svc.create_manifest_state("acme", None, "a@b.com")
    conn, needs_install, install_url = svc.complete_manifest("tempcode", st)
    assert needs_install is True
    assert install_url == "https://github.com/apps/agp-acme-provisioning/installations/new"
    assert conn.status == ConnStatus.PENDING and conn.installation_id is None


@mock_aws
def test_complete_manifest_tolerates_the_extra_oauth_converter_keys():
    """A conversion that returns the OAuth pair onboards the App exactly like one that omits it;
    capture itself is asserted in ``test_connection_oauth_client.py``."""
    pem = _gen_pem()

    def _conv(code, **kwargs):
        return {
            "app_id": "555",
            "pem": pem,
            "webhook_secret": "whsec_z",
            "slug": "agp-acme-provisioning",
            "client_id": "Iv1.abc123",
            "client_secret": "cs_x",
        }

    svc = _svc(_ok, resolver=lambda *a, **k: "222", converter=_conv)
    st = svc.create_manifest_state("acme", None, "a@b.com")
    conn, needs_install, install_url = svc.complete_manifest("tempcode", st)
    assert needs_install is False and install_url is None
    assert conn.status == ConnStatus.CONNECTED and conn.installation_id == "222"
    assert "client_secret" not in json.loads(conn.model_dump_json())


@mock_aws
def test_complete_manifest_rejects_bad_state():
    pem = _gen_pem()
    svc = _svc(_ok, resolver=lambda *a, **k: "222", converter=_converter(pem))
    with pytest.raises(ConnectionError) as ei:
        svc.complete_manifest("tempcode", "unknown-state")
    assert ei.value.kind == "bad_request"


@mock_aws
def test_create_connection_ensures_ecr_push_role_and_stores_arn():
    ecr = MagicMock()
    ecr.ensure_role.return_value = "arn:aws:iam::123456789012:role/agp-cp-dev-ecr-push-acme"
    svc = _svc_with_ecr(_ok, ecr)

    c = svc.create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"),
        created_by="admin@example.com",
    )

    ecr.ensure_role.assert_called_once_with("acme")
    assert c.ecr_push_role_arn == "arn:aws:iam::123456789012:role/agp-cp-dev-ecr-push-acme"


@mock_aws
def test_delete_connection_deletes_ecr_push_role_when_sole_connection_for_org():
    ecr = MagicMock()
    ecr.ensure_role.return_value = "arn:aws:iam::123456789012:role/agp-cp-dev-ecr-push-acme"
    svc = _svc_with_ecr(_ok, ecr)

    c = svc.create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"),
        created_by="a@b.com",
    )
    svc.delete_connection(c.id)

    ecr.delete_role.assert_called_once_with("acme")


@mock_aws
def test_delete_connection_keeps_ecr_push_role_when_second_connection_shares_org():
    """Two connections to one org share one role."""
    ecr = MagicMock()
    ecr.ensure_role.return_value = "arn:aws:iam::123456789012:role/agp-cp-dev-ecr-push-acme"
    svc = _svc_with_ecr(_ok, ecr, ids=["conn-a", "conn-b"])

    a = svc.create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"),
        created_by="a@b.com",
    )
    svc.create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_y"),
        created_by="a@b.com",
    )

    svc.delete_connection(a.id)

    ecr.delete_role.assert_not_called()


def _oidc_fake(arn="arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"):
    oidc = MagicMock()
    oidc.ensure_provider.return_value = arn
    return oidc


def _ecr_fake():
    """A role provisioner whose ``ensure_role`` returns a real ARN string, since the record's
    ``ecr_push_role_arn`` is validated as a string."""
    ecr = MagicMock()
    ecr.ensure_role.return_value = "arn:aws:iam::123456789012:role/agp-cp-dev-ecr-push-acme"
    ecr.ensure_shared_role.return_value = "arn:aws:iam::123456789012:role/agp-cp-dev-agent-ecr-push"
    return ecr


@mock_aws
def test_create_connection_bootstraps_the_provider_BEFORE_any_role():
    """A GitHub-OIDC role's trust names the provider as its ``Federated`` principal and IAM
    validates that the principal exists at role-create time, so the order is the contract."""
    calls = []
    oidc = MagicMock()
    oidc.ensure_provider.side_effect = lambda: (
        calls.append("provider"),
        "arn:aws:iam::123456789012:oidc-provider/x",
    )[1]
    ecr = MagicMock()
    ecr.ensure_shared_role.side_effect = lambda org: calls.append("shared")
    ecr.ensure_role.side_effect = lambda org: calls.append("per-org")

    _svc_with_ecr(_ok, ecr, oidc=oidc).create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"),
        created_by="a@b.com",
    )

    assert calls == ["provider", "shared", "per-org"]


@mock_aws
def test_a_second_connection_re_ensures_and_therefore_no_ops_on_the_get():
    """The bootstrap is re-ensured on every connection rather than tracked as first-only state,
    which would go stale the moment someone deleted the provider out of band."""
    oidc = _oidc_fake()
    ecr = _ecr_fake()
    svc = _svc_with_ecr(_ok, ecr, ids=["c-1", "c-2"], oidc=oidc)

    for token in ("ghp_x", "ghp_y"):
        svc.create_connection(
            ConnectionCreate(provider=Provider.GITHUB, org="acme", token=token),
            created_by="a@b.com",
        )

    assert oidc.ensure_provider.call_count == 2


@mock_aws
def test_a_gitlab_connection_NEVER_bootstraps_the_github_provider():
    """A customer who never connects GitHub never sees a GitHub dependency, including a customer
    who connects a different provider."""
    oidc = _oidc_fake()
    ecr = _ecr_fake()

    _svc_with_ecr(_ok, ecr, oidc=oidc).create_connection(
        ConnectionCreate(provider=Provider.GITLAB, org="acme", token="glpat_x"),
        created_by="a@b.com",
    )

    oidc.ensure_provider.assert_not_called()
    ecr.ensure_shared_role.assert_not_called()


@mock_aws
def test_a_failed_provider_bootstrap_does_not_block_the_connection():
    """An IAM bootstrap failure is logged, not raised, and the shared role is skipped because its
    trust would name a provider that does not exist."""
    oidc = MagicMock()
    oidc.ensure_provider.side_effect = RuntimeError("iam boom")
    ecr = _ecr_fake()
    ecr.ensure_role.return_value = None

    c = _svc_with_ecr(_ok, ecr, oidc=oidc).create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"),
        created_by="a@b.com",
    )

    assert c.status == ConnStatus.CONNECTED
    ecr.ensure_shared_role.assert_not_called()
    ecr.ensure_role.assert_called_once_with("acme")


@mock_aws
def test_an_inert_provider_service_skips_the_shared_role_but_keeps_the_per_org_path():
    oidc = MagicMock()
    oidc.ensure_provider.return_value = None
    ecr = _ecr_fake()

    _svc_with_ecr(_ok, ecr, oidc=oidc).create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"),
        created_by="a@b.com",
    )

    ecr.ensure_shared_role.assert_not_called()
    ecr.ensure_role.assert_called_once_with("acme")


@mock_aws
def test_delete_connection_never_touches_the_account_global_provider():
    """The provider is an account-global singleton and the shared push role is every connection's
    fallback, so one disconnect owns neither; only the per-org role is torn down."""
    oidc = _oidc_fake()
    ecr = _ecr_fake()
    svc = _svc_with_ecr(_ok, ecr, oidc=oidc)

    c = svc.create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"),
        created_by="a@b.com",
    )
    svc.delete_connection(c.id)

    assert not [n for n, *_ in oidc.method_calls if "delete" in n.lower()]
    assert not [n for n, *_ in ecr.method_calls if "shared" in n.lower() and "delete" in n.lower()]
    ecr.delete_role.assert_called_once_with("acme")


_BOTO_MARKER = "AGP_BOTO_INTERNALS_table=connections_requestid=DEADBEEF"


def _throttle():
    return ClientError(
        {
            "Error": {
                "Code": "ProvisionedThroughputExceededException",
                "Message": f"Throughput exceeded {_BOTO_MARKER}",
            }
        },
        "PutItem",
    )


def _unreachable():
    """The fault that is not a ``ClientError``: the request never reached DynamoDB."""
    return EndpointConnectionError(endpoint_url=f"https://{_BOTO_MARKER}.example.invalid/")


_FAULTS = [("client_error", _throttle), ("boto_core_error", _unreachable)]


def _faulting(svc, op, fault):
    """The service over a spy of its own memory table whose ``op`` raises ``fault``, so a test can
    drive a real flow and fault a single call; the rows written so far stay."""
    table = MagicMock(wraps=svc._store._table)
    getattr(table, op).side_effect = fault
    svc._store = StateStore("connections", table=table)
    assert svc._store.in_memory is False
    return svc


def _assert_safe_and_retryable(exc_info, site=""):
    """This service's own error, the retryable store kind, and no boto3 internals anywhere a
    route could serialize."""
    err = exc_info.value
    assert err.kind == "secret_error", f"{site}: kind must be the retryable store kind"
    for text in (err.message, str(err), repr(err.args)):
        assert _BOTO_MARKER not in text, f"{site}: boto3 internals leaked"
        assert "ProvisionedThroughput" not in text, f"{site}: boto3 error code leaked"
        assert "endpoint" not in text.lower(), f"{site}: endpoint URL leaked"
        assert "Traceback" not in text, f"{site}: traceback leaked"


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_every_guarded_store_write_translates_at_its_own_call_site(name, fault):
    seed = _svc(_ok)
    record = seed.create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"),
        created_by="a@b.com",
    )
    ops = {
        "_save": ("put_item", lambda s: s._save(record)),
        "_delete": ("delete_item", lambda s: s._delete(record.id)),
        "_save_state": ("put_item", lambda s: s._save_state("st-1", {"org": "acme"})),
        "_delete_state": ("delete_item", lambda s: s._delete_state("st-1")),
    }
    for op, (call, drive) in ops.items():
        svc = _faulting(_svc(_ok), call, fault())
        with pytest.raises(ConnectionError) as ei:
            drive(svc)
        _assert_safe_and_retryable(ei, site=op)


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_a_store_fault_on_a_public_write_stays_in_the_route_contract(name, fault):
    """The bare ``_save`` callers end to end: the verify passes, then the record write faults."""
    pem = _gen_pem()
    app = _svc(_ok).create_connection(
        ConnectionCreate(
            provider=Provider.GITHUB,
            org="acme",
            auth_type=ConnectionAuthType.GITHUB_APP,
            app_id="123",
            installation_id="456",
            private_key=pem,
        ),
        created_by="a@b.com",
    )

    for site, drive in {
        "test_connection": lambda s: s.test_connection(app.id),
        "replace_key": lambda s: s.replace_key(app.id, pem),
    }.items():
        svc = _svc(_ok)
        seed(svc._store, svc._to_item(app))
        _faulting(svc, "put_item", fault())
        with pytest.raises(ConnectionError) as ei:
            drive(svc)
        _assert_safe_and_retryable(ei, site=site)


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_both_families_swallow_identically_on_the_legacy_reads(name, fault):
    """``_get`` and ``_load_all`` swallow a store fault into ``None`` or ``[]``, which callers
    depend on; a ``BotoCoreError`` swallows exactly as a ``ClientError`` does and is never
    upgraded into a raise."""
    record = _svc(_ok).create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"),
        created_by="a@b.com",
    )

    svc = _svc(_ok)
    seed(svc._store, svc._to_item(record))
    _faulting(svc, "get_item", fault())
    assert svc._get(record.id) is None
    with pytest.raises(ConnectionError) as ei:
        svc.get_connection(record.id)
    assert ei.value.kind == "not_found"

    svc = _svc(_ok)
    seed(svc._store, svc._to_item(record))
    _faulting(svc, "query", fault())
    assert svc.list_connections() == []


@mock_aws
def test_two_callbacks_consume_one_state_once():
    """Two callbacks on one manifest state, the second landing inside the first's delete: the
    consume is one conditional delete whose return is the record, so exactly one callback gets it
    and the other is refused; a read before the delete would have handed both the same record."""
    svc = _svc(_ok)
    state = svc.create_manifest_state("acme", None, "a@b.com")
    table = svc._store._table
    spy = MagicMock(wraps=table)
    svc._store = StateStore("connections", table=spy)
    outcomes = []
    raced = {"done": False}

    def consume():
        try:
            outcomes.append(("ok", svc.consume_manifest_state(state)["org"]))
        except ConnectionError as exc:
            outcomes.append(("err", exc.kind))

    def racing_delete(Key, ConditionExpression=None, ReturnValues="NONE"):
        if not raced["done"]:
            raced["done"] = True
            consume()
        return table.delete_item(Key=Key, ConditionExpression=ConditionExpression, ReturnValues=ReturnValues)

    spy.delete_item.side_effect = racing_delete

    consume()

    assert sorted(outcomes) == [("err", "bad_request"), ("ok", "acme")]
    assert spy.delete_item.call_count == 2
    assert spy.get_item.call_count == 0


def test_an_empty_table_name_selects_the_in_memory_store():
    assert _svc(_ok)._store.in_memory is True


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_every_secrets_manager_call_translates_both_families(name, fault):
    """The four Secrets Manager guards. ``_create_secret``'s ``ResourceExistsException`` clause
    stays first, since it is itself a ``ClientError``; ``_delete_secret_best_effort`` keeps
    swallowing."""

    class _FaultSm:
        """Wrap the moto client so one method raises while ``.exceptions`` still resolves."""

        def __init__(self, inner, fail_on, fault):
            self._inner = inner
            self._fail_on = fail_on
            self._fault = fault

        def __getattr__(self, item):
            if item == "exceptions":
                return self._inner.exceptions
            attr = getattr(self._inner, item)
            if item != self._fail_on:
                return attr

            def _raise(*a, **k):
                raise self._fault

            return _raise

    real = aws_clients.client("secretsmanager", region="us-east-1")
    record = _svc(_ok).create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"),
        created_by="a@b.com",
    )

    raising = {
        "_create_secret": ("create_secret", lambda s: s._create_secret("id-9", {"token": "t"})),
        "_get_secret_body": ("get_secret_value", lambda s: s._get_secret_body(record.id)),
        "_put_secret_body": ("put_secret_value", lambda s: s._put_secret_body(record.id, {"a": 1})),
    }
    for site, (method, drive) in raising.items():
        svc = _svc(_ok)
        svc._sm = _FaultSm(real, method, fault())
        with pytest.raises(ConnectionError) as ei:
            drive(svc)
        _assert_safe_and_retryable(ei, site=site)

    svc = _svc(_ok)
    svc._sm = _FaultSm(real, "delete_secret", fault())
    svc._delete_secret_best_effort(record.id)


@pytest.mark.parametrize(
    "response",
    [{"SecretBinary": b"\x00\x01"}, {"SecretString": "not json"}, {"SecretString": "[1, 2]"}],
    ids=["binary", "not-json", "not-an-object"],
)
def test_a_secret_that_does_not_decode_is_secret_error(response):
    """A hand-edited secret (binary, not JSON, or a JSON value that is not an object) is
    ``secret_error`` like a store fault, never an escaped decode error."""
    sm = MagicMock()
    sm.create_secret.return_value = {"ARN": "secret-arn-1"}
    svc = ConnectionService(verify=_ok, secrets_client=sm, new_id=lambda: "id-1", now=lambda: FIXED)
    svc.create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"), created_by="a@b.com"
    )
    sm.get_secret_value.return_value = response

    with pytest.raises(ConnectionError) as ei:
        svc.get_bearer_token("id-1")

    assert ei.value.kind == "secret_error"
    assert str(ei.value) == "Failed to read connection secret"


def _pat_connection(svc):
    return svc.create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"), created_by="a@b.com"
    ).id


def _pending_app_connection(svc):
    return svc.create_pending_app_connection(
        org="acme",
        base_url=None,
        app_id="123",
        private_key=_gen_pem(),
        webhook_secret="whs_x",
        created_by="a@b.com",
    ).id


_UPDATES = {
    "test_connection": (_pat_connection, "_verify", lambda s, cid: s.test_connection(cid)),
    "replace_token": (_pat_connection, "_verify", lambda s, cid: s.replace_token(cid, "ghp_new")),
    "replace_key": (_pending_app_connection, "_verify", lambda s, cid: s.replace_key(cid, _gen_pem())),
    "finalize_app_connection": (
        _pending_app_connection,
        "_verify",
        lambda s, cid: s.finalize_app_connection(cid, "456"),
    ),
    "set_oauth_client": (
        _pending_app_connection,
        "_fetch_app_client_id",
        lambda s, cid: s.set_oauth_client(cid, "Iv1.client", "cs_x"),
    ),
}


@mock_aws
@pytest.mark.parametrize("site", sorted(_UPDATES))
def test_two_readers_of_one_connection_cannot_both_save(site):
    create, seam, drive = _UPDATES[site]
    svc = _svc(_ok)
    conn_id = create(svc)
    svc._fetch_app_client_id = lambda *a, **k: "Iv1.client"
    provider_call = getattr(svc, seam)

    def racing(*args, **kwargs):
        other = svc.get_connection(conn_id)
        other.status_detail = "saved by another request"
        svc._save(other)
        return provider_call(*args, **kwargs)

    setattr(svc, seam, racing)

    with pytest.raises(ConnectionError) as ei:
        drive(svc, conn_id)
    assert ei.value.kind == "changed"
    assert svc.get_connection(conn_id).status_detail == "saved by another request"


@mock_aws
def test_a_test_that_raced_a_delete_answers_changed_and_does_not_resurrect_the_connection():
    svc = _svc(_ok)
    conn_id = _pat_connection(svc)

    def racing(*args, **kwargs):
        svc.delete_connection(conn_id)
        return _ok()

    svc._verify = racing

    with pytest.raises(ConnectionError) as ei:
        svc.test_connection(conn_id)
    assert ei.value.kind == "changed"
    assert svc.list_connections() == []
    assert svc._store.get(("connection", conn_id)) is None


@mock_aws
def test_a_row_written_before_row_versions_takes_one_save_and_refuses_a_second_stale_one():
    origin = _svc(_ok)
    conn_id = _pat_connection(origin)
    original_row = origin._store.get(("connection", conn_id))
    assert original_row is not None
    assert original_row["row_version"] == 1

    svc = _svc(_ok)
    seed(svc._store, svc._to_item(origin.get_connection(conn_id)))
    first, second = svc.get_connection(conn_id), svc.get_connection(conn_id)

    svc._save(first)
    saved_row = svc._store.get(("connection", conn_id))
    assert saved_row is not None
    assert saved_row["row_version"] == 1
    with pytest.raises(ConnectionError) as ei:
        svc._save(second)
    assert ei.value.kind == "changed"


@mock_aws
@pytest.mark.parametrize("site", sorted(_UPDATES))
def test_an_update_reads_its_row_consistently_and_a_plain_read_does_not(site):
    create, _seam, drive = _UPDATES[site]
    svc = _svc(_ok)
    conn_id = create(svc)
    svc._fetch_app_client_id = lambda *a, **k: "Iv1.client"
    spy = MagicMock(wraps=svc._store._table)
    svc._store = StateStore("connections", table=spy)

    drive(svc, conn_id)
    svc.get_connection(conn_id)

    names = [name for name, _args, _kwargs in spy.mock_calls]
    reads = [kwargs["ConsistentRead"] for name, _args, kwargs in spy.mock_calls if name == "get_item"]
    assert names.index("get_item") < names.index("put_item")
    assert reads[0] is True
    assert reads[-1] is False
