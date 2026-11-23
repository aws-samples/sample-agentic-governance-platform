terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # Floor 6.66, not the tree's `~> 6.0`: `api_key_secret_source = "EXTERNAL"` +
      # `api_key_secret_config` on the API-key credential provider ship in 6.66, and they are
      # what keeps the customer's API key out of Terraform state (the alternative, `api_key_wo`,
      # needs Terraform >= 1.11 and CodeBuild installs 1.5.7).
      version = ">= 6.66, < 7.0"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.9"
    }
  }
}

# A standalone ROOT module (the per-org `agp-gateway-infra` repo CodeBuild clones), like
# agentcore_harness — so the provider block lives here. deploy_role_arn set = assume it;
# empty = deploy-in-place with ambient CodeBuild credentials. E40 builds gateways in the
# platform account only (the backend's gateway clients are platform-account), so in practice
# this is the default tenant's platform-account deploy role.
provider "aws" {
  region = var.region

  dynamic "assume_role" {
    for_each = var.deploy_role_arn != "" ? [1] : []
    content {
      role_arn = var.deploy_role_arn
    }
  }
}
