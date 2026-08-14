variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
}

variable "account_id" {
  description = "AWS account id (from data.aws_caller_identity at the root) — the replication destination and the base for the runtime-exec-role pull principal. Never a hardcoded literal."
  type        = string
}

variable "region" {
  description = "AWS region of this (source) ECR registry — from var.aws_region at the root."
  type        = string
}

variable "replication_destination_account_id" {
  description = "Destination account for cross-account ECR replication. Empty (default) = single-account, no replication. Never a hardcoded literal."
  type        = string
  default     = ""
}

variable "replication_destination_region" {
  description = "Destination region for cross-account ECR replication. Empty (default) falls back to var.region when a destination account is set."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Common tags"
  type        = map(string)
  default     = {}
}
