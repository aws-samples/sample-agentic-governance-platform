"""Pydantic models for Tenants — the multi-tenancy unit of ownership (E24/T1, reshaped E25/T1).

Plain ``BaseModel``s — the ``connection``/``project`` idiom. A ``Tenant`` is a
line-of-business unit that OWNS agents, MCP servers, and projects (they carry a
``tenant_id``) and maps to one or more Entra groups (the members who can see/act on
the tenant's resources) plus a per-stage AWS deployment config (dev/prod).

E25 reshape: the flat ``aws_account_dev``/``aws_account_prod``/``aws_region`` fields
became a nested ``stages: Dict[str, TenantStageConfig]`` map so each stage carries its
own account, region, ECR repo, and the push/deploy role ARNs the cross-account CICD
needs. Legacy flat records still validate on read via :func:`hydrate_tenant_item`,
which folds them into the nested shape (mirror of ``ProjectService._hydrate_project``).

E29 reshape: a tenant is now **platform-typed**. ``platform`` is an immutable
:class:`TenantPlatform` discriminator (``aws``/``databricks``) and ``stages`` became a
union of per-platform stage shapes — :class:`TenantStageConfig` (AWS, unchanged) and
:class:`DatabricksStageConfig`. Pre-E29 records carry no ``platform`` key and hydrate as
``"aws"`` via :func:`hydrate_tenant_item` (zero migration, same idiom as the E25 folding).
``TenantUpdate`` deliberately carries NO ``platform`` field — a live tenant can never be
re-typed; the service additionally refuses a stages update whose shape contradicts the
stored platform.

E29/T3 adds the WRITE-ONLY credential half (OB-7). ``DatabricksStageConfig.sp_client_secret``
and ``TenantCreate``/``TenantUpdate``'s ``account_admin_client_id``/``account_admin_secret``
are accepted on the way in and absent on the way out; the read model carries only the two
Secrets Manager pointers (``sp_client_secret_arn`` per stage, ``account_admin_secret_arn`` per
tenant). Before this the backend DROPPED all three — pydantic's ``extra="ignore"`` swallowed
keys the admin form had always sent, so credentials were collected and silently discarded.

The AWS account ids are 12-digit strings, validated on write via
:data:`ACCOUNT_ID_RE`; the Databricks workspace origin + id are validated via
:data:`WORKSPACE_URL_RE` / :data:`WORKSPACE_ID_RE`, and the Databricks *account* UUID via
:data:`DATABRICKS_ACCOUNT_ID_RE` (a security boundary, not a format check — see its comment).
Persistence + validation orchestration live in ``tenant_service`` — no boto3, no I/O here
(pure models).
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Dict, List, Optional, Union

from pydantic import BaseModel, Field

# A 12-digit AWS account id.
ACCOUNT_ID_RE = re.compile(r"^\d{12}$")

# A Databricks workspace origin: https, lowercase host, no port/path/query/fragment.
# Matched with ``fullmatch`` (never ``.match``) — ``$`` alone also matches just before a
# trailing newline, which would let "https://host\njavascript:…" through.
WORKSPACE_URL_RE = re.compile(r"^https://[a-z0-9][a-z0-9.-]+[a-z0-9]$", re.ASCII)

# A Databricks workspace id is a digits-string ("0" is a real value — see below).
# ``re.ASCII`` keeps ``\d`` to 0-9: without it ``\d`` also matches Unicode digits
# (e.g. Arabic-Indic "١٢٣"), which no Databricks API would accept.
WORKSPACE_ID_RE = re.compile(r"^\d+$", re.ASCII)

# A Databricks ACCOUNT id — a lowercase-hex UUID (E29/T3, OB-11).
#
# WHY THIS IS A SECURITY BOUNDARY AND NOT A TIDINESS CHECK. ``account_id`` is interpolated
# into ``/oidc/accounts/{account_id}/v1/token`` — the ACCOUNT-ADMIN token mint, the highest-
# privilege call in ``databricks_workspace_service``. A stored value shaped like
# ``a1/../../accounts/other/v1/token`` re-aims that mint at a DIFFERENT Databricks account.
# The client already ``quote``s the value, which stops the traversal at the URL layer; this
# regex stops such a value from ever being STORED, so the guarantee survives a future caller
# that forgets to quote. Two independent guards, deliberately.
#
# ``fullmatch`` at every call site (never ``.match``): ``$`` alone also matches just before a
# trailing newline, so ``.match`` would accept "<valid-uuid>\n../evil". ``re.ASCII`` for the
# same reason ``WORKSPACE_ID_RE`` has it. Lowercase only — Databricks emits lowercase, and a
# case-insensitive id would make two spellings of one account look like two accounts.
DATABRICKS_ACCOUNT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.ASCII
)

# The JSON keys inside a tenant's Secrets Manager entries (E29/T3). Declared HERE, next to
# the ARN fields that point at them, because the writer (``tenant_service``) and the readers
# (``runtime_catalog``, and T6's provisioning half) are different modules — a key spelled
# twice is a key that drifts, and the failure mode is a silent "no credential found".
SP_SECRET_KEY = "sp_client_secret"
ACCOUNT_ADMIN_ID_KEY = "account_admin_client_id"
ACCOUNT_ADMIN_SECRET_KEY = "account_admin_secret"


class TenantPlatform(str, Enum):
    """The runtime platform a tenant's agents live on — immutable after create.

    A ``str`` enum so it serializes to the wire value ("aws"/"databricks") and compares
    equal to it, matching the stored-record shape of pre-E29 items."""

    AWS = "aws"
    DATABRICKS = "databricks"


class TenantStageConfig(BaseModel):
    """Per-stage AWS deployment config (e.g. ``dev``/``prod``). ``account_id`` is a
    12-digit id (validated on write in the service); the ECR repo + role ARNs default
    empty and are populated as the cross-account CICD provisions them (E25)."""

    account_id: str  # 12-digit AWS account id
    region: str = "us-east-1"
    ecr_repo_uri: str = ""
    push_role_arn: str = ""
    deploy_role_arn: str = ""


class DatabricksStageConfig(BaseModel):
    """Per-stage Databricks workspace config (E29). The stage's *identity* is the
    workspace service principal: ``sp_client_id`` on the record, its secret ONLY in
    Secrets Manager (``sp_client_secret_arn``) — never in DynamoDB, never echoed.

    ``workspace_id`` is a digits-string, not an int: it is an opaque identifier and ``"0"``
    is a real value (a workspace URL that carries no ``o=`` parameter). ``account_id`` is
    the Databricks *account* UUID — required only for federation binding mode, and validated
    against :data:`DATABRICKS_ACCOUNT_ID_RE` on write because it aims a URL path.

    **``sp_client_secret`` IS WRITE-ONLY (E29/T3, OB-7)** — one field, two directions:

    * On the way IN it carries what the admin typed. It exists at all because the backend was
      otherwise DROPPING it: pydantic's default ``extra="ignore"`` swallowed the key the admin
      form has always sent, so the UI collected a credential, the API answered 201, and nothing
      was ever stored. A silently-discarded credential is worse than a rejected one.
    * On the way OUT it does not exist. ``exclude=True`` removes it from every
      ``model_dump``/``model_dump_json``, which is BOTH serializers that matter here — the
      FastAPI ``response_model`` and the DynamoDB ``_to_item`` persist. So the secret cannot
      reach a client and cannot reach the table; only ``sp_client_secret_arn`` does.

    This is ``connection.py``'s write/read split expressed on ONE model rather than two.
    ``Connection`` could split cleanly (``ConnectionCreate.token`` vs ``Connection.secret_arn``)
    because a connection's read and write shapes are separate models. A stage is not: the SAME
    ``DatabricksStageConfig`` is what a create body carries, what the record stores, and what
    the ``StageConfig`` union resolves on read, so a second class would have to be threaded
    through the union — and every ``isinstance(stage, DatabricksStageConfig)`` branch in the
    service and in T6 would then need to know which half it was holding. ``exclude=True``
    achieves the same asymmetry with no branch that can be got wrong."""

    workspace_url: str  # https origin, no trailing slash (validated on write)
    workspace_id: str = "0"  # digits-string; "0" is legal (no o= in the URL)
    cloud: str = "aws"  # "aws" | "azure" | "gcp"
    region: str = ""
    account_id: str = ""  # Databricks account UUID; REQUIRED for federation mode only
    sp_client_id: str = ""
    # WRITE-ONLY (see above): accepted on input, excluded from every dump. The service
    # consumes it into Secrets Manager and persists only the ARN below.
    sp_client_secret: str = Field(default="", exclude=True)
    sp_client_secret_arn: str = ""  # Secrets Manager ARN; the secret is NEVER in DDB


# A stage entry is one shape or the other, chosen by the tenant's ``platform``. Pydantic's
# smart union resolves a stored dict to the model its keys fit; the service is what refuses
# a shape that contradicts the stored platform (a dict carrying BOTH shapes' keys is
# otherwise ambiguous).
StageConfig = Union[TenantStageConfig, DatabricksStageConfig]


class Tenant(BaseModel):
    """Read-model — a line-of-business tenant. Carries only metadata, no credential
    (E29: a Databricks tenant's SP secret lives in Secrets Manager — only its ARN is here)."""

    id: str  # "ten-<8 hex>" (or "default" for the seed tenant)
    name: str
    line_of_business: str
    entra_group_ids: List[str]  # >= 1
    platform: TenantPlatform = TenantPlatform.AWS  # immutable after create
    stages: Dict[str, StageConfig]  # per-stage platform config (e.g. dev/prod)
    # Capability flags — written by the connect-time probe (E29/T3), read-only to clients:
    # keys "can_discover", "account_admin", "user_sync". Absent ⇒ never probed.
    capabilities: Dict[str, bool] = Field(default_factory=dict)
    # A plain ``str``, deliberately — not a ``Literal``/enum. Legacy envelopes (and pre-T14a
    # records carrying the dormant word) must keep loading; the vocabulary is declared once in
    # ``services.tenant_service`` (BINDING_*), which is the only writer of this field.
    binding_mode: str = ""  # "" (aws) | "federation" | "invoke_unavailable" | "sp_secret" (dormant, §3B)
    # Secrets Manager ARN of the OPTIONAL account-admin credential (E29/T3, OB-7). The ARN
    # ONLY — the client id and the secret both live inside that secret's JSON body (keys
    # :data:`ACCOUNT_ADMIN_ID_KEY` / :data:`ACCOUNT_ADMIN_SECRET_KEY`), so neither half is a
    # read field on this model. Empty ⇒ no account-admin credential was ever supplied, which
    # is the ordinary case: federation is an extra grant a customer may not have made.
    account_admin_secret_arn: str = ""
    description: str = ""
    created_by: str
    created_at: str
    updated_at: str


class TenantCreate(BaseModel):
    """Write-only input — the POST /tenants body. ``platform`` is settable HERE and only
    here (it is immutable afterwards); the capability fields are not client-writable —
    the connect-time probe owns them."""

    name: str
    line_of_business: str
    entra_group_ids: List[str]
    platform: TenantPlatform = TenantPlatform.AWS
    stages: Dict[str, StageConfig]
    # OPTIONAL account-admin credential, Databricks only, WRITE-ONLY (E29/T3, OB-7). Present
    # on a write model and on NO read model: the service puts both halves in Secrets Manager
    # and persists only ``Tenant.account_admin_secret_arn``.
    #
    # ALL-OR-NOTHING, and that is why it is two plain strings rather than a nested object: the
    # pair only means anything together (an id with no secret cannot mint a token), and the
    # frontend's own ``isAccountAdminCredentialUsable`` already refuses a half-filled pair.
    # These are what UNLOCK ``federation`` binding mode — without them the account-level
    # federation-policy probe cannot run, so the tenant is honestly badged
    # ``invoke_unavailable`` (E29/T14a, design §3B: no silent downgrade to ``sp_secret``).
    account_admin_client_id: str = ""
    account_admin_secret: str = ""
    description: str = ""


class TenantUpdate(BaseModel):
    """Write-only input — the PUT /tenants/{id} body. All optional (partial update).

    NO ``platform`` field, by contract: a tenant's platform is immutable after create, so
    a body carrying one is dropped rather than obeyed. A ``stages`` update must still match
    the stored platform's shape — enforced in ``tenant_service``."""

    name: Optional[str] = None
    line_of_business: Optional[str] = None
    entra_group_ids: Optional[List[str]] = None
    stages: Optional[Dict[str, StageConfig]] = None
    # ROTATION of the write-only account-admin credential (E29/T3, OB-7). Present here so an
    # admin can unlock federation on a tenant that connected WITHOUT the extra grant — a real
    # sequence, since Tier-3 account-admin access is often obtained after the workspace one.
    #
    # ``None`` (unset) means "leave the stored credential alone"; a supplied pair means
    # "replace it". The service reads that distinction off ``exclude_unset``, which is why
    # these are ``Optional`` rather than defaulting to ``""`` like their ``TenantCreate``
    # counterparts — on create, absent and blank mean the same thing; on update they do NOT.
    #
    # Note what is NOT here: ``account_admin_secret_arn``. The ARN is the SERVER's value
    # (OB-10) — a client that could send one could re-point a tenant at another tenant's
    # secret. The per-stage ``sp_client_secret_arn`` cannot be omitted the same way (it lives
    # inside the ``stages`` shape), so the service ignores a body-supplied one explicitly.
    account_admin_client_id: Optional[str] = None
    account_admin_secret: Optional[str] = None
    description: Optional[str] = None


def hydrate_tenant_item(clean: dict) -> dict:
    """Fold legacy tenant items forward on read — the zero-migration path.

    Two generations of defaults, both applied to the SAME dict object (mutate + return, so
    callers holding the reference see the hydration):

    * **E29** — a record written before platform typing has no ``platform`` key. Default it
      to ``"aws"``: every pre-E29 tenant was an AWS tenant. ``capabilities``/``binding_mode``
      need no defaulting here — the model's own field defaults cover them. An explicit
      ``platform`` is never overwritten.
    * **E25** — a pre-E25 record has no ``stages`` key but carries flat
      ``aws_account_dev``/``aws_account_prod``/``aws_region`` fields. Build a ``stages`` map
      (dev/prod, both inheriting the flat region; ECR + roles default empty) and drop the
      three flat keys. If ``stages`` is already present, leave it alone (mirror of the
      legacy-default pattern in ``ProjectService._hydrate_project``)."""
    clean.setdefault("platform", TenantPlatform.AWS.value)
    if "stages" in clean or "aws_account_dev" not in clean:
        return clean
    region = clean.get("aws_region", "us-east-1")
    clean["stages"] = {
        "dev": {"account_id": clean["aws_account_dev"], "region": region},
        "prod": {"account_id": clean["aws_account_prod"], "region": region},
    }
    for key in ("aws_account_dev", "aws_account_prod", "aws_region"):
        clean.pop(key, None)
    return clean
