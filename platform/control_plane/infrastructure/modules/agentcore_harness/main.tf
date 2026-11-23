# ---------------------------------------------------------------------------
# Platform-owned AgentCore HARNESS (native aws_bedrockagentcore_harness, AWS
# provider >= 6.32). E30/T7.
#
# A harness is the config-driven managed runtime: no container, no ECR, no
# orchestration code — the agent IS its parameters (model, system prompt, tools,
# iteration/timeout caps). So this module's whole job is to turn ONE developer-
# owned file, `harness.yaml`, into one AWS resource plus the execution role that
# resource assumes.
#
# PREVIEW. The harness API and its Terraform coverage are preview-status; CDK is
# AWS's blessed path. This module is the recorded IaC bet, and the recorded
# fallback (null_resource + AWS CLI create-harness IN THIS MODULE) is written down
# in README.md §"Preview caveat" so that taking it later is a decision, not a
# drift. Read that section before the live test.
#
# Structure deliberately mirrors ../agentcore_runtime: exec role created in-module
# with an optional override, a time_sleep for IAM propagation, both account-global
# names derived stage-scoped in ONE locals block, and preconditions on the derived
# names so an over-long name fails at `plan` instead of mid-deploy.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The agent definition. `harness.yaml` lives in the DEVELOPER's agent repo and is
# the deploy artifact — this module reads it rather than accepting the same values
# as tfvars, so `git log harness.yaml` stays the audit trail for what an agent was
# told to do.
#
# The five keys are PINNED with T6 (the scaffold that writes the file) and are
# consumed verbatim: model_id, system_prompt, tools, max_iterations,
# timeout_seconds. Renaming one here without renaming it in the scaffold produces
# a deploy that silently ignores the developer's value, so treat them as a
# cross-repo contract.
#
# EVERY key is read through `try(...)`, including the two REQUIRED ones — and that
# is about the error message, not about tolerance. A bare
# `local.harness_config["model_id"]` on a file missing the key fails in the
# `locals` block with Terraform's generic
#   Error: Invalid index … The given key does not identify an element
# which names neither `harness.yaml` nor what to put in it, and which aborts
# BEFORE the preconditions below can run — making them unreachable for the case
# they exist to catch (verified by running exactly that). Defaulting to "" instead
# lets the precondition fire and say "harness.yaml (<path>) must set a non-empty
# `model_id`". `tools` defaults to [] because a tool-less harness is legitimate
# (model + prompt only), and max_iterations/timeout_seconds default to null so the
# service picks its own.
# ---------------------------------------------------------------------------
locals {
  harness_config = yamldecode(file(var.harness_config_path))

  model_id      = try(local.harness_config["model_id"], "")
  system_prompt = try(local.harness_config["system_prompt"], "")

  tools             = try(local.harness_config["tools"], [])
  max_iterations    = try(local.harness_config["max_iterations"], null)
  timeout_seconds   = try(local.harness_config["timeout_seconds"], null)
  config_max_tokens = try(local.harness_config["max_tokens"], null)

  # BOTH account-global names, stage-scoped, derived HERE and only here — the same
  # rule (and the same live failure) as agentcore_runtime's locals: a prod promote
  # died on `creating IAM Role (…-agentcore-exec): EntityAlreadyExists` because the
  # state key was stage-scoped while the NAME was not.
  #
  # LIVEFIX (E30 dev deploy): the harness name takes an UNDERSCORE separator, same
  # as the runtime module's — NOT the hyphen this comment used to claim. The real
  # AWS harness-name constraint is `[a-zA-Z][a-zA-Z0-9_]{0,39}` (max 40 chars, no
  # hyphens at all); a hyphenated name like `e2e_harness_1003-dev` was rejected live.
  # The precondition below enforces the real pattern/length; this local only derives
  # the string. No truncation here, mirroring agentcore_runtime's rejected-truncation
  # call (main.tf comment above its locals): two long names sharing a prefix would
  # collide silently. An over-long agent_name+stage combination fails at `plan`
  # instead, naming harness.yaml/the agent name.
  harness_name = "${var.agent_name}_${var.stage}"

  # `-harness-exec`, NOT the runtime module's `-agentcore-exec`. This is load-
  # bearing: IAM role names are ACCOUNT-GLOBAL, and the same agent+stage may own a
  # container runtime as well (an agent can be re-materialized from a different
  # template). Sharing the suffix would make the two modules fight over one role
  # name — the second to apply fails with EntityAlreadyExists, which is exactly
  # the class of bug E28A/T1b removed from the runtime module.
  exec_role_name = "${var.agent_name}-${var.stage}-harness-exec"

  # The state key convention (design §3), spelled once. The BACKEND configures the
  # backend for this root module, so this local does not configure anything — it
  # exists so the convention is checkable from the module that lives under it (see
  # the state_key output).
  state_key = "agentcore-harness/${var.agent_id}/${var.stage}/terraform.tfstate"

  # Gateway ARNs the harness may reach. `tools` entries are AgentCore GATEWAY ARNs
  # (design §3: "tools (gateway references)"), so the exec-role grant below can be
  # scoped to exactly the gateways this agent's config names — no wildcard.
  gateway_arns = [for t in local.tools : tostring(t)]

  # Every entry that is NOT a well-formed AgentCore gateway ARN. Collected rather
  # than checked inline because each entry is used TWICE — as a `tool` block's
  # `gateway_arn` and as an IAM policy `Resource` — and the IAM half is the one
  # that fails badly: a bare gateway NAME (the intuitive thing to write, and what
  # the scaffold's comments used to suggest) produces
  # `MalformedPolicyDocument: Resource must be in ARN format` at apply, naming the
  # policy document and never `harness.yaml`. The developer who wrote the offending
  # line gets an error about a file they have never seen.
  #
  # Pattern notes: the partition is left open (`aws`, `aws-us-gov`, `aws-cn`) since
  # this module is deployed by callers that choose their own account and region; the
  # region and account-id segments must be non-empty (a gateway ARN is
  # account-owned, so a `::` there is malformed rather than "any"); and the id
  # segment after `gateway/` must be non-empty. Deliberately NOT strict about the
  # id's own alphabet — that is AWS's to define and a wrong guess here would refuse
  # a legal ARN, which is worse than accepting an odd one that AWS then rejects
  # with its own clear message.
  invalid_tool_entries = [
    for t in local.gateway_arns : t
    if !can(regex("^arn:aws[a-z0-9-]*:bedrock-agentcore:[a-z0-9-]+:[0-9]+:gateway/.+$", t))
  ]

  # E40/T10: governed gateway tools get TOKEN_EXCHANGE outbound auth (spike
  # 2026-10-06: PASS with the agent's MicrosoftOauth2 OBO provider). No provider
  # means no map, so an agent without one keeps today's tool shape exactly.
  tool_auth_scopes  = var.obo_provider_arn == "" ? {} : var.gateway_tool_auth
  oauth_tool_arns   = [for a in local.gateway_arns : a if contains(keys(local.tool_auth_scopes), a)]
  obo_provider_name = element(split("/", var.obo_provider_arn), length(split("/", var.obo_provider_arn)) - 1)
}

