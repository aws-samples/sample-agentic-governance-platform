# ============================================================================
# Default-tenant deploy-role (E25/T7)
# ============================================================================
# Provisions the cross-account deploy-role named `agp-deployment-<prefix>-default`
# — the naming both platform sts:AssumeRole grants are scoped to
# (arn:aws:iam::*:role/agp-deployment-*), trusting TWO platform principals: the
# CodeBuild role that provisions the runtime and (E36/T11) the backend ECS task
# role that tears it down.
#
# NOTE: this module used to ALSO run `backend/scripts/seed_default_tenant.py` as a
# local-exec on every apply. That seed is a DATA step (writes the default tenant row
# + stamps pre-E25 registry records), not infrastructure, so it was removed from
# Terraform: a clean deployment from zero has nothing to migrate, and infra applies
# must not depend on registry data states. Run it manually AFTER apply when needed:
#
#   cd ../backend
#   PYTHONPATH=src venv/bin/python scripts/seed_default_tenant.py \
#     --agent-registry-id "$(aws agent-registry-control list-registries \
#         --query "registries[?name=='$(terraform -chdir=../infrastructure output -raw agent_registry_name)'].registryId" \
#         --output text)" \
#     --mcp-registry-id "$(aws agent-registry-control list-registries \
#         --query "registries[?name=='$(terraform -chdir=../infrastructure output -raw mcp_registry_name)'].registryId" \
#         --output text)" \
#     --dev-ecr-uri  "$(terraform -chdir=../infrastructure output -raw agent_ecr_repository_url)" \
#     --prod-ecr-uri "$(terraform -chdir=../infrastructure output -raw agent_ecr_repository_url)" \
#     --dev-deploy-role-arn  "$(terraform -chdir=../infrastructure output -raw default_tenant_deploy_role_arn)" \
#     --prod-deploy-role-arn "$(terraform -chdir=../infrastructure output -raw default_tenant_deploy_role_arn)" \
#     --dev-push-role-arn  "$(terraform -chdir=../infrastructure output -raw agent_ecr_push_role_arn)" \
#     --prod-push-role-arn "$(terraform -chdir=../infrastructure output -raw agent_ecr_push_role_arn)" \
#     --group-id "<entra-group-object-id>"
#
# WHY THE REGISTRY IDS ARE LOOKED UP RATHER THAN PASTED, and why the flags are here at
# all. The registry ids used to be omitted from this command because the script could fall
# back to `AGENT_REGISTRY_ID` in `infrastructure/.env`. Nothing populates that any more:
# AWS mints registry ids, there is no Terraform resource for the `agent-registry`
# namespace, and the backend resolves the NAME to an id at runtime instead — so no id is
# written down anywhere and the command failed as printed on a fresh clone (loudly, with
# the lookup below in the error text, but it failed).
#
# This script takes ids ONLY — it does not accept a registry name and does not use
# `core.registry_resolver` — because it is a one-shot data step that must address exactly
# the registry the operator intends, with no resolution surprises mid-write. So the id is
# derived here from the NAME, which IS a Terraform output (`agent_registry_name` /
# `mcp_registry_name`), keeping the command copy-pasteable with nothing to substitute by
# hand except the Entra group id. Requires `--region`/`AWS_REGION` to point at the same
# region the registries were created in — the `list-registries` call is region-scoped, and
# a mismatch returns empty rather than wrong.
#
# Account/region/table names self-resolve from tfvars + STS (see the script's
# docstring); the ECR/role wiring args default to EMPTY, so pass them explicitly
# (via the terraform outputs above) or you get an unwired tenant. Idempotent —
# only-creates-when-absent; safe to re-run.

