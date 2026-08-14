# tests/test_github_user_link_service.py — E27B/T7.
#
# Service seam: ``table_name=""`` (in-memory local fallback), injected ``new_id`` /
# ``new_verifier`` / ``now``, moto Secrets Manager (the ONE collaborator whose exact
# ``create_secret``/``put_secret_value``/``delete_secret`` semantics this service depends on —
# same choice ``test_connection_service.py`` makes), and EVERY GitHub call injected as a fake.
# No ``httpx``, no live GitHub, no live AWS. The refresh guard lives in its own suite
# (``test_github_user_link_refresh.py``).

import json
import logging
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from datetime import datetime, timezone
from moto import mock_aws

from models.github_link import LinkStatus
from services.connection_service import ConnectionError as ConnectionServiceError
from services.github_user_link import (
    LINK_CALLBACK_PATH,
    GitHubLinkError,
    GitHubUserLinkService,
)
from services.github_user_oauth import GitHubOAuthError

FIXED = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

# Credential-shaped literals. Every "nothing leaked" assertion is written against these, so
# they must never appear in a message, a `.kind`, or a persisted record.
ACCESS = "ghu_access_token_value"
REFRESH = "ghr_refresh_token_value"
CLIENT_SECRET = "cs_client_secret_value"
CLIENT_ID = "Iv1.abc123"

GH_ID = 583231
GH_LOGIN = "octocat"

REDIRECT = f"https://console.example.com{LINK_CALLBACK_PATH}"

SECRET_PREFIX = "agp-test/github-user-link/"


def _loader_ok(connection_id):
    return (CLIENT_ID, CLIENT_SECRET)


def _loader_raises(connection_id):
    # Task 6's `get_oauth_client_credentials` raises `ConnectionError(kind="bad_request")` both
    # for a GHE (non-null base_url) connection and for a connection with no stored OAuth client.
    # T7 must not care WHICH of those it is — but it must care that they are DETERMINISTIC, so
    # the real exception (kind and all) is what the double raises. A bare RuntimeError here would
    # let "no client configured" and "the store blipped" look identical, which is precisely the
    # conflation that let a throttle purge a live grant.
    raise ConnectionServiceError(
        "per-user GitHub linking is available for github.com connections only", kind="bad_request"
    )


def _loader_faults(connection_id):
    # The SAME loader, failing TRANSIENTLY: it reads DynamoDB and Secrets Manager, so a throttle
    # is ordinary. This must never read as "this org has no OAuth client".
    raise ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "GetItem"
    )


def _exchange_ok(**kwargs):
    return {
        "access_token": ACCESS,
        "refresh_token": REFRESH,
        "expires_in": 28800,
        "refresh_token_expires_in": 15897600,
    }


def _identity_ok(access_token, **kwargs):
    return {"github_id": GH_ID, "github_login": GH_LOGIN}


def _svc(
    *,
    ids=None,
    loader=_loader_ok,
    exchange=None,
    identity=None,
    refresh=None,
    revoke=None,
    verifier="verifier-1",
    now=None,
    allowed_origins=(),
):
    ids = iter(ids or ["state-1", "link-1", "state-2", "link-2", "state-3", "link-3"])
    return GitHubUserLinkService(
        table_name="",  # in-memory local fallback
        secret_prefix=SECRET_PREFIX,
        region="us-east-1",
        allowed_origins=allowed_origins,
        client_credentials_loader=loader,
        secrets_client=boto3.client("secretsmanager", region_name="us-east-1"),
        new_id=lambda: next(ids),
        new_verifier=lambda: verifier,
        now=now or (lambda: FIXED),
        exchange_code=exchange or MagicMock(side_effect=_exchange_ok),
        refresh_user_token=refresh or MagicMock(),
        fetch_user_identity=identity or MagicMock(side_effect=_identity_ok),
        revoke_grant=revoke or MagicMock(return_value=None),
    )


def _link(svc, oid="oid-1", conn="c-1", redirect=REDIRECT):
    """Drive a full begin→complete so a test can start from a LINKED row."""
    _url, state = svc.begin_link(oid, conn, redirect)
    return svc.complete_link(oid, "code-1", state)


def _secret_body(link_id):
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    return json.loads(sm.get_secret_value(SecretId=f"{SECRET_PREFIX}{link_id}")["SecretString"])


# --------------------------------------------------------------------------- #
# begin_link
# --------------------------------------------------------------------------- #


@mock_aws
def test_begin_link_refuses_a_missing_oid():
    svc = _svc()
    with pytest.raises(GitHubLinkError) as ei:
        svc.begin_link("", "c-1", REDIRECT)
    assert ei.value.kind == "bad_request"
    assert svc._state == {}


@mock_aws
def test_begin_link_without_an_oauth_client_is_oauth_client_missing():
    svc = _svc(loader=_loader_raises)
    with pytest.raises(GitHubLinkError) as ei:
        svc.begin_link("oid-1", "c-1", REDIRECT)
    assert ei.value.kind == "oauth_client_missing"
    assert svc._state == {}


@mock_aws
@pytest.mark.parametrize(
    "redirect",
    [
        "https://console.example.com/ops/connections/callback",  # the OTHER callback
        "https://console.example.com/",
        "https://console.example.com" + LINK_CALLBACK_PATH + "/extra",
        "https://evil.example.com/ops/github-link/callback?next=x",  # extra params
    ],
)
def test_begin_link_rejects_a_foreign_redirect_path(redirect):
    svc = _svc()
    with pytest.raises(GitHubLinkError) as ei:
        svc.begin_link("oid-1", "c-1", redirect)
    assert ei.value.kind == "bad_request"


