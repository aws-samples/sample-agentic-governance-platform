"""Tests for the ``PlatformAdapter`` seam.

THE CONFORMANCE TESTS ARE THE CONTRACT. A ``Protocol`` is satisfied structurally and enforced
nowhere at runtime, so ``test_runtime_catalog.py``'s rule applies verbatim and is the reason this
file exists at all: the signatures are compared parameter by parameter against BOTH adapters,
because a renamed or re-defaulted parameter otherwise ships an adapter that type-checks and that
no caller can call.

Beyond the shape, three SHARED BEHAVIORS are asserted on both adapters — they are the honesty
rules the whole epic is about, and an adapter that breaks one is wrong even when its signature
is right:

  1. ``unknown`` is never ``failed`` — a read that could not reach the platform says so.
  2. A missing collaborator RAISES on a write and DEGRADES on a read. A write that silently
     no-ops strands a record at 'pending' with no error; a read that raises blanks the fleet
     view.
  3. The metadata-only fall-through: a record with no runtime ANYWHERE reads ``not_deployed``
     without touching a platform, on both platforms.

Collaborators are doubles. ``MagicMock``/``AsyncMock`` for the two identity services where only
DELEGATION is under test (the delegated logic keeps its own tests — this task rewrites no
behavior), and the REAL services over inert doubles for the metadata-only case, because that
fall-through is a property of the services the adapters wrap, and asserting it against a mock
would assert nothing.

The repo is NOT in pytest-asyncio ``auto`` mode, so every async test is decorated explicitly.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Generic, Protocol
from unittest.mock import AsyncMock, MagicMock

import pytest

from models.agent import Agent, AgentAuthType, LifecycleState, Platform, RuntimeStatus, RuntimeStatusValue
from models.tenant import DatabricksStageConfig, Tenant, TenantBindingMode, TenantPlatform, TenantStageConfig
from services.agent_identity_service import AgentIdentityService, RuntimeStatusWithEnv
from services.databricks_identity_service import DatabricksIdentityService
from services.platform_adapter import (
    AdapterError,
    Artifact,
    ArtifactPolicy,
    InvokeError,
    PlatformAdapter,
)
from services.platform_adapter_factory import _tenant_probe, adapter_for, build_adapters
from services.platform_adapters.aws import AwsPlatformAdapter
from services.platform_adapters.databricks import (
    _NO_READER_DETAIL,
    _READ_FAILED_DETAIL,
    DatabricksPlatformAdapter,
)
from services.platform_registry import declaration_for
from services.tenant_service import TenantService

# The protocol AS SHIPPED. Each method landed WITH both implementations — a member with no
# adapter behind it makes ``isinstance`` (structural) pass while every call fails, so this tuple
# is the conformance test's oracle and grows only when the adapters do.
_PROTOCOL_METHODS = (
    "declaration",
    "probe",
    "provision_runtime_binding",
    "runtime_status",
    "invoke",
    "named_artifacts",
    "remove_artifact",
)

WORKSPACE_URL = "https://dbc-test.cloud.databricks.com"
APP_URL = "https://claims-triage-1234.aws.databricksapps.com"


def _agent(
    *,
    platform: Platform = Platform.AWS_BEDROCK,
    agent_arn: str | None = None,
    runtime_handle: str | None = None,
    runtime_kind: str | None = None,
) -> Agent:
    now = datetime.now(timezone.utc)
    return Agent(
        id="rec-abc123",
        name="Claims Triage DE",
        purpose="Triage inbound motor claims",
        lifecycle_state=LifecycleState.APPROVED,
        platform=platform,
        auth_type=AgentAuthType.ENTRA,
        tenant_id="ten-1",
        agent_arn=agent_arn,
        runtime_handle=runtime_handle,
        runtime_kind=runtime_kind,
        created_at=now,
        updated_at=now,
        created_by="maria.bauer@example.com",
    )


def _aws_tenant() -> Tenant:
    return Tenant(
        id="ten-1",
        name="Retail Claims",
        line_of_business="Claims",
        entra_group_ids=[],
        platform=TenantPlatform.AWS,
        # An obviously-fake repdigit account id, the convention every AWS-stage test in this
        # repo uses — a real one is forbidden anywhere, tests included.
        stages={"dev": TenantStageConfig(account_id="111111111111", region="us-east-1")},
        created_by="seed",
        created_at="2026-08-19T00:00:00+00:00",
        updated_at="2026-08-19T00:00:00+00:00",
    )


def _dbx_tenant() -> Tenant:
    return Tenant(
        id="ten-2",
        name="Retail Claims DBX",
        line_of_business="Claims",
        entra_group_ids=[],
        platform=TenantPlatform.DATABRICKS,
        stages={
            "dev": DatabricksStageConfig(
                workspace_url=WORKSPACE_URL,
                workspace_id="0",
                cloud="aws",
                region="us-east-1",
                sp_client_id="ws-sp-client-id",
            )
        },
        created_by="seed",
        created_at="2026-08-19T00:00:00+00:00",
        updated_at="2026-08-19T00:00:00+00:00",
    )


def _status(status: RuntimeStatusValue) -> RuntimeStatus:
    return RuntimeStatus(
        agent_id="rec-abc123",
        stage="dev",
        status=status,
        checked_at="2026-08-19T00:00:00+00:00",
    )


def _aws_adapter(identity=None) -> AwsPlatformAdapter:
    return AwsPlatformAdapter(identity=identity or MagicMock(spec=AgentIdentityService))


def _dbx_adapter(databricks_identity=None, probe=None) -> DatabricksPlatformAdapter:
    return DatabricksPlatformAdapter(databricks_identity=databricks_identity, probe=probe)


@pytest.mark.parametrize("adapter", [AwsPlatformAdapter, DatabricksPlatformAdapter])
@pytest.mark.parametrize("method", _PROTOCOL_METHODS)
def test_adapters_conform_to_the_protocol_signature(adapter, method):
    """Parameter by parameter, name AND default AND kind — ``test_runtime_catalog``'s idiom.

    Defaults are compared because they are part of the call shape a caller relies on: an
    adapter that made ``stage`` required would satisfy a name-only comparison and 500 on
    every stage-less status read."""
    expected = inspect.signature(getattr(PlatformAdapter, method))
    actual = inspect.signature(getattr(adapter, method))

    def shape(sig):
        return [(p.name, p.kind, p.default) for p in sig.parameters.values()]

    assert shape(actual) == shape(expected)
    assert inspect.iscoroutinefunction(getattr(adapter, method)) is inspect.iscoroutinefunction(
        getattr(PlatformAdapter, method)
    )


def test_protocol_is_runtime_checkable_against_both_adapters():
    assert isinstance(_aws_adapter(), PlatformAdapter)
    assert isinstance(_dbx_adapter(), PlatformAdapter)


def test_protocol_carries_only_the_shipped_methods():
    """A method reaching the protocol EARLY is as much a drift as one reaching it late: a
    protocol that named a method with no adapter behind it would make ``isinstance``
    (structural) pass while every call failed.

    ``Protocol.__protocol_attrs__`` only exists on Python 3.12+; the image this runs in is
    ``python:3.11-slim``, so it is computed by hand here rather than read off that attribute —
    walking the MRO and collecting the non-dunder names each base contributes is exactly what
    ``__protocol_attrs__`` does internally, without depending on a version-gated private API.
    """
    declared = {
        name
        for base in inspect.getmro(PlatformAdapter)
        if base not in (object, Protocol, Generic)
        for name in base.__dict__
        if not name.startswith("_")
    }
    assert declared == set(_PROTOCOL_METHODS)


def test_adapter_error_keeps_a_well_shaped_kind():
    err = AdapterError("nope", kind="platform_unsupported")
    assert (str(err), err.kind, err.message) == (
        "nope",
        "platform_unsupported",
        "nope",
    )


@pytest.mark.parametrize(
    "kind", ["", "unknown", "platform_unsupported\ninjected: line", "PLATFORM_UNSUPPORTED"]
)
def test_adapter_error_refuses_a_kind_outside_its_table(kind):
    """A kind reaches an HTTP body through the table, so a kind the table lacks is a ``TypeError``
    at construction and the wrong raise fails in the test that exercises it."""
    with pytest.raises(TypeError, match="AdapterError: unknown kind"):
        AdapterError("nope", kind=kind)


def test_adapter_error_platform_unsupported_is_502_with_the_invoke_detail():
    err = AdapterError("nope", kind="platform_unsupported")
    assert (err.status_code, err.detail) == (502, "the agent could not be invoked [platform_unsupported]")
    assert err.code == "adapter.platform_unsupported"


def test_adapter_error_artifact_unremovable_is_500_with_the_delete_refusal():
    err = AdapterError("nope", kind="artifact_unremovable")
    assert err.status_code == 500
    assert (
        err.detail
        == "the agent's Databricks bindings cannot be removed on this deployment, so it was not deleted"
    )
    assert err.code == "adapter.artifact_unremovable"


def _adapters():
    return build_adapters(
        identity=MagicMock(spec=AgentIdentityService),
        databricks_identity=MagicMock(spec=DatabricksIdentityService),
        # ``spec=`` on purpose: an unspecced MagicMock would accept ``_probeX`` silently, so the
        # ONE place this diff names a private would be guarded by nothing.
        tenant_service=MagicMock(spec=TenantService),
    )


def test_build_adapters_is_exhaustive_over_tenant_platform():
    """Platform #3 fails HERE — one assert at the wiring site — rather than as a KeyError
    inside one projection in production (the ``DECLARATIONS`` rule, restated)."""
    assert set(_adapters()) == set(TenantPlatform)


def test_adapter_for_dispatches_by_platform_and_by_wire_string():
    adapters = _adapters()
    assert isinstance(adapter_for(adapters, TenantPlatform.AWS), AwsPlatformAdapter)
    assert isinstance(adapter_for(adapters, "databricks"), DatabricksPlatformAdapter)


@pytest.mark.parametrize("platform", ["gcp", "", "AWS ", None])
def test_adapter_for_refuses_an_unknown_platform(platform):
    """Never a silent fall-through: an unknown platform with no adapter behind it must not be
    handed the first adapter that happens to be in the dict."""
    with pytest.raises(AdapterError) as excinfo:
        adapter_for(_adapters(), platform)
    assert excinfo.value.kind == "platform_unsupported"


def test_adapter_for_refuses_a_platform_with_no_adapter_wired():
    """A deployment with no Databricks collaborator has no Databricks adapter, and asking for
    one is refused rather than answered with a half-wired object."""
    adapters = build_adapters(
        identity=MagicMock(spec=AgentIdentityService),
        databricks_identity=None,
        # ``spec=`` on purpose: an unspecced MagicMock would accept ``_probeX`` silently, so the
        # ONE place this diff names a private would be guarded by nothing.
        tenant_service=MagicMock(spec=TenantService),
    )
    with pytest.raises(AdapterError) as excinfo:
        adapter_for(adapters, TenantPlatform.DATABRICKS)
    assert excinfo.value.kind == "platform_unsupported"


def test_the_factory_bridges_the_tenant_probe_to_the_seam_shape():
    """The ONE line in this diff that names a private, executed.

    ``TenantService._probe`` takes a STAGE MAP (it is called mid-create, before a ``Tenant``
    exists) and the seam takes a ``Tenant``, so the bridge is where those two shapes meet. A
    rename or a reorder upstream must fail HERE, as a test, rather than as a runtime
    ``TypeError`` on the tenant CREATE path.

    The bridge threads ``previous`` / ``admin_unresolved``. ``previous`` is read from the STORE
    (``_get(tenant.id)``), not from the passed
    tenant — see the next test for why that distinction is load-bearing."""
    ts = MagicMock(spec=TenantService)
    ts._probe.return_value = ({"can_discover": True}, "federation")
    stored = _dbx_tenant()
    ts._get.return_value = stored  # the STORED record is the carry-over ``previous``
    tenant = _dbx_tenant()

    assert _tenant_probe(ts)(tenant, ("id", "sec")) == ({"can_discover": True}, "federation")
    ts._get.assert_called_once_with(tenant.id)
    ts._probe.assert_called_once_with(tenant.stages, ("id", "sec"), previous=stored, admin_unresolved=False)


def test_the_bridge_reads_previous_from_the_store_not_from_the_passed_tenant():
    """The ``previous`` trap, guarded as behaviour.

    At CREATE the record is not yet persisted, so ``_get(tenant.id)`` is ``None`` — and ``_probe``
    must see ``previous=None`` (its create arm), NOT the freshly-built ``tenant`` whose empty
    capabilities would make the carry-over arm hand back the AWS answer for a Databricks tenant."""
    ts = MagicMock(spec=TenantService)
    ts._probe.return_value = ({"can_discover": True}, "federation")
    ts._get.return_value = None  # nothing persisted under this id yet (create)
    tenant = _dbx_tenant()

    _tenant_probe(ts)(tenant, (None, None))
    _, kwargs = ts._probe.call_args
    assert kwargs["previous"] is None


def test_the_bridge_flags_an_unresolved_admin_secret():
    """``admin_unresolved`` is True iff the tenant HAS a stored account-admin secret but the
    resolved credential pair is incomplete — the signal ``_probe`` uses to carry the account-level
    flags over instead of reporting them False on an unreadable secret."""
    ts = MagicMock(spec=TenantService)
    ts._probe.return_value = ({}, "invoke_unavailable")
    ts._get.return_value = _dbx_tenant()
    tenant = _dbx_tenant()
    tenant.account_admin_secret_arn = "arn:aws:secretsmanager:us-east-1:x:secret:admin"

    _tenant_probe(ts)(tenant, (None, None))  # a stored secret, but no resolved creds
    _, kwargs = ts._probe.call_args
    assert kwargs["admin_unresolved"] is True


def test_declaration_comes_from_the_registry_not_from_the_adapter():
    """Identity, not equality: the adapter must not build its own declaration — a second
    copy of the support matrix is exactly the drift the registry removed."""
    assert _aws_adapter().declaration() is declaration_for(TenantPlatform.AWS)
    assert _dbx_adapter().declaration() is declaration_for(TenantPlatform.DATABRICKS)


@pytest.mark.asyncio
async def test_aws_probe_of_a_roleless_tenant_is_the_empty_pair():
    """The grandfather case: a stage that names no ``deploy_role_arn`` is deploy-in-place
    — nothing to assume, so no key is written and the pair is ``({}, "")``, exactly as it rendered
    before probing existed. ``_aws_tenant()``'s stage carries no roles."""
    assert await _aws_adapter().probe(_aws_tenant()) == ({}, "")


