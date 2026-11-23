#!/usr/bin/env python
"""Seed the ``default`` tenant and bulk-stamp ``tenant_id="default"`` onto legacy records.

This is the one-shot multi-tenancy migration for data written before tenants existed. It is
a USER / CONTROLLER step: without ``--dry-run`` it makes live AWS calls (DynamoDB via
``TenantService``/``ProjectService``, the registries via
``AgentRegistryService``/``McpServerRegistryService``) and therefore needs AWS
credentials plus the provisioned registry ids. What it does:

  1. **Seed the tenant** — if no tenant with id ``"default"`` exists, write one via
     ``TenantService.upsert_seed`` (idempotent by fixed id):
     ``name="Default-Platform"``, ``line_of_business="Platform"``,
     ``entra_group_ids=[<--group-id arg or DEFAULT_TENANT_GROUP_ID env>]``,
     ``aws_account_dev/prod=<--account-id arg, else the caller's account via STS
     get-caller-identity — the account is a property of the AWS credentials the
     script runs with, never checked-in config; validated against the tenant
     model's 12-digit rule before any write>``,
     ``aws_region=<--region arg, else the infra files' AWS_REGION/aws_region —
     there is no hardcoded account or region>``. In ``--dry-run`` an
     unreachable STS degrades to the placeholder ``<caller-account>`` in the plan
     output (dry-run works without cloud access; the region comes from the infra
     files, readable offline); a real run fails fast with a clear error instead.
     A default tenant that already exists but is UNWIRED (empty ECR URI and deploy
     role on both stages) is re-wired in place when the run carries wiring values;
     an already-wired tenant, or a run with no wiring values, leaves it untouched.
  2. **Bulk-stamp agents + MCP servers** — list each registry; every record whose
     ``tenant_id`` is missing (None/absent) is stamped ``"default"`` and re-written
     through the service's existing envelope-write path (``persist_identity`` —
     ``AgentUpdate``/``McpServerUpdate`` omit ``tenant_id``, so ``update()`` cannot
     carry it; ``persist_identity`` writes the full governance envelope, ``tenant_id``
     included).
  3. **Bulk-stamp projects** — list projects; any record missing ``tenant_id`` is
     re-saved with ``tenant_id="default"``. ``ProjectService``'s read path already
     hydrates legacy stored records as ``tenant_id="default"`` (``ProjectStore.hydrate_project``),
     so against the real service this step usually finds nothing to stamp — the
     hydration is the durable fallback and this pass is the belt to that suspenders.
  4. Print a per-resource summary (``stamped/total``). Re-running stamps nothing
     (idempotent): the tenant already exists and every record already carries a
     ``tenant_id``.

Runtime config comes from the INFRASTRUCTURE folder, not the backend's own settings
(the infra folder is the source of truth). Resolution precedence, per value:

  1. explicit CLI flag (``--region``, ``--account-id``, ``--agent-registry-id``,
     ``--mcp-registry-id``, ``--tenants-table``, ``--projects-table``), then
  2. the SCALAR assignments of ``terraform.tfvars`` under ``--infra-dir`` (default:
     ``platform/control_plane/infrastructure``), normalized to UPPER_SNAKE
     (``aws_region`` → ``AWS_REGION``). That file is what Terraform itself read, so
     its ``project_name``/``environment`` are what named the real tables, then
  3. derivation: the account via STS ``get-caller-identity`` — it is NOT read from
     the infra folder at all, since the account the script acts on is whichever one
     the ambient credentials belong to; the table names via Terraform's naming rule
     ``<PROJECT_NAME>-cp-<ENVIRONMENT>-<last-6-of-account>-{tenants,projects}``
     (root ``main.tf`` ``name_prefix`` + the dynamodb module's
     ``-tenants``/``-projects`` suffixes), then
  4. a hard error naming the sources tried, for every required value: region,
     account, agent registry id, tenants + projects table names. The MCP registry
     id stays optional — when unresolved, MCP servers are skipped with a warning.
     ``agent_registry_id`` in ``terraform.tfvars`` is not named as a source: the root
     variable no longer exists, so setting it there only draws an undeclared-variable
     warning from Terraform.

An ``infrastructure/.env`` is not read; anything it could have carried belongs on the
command line instead.

Failure envelope: one record failing to stamp is logged and skipped (the rest of the
migration proceeds); the exit code is then 1. An unresolved MCP registry id is
skipped with a warning, not an error.

Run from the backend dir (PYTHONPATH=src is required — src/ is not a package):

    cd platform/control_plane/backend && \
        PYTHONPATH=src venv/bin/python scripts/seed_default_tenant.py \
            --group-id <entra-group-object-id>

Offline check (NO writes — lists every resource and prints would-stamp counts):

    cd platform/control_plane/backend && \
        PYTHONPATH=src venv/bin/python scripts/seed_default_tenant.py \
            --group-id <entra-group-object-id> --dry-run

The infra files are read lazily inside ``main()`` — and boto3/STS lazily inside the
account resolver — so importing this module never reads any file or triggers AWS
setup; ``run()`` takes the services as parameters so tests drive it with in-memory
fakes.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path

logger = logging.getLogger("seed_default_tenant")

DEFAULT_TENANT_ID = "default"
DEFAULT_TENANT_NAME = "Default-Platform"
DEFAULT_TENANT_LOB = "Platform"
ACCOUNT_PLACEHOLDER = "<caller-account>"
REGION_PLACEHOLDER = "<caller-region>"
GROUP_ID_ENV_VAR = "DEFAULT_TENANT_GROUP_ID"
DEFAULT_INFRA_DIR = Path(__file__).resolve().parents[2] / "infrastructure"
TENANTS_TABLE_SUFFIX = "tenants"
PROJECTS_TABLE_SUFFIX = "projects"
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _parse_tfvars_file(path: Path) -> dict:
    """Parse the SCALAR assignments of a ``terraform.tfvars`` (``key = "value"`` /
    numbers / bools), normalizing keys to UPPER_SNAKE (``aws_region`` →
    ``AWS_REGION``). Non-scalar values (``tags = { … }`` blocks, lists) are skipped
    gracefully — including multi-line blocks — as are comments (full-line and
    inline after a value) and anything else that doesn't parse. Stdlib only."""
    values: dict = {}
    block_depth = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if block_depth > 0:
            block_depth += line.count("{") - line.count("}") + line.count("[") - line.count("]")
            continue
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        key, sep, rhs = line.partition("=")
        key = key.strip()
        rhs = rhs.strip()
        if not sep or not _IDENT_RE.match(key) or not rhs:
            continue
        if rhs[0] in "{[":
            block_depth = rhs.count("{") - rhs.count("}") + rhs.count("[") - rhs.count("]")
            continue
        if rhs[0] == '"':
            end = rhs.find('"', 1)
            if end == -1:
                continue
            value = rhs[1:end]
        else:
            value = rhs.split("#", 1)[0].strip()
        if value:
            values[key.upper()] = value
    return values


