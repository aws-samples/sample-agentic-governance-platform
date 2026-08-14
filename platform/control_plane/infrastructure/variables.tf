variable "aws_region" {
  description = "AWS region to deploy resources"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "app_name" {
  description = "Application name"
  type        = string
  default     = "control-plane"
}

variable "project_name" {
  description = "Project name"
  type        = string
  default     = "agp"
}

# NOTE: there is deliberately NO `github_org` / `create_github_oidc_provider`
# variable. This stack creates no GitHub artifacts at all — the OIDC provider and
# the shared ECR-push role are provisioned by the backend on the first GitHub
# connection, and the per-org roles are scoped to the orgs an operator actually
# connected (so no deploy-time org needs naming). See the GitHub block in main.tf.

# VPC Configuration (optional - use existing or create new)
variable "vpc_id" {
  description = "Existing VPC ID (leave empty to create new VPC)"
  type        = string
  default     = ""
}

variable "public_subnet_ids" {
  description = "Existing public subnet IDs (comma-separated)"
  type        = list(string)
  default     = []
}

variable "private_subnet_ids" {
  description = "Existing private subnet IDs (comma-separated)"
  type        = list(string)
  default     = []
}

variable "create_vpc" {
  description = "Whether to create a new VPC (false if using existing)"
  type        = bool
  default     = true
}

variable "vpc_cidr" {
  description = "CIDR block for VPC (only used if creating new VPC)"
  type        = string
  default     = "10.0.0.0/16"
}

variable "availability_zones" {
  description = "Availability zones for subnets"
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]
}

# Domain Configuration
variable "domain_name" {
  description = "Domain name for Control Plane"
  type        = string
  default     = "agp-platform.example.com"
}

