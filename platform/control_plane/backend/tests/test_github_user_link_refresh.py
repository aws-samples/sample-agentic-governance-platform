"""The refresh guard. GitHub's refresh is destructive and single-use and AGP runs two ECS tasks, so
the service claims a rotation with a conditional write on ``token_version`` before it calls GitHub
and exactly one caller wins. The store's memory table plus moto Secrets Manager, every GitHub call a
fake; the memory table enforces the same ``ConditionExpression`` DynamoDB does, so the
compare-and-set is exercised rather than simulated."""

import json
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from moto.core.decorator import mock_aws
from seed import seed

from core import aws_clients
from core.state_store import StateStore
from models.github_link import GitHubUserLink, LinkStatus
from services.github_user_link import (
    LINK_CALLBACK_PATH,
    GitHubLinkError,
    GitHubUserLinkService,
    _ClaimLost,
)
from services.github_user_oauth import GitHubOAuthError

FIXED = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

ACCESS = "ghu_old_access"
REFRESH = "ghr_old_refresh"
NEW_ACCESS = "ghu_new_access"
NEW_REFRESH = "ghr_new_refresh"
CLIENT_ID = "Iv1.abc123"
CLIENT_SECRET = "cs_client_secret_value"

SECRET_PREFIX = "agp-test/github-user-link/"
REDIRECT = f"https://console.example.com{LINK_CALLBACK_PATH}"
KEY = ("github_user_link", "oid-1#c-1")


def _rotated(**kwargs):
    return {
        "access_token": NEW_ACCESS,
        "refresh_token": NEW_REFRESH,
        "expires_in": 28800,
        "refresh_token_expires_in": 15897600,
    }


class _Clock:
    """A mutable injected clock, the only time source in these tests."""

    def __init__(self, t=FIXED):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t = self.t + timedelta(**kw)


def _svc(*, clock=None, refresh=None, ids=None, expires_in: int | None = 28800):
    """A service holding one linked link (``oid-1``/``c-1``, link id ``link-1``)."""
    clock = clock or _Clock()
    ids = iter(ids or ["state-1", "link-1", "state-2", "link-2"])
    svc = GitHubUserLinkService(
        table_name="",
        secret_prefix=SECRET_PREFIX,
        region="us-east-1",
        client_credentials_loader=lambda cid: (CLIENT_ID, CLIENT_SECRET),
        connection_org_loader=lambda cid: "acme",
        secrets_client=aws_clients.client("secretsmanager", region="us-east-1"),
        new_id=lambda: next(ids),
        new_verifier=lambda: "verifier-1",
        now=clock,
        exchange_code=MagicMock(
            return_value={
                "access_token": ACCESS,
                "refresh_token": REFRESH,
                "expires_in": expires_in,
                "refresh_token_expires_in": 15897600,
            }
        ),
        refresh_user_token=refresh if refresh is not None else MagicMock(side_effect=_rotated),
        fetch_user_identity=MagicMock(return_value={"github_id": 583231, "github_login": "octocat"}),
        revoke_grant=MagicMock(return_value=None),
    )
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)
    svc.complete_link("oid-1", "code-1", state)
    return svc


def _secret_body(link_id="link-1"):
    sm = aws_clients.client("secretsmanager", region="us-east-1")
    return json.loads(sm.get_secret_value(SecretId=f"{SECRET_PREFIX}{link_id}")["SecretString"])


def _row(svc: GitHubUserLinkService) -> GitHubUserLink:
    """The one linked row as the service reads it."""
    row = svc._get("oid-1", "c-1")
    assert row is not None
    return row


def _stored(svc: GitHubUserLinkService) -> dict:
    item = svc._store.get(KEY)
    assert item is not None
    return item


def _clock(svc: GitHubUserLinkService) -> _Clock:
    clock = svc._now
    assert isinstance(clock, _Clock)
    return clock


def _spied(svc):
    """The service's store over a spy of its own memory table, so a test can count and interpose
    the table calls; returns ``(spy, table)``."""
    table = svc._store._table
    spy = MagicMock(wraps=table)
    svc._store = StateStore("connections", table=spy)
    return spy, table


def _puts(spy):
    """``(conditional, unconditional)`` counts of the puts the spy saw."""
    conditions = [call.kwargs.get("ConditionExpression") for call in spy.put_item.call_args_list]
    return sum(c is not None for c in conditions), sum(c is None for c in conditions)