def _load_infra_config(infra_dir) -> dict:
    """Read the infra folder's ``terraform.tfvars`` scalars into one UPPER_SNAKE dict.

    ``terraform.tfvars`` is the ONLY config file consulted: it is the file Terraform
    itself read, so its ``project_name``/``environment`` are what named the deployed
    resources. An ``infrastructure/.env`` is not read; anything it could carry belongs on
    the command line or in the ambient environment instead.

    A missing file simply contributes nothing. This file is READ-ONLY here."""
    tfvars_path = Path(infra_dir) / "terraform.tfvars"
    return _parse_tfvars_file(tfvars_path) if tfvars_path.is_file() else {}


def _derive_table_name(infra: dict, account_id: str, suffix: str, *, infra_dir: Path) -> str:
    """Rebuild the table name Terraform derives (root ``main.tf``):
    ``<PROJECT_NAME>-cp-<ENVIRONMENT>-<last-6-of-account>-<suffix>``. Requires
    ``PROJECT_NAME`` + ``ENVIRONMENT`` from the tfvars — errors clearly when they're
    missing (the table name is never written down anywhere, so derivation is the only
    file-driven route)."""
    project_name = infra.get("PROJECT_NAME")
    environment = infra.get("ENVIRONMENT")
    if not project_name or not environment:
        raise RuntimeError(
            f"Could not derive the {suffix} table name — tried --{suffix}-table, then "
            f"project_name/environment in {infra_dir / 'terraform.tfvars'} "
            "(the table name itself is never in that file; Terraform derives it as "
            f"<project>-cp-<env>-<last-6-of-account>-{suffix})."
        )
    return f"{project_name}-cp-{environment}-{account_id[-6:]}-{suffix}"


