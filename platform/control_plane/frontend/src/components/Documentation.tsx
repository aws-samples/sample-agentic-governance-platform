import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
// The honesty vocabulary is INTERPOLATED, not retyped: the `roadmap` section below quotes the
// exact strings the ten bannered pages render, and `comingSoonCopy.test.ts` freezes them. Importing
// them is what makes it impossible for this page to describe the banner in words the banner does
// not use — the tenth phrasing the copy contract exists to prevent.
import {
  COMING_SOON_BODY,
  COMING_SOON_TITLE,
  SAMPLE_BADGE_LABEL,
  SOON_TAG_LABEL,
} from './shared/comingSoonCopy';

interface DocSection {
  id: string;
  title: string;
  children?: DocSection[];
  content?: string;
}

/**
 * In-app documentation for AGP itself.
 *
 * EVERY CLAIM ON THIS PAGE IS VERIFIABLE FROM THE CODE IN THIS REPOSITORY. It ships publicly, so
 * it is bound by the same honesty rule as the README: document what exists, and mark what does
 * not as roadmap (the `roadmap` section below, in the pinned `shared/comingSoonCopy.ts`
 * vocabulary). If you change a behaviour, change the section that describes it in the same commit
 * — a docs page that drifts is worse than no docs page.
 *
 * Deliberately NOT here: a hand-maintained endpoint list. The backend serves live OpenAPI at
 * `/docs`, `/openapi.json` and `/redoc`, which cannot drift; the `api` section links to it.
 *
 * The nav renders group → child, and a child carrying its own `children` renders as an expander
 * (see the sidebar below). The AGP tree is two levels deep today, so that branch is unexercised —
 * it is kept because it is the layout's contract, and adding a sub-page to any section below is
 * meant to be a data-only change.
 */
