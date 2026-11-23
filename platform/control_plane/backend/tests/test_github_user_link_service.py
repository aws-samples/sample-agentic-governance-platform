"""The link service over its seams: ``table_name=""`` (the in-memory store), injected ids,
verifier and clock, moto Secrets Manager (the one collaborator whose exact ``create_secret``,
``put_secret_value`` and ``delete_secret`` semantics the service depends on), and every GitHub call
injected as a fake. No ``httpx``, no live GitHub, no live AWS. The refresh guard lives in
``test_github_user_link_refresh``."""

import json
import logging
from datetime import datetime, timezone
from functools import partial
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from moto.core.decorator import mock_aws
from seed import seed

from core import aws_clients
from core.state_store import StateStore
from models.github_link import LinkStatus
from services.connection_service import ConnectionError as ConnectionServiceError
from services.github_user_link import (
    LINK_CALLBACK_PATH,
    GitHubLinkError,
    GitHubUserLinkService,
)
from services.github_user_oauth import GitHubOAuthError

FIXED = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

ACCESS = "ghu_access_token_value"
REFRESH = "ghr_refresh_token_value"
CLIENT_SECRET = "cs_client_secret_value"
CLIENT_ID = "Iv1.abc123"

GH_ID = 583231
GH_LOGIN = "octocat"

REDIRECT = f"https://console.example.com{LINK_CALLBACK_PATH}"

SECRET_PREFIX = "agp-test/github-user-link/"

_LINK_PK = "github_user_link"
_STATE_PK = "github_link_state"


def _loader_ok(connection_id):
    return (CLIENT_ID, CLIENT_SECRET)


def _loader_raises(connection_id):
    """The real loader's deterministic refusal (a GitHub Enterprise connection or no stored OAuth
    client), kind and all, so "no client configured" and "the store blipped" cannot look alike."""
    raise ConnectionServiceError(
        "per-user GitHub linking is available for github.com connections only", kind="bad_request"
    )


def _loader_faults(connection_id):
    """The same loader failing transiently: it reads DynamoDB and Secrets Manager, so a throttle is
    ordinary and must never read as "this org has no OAuth client"."""
    raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "GetItem")


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
    org_loader=None,
):
    ids = iter(ids or ["state-1", "link-1", "state-2", "link-2", "state-3", "link-3"])
    return GitHubUserLinkService(
        table_name="",
        secret_prefix=SECRET_PREFIX,
        region="us-east-1",
        allowed_origins=allowed_origins,
        client_credentials_loader=loader,
        connection_org_loader=org_loader or (lambda cid: "acme"),
        secrets_client=aws_clients.client("secretsmanager", region="us-east-1"),
        new_id=lambda: next(ids),
        new_verifier=lambda: verifier,
        now=now or (lambda: FIXED),
        exchange_code=exchange or MagicMock(side_effect=_exchange_ok),
        refresh_user_token=refresh or MagicMock(),
        fetch_user_identity=identity or MagicMock(side_effect=_identity_ok),
        revoke_grant=revoke or MagicMock(return_value=None),
    )


def _link(svc, oid="oid-1", conn="c-1", redirect=REDIRECT):
    """Drive a full begin and complete so a test can start from a linked row."""
    _url, state = svc.begin_link(oid, conn, redirect)
    return svc.complete_link(oid, "code-1", state)


def _secret_body(link_id):
    sm = aws_clients.client("secretsmanager", region="us-east-1")
    return json.loads(sm.get_secret_value(SecretId=f"{SECRET_PREFIX}{link_id}")["SecretString"])


def _rows(svc):
    """The link partition as stored, keyed by sk."""
    return {item["sk"]: svc._from_item(item) for item in svc._store.query(_LINK_PK)}


def _states(svc):
    """The state partition as stored, keyed by state, the key attributes stripped."""
    return {
        item["sk"]: {k: v for k, v in item.items() if k not in ("pk", "sk")}
        for item in svc._store.query(_STATE_PK)
    }