# ----------------------------------------------------------------------------
# Deploy-role — the platform assumes this to act inside the tenant's account.
# Name MUST match `agp-deployment-*` so the CodeBuild role's existing
# sts:AssumeRole (modules/codebuild: arn:aws:iam::*:role/agp-deployment-*) applies
# with no IAM widening. Single-account: same account, assume is a no-op-ish hop.
# E34/T13b: the prefix was renamed off the fork-origin naming. This name and the
# codebuild wildcard are ONE contract and moved in the same change — changing either
# side alone breaks runtime provisioning.
#
# E36/T11 — TWO PRINCIPALS, because provisioning and TEARDOWN are two different
# callers and only one of them was ever admitted here.
#
#   1. the CodeBuild role — PROVISIONS the runtime (it runs the runtime Terraform).
#   2. the backend ECS task role — TEARS IT DOWN (E36/T8's cross-account seam,
#      `services/tenant_credentials.stage_client`).
#
# Until E36/T8 every teardown client was built from the backend's AMBIENT
# control-plane credentials, so `get_agent_runtime` / `delete_role` asked the WRONG
# ACCOUNT and got the truthful answer `ResourceNotFound` / `NoSuchEntity` — which is
# also the idempotent already-done state, so the delete cascade reported `deleted` on
# a runtime that kept billing and an account-global exec role that kept blocking
# re-materialization. T8 made the backend assume this role instead and report
# `outcome="failed"` with an `assume_role_failed:` reason when it cannot. THIS is the
# half that lets it succeed: without the second principal here (and the matching
# sts:AssumeRole grant in modules/ecs) the honest report is the only thing that lands
# and every cross-account teardown fails loudly forever.
#
# Both principals are needed — dropping either one silently breaks the OTHER lifecycle
# half: no CodeBuild ⇒ nothing can be deployed; no task role ⇒ nothing can be reclaimed.
#
# The inline policy below is UNCHANGED and already carries what the teardown needs
# (`bedrock-agentcore:*` ⊇ DeleteAgentRuntime, `iam:DeleteRole`, `iam:DeleteRolePolicy`).
# BLAST RADIUS, HONESTLY: this is nonetheless a widening — the ECS task role gains its
# FIRST sts:AssumeRole, and in a single-account install THIS role lives in the control-plane
# account, so the request-serving role's effective ceiling now includes the admin-equivalent
# policy below (`iam:CreateRole`/`iam:AttachRolePolicy`/`iam:PassRole` on `Resource = "*"`,
# with no permissions boundary). Accepted for now because the alternative — an
# account-exclusion condition — breaks default-tenant teardown; narrowing this policy to the
# `*-agentcore-exec` role it actually provisions is the follow-up (see README's known
# limitations). TENANT-ACCOUNT COPIES OF THIS ROLE ARE HAND-BUILT and must be amended the
# same way: see docs/tenant-account-onboarding.md, which now names both principals.
# ----------------------------------------------------------------------------
resource "aws_iam_role" "deploy" {
  name = "agp-deployment-${var.name_prefix}-default"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = { AWS = [
          var.codebuild_role_arn,
          var.backend_task_role_arn,
        ] }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = var.tags
}

# Inline policy mirrors the runtime-provisioning scope the CodeBuild role holds
# (modules/codebuild iac-provisioning) so the assumed deploy-role can provision an
# AgentCore runtime end-to-end.
resource "aws_iam_role_policy" "deploy" {
  name = "runtime-provisioning"
  role = aws_iam_role.deploy.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "bedrock:*",
          "bedrock-agentcore:*",
          # E32: mirrors the codebuild iac-provisioning grant — Registry moved to the
          # `agent-registry` namespace (2026-08-06) and is no longer authorized by
          # `bedrock-agentcore:*`. Kept in lockstep with modules/codebuild by design.
          "agent-registry:*",
          "ecr:*",
          "ecs:*",
          "ec2:*",
          "elasticloadbalancing:*",
          "lambda:*",
          "logs:*",
          "cloudformation:*",
          "iam:CreateRole",
          "iam:DeleteRole",
          "iam:AttachRolePolicy",
          "iam:DetachRolePolicy",
          "iam:PutRolePolicy",
          "iam:DeleteRolePolicy",
          "iam:GetRole",
          "iam:GetRolePolicy",
          "iam:ListRolePolicies",
          "iam:ListAttachedRolePolicies",
          "iam:PassRole",
          "iam:CreatePolicy",
          "iam:DeletePolicy",
          "iam:GetPolicy",
          "iam:GetPolicyVersion",
          "iam:ListPolicyVersions"
        ]
        Resource = "*"
      }
    ]
  })
}

