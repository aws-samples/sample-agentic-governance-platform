"""Entra identity provisioning for AgentCore gateway and runtime MCP servers.

``McpIdentityService.provision`` wires a registered MCP up: it reads the resource (capturing the
gateway URL verbatim), discovers the gateway's tools, creates the per-MCP Entra app registration
and service principal, sets the SP's ``appRoleAssignmentRequired`` to ``False``, configures the
resource's own inbound ``CUSTOM_JWT`` authorizer to trust our tenant, and persists every id onto
the registry record. There is no consent step here: the agent-to-MCP delegated consent is granted
at grant time (``services.agent_mcp_grant``).

Provisioning is idempotent and resumable: the Entra app is created only when the record carries
no ``entra_sp_id``, the Graph writes and the authorizer configuration converge on re-run, and the
one ``pending`` persist after the Entra step carries the gateway fields, the tools and the five
Entra ids in a single write. On any failure except the best-effort tool discovery the record is
persisted ``identity_status='failed'`` and :class:`McpProvisioningError` is raised.

Tool discovery is best-effort and never wipes seeded tools: the native control-plane read
(``ListGatewayTargets`` -> ``GetGatewayTarget`` -> inline tool schemas) needs no token and works
on a locked gateway; the token-less wire scan is the fallback for a still-open gateway whose native
read found nothing. ``available_tools`` is overwritten only on a non-empty result.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Mapping
from typing import Any, ClassVar, Optional

import anyio
import anyio.to_thread
import yaml

from core import aws_clients
from core.errors import AppError, Outcome
from models.mcp_server import IdentityStatus, Kind, McpServer, McpTool
from services.graph_errors import GraphError
from services.graph_service import GraphService
from services.mcp_server_service import McpServerRegistryService
from services.mcp_tool_scanner import McpScanError, scan_mcp_tools

logger = logging.getLogger(__name__)

_POLL_MAX_ATTEMPTS = 60
_POLL_INTERVAL_SECONDS = 5.0

_READY_STATUS = "READY"
_FAILED_STATUSES = frozenset(
    {
        "CREATE_FAILED",
        "UPDATE_FAILED",
        "DELETE_FAILED",
        "FAILED",
        "UPDATE_UNSUCCESSFUL",
    }
)

_OPEN_AUTHORIZER_TYPES = frozenset({"NONE", "AWS_IAM"})

_MAX_TARGET_PAGES = 20

_OPENAPI_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

_GATEWAY_OPTIONAL_FIELDS = (
    "protocolType",
    "protocolConfiguration",
    "description",
    "kmsKeyArn",
    "interceptorConfigurations",
    "policyEngineConfiguration",
    "exceptionLevel",
)

_RUNTIME_OPTIONAL_FIELDS = (
    "protocolConfiguration",
    "requestHeaderConfiguration",
    "environmentVariables",
    "lifecycleConfiguration",
    "metadataConfiguration",
    "filesystemConfigurations",
    "description",
)


def should_provision_mcp(mcp: McpServer) -> bool:
    """The provisioning gate: an AgentCore gateway carrying a ``gateway_arn`` or an AgentCore
    runtime MCP carrying a ``runtime_arn``. Standard (external, metadata-only) records and
    handle-less records are skipped; MCP records carry no ``auth_type``, so kind plus handle is
    the whole gate."""
    return (mcp.kind == Kind.GATEWAY and bool(mcp.gateway_arn)) or (
        mcp.kind == Kind.RUNTIME and bool(mcp.runtime_arn)
    )


class _NoTimestampLoader(yaml.SafeLoader):
    """``SafeLoader`` without the implicit timestamp resolver: an unquoted ``2026-10-05`` stays
    the string it is in the spec instead of becoming a ``date`` the record write cannot
    json-serialise (or a mapping key json cannot encode).

    It also refuses YAML aliases: OpenAPI reuses through ``$ref``, never anchors, and a few
    hundred bytes of nested aliases expand exponentially in the JSON round-trip below (an
    "alias bomb" that OOM-kills the task). An alias makes the spec unparseable."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.events.AliasEvent):
            raise yaml.YAMLError("YAML aliases are not supported in OpenAPI specs")
        return super().compose_node(parent, index)


_NoTimestampLoader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _no_base60(construct):
    """YAML 1.1 base-60 ints/floats (``1:30``) stay strings: OpenAPI never means them, and
    SafeLoader builds them in quadratic time (one ``*= 60`` per segment of a long scalar)."""

    def _construct(loader, node):
        value = loader.construct_scalar(node)
        return value if ":" in value else construct(loader, node)

    return _construct


