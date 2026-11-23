# Bump these on a schedule — a stale pin is an exposed pin. See README.md §"Image
# currency" for the check-and-bump routine and why "newest" is not the selection rule.
# Base-OS packages are not the pin's job: the mirror applies them at build time
# (patch-base.Dockerfile, patch-base.rpm.Dockerfile). The application's own dependencies
# arrive only with a newer pin.
variable "langfuse_version" {
  description = "Langfuse image version to pull and push (web + worker share it)"
  type        = string
  default     = "4.83.0"
}

variable "clickhouse_version" {
  # ClickHouse ships security updates only for the releases its SECURITY.md lists, and Langfuse v4
  # needs 25.12 or newer. Pin an LTS line that file lists; a store cannot go back a release.
  description = "ClickHouse image version to pull and push (an LTS line upstream still supports)"
  type        = string
  default     = "26.3.66.4"
}

locals {
  ecr_base = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${data.aws_region.current.region}.amazonaws.com"

  ecr_repos = {
    langfuse        = { source = "langfuse/langfuse", tag = var.langfuse_version }
    langfuse-worker = { source = "langfuse/langfuse-worker", tag = var.langfuse_version }
    clickhouse      = { source = "clickhouse/clickhouse-server", tag = var.clickhouse_version }
  }
}

resource "aws_ecr_repository" "images" {
  for_each = local.ecr_repos

  name                 = "${var.name}-${each.key}"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Name = "${local.tag_name} ${each.key}"
  }
}

# Authenticate the apply host's container engine to ECR EXACTLY ONCE, before any mirror runs.
# This resource exists purely to serialize that one step: `for_each` gives push_images no
# ordering, so all three mirrors used to run their own `docker login` concurrently — and on
# macOS that means concurrent writes to the same login-keychain entry, whose losers die with
# "The specified item already exists in the keychain. (-25299)". That failed a real from-zero
# apply (2 of 3 mirrors succeeded, the apply exited 1). One shared login upstream of the fan-out
# removes the race instead of retrying into it; the credential lives in the engine's credential
# store, so all three pushes then reuse it.
resource "null_resource" "ecr_login" {
  triggers = {
    # Re-login when the registry changes; also re-run on every apply that has anything to
    # mirror, because an ECR authorization token is only valid for 12h.
    registry = local.ecr_base
    tags     = join(",", [for k, v in local.ecr_repos : "${k}=${v.tag}"])
  }

  provisioner "local-exec" {
    command = "${path.module}/ecr-login.sh ${local.ecr_base} ${data.aws_region.current.region}"
  }

  depends_on = [aws_ecr_repository.images]
}

resource "null_resource" "push_images" {
  for_each = local.ecr_repos

  triggers = {
    repo_url = aws_ecr_repository.images[each.key].repository_url
    tag      = each.value.tag
  }

  # Mirroring runs on the APPLY HOST, so it must work with whatever container engine the
  # operator has. The logic lives in mirror-image.sh (not inline here) so it can source the
  # shared scripts/container-engine.sh resolver — docker OR finch, same as the deploy scripts.
  # Set CONTAINER_ENGINE=finch (or run via `deploy-full.sh --finch`) to force finch.
  provisioner "local-exec" {
    command = "${path.module}/mirror-image.sh ${each.value.source} ${each.value.tag} ${aws_ecr_repository.images[each.key].repository_url} ${local.ecr_base} ${data.aws_region.current.region} ${var.name}-${each.key}"
  }

  # ecr_login is what makes the three parallel mirrors safe: one login completes before any of
  # them starts, so none of them writes to the credential store while a sibling is writing.
  depends_on = [aws_ecr_repository.images, null_resource.ecr_login]
}
