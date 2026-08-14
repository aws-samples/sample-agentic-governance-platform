# tests/test_github_user_link_refresh.py — E27B/T7, the refresh guard (D6).
#
# THE outage risk of this epic, in its own suite. GitHub's refresh is DESTRUCTIVE and
# SINGLE-USE: using a `ghr_` token invalidates both the old refresh token AND the old access
# token. AGP runs TWO ECS tasks, so two concurrent refreshes on one link would mutually
# invalidate and lock the human out of their own link — permanently, since the surviving
# stored pair would be the loser's. The service therefore CLAIMS the rotation with a
# conditional write on `token_version` before it ever calls GitHub, and exactly one caller can
# win that claim.
#
# Same seam as the sibling suite: local fallback + moto Secrets Manager + every GitHub call a
# fake. The DDB-mode tests use a fake Table that really enforces the ConditionExpression, so
# the CAS is exercised rather than simulated.

import json
import threading
from unittest.mock import MagicMock

import boto3
import pytest
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError
from datetime import datetime, timedelta, timezone
from moto import mock_aws

from models.github_link import LinkStatus
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


def _rotated(**kwargs):
    return {
        "access_token": NEW_ACCESS,
        "refresh_token": NEW_REFRESH,
        "expires_in": 28800,
        "refresh_token_expires_in": 15897600,
    }


class _Clock:
    """A mutable injected clock — the ONLY time source in these tests."""

    def __init__(self, t=FIXED):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t = self.t + timedelta(**kw)


