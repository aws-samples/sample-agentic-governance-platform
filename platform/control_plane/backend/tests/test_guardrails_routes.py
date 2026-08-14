"""RBAC + tenant scoping of the guardrails routes (E34/T5) — this vertical's FIRST tests.

Before E34 the guardrails vertical had ZERO test coverage, and the reason was structural:
``GuardrailService.__init__`` built a DynamoDB resource, a ``Table`` handle, a Bedrock client
and a CloudWatch client EAGERLY (``guardrail_service.py:127-133``), so merely constructing the
service reached for AWS credentials/IMDS — which conftest's outbound-network guard correctly
refuses. E34/T2 defaulted ``GUARDRAILS_TABLE_NAME`` to ``""`` and T5 makes an empty table name
mean "in-memory store, no boto3 client built" (the ``TenantService`` / ``ProjectRoleService``
convention), so the vertical becomes testable with this suite's ``MagicMock`` idiom — no moto.

Fixture idiom is ``test_projects_role_gating.py`` (the RBAC-table precedent: the autouse module
reset, the entra env fixture, a ``client_factory`` that starts a ``verify_entra_token`` patch
for the client's whole life, and NO ``dependency_overrides`` — the REAL ``require_role`` /
``current_principal`` / ``get_tenant_ctx`` chain runs) plus ``test_registry_tenant_scoping.py``
(the E24 tenant contract: a foreign detail 404 byte-identical to a truly-missing id).

What this pins:
  - the required role PER ROUTE for all EIGHT endpoints, that one level below is a 403 with
    ``require_role``'s fixed literal, and that the refusal lands BEFORE the service call — so
    no billable Bedrock guardrail is created/updated and none is destroyed on a refused path;
  - unauthenticated (no bearer) is 401 on every endpoint;
  - the E24 tenant contract: the list is post-filtered by ``visible()``; a foreign OR missing
    ``{template_id}`` is the SAME 404 body; ALL FIVE ``{template_id}`` routes are gated (missing
    one is this task's stated failure mode, so the five are checked against the router's own
    registered routes rather than a hand-written list); create into an unknown tenant is 400,
    into a foreign tenant 403;
  - attribution comes from the principal (``email or oid``), and the literal
    ``created_by="user"`` is gone from the module;
  - ``/presets`` still resolves ahead of ``/{template_id}``;
  - an empty table name builds NO boto3 client and still round-trips in memory;
  - (E34/T5b) the 502 for a failed create is a FIXED literal — it never forwards the service's
    status-history message, whose AWS failure text names the account id and execution role.
    The service-level half of that fix lives in ``test_guardrail_service.py``.

RED-FIRST NOTE FOR THE IMPLEMENTER: against today's un-retrofitted code this file DOES collect
(pydantic 2.x defaults to ``extra="ignore"``, so the ``tenant_id=`` kwarg ``_template()`` passes
to today's ``GuardrailTemplate`` is silently dropped rather than raising). It fails on
behaviour: no auth ⇒ every RBAC/401 case fails, no tenant dimension ⇒ every tenant case fails,
eager boto3 ⇒ both contract-5 cases fail. The two route-ordering cases already pass today —
they are regression guards, not new behaviour.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from models.guardrail import GuardrailMetrics, GuardrailStatus, GuardrailTemplate

FIXED_TS = "2026-08-11T00:00:00+00:00"

# The EXISTING 404 literal, reused byte-identically (guardrails.py:72/82/92/114).
NOT_FOUND = "Guardrail template not found"
# ``require_role``'s fixed refusal (core/rbac.py:108) and the no-token literal (:92/:155).
NO_TOKEN = "Missing authorization token"


def _insufficient(role: str) -> str:
    return f"Requires {role} role or higher"


@pytest.fixture(autouse=True)
def reset_modules():
    """Drop cached auth/config/route modules so monkeypatched env is honored."""
    import sys

    for mod in [
        "core.rbac",
        "core.security_entra",
        "core.config",
        "api.routes.guardrails",
        "api.routes.users",
        "api.routes.tenants",
    ]:
        sys.modules.pop(mod, None)
    yield


@pytest.fixture
def entra_settings(monkeypatch):
    monkeypatch.setenv("AUTH_PROVIDER", "entra")
    monkeypatch.setenv("USE_DEV_AUTH", "False")
    # DEBUG matters as much as USE_DEV_AUTH: either one switches on the dev-auth bypass, whose
    # header default is ADMIN — a stray DEBUG=true in a local .env would make every RBAC case
    # below pass vacuously.
    monkeypatch.setenv("DEBUG", "False")
    monkeypatch.setenv("ENTRA_TENANT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("ENTRA_AUDIENCE", "api://agp")
    monkeypatch.setenv("ENTRA_ROLE_ADMIN", "Platform.Admin")
    monkeypatch.setenv("ENTRA_ROLE_OPERATOR", "Platform.Operator")
    monkeypatch.setenv("ENTRA_ROLE_VIEWER", "Platform.Viewer")
    # T2's new default, pinned here too: even if a test reached the real ``get_service()``,
    # an empty table name must never build a DynamoDB client.
    monkeypatch.setenv("GUARDRAILS_TABLE_NAME", "")


# --- record / model helpers --------------------------------------------------

def _template(template_id="gr-1", tenant_id="ten-1", **overrides):
    """A real ``GuardrailTemplate`` for the mocked service's return values.

    ``guardrail_id`` is populated by default so the metrics route reaches ``get_metrics``
    instead of its 400 "no Bedrock resource (still in draft)" branch.
    """
    base = dict(
        template_id=template_id,
        name="fsi-standard",
        description="Standard FSI guardrail",
        status=GuardrailStatus.ACTIVE,
        guardrail_id="gr-bedrock-abc",
        guardrail_arn="arn:aws:bedrock:us-east-1:111122223333:guardrail/gr-bedrock-abc",
        guardrail_version="DRAFT",
        tenant_id=tenant_id,
        created_by="maria.bauer@example.com",
        created_at=FIXED_TS,
        updated_at=FIXED_TS,
    )
    base.update(overrides)
    return GuardrailTemplate(**base)


def _valid_create(tenant_id="ten-1"):
    """A minimal valid POST body. ``tenant_id`` is REQUIRED on create — the E24 rule already
    carried by ``AgentCreate`` (models/agent.py:222) and ``McpServerCreate`` (:94)."""
    return {
        "name": "fsi-standard",
        "description": "Standard FSI guardrail",
        "tenant_id": tenant_id,
    }


def _valid_update():
    return {"description": "tightened"}


# --- context + resolver fakes ------------------------------------------------

def _tenant_context(*, is_global=False, tenant_ids=("ten-1",)):
    from services.tenant_resolver import TenantContext

    return TenantContext(is_global=is_global, tenant_ids=frozenset(tenant_ids), tenants=())


class _FakeTenantResolver:
    """Async ``resolve`` stub returning a fixed context regardless of principal."""

    def __init__(self, ctx):
        self._ctx = ctx

    async def resolve(self, principal):
        return self._ctx


class _FakeTenantService:
    """Minimal ``TenantService``-shaped fake: only ``.get`` is exercised (by create)."""

    def __init__(self, known_ids):
        self._known = set(known_ids)

    def get(self, tenant_id):
        from services.tenant_service import TenantError

        if tenant_id not in self._known:
            raise TenantError("Unknown tenant", kind="not_found")
        return MagicMock(id=tenant_id)


_CLAIMS = {
    "viewer": "Platform.Viewer",
    "operator": "Platform.Operator",
    "admin": "Platform.Admin",
}


def _claims_for(platform_role: str):
    return {
        "oid": f"{platform_role}-oid",
        "preferred_username": f"{platform_role}.user@example.com",
        "roles": [_CLAIMS[platform_role]],
    }


# --- the shared service / client fixtures ------------------------------------

@pytest.fixture
def guardrail_svc():
    """The ``GuardrailService`` MagicMock on the guardrails module global (the ``_svc``
    injection idiom — no real DynamoDB / Bedrock / CloudWatch is ever constructed).

    Defaults are the happy path for all EIGHT endpoints, so the ROLE or TENANT gate is the
    only thing each test below is observing.
    """
    import api.routes.guardrails as guardrails_module
    from services.guardrail_service import FSI_PRESETS

    svc = MagicMock()
    svc.get_presets.return_value = FSI_PRESETS
    svc.create_template.return_value = _template(template_id="gr-new")
    svc.list_templates.return_value = [_template()]
    svc.get_template.return_value = _template()
    svc.update_template.return_value = _template()
    svc.delete_template.return_value = _template(status=GuardrailStatus.DELETED)
    svc.publish_version.return_value = _template(guardrail_version="1")
    svc.get_metrics.return_value = GuardrailMetrics(guardrail_id="gr-bedrock-abc")
    guardrails_module._svc = svc
    return svc


@pytest.fixture
def client_factory(entra_settings, guardrail_svc):
    """Build a TestClient for a caller with a given PLATFORM role + tenant scope.

    Mirrors ``test_projects_role_gating.py:273``: seed the ONE tenant-resolver singleton
    (``users._tenant_resolver`` — the accessor ``guardrails.get_tenant_ctx`` delegates to) and
    the tenant-service singleton (``tenants._svc``, read by the create route), build a minimal
    app with ONLY the guardrails router, and start a ``verify_entra_token`` patch for the
    client's whole life so table-driven tests need no ``with patch(...)`` block.

    ``authenticated=False`` is the one addition to the precedent's shape: it drops the
    Authorization header so the 401 path (no principal at all) is reachable. A client-level
    default header cannot be removed per-request, and the precedent file has no 401 coverage.
    """
    built = []

    def _make(
        *,
        role="operator",
        tenant_ctx=None,
        known_tenants=("ten-1", "ten-2"),
        authenticated=True,
    ):
        import api.routes.guardrails as guardrails_module
        import api.routes.tenants as tenants_module
        import api.routes.users as users_module

        users_module._tenant_resolver = _FakeTenantResolver(
            tenant_ctx if tenant_ctx is not None else _tenant_context()
        )
        tenants_module._svc = _FakeTenantService(known_tenants)

        app = FastAPI()
        app.include_router(guardrails_module.router, prefix="/api/v1")
        headers = {"Authorization": "Bearer fake-token"} if authenticated else {}
        client = TestClient(app, headers=headers)
        patcher = patch(
            "core.security_entra.verify_entra_token", return_value=_claims_for(role)
        )
        patcher.start()
        client._entra_patcher = patcher
        built.append(client)
        return client

    yield _make
    for client in built:
        client._entra_patcher.stop()


# ===========================================================================
# Pinned contract 1 — the required role PER ROUTE (all EIGHT endpoints)
# ===========================================================================

# (method, path, json body, required platform role, the service method that must NOT run when
# the caller is refused). ONE row per endpoint — all 8 — so a route that silently loses its
# gate cannot hide behind a sibling that kept one.
_GATED = [
    ("get", "/api/v1/guardrails/presets", None, "viewer", "get_presets"),
    ("post", "/api/v1/guardrails", _valid_create(), "operator", "create_template"),
    ("get", "/api/v1/guardrails", None, "viewer", "list_templates"),
    ("get", "/api/v1/guardrails/gr-1", None, "viewer", "get_template"),
    ("put", "/api/v1/guardrails/gr-1", _valid_update(), "operator", "update_template"),
    ("delete", "/api/v1/guardrails/gr-1", None, "admin", "delete_template"),
    ("post", "/api/v1/guardrails/gr-1/publish", None, "operator", "publish_version"),
    ("get", "/api/v1/guardrails/gr-1/metrics", None, "viewer", "get_metrics"),
]

# Explicit ids so a failure names the ENDPOINT, never "table row 5".
_GATED_IDS = [f"{method.upper()} {path}" for method, path, _, _, _ in _GATED]

_ONE_BELOW = {"viewer": None, "operator": "viewer", "admin": "operator"}


def _call(client, method, path, body):
    """One request, table-driven. ``httpx``'s ``get``/``delete`` take no ``json=``, so a
    bodyless verb goes through the plain method and a bodied one through ``request``."""
    if body is None:
        return getattr(client, method)(path)
    return client.request(method.upper(), path, json=body)


@pytest.mark.parametrize("method,path,body,required_role,svc_call", _GATED, ids=_GATED_IDS)
def test_route_requires_its_role(
    method, path, body, required_role, svc_call, client_factory, guardrail_svc
):
    """A caller one level BELOW the required role is refused with ``require_role``'s fixed
    literal — and the service is never reached, so no billable Bedrock guardrail is created or
    updated and none is destroyed on the refused path."""
    below = _ONE_BELOW[required_role]
    if below is None:
        pytest.skip("viewer is the floor")
    client = client_factory(role=below)
    r = _call(client, method, path, body)
    assert r.status_code == 403
    assert r.json()["detail"] == _insufficient(required_role)
    getattr(guardrail_svc, svc_call).assert_not_called()


@pytest.mark.parametrize("method,path,body,required_role,svc_call", _GATED, ids=_GATED_IDS)
def test_route_admits_exactly_its_role(
    method, path, body, required_role, svc_call, client_factory, guardrail_svc
):
    """...and the caller who DOES hold it gets through (the gate is a threshold, not a wall)."""
    client = client_factory(role=required_role)
    r = _call(client, method, path, body)
    assert r.status_code < 400, r.text
    getattr(guardrail_svc, svc_call).assert_called_once()


@pytest.mark.parametrize("method,path,body,required_role,svc_call", _GATED, ids=_GATED_IDS)
def test_admin_is_above_every_threshold(
    method, path, body, required_role, svc_call, client_factory, guardrail_svc
):
    """``Role`` is an ``IntEnum`` and ``require_role`` a ``<`` check, so ADMIN clears all three
    thresholds. Pinned per route because DELETE is the ONE endpoint where ADMIN is the
    requirement rather than a courtesy — a copy-paste that leaves it at OPERATOR would still
    pass this row while failing ``test_route_requires_its_role`` above."""
    client = client_factory(role="admin")
    r = _call(client, method, path, body)
    assert r.status_code < 400, r.text
    getattr(guardrail_svc, svc_call).assert_called_once()


@pytest.mark.parametrize("method,path,body,required_role,svc_call", _GATED, ids=_GATED_IDS)
def test_route_requires_authentication(
    method, path, body, required_role, svc_call, client_factory, guardrail_svc
):
    """No bearer token at all → 401 on EVERY endpoint. Today all eight are open to anyone who
    can reach the API, which is what makes this the epic's highest-risk task."""
    client = client_factory(authenticated=False)
    r = _call(client, method, path, body)
    assert r.status_code == 401
    assert r.json()["detail"] == NO_TOKEN
    getattr(guardrail_svc, svc_call).assert_not_called()


