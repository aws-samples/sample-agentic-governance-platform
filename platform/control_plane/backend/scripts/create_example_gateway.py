#!/usr/bin/env python
"""Stand up an example AgentCore Gateway (with one target) so you have an MCP to register.

This is a DEV HELPER / USER step. The Agentic Governance Platform is *register-only* for
production (the user creates the Gateway / Runtime-MCP; the platform wires the
inbound authorizer, scans tools, and grants access — it does NOT call
`CreateGateway`/`CreateGatewayTarget` in the product flow, research §8/§8.1). This
script is the exception: a one-command way to create a demo gateway so the
register-only flow has something to point at. It makes live
`bedrock-agentcore-control` calls (Bedrock AgentCore, Public Preview) and therefore
needs AWS credentials with the gateway permissions (research §9). It is NOT run by
automation; you run it once, then paste the printed `gatewayUrl` + `gatewayArn`
into the UI to register the MCP server.

What it does (research §8.1):
  1. `CreateGateway(name, roleArn=<--role-arn>, protocolType="MCP",
     authorizerType="CUSTOM_JWT", authorizerConfiguration={customJWTAuthorizer:
     {discoveryUrl, allowedAudience:[<placeholder>]}})`. The gateway is born LOCKED
     (CUSTOM_JWT) on purpose: a live `UpdateGateway` proved the `authorizerType` is
     IMMUTABLE after create (`ValidationException: Authorizer type cannot be updated
     for an existing gateway`). So we can no longer create as NONE and flip later —
     we must create as CUSTOM_JWT up-front with a *placeholder* authorizer config.
     The platform's registration step (UpdateGateway, research §1.1) then keeps the
     type CUSTOM_JWT (unchanged → allowed) and OVERWRITES the customJWTAuthorizer
     with the real per-MCP discoveryUrl + audience. (Only the *type* is immutable,
     not the config.) Consequence: because the gateway is CUSTOM_JWT from birth, the
     platform's pre-lockdown token-less tool-scan is skipped (existing provision()
     behavior when the authorizer is already CUSTOM_JWT). Fine for this demo — the
     inline-lambda target's tools come from its inline `toolSchema`, so the catalog
     still populates without a wire scan.
  2. Poll `GetGateway` until `status == READY` (enum
     CREATING|UPDATING|UPDATE_UNSUCCESSFUL|DELETING|READY|FAILED). On a terminal
     failure status the `statusReasons` are surfaced.
  3. `CreateGatewayTarget` with ONE of:
       OPTION A (default, `--lambda-arn`): an inline `lambda` target whose
         `targetConfiguration.mcp.lambda.toolSchema.inlinePayload` carries a small
         1-2 tool demo schema. Inline tool schemas read natively from
         `GetGatewayTarget` (no wire scan / token needed), so the demo tools are
         real and immediately scannable (research §8.1, §6.2). The gateway's own
         IAM role invokes the lambda, so `credentialProviderConfigurations` is a
         single `GATEWAY_IAM_ROLE` entry.
       OPTION B (`--remote-url`): a remote `mcpServer` target
         `{endpoint, listingMode:"DYNAMIC"}` — a DYNAMIC target whose tools are
         discovered by the server-side refresh / wire scan.
     `--lambda-arn` and `--remote-url` are mutually exclusive; exactly one is
     required for a live run.
  4. Poll `GetGatewayTarget` until `status == READY` (the target status enum adds
     SYNCHRONIZING / *_PENDING_AUTH transitional states).
  5. Print the `gatewayUrl` and `gatewayArn` (verbatim from the API response —
     never hand-construct the URL, research §1.3). Those are the two values you
     paste into the UI to register the MCP server.

Prerequisite (you create this out-of-band; this script does NOT create it):
  A gateway SERVICE ROLE whose trust policy trusts `bedrock-agentcore.amazonaws.com`
  and (for a lambda target) grants `lambda:InvokeFunction` on the demo lambda. The
  easy path is to attach the AWS managed policy `BedrockAgentCoreFullAccess` to that
  role. `CreateGateway` also needs `iam:PassRole` on this role for the caller
  (research §9). Pass the role's ARN as `--role-arn`.

Run from the backend dir (PYTHONPATH=src matches the other scripts; this module is
import-side-effect-free so PYTHONPATH is not strictly required, but keep it for
parity with `ensure_mcp_registry.py`):

    cd platform/control_plane/backend && \
        PYTHONPATH=src venv/bin/python scripts/create_example_gateway.py \
            --gateway-name demo-mcp \
            --role-arn arn:aws:iam::123456789012:role/demo-gateway-role \
            --lambda-arn arn:aws:lambda:us-east-1:123456789012:function:demo-mcp-tools

Validate the call shapes offline first (makes ZERO boto3 calls, exits 0):

    PYTHONPATH=src venv/bin/python scripts/create_example_gateway.py --dry-run \
        --gateway-name demo-mcp \
        --role-arn arn:aws:iam::123456789012:role/demo-gateway-role \
        --lambda-arn arn:aws:lambda:us-east-1:123456789012:function:demo-mcp-tools

Region caveat (research §11): AgentCore is Preview and is NOT available in
eu-central-1 (Frankfurt). The default region is us-east-1; supported regions
include us-east-1, us-west-2, eu-west-1. If the service is unavailable in the
chosen region (or access is denied / the endpoint can't be resolved) the script
prints an actionable message and exits non-zero instead of crashing with a raw
traceback.
"""

