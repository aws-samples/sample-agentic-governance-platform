# Project history

How the Agentic Governance Platform was built, which architectural decisions shaped it, and what was
tried and abandoned along the way. The [README](../README.md) describes what the platform does today;
this document explains *why it looks the way it does* — and is honest about the detours.

AGP started as a fork of an internal AWS accelerator and was rebuilt into a dedicated governance
product between **May and August 2026**. Development ran as a sequence of small, spec-first increments:
each feature got a locked design spec and a contract-heavy implementation plan before any code, and was
live-tested against a real Microsoft Entra ID tenant and a real AWS account before being called done.

---

## Where the platform stands

Built, integrated, and exercised against live infrastructure:

- **Identity and access are entirely Entra ID.** Users, agents, and MCP servers are all Entra
  principals. Every "may this caller reach this target" answer is an Entra app-role assignment, read
  and written live through Microsoft Graph.
- **Agents and MCP servers live in the AWS Agent Registry**, with governance metadata carried in the
  record's own envelope and lifecycle handled by the registry's native approval workflow.
- **Per-tool authorization is Cedar**, evaluated on the MCP gateway against the inbound
  on-behalf-of token, with parameter-level conditions and explicit deny.
- **Multi-tenancy is enforced server-side**, not in the UI: reads filter, writes verify, and a
  foreign record's detail page returns a 404 byte-identical to one that never existed.
- **The build-to-runtime pipeline works end to end** — push to a materialized repo, the project's own
  CI builds and pushes the image, the platform applies the runtime Terraform in its own CodeBuild, and
  the resulting agent lands in the registry already governed.
- **Observability and cost are real per agent** for the agents the platform deploys to Bedrock
  AgentCore, sourced from Langfuse's own token accounting; runtimes on other platforms are not
  instrumented by the platform.
- **Terraform is clean-from-zero, proven rather than asserted.** The whole account was deliberately
  wiped and redeployed in a single `terraform apply` — 197 resources, zero errors — then seeded. That
  exercise is what turned "should work from scratch" into a fact.

