"""The Databricks invoke leg behind ``DatabricksPlatformAdapter.invoke``: resolve the tenant, the
workspace and the app URL, validate them, obtain the token the tenant's binding mode allows, POST
the prompt, and return the envelope the AgentCore leg returns.

Order is the security property: the tenant, the workspace and the URL are resolved and validated
before any Databricks token is obtained, so a hostile ``runtime_handle`` is refused with no
credential in flight and a mis-keyed record costs no token mint (``test_invoke_route`` pins the
sequence, not only the absence of a call). Every refusal is an ``InvokeError`` kind with a fixed
detail; the upstream ``DatabricksError`` message, a workspace URL, a host or a principal id never
reaches a client, and the log lines carry a code and the agent id only. The tenants-route and
agents-route accessors are imported inside the functions that need them (a module-level import is
a cycle) so this module reads the same singletons the rest of the app writes; the SSE parser stays
on the route because both legs share it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

from services.agent_identity_service import ProvisioningError
from services.databricks_workspace_service import DatabricksError
from services.platform_adapter import UPSTREAM_ERROR, InvokeError
from services.tenant_service import BINDING_FEDERATION, BINDING_INVOKE_UNAVAILABLE, BINDING_SP_SECRET

if TYPE_CHECKING:
    from api.routes.agents import InvokeRequest
    from core.rbac import Principal
    from models.agent import Agent

logger = logging.getLogger(__name__)

_DATABRICKS_APPS_HOST_SUFFIX = ".databricksapps.com"
_DATABRICKS_DEFAULT_ROUTE = "/api/v1/agent"
_DATABRICKS_AGENT_SECRET_KEY = "client_secret"  # noqa: S105 — the key inside the secret body, not a credential; expires 2027-10-23; added E49A-50A/T8
_TIMEOUT_S = 120.0


def _httpx_can_build(url: str) -> bool:
    """Whether httpx can build a request from ``url``. ``httpx.URL`` is lazy and reading ``.host``
    runs the IDNA encode that would otherwise fail at the POST, after a token was spent: a
    non-printable character raises ``InvalidURL`` and a bad A-label raises ``idna.IDNAError``, a
    ``ValueError``. Neither is an ``HTTPError``."""
    try:
        return isinstance(httpx.URL(url).host, str)
    except (httpx.InvalidURL, ValueError):
        return False


def _validate_databricks_app_url(raw: str) -> str:
    """Return ``raw`` when it is an https Databricks Apps URL httpx can request; else
    ``invalid_runtime_handle``.

    This guards a credential: ``runtime_handle`` and ``endpoint_url`` are client-settable
    registration fields, and this path attaches a freshly obtained Databricks token to the URL.
    The scheme is exactly https; the netloc carries no userinfo (``https://trusted@attacker`` has
    the attacker as its hostname); the host ends with the dotted suffix, so the bare apex and
    ``evil-databricksapps.com`` are refused; and httpx can build the request, which catches a CRLF
    in the path, a soft hyphen and an empty punycode label ahead of the mint. The validated string
    is the one the caller POSTs. Serving endpoints live on the workspace host and are not accepted.
    """
    url = (raw or "").strip()
    if not url:
        raise InvokeError("the runtime handle is empty", "invalid_runtime_handle")
    try:
        parsed = urlparse(url)
    except ValueError as err:
        raise InvokeError("the runtime handle does not parse", "invalid_runtime_handle") from err
    if parsed.scheme != "https":
        raise InvokeError("the runtime handle is not https", "invalid_runtime_handle")
    if "@" in (parsed.netloc or ""):
        raise InvokeError("the runtime handle carries userinfo", "invalid_runtime_handle")
    host = (parsed.hostname or "").strip().lower()
    if not host.endswith(_DATABRICKS_APPS_HOST_SUFFIX):
        raise InvokeError("the runtime handle is not a Databricks Apps host", "invalid_runtime_handle")
    if not _httpx_can_build(url):
        raise InvokeError("the runtime handle cannot be requested", "invalid_runtime_handle")
    return url


def _databricks_invoke_url(agent: Agent) -> str:
    """``endpoint_url`` when set, else the handle plus the template's default route; both validated
    on the same rule, since ``endpoint_url`` is the value requested."""
    endpoint = (agent.endpoint_url or "").strip()
    if endpoint:
        return _validate_databricks_app_url(endpoint)
    handle = _validate_databricks_app_url(agent.runtime_handle or "")
    return f"{handle.rstrip('/')}{_DATABRICKS_DEFAULT_ROUTE}"


def _resolve_databricks_tenant(agent: Agent):
    """The agent's tenant through the tenants-route singleton, or ``tenant_unresolved``. The binding
    mode and the workspace come from the tenant, never from the agent record: ``Agent.binding_mode``
    is a client-settable copy taken at registration, and trusting it would let a caller pick the
    weaker credential path on a federation tenant."""
    tenant_id = (agent.tenant_id or "").strip()
    if not tenant_id:
        raise InvokeError("the agent names no tenant", "tenant_unresolved")
    from api.routes.tenants import get_tenant_service

    try:
        tenant = get_tenant_service().get(tenant_id)
    except Exception as err:
        logger.warning("[invoke] could not resolve the tenant for Databricks agent %s", agent.id)
        raise InvokeError("the agent's tenant could not be read", "tenant_unresolved") from err
    if tenant is None:
        raise InvokeError("the agent's tenant does not exist", "tenant_unresolved")
    return tenant


def _validate_databricks_workspace_url(url: str) -> str:
    """The accepted ``workspace_url``, through httpx's parser: it is where the first credential goes
    (the RFC 8693 exchange and the SP mint both POST there), so a URL httpx cannot request is
    refused before a credential leaves AGP. Not an allowlist: workspaces run on
    ``*.cloud.databricks.com``, ``*.azuredatabricks.net``, ``*.gcp.databricks.com`` and vanity
    CNAMEs. The URL is never echoed."""
    if not _httpx_can_build(url):
        raise InvokeError("the workspace URL cannot be requested", "workspace_url_invalid")
    return url


def _resolve_databricks_workspace(agent: Agent, tenant) -> str:
    """Which workspace hosts this agent: the stage provisioning bound it in (``bound_stage``) when
    that stage still carries a ``workspace_url``; else the tenant's one distinct workspace (a
    single-workspace operator duplicates one URL into dev and prod); else, among several, the one
    whose URL prefixes the agent's handle. No match is ``workspace_stage_unresolved``: an Apps
    hostname carries no workspace identity, and exchanging the caller's token at a workspace that
    does not host the app would leak an assertion or produce a 502 that looks like the app's fault.
    Only the accepted URL is validated; a rejected candidate only loses a prefix match."""
    stages = getattr(tenant, "stages", None) or {}
    bound = (getattr(agent, "bound_stage", "") or "").strip()
    if bound:
        stage = stages.get(bound)
        bound_url = (getattr(stage, "workspace_url", "") or "").strip() if stage else ""
        if bound_url:
            return _validate_databricks_workspace_url(bound_url)
    seen: dict[str, str] = {}
    for _name, stage in sorted(stages.items(), key=lambda kv: kv[0]):
        url = (getattr(stage, "workspace_url", "") or "").strip()
        if url:
            seen.setdefault(url.rstrip("/").lower(), url)
    workspaces = list(seen.values())
    if not workspaces:
        raise InvokeError("the tenant has no Databricks workspace stage", "workspace_stage_unresolved")
    if len(workspaces) == 1:
        return _validate_databricks_workspace_url(workspaces[0])
    handle = (agent.runtime_handle or "").strip().rstrip("/").lower()
    for url in workspaces:
        if handle.startswith(url.rstrip("/").lower()):
            return _validate_databricks_workspace_url(url)
    logger.warning(
        "[invoke] agent %s's handle matches none of its tenant's %d Databricks workspaces, and an Apps "
        "hostname carries no workspace identity to disambiguate on; record the bound workspace stage "
        "on the agent at provisioning time to make this resolvable",
        agent.id,
        len(workspaces),
    )
    raise InvokeError(
        "the agent's handle matches none of the tenant's workspaces", "workspace_stage_unresolved"
    )


def _databricks_agent_secret(agent: Agent) -> str:
    """The per-agent service-principal secret, honoured only when its ARN is one the identity
    service would have written for this agent (``DatabricksAgentSecrets.owns_secret_arn``, reused
    rather than copied: two disagreeing copies of an ownership rule mean the looser one is the
    bypass). Without that check a record registered with the tenant's workspace-SP ARN would mint a
    token from a credential the agent was never granted. The value goes straight into the mint:
    never returned, logged or interpolated into an error, and the ARN is never logged either, since
    it names another principal's secret."""
    from api.routes.agents import get_databricks_identity_service

    arn = (agent.databricks_sp_secret_arn or "").strip()
    client_id = (agent.databricks_sp_id or "").strip()
    if not arn or not client_id:
        raise InvokeError("the agent names no service-principal credential", "sp_credential_unavailable")
    identity = get_databricks_identity_service()
    if identity is None:
        logger.warning(
            "[invoke] no Databricks identity service is configured, so agent %s's stored "
            "service-principal credential cannot be read",
            agent.id,
        )
        raise InvokeError("no Databricks identity service is configured", "sp_credential_unavailable")
    if not identity._secrets.owns_secret_arn(agent, arn):
        logger.warning(
            "[invoke] agent %s names a Databricks secret this platform did not write for it; "
            "refusing to mint a token from it",
            agent.id,
        )
        raise InvokeError("the agent names a secret this platform did not write", "sp_credential_unavailable")
    try:
        secret = identity._secrets.read_secret(arn, _DATABRICKS_AGENT_SECRET_KEY)
    except ProvisioningError:
        logger.warning("[invoke] agent %s's stored Databricks credential could not be read", agent.id)
        raise InvokeError(
            "the agent's stored credential could not be read", "sp_credential_unavailable"
        ) from None
    if not secret:
        raise InvokeError("the agent's stored credential is empty", "sp_credential_unavailable")
    return secret


