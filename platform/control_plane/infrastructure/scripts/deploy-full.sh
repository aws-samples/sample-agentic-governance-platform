#!/bin/bash

# Full Control Plane Deployment Script
# Deploys infrastructure, backend, and frontend. Auth is Microsoft Entra ID;
# user management happens in the Entra directory, not in this script.
# Prerequisites: AWS credentials exported, Docker running, Node.js installed, Terraform installed.

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
INFRA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/platform/control_plane/backend"
FRONTEND_DIR="$REPO_ROOT/platform/control_plane/frontend"

# --finch forces the finch container engine (no Docker Desktop). See container-engine.sh.
USE_FINCH=0
for arg in "$@"; do
    case "$arg" in
        --finch) USE_FINCH=1 ;;
    esac
done
source "$SCRIPT_DIR/container-engine.sh"

# ============================================================================
# Frontend env contract
# ============================================================================
# The SPA reads exactly seven VITE_* variables (frontend/src/vite-env.d.ts). Two of them
# this script owns and always overwrites, because terraform is authoritative for them:
# VITE_API_URL and VITE_AUTH_PROVIDER. The other five are operator input and are only
# ever read, never invented.
#
# Resolution order for those five, highest first:
#   exported environment variable
#   frontend/.env.production.local     (Vite's highest-precedence file)
#   frontend/.env.local
#   frontend/.env                      <- the file the README tells operators to fill
#   frontend/.env.production           <- LAST: this is this script's own output, so a
#                                         stale generated value must never win over a
#                                         hand-edited .env
# The .local files rank above .env because Vite ranks them there for a production build,
# so what this script validates is what the build would actually use.

FRONTEND_ENV_SOURCES=(
    "$FRONTEND_DIR/.env.production.local"
    "$FRONTEND_DIR/.env.local"
    "$FRONTEND_DIR/.env"
    "$FRONTEND_DIR/.env.production"
)

# dotenv_get <file> <key> — echoes the value of KEY, last assignment wins, quotes stripped.
dotenv_get() {
    local file="$1" key="$2" line value
    [ -f "$file" ] || return 0
    line=$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file" | tail -n 1)
    [ -n "$line" ] || return 0
    value="${line#*=}"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    printf '%s' "$value"
}

# resolve_frontend_var <KEY> — echoes the operator-supplied value, or empty if there is none.
resolve_frontend_var() {
    local key="$1" value="" file
    value="${!key:-}"
    for file in "${FRONTEND_ENV_SOURCES[@]}"; do
        [ -n "$value" ] && break
        value=$(dotenv_get "$file" "$key")
    done
    printf '%s' "$value"
}

# True when a value cannot be used. An unfilled example placeholder is exactly as broken as
# a missing value — `cp .env.example .env` and forget is the common way login dies — so an
# angle-bracketed stub (`<api-id>`, `<cloudfront-domain>`) and an all-zero GUID both count
# as absent. No real tenant or client id is all zeros.
frontend_var_unusable() {
    case "$1" in
        "" | *"<"* | *">"* | 00000000-0000-0000-0000-*) return 0 ;;
        *) return 1 ;;
    esac
}

# ============================================================================
# Preflight Checks
# ============================================================================

echo -e "${BLUE}============================================${NC}"
echo -e "${BLUE}  Control Plane Full Deployment${NC}"
echo -e "${BLUE}============================================${NC}"
echo

echo "Running preflight checks..."

# AWS credentials
if ! aws sts get-caller-identity &> /dev/null; then
    echo -e "${RED}Error: AWS credentials not configured. Export your credentials and try again.${NC}"
    exit 1
fi
AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo "us-east-1")}"
echo -e "${GREEN}  AWS account: ${AWS_ACCOUNT} (${AWS_REGION})${NC}"

# Container engine (docker or finch)
if ! resolve_container_engine; then
    exit 1
fi
echo -e "${GREEN}  Container engine: ${CONTAINER_ENGINE} (running)${NC}"

# Terraform
if ! command -v terraform &> /dev/null; then
    echo -e "${RED}Error: Terraform is not installed.${NC}"
    exit 1
fi
echo -e "${GREEN}  Terraform: $(terraform version -json | python3 -c "import sys,json;print(json.load(sys.stdin)['terraform_version'])")${NC}"

