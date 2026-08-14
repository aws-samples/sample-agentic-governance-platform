#!/usr/bin/env python
"""One-command bootstrap of a COMPLETE demo AgentCore Gateway (no params needed).

This is a DEV HELPER / USER step — the sibling of `create_example_gateway.py`, for
when you have NOTHING yet (no demo Lambda, no IAM roles). It creates EVERYTHING the
register-only platform flow needs to point at:

  1. Lambda execution role   `agp-demo-mcp-lambda-exec-role`  (trusts lambda.amazonaws.com,
     + AWSLambdaBasicExecutionRole for CloudWatch logs).
  2. The demo Lambda          `agp-demo-mcp-tools`             (python3.12, inline code
     implementing the `echo`/`add` tools of `_DEMO_TOOL_SCHEMA`).
  3. Gateway service role     `agp-demo-gateway-role`          (trusts
     bedrock-agentcore.amazonaws.com, + lambda:InvokeFunction scoped to the demo
     Lambda ARN — least privilege, NOT `*`).
  4. The gateway + one inline-lambda target — REUSING `create_example_gateway.py`'s
     builders/pollers verbatim (no duplicated logic). The gateway is created LOCKED
     (authorizerType=CUSTOM_JWT) with a *placeholder* customJWTAuthorizer (the type is
     IMMUTABLE post-create — a live UpdateGateway proved it), so the platform's
     registration step keeps the type and overwrites the discoveryUrl + real audience.

Then it prints the `gatewayUrl` + `gatewayArn` (verbatim from GetGateway) to paste
into the UI to register the MCP server.

Safety guarantees (the reviewer checks these hardest):
  * IDEMPOTENT — every create is get-or-create keyed on the `agp-demo-*` name, so
    re-running converges (never errors on "already exists", never duplicates). A
    partial failure (role made, Lambda failed) resumes cleanly on re-run.
  * NAMESPACED — the script ONLY ever creates/reads resources named `agp-demo-*`
    (derive from --name-prefix). It NEVER deletes, modifies, or touches any resource
    it didn't create. No delete_*, no update_* of pre-existing resources. On re-run
    it REUSES an existing demo Lambda (does not update its code).
  * OFFLINE-VALIDATABLE — `--dry-run` makes ZERO boto3/IAM/Lambda calls (clients are
    constructed lazily, only on the live path) and exits 0.

Why this script exists (research §8/§8.1): the Agentic Governance Platform is register-only
for production — it does NOT call CreateGateway/CreateGatewayTarget (let alone create
Lambdas) in the product flow. This dev helper is the exception, so the register-only
flow has something real to point at. It is NOT run by automation; you run it once,
then register the printed gatewayUrl/gatewayArn. The live create is YOUR step (needs
AWS creds with IAM create_role/attach/put_role_policy, lambda create_function/get,
the gateway permissions, and iam:PassRole on both roles — research §9).

Run from the backend dir (PYTHONPATH=src matches the other scripts and lets this
module import its sibling `scripts.create_example_gateway`):

    cd platform/control_plane/backend && \
        PYTHONPATH=src venv/bin/python scripts/bootstrap_demo_gateway.py

Validate everything offline first (makes ZERO boto3 calls, exits 0):

    PYTHONPATH=src venv/bin/python scripts/bootstrap_demo_gateway.py --dry-run

Region caveat (research §11): AgentCore is Preview and is NOT available in
eu-central-1 (Frankfurt). Default region is us-east-1; supported regions include
us-east-1, us-west-2, eu-west-1.
"""

import argparse
import json
import logging
import os
import sys

# Reuse create_example_gateway's builders/pollers VERBATIM (no copy-paste of their
# bodies). `scripts/` is not a regular package (no __init__.py), but with the backend
# dir on sys.path Python treats `scripts` as an implicit namespace package, so
# `import scripts.create_example_gateway` works under `PYTHONPATH=src` (cwd is the
# backend dir). To stay robust when invoked as `python scripts/bootstrap_demo_gateway.py`
# from elsewhere, ensure the backend dir (this file's parent's parent) is on sys.path.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from scripts.create_example_gateway import (  # noqa: E402 — after sys.path shim
    _DEFAULT_TENANT_ID,
    _DEMO_TOOL_SCHEMA,
    _PLACEHOLDER_AUDIENCE,
    _discovery_url,
    build_create_gateway_kwargs,
    build_create_target_kwargs,
    create_example_gateway,
)

