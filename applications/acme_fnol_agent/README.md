# Acme FNOL (First Notification of Loss) Agent

A Strands-on-AgentCore-Runtime agent that closes the **C-3 loop** of the Acme
governance platform: *"an agent reaches an MCP server using the same access mechanism a
user uses to reach an agent."* When the platform invokes this agent with a real user's
identity, the agent asks **AgentCore Identity** for a **delegated (On-Behalf-Of,
user-preserving)** token for the MCP server's Entra audience, then uses Strands' built-in
MCP client to list and call the gateway's tools.

> **Persona — First Notification of Loss (FNOL).** This agent is the empathetic first
> contact when a customer reports a loss, incident, or accident. It puts **safety first**
> (directing anyone injured or in danger to the emergency services before anything else),
> then captures the incident in a structured way (what / when / where / who / damage /
> injuries / police references), confirms the details back, and sets clear expectations
> about the claims process. It **never admits or denies liability, never promises a payout
> or coverage**, and routes the report to a claims handler. The persona lives in the
> `SYSTEM_PROMPT` constant in `agent.py`.

This is a **separate deployable** with its own dependencies — it is **not** part of the AGP
control plane and shares nothing with the control-plane backend image. You deploy it
standalone with `deploy.sh`. The live deploy + invoke is **your step**; this README is the
runbook.

## How it works (the delegated token chain)

```
Platform UI ─(user token, aud=agent app)─▶ POST /agents/{id}/invoke  (E6 route)
  backend OBO ─▶ agent-audience token ─▶ invokes THIS runtime (E6, unchanged)
  INSIDE this agent (agent.py):
    WAT  = BedrockAgentCoreContext.get_workload_access_token()   # runtime-injected; binds the USER
    tok  = bedrock-agentcore.get_resource_oauth2_token(
              workloadIdentityToken         = WAT,
              resourceCredentialProviderName = CREDENTIAL_PROVIDER_NAME,
              oauth2Flow                     = "ON_BEHALF_OF_TOKEN_EXCHANGE",   # delegated — hardcoded
              scopes                         = [MCP_AUDIENCE + "/.default"])
           └─ AgentCore Identity does Entra OBO AS THE AGENT APP → MCP-audience token, USER PRESERVED
    MCPClient(streamablehttp_client(MCP_GATEWAY_URL, Authorization: Bearer tok)).list_tools_sync()
    └─ the gateway's CUSTOM_JWT authorizer validates aud == allowedAudience → the tool runs
```

The agent **never holds a printable secret**: the agent app's client secret lives in
AgentCore's Token Vault (a `MicrosoftOauth2` credential provider the control-plane creates
at grant time); the agent only calls the data-plane token util.

> **OBO is hardcoded — and that is deliberate.** `agent.py` calls
> `get_resource_oauth2_token(oauth2Flow="ON_BEHALF_OF_TOKEN_EXCHANGE")` directly. There is
> **no** env-var flow switch, **no** M2M branch, **no** dual-mode. The
> `@requires_access_token` decorator cannot do OBO (it is typed `M2M`/`USER_FEDERATION`
> only — research §3.3), so the raw boto3 call is required. **If the Preview OBO path does
> not preserve the user's identity at live bring-up, the pivot is a one-line change** —
> set `oauth2Flow="M2M"` in `get_mcp_obo_token` (autonomous/app-only: carries
> `roles:[Invoker]` + `idtyp:app`, but no user). That pivot is **made then, not pre-built**
> — this code commits to the delegated path.

## Environment variables

The runtime is configured with these. `MCP_SERVERS` and `CREDENTIAL_PROVIDER_NAME` are
**backend-managed** — the control-plane writes them at grant/revoke time (see the runbook);
you set `AWS_REGION` (and the optional `MODEL_ID`/`LOG_LEVEL`) in `agentcore
configure`/`launch` or on the runtime. The legacy `MCP_AUDIENCE`/`MCP_GATEWAY_URL` keys are a
pre-E12 fallback only.