def _svc(*, clock=None, refresh=None, ids=None, expires_in=28800):
    """A service holding ONE LINKED link (``oid-1``/``c-1``, link id ``link-1``)."""
    clock = clock or _Clock()
    ids = iter(ids or ["state-1", "link-1", "state-2", "link-2"])
    svc = GitHubUserLinkService(
        table_name="",
        secret_prefix=SECRET_PREFIX,
        region="us-east-1",
        client_credentials_loader=lambda cid: (CLIENT_ID, CLIENT_SECRET),
        secrets_client=boto3.client("secretsmanager", region_name="us-east-1"),
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
    svc._clock = clock
    return svc


def _secret_body(link_id="link-1"):
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    return json.loads(sm.get_secret_value(SecretId=f"{SECRET_PREFIX}{link_id}")["SecretString"])


# --------------------------------------------------------------------------- #
# The no-op paths — a refresh that should not happen is an outage waiting to happen.
# --------------------------------------------------------------------------- #


@mock_aws
def test_a_fresh_token_is_returned_without_refreshing():
    refresh = MagicMock(side_effect=_rotated)
    svc = _svc(refresh=refresh)

    assert svc.get_user_bearer_token("oid-1", "c-1") == ACCESS
    refresh.assert_not_called()
    assert svc._local["oid-1#c-1"].token_version == 0


@mock_aws
def test_a_link_with_no_expiry_never_refreshes():
    refresh = MagicMock(side_effect=_rotated)
    svc = _svc(refresh=refresh, expires_in=None)
    assert svc._local["oid-1#c-1"].access_token_expires_at is None

    svc._clock.advance(days=400)
    assert svc.get_user_bearer_token("oid-1", "c-1") == ACCESS
    refresh.assert_not_called()


@mock_aws
def test_a_token_just_outside_the_skew_is_not_refreshed():
    refresh = MagicMock(side_effect=_rotated)
    svc = _svc(refresh=refresh)
    # Expiry is +8h; the skew is 300s. Land 301s short of it.
    svc._clock.advance(seconds=28800 - 301)

    assert svc.get_user_bearer_token("oid-1", "c-1") == ACCESS
    refresh.assert_not_called()


# --------------------------------------------------------------------------- #
# The happy rotation
# --------------------------------------------------------------------------- #


@mock_aws
def test_a_token_inside_the_skew_refreshes_once_and_bumps_the_version():
    refresh = MagicMock(side_effect=_rotated)
    svc = _svc(refresh=refresh)
    svc._clock.advance(seconds=28800 - 299)  # inside the 300s skew

    assert svc.get_user_bearer_token("oid-1", "c-1") == NEW_ACCESS
    refresh.assert_called_once()
    kwargs = refresh.call_args.kwargs
    assert kwargs["refresh_token"] == REFRESH
    assert kwargs["client_id"] == CLIENT_ID and kwargs["client_secret"] == CLIENT_SECRET

    row = svc._local["oid-1#c-1"]
    assert row.status == LinkStatus.LINKED
    assert row.token_version == 1
    assert row.refresh_claimed_at is None  # the claim is released on publish
    assert row.access_token_expires_at == (svc._clock.t + timedelta(seconds=28800)).isoformat()


@mock_aws
def test_the_new_refresh_token_replaces_the_old_one_in_the_secret():
    svc = _svc()
    svc._clock.advance(seconds=28800)  # expired outright

    svc.get_user_bearer_token("oid-1", "c-1")

    assert _secret_body() == {"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH}
    # The superseded pair is GONE — keeping it would let a later caller present a dead
    # refresh token and hard-kill the link.
    assert REFRESH not in json.dumps(_secret_body())


@mock_aws
def test_an_expired_token_still_refreshes_rather_than_failing():
    svc = _svc()
    svc._clock.advance(days=2)
    assert svc.get_user_bearer_token("oid-1", "c-1") == NEW_ACCESS


# --------------------------------------------------------------------------- #
# Status gates
# --------------------------------------------------------------------------- #


@mock_aws
def test_an_unlinked_row_is_link_revoked_and_never_refreshes():
    refresh = MagicMock(side_effect=_rotated)
    svc = _svc(refresh=refresh)
    row = svc._local["oid-1#c-1"]
    svc._local["oid-1#c-1"] = row.model_copy(update={"status": LinkStatus.UNLINKED})

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
    row = svc._local["oid-1#c-1"]
    svc._local["oid-1#c-1"] = row.model_copy(
        update={
            "status": LinkStatus.REFRESHING,
            "refresh_claimed_at": svc._clock.t.isoformat(),
        }
    )
    svc._clock.advance(seconds=30)  # inside the 60s claim timeout

    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    assert ei.value.kind == "refresh_in_progress"
    refresh.assert_not_called()  # NEVER a second rotation
    assert svc._local["oid-1#c-1"].status == LinkStatus.REFRESHING


def _stale_claim(svc, *, advance=61):
    """Leave the link REFRESHING with a claim older than the 60s timeout."""
    row = svc._local["oid-1#c-1"]
    svc._local["oid-1#c-1"] = row.model_copy(
        update={
            "status": LinkStatus.REFRESHING,
            "refresh_claimed_at": svc._clock.t.isoformat(),
        }
    )
    svc._clock.advance(seconds=advance)


@mock_aws
def test_an_abandoned_claim_whose_stored_token_is_dead_unlinks():
    # The claimer died mid-rotation and the pair really is dead at GitHub (the rotation had
    # already been spent), so the only honest state is UNLINKED + "re-link" — never a blind
    # retry with a possibly-spent refresh token.
    refresh = MagicMock(side_effect=_rotated)
    svc = _svc(refresh=refresh)
    svc._fetch_user_identity = MagicMock(
        side_effect=GitHubOAuthError("GitHub rejected the user token (HTTP 401)", kind="revoked")
    )
    _stale_claim(svc)

    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    assert ei.value.kind == "link_revoked"
    assert svc._local["oid-1#c-1"].status == LinkStatus.UNLINKED
    refresh.assert_not_called()  # the recovery probe is a READ; it never spends a refresh token


@mock_aws
def test_an_abandoned_claim_whose_stored_token_still_works_is_recovered_not_unlinked():
    # C3(b). The rotation succeeded and only the PUBLISH write failed, so the secret holds a
    # working pair. Unlinking here would force the human through the whole web flow again to
    # replace a credential that was never broken.
    refresh = MagicMock(side_effect=_rotated)
    svc = _svc(refresh=refresh)
    identity = MagicMock(return_value={"github_id": 583231, "github_login": "octocat"})
    svc._fetch_user_identity = identity
    _stale_claim(svc)

    assert svc.get_user_bearer_token("oid-1", "c-1") == ACCESS
    identity.assert_called_once()  # a read probe, not a rotation
    refresh.assert_not_called()
    row = svc._local["oid-1#c-1"]
    assert row.status == LinkStatus.LINKED and row.refresh_claimed_at is None


@mock_aws
def test_an_abandoned_claim_is_not_unlinked_on_a_transient_probe_failure():
    # Nothing is PROVEN dead, so nothing may be destroyed: a provider blip must not convert a
    # stale claim into a forced re-link.
    svc = _svc(refresh=MagicMock(side_effect=_rotated))
    svc._fetch_user_identity = MagicMock(
        side_effect=GitHubOAuthError("could not reach GitHub (ConnectError)")
    )
    _stale_claim(svc)

    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    assert ei.value.kind == "provider_error"  # retryable, not terminal
    assert svc._local["oid-1#c-1"].status == LinkStatus.REFRESHING  # nothing destroyed


# --------------------------------------------------------------------------- #
# Provider failure during rotation
# --------------------------------------------------------------------------- #


@mock_aws
def test_bad_refresh_token_unlinks_and_raises_link_revoked():
    refresh = MagicMock(
        side_effect=GitHubOAuthError("GitHub rejected the token refresh (bad_refresh_token)", kind="bad_grant")
    )
    svc = _svc(refresh=refresh)
    svc._clock.advance(seconds=28800)

    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    assert ei.value.kind == "link_revoked"
    assert svc._local["oid-1#c-1"].status == LinkStatus.UNLINKED
    # The dead pair is not left looking usable.
    assert _secret_body() == {"access_token": ACCESS, "refresh_token": REFRESH}


@mock_aws
def test_a_failed_rotation_never_retries():
    refresh = MagicMock(
        side_effect=GitHubOAuthError("could not reach GitHub to token refresh (ConnectError)")
    )
    svc = _svc(refresh=refresh)
    svc._clock.advance(seconds=28800)

    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    assert ei.value.kind == "provider_error"
    refresh.assert_called_once()  # exactly one attempt — a retry could double-rotate

    # The claim is RELEASED so the next request is not wedged at refresh_in_progress for 60s
    # on a transient network blip; the version bump stands, so a concurrent loser still loses.
    row = svc._local["oid-1#c-1"]
    assert row.status == LinkStatus.LINKED
    assert row.refresh_claimed_at is None


@mock_aws
def test_a_secret_write_failure_after_rotation_unlinks():
    # The rotated pair is lost and the old pair is already dead at GitHub — the link IS
    # broken, and saying so is the only safe answer.
    svc = _svc()
    svc._clock.advance(seconds=28800)
    svc._put_secret_body = MagicMock(
        side_effect=GitHubLinkError("Failed to rotate the link secret", kind="secret_error")
    )

    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    assert ei.value.kind == "link_revoked"
    assert svc._local["oid-1#c-1"].status == LinkStatus.UNLINKED


# --------------------------------------------------------------------------- #
# THE Problem-5 tests: two concurrent callers, ONE rotation.
# --------------------------------------------------------------------------- #


class _CasTable:
    """A DDB Table double that really enforces ``ConditionExpression=Attr(...).eq(...)``.

    Only the subset the service uses: ``get_item``/``put_item``/``update_item``/``delete_item``/
    ``query`` on a ("pk","sk") key, plus the two condition shapes the service builds
    (``Attr(x).eq(v)`` and ``Attr(x).exists()``). A failed condition raises the real
    ``ConditionalCheckFailedException`` shape, so the service's CAS path is exercised, not
    simulated. ``unconditional_puts`` exists so a test can assert that NO whole-row write
    escapes the version guard.

    ``update_item`` UPSERTS, because real ``UpdateItem`` does: an absent key is CREATED with the
    key attributes plus whatever the ``SET`` clause names. This double used to raise
    ``ValidationException`` there instead — modelling the OPPOSITE of production and hiding the
    fact that an unguarded ``_save_label`` fabricates a partial, unparseable row when the link is
    deleted mid-probe. A double that contradicts the service it stands in for hides exactly the
    class of bug it exists to catch, so the only thing that may stop an update here is a
    ``ConditionExpression``."""

    def __init__(self):
        self.items = {}
        self.conditional_puts = 0
        self.unconditional_puts = 0
        self.conditional_failures = 0
        self.conditional_updates = 0
        self.unconditional_updates = 0

    def get_item(self, Key):  # noqa: N803 — boto3 param name
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, ConditionExpression=None):  # noqa: N803 — boto3 param name
        key = (Item["pk"], Item["sk"])
        if ConditionExpression is None:
            self.unconditional_puts += 1
        else:
            self.conditional_puts += 1
            self._require(ConditionExpression, self.items.get(key), "PutItem")
        self.items[key] = dict(Item)

    def update_item(  # noqa: N803 — boto3 param names
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeValues,
        ReturnValues=None,
        ConditionExpression=None,
    ):
        """The narrow ``SET a = :x, b = :y`` shape ``_save_label`` builds — a TARGETED write
        that touches only the named attributes, so a stale ``token_version`` cannot ride along.
        Parsed rather than pattern-matched, so a service change that widened the expression to
        include ``status``/``token_version`` would show up in the merged item.

        UPSERTS like real ``UpdateItem``: with no item at the key, the write still lands and
        produces the key attributes plus the ``SET`` attributes and NOTHING else — the partial
        row that fails ``model_validate``. Only a ``ConditionExpression`` can prevent it."""
        key = (Key["pk"], Key["sk"])
        item = self.items.get(key)
        if ConditionExpression is None:
            self.unconditional_updates += 1
        else:
            self.conditional_updates += 1
            self._require(ConditionExpression, item, "UpdateItem")
        assert UpdateExpression.strip().startswith("SET ")
        merged = dict(item) if item is not None else dict(Key)  # UPSERT, exactly as DDB does
        for clause in UpdateExpression.strip()[4:].split(","):
            name, placeholder = (p.strip() for p in clause.split("="))
            merged[name] = ExpressionAttributeValues[placeholder]
        self.items[key] = merged
        return {"Attributes": dict(merged)} if ReturnValues == "ALL_NEW" else {}

    def _require(self, condition, item, operation):
        """Evaluate a condition against the item as it stands (possibly absent) and raise the
        real failure shape when it does not hold — one evaluator for both writers."""
        if _condition_holds(condition, item):
            return
        self.conditional_failures += 1
        raise ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "failed"}},
            operation,
        )

    def delete_item(self, Key):  # noqa: N803 — boto3 param name
        self.items.pop((Key["pk"], Key["sk"]), None)

    def query(self, **kwargs):
        pk, prefix = _condition_parts(kwargs["KeyConditionExpression"])
        rows = [dict(i) for k, i in self.items.items() if k[0] == pk]
        if prefix is not None:
            rows = [i for i in rows if str(i.get("sk", "")).startswith(prefix)]
        return {"Items": rows}


