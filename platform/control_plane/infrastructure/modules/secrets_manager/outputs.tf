output "graph_client_secret_arn" {
  description = "ARN of the Graph client secret. The ECS task role needs secretsmanager:GetSecretValue on this ARN."
  value       = aws_secretsmanager_secret.graph_client_secret.arn
}

output "graph_client_secret_name" {
  description = "Full name of the secret (e.g. agp/graph-client-secret)."
  value       = aws_secretsmanager_secret.graph_client_secret.name
}