def _resolve_runtime_config(args) -> dict:
    """Resolve every runtime value the seed needs, with the precedence
    CLI flag > ``terraform.tfvars`` scalars > derivation (STS account; Terraform's
    table-naming rule) > hard error naming the sources tried. Required: region,
    account, agent registry id, tenants + projects tables. Optional: MCP registry id
    (unresolved ⇒ skip-with-warning)."""
    infra_dir = Path(args.infra_dir) if args.infra_dir else DEFAULT_INFRA_DIR
    infra = _load_infra_config(infra_dir)
    tfvars_path = infra_dir / "terraform.tfvars"

    region = args.region or infra.get("AWS_REGION")
    if not region:
        raise RuntimeError(
            f"Could not resolve the AWS region — tried --region and aws_region in {tfvars_path}."
        )

    account_id = _resolve_account_id(args.account_id, dry_run=args.dry_run)

    agent_registry_id = args.agent_registry_id or infra.get("AGENT_REGISTRY_ID")
    if not agent_registry_id:
        raise RuntimeError(
            "Could not resolve the agent registry id — tried --agent-registry-id. "
            "NOTE: AWS mints registry ids and the platform resolves the registry by NAME "
            "at runtime, so no id is written down anywhere. Get the id from AWS and pass "
            "it explicitly: `aws agent-registry-control list-registries --query "
            "\"registries[?name=='agp-agents'].registryId\" --output text`."
        )

    mcp_registry_id = args.mcp_registry_id or infra.get("MCP_REGISTRY_ID")

    tenants_table = args.tenants_table or _derive_table_name(
        infra, account_id, TENANTS_TABLE_SUFFIX, infra_dir=infra_dir
    )
    projects_table = args.projects_table or _derive_table_name(
        infra, account_id, PROJECTS_TABLE_SUFFIX, infra_dir=infra_dir
    )

    return {
        "region": region,
        "account_id": account_id,
        "agent_registry_id": agent_registry_id,
        "mcp_registry_id": mcp_registry_id,
        "tenants_table": tenants_table,
        "projects_table": projects_table,
    }


def _validate_account_id(account_id: str) -> str:
    """Enforce the tenant model's existing 12-digit account rule
    (``models.tenant.ACCOUNT_ID_RE``) BEFORE anything is written. Imported lazily so
    module import stays stdlib-only."""
    from models.tenant import ACCOUNT_ID_RE

    if not ACCOUNT_ID_RE.match(account_id or ""):
        raise RuntimeError(
            f"{account_id!r} is not a 12-digit AWS account id — check --account-id "
            "or the caller's STS identity."
        )
    return account_id


