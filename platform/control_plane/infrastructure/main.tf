terraform {
  # >= 1.15: `terraform_data` + `fileexists`-guarded preconditions in modules/agent_registry (E32).
  # AWS provider >= 6.53: modules/agentcore_runtime's lifecycle_configuration.max_lifetime
  # only plans consistently from that version onward.
  #
  # WHY A FLOOR AT ALL, given both constructs are older than 1.15. The previous floor (`>= 1.7`)
  # was inherited from the `removed` blocks this stack used to carry, which E32/T6 deleted — so
  # it no longer described anything real. A floor that names no requirement drifts into a floor
  # nobody dares raise. 1.15 is the version this tree is developed and validated against, and
  # `terraform validate` on an older CLI would accept configuration that has never been exercised
  # here. There is no reason to promise support for an untested CLI on a stack whose whole point
  # (this audit) is that a from-zero apply must be predictable.
  #
  # THE PROVIDER FLOOR IS THE LOAD-BEARING HALF, and it is a range, not `~> 6.0`. `~> 6.0` allows
  # 6.0.x, which cannot plan modules/agentcore_runtime — and because `.terraform.lock.hcl` is
  # GITIGNORED, a fresh clone resolves the constraint from scratch and an old provider mirror can
  # legitimately hand back 6.0.x. `>= 6.53` makes that a resolution error at `init` instead of an
  # inconsistent-plan error deep inside a runtime build. `< 7.0` keeps the major-version pin that
  # `~> 6.0` provided (a 7.x provider is a breaking change and must be adopted deliberately).
  # This floor is mirrored in EVERY modules/**/versions.tf — Terraform intersects all of them, so
  # a module left at `~> 6.0` would not lower the effective floor, but it would misreport what
  # that module needs to anyone reading or reusing it in isolation.
  required_version = ">= 1.15"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.53, < 7.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
    # E26: the langfuse base module uses random_bytes/random_password/random_string
    # (needs random >= 3.4 for random_bytes), null_resource, and time_sleep.
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
  }

  # Uncomment for remote state management
  # backend "s3" {
  #   bucket         = "agp-terraform-state"
  #   key            = "control-plane/terraform.tfstate"
  #   region         = "us-east-1"
  #   encrypt        = true
  #   dynamodb_table = "terraform-state-lock"
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = merge(var.tags, {
      Environment = var.environment
      Owner       = var.owner
      CostCenter  = var.cost_center
    })
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id_short = substr(data.aws_caller_identity.current.account_id, -6, 6)
  name_prefix      = "${var.project_name}-cp-${var.environment}-${local.account_id_short}"

  # Use existing VPC or create new
  use_existing_vpc = var.vpc_id != "" && length(var.public_subnet_ids) > 0 && length(var.private_subnet_ids) > 0

  vpc_id             = local.use_existing_vpc ? var.vpc_id : module.networking[0].vpc_id
  public_subnet_ids  = local.use_existing_vpc ? var.public_subnet_ids : module.networking[0].public_subnet_ids
  private_subnet_ids = local.use_existing_vpc ? var.private_subnet_ids : module.networking[0].private_subnet_ids
}

# ============================================================================
# Networking Module (Optional - only if creating new VPC)
# ============================================================================

module "networking" {
  source = "./modules/networking"
  count  = local.use_existing_vpc ? 0 : 1

  name_prefix        = local.name_prefix
  vpc_cidr           = var.vpc_cidr
  availability_zones = var.availability_zones
  environment        = var.environment

  tags = var.tags
}

# ============================================================================
# DynamoDB Tables Module
# ============================================================================

module "dynamodb" {
  source = "./modules/dynamodb"

  name_prefix = local.name_prefix
  environment = var.environment

  tags = var.tags
}

# ============================================================================
# S3 Buckets Module
# ============================================================================

module "s3" {
  source = "./modules/s3"

  name_prefix                    = local.name_prefix
  environment                    = var.environment
  project_archive_retention_days = var.project_archive_retention_days

  tags = var.tags
}

# ============================================================================
# ECR Repository Module
# ============================================================================

module "ecr" {
  source = "./modules/ecr"

  name_prefix = local.name_prefix
  environment = var.environment

  tags = var.tags
}

# ============================================================================
# ECS Cluster and Service Module
# ============================================================================

module "ecs" {
  source = "./modules/ecs"

  name_prefix        = local.name_prefix
  environment        = var.environment
  vpc_id             = local.vpc_id
  private_subnet_ids = local.private_subnet_ids
  public_subnet_ids  = local.public_subnet_ids

