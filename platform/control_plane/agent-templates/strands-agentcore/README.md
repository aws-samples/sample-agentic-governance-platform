# Strands + AgentCore Agent Template

A barebones Strands agent with tools and conversation memory, ready to deploy to
Bedrock AgentCore Runtime. Build your agent from this scaffold: edit the tools and system
prompt in `src/main.py`, and let the platform handle infra, deploy, and registration.

## What It Does

A single agent that can:
- Answer questions conversationally (multi-turn with sliding-window memory)
- Perform math calculations (safe AST-based evaluator)
- Tell the current date and time

## Tech Stack

- **Strands Agents SDK** (`strands-agents >= 1.38`)
- **Bedrock AgentCore Runtime** (`bedrock-agentcore`)
- **Claude Sonnet** on Amazon Bedrock (`us.anthropic.claude-sonnet-4-6`)
- Python 3.11+

## Quick Start

```bash
# Install dependencies (with dev extras for tests + lint)
pip install -e ".[dev]"

# Set environment variables (or copy .env.example)
export MODEL_ID=us.anthropic.claude-sonnet-4-6
export AWS_REGION=us-east-1

# Run locally
python -m src.main
```

The agent starts on `http://localhost:8080` (AgentCore default port), serving
`POST /invocations` and `GET /ping`.

### Test the running agent

```bash
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is sqrt(144) + 5?"}'
```

### Run the unit tests

```bash
python -m pytest tests/ -v
ruff check src tests
```

## Response contract

`POST /invocations` answers with an **SSE stream** of `data: <json>` lines. If you change how the
entrypoint in `src/main.py` yields events, keep this contract — the platform's invoke panel reads
your agent through it.

Two event shapes go on the wire, because the stream has two kinds of reader:

```
data: {"type": "content", "data": "<token>"}                                    # incremental
data: {"message": {"role": "assistant", "content": [{"text": "<full answer>"}]}}  # terminal
```

- A **streaming UI** renders the `content` chunks as they arrive.
- A **buffering client** — the platform's `POST /agents/{id}/invoke` — ignores the chunks and
  takes the answer from the **last message-shaped `data:` line**, joining its `content[].text`.

Two rules follow, and both are load-bearing:

1. **Always emit a terminal message-shaped line.** Streaming only the `content` chunks leaves a
   buffering client with nothing to key on: it falls back to the last raw event and shows a single
   token as the whole answer.
2. **Never forward a message that carries no `text`.** A tool-using run also emits `message`
   events for the tool call and its result (`toolUse` / `toolResult` blocks, no `text`). Since the
   buffering client keeps the last message-shaped line whether or not it has text, forwarding
   those breaks the **error** path: a run that finishes a tool cycle and *then* fails (Bedrock
   throttling, say) emits no terminal message, so the client would render the stale `toolResult`
   as the answer and the error would never be seen.

Errors are their own event: `data: {"type": "error", "data": "<message>"}`.

Both sides of this are tested. `tests/test_agent.py` pins the producer here; the platform asserts
its parser against a body captured from **this** template, so the two cannot drift apart silently.

## What you can change

**Behaviour lives in `src/main.py`** — the tools and the `SYSTEM_PROMPT`. That is the file to
edit.

**Per-agent settings are NOT in this repo.** `agent_name`, `framework` and `model_id` live on
your agent's record in the platform's registry, which is where the build reads them from. There
is deliberately no `agent.config.json` here: a copy of that state in the repo was a copy that
could disagree with the governed record, and the runtime never read it. Change those values in
the platform, not in a file.

Environment variables (`MODEL_ID` / `AWS_REGION` / `LOG_LEVEL`, see `.env.example`) configure the
container **when you run it locally**.

## Adding Tools

```python
from strands import tool

@tool
def my_tool(query: str) -> str:
    """Description of what this tool does.

    Args:
        query: The search query
    """
    return f"Result for {query}"

# Add to the agent
agent = Agent(tools=[calculator, get_current_datetime, my_tool])
```

## Project Structure

```
src/
├── __init__.py
├── config.py      # Environment-variable configuration
└── main.py        # Agent definition, tools, and AgentCore entrypoint
tests/
└── test_agent.py  # Unit tests for tools + config
Dockerfile         # linux/arm64 container image (CMD: python -m src.main)
```

