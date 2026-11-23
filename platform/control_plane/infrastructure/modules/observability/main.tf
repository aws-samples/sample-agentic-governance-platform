# ============================================================================
# CloudWatch Dashboard
# ============================================================================

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "${var.name_prefix}-dashboard"

  dashboard_body = jsonencode({
    widgets = [
      # ECS Service Metrics
      {
        type = "metric"
        properties = {
          metrics = [
            ["AWS/ECS", "CPUUtilization", { stat = "Average" }],
            [".", "MemoryUtilization", { stat = "Average" }]
          ]
          period = 300
          stat   = "Average"
          region = data.aws_region.current.region
          title  = "ECS Service - CPU & Memory"
          dimensions = {
            ServiceName = var.ecs_service_name
            ClusterName = var.ecs_cluster_name
          }
        }
      },

      # API Gateway Metrics
      {
        type = "metric"
        properties = {
          metrics = [
            ["AWS/ApiGateway", "Count", { stat = "Sum" }],
            [".", "4XXError", { stat = "Sum" }],
            [".", "5XXError", { stat = "Sum" }]
          ]
          period = 300
          stat   = "Sum"
          region = data.aws_region.current.region
          title  = "API Gateway - Requests & Errors"
          dimensions = {
            ApiId = var.api_gateway_id
          }
        }
      }
    ]
  })
}

# ============================================================================
# CloudWatch Alarms - ECS
# ============================================================================

resource "aws_cloudwatch_metric_alarm" "ecs_cpu_high" {
  alarm_name          = "${var.name_prefix}-ecs-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "CPUUtilization"
  namespace           = "AWS/ECS"
  period              = 300
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "ECS CPU utilization is too high"
  alarm_actions       = []

  dimensions = {
    ServiceName = var.ecs_service_name
    ClusterName = var.ecs_cluster_name
  }

  tags = var.tags
}

resource "aws_cloudwatch_metric_alarm" "ecs_memory_high" {
  alarm_name          = "${var.name_prefix}-ecs-memory-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "MemoryUtilization"
  namespace           = "AWS/ECS"
  period              = 300
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "ECS Memory utilization is too high"
  alarm_actions       = []

  dimensions = {
    ServiceName = var.ecs_service_name
    ClusterName = var.ecs_cluster_name
  }

  tags = var.tags
}

# ============================================================================
# CloudWatch Alarms - API Gateway
# ============================================================================

resource "aws_cloudwatch_metric_alarm" "api_5xx_errors" {
  alarm_name          = "${var.name_prefix}-api-5xx-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "5XXError"
  namespace           = "AWS/ApiGateway"
  period              = 300
  statistic           = "Sum"
  threshold           = 10
  alarm_description   = "API Gateway 5XX errors are too high"
  alarm_actions       = []
  treat_missing_data  = "notBreaching"

  dimensions = {
    ApiId = var.api_gateway_id
  }

  tags = var.tags
}

resource "aws_cloudwatch_metric_alarm" "api_latency_high" {
  alarm_name          = "${var.name_prefix}-api-latency-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "Latency"
  namespace           = "AWS/ApiGateway"
  period              = 300
  statistic           = "Average"
  threshold           = 5000
  alarm_description   = "API Gateway latency is too high (>5s)"
  alarm_actions       = []
  treat_missing_data  = "notBreaching"

  dimensions = {
    ApiId = var.api_gateway_id
  }

  tags = var.tags
}

# ============================================================================
# CloudWatch Log Insights Queries
# ============================================================================

resource "aws_cloudwatch_query_definition" "api_errors" {
  name = "${var.name_prefix}/api-errors"

  log_group_names = [
    "/aws/apigateway/${var.name_prefix}"
  ]

  query_string = <<-QUERY
    fields @timestamp, @message, status, httpMethod, routeKey
    | filter status >= 400
    | sort @timestamp desc
    | limit 100
  QUERY
}

resource "aws_cloudwatch_query_definition" "ecs_errors" {
  name = "${var.name_prefix}/ecs-errors"

  log_group_names = [
    "/ecs/${var.name_prefix}"
  ]

  query_string = <<-QUERY
    fields @timestamp, @message
    | filter @message like /ERROR/ or @message like /Exception/
    | sort @timestamp desc
    | limit 100
  QUERY
}

