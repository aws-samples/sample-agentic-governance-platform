# Documentation

The written guides to how this platform actually works. Each guide to a mechanism is derived from
the code in this repository, cites the files it describes, and states partial or defective
behaviour as a known limitation rather than leaving it implied; the Entra setup guide states its
failure modes in a troubleshooting table instead, and the history is an engineering record.

If you are standing the platform up for the first time, read
[Microsoft Entra ID setup](entra-setup.md) first — nothing else functions until an identity
provider is configured. Otherwise start with whichever question you arrived with.

## Security

- [Token propagation](token-propagation.md) — every hop a caller's token takes from
  browser sign-in to a tool call: where it is stored, how it is validated, the two On-Behalf-Of
  exchanges, what is never logged, and a failure catalog per hop.

## Architecture

- [AWS service inventory](services.md) — every AWS service the platform deploys, grouped by the
  account it lands in, with a plain-English role for each: the platform core, the part that exists
  only for tracing, agent hosting, and the build path.
- [Data model](data-model.md) — the five systems that own facts, every DynamoDB table
  and the partitions inside it, the conventions that repeat, and what is deliberately not stored.
- [Authorization layers](authorization-layers.md) — the three gates a governed request
  passes (platform role, tenant visibility, per-project role), the refusal each produces, and why
  two of them are routinely mistaken for defects.

## Registration and operations

- [Registering an agent or an MCP server](agentcore-registration.md) — the registry
  record and who owns each fact in it, the lifecycle state machine, the identity-minting timeline,
  the two registration paths, and grants.
- [How an agent is deployed](agent-deployment.md) — when a developer merges, GitHub builds the
  container image and the platform deploys it into the tenant's account; why GitHub can never
  deploy, and why reaching production is a human decision.

## Setup

- [Microsoft Entra ID setup](entra-setup.md) — the two app registrations, screen by screen in
  twelve steps with fill-in templates, where every value goes, and a troubleshooting table.
- [Databricks account onboarding](databricks-onboarding.md) — connect a Databricks workspace so
  its Apps can be discovered, registered, and governed under federated Entra identity: the two
  credentials, capability probing, federation vs. invoke-unavailable, and a troubleshooting table.
- [Tenant and AWS account onboarding](tenant-account-onboarding.md) — what a tenant is, what an
  AWS account must already have before it can host agents, the steps to create a tenant, and where
  the support stops today.

## Governance

- [Cedar tool policies](cedar-tool-policies.md) — per-tool authorization at the gateway: what a
  policy can and cannot say, where policies live, what the first policy does to a gateway, and what a
  deny looks like from the outside.

## History

- [Project history](project-history.md) — where the platform stands, the architecture decisions
  behind it and the reasoning at the time, and the engineering record that produced them.

## Component READMEs

- [Repository README](../README.md) — what the platform does, the repository layout, and how to
  deploy and run it.
- [Control-plane backend](../platform/control_plane/backend/README.md) — the FastAPI service: its
  layout, configuration, local run, and test suite.
- [Control-plane frontend](../platform/control_plane/frontend/README.md) — the React single-page
  application: its layout, configuration, local run, and build.

## Conventions these guides share

- **Code is the source of truth.** Claims about mechanism carry repo-relative paths, with
  `file.py:line` where the exact line matters. Where a guide and the code disagree, the code is
  right and the guide is a bug.
- **Known limitations are stated, not implied.** They appear as blockquoted callouts with the
  citation that proves them, in present tense, with no promise of a fix.
- **Identity is provider-neutral by design.** Microsoft Entra ID is the provider supported today;
  where a mechanism is specific to it, the guides say so.
- **No diagrams.** The [high-level architecture](high-level-architecture.png) is a hand-maintained
  executive drawing kept alongside these guides; the guides use prose and tables, and where the
  two disagree, the guides are the current truth.
