"""The portable runtime-catalog seam: ``list_agents(tenant, stage)``, the agents one tenant runs on
one stage as the platform reports them, with one adapter per platform.

One method, deliberately: registration copies a ``DiscoveredAgent`` onto an agent record and AGP
knows everything else from its own registry, so a second verb here is how a platform-specific
concept enters a platform-neutral seam. ``runtime_handle`` is read from the platform's response and
never constructed: a Databricks endpoint URL is populated only for route-optimised endpoints, and
an AgentCore ARN embeds an account id. A record whose handle cannot be read is skipped with a count
in the log; a listing that fails raises, because a partial inventory presented as complete is worse
than a failed step. An adapter reaches only the credential the tenant record points at (a stage's
``sp_client_secret_arn`` or ``deploy_role_arn``), never ambient credentials or another tenant's
secret, and secrets never reach a log or an exception message. ``already_registered`` is AGP's own
knowledge, applied by ``mark_already_registered`` over handles the caller collected from the
registry.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from typing import ClassVar, Protocol, TypeVar, runtime_checkable

import anyio.to_thread
from pydantic import ConfigDict

from core import aws_clients
from core.aws_clients import AssumedCredentials
from core.errors import AppError, Outcome
from models.base import WireModel
from models.tenant import SP_SECRET_KEY, DatabricksStageConfig, Tenant, TenantPlatform, TenantStageConfig
from services.databricks_workspace_service import DatabricksError, DatabricksWorkspaceService
from services.tenant_service import TenantError

logger = logging.getLogger(__name__)

_SAFE_CODE = re.compile(r"[A-Za-z_]{1,64}")
_AGENT_ENTITY_TYPE = "CUSTOM_MODEL"
_RESERVED_ENDPOINT_PREFIX = "databricks-"
_MAX_PAGES = 100
_DISCOVERY_FAILED = "platform discovery failed"

# The discovery kinds cross to the frontend as these strings and select the create body there.
KIND_APP = "app"
KIND_SERVING_ENDPOINT = "serving_endpoint"
KIND_AGENTCORE_RUNTIME = "agentcore_runtime"

_StageConfig = TypeVar("_StageConfig", DatabricksStageConfig, TenantStageConfig)


def _safe_code(code: str | None) -> str:
    """An upstream error code is a safe code only if it looks like one: letters and underscores,
    bounded, matched in full so a trailing newline cannot smuggle a second line into a detail."""
    return code if code and _SAFE_CODE.fullmatch(code) else "unknown"


class CatalogError(AppError):
    """A discovery listing failed.

    One class for the whole seam: each adapter translates its platform's failures
    (``DatabricksError``, boto3's ``ClientError`` and ``BotoCoreError``) into a kind, so a third
    platform adds no handling anywhere else. Every kind has a fixed detail except
    ``databricks_error``, whose message is composed here from ``_DISCOVERY_FAILED`` and the
    platform's own code passed through ``_safe_code`` (an operator seeing ``PERMISSION_DENIED``
    learns something true); the upstream message never crosses.
    """

    domain: ClassVar[str] = "catalog"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "no_credential": Outcome(502, "platform discovery failed (no_credential)"),
        "credential_unreadable": Outcome(502, "platform discovery failed (credential_unreadable)"),
        "unknown_stage": Outcome(502, "platform discovery failed (unknown_stage)"),
        "wrong_stage_shape": Outcome(502, "platform discovery failed (wrong_stage_shape)"),
        "assume_role_failed": Outcome(502, "platform discovery failed (assume_role_failed)"),
        "listing_failed": Outcome(502, "platform discovery failed (listing_failed)"),
        "pagination_overflow": Outcome(502, "platform discovery failed (pagination_overflow)"),
        "databricks_error": Outcome(502, None),
    }

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message, kind)


class DiscoveredAgent(WireModel):
    """One agent as a platform reports it: only the facts AGP acts on cross, so a platform response
    body cannot reach a client through this. ``runtime_handle`` is the app URL (Databricks) or the
    runtime ARN (AgentCore), read and never built. ``kind`` is one of the ``KIND_*`` strings.
    ``state`` is the platform's raw state string, display-only: AGP's runtime-status union has one
    producer (``agent_identity_service``) and a listing that also asserted runtime status would be a
    second."""

    model_config = ConfigDict(frozen=True)

    name: str
    runtime_handle: str
    kind: str
    state: str
    created_by: str = ""
    already_registered: bool = False


class DiscoveredAgents(WireModel):
    """The ``GET /admin/tenants/{tenant_id}/discovered-agents`` answer: the listing and the
    platform it came from, so a client picks the create body without re-reading the tenant."""

    agents: list[DiscoveredAgent]
    platform: TenantPlatform


@runtime_checkable
class RuntimeCatalog(Protocol):
    """What AGP needs from an agent-hosting platform: one method. Structural, so an adapter
    neither inherits from nor registers with this; ``test_runtime_catalog`` compares the signature
    parameter by parameter against both adapters."""

    async def list_agents(self, tenant: Tenant, stage: str) -> list[DiscoveredAgent]:
        """The agents on ``tenant``'s ``stage``, as the platform reports them. Takes the tenant, not
        a workspace URL or an account id, so a caller cannot point discovery at a target the tenant
        does not own. Raises ``CatalogError`` rather than returning a shortened answer; an empty
        list means the platform was reached and reported no agents, an ordinary answer that must
        stay distinguishable from "AGP could not look". Records with no readable handle are skipped
        with a safe log line, the one shortened answer allowed."""
        ...


async def discover_agents(catalog: RuntimeCatalog, tenant: Tenant, stage: str) -> list[DiscoveredAgent]:
    """``catalog.list_agents(tenant, stage)``, with any failure that is not a ``CatalogError`` raised
    as the tenant domain's ``discovery_failed``: an adapter's unforeseen fault is still a discovery
    failure with a fixed detail, and its text never crosses."""
    try:
        return await catalog.list_agents(tenant, stage)
    except CatalogError:
        raise
    except Exception as exc:
        raise TenantError(f"discovery failed for tenant {tenant.id}", kind="discovery_failed") from exc


def mark_already_registered(
    agents: Iterable[DiscoveredAgent], known_handles: set[str]
) -> list[DiscoveredAgent]:
    """Flag every discovered agent whose handle AGP already governs. ``known_handles`` holds both
    currencies, an agent's ``runtime_handle`` and its ``agent_arn``, because one registry holds both
    platforms. A blank handle never matches: registry rows legitimately carry neither field, and a
    match on ``""`` would flag every discovered record as governed and hide the ungoverned agents the
    operator came to find."""
    live = {h for h in known_handles if h}
    return [
        a.model_copy(update={"already_registered": True}) if a.runtime_handle in live else a for a in agents
    ]


def _stage_config(tenant: Tenant, stage: str, expected: type[_StageConfig]) -> _StageConfig:
    """``tenant.stages[stage]`` confirmed to be ``expected``'s shape, or a safe ``CatalogError``: the
    route dispatches on ``tenant.platform``, and an adapter handed the wrong shape says so instead
    of ``AttributeError``-ing its way to a 500. The stage name is customer-chosen and never echoed."""
    cfg = tenant.stages.get(stage)
    if cfg is None:
        raise CatalogError("the tenant has no such stage", "unknown_stage")
    if not isinstance(cfg, expected):
        raise CatalogError("the tenant's stage config does not match its platform", "wrong_stage_shape")
    return cfg


class DatabricksCatalog:
    """Discovery on a Databricks workspace: apps first, then serving endpoints, merged into one
    inventory. Apps are where new agents land (Databricks publishes a migration away from serving
    endpoints for agent hosting); endpoints are the shape AGP can still discover and register.
    ``workspace`` and ``secrets_client`` are injectable; built by default."""

    def __init__(
        self,
        *,
        workspace: DatabricksWorkspaceService | None = None,
        secrets_client=None,
        region: str | None = None,
    ) -> None:
        self._workspace = workspace or DatabricksWorkspaceService()
        self._sm = secrets_client
        self._region = region

    def _secrets(self):
        if self._sm is None:
            self._sm = aws_clients.client("secretsmanager", region=self._region)
        return self._sm

    def _read_sp_secret(self, secret_arn: str) -> str:
        """The stage SP's client secret out of Secrets Manager (sync; off-loaded by the caller). A
        missing secret, a denied read, a malformed body and a wrong-shaped JSON all mean "AGP holds
        no usable credential for this stage" and all carry upstream text that must not cross, so one
        kind covers them and the log carries the traceback only."""
        try:
            resp = self._secrets().get_secret_value(SecretId=secret_arn)
            secret = json.loads(resp["SecretString"]).get(SP_SECRET_KEY, "")
        except Exception:
            logger.exception("[runtime_catalog] could not read a Databricks stage secret")
            raise CatalogError(
                "the tenant's Databricks credential could not be read", "credential_unreadable"
            ) from None
        if not secret:
            raise CatalogError("the tenant's Databricks credential is empty", "no_credential")
        return secret

    async def list_agents(self, tenant: Tenant, stage: str) -> list[DiscoveredAgent]:
        cfg = _stage_config(tenant, stage, DatabricksStageConfig)
        if not cfg.sp_client_secret_arn:
            raise CatalogError("the tenant's Databricks stage has no stored credential", "no_credential")
        secret = await anyio.to_thread.run_sync(self._read_sp_secret, cfg.sp_client_secret_arn)
        try:
            token = await self._workspace.mint_m2m_token(cfg.workspace_url, cfg.sp_client_id, secret)
            apps = await self._workspace.list_apps(cfg.workspace_url, token)
            endpoints = await self._workspace.list_serving_endpoints(cfg.workspace_url, token)
        except DatabricksError as err:
            raise CatalogError(f"{_DISCOVERY_FAILED} ({_safe_code(err.kind)})", "databricks_error") from None
        return [*self._from_apps(apps), *self._from_endpoints(endpoints)]

    @staticmethod
    def _app_handle(record: dict) -> str:
        """The app's URL, read from ``url`` or ``app_url``; ``""`` when neither is present, so the
        record is skipped rather than given a URL derived from its name."""
        for key in ("url", "app_url"):
            value = record.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    def _from_apps(self, records: list[dict]) -> list[DiscoveredAgent]:
        """The live Apps payload carries ``app_status`` and ``compute_status``, no bare ``status``;
        both keys are read."""
        found: list[DiscoveredAgent] = []
        skipped = 0
        for record in records:
            handle = self._app_handle(record)
            if not handle:
                skipped += 1
                continue
            status = record.get("status") or record.get("app_status") or {}
            found.append(
                DiscoveredAgent(
                    name=str(record.get("name") or ""),
                    runtime_handle=handle,
                    kind=KIND_APP,
                    state=str(status.get("state") or "") if isinstance(status, dict) else "",
                    created_by=str(record.get("creator") or ""),
                )
            )
        if skipped:
            logger.info("[runtime_catalog] skipped %d app(s) with no readable URL", skipped)
        return found

    @staticmethod
    def _is_agent_endpoint(record: dict) -> bool:
        """An agent's served entity is ``CUSTOM_MODEL`` (the other entity types are models a
        workspace serves), and the ``databricks-`` prefix is reserved for Databricks' own endpoints,
        whose foundation-model catalogue would otherwise dominate a customer's inventory."""
        name = str(record.get("name") or "")
        if name.startswith(_RESERVED_ENDPOINT_PREFIX):
            return False
        config = record.get("config")
        entities = config.get("served_entities") if isinstance(config, dict) else None
        if not isinstance(entities, list):
            return False
        return any(isinstance(e, dict) and e.get("entity_type") == _AGENT_ENTITY_TYPE for e in entities)

    @staticmethod
    def _endpoint_handle(record: dict) -> str:
        """The endpoint's invocation URL, top-level ``endpoint_url`` first, then
        ``data_plane_info.query_info.endpoint_url``. Both are populated only for route-optimised
        endpoints, whose real URL lives on another host entirely, so a legitimate record can carry
        neither and is then skipped rather than given the documented workspace-relative shape."""
        top = record.get("endpoint_url")
        if isinstance(top, str) and top:
            return top
        info = record.get("data_plane_info")
        query = info.get("query_info") if isinstance(info, dict) else None
        nested = query.get("endpoint_url") if isinstance(query, dict) else None
        return nested if isinstance(nested, str) and nested else ""

    def _from_endpoints(self, records: list[dict]) -> list[DiscoveredAgent]:
        found: list[DiscoveredAgent] = []
        skipped = 0
        for record in records:
            if not self._is_agent_endpoint(record):
                continue
            handle = self._endpoint_handle(record)
            if not handle:
                skipped += 1
                continue
            state = record.get("state") or {}
            found.append(
                DiscoveredAgent(
                    name=str(record.get("name") or ""),
                    runtime_handle=handle,
                    kind=KIND_SERVING_ENDPOINT,
                    state=str(state.get("ready") or "") if isinstance(state, dict) else "",
                    created_by=str(record.get("creator") or ""),
                )
            )
        if skipped:
            logger.info("[runtime_catalog] skipped %d serving endpoint(s) with no readable URL", skipped)
        return found