import argparse
import json
import logging
import sys
import time
import uuid

logger = logging.getLogger("create_example_gateway")

# Control-plane status fields — terminal "READY" (research §8.1). The exact key is
# `status` on both Get responses; probe a couple of alternatives defensively in
# case the shape differs (mirrors ensure_mcp_registry.py).
_STATUS_KEYS = ("status", "gatewayStatus", "targetStatus")
_READY_STATUS = "READY"
# Terminal failure statuses for the gateway (research §1.1 enum) and the target
# (research §8.1 enum adds SYNCHRONIZE_UNSUCCESSFUL). Anything not READY and not
# terminal-failure is treated as still-in-progress -> keep polling.
_GATEWAY_FAILED_STATUSES = ("FAILED", "UPDATE_UNSUCCESSFUL", "DELETING")
_TARGET_FAILED_STATUSES = (
    "FAILED",
    "UPDATE_UNSUCCESSFUL",
    "SYNCHRONIZE_UNSUCCESSFUL",
    "DELETING",
)

# Bounded poll-to-READY loop (mirror ensure_mcp_registry.py): ~45 attempts x 4s
# ~= 3 min. Gateway/target creation is a touch slower than a registry, so the
# window is a little wider than the registry script's 60s.
_READY_POLL_ATTEMPTS = 45
_READY_POLL_DELAY = 4.0

# A tiny but real demo tool schema for the inline lambda target (OPTION A). Each
# tool is {name, description, inputSchema} per the CreateGatewayTarget
# `lambda.toolSchema.inlinePayload` (list-of-tool) shape (research §8.1, verified
# against botocore 1.43.16: inputSchema.type enum includes "object"). Two tools so
# the demo `tools/list` is non-trivial.
_DEMO_TOOL_SCHEMA = [
    {
        "name": "echo",
        "description": "Echo back the provided message (demo tool).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The message to echo back.",
                }
            },
            "required": ["message"],
        },
    },
    {
        "name": "add",
        "description": "Add two integers and return the sum (demo tool).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "integer", "description": "First addend."},
                "b": {"type": "integer", "description": "Second addend."},
            },
            "required": ["a", "b"],
        },
    },
]


def _status_from_resp(resp: dict):
    """Best-effort extraction of a status from a GetGateway/GetGatewayTarget response.

    The verified key is `status`; probe a couple of alternatives defensively in
    case the response shape differs (mirrors ensure_mcp_registry.py).
    """
    for key in _STATUS_KEYS:
        value = resp.get(key)
        if value:
            return value
    return None


