"""Tenants: the multi-tenancy unit of ownership, persisted through ``core.state_store`` and validated
on write.

A tenant is a line-of-business unit that owns agents, MCP servers and projects (they carry a
``tenant_id``) and maps to one or more Entra groups plus the unit's per-stage platform accounts.
``platform`` is set on create and immutable afterwards: every stage is validated against the
stored platform's shape, so a tenant can never carry the other platform's config.

A Databricks tenant holds credentials: the workspace service principal's client secret per stage
and an optional account-admin pair per tenant. They live in Secrets Manager only; the record
carries the ARNs (``sp_client_secret_arn`` per stage, ``account_admin_secret_arn`` per tenant)
and the input models' credential fields are write-only. Create mints the secrets, persists the
record and rolls the secrets back if the persist fails. Update preserves the stored ARNs and
ignores body-supplied ones, rotates on a newly typed secret, and prunes the secrets of removed
stages only after the write committed, with a recoverable delete. Delete removes the record
first, then the secrets by the ARNs the record carries. The account-admin secret is named
``<prefix><tenant>/account-admin`` beside the per-stage secrets ``<prefix><tenant>/<stage>``, so a
Databricks stage named ``account-admin`` is refused: the two names would collide.

An AWS stage's ``deploy_role_arn`` is validated on write against ``_ROLE_ARN_RE``, the write-side
twin of the parser in ``tenant_credentials`` (the two accept the same set, which a test pins): the
stored value reaches CodeBuild as the plaintext ``TARGET_ROLE_ARN`` and the buildspec writes it into
a double-quoted HCL string inside an unquoted heredoc, so the pattern bounds every path segment to
the IAM name charset, matches twelve ASCII digits rather than the digit class (which also accepts
non-ASCII digits) and anchors on the absolute end of the string rather than ``$`` (which tolerates a
trailing newline).

Creating or updating a tenant runs the connect-time probe and persists ``capabilities`` plus the
computed ``binding_mode``. Databricks probes fail closed and never block a write: a tenant whose
probe failed is badged, not refused, and ``binding_mode`` is ``federation`` only when both
``account_admin`` and ``user_sync`` hold, else ``invoke_unavailable``; the connect flow never
assigns ``sp_secret``, which stays dormant on purpose: an automatic degrade onto a shared
service-principal identity would break the audit trail at the platform's boundary. AWS tenants
probe each stage's deploy role for reach and keep an empty ``binding_mode``. The Databricks probe
is async and drives a fresh event loop through ``asyncio.run``, which is why the tenant write
routes are sync handlers.

A save rewrites the row only at the version it read, so one that lost to a concurrent write or
delete answers ``changed`` and recreates nothing.

The clock (``now``) and the id source (``new_id``) are injectable.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from collections.abc import Awaitable, Mapping
from datetime import datetime, timezone
from typing import Any, ClassVar, Dict, Final, List, Optional, Tuple
from uuid import uuid4

from botocore.exceptions import ClientError

from core import aws_clients
from core.errors import AppError, Outcome
from core.state_store import STORE_FAULTS, StateStore, VersionMismatch
from models.tenant import (
    ACCOUNT_ADMIN_ID_KEY,
    ACCOUNT_ADMIN_SECRET_KEY,
    ACCOUNT_ID_RE,
    DATABRICKS_ACCOUNT_ID_RE,
    SP_SECRET_KEY,
    WORKSPACE_ID_RE,
    WORKSPACE_URL_RE,
    DatabricksStageConfig,
    StageConfig,
    Tenant,
    TenantBindingMode,
    TenantCreate,
    TenantPlatform,
    TenantStageConfig,
    TenantUpdate,
    hydrate_tenant_item,
)

logger = logging.getLogger(__name__)

_PARTITION_KEY = "tenant"
_TENANT_ID_PREFIX = "ten-"

_ROLE_ARN_RE = re.compile(r"^arn:aws[\w-]*:iam::[0-9]{12}:role/(?:[\w+=,.@-]{1,64}/)*[\w+=,.@-]{1,64}\Z")

_DEFAULT_SECRET_PREFIX = "agp-dev/databricks-tenants/"  # noqa: S105 — a Secrets Manager name prefix, not a credential; expires 2027-10-23; added E49A-50A/T7
_ACCOUNT_ADMIN_SUFFIX = "account-admin"
_CAPABILITY_KEYS = ("can_discover", "account_admin", "user_sync")

BINDING_FEDERATION: Final = "federation"
BINDING_SP_SECRET: Final = "sp_secret"  # noqa: S105 — a binding-mode name, not a credential; expires 2027-10-23; added E49A-50A/T7
BINDING_INVOKE_UNAVAILABLE: Final = "invoke_unavailable"


class TenantError(AppError):
    """A tenant operation was refused or failed.

    Every kind has a fixed detail, so the message never reaches a client; it names the rule or
    the stage for the log and for callers that branch on ``.kind``. ``discovery_failed`` is
    raised by ``runtime_catalog.discover_agents`` for an adapter failure that is not a
    ``CatalogError``. ``not_permitted``, ``unknown_stage`` and ``referenced`` are refusals the
    routes decide: a tenant outside the caller's memberships, a stage the tenant does not list, a
    tenant some agent, MCP server or project still names or whose references cannot be read.
    ``changed`` is a save that lost to a concurrent write or delete of the row.
    """

    domain: ClassVar[str] = "tenant"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "not_found": Outcome(404, "Tenant not found"),
        "name_taken": Outcome(409, "tenant name already exists"),
        "validation": Outcome(400, "invalid tenant"),
        "secret_error": Outcome(502, "Secret store operation failed"),
        "discovery_failed": Outcome(502, "platform discovery failed (unknown)"),
        "unknown_stage": Outcome(400, "unknown stage"),
        "referenced": Outcome(409, "tenant is referenced by existing resources"),
        "not_permitted": Outcome(403, "tenant not permitted"),
        "changed": Outcome(409, "Tenant changed while this request ran; reload and retry"),
    }

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message, kind)


async def _await(awaitable: Awaitable[Any]) -> Any:
    return await awaitable


class TenantService:
    def __init__(
        self,
        table_name: str = "",
        region: str | None = None,
        *,
        secret_prefix: str = _DEFAULT_SECRET_PREFIX,
        workspace=None,
        secrets_client=None,
        aws_reach_prober=None,
        new_id=lambda: f"{_TENANT_ID_PREFIX}{uuid4().hex[:8]}",
        now=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.table_name = table_name
        self.region = region
        self.secret_prefix = secret_prefix
        self._new_id = new_id
        self._now = now
        self._aws_reach_prober = aws_reach_prober
        self._workspace = workspace
        self._sm = secrets_client
        self._store = StateStore(table_name, region=region)

    def list(self) -> List[Tenant]:
        return self._load_all()

    def get(self, tenant_id: str, *, consistent: bool = False) -> Tenant:
        record = self._get(tenant_id, consistent=consistent)
        if record is None:
            raise TenantError("Unknown tenant", kind="not_found")
        return record

    def create(self, data: TenantCreate, created_by: str) -> Tenant:
        """Validate, then for a Databricks tenant mint the credentials, probe, and persist.

        Validation runs before any side effect, so a rejected body stores nothing, secret
        included. A persist failure after the secrets were written rolls them back and raises
        ``secret_error``.
        """
        self._validate_stages(data.entra_group_ids, data.stages, platform=data.platform)
        self._require_name_available(data.name)

        tenant_id = self._new_id()
        is_dbx = data.platform == TenantPlatform.DATABRICKS

        stages, admin_arn, admin_creds = (
            self._mint_credentials(tenant_id, data) if is_dbx else (dict(data.stages), "", (None, None))
        )

        capabilities, binding_mode = self._probe_connect(
            data.platform, stages, admin_creds, previous=None, admin_unresolved=False
        )

        ts = self._now().isoformat()
        record = Tenant(
            id=tenant_id,
            name=data.name,
            line_of_business=data.line_of_business,
            entra_group_ids=list(data.entra_group_ids),
            platform=data.platform,
            stages=stages,
            capabilities=capabilities,
            binding_mode=binding_mode,
            account_admin_secret_arn=admin_arn,
            description=data.description,
            created_by=created_by,
            created_at=ts,
            updated_at=ts,
        )
        try:
            if not self._create(record):
                raise RuntimeError("a tenant with this id already exists")
        except Exception:
            logger.exception("[tenants] persist failed for %s; rolling back secrets", tenant_id)
            self._delete_secrets_best_effort(tenant_id, stages.keys())
            raise TenantError("Failed to persist tenant record", kind="secret_error") from None
        return record

    def update(self, tenant_id: str, data: TenantUpdate) -> Tenant:
        """Merge the fields set on ``data`` onto the stored record, re-probe, persist.

        An explicit ``None`` means no change. ``stages`` merges per stage: a stage the body never
        names survives, a stage named in both is replaced whole, so a stage cannot be deleted
        through an update. Stages are validated against the stored platform. Secrets of stages
        the update dropped are pruned after the write committed; a write that lost to a concurrent
        change answers ``changed`` and prunes nothing.
        """
        record = self.get(tenant_id, consistent=True)
        changes = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
        if "stages" in changes:
            changes["stages"] = {**record.stages, **(data.stages or {})}

        merged_groups = changes.get("entra_group_ids", record.entra_group_ids)
        merged_stages = changes.get("stages", record.stages)
        self._validate_stages(merged_groups, merged_stages, platform=record.platform)

        new_name = changes.get("name")
        if new_name is not None and new_name != record.name:
            self._require_name_available(new_name)

        changes.pop("account_admin_client_id", None)
        changes.pop("account_admin_secret", None)

        orphaned: List[str] = []
        if record.platform == TenantPlatform.DATABRICKS:
            (
                merged_stages,
                admin_arn,
                admin_creds,
                orphaned,
            ) = self._merge_databricks_credentials(record, data, merged_stages)
            changes["stages"] = merged_stages
            changes["account_admin_secret_arn"] = admin_arn
            capabilities, binding_mode = self._probe_connect(
                record.platform,
                merged_stages,
                admin_creds,
                previous=record,
                admin_unresolved=bool(admin_arn) and not all(admin_creds),
            )
        else:
            capabilities, binding_mode = self._probe_connect(
                record.platform,
                merged_stages,
                (None, None),
                previous=record,
                admin_unresolved=False,
            )
        changes["capabilities"] = capabilities
        changes["binding_mode"] = binding_mode

        updated = record.model_copy(update={**changes, "updated_at": self._now().isoformat()})
        self._save(updated)
        if orphaned:
            self._prune_secrets(orphaned, record.id)
        return updated

    def reprobe(self, tenant_id: str) -> Tenant:
        """Re-run the connect-time probe against the stored config and persist the result: an
        empty update, so the probe and persist path has one home."""
        return self.update(tenant_id, TenantUpdate())

    def delete(self, tenant_id: str) -> None:
        """Remove the record, then its secrets by the ARNs the record carries. The route runs the
        reference check; this does none."""
        record = self.get(tenant_id)
        self._delete(record.id)
        self._delete_secrets_by_arn(record)

    def upsert_seed(self, tenant: Tenant) -> Tenant:
        """Idempotent write by a fixed id, with no name-uniqueness check."""
        item = self._store.get((_PARTITION_KEY, tenant.id), consistent=True)
        if item is None:
            if not self._create(tenant):
                raise TenantError(f"Tenant {tenant.id} already exists", kind="changed")
        else:
            tenant._row_version = self._from_item(item)._row_version
            self._save(tenant)
        return tenant

    def _mint_credentials(
        self, tenant_id: str, data: TenantCreate
    ) -> Tuple[Dict[str, StageConfig], str, Tuple[Optional[str], Optional[str]]]:
        """Store every supplied credential; return the stages with their ARNs filled in and the
        write-only values cleared, the account-admin ARN, and the plain pair for the probe."""
        stages: Dict[str, StageConfig] = {}
        for key, stage in data.stages.items():
            if isinstance(stage, DatabricksStageConfig) and stage.sp_client_secret:
                arn = self._put_secret(
                    self._stage_secret_name(tenant_id, key),
                    {SP_SECRET_KEY: stage.sp_client_secret},
                    tenant_id,
                )
                stages[key] = stage.model_copy(update={"sp_client_secret_arn": arn, "sp_client_secret": ""})
            else:
                stages[key] = stage

        admin_arn, admin_creds = self._mint_account_admin(
            tenant_id, data.account_admin_client_id, data.account_admin_secret
        )
        return stages, admin_arn, admin_creds

    def _merge_databricks_credentials(
        self, record: Tenant, data: TenantUpdate, merged_stages: Dict[str, StageConfig]
    ) -> Tuple[Dict[str, StageConfig], str, Tuple[Optional[str], Optional[str]], List[str]]:
        """Resolve the stages' and the account-admin credentials for an update.

        A body-supplied ``sp_client_secret_arn`` is discarded and the stored one restored; a newly
        typed secret is minted and its ARN taken. Returns ``(stages, account_admin_arn,
        account_admin_creds, orphaned_secret_arns)``: the last names the secrets whose stage the
        update dropped or renamed, compared by ARN so a rotation is not mistaken for a removal, for
        the caller to prune once the record is written. Nothing is deleted here.
        """
        stages: Dict[str, StageConfig] = {}
        for key, stage in merged_stages.items():
            if not isinstance(stage, DatabricksStageConfig):
                stages[key] = stage
                continue
            stored = record.stages.get(key)
            stored_arn = stored.sp_client_secret_arn if isinstance(stored, DatabricksStageConfig) else ""
            if stage.sp_client_secret:
                arn = self._put_secret(
                    self._stage_secret_name(record.id, key),
                    {SP_SECRET_KEY: stage.sp_client_secret},
                    record.id,
                )
            else:
                arn = stored_arn
            stages[key] = stage.model_copy(update={"sp_client_secret_arn": arn, "sp_client_secret": ""})

        surviving = {
            s.sp_client_secret_arn
            for s in stages.values()
            if isinstance(s, DatabricksStageConfig) and s.sp_client_secret_arn
        }
        orphaned = [
            s.sp_client_secret_arn
            for s in record.stages.values()
            if isinstance(s, DatabricksStageConfig)
            and s.sp_client_secret_arn
            and s.sp_client_secret_arn not in surviving
        ]

        supplied = data.model_dump(exclude_unset=True)
        if ACCOUNT_ADMIN_ID_KEY in supplied or ACCOUNT_ADMIN_SECRET_KEY in supplied:
            admin_arn, admin_creds = self._mint_account_admin(
                record.id, data.account_admin_client_id, data.account_admin_secret
            )
            if not admin_arn:
                admin_arn = record.account_admin_secret_arn
                admin_creds = self._resolve_account_admin_creds(record)
        else:
            admin_arn = record.account_admin_secret_arn
            admin_creds = self._resolve_account_admin_creds(record)
        return stages, admin_arn, admin_creds, orphaned

    def _resolve_account_admin_creds(self, record: Tenant) -> Tuple[Optional[str], Optional[str]]:
        """Read the stored account-admin pair back; ``(None, None)`` when there is nothing to read,
        the read fails, or only half a pair is stored."""
        if not record.account_admin_secret_arn:
            return None, None
        try:
            resp = self._secrets().get_secret_value(SecretId=record.account_admin_secret_arn)
            body = json.loads(resp["SecretString"])
        except Exception:
            logger.exception("[tenants] could not read a stored account-admin credential")
            return None, None
        client_id = body.get(ACCOUNT_ADMIN_ID_KEY) or None
        secret = body.get(ACCOUNT_ADMIN_SECRET_KEY) or None
        return (client_id, secret) if (client_id and secret) else (None, None)

    def _mint_account_admin(
        self, tenant_id: str, client_id: Optional[str], secret: Optional[str]
    ) -> Tuple[str, Tuple[Optional[str], Optional[str]]]:
        """Store the account-admin pair in one secret body, both halves or neither."""
        if not (client_id and secret):
            return "", (None, None)
        arn = self._put_secret(
            self._account_admin_secret_name(tenant_id),
            {ACCOUNT_ADMIN_ID_KEY: client_id, ACCOUNT_ADMIN_SECRET_KEY: secret},
            tenant_id,
        )
        return arn, (client_id, secret)

    def _probe_connect(
        self,
        platform: TenantPlatform,
        stages: Dict[str, StageConfig],
        admin_creds: Tuple[Optional[str], Optional[str]],
        *,
        previous: Optional[Tenant],
        admin_unresolved: bool,
    ) -> Tuple[Dict[str, bool], TenantBindingMode]:
        """The connect-time probe, dispatched by platform. The AWS branch calls the adapter's
        shared ``probe_aws_reach`` rather than its async wrapper, because the Databricks probe
        already owns a fresh event loop and ``asyncio.run`` cannot nest."""
        if platform == TenantPlatform.DATABRICKS:
            return self._probe(stages, admin_creds, previous=previous, admin_unresolved=admin_unresolved)
        return self._probe_aws(stages)

    def _probe_aws(self, stages: Dict[str, StageConfig]) -> Tuple[Dict[str, bool], TenantBindingMode]:
        from services.platform_adapters.aws import probe_aws_reach

        prober = self._aws_reach_prober
        if prober is None:
            return probe_aws_reach(stages), ""
        return probe_aws_reach(stages, reach_prober=prober), ""

    def _probe(
        self,
        stages: Dict[str, StageConfig],
        admin_creds: Tuple[Optional[str], Optional[str]],
        *,
        previous: Optional[Tenant] = None,
        admin_unresolved: bool = False,
    ) -> Tuple[Dict[str, bool], TenantBindingMode]:
        """Probe every credentialed Databricks stage and reduce to one capability set and a
        binding mode.

        ``can_discover`` is ``all()`` over the stages; ``account_admin`` and ``user_sync`` are
        account-level facts, so ``any()``. Every probe fails closed and never raises. When no stage
        resolves a credential the stored capabilities carry over from ``previous`` (a stored
        ``sp_secret`` mode becomes ``invoke_unavailable``); when the stored account-admin secret
        exists but could not be read (``admin_unresolved``), the two account-level flags carry over
        while ``can_discover`` takes the fresh answer.
        """
        probeable: List[Tuple[DatabricksStageConfig, str]] = []
        for stage in stages.values():
            if not isinstance(stage, DatabricksStageConfig):
                continue
            secret = self._resolve_stage_secret(stage)
            if secret:
                probeable.append((stage, secret))

        if not probeable:
            if previous is not None:
                carried = previous.binding_mode
                if carried == BINDING_SP_SECRET:
                    carried = BINDING_INVOKE_UNAVAILABLE
                return dict(previous.capabilities), carried
            return {k: False for k in _CAPABILITY_KEYS}, BINDING_INVOKE_UNAVAILABLE

        results: List[Dict[str, bool]] = []
        for stage, secret in probeable:
            admin_id, admin_secret = admin_creds if stage.account_id else (None, None)
            results.append(self._probe_one(stage, secret, admin_id, admin_secret))

        capabilities = {
            "can_discover": all(r.get("can_discover") for r in results),
            "account_admin": any(r.get("account_admin") for r in results),
            "user_sync": any(r.get("user_sync") for r in results),
        }
        if admin_unresolved and previous is not None:
            capabilities["account_admin"] = bool(previous.capabilities.get("account_admin"))
            capabilities["user_sync"] = bool(previous.capabilities.get("user_sync"))
        binding_mode = (
            BINDING_FEDERATION
            if capabilities["account_admin"] and capabilities["user_sync"]
            else BINDING_INVOKE_UNAVAILABLE
        )
        return capabilities, binding_mode

    def _probe_one(
        self,
        stage: DatabricksStageConfig,
        secret: str,
        admin_id: Optional[str],
        admin_secret: Optional[str],
    ) -> Dict[str, bool]:
        """One stage's probe on a fresh event loop, fail-closed; logs the traceback, never a
        credential."""
        try:
            result = self._workspace_service().probe_capabilities(
                stage.workspace_url,
                stage.account_id,
                stage.sp_client_id,
                secret,
                account_admin_client_id=admin_id,
                account_admin_secret=admin_secret,
            )
            if inspect.isawaitable(result):
                result = asyncio.run(_await(result))
        except Exception:
            logger.exception("[tenants] capability probe failed; failing closed")
            return {k: False for k in _CAPABILITY_KEYS}
        if not isinstance(result, dict):
            return {k: False for k in _CAPABILITY_KEYS}
        return {k: bool(result.get(k)) for k in _CAPABILITY_KEYS}

    def _resolve_stage_secret(self, stage: DatabricksStageConfig) -> str:
        """The stage's secret: the one just typed, else the stored one read back, else ``""``."""
        if stage.sp_client_secret:
            return stage.sp_client_secret
        if not stage.sp_client_secret_arn:
            return ""
        try:
            resp = self._secrets().get_secret_value(SecretId=stage.sp_client_secret_arn)
            return json.loads(resp["SecretString"]).get(SP_SECRET_KEY, "")
        except Exception:
            logger.exception("[tenants] could not read a stored Databricks stage secret")
            return ""

    def _workspace_service(self):
        if self._workspace is None:
            from services.databricks_workspace_service import DatabricksWorkspaceService

            self._workspace = DatabricksWorkspaceService()
        return self._workspace

    def _stage_secret_name(self, tenant_id: str, stage: str) -> str:
        return f"{self.secret_prefix}{tenant_id}/{stage}"

    def _account_admin_secret_name(self, tenant_id: str) -> str:
        return f"{self.secret_prefix}{tenant_id}/{_ACCOUNT_ADMIN_SUFFIX}"

    def _secrets(self):
        if self._sm is None:
            self._sm = aws_clients.client("secretsmanager", region=self.region)
        return self._sm

    def _restore_if_scheduled(self, name: str, tenant_id: str) -> None:
        """Restore a secret that is scheduled for deletion before writing into it; a live secret
        is left alone. A describe or restore fault raises ``secret_error``, because writing into
        a still-marked secret succeeds and leaves an unreadable credential."""
        sm = self._secrets()
        try:
            described = sm.describe_secret(SecretId=name)
        except sm.exceptions.ResourceNotFoundException:
            return
        except STORE_FAULTS:
            logger.exception("[tenants] describe_secret failed for tenant %s", tenant_id)
            raise TenantError("Failed to store the tenant credential", kind="secret_error") from None

        if not described.get("DeletedDate"):
            return

        try:
            sm.restore_secret(SecretId=name)
        except STORE_FAULTS:
            logger.exception("[tenants] restore_secret failed for tenant %s", tenant_id)
            raise TenantError("Failed to store the tenant credential", kind="secret_error") from None
        logger.info(
            "[tenants] restored a scheduled-for-deletion secret for tenant %s "
            "(a removed stage was re-added within the recovery window)",
            tenant_id,
        )

    def _put_secret(self, name: str, body: dict, tenant_id: str) -> str:
        """Create or rotate one secret and return its ARN. The value and the exception text are
        never logged."""
        secret_string = json.dumps(body)
        sm = self._secrets()
        try:
            resp = sm.create_secret(
                Name=name,
                SecretString=secret_string,
                Tags=[
                    {"Key": "managed_by", "Value": "agp"},
                    {"Key": "tenant_id", "Value": tenant_id},
                ],
            )
            return resp["ARN"]
        except sm.exceptions.ResourceExistsException:
            self._restore_if_scheduled(name, tenant_id)
            try:
                resp = sm.put_secret_value(SecretId=name, SecretString=secret_string)
                return resp["ARN"]
            except STORE_FAULTS:
                logger.exception("[tenants] put_secret_value failed for tenant %s", tenant_id)
                raise TenantError("Failed to store the tenant credential", kind="secret_error") from None
        except STORE_FAULTS:
            logger.exception("[tenants] create_secret failed for tenant %s", tenant_id)
            raise TenantError("Failed to store the tenant credential", kind="secret_error") from None

    def _delete_secrets_best_effort(self, tenant_id: str, stage_keys) -> None:
        """The create rollback: delete by the names just written, since no record holds ARNs."""
        names = [self._stage_secret_name(tenant_id, k) for k in stage_keys]
        names.append(self._account_admin_secret_name(tenant_id))
        self._delete_each_best_effort(names, tenant_id)

    def _delete_secrets_by_arn(self, record: Tenant) -> None:
        """Delete a stored tenant's secrets by the ARNs the record carries; silent when there are
        none."""
        if record.platform != TenantPlatform.DATABRICKS:
            return
        arns = [
            stage.sp_client_secret_arn
            for stage in record.stages.values()
            if isinstance(stage, DatabricksStageConfig) and stage.sp_client_secret_arn
        ]
        if record.account_admin_secret_arn:
            arns.append(record.account_admin_secret_arn)
        if not arns:
            return
        self._delete_each_best_effort(arns, record.id)

    def _prune_secrets(self, secret_ids: List[str], tenant_id: str) -> None:
        """A recoverable delete for secrets whose tenant survives."""
        self._delete_each_best_effort(secret_ids, tenant_id, force=False)

    def _delete_each_best_effort(self, secret_ids: List[str], tenant_id: str, *, force: bool = True) -> None:
        """Delete each secret, never raising: a missing secret is success, a store fault is logged
        and the remaining ids are still attempted."""
        sm = self._secrets()
        kwargs = {"ForceDeleteWithoutRecovery": True} if force else {}
        for secret_id in secret_ids:
            try:
                sm.delete_secret(SecretId=secret_id, **kwargs)
            except sm.exceptions.ResourceNotFoundException:
                pass
            except sm.exceptions.InvalidRequestException:
                logger.warning(
                    "[tenants] delete_secret was refused for tenant %s "
                    "(secret may already be scheduled for deletion, or its key is unavailable)",
                    tenant_id,
                )
            except STORE_FAULTS:
                logger.exception("[tenants] delete_secret failed for tenant %s", tenant_id)

    @classmethod
    def _validate_stages(
        cls,
        entra_group_ids: List[str],
        stages: Dict[str, StageConfig],
        *,
        platform: TenantPlatform,
    ) -> None:
        if not entra_group_ids:
            raise TenantError("A tenant requires at least one Entra group", kind="validation")
        if not stages:
            raise TenantError("A tenant requires at least one stage config", kind="validation")
        for key, stage in stages.items():
            if platform == TenantPlatform.DATABRICKS:
                cls._validate_databricks_stage(key, stage)
            else:
                cls._validate_aws_stage(key, stage)

    @staticmethod
    def _validate_aws_stage(key: str, stage: StageConfig) -> None:
        if not isinstance(stage, TenantStageConfig):
            raise TenantError(
                f"stage {key} must be an AWS stage config on an aws tenant",
                kind="validation",
            )
        if not ACCOUNT_ID_RE.match(stage.account_id or ""):
            raise TenantError(
                f"stage {key} account_id must be a 12-digit AWS account id",
                kind="validation",
            )
        if stage.deploy_role_arn and not _ROLE_ARN_RE.match(stage.deploy_role_arn):
            raise TenantError(
                f"stage {key} deploy_role_arn must be an IAM role ARN "
                "(arn:aws:iam::<account-id>:role/<name>) or empty",
                kind="validation",
            )

    @staticmethod
    def _validate_databricks_stage(key: str, stage: StageConfig) -> None:
        if not isinstance(stage, DatabricksStageConfig):
            raise TenantError(
                f"stage {key} must be a Databricks stage config on a databricks tenant",
                kind="validation",
            )
        if key.strip().lower() == _ACCOUNT_ADMIN_SUFFIX:
            raise TenantError(f"stage {key} uses a reserved name", kind="validation")
        if not WORKSPACE_URL_RE.fullmatch(stage.workspace_url or ""):
            raise TenantError(
                f"stage {key} workspace_url must be an https workspace origin "
                "with no port, path, or trailing slash",
                kind="validation",
            )
        if not stage.sp_client_id:
            raise TenantError(f"stage {key} requires a service principal client id", kind="validation")
        if not WORKSPACE_ID_RE.fullmatch(stage.workspace_id or ""):
            raise TenantError(f"stage {key} workspace_id must be a digits-string", kind="validation")
        if stage.account_id and not DATABRICKS_ACCOUNT_ID_RE.fullmatch(stage.account_id):
            raise TenantError(
                f"stage {key} account_id must be a Databricks account UUID",
                kind="validation",
            )

    def _require_name_available(self, name: str) -> None:
        if any(t.name == name for t in self._load_all()):
            raise TenantError("A tenant with this name already exists", kind="name_taken")

    def _get(self, tenant_id: str, *, consistent: bool = False) -> Tenant | None:
        try:
            item = self._store.get((_PARTITION_KEY, tenant_id), consistent=consistent)
        except ClientError:
            logger.exception("Failed to fetch tenant %s from DDB", tenant_id)
            return None
        return self._from_item(item) if item else None

    def _load_all(self) -> List[Tenant]:
        try:
            items = self._scan_partition()
        except ClientError:
            logger.exception("Failed to load tenants from DDB")
            return []
        return [self._from_item(i) for i in items]

    def _create(self, record: Tenant) -> bool:
        if not self._store.put_new({**self._to_item(record), "row_version": 1}):
            return False
        record._row_version = 1
        return True

    def _save(self, record: Tenant) -> None:
        written = (record._row_version or 0) + 1
        try:
            self._store.put_if_version(
                {**self._to_item(record), "row_version": written}, "row_version", record._row_version
            )
        except VersionMismatch:
            raise TenantError(f"Tenant {record.id} changed", kind="changed") from None
        record._row_version = written

    def _delete(self, tenant_id: str) -> None:
        self._store.delete((_PARTITION_KEY, tenant_id))

    def _scan_partition(self) -> List[dict]:
        return self._store.query(_PARTITION_KEY)

    def _to_item(self, record: Tenant) -> dict:
        return {
            "pk": _PARTITION_KEY,
            "sk": record.id,
            **json.loads(record.model_dump_json()),
        }

    def _from_item(self, item: dict) -> Tenant:
        clean = {k: v for k, v in item.items() if k not in ("pk", "sk")}
        clean = hydrate_tenant_item(clean)
        record = Tenant.model_validate(clean)
        version = item.get("row_version")
        record._row_version = None if version is None else int(version)
        return record
