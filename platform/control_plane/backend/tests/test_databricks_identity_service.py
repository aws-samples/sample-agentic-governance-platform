"""Tests for ``services.databricks_identity_service``, the platform half of provisioning for a
Databricks-hosted agent.

The tenant's workspace service-principal secret is fetched at call time from Secrets Manager and
flows only into a token mint; it must never reach the agent record, a log line or an exception
message, and the tests for all three are driven by a sentinel value (``WS_SECRET_SENTINEL``) so a
leak is a failing assertion.

Every external collaborator is a double. ``DatabricksWorkspaceService`` is ``_FakeDatabricks``,
which rejects wrong-shaped calls: a token minted before any authenticated call,
``ensure_federation_audience`` keyword-called with ``present=``, a non-empty app name. boto3
``secretsmanager`` is ``_FakeSecrets``, which 404s on an unknown id, proving the stored ARN is what
is read. ``AgentRegistryService`` is a ``MagicMock`` whose ``persist_identity`` records each call.
The repo is not in pytest-asyncio ``auto`` mode, so every async test carries
``@pytest.mark.asyncio``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from models.agent import Agent, AgentAuthType, IdentityStatus, LifecycleState, Platform
from models.tenant import (
    ACCOUNT_ADMIN_ID_KEY,
    ACCOUNT_ADMIN_SECRET_KEY,
    SP_SECRET_KEY,
    DatabricksStageConfig,
    Tenant,
    TenantBindingMode,
    TenantPlatform,
)
from services.agent_identity_service import ProvisioningError
from services.databricks_workspace_service import DatabricksError
from services.platform_adapter import Artifact, ArtifactPolicy

WORKSPACE_URL = "https://dbc-test.cloud.databricks.com"
APP_NAME = "claims-triage"
APP_URL = "https://claims-triage-1234.aws.databricksapps.com"
APP_OAUTH_CLIENT_ID = "app-oauth-client-id-from-listing"
ACCOUNT_ID = "11111111-2222-3333-4444-555555555555"
WS_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:000000000000:secret:agp/databricks/ws-AbCdEf"
WS_SECRET_SENTINEL = "WORKSPACE_SP_SECRET_DO_NOT_LEAK"
ADMIN_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:000000000000:secret:agp/databricks/admin-GhIjKl"
ADMIN_CLIENT_ID = "account-admin-client-id"
ADMIN_SECRET_SENTINEL = "ACCOUNT_ADMIN_SECRET_DO_NOT_LEAK"
MINTED_SP_SECRET = "MINTED_PER_AGENT_SP_SECRET_DO_NOT_LEAK"
SCIM_SP_ID = "7788990011"
AGENT_AUDIENCE = "api://agp-agent-rec-abc123"
GROUP_A = "group-aaa"
GROUP_B = "group-bbb"
WS_SP_CLIENT_ID = "ws-sp-client-id"
_ADMINS_ACL = ("admins", "group", "CAN_MANAGE")
_TENANT_SP_ACL = (WS_SP_CLIENT_ID, "service_principal", "CAN_MANAGE")


def _asserted_acl(*extra: tuple) -> tuple:
    """The ACL the assert is expected to PUT, in the double's recorded (sorted) form."""
    return tuple(sorted((_ADMINS_ACL, _TENANT_SP_ACL, *extra)))


def _make_agent(
    *,
    agent_id: str = "rec-abc123",
    runtime_handle: str | None = APP_URL,
    runtime_kind: str | None = "app",
    platform: Platform | None = Platform.DATABRICKS,
    auth_type: AgentAuthType = AgentAuthType.ENTRA,
    tenant_id: str | None = "ten-1",
    identity_status: IdentityStatus = IdentityStatus.PENDING,
    binding_mode: TenantBindingMode | None = None,
    databricks_sp_id: str | None = None,
    databricks_sp_secret_arn: str | None = None,
    oauth2_app_client_id: str | None = None,
    entra_app_audience: str | None = AGENT_AUDIENCE,
    agent_arn: str | None = None,
) -> Agent:
    now = datetime.now(timezone.utc)
    return Agent(
        id=agent_id,
        name="Claims Triage DE",
        purpose="Triage inbound motor claims",
        lifecycle_state=LifecycleState.APPROVED,
        platform=platform,
        auth_type=auth_type,
        tenant_id=tenant_id,
        agent_arn=agent_arn,
        runtime_handle=runtime_handle,
        runtime_kind=runtime_kind,
        binding_mode=binding_mode,
        databricks_sp_id=databricks_sp_id,
        databricks_sp_secret_arn=databricks_sp_secret_arn,
        oauth2_app_client_id=oauth2_app_client_id,
        entra_app_audience=entra_app_audience,
        entra_app_id="agent-app-guid",
        entra_sp_id="agent-sp-guid",
        identity_status=identity_status,
        created_at=now,
        updated_at=now,
        created_by="maria.bauer@example.com",
    )


def _make_tenant(
    *,
    binding_mode: TenantBindingMode = "federation",
    capabilities: dict | None = None,
    entra_group_ids: list[str] | None = None,
    account_id: str = ACCOUNT_ID,
    sp_client_secret_arn: str = WS_SECRET_ARN,
    stages: dict | None = None,
    account_admin_secret_arn: str = ADMIN_SECRET_ARN,
) -> Tenant:
    if capabilities is None:
        capabilities = {"can_discover": True, "account_admin": True, "user_sync": True}
    if stages is None:
        stages = {
            "dev": DatabricksStageConfig(
                workspace_url=WORKSPACE_URL,
                workspace_id="0",
                cloud="aws",
                region="us-east-1",
                account_id=account_id,
                sp_client_id=WS_SP_CLIENT_ID,
                sp_client_secret_arn=sp_client_secret_arn,
            )
        }
    return Tenant(
        id="ten-1",
        name="Retail Claims",
        line_of_business="Claims",
        entra_group_ids=entra_group_ids if entra_group_ids is not None else [GROUP_A],
        platform=TenantPlatform.DATABRICKS,
        stages=stages,
        capabilities=capabilities,
        binding_mode=binding_mode,
        account_admin_secret_arn=account_admin_secret_arn,
        created_by="seed",
        created_at="2026-08-06T00:00:00+00:00",
        updated_at="2026-08-06T00:00:00+00:00",
    )


class _FakeDatabricks:
    """A DatabricksWorkspaceService double that refuses wrong-shaped calls.

    Anything it accepts is a shape the real client would accept; anything it rejects the real
    client would reject too (or would silently do the wrong thing, which is worse). Every call is
    recorded in ``self.calls`` so ordering is assertable. ``apps_by_workspace`` models a tenant
    with more than one workspace, only one of which lists the app: a Databricks app URL carries
    no workspace identity, so stage resolution is testable only by which workspace answers with
    the app.
    """

    def __init__(self, *, apps=None, apps_by_workspace=None):
        self.calls: list[tuple] = []
        self._apps_by_workspace = apps_by_workspace
        self._apps = (
            apps
            if apps is not None
            else [{"name": APP_NAME, "url": APP_URL, "oauth2_app_client_id": APP_OAUTH_CLIENT_ID}]
        )
        self.minted_ws_tokens = 0
        self.minted_account_tokens = 0
        self.audiences: set[str] = set()
        self.raise_on: dict[str, Exception] = {}
        self.grant_failures: set[str] = set()
        self.acl: list[dict] = [{"principal": "admins", "kind": "group", "level": "CAN_MANAGE"}]
        self.sp_seq = 0
        self.account_hosts: list[str | None] = []
        self.account_credentials: list[tuple[str, str]] = []

    def _maybe_raise(self, what: str) -> None:
        err = self.raise_on.get(what)
        if err is not None:
            raise err

    async def mint_m2m_token(self, workspace_url, client_id, client_secret):
        assert workspace_url.startswith("https://"), "a token must be minted over https"
        if self._apps_by_workspace is None:
            assert workspace_url == WORKSPACE_URL, "workspace token minted at the wrong host"
        assert client_id and client_secret, "M2M mint needs both halves of the credential"
        self.calls.append(("mint_m2m_token", workspace_url))
        self._maybe_raise("mint_m2m_token")
        self.minted_ws_tokens += 1
        return f"ws-token-{self.minted_ws_tokens}"

    async def mint_account_token(self, account_id, client_id, client_secret, *, account_host=None):
        assert account_id, "an account token cannot be minted without an account id"
        assert client_id and client_secret
        self.calls.append(("mint_account_token", account_id))
        self.account_hosts.append(account_host)
        self.account_credentials.append((client_id, client_secret))
        self._maybe_raise("mint_account_token")
        self.minted_account_tokens += 1
        return f"account-token-{self.minted_account_tokens}"

    async def list_apps(self, workspace_url, token):
        assert token.startswith("ws-token-"), "list_apps needs a WORKSPACE token"
        self.calls.append(("list_apps", workspace_url))
        self._maybe_raise("list_apps")
        if self._apps_by_workspace is not None:
            return list(self._apps_by_workspace.get(workspace_url, []))
        return list(self._apps)

    async def grant_app_can_use(
        self, workspace_url, token, app_name, service_principal_or_group, kind="group"
    ):
        """The additive (PATCH) grant. Kept on the double even though provisioning no longer calls
        it: the tests that assert it is never reached would pass trivially against a double that
        could not receive the call."""
        assert token.startswith("ws-token-"), "a grant needs a WORKSPACE token"
        assert app_name, "a grant with no app name would PATCH the collection root"
        assert service_principal_or_group, "a grant needs a principal"
        self.calls.append(("grant_app_can_use", app_name, service_principal_or_group))
        if service_principal_or_group in self.grant_failures:
            raise DatabricksError("Databricks rejected the request", kind="forbidden")
        self._maybe_raise("grant_app_can_use")

    async def get_app_permissions(self, workspace_url, token, app_name):
        assert token.startswith("ws-token-"), "reading an app ACL needs a WORKSPACE token"
        assert app_name, "no app name would read the permissions collection root"
        self.calls.append(("get_app_permissions", app_name))
        self._maybe_raise("get_app_permissions")
        return [dict(entry) for entry in self.acl]

    async def set_app_permissions(self, workspace_url, token, app_name, entries):
        """The ASSERT (PUT). Mirrors the real client's two refusals, because a double that
        accepted an admin-less or malformed list would let the service ship a PUT that locks
        the workspace admins out of their own app."""
        assert token.startswith("ws-token-"), "asserting an app ACL needs a WORKSPACE token"
        assert app_name, "no app name would PUT the permissions collection root"
        assert any(
            e.get("principal") == "admins" and e.get("kind") == "group" and e.get("level") == "CAN_MANAGE"
            for e in entries
        ), "the real client REFUSES an ACL that does not keep the admins' CAN_MANAGE"
        for entry in entries:
            assert entry.get("kind") in ("user", "group", "service_principal"), (
                f"an unrecognised ACL kind is refused upstream: {entry!r}"
            )
            assert entry.get("principal"), f"an ACL entry needs a principal: {entry!r}"
            assert entry.get("level") in ("CAN_USE", "CAN_MANAGE"), (
                f"an unrecognised ACL level is refused upstream: {entry!r}"
            )
            assert not entry.get("inherited"), (
                "an inherited entry must never be re-PUT — that would convert it into a "
                f"permanent direct grant: {entry!r}"
            )
        self.calls.append(
            (
                "set_app_permissions",
                app_name,
                tuple(sorted((e["principal"], e["kind"], e["level"]) for e in entries)),
            )
        )
        self._maybe_raise("set_app_permissions")
        self.acl = [dict(entry) for entry in entries]

    async def create_service_principal(self, workspace_url, token, display_name):
        assert token.startswith("ws-token-"), "SCIM create needs a WORKSPACE token"
        assert display_name
        self.calls.append(("create_service_principal", display_name))
        self._maybe_raise("create_service_principal")
        self.sp_seq += 1
        return {
            "id": f"{SCIM_SP_ID}{self.sp_seq}",
            "application_id": f"sp-app-id-{self.sp_seq}",
        }

    async def create_service_principal_secret(self, workspace_url, token, sp_id):
        assert token.startswith("ws-token-"), "minting an SP secret needs a WORKSPACE token"
        assert sp_id.startswith(SCIM_SP_ID), (
            f"the secret mint must be addressed by the SCIM id, got {sp_id!r}"
        )
        self.calls.append(("create_service_principal_secret", sp_id))
        self._maybe_raise("create_service_principal_secret")
        return {"secret": MINTED_SP_SECRET, "secret_hash": "sha256:abc", "id": "secret-rec-1"}

    async def ensure_federation_audience(self, account_host, account_id, token, audience, *, present):
        assert token.startswith("account-token-"), (
            "the federation policy is an ACCOUNT-level resource — a workspace token cannot write it"
        )
        assert audience, "an empty audience would be appended as a trust entry"
        forms = [audience] if isinstance(audience, str) else list(audience)
        assert all(forms), "an empty audience would be appended as a trust entry"
        for form in forms:
            self.calls.append(("ensure_federation_audience", form, present))
        self._maybe_raise("ensure_federation_audience")
        for form in forms:
            if present:
                self.audiences.add(form)
            else:
                self.audiences.discard(form)


class _FakeSecretsExceptions:
    class ResourceNotFoundException(Exception):
        pass

    class ResourceExistsException(Exception):
        pass


