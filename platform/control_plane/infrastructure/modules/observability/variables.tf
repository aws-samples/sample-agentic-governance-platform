variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
}

variable "environment" {
  description = "Environment name"
  type        = string
}

variable "ecs_cluster_name" {
  description = "ECS cluster name"
  type        = string
}

variable "ecs_service_name" {
  description = "ECS service name"
  type        = string
}

variable "api_gateway_id" {
  description = "API Gateway ID"
  type        = string
}

variable "api_gateway_name" {
  description = "API Gateway name"
  type        = string
}

variable "tags" {
  description = "Common tags"
  type        = map(string)
  default     = {}
}

# CloudWatch Transaction Search (E42) — see the Transaction Search section in main.tf.
variable "enable_transaction_search" {
  description = "Enable CloudWatch Transaction Search (X-Ray trace segments -> CloudWatch Logs `aws/spans`) for the account in this region. Account-wide per region; billed as CloudWatch Logs ingestion. See the root variable of the same name."
  type        = bool
  default     = true
}

variable "transaction_search_indexing_percentage" {
  description = "Percentage of ingested spans X-Ray INDEXES for Transaction Search queries (the `Default` indexing rule). Ingestion into `aws/spans` is always 100%; this only sets how many are searchable. 1% is AWS's default and its free tier."
  type        = number
  default     = 1
}
