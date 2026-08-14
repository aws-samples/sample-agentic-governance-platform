terraform {
  # DELIBERATELY NOT RAISED TO THE ROOT'S `>= 1.15`. Unlike every sibling, this module is a
  # standalone ROOT module that CodeBuild applies (see the module README), and
  # modules/codebuild/buildspec.yml's install phase installs **Terraform 1.5.7**. A 1.15 floor
  # here would fail `terraform init` inside every runtime build — the deploy path, not a local
  # convenience. Raise this only together with the buildspec's pinned Terraform version.
  required_version = ">= 1.0"

  required_providers {
    # >= 6.53 IS REQUIRED BY THIS MODULE SPECIFICALLY: `lifecycle_configuration.max_lifetime`
    # (agentcore runtime) only plans consistently from 6.53 onward, and `~> 6.0` admitted 6.0.x.
    # Because `.terraform.lock.hcl` is gitignored, a fresh clone / fresh CodeBuild container
    # resolves the constraint from scratch, so the constraint is the only thing standing between
    # a build and an old provider. `< 7.0` preserves the major-version pin `~> 6.0` gave.
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.53, < 7.0"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.9"
    }
  }
}

# Runtime ships as a standalone ROOT module (agp-runtime-infra) that CodeBuild
# clones, so an explicit provider block is correct here. When deploy_role_arn is
# set, the provider assumes that cross-account role and runtime resources land in
# the tenant account; empty = deploy-in-place with ambient CodeBuild creds.
provider "aws" {
  region = var.aws_region

  dynamic "assume_role" {
    for_each = var.deploy_role_arn != "" ? [1] : []
    content {
      role_arn = var.deploy_role_arn
    }
  }
}