class _FakeSecrets:
    """A Secrets Manager double that 404s on an unknown id, which is what proves the service reads
    the tenant's stored ARN rather than a name it made up. It resolves a name or an ARN, because
    the real API does: ``create_secret`` returns an ARN and every later call is made with it, so a
    fake that only answered to names would pass "store then read back" while the live path 404'd.
    """

    def __init__(self, store=None):
        self.exceptions = _FakeSecretsExceptions
        self.store: dict[str, str] = dict(store or {})
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.raise_on_delete = False
        self._arns: dict[str, str] = {k: k for k in self.store}

    def _resolve(self, secret_id: str) -> str:
        """A SecretId may be the name or the ARN. Returns the store key, or raises 404."""
        if secret_id in self.store:
            return secret_id
        for name, arn in self._arns.items():
            if arn == secret_id and name in self.store:
                return name
        raise self.exceptions.ResourceNotFoundException(secret_id)

    def get_secret_value(self, SecretId):
        key = self._resolve(SecretId)
        return {"SecretString": self.store[key], "ARN": self._arns.get(key, key)}

    def create_secret(self, Name, SecretString, Tags=None):
        if Name in self.store:
            raise self.exceptions.ResourceExistsException(Name)
        self.store[Name] = SecretString
        arn = f"arn:aws:secretsmanager:us-east-1:000000000000:secret:{Name}-AbC123"
        self._arns[Name] = arn
        self.created.append({"Name": Name, "Tags": Tags})
        return {"ARN": arn}

    def put_secret_value(self, SecretId, SecretString):
        key = self._resolve(SecretId)
        self.store[key] = SecretString
        return {"ARN": self._arns.get(key, key)}

    def delete_secret(self, SecretId, ForceDeleteWithoutRecovery=False):
        key = self._resolve(SecretId)
        if self.raise_on_delete:
            raise ClientError({"Error": {"Code": "InternalServiceError", "Message": "boom"}}, "DeleteSecret")
        self.store.pop(key)
        self.deleted.append(SecretId)


def _default_secret_store() -> dict:
    """The two tenant-level secrets the tenant service writes: the per-stage workspace SP
    credential, and the optional account-admin pair (both halves in one body)."""
    return {
        WS_SECRET_ARN: json.dumps({SP_SECRET_KEY: WS_SECRET_SENTINEL}),
        ADMIN_SECRET_ARN: json.dumps(
            {
                ACCOUNT_ADMIN_ID_KEY: ADMIN_CLIENT_ID,
                ACCOUNT_ADMIN_SECRET_KEY: ADMIN_SECRET_SENTINEL,
            }
        ),
    }


class _FakeTenants:
    """A one-tenant TenantService double.

    Needed only by the route-level teardown tests: the ROUTE calls
    ``delete_databricks_runtime(agent)`` with no tenant argument (it has none to pass), so the
    service resolves it through ``_resolve_tenant`` — the live shape. Every direct-call test
    keeps passing the tenant explicitly.
    """

    def __init__(self, tenant):
        self._tenant = tenant

    def get(self, tenant_id):
        return self._tenant if tenant_id == getattr(self._tenant, "id", None) else None


def _make_service(*, databricks=None, secrets=None, registry=None, tenants=None):
    from services.databricks_identity_service import DatabricksIdentityService

    databricks = databricks or _FakeDatabricks()
    secrets = secrets or _FakeSecrets(store=_default_secret_store())
    registry = registry or MagicMock()
    svc = DatabricksIdentityService(
        databricks=databricks,
        registry=registry,
        tenants=tenants,
        secrets_client=secrets,
        secret_prefix="agp-test/databricks-agent-sp/",
    )
    return svc, databricks, secrets, registry


def _persisted_statuses(registry) -> list[str]:
    return [c.args[0].identity_status for c in registry.persist_identity.call_args_list]


@pytest.fixture
def sp_secret_gate_on(monkeypatch):
    """The sp_secret leg is dormant behind an off-by-default flag; every test that exercises it
    opts in here, and the tests that assert the refusal do not take this fixture.

    Patched on the service module's ``settings`` object, not on a fresh
    ``from core.config import settings``: the ``reprovision_env`` fixture pops ``core.config`` out
    of ``sys.modules``, so a re-import here would hand back a different ``Settings`` instance than
    the one the service reads, and the flag would appear to have no effect."""
    import services.databricks_identity_service as dbx_module

    monkeypatch.setattr(dbx_module.settings, "DATABRICKS_ALLOW_SP_SECRET_BINDING", True)
    return dbx_module.settings


@pytest.mark.asyncio
async def test_federation_happy_path_adds_audience_asserts_the_acl_and_persists():
    svc, db, _secrets, registry = _make_service()
    agent = _make_agent()
    tenant = _make_tenant(entra_group_ids=[GROUP_A, GROUP_B])

    out = await svc.provision_databricks_runtime(agent, tenant)

    assert ("ensure_federation_audience", "agent-app-guid", True) in db.calls
    assert db.audiences == {"agent-app-guid"}
    assert not any(c == ("ensure_federation_audience", AGENT_AUDIENCE, True) for c in db.calls)
    assert ("set_app_permissions", APP_NAME, _asserted_acl()) in db.calls
    assert out.oauth2_app_client_id == APP_OAUTH_CLIENT_ID
    assert out.binding_mode == "federation"
    assert out.identity_status == "provisioned"
    assert _persisted_statuses(registry)[-1] == "provisioned"


@pytest.mark.asyncio
async def test_federation_never_writes_sp_fields():
    """Federation mode uses no per-agent Databricks SP, so an SP id/ARN on the record would
    be a credential path the invoke chain could pick up. Both are forced empty."""
    svc, db, _s, _r = _make_service()
    agent = _make_agent(databricks_sp_id="client-planted-sp", databricks_sp_secret_arn="arn:aws:planted")

    out = await svc.provision_databricks_runtime(agent, _make_tenant())

    assert out.databricks_sp_id is None
    assert out.databricks_sp_secret_arn is None
    assert not any(c[0] == "create_service_principal" for c in db.calls)


@pytest.mark.asyncio
async def test_ob2_client_supplied_binding_mode_is_overwritten_from_the_tenant():
    """``binding_mode`` is settable on ``AgentCreate``, so it arrives untrusted. The tenant's
    stored mode is the only truth: a caller claiming ``sp_secret`` on a federation tenant must not
    get the weaker attribution path."""
    svc, db, _s, _r = _make_service()
    agent = _make_agent(binding_mode="sp_secret")

    out = await svc.provision_databricks_runtime(agent, _make_tenant(binding_mode="federation"))

    assert out.binding_mode == "federation"
    assert any(c[0] == "ensure_federation_audience" for c in db.calls)
    assert not any(c[0] == "create_service_principal" for c in db.calls)


@pytest.mark.asyncio
async def test_ob2_client_supplied_federation_claim_cannot_skip_sp_mode(sp_secret_gate_on):
    """The mirror of the above: a caller claiming ``federation`` on an sp_secret tenant must not
    reach the account-level federation policy at all — it gets the sp_secret path it is due."""
    svc, db, _s, _r = _make_service()
    agent = _make_agent(binding_mode="federation")

    out = await svc.provision_databricks_runtime(agent, _make_tenant(binding_mode="sp_secret"))

    assert not any(c[0] == "ensure_federation_audience" for c in db.calls)
    assert out.binding_mode == "sp_secret"
    assert out.databricks_sp_id


@pytest.mark.asyncio
async def test_ob2_client_supplied_oauth_client_id_is_cleared_when_unconfirmed():
    """``oauth2_app_client_id`` is on ``AgentBase``, so it arrives client-settable. A provisioned
    record must not assert a Databricks OAuth client id that AGP never read from the workspace."""
    db = _FakeDatabricks(apps=[{"name": APP_NAME, "url": APP_URL}])
    svc, _db, _s, _r = _make_service(databricks=db)
    agent = _make_agent(oauth2_app_client_id="client-planted-oauth-id")

    out = await svc.provision_databricks_runtime(agent, _make_tenant())

    assert out.identity_status == "provisioned"
    assert out.oauth2_app_client_id is None


@pytest.mark.asyncio
async def test_ob2_the_sp_secret_leg_also_clears_a_planted_oauth_client_id(sp_secret_gate_on):
    """Only the federation leg reads this field back from a workspace listing, so on the
    sp_secret leg a caller's value would otherwise survive untouched onto a provisioned record."""
    svc, _db, _s, _r = _make_service()
    agent = _make_agent(oauth2_app_client_id="client-planted-oauth-id")

    out = await svc.provision_databricks_runtime(agent, _make_tenant(binding_mode="sp_secret"))

    assert out.identity_status == "provisioned"
    assert out.oauth2_app_client_id is None


@pytest.mark.asyncio
async def test_user_sync_missing_fails_loudly_and_never_downgrades():
    svc, db, _s, registry = _make_service()
    agent = _make_agent()
    tenant = _make_tenant(capabilities={"can_discover": True, "account_admin": True, "user_sync": False})

    with pytest.raises(ProvisioningError) as err:
        await svc.provision_databricks_runtime(agent, tenant)

    assert "user_sync_missing" in str(err.value)
    assert agent.identity_status == "failed"
    assert _persisted_statuses(registry)[-1] == "failed"
    assert agent.binding_mode == "federation"
    assert agent.databricks_sp_id is None
    assert db.calls == []


@pytest.mark.asyncio
async def test_unprobed_tenant_fails_closed_on_user_sync():
    """Capabilities absent = never probed. That is not evidence of sync, so it fails the
    same way an explicit False does (fail-closed, Global Constraints)."""
    svc, db, _s, _r = _make_service()

    with pytest.raises(ProvisioningError) as err:
        await svc.provision_databricks_runtime(_make_agent(), _make_tenant(capabilities={}))

    assert "user_sync_missing" in str(err.value)
    assert db.calls == []


@pytest.mark.parametrize(
    "kind",
    ["federation_policy_missing", "federation_policy_unreadable", "federation_policy_ambiguous"],
)
@pytest.mark.asyncio
async def test_ob6_federation_policy_errors_are_actionable(kind):
    db = _FakeDatabricks()
    db.raise_on["ensure_federation_audience"] = DatabricksError("safe upstream text", kind=kind)
    svc, _db, _s, registry = _make_service(databricks=db)
    agent = _make_agent()

    with pytest.raises(ProvisioningError) as err:
        await svc.provision_databricks_runtime(agent, _make_tenant())

    message = str(err.value)
    assert "Databricks account admin" in message
    assert "login.microsoftonline.com" in message
    assert kind in message
    assert agent.identity_status == "failed"
    assert _persisted_statuses(registry)[-1] == "failed"


@pytest.mark.asyncio
async def test_ob6_actionable_message_carries_no_upstream_body():
    db = _FakeDatabricks()
    db.raise_on["ensure_federation_audience"] = DatabricksError(
        "PERMISSION_DENIED: principal 42 lacks access to /Workspace/secret/path",
        kind="federation_policy_missing",
    )
    svc, _db, _s, _r = _make_service(databricks=db)

    with pytest.raises(ProvisioningError) as err:
        await svc.provision_databricks_runtime(_make_agent(), _make_tenant())

    assert "/Workspace/secret/path" not in str(err.value)
    assert "PERMISSION_DENIED" not in str(err.value)


@pytest.mark.asyncio
async def test_the_tenants_entra_groups_are_never_granted_can_use():
    svc, db, _s, _r = _make_service()

    await svc.provision_databricks_runtime(_make_agent(), _make_tenant(entra_group_ids=[GROUP_A, GROUP_B]))

    assert not any(c[0] == "grant_app_can_use" for c in db.calls)
    asserted = [c for c in db.calls if c[0] == "set_app_permissions"]
    assert asserted == [("set_app_permissions", APP_NAME, _asserted_acl())]
    assert not any(GROUP_A in entry or GROUP_B in entry for entry in asserted[0][2])


@pytest.mark.asyncio
async def test_the_assert_strips_pre_existing_entries_and_logs_each_by_principal(caplog):
    """A takeover is loud, never silent. The stripped entries are the record of what a workspace
    owner had granted around AGP, so each one is named."""
    db = _FakeDatabricks()
    db.acl = [
        {"principal": "admins", "kind": "group", "level": "CAN_MANAGE"},
        {"principal": "hand.granted@example.com", "kind": "user", "level": "CAN_USE"},
        {"principal": "data-eng", "kind": "group", "level": "CAN_MANAGE"},
    ]
    svc, _db, _s, _r = _make_service(databricks=db)

    with caplog.at_level(logging.WARNING):
        out = await svc.provision_databricks_runtime(_make_agent(), _make_tenant())

    assert out.identity_status == "provisioned"
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "hand.granted@example.com" in text
    assert "data-eng" in text
    assert "CAN_USE" in text and "user" in text
    assert "stripped 2" in text
    assert db.acl == [
        {"principal": "admins", "kind": "group", "level": "CAN_MANAGE"},
        {"principal": WS_SP_CLIENT_ID, "kind": "service_principal", "level": "CAN_MANAGE"},
    ]
    assert WS_SECRET_SENTINEL not in text
    assert ADMIN_SECRET_SENTINEL not in text


@pytest.mark.asyncio
async def test_an_inherited_entry_is_not_reported_as_stripped(caplog):
    """A PUT cannot remove an inherited grant, so calling one "stripped" would claim a takeover
    that did not happen. It is reported as what it is: access that survives the assert."""
    db = _FakeDatabricks()
    db.acl = [
        {"principal": "admins", "kind": "group", "level": "CAN_MANAGE", "inherited": False},
        {"principal": "workspace-users", "kind": "group", "level": "CAN_USE", "inherited": True},
    ]
    svc, _db, _s, _r = _make_service(databricks=db)

    with caplog.at_level(logging.WARNING):
        await svc.provision_databricks_runtime(_make_agent(), _make_tenant())

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "stripped" not in text
    assert "inherited entries survive" in text