_NoTimestampLoader.add_constructor("tag:yaml.org,2002:int", _no_base60(yaml.SafeLoader.construct_yaml_int))
_NoTimestampLoader.add_constructor(
    "tag:yaml.org,2002:float", _no_base60(yaml.SafeLoader.construct_yaml_float)
)


def _parse_openapi(payload: str) -> dict | None:
    """An inline OpenAPI spec as a dict: JSON first, then YAML. ``None`` when it is neither.

    The YAML result is round-tripped through JSON so anything still non-JSON (``!!binary``,
    ``!!set``) becomes a string; a structure JSON cannot hold (a self-referencing alias) is
    unparseable, not a crash."""
    try:
        spec = json.loads(payload)
    except ValueError:
        try:
            spec = yaml.load(payload, Loader=_NoTimestampLoader)  # noqa: S506 — a SafeLoader subclass with the timestamp resolver removed; expires 2027-10-23; added E49A-50A/T6
            spec = json.loads(json.dumps(spec, default=str))
        except (yaml.YAMLError, TypeError, ValueError):
            return None
    return spec if isinstance(spec, dict) else None


def _operation_input_schema(path_item: dict, op: dict) -> dict:
    """The tool input schema of one OpenAPI operation: an object built from the path and operation
    parameters (operation wins on a name clash) and the ``application/json`` request body, where an
    object body contributes its properties and any other body becomes a ``body`` property."""
    params = {
        p["name"]: p
        for p in (path_item.get("parameters") or []) + (op.get("parameters") or [])
        if isinstance(p, dict) and p.get("name")
    }
    properties: dict = {}
    required: list[str] = []
    for name, p in params.items():
        prop = dict(p.get("schema") or {})
        if p.get("description"):
            prop["description"] = p["description"]
        properties[name] = prop
        if p.get("required"):
            required.append(name)
    body = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema")
    if isinstance(body, dict):
        if body.get("type") == "object" and "properties" in body:
            properties.update(body["properties"])
            required.extend(body.get("required") or [])
        else:
            properties["body"] = body
            if (op.get("requestBody") or {}).get("required"):
                required.append("body")
    input_schema: dict = {"type": "object", "properties": properties}
    if required:
        input_schema["required"] = required
    return input_schema


def _openapi_tools(target_name: str, payload: str) -> list[McpTool]:
    """One :class:`McpTool` per OpenAPI operation that has an ``operationId``: the gateway's
    invocable name is ``<target>___<operationId>``, so an operation without one has no name and
    is skipped. ``$ref``s are kept as written (display only; the gateway does the real binding).
    An unparseable spec yields no tools."""
    spec = _parse_openapi(payload)
    if spec is None:
        logger.warning("[mcp_identity] OpenAPI target %s has an unparseable inline spec", target_name)
        return []
    tools: list[McpTool] = []
    for path_item in (spec.get("paths") or {}).values():
        if not isinstance(path_item, dict):
            continue
        for method in _OPENAPI_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict) or not op.get("operationId"):
                continue
            tools.append(
                McpTool(
                    name=f"{target_name}___{op['operationId']}",
                    description=op.get("summary") or op.get("description") or "",
                    input_schema=_operation_input_schema(path_item, op),
                )
            )
    return tools


def _same_tools(a: list[McpTool], b: list[McpTool]) -> bool:
    """Value equality of two tool lists, order-insensitive (a re-ordered discovery is no change)."""

    def key(tools: list[McpTool]) -> list[str]:
        return sorted(json.dumps(t.model_dump(), sort_keys=True) for t in tools)

    return key(a) == key(b)


async def _await(awaitable: Awaitable[Any]) -> Any:
    return await awaitable


def _run(result):
    """Drive a GraphService coroutine from sync code: a fresh loop when awaitable, a plain value
    passes through."""
    if inspect.isawaitable(result):
        return asyncio.run(_await(result))
    return result


class McpProvisioningError(AppError):
    """A step of the non-atomic Entra and AWS MCP provisioning sequence failed.

    The record's ``identity_status`` is persisted ``'failed'`` before this is raised, so a later
    re-provision can resume. The detail is fixed; the message names the MCP and the step for the
    log and never carries a Graph body or an AWS message.
    """

    domain: ClassVar[str] = "mcp_identity"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "provisioning_failed": Outcome(502, "MCP identity provisioning failed")
    }


