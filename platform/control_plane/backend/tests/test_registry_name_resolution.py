"""Offline tests for resolving an AWS Agent Registry NAME to its registryId.

WHAT THIS GUARDS AND WHY IT IS WORTH A FILE
-------------------------------------------
AWS mints the registryId — ``RegistryIdentifier`` accepts an ARN or a generated 12-16 char id
and never a name — and there is no Terraform resource for the ``agent-registry`` namespace, so
the id used to reach the backend through a capture file that Terraform read during the PLAN
walk, i.e. before the ``local-exec`` provisioner that writes it had run. That is what forced a
from-zero deploy to ``terraform apply`` TWICE. The backend now resolves the id from the NAME
(a static config value) at first use instead, which removes the capture file, the second apply
and the empty-id-tolerating guards in one move.

Every behaviour below is a failure mode that is INVISIBLE in production if it regresses:

* a name that resolves to nothing must be LOUD — an "empty registry" reading renders an inert
  UI with no error anywhere, which is precisely the symptom this change exists to kill;
* a duplicate name must REFUSE rather than pick one — picking wrong writes governance records
  into a registry nobody selected, and nothing errors because the records land somewhere;
* a transient AWS failure must NOT be cached — a memoised failure turns a 30-second outage
  into "restart the ECS task";
* an explicit id must still short-circuit the lookup — six operational scripts pass ids
  directly, and ``AGENT_REGISTRY_ID`` / ``MCP_REGISTRY_ID`` remain supported overrides.

No AWS call is made: every client is an injected ``MagicMock``, per the suite convention
(``tests/conftest.py``'s ``mock_registry_clients``; no moto — research §10).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.registry_resolver import (
    AmbiguousRegistryNameError,
    RegistryNotConfiguredError,
    RegistryNotFoundError,
    resolve_registry_id,
)
from services.agent_registry_service import AgentRegistryService
from services.mcp_server_service import McpServerRegistryService

_ARN = "arn:aws:agent-registry:us-east-1:123456789012:registry/REGAGENTS0001"


def _ctl(registries=None, *, pages=None, list_error=None):
    """A control client whose ``list_registries`` paginator yields ``pages``.

    ``pages`` (rather than only a flat list) is what lets a test prove the scan spans MORE than
    one page — a name living on page 2 is the realistic shape once an account holds a few
    registries, and a single-page fixture cannot fail if pagination is dropped.
    """
    ctl = MagicMock()
    paginator = ctl.get_paginator.return_value
    if list_error is not None:
        paginator.paginate.side_effect = list_error
    else:
        paginator.paginate.return_value = pages or [{"registries": registries or []}]
    return ctl


def _reg(name, rid, **extra):
    item = {"name": name, "registryId": rid, "registryArn": f"{_ARN[:-14]}{rid}", "status": "READY"}
    item.update(extra)
    return item


# ---------------------------------------------------------------------------
# The resolver itself.
# ---------------------------------------------------------------------------


def test_resolves_the_id_from_the_name():
    ctl = _ctl([_reg("other", "REGOTHER00001"), _reg("agp-agents", "REGAGENTS0001")])
    assert resolve_registry_id("agp-agents", "us-east-1", ctl=ctl) == "REGAGENTS0001"
    # The lookup is the paginated ListRegistries call and nothing else — in particular it does
    # NOT try to filter server-side on name, which the model does not support (the only
    # filterable fields are `status` and `discoveryConfiguration.authorizerType`).
    ctl.get_paginator.assert_called_once_with("list_registries")
    ctl.get_paginator.return_value.paginate.assert_called_once_with()


def test_resolution_spans_every_page():
    """A match on page 2 must be found. Guards the pagination, which a one-page fake cannot."""
    ctl = _ctl(
        pages=[
            {"registries": [_reg("noise-1", "REGNOISE00001")]},
            {"registries": [_reg("agp-mcp-servers", "REGMCP0000001")]},
        ]
    )
    assert resolve_registry_id("agp-mcp-servers", "us-east-1", ctl=ctl) == "REGMCP0000001"


def test_an_unknown_name_fails_LOUDLY_and_actionably():
    """The message must NAME the registry and tell the operator how to create it.

    The alternative — treating "not found" as an empty registry — is the failure mode this
    whole change exists to remove: the UI renders zero agents, no error is logged anywhere, and
    the operator has nothing to go on. So the assertion is on the CONTENT of the message, not
    merely on the exception type."""
    ctl = _ctl([_reg("something-else", "REGELSE000001")])
    with pytest.raises(RegistryNotFoundError) as exc:
        resolve_registry_id("agp-agents", "eu-west-1", ctl=ctl)

    msg = str(exc.value)
    assert "agp-agents" in msg, msg  # names the registry it looked for
    assert "eu-west-1" in msg, msg  # …and where it looked
    assert "terraform apply" in msg, msg  # …and the normal fix
    assert "ensure_registry.py" in msg and "--name agp-agents" in msg, msg  # …and the manual one


def test_a_duplicate_name_is_a_HARD_ERROR_naming_both_ids():
    """AWS does not enforce unique registry names, so two can share one.

    DECISION: refuse. Silently picking one writes agent/MCP records into a registry nobody
    selected — and because the records land SOMEWHERE, nothing errors; the only symptom is a
    catalog that looks halved, which is indistinguishable from data loss. The error names both
    ids so the operator can tell which to delete."""
    ctl = _ctl([_reg("agp-agents", "REGFIRST00001"), _reg("agp-agents", "REGSECOND0001")])
    with pytest.raises(AmbiguousRegistryNameError) as exc:
        resolve_registry_id("agp-agents", "us-east-1", ctl=ctl)

    msg = str(exc.value)
    assert "REGFIRST00001" in msg and "REGSECOND0001" in msg, msg
    # Non-vacuity: the duplicate must be detected even when the two entries sit on DIFFERENT
    # pages, which is the case a short-circuit-on-first-match implementation passes anyway.
    paged = _ctl(
        pages=[
            {"registries": [_reg("agp-agents", "REGFIRST00001")]},
            {"registries": [_reg("agp-agents", "REGSECOND0001")]},
        ]
    )
    with pytest.raises(AmbiguousRegistryNameError):
        resolve_registry_id("agp-agents", "us-east-1", ctl=paged)


def test_an_aws_failure_propagates_unchanged():
    """A throttle / credential / region failure must NOT be reported as "not found".

    Swallowing it would send an operator to create a registry that already exists."""
    boom = RuntimeError("ThrottlingException: Rate exceeded")
    ctl = _ctl(list_error=boom)
    with pytest.raises(RuntimeError) as exc:
        resolve_registry_id("agp-agents", "us-east-1", ctl=ctl)
    assert exc.value is boom


# ---------------------------------------------------------------------------
# The two services: lazy resolution, memoisation, and the id override.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "service_cls,name",
    [(AgentRegistryService, "agp-agents"), (McpServerRegistryService, "agp-mcp-servers")],
    ids=["agents", "mcp"],
)
def test_service_resolves_by_name_lazily_and_memoises(service_cls, name):
    """Constructing the service makes NO call; the first use resolves once and caches.

    Laziness is the property that lets the route-level singletons be built at import time
    without touching AWS, and memoisation is what keeps a per-request `registry_id` read from
    becoming a per-request `ListRegistries`."""
    ctl = _ctl([_reg(name, "REGRESOLVED01")])
    svc = service_cls(registry_name=name, control_client=ctl)

    # Construction alone must not have looked anything up.
    ctl.get_paginator.assert_not_called()

    assert svc.registry_id == "REGRESOLVED01"
    assert svc.registry_id == "REGRESOLVED01"  # second read is served from the cache
    assert ctl.get_paginator.call_count == 1


@pytest.mark.parametrize(
    "service_cls,name",
    [(AgentRegistryService, "agp-agents"), (McpServerRegistryService, "agp-mcp-servers")],
    ids=["agents", "mcp"],
)
def test_an_explicit_id_OVERRIDES_and_short_circuits_the_lookup(service_cls, name):
    """`AGENT_REGISTRY_ID` / `MCP_REGISTRY_ID` and the six scripts that pass ids must keep
    working — and must cost no AWS call. The name is supplied TOO here, so this proves
    precedence rather than merely the absence of a name."""
    ctl = _ctl([_reg(name, "REGRESOLVED01")])
    svc = service_cls(registry_id="REGPINNED0001", registry_name=name, control_client=ctl)

    assert svc.registry_id == "REGPINNED0001"
    ctl.get_paginator.assert_not_called()


@pytest.mark.parametrize(
    "service_cls", [AgentRegistryService, McpServerRegistryService], ids=["agents", "mcp"]
)
def test_the_positional_registry_id_signature_still_works(service_cls):
    """`AgentRegistryService(registry_id=..., control_client=...)` is what `conftest` and every
    script pass. Keeping `registry_id` FIRST (and positional) is a compatibility contract, not
    a style choice."""
    ctl = _ctl()
    assert service_cls("REGPOSITIONAL", control_client=ctl).registry_id == "REGPOSITIONAL"
    ctl.get_paginator.assert_not_called()


@pytest.mark.parametrize(
    "service_cls", [AgentRegistryService, McpServerRegistryService], ids=["agents", "mcp"]
)
def test_neither_id_nor_name_fails_on_first_use_not_on_construction(service_cls):
    """An unconfigured service must say so, rather than address the EMPTY-STRING registryId the
    old required-`registry_id` signature happily accepted (which 400s per call, or worse,
    reads as "no records")."""
    svc = service_cls(control_client=_ctl())  # constructing is fine…
    with pytest.raises(RegistryNotConfiguredError) as exc:
        svc.registry_id  # …using it is not
    assert "REGISTRY_NAME" in str(exc.value) and "REGISTRY_ID" in str(exc.value)


@pytest.mark.parametrize(
    "service_cls,name",
    [(AgentRegistryService, "agp-agents"), (McpServerRegistryService, "agp-mcp-servers")],
    ids=["agents", "mcp"],
)
def test_a_TRANSIENT_failure_is_NOT_cached_and_the_retry_succeeds(service_cls, name):
    """THE POISONED-SINGLETON GUARD.

    These services are process-lifetime singletons behind `get_service()`. If a failed lookup
    were memoised (e.g. by caching "" or the exception), a single throttle at the wrong moment
    would leave every subsequent request failing until the ECS task was restarted — an outage
    whose cause is unfindable from the symptom. Only a SUCCESS may be cached, so the first
    call raises and the second, once AWS answers, resolves normally."""
    ctl = _ctl()
    ctl.get_paginator.return_value.paginate.side_effect = [
        RuntimeError("ThrottlingException: Rate exceeded"),
        [{"registries": [_reg(name, "REGAFTERRETRY")]}],
    ]
    svc = service_cls(registry_name=name, control_client=ctl)

    with pytest.raises(RuntimeError):
        svc.registry_id
    assert svc.registry_id == "REGAFTERRETRY"  # the retry is not blocked by the first failure

    # A "not found" is transient in exactly the same way (the registry may not exist YET, mid
    # apply), so it must not poison the instance either.
    ctl2 = _ctl()
    ctl2.get_paginator.return_value.paginate.side_effect = [
        [{"registries": []}],
        [{"registries": [_reg(name, "REGCREATEDNOW")]}],
    ]
    svc2 = service_cls(registry_name=name, control_client=ctl2)
    with pytest.raises(RegistryNotFoundError):
        svc2.registry_id
    assert svc2.registry_id == "REGCREATEDNOW"


# ---------------------------------------------------------------------------
# Import purity — the property that makes lazy resolution meaningful.
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "module",
    ["core.registry_resolver", "services.agent_registry_service", "services.mcp_server_service"],
)
def test_importing_the_registry_modules_builds_no_client_and_needs_no_settings(module):
    """Importing must not construct a boto3 client, reach AWS, or require env config.

    Run in a SUBPROCESS with `PYTHONPATH=src` and a bare env (no `.env` in cwd, no fixtures,
    no conftest network guard) because that is the only way to observe a TRUE import — the
    in-process one already happened at collection. `boto3.client` / `boto3.resource` /
    `boto3.Session` are replaced with raisers BEFORE the import, so an import-time client (the
    thing that would actually hit credentials, IMDS or an endpoint) fails the test loudly;
    merely `import boto3` at module scope is fine and is what these two services have always
    done. Region/credential resolution and every API call live behind those constructors.

    Running from a bare env is also the "needs no settings" half: the import chain reaches
    `core.config`, so this fails if any registry setting ever becomes required. That preserves
    the property `tests/test_api_properties.py` protects by bypassing `src/__init__.py` to
    avoid triggering `Settings()` validation."""
    code = (
        "import boto3\n"
        "def _boom(*a, **k):\n"
        "    raise AssertionError('a boto3 client/session was constructed AT IMPORT TIME')\n"
        "boto3.client = _boom\n"
        "boto3.resource = _boom\n"
        "boto3.Session = _boom\n"
        "boto3.session.Session = _boom\n"
        f"__import__({module!r})\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_BACKEND_DIR,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(_BACKEND_DIR / "src")},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout.strip() == "OK"


def test_the_resolver_itself_does_not_even_IMPORT_boto3():
    """`core.registry_resolver` imports boto3 inside `_client` only.

    Stricter than the check above and deliberately so: this module is imported by
    `scripts/ensure_registry.py`, which Terraform runs through a bare interpreter, and it is
    the module whose whole contract is "resolving is a lookup, not a dependency"."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import core.registry_resolver\n"
            "leaked = [m for m in ('boto3', 'botocore') if m in sys.modules]\n"
            "assert not leaked, f'import pulled in {leaked}'\n"
            "print('OK')\n",
        ],
        cwd=_BACKEND_DIR,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(_BACKEND_DIR / "src")},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"