# Default Entra tenant for the demo.
# The discoveryUrl built from this is only a SANE DEFAULT at create time — the platform
# re-sets it (along with the real audience) at registration, so a wrong one self-heals.
_DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"
# Obviously-a-placeholder audience: it is OVERWRITTEN at registration with the real
# per-MCP audience. It exists only because authorizerType=CUSTOM_JWT requires a non-empty
# customJWTAuthorizer at create time (the type is immutable post-create — see below).
_PLACEHOLDER_AUDIENCE = "api://agp-mcp-PLACEHOLDER"


def _discovery_url(tenant_id: str, login_base: str = "https://login.microsoftonline.com") -> str:
    """The Entra v2 OIDC well-known URL for a tenant — mirrors the backend
    mcp_identity_service._discovery_url() so the create-time placeholder matches the
    shape the platform re-sets at registration."""
    return f"{login_base.rstrip('/')}/{tenant_id}/v2.0/.well-known/openid-configuration"


def build_create_gateway_kwargs(
    name: str,
    role_arn: str,
    discovery_url: str,
    placeholder_audience: str = _PLACEHOLDER_AUDIENCE,
) -> dict:
    """The exact kwargs CreateGateway would receive (research §8.1).

    authorizerType="CUSTOM_JWT" (was "NONE") because a live `UpdateGateway` proved the
    authorizerType is IMMUTABLE after create (`ValidationException: Authorizer type
    cannot be updated for an existing gateway`). We can no longer create as NONE and
    let the platform flip it to CUSTOM_JWT — that flip is impossible. So we create the
    gateway ALREADY CUSTOM_JWT with a *placeholder* customJWTAuthorizer config. At
    registration the platform keeps the type CUSTOM_JWT (unchanged → allowed by AWS)
    and OVERWRITES the whole customJWTAuthorizer with the real discoveryUrl + per-MCP
    audience (only the *type* is immutable, not the config).

    Notes:
      * `placeholder_audience` is a DELIBERATE stand-in (obviously-a-placeholder); the
        platform replaces it with the real per-MCP audience at registration.
      * `discovery_url` here is a sane default (the demo tenant's well-known URL); the
        platform RE-SETS it at registration too, so even a wrong discoveryUrl self-heals.

    protocolType="MCP". The clientToken is a FRESH random uuid4 per create (the AWS API
    requires clientToken min length 33; uuid4 is 36 chars). It was previously a
    deterministic uuid5 keyed on the name to make a racing double-invocation idempotent
    — but that BREAKS delete-then-recreate: AWS caches a clientToken in its idempotency
    window (hours) even after the resource is deleted, so deleting the demo gateway and
    re-running hit `ConflictException: gateway with the same clientToken already exists`.
    Idempotency is now provided by the get-or-create-by-name lookup in
    create_example_gateway (reuse the existing gateway instead of re-creating it); a
    fresh uuid4 per create means a delete-then-recreate has no cached-token conflict.
    """
    return {
        "name": name,
        "roleArn": role_arn,
        "protocolType": "MCP",
        "authorizerType": "CUSTOM_JWT",
        "authorizerConfiguration": {
            "customJWTAuthorizer": {
                "discoveryUrl": discovery_url,
                "allowedAudience": [placeholder_audience],
            }
        },
        "clientToken": str(uuid.uuid4()),
    }


