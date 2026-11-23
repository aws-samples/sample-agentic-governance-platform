# ---------------------------------------------------------------------------
# Inputs for the platform-owned AgentCore GATEWAY module (E40/T4).
#
# Same shape as agentcore_harness: the MCP-specific knobs live in TWO developer-owned files
# (`gateway.yaml` + the OpenAPI spec it names) that arrive as FILE PATHS, and everything else
# is platform-owned, written as tfvars by the CodeBuild `agentcore_gateway` branch.
# ---------------------------------------------------------------------------

variable "mcp_server_id" {
  # Names no AWS resource. It is the first segment of the state key convention
  # (`agentcore-gateway/{mcp_server_id}/{stage}/terraform.tfstate`), echoed by the state_key
  # output so a deploy can be traced to its state object.
  description = "AGP MCP server record id. Names no AWS resource; first segment of the state key convention."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._-]*$", var.mcp_server_id))
    error_message = "mcp_server_id must be non-empty and contain only letters, digits, dots, hyphens or underscores (it becomes a path segment in the Terraform state key)."
  }
}

variable "mcp_name" {
  # The platform's MCP name rule for Ops-created servers. Hyphens, NO underscores — the
  # opposite of the harness: the AgentCore gateway name pattern is `([0-9a-zA-Z][-]?){1,48}`.
  # Max 31 keeps `agp-<name>-<stage>-gateway-role` under IAM's 64.
  description = "MCP server name — the STEM of the stage-scoped gateway/role/credential-provider names. Must match ^[a-z][a-z0-9-]{1,30}$."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}$", var.mcp_name))
    error_message = "mcp_name must match ^[a-z][a-z0-9-]{1,30}$ (lowercase letter first, then lowercase letters/digits/hyphens, max 31 chars)."
  }
}

variable "stage" {
  # REQUIRED, no default — same reason as the harness/runtime modules: a forgotten stage
  # would otherwise name another stage's account-global resources.
  description = "Deployment stage (e.g. dev). REQUIRED, no default — suffixes every account-global name."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9-]+$", var.stage))
    error_message = "stage must be non-empty and contain only lowercase letters, digits or hyphens (it is part of the gateway name, which allows no underscores)."
  }
}

variable "region" {
  description = "AWS region the gateway is created in. Also the provider's region (versions.tf)."
  type        = string
}

variable "gateway_config_path" {
  # Read with `yamldecode(file(...))`, so it must resolve on the APPLY HOST at plan time — the
  # buildspec clones the MCP repo at the pushed commit and passes the path to its gateway.yaml.
  # NOTE: the buildspec's stale-module drift guard greps for this block's opening line; do not
  # rename it.
  description = "Filesystem path to the MCP repo's gateway.yaml. Keys: description, openapi_spec, auth (none | api_key, absent = api_key), api_key{secret_arn,json_key,location,parameter_name,prefix} (read only for auth api_key), instructions."
  type        = string
}

variable "openapi_spec_path" {
  # Resolved by the buildspec from gateway.yaml's `openapi_spec` (relative to the repo root).
  # Passed separately rather than re-derived here so the module never guesses the clone root.
  description = "Filesystem path to the OpenAPI spec (JSON or YAML) the gateway target exposes as MCP tools. Inlined into the target."
  type        = string
}

# ---- Platform-owned ----

# Inbound Entra authorizer — the same inputs, same shapes, as the harness/runtime modules and
# as McpIdentityService builds for a gateway. The MCP's Entra app is minted at materialize
# time, BEFORE the first build, so the gateway is born with its real audience.
variable "tenant_id" {
  description = "Entra tenant id (GUID) for the OIDC discovery URL."
  type        = string
}

variable "entra_app_audience" {
  description = "Per-MCP Entra app identifier URI (api://agp-mcp-<id>) — an allowed aud."
  type        = string
}

variable "entra_app_id" {
  description = "Per-MCP Entra app client GUID — the OTHER allowed aud form Entra may mint."
  type        = string
  default     = ""
}

variable "deploy_role_arn" {
  description = "Cross-account deploy role the provider assumes; empty = deploy-in-place (single-account, the only E40 path)."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to the gateway, its role and its credential provider."
  type        = map(string)
  default     = {}
}
