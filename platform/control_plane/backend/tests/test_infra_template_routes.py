"""Infra-template catalog API surface.

Three things under test:

1. ``GET /projects/templates/infra?connection_id=…`` → ``List[InfraTemplateView]``, with RBAC
   PARITY to the agent-template list route (a read: OPERATOR passes, VIEWER is refused) and the
   table's status and fixed-detail discipline (422 for malformed input, 503 for a store fault, and
   a raw store message never in the body).
2. The enriched ``TemplateView``: ``platform`` + ``build_kind`` JOINED from the infra record,
   falling back to ``"aws"``/``"container"`` when the id dangles, plus the record-owned
   ``infra_template_id`` + ``preview`` riding through.
3. ``tenant_platform()``: ``"aws"`` for a platform-less tenant, the tenant's own value otherwise.

Same harness idiom as ``test_github_templates_routes.py``: the real
``require_role``/``current_principal`` path against a mocked ``verify_entra_token`` (no live
Entra), with FAKES patched onto the router module's ``_svc`` / ``_infra_registry`` singletons
(no live GitHub / AWS).
"""

from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.github_template_service import TemplateView
from services.infra_template_registry import (
    InfraTemplateRecord,
    InfraTemplateRegistryError,
    InfraTemplateRegistryValidationError,
)

pytestmark = pytest.mark.usefixtures("reset_modules")


def _build_client(fake_svc=None, fake_infra=None):
    """Mount BOTH routers the module owns and patch both lazy singletons."""
    import api.routes.github_templates as gh_module
    from core.errors import install_error_handlers

    gh_module._svc = fake_svc if fake_svc is not None else MagicMock()
    gh_module._infra_registry = fake_infra if fake_infra is not None else MagicMock()

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(gh_module.router, prefix="/api/v1")
    app.include_router(gh_module.infra_templates_router, prefix="/api/v1")
    return TestClient(app)


def _claims_for(role: str):
    role_app = {
        "viewer": "Platform.Viewer",
        "operator": "Platform.Operator",
        "admin": "Platform.Admin",
    }[role]
    return {"oid": f"{role}-oid", "preferred_username": f"{role}@x.com", "roles": [role_app]}


def _headers():
    return {"Authorization": "Bearer fake-token"}


def _infra_record(
    id="agentcore-runtime",
    platform="aws",
    build_kind="container",
    iac_type="agentcore_runtime",
    repo_name="agp-runtime-infra",
):
    return InfraTemplateRecord(
        id=id,
        name=id.replace("-", " ").title(),
        description="An infra template",
        platform=platform,
        iac_type=iac_type,
        build_kind=build_kind,
        repo_name=repo_name,
        module_source=f"{id}/module.zip",
        version="1",
        connection_id="conn1",
        created_at="2026-08-06T00:00:00+00:00",
        updated_at="2026-08-06T01:00:00+00:00",
    )


def _template_view(name="strands-agentcore", **overrides):
    """A view as the SERVICE returns it — the record-owned fields are already set, the
    cross-registry ``platform``/``build_kind`` are still at their defaults."""
    fields: dict[str, Any] = dict(
        name=name,
        description="A scaffold",
        framework="strands",
        aws_services=["lambda"],
        tags=["fsi"],
        html_url=f"https://github.com/acme/{name}",
        updated_at="2026-07-08T00:00:00+00:00",
        infra_template_id="agentcore-runtime",
        preview=False,
    )
    fields.update(overrides)
    return TemplateView(**fields)


def test_infra_list_operator_ok(entra_settings):
    """OPERATOR reads, exactly like ``GET /github-templates``, and the pinned view fields
    come back."""
    infra = MagicMock()
    infra.list.return_value = [
        _infra_record(),
        _infra_record(
            id="agentcore-harness",
            build_kind="config",
            iac_type="agentcore_harness",
            repo_name="agp-harness-infra",
        ),
    ]
    client = _build_client(fake_infra=infra)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/projects/templates/infra?connection_id=conn1", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert [v["id"] for v in body] == ["agentcore-runtime", "agentcore-harness"]
    assert body[0] == {
        "id": "agentcore-runtime",
        "name": "Agentcore Runtime",
        "description": "An infra template",
        "platform": "aws",
        "iac_type": "agentcore_runtime",
        "build_kind": "container",
        "repo_name": "agp-runtime-infra",
        "version": "1",
        "updated_at": "2026-08-06T01:00:00+00:00",
    }
    assert body[1]["build_kind"] == "config"
    infra.list.assert_called_once_with("conn1")


