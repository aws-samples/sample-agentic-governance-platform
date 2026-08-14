# Bump these on a schedule — a stale pin is an exposed pin. See README.md §"Image
# currency" for the check-and-bump routine and why "newest" is not the selection rule.
variable "langfuse_version" {
  description = "Langfuse image version to pull and push (web + worker share it)"
  type        = string
  default     = "3.225.0"
}

variable "clickhouse_version" {
  # STAY ON 25.8.x. It is the LTS line ClickHouse still ships security updates for
  # (SECURITY.md: 25.8 supported; 25.9-25.12, 26.1, 26.2 and 26.4 are already EOL).
  # Newest is NOT most-patched here: 26.7.2.59 carries an OLDER libssl3 than 25.8.28.1,
  # because these images rebuild on Ubuntu 22.04 whenever upstream happens to. Moving to
  # 26.x is a planned major upgrade of an EFS-backed store, not a CVE fix.
  description = "ClickHouse image version to pull and push (stay on the 25.8 LTS line)"
  type        = string
  default     = "25.8.28.1"
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
