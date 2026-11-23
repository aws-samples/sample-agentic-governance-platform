"""`agent.platform` ↔ `tenant.platform` validation on POST /agents .

Two enums have always described the same axis in two alphabets and NOTHING translated
between them, so an `aws_bedrock` agent could be registered under a Databricks tenant and
then run the AgentCore provisioning path against a workspace that has no AgentCore. The bridge
(`GOVERNABLE_PLATFORMS` / `is_inventory_only`) translates; this file pins the route's use of it.

Contract under test:
  - A GOVERNABLE `platform` whose `GOVERNABLE_PLATFORMS` target != the tenant's `platform`
    -> 409 with the FIXED detail literal, and `svc.create` is never called (the refusal
    precedes every side effect, like the tenant-exists/permission checks it sits with).
  - A matching pair is unchanged: 201, create called, provisioning hook untouched.
  - The six INVENTORY-ONLY labels register under ANY tenant (`sap` under both) — they are
    registry rows, not governed runtimes, so there is no platform to contradict.

Harness mirrors `test_registry_tenant_scoping.py`'s create-path tests: a minimal app carrying
only the agents router, the registry singleton mocked, `api.routes.users._tenant_resolver`
seeded, and `api.routes.tenants._svc` replaced by a platform-typed fake.
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("reset_modules")


def _make_agent(**overrides):
    from models.agent import Agent, LifecycleState, Origin

    now = datetime.now(timezone.utc)
    base: dict[str, Any] = dict(
        id="rec-123",
        name="claims-triage-de",
        purpose="Triage claims",
        lifecycle_state=LifecycleState.PROPOSED,
        origin=Origin.REGISTERED,
        created_at=now,
        updated_at=now,
        created_by="operator.user@example.com",
        tenant_id="ten-1",
    )
    base.update(overrides)
    return Agent(**base)


def _claims_for(role: str):
    role_app = {
        "viewer": "Platform.Viewer",
        "operator": "Platform.Operator",
        "admin": "Platform.Admin",
    }[role]
    return {
        "oid": f"{role}-oid",
        "preferred_username": f"{role}.user@example.com",
        "roles": [role_app],
    }


def _headers():
    return {"Authorization": "Bearer fake-token"}


class _FakePlatformTenantService:
    """`TenantService`-shaped fake: tenant_id -> a tenant carrying a real `TenantPlatform`.

    Only `.get` is exercised by the create path, and only `.platform` is read off the
    result — but the value must be a genuine `TenantPlatform`, because that is what the
    stored model guarantees (`from_envelope` defaults an older record to `aws`).
    """

    def __init__(self, platforms):
        self._platforms = dict(platforms)

    def get(self, tenant_id):
        from services.tenant_service import TenantError

        if tenant_id not in self._platforms:
            raise TenantError("Unknown tenant", kind="not_found")
        return MagicMock(id=tenant_id, platform=self._platforms[tenant_id])


def _build_client(mock_svc):
    """Minimal agents-router app; both tenants are visible to a global caller."""
    import api.routes.agents as agents_module
    import api.routes.tenants as tenants_module
    import api.routes.users as users_module
    from core.errors import install_error_handlers
    from models.tenant import TenantPlatform
    from services.project_resolver import ProjectContext
    from services.tenant_resolver import TenantContext

    agents_module._svc = mock_svc

    class _GlobalResolver:
        def __init__(self, ctx):
            self._ctx = ctx

        async def resolve(self, principal):
            return self._ctx

    users_module._tenant_resolver = _GlobalResolver(
        TenantContext(is_global=True, tenant_ids=frozenset(), tenants=())
    )
    users_module._project_resolver = _GlobalResolver(ProjectContext(is_global=True, roles={}))
    tenants_module._svc = _FakePlatformTenantService(
        {"ten-aws": TenantPlatform.AWS, "ten-dbx": TenantPlatform.DATABRICKS}
    )

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(agents_module.router, prefix="/api/v1")
    return TestClient(app), agents_module


def _create(client, *, platform, tenant_id, name="new-agent"):
    body = {"name": name, "tenant_id": tenant_id}
    if platform is not None:
        body["platform"] = platform
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        return client.post("/api/v1/agents", json=body, headers=_headers())


def _update(client, agent_id="rec-123", **body):
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        return client.put(f"/api/v1/agents/{agent_id}", json=body, headers=_headers())


def test_aws_bedrock_agent_under_databricks_tenant_409(entra_settings):
    mock_svc = MagicMock()
    client, _agents_module = _build_client(mock_svc)

    resp = _create(client, platform="aws_bedrock", tenant_id="ten-dbx")

    assert resp.status_code == 409
    assert resp.json()["detail"] == "agent platform does not match the tenant's platform"
    mock_svc.create.assert_not_called()


def test_databricks_agent_under_aws_tenant_409(entra_settings):
    mock_svc = MagicMock()
    client, _ = _build_client(mock_svc)

    resp = _create(client, platform="databricks", tenant_id="ten-aws")

    assert resp.status_code == 409
    assert resp.json()["detail"] == "agent platform does not match the tenant's platform"
    mock_svc.create.assert_not_called()


def test_aws_bedrock_agent_under_aws_tenant_201(entra_settings):
    mock_svc = MagicMock()
    mock_svc.create.return_value = _make_agent(id="new", tenant_id="ten-aws")
    client, _ = _build_client(mock_svc)

    resp = _create(client, platform="aws_bedrock", tenant_id="ten-aws")

    assert resp.status_code == 201
    mock_svc.create.assert_called_once()


def test_databricks_agent_under_databricks_tenant_201(entra_settings):
    mock_svc = MagicMock()
    mock_svc.create.return_value = _make_agent(id="new", tenant_id="ten-dbx")
    client, _ = _build_client(mock_svc)

    resp = _create(client, platform="databricks", tenant_id="ten-dbx")

    assert resp.status_code == 201
    mock_svc.create.assert_called_once()


def test_agent_with_no_platform_label_is_unaffected(entra_settings):
    """The common case: no label at all is not a claim about a platform."""
    mock_svc = MagicMock()
    mock_svc.create.return_value = _make_agent(id="new", tenant_id="ten-dbx")
    client, _ = _build_client(mock_svc)

    resp = _create(client, platform=None, tenant_id="ten-dbx")

    assert resp.status_code == 201
    mock_svc.create.assert_called_once()


@pytest.mark.parametrize("tenant_id", ["ten-aws", "ten-dbx"])
def test_inventory_only_sap_agent_accepted_under_either_tenant(entra_settings, tenant_id):
    mock_svc = MagicMock()
    mock_svc.create.return_value = _make_agent(id="new", tenant_id=tenant_id)
    client, _ = _build_client(mock_svc)

    resp = _create(client, platform="sap", tenant_id=tenant_id)

    assert resp.status_code == 201
    mock_svc.create.assert_called_once()


def test_inventory_only_agent_update_is_accepted(entra_settings):
    """An inventory-only agent stays freely editable — no tenant read, no verdict."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(platform="sap", tenant_id="ten-dbx")
    mock_svc.update.return_value = _make_agent(platform="sap", tenant_id="ten-dbx", purpose="updated")
    client, _ = _build_client(mock_svc)

    resp = _update(client, purpose="updated")

    assert resp.status_code == 200
    mock_svc.update.assert_called_once()


