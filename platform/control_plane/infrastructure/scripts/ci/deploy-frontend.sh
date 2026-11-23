#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 <ENV_NAME> [--write-env-only]" >&2
  exit 2
}

[ $# -ge 1 ] || usage
export ENV_NAME="$1"
shift
WRITE_ENV_ONLY=0
case "${1:-}" in
  "") ;;
  --write-env-only) WRITE_ENV_ONLY=1 ;;
  *) usage ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"
require_ci_or_break_glass
FRONTEND_DIR="$(cd "$TF_DIR/../frontend" && pwd)"
ENV_FILE="$FRONTEND_DIR/.env.production"

tf_init 1>&2
OUTPUTS="$(cd "$TF_DIR" && terraform output -json)"

output_value() {
  jq -r --arg k "$1" '.[$k].value // ""' <<<"$OUTPUTS"
}

unusable() {
  case "$1" in
    "" | *"<"* | *">"* | 00000000-0000-0000-0000-*) return 0 ;;
    *) return 1 ;;
  esac
}

OUTPUT_TO_VITE=(
  api_endpoint:VITE_API_URL
  auth_provider:VITE_AUTH_PROVIDER
  entra_tenant_id:VITE_ENTRA_TENANT_ID
  entra_tenant_domain:VITE_ENTRA_TENANT_DOMAIN
  entra_spa_client_id:VITE_ENTRA_SPA_CLIENT_ID
  entra_spa_redirect_uri:VITE_ENTRA_SPA_REDIRECT_URI
  entra_spa_scope:VITE_ENTRA_SPA_SCOPE
  product_mode:VITE_PRODUCT_MODE
)

ENV_LINES=()
REJECTED=()
for pair in "${OUTPUT_TO_VITE[@]}"; do
  output_name="${pair%%:*}"
  vite_key="${pair#*:}"
  value="$(output_value "$output_name")"
  if unusable "$value"; then
    REJECTED+=("$output_name")
  fi
  ENV_LINES+=("${vite_key}=${value}")
done
if [ "${#REJECTED[@]}" -gt 0 ]; then
  echo "deploy-frontend: terraform output empty or still a placeholder: ${REJECTED[*]}" >&2
  exit 1
fi
BUILD_SHA="${CI_COMMIT_SHA:-$(git -C "$SCRIPT_DIR" rev-parse HEAD)}"
ENV_LINES+=("VITE_BUILD_SHA=${BUILD_SHA}")

printf '%s\n' "${ENV_LINES[@]}" > "$ENV_FILE"
echo "wrote $ENV_FILE (${#ENV_LINES[@]} lines)"
[ "$WRITE_ENV_ONLY" -eq 0 ] || exit 0

FRONTEND_BUCKET="$(output_value frontend_bucket_name)"
CLOUDFRONT_ID="$(output_value cloudfront_distribution_id)"
FRONTEND_URL="$(output_value frontend_url)"

(cd "$FRONTEND_DIR" && npm ci && npm run build)
grep -Fq "data-build-sha=\"$BUILD_SHA\"" "$FRONTEND_DIR/dist/index.html" \
  || { echo "deploy-frontend: dist/index.html lacks data-build-sha=\"$BUILD_SHA\"; the build did not consume $ENV_FILE" >&2; exit 1; }
if grep -lqw devauth "$FRONTEND_DIR"/dist/assets/*.js 2>/dev/null; then
  echo "deploy-frontend: dist carries the dev-auth arm; build in the default mode" >&2
  exit 1
fi

aws s3 sync "$FRONTEND_DIR/dist/assets/" "s3://${FRONTEND_BUCKET}/assets/" --delete --quiet \
  --cache-control "public, max-age=31536000, immutable"
aws s3 sync "$FRONTEND_DIR/dist/" "s3://${FRONTEND_BUCKET}/" --delete --quiet \
  --exclude "assets/*" --cache-control "no-cache"
aws cloudfront create-invalidation \
  --distribution-id "$CLOUDFRONT_ID" \
  --paths "/*" \
  --query 'Invalidation.Status' \
  --output text
echo "$FRONTEND_URL"
