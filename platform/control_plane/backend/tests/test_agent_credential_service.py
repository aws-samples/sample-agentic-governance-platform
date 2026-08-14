"""Tests for ``services.agent_credential_service`` — the Tier-2 AgentCore OAuth2
credential-provider orchestrator (E7, Task T-CRED-PROVIDER).

SECURITY-SENSITIVE / SECRET-HANDLING. The whole point of this service is that the
AGENT app's client secret (minted via Graph ``add_agent_password``) goes STRAIGHT into
AgentCore Identity's Token Vault (the ``MicrosoftOauth2`` credential provider's
``clientSecret``) and is NEVER logged, printed, persisted, or returned to a caller —
only the provider NAME (a non-secret) is persisted on the agent record. There is an
explicit test asserting the secret never reaches a log record.

ALL external collaborators are mocked — there are NO live AWS / Graph calls:
  - ``GraphService`` → an ``AsyncMock`` double whose ``add_agent_password`` returns a
    sentinel secret string.
  - the boto3 ``bedrock-agentcore-control`` client → a ``MagicMock`` whose
    ``create_oauth2_credential_provider`` records the call and
    ``get_oauth2_credential_provider`` either returns an existing provider (idempotent
    path) or raises ``ResourceNotFoundException`` (the get-or-create create path).

The repo is NOT in pytest-asyncio ``auto`` mode, so every async test is decorated
``@pytest.mark.asyncio`` explicitly. ``ensure_agent_credential_provider`` is now ASYNC
(T-CRED-ASYNC-FIX): it AWAITS the Graph calls directly on the running event loop (they use
``GraphService``'s shared loop-bound httpx client, so they MUST run on the loop that owns
it — the old ``asyncio.run``-in-a-worker-thread bridge broke that and silently 502'd) and
off-loads only its blocking boto3 control calls. So these tests await the method on the
test's own running loop — the exact condition that was broken. The async Graph collaborator
methods are ``AsyncMock``s; the boto3 control client stays a ``MagicMock``.

Contract pinned in the E7 plan, Task T-CRED-PROVIDER + research §3.2 (the
``CreateOauth2CredentialProvider`` shape: vendor ``MicrosoftOauth2``,
``microsoftOauth2ProviderConfig={clientId, clientSecret, tenantId}``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from models.agent import Agent, AuthType, LifecycleState, Platform
from services.agent_credential_service import AgentCredentialService

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------
TENANT_ID = "00000000-0000-0000-0000-000000000001"
REGION = "us-east-1"
PREFIX = "agp-agent-obo-"

# Distinct sentinels so we can assert the secret never leaks while the non-secret
# provider name + clientId DO surface.
SECRET_SENTINEL = "AGENT_CLIENT_SECRET_DO_NOT_LEAK"
AGENT_APP_CLIENT_ID = "agent-app-client-guid"
AGENT_APP_OBJECT_ID = "agent-app-object-id"


def _make_agent(
    *,
    agent_id: str = "rec-abc123",
    name: str = "Claims Triage DE",
    entra_app_id: str | None = AGENT_APP_CLIENT_ID,
    entra_app_object_id: str | None = None,
) -> Agent:
    now = datetime.now(timezone.utc)
    agent = Agent(
        id=agent_id,
        name=name,
        purpose="Triage inbound motor claims",
        lifecycle_state=LifecycleState.APPROVED,
        platform=Platform.AWS_BEDROCK,
        auth_type=AuthType.ENTRA,
        agent_arn=f"arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/{agent_id}",
        entra_app_id=entra_app_id,
        entra_sp_id=f"sp-{agent_id}",
        entra_app_audience=f"api://agp-agent-{agent_id}",
        created_at=now,
        updated_at=now,
    )
    return agent


def _graph_double(secret: str = SECRET_SENTINEL) -> AsyncMock:
    """An AsyncMock GraphService double.

    ``get_application_object_id`` returns the sentinel DIRECTORY OBJECT id (distinct
    from the appId/clientId GUID on the agent record) so tests can assert that
    ``add_agent_password`` is called with the RESOLVED object id, not the clientId.
    ``add_agent_password`` returns ``secret``.
    """
    graph = AsyncMock(name="GraphService")
    graph.get_application_object_id.return_value = AGENT_APP_OBJECT_ID
    graph.add_agent_password.return_value = secret
    return graph


def _resource_not_found_exc(control: MagicMock) -> type[Exception]:
    """Build a ``ResourceNotFoundException`` class on the mock control client's
    ``.exceptions`` namespace (mirrors a real boto3 client's exception model), so the
    service's get-or-create can ``except control.exceptions.ResourceNotFoundException``.
    """

    class ResourceNotFoundException(Exception):
        pass

    control.exceptions.ResourceNotFoundException = ResourceNotFoundException
    return ResourceNotFoundException


def _control_client(*, existing: bool) -> MagicMock:
    """A MagicMock boto3 control client.

    When ``existing`` is False, ``get_oauth2_credential_provider`` raises
    ``ResourceNotFoundException`` (so the service mints a secret + creates the provider).
    When True, it returns a provider payload (so the service short-circuits, minting
    NOTHING).
    """
    control = MagicMock(name="bedrock-agentcore-control")
    not_found = _resource_not_found_exc(control)
    if existing:
        control.get_oauth2_credential_provider.return_value = {
            "name": f"{PREFIX}rec-abc123",
            "credentialProviderArn": "arn:aws:bedrock-agentcore:us-east-1:123:cred/x",
        }
    else:
        control.get_oauth2_credential_provider.side_effect = not_found("no such provider")
    control.create_oauth2_credential_provider.return_value = {
        "name": f"{PREFIX}rec-abc123",
        "credentialProviderArn": "arn:aws:bedrock-agentcore:us-east-1:123:cred/x",
        "clientSecretArn": "arn:aws:secretsmanager:us-east-1:123:secret/y",
    }
    return control


def _make_service(*, graph: AsyncMock, control: MagicMock) -> AgentCredentialService:
    return AgentCredentialService(
        graph=graph,
        control_client=control,
        region=REGION,
        tenant_id=TENANT_ID,
        provider_name_prefix=PREFIX,
    )


# ===========================================================================
# create_oauth2_credential_provider — vendor + clientId + vaulted secret + tenant
# ===========================================================================
@pytest.mark.asyncio
async def test_ensure_credential_provider_creates_microsoft_provider():
    agent = _make_agent()
    graph = _graph_double()
    control = _control_client(existing=False)

    svc = _make_service(graph=graph, control=control)
    name = await svc.ensure_agent_credential_provider(agent)

    # Returns the deterministic provider name f'{prefix}{agent.id}'.
    assert name == f"{PREFIX}{agent.id}"

    # get_application_object_id was awaited with the agent's appId/clientId GUID to
    # resolve the directory object id before minting any secret.
    graph.get_application_object_id.assert_awaited_once_with(agent.entra_app_id)

    # add_agent_password was awaited with the RESOLVED object id (not the clientId GUID).
    # This is the load-bearing live-correctness assertion: the Graph call needs the
    # /applications/{objId} directory object id, which differs from the appId GUID stored
    # on the agent record.
    graph.add_agent_password.assert_awaited_once_with(AGENT_APP_OBJECT_ID)

    # create_oauth2_credential_provider was called with the MicrosoftOauth2 vendor, the
    # agent app's clientId (the GUID — the OBO middle tier), the VAULTED secret, and the
    # tenant id.
    control.create_oauth2_credential_provider.assert_called_once()
    _, kwargs = control.create_oauth2_credential_provider.call_args
    assert kwargs["name"] == f"{PREFIX}{agent.id}"
    assert kwargs["credentialProviderVendor"] == "MicrosoftOauth2"

    ms_config = kwargs["oauth2ProviderConfigInput"]["microsoftOauth2ProviderConfig"]
    assert ms_config["clientId"] == agent.entra_app_id   # the appId GUID (OBO middle tier)
    assert ms_config["clientSecret"] == SECRET_SENTINEL   # the vaulted secret
    assert ms_config["tenantId"] == TENANT_ID


@pytest.mark.asyncio
async def test_ensure_credential_provider_secret_never_logged(caplog):
    """SECURITY: the AGENT client secret minted by add_agent_password must never reach a
    log record. We drive the create path (which mints + vaults the secret) with logging
    captured at DEBUG and assert the sentinel is absent from every record."""
    agent = _make_agent()
    graph = _graph_double()
    control = _control_client(existing=False)

    svc = _make_service(graph=graph, control=control)
    with caplog.at_level(logging.DEBUG):
        name = await svc.ensure_agent_credential_provider(agent)

    # The non-secret provider name is the only thing returned.
    assert name == f"{PREFIX}{agent.id}"

    # No captured log record may contain the secret sentinel.
    all_logs = "\n".join(rec.getMessage() for rec in caplog.records)
    assert SECRET_SENTINEL not in all_logs


# ===========================================================================
# Idempotency — get-or-create: existing provider → no new secret, no create
# ===========================================================================
@pytest.mark.asyncio
async def test_ensure_credential_provider_idempotent():
    agent = _make_agent()
    graph = _graph_double()
    control = _control_client(existing=True)  # get_* returns an existing provider

    svc = _make_service(graph=graph, control=control)
    name = await svc.ensure_agent_credential_provider(agent)

    # The existing provider name is returned.
    assert name == f"{PREFIX}{agent.id}"
    # The get-or-create looked up by the deterministic name.
    control.get_oauth2_credential_provider.assert_called_once()
    _, get_kwargs = control.get_oauth2_credential_provider.call_args
    assert get_kwargs["name"] == f"{PREFIX}{agent.id}"

    # NO new secret minted, NO new provider created (the whole point of idempotency:
    # don't churn the vault on every call).
    graph.add_agent_password.assert_not_awaited()
    control.create_oauth2_credential_provider.assert_not_called()


# ===========================================================================
# Regression guard (T-CRED-ASYNC-FIX) — async, awaited on a LIVE running loop
# ===========================================================================
@pytest.mark.asyncio
async def test_ensure_credential_provider_is_async_awaits_graph_on_loop():
    """Regression guard for the cross-loop httpx bug (T-CRED-ASYNC-FIX).

    The method must be a coroutine that can be AWAITED directly on the test's already-
    running event loop — the exact condition that the old ``asyncio.run``-in-a-worker-thread
    bridge broke (a NEW loop in that thread couldn't drive GraphService's main-loop-bound
    httpx client, so it raised before any Graph request was sent). Here we await it on a live
    loop and assert it completes AND the two async Graph collaborators were genuinely
    AWAITED (not merely called) — proving the Graph calls run on this loop, not a foreign one.
    """
    import inspect

    agent = _make_agent()
    graph = _graph_double()
    control = _control_client(existing=False)
    svc = _make_service(graph=graph, control=control)

    # It is a coroutine function and returns an awaitable.
    assert inspect.iscoroutinefunction(svc.ensure_agent_credential_provider)
    coro = svc.ensure_agent_credential_provider(agent)
    assert inspect.iscoroutine(coro)

    name = await coro

    assert name == f"{PREFIX}{agent.id}"
    # Both async Graph calls were AWAITED on THIS running loop (the broken path never got here).
    graph.get_application_object_id.assert_awaited_once_with(agent.entra_app_id)
    graph.add_agent_password.assert_awaited_once_with(AGENT_APP_OBJECT_ID)
    # And the provider was created (the full mint→vault path ran on the loop).
    control.create_oauth2_credential_provider.assert_called_once()


# ===========================================================================
# Model field round-trip — oauth2_credential_provider_name survives to_envelope/from_record
# ===========================================================================
def test_oauth2_credential_provider_name_round_trips():
    """The new additive field on the Agent read-model carries through the envelope
    (to_envelope → from_record), like the E6 identity fields."""
    now = datetime(2026, 6, 1)
    record = {
        "recordId": "rec-cred",
        "name": "Claims Triage DE",
        "description": "Triage inbound motor claims",
        "status": "APPROVED",
        "createdAt": now,
        "updatedAt": now,
    }
    agent = Agent(
        id="rec-cred",
        name="Claims Triage DE",
        lifecycle_state=LifecycleState.APPROVED,
        created_at=now,
        updated_at=now,
        oauth2_credential_provider_name=f"{PREFIX}rec-cred",
    )

    # to_envelope carries the field.
    env = agent.to_envelope()
    assert env["oauth2_credential_provider_name"] == f"{PREFIX}rec-cred"

    # from_record hydrates it back.
    rebuilt = Agent.from_record(record, env)
    assert rebuilt.oauth2_credential_provider_name == f"{PREFIX}rec-cred"


def test_oauth2_credential_provider_name_defaults_none_when_missing():
    """A pre-T-CRED-PROVIDER envelope lacks the key → the field hydrates as None
    (purely additive, backward-compatible)."""
    now = datetime(2026, 6, 1)
    record = {
        "recordId": "rec-old",
        "name": "Legacy Agent",
        "description": "",
        "status": "APPROVED",
        "createdAt": now,
        "updatedAt": now,
    }
    # pre-T-CRED-PROVIDER envelope: no oauth2_credential_provider_name key.
    envelope = {"schema_version": 1, "mcp_server_ids": []}
    agent = Agent.from_record(record, envelope)
    assert agent.oauth2_credential_provider_name is None


# ===========================================================================
# Import smoke
# ===========================================================================
def test_import_smoke():
    from services.agent_credential_service import AgentCredentialService  # noqa: F401

    assert AgentCredentialService is not None


# ===========================================================================
# E36/T16 (research item 5B) — delete_agent_obo_provider: the missing teardown.
#
# The provider was get-or-created at grant time and NOTHING ever deleted it, so a torn-down
# agent left an AgentCore Token Vault entry holding a now-dangling clientId/clientSecret
# pair (the Entra app delete cascades the secret; the vault entry survived it).
#
# Same shape as the create half: the deterministic name ``f'{prefix}{agent_id}'``, the
# blocking boto3 call off-loaded, and ``ResourceNotFoundException`` == the desired end state.
# ===========================================================================
@pytest.mark.asyncio
async def test_delete_agent_obo_provider_deletes_the_deterministic_name():
    control = _control_client(existing=True)
    svc = _make_service(graph=_graph_double(), control=control)

    await svc.delete_agent_obo_provider("rec-abc123")

    control.delete_oauth2_credential_provider.assert_called_once_with(
        name=f"{PREFIX}rec-abc123"
    )


@pytest.mark.asyncio
async def test_delete_agent_obo_provider_is_idempotent_on_not_found():
    """Already gone IS the desired end state (a re-run of a teardown, a provider deleted by
    hand): no raise, so the caller can call it unconditionally."""
    control = _control_client(existing=True)
    not_found = control.exceptions.ResourceNotFoundException
    control.delete_oauth2_credential_provider.side_effect = not_found("no such provider")
    svc = _make_service(graph=_graph_double(), control=control)

    await svc.delete_agent_obo_provider("rec-abc123")

    control.delete_oauth2_credential_provider.assert_called_once_with(
        name=f"{PREFIX}rec-abc123"
    )


@pytest.mark.asyncio
async def test_delete_agent_obo_provider_reraises_other_errors():
    """Only not-found is the desired end state. A ThrottlingException / AccessDenied means
    the vault entry may still be there, and a swallowed failure would be reported to an
    operator as a completed teardown."""
    control = _control_client(existing=True)
    _resource_not_found_exc(control)
    control.delete_oauth2_credential_provider.side_effect = RuntimeError("throttled")
    svc = _make_service(graph=_graph_double(), control=control)

    with pytest.raises(RuntimeError):
        await svc.delete_agent_obo_provider("rec-abc123")


@pytest.mark.asyncio
async def test_delete_agent_obo_provider_never_mints_or_reads_a_secret():
    """SECRET-SAFETY: the teardown touches Graph not at all — there is no secret in this
    path, and it must not create one to delete something."""
    graph = _graph_double()
    control = _control_client(existing=True)
    svc = _make_service(graph=graph, control=control)

    await svc.delete_agent_obo_provider("rec-abc123")

    graph.add_agent_password.assert_not_awaited()
    graph.get_application_object_id.assert_not_awaited()
    control.create_oauth2_credential_provider.assert_not_called()
