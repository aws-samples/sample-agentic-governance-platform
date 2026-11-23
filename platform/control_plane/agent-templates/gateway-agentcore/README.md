# AgentCore Gateway MCP Server Template (preview)

Expose an existing REST API as **governed MCP tools**. There is no code here: an OpenAPI spec and
`gateway.yaml` become a Bedrock **AgentCore Gateway**, and AGP governs it like any MCP server —
Cedar policies, approval, marketplace listing, Entra grants.

> **Preview.** AgentCore Gateway's Terraform coverage is recent (API-key secrets need AWS provider
> 6.66). Expect rough edges.

## What you edit

| File | What it is |
| --- | --- |
| `openapi.yaml` | Your API. Every operation with an `operationId` becomes one MCP tool (`api___<operationId>`). Set `servers[0].url` to your real base URL — the deploy refuses the `https://REPLACE-ME.example.com` placeholder. |
| `gateway.yaml` | How the gateway is described and how it authenticates to your API. Key names are a contract. |

`gateway.yaml` keys:

| Key | Meaning |
| --- | --- |
| `description` | Card + gateway description, max 100 chars. |
| `openapi_spec` | Spec path relative to the repo root (JSON or YAML). |
| `api_key.secret_arn` | **Required.** Full ARN (with its 6-character suffix) of a Secrets Manager secret named `agp-mcp/<mcp_name>/...` in the platform account and region. Any other ARN is refused at deploy. |
| `api_key.json_key` | JSON field inside the secret's value (default `api_key`). |
| `api_key.location` | `HEADER` or `QUERY_PARAMETER`. |
| `api_key.parameter_name` | Header or query parameter name (e.g. `x-api-key`). |
| `api_key.prefix` | Optional value prefix (e.g. `Bearer `). |
| `instructions` | Optional MCP server instructions for calling agents. |

## Quick start

```bash
# 1. Store your API key (platform account, same region as the gateway). The name MUST start
#    with agp-mcp/<mcp_name>/ — <mcp_name> is this MCP server's name in AGP.
aws secretsmanager create-secret --name agp-mcp/<mcp_name>/api-key \
  --secret-string '{"api_key":"<your key>"}'

# 2. Put the returned ARN in gateway.yaml (api_key.secret_arn), and your real URL in openapi.yaml.
# 3. Ship it.
git commit -am "Point the gateway at the claims API"
git push origin main
```

The key value never enters this repo or Terraform state: the gateway's credential provider uses
the secret as an **external** source and reads it at call time.

## What happens on push

```
push to main -> deploy.yml (GitHub OIDC) -> POST /builds/runtime {mcp_server_id, sha}
  -> CodeBuild applies agentcore_gateway at that commit:
       role agp-<name>-dev-gateway-role, API-key credential provider,
       gateway (MCP, Entra CUSTOM_JWT), target "api" (your OpenAPI spec)
  -> AGP records the gateway on the MCP server and discovers its tools
```

Governance (Cedar, approval, marketplace, grants) then works on it from AGP. Agents reach it with
the user's Entra identity — the gateway only accepts tokens for this MCP server's Entra app.

## Permissions the gateway needs

The module grants the gateway role the Cedar evaluation actions, AgentCore Identity's
`GetWorkloadAccessToken` + `GetResourceApiKey`, and `secretsmanager:GetSecretValue` on your secret.
That outbound action list is **verified in the E40 live test**. The secret read is scoped to
`agp-mcp/<mcp_name>/*` — that is why the name is fixed. If your secret uses a customer-managed
KMS key, add the key-policy statement in the module README
(`infrastructure/modules/agentcore_gateway/README.md`): `kms:Decrypt` for the gateway role only,
conditioned on `kms:ViaService` and `kms:EncryptionContext:SecretARN`. Never `kms:Decrypt` on `*`.

## Limits today

- Dev only: one gateway per MCP server; promotion is not available yet.
- Platform account only.
- One target (`api`), outbound auth = API key (default) or none (`auth: none` in `gateway.yaml`, for public APIs).
- Editing `openapi.yaml` and rebuilding does not refresh the MCP server's tool list in AGP (the
  gateway is unchanged, so AGP skips re-reading it). Use **Refresh tools** on the MCP server; that
  writes the record, so an approved card returns to DRAFT — re-approve it.

## Project structure

```
gateway.yaml                     # Gateway config. Deploy artifact.
openapi.yaml                     # Your API. Deploy artifact.
template.json                    # Catalog metadata (artifact_kind: mcp_server, preview: true)
.github/workflows/deploy.yml     # Trunk push -> OIDC -> POST /builds/runtime. No build legs.
```
