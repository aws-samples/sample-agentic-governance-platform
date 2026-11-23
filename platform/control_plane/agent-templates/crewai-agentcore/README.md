# CrewAI + AgentCore Agent Template

A barebones single-crew CrewAI agent, ready to deploy to Bedrock AgentCore Runtime. Build your
agent from this scaffold: edit the crew's role/goal/backstory and add tools in `src/main.py`, and
let the platform handle infra, deploy, and registration.

## What It Does

One agent, one task, one crew. It answers the incoming prompt in plain prose — the smallest thing
that is a real CrewAI agent rather than a sketch. Growing it into a multi-agent crew is the
customer's design decision, not something the scaffold should pre-make (see "Adding a second
agent" below).

## Tech Stack

- **CrewAI** (`crewai >= 1.15, < 2.0`)
- **Bedrock AgentCore Runtime** (`bedrock-agentcore`)
- **Claude Sonnet** on Amazon Bedrock, via CrewAI's LiteLLM layer
  (`bedrock/us.anthropic.claude-sonnet-4-6`)
- **Langfuse** tracing over OTLP (`opentelemetry-instrumentation-crewai` + the HTTP OTLP exporter)
- Python 3.11+

## Quick Start

```bash
# Install dependencies (with dev extras for tests + lint)
pip install -c constraints.txt -e ".[dev]"

# Set environment variables (or copy .env.example)
export MODEL_ID=us.anthropic.claude-sonnet-4-6
export AWS_REGION=us-east-1

# Run locally
python -m src.main
```

The agent starts on `http://localhost:8080` (AgentCore default port), serving
`POST /invocations` and `GET /ping`.

The `Dockerfile` and the CI quality job install the versions pinned in `constraints.txt`, so
after editing `pyproject.toml` regenerate that file from this directory and commit both. The
compile needs `uv`:

```bash
uv pip compile pyproject.toml --universal --python-version 3.11 --all-extras --no-emit-package setuptools --no-emit-package pip --no-emit-package wheel -o constraints.txt
```

### Test the running agent

```bash
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Summarise what a control plane is, in two sentences."}'
```

### Run the unit tests

```bash
python -m pytest tests/ -v
ruff check src tests
```

Every test runs offline — no AWS credentials, no Bedrock call, no Secrets Manager call. The crew
builder is asserted for its shape, the entrypoint is driven with `build_crew` stubbed, and the
telemetry setup is exercised with `boto3` stubbed.

## Response contract

`POST /invocations` answers with a **single JSON object**:

```json
{"result": "<the answer>", "tool_calls": [{"name": "<tool>", "args": {}}]}
```

`tool_calls` lists the tools this invoke called, in order (`[]` if none, which is always the case
for the tool-less starter), and is read by the evaluation tool checks (see "Evaluations"). It is
collected from CrewAI's `ToolUsageStartedEvent` for this invoke's task.

Errors use the same shape with one key: `{"error": "<message>"}`. A missing or empty `prompt` is
an error, not an empty answer.

**This is buffered, not streamed — and it differs from the Strands template on purpose.**
`crew.kickoff()` is synchronous: it returns when the whole crew has finished, so there are no
incremental tokens to stream. Wrapping that in SSE would emit a single chunk and add a second
contract for a client to get wrong.

### What the caller actually receives

Worth being precise about, because it is easy to assume otherwise: because this agent answers with
`application/json` rather than `text/event-stream`, **the platform forwards the body verbatim — it
does not unwrap it.** `POST /agents/{id}/invoke` returns:

```json
{"response": {"result": "<the answer>"}}
```

The platform's invoke route buffers-and-extracts (pulling the text out of `result`) only on its
**SSE** branch, which is the path the Strands template takes. On the JSON branch it returns
`{"response": <your body>}` unchanged. So a caller reads `response.result` for the answer, and the
governance UI renders whatever this object contains.

Three rules if you edit the entrypoint:

1. **Keep the key name `result`.** It is the catalog's convention for a buffered template — the
   LangGraph template answers in the same shape, and it is the key the platform's extractor looks
   for on the SSE path, so an agent that later moves to streaming keeps working. Renaming it to
   `output`/`answer` still returns 200, and the answer then arrives nested under a key nothing
   expects. That is a breaking change for every agent already materialized from this template.
2. **Return `result.raw`, never the `CrewOutput` itself.** `kickoff()` returns a `CrewOutput`
   (raw text plus token usage and per-task outputs) which is not JSON-serialisable — returning it
   whole fails at the serialiser *after* the model spend, so you pay for the call and get no
   answer. `tests/test_agent.py` pins this.
3. **Keep the value a plain string.** Nothing downstream flattens it, so a list or dict under
   `result` reaches the UI as a raw JSON blob instead of prose.

## Observability — Langfuse, wired with no manual setup

Every agent provisioned from this template is traced. The platform provisions one Langfuse project
and key **per agent** and injects two non-secret values into the runtime environment:

| Variable | What it holds |
|---|---|
| `LANGFUSE_HOST` | The Langfuse base URL. Non-secret. |
| `LANGFUSE_SECRET_NAME` | The **name** of a Secrets Manager secret holding `{public_key, secret_key}` — never the key values themselves. |

`src/main.py` reads that pair from Secrets Manager at import and points an OTLP exporter at the
project. Attribution is therefore **structural**: each agent authenticates with its own project
key, so its traces land in its own project. The key is never a hardcoded literal, the secret value
lives only in Secrets Manager, and it is never logged (the base64 auth header *is* the secret).

**Graceful degrade is the rule.** If the two variables are unset, or the secret is unreadable, or
the secret lacks the key pair, or the instrumentation fails to import — telemetry is simply not
wired and the agent runs normally, with one `INFO`/`WARNING` line saying which. An agent with no
traces is degraded; an agent that will not start is *down*, and this ordering is deliberate: the
whole block runs at module import, so an unguarded fault there crash-loops the runtime rather than
losing a span. `tests/test_agent.py` covers the unset, unreadable, and incomplete-secret paths.

Three details worth knowing before you edit that block:

1. **`DISABLE_ADOT_OBSERVABILITY=true` is set only once Langfuse is configured.** AgentCore
   auto-instruments with ADOT and claims the global OTEL TracerProvider first, which is set-once —
   disabling it is what lets the Langfuse exporter win.
2. **A real `TracerProvider` is built explicitly.** Unlike the Strands template (whose
   `strands-agents[otel]` extra wires one), nothing in the CrewAI stack installs a provider, and
   ADOT's has just been disabled — so without this the global provider stays the no-op default and
   spans are dropped silently.
3. **The endpoint is the BASE form** (`{host}/api/public/otel`). The HTTP exporter appends
   `/v1/traces` itself, and Langfuse accepts `http/protobuf` or `http/json` but **not** gRPC.

Spans are force-flushed in the entrypoint's `finally:` block, because the AgentCore microVM freezes
between invocations and the background `BatchSpanProcessor` would otherwise lose whatever it had
buffered.

The flush targets the provider this module built, held in a module-level reference — **not**
`get_tracer_provider()`, and there is deliberately no fallback to it. Two distinct reasons:

- **Unwired, the global is a `ProxyTracerProvider`, which has no `force_flush` at all.** Calling it
  raises `AttributeError` on *every* invocation and logs "buffered spans may be lost" — an alarming
  warning about spans that were never produced. So the `None` check guards the whole block: nothing
  wired means nothing to flush and nothing to warn about.
- **Wired, the global may not be ours.** `set_tracer_provider` is set-once, so if anything claimed
  it first our call is a silent no-op, while the spans still reach our provider because it is passed
  to `instrument()` explicitly. Flushing the global there would flush a provider holding none of our
  spans, and the buffered Langfuse spans would die with the microVM.

Separately, the template sets `CREWAI_DISABLE_TELEMETRY=true`. That is a different concern and
neither setting replaces the other: the block above ships traces to the platform's own Langfuse
project, while this one stops CrewAI's opt-**out** client from exporting usage data to CrewAI Inc —
a third party nobody in the platform's trust chain reviewed.