def _resolve_account_id(explicit, *, dry_run: bool) -> str:
    """Resolve the seed tenant's AWS account: ``--account-id`` wins, else the caller's
    identity via STS ``get_caller_identity``. There is deliberately NO hardcoded fallback
    and NO file source — the account is a property of the credentials the script runs
    with, and reading it from checked-in config only creates a second answer that can go
    stale behind the live one. ``--account-id`` remains the deliberate override for
    seeding a tenant that points at ANOTHER account. Whatever value is used is validated
    against the 12-digit rule before it can be written.

    boto3 is imported lazily so importing this module stays stdlib-only. In ``--dry-run``
    an unreachable STS degrades to :data:`ACCOUNT_PLACEHOLDER` (dry-run must work without
    cloud access); a real run fails fast instead — the seed never writes a guessed
    account.
    """
    if explicit:
        return _validate_account_id(explicit)
    from botocore.exceptions import BotoCoreError, ClientError

    from core.aws_clients import client

    try:
        account = client("sts").get_caller_identity()["Account"]
    except (BotoCoreError, ClientError) as exc:
        if dry_run:
            logger.warning(
                "STS unreachable (%s) — dry-run continues with %s.",
                exc,
                ACCOUNT_PLACEHOLDER,
            )
            return ACCOUNT_PLACEHOLDER
        raise RuntimeError(
            f"Could not derive the AWS account id from STS ({exc}). Pass --account-id "
            "or configure AWS credentials — the seed tenant's aws_account_dev/prod "
            "come from the caller's identity, never a hardcoded value."
        ) from exc
    return _validate_account_id(account)


def _resolve_region(explicit, *, dry_run: bool) -> str:
    """Resolve the seed tenant's region: ``--region`` wins, else
    ``AWS_REGION``/``AWS_DEFAULT_REGION``, else the boto3 session's configured region.
    NEVER silently defaults (region is environment-specific): in ``--dry-run`` an
    unresolvable region degrades to :data:`REGION_PLACEHOLDER`; a real run fails fast.
    """
    if explicit:
        return explicit
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if region:
        return region
    from botocore.exceptions import BotoCoreError

    from core.aws_clients import ambient_region

    try:
        region = ambient_region()
    except BotoCoreError:
        region = None
    if region:
        return region
    if dry_run:
        logger.warning("No AWS region configured — dry-run continues with %s.", REGION_PLACEHOLDER)
        return REGION_PLACEHOLDER
    raise RuntimeError(
        "No AWS region configured — pass --region or set AWS_REGION/AWS_DEFAULT_REGION "
        "(the seed refuses to guess a region; it is environment-specific)."
    )


def _build_default_tenant(
    group_id: str,
    *,
    account_id: str,
    region: str,
    dev_ecr_uri: str = "",
    prod_ecr_uri: str = "",
    dev_deploy_role_arn: str = "",
    prod_deploy_role_arn: str = "",
    dev_push_role_arn: str = "",
    prod_push_role_arn: str = "",
):
    """Construct the seed ``Tenant`` (pure Pydantic — no AWS). Imported lazily so
    importing this module needs nothing beyond stdlib.

    The tenant's stages are a nested map. Both ``dev`` and ``prod`` point at the same
    (deploy) account in a single-account install; the ECR URIs and push/deploy role
    ARNs are passed in by the operator on the command line (``--dev-ecr-uri`` and
    friends, read off the ``terraform output`` after apply) and default empty —
    ``""`` means not-yet-wired / deploy-in-place."""
    from datetime import datetime, timezone

    from models.tenant import Tenant, TenantStageConfig

    ts = datetime.now(timezone.utc).isoformat()
    return Tenant(
        id=DEFAULT_TENANT_ID,
        name=DEFAULT_TENANT_NAME,
        line_of_business=DEFAULT_TENANT_LOB,
        entra_group_ids=[group_id],
        stages={
            "dev": TenantStageConfig(
                account_id=account_id,
                region=region,
                ecr_repo_uri=dev_ecr_uri,
                push_role_arn=dev_push_role_arn,
                deploy_role_arn=dev_deploy_role_arn,
            ),
            "prod": TenantStageConfig(
                account_id=account_id,
                region=region,
                ecr_repo_uri=prod_ecr_uri,
                push_role_arn=prod_push_role_arn,
                deploy_role_arn=prod_deploy_role_arn,
            ),
        },
        description="Seed tenant for the default/self tenant (E24/T9, E25/T7 wiring).",
        created_by="seed",
        created_at=ts,
        updated_at=ts,
    )