| Variable | Meaning | Example |
|---|---|---|
| `MCP_SERVERS` | A JSON **list** of the MCPs this agent is wired to — one entry per granted MCP, each `{"id","audience","gateway_url","label"}`. The backend rebuilds it from the agent's granted-MCP set on every grant/revoke, so an agent can hold **more than one** MCP at a time (the `label` is the per-server tool-namespace prefix, `{label}__{tool}`). `audience` is the MCP's Entra app audience (the OBO scope is `audience + "/.default"`); `gateway_url` is the **verbatim** `gatewayUrl` from `GetGateway` (never hand-construct it). The agent mints one OBO token per entry and degrades — if one MCP is unreachable it is dropped for that invoke and the others still load. | `[{"id":"<mcpId>","audience":"api://agp-mcp-<mcpId>","gateway_url":"https://<gatewayId>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp","label":"fnol"}]` |
| `MCP_AUDIENCE` / `MCP_GATEWAY_URL` | *(legacy, pre-E12 fallback only)* The flat single-MCP keys. Used **only** when `MCP_SERVERS` is absent on a not-yet-redeployed runtime, in which case the agent builds a one-element list from them. The backend neutralizes these to `""` once it writes `MCP_SERVERS`. | `api://agp-mcp-<mcpId>` / `https://….../mcp` |
| `CREDENTIAL_PROVIDER_NAME` | The agent's AgentCore `MicrosoftOauth2` credential provider name (one per agent — the same OBO identity for every MCP). Convention: `agp-agent-obo-<agentId>` (the control-plane's `AGENT_CRED_PROVIDER_PREFIX` + the agent record id). The backend creates this provider at grant time. | `agp-agent-obo-<agentId>` |
| `AWS_REGION` | AWS region for the data-plane token client and Bedrock. | `us-east-1` |
| `MODEL_ID` | *(optional)* Bedrock model id the agent reasons with. Defaults to `us.anthropic.claude-sonnet-4-6` (cross-region inference profile). Swap for any current Anthropic Claude model id valid on Bedrock. | `us.anthropic.claude-sonnet-4-6` |
| `LOG_LEVEL` | *(optional)* Log level for **this agent's own** loggers. Defaults to `INFO`. `DEBUG` is safe to set: `agent.py` floors the AWS/HTTP client loggers (`boto3`, `botocore`, `urllib3`, `httpx`, `httpcore`, `s3transfer`) at `INFO` regardless of this value, because `botocore.parsers` logs every response body verbatim at DEBUG and this agent reads its Langfuse secret key with `get_secret_value` — so wire-level DEBUG would put the key in CloudWatch. Do not remove that clamp. | `INFO` |

## Runtime execution-role IAM

`agentcore configure` creates the runtime's execution role. It needs the three AgentCore
Identity data-plane token utils for the OBO call (research §3.5 / §7, set up by
`T-INFRA-DEPS` on the platform side):

```
bedrock-agentcore:GetWorkloadAccessToken
bedrock-agentcore:GetWorkloadAccessTokenForJWT
bedrock-agentcore:GetResourceOauth2Token
```

Scope them to `token-vault/default/oauth2credentialprovider/*` and
`workload-identity-directory/default/*` (research §7). The agent also needs
`bedrock:InvokeModel*` for the Bedrock model. Attach these to the runtime exec role after
`agentcore configure`.

## Dependencies

`requirements.txt` lists the agent's **own** deps — `strands-agents[otel]`,
`bedrock-agentcore`, `mcp` — which are **not** in the control-plane backend image (this is a
separate deployable, research Decision 7). `boto3` is available transitively in the AgentCore
runtime base image. The **`[otel]` extra** on `strands-agents` is required for Langfuse
observability (see below): without it, `setup_otlp_exporter()` silently imports no exporter and
**no traces are emitted**.

## Langfuse observability

