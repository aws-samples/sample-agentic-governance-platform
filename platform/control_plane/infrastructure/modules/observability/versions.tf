terraform {
  # Pinned to the ROOT's floor (see infrastructure/main.tf for the full rationale) — this
  # module is only ever consumed by that root, so a single version story across the tree is
  # worth more than the lowest floor this file could technically get away with.
  required_version = ">= 1.15"

  required_providers {
    # This module has a REAL provider requirement of its own since E42:
    # `aws_xray_trace_segment_destination` and `aws_xray_indexing_rule` (the CloudWatch
    # Transaction Search resources in main.tf) were added in provider 6.46.0. The root's
    # `>= 6.53` floor already covers that; the constraint is restated here so the requirement
    # is visible to anyone reading or reusing this module on its own.
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.53, < 7.0"
    }
  }
}
