# ============================================================================
# Shared ECR Repository for Materialized Agent Images
# ============================================================================
# One repo for ALL materialized agent images. The image TAG (<agent_id>-<sha>)
# carries agent identity — no per-agent repos (the backend creates nothing).

resource "aws_ecr_repository" "agent_images" {
  name                 = "${var.name_prefix}-agent-images"
  image_tag_mutability = "MUTABLE"

  # Materializing a single agent puts an image in here, and a non-empty repo blocks
  # deletion — without this, `terraform destroy` fails on any stack that ever ran a build.
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-agent-images"
  })
}

# Lifecycle policy to keep only last 50 images
resource "aws_ecr_lifecycle_policy" "agent_images" {
  repository = aws_ecr_repository.agent_images.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last 50 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 50
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

# ============================================================================
# Cross-account replication → central platform ECR (E25)
# ============================================================================
# Replicate agent images to the central platform ECR for audit aggregation.
# OPT-IN: only created when a DISTINCT destination is configured. ECR rejects a
# replication rule whose destination equals the source registry+region, so this
# stays off by default. In single-account both destination vars default to ""
# ⇒ count 0 ⇒ no rule (tenant ECR == central ECR, nothing to replicate). Set
# replication_destination_account_id (+ optional region) to enable cross-account.

locals {
  replication_dest_region = var.replication_destination_region != "" ? var.replication_destination_region : var.region
}

resource "aws_ecr_replication_configuration" "agent_images" {
  count = (var.replication_destination_account_id != "" && !(var.replication_destination_account_id == var.account_id && local.replication_dest_region == var.region)) ? 1 : 0

  replication_configuration {
    rule {
      destination {
        region      = local.replication_dest_region
        registry_id = var.replication_destination_account_id
      }
    }
  }
}

# ============================================================================
# Runtime exec-role pull — granted on the exec ROLE, not this repo (E25)
# ============================================================================
# The AgentCore runtime exec role pulls the shared image via its OWN identity
# policy — the agentcore_runtime module self-provisions that role in-account at
# deploy time and scopes its ecr:BatchGetImage/GetDownloadUrlForLayer/
# BatchCheckLayerAvailability grant to the repo ARN passed in ecr_repo_arn.
# Granting on the role's identity — not via a repo policy here — keeps this repo
# free of a principal reference that may not exist yet, so no greenfield apply race.
# A genuine CROSS-account tenant runtime would need a resource policy here; add it
# guarded (like the replication config) when that case arrives.

# ============================================================================
# NO GITHUB PUSH-ROLE HERE — it is a platform capability (see root main.tf)
# ============================================================================
# This module used to create `${var.name_prefix}-agent-ecr-push`, the shared
# GitHub-OIDC role that Actions workflows assume to push agent images. It could
# not stay: its trust policy names the account's GitHub OIDC provider as its
# `Federated` principal, and IAM validates that the principal EXISTS when the role
# is created — so on an account with no provider (i.e. one that has never
# connected GitHub) the role was un-creatable at apply time, no matter the
# resource ordering. Git-provider integrations are a platform capability, so both
# the provider and this role are now created idempotently by the backend on the
# FIRST GitHub connection (`ecr_push_role_service.ensure_shared_role`), under the
# SAME name so an already-deployed account's live role is adopted.
#
# What that leaves here is GitHub-free by construction: the repo, its lifecycle
# policy, and the opt-in cross-account replication. Do not reintroduce an IAM
# principal that references a Git provider in this module.
