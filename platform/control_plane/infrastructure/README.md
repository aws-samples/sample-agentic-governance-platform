# Control Plane Infrastructure

This directory contains Terraform configuration for deploying the Agentic Governance Platform (AGP) control-plane infrastructure on AWS.

How a deploy and a teardown are run, the commands and the environment's files, is in [`DEPLOYING.md`](DEPLOYING.md); the scripts' reference is [scripts/README.md](scripts/README.md). This document is the Terraform-level reference behind them.

## Architecture

The infrastructure includes:

- **API Gateway**: HTTP API with VPC Link for private integration
- **ECS Fargate**: Containerized backend service with auto-scaling
- **DynamoDB**: Eight tables, one per platform domain — app factory, application catalog, deployment metadata, guardrails, marketplace, connections, projects, tenants. Agent and MCP-server records are **not** here; they live in the AWS Agent Registries (see below)
- **S3**: Project archives and frontend static hosting
- **CodeBuild**: Runtime-build execution (Epic 21/22) — builds and provisions each app-factory-generated AgentCore runtime in an isolated Docker container; triggered by the backend via `StartBuild`
- **Agent ECR**: Shared container registry for app-factory-generated agent images. The GitHub Actions OIDC push-role is **not** created here — its trust policy names the account's GitHub OIDC provider, which IAM validates at role-create time, so on an account that has never connected GitHub the role was un-creatable at apply time. The backend now creates both the provider and the role idempotently on the first GitHub connection (`ecr_push_role_service.ensure_shared_role`)
- **AgentCore Runtime**: The root apply only **zips the `modules/agentcore_runtime` module and uploads it to S3** (`runtime_module.tf`) — there is no `module "agentcore_runtime"` block here. The runtimes themselves are provisioned per agent, per stage, by CodeBuild against that staged module (Epic 20/21)
- **Langfuse**: Base Langfuse v4 observability stack (Epic 26) — provisioned by the root `terraform apply`; the backend reads its metrics/traces via the `/observability/*` routes
- **State Backend**: S3 bucket + DynamoDB table for the **CodeBuild agent-runtime** Terraform state, and the home of the staged runtime-module zip (`runtime_module.tf`). It is **not** this stack's own state — that bucket is created by `scripts/deploy-full.sh` on a first deploy and is named in `backend.<ENV_NAME>.config` (see [Remote State Management](#remote-state-management))
- **CloudFront**: CDN for frontend distribution
- **CloudWatch**: Logs, metrics, alarms, and dashboards
- **ECR**: Container registry for backend Docker images
- **VPC** (Optional): Can use existing VPC or create new one

### One thing here isn't a normal Terraform resource: the Agent Registries

Everything above is a standard AWS resource that Terraform creates directly. The two **AWS Agent
Registries** (`agp-agents` and `agp-mcp-servers`, where the platform stores its agent and MCP-server
records) are the exception, and it's worth knowing why.

**AWS doesn't offer a Terraform resource for them yet.** Agent Registry is a new service — it moved into
its own `agent-registry` namespace on 2026-08-06 — and the AWS provider hasn't caught up. The one
registry resource that exists (`aws_bedrockagentcore_registry`) points at the retired namespace, ships
marked deprecated, and doesn't work on newer AWS accounts at all. There's no data source either.

**So `modules/agent_registry/` calls a small script instead** (`backend/scripts/ensure_registry.py`),
which creates each registry if it isn't there already. Terraform still decides *whether* the registries
exist and what they're called — it just delegates the API call.

**What that costs you, in practice:**

