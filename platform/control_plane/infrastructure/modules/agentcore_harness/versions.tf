terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # Same `~> 6.0` family pin as the rest of the tree. The FLOOR that matters
      # for this module is higher than the runtime module's: `aws_bedrockagentcore_harness`
      # ships in provider >= 6.32 (design §3). The pin cannot express that without
      # diverging from the tree-wide constraint, so the floor is documented in the
      # README's "Provider floor" section and enforced in practice by
      # `terraform init -upgrade` resolving the newest 6.x.
      version = "~> 6.0"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.9"
    }
  }
}

# The harness module ships as a standalone ROOT module (the per-org `agp-harness-infra`
# repo that CodeBuild clones), exactly like `agentcore_runtime` — so an explicit provider
# block belongs here rather than in a caller. When deploy_role_arn is set the provider
# assumes that cross-account role and the harness + its exec role land in the TENANT
# account; empty = deploy-in-place with ambient CodeBuild credentials.
provider "aws" {
  region = var.region

  dynamic "assume_role" {
    for_each = var.deploy_role_arn != "" ? [1] : []
    content {
      role_arn = var.deploy_role_arn
    }
  }
}
