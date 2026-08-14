# Registering an agent or an MCP server

Registration is what turns a thing somebody built into a thing the platform governs. This document
explains what actually happens: which record is written where, which identity objects are minted and
*when*, which state machines exist (there are four, and only one of them is the one everybody talks
about), and where the AgentCore-specific machinery starts.

There are **two registration paths**, and they are not variants of each other:

| | "Declare" | "Materialize" |
|---|---|---|
| Entry point | `POST /api/v1/agents` | `POST /api/v1/projects/{id}/repos` |
| Route | `platform/control_plane/backend/src/api/routes/agents.py:327` | `platform/control_plane/backend/src/api/routes/projects.py:797` |
| Identity minted | only if the agent qualifies (see [§4](#4-the-identity-minting-timeline)) | always, as an explicit step |
| Repository, container, runtime | none created | repository now; container and runtime minutes later, from CI |
| Caller must already own | a live AgentCore Runtime ARN | nothing — the runtime does not exist yet |

Both paths call the same `AgentRegistryService.create`, write the same kind of record into the same
registry, and produce an agent whose lifecycle state starts at `proposed`. The difference is whether the
platform also *builds and deploys* the thing.

**Contents:** [1. The record](#1-the-record) · [2. The lifecycle state
machine](#2-the-lifecycle-state-machine) · [3. What approval actually
gates](#3-what-approval-actually-gates) · [4. The identity-minting
timeline](#4-the-identity-minting-timeline) · [5. The declare path](#5-the-declare-path) ·
[6. The materialize path](#6-the-materialize-path) · [7. MCP servers](#7-mcp-servers) ·
[8. Grants](#8-grants) · [9. Hosting-agnostic versus
AgentCore-specific](#9-hosting-agnostic-versus-agentcore-specific) ·
[10. Known limitations](#10-known-limitations)

---

## 1. The record

An agent is a **record in an AWS Agent Registry**. There is no local database row for it: every HTTP request
re-hydrates a fresh `Agent` object from the registry record, so the registry is the only place it exists.

Two registries are involved, one per kind of thing: `agp-agents` holds one `CUSTOM` record per agent
(`platform/control_plane/backend/src/services/agent_registry_service.py:55`) and `agp-mcp-servers` holds
one `MCP` record per MCP server
(`platform/control_plane/backend/src/services/mcp_server_service.py:63`).

### The registry is addressed by name, not by id

There is no registry-id Terraform variable to read back and paste anywhere. The backend holds only a
**name** — `AGENT_REGISTRY_NAME` / `MCP_REGISTRY_NAME`
(`platform/control_plane/backend/src/core/config.py:80,85`) — and resolves it to a registry id on first
use, then memoises it (`agent_registry_service.py:227`). Resolution is a `ListRegistries` call plus a
client-side name match, because the API's filters cannot filter on name
(`platform/control_plane/backend/src/core/registry_resolver.py`). Three properties of that resolver are
deliberate: a failed lookup is **never** cached, so a transient credential or throttling error cannot
poison the process until it is restarted; two viable registries with the same name is a **hard error**,
not first-match-wins; and `AGENT_REGISTRY_ID` / `MCP_REGISTRY_ID` remain as explicit overrides that
short-circuit resolution entirely, because several operational scripts pass ids directly
(`config.py:79,84`). Empty — the default — means "resolve by name".

### What is stored, and who owns each fact

The record carries the platform's governance model inside one descriptor as a JSON string — the **envelope** —
built and parsed by `Agent.to_envelope()` / `Agent.from_record()` (`models/agent.py:406,480`).

| Fact | Lives in | Cited |
|---|---|---|
| Existence, id, name, description, timestamps, native status | the registry record's own fields | `models/agent.py:559` |
| `lifecycle_state` | **derived, never stored** — mapped from the native status | `models/agent.py:145-183`; `to_envelope` omits it (`:474`) |
| Governance metadata (sponsor, business unit, region, classification, platform, framework, `model_id`, tenant, owning project, marketplace attestation) plus the **runtime handles** — the scalar `agent_arn`, body-settable on create *and* update, and the `agent_arns` stage-to-ARN map, settable on create but deliberately absent from `AgentUpdate` (AgentCore); `runtime_handle`, `runtime_kind`, `binding_mode`, `databricks_sp_id`, `databricks_sp_secret_arn`, `oauth2_app_client_id` (Databricks, E29/T5) | the envelope | `models/agent.py:259-309`, `:474-557` |
| Service-written handles: identity (`entra_app_id`, `entra_sp_id`, `entra_app_audience`, the two role ids, `identity_status`) and observability (`langfuse_project_id`, `langfuse_key_secret_name` — the key *values* live in Secrets Manager and never touch the record) | the envelope | `models/agent.py:356-403` |
| Who may invoke an agent, and which MCP servers it may call | the identity provider's directory — **no local mirror** | `platform/control_plane/backend/src/api/routes/grants.py:1-6` |
| Projects, repositories, deployment history | [DynamoDB](data-model.md#2-every-dynamodb-table) | `services/project_service.py` |

### The record id is the agent id

`CreateRegistryRecord` does not return a record id, so the platform parses it off the returned ARN
(`agent_registry_service.py:269`) and that value **is** the agent id (`:444`). The `agent_id` key inside
the envelope is a debug mirror — if the two ever disagree, the record id wins.

Two create-time facts shape everything downstream. **The name-uniqueness check is racy by design** — it
is a `ListRegistryRecords` filter, not a conditional put, so two concurrent creates of the same name can
both pass (`agent_registry_service.py:274`). And **a fresh record is briefly unmodifiable**: it comes back
`CREATING`, so `create()` polls it to `DRAFT` and derives the lifecycle state from that freshly observed
status rather than the stale create response (`agent_registry_service.py:349,450`). Finally, the registry
has **no tenant concept** — tenant filtering happens in the backend after the list call returns
(`api/routes/agents.py:386-406`).

---

## 2. The lifecycle state machine

The platform's vocabulary is five lowercase states — `proposed`, `pending_approval`, `approved`, `rejected`,
`deprecated` (`models/agent.py:33-39`) — imported rather than re-declared for MCP servers, so both kinds
share one machine (`platform/control_plane/backend/src/models/mcp_server.py:27-39`). **They are derived,
never stored:** a total mapping over the registry's own native status enum (`models/agent.py:134-144`).

| Native registry status | Platform lifecycle state |
|---|---|
| `DRAFT` | `proposed` — the state a fresh record lands in |
| `PENDING_APPROVAL` | `pending_approval` |
| `APPROVED` | `approved` |
| `REJECTED` / `DEPRECATED` | `rejected` / `deprecated` — both terminal |
| `CREATING`, `CREATE_FAILED` | `proposed` — transient, collapsed onto the nearest pre-live state |
| `UPDATING`, `UPDATE_FAILED` | `approved` — transient, collapsed onto the nearest post-live state |

> **Naming trap.** `DRAFT` maps to `proposed`, not to "draft". The uppercase names are the AWS-native status
> strings; routes, API responses and the UI all speak the lowercase set. An unmapped native status raises a
> named error rather than a bare `KeyError`, because the registry is a Preview service and can grow new
> statuses (`models/agent.py:154-172`).

### The transitions

| Edge | Route | Platform role | Registry call | Side effects |
|---|---|---|---|---|
| — → `proposed` | `POST /agents` (`agents.py:327`) | Operator | `CreateRegistryRecord`, then poll to `DRAFT` | the record; conditionally an identity ([§4](#4-the-identity-minting-timeline)); an observability project |
| `proposed` → `pending_approval` | `POST /agents/{id}/submit` (`agents.py:660`) | **Operator** | `SubmitRegistryRecordForApproval` (`agent_registry_service.py:616`) | none |
| `pending_approval` → `approved` | `POST /agents/{id}/transitions` with `{"action":"approve","reason":"…"}` (`agents.py:674`) | **Admin** | `UpdateRegistryRecordStatus` (`agent_registry_service.py:627`) | none |
| `pending_approval` → `rejected` | same route, `"action":"reject"` | Admin | same | none |
| `approved` → `deprecated` | same route, `"action":"deprecate"` | Admin | same | **the only transition with a side effect** — a published marketplace listing is rewritten with `published=False` (`agent_registry_service.py:657-678`) |

Mechanics worth stating precisely:

- **Exactly three verbs exist** — `approve`, `reject`, `deprecate` (`models/agent.py:147-151`); any other
  action string becomes a 400. **A non-empty reason is mandatory**, because the registry API requires it
  (`agent_registry_service.py:633`).
- **AWS enforces the legal edges, not the platform.** There is no transition table in the backend; an
  illegal edge (`DRAFT → APPROVED`, say) comes back as a `ValidationException`, mapped to a domain error
  and returned as **409** carrying the AWS message (`agent_registry_service.py:644-655`).
- **The transition routes carry [platform roles](authorization-layers.md#gate-1--the-platform-role) only**, with no per-project role gate — deliberately: a
  lifecycle decision belongs to the registry, not to a project (`api/routes/agents.py:24-26`). All of them
  load the agent through the tenant-visibility helper first (`agents.py:279`), so a caller from another
  tenant gets a byte-identical `404 Agent not found`, never a 403 that would confirm it exists.
- Deprecation **sticks**: only the `DEPRECATED` target touches the marketplace block, so only a fresh
  publish request can re-list a product, never a later re-approval.

---

## 3. What approval actually gates

Plainly: **marketplace publication and list filtering. Nothing else.**

That is the most important structural fact about registration, so here is the evidence. Every reader of
`lifecycle_state` in the backend is one of three: the optional list-query filter and the code applying it
(`api/routes/agents.py:390,397`, `api/routes/mcp_servers.py:257,264`, `agent_registry_service.py:460-485`,
`mcp_server_service.py:555-578`); the marketplace publish and subscribe gate, where a product must be
`approved` (`platform/control_plane/backend/src/services/marketplace_service.py:288,386,841,961,1169`); and a
value inside a governance-graph node's `metadata`
(`platform/control_plane/backend/src/services/governance_graph_service.py:144`) — the node label is `a.name`,
`:140`. Nothing else reads it: the build path, the deploy path, the invoke handler, the grant routes and the
runtime-environment writer never mention it.

The field that **does** gate behaviour is `identity_status` — a pinned four-value enum,
`IdentityStatus`: `none | pending | provisioned | failed` (`models/agent.py:72-91`), declared once and
shared by the MCP-server model. It subclasses `str`, so the gates below still compare it to a bare
string, and the stored form is always the value. Reads remain tolerant by design — an unrecognised
stored value coerces to `none` with a warning rather than raising (`models/agent.py:94-124`), because
the build writes this field into the envelope with `jq` (see the writer table below) and `none` fails
every gate closed. `POST /agents/{id}/invoke` returns **409**
unless it is `provisioned` *and* the agent qualifies as an AgentCore agent
(`api/routes/agents.py:871-872`), and creating a grant returns **409** unless it is `provisioned` and a
service-principal id exists (`api/routes/grants.py:176-241`).

So an agent sitting at `proposed` — never submitted, never approved — can be built, deployed, invoked and
granted, provided its identity was provisioned; an agent at `approved` whose identity failed can do none of
those things. Frame it that way when you operate the platform: lifecycle state is an **attestation** of who
declared what and who signed it off, the marketplace is the one surface that enforces it, and the operational
gate is identity. Two further state fields are orthogonal to both — a live runtime-status probe that is
never persisted (`models/agent.py:574-617`) and the repository's own `cicd_status`
(`models/repository.py:129-130`) — which is why four state machines coexist here and only one of them is
called "lifecycle".

---

## 4. The identity-minting timeline

Microsoft Entra ID is [the identity provider supported today](entra-setup.md). The platform mints one app registration and one
service principal per agent and per MCP server; users are directory users it never creates or modifies. What
matters here is **when** each artifact appears — [what each does at call time](token-propagation.md) is a separate concern.

### The gate: which agents get an identity at all

An agent qualifies only when **all three** of these hold: it names a runtime ARN, its auth type is the
identity provider, and its platform is AgentCore (`models/agent.py:384-392`; the module-level wrapper is
`platform/control_plane/backend/src/services/agent_identity_service.py:112`). A metadata-only agent — no
runtime ARN — gets **no identity at all** and keeps `identity_status = "none"`. This is the one place
hosting-specificity leaks into an otherwise hosting-agnostic registration path;
[§9](#9-hosting-agnostic-versus-agentcore-specific) treats the consequences.

### What fires when

| Moment | Code | What runs |
|---|---|---|
| `POST /agents` returns 201 **and** the agent qualifies | `api/routes/agents.py:447-450` | identity provisioning, as a background task **after** the response is sent — dispatched by platform (E29/T6) |
| `POST /agents` returns 201, unconditionally | `api/routes/agents.py:455-459` | observability provisioning **and** runtime wiring, as a background task — see [below](#observability-for-a-registered-agent) |
| The materialize path reaches its `mint_identity` step | `project_service.py:1764` | identity provisioning **only** — no runtime work |
| `POST /agents/{id}/reprovision` (202) | `api/routes/agents.py:898-937` | flips `identity_status` to `pending` and **persists first**, then schedules provisioning — accepts either governed platform (`:932-933`) |
| The build finishes `terraform apply` | `platform/control_plane/infrastructure/modules/codebuild/buildspec.yml:525` | writes `identity_status="provisioned"` straight into the envelope |
| An agent is granted access to an MCP server | `platform/control_plane/backend/src/services/agent_mcp_grant.py:204` | mints a client secret for the agent's app and creates its credential provider |

Note the fourth row: on the materialize path it is the **build**, not the backend, that finally marks an
agent's identity provisioned. Identity provisioning alone leaves the record at `pending`, and nothing in
the deploy path re-enters the backend to change that.

### The sequence, and what is persisted between steps

`AgentIdentityService.provision` (`agent_identity_service.py:197`) is identity provisioning, then —
**only if the agent already names at least one runtime** — the runtime half (`:335`). That gate reads
the resolved stage-to-ARN map, not the bare scalar: with the scalar alone, an agent whose runtimes were
known only from the map received zero authorizer calls and was stranded at `pending` with no error — an
unauthenticated copy of a governed agent (`agent_identity_service.py:217-239`).

Identity provisioning (`agent_identity_service.py:268`) runs four steps:

1. **Skip if already minted.** A present service-principal id skips the directory writes entirely, which
   is what makes reprovisioning idempotent.
2. **Create the app registration and its service principal** through one shared builder
   (`platform/control_plane/backend/src/services/graph_service.py:434`, entered at `:394`): single-tenant
   sign-in audience, an identifier URI of `api://agp-agent-<record id>` (prefix from `config.py:169`),
   access-token version 2, one delegated `Invoke` scope, and two app roles, `Invoker` and `Admin` — for an
   agent those allow member type `User` only. A duplicate identifier URI is treated as get-or-create.
3. **Persist immediately.** The app id, service-principal id, the two role ids, the audience and
   `identity_status="pending"` are written before anything else happens
   (`agent_identity_service.py:320-325`), because the sequence is non-atomic: if a later step failed first,
   the failure path would store a null service-principal id, the skip-guard above would create the app
   again, and the duplicate identifier URI would then fail **forever**.
4. **Require assignment, then consent the backend to the agent.** The service principal is set to require
   app-role assignment (`graph_service.py:692`, called at `:322`) — for a user, that assignment *is* the
   admission gate — and a delegated-permission grant for the `Invoke` scope, backend to agent, is written
   idempotently (`graph_service.py:723`).

The runtime half fans out over **every** runtime the agent names, wires each one's inbound JWT authorizer
(`agent_identity_service.py:618,635`), then flips `identity_status` to `provisioned`. It attempts all
runtimes and *then* raises if any failed — one unreachable runtime must not stop the others, but an agent
reported `provisioned` while one of its runtimes still accepts unauthenticated invocations is the dangerous
outcome (`agent_identity_service.py:618-651`). Any exception in the sequence persists
`identity_status="failed"` and raises, and a failure to persist the failure never masks the original error
(`agent_identity_service.py:242-266`).

### The MCP inversion, and why it is not a bug

An MCP server's service principal is set to **not** require app-role assignment — explicitly `False`,
not merely skipped (`platform/control_plane/backend/src/services/mcp_identity_service.py:310-326`). The
reason is live-confirmed and worth internalising before anyone "fixes" it: in the delegated exchange
that lets an agent call an MCP server, the identity provider evaluates the *resource* app's assignment
requirement against the **user**, not against the calling agent. The platform never assigns users to MCP
servers — a user is granted the *agent*, and the *agent* is granted the MCP server via a consent grant —
so requiring assignment there would block every legitimate user by design. The admission gate that
remains is the per-agent consent grant, and `False` is written explicitly so that reprovisioning a
service principal somebody flipped by hand converges back to it.

> Any statement that the platform sets assignment-required on every service principal it creates is
> true for agents and **wrong for MCP servers**. Generalising it breaks a working system.

### Artifacts that arrive later

Three artifacts arrive at **grant** time, not registration time: the per-agent credential provider in the
AgentCore Identity token vault (`agent_mcp_grant.py:204` →
`platform/control_plane/backend/src/services/agent_credential_service.py:97`), whose creation mints a client
secret for the agent's app (`:142`) and hands it straight to the vault (`:148`) — never persisted, never
logged, never returned, with only the provider *name* landing on the record; the agent-to-MCP consent grant
(`agent_mcp_grant.py:179`); and the runtime environment injection carrying the granted MCP list and provider
name (`agent_mcp_grant.py:248`). All three have a consumer in the shipped scaffold: the template parses the
injected MCP list, asks AgentCore Identity for a user-preserving token per server through the named
credential provider, and calls each gateway with it
(`platform/control_plane/agent-templates/strands-agentcore/src/mcp_runtime.py` and that template's
`src/main.py`) — which is also why the provider *name* on the record is enough and the secret can stay in
the vault.

### Observability for a registered agent

Registration also gives the agent its own observability project: one agent, one project, one key pair, so
traces are attributed structurally rather than by tag. The hook runs as a background task after the 201 and is
**best-effort by design** — a Langfuse outage must not fail a registration — so every failure is a logged
no-op that leaves the two join fields unset, and a later re-run is idempotent
(`api/routes/agents.py:166-209`, `services/langfuse_provisioning.py`).

Four things happen, and the last three exist because a registered agent's runtime is **not ours to deploy**.
For an agent the platform builds, the two variables its tracing SDK reads are written declaratively by the
runtime module; for an agent that arrives with a runtime ARN, nothing was ever writing them.

1. **The owning account is parsed from the runtime ARN.** Equal to the control plane's own account — the
   single-account shape that ships — nothing changes: the secret is created with the backend's ambient
   credentials, exactly as before. The check precedes the tenant lookup deliberately, because the default
   tenant's deploy role also lives in the control-plane account, so consulting the stages first would make
   every single-account registration depend on an `AssumeRole` and on the tenant store being readable.
2. **A different account** is matched against the account inside each of the tenant's per-stage
   `deploy_role_arn`s — the only field naming a credential the platform can actually assume — and the secret is
   created *there*, through the same cross-account seam the teardown cascade uses
   ([`services/tenant_credentials.py`](tenant-account-onboarding.md)). No stage owns that account, the tenant
   cannot be read, or the assume fails: the secret is created in the platform account with a warning naming
   the agent, and registration still succeeds. That is a knowing degradation — the runtime will not be able to
   read a key held in an account it has no principal in — not a silent one.
3. **The runtime is told.** `LANGFUSE_HOST` and `LANGFUSE_SECRET_NAME` are merged onto the runtime's
   environment through the same idempotent full-replace writer the grant path uses
   (`agent_identity_service.py:785`), under a client resolved for the runtime's own account.
4. **The runtime is allowed to read that one secret.** A resource policy on the secret grants
   `secretsmanager:GetSecretValue` to the runtime's execution role — the role ARN the environment write
   already read, so the grant costs no extra call. Required across accounts, where an identity policy alone
   cannot reach the secret, and a harmless narrower restatement within one.

The order of the last two is worth knowing: the environment lands first, because the role the grant names is a
by-product of the environment write's own read of the runtime. A runtime that restarts in that window can see
a secret name it is not yet allowed to read, and recovers on the next attempt.

### Teardown: what deleting the record reclaims

`DELETE /api/v1/agents/{id}` (`api/routes/agents.py:627-655`) is not record-only. For a **registered** agent —
one that arrived through `POST /agents` and therefore has no repository behind it — the record is the only
pointer the platform holds to what registration minted, so those artifacts are torn down first, in this order
(`agents.py:512`):

1. **The token-vault entry.** The `agp-agent-obo-<record id>` credential provider
   (`agent_credential_service.py:170`), deleted first, because the Entra application is what its stored
   clientId/clientSecret points at. Its own line-item on this path, so a vault entry that survives is reported
   rather than hidden inside a deleted identity.
2. **The identity.** The Entra application, whose deletion cascades its service principal and every consent
   granted on it (`agent_identity_service.py:435` → `services/graph_service.py:970`). This is the same method
   the repository cascade calls, so the two delete paths cannot drift; that cascade keeps the vault entry
   bundled into this leg, because there the item is blocking.
3. **The observability project.** The Langfuse project and the Secrets Manager secret holding its keys, the
   secret deleted in the account that *holds* it — resolved exactly as registration resolved where to create
   it. Where that account cannot be resolved, the item is reported **failed** rather than deleted: a
   "not found" answered by the control-plane account says nothing about a tenant account.

Each leg is best-effort and reported per resource — `deleted`, `failed` or `skipped` — and **none of them can
block the record delete**. The record is what the operator asked to remove, every leg is idempotent, and
refusing the delete would trap the row behind the very orphans the delete exists to reclaim. What that choice
costs is stated in [§10](#10-known-limitations). The reason vocabulary is the per-item repository delete's:
`assume_role_failed:` when the owning account is known and could not be entered, `stage_unresolved:` when it
could not be determined at all, and the failure's type name for everything else — three different operator
actions, which is why they are not one string.

An agent a **repository** owns is deliberately untouched by this cascade: its teardown is the per-item
repository delete, which reclaims more than this can — runtime, image, execution role, Terraform state — from
an operator-selected item set (`api/routes/projects.py:1122`). Ownership is asked of the repository partition
(`project_service.py:650`), not inferred from a blank `project_id`: that field is also blank on every envelope
predating per-project roles, and tearing one of *those* agents down would strip a live agent's identity and
observability from a Maintainer-gated route while its repository and runtime survived. An ownership question
that cannot be answered — an unreadable store — skips every leg rather than guessing, and the record still
goes.

---

## 5. The declare path

`POST /api/v1/agents` (`api/routes/agents.py:327-383`) registers an agent that already exists
somewhere. Its gates, in order: platform role Operator (`agents.py:333`); [the named tenant must exist](tenant-account-onboarding.md), or
**400** `unknown tenant`; for a non-global caller that tenant must be one of their own memberships, or
**403** `tenant not permitted` — 403 rather than 404 because no resource exists yet to hide; then a blank
sponsor is back-filled from the calling principal (`agent_registry_service.py:70-84`), as on the other path.

`name` and `tenant_id` are the only required fields (`models/agent.py:226-281`). Everything else —
purpose, sponsor, business unit, business region, data classification, platform, framework, `model_id`,
endpoint URL, auth type, runtime ARN — is optional governance metadata. Several fields are **deliberately
not accepted from a request body**, anywhere: the owning project (a server-side keyword only, so nobody can
plant a record into someone else's project), the marketplace attestation, and every identity and
observability handle. The runtime map `agent_arns` sits between the two: a create body may carry it, an
update body may not (`:304-309`). Unknown body keys are silently dropped (`models/agent.py:271-278`).

> **Known limitation — a model choice is write-once.** `AgentUpdate` (`models/agent.py:283-309`) has
> no `model_id`, so no route can change an agent's model after registration. The stored value is
> preserved by the read-modify-write update (`agent_registry_service.py:501`) but is unreachable, and
> the build reads it off the envelope at deploy time (`buildspec.yml:295-306`). Changing an agent's
> model today means registering a new agent.

### What the registration wizard collects

`platform/control_plane/frontend/src/components/governance/AgentRegistrationWizard.tsx` walks five steps:
Identity, Sponsor, Classification, Platform, Confirm. As of E29/T8, it is **discovery-driven**
(`agentRegistrationWizardModel.ts`): the operator picks a tenant (which is platform-typed), the wizard
calls `runtime_catalog.py` to discover that platform's runtimes, and the operator picks one from the list;
manual entry survives as a fallback for runtimes AGP cannot currently reach. `platform` is **inferred**
from the tenant, no longer an input — an operator cannot pick the wrong platform, and runtime handles
(`agent_arn` for AgentCore, `runtime_handle` for Databricks) come from the catalog, not from typing. Two
fields are still missing: there is **no model field** (so a manually registered agent always has no
`model_id`), and **no MCP-server field** (MCP wiring is post-registration). The wizard is also not where
you pick who may invoke the agent or which tools it may reach: grants are a separate surface
([§8](#8-grants)) and tool policies belong to the MCP server, not to the agent.

---

## 6. The materialize path

`POST /api/v1/projects/{id}/repos` (`api/routes/projects.py:797-864`) is the path that creates
something. It returns **202** and finishes the work in a background task.

[Gates](authorization-layers.md): platform role Operator, then tenant visibility on the project (404 if not visible), then the
project-level Maintainer role. A project with **no role rows at all** falls back to allowing any
tenant-visible caller for maintainer-level verbs — the "ungoverned project" fallback
(`api/routes/projects.py:205-233`).

Validation runs strictly before any side effect (`project_service.py:615-749`). `agent_config` must carry
two validated keys: `framework` equal to `strands` — the only supported scaffold — and an `agent_name`
matching `^[a-zA-Z][a-zA-Z0-9_]{0,31}$` (`platform/control_plane/backend/src/models/project.py:44-77`).
**That 32-character cap is arithmetic, not taste:** `agent_name` is the stem of two account-global AWS
names, `{agent_name}_{stage}` capped at 48 and `{agent_name}-{stage}-agentcore-exec` capped at 64
(`platform/control_plane/infrastructure/modules/agentcore_runtime/main.tf:65-68`). The template name must
match the catalog's name pattern — a boundary refusal added because the deeper path-traversal check ran at
step 3 of 5, i.e. *after* an identity had been minted and a repository created
(`project_service.py:649-669`). A third key, `model_id`, is **not** ignored: it lands on the agent record
(`project_service.py:690`) and the build reads it back at deploy time (`buildspec.yml:302`). Since neither
`AgentUpdate` nor the declare wizard has a model field, this is the *only* moment an agent's model is ever
set ([§5](#5-the-declare-path)). Nothing in `agent_config` reaches the repository: the `agent.config.json`
commit was removed because the runtime never read it (`project_service.py:1701`).

### Pre-registration: the agent record comes first

Before the 202, synchronously, the service registers the agent (`project_service.py:681-710`) so a duplicate
name fails loudly as a 409. **`auth_type` and `platform` are hardcoded** to the identity provider and
AgentCore, so every materialized agent is an AgentCore agent by construction; the tenant is inherited from
the project; the owning project is stamped through a server-side keyword; and the agent's name is
`agent_config.agent_name`, **not** the repository name. Note that **`agent_arn` is `None`** — the runtime does
not exist yet — so the agent does *not* qualify at create time, which is why `create()` does not stamp
`identity_status="pending"` here and why identity minting is an explicit timeline step instead.

### The six-step timeline

`MATERIALIZE_STEPS` (`platform/control_plane/backend/src/models/repository.py:91-98`):

| # | Step | Work |
|---|---|---|
| 1 | `mint_identity` | identity provisioning only — safe with no runtime ARN (`project_service.py:1721-1723`) |
| 2 | `create_repo` | create an empty private repository; the URL is persisted immediately so a retry can resume |
| 3 | `push_template` | the only tree write — one commit carrying the whole scaffold |
| 4 | `set_repo_vars` | the CI variables that are the deploy contract |
| 5 | `provision_langfuse` | a per-agent observability project plus a key pair in Secrets Manager |
| 6 | `finalize` | flips the repository record to `ready` |

The runner wraps each step running → work → done, **skips steps already done** so a retry resumes, and on
failure marks the step failed with a *safe* hint, flips the record to `failed`, stops, and swallows the
exception — it runs after the response, so it must never raise (`project_service.py:193-212,769-826`).

Step 5 is the **only** best-effort member of the list (`project_service.py:231`): its failure marks its own
row failed and the run *continues*, because an observability outage must not strand a repository whose
code, CI variables and identity are all already correct.

Registration's story ends here. From "the source exists and a runtime is needed" onward — the CI variables
step 4 writes, the container build, the Terraform apply that creates the runtime, promotion to production
— is [the build and deployment story](agent-deployment.md). The seam is exactly step 1: identity provisioning fired from
repository creation, with the build writing `identity_status="provisioned"` back onto the record once the
runtime finally exists (`buildspec.yml:525-541`).

---

## 7. MCP servers

MCP-server registration is the same machine: `POST /api/v1/mcp-servers`
(`api/routes/mcp_servers.py:188-250`) writes a record, the lifecycle is the *same object* imported rather
than re-declared (`models/mcp_server.py:27-39`), and the routes carry the same roles. Four things differ.

**The platform never creates a gateway.** There is no `CreateGateway` call in the backend's application
code; the only occurrences are operator scripts under `platform/control_plane/backend/scripts/`.
Registration therefore **adopts** an existing gateway: an operator supplies a gateway ARN (or, for a
runtime-hosted MCP server, a runtime ARN), and the platform derives the control-plane id from it, reads the
gateway, stores its URL verbatim, and locks the inbound authorizer to JWT (`mcp_identity_service.py:551`).

**The provisioning gate is kind plus handle**, not the agents' three-way conjunction: a `gateway` record
with a gateway ARN, or a `runtime` record with a runtime ARN (`mcp_identity_service.py:96-98`). `standard`
records — external or metadata-only servers — are skipped (`models/mcp_server.py:50-55`). MCP records carry
no auth-type field, so kind plus handle *is* the whole gate.

> **Known limitation — the ARN requirement is enforced only in the browser.** The wizard blocks a gateway
> with an empty ARN, with the reason stated in the code: "so a gateway can never be registered stuck at
> `identity_status=none`"
> (`platform/control_plane/frontend/src/components/governance/McpServerRegistrationWizard.tsx:189-195`).
> The backend marks both ARNs optional (`models/mcp_server.py:83-84`), so an API caller can register a
> gateway-kind record with no ARN. It is accepted, and then simply never provisioned.

**The record is schema-validated by AWS**, unlike the agent's opaque custom descriptor: it carries a
server descriptor at a pinned schema version plus an optional tools descriptor at its own version, omitted
when there are no tools (`mcp_server_service.py:63-70`). Governance rides *inside* the server descriptor
under a namespaced metadata key (`models/mcp_server.py:232-244`), and a schema rejection is a **422**.

**Provisioning has three deliberate deviations** (`mcp_identity_service.py:136-379`): there is **no**
backend-to-resource consent step, because the agent-to-MCP consent moves to grant time; a **tool scan runs
before lockdown**; and app creation is therefore not step 1. The scan prefers a token-less control-plane
read of the gateway's targets, falls back to a wire scan only if that found nothing *and* the authorizer is
still open, and **overwrites the stored tool list only on a non-empty result** so a flaky read can never
wipe seeded tools (`mcp_identity_service.py:226-266`); zero tools on a runtime-hosted server is logged as a
decision rather than left a silent gap. `POST /mcp-servers/{id}/refresh-tools` is the synchronous, tools-only
twin of that scan and returns **409** for anything that is not a gateway
(`api/routes/mcp_servers.py:553-580`). Registration creates **no** [tool-policy object](cedar-tool-policies.md): those fields start
empty (`models/mcp_server.py:144-147`) and the first policy ever written is what attaches an engine, in
enforcing mode (`platform/control_plane/backend/src/services/mcp_cedar_service.py:195-199`).

**Deletion tears that identity back down.** `DELETE /api/v1/mcp-servers/{id}`
(`api/routes/mcp_servers.py:435-464`) deletes the Entra application — cascading its service principal and its
consents — then detaches the gateway's policy engine and deletes it (`mcp_cedar_service.py:293`), and only then
deletes the record, because every id the teardown needs lives *on* the record (`mcp_servers.py:351`). Both legs
are best-effort with a per-resource report, and neither can block the record delete.

**The gateway itself is never deleted; the platform did not create it** — which is what fixes that order.
Authentication goes before authorization, so a teardown that stops halfway leaves a live gateway that can no
longer mint a token, rather than one still accepting every tool call with Cedar stripped and its authenticator
intact. The residual is worth knowing before the record is gone: the legs are independent, so a failed identity
leg does not stop the engine leg, and that combination *does* leave the gateway serving without Cedar. The
reported line-items are the only trace, and the reclaim is manual. Tokens already minted for a deleted
application also stay valid until they expire.

**Only an engine the record itself names is ever deleted.** An empty engine id is not treated as "the engine
this gateway reports is ours": `gateway_arn` is a caller-supplied field, so that engine may belong to another
team's gateway, and deleting a policy engine is irreversible — it takes every policy in it and that gateway's
default-deny with it. The live gateway is still read, so the report can say an engine survived
(`skipped`, *engine not owned by this record*), and nothing is deleted or detached. The cost is the case the
teardown was written for: a re-registered record starts empty while its gateway is still enforcing
([the policy routes](cedar-tool-policies.md) adopt exactly that engine), so its engine keeps default-denying
until someone reclaims it by hand — the fail-closed side of the trade.

---

## 8. Grants

Grants are **live directory reads and writes with no local mirror** — the app-role assignments on the
resource's service principal are the single source of truth (`api/routes/grants.py:1-6`). No grant entity
exists in the models, no table, nothing to reconcile.

| Grant | Route | Role | Notes |
|---|---|---|---|
| List who may invoke an agent | `GET /agents/{id}/grants` (`grants.py:146`) | Viewer | an unprovisioned agent returns `[]`, not an error |
| Grant a user access to an agent | `POST /agents/{id}/grants` (`grants.py:176`) | Operator (Admin to cross tenants) | **409** unless `identity_status` is `provisioned`; the role must be `Invoker` or `Admin` |
| Revoke | `DELETE /agents/{id}/grants/{assignment_id}` (`grants.py:244`) | Operator | a directory 404 becomes a 404 — the double-click race |
| Grant an agent access to an MCP server | `POST /mcp-servers/{mcp_id}/grants` | Operator | the principal is the *agent's* service principal |

That last one is four steps in a load-bearing order (`agent_mcp_grant.py:14-23,123-273`): the app-role
assignment on the MCP server's service principal, then the delegated consent that makes [the exchange](token-propagation.md#8-the-second-exchange-agent-to-mcp-server)
possible, then get-or-create of the agent's credential provider (minting the client secret on first
creation), then appending the MCP id to the agent's `mcp_server_ids` and rebuilding the runtime environment
from that set. That field is the important distinction: **`mcp_server_ids` is desired state, never the
authorization decision** (`agent_mcp_env.py:9-16`). The directory is the non-bypassable enforcement
boundary, and the asymmetry is what makes rebuild-from-record safe — a stale extra entry, where the record
says yes and the directory says no, can only **fail closed**, so a missing or unprovisioned MCP record is
skipped with a warning rather than aborting the rebuild.

If the directory grant succeeds but the environment rebuild fails, the grant path raises with a fixed
message saying exactly that — permission yes, wiring no. On **revoke** the opposite choice is made: the
kill switch already landed, so a failed rebuild is logged loudly and not raised
(`agent_mcp_grant.py:418-437`).

---

## 9. Hosting-agnostic versus AgentCore-specific

Most of what this document describes has nothing to do with AgentCore; separating the two makes the
platform's real coupling visible.

| Hosting-agnostic — identical whatever the agent runs on | AgentCore-specific |
|---|---|
| The registry record, the envelope, the record id as the agent id, and addressing the registry by name (`agent_registry_service.py`, `models/agent.py:406-562`, `core/registry_resolver.py`) | Runtime creation — the AgentCore runtime resource (`modules/agentcore_runtime/main.tf:174-263`) and the stage-scoped, account-global names that force the 32-character stem (`main.tf:65-68`) |
| The lifecycle machine: five states, three verbs, mandatory reason, AWS-enforced edges (`models/agent.py:33-172`) | The inbound JWT authorizer — declarative at apply time (`main.tf:204-209`), imperative on update (`agent_identity_service.py:655`) — and runtime environment injection (`:683`) |
| Platform roles, tenant scoping, and the 404-not-403 contract (`api/routes/agents.py:279`) | The live runtime-status probe and its six-value union (`models/agent.py:574-617`), and `POST /agents/{id}/invoke` (`api/routes/agents.py:821`) |
| App registration, service principal, `Invoke` scope, the two app roles, assignment-required, and the backend-to-agent consent (`graph_service.py:434,692,723`) | One shared container registry with the image-tag prefix as the agent boundary; the build project, the buildspec, and the registry write-back (`buildspec.yml:258-545`) |
| User-to-agent and agent-to-MCP grants, with no local mirror (`api/routes/grants.py`, `agent_mcp_grant.py:145-179`) | The credential provider in the token vault (`services/agent_credential_service.py`) |
| Observability project plus secret-name join; marketplace publication and its `approved` gate; projects, repositories and the timeline machinery as concepts | Gateway adoption, the native tool read, and the tool-policy engine (`services/mcp_identity_service.py`, `services/mcp_cedar_service.py`) |

Four questions the AgentCore path answers implicitly, which any other hosting provider must answer explicitly.
**What replaces the provisioning gate?** It is a hardcoded three-way conjunction requiring the AgentCore
platform value (`models/agent.py:384-392`), and nothing else dispatches on platform. **What plays the role of
the runtime ARN?** That handle is what the identity, invoke, status, environment-injection and teardown paths
all key on (`models/agent.py:394`); `endpoint_url` is the envelope's provider-neutral handle, declared and
displayed, never keyed on (`models/agent.py:246`). **Where is the inbound token validated?** At the runtime's
own edge (`agent_identity_service.py:655`) — which is why the invoke route forwards the caller's object id in
the body rather than relying on the agent to decode a header. **Who writes the "deployed" fact back onto the
record?** Today the build does, with a shell-level patch of the envelope (`buildspec.yml:525-541`).

> **Known limitation — a non-AgentCore, non-Databricks agent is registered and governed, but inert.** The
> platform enum accepts seven other hosting platforms and the wizard offers them (`models/agent.py:41-49`),
> but the provisioning gate requires either AgentCore (`models/agent.py:425-434`) or Databricks
> (`:436-473`): two mutually exclusive three-way conjunctions, both checking runtime handle + identity
> provider + platform. An agent on any of the other five platforms gets no app registration, no token
> audience and no grant surface, and `POST /agents/{id}/invoke` returns **409** for it
> (`api/routes/agents.py:1527-1531`). It can be registered, submitted, approved and listed. It cannot be
> operated. A Databricks-governed agent, by contrast, is fully operational: identity provisioning
> (`agents.py:447-450`), reprovision (`:932-933`), delete teardown (`:784-830`), invoke (`:1527-1531`), and
> grants all work.

---

## 10. Known limitations

Present-tense truth, each with its citation; none is a promise. Two more are stated where they belong: a
model choice is write-once ([§5](#5-the-declare-path)) and the gateway-ARN requirement is enforced only in
the browser ([§7](#7-mcp-servers)).

> **A replaced runtime is born without its MCP wiring.** Renaming an agent forces replacement of both the
> runtime and its execution role, and a replaced runtime carries only the declarative environment — so
> every agent that had MCP grants comes back unable to reach them. Ignoring environment changes cannot
> help, because the resource is replaced rather than updated, and the only writer of that environment is
> the grant/revoke path, which [no deploy or build-completion path](agent-deployment.md) calls. Recovery is no longer
> manual: reading an agent's runtime status heals it. `GET /agents/{agent_id}/runtime` already fetches the live
> environment, and when the record names MCP servers while that environment carries no `MCP_SERVERS` key, the read
> re-applies the grants (`api/routes/agents.py:1012` → `services/agent_mcp_env.py:244`). It triggers only on a read that
> *reached* the runtime — an unreachable or throttled runtime says nothing about the wiring and is not treated as a wipe —
> and it does not wait for the runtime to return to ready, so a heal that fails is simply observed and retried by the
> next read. It fails closed — no environment, no tools, no unauthorized reach — so this
> is a governance divergence rather than a security hole, but it is undetectable from the deploy itself
> (`modules/agentcore_runtime/main.tf:43-56`): nothing signals the loss, and until somebody reads that agent's runtime
> status the record still asserts a wiring the runtime no longer implements.

> **A stage-less invoke still reaches whichever stage deployed last.** `POST /agents/{id}/invoke` takes an
> optional `?stage=`: given one it invokes *that* stage's runtime, and a stage the agent owns no runtime for
> is refused with `404 unknown stage` rather than falling through to another stage's runtime
> (`api/routes/agents.py:833-852`, resolution at `:785-791`; the runtime-status route takes the same
> parameter at `:879`). Omitted, it invokes whatever the scalar runtime ARN names, which by contract is
> *whichever stage deployed last* — the route still refuses to invent a default stage, because silently
> defaulting to one would look deliberate while still being arbitrary. The Test-invoke panel offers the
> choice only when the agent owns more than one runtime, and an agent registered before per-stage runtimes
> existed cannot be stage-targeted at all: it owns one runtime nobody can attribute to a stage.

> **The shipped scaffold's MCP wiring has never been exercised live.** The template does now consume what
> the grant path injects — it parses the granted-MCP list, performs the on-behalf-of exchange per server,
> and advertises each server's tools namespaced
> (`platform/control_plane/agent-templates/strands-agentcore/src/mcp_runtime.py` and that template's
> `src/main.py`) — but what has been *verified* is offline only: the parser's unit tests, every degrade
> path, and that the template still lints, imports and answers with no MCP environment set. The full path,
> scaffold → build → deploy → grant → invoke against live AgentCore and the identity provider, has not been
> run, and it is the first thing to put weight on the runtime execution role's AgentCore Identity token
> permissions. Treat a first live grant as bring-up, not as a regression.

> **Materialize inputs are process-local until a retry.** The 202 stashes the background run's inputs in an
> in-memory dictionary (`project_service.py:546-551`), so a task replacement between the response and the
> background run loses them and the run no-ops with a warning. The retry route exists precisely for that:
> it re-derives the inputs from durable state — fetching the agent from the registry by id, never
> re-registering it — and resets every step that is not done **plus a done `finalize`**, because `finalize`
> is the only writer of `ready` and a retry flips the record back to provisioning
> (`project_service.py:818-848`).

> **A failed teardown leg cannot keep the record, so it has to be reclaimed by hand.** `DELETE /agents/{id}`
> now cascades over what a registered agent's record is the only pointer to — its Entra app and service
> principal, its credential-provider entry in the token vault, its observability project and that project's
> key secret ([§4](#4-the-identity-minting-timeline)) — and the same is true of
> `DELETE /mcp-servers/{id}` for a gateway's policy engine and Entra app ([§7](#7-mcp-servers)). For a
> **Databricks-governed agent** one leg runs before all of these and is deliberately **blocking** (E29): its
> federation audience — the customer's live account-level trust state, which the record is the only
> remaining pointer to — is withdrawn first, and a failure there refuses the whole delete (502; 500 when the
> deployment has no Databricks wiring at all) so the record survives to drive the retry. Every other leg is
> deliberately non-blocking: the record is deleted even when a leg fails, because the alternative traps the
> row behind the orphans the operator was trying to reclaim. The consequence is that once the record is gone
> the platform has no pointer left, so a `failed` line-item is an instruction to an operator, delivered only
> through the log — both registry cascades log every outcome under one `[teardown]` prefix for exactly that
> reason, while the repo cascade uses its own `[project] teardown step …` prefix
> (`project_service.py:2523`) because it also returns its items on the wire, so an operator sweep has to
> filter on both. Nothing re-drives it, and nothing sweeps for orphans. For the MCP cascade the specific
> combination to watch is a failed identity leg with a successful engine leg: that leaves a live gateway
> without Cedar ([§7](#7-mcp-servers)).
>
> Two lifecycles stay outside both cascades. A **materialized** agent's runtime, image, execution role and
> Terraform state are reclaimed only by the per-item repository delete (`api/routes/projects.py:1122`) — also
> the only path that can reclaim the account-global execution-role name, and a leaked name blocks
> re-materializing the same agent. A **registered** agent's runtime is never deleted at all: the platform did
> not create it and does not own it (for a Databricks agent the identity residue — audience, service
> principal, secret — is reclaimed, but the App itself stays the customer's).
