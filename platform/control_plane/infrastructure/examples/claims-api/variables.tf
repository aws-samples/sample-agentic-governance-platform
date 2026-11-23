variable "name" {
  description = "Name prefix for every resource in this fixture"
  type        = string
  default     = "claims-api"
}

variable "mcp_name" {
  description = "MCP server name used in AGP Ops for the gateway repo. MUST equal it: the secret is named agp-mcp/<mcp_name>/api-key, the only namespace that gateway can read."
  type        = string
  default     = "claims-api"
}

variable "stage_name" {
  description = "API Gateway stage name (part of invoke_url)"
  type        = string
  default     = "v1"
}

variable "tags" {
  description = "Tags applied to every resource"
  type        = map(string)
  default = {
    Project = "agp-claims-api-demo"
  }
}
