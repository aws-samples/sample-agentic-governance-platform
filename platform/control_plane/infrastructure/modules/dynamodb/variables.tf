variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
}

variable "environment" {
  description = "Environment name"
  type        = string
}

variable "tags" {
  description = "Common tags"
  type        = map(string)
  default     = {}
}

variable "ephemeral" {
  description = "Turn off deletion protection on the containment and audit tables so terraform destroy can remove them"
  type        = bool
  default     = false
}
