"""The per-agent AgentCore ``MicrosoftOauth2`` credential provider.

The backend mints the agent app a client secret through Graph and hands it to AgentCore
Identity's Token Vault as a credential provider, so the agent obtains MCP tokens without ever
holding a secret. The secret flows straight into the create call and is never logged, persisted
or returned; only the provider name is. ``ensure_agent_credential_provider`` is async and awaits
Graph on the event loop that owns ``GraphService``'s client; the blocking boto3 calls are
off-loaded with ``anyio.to_thread.run_sync``.
"""

from __future__ import annotations

import functools
import logging

import anyio.to_thread

from core import aws_clients
from core.config import settings
from models.agent import Agent
from services.graph_service import GraphService

logger = logging.getLogger(__name__)

_CREDENTIAL_PROVIDER_VENDOR = "MicrosoftOauth2"


class AgentCredentialService:
    """Creates (get-or-create) the per-agent ``MicrosoftOauth2`` credential provider."""

    def __init__(
        self,
        *,
        graph: GraphService,
        control_client=None,
        region: str | None = None,
        tenant_id: str | None = None,
        provider_name_prefix: str | None = None,
    ) -> None:
        self._graph = graph
        self._region = region
        self._tenant_id = tenant_id if tenant_id is not None else settings.ENTRA_TENANT_ID
        self._provider_name_prefix = (
            provider_name_prefix if provider_name_prefix is not None else settings.AGENT_CRED_PROVIDER_PREFIX
        )
        self._control = control_client or aws_clients.client("bedrock-agentcore-control", region=region)

    async def ensure_agent_credential_provider(self, agent: Agent) -> str:
        """Get-or-create the agent's provider, named ``f'{prefix}{agent.id}'``, and return the name. An
        existing provider is reused without minting a new secret; otherwise a fresh client secret is
        minted for the agent app and vaulted with ``clientId``, ``clientSecret`` and ``tenantId``.
        """
        provider_name = f"{self._provider_name_prefix}{agent.id}"

        exists = await anyio.to_thread.run_sync(self._provider_exists, provider_name)
        if exists:
            logger.info(
                "[agent_credential] credential provider %s already exists for agent %s "
                "— reusing (no new secret minted)",
                provider_name,
                agent.id,
            )
            return provider_name

        if not agent.entra_app_id:
            raise ValueError(f"agent {agent.id} has no Entra app, so no credential provider can be minted")
        obj_id = await self._graph.get_application_object_id(agent.entra_app_id)
        client_secret = await self._graph.add_agent_password(obj_id)

        await anyio.to_thread.run_sync(
            functools.partial(
                self._control.create_oauth2_credential_provider,
                name=provider_name,
                credentialProviderVendor=_CREDENTIAL_PROVIDER_VENDOR,
                oauth2ProviderConfigInput={
                    "microsoftOauth2ProviderConfig": {
                        "clientId": agent.entra_app_id,
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
        """Delete the agent's provider, the inverse of ``ensure_agent_credential_provider``, from the id
        alone. Idempotent: not-found is the desired end state and is swallowed; every other error
        propagates, so a surviving vault entry is never reported as reclaimed.
        """
        provider_name = f"{self._provider_name_prefix}{agent_id}"
        await anyio.to_thread.run_sync(self._delete_provider, provider_name)

    def _delete_provider(self, provider_name: str) -> None:
        """``delete_oauth2_credential_provider(name=...)``, swallowing not-found.

        The SYNC body of :meth:`delete_agent_obo_provider`; see that docstring for the
        idempotency contract."""
        try:
            self._control.delete_oauth2_credential_provider(name=provider_name)
        except self._control.exceptions.ResourceNotFoundException:
            logger.info(
                "[agent_credential] credential provider %s already gone — treating as success",
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
