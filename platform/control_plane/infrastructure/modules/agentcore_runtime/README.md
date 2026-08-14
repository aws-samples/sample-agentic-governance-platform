# agentcore_runtime

Platform-owned Terraform module that creates one AgentCore **Runtime**. The
platform (not the developer) applies this. Inputs mirror the developer's
`agent.config.json` (Task 1) plus platform-owned network/role/tfvars written by
the CodeBuild branch (Task 5).

## IaC path: native resource

This module uses the **native `aws_bedrockagentcore_agent_runtime` resource**
(not the CloudFormation-wrapper fallback).

Decision procedure (brief Step 1): the `aws` provider pin was bumped
`~> 5.0` → `~> 6.0` across the root stack and the `api_gateway` and
`cloudfront` modules. `terraform init -upgrade` resolved
**aws v6.53.0** (≥ 6.52), and `aws_bedrockagentcore_agent_runtime` is present in
the provider schema. `terraform validate` passes for the bumped stack, so the
native path was taken and the pins were kept. The archived
`aws_cloudformation_stack` wrapper was the fallback only and is not used here.

## Deploy sequencing & provider floor (bit us — read before applying)

Two non-obvious operational facts for a fresh apply against a new account:

1. **Provider must be ≥ 6.53.** Beyond `aws_bedrockagentcore_agent_runtime` being in the schema, the
   `lifecycle_configuration { max_lifetime }` set on the runtime (commit `86317b0c`) only plans/applies
   consistently on aws provider **≥ 6.53**. `.terraform.lock.hcl` is git-ignored, so a workspace carrying an
   older lock file fails `terraform init` with a version-constraint error against the `~> 6.0` pin. Fix:
   **`terraform init -upgrade`** (resolves 6.54.x). Ensure CI/deploy runners can serve AWS ≥ 6.53.

