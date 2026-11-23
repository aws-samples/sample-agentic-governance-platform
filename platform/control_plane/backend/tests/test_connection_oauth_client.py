"""The OAuth-client credential seam on ``ConnectionService``.

Capture: ``complete_manifest`` forwards the ``client_id`` / ``client_secret`` the conversion
returns, and a conversion that omits them still onboards the App. Read:
``get_oauth_client_credentials`` hands the pair to the link service and refuses a GitHub
Enterprise connection. Paste: ``set_oauth_client`` verifies the supplied ``client_id`` against
``GET /app`` before writing and merges into the existing secret body, so ``private_key`` and
``webhook_secret`` survive. No failure path may carry the ``client_secret`` in a message, a log
line or a read model.
"""

import json
from datetime import datetime, timezone
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from moto.core.decorator import mock_aws

from core import aws_clients
from models.connection import ConnectionAuthType, ConnectionCreate, ConnStatus, Provider
from services.connection_service import ConnectionError, ConnectionService
from services.connection_verify import VerifyResult
from services.github_app_manifest import GitHubManifestError

FIXED = datetime(2026, 6, 30, tzinfo=timezone.utc)

SECRET = "cs_super_secret_value"


def _gen_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _ok(*a, **k):
    return VerifyResult(ok=True, account_login="octocat", reason=None)


def _svc(verify=_ok, ids=None, resolver=None, converter=None, client_id_fetcher=None):
    ids = iter(ids or ["id-1", "id-2", "id-3"])
    kwargs: dict[str, Any] = {}
    if resolver is not None:
        kwargs["resolve_installation_id"] = resolver
    if converter is not None:
        kwargs["convert_manifest_code"] = converter
    if client_id_fetcher is not None:
        kwargs["fetch_app_client_id"] = client_id_fetcher
    return ConnectionService(
        table_name="",
        secret_prefix="agp-test/git-connections/",
        region="us-east-1",
        verify=verify,
        secrets_client=aws_clients.client("secretsmanager", region="us-east-1"),
        new_id=lambda: next(ids),
        now=lambda: FIXED,
        mint_installation_token=lambda *a, **k: "ghs_minted",
        **kwargs,
    )


def _converter(pem, **extra):
    """A ``convert_manifest_code`` stub; ``extra`` adds the OAuth keys when supplied."""

    def _conv(code, **kwargs):
        body = {
            "app_id": "555",
            "pem": pem,
            "webhook_secret": "whsec_z",
            "slug": "agp-acme-provisioning",
        }
        body.update(extra)
        return body

    return _conv


def _read_secret(secret_arn: str) -> dict:
    sm = aws_clients.client("secretsmanager", region="us-east-1")
    return json.loads(sm.get_secret_value(SecretId=secret_arn)["SecretString"])


def _app_conn(svc, *, base_url=None, pem=None):
    """A CONNECTED GitHub App connection with no OAuth client yet."""
    pem = pem or _gen_pem()
    conn = svc.create_pending_app_connection(
        org="acme",
        base_url=base_url,
        app_id="424242",
        private_key=pem,
        webhook_secret="whsec_x",
        created_by="a@b.com",
    )
    return svc.finalize_app_connection(conn.id, installation_id="999"), pem


@mock_aws
def test_manifest_capture_stores_client_id_and_secret():
    pem = _gen_pem()
    svc = _svc(
        resolver=lambda *a, **k: "222",
        converter=_converter(pem, client_id="Iv1.abc123", client_secret=SECRET),
    )
    st = svc.create_manifest_state("acme", None, "a@b.com")
    conn, needs_install, _ = svc.complete_manifest("tempcode", st)

    assert needs_install is False and conn.status == ConnStatus.CONNECTED
    assert conn.client_id == "Iv1.abc123"
    assert conn.has_oauth_client is True
    assert _read_secret(conn.secret_arn) == {
        "private_key": pem,
        "webhook_secret": "whsec_z",
        "client_secret": SECRET,
    }
    assert "client_secret" not in json.loads(conn.model_dump_json())


@mock_aws
def test_manifest_without_client_fields_leaves_has_oauth_client_false():
    """Onboarding survives a conversion with no OAuth pair, and no null ``client_secret`` key is
    written."""
    pem = _gen_pem()
    svc = _svc(resolver=lambda *a, **k: "222", converter=_converter(pem))
    st = svc.create_manifest_state("acme", None, "a@b.com")
    conn, needs_install, _ = svc.complete_manifest("tempcode", st)

    assert needs_install is False and conn.status == ConnStatus.CONNECTED
    assert conn.client_id is None and conn.has_oauth_client is False
    assert _read_secret(conn.secret_arn) == {"private_key": pem, "webhook_secret": "whsec_z"}


