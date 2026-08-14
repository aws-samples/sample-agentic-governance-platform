variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
}

variable "environment" {
  description = "Environment name"
  type        = string
}

variable "compute_type" {
  description = "CodeBuild compute type (BUILD_GENERAL1_SMALL, BUILD_GENERAL1_MEDIUM, BUILD_GENERAL1_LARGE)"
  type        = string
  default     = "BUILD_GENERAL1_SMALL"
}

# NOTE: there is deliberately no `image` variable. The CodeBuild environment
# image is hardcoded to the managed `aws/codebuild/amazonlinux2-aarch64-standard`
# image in main.tf — the stack does not build or push a custom CodeBuild image,
# so a variable here only implied a phantom `:codebuild-latest` push prerequisite.

variable "project_archives_bucket_arn" {
  description = "S3 bucket ARN for project archives (read access)"
  type        = string
}

variable "state_backend_bucket_arn" {
  description = "S3 bucket ARN for Terraform state backend (read/write)"
  type        = string
}

variable "deployment_metadata_table_arn" {
  description = "DynamoDB table ARN for deployment metadata"
  type        = string
}

variable "lock_table_arn" {
  description = "DynamoDB lock table ARN for Terraform state locking"
  type        = string
}

# --- Epic 21: agentcore_runtime branch statics -----------------------------

variable "aws_region" {
  description = "Deploy region. Set as AWS_TARGET_REGION for the EventBridge-triggered runtime build (StartBuild overrides still win for other paths)."
  type        = string
  default     = "us-east-1"
}

variable "state_bucket" {
  description = "Terraform state bucket name. Set as STATE_BUCKET for the runtime build backend-config."
  type        = string
  default     = ""
}

variable "aws_account_id" {
  description = "Account id. Composed into the runtime CONTAINER_IMAGE_URI in the agentcore_runtime branch."
  type        = string
  default     = ""
}

# There is deliberately NO `agent_registry_id` variable. The buildspec's runtime branch does
# read and write the agent envelope through `get-registry-record` / `update-registry-record`,
# which need an ID — but Terraform cannot know one at plan time (AWS mints registry ids, there
# is no Terraform resource for the `agent-registry` namespace, and the script-backed module's id
# was only readable from a capture file during the PLAN walk, which is what forced two applies
# from zero). The id therefore arrives as a per-build `environmentVariablesOverride` from
# `RuntimeBuildService.start_runtime_build` — the project's only trigger — which has already
# resolved it from the registry NAME. See the `AGENT_REGISTRY_ID` block in main.tf.

variable "entra_tenant_id" {
  description = "Entra tenant id (GUID). Written into runtime tfvars as tenant_id for the JWT authorizer discovery URL."
  type        = string
  default     = ""
}

variable "projects_table_name" {
  description = "Projects DynamoDB table name. The runtime branch writes cicd_status to the repo row (repo-row lookup wired in T6)."
  type        = string
  default     = ""
}

variable "projects_table_arn" {
  description = "Projects DynamoDB table ARN. Grants the runtime branch Query (agent_id-index) + UpdateItem on the projects table for the cicd_status write-back."
  type        = string
  default     = ""
}

variable "langfuse_host" {
  description = "Langfuse public HTTPS endpoint (CloudFront) — set as LANGFUSE_HOST for the agentcore_runtime build branch, which passes it (plus the per-agent secret NAME from the envelope) as runtime tfvars so every provisioned agent is observable. NON-SECRET (a URL). Empty until the langfuse module is applied ⇒ observability disabled."
  type        = string
  default     = ""
}


variable "tags" {
  description = "Common tags"
  type        = map(string)
  default     = {}
}
