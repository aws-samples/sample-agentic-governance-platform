#!/usr/bin/env python
"""One-command bootstrap of THREE AgentCore Gateways for insurance demo use cases.

This is the data-driven sibling of `bootstrap_demo_gateway.py` (which stands up ONE
echo/add demo gateway). It loops the `DEMO_USE_CASES` registry (`demo_use_cases.py`)
and, per domain, runs the SAME proven 4-step sequence the original bootstrap runs —
reusing the shared `demo_gateway_common` IAM/Lambda primitives and
`create_example_gateway` VERBATIM (zero duplicated IAM/Lambda/gateway logic):

  1. Lambda execution role   `<prefix>-mcp-lambda-exec-role`  (+ AWSLambdaBasicExecutionRole
     on a fresh create, for CloudWatch logs).
  2. The domain Lambda        `<prefix>-mcp-tools`             (python3.12, inline code
     zipping THAT domain's `handler_src` — only its tools).
  3. Gateway service role     `<prefix>-gateway-role`          (+ lambda:InvokeFunction
     scoped to THAT domain's Lambda ARN — least privilege, NOT `*`, on a fresh create).
  4. The gateway + one inline-lambda target — REUSING `create_example_gateway` with the
     domain's own `tool_schema`. Born LOCKED (authorizerType=CUSTOM_JWT) with a
     placeholder authorizer (the type is IMMUTABLE post-create); the platform overwrites
     the config at registration. Then poll both to READY and collect gatewayUrl/gatewayArn.

The three domains (design §5/§6):
  * Contact Center            -> agp-contact-center-mcp
  * First Notification of Loss-> agp-fnol-mcp
  * Insurance Support         -> agp-insurance-support-mcp

Safety guarantees (identical to bootstrap_demo_gateway.py):
  * IDEMPOTENT  — every create is get-or-create keyed on the `agp-*` name, so
    re-running converges; a partial failure resumes cleanly on re-run.
  * NAMESPACED  — only ever creates/reads resources named `agp-*`. NEVER deletes,
    modifies, or touches any resource it didn't create. The ONE artifact it writes is its
    own output file (`use_cases_gateways.txt`) — never an AWS resource.
  * OFFLINE-VALIDATABLE — `--dry-run` makes ZERO boto3/IAM/Lambda calls (clients are
    constructed lazily, only on the live path) and exits 0.

Resilience (continue-on-error): each domain is attempted independently inside a
try/except. A failure is logged with an actionable hint block, recorded, and the loop
continues to the next domain. After the loop a summary lists successes/failures; the
script exits NON-ZERO if any domain failed (successes are still printed + written to the
output file).

Output: prints each succeeded gateway's block AND writes `use_cases_gateways.txt`
into the scripts folder (overwritten each run, only succeeded domains; `--output-file`
overrides the location) for pasting the gatewayUrl/gatewayArn into the UI to register.

`--dry-run` HARD CONTRACT: ZERO boto3 calls; per domain `ast.parse(handler_src)` (catches
a syntax error without deploying) + assert every tool `name` in `tool_schema` appears as a
branch in `handler_src` (drift guard between the advertised schema and the mock
implementation — fail with a clear message otherwise); print the resource names + the
CreateGateway/CreateGatewayTarget call shapes (via the reused build_*_kwargs); exit 0.

Run from the backend dir (PYTHONPATH=src matches the other scripts and lets this module
import its siblings):

    cd platform/control_plane/backend && \
        PYTHONPATH=src venv/bin/python scripts/bootstrap_demo_use_cases.py

Validate everything offline first (makes ZERO boto3 calls, exits 0):

    PYTHONPATH=src venv/bin/python scripts/bootstrap_demo_use_cases.py --dry-run

Region caveat (research §11): AgentCore is Preview and is NOT available in eu-central-1
(Frankfurt). Default region is us-east-1; supported regions include us-east-1, us-west-2,
eu-west-1.
"""

import argparse
import ast
import json
import logging
import os
import sys

