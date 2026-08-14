# ---------------------------------------------------------------------------
# Platform-owned AgentCore Runtime (native aws_bedrockagentcore_agent_runtime,
# AWS provider >= 6.52). Network mode is configurable: PUBLIC (default, the
# proven pattern for this account) needs no subnets/SGs; VPC attaches the
# runtime to platform-owned subnets/SGs.
#
# UpdateAgentRuntime is a full-replace PUT. The inbound Entra authorizer is now
# set declaratively at apply-time (born wired) from the tenant/app tfvars. Only
# grant-time governance env vars are still written *after* this IaC applies
# (Epic-7 MCP env injection), so we ignore_changes on environment_variables
# alone — otherwise a re-apply would clobber them. This guard is load-bearing.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# E28A/T1b (finding #9, C-A1) — BOTH account-global names are STAGE-SCOPED, and
# derived HERE, in one place.
#
# The live failure: a prod promote died on
#   creating IAM Role (platform_agent-agentcore-exec): EntityAlreadyExists
# E28/T2 stage-scoped the Terraform STATE KEY, so prod gets a fresh state that
# sees no role and tries to create one — but the NAME was still agent-scoped and
# IAM names are ACCOUNT-GLOBAL, so dev already owned it. Prod deploys were
# structurally impossible on a single-account tenant. `agent_runtime_name` is the
# same class, latent: CreateAgentRuntime declares ConflictException; prod simply
# never reached it because IAM failed first.
#
# EVERY stage is suffixed, including dev (D-A5) — there is no magic default, so
# var.stage needs no special case and no stage can be silently assumed.
#
# Two DIFFERENT separators, and that is not a cosmetic inconsistency:
# agent_runtime_name's pattern is [a-zA-Z][a-zA-Z0-9_]{0,47} — UNDERSCORES ONLY,
# a hyphen is rejected outright — while the role name keeps the hyphenated
# `-agentcore-exec` idiom the backend's reclaim already derives. Do not "unify"
# them; the runtime name cannot take a hyphen.
#
# Renaming FORCES REPLACEMENT of both: agent_runtime_name carries
# stringplanmodifier.RequiresReplace() in the provider, and aws_iam_role.name is
# likewise destroy+create. So the first apply after this change destroys and
# recreates the existing dev runtime AND CHANGES ITS ARN. That is accepted
# (test-only tenant) and it is why the buildspec writes the ARN into a per-stage
# map (C-A2) — the recorded ARN must be re-read, never assumed stable.
#
# THIRD CONSEQUENCE OF THE REPLACEMENT, and the one that will look like a mystery
# regression during the live test: a replaced runtime is BORN WITH NO MCP_SERVERS
# ENV, so every agent that had MCP grants comes back unable to reach its MCP
# servers. `ignore_changes = [environment_variables]` cannot help — the resource is
# REPLACED, not updated, so the new runtime carries only the declarative env below.
# The only writer of MCP_SERVERS is agent_mcp_env.rebuild_runtime_mcp_env, and it
# is called ONLY from the grant/revoke paths (backend/src/services/
# agent_mcp_grant.py) — no deploy or build-completion path calls it, so nothing
# re-injects it. Recovery today is a manual re-grant (or revoke+re-grant) per
# affected agent. It fails CLOSED (no env, no tools, no unauthorized reach), so
# this is a governance divergence rather than a security hole — but it is
# undetectable from the deploy, which is why it is written down here. The real fix
# is a deploy-path call to rebuild_runtime_mcp_env; the deploy path never enters
# the backend today, so that is a separate task, deliberately NOT half-built here.
#
# The two ceilings are checked by preconditions on the resources below rather
# than here, because a `local` cannot carry one. They are the reason
# AGENT_NAME_RE was tightened to 32 (C-A3): the role name is ALREADY
# 48 + len("-agentcore-exec") = 63 of IAM's 64, so at the old 48 any stage suffix
# at all overflowed. TRUNCATING was rejected — two long names sharing a prefix
# would collide silently, the same account-global class being fixed here.
# ---------------------------------------------------------------------------
locals {
  runtime_name   = "${var.agent_name}_${var.stage}"                # AWS: [a-zA-Z][a-zA-Z0-9_]{0,47}
  exec_role_name = "${var.agent_name}-${var.stage}-agentcore-exec" # IAM: <= 64
}