def _condition_holds(expr, item):
    """Evaluate the two condition shapes the service builds against ``item`` (``None`` ⇒ the key
    does not exist). Real DDB evaluates a condition on a MISSING item too — that is what makes
    ``attribute_exists`` a usable existence guard and what makes ``eq`` fail there."""
    if expr.expression_operator == "attribute_exists":
        (attr,) = expr._values
        return item is not None and attr.name in item
    assert expr.expression_operator == "=", f"unsupported condition {expr.expression_operator}"
    attr, expected = expr._values
    return item is not None and item.get(attr.name) == expected


def _condition_parts(expr):
    if expr.expression_operator == "AND":
        pk_cond, sk_cond = expr._values
        return pk_cond._values[1], sk_cond._values[1]
    return expr._values[1], None


def _ddb_svc(*, refresh=None, clock=None):
    """A service in DDB mode over ``_CasTable``, holding one LINKED link inside the skew."""
    svc = _svc(refresh=refresh, clock=clock)
    table = _CasTable()
    # Re-home the already-created row into the fake table, then flip to DDB mode.
    row = svc._local["oid-1#c-1"]
    svc.table_name = "connections"
    svc._table = table
    svc._save(row)
    svc._local.clear()
    assert svc._has_ddb is True
    table.conditional_puts = 0
    table.unconditional_puts = 0  # the re-homing put above is setup, not behaviour
    return svc