def test_the_bootstrap_script_and_the_backend_share_ONE_find_by_name():
    """`scripts/ensure_registry.py` must not carry a second implementation.

    Two copies of "find the registry named X" WILL drift — most consequentially on the
    duplicate-name decision, where the script creating-or-finding one registry and the backend
    reading another would be a genuinely confusing split. Asserted by identity, so a
    copy-paste divergence fails here rather than in production."""
    import core.registry_resolver as resolver
    import scripts.ensure_registry as script

    assert script.find_registry_by_name is resolver.find_registry_by_name
    assert script.SERVICE_NAME == resolver.SERVICE_NAME == "agent-registry-control"


# ===========================================================================
# HTTP surface: the actionable message must reach the OPERATOR, not just CloudWatch
# ===========================================================================
#
# Every message above is written to be the whole remedy — it names the registry, the region,
# `terraform apply`, the `ensure_registry.py --name` fallback and the settings involved. That
# investment was worth nothing in production: these exceptions fell through to `main.py`'s
# `@app.exception_handler(Exception)`, which returns
# `{"detail": "Internal server error", "error": "An error occurred"}` unless `settings.DEBUG`
# — and DEBUG defaults to False and is NOT set in the ECS task definition. So the one person who
# could fix the misconfiguration got a bare 500, while the explanation sat in logs they had no
# reason to open.
#
# These tests pin the dedicated handler that fixes it. They drive the handler `main.py` REGISTERS
# (copied off `main.app.exception_handlers`) rather than a hand-rolled equivalent — a test that
# built its own handler would pass while main.py's wiring was missing entirely, which is the
# regression that matters.