# Where this deploy actually landed. Read from the module's OWN provider, so under a
# cross-account deploy (`deploy_role_arn` set) both resolve in the TENANT account — which is
# where the runtime runs and where the backend wrote the agent's Langfuse secret.
#
# These exist so the exec-role policy below can scope a resource ARN without a literal: a
# hardcoded account id is a hard project rule violation and, in a module that deploys into an
# account it does not choose, would also simply be wrong.
data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

# Execution role assumed by the AgentCore runtime service. Created under the
# module's provider (Task 2), so it lands in the tenant account whenever
# deploy_role_arn is set — letting the runtime pull the tenant's in-account ECR
# image. If exec_role_arn is passed (non-empty), that override wins and this role
# is still created but unused by the runtime (see role_arn below). Trust +
# policy actions mirror the E21 platform exec-role policy (now self-provisioned here).
#
# The name MUST stay byte-identical to `agentcore_exec_role_name()` in the
# backend's project_service.py: Terraform creates this role and the E23 delete
# cascade is the only thing that reclaims it (E28A/T2), so a drift does not raise
# anywhere — the teardown deletes a name that never existed, reports success, and
# leaks an account-global name that then blocks re-materializing the same agent.
resource "aws_iam_role" "exec" {
  name = local.exec_role_name
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "bedrock-agentcore.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = var.tags

  lifecycle {
    # IAM's own cap. Fails at `plan` with a readable message instead of at
    # CreateRole — and, because the role is created BEFORE the runtime, an
    # unguarded overflow here would strand a half-built deploy.
    precondition {
      condition     = length(local.exec_role_name) <= 64
      error_message = "The derived exec role name \"${local.exec_role_name}\" is ${length(local.exec_role_name)} characters; IAM allows at most 64. Shorten agent_name (max 32, see AGENT_NAME_RE) or the stage name."
    }
  }
}