# Reuse the sibling scripts VERBATIM (no copy-paste of their bodies). `scripts/` is not a
# regular package (no __init__.py), but with the backend dir on sys.path Python treats
# `scripts` as an implicit namespace package, so `import scripts.<mod>` works under
# `PYTHONPATH=src` (cwd is the backend dir). To stay robust when invoked as
# `python scripts/bootstrap_demo_use_cases.py` from elsewhere, ensure the backend dir
# (this file's parent's parent) is on sys.path — the SAME shim as bootstrap_demo_gateway.py.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from scripts.create_example_gateway import (  # noqa: E402 — after sys.path shim
    _DEFAULT_TENANT_ID,
    _PLACEHOLDER_AUDIENCE,
    _discovery_url,
    build_create_gateway_kwargs,
    build_create_target_kwargs,
    create_example_gateway,
)
from scripts.demo_gateway_common import (  # noqa: E402 — after sys.path shim
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
from scripts.demo_use_cases import DEMO_USE_CASES  # noqa: E402 — after sys.path shim

logger = logging.getLogger("bootstrap_demo_use_cases")

# Inline-policy name for the gateway role's lambda:InvokeFunction grant. Defined locally
# (NOT imported from demo_gateway_common, whose `agp-demo-invoke-demo-lambda` belongs to
# the original echo/add bootstrap) so the use-case gateways carry no `demo` in any created
# resource name.
_GATEWAY_INVOKE_POLICY_NAME = "agp-invoke-lambda"

# The output file the script owns: written next to the script, overwritten each run with
# only the domains that succeeded this run. `--output-file` overrides the location.
_DEFAULT_OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "use_cases_gateways.txt")


def _names(prefix: str) -> dict:
    """Derive the namespaced resource names from a domain's prefix.

    Mirrors `bootstrap_demo_gateway.py`'s `_names(prefix)`, with `target` aligned to
    `create_example_gateway`'s own derivation (`<gateway_name>-target`, i.e.
    `<prefix>-mcp-target`) so the dry-run printout matches the live-path target name.
    """
    return {
        "exec_role": f"{prefix}-mcp-lambda-exec-role",
        "lambda": f"{prefix}-mcp-tools",
        "gateway_role": f"{prefix}-gateway-role",
        "gateway": f"{prefix}-mcp",
        "target": f"{prefix}-mcp-target",
    }


def _validate_handler_src(domain: dict) -> None:
    """Drift guard (dry-run): ast.parse(handler_src) + assert every tool has a branch.

    Catches (a) a syntax error in the inline handler WITHOUT deploying it, and (b) a tool
    advertised in `tool_schema` that has no branch in `handler_src` (the advertised schema
    and the mock implementation drifting apart). Raises ValueError with a clear message on
    either, so the dry-run fails loudly instead of shipping a broken Lambda.
    """
    handler_src = domain["handler_src"]
    key = domain["key"]

    # (a) The handler must be syntactically valid Python.
    try:
        ast.parse(handler_src)
    except SyntaxError as exc:
        raise ValueError(
            f"domain {key!r}: handler_src failed to parse (SyntaxError): {exc}"
        ) from exc

    # (b) Every advertised tool name must appear as a branch in the handler. The handler
    # branches with `if name == "<tool>":`, so the quoted tool name must be present.
    missing = [
        tool["name"]
        for tool in domain["tool_schema"]
        if f'"{tool["name"]}"' not in handler_src
    ]
    if missing:
        raise ValueError(
            f"domain {key!r}: tool(s) {missing} advertised in tool_schema have no branch "
            f"in handler_src (schema/handler drift). Add a matching `if name == ...:` branch."
        )


