#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 <ENV_NAME>" >&2
  exit 2
}

[ $# -eq 1 ] || usage
export ENV_NAME="$1"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"
require_ci_or_break_glass
rm -f "$TF_DIR/image.env"
REPO_ROOT="$(cd "$TF_DIR/../../.." && pwd)"
DOCKERFILE="$REPO_ROOT/platform/control_plane/backend/Dockerfile"
PLATFORM=linux/amd64

FULL_SHA="${CI_COMMIT_SHA:-$(git -C "$REPO_ROOT" rev-parse HEAD)}"
SHORT_SHA="${FULL_SHA:0:8}"

tf_init 1>&2
ECR_REPO="$(cd "$TF_DIR" && terraform output -raw ecr_repository_url)"
REGISTRY="${ECR_REPO%%/*}"
REPO_NAME="${ECR_REPO#*/}"
REGION="${REGISTRY#*.dkr.ecr.}"
REGION="${REGION%%.*}"
TAG_REF="${ECR_REPO}:${SHORT_SHA}"

CONTAINER_ENGINE="${CONTAINER_ENGINE:-}"
source "$TF_DIR/scripts/container-engine.sh"
resolve_container_engine
{
  aws ecr get-login-password --region "$REGION" \
    | "$CONTAINER_ENGINE" login --username AWS --password-stdin "$REGISTRY"
  "$CONTAINER_ENGINE" build \
    --platform "$PLATFORM" \
    --build-arg BUILD_SHA="$FULL_SHA" \
    -f "$DOCKERFILE" \
    -t "$TAG_REF" \
    "$REPO_ROOT"
  "$CONTAINER_ENGINE" push "$TAG_REF"
} 1>&2

DIGEST="$(aws ecr describe-images \
  --region "$REGION" \
  --repository-name "$REPO_NAME" \
  --image-ids imageTag="$SHORT_SHA" \
  --query 'imageDetails[0].imageDigest' \
  --output text)"
IMAGE_REF="${ECR_REPO}@${DIGEST}"

printf 'IMAGE_REF=%s\n' "$IMAGE_REF" > "$TF_DIR/image.env"
echo "$IMAGE_REF"
