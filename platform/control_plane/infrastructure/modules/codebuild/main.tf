# ============================================================================
# CloudWatch Log Group for CodeBuild
# ============================================================================

resource "aws_cloudwatch_log_group" "codebuild" {
  name              = "/aws/codebuild/${var.name_prefix}-deployment"
  retention_in_days = 14

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-codebuild-logs"
  })
}

# ============================================================================
# IAM Role for CodeBuild
# ============================================================================

resource "aws_iam_role" "codebuild" {
  name = "${var.name_prefix}-codebuild-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "codebuild.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = var.tags
}

# Policy for CloudWatch Logs
resource "aws_iam_role_policy" "codebuild_logs" {
  name = "cloudwatch-logs"
  role = aws_iam_role.codebuild.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = [
          aws_cloudwatch_log_group.codebuild.arn,
          "${aws_cloudwatch_log_group.codebuild.arn}:*"
        ]
      }
    ]
  })
}

# Policy for S3 access (archives read + state backend read/write)
#
# E34/T13b: a dead fork-origin bucket wildcard (Get/Put on a legacy bucket prefix and
# its objects) was dropped from the first statement. The buildspec's only S3 data-plane
# calls are on $STATE_BUCKET (:551 plus the two `terraform init -backend-config=bucket=`)
# and the legacy $ARCHIVE_BUCKET (:154), which nothing sets any more — the git-clone
# contract replaced it (backend runtime_build_service.py) — and every bucket this stack
# creates is named from `name_prefix` (`agp-cp-…`), so the wildcard matched nothing. Do
# not re-add a cross-account bucket wildcard: name the real bucket ARNs instead.
resource "aws_iam_role_policy" "codebuild_s3" {
  name = "s3-access"
  role = aws_iam_role.codebuild.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:GetBucketLocation"
        ]
        Resource = [
          var.project_archives_bucket_arn,
          "${var.project_archives_bucket_arn}/*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          # E28/T1: the buildspec zip is staged on this (versioned) bucket and is
          # downloaded as the project's S3 source. CodeBuild reads a versioned
          # object by version, so GetObjectVersion is required — without it the
          # build fails in DOWNLOAD_SOURCE, not at apply.
          "s3:GetObjectVersion",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
          "s3:GetBucketLocation"
        ]
        Resource = [
          var.state_backend_bucket_arn,
          "${var.state_backend_bucket_arn}/*"
        ]
      }
    ]
  })
}

# Policy for DynamoDB access (deployment status + state locking)
resource "aws_iam_role_policy" "codebuild_dynamodb" {
  name = "dynamodb-access"
  role = aws_iam_role.codebuild.id

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
          "dynamodb:Query"
        ]
        Resource = [
          var.deployment_metadata_table_arn,
          var.lock_table_arn,
          var.projects_table_arn,
          "${var.projects_table_arn}/index/*"
        ]
      }
    ]
  })
}

# Policy for STS AssumeRole (cross-account deployments)
resource "aws_iam_role_policy" "codebuild_sts" {
  name = "sts-assume-role"
  role = aws_iam_role.codebuild.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = "sts:AssumeRole"
        Resource = [
          # E34/T13b: renamed off the fork-origin prefix in lockstep with
          # modules/default_tenant's role name — one contract, two sides.
          "arn:aws:iam::*:role/agp-deployment-*",
          "arn:aws:iam::*:role/cdk-*"
        ]
      }
    ]
  })
}

# Policy for CloudFormation (stack operations)
resource "aws_iam_role_policy" "codebuild_cloudformation" {
  name = "cloudformation-access"
  role = aws_iam_role.codebuild.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "cloudformation:CreateStack",
          "cloudformation:UpdateStack",
          "cloudformation:DeleteStack",
          "cloudformation:DescribeStacks",
          "cloudformation:DescribeStackEvents",
          "cloudformation:GetTemplate",
          "cloudformation:ListStackResources",
          "cloudformation:CreateChangeSet",
          "cloudformation:DescribeChangeSet",
          "cloudformation:ExecuteChangeSet",
          "cloudformation:DeleteChangeSet",
          "cloudformation:ListStacks",
          "cloudformation:GetTemplateSummary"
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = "cloudformation:GetTemplate"
        Resource = "*"
      }
    ]
  })
}