@mock_aws
def test_the_loser_of_a_concurrent_claim_does_not_rotate_twice():
    # THE Problem-5 test. TWO ECS tasks, one link, one destructive rotation: exactly ONE may
    # call GitHub. The interleaving is FORCED rather than hoped for — a barrier inside the row
    # read holds both callers until each has observed the same LINKED row at version 0, which
    # is the state the status gate cannot arbitrate (both see "linked, expiring") and only the
    # conditional write can.
    read_barrier = threading.Barrier(2)
    calls = []
    lock = threading.Lock()

    def counting_refresh(**kwargs):
        with lock:
            calls.append(kwargs["refresh_token"])
        return _rotated()

    svc = _ddb_svc(refresh=MagicMock(side_effect=counting_refresh))
    svc._clock.advance(seconds=28800 - 100)  # inside the skew

    real_get = svc._get
    synced = {"n": 0}

    def synced_get(principal_oid, connection_id):
        with lock:
            synced["n"] += 1
            first_two = synced["n"] <= 2
        row = real_get(principal_oid, connection_id)
        if first_two:
            read_barrier.wait(timeout=5)  # both callers now hold the SAME version-0 row
        return row

    svc._get = synced_get

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

    assert len(calls) == 1, "the destructive rotation ran twice"
    assert svc._table.conditional_failures == 1, "the CAS did not arbitrate the claim"
    outcomes = sorted(results.values())
    # One caller rotated; the other either read the freshly published token or was told to
    # retry — never a second rotation, and never a lockout.
    assert ("ok", NEW_ACCESS) in outcomes
    loser = [r for r in outcomes if r != ("ok", NEW_ACCESS)][0]
    assert loser in (("err", "refresh_in_progress"), ("ok", NEW_ACCESS))
    assert svc._table.items[("github_user_link", "oid-1#c-1")]["token_version"] == 1


