variable "name_prefix" {
  description = "Prefix for the secret name. The full name will be <name_prefix>/graph-client-secret."
  type        = string
  default     = "agp"
}

variable "secret_value" {
  description = "The confidential client secret value generated in Entra (S1.3 step 3). Provided at apply time via -var or a tfvars file. Never commit this value."
  type        = string
  sensitive   = true
}

variable "tags" {
  description = "Tags applied to the secret. Project + component tags are added automatically."
  type        = map(string)
  default     = {}
}
