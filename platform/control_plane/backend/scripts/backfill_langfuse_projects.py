#!/usr/bin/env python
"""Backfill per-agent Langfuse projects for pre-E26 agents (E26, Task 12).

Langfuse per-agent projects are auto-provisioned at agent REGISTRATION (E26/T4), so
agents registered BEFORE E26 carry no ``langfuse_project_id`` on their registry
envelope and their traces have nowhere to land. This is the operator BACKFILL: it
lists the agent registry, selects every agent MISSING ``langfuse_project_id`` and calls
``LangfuseProvisioningService.provision_agent_project`` for each — the same C2 code path
registration uses, already idempotent (it short-circuits on an envelope that carries the
project id, and re-mints only when the Secrets Manager secret is gone).

What it prints per provisioned agent: the Langfuse **project id** and the Secrets Manager
secret **NAME**. Operators need that name for ``LANGFUSE_SECRET_NAME`` when deploying the
reference agents (``applications/acme_*_agent/deploy.sh`` reads ``LANGFUSE_HOST`` +
``LANGFUSE_SECRET_NAME`` from env). The key VALUES live ONLY in Secrets Manager and are
**never** printed or logged here — that is a hard constraint of this script.

Safety envelope (mirrors ``seed_default_tenant.py``):
  - ``--dry-run`` prints the plan (which agents WOULD be backfilled: id + name +
    current state) and calls NEITHER Langfuse NOR Secrets Manager. Without it the run
    is live and mutates the registry envelopes.
  - ``--agent-id`` (repeatable and/or comma-separated) restricts the backfill to a
    single agent or subset. An id that is NOT in the registry (i.e. a typo) is counted
    as a FAILURE and the run exits non-zero — a mistyped id never yields a green run
    that provisioned nothing.
  - Resilient per agent: one agent's failure is logged and the run CONTINUES. The final
    summary prints ``provisioned / skipped / failed`` counts plus the failed ids, and the
    exit code is 1 when anything failed — but only AFTER every agent was attempted.
  - ``LANGFUSE_HOST`` must be configured; when it isn't, the script exits with a clear
    message instead of half-running.

Runtime config comes from the INFRASTRUCTURE folder, not the backend's own settings (the
infra folder is the source of truth). Resolution precedence, per value:

  1. explicit CLI flag (``--region``, ``--agent-registry-id``, ``--langfuse-host``,
     ``--langfuse-admin-secret-name``, ``--project-name``), then
  2. the SCALAR assignments of ``terraform.tfvars`` under ``--infra-dir`` (default:
     ``platform/control_plane/infrastructure``), normalized to UPPER_SNAKE
     (``aws_region`` → ``AWS_REGION``). That file is what Terraform itself read, so its
     ``project_name``/``environment`` are what actually named the deployed resources,
     then
  3. the ambient environment (``AWS_REGION``, ``LANGFUSE_HOST`` /
     ``LANGFUSE_ADMIN_SECRET_NAME`` — what the ECS task itself is given) and derivation:
     the Langfuse admin secret name via Terraform's rule
     ``<PROJECT_NAME>-cp-<ENVIRONMENT>-<last-6-of-account>-langfuse-secrets`` (root
     ``main.tf`` ``name_prefix`` + the langfuse module's ``-langfuse``/``-secrets``
     suffixes), with the account from STS ``get-caller-identity`` — there is deliberately
     NO hardcoded account, and ``us-east-1`` is only the last-resort region default (repo
     convention), then
  4. a hard error naming the sources tried, for the two required values: the agent
     registry id and ``LANGFUSE_HOST``.

There is deliberately NO ``infrastructure/.env`` leg (removed in E34/T10). That file was
a phantom config surface: nothing in the repo writes one, no setup step creates one, and
it merely shadowed tfvars keys under a per-key precedence no reader could see. The honest
sources for the two Langfuse values are ``terraform output langfuse_host`` and
``terraform output langfuse_secret_name`` — pass them as flags or export them.

Run from the backend dir (PYTHONPATH=src is required — src/ is not a package):

    cd platform/control_plane/backend && \
        PYTHONPATH=src venv/bin/python scripts/backfill_langfuse_projects.py --dry-run

    cd platform/control_plane/backend && \
        PYTHONPATH=src venv/bin/python scripts/backfill_langfuse_projects.py

The infra files are read lazily inside ``main()`` — and boto3/STS + the services lazily
inside their resolvers/builders — so importing this module never reads a file or triggers
AWS setup; ``run()`` takes the registry + provisioner as parameters so tests drive it with
in-memory fakes.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path

logger = logging.getLogger("backfill_langfuse_projects")

# --- infra config (source of truth) --------------------------------------------
# Runtime config is read from the INFRASTRUCTURE folder's terraform.tfvars, never the
# backend's own settings. The script lives at backend/scripts/, so the infra dir is the
# sibling of backend/ (this is a path computation only — nothing is read at import).
DEFAULT_INFRA_DIR = Path(__file__).resolve().parents[2] / "infrastructure"

# The langfuse module's secret is named "${name}-secrets" where name is
# "${name_prefix}-langfuse" (root main.tf module "langfuse"), and
# name_prefix = "${project_name}-cp-${environment}-${substr(account_id, -6, 6)}".
LANGFUSE_SECRET_SUFFIX = "langfuse-secrets"

# Repo convention: us-east-1 is the last-resort region default (never an account).
DEFAULT_REGION = "us-east-1"

# The AGP project token — prefixes the per-agent Langfuse project name
# (``<agp-project>-<agent>``), matching the provisioner's default.
DEFAULT_AGP_PROJECT_NAME = "agp"

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

# Third-party loggers that must NEVER reach DEBUG, not even under ``--verbose``:
# botocore's wire logging (``botocore.endpoint``/``botocore.parsers``) dumps the full
# request/response BODY of every AWS call at DEBUG — and for the provisioner's
# ``CreateSecret``/``PutSecretValue`` that body contains the Langfuse secret key VALUE
# (``"secret_key": "sk-lf-…"``). httpx/httpcore do the same for the Langfuse tRPC calls.
# Printing or logging that value is a hard constraint violation for this script, so
# ``--verbose`` raises verbosity for THIS module's logger only (see
# :func:`_configure_logging`) and these stay pinned at INFO.
WIRE_LOGGERS = ("boto3", "botocore", "urllib3", "httpx", "httpcore", "s3transfer")


def _configure_logging(verbose: bool) -> None:
    """Configure logging so ``--verbose`` is safe.

    The root logger is deliberately left at INFO: setting it to DEBUG would enable the
    AWS SDK's (and httpx's) wire logging, which writes the Langfuse secret key VALUE to
    stderr. Verbosity is raised on this script's own logger, and :data:`WIRE_LOGGERS` is
    explicitly pinned to INFO as a belt-and-braces guard."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    for name in WIRE_LOGGERS:
        logging.getLogger(name).setLevel(logging.INFO)