@pytest.mark.asyncio
async def test_an_already_asserted_app_logs_no_stripped_entries(caplog):
    """Re-provisioning an app AGP already owns is a no-op takeover — it must not manufacture a
    warning about entries it put there itself."""
    db = _FakeDatabricks()
    db.acl = [
        {"principal": "admins", "kind": "group", "level": "CAN_MANAGE"},
        {"principal": WS_SP_CLIENT_ID, "kind": "service_principal", "level": "CAN_MANAGE"},
    ]
    svc, _db, _s, _r = _make_service(databricks=db)

    with caplog.at_level(logging.WARNING):
        await svc.provision_databricks_runtime(_make_agent(), _make_tenant())

    assert not any("stripped" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_an_unreadable_acl_is_an_actionable_failure_not_a_blind_assert():
    """AGP will not replace a list it could not read: the stripped entries are the only record
    of what was taken over, so a blind PUT would delete customer grants silently."""
    db = _FakeDatabricks()
    db.raise_on["get_app_permissions"] = DatabricksError("nope", kind="forbidden")
    svc, _db, _s, registry = _make_service(databricks=db)
    agent = _make_agent()

    with pytest.raises(ProvisioningError) as err:
        await svc.provision_databricks_runtime(agent, _make_tenant())

    assert "CAN_MANAGE on the app" in str(err.value)
    assert "forbidden" in str(err.value)
    assert not any(c[0] == "set_app_permissions" for c in db.calls)
    assert agent.identity_status == "failed"
    assert _persisted_statuses(registry)[-1] == "failed"


@pytest.mark.asyncio
async def test_a_failed_acl_assert_fails_provisioning_loudly():
    """No silent skip: an agent reported provisioned whose ACL AGP does not own is a
    governance claim nothing supports."""
    db = _FakeDatabricks()
    db.raise_on["set_app_permissions"] = DatabricksError("rejected", kind="forbidden")
    svc, _db, _s, registry = _make_service(databricks=db)
    agent = _make_agent()

    with pytest.raises(ProvisioningError) as err:
        await svc.provision_databricks_runtime(agent, _make_tenant())

    assert "access list" in str(err.value)
    assert "forbidden" in str(err.value)
    assert agent.identity_status == "failed"
    assert _persisted_statuses(registry)[-1] == "failed"


@pytest.mark.asyncio
async def test_reprovision_after_a_mid_failure_completes_without_duplicating_anything():
    db = _FakeDatabricks()
    db.raise_on["set_app_permissions"] = DatabricksError("rejected", kind="forbidden")
    svc, _db, _s, _registry = _make_service(databricks=db)
    agent = _make_agent()
    tenant = _make_tenant()

    with pytest.raises(ProvisioningError):
        await svc.provision_databricks_runtime(agent, tenant)
    assert agent.identity_status == "failed"
    assert db.audiences == {"agent-app-guid"}

    db.raise_on.pop("set_app_permissions")
    out = await svc.provision_databricks_runtime(agent, tenant)

    assert out.identity_status == "provisioned"
    assert db.audiences == {"agent-app-guid"}
    assert not any(c[0] == "create_service_principal" for c in db.calls)


@pytest.mark.asyncio
async def test_app_missing_from_the_listing_is_an_actionable_failure():
    """The app NAME is read from the workspace listing, never derived from the URL. When the
    handle no longer resolves, provisioning refuses rather than PATCHing a guessed name."""
    db = _FakeDatabricks(apps=[{"name": "someone-elses-app", "url": "https://other.example"}])
    svc, _db, _s, _r = _make_service(databricks=db)

    with pytest.raises(ProvisioningError) as err:
        await svc.provision_databricks_runtime(_make_agent(), _make_tenant())

    assert "no Databricks workspace on this tenant lists" in str(err.value)
    assert not any(c[0] == "set_app_permissions" for c in db.calls)


@pytest.mark.asyncio
async def test_unreadable_oauth_client_id_does_not_fail_provisioning():
    """``oauth2_app_client_id`` is an unverified field name. Missing it is a gap in a convenience
    field, not a reason to leave the agent unprovisioned."""
    db = _FakeDatabricks(apps=[{"name": APP_NAME, "url": APP_URL}])
    svc, _db, _s, _r = _make_service(databricks=db)

    out = await svc.provision_databricks_runtime(_make_agent(), _make_tenant())

    assert out.identity_status == "provisioned"
    assert out.oauth2_app_client_id is None


@pytest.mark.asyncio
async def test_serving_endpoint_kind_is_refused_not_silently_ungranted():
    svc, db, _s, _r = _make_service()
    agent = _make_agent(runtime_kind="serving_endpoint")

    with pytest.raises(ProvisioningError) as err:
        await svc.provision_databricks_runtime(agent, _make_tenant())

    assert "serving_endpoint" in str(err.value)
    assert not any(c[0] == "set_app_permissions" for c in db.calls)


@pytest.mark.asyncio
async def test_sp_secret_creates_an_sp_mints_its_secret_stores_it_and_asserts_the_acl(
    sp_secret_gate_on,
):
    svc, db, _secrets, registry = _make_service()
    agent = _make_agent()
    tenant = _make_tenant(binding_mode="sp_secret")

    out = await svc.provision_databricks_runtime(agent, tenant)

    assert [c for c in db.calls if c[0] == "create_service_principal"] == [
        ("create_service_principal", "agp-agent-rec-abc123")
    ]
    assert out.databricks_sp_id == "sp-app-id-1"
    assert any(c[0] == "create_service_principal_secret" for c in db.calls)
    assert out.databricks_sp_secret_arn
    assert MINTED_SP_SECRET not in json.dumps(out.to_envelope(), default=str)
    assert (
        "set_app_permissions",
        APP_NAME,
        _asserted_acl(("sp-app-id-1", "service_principal", "CAN_USE")),
    ) in db.calls
    assert not any(c[0] == "grant_app_can_use" for c in db.calls)
    assert not any(c[0] == "ensure_federation_audience" for c in db.calls)
    assert out.binding_mode == "sp_secret"
    assert out.identity_status == "provisioned"
    assert _persisted_statuses(registry)[-1] == "provisioned"


@pytest.mark.asyncio
async def test_the_scim_id_rides_in_the_secret_body_not_on_the_agent_record(sp_secret_gate_on):
    """Minting needs the SCIM id; the record carries the application id. Rather than widen the
    client-settable model with a seventh field, the SCIM id lives in the secret body."""
    svc, _db, secrets, _r = _make_service()
    agent = _make_agent()

    out = await svc.provision_databricks_runtime(agent, _make_tenant(binding_mode="sp_secret"))

    body = json.loads(secrets.store[secrets.created[0]["Name"]])
    assert body["scim_id"] == f"{SCIM_SP_ID}1"
    assert body["client_secret"] == MINTED_SP_SECRET
    envelope = out.to_envelope()
    assert f"{SCIM_SP_ID}1" not in json.dumps(envelope, default=str)
    assert envelope["databricks_sp_id"] == "sp-app-id-1"


@pytest.mark.asyncio
async def test_sp_secret_reprovision_mints_nothing_new(sp_secret_gate_on):
    """A record already naming an SP and a stored secret must not mint a second credential —
    that would leave the previous one live and unreferenced."""
    svc, db, _s, _r = _make_service()
    agent = _make_agent()
    tenant = _make_tenant(binding_mode="sp_secret")
    await svc.provision_databricks_runtime(agent, tenant)
    first_arn = agent.databricks_sp_secret_arn

    await svc.provision_databricks_runtime(agent, tenant)

    assert len([c for c in db.calls if c[0] == "create_service_principal"]) == 1
    assert len([c for c in db.calls if c[0] == "create_service_principal_secret"]) == 1
    assert agent.databricks_sp_secret_arn == first_arn


@pytest.mark.asyncio
async def test_a_failed_mint_leaves_the_sp_recorded_so_a_retry_creates_no_second_one(
    sp_secret_gate_on,
):
    db = _FakeDatabricks()
    db.raise_on["create_service_principal_secret"] = DatabricksError("no", kind="forbidden")
    svc, _db, _s, registry = _make_service(databricks=db)
    agent = _make_agent()
    tenant = _make_tenant(binding_mode="sp_secret")

    with pytest.raises(ProvisioningError):
        await svc.provision_databricks_runtime(agent, tenant)

    assert agent.databricks_sp_id == "sp-app-id-1"
    assert "sp-app-id-1" in [c.args[0].databricks_sp_id for c in registry.persist_identity.call_args_list]
    assert agent.identity_status == "failed"

    db.raise_on.pop("create_service_principal_secret")
    out = await svc.provision_databricks_runtime(agent, tenant)
    assert out.identity_status == "provisioned"
    assert len([c for c in db.calls if c[0] == "create_service_principal"]) == 1


@pytest.mark.asyncio
async def test_a_persist_failure_creating_the_sp_entry_deletes_the_orphan(sp_secret_gate_on):
    """The FIRST persist writes both pointers. If it fails, the secret entry it just created is
    orphaned — live and unreferenced — so it is removed (the ``connection_service`` idiom). Safe
    because it holds no credential yet, only the SCIM id."""
    registry = MagicMock()
    registry.persist_identity.side_effect = RuntimeError("ddb conflict")
    svc, _db, secrets, _r = _make_service(registry=registry)
    agent = _make_agent()

    with pytest.raises(ProvisioningError):
        await svc.provision_databricks_runtime(agent, _make_tenant(binding_mode="sp_secret"))

    assert secrets.deleted, "the orphaned entry should have been removed"
    assert agent.databricks_sp_secret_arn is None


@pytest.mark.asyncio
async def test_a_persist_failure_after_the_mint_keeps_the_secret_so_a_retry_can_work(
    sp_secret_gate_on,
):
    """The OPPOSITE of the above, and deliberately so. The mint UPDATES the entry step 1 already
    persisted, so the record already points at it — nothing is orphaned. Deleting it would
    destroy the stored SCIM id, turning a retryable failure into a permanent one."""
    registry = MagicMock()
    calls = {"n": 0}

    def _persist(agent):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("ddb conflict")
        return agent

    registry.persist_identity.side_effect = _persist
    svc, db, secrets, _r = _make_service(registry=registry)
    agent = _make_agent()
    tenant = _make_tenant(binding_mode="sp_secret")

    with pytest.raises(ProvisioningError):
        await svc.provision_databricks_runtime(agent, tenant)

    assert secrets.deleted == [], "the recorded credential must NOT be deleted"
    assert agent.identity_status == "failed"

    registry.persist_identity.side_effect = lambda a: a
    out = await svc.provision_databricks_runtime(agent, tenant)
    assert out.identity_status == "provisioned"
    assert len([c for c in db.calls if c[0] == "create_service_principal"]) == 1
    assert len([c for c in db.calls if c[0] == "create_service_principal_secret"]) == 1


@pytest.mark.asyncio
async def test_the_minted_secret_never_reaches_a_log_or_the_record(caplog, sp_secret_gate_on):
    svc, _db, _s, _r = _make_service()
    agent = _make_agent()

    with caplog.at_level(logging.DEBUG):
        out = await svc.provision_databricks_runtime(agent, _make_tenant(binding_mode="sp_secret"))

    assert MINTED_SP_SECRET not in caplog.text
    assert MINTED_SP_SECRET not in json.dumps(out.to_envelope(), default=str)


@pytest.mark.asyncio
async def test_unknown_binding_mode_is_refused():
    svc, db, _s, _r = _make_service()

    with pytest.raises(ProvisioningError) as err:
        await svc.provision_databricks_runtime(_make_agent(), _make_tenant(binding_mode=""))

    assert "binding mode" in str(err.value).lower()
    assert db.calls == []


@pytest.mark.asyncio
async def test_invoke_unavailable_is_refused_naming_what_federation_needs():
    """The tenant could not be badged ``federation``, so there is nothing to provision — and the
    refusal must say WHICH grant is missing. A bare "no usable binding mode" would send an
    operator looking at the agent when the fix is on their Databricks account."""
    svc, db, _s, registry = _make_service()

    with pytest.raises(ProvisioningError) as err:
        await svc.provision_databricks_runtime(_make_agent(), _make_tenant(binding_mode="invoke_unavailable"))

    msg = str(err.value)
    assert "federation_unavailable" in msg
    assert "account-admin" in msg and "user sync" in msg
    assert db.calls == []
    assert _persisted_statuses(registry) == ["failed"]


@pytest.mark.asyncio
async def test_sp_secret_is_refused_naming_the_flag_when_the_gate_is_off():
    """DEFAULT DEPLOYMENT. A record deliberately carrying the dormant mode does not provision
    silently and does not fall through to federation either: it is refused, naming the flag, so
    the operator sees a decision rather than a mystery."""
    svc, db, _s, _r = _make_service()

    with pytest.raises(ProvisioningError) as err:
        await svc.provision_databricks_runtime(_make_agent(), _make_tenant(binding_mode="sp_secret"))

    msg = str(err.value)
    assert "sp_secret_disabled" in msg
    assert "DATABRICKS_ALLOW_SP_SECRET_BINDING" in msg
    assert not any(c[0] == "create_service_principal" for c in db.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke_unavailable", "sp_secret"])
async def test_a_refused_provision_never_persists_client_planted_sp_pointers(mode):
    """Both refusals raise, and ``provision_databricks_runtime`` turns that into ``_persist_failed``,
    an envelope write of the whole record. So a planted ``databricks_sp_id`` and foreign ARN must
    be scrubbed before the dispatch, or the failed record keeps naming another agent's principal
    and the teardown residue log names it too."""
    svc, _db, _s, registry = _make_service()
    agent = _make_agent(
        databricks_sp_id="foreign-sp-id",
        databricks_sp_secret_arn=_agent_secret_arn("rec-some-other-agent"),
    )

    with pytest.raises(ProvisioningError):
        await svc.provision_databricks_runtime(agent, _make_tenant(binding_mode=mode))

    persisted = registry.persist_identity.call_args.args[0]
    assert persisted.identity_status == "failed"
    assert persisted.databricks_sp_id is None
    assert persisted.databricks_sp_secret_arn is None
    envelope = persisted.to_envelope()
    assert not envelope.get("databricks_sp_id")
    assert not envelope.get("databricks_sp_secret_arn")


@pytest.mark.asyncio
async def test_the_dormant_sp_secret_leg_still_works_when_the_gate_is_on(sp_secret_gate_on):
    """The capability is dormant, not deleted — the gate is the only thing standing between a
    deliberate record and the unchanged leg."""
    svc, db, _s, _r = _make_service()

    out = await svc.provision_databricks_runtime(_make_agent(), _make_tenant(binding_mode="sp_secret"))

    assert out.identity_status == "provisioned"
    assert out.databricks_sp_id == "sp-app-id-1"
    assert any(c[0] == "create_service_principal" for c in db.calls)


@pytest.mark.asyncio
async def test_workspace_secret_never_reaches_the_record_a_log_or_an_error(caplog):
    db = _FakeDatabricks()
    db.raise_on["ensure_federation_audience"] = DatabricksError("safe", kind="federation_policy_missing")
    svc, _db, _s, _r = _make_service(databricks=db)
    agent = _make_agent()

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ProvisioningError) as err:
            await svc.provision_databricks_runtime(agent, _make_tenant())

    assert WS_SECRET_SENTINEL not in str(err.value)
    assert WS_SECRET_SENTINEL not in caplog.text
    assert WS_SECRET_SENTINEL not in json.dumps(agent.to_envelope(), default=str)


@pytest.mark.asyncio
async def test_the_secret_is_read_by_the_tenants_stored_arn_only():
    """A fake that 404s an unknown id is what makes this assertion real: if the service
    built a secret NAME from a prefix instead of using the stored ARN, this fails."""
    secrets = _FakeSecrets(store={"some/other/name": json.dumps({"sp_client_secret": "x"})})
    svc, db, _s, _r = _make_service(secrets=secrets)

    with pytest.raises(ProvisioningError) as err:
        await svc.provision_databricks_runtime(_make_agent(), _make_tenant())

    assert "secret" in str(err.value).lower()
    assert db.calls == []


@pytest.mark.asyncio
async def test_a_raw_string_workspace_secret_is_accepted():
    """The tenant service writes a JSON envelope, but a secret rotated by hand in the console is a
    bare string. Refusing that would turn a console-side fix into an outage, so both are read."""
    store = _default_secret_store()
    store[WS_SECRET_ARN] = WS_SECRET_SENTINEL
    svc, _db, _s, _r = _make_service(secrets=_FakeSecrets(store=store))

    out = await svc.provision_databricks_runtime(_make_agent(), _make_tenant(account_admin_secret_arn=""))

    assert out.identity_status == "provisioned"


@pytest.mark.asyncio
async def test_a_non_databricks_agent_is_refused():
    svc, db, _s, _r = _make_service()
    agent = _make_agent(platform=Platform.AWS_BEDROCK, runtime_handle=None)

    with pytest.raises(ProvisioningError):
        await svc.provision_databricks_runtime(agent, _make_tenant())

    assert db.calls == []


@pytest.mark.asyncio
async def test_a_tenant_can_be_resolved_from_the_agents_tenant_id():
    tenants = MagicMock()
    tenants.get.return_value = _make_tenant()
    svc, _db, _s, _r = _make_service(tenants=tenants)

    out = await svc.provision_databricks_runtime(_make_agent(), None)

    tenants.get.assert_called_once_with("ten-1")
    assert out.identity_status == "provisioned"


@pytest.mark.asyncio
async def test_an_agent_with_no_tenant_cannot_be_provisioned():
    tenants = MagicMock()
    svc, db, _s, _r = _make_service(tenants=tenants)

    with pytest.raises(ProvisioningError) as err:
        await svc.provision_databricks_runtime(_make_agent(tenant_id=None), None)

    assert "tenant" in str(err.value).lower()
    assert db.calls == []


@pytest.mark.asyncio
async def test_the_stage_is_the_workspace_that_actually_lists_the_app_not_the_first_one():
    """A tenant with two workspaces must not have its ACCOUNT-ADMIN token aimed at the wrong
    one — that token writes account-level trust state.

    A Databricks app URL is ``<app>-<n>.<region>.databricksapps.com``: it carries no
    workspace identity, so the handle CANNOT be matched against ``workspace_url``. The only
    honest evidence is which workspace answers with the app, so that is what selects the
    stage — and it is the same listing the app name is read from, so it costs nothing extra.
    """
    other_url = "https://dbc-other.cloud.databricks.com"
    other = DatabricksStageConfig(
        workspace_url=other_url,
        account_id="99999999-9999-9999-9999-999999999999",
        sp_client_id="other-sp",
        sp_client_secret_arn="arn:aws:secretsmanager:us-east-1:000000000000:secret:other",
    )
    mine = DatabricksStageConfig(
        workspace_url=WORKSPACE_URL,
        account_id=ACCOUNT_ID,
        sp_client_id="ws-sp-client-id",
        sp_client_secret_arn=WS_SECRET_ARN,
    )
    tenant = _make_tenant(stages={"prod": other, "dev": mine})
    db = _FakeDatabricks(
        apps_by_workspace={
            other_url: [{"name": "someone-elses-app", "url": "https://other.example"}],
            WORKSPACE_URL: [{"name": APP_NAME, "url": APP_URL, "oauth2_app_client_id": APP_OAUTH_CLIENT_ID}],
        }
    )
    store = _default_secret_store()
    store["arn:aws:secretsmanager:us-east-1:000000000000:secret:other"] = json.dumps(
        {SP_SECRET_KEY: "other-secret"}
    )
    secrets = _FakeSecrets(store=store)
    svc, _db, _s, _r = _make_service(databricks=db, secrets=secrets)

    out = await svc.provision_databricks_runtime(_make_agent(), tenant)

    assert out.identity_status == "provisioned"
    assert ("mint_account_token", ACCOUNT_ID) in db.calls
    assert ("mint_account_token", "99999999-9999-9999-9999-999999999999") not in db.calls


@pytest.mark.asyncio
async def test_federation_without_an_account_id_is_refused():
    stage = DatabricksStageConfig(
        workspace_url=WORKSPACE_URL,
        account_id="",
        sp_client_id="ws-sp-client-id",
        sp_client_secret_arn=WS_SECRET_ARN,
    )
    svc, db, _s, _r = _make_service()

    with pytest.raises(ProvisioningError) as err:
        await svc.provision_databricks_runtime(_make_agent(), _make_tenant(stages={"dev": stage}))

    assert "account id" in str(err.value).lower()
    assert not any(c[0] == "ensure_federation_audience" for c in db.calls)


@pytest.mark.asyncio
async def test_the_account_admin_credential_is_preferred_over_the_workspace_sp():
    """Writing a federation policy is an account-admin act, and the tenant stores exactly that
    credential at tenant level. Silently using the workspace SP when an account-admin credential
    exists would make a correctly configured tenant fail for no visible reason."""
    svc, db, _s, _r = _make_service()

    out = await svc.provision_databricks_runtime(_make_agent(), _make_tenant())

    assert out.identity_status == "provisioned"
    assert db.account_credentials == [(ADMIN_CLIENT_ID, ADMIN_SECRET_SENTINEL)]
    assert db.minted_ws_tokens >= 1


@pytest.mark.asyncio
async def test_without_an_account_admin_credential_the_workspace_sp_is_the_fallback():
    """Federation is an extra grant a customer may not have made. Falling back is what turns a
    missing optional credential into one actionable Databricks error instead of a crash."""
    svc, db, _s, _r = _make_service()

    await svc.provision_databricks_runtime(_make_agent(), _make_tenant(account_admin_secret_arn=""))

    assert db.account_credentials == [("ws-sp-client-id", WS_SECRET_SENTINEL)]


@pytest.mark.asyncio
async def test_a_half_filled_account_admin_credential_is_refused_not_silently_skipped():
    store = _default_secret_store()
    store[ADMIN_SECRET_ARN] = json.dumps({ACCOUNT_ADMIN_ID_KEY: ADMIN_CLIENT_ID})
    svc, db, _s, _r = _make_service(secrets=_FakeSecrets(store=store))

    with pytest.raises(ProvisioningError) as err:
        await svc.provision_databricks_runtime(_make_agent(), _make_tenant())

    assert "incomplete" in str(err.value)
    assert db.account_credentials == []


@pytest.mark.asyncio
async def test_the_account_admin_secret_never_reaches_a_log_or_an_error(caplog):
    db = _FakeDatabricks()
    db.raise_on["mint_account_token"] = DatabricksError("safe", kind="unauthorized")
    svc, _db, _s, _r = _make_service(databricks=db)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ProvisioningError) as err:
            await svc.provision_databricks_runtime(_make_agent(), _make_tenant())

    assert ADMIN_SECRET_SENTINEL not in caplog.text
    assert ADMIN_SECRET_SENTINEL not in str(err.value)