resource "aws_iam_role_policy" "exec" {
  name = "runtime-exec"
  role = aws_iam_role.exec.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
      { Effect = "Allow", Action = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"], Resource = var.ecr_repo_arn != "" ? var.ecr_repo_arn : "*" },
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "*" },
      { Effect = "Allow", Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], Resource = "*" },
      # E28C/T5 (D-C5) — the OTHER HALF of the Langfuse fix, and without it the first half
      # changes nothing. The platform provisions one Langfuse project + key per agent and passes
      # only the Secrets Manager NAME to the container (LANGFUSE_SECRET_NAME); the agent resolves
      # the {public_key, secret_key} pair itself at import (agent-templates .../src/main.py). This
      # role had NO Secrets Manager grant at all, so that resolve would AccessDenied and the agent
      # would fall back to tracing nowhere — the same silent zero, now with the secret provisioned.
      #
      # Scoped to the per-agent Langfuse secrets by NAME PATTERN, not to one ARN: the secret's name
      # is derived from the agent id (LangfuseProvisioningService._agent_secret_name →
      # `langfuse-agent-{agent.id}-keys`) and is created by the BACKEND at register/materialize
      # time, so this module has no ARN to reference and must not take the agent id as another
      # variable to keep in sync.
      #
      # The trailing `*` is load-bearing twice over: it covers the agent id AND Secrets Manager's
      # own 6-character random suffix, which is appended to every secret ARN. A pattern ending at
      # `-keys` would match no ARN that exists.
      #
      # Still narrow: the prefix is a namespace only the Langfuse provisioner writes, so this role
      # cannot read the platform's Graph client secret, a tenant's credentials, or another
      # platform's secrets. It CAN read another agent's Langfuse keys — accepted, because the
      # alternative is threading the agent id into this module as a variable that Terraform,
      # buildspec and the backend must all keep in sync, and a drift there would be the same
      # silent AccessDenied this grant exists to remove.
      #
      # Account and region come from data sources — never a literal (hard project rule). Under a
      # cross-account deploy both resolve in the TENANT account, which is correct: that is where
      # the runtime runs and where the backend wrote the secret.
      {
        Effect = "Allow"
        Action = "secretsmanager:GetSecretValue"
        # `.region`, NOT the `.id` the older langfuse module uses: AWS provider 6.x deprecates
        # `.id` on this data source and `terraform validate` warns on it. New code takes the
        # non-deprecated attribute so this module's validate output stays clean — a module whose
        # validate already prints warnings is one where the next real warning goes unread.
        Resource = "arn:aws:secretsmanager:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:secret:langfuse-agent-*"
      },
      # E36/T4 (item 8) — the AgentCore Identity token actions. Every acme agent's own deploy.sh
      # attaches these three by hand after `agentcore configure`
      # (applications/acme_reference_agent/deploy.sh:51-58 and identically in the other three), which
      # is precisely why the gap never raised: those roles are built OUTSIDE Terraform, so the
      # platform-provisioned role was the only one missing them and no acme agent could reveal it.
      #
      # Without this statement an agent that asks the workload identity for a token
      # (GetWorkloadAccessToken / ...ForJWT for its own workload identity, GetResourceOauth2Token for
      # an OAuth2 credential provider in the token vault) AccessDenies at the first outbound
      # OBO/on-behalf-of call. The failure is not silent for the caller but IS invisible to the
      # deploy: `terraform apply` and the runtime both come up green, and the denial only surfaces on
      # a live invoke that actually exchanges a token.
      #
      # Currently DOUBLE-MASKED and therefore not observable from this repo's own tests: the scaffold
      # template ships no MCP/OBO code (item 7), so nothing the platform generates calls these APIs
      # yet, and the acme agents run on hand-augmented roles. This lands with-or-before item 7 so the
      # first scaffolded live invoke does not fail on IAM.
      #
      # `default` is the account's implicit token vault / workload identity directory — the same
      # names the agents' READMEs scope to. Both the bare ARN and its `/*` children are listed: the
      # vault/directory resource itself is the target of some of these calls and its contained
      # credential-provider and workload-identity resources of the others, and an ARN pattern ending
      # at `default` matches only the former. Account and region come from the data sources above,
      # never a literal (hard project rule) — under a cross-account deploy they resolve in the TENANT
      # account, which is where the runtime actually asks for the token.
      {
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:GetWorkloadAccessToken",
          "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
          "bedrock-agentcore:GetResourceOauth2Token",
        ]
        Resource = [
          "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:token-vault/default",
          "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:token-vault/default/*",
          "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default",
          "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default/*",
        ]
      }
    ]
  })
}

# IAM is eventually consistent: on first provision the runtime validates the
# freshly-created exec role for the bedrock-agentcore.amazonaws.com principal
# before IAM has propagated it, yielding a ValidationException ("Role validation
# failed ..."). This sleep lets IAM propagate before the runtime consumes the
# role. Only needed when the module self-provisions the role (exec_role_arn ==
# ""); an override role pre-exists, so count = 0 skips the wait entirely.
resource "time_sleep" "wait_for_exec_role" {
  count           = var.exec_role_arn == "" ? 1 : 0
  depends_on      = [aws_iam_role.exec, aws_iam_role_policy.exec]
  create_duration = "20s"
}