const docs: DocSection[] = [
  {
    id: 'start-here',
    title: 'Start here',
    children: [
      {
        id: 'overview',
        title: 'Getting started',
        content: `# Agentic Governance Platform

AGP is a self-hostable, AWS-native **control plane for governing AI agents through your own identity provider** — Microsoft Entra ID is the provider supported today. It is the registry, access-grant, policy, marketplace and observability surface an operator uses to answer one question about every agent in an organisation: who is allowed to call it, and what is it allowed to do?

Identity is never invented here. Entra ID is the system of record for principals and access; AGP records governance metadata and drives Microsoft Graph, but it does not become a second directory.

## The three principal types

Everything AGP governs is an Entra principal with its own app registration and its own \`Invoker\` / \`Admin\` app roles:

| Principal | What it is | Registered through |
|---|---|---|
| **Users** | Humans signing in to the platform | Your Entra tenant |
| **Agents** | An agent runtime, with an identity of its own | The agent registration wizard |
| **MCP Servers** | A tool surface an agent may reach | The MCP server registration wizard |

Because an agent has its own app registration, it is a first-class caller — an agent calling an MCP server is authorized by exactly the same mechanism as a human calling an agent.

## The two authorization questions

AGP keeps these deliberately separate, and they are enforced in different places:

**1. May this caller reach this target at all?**

That is an **Entra app-role assignment**. A grant is the assignment itself — there is no local mirror that could disagree with the directory. Invocation goes through the backend, which exchanges the caller's token for a target-audience token On-Behalf-Of; Entra's token endpoint is the real enforcement point. An unassigned caller is refused there, not by our code.

**2. May this agent call this specific tool, with these arguments, under these conditions?**

That is a **Cedar policy**, evaluated per call by the gateway's own Policy Engine against the inbound token. It is finer-grained than an app role by design: an agent may hold a valid grant to an MCP server and still be denied one tool on it, or one tool with one argument value.

> Read the two in order. Question 1 is coarse and directory-backed; question 2 is fine and policy-backed. A call must pass both.

## Where to go next

- **Deploying AGP** — stand the platform up in your own AWS account and Entra tenant.
- **The agent registry** — register an agent and move it through approval.
- **Access and grants** — hand out and revoke access.
- **Cedar tool policies** — restrict what a granted agent may actually do.
- **Not built yet** — what is roadmap, stated plainly.`,
      },
      {
        id: 'deploying',
        title: 'Deploying AGP',
        content: `# Deploying AGP

This is a **summary with links**. The repository README is the source of truth for the deploy path and \`docs/entra-setup.md\` is the source of truth for the directory work — this page deliberately does not restate either in full, because two copies of a procedure drift.

## Prerequisites

| Requirement | Note |
|---|---|
| **AWS account + credentials** | \`aws sts get-caller-identity\` must succeed; region exported |
| **AWS CLI v2, recent** | The seed step calls \`aws agent-registry-control\`; an older v2 does not have the command |
| **Terraform >= 1.15** | The floor declared in \`infrastructure/main.tf\` |
| **docker or finch, running** | Not optional — container images are mirrored into your ECR **during** \`terraform apply\` |
| **Node.js + npm** | The frontend is built locally and synced to S3 |
| **\`jq\` and \`python3\`** | Used by the deploy and seed scripts |
| **The backend virtualenv** | Created **before** the first apply; a plan-time precondition fails by name without it |

## Do Entra first

The whole authorization model is Entra app-role assignments, so the directory work comes before any Terraform. Follow **\`docs/entra-setup.md\`** — it walks the backend app registration, its Application ID URI (the audience), the client secret, the Graph application permissions, the SPA app registration, and the three platform app roles.

Create the three app roles on the **backend** app registration, not on the SPA. Role claims arrive on the token whose audience is the backend, so the assignments must live there.

The audience has to match in three places, byte for byte: the \`entra_audience\` Terraform variable, the Application ID URI in Entra, and the URI half of the SPA's scope. The examples use **\`api://agp\`**, so the SPA scope reads \`api://agp/Access.Default\`. When they disagree every API call returns 401 and nothing in the response says which side is wrong.

## Configure, then apply once

- **1.** \`terraform.tfvars\` — the defaults work as-is for a single-account deploy.
- **2.** \`secrets.auto.tfvars\` — carries your Entra values. Terraform auto-loads any \`*.auto.tfvars\`, so there is no \`-var-file\` flag. Both files are gitignored.
- **3.** \`frontend/.env\` — the SPA reads seven \`VITE_*\` variables. Fill in the five you already know; the other two come back from the first deploy.
- **4.** \`infrastructure/scripts/deploy-full.sh\` — Terraform, backend image build and push, ECS rolling deploy, frontend build, frontend deploy. It pauses after \`terraform plan\` and waits for you to type \`yes\`.

**One apply is enough.** The agent and MCP registries are created by Terraform and resolved **by name** at runtime, so there is no registry id to read back or paste anywhere. If resolution ever fails, the backend answers **503 carrying the resolver's own message**, which names the registry, the region and the command that fixes it.

## Close the redirect-URI loop

A genuine chicken-and-egg, and the one place sign-in breaks silently: the SPA's redirect URI is a CloudFront domain that does not exist until Terraform has run. After the first apply, read \`frontend_url\` and \`api_endpoint\` from \`terraform output\`, add \`<frontend_url>/auth/callback\` as a redirect URI on the **SPA** app registration, put that same value plus the API URL into \`frontend/.env\`, and redeploy the frontend. Until both sides point at the same URI, sign-in fails with no useful error.

## Seed data (optional)

Nothing below is required for the platform to work — it puts something credible in the UI. Run these from the backend directory with \`PYTHONPATH=src\`, in this order:

- **1.** \`seed_default_tenant.py\` — the default tenant row and its deploy configuration. Idempotent.
- **2.** \`seed_agents.py\` and \`seed_mcp_servers.py\` — representative agents and MCP servers. Both accept \`--dry-run\`.
- **3.** \`bootstrap_demo_use_cases.py\` — stands up demo gateways with their Lambdas and IAM roles. Idempotent and namespaced.

> **Teardown needs supervision.** \`terraform destroy\` tears the stack down, but it can leave resources behind on the first pass. Check before assuming the account is clean.`,
      },
    ],
  },
  {
    id: 'govern',
    title: 'Govern',
    children: [
      {
        id: 'registry',
        title: 'The agent registry',
        content: `# The agent registry

An agent's record lives in **AWS Agent Registry**. AGP does not keep a parallel table of agents: it writes the registry's own records and carries its governance fields — sponsor, business unit, region, data classification, platform, framework, tenant — inside the **record envelope**. That is why the registry, not this platform, is the thing you back up.

## Registering an agent

The registration wizard collects the governance metadata in five steps:

- **1.** **Name and purpose** — the name must be unique; a collision comes back inline on this step.
- **2.** **Sponsor** — the accountable human.
- **3.** **Business unit, region and data classification.**
- **4.** **Platform, framework and origin.**
- **5.** **Confirm** — a read-back of everything before the record is created.

A **tenant** is required at create time and is what gates the classification step; see **Multi-tenancy and roles**.

## Lifecycle is the registry's own approval workflow

AGP does not invent a state machine. It drives the registry's native statuses:

| Status | Meaning |
|---|---|
| \`DRAFT\` | Created, not yet submitted. Surfaced in the UI as **proposed** |
| \`PENDING_APPROVAL\` | Submitted, waiting on a decision |
| \`APPROVED\` | Live and usable |
| \`REJECTED\` | Declined |
| \`DEPRECATED\` | Retired |

The edges are enforced by the registry, not by us: **\`DRAFT\` cannot go straight to \`APPROVED\`** — a record has to be submitted first. Attempting the illegal jump is rejected as a validation error rather than silently allowed.

Deprecating an agent also **unlists it from the marketplace**, and that unlisting is not undone by a later re-approval — a deprecation sticks. The lifecycle write happens first and the unlisting second, and a failed unlisting is surfaced rather than swallowed, so an agent can never be left advertised after being retired.

## Identity provisioning is gated

An agent only gets its own Entra app registration, service principal and \`Invoker\` / \`Admin\` roles when it is a real invocable runtime: it must carry a runtime ARN, use Entra auth, and be on the Bedrock AgentCore platform. Records that do not meet that gate are still governed — they are registry records with governance metadata — but they have no identity to grant on, and the marketplace refuses to publish them for exactly that reason.

Registering an agent also **attempts** to provision its own Langfuse project and key, so traces land in the agent's own project. That provisioning is deliberately best-effort: it is a no-op when Langfuse is not configured, and a failure is logged rather than failing the registration — the agent is simply left without a project. Only the project id and the **name** of the Secrets Manager secret are stored on the record; the key values never touch the record, a response or a log.`,
      },
      {
        id: 'access-grants',
        title: 'Access and grants',
        content: `# Access and grants

**A grant IS an Entra app-role assignment.** There is no local mirror of who can call what, which is the whole point: nothing in this platform can disagree with the directory, and revoking in Entra revokes here with no reconciliation step.

## One mechanism, two directions

| Grant | Principal | Resource |
|---|---|---|
| **User → Agent** | The user's object id | The agent's service principal |
| **Agent → MCP Server** | The agent's service principal | The MCP server's service principal |

Both are \`appRoleAssignedTo\` writes against the **resource's** service principal, using the role id stored on that resource's record. The Access tab on an agent grants the first; the Grants surface on an MCP server grants the second.

A user→agent grant is exactly one Graph write. The assignment id that comes back is persisted, because it is the only handle by which the grant can later be revoked — a grant recorded without its assignment id would be one that revoke could never tear down, so the platform treats a missing id as a failure rather than storing a dead reference.

Grants are **idempotent**. If Entra reports the assignment already exists, the existing one is looked up and returned — a user who already holds \`Invoker\` from a direct grant can be granted again (or a retry can run after a partial failure) without an error.

## Invoking goes through the backend

The SPA never calls an agent runtime directly. \`POST /agents/{id}/invoke\`:

- **1.** Checks the agent's **identity** state — pending, failed and unprovisioned are all refused before any token is minted, as is an agent with no runtime to invoke.
- **2.** Exchanges the caller's token for an **agent-audience token On-Behalf-Of**.
- **3.** Posts to the runtime the agent's ARN names, forwarding the caller's object id in the payload so the agent itself knows who is asking.

Step 2 is where authorization actually happens. If the caller holds no app-role assignment on the agent, Entra refuses the exchange with **\`AADSTS50105\`**, and the platform surfaces that as a **403**. Nothing in AGP had to check a list — the directory answered.

## Revoking

Deleting the app-role assignment is the revocation. There is no cache to invalidate and no "pending revocation" state, because the assignment was the only record of access.`,
      },
      {
        id: 'mcp-servers',
        title: 'MCP servers and tools',
        content: `# MCP servers and tools

An MCP server is the tool surface an agent reaches. AGP registers them the same way it registers agents — a record with governance metadata, its own Entra app registration, and \`Invoker\` / \`Admin\` roles — and adds one discriminator that changes what the platform can do with it.

## Two kinds

| Kind | What it means |
|---|---|
| \`standard\` | A registered MCP endpoint. Governed and grantable |
| \`gateway\` | An AgentCore Gateway. Governed, grantable, **and policy-capable** |

\`kind\` defaults to \`standard\`. It is the flag that gates the **Policies** tab on the server's detail page: only a gateway has a native Policy Engine to attach Cedar policies to, so the tab does not appear for a standard server rather than appearing and failing.

## Granting an agent access to an MCP server

The grant does four things, in this order, and the order is load-bearing:

- **1.** **The app-role assignment** on the MCP server's service principal, with the agent's service principal as the principal and the role id stored on the MCP record.
- **2.** **The agent→MCP delegated consent** — the precondition for the On-Behalf-Of invoke. Without it the assignment exists but the token exchange cannot happen.
- **3.** **The per-agent AgentCore Identity credential provider**, get-or-create. Only the provider **name** — never a secret — is persisted on the agent record.
- **4.** **A rebuild of the agent's runtime environment** from its full set of granted servers.

## Multi-MCP: one agent, many servers

Step 4 is what makes that true. The agent's granted MCP servers are the authoritative desired state, and the runtime environment is rebuilt **from the whole set** on every grant — it writes a list of servers, each with its own audience and endpoint, plus the credential provider name. Each server's tools stay namespaced to it, so granting a second server no longer overwrites the first and two servers exposing a same-named tool do not collide. Because every entry carries its own audience, the runtime acquires a token per server at call time — that minting is **AgentCore Identity's** behaviour inside the agent runtime, documented by AWS rather than by this repository; what the control plane guarantees is the per-server audience the runtime asks for.

## Revoking is a real kill switch

Revoking deletes the app-role assignment **and** the delegated consent. The agent does not lose a cached permission that expires later — the token exchange for that server stops working. That is the difference between a governance UI and a governance control plane.

> A grant failure is not silent. The platform distinguishes a Graph "already assigned" (recovered and treated as success) from a real failure (surfaced, with the operation retryable).`,
      },
      {
        id: 'cedar-policies',
        title: 'Cedar tool policies',
        content: `# Cedar tool policies

An app-role assignment answers **may this caller reach this server**. Cedar answers **may this caller call this tool, with these arguments**. Policies live on a **gateway** MCP server's native **AgentCore Policy Engine** and are evaluated by the gateway on every call, against the inbound On-Behalf-Of token — not by this control plane, and not against a copy of anything.

## What the platform does when you author a policy

- On the **first** policy it creates the Policy Engine (idempotent — skipped if the record already has one), associates the engine to the gateway, and creates the policy.
- The policy text is generated from the form: principal, tool (or all tools), effect, and any parameter conditions.
- Removing a policy deletes the policy; the engine survives.
- Disabling authorization detaches the engine from the gateway.

There is **no local policy mirror**. The friendly row you see in the UI is re-derived by parsing the Cedar text the gateway returns, so what is displayed is what is deployed.

## Effects, and why Deny wins

The form speaks \`allow\` / \`deny\`; Cedar speaks \`permit\` / \`forbid\`. In Cedar a \`forbid\` is not a lower-priority rule — **it wins over every \`permit\`**, so an explicit Deny is a hard stop that no other policy can re-open. Use it for the tool nobody may call, and rely on default-deny for everything you simply have not permitted.

One deny is rejected by construction: an **all-principals deny with no conditions**, because it would block the entire tool or gateway for everyone. If that is what you want, detach the engine or delete the grant.

## Parameter conditions

A policy can constrain the arguments as well as the tool. Numeric parameters support \`= != < <= > >=\`; string parameters support \`=\` and \`!=\`. So "may call \`transfer\`, but only when \`amount <= 1000\`" is one policy, not an agent-side check you have to trust.

## Enforcement mode

| Mode | Behaviour |
|---|---|
| \`LOG_ONLY\` | Decisions are evaluated and recorded; nothing is blocked |
| \`ENFORCE\` | Decisions are applied — and **default-deny is live** |

Start in \`LOG_ONLY\` to see what your policies would do, then switch to \`ENFORCE\`. The switch is the moment default-deny begins: once the engine is attached in \`ENFORCE\`, a tool with no policy permitting it is denied.

> Authoring a policy replays the gateway's existing inbound authorizer configuration, so adding per-tool authorization can never strip the token validation that got the caller there.`,
      },
      {
        id: 'multi-tenancy',
        title: 'Multi-tenancy and roles',
        content: `# Multi-tenancy and roles

## A tenant is a set of Entra groups

A tenant record carries one or more **Entra group ids**, and that is the whole membership model — there is no separate user-to-tenant table to keep in sync. A caller's tenants are resolved by intersecting their group membership against the registered tenants.

Group membership comes from the token's \`groups\` claim when that claim is present (an **empty** claim is authoritative — the directory is not consulted to second-guess it), and otherwise from a Microsoft Graph transitive-membership lookup. Belonging to no tenant is a normal state, not an error: it yields empty lists, never a failure.

\`tenant_id\` is **required when you create** an agent or an MCP server. A record cannot be created outside a tenant, which is what makes the scoping rule below total.

## Scoping is server-side, and a foreign resource is a 404

Every scoped read and write passes through one visibility check: a **platform admin sees every tenant**, a resource explicitly marked shared is visible to anyone, and everyone else sees only resources belonging to a tenant they are a member of. A resource with no tenant at all is invisible to a non-admin.

The consequence matters more than the rule:

- Reading another tenant's resource returns **404 — byte-identical to a resource that does not exist**. The API never confirms that someone else's agent exists.
- Creating **into** another tenant returns **403**, because no resource exists yet to hide.

This is enforced in the backend, on every route, not in the UI. Hiding a card would not be security; refusing the request is.

## The three platform roles

Roles are Entra **app roles** on the backend app registration, and arrive as claims:

| App role | May do |
|---|---|
| \`Platform.Viewer\` | Read — every GET |
| \`Platform.Operator\` | Read, plus mutations: register, update, request publication |
| \`Platform.Admin\` | Everything, plus lifecycle decisions, grants administration, approvals — and cross-tenant visibility |

The rule is uniform and worth memorising: **GET needs Viewer, a mutation needs Operator, and lifecycle / approval / user administration needs Admin.** Gating is applied per endpoint.

A caller whose token carries none of the three is treated as **Viewer** — least privilege, not an error, so an unassigned user gets a readable platform rather than a wall of failures.`,
      },
    ],
  },
  {
    id: 'operate',
    title: 'Consume and operate',
    children: [
      {
        id: 'marketplace',
        title: 'Marketplace',
        content: `# Marketplace

The marketplace is the consumer surface: two catalogues — **Agents** and **MCP Servers** — of products another team has published, with a subscription request that ends in real access.

## Publication is the only door

A product does not appear in the marketplace because someone flipped a flag. A publisher declares a **datasheet**, an admin reviews the declaration, and approval writes the publication onto the product's own record. Both product types go through the identical flow, and the same publish panel renders on an agent's detail page and an MCP server's.

**Requesting publication** (Operator) is refused unless all four hold:

| Guard | Why |
|---|---|
| The product exists **and is visible to you** | A foreign tenant's product is indistinguishable from a missing one |
| Its lifecycle state is **approved** | The marketplace must not advertise what the platform has not approved |
| Its identity is **provisioned** | A product with no service principal or Invoker role can never be granted — publishing it would advertise a guaranteed dead end |
| No request is already **pending** | A second declaration would overwrite the one an admin is mid-review on |

There is one request record per product; a re-publication overwrites it, and the decision history lives in the record's audit fields.

## The datasheet is a declaration, not a measurement

| Required | Optional |
|---|---|
| \`owner_team\`, \`support_contact\`, \`data_classification\` | SLA tier, compliance list, support hours, version, region, guardrails, pitch |

The three required fields are the ones a consumer cannot act without: who owns it, who to call, and how the data is classified. Everything else may be omitted, and an omitted field is simply not rendered.

**There is deliberately no field for anything the platform would have to measure** — no uptime, no latency, no live status, no rating. A publisher-asserted datasheet cannot truthfully declare a measurement, so those fields do not exist rather than existing and being empty. What you see on a card is either something a publisher declared and an admin approved, or something the platform itself counts (such as how many teams currently subscribe).

## The admin publish queue

Publish requests land in an admin queue, pending first. An admin reviews the declared datasheet and approves, rejects with a reason, or later unpublishes.

**Approval writes the record first, then the decision.** The publication — the datasheet plus who attested it and when — is written onto the product's record before the request is marked approved, so a failed write leaves the request pending and retryable rather than marked done. The attesting identity comes from the approving admin's validated token, never from the request body: a publisher cannot self-certify their own SLA tier.

Unpublishing keeps the publication block and marks it not-published, so the declared history survives a delisting.

## Subscribing

A consumer requests a subscription from a card. An admin approves, and **approval applies the real Entra grant** — the same app-role assignment described in **Access and grants**, with the \`Invoker\` role only. A subscription grants the ability to **use** a product, never to administer it.

| Subscription state | Meaning |
|---|---|
| \`pending\` | Requested, awaiting an admin decision |
| \`approved\` | The Entra assignment is in place |
| \`rejected\` | Declined |
| \`failed\` | The decision was made but the grant did not apply — retryable |
| \`revoked\` | Access withdrawn |

\`failed\` exists because the grant is real work against a real directory. The platform records the honest outcome and offers a retry instead of reporting success for access that was never applied. Revoking a subscription tears down the assignment it created.`,
      },
      {
        id: 'observability',
        title: 'Observability and cost',
        content: `# Observability and cost

Observability is **Langfuse**, deployed as part of the stack by the same \`terraform apply\`, and read back through the API rather than embedded as an iframe. Registration **attempts** to provision each agent its own Langfuse project, on a best-effort basis: it is a no-op when Langfuse is not configured, and a failure is logged rather than failing the registration, which leaves that agent without a project. Where a project does exist, traces are attributed structurally — by which project they landed in — rather than by a tag that could be omitted or forged.

## What you get per agent

The agent detail page has live **Traces** and **Cost** tabs:

- **Traces** — the agent's recent traces, with latency, cost and the calling user.
- **Cost** — token usage and spend, broken down by model.

## What you get across a scope

The Observability dashboard reads one endpoint with a \`scope\` of \`platform\`, \`tenant\` or \`project\`, over a date window that defaults to the trailing 30 days. It returns merged totals, a daily series, a per-model breakdown, and a **per-agent list** so you can see which agent is driving the number.

Cost is **real**, not modelled: it is aggregated from the same Langfuse data as the traces, per agent and per tenant.

## Tenant scoping applies here too

Every scope is filtered through the same visibility rule as everything else — a scoped caller sees only their own tenant's agents, an admin sees all — and the per-agent list is built from that filtered set, so a foreign agent is never enumerated. A project scope you cannot see returns **zeroed metrics rather than an error**, which is deliberate: an error would confirm that the project exists.

Reading metrics needs **Viewer**. If Langfuse is not configured, the settings endpoint reports that plainly and the UI renders a not-configured state instead of empty charts. The endpoint never returns a key value — only whether a host is configured, and which one.

## The honest limitation

**Only Bedrock AgentCore runtimes are wired for tracing today.** The Langfuse project is attempted for every agent, so it is not the gate. What gates tracing is invocation: only an agent that passes the AgentCore gate — a runtime ARN, Entra auth, and the Bedrock platform — can be invoked through the platform, and only an invocation produces a trace. The registry accepts other platforms and governs those records fully, but they emit no traces here, so their Traces and Cost tabs have nothing to show. That is a wiring gap, not a display bug.`,
      },
    ],
  },
  {
    id: 'reference',
    title: 'Reference',
    children: [
      {
        id: 'api',
        title: 'API reference',
        content: `# API reference

**The API documents itself, and this page will not restate it.** The backend serves live OpenAPI, generated from the routes that are actually deployed, so it cannot drift the way a hand-maintained endpoint list does. Point your browser or your client generator at your own deployment:

| Path | What it serves |
|---|---|
| \`/docs\` | Interactive Swagger UI over the live schema |
| \`/redoc\` | The same schema, ReDoc rendering |
| \`/openapi.json\` | The raw OpenAPI document — feed this to a client generator |

Every governance route is versioned under **\`/api/v1\`**. When the API is behind API Gateway the stage segment is also served, so both \`/api/v1/...\` and \`/<stage>/api/v1/...\` resolve.

## The unauthenticated surface

Exactly these paths are public, and this is deliberate rather than accidental:

\`\`\`
/                 name, version, status
/ping             reachability
/health           health probe
/docs             Swagger UI
/redoc            ReDoc
/openapi.json     OpenAPI document
\`\`\`

Nothing else. Every route under \`/api/v1\` requires a validated Entra token, and the public endpoints above disclose no configuration, no environment name and no internal identifiers.

## Calling it

Send the Entra access token as a bearer token. The token's audience must be the backend's Application ID URI — \`api://agp\` in the examples — and its \`roles\` claim is what selects your platform role. See **Multi-tenancy and roles** for what each role may do.

\`\`\`bash
curl -H "Authorization: Bearer <ACCESS_TOKEN>" \\
  "$API_URL/api/v1/agents"
\`\`\``,
      },
      {
        id: 'roadmap',
        title: 'Not built yet',
        content: `# Not built yet

This page exists so the rest of the documentation can be trusted. Everything described in the other sections is implemented and verifiable in the source. The surfaces below are **not** — they are roadmap, and the platform says so in its own UI rather than here only.

## Pages that are design, not function

Ten pages ship as **${COMING_SOON_TITLE.toLowerCase()}** designs. Each one carries a banner reading **"${COMING_SOON_BODY}"**, and its nav entry is tagged \`${SOON_TAG_LABEL}\`. Where a live page shows one illustrative widget, that widget is labelled **"${SAMPLE_BADGE_LABEL}"**:

| Page | Where |
|---|---|
| Audit & Incidents | Govern |
| Cost & FinOps | Govern |
| Model Registry | Govern |
| Prompts | Govern |
| Access Keys | Operations |
| Deployments | Operations |
| Experiments | Operations |
| Model Catalog | Operations |
| Playground | Operations |
| Studio | Operations |

They are kept in the tree because the shape of the eventual feature is part of the design, and deleting them would hide the intent. The banner is the contract: if you see it, nothing on that page is wired to anything.

## Named gaps

- **The audit log.** Governance decisions are recorded on the records they change — a lifecycle transition, a publish decision, a subscription decision all carry who and when — but there is no queryable, tamper-evident audit trail across all of them, and the Audit & Incidents page is a design.
- **The FinOps business-unit rollup.** Cost is real per agent and per tenant (see **Observability and cost**). Rolling it up by business unit or chargeback owner is not built.
- **Automated vendor inventory ingestion.** Third-party AI services are not discovered or inventoried automatically; nothing crawls your estate.
- **Tracing for non-AgentCore runtimes.** The registry governs agents on any platform, but only Bedrock AgentCore runtimes emit traces and cost today.
- **Identity providers other than Entra ID.** The authorization model **is** Entra app-role assignments. Other providers are roadmap, with no date.
- **Unsupervised teardown.** \`terraform destroy\` can leave resources behind on the first pass.

> If a capability is not documented in one of the other sections, assume it is not built. That is the rule this page enforces.`,
      },
    ],
  },
];