def _parse_tfvars_file(path: Path) -> dict:
    """Parse the SCALAR assignments of a ``terraform.tfvars`` (``key = "value"`` /
    numbers / bools), normalizing keys to UPPER_SNAKE (``aws_region`` → ``AWS_REGION``).
    Non-scalar values (``tags = { … }`` blocks, lists) are skipped gracefully — including
    multi-line blocks — as are comments and anything else that doesn't parse."""
    values: dict = {}
    block_depth = 0  # >0 while inside a skipped non-scalar block (e.g. ``tags = {``)
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if block_depth > 0:
            block_depth += (
                line.count("{") - line.count("}") + line.count("[") - line.count("]")
            )
            continue
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        key, sep, rhs = line.partition("=")
        key = key.strip()
        rhs = rhs.strip()
        if not sep or not _IDENT_RE.match(key) or not rhs:
            continue
        if rhs[0] in "{[":
            block_depth = (
                rhs.count("{") - rhs.count("}") + rhs.count("[") - rhs.count("]")
            )
            continue
        if rhs[0] == '"':
            end = rhs.find('"', 1)
            if end == -1:
                continue  # unterminated string — can't parse, skip
            value = rhs[1:end]
        else:
            value = rhs.split("#", 1)[0].strip()  # drop inline comment
        if value:
            values[key.upper()] = value
    return values


def _load_infra_config(infra_dir) -> dict:
    """Read the infra folder's ``terraform.tfvars`` scalars into one UPPER_SNAKE dict.

    ``terraform.tfvars`` is the ONLY config file consulted: it is the file Terraform
    itself read, so its ``project_name``/``environment`` are what named the deployed
    resources. An ``infrastructure/.env`` is deliberately NOT read (E34/T10) — anything
    it could carry belongs on the command line or in the ambient environment instead.

    A missing file simply contributes nothing. This file is READ-ONLY here."""
    tfvars_path = Path(infra_dir) / "terraform.tfvars"
    return _parse_tfvars_file(tfvars_path) if tfvars_path.is_file() else {}


