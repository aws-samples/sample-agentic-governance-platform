"""
Configuration settings for Control Plane backend
"""

from pydantic_settings import BaseSettings
from pydantic import Field
from pathlib import Path


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""

    # Application
    APP_NAME: str = "Control Plane API"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    USE_DEV_AUTH: bool = Field(default=False, description="Use development auth bypass (explicit opt-in; defaults off so a bare env uses real Entra validation)")

    # Database
    DATABASE_URL: str = Field(
        default="sqlite:///./control_plane.db",
        description="PostgreSQL connection string"
    )
    DATABASE_POOL_SIZE: int = 5
    DATABASE_MAX_OVERFLOW: int = 10

    # AWS
    AWS_REGION: str = Field(default="us-east-1")
    GUARDRAILS_TABLE_NAME: str = Field(default="", description="DynamoDB table for guardrail templates; empty ⇒ in-memory fallback")
    MARKETPLACE_TABLE_NAME: str = Field(default="", description="DynamoDB table for marketplace subscriptions + listings (E9); empty ⇒ in-memory fallback")
    CONNECTIONS_TABLE_NAME: str = Field(default="", description="DynamoDB table for Git org connections (E19); empty ⇒ in-memory fallback")
    CONNECTIONS_SECRET_PREFIX: str = Field(default="agp-dev/git-connections/", description="Secrets Manager name prefix for per-connection credential secrets (E19 PAT / E20 GitHub App private key); secret name = <prefix><connection_id>")
    RUNTIME_BUILD_TOKEN_PREFIX: str = Field(default="agp-dev/runtime-build-token/", description="Secrets Manager name prefix for short-lived per-build scratch clone tokens (E22/T6b); the runtime-build service resolves a ready bearer token (PAT or minted App installation token), writes {\"token\": ...} to <prefix><image_tag>, and passes that scratch ARN to CodeBuild so the buildspec's uniform jq .token clone works for both connection types")
    GITHUB_USER_LINK_SECRET_PREFIX: str = Field(default="agp-dev/github-user-link/", description="Secrets Manager name prefix for per-user GitHub link tokens (E27B); secret name = <prefix><link_id>, body {\"access_token\", \"refresh_token\"}")
    DATABRICKS_TENANT_SECRET_PREFIX: str = Field(default="agp-dev/databricks-tenants/", description="Secrets Manager name prefix for a Databricks tenant's credentials (E29/T3); per-stage workspace SP secret = <prefix><tenant_id>/<stage> body {\"sp_client_secret\": ...}, optional account-admin pair = <prefix><tenant_id>/account-admin body {\"account_admin_client_id\", \"account_admin_secret\"}. The ECS task role needs CreateSecret/PutSecretValue/GetSecretValue/DeleteSecret on <prefix>* or every Databricks tenant write returns secret_error")
    DATABRICKS_ALLOW_SP_SECRET_BINDING: bool = Field(default=False, description="Dormant: allow the per-agent service-principal (sp_secret) Databricks binding for records that deliberately carry it. Never auto-selected — the connect flow only produces federation | invoke_unavailable. See design §3B.")
    PROJECTS_TABLE_NAME: str = Field(default="", description="DynamoDB table for template-materialized projects (E20/T7); empty ⇒ in-memory fallback")
    TENANTS_TABLE_NAME: str = Field(default="", description="DynamoDB tenants table (E24); empty ⇒ in-memory fallback")
    # Build-only CI repo variables written onto a materialized project repo (E20/T7).
    # These are GitHub Actions `vars` the scaffold's build.yml reads; empty values are skipped.
    PROJECT_ECR_PUSH_ROLE_ARN: str = Field(default="", description="Fallback IAM role ARN assumed via GitHub OIDC for ECR push (E20/T7 repo var AWS_ECR_PUSH_ROLE_ARN). Used only when a connection has no per-org role ARN (E22 multi-org: each org gets its own role, resolved at materialize time).")
    # Per-org GitHub-OIDC ECR-push role provisioning (E22 multi-org bugfix). Each connected
    # org gets its own role (trust sub = repo:<org>/*:*) so a repo in org A cannot assume
    # org B's push role. All three must be set for provisioning to run (else inert).
    ECR_PUSH_ROLE_NAME_PREFIX: str = Field(default="", description="Prefix for per-org ECR-push role names → <prefix>-ecr-push-<org> (E22). Sourced from the resource name_prefix. Empty ⇒ per-org role provisioning is inert (falls back to PROJECT_ECR_PUSH_ROLE_ARN).")
    GITHUB_OIDC_PROVIDER_ARN: str = Field(default="", description="ARN of the account's token.actions.githubusercontent.com OIDC provider; the Federated principal in each per-org ECR-push role's trust policy (E22). Terraform passes the DETERMINISTIC string (it owns no provider resource — Git-provider integrations are a platform capability, so the backend creates the provider on the first GitHub connection). Empty ⇒ services.github_oidc_provider_service.resolve_github_oidc_provider_arn derives it from the STS account id.")
    AGENT_IMAGES_ECR_ARN: str = Field(default="", description="ARN of the shared agent-images ECR repo the per-org ECR-push roles may push to (E22); Resource in the role's inline policy.")
    PROJECT_ECR_REPOSITORY: str = Field(default="", description="Target ECR repo URI for the built agent image (E20/T7 repo var ECR_REPOSITORY)")
    PROJECT_BUILD_DONE_URL: str = Field(default="", description="Optional platform build-ready webhook URL (E20/T7 repo var PLATFORM_BUILD_DONE_URL); empty ⇒ omitted")
    AGP_API_URL: str = Field(default="", description="Public AGP API base URL (incl. stage+prefix, e.g. https://<id>.execute-api.<region>.amazonaws.com/dev/api/v1) written as the AGP_API_URL repo var; the scaffold build.yml trigger job POSTs to `${AGP_API_URL}/builds/runtime`. Empty ⇒ omitted (trigger job cannot reach the platform).")
    # Build-trigger endpoint (E22/T6): a GitHub Action authenticates via GitHub OIDC and
    # asks AGP to StartBuild the runtime-provision CodeBuild project.
    CODEBUILD_PROJECT_NAME: str = Field(default="", description="CodeBuild project the build-trigger endpoint calls StartBuild on (E22/T6); sourced from module.codebuild.project_name")
    GITHUB_OIDC_ISSUER: str = Field(default="https://token.actions.githubusercontent.com", description="Expected iss claim for GitHub Actions OIDC tokens (E22/T6)")
    GITHUB_OIDC_AUDIENCE: str = Field(default="agp-runtime-build", description="Expected aud claim for GitHub Actions OIDC tokens; the calling GitHub Action MUST request this exact audience via its id-token permission (E22/T6). A fixed AGP audience (not the github.com per-org default) so a token minted for any other purpose is rejected.")
    # Runtime-infra module archive (E22 bugfix-A): the agentcore_runtime Terraform module,
    # zipped + uploaded to S3 by `terraform apply` (runtime_module.tf). The template rollout
    # DOWNLOADS this zip and pushes it into the org as the forced agp-runtime-infra repo —
    # the module is NOT shipped in the backend image, so S3 is the transport. Empty ⇒ the
    # infra rollout has no source and raises a SAFE RolloutError.
    RUNTIME_MODULE_BUCKET: str = Field(default="", description="S3 bucket holding the zipped agentcore_runtime module the rollout pushes as agp-runtime-infra (E22 bugfix-A); empty ⇒ infra rollout unavailable")
    RUNTIME_MODULE_KEY: str = Field(default="", description="S3 key of the zipped agentcore_runtime module (E22 bugfix-A)")
    S3_BUCKET_NAME: str = Field(
        default="",
        description="S3 bucket for project archives (falls back to PROJECT_ARCHIVES_BUCKET)"
    )
    PROJECT_ARCHIVES_BUCKET: str = Field(default="", description="Project archives S3 bucket")

    # === AWS Agent Registry (`agent-registry` namespace) ===
    # THE NAME IS THE OPERATIONAL SETTING; THE ID IS AN OPTIONAL OVERRIDE.
    #
    # AWS mints the registryId — `RegistryIdentifier` accepts an ARN or a generated 12-16 char
    # id, never a name — and there is no Terraform resource for this namespace, so the id could
    # only reach us through a capture file Terraform read during its PLAN walk, before the
    # provisioner that writes it had run. That is what used to make a from-zero deploy need
    # `terraform apply` twice. The registry services now resolve NAME -> id on first use and
    # memoise it (see `core.registry_resolver`), so Terraform passes only the names and a single
    # apply produces a fully wired container. `*_REGISTRY_ID` is still honoured as an explicit
    # override that short-circuits resolution (six operational scripts pass ids directly) —
    # EMPTY, the default, means "resolve by name" and is the normal state.
    AGENT_REGISTRY_ID: str = Field(default="", description="OPTIONAL override: AWS Agent Registry registryId. Empty (default) => resolved from AGENT_REGISTRY_NAME on first use.")
    AGENT_REGISTRY_NAME: str = Field(default="agp-agents", description="AWS Agent Registry name for agent records — the identifier the backend resolves to a registryId at runtime. Must match the agent_registry_name tfvar.")
    AGENT_REGISTRY_REGION: str = Field(default="us-east-1", description="Region hosting the agent registry (Preview: not eu-central-1)")

    # === MCP Server Registry (a second registry in the same namespace) ===
    MCP_REGISTRY_ID: str = Field(default="", description="OPTIONAL override: registryId of the MCP-server registry. Empty (default) => resolved from MCP_REGISTRY_NAME on first use.")
    MCP_REGISTRY_NAME: str = Field(default="agp-mcp-servers", description="AWS Agent Registry name for MCP-server records — resolved to a registryId at runtime. Must match the mcp_registry_name tfvar.")
    MCP_REGISTRY_REGION: str = Field(default="us-east-1", description="Region hosting the MCP registry (Preview: not eu-central-1)")
    CEDAR_POLICY_ENGINE_PREFIX: str = Field(default="agp-cedar-", description="Name prefix for per-gateway AgentCore Cedar Policy Engines (E8)")

    # === Inbound JWT validation (shared) ===
    # ONE knob for one physical phenomenon (container clock drift), applied by BOTH
    # inbound JWT validators: core/security_entra.py (user logins) and
    # core/security_github_oidc.py (GitHub Actions build-trigger tokens). Two knobs for
    # one cause would be drift waiting to happen.
    # Honesty note: PyJWT's `leeway` loosens `exp` AND `nbf`/`iat`, so 60 s means a token
    # is accepted for up to 60 s past its expiry (and up to 60 s before it becomes valid).
    # That is a security-relevant loosening, traded deliberately against the blanket-401
    # outage a skewed container clock causes at zero tolerance. Set to 0 to restore it.
    JWT_LEEWAY_SECONDS: int = 60

    # === Microsoft Entra ID ===
    # Entra is the sole real auth provider. The setting is retained (default
    # 'entra') so the per-call `== "entra"` guards elsewhere stay valid without
    # a wider strip; a local dev-auth bypass is selected via USE_DEV_AUTH/DEBUG,
    # independently of this value.
    AUTH_PROVIDER: str = Field(
        default="entra",
        description="Auth provider (Entra only). Retained for compatibility; defaults to 'entra'.",
    )

    # Entra tenant identity — used to build the OIDC discovery URL and JWKS endpoint.
    ENTRA_TENANT_ID: str = Field(default="", description="Microsoft Entra tenant ID (GUID)")

    # The audience the SPA app exposes; backend validates inbound user JWTs against this.
    ENTRA_AUDIENCE: str = Field(
        default="api://agp",
        description="Expected `aud` claim in inbound user JWTs",
    )

    # SPA app reg client ID. Entra's v2.0 access tokens for the SPA's own
    # exposed-API scope sometimes carry aud=<client-id-GUID> instead of the
    # configured identifier URI — this is a known Microsoft inconsistency.
    # Backend accepts both forms so MSAL works without a quirk-specific config
    # in the frontend. Optional; if blank only ENTRA_AUDIENCE is accepted.
    ENTRA_SPA_CLIENT_ID: str = Field(
        default="",
        description=(
            "SPA app reg client ID (GUID). Accepted as alternative aud value "
            "in inbound JWT validation. Optional."
        ),
    )

    # Backend's confidential app reg — for outbound Microsoft Graph calls in E5+.
    ENTRA_BACKEND_CLIENT_ID: str = Field(
        default="",
        description="Confidential Entra app reg client_id used to call Microsoft Graph",
    )
    ENTRA_BACKEND_CLIENT_SECRET: str = Field(
        default="",
        description=(
            "Confidential client secret. In local dev, set it in backend/.env "
            "(see backend/.env.example). In cloud, leave it out of any file: the ECS "
            "task definition injects it natively from Secrets Manager via its "
            "`secrets`/`valueFrom` block, sourced from `entra_backend_client_secret` "
            "in infrastructure/secrets.auto.tfvars."
        ),
    )
    ENTRA_BACKEND_CLIENT_SECRET_ARN: str = Field(
        default="",
        description=(
            "ARN of the Secrets Manager secret holding ENTRA_BACKEND_CLIENT_SECRET. "
            "Optional fallback for a runtime that must fetch the secret itself: the "
            "Terraform tree does not set it, because ECS injects the secret value "
            "directly. Ignored if ENTRA_BACKEND_CLIENT_SECRET is set."
        ),
    )

    # The platform app role names defined on the BACKEND app reg — the app the
    # inbound token's `aud` identifies (E1/S1.2 step 5). Entra sources the inbound
    # JWT's `roles` claim from the app-role assignments on the token-audience
    # (backend) app's SP, and the backend matches those values here to pick a Role
    # enum value.
    ENTRA_ROLE_ADMIN: str = Field(default="Platform.Admin")
    ENTRA_ROLE_OPERATOR: str = Field(default="Platform.Operator")
    ENTRA_ROLE_VIEWER: str = Field(default="Platform.Viewer")

    # The app reg whose service principal carries the Platform.* app-role
    # ASSIGNMENTS managed by the Users admin tab (E16). The inbound token's `aud` is
    # the BACKEND app, and Entra sources the `roles` claim from the assignments on the
    # token-audience (backend) app's SP — so assignments must live on the backend app.
    # This defaults to the backend client id and falls back to the SPA client id as a
    # last resort. Override in cloud only if the roles live on a different app reg.
    ENTRA_PLATFORM_APP_CLIENT_ID: str = Field(
        default="",
        description="Client id (GUID) of the app reg whose SP holds Platform.* role assignments. Blank ⇒ ENTRA_BACKEND_CLIENT_ID (the token-audience app whose SP holds Platform.* assignments), then ENTRA_SPA_CLIENT_ID as a last resort.",
    )

    # Entra OBO / Microsoft Graph (E6) — outbound. Bases are overridable for tests/sovereign clouds.
    ENTRA_LOGIN_BASE: str = Field(default="https://login.microsoftonline.com", description="Entra OAuth2 authority base")
    GRAPH_API_BASE: str = Field(default="https://graph.microsoft.com/v1.0", description="Microsoft Graph base URL")
    AGENT_APP_AUDIENCE_PREFIX: str = Field(default="api://agp-agent-", description="Identifier-URI prefix for per-agent Entra app regs; <id> is appended")
    MCP_APP_AUDIENCE_PREFIX: str = Field(default="api://agp-mcp-", description="Identifier-URI prefix for per-MCP Entra app regs")
    AGENT_CRED_PROVIDER_PREFIX: str = Field(default="agp-agent-obo-", description="Name prefix for per-agent AgentCore OAuth2 credential providers (T-CRED-PROVIDER)")

    # API
    API_PREFIX: str = "/api/v1"
    ROOT_PATH: str = Field(default="", description="Root path for API (e.g., /dev for API Gateway stage)")
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://localhost:5174", "http://localhost:3000"]

    # Infrastructure
    CONTROL_PLANE_VPC_ID: str = Field(default="", description="Control plane VPC ID for foundation stack reuse")

    # === Langfuse observability (E26) ===
    LANGFUSE_HOST: str = Field(default="", description="Langfuse base URL (e.g. https://<cloudfront>); empty ⇒ not configured (configured = bool(LANGFUSE_HOST))")
    LANGFUSE_ADMIN_SECRET_NAME: str = Field(default="", description="Secrets Manager name for the seed-org/admin login the provisioner uses")

    # Templates
    AGENT_TEMPLATES_DIR: str = Field(default="", description="Agent-template scaffold dir (E20); empty ⇒ resolve to ../agent-templates relative to backend")

    # Logging
    LOG_LEVEL: str = "INFO"

    class Config:
        env_file = ".env"
        case_sensitive = True
        # `.env` is the single canonical local file (see env_file above), and it is
        # shared: allow it to carry env vars belonging to sibling tools (frontend
        # Vite, infra scripts, etc.) without breaking backend startup.
        # Each stack declares only the vars it cares about.
        extra = "ignore"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Resolve the agent-template scaffold dir (E20). Sibling of backend/ →
        # control_plane/agent-templates. Overridable via AGENT_TEMPLATES_DIR env.
        if not self.AGENT_TEMPLATES_DIR:
            backend_dir = Path(__file__).parent.parent.parent
            self.AGENT_TEMPLATES_DIR = str((backend_dir.parent / "agent-templates").resolve())


# Global settings instance
settings = Settings()
