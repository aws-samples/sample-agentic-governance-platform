"""RBAC and tenant scoping of the guardrails routes.

The fixture idiom: the module reset, the entra env fixture, a ``guardrails_client_factory`` that
starts a ``verify_entra_token`` patch for the client's whole life, and no ``dependency_overrides``,
so the real ``require_role`` / ``current_principal`` / ``get_tenant_ctx`` chain runs. The
``GuardrailService`` is a ``MagicMock`` on the module global, so no DynamoDB, Bedrock or
CloudWatch client is ever built.

What this pins: the required role per route for all eight endpoints, a 403 one level below with
``require_role``'s literal and no service call; 401 without a bearer on every endpoint; the
tenant contract (the list is post-filtered, a foreign or missing ``{template_id}`` is the same
404 body on all five such routes, checked against the router's own registered routes); create
into an unknown tenant renders ``TenantError``'s table, into a foreign tenant 403; attribution
from the principal; ``/presets`` resolving ahead of ``/{template_id}``; an empty table name
building no boto3 client; and the fixed 502 literal for a failed create.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from models.guardrail import GuardrailMetrics, GuardrailStatus, GuardrailTemplate

pytestmark = pytest.mark.usefixtures("reset_modules")

FIXED_TS = "2026-08-11T00:00:00+00:00"

NOT_FOUND = {"detail": "Guardrail template not found", "code": "guardrail.not_found"}
CREATION_FAILED = {"detail": "Guardrail creation failed", "code": "guardrail.creation_failed"}
NO_TOKEN = "Missing authorization token"


def _insufficient(role: str) -> str:
    return f"Requires {role} role or higher"


def _template(template_id="gr-1", tenant_id: str | None = "ten-1", **overrides):
    """A real ``GuardrailTemplate`` with a ``guardrail_id``, so the metrics route reaches
    ``get_metrics`` instead of its 400 branch."""
    base: dict[str, Any] = dict(
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
    return {
        "name": "fsi-standard",
        "description": "Standard FSI guardrail",
        "tenant_id": tenant_id,
    }


def _valid_update():
    return {"description": "tightened"}


def _tenant_context(*, is_global=False, tenant_ids=("ten-1",)):
    from services.tenant_resolver import TenantContext

    return TenantContext(is_global=is_global, tenant_ids=frozenset(tenant_ids), tenants=())


class _FakeTenantResolver:
    def __init__(self, ctx):
        self._ctx = ctx

    async def resolve(self, principal):
        return self._ctx


class _FakeTenantService:
    """Only ``.get`` is exercised, by create."""

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


@pytest.fixture
def guardrail_svc():
    """The ``GuardrailService`` MagicMock on the guardrails module global, defaulting to the happy
    path for all eight endpoints so each test observes only its gate."""
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
def guardrails_client_factory(entra_settings, guardrail_svc):
    """A TestClient for a caller with a platform role and a tenant scope: seeds the tenant-resolver
    singleton ``users.py`` owns and the tenant-service singleton the create route reads, builds an
    app with the guardrails router and the ``AppError`` handler, and patches ``verify_entra_token``
    for the client's life. ``authenticated=False`` drops the Authorization header."""
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
        from core.errors import install_error_handlers

        users_module._tenant_resolver = _FakeTenantResolver(
            tenant_ctx if tenant_ctx is not None else _tenant_context()
        )
        tenants_module._svc = _FakeTenantService(known_tenants)

        app = FastAPI()
        install_error_handlers(app)
        app.include_router(guardrails_module.router, prefix="/api/v1")
        headers = {"Authorization": "Bearer fake-token"} if authenticated else {}
        client = TestClient(app, headers=headers)
        patcher = patch("core.security_entra.verify_entra_token", return_value=_claims_for(role))
        patcher.start()
        client._entra_patcher = patcher
        built.append(client)
        return client

    yield _make
    for client in built:
        client._entra_patcher.stop()


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
_GATED_IDS = [f"{method.upper()} {path}" for method, path, _, _, _ in _GATED]

_ONE_BELOW = {"viewer": None, "operator": "viewer", "admin": "operator"}


def _call(client, method, path, body):
    if body is None:
        return getattr(client, method)(path)
    return client.request(method.upper(), path, json=body)


