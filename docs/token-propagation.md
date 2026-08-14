# Token propagation

One user action — "invoke this agent" — crosses a chain of trust boundaries, minting a new token wherever the audience
changes. This document follows one request down that chain, answering the same four questions at every hop: **which
token is on the wire, who validates it, what failure looks like, and what is never logged.** In short: the browser
holds a token for the backend's exposed API, the application validates it and derives a role and a tenant scope, the
backend spends that raw token for an agent-audience token, the runtime exchanges again for an MCP-audience token, and
only then does Cedar decide, per tool call. For a **Databricks-governed agent** (E29) the chain forks after the first
exchange: the backend performs an RFC 8693 federated token exchange at the tenant's workspace and bearer-POSTs the
exchanged token to the Databricks App or serving endpoint — no AgentCore hop, and no second OBO exchange. Microsoft
Entra ID is the provider supported today; configuring the tenant is [Microsoft Entra ID setup](entra-setup.md).

## 1. Sign-in, and the two places a token lives

The browser runs MSAL v5. Sign-in is **always a redirect** — the SPA has no popup path — and the only scope requested
is the backend's exposed API scope, `api://agp/Access.Default` by default. It acquires silently on mount, redirects
interactively when MSAL needs it, and clears the token on any other error. **The token then lives in two stores,
deliberately different.**

| Store | Contents | Cleared when |
|---|---|---|
| `sessionStorage` | the whole MSAL cache — accounts, refresh artifacts, ID and access tokens | tab close |
| `localStorage.auth_token` | a hand-managed **mirror of the raw access token string** | on acquire failure, on sign-out, on any 401 |

The mirror exists because the axios interceptor reads that key (§2) and the MSAL cache is not addressable that way;
MSAL keeps its own in `sessionStorage` for a smaller cross-site-scripting blast radius. The consequence: **the raw
bearer token outlives the MSAL session** — close the tab and MSAL's cache is gone, `auth_token` is not. Only
`ACQUIRE_TOKEN_SUCCESS` is mirrored, **never** `LOGIN_SUCCESS`, whose `payload.accessToken` is the **ID token**,
audience the SPA client-ID GUID — a valid JWT the backend rejects on audience, and one reason the validator accepts
that GUID at all (§4.1).

## 2. Attaching the token, and what a 401 does

One axios instance is the **only** egress to the API — no raw `fetch()` reaches the backend — so one request
interceptor reads `localStorage.auth_token` and sets `Authorization: Bearer <token>`. The **first 401 of an incident**
removes `auth_token`, calls `window.location.reload()`, and rejects with `Session expired. Please log in again.`;
anything else rejects with the response's `detail`, which is why the FastAPI `detail` string *is* the client-side error
message quoted throughout. There is **no refresh-and-retry**: it hard-reloads, and the provider re-acquires silently
because MSAL still holds the account.

**The reload budget is one** (E36/T19). A 401 that survives the reload is not expiry, so the second one does *not*
reload: it rejects with a terminal message naming the configuration causes, and `auth_token` is deliberately left in
place — clearing it would assert "your session ended" about a token the provider issued and the backend refuses. The
count lives in `sessionStorage` under `agp.auth.401count`, is written *before* the reload it authorises, and is
cleared by **any** successful response — a 2xx is the only evidence that actually retires the suspicion, which makes
the budget per-incident rather than per-session. The decision is `decide401` in `frontend/src/api/authRetry.ts`; the
interceptor only does the storage and `window` calls.

Requests go to `VITE_API_URL`, which **already contains the stage segment**, and client paths are absolute — so the
composed URL carries the stage, which is why the application registers every route twice (§12).

> **Known limitation — a structural 401 states itself, but only as an error message.** Reloading recovers *expiry*
> and nothing else: if the cause is structural — mismatched audience, roles on the wrong app registration, the wrong
> scope baked into the SPA build — the fresh token is rejected identically. That used to loop 401 → reload → 401 for
> as long as the tab was open, which was self-obscuring: the page never settled long enough to render the error, so
> the most diagnosable failure in the system presented as a flickering blank page. Bounded since E36/T19 — one
> reload, then every call rejects with *"Sign-in is not working for this deployment… ask an administrator to check
> the token audience, the requested scope, and the app registration holding the role assignments."* What remains is
> that the terminal state **is that message and nothing more**: there is no full-page error screen, so it appears
> wherever the calling surface renders `err.message`, and the count is per-tab (`sessionStorage`), so a new tab gets
> a fresh reload before it halts again. §11 maps the causes.