def bootstrap_one(region: str, tenant_id: str, domain: dict) -> dict:
    """Create ONE domain's gateway end-to-end (live path only). Returns a summary dict.

    The caller gates this behind the non-dry-run branch, so a dry-run never constructs a
    client or makes a call. Every step is get-or-create; re-running converges. Mirrors
    `bootstrap_demo_gateway.py`'s `bootstrap()` but threads the domain's `handler_src`
    (its Lambda code) and `tool_schema` (its inline target schema).
    """
    import boto3  # imported lazily so a dry-run never constructs a client

    names = _names(domain["prefix"])
    iam = boto3.client("iam", region_name=region)
    lam = boto3.client("lambda", region_name=region)

    # 1) Lambda execution role (+ basic-exec managed policy for CloudWatch logs).
    exec_role_arn, exec_role_created = ensure_role(
        iam,
        names["exec_role"],
        _LAMBDA_TRUST_POLICY,
        f"Agentic Governance Platform {domain['display_name']} MCP-tools Lambda execution role (dev helper).",
    )
    if exec_role_created:
        # Only attach on a fresh create — a found role was already set up by a prior run.
        ensure_attached_policy(iam, names["exec_role"], _LAMBDA_BASIC_EXEC_POLICY_ARN)

    # 2) The domain Lambda (inline handler — only this domain's tools).
    lambda_arn = ensure_lambda(
        lam,
        names["lambda"],
        exec_role_arn,
        domain["handler_src"],
        description=domain["description"],
    )

    # 3) Gateway service role (+ least-privilege lambda:InvokeFunction on THIS Lambda).
    gateway_role_arn, gateway_role_created = ensure_role(
        iam,
        names["gateway_role"],
        _GATEWAY_TRUST_POLICY,
        f"Agentic Governance Platform {domain['display_name']} gateway service role (dev helper).",
    )
    if gateway_role_created:
        # Only put the inline policy on a fresh create — a found role was already set up.
        ensure_inline_policy(
            iam,
            names["gateway_role"],
            _GATEWAY_INVOKE_POLICY_NAME,
            _gateway_invoke_policy_doc(lambda_arn),
        )

    # 4) The gateway + inline-lambda target — REUSE create_example_gateway with this
    # domain's tool_schema. It is get-or-create-by-name (idempotent) and waits both to
    # READY, returning the GetGateway response (gatewayUrl/gatewayArn verbatim).
    gw_ready = create_example_gateway(
        gateway_name=names["gateway"],
        role_arn=gateway_role_arn,
        region=region,
        discovery_url=_discovery_url(tenant_id),
        lambda_arn=lambda_arn,
        tool_schema=domain["tool_schema"],
    )

    gateway_url = gw_ready.get("gatewayUrl")
    gateway_arn = gw_ready.get("gatewayArn")
    if not gateway_url or not gateway_arn:
        raise RuntimeError(
            "Gateway is READY but the response is missing gatewayUrl/gatewayArn; "
            f"keys: {sorted(gw_ready.keys())}"
        )

    return {
        "key": domain["key"],
        "display_name": domain["display_name"],
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


def _select_domains(only):
    """Filter DEMO_USE_CASES by the --only csv of keys (None/empty -> all). Raises on a
    typo so the user gets a clear error instead of silently running nothing."""
    if not only:
        return list(DEMO_USE_CASES)
    wanted = [k.strip() for k in only.split(",") if k.strip()]
    known = {d["key"] for d in DEMO_USE_CASES}
    unknown = [k for k in wanted if k not in known]
    if unknown:
        raise ValueError(
            f"--only contains unknown key(s) {unknown}; known keys: {sorted(known)}"
        )
    return [d for d in DEMO_USE_CASES if d["key"] in wanted]


def _print_dry_run_domain(region: str, tenant_id: str, domain: dict) -> None:
    """Print every resource ONE domain WOULD create — ZERO boto3 calls.

    Runs the drift guard first (ast.parse + tool-branch check), then prints the resource
    names + the CreateGateway/CreateGatewayTarget call shapes (reusing the same
    build_*_kwargs the live path uses, threading the domain's tool_schema).
    """
    # Drift guard — raises ValueError (with a clear message) on a syntax error or a tool
    # advertised in tool_schema with no matching branch in handler_src.
    _validate_handler_src(domain)

    names = _names(domain["prefix"])
    lambda_arn_placeholder = (
        f"arn:aws:lambda:{region}:<account-id>:function:{names['lambda']}"
    )
    gw_kwargs = build_create_gateway_kwargs(
        names["gateway"],
        f"arn:aws:iam::<account-id>:role/{names['gateway_role']}",
        _discovery_url(tenant_id),
    )
    tgt_kwargs = build_create_target_kwargs(
        gateway_identifier="<gatewayId from CreateGateway response>",
        target_name=names["target"],
        lambda_arn=lambda_arn_placeholder,
        tool_schema=domain["tool_schema"],
    )
    tool_names = [t["name"] for t in domain["tool_schema"]]

    print(f"\n========== {domain['display_name']}  ({names['gateway']}) ==========")
    print(f"  handler_src OK (ast.parse passed); {len(tool_names)} tools, every tool has "
          "a handler branch (drift guard passed).")

    print(f"\n[1/4] IAM role {names['exec_role']!r} (Lambda execution role)")
    print("  get-or-create via iam.get_role(RoleName=...) -> on NoSuchEntity, iam.create_role(")
    print(_indent(json.dumps({
        "RoleName": names["exec_role"],
        "AssumeRolePolicyDocument": _LAMBDA_TRUST_POLICY,
    }, indent=2), 4))
    print("  )")
    print(f"  then iam.attach_role_policy(RoleName={names['exec_role']!r}, "
          f"PolicyArn={_LAMBDA_BASIC_EXEC_POLICY_ARN!r})")

    print(f"\n[2/4] Lambda {names['lambda']!r} (inline {domain['key']} handler)")
    print("  get-or-create via lambda.get_function(FunctionName=...) -> on "
          "ResourceNotFoundException, lambda.create_function(")
    print(_indent(json.dumps({
        "FunctionName": names["lambda"],
        "Runtime": _LAMBDA_RUNTIME,
        "Role": f"arn:aws:iam::<account-id>:role/{names['exec_role']}",
        "Handler": _LAMBDA_HANDLER,
        "Code": {"ZipFile": "<in-memory zip of index.py from this domain's handler_src>"},
        "Timeout": 15,
        "MemorySize": 128,
    }, indent=2), 4))
    print("  )  # bounded retry on InvalidParameterValueException (IAM eventual consistency)")
    print(f"  inline handler implements this domain's tools: {tool_names}")
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
    print("  )  # lambda:InvokeFunction scoped to THIS Lambda ARN (least privilege, NOT '*')")

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
    print("  # then poll GetGatewayTarget(...) until status==READY, then collect "
          "gatewayUrl + gatewayArn (verbatim) to register the MCP server")


def _print_dry_run(region: str, tenant_id: str, domains: list) -> None:
    """Print every resource ALL selected domains WOULD create — ZERO boto3 calls."""
    print("DRY RUN — no boto3/IAM/Lambda calls will be made. The script WOULD create, "
          f"for {len(domains)} domain(s):")
    for domain in domains:
        _print_dry_run_domain(region, tenant_id, domain)

    print("\nTo clean up afterwards, delete each domain's agp-* resources (this "
          "script NEVER deletes anything): the exec role (detach "
          "AWSLambdaBasicExecutionRole first), the Lambda, the gateway role (delete the "
          f"inline policy {_GATEWAY_INVOKE_POLICY_NAME} first), and the gateway (delete "
          "its target first).")
    print("\nDry run OK: handlers parsed, tool/handler drift guard passed, resource shapes "
          "printed, no AWS calls made.")


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in text.splitlines())


