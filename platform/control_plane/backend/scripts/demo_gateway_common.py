#!/usr/bin/env python
"""Shared IAM/Lambda primitives for the demo AgentCore Gateway bootstraps.

This module is the DRY extraction point for the two demo-gateway bootstraps:
`bootstrap_demo_gateway.py` (the original echo/add helper) and
`bootstrap_demo_use_cases.py` (the three insurance-domain gateways). The IAM
get-or-create role/policy helpers and the inline-Lambda get-or-create were
ORIGINALLY defined inside `bootstrap_demo_gateway.py`; they are relocated here
VERBATIM (logic unchanged) so both bootstraps import them instead of copy-pasting.

What lives here (no domain knowledge, no gateway-specific logic):

  * Constants: the Lambda exec policy ARN, the two trust policies, the gateway
    invoke inline-policy name, the create-retry bounds, and the runtime/handler.
  * `ensure_role` / `ensure_attached_policy` / `ensure_inline_policy` — the
    get-or-create IAM primitives (never touch a role they didn't just create).
  * `ensure_lambda` — get-or-create the inline Lambda, with the IAM
    eventual-consistency retry. Takes the handler SOURCE as a parameter (so each
    domain zips its own handler) and an optional `description`.
  * `_gateway_invoke_policy_doc` — the least-privilege lambda:InvokeFunction doc, ALSO granting the E8 Cedar policy-engine read/evaluate actions (GetPolicyEngine/AuthorizeAction/PartiallyAuthorizeActions).
  * `zip_handler_bytes(src)` — PUBLIC zip helper that takes the handler source as
    a parameter (the old private `_zip_handler_bytes` hard-coded the echo/add
    source; the shared version lets each domain zip its own handler).

boto3/botocore are imported LAZILY inside the functions (matching the bootstraps),
so importing this module is side-effect-free and a dry-run never constructs a client.
"""

import io
import json
import logging
import time
import zipfile

logger = logging.getLogger("demo_gateway_common")

# AWS-managed policy that grants the Lambda CloudWatch Logs write (the minimal exec
# role for a logging Lambda).
_LAMBDA_BASIC_EXEC_POLICY_ARN = (
    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
)

# Trust policies. Each role trusts exactly one principal (least privilege).
_LAMBDA_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}
_GATEWAY_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}

# Inline policy name for the gateway role's lambda:InvokeFunction grant.
_GATEWAY_INVOKE_POLICY_NAME = "agp-demo-invoke-demo-lambda"

# IAM is eventually consistent: create_function right after create_role can fail with
# InvalidParameterValueException ("The role defined for the function cannot be assumed
# by Lambda."). Mirror the gateway poll loops' patience with a bounded retry.
_LAMBDA_CREATE_ATTEMPTS = 12
_LAMBDA_CREATE_DELAY = 5.0

_LAMBDA_RUNTIME = "python3.12"
_LAMBDA_HANDLER = "index.handler"

# The default Lambda description — today's echo/add string, so the original bootstrap
# (which omits the `description` arg) creates a byte-identical Lambda to before.
_DEFAULT_LAMBDA_DESCRIPTION = "Agentic Governance Platform demo MCP tools (echo/add) — dev helper."


