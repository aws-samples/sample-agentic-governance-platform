# AWS service inventory

What this platform deploys, in plain English: which AWS services it uses, which account each one
lands in, and what it is there for. Nothing here is about Terraform or module internals — those live
in the [infrastructure README](../platform/control_plane/infrastructure/README.md).

Two rules make the list easier to read.

**Naming.** Almost everything the platform creates is named with the same prefix —
`agp-cp-<environment>-<last six digits of the account number>`, so a live name reads like
`agp-cp-dev-123456`. Searching an account for that prefix finds most of what the platform created,
but not quite all of it: a handful of names are deliberately different, because they had to be
shorter or because the name itself is a contract something outside the platform relies on. Treat a
prefix search as a good starting point rather than a complete inventory. The account digits are in
the name so that globally unique names cannot collide between accounts.

**Accounts.** There is one **platform account**. It holds everything in the first two sections
below, and the build and deploy machinery in the last one. Deployed agents run in **tenant
accounts**. By default there is only one AWS account and it plays both roles; a separate tenant
account is supported, but adding one is [an onboarding exercise](tenant-account-onboarding.md)
rather than a switch.

## Platform account — core

| AWS service | Role |
|---|---|
| Amazon VPC | The private network everything runs in: subnets across two availability zones, NAT gateways for outbound traffic, and endpoints that keep image pulls and logging off the public internet. Created only if you do not supply a network of your own. |
| Amazon ECS on AWS Fargate | Runs the backend API as containers, with no servers to manage. |
| Elastic Load Balancing | Spreads requests across the backend containers and health-checks them. |
| Amazon API Gateway | The HTTPS entry point for the API — every call the console makes arrives here. |
| Application Auto Scaling | Adds and removes backend containers as load moves, between one and four. |
| Amazon DynamoDB | The platform's own records: tenants, projects, repositories, deployment history, guardrails, marketplace listings, the application catalog, Git connections and App Factory submissions. |
| Amazon S3 | Hosts the built web console, and holds project archives uploaded from the browser. |
| Amazon CloudFront | Serves the console over HTTPS, and is the only route into the bucket behind it. |
| Amazon ECR | Stores the backend's container image, and the one shared repository that holds every agent image. |
| AWS Secrets Manager | The identity provider's client secret, plus the tokens and keys the platform creates as you connect Git organizations and register agents. |
| AWS IAM | The roles the backend runs as, and the roles it creates so your CI can push agent images. |
| Amazon CloudWatch | Logs, metrics, one dashboard and four alarms for the platform's own services. |
| Amazon Bedrock | Holds the guardrails the platform creates, which filter what a governed agent may be asked and may reply. |
| Amazon Bedrock AgentCore | The identities, credential providers and tool-authorization policies the platform mints for each registered agent and tool server. |
| AWS Agent Registry | Two registries — one for agents, one for tool servers — the catalog the console reads from. |
| AWS Certificate Manager and Amazon Route 53 | A certificate and DNS records for a custom domain. Created only if you supply one; a default deploy has neither. |

*Derived from `platform/control_plane/infrastructure/modules/{networking, ecs, api_gateway, dynamodb, s3, cloudfront, ecr, secrets_manager, observability, agent_registry, agent_ecr}`.*

## Platform account — only because of Langfuse

Langfuse is the tracing tool the platform runs for you, so that every agent call is recorded and
reviewable. It is self-hosted, which means the platform deploys the whole application — database
included — into the platform account. This is by far the largest group.

| AWS service | Role |
|---|---|
| Amazon Aurora PostgreSQL | Langfuse's main database: its users, projects and API keys. Two always-on instances. |
| Amazon ECS on AWS Fargate | Runs Langfuse's three containers: the web app, a background worker, and its trace store (ClickHouse). |
| Amazon EFS | Fargate containers have no permanent disk, so the trace store keeps its files on a shared file system. |
| Amazon ElastiCache | The queue and cache between the Langfuse web app and its worker. |
| Amazon S3 | Uploaded trace events, batch exports and media files. |
| Amazon ECR | In-account copies of the Langfuse and trace-store images, because Docker Hub limits how often images may be pulled. |
| Elastic Load Balancing | The front door for the Langfuse web app; it refuses any request that did not arrive through CloudFront. |
| Amazon CloudFront | Langfuse's HTTPS address, and the thing that adds the secret header the load balancer checks for. |
| AWS Lambda (at the edge) | Signs users into Langfuse transparently, and adjusts response headers so the Langfuse screens can be shown inside the console. |
| AWS Cloud Map | Private DNS — how the Langfuse containers find each other. |
| AWS Secrets Manager | Langfuse's database, cache and trace-store passwords, and the keys the platform uses to call it. |
| Amazon CloudWatch | Container logs, one group per Langfuse service, plus a fourth group for shell sessions opened into a running container. |

