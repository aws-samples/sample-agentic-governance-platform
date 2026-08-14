# ---------------------------------------------------------------------------
# Inputs for the platform-owned AgentCore Runtime module.
#
# The developer-set values (agent_name, model_id, log_level, guardrail_id,
# otel_enabled, lifecycle_idle_timeout_seconds) mirror agent.config.json
# (Task 1). The remaining inputs are platform-owned: they arrive as tfvars
# written by the CodeBuild branch (Task 5). This module never creates the
# execution role, subnets, or security groups — they are inputs.
# ---------------------------------------------------------------------------

# ---- Developer-set (agent.config.json) ----

variable "agent_name" {
  description = "Agent name — the STEM of the stage-scoped runtime/role names, not a resource name itself (see main.tf locals). Must match [a-zA-Z][a-zA-Z0-9_]{0,31} (max 32 chars: the backend's AGENT_NAME_RE, tightened in E28A/T1b so both derived names fit their AWS ceilings)."
  type        = string

  # E28A/T1b: 32, not 48. This var no longer NAMES anything — main.tf derives
  # `{agent_name}_{stage}` (AWS cap 48) and `{agent_name}-{stage}-agentcore-exec`
  # (IAM cap 64) from it, and at 48 the role name was already 63 of 64 before any
  # stage suffix. The DERIVED names carry their own preconditions in main.tf; this
  # stays as the stem's own contract and mirrors backend models/project.py's
  # AGENT_NAME_RE, which refuses an over-long name at repo-create — the earliest
  # and only place a human sees a useful error.
  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,31}$", var.agent_name))
    error_message = "agent_name must match ^[a-zA-Z][a-zA-Z0-9_]{0,31}$ (start with a letter, then letters/digits/underscores, max 32 chars)."
  }
}

variable "stage" {
  # REQUIRED, and deliberately WITHOUT a default. A default would make the single
  # most dangerous mistake in this module silent: a caller that forgets to thread
  # the stage would name PROD's resources `dev`, then either collide with dev's
  # real runtime or — worse — succeed and have prod traffic served by a resource
  # every operator reads as dev. With no default, a missing thread fails at
  # `terraform plan` and names itself. (D-A5: EVERY stage is suffixed, including
  # dev, so there is no "the normal case" to default to.)
  #
  # Free-form (D8) — NOT an enum of dev/prod. The platform already treats stage as
  # a string end to end (the state key, the Deployment row, `_tag`), so an enum
  # here would be the module refusing a stage the rest of the system accepts.
  description = "Deployment stage (e.g. dev, prod). REQUIRED, no default — suffixes BOTH account-global resource names (see main.tf locals). Free-form, but must be usable inside an AgentCore runtime name."
  type        = string

  # Only what the derived names cannot tolerate: emptiness (which would produce
  # `agent_` / `agent--agentcore-exec` — legal-looking, silently un-scoped names)
  # and characters illegal in an agent_runtime_name. Length is NOT capped here
  # because the ceiling belongs to the COMBINATION, and main.tf's preconditions
  # check that on the real derived string rather than guessing a budget.
  validation {
    condition     = can(regex("^[a-zA-Z0-9_]+$", var.stage))
    error_message = "stage must be non-empty and contain only letters, digits or underscores (it is embedded in the runtime name, whose pattern forbids hyphens)."
  }
}

variable "model_id" {
  description = "Bedrock model id passed to the container as MODEL_ID."
  type        = string
}

variable "log_level" {
  description = "Log level passed to the container as LOG_LEVEL."
  type        = string
  default     = "INFO"
}

variable "guardrail_id" {
  description = "Optional Bedrock guardrail id. When non-empty, passed to the container as GUARDRAIL_ID."
  type        = string
  default     = ""
}

variable "otel_enabled" {
  description = "Whether OpenTelemetry instrumentation is enabled. Passed to the container as OTEL_ENABLED."
  type        = bool
  default     = false
}

variable "lifecycle_idle_timeout_seconds" {
  description = "Idle runtime session timeout in seconds (60–28800)."
  type        = number
  default     = 900

  validation {
    condition     = var.lifecycle_idle_timeout_seconds >= 60 && var.lifecycle_idle_timeout_seconds <= 28800
    error_message = "lifecycle_idle_timeout_seconds must be between 60 and 28800."
  }
}

variable "lifecycle_max_lifetime_seconds" {
  description = "Max runtime session lifetime in seconds (AgentCore hard max 28800/8h). Set explicitly so the provider plan matches the API's auto-default (avoids 'inconsistent result after apply')."
  type        = number
  default     = 28800

  validation {
    condition     = var.lifecycle_max_lifetime_seconds >= 60 && var.lifecycle_max_lifetime_seconds <= 28800
    error_message = "lifecycle_max_lifetime_seconds must be between 60 and 28800."
  }
}

# ---- Platform-owned (tfvars from CodeBuild, Task 5) ----