@mock_aws
def test_manifest_with_client_id_but_no_secret_is_not_oauth_ready():
    pem = _gen_pem()
    svc = _svc(
        resolver=lambda *a, **k: "222",
        converter=_converter(pem, client_id="Iv1.abc123"),
    )
    st = svc.create_manifest_state("acme", None, "a@b.com")
    conn, _, _ = svc.complete_manifest("tempcode", st)

    assert conn.client_id == "Iv1.abc123" and conn.has_oauth_client is False
    assert "client_secret" not in _read_secret(conn.secret_arn)


@mock_aws
def test_get_oauth_client_credentials_returns_the_pair():
    pem = _gen_pem()
    svc = _svc(
        resolver=lambda *a, **k: "222",
        converter=_converter(pem, client_id="Iv1.abc123", client_secret=SECRET),
    )
    st = svc.create_manifest_state("acme", None, "a@b.com")
    conn, _, _ = svc.complete_manifest("tempcode", st)

    assert svc.get_oauth_client_credentials(conn.id) == ("Iv1.abc123", SECRET)


@mock_aws
def test_get_oauth_client_credentials_missing_secret_is_bad_request():
    svc = _svc()
    conn, _ = _app_conn(svc)
    with pytest.raises(ConnectionError) as ei:
        svc.get_oauth_client_credentials(conn.id)
    assert ei.value.kind == "bad_request"


@mock_aws
def test_get_oauth_client_credentials_unknown_connection_is_not_found():
    svc = _svc()
    with pytest.raises(ConnectionError) as ei:
        svc.get_oauth_client_credentials("nope")
    assert ei.value.kind == "not_found"


@mock_aws
def test_get_oauth_client_credentials_refuses_a_ghe_connection():
    """A captured pair on a ``base_url`` connection is not usable for the github.com-only OAuth legs."""
    pem = _gen_pem()
    svc = _svc(
        resolver=lambda *a, **k: "222",
        converter=_converter(pem, client_id="Iv1.abc123", client_secret=SECRET),
    )
    st = svc.create_manifest_state("acme", "https://ghe.example.com/api/v3", "a@b.com")
    conn, _, _ = svc.complete_manifest("tempcode", st)
    assert conn.has_oauth_client is True

    with pytest.raises(ConnectionError) as ei:
        svc.get_oauth_client_credentials(conn.id)
    assert ei.value.kind == "bad_request"


@mock_aws
def test_set_oauth_client_verifies_client_id_against_get_app():
    """Verified with the App's own stored key, against the record's base, on the injected clock."""
    seen = {}

    def _fetch(app_id, private_key_pem, *, client, base_url, now_epoch):
        seen.update(app_id=app_id, pem=private_key_pem, base_url=base_url, now_epoch=now_epoch)
        return "Iv1.abc123"

    svc = _svc(client_id_fetcher=_fetch)
    conn, pem = _app_conn(svc)

    updated = svc.set_oauth_client(conn.id, "Iv1.abc123", SECRET)

    assert updated.client_id == "Iv1.abc123" and updated.has_oauth_client is True
    assert _read_secret(conn.secret_arn)["client_secret"] == SECRET
    assert seen["app_id"] == "424242" and seen["pem"] == pem
    assert seen["base_url"] is None and seen["now_epoch"] == int(FIXED.timestamp())


@mock_aws
def test_set_oauth_client_mismatch_leaves_the_stored_secret_unchanged():
    svc = _svc(client_id_fetcher=lambda *a, **k: "Iv1.the_real_one")
    conn, _ = _app_conn(svc)
    before = _read_secret(conn.secret_arn)

    with pytest.raises(ConnectionError) as ei:
        svc.set_oauth_client(conn.id, "Iv1.someone_elses_app", SECRET)
    assert ei.value.kind == "verify_failed"

    assert _read_secret(conn.secret_arn) == before
    after = svc.get_connection(conn.id)
    assert after.client_id is None and after.has_oauth_client is False