def test_the_403_is_id_independent_so_it_leaks_no_existence(client_factory, guardrail_svc):
    """``require_role`` is a DEPENDENCY, so it runs before the visibility gate in the body: an
    under-privileged caller gets the SAME 403 for a real id and for a nonexistent one. That is
    the safe direction — the refusal is a statement about the CALLER, never about whether the
    template exists (which is what the 404 contract below protects)."""
    guardrail_svc.get_template.side_effect = lambda template_id: (
        _template(template_id=template_id) if template_id == "gr-1" else None
    )
    client = client_factory(role="operator")  # one below DELETE's ADMIN
    real = client.delete("/api/v1/guardrails/gr-1")
    missing = client.delete("/api/v1/guardrails/truly-missing")
    assert real.status_code == missing.status_code == 403
    assert real.content == missing.content
    guardrail_svc.delete_template.assert_not_called()


# ===========================================================================
# Pinned contract 3 — E24 tenant scoping: the list is post-filtered
# ===========================================================================

def test_list_filters_to_the_callers_tenant(client_factory, guardrail_svc):
    guardrail_svc.list_templates.return_value = [
        _template(template_id="own", tenant_id="ten-1"),
        _template(template_id="foreign", tenant_id="ten-2"),
    ]
    client = client_factory(role="viewer", tenant_ctx=_tenant_context(tenant_ids=["ten-1"]))
    r = client.get("/api/v1/guardrails")
    assert r.status_code == 200, r.text
    assert [t["template_id"] for t in r.json()] == ["own"]