@mock_aws
def test_begin_link_refuses_a_missing_oid():
    svc = _svc()
    with pytest.raises(GitHubLinkError) as ei:
        svc.begin_link("", "c-1", REDIRECT)
    assert ei.value.kind == "bad_request"
    assert _states(svc) == {}


@mock_aws
def test_begin_link_without_an_oauth_client_is_oauth_client_missing():
    svc = _svc(loader=_loader_raises)
    with pytest.raises(GitHubLinkError) as ei:
        svc.begin_link("oid-1", "c-1", REDIRECT)
    assert ei.value.kind == "oauth_client_missing"
    assert _states(svc) == {}


@mock_aws
@pytest.mark.parametrize(
    "redirect",
    [
        "https://console.example.com/ops/connections/callback",
        "https://console.example.com/",
        "https://console.example.com" + LINK_CALLBACK_PATH + "/extra",
        "https://evil.example.com/ops/github-link/callback?next=x",
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
        f"https://evil.example.com{LINK_CALLBACK_PATH}",
        f"https://console.example.com.evil.io{LINK_CALLBACK_PATH}",
        f"https://evilconsole.example.com{LINK_CALLBACK_PATH}",
        f"https://evil.console.example.com{LINK_CALLBACK_PATH}",
        f"https://user:pass@evil.example.com{LINK_CALLBACK_PATH}",
        f"https://console.example.com@evil.example.com{LINK_CALLBACK_PATH}",
        f"https://console.example.com:8443{LINK_CALLBACK_PATH}",
        f"http://console.example.com{LINK_CALLBACK_PATH}",
    ],
)
def test_begin_link_rejects_a_foreign_redirect_origin(redirect):
    """Every one of these passed a path-only check; a suffix, a subdomain, userinfo, another port or
    another scheme is a different origin, and nothing is minted for it."""
    svc = _svc(allowed_origins=("https://console.example.com",))
    with pytest.raises(GitHubLinkError) as ei:
        svc.begin_link("oid-1", "c-1", redirect)
    assert ei.value.kind == "bad_request"
    assert _states(svc) == {}


@mock_aws
def test_begin_link_accepts_the_configured_origin():
    svc = _svc(allowed_origins=("https://console.example.com", "http://localhost:5173"))
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)
    assert state == "state-1"
    second = _svc(allowed_origins=("https://console.example.com", "http://localhost:5173"))
    _url, state = second.begin_link("oid-1", "c-1", f"http://localhost:5173{LINK_CALLBACK_PATH}")
    assert state == "state-1"


@mock_aws
def test_begin_link_rejects_userinfo_even_with_no_configured_origins():
    svc = _svc()
    with pytest.raises(GitHubLinkError) as ei:
        svc.begin_link("oid-1", "c-1", f"https://user:pass@console.example.com{LINK_CALLBACK_PATH}")
    assert ei.value.kind == "bad_request"


@mock_aws
def test_an_empty_allowed_origins_warns_loudly_instead_of_failing_open_silently():
    """The empty default fails open to the https/localhost floor on purpose, so the miss is warned
    about once at construction and never per request."""
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    logger = logging.getLogger("services.github_user_link")
    logger.addHandler(handler)
    try:
        _svc(allowed_origins=())
        assert any(r.levelno == logging.WARNING and "allowed_origins" in r.getMessage() for r in records), (
            "a fail-open origin check must not be silent"
        )
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
    """The authorize URL carries the S256 challenge, never the verifier itself."""
    svc = _svc(verifier="v-abc")
    url, state = svc.begin_link("oid-1", "c-1", REDIRECT)

    assert state == "state-1"
    record = _states(svc)[state]
    assert record == {
        "principal_oid": "oid-1",
        "connection_id": "c-1",
        "redirect_uri": REDIRECT,
        "code_verifier": "v-abc",
        "exp": int(FIXED.timestamp()) + 900,
    }
    assert "code_challenge_method=S256" in url
    assert "v-abc" not in url
    assert f"client_id={CLIENT_ID}".replace(".", "%2E") in url or "client_id=Iv1" in url
    assert "scope" not in url