# ============================================================================
# Data Sources
# ============================================================================

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# ============================================================================
# CloudWatch Transaction Search (E42 evaluations / Ops Observability traces)
# ============================================================================
# Every agent runtime the platform deploys exports its spans to X-Ray (harness agents
# natively, container agents via SigV4 OTLP). X-Ray only writes those spans to the
# CloudWatch Logs group `aws/spans` — which is what the Ops Observability traces list and
# the E42 eval judges read — once the account's trace-segment destination is switched from
# `XRay` to `CloudWatchLogs`, i.e. once Transaction Search is on. It is OFF in a fresh
# account, nothing else in this stack turned it on, and the symptom is silent: runtimes
# deploy, invocations succeed, the traces list stays empty and every judge scores nothing
# (the last "Known gaps" row in docs/superpowers/specs/2026-10-05-epic-42-evaluations-design.md).
#
# IT IS AN ACCOUNT-WIDE SETTING (per region), not a resource this stack owns outright, so:
#   - `count` on the root toggle rather than a hard dependency — an operator who manages the
#     setting elsewhere (or must not pay for it) sets `enable_transaction_search = false`.
#   - Both resources are NO-OP ON DELETE in the provider (it never calls
#     UpdateTraceSegmentDestination back to `XRay`), so `terraform destroy`, or flipping the
#     toggle to false later, leaves the setting exactly as it is. Turning it off is a deliberate
#     CLI act: `aws xray update-trace-segment-destination --destination XRay`.
#   - Create is idempotent: the provider tolerates "already set" and waits for Status ACTIVE,
#     so a region where an operator enabled it by hand adopts cleanly on the first apply.
#   - The apply principal (the OPERATOR's credentials) needs xray:GetTraceSegmentDestination,
#     xray:UpdateTraceSegmentDestination, xray:GetIndexingRules, xray:UpdateIndexingRule, and for
#     the resource policy below logs:PutResourcePolicy, logs:DescribeResourcePolicies,
#     logs:DeleteResourcePolicy. The backend ECS task role needs nothing new; the backend only
#     ever reads `aws/spans`.
#   - The `aws/spans` log group itself is NOT managed here: AWS creates it on first delivery
#     (30-day retention). Declaring it would race that creation for no gain.
#
# COST. Span ingestion into `aws/spans` is billed as CloudWatch Logs ingestion (every span, not
# a sample). The indexing rule below is the second knob: it is the share of spans X-Ray indexes
# for Transaction Search QUERIES, 1% at no charge, more billed per indexed span. It does not
# change what lands in `aws/spans`, so the platform reads the same data at any percentage.
#
# Verify after apply (expect Destination CloudWatchLogs, Status ACTIVE):
#   aws xray get-trace-segment-destination
# and, after the first agent invocation, that the group exists:
#   aws logs describe-log-groups --log-group-name-prefix aws/spans

# X-Ray can only WRITE to `aws/spans` once a CloudWatch Logs RESOURCE policy admits it. The
# X-Ray resource below calls UpdateTraceSegmentDestination and nothing else; AWS's own CLI/SDK
# procedure puts this policy FIRST, and the console creates it silently — which is why the dev
# account never noticed (it was enabled from the console). Without it the destination reads
# CloudWatchLogs / ACTIVE and not one span arrives.
#
# `policy_name` is the console's own name on purpose: PutResourcePolicy is an UPSERT by name,
# so an account that already carries the console-created policy adopts it instead of ending
# up with two. The document is the console's, verbatim (the Application Signals group is part
# of what the console grants; harmless here).
#
# UNLIKE THE TWO X-RAY RESOURCES, THIS ONE IS DELETED on destroy / toggle-off. The destination
# then stays CloudWatchLogs while the policy is gone, and spans silently stop. Turning
# Transaction Search off is therefore TWO acts — flip the toggle AND run
# `aws xray update-trace-segment-destination --destination XRay` — and so is turning it back on.
resource "aws_cloudwatch_log_resource_policy" "transaction_search" {
  count = var.enable_transaction_search ? 1 : 0

  policy_name = "TransactionSearchXRayAccess"
  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "TransactionSearchXRayAccess"
        Effect = "Allow"
        Principal = {
          Service = "xray.amazonaws.com"
        }
        Action = "logs:PutLogEvents"
        Resource = [
          "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:aws/spans:*",
          "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/application-signals/data:*",
        ]
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = data.aws_caller_identity.current.account_id
          }
          ArnLike = {
            "aws:SourceArn" = "arn:aws:xray:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
          }
        }
      }
    ]
  })
}

resource "aws_xray_trace_segment_destination" "transaction_search" {
  count = var.enable_transaction_search ? 1 : 0

  destination = "CloudWatchLogs"

  # The policy must exist before X-Ray starts delivering, or the first spans are dropped.
  depends_on = [aws_cloudwatch_log_resource_policy.transaction_search]
}

# `Default` is the one indexing rule AWS creates with Transaction Search; there is no Create
# API for rules, only Update, so this resource adopts it (import id is the name) rather than
# creating a second one. depends_on: the rule is only addressable once the destination is on.
resource "aws_xray_indexing_rule" "transaction_search" {
  count = var.enable_transaction_search ? 1 : 0

  name = "Default"

  rule {
    probabilistic {
      desired_sampling_percentage = var.transaction_search_indexing_percentage
    }
  }

  depends_on = [aws_xray_trace_segment_destination.transaction_search]
}