@mock_aws
def test_the_loser_reads_the_published_token_when_the_winner_finished():
    # Deterministic version of the race: the loser's conditional write fails because the
    # winner already PUBLISHED, so the single re-read finds LINKED at a higher version and
    # returns the fresh token instead of erroring.
    refresh = MagicMock(side_effect=_rotated)
    svc = _ddb_svc(refresh=refresh)
    svc._clock.advance(seconds=28800 - 100)

    real_put = svc._table.put_item
    state = {"done": False}

    def racing_put(Item, ConditionExpression=None):  # noqa: N803
        if ConditionExpression is not None and not state["done"]:
            # Another task wins the claim, rotates, and publishes — all before our CAS lands.
            state["done"] = True
            svc.get_user_bearer_token("oid-1", "c-1")
        return real_put(Item, ConditionExpression=ConditionExpression)

    svc._table.put_item = racing_put

    assert svc.get_user_bearer_token("oid-1", "c-1") == NEW_ACCESS
    refresh.assert_called_once()  # the inner (winning) call only


@mock_aws
def test_the_loser_is_told_to_retry_when_the_winner_is_still_rotating():
    # The claim landed but no publish yet: the re-read sees REFRESHING at a higher version,
    # which is a retry, not a rotation.
    refresh = MagicMock(side_effect=_rotated)
    svc = _ddb_svc(refresh=refresh)
    svc._clock.advance(seconds=28800 - 100)

    real_put = svc._table.put_item
    state = {"done": False}

    def racing_put(Item, ConditionExpression=None):  # noqa: N803
        if ConditionExpression is not None and not state["done"]:
            state["done"] = True
            # Simulate the winner's CLAIM only (no publish).
            key = ("github_user_link", "oid-1#c-1")
            claimed = dict(svc._table.items[key])
            claimed["status"] = "refreshing"
            claimed["token_version"] = claimed["token_version"] + 1
            claimed["refresh_claimed_at"] = svc._clock.t.isoformat()
            svc._table.items[key] = claimed
        return real_put(Item, ConditionExpression=ConditionExpression)

    svc._table.put_item = racing_put

    with pytest.raises(GitHubLinkError) as ei:
        svc.get_user_bearer_token("oid-1", "c-1")
    assert ei.value.kind == "refresh_in_progress"
    refresh.assert_not_called()


@mock_aws
def test_the_claim_is_a_conditional_write_on_the_observed_version():
    refresh = MagicMock(side_effect=_rotated)
    svc = _ddb_svc(refresh=refresh)
    svc._clock.advance(seconds=28800 - 100)

    svc.get_user_bearer_token("oid-1", "c-1")

    # EVERY row write is conditional on the version the writer read — the claim AND the
    # publish. A version-blind put anywhere can erase a concurrent claim, and an erased claim
    # is how one refresh token gets spent twice (C1).
    assert svc._table.conditional_puts == 2
    assert svc._table.unconditional_puts == 0
    assert svc._table.conditional_failures == 0
    item = svc._table.items[("github_user_link", "oid-1#c-1")]
    assert item["token_version"] == 1 and item["status"] == "linked"


@mock_aws
def test_github_is_never_called_before_the_claim_lands():
    # Ordering matters more than it looks: calling GitHub first and CAS-ing after would let
    # both tasks rotate and only then discover the conflict — the destruction has already
    # happened by then.
    order = []
    refresh = MagicMock(side_effect=lambda **kw: (order.append("refresh"), _rotated())[1])
    svc = _ddb_svc(refresh=refresh)
    svc._clock.advance(seconds=28800 - 100)

    real_put = svc._table.put_item

    def tracking_put(Item, ConditionExpression=None):  # noqa: N803
        # Both writes are conditional now (every row writer is version-aware), so the CLAIM is
        # told apart by what it writes rather than by carrying a condition.
        assert ConditionExpression is not None, "a row write escaped the version guard"
        order.append("claim" if Item["status"] == "refreshing" else "publish")
        return real_put(Item, ConditionExpression=ConditionExpression)

    svc._table.put_item = tracking_put
    svc.get_user_bearer_token("oid-1", "c-1")

    assert order == ["claim", "refresh", "publish"]


