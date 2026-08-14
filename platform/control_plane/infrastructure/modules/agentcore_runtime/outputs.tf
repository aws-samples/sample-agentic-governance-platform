output "agent_runtime_arn" {
  description = "ARN of the AgentCore runtime."
  value       = aws_bedrockagentcore_agent_runtime.this.agent_runtime_arn
}

output "agent_runtime_id" {
  description = "Id of the AgentCore runtime."
  value       = aws_bedrockagentcore_agent_runtime.this.agent_runtime_id
}
