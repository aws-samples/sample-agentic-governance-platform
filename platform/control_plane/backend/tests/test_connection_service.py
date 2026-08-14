import json
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from moto import mock_aws
from datetime import datetime, timezone
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from models.connection import AuthType, ConnectionCreate, Provider, ConnStatus
from services.connection_service import ConnectionService, ConnectionError
from services.connection_verify import VerifyResult

FIXED = datetime(2026, 6, 30, tzinfo=timezone.utc)


def _gen_pem() -> str:
    """Generate a throwaway RSA private-key PEM (no fixed key material committed)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _svc(verify, ids=None, app_minter=None, resolver=None, converter=None):
    ids = iter(ids or ["id-1", "id-2", "id-3"])
    kwargs = {}
    if resolver is not None:
        kwargs["resolve_installation_id"] = resolver
    if converter is not None:
        kwargs["convert_manifest_code"] = converter
    return ConnectionService(
        table_name="",  # in-memory local fallback
        secret_prefix="agp-test/git-connections/",
        region="us-east-1",
        verify=verify,
        secrets_client=boto3.client("secretsmanager", region_name="us-east-1"),
        new_id=lambda: next(ids),
        now=lambda: FIXED,
        mint_installation_token=app_minter or (lambda *a, **k: "ghs_minted"),
        **kwargs,
    )


def _svc_with_ecr(verify, ecr, ids=None, oidc=None):
    """Like ``_svc`` but injects a fake per-org ECR-push role provisioner (E22 multi-org).

    Mirrors the ``_svc`` idiom (in-memory local fallback, moto Secrets Manager client,
    fixed clock + deterministic ids) and adds ``ecr_push_role_service=<fake>`` so the
    connection→role lifecycle can be asserted without touching IAM. ``oidc`` is the
    GitHub-OIDC provider bootstrap (also a fake) — omitted ⇒ the bootstrap is skipped."""
    ids = iter(ids or ["id-1", "id-2", "id-3"])
    return ConnectionService(
        table_name="",
        secret_prefix="agp-test/git-connections/",
        region="us-east-1",
        verify=verify,
        secrets_client=boto3.client("secretsmanager", region_name="us-east-1"),
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
    c = svc.create_connection(ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"),
                              created_by="admin@example.com")
    assert c.id == "id-1" and c.status == ConnStatus.CONNECTED and c.account_login == "octocat"
    assert c.created_by == "admin@example.com" and c.has_secret is True
    # secret really stored, body is JSON with the token:
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    body = json.loads(sm.get_secret_value(SecretId=c.secret_arn)["SecretString"])
    assert body == {"token": "ghp_x"}
    # record retrievable, and never carries a token:
    assert "token" not in json.loads(c.model_dump_json())


@mock_aws
def test_create_with_bad_token_stores_nothing():
    svc = _svc(_fail)
    with pytest.raises(ConnectionError) as ei:
        svc.create_connection(ConnectionCreate(provider=Provider.GITHUB, org="acme", token="bad"),
                              created_by="a@b.com")
    assert ei.value.kind == "verify_failed"
    assert svc.list_connections() == []
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    assert sm.list_secrets()["SecretList"] == []  # no orphan secret


@mock_aws
def test_test_connection_flips_status_on_later_failure():
    flips = iter([_ok(), _fail()])
    svc = _svc(lambda *a, **k: next(flips))
    c = svc.create_connection(ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"),
                              created_by="a@b.com")
    re = svc.test_connection(c.id)
    assert re.status == ConnStatus.ERROR and "authenticate" in re.status_detail


@mock_aws
def test_replace_token_updates_secret_only_on_success():
    svc = _svc(_ok)
    c = svc.create_connection(ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"),
                              created_by="a@b.com")
    svc.replace_token(c.id, "ghp_new")
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    assert json.loads(sm.get_secret_value(SecretId=c.secret_arn)["SecretString"]) == {"token": "ghp_new"}


@mock_aws
def test_delete_removes_record_and_secret():
    svc = _svc(_ok)
    c = svc.create_connection(ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"),
                              created_by="a@b.com")
    svc.delete_connection(c.id)
    assert svc.list_connections() == []
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    assert sm.list_secrets()["SecretList"] == []


@mock_aws
def test_get_unknown_id_raises_not_found():
    svc = _svc(_ok)
    with pytest.raises(ConnectionError) as ei:
        svc.get_connection("nope")
    assert ei.value.kind == "not_found"


# --------------------------------------------------------------------------- #
# GitHub App auth (E20/T9)
# --------------------------------------------------------------------------- #

_PEM = _gen_pem()  # one throwaway key reused where the same key must persist


def _app_body():
    return ConnectionCreate(
        provider=Provider.GITHUB,
        org="acme",
        auth_type=AuthType.GITHUB_APP,
        app_id="123456",
        installation_id="78910",
        private_key=_PEM,
    )


@mock_aws
def test_create_app_connection_mints_token_verifies_then_stores_private_key():
    minted = {}

    def minter(app_id, installation_id, private_key_pem, **kwargs):
        minted["args"] = (app_id, installation_id, private_key_pem)
        return "ghs_installation_token"

    # Verify must be handed the MINTED token, not the private key.
    seen_token = {}

    def verify(provider, org, base_url, token, *, client, is_app=False):
        seen_token["token"] = token
        seen_token["is_app"] = is_app
        return VerifyResult(ok=True, account_login="acme-app", reason=None)

    svc = _svc(verify, app_minter=minter)
    c = svc.create_connection(_app_body(), created_by="admin@example.com")

    assert c.auth_type == AuthType.GITHUB_APP
    assert c.app_id == "123456" and c.installation_id == "78910"
    assert c.status == ConnStatus.CONNECTED and c.account_login == "acme-app"
    assert minted["args"] == ("123456", "78910", _PEM)
    assert seen_token["token"] == "ghs_installation_token"
    # App connections must verify with the installation-token probe, not /user.
    assert seen_token["is_app"] is True

    # Secret body holds the PRIVATE KEY, never a token; read model never carries the key.
    sm = boto3.client("secretsmanager", region_name="us-east-1")
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
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    assert sm.list_secrets()["SecretList"] == []


@mock_aws
def test_replace_key_verifies_new_key_then_replaces_stored_secret():
    minted = []

    def minter(app_id, installation_id, private_key_pem, **kwargs):
        minted.append(private_key_pem)
        return "ghs_minted"

    svc = _svc(_ok, app_minter=minter)
    c = svc.create_connection(_app_body(), created_by="admin@example.com")

    new_pem = _gen_pem()  # a DIFFERENT key from _PEM (rotation)
    re = svc.replace_key(c.id, new_pem)

    assert re.status == ConnStatus.CONNECTED
    # verify was handed a token minted from the NEW key (last mint call used new_pem).
    assert minted[-1] == new_pem
    # stored secret now holds the new private key, never a token.
    sm = boto3.client("secretsmanager", region_name="us-east-1")
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
    # stored key is still the original PEM.
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    body = json.loads(sm.get_secret_value(SecretId=c.secret_arn)["SecretString"])
    assert body == {"private_key": _PEM}


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
            provider=Provider.GITHUB, org="acme", auth_type=AuthType.GITHUB_APP, app_id="1"
        )  # missing installation_id + private_key


# --------------------------------------------------------------------------- #
# C1 — THE MODEL is the first of two gates on an org that reaches an IAM trust policy.
#
# `org` flows create_connection → _ensure_ecr_push_role → ensure_role/ensure_shared_role →
# `_trust_policy`, where it is interpolated into a `sub` StringLike PATTERN. `*` and `?` are
# wildcards there, so `org="*"` yields `repo:*/*:*` — a role assumable by ANY GitHub repo,
# whose ARN AGP publishes to third-party repos as `AWS_ECR_PUSH_ROLE_ARN`.
#
# Rejecting it HERE means the value never reaches AWS at all — no IAM call, no partially
# provisioned role, and a 422 the operator can read instead of a swallowed best-effort log
# (`_ensure_ecr_push_role` catches everything, so a service-layer-only fix would make the
# connection succeed with no push role and no visible reason).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "org",
    [
        "*",        # the attack — repo:*/*:*
        "a/*",
        "a:b",
        "?",
        "acme*",
        "-acme",    # leading hyphen: not a legal GitHub login
        "acme-",    # trailing hyphen
        "a--b",     # doubled internal hyphen
        "",
        "a" * 40,   # over GitHub's 39-char limit
        "acme corp",
        "acme\n",   # trailing newline — match()'s `$` tolerates it, fullmatch doesn't
    ],
)
def test_ConnectionCreate_REJECTS_an_org_that_could_widen_an_iam_trust_policy(org):
    with pytest.raises(ValueError):
        ConnectionCreate(provider=Provider.GITHUB, org=org, token="ghp_x")


@pytest.mark.parametrize("org", ["a", "A1", "AWS-AIOPS", "AgenticOps-Platform", "a" * 39])
def test_ConnectionCreate_still_accepts_every_legal_github_login(org):
    """The fence must not be a wall — real logins, including the 39-char maximum and a
    single character, must keep working."""
    assert ConnectionCreate(provider=Provider.GITHUB, org=org, token="ghp_x").org == org


def test_the_validator_also_guards_the_GITLAB_provider():
    """DELIBERATE, and it is a narrowing: `_ensure_ecr_push_role` calls `ensure_role(org)`
    for EVERY provider, so a GitLab group name reaches the same IAM trust document. A
    wildcard that mints `repo:*/*:*` must not become reachable by sending
    `provider="gitlab"`. The cost is that GitLab paths containing `/`, `.` or `_` are
    refused — accepted, since AGP ships no GitLab repo provider."""
    with pytest.raises(ValueError):
        ConnectionCreate(provider=Provider.GITLAB, org="*", token="glpat_x")
    with pytest.raises(ValueError):
        ConnectionCreate(provider=Provider.GITLAB, org="group/subgroup", token="glpat_x")


def test_the_manifest_start_org_is_validated_too():
    """The SECOND door to the same trust policy. `ManifestStart.org` is stashed in the CSRF
    state, carried through `complete_manifest` into `create_pending_app_connection`, and
    reaches `_ensure_ecr_push_role` on finalize — never passing through `ConnectionCreate`.
    A gate on one model only would leave that whole path open."""
    from models.connection import ManifestStart

    with pytest.raises(ValueError):
        ManifestStart(org="*", redirect_url="https://agp.example/ops/connections/callback")
    # Trailing newline: the App-via-Manifest probe URL carries no org, so nothing upstream
    # would catch it — and adopt-don't-retrust makes a bad trust sub permanent.
    with pytest.raises(ValueError):
        ManifestStart(org="acme\n", redirect_url="https://agp.example/ops/connections/callback")
    ok = ManifestStart(
        org="AgenticOps-Platform", redirect_url="https://agp.example/ops/connections/callback"
    )
    assert ok.org == "AgenticOps-Platform"


@mock_aws
def test_a_wildcard_org_never_reaches_IAM_at_all():
    """The point of validating at the MODEL and not only in the service: rejected before any
    AWS call, so there is no secret written, no record persisted, and nothing to clean up."""
    svc = _svc(_ok)
    with pytest.raises(ValueError):
        svc.create_connection(
            ConnectionCreate(provider=Provider.GITHUB, org="*", token="ghp_x"), "op@x"
        )
    assert svc.list_connections() == []


# --------------------------------------------------------------------------- #
# Pending-connection lifecycle + CSRF state store + manifest (E20/U2)
# --------------------------------------------------------------------------- #


@mock_aws
def test_create_pending_app_connection_stores_key_and_webhook_secret():
    svc = _svc(_ok)
    pem = _gen_pem()
    c = svc.create_pending_app_connection(
        org="acme", base_url=None, app_id="424242", private_key=pem,
        webhook_secret="whsec_x", created_by="a@b.com",
    )
    assert c.status == ConnStatus.PENDING and c.app_id == "424242" and c.installation_id is None
    assert c.auth_type == AuthType.GITHUB_APP
    assert c.account_login is None and c.last_verified_at is None
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    body = json.loads(sm.get_secret_value(SecretId=c.secret_arn)["SecretString"])
    assert body == {"private_key": pem, "webhook_secret": "whsec_x"}
    assert "private_key" not in json.loads(c.model_dump_json())


@mock_aws
def test_finalize_with_explicit_installation_id_connects():
    svc = _svc(_ok)
    pem = _gen_pem()
    c = svc.create_pending_app_connection(org="acme", base_url=None, app_id="1",
        private_key=pem, webhook_secret=None, created_by="a@b.com")
    done = svc.finalize_app_connection(c.id, installation_id="999")
    assert done.status == ConnStatus.CONNECTED and done.installation_id == "999"
    assert done.account_login == "octocat" and done.last_verified_at is not None


@mock_aws
def test_finalize_auto_resolves_installation_and_connects():
    svc = _svc(_ok, resolver=lambda *a, **k: "222")
    pem = _gen_pem()
    c = svc.create_pending_app_connection(org="acme", base_url=None, app_id="1",
        private_key=pem, webhook_secret=None, created_by="a@b.com")
    done = svc.finalize_app_connection(c.id)
    assert done.status == ConnStatus.CONNECTED and done.installation_id == "222"


@mock_aws
def test_finalize_not_installed_stays_pending():
    svc = _svc(_ok, resolver=lambda *a, **k: None)
    pem = _gen_pem()
    c = svc.create_pending_app_connection(org="acme", base_url=None, app_id="1",
        private_key=pem, webhook_secret=None, created_by="a@b.com")
    with pytest.raises(ConnectionError) as ei:
        svc.finalize_app_connection(c.id)
    assert ei.value.kind == "verify_failed"
    assert svc.get_connection(c.id).status == ConnStatus.PENDING
    assert svc.get_connection(c.id).installation_id is None


@mock_aws
def test_finalize_verify_failure_stays_pending_no_installation_id():
    svc = _svc(_fail)
    pem = _gen_pem()
    c = svc.create_pending_app_connection(org="acme", base_url=None, app_id="1",
        private_key=pem, webhook_secret=None, created_by="a@b.com")
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
        svc.consume_manifest_state(st)  # already consumed
    assert ei.value.kind == "bad_request"


@mock_aws
def test_consume_unknown_manifest_state_is_bad_request():
    svc = _svc(_ok)
    with pytest.raises(ConnectionError) as ei:
        svc.consume_manifest_state("never-issued")
    assert ei.value.kind == "bad_request"


@mock_aws
def test_manifest_state_expired_is_bad_request():
    # Clock that jumps forward > 900s between create and consume.
    clock = iter([
        datetime(2026, 6, 30, 0, 0, 0, tzinfo=timezone.utc),   # create → exp = t0 + 900
        datetime(2026, 6, 30, 0, 20, 0, tzinfo=timezone.utc),  # consume at t0 + 1200 (expired)
    ])
    svc = ConnectionService(
        table_name="",
        secret_prefix="agp-test/git-connections/",
        region="us-east-1",
        verify=_ok,
        secrets_client=boto3.client("secretsmanager", region_name="us-east-1"),
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
    sm = boto3.client("secretsmanager", region_name="us-east-1")
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
    """E27B: the converter now returns six keys. The four-key path above must keep working
    (it is the "GitHub omitted the OAuth pair" case), and the six-key path must not disturb
    onboarding either — capture behaviour itself is asserted in
    ``tests/test_connection_oauth_client.py``."""
    pem = _gen_pem()

    def _conv(code, **kwargs):
        return {"app_id": "555", "pem": pem, "webhook_secret": "whsec_z",
                "slug": "agp-acme-provisioning",
                "client_id": "Iv1.abc123", "client_secret": "cs_x"}

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


# --------------------------------------------------------------------------- #
# Per-org GitHub-OIDC ECR-push role lifecycle wiring (E22 multi-org)
# --------------------------------------------------------------------------- #


@mock_aws
def test_create_connection_ensures_ecr_push_role_and_stores_arn():
    """create_connection calls ensure_role(org) and stores the returned ARN on the record."""
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
    """delete_connection calls delete_role(org) when it is the ONLY connection for that org."""
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
    """delete_connection does NOT call delete_role while another connection to the same org
    still exists (the shared-org guard: two connections to one org share one role)."""
    ecr = MagicMock()
    ecr.ensure_role.return_value = "arn:aws:iam::123456789012:role/agp-cp-dev-ecr-push-acme"
    svc = _svc_with_ecr(_ok, ecr, ids=["conn-a", "conn-b"])

    a = svc.create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"),
        created_by="a@b.com",
    )
    svc.create_connection(  # second connection to the SAME org
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_y"),
        created_by="a@b.com",
    )

    svc.delete_connection(a.id)  # one still remains for "acme"

    ecr.delete_role.assert_not_called()


# --------------------------------------------------------------------------- #
# GitHub-OIDC bootstrap wiring — the platform, not Terraform, owns the provider
# --------------------------------------------------------------------------- #


def _oidc_fake(arn="arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"):
    oidc = MagicMock()
    oidc.ensure_provider.return_value = arn
    return oidc


def _ecr_fake():
    """A role provisioner whose ``ensure_role`` returns a real ARN string. A bare MagicMock
    return would fail ``Connection.ecr_push_role_arn``'s str validation, which is a fixture
    artifact rather than anything these tests are about."""
    ecr = MagicMock()
    ecr.ensure_role.return_value = "arn:aws:iam::123456789012:role/agp-cp-dev-ecr-push-acme"
    ecr.ensure_shared_role.return_value = (
        "arn:aws:iam::123456789012:role/agp-cp-dev-agent-ecr-push"
    )
    return ecr


@mock_aws
def test_create_connection_bootstraps_the_provider_BEFORE_any_role():
    """ORDER IS THE WHOLE POINT. A GitHub-OIDC role's trust names the provider as its
    ``Federated`` principal and IAM validates that principal EXISTS at role-create time — so
    a role created before the provider fails outright. That create-time dependency is why
    neither object could stay in Terraform on a provider-less account, and asserting the
    order here is what keeps the replacement correct."""
    calls = []
    oidc = MagicMock()
    oidc.ensure_provider.side_effect = lambda: (
        calls.append("provider"), "arn:aws:iam::123456789012:oidc-provider/x"
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
    """Idempotency at the wiring level: the bootstrap is not "first connection only" state
    the service has to track — it is re-ensured every time and the service's own GET makes
    the repeat free. Tracking firstness would be a cache that goes stale the moment someone
    deletes the provider out-of-band."""
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
    """The product promise, asserted. "Customers who never connect GitHub never see a GitHub
    dependency" has to hold for a customer who connects a DIFFERENT provider too — otherwise
    the dependency merely moved from apply-time to first-connection-time. A future GitLab
    integration bootstraps its own identity objects on this same seam."""
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
    """Same rule the per-org ensure already follows: an IAM bootstrap failure is logged, not
    raised. A connection is an operator's credential handshake; failing it because the
    account's IAM grants are incomplete would make a recoverable misconfiguration look like
    a broken credential. The shared role is skipped too — creating a role whose trust names
    a provider that does not exist is the IAM error this ordering exists to avoid."""
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
    """Inert (unresolvable ARN) ⇒ ensure_provider returns None. The shared role must be
    skipped — its trust needs that same ARN — while the per-org ensure still runs and
    reports its own inertness, matching the pre-existing "a partially-configured env never
    blocks a connection" rule."""
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
    """Asymmetric on purpose. The provider is an account-global singleton shared with
    everything else in the account that trusts GitHub Actions, and the shared push role is
    the fallback for every connection — so one disconnect owns neither. Only the per-org
    role is torn down."""
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