@mock_aws
def test_a_concurrent_verify_link_cannot_erase_a_refresh_claim():
    # C1, the review's reproduced outage. `verify_link` reads its row, spends up to 15s on
    # GET /user, and then writes. Putting that stale row back would REGRESS token_version and
    # erase a claim landed in the meantime — after which a third caller's CAS on the old
    # version passes and the SAME `ghr_` token is spent a SECOND time. GitHub honours only one
    # of the two rotations, so the human is locked out of their own link.
    spent = []
    lock = threading.Lock()

    def counting_refresh(**kwargs):
        with lock:
            spent.append(kwargs["refresh_token"])
        return _rotated()

    svc = _ddb_svc(refresh=MagicMock(side_effect=counting_refresh))
    key = ("github_user_link", "oid-1#c-1")
    versions = []

    # A is mid-probe: it holds the row as it was at version 0.
    stale_row = svc._require_row("oid-1", "c-1")
    assert stale_row.token_version == 0

    svc._clock.advance(seconds=28800 - 100)  # inside the skew
    svc.get_user_bearer_token("oid-1", "c-1")  # B claims v0→v1 and spends the refresh token
    versions.append(svc._table.items[key]["token_version"])

    # A's probe returns and it writes its label update from the STALE row. Only A's OWN read is
    # stale — every other read in the flow stays real, so the write is the sole thing under test.
    svc._fetch_user_identity = MagicMock(
        return_value={"github_id": 583231, "github_login": "octocat-renamed"}
    )
    real_require = svc._require_row
    reads = {"n": 0}

    def stale_second_read(principal_oid, connection_id):
        reads["n"] += 1
        # 1st = get_user_bearer_token's read; 2nd = verify_link's own, the one A read pre-probe.
        return stale_row if reads["n"] == 2 else real_require(principal_oid, connection_id)

    svc._require_row = stale_second_read
    svc.verify_link("oid-1", "c-1")
    svc._require_row = real_require
    versions.append(svc._table.items[key]["token_version"])

    # The label moved; the concurrency token did NOT regress.
    assert svc._table.items[key]["github_login"] == "octocat-renamed"
    assert versions == [1, 1], f"token_version regressed: {versions}"
    assert svc._table.items[key]["status"] == "linked"

    # C now arrives on a fresh read. With A's write having erased the claim it would re-spend
    # the ORIGINAL refresh token; with the claim intact it can only ever spend the new one.
    svc._clock.advance(seconds=28800 - 100)
    svc.get_user_bearer_token("oid-1", "c-1")
    assert spent == [REFRESH, NEW_REFRESH], f"a refresh token was spent twice: {spent}"


@mock_aws
def test_a_link_deleted_during_the_verify_probe_is_not_fabricated_as_a_partial_row():
    # The re-review's Critical, and the reason `_CasTable.update_item` now upserts. DynamoDB
    # `UpdateItem` CREATES the item when the key is absent, so an unguarded label write on a link
    # `unlink` deleted during the up-to-15s `GET /user` window writes a 5-field orphan: pk, sk,
    # github_login, last_verified_at, updated_at. That row fails `GitHubUserLink.model_validate`,
    # and `_from_item` is applied to EVERY row by `_load_all_strict` — which `complete_link` calls
    # through `_refuse_foreign_binding`. One racing verify would therefore stop EVERY human on
    # EVERY connection from creating a link, with a raw pydantic ValidationError escaping the
    # `.kind` contract and no code path able to delete the orphan again.
    #
    # So the write must be GUARDED on the row pre-existing, and refusing must land in contract as
    # `not_found` — which is what the local branch already answers, so the two branches agree.
    svc = _ddb_svc(refresh=MagicMock(side_effect=_rotated))
    key = ("github_user_link", "oid-1#c-1")
    svc._fetch_user_identity = MagicMock(
        return_value={"github_id": 583231, "github_login": "octocat-renamed"}
    )

    row = svc._require_row("oid-1", "c-1")
    real_require = svc._require_row
    reads = {"n": 0}

    def deleting_second_read(principal_oid, connection_id):
        reads["n"] += 1
        if reads["n"] == 2:
            # verify_link's own read returns the row, and THEN the other task unlinks it — the
            # probe window. The row in hand is now a row that no longer exists.
            svc._delete(principal_oid, connection_id)
            return row
        return real_require(principal_oid, connection_id)

    svc._require_row = deleting_second_read

    with pytest.raises(GitHubLinkError) as ei:
        svc.verify_link("oid-1", "c-1")
    assert ei.value.kind == "not_found"  # in contract — never a raw ValidationError
    assert key not in svc._table.items, "the label write fabricated a row that was deleted"

    # And the partition is still readable, which is the part that made this cross-tenant: a
    # single unparseable orphan breaks `complete_link` for everybody.
    assert svc._load_all_strict() == []