@mock_aws
def test_complete_link_deletes_the_state_before_validating():
    """An expired state is still consumed: single-use regardless of validity."""
    clock = {"t": FIXED}
    svc = _svc(now=lambda: clock["t"])
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)
    clock["t"] = FIXED.replace(hour=13)

    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-1", "code-1", state)
    assert ei.value.kind == "bad_request"
    assert _states(svc) == {}
    assert _rows(svc) == {}


@mock_aws
def test_complete_link_with_an_unknown_state_is_bad_request():
    svc = _svc()
    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-1", "code-1", "never-issued")
    assert ei.value.kind == "bad_request"


@mock_aws
def test_complete_link_refuses_a_state_issued_to_another_oid():
    """The session that finishes must be the session that started."""
    exchange = MagicMock(side_effect=_exchange_ok)
    svc = _svc(exchange=exchange)
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)

    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-2", "code-1", state)
    assert ei.value.kind == "bad_request"
    exchange.assert_not_called()
    assert _rows(svc) == {}


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
    """The token never reaches the row; it reaches Secrets Manager with exactly the pinned two
    keys, and the sk is the composite (oid, connection)."""
    svc = _svc()
    link = _link(svc)

    assert link.id == "link-1"
    assert link.principal_oid == "oid-1" and link.connection_id == "c-1"
    assert link.github_id == GH_ID and link.github_login == GH_LOGIN
    assert link.status == LinkStatus.LINKED
    assert link.token_version == 0
    assert link.secret_arn and link.created_at == FIXED.isoformat()
    assert link.access_token_expires_at == FIXED.replace(hour=20).isoformat()
    assert link.refresh_token_expires_at is not None
    dumped = json.loads(link.model_dump_json())
    assert ACCESS not in json.dumps(dumped) and REFRESH not in json.dumps(dumped)
    assert _secret_body("link-1") == {"access_token": ACCESS, "refresh_token": REFRESH}
    assert list(_rows(svc)) == ["oid-1#c-1"]


@mock_aws
def test_complete_link_conflicts_when_the_github_id_is_bound_elsewhere():
    svc = _svc()
    _link(svc, oid="oid-1", conn="c-1")

    _url, state = svc.begin_link("oid-2", "c-1", REDIRECT)
    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-2", "code-2", state)
    assert ei.value.kind == "conflict"
    assert list(_rows(svc)) == ["oid-1#c-1"]


@mock_aws
def test_the_same_github_id_may_be_linked_twice_by_the_same_human():
    svc = _svc()
    _link(svc, oid="oid-1", conn="c-1")
    second = _link(svc, oid="oid-1", conn="c-2")
    assert second.connection_id == "c-2"
    assert sorted(_rows(svc)) == ["oid-1#c-1", "oid-1#c-2"]


@mock_aws
def test_complete_link_uniqueness_read_fails_closed():
    """An unreadable partition cannot rule a conflict out."""
    svc = _svc()
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)
    _faulting(
        svc, "query", ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Query")
    )

    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-1", "code-1", state)
    assert ei.value.kind == "conflict"
    svc._store._table.put_item.assert_not_called()


@mock_aws
def test_complete_link_persists_nothing_when_the_exchange_fails():
    exchange = MagicMock(
        side_effect=GitHubOAuthError(
            "GitHub rejected the code exchange (bad_verification_code)", kind="bad_grant"
        )
    )
    identity = MagicMock(side_effect=_identity_ok)
    svc = _svc(exchange=exchange, identity=identity)
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)

    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-1", "code-1", state)
    assert ei.value.kind == "bad_request"
    identity.assert_not_called()
    assert _rows(svc) == {}
    sm = aws_clients.client("secretsmanager", region="us-east-1")
    with pytest.raises(sm.exceptions.ResourceNotFoundException):
        sm.get_secret_value(SecretId=f"{SECRET_PREFIX}link-1")


@mock_aws
def test_complete_link_persists_nothing_when_the_identity_body_is_malformed():
    identity = MagicMock(
        side_effect=GitHubOAuthError("GitHub returned an incomplete user identity", kind="provider_error")
    )
    svc = _svc(identity=identity)
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)

    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-1", "code-1", state)
    assert ei.value.kind == "provider_error"
    assert _rows(svc) == {}