*Derived from `platform/control_plane/infrastructure/modules/langfuse`.*

## Tenant accounts — agent hosting

Where a governed agent actually runs — in the platform account by default, or in a separate tenant
account when the tenant's record names a deploy role there. The runtime, its execution role, its
image copy and its tracing secret all appear when you deploy an agent, not when you install the
platform. The deploy role the platform assumes in order to create them is the exception: a default
single-account install creates that role for you up front, but a separate tenant account is expected
to have one already — its owner creates it by hand before the first deployment, and nothing here
creates it for them. Both sides of that contract are in
[tenant and AWS account onboarding](tenant-account-onboarding.md).

| AWS service | Role |
|---|---|
| Amazon Bedrock AgentCore | Runs the agent: one runtime per agent per stage, created when you deploy it. |
| Amazon Bedrock | The models the agent calls; its runtime is granted access to them. |
| AWS IAM | An execution role for each runtime, carrying only what that agent needs. The deploy role the platform assumes in order to create that runtime is the prerequisite described above, not something created here. |
| Amazon ECR | The registry a runtime pulls its image from. When the runtime lives in a separate account, the image is copied there first. |
| AWS Secrets Manager | The agent's own tracing keys, read by its runtime at start-up. |

*Derived from `platform/control_plane/infrastructure/modules/{agentcore_runtime, default_tenant}`.*

## Build & deploy

| AWS service | Role |
|---|---|
| AWS CodeBuild | One project for the whole platform, in the platform account. Every agent deployment runs here: it reads the agent's record, checks the image, and creates or updates the runtime. |
| Amazon S3 | Deployment state — one record per agent per stage — plus the build instructions a build downloads before it starts, and the packaged runtime module the platform hands to each connected Git organization. |
| Amazon DynamoDB | A lock table, so two deployments of the same agent cannot run at the same time. |
| AWS IAM cross-account deploy role | The role in the target account that a build assumes in order to create the runtime. Both sides of that contract are in [tenant and AWS account onboarding](tenant-account-onboarding.md). |
| AWS STS | Issues the short-lived credentials for that hop. |
| Amazon CloudWatch | Build logs, kept for two weeks. |

*Derived from `platform/control_plane/infrastructure/modules/{codebuild, state_backend}`.*

## Worth knowing

- **The tracing stack is most of what runs all the time.** Two database instances that never scale to
  zero, a cache node, and roughly ten times the compute the platform's own API uses — all of it up
  whether or not anyone opens a trace. What that adds up to on a bill is not worked out here.
- **Langfuse's address is on the public internet.** What keeps strangers out is a firewall rule that
  admits only CloudFront, plus a secret header CloudFront adds — not private networking.
- **The trace store is a single container with a single disk**, so trace collection pauses briefly
  every time it is redeployed. A replicated setup would avoid that; this one cannot.
- **The four alarms notify nobody.** They change state, and there is no mailing list, chat channel or
  paging target behind them. Wiring one up is a deliberate follow-up, not an oversight to discover
  during an incident.
- **A default deploy has no custom domain.** The console and the API answer on AWS-generated
  hostnames, over HTTPS.
- **The backend has no image until you push one**, so a fresh deploy shows zero running API
  containers. That is the expected first state, not a failure.
- **The machine you deploy from is part of the picture.** It copies the tracing images into your
  account and creates the two registries itself, so it needs a working container engine with access
  to Docker Hub *and* a working backend Python environment. Missing either one fails the install
  partway through.
- **Deleting one of the two registries would delete every record in it**, so nothing in the platform
  ever deletes one — not even on teardown.

## Related

- [Data model](data-model.md) — what the platform's tables actually hold, and which system owns
  each fact.
- [How an agent is deployed](agent-deployment.md) — how an agent image becomes a running runtime, and
  why GitHub never deploys it.
- [Tenant and AWS account onboarding](tenant-account-onboarding.md) — what a tenant is, and the
  deploy role a tenant account must already have.
- [Infrastructure README](../platform/control_plane/infrastructure/README.md) — the deploy runbook
  and the Terraform detail this document deliberately leaves out.
