/**
 * Secrets Manager — Agentic Governance Platform
 *
 * Stores the confidential client secret for the platform's "Backend Graph Client"
 * Entra app registration. The FastAPI backend fetches this at runtime via boto3
 * from inside ECS (task role grants secretsmanager:GetSecretValue on this ARN).
 *
 * For local dev the same secret is set as ENTRA_BACKEND_CLIENT_SECRET in
 * `backend/.env` (gitignored; see `backend/.env.example`) and read directly
 * without hitting AWS.
 *
 * Provisioning: `var.secret_value` comes from `entra_backend_client_secret` in
 * `infrastructure/secrets.auto.tfvars` (gitignored; see
 * `secrets.auto.tfvars.example`). The value is NEVER committed to git. To
 * rotate, generate a new secret in Entra, update that file, and re-apply.
 *
 * See: docs/entra-setup.md
 */

resource "aws_secretsmanager_secret" "graph_client_secret" {
  name        = "${var.name_prefix}/graph-client-secret"
  description = "Client secret for the AGP backend Graph client app registration. Used by FastAPI to authenticate to Microsoft Graph via OAuth client credentials."

  tags = merge(var.tags, {
    project    = "agp"
    component  = "backend-graph-client"
    rotated_at = "manual"
  })
}

resource "aws_secretsmanager_secret_version" "graph_client_secret" {
  secret_id     = aws_secretsmanager_secret.graph_client_secret.id
  secret_string = var.secret_value
}