@pytest.mark.asyncio
async def test_the_account_host_follows_the_stages_cloud():
    """The account console host is per cloud and not derivable from the workspace URL, so an
    Azure tenant's account-level call must not be aimed at the AWS console."""
    azure_stage = DatabricksStageConfig(
        workspace_url="https://adb-123.4.azuredatabricks.net",
        cloud="azure",
        account_id=ACCOUNT_ID,
        sp_client_id="ws-sp-client-id",
        sp_client_secret_arn=WS_SECRET_ARN,
    )
    db = _FakeDatabricks(
        apps_by_workspace={
            "https://adb-123.4.azuredatabricks.net": [
                {"name": APP_NAME, "url": APP_URL, "oauth2_app_client_id": APP_OAUTH_CLIENT_ID}
            ]
        }
    )
    svc, _db, _s, _r = _make_service(databricks=db)

    out = await svc.provision_databricks_runtime(_make_agent(), _make_tenant(stages={"dev": azure_stage}))

    assert out.identity_status == "provisioned"
    assert db.account_hosts == ["https://accounts.azuredatabricks.net"]


@pytest.mark.asyncio
async def test_an_aws_tenant_uses_the_aws_account_console():
    svc, db, _s, _r = _make_service()

    await svc.provision_databricks_runtime(_make_agent(), _make_tenant())

    assert db.account_hosts == ["https://accounts.cloud.databricks.com"]


@pytest.mark.asyncio
async def test_teardown_of_an_invoke_unavailable_agent_is_a_graceful_no_op():
    """A tenant that could never be provisioned still copies its mode onto the agent, so
    ``binding_mode`` alone is truthy and the record enters the teardown path. Nothing exists to
    remove, so this must succeed silently rather than fail a delete cascade."""
    svc, db, secrets, _r = _make_service()
    agent = _make_agent(binding_mode="invoke_unavailable")

    await svc.delete_databricks_runtime(agent, _make_tenant(binding_mode="invoke_unavailable"))

    assert db.calls == []
    assert secrets.deleted == []


@pytest.mark.asyncio
async def test_deprovision_federation_removes_the_audience():
    svc, db, _s, _r = _make_service()
    agent = _make_agent(binding_mode="federation")
    tenant = _make_tenant()
    await svc.provision_databricks_runtime(agent, tenant)

    await svc.delete_databricks_runtime(agent, tenant)

    assert ("ensure_federation_audience", "agent-app-guid", False) in db.calls
    assert ("ensure_federation_audience", AGENT_AUDIENCE, False) in db.calls
    assert db.audiences == set()


@pytest.mark.asyncio
async def test_deprovision_sp_secret_deletes_the_secret_by_its_stored_arn():
    """Deleted by the stored ARN, never by a reconstructed name: the ARN is what the record holds,
    and rebuilding a name from a prefix would silently miss (or hit the wrong) secret if the prefix
    ever changed. Deliberately takes no ``sp_secret_gate_on`` fixture: teardown is never gated,
    because leaving a live credential behind over a flag would be the harm the flag prevents."""
    agent_secret_arn = (
        "arn:aws:secretsmanager:us-east-1:000000000000:secret:agp-test/databricks-agent-sp/rec-abc123-QrStUv"
    )
    secrets = _FakeSecrets(
        store={
            WS_SECRET_ARN: json.dumps({SP_SECRET_KEY: WS_SECRET_SENTINEL}),
            agent_secret_arn: json.dumps({"client_secret": "per-agent"}),
        }
    )
    svc, db, _s, _r = _make_service(secrets=secrets)
    agent = _make_agent(
        binding_mode="sp_secret",
        databricks_sp_id="sp-app-id-1",
        databricks_sp_secret_arn=agent_secret_arn,
    )
    tenant = _make_tenant(binding_mode="sp_secret")

    await svc.delete_databricks_runtime(agent, tenant)
    assert secrets.deleted == [agent_secret_arn]
    assert not any(c[0] == "ensure_federation_audience" for c in db.calls)

    await svc.delete_databricks_runtime(agent, tenant)


@pytest.mark.asyncio
async def test_deprovision_of_a_never_provisioned_agent_is_a_no_op():
    svc, db, _s, _r = _make_service()
    agent = _make_agent(binding_mode=None, entra_app_audience=None)

    await svc.delete_databricks_runtime(agent, _make_tenant())

    assert db.calls == []


