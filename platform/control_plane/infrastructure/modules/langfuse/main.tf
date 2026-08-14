# Data sources
data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_region" "current" {}

data "aws_caller_identity" "current" {}

data "aws_vpc" "vpc" {
  id = var.vpc_id
}

# Local variables
locals {
  tag_name      = var.domain != null ? "${var.name}-${var.environment} (${var.domain})" : "${var.name}-${var.environment}"
  azs           = slice(data.aws_availability_zones.available.names, 0, 3)
  bucket_prefix = var.domain != null ? replace(var.domain, ".", "-") : var.name

  common_tags = {
    Environment = var.environment
    Project     = var.name
    ManagedBy   = "terraform"
  }
}

# Service-linked roles (required for RDS, ElastiCache, Lambda@Edge replication)
#
# Idempotent: an SLR that already exists makes CreateServiceLinkedRole fail with
# InvalidInput / "Service role name AWSServiceRoleForX has been taken in this
# account" — that is the common case (any account that ever used RDS) and is
# tolerated. EVERY OTHER failure aborts the apply, notably AccessDenied when the
# apply principal lacks iam:CreateServiceLinkedRole: previously `|| true`
# swallowed that and the apply died ~10 minutes later inside Aurora/ElastiCache
# creation with an opaque service error instead of failing here.
resource "null_resource" "service_linked_roles" {
  provisioner "local-exec" {
    command = <<-EOT
      # `set -e` is what makes an ensure_slr failure fail the APPLY (its `aws` call
      # sits inside an `if` condition where -e is suppressed, so the explicit
      # `return 1` below propagates to the bare call sites and -e stops the script).
      # No `-o pipefail`: there are no pipes, and it's a bashism — local-exec runs
      # /bin/sh, which is dash on Debian/Ubuntu CI and rejects it at line 1.
      set -eu

      ensure_slr() {
        service="$1"
        if err=$(aws iam create-service-linked-role --aws-service-name "$service" 2>&1); then
          echo "Created service-linked role for $service"
          return 0
        fi
        case "$err" in
          *"has been taken"*|*AlreadyExists*|*"already exists"*)
            echo "Service-linked role for $service already exists"
            return 0
            ;;
        esac
        echo "ERROR: could not ensure the service-linked role for $service." >&2
        echo "The apply principal most likely lacks iam:CreateServiceLinkedRole." >&2
        echo "$err" >&2
        return 1
      }

      ensure_slr rds.amazonaws.com
      ensure_slr elasticache.amazonaws.com
      ensure_slr replicator.lambda.amazonaws.com

      echo "Service-linked roles ensured"
      # Give IAM a moment to propagate before Aurora/ElastiCache reference the roles.
      sleep 10
    EOT
  }
}

# Ensure SLRs are created before dependent resources
# RDS, ElastiCache, and Lambda@Edge depend on these

# Random passwords and secrets
resource "random_bytes" "salt" {
  length = 32
}

resource "random_bytes" "nextauth_secret" {
  length = 32
}

resource "random_bytes" "encryption_key" {
  count  = var.use_encryption_key ? 1 : 0
  length = 32
}

resource "random_password" "postgres_password" {
  length      = 64
  special     = false
  min_lower   = 1
  min_upper   = 1
  min_numeric = 1
}

resource "random_password" "redis_password" {
  length      = 64
  special     = false
  min_lower   = 1
  min_upper   = 1
  min_numeric = 1
}

resource "random_password" "clickhouse_password" {
  length      = 64
  special     = false
  min_lower   = 1
  min_upper   = 1
  min_numeric = 1
}

# Secrets Manager
resource "aws_secretsmanager_secret" "langfuse" {
  name                    = "${var.name}-secrets"
  recovery_window_in_days = 0
  tags = {
    Name = "${local.tag_name} Secrets"
  }
}

resource "aws_secretsmanager_secret_version" "langfuse" {
  secret_id = aws_secretsmanager_secret.langfuse.id
  secret_string = jsonencode({
    postgres_password   = random_password.postgres_password.result
    redis_password      = random_password.redis_password.result
    clickhouse_password = random_password.clickhouse_password.result
    salt                = random_bytes.salt.base64
    nextauth_secret     = random_bytes.nextauth_secret.base64
    encryption_key      = var.use_encryption_key ? random_bytes.encryption_key[0].hex : ""
  })
}

# -----------------------------------------------------------------------------
# Cognito OIDC client for Langfuse SSO (optional — only when cognito_user_pool_id is set)
# -----------------------------------------------------------------------------

locals {
  enable_sso = var.cognito_user_pool_id != ""
}

resource "aws_cognito_user_pool_client" "langfuse" {
  count = local.enable_sso ? 1 : 0

  name            = "${var.name}-langfuse-sso"
  user_pool_id    = var.cognito_user_pool_id
  generate_secret = true

  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]

  callback_urls = [
    "https://${aws_cloudfront_distribution.langfuse.domain_name}/api/auth/callback/custom"
  ]

  logout_urls = [
    "https://${aws_cloudfront_distribution.langfuse.domain_name}"
  ]

  explicit_auth_flows = [
    "ALLOW_REFRESH_TOKEN_AUTH",
    "ALLOW_USER_SRP_AUTH"
  ]
}
