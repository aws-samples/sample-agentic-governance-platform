terraform {
  # Pinned to the ROOT's floor (see infrastructure/main.tf for the full rationale) — this
  # module is only ever consumed by that root, so a single version story across the tree is
  # worth more than the lowest floor this file could technically get away with. Its
  # `terraform_data` (the built-in successor to `null_resource`, needing no third-party
  # provider) has been stable since 1.4; the reference to the root's now-deleted `removed`
  # blocks that used to justify `>= 1.7` here is gone with them (E32/T6).
  required_version = ">= 1.15"

  required_providers {
    # No AWS resource is declared here (see main.tf: there is no Terraform resource
    # for the `agent-registry` namespace). The constraint is still declared so this
    # module carries the same provider floor as every sibling module and so an AWS
    # resource can be added later without a version-constraint surprise.
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.53, < 7.0"
    }
  }
}