def _tenant_is_unwired(tenant) -> bool:
    """A default tenant is UNWIRED iff BOTH its dev and prod stages have an empty
    ``deploy_role_arn`` AND an empty ``ecr_repo_uri`` — i.e. it is provably
    untouched-by-wiring. Any non-empty deploy-role/ecr on either stage means an
    operator (or a prior wired run) has already set it, so we treat it as wired and
    never overwrite it. Missing stages (a hand-edited/legacy record) also count as
    NOT unwired, so we err on the side of leaving it alone."""
    stages = getattr(tenant, "stages", None) or {}
    if not ({"dev", "prod"} <= set(stages)):
        return False
    for key in ("dev", "prod"):
        stage = stages[key]
        if getattr(stage, "deploy_role_arn", "") or getattr(stage, "ecr_repo_uri", ""):
            return False
    return True


def _needs_stamp(record) -> bool:
    """A record needs stamping iff its ``tenant_id`` is missing (None or absent).
    A record that already carries ANY tenant (including ``"default"``) is untouched."""
    return getattr(record, "tenant_id", None) is None


def _stamp_resource(service, label: str, list_records, write_record, *, dry_run: bool):
    """List one resource type and stamp ``tenant_id="default"`` where missing.

    Returns ``(stamped, total, failures)``. ``dry_run`` counts would-stamps without
    mutating or writing anything. A failing write is logged and skipped so one bad
    record cannot abort the migration.
    """
    if service is None:
        logger.warning("  %s: service not configured — skipping.", label)
        return 0, 0, 0

    records = list_records()
    stamped = 0
    failures = 0
    for record in records:
        if not _needs_stamp(record):
            continue
        if dry_run:
            stamped += 1
            logger.info("  [dry-run] would stamp %s %r", label, getattr(record, "id", "?"))
            continue
        try:
            record.tenant_id = DEFAULT_TENANT_ID
            write_record(record)
            stamped += 1
            logger.info(
                "  stamped %s %r -> tenant_id=%r", label, getattr(record, "id", "?"), DEFAULT_TENANT_ID
            )
        except Exception as exc:
            failures += 1
            logger.exception(
                "  failed to stamp %s %r: %s — continuing", label, getattr(record, "id", "?"), exc
            )
    return stamped, len(records), failures