class McpIdentityService:
    """Orchestrates Entra identity provisioning for an AgentCore gateway or runtime MCP."""

    def __init__(
        self,
        *,
        graph: GraphService,
        registry: McpServerRegistryService,
        control_client=None,
        region: str | None = None,
        tenant_id: str,
        login_base: str,
    ) -> None:
        self._graph = graph
        self._registry = registry
        self._region = region
        self._tenant_id = tenant_id
        self._login_base = login_base.rstrip("/")
        self._control = control_client or aws_clients.client("bedrock-agentcore-control", region=region)

    async def provision(self, mcp: McpServer) -> McpServer:
        """Idempotent, resumable provisioning (see the module docstring for the sequence).

        The sync boto3 steps run off the event loop through ``anyio.to_thread.run_sync`` because
        this runs as a background task on the uvicorn loop and the authorizer configuration polls
        for minutes. On any failure except tool discovery: ``identity_status='failed'`` is persisted
        and :class:`McpProvisioningError` is raised from the cause.
        """
        try:
            logger.info("[mcp_identity] provisioning MCP %s (kind=%s)", mcp.id, mcp.kind)
            handle, current_authorizer_type = self._read_resource(mcp)
            mcp.identity_status = IdentityStatus.PENDING
            await self._discover_tools(mcp, current_authorizer_type)
            sp_id = await self._ensure_entra_app(mcp)
            await anyio.to_thread.run_sync(self._registry.persist_identity, mcp)
            await self._graph.set_assignment_required(sp_id, required=False)
            if mcp.kind == Kind.GATEWAY:
                await anyio.to_thread.run_sync(self._configure_gateway_authorizer, handle, mcp)
            else:
                await anyio.to_thread.run_sync(self._configure_runtime_authorizer, handle, mcp)
            logger.info("[mcp_identity] MCP %s inbound CUSTOM_JWT authorizer configured", mcp.id)
            mcp.identity_status = IdentityStatus.PROVISIONED
            await anyio.to_thread.run_sync(self._registry.persist_identity, mcp)
            logger.info("[mcp_identity] MCP %s provisioned", mcp.id)
            return mcp
        except Exception as err:
            await self._persist_failed(mcp)
            if isinstance(err, GraphError):
                logger.warning("[mcp_identity] provisioning failed for MCP %s: %s", mcp.id, err)
            else:
                logger.exception("[mcp_identity] provisioning failed for MCP %s (unexpected error)", mcp.id)
            raise McpProvisioningError(f"provisioning failed for MCP {mcp.id}") from err

    def _read_resource(self, mcp: McpServer) -> tuple[str, Optional[str]]:
        """Read the resource and capture its identifiers in memory.

        For a gateway: the short gateway id is parsed from the ARN (control calls take the id, not
        the ARN), the ``gatewayUrl`` is stored verbatim, and the current ``authorizerType`` is
        returned because it decides whether the token-less wire scan is still possible. For a
        runtime: the read only surfaces a missing or failed resource early. Returns the handle the
        authorizer step needs (gateway id or runtime ARN) and the authorizer type.
        """
        if mcp.kind == Kind.GATEWAY:
            gateway_arn = mcp.gateway_arn
            if not gateway_arn:
                raise McpProvisioningError(f"MCP {mcp.id} is a gateway without a gateway_arn")
            gateway_id = self._gateway_id_from_arn(gateway_arn)
            mcp.gateway_id = gateway_id
            gw = self._control.get_gateway(gatewayIdentifier=gateway_id)
            if gw.get("gatewayUrl"):
                mcp.gateway_url = gw["gatewayUrl"]
            current_authorizer_type = gw.get("authorizerType")
            logger.info(
                "[mcp_identity] MCP %s gateway_id=%s authorizer=%s",
                mcp.id,
                gateway_id,
                current_authorizer_type,
            )
            return gateway_id, current_authorizer_type
        runtime_arn = mcp.runtime_arn
        if not runtime_arn:
            raise McpProvisioningError(f"MCP {mcp.id} is a runtime without a runtime_arn")
        self._control.get_agent_runtime(agentRuntimeId=runtime_arn.rsplit("/", 1)[-1])
        return runtime_arn, None

    async def _discover_tools(self, mcp: McpServer, current_authorizer_type: Optional[str]) -> None:
        """Best-effort tool discovery for a gateway; a runtime has no tools to discover.

        The native read runs first and off the loop; the token-less wire scan runs only when it
        found nothing and the gateway is still open. A failed read or scan is logged and never
        fails provisioning; ``available_tools`` is overwritten only on a non-empty result, so an
        empty or failed discovery keeps the seeded tools. One outcome line names the source.
        """
        if mcp.kind != Kind.GATEWAY:
            logger.info(
                "[mcp_identity] MCP %s tool discovery skipped (kind=%s, gateway-only)", mcp.id, mcp.kind
            )
            return
        if not mcp.gateway_id:
            return
        tools: list[McpTool] = []
        source = "none"
        try:
            tools = await anyio.to_thread.run_sync(self._read_gateway_tools_native, mcp.gateway_id)
            if tools:
                source = "native"
        except Exception:
            logger.warning(
                "[mcp_identity] native tool read failed for MCP %s (continuing)", mcp.id, exc_info=True
            )
        if not tools and mcp.gateway_url and current_authorizer_type in _OPEN_AUTHORIZER_TYPES:
            try:
                tools = await scan_mcp_tools(mcp.gateway_url, bearer=None)
                if tools:
                    source = "wire-scan"
            except McpScanError as scan_err:
                logger.warning(
                    "[mcp_identity] pre-lockdown wire-scan failed for MCP %s "
                    "(continuing, seeded tools kept): %s",
                    mcp.id,
                    scan_err,
                )
        if tools:
            mcp.available_tools = tools
        logger.info("[mcp_identity] MCP %s tool discovery: %d tools via %s", mcp.id, len(tools), source)

    async def _ensure_entra_app(self, mcp: McpServer) -> str:
        """Create the per-MCP Entra app registration and SP unless the record already carries an
        ``entra_sp_id`` (a re-provision), setting all five ids.

        ``entra_app_audience`` is the app URI form; the authorizer step feeds both it and the client
        GUID to ``allowedAudience`` so whichever form a token carries validates. A get-or-create
        that found the app but no SP raises before any later step: the app ids set here make the
        next re-provision resolve the duplicate instead of re-creating the identifier URI.
        """
        if mcp.entra_sp_id:
            return mcp.entra_sp_id
        created = await self._graph.create_mcp_app(mcp.id, mcp.name)
        mcp.entra_app_id = created["app_id"]
        mcp.entra_sp_id = created["sp_id"]
        mcp.entra_app_audience = created["app_uri"]
        mcp.invoker_role_id = created["invoker_role_id"]
        mcp.admin_role_id = created["admin_role_id"]
        if not mcp.entra_sp_id:
            raise McpProvisioningError(f"create_mcp_app returned no sp_id for MCP {mcp.id}")
        return mcp.entra_sp_id

    async def _persist_failed(self, mcp: McpServer) -> None:
        mcp.identity_status = IdentityStatus.FAILED
        try:
            await anyio.to_thread.run_sync(self._registry.persist_identity, mcp)
        except Exception:
            logger.exception("[mcp_identity] failed to persist 'failed' status for MCP %s", mcp.id)

    def mint_app(self, mcp: McpServer) -> McpServer:
        """Create the MCP's Entra app and SP and persist once; no gateway call.

        A repository-built gateway does not exist at materialize time, but Terraform needs the
        app's audience and id to create it born ``CUSTOM_JWT``, so the app is minted first and the
        build creates the gateway around it. Sync like the materialize step that calls it; the
        Graph coroutines run on a fresh loop. Idempotent: returns ``mcp`` unchanged when
        ``entra_sp_id`` is set. ``identity_status`` becomes ``pending``.
        """
        if mcp.entra_sp_id:
            return mcp
        try:
            created = _run(self._graph.create_mcp_app(mcp.id, mcp.name))
            mcp.entra_app_id = created["app_id"]
            mcp.entra_sp_id = created["sp_id"]
            mcp.entra_app_audience = created["app_uri"]
            mcp.invoker_role_id = created["invoker_role_id"]
            mcp.admin_role_id = created["admin_role_id"]
            if not mcp.entra_sp_id:
                raise McpProvisioningError(f"create_mcp_app returned no sp_id for MCP {mcp.id}")
            mcp.identity_status = IdentityStatus.PENDING
            self._registry.persist_identity(mcp)
            _run(self._graph.set_assignment_required(mcp.entra_sp_id, required=False))
        except McpProvisioningError:
            raise
        except Exception as err:
            raise McpProvisioningError(f"minting the Entra app failed for MCP {mcp.id}") from err
        logger.info("[mcp_identity] MCP %s Entra app minted (no gateway yet)", mcp.id)
        return mcp

    def adopt_built_gateway(
        self, mcp: McpServer, *, gateway_arn: str, gateway_id: str, gateway_url: str
    ) -> McpServer:
        """Record a Terraform-built gateway on the MCP record.

        ``GetGateway`` as the existence check, native tool discovery (OpenAPI included), then one
        persist with the gateway fields, the tools and ``identity_status=PROVISIONED``, or no write
        at all when every one of those already matches: every repository read reconciles through
        here and a registry write resets ``APPROVED`` to ``DRAFT``. Never calls ``UpdateGateway``
        (Terraform owns the authorizer). Discovery is best-effort and keeps the existing tools on
        an empty or failed read. Raises :class:`McpProvisioningError` when the gateway cannot be
        read.
        """
        try:
            self._control.get_gateway(gatewayIdentifier=gateway_id)
        except Exception as err:
            raise McpProvisioningError(f"gateway {gateway_id} for MCP {mcp.id} could not be read") from err
        tools: list[McpTool] = []
        try:
            tools = self._read_gateway_tools_native(gateway_id)
        except Exception:
            logger.warning(
                "[mcp_identity] native tool read failed adopting MCP %s (keeping tools)",
                mcp.id,
                exc_info=True,
            )
        tools = tools or mcp.available_tools
        if (
            mcp.gateway_arn == gateway_arn
            and mcp.gateway_id == gateway_id
            and mcp.gateway_url == gateway_url
            and mcp.identity_status == IdentityStatus.PROVISIONED
            and _same_tools(tools, mcp.available_tools)
        ):
            return mcp
        mcp.gateway_arn = gateway_arn
        mcp.gateway_id = gateway_id
        mcp.gateway_url = gateway_url
        mcp.available_tools = tools
        mcp.identity_status = IdentityStatus.PROVISIONED
        self._registry.persist_identity(mcp)
        logger.info(
            "[mcp_identity] MCP %s adopted built gateway %s (%d tools)", mcp.id, gateway_id, len(tools)
        )
        return mcp

    async def refresh_tools(self, mcp: McpServer) -> McpServer:
        """Re-read a gateway's tools natively and persist them, without touching identity.

        Derives the gateway id from ``mcp.gateway_id`` or ``mcp.gateway_arn``; with neither the
        record is returned unchanged. A read failure is logged and the record returned unchanged
        (a flaky read must not fail the refresh button); ``available_tools`` is overwritten and
        persisted only on a non-empty result that differs from the stored list, because every
        registry write resets an ``APPROVED`` record to ``DRAFT``.
        """
        gateway_id = mcp.gateway_id or (
            self._gateway_id_from_arn(mcp.gateway_arn) if mcp.gateway_arn else None
        )
        if not gateway_id:
            return mcp

        tools: list[McpTool] = []
        try:
            tools = await anyio.to_thread.run_sync(self._read_gateway_tools_native, gateway_id)
        except Exception:
            logger.warning(
                "[mcp_identity] refresh tool read failed for MCP %s (returning unchanged)",
                mcp.id,
                exc_info=True,
            )
            return mcp

        if tools and not _same_tools(tools, mcp.available_tools):
            mcp.available_tools = tools
            await anyio.to_thread.run_sync(self._registry.persist_identity, mcp)
        logger.info("[mcp_identity] refresh_tools MCP %s: %d tools", mcp.id, len(tools))
        return mcp

    @staticmethod
    def _gateway_id_from_arn(gateway_arn: str) -> str:
        """The short gatewayId from a gatewayArn, whose tail is ``.../gateway/{gatewayId}``."""
        return gateway_arn.rsplit("/", 1)[-1]

    def _read_gateway_tools_native(self, gateway_id: str) -> list[McpTool]:
        """Read a gateway's tools from the control plane; no token, works on a locked gateway.

        ``ListGatewayTargets`` (paginated, bounded by ``_MAX_TARGET_PAGES``) then
        ``GetGatewayTarget`` per target; see :meth:`_target_tools` for which targets contribute.
        Best-effort per target: one bad target is skipped, not fatal. Sync boto3, dispatched off
        the loop by the caller. The summary line counts targets seen and skipped so "the gateway
        has tools but we read none" is answerable from the log.
        """
        tools: list[McpTool] = []
        n_targets = 0
        n_skipped = 0
        next_token: str | None = None
        for _ in range(_MAX_TARGET_PAGES):
            kwargs: dict = {"gatewayIdentifier": gateway_id}
            if next_token:
                kwargs["nextToken"] = next_token
            page = self._control.list_gateway_targets(**kwargs)
            for item in page.get("items") or []:
                target_id = item.get("targetId")
                if not target_id:
                    continue
                n_targets += 1
                target_tools = self._target_tools(gateway_id, target_id, item.get("name") or "")
                if target_tools is None:
                    n_skipped += 1
                else:
                    tools.extend(target_tools)
            next_token = page.get("nextToken")
            if not next_token:
                break
        logger.info(
            "[mcp_identity] native tool read for gateway %s: %d tools across %d targets (%d skipped)",
            gateway_id,
            len(tools),
            n_targets,
            n_skipped,
        )
        return tools

    def _target_tools(self, gateway_id: str, target_id: str, list_name: str) -> list[McpTool] | None:
        """The tools one gateway target declares, or ``None`` when the target is skipped.

        A LAMBDA target with an inline ``toolSchema`` maps each entry to the invocable
        ``<targetName>___<toolName>``; an OpenAPI target with an inline spec maps one tool per
        ``operationId`` (an unparseable or empty spec is a skip, logged, and never loses the other
        targets). Every other target (S3-hosted specs, ``smithyModel``, ``mcpServer``,
        ``apiGateway``, a lambda with an S3 schema) needs the wire scan and is skipped, as is a
        target whose read fails. The target name is the invocable prefix: ``GetGatewayTarget``'s
        name first, the list item's as the fallback.
        """
        try:
            gt = self._control.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
        except Exception:
            logger.debug(
                "[mcp_identity] GetGatewayTarget failed for target %s on gateway %s (skipping)",
                target_id,
                gateway_id,
                exc_info=True,
            )
            return None
        tc = gt.get("targetConfiguration") or {}
        target_name = gt.get("name") or list_name
        spec_payload = ((tc.get("mcp") or {}).get("openApiSchema") or {}).get("inlinePayload")
        if spec_payload:
            try:
                openapi_tools = _openapi_tools(target_name, spec_payload)
            except Exception:
                logger.warning(
                    "[mcp_identity] OpenAPI target %s on gateway %s skipped",
                    target_name,
                    gateway_id,
                    exc_info=True,
                )
                return None
            return openapi_tools or None
        lam = (tc.get("mcp") or {}).get("lambda") or {}
        inline_payload = (lam.get("toolSchema") or {}).get("inlinePayload")
        if not inline_payload:
            logger.debug(
                "[mcp_identity] gateway %s target %s has no inline lambda toolSchema (skipping native read)",
                gateway_id,
                target_id,
            )
            return None
        return [
            McpTool(
                name=f"{target_name}___{tool['name']}",
                description=tool.get("description") or "",
                input_schema=tool.get("inputSchema") or {},
            )
            for tool in inline_payload
            if tool.get("name")
        ]

    def _discovery_url(self) -> str:
        """The Entra v2 OIDC well-known URL for our tenant, the same one the agent-side runtime
        authorizer uses."""
        return f"{self._login_base}/{self._tenant_id}/v2.0/.well-known/openid-configuration"

    @staticmethod
    def _allowed_audience(mcp: McpServer) -> list[str]:
        """Both audience forms, the ``api://`` URI and the app client GUID, in that order, dropping
        any that is unset: a token's ``aud`` can be either, and each is per-MCP unique."""
        return [a for a in (mcp.entra_app_audience, mcp.entra_app_id) if a]

    def _require_audience(self, mcp: McpServer) -> list[str]:
        """The allowed audience, refusing an empty one before any update: an empty
        ``allowedAudience`` rejects every token with no clear error."""
        audience = self._allowed_audience(mcp)
        if not audience:
            raise McpProvisioningError(
                f"refusing to configure CUSTOM_JWT with empty allowedAudience for MCP {mcp.id}"
            )
        return audience

    def _configure_gateway_authorizer(self, gateway_id: str, mcp: McpServer) -> None:
        """Sync boto3: set the gateway's inbound ``CUSTOM_JWT`` authorizer and poll to READY.

        ``UpdateGateway`` is a full-replace PUT, so the required fields (``name``, ``roleArn``) and
        every optional field present on the gateway are replayed from ``GetGateway``;
        ``protocolConfiguration`` carries settings that can only be set at create time. AGP owns
        ``discoveryUrl`` and ``allowedAudience`` only: under the same Entra issuer every other
        authorizer field already on the gateway is carried through (a Terraform-built gateway is
        born with ``allowedScopes=["Invoke"]``, the gate that refuses app-only tokens), while a
        gateway locked to another issuer is rebuilt fresh because its clients and scopes belong
        to that IdP.
        """
        gw = self._control.get_gateway(gatewayIdentifier=gateway_id)
        audience = self._require_audience(mcp)
        discovery_url = self._discovery_url()
        existing = (gw.get("authorizerConfiguration") or {}).get("customJWTAuthorizer") or {}
        if existing.get("discoveryUrl") != discovery_url:
            existing = {}
        kwargs: dict = {
            "gatewayIdentifier": gw["gatewayId"],
            "name": gw["name"],
            "roleArn": gw["roleArn"],
            "authorizerType": "CUSTOM_JWT",
            "authorizerConfiguration": {
                "customJWTAuthorizer": {
                    **existing,
                    "discoveryUrl": discovery_url,
                    "allowedAudience": audience,
                }
            },
        }
        for key in _GATEWAY_OPTIONAL_FIELDS:
            if gw.get(key):
                kwargs[key] = gw[key]
        self._control.update_gateway(**kwargs)
        self._poll_gateway_ready(gateway_id)

    def _poll_gateway_ready(self, gateway_id: str) -> None:
        """Poll ``get_gateway`` until READY; a failed status or an exhausted budget raises so the
        caller marks the MCP ``failed``."""
        for _ in range(_POLL_MAX_ATTEMPTS):
            resp = self._control.get_gateway(gatewayIdentifier=gateway_id)
            status = resp.get("status")
            if status == _READY_STATUS:
                return
            if status in _FAILED_STATUSES:
                raise RuntimeError(f"gateway {gateway_id} reached terminal status {status}")
            time.sleep(_POLL_INTERVAL_SECONDS)
        raise RuntimeError(f"gateway {gateway_id} did not reach READY within {_POLL_MAX_ATTEMPTS} polls")

    def _configure_runtime_authorizer(self, runtime_arn: str, mcp: McpServer) -> None:
        """Sync boto3: set a runtime MCP's inbound JWT authorizer and poll to READY.

        The same ``customJWTAuthorizer`` shape and the same ``GetAgentRuntime`` -> replay ->
        ``UpdateAgentRuntime`` path as the agent side; the control calls take the runtime id (the
        ARN's last segment). Optional fields present on the runtime are replayed so the update
        drops none of them.
        """
        runtime_id = runtime_arn.rsplit("/", 1)[-1]
        current = self._control.get_agent_runtime(agentRuntimeId=runtime_id)
        audience = self._require_audience(mcp)
        kwargs: dict = {
            "agentRuntimeId": runtime_id,
            "agentRuntimeArtifact": current["agentRuntimeArtifact"],
            "roleArn": current["roleArn"],
            "networkConfiguration": current["networkConfiguration"],
            "authorizerConfiguration": {
                "customJWTAuthorizer": {
                    "discoveryUrl": self._discovery_url(),
                    "allowedAudience": audience,
                }
            },
        }
        for key in _RUNTIME_OPTIONAL_FIELDS:
            if current.get(key):
                kwargs[key] = current[key]
        self._control.update_agent_runtime(**kwargs)
        self._poll_runtime_ready(runtime_id)

    def _poll_runtime_ready(self, runtime_id: str) -> None:
        """Poll ``get_agent_runtime`` until READY; a failed status or an exhausted budget raises so
        the caller marks the MCP ``failed``."""
        for _ in range(_POLL_MAX_ATTEMPTS):
            resp = self._control.get_agent_runtime(agentRuntimeId=runtime_id)
            status = resp.get("status")
            if status == _READY_STATUS:
                return
            if status in _FAILED_STATUSES:
                raise RuntimeError(f"runtime {runtime_id} reached terminal status {status}")
            time.sleep(_POLL_INTERVAL_SECONDS)
        raise RuntimeError(f"runtime {runtime_id} did not reach READY within {_POLL_MAX_ATTEMPTS} polls")