@mock_aws
def test_set_oauth_client_merges_and_preserves_private_key_and_webhook_secret():
    svc = _svc(client_id_fetcher=lambda *a, **k: "Iv1.abc123")
    conn, pem = _app_conn(svc)

    svc.set_oauth_client(conn.id, "Iv1.abc123", SECRET)

    assert _read_secret(conn.secret_arn) == {
        "private_key": pem,
        "webhook_secret": "whsec_x",
        "client_secret": SECRET,
    }
    assert svc.get_oauth_client_credentials(conn.id) == ("Iv1.abc123", SECRET)


@mock_aws
def test_set_oauth_client_refuses_a_pat_connection():
    svc = _svc(client_id_fetcher=lambda *a, **k: pytest.fail("must not probe a PAT connection"))
    conn = svc.create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"),
        created_by="a@b.com",
    )
    with pytest.raises(ConnectionError) as ei:
        svc.set_oauth_client(conn.id, "Iv1.abc123", SECRET)
    assert ei.value.kind == "verify_failed"
    assert _read_secret(conn.secret_arn) == {"token": "ghp_x"}


@mock_aws
def test_set_oauth_client_unknown_connection_is_not_found():
    svc = _svc(client_id_fetcher=lambda *a, **k: "Iv1.abc123")
    with pytest.raises(ConnectionError) as ei:
        svc.set_oauth_client("nope", "Iv1.abc123", SECRET)
    assert ei.value.kind == "not_found"


@mock_aws
def test_set_oauth_client_probe_failure_is_verify_failed_with_a_safe_message():
    def _boom(*a, **k):
        raise GitHubManifestError("GitHub declined the app lookup (HTTP 401)")

    svc = _svc(client_id_fetcher=_boom)
    conn, _ = _app_conn(svc)
    with pytest.raises(ConnectionError) as ei:
        svc.set_oauth_client(conn.id, "Iv1.abc123", SECRET)
    assert ei.value.kind == "verify_failed"
    assert "HTTP 401" in ei.value.message
    assert "client_secret" not in _read_secret(conn.secret_arn)


@mock_aws
def test_set_oauth_client_error_never_contains_the_secret(caplog):
    """``verify_failed`` messages reach the HTTP client, so no failure path may carry the pasted
    secret in a message, a log line or a model; the cause is dropped so no chained args leak."""
    caplog.set_level("DEBUG")

    def _boom(*a, **k):
        raise GitHubManifestError("GitHub declined the app lookup (HTTP 401)")

    for fetcher in (_boom, lambda *a, **k: "Iv1.the_real_one"):
        svc = _svc(client_id_fetcher=fetcher)
        conn, _ = _app_conn(svc)
        with pytest.raises(ConnectionError) as ei:
            svc.set_oauth_client(conn.id, "Iv1.someone_elses_app", SECRET)
        assert SECRET not in ei.value.message
        assert SECRET not in str(ei.value)
        assert SECRET not in repr(ei.value.args)
        assert ei.value.__cause__ is None
        assert SECRET not in json.dumps(json.loads(svc.get_connection(conn.id).model_dump_json()))

    assert SECRET not in caplog.text


@mock_aws
def test_replace_key_preserves_a_stored_client_secret():
    """A routine key rotation must not destroy the OAuth client secret the body also carries."""
    svc = _svc(client_id_fetcher=lambda *a, **k: "Iv1.abc123")
    conn, _ = _app_conn(svc)
    svc.set_oauth_client(conn.id, "Iv1.abc123", SECRET)

    new_pem = _gen_pem()
    svc.replace_key(conn.id, new_pem)

    assert _read_secret(conn.secret_arn) == {
        "private_key": new_pem,
        "webhook_secret": "whsec_x",
        "client_secret": SECRET,
    }
    assert svc.get_connection(conn.id).has_oauth_client is True
    assert svc.get_oauth_client_credentials(conn.id) == ("Iv1.abc123", SECRET)


@mock_aws
def test_get_bearer_token_is_unchanged_for_pat_and_app():
    svc = _svc(client_id_fetcher=lambda *a, **k: "Iv1.abc123")
    pat = svc.create_connection(
        ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x"),
        created_by="a@b.com",
    )
    app, _ = _app_conn(svc)

    assert svc.get_bearer_token(pat.id) == "ghp_x"
    assert svc.get_bearer_token(app.id) == "ghs_minted"
    assert svc.get_connection(app.id).auth_type == ConnectionAuthType.GITHUB_APP

    svc.set_oauth_client(app.id, "Iv1.abc123", SECRET)
    assert svc.get_bearer_token(app.id) == "ghs_minted"
    assert svc.get_bearer_token(pat.id) == "ghp_x"