def build_create_target_kwargs(
    gateway_identifier: str,
    target_name: str,
    lambda_arn: str = None,
    remote_url: str = None,
    tool_schema: list = None,
) -> dict:
    """The exact kwargs CreateGatewayTarget would receive (research §8.1).

    Exactly one of `lambda_arn` / `remote_url` must be set (the caller enforces
    this; we assert defensively).

    OPTION A (lambda_arn): inline `lambda` target. The gateway's IAM role invokes
    the lambda, so `credentialProviderConfigurations` is a single
    `GATEWAY_IAM_ROLE` entry (botocore enum value, research §8.1). The tool schema
    is carried inline so `GetGatewayTarget` reads the tools natively.

    `tool_schema` is the inline tool schema (list-of-tool dicts) for the lambda
    target's `toolSchema.inlinePayload`. It DEFAULTS to the echo/add `_DEMO_TOOL_SCHEMA`
    when omitted (backward-compatible — the original echo/add behavior is unchanged),
    so each demo domain can pass its own per-domain schema (the use-case bootstrap
    does). Only consulted on the lambda (OPTION A) path.

    OPTION B (remote_url): remote `mcpServer` target with listingMode=DYNAMIC.

    The clientToken is a FRESH random uuid4 per create (not a deterministic uuid5).
    AWS caches a clientToken in its idempotency window (hours) even after the resource
    is deleted, so a deterministic token keyed on the name would block
    delete-then-recreate with `ConflictException`. Idempotency is instead provided by
    the get-or-create-by-name lookup in create_example_gateway (reuse the existing
    target); a fresh uuid4 avoids the cached-token conflict on a clean recreate.
    """
    if bool(lambda_arn) == bool(remote_url):
        raise ValueError(
            "exactly one of lambda_arn / remote_url must be provided to build the target kwargs"
        )

    # Default to the echo/add schema when none is passed (backward-compat). Each
    # demo domain passes its own schema; omitting it preserves the original behavior.
    if tool_schema is None:
        tool_schema = _DEMO_TOOL_SCHEMA

    kwargs = {
        "gatewayIdentifier": gateway_identifier,
        "name": target_name,
        "clientToken": str(uuid.uuid4()),
    }

    if lambda_arn:
        # research §8.1 OPTION A — inline lambda target. The gateway role invokes
        # the lambda; GATEWAY_IAM_ROLE means "use the gateway's own service role"
        # (no separate credential provider object).  # research §8.1
        kwargs["targetConfiguration"] = {
            "mcp": {
                "lambda": {
                    "lambdaArn": lambda_arn,
                    "toolSchema": {"inlinePayload": tool_schema},
                }
            }
        }
        kwargs["credentialProviderConfigurations"] = [
            {"credentialProviderType": "GATEWAY_IAM_ROLE"}  # research §8.1
        ]
    else:
        # research §8.1 OPTION B — remote mcpServer target. DYNAMIC listing means
        # the tools are discovered server-side / via the wire scan (no inline
        # schema). No credentialProviderConfigurations for a bare demo endpoint.
        kwargs["targetConfiguration"] = {
            "mcp": {
                "mcpServer": {
                    "endpoint": remote_url,
                    "listingMode": "DYNAMIC",  # research §8.1
                }
            }
        }

    return kwargs


def wait_for_gateway_ready(
    ctl,
    gateway_identifier: str,
    attempts: int = _READY_POLL_ATTEMPTS,
    delay: float = _READY_POLL_DELAY,
) -> dict:
    """Poll GetGateway until status == "READY" (research §8.1). Returns the final response.

    A freshly-created gateway is CREATING then READY; creating a target before the
    gateway is READY can fail. Transient not-ready states (CREATING / UPDATING /
    an unreadable status, or a transient ConflictException) are expected during the
    wait — we keep polling. A terminal failure status (FAILED / UPDATE_UNSUCCESSFUL
    / DELETING) or exhausting the bounded loop is an error; on a failure status the
    `statusReasons` are surfaced.
    """
    from botocore.exceptions import ClientError

    last_status = None
    last_resp = {}
    for attempt in range(1, attempts + 1):
        try:
            resp = ctl.get_gateway(gatewayIdentifier=gateway_identifier)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConflictException":
                logger.debug(
                    "get_gateway attempt %d/%d: transient ConflictException, retrying ...",
                    attempt,
                    attempts,
                )
                time.sleep(delay)
                continue
            raise

        last_resp = resp
        status = _status_from_resp(resp)
        last_status = status
        logger.debug("get_gateway attempt %d/%d: status=%r", attempt, attempts, status)
        if status == _READY_STATUS:
            logger.info("Gateway %s is READY.", gateway_identifier)
            return resp
        if status in _GATEWAY_FAILED_STATUSES:
            reasons = resp.get("statusReasons") or []
            raise RuntimeError(
                f"Gateway {gateway_identifier} reached terminal status {status!r}; "
                f"statusReasons={reasons}"
            )
        # CREATING / UPDATING / unknown-but-non-terminal / absent status -> keep polling.
        time.sleep(delay)

    raise TimeoutError(
        f"Gateway {gateway_identifier} did not reach {_READY_STATUS!r} within "
        f"{attempts} attempts (last status: {last_status!r}); last statusReasons="
        f"{last_resp.get('statusReasons')}"
    )


