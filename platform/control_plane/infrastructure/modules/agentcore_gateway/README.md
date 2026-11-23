# agentcore_gateway — platform-owned AgentCore Gateway module (E40)

Turns an MCP repo's `gateway.yaml` + OpenAPI spec into a governed AgentCore Gateway. Rolled out
to each connected org as **`agp-gateway-infra`** (staged in S3 as
`gateway-module/agentcore_gateway.zip` by `infrastructure/runtime_module.tf`) and applied by the
CodeBuild `agentcore_gateway` branch. CodeBuild applies the **org repo copy** — a module edit is
inert until a rollout with overwrite.

## What it creates

| Resource | Name | Notes |
| --- | --- | --- |
| `aws_iam_role` | `agp-<mcp_name>-<stage>-gateway-role` | Trust `bedrock-agentcore.amazonaws.com`, conditioned on `aws:SourceAccount` = this account. Name matches the ECS task role's PassRole pattern `role/agp-*-gateway-role` — required for the backend's `UpdateGateway`. |
| `aws_bedrockagentcore_api_key_credential_provider` | `agp-<mcp_name>-<stage>-api-key` | `api_key_secret_source = "EXTERNAL"`: points at the customer's secret; no key value in state. |
| `aws_bedrockagentcore_gateway` | `agp-<mcp_name>-<stage>` | `MCP`, `CUSTOM_JWT` (Entra, both audience forms, `allowed_scopes = ["Invoke"]`). |
| `aws_bedrockagentcore_gateway_target` | `api` | OpenAPI spec inlined; API-key credentials from `gateway.yaml`. |

State key: `agentcore-gateway/<mcp_server_id>/<stage>/terraform.tfstate` (the `state_key` output).

## Inputs

Platform tfvars (written by the buildspec): `mcp_server_id`, `mcp_name`, `stage`, `region`,
`deploy_role_arn`, `tenant_id`, `entra_app_audience`, `entra_app_id`, `gateway_config_path`,
`openapi_spec_path`. Developer-owned, in the MCP repo: `gateway.yaml` (see the
`gateway-agentcore` template README for its keys) and the OpenAPI spec it names.

Outputs: `gateway_arn`, `gateway_id`, `gateway_url`, `state_key`.

## Backend-owned attributes (drift)

The backend attaches the Cedar policy engine and replays the authorizer with `UpdateGateway`.
Both are in `lifecycle.ignore_changes`; removing either makes the next apply silently undo
governance (detached ENFORCE, or a reset audience).

## Inbound authorizer: delegated tokens only

`allowed_scopes = ["Invoke"]`. Measured in the E40/T1 spike: an app-only (client_credentials)
token for the MCP audience carries no `scp` and passes an audience-only authorizer; with Invoke
required it gets 403 `insufficient_scope`, while the harness's OBO token (`scp=Invoke`) passes.
Do not use `custom_claim` on `scp` — AgentCore rejects it and the gateway goes
`UPDATE_UNSUCCESSFUL`. The backend's Cedar attach/detach replays the authorizer verbatim, so the
scope survives it.

## Gateway role IAM

| Grant | Why |
| --- | --- |
| `GetPolicyEngine`, `AuthorizeAction`, `PartiallyAuthorizeActions` on `*` | Cedar attach + per-request evaluation |
| `GetWorkloadAccessToken`, `GetResourceApiKey` (account+region scoped) | API-key outbound via the token vault — **measured in the E40/T1 spike** |
| `secretsmanager:GetSecretValue` on `secret:agp-mcp/<mcp_name>/*` (this account + region, from data sources) | EXTERNAL secret read |

All three API-key grants are required: without any one, the target still goes READY but every
tool call fails with a generic "internal error" (pinned by `test_gateway_module_contract.py`).

**Secret namespace.** `api_key.secret_arn` must be a full ARN (with the 6-character suffix) in
this account and region, named under `agp-mcp/<mcp_name>/` — a precondition refuses anything
else at plan. The grant is built from that namespace, never from the developer's string, so a
repo pusher cannot aim the gateway at a platform secret and have it sent to their own server.

**Deploy role.** `CreateApiKeyCredentialProvider` reads the EXTERNAL secret with the CALLER's
credentials, so the role this module's provider assumes (`deploy_role_arn`, the default tenant's
`agp-deployment-<prefix>-default`) also needs `secretsmanager:GetSecretValue` +
`secretsmanager:DescribeSecret` on `secret:agp-mcp/*`. Granted in `modules/default_tenant`; without
it the apply fails at the credential provider with `AccessDeniedException`.

**Customer-managed KMS key.** A secret on the default `aws/secretsmanager` key needs nothing
more. A CMK needs this statement in the KEY policy (not granted here). Never grant
`kms:Decrypt` on `*`:

```json
{
  "Sid": "AllowAgpGatewayToDecryptItsApiKeySecret",
  "Effect": "Allow",
  "Principal": { "AWS": "arn:aws:iam::<account-id>:role/agp-<mcp_name>-<stage>-gateway-role" },
  "Action": "kms:Decrypt",
  "Resource": "*",
  "Condition": {
    "StringEquals": { "kms:ViaService": "secretsmanager.<region>.amazonaws.com" },
    "StringLike": {
      "kms:EncryptionContext:SecretARN": "arn:aws:secretsmanager:<region>:<account-id>:secret:agp-mcp/<mcp_name>/*"
    }
  }
}
```

(`Resource: "*"` in a key policy means *this key*.) The deploy role reads the secret at create
time too, so add its ARN to the same statement's `Principal`.

## Provider floor

`>= 6.66, < 7.0` — the EXTERNAL secret source arrived in 6.66. Not `api_key_wo`: write-only
arguments need Terraform >= 1.11 and CodeBuild runs 1.5.7.

## Scope

Platform account only: the backend's gateway clients are platform-account, so a tenant-account
gateway could not be governed. The buildspec still passes the default tenant's deploy role, which
lives in the platform account. Hand-built tenant-account deploy roles need no change for E40.