def _app_with_mains_handlers(exc: Exception):
    """A minimal app carrying `main.app`'s exception handlers and one route that raises `exc`.

    Uses `main.app`'s OWN handler map so the wiring is under test, not just the behaviour. A
    minimal app rather than `main.app` itself because the real one needs Entra auth, a tenant
    resolver and a project resolver on the registry routes — none of which this is about, and all
    of which would have to be faked into place to observe a status code that the handler decides
    on its own.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import main as main_module

    app = FastAPI()
    for exc_type, handler in main_module.app.exception_handlers.items():
        app.add_exception_handler(exc_type, handler)

    @app.get("/boom")
    async def boom():
        raise exc

    # raise_server_exceptions=False so the generic `Exception` handler's response is observable
    # instead of being re-raised into the test.
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "exc",
    [
        RegistryNotFoundError(
            "No AWS Agent Registry named 'agp-agents' exists in region 'eu-west-1'. Create it "
            "with `terraform apply` in platform/control_plane/infrastructure."
        ),
        RegistryNotConfiguredError(
            "AgentRegistryService has neither a registry id nor a registry name; set "
            "AGENT_REGISTRY_NAME."
        ),
        AmbiguousRegistryNameError(
            "2 registries are named 'agp-agents' (ids: AAA, BBB). Delete the one that should "
            "not exist, then retry."
        ),
    ],
    ids=["not_found", "not_configured", "ambiguous"],
)
def test_a_resolution_failure_returns_503_WITH_the_actionable_message(exc, monkeypatch):
    """503, and the resolver's own message VERBATIM in the body — with DEBUG false.

    DEBUG is forced False explicitly because that is the production shape and the entire point:
    the old behaviour only ever showed the message when DEBUG was true, which it never is in ECS.
    Asserting on message CONTENT rather than merely on the status is deliberate — a 503 whose body
    said "An error occurred" would be the same failure wearing a better number.

    503 rather than 404: the REQUEST was fine. Nothing is missing from the catalog; the platform
    cannot reach its registry. A 404 would send the operator hunting for deleted data. 503 also
    tells the truth about retryability — the identical request succeeds once the registry exists.
    """
    import main as main_module

    monkeypatch.setattr(main_module.settings, "DEBUG", False)

    resp = _app_with_mains_handlers(exc).get("/boom")

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == str(exc)
    # Non-vacuity: the body carries the parts that make the message actionable, not just a type
    # name. `str(exc)` alone would still pass if the message had degraded to "not found".
    assert "agp-agents" in resp.text or "AGENT_REGISTRY_NAME" in resp.text, resp.text
    # And it must NOT have been laundered through the generic handler.
    assert "An error occurred" not in resp.text, resp.text


def test_every_OTHER_exception_still_gets_the_generic_masked_500(monkeypatch):
    """The exemption is per-type, and this is the guard that keeps it that way.

    The tempting shortcut for the test above is to make the generic handler leak `str(exc)`
    unconditionally, which would surface the registry message AND every other exception's — a
    connection string in a DB error, a token in a botocore message. This test fails if anyone
    "fixes" the problem that way: an unrelated exception must still be masked with DEBUG false.
    """
    import main as main_module

    monkeypatch.setattr(main_module.settings, "DEBUG", False)

    resp = _app_with_mains_handlers(
        RuntimeError("postgres://user:hunter2@db.internal:5432 refused the connection")
    ).get("/boom")

    assert resp.status_code == 500, resp.text
    assert resp.json()["error"] == "An error occurred"
    assert "hunter2" not in resp.text, resp.text


def test_the_503_response_carries_CORS_headers_so_the_SPA_can_READ_it(monkeypatch):
    """An actionable message the browser refuses to hand over is not actionable.

    `CORSMiddleware` does not decorate a response produced by an exception handler, so without
    the explicit re-attachment the SPA sees a bare CORS failure — no status, no body — and the
    operator is back to guessing. The generic handler has always done this; the point here is
    that the new handler does it too (both go through the shared `_with_cors`)."""
    import main as main_module

    monkeypatch.setattr(main_module.settings, "DEBUG", False)
    origin = main_module.settings.CORS_ORIGINS[0]

    resp = _app_with_mains_handlers(RegistryNotFoundError("no registry named 'agp-agents'")).get(
        "/boom", headers={"origin": origin}
    )

    assert resp.status_code == 503
    assert resp.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize(
    "exc_type",
    [RegistryNotFoundError, RegistryNotConfiguredError, AmbiguousRegistryNameError],
)
def test_main_REGISTERS_a_handler_for_each_resolver_exception(exc_type):
    """Pins the wiring by type, so deleting one decorator is caught even though all three
    currently share a handler function. `RegistryNotConfiguredError` is the easiest to lose —
    it is raised by the services rather than the resolver, so it reads as unrelated."""
    import main as main_module

    assert exc_type in main_module.app.exception_handlers, (
        f"{exc_type.__name__} has no handler in main.py, so it falls through to the generic "
        "500 handler and its message is replaced with 'An error occurred' in production"
    )


# ===========================================================================
# Status is part of the match: a failed bootstrap must not poison its own remedy
# ===========================================================================
#
# Matching on name ALONE created a state that broke the fix for itself. The realistic route to a
# duplicate name is an interrupted bootstrap: attempt 1 leaves a registry in CREATE_FAILED,
# attempt 2 creates one that reaches READY, and the account now holds two named `agp-agents`.
# Name-only matching found both, raised AmbiguousRegistryNameError, and the platform hard-errored
# on EVERY registry request — permanently, until a human deleted the corpse. And because
# `scripts/ensure_registry.py` shares this exact function, the operator's most natural remedy
# (re-run the bootstrap) refused too, instead of adopting the healthy registry. A self-healing
# state became an outage, and the remedy path was the thing that was broken.
#
# The statuses below are the real `RegistryStatus` enum from the pinned botocore model, read
# offline: CREATING, READY, UPDATING, CREATE_FAILED, UPDATE_FAILED, DELETING, DELETE_FAILED.


def test_a_READY_registry_is_preferred_over_a_CREATE_FAILED_twin():
    """THE case the finding is about: the interrupted-bootstrap pair.

    Both are named `agp-agents`. Name-only matching saw 2 matches and refused forever; only the
    READY one is a real candidate, so resolution must succeed and return it. Ordering is not
    contractual, so the broken one is placed FIRST — a "take the first match" implementation that
    merely stopped counting failures would return the corpse and pass a weaker test."""
    ctl = _ctl(
        [
            _reg("agp-agents", "REGBROKEN0001", status="CREATE_FAILED"),
            _reg("agp-agents", "REGHEALTHY001", status="READY"),
        ]
    )
    assert resolve_registry_id("agp-agents", "us-east-1", ctl=ctl) == "REGHEALTHY001"


def test_only_a_BROKEN_registry_errors_CLEARLY_rather_than_saying_not_found():
    """"It exists but is unusable" and "it does not exist" have different remedies.

    This is why the non-viable filter is applied CLIENT-side even though the API can filter on
    `status` server-side: a server-side filter would return an empty page and make this case
    indistinguishable from plain absence — i.e. it would report "no registry named 'agp-agents'"
    about a registry the operator can see in the console, which is the most confusing message
    this resolver could possibly emit. The message must instead name the id, name the status, and
    say to delete it and re-run."""
    ctl = _ctl([_reg("agp-agents", "REGBROKEN0001", status="CREATE_FAILED")])
    with pytest.raises(RegistryNotFoundError) as exc:
        resolve_registry_id("agp-agents", "us-east-1", ctl=ctl)

    msg = str(exc.value)
    assert "REGBROKEN0001" in msg, msg
    assert "CREATE_FAILED" in msg, msg
    assert "exists but" in msg, msg
    # And it must point at a remedy, not merely describe the state.
    assert "Delete" in msg and "ensure_registry.py" in msg, msg


def test_TWO_VIABLE_registries_STILL_hard_error():
    """The deliberate decision the status refinement must NOT erode.

    Narrowing "duplicate" to "duplicate among viable" is the fix; weakening it to "pick one" is
    not. Two READY registries sharing a name is genuinely ambiguous — records land in whichever
    one resolved first, nothing errors, and the only symptom is a catalog that looks halved. Both
    ids are still named so the operator can tell which to delete.

    The cross-page variant is kept for the same reason the original test has one: a
    short-circuit-on-first-viable-match implementation cannot see the second."""
    ctl = _ctl([_reg("agp-agents", "REGFIRST00001"), _reg("agp-agents", "REGSECOND0001")])
    with pytest.raises(AmbiguousRegistryNameError) as exc:
        resolve_registry_id("agp-agents", "us-east-1", ctl=ctl)
    msg = str(exc.value)
    assert "REGFIRST00001" in msg and "REGSECOND0001" in msg, msg

    paged = _ctl(
        pages=[
            {"registries": [_reg("agp-agents", "REGFIRST00001")]},
            {"registries": [_reg("agp-agents", "REGSECOND0001")]},
        ]
    )
    with pytest.raises(AmbiguousRegistryNameError):
        resolve_registry_id("agp-agents", "us-east-1", ctl=paged)


@pytest.mark.parametrize("status", ["CREATE_FAILED", "DELETING", "DELETE_FAILED"])
def test_a_broken_twin_does_not_make_a_healthy_registry_AMBIGUOUS(status):
    """Every non-viable status must be skipped, not merely CREATE_FAILED.

    DELETING and DELETE_FAILED matter as much: adopting either writes governance records into a
    registry someone is actively removing, and counting either towards the duplicate check
    reinstates the permanent hard error. Parametrised so adding a status to the denylist without
    covering it here is visible."""
    ctl = _ctl(
        [
            _reg("agp-agents", "REGBROKEN0001", status=status),
            _reg("agp-agents", "REGHEALTHY001", status="READY"),
        ]
    )
    assert resolve_registry_id("agp-agents", "us-east-1", ctl=ctl) == "REGHEALTHY001"


@pytest.mark.parametrize("status", ["READY", "CREATING", "UPDATING", "UPDATE_FAILED"])
def test_every_VIABLE_status_still_resolves(status):
    """The other half, and the one that keeps the denylist from creeping into an allowlist.

    CREATING is LOAD-BEARING: `ensure_registry.py` adopts a match and then waits on the
    `registry_ready` waiter, so a registry found mid-create is fine — it is waited for. If
    CREATING were treated as non-viable, the bootstrap would CREATE A SECOND registry beside one
    already coming up, manufacturing the very duplicate-name state it exists to avoid.

    UPDATE_FAILED is the non-obvious inclusion, and deliberate: unlike CREATE_FAILED it describes
    a registry that WAS ready and still holds every record — only a config change failed.
    Refusing it would be an outage we inflicted on a registry that reads and writes fine."""
    ctl = _ctl([_reg("agp-agents", "REGHEALTHY001", status=status)])
    assert resolve_registry_id("agp-agents", "us-east-1", ctl=ctl) == "REGHEALTHY001"


def test_an_UNKNOWN_future_status_is_treated_as_VIABLE():
    """Denylist, not allowlist — asserted, because it is a decision and not an accident.

    The two directions are not symmetric. If AWS adds a status value and we treated unknowns as
    non-viable, a HEALTHY registry would stop resolving the day that happened — an outage caused
    by our own optimism, on a working registry. Treating an unknown as viable at worst addresses a
    registry that then errors on its own terms, which is loud and local. The duplicate-name hard
    error remains the backstop if a future failure status ever co-exists with a healthy twin."""
    ctl = _ctl([_reg("agp-agents", "REGHEALTHY001", status="SOME_FUTURE_STATE")])
    assert resolve_registry_id("agp-agents", "us-east-1", ctl=ctl) == "REGHEALTHY001"


def test_a_MISSING_status_field_is_treated_as_VIABLE():
    """Robustness against a fake/older response shape that omits `status` entirely.

    Same asymmetry as the unknown-status case: `None` is not in the denylist, so it resolves. This
    also keeps every pre-existing fixture in this suite meaningful rather than silently
    unresolvable if `status` were ever dropped from `_reg`."""
    item = {"name": "agp-agents", "registryId": "REGHEALTHY001", "registryArn": "arn:x"}
    assert resolve_registry_id("agp-agents", "us-east-1", ctl=_ctl([item])) == "REGHEALTHY001"


def test_a_TRULY_absent_name_still_reports_ABSENCE_not_brokenness():
    """Non-vacuity guard for the broken-registry message: the two must stay distinguishable.

    A single message covering both would be the easy shortcut and it would send an operator to
    delete a registry that does not exist. Absence keeps the original "no AWS Agent Registry
    named X exists" wording; brokenness gets the "exists but ... is in a usable state" one."""
    ctl = _ctl([_reg("something-else", "REGOTHER00001")])
    with pytest.raises(RegistryNotFoundError) as exc:
        resolve_registry_id("agp-agents", "us-east-1", ctl=ctl)

    msg = str(exc.value)
    assert "No AWS Agent Registry named" in msg, msg
    assert "exists but" not in msg, msg