def run(
    *,
    group_id: str,
    dry_run: bool,
    tenant_service,
    agent_service,
    mcp_server_service,
    project_service,
    account_id: str | None = None,
    region: str | None = None,
    dev_ecr_uri="",
    prod_ecr_uri="",
    dev_deploy_role_arn="",
    prod_deploy_role_arn="",
    dev_push_role_arn="",
    prod_push_role_arn="",
) -> int:
    """Seed the default tenant + stamp unstamped records. Returns an exit code.

    Services are parameters (built in ``main()`` for the real run; in-memory fakes in
    tests) so this function performs no service construction and no config access.
    ``account_id``/``region`` arrive already resolved by ``main()`` (CLI > infra
    files > STS); when ``None`` they fall back to the caller's environment (STS /
    region config), and only when a tenant will actually be created (an existing
    tenant needs no AWS call). The ``*_ecr_uri`` / ``*_role_arn`` values are the
    cross-account CICD wiring, supplied as CLI flags from the ``terraform output``
    of the apply that created them; they default empty.

    The default tenant has three cases: absent → create; exists but UNWIRED (empty
    deploy role and ECR URI on both stages, see ``_tenant_is_unwired``) and this run
    carries wiring values → re-wire it in place, preserving its account, region and
    group unless the run overrides them; exists and already wired, or the run carries
    no wiring → leave it untouched, so a bare re-run stays a no-op and an operator's
    changes are never overwritten.
    """
    existing_default = next(
        (t for t in tenant_service.list() if getattr(t, "id", None) == DEFAULT_TENANT_ID),
        None,
    )
    run_has_wiring = any(
        (
            dev_ecr_uri,
            prod_ecr_uri,
            dev_deploy_role_arn,
            prod_deploy_role_arn,
            dev_push_role_arn,
            prod_push_role_arn,
        )
    )

    if existing_default is not None and not (run_has_wiring and _tenant_is_unwired(existing_default)):
        tenant_status = "exists"
        logger.info("Tenant %r already exists — nothing to create.", DEFAULT_TENANT_ID)
    else:
        rewiring = existing_default is not None
        if rewiring:
            existing_stage = (getattr(existing_default, "stages", {}) or {}).get("dev")
            resolved_account = account_id or getattr(existing_stage, "account_id", None)
            resolved_region = region or getattr(existing_stage, "region", None)
            resolved_group = group_id or next(
                iter(getattr(existing_default, "entra_group_ids", []) or []), None
            )
            if not (resolved_account and resolved_region and resolved_group):
                raise RuntimeError(
                    f"Tenant {DEFAULT_TENANT_ID!r} exists but carries no dev-stage account/region or "
                    "no Entra group to preserve; pass --account-id, --region and --group-id to re-wire it."
                )
        else:
            resolved_account = _resolve_account_id(account_id, dry_run=dry_run)
            resolved_region = _resolve_region(region, dry_run=dry_run)
            resolved_group = group_id

        if dry_run:
            tenant_status = "would-wire" if rewiring else "would-create"
            verb = "would re-wire existing" if rewiring else "would create"
            print(
                f"[dry-run] {verb} tenant {DEFAULT_TENANT_ID!r} with "
                f"stages.dev/prod account_id={resolved_account} region={resolved_region} "
                f"dev_ecr={dev_ecr_uri or '(unset)'} prod_ecr={prod_ecr_uri or '(unset)'} "
                f"dev_deploy_role={dev_deploy_role_arn or '(unset)'} "
                f"prod_deploy_role={prod_deploy_role_arn or '(unset)'}"
            )
        else:
            tenant_service.upsert_seed(
                _build_default_tenant(
                    resolved_group,
                    account_id=resolved_account,
                    region=resolved_region,
                    dev_ecr_uri=dev_ecr_uri,
                    prod_ecr_uri=prod_ecr_uri,
                    dev_deploy_role_arn=dev_deploy_role_arn,
                    prod_deploy_role_arn=prod_deploy_role_arn,
                    dev_push_role_arn=dev_push_role_arn,
                    prod_push_role_arn=prod_push_role_arn,
                )
            )
            tenant_status = "wired" if rewiring else "created"
            logger.info(
                "%s tenant %r (group %s, account %s, region %s).",
                "Re-wired" if rewiring else "Created",
                DEFAULT_TENANT_ID,
                resolved_group,
                resolved_account,
                resolved_region,
            )

    failures = 0
    a_stamped, a_total, a_fail = _stamp_resource(
        agent_service,
        "agent",
        lambda: agent_service.list(),
        lambda r: agent_service.persist_identity(r),
        dry_run=dry_run,
    )
    m_stamped, m_total, m_fail = _stamp_resource(
        mcp_server_service,
        "mcp_server",
        lambda: mcp_server_service.list(),
        lambda r: mcp_server_service.persist_identity(r),
        dry_run=dry_run,
    )
    p_stamped, p_total, p_fail = _stamp_resource(
        project_service,
        "project",
        lambda: project_service.list_projects(),
        lambda r: project_service._save_project(r),
        dry_run=dry_run,
    )
    failures = a_fail + m_fail + p_fail

    summary = (
        f"tenant={tenant_status} "
        f"agents={a_stamped}/{a_total} "
        f"mcp_servers={m_stamped}/{m_total} "
        f"projects={p_stamped}/{p_total}"
    )
    if dry_run:
        print(f"dry run (no writes): {summary}")
    elif failures:
        print(f"seed FINISHED WITH {failures} FAILURE(S): {summary}")
    else:
        print(f"seed complete: {summary}")
    return 1 if failures else 0