For current test-suite numbers and the commands that produce them, see
[Development](../README.md#development). For the sharp edges that ship with the platform, see
[Known limitations](../README.md#known-limitations) — that list is the authoritative one and is not
duplicated here.

---

## Architecture decisions

### Entra ID instead of a local user store

The first real decision, and the one everything else follows from: the platform holds **no** identity
of its own. No password store, no user table, no local group model.

The alternative — a local user store synced from an IdP — was rejected because a governance product
that mirrors identity inherits two problems it cannot solve: drift between the mirror and the
directory, and a second, weaker place to attack. An enterprise buying a *governance* tool cannot be
asked to trust that tool's own copy of who works there.

So users authenticate through Entra ID with the tenant's existing MFA and Conditional Access, and the
platform's roles (`Platform.Admin` / `Platform.Operator` / `Platform.Viewer`) are Entra app roles on the
backend app registration. The cost of the decision is real and worth stating: standing up AGP requires
tenant-admin work — two app registrations and admin consent for a set of Graph application permissions —
before anything can be signed into. Documenting that honestly was easier than owning an identity store.

An early alternative auth stack (Amazon Cognito) existed in the forked codebase and was removed
entirely rather than kept as a fallback. Two auth paths would have meant two authorization models, and
the whole value of the design is that there is exactly one.

### Agents and MCP servers are principals, not resources

Most agent platforms treat "the agent" as a row in a table and "access" as a field on that row. AGP
makes the agent a workload identity with its own app registration, and makes tool gateways (MCP
servers) a third principal type beside users and agents.

The payoff is that **agent-to-agent and agent-to-tool access uses the same mechanism as human access** —
an app-role assignment — instead of a parallel, privately invented scheme. Granting an agent access to
another agent is the same operation, the same audit surface, and the same revoke path as granting a
person.

### Grants live in Microsoft Graph, with no database mirror

Access is never copied into the platform's own store. A grant *is* an `appRoleAssignedTo` entry in
Entra; the governance graph, the access tabs, and the revoke button all read and write Graph directly.

This was a deliberate trade of latency for truth. A local mirror would render faster and would let the
UI work when Graph is slow — and it would also, eventually, be wrong. Revocation is the case that
settles it: a mirrored grant that fails to sync is a permission that still works after somebody
removed it. The gateway enforces against Entra, so Entra had better be the only place the answer lives.

### Governance metadata rides in the registry record's envelope

Agents are stored as records in the AWS Agent Registry, and their governance fields — sponsor, business
unit, data classification, tenant, publication state — are carried inside the record's own custom-JSON
envelope rather than in a side table keyed by agent id.

The reason is the same as for grants: one object, one truth. A side table would have created records
that exist in one store and not the other, and a deletion path that can half-succeed. Lifecycle uses
the registry's **native** approval workflow (`DRAFT → PENDING_APPROVAL → APPROVED`, plus rejected and
deprecated) instead of a hand-rolled state machine, so approval state cannot disagree with the registry.

The platform's *own* domains — tenants, projects, connections, the marketplace, guardrail templates,
deployment metadata — do live in DynamoDB. Those have no registry to belong to.

### Cedar for per-tool authorization

Entra answers "may this caller reach this target at all". It cannot answer "may this agent call *this
specific tool* with *these arguments* under *these conditions*" — that question is per call, per
argument, and needs a policy language.

[Cedar](https://www.cedarpolicy.com/) evaluated by the AgentCore Gateway's policy engine answers it,
after the token is validated and at the point of the call. Policies are authored in plain language in
the UI ("EU data only", "read-only", "business hours") and compiled to Cedar text by the backend; the
backend deliberately runs **no Cedar engine of its own**, because a decision made in the control plane
is a decision that can be bypassed by calling the gateway directly.

Two properties came out of that: the gateway is default-deny once an engine is attached, and `forbid`
wins, which is what makes an all-users guardrail actually a guardrail. A log-only mode exists so a
policy can be observed before it starts blocking traffic.

### Registries are resolved by name at runtime

The two registries (agents and MCP servers) are created by Terraform, but their ids are never written
into configuration. The backend resolves a registry **by name** on first use.

This one was learned the hard way. When the id was a Terraform value, it could only reach the ECS task
definition through a file captured during the plan walk — so the first apply rendered it empty and a
second apply was needed to fix it up. A "deploy the platform" instruction that requires running apply
twice is a bug, not a footnote. Resolving by name deleted the second apply, and there are deliberately
no `*_registry_id` variables to re-add.

### Infrastructure-as-code never lives in the agent's repository

When the platform provisions a governed agent, the developer's repository contains the agent and a
workflow that builds and pushes a container image. It does **not** contain the runtime Terraform.

The platform applies that Terraform itself, in its own CodeBuild project, using a per-stage deploy role
derived server-side from the agent id — never from the request body. The point is that a developer with
write access to their own repo cannot change where their agent deploys, what role it runs as, or which
authorizer guards it. Governance that a pull request can edit is not governance.

Production promotion follows the same principle: `main` is the prod *candidate*, promotion is an
explicit owner-gated action in AGP, and the build endpoint **refuses** a prod deploy arriving over
CI credentials. Images are addressed by content — a git tree sha, then a digest — so promoting means
shipping the exact image that was tested rather than rebuilding and hoping.

### Secrets have exactly one home

Personal access tokens, app keys, user OAuth tokens, and observability credentials go to AWS Secrets
Manager and nowhere else. No secret is stored in a registry record, returned in an API response, or
written to a log. Connection-verification errors map to fixed literal strings rather than
`str(exception)`, because the exception text is where a token ends up in a log by accident.

Runtime agents receive their configuration — granted MCP servers, the delegated-token credential
provider, observability wiring — as injected environment variables, so a governed agent image carries
no gateway URL and no credential.

---

## The engineering record

### Identity first (May – early June 2026)

The rebuild started at the bottom: an access-model spec that made MCP servers a first-class principal,
a real Entra tenant with the platform app registrations, then the full auth swap — MSAL in the
frontend, Entra JWT validation in the backend. A wrinkle worth recording: Microsoft's v2 tokens are
inconsistent about the `aud` claim, so validation accepts a dual audience.

Registry, MCP catalog, user-to-agent access with a genuine on-behalf-of invoke, agent-to-MCP grants,
and Cedar per-tool policies followed in about three weeks. From that point on the platform could
already do the thing it claims to do: grant a human access to an agent, have that agent call a tool
through a gateway, and have the gateway refuse a tool the policy did not allow.

### Governance surfaces (June)

The governance graph — an interactive node-link view of users and groups reaching agents reaching MCP
servers — was built on React Flow and dagre over a single read-only aggregation endpoint sourced live
from Entra. The marketplace arrived next: any signed-in user can subscribe, an admin approves, and
approval applies the *real* grant through the same shared code path the access tab uses. There is no
second grant implementation for the marketplace to drift away from.

Two structural changes landed in the same stretch. Multi-MCP support replaced "latest grant wins" with
an authoritative desired-state set, rebuilding an agent's whole runtime MCP environment on every grant
and revoke. And the legacy cleanup archived every surface inherited from the accelerator that the
governance product does not use — the auth swap's Cognito path went with it.

### From registry to runtime (July)

The largest body of work: turning "an agent is registered" into "an agent is running". A template repo
is materialized into the customer's own Git organization; the project's CI builds and pushes an image;
the platform applies the runtime Terraform; the runtime is born with a declarative Entra JWT
authorizer; its ARN is written back to the registry record. The agent lands governed with no manual
step.

Delete came with it, and shaped more of the design than expected. Teardown is a best-effort, ordered,
idempotent cascade — repository, images, runtime and its state, Entra identity, then the registry
record last — where "already absent" counts as success, so a cascade that fails partway can simply be
re-run. A preview probe drives the confirmation dialog so it only offers artifacts that actually exist.

The constraints that bit hardest here were unglamorous: a CodeBuild buildspec size cap that forced the
spec into S3, an IAM create-then-consume race that needed an explicit wait, and an API Gateway 30-second
timeout that forced repository materialization to become a 202-plus-timeline flow with a resumable
retry rather than a synchronous call.

### The multi-tenancy retrofit (mid-July)

Multi-tenancy was added *after* the registry, marketplace, ops, and graph existed — and retrofitting
isolation into a working system is materially harder than starting with it. Tenant membership was
defined as Entra group membership (resolved from the token's `groups` claim, with a Graph fallback), a
single shared visibility primitive was written, and then **every** read path was made to filter, every
write path to verify, and every cross-tenant detail response to become indistinguishable from a
missing record.

The honest lesson: this should have been decided on day one. The design that came out of the retrofit
is the right one, but it touched nearly every router to get there, and each surface had to be argued
about individually — including the deliberate exception that lets an MCP server be marked shared for
cross-tenant *read and grant* while never being writable from another tenant.

### Observability and cost (late July)

Langfuse became a base Terraform module deployed with the platform, replacing an earlier pipeline-based
deployment. Attribution is **structural** rather than tag-based: one Langfuse project per agent, and
each agent authenticates with its own project key drawn from Secrets Manager. A mistagged trace cannot
land in the wrong agent's cost report, because the credential is the attribution.

Two constraints are worth recording for anyone extending it. Project and key provisioning uses
Langfuse's internal API because the open-source edition gates the creation REST APIs to its enterprise
tier — so that step is deliberately best-effort and idempotent rather than a hard dependency of
registration. And the Langfuse module mirrors upstream container images into your own ECR *during*
`terraform apply`, which is why a local container engine is a prerequisite for applying and not just for
building.

### Hardening and release preparation (early August)

A deliberate non-feature stretch: dead code paths deleted so the test count means something, dev
tooling removed from the backend container along with 35 CVEs, frontend advisories cut from 13 to 2,
and a pass to get live account identifiers, tenant GUIDs, and domains out of the tracked tree. The
research-notes directory became ignored rather than curated, which made "the repository carries design
records, not scratch files" mechanical instead of a habit.

### The design-system migration that was reverted (August)

The whole frontend was migrated to the Cloudscape Design System: application shell, navigation, tables,
modals, wizards, charts, and both graph canvases, across 26 planned tasks. The migration was not
cosmetic — it fixed real defects on the way through, including two detail pages whose error branches
were unreachable, a graph canvas that rendered blank, sizing bugs caused by utility CSS losing to a
framework reset, and **59 status indicators that conveyed meaning by colour alone**.

Then it was rejected. A polish round drew "everything feels huge"; a full mockup-match redesign was
built to five hand-drawn mockups and never got its live test; a fourth round adopting a different
console's chrome was parked two tasks into eleven. At that point the verdict was not about any single
round — the design system as a whole was not what the product wanted.

So the frontend was reverted to the previous UI, and the revert was executed as a **tree restore rather
than a rewrite**: the frontend directory was made byte-identical to the commit before the migration's
first code change. That mattered because it made a claim provable instead of hopeful — every commit
touching the frontend since the anchor belonged to the migration family, and no migration commit
touched anything outside the frontend, so the backend and infrastructure work of the same period was
untouched by construction. The Cloudscape tree is preserved in version control for whichever future
effort revives it, behind a runtime UI-flavor seam that currently has exactly one working option.

**This is in the history on purpose.** Four rounds of UI work were spent and reverted, and the
regressions the revert restored — the colour-only status indicators among them — are documented as
accepted debt rather than quietly reopened. The lesson is cheap to state and was expensive to learn: a
design-system migration needs a live user test at the *end of the first round*, not after the third.

### Registry namespace migration and the clean-from-zero proof (August)

AWS moved the agent registry into its own service namespace in August 2026, with the old namespace
scheduled to disappear and new accounts never having it. Both registry services were ported to the new
namespace and its record schema. Only the registry moved — gateway, runtime, identity, and policy
remained where they were, so the platform's IAM policy grants both namespaces deliberately.

Execution corrected the plan in one place worth remembering: the update envelope needs three levels of
optional-value nesting for custom records and five for MCP tools, not the two the plan predicted.
Registries also became Terraform-owned in this pass, through a script-backed resource, because AWS
ships no Terraform resource for them.

The pass ended with the wipe-and-redeploy described at the top of this document, and that exercise
found four blockers that no incremental deploy could have surfaced: a `count` referencing an unborn
sibling's attribute (latent for over a month), a container-login keychain race under parallel image
mirroring, a TLS-version floor on the default CDN certificate that can never converge and therefore
showed up as permanent drift, and a placeholder account id in an example env file that silently
outranked real ambient credentials. Two Cedar bugs were fixed in the same pass, including a
double-engine case resolved by adopting a gateway's pre-attached engine instead of creating a second.

If there is one process recommendation in this document, it is this: **destroy and rebuild from zero
before you claim reproducibility.** Nothing else finds these.

### UI truth cleanup (August)

The reverted UI came back with its dishonesty intact: pages that looked live but called no API, and a
marketplace inventing service-level numbers. Every route was audited and classified, and the product
was made to say what is real.

One vocabulary covers three granularities — a non-dismissible banner for a page that is entirely a
mock-up, a badge for one illustrative widget on an otherwise-live page, and a tag on a navigation row
whose destination is not built. The copy is pinned in one module and frozen by a test, so no two pages
can quietly disagree about how honest the platform is being. Ten mock pages were bannered with their
content kept and visible; nothing was deleted and no route removed, so each page un-banners when its
backend lands.

Three specific fixes are the ones that show the standard being applied:

- The home page fabricated its metrics. It now reads the registry and the MCP catalog and tallies
  client-side. A tile that showed invented "agent risks" was **replaced** rather than labeled, because
  no risk endpoint exists to replace it with — and the one genuinely unmeasurable tile carries a sample
  badge rather than a plausible number.
- The marketplace's invented telemetry — SLA tier, uptime, p95 latency, star ratings — were literal
  strings in a hardcoded list. They were **removed, not annotated**. They returned later as *declared*
  metadata that a publisher fills in at approval time and the platform stores on the registry record;
  measured telemetry still waits for real observability wiring.
- A seeded "adoption floor" that made consumer counts look healthier than they were was deleted, so a
  cold environment honestly shows zero.

The agent list also stopped prepending a synthetic row from the operations demo store. A fabricated row
in a registry listing is indistinguishable from a governed agent, which makes it the most expensive
kind of fake.

### Inherited surfaces that never became product

The fork brought whole verticals with it — a planning suite and a documentation section. They were kept
through the legacy cleanup rather than deleted, and then never developed into the governance product.
What survives today is informational: static pages that make no API calls and claim nothing. They are
named here so their presence in the navigation is not mistaken for a feature, and their absence from
the README's capability table is not mistaken for an omission.

---

## What this history is for

Three things an adopter can reasonably take from it:

1. **The identity and authorization design is the load-bearing part**, it was decided first, and it has
   not changed since. Everything else in the platform is built on top of "Entra is the only source of
   truth for access".
2. **The parts that were retrofitted show it.** Multi-tenancy is correct but was expensive; that is
   why the isolation rules are documented as explicit contracts rather than left implicit.
3. **What is unbuilt is named, not implied.** The [Roadmap](../README.md#roadmap) and
   [Known limitations](../README.md#known-limitations) sections of the README exist because the same
   audit that bannered ten mock pages also went through the documentation.

*Last updated 2026-08-11.*