def test_probe_aws_reach_writes_one_bool_per_named_deploy_role_in_sorted_order():
    """The real per-stage reach probe: ``can_deploy_reach:<stage>``
    for each stage NAMING a deploy role, from a fake reach prober so no STS is touched.
    ``binding_mode`` is not this function's concern — the adapter pairs the dict with ``""``.

    The push role is NOT probed: its trust policy names GitHub's OIDC provider, never
    AGP, so there is nothing honest to assert about it here."""
    from services.platform_adapters.aws import probe_aws_reach

    stages = {
        "prod": TenantStageConfig(
            account_id="222222222222",
            deploy_role_arn="arn:aws:iam::222222222222:role/agp-deployment-prod",
            push_role_arn="arn:aws:iam::222222222222:role/prod-agent-ecr-push",
        ),
        # deploy-in-place: no role, so no key — not a False.
        "dev": TenantStageConfig(account_id="111111111111"),
    }
    seen: list = []

    def fake_reach(role_arn, region):
        seen.append(role_arn)
        return (True, "")

    caps = probe_aws_reach(stages, reach_prober=fake_reach)

    assert caps == {"can_deploy_reach:prod": True}
    assert "can_deploy_reach:dev" not in caps  # the roleless stage wrote nothing
    # only the deploy role is ever handed to the reach prober
    assert seen == ["arn:aws:iam::222222222222:role/agp-deployment-prod"]


