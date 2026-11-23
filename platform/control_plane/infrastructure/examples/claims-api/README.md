# Claims API demo fixture

A small, real "Claims API" for the AgentCore Gateway template to front in the demo:
a REST API Gateway + one Python Lambda serving three operations from in-memory sample
data. Standalone Terraform root with its own (local) state — the platform root does not
reference it.

| operationId | Route | Notes |
|---|---|---|
| `getPolicy` | `GET /policies/{policyId}` | `POL-1001`, `POL-1002`, `POL-1003` |
| `openClaim` | `POST /claims` | body `{policyId, description, incidentDate?}`; returns a new `claimId`, status `received` |
| `getClaimStatus` | `GET /claims/{claimId}` | sample claim `CLM-2026-0001` |

Claims opened with `openClaim` live only in the warm Lambda container (demo data, no store).

## Auth

Every call needs header `x-api-key`. The key is checked twice: by a REQUEST authorizer
(the same Lambda, constant-time compare against the secret) and by API Gateway's usage plan. The
authorizer keeps every method off "no authorizer" audits; the Lambda permission is scoped
to this API's execution ARN.

The key is generated with `random_password` and stored in Secrets Manager as
`agp-mcp/<mcp_name>/api-key` = `{"api_key": "<value>"}` — the shape the gateway's `gateway.yaml` `api_key` block expects
(`json_key: api_key`, `parameter_name: x-api-key`). The key value is also in **this
fixture's Terraform state**; acceptable for a demo fixture, not for a real API.

## Deploy

Region and account come from your ambient AWS credentials. Deploy into the **same account
and region as the AGP platform**: the gateway reads the key secret from there (MCP servers
are built in the platform account only for now).

`mcp_name` (default `claims-api`) **must equal the MCP server name you use in Ops** when adding
the gateway repo: the gateway can read only secrets under `agp-mcp/<mcp_name>/`, and its deploy
refuses any other ARN. Different name? `terraform apply -var mcp_name=<your-mcp-name>`.

```bash
terraform init
terraform apply
terraform output invoke_url          # https://<id>.execute-api.<region>.amazonaws.com/v1
terraform output api_key_secret_arn
```

Smoke test:

```bash
KEY=$(aws secretsmanager get-secret-value --secret-id "$(terraform output -raw api_key_secret_arn)" \
  --query SecretString --output text | python3 -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')
curl -s -H "x-api-key: $KEY" "$(terraform output -raw invoke_url)/policies/POL-1001"
```

## Wire it into a gateway repo

In the repo created from the AgentCore Gateway template:

1. Copy `openapi.yaml` to the repo root and set `servers[0].url` to the `invoke_url`
   output. The gateway build refuses the `https://REPLACE-ME.example.com` placeholder.
2. Set `api_key.secret_arn` in `gateway.yaml` to the `api_key_secret_arn` output.
3. Commit and Build now.

## Teardown

`terraform destroy`. The secret is deleted without a recovery window.

## Tests

```bash
<main-checkout>/platform/control_plane/backend/venv/bin/python -m pytest test_handler.py -q
```