This agent ships **full observability to a self-hosted Langfuse**: complete Strands traces
(prompts, completions, every tool call), **token usage**, **token cost** (computed out of the box
via Langfuse's built-in `claude-sonnet-4-6` model — see below), and **per-USER attribution** keyed
on the invoking user's Entra `oid`. The wiring
is in `agent.py`:

- A module-top block sets the Langfuse OTEL endpoint/auth/protocol env vars and calls
  `StrandsTelemetry().setup_otlp_exporter()` **once** at import. Strands then emits the GenAI
  semantic-convention spans (`invoke_agent` / `execute_event_loop_cycle` / `chat` /
  `execute_tool`) to Langfuse — MCP tool calls and model calls become first-class child
  observations automatically, no per-tool code.
- Per invocation, `agent.py` builds `trace_attributes` and passes them to the `Agent(...)` it
  constructs (on **both** the MCP and prompt-only paths): `langfuse.user.id` = the inbound
  user's `oid` (read best-effort from the inbound bearer), `langfuse.session.id` = the client's
  `session_id`/`conversation_id` (when present), plus `langfuse.trace.tags` /
  `langfuse.trace.name`. These attach to **every** span, so the Langfuse **Users** dashboard
  aggregates cost/tokens/traces per user.
- A `finally` in the entrypoint calls `force_flush()` after the stream drains — mandatory on the
  short-lived AgentCore runtime, or spans buffered by the background exporter are lost when the
  microVM freezes.

> ✅ **The Langfuse project key is read from AWS Secrets Manager** (E26/T10 — closes E12 release
> gate #2). `agent.py` no longer carries any hardcoded key literal. The platform provisions one
> Langfuse **project + key per agent** and stores the `{public_key, secret_key}` pair in Secrets
> Manager; the runtime is injected with `LANGFUSE_HOST` + `LANGFUSE_SECRET_NAME`. At import,
> `_setup_langfuse_telemetry()` fetches the pair from that secret and builds the OTLP Basic-auth
> header from it — attribution is **structural** (each agent authenticates with its own project
> key, so traces land in its own project). If `LANGFUSE_HOST`/`LANGFUSE_SECRET_NAME` are unset or
> the secret is unreadable, telemetry is simply disabled and the agent runs fine. The secret
> VALUE lives only in Secrets Manager — never in code, never logged (the base64 auth IS the secret).

> ⚠️ **The `DISABLE_ADOT_OBSERVABILITY=true` trap.** On AgentCore, the runtime auto-instruments
> with ADOT and owns the global OTEL TracerProvider **first** (set-once), which silently no-ops a
> later `StrandsTelemetry()` so **Langfuse would receive nothing**. `agent.py` sets
> `DISABLE_ADOT_OBSERVABILITY=true` at module top as a best-effort belt-and-suspenders fallback,
> but the **reliable** place is a **deploy-time runtime env var** — and **`deploy.sh` now sets it
> on the runtime for you** (no longer a manual step). It does so two reinforcing ways: it passes
> `agentcore launch --env DISABLE_ADOT_OBSERVABILITY=true` (lands it on the **first** deploy), and
> its preserve-on-redeploy step re-asserts `DISABLE_ADOT_OBSERVABILITY=true` in the merged
> `environmentVariables` after `launch` (so a **redeploy** keeps it alongside the restored MCP env,
> without dropping the inbound authorizer). Override it with `DISABLE_ADOT_OBSERVABILITY=false
> ./deploy.sh` if you ever want ADOT/CloudWatch instead. `deploy.sh` sets `DISABLE_ADOT_OBSERVABILITY`
> and passes the non-secret `LANGFUSE_HOST` + `LANGFUSE_SECRET_NAME` through to the runtime; the
> Langfuse OTEL endpoint/headers/protocol are built in `agent.py` from the Secrets-Manager key
> pair (the secret-bearing `OTEL_EXPORTER_OTLP_HEADERS` deliberately never transits the deploy script).

### Token COST — computed out of the box (built-in model)

Token **usage** flows to Langfuse automatically, and on this Langfuse instance **token cost is
computed automatically too — no custom model needed.** Langfuse ships a built-in,
Langfuse-maintained `claude-sonnet-4-6` model whose match pattern includes the
`(eu\.|us\.|apac\.|global\.)?anthropic\.claude-sonnet-4-6` form, which **matches** the Bedrock id
this agent emits (`us.anthropic.claude-sonnet-4-6`), and it carries per-token **input/output (+
cache)** pricing. So cost lands on traces with zero extra setup.

> **Fallback (only if your instance lacks a matching built-in model).** If your Langfuse
> instance's built-in model list does **not** include a matching `claude-sonnet-4-6` entry, add a
> custom model in **Langfuse UI → Settings → Models → Add model definition** (per Langfuse
> project) with match pattern `(?i)^(us\.|eu\.|apac\.|global\.)?anthropic\.claude-sonnet-4-6.*$`,
> **Unit:** `TOKENS`, and per-token **input**/**output** USD prices. Custom pricing is **not
> retroactive** (new traces only) and is **per Langfuse project** (script it per project via
> `POST /api/public/models` if the platform mints one project per use case). Verify on a real
> trace that the emitted usage-detail key names line up with the price keys.

> **PII note (prototype stance).** With content capture on, full prompts/completions/tool I/O
> land in Langfuse (first-report-of-loss text — incident details, customer and third-party PII).
> For the prototype this targets a **non-prod** Langfuse project with restricted access + short
> retention. **Before production**, add a redaction layer (OTEL Collector or source-level) — the
> Langfuse SDK `mask=` does **not** cover Strands' raw OTLP spans.

## Runbook — the full sequence you run

> **Prereq:** `terraform apply` the platform's gateway + credential-provider IAM
> (`T-INFRA-DEPS`) so the backend can call the new control-plane actions, and ensure your
> AWS credentials/region are set. Once deps are installed in your build or CI environment,
> run a dependency vulnerability scan:
> `pip install -r requirements.txt && pip-audit -r requirements.txt`
> (The deps are not installed in this offline build; the scan is a documented follow-up
> before routing traffic.)

1. **Configure and launch** the runtime (writes `.bedrock_agentcore.yaml`, builds and pushes
   the image, creates the AgentCore Runtime):
   ```bash
   ./deploy.sh
   # equivalently: agentcore configure --entrypoint agent.py --name acme_fnol_agent && agentcore launch
   ```
   `deploy.sh` installs the `bedrock-agentcore-starter-toolkit` if absent, then runs
   `agentcore configure` + `agentcore launch` (builds `linux/arm64`, pushes ECR, creates the
   runtime).

   > **A REDEPLOY is code-only and self-preserving — no re-prompt, no manual re-grant.** On a
   > redeploy of a runtime that **already exists**, `deploy.sh` **skips `agentcore configure`**
   > and runs only `agentcore launch` (which reads the on-disk `.bedrock_agentcore.yaml`
   > standalone). This is deliberate: `agentcore configure` is interactive and **always**
   > re-runs — on an existing runtime it re-prompts (python version / S3 / IAM-auth) and
   > **rewrites `.bedrock_agentcore.yaml` with the inbound authorizer defaulted to IAM**
   > (`authorizer_configuration: null`), which the subsequent full-replace `launch` would then
   > push, **dropping the `CUSTOM_JWT`/Entra gate**. Skipping it means a redeploy **maintains all
   > existing runtime settings** and ships only the new code. `configure` still runs on a **first
   > deploy** (no runtime yet) and when the local `.bedrock_agentcore.yaml` is **absent** (the
   > runtime id was resolved via the `list-agent-runtimes` fallback — `configure` regenerates the
   > yaml so `launch` can read it; the authorizer is then restored after `launch`, see below).
   > **Escape hatch:** set `FORCE_CONFIGURE=1` to deliberately reconfigure an existing runtime
   > (re-run the wizard) — only use it when you actually intend to change the runtime's config.

   > **NOTE: the runtime is not Entra-authorizer-gated after the FIRST deploy alone.** On a
   > first deploy (no runtime yet) `agentcore launch` creates the runtime but its inbound auth
   > is the toolkit default (IAM), not the platform's `CUSTOM_JWT`/Entra gate. Do not route real
   > user traffic to it until step 2 (control-plane provisioning **or** `SET_AUTHORIZER=1`) has
   > completed and the authorizer reads back as a `customJWTAuthorizer`.

   > **PRESERVE-ON-REDEPLOY.** Even with `configure` skipped, `agentcore launch` itself does a
   > full-replace `UpdateAgentRuntime` under the hood (research §12.7: any field not replayed is
   > silently dropped), which can RESET two things the platform set on the runtime: the **inbound
   > authorizer** (the security gate) **and** the **`environmentVariables`** the backend injects
   > at grant time (`MCP_SERVERS` / `CREDENTIAL_PROVIDER_NAME` / `AWS_REGION` — dropping those
   > makes the agent lose its MCP wiring, falling back to prompt-only). So when the runtime
   > **already exists**, `deploy.sh` is existence-aware: it derives the runtime id (from
   > `.bedrock_agentcore.yaml`, else a `list-agent-runtimes` lookup by name), **captures** the
   > current inbound authorizer **and** environment variables via `GetAgentRuntime` **before**
   > `launch`, then **after** `launch` re-reads the runtime and — if launch reset/changed
   > **either** — **restores** them in one full-replace `UpdateAgentRuntime` that replays the
   > **fresh post-launch `agentRuntimeArtifact`** (the new code you just shipped) plus `roleArn`/
   > `networkConfiguration`, the **pre-launch `authorizerConfiguration`**, and a **merge of the
   > environment** (post-launch env overlaid by the pre-launch env, so the backend-injected vars
   > win and survive while any env a new launch legitimately introduced is kept). Inbound auth on
   > an AgentCore **Runtime** is carried **solely** by `authorizerConfiguration.customJWTAuthorizer`
   > — there is **no** `authorizerType` field on `GetAgentRuntime`/`UpdateAgentRuntime` (that
   > exists only on **Gateways**) — so the capture/compare/restore all key on the
   > `customJWTAuthorizer`, and the restore sends **only** `authorizerConfiguration` (sending an
   > `authorizerType` would be rejected). The authorizer and the env are preserved
   > **independently** (a runtime with only one of them is handled). If launch left both untouched
   > it logs `inbound authorizer and environment variables preserved by launch; no restore needed`
   > and does nothing (idempotent). **A code redeploy therefore self-preserves the gate and the
   > env — no manual re-grant is required** (the runtime ends up with the new code + the same
   > `customJWTAuthorizer` + the same env). The preserve path uses `jq` to capture/rebuild the
   > structured replay; if `jq` is **not** installed (or any capture/restore step fails) the
   > script **warns loudly** that the authorizer **and** env could not be auto-preserved and that
   > you must verify and restore them manually (re-run platform provisioning / re-grant the agent,
   > or `SET_AUTHORIZER=1`) — it never silently proceeds as if they survived. (Setting
   > `SET_AUTHORIZER=1` skips the preserve path, since that run explicitly sets the authorizer
   > itself — see step 2.)

2. **Set the inbound authorizer (Entra).** The runtime's inbound JWT authorizer must trust
   our Entra tenant so only the platform's user-minted tokens get in. This is normally set
   by **our control-plane's agent provisioning** (the E6 `UpdateAgentRuntime` path) when the
   agent is registered/provisioned in the platform. To set it from the script instead, run
   `deploy.sh` with `SET_AUTHORIZER=1` (plus `TENANT_ID`, `AGENT_AUDIENCE`,
   `AGENT_RUNTIME_ID`). `UpdateAgentRuntime` is a full-replace PUT — replay
   `agentRuntimeArtifact`/`roleArn`/`networkConfiguration` from `GetAgentRuntime`.

3. **Create the credential provider** — *handled by the backend, document-only here.* When
   you **grant this agent access to an MCP** (next step), the control-plane mints the agent
   app a client secret (Graph `addPassword`) and creates the `MicrosoftOauth2` credential
   provider `agp-agent-obo-<agentId>` in the Token Vault (`T-CRED-PROVIDER`). Set this
   agent's `CREDENTIAL_PROVIDER_NAME` env var to that name. *Dependency: the provider must
   exist before the agent's first OBO call succeeds.*

4. **Grant the agent the MCP (via the UI).** On the MCP server's **Connected Agents** tab
   (or the agent's **MCP Servers** tab), grant this agent the **Invoker** role on the target
   MCP. This (a) writes the Entra app-role assignment — the binary gate — and (b) writes the
   agent->MCP delegated `oauth2PermissionGrant` (the OBO precondition). Because the MCP
   wiring in `agent.py` is intact, granting the MCP **auto-wires the agent's tools with zero
   code change** (the grant-time env injection above does the rest).

5. **The agent's MCP env is set by the backend** (document-only here). On the grant in step 4
   — and on every later grant/revoke — the control-plane rebuilds the agent's `MCP_SERVERS`
   list (one entry per MCP in its granted set, each carrying the registry's verbatim
   `gateway_url` and `entra_app_audience`) plus `CREDENTIAL_PROVIDER_NAME`, and pushes them to
   the runtime. Granting a SECOND MCP **adds** to the list rather than overwriting the first.
   You only set `AWS_REGION` (and the optional `MODEL_ID`/`LOG_LEVEL`) yourself.

6. **Invoke as an assigned user.** Through the platform `POST /agents/{id}/invoke` with a
   real user's token (or, for a quick local check, `agentcore invoke '{"prompt": "list the
   available tools"}'`). An **assigned** user → the call succeeds and the gateway's tools
   run. An **unassigned** agent/user → Entra refuses the OBO token (`AADSTS50105`/`65001`),
   so the agent cannot reach the MCP — proving the grant gates the invoke.

7. **Verify the delegation.** Decode the MCP token the agent obtained: it should carry the
   **user's `oid`/`sub`** (delegated) and `aud` == the MCP audience. If it does → full C-3
   proven. If the Preview OBO path does not preserve the user, apply the one-line `M2M`
   pivot described above (a deliberate follow-up, not pre-built).

## Local notes

- The entrypoint serves `POST /invocations` + `GET /ping` on `:8080` (AgentCore default).
  `python agent.py` runs it locally, but the OBO call only succeeds inside an AgentCore
  runtime with a JWT-inbound user context (the WAT is runtime-injected) and a configured
  credential provider — so end-to-end exercise happens after deploy, not locally.