class AgentCoreCatalog:
    """Discovery on AWS: ``bedrock-agentcore-control.list_agent_runtimes`` in the tenant's account.
    A cross-account tenant's runtimes are invisible to the platform's own credentials, so the stage's
    ``deploy_role_arn`` is assumed exactly as the runtime build does, and an empty one means "read
    in place": a blank ``RoleArn`` is never assumed. ``sts_client`` and ``control_factory`` are
    injectable so tests can pin which credentials the control client was built with."""

    def __init__(self, *, region: str | None = None, sts_client=None, control_factory=None) -> None:
        self._region = region
        self._sts = sts_client
        self._control_factory = control_factory or _default_control_factory

    def _sts_client(self):
        if self._sts is None:
            self._sts = aws_clients.client("sts", region=self._region)
        return self._sts

    def _assume(self, role_arn: str) -> AssumedCredentials | None:
        """The credentials of ``role_arn`` once assumed, or ``None`` for an empty role: a blank
        ``RoleArn`` is never assumed and the stage is read in place. A denied assume, an expired
        platform credential and an unreachable STS endpoint all mean "AGP cannot reach this tenant's
        account", and all carry upstream text (role ARNs, principal ids) that must not cross into a
        detail."""
        if not role_arn:
            return None
        try:
            resp = self._sts_client().assume_role(RoleArn=role_arn, RoleSessionName="agp-runtime-catalog")
            creds = resp["Credentials"]
            return AssumedCredentials(creds["AccessKeyId"], creds["SecretAccessKey"], creds["SessionToken"])
        except Exception:
            logger.exception("[runtime_catalog] could not assume a tenant deploy role")
            raise CatalogError("AGP could not reach the tenant's AWS account", "assume_role_failed") from None

    def _list_runtimes(self, cfg: TenantStageConfig) -> list[dict]:
        """Page ``list_agent_runtimes`` in the stage's account and region (sync; off-loaded by the
        caller). The exit condition is upstream-controlled, so a repeated token is refused at once
        and ``_MAX_PAGES`` bounds a slower cycle; both raise, because a truncated inventory presented
        as complete is the lie a discovery surface would tell most convincingly."""
        credentials = self._assume(cfg.deploy_role_arn)
        control = self._control_factory(region=cfg.region or self._region, credentials=credentials)

        records: list[dict] = []
        token: str | None = None
        seen: set[str] = set()
        pages = 0
        while True:
            pages += 1
            if pages > _MAX_PAGES:
                raise CatalogError(
                    "the AWS account returned too many pages of runtimes", "pagination_overflow"
                )
            kwargs = {"nextToken": token} if token else {}
            try:
                resp = control.list_agent_runtimes(**kwargs)
            except Exception:
                logger.exception("[runtime_catalog] list_agent_runtimes failed")
                raise CatalogError("the AWS agent runtime listing failed", "listing_failed") from None
            page = resp.get("agentRuntimes")
            if isinstance(page, list):
                records.extend(r for r in page if isinstance(r, dict))
            token = resp.get("nextToken") or None
            if not token:
                return records
            token = str(token)
            if token in seen:
                raise CatalogError("the AWS account repeated a pagination token", "pagination_overflow")
            seen.add(token)

    async def list_agents(self, tenant: Tenant, stage: str) -> list[DiscoveredAgent]:
        cfg = _stage_config(tenant, stage, TenantStageConfig)
        records = await anyio.to_thread.run_sync(self._list_runtimes, cfg)

        found: list[DiscoveredAgent] = []
        skipped = 0
        for record in records:
            arn = record.get("agentRuntimeArn")
            if not isinstance(arn, str) or not arn:
                skipped += 1
                continue
            found.append(
                DiscoveredAgent(
                    name=str(record.get("agentRuntimeName") or ""),
                    runtime_handle=arn,
                    kind=KIND_AGENTCORE_RUNTIME,
                    state=str(record.get("status") or ""),
                    created_by="",
                )
            )
        if skipped:
            logger.info("[runtime_catalog] skipped %d runtime(s) with no ARN", skipped)
        return found


def _default_control_factory(*, region: str | None, credentials: AssumedCredentials | None):
    """The production ``bedrock-agentcore-control`` client, optionally with assumed credentials;
    tests inject a factory."""
    return aws_clients.client("bedrock-agentcore-control", region=region, credentials=credentials)