def test_find_by_name_returns_NONE_for_absence_so_the_bootstrap_can_CREATE():
    """`ensure_registry.py`'s find-or-CREATE contract: absence stays an ordinary answer.

    Only the read-only callers turn absence into an error (via `resolve_registry_id`). If this
    raised instead of returning `(None, None)`, the bootstrap could never create the first
    registry in a fresh account — the from-zero deploy would be impossible."""
    from core.registry_resolver import find_registry_by_name

    assert find_registry_by_name(_ctl([]), "agp-agents") == (None, None)
    # ... but a name that matches only a corpse is NOT absence: returning (None, None) there
    # would make the bootstrap create a SECOND registry beside the broken one, which is how the
    # duplicate-name state gets manufactured in the first place.
    broken = _ctl([_reg("agp-agents", "REGBROKEN0001", status="CREATE_FAILED")])
    with pytest.raises(RegistryNotFoundError):
        find_registry_by_name(broken, "agp-agents")


def test_the_denylist_is_a_SUBSET_of_the_real_RegistryStatus_enum():
    """Pins the denylist against the AWS model itself, offline.

    A typo like `CREATE_FAILURE` would silently disable the whole fix — the string would never
    match, every corpse would count as viable, and the permanent hard error would be back with
    every test above still green in a different way. Read from the pinned botocore model, so this
    also fails loudly if AWS renames a status out from under the denylist."""
    from botocore.session import get_session

    from core.registry_resolver import _NON_VIABLE_STATUSES

    shapes = (
        get_session()
        .get_service_model("agent-registry-control")
        ._shape_resolver._shape_map
    )
    enum = set(shapes["RegistryStatus"]["enum"])

    assert _NON_VIABLE_STATUSES <= enum, _NON_VIABLE_STATUSES - enum
    # Non-vacuity: the enum really was read, and READY is deliberately NOT denied.
    assert "READY" in enum and "READY" not in _NON_VIABLE_STATUSES
