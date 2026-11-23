variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
}

variable "environment" {
  description = "Environment name"
  type        = string
}

variable "project_archive_retention_days" {
  description = "Number of days to retain project archives"
  type        = number
  default     = 7
}

variable "tags" {
  description = "Common tags"
  type        = map(string)
  default     = {}
}

variable "ephemeral" {
  description = "Allow terraform destroy to empty and delete the buckets"
  type        = bool
  default     = false
}