def _resolve_account_id():
    """Caller's AWS account via STS ``get-caller-identity``, or ``None`` when it can't
    be reached (the caller then has to pass ``--langfuse-admin-secret-name``). boto3 is
    imported lazily so importing this module stays stdlib-only. There is deliberately NO
    hardcoded account."""
    try:
        import boto3  # lazy — module import must not require boto3 or any env

        return boto3.client("sts").get_caller_identity()["Account"]
    except Exception as exc:  # noqa: BLE001 — any failure means "cannot derive"
        logger.warning("STS unreachable (%s) — cannot derive the account id.", exc)
        return None


def _derive_langfuse_secret_name(infra: dict, account_id) -> str:
    """Rebuild the Langfuse secret name Terraform derives:
    ``<PROJECT_NAME>-cp-<ENVIRONMENT>-<last-6-of-account>-langfuse-secrets``. Returns
    ``""`` when the inputs aren't resolvable — the admin secret is only needed by the
    provisioner's list-and-match fallback, so an empty value is a soft miss, not fatal."""
    project_name = infra.get("PROJECT_NAME")
    environment = infra.get("ENVIRONMENT")
    if not project_name or not environment or not account_id:
        return ""
    return f"{project_name}-cp-{environment}-{account_id[-6:]}-{LANGFUSE_SECRET_SUFFIX}"


def _resolve_runtime_config(args) -> dict:
    """Resolve every runtime value the backfill needs, with the precedence
    CLI flag > ``terraform.tfvars`` scalars > ambient environment / derivation
    (STS account; Terraform's naming rule) > hard error naming the sources tried.

    Required: the agent registry id and ``LANGFUSE_HOST``. Soft: the Langfuse admin
    secret name (only the provisioner's fallback path needs it); region defaults to
    :data:`DEFAULT_REGION` per repo convention."""
    infra_dir = Path(args.infra_dir) if args.infra_dir else DEFAULT_INFRA_DIR
    infra = _load_infra_config(infra_dir)
    tfvars_path = infra_dir / "terraform.tfvars"

    region = (
        args.region
        or infra.get("AWS_REGION")
        or os.environ.get("AWS_REGION")
        or DEFAULT_REGION
    )

    agent_registry_id = args.agent_registry_id or infra.get("AGENT_REGISTRY_ID")
    if not agent_registry_id:
        raise RuntimeError(
            f"Could not resolve the agent registry id — tried --agent-registry-id and "
            f"agent_registry_id in {tfvars_path}. NOTE: Terraform does not populate that "
            "file — the platform resolves the registry by NAME at runtime, so no id is "
            "written down. Get the id from AWS and pass it explicitly: `aws "
            "agent-registry-control list-registries --query "
            "\"registries[?name=='agp-agents'].registryId\" --output text`."
        )

    langfuse_host = (
        args.langfuse_host
        or infra.get("LANGFUSE_HOST")
        or os.environ.get("LANGFUSE_HOST")
        or ""
    )
    if not langfuse_host:
        raise RuntimeError(
            "Langfuse is not configured — no LANGFUSE_HOST. Tried --langfuse-host, "
            f"langfuse_host in {tfvars_path}, and the LANGFUSE_HOST env var. Get it from "
            f"`terraform output langfuse_host` in {infra_dir}. Refusing to half-run the "
            "backfill."
        )

    admin_secret_name = (
        args.langfuse_admin_secret_name
        or infra.get("LANGFUSE_ADMIN_SECRET_NAME")
        or os.environ.get("LANGFUSE_ADMIN_SECRET_NAME")
        or _derive_langfuse_secret_name(infra, _resolve_account_id())
    )
    if not admin_secret_name:
        logger.warning(
            "Could not resolve the Langfuse admin secret name (tried "
            "--langfuse-admin-secret-name, langfuse_admin_secret_name in "
            "terraform.tfvars, the LANGFUSE_ADMIN_SECRET_NAME env var, and the derived "
            "<project>-cp-<env>-<last-6-of-account>-%s). "
            "The provisioner only needs it for its list-and-match fallback; the "
            "primary create path still works.",
            LANGFUSE_SECRET_SUFFIX,
        )

    return {
        "region": region,
        "agent_registry_id": agent_registry_id,
        "langfuse_host": langfuse_host,
        "langfuse_admin_secret_name": admin_secret_name,
        "agp_project_name": args.project_name or DEFAULT_AGP_PROJECT_NAME,
    }