def test_list_admin_sees_every_tenant(client_factory, guardrail_svc):
    guardrail_svc.list_templates.return_value = [
        _template(template_id="own", tenant_id="ten-1"),
        _template(template_id="foreign", tenant_id="ten-2"),
    ]
    client = client_factory(role="admin", tenant_ctx=_tenant_context(is_global=True))
    r = client.get("/api/v1/guardrails")
    assert r.status_code == 200, r.text
    assert {t["template_id"] for t in r.json()} == {"own", "foreign"}


def test_list_hides_an_untagged_legacy_template_from_a_scoped_caller(
    client_factory, guardrail_svc
):
    """A pre-retrofit record carries no ``tenant_id``, and ``visible()`` treats ``None`` as
    invisible to a non-global caller (tenant_resolver.py:46-52).

    CONTRACT CONFLICT, encoded deliberately: contract 3 says "``tenant_id: str`` on
    ``GuardrailTemplate``". Read literally (no default) EVERY legacy DynamoDB record blows up in
    ``_from_item``'s ``GuardrailTemplate(**item)`` with a ValidationError — a 500 per read. The
    E24 precedent is ``Optional[str] = None`` on the READ model (models/agent.py:209,
    models/mcp_server.py:86) and REQUIRED only on the CREATE model (agent.py:222,
    mcp_server.py:94). This test encodes the precedent, so if the read model's field is made
    mandatory it fails at construction here and the conflict surfaces instead of shipping."""
    guardrail_svc.list_templates.return_value = [
        _template(template_id="own", tenant_id="ten-1"),
        _template(template_id="legacy", tenant_id=None),
    ]
    client = client_factory(role="viewer", tenant_ctx=_tenant_context(tenant_ids=["ten-1"]))
    r = client.get("/api/v1/guardrails")
    assert r.status_code == 200, r.text
    assert [t["template_id"] for t in r.json()] == ["own"]


