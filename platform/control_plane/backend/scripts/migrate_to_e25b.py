#!/usr/bin/env python
"""Migrate an EXISTING pre-E25 / E25 install to E25B (cross-account runtime deploy).

This is an OPERATOR / CONTROLLER step run as part of deploying the E25B version. It
ORCHESTRATES existing services only — it provisions nothing of its own and writes no
new data model. E25B makes agent runtimes deploy CROSS-ACCOUNT; the operator-facing
consequences for an existing install (and how this script addresses each) are:

  1. **The operator's own ``terraform apply`` (NOT this script) DELETES the platform
     ``agentcore_runtime_exec_role`` module and changes the buildspec so it no longer
     passes ``exec_role_arn``.** The runtime module now self-provisions its own exec
     role. That apply also re-stages the S3 runtime-module zip
     (``runtime-module/agentcore_runtime.zip`` on the state bucket). This script does
     NOT run terraform — it must be run AFTER the apply (see the ORDER below).

  2. **The per-org ``agp-runtime-infra`` GitHub repo must be updated to the E25B
     module.** CRITICAL: the pre-E25B module declared ``exec_role_arn`` as a REQUIRED
     variable (no default). After the apply the buildspec stops passing it, so an org
     still running the OLD module fails ``terraform`` with "No value for required
     variable exec_role_arn". The backend rollout SKIPS existing repos by default, so
     they must be FORCE-OVERWRITTEN. This is the script's CORE job (Step 2): for every
     connected org it re-pushes ``agp-runtime-infra`` from the fresh S3 module zip via
     ``RolloutService.rollout(template_names=[], overwrite=True, overwrite_infra=True)``
     — that call touches ONLY the forced infra repo, no base templates.
     NOTE (E28D/T8): ``overwrite_infra`` is the flag that does the forcing. ``overwrite``
     governs TEMPLATE repos only and no longer reaches the infra repo at all, so a call
     that omits ``overwrite_infra`` gets ``action="skipped"`` on an existing repo — i.e.
     this script's core job would silently not happen. It is passed explicitly, and Step 2
     counts an org as done ONLY when the infra item's action is ``overwritten``/``created``
     (a ``skipped`` is reported as such and makes the run exit nonzero).
     NOTE (E28C/T3): "overwrite" is no longer delete+recreate. It is now an idempotent
     ONE-commit push of the module files onto the existing repo, so the repository, its
     history and its issues survive — and a re-run whose bytes are unchanged writes no
     commit at all. Same outcome for this migration (the module files end up current),
     without destroying an org's repo to get there.

  3. **Already-deployed runtimes point at the now-deleted platform exec role.** They
     need a re-push to self-provision an in-account role. This script DETECTS + REPORTS
     them (Step 3) — it does NOT auto-trigger any build (report-only in both modes).

  4. **E25B does NOT change the tenant/agent/repo DATA model** — there is no E25B-only
     DynamoDB migration. BUT the operator may be coming from PRE-E25 (before the
     E24/E25 tenant reshape), so Step 1 CHAINS the existing E24/E25 tenant migration
     (``seed_default_tenant.run``). It is idempotent: on an already-E25 install it
     stamps nothing.

STEPS (in order; each dry-run-aware; each prints a per-step summary):

  Step 1 — tenant migration (chained): call ``seed_default_tenant.run`` with the same
           resolved config + ``--group-id``. Idempotent; ``dry_run`` passes through.
           NOTE: the chained seed CREATES the default tenant if absent but does NOT
           re-wire an existing tenant's stages — the terraform apply's local-exec seed
           (run first, see ORDER below) does the stage wiring.
  Step 2 — overwrite ``agp-runtime-infra`` per connected org: for each connection from
           ``ConnectionService.list_connections()`` call ``rollout(connection_id,
           template_names=[], overwrite=True, overwrite_infra=True)`` and record the
           infra-repo item's action. In ``--dry-run`` NO ``rollout`` call is made (it
           writes a real commit to a real repo); instead the orgs that WOULD be
           overwritten are listed.
  Step 3 — detect + report deployed runtimes: list agents with a non-empty
           ``agent_arn`` and print each (agent id, name, ARN) with a NOTE that it must
           be RE-PUSHED to self-provision an in-account exec role. Report-only in BOTH
           modes — no build is ever triggered.

⚠️ OPERATOR RUN ORDER (unmissable — also printed at the end of every run):
   (a) run ``terraform apply`` FIRST — it deploys the new buildspec, DELETES the old
       platform exec-role module, and re-stages the S3 runtime-module zip;
   (b) THEN run this script — it force-overwrites the per-org ``agp-runtime-infra``
       repos from the FRESH zip and reports the runtimes needing a re-push;
   (c) THEN re-push each reported runtime's repo so it self-provisions its in-account
       exec role.
   Running this script BEFORE the apply would push the STALE module zip — do not.

Runtime config comes from the INFRASTRUCTURE folder (the source of truth), resolved
EXACTLY the ``seed_default_tenant`` way (CLI flag > ``terraform.tfvars`` scalars >
derivation via STS / Terraform's own naming rules > hard error). There are NO
hardcoded AWS account ids or regions anywhere. On top of the seed's values (region,
account, agent registry id, tenants/projects tables) this script also resolves:

  - the **connections table** (``<PROJECT_NAME>-cp-<ENVIRONMENT>-<last-6-of-account>-
    connections`` — Terraform's naming rule, ``--connections-table`` overrides);
  - the **connections secret prefix** (``connections_secret_prefix`` in the tfvars,
    else the backend default ``agp-dev/git-connections/``);
  - the **runtime-module S3 bucket + key** (the state bucket
    ``<name_prefix>-tf-state`` — or the operator's ``STATE_BACKEND_BUCKET_NAME_PREFIX``
    — and the fixed key ``runtime-module/agentcore_runtime.zip``).

``--dry-run`` makes ZERO mutating calls: it PLANS and prints (Step 1 stamps nothing,
Step 2 lists orgs but never overwrites a repo, Step 3 is read-only). A real run makes
the calls. Re-running is safe (idempotent): the tenant step stamps nothing, a repo
overwrite is delete+recreate (same result), the runtime report is read-only.

Run from the backend dir (PYTHONPATH=src is required — src/ is not a package):

    cd platform/control_plane/backend && \
        PYTHONPATH=src venv/bin/python scripts/migrate_to_e25b.py \
            --group-id <entra-group-object-id>

Offline plan (NO mutating calls — lists orgs that WOULD be overwritten + runtimes):

    cd platform/control_plane/backend && \
        PYTHONPATH=src venv/bin/python scripts/migrate_to_e25b.py \
            --group-id <entra-group-object-id> --dry-run

The tfvars is read lazily inside ``main()`` (and boto3/STS lazily inside the
resolvers), so importing this module never reads a file or triggers AWS setup;
``run()`` takes the services as parameters so tests drive it with in-memory fakes.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# The seed script lives beside this one in scripts/. Put the scripts dir on sys.path so
# ``import seed_default_tenant`` resolves both when this file is run directly AND when a
# test imports it as ``scripts.migrate_to_e25b`` (the scripts dir is not otherwise on the
# path). seed_default_tenant is stdlib-only at import top, so importing it is import-safe.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import seed_default_tenant as seed  # noqa: E402 — after the sys.path shim

logger = logging.getLogger("migrate_to_e25b")

# --- E25B-only config (on top of the seed's resolved values) -------------------
# The connections table is named by Terraform's rule, same as tenants/projects.
CONNECTIONS_TABLE_SUFFIX = "connections"
# The state bucket (which stages the runtime-module zip) is "<name_prefix>-tf-state"
# when the operator leaves STATE_BACKEND_BUCKET_NAME_PREFIX empty (root main.tf /
# modules/state_backend/main.tf); the runtime_module.tf uploads the zip to a FIXED key.
STATE_BUCKET_SUFFIX = "tf-state"
DEFAULT_RUNTIME_MODULE_KEY = "runtime-module/agentcore_runtime.zip"
# Backend default (core/config.py CONNECTIONS_SECRET_PREFIX) — used when the tfvars
# doesn't carry one. The secret prefix is not account/region-specific (no hardcoded ids).
DEFAULT_CONNECTIONS_SECRET_PREFIX = "agp-dev/git-connections/"

# Base-template scaffolds ship in the image beside backend/ (config.py resolves
# AGENT_TEMPLATES_DIR to control_plane/agent-templates). Step 2 passes template_names=[]
# so this dir is never read — RolloutService still needs it constructed, so we compute
# the same default (overridable via --agent-templates-dir).
DEFAULT_AGENT_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "agent-templates"

# The forced per-org infra repo whose action we report per org (from the rollout module).
try:  # keep import lazy-safe: the service imports httpx, which the image ships.
    from services.template_rollout_service import INFRA_REPO_NAME
except Exception:  # noqa: BLE001 — module-import must not hard-require the service tree.
    INFRA_REPO_NAME = "agp-runtime-infra"

# The only infra-repo actions that mean "this org now runs the E25B module" (``_ensure_infra``
# derives its word from the repo's state: "overwritten" = re-pushed on top, "created" = the repo
# was absent and was made). Anything else — notably "skipped", which is what a rollout WITHOUT
# ``overwrite_infra=True`` returns for an existing repo — means the migration did NOT happen for
# that org, so it must not be counted as a success.
_DONE_ACTIONS = frozenset({"overwritten", "created"})


def _resolve_e25b_config(args) -> dict:
    """Resolve every runtime value E25B needs. Reuses the seed's resolver verbatim for
    the shared values (region, account, agent/mcp registry ids, tenants/projects tables),
    then derives the E25B-only values the seed way (CLI > terraform.tfvars > Terraform
    naming rule / backend default). NO hardcoded account ids or regions anywhere.
    """
    base = seed._resolve_runtime_config(args)  # region/account/registries/tables
    infra_dir = Path(args.infra_dir) if args.infra_dir else seed.DEFAULT_INFRA_DIR
    infra = seed._load_infra_config(infra_dir)
    account_id = base["account_id"]

    connections_table = args.connections_table or seed._derive_table_name(
        infra, account_id, CONNECTIONS_TABLE_SUFFIX, infra_dir=infra_dir
    )
    connections_secret_prefix = (
        args.connections_secret_prefix
        or infra.get("CONNECTIONS_SECRET_PREFIX")
        or DEFAULT_CONNECTIONS_SECRET_PREFIX
    )

    # Runtime-module bucket: CLI > infra RUNTIME_MODULE_BUCKET > the operator's explicit
    # state-bucket prefix (STATE_BACKEND_BUCKET_NAME_PREFIX, used verbatim as the bucket
    # name) > the derived default "<name_prefix>-tf-state" (Terraform's own rule). The
    # runtime_module.tf key is fixed; infra RUNTIME_MODULE_KEY / --runtime-module-key win.
    runtime_module_bucket = (
        args.runtime_module_bucket
        or infra.get("RUNTIME_MODULE_BUCKET")
        or infra.get("STATE_BACKEND_BUCKET_NAME_PREFIX")
        or seed._derive_table_name(
            infra, account_id, STATE_BUCKET_SUFFIX, infra_dir=infra_dir
        )
    )
    runtime_module_key = (
        args.runtime_module_key
        or infra.get("RUNTIME_MODULE_KEY")
        or DEFAULT_RUNTIME_MODULE_KEY
    )

    agent_templates_dir = args.agent_templates_dir or str(DEFAULT_AGENT_TEMPLATES_DIR)

    return {
        **base,
        "connections_table": connections_table,
        "connections_secret_prefix": connections_secret_prefix,
        "runtime_module_bucket": runtime_module_bucket,
        "runtime_module_key": runtime_module_key,
        "agent_templates_dir": agent_templates_dir,
    }


def _build_e25b_services(config: dict):
    """Construct the two E25B-only services (ConnectionService + RolloutService) from the
    resolved infra config — mirrors ``connections.py``'s ``get_rollout_service``. Lazy
    (main()-only) so importing this module never triggers boto3/service imports.

    The RolloutService gets a real provider client and S3 client; Step 2 only exercises the
    FORCED infra-repo path (``template_names=[]``), so the agent templates dir is never read
    — it is still required by the constructor.

    ``known_repo_names`` is deliberately NOT passed: it exists for the reconcile surface's adopt
    picker (E28C/T3), and this script never reconciles. Reconcile fails closed without it, which
    is the intended posture — an operator script must not be able to offer an adopt list built
    from an under-subtracted inventory.
    """
    import boto3

    from services.connection_service import ConnectionService
    from services.github_repo_service import GitHubRepoService
    from services.template_registry import TemplateRegistry
    from services.template_rollout_service import RolloutService

    connection_service = ConnectionService(
        table_name=config["connections_table"],
        secret_prefix=config["connections_secret_prefix"],
        region=config["region"],
    )
    rollout_service = RolloutService(
        GitHubRepoService(),
        connection_service,
        agent_templates_dir=config["agent_templates_dir"],
        s3_client=boto3.client("s3", region_name=config["region"]),
        runtime_module_bucket=config["runtime_module_bucket"],
        runtime_module_key=config["runtime_module_key"],
        # The template catalog (E28B/T2). Step 2 only rolls out the FORCED infra repo
        # (``template_names=[]``), which is never registered — but the constructor requires
        # the registry, and reconcile reads it.
        template_registry=TemplateRegistry(
            table_name=config["projects_table"], region=config["region"]
        ),
    )
    return connection_service, rollout_service


def _overwrite_infra_repos(connection_service, rollout_service, *, dry_run: bool):
    """Step 2 — force-overwrite the ``agp-runtime-infra`` repo in every connected org.

    Returns ``(overwritten, total, failures)``. In ``dry_run`` this makes NO mutating
    call (rollout writes a real commit to a real repo): it only LISTS the orgs that would
    be overwritten. A single org failing is logged + skipped (the rest still run) and bumps
    the failure count; the overwrite itself is idempotent (an unchanged re-push writes no
    commit at all).

    ``overwrite_infra=True`` is what forces the infra repo (E28D/T8 narrowed ``overwrite``
    to TEMPLATE repos only) — and the count is derived from the infra item's ACTION, never
    from "the call did not raise": only ``overwritten``/``created`` count as done. A
    ``skipped`` (or a missing infra item) is reported honestly and counted as a failure,
    because for THIS script a skip means its core job did not happen.
    """
    if connection_service is None or rollout_service is None:
        logger.warning("  connections/rollout service not configured — skipping Step 2.")
        return 0, 0, 0

    connections = connection_service.list_connections()
    overwritten = 0
    failures = 0
    for conn in connections:
        org = getattr(conn, "org", "?")
        conn_id = getattr(conn, "id", None)
        if dry_run:
            logger.info("  [dry-run] would overwrite %s in org %r", INFRA_REPO_NAME, org)
            print(f"  {org} -> would-overwrite")
            continue
        try:
            result = rollout_service.rollout(
                conn_id, template_names=[], overwrite=True, overwrite_infra=True
            )
            infra_item = next(
                (i for i in result.items if i.name == INFRA_REPO_NAME), None
            )
            action = getattr(infra_item, "action", "unknown") if infra_item else "unknown"
            if action in _DONE_ACTIONS:
                overwritten += 1
                logger.info("  overwrote %s in org %r -> %s", INFRA_REPO_NAME, org, action)
            else:
                failures += 1
                logger.error(
                    "  %s in org %r was NOT re-pushed -> %s — the E25B module is NOT "
                    "current in this org; terraform will still fail there",
                    INFRA_REPO_NAME,
                    org,
                    action,
                )
            print(f"  {org} -> {action}")
        except Exception as exc:  # noqa: BLE001 — log + continue, don't abort the migration
            failures += 1
            logger.error("  failed to overwrite %s in org %r: %s — continuing", INFRA_REPO_NAME, org, exc)
            print(f"  {org} -> failed")
    return overwritten, len(connections), failures


def _report_deployed_runtimes(agent_service):
    """Step 3 — DETECT + REPORT deployed runtimes (report-only in BOTH modes; no build is
    ever triggered). A deployed runtime is an agent with a non-empty ``agent_arn``. Each
    is printed with a NOTE that it must be RE-PUSHED so the runtime self-provisions its
    own in-account exec role (the apply deleted the platform exec role it pointed at).

    Returns the count of runtimes needing a re-push.
    """
    if agent_service is None:
        logger.warning("  agent service not configured — skipping Step 3.")
        return 0

    deployed = [a for a in agent_service.list() if getattr(a, "agent_arn", None)]
    if not deployed:
        print("  no deployed runtimes found — nothing to re-push.")
        return 0

    print(
        f"  {len(deployed)} deployed runtime(s) point at the now-deleted platform exec "
        "role and MUST be RE-PUSHED to self-provision an in-account exec role:"
    )
    for agent in deployed:
        print(
            f"  - agent_id={getattr(agent, 'id', '?')!r} "
            f"name={getattr(agent, 'name', '?')!r} "
            f"agent_arn={getattr(agent, 'agent_arn', '?')}"
        )
    print("  ACTION: re-push each repo above so its runtime provisions its own exec role.")
    return len(deployed)


def run(
    *,
    group_id: str,
    dry_run: bool,
    tenant_service,
    agent_service,
    mcp_server_service,
    project_service,
    connection_service,
    rollout_service,
    account_id=None,
    region=None,
    **wiring,
) -> int:
    """Run the three E25B migration steps in order. Returns an exit code (nonzero if the
    chained tenant step reported a stamp failure OR any org's infra-repo overwrite
    failed). Services are parameters (built in ``main()``; in-memory fakes in tests) so
    this performs no service construction and no config access. ``**wiring`` (the E25
    ``*_ecr_uri`` / ``*_role_arn`` values) passes straight through to the seed step.
    """
    print("=== Step 1: tenant migration (chained E24/E25 seed) ===")
    seed_rc = seed.run(
        group_id=group_id,
        dry_run=dry_run,
        tenant_service=tenant_service,
        agent_service=agent_service,
        mcp_server_service=mcp_server_service,
        project_service=project_service,
        account_id=account_id,
        region=region,
        **wiring,
    )

    print("=== Step 2: overwrite 'agp-runtime-infra' per connected org ===")
    ow, total, ow_failures = _overwrite_infra_repos(
        connection_service, rollout_service, dry_run=dry_run
    )
    if dry_run:
        print(f"  [dry-run] {total} org(s) would be overwritten (no repo touched).")
    else:
        print(f"  overwrote {ow}/{total} org repo(s) ({ow_failures} failure(s)).")

    print("=== Step 3: detect + report deployed runtimes (report-only) ===")
    needing_repush = _report_deployed_runtimes(agent_service)

    _print_operator_instructions(dry_run=dry_run, needing_repush=needing_repush)

    return 1 if (seed_rc or ow_failures) else 0


def _print_operator_instructions(*, dry_run: bool, needing_repush: int) -> None:
    """Print the unmissable operator run ORDER + a next-action summary."""
    print("")
    print("=== E25B migration: REQUIRED operator run ORDER ===")
    print("  (a) terraform apply FIRST — deploys the new buildspec, DELETES the old")
    print("      platform exec-role module, and re-stages the S3 runtime-module zip.")
    print("  (b) THEN run THIS script — force-overwrites each org's agp-runtime-infra")
    print("      repo from the FRESH zip and reports runtimes needing a re-push.")
    print("  (c) THEN re-push each reported runtime's repo so it self-provisions its")
    print("      own in-account exec role.")
    print("  Running this script BEFORE the apply pushes the STALE module zip — do not.")
    if dry_run:
        print("")
        print("  [dry-run] no repo was overwritten and no data was written. Re-run")
        print("  without --dry-run (after the terraform apply) to apply Step 2.")
    if needing_repush:
        print("")
        print(f"  NEXT: re-push the {needing_repush} runtime repo(s) listed above (step c).")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Migrate an EXISTING pre-E25 / E25 install to E25B (cross-account runtime "
            "deploy). Chains the E24/E25 tenant migration, force-overwrites each "
            "connected org's 'agp-runtime-infra' repo to the E25B module, and reports "
            "already-deployed runtimes that must be re-pushed."
        ),
        epilog=(
            "REQUIRED operator run ORDER:\n"
            "  (a) terraform apply FIRST — deploys the new buildspec, DELETES the old\n"
            "      platform exec-role module, and re-stages the S3 runtime-module zip.\n"
            "  (b) THEN run this script — force-overwrites each org's agp-runtime-infra\n"
            "      repo from the FRESH zip and reports runtimes needing a re-push.\n"
            "  (c) THEN re-push each reported runtime's repo so it self-provisions its\n"
            "      own in-account exec role.\n"
            "  Running this script BEFORE the apply pushes the STALE module zip — do not.\n"
            "\n"
            "--dry-run makes ZERO mutating calls (no repo delete/create, no DynamoDB\n"
            "writes): it lists the orgs that WOULD be overwritten and reports runtimes.\n"
        ),
    )
    # --- shared with seed_default_tenant (feed _resolve_runtime_config) ---
    parser.add_argument(
        "--group-id",
        default=None,
        help=(
            "Entra group object id for the default tenant's entra_group_ids, chained to "
            f"the seed step (default: the {seed.GROUP_ID_ENV_VAR} env var)."
        ),
    )
    parser.add_argument(
        "--account-id",
        default=None,
        help=(
            "AWS account id (default: the caller's account via STS; must be 12 "
            "digits — no hardcoded fallback)."
        ),
    )
    parser.add_argument(
        "--region",
        default=None,
        help=(
            "AWS region for the stack (default: aws_region in terraform.tfvars; no "
            "hardcoded fallback)."
        ),
    )
    parser.add_argument(
        "--infra-dir",
        default=None,
        help=(
            "Infrastructure folder holding terraform.tfvars — the source of truth for "
            f"runtime config, READ ONLY (default: {seed.DEFAULT_INFRA_DIR})."
        ),
    )
    parser.add_argument(
        "--agent-registry-id",
        default=None,
        help=(
            "Agent registry id. Nothing in the repo records it — the platform resolves "
            "registries by NAME at runtime — so this flag is the normal way to supply "
            "it; get the id from `aws agent-registry-control list-registries`."
        ),
    )
    parser.add_argument(
        "--mcp-registry-id",
        default=None,
        help=(
            "MCP registry id (same note as --agent-registry-id — pass it explicitly)."
        ),
    )
    parser.add_argument(
        "--tenants-table",
        default=None,
        help="Tenants DynamoDB table name override (default: derived by Terraform's rule).",
    )
    parser.add_argument(
        "--projects-table",
        default=None,
        help="Projects DynamoDB table name override (default: derived by Terraform's rule).",
    )
    # --- E25B-only ---
    parser.add_argument(
        "--connections-table",
        default=None,
        help=(
            "Connections DynamoDB table name override (default: derived as "
            "<PROJECT_NAME>-cp-<ENVIRONMENT>-<last-6-of-account>-connections)."
        ),
    )
    parser.add_argument(
        "--connections-secret-prefix",
        default=None,
        help=(
            "Secrets Manager name prefix for per-connection credentials (default: "
            "connections_secret_prefix in terraform.tfvars, else the backend default "
            f"'{DEFAULT_CONNECTIONS_SECRET_PREFIX}')."
        ),
    )
    parser.add_argument(
        "--runtime-module-bucket",
        default=None,
        help=(
            "S3 bucket holding the staged agentcore_runtime module zip (default: "
            "runtime_module_bucket / state_backend_bucket_name_prefix in "
            "terraform.tfvars, else the derived state bucket <name_prefix>-tf-state)."
        ),
    )
    parser.add_argument(
        "--runtime-module-key",
        default=None,
        help=(
            "S3 key of the staged runtime module zip (default: runtime_module_key in "
            f"terraform.tfvars, else '{DEFAULT_RUNTIME_MODULE_KEY}')."
        ),
    )
    parser.add_argument(
        "--agent-templates-dir",
        default=None,
        help=(
            "Base-template scaffold dir (unused by Step 2 — template_names=[] — but "
            f"required by the rollout service; default: {DEFAULT_AGENT_TEMPLATES_DIR})."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only: list orgs that WOULD be overwritten + report runtimes; NO mutating calls.",
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

    # Resolve the tenant's Entra group BEFORE importing settings/services — the script
    # must not require full env config just to tell the caller what's missing.
    group_id = args.group_id or os.environ.get(seed.GROUP_ID_ENV_VAR, "")
    if not group_id:
        logger.error(
            "No Entra group id for the default tenant — pass --group-id or set %s.",
            seed.GROUP_ID_ENV_VAR,
        )
        return 2

    try:
        config = _resolve_e25b_config(args)
        # The seed step's four services + this migration's two, both from the infra config.
        tenant_service, agent_service, mcp_server_service, project_service = (
            seed._build_services(config)
        )
        connection_service, rollout_service = _build_e25b_services(config)
        return run(
            group_id=group_id,
            dry_run=args.dry_run,
            tenant_service=tenant_service,
            agent_service=agent_service,
            mcp_server_service=mcp_server_service,
            project_service=project_service,
            connection_service=connection_service,
            rollout_service=rollout_service,
            account_id=(
                None
                if config["account_id"] == seed.ACCOUNT_PLACEHOLDER
                else config["account_id"]
            ),
            region=config["region"],
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean, actionable message
        logger.error("E25B migration failed: %s", exc)
        logger.error(
            "Runtime config comes from the INFRASTRUCTURE folder's terraform.tfvars "
            "(--infra-dir, default %s), not the backend's own settings. Check that: "
            "(1) AWS credentials are configured; (2) terraform.tfvars carries aws_region "
            "plus project_name/environment (for the derived table + bucket names), and "
            "the agent registry id is passed explicitly; (3) the connections table "
            "and the S3 runtime-module zip exist (the zip is staged by terraform apply, "
            "which MUST run BEFORE this script).",
            seed.DEFAULT_INFRA_DIR,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