## 3. The wire path

Four hops; only the first is encrypted.

| Hop | What it is | Transport |
|---|---|---|
| Internet → gateway | API Gateway **HTTP API** (v2), one `$default` route, `HTTP_PROXY` integration | HTTPS — **TLS terminates here** |
| gateway → load balancer | VPC Link into the task security group, over the *public* subnets | plain HTTP, port 80 |
| load balancer → task | target group, health check on `/health` | plain HTTP, port 8000 |
| the task | ECS Fargate, container port 8000 | — |

**The API Gateway has no authorizer of any kind** — no authorizer resource, no JWT configuration, no WAF in any tracked
Terraform file. The only authorizer in the infrastructure is the *agent runtime's* inbound one (§7), a different trust
boundary, so **every authentication and authorization decision for the control plane is made in the application**. No
request-parameter mapping is declared, so the full path — **stage segment included** — reaches it (§12), and access logs
carry request metadata and `$context.error.message` but **no headers, and therefore no tokens**. Two properties follow:
**the bearer token crosses the VPC in cleartext**, since TLS ends at the gateway — a prototype posture a reviewer will
look for; and the load balancer, though internal and admitting only the VPC CIDR, shares *public* subnets with the tasks
— which **have public IPs**, reachable only because their security group admits traffic from the load balancer's group
alone.

## 4. Backend validation

The validator does one thing: take a JWT string, verify it, return the claims.

| Property | Value |
|---|---|
| Issuer | pinned to `https://login.microsoftonline.com/<tenant-id>/v2.0` — single tenant, v2.0 only, no `common`, no v1 fallback |
| Audience | one of **three** accepted values (§4.1) |
| Clock skew | `leeway=JWT_LEEWAY_SECONDS`, default **60 s** — one setting shared by both inbound validators |
| Verifications | signature (**RS256 only**, an allow-list of one — `none`, HMAC and EC/PS refused), `exp`, `iat`, `nbf`, `iss`, `aud`, each switched on explicitly |

Signing keys come from that tenant's JWKS endpoint through a lazily created client. The customary sixty-second grace is
real, and it is honestly symmetric: PyJWT's `leeway` loosens `exp` **and** `nbf`/`iat`, so a token is honored for up to
a minute past its expiry *and* for up to a minute before it becomes valid. That is a security-relevant loosening, taken
deliberately against the blanket 401s a badly skewed container clock produced at zero tolerance; set
`JWT_LEEWAY_SECONDS=0` to restore that. The same setting governs the separate GitHub Actions OIDC validator behind the
build-trigger routes (§12), where skew breaks deploys rather than logins. Rotation is the library's job — the JWK-set
cache lives 300 seconds, so keys refetch roughly every five minutes, and at once when no `kid` matches, so **adding** a
key works. But per-`kid` results sit in an unexpiring cache, so a *withdrawn* key keeps validating until the process
restarts.

### 4.1 The three accepted audiences, and why each

The decode passes if **any** entry matches, and each GUID is appended only when non-empty, so leaving either setting
blank rejects legitimate tokens with a 401. Which value goes where is in the [setup guide](entra-setup.md).

| Accepted value | Setting | Why it is accepted |
|---|---|---|
| the canonical URI, default `api://agp` | `ENTRA_AUDIENCE` | the Application ID URI of the backend app — the documented, intended `aud` |
| the **SPA** app's client-ID GUID | `ENTRA_SPA_CLIENT_ID` | tokens for an app's own exposed scope sometimes carry the client-ID GUID as `aud` instead of the URI form; a safe additive transition for older SPA-scope tokens |
| the **backend** app's client-ID GUID | `ENTRA_BACKEND_CLIENT_ID` | **the current path**: a token for the backend confidential app's own exposed scope `api://agp/Access.Default` carries the backend client GUID as `aud` |

### 4.2 The error taxonomy

Every failure is a 401 whose `detail` is what the browser surfaces (§2). **No `WWW-Authenticate` header is ever set.**