# The IAM/Lambda primitives + constants used to live here; they are now extracted
# VERBATIM into the shared `demo_gateway_common` module so this bootstrap and the
# new `bootstrap_demo_use_cases` import them with zero copy-paste. The behavior is
# unchanged — this script just imports what it used to define.
from scripts.demo_gateway_common import (  # noqa: E402 — after sys.path shim
    _GATEWAY_INVOKE_POLICY_NAME,
    _GATEWAY_TRUST_POLICY,
    _LAMBDA_BASIC_EXEC_POLICY_ARN,
    _LAMBDA_HANDLER,
    _LAMBDA_RUNTIME,
    _LAMBDA_TRUST_POLICY,
    _gateway_invoke_policy_doc,
    ensure_attached_policy,
    ensure_inline_policy,
    ensure_lambda,
    ensure_role,
)

logger = logging.getLogger("bootstrap_demo_gateway")

# The inline Lambda handler source. It is assigned to a module constant (rather than
# zipped from a file on disk) so the embedded code is offline-parseable: a verify step
# `ast.parse`s LAMBDA_HANDLER_SRC to catch a syntax error without ever deploying.
#
# AgentCore lambda-target contract (research §8.1, §6.2): the Gateway invokes the
# Lambda with the tool ARGS as the `event` and the tool NAME in the client context at
# `context.client_context.custom["bedrockAgentCoreToolName"]`. The gateway prefixes the
# tool name with the target name joined by "___" (e.g. "agp-demo-target___echo"), so we
# split on "___" and take the last segment. This exact event/context shape IS the
# AgentCore lambda-target contract — keep it in sync with the gateway target config.
LAMBDA_HANDLER_SRC = '''\
"""agp-demo-mcp-tools — demo Lambda backing the AgentCore Gateway inline-lambda target.

AgentCore lambda-target contract: the Gateway invokes this function with the tool
ARGUMENTS as the `event` dict and the tool NAME carried in the client context at
context.client_context.custom["bedrockAgentCoreToolName"]. The gateway prefixes the
tool name with the target name joined by "___", so we split on "___" and use the last
segment. This event/context shape is the AgentCore lambda-target contract.
"""


def _tool_name(context):
    """Extract the bare tool name from the AgentCore client context."""
    custom = {}
    client_context = getattr(context, "client_context", None)
    if client_context is not None:
        custom = getattr(client_context, "custom", None) or {}
    raw = custom.get("bedrockAgentCoreToolName", "")
    # The gateway namespaces the tool as "<targetName>___<toolName>".
    return raw.split("___")[-1] if raw else raw


def handler(event, context):
    """Branch on the tool name and implement the two demo tools (echo, add)."""
    event = event or {}
    name = _tool_name(context)

    if name == "echo":
        # echo -> return the provided message verbatim.
        return {"message": event.get("message")}

    if name == "add":
        # add -> return the integer sum a + b.
        a = event.get("a", 0)
        b = event.get("b", 0)
        return {"sum": a + b}

    raise ValueError("unknown tool: %r (expected one of: echo, add)" % name)
'''


def _names(prefix: str) -> dict:
    """Derive the namespaced resource names from --name-prefix."""
    return {
        "exec_role": f"{prefix}-mcp-lambda-exec-role",
        "lambda": f"{prefix}-mcp-tools",
        "gateway_role": f"{prefix}-gateway-role",
        "gateway": f"{prefix}-mcp",
        "target": f"{prefix}-target",
    }


