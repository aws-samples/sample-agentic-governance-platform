# Deploying

One script deploys the whole platform into your own AWS account from your own machine, and one script
tears it down. What a deploy needs first — the AWS account, the Entra app registrations, the tooling — is
in the root README's Prerequisites and
Bootstrapping sections; the Terraform-level reference is [`README.md`](README.md)
and the scripts' reference is [`scripts/README.md`](scripts/README.md).

## The environment's files

An environment is a name (`dev` unless you say otherwise) and two files in this directory, both git-ignored so your tenant's values stay out of a fork:

- **`env/<ENV_NAME>.tfvars`** — every non-secret variable. Copy the example and replace each
  `<placeholder>`:

  ```bash
  cd platform/control_plane/infrastructure
  cp env/dev.tfvars.example env/dev.tfvars
  ```

- **`backend.<ENV_NAME>.config`** — where Terraform keeps this stack's state. The deploy script writes it
  on the first run, naming a state bucket after the environment and your account, and creates that bucket
  (versioned, encrypted, public access blocked). Write the file yourself first if you want another bucket.

The two secrets are never in a file. Export them in the shell that runs the deploy:

```bash
export TF_VAR_entra_backend_client_secret='<the backend app registration's client secret value>'
export TF_VAR_langfuse_admin_password='<a password for the seed Langfuse admin>'
```

## The deploy

```bash
cd platform/control_plane/infrastructure/scripts
./deploy-full.sh            # the dev environment
./deploy-full.sh prod       # another environment
```

It takes no flags and does not pause for confirmation. With Langfuse on (`create_langfuse = true`, the
example's value) the name must be `dev` or `prod`; without it, any short name of lowercase letters, digits
and dashes. Plan on one environment per account: the agent registries, the GitHub OIDC provider and
Transaction Search are account-wide, and a second environment needs its own registry names. In order, it:

1. checks the credentials, the tools, the backend virtualenv, the two secrets and the variables file;
2. creates the state bucket when it does not exist;
3. on a first deploy, creates the backend's ECR repository;
4. builds the backend image and pushes it tagged with the short commit SHA (never `latest`);
5. plans and applies Terraform with that image by digest, and records the digest in the SSM parameter
   `/agp/<ENV_NAME>/backend-image`;
6. builds the frontend from Terraform outputs and syncs it to S3 and CloudFront;
7. waits for the ECS rollout to reach steady state and runs an unauthenticated smoke check.

A first deploy takes 30 to 40 minutes; Aurora, ElastiCache, ClickHouse and CloudFront dominate. Run it from
a git clone, not a downloaded archive: the build is stamped with the commit SHA. `CONTAINER_ENGINE=finch`
selects finch over docker.

Its last lines print `FRONTEND_URL` and `API_ENDPOINT`. Two things follow a first deploy, and the root
README's steps 4 and 5 explain both: set `agp_api_url = "<API_ENDPOINT>/api/v1"` in your variables file and
run the script again, and add `<FRONTEND_URL>/auth/callback` as a redirect URI on the SPA app registration.

Every later deploy is the same command.

The steps are also separate scripts under `scripts/ci/` (`build-image.sh`, `plan.sh`, `apply.sh`,
`deploy-frontend.sh`, `wait-stable.sh`, `smoke.sh`), each taking the environment name first. Run by hand
the first four need `AGP_BREAK_GLASS=1` in the environment, which `deploy-full.sh` sets for you. They are what a
pipeline of your own would call, one job per script.

## Teardown

```bash
cd platform/control_plane/infrastructure/scripts
./destroy-full.sh dev
```

Delete your agents and MCP servers in the UI first: each one's runtime has its own Terraform state in a
bucket of this stack, and destroying the stack deletes the only record that can tear them down.

The script asks you to type the environment name, then destroys the stack and removes what the platform
created outside Terraform: the image parameter, the stack's secrets and the ECR push roles. It needs no
secret exported. Left for you to delete by hand: the state bucket, the two agent registries, the GitHub
OIDC provider if you connected GitHub, and the edge functions' log groups.

The stack must have been applied with `ephemeral = true` in its variables file (the example's value);
otherwise DynamoDB deletion protection and non-empty buckets stop the destroy, and the secrets' recovery
window blocks a re-create under the same name.
On a stack deployed with `false`, set it to `true`, run `deploy-full.sh` once, then destroy.

CloudFront keeps replicas of the two Langfuse edge functions for a few hours after the distribution is
disabled, and the destroy fails on them until it lets go. Waiting is the only fix; run the script again.
