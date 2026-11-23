#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 [ENV_NAME]   (default: dev)" >&2
  exit 2
}

[ $# -le 1 ] || usage
export ENV_NAME="${1:-dev}"
export AGP_BREAK_GLASS=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

die() { echo "deploy-full: $*" >&2; exit 1; }

[[ "$ENV_NAME" =~ ^[a-z0-9-]+$ ]] || die "ENV_NAME must match ^[a-z0-9-]+$, got '$ENV_NAME'"
[[ ! "$ENV_NAME" =~ ^pr-[0-9]+$ ]] || die "pr-<number> names are reserved; pick another ENV_NAME"
[ -f "$INFRA_DIR/env/$ENV_NAME.tfvars" ] \
  || die "missing env/$ENV_NAME.tfvars; copy env/dev.tfvars.example to it and fill in the placeholders"
! grep -Eq '^[^#]*"[^"]*<[^">]*>[^"]*"' "$INFRA_DIR/env/$ENV_NAME.tfvars" \
  || die "env/$ENV_NAME.tfvars still carries a <placeholder>"
if grep -Eq '^[[:space:]]*create_langfuse[[:space:]]*=[[:space:]]*true' "$INFRA_DIR/env/$ENV_NAME.tfvars"; then
  [[ "$ENV_NAME" =~ ^(dev|prod)$ ]] \
    || die "with create_langfuse = true the Langfuse module accepts only dev or prod as ENV_NAME, got '$ENV_NAME'"
fi
[ -n "${TF_VAR_entra_backend_client_secret:-}" ] || die "export TF_VAR_entra_backend_client_secret"
[ -n "${TF_VAR_langfuse_admin_password:-}" ] || die "export TF_VAR_langfuse_admin_password"
[ -x "$INFRA_DIR/../backend/venv/bin/python" ] \
  || die "create the backend virtualenv first: cd ../backend && python3 -m venv venv && venv/bin/pip install -r requirements.txt"
for tool in aws terraform jq npm git; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed"
done
git -C "$SCRIPT_DIR" rev-parse HEAD >/dev/null 2>&1 \
  || die "run from a git clone; the build is stamped with the commit SHA"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)" || die "no AWS credentials"
echo "credentials ok"

source "$SCRIPT_DIR/ci/common.sh"

if [ ! -f "$TF_DIR/$BACKEND_CONFIG" ]; then
  cat > "$TF_DIR/$BACKEND_CONFIG" <<CONFIG
bucket       = "agp-${ENV_NAME}-tf-state-${ACCOUNT_ID}"
key          = "env/${ENV_NAME}/terraform.tfstate"
region       = "${AWS_REGION}"
use_lockfile = "true"
encrypt      = "true"
CONFIG
  echo "wrote $BACKEND_CONFIG"
fi

load_state_bucket
BUCKET_REGION="$(config_value region)"
[ -n "$BUCKET_REGION" ] || die "$BACKEND_CONFIG needs region"
if aws s3api head-bucket --bucket "$STATE_BUCKET" >/dev/null 2>&1; then
  echo "state bucket $STATE_BUCKET: exists"
else
  if [ "$BUCKET_REGION" = us-east-1 ]; then
    aws s3api create-bucket --bucket "$STATE_BUCKET" --region "$BUCKET_REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$STATE_BUCKET" --region "$BUCKET_REGION" \
      --create-bucket-configuration "LocationConstraint=$BUCKET_REGION" >/dev/null
  fi
  aws s3api put-bucket-versioning --bucket "$STATE_BUCKET" --versioning-configuration Status=Enabled
  aws s3api put-bucket-encryption --bucket "$STATE_BUCKET" \
    --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
  aws s3api put-public-access-block --bucket "$STATE_BUCKET" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  echo "state bucket $STATE_BUCKET: created, versioned, encrypted, public access blocked"
fi

tf_init
if ! (cd "$TF_DIR" && terraform output -raw ecr_repository_url >/dev/null 2>&1); then
  echo "first deploy: creating the backend image repository"
  (cd "$TF_DIR" && terraform apply -input=false -auto-approve -target=module.ecr \
    -var-file="$TFVARS" "${TF_ENV_ARGS[@]}" -var container_image=not-built-yet)
fi

IMAGE_REF="$("$SCRIPT_DIR/ci/build-image.sh" "$ENV_NAME" | tail -n 1)"
SHA="$(git -C "$SCRIPT_DIR" rev-parse HEAD)"

"$SCRIPT_DIR/ci/plan.sh" "$ENV_NAME" --image "$IMAGE_REF"
"$SCRIPT_DIR/ci/apply.sh" "$ENV_NAME" --image "$IMAGE_REF"
"$SCRIPT_DIR/ci/deploy-frontend.sh" "$ENV_NAME"
"$SCRIPT_DIR/ci/wait-stable.sh" "$ENV_NAME"
"$SCRIPT_DIR/ci/smoke.sh" "$ENV_NAME" --expect-sha "$SHA"

cat "$TF_DIR/deploy.env"
if grep -Eq '^[[:space:]]*agp_api_url[[:space:]]*=[[:space:]]*""' "$TF_DIR/$TFVARS"; then
  echo "next: set agp_api_url = \"<API_ENDPOINT>/api/v1\" in $TFVARS and run this script again"
fi
echo "next: add <FRONTEND_URL>/auth/callback as a redirect URI on the SPA app registration"
