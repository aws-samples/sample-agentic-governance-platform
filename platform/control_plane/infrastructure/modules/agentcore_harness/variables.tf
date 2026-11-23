# ---------------------------------------------------------------------------
# Inputs for the platform-owned AgentCore Harness module (E30/T7).
#
# The variable surface is deliberately TINY compared with agentcore_runtime's:
# a harness carries no container, no ECR, no networking, so the only agent-
# specific knobs are the five keys of `harness.yaml` — and those arrive as a
# FILE PATH, not as variables. The developer edits `harness.yaml` in their agent
# repo; the platform passes the path to it. That is the whole point of the
# config-driven template (design §3).
#
# Platform-owned inputs (agent_id, agent_name, stage, region, deploy_role_arn,
# tenant_id, entra_app_audience, entra_app_id,
# tags) arrive as tfvars written by the CodeBuild `agentcore_harness` branch.
# ---------------------------------------------------------------------------

variable "agent_id" {
  # Not a resource-name component — the harness and the exec role are named from
  # agent_name + stage, mirroring the runtime module. agent_id exists because it
  # is the FIRST segment of the state key convention
  # (`agentcore-harness/{agent_id}/{stage}/terraform.tfstate`, design §3): the
  # backend derives that key when it configures the backend for this root module,
  # and the `state_key` output echoes it so a deploy can be traced to its state
  # object without re-deriving the convention by hand.
  description = "AGP agent id (the registry's id, e.g. agent-abc123). Names no AWS resource; it is the first segment of the state key convention — see the state_key output and README."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._-]*$", var.agent_id))
    error_message = "agent_id must be non-empty and contain only letters, digits, dots, hyphens or underscores (it becomes a path segment in the Terraform state key)."
  }
}

variable "agent_name" {
  # Capped at 32 for the same reason as agentcore_runtime's (mirrors the backend's
  # AGENT_NAME_RE): the derived exec-role name is `{agent_name}-{stage}-harness-exec`
  # and IAM caps a role name at 64. Underscores are allowed here because the
  # runtime module's AgentCore name pattern forbids hyphens and both modules take
  # the SAME agent_name from the SAME registry record — a stricter-here/looser-there
  # split would mean one template accepting a name the other refuses.
  description = "Agent name — the STEM of the stage-scoped harness/role names, not a resource name itself (see main.tf locals). Must match [a-zA-Z][a-zA-Z0-9_]{0,31} (max 32 chars: the backend's AGENT_NAME_RE)."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,31}$", var.agent_name))
    error_message = "agent_name must match ^[a-zA-Z][a-zA-Z0-9_]{0,31}$ (start with a letter, then letters/digits/underscores, max 32 chars)."
  }
}

variable "stage" {
  # REQUIRED and deliberately WITHOUT a default — the same call, for the same
  # reason, as agentcore_runtime's `stage` (E28A/T1b): a caller that forgets to
  # thread the stage would otherwise name PROD's account-global resources `dev`
  # and either collide with dev or, worse, succeed and have prod traffic served by
  # a resource every operator reads as dev. With no default that fails at `plan`.
  # EVERY stage is suffixed, including dev — there is no magic default case.
  description = "Deployment stage (e.g. dev, prod). REQUIRED, no default — suffixes BOTH account-global names (harness name + exec role name). Free-form, matching the platform's end-to-end string treatment of stage."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z0-9_-]+$", var.stage))
    error_message = "stage must be non-empty and contain only letters, digits, hyphens or underscores."
  }
}

variable "region" {
  # Named `region` (not `aws_region`) because the brief pins that name and the
  # harness resource itself has a `region` attribute.
  #
  # THERE IS EXACTLY ONE REGION VARIABLE, and this is it (E30/T9). An earlier
  # version of this comment described an `aws_region` companion "below" that would
  # default to this value — that variable was never declared, so the note was a
  # dangling reference to nothing. It was not harmless: it read as an invitation to
  # pass `aws_region=`, which Terraform accepts as a WARNING ("Value for undeclared
  # variable") and then DISCARDS — the same silent-drop failure mode as E28A/T1b's
  # stale-module defect. The buildspec's agentcore_harness branch passes `region=`
  # only, and `versions.tf`'s provider reads `var.region`, so this one variable is
  # both the harness resource's region and the provider's.
  description = "AWS region the harness is created in. Passed to the harness resource's region attribute, and to the provider (versions.tf)."
  type        = string
}

variable "harness_config_path" {
  # The deploy artifact. Read with `yamldecode(file(...))` in main.tf, so it must
  # resolve on the APPLY HOST at plan time — the CodeBuild `agentcore_harness`
  # branch clones the agent repo alongside the infra repo and passes the path to
  # the cloned `harness.yaml` (design §3: "the config build needs the agent repo
  # where the container build needed the image").
  #
  # A path, NOT the file's contents: the five keys stay a single reviewable file in
  # the developer's repo instead of being flattened into tfvars, which is what makes
  # `git log harness.yaml` the audit trail for what an agent was told to do.
  description = "Filesystem path to the agent's harness.yaml (cloned agent repo). Read via yamldecode(file(...)); must exist on the apply host at plan time. Keys: model_id, system_prompt, tools, max_iterations, timeout_seconds."
  type        = string
}

# ---- Platform-owned ----

# Inbound Entra authorizer (E30A/T1) — the same three inputs, same shapes, as
# agentcore_runtime's. Without them the harness accepts only SigV4 and AGP cannot
# invoke it with the user's identity.
variable "tenant_id" {
  description = "Entra tenant id (GUID) for the OIDC discovery URL."
  type        = string
}

variable "entra_app_audience" {
  description = "Per-agent Entra app identifier URI (api://agp-agent-<id>) — an allowed aud."
  type        = string
}

variable "entra_app_id" {
  description = "Per-agent Entra app client GUID — the OTHER allowed aud form Entra may mint."
  type        = string
  default     = ""
}

# Outbound auth for governed gateway tools (E40/T10). Both written by the CodeBuild
# harness arm as tool_auth.auto.tfvars.json from GATEWAY_TOOL_AUTH / OBO_PROVIDER_ARN.
# Defaults keep today's shape: no map or no provider = no outbound_auth on any tool.
variable "gateway_tool_auth" {
  description = "Gateway ARN -> OAuth scope (api://agp-mcp-<id>/.default) for every gateway AGP governs. A harness.yaml tool whose ARN is a key gets TOKEN_EXCHANGE outbound auth; other ARNs are unchanged."
  type        = map(string)
  default     = {}
}

variable "obo_provider_arn" {
  description = "The agent's OBO (MicrosoftOauth2) credential provider ARN, used as the TOKEN_EXCHANGE provider. Empty = no tool gets outbound auth."
  type        = string
  default     = ""
}

variable "exec_role_arn" {
  description = "Optional execution role ARN override for the harness. If empty, the module creates its own in-account exec role; if set, that role is used instead (the created role stays unused)."
  type        = string
  default     = ""
}

variable "deploy_role_arn" {
  description = "Cross-account deploy role the provider assumes so harness resources land in the tenant account; empty = deploy-in-place (single-account)."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to the harness and its exec role."
  type        = map(string)
  default     = {}
}