@mock_aws
def test_the_local_fallback_refuses_the_same_deleted_link():
    # Parity: the DDB and local branches must answer a mid-probe delete identically. The local
    # branch is the one that was already right, so this pins it against a future "simplification"
    # that makes it upsert to match a mistaken reading of DDB.
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
    assert svc._local == {}


@mock_aws
def test_the_table_double_upserts_on_update_item_exactly_as_dynamodb_does():
    # The double is load-bearing evidence, so its fidelity is asserted rather than assumed. Real
    # `UpdateItem` on an absent key CREATES the item; the previous double raised
    # ValidationException there, which modelled the OPPOSITE of production and made the Critical
    # above untestable. Unguarded ⇒ a partial row appears; guarded ⇒ the condition refuses.
    table = _CasTable()
    key = {"pk": "github_user_link", "sk": "oid-x#c-x"}
    table.update_item(
        Key=key,
        UpdateExpression="SET github_login = :login, updated_at = :ts",
        ExpressionAttributeValues={":login": "octocat", ":ts": "2026-07-29T12:00:00+00:00"},
    )
    assert table.items[("github_user_link", "oid-x#c-x")] == {
        **key,
        "github_login": "octocat",
        "updated_at": "2026-07-29T12:00:00+00:00",
    }, "the double must UPSERT, or it hides the bug it exists to catch"

    # ...and the existence guard is what stops it.
    table.items.clear()
    with pytest.raises(ClientError) as ei:
        table.update_item(
            Key=key,
            UpdateExpression="SET github_login = :login",
            ExpressionAttributeValues={":login": "octocat"},
            ConditionExpression=Attr("pk").exists(),
        )
    assert ei.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
    assert table.items == {}


@mock_aws
def test_a_stale_unlink_publish_cannot_clobber_a_newer_claim():
    # C1, the same class on the OTHER writers. `_publish_unlinked` and `_release_claim` both put
    # a whole row read earlier, so an unlink decided against version 0 must not land on a row a
    # concurrent task has already claimed at version 1 — that erases the claim and re-opens the
    # double-spend window just as a stale `verify_link` write would.
    svc = _ddb_svc(refresh=MagicMock(side_effect=_rotated))
    key = ("github_user_link", "oid-1#c-1")

    stale_row = svc._require_row("oid-1", "c-1")
    assert stale_row.token_version == 0

    # Another task claims the refresh: the row moves to REFRESHING at version 1.
    svc._clock.advance(seconds=28800 - 100)
    claimed = stale_row.model_copy(
        update={
            "status": LinkStatus.REFRESHING,
            "token_version": 1,
            "refresh_claimed_at": svc._clock.t.isoformat(),
        }
    )
    svc._save(claimed)

    # The stale decision tries to publish over it. It must be DROPPED, not applied.
    svc._publish_unlinked(stale_row)
    assert svc._table.items[key]["status"] == "refreshing"
    assert svc._table.items[key]["token_version"] == 1

    # Same for a stale release.
    svc._release_claim(stale_row)
    assert svc._table.items[key]["status"] == "refreshing"
    assert svc._table.items[key]["token_version"] == 1


@mock_aws
def test_a_released_claim_is_not_mistaken_for_a_completed_refresh():
    # C2. A transient provider failure releases the claim but KEEPS the version bump, leaving
    # `LINKED` at a higher version while the secret still holds the OLD, expiring pair. A loser
    # that trusts the bump alone is handed a corpse: its consumer 401s, nothing marks the link
    # dead, and the retry repeats it forever. Freshness must come from the EXPIRY.
    svc = _svc(
        refresh=MagicMock(
            side_effect=GitHubOAuthError("could not reach GitHub to token refresh (ConnectError)")
        )
    )
    svc._clock.advance(seconds=28800 + 3600)  # the access token expired an hour ago

    loser_row = svc._require_row("oid-1", "c-1")  # a loser holding the row at version 0

    with pytest.raises(GitHubLinkError):
        svc.get_user_bearer_token("oid-1", "c-1")  # the winner claims, fails, releases
    released = svc._local["oid-1#c-1"]
    assert released.status == LinkStatus.LINKED and released.token_version == 1
    assert _secret_body() == {"access_token": ACCESS, "refresh_token": REFRESH}  # NOT rotated

    with pytest.raises(GitHubLinkError) as ei:
        svc._read_after_lost_claim(loser_row)
    # Retryable, not a dead token presented as live.
    assert ei.value.kind == "refresh_in_progress"


