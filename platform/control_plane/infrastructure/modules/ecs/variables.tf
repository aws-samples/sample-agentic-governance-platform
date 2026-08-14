variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
}

variable "cors_origins" {
  description = "List of allowed CORS origins"
  type        = list(string)
  default     = ["http://localhost:5173", "http://localhost:5174", "http://localhost:3000"]
}

variable "environment" {
  description = "Environment name"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID"
  type        = string
}

variable "private_subnet_ids" {
  description = "List of private subnet IDs"
  type        = list(string)
}

variable "public_subnet_ids" {
  description = "List of public subnet IDs"
  type        = list(string)
}

variable "task_cpu" {
  description = "CPU units for ECS task"
  type        = number
  default     = 512
}

variable "task_memory" {
  description = "Memory for ECS task in MB"
  type        = number
  default     = 1024
}

variable "desired_count" {
  description = "Desired number of ECS tasks"
  type        = number
  default     = 2
}

variable "min_capacity" {
  description = "Minimum number of ECS tasks"
  type        = number
  default     = 1
}

variable "max_capacity" {
  description = "Maximum number of ECS tasks"
  type        = number
  default     = 4
}

variable "container_image" {
  description = "Docker container image URL"
  type        = string
}

variable "application_catalog_table_name" {
  description = "Application Catalog DynamoDB table name"
  type        = string
}

variable "application_catalog_table_arn" {
  description = "Application Catalog DynamoDB table ARN"
  type        = string
}

variable "deployment_metadata_table_name" {
  description = "Deployment Metadata DynamoDB table name"
  type        = string
}

variable "deployment_metadata_table_arn" {
  description = "Deployment Metadata DynamoDB table ARN"
  type        = string
}

variable "project_archives_bucket_name" {
  description = "Project archives S3 bucket name"
  type        = string
}

variable "project_archives_bucket_arn" {
  description = "Project archives S3 bucket ARN"
  type        = string
}

variable "project_ecr_repository" {
  type        = string
  default     = ""
  description = "Shared agent-image ECR repo URI — stamped onto materialized repos as ECR_REPOSITORY."
}

variable "project_ecr_push_role_arn" {
  type        = string
  default     = ""
  description = "Shared GitHub-OIDC push-role ARN — stamped onto materialized repos as AWS_ECR_PUSH_ROLE_ARN when their connection has no per-org role. A DETERMINISTIC string from the root: the role is created by the backend on the first GitHub connection, not by Terraform (its trust names the OIDC provider, which IAM validates at role-create time)."
}

variable "frontend_bucket_name" {
  description = "Frontend S3 bucket name"
  type        = string
}

variable "frontend_bucket_arn" {
  description = "Frontend S3 bucket ARN"
  type        = string
}

variable "tags" {
  description = "Common tags"
  type        = map(string)
  default     = {}
}

variable "app_factory_table_name" {
  description = "Name of the App Factory DynamoDB table"
  type        = string
}

variable "app_factory_table_arn" {
  description = "ARN of the App Factory DynamoDB table"
  type        = string
}

variable "guardrails_table_name" {
  description = "Name of the guardrails DynamoDB table"
  type        = string
}

variable "guardrails_table_arn" {
  description = "ARN of the guardrails DynamoDB table"
  type        = string
}

variable "marketplace_table_name" {
  description = "Marketplace DynamoDB table name (E9); empty ⇒ backend uses in-memory fallback"
  type        = string
  default     = ""
}

variable "marketplace_table_arn" {
  description = "Marketplace DynamoDB table ARN (E9)"
  type        = string
  default     = ""
}

variable "connections_table_name" {
  description = "Connections DynamoDB table name (E19); empty ⇒ backend uses in-memory fallback"
  type        = string
  default     = ""
}

variable "connections_table_arn" {
  description = "Connections DynamoDB table ARN (E19)"
  type        = string
  default     = ""
}

variable "projects_table_name" {
  description = "Projects DynamoDB table name (E20); empty ⇒ backend uses in-memory fallback"
  type        = string
  default     = ""
}

variable "projects_table_arn" {
  description = "Projects DynamoDB table ARN (E20)"
  type        = string
  default     = ""
}

variable "codebuild_project_name" {
  description = "CodeBuild project the build-trigger endpoint calls StartBuild on (E22/T6)"
  type        = string
  default     = ""
}

variable "agp_api_url" {
  description = "Public AGP API base URL (incl. stage+prefix) written as the AGP_API_URL repo var on materialized agent repos; the scaffold trigger job POSTs to $${AGP_API_URL}/builds/runtime (E22 bugfix). Empty ⇒ omitted."
  type        = string
  default     = ""
}

variable "github_oidc_provider_arn" {
  description = "ARN of the token.actions.githubusercontent.com OIDC provider; Federated principal in each per-org ECR-push role trust policy (E22 multi-org). A DETERMINISTIC string from the root — the provider is created by the backend on the first GitHub connection, not by Terraform. Empty ⇒ the backend derives it from the STS account id."
  type        = string
  default     = ""
}

variable "agent_images_ecr_arn" {
  description = "ARN of the shared agent-images ECR repo the per-org ECR-push roles may push to (E22 multi-org). REQUIRED — the shared repo is where every materialized agent's image is pushed, and the root always passes module.agent_ecr.repository_arn."
  type        = string
  # NO `default = ""` — see codebuild_project_arn above for why the default was actively
  # harmful here (it justified a plan-time `count` guard on an apply-time value).
}

variable "tenants_table_name" {
  description = "Tenants DynamoDB table name (E24); empty ⇒ backend uses in-memory fallback"
  type        = string
  default     = ""
}