@mock_aws
def test_a_failed_row_write_rolls_the_credential_back_instead_of_orphaning_it():
    """The secret is created before the row, so a store fault on the row write kills the live
    pair at GitHub and deletes the secret rather than leave it unfindable for six months."""
    revoke = MagicMock(return_value=None)
    svc = _svc(revoke=revoke)
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)

    real_save = svc._save
    svc._save = MagicMock(
        side_effect=ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "PutItem")
    )

    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-1", "code-1", state)
    assert ei.value.kind == "secret_error"

    assert revoke.call_args.kwargs["access_token"] == ACCESS
    sm = aws_clients.client("secretsmanager", region="us-east-1")
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
    second = _link(svc)

    assert second.id == "link-2"
    assert list(_rows(svc)) == ["oid-1#c-1"]
    sm = aws_clients.client("secretsmanager", region="us-east-1")
    with pytest.raises(sm.exceptions.ResourceNotFoundException):
        sm.get_secret_value(SecretId=f"{SECRET_PREFIX}link-1")


def _racing_link_put(svc, before):
    table = svc._store._table
    spy = MagicMock(wraps=table)
    svc._store = StateStore("connections", table=spy)
    raced = {"done": False}

    def racing_put(Item, ConditionExpression=None):
        if Item["pk"] == _LINK_PK and not raced["done"]:
            raced["done"] = True
            before()
        return table.put_item(Item=Item, ConditionExpression=ConditionExpression)

    spy.put_item.side_effect = racing_put


@mock_aws
def test_a_concurrent_first_link_answers_changed_and_deletes_its_own_secret():
    revoke = MagicMock(return_value=None)
    svc = _svc(ids=["state-1", "state-2", "link-1", "link-2"], revoke=revoke)
    _url, first_state = svc.begin_link("oid-1", "c-1", REDIRECT)
    _url, second_state = svc.begin_link("oid-1", "c-1", REDIRECT)
    _racing_link_put(svc, lambda: svc.complete_link("oid-1", "code-2", second_state))

    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-1", "code-1", first_state)
    assert ei.value.kind == "changed"

    assert _rows(svc)["oid-1#c-1"].id == "link-2"
    assert _secret_body("link-2") == {"access_token": ACCESS, "refresh_token": REFRESH}
    sm = aws_clients.client("secretsmanager", region="us-east-1")
    with pytest.raises(sm.exceptions.ResourceNotFoundException):
        sm.get_secret_value(SecretId=f"{SECRET_PREFIX}link-1")
    revoke.assert_not_called()


@mock_aws
def test_a_relink_that_raced_a_refresh_claim_answers_changed_and_keeps_the_claim():
    revoke = MagicMock(return_value=None)
    svc = _svc(revoke=revoke)
    live = _link(svc)
    claimed = live.model_copy(
        update={"status": LinkStatus.REFRESHING, "token_version": 1, "refresh_claimed_at": FIXED.isoformat()}
    )
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)
    _racing_link_put(svc, lambda: svc._save_guarded(claimed, expected_version=0))

    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-1", "code-2", state)
    assert ei.value.kind == "changed"

    row = _rows(svc)["oid-1#c-1"]
    assert row.id == "link-1" and row.status == LinkStatus.REFRESHING and row.token_version == 1
    assert _secret_body("link-1") == {"access_token": ACCESS, "refresh_token": REFRESH}
    sm = aws_clients.client("secretsmanager", region="us-east-1")
    with pytest.raises(sm.exceptions.ResourceNotFoundException):
        sm.get_secret_value(SecretId=f"{SECRET_PREFIX}link-2")
    revoke.assert_not_called()


@mock_aws
def test_a_relink_reads_the_link_partition_consistently_before_its_write():
    svc = _svc()
    _link(svc)
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)
    spy = MagicMock(wraps=svc._store._table)
    svc._store = StateStore("connections", table=spy)

    svc.complete_link("oid-1", "code-2", state)

    calls = [(name, kwargs) for name, _args, kwargs in spy.mock_calls if name in ("query", "put_item")]
    assert [name for name, _kwargs in calls] == ["query", "put_item"]
    assert calls[0][1]["ConsistentRead"] is True


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


