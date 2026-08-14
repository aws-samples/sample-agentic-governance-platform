#!/usr/bin/env python
"""Idempotently provision ONE AWS Agent Registry on the `agent-registry` namespace (E32).

Why this script exists at all
-----------------------------
AWS moved the Registry APIs out of Bedrock AgentCore into their own `agent-registry`
service on 2026-08-06. The old `bedrock-agentcore-control` namespace shuts down on
2026-09-17 and brand-new AWS accounts cannot reach it *at all*, so every registry
call in this platform now goes through `boto3.client("agent-registry-control")`.
This module replaces the two Preview-era bootstrap scripts
(`ensure_agent_registry.py` + `ensure_mcp_registry.py`) with a single
name-parameterised one: the agent registry and the MCP-server registry differ only
by `--name`, so one script covers both.

Why it is so much smaller than what it replaces
-----------------------------------------------
The old scripts were written against an *unpublished* Preview API and were full of
defensive guesswork. The service model is published now, so all of that guesswork is
dead weight and has been deleted rather than translated. Verified against
botocore 1.43.67 (`agent-registry-control`):

  * `ListRegistries` returns exactly `{"registries": [...], "nextToken": ...}` and its
    paginator IS registered (`ctl.can_paginate("list_registries")` is True). No more
    probing `("registries", "registrySummaries", "items")` — we paginate and match on
    `item["name"]`.
  * `ListRegistries` items carry a real `registryId` member (alongside `name`,
    `registryArn`, `status`, ...), so the found path needs no ARN parsing either.
  * A `RegistryReady` waiter ships with the model (`ctl.waiter_names` includes
    `registry_ready`; delay 30, maxAttempts 5, success on `status == READY`, failure on
    `CREATE_FAILED`/`UPDATE_FAILED`/`DELETE_FAILED`). That replaces
    `ensure_mcp_registry.py`'s hand-rolled 30x2s poll-to-READY loop, which is gone.
  * `CreateRegistry` returns **ONLY** `registryArn` — there is no `registryId` member in
    its output shape. The id is the ARN tail (`arn:...:registry/{registryId}`), so we
    parse it, exactly as the Preview scripts did.
  * The create shape changed: auth is nested under
    `discoveryConfiguration={"authorizerType": "AWS_IAM"}` (with AWS_IAM we omit
    `authorizerConfiguration` entirely), and the old `autoApproval: False` boolean became
    an enum list. `approvalConfiguration={"autoApprovalRules": []}` is the new spelling of
    "manual approval required" — an empty rule list auto-approves nothing.
  * `clientToken` has a minimum length of 33, so the deterministic `uuid5` idiom is kept:
    a stable 36-char token per registry name makes a racing double-invocation (or a plain
    retry) idempotent instead of creating a duplicate registry.

Waiting for READY on BOTH paths is deliberate: a freshly created registry is `CREATING`
before `READY`, and creating a record against a non-READY registry fails with
`ConflictException: "Registry is not in READY state"`. A registry that already exists is
normally READY, but one found mid-`CREATING` must still be waited on, and the waiter is a
no-op cost when it is already READY.

The `--json` output is a machine contract
-----------------------------------------
Terraform (the `agent_registry` bootstrap module) shells out to this script and parses
**exactly one line of JSON from stdout**:

    {"registry_id": "...", "registry_arn": "...", "name": "...", "region": "..."}

Therefore **all** logging goes to stderr (`logging.basicConfig(stream=sys.stderr, ...)`)
and the JSON line is the only thing ever written to stdout in `--json` mode. A single
stray `print()` — or one log record leaking onto stdout — breaks that parse, including at
`--verbose`, where logging is at its loudest. Exit status is 0 on success; on failure the
script prints one actionable line to stderr and exits non-zero rather than dumping a raw
traceback at an operator.

Usage (from the backend dir; PYTHONPATH=src is required — src/ is not a package):

    cd platform/control_plane/backend && \
        PYTHONPATH=src venv/bin/python scripts/ensure_registry.py --name agp-agents

Terraform now creates both registries, so a manual invocation is a fallback/diagnostic.
"""

import argparse
import json
import logging
import os
import sys
import uuid

# The find-by-name lookup this script pioneered now lives in `core.registry_resolver`, because
# the BACKEND resolves its registry ids by name too (it stopped taking them from Terraform,
# which is what killed the two-applies-from-zero problem — see that module's docstring). ONE
# implementation, imported by both, rather than a second copy that can drift: the two must
# agree on what "the registry named X" means, including that a DUPLICATE name is a hard error
# rather than a silent first-match. `SERVICE_NAME` comes from there for the same reason.
#
# WHY THE sys.path SHIM RATHER THAN REQUIRING PYTHONPATH=src. Terraform runs this script
# through `python_bin` with NO PYTHONPATH — deliberately, and documented as such in
# `modules/agent_registry/main.tf` ("The script needs no PYTHONPATH"). Making the Terraform
# contract depend on an env var would move a plan-time-invisible failure into the middle of an
# apply. So `src/` is put on `sys.path` here instead, the same idiom `bootstrap_demo_gateway.py`
# and `migrate_to_e25b.py` already use. It is a no-op under `PYTHONPATH=src` (pytest, the
# documented manual invocation), because the entry is only inserted when absent.
_SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from core.registry_resolver import (  # noqa: E402 — after the sys.path shim
    SERVICE_NAME,
    find_registry_by_name,
)