@pytest.mark.asyncio
async def test_aws_provision_runtime_binding_delegates_to_the_agentcore_half():
    identity = MagicMock(spec=AgentIdentityService)
    identity.provision_runtime = AsyncMock(return_value=_agent())
    agent = _agent(agent_arn="arn:aws:bedrock-agentcore:us-east-1:x:runtime/r-1")

    assert await _aws_adapter(identity).provision_runtime_binding(agent) is None
    identity.provision_runtime.assert_awaited_once_with(agent)


def test_aws_runtime_status_delegates_with_the_stage():
    identity = MagicMock(spec=AgentIdentityService)
    identity.runtime_status.return_value = RuntimeStatusWithEnv(**_status("ready").model_dump())
    agent = _agent()

    result = _aws_adapter(identity).runtime_status(agent, "prod")

    # POSITIONAL: the collaborator's own signature takes ``stage`` positionally, and a keyword
    # here would make a bare delegation read as a translation.
    identity.runtime_status.assert_called_once_with(agent, "prod")
    assert result.status == "ready"


@pytest.mark.asyncio
async def test_databricks_probe_delegates_to_the_injected_probe_seam():
    probe = MagicMock(return_value=({"can_discover": True}, "federation"))
    tenant = _dbx_tenant()

    assert await _dbx_adapter(probe=probe).probe(tenant, ("admin-id", "admin-secret")) == (
        {"can_discover": True},
        "federation",
    )
    probe.assert_called_once_with(tenant, ("admin-id", "admin-secret"))


