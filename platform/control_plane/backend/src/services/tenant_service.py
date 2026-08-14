"""Service layer for Tenants (E24/T1) — the multi-tenancy unit of ownership.

A **Tenant** is a line-of-business unit that owns agents / MCP servers / projects (they
carry a ``tenant_id``) and maps to one or more Entra groups plus the LoB's per-stage AWS
accounts (metadata only; stage names are open — e.g. ``dev``/``uat``/``prod``). This
service persists tenants and validates them on write.

Persistence is DDB-or-local, cloned verbatim from ``connection_service`` /
``project_service``: the ``_has_ddb`` guard, ``boto3.resource("dynamodb")`` +
``.Table(name)``, a local dict + ``threading.Lock``, serialize via
``{"pk":..., "sk":..., **json.loads(model.model_dump_json())}`` and deserialize via
``Tenant.model_validate(clean)``. ``pk="tenant"``, ``sk=<id>``; a single-partition
``query`` loop pages on ``LastEvaluatedKey``.

E29/T3 — THIS SERVICE NOW HOLDS SECRETS (it did not before)
-----------------------------------------------------------
A Databricks tenant HAS a credential: its workspace service principal's client secret, plus
an optional account-admin pair. So the "no Secrets Manager path" this docstring used to claim
is gone, and ``connection_service``'s idiom is followed exactly — **create the secret, persist
the record carrying only the ARN, best-effort delete the secret if the persist then fails** —
because an orphaned secret is the failure that outlives every retry.

The secrets are why the credential FIELDS are write-only (``models.tenant``, OB-7): the input
models carry the values, the record carries only ``sp_client_secret_arn`` (per stage) and
``account_admin_secret_arn`` (per tenant). Before this the backend DROPPED all three keys —
``extra="ignore"`` swallowed what the admin form sent — so a credential was collected, a 201
was returned, and nothing was stored.

**A body-supplied ARN is IGNORED on update** (OB-10). The frontend echoes the ARN back so an
untouched secret box does not destroy the pointer, but the STORED value is what wins: a client
that could set this field could aim its own tenant at ANOTHER tenant's secret, which is a
cross-tenant credential read dressed up as a config edit. Mirrors how ``platform``
immutability is enforced — the stored value overrides whatever arrived.

CONNECT-TIME CAPABILITY PROBING (E29/T3)
----------------------------------------
Creating or updating a Databricks tenant runs ``DatabricksWorkspaceService.probe_capabilities``
and persists ``capabilities`` plus the COMPUTED ``binding_mode`` (``federation`` iff
``account_admin AND user_sync``, else ``invoke_unavailable``). Two rules, both deliberate:

* **Fail closed.** Any probe failure ⇒ all flags False ⇒ ``invoke_unavailable``. A capability
  flag is a promise to the UI, and the honest answer to "the probe blew up" is False. The connect
  flow NEVER yields ``sp_secret`` (E29/T14a, design §3B): a silent downgrade to the shared
  per-agent service-principal path would change what the audit log can prove, so a tenant that
  cannot federate is badged ``invoke_unavailable`` and refused at provision/invoke time instead.
  A legacy stored ``sp_secret`` is normalized to ``invoke_unavailable`` on its next probe.
* **Badge, do not block.** A failed ``can_discover`` STILL creates the tenant. The record is an
  operator's intent; the capabilities are what AGP could verify. Refusing the first because the
  second failed conflates them and leaves the operator retrying blindly instead of reading a
  badge that says which grant is missing.

The probe is async and these methods are sync, so it is driven with the
``inspect.isawaitable`` → ``asyncio.run`` guard ``project_service._provision_identity`` uses.
That requires NO running loop, which is why the tenant create/update ROUTES are sync handlers
(FastAPI dispatches them to a threadpool) — exactly the shape ``projects.add_repo`` has.

Determinism: the clock (``now``) and id source (``new_id``) are injectable; tests pass a
fixed clock + a deterministic id iterator. No ``datetime.now()`` sprinkled inline.

``TenantError`` carries a SAFE ``.message`` + a ``.kind`` hint
(``{"not_found","name_taken","validation","secret_error"}``) the route maps to a fixed HTTP
status + fixed detail literal (never ``str(exc)``).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

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
    TenantCreate,
    TenantPlatform,
    TenantStageConfig,
    TenantUpdate,
    hydrate_tenant_item,
)

logger = logging.getLogger(__name__)

_PARTITION_KEY = "tenant"  # single partition (single-partition list via query(pk))

# A tenant id is "ten-<8 hex>" (the seed tenant uses the fixed id "default").
_TENANT_ID_PREFIX = "ten-"

# An IAM role ARN, any partition, IAM path allowed. E36/T11: this is a DELIBERATE COPY of
# ``tenant_credentials._ROLE_ARN_RE`` (minus its capture group) rather than an import — that
# module is the teardown seam's private parser and this is a write-side rule; the two must
# accept the SAME set and the coupling is pinned by
# ``test_accepted_deploy_role_arns_are_exactly_what_the_assume_seam_can_parse``. Kept here
# and not in ``models/tenant.py`` because it is a service-layer write rule, exactly like the
# ``ACCOUNT_ID_RE`` check it sits beside (the model leaves the field free-form).
#
# EVERY PIECE OF THIS PATTERN IS LOAD-BEARING (E36/T11 fix round 1, security review Q-I2).
# The first cut wrote the PATH as ``(?:.*/)?``, which is any-char and unbounded, so
# ``role/"; rm -rf / ; echo "/x``, ``role/a"}]}/x``, ``role/${…}/x`` and a 300-char path all
# passed. That is not cosmetic: this value is stored, handed to CodeBuild as the PLAINTEXT
# ``TARGET_ROLE_ARN`` (``runtime_build_service``), and written by
# ``modules/codebuild/buildspec.yml`` into a DOUBLE-QUOTED HCL string inside an unquoted
# heredoc — so a ``"`` in the path closes the HCL string and a ``${…}`` is a template
# expression Terraform evaluates. Hence: path segments bounded to the SAME IAM charset as
# the name, ``[0-9]{12}`` rather than ``\d{12}`` (``\d`` also matches Arabic-Indic digits),
# and ``\Z`` rather than ``$`` (``$`` tolerates a trailing newline). The whole pre-existing
# accept-set — including IAM paths like ``role/service-role/foo`` — still passes.
_ROLE_ARN_RE = re.compile(
    r"^arn:aws[\w-]*:iam::[0-9]{12}:role/(?:[\w+=,.@-]{1,64}/)*[\w+=,.@-]{1,64}\Z"
)

# Secrets Manager name prefix for a Databricks tenant's credentials. A module default rather
# than a ``Settings`` field because ``core/config.py`` is outside this task's file manifest;
# every caller may override it, and the deploy should set it from the resource name prefix the
# way ``CONNECTIONS_SECRET_PREFIX`` is set.
_DEFAULT_SECRET_PREFIX = "agp-dev/databricks-tenants/"

# The account-admin secret's suffix. A per-stage secret is "<prefix><tenant>/<stage>", so this
# name must not collide with a stage name — hence the reserved-looking suffix.
_ACCOUNT_ADMIN_SUFFIX = "account-admin"

# Both boto3 error families, the ``connection_service._STORE_FAULTS`` idiom: a Secrets Manager
# fault arrives as ``ClientError`` for a service error and ``BotoCoreError`` for a transport
# one, and catching only the first leaves an endpoint failure escaping as a raw 500.
_STORE_FAULTS = (ClientError, BotoCoreError)

# The capability keys the probe answers with (contract C-2). Named here so a fail-closed
# default and a real probe result always have the SAME key set — a UI reading a missing key as
# "unknown" and a False key as "denied" would render two different things for one meaning.
_CAPABILITY_KEYS = ("can_discover", "account_admin", "user_sync")

# THE BINDING-MODE VOCABULARY — declared HERE and imported everywhere else (E29/T14a). The
# probe is the only writer of the field, so the words belong beside it; the identity service and
# the invoke route import these names rather than restating the literals, which is what stops
# the vocabulary drifting between the three places that branch on it.
BINDING_FEDERATION = "federation"
# DORMANT (design §3B). Nothing in the connect flow ever ASSIGNS this: the auto-degrade was
# removed, because a tenant silently moved onto a shared service-principal identity gets an
# audit trail that dies at AGP's boundary while its page still promises per-caller attribution.
# The word survives for records that deliberately carry it, consumable only behind
# ``settings.DATABRICKS_ALLOW_SP_SECRET_BINDING``.
BINDING_SP_SECRET = "sp_secret"
# The honest answer for a Databricks tenant that cannot federate: AGP will not invoke agents on
# it, and the tenant page says why (missing account-admin credential and/or user sync). "Not
# invocable, here is the gap" is actionable; a silent downgrade is not.
BINDING_INVOKE_UNAVAILABLE = "invoke_unavailable"


class TenantError(Exception):
    """A tenant operation failed. Carries a SAFE message (never internal store detail) and a
    ``.kind`` hint the route maps to a fixed HTTP status + fixed detail literal:
    ``{"not_found","name_taken","validation","secret_error"}``.

    ``secret_error`` is E29/T3's addition (``connection_service``'s kind of the same name): a
    Secrets Manager write that fails must not read as a validation problem, because the
    operator's input was fine and retrying it unchanged is the right next move."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