@mock_aws
@pytest.mark.parametrize(
    "redirect",
    [
        # F1. Every one of these passed a path-only check. The suffix and userinfo forms are the
        # dangerous ones: they read as the console's own origin at a glance, so AGP would be
        # vouching for a phishing-grade authorize URL. GitHub's byte-for-byte match against its
        # one registered callback is a backstop AGP does not own.
        f"https://evil.example.com{LINK_CALLBACK_PATH}",
        f"https://console.example.com.evil.io{LINK_CALLBACK_PATH}",  # NOT a suffix match
        f"https://evilconsole.example.com{LINK_CALLBACK_PATH}",  # NOT a prefix extension
        f"https://evil.console.example.com{LINK_CALLBACK_PATH}",  # NOT a subdomain match
        f"https://user:pass@evil.example.com{LINK_CALLBACK_PATH}",  # userinfo hides the host
        f"https://console.example.com@evil.example.com{LINK_CALLBACK_PATH}",
        f"https://console.example.com:8443{LINK_CALLBACK_PATH}",  # a different port is a
        # different origin
        f"http://console.example.com{LINK_CALLBACK_PATH}",  # scheme is part of the origin
    ],
)
def test_begin_link_rejects_a_foreign_redirect_origin(redirect):
    svc = _svc(allowed_origins=("https://console.example.com",))
    with pytest.raises(GitHubLinkError) as ei:
        svc.begin_link("oid-1", "c-1", redirect)
    assert ei.value.kind == "bad_request"
    assert svc._state == {}  # nothing minted for a foreign origin


@mock_aws
def test_begin_link_accepts_the_configured_origin():
    svc = _svc(allowed_origins=("https://console.example.com", "http://localhost:5173"))
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)
    assert state == "state-1"
    second = _svc(
        allowed_origins=("https://console.example.com", "http://localhost:5173")
    )
    _url, state = second.begin_link("oid-1", "c-1", f"http://localhost:5173{LINK_CALLBACK_PATH}")
    assert state == "state-1"


@mock_aws
def test_begin_link_rejects_userinfo_even_with_no_configured_origins():
    # The floor still holds when T8 has not wired the origin list: an authority a human cannot
    # read is refused regardless.
    svc = _svc()
    with pytest.raises(GitHubLinkError) as ei:
        svc.begin_link("oid-1", "c-1", f"https://user:pass@console.example.com{LINK_CALLBACK_PATH}")
    assert ei.value.kind == "bad_request"


@mock_aws
def test_an_empty_allowed_origins_warns_loudly_instead_of_failing_open_silently():
    # The empty default fails OPEN to the https/localhost floor, which is deliberate — a T8
    # wiring miss must not break every link. But a silent fail-open means nobody notices that the
    # host is unchecked, so it is WARNED about at construction (once, not per request) and the
    # semantics are left exactly as they were.
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    logger = logging.getLogger("services.github_user_link")
    logger.addHandler(handler)
    try:
        _svc(allowed_origins=())
        assert any(
            r.levelno == logging.WARNING and "allowed_origins" in r.getMessage() for r in records
        ), "a fail-open origin check must not be silent"
        records.clear()
        _svc(allowed_origins=("https://console.example.com",))
        assert not [r for r in records if r.levelno >= logging.WARNING]
    finally:
        logger.removeHandler(handler)


@mock_aws
def test_begin_link_rejects_a_plain_http_non_localhost_redirect():
    svc = _svc()
    with pytest.raises(GitHubLinkError) as ei:
        svc.begin_link("oid-1", "c-1", f"http://console.example.com{LINK_CALLBACK_PATH}")
    assert ei.value.kind == "bad_request"


@mock_aws
def test_begin_link_allows_plain_http_on_localhost():
    svc = _svc()
    url, state = svc.begin_link("oid-1", "c-1", f"http://localhost:5173{LINK_CALLBACK_PATH}")
    assert state == "state-1"
    assert url.startswith("https://github.com/login/oauth/authorize?")


@mock_aws
def test_begin_link_persists_verifier_and_oid_in_the_state():
    svc = _svc(verifier="v-abc")
    url, state = svc.begin_link("oid-1", "c-1", REDIRECT)

    assert state == "state-1"
    record = svc._state[state]
    assert record == {
        "principal_oid": "oid-1",
        "connection_id": "c-1",
        "redirect_uri": REDIRECT,
        "code_verifier": "v-abc",
        "exp": int(FIXED.timestamp()) + 900,
    }
    # The authorize URL carries the S256 CHALLENGE, never the verifier itself.
    assert "code_challenge_method=S256" in url
    assert "v-abc" not in url
    assert f"client_id={CLIENT_ID}".replace(".", "%2E") in url or "client_id=Iv1" in url
    assert "scope" not in url


# --------------------------------------------------------------------------- #
# complete_link
# --------------------------------------------------------------------------- #