| Cause | `detail` |
|---|---|
| no `kid`, JWKS unreachable, or no key matches | `Invalid token: signing key lookup failed` |
| `exp` in the past | `Token has expired` |
| issuer mismatch | `Invalid issuer (expected https://login.microsoftonline.com/<tenant>/v2.0)` |
| audience matches none of the three | `Invalid audience (accepted ['api://agp', '<spa-guid>', '<backend-guid>'])` |
| bad signature | `Invalid token signature` |
| malformed, wrong algorithm, future `nbf`, non-JWT string | `Invalid token: <ExceptionClassName>` |
| `ENTRA_TENANT_ID` is empty | **not a 401** — a bare `RuntimeError`, so a generic 500 |

Two rows are deliberate. **The issuer and audience errors echo what the backend expected**, which makes
troubleshooting actionable at the cost of disclosing the tenant and client-ID GUIDs to anyone presenting a
wrong-audience token. And **an unconfigured tenant is a 500, not a 401**, because the expected-issuer helper raises
before the JWKS lookup: the client gets a generic internal error while `ENTRA_TENANT_ID is not configured` reaches
CloudWatch only.

### 4.3 Which dependency runs, and how often

There is **no `HTTPBearer` security scheme on the live path**: the RBAC module reads the header itself, once to
extract the role and once to build the principal. So the prefix check is case-sensitive against the raw header (§11),
and **the token is validated per dependency, not once per request** — a gated route declares both a principal
dependency and a role check, each calling the validator, and the current-user endpoint reaches three validations for
one request.

### 4.4 The bypass that is real

> **Read this before reading any auth code here. The live path is `rbac.py` → `security_entra.py`, and nothing
> else** — there is no second module that looks authoritative. There were four: a provider dispatcher nothing
> imported, an `HTTPBearer` facade no route used, a `dev_auth` module whose name and docstring promised the bypass
> while its body returned the admin fixture unconditionally, and a path-prefix-stripping middleware that was defined
> and never added. All four are deleted, so nothing here can be read as the auth path without being it.
>
> The bypass that *does* run is in `rbac.py`, gated on `USE_DEV_AUTH or DEBUG` (both default to `False`) and checked
> *before* the token is validated. It maps dev headers to a role, and **with no headers at all the caller is
> `ADMIN`**. It skips this whole section and **defeats tenant isolation**: the principal carries empty claims, so no
> group memberships, yet it is `ADMIN` and therefore global (§5). Neither flag appears in the ECS task definition, and
> the Compose file is explicit: `USE_DEV_AUTH=true` is for local use, **`DEBUG=true` is not to be set** — but nothing
> stops a deployment setting either, which is why an anonymous `GET $API/api/v1/agents` answering **200** instead of
> **401** means it is live.

## 5. From claims to decisions

Validation proves the token genuine; three gates then decide what the caller may do. **Gate 1, the platform role:**
the `roles` claim maps onto an ordered enum (`VIEWER < OPERATOR < ADMIN`), the highest wins, and nothing recognised
resolves to **`VIEWER`, not a rejection**; each route sets its own minimum. **Gate 2, tenant visibility:** a second,
independent dependency resolves group memberships into a tenant context, and a resource is visible only to a global
caller (which means `ADMIN`), when shared, or when its tenant tag matches — so an **untagged** resource is invisible
to everyone non-global. **Gate 3, per-project roles:** evaluated *after* the tenant gate, so another tenant's resource
answers a 404 **byte-identical** to a missing one's, never a 403 that would confirm it exists. The bypass in §4.4
defeats all three gates. How they compose is the [authorization guide](authorization-layers.md).

## 6. The first exchange: user to agent

At the agent invoke route the inbound token stops being something to *check* and becomes something to *spend*. Two
gates run first (§11): tenant visibility, then a **409** `agent identity is not provisioned` unless the agent is
provisioned and governed on one of the two platforms — AgentCore-hosted or Databricks-governed (E29). Invoking needs
only `VIEWER`. The handler then re-reads the **raw** `Authorization`
header, of necessity: the principal object carries `oid`, `email`, `role` and the claims dict but **not the raw token
string**, so the only source of the assertion is the request, again. The exchange is one HTTP POST to the tenant's
token endpoint:

| Form field | Value |
|---|---|
| `grant_type` | `urn:ietf:params:oauth:grant-type:jwt-bearer` |
| `client_id` / `client_secret` | the **backend** app's client ID and secret |
| `assertion` | the raw inbound user token |
| `scope` | the agent app's Application ID URI plus `/.default` — e.g. `api://agp-agent-<id>/.default` |
| `requested_token_use` | `on_behalf_of` |

