#!/usr/bin/env python
"""Stamp ONE ``owner`` role per EXISTING project (E27/T6) — the pre-E27 backfill.

E27 makes every project GOVERNED by per-project roles. Projects created BEFORE E27 hold
no role row at all, so they fall through to the design-§3 "ungoverned project" fallback
forever. This one-shot migration closes that gap: for each existing project it resolves
the recorded creator to an Entra principal and grants them ``owner``, so the project
becomes governed by a real, named principal instead of relying on the fallback.

It ORCHESTRATES existing services only (``ProjectService.list_projects``,
``ProjectRoleService.has_role_rows``/``grant``, ``GraphService.resolve_user_by_email``) —
no new data model, no new table, no route.

Per project, in order:

  1. **Already governed?** ``ProjectRoleService.has_role_rows(project_id)`` — the STRICT
     read, deliberately NOT the degrading ``list_for_project``. A migration that mistook
     an unreadable partition for "ungoverned" would grant into a project whose existing
     grants it cannot see (a second, unintended owner). So: has rows ⇒ SKIP (this is what
     makes a re-run grant nothing); read RAISES ⇒ governance is UNKNOWN ⇒ report as
     unresolved and SKIP — never grant on an unverified partition.
  2. **Resolve the creator.** ``Project.created_by`` is normally an EMAIL. Three shapes
     are handled without crashing:
       - blank ⇒ unresolved (reported as ``(blank)``) — there is nobody to grant to;
       - already a uuid-shaped Entra **oid** (some records store the oid) ⇒ used
         DIRECTLY, no Graph call, and called out separately in the summary so the
         operator can see it was not an email match;
       - an email ⇒ ``await graph.resolve_user_by_email(email)`` — an EXACT Graph
         ``$filter`` on ``mail``/``userPrincipalName`` (NOT the fuzzy
         ``search_principals``, which only ``$search``es ``displayName`` and so can
         never resolve an address). The result is still re-checked locally: only a
         ``user`` hit whose ``mail`` or ``userPrincipalName`` equals that email
         (case-insensitively) is accepted, so the script never depends on the
         collaborator having filtered correctly.
  3. **Grant or report.** Exactly ONE exact match ⇒ grant ``owner``. Zero matches,
     several matches, or only non-exact hits ⇒ **report and SKIP. The migration never
     guesses** which principal owns a project.

``--dry-run`` (the DEFAULT — a bare run writes nothing) makes zero mutating calls: it
still reads projects/roles and still resolves creators through Graph (a read), and prints
what it WOULD grant. ``--apply`` is required to write. Re-running after an apply grants
nothing: every project it touched now holds a role row and is skipped at step 1.

A single failing ``grant`` is logged and skipped (the remaining projects still run) and
makes the exit code nonzero; so does leaving any project unresolved on a real run,
because an unresolved project stays ungoverned and needs a manual
``POST /projects/{id}/roles``. A ``--dry-run`` is a PLAN and always exits 0.

Runtime config comes from the INFRASTRUCTURE folder (the source of truth), resolved with
``seed_default_tenant``'s own helpers so the precedence is identical: CLI flag > infra
``.env`` > ``terraform.tfvars`` (with tfvars winning for the PROJECT_NAME/ENVIRONMENT
name-prefix derivation inputs) > derivation (account via STS ``get-caller-identity``;
table name via Terraform's ``<PROJECT_NAME>-cp-<ENVIRONMENT>-<last-6-of-account>-projects``
rule) > a hard error naming the sources tried. There are NO hardcoded AWS account ids or
regions anywhere. Only the ``projects`` table is needed — project ROLES are a third
partition in that same table (E27/T1), so there is no second table and no new env var.

The Entra values the Graph client needs (tenant id, backend client id, login/Graph bases)
come from the backend ``settings`` — the same source ``api/routes/grants.py`` builds its
``GraphService`` from — because they are app config, not infrastructure outputs.

Run from the backend dir (PYTHONPATH=src is required — src/ is not a package):

    cd platform/control_plane/backend && \
        PYTHONPATH=src venv/bin/python scripts/migrate_e26_project_owners.py --apply

Offline-safe plan (the DEFAULT — reads only, writes nothing):

    cd platform/control_plane/backend && \
        PYTHONPATH=src venv/bin/python scripts/migrate_e26_project_owners.py

One project only (re-run the pass for a project you have since fixed up):

    … scripts/migrate_e26_project_owners.py --project <project-id> --apply

The infra files are read lazily inside ``main()`` (and boto3/STS lazily inside the seed's
resolvers), so importing this module never reads a file or triggers AWS setup;
``migrate()`` takes its three collaborators as parameters so tests drive it with mocks.
It is ``async`` because ``GraphService.resolve_user_by_email`` is — ``main()`` drives it with
``asyncio.run``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from typing import List, Optional

# The seed script lives beside this one in scripts/. Put the scripts dir on sys.path so
# ``import seed_default_tenant`` resolves both when this file is run directly AND when a
# test imports it as ``scripts.migrate_e26_project_owners`` (same shim as
# migrate_to_e25b.py). seed_default_tenant is stdlib-only at import top, so importing it
# is import-safe.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import seed_default_tenant as seed  # noqa: E402 — after the sys.path shim

logger = logging.getLogger("migrate_e26_project_owners")

# Project roles live in a THIRD partition of the EXISTING projects table (E27/T1) — the
# only table this migration needs.
PROJECTS_TABLE_SUFFIX = seed.PROJECTS_TABLE_SUFFIX

# ``granted_by`` on the written row. There is no calling Principal in a migration, so the
# provenance recorded is the migration itself — never a guessed operator identity.
GRANTED_BY = "migration:e26-project-owners"

# Reported in place of an empty ``created_by`` so a blank creator is visible in the
# summary instead of an empty string nobody notices.
BLANK_CREATOR = "(blank)"

# Canonical Entra object-id shape (8-4-4-4-12 hex). Deliberately strict: braced/urn UUID
# forms are NOT accepted, so anything unusual falls through to the email path rather than
# being written as a principal id.
_OID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)

# The Graph hit keys that carry an ACTUAL address — only one of these comparing equal is
# evidence that the hit IS the recorded creator (a displayName match never is).
_EXACT_MATCH_KEYS = ("mail", "userPrincipalName")


@dataclass
class MigrationResult:
    """What one pass did (or would do). ``granted``/``would_grant`` are mutually
    exclusive by mode: an ``--apply`` pass fills ``granted``, a dry run ``would_grant``."""

    total: int = 0                          # projects considered (after --project filter)
    granted: int = 0                        # owner rows actually written
    would_grant: int = 0                    # owner rows a dry run WOULD have written
    skipped_governed: int = 0               # already held a role row — untouched
    failures: int = 0                       # grant raised; project left ungoverned
    unresolved: List[str] = field(default_factory=list)         # creators we refused to guess
    used_oid_directly: List[str] = field(default_factory=list)   # created_by was already an oid


async def migrate(
    *,
    projects,
    roles,
    graph,
    apply: bool = False,
    project_id: Optional[str] = None,
) -> MigrationResult:
    """Stamp one ``owner`` per ungoverned project. Returns a :class:`MigrationResult`.

    Collaborators are parameters (built in ``main()`` for the real run; mocks in tests) so
    this function constructs no client and reads no config. ``apply=False`` (the default)
    writes nothing. ``project_id`` limits the pass to one project.
    """
    result = MigrationResult()

    for project in projects.list_projects():
        pid = getattr(project, "id", "")
        if project_id and pid != project_id:
            continue
        result.total += 1
        creator = (getattr(project, "created_by", "") or "").strip()

        # 1) Already governed? STRICT read — an unreadable partition is UNKNOWN, not empty.
        try:
            if roles.has_role_rows(pid):
                result.skipped_governed += 1
                logger.info("  %s: already governed — skipping.", pid)
                continue
        except Exception as exc:  # noqa: BLE001 — unverified governance ⇒ never grant
            result.unresolved.append(creator or BLANK_CREATOR)
            logger.error(
                "  %s: could not verify whether it is already governed (%s) — skipping "
                "WITHOUT granting; re-run once the role partition is readable.",
                pid,
                exc,
            )
            continue

        # 2) Resolve the creator to an Entra principal (or refuse to).
        resolved = await _resolve_creator(creator, graph, project_id=pid)
        if resolved is None:
            result.unresolved.append(creator or BLANK_CREATOR)
            continue
        principal_id, display, from_oid = resolved
        if from_oid:
            result.used_oid_directly.append(principal_id)

        # 3) Grant (or plan to).
        if not apply:
            result.would_grant += 1
            print(
                f"  [dry-run] would grant owner on project {pid!r} to "
                f"{principal_id} ({display})"
            )
            continue
        try:
            roles.grant(pid, _owner_grant(principal_id, display), granted_by=GRANTED_BY)
            result.granted += 1
            logger.info("  %s: granted owner to %s (%s).", pid, principal_id, display)
        except Exception as exc:  # noqa: BLE001 — log + continue, don't abort the migration
            result.failures += 1
            logger.error(
                "  %s: failed to grant owner to %s: %s — continuing (project stays "
                "ungoverned).",
                pid,
                principal_id,
                exc,
            )

    _print_summary(result, apply=apply)
    return result


async def _resolve_creator(creator: str, graph, *, project_id: str):
    """Resolve a ``created_by`` value to ``(principal_id, display, from_oid)``, or ``None``
    when it cannot be resolved to EXACTLY one principal (the caller reports + skips).

    Three shapes, none of which may crash: blank ⇒ ``None``; a uuid-shaped Entra oid ⇒
    used directly (no Graph call — the value already IS the principal id); anything else
    is treated as an email and looked up, accepting only an exact ``mail`` /
    ``userPrincipalName`` match. Zero, several, or only non-exact hits ⇒ ``None``.
    """
    if not creator:
        logger.error(
            "  %s: created_by is blank — no principal to grant owner to; grant one "
            "manually via POST /projects/%s/roles.",
            project_id,
            project_id,
        )
        return None

    if _OID_RE.match(creator):
        logger.info(
            "  %s: created_by is already an Entra object id — using it directly (no "
            "Graph lookup).",
            project_id,
        )
        return creator, creator, True

    try:
        found = await graph.resolve_user_by_email(creator)
    except Exception as exc:  # noqa: BLE001 — a Graph blip must not abort the migration
        logger.error(
            "  %s: Graph lookup for %r failed (%s) — skipping WITHOUT granting.",
            project_id,
            creator,
            exc,
        )
        return None

    # The resolver returns at most ONE user (or ``None``). A LIST is still tolerated so
    # ``_exact_matches`` stays the single place that decides what is acceptable — this
    # script must not depend on a collaborator narrowing the result for it.
    hits = [found] if isinstance(found, dict) else list(found or [])
    exact = _exact_matches(hits, creator)
    if len(exact) == 1:
        hit = exact[0]
        return hit.get("id"), hit.get("displayName") or creator, False

    if not exact:
        logger.error(
            "  %s: no Entra user has the exact address %r (%d unusable hit(s)) "
            "— reporting, NOT guessing.",
            project_id,
            creator,
            len(hits),
        )
    else:
        logger.error(
            "  %s: %d Entra users share the address %r — ambiguous, reporting, NOT "
            "guessing (ids: %s).",
            project_id,
            len(exact),
            creator,
            ", ".join(str(h.get("id")) for h in exact),
        )
    return None


def _exact_matches(hits, email: str) -> list:
    """The USER hits whose ``mail`` or ``userPrincipalName`` EQUALS ``email``
    (case-insensitive, trimmed), de-duplicated by principal id.

    This is belt-and-braces: the resolver is already an exact ``$filter``, but this
    script must not hand out ownership on a collaborator's promise. A hit with no id, a
    non-``user`` hit, or one whose addresses do not actually equal ``email`` is dropped.
    """
    wanted = email.strip().lower()
    matched: dict = {}
    for hit in hits:
        if not isinstance(hit, dict) or not hit.get("id"):
            continue
        # Only a USER may be granted here — the row is written with
        # principal_type="user", and a group oid in that row would grant OWNER to every
        # member while misreporting the blast radius. Groups DO carry ``mail`` in Entra,
        # so a mail-enabled group sharing an address with a user is a real shape. ``None``
        # is accepted so a hit that carries no ``type`` at all still works.
        if hit.get("type") not in (None, "user"):
            continue
        for key in _EXACT_MATCH_KEYS:
            value = hit.get(key)
            if isinstance(value, str) and value.strip().lower() == wanted:
                matched.setdefault(hit["id"], hit)
                break
    return list(matched.values())


def _owner_grant(principal_id: str, display: str):
    """Build the ``owner`` grant body. Imported lazily so importing this module stays
    stdlib-only (the seed's own convention)."""
    from models.project_role import ProjectRole, ProjectRoleCreate, role_name

    return ProjectRoleCreate(
        principal_id=principal_id,
        principal_type="user",
        principal_display=display,
        role=role_name(ProjectRole.OWNER),
    )


def _print_summary(result: MigrationResult, *, apply: bool) -> None:
    """Print granted / skipped-already-governed / unresolved (with the creators)."""
    verb = "granted" if apply else "would grant"
    count = result.granted if apply else result.would_grant
    print("")
    print("=== E27 project-owner backfill summary ===")
    print(f"  projects considered:      {result.total}")
    print(f"  {verb + ' owner:':<25} {count}")
    print(f"  skipped (already governed): {result.skipped_governed}")
    print(f"  unresolved (skipped):     {len(result.unresolved)}")
    if result.failures:
        print(f"  FAILED to grant:          {result.failures}")
    if result.used_oid_directly:
        print(
            "  NOTE: created_by was already an Entra object id (used directly, no "
            f"email match) for {len(result.used_oid_directly)} project(s): "
            + ", ".join(result.used_oid_directly)
        )
    if result.unresolved:
        print("")
        print(
            "  ACTION REQUIRED — these creators could not be resolved to exactly one "
            "Entra user, so their projects stay UNGOVERNED. Grant an owner manually "
            "(POST /projects/{id}/roles):"
        )
        for creator in result.unresolved:
            print(f"    - {creator}")
    if not apply:
        print("")
        print("  [dry-run] nothing was written. Re-run with --apply to grant.")


def _resolve_projects_table(args) -> dict:
    """Resolve the region + projects table this migration needs, using the seed's own
    helpers so the precedence is IDENTICAL (CLI > infra ``.env`` > ``terraform.tfvars``,
    with tfvars winning for the PROJECT_NAME/ENVIRONMENT derivation inputs > derivation
    via STS + Terraform's naming rule > hard error naming the sources tried).

    Only the projects table is resolved — role rows are a third partition IN it (E27/T1).
    The seed's whole-config resolver is deliberately not reused: it also REQUIRES an agent
    registry id, which this migration never touches.
    """
    from pathlib import Path

    infra_dir = Path(args.infra_dir) if args.infra_dir else seed.DEFAULT_INFRA_DIR
    infra = seed._load_infra_config(infra_dir)

    region = args.region or infra.get("AWS_REGION")
    if not region:
        raise RuntimeError(
            f"Could not resolve the AWS region — tried --region, AWS_REGION in "
            f"{infra_dir / '.env'}, aws_region in {infra_dir / 'terraform.tfvars'}."
        )

    # dry_run=False: this migration READS DynamoDB in both modes, so it needs the REAL
    # account (the placeholder would derive a table name that does not exist).
    account_id = seed._resolve_account_id(args.account_id, dry_run=False)
    projects_table = args.projects_table or seed._derive_table_name(
        infra, account_id, PROJECTS_TABLE_SUFFIX, infra_dir=infra_dir
    )
    return {"region": region, "projects_table": projects_table}


def _build_collaborators(config: dict):
    """Construct the three real collaborators (lazy — ``main()`` only, so importing this
    module triggers no boto3/httpx/service import).

    ``ProjectService`` only needs its persistence surface (``list_projects``), so its
    orchestration collaborators are ``None`` — the same shortcut ``seed_default_tenant``
    takes. ``ProjectRoleService`` points at the SAME table (roles are a partition in it).
    ``GraphService`` is built from the backend ``settings`` exactly as
    ``api/routes/grants.get_graph_service`` does: those Entra values are app config, not
    infrastructure outputs.
    """
    from core.config import settings
    from services.graph_service import GraphService
    from services.project_role_service import ProjectRoleService
    from services.project_service import ProjectService

    projects = ProjectService(
        table_name=config["projects_table"],
        registry=None,
        identity=None,
        connection_service=None,
        region=config["region"],
    )
    roles = ProjectRoleService(
        table_name=config["projects_table"], region=config["region"]
    )
    graph = GraphService(
        tenant_id=settings.ENTRA_TENANT_ID,
        backend_client_id=settings.ENTRA_BACKEND_CLIENT_ID,
        login_base=settings.ENTRA_LOGIN_BASE,
        graph_base=settings.GRAPH_API_BASE,
        audience_prefix=settings.AGENT_APP_AUDIENCE_PREFIX,
    )
    return projects, roles, graph


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Stamp ONE 'owner' project role per EXISTING project (E27/T6), resolving "
            "each project's recorded creator to an Entra user. Projects that already "
            "hold a role row are skipped, so a re-run grants nothing. A creator that "
            "cannot be resolved to exactly one user is REPORTED, never guessed."
        ),
        epilog=(
            "--dry-run is the DEFAULT: a bare run writes nothing, it only reads and\n"
            "prints what it WOULD grant. Pass --apply to write.\n"
            "\n"
            "Exit code: 0 on a clean run (and on ANY dry run — a dry run is a plan);\n"
            "1 when a grant failed OR any project was left unresolved on a real run\n"
            "(an unresolved project stays ungoverned and needs a manual role grant).\n"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Read + report only, writing nothing. This is the DEFAULT — the flag exists "
            "so the intent can be stated explicitly in a runbook."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the owner rows (required — without it nothing is written).",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Limit the pass to ONE project id (default: every project).",
    )
    parser.add_argument(
        "--region",
        default=None,
        help=(
            "AWS region for the stack (default: AWS_REGION in infrastructure/.env, else "
            "aws_region in terraform.tfvars; no hardcoded fallback)."
        ),
    )
    parser.add_argument(
        "--account-id",
        default=None,
        help=(
            "AWS account id used to derive the table name (default: the caller's "
            "account via STS get-caller-identity; must be 12 digits — no hardcoded "
            "fallback)."
        ),
    )
    parser.add_argument(
        "--infra-dir",
        default=None,
        help=(
            "Infrastructure folder holding .env and terraform.tfvars — the source of "
            f"truth for runtime config (default: {seed.DEFAULT_INFRA_DIR})."
        ),
    )
    parser.add_argument(
        "--projects-table",
        default=None,
        help=(
            "Projects DynamoDB table name override — role rows live in this SAME table "
            "(default: derived as <PROJECT_NAME>-cp-<ENVIRONMENT>-<last-6-of-account>-"
            "projects, Terraform's own naming rule)."
        ),
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

    # --dry-run and --apply together is an ambiguous instruction about WRITING, so refuse
    # rather than silently picking one.
    if args.apply and args.dry_run:
        logger.error("--apply and --dry-run are mutually exclusive — pass one.")
        return 2

    try:
        config = _resolve_projects_table(args)
        projects, roles, graph = _build_collaborators(config)
        result = asyncio.run(
            migrate(
                projects=projects,
                roles=roles,
                graph=graph,
                apply=args.apply,
                project_id=args.project,
            )
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean, actionable message
        logger.error("E27 project-owner backfill failed: %s", exc)
        logger.error(
            "Runtime config comes from the INFRASTRUCTURE folder (--infra-dir, default "
            "%s), not the backend .env. Check that: (1) AWS credentials are configured "
            "(the account is derived via STS); (2) infrastructure/.env or "
            "terraform.tfvars carry the region and PROJECT_NAME/ENVIRONMENT for the "
            "derived table name — or pass --region/--account-id/--projects-table; "
            "(3) the backend Entra settings (ENTRA_TENANT_ID, ENTRA_BACKEND_CLIENT_ID) "
            "are set and the backend Graph client secret is reachable, since creators "
            "are resolved through Microsoft Graph.",
            seed.DEFAULT_INFRA_DIR,
        )
        return 1

    if not args.apply:
        return 0  # a dry run is a PLAN — never a nonzero verdict
    return 1 if (result.failures or result.unresolved) else 0


if __name__ == "__main__":
    sys.exit(main())
