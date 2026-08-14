# ============================================================================
# VPC Outputs
# ============================================================================

output "vpc_id" {
  description = "VPC ID"
  value       = local.vpc_id
}

output "public_subnet_ids" {
  description = "Public subnet IDs"
  value       = local.public_subnet_ids
}

output "private_subnet_ids" {
  description = "Private subnet IDs"
  value       = local.private_subnet_ids
}

# ============================================================================
# DynamoDB Outputs
# ============================================================================

output "application_catalog_table_name" {
  description = "Application Catalog DynamoDB table name"
  value       = module.dynamodb.application_catalog_table_name
}

output "deployment_metadata_table_name" {
  description = "Deployment Metadata DynamoDB table name"
  value       = module.dynamodb.deployment_metadata_table_name
}

# ============================================================================
# S3 Outputs
# ============================================================================

output "project_archives_bucket_name" {
  description = "Project archives S3 bucket name"
  value       = module.s3.project_archives_bucket_name
}

output "frontend_bucket_name" {
  description = "Frontend S3 bucket name"
  value       = module.s3.frontend_bucket_name
}

# ============================================================================
# ECR Outputs
# ============================================================================

output "ecr_repository_url" {
  description = "ECR repository URL"
  value       = module.ecr.repository_url
}

# ============================================================================
# ECS Outputs
# ============================================================================

output "ecs_cluster_name" {
  description = "ECS cluster name"
  value       = module.ecs.cluster_name
}

output "ecs_service_name" {
  description = "ECS service name"
  value       = module.ecs.service_name
}

output "alb_dns_name" {
  description = "Application Load Balancer DNS name"
  value       = module.ecs.alb_dns_name
}

# ============================================================================
# API Gateway Outputs
# ============================================================================

output "api_endpoint" {
  description = "API Gateway endpoint URL"
  value       = module.api_gateway.api_endpoint
}

output "api_custom_domain_url" {
  description = "API Gateway custom domain URL (if configured)"
  value       = module.api_gateway.custom_domain_url
}

# ============================================================================
# CloudFront Outputs
# ============================================================================

output "cloudfront_distribution_id" {
  description = "CloudFront distribution ID"
  value       = module.cloudfront.distribution_id
}

output "frontend_url" {
  description = "Frontend URL"
  value       = module.cloudfront.frontend_url
}

# ============================================================================
# Observability Outputs
# ============================================================================

output "dashboard_name" {
  description = "CloudWatch dashboard name"
  value       = module.observability.dashboard_name
}

# ============================================================================
# CodeBuild Outputs
# ============================================================================

output "codebuild_project_name" {
  description = "CodeBuild project name"
  value       = module.codebuild.project_name
}

output "codebuild_project_arn" {
  description = "CodeBuild project ARN"
  value       = module.codebuild.project_arn
}

output "codebuild_role_arn" {
  description = "CodeBuild IAM role ARN"
  value       = module.codebuild.role_arn
}

# E36/T11 — surfaced for the SAME reason `codebuild_role_arn` is: a tenant account's
# hand-built `agp-deployment-*` role must name both platform principals in its trust
# policy, and docs/tenant-account-onboarding.md tells its owner to READ each ARN from
# these outputs rather than assemble it (an assembled ARN is a silent teardown failure,
# reported as `assume_role_failed:`). Provisioning uses the CodeBuild role, teardown uses
# this one (services/tenant_credentials.py).
output "backend_task_role_arn" {
  description = "Backend ECS task role ARN — the principal the backend assumes a tenant deploy-role FROM. Second trust principal for every agp-deployment-* role."
  value       = module.ecs.task_role_arn
}

# ============================================================================
# Agent Registry Outputs (AWS Agent Registry, `agent-registry` namespace)
# ============================================================================
# Terraform CREATES both registries (see module "agent_registry" in main.tf) but no longer
# PUBLISHES their ids, so the NAMES are what this stack emits. The ids used to be outputs
# (`agent_registry_id` / `mcp_registry_id`), populated from a capture file the module read during
# the PLAN walk — which is exactly what forced a from-zero deploy to `terraform apply` twice, and
# why they are gone. The backend resolves name -> id itself at first use; `terraform apply` is now
# single-pass. To see the ids, ask AWS rather than Terraform:
#
#   aws agent-registry-control list-registries \
#     --query 'registries[].{name:name,id:registryId,status:status}' --output table