variable "agp_api_url" {
  description = <<-EOT
    Public AGP API base URL INCLUDING the API Gateway stage + API prefix
    (e.g. https://<api-id>.execute-api.<region>.amazonaws.com/<env>/api/v1).
    Written as the AGP_API_URL GitHub Actions repo var on materialized agent
    repos; the scaffold build.yml `trigger` job POSTs to `$${AGP_API_URL}/builds/runtime`.
    Supplied as a root var (not module.api_gateway.api_endpoint) because api_gateway
    already depends on module.ecs — wiring the endpoint back would create a cycle.
    Empty ⇒ the repo var is omitted (the runtime-build trigger cannot reach the platform).
  EOT
  type        = string
  default     = ""
}

variable "hosted_zone_id" {
  description = "Route53 hosted zone ID (optional)"
  type        = string
  default     = ""
}

# ECS Configuration
variable "ecs_task_cpu" {
  description = "CPU units for ECS task (256, 512, 1024, 2048, 4096)"
  type        = number
  default     = 512
}

variable "ecs_task_memory" {
  description = "Memory for ECS task in MB"
  type        = number
  default     = 1024
}

variable "ecs_desired_count" {
  description = "Desired number of ECS tasks"
  type        = number
  default     = 2
}

variable "ecs_min_capacity" {
  description = "Minimum number of ECS tasks (auto-scaling)"
  type        = number
  default     = 1
}

variable "ecs_max_capacity" {
  description = "Maximum number of ECS tasks (auto-scaling)"
  type        = number
  default     = 4
}

variable "container_image" {
  description = "Docker container image URL (ECR)"
  type        = string
  default     = "" # Will be set to ECR URL after first build
}

# S3 Configuration
variable "project_archive_retention_days" {
  description = "Number of days to retain project archives in S3"
  type        = number
  default     = 7
}

# CodeBuild Configuration
variable "codebuild_compute_type" {
  description = "CodeBuild compute type for deployment builds"
  type        = string
  default     = "BUILD_GENERAL1_SMALL"
}

# NOTE: `codebuild_image` used to be declared here. The codebuild module
# hardcodes the AWS-managed `aws/codebuild/amazonlinux2-aarch64-standard` image;
# nothing consumed the override, and it implied a `:codebuild-latest` image push
# that the stack never needs.

# State Backend Configuration
variable "state_backend_bucket_name_prefix" {
  description = "Prefix for the Terraform state backend S3 bucket name (leave empty for auto-generated)"
  type        = string
  default     = ""
}

variable "state_backend_lock_table_name" {
  description = "Name for the Terraform state lock DynamoDB table (leave empty for auto-generated)"
  type        = string
  default     = ""
}

# Tags
variable "tags" {
  description = "Common tags for all resources"
  type        = map(string)
  default = {
    Project   = "agp"
    Component = "control-plane"
    ManagedBy = "terraform"
  }
}

variable "owner" {
  description = "Owner tag"
  type        = string
  default     = "platform-team"
}

variable "cost_center" {
  description = "Cost center tag"
  type        = string
  default     = "ai-platform"
}

# ============================================================================
# Prototype — Entra ID
# ============================================================================
# The confidential client secret generated for the platform's Entra "Backend
# Graph Client" app registration (E1, S1.3). Provided at apply time via
# `terraform.tfvars.local` (gitignored) or `-var`. Never commit the value.

variable "entra_backend_client_secret" {
  description = "Client secret VALUE for the Entra backend Graph client app reg. Generated once in the Entra portal; rotate every 24 months."
  type        = string
  sensitive   = true
}

variable "entra_tenant_id" {
  description = "AGP Entra tenant ID (a GUID). Used by the backend to validate inbound user JWTs and to acquire Microsoft Graph tokens via client credentials."
  type        = string
  default     = ""
}

variable "entra_backend_client_id" {
  description = "Application (client) ID of the platform's confidential 'Backend Graph Client' app registration in Entra. The companion to entra_backend_client_secret."
  type        = string
  default     = ""
}

variable "entra_audience" {
  description = "Audience claim the backend requires on inbound user JWTs. Matches the Application ID URI of the BACKEND app reg — the app that exposes the API scope, whose SP also carries the Platform.* role assignments that become the token's `roles` claim (the SPA app is only the OAuth client / `azp`). Must be byte-identical to the URI half of the SPA's VITE_ENTRA_SPA_SCOPE. Default 'api://agp' matches the dev tenant; override for other environments."
  type        = string
  default     = "api://agp"
}

variable "auth_provider" {
  description = "Inbound JWT validator the backend uses. Entra is the sole provider; drives AUTH_PROVIDER in the ECS task definition. Defaults to 'entra'."
  type        = string
  default     = "entra"

  validation {
    condition     = contains(["entra"], var.auth_provider)
    error_message = "auth_provider must be 'entra' — it is the only supported inbound JWT validator."
  }
}

variable "entra_spa_client_id" {
  description = "Application (client) ID (GUID) of the SPA app reg ('AGP — Frontend'). Accepted as an alternative `aud` value during inbound JWT validation, because Entra's v2.0 access tokens for the SPA's own exposed-API scope sometimes carry aud=<client-id-GUID> instead of the identifier URI. Required for the entra flow to admit MSAL-issued tokens."
  type        = string
  default     = ""
}

# ============================================================================
# Agent Registry (E4; Terraform-owned since E32)
# Wired into the ECS task definition so the backend can talk to AWS Agent Registry.
# TERRAFORM CREATES BOTH REGISTRIES — the agent registry (`agent_registry_name`) and the
# MCP-server registry (`mcp_registry_name`) — in the `agent-registry` namespace AWS split
# the Registry APIs into on 2026-08-06. See module "agent_registry" in main.tf.
#
# THERE IS NO `*_id` VARIABLE, AND THIS STACK NO LONGER HANDLES REGISTRY IDS AT ALL.
#
# The variables were removed in stages, for two different reasons. First they stopped being
# operator input: they had been the pre-E32 "paste the bootstrap output" inputs and then an
# "adoption override" for pointing this stack at a registry it did not create, and adoption was
# dropped as a feature. For a while the ids still flowed through the stack as locals fed by the
# creating module — and THAT is what made a from-zero deploy need `terraform apply` twice: AWS
# mints registryIds, no Terraform resource exists for the `agent-registry` namespace, and a
# `local-exec` provisioner cannot return a value, so the id had to be read back from a capture
# file during the PLAN walk, before the provisioner that writes it had run.
#
# The ids are now nobody's input and nobody's output here. The NAME is the identifier the whole
# stack is keyed on: it is static, known at plan time, and the backend resolves NAME -> id itself
# at first use (`backend/src/core/registry_resolver.py`). The `*_name` / `*_region` variables
# below define what gets created AND what the backend looks for, so they cannot disagree.
# ============================================================================

variable "agent_registry_name" {
  description = "AWS Agent Registry name Terraform creates for agent records. Must match the backend's AGENT_REGISTRY_NAME default (core/config.py)."
  type        = string
  default     = "agp-agents"
}

# NOTE: `default_tenant_group_id` used to be declared here. The E25 bootstrap
# seed it fed (`backend/scripts/seed_default_tenant.py`) is no longer run by
# Terraform, so no module consumed the variable. Pass the group id to the
# script's `--group-id` flag instead (see modules/default_tenant/main.tf).

variable "agent_registry_region" {
  description = "Region Terraform creates the agent registry in (and the region the backend calls it in). Also the region used for the MCP-server registry."
  type        = string
  default     = "us-east-1"
}

variable "mcp_registry_name" {
  description = "AWS Agent Registry name Terraform creates for MCP-server records. Must match the backend's MCP_REGISTRY_NAME default (core/config.py)."
  type        = string
  default     = "agp-mcp-servers"
}

variable "mcp_registry_region" {
  description = "Region the backend calls the MCP-server registry in. Creation uses agent_registry_region (both registries are created in one region), so override both together if you move them."
  type        = string
  default     = "us-east-1"
}

# ============================================================================
# Langfuse Observability (E26)
# ============================================================================
# Langfuse ships as a base module (see module "langfuse" in main.tf). The seed
# admin login is operator-supplied at apply time — NEVER commit a value. Set
# these in a gitignored tfvars / -var. The password feeds the headless
# LANGFUSE_INIT_USER_PASSWORD + the Lambda@Edge auto-login the provisioner uses.

variable "langfuse_admin_email" {
  description = "Email for the seed Langfuse admin user (LANGFUSE_INIT_USER_EMAIL). Operator-supplied at apply time; do not commit a value."
  type        = string
  default     = ""
}

variable "langfuse_admin_password" {
  description = "Password for the seed Langfuse admin user (LANGFUSE_INIT_USER_PASSWORD). Must contain letters, numbers, and at least one special character. Operator-supplied at apply time; NEVER commit a value."
  type        = string
  sensitive   = true
  default     = ""
}