**It is the first thing `src/main.py` does, and the position is load-bearing.** CrewAI reads the
flag when `crewai.telemetry` is imported, and the Langfuse instrumentor above imports `crewai`
itself in order to patch it — so the flag has to be set before *that*, not merely before the
explicit `from crewai import ...` further down. Set it too late and the failure is invisible from
the outside: the variable reads `true` while CrewAI's telemetry is already initialised, its signal
handlers installed, and its span processor attached to your own Langfuse provider. And because the
instrumentor only runs when Langfuse is configured, that misfires **only in production**.

The tests assert `Telemetry().ready is False`, not the env string — the string is set correctly in
the broken ordering too, so it cannot catch this. One of them re-imports the module in a subprocess
with Langfuse configured, since an import-order defect cannot be observed in an interpreter that
has already done the import.

CloudWatch logs and AgentCore's own runtime metrics work normally regardless.

## What you can change

**Behaviour lives in `src/main.py`.** CrewAI builds its prompt from the ROLE / GOAL / BACKSTORY
triple plus the task's `expected_output` — those four strings are the agent's personality and
they are constants at the top of the file. That is the first thing to edit.

**Per-agent settings are NOT in this repo.** `agent_name`, `framework` and `model_id` live on
your agent's record in the platform's registry, which is where the build reads them from. There is
deliberately no `agent.config.json` here: a copy of that state in the repo was a copy that could
disagree with the governed record, and the runtime never read it. Change those values in the
platform, not in a file.

Environment variables (`MODEL_ID` / `AWS_REGION` / `LOG_LEVEL`, see `.env.example`) configure the
container **when you run it locally**.

Note `MODEL_ID` holds the **bare** Bedrock id, matching what the registry injects. CrewAI routes
through LiteLLM and needs a `<provider>/<model>` string, so `src/config.py` prepends `bedrock/`
(idempotently). Keep that split: the injected env value stays framework-neutral.

## Adding tools

```python
from crewai.tools import tool


@tool("Search the knowledge base")
def search_kb(query: str) -> str:
    """Look up an answer in the knowledge base."""
    return f"Result for {query}"


# Pass it to the Agent inside build_crew()
agent = Agent(..., tools=[search_kb])
```

## Adding a second agent

Append it to the crew and give it its own `Task`. CrewAI runs tasks sequentially by default and
passes each task's output forward as context:

```python
reviewer = Agent(role="Reviewer", goal="...", backstory="...", llm=llm)
review = Task(description="Check the draft above.", expected_output="...", agent=reviewer)
return Crew(agents=[agent, reviewer], tasks=[task, review])
```