@pytest.mark.asyncio
async def test_databricks_probe_passes_an_absent_credential_through_as_a_pair():
    """``None`` means "resolve the stored credential"; the seam's callable always sees the
    two-slot shape ``tenant_service._probe`` already takes, so the adapter normalizes rather
    than making every implementation handle two input shapes."""
    probe = MagicMock(return_value=({}, "invoke_unavailable"))
    await _dbx_adapter(probe=probe).probe(_dbx_tenant())
    assert probe.call_args.args[1] == (None, None)


@pytest.mark.asyncio
async def test_databricks_provision_runtime_binding_delegates_to_the_platform_half():
    dbx = MagicMock(spec=DatabricksIdentityService)
    agent = _agent(platform=Platform.DATABRICKS, runtime_handle=APP_URL, runtime_kind="app")
    dbx.provision_databricks_runtime = AsyncMock(return_value=agent)

    assert await _dbx_adapter(dbx).provision_runtime_binding(agent) is None
    dbx.provision_databricks_runtime.assert_awaited_once_with(agent)


def test_databricks_runtime_status_rewraps_the_wire_model_with_no_env_evidence():
    """The Databricks producer returns the plain wire model; the internal carrier's
    ``environment_variables`` stays ``None`` — a Databricks app holds no backend-injected MCP
    env, so ``None`` ("this read holds no evidence") is the true claim and ``{}`` ("its env is
    empty") would make the reconciler read a structural absence as a wipe."""
    dbx = MagicMock(spec=DatabricksIdentityService)
    dbx.runtime_status.return_value = _status("ready")
    agent = _agent(platform=Platform.DATABRICKS, runtime_handle=APP_URL, runtime_kind="app")

    result = _dbx_adapter(dbx).runtime_status(agent, "dev")

    dbx.runtime_status.assert_called_once_with(agent, stage="dev")
    assert isinstance(result, RuntimeStatusWithEnv)
    assert (result.status, result.environment_variables) == ("ready", None)


