"""E27B/T6 — the OAuth-client credential seam on ``ConnectionService``.

Three things are under test here, and one of them is load-bearing for the whole epic:

  1. CAPTURE — ``complete_manifest`` forwards the ``client_id``/``client_secret`` that
     ``convert_manifest_code`` now returns, and a conversion that omits them still completes
     App onboarding (degrade to "user linking unavailable", never break onboarding).
  2. READ — ``get_oauth_client_credentials`` hands the pair to the link service, and refuses
     a GitHub Enterprise connection (the OAuth legs are github.com-only).
  3. PASTE — ``set_oauth_client`` is the ONLY way to recover the secret half for an App that
     was onboarded before capture existed, because GitHub exposes a client secret through no
     API at all. It verifies the supplied ``client_id`` against ``GET /app`` BEFORE writing
     and merges into the existing secret body, so ``private_key``/``webhook_secret`` survive.

SECURITY: no assertion here may make the ``client_secret`` reachable from a log line, an
exception message, or a read model — one test asserts exactly that.

Same seam as ``tests/test_connection_service.py``: ``table_name=""`` (in-memory fallback),
a moto Secrets Manager client, deterministic ids, a fixed clock, injected fakes.
"""

import json
from datetime import datetime, timezone

import boto3
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from moto import mock_aws

from models.connection import AuthType, ConnectionCreate, ConnStatus, Provider
from services.connection_service import ConnectionError, ConnectionService
from services.connection_verify import VerifyResult
from services.github_app_manifest import GitHubManifestError

FIXED = datetime(2026, 6, 30, tzinfo=timezone.utc)

# The pasted secret used throughout — nothing may echo it back.
SECRET = "cs_super_secret_value"


def _gen_pem() -> str:
    """Generate a throwaway RSA private-key PEM (no fixed key material committed)."""
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
    kwargs = {}
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
        secrets_client=boto3.client("secretsmanager", region_name="us-east-1"),
        new_id=lambda: next(ids),
        now=lambda: FIXED,
        mint_installation_token=lambda *a, **k: "ghs_minted",
        **kwargs,
    )


def _converter(pem, **extra):
    """A ``convert_manifest_code`` stub. ``extra`` adds the E27B OAuth keys when supplied."""

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
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    return json.loads(sm.get_secret_value(SecretId=secret_arn)["SecretString"])


def _app_conn(svc, *, base_url=None, pem=None):
    """A CONNECTED GitHub App connection with no OAuth client yet (the pre-E27B shape)."""
    pem = pem or _gen_pem()
    conn = svc.create_pending_app_connection(
        org="acme", base_url=base_url, app_id="424242", private_key=pem,
        webhook_secret="whsec_x", created_by="a@b.com",
    )
    return svc.finalize_app_connection(conn.id, installation_id="999"), pem


# --------------------------------------------------------------------------- #
# 1. Capture at manifest conversion
# --------------------------------------------------------------------------- #


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
    # All THREE keys in the secret body; the secret never reaches the read model.
    assert _read_secret(conn.secret_arn) == {
        "private_key": pem, "webhook_secret": "whsec_z", "client_secret": SECRET,
    }
    assert "client_secret" not in json.loads(conn.model_dump_json())


@mock_aws
def test_manifest_without_client_fields_leaves_has_oauth_client_false():
    """The regression that matters: onboarding must survive a conversion with no OAuth pair."""
    pem = _gen_pem()
    svc = _svc(resolver=lambda *a, **k: "222", converter=_converter(pem))
    st = svc.create_manifest_state("acme", None, "a@b.com")
    conn, needs_install, _ = svc.complete_manifest("tempcode", st)

    assert needs_install is False and conn.status == ConnStatus.CONNECTED
    assert conn.client_id is None and conn.has_oauth_client is False
    # No stray ``client_secret`` key written with a null value.
    assert _read_secret(conn.secret_arn) == {"private_key": pem, "webhook_secret": "whsec_z"}


@mock_aws
def test_manifest_with_client_id_but_no_secret_is_not_oauth_ready():
    """A half-captured pair: the id is usable metadata, but linking needs the paste."""
    pem = _gen_pem()
    svc = _svc(
        resolver=lambda *a, **k: "222",
        converter=_converter(pem, client_id="Iv1.abc123"),
    )
    st = svc.create_manifest_state("acme", None, "a@b.com")
    conn, _, _ = svc.complete_manifest("tempcode", st)

    assert conn.client_id == "Iv1.abc123" and conn.has_oauth_client is False
    assert "client_secret" not in _read_secret(conn.secret_arn)