Two things to change with it: `allow_delegation=False` is set because a lone agent has nobody to
delegate to, so reconsider it once there is a second; and `build_crew` returns a fresh `Crew` per
invocation, which must stay that way (a `Task` carries the request, so a shared crew would answer
the second caller with the first caller's task).

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

**Where the scores come from.** The tool checks read the `tool_calls` this agent returns (see
"Response contract"); the judges read this agent's spans from CloudWatch. `src/main.py` therefore
exports every span to **both** Langfuse (when configured) and CloudWatch: a second span processor
sends them to the X-Ray OTLP endpoint (`https://xray.{AWS_REGION}.amazonaws.com/v1/traces`),
signed with SigV4 from the runtime role, and tags each span with the AgentCore session id as
`session.id`. Set `AGP_CLOUDWATCH_TRACES=false` to turn the CloudWatch export off; if it cannot be
set up, the agent logs a warning and runs without it.

## Project Structure

```
src/
├── __init__.py
├── config.py      # Environment-variable configuration (+ the LiteLLM model prefix)
└── main.py        # Langfuse wiring, crew definition, builder, and AgentCore entrypoint
tests/
└── test_agent.py  # Unit tests for config, telemetry setup, the crew builder, and the contract
evals/
├── evals.yaml     # Evaluation checks, judges and gate (see "Evaluations")
└── dataset.jsonl  # Golden dataset, one item per line (ships empty)
Dockerfile         # linux/arm64 container image (CMD: python -m src.main)
```

## Infra, deploy, and registration are platform-owned

This repo is **just the agent scaffold**. You do not write Terraform, deploy scripts, or register
the agent yourself — the platform owns runtime provisioning, ECR, IAM, the inbound authorizer, and
agent registration. **This repo's CI only builds and pushes the container image**; the platform
deploys it from there.

## CI/CD — dev build here, prod promotion in the platform (`.github/workflows/build.yml`)

**The rule: a push to this repo's trunk builds and deploys dev. Nothing else deploys anything.**
A pull request runs `quality` only; a push to any other branch runs **nothing**, so open a PR into
the trunk to get CI on your work. (`build.yml`'s `push.branches` list names the trunk literally —
if your project's trunk is not `main`, that is the one line to edit.)

The narrowness is deliberate: there is exactly **one dev runtime per agent**, so if every feature
branch deployed, N branches would race to overwrite the same deployment, last-push-wins.

**Two pushes to the trunk genuinely serialize — LIVEFIX, E30 dev deploy.** `concurrency`
keyed on the ref queues a second WORKFLOW RUN, but that alone used to be cosmetic: the run is
"done" the instant the `trigger` job's `fetch` returns, seconds after AGP answers 202 — long
before CodeBuild's async `terraform apply` on the one state per agent actually finishes. Two
builds 11 seconds apart once raced that lock — the loser failed, the winner deployed a stale
image, and the platform's own record claimed success. The real fix is AGP's: `POST
/builds/runtime` now answers 409 while a build for this agent+stage is in flight, and the
`trigger` job polls on that 409 until it clears (bounded, so a stuck prior build fails the job
instead of hanging forever). `concurrency` still matters — it stops two trigger jobs for the same
ref from polling AGP at once — but the 409 retry loop is what makes the applies serialize. Never
cancel either: a deploy already in flight must be allowed to finish, since killing it halfway
abandons the lock and half-applies the runtime.

Beyond that, no methodology is imposed. Landing on the trunk **nominates** an image for
production; the prod deploy itself is a gated action in the platform, never a side effect of a
merge. **There is no prod path in this workflow at all.**

- **`quality`** (every PR + every trunk push): `pip install -c constraints.txt -e ".[dev]"` → `ruff check src tests`
  → `pytest tests/ -v` → `pip-audit`. Branch-agnostic, no environment.
- **`build`** (trunk push, after `quality`): assumes an **ECR-push-only** OIDC role, then
  `docker buildx build --platform linux/arm64 -t <ECR_REPOSITORY>:<AGENT_ID>-<tree_sha> --push .`,
  and resolves the **digest of what it just pushed**.
- **`trigger`** (after `build`): mints a **GitHub OIDC token** (audience `agp-runtime-build`) and
  `POST`s it to `${AGP_API_URL}/builds/runtime` with the tag, the **digest**, and `stage: dev`.

`[skip ci]` works normally — GitHub honours it on `push`, and nothing here reaches around the
commit message (no `workflow_dispatch`, no message-blind trigger). The platform's own setup commit
carries it, which is why materializing this repo does not burn a build.

### What identifies the image: a DIGEST, not a tag

The tag is `<AGENT_ID>-<tree_sha[:7]>`, from `git rev-parse HEAD^{tree}` — the sha of the
repository *content*. It is a convenient, human-readable label, and it is **not** what the
platform deploys.

A tag is a mutable pointer. The registry keeps tags mutable and this build is not
byte-reproducible (the dependencies install from the pins in `constraints.txt`, but the
`python:3.11-slim` base tag floats), so the bytes behind one tag can differ between the
moment something is approved and the moment production deploys it. **The digest names the bytes
themselves**, which is the only value an approval can honestly attest to.

The digest is read **from the push itself** (buildx's own `--metadata-file`), not from a later
registry lookup. Two reasons, both load-bearing: a read-back of the tag may already have been
overwritten by a concurrent push — the exact class of bug the digest closes — and it needs no ECR
read grant, so the push-only role does not have to be widened.

**If the digest cannot be resolved, the build FAILS.** Empty and malformed collapse into the same
refusal on purpose: anything that is not a `sha256:` reference means this build cannot name what it
pushed, and reporting a deploy for an unnamed image is precisely how a stale runtime gets recorded
as a success. A red job is strictly cheaper than that.

### Trunk push → a candidate; an Owner approves in AGP

- **Push to the trunk → tenant dev account.** `build` + `trigger` run automatically and POST
  `stage=dev`. The platform deploys that digest to the dev runtime.
- **The same POST registers the prod candidate**, and only from the trunk. The platform reads the
  ref from the **OIDC token** — so it is cryptographically proven, not self-reported — and compares
  it against the project's configured trunk branch. A push to any other branch registers nothing,
  which is what keeps an unreviewed branch from nominating itself for production. A newer trunk
  push **replaces** the candidate; there is one candidate, so there is no queue.
- **Prod deploy → tenant prod account** is **an action in the platform, not in this repo.** A
  project **Owner** approves the pending candidate in AGP
  (`POST /projects/{id}/repos/{repo_id}/promote`); the platform authorizes the caller against the
  project's roles and deploys the **approved digest verbatim** — it does not re-resolve the tag,
  because re-resolving is what would reopen the window the digest closes. **No tag and no digest is
  ever entered by an operator, and no rebuild happens.**

  Note where this workflow pushes: the **dev** registry, with the credential it already holds.
  GitHub is never granted write access to the prod account. The image crosses the account boundary
  only *after* the Owner authorizes, so the gate governs **entry to the prod account**, not merely
  the deploy. `/builds/runtime` **refuses `stage=prod`** when called with a GitHub OIDC token, so
  CI cannot deploy prod even if a workflow asked it to.

**One caveat worth knowing.** AGP never expires the candidate *record*, but the **image** it points
at can age out: the tenant's registry keeps only the newest 50 images, counted across every agent
in the tenant. Nothing detects this in advance (by design the platform never reads ECR), so it
surfaces late, as a pull failure in the promotion build's log. Practical rule: **promote a
candidate you care about reasonably promptly**, and if a promotion fails to pull, push to the trunk
again to register a fresh one.

### Required repository configuration

The platform wires all of these when it materializes the repo — you do not set them by hand. They
are written as **repository-level** Actions variables, which is deliberate: a build resolves a
variable environment-first and falls back to the repository set, so `vars.*` below resolve with no
GitHub Environment defined at all.

**Variables** (`Settings → Secrets and variables → Actions → Variables`):

| Variable | Required | Purpose |
|---|---|---|
| `AWS_REGION` | yes | Region for the OIDC credential + ECR login (e.g. `us-east-1`). |
| `AWS_ECR_PUSH_ROLE_ARN` | yes | IAM role assumed via GitHub OIDC. **ECR-push permissions only** — no provisioning, runtime, or IAM rights (those live on the platform's CodeBuild role). |
| `AGENT_ID` | yes | Per-agent id the platform stamps at materialize time. The image tag is `<AGENT_ID>-<tree_sha>`, so the platform can map the pushed image back to the agent. |
| `ECR_REPOSITORY` | yes | Target ECR repo URI; the image is tagged `<ECR_REPOSITORY>:<AGENT_ID>-<tree_sha>`. |
| `AGP_API_URL` | yes | Base URL of the AGP control-plane API; `trigger` POSTs to `<AGP_API_URL>/builds/runtime`. |
| `CONNECTION_ID` | yes | Platform connection id that scopes the runtime build to this org's infra repo. |

These carry **one stage's credentials, and that is the security property rather than a
limitation.** The values above are the DEV account's. A prod deploy is AGP copying an
already-approved digest into the prod account, so prod's push role must never reach CI.

**Secrets** (`Settings → Secrets and variables → Actions → Secrets`): none required.

There is **no Terraform and no runtime creation here** — the platform applies all infrastructure
(runtime, IAM, authorizer, registration) in its own pipeline. If you find yourself wanting to add
an `apply` step, that work belongs to the platform, not this repo.
