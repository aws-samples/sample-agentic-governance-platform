# ---------------------------------------------------------------------------
# Platform-owned AgentCore GATEWAY (native aws_bedrockagentcore_gateway, AWS provider
# >= 6.66). E40/T4.
#
# An MCP server built from Ops is a gateway in front of the customer's existing REST API: the
# developer owns an OpenAPI spec and a `gateway.yaml`, and this module turns them into four
# resources — a gateway service role, an API-key credential provider (pointing at the
# customer's own Secrets Manager secret; omitted for `auth: none`), the gateway (MCP, Entra CUSTOM_JWT inbound), and one
# OpenAPI target named `api`.
#
# Terraform CREATES; the backend then GOVERNS the same gateway with UpdateGateway (Cedar
# policy-engine attach, authorizer replay). The two attributes the backend writes are in
# `ignore_changes` below, or the next apply would silently undo governance.
#
# Structure mirrors ../agentcore_harness: names derived stage-scoped in ONE locals block, a
# time_sleep for IAM propagation, preconditions so a bad config fails at `plan` naming the
# developer's file instead of an AWS ValidationException mid-build.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# `gateway.yaml` keys are a cross-task contract (C5): description, openapi_spec, auth, api_key
# {secret_arn, json_key, location, parameter_name, prefix}, instructions. Extra keys are
# ignored. Every key goes through `try(...)` for the harness module's reason: a bare index on
# a missing key aborts in `locals` with a generic "Invalid index" BEFORE the preconditions can
# say which file and which key. `openapi_spec` itself is resolved by the buildspec into
# var.openapi_spec_path, so it is not read here.
# ---------------------------------------------------------------------------
locals {
  gateway_config = yamldecode(file(var.gateway_config_path))

  description  = try(tostring(local.gateway_config["description"]), "")
  instructions = try(tostring(local.gateway_config["instructions"]), "")

  # E45 C2: `auth: none` = an OpenAPI target with no outbound credential (public APIs); the
  # `api_key:` block is then ignored. Absent = api_key, so existing repos are unchanged. A blank
  # `auth:` (YAML null) or "" also means api_key: coalesce errors on null/"", try catches it.
  auth_mode = try(coalesce(tostring(local.gateway_config["auth"])), "api_key")

  api_key_cfg        = try(local.gateway_config["api_key"], {})
  api_key_secret_arn = try(tostring(local.api_key_cfg["secret_arn"]), "")
  api_key_json_key   = try(tostring(local.api_key_cfg["json_key"]), "api_key")
  api_key_location   = try(tostring(local.api_key_cfg["location"]), "HEADER")
  api_key_parameter  = try(tostring(local.api_key_cfg["parameter_name"]), "x-api-key")
  api_key_prefix     = try(tostring(local.api_key_cfg["prefix"]), "")

  # yamldecode reads JSON too, so either spec format works. Only servers[0].url is checked —
  # AgentCore validates the rest of the spec itself, with better messages than a guess here.
  openapi_spec       = file(var.openapi_spec_path)
  openapi_server_url = try(tostring(yamldecode(local.openapi_spec)["servers"][0]["url"]), "")

  # The template ships this placeholder so a first push of an unedited repo fails at plan
  # instead of creating a gateway whose every tool call goes nowhere.
  placeholder_server_url = "https://REPLACE-ME.example.com"

  # All account-global names, stage-scoped, derived HERE only. The role name is load-bearing:
  # the ECS task role's iam:PassRole is scoped to `role/agp-*-gateway-role`, and the backend's
  # UpdateGateway (authorizer replay, Cedar attach) passes this role — any other name is an
  # AccessDenied there, long after a green apply.
  gateway_name             = "agp-${var.mcp_name}-${var.stage}"
  role_name                = "agp-${var.mcp_name}-${var.stage}-gateway-role"
  credential_provider_name = "agp-${var.mcp_name}-${var.stage}-api-key"

  state_key = "agentcore-gateway/${var.mcp_server_id}/${var.stage}/terraform.tfstate"

  # The API-key secret must live in THIS MCP server's reserved namespace, in this account and
  # region (E40 security S1). The IAM grant is built from these data sources — never from the
  # developer's `secret_arn` — so a repo pusher cannot point the gateway at another secret
  # (a platform credential) and have the key sent to their own server URL. mcp_name is
  # ^[a-z][a-z0-9-]+$, so it is regex-safe; the trailing `/` keeps `foo` out of `foo-bar/`.
  secret_name_prefix = "agp-mcp/${var.mcp_name}/"
  secret_arn_prefix  = "arn:${data.aws_partition.current.partition}:secretsmanager:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:secret:${local.secret_name_prefix}"
}