  # Task configuration
  task_cpu      = var.ecs_task_cpu
  task_memory   = var.ecs_task_memory
  desired_count = var.ecs_desired_count
  min_capacity  = var.ecs_min_capacity
  max_capacity  = var.ecs_max_capacity
  # Default is the bare ECR repo URL, which Docker resolves as :latest. That tag
  # does not exist on a fresh account, so the ECS service comes up at 0 running
  # tasks (the deployment circuit breaker rolls the deployment back) until the
  # first backend image push — `terraform apply` still SUCCEEDS. See README
  # §"Building and Deploying Backend Container". Left as the bare URL on purpose:
  # spelling `:latest` explicitly is identical to Docker but forces a new ECS task
  # definition revision on every already-deployed stack for zero behavior change.
  container_image = var.container_image != "" ? var.container_image : module.ecr.repository_url

  # DynamoDB tables
  application_catalog_table_name = module.dynamodb.application_catalog_table_name
  application_catalog_table_arn  = module.dynamodb.application_catalog_table_arn
  deployment_metadata_table_name = module.dynamodb.deployment_metadata_table_name
  deployment_metadata_table_arn  = module.dynamodb.deployment_metadata_table_arn

  # S3 buckets
  project_archives_bucket_name = module.s3.project_archives_bucket_name
  project_archives_bucket_arn  = module.s3.project_archives_bucket_arn
  frontend_bucket_name         = module.s3.frontend_bucket_name
  frontend_bucket_arn          = module.s3.frontend_bucket_arn

  # Shared agent-image ECR (E21) — stamped onto materialized repos
  project_ecr_repository = module.agent_ecr.repository_url
  # A DETERMINISTIC STRING, not a resource reference: the shared push role is
  # platform-provisioned on the first GitHub connection (it trusts the OIDC provider,
  # which IAM validates at role-create time, so it cannot be an apply-time object on a
  # provider-less account). A string has no dependency, so a fresh apply succeeds and the
  # env var is already correct by the time the object exists. See the GitHub block below.
  project_ecr_push_role_arn = local.agent_ecr_push_role_arn

  app_factory_table_name = module.dynamodb.app_factory_table_name
  app_factory_table_arn  = module.dynamodb.app_factory_table_arn
  guardrails_table_name  = module.dynamodb.guardrails_table_name
  guardrails_table_arn   = module.dynamodb.guardrails_table_arn
  marketplace_table_name = module.dynamodb.marketplace_table_name
  marketplace_table_arn  = module.dynamodb.marketplace_table_arn
  connections_table_name = module.dynamodb.connections_table_name
  connections_table_arn  = module.dynamodb.connections_table_arn
  projects_table_name    = module.dynamodb.projects_table_name
  projects_table_arn     = module.dynamodb.projects_table_arn
  tenants_table_name     = module.dynamodb.tenants_table_name
  tenants_table_arn      = module.dynamodb.tenants_table_arn
  codebuild_project_name = module.codebuild.project_name
  codebuild_project_arn  = module.codebuild.project_arn
  # E22 bugfix: AGP_API_URL repo var (incl. stage+prefix). Root var, not
  # module.api_gateway.api_endpoint — api_gateway already depends on module.ecs (cycle).
  agp_api_url = var.agp_api_url
  # E22 multi-org: per-org GitHub-OIDC ECR-push role provisioning (the backend creates a
  # role per connected org). ECR-push role name prefix is the resource name_prefix.
  # Also a DETERMINISTIC STRING (see the GitHub block below): the provider itself is
  # created by the backend on the first GitHub connection, so there is no resource here to
  # reference — and passing the ARN it WILL have needs no resource at all.
  github_oidc_provider_arn = local.github_oidc_provider_arn
  agent_images_ecr_arn     = module.agent_ecr.repository_arn
  # E22 bugfix-A: S3-staged agentcore_runtime module the rollout pushes as agp-runtime-infra.
  runtime_module_bucket = module.state_backend.bucket_name
  runtime_module_key    = aws_s3_object.runtime_module.key
  cors_origins          = concat(["http://localhost:5173", "http://localhost:5174", "http://localhost:3000"], ["https://${module.cloudfront.distribution_domain_name}"], var.domain_name != "" ? ["https://${var.domain_name}"] : [])