def _build_services(config: dict):
    """Construct the four real services from the resolved infra config (lazy —
    main()-only; NEVER from the backend's own settings — the infrastructure folder
    is the source of truth, see :func:`_resolve_runtime_config`).

    The MCP registry is optional: an unresolved ``mcp_registry_id`` yields ``None``
    (MCP servers are skipped with a warning). Both registries live in the stack's
    one resolved region. ``ProjectService`` only needs its persistence surface here —
    the orchestration collaborators (registry/identity/connections) are never touched
    by ``list_projects``/``_save_project``, so they are passed as ``None``.
    """
    from services.agent_registry_service import AgentRegistryService
    from services.mcp_server_service import McpServerRegistryService
    from services.project_service import ProjectService
    from services.tenant_service import TenantService

    tenant_service = TenantService(table_name=config["tenants_table"], region=config["region"])

    agent_service = AgentRegistryService(
        registry_id=config["agent_registry_id"],
        region=config["region"],
    )

    mcp_server_service = None
    if config["mcp_registry_id"]:
        mcp_server_service = McpServerRegistryService(
            registry_id=config["mcp_registry_id"],
            region=config["region"],
        )
    else:
        logger.warning(
            "No MCP registry id resolved (--mcp-registry-id) — MCP servers will be "
            "skipped. Terraform writes an id nowhere (the platform resolves registries "
            "by NAME at runtime), so pass --mcp-registry-id explicitly if you want MCP "
            "servers stamped: "
            "`aws agent-registry-control list-registries --query "
            "\"registries[?name=='agp-mcp-servers'].registryId\" --output text`."
        )

    project_service = ProjectService(
        table_name=config["projects_table"],
        registry=None,
        identity=None,
        connection_service=None,
        region=config["region"],
    )
    return tenant_service, agent_service, mcp_server_service, project_service


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Seed the 'default' tenant and stamp tenant_id='default' onto every "
            "agent / MCP server / project record that is missing one (E24/T9)."
        ),
    )
    parser.add_argument(
        "--group-id",
        default=None,
        help=(
            "Entra group object id for the default tenant's entra_group_ids "
            f"(default: the {GROUP_ID_ENV_VAR} env var)."
        ),
    )
    parser.add_argument(
        "--account-id",
        default=None,
        help=(
            "AWS account id for the default tenant's aws_account_dev/prod "
            "(default: the caller's account via STS get-caller-identity; must be 12 "
            "digits — it is never read from the infra files and there is no hardcoded "
            "fallback). Pass this only to deliberately seed a tenant pointing at "
            "another account than the credentials' own."
        ),
    )
    parser.add_argument(
        "--region",
        default=None,
        help=(
            "AWS region for the stack (default: aws_region in terraform.tfvars; there "
            "is no hardcoded fallback)."
        ),
    )
    parser.add_argument(
        "--infra-dir",
        default=None,
        help=(
            "Infrastructure folder holding terraform.tfvars — the source of truth for "
            f"runtime config, READ ONLY (default: {DEFAULT_INFRA_DIR})."
        ),
    )
    parser.add_argument(
        "--agent-registry-id",
        default=None,
        help=(
            "Agent registry id. Nothing in the repo records it — AWS mints the id and "
            "the platform resolves the registry by NAME at runtime — so this flag is "
            "the normal way to supply it; get the id from `aws agent-registry-control "
            "list-registries`. terraform.tfvars is not a source you should edit: the "
            "root variable was deleted, so setting an id there only produces an "
            "undeclared-variable warning from Terraform."
        ),
    )
    parser.add_argument(
        "--mcp-registry-id",
        default=None,
        help=(
            "MCP registry id (unresolved = MCP servers are skipped with a warning). "
            "Same note as --agent-registry-id: pass it explicitly."
        ),
    )
    parser.add_argument(
        "--tenants-table",
        default=None,
        help=(
            "Tenants DynamoDB table name override (default: derived as "
            "<PROJECT_NAME>-cp-<ENVIRONMENT>-<last-6-of-account>-tenants from the "
            "tfvars — Terraform's own naming rule)."
        ),
    )
    parser.add_argument(
        "--projects-table",
        default=None,
        help=(
            "Projects DynamoDB table name override (default: derived as "
            "<PROJECT_NAME>-cp-<ENVIRONMENT>-<last-6-of-account>-projects from the "
            "tfvars — Terraform's own naming rule)."
        ),
    )
    parser.add_argument(
        "--dev-ecr-uri",
        default="",
        help="ECR repo URI for the default tenant's dev stage (E25 wiring; default empty).",
    )
    parser.add_argument(
        "--prod-ecr-uri",
        default="",
        help="ECR repo URI for the default tenant's prod stage (E25 wiring; default empty).",
    )
    parser.add_argument(
        "--dev-deploy-role-arn",
        default="",
        help=(
            "Cross-account deploy-role ARN CodeBuild assumes for the dev stage "
            "(E25; must be an agp-deployment-* role; default empty = deploy-in-place)."
        ),
    )
    parser.add_argument(
        "--prod-deploy-role-arn",
        default="",
        help=(
            "Cross-account deploy-role ARN CodeBuild assumes for the prod stage "
            "(E25; must be an agp-deployment-* role; default empty = deploy-in-place)."
        ),
    )
    parser.add_argument(
        "--dev-push-role-arn",
        default="",
        help="GitHub-OIDC ECR push-role ARN for the dev stage (E25 wiring; default empty).",
    )
    parser.add_argument(
        "--prod-push-role-arn",
        default="",
        help="GitHub-OIDC ECR push-role ARN for the prod stage (E25 wiring; default empty).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List resources and print would-stamp counts without writing anything.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    group_id = args.group_id or os.environ.get(GROUP_ID_ENV_VAR, "")
    if not group_id:
        logger.error(
            "No Entra group id for the default tenant — pass --group-id or set %s.",
            GROUP_ID_ENV_VAR,
        )
        return 2

    try:
        config = _resolve_runtime_config(args)
        tenant_service, agent_service, mcp_server_service, project_service = _build_services(config)
        return run(
            group_id=group_id,
            dry_run=args.dry_run,
            tenant_service=tenant_service,
            agent_service=agent_service,
            mcp_server_service=mcp_server_service,
            project_service=project_service,
            account_id=(None if config["account_id"] == ACCOUNT_PLACEHOLDER else config["account_id"]),
            region=config["region"],
            dev_ecr_uri=args.dev_ecr_uri,
            prod_ecr_uri=args.prod_ecr_uri,
            dev_deploy_role_arn=args.dev_deploy_role_arn,
            prod_deploy_role_arn=args.prod_deploy_role_arn,
            dev_push_role_arn=args.dev_push_role_arn,
            prod_push_role_arn=args.prod_push_role_arn,
        )
    except Exception as exc:
        logger.exception("Seeding failed: %s", exc)
        logger.error(
            "Runtime config comes from the INFRASTRUCTURE folder's terraform.tfvars "
            "(--infra-dir, default %s), not the backend's own settings — except the AWS "
            "account, which comes from the credentials this script runs with. Check "
            "that: (1) AWS credentials are configured, for the account that owns the "
            "deployment (`aws sts get-caller-identity`); (2) terraform.tfvars carries "
            "aws_region plus project_name/environment (for the derived table names) — "
            "or pass the explicit flags (--region, --account-id, --agent-registry-id, "
            "--mcp-registry-id, --tenants-table, --projects-table); (3) the registries "
            "are provisioned in the resolved region.",
            DEFAULT_INFRA_DIR,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
