"""Tests for the ``runtime_catalog`` discovery seam (E29/T3 — contract C-3).

Three things are under test and they are deliberately independent:

1. **The seam** — ``DiscoveredAgent``'s frozen projection shape and ``RuntimeCatalog``'s
   Protocol conformance, checked structurally against both adapters (``repo_provider``'s
   ``test_repo_provider`` idiom: a Protocol is not enforced at runtime, so a drifted
   parameter name would ship a "conforming" adapter no caller can call).
2. **``DatabricksCatalog``** against a fake ``DatabricksWorkspaceService`` that answers the
   REAL response shapes from the platform research (§2.1/§2.2) and REFUSES to be more
   generous than reality — a fake that invents an ``url`` on every app makes the
   defensive-read tests unable to fail.
3. **``AgentCoreCatalog``** against fake boto3 clients, pinning the assume-role idiom
   (``deploy_role_arn`` present ⇒ assume, empty ⇒ in-place) and paginated reads.

``already_registered`` is tested as what it is — a pure matching function over handles, not
an adapter concern. Neither adapter may reach the registry: a discovery listing that also
queries AGP's own store is a seam with two responsibilities.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Optional

import pytest

from models.tenant import (
    DatabricksStageConfig,
    Tenant,
    TenantPlatform,
    TenantStageConfig,
)
from services.databricks_workspace_service import DatabricksError
from services.runtime_catalog import (
    AgentCoreCatalog,
    CatalogError,
    DatabricksCatalog,
    DiscoveredAgent,
    RuntimeCatalog,
    mark_already_registered,
)

WS = "https://dbc-test.cloud.databricks.com"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:agp-dev/dbx-abc"


# --------------------------------------------------------------------------- #
# Fixtures — tenants
# --------------------------------------------------------------------------- #

def _dbx_tenant(*, stage="dev", secret_arn=SECRET_ARN, **stage_overrides) -> Tenant:
    cfg = {
        "workspace_url": WS,
        "workspace_id": "1234567890123456",
        "sp_client_id": "sp-abc",
        "sp_client_secret_arn": secret_arn,
    }
    cfg.update(stage_overrides)
    return Tenant(
        id="ten-dbx", name="Analytics", line_of_business="A", entra_group_ids=["g"],
        platform=TenantPlatform.DATABRICKS,
        stages={stage: DatabricksStageConfig(**cfg)},
        created_by="u", created_at="t", updated_at="t",
    )


def _aws_tenant(*, stage="dev", deploy_role_arn="", region="us-east-1") -> Tenant:
    return Tenant(
        id="ten-aws", name="Retail", line_of_business="R", entra_group_ids=["g"],
        stages={stage: TenantStageConfig(
            account_id="111111111111", region=region, deploy_role_arn=deploy_role_arn)},
        created_by="u", created_at="t", updated_at="t",
    )


# --------------------------------------------------------------------------- #
# Fakes — a Databricks client that answers the REAL shapes and no more
# --------------------------------------------------------------------------- #

class _FakeWorkspace:
    """Answers ``mint_m2m_token`` / ``list_apps`` / ``list_serving_endpoints`` only.

    STRICT on purpose: ``mint_m2m_token`` refuses a blank client id or secret (a real
    workspace answers 400), because the whole point of resolving the stage's secret from
    Secrets Manager is that a catalog with no credential must FAIL rather than list nothing
    and look healthy."""

    def __init__(self, *, apps=None, endpoints=None, apps_error=None, endpoints_error=None,
                 mint_error=None):
        self._apps = apps if apps is not None else []
        self._endpoints = endpoints if endpoints is not None else []
        self._apps_error = apps_error
        self._endpoints_error = endpoints_error
        self._mint_error = mint_error
        self.calls: list[tuple] = []

    async def mint_m2m_token(self, workspace_url, client_id, client_secret):
        self.calls.append(("mint", workspace_url, client_id, client_secret))
        if self._mint_error:
            raise self._mint_error
        if not client_id or not client_secret:
            raise DatabricksError("Databricks rejected the request", kind="invalid_client")
        return "dbx-token"

    async def list_apps(self, workspace_url, token):
        self.calls.append(("apps", workspace_url, token))
        if self._apps_error:
            raise self._apps_error
        return list(self._apps)

    async def list_serving_endpoints(self, workspace_url, token):
        self.calls.append(("endpoints", workspace_url, token))
        if self._endpoints_error:
            raise self._endpoints_error
        return list(self._endpoints)


class _FakeSecrets:
    """A minimal Secrets Manager stand-in: ``get_secret_value`` by SecretId, or raise."""

    def __init__(self, bodies: Optional[dict] = None, error=None):
        self._bodies = bodies or {}
        self._error = error
        self.requested: list[str] = []

    def get_secret_value(self, SecretId: str):  # noqa: N803 — boto3's parameter name
        self.requested.append(SecretId)
        if self._error:
            raise self._error
        if SecretId not in self._bodies:
            raise KeyError(SecretId)
        return {"SecretString": self._bodies[SecretId]}


def _dbx_catalog(workspace, *, secret_body='{"sp_client_secret": "s3cr3t"}', secrets=None):
    return DatabricksCatalog(
        workspace=workspace,
        secrets_client=secrets or _FakeSecrets({SECRET_ARN: secret_body}),
    )


# Real-shaped app + endpoint records (platform research §2.1 / §2.2).
def _app(name="fraud-agent", url=f"{WS}/apps/fraud-agent", state="RUNNING", creator="a@b.com"):
    record = {
        "name": name,
        "create_time": "2026-08-01T00:00:00Z",
        "creator": creator,
        "status": {"state": state, "message": "App is running"},
        "oauth2_app_client_id": "oauth-client-1",
    }
    if url is not None:
        record["url"] = url
    return record


def _endpoint(name="claims-agent", entity_type="CUSTOM_MODEL", ready="READY",
              endpoint_url=f"{WS}/serving-endpoints/claims-agent", creator="a@b.com"):
    record = {
        "name": name,
        "id": "abcdef0123456789abcdef0123456789",
        "creator": creator,
        "state": {"ready": ready, "config_update": "NOT_UPDATING"},
        "config": {"served_entities": [
            {"name": "claims-1", "entity_name": "cat.sch.claims", "entity_type": entity_type},
        ]},
    }
    if endpoint_url is not None:
        record["endpoint_url"] = endpoint_url
    return record


# --------------------------------------------------------------------------- #
# 1. The seam itself
# --------------------------------------------------------------------------- #

def test_discovered_agent_fields_and_defaults():
    a = DiscoveredAgent(name="n", runtime_handle="h", kind="app", state="RUNNING")
    assert (a.created_by, a.already_registered) == ("", False)


def test_discovered_agent_is_frozen():
    """Evidence about a platform at a moment in time — a mutated probe result is a record
    that disagrees with what was observed (``RepoView``'s reasoning)."""
    a = DiscoveredAgent(name="n", runtime_handle="h", kind="app", state="s")
    with pytest.raises(FrozenInstanceError):
        a.name = "other"  # type: ignore[misc]


@pytest.mark.parametrize("adapter", [DatabricksCatalog, AgentCoreCatalog])
def test_adapters_conform_to_the_protocol_signature(adapter):
    """A Protocol is satisfied STRUCTURALLY and enforced nowhere at runtime, so the
    signature is compared method by method — a renamed parameter would otherwise ship an
    adapter that type-checks and that no caller can call (``test_repo_provider``'s idiom)."""
    import inspect

    expected = inspect.signature(RuntimeCatalog.list_agents)
    actual = inspect.signature(adapter.list_agents)
    assert list(actual.parameters) == list(expected.parameters)
    assert inspect.iscoroutinefunction(adapter.list_agents)


def test_protocol_is_runtime_checkable_against_both_adapters():
    assert isinstance(DatabricksCatalog(workspace=_FakeWorkspace()), RuntimeCatalog)
    assert isinstance(AgentCoreCatalog(), RuntimeCatalog)


# --------------------------------------------------------------------------- #
# 2. already_registered — a pure match over handles, not an adapter concern
# --------------------------------------------------------------------------- #

def test_mark_already_registered_flags_matching_handles():
    found = [
        DiscoveredAgent(name="a", runtime_handle="h-1", kind="app", state="RUNNING"),
        DiscoveredAgent(name="b", runtime_handle="h-2", kind="app", state="RUNNING"),
    ]
    marked = mark_already_registered(found, {"h-2"})
    assert [a.already_registered for a in marked] == [False, True]


def test_mark_already_registered_returns_new_objects_and_keeps_every_field():
    """Frozen ⇒ the flag cannot be set in place; the copies must carry everything else."""
    found = [DiscoveredAgent(name="a", runtime_handle="h-1", kind="app", state="RUNNING",
                             created_by="x@y.com")]
    marked = mark_already_registered(found, {"h-1"})
    assert marked[0] == DiscoveredAgent(
        name="a", runtime_handle="h-1", kind="app", state="RUNNING",
        created_by="x@y.com", already_registered=True,
    )
    assert found[0].already_registered is False  # the input is untouched


def test_mark_already_registered_ignores_blank_handles():
    """A registry agent with neither ``runtime_handle`` nor ``agent_arn`` contributes an
    empty string. If "" matched, every discovered record with an unreadable handle would be
    flagged as already governed — the most dangerous possible false positive."""
    found = [DiscoveredAgent(name="a", runtime_handle="", kind="app", state="s")]
    assert mark_already_registered(found, {""})[0].already_registered is False


# --------------------------------------------------------------------------- #
# 3. DatabricksCatalog
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_databricks_lists_apps_as_the_primary_shape():
    ws = _FakeWorkspace(apps=[_app()])
    agents = await _dbx_catalog(ws).list_agents(_dbx_tenant(), "dev")
    assert agents == [DiscoveredAgent(
        name="fraud-agent", runtime_handle=f"{WS}/apps/fraud-agent", kind="app",
        state="RUNNING", created_by="a@b.com",
    )]


@pytest.mark.asyncio
async def test_databricks_merges_apps_and_serving_endpoints():
    ws = _FakeWorkspace(apps=[_app()], endpoints=[_endpoint()])
    agents = await _dbx_catalog(ws).list_agents(_dbx_tenant(), "dev")
    assert [(a.name, a.kind) for a in agents] == [
        ("fraud-agent", "app"), ("claims-agent", "serving_endpoint"),
    ]


@pytest.mark.asyncio
async def test_databricks_reads_the_handle_and_never_constructs_it():
    """C-3's pin. The handle is what T7 will POST to, so a CONSTRUCTED one would send an
    invoke at a URL Databricks never published — the research notes ``endpoint_url`` is only
    populated for route-optimized endpoints, so guessing would be wrong most of the time."""
    ws = _FakeWorkspace(apps=[_app(url=f"{WS}/apps/renamed-elsewhere")])
    agents = await _dbx_catalog(ws).list_agents(_dbx_tenant(), "dev")
    # The handle follows the RESPONSE, not the app's name.
    assert agents[0].runtime_handle == f"{WS}/apps/renamed-elsewhere"


@pytest.mark.asyncio
async def test_databricks_skips_an_app_with_no_readable_url():
    """The Apps field names are UNVERIFIED until T12 (C-2's own warning). A record whose URL
    key AGP does not recognise is SKIPPED with a safe log — never a KeyError, and never a
    fabricated handle."""
    ws = _FakeWorkspace(apps=[_app(url=None), _app(name="ok")])
    agents = await _dbx_catalog(ws).list_agents(_dbx_tenant(), "dev")
    assert [a.name for a in agents] == ["ok"]


@pytest.mark.asyncio
async def test_databricks_accepts_app_url_as_the_alternate_key():
    """``url`` vs ``app_url`` is the exact ambiguity the research flagged. Both are READ —
    which is different from constructing one."""
    record = _app(url=None)
    record["app_url"] = f"{WS}/apps/alt"
    ws = _FakeWorkspace(apps=[record])
    agents = await _dbx_catalog(ws).list_agents(_dbx_tenant(), "dev")
    assert agents[0].runtime_handle == f"{WS}/apps/alt"


@pytest.mark.asyncio
async def test_databricks_filters_endpoints_to_custom_model_entities():
    """Foundation/external models are not agents. Only ``CUSTOM_MODEL`` served entities are
    (research §2.2), and a workspace's endpoint list is mostly the other kinds."""
    ws = _FakeWorkspace(endpoints=[
        _endpoint(name="agent-one", entity_type="CUSTOM_MODEL"),
        _endpoint(name="fm-endpoint", entity_type="FOUNDATION_MODEL"),
        _endpoint(name="ext-endpoint", entity_type="EXTERNAL_MODEL"),
        _endpoint(name="feature-spec", entity_type="FEATURE_SPEC"),
    ])
    agents = await _dbx_catalog(ws).list_agents(_dbx_tenant(), "dev")
    assert [a.name for a in agents] == ["agent-one"]


@pytest.mark.asyncio
async def test_databricks_excludes_reserved_databricks_prefixed_endpoints():
    """``databricks-`` is reserved for Databricks' own endpoints — a cheap, documented way
    to keep the foundation-model catalogue out of a customer's agent inventory."""
    ws = _FakeWorkspace(endpoints=[
        _endpoint(name="databricks-meta-llama-3-70b-instruct"),
        _endpoint(name="databricks-claims-agent"),
        _endpoint(name="our-claims-agent"),
    ])
    agents = await _dbx_catalog(ws).list_agents(_dbx_tenant(), "dev")
    assert [a.name for a in agents] == ["our-claims-agent"]


@pytest.mark.asyncio
async def test_databricks_endpoint_state_is_the_raw_ready_string():
    ws = _FakeWorkspace(endpoints=[_endpoint(ready="NOT_READY")])
    agents = await _dbx_catalog(ws).list_agents(_dbx_tenant(), "dev")
    assert agents[0].state == "NOT_READY"


@pytest.mark.asyncio
async def test_databricks_reads_the_data_plane_endpoint_url_when_the_top_level_one_is_absent():
    record = _endpoint(endpoint_url=None)
    record["data_plane_info"] = {
        "query_info": {"endpoint_url": f"{WS}/serving-endpoints/dp/invocations"}
    }
    ws = _FakeWorkspace(endpoints=[record])
    agents = await _dbx_catalog(ws).list_agents(_dbx_tenant(), "dev")
    assert agents[0].runtime_handle == f"{WS}/serving-endpoints/dp/invocations"


@pytest.mark.asyncio
async def test_databricks_skips_an_endpoint_with_no_readable_url():
    ws = _FakeWorkspace(endpoints=[_endpoint(endpoint_url=None), _endpoint(name="ok")])
    agents = await _dbx_catalog(ws).list_agents(_dbx_tenant(), "dev")
    assert [a.name for a in agents] == ["ok"]


@pytest.mark.asyncio
async def test_databricks_never_flags_already_registered_itself():
    """The adapters do not know about AGP's registry, by design — the route matches handles.
    A catalog that queried the registry would be a seam with two responsibilities."""
    ws = _FakeWorkspace(apps=[_app()], endpoints=[_endpoint()])
    agents = await _dbx_catalog(ws).list_agents(_dbx_tenant(), "dev")
    assert all(a.already_registered is False for a in agents)


@pytest.mark.asyncio
async def test_databricks_resolves_the_stage_secret_from_its_arn():
    ws = _FakeWorkspace(apps=[_app()])
    secrets = _FakeSecrets({SECRET_ARN: '{"sp_client_secret": "s3cr3t"}'})
    await _dbx_catalog(ws, secrets=secrets).list_agents(_dbx_tenant(), "dev")
    assert secrets.requested == [SECRET_ARN]
    assert ("mint", WS, "sp-abc", "s3cr3t") in ws.calls


@pytest.mark.asyncio
async def test_databricks_raises_a_safe_error_when_the_stage_has_no_secret_arn():
    """No credential ⇒ no listing. Answering an EMPTY list would tell an operator this
    workspace hosts no agents, which is a governance lie about an unconfigured tenant."""
    ws = _FakeWorkspace(apps=[_app()])
    with pytest.raises(CatalogError) as ei:
        await _dbx_catalog(ws).list_agents(_dbx_tenant(secret_arn=""), "dev")
    assert ei.value.kind == "no_credential"
    assert ws.calls == []  # nothing was even attempted


@pytest.mark.asyncio
async def test_databricks_raises_a_safe_error_when_the_secret_cannot_be_read():
    ws = _FakeWorkspace(apps=[_app()])
    secrets = _FakeSecrets(error=RuntimeError("AccessDeniedException: nope"))
    with pytest.raises(CatalogError) as ei:
        await _dbx_catalog(ws, secrets=secrets).list_agents(_dbx_tenant(), "dev")
    assert ei.value.kind == "credential_unreadable"
    assert "AccessDenied" not in str(ei.value)  # the upstream message never crosses


@pytest.mark.asyncio
async def test_databricks_surfaces_the_platform_error_kind_not_its_message():
    ws = _FakeWorkspace(mint_error=DatabricksError(
        "Databricks rejected the request (mint workspace token, status 401)",
        kind="invalid_client"))
    with pytest.raises(CatalogError) as ei:
        await _dbx_catalog(ws).list_agents(_dbx_tenant(), "dev")
    assert ei.value.kind == "invalid_client"


@pytest.mark.asyncio
async def test_databricks_raises_when_a_listing_fails_rather_than_shortening_the_answer():
    """``repo_provider``'s rule: a partial inventory presented as complete is worse than a
    failed step. Apps failing must not silently degrade to endpoints-only."""
    ws = _FakeWorkspace(
        apps_error=DatabricksError("Databricks rejected the request", kind="PERMISSION_DENIED"),
        endpoints=[_endpoint()],
    )
    with pytest.raises(CatalogError) as ei:
        await _dbx_catalog(ws).list_agents(_dbx_tenant(), "dev")
    assert ei.value.kind == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_databricks_empty_workspace_lists_nothing_without_error():
    """An SP with no app grants legitimately sees an empty list (research §5.3) — that is a
    successful empty answer, not a failure."""
    assert await _dbx_catalog(_FakeWorkspace()).list_agents(_dbx_tenant(), "dev") == []


@pytest.mark.asyncio
async def test_databricks_rejects_an_unknown_stage():
    with pytest.raises(CatalogError) as ei:
        await _dbx_catalog(_FakeWorkspace()).list_agents(_dbx_tenant(), "prod")
    assert ei.value.kind == "unknown_stage"


@pytest.mark.asyncio
async def test_databricks_refuses_an_aws_shaped_stage():
    """Platform dispatch is the CALLER's job, but an adapter handed the wrong shape must say
    so rather than ``AttributeError`` its way to a 500."""
    with pytest.raises(CatalogError) as ei:
        await _dbx_catalog(_FakeWorkspace()).list_agents(_aws_tenant(), "dev")
    assert ei.value.kind == "wrong_stage_shape"


@pytest.mark.asyncio
async def test_databricks_error_message_never_carries_the_secret():
    ws = _FakeWorkspace(apps_error=DatabricksError("boom", kind="bad_response"))
    with pytest.raises(CatalogError) as ei:
        await _dbx_catalog(ws, secret_body='{"sp_client_secret": "s3cr3t"}').list_agents(
            _dbx_tenant(), "dev")
    assert "s3cr3t" not in str(ei.value)


# --------------------------------------------------------------------------- #
# 4. AgentCoreCatalog
# --------------------------------------------------------------------------- #

class _FakeControl:
    """A ``bedrock-agentcore-control`` stand-in with ``list_agent_runtimes`` pagination."""

    def __init__(self, pages=None, error=None):
        self._pages = pages if pages is not None else [{"agentRuntimes": []}]
        self._error = error
        self.tokens: list[Optional[str]] = []

    def list_agent_runtimes(self, **kwargs):
        if self._error:
            raise self._error
        self.tokens.append(kwargs.get("nextToken"))
        return self._pages[len(self.tokens) - 1]


class _FakeSts:
    def __init__(self, error=None):
        self._error = error
        self.assumed: list[dict] = []

    def assume_role(self, **kwargs):
        if self._error:
            raise self._error
        self.assumed.append(kwargs)
        return {"Credentials": {
            "AccessKeyId": "AK", "SecretAccessKey": "SK", "SessionToken": "ST",
        }}


def _runtime(name="fraud", arn=None, status="READY"):
    return {
        "agentRuntimeArn": arn or f"arn:aws:bedrock-agentcore:us-east-1:111111111111:runtime/{name}",
        "agentRuntimeId": f"{name}-id",
        "agentRuntimeVersion": "1",
        "agentRuntimeName": name,
        "description": "d",
        "status": status,
    }


def _ac_catalog(control, sts=None, seen=None):
    """Build an ``AgentCoreCatalog`` whose control-client factory records how it was built —
    the assume-role idiom's observable effect is WHICH credentials the client gets."""
    def factory(*, region, credentials):
        if seen is not None:
            seen.append({"region": region, "credentials": credentials})
        return control

    return AgentCoreCatalog(sts_client=sts or _FakeSts(), control_factory=factory)


@pytest.mark.asyncio
async def test_agentcore_lists_runtimes_with_the_arn_as_the_handle():
    control = _FakeControl([{"agentRuntimes": [_runtime()]}])
    agents = await _ac_catalog(control).list_agents(_aws_tenant(), "dev")
    assert agents == [DiscoveredAgent(
        name="fraud",
        runtime_handle="arn:aws:bedrock-agentcore:us-east-1:111111111111:runtime/fraud",
        kind="agentcore_runtime", state="READY",
    )]


@pytest.mark.asyncio
async def test_agentcore_reads_the_arn_and_never_builds_one():
    """A constructed ARN would need an account id — which this repo forbids hardcoding, and
    which is exactly the value a caller must not have to know."""
    odd = "arn:aws:bedrock-agentcore:eu-west-1:222222222222:runtime/renamed-XYZ"
    control = _FakeControl([{"agentRuntimes": [_runtime(name="fraud", arn=odd)]}])
    agents = await _ac_catalog(control).list_agents(_aws_tenant(), "dev")
    assert agents[0].runtime_handle == odd


@pytest.mark.asyncio
async def test_agentcore_skips_a_runtime_with_no_arn():
    broken = _runtime(name="broken")
    broken.pop("agentRuntimeArn")
    control = _FakeControl([{"agentRuntimes": [broken, _runtime(name="ok")]}])
    agents = await _ac_catalog(control).list_agents(_aws_tenant(), "dev")
    assert [a.name for a in agents] == ["ok"]


@pytest.mark.asyncio
async def test_agentcore_pages_on_next_token():
    control = _FakeControl([
        {"agentRuntimes": [_runtime(name="a")], "nextToken": "t1"},
        {"agentRuntimes": [_runtime(name="b")]},
    ])
    agents = await _ac_catalog(control).list_agents(_aws_tenant(), "dev")
    assert [a.name for a in agents] == ["a", "b"]
    assert control.tokens == [None, "t1"]


@pytest.mark.asyncio
async def test_agentcore_refuses_a_repeated_pagination_token():
    """The exit condition is upstream-controlled. A constant token would spin this coroutine
    forever, holding a request slot — and a truncated inventory presented as complete is the
    governance lie the Databricks pager also refuses."""
    control = _FakeControl([
        {"agentRuntimes": [_runtime(name="a")], "nextToken": "same"},
        {"agentRuntimes": [_runtime(name="b")], "nextToken": "same"},
    ])
    with pytest.raises(CatalogError) as ei:
        await _ac_catalog(control).list_agents(_aws_tenant(), "dev")
    assert ei.value.kind == "pagination_overflow"


@pytest.mark.asyncio
async def test_agentcore_assumes_the_stages_deploy_role_when_one_is_set():
    """The ``runtime_build`` idiom: the stage's ``deploy_role_arn`` is what reaches the
    tenant's OWN account, and it is derived server-side from the tenant record."""
    control = _FakeControl([{"agentRuntimes": [_runtime()]}])
    sts, seen = _FakeSts(), []
    role = "arn:aws:iam::111111111111:role/agp-deployment-dev"
    catalog = _ac_catalog(control, sts=sts, seen=seen)
    await catalog.list_agents(
        _aws_tenant(deploy_role_arn=role, region="eu-west-1"), "dev")
    assert sts.assumed[0]["RoleArn"] == role
    assert seen[0]["region"] == "eu-west-1"          # the STAGE's region, not the platform's
    assert seen[0]["credentials"]["aws_access_key_id"] == "AK"


@pytest.mark.asyncio
async def test_agentcore_reads_in_place_when_the_stage_has_no_deploy_role():
    """Empty ``deploy_role_arn`` ⇒ deploy/read in-place, byte-for-byte the buildspec's rule.
    An empty role must not be assumed (a blank RoleArn is an error, not an identity)."""
    control = _FakeControl([{"agentRuntimes": [_runtime()]}])
    sts, seen = _FakeSts(), []
    await _ac_catalog(control, sts=sts, seen=seen).list_agents(_aws_tenant(), "dev")
    assert sts.assumed == []
    assert seen[0]["credentials"] is None


@pytest.mark.asyncio
async def test_agentcore_surfaces_an_assume_role_failure_as_a_safe_error():
    control = _FakeControl([{"agentRuntimes": []}])
    sts = _FakeSts(error=RuntimeError("AccessDenied: user is not authorized to sts:AssumeRole"))
    catalog = _ac_catalog(control, sts=sts)
    with pytest.raises(CatalogError) as ei:
        await catalog.list_agents(
            _aws_tenant(deploy_role_arn="arn:aws:iam::111111111111:role/r"), "dev")
    assert ei.value.kind == "assume_role_failed"
    assert "not authorized" not in str(ei.value)


@pytest.mark.asyncio
async def test_agentcore_surfaces_a_listing_failure_as_a_safe_error():
    control = _FakeControl(error=RuntimeError("ValidationException: bad shape"))
    with pytest.raises(CatalogError) as ei:
        await _ac_catalog(control).list_agents(_aws_tenant(), "dev")
    assert ei.value.kind == "listing_failed"
    assert "ValidationException" not in str(ei.value)


@pytest.mark.asyncio
async def test_agentcore_rejects_an_unknown_stage():
    with pytest.raises(CatalogError) as ei:
        await _ac_catalog(_FakeControl()).list_agents(_aws_tenant(), "prod")
    assert ei.value.kind == "unknown_stage"


@pytest.mark.asyncio
async def test_agentcore_refuses_a_databricks_shaped_stage():
    with pytest.raises(CatalogError) as ei:
        await _ac_catalog(_FakeControl()).list_agents(_dbx_tenant(), "dev")
    assert ei.value.kind == "wrong_stage_shape"


@pytest.mark.asyncio
async def test_agentcore_empty_account_lists_nothing_without_error():
    assert await _ac_catalog(_FakeControl()).list_agents(_aws_tenant(), "dev") == []


# --------------------------------------------------------------------------- #
# 5. CatalogError's kind is a SAFE code
# --------------------------------------------------------------------------- #

def test_catalog_error_kind_is_a_safe_code():
    """The route puts ``.kind`` in a 502 body. A Databricks ``PERMISSION_DENIED`` is safe;
    an upstream string carrying a workspace path is not, so the shape is enforced here."""
    import re

    for kind in ("no_credential", "credential_unreadable", "unknown_stage",
                 "wrong_stage_shape", "assume_role_failed", "listing_failed",
                 "pagination_overflow"):
        assert re.fullmatch(r"[A-Za-z_]{1,64}", kind)


# --------------------------------------------------------------------------- #
# FIX round 1 — the safe-code check is a fullmatch, EXECUTED (review MINOR)
#
# ``_SAFE_KIND.match`` accepted "PERMISSION_DENIED\n<anything>": ``$`` also matches just before a
# trailing newline, and ``.match`` only anchors the START. That value goes into a 502 body, so the
# smuggled second line would have crossed. The regex was always right; the CALL was not — which
# is why these cases are executed rather than asserted by inspection.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("hostile", [
    "PERMISSION_DENIED\ninjected",
    "PERMISSION_DENIED\n",
    "ok\r\nSet-Cookie: x=1",
    "kind with spaces",
    "PERMISSION_DENIED 403 /Workspace/secret",
    "arn:aws:iam::111111111111:role/r",
    "a" * 65,          # over the length bound
    "",
    "1nvalid",         # digits are not in the safe class
])
def test_catalog_error_normalises_an_unsafe_kind_to_unknown(hostile):
    assert CatalogError("failed", kind=hostile).kind == "unknown"


@pytest.mark.parametrize("safe", ["PERMISSION_DENIED", "unreachable", "no_credential", "a"])
def test_catalog_error_passes_a_genuinely_safe_kind_through(safe):
    """The normalisation must not be so aggressive that a real upstream code is lost — an
    operator seeing PERMISSION_DENIED learns something true."""
    assert CatalogError("failed", kind=safe).kind == safe