| | |
|---|---|
| **A Python environment is required at apply time** | The script runs on whatever machine runs `terraform apply`, so the backend virtualenv must exist. Terraform checks this up front and tells you how to fix it, rather than failing halfway through. |
| **Terraform can't see inside a registry** | It knows the registry exists; it doesn't track the records in it. Those are application data, managed through the app. |
| **Deleting a registry is deliberately not automated** | Destroying a registry destroys every record in it, so the module has no destroy step. If you really want one gone, delete it by hand. |
| **`terraform plan` won't show registry drift** | If someone renames or deletes a registry outside Terraform, plan won't notice. The next apply re-creates it if it's missing. |
| **Two registries can share a name** | AWS doesn't enforce unique names. If that ever happens the platform refuses to guess and fails with both ids — picking one silently would split your catalog in a way nobody would notice. A half-created (`CREATE_FAILED`) registry is ignored in favour of a healthy one. |

**What it does *not* cost you:** a fresh deploy is still a single `terraform apply`, and you never
paste an id anywhere. AWS generates each registry's id, and the platform looks it up by name when it
needs it — so nothing waits on a value Terraform can't know yet.

**When AWS ships a real resource,** this becomes a small, contained change: replace the script call in
`modules/agent_registry/main.tf` with the resource, drop the Python prerequisite, and registry drift
starts showing up in `plan` like everything else. The module boundary exists precisely so that swap
touches one file. Tracking issue: [hashicorp/terraform-provider-aws#48694](https://github.com/hashicorp/terraform-provider-aws/issues/48694).

## Prerequisites

Everything below is a **host** prerequisite. Nothing has to exist in the AWS account beforehand:
`scripts/deploy-full.sh` creates the Terraform state bucket on a first deploy, and the SSM image parameter
`/agp/<ENV_NAME>/backend-image` is written by the first apply. The only values you supply outside the
variables file are the two secrets, `TF_VAR_entra_backend_client_secret` and
`TF_VAR_langfuse_admin_password`, exported in your shell.

- **Terraform 1.15.7** (pinned in `.terraform-version`; `main.tf` requires >= 1.15) and the **AWS provider
  >= 6.53, < 7.0**. The provider floor is the load-bearing half: `modules/agentcore_runtime`'s
  `lifecycle_configuration.max_lifetime` only plans consistently from 6.53 onward. The root
  `.terraform.lock.hcl` is **committed**, generated for `linux_amd64` and `darwin_arm64`, so every host resolves the same provider build; module-level lock files stay ignored. Bump the
  constraint and regenerate the lock file for both platforms in the same change
  (`terraform providers lock -platform=linux_amd64 -platform=darwin_arm64`).
- AWS CLI configured with appropriate credentials, targeting **us-east-1**.
- **The backend Python venv must exist before the first apply**, at
  `platform/control_plane/backend/venv/`. `modules/agent_registry` creates both registries by running
  `backend/scripts/ensure_registry.py` through that interpreter (`python_bin` defaults to
  `../backend/venv/bin/python`), because the script needs `boto3`. The venv is **gitignored**, so it does
  not exist on a fresh clone and `terraform apply` fails inside the provisioner with a bare "No such file
  or directory". Create it first:

  ```bash
  cd ../backend && python3 -m venv venv && venv/bin/pip install -r requirements.txt
  # or simply: ../backend/run_dev.sh   (creates the venv, installs deps, then serves — Ctrl-C after setup)
  ```

  Creating the venv is the **only** supported remedy. There is deliberately **no `-var` that points the
  module at a different interpreter**: `python_bin` is an input of `modules/agent_registry` with a default
  and is not plumbed through from the root, so any `-var` naming it would be an *undeclared* root variable —
  Terraform accepts those with a warning and then ignores them, which would leave you blocked with no
  indication why. (An earlier version of this section advertised exactly such a flag. It never did
  anything.) A `fileexists(var.python_bin)` **precondition** on `terraform_data.registry` now catches a
  missing interpreter at **plan** time, and its message repeats the command above — so this fails before any
  AWS call rather than midway through the provisioner.
- **A running container engine (Docker or [finch](https://github.com/runfinch/finch)) with Docker Hub egress
  on the apply host.** The Langfuse module mirrors its upstream images into ECR from the apply host
  (`null_resource.push_images` → `modules/langfuse/mirror-image.sh`), and the Langfuse ECS services cannot
  start without it — with no engine reachable, the apply **fails partway through**. `CONTAINER_ENGINE=finch` in front of
  `scripts/deploy-full.sh` forces finch. See `modules/langfuse/README.md`, "Operator notes".
  *An ECR pull-through cache would remove this prerequisite entirely — recommended, not yet implemented.*
- An AWS account with permissions to create IAM roles and service-linked roles
  (`iam:CreateServiceLinkedRole` — Aurora/ElastiCache/Lambda@Edge replication need theirs on the first apply
  in a new account). No OIDC-provider permission is needed to apply: the stack creates none (see
  [Fresh-account flags](#fresh-account-flags)).

### Fresh-account flags

**There are none, and there is nothing to do between applies either.** Terraform creates both registries
itself (E32) and no longer handles their ids at all — the backend resolves each registry by NAME at first use
— so the row that used to live here ("set `agent_registry_id` / `mcp_registry_id` after the first apply") and
the second apply it belonged to are both gone. Those two variables no longer exist, and adopting a registry
this stack did not create is deliberately **not** supported.

> **A from-zero deploy is a SINGLE `terraform apply`.** It used to need two — see
> [One apply is enough](#one-apply-is-enough--including-from-zero) if you are working from an older runbook.

**There is nothing to configure for GitHub.** This stack deploys **zero GitHub artifacts** — no OIDC provider,
no push role, no data source that needs either to pre-exist — so a fresh apply works on an account that has
never seen GitHub, and a customer who never connects GitHub never carries a GitHub dependency. Git-provider
integrations are a **platform** capability: the first GitHub org an operator connects in the UI bootstraps the
account-global `token.actions.githubusercontent.com` OIDC provider and the shared ECR-push role, then a per-org
push role per connected org (`backend/src/services/github_oidc_provider_service.py` +
`ecr_push_role_service.py`; the ECS task role carries the scoped IAM grants). Future Git providers follow the
same pattern. If an account already has the provider (from GitHub's own onboarding or another stack), the
bootstrap adopts it — IAM allows only one per URL and the backend's ensure is a get-or-create.

## How the pieces fit

The setup path lives in the root README: the two Entra app registrations you create by hand and the
redirect-URI loop that can only be closed after the first deploy are in its Bootstrapping section, alongside
the host Prerequisites. [`DEPLOYING.md`](DEPLOYING.md) is the deploy itself. What follows is what the deploy
script does with Terraform, for when you need to look underneath it.

There is **no `.env`** in this directory and Terraform never reads one. The configuration chain is
**`env/<ENV_NAME>.tfvars` + `TF_VAR_*` secrets > ambient environment**: region and account come from the
credentials in play, and everything else is a Terraform variable declared in `variables.tf` and assigned in
your variables file.

### The variables file

`env/<ENV_NAME>.tfvars`, copied from `env/dev.tfvars.example`, assigns **every** variable in `variables.tf`
explicitly — including those with defaults, so a default change never silently reaches a deployed
environment — except `environment`, which the scripts pass as `-var environment=<ENV_NAME>`, and
`container_image`, which they pass as the image they just built. To reuse an existing VPC, set `vpc_id` /
`public_subnet_ids` / `private_subnet_ids` there — see [Using Existing VPC](#using-existing-vpc). Leave
`hosted_zone_id` empty and you get the CloudFront domain rather than a custom one.

`plan.sh` runs Terraform with `-input=false`, so a missing `TF_VAR_entra_backend_client_secret` fails the
plan with "No value for required variable"; `langfuse_admin_password` has a default and a missing one is
not caught that way.

### Initialising by hand

The root `backend "s3" {}` block is empty on purpose; a bare `terraform init` does not initialise. Every
script runs the init itself. Run it yourself only to inspect state or outputs (`terraform output`,
`terraform state list`):

```bash
terraform init -backend-config=backend.<ENV_NAME>.config -input=false -reconfigure
```

### Plan and apply

`scripts/ci/plan.sh <ENV_NAME> --image <ref>` saves `plan.out` and prints a create/update/delete summary;
`scripts/ci/apply.sh <ENV_NAME> --image <ref>` applies it and records the image digest in the SSM
parameter. **The parameter, not a tag, selects the running image**: a plan or apply without `--image` reads
it back and deploys nothing new, so it never rolls ECS to a different image by accident. A read-only plan
that takes no lock:

```bash
AGP_BREAK_GLASS=1 scripts/ci/plan.sh dev --lock=false
```

A first apply takes 15-20 minutes or more; Aurora, ElastiCache, ClickHouse and CloudFront in the Langfuse
module dominate.

### One apply is enough — including from zero

**There is no second apply.** This deploy used to need `terraform apply` twice on a fresh account: apply #1
brought up the control plane and created both registries but rendered their ids as `""` into everything that
consumed them, leaving the registry-backed pages **inert**, and apply #2 substituted the real ids.

That is fixed, by removing the registry id from Terraform's concern rather than by plumbing it more cleverly.
AWS mints the registryId (`RegistryIdentifier` accepts an ARN or a generated 12-16 char id, **never** a name),
there is no Terraform resource *or* data source for the `agent-registry` namespace the Registry APIs moved to
on 2026-08-06 (the provider's `aws_bedrockagentcore_registry` targets the retired `bedrock-agentcore`
namespace, ships deprecated, and is unreachable from accounts created after the split), and a `local-exec`
provisioner **has no channel to return a value**. So the id had to be written to a file and read back — and
that read resolved during the **plan walk**, before any provisioner ran.

The registry **name**, by contrast, is a static tfvar known at plan time. So the stack passes only names, and
the backend resolves name → id at first use with a single `ListRegistries` call, memoised
(`backend/src/core/registry_resolver.py`). CodeBuild — whose buildspec genuinely needs an id, since
`get-registry-record` / `update-registry-record` take a `RegistryIdentifier` — receives it as a **per-build
environment override** from the backend, which is the build's only trigger. Nothing is left that needs a value
Terraform cannot know yet, so there is no capture file, no `""` sentinel, and no second pass.

Confirm the registries exist by asking AWS (Terraform no longer outputs the ids; it outputs the names):

```bash
aws agent-registry-control list-registries \
  --query 'registries[].{name:name,id:registryId,status:status}' --output table
```

Both `agp-agents` and `agp-mcp-servers` should be listed as `READY`. If a registry is missing, the backend
fails **loudly** on the first request that needs it, with a message naming the registry and how to create it —
it does not quietly render an empty catalog.

### After the apply

`deploy-full.sh` builds and deploys the backend image and the frontend. Two things are left:

1. **Registries — nothing to do.** Terraform creates both registries itself, and no id has to be wired
   anywhere: the backend resolves each registry by NAME at first use. There is no registry-id tfvar to
   paste one into, and adopting a registry this stack did not create is not supported. The
   script below is the **same script Terraform runs**, exposed as a fallback / diagnostic for when you
   want to inspect or bootstrap a registry out of band:

   ```bash
   cd ../backend
   PYTHONPATH=src venv/bin/python scripts/ensure_registry.py --name agp-agents        # prints registryId
   PYTHONPATH=src venv/bin/python scripts/ensure_registry.py --name agp-mcp-servers   # prints registryId
   ```

   One script covers both registries — they differ only by `--name`. It is find-or-create and safe
   to re-run, waits for the registry to reach `READY`, and defaults to `--region us-east-1`. The name
   must match `agent_registry_name` / `mcp_registry_name` (defaults `agp-agents` / `agp-mcp-servers`) —
   and, in turn, the backend's `AGENT_REGISTRY_NAME` / `MCP_REGISTRY_NAME`, since the name is the only
   identifier the two sides share. Add `--json` for a single machine-readable line on stdout (all logging
   goes to stderr); Terraform no longer parses it — nothing reads an id out of this script any more — but
   it is still there for scripting.
2. **Seed the default tenant** — the one manual post-apply **data** step, and the only one. The *script* is
   optional (an admin can create a tenant through the API or the UI instead), but **having at least one tenant
   is not**: with no tenant row, `POST /agents` and `POST /projects` answer `400 unknown tenant` and every
   runtime build refuses with `unknown tenant or stage`. Run it when you want a pre-wired `default` tenant
   without going through the UI. The command is documented verbatim (with the
   `terraform output` substitutions it needs) in
   `modules/default_tenant/main.tf`. It is deliberately **not** run by
   `terraform apply`: it writes registry/DynamoDB **data**, and an infra apply must never depend on a data
   state. It is idempotent (only-creates-when-absent), so it is safe to re-run. A clean deployment from
   zero has nothing to migrate — there is no data-migration step anywhere in this runbook.

## Using Existing VPC

To use an existing VPC (recommended to avoid long VPC creation time):

1. Set these variables in `env/<ENV_NAME>.tfvars`:

```hcl
vpc_id             = "vpc-xxxxx"
public_subnet_ids  = ["subnet-xxxxx", "subnet-yyyyy"]
private_subnet_ids = ["subnet-aaaaa", "subnet-bbbbb"]
```

2. Leave them empty to create a new VPC:

```hcl
vpc_id             = ""
public_subnet_ids  = []
private_subnet_ids = []
```

## Agent runtime builds (Epic 21/22)

App-factory-generated agents are built and provisioned by the `codebuild` module, not by a standalone deployment
pipeline. The backend kicks off a build via `StartBuild`; CodeBuild fetches the `agentcore_runtime` module — which
the **root stack** zips and uploads to S3 on `terraform apply` (`runtime_module.tf`: `archive_file` +
`aws_s3_object.runtime_module`, `etag = output_md5`) — and provisions the agent's AgentCore runtime. There is no
`agentcore_runtime_trigger` module; see `modules/agentcore_runtime/README.md`.
That indirection makes the ordering load-bearing: after editing anything under
`modules/agentcore_runtime/`, run a root `terraform apply` **before** the next agent push, or the
pipeline will silently build with the previously uploaded copy of the module — no error, just stale
behaviour. The `etag = output_md5` is what re-uploads the zip on apply when (and only when) the
module content changes.

## The backend image and the frontend

`scripts/ci/build-image.sh` builds the `linux/amd64` image from `backend/Dockerfile` with the repository
root as context, tags it `<short-sha>` (never `latest`), pushes it to the stack's ECR repository and prints
the digest reference on its last line. That digest travels to `plan.sh` and `apply.sh` as `--image`.

`scripts/ci/deploy-frontend.sh` bakes the frontend's `VITE_*` values at build time from Terraform outputs
only — it writes `frontend/.env.production` from them and reads no hand-written file — then syncs `dist/`
to the frontend bucket and invalidates CloudFront. The build SHA travels with it (`data-build-sha` in
`index.html`), which is what `scripts/ci/smoke.sh` compares.

## Module Structure

```
infrastructure/
├── main.tf                       # Main orchestration
├── variables.tf                  # Input variables; every one assigned in env/<ENV_NAME>.tfvars except the
│                                 #   two TF_VAR_* secrets, environment and container_image
├── outputs.tf                    # Output values
├── providers.tf                  # Provider configuration
├── runtime_module.tf             # Zips + uploads modules/agentcore_runtime to S3 for CodeBuild
├── DEPLOYING.md                  # How to deploy and tear down
├── env/
│   └── dev.tfvars.example        # Copy to env/<ENV_NAME>.tfvars; your copy is git-ignored
├── backend.<ENV_NAME>.config     # Written by deploy-full.sh on a first deploy; git-ignored
├── .terraform-version            # Terraform 1.15.7
├── .terraform.lock.hcl           # Committed (linux_amd64 + darwin_arm64); module-level lock files stay ignored
├── scripts/
│   ├── deploy-full.sh            # The whole deploy from your machine
│   ├── destroy-full.sh           # The teardown
│   ├── ci/                       # The steps deploy-full.sh calls: <name>.sh <ENV_NAME>
│   │   ├── common.sh             #   shared environment resolution (variables file + backend config)
│   │   ├── build-image.sh        #   docker/finch build + push <short-sha>, emit the digest
│   │   ├── plan.sh / plan-summary.sh / apply.sh
│   │   ├── deploy-frontend.sh    #   build from Terraform outputs, sync S3, invalidate CloudFront
│   │   └── wait-stable.sh / smoke.sh
│   ├── container-engine.sh       # Resolves docker-or-finch; sourced by the deploy scripts and
│   │                             #   by modules/langfuse's image mirror
│   ├── setup-dockerhub-auth.sh   # Store Docker Hub creds in Secrets Manager (pull rate limits)
│   ├── import-existing.sh        # Import pre-existing AWS resources into Terraform state
│   └── README.md                 # Per-script reference
└── modules/
    ├── networking/                    # VPC, subnets, security groups
    ├── dynamodb/                      # DynamoDB tables (governance + per-domain)
    ├── s3/                            # S3 buckets (project archives + frontend)
    ├── ecr/                           # ECR repository (backend image)
    ├── ecs/                           # ECS cluster, service, tasks (backend API)
    ├── api_gateway/                   # API Gateway with VPC Link
    ├── codebuild/                     # Runtime-build execution
    ├── agent_registry/                # The two AWS Agent Registries, via ensure_registry.py
    ├── agent_ecr/                     # ECR registry for generated agent images
    ├── agentcore_runtime/             # AgentCore runtime module (NOT a root module — zipped
    │                                  #   + uploaded to S3 by runtime_module.tf, applied by CodeBuild)
    ├── default_tenant/                # agp-deployment-* deploy-role for the default tenant
    ├── secrets_manager/               # Entra backend-client secret storage
    ├── langfuse/                      # Base Langfuse v4 observability stack
    ├── state_backend/                 # CodeBuild agent-runtime state + runtime-module zip home
    │                                  #   (NOT this stack's state — see Remote State Management)
    ├── cloudfront/                    # CloudFront distribution (control plane frontend)
    └── observability/                 # CloudWatch dashboards and alarms
```

## Important Outputs

After deployment, Terraform outputs key information (initialise against the remote state first — see
[Initialising by hand](#initialising-by-hand)):

```bash
# Get all outputs
terraform output

# Get specific outputs
terraform output api_endpoint
terraform output frontend_url
terraform output ecr_repository_url
```

## Cleanup

`scripts/destroy-full.sh <ENV_NAME>` is the teardown; [`DEPLOYING.md`](DEPLOYING.md#teardown) says what it
needs and what it leaves. The `ephemeral` switch in the variables file decides whether a stack can be
destroyed at all: with `false`, buckets are not force-destroyable, tables are deletion-protected and the
secret sits on a 30-day recovery window, so a `terraform destroy` stalls on them by design. The state bucket
is not part of the stack and survives.

## Cost Considerations

The control plane's own services, all usage-priced:

- **ECS Fargate**: Pay per vCPU and memory per hour
- **API Gateway**: Pay per request
- **CloudFront**: Pay per data transfer
- **DynamoDB**: On-demand billing
- **S3**: Pay per GB stored and data transfer
- **CodeBuild**: Pay per build minute
- **CloudWatch**: Logs and metrics storage

**Estimated monthly cost for the seven services above, dev environment, minimal traffic: $50-100.**

That figure covers only that list. The same `terraform apply` also stands up the self-hosted Langfuse
stack and the networking underneath it, and those are the ones that dominate the bill because they are
mostly *always-on* rather than per-request:

- **Aurora PostgreSQL** — one cluster plus its instance (`modules/langfuse/postgresql.tf`)
- **ElastiCache** — one Valkey/Redis replication group (`modules/langfuse/redis.tf`)
- **Three more Fargate services** — Langfuse web, Langfuse worker, and ClickHouse (`modules/langfuse/ecs.tf`)
- **EFS** — the ClickHouse data volume (`modules/langfuse/ecs.tf`)
- **A second ALB** — one for the control plane, one for Langfuse (`modules/ecs/main.tf`, `modules/langfuse/alb.tf`)
- **NAT gateways** — one per availability zone (two by default) whenever this stack creates the VPC rather
  than adopting an existing one (`modules/networking/main.tf`)
- **A second CloudFront distribution + two Lambda@Edge functions** in front of Langfuse

No dollar figure is quoted for those: price them for your own region and traffic with the AWS Pricing
Calculator before treating any total as a budget. `create_langfuse = false` in the variables file leaves the whole Langfuse stack out, at the price of
an empty Observability tab.

## Troubleshooting

### ECS Tasks Not Starting

Check CloudWatch logs:

```bash
aws logs tail /ecs/agp-control-plane-dev --follow
```

### API Gateway 502 Errors

Check:
1. ECS service is running
2. Target group health checks are passing
3. VPC Link is active

### CloudFront Not Serving Updated Content

Invalidate cache:

```bash
aws cloudfront create-invalidation --distribution-id <ID> --paths "/*"
```

## Remote State Management

This stack's state is remote from the first apply; there is no local-state phase and nothing to migrate.

- **The bucket** is created by `scripts/deploy-full.sh` on a first deploy, versioned, encrypted and with
  public access blocked. It is **not** a resource of this stack — a destroy leaves it in place — and it is
  not the `state_backend` module: that module is the CodeBuild agent-runtime state and the home of the
  staged runtime-module zip, and is unrelated to how this root is stored.
- **The backend configuration** is `backend.<ENV_NAME>.config`, carrying the five keys `bucket`, `key`
  (`env/<ENV_NAME>/terraform.tfstate`), `region`, `use_lockfile = "true"` (S3-native locking; no DynamoDB
  table) and `encrypt = "true"`. `deploy-full.sh` writes it when it is absent; write it yourself first to
  choose the bucket. `main.tf` declares an empty `backend "s3" {}`; every script initialises with
  `terraform init -backend-config=backend.<ENV_NAME>.config -input=false -reconfigure`.
- **One environment, one pair of files**: a new environment is a new `env/<ENV_NAME>.tfvars` and its own
  `backend.<ENV_NAME>.config`.

## Security Notes

- All S3 buckets have public access blocked
- The control-plane ECS service runs in the **public** subnets with `assign_public_ip = true`
  (`modules/ecs/main.tf:846-850`) — it needs egress to Entra, Microsoft Graph and the AWS APIs, and this
  stack routes that straight out rather than through a NAT gateway. It is not internet-*reachable*: the only
  inbound path is the internal ALB, whose security group accepts traffic from the API Gateway VPC Link.
  The three Langfuse services are the ones in private subnets (`modules/langfuse/ecs.tf:587,638,662`)
- API Gateway uses VPC Link for private integration
- DynamoDB tables use encryption at rest
- CloudWatch log retention differs by group and is deliberate, not uniform: **365 days** for the ECS service
  and API Gateway access logs (`modules/ecs/main.tf:26`, `modules/api_gateway/main.tf:104`), **14 days** for
  CodeBuild (`modules/codebuild/main.tf:7`), **7 days** for the four Langfuse groups
  (`modules/langfuse/alb.tf:197-224`)
- Authentication is handled by Microsoft Entra ID (JWT validation in the backend)

## Support

For issues or questions:
- Check CloudWatch dashboards for metrics
- Review CloudWatch logs for errors
