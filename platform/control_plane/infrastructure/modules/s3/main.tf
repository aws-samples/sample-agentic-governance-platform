# ============================================================================
# Project Archives Bucket
# ============================================================================

resource "aws_s3_bucket" "project_archives" {
  bucket        = "${var.name_prefix}-project-archives"
  force_destroy = var.ephemeral

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-project-archives"
  })
}

resource "aws_s3_bucket_versioning" "project_archives" {
  bucket = aws_s3_bucket.project_archives.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "project_archives" {
  bucket = aws_s3_bucket.project_archives.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "project_archives" {
  bucket = aws_s3_bucket.project_archives.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "project_archives" {
  bucket = aws_s3_bucket.project_archives.id

  rule {
    id     = "expire-old-archives"
    status = "Enabled"

    filter {}

    expiration {
      days = var.project_archive_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }
}

resource "aws_s3_bucket_cors_configuration" "project_archives" {
  bucket = aws_s3_bucket.project_archives.id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["GET", "PUT", "POST"]
    allowed_origins = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}

# ============================================================================
# Evals Bucket — eval run records: runs/<repo_id>/<run_id>/{run.json,
# results.jsonl,dataset.jsonl} (E42). Durable audit evidence, so no expiry.
# ============================================================================

resource "aws_s3_bucket" "evals" {
  bucket        = "${var.name_prefix}-evals"
  force_destroy = var.ephemeral

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-evals"
  })
}

resource "aws_s3_bucket_versioning" "evals" {
  bucket = aws_s3_bucket.evals.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "evals" {
  bucket = aws_s3_bucket.evals.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "evals" {
  bucket = aws_s3_bucket.evals.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ============================================================================
# Frontend Hosting Bucket
# ============================================================================

resource "aws_s3_bucket" "frontend" {
  bucket        = "${var.name_prefix}-frontend"
  force_destroy = var.ephemeral

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-frontend"
  })
}

resource "aws_s3_bucket_versioning" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# NOTE: the frontend bucket policy is owned by the cloudfront module
# (aws_s3_bucket_policy.frontend_cloudfront) — it has the AWS:SourceArn
# condition scoping access to this distribution. A bucket allows only one
# policy, so defining one here too caused a perpetual apply/clobber diff.