# Node
if ! command -v node &> /dev/null; then
    echo -e "${RED}Error: Node.js is not installed.${NC}"
    exit 1
fi
echo -e "${GREEN}  Node: $(node --version)${NC}"

# Entra SPA config. Checked HERE, before anything is created, because a frontend built
# without these values deploys and reports success but cannot start sign-in: MSAL has no
# client id, no authority and no redirect URI. A deploy nobody can log into is a failed
# deploy, so refuse it up front instead of at the end.
# VITE_ENTRA_SPA_REDIRECT_URI is deliberately NOT required: it is a CloudFront domain that
# does not exist before the first apply, so step 4 derives it from terraform output.
ENTRA_TENANT_ID=$(resolve_frontend_var VITE_ENTRA_TENANT_ID)
ENTRA_TENANT_DOMAIN=$(resolve_frontend_var VITE_ENTRA_TENANT_DOMAIN)
ENTRA_SPA_CLIENT_ID=$(resolve_frontend_var VITE_ENTRA_SPA_CLIENT_ID)
ENTRA_SPA_SCOPE=$(resolve_frontend_var VITE_ENTRA_SPA_SCOPE)

MISSING_FRONTEND_VARS=""
frontend_var_unusable "$ENTRA_TENANT_ID" && MISSING_FRONTEND_VARS="${MISSING_FRONTEND_VARS} VITE_ENTRA_TENANT_ID"
frontend_var_unusable "$ENTRA_TENANT_DOMAIN" && MISSING_FRONTEND_VARS="${MISSING_FRONTEND_VARS} VITE_ENTRA_TENANT_DOMAIN"
frontend_var_unusable "$ENTRA_SPA_CLIENT_ID" && MISSING_FRONTEND_VARS="${MISSING_FRONTEND_VARS} VITE_ENTRA_SPA_CLIENT_ID"
frontend_var_unusable "$ENTRA_SPA_SCOPE" && MISSING_FRONTEND_VARS="${MISSING_FRONTEND_VARS} VITE_ENTRA_SPA_SCOPE"

if [ -n "$MISSING_FRONTEND_VARS" ]; then
    echo
    echo -e "${RED}Error: the frontend has no Entra sign-in configuration. Missing (or still a placeholder):${NC}"
    for missing_var in $MISSING_FRONTEND_VARS; do
        echo -e "${RED}    ${missing_var}${NC}"
    done
    echo
    echo "  Set each one in ${FRONTEND_DIR}/.env (cp .env.example .env) or export it."
    echo "  Values come from your Entra app registrations — see \"Bootstrapping\" in the repo README."
    echo "  Refusing to deploy: the build would succeed and sign-in would fail with no diagnostic."
    exit 1
fi
echo -e "${GREEN}  Entra SPA config: present (tenant ${ENTRA_TENANT_DOMAIN}, client ${ENTRA_SPA_CLIENT_ID})${NC}"

echo

# ============================================================================
# Step 1: Terraform
# ============================================================================

echo -e "${BLUE}[1/5] Infrastructure${NC}"

cd "$INFRA_DIR"

if [ ! -f terraform.tfvars ]; then
    echo "Creating terraform.tfvars from example..."
    cp terraform.tfvars.example terraform.tfvars
fi

