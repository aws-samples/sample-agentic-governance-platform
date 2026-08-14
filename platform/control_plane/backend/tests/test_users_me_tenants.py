"""E24 Task 4 — /users/me gains a resolved ``tenants`` list.

The route resolves the caller's tenant memberships via ``TenantResolver.resolve``
(patched here — no live Graph/DDB) and returns them as
``tenants: list[{id, name, line_of_business, aws_account_dev, aws_account_prod,
aws_region}]``. A resolver failure must NEVER break /users/me — it degrades to
``tenants: []``. Admins get their *memberships* (possibly ``[]``) — the ``role``
field already tells the FE they are global.

Follows ``test_users_me_entra.py``'s idiom: Entra env + module reset + mocked
``verify_entra_token``; the resolver singleton is pre-seeded by assigning
``api.routes.users._tenant_resolver`` (the ``tenants.py`` ``_svc``-patch idiom).
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

FIXED_TS = "2026-07-13T00:00:00+00:00"

EXPECTED_TENANT_KEYS = {
    "id",
    "name",
    "line_of_business",
    # E29/T9 (OB-14). Their ABSENCE was the defect: the registration wizard infers a tenant's
    # platform from this projection and defaults an absent one to "aws", so a non-admin operator on
    # a Databricks tenant got the AgentCore branch — an ARN field for a platform with no ARNs.
    "platform",
    "binding_mode",
    "stages",
}

# Every field a Databricks stage is allowed to put on this wire (E29/T9, OB-9). Asserted as an
# EXACT set, not a subset: the point of the projection is what it does NOT send.
EXPECTED_DATABRICKS_STAGE_KEYS = {"workspace_url", "workspace_id", "region", "account_id"}

# A fake workspace, per the epic's Global Constraints (the one allowed hardcoded URL shape).
FAKE_WORKSPACE_URL = "https://dbc-test.cloud.databricks.com"


@pytest.fixture(autouse=True)
def reset_modules(monkeypatch):
    """Configure Entra mode and reload all modules so the env takes effect."""
    monkeypatch.setenv("AUTH_PROVIDER", "entra")
    monkeypatch.setenv("USE_DEV_AUTH", "False")
    monkeypatch.setenv("DEBUG", "False")
    monkeypatch.setenv("ENTRA_TENANT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("ENTRA_AUDIENCE", "api://agp")
    monkeypatch.setenv("ENTRA_ROLE_ADMIN", "Platform.Admin")
    monkeypatch.setenv("ENTRA_ROLE_OPERATOR", "Platform.Operator")
    monkeypatch.setenv("ENTRA_ROLE_VIEWER", "Platform.Viewer")

    import sys
    for mod in list(sys.modules):
        if mod.startswith("core.") or mod.startswith("api.") or mod == "main":
            sys.modules.pop(mod, None)
    yield


# --- fakes -------------------------------------------------------------------


def _tenant(*, id: str, name: str):
    from models.tenant import Tenant, TenantStageConfig

    return Tenant(
        id=id,
        name=name,
        line_of_business="Retail",
        entra_group_ids=["grp-1"],
        stages={
            "dev": TenantStageConfig(account_id="111111111111", region="eu-central-1"),
            "prod": TenantStageConfig(account_id="222222222222", region="eu-central-1"),
        },
        description="",
        created_by="seed",
        created_at=FIXED_TS,
        updated_at=FIXED_TS,
    )


def _databricks_tenant(*, id: str = "ten-db", name: str = "Claims", binding_mode: str = "sp_secret"):
    """A platform-typed Databricks tenant whose stage carries EVERY field the model allows —
    including the two that must never reach this wire (``sp_client_id``, ``sp_client_secret_arn``)
    and the AWS-shaped ``account_id``, which on a Databricks stage is a Databricks account UUID.

    Populating them all is the point: a fixture that left them empty could not tell a projection
    that omits a field from one that merely had nothing to send.
    """
    from models.tenant import DatabricksStageConfig, Tenant, TenantPlatform

    return Tenant(
        id=id,
        name=name,
        line_of_business="Insurance",
        entra_group_ids=["grp-db"],
        platform=TenantPlatform.DATABRICKS,
        binding_mode=binding_mode,
        stages={
            "dev": DatabricksStageConfig(
                workspace_url=FAKE_WORKSPACE_URL,
                workspace_id="1234567890123456",
                cloud="aws",
                region="us-east-1",
                # A Databricks ACCOUNT UUID — NOT an AWS account id, which is the whole OB-9 point.
                account_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                sp_client_id="sp-client-abc",
                sp_client_secret_arn="arn:aws:secretsmanager:us-east-1:redacted:secret:x",
            ),
        },
        description="",
        created_by="seed",
        created_at=FIXED_TS,
        updated_at=FIXED_TS,
    )


def _context(tenants):
    from services.tenant_resolver import TenantContext

    return TenantContext(
        is_global=False,
        tenant_ids=frozenset(t.id for t in tenants),
        tenants=tuple(tenants),
    )


class _FakeResolver:
    """Async ``resolve`` stub — records the principals it saw."""

    def __init__(self, *, context=None, exc=None):
        self._context = context
        self._exc = exc
        self.calls = []

    async def resolve(self, principal):
        self.calls.append(principal)
        if self._exc is not None:
            raise self._exc
        return self._context


def _build_client(resolver) -> TestClient:
    """Fresh app + pre-seeded resolver singleton on the freshly-imported module."""
    from main import app

    import api.routes.users as users_module

    users_module._tenant_resolver = resolver
    return TestClient(app)


def _claims(role_value: str, oid: str = "maria-oid") -> dict:
    return {
        "oid": oid,
        "preferred_username": "maria.bauer@contoso.onmicrosoft.com",
        "name": "Maria Bauer",
        "roles": [role_value],
    }


# --- tests ---------------------------------------------------------------------


def test_users_me_returns_resolved_tenants_with_exact_keys():
    """Operator with two memberships → both tenants, each with exactly the six keys."""
    tenants = [_tenant(id="ten-1", name="Retail"), _tenant(id="ten-2", name="Wholesale")]
    resolver = _FakeResolver(context=_context(tenants))
    client = _build_client(resolver)

    with patch("core.security_entra.verify_entra_token", return_value=_claims("Platform.Operator")):
        resp = client.get(
            "/api/v1/users/me", headers={"Authorization": "Bearer fake-token"}
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [t["id"] for t in body["tenants"]] == ["ten-1", "ten-2"]
    for t in body["tenants"]:
        assert set(t.keys()) == EXPECTED_TENANT_KEYS
    first = body["tenants"][0]
    assert first["name"] == "Retail"
    assert first["line_of_business"] == "Retail"
    assert first["stages"]["dev"]["account_id"] == "111111111111"
    assert first["stages"]["prod"]["account_id"] == "222222222222"
    assert first["stages"]["dev"]["region"] == "eu-central-1"
    # E29: a pre-E29 record hydrates platform="aws" and carries no binding mode. Both are PRESENT
    # on the wire — an absent key is what made the wizard guess (OB-14).
    assert first["platform"] == "aws"
    assert first["binding_mode"] == ""


def test_users_me_aws_stage_projection_is_byte_identical_to_model_dump():
    """The AWS branch is the FENCE: its stage projection is exactly ``model_dump()``.

    Asserted against the model rather than against a hand-written dict, so a field ADDED to
    ``TenantStageConfig`` in future must appear here too — which is the pre-E29 contract. The
    Databricks branch enumerates instead (new fields omitted by default); only this branch
    publishes by default, and that asymmetry should be visible in a test.
    """
    tenants = [_tenant(id="ten-1", name="Retail")]
    client = _build_client(_FakeResolver(context=_context(tenants)))

    with patch("core.security_entra.verify_entra_token", return_value=_claims("Platform.Operator")):
        resp = client.get("/api/v1/users/me", headers={"Authorization": "Bearer fake-token"})

    assert resp.status_code == 200, resp.text
    stages = resp.json()["tenants"][0]["stages"]
    for name, cfg in tenants[0].stages.items():
        assert stages[name] == cfg.model_dump()


def test_users_me_projects_platform_and_binding_mode_for_databricks():
    """OB-14: a Databricks membership states its platform and mode, so the wizard need not guess."""
    tenants = [_databricks_tenant(binding_mode="federation")]
    client = _build_client(_FakeResolver(context=_context(tenants)))

    with patch("core.security_entra.verify_entra_token", return_value=_claims("Platform.Operator")):
        resp = client.get("/api/v1/users/me", headers={"Authorization": "Bearer fake-token"})

    assert resp.status_code == 200, resp.text
    t = resp.json()["tenants"][0]
    assert set(t.keys()) == EXPECTED_TENANT_KEYS
    assert t["platform"] == "databricks"
    assert t["binding_mode"] == "federation"
    # A plain JSON string, not a serialized enum member — `TenantPlatform` is a str Enum and
    # `tenants` is an unmodelled `list[dict]`, so nothing downstream would coerce it for us.
    assert isinstance(t["platform"], str)


def test_users_me_databricks_stage_omits_aws_only_and_credential_fields():
    """OB-9: the projected Databricks stage says what is true and omits what would mislead."""
    tenants = [_databricks_tenant()]
    client = _build_client(_FakeResolver(context=_context(tenants)))

    with patch("core.security_entra.verify_entra_token", return_value=_claims("Platform.Operator")):
        resp = client.get("/api/v1/users/me", headers={"Authorization": "Bearer fake-token"})

    assert resp.status_code == 200, resp.text
    stage = resp.json()["tenants"][0]["stages"]["dev"]

    # EXACT key set — the omissions are the deliverable, so a subset assertion would not test it.
    assert set(stage.keys()) == EXPECTED_DATABRICKS_STAGE_KEYS

    # What IS true about the stage.
    assert stage["workspace_url"] == FAKE_WORKSPACE_URL
    assert stage["workspace_id"] == "1234567890123456"
    assert stage["region"] == "us-east-1"

    # THE OB-9 DEFECT, pinned. `ProjectDetail.tsx` renders `stages[x].account_id` under a
    # "{stage} account" heading. The record holds a Databricks account UUID there; sending it would
    # make that panel print a plausible-looking wrong answer. Empty is not a claim.
    assert stage["account_id"] == ""
    assert "aaaaaaaa" not in resp.text

    # Credential metadata must not be handed to every member of the tenant.
    assert "sp_client_id" not in stage
    assert "sp_client_secret_arn" not in stage
    assert "sp-client-abc" not in resp.text
    assert "secretsmanager" not in resp.text
    # The write-only secret field cannot appear at all (Field(exclude=True)) — asserted anyway,
    # because "cannot" is a property of a model this function does not control.
    assert "sp_client_secret" not in stage
    # `cloud` is dropped: no reader on this wire uses it.
    assert "cloud" not in stage


def test_users_me_mixed_memberships_project_per_platform():
    """A caller in BOTH an AWS and a Databricks tenant gets each projected on its own rules.

    The case a single-platform test cannot cover: one shared code path, two shapes, and the AWS
    membership must be untouched by the Databricks branch existing.
    """
    aws = _tenant(id="ten-aws", name="Retail")
    db = _databricks_tenant(id="ten-db", name="Claims")
    client = _build_client(_FakeResolver(context=_context([aws, db])))

    with patch("core.security_entra.verify_entra_token", return_value=_claims("Platform.Operator")):
        resp = client.get("/api/v1/users/me", headers={"Authorization": "Bearer fake-token"})

    assert resp.status_code == 200, resp.text
    by_id = {t["id"]: t for t in resp.json()["tenants"]}

    assert by_id["ten-aws"]["platform"] == "aws"
    assert by_id["ten-aws"]["stages"]["dev"] == aws.stages["dev"].model_dump()

    assert by_id["ten-db"]["platform"] == "databricks"
    assert set(by_id["ten-db"]["stages"]["dev"].keys()) == EXPECTED_DATABRICKS_STAGE_KEYS


def test_users_me_resolver_receives_validated_principal():
    """The resolver is fed the validated Principal (oid from the token claims)."""
    resolver = _FakeResolver(context=_context([]))
    client = _build_client(resolver)

    with patch("core.security_entra.verify_entra_token", return_value=_claims("Platform.Operator")):
        resp = client.get(
            "/api/v1/users/me", headers={"Authorization": "Bearer fake-token"}
        )

    assert resp.status_code == 200, resp.text
    assert len(resolver.calls) == 1
    assert resolver.calls[0].oid == "maria-oid"


def test_users_me_resolver_failure_degrades_to_empty_tenants():
    """A raising resolver must NOT break /users/me — 200 with tenants: []."""
    resolver = _FakeResolver(exc=RuntimeError("boom"))
    client = _build_client(resolver)

    with patch("core.security_entra.verify_entra_token", return_value=_claims("Platform.Operator")):
        resp = client.get(
            "/api/v1/users/me", headers={"Authorization": "Bearer fake-token"}
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenants"] == []
    # The rest of the identity payload is intact.
    assert body["email"] == "maria.bauer@contoso.onmicrosoft.com"
    assert body["role"] == "operator"


def test_users_me_admin_gets_memberships_possibly_empty():
    """Admins get their memberships here (possibly []) — role already says global."""
    resolver = _FakeResolver(context=_context([]))
    client = _build_client(resolver)

    with patch("core.security_entra.verify_entra_token", return_value=_claims("Platform.Admin", oid="lars-oid")):
        resp = client.get(
            "/api/v1/users/me", headers={"Authorization": "Bearer fake-token"}
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role"] == "admin"
    assert body["tenants"] == []


def test_users_me_dev_auth_includes_tenants(monkeypatch):
    """The dev-auth branch resolves tenants too (patched resolver; no token)."""
    monkeypatch.setenv("USE_DEV_AUTH", "True")

    import sys
    for mod in list(sys.modules):
        if mod.startswith("core.") or mod.startswith("api.") or mod == "main":
            sys.modules.pop(mod, None)

    tenants = [_tenant(id="ten-dev", name="DevTenant")]
    resolver = _FakeResolver(context=_context(tenants))
    client = _build_client(resolver)

    resp = client.get("/api/v1/users/me", headers={"x-user-role": "admin"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [t["id"] for t in body["tenants"]] == ["ten-dev"]