@mock_aws
def test_complete_link_deletes_the_state_before_validating():
    # An EXPIRED state still gets deleted — single-use regardless of validity.
    clock = {"t": FIXED}
    svc = _svc(now=lambda: clock["t"])
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)
    clock["t"] = FIXED.replace(hour=13)  # +1h ⇒ past the 900s TTL

    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-1", "code-1", state)
    assert ei.value.kind == "bad_request"
    assert svc._state == {}  # consumed anyway
    assert svc._local == {}


@mock_aws
def test_complete_link_with_an_unknown_state_is_bad_request():
    svc = _svc()
    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-1", "code-1", "never-issued")
    assert ei.value.kind == "bad_request"


@mock_aws
def test_complete_link_refuses_a_state_issued_to_another_oid():
    # The forgery guard: the session that finishes must be the session that started.
    exchange = MagicMock(side_effect=_exchange_ok)
    svc = _svc(exchange=exchange)
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)

    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-2", "code-1", state)
    assert ei.value.kind == "bad_request"
    exchange.assert_not_called()
    assert svc._local == {}


@mock_aws
def test_complete_link_uses_the_states_redirect_uri_not_the_requests():
    exchange = MagicMock(side_effect=_exchange_ok)
    svc = _svc(exchange=exchange, verifier="v-abc")
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)

    svc.complete_link("oid-1", "code-1", state)

    kwargs = exchange.call_args.kwargs
    assert kwargs["redirect_uri"] == REDIRECT
    assert kwargs["code_verifier"] == "v-abc"
    assert kwargs["code"] == "code-1"
    assert kwargs["client_id"] == CLIENT_ID and kwargs["client_secret"] == CLIENT_SECRET


@mock_aws
def test_complete_link_stores_numeric_id_as_key_and_login_as_label():
    svc = _svc()
    link = _link(svc)

    assert link.id == "link-1"
    assert link.principal_oid == "oid-1" and link.connection_id == "c-1"
    assert link.github_id == GH_ID and link.github_login == GH_LOGIN
    assert link.status == LinkStatus.LINKED
    assert link.token_version == 0
    assert link.secret_arn and link.created_at == FIXED.isoformat()
    # 28800s from the fixed clock, and the refresh window 6 months out.
    assert link.access_token_expires_at == FIXED.replace(hour=20).isoformat()
    assert link.refresh_token_expires_at is not None
    # The token NEVER reaches the row.
    dumped = json.loads(link.model_dump_json())
    assert ACCESS not in json.dumps(dumped) and REFRESH not in json.dumps(dumped)
    # ...it reaches Secrets Manager, with exactly the pinned two keys.
    assert _secret_body("link-1") == {"access_token": ACCESS, "refresh_token": REFRESH}
    # sk is the composite (oid, connection).
    assert list(svc._local) == ["oid-1#c-1"]


@mock_aws
def test_complete_link_conflicts_when_the_github_id_is_bound_elsewhere():
    svc = _svc()
    _link(svc, oid="oid-1", conn="c-1")

    # A DIFFERENT Entra human lands on the SAME GitHub account.
    _url, state = svc.begin_link("oid-2", "c-1", REDIRECT)
    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-2", "code-2", state)
    assert ei.value.kind == "conflict"
    assert list(svc._local) == ["oid-1#c-1"]  # nothing persisted for oid-2


@mock_aws
def test_the_same_github_id_may_be_linked_twice_by_the_same_human():
    # Two org connections, one human, one GitHub account — legitimate.
    svc = _svc()
    _link(svc, oid="oid-1", conn="c-1")
    second = _link(svc, oid="oid-1", conn="c-2")
    assert second.connection_id == "c-2"
    assert sorted(svc._local) == ["oid-1#c-1", "oid-1#c-2"]


@mock_aws
def test_complete_link_uniqueness_read_fails_closed():
    svc = _svc()
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)
    # Flip into DDB mode with a table that cannot be read.
    svc.table_name = "connections"
    svc._table = MagicMock()
    svc._table.query.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Query"
    )
    svc._table.get_item.return_value = {
        "Item": {
            "pk": "github_link_state",
            "sk": state,
            "principal_oid": "oid-1",
            "connection_id": "c-1",
            "redirect_uri": REDIRECT,
            "code_verifier": "verifier-1",
            "exp": int(FIXED.timestamp()) + 900,
        }
    }

    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-1", "code-1", state)
    assert ei.value.kind == "conflict"  # unreadable ⇒ a conflict cannot be ruled out
    svc._table.put_item.assert_not_called()


@mock_aws
def test_complete_link_persists_nothing_when_the_exchange_fails():
    exchange = MagicMock(
        side_effect=GitHubOAuthError("GitHub rejected the code exchange (bad_verification_code)", kind="bad_grant")
    )
    identity = MagicMock(side_effect=_identity_ok)
    svc = _svc(exchange=exchange, identity=identity)
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)

    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-1", "code-1", state)
    assert ei.value.kind == "bad_request"  # a spent code ⇒ re-run the web flow
    identity.assert_not_called()
    assert svc._local == {}
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    with pytest.raises(sm.exceptions.ResourceNotFoundException):
        sm.get_secret_value(SecretId=f"{SECRET_PREFIX}link-1")


@mock_aws
def test_complete_link_persists_nothing_when_the_identity_body_is_malformed():
    # T1 REFUSES a string-typed `id` rather than coercing, so provider junk arrives as
    # provider_error — not a silent success with a garbage join key.
    identity = MagicMock(
        side_effect=GitHubOAuthError("GitHub returned an incomplete user identity", kind="provider_error")
    )
    svc = _svc(identity=identity)
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)

    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-1", "code-1", state)
    assert ei.value.kind == "provider_error"
    assert svc._local == {}


