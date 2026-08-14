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