def test_update_flipping_platform_to_a_contradicting_one_409(entra_settings):
    """`AgentUpdate.platform` is live: without the gate, one PUT undoes the create check."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(platform="aws_bedrock", tenant_id="ten-aws")
    client, _ = _build_client(mock_svc)

    resp = _update(client, platform="databricks")

    assert resp.status_code == 409
    assert resp.json()["detail"] == "agent platform does not match the tenant's platform"
    mock_svc.update.assert_not_called()


def test_update_to_the_matching_platform_is_accepted(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(tenant_id="ten-dbx")
    mock_svc.update.return_value = _make_agent(platform="databricks", tenant_id="ten-dbx")
    client, _ = _build_client(mock_svc)

    resp = _update(client, platform="databricks")

    assert resp.status_code == 200
    mock_svc.update.assert_called_once()


def test_update_without_a_platform_key_is_untouched_behavior(entra_settings):
    """The stored (matching) label is the effective one — a PUT of other fields is unaffected."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(platform="databricks", tenant_id="ten-dbx")
    mock_svc.update.return_value = _make_agent(platform="databricks", tenant_id="ten-dbx", purpose="updated")
    client, _ = _build_client(mock_svc)

    resp = _update(client, purpose="updated")

    assert resp.status_code == 200
    mock_svc.update.assert_called_once()