@pytest.mark.parametrize("method,path,body,required_role,svc_call", _GATED, ids=_GATED_IDS)
def test_route_requires_its_role(
    method, path, body, required_role, svc_call, guardrails_client_factory, guardrail_svc
):
    """One level below the required role is refused with ``require_role``'s literal before the
    service is reached, so no Bedrock guardrail is created, updated or destroyed."""
    below = _ONE_BELOW[required_role]
    if below is None:
        pytest.skip("viewer is the floor")
    client = guardrails_client_factory(role=below)
    r = _call(client, method, path, body)
    assert r.status_code == 403
    assert r.json() == {"detail": _insufficient(required_role), "code": "auth.role_required"}
    getattr(guardrail_svc, svc_call).assert_not_called()


@pytest.mark.parametrize("method,path,body,required_role,svc_call", _GATED, ids=_GATED_IDS)
def test_route_admits_exactly_its_role(
    method, path, body, required_role, svc_call, guardrails_client_factory, guardrail_svc
):
    client = guardrails_client_factory(role=required_role)
    r = _call(client, method, path, body)
    assert r.status_code < 400, r.text
    getattr(guardrail_svc, svc_call).assert_called_once()


@pytest.mark.parametrize("method,path,body,required_role,svc_call", _GATED, ids=_GATED_IDS)
def test_admin_is_above_every_threshold(
    method, path, body, required_role, svc_call, guardrails_client_factory, guardrail_svc
):
    client = guardrails_client_factory(role="admin")
    r = _call(client, method, path, body)
    assert r.status_code < 400, r.text
    getattr(guardrail_svc, svc_call).assert_called_once()


@pytest.mark.parametrize("method,path,body,required_role,svc_call", _GATED, ids=_GATED_IDS)
def test_route_requires_authentication(
    method, path, body, required_role, svc_call, guardrails_client_factory, guardrail_svc
):
    client = guardrails_client_factory(authenticated=False)
    r = _call(client, method, path, body)
    assert r.status_code == 401
    assert r.json()["detail"] == NO_TOKEN
    getattr(guardrail_svc, svc_call).assert_not_called()


def test_the_403_is_id_independent_so_it_leaks_no_existence(guardrails_client_factory, guardrail_svc):
    """``require_role`` runs before the visibility gate, so an under-privileged caller gets the
    same 403 for a real id and a nonexistent one."""
    guardrail_svc.get_template.side_effect = lambda template_id: (
        _template(template_id=template_id) if template_id == "gr-1" else None
    )
    client = guardrails_client_factory(role="operator")
    real = client.delete("/api/v1/guardrails/gr-1")
    missing = client.delete("/api/v1/guardrails/truly-missing")
    assert real.status_code == missing.status_code == 403
    assert real.content == missing.content
    guardrail_svc.delete_template.assert_not_called()


def test_list_filters_to_the_callers_tenant(guardrails_client_factory, guardrail_svc):
    guardrail_svc.list_templates.return_value = [
        _template(template_id="own", tenant_id="ten-1"),
        _template(template_id="foreign", tenant_id="ten-2"),
    ]
    client = guardrails_client_factory(role="viewer", tenant_ctx=_tenant_context(tenant_ids=["ten-1"]))
    r = client.get("/api/v1/guardrails")
    assert r.status_code == 200, r.text
    assert [t["template_id"] for t in r.json()] == ["own"]


def test_list_admin_sees_every_tenant(guardrails_client_factory, guardrail_svc):
    guardrail_svc.list_templates.return_value = [
        _template(template_id="own", tenant_id="ten-1"),
        _template(template_id="foreign", tenant_id="ten-2"),
    ]
    client = guardrails_client_factory(role="admin", tenant_ctx=_tenant_context(is_global=True))
    r = client.get("/api/v1/guardrails")
    assert r.status_code == 200, r.text
    assert {t["template_id"] for t in r.json()} == {"own", "foreign"}


def test_list_hides_an_untagged_legacy_template_from_a_scoped_caller(
    guardrails_client_factory, guardrail_svc
):
    """A record with no ``tenant_id`` is invisible to a non-global caller. The read model keeps
    ``tenant_id`` optional so legacy records still deserialize; only the create model requires it."""
    guardrail_svc.list_templates.return_value = [
        _template(template_id="own", tenant_id="ten-1"),
        _template(template_id="legacy", tenant_id=None),
    ]
    client = guardrails_client_factory(role="viewer", tenant_ctx=_tenant_context(tenant_ids=["ten-1"]))
    r = client.get("/api/v1/guardrails")
    assert r.status_code == 200, r.text
    assert [t["template_id"] for t in r.json()] == ["own"]


def test_list_still_passes_the_status_filter_through(guardrails_client_factory, guardrail_svc):
    client = guardrails_client_factory(role="viewer")
    r = client.get("/api/v1/guardrails", params={"status": "active"})
    assert r.status_code == 200, r.text
    _, kwargs = guardrail_svc.list_templates.call_args
    assert kwargs.get("status") == GuardrailStatus.ACTIVE


