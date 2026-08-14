#!/usr/bin/env python
"""Purge the leftover per-build scratch clone-token secrets from Secrets Manager (E36/T9).

Why this script exists
----------------------
`services/runtime_build_service.py::_write_scratch_token` writes ONE Secrets Manager secret per
runtime build — `<RUNTIME_BUILD_TOKEN_PREFIX><image_tag>`, body `{"token": <ready bearer token>}`,
tagged `managed_by=agp` + `purpose=runtime-build-token`. Until E36/T9 nothing ever deleted them, so
they accumulated one per build, forever, each billing monthly and each still holding a live git
credential (a verbatim PAT, or a ~1h GitHub App installation token).

T9 closed the leak going forward with two code legs:

  1. the buildspec's `post_build` force-deletes `$GIT_SECRET_ARN` (covers every build that STARTED,
     on success and on failure alike);
  2. `RuntimeBuildService._delete_secret_best_effort` covers the case (1) cannot — the
     secret is written BEFORE StartBuild, so a StartBuild that faults leaves a secret behind with
     no build to run post_build.

This script is the third leg: a one-off, operator-run reclaim of the ALREADY-ACCUMULATED backlog,
and the backstop for anything the other two miss (a build killed before post_build ran, a WARNed
delete). It is safe to re-run — deleting an already-deleted secret is a no-op here.

How a secret is selected (two independent criteria, OR-ed)
---------------------------------------------------------
  * **tags** — `managed_by=agp` AND `purpose=runtime-build-token`, both, exactly;
  * **name prefix** — `RUNTIME_BUILD_TOKEN_PREFIX` (`--prefix`).

The name-prefix leg is NOT redundant. `_write_scratch_token` tags only on the `create_secret` path;
when the name already exists (a retry of the same image tag) it falls back to `put_secret_value`,
which does not re-tag. A secret first created before the tags were introduced, and since rewritten
by that retry path, therefore carries NO tags and is only reachable by name.

Matching is done CLIENT-SIDE on a full `list_secrets` walk rather than through `Filters`, on
purpose: the two criteria cannot be expressed as one server-side filter (`tag-key`/`tag-value`
filters are independent and do not assert a PAIR), and a strict local match is auditable — a
secret that satisfies neither criterion can never be selected, whatever the API returns. An
operator one-off pays a few extra `ListSecrets` pages for that.

`--min-age-hours` (default 24) is a safety guard, not a nicety: a sweep run while a build is in
flight would delete the token that build has not cloned with yet, failing a live deploy. 24 h is
comfortably longer than any build.

DRY RUN IS THE DEFAULT. Nothing is deleted unless `--delete` is passed.

Usage (from the backend dir):

    cd platform/control_plane/backend && \
        venv/bin/python scripts/sweep_runtime_build_tokens.py                  # plan only
    cd platform/control_plane/backend && \
        venv/bin/python scripts/sweep_runtime_build_tokens.py --delete         # reclaim

Exit status is 0 when the sweep completed (including a dry run, and including "nothing matched"),
1 when the listing failed or any individual delete failed — the sweep does not abort on the first
delete failure, it reports every one and then exits non-zero.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

logger = logging.getLogger("sweep_runtime_build_tokens")

DEFAULT_REGION = "us-east-1"
# Mirrors the default of `RUNTIME_BUILD_TOKEN_PREFIX` (src/core/config.py) — duplicated as a
# literal rather than imported so this script needs no PYTHONPATH=src and constructs no Settings
# object (which would demand the backend's whole environment just to read one default).
DEFAULT_PREFIX = "agp-dev/runtime-build-token/"
DEFAULT_MIN_AGE_HOURS = 24.0

# Both required, exactly — the tag pair `_write_scratch_token` applies on the create path.
REQUIRED_TAGS = {"managed_by": "agp", "purpose": "runtime-build-token"}


def _tags_of(secret: dict) -> dict:
    """`{Key: Value}` for one `list_secrets` entry (an untagged secret has no `Tags` key)."""
    return {t["Key"]: t["Value"] for t in secret.get("Tags") or []}


def match_reason(secret: dict, prefix: str) -> str:
    """Why this secret is a scratch clone token — `""` when it is not one.

    Returns a human reason ("tags", "name prefix", or both) so the printed plan says WHY each
    secret was selected; an operator reviewing a dry run can then spot a wrong `--prefix`.
    """
    reasons = []
    if all(_tags_of(secret).get(k) == v for k, v in REQUIRED_TAGS.items()):
        reasons.append("tags")
    # The tag-less half: `put_secret_value` (the same-image-tag retry path) does not re-tag.
    if prefix and secret.get("Name", "").startswith(prefix):
        reasons.append("name prefix")
    return " + ".join(reasons)


def age_hours(secret: dict, now: datetime) -> float:
    """Hours since the secret was last WRITTEN (not merely created).

    `LastChangedDate` is the conservative signal: a `put_secret_value` retry refreshes the token
    while leaving `CreatedDate` at the original creation, so judging by creation alone could call a
    seconds-old token "3 days old" and delete it out from under a running build. The newest of the
    two dates wins; a secret carrying neither is treated as age 0 (i.e. too young to touch).
    """
    dates = [secret[k] for k in ("CreatedDate", "LastChangedDate") if secret.get(k)]
    if not dates:
        return 0.0
    return (now - max(dates)).total_seconds() / 3600.0


def list_scratch_secrets(sm, *, prefix: str) -> list:
    """Every scratch clone-token secret in the region, as `(secret, reason)` pairs."""
    out = []
    for page in sm.get_paginator("list_secrets").paginate():
        for secret in page.get("SecretList", []):
            # Already scheduled for deletion by an earlier sweep — nothing left to reclaim.
            if secret.get("DeletedDate"):
                continue
            reason = match_reason(secret, prefix)
            if reason:
                out.append((secret, reason))
    return out


def run(*, sm, prefix: str, min_age_hours: float, delete: bool, now=None) -> int:
    """Plan (and optionally perform) the sweep. Returns the process exit status."""
    now = now or datetime.now(timezone.utc)
    candidates = list_scratch_secrets(sm, prefix=prefix)

    logger.info(
        "%d scratch clone-token secret(s) found (tags %s, or name prefix %r)",
        len(candidates),
        ", ".join(f"{k}={v}" for k, v in REQUIRED_TAGS.items()),
        prefix,
    )

    failures = 0
    deleted = 0
    skipped = 0
    for secret, reason in candidates:
        name, arn = secret["Name"], secret["ARN"]
        age = age_hours(secret, now)
        if age < min_age_hours:
            # Never race a live build (see the module docstring).
            logger.info(
                "  [skip] %s — last written %.1fh ago, under the %.1fh floor (a build may still "
                "be using it)",
                name,
                age,
                min_age_hours,
            )
            skipped += 1
            continue
        if not delete:
            logger.info("  [dry-run] would delete %s (%.1fh old, matched by %s)", name, age, reason)
            continue
        try:
            # Force delete: the default 30-day recovery window would leave the git token readable
            # (and billing) for that whole window, which is not a reclaim.
            sm.delete_secret(SecretId=arn, ForceDeleteWithoutRecovery=True)
        except Exception as exc:  # noqa: BLE001 — one bad secret must not abort the sweep
            logger.error("  [FAILED] %s — %s", name, exc)
            failures += 1
            continue
        logger.info("  [deleted] %s (%.1fh old, matched by %s)", name, age, reason)
        deleted += 1

    if delete:
        logger.info("Done: %d deleted, %d too young, %d failed", deleted, skipped, failures)
    else:
        logger.info(
            "Dry run: %d would be deleted, %d too young. Re-run with --delete to reclaim them.",
            len(candidates) - skipped,
            skipped,
        )
    return 1 if failures else 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Purge leftover per-build scratch clone-token secrets from Secrets Manager (E36/T9). "
            "DRY RUN BY DEFAULT — pass --delete to actually reclaim them."
        ),
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help=(
            "Actually force-delete the matched secrets (ForceDeleteWithoutRecovery, no 30-day "
            "recovery window). Omit for a dry run, which is the default and makes no mutating "
            "call at all."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicitly ask for the default behaviour (plan only). Refuses to combine with --delete.",
    )
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help=(
            "Secrets Manager name prefix of the scratch tokens — RUNTIME_BUILD_TOKEN_PREFIX "
            f"(default: {DEFAULT_PREFIX}). Catches the tag-less secrets left by the "
            "put_secret_value retry path, which does not re-tag."
        ),
    )
    parser.add_argument(
        "--min-age-hours",
        type=float,
        default=DEFAULT_MIN_AGE_HOURS,
        help=(
            "Leave secrets written more recently than this alone, so the sweep cannot delete the "
            f"token an in-flight build has not cloned with yet (default: {DEFAULT_MIN_AGE_HOURS:g})."
        ),
    )
    parser.add_argument(
        "--region",
        default=None,
        help=(
            "AWS region to sweep (default: the AWS_REGION env var, else "
            f"{DEFAULT_REGION}). Scratch secrets live in the CONTROL-PLANE account/region, not the "
            "tenant's."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Enable DEBUG logging for THIS script only — the AWS SDK / HTTP wire loggers stay at "
            "INFO so no secret value can ever reach stderr."
        ),
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if args.verbose:
        logger.setLevel(logging.DEBUG)

    if args.dry_run and args.delete:
        logger.error("--dry-run and --delete are mutually exclusive; pick one.")
        return 2

    region = args.region or os.environ.get("AWS_REGION") or DEFAULT_REGION
    try:
        import boto3  # imported lazily so importing this module never touches AWS setup

        sm = boto3.client("secretsmanager", region_name=region)
        return run(
            sm=sm,
            prefix=args.prefix,
            min_age_hours=args.min_age_hours,
            delete=args.delete,
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean, actionable message
        logger.error("Sweep failed in region %s: %s", region, exc)
        logger.error(
            "Check that (1) AWS credentials for the CONTROL-PLANE account are configured, "
            "(2) they carry secretsmanager:ListSecrets (and secretsmanager:DeleteSecret with "
            "--delete), and (3) --region names the region the runtime-build service writes to.",
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
