#!/usr/bin/env bash

TF_DIR="${TF_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
ENV_NAME="${ENV_NAME:?ENV_NAME is required}"
[[ "$ENV_NAME" =~ ^[a-z0-9-]+$ ]] || { echo "common.sh: ENV_NAME must match ^[a-z0-9-]+$, got '$ENV_NAME'" >&2; exit 2; }
if [[ "$ENV_NAME" =~ ^pr-[0-9]+$ ]]; then
  BACKEND_CONFIG="backend.ephemeral.config"
  TFVARS="env/ephemeral.tfvars"
  TF_BACKEND_KEY_ARGS=(-backend-config="key=env/${ENV_NAME}/terraform.tfstate")
else
  BACKEND_CONFIG="backend.${ENV_NAME}.config"
  TFVARS="env/${ENV_NAME}.tfvars"
  TF_BACKEND_KEY_ARGS=()
fi
TF_ENV_ARGS=(-var "environment=${ENV_NAME}")

[ -f "$TF_DIR/$TFVARS" ] || { echo "common.sh: missing $TF_DIR/$TFVARS for ENV_NAME=$ENV_NAME" >&2; exit 1; }
AWS_REGION="$(sed -nE 's/^[[:space:]]*aws_region[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "$TF_DIR/$TFVARS" 2>/dev/null | tail -n 1)"
[ -n "$AWS_REGION" ] || { echo "common.sh: cannot read aws_region = \"<region>\" from $TF_DIR/$TFVARS" >&2; exit 1; }
export AWS_REGION
export AWS_DEFAULT_REGION="$AWS_REGION"

require_digest_ref() {
  [[ "$1" =~ ^[^@[:space:]]+@sha256:[0-9a-f]{64}$ ]] && return 0
  echo "$2 must be <ecr-repo-url>@sha256:<64 hex>, got '$1'" >&2
  return 1
}

require_ci_or_break_glass() {
  [[ "$ENV_NAME" =~ ^pr-[0-9]+$ ]] || [ "${GITLAB_CI:-}" = true ] || [ "${AGP_BREAK_GLASS:-}" = 1 ] \
    || { echo "${0##*/}: only pr-<iid> stacks run outside CI or break-glass (deploy-full.sh)" >&2; exit 2; }
}

config_value() {
  sed -nE "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"([^\"]*)\".*/\1/p" "$TF_DIR/$BACKEND_CONFIG" | tail -n 1
}

load_state_bucket() {
  [ -f "$TF_DIR/$BACKEND_CONFIG" ] || { echo "common.sh: missing $TF_DIR/$BACKEND_CONFIG for ENV_NAME=$ENV_NAME" >&2; return 1; }
  STATE_BUCKET="$(config_value bucket)"
  [ -n "$STATE_BUCKET" ] || { echo "common.sh: $BACKEND_CONFIG needs bucket" >&2; return 1; }
}

tf_init() {
  local missing=0 f
  for f in "$BACKEND_CONFIG" "$TFVARS"; do
    if [ ! -f "$TF_DIR/$f" ]; then
      echo "tf_init: missing $TF_DIR/$f for ENV_NAME=$ENV_NAME" >&2
      missing=1
    fi
  done
  [ "$missing" -eq 0 ] || return 1
  (cd "$TF_DIR" && terraform init -backend-config="$BACKEND_CONFIG" \
    ${TF_BACKEND_KEY_ARGS[@]+"${TF_BACKEND_KEY_ARGS[@]}"} -input=false -reconfigure)
}
