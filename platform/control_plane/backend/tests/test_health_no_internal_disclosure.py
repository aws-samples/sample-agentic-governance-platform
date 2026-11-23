"""`/health` must not disclose internals to anonymous callers.

`/health` and `/ping` are among the handful of routes that are deliberately PUBLIC (no auth
dependency, registered without `API_PREFIX` — `main.py:280`). That makes the probe failure path a
disclosure surface, not merely a logging concern: `health.py` interpolated `str(e)` from its
database and S3 probes straight into the response body, so an unauthenticated caller could read
whatever the exception happened to carry — bucket names, table ARNs, DSNs, account ids.
Everywhere else in this backend maps errors to fixed literals (`main.py:270` returns
"An error occurred" unless DEBUG); `/health` was the outlier. The database probe has since been
deleted with the persistence path behind it; `/health` probes S3 alone, and
`test_health_body_has_no_database_check` pins that the `database` key does not come back.

The two properties pinned here are in tension, which is why both are asserted on the probe:
  1. the response body carries NONE of the exception detail, and
  2. the component is still reported degraded.
Redaction that also swallowed the failure signal would pass (1) while making the endpoint useless
to the operator it exists for — so (2) is what stops "return 'healthy' unconditionally" from being
a legal way to pass this file.

Also pins amendment A4: the anonymous `/test` endpoint (which echoed `ROOT_PATH` to any caller) is
gone and cannot silently return, and pins that the STAGE-prefixed `/health` — the only
internet-reachable one — really runs this hardened code rather than a hard-coded `"healthy"`. The
last test extends that same rule to `{ROOT_PATH}/ping`, whose hand-written twin returned a
different body until it was deleted.

The sentinel is shaped like the worst thing the probe could realistically leak: a real ARN naming
a private bucket. The substring sweep in `_assert_body_is_clean` is defence in depth only; what makes
the guard total is that every test also pins the component's message to the exact fixed literal
`health.py` may emit, so no partial leak (a truncated message, the bare exception class name, a
table or bucket name that misses every sentinel substring) can pass.
"""

from contextlib import contextmanager
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

_SENTINEL_ARN = "arn:aws:s3:::super-secret-bucket"
_SENTINEL_BUCKET = "super-secret-bucket"
_LEAKY_MESSAGE = (
    f"An error occurred (AccessDenied) when calling HeadBucket: {_SENTINEL_ARN} "
    "is not accessible from arn:aws:iam::123456789012:role/cp-dev-ecs-task-role"
)

_S3_FAILURE_LITERAL = "error: s3 probe failed"

_RELOAD_PREFIXES = ("api.", "core.config")


def _health_client():
    """A minimal app carrying ONLY the health router.

    Mirrors the route-test idiom used across this suite (`tests/test_grants_routes.py:142`):
    build a bare `FastAPI()` rather than importing `main`, so no startup hook, no settings
    validation and no other router participates in the assertion.
    """
    from api.routes import health_router

    app = FastAPI()
    app.include_router(health_router)
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
    (`ecs/main.tf` always sets `ROOT_PATH=/${environment}`). `main` bakes `ROOT_PATH` into its stage
    route paths at import time, so the modules holding a `settings` reference (`_RELOAD_PREFIXES`)
    are evicted with it.
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


def test_s3_probe_failure_does_not_leak_the_exception(caplog):
    """A raising S3 probe reports `degraded` without echoing the exception."""
    with patch("api.routes.health._s3_service") as s3_service:
        s3_service.return_value.check_bucket_accessible.side_effect = RuntimeError(_LEAKY_MESSAGE)
        with caplog.at_level("WARNING", logger="api.routes.health"):
            response = _health_client().get("/health")

    assert response.status_code == 200
    _assert_body_is_clean(response.text)

    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["s3"] == _S3_FAILURE_LITERAL

    assert _SENTINEL_ARN in caplog.text


def test_health_body_has_no_database_check():
    """The S3 probe is the only check; the deleted database probe does not come back under any key."""
    with patch("api.routes.health._s3_service") as s3_service:
        s3_service.return_value.check_bucket_accessible.return_value = True
        response = _health_client().get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert set(body) == {"status", "timestamp", "checks"}
    assert "database" not in body["checks"]
    assert set(body["checks"]) == {"s3"}
    assert body["checks"]["s3"] == "accessible"


def test_an_inaccessible_bucket_is_degraded_without_being_an_error():
    """The probe's ``False`` answer is the third fixed literal; the body keeps today's keys."""
    with patch("api.routes.health._s3_service") as s3_service:
        s3_service.return_value.check_bucket_accessible.return_value = False
        response = _health_client().get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"] == {"s3": "not accessible"}
    assert set(body) == {"status", "timestamp", "checks"}


def test_health_and_ping_declare_their_bodies_with_exactly_todays_keys():
    """The document names the two bodies ``smoke.sh`` reads, each with the keys the handler emits."""
    from api.routes import health_router

    app = FastAPI()
    app.include_router(health_router)
    doc = app.openapi()
    schemas = doc["components"]["schemas"]

    def declared(path: str) -> str:
        schema = doc["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        return schema["$ref"].rsplit("/", 1)[1]

    assert declared("/health") == "HealthResponse"
    assert set(schemas["HealthResponse"]["properties"]) == {"status", "timestamp", "checks"}
    assert set(schemas["HealthChecks"]["properties"]) == {"s3"}
    assert declared("/ping") == "PingResponse"
    assert set(schemas["PingResponse"]["properties"]) == {"ping", "timestamp"}


def test_no_test_route_exists_even_when_root_path_is_set(monkeypatch):
    """The anonymous `/test` endpoint must not exist.

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

        # Trailing slashes are normalised: `f"{ROOT_PATH}/test/"` would otherwise evade every
        # comparison below while FastAPI's `redirect_slashes` still served `GET /dev/test` via 307.
        stripped = {p.rstrip("/") for p in paths}

        assert "/dev/ping" in paths
        assert "/dev/health" in paths
        assert "/dev/" in paths
        assert "/dev/test" not in stripped
        assert "/test" not in stripped

        # The depth bound keeps the authenticated `/api/v1/admin/connections/{id}/test` out of this
        # net; what is forbidden is an unauthenticated test route directly under `/` or the stage.
        top_level_test_routes = [q for q in stripped if q.split("/")[-1] == "test" and q.count("/") <= 2]
        assert not top_level_test_routes


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
    with _main_reloaded_with_stage_prefix(monkeypatch) as main:
        with patch("api.routes.health._s3_service") as s3_service:
            s3_service.return_value.check_bucket_accessible.side_effect = RuntimeError(_LEAKY_MESSAGE)
            client = TestClient(main.app)
            with caplog.at_level("WARNING", logger="api.routes.health"):
                staged = client.get("/dev/health")
                bare = client.get("/health")

    assert staged.status_code == 200
    _assert_body_is_clean(staged.text)

    staged_body = staged.json()
    assert staged_body["status"] == "degraded"
    assert staged_body["checks"]["s3"] == _S3_FAILURE_LITERAL
    assert _without_timestamp(staged_body) == _without_timestamp(bare.json())

    assert _SENTINEL_ARN in caplog.text


def test_stage_prefixed_ping_serves_the_same_route_as_bare_ping(monkeypatch):
    """`{ROOT_PATH}/ping` must answer with `health.py`'s body, not a hand-written twin.

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
    assert staged.json()["ping"] == "pong"
    assert "message" not in staged.json()
    assert "timestamp" in staged.json()
