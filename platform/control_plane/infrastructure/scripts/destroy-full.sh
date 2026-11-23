#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 <ENV_NAME>" >&2
  exit 2
}

[ $# -eq 1 ] || usage
export ENV_NAME="$1"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "destroy-full: $*" >&2; exit 1; }

[[ ! "$ENV_NAME" =~ ^pr-[0-9]+$ ]] || die "pr-<number> names are reserved"
source "$SCRIPT_DIR/ci/common.sh"
load_state_bucket
grep -Eq '^[[:space:]]*ephemeral[[:space:]]*=[[:space:]]*true' "$TF_DIR/$TFVARS" \
  || die "$TFVARS needs ephemeral = true, applied once with deploy-full.sh, before the stack can be destroyed"

PROJECT_NAME="$(sed -nE 's/^[[:space:]]*project_name[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "$TF_DIR/$TFVARS" | tail -n 1)"
[ -n "$PROJECT_NAME" ] || die "cannot read project_name from $TFVARS"
export TF_VAR_entra_backend_client_secret="${TF_VAR_entra_backend_client_secret:-unused-by-destroy}"
export TF_VAR_langfuse_admin_password="${TF_VAR_langfuse_admin_password:-unused-by-destroy}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)" || die "no AWS credentials"
echo "This destroys every resource of the '$ENV_NAME' stack in account $ACCOUNT_ID ($AWS_REGION), data included."
echo "Langfuse's Lambda@Edge replicas can block the destroy for a few hours after CloudFront is disabled; rerun this script then."
echo "Delete your agents and MCP servers in the UI first: their runtimes' Terraform state lives in a bucket this destroys."
read -r -p "Type the environment name to confirm: " answer
[ "$answer" = "$ENV_NAME" ] || die "not confirmed"

tolerate() {
  local pattern="$1" out
  shift
  if out="$("$@" 2>&1)"; then
    [ -z "$out" ] || echo "$out"
    return 0
  fi
  grep -q "$pattern" <<<"$out" && return 0
  echo "$out" >&2
  return 1
}

list_text() {
  local out
  out="$("$@")" || return 1
  [ "$out" != "None" ] || return 0
  tr '\t' '\n' <<<"$out" | grep . || true
}

tf_init
(cd "$TF_DIR" && terraform destroy -input=false -auto-approve -var-file="$TFVARS" "${TF_ENV_ARGS[@]}" \
  -var container_image=unused-by-destroy)

tolerate ParameterNotFound aws ssm delete-parameter --name "/agp/${ENV_NAME}/backend-image"

SECRET_ARNS="$(list_text aws secretsmanager list-secrets --filters "Key=name,Values=agp-${ENV_NAME}/" --query 'SecretList[].ARN' --output text)"
while read -r secret_arn; do
  [ -n "$secret_arn" ] || continue
  tolerate ResourceNotFoundException aws secretsmanager delete-secret --secret-id "$secret_arn" --force-delete-without-recovery
done <<<"$SECRET_ARNS"

ALL_ROLE_NAMES="$(list_text aws iam list-roles --query 'Roles[].RoleName' --output text)"
ROLE_NAMES="$(grep -E "^${PROJECT_NAME}-cp-${ENV_NAME}-[0-9a-z]{6}-(ecr-push-.+|agent-ecr-push)$" <<<"$ALL_ROLE_NAMES" || true)"
while read -r role_name; do
  [ -n "$role_name" ] || continue
  POLICY_ARNS="$(list_text aws iam list-attached-role-policies --role-name "$role_name" --query 'AttachedPolicies[].PolicyArn' --output text)"
  while read -r policy_arn; do
    [ -n "$policy_arn" ] || continue
    tolerate NoSuchEntity aws iam detach-role-policy --role-name "$role_name" --policy-arn "$policy_arn"
  done <<<"$POLICY_ARNS"
  POLICY_NAMES="$(list_text aws iam list-role-policies --role-name "$role_name" --query 'PolicyNames' --output text)"
  while read -r policy_name; do
    [ -n "$policy_name" ] || continue
    tolerate NoSuchEntity aws iam delete-role-policy --role-name "$role_name" --policy-name "$policy_name"
  done <<<"$POLICY_NAMES"
  tolerate NoSuchEntity aws iam delete-role --role-name "$role_name"
done <<<"$ROLE_NAMES"

echo "destroy-full: ${ENV_NAME} removed. The state bucket $STATE_BUCKET is kept; delete it by hand if you are done."