@mock_aws
def test_a_failed_row_write_rolls_the_credential_back_instead_of_orphaning_it():
    # F2. The secret is created before the row. An unguarded row write would, on a DDB fault,
    # leave a LIVE ghu_/ghr_ pair for a real human in Secrets Manager with no row pointing at
    # it: unfindable (no row → no link_id) and unrevocable (unlink needs the row) for the whole
    # 6-month refresh window. The credential must be killed at GitHub and deleted locally.
    revoke = MagicMock(return_value=None)
    svc = _svc(revoke=revoke)
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)

    real_save = svc._save
    svc._save = MagicMock(
        side_effect=ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "PutItem"
        )
    )

    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-1", "code-1", state)
    # This service's own vocabulary, not a raw ClientError that would 500 past T8's mapping.
    assert ei.value.kind == "secret_error"

    # Killed at GitHub — forgetting a live grant is worse than failing the link.
    assert revoke.call_args.kwargs["access_token"] == ACCESS
    # ...and no orphan left behind.
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    with pytest.raises(sm.exceptions.ResourceNotFoundException):
        sm.get_secret_value(SecretId=f"{SECRET_PREFIX}link-1")
    svc._save = real_save


@mock_aws
def test_complete_link_without_expires_in_stores_no_expiry():
    exchange = MagicMock(
        return_value={
            "access_token": ACCESS,
            "refresh_token": None,
            "expires_in": None,
            "refresh_token_expires_in": None,
        }
    )
    svc = _svc(exchange=exchange)
    link = _link(svc)
    assert link.access_token_expires_at is None
    assert link.refresh_token_expires_at is None
    assert _secret_body("link-1") == {"access_token": ACCESS, "refresh_token": None}


@mock_aws
def test_relink_after_unlink_mints_a_new_link_id_and_secret():
    svc = _svc()
    first = _link(svc)
    svc.unlink("oid-1", "c-1")
    second = _link(svc)

    assert first.id == "link-1" and second.id == "link-2"
    assert first.secret_arn != second.secret_arn
    assert _secret_body("link-2") == {"access_token": ACCESS, "refresh_token": REFRESH}


@mock_aws
def test_relink_over_a_live_link_reuses_the_sk_and_purges_the_old_secret():
    svc = _svc()
    _link(svc)
    second = _link(svc)  # same (oid, connection), no unlink in between

    assert second.id == "link-2"
    assert list(svc._local) == ["oid-1#c-1"]  # ONE row, same composite sk
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    with pytest.raises(sm.exceptions.ResourceNotFoundException):
        sm.get_secret_value(SecretId=f"{SECRET_PREFIX}link-1")


@mock_aws
def test_list_for_principal_returns_only_that_principals_rows():
    svc = _svc(ids=["s1", "l1", "s2", "l2", "s3", "l3"])
    _link(svc, oid="oid-1", conn="c-1")
    _link(svc, oid="oid-1", conn="c-2")
    identity = MagicMock(return_value={"github_id": 999, "github_login": "hubot"})
    svc._fetch_user_identity = identity
    _link(svc, oid="oid-2", conn="c-1")

    rows = svc.list_for_principal("oid-1")
    assert sorted(r.connection_id for r in rows) == ["c-1", "c-2"]
    assert all(r.principal_oid == "oid-1" for r in rows)


# --------------------------------------------------------------------------- #
# verify_link
# --------------------------------------------------------------------------- #


@mock_aws
def test_verify_updates_the_login_but_never_the_github_id():
    svc = _svc()
    _link(svc)
    # The probe answers with a renamed login AND (hypothetically) a different id. Only the
    # LABEL moves: github_id is the join key and this row's binding, so it is never rewritten.
    svc._fetch_user_identity = MagicMock(
        return_value={"github_id": 111, "github_login": "octocat-renamed"}
    )

    verified = svc.verify_link("oid-1", "c-1")
    assert verified.github_login == "octocat-renamed"
    assert verified.github_id == GH_ID
    assert verified.last_verified_at == FIXED.isoformat()
    assert verified.status == LinkStatus.LINKED
    assert svc._local["oid-1#c-1"].github_login == "octocat-renamed"


@mock_aws
def test_verify_401_marks_the_link_unlinked_and_raises_link_revoked():
    svc = _svc()
    _link(svc)
    svc._fetch_user_identity = MagicMock(
        side_effect=GitHubOAuthError("GitHub rejected the user token (HTTP 401)", kind="revoked")
    )

    with pytest.raises(GitHubLinkError) as ei:
        svc.verify_link("oid-1", "c-1")
    assert ei.value.kind == "link_revoked"
    assert svc._local["oid-1#c-1"].status == LinkStatus.UNLINKED


@mock_aws
def test_verify_unknown_link_is_not_found():
    svc = _svc()
    with pytest.raises(GitHubLinkError) as ei:
        svc.verify_link("oid-1", "c-1")
    assert ei.value.kind == "not_found"


@mock_aws
def test_an_unreadable_row_is_not_reported_as_not_found():
    # A DDB blip must not tell a linked human "not found" (which would send them round the
    # whole web flow again) — it is a retryable store fault.
    svc = _svc()
    svc.table_name = "connections"
    svc._table = MagicMock()
    svc._table.get_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "GetItem"
    )
    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    assert ei.value.kind == "secret_error"