resource "aws_bedrockagentcore_agent_runtime" "this" {
  agent_runtime_name = local.runtime_name
  role_arn           = var.exec_role_arn != "" ? var.exec_role_arn : aws_iam_role.exec.arn

  depends_on = [time_sleep.wait_for_exec_role]

  network_configuration {
    network_mode = var.network_mode

    # VPC mode carries subnets/SGs; PUBLIC mode omits network_mode_config entirely.
    dynamic "network_mode_config" {
      for_each = var.network_mode == "VPC" ? [1] : []
      content {
        subnets         = var.vpc_subnet_ids
        security_groups = var.security_group_ids
      }
    }
  }

  agent_runtime_artifact {
    container_configuration {
      container_uri = var.container_image_uri
    }
  }

  lifecycle_configuration {
    idle_runtime_session_timeout = var.lifecycle_idle_timeout_seconds
    max_lifetime                 = var.lifecycle_max_lifetime_seconds
  }

  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url    = "https://login.microsoftonline.com/${var.tenant_id}/v2.0/.well-known/openid-configuration"
      allowed_audience = compact([var.entra_app_audience, var.entra_app_id])
    }
  }

  environment_variables = merge(
    {
      # AGENT_NAME gives each provisioned agent a correct per-agent OTEL service.name
      # (agent-templates/strands-agentcore/src/main.py reads it for OTEL_SERVICE_NAME).
      # Sourced from the runtime name, so it is now STAGE-SCOPED too (E28A/T1b) — a
      # deliberate call, not a side effect of the rename. Langfuse provisions ONE project
      # PER AGENT (not per stage), so dev and prod traces land in the same project and
      # service.name is the only thing that can tell them apart; an agent-scoped value
      # would silently blend a prod incident into dev's traces. Nothing on the read side
      # correlates on it (langfuse_metrics_service groups by userId and trace name), so
      # splitting it costs no existing query. The rename forces replacement anyway, so
      # the value changes whether intended or not — this makes the choice explicit.
      AGENT_NAME   = local.runtime_name
      MODEL_ID     = var.model_id
      AWS_REGION   = var.aws_region
      LOG_LEVEL    = var.log_level
      OTEL_ENABLED = tostring(var.otel_enabled)
    },
    var.guardrail_id != "" ? { GUARDRAIL_ID = var.guardrail_id } : {},
    # Langfuse observability (E26/T10): inject the NON-SECRET host + the Secrets Manager NAME so the
    # agent reads its per-agent Langfuse key from Secrets Manager at import — zero manual wiring, no
    # key VALUE in the env. Only set when configured (empty ⇒ observability off). NOTE: these land
    # in the initial env only — a later grant-time set_runtime_environment merge preserves them (the
    # backend MERGES existing ∪ new), and the ignore_changes on environment_variables below stops a
    # re-apply from clobbering the grant-time keys.
    var.langfuse_host != "" ? { LANGFUSE_HOST = var.langfuse_host } : {},
    var.langfuse_secret_name != "" ? { LANGFUSE_SECRET_NAME = var.langfuse_secret_name } : {},
  )

  tags = var.tags

  lifecycle {
    ignore_changes = [
      environment_variables, # Epic-7 grant-time MCP env injection — never clobber
    ]

    precondition {
      condition     = var.network_mode != "VPC" || length(var.vpc_subnet_ids) > 0
      error_message = "network_mode = \"VPC\" requires at least one entry in vpc_subnet_ids."
    }

    # E28A/T1b — the runtime name's own pattern, checked on the DERIVED name.
    # var.agent_name carried this regex before T1b, where it validated the wrong
    # string: the resource is named `{agent_name}_{stage}`, so a legal agent_name
    # plus an illegal or over-long stage still produced an illegal resource name.
    # Underscores only (a hyphen is rejected by the API), 48 max. Both halves are
    # one condition because the API reports them as one InvalidParameter.
    precondition {
      condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,47}$", local.runtime_name))
      error_message = "The derived agent runtime name \"${local.runtime_name}\" (${length(local.runtime_name)} chars) must match ^[a-zA-Z][a-zA-Z0-9_]{0,47}$ — a leading letter, then letters/digits/UNDERSCORES only (no hyphens), max 48. Shorten agent_name (max 32, see AGENT_NAME_RE) or the stage name."
    }
  }
}