def _lost_claims(svc):
    """Record the ``expected_version`` of every ``_ClaimLost`` the service's guarded write raises."""
    real = svc._save_guarded
    lost = []
    lock = threading.Lock()

    def counting(record, *, expected_version):
        try:
            real(record, expected_version=expected_version)
        except _ClaimLost:
            with lock:
                lost.append(expected_version)
            raise

    svc._save_guarded = counting
    return lost


@mock_aws
def test_a_fresh_token_is_returned_without_refreshing():
    refresh = MagicMock(side_effect=_rotated)
    svc = _svc(refresh=refresh)

    assert svc.get_user_bearer_token("oid-1", "c-1") == ACCESS
    refresh.assert_not_called()
    assert _row(svc).token_version == 0


@mock_aws
def test_a_link_with_no_expiry_never_refreshes():
    refresh = MagicMock(side_effect=_rotated)
    svc = _svc(refresh=refresh, expires_in=None)
    assert _row(svc).access_token_expires_at is None

    _clock(svc).advance(days=400)
    assert svc.get_user_bearer_token("oid-1", "c-1") == ACCESS
    refresh.assert_not_called()


@mock_aws
def test_a_token_just_outside_the_skew_is_not_refreshed():
    """Expiry is +8h and the skew is 300s; land 301s short of it."""
    refresh = MagicMock(side_effect=_rotated)
    svc = _svc(refresh=refresh)
    _clock(svc).advance(seconds=28800 - 301)

    assert svc.get_user_bearer_token("oid-1", "c-1") == ACCESS
    refresh.assert_not_called()


@mock_aws
def test_a_token_inside_the_skew_refreshes_once_and_bumps_the_version():
    refresh = MagicMock(side_effect=_rotated)
    svc = _svc(refresh=refresh)
    _clock(svc).advance(seconds=28800 - 299)

    assert svc.get_user_bearer_token("oid-1", "c-1") == NEW_ACCESS
    refresh.assert_called_once()
    kwargs = refresh.call_args.kwargs
    assert kwargs["refresh_token"] == REFRESH
    assert kwargs["client_id"] == CLIENT_ID and kwargs["client_secret"] == CLIENT_SECRET

    row = _row(svc)
    assert row.status == LinkStatus.LINKED
    assert row.token_version == 1
    assert row.refresh_claimed_at is None
    assert row.access_token_expires_at == (_clock(svc).t + timedelta(seconds=28800)).isoformat()


