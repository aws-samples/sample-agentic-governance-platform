# agentcore_harness

Platform-owned Terraform module that creates one AgentCore **Harness** — the
config-driven managed runtime. The platform (not the developer) applies this. It is
the infra half of the `harness-agentcore` template pair (design §3): agent template
`harness-agentcore` → infra template `agentcore-harness` (`iac_type =
"agentcore_harness"`, org repo `agp-harness-infra`).

Where `../agentcore_runtime` deploys a **container** the developer built, this module
deploys a **file** the developer wrote. There is no image, no ECR, no orchestration
code — `harness.yaml` *is* the deploy artifact, and this module turns it into one
`aws_bedrockagentcore_harness` plus the execution role that harness assumes.

## The contract: `harness.yaml`

`var.harness_config_path` points at the agent repo's `harness.yaml`, read with
`yamldecode(file(...))`. The key names are **pinned with the T6 scaffold** and are
consumed verbatim — renaming one on either side produces a deploy that silently
ignores the developer's value:

| key | required | maps to |
|---|---|---|
| `model_id` | **yes** | `model { bedrock_model_config { model_id } }` |
| `system_prompt` | **yes** | `system_prompt { text }` |
| `tools` | no (default `[]`) | one `tool { … agentcore_gateway { gateway_arn } }` block per entry |
| `max_iterations` | no | `max_iterations` (null ⇒ service default) |
| `timeout_seconds` | no | `timeout_seconds` (null ⇒ service default) |
| `max_tokens` | no, **not pinned** | `model { bedrock_model_config { max_tokens } }` — read opportunistically so a developer who adds it is not silently ignored |

`model_id` and `system_prompt` carry `precondition`s that name the file and the
missing key: a harness with no model or no instructions is a nonsense agent, and
failing at `plan` beats an AWS `ValidationException` three minutes into a build.

Both required keys are still read through `try(..., "")` rather than indexed
directly, and that is *why the preconditions work*. A bare
`local.harness_config["model_id"]` on a file missing the key fails inside the
`locals` block with Terraform's generic `Error: Invalid index` — which names neither
the file nor the key, and aborts before any precondition can run. Defaulting to `""`
moves the failure to the precondition that can say something useful. Verified by
running both cases.