# --------------------------------------------------------------------------- #
# unlink
# --------------------------------------------------------------------------- #


@mock_aws
def test_unlink_revokes_at_github_then_purges_secret_then_row():
    revoke = MagicMock(return_value=None)
    svc = _svc(revoke=revoke)
    _link(svc)

    svc.unlink("oid-1", "c-1")

    kwargs = revoke.call_args.kwargs
    assert kwargs["client_id"] == CLIENT_ID and kwargs["client_secret"] == CLIENT_SECRET
    assert kwargs["access_token"] == ACCESS  # the LIVE token, so the grant really dies
    assert svc._local == {}
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    with pytest.raises(sm.exceptions.ResourceNotFoundException):
        sm.get_secret_value(SecretId=f"{SECRET_PREFIX}link-1")


@mock_aws
def test_unlink_does_not_report_success_when_the_revoke_failed():
    # F3. Purging here would tell the human their authorization is gone while the grant is LIVE
    # at GitHub for up to 6 months, AND destroy the only token that could revoke it — after
    # which a retry cannot even find the link. A retryable failure is strictly better.
    revoke = MagicMock(
        side_effect=GitHubOAuthError("GitHub declined the authorization revocation (HTTP 500)")
    )
    svc = _svc(revoke=revoke)
    _link(svc)

    with pytest.raises(GitHubLinkError) as ei:
        svc.unlink("oid-1", "c-1")
    assert ei.value.kind == "provider_error"  # retryable, and honest
    revoke.assert_called_once()

    # The token survives, so a retry can actually revoke...
    assert _secret_body("link-1") == {"access_token": ACCESS, "refresh_token": REFRESH}
    # ...and the row survives so the link is still findable — shown UNLINKED, not "linked".
    assert svc._local["oid-1#c-1"].status == LinkStatus.UNLINKED
    # No token is handed out in the meantime.
    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    assert ei.value.kind == "link_revoked"


@mock_aws
def test_a_retried_unlink_succeeds_once_github_recovers():
    # The whole point of refusing to lie: the human can try again and it actually works.
    revoke = MagicMock(
        side_effect=GitHubOAuthError("GitHub declined the authorization revocation (HTTP 500)")
    )
    svc = _svc(revoke=revoke)
    _link(svc)
    with pytest.raises(GitHubLinkError):
        svc.unlink("oid-1", "c-1")

    svc._revoke_grant = MagicMock(return_value=None)  # GitHub recovers
    svc.unlink("oid-1", "c-1")

    assert svc._revoke_grant.call_args.kwargs["access_token"] == ACCESS  # the LIVE token
    assert svc._local == {}
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    with pytest.raises(sm.exceptions.ResourceNotFoundException):
        sm.get_secret_value(SecretId=f"{SECRET_PREFIX}link-1")


@mock_aws
def test_unlink_still_purges_when_the_oauth_client_is_gone():
    # The admin rotated/removed the OAuth client: revoke is impossible, but the row must go.
    svc = _svc()
    _link(svc)
    svc._load_credentials = MagicMock(side_effect=GitHubLinkError("no client", kind="oauth_client_missing"))

    svc.unlink("oid-1", "c-1")
    assert svc._local == {}


@mock_aws
def test_a_transient_loader_fault_does_not_purge_without_revoking():
    # F3 again, through the OTHER door. `unlink` purges without revoking when the OAuth client is
    # GONE — defensible, because AGP could then never call revoke again. But the loader reads DDB
    # and Secrets Manager, so it also fails transiently, and collapsing a throttle into "no client
    # configured" reproduced the original defect exactly: 204 SUCCESS, revoke never called, and
    # the only token that could revoke a grant STILL LIVE at GitHub destroyed.
    revoke = MagicMock(return_value=None)
    svc = _svc(revoke=revoke)
    _link(svc)
    svc._client_credentials_loader = _loader_faults

    with pytest.raises(GitHubLinkError) as ei:
        svc.unlink("oid-1", "c-1")
    assert ei.value.kind == "secret_error"  # retryable, NOT oauth_client_missing
    revoke.assert_not_called()

    # Nothing was destroyed on a blip: the token is still there to revoke with, and the row is
    # still there to find it by.
    assert _secret_body("link-1") == {"access_token": ACCESS, "refresh_token": REFRESH}
    assert svc._local["oid-1#c-1"].status == LinkStatus.LINKED

    # And once the store recovers, the retry does the real thing.
    svc._client_credentials_loader = _loader_ok
    svc.unlink("oid-1", "c-1")
    assert revoke.call_args.kwargs["access_token"] == ACCESS
    assert svc._local == {}


@mock_aws
def test_a_transient_loader_fault_is_retryable_on_begin_link_too():
    # The same classification, on the read path: a store blip must not tell an admin their org
    # needs a one-time OAuth-client paste it already has.
    svc = _svc(loader=_loader_faults)
    with pytest.raises(GitHubLinkError) as ei:
        svc.begin_link("oid-1", "c-1", REDIRECT)
    assert ei.value.kind == "secret_error"
    assert svc._state == {}


@mock_aws
def test_unlink_unknown_link_is_not_found():
    svc = _svc()
    with pytest.raises(GitHubLinkError) as ei:
        svc.unlink("oid-1", "c-1")
    assert ei.value.kind == "not_found"