@mock_aws
def test_the_new_refresh_token_replaces_the_old_one_in_the_secret():
    """The superseded pair is gone: keeping it would let a later caller present a dead refresh
    token and hard-kill the link."""
    svc = _svc()
    _clock(svc).advance(seconds=28800)

    svc.get_user_bearer_token("oid-1", "c-1")

    assert _secret_body() == {"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH}
    assert REFRESH not in json.dumps(_secret_body())


@mock_aws
def test_an_expired_token_still_refreshes_rather_than_failing():
    svc = _svc()
    _clock(svc).advance(days=2)
    assert svc.get_user_bearer_token("oid-1", "c-1") == NEW_ACCESS


@mock_aws
def test_an_unlinked_row_is_link_revoked_and_never_refreshes():
    refresh = MagicMock(side_effect=_rotated)
    svc = _svc(refresh=refresh)
    seed(svc._store, svc._to_item(_row(svc).model_copy(update={"status": LinkStatus.UNLINKED})))

    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    assert ei.value.kind == "link_revoked"
    refresh.assert_not_called()


@mock_aws
def test_a_missing_row_is_not_found():
    svc = _svc()
    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-other")
    assert ei.value.kind == "not_found"


@mock_aws
def test_a_fresh_claim_by_another_task_is_refresh_in_progress():
    refresh = MagicMock(side_effect=_rotated)
    svc = _svc(refresh=refresh)
    seed(
        svc._store,
        svc._to_item(
            _row(svc).model_copy(
                update={
                    "status": LinkStatus.REFRESHING,
                    "refresh_claimed_at": _clock(svc).t.isoformat(),
                }
            )
        ),
    )
    _clock(svc).advance(seconds=30)

    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    assert ei.value.kind == "refresh_in_progress"
    refresh.assert_not_called()
    assert _row(svc).status == LinkStatus.REFRESHING


def _stale_claim(svc, *, advance=61):
    """Leave the link REFRESHING with a claim older than the 60s timeout."""
    seed(
        svc._store,
        svc._to_item(
            _row(svc).model_copy(
                update={
                    "status": LinkStatus.REFRESHING,
                    "refresh_claimed_at": _clock(svc).t.isoformat(),
                }
            )
        ),
    )
    _clock(svc).advance(seconds=advance)


@mock_aws
def test_an_abandoned_claim_whose_stored_token_is_dead_unlinks():
    """The claimer died mid-rotation and the pair is dead at GitHub, so the honest state is
    UNLINKED plus "re-link"; the recovery probe is a read and never spends a refresh token."""
    refresh = MagicMock(side_effect=_rotated)
    svc = _svc(refresh=refresh)
    svc._fetch_user_identity = MagicMock(
        side_effect=GitHubOAuthError("GitHub rejected the user token (HTTP 401)", kind="revoked")
    )
    _stale_claim(svc)

    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    assert ei.value.kind == "link_revoked"
    assert _row(svc).status == LinkStatus.UNLINKED
    refresh.assert_not_called()


@mock_aws
def test_an_abandoned_claim_whose_stored_token_still_works_is_recovered_not_unlinked():
    """The rotation succeeded and only the publish failed, so the secret holds a working pair;
    unlinking would force the human through the web flow to replace a credential that was never
    broken."""
    refresh = MagicMock(side_effect=_rotated)
    svc = _svc(refresh=refresh)
    identity = MagicMock(return_value={"github_id": 583231, "github_login": "octocat"})
    svc._fetch_user_identity = identity
    _stale_claim(svc)

    assert svc.get_user_bearer_token("oid-1", "c-1") == ACCESS
    identity.assert_called_once()
    refresh.assert_not_called()
    row = _row(svc)
    assert row.status == LinkStatus.LINKED and row.refresh_claimed_at is None


@mock_aws
def test_an_abandoned_claim_is_not_unlinked_on_a_transient_probe_failure():
    """Nothing is proven dead, so nothing may be destroyed."""
    svc = _svc(refresh=MagicMock(side_effect=_rotated))
    svc._fetch_user_identity = MagicMock(
        side_effect=GitHubOAuthError("could not reach GitHub (ConnectError)")
    )
    _stale_claim(svc)

    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    assert ei.value.kind == "provider_error"
    assert _row(svc).status == LinkStatus.REFRESHING


@mock_aws
def test_bad_refresh_token_unlinks_and_raises_link_revoked():
    refresh = MagicMock(
        side_effect=GitHubOAuthError(
            "GitHub rejected the token refresh (bad_refresh_token)", kind="bad_grant"
        )
    )
    svc = _svc(refresh=refresh)
    _clock(svc).advance(seconds=28800)

    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    assert ei.value.kind == "link_revoked"
    assert _row(svc).status == LinkStatus.UNLINKED
    assert _secret_body() == {"access_token": ACCESS, "refresh_token": REFRESH}


@mock_aws
def test_a_failed_rotation_never_retries():
    """Exactly one attempt, since a retry could double-rotate; the claim is released so the next
    request is not wedged for the full timeout, and the version bump stands so a concurrent loser
    still loses."""
    refresh = MagicMock(
        side_effect=GitHubOAuthError("could not reach GitHub to token refresh (ConnectError)")
    )
    svc = _svc(refresh=refresh)
    _clock(svc).advance(seconds=28800)

    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    assert ei.value.kind == "provider_error"
    refresh.assert_called_once()

    row = _row(svc)
    assert row.status == LinkStatus.LINKED
    assert row.refresh_claimed_at is None


@mock_aws
def test_a_secret_write_failure_after_rotation_unlinks():
    """The rotated pair is lost and the old pair is already dead at GitHub, so the link is broken
    and saying so is the only safe answer."""
    svc = _svc()
    _clock(svc).advance(seconds=28800)
    svc._put_secret_body = MagicMock(
        side_effect=GitHubLinkError("Failed to rotate the link secret", kind="secret_error")
    )

    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    assert ei.value.kind == "link_revoked"
    assert _row(svc).status == LinkStatus.UNLINKED


def _synced_get(svc, lock, barrier):
    """A ``_get`` whose first two callers wait for each other after reading, so both hold the same
    version-0 row, the one state a status gate cannot arbitrate."""
    real_get = svc._get
    synced = {"n": 0}

    def synced_get(principal_oid, connection_id):
        with lock:
            synced["n"] += 1
            first_two = synced["n"] <= 2
        row = real_get(principal_oid, connection_id)
        if first_two:
            barrier.wait(timeout=5)
        return row

    return synced_get


def _two_callers(svc):
    results = {}

    def caller(tag):
        try:
            results[tag] = ("ok", svc.get_user_bearer_token("oid-1", "c-1"))
        except GitHubLinkError as exc:
            results[tag] = ("err", exc.kind)

    threads = [threading.Thread(target=caller, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    return sorted(results.values())


@mock_aws
def test_the_loser_of_a_concurrent_claim_does_not_rotate_twice():
    """Two tasks, one link, one destructive rotation: exactly one may call GitHub. The interleaving
    is forced by a barrier inside the row read, so both hold the same version-0 row, and the
    compare-and-set is what arbitrates, observed directly by counting ``_ClaimLost``."""
    read_barrier = threading.Barrier(2)
    calls = []
    lock = threading.Lock()

    def counting_refresh(**kwargs):
        with lock:
            calls.append(kwargs["refresh_token"])
        return _rotated()

    svc = _svc(refresh=MagicMock(side_effect=counting_refresh))
    _clock(svc).advance(seconds=28800 - 100)
    svc._get = _synced_get(svc, lock, read_barrier)
    lost = _lost_claims(svc)

    outcomes = _two_callers(svc)

    assert len(calls) == 1, "the destructive rotation ran twice"
    assert lost == [0], f"the CAS did not arbitrate the claim: {lost}"
    assert ("ok", NEW_ACCESS) in outcomes
    loser = next(r for r in outcomes if r != ("ok", NEW_ACCESS))
    assert loser in (("err", "refresh_in_progress"), ("ok", NEW_ACCESS))
    assert _stored(svc)["token_version"] == 1


@mock_aws
def test_the_loser_reads_the_published_token_when_the_winner_finished():
    """The loser's conditional write fails because the winner already published, so the single
    re-read finds LINKED at a higher version and returns the fresh token."""
    refresh = MagicMock(side_effect=_rotated)
    svc = _svc(refresh=refresh)
    _clock(svc).advance(seconds=28800 - 100)
    spy, table = _spied(svc)
    state = {"done": False}

    def racing_put(Item, ConditionExpression=None):
        if ConditionExpression is not None and not state["done"]:
            state["done"] = True
            svc.get_user_bearer_token("oid-1", "c-1")
        return table.put_item(Item=Item, ConditionExpression=ConditionExpression)

    spy.put_item.side_effect = racing_put

    assert svc.get_user_bearer_token("oid-1", "c-1") == NEW_ACCESS
    refresh.assert_called_once()


@mock_aws
def test_the_loser_is_told_to_retry_when_the_winner_is_still_rotating():
    refresh = MagicMock(side_effect=_rotated)
    svc = _svc(refresh=refresh)
    _clock(svc).advance(seconds=28800 - 100)
    spy, table = _spied(svc)
    state = {"done": False}

    def racing_put(Item, ConditionExpression=None):
        if ConditionExpression is not None and not state["done"]:
            state["done"] = True
            claimed = _stored(svc)
            claimed["status"] = "refreshing"
            claimed["token_version"] = claimed["token_version"] + 1
            claimed["refresh_claimed_at"] = _clock(svc).t.isoformat()
            table.put_item(Item=claimed)
        return table.put_item(Item=Item, ConditionExpression=ConditionExpression)

    spy.put_item.side_effect = racing_put

    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    assert ei.value.kind == "refresh_in_progress"
    refresh.assert_not_called()


@mock_aws
def test_the_claim_is_a_conditional_write_on_the_observed_version():
    """Every row write, the claim and the publish, is conditional on the version the writer read."""
    refresh = MagicMock(side_effect=_rotated)
    svc = _svc(refresh=refresh)
    _clock(svc).advance(seconds=28800 - 100)
    spy, _table = _spied(svc)
    lost = _lost_claims(svc)

    svc.get_user_bearer_token("oid-1", "c-1")

    assert _puts(spy) == (2, 0)
    assert lost == []
    row = _stored(svc)
    assert row["token_version"] == 1 and row["status"] == "linked"


@mock_aws
def test_github_is_never_called_before_the_claim_lands():
    """Calling GitHub first and claiming after would let both tasks rotate and discover the conflict
    once the destruction has already happened."""
    order = []
    refresh = MagicMock(side_effect=lambda **kw: (order.append("refresh"), _rotated())[1])
    svc = _svc(refresh=refresh)
    _clock(svc).advance(seconds=28800 - 100)
    spy, table = _spied(svc)

    def tracking_put(Item, ConditionExpression=None):
        assert ConditionExpression is not None, "a row write escaped the version guard"
        order.append("claim" if Item["status"] == "refreshing" else "publish")
        return table.put_item(Item=Item, ConditionExpression=ConditionExpression)

    spy.put_item.side_effect = tracking_put
    svc.get_user_bearer_token("oid-1", "c-1")

    assert order == ["claim", "refresh", "publish"]


@mock_aws
def test_a_concurrent_verify_link_cannot_erase_a_refresh_claim():
    """``verify_link`` reads its row, spends up to 15s on ``GET /user``, then writes. Putting that
    stale row back would regress ``token_version`` and erase a claim landed in the meantime, after
    which a third caller's CAS on the old version passes and the same refresh token is spent twice."""
    spent = []
    lock = threading.Lock()

    def counting_refresh(**kwargs):
        with lock:
            spent.append(kwargs["refresh_token"])
        return _rotated()

    svc = _svc(refresh=MagicMock(side_effect=counting_refresh))
    versions = []

    stale_row = svc._require_row("oid-1", "c-1")
    assert stale_row.token_version == 0

    _clock(svc).advance(seconds=28800 - 100)
    svc.get_user_bearer_token("oid-1", "c-1")
    versions.append(_stored(svc)["token_version"])

    svc._fetch_user_identity = MagicMock(
        return_value={"github_id": 583231, "github_login": "octocat-renamed"}
    )
    real_require = svc._require_row
    reads = {"n": 0}

    def stale_second_read(principal_oid, connection_id):
        reads["n"] += 1
        return stale_row if reads["n"] == 2 else real_require(principal_oid, connection_id)

    svc._require_row = stale_second_read
    svc.verify_link("oid-1", "c-1")
    svc._require_row = real_require
    versions.append(_stored(svc)["token_version"])

    assert _stored(svc)["github_login"] == "octocat-renamed"
    assert versions == [1, 1], f"token_version regressed: {versions}"
    assert _stored(svc)["status"] == "linked"

    _clock(svc).advance(seconds=28800 - 100)
    svc.get_user_bearer_token("oid-1", "c-1")
    assert spent == [REFRESH, NEW_REFRESH], f"a refresh token was spent twice: {spent}"


@mock_aws
def test_a_link_deleted_during_the_verify_probe_is_not_fabricated_as_a_partial_row():
    """DynamoDB ``UpdateItem`` creates the item when the key is absent, so an unguarded label write
    on a link deleted during the probe would write a five-field orphan that fails
    ``model_validate`` for every row read of the partition. The write is guarded on existence and
    refuses as ``not_found``."""
    svc = _svc(refresh=MagicMock(side_effect=_rotated))
    svc._fetch_user_identity = MagicMock(
        return_value={"github_id": 583231, "github_login": "octocat-renamed"}
    )

    row = svc._require_row("oid-1", "c-1")
    real_require = svc._require_row
    reads = {"n": 0}

    def deleting_second_read(principal_oid, connection_id):
        reads["n"] += 1
        if reads["n"] == 2:
            svc._delete(principal_oid, connection_id)
            return row
        return real_require(principal_oid, connection_id)

    svc._require_row = deleting_second_read

    with pytest.raises(GitHubLinkError) as ei:
        svc.verify_link("oid-1", "c-1")
    assert ei.value.kind == "not_found"
    assert svc._store.get(KEY) is None, "the label write fabricated a row that was deleted"
    assert svc._load_all_strict() == []


@mock_aws
def test_a_stale_unlink_publish_cannot_clobber_a_newer_claim():
    """``_publish_unlinked`` and ``_release_claim`` put a whole row read earlier; a decision made
    against version 0 must not land on a row claimed at version 1."""
    svc = _svc(refresh=MagicMock(side_effect=_rotated))

    stale_row = svc._require_row("oid-1", "c-1")
    assert stale_row.token_version == 0

    _clock(svc).advance(seconds=28800 - 100)
    claimed = stale_row.model_copy(
        update={
            "status": LinkStatus.REFRESHING,
            "token_version": 1,
            "refresh_claimed_at": _clock(svc).t.isoformat(),
        }
    )
    seed(svc._store, svc._to_item(claimed))

    svc._publish_unlinked(stale_row)
    assert _stored(svc)["status"] == "refreshing"
    assert _stored(svc)["token_version"] == 1

    svc._release_claim(stale_row)
    assert _stored(svc)["status"] == "refreshing"
    assert _stored(svc)["token_version"] == 1


@mock_aws
def test_a_release_after_unlink_does_not_recreate_the_row():
    """A claim taken before the link was unlinked and released after it: the version guard refuses
    an absent row, so the late release is a lost claim and the deleted row stays deleted instead of
    coming back with its secret pointer."""
    svc = _svc()
    row = svc._require_row("oid-1", "c-1")
    claimed = row.model_copy(
        update={
            "status": LinkStatus.REFRESHING,
            "token_version": 1,
            "refresh_claimed_at": _clock(svc).t.isoformat(),
        }
    )
    svc._save_guarded(claimed, expected_version=0)
    svc.unlink("oid-1", "c-1")
    assert svc._store.get(KEY) is None
    lost = _lost_claims(svc)

    svc._save_lossy(
        claimed.model_copy(update={"status": LinkStatus.LINKED, "refresh_claimed_at": None}),
        expected_version=claimed.token_version,
    )

    assert lost == [1]
    assert svc._store.get(KEY) is None
    assert svc._load_all_strict() == []


@mock_aws
def test_a_stale_guarded_write_cannot_land_after_a_relink():
    svc = _svc()
    stale = svc._require_row("oid-1", "c-1")
    assert stale.token_version == 0
    _url, state = svc.begin_link("oid-1", "c-1", REDIRECT)
    relinked = svc.complete_link("oid-1", "code-2", state)
    lost = _lost_claims(svc)

    svc._save_lossy(
        stale.model_copy(update={"status": LinkStatus.LINKED, "refresh_claimed_at": None}),
        expected_version=stale.token_version,
    )

    assert lost == [0]
    row = _stored(svc)
    assert row["id"] == "link-2" and row["secret_arn"] == relinked.secret_arn
    assert row["token_version"] == 1


@mock_aws
def test_a_released_claim_is_not_mistaken_for_a_completed_refresh():
    """A transient provider failure releases the claim but keeps the version bump, leaving LINKED
    at a higher version while the secret still holds the old, expiring pair; freshness must come
    from the expiry."""
    svc = _svc(
        refresh=MagicMock(
            side_effect=GitHubOAuthError("could not reach GitHub to token refresh (ConnectError)")
        )
    )
    _clock(svc).advance(seconds=28800 + 3600)

    loser_row = svc._require_row("oid-1", "c-1")

    with pytest.raises(GitHubLinkError):
        svc.get_user_bearer_token("oid-1", "c-1")
    released = _row(svc)
    assert released.status == LinkStatus.LINKED and released.token_version == 1
    assert _secret_body() == {"access_token": ACCESS, "refresh_token": REFRESH}

    with pytest.raises(GitHubLinkError) as ei:
        svc._read_after_lost_claim(loser_row)
    assert ei.value.kind == "refresh_in_progress"


@mock_aws
def test_a_publish_write_failure_stays_inside_the_error_contract():
    """A store fault on the publish is this service's own retryable kind, and the good pair is
    still there to be recovered."""
    svc = _svc(refresh=MagicMock(side_effect=_rotated))
    _clock(svc).advance(seconds=28800 - 100)
    spy, table = _spied(svc)

    def failing_publish(Item, ConditionExpression=None):
        if Item["status"] == "linked":
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "PutItem")
        return table.put_item(Item=Item, ConditionExpression=ConditionExpression)

    spy.put_item.side_effect = failing_publish

    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    assert ei.value.kind == "secret_error"

    assert _secret_body() == {"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH}
    assert _stored(svc)["status"] == "refreshing"
