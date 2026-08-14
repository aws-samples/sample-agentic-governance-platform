#!/bin/bash

# Quick frontend redeploy for cloud debugging.
# Builds the UI as-is (using whatever env files are present) and syncs to
# S3 + CloudFront. Does NOT generate any env files — edit frontend/.env*
# yourself before running. Assumes infrastructure already exists.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FRONTEND_DIR="$(cd "$SCRIPT_DIR/../../frontend" && pwd)"

cd "$INFRA_DIR"
FRONTEND_BUCKET=$(terraform output -raw frontend_bucket_name)
CLOUDFRONT_ID=$(terraform output -raw cloudfront_distribution_id)
FRONTEND_URL=$(terraform output -raw frontend_url)

echo "Building frontend..."
cd "$FRONTEND_DIR"
npm run build

echo "Syncing to s3://$FRONTEND_BUCKET ..."
aws s3 sync dist/ "s3://$FRONTEND_BUCKET/" --delete --quiet

echo "Invalidating CloudFront $CLOUDFRONT_ID ..."
aws cloudfront create-invalidation \
    --distribution-id "$CLOUDFRONT_ID" \
    --paths "/*" \
    --query 'Invalidation.Status' \
    --output text

echo "Done: $FRONTEND_URL"
