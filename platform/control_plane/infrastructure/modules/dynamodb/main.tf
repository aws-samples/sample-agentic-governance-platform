# ============================================================================
# App Factory Submissions Table
# ============================================================================

resource "aws_dynamodb_table" "app_factory" {
  name         = "${var.name_prefix}-app-factory"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-app-factory"
  })
}

# ============================================================================
# Application Catalog Table
# ============================================================================

resource "aws_dynamodb_table" "application_catalog" {
  name         = "${var.name_prefix}-application-catalog"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "application_id"
  range_key    = "version"

  attribute {
    name = "application_id"
    type = "S"
  }

  attribute {
    name = "version"
    type = "S"
  }

  attribute {
    name = "template_id"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  # GSI for querying by template
  global_secondary_index {
    name            = "TemplateIndex"
    projection_type = "ALL"

    key_schema {
      attribute_name = "template_id"
      key_type       = "HASH"
    }

    key_schema {
      attribute_name = "created_at"
      key_type       = "RANGE"
    }
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-application-catalog"
  })
}

# ============================================================================
# Deployment Metadata Table
# ============================================================================

resource "aws_dynamodb_table" "deployment_metadata" {
  name         = "${var.name_prefix}-deployment-metadata"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "deployment_id"
  range_key    = "timestamp"

  attribute {
    name = "deployment_id"
    type = "S"
  }

  attribute {
    name = "timestamp"
    type = "S"
  }

  attribute {
    name = "application_id"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  # GSI for querying deployments by application
  global_secondary_index {
    name            = "ApplicationIndex"
    projection_type = "ALL"

    key_schema {
      attribute_name = "application_id"
      key_type       = "HASH"
    }

    key_schema {
      attribute_name = "timestamp"
      key_type       = "RANGE"
    }
  }

  # GSI for querying deployments by status
  global_secondary_index {
    name            = "StatusIndex"
    projection_type = "ALL"

    key_schema {
      attribute_name = "status"
      key_type       = "HASH"
    }

    key_schema {
      attribute_name = "timestamp"
      key_type       = "RANGE"
    }
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-deployment-metadata"
  })
}

# ============================================================================
# Guardrails Table
# ============================================================================

resource "aws_dynamodb_table" "guardrails" {
  name         = "${var.name_prefix}-guardrails"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  global_secondary_index {
    name            = "status-index"
    projection_type = "ALL"

    key_schema {
      attribute_name = "status"
      key_type       = "HASH"
    }
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-guardrails"
  })
}

# ============================================================================
# Marketplace Table — subscriptions + listings for the Marketplace (E9)
# ============================================================================

resource "aws_dynamodb_table" "marketplace" {
  name         = "${var.name_prefix}-marketplace"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-marketplace"
  })
}

# ============================================================================
# Connections Table — Git org connections + per-connection secrets refs (E19)
# ============================================================================

resource "aws_dynamodb_table" "connections" {
  name         = "${var.name_prefix}-connections"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-connections"
  })
}

# ============================================================================
# Projects Table — template-materialized projects + repositories (E20/T7)
# Single table: project + repository partitions (pk/sk).
# ============================================================================

resource "aws_dynamodb_table" "projects" {
  name         = "${var.name_prefix}-projects"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  # E21/T6: resolve a repository row from its agent_id (buildspec cicd_status
  # write-back). KEYS_ONLY returns pk+sk — enough to build the update key.
  attribute {
    name = "agent_id"
    type = "S"
  }

  global_secondary_index {
    name            = "agent_id-index"
    projection_type = "KEYS_ONLY"

    key_schema {
      attribute_name = "agent_id"
      key_type       = "HASH"
    }
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-projects"
  })
}
# ============================================================================
# Tenants Table — tenant records for multi-tenancy (E24)
# ============================================================================

resource "aws_dynamodb_table" "tenants" {
  name         = "${var.name_prefix}-tenants"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-tenants"
  })
}

# ============================================================================
# Evals Table — eval run + promote-override index per repo (E42)
# pk = REPO#<repo_id>, sk = RUN#<iso>#<run_id> | OVERRIDE#<iso>#<digest>.
# Full run records live in the evals S3 bucket.
# ============================================================================

resource "aws_dynamodb_table" "evals" {
  name         = "${var.name_prefix}-evals"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-evals"
  })
}

# ============================================================================
# Containment Table — kill-switch state, monitor, signals, sessions (E41)
# pk = AGENT#<id>, sk = STATE | MONITOR | SIGNAL#<iso>#<span_id> | SESSION#<sid>;
# pk = MONITORED / LEASE for the monitor index + leader lease. Mutable.
# ============================================================================

resource "aws_dynamodb_table" "containment" {
  name                        = "${var.name_prefix}-containment"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "pk"
  range_key                   = "sk"
  deletion_protection_enabled = !var.ephemeral

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-containment"
  })
}

# ============================================================================
# Audit Table — append-only containment audit trail (E41)
# pk = AGENT#<id>, sk = EVENT#<iso>#<uuid>. The ECS task may only Put/Get/Query
# (explicit Deny on every mutation — see ecs module `ecs_task_audit`).
# ============================================================================

resource "aws_dynamodb_table" "audit" {
  name                        = "${var.name_prefix}-audit"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "pk"
  range_key                   = "sk"
  deletion_protection_enabled = !var.ephemeral

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-audit"
  })
}

# Resource-side Deny: binds EVERY principal (incl. roles the task role can assume or
# mint), unlike the task-role Deny in the ecs module. Changing this policy is itself a
# CloudTrail management event. Deny-only — access is still granted by identity policies.
resource "aws_dynamodb_resource_policy" "audit" {
  resource_arn = aws_dynamodb_table.audit.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AuditAppendOnly"
        Effect    = "Deny"
        Principal = "*"
        Action = [
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:BatchWriteItem",
          "dynamodb:PartiQLUpdate",
          "dynamodb:PartiQLDelete"
        ]
        Resource = aws_dynamodb_table.audit.arn
      }
    ]
  })
}
