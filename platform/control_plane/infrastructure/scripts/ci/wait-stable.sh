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

INTERVAL_SECONDS=15
TIMEOUT_SECONDS=900

tf_init 1>&2
CLUSTER="$(cd "$TF_DIR" && terraform output -raw ecs_cluster_name)"
SERVICE="$(cd "$TF_DIR" && terraform output -raw ecs_service_name)"
[ -n "$CLUSTER" ] && [ -n "$SERVICE" ] \
  || { echo "wait-stable: outputs ecs_cluster_name/ecs_service_name are empty for ENV_NAME=$ENV_NAME" >&2; exit 1; }

deadline=$((SECONDS + TIMEOUT_SECONDS))
state=""
reason=""
while [ "$SECONDS" -lt "$deadline" ]; do
  primary="$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
    --query 'services[0].deployments[?status==`PRIMARY`] | [0].[rolloutState, rolloutStateReason]' \
    --output json)"
  state="$(jq -r '.[0] // ""' <<<"$primary")"
  reason="$(jq -r '.[1] // ""' <<<"$primary")"
  echo "$CLUSTER/$SERVICE primary rolloutState=$state"
  case "$state" in
    COMPLETED) exit 0 ;;
    FAILED)
      echo "wait-stable: rollout FAILED: $reason" >&2
      exit 1
      ;;
  esac
  sleep "$INTERVAL_SECONDS"
done
echo "wait-stable: timed out after ${TIMEOUT_SECONDS}s in rolloutState=$state: $reason" >&2
exit 1
