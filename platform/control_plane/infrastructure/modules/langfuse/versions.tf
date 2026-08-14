terraform {
  # Matches the root's floor (see infrastructure/main.tf) — this module is only consumed by
  # that root, so there is no scenario in which it is initialised by an older CLI.
  required_version = ">= 1.15"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # Mirrors the root's floor. Nothing in THIS module needs 6.53 (the requirement comes from
      # modules/agentcore_runtime), but Terraform intersects every declared constraint, so
      # leaving `~> 6.0` here would advertise a compatibility this tree does not test.
      version = ">= 6.53, < 7.0"
      # Lambda@Edge functions MUST live in us-east-1 regardless of where the
      # rest of the stack is deployed, so the caller has to pass an explicitly
      # us-east-1 provider. Everything else uses the default (regional) one.
      configuration_aliases = [aws.us_east_1]
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.4"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0"
    }
    time = {
      source  = "hashicorp/time"
      version = ">= 0.9"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }
}