# --------------------------------------------------------------------------- #
# 2. Reading the pair (the link service's injected loader)
# --------------------------------------------------------------------------- #


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
    """base_url set → github.com-only refusal (design §3), not a wrong-host authorize URL."""
    pem = _gen_pem()
    svc = _svc(
        resolver=lambda *a, **k: "222",
        converter=_converter(pem, client_id="Iv1.abc123", client_secret=SECRET),
    )
    st = svc.create_manifest_state("acme", "https://ghe.example.com/api/v3", "a@b.com")
    conn, _, _ = svc.complete_manifest("tempcode", st)
    assert conn.has_oauth_client is True  # captured, just not usable for the OAuth legs

    with pytest.raises(ConnectionError) as ei:
        svc.get_oauth_client_credentials(conn.id)
    assert ei.value.kind == "bad_request"


# --------------------------------------------------------------------------- #
# 3. The admin paste path
# --------------------------------------------------------------------------- #


@mock_aws
def test_set_oauth_client_verifies_client_id_against_get_app():
    seen = {}

    def _fetch(app_id, private_key_pem, *, client, base_url, now_epoch):
        seen.update(app_id=app_id, pem=private_key_pem, base_url=base_url, now_epoch=now_epoch)
        return "Iv1.abc123"

    svc = _svc(client_id_fetcher=_fetch)
    conn, pem = _app_conn(svc)

    updated = svc.set_oauth_client(conn.id, "Iv1.abc123", SECRET)

    assert updated.client_id == "Iv1.abc123" and updated.has_oauth_client is True
    assert _read_secret(conn.secret_arn)["client_secret"] == SECRET
    # Verified with the App's own stored key, against the record's base, on the injected clock.
    assert seen["app_id"] == "424242" and seen["pem"] == pem
    assert seen["base_url"] is None and seen["now_epoch"] == int(FIXED.timestamp())


@mock_aws
def test_set_oauth_client_mismatch_leaves_the_stored_secret_unchanged():
    svc = _svc(client_id_fetcher=lambda *a, **k: "Iv1.the_real_one")
    conn, pem = _app_conn(svc)
    before = _read_secret(conn.secret_arn)

    with pytest.raises(ConnectionError) as ei:
        svc.set_oauth_client(conn.id, "Iv1.someone_elses_app", SECRET)
    assert ei.value.kind == "verify_failed"

    assert _read_secret(conn.secret_arn) == before  # byte-identical
    after = svc.get_connection(conn.id)
    assert after.client_id is None and after.has_oauth_client is False


@mock_aws
def test_set_oauth_client_merges_and_preserves_private_key_and_webhook_secret():
    svc = _svc(client_id_fetcher=lambda *a, **k: "Iv1.abc123")
    conn, pem = _app_conn(svc)

    svc.set_oauth_client(conn.id, "Iv1.abc123", SECRET)

    assert _read_secret(conn.secret_arn) == {
        "private_key": pem, "webhook_secret": "whsec_x", "client_secret": SECRET,
    }
    # And the pair reads back through the loader the link service injects.
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
    """The last barrier: ``connections.py`` surfaces ``verify_failed`` messages to the HTTP
    client, so no failure path may carry the pasted secret in a message, a log, or a model."""
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
        assert ei.value.__cause__ is None  # raise … from None — no chained args to leak
        assert SECRET not in json.dumps(json.loads(svc.get_connection(conn.id).model_dump_json()))

    assert SECRET not in caplog.text


@mock_aws
def test_replace_key_preserves_a_stored_client_secret():
    # A routine ADMIN key rotation must not destroy the OAuth client secret. Before E27B the
    # body's only read-back key was ``private_key``, so a full-body replace was harmless; now
    # ``client_secret`` is read back too, and clobbering it would break every per-user link on
    # the org while ``has_oauth_client`` still reported True.
    svc = _svc(client_id_fetcher=lambda *a, **k: "Iv1.abc123")
    conn, pem = _app_conn(svc)
    svc.set_oauth_client(conn.id, "Iv1.abc123", SECRET)

    new_pem = _gen_pem()
    svc.replace_key(conn.id, new_pem)

    assert _read_secret(conn.secret_arn) == {
        "private_key": new_pem, "webhook_secret": "whsec_x", "client_secret": SECRET,
    }
    # The record's claim and the stored reality still agree.
    assert svc.get_connection(conn.id).has_oauth_client is True
    assert svc.get_oauth_client_credentials(conn.id) == ("Iv1.abc123", SECRET)


# --------------------------------------------------------------------------- #
# 4. Regression — E27B adds no third auth type and changes no minting behaviour
# --------------------------------------------------------------------------- #


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
    assert svc.get_connection(app.id).auth_type == AuthType.GITHUB_APP

    # Still unchanged after an OAuth client is attached — the pair is a separate seam.
    svc.set_oauth_client(app.id, "Iv1.abc123", SECRET)
    assert svc.get_bearer_token(app.id) == "ghs_minted"
    assert svc.get_bearer_token(pat.id) == "ghp_x"
