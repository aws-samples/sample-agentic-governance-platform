# ============================================================================
# ECS Cluster
# ============================================================================

resource "aws_ecs_cluster" "main" {
  name = "${var.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-cluster"
  })
}

# ============================================================================
# CloudWatch Log Group
# ============================================================================

resource "aws_cloudwatch_log_group" "ecs" {
  name = "/ecs/${var.name_prefix}"
  # E28D security pass (CKV_AWS_338): these are the control plane's own audit
  # trail — 7 days is below the 1-year floor a governance platform should keep.
  retention_in_days = 365

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-logs"
  })
}

# ============================================================================
# IAM Roles
# ============================================================================

# ECS Task Execution Role (for pulling images, writing logs)
resource "aws_iam_role" "ecs_task_execution" {
  name = "${var.name_prefix}-ecs-task-execution-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# The task definition's `secrets` block injects ENTRA_BACKEND_CLIENT_SECRET from
# Secrets Manager at task start. ECS resolves `valueFrom` ARNs with the EXECUTION
# role, so it (not the task role) needs GetSecretValue on that specific ARN.
# Gated on auth_provider (a plan-time-known string) rather than the secret ARN, so
# the count is resolvable during plan even while the secret is being created/replaced
# (an "(known after apply)" ARN in the count would fail with "Invalid count argument").
resource "aws_iam_role_policy" "ecs_task_execution_secrets" {
  count = var.auth_provider == "entra" ? 1 : 0

  name = "entra-secret-injection"
  role = aws_iam_role.ecs_task_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = var.entra_backend_client_secret_arn
      }
    ]
  })
}

# ECS Task Role (for application permissions)
resource "aws_iam_role" "ecs_task" {
  name = "${var.name_prefix}-ecs-task-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = var.tags
}

# Policy for DynamoDB access
resource "aws_iam_role_policy" "ecs_task_dynamodb" {
  name = "dynamodb-access"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan"
        ]
        Resource = [
          var.application_catalog_table_arn,
          "${var.application_catalog_table_arn}/index/*",
          var.deployment_metadata_table_arn,
          "${var.deployment_metadata_table_arn}/index/*",
          var.app_factory_table_arn,
          "${var.app_factory_table_arn}/index/*",
          var.guardrails_table_arn,
          "${var.guardrails_table_arn}/index/*",
          var.marketplace_table_arn,
          "${var.marketplace_table_arn}/index/*",
          var.connections_table_arn,
          "${var.connections_table_arn}/index/*",
          var.projects_table_arn,
          "${var.projects_table_arn}/index/*",
          var.tenants_table_arn,
          "${var.tenants_table_arn}/index/*"
        ]
      }
    ]
  })
}

# Policy for S3 access
resource "aws_iam_role_policy" "ecs_task_s3" {
  name = "s3-access"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket"
        ]
        Resource = [
          var.project_archives_bucket_arn,
          "${var.project_archives_bucket_arn}/*",
          var.frontend_bucket_arn,
          "${var.frontend_bucket_arn}/*"
        ]
      }
    ]
  })
}

# Policy for Step Functions access
resource "aws_iam_role_policy" "ecs_task_step_functions" {
  name = "step-functions-access"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "states:StartExecution",
          "states:DescribeExecution",
          "states:StopExecution",
          "states:ListExecutions"
        ]
        Resource = "arn:aws:states:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:stateMachine:${var.name_prefix}-*"
      }
    ]
  })
}

# Policy for CodeBuild StartBuild (E22/T6 build-trigger endpoint) — least-privilege:
# StartBuild on the single runtime-provision project only. The GitHub Action authenticates
# to the endpoint via GitHub OIDC; the endpoint (this task role) starts the build.
#
# DELIBERATELY UNCONDITIONAL — do not re-add `count = var.codebuild_project_arn != "" ? 1 : 0`.
# That guard was here from E22/T6 until it broke the first apply ever run against an EMPTY
# state (2026-08-09). `count` must be resolved while Terraform is still building the plan
# graph, but `var.codebuild_project_arn` is `module.codebuild.project_arn` — unknown until
# apply — so the condition was neither true nor false and Terraform aborted with
# "Invalid count argument". Note the ARN is used freely in the policy body below: an
# unknown value is fine INSIDE a resource, it just cannot decide whether the resource
# exists. The guard was also moot: CodeBuild is not optional in this platform (the module
# has no count/toggle and applies every time — it is what runs the runtime Terraform when
# an agent is pushed), and the root has always passed a real ARN. `var.codebuild_project_arn`
# is now a REQUIRED input, so there is no empty case left to guard.
resource "aws_iam_role_policy" "ecs_task_codebuild_startbuild" {
  name = "codebuild-startbuild"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "codebuild:StartBuild"
        Resource = var.codebuild_project_arn
      }
    ]
  })
}