variable "container_image_uri" {
  # E28B/T4 (D-B3): the buildspec now passes the DIGEST form `<repo>@sha256:<64 hex>` whenever it
  # has a digest, falling back to `<repo>:<tag>` only for a rollback (whose target is validated
  # from tag-keyed deployment rows carrying no digest) or a pre-E28B agent repo. A tag is a mutable
  # pointer, so the bytes behind one can change between the moment an owner approves and the moment
  # prod deploys; a digest names the bytes themselves. Both forms are accepted by AgentCore's own
  # `containerUri` grammar, which permits an optional `:tag` and an optional `@digest`.
  description = "Full ECR image URI for the runtime container. Prefer the immutable digest form <repo>@sha256:<hex>; the <repo>:<tag> form is accepted for rollback and pre-digest callers."
  type        = string

  validation {
    # Refuse a reference with NEITHER a tag nor a digest. `<repo>` alone silently means `:latest`
    # to the runtime, which on a shared tenant registry is very unlikely to be this agent's image
    # and is exactly the "deploying something you cannot name" failure this epic removes. The
    # buildspec guards the digest's shape before it gets here; this is the module-side backstop for
    # any other caller.
    #
    # BOTH alternatives spell out the REPOSITORY part as well as the reference part, and every
    # loosening of either half was caught by executing the condition rather than by reading it —
    # three separate times, which is why it is written out in full:
    #
    #   `:[^:/@]+$`      (unanchored tag) matched the TAIL of a malformed digest, so
    #                    `<repo>@sha256:zz` passed as though `:zz` were a tag.
    #   `[^:/@]+` (tag)  admitted `<repo>:tag; echo …`, `$()` and backtick forms. Inert today (the
    #                    value is only ever written into a quoted heredoc, never shell-evaluated)
    #                    but a validation whose stated job is "name a specific image" must not be
    #                    the thing relying on that. Now ECR's own tag grammar: [A-Za-z0-9._-], max
    #                    128, no leading hyphen.
    #   `^[^@]+` (repo)  admitted `junk stuff@sha256:…` and `<repo>:tag@sha256:…` — a reference
    #                    carrying BOTH a tag and a digest, which is ambiguous about what deploys.
    #
    # Verified by evaluating the real condition through `terraform plan` across 14 inputs (the
    # parametrized test in tests/test_buildspec_contract.py drives the same expression): the digest
    # and tag forms pass for both a bare host and a full `<acct>.dkr.ecr.<region>.amazonaws.com`
    # one, while every metacharacter, space, leading-hyphen, double-`@`, tag+digest, empty-repo and
    # malformed-digest form is refused.
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._/-]*@sha256:[0-9a-f]{64}$", var.container_image_uri)) || can(regex("^[A-Za-z0-9][A-Za-z0-9._/-]*:[A-Za-z0-9._][A-Za-z0-9._-]{0,127}$", var.container_image_uri))
    error_message = "container_image_uri must name a specific image: either <repo>@sha256:<64 lowercase hex> (preferred, immutable) or <repo>:<tag>."
  }
}

variable "network_mode" {
  description = "Runtime network mode. PUBLIC (default, the proven pattern) needs no subnets/SGs; VPC attaches the runtime to vpc_subnet_ids/security_group_ids."
  type        = string
  default     = "PUBLIC"

  validation {
    condition     = contains(["PUBLIC", "VPC"], var.network_mode)
    error_message = "network_mode must be one of: PUBLIC, VPC."
  }
}

variable "vpc_subnet_ids" {
  description = "Platform-owned subnet ids the runtime attaches to. Required only when network_mode = \"VPC\"; ignored for PUBLIC."
  type        = list(string)
  default     = []
}

variable "security_group_ids" {
  description = "Platform-owned security group ids for the runtime. Required only when network_mode = \"VPC\"; ignored for PUBLIC."
  type        = list(string)
  default     = []
}

variable "exec_role_arn" {
  description = "Optional execution role ARN override for the runtime. If empty, the module creates its own in-account exec role; if set, that role is used instead."
  type        = string
  default     = ""
}

variable "ecr_repo_arn" {
  description = "ARN of the tenant ECR repo the exec role may pull from; empty = pull scoped to * (account-local by assumed-role boundary)."
  type        = string
  default     = ""
}

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

variable "aws_region" {
  description = "AWS region, passed to the container as AWS_REGION."
  type        = string
}

variable "deploy_role_arn" {
  description = "Cross-account deploy role the provider assumes so runtime resources land in the tenant account; empty = deploy-in-place (single-account)."
  type        = string
  default     = ""
}

# ---- Langfuse observability (E26/T10) ----
# The platform provisions one Langfuse project + key PER AGENT and stores the {public_key,
# secret_key} pair in Secrets Manager. Only the NON-SECRET host + the Secrets Manager NAME are
# passed to the runtime (as LANGFUSE_HOST / LANGFUSE_SECRET_NAME); the agent reads the key VALUES
# from Secrets Manager at import. The key VALUES NEVER transit tfvars/env. Empty ⇒ agent runs with
# observability disabled.

variable "langfuse_host" {
  description = "Langfuse public HTTPS endpoint (CloudFront). Passed to the container as LANGFUSE_HOST. Empty ⇒ observability disabled."
  type        = string
  default     = ""
}

variable "langfuse_secret_name" {
  description = "Secrets Manager NAME holding the per-agent Langfuse {public_key, secret_key} pair. Passed to the container as LANGFUSE_SECRET_NAME (a NAME, never a secret value). Empty ⇒ observability disabled."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to the runtime."
  type        = map(string)
  default     = {}
}