## Infra, deploy, and registration are platform-owned

This repo is **just the agent scaffold**. You do not write Terraform, deploy scripts, or
register the agent yourself — the platform owns runtime provisioning, ECR, IAM, the inbound
authorizer, and agent registration. **This repo's CI only builds and pushes the container
image**; the platform deploys it from there.

## CI/CD — dev build here, prod promotion in the platform (`.github/workflows/build.yml`)


**The rule: a push to this repo's trunk builds and deploys dev. Nothing else deploys anything.**
A pull request runs `quality` only; a push to any other branch runs **nothing**, so open a PR into
the trunk to get CI on your work. (`build.yml`'s `push.branches` list names the trunk literally —
if your project's trunk is not `main`, that is the one line to edit.)

The narrowness is deliberate: there is exactly **one dev runtime per agent**, so if every feature
branch deployed, N branches would race to overwrite the same deployment, last-push-wins.

**Two pushes to the trunk QUEUE rather than race.** `concurrency` is keyed on the ref with
`cancel-in-progress: false`, and that is load-bearing, not hygiene: the platform build runs
`terraform apply` against ONE state per agent, and two builds 11 seconds apart once raced that
state lock — the loser failed, the winner deployed a stale image, and the platform's own record
claimed success. Never cancel either: a deploy already in flight must be allowed to finish, since
killing it halfway abandons the lock and half-applies the runtime.

Beyond that, no methodology is imposed. Landing on the trunk **nominates** an image for
production; the prod deploy itself is a gated action in the platform, never a side effect of a
merge. **There is no prod path in this workflow at all.**

- **`quality`** (every PR + every trunk push): `pip install -e ".[dev]"` → `ruff check src tests`
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
repository *content*. It is a convenient, human-readable label, and it is **not** what the platform
deploys.

A tag is a mutable pointer. The registry keeps tags mutable and this build is not
byte-reproducible (a floating `python:3.11-slim` base, and version *ranges* in `pyproject.toml`
which the `Dockerfile` installs from directly — `uv.lock` ships in this repository but the image
build does not install from it), so the bytes behind one tag can differ between the moment
something is approved and the moment production deploys it. **The digest names the bytes themselves**, which is the only value an
approval can honestly attest to.

The digest is read **from the push itself** (buildx's own `--metadata-file`), not from a later
registry lookup. Two reasons, both load-bearing: a read-back of the tag may already have been
overwritten by a concurrent push — the exact class of bug the digest closes — and it needs no ECR
read grant, so the push-only role does not have to be widened.

**If the digest cannot be resolved, the build FAILS.** Empty and malformed collapse into the same
refusal on purpose: anything that is not a `sha256:` reference means this build cannot name what it
pushed, and reporting a deploy for an unnamed image is precisely how a stale runtime gets recorded
as a success. A red job is strictly cheaper than that. The `trigger` job re-validates the digest
against `^sha256:[0-9a-f]{64}$` before it POSTs, so a malformed value cannot reach the platform
even if the build step's own guard were ever loosened.

### Trunk push → a candidate; an Owner approves in AGP

- **Push to the trunk → tenant dev account.** `build` + `trigger` run automatically and POST
  `stage=dev`. The platform deploys that digest to the dev runtime.
- **The same POST registers the prod candidate**, and only from the trunk. The platform reads the
  ref from the **OIDC token** — so it is cryptographically proven, not self-reported — and compares
  it against the project's configured trunk branch. A push to any other branch registers nothing,
  which is what keeps an unreviewed branch from nominating itself for production. The candidate
  records the digest, the commit sha, and the GitHub login the token attests to. A newer trunk push
  **replaces** it; there is one candidate, so there is no queue.
- **Prod deploy → tenant prod account** is **an action in the platform, not in this repo.** A
  project **Owner** approves the pending candidate in AGP
  (`POST /projects/{id}/repos/{repo_id}/promote`); the platform authorizes the caller against the
  project's roles and deploys the **approved digest verbatim** — it does not re-resolve the tag,
  because re-resolving is what would reopen the window the digest closes. **No tag and no digest is
  ever entered by an operator, and no rebuild happens.** With no pending candidate there is nothing
  to promote and the platform refuses.

  Note where this workflow pushes: the **dev** registry, with the credential it already holds.
  GitHub is never granted write access to the prod account. The image crosses the account boundary
  only *after* the Owner authorizes, so the gate governs **entry to the prod account**, not merely
  the deploy.

  The platform is the **only** prod path: `/builds/runtime` **refuses `stage=prod`** when called
  with a GitHub OIDC token, so CI cannot deploy prod even if a workflow asked it to. Nothing you
  can add here will reach the prod account — promote through AGP.

**One caveat worth knowing.** AGP never expires the candidate *record*, but the **image** it points
at can age out: the tenant's registry keeps only the newest 50 images, counted across every agent
in the tenant. Nothing detects this in advance (by design the platform never reads ECR), so it
surfaces late, as a pull failure in the promotion build's log. Practical rule: **promote a
candidate you care about reasonably promptly**, and if a promotion fails to pull, push to the trunk
again to register a fresh one.

**Who attests to what** stays split, with neither system copying the other's fact: your repo
provider owns *who wrote, reviewed, and merged the code* (natively attributed to real humans); AGP
owns *who authorized the prod deploy* — a verb no provider models.

There is **no Terraform and no runtime creation here** — the platform applies all infrastructure
(runtime, IAM, authorizer, registration) in its own pipeline. If you find yourself wanting to add
an `apply` step, that work belongs to the platform, not this repo.

### Required repository configuration

The platform wires all of these when it materializes the repo — you do not set them by hand. They
are written as **repository-level** Actions variables, which is deliberate: a build resolves a
variable environment-first and falls back to the repository set, so `vars.*` below resolve with no
GitHub Environment defined at all. AGP creates no environments (they are a GitHub-only concept).

**Variables** (`Settings → Secrets and variables → Actions → Variables`):

| Variable | Required | Purpose |
|---|---|---|
| `AWS_REGION` | yes | Region for the OIDC credential + ECR login (e.g. `us-east-1`). |
| `AWS_ECR_PUSH_ROLE_ARN` | yes | IAM role assumed via GitHub OIDC. **ECR-push permissions only** — no provisioning, runtime, or IAM rights (those live on the platform's CodeBuild role). |
| `AGENT_ID` | yes | Per-agent id the platform stamps at materialize time. The image tag is `<AGENT_ID>-<tree_sha>`, so the platform can map the pushed image back to the agent. |
| `ECR_REPOSITORY` | yes | Target ECR repo URI; the image is tagged `<ECR_REPOSITORY>:<AGENT_ID>-<tree_sha>`. |
| `AGP_API_URL` | yes | Base URL of the AGP control-plane API; `trigger` POSTs to `<AGP_API_URL>/builds/runtime`. |
| `CONNECTION_ID` | yes | Platform connection id that scopes the runtime build to this org's infra repo. |

These carry **one stage's credentials, and that is the security property rather than a limitation.**
The values above are the DEV account's. A prod deploy is AGP copying an already-approved digest into
the prod account, so prod's push role must never reach CI.

**Secrets** (`Settings → Secrets and variables → Actions → Secrets`):

None required.

### The OIDC ECR-push role

The `build` job authenticates to AWS with GitHub's OIDC provider (`id-token: write`, no long-lived
keys). The assumed role (`AWS_ECR_PUSH_ROLE_ARN`) must be scoped to **ECR push only** —
`ecr:GetAuthorizationToken` plus the layer/image push actions on the target repo — and its trust
policy must be limited to this repository. It must **not** carry any provisioning,
runtime-creation, or IAM permissions; the platform's own CodeBuild role owns that surface. It
grants access to the **dev** registry only.

### How the platform picks up the image

After the image is pushed, the `trigger` job calls the platform explicitly: it mints a GitHub OIDC
token (audience `agp-runtime-build`, no long-lived keys) and `POST`s to
`<AGP_API_URL>/builds/runtime` with `{agent_id, image_tag, image_digest, ecr_repo, connection_id,
stage}`. The platform validates the token, checks that the token's repository is the one bound to
this agent, then starts a CodeBuild that clones the org's infra repo and provisions the runtime for
**the digest** — `<repo>@<digest>`, never a tag lookup. `id-token: write` is the only extra grant
the `trigger` job needs.

The build reads `AGENT_NAME` and `MODEL_ID` from the **governed registry record**, not from any file
in this repo. That is why there is no `agent.config.json` to keep in sync.
