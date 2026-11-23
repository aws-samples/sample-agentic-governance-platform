#!/usr/bin/env python
"""Idempotently provision ONE AWS Agent Registry on the `agent-registry` namespace.

The agent registry and the MCP-server registry differ only by `--name`, so one script
covers both. Every registry call goes through the client `core.aws_clients.registry_control_client`
returns.

Service facts this script relies on (botocore 1.43.67, `agent-registry-control`)
---------------------------------------------------------------------------------
  * `ListRegistries` returns `{"registries": [...], "nextToken": ...}`, its paginator is
    registered, and each item carries `registryId`, `name`, `registryArn` and `status`.
    The lookup itself is `core.registry_resolver.find_registry_by_name`, the one
    implementation the backend uses too, so both agree on what "the registry named X"
    means (a duplicate name is a hard error, never a silent first match).
  * A `RegistryReady` waiter ships with the model (delay 30, maxAttempts 5, success on
    `status == READY`, failure on `CREATE_FAILED`/`UPDATE_FAILED`/`DELETE_FAILED`).
  * `CreateRegistry` returns ONLY `registryArn`; the id is the ARN tail
    (`arn:...:registry/{registryId}`).
  * Auth is nested under `discoveryConfiguration={"authorizerType": "AWS_IAM"}` (with
    AWS_IAM, `authorizerConfiguration` is omitted), and
    `approvalConfiguration={"autoApprovalRules": []}` means "manual approval required".
  * `clientToken` has a minimum length of 33: a stable `uuid5` per registry name makes a
    racing double-invocation (or a plain retry) idempotent instead of creating a duplicate.

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

Terraform runs this script with NO `PYTHONPATH` (`modules/agent_registry/main.tf`), so the
backend's `src/` is put on `sys.path` here rather than required from the environment; the
insert is a no-op under `PYTHONPATH=src`.

Usage (from the backend dir):

    cd platform/control_plane/backend && \
        PYTHONPATH=src venv/bin/python scripts/ensure_registry.py --name agp-agents

Terraform creates both registries, so a manual invocation is a fallback/diagnostic.
"""

import argparse
import json
import logging
import os
import sys
import uuid

_SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from core.aws_clients import (  # noqa: E402 — `core.` resolves only after the sys.path shim above; expires 2027-10-23; added E52A/T4
    REGISTRY_CONTROL,
    registry_control_client,
)
from core.registry_resolver import (  # noqa: E402 — `core.` resolves only after the sys.path shim above; expires 2027-10-23; added E49A-50A/T17
    find_registry_by_name,
)

logger = logging.getLogger("ensure_registry")

DEFAULT_REGION = "us-east-1"

_DESCRIPTION_TEMPLATE = "Agentic Governance Platform registry: {name}"


def create_registry(ctl, name: str, description: str):
    """Create the registry and return `(registryId, registryArn)`.

    `discoveryConfiguration={"authorizerType": "AWS_IAM"}` -> omit
    `authorizerConfiguration`. `approvalConfiguration={"autoApprovalRules": []}` -> every
    record requires explicit approval. The deterministic `clientToken` (>= 33 chars, as
    the API demands) makes retries idempotent. `description` is create-only; it never
    updates a registry that already exists. The id is the ARN tail, because CreateRegistry
    returns only `registryArn`.
    """
    resp = ctl.create_registry(
        name=name,
        description=description,
        discoveryConfiguration={"authorizerType": "AWS_IAM"},
        approvalConfiguration={"autoApprovalRules": []},
        clientToken=str(uuid.uuid5(uuid.NAMESPACE_DNS, f"agp-registry:{name}")),
    )
    arn = resp.get("registryArn")
    if not arn:
        raise RuntimeError(
            f"CreateRegistry succeeded but returned no registryArn; keys: {sorted(resp.keys())}"
        )
    return arn.rsplit("/", 1)[-1], arn


def resolve_registry(name: str, region: str, *, ctl=None):
    """Find-or-create the registry, wait for READY, return `(registryId, registryArn)`.

    `ensure_registry` is the id-only contract the Terraform module depends on; this
    variant also hands back the ARN for the `--json` payload. READY is awaited on both
    paths: a fresh registry is CREATING first, and an existing one found mid-CREATING is
    not yet usable for records either.
    """
    ctl = ctl if ctl is not None else registry_control_client(region)

    logger.info("Looking for an existing registry named %r in region %s ...", name, region)
    rid, arn = find_registry_by_name(ctl.get_paginator("list_registries").paginate(), name)
    if rid:
        logger.info("Found existing registry %r -> %s (no create needed)", name, rid)
    else:
        logger.info("No registry named %r found; creating it ...", name)
        rid, arn = create_registry(ctl, name, _DESCRIPTION_TEMPLATE.format(name=name))
        logger.info("Created registry %r -> %s", name, rid)

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
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        rid, arn = resolve_registry(args.name, args.region)
    except Exception as exc:
        logger.error(
            "Failed to ensure registry %r in region %s: %s "
            "(check AWS credentials/permissions for %s and that boto3 exposes it; "
            "override with --name/--region)",
            args.name,
            args.region,
            exc,
            REGISTRY_CONTROL,
        )
        logger.debug("the failure's traceback", exc_info=True)
        return 1

    if args.json:
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