def _selected_agent_ids(args):
    """Flatten ``--agent-id`` (repeatable AND/OR comma-separated) into an ordered,
    de-duplicated list. ``None`` (the default) means "every agent in the registry"."""
    if not args.agent_id:
        return None
    ids: list = []
    for raw in args.agent_id:
        for piece in raw.split(","):
            piece = piece.strip()
            if piece and piece not in ids:
                ids.append(piece)
    return ids or None


def _needs_backfill(agent) -> bool:
    """An agent needs backfilling iff its envelope carries no ``langfuse_project_id``."""
    return not getattr(agent, "langfuse_project_id", None)


def run(*, registry, provisioner, dry_run: bool, agent_ids=None) -> int:
    """Backfill the Langfuse project for every agent missing one. Returns an exit code.

    The registry + provisioner are parameters (built in ``main()`` for the real run;
    in-memory fakes in tests) so this function performs no service construction and no
    config access. NOTHING here prints a secret VALUE — only the Secrets Manager secret
    NAME and the Langfuse project id, which are the non-secret join operators need.
    """
    agents = registry.list()
    # An unknown --agent-id is almost always a TYPO: it must never yield a green run
    # that did nothing, so unknown ids are counted as FAILURES (exit code 1) and named
    # in the summary — in dry-run too.
    unknown_ids: list = []
    if agent_ids is not None:
        wanted = set(agent_ids)
        selected = [a for a in agents if getattr(a, "id", None) in wanted]
        unknown_ids = sorted(wanted - {getattr(a, "id", None) for a in selected})
        for agent_id in unknown_ids:
            logger.error("Agent %r not found in the registry.", agent_id)
    else:
        selected = agents

    todo = [a for a in selected if _needs_backfill(a)]
    skipped = len(selected) - len(todo)

    # --- dry run: print the plan, touch NOTHING (no Langfuse, no Secrets Manager) ---
    if dry_run:
        print(
            f"dry run (no writes): {len(todo)} agent(s) WOULD be backfilled, "
            f"{skipped} already provisioned"
        )
        for agent in todo:
            print(
                f"  [dry-run] would provision agent {agent.id} "
                f"({getattr(agent, 'name', '?')}) — "
                f"langfuse_project_id=None, "
                f"langfuse_key_secret_name="
                f"{getattr(agent, 'langfuse_key_secret_name', None)}"
            )
        if unknown_ids:
            print(f"  unknown agent id(s) — not in the registry: {', '.join(unknown_ids)}")
            return 1
        return 0

    # --- live run: per-agent, failures logged and the run continues -----------------
    provisioned = 0
    failed_ids: list = list(unknown_ids)
    for agent in todo:
        try:
            result = provisioner.provision_agent_project(agent)
            provisioned += 1
            # NON-SECRET output only: the project id + the Secrets Manager NAME
            # (operators pass that name as LANGFUSE_SECRET_NAME when deploying agents).
            print(
                f"  provisioned agent {agent.id} ({getattr(agent, 'name', '?')}) -> "
                f"langfuse_project_id={result.get('project_id')} "
                f"secret_name={result.get('secret_name')}"
            )
        except Exception as exc:  # noqa: BLE001 — log + continue; never abort the run
            failed_ids.append(agent.id)
            logger.error(
                "  failed to provision agent %s (%s): %s — continuing",
                agent.id,
                getattr(agent, "name", "?"),
                exc,
            )

    summary = (
        f"provisioned={provisioned} skipped={skipped} failed={len(failed_ids)} "
        f"(selected={len(selected)})"
    )
    if failed_ids:
        print(f"backfill FINISHED WITH FAILURES: {summary}")
        print(f"  failed agent ids: {', '.join(failed_ids)}")
        return 1
    print(f"backfill complete: {summary}")
    return 0