@mock_aws
def test_verify_updates_the_login_but_never_the_github_id():
    """Only the label moves: ``github_id`` is the join key and this row's binding."""
    svc = _svc()
    _link(svc)
    svc._fetch_user_identity = MagicMock(return_value={"github_id": 111, "github_login": "octocat-renamed"})

    verified = svc.verify_link("oid-1", "c-1")
    assert verified.github_login == "octocat-renamed"
    assert verified.github_id == GH_ID
    assert verified.last_verified_at == FIXED.isoformat()
    assert verified.status == LinkStatus.LINKED
    assert _rows(svc)["oid-1#c-1"].github_login == "octocat-renamed"


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
    assert _rows(svc)["oid-1#c-1"].status == LinkStatus.UNLINKED


@mock_aws
def test_verify_unknown_link_is_not_found():
    svc = _svc()
    with pytest.raises(GitHubLinkError) as ei:
        svc.verify_link("oid-1", "c-1")
    assert ei.value.kind == "not_found"


@mock_aws
def test_an_unreadable_row_is_not_reported_as_not_found():
    """A DDB blip must not tell a linked human "not found" and send them round the web flow."""
    throttle = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "GetItem")
    svc = _faulting(_svc(), "get_item", throttle)
    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    assert ei.value.kind == "secret_error"


@mock_aws
def test_unlink_revokes_at_github_then_purges_secret_then_row():
    revoke = MagicMock(return_value=None)
    svc = _svc(revoke=revoke)
    _link(svc)

    svc.unlink("oid-1", "c-1")

    kwargs = revoke.call_args.kwargs
    assert kwargs["client_id"] == CLIENT_ID and kwargs["client_secret"] == CLIENT_SECRET
    assert kwargs["access_token"] == ACCESS
    assert _rows(svc) == {}
    sm = aws_clients.client("secretsmanager", region="us-east-1")
    with pytest.raises(sm.exceptions.ResourceNotFoundException):
        sm.get_secret_value(SecretId=f"{SECRET_PREFIX}link-1")


@mock_aws
def test_unlink_does_not_report_success_when_the_revoke_failed():
    """Purging would tell the human their authorization is gone while the grant is live and destroy
    the only token that could revoke it; the token and the row survive, shown UNLINKED, and no
    token is handed out meanwhile."""
    revoke = MagicMock(
        side_effect=GitHubOAuthError("GitHub declined the authorization revocation (HTTP 500)")
    )
    svc = _svc(revoke=revoke)
    _link(svc)

    with pytest.raises(GitHubLinkError) as ei:
        svc.unlink("oid-1", "c-1")
    assert ei.value.kind == "provider_error"
    revoke.assert_called_once()

    assert _secret_body("link-1") == {"access_token": ACCESS, "refresh_token": REFRESH}
    assert _rows(svc)["oid-1#c-1"].status == LinkStatus.UNLINKED
    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    assert ei.value.kind == "link_revoked"


@mock_aws
def test_a_retried_unlink_succeeds_once_github_recovers():
    revoke = MagicMock(
        side_effect=GitHubOAuthError("GitHub declined the authorization revocation (HTTP 500)")
    )
    svc = _svc(revoke=revoke)
    _link(svc)
    with pytest.raises(GitHubLinkError):
        svc.unlink("oid-1", "c-1")

    svc._revoke_grant = MagicMock(return_value=None)
    svc.unlink("oid-1", "c-1")

    assert svc._revoke_grant.call_args.kwargs["access_token"] == ACCESS
    assert _rows(svc) == {}
    sm = aws_clients.client("secretsmanager", region="us-east-1")
    with pytest.raises(sm.exceptions.ResourceNotFoundException):
        sm.get_secret_value(SecretId=f"{SECRET_PREFIX}link-1")


@mock_aws
def test_unlink_still_purges_when_the_oauth_client_is_gone():
    svc = _svc()
    _link(svc)
    svc._load_credentials = MagicMock(side_effect=GitHubLinkError("no client", kind="oauth_client_missing"))

    svc.unlink("oid-1", "c-1")
    assert _rows(svc) == {}