def test_infra_list_viewer_forbidden(entra_settings):
    """RBAC parity is a PARITY claim: a VIEWER is refused here for the same reason the
    agent-template list refuses one, and the store is never read."""
    infra = MagicMock()
    client = _build_client(fake_infra=infra)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/projects/templates/infra?connection_id=conn1", headers=_headers())
    assert resp.status_code == 403
    infra.list.assert_not_called()


def test_infra_list_admin_ok(entra_settings):
    """An ADMIN outranks OPERATOR — the read must not be operator-ONLY."""
    infra = MagicMock()
    infra.list.return_value = []
    client = _build_client(fake_infra=infra)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/projects/templates/infra?connection_id=conn1", headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == []


def test_infra_list_requires_connection_id(entra_settings):
    """``connection_id`` is REQUIRED — a catalog is per-connection, so a missing one must be a
    422, never a cross-connection read."""
    infra = MagicMock()
    client = _build_client(fake_infra=infra)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/projects/templates/infra", headers=_headers())
    assert resp.status_code == 422
    infra.list.assert_not_called()


def test_infra_list_validation_error_is_422(entra_settings):
    """Malformed input is PERMANENT: a ``#``-bearing connection id can never succeed, so the
    console must not be told to retry it. The VALIDATION subclass has to be caught before its
    store-fault parent for this to hold."""
    infra = MagicMock()
    infra.list.side_effect = InfraTemplateRegistryValidationError("connection_id must not contain '#'")
    client = _build_client(fake_infra=infra)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/projects/templates/infra?connection_id=a%23b", headers=_headers())
    assert resp.status_code == 422
    assert resp.status_code != 503


def test_infra_list_store_error_is_503_and_never_leaks(entra_settings):
    """A store fault is TRANSIENT (503, so the console retries instead of rendering an empty
    catalog) and its message is FIXED — a raw store body never reaches the client."""
    infra = MagicMock()
    infra.list.side_effect = InfraTemplateRegistryError("Table agp-projects-xyz throughput exceeded")
    client = _build_client(fake_infra=infra)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/projects/templates/infra?connection_id=conn1", headers=_headers())
    assert resp.status_code == 503
    assert "agp-projects-xyz" not in resp.text
    assert "throughput" not in resp.text


def test_infra_view_never_exposes_the_module_source(entra_settings):
    """``module_source`` is an internal S3 key and is NOT in the pinned view — the catalog
    surface must not hand the console a storage path it has no use for."""
    infra = MagicMock()
    infra.list.return_value = [_infra_record()]
    client = _build_client(fake_infra=infra)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/projects/templates/infra?connection_id=conn1", headers=_headers())
    assert resp.status_code == 200
    assert "module_source" not in resp.json()[0]
    assert "module.zip" not in resp.text


def test_template_list_joins_platform_and_build_kind(entra_settings):
    """The compatibility fact the picker filters on is a DERIVED JOIN, not a stored duplicate:
    the agent template's ``infra_template_id`` resolves to an infra record and the view carries
    that record's ``platform`` + ``build_kind``."""
    svc = MagicMock()
    svc.list_templates.return_value = [
        _template_view(name="strands-agentcore", infra_template_id="agentcore-runtime"),
        _template_view(name="harness-agentcore", infra_template_id="agentcore-harness"),
    ]
    infra = MagicMock()
    infra.list.return_value = [
        _infra_record(),
        _infra_record(
            id="agentcore-harness",
            platform="databricks",
            build_kind="config",
            iac_type="agentcore_harness",
        ),
    ]
    client = _build_client(fake_svc=svc, fake_infra=infra)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/github-templates?connection_id=conn1", headers=_headers())
    assert resp.status_code == 200
    by_name = {v["name"]: v for v in resp.json()}
    assert by_name["strands-agentcore"]["platform"] == "aws"
    assert by_name["strands-agentcore"]["build_kind"] == "container"
    assert by_name["harness-agentcore"]["platform"] == "databricks"
    assert by_name["harness-agentcore"]["build_kind"] == "config"
    infra.list.assert_called_once_with("conn1")
    assert infra.get.call_count == 0