@pytest.mark.asyncio
async def test_deprovision_attempts_every_item_then_raises():
    db = _FakeDatabricks()
    svc, _db, _s, _r = _make_service(databricks=db)
    agent = _make_agent(binding_mode="federation")
    tenant = _make_tenant()
    await svc.provision_databricks_runtime(agent, tenant)
    db.raise_on["ensure_federation_audience"] = DatabricksError("nope", kind="forbidden")

    with pytest.raises(ProvisioningError) as err:
        await svc.delete_databricks_runtime(agent, tenant)

    assert "audience" in str(err.value).lower()


def _reprovision_client(agent):
    """A minimal app carrying only the agents router, with both resolver singletons and the
    registry seeded — the ``test_agent_project_gating.py`` idiom (no dependency_overrides)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import api.routes.agents as agents_module
    import api.routes.projects as projects_module
    import api.routes.tenants as tenants_module
    import api.routes.users as users_module
    from core.errors import install_error_handlers
    from services.project_resolver import ProjectContext
    from services.tenant_resolver import TenantContext

    class _TR:
        async def resolve(self, principal):
            return TenantContext(is_global=True, tenant_ids=frozenset(), tenants=())

    class _PR:
        def __init__(self):
            self.invalidate = MagicMock()

        async def resolve(self, principal):
            return ProjectContext(is_global=True, roles={})

    svc = MagicMock()
    svc.get.return_value = agent
    svc.persist_identity.return_value = None
    agents_module._svc = svc
    users_module._tenant_resolver = _TR()
    users_module._project_resolver = _PR()
    projects_module._role_svc = MagicMock()
    tenants_module._svc = MagicMock()

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(agents_module.router, prefix="/api/v1")
    client = TestClient(app, headers={"Authorization": "Bearer fake"})
    return client, svc


@pytest.fixture
def reprovision_env(monkeypatch):
    import sys

    for mod in [
        "core.rbac",
        "core.security_entra",
        "core.config",
        "api.routes.agents",
        "api.routes.project_repos",
        "api.routes.projects",
        "api.routes.repositories",
        "api.routes.users",
        "api.routes.tenants",
    ]:
        sys.modules.pop(mod, None)
    monkeypatch.setenv("AUTH_PROVIDER", "entra")
    monkeypatch.setenv("USE_DEV_AUTH", "False")
    monkeypatch.setenv("ENTRA_TENANT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("ENTRA_AUDIENCE", "api://agp")
    monkeypatch.setenv("ENTRA_ROLE_ADMIN", "Platform.Admin")
    monkeypatch.setenv("ENTRA_ROLE_OPERATOR", "Platform.Operator")
    monkeypatch.setenv("ENTRA_ROLE_VIEWER", "Platform.Viewer")
    claims = {
        "oid": "operator-oid",
        "preferred_username": "operator@x.com",
        "roles": ["Platform.Operator"],
    }
    patcher = patch("core.security_entra.verify_entra_token", return_value=claims)
    patcher.start()
    yield
    patcher.stop()
    for mod in [
        "api.routes.agents",
        "api.routes.project_repos",
        "api.routes.projects",
        "api.routes.repositories",
        "api.routes.users",
        "api.routes.tenants",
    ]:
        sys.modules.pop(mod, None)


def test_reprovision_accepts_a_databricks_governed_agent(reprovision_env):
    """The route used to 409 every non-AgentCore agent, so a Databricks agent stuck at 'failed'
    had no recovery affordance and the resumable design was unreachable."""
    agent = _make_agent(identity_status=IdentityStatus.FAILED)
    client, svc = _reprovision_client(agent)

    with patch("api.routes.agents.get_identity_service") as get_identity:
        get_identity.return_value = MagicMock()
        resp = client.post("/api/v1/agents/rec-abc123/reprovision")

    assert resp.status_code == 202
    assert svc.persist_identity.called
    assert svc.persist_identity.call_args.args[0].identity_status == "pending"


def test_reprovision_still_409s_a_metadata_only_agent(reprovision_env):
    agent = _make_agent(platform=None, runtime_handle=None, agent_arn=None)
    client, svc = _reprovision_client(agent)

    resp = client.post("/api/v1/agents/rec-abc123/reprovision")

    assert resp.status_code == 409
    svc.persist_identity.assert_not_called()


@pytest.mark.asyncio
async def test_teardown_keys_on_the_sp_id_so_partial_state_is_still_cleaned():
    """THE STATE THIS COVERS: sp_secret provisioning creates the SP first and stores its
    credential second, so a run that failed in between leaves a live service principal and no
    usable secret. Gating teardown on the secret ARN skipped exactly that case and left the
    principal behind forever."""
    svc, db, secrets, _r = _make_service()
    agent = _make_agent(
        binding_mode="sp_secret",
        databricks_sp_id="sp-app-id-1",
        databricks_sp_secret_arn=None,
    )

    await svc.delete_databricks_runtime(agent, _make_tenant(binding_mode="sp_secret"))

    assert secrets.deleted == []
    assert not any(c[0] == "ensure_federation_audience" for c in db.calls)


@pytest.mark.asyncio
async def test_teardown_reports_the_surviving_service_principal_by_id(caplog):
    svc, _db, _s, _r = _make_service()
    agent = _make_agent(binding_mode="sp_secret", databricks_sp_id="sp-app-id-1")

    with caplog.at_level(logging.INFO):
        await svc.delete_databricks_runtime(agent, _make_tenant(binding_mode="sp_secret"))

    assert "sp-app-id-1" in caplog.text
    assert "no SCIM delete" in caplog.text


def _wire_agents_module(monkeypatch, *, tenants_table: str):
    """The shared arrange: a route module with inert singletons and a given tenants table."""
    import api.routes.agents as agents_module
    import api.routes.tenants as tenants_module

    monkeypatch.setattr(agents_module.settings, "TENANTS_TABLE_NAME", tenants_table)
    monkeypatch.setattr(agents_module, "_identity_svc", None)
    monkeypatch.setattr(agents_module, "_databricks_identity_svc", None)
    monkeypatch.setattr(agents_module, "get_graph_service", lambda: MagicMock())
    monkeypatch.setattr(agents_module, "get_service", lambda: MagicMock())
    monkeypatch.setattr(tenants_module, "_svc", MagicMock())
    return agents_module


def test_the_identity_service_factory_wires_the_databricks_half(monkeypatch, reprovision_env):
    """The seam existed but nothing constructed the collaborator, so every live Databricks
    provisioning would have taken the fail-closed branch. Pinned so it cannot regress to that."""
    from models.tenant import TenantPlatform

    agents_module = _wire_agents_module(monkeypatch, tenants_table="agp-tenants")

    adapter = agents_module.get_adapters()[TenantPlatform.DATABRICKS]

    assert adapter._databricks_identity is not None
    assert type(adapter._databricks_identity).__name__ == "DatabricksIdentityService"


def test_an_agentcore_only_deployment_needs_no_databricks_config(monkeypatch, reprovision_env):
    """No tenants table ⇒ no Databricks wiring. NOT a silent skip: the platform is left OUT of
    the adapter dict, so a Databricks WRITE is refused with `platform_unsupported` (a persisted
    'failed', never a stranded 'pending') and a status READ degrades to `unknown`."""
    from models.tenant import TenantPlatform

    agents_module = _wire_agents_module(monkeypatch, tenants_table="")

    assert TenantPlatform.DATABRICKS not in agents_module.get_adapters()


def _agent_secret_arn(agent_id: str) -> str:
    """An ARN of the shape this service itself writes, for the given agent id."""
    return (
        f"arn:aws:secretsmanager:us-east-1:000000000000:secret:agp-test/databricks-agent-sp/{agent_id}-AbC123"
    )


@pytest.mark.asyncio
async def test_agent_b_cannot_provision_onto_agent_as_service_principal(sp_secret_gate_on):
    """THE ATTACK: register B carrying A's pointers. Before the fix, B reached 'provisioned' on
    A's principal — two governed agents sharing one Databricks identity, so B's calls are
    attributed to A and A's audit trail is no longer A's."""
    svc, db, secrets, _r = _make_service()
    tenant = _make_tenant(binding_mode="sp_secret")

    agent_a = _make_agent(agent_id="rec-agent-a")
    await svc.provision_databricks_runtime(agent_a, tenant)
    a_sp_id, a_arn = agent_a.databricks_sp_id, agent_a.databricks_sp_secret_arn

    agent_b = _make_agent(agent_id="rec-agent-b", databricks_sp_id=a_sp_id, databricks_sp_secret_arn=a_arn)
    await svc.provision_databricks_runtime(agent_b, tenant)

    assert agent_b.databricks_sp_id != a_sp_id, "B provisioned onto A's service principal"
    assert agent_b.databricks_sp_secret_arn != a_arn, "B is pointing at A's secret"
    assert len([c for c in db.calls if c[0] == "create_service_principal"]) == 2
    assert "agp-test/databricks-agent-sp/rec-agent-b" in secrets.store
    assert "agp-test/databricks-agent-sp/rec-agent-a" in secrets.store


@pytest.mark.asyncio
async def test_agent_bs_teardown_cannot_delete_agent_as_secret(sp_secret_gate_on):
    """THE SECOND HALF: even a NEVER-PROVISIONED B carrying A's ARN must not be able to delete
    A's credential through its own teardown."""
    svc, _db, secrets, _r = _make_service()
    tenant = _make_tenant(binding_mode="sp_secret")
    agent_a = _make_agent(agent_id="rec-agent-a")
    await svc.provision_databricks_runtime(agent_a, tenant)
    a_arn = agent_a.databricks_sp_secret_arn

    agent_b = _make_agent(
        agent_id="rec-agent-b",
        binding_mode="sp_secret",
        databricks_sp_id=agent_a.databricks_sp_id,
        databricks_sp_secret_arn=a_arn,
    )
    await svc.delete_databricks_runtime(agent_b, tenant)

    assert secrets.deleted == [], "B's teardown deleted a secret it does not own"
    assert "agp-test/databricks-agent-sp/rec-agent-a" in secrets.store


@pytest.mark.asyncio
async def test_agent_bs_teardown_cannot_delete_another_agents_secret_with_the_gate_off():
    """The same attack in the default configuration. The gate-on variant above needs the flag only
    because its setup provisions agent A; here B's record is constructed directly, so nothing
    provisions and the flag stays at its shipped default. Teardown is never gated, so the
    ownership guard, not the flag, is what must refuse the cross-agent delete."""
    import services.databricks_identity_service as dbx_module

    assert dbx_module.settings.DATABRICKS_ALLOW_SP_SECRET_BINDING is False

    a_arn = _agent_secret_arn("rec-agent-a")
    svc, _db, secrets, _r = _make_service(
        secrets=_FakeSecrets(store={**_default_secret_store(), a_arn: MINTED_SP_SECRET})
    )
    agent_b = _make_agent(
        agent_id="rec-agent-b",
        binding_mode="sp_secret",
        databricks_sp_id="rec-agent-a-sp-app-id",
        databricks_sp_secret_arn=a_arn,
    )

    await svc.delete_databricks_runtime(agent_b, _make_tenant(binding_mode="sp_secret"))

    assert secrets.deleted == [], "B's teardown deleted a secret it does not own"
    assert a_arn in secrets.store
    assert agent_b.databricks_sp_secret_arn is None
    assert agent_b.databricks_sp_id is None


@pytest.mark.asyncio
async def test_a_planted_tenant_credential_arn_is_refused_and_never_deleted(sp_secret_gate_on):
    """THE WORST CASE: the planted ARN is the TENANT's own workspace-SP secret. Provisioning
    refused even before the fix — but teardown deleted the tenant credential, breaking every
    agent on that tenant."""
    svc, _db, secrets, _r = _make_service()
    tenant = _make_tenant(binding_mode="sp_secret")
    agent = _make_agent(databricks_sp_id="whatever", databricks_sp_secret_arn=WS_SECRET_ARN)

    out = await svc.provision_databricks_runtime(agent, tenant)
    assert out.databricks_sp_secret_arn != WS_SECRET_ARN
    assert WS_SECRET_ARN in secrets.store

    planted = _make_agent(
        agent_id="rec-agent-c",
        binding_mode="sp_secret",
        databricks_sp_id="whatever",
        databricks_sp_secret_arn=WS_SECRET_ARN,
    )
    await svc.delete_databricks_runtime(planted, tenant)

    assert WS_SECRET_ARN in secrets.store, "the tenant's workspace credential was deleted"
    assert secrets.deleted == []


@pytest.mark.asyncio
async def test_an_agents_own_stored_arn_is_still_trusted_so_resumability_survives(
    sp_secret_gate_on,
):
    """The fix must not break what it protects: a record's OWN pointers are resumability state
    and must still short-circuit the mint."""
    svc, db, _s, _r = _make_service()
    agent = _make_agent()
    tenant = _make_tenant(binding_mode="sp_secret")
    await svc.provision_databricks_runtime(agent, tenant)

    await svc.provision_databricks_runtime(agent, tenant)

    assert len([c for c in db.calls if c[0] == "create_service_principal"]) == 1
    assert len([c for c in db.calls if c[0] == "create_service_principal_secret"]) == 1