def wait_for_target_ready(
    ctl,
    gateway_identifier: str,
    target_id: str,
    attempts: int = _READY_POLL_ATTEMPTS,
    delay: float = _READY_POLL_DELAY,
) -> dict:
    """Poll GetGatewayTarget until status == "READY" (research §8.1). Returns the final response.

    The target status enum adds SYNCHRONIZING / *_PENDING_AUTH transitional states
    (research §8.1) — all treated as still-in-progress. A terminal failure status
    (FAILED / UPDATE_UNSUCCESSFUL / SYNCHRONIZE_UNSUCCESSFUL / DELETING) or
    exhausting the bounded loop is an error; failures surface `statusReasons`.
    """
    from botocore.exceptions import ClientError

    last_status = None
    last_resp = {}
    for attempt in range(1, attempts + 1):
        try:
            resp = ctl.get_gateway_target(
                gatewayIdentifier=gateway_identifier, targetId=target_id
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConflictException":
                logger.debug(
                    "get_gateway_target attempt %d/%d: transient ConflictException, retrying ...",
                    attempt,
                    attempts,
                )
                time.sleep(delay)
                continue
            raise

        last_resp = resp
        status = _status_from_resp(resp)
        last_status = status
        logger.debug(
            "get_gateway_target attempt %d/%d: status=%r", attempt, attempts, status
        )
        if status == _READY_STATUS:
            logger.info("Gateway target %s is READY.", target_id)
            return resp
        if status in _TARGET_FAILED_STATUSES:
            reasons = resp.get("statusReasons") or []
            raise RuntimeError(
                f"Gateway target {target_id} reached terminal status {status!r}; "
                f"statusReasons={reasons}"
            )
        # CREATING / UPDATING / SYNCHRONIZING / *_PENDING_AUTH / absent -> keep polling.
        time.sleep(delay)

    raise TimeoutError(
        f"Gateway target {target_id} did not reach {_READY_STATUS!r} within "
        f"{attempts} attempts (last status: {last_status!r}); last statusReasons="
        f"{last_resp.get('statusReasons')}"
    )


# Bounded paging guard for the list lookups (read-only). A handful of pages is far
# more than a demo account will ever have; the bound just stops a pathological
# nextToken loop. The list responses use `items` + `nextToken` (verified against the
# botocore 1.43.16 model); probe a couple of plausible key names defensively so an
# unexpected shape degrades to "not found" (-> attempt the create) rather than crashing.
_LIST_PAGE_LIMIT = 50
_LIST_ITEMS_KEYS = ("items", "gateways", "gatewayTargets", "targets")


def _find_gateway_by_name(ctl, gateway_name: str):
    """Look up an existing gateway by exact name; return its summary item or None.

    Pages list_gateways (handling nextToken) and matches on item["name"]. READ-only.
    Defensive: tolerates response-key variance with .get; if list_gateways is
    unavailable or returns an unexpected shape, the lookup yields None and the caller
    falls back to attempting the create (mirrors the ensure_* idiom in
    bootstrap_demo_gateway.py).
    """
    next_token = None
    for _ in range(_LIST_PAGE_LIMIT):
        kwargs = {"nextToken": next_token} if next_token else {}
        resp = ctl.list_gateways(**kwargs)
        for item in _items_from_list(resp):
            if item.get("name") == gateway_name and item.get("gatewayId"):
                return item
        next_token = resp.get("nextToken")
        if not next_token:
            break
    return None


def _find_target_by_name(ctl, gateway_identifier: str, target_name: str):
    """Look up an existing target by exact name within a gateway; return it or None.

    Pages list_gateway_targets(gatewayIdentifier=...) (handling nextToken) and matches
    on item["name"]. READ-only and defensive (see _find_gateway_by_name).
    """
    next_token = None
    for _ in range(_LIST_PAGE_LIMIT):
        kwargs = {"gatewayIdentifier": gateway_identifier}
        if next_token:
            kwargs["nextToken"] = next_token
        resp = ctl.list_gateway_targets(**kwargs)
        for item in _items_from_list(resp):
            if item.get("name") == target_name and item.get("targetId"):
                return item
        next_token = resp.get("nextToken")
        if not next_token:
            break
    return None


def _items_from_list(resp: dict) -> list:
    """Best-effort extraction of the list-of-resources from a List* response.

    The verified key is `items`; probe a couple of alternatives defensively so an
    unexpected response shape degrades to an empty list (-> caller attempts the create)
    rather than raising.
    """
    if not isinstance(resp, dict):
        return []
    for key in _LIST_ITEMS_KEYS:
        value = resp.get(key)
        if isinstance(value, list):
            return value
    return []


def create_example_gateway(
    gateway_name: str,
    role_arn: str,
    region: str,
    discovery_url: str,
    lambda_arn: str = None,
    remote_url: str = None,
    placeholder_audience: str = _PLACEHOLDER_AUDIENCE,
    tool_schema: list = None,
) -> dict:
    """Create the gateway + one target, wait for both READY. Returns the GetGateway response.

    Live path only — the caller gates this behind the non-dry-run branch so a
    dry-run never constructs a client or makes a call. Exactly one of `lambda_arn`
    / `remote_url` must be set (caller-enforced; build_* asserts it again).

    `tool_schema` is the inline tool schema for the lambda target; it DEFAULTS to the
    echo/add `_DEMO_TOOL_SCHEMA` when omitted (backward-compatible). Each demo domain
    passes its own schema (the use-case bootstrap does). Threaded to
    build_create_target_kwargs -> targetConfiguration.mcp.lambda.toolSchema.inlinePayload.

    The gateway is created CUSTOM_JWT with a placeholder customJWTAuthorizer
    (discovery_url + placeholder_audience); the platform overwrites that config at
    registration (see build_create_gateway_kwargs — the type is immutable post-create).

    Raises on AWS errors / poll timeouts.
    """
    import boto3  # imported lazily so importing this module is side-effect-free

    ctl = boto3.client("bedrock-agentcore-control", region_name=region)

    # Get-or-create the gateway BY NAME (idempotency). Re-running without deleting
    # reuses the existing gateway (no duplicate, no ConflictException); a fresh uuid4
    # clientToken on the create path means delete-then-recreate has no cached-token
    # conflict (AWS caches a clientToken past deletion — see build_create_gateway_kwargs).
    existing_gw = _find_gateway_by_name(ctl, gateway_name)
    if existing_gw:
        gateway_id = existing_gw["gatewayId"]
        logger.info(
            "Reusing existing gateway %r (gatewayId=%s); skipping CreateGateway.",
            gateway_name,
            gateway_id,
        )
    else:
        gw_kwargs = build_create_gateway_kwargs(
            gateway_name, role_arn, discovery_url, placeholder_audience=placeholder_audience
        )
        logger.info("Creating gateway %r in region %s ...", gateway_name, region)
        gw = ctl.create_gateway(**gw_kwargs)
        # CreateGateway returns gatewayId + gatewayArn + gatewayUrl directly (verified
        # against botocore 1.43.16). gatewayIdentifier for subsequent calls is the
        # short gatewayId, NOT the arn (research §8.1 / Spike G note).
        gateway_id = gw.get("gatewayId")
        if not gateway_id:
            raise RuntimeError(
                f"CreateGateway succeeded but no gatewayId in response; keys: {sorted(gw.keys())}"
            )
        logger.info(
            "Created gateway %r -> gatewayId=%s; waiting for READY ...", gateway_name, gateway_id
        )

    # Poll to READY either way — a reused gateway is typically already READY (the poll
    # returns on the first GetGateway), and we need the full GetGateway record (with
    # gatewayUrl/gatewayArn) to return regardless. We do NOT update/"fix" a reused
    # gateway's authorizer — the platform's registration step owns that config.
    gw_ready = wait_for_gateway_ready(ctl, gateway_id)

    target_name = f"{gateway_name}-target"
    # Get-or-create the target BY NAME within this gateway (same idempotency rationale).
    existing_tgt = _find_target_by_name(ctl, gateway_id, target_name)
    if existing_tgt:
        target_id = existing_tgt["targetId"]
        logger.info(
            "Reusing existing gateway target %r (targetId=%s); skipping CreateGatewayTarget.",
            target_name,
            target_id,
        )
    else:
        tgt_kwargs = build_create_target_kwargs(
            gateway_identifier=gateway_id,
            target_name=target_name,
            lambda_arn=lambda_arn,
            remote_url=remote_url,
            tool_schema=tool_schema,
        )
        option = "inline-lambda (OPTION A)" if lambda_arn else "remote mcpServer (OPTION B)"
        logger.info("Creating gateway target %r [%s] ...", target_name, option)
        tgt = ctl.create_gateway_target(**tgt_kwargs)
        target_id = tgt.get("targetId")
        if not target_id:
            raise RuntimeError(
                f"CreateGatewayTarget succeeded but no targetId in response; keys: {sorted(tgt.keys())}"
            )
        logger.info("Created gateway target -> targetId=%s; waiting for READY ...", target_id)
    wait_for_target_ready(ctl, gateway_id, target_id)

    # Return the gateway-ready response; gatewayUrl/gatewayArn come from it verbatim
    # (research §1.3 — never hand-construct the URL).
    return gw_ready


def _print_call_shapes(gw_kwargs: dict, tgt_kwargs: dict) -> None:
    """Pretty-print the exact CreateGateway / CreateGatewayTarget kwargs (dry-run)."""
    print("DRY RUN — no boto3 calls will be made. The script WOULD send:")
    print("\n  bedrock-agentcore-control.CreateGateway(")
    print(_indent(json.dumps(gw_kwargs, indent=2), 4))
    print("  )")
    print(
        "\n  # then poll GetGateway(gatewayIdentifier=<gatewayId from the response>) until status==READY\n"
    )
    print("  bedrock-agentcore-control.CreateGatewayTarget(")
    print(_indent(json.dumps(tgt_kwargs, indent=2), 4))
    print("  )")
    print(
        "\n  # then poll GetGatewayTarget(gatewayIdentifier=<gatewayId>, targetId=<targetId>) until status==READY"
    )
    print(
        "  # then print the gatewayUrl + gatewayArn (verbatim from the GetGateway response) to register the MCP server"
    )


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in text.splitlines())


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Stand up an example AgentCore Gateway (with one target) so you have an "
            "MCP to register. Dev helper — the live create is your step (needs AWS "
            "creds with the gateway permissions + iam:PassRole on the service role)."
        ),
        epilog=(
            "Prerequisite (you create it out-of-band, NOT this script): a gateway "
            "service role trusting bedrock-agentcore.amazonaws.com (+ "
            "lambda:InvokeFunction on the demo lambda for a lambda target). The easy "
            "path is to attach the AWS managed policy BedrockAgentCoreFullAccess to "
            "that role and pass its ARN as --role-arn. CreateGateway also needs "
            "iam:PassRole on that role for the caller."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--gateway-name",
        required=True,
        help="Name for the new gateway (also used to derive the target name <name>-target).",
    )
    parser.add_argument(
        "--role-arn",
        required=True,
        help=(
            "ARN of the gateway SERVICE ROLE (trusts bedrock-agentcore.amazonaws.com; "
            "+ lambda:InvokeFunction for a lambda target). BedrockAgentCoreFullAccess "
            "is the easy path. NOT created by this script."
        ),
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--lambda-arn",
        default=None,
        help=(
            "OPTION A (default target): ARN of a Lambda backing an inline-tool-schema "
            "target. Its tools read natively from GetGatewayTarget (no token needed). "
            "Mutually exclusive with --remote-url."
        ),
    )
    target.add_argument(
        "--remote-url",
        default=None,
        help=(
            "OPTION B: endpoint URL of a remote MCP server (mcpServer target, "
            "listingMode=DYNAMIC). Mutually exclusive with --lambda-arn."
        ),
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region hosting the gateway (default: us-east-1; AgentCore is NOT in eu-central-1).",
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
            "Validate the args + print the exact CreateGateway/CreateGatewayTarget "
            "call shapes WITHOUT making any boto3 call. Exit 0."
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

    # Exactly one of --lambda-arn / --remote-url is required for a target.
    # argparse's mutually_exclusive_group already forbids BOTH; we additionally
    # require that one is present (for the live run AND the dry-run validation).
    if not args.lambda_arn and not args.remote_url:
        logger.error(
            "exactly one of --lambda-arn (OPTION A inline lambda) or --remote-url "
            "(OPTION B remote mcpServer) is required."
        )
        return 2

    # Build the call shapes up-front so both the dry-run and the live path use the
    # SAME kwargs (the dry-run prints exactly what the live path would send). The
    # target name mirrors create_example_gateway's derivation.
    target_name = f"{args.gateway_name}-target"
    discovery = _discovery_url(args.tenant_id)
    gw_kwargs = build_create_gateway_kwargs(args.gateway_name, args.role_arn, discovery)
    tgt_kwargs = build_create_target_kwargs(
        gateway_identifier="<gatewayId from CreateGateway response>",
        target_name=target_name,
        lambda_arn=args.lambda_arn,
        remote_url=args.remote_url,
    )

    if args.dry_run:
        # ZERO boto3 calls — we never even import/construct a client on this path.
        _print_call_shapes(gw_kwargs, tgt_kwargs)
        print("\nDry run OK: args validated, call shapes printed, no AWS calls made.")
        return 0

    try:
        gw_ready = create_example_gateway(
            gateway_name=args.gateway_name,
            role_arn=args.role_arn,
            region=args.region,
            discovery_url=discovery,
            lambda_arn=args.lambda_arn,
            remote_url=args.remote_url,
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean, actionable message
        # botocore exceptions (ClientError, EndpointConnectionError, NoCredentials,
        # UnknownServiceError, AccessDenied, etc.) plus the poll TimeoutError/
        # RuntimeError are caught broadly so the user never sees a raw traceback.
        logger.error("Failed to create the example gateway: %s", exc)
        logger.error(
            "Check that: (1) AWS credentials are configured with the gateway "
            "permissions AND iam:PassRole on the --role-arn (research §9); (2) the "
            "--role-arn service role trusts bedrock-agentcore.amazonaws.com (+ "
            "lambda:InvokeFunction for a lambda target) — BedrockAgentCoreFullAccess "
            "is the easy path; (3) Bedrock AgentCore (Preview) is available in region "
            "%r — it is NOT in eu-central-1/Frankfurt; supported regions include "
            "us-east-1, us-west-2, eu-west-1 (research §11); (4) boto3 is recent "
            "enough to expose 'bedrock-agentcore-control'.",
            args.region,
        )
        return 1

    gateway_url = gw_ready.get("gatewayUrl")
    gateway_arn = gw_ready.get("gatewayArn")
    if not gateway_url or not gateway_arn:
        logger.error(
            "Gateway is READY but the response is missing gatewayUrl/gatewayArn; keys: %s",
            sorted(gw_ready.keys()),
        )
        return 1

    print("\nExample gateway is READY. Paste these into the UI to register the MCP server:")
    print(f"gatewayUrl: {gateway_url}")
    print(f"gatewayArn: {gateway_arn}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
