# AgentCore Harness Agent Template (preview)

A config-driven agent on the **Bedrock AgentCore harness**: a managed runtime where an agent is
*parameters*, not a program. There is no framework code here, no container, and nothing to build.
Edit `harness.yaml`, push to the trunk, and the platform applies it.

> **Preview.** The harness itself is an AWS preview feature, and so is the platform's Terraform
> coverage of it. Read [Preview status — what can bite you](#preview-status--what-can-bite-you)
> before you build anything load-bearing on this template.

## What It Does

Whatever `harness.yaml` says. Out of the box: a single conversational agent on Claude Sonnet with
no tools, a 10-iteration reasoning ceiling, and a 300-second invocation timeout.

## The whole agent is one file

`harness.yaml` **is the deploy artifact**. Five keys, and the key names are a contract — the
platform's Terraform module reads this file with `yamldecode(file(...))` and consumes them
verbatim, so renaming, nesting, or dropping one does not fall back to a default, it fails the
apply. Extra keys of your own are ignored, harmlessly.

| Key | What it is |
| --- | --- |
| `model_id` | The Bedrock model (an inference-profile id, e.g. `us.anthropic.claude-sonnet-4-6`). Must be enabled in the target account/region. |
| `system_prompt` | The agent's standing instructions. This is the whole of its behaviour. |
| `tools` | AgentCore Gateway **ARNs** — full ARN strings, not names. `[]` is a valid, working agent. |
| `max_iterations` | Reasoning-loop ceiling per invocation — a cost/latency bound, not a quality dial. |
| `timeout_seconds` | Wall-clock ceiling per invocation. Should expire *before* your caller's own timeout. |

The file's comments carry the same guidance inline; keep them when you edit, they are the
documentation a future reader of your repo will actually read.

## Quick Start

```bash
# 1. Edit the agent.
$EDITOR harness.yaml

# 2. Ship it.
git commit -am "Teach the agent about our refund policy"
git push origin main
```

There is no local run. A harness has no container and no `localhost:8080` — the runtime is
AWS-managed, so the only way to exercise a change is to deploy it to dev and then use *Test invoke*
in AGP (or the AgentCore console against the `harness_arn` the build prints at the end of its log).

**A harness agent is a first-class AGP agent once its first build lands (E30A).** The build
applies a real `aws_bedrockagentcore_harness` in the target account and writes the harness ARN back
to the agent's record, exactly as a container build writes a runtime ARN. From that moment the agent
is `provisioned`, and every governance surface keys off the ARN rather than off a container:

- **Invoke.** The agent page's *Test invoke* panel reaches the harness through AGP: your Entra
  sign-in is exchanged for a token in the agent's own audience and presented to the harness's
  inbound JWT authorizer, which the build configures from the same Entra inputs the container
  runtimes use. A user who is not assigned to the agent is refused with *"<email> is not assigned
  to this agent"* until an operator grants them on the Access tab.
- **Access grants.** Invoker and Admin grants work as for any agent; they are Entra app-role
  assignments on the agent's service principal, evaluated at every invoke.
- **The governance graph** shows the agent with its access and MCP edges.
- **MCP servers.** *Grant MCP access* on the agent's MCP Servers tab gives the agent's identity the
  consent it needs to call a governed gateway. Note the harness-specific half: **which** gateways
  the agent can call is the `tools:` list in `harness.yaml`, fixed at build time. Adding a gateway
  is a commit and a build; granting access is a click. Both are needed, in that order, and a harness
  that lists a gateway it has no grant for is refused on every invoke until the grant exists.
- **Build now.** A config repo has no image to wait for, so the repository page offers *Build now*
  to redeploy the current `main` without a commit.

**Traces.** A harness traces to CloudWatch only (the `aws/spans` log group plus its own runtime log
group). It cannot export to Langfuse, so the Governance *Traces* tab, which reads Langfuse, stays
empty for a harness; use the repository's *Observability* tab in Ops, which reads CloudWatch, and the
*Evaluations* tab, whose judges read the same spans.

Before pushing, it is worth confirming the file still parses:

```bash
python -c "import yaml; print(yaml.safe_load(open('harness.yaml')))"
```

## Adding tools

A tool is **not** code you write in this repo — there is no code in this repo. Register the tool as
an **AgentCore Gateway** target, then list that gateway's **ARN**:

```yaml
tools:
  - arn:aws:bedrock-agentcore:<region>:<account-id>:gateway/<gateway-id>
```

**Each entry must be a full gateway ARN, not a name.** The platform's Terraform module feeds every
entry through verbatim — into the harness's `tool { agentcore_gateway { gateway_arn = … } }` block
*and* into the `Resource` list of the execution role's IAM policy. A bare name is not an ARN, so IAM
rejects the policy and the deploy fails with `MalformedPolicyDocument`. The list is also what scopes
the grant: the exec role is allowed to reach exactly the gateways named here and nothing else, which
is why the ARNs cannot be loosened to names or wildcards.

Region and account id come from wherever the agent is deployed — read the ARN off the gateway in the
console or from `aws bedrock-agentcore list-gateways`. Do not paste an account id from another
environment; the same agent deploys to dev and prod accounts.

Anything you name must exist and be reachable by the harness's execution role *before* the deploy
that references it, or the apply fails on an unresolvable target.

Richer per-tool forms the provider supports (`remote_mcp`, `inline_function`, `agentcore_browser`,
`agentcore_code_interpreter`) are **not wired yet** — a gateway ARN is the only tool shape this
template accepts today.

## Evaluations

AGP evaluates this agent against a golden dataset that lives **in this repo**. You can edit the
two files by hand or through the platform (Repo → Evaluations), which commits them to `main` and
starts a dev build. A run invokes every item against the **dev** agent, scores it, and a passing
run for the current build is what lets an Owner promote it to prod (or override, with a recorded
reason).

`evals/evals.yaml` sets the checks, the judges and the gate:

```yaml
version: 1
dataset: evals/dataset.jsonl
checks:
  tools: in_order        # exact | in_order | any_order | off   (tool names)
  args: subset           # exact | subset | off                  (tool arguments)
  no_error: true         # the invoke must succeed
judges:                  # AgentCore Evaluations built-in ids; [] = checks only
  - Builtin.GoalSuccessRate
  - Builtin.Correctness
gate:
  checks: all_pass       # every item must pass every enabled check
  judge_min_mean: 0.8    # every judge's mean score across items must be >= this
```

`evals/dataset.jsonl` has one item per line and ships empty. Only `id` and `input` are required:

```json
{"id":"fnol-001","input":"My car was hit on the A40, policy P-1001","expected_output":"A claim is opened ...","assertions":["Opens a claim","Quotes the claim number"],"expected_tools":[{"name":"get_policy","args":{"policy_id":"P-1001"}},{"name":"open_claim"}],"source_trace_id":null,"tags":["happy-path"]}
```

| Field | Used by |
|---|---|
| `expected_tools[].name` | the `tools` check (exact = same sequence; in_order = these appear in this order, extras allowed; any_order = all present) |
| `expected_tools[].args` | the `args` check. Subset = every listed key equals the actual value; exact = equal dicts. Omitting `args` skips argument checking for that call |
| `expected_output` | `Builtin.Correctness` (reference answer) |
| `assertions` | `Builtin.GoalSuccessRate` |

**Where the scores come from.** The harness traces to CloudWatch natively, so the judges read
its spans from there, and the tool checks read the tool calls from the harness event stream.
There is nothing to wire in this repo.

## Project Structure

```
harness.yaml                     # The agent. The deploy artifact.
template.json                    # Catalog metadata (framework: harness, preview: true)
evals/evals.yaml                 # Evaluation checks, judges and gate (see "Evaluations")
evals/dataset.jsonl              # Golden dataset, one item per line (ships empty)
.github/workflows/deploy.yml     # Trunk push → OIDC → POST /builds/runtime. No build legs.
```

No `Dockerfile`, no `requirements.txt`, no `src/` — by design, not by omission. A config-driven
agent has no image to build and no dependencies to install, so adding any of those would create a
second, unread source of truth beside `harness.yaml`.

## Infra, deploy, and registration are platform-owned

This repo is **just the agent definition**. You do not write Terraform, deploy scripts, or register
the agent yourself — the platform owns provisioning, IAM, the harness resources, and agent
registration. Its Terraform state for this agent is keyed
`agentcore-harness/{agent_id}/{stage}/terraform.tfstate`.

**Per-agent settings are NOT in this repo.** `agent_name` and `framework` live on your agent's
record in the platform's registry, which is where the build reads them from. There is deliberately
no `agent.config.json` here: a copy of governed state in the repo is a copy that can disagree with
the record. Change those values in the platform, not in a file.

## CI/CD — dev deploy here, prod promotion in the platform (`.github/workflows/deploy.yml`)

**The rule: a push to this repo's trunk deploys dev. Nothing else deploys anything.** A push to any
other branch runs **nothing** — open a PR into the trunk. (`deploy.yml`'s `push.branches` names the
trunk literally; if your project's trunk is not `main`, that is the one line to edit.)

The narrowness is deliberate: there is exactly **one dev harness per agent**, so if every feature
branch deployed, N branches would race to overwrite it, last-push-wins.

**Two pushes to the trunk genuinely serialize — LIVEFIX, E30 dev deploy.** `concurrency` keyed on
the ref queues a second WORKFLOW RUN, but that alone used to be cosmetic: this workflow has one
job, so the run is "done" the instant its `fetch` returns, seconds after AGP answers 202 — long
before CodeBuild's async `terraform apply` on the one state per agent actually finishes. A race on
that lock leaves the loser failed while the winner applies stale config — with the platform's own
record claiming success, exactly what happened live. The real fix is AGP's: `POST /builds/runtime`
now answers 409 while a build for this agent+stage is in flight, and the `trigger` job polls on
that 409 until it clears (bounded, so a stuck prior build fails the job instead of hanging
forever). `concurrency` still matters — it stops two trigger jobs for the same ref from polling
AGP at once — but the 409 retry loop is what makes the applies serialize. Never cancel either; an
apply killed halfway abandons the lock and half-applies the harness.

The workflow is a **single job** with a single credential (`id-token: write`). It mints a GitHub
OIDC token (audience `agp-runtime-build`) and `POST`s to `${AGP_API_URL}/builds/runtime` with
`stage: dev`. It assumes **no AWS role**: there is no registry to push to, and the deploy
credential is the platform's, never this repository's.

### What identifies a deploy here: the commit SHA

The container templates POST an image **digest**, because the thing they deploy is a mutable-tagged
blob in a registry. Here the artifact is a **file in git**, so the commit SHA plays that role: it
names the exact bytes of `harness.yaml` that were applied, and it is what the platform records and
what a later production approval attests to.

Mechanically, the SHA rides in the `image_tag` field of the existing `POST /builds/runtime` body —
one request contract serves both build kinds, and `image_digest` / `ecr_repo` go out empty because
no image exists. That the field is named for an image is history, not meaning.

The platform resolves the build kind and the IaC type from **your agent's template chain**, never
from anything this workflow sends. A repo cannot talk itself into a different deploy path.

**No ECR variables are set on this repo, and that is correct.** The materialize step skips them for
a config build; their absence is not a step that failed.

### Trunk push → a candidate; an Owner approves in AGP

- **Push to the trunk → tenant dev account.** The trigger job POSTs `stage=dev` and the platform
  applies that revision to the dev harness.
- **Production is never a side effect of a merge.** Landing on the trunk *nominates* a revision; a
  project **Owner** approves it in AGP, which applies those exact bytes in the prod account. There
  is **no prod path in this workflow at all**, and the platform refuses a prod deploy asked for
  over the GitHub OIDC audience.

`[skip ci]` works normally — GitHub honours it on `push`, and nothing here reaches around the
commit message (no `workflow_dispatch`, no message-blind trigger). The platform's setup commit
carries it, which is why materializing this repo does not burn a deploy.

## Preview status — what can bite you

**The harness Terraform coverage is preview-status.** The `aws_bedrockagentcore_*` resources ship
in the AWS provider (≥ 6.32), but the coverage is low-level and moving, and **CDK is currently
AWS's blessed path** for the harness. Concretely, expect:

- **Resource gaps.** A harness capability that exists in the API may have no Terraform resource or
  attribute yet. If the platform hits one, the recorded fallback is a `null_resource` +
  AWS CLI `create-harness` call inside the infra module — **a decision recorded in the platform's
  `modules/agentcore_harness` README, not a drift to discover later.**
- **Churn between provider versions.** A preview resource's schema can change in a minor provider
  bump in ways a stable resource would not.
- **Fewer knobs than the container path.** Anything not expressible in `harness.yaml`'s five keys
  is not configurable here yet.

**Choose accordingly.** For a demo, a conversational assistant, or an agent whose behaviour is a
prompt plus registered Gateway tools, this template is the shortest path there is — no image, no
dependency management, no CVE surface of your own. For anything that needs custom orchestration,
its own libraries, or a stable IaC surface today, start from a **container** template
(`strands-agentcore`, `langgraph-agentcore`, `crewai-agentcore`) instead. Moving from harness to
container later means writing the agent as code, not editing config — plan for it if it is likely.
