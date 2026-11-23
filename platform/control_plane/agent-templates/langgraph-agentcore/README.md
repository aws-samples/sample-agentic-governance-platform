# LangGraph + AgentCore Agent Template

A barebones LangGraph agent — an LLM node and a tool node joined by a conditional edge — ready to
deploy to Bedrock AgentCore Runtime. Build your agent from this scaffold: edit the graph, the
tools and the system prompt in `src/main.py`, and let the platform handle infra, deploy, and
registration.

## What It Does

A single tool-using graph that can:
- Answer questions conversationally
- Tell the current date and time (via a tool, so the tool loop is exercised end to end)

The graph is the smallest shape that is recognisably LangGraph rather than one LLM call:
`START → agent → (tools → agent)* → END`, with `tools_condition` deciding whether the model asked
for a tool. Add nodes and edges from there.

## Tech Stack

- **LangGraph** (`langgraph >= 0.2`) + **LangChain AWS** (`langchain-aws`)
- **Bedrock AgentCore Runtime** (`bedrock-agentcore`)
- **Claude Sonnet** on Amazon Bedrock (`us.anthropic.claude-sonnet-4-6`), via `ChatBedrockConverse`
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
  -d '{"prompt": "What time is it?"}'
```

### Run the unit tests

```bash
python -m pytest tests/ -v
ruff check src tests
```

## Response contract

`POST /invocations` takes `{"prompt": "..."}` and answers with **one JSON object**:

```json
{"result": "It is 12:00:00 UTC.", "tool_calls": [{"name": "get_current_datetime", "args": {}}]}
```

`tool_calls` lists the tools this invoke called, in order (`[]` if none), and is read by the
evaluation tool checks (see "Evaluations"). It is only present on success. Beyond that key, it is
worth knowing two things about this contract.

**It is not a stream, unlike the Strands template in this catalog.** LangGraph's `invoke` is
request/response, so wrapping it in an SSE stream would be a translation layer with nothing to
translate. If you switch this handler to `graph.astream()` to get incremental tokens, follow the
Strands template's event shapes rather than inventing new ones.

**What the platform hands back to its own caller is nested one level deeper than you might
expect.** `POST /agents/{id}/invoke` always wraps in `{"response": ...}`, and it only unwraps the
payload on the **SSE** branch. This template takes the `application/json` branch, where the body is
forwarded verbatim, so a client sees:

```json
{"response": {"result": "It is 12:00:00 UTC."}}
```

Not `{"response": "It is 12:00:00 UTC."}` — that is the Strands (streaming) shape. Nothing unwraps
`result` for a non-streaming agent. This is a genuine difference between the two templates rather
than something to work around here; if it should be unified, that belongs in the platform's route.

**Keep `result` a string, and keep it called `result`.** It is the key the SSE extractor looks for,
so a rename would break a future streaming variant of this template, and the governance UI keys on
it as well. The string part matters for a subtler reason: `ChatBedrockConverse` returns `content` as
*either* a string *or* a list of content blocks depending on the model and whether tools were used,
so a list-valued `result` reaches the client as a JSON array where a string is expected — HTTP 200,
no log line, a blob in the UI. That is why `_answer_text()` flattens it and drops the non-text
blocks (a reasoning or tool-use block is not the answer). If you replace that helper, keep both
behaviours.

**Errors come back in the same shape** — `{"result": "Error: ..."}` — rather than raised. A raise
becomes a 500 with no body the platform can read, so the governance UI would show a failed
invocation with no reason attached.

## What you can change

**Behaviour lives in `src/main.py`** — the graph in `build_graph()`, the tools, and the
`SYSTEM_PROMPT`. That is the file to edit.

**Two import-order rules in that file are load-bearing.** The langchain/langgraph imports sit
*below* `_setup_langfuse_telemetry()` on purpose: LangChain reads `LANGSMITH_OTEL_ENABLED` and the
OTLP environment at import time, so a top-of-file import captures the un-configured values and
your traces silently never appear. (That is also why `src/main.py` ignores ruff's `E402` — the
ordering is the fix, not a lapse.) And `build_graph()` is called on first invocation, not at
import: the Bedrock client it builds would otherwise be constructed while the container is still
starting, where a credential or region fault kills the runtime instead of failing one invocation.

**Per-agent settings are NOT in this repo.** `agent_name`, `framework` and `model_id` live on your
agent's record in the platform's registry, which is where the build reads them from. There is
deliberately no `agent.config.json` here: a copy of that state in the repo is a copy that can
disagree with the governed record, and the runtime never reads it. Change those values in the
platform, not in a file.

Environment variables (`MODEL_ID` / `AWS_REGION` / `LOG_LEVEL`, see `.env.example`) configure the
container **when you run it locally**.

## Adding Tools

```python
from langchain_core.tools import tool


