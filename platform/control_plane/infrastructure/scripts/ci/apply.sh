#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 <ENV_NAME> [--image <ref>]" >&2
  exit 2
}

[ $# -ge 1 ] || usage
export ENV_NAME="$1"
shift

IMAGE_REF=""
WITH_IMAGE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --image)
      [ $# -ge 2 ] || usage
      IMAGE_REF="$2"
      WITH_IMAGE=1
      shift 2
      ;;
    *) usage ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"
require_ci_or_break_glass

if [ "$WITH_IMAGE" -eq 1 ]; then
  require_digest_ref "$IMAGE_REF" "apply: --image" || exit 1
fi

tf_init
(cd "$TF_DIR" && terraform apply -input=false plan.out)

if [ "$WITH_IMAGE" -eq 1 ]; then
  aws ssm put-parameter \
    --name "/agp/${ENV_NAME}/backend-image" \
    --type String \
    --overwrite \
    --value "$IMAGE_REF"
fi

OUTPUTS="$(cd "$TF_DIR" && terraform output -json)"
{
  printf 'FRONTEND_URL=%s\n' "$(jq -r '.frontend_url.value' <<<"$OUTPUTS")"
  printf 'API_ENDPOINT=%s\n' "$(jq -r '.api_endpoint.value' <<<"$OUTPUTS")"
} > "$TF_DIR/deploy.env"
cat "$TF_DIR/deploy.env"
