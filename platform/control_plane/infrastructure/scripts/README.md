# Control Plane Infrastructure Scripts

The scripts that deploy and tear down the control plane. [`../DEPLOYING.md`](../DEPLOYING.md) is the
walkthrough; this page is the reference.

| Script | What it does |
|---|---|
| `deploy-full.sh [ENV_NAME]` | The whole deploy from your machine: state bucket, image, Terraform, frontend, steady-state wait, smoke. `dev` when no name is given. |
| `destroy-full.sh <ENV_NAME>` | Destroys one environment after you type its name, with the parameter, secrets and roles the platform created outside Terraform. |
| `ci/build-image.sh <ENV_NAME>` | Builds the backend image, pushes it tagged with the short commit SHA and prints its digest reference on the last line. |
| `ci/plan.sh <ENV_NAME> [--image <ref>] [--lock=false]` | `terraform plan` into `plan.out`, with a create/update/delete summary. Without `--image` it plans against the image already in the SSM parameter. |
| `ci/apply.sh <ENV_NAME> [--image <ref>]` | Applies `plan.out`; with `--image`, records the digest in `/agp/<ENV_NAME>/backend-image`. Prints `FRONTEND_URL` and `API_ENDPOINT`. |
| `ci/deploy-frontend.sh <ENV_NAME> [--write-env-only]` | Writes `frontend/.env.production` from Terraform outputs, builds the frontend, syncs it to S3 and invalidates CloudFront. |
| `ci/wait-stable.sh <ENV_NAME>` | Waits up to 15 minutes for the backend's ECS rollout to complete. |
| `ci/smoke.sh <ENV_NAME> [--expect-sha <sha>] [--image <ref>] [--dry-run]` | Unauthenticated checks against the deployed API and frontend. |
| `setup-dockerhub-auth.sh` | Stores Docker Hub credentials in Secrets Manager so the Langfuse image mirror is not subject to anonymous pull rate limits. Optional, before the first apply. |
| `import-existing.sh` | Imports pre-existing AWS resources into Terraform state, to recover from a partial failure. |
| `databricks-onboard.sh` | Onboards a Databricks workspace; see `docs/databricks-onboarding.md` at the repository root. |
| `container-engine.sh` | Sourced by the others: resolves docker or finch. `CONTAINER_ENGINE=finch` forces finch. |

## What every script reads

- **`ENV_NAME`**, the first argument: it selects `env/<ENV_NAME>.tfvars` and `backend.<ENV_NAME>.config`
  in the infrastructure directory, and is passed to Terraform as `environment`.
- **The AWS credentials in play.** The account is never named in a file; the region is `aws_region` in the
  variables file.
- **`TF_VAR_entra_backend_client_secret` and `TF_VAR_langfuse_admin_password`**, exported in the shell.
  Terraform runs with `-input=false`, so a missing one fails the plan instead of prompting.
- **`AGP_BREAK_GLASS=1`** for `build-image.sh`, `plan.sh`, `apply.sh` and `deploy-frontend.sh` when you run
  them one by one; they exit 2 without it. `deploy-full.sh` sets it.

## Troubleshooting

- **`missing env/<ENV_NAME>.tfvars`** — copy `env/dev.tfvars.example` to that name and fill it in.
- **`still carries a <placeholder>`** — a value in the variables file was not replaced.
- **The apply fails while mirroring Langfuse images** — the container engine is not running, or Docker Hub
  rate-limited the anonymous pull; run `setup-dockerhub-auth.sh` and deploy again.
- **`Invalid choice: 'agent-registry-control'`** — the AWS CLI is too old; update it.
- **Sign-in fails after a deploy** — the SPA app registration does not carry `<FRONTEND_URL>/auth/callback`
  as a redirect URI.