# Where this deploy landed — scopes the AgentCore Identity grant below without a literal
# account id.
data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

data "aws_partition" "current" {}

resource "aws_iam_role" "gateway" {
  name = local.role_name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    # aws:SourceAccount (E40 security S2): only AgentCore acting for THIS account may assume
    # the role — the confused-deputy guard the T1 spike role was tested with. No SourceArn:
    # the gateway ARN does not exist at role-create time and that form is untested live.
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
    }]
  })
  tags = var.tags

  lifecycle {
    precondition {
      condition     = length(local.role_name) <= 64
      error_message = "The derived gateway role name \"${local.role_name}\" is ${length(local.role_name)} characters; IAM allows at most 64. Shorten the MCP name or the stage name."
    }
  }
}

resource "aws_iam_role_policy" "gateway" {
  name = "gateway"
  role = aws_iam_role.gateway.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      # Cedar (E8): when the backend attaches a policy engine, AgentCore uses THIS role to
      # GetPolicyEngine at attach time and AuthorizeAction / PartiallyAuthorizeActions per
      # request. Without them the attach is denied and every tool call default-denies.
      # Resource "*": the engine ARN does not exist yet at role-create time, and these are
      # read/evaluate only (same grant as backend/scripts/demo_gateway_common.py).
      {
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:GetPolicyEngine",
          "bedrock-agentcore:AuthorizeAction",
          "bedrock-agentcore:PartiallyAuthorizeActions",
        ]
        Resource = "*"
      },
      # API-key outbound: the gateway fetches the key from the token vault through its
      # workload identity. Measured live (E40/T1): these two plus GetSecretValue below, each
      # one required — without any of them the target is still READY but every tools/call
      # fails with a generic "internal error". Scoped to this account+region's AgentCore
      # resources: the workload identity and token vault ARNs are service-generated.
      {
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:GetWorkloadAccessToken",
          "bedrock-agentcore:GetResourceApiKey",
        ]
        Resource = "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      },
      ],
      # EXTERNAL secret source: the key lives in the customer's own secret and is read at call
      # time — never copied into Terraform state or a platform-managed secret. Scoped to this MCP
      # server's namespace (local.secret_arn_prefix), NOT to the gateway.yaml string: the
      # credential provider's precondition pins that string inside the same namespace. A
      # CMK-encrypted secret also needs kms:Decrypt on that key, granted on the key policy
      # (README). `auth: none` reads no secret, so it gets no grant.
      local.auth_mode == "api_key" ? [
        {
          Effect   = "Allow"
          Action   = ["secretsmanager:GetSecretValue"]
          Resource = "${local.secret_arn_prefix}*"
        },
    ] : [])
  })
}

# IAM is eventually consistent: CreateGateway validates the fresh role before it has
# propagated. Same 20s wait the harness/runtime modules use.
resource "time_sleep" "wait_for_gateway_role" {
  depends_on      = [aws_iam_role.gateway, aws_iam_role_policy.gateway]
  create_duration = "20s"
}

# EXTERNAL source: AgentCore Identity reads the key from the customer's secret; the provider
# stores only the reference, so no key value ever lands in Terraform state (provider >= 6.66).
# Only for `auth: api_key` — with count 0 its precondition is never evaluated either.
resource "aws_bedrockagentcore_api_key_credential_provider" "this" {
  count                 = local.auth_mode == "api_key" ? 1 : 0
  name                  = local.credential_provider_name
  api_key_secret_source = "EXTERNAL"

  api_key_secret_config {
    secret_id = local.api_key_secret_arn
    json_key  = local.api_key_json_key
  }

  tags = var.tags

  lifecycle {
    # Checked on the resource that points at the secret (S1): the ARN must be in THIS account
    # and region and under agp-mcp/<mcp_name>/, so the provider can only ever read a secret the
    # namespaced grant on the gateway role covers. Full ARN with the 6-char random suffix; the
    # name class excludes `*` and `?`, so no wildcard can widen what the provider points at.
    precondition {
      condition     = can(regex("^${local.secret_arn_prefix}[A-Za-z0-9/_+=.@-]+-[A-Za-z0-9]{6}$", local.api_key_secret_arn))
      error_message = "gateway.yaml (${var.gateway_config_path}) must set `api_key.secret_arn` to the full ARN of a Secrets Manager secret named under agp-mcp/${var.mcp_name}/ in this account and region (${data.aws_region.current.region}), including its 6-character suffix — e.g. ${local.secret_arn_prefix}api-key-AbCdEf. Create it with: aws secretsmanager create-secret --name agp-mcp/${var.mcp_name}/api-key --secret-string '{\"api_key\":\"<your key>\"}'. The gateway can read only secrets in that namespace. Got \"${local.api_key_secret_arn}\"."
    }
  }
}

