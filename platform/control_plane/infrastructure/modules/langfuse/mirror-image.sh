#!/usr/bin/env bash
# Mirror one upstream image into our ECR, using whichever container engine is
# available (docker or finch). Called by null_resource.push_images (ecr.tf) on the
# machine running `terraform apply`.
#
# Usage: mirror-image.sh <source_image> <tag> <target_repo_url> <ecr_registry> <region> <ecr_repo_name>
#
# Engine selection is delegated to ../../scripts/container-engine.sh — the same
# resolver the deploy scripts use, so `deploy-full.sh --finch` (which exports
# CONTAINER_ENGINE) transparently applies here too.
set -euo pipefail

SRC="$1"; TAG="$2"; TARGET_REPO="$3"; ECR_REGISTRY="$4"; REGION="$5"; REPO_NAME="$6"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# container-engine.sh tests $CONTAINER_ENGINE unquoted, which trips `set -u` when it is
# unset — default it to empty first (empty = "no explicit override", so auto-detect runs).
CONTAINER_ENGINE="${CONTAINER_ENGINE:-}"
# shellcheck source=../../scripts/container-engine.sh
source "$SCRIPT_DIR/../../scripts/container-engine.sh"
resolve_container_engine || exit 1
CE="$CONTAINER_ENGINE"
export CONTAINER_ENGINE   # ecr-login.sh must use the engine we resolved, not re-detect its own
echo "Container engine: $CE"

# Already mirrored? Nothing to do — this is what makes a version bump / re-apply cheap.
if aws ecr describe-images --repository-name "$REPO_NAME" --image-ids imageTag="$TAG" --region "$REGION" >/dev/null 2>&1; then
  echo "Image already exists in ECR: $TARGET_REPO:$TAG — skipping pull"
  exit 0
fi

# ECR auth. null_resource.ecr_login (ecr.tf) already ran this ONCE for the whole apply, before
# any mirror started — that is what keeps the three parallel copies of this script from racing
# each other's `docker login`. Calling it again here is not a second login: ecr-login.sh sees
# the fresh-login stamp and returns immediately. It matters for the case Terraform's graph does
# not cover — a resumed/partial apply where ecr_login is unchanged (so it does not re-run) but
# one mirror does, possibly hours later with an expired 12h token.
"$SCRIPT_DIR/ecr-login.sh" "$ECR_REGISTRY" "$REGION"

# Optional Docker Hub auth (doubles the rate limit from 100 to 200 pulls/6hrs).
# Reads Secrets Manager secret "dockerhub-credentials" if it exists: {"username":"...","token":"..."}
DOCKERHUB_SECRET=$(aws secretsmanager get-secret-value --secret-id dockerhub-credentials --region "$REGION" --query SecretString --output text 2>/dev/null || true)
if [ -n "$DOCKERHUB_SECRET" ]; then
  DH_USER=$(echo "$DOCKERHUB_SECRET" | python3 -c "import sys,json; print(json.load(sys.stdin).get('username',''))" 2>/dev/null || true)
  DH_TOKEN=$(echo "$DOCKERHUB_SECRET" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null || true)
  if [ -n "$DH_USER" ] && [ -n "$DH_TOKEN" ]; then
    echo "$DH_TOKEN" | "$CE" login --username "$DH_USER" --password-stdin 2>/dev/null \
      && echo "Docker Hub: authenticated" \
      || echo "Docker Hub: auth failed, continuing unauthenticated"
  fi
fi

PULLED=false
for i in 1 2 3 4 5; do
  if "$CE" pull --platform linux/amd64 "$SRC:$TAG"; then
    PULLED=true
    break
  fi
  echo "Pull attempt $i failed, waiting 60s before retry..."
  sleep 60
done
if [ "$PULLED" = "false" ]; then
  echo "ERROR: Failed to pull image after 5 attempts"
  exit 1
fi

"$CE" tag "$SRC:$TAG" "$TARGET_REPO:$TAG"

# --platform is REQUIRED for finch/nerdctl, not just cosmetic: without it nerdctl tries to
# build a "-tmp-reduced-platform" single-platform image from the multi-arch manifest and
# fails ("content digest ...: not found") because only the amd64 blobs were pulled above.
# The ECS task definitions run X86_64, so amd64 is the right and only platform to publish.
# docker accepts the same flag, so one command serves both engines.
if ! PUSH_OUT="$("$CE" push --platform linux/amd64 "$TARGET_REPO:$TAG" 2>&1)"; then
  echo "$PUSH_OUT"
  echo "ERROR: push to $TARGET_REPO:$TAG failed (see the engine output above)." >&2
  case "$PUSH_OUT" in
    *nauthorized* | *"authentication required"* | *"no basic auth credentials"* | *"denied"*)
      # The login reported success (or a tolerated -25299) yet the registry rejects us: the
      # stored credential is stale. On macOS that is the stuck-keychain case, and no number of
      # re-applies clears it on its own — name the remedy instead of looping.
      cat >&2 <<EOF
       The registry rejected our credentials even though login did not fail, so the stored
       credential for $ECR_REGISTRY is stale. On macOS, clear it and re-apply:
         security delete-internet-password -s "$ECR_REGISTRY"
         terraform apply
       On other platforms / with finch: $CE logout "$ECR_REGISTRY" then re-apply.
EOF
      ;;
  esac
  exit 1
fi
echo "$PUSH_OUT"