logger = logging.getLogger("ensure_registry")

DEFAULT_REGION = "us-east-1"

# One script now serves both the agent registry and the MCP-server registry, which the two
# Preview scripts described distinctly. The name is the only thing that differs, so the
# description is derived from it rather than adding a CLI flag the Terraform contract
# does not include. `description` is create-only — it is never used to update a registry
# that already exists.
_DESCRIPTION_TEMPLATE = "Agentic Governance Platform registry: {name}"


def _client(region):
    """Return an `agent-registry-control` client for `region`.

    boto3 is imported lazily so that importing this module stays side-effect-free (tests
    inject their own client and never touch AWS). Tests monkeypatch this function.
    """
    import boto3

    return boto3.client(SERVICE_NAME, region_name=region)


def create_registry(ctl, name: str, description: str):
    """Create the registry and return `(registryId, registryArn)`.

    `discoveryConfiguration={"authorizerType": "AWS_IAM"}` -> omit
    `authorizerConfiguration`. `approvalConfiguration={"autoApprovalRules": []}` -> every
    record requires explicit approval. The deterministic `clientToken` (>= 33 chars, as
    the API demands) makes retries idempotent.
    """
    resp = ctl.create_registry(
        name=name,
        description=description,
        discoveryConfiguration={"authorizerType": "AWS_IAM"},
        approvalConfiguration={"autoApprovalRules": []},
        clientToken=str(uuid.uuid5(uuid.NAMESPACE_DNS, f"agp-registry:{name}")),
    )
    # CreateRegistry's output shape contains registryArn and nothing else, so the id has
    # to come from the ARN tail (arn:...:registry/{registryId}).
    arn = resp.get("registryArn")
    if not arn:
        raise RuntimeError(
            f"CreateRegistry succeeded but returned no registryArn; keys: {sorted(resp.keys())}"
        )
    return arn.rsplit("/", 1)[-1], arn


def resolve_registry(name: str, region: str, *, ctl=None):
    """Find-or-create the registry, wait for READY, return `(registryId, registryArn)`.

    `ensure_registry` is the id-only contract Task 6's Terraform module depends on; this
    variant also hands back the ARN for the `--json` payload.
    """
    ctl = ctl if ctl is not None else _client(region)

    logger.info("Looking for an existing registry named %r in region %s ...", name, region)
    rid, arn = find_registry_by_name(ctl, name)
    if rid:
        logger.info("Found existing registry %r -> %s (no create needed)", name, rid)
    else:
        logger.info("No registry named %r found; creating it ...", name)
        rid, arn = create_registry(ctl, name, _DESCRIPTION_TEMPLATE.format(name=name))
        logger.info("Created registry %r -> %s", name, rid)

    # Wait on both paths: a fresh registry is CREATING before READY, and an existing one
    # found mid-CREATING is not yet usable for records either.
    logger.info("Waiting for registry %s to become READY ...", rid)
    ctl.get_waiter("registry_ready").wait(registryId=rid)
    logger.info("Registry %s is READY.", rid)
    return rid, arn


def ensure_registry(name: str, region: str, *, ctl=None) -> str:
    """Find-or-create the registry and return its `registryId`. Raises on AWS errors.

    Pass `ctl` to inject a client (that keyword is what makes this testable offline).
    """
    rid, _arn = resolve_registry(name, region, ctl=ctl)
    return rid


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Idempotently create or find an AWS Agent Registry and print its id.",
    )
    parser.add_argument(
        "--name",
        required=True,
        help="Registry name (e.g. agp-agents or agp-mcp-servers).",
    )
    parser.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help=f"AWS region hosting the registry (default: {DEFAULT_REGION}).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit exactly one line of JSON on stdout (the Terraform contract).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging on stderr (logs every registry inspected).",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    # stream=sys.stderr is load-bearing: stdout must stay parseable by Terraform even at
    # --verbose. logging's default handler writes to stderr already, but pinning it makes
    # the contract explicit and immune to a future default change.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        rid, arn = resolve_registry(args.name, args.region)
    except Exception as exc:  # noqa: BLE001 — surface a clean, actionable message
        # botocore exceptions (ClientError, EndpointConnectionError, NoCredentials,
        # UnknownServiceError) and the waiter's WaiterError are caught broadly so an
        # operator never sees a raw traceback from a bootstrap step.
        logger.error(
            "Failed to ensure registry %r in region %s: %s "
            "(check AWS credentials/permissions for %s and that boto3 exposes it; "
            "override with --name/--region)",
            args.name,
            args.region,
            exc,
            SERVICE_NAME,
        )
        return 1

    if args.json:
        # The ONLY write to stdout. One line, no trailing content.
        print(
            json.dumps(
                {
                    "registry_id": rid,
                    "registry_arn": arn,
                    "name": args.name,
                    "region": args.region,
                }
            )
        )
    else:
        print(f"registryId: {rid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
