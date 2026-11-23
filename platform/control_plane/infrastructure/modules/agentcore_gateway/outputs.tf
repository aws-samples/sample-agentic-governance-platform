# The three the buildspec writes back onto the repo row (C6); the backend then adopts the
# gateway onto the MCP record.
output "gateway_arn" {
  description = "ARN of the AgentCore gateway."
  value       = aws_bedrockagentcore_gateway.this.gateway_arn
}

output "gateway_id" {
  description = "Short gateway id (the gatewayIdentifier the backend's UpdateGateway/GetGateway take)."
  value       = aws_bedrockagentcore_gateway.this.gateway_id
}

output "gateway_url" {
  description = "The gateway's MCP endpoint URL."
  value       = aws_bedrockagentcore_gateway.this.gateway_url
}

output "state_key" {
  description = "The documented Terraform state key: agentcore-gateway/{mcp_server_id}/{stage}/terraform.tfstate. Informational — the caller configures the backend."
  value       = local.state_key
}