2. **A module edit does NOT reach the pipeline at all until the module is ROLLED OUT to the org.**
   *(Corrected E28A/T1b — the previous wording said CodeBuild fetches the S3 zip. It does not, and that
   error is how finding #11 hid for a whole epic.)*

   What actually happens: `runtime_build_service.py` sends `GIT_INFRA_ORG`/`GIT_INFRA_REPO`, and
   `modules/codebuild/buildspec.yml` **git-clones the per-org `agp-runtime-infra` repo** into
   `/tmp/workspace`; the `agentcore_runtime` branch then applies whatever `.tf` it finds *there*. **The
   module in this repo is not the module that runs.** The S3 zip (staged by `../../runtime_module.tf` —
   *not* a nonexistent `agentcore_runtime_trigger` module — key `runtime-module/agentcore_runtime.zip`,
   `etag = output_md5`) is consumed only by the create-once **template rollout** that populates that org
   repo.

   So the flow is **root `terraform apply` (re-stages the zip) → template rollout with `overwrite=true`
   (pushes it to `agp-runtime-infra`) → push an agent → deploy runs**. Skip the rollout and every edit
   here is **inert**, silently: terraform treats a tfvar with no matching `variable` block as a *warning*,
   so the value is discarded, the build goes green, and nothing changed.

   `buildspec.yml` now carries a **drift guard** for exactly this: it fails the build when the cloned root
   declares no `variable "stage"`, turning a silent stale-module deploy into a loud one. `overwrite=true`
   is safe only while no org-side hand edits exist (true today: `agp-runtime-infra` has two bot commits
   and nothing else) — a customer-edited infra repo needs an update path first.

## Stage-scoped resource names (E28A/T1b — read before renaming anything)

Both of this module's **account-global** names are suffixed with `var.stage`, derived in ONE place
(`main.tf` `locals`):

```hcl
runtime_name   = "${var.agent_name}_${var.stage}"                  # AWS: [a-zA-Z][a-zA-Z0-9_]{0,47}
exec_role_name = "${var.agent_name}-${var.stage}-agentcore-exec"   # IAM: <= 64
```

**Why.** A prod promote failed live with
`creating IAM Role (platform_agent-agentcore-exec): EntityAlreadyExists`. E28/T2 stage-scoped the
Terraform *state key*, so prod gets a fresh state, sees no role, and tries to create one — but IAM role
names are account-global and dev already owned it. Prod deploys were structurally impossible on a
single-account tenant. `agent_runtime_name` is the same class, latent (`CreateAgentRuntime` declares
`ConflictException`; prod never got there because IAM failed first).

- **`stage` is REQUIRED and has NO default.** A default would name prod's resources `dev` silently.
  Every stage is suffixed, **including `dev`** — there is no magic default case.
- **Two different separators, deliberately.** `agent_runtime_name` allows **underscores only** (a hyphen
  is rejected by the API); the role name keeps the hyphenated `-agentcore-exec` idiom the backend's
  reclaim derives. Do not "unify" them.
- **Renaming FORCES REPLACEMENT of both.** `agent_runtime_name` carries
  `stringplanmodifier.RequiresReplace()` in the provider and `aws_iam_role.name` is destroy+create, so
  the first apply after this change **destroys and recreates the existing runtime and its ARN changes**.
  That is why the buildspec records the ARN in a per-stage map (`agent_arns[$STAGE]`, plus the scalar for
  back-compat) — a recorded ARN must be re-read, never assumed stable.
- **The replacement also STRIPS every granted agent's `MCP_SERVERS` env, and nothing re-injects it.**
  A replaced runtime is born with only the declarative `environment_variables` below;
  `ignore_changes = [environment_variables]` does not help, because the resource is *replaced*, not
  updated. The only writer of `MCP_SERVERS` is `agent_mcp_env.rebuild_runtime_mcp_env`, called **only**
  from the grant/revoke paths — no deploy or build-completion path calls it. So after the first
  post-rollout apply, every agent that had MCP grants comes back unable to reach its MCP servers, and
  **the deploy reports success**. It fails closed (no env → no tools → no unauthorized reach), so this
  is a governance divergence, not a security hole. **Recovery: re-grant (or revoke + re-grant) each
  affected agent's MCP servers** — add that step to the live test. The real fix is a deploy-path call to
  `rebuild_runtime_mcp_env`; the deploy path never enters the backend today, so that is a separate task.
- **`agent_name` is capped at 32**, not 48 (`variables.tf`, mirroring the backend's `AGENT_NAME_RE`). The
  role name was already `48 + len("-agentcore-exec") = 63` of IAM's 64, so at 48 *any* stage suffix
  overflowed. 32 fits both ceilings for stages up to 15 chars. Truncating was rejected: two long names
  sharing a prefix would collide silently — the same account-global failure being fixed.
- **`precondition`s on both resources** check the derived names (pattern + length), so an over-long name
  fails at `plan` with a readable message instead of at the AWS API mid-deploy.
- **`AGENT_NAME`** (the container env var feeding OTEL `service.name`) is derived from `runtime_name`, so
  it is stage-scoped too. Langfuse provisions one project *per agent*, so `service.name` is the only
  thing that separates dev from prod traces.

**Cross-file contract:** `exec_role_name` must stay byte-identical to `agentcore_exec_role_name()` in the
backend's `project_service.py`. Terraform *creates* the role; the E23 delete cascade is the only thing
that *reclaims* it. A drift raises nowhere — the teardown deletes a name that never existed, reports
success, and leaks an account-global name that then blocks re-materializing the same agent.

## Full-replace guard (load-bearing)

`UpdateAgentRuntime` is a full-replace PUT. The inbound Entra authorizer is now set
**declaratively at apply time** (born wired) from the tenant/app tfvars, so only the
grant-time governance env vars (Epic-7 MCP env injection) are still written *after*
this IaC applies. The ignore therefore covers `environment_variables` **alone**:

```hcl
lifecycle {
  ignore_changes = [environment_variables]
}
```

Never remove this — a re-apply would otherwise clobber the grant-time env the platform
manages out-of-band. (Corrected E28A/T1b: this section previously also listed
`authorizer_configuration`, which the resource stopped ignoring when the authorizer
became declarative.)

## Execution role (self-provisioned, with override)

The runtime needs an execution role the AgentCore service assumes to pull the
container image, write CloudWatch logs, and invoke Bedrock. This module now
**creates that role itself** (`aws_iam_role.exec`, trust + least-privilege
policy inline — there is no separate `agentcore_runtime_exec_role` module). Because the
role is created under the module's provider (which assumes `deploy_role_arn`
when set), it lands **in the tenant account** — so the runtime can pull the
tenant's *in-account* ECR image.

`exec_role_arn` is an **optional override**. Precedence:

- **empty `exec_role_arn`** (default) → the module creates and uses its own
  in-account exec role. This is the real **cross-account tenant** path.
- **non-empty `exec_role_arn`** → the runtime uses that ARN and ignores the
  created role. This preserves the **single-account** path: the Task 1 buildspec
  still passes `exec_role_arn="$EXEC_ROLE_ARN"` (the platform-account exec role),
  so a same-account deploy keeps working via the override.

ECR pull scoping: the three ECR pull actions are scoped to `var.ecr_repo_arn`
when set, else `*`. Nothing wires `ecr_repo_arn` today, so it defaults to `*` —
safe because an assumed tenant role can only reach its own account's ECR; a
later task can pass the real tenant repo ARN for tighter scoping.
`ecr:GetAuthorizationToken` stays on `*` (it does not accept a resource scope).

**IAM propagation race:** when the module self-provisions the role, a
`time_sleep.wait_for_exec_role` (20s) sits between role creation and the runtime
so IAM propagates the role for `bedrock-agentcore.amazonaws.com` before the
runtime validates it — otherwise first provision fails with a `ValidationException`.
It is skipped (`count = 0`) when an `exec_role_arn` override is supplied (that role
pre-exists).

**Live-test note:** single-account currently exercises the **override** path
(buildspec passes the platform exec role). A real tenant deploy passes an empty
`exec_role_arn` to force in-account role creation.

## Network mode (configurable)

`network_mode` selects the runtime's networking, defaulting to **`PUBLIC`** — the
proven pattern every working AgentCore runtime in the target account uses. It
needs no subnets or security groups, so `vpc_subnet_ids` / `security_group_ids`
can be left empty (they default to `[]`).

Set `network_mode = "VPC"` to attach the runtime to platform-owned networking. In
that mode `vpc_subnet_ids` must be non-empty (enforced by a `precondition`) and
`security_group_ids` is passed alongside it; both feed the resource's
`network_configuration.network_mode_config` block, which is rendered only for VPC
mode via a `dynamic` block. In PUBLIC mode that nested block is omitted entirely.

```hcl
# PUBLIC (default) — no networking inputs required
module "runtime" {
  # network_mode = "PUBLIC"  # implied
}

# VPC — opt-in, requires subnets (SGs recommended)
module "runtime" {
  network_mode       = "VPC"
  vpc_subnet_ids     = ["subnet-…"]
  security_group_ids = ["sg-…"]
}
```

## Notes

- Network mode defaults to **PUBLIC**; `vpc_subnet_ids` / `security_group_ids`
  are only consumed when `network_mode = "VPC"`.
- `exec_role_arn` is now an **optional override**; empty makes the module create
  its own in-account exec role (see "Execution role" above).
- `lifecycle_idle_timeout_seconds` maps to
  `lifecycle_configuration.idle_runtime_session_timeout`.
- `otel_enabled` is passed to the container as the `OTEL_ENABLED` env var
  (part of the initial `environment_variables` map, which is then ignored on
  subsequent applies per the guard above).
- `stage` is **required** and suffixes both account-global resource names — see
  "Stage-scoped resource names" above.
- Outputs: `agent_runtime_arn`, `agent_runtime_id`. (The native resource has no
  bare `id` attribute — `agent_runtime_id` is the canonical id.)
