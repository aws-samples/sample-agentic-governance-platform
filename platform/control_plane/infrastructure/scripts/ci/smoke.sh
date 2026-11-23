#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 <ENV_NAME> [--expect-sha <sha>] [--image <ref>] [--dry-run]" >&2
  exit 2
}

[ $# -ge 1 ] || usage
export ENV_NAME="$1"
shift

EXPECT_SHA=""
IMAGE_REF=""
WITH_IMAGE=0
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --expect-sha)
      [ $# -ge 2 ] || usage
      [[ "$2" =~ ^[0-9a-f]{7,64}$ ]] || { echo "smoke: --expect-sha must match ^[0-9a-f]{7,64}$, got '$2'" >&2; exit 2; }
      EXPECT_SHA="$2"
      shift 2
      ;;
    --image)
      [ $# -ge 2 ] || usage
      IMAGE_REF="$2"
      WITH_IMAGE=1
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    *) usage ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"
if [ "$WITH_IMAGE" -eq 1 ]; then
  require_digest_ref "$IMAGE_REF" "smoke: --image" || exit 2
fi

CHECKS=(
  "GET <api>/health -> 200, .status in {healthy,degraded}, .checks present"
  "GET <api>/ping -> .ping == \"pong\""
  "GET <api>/ -> .build_sha == <expect-sha>"
  "GET <frontend>/ -> contains data-build-sha=\"<expect-sha>\""
  "GET <api>/api/v1/agents without token -> 401"
  "aws ecs describe-task-definition <PRIMARY taskDefinition> -> containerDefinitions[0].image == <image>"
  "aws ssm get-parameter /agp/<env>/backend-image -> Parameter.Value == <image>"
)
NEEDS_SHA=(0 0 1 1 0 0 0)
NEEDS_IMAGE=(0 0 0 0 0 1 1)

FAILURES=0
report() {
  echo "$1 $2"
  [ "$1" != FAIL ] || FAILURES=$((FAILURES + 1))
}

if [ "$DRY_RUN" -eq 1 ]; then
  for i in "${!CHECKS[@]}"; do
    if [ "${NEEDS_SHA[$i]}" -eq 1 ] && [ -z "$EXPECT_SHA" ]; then
      report "SKIPPED" "${CHECKS[$i]} (--expect-sha absent)"
    elif [ "${NEEDS_IMAGE[$i]}" -eq 1 ] && [ "$WITH_IMAGE" -eq 0 ]; then
      report "SKIPPED" "${CHECKS[$i]} (--image absent)"
    else
      report "WOULD-RUN" "${CHECKS[$i]}"
    fi
  done
  exit 0
fi

tf_init 1>&2
API="$(cd "$TF_DIR" && terraform output -raw api_endpoint)"
FRONTEND="$(cd "$TF_DIR" && terraform output -raw frontend_url)"
API="${API%/}"
FRONTEND="${FRONTEND%/}"

BODY_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE"' EXIT

fetch() {
  curl -sS -o "$BODY_FILE" -w '%{http_code}' --max-time 20 "$1" || echo 000
}

code="$(fetch "$API/health")"
if [ "$code" = 200 ] && jq -e '(.status == "healthy" or .status == "degraded") and has("checks")' "$BODY_FILE" >/dev/null 2>&1; then
  report PASS "${CHECKS[0]} (status=$(jq -r .status "$BODY_FILE"))"
else
  report FAIL "${CHECKS[0]} (http $code)"
fi

code="$(fetch "$API/ping")"
if [ "$code" = 200 ] && jq -e '.ping == "pong"' "$BODY_FILE" >/dev/null 2>&1; then
  report PASS "${CHECKS[1]}"
else
  report FAIL "${CHECKS[1]} (http $code)"
fi

if [ -z "$EXPECT_SHA" ]; then
  report SKIPPED "${CHECKS[2]} (--expect-sha absent)"
else
  code="$(fetch "$API/")"
  got="$(jq -r '.build_sha // ""' "$BODY_FILE" 2>/dev/null || true)"
  if [ "$code" = 200 ] && jq -e --arg s "$EXPECT_SHA" '.build_sha == $s' "$BODY_FILE" >/dev/null 2>&1; then
    report PASS "${CHECKS[2]}"
  else
    report FAIL "${CHECKS[2]} (http $code, build_sha=$got)"
  fi
fi

if [ -z "$EXPECT_SHA" ]; then
  report SKIPPED "${CHECKS[3]} (--expect-sha absent)"
else
  code="$(fetch "$FRONTEND/")"
  if [ "$code" = 200 ] && grep -Fq "data-build-sha=\"$EXPECT_SHA\"" "$BODY_FILE"; then
    report PASS "${CHECKS[3]}"
  else
    report FAIL "${CHECKS[3]} (http $code)"
  fi
fi

code="$(fetch "$API/api/v1/agents")"
if [ "$code" = 401 ]; then
  report PASS "${CHECKS[4]}"
else
  report FAIL "${CHECKS[4]} (http $code)"
fi

if [ "$WITH_IMAGE" -eq 0 ]; then
  report SKIPPED "${CHECKS[5]} (--image absent)"
  report SKIPPED "${CHECKS[6]} (--image absent)"
else
  CLUSTER="$(cd "$TF_DIR" && terraform output -raw ecs_cluster_name)"
  SERVICE="$(cd "$TF_DIR" && terraform output -raw ecs_service_name)"
  TASK_DEFINITION="$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
    --query 'services[0].deployments[?status==`PRIMARY`] | [0].taskDefinition' --output text)"
  got="$(aws ecs describe-task-definition --task-definition "$TASK_DEFINITION" \
    --query 'taskDefinition.containerDefinitions[0].image' --output text)"
  if [ "$got" = "$IMAGE_REF" ]; then
    report PASS "${CHECKS[5]}"
  else
    report FAIL "${CHECKS[5]} (task definition $TASK_DEFINITION runs $got)"
  fi

  got="$(aws ssm get-parameter --name "/agp/${ENV_NAME}/backend-image" --query Parameter.Value --output text)"
  if [ "$got" = "$IMAGE_REF" ]; then
    report PASS "${CHECKS[6]}"
  else
    report FAIL "${CHECKS[6]} (parameter holds $got)"
  fi
fi

[ "$FAILURES" -eq 0 ]