def test_the_ownership_check_is_injective_and_prefix_independent():
    """Executed over hostile inputs. Ownership keys on the trailing ``/{agent.id}`` segment only,
    never on the configured prefix (a settable config value must not decide whether an agent
    recognises its own state) and never on ``-``-splitting (non-injective).
    """
    svc, _db, _s, _r = _make_service()
    agent = _make_agent(agent_id="rec-abc")

    assert svc._secrets.owns_secret_arn(agent, _agent_secret_arn("rec-abc"))
    assert svc._secrets.owns_secret_arn(
        agent, "arn:aws:secretsmanager:us-east-1:000000000000:secret:agp-test/databricks-agent-sp/rec-abc"
    )
    assert svc._secrets.owns_secret_arn(
        agent, "arn:aws:secretsmanager:us-east-1:000000000000:secret:totally/different/prefix/rec-abc-AbC123"
    )

    for hostile in (
        _agent_secret_arn("rec-abc123"),
        _agent_secret_arn("rec-ab"),
        _agent_secret_arn("../rec-abc"),
        _agent_secret_arn("./rec-abc"),
        WS_SECRET_ARN,
        "arn:aws:ssm:us-east-1:000000000000:parameter:agp/rec-abc",
        "rec-abc",
        "",
    ):
        assert not svc._secrets.owns_secret_arn(agent, hostile), hostile


def test_the_ownership_check_is_injective_for_dash_bearing_ids():
    """Item D, the exact reported pair. ``rsplit("-", 1)[0]`` turned ``rec-agent-a``'s name into
    ``rec-agent``, so id ``rec-agent`` adopted ``rec-agent-a``'s principal. Agent ids are
    AWS-generated recordIds, so "-" is NOT a field separator this code may assume."""
    svc, _db, _s, _r = _make_service()
    short = _make_agent(agent_id="rec-agent")
    longer = _make_agent(agent_id="rec-agent-a")

    assert svc._secrets.owns_secret_arn(short, _agent_secret_arn("rec-agent"))
    assert svc._secrets.owns_secret_arn(longer, _agent_secret_arn("rec-agent-a"))
    assert not svc._secrets.owns_secret_arn(short, _agent_secret_arn("rec-agent-a"))
    assert not svc._secrets.owns_secret_arn(longer, _agent_secret_arn("rec-agent"))


@pytest.mark.asyncio
async def test_a_prefix_change_does_not_orphan_an_agents_own_principal(sp_secret_gate_on):
    """Before ownership became prefix-independent, a prefix change made the agent stop recognising
    its own secret: re-provision minted a second service principal, the old secret stayed live and
    unreferenced, the old principal kept CAN_USE, and teardown refused to delete the agent's own
    secret and then dropped the pointer, so nothing named the leak."""
    from services.databricks_identity_service import DatabricksIdentityService

    db = _FakeDatabricks()
    secrets = _FakeSecrets(store=_default_secret_store())
    registry = MagicMock()
    tenant = _make_tenant(binding_mode="sp_secret")
    agent = _make_agent()

    old_svc = DatabricksIdentityService(
        databricks=db,
        registry=registry,
        secrets_client=secrets,
        secret_prefix="agp-old/databricks-agent-sp/",
    )
    await old_svc.provision_databricks_runtime(agent, tenant)
    original_arn = agent.databricks_sp_secret_arn
    original_sp = agent.databricks_sp_id

    new_svc = DatabricksIdentityService(
        databricks=db,
        registry=registry,
        secrets_client=secrets,
        secret_prefix="agp-new/databricks-agent-sp/",
    )
    await new_svc.provision_databricks_runtime(agent, tenant)

    assert len([c for c in db.calls if c[0] == "create_service_principal"]) == 1
    assert len([c for c in db.calls if c[0] == "create_service_principal_secret"]) == 1
    assert agent.databricks_sp_id == original_sp
    assert agent.databricks_sp_secret_arn == original_arn

    await new_svc.delete_databricks_runtime(agent, tenant)
    assert secrets.deleted == [original_arn]


@pytest.mark.asyncio
async def test_a_body_naming_another_owner_is_not_resumability_state(sp_secret_gate_on):
    """Defence in depth: the body carries the owning agent id, and the paths that ALREADY read the
    body verify it — so the authoritative check costs no extra API call.

    The outcome is a LOUD REFUSAL, not a silent re-mint. To reach this state the ARN must already
    have passed the name check (so a caller cannot plant it — that is
    ``test_agent_b_cannot_provision_onto_agent_as_service_principal``); a name-owned secret whose
    body names someone else means genuine corruption or id reuse, and minting a second credential
    for a principal AGP can no longer identify would leave the first live and unreferenced.
    """
    svc, db, secrets, _r = _make_service()
    tenant = _make_tenant(binding_mode="sp_secret")
    agent = _make_agent()
    await svc.provision_databricks_runtime(agent, tenant)

    assert agent.databricks_sp_secret_arn is not None
    key = secrets._resolve(agent.databricks_sp_secret_arn)
    body = json.loads(secrets.store[key])
    body["agent_id"] = "rec-someone-else"
    secrets.store[key] = json.dumps(body)

    with pytest.raises(ProvisioningError) as err:
        await svc.provision_databricks_runtime(agent, tenant)

    assert "SCIM id" in str(err.value)
    assert len([c for c in db.calls if c[0] == "create_service_principal_secret"]) == 1


@pytest.mark.asyncio
async def test_discarding_untrusted_pointers_never_logs_the_arn(caplog, sp_secret_gate_on):
    """The discarded ARN names another agent's — or the tenant's — secret, so it must not be
    written to a log line that a support bundle would carry."""
    svc, _db, _s, _r = _make_service()
    agent = _make_agent(databricks_sp_id="a-sp", databricks_sp_secret_arn=WS_SECRET_ARN)

    with caplog.at_level(logging.DEBUG):
        await svc.provision_databricks_runtime(agent, _make_tenant(binding_mode="sp_secret"))

    assert WS_SECRET_ARN not in caplog.text
    assert "discarding" in caplog.text


@pytest.mark.asyncio
async def test_a_foreign_arn_is_a_discarded_claim_not_a_teardown_failure():
    """A planted ARN must not make the agent UNDELETABLE.

    Refusing to delete it is right; RAISING would be a second bug: no retry can make a foreign
    ARN deletable, so a raised failure would gate the record forever (the `_NON_BLOCKING_ITEMS`
    lesson) and let a caller who planted one deny deletion of their own agent. The pointer is
    dropped, the cascade proceeds, and nobody else's secret is touched.
    """
    svc, _db, secrets, _r = _make_service()
    agent = _make_agent(
        binding_mode="sp_secret",
        databricks_sp_id="a-sp",
        databricks_sp_secret_arn=WS_SECRET_ARN,
    )

    await svc.delete_databricks_runtime(agent, _make_tenant(binding_mode="sp_secret"))

    assert secrets.deleted == []
    assert WS_SECRET_ARN in secrets.store
    assert agent.databricks_sp_secret_arn is None
    assert agent.databricks_sp_id is None


@pytest.mark.asyncio
async def test_a_foreign_pointer_is_never_reported_as_this_agents_orphan(caplog):
    """The surviving-principal line is the source of the orphan list, so reporting another agent's
    SP under this agent's id would send an operator to delete a live principal belonging to a
    different agent."""
    svc, _db, _s, _r = _make_service()
    agent = _make_agent(
        agent_id="rec-agent-b",
        binding_mode="sp_secret",
        databricks_sp_id="agent-a-sp-app-id",
        databricks_sp_secret_arn=_agent_secret_arn("rec-agent-a"),
    )

    with caplog.at_level(logging.INFO):
        await svc.delete_databricks_runtime(agent, _make_tenant(binding_mode="sp_secret"))

    assert "agent-a-sp-app-id" not in caplog.text
    assert "survives" not in caplog.text
    assert agent.databricks_sp_id is None


def _runtime_status(svc, agent, tenant=None, stage=None):
    """Call the SYNC producer the way the route's threadpool does (no running loop)."""
    return svc.runtime_status(agent, tenant=tenant, stage=stage)


def _app(state, *, container="status", url=APP_URL):
    record = {"name": APP_NAME, "url": url, container: {"state": state}}
    return record


@pytest.mark.parametrize(
    "state,expected",
    [
        ("RUNNING", "ready"),
        ("ACTIVE", "ready"),
        ("DEPLOYING", "creating"),
        ("STARTING", "creating"),
        ("UPDATING", "updating"),
        ("CRASHED", "failed"),
        ("ERROR", "failed"),
        ("STOPPED", "unknown"),
        ("STOPPING", "unknown"),
        ("DELETING", "unknown"),
        ("UNAVAILABLE", "unknown"),
    ],
)
def test_the_app_state_maps_into_the_closed_union(state, expected):
    db = _FakeDatabricks(apps=[_app(state)])
    svc, _db, _s, _r = _make_service(databricks=db)

    result = _runtime_status(svc, _make_agent(), _make_tenant())

    assert result.status == expected


def test_every_produced_status_is_a_member_of_the_closed_union():
    """The union is CLOSED (models/agent.RUNTIME_STATUSES). Executed over every state this
    producer knows plus a state it does not, so a future mapping entry that invents a value
    fails offline."""
    from models.agent import RUNTIME_STATUSES
    from services.databricks_runtime_status import _APP_STATE_TO_RUNTIME_STATUS

    assert set(_APP_STATE_TO_RUNTIME_STATUS.values()) <= set(RUNTIME_STATUSES)

    for state in [*_APP_STATE_TO_RUNTIME_STATUS, "SOME_STATE_DATABRICKS_ADDS_LATER"]:
        db = _FakeDatabricks(apps=[_app(state)])
        svc, _db, _s, _r = _make_service(databricks=db)
        assert _runtime_status(svc, _make_agent(), _make_tenant()).status in RUNTIME_STATUSES


def test_an_unmapped_state_is_unknown_and_names_the_state():
    """A state Databricks adds later must degrade to "unknown", never to "failed" — and naming
    it is what makes the state debuggable. The value is a fixed Databricks vocabulary, not
    caller-supplied, and it is regex-guarded before it is echoed."""
    db = _FakeDatabricks(apps=[_app("SOME_FUTURE_STATE")])
    svc, _db, _s, _r = _make_service(databricks=db)

    result = _runtime_status(svc, _make_agent(), _make_tenant())

    assert result.status == "unknown"
    assert "SOME_FUTURE_STATE".lower() in (result.detail or "").lower()


def test_a_hostile_state_string_is_never_echoed_into_the_detail():
    """The state is echoed only when it LOOKS like a state code. An upstream that smuggled a
    URL, a token, or an account id through that field must not have it rendered in a UI."""
    hostile = "https://dbc-evil.example.com/?token=SECRET_TOKEN_VALUE 123456789012"
    db = _FakeDatabricks(apps=[_app(hostile)])
    svc, _db, _s, _r = _make_service(databricks=db)

    result = _runtime_status(svc, _make_agent(), _make_tenant())

    assert result.status == "unknown"
    assert "SECRET_TOKEN_VALUE" not in (result.detail or "")
    assert "123456789012" not in (result.detail or "")
    assert "dbc-evil" not in (result.detail or "")


def test_a_missing_state_container_is_unknown_not_failed():
    db = _FakeDatabricks(apps=[{"name": APP_NAME, "url": APP_URL}])
    svc, _db, _s, _r = _make_service(databricks=db)

    assert _runtime_status(svc, _make_agent(), _make_tenant()).status == "unknown"


def test_the_state_is_read_from_app_status_when_that_is_the_container_name():
    """The app record's field names are unverified, so the producer reads both plausible
    containers, the same two-key tolerance discovery's ``url``/``app_url`` handling uses. Reading
    only one would report a live agent as "unknown" for a field-name difference."""
    db = _FakeDatabricks(apps=[_app("RUNNING", container="app_status")])
    svc, _db, _s, _r = _make_service(databricks=db)

    assert _runtime_status(svc, _make_agent(), _make_tenant()).status == "ready"


def test_no_runtime_handle_is_not_deployed_with_zero_databricks_calls():
    """A registered-but-inert record is a fact we already hold locally — asking Databricks
    about it would be a round-trip to learn nothing."""
    db = _FakeDatabricks()
    svc, _db, _s, _r = _make_service(databricks=db)

    result = _runtime_status(svc, _make_agent(runtime_handle=None), _make_tenant())

    assert result.status == "not_deployed"
    assert db.calls == []


def test_an_app_no_workspace_lists_is_not_deployed():
    """The listing answered, and the app is not in it. That is evidence of absence, not a
    probe failure — the same distinction ``runtime_exists`` draws between NotFound and an
    ambiguous error."""
    db = _FakeDatabricks(apps=[{"name": "someone-else", "url": "https://other.example.com"}])
    svc, _db, _s, _r = _make_service(databricks=db)

    result = _runtime_status(svc, _make_agent(), _make_tenant())

    assert result.status == "not_deployed"


@pytest.mark.parametrize("failing_call", ["mint_m2m_token", "list_apps"])
def test_a_databricks_error_degrades_to_unknown_with_a_safe_code(failing_call):
    """A denying or unreachable workspace is ambiguous: the agent may be healthy behind a rotated
    credential. Reporting "failed" would state a conclusion the evidence does not support."""
    db = _FakeDatabricks()
    db.raise_on[failing_call] = DatabricksError(
        "Databricks rejected the request (list apps, status 403)", kind="forbidden"
    )
    svc, _db, _s, _r = _make_service(databricks=db)

    result = _runtime_status(svc, _make_agent(), _make_tenant())

    assert result.status == "unknown"
    assert "forbidden" in (result.detail or "")


def test_the_upstream_error_message_never_reaches_the_detail():
    """The client composes a safe message, but this layer does not depend on that: it reads
    ``.kind`` only, because ``detail`` is rendered in a UI."""
    db = _FakeDatabricks()
    db.raise_on["list_apps"] = DatabricksError(
        "Databricks rejected https://dbc-test.cloud.databricks.com/api/2.0/apps "
        "with Bearer LEAKED_TOKEN_VALUE for account 123456789012",
        kind="forbidden",
    )
    svc, _db, _s, _r = _make_service(databricks=db)

    detail = _runtime_status(svc, _make_agent(), _make_tenant()).detail or ""

    assert "LEAKED_TOKEN_VALUE" not in detail
    assert "123456789012" not in detail
    assert "/api/2.0/apps" not in detail