**The exchange is performed by the backend app**, with the backend client secret — a *different* app and secret from §8.
**The result is never persisted, logged, or returned to the browser, and never cached:** a fresh exchange every invoke.
And **this is the real user-to-agent enforcement point**, because the provider refuses the exchange when the user holds
no app-role assignment on the agent's service principal — which is why the backend sets assignment-required on every one
it creates. Failures map three ways (§11 catalogues two; anything else answers with status and code alone); only the
`AADSTS50105` one is a governance answer rather than a fault, shown as a calm amber card.

## 7. What the runtime receives

The token reaching the runtime is **the user's On-Behalf-Of, agent-audience token** — not a workload token and not the
raw browser token. The call is a plain HTTPS POST, deliberately: a runtime with a JWT inbound authorizer rejects
request signing. It carries the bearer token and a session id to the invocation endpoint, times out after 120 seconds,
and puts the user's `oid` in the *payload*, because the edge authorizer consumes the `Authorization` header and the
agent therefore cannot decode the token the backend already validated. **That inbound gate** is a custom JWT
authorizer configured with the tenant's OIDC **discovery URL** and an allowed-audience list — no allowed-clients list,
no custom-claim matching, so **audience-only**. The list carries **both** forms, the `api://` URI *and* the app's
client-ID GUID, because the exchanged token's real `aud` can be either; both are per-agent, so cross-agent replay is
refused either way, and the *scope* still uses the URI form, because scope is not audience. Terraform sets the same
configuration at create time, so a provisioned runtime is born gated, and the backend fans the authorizer across
**every** per-stage runtime it did not create, because a runtime with no authorizer configuration accepts
**unauthenticated** invocations. Every update is a full replace, so both writers replay it; losing it reopens the gate.
Which runtime is reached depends on the optional `?stage=`: given one, that stage's runtime is invoked, and a stage the
agent owns no runtime for gets a **404 `unknown stage`** instead of another stage's runtime; omitted, the agent record's
scalar ARN decides — **by contract, whichever stage deployed last**, so omitting the parameter while believing you are
invoking dev can reach prod.

## 8. The second exchange: agent to MCP server

Inside the runtime the agent needs a token for an MCP server and gets one **without ever holding a secret**. It asks
AgentCore Identity to run it, with a resource-token call whose OAuth2 flow is the On-Behalf-Of exchange, a
credential-provider name, the target scopes, and a runtime-injected **Workload Access Token**, bound to the user who
made the inbound call, as the assertion. The flow type is hardcoded, because the SDK's decorator helpers **cannot**
express On-Behalf-Of. **The identity here is the agent's own app registration**, and its secret lives in the AgentCore
**Token Vault**: minted once, at grant time, by a Graph password-add call and passed straight into the credential
provider's configuration — never written to Secrets Manager, never persisted, never logged, never returned. Only the
provider **name** is recorded. The two exchanges therefore differ on every axis:

| | §6 — user to agent | §8 — agent to MCP server |
|---|---|---|
| Who runs it | our own backend code, one HTTPS POST | AgentCore Identity, inside the runtime |
| Client identity | the **backend** app registration | the **agent's own** app registration |
| Secret used | backend client secret (environment / Secrets Manager) | agent app secret, held only in the **Token Vault** |
| Assertion | the raw inbound user token | the runtime-injected Workload Access Token |
| Resulting audience | the per-agent app (`api://agp-agent-<id>`) | the per-MCP app (`api://agp-mcp-<id>`) |
| Caching | none — fresh on every invoke | the Workload Access Token is fetched **once per invoke** and reused; the MCP-audience token is minted **per MCP, per invoke** |

The runtime's environment carries three things and no credentials: an `MCP_SERVERS` list, a
`CREDENTIAL_PROVIDER_NAME`, and the legacy single-MCP keys emptied. **No IAM credentials, no client secret and no
token are ever injected into the runtime environment.** The minted token opens the connection, one client per gateway,
every tool namespaced per server. Grants live in **exactly one place**: app-role assignments on the *resource* service
principal, written through Graph, with **no mirror in any platform database**. The `mcp_server_ids` list is **desired
state, not the decision**, so a stale entry fails closed and revocation is real — removing the consent stops the
exchange at the provider.