def test_list_still_passes_the_status_filter_through(client_factory, guardrail_svc):
    """Regression guard on contract 2's reshuffle: the RBAC params are inserted BEFORE the
    ``Query()`` params in the list route, and that is exactly the edit that loses a query
    parameter without any error."""
    client = client_factory(role="viewer")
    r = client.get("/api/v1/guardrails", params={"status": "active"})
    assert r.status_code == 200, r.text
    _, kwargs = guardrail_svc.list_templates.call_args
    assert kwargs.get("status") == GuardrailStatus.ACTIVE


# ===========================================================================
# Pinned contract 3 — the 404-not-403 invariant, byte-identical
# ===========================================================================

def test_foreign_detail_404_is_byte_identical_to_a_missing_template(
    client_factory, guardrail_svc
):
    """A foreign tenant's template must look ABSENT. A 403 — or any different body — CONFIRMS
    that it exists, which is precisely the leak E24 closed on every other registry route."""
    guardrail_svc.get_template.side_effect = lambda template_id: (
        _template(template_id="foreign", tenant_id="ten-2")
        if template_id == "foreign"
        else None
    )
    client = client_factory(role="viewer", tenant_ctx=_tenant_context(tenant_ids=["ten-1"]))
    foreign = client.get("/api/v1/guardrails/foreign")
    missing = client.get("/api/v1/guardrails/truly-missing")

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {"detail": NOT_FOUND}
    # BYTE-identical, not merely equal after parsing.
    assert foreign.content == missing.content