@mock_aws
def test_a_transient_loader_fault_does_not_purge_without_revoking():
    """Collapsing a throttle into "no client configured" would answer 204 with the revoke never
    called and the only token that could revoke a live grant destroyed; a blip destroys nothing,
    and the retry does the real thing once the store recovers."""
    revoke = MagicMock(return_value=None)
    svc = _svc(revoke=revoke)
    _link(svc)
    svc._client_credentials_loader = _loader_faults

    with pytest.raises(GitHubLinkError) as ei:
        svc.unlink("oid-1", "c-1")
    assert ei.value.kind == "secret_error"
    revoke.assert_not_called()

    assert _secret_body("link-1") == {"access_token": ACCESS, "refresh_token": REFRESH}
    assert _rows(svc)["oid-1#c-1"].status == LinkStatus.LINKED

    svc._client_credentials_loader = _loader_ok
    svc.unlink("oid-1", "c-1")
    assert revoke.call_args.kwargs["access_token"] == ACCESS
    assert _rows(svc) == {}


@mock_aws
def test_a_transient_loader_fault_is_retryable_on_begin_link_too():
    svc = _svc(loader=_loader_faults)
    with pytest.raises(GitHubLinkError) as ei:
        svc.begin_link("oid-1", "c-1", REDIRECT)
    assert ei.value.kind == "secret_error"
    assert _states(svc) == {}


@mock_aws
def test_unlink_unknown_link_is_not_found():
    svc = _svc()
    with pytest.raises(GitHubLinkError) as ei:
        svc.unlink("oid-1", "c-1")
    assert ei.value.kind == "not_found"


def _boom_oauth(*a, **k):
    raise GitHubOAuthError("GitHub declined the code exchange (HTTP 500)")


@mock_aws
@pytest.mark.parametrize(
    "scenario",
    [
        "no_oauth_client",
        "bad_redirect",
        "unknown_state",
        "foreign_state",
        "exchange_failed",
        "conflict",
        "revoked",
        "not_found",
    ],
)
def test_no_error_message_contains_a_token_or_the_client_secret(scenario):
    forbidden = (ACCESS, REFRESH, CLIENT_SECRET, "code-1", "verifier-1")

    if scenario == "no_oauth_client":
        svc = _svc(loader=_loader_raises)
        call = partial(svc.begin_link, "oid-1", "c-1", REDIRECT)
    elif scenario == "bad_redirect":
        svc = _svc()
        call = partial(svc.begin_link, "oid-1", "c-1", "https://x.example.com/nope")
    elif scenario == "unknown_state":
        svc = _svc()
        call = partial(svc.complete_link, "oid-1", "code-1", "nope")
    elif scenario == "foreign_state":
        svc = _svc()
        _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)
        call = partial(svc.complete_link, "oid-2", "code-1", state)
    elif scenario == "exchange_failed":
        svc = _svc(exchange=MagicMock(side_effect=_boom_oauth))
        _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)
        call = partial(svc.complete_link, "oid-1", "code-1", state)
    elif scenario == "conflict":
        svc = _svc()
        _link(svc, oid="oid-1", conn="c-1")
        _url, state = svc.begin_link("oid-2", "c-1", REDIRECT)
        call = partial(svc.complete_link, "oid-2", "code-1", state)
    elif scenario == "revoked":
        svc = _svc()
        _link(svc)
        svc._fetch_user_identity = MagicMock(
            side_effect=GitHubOAuthError("GitHub rejected the user token (HTTP 401)", kind="revoked")
        )
        call = partial(svc.verify_link, "oid-1", "c-1")
    else:
        svc = _svc()
        call = partial(svc.unlink, "oid-1", "c-1")

    with pytest.raises(GitHubLinkError) as ei:
        call()
    blob = f"{ei.value.message}|{ei.value}|{ei.value.args}"
    for bad in forbidden:
        assert bad not in blob


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
    """The half that is not a ``ClientError``: the request never reached DynamoDB at all."""
    return EndpointConnectionError(endpoint_url=f"https://{_BOTO_MARKER}.example.invalid/")