def test_template_list_falls_back_to_aws_on_a_dangling_infra_id(entra_settings):
    """A dangling ``infra_template_id`` (the infra catalog was never seeded for this
    connection, or the entry was deregistered) must NOT drop the template from the catalog:
    the view falls back to the pinned ``"aws"``/``"container"`` — what every template before the
    infra catalog was — so the console still renders the row."""
    svc = MagicMock()
    svc.list_templates.return_value = [
        _template_view(name="orphan", infra_template_id="deleted-infra-template")
    ]
    infra = MagicMock()
    infra.list.return_value = [_infra_record()]
    client = _build_client(fake_svc=svc, fake_infra=infra)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/github-templates?connection_id=conn1", headers=_headers())
    assert resp.status_code == 200
    view = resp.json()[0]
    assert view["name"] == "orphan"
    assert view["platform"] == "aws"
    assert view["build_kind"] == "container"
    assert view["infra_template_id"] == "deleted-infra-template"


def test_template_list_carries_the_preview_flag(entra_settings):
    """``preview`` rides through from the record so the console can chip it. Set EXPLICITLY
    here: the flag is what a preview template's ``template.json`` declares, and a view that
    dropped it would render the harness template as a general-availability choice."""
    svc = MagicMock()
    svc.list_templates.return_value = [
        _template_view(name="harness-agentcore", infra_template_id="agentcore-harness", preview=True),
        _template_view(name="strands-agentcore", preview=False),
    ]
    infra = MagicMock()
    infra.list.return_value = [
        _infra_record(),
        _infra_record(id="agentcore-harness", build_kind="config", iac_type="agentcore_harness"),
    ]
    client = _build_client(fake_svc=svc, fake_infra=infra)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/github-templates?connection_id=conn1", headers=_headers())
    assert resp.status_code == 200
    by_name = {v["name"]: v for v in resp.json()}
    assert by_name["harness-agentcore"]["preview"] is True
    assert by_name["strands-agentcore"]["preview"] is False


def test_template_list_join_survives_a_blank_framework(entra_settings):
    """A blank ``framework`` (``adopt`` registers templates without one) must not break
    the join — ``framework`` is not a join key, and an adopted template still needs a platform
    to be filterable."""
    svc = MagicMock()
    svc.list_templates.return_value = [_template_view(name="adopted", framework="")]
    infra = MagicMock()
    infra.list.return_value = [_infra_record()]
    client = _build_client(fake_svc=svc, fake_infra=infra)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/github-templates?connection_id=conn1", headers=_headers())
    assert resp.status_code == 200
    view = resp.json()[0]
    assert view["framework"] == ""
    assert view["platform"] == "aws"


def test_template_list_degrades_to_aws_when_the_infra_catalog_is_unreadable(entra_settings):
    """The join is BEST-EFFORT on the agent-template route, and that asymmetry is deliberate.
    The infra list route is strict (503) because there the catalog IS the answer; here it is an
    enrichment, so an unreadable infra partition must not 503 a template list that read fine —
    the console would show no templates at all rather than templates it cannot yet filter."""
    svc = MagicMock()
    svc.list_templates.return_value = [_template_view()]
    infra = MagicMock()
    infra.list.side_effect = InfraTemplateRegistryError("Could not read the catalog")
    client = _build_client(fake_svc=svc, fake_infra=infra)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/github-templates?connection_id=conn1", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()[0]["platform"] == "aws"
    assert resp.json()[0]["build_kind"] == "container"


def test_empty_infra_catalog_logs_the_wholesale_fallback(entra_settings, caplog):
    """An EMPTY infra catalog is the worst fallback case and must not be silent.

    A connection whose rollout never ran has no infra entries at all, so EVERY template falls back
    to aws/container — the same output a correctly-seeded all-AWS catalog produces. Without a log
    line the two are indistinguishable, which is exactly the masking the fallback is supposed to be
    visible about. One summary line, not one per template: the cause is a single missing rollout."""
    svc = MagicMock()
    svc.list_templates.return_value = [_template_view("a"), _template_view("b")]
    infra = MagicMock()
    infra.list.return_value = []
    client = _build_client(fake_svc=svc, fake_infra=infra)
    with caplog.at_level("WARNING"):
        with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
            resp = client.get("/api/v1/github-templates?connection_id=conn1", headers=_headers())
    assert resp.status_code == 200
    assert [v["platform"] for v in resp.json()] == ["aws", "aws"]
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    fallback_lines = [m for m in warnings if "no infra templates registered" in m]
    assert len(fallback_lines) == 1, warnings
    assert "conn1" in fallback_lines[0]


