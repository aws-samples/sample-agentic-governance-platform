output "deploy_role_arn" {
  description = "ARN of the agp-deployment-* deploy-role CodeBuild assumes for the default tenant."
  value       = aws_iam_role.deploy.arn
}