# The invoke ROUTE CONTRACT stays in ``tests/test_invoke_route.py`` end-to-end through the seam,
# and the Databricks leg's own unit tests are in ``tests/test_databricks_invoke.py``. What is
# asserted HERE is only what the SEAM owes: that the AWS adapter carries the AgentCore body it
# was given (including the stage→ARN resolution, which is the one edit the relocation forced),
# and that the Databricks adapter is the thin pass-through it claims to be.


def _invoke_req(prompt: str = "triage this claim"):
    """The route's own request model — never a stand-in, since it is the pinned parameter type."""
    from api.routes.agents import InvokeRequest

    return InvokeRequest(prompt=prompt)


def _principal(oid: str | None = "viewer-oid"):
    from core.rbac import Principal, Role

    return Principal(oid=oid, email="maria.bauer@example.com", role=Role.VIEWER, raw_claims={})


class _FakeAsyncClient:
    """``httpx.AsyncClient``'s async-CM + ``post`` surface, recording the call."""

    def __init__(self, response, capture):
        self._response = response
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        self._capture["url"] = url
        self._capture["kwargs"] = kwargs
        return self._response


def _sse_response(text: str = 'data: {"result": "ok"}\n\n'):
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"content-type": "text/event-stream"}
    resp.text = text
    return resp


_ARN_DEV = "arn:aws:bedrock-agentcore:eu-central-1:111111111111:runtime/agent-dev"
_ARN_SCALAR = "arn:aws:bedrock-agentcore:us-east-1:111111111111:runtime/agent-last"


@pytest.mark.asyncio
async def test_aws_invoke_posts_the_obo_token_to_the_agentcore_runtime():
    """The relocated AgentCore leg, exercised through the seam.

    Region comes from ARN segment 3, the OBO token is the bearer, and the
    session header is AgentCore's own protocol element — the Databricks leg deliberately does not
    send it."""
    capture: dict = {}
    agent = _agent(agent_arn=_ARN_SCALAR)

    with pytest.MonkeyPatch.context() as mp:
        import httpx

        mp.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeAsyncClient(_sse_response(), capture))
        result = await _aws_adapter().invoke(agent, _invoke_req(), _principal(), "obo-agent-token")

    assert result["response"] == "ok"
    assert capture["url"] == (
        "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/"
        "arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A111111111111%3Aruntime%2Fagent-last"
        "/invocations?qualifier=DEFAULT"
    )
    headers = capture["kwargs"]["headers"]
    assert headers["Authorization"] == "Bearer obo-agent-token"
    assert len(headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"]) >= 33
    assert capture["kwargs"]["json"] == {
        "prompt": "triage this claim",
        "user_oid": "viewer-oid",
    }


@pytest.mark.asyncio
async def test_aws_invoke_resolves_the_arn_from_the_stage():
    """``stage`` SELECTS the runtime, through ``runtime_arns()`` — the same resolver
    ``runtime_status`` reads and the same one the route's 404 gate consults, so the invoke surface
    and the status surface cannot disagree about which runtime a stage names. ``None`` keeps the
    scalar (whichever stage deployed last)."""
    capture: dict = {}
    agent = _agent(agent_arn=_ARN_SCALAR)
    agent.agent_arns = {"dev": _ARN_DEV}

    with pytest.MonkeyPatch.context() as mp:
        import httpx

        mp.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeAsyncClient(_sse_response(), capture))
        await _aws_adapter().invoke(agent, _invoke_req(), _principal(), "obo", "dev")
        staged = capture["url"]
        await _aws_adapter().invoke(agent, _invoke_req(), _principal(), "obo")

    assert "eu-central-1" in staged and "agent-dev" in staged
    assert "us-east-1" in capture["url"] and "agent-last" in capture["url"]


@pytest.mark.asyncio
async def test_aws_invoke_refuses_a_malformed_arn_as_a_502_not_a_500():
    """A record whose ARN has no region segment would ``IndexError`` in the URL build. Behind the
    route's 409 provisioned-gate that is low-likelihood, but an unhandled 500 on the invoke
    surface is the expensive kind of cheap-to-prevent."""
    with pytest.raises(InvokeError) as excinfo:
        await _aws_adapter().invoke(
            _agent(agent_arn="arn:aws:bedrock-agentcore"), _invoke_req(), _principal(), "obo"
        )
    assert (excinfo.value.status_code, excinfo.value.detail) == (
        502,
        "agent ARN is malformed",
    )