def test_own_tenant_detail_is_visible(client_factory, guardrail_svc):
    """The other half of the contract: the gate must not swallow the caller's OWN templates."""
    guardrail_svc.get_template.return_value = _template(template_id="own", tenant_id="ten-1")
    client = client_factory(role="viewer", tenant_ctx=_tenant_context(tenant_ids=["ten-1"]))
    r = client.get("/api/v1/guardrails/own")
    assert r.status_code == 200, r.text
    assert r.json()["template_id"] == "own"


# (method, path suffix, json body, the role the route requires, the service method that must
# NOT run) for ALL FIVE ``{template_id}`` routes. Each caller HOLDS the required role, so a 403
# cannot be what produces the 404 — only ``_load_visible_guardrail`` can.
_TEMPLATE_ID_ROUTES = [
    # GET detail: the visibility load IS the read, so there is no further call to withhold.
    ("get", "", None, "viewer", None),
    ("put", "", _valid_update(), "operator", "update_template"),
    ("delete", "", None, "admin", "delete_template"),
    ("post", "/publish", None, "operator", "publish_version"),
    ("get", "/metrics", None, "viewer", "get_metrics"),
]
_TEMPLATE_ID_IDS = [
    f"{method.upper()} /{{template_id}}{suffix}"
    for method, suffix, _, _, _ in _TEMPLATE_ID_ROUTES
]