def test_foreign_detail_404_is_byte_identical_to_a_missing_template(guardrails_client_factory, guardrail_svc):
    """A foreign tenant's template looks absent: any different body would confirm it exists."""
    guardrail_svc.get_template.side_effect = lambda template_id: (
        _template(template_id="foreign", tenant_id="ten-2") if template_id == "foreign" else None
    )
    client = guardrails_client_factory(role="viewer", tenant_ctx=_tenant_context(tenant_ids=["ten-1"]))
    foreign = client.get("/api/v1/guardrails/foreign")
    missing = client.get("/api/v1/guardrails/truly-missing")

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == NOT_FOUND
    assert foreign.content == missing.content


def test_own_tenant_detail_is_visible(guardrails_client_factory, guardrail_svc):
    guardrail_svc.get_template.return_value = _template(template_id="own", tenant_id="ten-1")
    client = guardrails_client_factory(role="viewer", tenant_ctx=_tenant_context(tenant_ids=["ten-1"]))
    r = client.get("/api/v1/guardrails/own")
    assert r.status_code == 200, r.text
    assert r.json()["template_id"] == "own"


_TEMPLATE_ID_ROUTES = [
    ("get", "", None, "viewer", None),
    ("put", "", _valid_update(), "operator", "update_template"),
    ("delete", "", None, "admin", "delete_template"),
    ("post", "/publish", None, "operator", "publish_version"),
    ("get", "/metrics", None, "viewer", "get_metrics"),
]
_TEMPLATE_ID_IDS = [
    f"{method.upper()} /{{template_id}}{suffix}" for method, suffix, _, _, _ in _TEMPLATE_ID_ROUTES
]


@pytest.mark.parametrize("method,suffix,body,role,svc_call", _TEMPLATE_ID_ROUTES, ids=_TEMPLATE_ID_IDS)
def test_a_foreign_template_is_404_on_every_template_id_route(
    method, suffix, body, role, svc_call, guardrails_client_factory, guardrail_svc
):
    """Every ``{template_id}`` route passes through ``_load_visible_guardrail`` before any side
    effect; each caller holds the required role, so only the visibility gate can produce the 404."""
    guardrail_svc.get_template.return_value = _template(template_id="foreign", tenant_id="ten-2")
    client = guardrails_client_factory(role=role, tenant_ctx=_tenant_context(tenant_ids=["ten-1"]))
    r = _call(client, method, f"/api/v1/guardrails/foreign{suffix}", body)

    assert r.status_code == 404, r.text
    assert r.json() == NOT_FOUND
    if svc_call is not None:
        getattr(guardrail_svc, svc_call).assert_not_called()


@pytest.mark.parametrize(
    "method,suffix,body,role,svc_call",
    [
        ("put", "", _valid_update(), "operator", "update_template"),
        ("delete", "", None, "admin", "delete_template"),
    ],
    ids=["PUT /{template_id}", "DELETE /{template_id}"],
)
def test_a_template_gone_between_the_gate_and_the_write_is_the_same_404(
    method, suffix, body, role, svc_call, guardrails_client_factory, guardrail_svc
):
    getattr(guardrail_svc, svc_call).return_value = None
    client = guardrails_client_factory(role=role)
    r = _call(client, method, f"/api/v1/guardrails/gr-1{suffix}", body)

    assert r.status_code == 404, r.text
    assert r.json() == NOT_FOUND
    getattr(guardrail_svc, svc_call).assert_called_once()


def test_publishing_a_template_with_no_bedrock_resource_is_its_own_404(
    guardrails_client_factory, guardrail_svc
):
    guardrail_svc.publish_version.return_value = None
    client = guardrails_client_factory(role="operator")
    r = client.post("/api/v1/guardrails/gr-1/publish")

    assert r.status_code == 404, r.text
    assert r.json() == {
        "detail": "Guardrail template not found or has no Bedrock resource",
        "code": "guardrail.not_publishable",
    }
    guardrail_svc.publish_version.assert_called_once()


def test_metrics_of_a_draft_with_no_bedrock_resource_is_400(guardrails_client_factory, guardrail_svc):
    guardrail_svc.get_template.return_value = _template(guardrail_id=None)
    client = guardrails_client_factory(role="viewer")
    r = client.get("/api/v1/guardrails/gr-1/metrics")

    assert r.status_code == 400, r.text
    assert r.json() == {
        "detail": "Guardrail has no Bedrock resource (still in draft)",
        "code": "guardrail.no_bedrock_resource",
    }
    guardrail_svc.get_metrics.assert_not_called()