@pytest.mark.parametrize(
    "region",
    [
        # One `PUT /agents/{id}` write and the OBO'd bearer token is POSTed to
        # `https://bedrock-agentcore.evil.example.com/x.amazonaws.com/…`.
        "evil.example.com/x",
        "evil.example.com",
        "us-east-1.evil.example.com",  # a plausible-looking prefix on a foreign host
        "attacker@us-east-1",  # userinfo, the classic allowlist bypass
        "us-east-1\ninjected",  # why `fullmatch`: `$` also matches before a newline
        "us-east-1/",
        "US-EAST-1",  # the shape is lowercase; an uppercase host is not it
        "us-east",  # no numeric suffix
        "useast1",
    ],
)
@pytest.mark.asyncio
async def test_aws_invoke_refuses_an_arn_whose_region_is_not_a_region(region):
    """The AWS twin of the ``workspace_url`` check.

    ``agent_arn`` is caller-settable via ``PUT /agents/{id}`` (no validator, repo-wide) and its
    segment 3 is interpolated into the HOST of the request that carries the OBO'd bearer token.
    So it is the same class the app-URL allowlist closed on the two Databricks app fields: a
    caller-settable field that becomes an outbound token target.

    THE ASSERTION THAT MATTERS is that httpx was never reached — patched with a client that FAILS
    the test if it is called, so "refused" cannot be satisfied by a refusal that happens after the
    token is already on the wire. The detail carries the fixed kind and never the ARN, which is
    attacker-supplied text that can also name an account id.
    """

    def _explode(*a, **k):
        raise AssertionError("a bearer token was sent to an unvalidated host")

    with pytest.MonkeyPatch.context() as mp:
        import httpx

        mp.setattr(httpx, "AsyncClient", _explode)
        with pytest.raises(InvokeError) as excinfo:
            await _aws_adapter().invoke(
                _agent(agent_arn=f"arn:aws:bedrock-agentcore:{region}:111111111111:runtime/r-1"),
                _invoke_req(),
                _principal(),
                "obo-agent-token",
            )

    assert excinfo.value.status_code == 502
    assert excinfo.value.detail == "the agent could not be invoked [agent_arn_invalid]"
    assert region not in excinfo.value.detail


@pytest.mark.parametrize(
    "region",
    ["us-east-1", "eu-central-1", "ap-southeast-4", "us-gov-west-1", "il-central-1"],
)
@pytest.mark.asyncio
async def test_aws_invoke_still_accepts_every_real_region_shape(region):
    """THE REGRESSION HALF. An allowlist tightened until it refuses a live region is an
    outage, and AgentCore is offered in commercial AND GovCloud regions — so the multi-word and
    two-digit forms are pinned, not assumed. The region reaches the URL host unchanged."""
    capture: dict = {}
    agent = _agent(agent_arn=f"arn:aws:bedrock-agentcore:{region}:111111111111:runtime/r-1")

    with pytest.MonkeyPatch.context() as mp:
        import httpx

        mp.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeAsyncClient(_sse_response(), capture))
        result = await _aws_adapter().invoke(agent, _invoke_req(), _principal(), "obo")
        assert result["response"] == "ok"

    assert capture["url"].startswith(f"https://bedrock-agentcore.{region}.amazonaws.com/")


@pytest.mark.asyncio
async def test_databricks_invoke_delegates_to_the_relocated_leg_and_ignores_the_stage():
    """A THIN pass-through, and the missing ``stage`` argument is the assertion.

    A Databricks App has no per-stage runtime identity — its hostname carries no workspace
    identity at all — so forwarding a stage would invite the leg to answer a stage-scoped
    question with the one app it has. That fabrication is exactly what
    ``_resolve_databricks_workspace`` fails closed rather than commit."""
    import services.databricks_invoke as leg

    agent = _agent(platform=Platform.DATABRICKS, runtime_handle=APP_URL, runtime_kind="app")
    req, principal = _invoke_req(), _principal()
    delegate = AsyncMock(return_value={"response": "hi"})

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(leg, "invoke_databricks", delegate)
        result = await _dbx_adapter(None).invoke(agent, req, principal, "obo-token", "dev")

    assert result == {"response": "hi", "session_id": None, "tool_calls": None}
    delegate.assert_awaited_once_with(agent, req, principal, "obo-token")


@pytest.mark.asyncio
async def test_a_missing_collaborator_raises_on_a_write():
    """A write may not silently no-op: an agent left 'pending' with no error is invisible to
    the recovery affordance that reads 'failed'."""
    agent = _agent(platform=Platform.DATABRICKS, runtime_handle=APP_URL, runtime_kind="app")
    with pytest.raises(AdapterError) as excinfo:
        await _dbx_adapter(None).provision_runtime_binding(agent)
    assert excinfo.value.kind == "platform_unsupported"


@pytest.mark.asyncio
async def test_a_missing_probe_seam_raises_rather_than_writing_the_aws_empty_pair():
    """``({}, "")`` is the AWS answer. Returning it for a Databricks tenant would persist a
    binding mode that says "no binding needed" onto a tenant whose whole invoke path depends
    on one — a fabricated capability, not a fail-closed one."""
    with pytest.raises(AdapterError) as excinfo:
        await _dbx_adapter(probe=None).probe(_dbx_tenant())
    assert excinfo.value.kind == "platform_unsupported"


def test_a_missing_collaborator_degrades_on_a_read_and_is_never_failed():
    """``unknown``, not ``not_deployed`` and not ``failed``: nothing was wired to ask, which
    is neither a positive claim that nothing runs nor evidence that the runtime is broken."""
    agent = _agent(platform=Platform.DATABRICKS, runtime_handle=APP_URL, runtime_kind="app")
    result = _dbx_adapter(None).runtime_status(agent, "dev")
    assert result.status == "unknown"
    assert result.environment_variables is None
    assert result.detail == _NO_READER_DETAIL