def bootstrap(region: str, prefix: str, tenant_id: str) -> dict:
    """Create the whole demo end-to-end (live path only). Returns a summary dict.

    The caller gates this behind the non-dry-run branch, so a dry-run never constructs
    a client or makes a call. Every step is get-or-create; re-running converges.

    `tenant_id` builds the create-time placeholder discoveryUrl (a sane default the
    platform re-sets at registration — see create_example_gateway.build_create_gateway_kwargs).
    """
    import boto3  # imported lazily so a dry-run never constructs a client

    names = _names(prefix)
    iam = boto3.client("iam", region_name=region)
    lam = boto3.client("lambda", region_name=region)

    # 1) Lambda execution role (+ basic-exec managed policy for CloudWatch logs).
    exec_role_arn, exec_role_created = ensure_role(
        iam,
        names["exec_role"],
        _LAMBDA_TRUST_POLICY,
        "Agentic Governance Platform demo MCP-tools Lambda execution role (dev helper).",
    )
    if exec_role_created:
        # Only attach on a fresh create — a found role was already set up by a prior run.
        ensure_attached_policy(iam, names["exec_role"], _LAMBDA_BASIC_EXEC_POLICY_ARN)

    # 2) The demo Lambda (inline echo/add handler). Pass LAMBDA_HANDLER_SRC to the
    # shared ensure_lambda (it takes the handler source as a parameter now); the
    # description defaults to the echo/add string, so this is byte-identical to before.
    lambda_arn = ensure_lambda(lam, names["lambda"], exec_role_arn, LAMBDA_HANDLER_SRC)

    # 3) Gateway service role (+ least-privilege lambda:InvokeFunction on the demo Lambda).
    gateway_role_arn, gateway_role_created = ensure_role(
        iam,
        names["gateway_role"],
        _GATEWAY_TRUST_POLICY,
        "Agentic Governance Platform demo gateway service role (dev helper).",
    )
    if gateway_role_created:
        # Only put the inline policy on a fresh create — a found role was already set up.
        ensure_inline_policy(
            iam,
            names["gateway_role"],
            _GATEWAY_INVOKE_POLICY_NAME,
            _gateway_invoke_policy_doc(lambda_arn),
        )

    # 4) The gateway + inline-lambda target — REUSE create_example_gateway verbatim.
    # create_example_gateway() is get-or-create-free, but its create_gateway/
    # create_gateway_target use deterministic uuid5 clientTokens (build_*_kwargs), so a
    # racing/retried create with the same name+token is idempotent at the API. It waits
    # both to READY and returns the GetGateway response (gatewayUrl/gatewayArn verbatim).
    gw_ready = create_example_gateway(
        gateway_name=names["gateway"],
        role_arn=gateway_role_arn,
        region=region,
        discovery_url=_discovery_url(tenant_id),
        lambda_arn=lambda_arn,
    )

    gateway_url = gw_ready.get("gatewayUrl")
    gateway_arn = gw_ready.get("gatewayArn")
    if not gateway_url or not gateway_arn:
        raise RuntimeError(
            "Gateway is READY but the response is missing gatewayUrl/gatewayArn; "
            f"keys: {sorted(gw_ready.keys())}"
        )

    return {
        "exec_role_name": names["exec_role"],
        "exec_role_arn": exec_role_arn,
        "lambda_name": names["lambda"],
        "lambda_arn": lambda_arn,
        "gateway_role_name": names["gateway_role"],
        "gateway_role_arn": gateway_role_arn,
        "gateway_name": names["gateway"],
        "gateway_url": gateway_url,
        "gateway_arn": gateway_arn,
    }