# Where this deploy actually landed. Read from the module's OWN provider, so under
# a cross-account deploy (deploy_role_arn set) both resolve in the TENANT account
# — which is where the harness runs. These exist so the exec-role policy can scope
# resource ARNs without a literal: a hardcoded account id is a hard project rule
# violation and, in a module that deploys into an account it does not choose,
# would also simply be wrong.
data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

# Execution role the AgentCore harness service assumes. Created under the module's
# provider, so it lands in the tenant account whenever deploy_role_arn is set. If
# exec_role_arn is passed (non-empty) that override wins and this role is still
# created but unused by the harness (see execution_role_arn below) — the same
# precedence the runtime module uses, so the single-account path can keep passing
# a platform-account role.
resource "aws_iam_role" "exec" {
  name = local.exec_role_name
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "bedrock-agentcore.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = var.tags

  lifecycle {
    # IAM's own cap, checked at `plan` rather than at CreateRole — and, because the
    # role is created BEFORE the harness, an unguarded overflow here would strand a
    # half-built deploy.
    precondition {
      condition     = length(local.exec_role_name) <= 64
      error_message = "The derived exec role name \"${local.exec_role_name}\" is ${length(local.exec_role_name)} characters; IAM allows at most 64. Shorten agent_name (max 32, see AGENT_NAME_RE) or the stage name."
    }
  }
}

