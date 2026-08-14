variable "name_prefix" {
  description = "Prefix for resource names (matches the root name_prefix)."
  type        = string
}

variable "codebuild_role_arn" {
  description = "ARN of the platform CodeBuild role permitted to assume the deploy-role (cross-account provisioning seam)."
  type        = string
}

# E36/T11. REQUIRED, not defaulted-empty: an empty string in a Principal list is not a
# valid ARN and IAM rejects the whole trust policy at apply, which is a better failure
# than a role that silently cannot be assumed by the teardown. The root always has the
# value (`module.ecs.task_role_arn`) and there is no dependency cycle — modules/ecs
# consumes nothing from this module.
variable "backend_task_role_arn" {
  description = "ARN of the backend ECS task role permitted to assume the deploy-role (cross-account TEARDOWN seam, E36/T8 services/tenant_credentials.py)."
  type        = string
}

variable "tags" {
  description = "Common tags"
  type        = map(string)
  default     = {}
}