def _print_dry_run(region: str, prefix: str, tenant_id: str) -> None:
    """Print every resource the script WOULD create — ZERO boto3 calls."""
    names = _names(prefix)
    lambda_arn_placeholder = (
        f"arn:aws:lambda:{region}:<account-id>:function:{names['lambda']}"
    )
    # Build the gateway/target kwargs with the REUSED builders so the dry-run shows
    # exactly what the live path sends — including the CUSTOM_JWT placeholder authorizer
    # (the gateway is born locked; the type is immutable post-create).
    gw_kwargs = build_create_gateway_kwargs(
        names["gateway"],
        f"arn:aws:iam::<account-id>:role/{names['gateway_role']}",
        _discovery_url(tenant_id),
    )
    tgt_kwargs = build_create_target_kwargs(
        gateway_identifier="<gatewayId from CreateGateway response>",
        target_name=names["target"],
        lambda_arn=lambda_arn_placeholder,
    )

    print("DRY RUN — no boto3/IAM/Lambda calls will be made. The script WOULD create:")

    print(f"\n[1/4] IAM role {names['exec_role']!r} (Lambda execution role)")
    print("  get-or-create via iam.get_role(RoleName=...) -> on NoSuchEntity, iam.create_role(")
    print(_indent(json.dumps({
        "RoleName": names["exec_role"],
        "AssumeRolePolicyDocument": _LAMBDA_TRUST_POLICY,
    }, indent=2), 4))
    print("  )")
    print(f"  then iam.attach_role_policy(RoleName={names['exec_role']!r}, "
          f"PolicyArn={_LAMBDA_BASIC_EXEC_POLICY_ARN!r})")

    print(f"\n[2/4] Lambda {names['lambda']!r} (inline echo/add handler)")
    print("  get-or-create via lambda.get_function(FunctionName=...) -> on "
          "ResourceNotFoundException, lambda.create_function(")
    print(_indent(json.dumps({
        "FunctionName": names["lambda"],
        "Runtime": _LAMBDA_RUNTIME,
        "Role": f"arn:aws:iam::<account-id>:role/{names['exec_role']}",
        "Handler": _LAMBDA_HANDLER,
        "Code": {"ZipFile": "<in-memory zip of index.py from LAMBDA_HANDLER_SRC>"},
        "Timeout": 15,
        "MemorySize": 128,
    }, indent=2), 4))
    print("  )  # bounded retry on InvalidParameterValueException (IAM eventual consistency)")
    print("  inline handler implements the _DEMO_TOOL_SCHEMA tools: "
          f"{[t['name'] for t in _DEMO_TOOL_SCHEMA]}")
    print("  (reads tool name from context.client_context.custom['bedrockAgentCoreToolName'], "
          "split on '___')")

    print(f"\n[3/4] IAM role {names['gateway_role']!r} (gateway service role)")
    print("  get-or-create via iam.get_role(RoleName=...) -> on NoSuchEntity, iam.create_role(")
    print(_indent(json.dumps({
        "RoleName": names["gateway_role"],
        "AssumeRolePolicyDocument": _GATEWAY_TRUST_POLICY,
    }, indent=2), 4))
    print("  )")
    print(f"  then iam.put_role_policy(RoleName={names['gateway_role']!r}, "
          f"PolicyName={_GATEWAY_INVOKE_POLICY_NAME!r}, PolicyDocument=")
    print(_indent(json.dumps(_gateway_invoke_policy_doc(lambda_arn_placeholder), indent=2), 4))
    print("  )  # lambda:InvokeFunction scoped to the demo Lambda ARN (least privilege, NOT '*')")

    print(f"\n[4/4] Gateway {names['gateway']!r} + target {names['target']!r} "
          "(REUSING create_example_gateway builders)")
    print("  bedrock-agentcore-control.CreateGateway(")
    print(_indent(json.dumps(gw_kwargs, indent=2), 4))
    print("  )")
    print("  # gateway is BORN LOCKED: authorizerType=CUSTOM_JWT + placeholder "
          f"allowedAudience=[{_PLACEHOLDER_AUDIENCE!r}] + a default discoveryUrl.")
    print("  # The type is IMMUTABLE post-create; the platform overwrites the "
          "customJWTAuthorizer (real discoveryUrl + audience) at registration.")
    print("  # then poll GetGateway(gatewayIdentifier=<gatewayId>) until status==READY")
    print("  bedrock-agentcore-control.CreateGatewayTarget(")
    print(_indent(json.dumps(tgt_kwargs, indent=2), 4))
    print("  )")
    print("  # then poll GetGatewayTarget(...) until status==READY, then print "
          "gatewayUrl + gatewayArn (verbatim) to register the MCP server")

    print("\nTo clean up afterwards, delete these agp-demo-* resources (this script "
          "NEVER deletes anything):")
    print(f"  - IAM role {names['exec_role']}  (detach AWSLambdaBasicExecutionRole first)")
    print(f"  - Lambda function {names['lambda']}")
    print(f"  - IAM role {names['gateway_role']}  (delete inline policy "
          f"{_GATEWAY_INVOKE_POLICY_NAME} first)")
    print(f"  - Gateway {names['gateway']} (delete its target {names['target']} first)")

    print("\nDry run OK: resource shapes printed, no AWS calls made.")


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in text.splitlines())


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Bootstrap a COMPLETE demo AgentCore Gateway in one command (no required "
            "args): creates the Lambda exec role, the demo Lambda (echo/add), the "
            "gateway service role, then the gateway + target (reusing "
            "create_example_gateway). Prints the gatewayUrl/gatewayArn to register. "
            "Dev helper — the live create is your step (needs AWS creds with IAM + "
            "Lambda + gateway permissions and iam:PassRole on both roles)."
        ),
        epilog=(
            "Everything it creates is namespaced agp-demo-* and idempotent "
            "(get-or-create), so re-running is safe and converges. It NEVER deletes, "
            "modifies, or touches any resource it did not create. To clean up, delete "
            "the agp-demo-* IAM roles, Lambda, gateway and target it lists in its "
            "summary (the script does NOT delete them for you)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region (default: us-east-1; AgentCore is NOT in eu-central-1).",
    )
    parser.add_argument(
        "--name-prefix",
        default="agp-demo",
        help=(
            "Prefix for all created resource names (default: agp-demo). Resources are "
            "<prefix>-mcp-lambda-exec-role / <prefix>-mcp-tools / <prefix>-gateway-role "
            "/ <prefix>-mcp (+ <prefix>-target)."
        ),
    )
    parser.add_argument(
        "--tenant-id",
        default=_DEFAULT_TENANT_ID,
        help=(
            "Entra tenant ID for the create-time placeholder discoveryUrl (default: the "
            "demo tenant). Only a sane default — the platform RE-SETS the discoveryUrl + "
            "real audience at registration, so even a wrong value self-heals."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print every resource it WOULD create (IAM roles, Lambda, gateway, target) "
            "WITHOUT making any boto3/IAM/Lambda call. Exit 0."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging (prints each poll attempt).",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.dry_run:
        # ZERO boto3 calls — we never construct a client on this path.
        _print_dry_run(args.region, args.name_prefix, args.tenant_id)
        return 0

    try:
        summary = bootstrap(
            region=args.region, prefix=args.name_prefix, tenant_id=args.tenant_id
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean, actionable message
        # botocore exceptions (ClientError, EndpointConnectionError, NoCredentials,
        # UnknownServiceError, AccessDenied, etc.) plus the poll TimeoutError/
        # RuntimeError are caught broadly so the user never sees a raw traceback.
        logger.error("Failed to bootstrap the demo gateway: %s", exc)
        logger.error(
            "Check that: (1) AWS credentials are configured with IAM "
            "(create_role/attach_role_policy/put_role_policy/get_role), Lambda "
            "(create_function/get_function), and the gateway permissions, plus "
            "iam:PassRole on both agp-demo roles (research §9); (2) Bedrock AgentCore "
            "(Preview) is available in region %r — it is NOT in eu-central-1/Frankfurt; "
            "supported regions include us-east-1, us-west-2, eu-west-1 (research §11); "
            "(3) boto3 is recent enough to expose 'bedrock-agentcore-control'. Re-running "
            "is safe — every step is get-or-create and resumes cleanly.",
            args.region,
        )
        return 1

    print("\nDemo gateway is READY. Ensured these agp-demo-* resources:")
    print(f"  Lambda exec role : {summary['exec_role_name']}  ({summary['exec_role_arn']})")
    print(f"  Demo Lambda      : {summary['lambda_name']}  ({summary['lambda_arn']})")
    print(f"  Gateway role     : {summary['gateway_role_name']}  ({summary['gateway_role_arn']})")
    print(f"  Gateway          : {summary['gateway_name']}")
    print("\nPaste these into the UI to register the MCP server:")
    print(f"gatewayUrl: {summary['gateway_url']}")
    print(f"gatewayArn: {summary['gateway_arn']}")
    print(
        "\nTo clean up later, delete the agp-demo-* resources listed above (this "
        "script never deletes anything)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