  # Prototype — Entra ID
  auth_provider                   = var.auth_provider
  entra_tenant_id                 = var.entra_tenant_id
  entra_spa_client_id             = var.entra_spa_client_id
  entra_backend_client_id         = var.entra_backend_client_id
  entra_backend_client_secret_arn = module.secrets_manager.graph_client_secret_arn
  entra_audience                  = var.entra_audience

  # Agent Registry (E4, created by Terraform since E32) — NAME + REGION ONLY. No id is passed:
  # the backend resolves name -> id itself at first use, which is what makes `terraform apply`
  # single-pass from zero (see the Agent Registry section below for the full reasoning).
  agent_registry_name   = var.agent_registry_name
  agent_registry_region = var.agent_registry_region

  # MCP Server Registry (E5) — same, keyed "mcp" in module.agent_registry.
  mcp_registry_name   = var.mcp_registry_name
  mcp_registry_region = var.mcp_registry_region

  # Langfuse Observability (E26) — host + admin-secret NAME as plaintext backend env.
  # module.langfuse depends only on networking + vars (not on module.ecs), so no cycle.
  langfuse_host              = module.langfuse.langfuse_host
  langfuse_admin_secret_name = module.langfuse.langfuse_secret_name

  tags = var.tags
}

# ============================================================================
# API Gateway Module
# ============================================================================

module "api_gateway" {
  source = "./modules/api_gateway"

  name_prefix        = local.name_prefix
  environment        = var.environment
  vpc_id             = local.vpc_id
  private_subnet_ids = local.private_subnet_ids
  public_subnet_ids  = local.public_subnet_ids

  # ECS service
  ecs_service_name      = module.ecs.service_name
  ecs_security_group_id = module.ecs.security_group_id
  ecs_target_group_arn  = module.ecs.target_group_arn
  ecs_listener_arn      = module.ecs.listener_arn

  # Domain
  domain_name    = var.domain_name
  hosted_zone_id = var.hosted_zone_id

  tags = var.tags
}

# ============================================================================
# CodeBuild Module (CI/CD Pipeline)
# ============================================================================

module "codebuild" {
  source = "./modules/codebuild"

  name_prefix = local.name_prefix
  environment = var.environment

  compute_type = var.codebuild_compute_type

  # S3 buckets
  project_archives_bucket_arn = module.s3.project_archives_bucket_arn
  state_backend_bucket_arn    = module.state_backend.bucket_arn

  # DynamoDB tables
  deployment_metadata_table_arn = module.dynamodb.deployment_metadata_table_arn
  lock_table_arn                = module.state_backend.lock_table_arn

  # Epic 21: agentcore_runtime branch statics (build triggered via backend StartBuild)
  aws_region     = var.aws_region
  state_bucket   = module.state_backend.bucket_name
  aws_account_id = data.aws_caller_identity.current.account_id
  # NO `agent_registry_id` HERE, and that is what removed the second apply. The buildspec's
  # registry calls DO need an id (`RegistryIdentifier` never accepts a name), but the id cannot
  # exist at plan time, so it now arrives as a per-build env OVERRIDE from the backend — the
  # build's only trigger (`RuntimeBuildService` → `StartBuild`; the EventBridge trigger went in
  # E22/T7). See the Agent Registry section below.
  entra_tenant_id     = var.entra_tenant_id
  projects_table_name = module.dynamodb.projects_table_name
  projects_table_arn  = module.dynamodb.projects_table_arn

  # E26/T10: Langfuse host → LANGFUSE_HOST env for the agentcore_runtime branch, which passes it
  # (plus each agent's key secret NAME from the envelope) as runtime tfvars so every provisioned
  # agent is observable with zero manual wiring. NON-SECRET (a URL); empty until langfuse applies.
  langfuse_host = module.langfuse.langfuse_host


  tags = var.tags
}

