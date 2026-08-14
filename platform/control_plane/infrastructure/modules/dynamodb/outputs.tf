output "app_factory_table_name" {
  description = "Name of App Factory DynamoDB table"
  value       = aws_dynamodb_table.app_factory.name
}

output "app_factory_table_arn" {
  description = "ARN of App Factory DynamoDB table"
  value       = aws_dynamodb_table.app_factory.arn
}

output "application_catalog_table_name" {
  description = "Name of Application Catalog DynamoDB table"
  value       = aws_dynamodb_table.application_catalog.name
}

output "application_catalog_table_arn" {
  description = "ARN of Application Catalog DynamoDB table"
  value       = aws_dynamodb_table.application_catalog.arn
}

output "deployment_metadata_table_name" {
  description = "Name of Deployment Metadata DynamoDB table"
  value       = aws_dynamodb_table.deployment_metadata.name
}

output "deployment_metadata_table_arn" {
  description = "ARN of Deployment Metadata DynamoDB table"
  value       = aws_dynamodb_table.deployment_metadata.arn
}

output "guardrails_table_name" {
  description = "Name of the guardrails DynamoDB table"
  value       = aws_dynamodb_table.guardrails.name
}

output "guardrails_table_arn" {
  description = "ARN of the guardrails DynamoDB table"
  value       = aws_dynamodb_table.guardrails.arn
}

output "marketplace_table_name" {
  description = "Name of the marketplace DynamoDB table"
  value       = aws_dynamodb_table.marketplace.name
}

output "marketplace_table_arn" {
  description = "ARN of the marketplace DynamoDB table"
  value       = aws_dynamodb_table.marketplace.arn
}

output "connections_table_name" {
  description = "Name of the connections DynamoDB table"
  value       = aws_dynamodb_table.connections.name
}

output "connections_table_arn" {
  description = "ARN of the connections DynamoDB table"
  value       = aws_dynamodb_table.connections.arn
}

output "projects_table_name" {
  description = "Name of the projects DynamoDB table"
  value       = aws_dynamodb_table.projects.name
}

output "projects_table_arn" {
  description = "ARN of the projects DynamoDB table"
  value       = aws_dynamodb_table.projects.arn
}

output "tenants_table_name" {
  description = "Name of the tenants DynamoDB table"
  value       = aws_dynamodb_table.tenants.name
}

output "tenants_table_arn" {
  description = "ARN of the tenants DynamoDB table"
  value       = aws_dynamodb_table.tenants.arn
}