_FAULTS = [("client_error", _throttle), ("boto_core_error", _unreachable)]


def _faulting(svc, op, fault):
    """The service over a spy of its own memory table whose ``op`` raises ``fault``, so a test can
    drive a whole real flow and fault a single call; the rows written so far stay."""
    table = MagicMock(wraps=svc._store._table)
    getattr(table, op).side_effect = fault
    svc._store = StateStore("connections", table=table)
    assert svc._store.in_memory is False
    return svc


def _assert_retryable_and_quiet(exc_info, site=""):
    """This service's own error, the retryable kind, and not one byte of boto3 internals in anything
    a route could serialize."""
    err = exc_info.value
    assert isinstance(err, GitHubLinkError), site
    assert err.kind == "secret_error", site
    blob = f"{err.message}|{err}|{err.args}"
    assert _BOTO_MARKER not in blob, site
    for bad in ("ProvisionedThroughputExceeded", "endpoint", "Traceback", ACCESS, REFRESH):
        assert bad not in blob, site


@mock_aws
def test_an_empty_table_name_selects_the_in_memory_store():
    assert _svc()._store.in_memory is True


@mock_aws
def test_connection_org_answers_the_loaders_org():
    svc = _svc(org_loader=lambda cid: f"org-of-{cid}")
    assert svc.connection_org("c-1") == "org-of-c-1"


@mock_aws
def test_a_vanished_connection_is_connection_missing_and_renders_as_the_link_not_found():
    """A link cannot exist on a connection that does not, and the console keys on that detail."""

    def gone(connection_id):
        raise ConnectionServiceError("Unknown connection", kind="not_found")

    svc = _svc(org_loader=gone)
    with pytest.raises(GitHubLinkError) as ei:
        svc.connection_org("c-1")
    assert ei.value.kind == "connection_missing"
    assert (ei.value.status_code, ei.value.detail) == (404, "GitHub link not found")
    assert ei.value.code == "github_link.connection_missing"
    assert "Unknown connection" not in ei.value.message


@mock_aws
def test_a_store_fault_on_the_connection_read_is_retryable():
    def faulting(connection_id):
        raise ConnectionServiceError("Secret store operation failed", kind="secret_error")

    svc = _svc(org_loader=faulting)
    with pytest.raises(GitHubLinkError) as ei:
        svc.connection_org("c-1")
    assert ei.value.kind == "secret_error"


@mock_aws
def test_any_other_failure_on_the_connection_read_is_provider_error():
    def broken(connection_id):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "GetItem")

    svc = _svc(org_loader=broken)
    with pytest.raises(GitHubLinkError) as ei:
        svc.connection_org("c-1")
    assert ei.value.kind == "provider_error"
    assert "ProvisionedThroughput" not in f"{ei.value.message}|{ei.value.args}"


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_a_state_write_fault_is_retryable_not_a_500(name, fault):
    """With no stored state the callback could never be validated, so handing back an authorize
    URL would send the human to GitHub to earn a code this service is guaranteed to refuse."""
    svc = _faulting(_svc(), "put_item", fault())

    with pytest.raises(GitHubLinkError) as ei:
        svc.begin_link("oid-1", "c-1", REDIRECT)
    _assert_retryable_and_quiet(ei)


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_a_state_consume_fault_is_retryable_and_never_exchanges_the_code(name, fault):
    """The delete is what makes the state single-use; continuing past a failed one would spend a
    state that is still replayable."""
    exchange = MagicMock(side_effect=_exchange_ok)
    svc = _svc(exchange=exchange)
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)
    _faulting(svc, "delete_item", fault())

    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-1", "code-1", state)
    _assert_retryable_and_quiet(ei)
    exchange.assert_not_called()


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_a_link_row_write_fault_rolls_the_credential_back_and_stays_in_contract(name, fault):
    """The one window where a live pair exists with no row pointing at it: the error speaks this
    service's vocabulary and the rollback still fires, in both boto3 shapes."""
    revoke = MagicMock(return_value=None)
    svc = _svc(revoke=revoke)
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)
    _faulting(svc, "put_item", fault())

    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-1", "code-1", state)
    _assert_retryable_and_quiet(ei)

    assert revoke.call_args.kwargs["access_token"] == ACCESS
    sm = aws_clients.client("secretsmanager", region="us-east-1")
    with pytest.raises(sm.exceptions.ResourceNotFoundException):
        sm.get_secret_value(SecretId=f"{SECRET_PREFIX}link-1")


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_a_link_row_delete_fault_is_retryable_not_a_500(name, fault):
    revoke = MagicMock(return_value=None)
    svc = _svc(revoke=revoke)
    _link(svc)
    _faulting(svc, "delete_item", fault())

    with pytest.raises(GitHubLinkError) as ei:
        svc.unlink("oid-1", "c-1")
    _assert_retryable_and_quiet(ei)
    revoke.assert_called_once()


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_a_row_read_fault_is_retryable_not_not_found(name, fault):
    svc = _svc()
    _link(svc)
    _faulting(svc, "get_item", fault())

    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    _assert_retryable_and_quiet(ei)


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_a_partition_read_fault_on_the_list_path_is_retryable(name, fault):
    svc = _svc()
    _link(svc)
    _faulting(svc, "query", fault())

    with pytest.raises(GitHubLinkError) as ei:
        svc.list_for_principal("oid-1")
    _assert_retryable_and_quiet(ei)