@mock_aws
def test_a_publish_write_failure_stays_inside_the_error_contract():
    # C3(a). The publish write is the one step that used to sit outside any `try`, so a DDB
    # fault escaped as a raw ClientError — an HTTP 500 outside this epic's pinned status set,
    # which T8's `except GitHubLinkError` mapping would miss entirely.
    svc = _ddb_svc(refresh=MagicMock(side_effect=_rotated))
    svc._clock.advance(seconds=28800 - 100)

    real_put = svc._table.put_item

    def failing_publish(Item, ConditionExpression=None):  # noqa: N803
        if Item["status"] == "linked":  # the publish, not the claim
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "PutItem"
            )
        return real_put(Item, ConditionExpression=ConditionExpression)

    svc._table.put_item = failing_publish

    with pytest.raises(GitHubLinkError) as ei:  # never a bare ClientError
        svc.get_user_bearer_token("oid-1", "c-1")
    assert ei.value.kind == "secret_error"  # 502, retryable — the stored pair is GOOD

    # And the good pair is still there to be recovered rather than thrown away.
    assert _secret_body() == {"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH}
    assert svc._table.items[("github_user_link", "oid-1#c-1")]["status"] == "refreshing"


@mock_aws
def test_the_local_fallback_enforces_the_same_single_flight_guard():
    # The local branch is a real code path (dev + every service test), so it must arbitrate
    # too — a lock-free dict would let dev double-rotate against LIVE GitHub.
    #
    # I3: this test used to pass with the local compare-and-set DELETED, because the two `_get`
    # calls were unsynchronized and thread B in practice read AFTER A's claim had landed — so
    # the STATUS gate (REFRESHING → refresh_in_progress) arbitrated and `len(calls) == 1` was
    # satisfied without the CAS ever running. It now does what its DDB sibling does: a barrier
    # INSIDE the read forces both threads to hold the same version-0 row (the one state a status
    # gate cannot separate), and the compare-and-set is observed DIRECTLY by counting _ClaimLost.
    # ONE barrier, and it sits inside the READ. A second barrier inside the refresh (holding the
    # winner until the loser arrives) is what made this test deadlock intermittently: the loser
    # is already past its own read, so nothing guarantees it reaches the refresh barrier at all.
    # The read barrier alone is what the property needs — after it, both threads provably hold
    # the same version-0 row, and only the compare-and-set can separate them.
    read_barrier = threading.Barrier(2)
    calls = []
    lock = threading.Lock()

    def counting_refresh(**kwargs):
        with lock:
            calls.append(kwargs["refresh_token"])
        return _rotated()

    svc = _svc(refresh=MagicMock(side_effect=counting_refresh))
    svc._clock.advance(seconds=28800 - 100)

    real_get = svc._get
    synced = {"n": 0}

    def synced_get(principal_oid, connection_id):
        with lock:
            synced["n"] += 1
            first_two = synced["n"] <= 2
        row = real_get(principal_oid, connection_id)
        if first_two:
            read_barrier.wait(timeout=5)  # both callers now hold the SAME version-0 row
        return row

    svc._get = synced_get

    # Count the compare-and-set losses directly, so the assertion cannot be satisfied by the
    # status gate the way it silently was before.
    real_save_claim = svc._save_guarded
    claim_lost = []

    def counting_save_guarded(record, *, expected_version):
        try:
            real_save_claim(record, expected_version=expected_version)
        except _ClaimLost:
            with lock:
                claim_lost.append(expected_version)
            raise

    svc._save_guarded = counting_save_guarded

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

    assert len(calls) == 1, "the destructive rotation ran twice"
    # THE point of the test: the loser lost to the compare-and-set on version 0, not to a
    # status check. Delete the local CAS and this is 0.
    assert claim_lost == [0], f"the local CAS did not arbitrate the claim: {claim_lost}"
    assert svc._local["oid-1#c-1"].token_version == 1
    outcomes = sorted(results.values())
    assert ("ok", NEW_ACCESS) in outcomes
    loser = [r for r in outcomes if r != ("ok", NEW_ACCESS)][0]
    assert loser in (("err", "refresh_in_progress"), ("ok", NEW_ACCESS))
