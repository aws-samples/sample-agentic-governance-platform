"""AgentCore OAuth2 credential-provider orchestrator (Epic 7, Tier-2, Task T-CRED-PROVIDER).

``AgentCredentialService`` gives the reference agent a way to obtain an MCP token
**without ever holding a secret in its code** — the security property the user insisted
on. The backend mints the AGENT app a fresh client secret via Microsoft Graph
(``GraphService.add_agent_password``) and hands it to AgentCore Identity's **Token Vault**
as a ``MicrosoftOauth2`` credential provider (``create_oauth2_credential_provider``). From
then on the agent calls an AgentCore token util (a data-plane call inside the runtime) and
the secret stays vaulted — the agent never sees a printable secret.

SECURITY (the whole point of this service):
  - The AGENT's client secret (from ``add_agent_password``) flows STRAIGHT into the
    provider config's ``clientSecret`` and is NEVER logged, printed, persisted to the
    registry/DB, placed in an exception message, or returned to a caller. The ONLY thing
    persisted on the agent record is the provider NAME (a non-secret).
  - This module logs only the provider NAME + the agent id — never the secret. There is a
    unit test pinning that the secret never reaches a log record.

It is a CONTROL-PLANE service like ``agent_identity_service`` / ``mcp_identity_service``
(injectable ``control_client``, region, the boto3 ``bedrock-agentcore-control`` client
construction). ``ensure_agent_credential_provider`` is **async** and runs ON the uvicorn
event loop (mirroring ``mcp_identity_service.provision``): the Graph calls
(``get_application_object_id`` / ``add_agent_password``) are awaited DIRECTLY on the loop —
they use ``GraphService``'s shared, loop-bound ``httpx.AsyncClient``, so they MUST run on
the same loop that owns that client (T-CRED-ASYNC-FIX: the prior ``asyncio.run`` opened a
NEW loop in a worker thread, and using the main-loop-bound client from that other loop
raised before any request was sent). The genuinely-blocking **SYNC boto3** control-plane
calls (``get_oauth2_credential_provider`` / ``create_oauth2_credential_provider``) are the
only things off-loaded, via ``anyio.to_thread.run_sync`` — exactly the idiom
``mcp_identity_service`` uses for its boto3 authorizer config.

Idempotent (get-or-create): a provider named ``f'{prefix}{agent.id}'`` is looked up first
(``get_oauth2_credential_provider``); if it already exists, its name is returned WITHOUT
minting a new secret (minting on every call would churn the vault). Only when it is
absent (``ResourceNotFoundException``) does it mint the secret + create the provider.

Mechanics source: research §3.2 (the ``CreateOauth2CredentialProvider`` shape — vendor
``MicrosoftOauth2``, ``microsoftOauth2ProviderConfig={clientId, clientSecret, tenantId}``;
Microsoft's OBO is preconfigured, no explicit ``onBehalfOf`` config needed for the
Microsoft vendor; OUT carries ``credentialProviderArn`` + ``clientSecretArn`` with the
secret in the Token Vault), §2.4(d) (the provider stores the AGENT app's clientId +
secret), §3.5 (per-agent granularity; persist the provider name on the agent record).
``agent_identity_service`` is the constructor/client template.
"""

from __future__ import annotations

import functools
import logging

import anyio.to_thread
import boto3

from core.config import settings
from models.agent import Agent
from services.graph_service import GraphService

logger = logging.getLogger(__name__)

# The AgentCore credential-provider vendor for Microsoft Entra OBO (research §3.2).
_CREDENTIAL_PROVIDER_VENDOR = "MicrosoftOauth2"