@pytest.mark.parametrize(
    "method,suffix,body,role,svc_call", _TEMPLATE_ID_ROUTES, ids=_TEMPLATE_ID_IDS
)
def test_a_foreign_template_is_404_on_every_template_id_route(
    method, suffix, body, role, svc_call, client_factory, guardrail_svc
):
    """ALL FIVE ``{template_id}`` routes pass through ``_load_visible_guardrail`` BEFORE any
    side effect. This is the row-per-route form of "none was missed": the write routes never
    reach the service, so no Bedrock guardrail is updated, published or destroyed for a tenant
    the caller cannot see, and the metrics route leaks no CloudWatch data about it."""
    guardrail_svc.get_template.return_value = _template(
        template_id="foreign", tenant_id="ten-2"
    )
    client = client_factory(role=role, tenant_ctx=_tenant_context(tenant_ids=["ten-1"]))
    r = _call(client, method, f"/api/v1/guardrails/foreign{suffix}", body)

    assert r.status_code == 404, r.text
    assert r.json() == {"detail": NOT_FOUND}
    if svc_call is not None:
        getattr(guardrail_svc, svc_call).assert_not_called()


def test_the_table_above_covers_every_template_id_route_the_router_declares(entra_settings):
    """Exhaustiveness, mechanically. A hand-written list of five silently stops proving anything
    the moment a sixth ``{template_id}`` endpoint appears (or this retrofit gates only four), so
    compare it against the router's OWN registered routes."""
    import api.routes.guardrails as guardrails_module

    registered = {
        (method.lower(), route.path)
        for route in guardrails_module.router.routes
        for method in route.methods
        if "{template_id}" in route.path and method != "HEAD"
    }
    tabled = {
        (method, f"/guardrails/{{template_id}}{suffix}")
        for method, suffix, _, _, _ in _TEMPLATE_ID_ROUTES
    }
    assert registered == tabled


# ===========================================================================
# Pinned contract 3 — create: 400 unknown tenant / 403 foreign tenant
# ===========================================================================

def test_create_into_an_unknown_tenant_is_400(client_factory, guardrail_svc):
    client = client_factory(
        role="operator",
        tenant_ctx=_tenant_context(tenant_ids=["ten-1"]),
        known_tenants=("ten-1",),
    )
    r = client.post("/api/v1/guardrails", json=_valid_create(tenant_id="ten-unknown"))
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "unknown tenant"
    guardrail_svc.create_template.assert_not_called()


def test_create_into_a_foreign_tenant_is_403(client_factory, guardrail_svc):
    """403, not 404: no resource exists yet to conceal (the agents/mcp precedent)."""
    client = client_factory(
        role="operator",
        tenant_ctx=_tenant_context(tenant_ids=["ten-1"]),
        known_tenants=("ten-1", "ten-2"),
    )
    r = client.post("/api/v1/guardrails", json=_valid_create(tenant_id="ten-2"))
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "tenant not permitted"
    guardrail_svc.create_template.assert_not_called()


def test_create_into_own_tenant_succeeds(client_factory, guardrail_svc):
    client = client_factory(
        role="operator", tenant_ctx=_tenant_context(tenant_ids=["ten-1"])
    )
    r = client.post("/api/v1/guardrails", json=_valid_create(tenant_id="ten-1"))
    assert r.status_code == 201, r.text
    guardrail_svc.create_template.assert_called_once()


def test_create_admin_bypasses_tenant_membership(client_factory, guardrail_svc):
    client = client_factory(role="admin", tenant_ctx=_tenant_context(is_global=True))
    r = client.post("/api/v1/guardrails", json=_valid_create(tenant_id="ten-2"))
    assert r.status_code == 201, r.text
    guardrail_svc.create_template.assert_called_once()