variable "tenants_table_arn" {
  description = "Tenants DynamoDB table ARN (E24)"
  type        = string
  default     = ""
}

variable "codebuild_project_arn" {
  description = "CodeBuild project ARN the ECS task role is granted codebuild:StartBuild on (E22/T6). REQUIRED — CodeBuild is not optional in this platform (it runs the runtime Terraform when an agent is pushed), and the root always passes module.codebuild.project_arn."
  type        = string
  # NO `default = ""`, deliberately. The default used to make this look optional, which is
  # why a `count = var.codebuild_project_arn != "" ? 1 : 0` guard was added to the policy
  # that consumes it — and that guard aborted the first apply ever run against an empty
  # state, because the value is a sibling module's attribute and so is unknown at plan time.
  # Requiring it keeps the module's contract honest: there is no empty case to handle.
}

variable "runtime_module_bucket" {
  description = "S3 bucket holding the zipped agentcore_runtime Terraform module the rollout pushes as agp-runtime-infra (E22 bugfix-A)"
  type        = string
  default     = ""
}

variable "runtime_module_key" {
  description = "S3 key of the zipped agentcore_runtime module (E22 bugfix-A)"
  type        = string
  default     = ""
}


# ============================================================================
# Prototype — Entra ID
# Wired into the FastAPI container as env vars so the backend can validate
# inbound JWTs (tenant_id) and authenticate to Microsoft Graph
# (backend_client_id + secret fetched from Secrets Manager at runtime).
# See: docs/entra-setup.md
# ============================================================================

variable "entra_tenant_id" {
  description = "AGP Entra tenant ID. Used by the backend to validate inbound user JWTs and to acquire Graph tokens via client credentials."
  type        = string
  default     = ""
}

variable "entra_backend_client_id" {
  description = "Application (client) ID of the platform's confidential 'Backend Graph Client' app registration in Entra."
  type        = string
  default     = ""
}

variable "auth_provider" {
  description = "Inbound JWT validator. Entra is the sole provider. Injected as AUTH_PROVIDER in the task definition; the backend reads it at startup to pick the validator."
  type        = string
  default     = "entra"
}

variable "entra_spa_client_id" {
  description = "SPA app reg client ID (GUID), injected as ENTRA_SPA_CLIENT_ID. Accepted as an alternative `aud` during inbound JWT validation. Required for the entra flow to admit MSAL-issued access tokens."
  type        = string
  default     = ""
}

variable "entra_backend_client_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the backend Graph client secret. Referenced from the task definition's `secrets` block to inject the value into the container as ENTRA_BACKEND_CLIENT_SECRET at task start; the ECS execution role grants secretsmanager:GetSecretValue on this ARN so the agent can resolve it."
  type        = string
  default     = ""
}

variable "entra_audience" {
  description = "Audience claim the backend requires on inbound user JWTs. Matches the Application ID URI of the BACKEND app reg — the app that exposes the API scope (the SPA app is only the OAuth client / `azp`). Must be byte-identical to the URI half of the SPA's VITE_ENTRA_SPA_SCOPE. Default 'api://agp' matches the dev tenant; override per environment if needed."
  type        = string
  default     = "api://agp"
}

# ============================================================================
# Agent Registry (E4; created by Terraform since E32)
# Injected into the FastAPI container so the backend can construct the
# AgentRegistryService / McpServerRegistryService against AWS Agent Registry
# (`agent-registry` namespace).
#
# NAMES, NOT IDS — and there are deliberately no `*_registry_id` variables here any more.
# AWS mints the registryId and `RegistryIdentifier` never accepts a name, so the root stack
# could only obtain an id from a capture file its script-backed `agent_registry` module read
# during the PLAN walk (a `local-exec` provisioner cannot return a value) — i.e. before the
# provisioner that writes it had run. That is what made a from-zero deploy need `terraform
# apply` TWICE: apply #1 rendered an EMPTY id into the task definition, apply #2 replaced it.
# The names are static tfvars known at plan time, and the backend resolves NAME -> id on first
# use and memoises it (backend/src/core/registry_resolver.py), so a single apply now produces
# a fully wired container. Do not re-add the id variables — that puts the capture file, the
# plan-time read and the second apply back.
# ============================================================================

variable "agent_registry_name" {
  description = "AWS Agent Registry name for agent records. Passed to the container as AGENT_REGISTRY_NAME; the backend resolves it to a registryId at first use, so this value (not an id) is what must match the registry Terraform creates."
  type        = string
  default     = "agp-agents"
}

variable "agent_registry_region" {
  description = "Region hosting the agent registry (Preview: not eu-central-1)."
  type        = string
  default     = "us-east-1"
}

variable "mcp_registry_name" {
  description = "AWS Agent Registry name for MCP-server records. Passed to the container as MCP_REGISTRY_NAME; resolved to a registryId by the backend at first use."
  type        = string
  default     = "agp-mcp-servers"
}

variable "mcp_registry_region" {
  description = "Region hosting the MCP registry (Preview: not eu-central-1)."
  type        = string
  default     = "us-east-1"
}

# === Langfuse Observability (E26) ===
# Base Langfuse module outputs, injected as plaintext backend env vars. The
# actual per-agent SECRET reads happen in later tasks; here the backend only
# learns the host + the admin/seed secret name.

variable "langfuse_host" {
  description = "Langfuse public HTTPS endpoint (CloudFront). Set as LANGFUSE_HOST on the backend task. Empty until the langfuse module is applied."
  type        = string
  default     = ""
}

variable "langfuse_admin_secret_name" {
  description = "Secrets Manager secret name holding the Langfuse seed-org/admin credentials. Set as LANGFUSE_ADMIN_SECRET_NAME on the backend task."
  type        = string
  default     = ""
}