@tool
def my_tool(query: str) -> str:
    """Description of what this tool does.

    Args:
        query: The search query
    """
    return f"Result for {query}"


# Add to the module-level list — both the model binding and the ToolNode read it, so one
# edit registers the tool on both halves of the loop.
TOOLS = [get_current_datetime, my_tool]
```

## Observability

Traces go to the platform's Langfuse with **no wiring on your part**. The platform provisions one
Langfuse project + key per agent and injects the non-secret host plus the Secrets Manager secret
*name* into the runtime environment; `src/main.py` reads the `{public_key, secret_key}` pair from
Secrets Manager at import and points the OTLP exporter at that project. Attribution is structural
— each agent authenticates with its own project key, so its traces land in its own project — and
the key value is never a literal in this repo.

Spans come from `opentelemetry-instrumentation-langchain`, which covers LLM calls, tool
invocations and graph node transitions. Both it and the HTTP OTLP exporter are declared
dependencies rather than left to transitive resolution: LangChain ships no `[otel]` extra, and an
undeclared exporter is a `ModuleNotFoundError` at module import — i.e. the container never reaches
`app.run()` and the runtime crash-loops — the moment a Langfuse secret is present. Unconfigured
(no `LANGFUSE_HOST`/`LANGFUSE_SECRET_NAME`) simply means no telemetry; the agent still runs.

**Do not simplify `_setup_langfuse_telemetry()` down to `LangchainInstrumentor().instrument()`.**
That call patches LangChain to *emit* spans but installs no `TracerProvider`, and this template
disables AgentCore's ADOT provider a few lines earlier — so the global provider stays the no-op
`ProxyTracerProvider`, every span reports `is_recording() == False`, and **all** telemetry is
dropped silently with the Langfuse environment correctly set and nothing in the logs to say so.
Hence the explicit `TracerProvider` + `BatchSpanProcessor(OTLPSpanExporter())`, passed to
`.instrument(tracer_provider=...)` as well as set globally (`set_tracer_provider` is set-once, so
losing that race must not cost the spans). The provider is kept in a module-level variable purely
so the entrypoint can force-flush **it** before the microVM freezes — `get_tracer_provider()` is
not a substitute, since the Proxy has no `force_flush` at all.

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
├── config.py      # Environment-variable configuration
└── main.py        # Graph definition, tools, telemetry, and AgentCore entrypoint
tests/
└── test_agent.py  # Unit tests for the response contract + config
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

The workflow is byte-identical to the Strands template's, because nothing in it is
framework-specific: it lints, tests, builds a container and names the digest. Read that template's
README for the full reasoning on the digest-not-tag rule, the required repository variables, the
scope of the OIDC ECR-push role, and how the platform picks up the image — all of it applies here
unchanged.

`[skip ci]` works normally — GitHub honours it on `push`, and nothing here reaches around the
commit message (no `workflow_dispatch`, no message-blind trigger). The platform's own setup commit
carries it, which is why materializing this repo does not burn a build.

### Required repository configuration

The platform wires these when it materializes the repo — you do not set them by hand. They are
**repository-level** Actions variables (a build resolves a variable environment-first and falls
back to the repository set, so `vars.*` resolve with no GitHub Environment defined at all):
`AWS_REGION`, `AWS_ECR_PUSH_ROLE_ARN`, `AGENT_ID`, `ECR_REPOSITORY`, `AGP_API_URL`,
`CONNECTION_ID`. No secrets are required.

These carry **one stage's credentials, and that is the security property rather than a
limitation.** The values are the DEV account's. A prod deploy is the platform copying an
already-approved digest into the prod account, so prod's push role never reaches CI.