/** Inline markdown: `**bold**` and `` `code` ``. */
function inline(text: string): string {
  return text
    .replace(/\*\*(.*?)\*\*/g, '<strong class="text-slate-800">$1</strong>')
    .replace(/`(.*?)`/g, '<code class="bg-slate-100 px-1.5 py-0.5 rounded text-xs text-blue-700 font-mono">$1</code>');
}

/** Render collected pipe-table rows (header first) as one `<table>`. */
function renderTable(tableRows: string[]): string {
  const headerCells = tableRows[0].split('|').filter(c => c.trim());
  const bodyRows = tableRows.slice(1);
  let table = '<div class="overflow-x-auto my-4"><table class="w-full text-sm border-collapse">';
  table += '<thead><tr class="bg-slate-50">' + headerCells.map(c => `<th class="border border-slate-200 px-3 py-2.5 text-left font-semibold text-slate-700">${c.trim().replace(/\*\*/g, '')}</th>`).join('') + '</tr></thead><tbody>';
  for (const row of bodyRows) {
    const cells = row.split('|').filter(c => c.trim());
    table += '<tr class="hover:bg-slate-50/50 transition-colors">' + cells.map(c => `<td class="border border-slate-200 px-3 py-2.5 text-slate-600">${inline(c.trim())}</td>`).join('') + '</tr>';
  }
  return table + '</tbody></table></div>';
}

