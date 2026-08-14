"""`/health` must not disclose internals to anonymous callers (E34/T9).

`/health` and `/ping` are among the handful of routes that are deliberately PUBLIC (no auth
dependency, registered without `API_PREFIX` — `main.py:280`). That makes the probe failure paths a
disclosure surface, not merely a logging concern: `health.py` interpolated `str(e)` from its
DynamoDB/SQLAlchemy and S3 probes straight into the response body, so an unauthenticated caller
could read whatever the exception happened to carry — bucket names, table ARNs, DSNs, account ids.
Everywhere else in this backend maps errors to fixed literals (`main.py:270` returns
"An error occurred" unless DEBUG); `/health` was the outlier.

The two properties pinned here are in tension, which is why both are asserted per probe:
  1. the response body carries NONE of the exception detail, and
  2. the component is still reported UNHEALTHY/degraded.
Redaction that also swallowed the failure signal would pass (1) while making the endpoint useless
to the operator it exists for — so (2) is what stops "return 'healthy' unconditionally" from being
a legal way to pass this file.

Also pins amendment A4: the anonymous `/test` endpoint (which echoed `ROOT_PATH` to any caller) is
gone and cannot silently return, and pins that the STAGE-prefixed `/health` — the only
internet-reachable one — really runs this hardened code rather than a hard-coded `"healthy"`. The
last test extends that same rule to `{ROOT_PATH}/ping`, whose hand-written twin returned a
different body until E36/T14 deleted it.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# The sentinel is shaped like the worst thing these probes could realistically leak: a real ARN
# naming a private bucket. The substring sweep in `_assert_body_is_clean` is defence in depth
# only — what makes the guard TOTAL is that every test also pins each component's message to the
# exact fixed literal below, so no partial leak (a truncated message, the bare exception class
# name, a table or bucket name that happens to miss every sentinel substring) can pass.
_SENTINEL_ARN = "arn:aws:s3:::super-secret-bucket"
_SENTINEL_BUCKET = "super-secret-bucket"
_LEAKY_MESSAGE = (
    f"An error occurred (AccessDenied) when calling HeadBucket: {_SENTINEL_ARN} "
    "is not accessible from arn:aws:iam::123456789012:role/cp-dev-ecs-task-role"
)

# The exact strings `health.py` is allowed to put in the body on a probe failure. Equality, not
# `!=  "connected"`: an enumerated blocklist can only catch the leaks someone thought of.
_DB_FAILURE_LITERAL = "error: database probe failed"
_S3_FAILURE_LITERAL = "error: s3 probe failed"

# `main` bakes `ROOT_PATH` into its stage route paths at import time, so the stage tests must
# re-import it. These are the module prefixes that hold a `settings` reference.
_RELOAD_PREFIXES = ("api.", "core.config")


def _health_client(db):
    """A minimal app carrying ONLY the health router, with `get_db` overridden.

    Mirrors the route-test idiom used across this suite (`tests/test_grants_routes.py:142`):
    build a bare `FastAPI()` rather than importing `main`, so no startup hook, no settings
    validation and no other router participates in the assertion.
    """
    from api.routes import health_router
    from core.database import get_db

    app = FastAPI()
    app.include_router(health_router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _assert_body_is_clean(response_text: str) -> None:
    """No form of the exception detail may appear in the response body."""
    assert _SENTINEL_ARN not in response_text
    assert _SENTINEL_BUCKET not in response_text
    assert "arn:" not in response_text
    assert "AccessDenied" not in response_text
    assert "HeadBucket" not in response_text
    assert "Traceback" not in response_text
    assert "123456789012" not in response_text


def _without_timestamp(body: dict) -> dict:
    """The one field that legitimately differs between two calls to the same handler."""
    return {k: v for k, v in body.items() if k != "timestamp"}


@contextmanager
def _main_reloaded_with_stage_prefix(monkeypatch):
    """Import `main` with `ROOT_PATH=/dev` set, then evict it again.

    `ROOT_PATH` defaults to `""` locally, so any stage assertion made against the app as already
    imported would be VACUOUSLY true while every deployed environment behaves differently
    (`ecs/main.tf` always sets `ROOT_PATH=/${environment}`). The reload is what makes the two
    stage tests below real. House pattern: `tests/test_github_link_routes.py:604`.
    """
    import sys

    monkeypatch.setenv("ROOT_PATH", "/dev")

    def _evict():
        for key in [k for k in sys.modules if k == "main" or k.startswith(_RELOAD_PREFIXES)]:
            sys.modules.pop(key, None)

    _evict()
    try:
        import main

        yield main
    finally:
        _evict()


# --- the database probe ------------------------------------------------------


def test_database_probe_failure_does_not_leak_the_exception(caplog):
    """A raising DB probe reports `unhealthy` without echoing the exception.

    S3 is patched HEALTHY here on purpose, isolating the DB probe: left unpatched, the real
    `S3Service` is constructed with an empty bucket name and boto3 raises its own parameter-
    validation error, so the assertion would no longer be about the database probe at all.
    """
    db = MagicMock()
    db.execute.side_effect = RuntimeError(_LEAKY_MESSAGE)

    with patch("api.routes.health.S3Service") as s3_cls:
        s3_cls.return_value.check_bucket_accessible.return_value = True
        with caplog.at_level("WARNING", logger="api.routes.health"):
            response = _health_client(db).get("/health")

    assert response.status_code == 200
    _assert_body_is_clean(response.text)

    body = response.json()
    # Property 2: the failure still SHOWS. Redaction must not cost the operator the signal.
    assert body["status"] == "unhealthy"
    assert body["checks"]["database"] == _DB_FAILURE_LITERAL
    assert body["checks"]["s3"] == "accessible"

    # The detail is not destroyed, only relocated: operators keep it server-side.
    assert _SENTINEL_ARN in caplog.text


# --- the S3 probe -----------------------------------------------------------


def test_s3_probe_failure_does_not_leak_the_exception(caplog):
    """A raising S3 probe reports `degraded` without echoing the exception."""
    db = MagicMock()

    with patch("api.routes.health.S3Service") as s3_cls:
        s3_cls.return_value.check_bucket_accessible.side_effect = RuntimeError(_LEAKY_MESSAGE)
        with caplog.at_level("WARNING", logger="api.routes.health"):
            response = _health_client(db).get("/health")

    assert response.status_code == 200
    _assert_body_is_clean(response.text)

    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["s3"] == _S3_FAILURE_LITERAL
    assert body["checks"]["database"] == "connected"

    assert _SENTINEL_ARN in caplog.text


def test_both_probes_failing_leaks_nothing_and_still_reports_unhealthy(caplog):
    """Both probes down: two redactions in one response, and both failures still show.

    Deliberately asserts `status != "healthy"` rather than a specific string. The top-level
    `status` is assigned by whichever `except` ran LAST, so a DB failure ("unhealthy") is
    currently overwritten by a subsequent S3 failure ("degraded") — a pre-existing precedence
    bug, orthogonal to disclosure and out of this task's scope. Pinning either literal here
    would either fail for the wrong reason or freeze that bug into the suite; the per-component
    assertions below are the ones that carry the signal-survives property.
    """
    db = MagicMock()
    db.execute.side_effect = RuntimeError(_LEAKY_MESSAGE)

    with patch("api.routes.health.S3Service") as s3_cls:
        s3_cls.return_value.check_bucket_accessible.side_effect = RuntimeError(_LEAKY_MESSAGE)
        with caplog.at_level("WARNING", logger="api.routes.health"):
            response = _health_client(db).get("/health")

    assert response.status_code == 200
    _assert_body_is_clean(response.text)

    body = response.json()
    assert body["status"] != "healthy"
    assert body["checks"]["database"] == _DB_FAILURE_LITERAL
    assert body["checks"]["s3"] == _S3_FAILURE_LITERAL

    # Both probes logged their own exception: redaction relocated the detail twice, and a future
    # change that redacts the body while ALSO dropping the server-side log fails here.
    assert caplog.text.count(_SENTINEL_ARN) == 2


# --- amendment A4: the anonymous /test endpoint is gone ----------------------


def test_no_test_route_exists_even_when_root_path_is_set(monkeypatch):
    """The anonymous `/test` endpoint must not exist (E34/T9 amendment A4).

    `ROOT_PATH` is the whole point of the reload: `/test` was registered INSIDE
    `if settings.ROOT_PATH:` (`main.py:169`), and `ROOT_PATH` defaults to `""` locally — so
    against the default app this assertion would be vacuously true while the route stayed live
    in every deployed environment (`ecs/main.tf` always sets `ROOT_PATH=/${environment}`).
    Reloading `main` with the stage prefix set is what makes the check real.

    The sibling stage routes are asserted PRESENT in the same breath: that proves the stage
    block still executes, so a future edit cannot satisfy this test by disabling the block
    wholesale (which would 404 the cloud health check instead).
    """
    with _main_reloaded_with_stage_prefix(monkeypatch) as main:
        from conftest import app_route_paths

        paths = app_route_paths(main.app)

        # Every comparison below is made on TRAILING-SLASH-NORMALISED paths. Registering the
        # route as `f"{ROOT_PATH}/test/"` would otherwise evade this whole test: the path string
        # is then `/dev/test/`, which is neither `"/dev/test"` nor within a `p.count("/") <= 2`
        # bound — while FastAPI's `redirect_slashes` keeps `GET /dev/test` reaching it via 307.
        stripped = {p.rstrip("/") for p in paths}

        # The stage block ran...
        assert "/dev/ping" in paths
        assert "/dev/health" in paths
        assert "/dev/" in paths
        # ...and carries no /test escape hatch, at the stage prefix or bare.
        assert "/dev/test" not in stripped
        assert "/test" not in stripped

        # Catch a re-add under a different spelling, but only at the TOP level: the depth bound
        # is what keeps the legitimate authenticated `/api/v1/admin/connections/{id}/test`
        # (E19 connection credential check, behind `require_role`) out of this net. What is
        # forbidden is an unauthenticated test route sitting directly under `/` or the stage.
        top_level_test_routes = [
            q for q in stripped if q.split("/")[-1] == "test" and q.count("/") <= 2
        ]
        assert not top_level_test_routes


# --- the stage-prefixed /health is the REAL one ------------------------------


def test_stage_prefixed_health_serves_the_hardened_route(monkeypatch, caplog):
    """`{ROOT_PATH}/health` must run `health.py`, not a hard-coded `"healthy"`.

    This is the health URL that matters: API Gateway forwards the stage segment (`$default`
    route, `HTTP_PROXY`, no `overwrite:path` mapping), so `{ROOT_PATH}/health` is the only
    INTERNET-reachable one — the ALB serving bare `/health` is `internal = true`. `main.py` used
    to declare an explicit `{ROOT_PATH}/health` returning `{"status": "healthy"}`, and because
    Starlette matches in registration order it SHADOWED the real route that
    `include_router(health_router, prefix=ROOT_PATH)` registers. Two live consequences: the
    public endpoint reported `healthy` through a total database outage, and every redaction
    asserted above never executed on the public path at all.

    Asserted as equality with the internal path's body (timestamp aside) rather than as a
    property list, so the two can never drift apart again — including by someone "fixing" this
    with a second copy of the probe logic.
    """
    db = MagicMock()
    db.execute.side_effect = RuntimeError(_LEAKY_MESSAGE)

    with _main_reloaded_with_stage_prefix(monkeypatch) as main:
        # `core.database` is NOT evicted by the reload, so this is the same `get_db` object the
        # reloaded `api.routes.health` bound into its `Depends(...)`.
        from core.database import get_db

        main.app.dependency_overrides[get_db] = lambda: db
        with patch("api.routes.health.S3Service") as s3_cls:
            s3_cls.return_value.check_bucket_accessible.return_value = True
            client = TestClient(main.app)
            with caplog.at_level("WARNING", logger="api.routes.health"):
                staged = client.get("/dev/health")
                bare = client.get("/health")

    assert staged.status_code == 200
    _assert_body_is_clean(staged.text)

    staged_body = staged.json()
    assert staged_body["status"] == "unhealthy"
    assert staged_body["checks"]["database"] == _DB_FAILURE_LITERAL
    assert _without_timestamp(staged_body) == _without_timestamp(bare.json())

    assert _SENTINEL_ARN in caplog.text


# --- the stage-prefixed /ping is the REAL one too ----------------------------


def test_stage_prefixed_ping_serves_the_same_route_as_bare_ping(monkeypatch):
    """`{ROOT_PATH}/ping` must answer with `health.py`'s body, not a hand-written twin (E36/T14).

    The same shadowing defect as `/health` above, one endpoint over. `main.py` declared
    `@app.get(f"{ROOT_PATH}/ping")` returning `{"message": "pong"}` inside the stage block, so —
    registered before `include_router(health_router, prefix=ROOT_PATH)` and matched in
    registration order — the internet-facing `/ping` returned a key (`message`) that the real
    route never produces, and never the `timestamp` it always produces. Two callers of the same
    documented endpoint got two different contracts depending on which spelling they hit.

    Body equality with the bare path (timestamp aside) is the assertion, not "has a `ping` key":
    a re-added twin that happened to copy today's keys would still be a second copy free to drift,
    and equality is what forbids it. `{ROOT_PATH}/` is the deliberate exception — bare `/` is
    declared on `app` with no router behind it, so its twin has nothing to fall back to.
    """
    with _main_reloaded_with_stage_prefix(monkeypatch) as main:
        client = TestClient(main.app)
        staged = client.get("/dev/ping")
        bare = client.get("/ping")

    assert staged.status_code == 200
    assert bare.status_code == 200
    assert _without_timestamp(staged.json()) == _without_timestamp(bare.json())
    # Spelled out, because equality alone would also be satisfied by two identical wrong bodies:
    # this is `health.py`'s contract, and the deleted twin's `{"message": "pong"}` fails it.
    assert staged.json()["ping"] == "pong"
    assert "message" not in staged.json()
    assert "timestamp" in staged.json()