output "agent_registry_name" {
  description = "Shared AWS Agent Registry name the backend uses (var.agent_registry_name). This is the identifier the whole stack is keyed on: the backend resolves it to a registryId at runtime, so it is the value that must match the backend's AGENT_REGISTRY_NAME."
  value       = var.agent_registry_name
}

output "mcp_registry_name" {
  description = "AWS Agent Registry name for MCP-server records (var.mcp_registry_name). Must match the backend's MCP_REGISTRY_NAME; the backend resolves it to a registryId at runtime."
  value       = var.mcp_registry_name
}

# ============================================================================
# Agent ECR Outputs (E21 Runtime Provisioning)
# ============================================================================

output "agent_ecr_repository_url" {
  description = "Shared agent-image ECR repository URL."
  value       = module.agent_ecr.repository_url
}

output "agent_ecr_push_role_arn" {
  description = "GitHub Actions OIDC push-role ARN for the shared agent ECR. The role itself is platform-provisioned on the first GitHub connection (Terraform ships no GitHub artifacts), so this is the deterministic ARN it will have — documented for the seed_default_tenant.py invocation in modules/default_tenant/main.tf, which only needs the string."
  value       = local.agent_ecr_push_role_arn
}

# ============================================================================
# Default-tenant bootstrap (E25/T7)
# ============================================================================
# Surfaced so the documented post-apply `seed_default_tenant.py` invocation in
# modules/default_tenant/main.tf works verbatim.

output "default_tenant_deploy_role_arn" {
  description = "Deploy-role ARN CodeBuild assumes for the default tenant (agp-deployment-<prefix>-default)."
  value       = module.default_tenant.deploy_role_arn
}

# ============================================================================
# State Backend Outputs
# ============================================================================

output "state_backend_bucket_name" {
  description = "Terraform state backend S3 bucket name"
  value       = module.state_backend.bucket_name
}

output "state_backend_bucket_arn" {
  description = "Terraform state backend S3 bucket ARN"
  value       = module.state_backend.bucket_arn
}

output "state_backend_lock_table_name" {
  description = "Terraform state lock DynamoDB table name"
  value       = module.state_backend.lock_table_name
}

output "state_backend_lock_table_arn" {
  description = "Terraform state lock DynamoDB table ARN"
  value       = module.state_backend.lock_table_arn
}

# ============================================================================
# Prototype — Secrets Manager
# ============================================================================

output "entra_backend_client_secret_arn" {
  description = "ARN of the Entra backend Graph client secret in AWS Secrets Manager. Backend reads this at runtime via boto3."
  value       = module.secrets_manager.graph_client_secret_arn
}

output "entra_backend_client_secret_name" {
  description = "Full secret name (e.g. agp-dev/graph-client-secret)."
  value       = module.secrets_manager.graph_client_secret_name
}

# ============================================================================
# Langfuse Observability (E26)
# ============================================================================

output "langfuse_host" {
  description = "Langfuse public HTTPS endpoint (CloudFront). Fed to the backend ECS task as LANGFUSE_HOST."
  value       = module.langfuse.langfuse_host
}

output "langfuse_secret_name" {
  description = "Secrets Manager secret name holding the Langfuse seed-org/admin credentials + seed project keys. Fed to the backend as LANGFUSE_ADMIN_SECRET_NAME."
  value       = module.langfuse.langfuse_secret_name
}

# ============================================================================
# Summary Output
# ============================================================================

output "deployment_summary" {
  description = "Control Plane deployment summary"
  value = {
    environment          = var.environment
    region               = var.aws_region
    frontend_url         = module.cloudfront.frontend_url
    api_endpoint         = module.api_gateway.api_endpoint
    ecr_repository       = module.ecr.repository_url
    codebuild_project    = module.codebuild.project_name
    state_backend_bucket = module.state_backend.bucket_name
  }
}