def _build_services(config: dict):
    """Construct the registry + provisioner from the resolved infra config (lazy —
    main()-only; NEVER from the backend's own settings — the infrastructure folder is the
    source of truth). The registry is injected into the provisioner so it persists the
    C1 join (``langfuse_project_id`` + ``langfuse_key_secret_name``) onto the envelope,
    exactly like the registration hook in ``routes/agents.py`` does."""
    from services.agent_registry_service import AgentRegistryService
    from services.langfuse_provisioning import LangfuseProvisioningService

    registry = AgentRegistryService(
        registry_id=config["agent_registry_id"],
        region=config["region"],
    )
    provisioner = LangfuseProvisioningService(
        langfuse_host=config["langfuse_host"],
        langfuse_secret_name=config["langfuse_admin_secret_name"],
        region=config["region"],
        registry=registry,
        agp_project_name=config["agp_project_name"],
    )
    return registry, provisioner


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Backfill the per-agent Langfuse project + key for every registry agent "
            "that has no langfuse_project_id (E26/T12). Idempotent — safe to re-run."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print which agents WOULD be backfilled (id + name + current state) "
            "without calling Langfuse or Secrets Manager."
        ),
    )
    parser.add_argument(
        "--agent-id",
        action="append",
        default=None,
        help=(
            "Restrict the backfill to this agent id. Repeatable and/or "
            "comma-separated (default: every agent in the registry). An id that is "
            "not in the registry counts as a failure (non-zero exit)."
        ),
    )
    parser.add_argument(
        "--region",
        default=None,
        help=(
            "AWS region for the registry + Secrets Manager (default: aws_region in "
            f"terraform.tfvars, else the AWS_REGION env var, else {DEFAULT_REGION})."
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
            "Agent registry id (falls back to agent_registry_id in terraform.tfvars, "
            "which Terraform does not populate — so this flag is the normal way to "
            "supply it; get the id from `aws agent-registry-control list-registries`)."
        ),
    )
    parser.add_argument(
        "--langfuse-host",
        default=None,
        help=(
            "Langfuse base URL (default: langfuse_host in terraform.tfvars, else the "
            "LANGFUSE_HOST env var; `terraform output langfuse_host` prints it). "
            "REQUIRED — the backfill refuses to run without it."
        ),
    )
    parser.add_argument(
        "--langfuse-admin-secret-name",
        default=None,
        help=(
            "Secrets Manager name of the Langfuse seed-org/admin credentials (default: "
            "langfuse_admin_secret_name in terraform.tfvars, else the "
            "LANGFUSE_ADMIN_SECRET_NAME env var, else derived as "
            "<PROJECT_NAME>-cp-<ENVIRONMENT>-<last-6-of-account>-"
            f"{LANGFUSE_SECRET_SUFFIX}; `terraform output langfuse_secret_name` prints "
            "it)."
        ),
    )
    parser.add_argument(
        "--project-name",
        default=None,
        help=(
            "AGP project token prefixing each per-agent Langfuse project name "
            f"(<token>-<agent>; default: {DEFAULT_AGP_PROJECT_NAME})."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Enable DEBUG logging for THIS script only — the AWS SDK / HTTP wire "
            "loggers stay at INFO so no secret value can ever reach stderr."
        ),
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    _configure_logging(args.verbose)

    # Resolve runtime config from the infra folder (CLI > terraform.tfvars > ambient
    # env/derivation) and build the services from it — all here (not at module top) so
    # importing this module never reads a file or triggers any AWS setup.
    try:
        config = _resolve_runtime_config(args)
        registry, provisioner = _build_services(config)
        return run(
            registry=registry,
            provisioner=provisioner,
            dry_run=args.dry_run,
            agent_ids=_selected_agent_ids(args),
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean, actionable message
        logger.error("Backfill failed: %s", exc)
        logger.error(
            "Runtime config comes from the INFRASTRUCTURE folder's terraform.tfvars "
            "(--infra-dir, default %s), not the backend's own settings. Check that: "
            "(1) AWS credentials are configured; (2) the region and LANGFUSE_HOST "
            "resolve (terraform.tfvars, the ambient environment, or `terraform output "
            "langfuse_host`) and a registry id is supplied — pass --region / "
            "--agent-registry-id / --langfuse-host, and note that Terraform writes a "
            "registry id nowhere, so --agent-registry-id is normally required; (3) the "
            "registry and the Langfuse stack are reachable from the resolved region.",
            DEFAULT_INFRA_DIR,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