# =========================================================================== #
# Store-fault translation — BOTH boto3 families (E27B wave-3)
#
# `BotoCoreError` is NOT a `ClientError` subclass, so `ConnectionService`'s original
# `except ClientError` guards were half-open: an endpoint/DNS/connect-timeout fault
# propagated RAW and answered HTTP 500 on `GET /api/v1/me/github-link` and
# `POST /api/v1/me/github-link/{id}/verify` — outside E27B's pinned {400,404,409,502}.
#
# Every test below is parametrized over BOTH families, so narrowing any guard back to
# one family fails a test rather than shipping a half-fix.
# =========================================================================== #

# Deliberately noisy, credential-shaped boto3 text. A real throttle carries a table name
# and a request id; none of it may reach a `ConnectionError.message` and hence an HTTP body.
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
    # The half that is NOT a ClientError: the request never reached DynamoDB at all.
    # Ordinary from ECS (a VPC-endpoint blip, a DNS stall) and the exact shape the
    # original guards missed.
    return EndpointConnectionError(endpoint_url=f"https://{_BOTO_MARKER}.example.invalid/")


_FAULTS = [("client_error", _throttle), ("boto_core_error", _unreachable)]


class _FaultTable:
    """A minimal ("pk","sk") Table double that breaks exactly ONE operation.

    Everything else stores and reads normally, so a test can drive a real flow and fault a
    single call rather than a whole table. ``only_pk`` narrows the break to one partition,
    which is what lets a test fault a CONNECTION write while state writes still land."""

    def __init__(self, *, fail_on, fault, only_pk=None):
        self.items = {}
        self.calls = []
        self._fail_on = fail_on
        self._fault = fault
        self._only_pk = only_pk

    def _maybe_fail(self, op, pk):
        self.calls.append(op)
        if op == self._fail_on and (self._only_pk is None or pk == self._only_pk):
            raise self._fault

    def get_item(self, Key):  # noqa: N803 — boto3 param name
        self._maybe_fail("get_item", Key["pk"])
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):  # noqa: N803 — boto3 param name
        self._maybe_fail("put_item", Item["pk"])
        self.items[(Item["pk"], Item["sk"])] = dict(Item)

    def delete_item(self, Key):  # noqa: N803 — boto3 param name
        self._maybe_fail("delete_item", Key["pk"])
        self.items.pop((Key["pk"], Key["sk"]), None)

    def query(self, **kwargs):
        self._maybe_fail("query", _CONN_PK)
        return {"Items": [dict(v) for k, v in self.items.items() if k[0] == _CONN_PK]}