def test_a_hostile_error_kind_is_not_echoed():
    """``.kind`` is constrained upstream, but a producer that trusted that would be one
    upstream change away from rendering a payload. Re-validated here, over a hostile value."""
    db = _FakeDatabricks()
    db.raise_on["list_apps"] = DatabricksError(
        "safe", kind="forbidden https://evil.example.com SECRET_TOKEN_VALUE"
    )
    svc, _db, _s, _r = _make_service(databricks=db)

    result = _runtime_status(svc, _make_agent(), _make_tenant())

    assert result.status == "unknown"
    assert "SECRET_TOKEN_VALUE" not in (result.detail or "")
    assert "evil.example.com" not in (result.detail or "")


def test_the_workspace_secret_never_reaches_the_status_response_or_a_log(caplog):
    """The producer reads the tenant's stored SP secret to mint a token. The sentinel must not
    appear in the response or in anything logged."""
    db = _FakeDatabricks()
    svc, _db, _s, _r = _make_service(databricks=db)

    with caplog.at_level(logging.DEBUG):
        result = _runtime_status(svc, _make_agent(), _make_tenant())

    assert WS_SECRET_SENTINEL not in repr(result.model_dump())
    assert WS_SECRET_SENTINEL not in caplog.text


def test_a_missing_tenant_is_unknown_and_does_not_raise():
    """A record whose tenant is gone is a governance problem, not a runtime verdict."""
    tenants = MagicMock()
    tenants.get.return_value = None
    svc, db, _s, _r = _make_service(tenants=tenants)

    result = _runtime_status(svc, _make_agent(), None)

    assert result.status == "unknown"
    assert db.calls == []


def test_a_tenant_with_no_databricks_stage_is_unknown():
    svc, _db, _s, _r = _make_service()

    result = _runtime_status(svc, _make_agent(), _make_tenant(stages={}))

    assert result.status == "unknown"


def test_an_unresolvable_credential_is_unknown_not_failed():
    """The stage names a Secrets Manager ARN that no longer resolves. ``_workspace_token``
    raises ``ProvisioningError`` for the provisioning path; the STATUS path must absorb it."""
    svc, db, _s, _r = _make_service()

    result = _runtime_status(svc, _make_agent(), _make_tenant(sp_client_secret_arn=""))

    assert result.status == "unknown"
    assert db.calls == []


def test_an_unexpected_collaborator_fault_is_unknown_not_a_raise():
    """A read surface has no honest way to 5xx. Even an AttributeError from a half-shaped
    collaborator degrades to "unknown" rather than blanking the fleet view."""
    db = _FakeDatabricks()
    db.raise_on["list_apps"] = RuntimeError("boom")
    svc, _db, _s, _r = _make_service(databricks=db)

    result = _runtime_status(svc, _make_agent(), _make_tenant())

    assert result.status == "unknown"
    assert "boom" not in (result.detail or "")


def test_the_result_carries_the_agent_id_the_stage_and_no_arn():
    """``runtime_arn`` stays None for a Databricks agent: it is an ARN field that the delete
    cascade and the per-stage map both PARSE as one, so putting an app URL in it would feed a
    URL to ``arn.rsplit("/", 1)[-1]`` (models/agent.py's two-fields-two-platforms rule)."""
    svc, _db, _s, _r = _make_service()

    result = _runtime_status(svc, _make_agent(), _make_tenant())

    assert result.agent_id == "rec-abc123"
    assert result.stage == "dev"
    assert result.runtime_arn is None
    assert result.image_tag is None
    assert result.checked_at


def test_an_explicit_stage_probes_only_that_stage():
    """Asking about "dev" must never be answered with another workspace's reading — the same
    rule the AgentCore path states: a guessed stage manufactures per-stage evidence."""
    other_url = "https://dbc-other.cloud.databricks.com"
    stages = {
        "dev": DatabricksStageConfig(
            workspace_url=WORKSPACE_URL,
            workspace_id="0",
            account_id=ACCOUNT_ID,
            sp_client_id="ws-sp-client-id",
            sp_client_secret_arn=WS_SECRET_ARN,
        ),
        "prod": DatabricksStageConfig(
            workspace_url=other_url,
            workspace_id="0",
            account_id=ACCOUNT_ID,
            sp_client_id="ws-sp-client-id",
            sp_client_secret_arn=WS_SECRET_ARN,
        ),
    }
    db = _FakeDatabricks(apps_by_workspace={other_url: [_app("RUNNING")], WORKSPACE_URL: []})
    svc, _db, _s, _r = _make_service(databricks=db)
    tenant = _make_tenant(stages=stages)

    dev = _runtime_status(svc, _make_agent(), tenant, stage="dev")
    prod = _runtime_status(svc, _make_agent(), tenant, stage="prod")

    assert (dev.status, dev.stage) == ("not_deployed", "dev")
    assert (prod.status, prod.stage) == ("ready", "prod")
    assert ("list_apps", other_url) in db.calls


def test_an_unknown_stage_name_is_not_deployed_with_no_databricks_call():
    """A stage the tenant does not have is answered locally — never by falling through to
    another stage's runtime, which would look like an answer to the question asked."""
    db = _FakeDatabricks()
    svc, _db, _s, _r = _make_service(databricks=db)

    result = _runtime_status(svc, _make_agent(), _make_tenant(), stage="staging")

    assert (result.status, result.stage) == ("not_deployed", "staging")
    assert db.calls == []


def test_with_no_stage_asked_the_hosting_workspace_is_the_reported_stage():
    """Stage-less is the default the route uses. It probes the tenant's workspaces in a
    deterministic order and reports the stage that ACTUALLY listed the app — evidence, not a
    positional guess. (A Databricks app URL carries no workspace identity, which is why the
    listing is the only honest source; see ``_resolve_stage_and_app``.)"""
    other_url = "https://dbc-other.cloud.databricks.com"
    stages = {
        "dev": DatabricksStageConfig(
            workspace_url=WORKSPACE_URL,
            workspace_id="0",
            account_id=ACCOUNT_ID,
            sp_client_id="ws-sp-client-id",
            sp_client_secret_arn=WS_SECRET_ARN,
        ),
        "prod": DatabricksStageConfig(
            workspace_url=other_url,
            workspace_id="0",
            account_id=ACCOUNT_ID,
            sp_client_id="ws-sp-client-id",
            sp_client_secret_arn=WS_SECRET_ARN,
        ),
    }
    db = _FakeDatabricks(apps_by_workspace={other_url: [_app("RUNNING")], WORKSPACE_URL: []})
    svc, _db, _s, _r = _make_service(databricks=db)

    result = _runtime_status(svc, _make_agent(), _make_tenant(stages=stages))

    assert (result.status, result.stage) == ("ready", "prod")


def test_one_unreachable_workspace_does_not_hide_the_one_that_answers():
    """Attempt-all, the multi-target idiom this file already uses. A rotated credential on one
    workspace must not turn a healthy agent on another into "unknown"."""
    other_url = "https://dbc-other.cloud.databricks.com"
    stages = {
        "dev": DatabricksStageConfig(
            workspace_url=WORKSPACE_URL,
            workspace_id="0",
            account_id=ACCOUNT_ID,
            sp_client_id="ws-sp-client-id",
            sp_client_secret_arn="",
        ),
        "prod": DatabricksStageConfig(
            workspace_url=other_url,
            workspace_id="0",
            account_id=ACCOUNT_ID,
            sp_client_id="ws-sp-client-id",
            sp_client_secret_arn=WS_SECRET_ARN,
        ),
    }
    db = _FakeDatabricks(apps_by_workspace={other_url: [_app("RUNNING")]})
    svc, _db, _s, _r = _make_service(databricks=db)

    result = _runtime_status(svc, _make_agent(), _make_tenant(stages=stages))

    assert (result.status, result.stage) == ("ready", "prod")


def test_when_every_workspace_read_fails_the_answer_is_unknown_not_not_deployed():
    """The distinction that matters most in this file. "not_deployed" is a CLAIM that nothing
    is running; nobody was able to look, so the only honest answer is "unknown"."""
    db = _FakeDatabricks()
    db.raise_on["list_apps"] = DatabricksError("safe", kind="unreachable")
    svc, _db, _s, _r = _make_service(databricks=db)

    result = _runtime_status(svc, _make_agent(), _make_tenant())

    assert result.status == "unknown"
    assert "unreachable" in (result.detail or "")


def test_a_non_databricks_agent_is_refused_by_the_producer_not_answered():
    """The producer is not the dispatcher. An AgentCore agent reaching it means the seam broke,
    and answering anyway would report an AgentCore runtime's status from a Databricks
    listing — a reading of the wrong runtime is worse than no reading."""
    svc, db, _s, _r = _make_service()

    result = _runtime_status(svc, _make_agent(platform=Platform.AWS_BEDROCK), _make_tenant())

    assert result.status == "unknown"
    assert db.calls == []


def test_a_deleted_tenant_that_RAISES_on_lookup_is_unknown_not_a_500():
    """The live ``TenantService.get`` raises ``TenantError(kind="not_found")`` — it never returns
    None. Catching only ``ProvisioningError`` here would therefore let the MOST LIKELY
    missing-tenant case in production escape a method whose whole contract is that it never
    raises, turning a deleted tenant into a 500 that blanks the fleet view."""

    class _TenantError(Exception):
        pass

    tenants = MagicMock()
    tenants.get.side_effect = _TenantError("Unknown tenant")
    svc, db, _s, _r = _make_service(tenants=tenants)

    result = _runtime_status(svc, _make_agent(), None)

    assert result.status == "unknown"
    assert db.calls == []


def test_mixed_evidence_is_unknown_not_not_deployed():
    """One workspace answered empty and another failed, while the app is in fact running on the
    one that failed. ``not_deployed`` is a positive claim that nothing is running, so it needs a
    complete negative result: a workspace nobody could read is not a workspace with nothing in
    it. Reporting "not_deployed|dev" here would tell an operator their live agent was undeployed."""
    other_url = "https://dbc-other.cloud.databricks.com"
    stages = {
        "dev": DatabricksStageConfig(
            workspace_url=WORKSPACE_URL,
            workspace_id="0",
            account_id=ACCOUNT_ID,
            sp_client_id="ws-sp-client-id",
            sp_client_secret_arn=WS_SECRET_ARN,
        ),
        "prod": DatabricksStageConfig(
            workspace_url=other_url,
            workspace_id="0",
            account_id=ACCOUNT_ID,
            sp_client_id="ws-sp-client-id",
            sp_client_secret_arn="",
        ),
    }
    db = _FakeDatabricks(apps_by_workspace={WORKSPACE_URL: [], other_url: [_app("RUNNING")]})
    svc, _db, _s, _r = _make_service(databricks=db)

    result = _runtime_status(svc, _make_agent(), _make_tenant(stages=stages))

    assert result.status == "unknown", (
        "one workspace answering empty is not evidence that the app is undeployed while "
        "another workspace could not be read at all"
    )
    assert result.status != "not_deployed"
    assert "credential_unavailable" in (result.detail or "")


def test_a_complete_negative_sweep_is_still_not_deployed():
    """When every workspace answers and none lists the app, the negative result is complete and
    ``not_deployed`` is the honest answer; a real absence must not become a permanent "unknown"."""
    other_url = "https://dbc-other.cloud.databricks.com"
    stages = {
        "dev": DatabricksStageConfig(
            workspace_url=WORKSPACE_URL,
            workspace_id="0",
            account_id=ACCOUNT_ID,
            sp_client_id="ws-sp-client-id",
            sp_client_secret_arn=WS_SECRET_ARN,
        ),
        "prod": DatabricksStageConfig(
            workspace_url=other_url,
            workspace_id="0",
            account_id=ACCOUNT_ID,
            sp_client_id="ws-sp-client-id",
            sp_client_secret_arn=WS_SECRET_ARN,
        ),
    }
    db = _FakeDatabricks(apps_by_workspace={WORKSPACE_URL: [], other_url: []})
    svc, _db, _s, _r = _make_service(databricks=db)

    result = _runtime_status(svc, _make_agent(), _make_tenant(stages=stages))

    assert result.status == "not_deployed"


def test_an_explicit_stage_that_fails_is_unknown_not_not_deployed():
    """The single-stage form of the same rule: asked about "dev" and unable to read dev, the
    answer is "we could not look", never "nothing is deployed there"."""
    db = _FakeDatabricks()
    db.raise_on["list_apps"] = DatabricksError("safe", kind="forbidden")
    svc, _db, _s, _r = _make_service(databricks=db)

    result = _runtime_status(svc, _make_agent(), _make_tenant(), stage="dev")

    assert (result.status, result.stage) == ("unknown", "dev")
    assert "forbidden" in (result.detail or "")


def _delete_client(agent, databricks=None, identity=None):
    """`_reprovision_client` plus seeded Databricks and Entra identity singletons.

    Seeded on the module (not via `patch`) for the same reason the registry is: the route reaches
    the collaborators through `get_databricks_identity_service()` / `get_identity_service()`, and
    pinning the singletons proves the wired path rather than a patched stand-in of it. `identity`
    defaults to a no-op mock whose `delete_identity` succeeds (the delete route tears the Entra
    app down too). The registry's `delete` answers with the real record because
    `response_model=Agent` validates what the route returns.
    """
    import api.routes.agents as agents_module

    client, svc = _reprovision_client(agent)
    svc.delete.return_value = agent
    agents_module._databricks_identity_svc = databricks
    if identity is None:
        identity = MagicMock()
        identity.delete_identity = AsyncMock(return_value=None)
    agents_module._identity_svc = identity
    return client, svc