def test_the_table_above_covers_every_template_id_route_the_router_declares(entra_settings):
    import api.routes.guardrails as guardrails_module

    registered: set[tuple[str, str]] = set()
    for route in guardrails_module.router.routes:
        assert isinstance(route, APIRoute)
        for method in route.methods or ():
            if "{template_id}" in route.path and method != "HEAD":
                registered.add((method.lower(), route.path))
    tabled = {
        (method, f"/guardrails/{{template_id}}{suffix}") for method, suffix, _, _, _ in _TEMPLATE_ID_ROUTES
    }
    assert registered == tabled


def test_guardrails_not_found_is_404(guardrails_client_factory, guardrail_svc):
    """Create into an unknown tenant renders ``TenantError``'s ``not_found`` row, the tenant
    domain's wire; before the error layer this route answered 400 "unknown tenant"."""
    client = guardrails_client_factory(
        role="operator",
        tenant_ctx=_tenant_context(tenant_ids=["ten-1"]),
        known_tenants=("ten-1",),
    )
    r = client.post("/api/v1/guardrails", json=_valid_create(tenant_id="ten-unknown"))
    assert r.status_code == 404, r.text
    assert r.json() == {"detail": "Tenant not found", "code": "tenant.not_found"}
    guardrail_svc.create_template.assert_not_called()


def test_create_into_a_foreign_tenant_is_403(guardrails_client_factory, guardrail_svc):
    """403, not 404: no resource exists yet to conceal."""
    client = guardrails_client_factory(
        role="operator",
        tenant_ctx=_tenant_context(tenant_ids=["ten-1"]),
        known_tenants=("ten-1", "ten-2"),
    )
    r = client.post("/api/v1/guardrails", json=_valid_create(tenant_id="ten-2"))
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": "tenant not permitted", "code": "tenant.not_permitted"}
    guardrail_svc.create_template.assert_not_called()


def test_create_into_own_tenant_succeeds(guardrails_client_factory, guardrail_svc):
    client = guardrails_client_factory(role="operator", tenant_ctx=_tenant_context(tenant_ids=["ten-1"]))
    r = client.post("/api/v1/guardrails", json=_valid_create(tenant_id="ten-1"))
    assert r.status_code == 201, r.text
    guardrail_svc.create_template.assert_called_once()


def test_create_admin_bypasses_tenant_membership(guardrails_client_factory, guardrail_svc):
    client = guardrails_client_factory(role="admin", tenant_ctx=_tenant_context(is_global=True))
    r = client.post("/api/v1/guardrails", json=_valid_create(tenant_id="ten-2"))
    assert r.status_code == 201, r.text
    guardrail_svc.create_template.assert_called_once()


def test_create_without_a_tenant_id_is_422(guardrails_client_factory, guardrail_svc):
    client = guardrails_client_factory(role="operator")
    r = client.post("/api/v1/guardrails", json={"name": "fsi-standard"})
    assert r.status_code == 422, r.text
    guardrail_svc.create_template.assert_not_called()


AWS_LEAK = (
    "An error occurred (AccessDeniedException) when calling the CreateGuardrail operation: "
    "User: arn:aws:sts::123456789012:assumed-role/agp-ecs-task-execution-role/i-0abc123def4567890"
)


def test_a_failed_create_answers_a_fixed_502_literal(guardrails_client_factory, guardrail_svc):
    """The route never interpolates ``status_history[-1].message`` into the detail: records written
    before the service wrote fixed literals still carry raw AWS text, so the mocked template
    carries the leak and the route has to refuse to forward it."""
    failed = _template(
        template_id="gr-new",
        status=GuardrailStatus.FAILED,
        status_history=[
            {"status": "creating", "timestamp": FIXED_TS, "message": "Creating Bedrock guardrail"},
            {"status": "failed", "timestamp": FIXED_TS, "message": f"Bedrock API error: {AWS_LEAK}"},
        ],
    )
    guardrail_svc.create_template.return_value = failed

    client = guardrails_client_factory(role="operator")
    r = client.post("/api/v1/guardrails", json=_valid_create())

    assert r.status_code == 502, r.text
    assert r.json() == CREATION_FAILED
    for leak in ("123456789012", "agp-ecs-task-execution-role", "i-0abc123def4567890"):
        assert leak not in r.text


