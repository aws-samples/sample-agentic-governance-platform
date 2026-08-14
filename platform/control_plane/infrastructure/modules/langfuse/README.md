# Langfuse module (base observability) — E26

Self-hosted **Langfuse v3** (Aurora PostgreSQL Serverless v2 + ElastiCache/Valkey +
ClickHouse-on-EFS on ECS Fargate), fronted by an ALB and a CloudFront
distribution with a Lambda@Edge **auto-login** and a `strip_frame_headers`
origin-response Lambda so the platform can embed the Langfuse UI in an iframe.

**The ALB is internet-facing, not internal.** `var.alb_scheme` defaults to `"internet-facing"`
(`variables.tf:134-138`) and the root passes no override, so `internal = false` and the ALB sits in
the **public** subnets (`alb.tf:81-84`). Two controls make that safe, and they are what to reason
about — not a private-subnet placement that does not exist:

1. The ALB security group admits HTTP only from the AWS-managed
   `com.amazonaws.global.cloudfront.origin-facing` prefix list (`alb.tf:48-63`), so only CloudFront
   edge locations can open a connection at all.
2. The listener forwards only when the request carries the secret `x-origin-verify` header that this
   module's CloudFront distribution injects (`alb.tf:139-152`, `cloudfront.tf:6,29-30`) — so another
   account's CloudFront distribution pointed at this ALB gets nothing.

This module was ported from `templates/foundation-stack/iac/terraform/modules/langfuse/`.
Under E26 Langfuse is a **base Terraform module** applied by the standard
`terraform apply` — it is no longer deployed by the (now retired) CodeBuild /
Step-Functions deployments pipeline.

## How it's wired into the base stack

- `module "langfuse"` in `infrastructure/main.tf` injects the VPC + subnets **by ID**
  from the base networking locals (`local.vpc_id`, `local.private_subnet_ids`,
  `local.public_subnet_ids`). The foundation-stack's `existing_vpc_id` data source and
  `tag:Name = *public*/*private*` subnet discovery are gone — IDs are passed directly.
- Outputs `langfuse_host` and `langfuse_secret_name` are surfaced at the root and fed to
  the backend ECS task as the `LANGFUSE_HOST` and `LANGFUSE_ADMIN_SECRET_NAME` env vars
  (plaintext host + secret **name**, not the secret value).
- A **seed org** (`LANGFUSE_INIT_ORG_ID = "seed-org"`) + seed project/key are created
  headlessly at container boot. The per-agent provisioner (E26/T4) creates additional
  projects against that seed org via the Lambda@Edge auto-login — keep the CloudFront
  auto-login + `strip_frame_headers` Lambdas intact.

## Operator notes (READ before `deploy-full.sh` / `terraform apply`)

1. **A container engine (Docker OR finch) is required for `terraform apply`.**
   `null_resource.push_images` (in `ecr.tf`) shells out to `mirror-image.sh`, which mirrors
   each upstream image into ECR **on the machine running `terraform apply`** (previously this
   ran inside the CodeBuild container). The operator's laptop / CI runner must have a running
   container engine **and Docker Hub egress**. `deploy-full.sh` already needs one for the
   backend image, but the base `terraform apply` step did not — it does now. The step also runs
   `aws iam create-service-linked-role` (`main.tf`), so the apply principal needs IAM
   permissions the first time per account.
   - **Engine selection** is delegated to `infrastructure/scripts/container-engine.sh`, the
     same resolver the deploy scripts use: `CONTAINER_ENGINE` env var wins, else Docker if its
     daemon answers, else finch (whose VM is started automatically). Force finch with
     `CONTAINER_ENGINE=finch terraform apply`, or run `deploy-full.sh --finch` (it exports the
     variable, which `terraform apply` inherits).
   - **finch/arm64 note:** the push MUST pass `--platform linux/amd64` (it does). Without it
     nerdctl tries to reduce the multi-arch manifest to a `-tmp-reduced-platform` image and
     fails with `content digest …: not found`, because only the amd64 blobs were pulled. The
     ECS task definitions run `X86_64`, so amd64 is the correct single platform to publish.
   - **ECR login happens ONCE per apply, in its own resource.** `null_resource.ecr_login`
     (`ecr-login.sh`) authenticates the engine to ECR, and all three `push_images` instances
     `depends_on` it. That ordering is load-bearing, not tidiness: `for_each` runs the three
     mirrors in parallel, and on macOS `docker login` writes to the login keychain — three
     concurrent writes to the same registry entry race, and the losers exit with
     `The specified item already exists in the keychain. (-25299)`, failing the whole apply.
     One login upstream of the fan-out means the mirrors never write credentials concurrently.
     `ecr-login.sh` additionally holds a host mutex and stamps its success, so even a resumed
     apply that re-runs a single mirror without `ecr_login` logs in at most once.
     **If a login ever fails with -25299 anyway** (a keychain entry left stuck by an earlier
     crashed login blocks all future writes), the message says so and names the fix:
     `security delete-internet-password -s <ecr-registry-host>`, then re-apply.
   - **Docker Hub rate limits:** 100 pulls/6h unauthenticated, 200 authenticated. To
     authenticate, create a Secrets Manager secret named `dockerhub-credentials` with
     `{"username":"…","token":"…"}` — `mirror-image.sh` picks it up automatically.
   - Pulls are skipped when the image tag already exists in ECR (idempotent re-apply).
   - **No daemon at all?** Mirror registry-to-registry instead, then re-apply (the skip check
     above makes the provisioner a no-op):
     `crane copy --platform linux/amd64 langfuse/langfuse:<ver> <ecr>/<name>-langfuse:<ver>`