def test_dangling_id_is_logged_per_template(entra_settings, caplog):
    """A catalog that HAS entries but not this one is a broken LINK, so it logs per template —
    unlike the empty-catalog case, each miss is its own defect."""
    svc = MagicMock()
    svc.list_templates.return_value = [_template_view("orphan", infra_template_id="gone")]
    infra = MagicMock()
    infra.list.return_value = [_infra_record()]
    client = _build_client(fake_svc=svc, fake_infra=infra)
    with caplog.at_level("WARNING"):
        with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
            resp = client.get("/api/v1/github-templates?connection_id=conn1", headers=_headers())
    assert resp.status_code == 200
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("unknown infra template gone" in m for m in warnings), warnings


def test_store_fault_does_not_double_log_as_an_empty_catalog(entra_settings, caplog):
    """The unreadable-catalog and empty-catalog lines are DIFFERENT diagnoses ("retry" vs "run the
    rollout"). A store fault leaves ``by_id`` empty too, so it must emit only its own line —
    otherwise the log tells an operator to run a rollout that is already done."""
    svc = MagicMock()
    svc.list_templates.return_value = [_template_view()]
    infra = MagicMock()
    infra.list.side_effect = InfraTemplateRegistryError("Could not read the catalog")
    client = _build_client(fake_svc=svc, fake_infra=infra)
    with caplog.at_level("WARNING"):
        with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
            resp = client.get("/api/v1/github-templates?connection_id=conn1", headers=_headers())
    assert resp.status_code == 200
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("infra catalog unreadable" in m for m in warnings), warnings
    assert not any("no infra templates registered" in m for m in warnings), warnings


def test_patch_response_is_joined(entra_settings):
    """A PATCH answered UNJOINED contradicted itself: ``infra_template_id="agentcore-harness"``
    beside the default ``build_kind="container"``. A console that renders the response
    optimistically (rather than refetching) would badge a config template as a container one."""
    svc = MagicMock()
    svc.patch_template.return_value = _template_view(
        name="harness-agentcore", infra_template_id="agentcore-harness", preview=True
    )
    infra = MagicMock()
    infra.list.return_value = [
        _infra_record(id="agentcore-harness", build_kind="config", iac_type="agentcore_harness")
    ]
    client = _build_client(fake_svc=svc, fake_infra=infra)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.patch(
            "/api/v1/github-templates/harness-agentcore?connection_id=conn1",
            json={"tags": ["fsi"]},
            headers=_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["build_kind"] == "config"
    assert body["infra_template_id"] == "agentcore-harness"
    assert body["platform"] == "aws"
    assert body["preview"] is True


def test_upload_response_is_joined(entra_settings):
    """Same contract on create — a 201 must describe the template the same way the list does."""
    svc = MagicMock()
    svc.upload_template.return_value = _template_view(
        name="harness-agentcore", infra_template_id="agentcore-harness"
    )
    infra = MagicMock()
    infra.list.return_value = [
        _infra_record(
            id="agentcore-harness",
            platform="databricks",
            build_kind="config",
            iac_type="agentcore_harness",
        )
    ]
    client = _build_client(fake_svc=svc, fake_infra=infra)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/github-templates",
            files={"file": ("t.zip", b"zipbytes", "application/zip")},
            data={"connection_id": "conn1", "name": "harness-agentcore", "framework": "none"},
            headers=_headers(),
        )
    assert resp.status_code == 201
    assert resp.json()["build_kind"] == "config"
    assert resp.json()["platform"] == "databricks"