// Simple markdown renderer
function renderMarkdown(md: string) {
  const lines = md.split('\n');
  const html: string[] = [];
  let inCode = false;
  let inTable = false;
  let codeBlock: string[] = [];
  let tableRows: string[] = [];

  for (const line of lines) {
    if (line.startsWith('```')) {
      if (inCode) {
        html.push(`<pre class="bg-slate-900 text-slate-100 rounded-xl p-4 overflow-x-auto text-sm my-4 border border-slate-800"><code>${codeBlock.join('\n').replace(/</g, '&lt;')}</code></pre>`);
        codeBlock = [];
      }
      inCode = !inCode;
      continue;
    }
    if (inCode) { codeBlock.push(line); continue; }

    if (line.startsWith('|') && line.includes('|')) {
      if (!inTable) { inTable = true; tableRows = []; }
      if (line.match(/^\|[\s-|]+\|$/)) continue;
      tableRows.push(line);
      continue;
    } else if (inTable) {
      inTable = false;
      html.push(renderTable(tableRows));
      tableRows = [];
    }

    if (line.startsWith('# ')) html.push(`<h1 class="text-3xl font-semibold text-slate-900 mb-4 mt-8 tracking-tight">${line.slice(2)}</h1>`);
    else if (line.startsWith('## ')) html.push(`<h2 class="text-2xl font-bold text-slate-900 mb-3 mt-8">${line.slice(3)}</h2>`);
    else if (line.startsWith('### ')) html.push(`<h3 class="text-lg font-semibold text-slate-900 mb-2 mt-5">${line.slice(4)}</h3>`);
    else if (line.startsWith('- ')) html.push(`<li class="ml-4 text-slate-600 mb-1.5 list-disc list-inside leading-relaxed">${inline(line.slice(2))}</li>`);
    else if (line.startsWith('> ')) html.push(`<blockquote class="border-l-4 border-amber-400 bg-amber-50/50 pl-4 pr-4 py-3 my-4 rounded-r-xl text-slate-700">${line.slice(2).replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')}</blockquote>`);
    else if (line.trim() === '') html.push('<div class="h-2"></div>');
    else html.push(`<p class="text-slate-600 leading-relaxed mb-2">${inline(line)}</p>`);
  }

  if (inTable && tableRows.length) html.push(renderTable(tableRows));

  return html.join('\n');
}