def test_a_failed_create_with_no_status_history_is_the_same_502_literal(
    guardrails_client_factory, guardrail_svc
):
    guardrail_svc.create_template.return_value = _template(
        template_id="gr-new", status=GuardrailStatus.FAILED, status_history=[]
    )

    client = guardrails_client_factory(role="operator")
    r = client.post("/api/v1/guardrails", json=_valid_create())

    assert r.status_code == 502, r.text
    assert r.json() == CREATION_FAILED


def test_created_by_is_the_principals_email(guardrails_client_factory, guardrail_svc):
    client = guardrails_client_factory(role="operator")
    r = client.post("/api/v1/guardrails", json=_valid_create())
    assert r.status_code == 201, r.text
    _, kwargs = guardrail_svc.create_template.call_args
    assert kwargs.get("created_by") == "operator.user@example.com"


def test_created_by_falls_back_to_the_oid_when_the_token_carries_no_email(
    guardrails_client_factory, guardrail_svc
):
    """A token with no email claim (a service principal, say) attributes to the oid."""
    client = guardrails_client_factory(role="operator")
    with patch(
        "core.security_entra.verify_entra_token",
        return_value={"oid": "sp-oid", "roles": ["Platform.Operator"]},
    ):
        r = client.post("/api/v1/guardrails", json=_valid_create())
    assert r.status_code == 201, r.text
    _, kwargs = guardrail_svc.create_template.call_args
    assert kwargs.get("created_by") == "sp-oid"


def test_the_literal_user_attribution_is_gone_from_the_route_module(entra_settings):
    """A response-body check cannot do this job, since a real email legitimately contains
    "user"; the absence is pinned at the source."""
    import inspect

    import api.routes.guardrails as guardrails_module

    source = inspect.getsource(guardrails_module)
    assert 'created_by="user"' not in source
    assert "created_by='user'" not in source


def test_presets_route_still_wins_over_the_template_id_route(guardrails_client_factory, guardrail_svc):
    """``/presets`` is declared before ``/{template_id}``; reordered, "presets" becomes a template
    id and the endpoint answers the 404 literal with no error anywhere."""
    client = guardrails_client_factory(role="viewer")
    r = client.get("/api/v1/guardrails/presets")
    assert r.status_code == 200, r.text
    assert [p["id"] for p in r.json()] == [
        "fsi-standard",
        "market-surveillance",
        "customer-service",
    ]
    guardrail_svc.get_presets.assert_called_once()
    guardrail_svc.get_template.assert_not_called()


def test_presets_is_declared_before_the_template_id_route(entra_settings):
    import api.routes.guardrails as guardrails_module

    paths = [route.path for route in guardrails_module.router.routes if isinstance(route, APIRoute)]
    assert paths.index("/guardrails/presets") < paths.index("/guardrails/{template_id}")


def test_an_empty_table_name_builds_no_boto3_client():
    """An empty table name is the in-memory mode the suite runs on; no client may be built, or
    constructing the service reaches for AWS credentials."""
    from services.guardrail_service import GuardrailService

    with (
        patch("core.aws_clients.dynamodb_table") as table,
        patch("core.aws_clients.client") as client,
    ):
        svc = GuardrailService(table_name="", region="us-east-1")

    table.assert_not_called()
    client.assert_not_called()
    assert svc.table_name == ""
    assert svc._store.in_memory


def test_the_in_memory_store_round_trips_and_keeps_the_tenant_id():
    """The in-memory store works and carries ``tenant_id`` through ``_to_item`` / ``_from_item``;
    the Bedrock call is stubbed at the service's private seam so nothing reaches AWS."""
    from models.guardrail import GuardrailTemplateCreate
    from services.guardrail_service import GuardrailService

    with (
        patch("core.aws_clients.dynamodb_table") as table,
        patch("core.aws_clients.client") as client,
    ):
        svc = GuardrailService(table_name="")
        with patch.object(
            svc,
            "_create_bedrock_guardrail",
            return_value={
                "guardrailId": "gr-bedrock-abc",
                "guardrailArn": ("arn:aws:bedrock:us-east-1:111122223333:guardrail/gr-bedrock-abc"),
                "version": "DRAFT",
            },
        ):
            created = svc.create_template(
                GuardrailTemplateCreate(name="fsi-standard", tenant_id="ten-1"),
                created_by="maria.bauer@example.com",
            )
        fetched = svc.get_template(created.template_id)
        listed = svc.list_templates()

    table.assert_not_called()
    client.assert_not_called()
    assert fetched is not None
    assert fetched.tenant_id == "ten-1"
    assert fetched.created_by == "maria.bauer@example.com"
    assert [t.template_id for t in listed] == [created.template_id]
