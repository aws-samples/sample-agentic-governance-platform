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