> **Known limitation — a revoked agent-to-MCP consent produces no user-visible signal.** When the exchange fails the
> agent logs a warning, **drops that MCP server for the invoke**, and answers with the remaining tools: a plausible
> answer, **no error**. Governance-correct — it fails closed and the revoke path relies on it — but the only evidence
> is a log line inside the runtime.

## 9. The gateway, and Cedar per tool call

Every gate so far is **per token** — the consent in §8 and the gateway's inbound authorizer are decided once, when the
connection opens, and that connection serves every tool call the model makes. Cedar is the only gate that fires **per
tool call**, and **the gateway evaluates it; the backend never does** — the gateway's service role holds the
policy-engine read and authorize permissions, found nowhere else. A policy matches the **principal** on an `oid`
**tag** carrying the inbound token's `oid` claim (design intent, not proven in-repo) — which is why §8's exchange must
preserve the user: a machine-to-machine token matches no policy. Cedar is opt-in per gateway and a gateway with no
policies has enforcement mode `none`, but the **first** policy attaches the engine in enforcing mode, and Cedar is
default-deny — so one "allow this user to call this one tool" click flips that whole gateway to deny-by-default for
every other user and tool. And **a deny is nothing user-facing**: it surfaces as a tool error inside the agent's
reasoning loop, and nothing turns it into a governance message. Policies live **only** in the Policy Engine, never in a
platform database. What a policy can express is the [tool-policy governance guide](cedar-tool-policies.md).

## 10. What is never logged

Not promises — mechanisms; four of them, each preventing a different class of leak. **1. The wire-logger floor.**
Startup clamps the AWS SDK, HTTP and transfer libraries to a **minimum** effective level of `INFO`, because
`LOG_LEVEL` is operator-settable and configures the *root* logger — and the AWS SDK's response parser logs every
response body verbatim. Since a Secrets Manager response body **is** the secret, one `LOG_LEVEL=DEBUG` deployment
would write live credentials to CloudWatch. A test pins the *effective* levels here and in each agent.

**2. The redaction boundary between the two provider error paths** — asymmetric on purpose. The token-endpoint path,
covering the application token and **both** On-Behalf-Of exchanges, carries **status and error code only, never the
body**, because `error_description` can echo the assertion or the client secret back at you. The resource-endpoint
path *may* carry the provider's message, since those endpoints never echo the `Authorization` header. The stated
contract: no client secret, inbound token or exchanged token is ever logged or put in an exception message.

**3. The no-token-logging guard.** A test is regression cover for a real incident: a reference agent logged raw
On-Behalf-Of bearer tokens **and inbound user JWTs** at `INFO`, putting live unexpired credentials in CloudWatch. It
**parses** each file rather than grepping, so a value position reads differently from prose, and it refuses truncated
and **hashed** tokens while allowing token *counts*. It scans the reference agents, **not** the backend.

**4. Fixed literals instead of exception strings.** Health probes log a stack trace but put a **fixed** literal in the
body — `error: database probe failed` / `error: s3 probe failed` — because `/health` is public and `str(e)` there once
exposed bucket names, table ARNs and account ids. Its test asserts equality with that literal, since a blocklist only
catches known leaks. The agent path follows the same convention, and only an AgentCore error *code* of plain letters
may surface — a real access-denied message names the role ARN and account id.

## 11. Failure catalog

