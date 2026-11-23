# Langfuse module (base observability) — E26

Self-hosted **Langfuse v4** (Aurora PostgreSQL Serverless v2 + ElastiCache/Valkey +
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

## Image currency (the mirror patches the base OS; the pins track features)

Almost every finding these images attract is an **OS base-layer** package (openssl, glibc, perl,
krb5) — not Langfuse or ClickHouse code — and new Alpine/Ubuntu advisories land continuously.

**Version height does not fix them, because upstream never runs a full upgrade.** Measured
2026-09-02: `clickhouse-server:25.8.33.6` — the newest 25.8 LTS
build, byte-identical to Docker Official `clickhouse:25.8.33.6` — shipped `openssl`
`3.0.2-0ubuntu1.29` next to `libssl3` `3.0.2-0ubuntu1.26`. Same source package, two versions:
the build pulled `openssl` fresh as a dependency and left the base layer's `libssl3` alone.
**27** packages were upgradable straight from the Ubuntu 22.04 archive, two of them Critical
(`libssl3` USN-8678-1, `perl-base` USN-8675-1). No published ClickHouse image was clean —
`clickhouse/clickhouse-server`, Docker Official `clickhouse`, and `25.8-alpine` were all checked.
So a tag bump alone **cannot** clear a container finding.

**`mirror-image.sh` therefore builds rather than re-tags.** `patch-base.Dockerfile` layers
`apt-get upgrade` (ClickHouse, Ubuntu) onto the pulled image. The Langfuse v4 images ship on UBI
micro, which has no package manager, so `patch-base.rpm.Dockerfile` runs `dnf update` from a full
UBI stage against a copy of the image's root and layers back only the paths it changed. What
lands in ECR is upstream plus every distro security update available that day, and base-OS
remediation no longer waits on upstream's rebuild cadence.

**The mirror does not reach the application's own dependencies.** Measured 2026-11-18 with Docker
Scout on the worker image: the newest v3, `3.227.1`, still carried Critical findings in
`next@16.2.11` and `proxy-addr@2.0.7`, at the versions `3.225.11` had; `4.83.0` carried none.
Those arrive with a newer pin and nothing else.

Two constraints that mechanism must respect, both learned the hard way:

- **Third-party apt repos are parked during the upgrade.** The ClickHouse image carries
  `/etc/apt/sources.list.d/clickhouse.list`, so a bare `apt-get upgrade` moved
  `clickhouse-server` from `25.8.33.6` to **`26.8.2.7`** — off the supported LTS line and into an
  unplanned major upgrade of an EFS-backed store. The Dockerfile moves those sources aside and
  restores them after, keeping this strictly a base-OS patch.
- **A fixed pin still goes stale.** ECR holds "upstream `$TAG` as patched the day we built it",
  and the mirror skips tags already present. Bumping a pin creates a new tag and a fresh build;
  to refresh *without* a bump, force the resource and set the env var:
  ```bash
  MIRROR_FORCE_REBUILD=1 terraform apply \
    -replace='module.langfuse.null_resource.push_images["clickhouse"]'
  ```

**Routine — quarterly, ~15 minutes.** Bump both and `terraform apply`. Mirroring no-ops for tags
already in ECR, so a re-apply is cheap.

**The apply rolls the three services in order and waits for each:** ClickHouse, then web, then
worker (`wait_for_steady_state` and `depends_on` in `ecs.tf`). The web container migrates Postgres
and ClickHouse when it starts, so it needs the pinned ClickHouse release already serving; a worker
that starts before those migrations end marks the background migrations it validates against them
as failed, and a failed background migration is not retried on restart. An apply that changes a
pin therefore lasts as long as the slowest service takes to become healthy, and fails when one
does not.

**"Newest" is still not the selection rule.** Two traps, measured 2026-08-04:

- **ClickHouse: pin an LTS line upstream still supports.** `SECURITY.md` upstream lists the
  releases that receive security updates. Every 25.x release has left that list, so the pin sits
  on the 26.3 LTS line, the one Langfuse's own v4 compose file pins (v4 needs 25.12 or newer). A
  move between lines is a release upgrade of an EFS-backed store and cannot be undone by
  redeploying the older image: back the EFS volume up first, and never fold it into a base-OS
  refresh.
- **Langfuse: stay current on the v4 line.** Upstream patches v3 only until the end of January
  2027, and by November 2026 its npm dependencies were already behind. A v4 image ages too:
  `4.75.0` had 17 base packages to upgrade sixteen days after its release, and `4.54.0`, six weeks
  old, carried Critical findings that `4.83.0` does not.

**Verify against the packages, not the tag** — and verify the **patched** image, not upstream.
What clears a finding is the installed version inside what we pushed, on the arch ECS runs
(`X86_64` — pass `--platform linux/amd64`, or a native arm64 Mac silently inspects the wrong
image and reports different package versions):

```bash
finch run --rm --platform linux/amd64 --entrypoint bash <acct>.dkr.ecr.<region>.amazonaws.com/<name>-clickhouse:<ver> \
  -c 'apt-get update -qq && apt-get -s upgrade | grep ^Inst'   # expect: only clickhouse-* from packages.clickhouse.com
docker scout cves --platform linux/amd64 --only-severity critical,high \
  local://<acct>.dkr.ecr.<region>.amazonaws.com/<name>-langfuse:<ver>   # expect: no Critical, no fixable row
```

The Langfuse image has no package manager to ask, so it is scanned rather than queried; the scan
also reads the npm and Go dependencies the mirror cannot patch.

**Runtime validation cannot happen on an arm64 Mac.** `clickhouse-server` segfaults under qemu
during `entrypoint.sh` (`extract-from-config`) — upstream's own unmodified image does it too, so
it proves nothing about a patched build. Package checks above are valid (dpkg/apk metadata is
static); functional confirmation is the ECS service reaching steady state on X86_64.

Findings only clear once the **old tasks are gone** and new ones run on the new digests —
restarting before the tag bump is pointless, since the pinned tags resolve to the same digests.
Web and worker MUST stay on the same `langfuse_version`.

## Langfuse v4 runs in the `legacy` write mode

Langfuse v4 replaces the trace and observation tables with one `events` model, and by default
(`events_only`) it stops serving what v3 served: the batch ingestion endpoints, and the read
endpoints for traces, observations, sessions, scores and metrics all return `404`. `ecs.tf` sets
`local.v4_migration_env` on the web and worker containers so that v4 keeps the v3 ingestion and
read behavior, with the new tables created and not written.

The mode is kept because the platform still reads the v3 endpoints:
`services/langfuse_metrics_service.py` calls `GET /api/public/traces`, `/api/public/metrics` and
`/api/public/metrics/daily`. Upstream removes the `legacy` and `dual` modes in a later major, so
the move to `events_only` is owed. Before it, in the order of the
[v3 to v4 guide](https://langfuse.com/self-hosting/upgrade/upgrade-guides/upgrade-v3-to-v4):

1. The backend's readers move to the Observations API v2 and the Metrics API v2.
2. The agents' OpenTelemetry exporters send `x-langfuse-ingestion-version: 4`, so their spans are
   written to the new tables directly.
3. The write mode becomes `dual`; once new data shows in both models, the historic backfill is
   enabled, or the retention window is waited out.
4. The four overrides are removed.

**Moving an environment onto a new pin.** CI has no container engine, so the tags are mirrored
into that account's ECR from a laptop before the apply, with the script the apply itself calls:

```bash
./mirror-image.sh langfuse/langfuse <ver> <ecr>/<name>-langfuse <ecr> <region> <name>-langfuse
```

and the same for `langfuse/langfuse-worker` and `clickhouse/clickhouse-server`. Before a major of
either, back up the ClickHouse EFS volume and snapshot the Aurora cluster, and confirm on the
running release that every background migration has finished (the version tag in the Langfuse UI,
**Background Migrations**).

**Going back to v3** is possible while the mode is `legacy` or `dual`, and not by redeploying the
v3 image alone: the v3 web container refuses a ClickHouse schema newer than its own. The guide's
"Rolling back without a restore" rewinds the schema with the `migrate` binary in the v4 web image
(`goto 37`); the pin then returns to the newest v3.

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