async def _databricks_token(agent: Agent, tenant, workspace_url: str, obo_token: str) -> str:
    """The Databricks token to bear, by the tenant's binding mode.

    ``federation``: an RFC 8693 exchange of the OBO token at the workspace, so the app and Unity
    Catalog see the real caller. ``invoke_unavailable``: the tenant was probed and cannot federate,
    refused with its own code (``binding_mode_unresolved`` means it was never probed, a different
    fix). ``sp_secret``: dormant; refused unless ``DATABRICKS_ALLOW_SP_SECRET_BINDING`` is on, and
    then a client-credentials token minted from the agent's stored secret, read before any
    Databricks call, which attributes every call to that service principal. An unrecognised or
    empty mode is refused, never defaulted, because the modes differ in what the audit log can
    prove. ``settings`` and the workspace client are resolved at call time so tests and the app
    read one instance.
    """
    from api.routes.agents import get_databricks_workspace_service
    from core.config import settings

    mode = (getattr(tenant, "binding_mode", "") or "").strip()
    databricks = get_databricks_workspace_service()

    if mode == BINDING_FEDERATION:
        try:
            return await databricks.exchange_federated_token(workspace_url, obo_token)
        except DatabricksError as err:
            logger.warning(
                "[invoke] the federated-token exchange failed for agent %s (%s)",
                agent.id,
                err.kind,
            )
            raise InvokeError("the federated-token exchange failed", "federation_exchange_failed") from None

    if mode == BINDING_INVOKE_UNAVAILABLE:
        logger.warning(
            "[invoke] agent %s's tenant cannot federate, so AGP will not invoke it (federation_unavailable)",
            agent.id,
        )
        raise InvokeError("the tenant cannot federate", "federation_unavailable")

    if mode == BINDING_SP_SECRET and not settings.DATABRICKS_ALLOW_SP_SECRET_BINDING:
        logger.warning(
            "[invoke] agent %s carries the dormant sp_secret binding and this deployment does not "
            "allow it (sp_secret_disabled)",
            agent.id,
        )
        raise InvokeError("the sp_secret binding is not allowed on this deployment", "sp_secret_disabled")

    if mode == BINDING_SP_SECRET:
        secret = _databricks_agent_secret(agent)
        try:
            return await databricks.mint_m2m_token(workspace_url, str(agent.databricks_sp_id or ""), secret)
        except DatabricksError as err:
            logger.warning(
                "[invoke] the service-principal token mint failed for agent %s (%s)",
                agent.id,
                err.kind,
            )
            raise InvokeError("the service-principal token mint failed", "token_mint_failed") from None

    logger.warning("[invoke] agent %s's tenant carries no usable Databricks binding mode", agent.id)
    raise InvokeError("the tenant carries no usable binding mode", "binding_mode_unresolved")


