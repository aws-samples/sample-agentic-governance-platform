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
  excludes = [".omc", ".git", ".terraform", "__pycache__"]
}

resource "aws_s3_object" "runtime_module" {
  bucket = module.state_backend.bucket_name
  key    = "runtime-module/agentcore_runtime.zip"
  source = data.archive_file.runtime_module.output_path
  etag   = data.archive_file.runtime_module.output_md5
  tags   = var.tags
}