def test_update_of_an_already_contradicting_stored_agent_409(entra_settings):
    """An older record whose stored label already contradicts its tenant cannot be edited
    further into place — the effective platform is judged on every write."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(platform="aws_bedrock", tenant_id="ten-dbx")
    client, _ = _build_client(mock_svc)

    resp = _update(client, purpose="updated")

    assert resp.status_code == 409
    mock_svc.update.assert_not_called()


def test_update_to_an_inventory_only_platform_is_accepted(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(platform="aws_bedrock", tenant_id="ten-aws")
    mock_svc.update.return_value = _make_agent(platform="sap", tenant_id="ten-aws")
    client, _ = _build_client(mock_svc)

    resp = _update(client, platform="sap")

    assert resp.status_code == 200
    mock_svc.update.assert_called_once()


def test_update_with_an_unresolvable_tenant_is_404(entra_settings):
    """`TenantService._get` swallows a DDB `ClientError` into `kind="not_found"`, so a table
    hiccup and a deleted tenant look identical from the route. Either way a governable label is
    not waved through: the tenant domain's ``not_found`` row is the refusal (before the error
    layer this route answered a 502 of its own)."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(platform="aws_bedrock", tenant_id="ten-gone")
    client, _ = _build_client(mock_svc)

    resp = _update(client, platform="databricks")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Tenant not found", "code": "tenant.not_found"}
    mock_svc.update.assert_not_called()


def test_update_when_the_tenant_read_raises_unexpectedly_does_not_fail_open(entra_settings):
    """A raw store/transport error (not a `TenantError`) is not swallowed into a pass: it leaves
    the route unhandled, which the app's generic handler renders as a 500, and no write happens."""
    import api.routes.tenants as tenants_module

    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(platform="aws_bedrock", tenant_id="ten-aws")
    client, _ = _build_client(mock_svc)

    class _ExplodingTenantService:
        def get(self, tenant_id):
            raise RuntimeError("boto blew up")

    tenants_module._svc = _ExplodingTenantService()

    with pytest.raises(RuntimeError):
        _update(client, platform="databricks")

    mock_svc.update.assert_not_called()


def test_update_of_a_pre_e24_agent_without_a_tenant_id_passes_through(entra_settings):
    """The ONE remaining permissive case: no `tenant_id` at all means there is no tenant to
    contradict — and no tenant read is attempted, so the update path stays independent of the
    tenants table for legacy records. Create's 400 owns nonexistent tenants."""
    import api.routes.tenants as tenants_module

    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(platform="aws_bedrock", tenant_id=None)
    mock_svc.update.return_value = _make_agent(platform="databricks", tenant_id=None)
    client, _ = _build_client(mock_svc)

    class _NeverCalledTenantService:
        def get(self, tenant_id):
            raise AssertionError("the tenants table must not be read for a tenant-less agent")

    tenants_module._svc = _NeverCalledTenantService()

    resp = _update(client, platform="databricks")

    assert resp.status_code == 200
    mock_svc.update.assert_called_once()


def test_update_repairing_a_real_stored_contradiction_is_accepted(entra_settings):
    """The documented escape hatch, with a REAL stored contradiction (not `None`):
    an older `aws_bedrock` agent under a Databricks tenant is repaired by the same PUT."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(platform="aws_bedrock", tenant_id="ten-dbx")
    mock_svc.update.return_value = _make_agent(platform="databricks", tenant_id="ten-dbx")
    client, _ = _build_client(mock_svc)

    resp = _update(client, platform="databricks")

    assert resp.status_code == 200
    assert resp.json()["platform"] == "databricks"
    mock_svc.update.assert_called_once()


def test_every_inventory_only_label_is_accepted(entra_settings):
    """Derived, not hand-listed: the six come from the ``GOVERNABLE_PLATFORMS`` bridge."""
    from models.agent import INVENTORY_ONLY_PLATFORMS

    for platform in sorted(p.value for p in INVENTORY_ONLY_PLATFORMS):
        mock_svc = MagicMock()
        mock_svc.create.return_value = _make_agent(id="new", tenant_id="ten-dbx")
        client, _ = _build_client(mock_svc)

        resp = _create(client, platform=platform, tenant_id="ten-dbx")

        assert resp.status_code == 201, f"{platform} was refused"