2. **`langfuse_admin_email` / `langfuse_admin_password` are operator-supplied tfvars.**
   They are declared in `infrastructure/variables.tf` (`langfuse_admin_password` is
   `sensitive = true`) with **empty defaults** — the module never ships a real value.
   Populate them at apply time in a gitignored tfvars file or via `-var`; **never commit
   a value.** The password must contain letters, numbers, and at least one special
   character (it feeds `LANGFUSE_INIT_USER_PASSWORD` and the Lambda@Edge auto-login).

3. **Heavyweight + slow.** Aurora + ElastiCache + ClickHouse + CloudFront + Lambda@Edge
   materially lengthen every `apply`, and CloudFront / Lambda@Edge **deletes are slow**
   (affects `destroy`). Langfuse now shares the root `terraform.tfstate`, so a base
   `terraform destroy` tears down Langfuse and its trace data / ClickHouse EFS.

## Image currency (bump on a schedule, not on an alert)

The two pins in `ecr.tf` are the module's main security debt. Both images are long-lived and
internet-facing (ALB + CloudFront), so a stale pin is an **exposed** pin, not merely an old one.
Almost every finding these images attract is an **OS base-layer** package (openssl, gnutls,
libc6) — not Langfuse code — and new Alpine/Ubuntu advisories land continuously. Version height
cannot prevent them; only cadence keeps you inside SLA when they appear.

**Routine — quarterly, ~15 minutes.** Bump both, `terraform apply`, confirm all 3 services reach
steady state. Mirroring no-ops for tags already in ECR, so a re-apply is cheap.

**"Newest" is not the selection rule.** Two traps, both measured on 2026-08-04 while remediating
SIM V2313251216:

- **ClickHouse: stay on the 25.8 LTS line.** `SECURITY.md` upstream lists 25.8 as supported for
  security updates while 25.9–25.12, 26.1, 26.2 and 26.4 are already EOL. And newest ≠ most
  patched: `26.7.2.59` shipped an **older** `libssl3` than `25.8.28.1`, because these images
  rebuild on Ubuntu 22.04 whenever upstream happens to. Crossing into 26.x is a planned major
  upgrade of an EFS-backed store — never fold it into a CVE fix.
- **Langfuse: check the base layer before paying for a major.** `4.4.0` and `3.225.0` ship
  *identical* Alpine 3.24.1 and openssl 3.5.7-r0, so moving to v4 buys **zero** CVE benefit
  while incurring the documented [v3→v4 migration](https://langfuse.com/self-hosting/upgrade/upgrade-guides/upgrade-v3-to-v4).
  Upgrade to v4 for its features, on its own schedule — not as remediation.

**Verify against the packages, not the tag.** What actually clears a finding is the installed
version inside the image, on the arch ECS runs (`X86_64` — pass `--platform linux/amd64`, or a
native arm64 Mac silently inspects the wrong image and can report different package versions):

```bash
finch run --rm --platform linux/amd64 --entrypoint sh langfuse/langfuse:<ver> \
  -c 'apk list --installed | grep -E "^(libssl3|libcrypto3)-"'
finch run --rm --platform linux/amd64 --entrypoint bash clickhouse/clickhouse-server:<ver> \
  -c 'dpkg -l | grep -E "^ii\s+(openssl|libssl3|libgnutls30|libc6):"'
```

Findings only clear once the **old tasks are gone** and new ones run on the new digests —
restarting before the tag bump is pointless, since the pinned tags resolve to the same digests.
Web and worker MUST stay on the same `langfuse_version`.

## Provider requirements

The module uses `aws (~> 6.0)`, `random (>= 3.4, for random_bytes)`, `null`, `time`, and
`archive`. These are declared in this module's `versions.tf` and in the root
`required_providers` (`infrastructure/main.tf`). Run `terraform init -upgrade` so the
lock file resolves them.

## Vestigial Cognito SSO

The `aws_cognito_user_pool_client` + `AUTH_CUSTOM_*` env are gated on
`cognito_user_pool_id != ""`. The base stack has no Cognito (Entra ID is the sole IdP),
so `cognito_user_pool_id` defaults to `""` → SSO stays off and Langfuse uses
username/password login for the seed admin. This SSO path is dead code in the Entra world.
