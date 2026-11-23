output "harness_arn" {
  description = "ARN of the AgentCore harness. The config-build equivalent of the runtime module's agent_runtime_arn — the buildspec records it against the agent+stage."
  value       = aws_bedrockagentcore_harness.this.arn
}

output "harness_id" {
  description = "Id of the AgentCore harness."
  value       = aws_bedrockagentcore_harness.this.harness_id
}

output "harness_name" {
  description = "Derived stage-scoped harness name ({agent_name}-{stage}). Exposed because it is account-global — an operator diagnosing a name conflict needs to read it without re-deriving it."
  value       = aws_bedrockagentcore_harness.this.harness_name
}

output "exec_role_arn" {
  description = "ARN of the exec role the harness assumes: the module-created role, or the exec_role_arn override when one was supplied. Mirrors the harness's own execution_role_arn, so it reports what is actually in effect."
  value       = aws_bedrockagentcore_harness.this.execution_role_arn
}

output "state_key" {
  # The convention lives in exactly one place in code (main.tf's local) and is
  # echoed here so a deploy can be traced to its state object without a human
  # re-deriving the pattern from the design doc. This module does NOT configure its
  # own backend — the backend/buildspec does, from the same convention — so this
  # output is the cross-check that the two agree.
  description = "The documented Terraform state key for this harness: agentcore-harness/{agent_id}/{stage}/terraform.tfstate. Informational — the caller configures the backend."
  value       = local.state_key
}