def _format_output_file(summaries: list) -> str:
    """Render the use_cases_gateways.txt body from the succeeded-domain summaries.

    Same block style as the design §5 sample (a header + one `##`-titled block per
    succeeded domain with its gatewayUrl/gatewayArn).
    """
    lines = [
        "# Agentic Governance Platform use-case gateways — generated by bootstrap_demo_use_cases.py",
        "# (Paste each gatewayUrl/gatewayArn into the UI to register the MCP server.)",
    ]
    for summary in summaries:
        lines.append("")
        lines.append(f"## {summary['display_name']}  ({summary['gateway_name']})")
        lines.append(f"gatewayUrl: {summary['gateway_url']}")
        lines.append(f"gatewayArn: {summary['gateway_arn']}")
    lines.append("")
    return "\n".join(lines)


def _write_output_file(path: str, summaries: list) -> None:
    """Overwrite the output file with the succeeded-domain blocks (the ONE artifact this
    script owns — never an AWS resource)."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_format_output_file(summaries))
    logger.info("Wrote %d succeeded gateway(s) to %s", len(summaries), path)


def _print_failure_hint(region: str) -> None:
    """The same actionable hint block bootstrap_demo_gateway.py prints on failure."""
    logger.error(
        "Check that: (1) AWS credentials are configured with IAM "
        "(create_role/attach_role_policy/put_role_policy/get_role), Lambda "
        "(create_function/get_function), and the gateway permissions, plus "
        "iam:PassRole on both agp-* roles (research §9); (2) Bedrock AgentCore "
        "(Preview) is available in region %r — it is NOT in eu-central-1/Frankfurt; "
        "supported regions include us-east-1, us-west-2, eu-west-1 (research §11); "
        "(3) boto3 is recent enough to expose 'bedrock-agentcore-control'. Re-running "
        "is safe — every step is get-or-create and resumes cleanly.",
        region,
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Bootstrap THREE AgentCore Gateways (Contact Center, FNOL, Insurance "
            "Support) in one command: per domain creates the Lambda exec role, the "
            "Lambda (its tools), the gateway service role, then the gateway + target "
            "(reusing create_example_gateway with the domain's tool schema). Prints each "
            "gatewayUrl/gatewayArn AND writes them to use_cases_gateways.txt. Dev "
            "helper — the live create is your step (needs AWS creds with IAM + Lambda + "
            "gateway permissions and iam:PassRole on both roles per domain)."
        ),
        epilog=(
            "Everything it creates is namespaced agp-* and idempotent "
            "(get-or-create), so re-running is safe and converges. It NEVER deletes, "
            "modifies, or touches any resource it did not create (the only file it writes "
            "is its own output file). Per-domain continue-on-error: a failure is logged "
            "and the loop continues; the script exits non-zero if any domain failed."
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
        default="agp",
        help=(
            "Umbrella prefix for the resource names (default: agp). NOTE: the "
            "per-domain prefixes (agp-contact-center / agp-fnol / "
            "agp-insurance-support) come from the DEMO_USE_CASES registry; this flag "
            "is accepted for parity with bootstrap_demo_gateway.py."
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
        "--only",
        default=None,
        help=(
            "Comma-separated subset of domain keys to run (e.g. --only fnol, or "
            "--only contact-center,fnol). Default: all three. Known keys: "
            "contact-center, fnol, insurance-support."
        ),
    )
    parser.add_argument(
        "--output-file",
        default=_DEFAULT_OUTPUT_FILE,
        help=(
            "Where to write the succeeded gateways' URLs/ARNs (default: "
            "use_cases_gateways.txt next to this script). Overwritten each run."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Per domain: ast.parse the handler + assert every tool has a handler branch "
            "(drift guard), then print the resource names + CreateGateway/"
            "CreateGatewayTarget call shapes WITHOUT making any boto3/IAM/Lambda call. "
            "Exit 0."
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

    try:
        domains = _select_domains(args.only)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    if args.dry_run:
        # ZERO boto3 calls — we never construct a client on this path. The drift guard
        # (ast.parse + tool-branch check) runs inside _print_dry_run_domain and raises a
        # clear ValueError on a syntax error or a schema/handler mismatch.
        try:
            _print_dry_run(args.region, args.tenant_id, domains)
        except ValueError as exc:
            logger.error("Dry-run drift guard FAILED: %s", exc)
            return 1
        return 0

    # Live path: per-domain continue-on-error. Each domain is attempted independently;
    # a failure is logged + recorded and the loop continues to the next domain.
    successes = []
    failures = []
    for domain in domains:
        logger.info("=== Bootstrapping gateway: %s (%s) ===",
                    domain["display_name"], domain["key"])
        try:
            summary = bootstrap_one(args.region, args.tenant_id, domain)
            successes.append(summary)
            logger.info("Domain %s is READY.", domain["key"])
        except Exception as exc:  # noqa: BLE001 — surface a clean, actionable message
            # botocore exceptions (ClientError, EndpointConnectionError, NoCredentials,
            # UnknownServiceError, AccessDenied, etc.) plus the poll TimeoutError/
            # RuntimeError are caught broadly so one domain's failure never aborts the run.
            logger.error("Failed to bootstrap gateway for domain %s: %s",
                         domain["key"], exc)
            _print_failure_hint(args.region)
            failures.append({"key": domain["key"], "display_name": domain["display_name"],
                             "error": str(exc)})

    # Always (over)write the output file with the domains that succeeded this run.
    _write_output_file(args.output_file, successes)

    # Print each succeeded gateway's block (same style as the existing bootstrap summary).
    if successes:
        print("\nUse-case gateways that are READY (paste into the UI to register):")
        for summary in successes:
            print(f"\n## {summary['display_name']}  ({summary['gateway_name']})")
            print(f"  Lambda exec role : {summary['exec_role_name']}  ({summary['exec_role_arn']})")
            print(f"  Lambda           : {summary['lambda_name']}  ({summary['lambda_arn']})")
            print(f"  Gateway role     : {summary['gateway_role_name']}  ({summary['gateway_role_arn']})")
            print(f"gatewayUrl: {summary['gateway_url']}")
            print(f"gatewayArn: {summary['gateway_arn']}")
        print(f"\nWrote the above to {args.output_file}")

    # Final summary: successes/failures.
    print(f"\nSummary: {len(successes)} succeeded, {len(failures)} failed "
          f"(of {len(domains)} selected).")
    for summary in successes:
        print(f"  OK   {summary['key']}  ->  {summary['gateway_name']}")
    for failure in failures:
        print(f"  FAIL {failure['key']}  ->  {failure['error']}")

    if failures:
        print("\nTo clean up later, delete the agp-* resources listed above (this "
              "script never deletes anything). Re-running is safe (get-or-create); use "
              "--only <key> to retry a single failed domain.")
        # Non-zero exit if any domain failed (successes are still printed + written).
        return 1

    print("\nTo clean up later, delete the agp-* resources listed above (this script "
          "never deletes anything).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
