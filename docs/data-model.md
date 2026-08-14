# Data model — where every fact lives

There is no single database. A governed agent's facts are split across five systems, and the split
is deliberate: the record lives in one place, the permission to use it lives in another, the secret
that proves the permission lives in a third, and the rule that evaluates it lives in a fourth. A
newcomer reading the code hits all of them within the first hour with no map, so this document is
the map. It answers one question — *where does fact X live, and who writes it?* — and stops there.

Ground truth is the code. Every load-bearing claim cites a repo-relative path, with a line where
pinpointing matters. Backend paths such as `services/…`, `models/…`, `api/routes/…` and `core/…` are
relative to `platform/control_plane/backend/src/`; Terraform paths such as `modules/…` are relative
to `platform/control_plane/infrastructure/`. The sibling
[AWS service inventory](services.md) names DynamoDB as one of the services deployed and sketches its
contents in a phrase; this document is the other half of that pair.

Two things are worth knowing before the detail, because both surprise people:

- **DynamoDB here is not one table per domain.** Five domains share the `projects` table as
  separate partitions and four share `connections`. Adding a partition costs nothing; adding a
  table costs an entry in an explicit IAM allowlist. See [§3.2](#32-one-table-many-partitions).
- **Three tables exist that nothing reads.** They are created by Terraform and passed to the
  running container as environment variables, and no backend source file references them. See
  [§2.6](#26-the-three-tables-nothing-reads).

## 1. The five owning systems

| System | Owns | Mirrored into DynamoDB? |
|---|---|---|
| **AWS Agent Registry** | Agent records and MCP-server records, including all governance metadata | n/a — it *is* the record store |
| **Identity provider directory** (Microsoft Entra ID today) | Every access fact: app registrations, service principals, grants, on-behalf-of consents, platform roles, tenant group membership | **No.** Read live on every request |
| **DynamoDB** | The platform's own domains: tenants, projects, repositories, delivery history, Git connections, marketplace, guardrails | n/a — it *is* the domain store |
| **AWS Secrets Manager** + **AgentCore Identity Token Vault** | All secret material. Records hold a name or an ARN, never a value | Names only, never values |
| **AgentCore Policy Engine** | Cedar policy text, one engine per gateway | **No.** Three non-authoritative handles on the record |

The pattern that repeats across all five: **whatever enforces a decision is the thing that stores
it.** Access is enforced by the directory, so the directory stores it. Tool authorization is
enforced by the gateway, so the gateway stores the policy. Anything the platform kept locally would
be a cache it could not invalidate, and a stale cache of a permission is a permission that outlives
its revocation.

### 1.1 AWS Agent Registry — agent and MCP-server records

Two registries in the `agent-registry` namespace: `agp-agents` for agents and `agp-mcp-servers` for
MCP servers. **They are addressed by name, not by id** — AWS mints the `registryId` and it cannot
be chosen, so the backend resolves name to id on first use and memoises the result
(`core/registry_resolver.py`). The names are the operational setting; `AGENT_REGISTRY_ID` and
`MCP_REGISTRY_ID` exist only as explicit overrides, empty by default (`core/config.py:79`, `:84`).

The two record shapes are not symmetric, and the asymmetry matters:

| | Agent record | MCP-server record |
|---|---|---|
| `recordType` | `CUSTOM` (`services/agent_registry_service.py:55`) | `MCP` (`services/mcp_server_service.py:63`) |
| Governance payload | `descriptors.custom.data` — one JSON string, the *envelope* (`models/agent.py:406-477`) | inside a stringified `server.json`, at `_meta["com.agp/governance"]` (`models/mcp_server.py:232-244`) |
| Schema-validated by AWS | No — the blob is opaque | **Yes.** `server.json` at `dataSchemaVersion="2025-12-11"`, the tools blob at `2025-11-25` (`mcp_server_service.py:65-69`) |
| Rejection surfaces as | n/a | `ValidationException` → 422 (`mcp_server_service.py:123-133`) |

Everything governance-related rides in that payload: sponsor, business unit, region, data
classification, platform, framework, model id, tenant, marketplace block, the runtime ARNs (`agent_arn`
scalar + `agent_arns` stage map for AgentCore; `runtime_handle`, `runtime_kind`, `binding_mode`,
`databricks_sp_id`, `databricks_sp_secret_arn`, `oauth2_app_client_id` for Databricks, `models/agent.py:189-239`),
the directory identity handles, and the observability project join. What does *not* ride in it is the
lifecycle state — that is derived from the registry's native record status on every read and never
stored (`models/agent.py:145-183`). The registry also has **no tenant concept**, so tenant filtering
is a post-filter in Python after the list returns (`api/routes/agents.py:483-485`).

For the write timeline — what is created when, and in what order —
see [Agent and MCP-server registration](agentcore-registration.md#1-the-record).

### 1.2 The identity provider — every access fact

Microsoft Entra ID is the currently supported identity provider. It owns, exclusively:

- the per-agent and per-MCP app registrations and service principals;
- **every grant**, as an `appRoleAssignedTo` entry on the *resource* service principal — this is
  the same primitive for "user may invoke agent" and "agent may call MCP server";
- the on-behalf-of consents (`oauth2PermissionGrants`) that let one identity exchange a token for
  another;
- platform role assignments, which arrive as a `roles` claim;
- the groups whose membership defines tenant membership.

**None of it is mirrored.** The routes read the directory live on every call — the grants module
says so in its own first lines: "live Microsoft Graph reads/writes — **NO DynamoDB**"
(`api/routes/grants.py:1-6`). The governance graph is likewise reassembled from directory reads per
request (`services/governance_graph_service.py`). There is no grant model in `models/` at all. The
rationale is in [§4.1](#41-no-grant-mirror).

Configuring the directory side — app registrations, the exposed scope, role assignment — belongs to
[Microsoft Entra ID setup](entra-setup.md#assign-users-to-platform-roles) and is not
repeated here.

### 1.3 DynamoDB — the platform's own domains

Eight tables, all `PAY_PER_REQUEST` with point-in-time recovery and server-side encryption,
declared in `platform/control_plane/infrastructure/modules/dynamodb/main.tf` and wired at the
Terraform root's `main.tf:106-113`. Names are derived, never written down:
`name_prefix = "${project_name}-cp-${environment}-<last 6 digits of the account id>"`
(`main.tf:76`), so the tenants table is
`<project>-cp-<env>-<account6>-tenants`. Each name reaches the backend as an environment variable
on the ECS task definition. Table by table: [§2](#2-every-dynamodb-table).

### 1.4 Secrets Manager and the Token Vault — secret material

Two secret stores, and which one a secret lands in is decided by **who redeems it**.

| Secret | Where it lives | Written by | Read by |
|---|---|---|---|
| Backend's own directory client secret | Environment variable, or Secrets Manager resolved from an ARN | Operator / Terraform | `core/secrets_loader.py`, then every directory call |
| Per-connection Git credential (token, or an app private key) | Secrets Manager under `CONNECTIONS_SECRET_PREFIX` | The connection service | The Git write path |
| Per-user Git link token pair | Secrets Manager under `GITHUB_USER_LINK_SECRET_PREFIX` | The user-link service | The same, acting as the user |
| Per-build scratch clone token | Secrets Manager under `RUNTIME_BUILD_TOKEN_PREFIX` | The runtime-build service | The build job, once |
| Databricks tenant credentials | Secrets Manager under `DATABRICKS_TENANT_SECRET_PREFIX` — per-stage workspace SP secret at `<prefix><tenant_id>/<stage>`, optional account-admin pair at `<prefix><tenant_id>/account-admin`, per-agent SP secrets (sp_secret mode) at `<prefix><tenant_id>/agents/<agent_id>` | Tenant service, databricks identity service | Runtime catalog, databricks invoke path, databricks identity service |
| Per-agent observability key pair | Secrets Manager, `langfuse-agent-<agentId>-keys` | `services/langfuse_provisioning.py` | The agent runtime at start-up |
| **Per-agent directory client secret** | **AgentCore Identity Token Vault only** | `services/agent_credential_service.py:141-162` | AgentCore Identity, during the token exchange |

The first four prefixes are settings with those exact names (`core/config.py:31-34`); the Databricks prefix
is `DATABRICKS_TENANT_SECRET_PREFIX` (`:35`, injected as `agp-${var.environment}/databricks-tenants/` per
`modules/ecs/main.tf:758-759`). The last row is the
interesting one. The per-agent client secret is minted at grant time and handed straight to the
Token Vault as a credential provider's `clientSecret`. It is never written to Secrets Manager,
never persisted on the record, never logged, and never returned; the only thing that lands on the
record is the provider **name**, `agp-agent-obo-<agentId>` (`config.py:171`,
`agent_credential_service.py:1-18`). The agent therefore holds no printable secret at all — it
presents a workload token and the vault does the rest. MCP-server apps get no secret whatsoever:
an MCP app is a resource, an audience, never a client.

**No token of any kind is stored anywhere.** Browser tokens, exchanged agent tokens, workload
tokens and MCP tokens are all per-request and in-memory. The mechanics are in
[Token propagation](token-propagation.md#8-the-second-exchange-agent-to-mcp-server).

### 1.5 The AgentCore Policy Engine — Cedar text

Per-tool authorization rules are [Cedar statements](cedar-tool-policies.md), and they live in an AWS-side **Policy Engine**,
one per gateway. There is no local policy store — no table, no bundled Cedar library, no second
copy (`services/cedar_policy_text.py:1-13`, `services/mcp_cedar_service.py:19-20`). The engine is
attached to the gateway by a `policyEngineConfiguration` carrying an ARN and a mode, either
`ENFORCE` or `LOG_ONLY`; detaching is the same call with the field omitted.

Because Cedar text is the only copy, the platform has to reconstruct its own friendly editing form
out of it. It does that with a header comment: every generated statement is prefixed
`// agp:v2 effect=… oid=… tool=… label=… cond=…`, and the list path parses that header back out of
the text AWS returns to rebuild the row the UI shows (`cedar_policy_text.py:1-13`). One vocabulary
note that trips people: Cedar says `permit`/`forbid`, the API and UI say `allow`/`deny`, and the
generator maps between them. The record mirrors exactly three handles — `cedar_policy_engine_id`,
`cedar_policy_engine_arn`, `cedar_enforcement_mode` (`models/mcp_server.py:145-147`) — and none of
them is the policy. What the gateway evaluates per tool call is summarised in
[Token propagation](token-propagation.md#9-the-gateway-and-cedar-per-tool-call).

## 2. Every DynamoDB table

Paths in this section are relative to
`platform/control_plane/infrastructure/modules/dynamodb/main.tf` for the table definitions and to
`platform/control_plane/backend/src/` for the code. Six of the eight tables use a generic `pk`/`sk`
pair; the two with a domain-shaped key schema are both tables nobody reads, which is itself a tell.

### 2.1 `projects` — five partitions in one table

Definition `:292-336`. Keys `pk` / `sk`; one secondary index, `agent_id-index`, hashed on `agent_id`
with **`KEYS_ONLY`** projection — enough to rebuild an update key and nothing more.

| `pk` | Holds | `sk` | Declared at |
|---|---|---|---|
| `project` | Project containers, each scoped to one Git organisation | project id | `services/project_service.py:129` (`_PROJECT_PK`) |
| `repository` | Materialized repositories, each with its pre-registered agent id and delivery status | repository id | same module, `:130` (`_REPOSITORY_PK`) |
| `deployment` | Append-only delivery history | `{repo_id}#{stage}#{started_at}#{id[-4:]}` | same module, `:134` (`_DEPLOYMENT_PK`) |
| `project_role` | Per-project role assignments | composite | `services/project_role_service.py:59` |
| `template` | The platform's own template catalogue | composite | `services/template_registry.py:51` |

The `deployment` sort key is the design worth understanding. It is not a record id: the
`started_at` prefix makes the partition time-sortable per repository and stage with no counter, so
an append needs no read-modify-write and therefore has no race, and the four-character id suffix
removes same-millisecond collisions (`models/deployment.py:52-59`). Reads use
`begins_with(sk, "{repo_id}#{stage}#")` with the scan direction reversed for newest-first.

**Writers.** The backend is the sole writer of `project`, `project_role` and `template`; `repository`
and `deployment` each have **two**. The backend appends a `deployment` row when a build is requested
(`api/routes/builds.py:322`→`:429`, persisted by `services/project_service.py:3295`) and a terminal
`failed` row when one never starts (`api/routes/builds.py:317`, and `services/project_service.py:1340`
for the promote/rollback path); the build job is the only writer of a **successful**
outcome — only the build knows it. The buildspec assembles that item and calls `PutItem` directly, and
uses `agent_id-index` to find the repository row it updates (`modules/codebuild/buildspec.yml:214`,
`:325-326`, `:377`). The key shape is thus duplicated in two languages, a hazard the buildspec's own
comment flags (`:349-354`): drift there means history silently reads empty.

### 2.2 `connections` — four partitions

Definition `:258-285`. Keys `pk` / `sk`, no secondary index.

| `pk` | Holds | Declared at |
|---|---|---|
| `connection` | One Git-organisation connection per item, including the *name* of its credential secret | `services/connection_service.py:74` |
| `conn_state` | Short-lived cross-site-request-forgery state for the connect handshake | `connection_service.py:75` |
| `github_user_link` | Per-user Git account links, `sk = "<principal oid>#<connection id>"` | `services/github_user_link.py:131` |
| `github_link_state` | The link handshake's state and proof-key material | `github_user_link.py:132` |

Neither state partition has a DynamoDB TTL; both are expired by the application. Read and written
by the connection, user-link, project and build routes.

### 2.3 `tenants`

Definition `:341-368`. Keys `pk` / `sk`, no secondary index. **Every tenant is one item in a single
literal partition, `pk="tenant"`** (`services/tenant_service.py:48`), with `sk` the tenant id, and
the list is a paged `Query` on that one partition.

A tenant row carries the tenant's name, line of business, description, the identity-provider group
ids whose membership defines tenant membership (at least one required), and a `stages` map — one
entry per deployment stage, each naming a target AWS account and the roles used to reach it. That
map is the cross-account seam: an empty deploy role means deploy-in-place.

Read by every tenant-scoped route and by the build path; written only by the tenant admin routes
and the seed script.

> **Known limitation — a stage cannot be deleted through the update route.** The tenant update
> route merges the `stages` map at *stage* granularity (`services/tenant_service.py:158`): a stage
> the body never names survives, and a stage named in both is replaced whole, fields included. A
> body naming only `dev` on a `dev`+`prod` tenant therefore keeps `prod` untouched instead of
> dropping it (`backend/tests/test_tenant_service.py:260-298`). The cost of that guarantee is the
> other direction — removing a stage takes a direct write to the table, because no request shape
> expresses it.

### 2.4 `marketplace`

Definition `:225-252`. Keys `pk` / `sk`, no secondary index. One literal partition, `MARKETPLACE`
(`services/marketplace_service.py:70`), with three record kinds discriminated purely by the sort
key: a bare subscription id, `listing#<product type>#<product id>`, and
`publish#<product type>#<product id>` (`marketplace_service.py:137-154`). Keying a publish request
on the product means one per product — a re-publish overwrites, and decision history lives in the
request's own audit fields, not extra rows. Read and written only by the marketplace routes
(`api/routes/marketplace.py`); approving a publish request also writes the approved datasheet onto
the registry record itself (`marketplace_service.py:976`).

### 2.5 `guardrails`

Definition `:177-219`. Keys `pk` / `sk`, plus a `status-index` hashed on `status` with an `ALL`
projection. This table breaks the single-partition idiom: **the partition key is per item**,
`GUARDRAIL#<template_id>`, with `sk = "META"` (`services/guardrail_service.py:203`, `:231`), so a
list is a `Scan` filtered on `begins_with(pk, "GUARDRAIL#")` (`:216`) rather than a `Query`. Read
and written by the guardrail routes, which also call the Bedrock guardrail APIs.

> **Known limitation — the `status-index` has no reader.** The guardrail service lists by scanning
> the table; nothing in the backend queries the index it provisions.

### 2.6 The three tables nothing reads

`app-factory` (`:5-32`), `application-catalog` (`:38-91`) and `deployment-metadata` (`:97-171`) are
created by Terraform, granted to the task role, and passed into the running container as the
environment variables `APP_FACTORY_TABLE_NAME`, `APPLICATION_CATALOG_TABLE` and
`DEPLOYMENT_METADATA_TABLE` (`modules/ecs/main.tf:656`, `:660`, `:713`). **No backend source file
reads any of them.** There is no matching setting in `core/config.py`, and the settings class
ignores unknown environment variables, so the three values are dropped on start-up without a
warning.

Two of them are also the only tables in the stack with a domain-shaped key schema:
`application-catalog` is keyed `application_id`/`version` with a `TemplateIndex`, and
`deployment-metadata` is keyed `deployment_id`/`timestamp` with an `ApplicationIndex` and a
`StatusIndex` and is the only table anywhere in the stack with a TTL attribute. (`app-factory` is a
plain `pk`/`sk` table with no index.) Say it plainly, because a reader who finds them by any other route
will assume they are load-bearing: **they are not.** Delivery history lives in the `projects`
table's `deployment` partition ([§2.1](#21-projects--five-partitions-in-one-table)), which is a
different table with a different shape and no relation to `deployment-metadata`.

### 2.7 The ninth table, and the developer database

A ninth table exists in the account — the Terraform state lock, `<prefix>-tf-lock`, hashed on
`LockID` (`modules/state_backend/main.tf:60-78`). It belongs to the bootstrap layer, not the
application data layer; nothing in the platform reads or writes it, and it is the only DynamoDB
table in the stack without point-in-time recovery.

A SQLite file, `control_plane.db`, is the default `DATABASE_URL` (`core/config.py:20-23`). It is
reached by exactly two modules — the engine construction and the health probe — and holds no
platform domain. It is a local development convenience, not a deployment mode.

## 3. Conventions that repeat

### 3.1 An empty table name means an in-memory store

Every table setting defaults to the empty string, and each one documents what that means in its own
description: *empty ⇒ in-memory fallback* (`core/config.py:29-36`). It is not an error state and it
is not a missing-configuration bug. A service constructed with an empty table name builds **no boto3
client at all** and keeps its items in a process-local dictionary behind a lock.

The important property is that the fallback is not a separate code path: items round-trip through
the same serialisers the DynamoDB path uses, so the enum-to-string, float-to-`Decimal` and
tenant-field handling those helpers do is exercised by the test suite rather than only in the cloud
(`services/guardrail_service.py:1-13`) — which is what lets the whole suite run with no AWS
credentials. It is a test and development affordance, not a deployment mode: nothing persists across
a restart and nothing is shared between tasks.

### 3.2 One table, many partitions

Adding a domain to this codebase means adding a **partition**, not a table, and the reason is an
IAM allowlist. The task role's DynamoDB policy enumerates all eight table ARNs and their
`/index/*` children explicitly (`modules/ecs/main.tf:109-147`), so a new table needs a Terraform
change, a new environment variable, and a new setting before a single item can be written. A new
partition in an existing table needs none of those.

The second reason is read shape. Partitions in one table are separated by a literal `pk` value and
listed with a `Query` on that value — or, where the sort keys are composite, with a `begins_with`
range on the prefix — which gives exact per-domain listing without provisioning a secondary index:
a partition never appears in another partition's list by accident. The cost is that a table's items
are heterogeneous, so nothing can be inferred from a raw table dump, and that the partition literal
is the only thing keeping two domains apart.

### 3.3 Tenant scoping lives on the record

[Tenant ownership](authorization-layers.md#gate-2--tenant-visibility) is a field on the record, not a partition and not a table:
`tenant_id` on agents (`models/agent.py:267`), MCP servers (`models/mcp_server.py:88`) and projects
(`models/project.py:91`). It is required on creation and optional on read, because making it
mandatory on read would break records written before it existed.

**Repositories carry no tenant field.** They inherit through their parent project, so the flat
repository list filters each repository by its project's tenant
(`api/routes/projects.py:24-25`). Because the registry itself has no tenant concept
([§1.1](#11-aws-agent-registry--agent-and-mcp-server-records)), agent and MCP-server tenant
filtering happens in Python after a full list — not in the query.

### 3.4 Envelope mirrors are debug copies; the record id is authoritative

The registry mints the record id, and **that id is the agent id**. The envelope also carries an
`agent_id` key, and it is a debug mirror only: the create path builds the object with a placeholder
id purely so the envelope can be serialised, then overwrites it from the returned record ARN
(`services/agent_registry_service.py:395-412`). The same rule applies to the other mirrored fields
listed in [§1.5](#15-the-agentcore-policy-engine--cedar-text) — the Cedar handles on an MCP record
tell you *which* engine to ask, never what it would answer.

Where a mirror and its source disagree, the source wins and the mirror is the stale one. The
lifecycle state avoids the problem entirely by not being stored: it is mapped from the registry's
native record status on every read, and an unrecognised native status raises a named error rather
than silently defaulting (`models/agent.py:134-172`).

### 3.5 `identity_status` is the operational gate

Four different state fields describe a governed agent, and only one of them gates behaviour.
`identity_status` is a **pinned enum** — `IdentityStatus`, with the four values `none`, `pending`,
`provisioned` and `failed` (`models/agent.py:72-102`), declared once and shared by the MCP-server model
(`models/mcp_server.py:136-139`). It subclasses `str`, so it compares equal to its own value and every gate
below reads it as a plain string; the stored form is always the value, never the enum's name. Reads stay
deliberately **tolerant**: an unrecognised stored value coerces to `none` with a warning instead of
raising (`models/agent.py:105-135`), because the build writes this one field into the stored envelope
directly with `jq` and because `none` fails every gate *closed*. That tolerance is the one place the
model departs from the [§3.4](#34-envelope-mirrors-are-debug-copies-the-record-id-is-authoritative) convention of failing loudly.

It is the field that decides whether an agent can be used at all:
invoking an agent returns 409 unless it reads `provisioned` *and* the agent is governed on either
platform — AgentCore (`is_agentcore_agent`) or Databricks (`is_databricks_governed_agent`)
(`api/routes/agents.py:1527-1531`) — and granting access returns 409 unless it reads `provisioned` *and*
a service-principal id is present (`api/routes/grants.py:151-153`, enforced at `:763`, `:792`, `:828`,
`:907`). The lifecycle state — approved, deprecated and so on — gates nothing at call time. Runtime
status is a live probe and is never persisted. Repository status describes a build, not an identity.

The practical consequence: an agent that looks fully approved in the governance UI and still cannot
be invoked is almost always an `identity_status` that never left `pending`. The minting timeline is
in [Agent and MCP-server registration](agentcore-registration.md#4-the-identity-minting-timeline).

## 4. What is deliberately not stored

### 4.1 No grant mirror

There is no local copy of who may invoke what. No grant model exists in `models/`, no
DynamoDB partition holds an assignment id, and every read goes to the directory
(`api/routes/grants.py:1-6`, `services/governance_graph_service.py`). Listing an agent's grants
means a live call per agent; rendering the governance graph means a fan-out of them.

That is a real cost and it buys a specific property. A grant is a permission, and the directory is
the thing that enforces it: a token exchange either finds a consent there or it fails. A local
mirror would be a second answer to the same question with no way to invalidate it when an
administrator revokes an assignment in the directory's own console — so a revoked grant would keep
working for as long as the cache lived. Per-request latency in exchange for exactly one answer is
the trade, and it is made on purpose.

### 4.2 No audit log

Nothing records who changed what, when. There is no audit table, no audit partition and no audit
setting. Some records carry provenance fields — `created_by`, `created_at`, `updated_at`, a
marketplace publish request's decision fields — and a lifecycle transition's mandatory reason is
stored by the registry rather than by the platform. But those are per-record current state, not a
history: an overwritten field leaves no trace, and a deleted record leaves none at all.

> **Known limitation.** Reconstructing "who deprecated this agent last Tuesday" is not possible from
> platform data. The available evidence is external: the registry's own record history,
> CloudTrail, and the application logs.

### 4.3 Desired state versus the authorization decision

`mcp_server_ids` on an agent record is the one field most likely to be misread. It is **desired
state** — intent plus connection details — and never the authorization decision. The decision is
the directory's on-behalf-of consent, which is the non-bypassable boundary
(`services/agent_mcp_env.py:1-16`).

The asymmetry is what makes rebuilding an agent's runtime wiring from the record safe. If the record
lists an MCP server the directory has not consented to, the token exchange fails and the call does
not happen — the drift can only **fail closed**. So the rebuild helper skips a missing or
unprovisioned MCP record with a warning and never aborts. The reverse drift cannot open access
either: a consent with no record entry simply means nothing wires the endpoint into the runtime.
Granting therefore writes the directory first and the record second, and a failure between the two
raises an explicit split-state error rather than reporting success.

### 4.4 Pull requests are not cached

The Git provider is authoritative; the platform reads them live and projects them into a view model
carrying no provider body and no token (`models/repository.py:309-320`). Their author is a Git
login, never a platform principal: the two are proven by different issuers and the platform holds no
mapping between them.

## Related

- [AWS service inventory](services.md#platform-account--core) — the AWS services these tables sit
  among, and which account each service lands in.
- [Token propagation](token-propagation.md) — what the directory's grants and consents
  actually do at call time, hop by hop.
- [Agent and MCP-server registration](agentcore-registration.md) — when each record
  and identity artifact is written, and by what.
- [Microsoft Entra ID setup](entra-setup.md) — configuring the identity provider whose
  directory owns every access fact above.