# Policy for IaC resource provisioning (Bedrock, ECR, IAM, Lambda, etc.)
resource "aws_iam_role_policy" "codebuild_iac_provisioning" {
  name = "iac-provisioning"
  role = aws_iam_role.codebuild.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "bedrock:*",
          "bedrock-agentcore:*",
          # E32: Registry moved to its own `agent-registry` namespace (2026-08-06), whose
          # signingName is `agent-registry` — so `bedrock-agentcore:*` above no longer
          # authorizes registry calls. buildspec.yml reads and writes the agent's registry
          # record (get-registry-record / update-registry-record) with this role, so
          # without this grant the build AccessDenies mid-deploy and leaves the runtime
          # ARN unrecorded. ADD, not replace: runtime/identity/gateway stay above.
          "agent-registry:*",
          "ecr:*",
          "ecs:*",
          "ec2:*",
          "elasticloadbalancing:*",
          "lambda:*",
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
          "iam:ListPolicyVersions",
          "iam:CreatePolicyVersion",
          "iam:DeletePolicyVersion",
          "iam:CreateInstanceProfile",
          "iam:DeleteInstanceProfile",
          "iam:AddRoleToInstanceProfile",
          "iam:RemoveRoleFromInstanceProfile",
          "iam:GetInstanceProfile",
          "iam:TagInstanceProfile",
          "iam:UntagInstanceProfile",
          "iam:TagRole",
          "iam:UntagRole",
          "iam:TagPolicy",
          "iam:UntagPolicy",
          "iam:ListInstanceProfilesForRole",
          "iam:CreateServiceLinkedRole",
          "s3:*",
          "dynamodb:*",
          "logs:*",
          "states:*",
          "events:*",
          "sqs:*",
          "sns:*",
          "ssm:GetParameter",
          "ssm:GetParameters",
          "ssm:GetParametersByPath",
          "ssm:PutParameter",
          "ssm:DeleteParameter",
          "ssm:DeleteParameters",
          "ssm:DescribeParameters",
          "ssm:AddTagsToResource",
          "ssm:RemoveTagsFromResource",
          "ssm:ListTagsForResource",
          # Port-forwarding to RDS via bastion (market-surveillance seed step).
          # StartSession on AWS-StartPortForwardingSessionToRemoteHost requires
          # these three actions; without them, deploy.sh silently skips seeding.
          "ssm:StartSession",
          "ssm:TerminateSession",
          "ssm:DescribeSessions",
          "sts:GetCallerIdentity",
          "appsync:*",
          "amplify:*",
          "cloudfront:*",
          "apigateway:*",
          "execute-api:*",
          "acm:*",
          "route53:*",
          "wafv2:*",
          "secretsmanager:*",
          "kms:*",
          "elasticache:*",
          "rds:*",
          "rds-db:*",
          "elasticfilesystem:*",
          "servicediscovery:*",
          "xray:*",
          "autoscaling:*",
          "cloudwatch:*",
          "application-signals:*",
          "cloudtrail:*"
        ]
        Resource = "*"
      }
    ]
  })
}

# Policy for CodeCommit access (read-only for pulling source code)
resource "aws_iam_role_policy" "codebuild_codecommit" {
  name = "codecommit-access"
  role = aws_iam_role.codebuild.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "codecommit:GetBranch",
          "codecommit:GetCommit",
          "codecommit:GetRepository",
          "codecommit:ListBranches",
          "codecommit:ListRepositories",
          "codecommit:GitPull",
          "codecommit:GetUploadArchiveStatus",
          "codecommit:UploadArchive",
          "codecommit:CancelUploadArchive"
        ]
        Resource = "*"
      }
    ]
  })
}

# ============================================================================
# Buildspec staging in S3 (E28/T1)
# ============================================================================
# The buildspec used to be inlined via `file()`, which put it under CodeBuild's
# HARD 25600-char cap on an inline buildspec — it had reached 25342. Breaching
# that cap fails at UpdateProject during a live `terraform apply`, which no
# offline check can catch. Staging the buildspec in S3 removes the cap entirely.
#
# Delivered as a ZIP through an `S3` source (not via the `buildspec = "arn:aws:s3:::..."`
# form): the project's source was `NO_SOURCE`, and AWS documents that "if you use
# NO_SOURCE, the buildspec cannot be a file" — it must be an inline string. So the
# supported way to reach a buildspec FILE is an actual source. CodeBuild then reads
# `buildspec.yml` from the source root, which is exactly what this zip contains.
# Safe here because the buildspec never uses CODEBUILD_SRC_DIR — it cd's to
# absolute /tmp/workspace — so gaining a source directory changes no behaviour.
#
# etag = archive md5 → a buildspec edit re-uploads on the next `terraform apply`
# (mirrors runtime_module.tf). Carries the same E20/E21 gotcha: after editing
# buildspec.yml, run a root `terraform apply` BEFORE the next push, or the
# pipeline silently runs the PREVIOUS buildspec.
data "archive_file" "buildspec" {
  type        = "zip"
  output_path = "${path.module}/.build/buildspec.zip"

  source {
    content  = file("${path.module}/buildspec.yml")
    filename = "buildspec.yml"
  }
}

resource "aws_s3_object" "buildspec" {
  bucket = var.state_bucket
  key    = "codebuild-buildspec/buildspec.zip"
  source = data.archive_file.buildspec.output_path
  etag   = data.archive_file.buildspec.output_md5
  tags   = var.tags
}