# ============================================================================
# Agent Registry Module (E32) — Terraform now OWNS registry creation
# ============================================================================
# Both registries the platform needs are created here, in the `agent-registry`
# namespace AWS split the Registry APIs into on 2026-08-06 (the old
# `bedrock-agentcore` namespace shuts down 2026-09-17 and is unreachable from
# accounts created after the split). They used to be provisioned out-of-band by
# two bootstrap scripts an operator ran by hand, pasting the ids into tfvars;
# a clean deploy from zero now creates them as part of `terraform apply`.
#
# The module is SCRIPT-BACKED because there is no Terraform resource for this
# namespace — the provider's `aws_bedrockagentcore_registry` targets the old one
# and ships deprecated, and no data source exists. See modules/agent_registry/main.tf
# for the full rationale and for the two rules that module must never break
# (no unguarded plan-time read of its capture file; no destroy-time provisioner).
#
# HISTORY / STATE NOTE: three `removed` blocks used to sit here — `module.agent_registry`
# (the deleted Preview-era module) and `module.agent_ecr`'s two `github_push` IAM
# resources — each with `lifecycle { destroy = false }` so Terraform would FORGET rather
# than destroy them. They were deleted in E32/T6 after inspecting `terraform.tfstate`
# (serial 817): none of those addresses remain in state, so all three were no-ops. That
# is verified for THIS state file only. Any OTHER state file that still holds
# `module.agent_registry` or `module.agent_ecr.aws_iam_role[_policy].github_push` needs
# those `removed` blocks restored (see git history) before its first apply, or Terraform
# will DESTROY a live registry and every record in it / a live GitHub-OIDC push role.
# Reusing the `agent_registry` module name below is safe for the same reason: no
# `module.agent_registry` address remains to collide with.

module "agent_registry" {
  source = "./modules/agent_registry"

  # Two instances, one per registry: the agent registry and the MCP-server registry are
  # separate registries that differ only by name (they hold different record types and are
  # granted separately). `for_each` keys them by role so the root can address them as
  # module.agent_registry["agents"] / ["mcp"].
  #
  # NAME IS THE ONLY INPUT because it is the only thing that differs — and the only thing
  # that reaches AWS. `ensure_registry.py` derives each registry's description from its name
  # and exposes no `--description` flag, so the two per-registry description strings this map
  # used to carry configured nothing while reading at the call site as though they did. They
  # were removed along with the module's dead `description` variable (see
  # modules/agent_registry/variables.tf).
  #
  # `read_region` is the region the BACKEND will query this registry from — for the mcp
  # instance that is `var.mcp_registry_region`, which is NOT the variable creation uses. The
  # module asserts the two match (`precondition` in its main.tf), so setting the two region
  # vars to different values now fails at PLAN time with both values named, instead of
  # applying cleanly and then failing every MCP registry call at runtime with a bare
  # "registry not found".
  for_each = {
    agents = { name = var.agent_registry_name, read_region = var.agent_registry_region }
    mcp    = { name = var.mcp_registry_name, read_region = var.mcp_registry_region }
  }

  name        = each.value.name
  region      = var.agent_registry_region
  read_region = each.value.read_region
}

# NO `local.agent_registry_id` / `local.mcp_registry_id` — AND `terraform apply` IS THEREFORE
# SINGLE-PASS FROM ZERO.
#
# Those two locals used to exist, carrying a careful "always a string, never null" contract, and
# they are what made a from-zero deploy need TWO applies. AWS mints the registryId, there is no
# Terraform resource for the `agent-registry` namespace, and `local-exec` cannot return a value —
# so the module had to write its id to a capture file and read it back with `fileexists`/`file`,
# which resolve during the PLAN walk, before the provisioner that writes it has run. Apply #1
# therefore planned `AGENT_REGISTRY_ID=""` into the ECS task definition and the CodeBuild env, and
# only apply #2 substituted the real ids.
#
# THE ID IS NO LONGER PASSED THROUGH TERRAFORM AT ALL. The backend resolves it from the registry
# NAME — a static tfvar, known at plan time — on first use, and memoises it
# (`backend/src/core/registry_resolver.py`). ECS receives only the NAMES. CodeBuild, whose
# buildspec genuinely needs an ID (`agent-registry-control get-registry-record` /
# `update-registry-record` take a `RegistryIdentifier`, never a name), receives it as a per-build
# env override from the backend — which is the build's only trigger, since the EventBridge
# trigger was deleted in E22/T7. So nothing downstream needs a value Terraform cannot know yet.
#
# This is the same call the root makes for the GitHub OIDC provider and the shared push role a few
# lines below: an object that cannot be a clean Terraform object is bootstrapped by the platform
# instead of being half-modelled here.
#
# DO NOT REINTRODUCE THESE LOCALS. Doing so requires the module to publish an id again, which
# requires the capture file, which brings back the plan-time read and the second apply.

