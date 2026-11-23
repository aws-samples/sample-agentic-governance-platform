# ============================================================================
# Runtime-infra module archive (E22 bugfix-A)
# ============================================================================
# The backend template-rollout pushes the on-disk `agentcore_runtime` Terraform
# module into each connected org as the FORCED `agp-runtime-infra` repo. The
# backend Docker image does NOT ship the infrastructure/ tree, so the module is
# staged here in S3 instead: zip the module dir and upload to a FIXED key on the
# versioned, non-expiring state bucket. etag = archive md5 → a module .tf edit
# re-uploads on the next `terraform apply` (no backend rebuild). The ECS backend
# DOWNLOADS this zip at rollout time (RUNTIME_MODULE_BUCKET / RUNTIME_MODULE_KEY).

data "archive_file" "runtime_module" {
  type        = "zip"
  source_dir  = "${path.module}/modules/agentcore_runtime"
  output_path = "${path.module}/.build/agentcore_runtime.zip"

  # Never ship local dev cruft into the per-org agp-runtime-infra repo. The old
  # on-disk rollout path filtered these via collect_scaffold_files; the S3 path
  # must match. .omc = OMC session state; .git/.terraform/__pycache__ = build cruft.
  #
  # `.terraform.lock.hcl` is a SEPARATE ENTRY and must stay one (E30/T7 review):
  # it is a SIBLING FILE of the `.terraform/` directory, not a file inside it, so
  # excluding the directory does NOT exclude the lock. Anyone who runs
  # `terraform init` in a module dir to validate it leaves one behind, and it is
  # git-ignored — so `git status` stays clean and nothing warns.
  #
  # Why that breaks a deploy rather than merely being untidy: a lock written on a
  # dev machine records provider hashes for THAT PLATFORM ONLY (a single
  # `h1:` for darwin_arm64, no `zh:` entries). Staged into the zip, the rollout
  # pushes it into the org infra repo, and CodeBuild's `terraform init` on
  # linux_amd64 then fails the checksum comparison — a build failure that reads
  # like a provider/registry problem and points nowhere near this file.
  excludes = [".omc", ".git", ".terraform", ".terraform.lock.hcl", "__pycache__"]
}

resource "aws_s3_object" "runtime_module" {
  bucket = module.state_backend.bucket_name
  key    = "runtime-module/agentcore_runtime.zip"
  source = data.archive_file.runtime_module.output_path
  etag   = data.archive_file.runtime_module.output_md5
  tags   = var.tags
}

# ============================================================================
# Harness-infra module archive (E30/T7)
# ============================================================================
# The SECOND infra template. `agentcore_harness` is staged exactly like the
# runtime module above — same bucket, same etag-on-content mechanism — and the
# rollout pushes it into each connected org as `agp-harness-infra`. Two staged
# zips instead of one is the whole infrastructure cost of infra templates becoming
# first-class (design §2): a third infra template adds another pair here, nothing
# more.
#
# Kept in THIS file rather than a new `harness_module.tf` so both stagings stay
# side by side — the pattern is only obvious while the two blocks are readable
# together, and the runtime module's README already points here.

data "archive_file" "harness_module" {
  type        = "zip"
  source_dir  = "${path.module}/modules/agentcore_harness"
  output_path = "${path.module}/.build/agentcore_harness.zip"

  # Same exclude list as the runtime module's, for the same reason: never ship
  # local dev cruft into the per-org agp-harness-infra repo. Keep the two in sync.
  # `.terraform.lock.hcl` is listed for the reason spelled out on the runtime
  # block above — it is a SIBLING FILE of `.terraform/`, not inside it, so
  # excluding the directory does not exclude the lock.
  excludes = [".omc", ".git", ".terraform", ".terraform.lock.hcl", "__pycache__"]
}

resource "aws_s3_object" "harness_module" {
  bucket = module.state_backend.bucket_name
  key    = "harness-module/agentcore_harness.zip"
  source = data.archive_file.harness_module.output_path
  etag   = data.archive_file.harness_module.output_md5
  tags   = var.tags
}

# ============================================================================
# Gateway-infra module archive (E40/T4)
# ============================================================================
# The THIRD infra template, staged exactly like the two above. The rollout pushes it into
# each connected org as `agp-gateway-infra`; MCP-server repos built from Ops deploy with it.

data "archive_file" "gateway_module" {
  type        = "zip"
  source_dir  = "${path.module}/modules/agentcore_gateway"
  output_path = "${path.module}/.build/agentcore_gateway.zip"

  # Same exclude list as the two above — keep all three in sync (see the runtime block for
  # why `.terraform.lock.hcl` is its own entry).
  excludes = [".omc", ".git", ".terraform", ".terraform.lock.hcl", "__pycache__"]
}

resource "aws_s3_object" "gateway_module" {
  bucket = module.state_backend.bucket_name
  key    = "gateway-module/agentcore_gateway.zip"
  source = data.archive_file.gateway_module.output_path
  etag   = data.archive_file.gateway_module.output_md5
  tags   = var.tags
}