def test_a_handle_less_record_is_not_deployed_even_with_no_reader_wired():
    """THE ORDER OF THE TWO GUARDS. The no-reader degrade must NOT shadow
    the local answer: a record naming no ``runtime_handle`` was never bound to anything, which is
    COMPLETE negative evidence AGP already holds — no reader is needed to state it.

    Concrete regression this pins: ``scripts/seed_agents.py`` ships ``platform: "databricks"``
    records with no handle, and in an AgentCore-only deployment (no Databricks collaborator at
    all, so the platform is absent from the adapter dict) every one of them read
    ``unknown``/"no Databricks reader is configured" — losing the metadata-only fall-through on
    exactly the platform whose collaborator is optional."""
    result = _dbx_adapter(None).runtime_status(_agent(platform=Platform.DATABRICKS))

    assert result.status == "not_deployed"
    assert result.detail == "no runtime has been deployed yet"
    assert result.detail != _NO_READER_DETAIL
    assert result.environment_variables is None


def test_the_degraded_detail_strings_are_pinned():
    """Detail text is user-visible copy, so both sentences are pinned rather than diffed.

    ``_NO_READER_DETAIL`` is byte-identical to the sentence ``AgentIdentityService`` emits today
    (the frontend pins it). ``_READ_FAILED_DETAIL`` DELIBERATELY diverges from that module's
    ``_PROBE_UNREACHABLE_DETAIL`` ("control plane unreachable"), which is the AWS-shaped claim and
    untrue of a workspace that denied or timed out — AGP's control plane is not implicated. This
    assert is the declaration: the change is on the wire, and it cannot change silently."""
    assert _NO_READER_DETAIL == ("runtime status could not be read (no Databricks reader is configured)")
    assert _READ_FAILED_DETAIL == ("runtime status could not be read (the workspace could not be reached)")
    assert "control plane" not in _READ_FAILED_DETAIL


def test_a_raising_collaborator_degrades_on_a_read():
    """Defense in depth over a producer that already promises never to raise — a 5xx from a
    status read blanks the fleet view, and the fallback value here is honest."""
    dbx = MagicMock(spec=DatabricksIdentityService)
    dbx.runtime_status.side_effect = RuntimeError("boom")
    agent = _agent(platform=Platform.DATABRICKS, runtime_handle=APP_URL, runtime_kind="app")

    result = _dbx_adapter(dbx).runtime_status(agent, "dev")
    assert (result.status, result.detail) == ("unknown", _READ_FAILED_DETAIL)


def test_a_raising_collaborator_never_leaks_its_message():
    dbx = MagicMock(spec=DatabricksIdentityService)
    dbx.runtime_status.side_effect = RuntimeError("workspace https://secret.example/leak")
    agent = _agent(platform=Platform.DATABRICKS, runtime_handle=APP_URL, runtime_kind="app")

    result = _dbx_adapter(dbx).runtime_status(agent, "dev")
    assert "secret.example" not in (result.detail or "")


@pytest.mark.parametrize("platform", [TenantPlatform.AWS, TenantPlatform.DATABRICKS])
def test_the_metadata_only_fallthrough_is_not_deployed_on_both_platforms(platform):
    """A record with no runtime anywhere reads ``not_deployed`` WITHOUT touching a platform.

    The REAL services over inert doubles, deliberately: this fall-through is a documented
    honesty rule of the code the adapters wrap, and asserting it against a mock would assert
    only that the mock was called."""
    if platform is TenantPlatform.AWS:
        adapter = _aws_adapter(
            AgentIdentityService(
                graph=MagicMock(),
                registry=MagicMock(),
                control_client=MagicMock(),
                tenant_id="entra-tenant",
                login_base="https://login.microsoftonline.com",
            )
        )
        agent = _agent()
    else:
        adapter = _dbx_adapter(
            DatabricksIdentityService(
                databricks=MagicMock(), registry=MagicMock(), secrets_client=MagicMock()
            )
        )
        agent = _agent(platform=Platform.DATABRICKS)

    assert adapter.runtime_status(agent).status == "not_deployed"


def _dbx_agent(
    *,
    binding_mode: TenantBindingMode | None = None,
    databricks_sp_id: str | None = None,
    databricks_sp_secret_arn: str | None = None,
    entra_app_id: str | None = "agent-app-guid",
    entra_app_audience: str | None = "api://agp-agent-rec-abc123",
) -> Agent:
    now = datetime.now(timezone.utc)
    return Agent(
        id="rec-abc123",
        name="Claims Triage DE",
        purpose="",
        lifecycle_state=LifecycleState.APPROVED,
        platform=Platform.DATABRICKS,
        auth_type=AgentAuthType.ENTRA,
        tenant_id="ten-2",
        runtime_handle=APP_URL,
        runtime_kind="app",
        binding_mode=binding_mode,
        databricks_sp_id=databricks_sp_id,
        databricks_sp_secret_arn=databricks_sp_secret_arn,
        entra_app_id=entra_app_id,
        entra_app_audience=entra_app_audience,
        created_at=now,
        updated_at=now,
        created_by="maria.bauer@example.com",
    )


