# Acme Reference Agent

The reference Strands-on-AgentCore-Runtime agent that closes the **C-3 loop** of the
Acme governance platform: *"an agent reaches an MCP server using the same access
mechanism a user uses to reach an agent."* When the platform invokes this agent with a
real user's identity, the agent asks **AgentCore Identity** for a **delegated
(On-Behalf-Of, user-preserving)** token for the MCP server's Entra audience, then uses
Strands' built-in MCP client to list and call the gateway's tools.

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
| `MCP_SERVERS` | A JSON **list** of the MCPs this agent is wired to — one entry per granted MCP, each `{"id","audience","gateway_url","label"}`. The backend rebuilds it from the agent's granted-MCP set on every grant/revoke, so an agent can hold **more than one** MCP at a time (the `label` is the per-server tool-namespace prefix, `{label}__{tool}`). `audience` is the MCP's Entra app audience (the OBO scope is `audience + "/.default"`); `gateway_url` is the **verbatim** `gatewayUrl` from `GetGateway` (never hand-construct it). The agent mints one OBO token per entry and degrades — if one MCP is unreachable it is dropped for that invoke and the others still load. | `[{"id":"<mcpId>","audience":"api://agp-mcp-<mcpId>","gateway_url":"https://<gatewayId>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp","label":"reference"}]` |
| `MCP_AUDIENCE` / `MCP_GATEWAY_URL` | *(legacy, pre-E12 fallback only)* The flat single-MCP keys. Used **only** when `MCP_SERVERS` is absent on a not-yet-redeployed runtime, in which case the agent builds a one-element list from them. The backend neutralizes these to `""` once it writes `MCP_SERVERS`. | `api://agp-mcp-<mcpId>` / `https://….../mcp` |
| `CREDENTIAL_PROVIDER_NAME` | The agent's AgentCore `MicrosoftOauth2` credential provider name (one per agent — the same OBO identity for every MCP). Convention: `agp-agent-obo-<agentId>` (the control-plane's `AGENT_CRED_PROVIDER_PREFIX` + the agent record id). The backend creates this provider at grant time. | `agp-agent-obo-<agentId>` |
| `AWS_REGION` | AWS region for the data-plane token client and Bedrock. | `us-east-1` |
| `MODEL_ID` | *(optional)* Bedrock model id the agent reasons with. Defaults to `us.anthropic.claude-sonnet-4-20250514-v1:0` (cross-region inference profile). Swap for any current Anthropic Claude model id valid on Bedrock. | `us.anthropic.claude-sonnet-4-20250514-v1:0` |
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

`requirements.txt` lists the agent's **own** deps — `strands-agents`, `bedrock-agentcore`,
`mcp` — which are **not** in the control-plane backend image (this is a separate
deployable, research Decision 7). `boto3` is available transitively in the AgentCore
runtime base image.

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
   # equivalently: agentcore configure --entrypoint agent.py && agentcore launch
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
   agent->MCP delegated `oauth2PermissionGrant` (the OBO precondition).

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
