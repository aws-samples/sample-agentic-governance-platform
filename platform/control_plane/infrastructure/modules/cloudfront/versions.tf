terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
      # Mirrors the root's floor (see infrastructure/main.tf for why it is `>= 6.53, < 7.0`
      # rather than `~> 6.0`). No `required_version` here on purpose: this module declares no
      # version-gated language feature, and the root's floor already governs the whole tree.
      version               = ">= 6.53, < 7.0"
      configuration_aliases = [aws.us_east_1]
    }
  }
}
