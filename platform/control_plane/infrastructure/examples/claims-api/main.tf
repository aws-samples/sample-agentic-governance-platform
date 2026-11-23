# Claims API demo fixture — a small REST API the AgentCore Gateway template
# fronts in the demo. Standalone root with its own local state; region and
# account come from ambient credentials. See README.md.

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.66, < 7.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.6"
    }
  }
}

provider "aws" {
  default_tags {
    tags = var.tags
  }
}

# ============================================================================
# API key (stored in Secrets Manager as {"api_key": "..."})
# ============================================================================

resource "random_password" "api_key" {
  length  = 40
  special = false
}

# Named inside the MCP server's reserved namespace: the gateway module grants (and accepts)
# only secrets under agp-mcp/<mcp_name>/ (E40 security S1).
resource "aws_secretsmanager_secret" "api_key" {
  name                    = "agp-mcp/${var.mcp_name}/api-key"
  description             = "API key for the ${var.name} demo fixture"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "api_key" {
  secret_id     = aws_secretsmanager_secret.api_key.id
  secret_string = jsonencode({ api_key = random_password.api_key.result })
}

# ============================================================================
# Lambda (serves the 3 operations and is the REQUEST authorizer)
# ============================================================================

data "archive_file" "lambda" {
  type        = "zip"
  source_file = "${path.module}/lambda/handler.py"
  output_path = "${path.module}/.build/handler.zip"
}

resource "aws_iam_role" "lambda" {
  name = "agp-${var.name}-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "lambda_secret" {
  name = "read-api-key"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.api_key.arn
    }]
  })
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = 14
}

resource "aws_lambda_function" "this" {
  function_name    = var.name
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 10

  environment {
    variables = {
      SECRET_ARN = aws_secretsmanager_secret.api_key.arn
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy_attachment.lambda_logs]
}

# Only this API may invoke the function (methods and the authorizer).
resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowClaimsApiInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.this.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.this.execution_arn}/*"
}

# ============================================================================
# REST API
# ============================================================================

resource "aws_api_gateway_rest_api" "this" {
  name        = var.name
  description = "Claims API demo fixture (fronted by an AgentCore Gateway)"
  # A Regional API defaults to TLS_1_0 and rejects "TLS_1_2"; enhanced
  # policies enforce TLS 1.2+ and need an endpoint access mode.
  security_policy      = "SecurityPolicy_TLS13_1_2_2021_06"
  endpoint_access_mode = "STRICT"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

# Every method carries an authorizer (not just an API key) so the API is not
# an unauthenticated public endpoint.
resource "aws_api_gateway_authorizer" "api_key" {
  name                             = "x-api-key"
  rest_api_id                      = aws_api_gateway_rest_api.this.id
  type                             = "REQUEST"
  authorizer_uri                   = aws_lambda_function.this.invoke_arn
  identity_source                  = "method.request.header.x-api-key"
  authorizer_result_ttl_in_seconds = 0
}

resource "aws_api_gateway_resource" "policies" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_rest_api.this.root_resource_id
  path_part   = "policies"
}

resource "aws_api_gateway_resource" "policy" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_resource.policies.id
  path_part   = "{policyId}"
}

resource "aws_api_gateway_resource" "claims" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_rest_api.this.root_resource_id
  path_part   = "claims"
}

resource "aws_api_gateway_resource" "claim" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_resource.claims.id
  path_part   = "{claimId}"
}

locals {
  # operationId => method wiring
  operations = {
    getPolicy      = { resource_id = aws_api_gateway_resource.policy.id, method = "GET" }
    openClaim      = { resource_id = aws_api_gateway_resource.claims.id, method = "POST" }
    getClaimStatus = { resource_id = aws_api_gateway_resource.claim.id, method = "GET" }
  }
}

resource "aws_api_gateway_method" "op" {
  for_each = local.operations

  rest_api_id      = aws_api_gateway_rest_api.this.id
  resource_id      = each.value.resource_id
  http_method      = each.value.method
  authorization    = "CUSTOM"
  authorizer_id    = aws_api_gateway_authorizer.api_key.id
  api_key_required = true
  operation_name   = each.key
}

resource "aws_api_gateway_integration" "op" {
  for_each = local.operations

  rest_api_id             = aws_api_gateway_rest_api.this.id
  resource_id             = each.value.resource_id
  http_method             = aws_api_gateway_method.op[each.key].http_method
  type                    = "AWS_PROXY"
  integration_http_method = "POST"
  uri                     = aws_lambda_function.this.invoke_arn
}

resource "aws_api_gateway_deployment" "this" {
  rest_api_id = aws_api_gateway_rest_api.this.id

  triggers = {
    redeploy = sha1(jsonencode([
      aws_api_gateway_authorizer.api_key,
      aws_api_gateway_method.op,
      aws_api_gateway_integration.op,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "this" {
  rest_api_id   = aws_api_gateway_rest_api.this.id
  deployment_id = aws_api_gateway_deployment.this.id
  stage_name    = var.stage_name
}

# ============================================================================
# Usage plan (API Gateway's own API-key check, same key value as the secret)
# ============================================================================

resource "aws_api_gateway_api_key" "this" {
  name  = var.name
  value = random_password.api_key.result
}

resource "aws_api_gateway_usage_plan" "this" {
  name = var.name

  api_stages {
    api_id = aws_api_gateway_rest_api.this.id
    stage  = aws_api_gateway_stage.this.stage_name
  }

  throttle_settings {
    rate_limit  = 10
    burst_limit = 20
  }
}

resource "aws_api_gateway_usage_plan_key" "this" {
  key_id        = aws_api_gateway_api_key.this.id
  key_type      = "API_KEY"
  usage_plan_id = aws_api_gateway_usage_plan.this.id
}