export default function Documentation() {
  const { section } = useParams<{ section?: string }>();
  const [activeId, setActiveId] = useState(section || 'overview');
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [showFloatingButton, setShowFloatingButton] = useState(false);
  const [expandedSections, setExpandedSections] = useState<Set<string>>(new Set());

  // Deep link: when URL param changes, navigate to that section and expand parents
  useEffect(() => {
    if (!section) return;
    setActiveId(section);
    // Auto-expand parent sections so the nav item is visible
    const expandParents = (sections: DocSection[], targetId: string, parents: string[] = []): string[] | null => {
      for (const s of sections) {
        if (s.id === targetId) return parents;
        if (s.children) {
          const found = expandParents(s.children, targetId, [...parents, s.id]);
          if (found) return found;
        }
      }
      return null;
    };
    const parents = expandParents(docs, section);
    if (parents) {
      setExpandedSections(prev => {
        const next = new Set(prev);
        parents.forEach(p => next.add(p));
        return next;
      });
    }
  }, [section]);

  const toggleSection = (sectionId: string) => {
    setExpandedSections(prev => {
      const next = new Set(prev);
      if (next.has(sectionId)) {
        next.delete(sectionId);
      } else {
        next.add(sectionId);
      }
      return next;
    });
  };

  const findContent = (sections: DocSection[], id: string): string | undefined => {
    for (const s of sections) {
      if (s.id === id) return s.content;
      if (s.children) {
        const found = findContent(s.children, id);
        if (found) return found;
      }
    }
  };

  const content = findContent(docs, activeId) || '';

  // Show floating button when scrolled down
  const handleScroll = (e: React.UIEvent<HTMLElement>) => {
    const scrollTop = (e.target as HTMLElement).scrollTop;
    setShowFloatingButton(scrollTop > 100);
  };

  return (
    <div className="h-[calc(100vh-4rem)] bg-white flex relative overflow-hidden">
      {/* Overlay for mobile */}
      {sidebarOpen && (
        <div
          className="lg:hidden fixed inset-0 bg-black/50 z-30"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* Sidebar - fixed height with independent scroll */}
      <aside className={`
        w-64 flex-shrink-0 border-r border-slate-200 bg-white overflow-y-auto
        fixed lg:relative inset-y-0 left-0 z-40 lg:z-auto transform transition-transform duration-300 shadow-xl lg:shadow-none
        h-full
        ${sidebarOpen ? 'translate-x-0' : '-translate-x-full lg:translate-x-0'}
      `}>
        <div className="p-6">
          <h2 className="text-xs font-bold text-slate-400 uppercase tracking-widest mb-5">Documentation</h2>
          <nav className="space-y-1">
            {docs.map((section) => (
              <div key={section.id} className="mb-4">
                <div className="text-[11px] font-bold text-slate-400 uppercase tracking-widest mb-2 px-2">
                  {section.title}
                </div>
                {section.children?.map((child) => (
                  <div key={child.id}>
                    {/* If child has sub-children, show expandable button */}
                    {child.children && child.children.length > 0 ? (
                      <>
                        <button
                          onClick={() => toggleSection(child.id)}
                          className="w-full flex items-center justify-between text-left px-3 py-2 rounded-xl text-sm text-slate-700 hover:bg-slate-100 transition-all duration-150 font-medium"
                        >
                          <span>{child.title}</span>
                          <svg
                            className={`w-4 h-4 transition-transform duration-200 ${expandedSections.has(child.id) ? 'rotate-90' : ''}`}
                            fill="none"
                            viewBox="0 0 24 24"
                            stroke="currentColor"
                            strokeWidth={2}
                          >
                            <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                          </svg>
                        </button>
                        {/* Show nested children when expanded */}
                        {expandedSections.has(child.id) && (
                          <div className="ml-3 mt-1 space-y-1 border-l-2 border-slate-200 pl-2">
                            {child.children.map((subChild) => (
                              <button
                                key={subChild.id}
                                onClick={() => {
                                  setActiveId(subChild.id);
                                  setSidebarOpen(false);
                                }}
                                className={`w-full text-left px-2 py-1.5 rounded-lg text-sm transition-all duration-150 ${
                                  activeId === subChild.id
                                    ? 'bg-blue-50 text-blue-700 font-semibold'
                                    : 'text-slate-500 hover:text-slate-900 hover:bg-slate-50'
                                }`}
                              >
                                {subChild.title}
                              </button>
                            ))}
                          </div>
                        )}
                      </>
                    ) : (
                      /* Regular page without children - clickable directly */
                      <button
                        onClick={() => {
                          setActiveId(child.id);
                          setSidebarOpen(false);
                        }}
                        className={`w-full text-left px-3 py-2 rounded-xl text-sm transition-all duration-150 ${
                          activeId === child.id
                            ? 'bg-blue-50 text-blue-700 font-semibold'
                            : 'text-slate-500 hover:text-slate-900 hover:bg-slate-100'
                        }`}
                      >
                        {child.title}
                      </button>
                    )}
                  </div>
                ))}
              </div>
            ))}
          </nav>
        </div>
      </aside>

      {/* Content - independent scroll */}
      <main className="flex-1 overflow-y-auto" onScroll={handleScroll}>
        {/* Mobile menu button at top */}
        <div className="lg:hidden sticky top-0 z-20 bg-white border-b border-slate-200 px-6 py-4">
          <button
            onClick={() => setSidebarOpen(!sidebarOpen)}
            className="inline-flex items-center gap-2 px-4 py-2 bg-slate-50 hover:bg-slate-100 border border-slate-200 rounded-lg text-sm font-medium text-slate-700 transition-colors"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5" />
            </svg>
            Documentation Menu
          </button>
        </div>

        <div className="max-w-4xl mx-auto px-6 lg:px-10 py-12">
          {/*
            SAFETY INVARIANT for the raw-HTML render immediately below — the ONLY such call in
            this file, and the reason the whole page is authored as constants.
            Every string `renderMarkdown` is ever given is a COMPILE-TIME CONSTANT defined in the
            `docs` tree above (plus the four imported honesty-copy constants, themselves literals).
            No user input, no API response, no route parameter and no `localStorage` value may EVER
            reach it: `activeId` is used only to LOOK UP a section by id, never as content, so a
            hostile `/docs/:section` value can at worst select nothing and render an empty page.
            `renderMarkdown` is intentionally not an HTML sanitizer and must not be treated as one
            — it emits raw tags. If a future change makes any part of this content dynamic, this
            call must be replaced (or the input sanitized) in the SAME commit.
          */}
          <div dangerouslySetInnerHTML={{ __html: renderMarkdown(content) }} />
        </div>
      </main>

      {/* Floating button - outside scroll container, mobile only */}
      {showFloatingButton && (
        <button
          onClick={() => setSidebarOpen(true)}
          className="lg:hidden fixed bottom-6 right-6 z-50 p-4 bg-blue-600 text-white rounded-full shadow-lg hover:bg-blue-700 transition-all animate-fade-in"
          aria-label="Open documentation menu"
        >
          <svg className="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5" />
          </svg>
        </button>
      )}
    </div>
  );
}