def test_infra_route_mounts_on_the_real_app_and_projects_router_does_not_shadow_it():
    """The path lives under ``/projects`` while its router is declared in ``github_templates.py``,
    and ``projects_router`` is registered FIRST — so "nothing shadows it" has to be a claim about
    the actual mounted routes, not about how the patterns look.

    Asserts the RESOLVED endpoint, not a status code. An earlier version issued an unauthenticated
    request and accepted 401/403 as proof, which proves nothing: every ``/projects/*`` route is
    auth-gated, so a SHADOWING pattern would answer 401 too. Starlette resolves in registration
    order, so the discriminating question is which endpoint the first matching route carries — that
    is what would actually change if someone added ``GET /projects/{a}/{b}`` to ``projects_router``.
    Each leaf is matched against the path with its own outer prefix stripped, because a bare
    ``APIRoute.matches()`` knows only its router's path; the first match is compared by module and
    name, since ``reset_modules`` evicts the module and an ``is`` check would fail for a reason
    that has nothing to do with routing; and the restored ``path_format`` proves no path parameter
    was captured, as a shadowing ``/projects/{id}/...`` match would bind one.
    """
    from starlette.routing import Match

    from main import app

    def _flatten(routes, prefix=""):
        """Every leaf route with the prefix it is mounted under. Starlette 1.x keeps each
        ``include_router`` as one ``_IncludedRouter`` wrapper carrying ``.original_router`` (paths
        relative to its own prefix) and ``.include_context.prefix``, so both levels are walked."""
        for r in routes:
            if hasattr(r, "original_router"):
                outer_prefix = prefix + r.include_context.prefix
                yield from _flatten(r.original_router.routes, outer_prefix)
            elif hasattr(r, "path"):
                yield prefix + r.path, prefix, r

    flattened = list(_flatten(app.routes))

    path = "/api/v1/projects/templates/infra"
    assert path in {full_path for full_path, _, _ in flattened}

    matched = []
    for _full_path, prefix, leaf_route in flattened:
        if not path.startswith(prefix):
            continue
        inner_scope = {
            "type": "http",
            "method": "GET",
            "path": path[len(prefix) :],
            "headers": [],
            "path_params": {},
        }
        if leaf_route.matches(inner_scope)[0] is Match.FULL:
            matched.append((prefix, leaf_route))
    assert matched, "no route matched the infra catalog path"

    first_prefix, first_route = matched[0]
    endpoint = first_route.endpoint
    assert endpoint.__module__ == "api.routes.github_templates"
    assert endpoint.__name__ == "list_infra_templates"
    assert first_prefix + first_route.path_format == path


def _tenant(**overrides):
    from models.tenant import Tenant, TenantStageConfig

    fields: dict[str, Any] = dict(
        id="default",
        name="Default",
        line_of_business="Platform",
        entra_group_ids=["g1"],
        stages={"dev": TenantStageConfig(account_id="000000000000")},
        created_by="a@x.com",
        created_at="2026-08-06T00:00:00+00:00",
        updated_at="2026-08-06T00:00:00+00:00",
    )
    fields.update(overrides)
    return Tenant(**fields)


def test_tenant_platform_reads_aws_for_a_default_tenant():
    """An unremarkable tenant — the model's own default — answers ``"aws"``, which is the value the
    picker filters the shipped AgentCore templates on."""
    from models.tenant import tenant_platform

    assert tenant_platform(_tenant()) == "aws"


def test_tenant_platform_reads_the_stored_platform():
    """The seam is closed: ``Tenant.platform`` exists, so the SAME call answers the stored platform.

    Uses a real Databricks tenant rather than a stand-in subclass — that stand-in was what the
    helper was written against before the platform field existed, and asserting against it now
    would pin a shape the model no longer has."""
    from models.tenant import DatabricksStageConfig, TenantPlatform, tenant_platform

    tenant = _tenant(
        platform=TenantPlatform.DATABRICKS,
        stages={
            "dev": DatabricksStageConfig(
                workspace_url="https://dbc-test.cloud.databricks.com",
                sp_client_id="sp-1",
            )
        },
    )
    assert tenant_platform(tenant) == "databricks"


def test_tenant_platform_folds_a_blank_platform_to_aws():
    """A present-but-empty ``platform`` is the post-merge legacy-item case: the field exists on the
    model but the stored record predates it. ``""`` would match no infra template and filter EVERY
    option out of the picker, so it folds to the same default as "absent"."""
    from models.tenant import Tenant, tenant_platform

    class _BlankPlatform(Tenant):
        platform: str = ""

    class _NullPlatform(Tenant):
        platform: Optional[str] = None

    base = _tenant().model_dump()
    assert tenant_platform(_BlankPlatform(**base)) == "aws"
    assert tenant_platform(_NullPlatform(**base)) == "aws"