| Hop | Symptom | What it means | Where the truth is |
|---|---|---|---|
| sign-in | `AADSTS50011` | registered redirect URI and `VITE_ENTRA_SPA_REDIRECT_URI` differ, trailing slash included | the provider's redirect, not any platform log |
| sign-in | `AADSTS50105`, no token issued | assignment-required is on and the user holds no platform role | provider-side; see the [setup guide](entra-setup.md) |
| gateway | never a 401 | no authorizer exists here (§3); it only 5xx's on integration failure | the gateway access log's `$context.error.message` |
| validation | `401 Missing authorization token` | no header, wrong case (`bearer`), or `Basic` — the check is case-sensitive | the request's own header; nothing logs it |
| validation | `401 Invalid audience (accepted [...])` / `Invalid issuer (expected …)` | audience or tenant mismatch; **the body lists what was expected** | the response body itself (§4.2) |
| validation | `401 Token has expired` | genuine expiry — or a clock skewed by more than `JWT_LEEWAY_SECONDS` (60 s) | the token's `exp` against the container's clock |
| validation | `500` on every authenticated call, `/health` fine | `ENTRA_TENANT_ID` was never set | CloudWatch only: `ENTRA_TENANT_ID is not configured` |
| SPA, any 401 | the page reloads **once**, then every call fails with *"Sign-in is not working for this deployment"* | a structural (not expiry) 401 — the one-reload budget gave up (§2) | the 401 body; the browser network tab |
| authorization | `403 Requires operator role or higher` | role below the route's minimum — often roles assigned on the SPA app registration | nothing logs it; inspect the token's `roles` claim |
| tenant scope | `404 "Agent not found"` | missing **or** another tenant's — byte-identical by contract (§5) | nothing distinguishes them by design; check the resource's tenant tag |
| invoke | `409 agent identity is not provisioned` | `identity_status` is not `provisioned`, or the agent is governed on neither platform (not AgentCore-hosted, not Databricks-governed) | the agent's registry record |
| exchange 1 | `403 <who> is not assigned to this agent` | `AADSTS50105` — no user-to-agent grant. A governance answer, not a fault | the agent service principal's app-role assignments |
| exchange 1 | `500 agent identity is misconfigured — re-provision` | `AADSTS65001` or `500011` — the backend-to-agent consent is missing | backend logs: status and code only, never the provider body |
| Databricks exchange | `502 federation_exchange_failed` | the workspace refused the RFC 8693 exchange — auth server unreachable, or the federation policy's issuer/audience is wrong | the workspace's federation policy; backend logs (code only) |
| Databricks exchange | `502 federation_unavailable` | the tenant cannot federate Entra tokens — the capability probe answered no | the tenant page's capability badges |
| Databricks exchange | `502 sp_secret_disabled` | the record carries the dormant `sp_secret` binding and this deployment keeps it off | `DATABRICKS_ALLOW_SP_SECRET_BINDING`; the agent's binding badge |
| Databricks exchange | `502 workspace_stage_unresolved` | the invoke's stage has no workspace in the tenant's config | the tenant's stage set |
| Databricks exchange | `502 invalid_runtime_handle` | the stored App URL failed validation or lookup | the agent's runtime handle on its detail page |
| Databricks exchange | `502 binding_mode_unresolved` | the agent's `binding_mode` is empty or unrecognized | the agent's registry record |
| runtime (AgentCore) | `502 agent rejected the token` | the runtime's JWT authorizer refused: wrong `aud`, expired, or authorizer unwired | the runtime's authorizer configuration |
| runtime (AgentCore) | `504 agent invocation timed out` (120 s) or `502` transport / non-2xx | the runtime was unreachable, slow, or errored | backend logs, then the runtime's own logs |
| exchange 2 (AgentCore) | **an answer with fewer tools, and no error** | consent revoked, or the gateway refused the MCP token — that server is silently degrade-dropped | a warning log inside the runtime |
| tool call | the model reports a tool failure | a Cedar deny; nothing shapes it into a governance message (§9) | the gateway's policy set and enforcement mode |

## 12. Deliberately public endpoints

Six paths answer with no bearer token. They are a decision, not a misconfiguration, and finding them open is not a
finding: `/` and `/ping` are liveness banners, `/health` is what the load balancer probes, and `/docs`, `/redoc` and
`/openapi.json` describe the API surface while exposing no secrets and no data. **Everything else requires a valid
bearer token** — every other route carries a role gate or a direct validation call, and the build-trigger routes
authenticate differently: GitHub's own OIDC tokens, a fixed audience, no platform role mapping. Because the gateway
forwards the stage segment intact, every router is registered twice, bare and under `ROOT_PATH`, plus **one**
hand-written stage twin for `/` — the only public endpoint declared on the app rather than on a router, so the only
one with nothing to fall back to. Registration order is load-bearing, and it is why the other twins are gone: declared
before the prefixed router, a twin **shadows** it, and both `{ROOT_PATH}/health` and `{ROOT_PATH}/ping` used to answer
from a hard-coded body instead of the route they name. Only **three** of the six answer under the prefix:
`{ROOT_PATH}/` from the twin, and `{ROOT_PATH}/health` and `{ROOT_PATH}/ping` from the prefixed router — the real
probe logic. So **`{ROOT_PATH}/health` is the only internet-reachable health URL**, the bare one internal-only, while
the other three exist at bare paths only and `$API/docs` returns **404 by design**.

*Derived from the frontend `auth/` and `api/` modules, the backend `core/`, `services/` and `api/routes/`, and
`infrastructure/modules/`.*