# --------------------------------------------------------------------------- #
# The absolute rule: no credential in a message, ever.
# --------------------------------------------------------------------------- #


def _boom_oauth(*a, **k):
    # A provider error whose message is SAFE by construction (T1's contract) — this test
    # guards T7 against re-wrapping something unsafe of its own.
    raise GitHubOAuthError("GitHub declined the code exchange (HTTP 500)")


@mock_aws
@pytest.mark.parametrize(
    "scenario",
    ["no_oauth_client", "bad_redirect", "unknown_state", "foreign_state", "exchange_failed", "conflict", "revoked", "not_found"],
)
def test_no_error_message_contains_a_token_or_the_client_secret(scenario):
    forbidden = (ACCESS, REFRESH, CLIENT_SECRET, "code-1", "verifier-1")

    if scenario == "no_oauth_client":
        svc = _svc(loader=_loader_raises)
        call = lambda: svc.begin_link("oid-1", "c-1", REDIRECT)  # noqa: E731
    elif scenario == "bad_redirect":
        svc = _svc()
        call = lambda: svc.begin_link("oid-1", "c-1", "https://x.example.com/nope")  # noqa: E731
    elif scenario == "unknown_state":
        svc = _svc()
        call = lambda: svc.complete_link("oid-1", "code-1", "nope")  # noqa: E731
    elif scenario == "foreign_state":
        svc = _svc()
        _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)
        call = lambda: svc.complete_link("oid-2", "code-1", state)  # noqa: E731
    elif scenario == "exchange_failed":
        svc = _svc(exchange=MagicMock(side_effect=_boom_oauth))
        _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)
        call = lambda: svc.complete_link("oid-1", "code-1", state)  # noqa: E731
    elif scenario == "conflict":
        svc = _svc()
        _link(svc, oid="oid-1", conn="c-1")
        _url, state = svc.begin_link("oid-2", "c-1", REDIRECT)
        call = lambda: svc.complete_link("oid-2", "code-1", state)  # noqa: E731
    elif scenario == "revoked":
        svc = _svc()
        _link(svc)
        svc._fetch_user_identity = MagicMock(
            side_effect=GitHubOAuthError("GitHub rejected the user token (HTTP 401)", kind="revoked")
        )
        call = lambda: svc.verify_link("oid-1", "c-1")  # noqa: E731
    else:
        svc = _svc()
        call = lambda: svc.unlink("oid-1", "c-1")  # noqa: E731

    with pytest.raises(GitHubLinkError) as ei:
        call()
    blob = f"{ei.value.message}|{ei.value}|{ei.value.args}"
    for bad in forbidden:
        assert bad not in blob


# --------------------------------------------------------------------------- #
# NO RAW boto3 EXCEPTION LEAVES THIS SERVICE (review-8 Important #1).
#
# Every store operation the write/state paths reach used to be unguarded, so a throttle, an IAM
# hiccup or a connect timeout escaped the `.kind` vocabulary as a raw boto3 exception and became
# an HTTP 500 on all five link routes — outside the {400,404,409,502} set this epic pins, and
# unactionable where a retryable 502 was intended. These tests pin one call site each, in BOTH
# boto3 shapes: `ClientError` (the service answered and refused) and `BotoCoreError` (the request
# never arrived — NOT a ClientError subclass, so a guard naming only ClientError misses it).
# --------------------------------------------------------------------------- #

_LINK_PK = "github_user_link"
_STATE_PK = "github_link_state"

# Deliberately noisy, credential-shaped boto3 text. Real throttle messages carry a table name and
# a request id; none of it may reach a `GitHubLinkError.message` and therefore an HTTP body.
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
    # The half that is NOT a ClientError: the request never reached DynamoDB at all. Ordinary
    # from ECS (a VPC endpoint blip, a DNS stall) and the exact shape the original guards missed.
    return EndpointConnectionError(endpoint_url=f"https://{_BOTO_MARKER}.example.invalid/")


# Both shapes, run against every guarded call site below.
_FAULTS = [("client_error", _throttle), ("boto_core_error", _unreachable)]


class _FaultTable:
    """A minimal ("pk","sk") Table double that breaks exactly ONE operation.

    Everything else stores and reads normally, so a test can drive a whole real flow and fault a
    single call rather than a whole table. ``only_pk`` narrows the break to one partition, which
    is what lets a test fault the LINK-row write while ``begin_link``'s state write still lands."""

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

    def put_item(self, Item, ConditionExpression=None):  # noqa: N803 — boto3 param name
        self._maybe_fail("put_item", Item["pk"])
        self.items[(Item["pk"], Item["sk"])] = dict(Item)

    def delete_item(self, Key):  # noqa: N803 — boto3 param name
        self._maybe_fail("delete_item", Key["pk"])
        self.items.pop((Key["pk"], Key["sk"]), None)

    def update_item(  # noqa: N803 — boto3 param names
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeValues,
        ReturnValues=None,
        ConditionExpression=None,
    ):
        self._maybe_fail("update_item", Key["pk"])
        key = (Key["pk"], Key["sk"])
        merged = dict(self.items.get(key) or Key)
        for clause in UpdateExpression.strip()[4:].split(","):
            name, placeholder = (p.strip() for p in clause.split("="))
            merged[name] = ExpressionAttributeValues[placeholder]
        self.items[key] = merged
        return {"Attributes": dict(merged)} if ReturnValues == "ALL_NEW" else {}

    def query(self, **kwargs):
        expr = kwargs["KeyConditionExpression"]
        if expr.expression_operator == "AND":
            pk_cond, sk_cond = expr._values
            pk, prefix = pk_cond._values[1], sk_cond._values[1]
        else:
            pk, prefix = expr._values[1], None
        self._maybe_fail("query", pk)
        rows = [dict(i) for k, i in self.items.items() if k[0] == pk]
        if prefix is not None:
            rows = [i for i in rows if str(i.get("sk", "")).startswith(prefix)]
        return {"Items": rows}