resource "aws_iam_role_policy" "exec" {
  name = "harness-exec"
  role = aws_iam_role.exec.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        # The harness invokes the model named in harness.yaml. Two ARN forms,
        # because `model_id` may be a bare foundation-model id OR an inference
        # profile id, and they live under different prefixes — the T6 scaffold ships
        # a `us.` CROSS-REGION inference profile as its default, so both are needed
        # on the very first deploy, not eventually.
        #
        # NOT scoped to local.model_id itself: the value is a developer-editable
        # string in another repo, so a typo would produce an AccessDenied that reads
        # like an outage instead of a validation error.
        #
        # The foundation-model half is scoped by ARN SHAPE, not by region, and that
        # is deliberate: a cross-region inference profile routes the invoke to
        # foundation models in OTHER regions (us-east-1/us-west-2/us-east-2 for
        # `us.`), so a deploy-region-only grant AccessDenies the very default the
        # scaffold ships — and it would fail at invoke time, long after a green
        # apply. Widening it costs little: foundation-model ARNs carry NO account id
        # (`…:bedrock:<region>::foundation-model/…`) because they name AWS's public
        # model catalog, so this grants no access to anything of ours, and Bedrock
        # still refuses any model the account has not been granted.
        #
        # The inference-profile half stays scoped to the deploy region AND account —
        # that ARN *is* account-owned, so it is a real boundary worth keeping.
        {
          Effect = "Allow"
          Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
          Resource = [
            "arn:aws:bedrock:*::foundation-model/*",
            "arn:aws:bedrock:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:inference-profile/*",
          ]
        },
        # CloudWatch logs, as the runtime module's exec role has. CreateLogGroup
        # cannot be scoped to a group that does not exist yet, so this stays on "*"
        # — the same call the runtime module makes.
        {
          Effect   = "Allow"
          Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
          Resource = "*"
        },
        # E42/T17 — span export: without these the harness's ADOT exporter gets 403 from the X-Ray
        # OTLP endpoint on every invoke and no span lands. Same set as agentcore_runtime's exec role.
        {
          Effect   = "Allow"
          Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets", "xray:PutSpans", "xray:PutSpansForIndexing"]
          Resource = "*"
        },
        {
          Effect    = "Allow"
          Action    = "cloudwatch:PutMetricData"
          Resource  = "*"
          Condition = { StringEquals = { "cloudwatch:namespace" = "bedrock-agentcore" } }
        },
        # LIVEFIX (E30 dev harness invoke): AWS auto-creates a MANAGED MEMORY
        # alongside every harness (visible on the harness as
        # `memory.managedMemoryConfiguration.arn`, not something this module
        # provisions), and the harness's own invoke path reads/writes it directly —
        # verified live: invoking `e2e_hrn2_1003_dev` failed with
        # `AccessDeniedException ... e2e_hrn2_1003-dev-harness-exec is not
        # authorized to perform: bedrock-agentcore:ListEvents on resource:
        # arn:aws:bedrock-agentcore:<region>:<account>:memory/e2e_hrn2_1003_dev-<id>`
        # before this grant existed.
        #
        # Actions are exactly the bedrock-agentcore DATA-PLANE memory operations
        # from the service model (botocore bedrock-agentcore/2024-02-28
        # service-2.json) that take a memoryId: event read/write (CreateEvent,
        # GetEvent, ListEvents, ListSessions) and memory-record read (GetMemoryRecord,
        # ListMemoryRecords, RetrieveMemoryRecords). DeleteEvent/DeleteMemoryRecord
        # are in the same model but not granted — nothing observed at invoke time
        # needs them, and adding them would widen past what is evidenced.
        #
        # Resource is a NAME-PREFIX pattern, not the literal ARN from the live
        # harness above: the id suffix (`-zbDf1UBlqa`) is AWS-generated per managed
        # memory, so it cannot be known at `plan` time. `local.harness_name` is
        # exactly the prefix AWS uses (`<agent_name>_<stage>`, confirmed against the
        # live memory id `e2e_hrn2_1003_dev-zbDf1UBlqa`), so the wildcard only
        # reaches memories this harness (by name) could own — not every memory in
        # the account.
        {
          Effect = "Allow"
          Action = [
            "bedrock-agentcore:CreateEvent",
            "bedrock-agentcore:GetEvent",
            "bedrock-agentcore:ListEvents",
            "bedrock-agentcore:ListSessions",
            "bedrock-agentcore:GetMemoryRecord",
            "bedrock-agentcore:ListMemoryRecords",
            "bedrock-agentcore:RetrieveMemoryRecords",
          ]
          Resource = "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:memory/${local.harness_name}-*"
        },
      ],
      # Gateway tool access, and ONLY for the gateways harness.yaml names. Omitted
      # entirely when `tools` is empty: a tool-less harness gets no gateway grant at
      # all, so the least-privilege shape follows the config instead of being a
      # constant. `length(...) > 0` guard because an empty Resource list is invalid
      # IAM and would fail at apply, not plan.
      length(local.gateway_arns) > 0 ? [{
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:InvokeGateway", "bedrock-agentcore:GetGateway", "bedrock-agentcore:ListGatewayTargets"]
        Resource = local.gateway_arns
      }] : [],
      # E40/T10: the token exchange behind a TOKEN_EXCHANGE tool, exactly as the
      # spike's denials named it (one resource per denial, added until it passed).
      # The service mints the workload token itself, so no GetWorkloadAccessToken.
      # The workload identity is named after the harness's backing runtime
      # (`harness_<harness_name>-<suffix>`), and the provider's vault secret is
      # `<provider>-<suffix>`, hence the two wildcards. Omitted with no oauth tool.
      # Both Resources are lists so the two objects share one type (else the
      # conditional's tuple/empty branches cannot unify).
      length(local.oauth_tool_arns) > 0 ? [
        {
          Effect = "Allow"
          Action = ["bedrock-agentcore:GetResourceOauth2Token"]
          Resource = [
            "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:token-vault/default",
            var.obo_provider_arn,
            "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default",
            "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default/workload-identity/harness_${local.harness_name}-*",
          ]
        },
        {
          Effect   = "Allow"
          Action   = ["secretsmanager:GetSecretValue"]
          Resource = ["arn:aws:secretsmanager:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:secret:bedrock-agentcore-identity!default/oauth2/${local.obo_provider_name}-*"]
        },
      ] : [],
    )
  })
}

