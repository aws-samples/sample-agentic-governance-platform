# Control Plane Infrastructure Scripts

Shell and Python scripts for deploying, tearing down, and seeding the Control Plane infrastructure. Every script in this directory is safe to run from any working directory — they `cd` to the infrastructure root before invoking Terraform.

## Scripts at a Glance

| Script | Purpose | When to run |
|--------|---------|-------------|
| [`deploy-full.sh`](#deploy-fullsh) | End-to-end deployment — infra + backend image + frontend | First-time deployment, or after pulling new code |
| [`destroy.sh`](#destroysh) | Tear down everything this Terraform state owns | End of project, or resetting a dev environment |
| [`import-existing.sh`](#import-existingsh) | Import pre-existing AWS resources into Terraform state | Recovering from partial failures or adopting manually-created resources |
| [`setup-dockerhub-auth.sh`](#setup-dockerhub-authsh) | Store Docker Hub credentials in Secrets Manager to avoid pull rate limits | Before the first `terraform apply` (optional) |
| [`databricks-onboard.sh`](../../../../docs/databricks-onboarding.md) | Instrument a Databricks account for AGP — create the account service principal (account admin) + secret, assign it to the workspace, print the tenant-form values | Before connecting a Databricks tenant in AGP |

## Prerequisites

- AWS credentials configured (via `~/.aws/credentials`, env vars, or IAM role)
- Terraform ≥ 1.15 (the floor declared by the root stack's `required_version`)
- Docker with buildx (for `deploy-full.sh`)
- Node.js ≥ 22 (for `deploy-full.sh`)
- `jq`

---

## `deploy-full.sh`

One-command deployment of the entire Control Plane. Runs six phases in sequence and never prompts except for the final apply confirmation.

**What it does**

1. **Preflight** — verifies AWS credentials, Docker, Terraform, Node.js are all available
2. **Infrastructure** — `terraform init / plan / apply` of every module in this directory
3. **Backend image** — builds the linux/amd64 Docker image from `platform/control_plane/backend/Dockerfile` and pushes to ECR
4. **ECS rolling deploy** — `force-new-deployment` on the service so tasks pick up the new image
5. **Frontend build** — generates `.env.production`, runs `npm install` + `vite build`, syncs to the frontend S3 bucket, invalidates CloudFront

**Stale-state safeguard.** Before running Terraform, the script inspects `terraform.tfstate` for resource ARNs that belong to a different AWS account than the one currently authenticated. If it finds a mismatch, it offers to back up and reset the state — handy when switching accounts.

**Usage**
```bash
cd platform/control_plane/infrastructure/scripts
./deploy-full.sh
```

Expected duration: ~10–15 minutes on a fresh account.

---

## `destroy.sh`

Tears down every resource this Terraform state owns. Double-confirms before proceeding and empties S3 buckets first so the destroy doesn't stall on non-empty bucket errors.

**What it does**

1. Prints the list of resources about to be destroyed
2. First confirmation: type `yes`
3. Second confirmation: type `destroy`
4. Empties `project_archives` and `frontend` S3 buckets via `aws s3 rm --recursive`
5. Runs `terraform destroy`

**Usage**
```bash
cd platform/control_plane/infrastructure/scripts
./destroy.sh
```


---

## `import-existing.sh`

Imports pre-existing AWS resources into Terraform state so a subsequent `apply` doesn't try to recreate them. Useful after a partially-failed deploy, or when adopting resources that were created manually.

**What it imports** (all guarded so they skip missing resources):

- CloudWatch log groups (API Gateway, CodeBuild, ECS)
- IAM roles (CodeBuild, ECS task execution/task)
- DynamoDB tables (app factory, application catalog, deployment metadata, TF lock)
- ECR repository
- S3 buckets (project archives, frontend, TF state)
- CloudFront OAC
- CloudWatch Logs Insights query definitions
- ECS Cluster
- VPC (only if using an existing VPC)

An EventBridge event bus and an SQS DLQ used to be listed here. Neither is imported by the script and
neither resource exists anywhere in the Terraform tree — they were left over from the retired
Step-Functions/EventBridge deployments pipeline.

**Usage**
```bash
cd platform/control_plane/infrastructure/scripts
./import-existing.sh
```

After import, run `terraform plan` to verify the imported state matches the current config, then `terraform apply` to reconcile any drift.

---

## How these scripts fit into the bigger picture


See:
- [Infrastructure README](../README.md) — module layout, outputs, per-module docs
- [Platform README](../../../README.md) — control-plane components, deployment pipeline, deploy options
- [`modules/codebuild/buildspec.yml`](../modules/codebuild/buildspec.yml) — the CodeBuild phases, env vars, and dual-source flow, as executed

## `setup-dockerhub-auth.sh`

Stores Docker Hub credentials in AWS Secrets Manager so the Langfuse image mirror (`modules/langfuse/mirror-image.sh`, run on the apply host) can authenticate before pulling images. This avoids Docker Hub's unauthenticated rate limit (100 pulls/6hrs) which frequently causes deployment failures.

**This is optional** — deployments work without it but may fail with `toomanyrequests` errors. With a free Docker Hub account, the rate limit doubles to 200 pulls/6hrs.

**Setup steps**

1. Create a free Docker Hub account at https://hub.docker.com/signup (or use an existing one)
2. Create a read-only access token:
   - Go to https://hub.docker.com/settings/security
   - Click **New Access Token**
   - Name: `codebuild-pull` (or any name)
   - Permissions: **Read-only**
   - Click **Generate** and copy the token
3. Run the script:

```bash
cd platform/control_plane/infrastructure/scripts
./setup-dockerhub-auth.sh
```

Or non-interactively:

```bash
./setup-dockerhub-auth.sh --username myuser --token dckr_pat_xxx --region us-east-1
```

**What it does**

1. Verifies the credentials work against Docker Hub
2. Creates (or updates) a Secrets Manager secret named `dockerhub-credentials`
3. `modules/langfuse/mirror-image.sh` checks for this secret before pulling images — if found, it authenticates; if not, it pulls unauthenticated

**When to run** — Once per AWS account, before the first `terraform apply`. The secret persists across deployments.

---

## Troubleshooting

**"AWS credentials not configured"** — Run `aws sts get-caller-identity` first. If it fails, set `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` (+ `AWS_SESSION_TOKEN` if using SSO), or `aws configure`.

**"State references a different account"** — `deploy-full.sh` offers to back up and reset when it sees mismatched ARNs. Accept, or manually `mv terraform.tfstate terraform.tfstate.backup.<old-account>` and rerun.

**Langfuse 503 / "toomanyrequests" from Docker Hub** — The Langfuse module mirrors its images from Docker Hub, which rate-limits unauthenticated pulls. Run `./setup-dockerhub-auth.sh` to store Docker Hub credentials, then redeploy. Alternatively, push images manually: `docker tag langfuse/langfuse:3.161.0 <ECR_URL>:3.161.0 && docker push <ECR_URL>:3.161.0` (repeat for langfuse-worker and clickhouse).

**`terraform apply` fails inside the Langfuse image mirror** — The mirror runs on the apply host, so a container engine must be reachable. Start Docker Desktop, or force finch with `CONTAINER_ENGINE=finch terraform apply` / `deploy-full.sh --finch`.

**Agent runtime build doesn't run after a push** — Builds are started by the backend via CodeBuild `StartBuild`, not by an EventBridge/CodeCommit rule (the Step-Functions deployments orchestrator was removed in Epic 26). Check the CodeBuild project's build history and its CloudWatch log group. If a runtime-module edit does not take effect, run a root `terraform apply` first — the pipeline consumes the S3-staged zip that apply uploads.