# ============================================================================
# GitHub is a PLATFORM capability, not a deploy-time object
# ============================================================================
# THIS STACK SHIPS ZERO GITHUB ARTIFACTS — no OIDC provider, no push role, no
# data source that requires either to pre-exist. `terraform apply` therefore
# succeeds on an account that has never seen GitHub, and a customer who never
# connects GitHub never carries a GitHub dependency. A future GitLab (or any
# other Git provider) integration follows the same pattern: its identity objects
# are bootstrapped by ITS connection path, not by this stack.
#
# WHAT REPLACED WHAT. There used to be an `aws_iam_openid_connect_provider`
# behind a `create_github_oidc_provider` toggle (plus a data source for the
# other branch), and `modules/agent_ecr` created the shared GitHub-OIDC push
# role. The role is what forced the move: its trust policy names the provider as
# its `Federated` principal, and IAM VALIDATES that the principal exists when the
# role is created. On a provider-less account there is no ordering of Terraform
# resources that works — the provider must be an account fact before any role can
# trust it, and only the connection path knows when a customer wants that.
#
# The backend now owns both, idempotently, on the FIRST GitHub connection:
#   backend/src/services/github_oidc_provider_service.py  → the provider
#   backend/src/services/ecr_push_role_service.py         → ensure_shared_role()
#                                                            + the per-org roles
# The shared role keeps the name this module used, so an already-deployed
# account's live role is ADOPTED BY NAME rather than duplicated (two `removed`
# blocks used to sit below to make Terraform forget it instead of destroying it;
# they were deleted in E32/T6 — see the note below them), and the ECS task role
# carries the IAM grants for both (modules/ecs/main.tf, Sids
# ManageGithubOidcProvider / ManageEcrPushRoles).

# Both ARNs the backend needs are handed to it as DETERMINISTIC STRINGS built from
# the caller identity + the names the backend itself uses. A string needs no
# resource to exist, which is precisely the point: a fresh apply has nothing to
# create or read, and the objects become real on the first GitHub connection.
locals {
  # `oidc-provider/<host>` — OIDC-provider ARNs have no random component.
  # Mirrors github_oidc_provider_service.github_oidc_provider_arn().
  github_oidc_provider_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
  # The shared platform-default push role. Mirrors
  # EcrPushRoleService.shared_role_name() — `<name_prefix>-agent-ecr-push`, the
  # same name modules/agent_ecr used, which is what makes the adoption work.
  agent_ecr_push_role_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.name_prefix}-agent-ecr-push"
}

# The shared push role WAS created by module.agent_ecr and IS in older state files, where
# deleting it from config would DESTROY a live role that GitHub Actions workflows assume on
# every agent build. Two `removed` blocks used to sit here to make Terraform forget it
# instead, leaving the live role for the backend's `ensure_shared_role` to adopt by name.
# They were deleted in E32/T6 once `terraform.tfstate` (serial 817) was confirmed to hold
# neither IAM address — see the state note in the Agent Registry section above, which
# applies verbatim here: the deletion is proven safe for THIS state file only.

# ============================================================================
# Agent ECR (E21 Runtime Provisioning)
# Shared repo for materialized agent images. The GitHub-OIDC push role that used
# to live here is now platform-provisioned — see the block above.
# ============================================================================

module "agent_ecr" {
  source      = "./modules/agent_ecr"
  name_prefix = local.name_prefix

  # E25: replication destination (cross-account audit aggregation).
  # The runtime exec-role's pull grant lives on the exec role's OWN identity policy
  # (self-provisioned in-account by the agentcore_runtime module) — not a repo policy
  # here — so it gets a real dependency edge (no cycle, no greenfield apply race).
  account_id = data.aws_caller_identity.current.account_id
  region     = var.aws_region

  # Single-account = no replication (tenant ECR == central ECR, nothing to
  # replicate; a same-account/same-region rule is rejected by ECR at apply).
  # Enable cross-account replication by setting replication_destination_account_id
  # (+ optional replication_destination_region) — left unset here on purpose.

  tags = var.tags
}

# ============================================================================
# Default-tenant bootstrap (E25/T7)
# Creates the agp-deployment-*-default deploy-role only. Seeding the `default` tenant
# row is a DATA step, not infrastructure: the operator runs
# backend/scripts/seed_default_tenant.py once AFTER apply (see the module's own header
# for the exact command, and step 6 of the root README).
# ============================================================================
module "default_tenant" {
  source = "./modules/default_tenant"

  name_prefix        = local.name_prefix
  codebuild_role_arn = module.codebuild.role_arn
  # E36/T11 — the deploy-role's SECOND trust principal. CodeBuild provisions the runtime;
  # the backend task role tears it down (E36/T8's assume-role seam), and until now nothing
  # admitted it, so every cross-account teardown asked the control-plane account and
  # reported a false `deleted`. No cycle: modules/ecs consumes nothing from default_tenant.
  backend_task_role_arn = module.ecs.task_role_arn