@mock_aws
def test_an_unreachable_store_fails_the_uniqueness_guard_closed():
    svc = _faulting(_svc(), "query", _unreachable())
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)

    with pytest.raises(GitHubLinkError) as ei:
        svc.complete_link("oid-1", "code-1", state)
    assert ei.value.kind == "conflict"
    assert _BOTO_MARKER not in f"{ei.value.message}|{ei.value.args}"


@mock_aws
def test_an_unreachable_store_on_a_label_write_is_not_mistaken_for_a_deleted_link():
    svc = _svc()
    _link(svc)
    _faulting(svc, "update_item", _unreachable())

    with pytest.raises(GitHubLinkError) as ei:
        svc.verify_link("oid-1", "c-1")
    _assert_retryable_and_quiet(ei)


@mock_aws
def test_an_unreachable_store_on_a_guarded_write_is_not_mistaken_for_a_lost_claim():
    refresh = MagicMock(side_effect=AssertionError("GitHub must not be called: the claim failed"))
    svc = _svc(refresh=refresh, now=lambda: FIXED)
    row = _link(svc)
    seed(svc._store, svc._to_item(row.model_copy(update={"access_token_expires_at": FIXED.isoformat()})))
    _faulting(svc, "put_item", _unreachable())

    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    _assert_retryable_and_quiet(ei)
    refresh.assert_not_called()


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_a_secrets_manager_fault_never_escapes_raw_either(name, fault):
    """The ``ResourceExistsException`` fallback's ``put_secret_value`` is translated too."""
    svc = _svc()
    sm = MagicMock()
    sm.exceptions.ResourceExistsException = aws_clients.client(
        "secretsmanager", region="us-east-1"
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
    assert _rows(svc) == {}


@mock_aws
@pytest.mark.parametrize("name,fault", _FAULTS, ids=[n for n, _ in _FAULTS])
def test_every_guarded_store_operation_translates_at_its_own_call_site(name, fault):
    row = _link(_svc()).model_copy()
    ops = {
        "_save first link": ("put_item", lambda s: s._save(row, existing=None)),
        "_save re-link": ("put_item", lambda s: s._save(row, existing=row)),
        "_delete": ("delete_item", lambda s: s._delete("oid-1", "c-1")),
        "_save_state": ("put_item", lambda s: s._save_state("st-1", {"a": "b"})),
        "_delete_state": ("delete_item", lambda s: s._delete_state("st-1")),
    }
    for op, (call, drive) in ops.items():
        svc = _faulting(_svc(), call, fault())
        with pytest.raises(GitHubLinkError) as ei:
            drive(svc)
        _assert_retryable_and_quiet(ei, site=op)