_CONN_PK = "connection"
_STATE_PK = "conn_state"


def _to_ddb(svc, table):
    """Flip an in-memory service into DDB mode over ``table``, carrying existing records
    across so a test can build state locally and then fault the store."""
    for sk, record in svc._local.items():
        table.items[(_CONN_PK, sk)] = {
            "pk": _CONN_PK,
            "sk": sk,
            **json.loads(record.model_dump_json()),
        }
    svc.table_name = "connections"
    svc._table = table
    svc._local.clear()
    assert svc._has_ddb is True
    return svc


def _assert_safe_and_retryable(exc_info, site=""):
    """The whole contract in one place: this service's OWN error, a kind the routes map to a
    retryable 502, and not one byte of boto3 internals in anything a route could serialize."""
    err = exc_info.value
    assert err.kind == "secret_error", f"{site}: kind must be the retryable store kind"
    for text in (err.message, str(err), repr(err.args)):
        assert _BOTO_MARKER not in text, f"{site}: boto3 internals leaked"
        assert "ProvisionedThroughput" not in text, f"{site}: boto3 error code leaked"
        assert "endpoint" not in text.lower(), f"{site}: endpoint URL leaked"
        assert "Traceback" not in text, f"{site}: traceback leaked"


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_every_guarded_ddb_write_translates_at_its_own_call_site(name, fault):
    """The call sites DIRECTLY, not only through their callers. ``create_connection`` and
    ``create_pending_app_connection`` wrap ``_save`` in ``except Exception`` for the
    secret-rollback, which MASKS whether ``_save`` itself translates — so a mutation that
    strips ``_save``'s own guard passes every flow-level test while leaving the five bare
    callers (``test_connection``, ``replace_token``, ``replace_key``, ``set_oauth_client``,
    ``finalize_app_connection``) answering 500. Pinned here at the seam instead."""
    seed = _svc(_ok)
    record = seed.create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"),
        created_by="a@b.com",
    )
    ops = {
        "_save": ("put_item", _CONN_PK, lambda s: s._save(record)),
        "_delete": ("delete_item", _CONN_PK, lambda s: s._delete(record.id)),
        "_save_state": ("put_item", _STATE_PK, lambda s: s._save_state("st-1", {"org": "acme"})),
        "_delete_state": ("delete_item", _STATE_PK, lambda s: s._delete_state("st-1")),
    }
    for op, (call, pk, drive) in ops.items():
        svc = _to_ddb(_svc(_ok), _FaultTable(fail_on=call, fault=fault(), only_pk=pk))
        with pytest.raises(ConnectionError) as ei:
            drive(svc)
        _assert_safe_and_retryable(ei, site=op)


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_a_store_fault_on_a_public_write_stays_in_the_route_contract(name, fault):
    """The bare ``_save`` callers, end to end. ``set_oauth_client``'s verify passes and the
    secret is merged, then the RECORD write faults — which used to escape as a 500."""
    pem = _gen_pem()
    seed = _svc(_ok)
    app = seed.create_connection(
        ConnectionCreate(
            provider=Provider.GITHUB, org="acme", auth_type=AuthType.GITHUB_APP,
            app_id="123", installation_id="456", private_key=pem,
        ),
        created_by="a@b.com",
    )

    for site, drive in {
        "test_connection": lambda s: s.test_connection(app.id),
        "replace_key": lambda s: s.replace_key(app.id, pem),
    }.items():
        svc = _to_ddb(_svc(_ok), _FaultTable(fail_on="put_item", fault=fault(), only_pk=_CONN_PK))
        # the faulted service needs the seeded record + its secret to reach the write
        svc._table.items[(_CONN_PK, app.id)] = {
            "pk": _CONN_PK, "sk": app.id, **json.loads(app.model_dump_json())
        }
        svc.secret_prefix = seed.secret_prefix
        with pytest.raises(ConnectionError) as ei:
            drive(svc)
        _assert_safe_and_retryable(ei, site=site)


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_both_families_swallow_identically_on_the_legacy_reads(name, fault):
    """``_get`` / ``_load_all`` / ``_get_state`` have SHIPPED swallow semantics that six
    ``get_bearer_token``-era callers depend on, so the fix must widen the exception TYPE
    without changing a single return value. A ``BotoCoreError`` that used to escape here is
    what answered 500 on ``GET /me/github-link``; it must now swallow exactly as a
    ``ClientError`` does — and it must NOT be upgraded into a raise."""
    record = _svc(_ok).create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"),
        created_by="a@b.com",
    )

    # _get -> None, which get_connection turns into the pinned `not_found` (404), never a 500
    svc = _to_ddb(_svc(_ok), _FaultTable(fail_on="get_item", fault=fault(), only_pk=_CONN_PK))
    svc._table.items[(_CONN_PK, record.id)] = {
        "pk": _CONN_PK, "sk": record.id, **json.loads(record.model_dump_json())
    }
    assert svc._get(record.id) is None
    with pytest.raises(ConnectionError) as ei:
        svc.get_connection(record.id)
    assert ei.value.kind == "not_found"

    # _load_all -> [], so list_connections answers 200 with an empty list (legacy, shipped)
    svc = _to_ddb(_svc(_ok), _FaultTable(fail_on="query", fault=fault(), only_pk=_CONN_PK))
    svc._table.items[(_CONN_PK, record.id)] = {
        "pk": _CONN_PK, "sk": record.id, **json.loads(record.model_dump_json())
    }
    assert svc.list_connections() == []

    # _get_state -> None, which consume_manifest_state refuses as `bad_request` (fail-CLOSED:
    # an unreadable CSRF state is never treated as valid)
    svc = _to_ddb(_svc(_ok), _FaultTable(fail_on="get_item", fault=fault(), only_pk=_STATE_PK))
    assert svc._get_state("st-1") is None
    with pytest.raises(ConnectionError) as ei:
        svc.consume_manifest_state("st-1")
    assert ei.value.kind == "bad_request"


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_every_secrets_manager_call_translates_both_families(name, fault):
    """The four SM guards. ``_create_secret``'s ``ResourceExistsException`` clause must stay
    FIRST (it is itself a ``ClientError`` subclass, so a widened guard placed above it would
    swallow the overwrite path); ``_delete_secret_best_effort`` must keep SWALLOWING."""

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

    real = boto3.client("secretsmanager", region_name="us-east-1")
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

    # best-effort delete keeps swallowing BOTH families (spec §5: a missing/unreachable
    # secret must not fail an already-completed delete)
    svc = _svc(_ok)
    svc._sm = _FaultSm(real, "delete_secret", fault())
    svc._delete_secret_best_effort(record.id)  # must not raise