class AgentCredentialService:
    """Creates (get-or-create) the per-agent ``MicrosoftOauth2`` credential provider."""

    def __init__(
        self,
        *,
        graph: GraphService,
        control_client=None,
        region: str = "us-east-1",
        tenant_id: str | None = None,
        provider_name_prefix: str | None = None,
    ) -> None:
        self._graph = graph
        self._region = region
        # tenantId for the provider config. Defaults to the configured Entra tenant id;
        # injectable for tests.
        self._tenant_id = tenant_id if tenant_id is not None else settings.ENTRA_TENANT_ID
        # Name prefix for per-agent providers (default "agp-agent-obo-", from config —
        # added by T-INFRA-DEPS). Injectable for tests.
        self._provider_name_prefix = (
            provider_name_prefix
            if provider_name_prefix is not None
            else settings.AGENT_CRED_PROVIDER_PREFIX
        )
        # boto3 ``bedrock-agentcore-control`` client. Injectable for tests (a MagicMock);
        # built by default (no live AWS in unit tests, which always inject). Control-plane
        # signing name is ``bedrock-agentcore-control``. (Same as agent_identity_service.)
        self._control = control_client or boto3.client(
            "bedrock-agentcore-control", region_name=region
        )

    # -- public ------------------------------------------------------------

    async def ensure_agent_credential_provider(self, agent: Agent) -> str:
        """Get-or-create the agent's ``MicrosoftOauth2`` credential provider; return its name.

        Idempotent. The deterministic provider name is ``f'{prefix}{agent.id}'``:
          1. ``get_oauth2_credential_provider(name=...)`` — if it already exists, return
             the name WITHOUT minting a new secret (don't churn the vault).
          2. On ``ResourceNotFoundException`` → mint a fresh client secret for the AGENT
             app via Graph ``add_agent_password`` and create the provider with vendor
             ``MicrosoftOauth2`` and ``microsoftOauth2ProviderConfig={clientId:
             agent.entra_app_id, clientSecret: <vaulted>, tenantId: TENANT_ID}``.

        SECURITY: the minted secret is passed directly into the create call's
        ``clientSecret`` and is never logged/persisted/returned. Only the provider NAME is
        returned (the caller persists it on the agent record's
        ``oauth2_credential_provider_name`` — a non-secret).

        ASYNC, runs ON the event loop (T-CRED-ASYNC-FIX). The two Graph calls are awaited
        DIRECTLY on the loop because they share ``GraphService``'s loop-bound
        ``httpx.AsyncClient`` (driving them on a different loop — the old ``asyncio.run``
        bug — raised before any request was sent). The blocking boto3 control calls
        (``get_oauth2_credential_provider`` / ``create_oauth2_credential_provider``) are
        off-loaded via ``anyio.to_thread.run_sync`` so the uvicorn loop never blocks —
        the same idiom ``mcp_identity_service`` uses for its boto3 authorizer config.
        """
        provider_name = f"{self._provider_name_prefix}{agent.id}"

        # (1) Get-or-create: a provider for this agent may already exist (a prior grant /
        # re-provision). The lookup is a SYNC boto3 call → off-load it. Return its name
        # WITHOUT minting a new secret.
        exists = await anyio.to_thread.run_sync(self._provider_exists, provider_name)
        if exists:
            logger.info(
                "[agent_credential] credential provider %s already exists for agent %s "
                "— reusing (no new secret minted)",
                provider_name,
                agent.id,
            )
            return provider_name

        # (2) Mint a fresh client secret for the AGENT app and vault it via the provider.
        # The Graph calls are awaited DIRECTLY on the loop (they use the shared loop-bound
        # httpx client — see the docstring / T-CRED-ASYNC-FIX). SECURITY: ``client_secret``
        # is the most sensitive value here — it is passed straight into the create call
        # below and is NEVER logged, persisted, or returned.
        obj_id = await self._graph.get_application_object_id(agent.entra_app_id)
        client_secret = await self._graph.add_agent_password(obj_id)

        # The create call is SYNC blocking boto3 → off-load it. functools.partial carries
        # the kwargs (run_sync passes only positional args).
        await anyio.to_thread.run_sync(
            functools.partial(
                self._control.create_oauth2_credential_provider,
                name=provider_name,
                credentialProviderVendor=_CREDENTIAL_PROVIDER_VENDOR,
                oauth2ProviderConfigInput={
                    "microsoftOauth2ProviderConfig": {
                        # The AGENT app's clientId — the OBO middle tier (research §2.4(d)).
                        "clientId": agent.entra_app_id,
                        # The vaulted secret. Never logged/persisted; lives only in the
                        # Token Vault from here on.
                        "clientSecret": client_secret,
                        "tenantId": self._tenant_id,
                    }
                },
            )
        )
        logger.info(
            "[agent_credential] created MicrosoftOauth2 credential provider %s for agent %s",
            provider_name,
            agent.id,
        )
        return provider_name

    async def delete_agent_obo_provider(self, agent_id: str) -> None:
        """Delete the agent's ``MicrosoftOauth2`` credential provider — the DELETE twin of
        :meth:`ensure_agent_credential_provider` (E36/T16, research item 5B).

        Nothing in the platform deleted it before. On teardown the agent's Entra app was
        deleted (cascading the client secret Entra holds), but the AgentCore Token Vault
        entry survived — holding a now-dangling ``clientId``/``clientSecret`` pair for an
        application that no longer exists, and accumulating one entry per agent ever
        registered. Called from ``AgentIdentityService.delete_identity`` (the ONE identity
        teardown both delete paths go through).

        Takes the AGENT ID, not an ``Agent``: the provider name is derived from the id
        alone, and the teardown must work from a record whose envelope may already be
        half-gone. The name is the SAME deterministic ``f'{prefix}{agent_id}'`` the create
        path uses — one derivation, or the deleter would be free to miss.

        IDEMPOTENT: ``ResourceNotFoundException`` is the DESIRED end state (a re-run
        teardown, an agent that never got a grant, a provider removed by hand) and is
        swallowed. Every OTHER error PROPAGATES — the vault entry may still be there, and a
        swallowed failure is what turns a leak into a teardown reported as complete. The
        caller decides how to report it (the route cascades report it as a failed
        line-item).

        SECURITY: no secret in this path at all. It touches Graph not at all and never
        mints, reads or logs a client secret — only the provider NAME (a non-secret) is
        logged, exactly as the create half does. The blocking boto3 call is off-loaded via
        ``anyio.to_thread.run_sync`` so the uvicorn loop never blocks (same idiom as the
        create path's control calls).
        """
        provider_name = f"{self._provider_name_prefix}{agent_id}"
        await anyio.to_thread.run_sync(self._delete_provider, provider_name)

    # -- helpers -----------------------------------------------------------

    def _delete_provider(self, provider_name: str) -> None:
        """``delete_oauth2_credential_provider(name=...)``, swallowing not-found.

        The SYNC body of :meth:`delete_agent_obo_provider`; see that docstring for the
        idempotency contract."""
        try:
            self._control.delete_oauth2_credential_provider(name=provider_name)
        except self._control.exceptions.ResourceNotFoundException:
            logger.info(
                "[agent_credential] credential provider %s already gone — treating as "
                "success",
                provider_name,
            )
            return
        logger.info(
            "[agent_credential] deleted MicrosoftOauth2 credential provider %s",
            provider_name,
        )

    def _provider_exists(self, provider_name: str) -> bool:
        """True if a credential provider with ``provider_name`` already exists.

        Uses ``get_oauth2_credential_provider(name=...)``; a
        ``ResourceNotFoundException`` means "not present" → return False (the create
        path). Any OTHER boto error propagates (we never mask a real failure as
        "not present", which would re-mint + re-create).
        """
        try:
            self._control.get_oauth2_credential_provider(name=provider_name)
            return True
        except self._control.exceptions.ResourceNotFoundException:
            return False