def test_create_without_a_tenant_id_is_422(client_factory, guardrail_svc):
    """``tenant_id`` is REQUIRED on the create model, so pydantic refuses the payload before a
    billable Bedrock guardrail can be provisioned into no tenant at all."""
    client = client_factory(role="operator")
    r = client.post("/api/v1/guardrails", json={"name": "fsi-standard"})
    assert r.status_code == 422, r.text
    guardrail_svc.create_template.assert_not_called()


# ===========================================================================
# E34/T5b — the 502 body for a failed create leaks nothing
# ===========================================================================

# A botocore AccessDeniedException's text, which is what the service used to write into the
# status history and this route used to interpolate into the 502 body verbatim.
AWS_LEAK = (
    "An error occurred (AccessDeniedException) when calling the CreateGuardrail operation: "
    "User: arn:aws:sts::123456789012:assumed-role/agp-ecs-task-execution-role/i-0abc123def4567890"
)


def test_a_failed_create_answers_a_fixed_502_literal(client_factory, guardrail_svc):
    """The route must not interpolate ``status_history[-1].message`` into the detail.

    The service now writes fixed literals, but the route is a SECOND layer on purpose: records
    written before T5b are still in the store carrying the raw AWS text, and a status message is
    service-supplied data either way. So the mocked template deliberately carries the leak — the
    route is what has to refuse to forward it.
    """
    failed = _template(
        template_id="gr-new",
        status=GuardrailStatus.FAILED,
        status_history=[
            {"status": "creating", "timestamp": FIXED_TS, "message": "Creating Bedrock guardrail"},
            {"status": "failed", "timestamp": FIXED_TS, "message": f"Bedrock API error: {AWS_LEAK}"},
        ],
    )
    guardrail_svc.create_template.return_value = failed

    client = client_factory(role="operator")
    r = client.post("/api/v1/guardrails", json=_valid_create())

    assert r.status_code == 502, r.text
    assert r.json() == {"detail": "Guardrail creation failed"}
    for leak in ("123456789012", "agp-ecs-task-execution-role", "i-0abc123def4567890"):
        assert leak not in r.text


def test_a_failed_create_with_no_status_history_is_the_same_502_literal(
    client_factory, guardrail_svc
):
    """The old detail had an ``if template.status_history else 'Unknown error'`` branch, so the
    body varied with the record's shape. One literal now covers both — pinned so the branch is
    not reintroduced as "just a fallback"."""
    guardrail_svc.create_template.return_value = _template(
        template_id="gr-new", status=GuardrailStatus.FAILED, status_history=[]
    )

    client = client_factory(role="operator")
    r = client.post("/api/v1/guardrails", json=_valid_create())

    assert r.status_code == 502, r.text
    assert r.json() == {"detail": "Guardrail creation failed"}


# ===========================================================================
# Pinned contract 4 — attribution comes from the principal
# ===========================================================================

def test_created_by_is_the_principals_email(client_factory, guardrail_svc):
    """``created_by=principal.email or principal.oid`` (the ``agents.py:321`` idiom), never the
    literal "user" that every guardrail record written so far carries."""
    client = client_factory(role="operator")
    r = client.post("/api/v1/guardrails", json=_valid_create())
    assert r.status_code == 201, r.text
    _, kwargs = guardrail_svc.create_template.call_args
    assert kwargs.get("created_by") == "operator.user@example.com"


def test_created_by_falls_back_to_the_oid_when_the_token_carries_no_email(
    client_factory, guardrail_svc
):
    """The ``or`` half of the idiom: a token with no preferred_username / email / upn (a service
    principal, say) attributes to the oid rather than to ``None``. Patching
    ``verify_entra_token`` again simply shadows the client's own patch for this block."""
    client = client_factory(role="operator")
    with patch(
        "core.security_entra.verify_entra_token",
        return_value={"oid": "sp-oid", "roles": ["Platform.Operator"]},
    ):
        r = client.post("/api/v1/guardrails", json=_valid_create())
    assert r.status_code == 201, r.text
    _, kwargs = guardrail_svc.create_template.call_args
    assert kwargs.get("created_by") == "sp-oid"


