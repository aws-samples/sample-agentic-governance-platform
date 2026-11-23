#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 <ENV_NAME> [--image <ref>] [--lock=false]" >&2
  exit 2
}

[ $# -ge 1 ] || usage
export ENV_NAME="$1"
shift

IMAGE_REF=""
WITH_IMAGE=0
LOCK_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --image)
      [ $# -ge 2 ] || usage
      IMAGE_REF="$2"
      WITH_IMAGE=1
      shift 2
      ;;
    --lock=false)
      LOCK_ARGS=(-lock=false)
      shift
      ;;
    *) usage ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"
require_ci_or_break_glass

IMAGE_ARGS=()
if [ "$WITH_IMAGE" -eq 1 ]; then
  require_digest_ref "$IMAGE_REF" "plan: --image" || exit 1
  IMAGE_ARGS=(-var "container_image=$IMAGE_REF")
fi

if [ "${#TF_BACKEND_KEY_ARGS[@]}" -gt 0 ]; then
  load_state_bucket
  STATE_KEYS="$(aws s3api list-objects-v2 --bucket "$STATE_BUCKET" --prefix env/pr- \
    --query 'Contents[].Key' --output text)"
  OTHER_STACKS="$(tr '\t' '\n' <<<"$STATE_KEYS" | grep -E '/terraform\.tfstate$' | grep -v "^env/${ENV_NAME}/" || true)"
  OTHER_COUNT="$(grep -c . <<<"$OTHER_STACKS" || true)"
  if [ "$OTHER_COUNT" -ge "${AGP_EPHEMERAL_CAP:-8}" ]; then
    echo "$OTHER_STACKS" >&2
    echo "plan: ephemeral cap reached; run scripts/ci/sweep.sh dev" >&2
    exit 1
  fi
fi

tf_init
(
  cd "$TF_DIR"
  terraform plan -out=plan.out -input=false -var-file="$TFVARS" "${TF_ENV_ARGS[@]}" \
    ${IMAGE_ARGS[@]+"${IMAGE_ARGS[@]}"} ${LOCK_ARGS[@]+"${LOCK_ARGS[@]}"}
  terraform show -json plan.out > plan.json
  "$SCRIPT_DIR/plan-summary.sh" plan.json > plan-summary.json
  cat plan-summary.json
)
