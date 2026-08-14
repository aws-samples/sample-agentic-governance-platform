# Applications

The standalone **demo agents** governed by the Agentic Governance Platform. These are not part of the
control-plane backend image — each is a separate deployable (Strands-on-AgentCore-Runtime) with its own
dependencies. The platform **registers** them and **invokes** them via `POST /agents/{id}/invoke`, and they
demonstrate the end-to-end governance loop: a user's identity flows through the platform to the agent, the agent
asks AgentCore Identity for a **delegated (On-Behalf-Of, user-preserving)** token for an MCP server's Entra
audience, and uses the Strands MCP client to list and call that gateway's tools — under live Entra grants and
Cedar per-tool policies.

This is **capability C-3** of the governance demo: *"an agent reaches an MCP server using the same access
mechanism a user uses to reach an agent."* The three insurance-persona agents below extend the proven reference
runtime with the **E12 multi-MCP loop** — one agent granted and actively using several MCP servers at once, with
per-server OBO tokens and namespaced tools.

---

## The agents

| Agent | Persona | Role |
|-------|---------|------|
| [`acme_reference_agent/`](acme_reference_agent/) | Reference runtime | The proven Strands-on-AgentCore reference agent that closes the C-3 loop. Its [`deploy.sh`](acme_reference_agent/deploy.sh) is where that runtime configuration is pinned; the other three each carry their own `deploy.sh` modelled on it — `agentcore configure` writes the config to a local file that is machine-specific and never committed. |
| [`acme_contact_center_agent/`](acme_contact_center_agent/) | Contact Center | The warm, efficient virtual front desk — triages servicing enquiries (policy/coverage basics, billing, document requests, claim status) and routes to the right specialist. |
| [`acme_fnol_agent/`](acme_fnol_agent/) | First Notification of Loss (FNOL) | The empathetic first responder for new loss reports — gathers incident details and starts a claim. |
| [`acme_onboarding_agent/`](acme_onboarding_agent/) | Insurance Onboarding Support | Helps new policyholders get set up and oriented after purchase. |

Each agent's persona lives in the `SYSTEM_PROMPT` constant in its `agent.py`; the delegated-token chain lives in
`agent.py` + `mcp_runtime.py`. Each is governed in the platform via the E6/E7 grant path — the backend injects
`MCP_SERVERS` / `CREDENTIAL_PROVIDER_NAME` env at registration — and several carry a marketplace `blueprint_ref`
(E9). Their READMEs are the per-agent runbooks; read them before deploying.

---

## Deploying

The **live deploy is your step** — these agents run as AgentCore runtimes, which is outside the control plane.

```bash
# Deploy all three persona agents to Bedrock AgentCore in one non-interactive command.
# (--dry-run prints the exact commands and makes no AWS/agentcore calls.)
./deploy_all_agents.sh --dry-run
./deploy_all_agents.sh

# Or deploy a single agent (e.g. the reference runtime) from its own dir:
cd acme_reference_agent
./deploy.sh
```

`deploy_all_agents.sh` loops over `acme_contact_center_agent`, `acme_fnol_agent`, and
`acme_onboarding_agent`, reproducing the proven reference runtime config and applying the **preserve-on-redeploy**
logic so a redeploy never drops the CUSTOM_JWT/Entra authorizer the platform sets at registration. New runtimes
start with **auth = NONE on purpose** — the platform locks them down at registration via the E6/E7 provisioning
path; do not configure an inbound authorizer in the deploy scripts.

---

## The runtime environment contract (how an agent reads its MCPs)

Any agent deployed on the platform — these demos or your own — is configured **entirely through
environment variables** that the backend injects into the AgentCore runtime at registration/grant time.
An agent never hardcodes gateway URLs or audiences; it reads them from its environment on each invoke:

| Env var | Written by | Contents |
|---|---|---|
| `MCP_SERVERS` | Backend (`build_runtime_mcp_env`) on every grant change | JSON list of the MCPs this agent is granted: `[{"id", "audience", "gateway_url", "label"}, …]` |
| `CREDENTIAL_PROVIDER_NAME` | Backend at registration | The AgentCore Identity OAuth2 credential provider used to mint per-MCP delegated (OBO) tokens |
| `MCP_AUDIENCE` / `MCP_GATEWAY_URL` | Legacy (pre-multi-MCP) | Single-MCP fallback, honored only when both are non-empty and `MCP_SERVERS` is absent |
| `LANGFUSE_HOST` / `LANGFUSE_SECRET_NAME` | Backend at registration | Observability wiring — the agent resolves the secret and sets the `OTEL_*` exporter env itself |

Rules an agent implementation must follow (see `acme_reference_agent/mcp_runtime.py` for the canonical
consumer):

- **Degrade, never crash.** A malformed or absent `MCP_SERVERS` means "run prompt-only" — it must never
  raise out of an invoke.
- **Per-server OBO tokens.** For each entry, ask AgentCore Identity for a delegated token with that
  server's `audience` (`oauth2Flow="ON_BEHALF_OF_TOKEN_EXCHANGE"`) — the user's identity is preserved
  end-to-end.
- **Namespaced tools.** Advertise each MCP tool to the model as `{label}__{name}` so names never collide
  across servers and every call is traceable to its source MCP.

---

## How it works (the delegated token chain)

```
Platform UI ─(user token, aud=agent app)─▶ POST /agents/{id}/invoke  (E6 route)
  backend OBO ─▶ agent-audience token ─▶ invokes the agent runtime (E6)
  INSIDE the agent (agent.py):
    WAT = get_workload_access_token()                       # runtime-injected; binds the USER
    tok = get_resource_oauth2_token(ON_BEHALF_OF…)          # delegated, per MCP server
    Strands MCP client ─(tok)─▶ MCP gateway                 # Cedar per-tool authz on each call
```

See each agent's `README.md` for the full runbook and [`docs/project-history.md`](../docs/project-history.md)
for how the reference runtime and the multi-MCP loop came to be.

---

## License

See [LICENSE](../LICENSE) for details.