  tags = var.tags
}

# ============================================================================
# State Backend Module (Terraform Remote State)
# ============================================================================

module "state_backend" {
  source = "./modules/state_backend"

  name_prefix = local.name_prefix
  environment = var.environment

  bucket_name_prefix = var.state_backend_bucket_name_prefix
  lock_table_name    = var.state_backend_lock_table_name

  tags = var.tags
}

# ============================================================================
# Secrets Manager Module (prototype — Entra backend Graph client secret)
# ============================================================================
# Stores the confidential client secret for the platform's "Backend Graph
# Client" Entra app registration. The FastAPI backend fetches it at runtime
# via boto3 (production) or reads it from .env.local (local dev).
# See: docs/entra-setup.md

module "secrets_manager" {
  source = "./modules/secrets_manager"

  name_prefix  = "agp-${var.environment}"
  secret_value = var.entra_backend_client_secret

  tags = var.tags
}

# ============================================================================
# CloudFront + S3 for Frontend
# ============================================================================

module "cloudfront" {
  source = "./modules/cloudfront"

  providers = {
    aws.us_east_1 = aws.us_east_1
  }

  name_prefix         = local.name_prefix
  environment         = var.environment
  frontend_bucket_id  = module.s3.frontend_bucket_id
  frontend_bucket_arn = module.s3.frontend_bucket_arn
  domain_name         = var.domain_name
  hosted_zone_id      = var.hosted_zone_id

  tags = var.tags
}

# ============================================================================
# Observability Module (CloudWatch, Alarms)
# ============================================================================

module "observability" {
  source = "./modules/observability"

  name_prefix = local.name_prefix
  environment = var.environment

  # ECS monitoring
  ecs_cluster_name = module.ecs.cluster_name
  ecs_service_name = module.ecs.service_name

  # API Gateway monitoring
  api_gateway_id   = module.api_gateway.api_id
  api_gateway_name = module.api_gateway.api_name

  tags = var.tags
}

# ============================================================================
# Langfuse Observability Module (E26)
# ============================================================================
# Langfuse v3 self-hosted (Aurora Postgres + ElastiCache + ClickHouse on ECS,
# fronted by an INTERNET-FACING ALB + CloudFront with a Lambda@Edge auto-login).
# `alb_scheme` defaults to "internet-facing" and no override is passed below, so the
# ALB sits in the public subnets; what restricts it is the CloudFront prefix-list
# ingress on its SG plus the x-origin-verify header rule, not a private placement
# (see modules/langfuse/README.md). Ported from templates/foundation-stack; now
# applied by the standard `terraform apply` instead of the retired
# CodeBuild/Step-Functions deployments pipeline.
#
# The VPC + subnets are injected directly by ID from the base networking locals
# (the foundation-stack's existing_vpc_id data-source + name-tag subnet discovery
# are gone). The seed org (LANGFUSE_INIT_ORG_ID="seed-org") + seed project/key are
# created headlessly at container boot; the per-agent provisioner (E26/T4) creates
# additional projects against that seed org via the Lambda@Edge auto-login.
#
# NOTE: null_resource.push_images runs docker pull/push on the apply host, so the
# operator's machine/CI runner needs a running Docker/finch engine + Docker Hub
# egress. See modules/langfuse/README.md.
module "langfuse" {
  source = "./modules/langfuse"

  # Lambda@Edge (auto-login + strip-frame-headers) must be created in us-east-1
  # regardless of var.aws_region, or CloudFront rejects the association with
  # InvalidLambdaFunctionAssociation. Everything else in the module uses the
  # default regional provider.
  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  name        = "${local.name_prefix}-langfuse"
  environment = var.environment

  vpc_id             = local.vpc_id
  private_subnet_ids = local.private_subnet_ids
  public_subnet_ids  = local.public_subnet_ids

  langfuse_init_user_email    = var.langfuse_admin_email
  langfuse_init_user_password = var.langfuse_admin_password

  # No Cognito in the base stack (Entra ID is the sole IdP) — SSO stays off
  # (cognito_user_pool_id defaults to "") and Langfuse uses username/password
  # login for the seed admin. The module has no tags variable — it stamps its
  # own Name tags and inherits the provider default_tags.
}