# IAM is eventually consistent: on first provision the harness validates the
# freshly-created exec role for the bedrock-agentcore.amazonaws.com principal
# before IAM has propagated it, yielding a ValidationException. This sleep lets IAM
# propagate before the harness consumes the role. Only needed when the module
# self-provisions the role (exec_role_arn == ""); an override role pre-exists, so
# count = 0 skips the wait entirely. Copied deliberately from agentcore_runtime,
# where this exact race was observed live.
resource "time_sleep" "wait_for_exec_role" {
  count           = var.exec_role_arn == "" ? 1 : 0
  depends_on      = [aws_iam_role.exec, aws_iam_role_policy.exec]
  create_duration = "20s"
}

resource "aws_bedrockagentcore_harness" "this" {
  harness_name       = local.harness_name
  execution_role_arn = var.exec_role_arn != "" ? var.exec_role_arn : aws_iam_role.exec.arn
  region             = var.region

  depends_on = [time_sleep.wait_for_exec_role]

  # Both are opt+computed in the provider schema, so `null` means "let the service
  # pick" rather than "set 0" — which is why harness.yaml may omit them.
  max_iterations  = local.max_iterations
  timeout_seconds = local.timeout_seconds

  system_prompt {
    text = local.system_prompt
  }

  model {
    bedrock_model_config {
      model_id = local.model_id

      # Only forwarded when harness.yaml sets it; null leaves the service default.
      # max_tokens is NOT one of the five pinned keys — it is read opportunistically
      # so a developer who adds it is not silently ignored.
      max_tokens = local.config_max_tokens
    }
  }

  # One `tool` block per gateway reference in harness.yaml. A dynamic block (not a
  # static list) because the count is the developer's to choose, including zero:
  # a harness with model + prompt and no tools is legitimate and must not be a
  # special case in the module.
  dynamic "tool" {
    for_each = local.gateway_arns
    content {
      # Lower-case: the provider's enum is `agentcore_gateway` (6.67 rejects the
      # upper-case form at plan; found by E40/T10's mocked plan — no tool had ever
      # been deployed through this module before).
      type = "agentcore_gateway"

      # The gateway's own name segment, so the harness's tool list is readable in
      # the console instead of being three unnamed entries. ARN shape is
      # arn:aws:bedrock-agentcore:<region>:<acct>:gateway/<id>, hence the last
      # slash-segment.
      name = element(split("/", tool.value), length(split("/", tool.value)) - 1)

      config {
        agentcore_gateway {
          gateway_arn = tool.value

          # E40/T10: only for ARNs AGP governs (keys of the map). The scope is the
          # MCP app's `/.default`; access itself is the MCP owner's grant (the
          # agent->MCP consent), without which the exchange fails at tool load.
          dynamic "outbound_auth" {
            for_each = contains(keys(local.tool_auth_scopes), tool.value) ? [local.tool_auth_scopes[tool.value]] : []
            content {
              oauth {
                provider_arn = var.obo_provider_arn
                scopes       = [outbound_auth.value]
                grant_type   = "TOKEN_EXCHANGE"
              }
            }
          }
        }
      }
    }
  }

  # Inbound Entra authorizer (E30A/T1), identical to agentcore_runtime's: Entra v2
  # mints `aud` as the app GUID or the api:// URI, so both are allowed. With this
  # set the harness refuses SigV4 and accepts only `Authorization: Bearer` (spike
  # 2026-10-05).
  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url    = "https://login.microsoftonline.com/${var.tenant_id}/v2.0/.well-known/openid-configuration"
      allowed_audience = compact([var.entra_app_audience, var.entra_app_id])
    }
  }

  tags = var.tags

  lifecycle {
    # harness.yaml is the SOLE source of truth for the agent definition, so there is
    # deliberately NO ignore_changes here — unlike the runtime module, which must
    # ignore environment_variables because the platform injects MCP grants into it
    # out-of-band. Nothing writes to a harness after apply today; if something ever
    # does, add the ignore then and say what writes it, rather than pre-emptively
    # blinding this resource to drift.

    precondition {
      # LIVEFIX: the real AWS harness-name constraint is `[a-zA-Z][a-zA-Z0-9_]{0,39}`
      # (max 40 chars, letters/digits/underscores only — NO hyphens), not the
      # hyphen-tolerant 48-char pattern this precondition used to check. harness.yaml
      # (`e2e_harness_1003-dev`, derived from `agent_name-stage`) was rejected live by
      # the service with exactly this pattern. agent_name is already capped at 32 by
      # AGENT_NAME_RE, so a non-overflowing stage is at most 7 chars (32 + "_" + 7 =
      # 40); longer stage names fail here, at `plan`, rather than being truncated —
      # truncation risks two distinct agent+stage pairs colliding on one AWS-global
      # name (see agentcore_runtime's main.tf, which rejects truncation for the same
      # reason).
      condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,39}$", local.harness_name))
      error_message = "The derived harness name \"${local.harness_name}\" (${length(local.harness_name)} chars) must start with a letter, then contain only letters/digits/underscores (AWS allows no hyphens here), max 40. Shorten agent_name (max 32, see AGENT_NAME_RE) or the stage name in harness.yaml's agent registration."
    }

    # The two keys with no default. Checked HERE, on the decoded values, so the
    # error names harness.yaml and the missing key instead of surfacing as an AWS
    # ValidationException mid-build.
    precondition {
      condition     = try(length(trimspace(tostring(local.model_id))), 0) > 0
      error_message = "harness.yaml (${var.harness_config_path}) must set a non-empty `model_id`."
    }

    precondition {
      condition     = try(length(trimspace(tostring(local.system_prompt))), 0) > 0
      error_message = "harness.yaml (${var.harness_config_path}) must set a non-empty `system_prompt`."
    }

    # E30A/T1: an empty audience pair would create an authorizer with
    # `allowed_audience = []` — one that refuses every token — so fail at plan.
    precondition {
      condition     = length(compact([var.entra_app_audience, var.entra_app_id])) > 0
      error_message = "The inbound Entra authorizer needs an audience: entra_app_audience and entra_app_id are both empty. The agent record carries no Entra app identity — mint it (re-materialize the agent) and retry."
    }

    # Every `tools` entry must be a gateway ARN. Checked here, on the decoded list,
    # because these values flow into an IAM policy `Resource` as well as into the
    # tool blocks: a bare gateway name fails at apply with
    # `MalformedPolicyDocument: Resource must be in ARN format`, which names the
    # policy document and never harness.yaml. The message lists the offending
    # entries and shows the expected form, so the fix is obvious from the error.
    precondition {
      condition     = length(local.invalid_tool_entries) == 0
      error_message = "harness.yaml (${var.harness_config_path}): every entry in `tools` must be a full AgentCore Gateway ARN, not a gateway name. Expected form: arn:aws:bedrock-agentcore:<region>:<account-id>:gateway/<gateway-id>. Offending ${length(local.invalid_tool_entries) == 1 ? "entry" : "entries"}: ${join(", ", [for t in local.invalid_tool_entries : "\"${t}\""])}."
    }

    # E40/T10: the provider ARN is an IAM Resource and its last segment names the
    # vault secret, so a malformed one must fail here, not as MalformedPolicyDocument.
    precondition {
      condition     = var.obo_provider_arn == "" || can(regex("^arn:aws[a-z0-9-]*:bedrock-agentcore:[a-z0-9-]+:[0-9]+:token-vault/default/oauth2credentialprovider/[^/]+$", var.obo_provider_arn))
      error_message = "obo_provider_arn must be empty or an AgentCore OAuth2 credential provider ARN (arn:aws:bedrock-agentcore:<region>:<account-id>:token-vault/default/oauth2credentialprovider/<name>); got \"${var.obo_provider_arn}\"."
    }
  }
}
