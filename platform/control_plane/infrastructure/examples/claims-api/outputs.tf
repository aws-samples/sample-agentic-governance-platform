output "invoke_url" {
  description = "Base URL of the API stage; paste into openapi.yaml servers[0].url"
  value       = aws_api_gateway_stage.this.invoke_url
}

output "api_key_secret_arn" {
  description = "Secrets Manager secret holding {\"api_key\": ...}; paste into gateway.yaml api_key.secret_arn"
  value       = aws_secretsmanager_secret.api_key.arn
}