# Check for stale terraform state from a different AWS account
if [ -f terraform.tfstate ]; then
    STATE_ACCOUNT=$(python3 -c "
import json, sys
try:
    with open('terraform.tfstate') as f:
        state = json.load(f)
    for r in state.get('resources', []):
        for i in r.get('instances', []):
            arn = i.get('attributes', {}).get('arn', '')
            if ':' in arn:
                parts = arn.split(':')
                if len(parts) >= 5 and parts[4]:
                    print(parts[4])
                    sys.exit(0)
except Exception:
    pass
" 2>/dev/null)

    if [ -n "$STATE_ACCOUNT" ] && [ "$STATE_ACCOUNT" != "$AWS_ACCOUNT" ]; then
        echo -e "${YELLOW}  Existing terraform state references account ${STATE_ACCOUNT}${NC}"
        echo -e "${YELLOW}  but current credentials are for account ${AWS_ACCOUNT}.${NC}"
        echo
        read -p "  Back up and reset state for clean deployment? (yes/no): " reset_state
        if [ "$reset_state" = "yes" ]; then
            backup="terraform.tfstate.backup.${STATE_ACCOUNT}.$(date +%Y%m%d%H%M%S)"
            mv terraform.tfstate "$backup"
            [ -f terraform.tfstate.backup ] && mv terraform.tfstate.backup "${backup}.prev"
            echo -e "${GREEN}  State backed up to ${backup} and reset.${NC}"
        else
            echo -e "${RED}  Cannot proceed with mismatched state. Either reset the state or switch AWS credentials.${NC}"
            exit 1
        fi
    fi
fi

# Clean terraform cache if state was reset
if [ ! -f terraform.tfstate ]; then
    rm -rf .terraform .terraform.lock.hcl
fi

terraform init -input=false
terraform plan -out=tfplan

echo
echo -e "${YELLOW}Review the plan above.${NC}"
read -p "Apply? (yes/no): " confirm
if [ "$confirm" != "yes" ]; then
    echo -e "${YELLOW}Deployment cancelled.${NC}"
    rm -f tfplan
    exit 0
fi

terraform apply tfplan
rm -f tfplan

# Capture outputs
ECR_REPO=$(terraform output -raw ecr_repository_url)
FRONTEND_BUCKET=$(terraform output -raw frontend_bucket_name)
CLOUDFRONT_ID=$(terraform output -raw cloudfront_distribution_id)
API_ENDPOINT=$(terraform output -raw api_endpoint)
FRONTEND_URL=$(terraform output -raw frontend_url)
ECS_CLUSTER=$(terraform output -raw ecs_cluster_name)
ECS_SERVICE=$(terraform output -raw ecs_service_name)

# Get region from the ECR repo URL (authoritative from terraform)
AWS_REGION=$(echo "$ECR_REPO" | sed 's/.*\.ecr\.\(.*\)\.amazonaws\.com.*/\1/')

echo -e "${GREEN}  Infrastructure deployed.${NC}"
echo

# ============================================================================
# Step 2: Backend Docker Image
# ============================================================================

echo -e "${BLUE}[2/5] Backend Docker image${NC}"

AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
echo "  Logging into ECR ($ECR_REGISTRY)..."
aws ecr get-login-password --region "$AWS_REGION" | "$CONTAINER_ENGINE" login --username AWS --password-stdin "$ECR_REGISTRY"

echo "  Building linux/amd64 image (via ${CONTAINER_ENGINE})..."
"$CONTAINER_ENGINE" build \
    --platform linux/amd64 \
    -f "$BACKEND_DIR/Dockerfile" \
    -t "${ECR_REPO}:latest" \
    "$REPO_ROOT"

echo "  Pushing to ECR..."
"$CONTAINER_ENGINE" push "${ECR_REPO}:latest"

echo -e "${GREEN}  Backend image pushed.${NC}"
echo

# ============================================================================
# Step 3: ECS Deployment
# ============================================================================

echo -e "${BLUE}[3/5] ECS rolling deployment${NC}"

aws ecs update-service \
    --cluster "$ECS_CLUSTER" \
    --service "$ECS_SERVICE" \
    --force-new-deployment \
    --region "$AWS_REGION" \
    --query 'service.deployments[0].rolloutState' \
    --output text

echo "  Waiting for tasks to stabilize..."
aws ecs wait services-stable \
    --cluster "$ECS_CLUSTER" \
    --services "$ECS_SERVICE" \
    --region "$AWS_REGION" 2>/dev/null || true

echo -e "${GREEN}  ECS service updated.${NC}"
echo

# ============================================================================
# Step 4: Frontend Build
# ============================================================================

echo -e "${BLUE}[4/5] Frontend build${NC}"

# The redirect URI is the one value that cannot exist before the first apply, so fall back
# to the CloudFront/custom domain terraform just reported. The SAME string has to be
# registered as a redirect URI on the SPA app registration or Entra rejects the sign-in.
ENTRA_SPA_REDIRECT_URI=$(resolve_frontend_var VITE_ENTRA_SPA_REDIRECT_URI)
if frontend_var_unusable "$ENTRA_SPA_REDIRECT_URI"; then
    ENTRA_SPA_REDIRECT_URI="${FRONTEND_URL%/}/auth/callback"
    echo -e "${YELLOW}  No VITE_ENTRA_SPA_REDIRECT_URI configured; deriving ${ENTRA_SPA_REDIRECT_URI}${NC}"
    echo -e "${YELLOW}  Register that exact URI on the SPA app registration (platform: single-page application).${NC}"
fi

# Write all seven variables the SPA reads. Any other key already in .env.production belongs
# to the operator, not to this script, so it is carried over instead of being truncated
# away.
FRONTEND_ENV_FILE="$FRONTEND_DIR/.env.production"

# Read the keys to carry over BEFORE the redirect below truncates the file.
PRESERVED_ENV_LINES=""
if [ -f "$FRONTEND_ENV_FILE" ]; then
    PRESERVED_ENV_LINES=$(grep -E "^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=" "$FRONTEND_ENV_FILE" \
        | grep -vE "^[[:space:]]*(export[[:space:]]+)?(VITE_API_URL|VITE_AUTH_PROVIDER|VITE_ENTRA_TENANT_ID|VITE_ENTRA_TENANT_DOMAIN|VITE_ENTRA_SPA_CLIENT_ID|VITE_ENTRA_SPA_REDIRECT_URI|VITE_ENTRA_SPA_SCOPE)=" \
        || true)
fi

{
    echo "# Generated by deploy-full.sh — do not hand-edit; edit frontend/.env instead."
    echo "VITE_API_URL=${API_ENDPOINT}"
    echo "VITE_AUTH_PROVIDER=entra"
    echo "VITE_ENTRA_TENANT_ID=${ENTRA_TENANT_ID}"
    echo "VITE_ENTRA_TENANT_DOMAIN=${ENTRA_TENANT_DOMAIN}"
    echo "VITE_ENTRA_SPA_CLIENT_ID=${ENTRA_SPA_CLIENT_ID}"
    echo "VITE_ENTRA_SPA_REDIRECT_URI=${ENTRA_SPA_REDIRECT_URI}"
    echo "VITE_ENTRA_SPA_SCOPE=${ENTRA_SPA_SCOPE}"
    if [ -n "$PRESERVED_ENV_LINES" ]; then
        echo
        echo "# Preserved from the previous .env.production (not managed by deploy-full.sh)."
        printf '%s\n' "$PRESERVED_ENV_LINES"
    fi
} > "$FRONTEND_ENV_FILE"

echo "  Wrote ${FRONTEND_ENV_FILE} (7 SPA variables)."

cd "$FRONTEND_DIR"
npm install --silent
npm run build

# Fail here rather than shipping a white page. If msalConfig's throw survived into the
# bundle, the Entra config was missing at build time and MSAL got tree-shaken out — every
# asset would still serve 200, so only the bundle betrays it.
if grep -rqs 'requires VITE_ENTRA_TENANT_ID' "$FRONTEND_DIR/dist/assets/"; then
    echo -e "${RED}  Built bundle carries the MSAL config-missing throw — the app would be a white page.${NC}"
    echo "  Check VITE_ENTRA_* in $FRONTEND_DIR/.env.production and re-run. NOT deploying."
    exit 1
fi

echo -e "${GREEN}  Frontend built.${NC}"
echo

# ============================================================================
# Step 5: Frontend Deploy
# ============================================================================

echo -e "${BLUE}[5/5] Frontend deploy to S3 + CloudFront${NC}"

aws s3 sync "$FRONTEND_DIR/dist/" "s3://$FRONTEND_BUCKET/" --delete --quiet
aws cloudfront create-invalidation \
    --distribution-id "$CLOUDFRONT_ID" \
    --paths "/*" \
    --query 'Invalidation.Status' \
    --output text

echo -e "${GREEN}  Frontend deployed.${NC}"
echo

# ============================================================================
# Summary
# ============================================================================

echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Deployment complete${NC}"
echo -e "${GREEN}============================================${NC}"
echo
echo -e "  Frontend:  ${FRONTEND_URL}"
echo -e "  API:       ${API_ENDPOINT}"
echo -e "  ECR:       ${ECR_REPO}"
echo