def test_the_literal_user_attribution_is_gone_from_the_route_module(entra_settings):
    """The defect is a hardcoded string, so pin its ABSENCE at the source level. A response-body
    check cannot do this job: a real principal's email legitimately contains "user" (e.g.
    ``operator.user@example.com``), so any substring assertion on the payload is meaningless."""
    import inspect

    import api.routes.guardrails as guardrails_module

    source = inspect.getsource(guardrails_module)
    assert 'created_by="user"' not in source
    assert "created_by='user'" not in source


# ===========================================================================
# Pinned contract 6 — route ordering: /presets before /{template_id}
# ===========================================================================

def test_presets_route_still_wins_over_the_template_id_route(client_factory, guardrail_svc):
    """``/presets`` is declared BEFORE ``/{template_id}``, so it must resolve as the presets
    route. Reorder the two and "presets" becomes a template id: the endpoint returns the 404
    literal instead, with no error anywhere to notice."""
    client = client_factory(role="viewer")
    r = client.get("/api/v1/guardrails/presets")
    assert r.status_code == 200, r.text
    assert [p["id"] for p in r.json()] == [
        "fsi-standard",
        "market-surveillance",
        "customer-service",
    ]
    guardrail_svc.get_presets.assert_called_once()
    # The clincher: the ``{template_id}`` handler never ran, so "presets" was never read as an id.
    guardrail_svc.get_template.assert_not_called()


def test_presets_is_declared_before_the_template_id_route(entra_settings):
    """The same property one level down, so the ordering stays pinned even if a future
    ``{template_id}`` handler starts answering 200 for an unknown id."""
    import api.routes.guardrails as guardrails_module

    paths = [route.path for route in guardrails_module.router.routes]
    assert paths.index("/guardrails/presets") < paths.index("/guardrails/{template_id}")


# ===========================================================================
# Pinned contract 5 — testability: empty table name ⇒ in-memory, no boto3 client
# ===========================================================================

def test_an_empty_table_name_builds_no_boto3_client():
    """THE reason this vertical had no tests. Today ``__init__`` builds a DynamoDB resource, a
    Table handle, a Bedrock client and a CloudWatch client unconditionally
    (``guardrail_service.py:127-133``), so merely constructing the service reaches for AWS
    credentials/IMDS — which conftest's outbound-network guard refuses (169.254.169.254 is not
    loopback). With an empty table name no client may be built at all, matching the
    ``TenantService`` (:79-86) / ``ProjectRoleService`` convention."""
    from services.guardrail_service import GuardrailService

    with patch("services.guardrail_service.boto3.resource") as resource, patch(
        "services.guardrail_service.boto3.client"
    ) as client:
        svc = GuardrailService(table_name="", region="us-east-1")

    resource.assert_not_called()
    client.assert_not_called()
    assert svc.table_name == ""


def test_the_in_memory_store_round_trips_and_keeps_the_tenant_id():
    """Not merely "no client built" — the fallback has to WORK, and it has to carry the new
    tenant dimension through ``_to_item`` / ``_from_item``. The Bedrock call is stubbed at the
    existing private seam (``_create_bedrock_guardrail``) so the STORE is what is under test and
    nothing reaches AWS."""
    from models.guardrail import GuardrailTemplateCreate
    from services.guardrail_service import GuardrailService

    with patch("services.guardrail_service.boto3.resource") as resource, patch(
        "services.guardrail_service.boto3.client"
    ) as client:
        svc = GuardrailService(table_name="")
        with patch.object(
            svc,
            "_create_bedrock_guardrail",
            return_value={
                "guardrailId": "gr-bedrock-abc",
                "guardrailArn": (
                    "arn:aws:bedrock:us-east-1:111122223333:guardrail/gr-bedrock-abc"
                ),
                "version": "DRAFT",
            },
        ):
            created = svc.create_template(
                GuardrailTemplateCreate(name="fsi-standard", tenant_id="ten-1"),
                created_by="maria.bauer@example.com",
            )
        fetched = svc.get_template(created.template_id)
        listed = svc.list_templates()

    resource.assert_not_called()
    client.assert_not_called()
    assert fetched is not None
    assert fetched.tenant_id == "ten-1"
    assert fetched.created_by == "maria.bauer@example.com"
    assert [t.template_id for t in listed] == [created.template_id]