resource "aws_bedrockagentcore_gateway" "this" {
  name            = local.gateway_name
  role_arn        = aws_iam_role.gateway.arn
  protocol_type   = "MCP"
  authorizer_type = "CUSTOM_JWT"
  description     = local.description != "" ? local.description : null

  depends_on = [time_sleep.wait_for_gateway_role]

  # The tenant's Entra v2 discovery URL and both aud forms (api:// URI then app GUID), as
  # McpIdentityService._configure_gateway_authorizer builds them. authorizer_type is immutable
  # after create, so the gateway is BORN CUSTOM_JWT with the real audience — the MCP's Entra
  # app is minted at materialize, before the first build.
  #
  # allowed_scopes = Invoke admits DELEGATED tokens only. Measured live (E40/T1): an app-only
  # client_credentials token for this audience has no `scp` and passes an audience-only
  # authorizer; with Invoke required it gets 403 while the harness's OBO token passes. Do not
  # use custom_claim on `scp` instead — AgentCore rejects it (gateway UPDATE_UNSUCCESSFUL).
  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url    = "https://login.microsoftonline.com/${var.tenant_id}/v2.0/.well-known/openid-configuration"
      allowed_audience = compact([var.entra_app_audience, var.entra_app_id])
      allowed_scopes   = ["Invoke"]
    }
  }

  # Only when instructions are set: an empty `mcp {}` is a CreateGateway ValidationException
  # ("MCP configuration cannot be empty"), measured live on the E40 module smoke.
  dynamic "protocol_configuration" {
    for_each = local.instructions != "" ? [1] : []
    content {
      mcp {
        instructions = local.instructions
      }
    }
  }

  tags = var.tags

  lifecycle {
    # The backend owns both after create: McpCedarService attaches/detaches the policy engine
    # (an apply would otherwise DETACH it — ENFORCE silently gone), and
    # McpIdentityService replays the authorizer. Terraform sets the authorizer correctly at
    # create; ignoring it afterwards is the guard against ever resetting the audience.
    ignore_changes = [policy_engine_configuration, authorizer_configuration]

    precondition {
      condition     = contains(["none", "api_key"], local.auth_mode)
      error_message = "gateway.yaml (${var.gateway_config_path}): `auth` must be none or api_key (absent = api_key), got \"${local.auth_mode}\"."
    }

    precondition {
      condition     = can(regex("^([0-9a-zA-Z][-]?){1,48}$", local.gateway_name))
      error_message = "The derived gateway name \"${local.gateway_name}\" must be letters/digits with single hyphens, max 48 (AgentCore's gateway name rule)."
    }

    # An empty audience pair = an authorizer that refuses every token.
    precondition {
      condition     = length(compact([var.entra_app_audience, var.entra_app_id])) > 0
      error_message = "The inbound Entra authorizer needs an audience: entra_app_audience and entra_app_id are both empty. The MCP record carries no Entra app identity — re-materialize the repo and retry."
    }
  }
}

resource "aws_bedrockagentcore_gateway_target" "api" {
  name               = "api"
  gateway_identifier = aws_bedrockagentcore_gateway.this.gateway_id

  target_configuration {
    mcp {
      open_api_schema {
        inline_payload {
          payload = file(var.openapi_spec_path)
        }
      }
    }
  }

  # Absent for `auth: none` — AgentCore's "No authorization" for OpenAPI targets.
  dynamic "credential_provider_configuration" {
    for_each = local.auth_mode == "api_key" ? [1] : []
    content {
      api_key {
        provider_arn              = aws_bedrockagentcore_api_key_credential_provider.this[0].credential_provider_arn
        credential_location       = local.api_key_location
        credential_parameter_name = local.api_key_parameter
        credential_prefix         = local.api_key_prefix != "" ? local.api_key_prefix : null
      }
    }
  }

  lifecycle {
    precondition {
      condition     = local.openapi_server_url != "" && local.openapi_server_url != local.placeholder_server_url
      error_message = "The OpenAPI spec (${var.openapi_spec_path}) must set servers[0].url to your API's real base URL — it is \"${local.openapi_server_url}\" (the template ships ${local.placeholder_server_url} as a placeholder)."
    }

    precondition {
      condition     = local.auth_mode != "api_key" || contains(["HEADER", "QUERY_PARAMETER"], local.api_key_location)
      error_message = "gateway.yaml (${var.gateway_config_path}): `api_key.location` must be HEADER or QUERY_PARAMETER, got \"${local.api_key_location}\"."
    }
  }
}