def _to_ddb(svc, table):
    """Flip an in-memory service into DDB mode over ``table``, carrying any existing rows across
    so a test can build state locally and then fault the store."""
    for sk, row in svc._local.items():
        table.items[(_LINK_PK, sk)] = {
            "pk": _LINK_PK,
            "sk": sk,
            **json.loads(row.model_dump_json()),
        }
    svc.table_name = "connections"
    svc._table = table
    svc._local.clear()
    assert svc._has_ddb is True
    return svc


def _assert_retryable_and_quiet(exc_info, site=""):
    """The whole contract in one place: this service's own error, a RETRYABLE kind the routes map
    to 502, and not one byte of boto3 internals in anything a route could serialize.

    ``site`` labels the call site when a loop drives several, so a failure names which one."""
    err = exc_info.value
    assert isinstance(err, GitHubLinkError), site
    assert err.kind == "secret_error", site
    blob = f"{err.message}|{err}|{err.args}"
    assert _BOTO_MARKER not in blob, site
    for bad in ("ProvisionedThroughputExceeded", "endpoint", "Traceback", ACCESS, REFRESH):
        assert bad not in blob, site


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_a_state_write_fault_is_retryable_not_a_500(name, fault):
    # `_save_state`, reached from `begin_link`. Aborting is right: with no stored state the
    # callback could never be validated, so handing back an authorize URL would send the human to
    # GitHub to earn a code this service is guaranteed to refuse.
    svc = _to_ddb(_svc(), _FaultTable(fail_on="put_item", fault=fault(), only_pk=_STATE_PK))

    with pytest.raises(GitHubLinkError) as ei:
        svc.begin_link("oid-1", "c-1", REDIRECT)
    _assert_retryable_and_quiet(ei)


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_a_state_consume_fault_is_retryable_and_never_exchanges_the_code(name, fault):
    # `_delete_state`, reached from `complete_link`. That delete is what makes the state
    # single-use, so continuing past a failed one would exchange the code against a state row
    # still sitting in the table — i.e. spend a state that is STILL REPLAYABLE.
    exchange = MagicMock(side_effect=_exchange_ok)
    svc = _svc(exchange=exchange)
    _to_ddb(svc, _FaultTable(fail_on="delete_item", fault=fault(), only_pk=_STATE_PK))
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)

    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-1", "code-1", state)
    _assert_retryable_and_quiet(ei)
    exchange.assert_not_called()  # nothing redeemed, so the abort costs the human nothing


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_a_link_row_write_fault_rolls_the_credential_back_and_stays_in_contract(name, fault):
    # `_save`, reached from `complete_link` — the ONE window where a LIVE ghu_/ghr_ pair exists
    # with no row pointing at it. Two things must hold together: the error speaks this service's
    # vocabulary (a raw boto3 error would 500), AND the rollback still fires. The BotoCoreError
    # case is the one that used to fail BOTH ways at once — it escaped as a 500 and slipped past
    # the rollback's `except ClientError`, orphaning the pair for the full 6-month window.
    revoke = MagicMock(return_value=None)
    svc = _svc(revoke=revoke)
    _to_ddb(svc, _FaultTable(fail_on="put_item", fault=fault(), only_pk=_LINK_PK))
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)

    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-1", "code-1", state)
    _assert_retryable_and_quiet(ei)

    assert revoke.call_args.kwargs["access_token"] == ACCESS  # killed at GitHub
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    with pytest.raises(sm.exceptions.ResourceNotFoundException):  # and no orphan left behind
        sm.get_secret_value(SecretId=f"{SECRET_PREFIX}link-1")


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_a_link_row_delete_fault_is_retryable_not_a_500(name, fault):
    # `_delete`, reached from `unlink`. By this point the grant IS revoked at GitHub, so the row
    # is a stale husk and the honest answer is "retry" — never 500.
    revoke = MagicMock(return_value=None)
    svc = _svc(revoke=revoke)
    _link(svc)
    _to_ddb(svc, _FaultTable(fail_on="delete_item", fault=fault(), only_pk=_LINK_PK))

    with pytest.raises(GitHubLinkError) as ei:
        svc.unlink("oid-1", "c-1")
    _assert_retryable_and_quiet(ei)
    revoke.assert_called_once()  # the revoke DID happen; only the row write failed


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_a_row_read_fault_is_retryable_not_not_found(name, fault):
    # `_require_row`. Already guarded for ClientError; the BotoCoreError half was open. It must
    # never read as `not_found` either — telling a linked human "no link" sends them through the
    # whole web flow again to fix a blip.
    svc = _svc()
    _link(svc)
    _to_ddb(svc, _FaultTable(fail_on="get_item", fault=fault(), only_pk=_LINK_PK))

    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    _assert_retryable_and_quiet(ei)


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_a_partition_read_fault_on_the_list_path_is_retryable(name, fault):
    # `list_for_principal` → `_scan_partition`. Same story: guarded for ClientError, open to a
    # BotoCoreError.
    svc = _svc()
    _link(svc)
    _to_ddb(svc, _FaultTable(fail_on="query", fault=fault(), only_pk=_LINK_PK))

    with pytest.raises(GitHubLinkError) as ei:
        svc.list_for_principal("oid-1")
    _assert_retryable_and_quiet(ei)