async def invoke_databricks(agent: Agent, req: InvokeRequest, principal: Principal, obo_token: str) -> dict:
    """POST the prompt to a Databricks-governed agent and return ``{"response": ...}``.

    The response contract is the AgentCore leg's, through the same ``_extract_sse_text``; the
    AgentCore session header is not sent. Redirects are never followed: the allowlist is enforced on
    the URL AGP requests, and a validated host answering ``302 Location: https://attacker`` must not
    have the Authorization header re-sent to it. ``httpx.InvalidURL`` and ``ValueError`` are caught
    beside ``HTTPError`` because neither subclasses it, so a lazily rejected URL is a 502, not a 500.
    The app refusing the Databricks token (401/403) is ``token_rejected``, the same fact as on
    AgentCore and distinct from the OBO 403 upstream.
    """
    from api.routes.agents import _extract_sse_text

    tenant = _resolve_databricks_tenant(agent)
    workspace_url = _resolve_databricks_workspace(agent, tenant)
    url = _databricks_invoke_url(agent)

    db_token = await _databricks_token(agent, tenant, workspace_url, obo_token)

    headers = {"Authorization": f"Bearer {db_token}", "Content-Type": "application/json"}
    invoke_body = {"prompt": req.prompt, "user_oid": principal.oid}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S, follow_redirects=False) as client:
            app_resp = await client.post(url, headers=headers, json=invoke_body)
    except httpx.TimeoutException:
        raise InvokeError("agent invocation timed out", "timed_out") from None
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        raise InvokeError("failed to reach the agent", "unreachable") from None

    if app_resp.status_code in (401, 403):
        raise InvokeError("agent rejected the token", "token_rejected")
    if not (200 <= app_resp.status_code < 300):
        raise InvokeError(UPSTREAM_ERROR, "upstream_error")

    if "text/event-stream" in app_resp.headers.get("content-type", ""):
        return {"response": _extract_sse_text(app_resp.text)}
    try:
        return {"response": app_resp.json()}
    except (ValueError, TypeError):
        return {"response": app_resp.text}