# ============================================================================
# CodeBuild Project
# ============================================================================

resource "aws_codebuild_project" "deployment" {
  name                   = "${var.name_prefix}-deployment"
  description            = "Executes IaC commands for platform deployments"
  service_role           = aws_iam_role.codebuild.arn
  build_timeout          = 60
  concurrent_build_limit = 10

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    compute_type                = var.compute_type
    image                       = "aws/codebuild/amazonlinux2-aarch64-standard:3.0"
    type                        = "ARM_CONTAINER"
    image_pull_credentials_type = "CODEBUILD"
    privileged_mode             = true

    environment_variable {
      name  = "ENVIRONMENT"
      value = var.environment
    }

    environment_variable {
      name  = "LOCK_TABLE"
      value = "${var.name_prefix}-tf-lock"
    }

    # --- Epic 21: agentcore_runtime branch statics (EventBridge supplies only
    # IAC_TYPE/IMAGE_TAG/ECR_REPO/ARCHIVE_*; everything else must be project-level).
    environment_variable {
      name  = "AWS_TARGET_REGION"
      value = var.aws_region
    }

    environment_variable {
      name  = "STATE_BUCKET"
      value = var.state_bucket
    }

    environment_variable {
      name  = "AWS_ACCOUNT_ID"
      value = var.aws_account_id
    }

    # AGENT_REGISTRY_ID IS DECLARED HERE BUT ITS VALUE COMES FROM THE BACKEND, PER BUILD.
    #
    # The buildspec's `agentcore_runtime` branch calls `agent-registry-control
    # get-registry-record` / `update-registry-record`, and both take a `RegistryIdentifier` —
    # an ARN or a generated id, never a name. So unlike ECS (which now gets only the registry
    # NAME and resolves it in-process), CodeBuild genuinely needs an ID and has no backend to
    # ask for one.
    #
    # Terraform cannot supply it: AWS mints registry ids, there is no Terraform resource for
    # the `agent-registry` namespace, and the script-backed module's id could only be read from
    # a capture file during the PLAN walk — before the provisioner that writes it had run. That
    # is precisely what used to make a from-zero deploy need TWO applies.
    #
    # So the value arrives as a per-build `environmentVariablesOverride` from
    # `RuntimeBuildService.start_runtime_build`, which is the ONLY thing that starts this
    # project (the EventBridge trigger was deleted in E22/T7) and which holds an
    # AgentRegistryService that has already resolved the id by name. An override takes
    # precedence over this declaration, so the build always reads the registry the control
    # plane is actually using — not a value frozen at apply time, which could go stale.
    #
    # The declaration is kept (empty) on purpose, for two reasons: `aws_codebuild_project`
    # requires a `value` for every `environment_variable`, and declaring the name documents the
    # build contract in the project itself.
    #
    # WHAT HAPPENS IF A BUILD IS NOT OVERRIDDEN — and why it takes a buildspec guard to be safe.
    # It is tempting to assume `get-registry-record` "fails loudly" on an empty `--registry-id`.
    # It does not: the buildspec has NO `set -e`, so that call fails NON-FATALLY, every `jq` read
    # of its empty output yields "" at rc 0, and `terraform apply` runs anyway — provisioning a
    # LIVE AgentCore runtime that the later write-back guard can only report as UNTRACKED, after
    # the fact. So the buildspec's `agentcore_runtime` branch opens with an explicit emptiness
    # guard (a bare `exit 1` before any AWS or Terraform call), which is what actually makes an
    # un-overridden build fail having provisioned nothing. `tests/test_buildspec_contract.py`
    # pins that guard, including its position ahead of the first registry call and the apply.
    # A platform-started build always carries the override, and `retry-build` replays it — the
    # guard exists for hand-run `aws codebuild start-build` invocations, which is the one path
    # that can reach this branch without one.
    environment_variable {
      name  = "AGENT_REGISTRY_ID"
      value = ""
    }

    environment_variable {
      name  = "ENTRA_TENANT_ID"
      value = var.entra_tenant_id
    }

    environment_variable {
      name  = "PROJECTS_TABLE_NAME"
      value = var.projects_table_name
    }

    # E26/T10: Langfuse host for the agentcore_runtime branch → runtime tfvars (LANGFUSE_HOST).
    # NON-SECRET (a URL). The per-agent key secret NAME comes from the agent envelope in-branch;
    # the {public_key,secret_key} VALUE is read by the container from Secrets Manager at runtime.
    environment_variable {
      name  = "LANGFUSE_HOST"
      value = var.langfuse_host
    }
  }

  # E28/T1: buildspec comes from S3, not inline — see the staging block above.
  # No `buildspec` attribute: CodeBuild reads buildspec.yml from the source root.
  source {
    type     = "S3"
    location = "${var.state_bucket}/${aws_s3_object.buildspec.key}"
  }


  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild.name
      status     = "ENABLED"
    }
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-deployment"
  })
}