@mock_aws
def test_an_unreachable_store_fails_the_uniqueness_guard_closed():
    # `_refuse_foreign_binding`'s strict partition read, on the BotoCoreError half. It must stay
    # FAIL-CLOSED (`conflict`): an unreadable partition can never be mistaken for "no conflict",
    # because that is precisely how one human's GitHub account gets bound to another's oid.
    svc = _svc()
    _to_ddb(svc, _FaultTable(fail_on="query", fault=_unreachable(), only_pk=_LINK_PK))
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)

    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-1", "code-1", state)
    assert ei.value.kind == "conflict"
    assert _BOTO_MARKER not in f"{ei.value.message}|{ei.value.args}"


@mock_aws
def test_an_unreachable_store_on_a_label_write_is_not_mistaken_for_a_deleted_link():
    # `_save_label`'s BotoCoreError clause. The ClientError clause reads `exc.response` to
    # recognize the existence guard's refusal (`not_found`); a BotoCoreError never reached
    # DynamoDB, so it proves NOTHING about whether the row exists and must not read as a link
    # the console should show as vanished.
    svc = _svc()
    _link(svc)
    _to_ddb(svc, _FaultTable(fail_on="update_item", fault=_unreachable(), only_pk=_LINK_PK))

    with pytest.raises(GitHubLinkError) as ei:
        svc.verify_link("oid-1", "c-1")
    _assert_retryable_and_quiet(ei)


@mock_aws
def test_an_unreachable_store_on_a_guarded_write_is_not_mistaken_for_a_lost_claim():
    # `_save_guarded`'s BotoCoreError clause, reached through the claim. Its ClientError clause
    # reads `exc.response` to tell a lost claim from a real fault; a BotoCoreError is never a lost
    # claim, and must not be silently swallowed as one — that would report success on a rotation
    # that never happened.
    refresh = MagicMock(side_effect=AssertionError("GitHub must not be called: the claim failed"))
    svc = _svc(refresh=refresh, now=lambda: FIXED)
    _link(svc)
    # Park the access token inside the refresh skew so `get_user_bearer_token` tries to claim.
    row = svc._local["oid-1#c-1"]
    svc._local["oid-1#c-1"] = row.model_copy(
        update={"access_token_expires_at": FIXED.isoformat()}
    )
    _to_ddb(svc, _FaultTable(fail_on="put_item", fault=_unreachable(), only_pk=_LINK_PK))

    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    _assert_retryable_and_quiet(ei)
    refresh.assert_not_called()  # claim-before-GitHub ordering intact


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_a_secrets_manager_fault_never_escapes_raw_either(name, fault):
    # `_create_secret`'s ResourceExistsException fallback used to be a SIBLING `except`, so the
    # `put_secret_value` on that path could throw straight past the translation — a handler does
    # not catch what another handler raises. Nested now, so both shapes are translated.
    svc = _svc()
    sm = MagicMock()
    sm.exceptions.ResourceExistsException = boto3.client(
        "secretsmanager", region_name="us-east-1"
    ).exceptions.ResourceExistsException
    sm.create_secret.side_effect = sm.exceptions.ResourceExistsException(
        {"Error": {"Code": "ResourceExistsException"}}, "CreateSecret"
    )
    sm.put_secret_value.side_effect = fault()
    svc._sm = sm
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)

    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-1", "code-1", state)
    _assert_retryable_and_quiet(ei)
    assert svc._local == {}  # no row for a secret that was never stored


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_every_guarded_store_operation_translates_at_its_own_call_site(name, fault):
    # The call sites DIRECTLY, not only through their callers. `complete_link` also catches raw
    # boto3 faults around `_save` (belt-and-braces, because that is the one window where a live
    # ghu_/ghr_ pair has no row pointing at it), which MASKS whether `_save` itself translates —
    # so a mutation that strips `_save`'s own guard passes every flow-level test while leaving the
    # next caller of `_save` exposed to a 500. Pinned here at the seam instead.
    row = _link(_svc()).model_copy()
    ops = {
        "_save": ("put_item", _LINK_PK, lambda s: s._save(row)),
        "_delete": ("delete_item", _LINK_PK, lambda s: s._delete("oid-1", "c-1")),
        "_save_state": ("put_item", _STATE_PK, lambda s: s._save_state("st-1", {"a": "b"})),
        "_delete_state": ("delete_item", _STATE_PK, lambda s: s._delete_state("st-1")),
    }
    for op, (call, pk, drive) in ops.items():
        svc = _to_ddb(_svc(), _FaultTable(fail_on=call, fault=fault(), only_pk=pk))
        with pytest.raises(GitHubLinkError) as ei:
            drive(svc)
        _assert_retryable_and_quiet(ei, site=op)