@pytest.mark.asyncio
async def test_deleting_a_databricks_agent_removes_the_federation_audience(reprovision_env):
    """THE CRITICAL. A real service over fakes — so the assertion is that the AUDIENCE actually
    left the fake account policy, not merely that a mock was called.

    Federation mode has no per-agent secret by construction (provisioning clears both SP fields
    on that leg), so the sp_secret residue is pinned by the next test rather than here.
    """
    tenant = _make_tenant()
    svc, db, _s, _r = _make_service(tenants=_FakeTenants(tenant))
    agent = _make_agent(binding_mode="federation")
    await svc.provision_databricks_runtime(agent, tenant)
    assert db.audiences == {"agent-app-guid"}

    client, registry = _delete_client(agent, databricks=svc)
    resp = client.delete("/api/v1/agents/rec-abc123")

    assert resp.status_code == 200
    assert ("ensure_federation_audience", "agent-app-guid", False) in db.calls
    assert ("ensure_federation_audience", AGENT_AUDIENCE, False) in db.calls
    assert db.audiences == set()
    registry.delete.assert_called_once_with("rec-abc123")


def test_deleting_an_sp_secret_agent_deletes_its_service_principal_secret(reprovision_env):
    """The other residue the route must reach: the per-agent SP credential, deleted BY ITS
    STORED ARN through the wired path (previously nothing called this at all)."""
    agent_arn_secret = _agent_secret_arn("rec-abc123")
    secrets = _FakeSecrets(
        store={
            **_default_secret_store(),
            agent_arn_secret: json.dumps({"client_secret": "per-agent"}),
        }
    )
    tenant = _make_tenant(binding_mode="sp_secret")
    svc, db, _s, _r = _make_service(secrets=secrets, tenants=_FakeTenants(tenant))
    agent = _make_agent(
        binding_mode="sp_secret",
        databricks_sp_id="sp-app-id-1",
        databricks_sp_secret_arn=agent_arn_secret,
    )

    client, registry = _delete_client(agent, databricks=svc)
    resp = client.delete("/api/v1/agents/rec-abc123")

    assert resp.status_code == 200
    assert secrets.deleted == [agent_arn_secret]
    assert not any(c[0] == "ensure_federation_audience" for c in db.calls)
    registry.delete.assert_called_once_with("rec-abc123")


def test_deleting_an_agent_tears_down_its_entra_identity_before_the_record(reprovision_env):
    """The direct delete route once left the per-agent Entra app and SP orphaned: ``delete_identity``
    was wired only into the repo cascade, and a Databricks agent has no repo. The route tears the
    Entra identity down before the registry delete (the record is the only thing naming the app
    ids) as the registered-agent cascade's ``identity`` line item, with the OBO provider split
    into its own leg (``include_obo_provider=False`` stops a second delete of the resource that
    leg already reported)."""
    identity = MagicMock()
    identity.delete_identity = AsyncMock(return_value=None)
    agent = _make_agent(binding_mode=None)

    client, registry = _delete_client(agent, identity=identity)
    resp = client.delete("/api/v1/agents/rec-abc123")

    assert resp.status_code == 200
    identity.delete_identity.assert_awaited_once_with(agent, include_obo_provider=False)
    registry.delete.assert_called_once_with("rec-abc123")


def test_a_failed_entra_teardown_is_reported_and_the_record_still_goes(reprovision_env, caplog):
    """The registered-agent cascade's ``identity`` leg is best-effort: a Graph outage is reported
    (one ``[teardown]`` line per resource, the operator's reclaim instruction) and the record
    still goes, because trapping the row behind the orphan the operator was trying to reclaim
    helps nobody and every leg is idempotent. The Databricks residue leg stays the one blocking
    exception (the audience test below): the customer's live trust state, not the platform's own
    directory object."""
    identity = MagicMock()
    identity.delete_identity = AsyncMock(side_effect=RuntimeError("graph down"))
    agent = _make_agent(binding_mode=None)

    client, registry = _delete_client(agent, identity=identity)
    with caplog.at_level(logging.INFO, logger="api.routes.agents"):
        resp = client.delete("/api/v1/agents/rec-abc123")

    assert resp.status_code == 200
    registry.delete.assert_called_once_with("rec-abc123")
    assert "item=identity outcome=failed" in caplog.text


@pytest.mark.asyncio
async def test_a_failed_audience_removal_fails_the_delete_and_keeps_the_record(reprovision_env):
    """The BLOCKING half. A surviving audience means a token minted for this agent's Entra
    audience is still exchangeable at the customer's account — and the record is the ONLY thing
    that still names which audience that is. So the delete fails loudly and the row survives,
    which is what makes the retry possible."""
    db = _FakeDatabricks()
    tenant = _make_tenant()
    svc, _db, _s, _r = _make_service(databricks=db, tenants=_FakeTenants(tenant))
    agent = _make_agent(binding_mode="federation")
    await svc.provision_databricks_runtime(agent, tenant)
    db.raise_on["ensure_federation_audience"] = DatabricksError("nope", kind="forbidden")

    client, registry = _delete_client(agent, databricks=svc)
    resp = client.delete("/api/v1/agents/rec-abc123")

    assert resp.status_code == 502
    registry.delete.assert_not_called()
    assert "not deleted" in resp.json()["detail"]
    assert "account admin" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_a_failed_secret_delete_still_deletes_the_record(reprovision_env):
    """The non-blocking half, and the asymmetry's whole point. A Secrets Manager fault no operator
    retry addresses must not trap the record: the secret is ours, in our account, and it reports
    itself for reclamation."""
    agent_arn_secret = _agent_secret_arn("rec-abc123")
    secrets = _FakeSecrets(store={agent_arn_secret: json.dumps({"client_secret": "x"})})
    secrets.raise_on_delete = True
    tenant = _make_tenant(binding_mode="sp_secret")
    svc, db, _s, _r = _make_service(secrets=secrets, tenants=_FakeTenants(tenant))
    agent = _make_agent(
        binding_mode="sp_secret",
        databricks_sp_id="sp-app-id-1",
        databricks_sp_secret_arn=agent_arn_secret,
    )

    client, registry = _delete_client(agent, databricks=svc)
    resp = client.delete("/api/v1/agents/rec-abc123")

    assert resp.status_code == 200
    registry.delete.assert_called_once_with("rec-abc123")
    assert not any(c[0] == "ensure_federation_audience" for c in db.calls)


@pytest.mark.asyncio
async def test_the_secret_leak_is_reported_by_agent_id_and_never_by_arn(caplog):
    """A secretsmanager ARN carries the AWS ACCOUNT ID, which a hard project rule bans
    everywhere including logs. The agent id is enough to find the secret: ownership was just
    established by the `/{agent.id}` suffix, the only non-configuration part of the name."""
    agent_arn_secret = _agent_secret_arn("rec-abc123")
    secrets = _FakeSecrets(store={agent_arn_secret: json.dumps({"client_secret": "x"})})
    secrets.raise_on_delete = True
    svc, _db, _s, _r = _make_service(secrets=secrets)
    agent = _make_agent(binding_mode="sp_secret", databricks_sp_secret_arn=agent_arn_secret)

    with caplog.at_level(logging.INFO):
        await svc.delete_databricks_runtime(agent, _make_tenant(binding_mode="sp_secret"))

    assert "rec-abc123" in caplog.text
    assert "survives teardown" in caplog.text
    assert agent_arn_secret not in caplog.text
    assert "arn:aws:secretsmanager" not in caplog.text


def test_deleting_an_agentcore_agent_touches_no_databricks_service(reprovision_env):
    """THE FENCE. An AgentCore agent's delete path must be behaviourally unchanged: the
    Databricks collaborator is a strict mock, so ANY call on it fails this test."""
    databricks = MagicMock()
    databricks.delete_databricks_runtime = MagicMock(
        side_effect=AssertionError("an AgentCore delete reached the Databricks teardown")
    )
    agent = _make_agent(
        platform=Platform.AWS_BEDROCK,
        runtime_handle=None,
        runtime_kind=None,
        agent_arn="arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/abc",
    )

    client, registry = _delete_client(agent, databricks=databricks)
    resp = client.delete("/api/v1/agents/rec-abc123")

    assert resp.status_code == 200
    registry.delete.assert_called_once_with("rec-abc123")
    databricks.delete_databricks_runtime.assert_not_called()


def test_a_never_provisioned_databricks_agent_needs_no_databricks_wiring(reprovision_env):
    """No residue ⇒ no teardown, so a registered-but-never-provisioned record stays deletable
    even on a deployment with NO Databricks identity service at all (`None`)."""
    agent = _make_agent(binding_mode=None, databricks_sp_id=None, databricks_sp_secret_arn=None)

    client, registry = _delete_client(agent, databricks=None)
    resp = client.delete("/api/v1/agents/rec-abc123")

    assert resp.status_code == 200
    registry.delete.assert_called_once_with("rec-abc123")


def test_a_provisioned_databricks_agent_is_refused_when_nothing_is_wired(reprovision_env):
    """The inverse. A record carrying LIVE state on an unwired deployment is REFUSED, not
    deleted: dropping it would erase the only thing that knows which audience to withdraw."""
    agent = _make_agent(binding_mode="federation")

    client, registry = _delete_client(agent, databricks=None)
    resp = client.delete("/api/v1/agents/rec-abc123")

    assert resp.status_code == 500
    registry.delete.assert_not_called()
    assert "not deleted" in resp.json()["detail"]


def test_teardown_is_gated_on_platform_not_on_the_governed_predicate(reprovision_env):
    """WHY THE GATE IS `platform`, NOT `is_databricks_governed_agent`. The governed gate also
    needs `auth_type == ENTRA` and a `runtime_handle`, either of which a later EDIT can drop
    from an ALREADY-provisioned record — and that record, with live residue and a broken gate,
    is exactly the one that must still be cleaned up."""
    from models.agent import is_databricks_governed_agent

    agent = _make_agent(binding_mode="federation", runtime_handle=None)
    assert not is_databricks_governed_agent(agent)

    databricks = MagicMock()
    databricks.remove_databricks_artifact = AsyncMock(return_value=None)

    client, registry = _delete_client(agent, databricks=databricks)
    resp = client.delete("/api/v1/agents/rec-abc123")

    assert resp.status_code == 200
    databricks.remove_databricks_artifact.assert_awaited_once()
    assert databricks.remove_databricks_artifact.await_args.args[1].kind == "federation_audience"
    registry.delete.assert_called_once_with("rec-abc123")


@pytest.mark.asyncio
async def test_provisioning_persists_the_resolved_bound_stage(reprovision_env):
    """Provisioning records which workspace stage hosts the agent, so the invoke resolver never has
    to re-derive it from an Apps hostname (which carries none). The default tenant has one stage,
    ``dev``, the stage ``_resolve_stage_and_app`` matches the app in."""
    tenant = _make_tenant()
    svc, _db, _s, registry = _make_service(tenants=_FakeTenants(tenant))
    agent = _make_agent(binding_mode="federation")

    await svc.provision_databricks_runtime(agent, tenant)

    assert agent.bound_stage == "dev"
    assert registry.persist_identity.call_args.args[0].bound_stage == "dev"


def test_bound_stage_round_trips_through_the_envelope_and_absent_is_none():
    """Additive envelope key: a record carrying it round-trips, and an older envelope that lacks it
    hydrates as None, which sends the invoke resolver to its heuristic."""
    agent = _make_agent(binding_mode="federation")
    agent.bound_stage = "prod"
    env = agent.to_envelope()
    assert env["bound_stage"] == "prod"

    record = {
        "recordId": "rec-abc123",
        "name": "Claims Triage DE",
        "description": "",
        "status": "APPROVED",
        "createdAt": "2026-08-19T00:00:00+00:00",
        "updatedAt": "2026-08-19T00:00:00+00:00",
    }
    assert Agent.from_record(record, env).bound_stage == "prod"
    legacy = {k: v for k, v in env.items() if k != "bound_stage"}
    assert Agent.from_record(record, legacy).bound_stage is None


class _FakeArtifactAdapter:
    """A minimal PlatformAdapter double: it enumerates a fixed artifact list and records
    removals, so the route's per-policy arms can be exercised without a real platform."""

    def __init__(self, artifacts):
        self._artifacts = artifacts
        self.removed = []

    def named_artifacts(self, agent):
        return list(self._artifacts)

    async def remove_artifact(self, agent, artifact):
        self.removed.append(artifact)


def test_a_never_touch_artifact_is_skipped_and_the_record_still_goes(reprovision_env, monkeypatch, caplog):
    """The reserved third policy (no artifact carries it today — the enum member + this route arm
    exist so the GCP shared-provider case is not first written under pressure). A NEVER_TOUCH
    artifact is SKIPPED with its reason: never removed, and the record still deletes."""
    import api.routes.agents as agents_module

    fake = _FakeArtifactAdapter(
        [Artifact(kind="shared_provider", ref="shared", policy=ArtifactPolicy.NEVER_TOUCH)]
    )
    monkeypatch.setattr(agents_module, "status_adapter", lambda agent: fake)

    agent = _make_agent(binding_mode=None)
    client, registry = _delete_client(agent)
    with caplog.at_level(logging.INFO, logger="api.routes.agents"):
        resp = client.delete("/api/v1/agents/rec-abc123")

    assert resp.status_code == 200
    assert fake.removed == []
    assert "item=shared_provider outcome=skipped reason=never_touch" in caplog.text
    registry.delete.assert_called_once_with("rec-abc123")