def zip_handler_bytes(src: str) -> bytes:
    """Zip the handler `src` in-memory as index.py (no file on disk required).

    PUBLIC (was the private `_zip_handler_bytes`, which hard-coded the echo/add
    LAMBDA_HANDLER_SRC); taking the source as a parameter lets each domain zip its
    own handler from this one shared helper.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.py", src)
    return buf.getvalue()


def ensure_role(
    iam,
    role_name: str,
    trust_policy: dict,
    description: str,
) -> tuple:
    """Get-or-create an IAM role keyed on `role_name`; return (arn, created).

    get_role(RoleName) -> on success, return (arn, False) — reuse WITHOUT attaching
    any policies (the prior run already set them up; we never touch a role we didn't
    just create, preserving the "never touches any resource it didn't create" guarantee).
    On NoSuchEntity: create_role, return (arn, True) — the caller must then attach
    whatever policies the new role needs.
    """
    from botocore.exceptions import ClientError

    try:
        resp = iam.get_role(RoleName=role_name)
        arn = resp["Role"]["Arn"]
        logger.info(
            "Reusing existing IAM role %s -> %s (skipping policy attach — "
            "prior run already set it up)",
            role_name,
            arn,
        )
        return arn, False
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
            raise

    logger.info("Creating IAM role %s ...", role_name)
    resp = iam.create_role(
        RoleName=role_name,
        AssumeRolePolicyDocument=json.dumps(trust_policy),
        Description=description,
    )
    arn = resp["Role"]["Arn"]
    logger.info("Created IAM role %s -> %s", role_name, arn)
    return arn, True


def ensure_attached_policy(iam, role_name: str, policy_arn: str) -> None:
    """Attach an AWS-managed policy to a role.

    Only called on the create branch (when ensure_role returned created=True), so this
    never attaches a policy to a role the script didn't just create.
    """
    logger.info("Attaching managed policy %s to role %s", policy_arn, role_name)
    iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)


def ensure_inline_policy(
    iam, role_name: str, policy_name: str, policy_doc: dict
) -> None:
    """Put an inline policy on a role.

    Only called on the create branch (when ensure_role returned created=True), so this
    never overwrites a policy on a role the script didn't just create.
    """
    logger.info("Ensuring inline policy %s on role %s", policy_name, role_name)
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=policy_name,
        PolicyDocument=json.dumps(policy_doc),
    )


def ensure_lambda(
    lam,
    function_name: str,
    role_arn: str,
    handler_src: str,
    description: str = None,
) -> str:
    """Get-or-create the demo Lambda keyed on `function_name`; return its FunctionArn.

    get_function -> reuse on success (does NOT update code — keep re-runs simple/safe).
    On ResourceNotFoundException, create_function with the zipped `handler_src`,
    wrapped in a bounded retry for the IAM-eventual-consistency
    InvalidParameterValueException ("role cannot be assumed").

    `handler_src` is the handler SOURCE to zip (each domain passes its own). `description`
    defaults to the echo/add string so the original bootstrap (which omits it) is
    byte-identical to before; each domain may pass its own.
    """
    from botocore.exceptions import ClientError

    if description is None:
        description = _DEFAULT_LAMBDA_DESCRIPTION

    try:
        resp = lam.get_function(FunctionName=function_name)
        arn = resp["Configuration"]["FunctionArn"]
        logger.info("Reusing existing Lambda %s -> %s", function_name, arn)
        return arn
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise

    zip_bytes = zip_handler_bytes(handler_src)
    last_exc = None
    for attempt in range(1, _LAMBDA_CREATE_ATTEMPTS + 1):
        try:
            resp = lam.create_function(
                FunctionName=function_name,
                Runtime=_LAMBDA_RUNTIME,
                Role=role_arn,
                Handler=_LAMBDA_HANDLER,
                Code={"ZipFile": zip_bytes},
                Description=description,
                Timeout=15,
                MemorySize=128,
            )
            arn = resp["FunctionArn"]
            logger.info("Created Lambda %s -> %s", function_name, arn)
            return arn
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "InvalidParameterValueException" and attempt < _LAMBDA_CREATE_ATTEMPTS:
                # IAM hasn't propagated the new role yet — wait and retry.
                logger.info(
                    "create_function attempt %d/%d: role not yet assumable (IAM "
                    "eventual consistency), retrying in %.0fs ...",
                    attempt,
                    _LAMBDA_CREATE_ATTEMPTS,
                    _LAMBDA_CREATE_DELAY,
                )
                last_exc = exc
                time.sleep(_LAMBDA_CREATE_DELAY)
                continue
            raise

    raise RuntimeError(
        f"create_function for {function_name} kept failing on IAM eventual "
        f"consistency after {_LAMBDA_CREATE_ATTEMPTS} attempts; last error: {last_exc}"
    )


def _gateway_invoke_policy_doc(lambda_arn: str) -> dict:
    """Inline policy for the gateway service role:
      1. lambda:InvokeFunction scoped to THIS Lambda ARN (least privilege — the demo
         gateway's only target is this Lambda).
      2. bedrock-agentcore policy-engine read/evaluate (E8 Cedar): when a Cedar Policy
         Engine is attached, AgentCore uses the GATEWAY's role to GetPolicyEngine at
         attach time and to AuthorizeAction / PartiallyAuthorizeActions at request time.
         Without these the UpdateGateway attach is denied and all tool calls default-deny.
         Resource="*" — the engine ARN isn't known at role-create time and these are
         read/evaluate-only.
    """
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "lambda:InvokeFunction",
                "Resource": lambda_arn,
            },
            {
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:GetPolicyEngine",
                    "bedrock-agentcore:AuthorizeAction",
                    "bedrock-agentcore:PartiallyAuthorizeActions",
                ],
                "Resource": "*",
            },
        ],
    }