**`tools` entries are AgentCore gateway ARNs — full ARNs, not gateway names.** Design §3
says "tools (gateway references)", and a Terraform list must be homogeneous, so a list of
ARN strings is the shape. Each becomes a `tool` block of `type = "agentcore_gateway"` (the provider's lower-case enum),
named from the ARN's last slash-segment so the console shows readable tool names.

A `precondition` enforces the ARN shape
(`arn:aws:bedrock-agentcore:<region>:<account-id>:gateway/<gateway-id>`) and its message
names `harness.yaml`, shows the expected form, and lists the offending entries. That
check is worth more than it looks: each entry is used **twice** — as a `gateway_arn` and
as an IAM policy `Resource` — and the IAM half is the one that fails badly. A bare
gateway name (the intuitive thing to write) produces
`MalformedPolicyDocument: Resource must be in ARN format` at apply, which names the
policy document and never `harness.yaml`, so the developer gets an error about a file
they have never seen. The pattern leaves the partition open (`aws`, `aws-us-gov`,
`aws-cn`) and does not police the gateway id's alphabet — refusing a legal ARN would be
worse than accepting an odd one AWS then rejects itself. Richer per-tool
forms the provider supports (`remote_mcp`, `inline_function`, `agentcore_browser`,
`agentcore_code_interpreter`) are deliberately **not** wired this epic — see
"Not built here".

The path must resolve **on the apply host at plan time**. The CodeBuild
`agentcore_harness` branch clones the *agent* repo alongside the infra repo for
exactly this reason (design §3: "the config build needs the agent repo where the
container build needed the image").

## State key convention

```
agentcore-harness/{agent_id}/{stage}/terraform.tfstate
```

Stage-scoped, mirroring the runtime module's layout (E28/T2). This module does **not**
configure its own backend — the backend/buildspec does, from this same convention —
so the key is echoed as the **`state_key` output** to make the two sides checkable
against each other rather than trusting that they agree.

That is also the only reason `agent_id` is an input: it names no AWS resource.

## Stage-scoped resource names (read before renaming anything)

Both **account-global** names are suffixed with `var.stage`, derived in ONE place
(`main.tf` `locals`):

```hcl
harness_name   = "${var.agent_name}_${var.stage}"                # AWS: [a-zA-Z][a-zA-Z0-9_]{0,39}, <= 40
exec_role_name = "${var.agent_name}-${var.stage}-harness-exec"   # IAM: <= 64
```

**Why.** A prod promote in the runtime module failed live with
`creating IAM Role (platform_agent-agentcore-exec): EntityAlreadyExists` — the state
key was stage-scoped, so prod got a fresh state, saw no role and tried to create one,
but IAM role names are account-global and dev already owned it. This module is born
with that fix rather than acquiring it.

- **`stage` is REQUIRED with no default.** A default would name prod's resources `dev`
  silently. Every stage is suffixed, **including `dev`**.
- **`-harness-exec`, not `-agentcore-exec`** — load-bearing. IAM role names are
  account-global and the same agent+stage may also own a container runtime (an agent
  can be re-materialized from a different template). Sharing the suffix would make the
  two modules fight over one role name, and the second to apply would fail with
  `EntityAlreadyExists`. **Note the consequence:** unlike the runtime module's role,
  this name is *not* the one the backend's `agentcore_exec_role_name()` derives, so
  the E23 delete cascade does **not** reclaim it today — see "Open items".
- **Underscore separator, same as the runtime module's — LIVEFIX.** This README used
  to claim the harness name takes a hyphen ("the harness's [API] does not [reject
  hyphens]… do not unify the two modules on one separator"). That was wrong: the real
  AWS harness-name constraint is `[a-zA-Z][a-zA-Z0-9_]{0,39}` (max 40 chars, NO
  hyphens), proven live when `e2e_harness_1003-dev` was rejected. The harness name now
  uses the same underscore separator and the same character class as the runtime
  module's name — the two names differ only in their length ceiling (40 vs. 48).
- **`agent_name` is capped at 32** (mirroring the backend's `AGENT_NAME_RE`), and both
  derived names carry `precondition`s so an over-long name fails at `plan` with a
  readable message. Because the harness ceiling (40) is tighter than the runtime's
  (48), a stage name that fits the runtime module can still overflow the harness one
  (32-char agent_name leaves only 7 chars for `_` + stage). No truncation is applied —
  deploy fails at `plan` with a message naming harness.yaml/the agent name, the same
  policy agentcore_runtime uses and for the same reason (truncation can collide two
  distinct agent+stage pairs on one AWS-global name).
- **The `_` join itself can collide two different agent+stage pairs, independent of
  truncation — same latent gap as the runtime module's.** Agent `a_b` + stage `c` and
  agent `a` + stage `b_c` both join to the literal name `a_b_c`; nothing here reserves
  the separator so it can only ever be read back one way. Not observed live (today's
  `AGENT_NAME_RE` naming conventions have not produced a real collision), but it is not
  structurally prevented either — flagged, not fixed, in this pass.

## Inbound Entra authorizer (E30A)

`authorizer_configuration.custom_jwt_authorizer` is set from `tenant_id`,
`entra_app_audience` and `entra_app_id` — the same inputs, discovery URL
(`https://login.microsoftonline.com/<tenant>/v2.0/.well-known/openid-configuration`) and
audience list (`compact([entra_app_audience, entra_app_id])`) as `../agentcore_runtime`.
The CodeBuild `agentcore_harness` branch passes all three and writes the harness ARN back
to the registry record (`agent_arns[stage]`, `agent_arn`, `identity_status`), exactly as
the runtime branch does. With the authorizer set the harness accepts **only**
`Authorization: Bearer` — SigV4 invokes are refused, so anything still invoking a harness
via boto3 stops working once this module is rolled out.

## Gateway tool outbound auth (E40)

A `tools` ARN that is a key of `gateway_tool_auth` gets
`outbound_auth { oauth { provider_arn = obo_provider_arn, scopes = [<map value>], grant_type = "TOKEN_EXCHANGE" } }`:
the harness exchanges the caller's Entra token (OBO) for one scoped to the MCP app
(`api://agp-mcp-<id>/.default`). Verified live (spike 2026-10-06) with the agent's
MicrosoftOauth2 provider. Unmapped ARNs, or `obo_provider_arn = ""`, keep today's tool
block (no `outbound_auth`, service default). Access is the MCP owner's grant: without the
agent→MCP consent the exchange fails at tool load, not at build.

The CodeBuild harness arm writes both inputs to `tool_auth.auto.tfvars.json` from
`GATEWAY_TOOL_AUTH` / `OBO_PROVIDER_ARN` (jq, so the JSON is data, never interpolated),
and refuses a stale module (no `gateway_tool_auth` variable) only when a provider and a
non-empty map would otherwise be discarded.

With at least one such tool the exec role also gets, exactly as the spike's denials named:
`bedrock-agentcore:GetResourceOauth2Token` on `token-vault/default`, the provider ARN,
`workload-identity-directory/default` and `…/workload-identity/harness_<harness_name>-*`;
and `secretsmanager:GetSecretValue` on
`secret:bedrock-agentcore-identity!default/oauth2/<provider name>-*`. No
`GetWorkloadAccessToken` (the service mints it). Needs provider ≥ 6.53 for `outbound_auth`.

## Execution role (self-provisioned, with override)

Created in-module (`aws_iam_role.exec`) under the module's own provider, so it lands
**in the tenant account** when `deploy_role_arn` is set. `exec_role_arn` is an optional
override with the same precedence as the runtime module's:

- **empty** (default) → the module creates and uses its own in-account role (the real
  cross-account tenant path);
- **non-empty** → the harness uses that ARN; the created role stays unused.

Policy, least-privilege and **config-shaped**:

- `bedrock:InvokeModel*` on two ARN forms, because `model_id` may be a bare
  foundation-model id **or** an inference profile id: `arn:aws:bedrock:*::foundation-model/*`
  and the deploy region + account's `inference-profile/*`. Not scoped to
  `local.model_id` itself — that value is a developer-editable string in another repo,
  so a typo would surface as an `AccessDenied` that reads like an outage instead of a
  validation error.

  **The foundation-model half is region-wildcarded on purpose.** The T6 scaffold's
  default `model_id` is a `us.` **cross-region** inference profile, which routes the
  invoke to foundation models in *other* regions; a deploy-region-only grant would
  `AccessDenied` the shipped default at invoke time, long after a green apply. The
  widening is cheap: foundation-model ARNs carry **no account id** (they name AWS's
  public model catalog), so this grants nothing of ours, and Bedrock still refuses any
  model the account has not been granted. The inference-profile half stays scoped to the
  deploy region and account, because that ARN *is* account-owned.
- CloudWatch logs on `*` (`CreateLogGroup` cannot be scoped to a group that does not
  exist yet) — the same call the runtime module makes.
- Gateway access **only when `tools` is non-empty**, scoped to exactly the ARNs
  `harness.yaml` names. A tool-less harness gets no gateway grant at all.
- **LIVEFIX: managed-memory data-plane access.** AWS auto-creates a managed memory
  alongside every harness (`harness.memory.managedMemoryConfiguration.arn`), and the
  harness's own invoke path reads/writes it directly — invoking `e2e_hrn2_1003_dev`
  failed live with `AccessDeniedException ... not authorized to perform:
  bedrock-agentcore:ListEvents` before this grant existed. Granted: the
  bedrock-agentcore data-plane memory operations (service model
  `bedrock-agentcore/2024-02-28`) that take a memoryId — `CreateEvent`, `GetEvent`,
  `ListEvents`, `ListSessions`, `GetMemoryRecord`, `ListMemoryRecords`,
  `RetrieveMemoryRecords` — scoped to `memory/${local.harness_name}-*` (a name-prefix
  pattern, because the id suffix is AWS-generated and unknowable at `plan`).
  `DeleteEvent`/`DeleteMemoryRecord` are in the same model but not granted — nothing
  observed at invoke time needs them.

**IAM propagation race:** `time_sleep.wait_for_exec_role` (20s) sits between role
creation and the harness so IAM propagates the role for
`bedrock-agentcore.amazonaws.com` before the harness validates it — the runtime module
hit exactly this `ValidationException` on first provision. Skipped (`count = 0`) when
an `exec_role_arn` override is supplied.

## Provider floor

`aws_bedrockagentcore_harness` ships in provider **≥ 6.32**. `versions.tf` keeps the
tree-wide `~> 6.0` pin (a tighter constraint here would diverge from every other
module), so the floor is documented rather than encoded — `terraform init -upgrade`
resolves the newest 6.x. `.terraform.lock.hcl` is git-ignored, so a workspace carrying
an older lock file fails `init` against the pin; the fix is always
`terraform init -upgrade`.

**Verified, not assumed:** the resource and every attribute/block used here were read
out of the real provider schema (`terraform providers schema -json`, aws **6.58.0**),
and `terraform validate` passes for this module.

## Preview caveat and the recorded CLI fallback

**The harness API and its Terraform coverage are preview-status. CDK is AWS's blessed
path.** This module is the recorded IaC bet, taken because the whole platform deploys
through Terraform and a second toolchain for one template would be worse than a
preview resource.

**Recorded fallback — a decision, not a drift (design §3):** if live testing finds the
`aws_bedrockagentcore_*` harness coverage falls short (a missing field, a broken
update path, an unusable import), the fallback is a **`null_resource` + AWS CLI
`create-harness` call inside this module** — same module, same state key, same exec
role, only the resource swapped. It is written down here so that taking it later is an
implementation of a recorded decision rather than an undocumented divergence. Two
things to know before taking it: a CLI-provisioned harness is invisible to Terraform's
diff (updates become a `triggers` map on the `null_resource`), and delete stops being
automatic.

Related risk the schema itself raises: several harness attributes are
`optional + computed` (`max_iterations`, `timeout_seconds`, `region`, `allowed_tools`,
`truncation`, `environment`). Optional+computed attributes are where preview providers
most often produce "inconsistent result after apply" — the runtime module hit the same
class with `lifecycle_configuration { max_lifetime }`, which is why it now sets that
value explicitly. If the live test reports a plan/apply inconsistency on one of these,
setting it explicitly is the first thing to try.

## No `ignore_changes` — deliberately

`harness.yaml` is the sole source of truth for the agent definition, so this resource
ignores nothing. That is the *opposite* of the runtime module, which must
`ignore_changes = [environment_variables]` because the platform injects Epic-7 MCP
grants into that map out-of-band. Nothing writes to a harness after apply today. If
something ever does, add the ignore **then**, and say what writes it — do not
pre-emptively blind this resource to drift.

## Deploy sequencing (same trap as the runtime module)

**The module in this repo is not the module that runs.** `../../runtime_module.tf` — the
same root-stack file that stages the runtime module, *not* a nonexistent
`agentcore_runtime_trigger` module — zips this directory and stages it at S3 key
`harness-module/agentcore_harness.zip` (`etag = output_md5`, so a `.tf` edit re-uploads
on the next apply). That zip is
consumed by the **template rollout**, which populates the per-org `agp-harness-infra`
repo; CodeBuild then **clones that repo** and applies whatever `.tf` it finds there.

So the flow is: **root `terraform apply`** (re-stages the zip) → **template rollout
with `overwrite=true`** (pushes it to `agp-harness-infra`) → **push an agent** → deploy
runs. Skip the rollout and every edit here is inert, silently — Terraform treats a
tfvar with no matching `variable` block as a *warning*, so the value is discarded and
the build goes green.

**Never let `.terraform.lock.hcl` into that zip.** Running `terraform init` in this
directory (to validate a change) leaves a lock file behind that is git-ignored, so
`git status` stays clean and nothing warns you. A lock written on a dev machine records
provider hashes for **that platform only** — a single `h1:` for e.g. darwin_arm64, no
`zh:` entries — so once staged and rolled out, CodeBuild's `terraform init` on
linux_amd64 fails the checksum comparison with an error that reads like a
provider/registry problem and points nowhere near here. Both `archive_file` blocks in
`../../runtime_module.tf` therefore exclude it **by name**: it is a *sibling file* of
`.terraform/`, not a file inside it, so excluding the directory does not exclude the
lock. Keep both lists in sync, and delete any stray lock from this directory.

## Not built here (on purpose)

- **No memory, skills, browser/code-interpreter/remote-MCP tools, or truncation
  config.** All available in the schema; none in the pinned contract.
- **No Langfuse wiring.** The harness has an `environment_variables` map, but nothing
  reads `LANGFUSE_*` inside a managed harness the way the container templates' `main.py`
  does, so setting them would be decoration.

## Open items for the live test

1. Does `CreateHarness` accept the values this module sends (`agentcore_gateway` tool
   type string, the model-config shape, the name pattern)? These come from the provider
   schema, which does not publish the API's accepted enum values.
2. Are the `bedrock-agentcore:InvokeGateway` / `GetGateway` / `ListGatewayTargets`
   action names the ones a harness actually needs to reach a gateway? IAM action names
   for a preview service are the least verifiable part of this module. A tool-less
   harness (`tools: []`) is unaffected and is the cheaper first test. Note that a
   *malformed* gateway reference is no longer part of this risk — it now fails at
   `plan` with a message naming `harness.yaml` (see "The contract" above).
3. Any "inconsistent result after apply" on the optional+computed attributes above.
4. Exec-role reclaim: `-harness-exec` is not in the E23 delete cascade's derivation.
5. **Not built / known gap:** AWS auto-creates a managed memory AND a managed runtime
   alongside every harness (visible as `harness.memory.managedMemoryConfiguration.arn`
   and `harness.environment.agentCoreRuntimeEnvironment.agentRuntimeArn` on
   `get-harness`), but neither is a resource this module provisions, so neither is
   reachable by the E23 delete cascade — deleting a harness today leaves its managed
   memory and managed runtime orphaned in the account.

## Inputs

| name | type | default | notes |
|---|---|---|---|
| `agent_id` | string | — | state-key segment only; names no resource |
| `agent_name` | string | — | name stem, `[a-zA-Z][a-zA-Z0-9_]{0,31}` |
| `stage` | string | — | **required**, suffixes both account-global names |
| `region` | string | — | harness region; also the module provider's region |
| `harness_config_path` | string | — | path to `harness.yaml` on the apply host |
| `gateway_tool_auth` | map(string) | `{}` | gateway ARN → OAuth scope; mapped `tools` get TOKEN_EXCHANGE |
| `obo_provider_arn` | string | `""` | agent's OBO credential provider; empty ⇒ no outbound auth |
| `exec_role_arn` | string | `""` | override; empty ⇒ module creates the role |
| `deploy_role_arn` | string | `""` | cross-account deploy role; empty ⇒ in-place |
| `tags` | map(string) | `{}` | applied to the harness and the exec role |

## Outputs

`harness_arn`, `harness_id`, `harness_name`, `exec_role_arn` (reports the role actually
in effect, override included), `state_key` (the documented convention).
