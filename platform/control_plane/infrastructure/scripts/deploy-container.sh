#!/bin/bash

# Quick backend container redeploy for cloud debugging.
# Builds the backend Docker image, pushes it to ECR, and forces a new ECS
# deployment so the service picks up the new image. Does NOT touch
# infrastructure — assumes Terraform has already provisioned ECR + ECS.
# Prerequisites: AWS credentials exported, Docker running.

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
INFRA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/platform/control_plane/backend"

# --finch forces the finch container engine (no Docker Desktop). See container-engine.sh.
USE_FINCH=0
for arg in "$@"; do
    case "$arg" in
        --finch) USE_FINCH=1 ;;
    esac
done
source "$SCRIPT_DIR/container-engine.sh"

echo -e "${BLUE}============================================${NC}"
echo -e "${BLUE}  Control Plane Container Deployment${NC}"
echo -e "${BLUE}============================================${NC}"
echo

# ============================================================================
# Preflight Checks
# ============================================================================

if ! aws sts get-caller-identity &> /dev/null; then
    echo -e "${RED}Error: AWS credentials not configured. Export your credentials and try again.${NC}"
    exit 1
fi

if ! resolve_container_engine; then
    exit 1
fi

# ============================================================================
# Read existing infrastructure outputs
# ============================================================================

cd "$INFRA_DIR"
ECR_REPO=$(terraform output -raw ecr_repository_url)
ECS_CLUSTER=$(terraform output -raw ecs_cluster_name)
ECS_SERVICE=$(terraform output -raw ecs_service_name)

# Region is authoritative from the ECR repo URL
AWS_REGION=$(echo "$ECR_REPO" | sed 's/.*\.ecr\.\(.*\)\.amazonaws\.com.*/\1/')
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# ============================================================================
# Step 1: Backend Docker Image
# ============================================================================

echo -e "${BLUE}[1/2] Backend Docker image${NC}"

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
# Step 2: ECS Deployment
# ============================================================================

echo -e "${BLUE}[2/2] ECS rolling deployment${NC}"

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

echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Container deployment complete${NC}"
echo -e "${GREEN}============================================${NC}"
echo
echo -e "  ECR:      ${ECR_REPO}"
echo -e "  Cluster:  ${ECS_CLUSTER}"
echo -e "  Service:  ${ECS_SERVICE}"
echo