# Policy for per-org GitHub-OIDC ECR-push role management (E22 multi-org bugfix) —
# least-privilege: the backend provisions/tears down one IAM role PER connected org
# (trust sub = repo:<org>/*:*) so a repo in org A cannot assume org B's push role. Scoped
# by name to the "<prefix>-ecr-push-*" roles ONLY — this task role can touch no other IAM
# role. No iam:PassRole here: GitHub Actions assumes the role via OIDC; the task never does.
#
# E28C/T5 (D-C5) — A SECOND STATEMENT, because the first one could never match the role the
# delete cascade was written to reclaim. E28A/T2 gave the backend an exec-role reclaim
# (`_reclaim_exec_role`) targeting `{agent_name}-{stage}-agentcore-exec`, created by
# modules/agentcore_runtime. This policy's only resource is `role/{prefix}-ecr-push-*`, so the
# LIVE answer was always AccessDenied — and the code swallowed it into a success report. Six
# account-global roles leaked behind clean teardown reports. The backend now REPORTS a denied
# reclaim (its own non-blocking `exec_role` cascade item), and this grant is what lets the reclaim
# succeed in the first place. Both halves are needed: the grant without the report re-hides the
# next failure, the report without the grant just makes every delete honest about failing.
resource "aws_iam_role_policy" "ecs_task_ecr_push_role_mgmt" {
  name = "ecr-push-role-mgmt"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ManageEcrPushRoles"
        Effect = "Allow"
        Action = [
          "iam:CreateRole",
          "iam:DeleteRole",
          "iam:GetRole",
          "iam:TagRole",
          "iam:UpdateAssumeRolePolicy",
          "iam:PutRolePolicy",
          "iam:DeleteRolePolicy",
        ]
        Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.name_prefix}-ecr-push-*"
      },
      {
        # The SHARED platform-default push role, which this module's pattern above cannot
        # match: it is `{prefix}-agent-ecr-push` (no `-ecr-push-<org>` suffix). It used to be
        # a Terraform resource in modules/agent_ecr, but its trust names the GitHub OIDC
        # provider and IAM validates that principal at role-create time — un-creatable at
        # apply on an account with no provider. The backend now bootstraps it on the first
        # GitHub connection (`ecr_push_role_service.ensure_shared_role`), under the same name
        # so an already-deployed account's live role is adopted.
        #
        # A SEPARATE statement rather than a widened pattern above: making that pattern
        # `{prefix}-*ecr-push*` would also admit every future `{prefix}-…-ecr-push…` role,
        # and this one is a single EXACT name — no wildcard needed, so none is granted.
        #
        # NO DeleteRole/DeleteRolePolicy: the shared role is the fallback stamped onto repos
        # whose connection has no per-org role, so no single disconnect owns it and the
        # backend never deletes it (see the module docstring). Granting a delete it never
        # issues would only be a way to lose the role.
        Sid    = "ManageSharedEcrPushRole"
        Effect = "Allow"
        Action = [
          "iam:CreateRole",
          "iam:GetRole",
          "iam:TagRole",
          "iam:PutRolePolicy",
        ]
        Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.name_prefix}-agent-ecr-push"
      },
      {
        # RECLAIM-ONLY, deliberately narrower than the statement above: delete the role and its
        # inline policy, and read it (the reclaim tolerates NoSuchEntity as already-done). NO
        # CreateRole/PutRolePolicy/UpdateAssumeRolePolicy — Terraform is the only thing that
        # CREATES these roles, and a backend able to mint or re-trust them would be a privilege
        # escalation path (a role whose trust policy it controls) for no benefit.
        #
        # The name pattern has no `{prefix}` to anchor on: the role name is
        # `{agent_name}-{stage}-agentcore-exec` and the agent name is operator-chosen (AGENT_NAME_RE,
        # <= 32 chars), so the platform prefix appears nowhere in it. The `-agentcore-exec` SUFFIX is
        # the anchor instead, and it is the module's own literal — the same string
        # `agentcore_exec_role_name()` produces, pinned against this file by
        # `test_exec_role_name_matches_the_terraform_module_that_creates_it`.
        #
        # This DOES admit any `*-agentcore-exec` role in the account, including one created by
        # something other than this platform. Accepted: the suffix is a namespace AGP defines, and
        # narrowing it further would require the backend to pass its own name prefix into a module
        # that does not have it, re-creating the "grant cannot match the name" drift this statement
        # exists to fix.
        Sid    = "ReclaimAgentcoreExecRoles"
        Effect = "Allow"
        Action = [
          "iam:DeleteRole",
          "iam:DeleteRolePolicy",
          "iam:GetRole",
        ]
        Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/*-agentcore-exec"
      }
    ]
  })
}

# Policy for assuming a TENANT's deploy-role (E36/T11) — the grant that makes E36/T8's
# cross-account teardown seam able to succeed instead of only able to fail honestly.
#
# THE DEFECT. Every AWS client in the backend service layer was built from THIS role's
# ambient credentials, which live in the control-plane account. A tenant stage carrying a
# `deploy_role_arn` deploys its runtime into the TENANT's account, so the teardown paths
# asked the wrong account and got the truthful answer `ResourceNotFoundException` /
# `NoSuchEntity` — which is ALSO the idempotent already-done state. The delete cascade
# therefore reported `deleted` on a runtime that kept billing and an account-global
# `*-agentcore-exec` role that kept blocking re-materialization. Same defect class as the
# `ReclaimAgentcoreExecRoles` statement above (E28C/T5), one account boundary further out.
# E36/T8 made the backend assume the tenant's deploy-role first
# (`services/tenant_credentials.stage_client`) and report `outcome="failed"` with an
# `assume_role_failed:` reason when it cannot. Without this grant that report is ALL that
# ever happens; with it the teardown actually reclaims. Both halves are required — the
# grant alone would re-hide the next failure, the report alone is just a louder leak.
#
# SCOPED TO THE ROLE-NAME NAMESPACE, mirroring modules/codebuild's `sts-assume-role`
# (`arn:aws:iam::*:role/agp-deployment-*`) — the same wildcard the deploy-role's NAME
# contract in modules/default_tenant exists to satisfy. That name pattern IS the
# authorization boundary here, and it is load-bearing for a specific reason: the ARN this
# role assumes comes from a tenant record, i.e. from a tenant-ADMIN write, so a wider
# resource (`role/*`) would let that write aim the control plane at an arbitrary role —
# including a privileged role in THIS account, where a `Principal: {AWS: "<self>:root"}`
# trust is common. That is a privilege-escalation path, and the paired half of closing it is
# the write-side ARN validation in `TenantService._validate_stages` (E36/T11, review B-4).
#
# NO ACCOUNT-EXCLUSION CONDITION, deliberately. Excluding this account would break the
# DEFAULT install: modules/default_tenant creates `agp-deployment-<prefix>-default` in the
# control-plane account itself, and a single-account tenant whose record carries that ARN
# must still be able to reclaim — denying it would re-manufacture the exact false `deleted`
# above. (`aws:PrincipalAccount` would also be the wrong key: the principal is always this
# account.) The `agp-deployment-*` namespace is the whole boundary.
#
# The wildcard ACCOUNT is unavoidable and not a widening: tenant accounts are customer-owned
# and unknown at apply time — the same reason modules/codebuild has carried this exact
# resource since E25. Cross-account access still requires the OTHER side to trust this role
# in its own trust policy, which is a hand-built, per-tenant, customer-side act
# (docs/tenant-account-onboarding.md), so this grant on its own reaches nothing.
resource "aws_iam_role_policy" "ecs_task_tenant_deploy_role_assume" {
  name = "tenant-deploy-role-assume"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AssumeTenantDeployRole"
        Effect = "Allow"
        Action = "sts:AssumeRole"
        # Kept in lockstep with modules/default_tenant's role NAME and modules/codebuild's
        # identical resource — one contract, three sides. Changing any one alone breaks
        # either provisioning or teardown.
        Resource = "arn:aws:iam::*:role/agp-deployment-*"
      }
    ]
  })
}

# Policy for the account-global GitHub Actions OIDC provider bootstrap.
#
# Git-provider integrations are a PLATFORM capability, so this stack ships no GitHub
# artifacts and the backend creates the provider on the FIRST GitHub connection
# (`services/github_oidc_provider_service.py`). This is the grant that lets it. The provider
# had to leave Terraform because every GitHub-OIDC role's trust policy names it as the
# `Federated` principal and IAM validates that principal EXISTS at role-create time — on a
# provider-less account no ordering of apply-time resources works.
#
# Scoped to the ONE provider ARN, which is exact because OIDC-provider ARNs are
# deterministic (`oidc-provider/<host>`, no random component) — so this task role can
# neither read nor create a provider for any other issuer.
#
# NO iam:DeleteOpenIDConnectProvider, and that omission is the point. The provider is an
# ACCOUNT-GLOBAL SINGLETON: anything else in the account that trusts GitHub Actions (other
# stacks, other teams, roles AGP never created) breaks the instant it is removed. The
# backend has no delete path for it, and granting one would only make a disconnect able to
# take down every unrelated consumer.
resource "aws_iam_role_policy" "ecs_task_github_oidc_provider" {
  name = "github-oidc-provider-bootstrap"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ManageGithubOidcProvider"
        Effect = "Allow"
        Action = [
          "iam:GetOpenIDConnectProvider",
          "iam:CreateOpenIDConnectProvider",
          "iam:TagOpenIDConnectProvider",
        ]
        Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
      }
    ]
  })
}

# Policy for agent-images ECR image delete (E23/T7 delete cascade) — the backend
# removes an agent's pushed image tags from the shared agent-images repo on delete.
# Scoped to the single agent-images repo ARN (least-privilege); reuses the same var
# that feeds the AGENT_IMAGES_ECR_ARN backend env.
#
# DELIBERATELY UNCONDITIONAL — same story as the codebuild-startbuild policy above (see
# its comment for the full reasoning). `count = var.agent_images_ecr_arn != "" ? 1 : 0`
# broke the first apply against an empty state, because the ARN is
# `module.agent_ecr.repository_arn` and so is unknown at plan time. The shared agent-images
# repo is not optional either — it is where every materialized agent's image is pushed —
# and the root always passes its ARN, so `var.agent_images_ecr_arn` is now REQUIRED.
resource "aws_iam_role_policy" "ecs_task_agent_images_ecr_delete" {
  name = "agent-images-ecr-delete"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchDeleteImage",
          "ecr:ListImages",
          "ecr:DescribeImages"
        ]
        Resource = var.agent_images_ecr_arn
      }
    ]
  })
}

# Policy for CloudWatch Logs access (CodeBuild logs + AgentCore runtime logs)
resource "aws_iam_role_policy" "ecs_task_logs" {
  name = "codebuild-logs-access"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:GetLogEvents",
          "logs:DescribeLogStreams"
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/codebuild/*"
      },
      {
        Effect = "Allow"
        Action = [
          "logs:FilterLogEvents",
          "logs:GetLogEvents",
          "logs:DescribeLogStreams",
        ]
        Resource = [
          "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock-agentcore/*",
          "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock-agentcore/*:log-stream:*",
          "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/vendedlogs/bedrock-agentcore/*",
          "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/vendedlogs/bedrock-agentcore/*:log-stream:*",
        ]
      }
    ]
  })
}

# Policy for Bedrock Guardrails and CloudWatch Metrics
resource "aws_iam_role_policy" "ecs_task_bedrock_guardrails" {
  name = "bedrock-guardrails-access"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "bedrock:CreateGuardrail",
          "bedrock:UpdateGuardrail",
          "bedrock:DeleteGuardrail",
          "bedrock:GetGuardrail",
          "bedrock:ListGuardrails",
          "bedrock:CreateGuardrailVersion"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "cloudwatch:GetMetricData"
        ]
        Resource = "*"
      }
    ]
  })
}

# ============================================================================
# Security Groups
# ============================================================================

resource "aws_security_group" "ecs_tasks" {
  name_prefix = "${var.name_prefix}-ecs-tasks-"
  description = "Security group for ECS tasks"
  vpc_id      = var.vpc_id

  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
    description     = "Allow traffic from ALB"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Allow all outbound traffic"
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-ecs-tasks-sg"
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "alb" {
  name_prefix = "${var.name_prefix}-alb-"
  description = "Security group for Application Load Balancer"
  vpc_id      = var.vpc_id

  # E28D security pass (CKV_AWS_260): was cidr_blocks = ["0.0.0.0/0"] on both
  # ports. This ALB is `internal = true` and is only ever reached through the API
  # Gateway VPC link, so the whole internet was admitted for nothing — `internal`
  # was doing the work the SG should do. Scoped to the VPC CIDR (defence in depth).
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.current.cidr_block]
    description = "HTTP from inside the VPC (API Gateway VPC link)"
  }

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.current.cidr_block]
    description = "HTTPS from inside the VPC (API Gateway VPC link)"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Allow all outbound traffic"
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-alb-sg"
  })

  lifecycle {
    create_before_destroy = true
  }
}

# ============================================================================
# Application Load Balancer
# ============================================================================

resource "aws_lb" "main" {
  name               = "cp-${var.environment}-alb"
  internal           = true
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids

  enable_deletion_protection = false
  enable_http2               = true

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-alb"
  })
}

resource "aws_lb_target_group" "main" {
  name        = "cp-${var.environment}-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    enabled             = true
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    path                = "/health"
    matcher             = "200"
  }

  deregistration_delay = 30

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-tg"
  })
}

resource "aws_lb_listener" "main" {
  load_balancer_arn = aws_lb.main.arn
  port              = "80"
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.main.arn
  }
}

# ============================================================================
# ECS Task Definition
# ============================================================================

resource "aws_ecs_task_definition" "main" {
  family                   = "${var.name_prefix}-task"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "backend"
      image     = var.container_image
      essential = true

      portMappings = [
        {
          containerPort = 8000
          protocol      = "tcp"
        }
      ]

      environment = [
        {
          name  = "ENVIRONMENT"
          value = var.environment
        },
        {
          name  = "ROOT_PATH"
          value = "/${var.environment}"
        },
        # === Prototype — Entra ID ===
        # Three plain env vars + one injected secret (see `secrets` block below)
        # are everything the backend needs to (a) validate inbound user JWTs and
        # (b) call Microsoft Graph as the platform service principal. Everything
        # else (token endpoints, JWKS, issuer, role names, Graph base URL) is
        # derived in code from these.
        # See docs/entra-setup.md for how these three values are obtained.
        {
          name  = "AUTH_PROVIDER"
          value = var.auth_provider
        },
        {
          name  = "ENTRA_TENANT_ID"
          value = var.entra_tenant_id
        },
        {
          name  = "ENTRA_AUDIENCE"
          value = var.entra_audience
        },
        {
          name  = "ENTRA_SPA_CLIENT_ID"
          value = var.entra_spa_client_id
        },
        {
          name  = "ENTRA_BACKEND_CLIENT_ID"
          value = var.entra_backend_client_id
        },
        {
          name  = "REFERENCE_IMPLEMENTATIONS_DIR"
          value = "/app/reference_implementations"
        },
        {
          name  = "APPLICATION_CATALOG_TABLE"
          value = var.application_catalog_table_name
        },
        {
          name  = "DEPLOYMENT_METADATA_TABLE"
          value = var.deployment_metadata_table_name
        },
        {
          name  = "PROJECT_ARCHIVES_BUCKET"
          value = var.project_archives_bucket_name
        },
        {
          name  = "PROJECT_ECR_REPOSITORY"
          value = var.project_ecr_repository
        },
        {
          name  = "PROJECT_ECR_PUSH_ROLE_ARN"
          value = var.project_ecr_push_role_arn
        },
        # E22 bugfix: written as the AGP_API_URL repo var on materialized agent repos so the
        # scaffold build.yml trigger job can POST to ${AGP_API_URL}/builds/runtime. Empty ⇒
        # the backend omits the repo var (set_repo_variables skips empty values).
        {
          name  = "AGP_API_URL"
          value = var.agp_api_url
        },
        # E22 multi-org: per-org GitHub-OIDC ECR-push role provisioning. The backend
        # creates <prefix>-ecr-push-<org> on connect and deletes it on disconnect.
        {
          name  = "ECR_PUSH_ROLE_NAME_PREFIX"
          value = var.name_prefix
        },
        {
          name  = "GITHUB_OIDC_PROVIDER_ARN"
          value = var.github_oidc_provider_arn
        },
        {
          name  = "AGENT_IMAGES_ECR_ARN"
          value = var.agent_images_ecr_arn
        },
        {
          name  = "FRONTEND_BUCKET"
          value = var.frontend_bucket_name
        },
        {
          name  = "GUARDRAILS_TABLE_NAME"
          value = var.guardrails_table_name
        },
        {
          name  = "AWS_DEFAULT_REGION"
          value = data.aws_region.current.region
        },
        {
          name  = "CONTROL_PLANE_VPC_ID"
          value = var.vpc_id
        },
        {
          name  = "APP_FACTORY_TABLE_NAME"
          value = var.app_factory_table_name
        },
        {
          name  = "CORS_ORIGINS"
          value = jsonencode(var.cors_origins)
        },
        {
          name  = "MARKETPLACE_TABLE_NAME"
          value = var.marketplace_table_name
        },
        {
          name  = "CONNECTIONS_TABLE_NAME"
          value = var.connections_table_name
        },
        # === Ops Template Provisioning (E20) ===
        {
          name  = "PROJECTS_TABLE_NAME"
          value = var.projects_table_name
        },
        # === Build-trigger endpoint (E22/T6) ===
        {
          name  = "CODEBUILD_PROJECT_NAME"
          value = var.codebuild_project_name
        },
        # === Runtime-infra module archive (E22 bugfix-A) ===
        # The rollout downloads this S3 zip and pushes it as the agp-runtime-infra repo.
        {
          name  = "RUNTIME_MODULE_BUCKET"
          value = var.runtime_module_bucket
        },
        {
          name  = "RUNTIME_MODULE_KEY"
          value = var.runtime_module_key
        },
        # === Multi-tenancy (E24) ===
        {
          name  = "TENANTS_TABLE_NAME"
          value = var.tenants_table_name
        },
        # === Databricks tenant credentials (E29/T3) ===
        # Environment-scoped so a prod deploy namespaces its Databricks secrets under
        # agp-prod/… instead of silently inheriting the backend default's literal "agp-dev/…".
        # Must stay in lockstep with the IAM statements below that grant on this prefix.
        {
          name  = "DATABRICKS_TENANT_SECRET_PREFIX"
          value = "agp-${var.environment}/databricks-tenants/"
        },
        # === Agent Registry (E4 — AWS Agent Registry, `agent-registry` namespace) ===
        # NAME + REGION ONLY — there is deliberately no AGENT_REGISTRY_ID here. AWS mints the
        # registryId and it cannot be chosen, so the root could only obtain it from a capture
        # file its script-backed `agent_registry` module read during the PLAN walk, before the
        # provisioner that writes it had run: apply #1 baked an EMPTY id into this very task
        # definition and apply #2 replaced it, which is why a from-zero deploy needed two
        # applies. These two values, by contrast, are static tfvars known at plan time, and
        # `AgentRegistryService` resolves NAME -> id itself on first use (one `ListRegistries`
        # call, memoised — see backend/src/core/registry_resolver.py). AGENT_REGISTRY_ID is
        # still honoured by the backend as an explicit override if one is ever set by hand.
        {
          name  = "AGENT_REGISTRY_NAME"
          value = var.agent_registry_name
        },
        {
          name  = "AGENT_REGISTRY_REGION"
          value = var.agent_registry_region
        },
        # === MCP Server Registry (E5 — separate AWS Agent Registry for MCP-type records) ===
        # Same shape and same reason: `McpServerRegistryService` resolves MCP_REGISTRY_NAME to
        # an id at first use, so no MCP_REGISTRY_ID is passed.
        {
          name  = "MCP_REGISTRY_NAME"
          value = var.mcp_registry_name
        },
        {
          name  = "MCP_REGISTRY_REGION"
          value = var.mcp_registry_region
        },
        # === Langfuse Observability (E26) ===
        # Plaintext host + admin-secret NAME (not the secret value). The backend
        # reads the secret contents at runtime via boto3 in later tasks.
        {
          name  = "LANGFUSE_HOST"
          value = var.langfuse_host
        },
        {
          name  = "LANGFUSE_ADMIN_SECRET_NAME"
          value = var.langfuse_admin_secret_name
        }
      ]

      # ECS resolves each `valueFrom` ARN at task start using the EXECUTION role
      # (not the task role) and injects the plaintext value as the named env var.
      # The backend reads ENTRA_BACKEND_CLIENT_SECRET like any other env var — no
      # boto3 / Secrets Manager call happens in app code. Gated on auth_provider (a
      # plan-time-known string) rather than the secret ARN, so the block stays
      # resolvable during plan even while the secret is being created/replaced.
      # The empty branch is defensive rather than reachable from the root stack, whose
      # `auth_provider` variable validates to "entra": a `secrets` entry with an empty
      # valueFrom is invalid, so any other value must yield no entry at all.
      secrets = var.auth_provider == "entra" ? [
        {
          name      = "ENTRA_BACKEND_CLIENT_SECRET"
          valueFrom = var.entra_backend_client_secret_arn
        }
      ] : []

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.ecs.name
          "awslogs-region"        = data.aws_region.current.region
          "awslogs-stream-prefix" = "ecs"
        }
      }

      healthCheck = {
        command     = ["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }
    }
  ])

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-task"
  })
}

# ============================================================================
# ECS Service
# ============================================================================

resource "aws_ecs_service" "main" {
  name            = "${var.name_prefix}-service"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.main.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.public_subnet_ids
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.main.arn
    container_name   = "backend"
    container_port   = 8000
  }

  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 100

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-service"
  })

  depends_on = [aws_lb_listener.main]
}

# ============================================================================
# Auto Scaling
# ============================================================================

resource "aws_appautoscaling_target" "ecs" {
  max_capacity       = var.max_capacity
  min_capacity       = var.min_capacity
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.main.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "ecs_cpu" {
  name               = "${var.name_prefix}-cpu-autoscaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.ecs.resource_id
  scalable_dimension = aws_appautoscaling_target.ecs.scalable_dimension
  service_namespace  = aws_appautoscaling_target.ecs.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 70.0
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
  }
}

resource "aws_appautoscaling_policy" "ecs_memory" {
  name               = "${var.name_prefix}-memory-autoscaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.ecs.resource_id
  scalable_dimension = aws_appautoscaling_target.ecs.scalable_dimension
  service_namespace  = aws_appautoscaling_target.ecs.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageMemoryUtilization"
    }
    target_value       = 70.0
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
  }
}

# ============================================================================
# Data Sources
# ============================================================================

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# VPC CIDR for the ALB security group (E28D: replaces 0.0.0.0/0 ingress).
data "aws_vpc" "current" {
  id = var.vpc_id
}

resource "aws_iam_role_policy" "ecs_task_sts" {
  name = "sts-access"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sts:GetCallerIdentity"]
        Resource = "*"
      }
    ]
  })
}

# Policy for CodeCommit read access (backend lists seeded use case repos)
resource "aws_iam_role_policy" "ecs_task_codecommit" {
  name = "codecommit-read-access"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "codecommit:ListRepositories",
          "codecommit:GetRepository",
          "codecommit:ListBranches",
          "codecommit:GetBranch"
        ]
        Resource = "*"
      }
    ]
  })
}

# Policy for Bedrock AgentCore access (test deployment invocations)
#
# E32 NAME NOTE: this policy now grants two namespaces (`bedrock-agentcore` +
# `agent-registry`), so `agentcore-and-registry-access` would read better. The name is
# deliberately LEFT ALONE: `name` is part of this resource's identity, so changing it makes
# Terraform delete-then-recreate the inline policy on the LIVE ECS task role. Inline-policy
# replacement is not atomic, so any in-flight request during the apply window sees a task
# role with no AgentCore grant at all — a real (if brief) outage, traded for a cosmetic
# rename. The comments below carry the meaning instead.
resource "aws_iam_role_policy" "ecs_task_bedrock_agentcore" {
  name = "bedrock-agentcore-access"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          # Broad bedrock-agentcore data/control access. E23 delete teardown showed
          # DeleteAgentRuntime cascades server-side through several sub-resource deletes
          # (workload identity, runtime endpoint(s), …) — enumerating each proved brittle
          # (repeated live AccessDenied whack-a-mole), so the runtime/identity/endpoint
          # lifecycle is granted as a wildcard here. Registry/gateway/oauth/policy actions
          # below stay enumerated. Dev/tooling account; revisit scoping before prod.
          "bedrock-agentcore:*",
          # Agent Registry (E4) — CRUD + lifecycle on the registry and its records.
          #
          # E32: AWS moved ONLY Registry from `bedrock-agentcore` to `agent-registry`
          # (2026-08-06; the old namespace shuts down 2026-09-17 and is unreachable from
          # accounts created after the split). Registry calls are now authorized under
          # `agent-registry:`. VERIFIED against the botocore model this backend pins
          # (botocore 1.43.67): BOTH new clients — `agent-registry-control` (CRUD +
          # lifecycle) and `agent-registry` (discoverable-record search) — carry
          # `signingName = "agent-registry"`, and signingName is what IAM evaluates. So
          # `bedrock-agentcore:*` below no longer authorizes a single registry call.
          #
          # This grant is DEPLOY-BLOCKING and invisible to the offline test suite: the
          # only symptom is a runtime AccessDenied on every registry request while every
          # unit test stays green. That is exactly why tests/test_iam_registry_actions.py
          # asserts over this file's text rather than over any Python behaviour.
          #
          # This is an ADD, not a replace — workload identity + OAuth credential providers
          # deliberately STAY in the bedrock-agentcore namespace (see below), and the
          # AWS-managed BedrockAgentCoreFullAccess policy will NOT be updated to cover
          # agent-registry (its replacement for that is AgentRegistryFullAccess). The
          # wildcard matches the `bedrock-agentcore:*` idiom already in this statement and
          # covers the control plane (Create/Get/Update/List/Delete on Registry and
          # RegistryRecord, SubmitRegistryRecordForApproval, UpdateRegistryRecordStatus)
          # plus the data plane (SearchDiscoverableRegistryRecords,
          # ListDiscoverableRegistryRecords, BatchGetDiscoverableRegistryRecord).
          # Resource = "*" matches the existing wildcard style; resource-level ARNs are a
          # later hardening item.
          "agent-registry:*",
          # E32 DELETED THREE THINGS HERE, none of which needs replacing:
          #   1. ten `bedrock-agentcore-control:*Registry*` entries. That prefix was NEVER
          #      a real IAM namespace — `-control` is the boto3 client/endpoint name, and
          #      that model's signingName was always plain `bedrock-agentcore` — so the
          #      "belt-and-suspenders" block granted precisely nothing.
          #   2. the old namespace's un-prefixed record-search action, now dead: it was
          #      renamed `SearchDiscoverableRegistryRecords` and moved to the data plane,
          #      so the pre-rename name authorizes nothing under either namespace today.
          #      Its literal spelling is deliberately ABSENT from this file — the guard
          #      test asserts the dead name appears nowhere here, comments included, so
          #      that a future copy-paste cannot quietly reintroduce it.
          #   3. ten `bedrock-agentcore:*Registry*` entries, superseded by
          #      `agent-registry:*` above (and still covered by the `bedrock-agentcore:*`
          #      wildcard for as long as the old namespace answers at all).
          #
          # Workload identity STAYS in `bedrock-agentcore` — AWS split out Registry ONLY.
          # Verified in the same model dump: Create/Get/Update/Delete/ListWorkloadIdentity
          # are still `bedrock-agentcore-control` operations with signingName
          # `bedrock-agentcore`. These three are a strict subset of the
          # `bedrock-agentcore:*` wildcard above, so spelling them out grants nothing new;
          # they are named so the namespace boundary is self-documenting and so the E32
          # guard test can prove the retention was deliberate and not an oversight. The
          # runtime delete cascade goes through workload identity (see wildcard note above).
          "bedrock-agentcore:CreateWorkloadIdentity",
          "bedrock-agentcore:GetWorkloadIdentity",
          "bedrock-agentcore:DeleteWorkloadIdentity",
          # Gateway control-plane (E7) — the backend reads a manually-created
          # AgentCore Gateway and flips its inbound authorizer to CUSTOM_JWT
          # (GetGateway -> replay -> UpdateGateway -> poll), and reads its targets
          # for native (no-token) tool discovery. CreateGateway/CreateGatewayTarget/
          # SynchronizeGatewayTargets are intentionally NOT here: the example-gateway
          # creator is a standalone dev script run with the user's own creds, not the
          # backend. Signing name is `bedrock-agentcore` (the `-control` suffix is the
          # boto3 client/endpoint name only).
          "bedrock-agentcore:GetGateway",
          "bedrock-agentcore:UpdateGateway",
          "bedrock-agentcore:ListGateways",
          "bedrock-agentcore:ListGatewayTargets",
          "bedrock-agentcore:GetGatewayTarget",
          # OAuth2 credential providers (E7 Tier 2) — the backend creates/reads the
          # per-agent MicrosoftOauth2 provider in AgentCore's Token Vault so the agent
          # can obtain a delegated MCP token without ever holding a printable secret.
          "bedrock-agentcore:CreateOauth2CredentialProvider",
          "bedrock-agentcore:GetOauth2CredentialProvider",
          "bedrock-agentcore:UpdateOauth2CredentialProvider",
          "bedrock-agentcore:ListOauth2CredentialProviders",
          # Cedar Policy Engine (E8) — the backend creates a per-gateway Policy Engine,
          # associates it to the gateway (via UpdateGateway.policyEngineConfiguration,
          # already covered by UpdateGateway above), and adds/lists/removes Cedar policies.
          # DeletePolicyEngine is included for completeness though E8 only detaches (never
          # deletes the engine). Signing name `bedrock-agentcore` (the `-control` suffix is
          # the boto3 client name only).
          "bedrock-agentcore:CreatePolicyEngine",
          "bedrock-agentcore:GetPolicyEngine",
          "bedrock-agentcore:ListPolicyEngineSummaries",
          "bedrock-agentcore:DeletePolicyEngine",
          "bedrock-agentcore:CreatePolicy",
          "bedrock-agentcore:GetPolicy",
          "bedrock-agentcore:ListPolicySummaries",
          "bedrock-agentcore:DeletePolicy"
        ]
        Resource = "*"
      },
      {
        Sid    = "PassAgentCoreRuntimeExecutionRole"
        Effect = "Allow"
        Action = "iam:PassRole"
        # UpdateAgentRuntime (E6 authorizer config) is full-replace: the backend replays
        # the runtime's execution roleArn, so the ECS task role needs PassRole on it.
        # Two exec-role naming schemes exist and both must be passable: the console
        # creates `role/service-role/AmazonBedrockAgentCoreRuntime*`, while the
        # agentcore starter toolkit (`agentcore configure` exec-role auto-create — the
        # applications/ deploy scripts' path) creates
        # `role/AmazonBedrockAgentCoreSDKRuntime-<region>-<hash>` (note the SDK infix
        # and NO service-role path). The PassedToService condition ensures either can
        # only be passed to AgentCore.
        # The THIRD pattern is the gateway twin (E7 registration): UpdateGateway is the
        # same full-replace shape — the backend replays the gateway's service roleArn when
        # it flips the inbound authorizer to CUSTOM_JWT — and the demo-gateway bootstrap
        # scripts (scripts/bootstrap_demo_*.py) name those roles `agp-*-gateway-role`.
        Resource = [
          "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/service-role/AmazonBedrockAgentCoreRuntime*",
          "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/AmazonBedrockAgentCoreSDKRuntime*",
          "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/agp-*-gateway-role",
        ]
        Condition = {
          StringEquals = {
            "iam:PassedToService" = "bedrock-agentcore.amazonaws.com"
          }
        }
      },
      {
        Sid    = "ReadAgentCoreRuntimeArtifacts"
        Effect = "Allow"
        # UpdateAgentRuntime (E6 authorizer config) is full-replace: AgentCore re-reads
        # the runtime's agentRuntimeArtifact zip from S3 during validation, so the ECS
        # task role needs S3 read. Broad read for the prototype (the artifact bucket is
        # not known to this module); tighten to the specific bucket ARN as follow-up.
        Action = [
          "s3:GetObject",
          "s3:GetObjectVersion",
          "s3:ListBucket",
          "s3:GetBucketLocation"
        ]
        Resource = "*"
      }
    ]
  })
}

# Policy for Secrets Manager access
# - Langfuse project provisioning creates per-project secrets at runtime, so we
#   keep wildcard CreateSecret/PutSecretValue/TagResource (legacy path).
# - GetSecretValue/DescribeSecret are scoped to the four name-spaces the task
#   actually reads (see the Resource list below).
resource "aws_iam_role_policy" "ecs_task_secrets_manager" {
  name = "secrets-manager-access"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadSpecificSecrets"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret"
        ]
        # Scoped to the name-spaces this task actually reads (E28D security pass —
        # was Resource = "*", which let the backend read EVERY secret in the account).
        # Secrets Manager appends a random 6-char suffix to every ARN, hence the
        # trailing "*" on each name pattern.
        #   1. the platform's own prefix -> ${name_prefix}-* (incl. <prefix>-langfuse-secrets)
        #   2. Langfuse per-project keys -> langfuse-<use_case>-keys and
        #      langfuse-agent-<id>-keys (services/langfuse_provisioning.py)
        #   3. Terraform-created platform secrets -> agp-${environment}/* — this one IS
        #      environment-templated (modules/secrets_manager gets name_prefix =
        #      "agp-${var.environment}", e.g. agp-dev/graph-client-secret).
        #   4. THE CODE'S OWN PREFIXES ARE HARDCODED "agp-dev/" AND ARE **NOT**
        #      ENVIRONMENT-TEMPLATED. core/config.py pins three literal defaults —
        #      CONNECTIONS_SECRET_PREFIX = "agp-dev/git-connections/",
        #      RUNTIME_BUILD_TOKEN_PREFIX = "agp-dev/runtime-build-token/",
        #      GITHUB_USER_LINK_SECRET_PREFIX = "agp-dev/github-user-link/" — and NOTHING
        #      injects them into this task definition, so the backend reads "agp-dev/..."
        #      whatever var.environment says. The literal agp-dev/* entry below is therefore
        #      load-bearing: without it a deploy with environment = "prod" would grant
        #      agp-prod/* while the code still reads agp-dev/*, and EVERY git-connection
        #      token and GitHub user-link token read would fail with AccessDenied — an authz
        #      error that looks like a broken credential.
        #      The real fix is to parameterize those three settings and inject them here;
        #      that is a deferred E28D row (owner: Jannis). Until then keep BOTH entries.
        #   5. the Entra backend client secret, whose ARN is passed in explicitly.
        Resource = compact([
          "arn:aws:secretsmanager:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:secret:${var.name_prefix}-*",
          "arn:aws:secretsmanager:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:secret:langfuse-*-keys*",
          "arn:aws:secretsmanager:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:secret:agp-${var.environment}/*",
          "arn:aws:secretsmanager:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:secret:agp-dev/*",
          var.entra_backend_client_secret_arn,
        ])
      },
      {
        Sid    = "ManageDynamicSecrets"
        Effect = "Allow"
        Action = [
          "secretsmanager:CreateSecret",
          "secretsmanager:PutSecretValue",
          "secretsmanager:TagResource",
          "secretsmanager:DeleteSecret",
          "secretsmanager:UpdateSecret"
        ]
        Resource = "*"
      },
      {
        # E29: RestoreSecret, and ONLY on the Databricks tenant name-spaces.
        #
        # DeleteSecret does not destroy a secret — it SCHEDULES deletion, and the name stays
        # taken for the recovery window (7 days minimum). Re-adding a tenant stage that was
        # dropped inside that window therefore has to un-schedule the existing secret rather
        # than create a new one, which is what `tenant_service._restore_if_scheduled` does.
        # Without this grant that path fails closed as `secret_error`: the operator's only
        # recovery is to wait out the window.
        #
        # Its own statement, NOT folded into ManageDynamicSecrets above, precisely because that
        # one is `Resource = "*"`: restore is needed by exactly one code path on exactly one
        # name-space, so widening it account-wide would be a strictly larger grant than the
        # feature needs. The prefix matches the DATABRICKS_TENANT_SECRET_PREFIX injected into
        # the task definition above — the two must move together. The per-agent secrets live at
        # "<prefix>agents/<agent_id>" (routes/agents.py), i.e. nested under the same prefix, so
        # the trailing "*" covers both. The second trailing "*" is Secrets Manager's own 6-char
        # ARN suffix.
        Sid    = "RestoreDatabricksTenantSecrets"
        Effect = "Allow"
        Action = [
          "secretsmanager:RestoreSecret"
        ]
        Resource = [
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:agp-${var.environment}/databricks-tenants/*",
        ]
      }
    ]
  })
}