class TenantService:
    def __init__(
        self,
        table_name: str = "",
        region: str = "us-east-1",
        *,
        secret_prefix: str = _DEFAULT_SECRET_PREFIX,
        workspace=None,
        secrets_client=None,
        new_id=lambda: f"{_TENANT_ID_PREFIX}{uuid4().hex[:8]}",
        now=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.table_name = table_name
        self.region = region
        self.secret_prefix = secret_prefix
        self._new_id = new_id
        self._now = now

        # The Databricks client (contract C-2) — used ONLY for ``probe_capabilities`` here.
        # Injected in tests; lazily built in production so an AWS-only deployment never
        # constructs it. Kept as the whole service rather than a bound method so T6 can reuse
        # the same seam without this constructor growing a second parameter.
        self._workspace = workspace
        self._sm = secrets_client

        self._ddb = None
        self._table = None
        if table_name:
            try:
                self._ddb = boto3.resource("dynamodb", region_name=region)
                self._table = self._ddb.Table(table_name)
            except Exception:  # pragma: no cover — degrade to local fallback.
                self._table = None

        # Local fallback cache (used when no DDB table is configured).
        self._local: Dict[str, Tenant] = {}
        self._local_lock = threading.Lock()

    # -- mode helper --------------------------------------------------------

    @property
    def _has_ddb(self) -> bool:
        return bool(self.table_name) and self._table is not None

    # ===================================================================== #
    # Public API
    # ===================================================================== #

    def list(self) -> List[Tenant]:
        return self._load_all()

    def get(self, tenant_id: str) -> Tenant:
        record = self._get(tenant_id)
        if record is None:
            raise TenantError("Unknown tenant", kind="not_found")
        return record

    def create(self, data: TenantCreate, created_by: str) -> Tenant:
        """Validate (>=1 group, >=1 stage, per-platform stage rules, unique name) then persist.

        ``platform`` is settable ONLY here — it is immutable afterwards.

        E29/T3 — for a DATABRICKS tenant this additionally, in this order:

        1. mints the supplied credentials into Secrets Manager (nothing is stored until every
           input has passed validation, so a rejected body leaves no orphan secret);
        2. runs the connect-time capability probe with those credentials;
        3. persists ``capabilities`` + the computed ``binding_mode``.

        The capability fields remain NON-client-input throughout: the probe is their only
        writer, and a ``TenantCreate`` carries no way to assert them."""
        # Validation FIRST, before any side effect — the ``connection_service`` ordering: a
        # rejected body must store nothing at all, secret included.
        self._validate_stages(data.entra_group_ids, data.stages, platform=data.platform)
        self._require_name_available(data.name)

        tenant_id = self._new_id()
        is_dbx = data.platform == TenantPlatform.DATABRICKS

        # Mint the credentials, capturing the plain values for the probe. ``stages`` comes back
        # with the ARNs filled in and the write-only secrets consumed.
        stages, admin_arn, admin_creds = (
            self._mint_credentials(tenant_id, data)
            if is_dbx
            else (dict(data.stages), "", (None, None))
        )

        capabilities, binding_mode = (
            self._probe(stages, admin_creds) if is_dbx else ({}, "")
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
            self._save(record)
        except Exception:
            # A secret written but a record that never landed is an orphan nobody will ever
            # find — the ``connection_service`` rollback, same best-effort shape.
            logger.exception(
                "[tenants] persist failed for %s; rolling back secrets", tenant_id
            )
            self._delete_secrets_best_effort(tenant_id, stages.keys())
            raise TenantError(
                "Failed to persist tenant record", kind="secret_error"
            ) from None
        return record

    def update(self, tenant_id: str, data: TenantUpdate) -> Tenant:
        """Partial-merge the provided fields onto an existing tenant + bump ``updated_at``.

        Only fields explicitly set on ``data`` are applied; the rest are preserved. Any
        changed account/group value is re-validated, and a changed name must stay unique."""
        record = self.get(tenant_id)
        # ``exclude_unset`` drops fields never provided; also drop any explicitly-provided
        # ``None``. Every ``TenantUpdate`` field is non-nullable in ``Tenant``, so an explicit
        # null means "no change" — NOT "set to None". This matters because ``model_copy`` does
        # not re-validate: without this filter a body like ``{"name": null}`` would produce a
        # ``Tenant`` with ``name=None`` (required-str violated), corrupting the record and — in
        # DDB mode — poisoning the partition (``_from_item``'s validate would then raise on
        # every ``list``/``get``).
        changes = {
            k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None
        }
        # ``model_dump`` flattens ``stages`` into raw dicts, but ``model_copy`` does NOT
        # re-validate — writing those raw dicts would store un-typed stage entries. Restore
        # the typed ``TenantStageConfig`` objects from the input model so the stored record
        # keeps the nested shape.
        #
        # E36/T1: the restore is also a MERGE, not a wholesale assign. A stage the body
        # never names SURVIVES; a stage named in both is replaced WHOLE (per-stage
        # granularity, not per-field). Wholesale assign silently dropped every unnamed
        # stage — no warning, no diff — and every reader of the dropped stage then faulted.
        # Consequence of the merge: a stage cannot be DELETED through PUT.
        if "stages" in changes:
            changes["stages"] = {**record.stages, **data.stages}

        merged_groups = changes.get("entra_group_ids", record.entra_group_ids)
        merged_stages = changes.get("stages", record.stages)
        # Validated against the STORED platform, never a body-supplied one: ``TenantUpdate``
        # carries no ``platform`` field, so a stages update whose shape belongs to the other
        # platform fails here. That is what platform immutability means operationally — a
        # tenant can never end up with ``platform == "aws"`` governing Databricks config.
        # Passed EXPLICITLY (never relying on a default) so a caller cannot silently validate a
        # Databricks tenant against the AWS rules by omitting an argument.
        self._validate_stages(merged_groups, merged_stages, platform=record.platform)

        new_name = changes.get("name")
        if new_name is not None and new_name != record.name:
            self._require_name_available(new_name)

        # The account-admin credential halves are WRITE-ONLY: they are consumed here and must
        # never reach ``model_copy``, which would put them on the read model.
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
            # A stored admin secret that exists but yielded no credential was UNREADABLE — the
            # account-level flags must then carry over rather than read False (see ``_probe``).
            capabilities, binding_mode = self._probe(
                merged_stages,
                admin_creds,
                previous=record,
                admin_unresolved=bool(admin_arn) and not all(admin_creds),
            )
            changes["capabilities"] = capabilities
            changes["binding_mode"] = binding_mode

        updated = record.model_copy(update={**changes, "updated_at": self._now().isoformat()})
        self._save(updated)
        # PRUNE LAST (FIX round 3). The write has committed, so the stored record no longer names
        # these secrets and removing them cannot strand a live credential. Deleting before the save
        # meant a failed PutItem left the record pointing at an already-destroyed secret — with no
        # recovery window and nothing to surface it, since an uncredentialed stage is silently
        # excluded from probing. ``_prune_secrets`` never raises, so a prune failure here cannot
        # undo a good write; the worst case is a recoverable secret lingering until its window ends.
        if orphaned:
            self._prune_secrets(orphaned, record.id)
        return updated

    def delete(self, tenant_id: str) -> None:
        """Remove a tenant AND its credentials. NO reference check here — the route does it (Task 2).

        The secret cleanup (E29/T3, FIX round 1) is the delete-path counterpart of ``create``'s
        rollback: without it a deleted tenant left a LIVE workspace credential in Secrets Manager
        with no record pointing at it, so nothing would ever find it again. An orphaned credential
        for a workspace AGP no longer governs is the worst kind of leftover — it outlives the
        governance that justified it.

        Deletion is BY STORED ARN (:meth:`_delete_secrets_by_arn`), never by a name rebuilt from
        ``secret_prefix`` — rebuilding leaked a removed stage's secret and orphaned everything on
        a prefix change (FIX round 2; the reasoning is on that method). Reading the ARNs is also
        why the record must be captured BEFORE the row is dropped.

        Order matters: the record goes FIRST. A failed secret delete must not leave a tenant whose
        credentials are gone but whose record still claims they exist (every later discovery call
        would fail confusingly); the reverse — a removed record and a stray secret — is a cleanup
        item, and the cleanup never raises anyway."""
        record = self.get(tenant_id)
        self._delete(record.id)
        self._delete_secrets_by_arn(record)

    def upsert_seed(self, tenant: Tenant) -> Tenant:
        """Idempotent write by a fixed id (Task 9's seed uses this). Writing the same id
        twice yields exactly one record — no name-uniqueness check (a re-seed re-writes
        the same tenant)."""
        self._save(tenant)
        return tenant

    # ===================================================================== #
    # E29/T3 — Databricks credentials (Secrets Manager) + capability probing
    # ===================================================================== #

    def _mint_credentials(
        self, tenant_id: str, data: TenantCreate
    ) -> Tuple[Dict[str, StageConfig], str, Tuple[Optional[str], Optional[str]]]:
        """CREATE path: put every supplied credential in Secrets Manager, return the stage map
        with ARNs filled in, the account-admin ARN, and the plain values for the probe.

        Returned rather than stashed on ``self`` because the probe needs the plain values ONCE
        and nothing afterwards does: a service field holding a customer's client secret for the
        lifetime of a singleton is a credential with no expiry and no owner."""
        stages: Dict[str, StageConfig] = {}
        for key, stage in data.stages.items():
            # Nothing supplied ⇒ nothing stored, and the ARN slot stays empty. An empty secret
            # would read as "configured" to every later caller and then fail at mint time.
            if isinstance(stage, DatabricksStageConfig) and stage.sp_client_secret:
                arn = self._put_secret(
                    self._stage_secret_name(tenant_id, key),
                    {SP_SECRET_KEY: stage.sp_client_secret},
                    tenant_id,
                )
                # ``sp_client_secret=""`` clears the write-only value from the record we return,
                # so the credential does not survive in memory either.
                stages[key] = stage.model_copy(
                    update={"sp_client_secret_arn": arn, "sp_client_secret": ""}
                )
            else:
                stages[key] = stage

        admin_arn, admin_creds = self._mint_account_admin(
            tenant_id, data.account_admin_client_id, data.account_admin_secret
        )
        return stages, admin_arn, admin_creds

    def _merge_databricks_credentials(
        self, record: Tenant, data: TenantUpdate, merged_stages: Dict[str, StageConfig]
    ) -> Tuple[
        Dict[str, StageConfig], str, Tuple[Optional[str], Optional[str]], List[str]
    ]:
        """UPDATE path: OB-10. Preserve stored ARNs, ignore body-supplied ones, rotate on a
        newly-supplied secret, and resolve credentials for the re-probe.

        Returns ``(stages, account_admin_arn, account_admin_creds, orphaned_secret_arns)``. The
        last element is ADVISORY — a list of secrets the caller should delete once the record has
        been written successfully. This method performs no deletion itself (FIX round 3): it runs
        before the save, and deleting a credential the stored record still names is not
        recoverable.

        THE ATTACK THIS BLOCKS: a body-supplied ``sp_client_secret_arn`` would let a client aim
        its own tenant at another tenant's secret and have AGP read it on every discovery call.
        So the incoming value is DISCARDED unconditionally and the stored one restored — the
        same "stored value wins" rule that makes ``platform`` immutable. The ARN is not simply
        absent from the update model (as ``account_admin_secret_arn`` is) because it lives
        inside the ``stages`` shape, which is one model for reads and writes both."""
        stages: Dict[str, StageConfig] = {}
        for key, stage in merged_stages.items():
            if not isinstance(stage, DatabricksStageConfig):
                stages[key] = stage
                continue
            stored = record.stages.get(key)
            stored_arn = (
                stored.sp_client_secret_arn
                if isinstance(stored, DatabricksStageConfig)
                else ""
            )
            if stage.sp_client_secret:
                # A new secret was typed → mint/rotate and take the SERVER's ARN.
                arn = self._put_secret(
                    self._stage_secret_name(record.id, key),
                    {SP_SECRET_KEY: stage.sp_client_secret},
                    record.id,
                )
            else:
                arn = stored_arn
            stages[key] = stage.model_copy(
                update={"sp_client_secret_arn": arn, "sp_client_secret": ""}
            )

        # IDENTIFY (but do NOT yet delete) the secrets of stages this update removed or renamed.
        #
        # Computed here because it needs the PRE-merge record — once a stage leaves ``stages`` its
        # ``sp_client_secret_arn`` leaves the record with it, so this is the last point the ARN is
        # knowable at all (by delete time it is unrecoverable — verified by execution).
        #
        # **EXECUTED ONLY AFTER A SUCCESSFUL SAVE** (FIX round 3). Round 2 deleted here, before the
        # write: a failed PutItem then left the STORED record still naming a secret that had already
        # been force-deleted — irreversible, and silent, because an uncredentialed stage is excluded
        # from probing rather than failed closed. The caller executes the prune last; see ``update``.
        #
        # Compared by ARN, not by stage key, which makes both edits correct: a RENAME (uat →
        # staging) drops the old ARN and is pruned, while a ROTATION keeps the same secret NAME
        # and therefore the same ARN, so it is not mistaken for a removal and deleted out from
        # under the stage that still uses it.
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

        # Account-admin: unset ⇒ keep the stored ARN untouched; a supplied pair ⇒ rotate.
        supplied = data.model_dump(exclude_unset=True)
        if ACCOUNT_ADMIN_ID_KEY in supplied or ACCOUNT_ADMIN_SECRET_KEY in supplied:
            admin_arn, admin_creds = self._mint_account_admin(
                record.id, data.account_admin_client_id, data.account_admin_secret
            )
            if not admin_arn:
                # A half-filled pair is not a deletion — leave what is stored alone, and fall
                # back to the STORED credential for the re-probe (below).
                admin_arn = record.account_admin_secret_arn
                admin_creds = self._resolve_account_admin_creds(record)
        else:
            # NO new credential was sent — so READ BACK the stored one (FIX round 1).
            #
            # THE BUG THIS FIXES: this branch used to return ``(None, None)``, so the re-probe ran
            # WITHOUT the account credential, ``account_admin``/``user_sync`` came back False, and
            # ``binding_mode`` fell from ``federation`` to ``sp_secret``. A bare description edit
            # silently DEMOTED a federation tenant.
            #
            # Why that is more than a wrong badge: ``binding_mode`` is copied onto every agent at
            # register (C-4), and ``sp_secret`` attributes calls to a service principal instead of
            # the caller. So an unrelated metadata edit would quietly change who a Databricks
            # audit log blames for an invoke — and the epic's design forbids a silent downgrade to
            # sp_secret by name (T6 refuses it loudly; this did it invisibly, from the tenant side).
            admin_arn = record.account_admin_secret_arn
            admin_creds = self._resolve_account_admin_creds(record)
        return stages, admin_arn, admin_creds, orphaned

    def _resolve_account_admin_creds(
        self, record: Tenant
    ) -> Tuple[Optional[str], Optional[str]]:
        """Read the stored account-admin pair back out of Secrets Manager.

        Both halves live in ONE secret body (keys :data:`ACCOUNT_ADMIN_ID_KEY` /
        :data:`ACCOUNT_ADMIN_SECRET_KEY`), which is why the client id is not a record field — so
        this is the only way to recover the pair for a re-probe.

        Returns ``(None, None)`` when there is nothing to read OR the read fails, mirroring
        :meth:`_resolve_stage_secret`. An unreadable secret must NOT raise: a Secrets Manager
        blip should not turn a description edit into a 500. It must also not downgrade the
        tenant — :meth:`_probe` treats a probe it could not credential as "no evidence" and keeps
        the stored capabilities (see the ``previous`` argument there)."""
        if not record.account_admin_secret_arn:
            return None, None
        try:
            resp = self._secrets().get_secret_value(SecretId=record.account_admin_secret_arn)
            body = json.loads(resp["SecretString"])
        except Exception:  # noqa: BLE001 — see the docstring; never raises, never logs a value.
            logger.exception("[tenants] could not read a stored account-admin credential")
            return None, None
        client_id = body.get(ACCOUNT_ADMIN_ID_KEY) or None
        secret = body.get(ACCOUNT_ADMIN_SECRET_KEY) or None
        # All-or-nothing on the way out too: half a pair cannot mint a token.
        return (client_id, secret) if (client_id and secret) else (None, None)

    def _mint_account_admin(
        self, tenant_id: str, client_id: Optional[str], secret: Optional[str]
    ) -> Tuple[str, Tuple[Optional[str], Optional[str]]]:
        """Store the OPTIONAL account-admin pair, ALL-OR-NOTHING.

        Both halves or neither: an id with no secret cannot mint a token, so storing half of it
        would create a credential that can only fail while making the tenant look as though it
        had one. Both halves live in ONE secret body — they are useless apart, and one secret is
        one thing to rotate and one thing to delete."""
        if not (client_id and secret):
            return "", (None, None)
        arn = self._put_secret(
            self._account_admin_secret_name(tenant_id),
            {ACCOUNT_ADMIN_ID_KEY: client_id, ACCOUNT_ADMIN_SECRET_KEY: secret},
            tenant_id,
        )
        return arn, (client_id, secret)

    def _probe(
        self,
        stages: Dict[str, StageConfig],
        admin_creds: Tuple[Optional[str], Optional[str]],
        *,
        previous: Optional[Tenant] = None,
        admin_unresolved: bool = False,
    ) -> Tuple[Dict[str, bool], str]:
        """Probe every Databricks stage and reduce to one capability set + a binding mode.

        **THE ANSWER IS ``federation`` OR ``invoke_unavailable`` — NEVER ``sp_secret``**
        (E29/T14a, design §3B). This is the ONLY writer of ``binding_mode``, and it no longer has
        a degrade path: ``federation`` iff ``account_admin and user_sync``, otherwise
        ``invoke_unavailable``, and that holds on every return including the not-probeable one.
        A tenant is never silently moved onto a shared service-principal identity, because that
        changes who the customer's Databricks audit log blames for an invoke while the tenant page
        goes on promising per-caller attribution. ``invoke_unavailable`` is the honest, actionable
        answer: AGP will not provision or invoke there until the account-admin credential and the
        Entra→Databricks user sync exist. A STORED ``sp_secret`` (a pre-T14a record) is normalized
        to ``invoke_unavailable`` on its next probe rather than carried over — see the
        ``previous`` branch below; ``sp_secret`` remains a dormant word only for records that
        deliberately carry it, consumable behind ``DATABRICKS_ALLOW_SP_SECRET_BINDING``.

        **HOW N STAGES BECOME ONE ANSWER.** ``capabilities`` is a tenant-level field but a
        credential is per-stage, so the reduction has to pick a direction per flag and the two
        directions are different on purpose:

        * ``can_discover`` is ``all()``. It is a claim about the tenant, and a tenant with one
          unreachable workspace is not fully discoverable — badging it True would hide the
          broken stage behind the working one.
        * ``account_admin`` / ``user_sync`` are ``any()``. They are ACCOUNT-level facts, so one
          stage proving them proves them; only stages carrying a Databricks ``account_id`` can
          probe them at all.

        **FAIL CLOSED, AND NEVER RAISE.** Every probe is wrapped: an exception, a missing
        credential or an unreadable secret all yield False. A create then proceeds — badged, not
        blocked (see the class docstring).

        ``previous`` is the UPDATE path's guard: when NO stage could resolve a credential there
        was nothing to probe with, and the stored capabilities are LEFT ALONE. "AGP could not
        look" is not evidence that a capability was lost, and zeroing it would be a fabricated
        downgrade an operator would read as a real regression (``read_repo``'s rule for a read).

        ``admin_unresolved`` is the same rule applied to ONE credential rather than all of them:
        the tenant HAS a stored account-admin secret but it could not be read, so the two
        account-level flags carry over from ``previous`` instead of being reported False. Without
        this, an unreadable admin secret during an unrelated edit demotes a federation tenant.
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
                # NORMALIZE A LEGACY ``sp_secret`` (E29/T14a). The capabilities carry over
                # untouched — absent evidence is not negative evidence — but the mode may not:
                # the contract is that NOTHING assigns ``sp_secret``, and re-emitting a stored
                # one would make this path the last assigner of it. A pre-T14a record therefore
                # re-maps to ``invoke_unavailable`` on its next probe. ``federation`` and the AWS
                # ``""`` are preserved exactly as before.
                carried = previous.binding_mode
                if carried == BINDING_SP_SECRET:
                    carried = BINDING_INVOKE_UNAVAILABLE
                return dict(previous.capabilities), carried
            return {k: False for k in _CAPABILITY_KEYS}, BINDING_INVOKE_UNAVAILABLE

        results: List[Dict[str, bool]] = []
        for stage, secret in probeable:
            # Account-admin credentials are only meaningful with an account id to aim them at.
            admin_id, admin_secret = admin_creds if stage.account_id else (None, None)
            results.append(
                self._probe_one(stage, secret, admin_id, admin_secret)
            )

        capabilities = {
            "can_discover": all(r.get("can_discover") for r in results),
            "account_admin": any(r.get("account_admin") for r in results),
            "user_sync": any(r.get("user_sync") for r in results),
        }
        if admin_unresolved and previous is not None:
            # A stored account-admin credential EXISTS but could not be read, so the
            # account-level probe never ran and its two flags are absent-evidence, not
            # negative-evidence. Carry the previous values rather than reporting False:
            # otherwise a Secrets Manager blip during an unrelated edit demotes a federation
            # tenant, which is the same fabricated downgrade the ``probeable`` guard above
            # refuses. ``can_discover`` still takes the FRESH answer — that probe did run.
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
        """One stage's probe, fail-closed. Never raises, never logs a credential.

        The probe is async and this service is sync, so the coroutine runs on a FRESH loop via
        the ``project_service._provision_identity`` guard (``inspect.isawaitable`` →
        ``asyncio.run``). That requires no running loop, which is exactly why the tenant
        create/update routes are sync handlers — an ``asyncio.run`` under a live uvicorn loop
        would raise, and the failure would be a 500 on a working credential."""
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
                result = asyncio.run(result)
        except Exception:  # noqa: BLE001 — fail closed; a probe must never break a write.
            # The exception can carry a workspace path or an upstream body: traceback only, and
            # a fixed message. Nothing from it reaches the record (asserted by test).
            logger.exception("[tenants] capability probe failed; failing closed")
            return {k: False for k in _CAPABILITY_KEYS}
        if not isinstance(result, dict):  # pragma: no cover — a drifted C-2 return shape.
            return {k: False for k in _CAPABILITY_KEYS}
        return {k: bool(result.get(k)) for k in _CAPABILITY_KEYS}

    def _resolve_stage_secret(self, stage: DatabricksStageConfig) -> str:
        """The stage's SP secret: the one just typed, else the stored one read back.

        Reading it back is what lets a metadata-only edit still re-probe with a real credential.
        An unreadable secret returns "" (the caller then fails that flag closed) rather than
        raising: a probe is a best-effort observation, and a Secrets Manager blip must not turn
        a description edit into a 500."""
        if stage.sp_client_secret:
            return stage.sp_client_secret
        if not stage.sp_client_secret_arn:
            return ""
        try:
            resp = self._secrets().get_secret_value(SecretId=stage.sp_client_secret_arn)
            return json.loads(resp["SecretString"]).get(SP_SECRET_KEY, "")
        except Exception:  # noqa: BLE001 — see the docstring.
            logger.exception("[tenants] could not read a stored Databricks stage secret")
            return ""

    def _workspace_service(self):
        """Lazily build the C-2 client. Imported HERE, not at module import, so an AWS-only
        deployment never pays for the Databricks module and the import graph stays acyclic."""
        if self._workspace is None:  # pragma: no cover — tests always inject.
            from services.databricks_workspace_service import DatabricksWorkspaceService

            self._workspace = DatabricksWorkspaceService()
        return self._workspace

    # ===================================================================== #
    # Secrets Manager (mirror connection_service._create_secret)
    # ===================================================================== #

    def _stage_secret_name(self, tenant_id: str, stage: str) -> str:
        return f"{self.secret_prefix}{tenant_id}/{stage}"

    def _account_admin_secret_name(self, tenant_id: str) -> str:
        return f"{self.secret_prefix}{tenant_id}/{_ACCOUNT_ADMIN_SUFFIX}"

    def _secrets(self):
        if self._sm is None:  # pragma: no cover — tests always inject.
            self._sm = boto3.client("secretsmanager", region_name=self.region)
        return self._sm

    def _restore_if_scheduled(self, name: str, tenant_id: str) -> None:
        """Un-mark a secret that is SCHEDULED for deletion, so it can be written to again.

        The inverse of :meth:`_prune_secrets`, and the operation the recovery window implies exists
        (E29/T3, FIX round 4). Called only from the ResourceExistsException branch — a secret that
        did not already exist cannot be marked.

        CONDITIONAL, on purpose: an unconditional ``restore_secret`` would be a pointless write on
        every ordinary rotation, and it would quietly un-schedule a secret some other actor had
        deliberately scheduled. ``DeletedDate`` present is the only trigger.

        A restore FAILURE RAISES rather than falling through to the write. Writing into a still-
        marked secret is precisely the "success with a permanently unreadable credential" outcome
        this fix removes, so the honest answer is ``secret_error`` — the operator retries and
        nothing is left half-done. ``describe_secret`` failing is treated the same way: AGP cannot
        confirm the secret is writable, and proceeding on that uncertainty is what caused the bug."""
        sm = self._secrets()
        try:
            described = sm.describe_secret(SecretId=name)
        except sm.exceptions.ResourceNotFoundException:
            return  # raced away between create and describe — the caller's write will fault legibly
        except _STORE_FAULTS:
            logger.exception("[tenants] describe_secret failed for tenant %s", tenant_id)
            raise TenantError(
                "Failed to store the tenant credential", kind="secret_error"
            ) from None

        if not described.get("DeletedDate"):
            return  # live secret — an ordinary rotation, nothing to undo

        try:
            sm.restore_secret(SecretId=name)
        except _STORE_FAULTS:
            logger.exception("[tenants] restore_secret failed for tenant %s", tenant_id)
            raise TenantError(
                "Failed to store the tenant credential", kind="secret_error"
            ) from None
        logger.info(
            "[tenants] restored a scheduled-for-deletion secret for tenant %s "
            "(a removed stage was re-added within the recovery window)",
            tenant_id,
        )

    def _put_secret(self, name: str, body: dict, tenant_id: str) -> str:
        """Create-or-overwrite one secret and return its ARN — ``connection_service``'s exact
        shape, including that an existing name is a ROTATION (``put_secret_value``) rather than
        a conflict: re-connecting a tenant with a fresh credential is the ordinary case.

        The secret VALUE and the exception's own text are never logged — traceback only."""
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
            # PRECEDENT DIVERGENCE, deliberate (E29/T3, FIX round 1): the rotation is wrapped in
            # its OWN guard. ``connection_service._create_secret`` leaves the equivalent
            # ``put_secret_value`` outside the ``except _STORE_FAULTS`` clause — an except block
            # does not shelter under a later sibling — so a fault while ROTATING escapes as a raw
            # botocore error and becomes an unmapped 500 carrying an upstream message. That is
            # the path a RE-CONNECT takes (the secret name already exists), i.e. the common case,
            # not an edge one. Copying the precedent would have copied the bug.
            # RESURRECT A SCHEDULED SECRET BEFORE WRITING INTO IT (E29/T3, FIX round 4).
            #
            # Round 3's prune schedules a RECOVERABLE delete, which created a state force-delete
            # never could: a secret that still EXISTS while marked ``DeletedDate``. Drop a stage and
            # re-add it inside the recovery window and this branch walked straight into it —
            # ``put_secret_value`` SUCCEEDS against a marked secret, so the write "worked" while the
            # ARN stayed marked, every ``get_secret_value`` failed forever, ``_resolve_stage_secret``
            # swallowed that into "" (excluding the stage from probing rather than failing closed),
            # and ``update`` reported success. A write that reports success and leaves an unreadable
            # credential is worse than one that fails.
            #
            # So: describe first, restore only if actually marked, THEN rotate. ``describe_secret``
            # is one extra read on the re-connect path only — the create path never reaches here.
            self._restore_if_scheduled(name, tenant_id)
            try:
                resp = sm.put_secret_value(SecretId=name, SecretString=secret_string)
                return resp["ARN"]
            except _STORE_FAULTS:
                logger.exception(
                    "[tenants] put_secret_value failed for tenant %s", tenant_id
                )
                raise TenantError(
                    "Failed to store the tenant credential", kind="secret_error"
                ) from None
        except _STORE_FAULTS:
            logger.exception("[tenants] create_secret failed for tenant %s", tenant_id)
            raise TenantError(
                "Failed to store the tenant credential", kind="secret_error"
            ) from None

    def _delete_secrets_best_effort(self, tenant_id: str, stage_keys) -> None:
        """Roll back every secret a FAILED CREATE may have written, addressed BY NAME.

        **The create-rollback path only.** It runs when the record does not exist (the persist is
        what failed), so there is no stored ARN to delete by and the name AGP just wrote with is
        the only identifier available. The delete path must NOT use this — see
        :meth:`_delete_secrets_by_arn` for why.

        Best-effort by design: a rollback that raises would replace a legible persist failure with
        an opaque one."""
        names = [self._stage_secret_name(tenant_id, k) for k in stage_keys]
        names.append(self._account_admin_secret_name(tenant_id))
        self._delete_each_best_effort(names, tenant_id)

    def _delete_secrets_by_arn(self, record: Tenant) -> None:
        """Delete a stored tenant's secrets by the ARNs THE RECORD CARRIES (E29/T3, FIX round 2).

        **NEVER rebuild a name from the prefix here.** Round 1's delete reused the name-based
        rollback helper above, which regenerates names from ``secret_prefix`` + the CURRENT
        stages, and that leaked two ways — both reproduced before this fix:

        * a stage REMOVED (or renamed) after creation is no longer in ``record.stages``, so no
          name is generated for its secret and the credential outlives the tenant entirely;
        * a CHANGED ``DATABRICKS_TENANT_SECRET_PREFIX`` makes every rebuilt name miss, orphaning
          BOTH secrets — a settings edit silently becoming a credential leak.

        A name is a guess about how a record was written; the ARN is what the record actually
        stores. They are not even interconvertible: Secrets Manager appends a random 6-character
        suffix to a secret's ARN, so the ARN cannot be derived from the name (verified by
        execution, not assumed). ``delete_secret`` accepts either — likewise verified.

        Silent when there is nothing to delete: an AWS tenant has no credentials and a Databricks
        tenant registered before its credential exists has empty ARN slots. Firing DeleteSecret
        anyway logged an ERROR on a perfectly normal delete, which is how a clean operation ends
        up looking broken in CloudWatch."""
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
        """Delete secrets whose TENANT SURVIVES — a RECOVERABLE (scheduled) delete.

        The distinction from :meth:`_delete_each_best_effort` is the record's fate, and it decides
        whether an operator can undo the deletion (FIX round 3):

        * Here the tenant is alive and merely stopped naming this secret — an operator who removed
          the wrong stage, or renamed one by mistake, is one ``restore_secret`` away from their
          credential. Secrets Manager's default window makes that possible.
        * ``ForceDeleteWithoutRecovery`` destroys it immediately with no undo. That is correct only
          where nothing will ever reference the secret again (tenant delete) or where the record
          never existed (create rollback) — there is nothing to recover it *for*.

        A lingering recoverable secret is a bounded, visible cost; an unrecoverable one is
        permanent data loss. When the two rules disagree, this is the direction to be wrong in."""
        self._delete_each_best_effort(secret_ids, tenant_id, force=False)

    def _delete_each_best_effort(
        self, secret_ids: List[str], tenant_id: str, *, force: bool = True
    ) -> None:
        """Delete each ``SecretId`` (a name or an ARN — the API takes both), never raising.

        ``force`` selects the recovery mode: ``True`` (the default, for paths where the record is
        gone or was never written) deletes irreversibly; ``False`` schedules a restorable delete —
        see :meth:`_prune_secrets` for which path gets which and why.

        Shared by every delete path so the failure semantics are identical: a missing secret is
        SUCCESS (already gone is the desired end state), and a store fault is logged and stepped
        over rather than abandoning the remaining ids — one unreachable secret must not strand the
        others."""
        sm = self._secrets()
        kwargs = {"ForceDeleteWithoutRecovery": True} if force else {}
        for secret_id in secret_ids:
            try:
                sm.delete_secret(SecretId=secret_id, **kwargs)
            except sm.exceptions.ResourceNotFoundException:
                pass  # never created (or already gone) — success either way
            except sm.exceptions.InvalidRequestException:
                # NON-FATAL but NOT diagnosed (FIX round 4). ``InvalidRequestException`` covers
                # more than "already scheduled" — an unavailable KMS key raises it too — so the log
                # line no longer ASSERTS a cause it has not checked. Claiming a schedule that never
                # happened is how a real store problem gets read as routine cleanup.
                #
                # Still swallowed rather than raised: this is best-effort deletion, and the callers
                # (a committed update, a completed delete, a rollback already handling a failure)
                # have nothing useful to do with it. WARNING rather than INFO so an operator can
                # actually find it. Diagnosing it here would cost a ``describe_secret`` per secret
                # on every prune to improve a log line, which is not worth the call.
                logger.warning(
                    "[tenants] delete_secret was refused for tenant %s "
                    "(secret may already be scheduled for deletion, or its key is unavailable)",
                    tenant_id,
                )
            except _STORE_FAULTS:
                logger.exception("[tenants] delete_secret failed for tenant %s", tenant_id)

    # ===================================================================== #
    # Validation
    # ===================================================================== #

    @classmethod
    def _validate_stages(
        cls,
        entra_group_ids: List[str],
        stages: Dict[str, StageConfig],
        *,
        platform: TenantPlatform,
    ) -> None:
        """Shape rules shared by create + update, branched on the tenant's ``platform``.

        The platform-neutral rules (>=1 group, >=1 stage) run first; then EVERY stage is
        held to its platform's rules — a stage config of the other platform's shape is a
        validation failure, which is what makes ``platform`` immutable in practice (E29)."""
        if not entra_group_ids:
            raise TenantError("A tenant requires at least one Entra group", kind="validation")
        # E28/T6: stages are an open axis — any stage name is valid (``uat``, a per-region
        # stage, …), so the only shape rule is "at least one". Stage NAMES are never
        # validated against a dev/prod literal; the per-stage rules below stand.
        if not stages:
            raise TenantError("A tenant requires at least one stage config", kind="validation")
        for key, stage in stages.items():
            if platform == TenantPlatform.DATABRICKS:
                cls._validate_databricks_stage(key, stage)
            else:
                cls._validate_aws_stage(key, stage)

    @staticmethod
    def _validate_aws_stage(key: str, stage: StageConfig) -> None:
        """The AWS stage rules (``account_id`` + the E36/T11 ``deploy_role_arn`` rule) — plus
        a shape guard so a Databricks-shaped stage can never slip past them by simply not
        carrying an ``account_id``."""
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
        # E36/T11 — the field stopped being inert metadata. ``tenant_credentials``
        # hands it straight to ``sts:AssumeRole`` (E36/T8) and the ECS task role now
        # HOLDS that assume on ``agp-deployment-*`` (modules/ecs), so an unvalidated
        # value is the confused-deputy half of a live grant: a tenant-admin write is
        # an address the control plane acts on. The role-NAME wildcard in the grant is
        # the authorization boundary; this is the input hygiene that pairs with it
        # (security review B-4). It is also the string ``_safe_role_label`` parses for
        # the operator-facing teardown message — a value it cannot parse degrades that
        # message to a generic label.
        #
        # EMPTY IS VALID and must stay so: "" means deploy-in-place (the platform
        # account), which is both the default and how an operator moves a stage back.
        if stage.deploy_role_arn and not _ROLE_ARN_RE.match(stage.deploy_role_arn):
            # NO VALUE IN THE MESSAGE: a deploy_role_arn contains the 12-digit account
            # id, and this string reaches the console and the logs, where a hard project
            # rule bans it. The stage name plus the expected format is the actionable
            # part anyway.
            raise TenantError(
                f"stage {key} deploy_role_arn must be an IAM role ARN "
                "(arn:aws:iam::<account-id>:role/<name>) or empty",
                kind="validation",
            )

    @staticmethod
    def _validate_databricks_stage(key: str, stage: StageConfig) -> None:
        """Databricks stage rules (E29/C-1). ``fullmatch`` throughout: with ``re.match``
        the trailing ``$`` would also accept a trailing newline, letting a smuggled second
        line ride along behind a valid-looking origin."""
        if not isinstance(stage, DatabricksStageConfig):
            raise TenantError(
                f"stage {key} must be a Databricks stage config on a databricks tenant",
                kind="validation",
            )
        # RESERVED STAGE NAME (E29/T3, FIX round 3). Stage secrets are named
        # "<prefix><tenant>/<stage>" and the account-admin secret is
        # "<prefix><tenant>/account-admin", so a stage literally keyed ``account-admin`` makes the
        # two writes land on ONE secret: whichever runs second overwrites the other's credential,
        # and the prune/delete paths would then remove a secret the surviving half still needs.
        # An earlier comment called the suffix "reserved-looking"; nothing enforced it, so this does.
        #
        # REJECTION rather than namespacing the stage secrets, deliberately: adding a segment (e.g.
        # ".../stages/<name>") would change the name of every secret ALREADY WRITTEN, orphaning
        # them at the next prune or delete — a data migration, to accommodate one absurd stage name.
        # Rejecting is one comparison and breaks nothing. Case-insensitive because a near-miss here
        # is never a legitimate stage name, only a collision waiting on a case-insensitive layer.
        #
        # DATABRICKS ONLY: an AWS tenant writes no secrets, so its stage axis stays fully open
        # (E28/D8) and T1's tests keep governing it unchanged.
        if key.strip().lower() == _ACCOUNT_ADMIN_SUFFIX:
            raise TenantError(
                f"stage {key} uses a reserved name", kind="validation"
            )
        if not WORKSPACE_URL_RE.fullmatch(stage.workspace_url or ""):
            raise TenantError(
                f"stage {key} workspace_url must be an https workspace origin "
                "with no port, path, or trailing slash",
                kind="validation",
            )
        if not stage.sp_client_id:
            raise TenantError(
                f"stage {key} requires a service principal client id", kind="validation"
            )
        # "0" is a legal workspace id (a URL with no o= parameter) — digits, not truthiness.
        if not WORKSPACE_ID_RE.fullmatch(stage.workspace_id or ""):
            raise TenantError(
                f"stage {key} workspace_id must be a digits-string", kind="validation"
            )
        # The Databricks ACCOUNT id (E29/T3, OB-11). EMPTY IS LEGAL — C-1 requires it for
        # federation mode ONLY, so an sp_secret tenant carries none and rejecting empty would
        # make the account-admin grant mandatory for every Databricks tenant.
        #
        # A NON-EMPTY value must be a UUID, because it is interpolated into
        # ``/oidc/accounts/{account_id}/v1/token`` — the account-admin token mint. A stored
        # ``a1/../../accounts/other/v1/token`` re-aims that mint at a different Databricks
        # account. ``databricks_workspace_service`` also quotes the value; this is the second,
        # independent guard, and it is the one that keeps such a value from ever being STORED.
        if stage.account_id and not DATABRICKS_ACCOUNT_ID_RE.fullmatch(stage.account_id):
            raise TenantError(
                f"stage {key} account_id must be a Databricks account UUID",
                kind="validation",
            )

    def _require_name_available(self, name: str) -> None:
        if any(t.name == name for t in self._load_all()):
            raise TenantError("A tenant with this name already exists", kind="name_taken")

    # ===================================================================== #
    # Persistence (DDB-or-local, mirror connection_service.py)
    # ===================================================================== #

    def _get(self, tenant_id: str) -> Tenant | None:
        if self._has_ddb:
            try:
                resp = self._table.get_item(Key={"pk": _PARTITION_KEY, "sk": tenant_id})
                item = resp.get("Item")
                return self._from_item(item) if item else None
            except ClientError:
                logger.exception("Failed to fetch tenant %s from DDB", tenant_id)
                return None
        with self._local_lock:
            record = self._local.get(tenant_id)
            return record.model_copy(deep=True) if record else None

    def _load_all(self) -> List[Tenant]:
        if self._has_ddb:
            try:
                items = self._scan_partition()
                return [self._from_item(i) for i in items]
            except ClientError:
                logger.exception("Failed to load tenants from DDB")
                return []
        with self._local_lock:
            return [t.model_copy(deep=True) for t in self._local.values()]

    def _save(self, record: Tenant) -> None:
        if self._has_ddb:
            self._table.put_item(Item=self._to_item(record))
            return
        with self._local_lock:
            self._local[record.id] = record.model_copy(deep=True)

    def _delete(self, tenant_id: str) -> None:
        if self._has_ddb:
            self._table.delete_item(Key={"pk": _PARTITION_KEY, "sk": tenant_id})
            return
        with self._local_lock:
            self._local.pop(tenant_id, None)

    def _scan_partition(self) -> List[dict]:
        items: List[dict] = []
        kwargs = {"KeyConditionExpression": Key("pk").eq(_PARTITION_KEY)}
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        return items

    def _to_item(self, record: Tenant) -> dict:
        return {
            "pk": _PARTITION_KEY,
            "sk": record.id,
            **json.loads(record.model_dump_json()),
        }

    def _from_item(self, item: dict) -> Tenant:
        clean = {k: v for k, v in item.items() if k not in ("pk", "sk")}
        clean = hydrate_tenant_item(clean)
        return Tenant.model_validate(clean)