def test_databricks_named_artifacts_maps_the_federation_audience_as_blocking():
    """The map is pinned by the teardown asymmetry: the account-wide audience is the customer's
    live trust state, so its removal must BLOCK the delete (the one-call rule lives in the
    removal, not the enumeration)."""
    agent = _dbx_agent(binding_mode="federation")
    artifacts = _dbx_adapter().named_artifacts(agent)

    audience = [a for a in artifacts if a.kind == "federation_audience"]
    assert len(audience) == 1
    assert audience[0].policy is ArtifactPolicy.BLOCKING
    # ref is a SAFE identifier, never a secret ARN.
    assert audience[0].ref == "agent-app-guid"
    # A federation record with no SP names only the audience.
    assert [a.kind for a in artifacts] == ["federation_audience"]


def test_databricks_named_artifacts_maps_sp_secret_residue_as_best_effort():
    """sp_secret mode: the per-agent secret and the surviving service principal are OURS, in our
    account — best-effort, never gating the record."""
    agent = _dbx_agent(
        binding_mode="sp_secret",
        databricks_sp_id="sp-app-id-1",
        databricks_sp_secret_arn="arn:aws:secretsmanager:us-east-1:000000000000:secret:x/rec-abc123-Ab12Cd",
    )
    artifacts = {a.kind: a for a in _dbx_adapter().named_artifacts(agent)}

    assert artifacts["agent_secret"].policy is ArtifactPolicy.BEST_EFFORT
    assert artifacts["platform_sp"].policy is ArtifactPolicy.BEST_EFFORT
    # sp_secret mode never names the federation audience.
    assert "federation_audience" not in artifacts
    # No secret ARN reaches a ref (it carries the AWS account id).
    assert artifacts["agent_secret"].ref == "rec-abc123"
    assert artifacts["platform_sp"].ref == "sp-app-id-1"


def test_databricks_named_artifacts_is_empty_for_a_never_provisioned_record():
    """No binding mode, no SP pointers ⇒ nothing to tear down — the property that keeps a
    registered-but-never-provisioned Databricks agent deletable."""
    assert _dbx_adapter().named_artifacts(_dbx_agent(binding_mode=None)) == []


def test_databricks_named_artifacts_enumerates_without_a_collaborator():
    """A READ of the record's own fields, so it answers even on a deployment wired for NO
    Databricks — which is what lets the delete route detect a blocking artifact it cannot
    remove and REFUSE rather than orphan the customer's trust entry."""
    artifacts = _dbx_adapter(databricks_identity=None).named_artifacts(_dbx_agent(binding_mode="federation"))
    assert [a.kind for a in artifacts] == ["federation_audience"]


def test_aws_named_artifacts_is_empty_agentcore_names_no_platform_artifact():
    """AgentCore's identity teardown is the platform-NEUTRAL registered-agent cascade and its
    runtime teardown is the repo cascade — neither is an adapter artifact, so the honest answer
    is the empty list and an AgentCore delete is behaviourally unchanged."""
    assert _aws_adapter().named_artifacts(_agent()) == []


@pytest.mark.asyncio
async def test_databricks_remove_artifact_delegates_to_the_service():
    dbx = MagicMock(spec=DatabricksIdentityService)
    dbx.remove_databricks_artifact = AsyncMock(return_value=None)
    agent = _dbx_agent(binding_mode="federation")
    artifact = Artifact("federation_audience", "agent-app-guid", ArtifactPolicy.BLOCKING)

    await _dbx_adapter(dbx).remove_artifact(agent, artifact)

    dbx.remove_databricks_artifact.assert_awaited_once_with(agent, artifact)


@pytest.mark.asyncio
async def test_databricks_remove_artifact_raises_on_a_missing_collaborator():
    """The write asymmetry: enumeration degrades to a local read, but REMOVAL with no
    collaborator RAISES, so the route refuses the delete of a record it cannot clean up."""
    artifact = Artifact("federation_audience", "agent-app-guid", ArtifactPolicy.BLOCKING)
    with pytest.raises(AdapterError) as err:
        await _dbx_adapter(databricks_identity=None).remove_artifact(_dbx_agent(), artifact)
    assert err.value.kind == "artifact_unremovable"


@pytest.mark.asyncio
async def test_aws_remove_artifact_raises_agentcore_owns_no_platform_artifact():
    artifact = Artifact("entra_identity", "x", ArtifactPolicy.BLOCKING)
    with pytest.raises(AdapterError) as err:
        await _aws_adapter().remove_artifact(_agent(), artifact)
    assert err.value.kind == "artifact_unremovable"
