"""Application settings, read from the environment and the dotenv file ``ENV_FILE`` names: unset means
``.env`` in the working directory, empty means none.

``Settings()`` is instantiated per call wherever a test may monkeypatch the environment; the
module-level ``settings`` is the import-time snapshot. Every DynamoDB table and S3 bucket setting
is optional: an empty value selects the in-memory store, so the suite never needs AWS.
"""

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "Control Plane API"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = Field(
        default=False,
        description="Raise the log level to DEBUG whatever LOG_LEVEL says",
    )
    USE_DEV_AUTH: bool = Field(
        default=False,
        description=(
            "Development auth bypass; explicit opt-in so a bare environment validates real Entra tokens."
        ),
    )
    REGISTRY_IN_MEMORY: bool = Field(
        default=False,
        description=(
            "Serve the agent and MCP registries from this process's memory instead of AWS; explicit opt-in "
            "for the local launcher, never set on a stack."
        ),
    )
    GRAPH_IN_MEMORY: bool = Field(
        default=False,
        description=(
            "Serve Microsoft Entra and Graph from this process's memory instead of the directory; explicit "
            "opt-in for the local launcher, never set on a stack."
        ),
    )

    AWS_REGION: str = Field(default="us-east-1")
    GUARDRAILS_TABLE_NAME: str = Field(
        default="", description="DynamoDB table for guardrail templates; empty selects the in-memory store"
    )
    MARKETPLACE_TABLE_NAME: str = Field(
        default="",
        description="DynamoDB table of marketplace subscriptions and listings; empty selects in-memory",
    )
    CONNECTIONS_TABLE_NAME: str = Field(
        default="", description="DynamoDB table for Git org connections; empty selects the in-memory store"
    )
    CONNECTIONS_SECRET_PREFIX: str = Field(
        default="agp-dev/git-connections/",
        description=(
            "Secrets Manager name prefix for per-connection credentials (a PAT or a GitHub App private key); "
            "the secret name is <prefix><connection_id>"
        ),
    )
    RUNTIME_BUILD_TOKEN_PREFIX: str = Field(
        default="agp-dev/runtime-build-token/",
        description=(
            "Secrets Manager name prefix for the short-lived per-build clone token. The runtime-build "
            "service resolves a ready bearer token (a PAT or a minted App installation token), writes "
            '{"token": ...} to <prefix><image_tag> and passes that ARN to CodeBuild, so the buildspec '
            "clones the same way for both connection types."
        ),
    )
    GITHUB_USER_LINK_SECRET_PREFIX: str = Field(
        default="agp-dev/github-user-link/",
        description=(
            "Secrets Manager name prefix for per-user GitHub link tokens; the secret name is "
            '<prefix><link_id>, the body {"access_token", "refresh_token"}'
        ),
    )
    DATABRICKS_TENANT_SECRET_PREFIX: str = Field(
        default="agp-dev/databricks-tenants/",
        description=(
            "Secrets Manager name prefix for a Databricks tenant's credentials: the per-stage workspace "
            'service-principal secret at <prefix><tenant_id>/<stage> (body {"sp_client_secret": ...}) and '
            "the optional account-admin pair at <prefix><tenant_id>/account-admin (body "
            '{"account_admin_client_id", "account_admin_secret"}). The ECS task role needs CreateSecret, '
            "PutSecretValue, GetSecretValue and DeleteSecret on <prefix>*, or every Databricks tenant write "
            "returns secret_error."
        ),
    )
    DATABRICKS_ALLOW_SP_SECRET_BINDING: bool = Field(
        default=False,
        description=(
            "Dormant: allow the per-agent service-principal (sp_secret) Databricks binding for records that "
            "carry it. Never auto-selected; the connect flow only produces federation or invoke_unavailable."
        ),
    )
    PROJECTS_TABLE_NAME: str = Field(
        default="",
        description="DynamoDB table for template-materialized projects; empty selects the in-memory store",
    )
    TENANTS_TABLE_NAME: str = Field(
        default="", description="DynamoDB tenants table; empty selects the in-memory store"
    )
    EVALS_BUCKET_NAME: str = Field(
        default="",
        description=(
            "S3 bucket for eval run records at runs/<repo_id>/<run_id>/run.json|results.jsonl|dataset.jsonl; "
            "empty selects the in-memory store"
        ),
    )
    EVALS_TABLE_NAME: str = Field(
        default="",
        description="DynamoDB index of eval runs and promote overrides; empty selects the in-memory store",
    )
    EVALS_LOG_GROUP: str = Field(
        default="",
        description=(
            "CloudWatch log group the eval scores are written to as EMF (namespace AGP/Evals); empty means "
            "no metrics"
        ),
    )
    CONTAINMENT_TABLE_NAME: str = Field(
        default="",
        description=(
            "DynamoDB table for agent containment state, monitor flags, signals, sessions and leases; empty "
            "selects the in-memory store"
        ),
    )
    AUDIT_TABLE_NAME: str = Field(
        default="",
        description="Append-only DynamoDB table of containment audit events; empty selects the in-memory one",
    )
    STUDIO_MODEL_ID: str = Field(
        default="us.anthropic.claude-sonnet-4-6", description="The Bedrock model the Studio draft calls"
    )
    PROJECT_ECR_PUSH_ROLE_ARN: str = Field(
        default="",
        description=(
            "Default IAM role ARN assumed through GitHub OIDC for ECR push, written as the "
            "AWS_ECR_PUSH_ROLE_ARN repository variable. Used only when a connection has no per-org role ARN."
        ),
    )
    ECR_PUSH_ROLE_NAME_PREFIX: str = Field(
        default="",
        description=(
            "Prefix for per-org ECR-push role names, <prefix>-ecr-push-<org>, sourced from the resource "
            "name_prefix. Each connected org gets its own role (trust sub = repo:<org>/*:*) so a repository "
            "in one org cannot assume another org's push role. Empty leaves per-org provisioning inert and "
            "every repository uses PROJECT_ECR_PUSH_ROLE_ARN."
        ),
    )
    GITHUB_OIDC_PROVIDER_ARN: str = Field(
        default="",
        description=(
            "ARN of the account's token.actions.githubusercontent.com OIDC provider, the Federated principal "
            "in each per-org ECR-push role's trust policy. Terraform passes the deterministic string without "
            "owning the resource: the backend creates the provider on the first GitHub connection. Empty "
            "makes services.github_oidc_provider_service derive it from the STS account id."
        ),
    )
    AGENT_IMAGES_ECR_ARN: str = Field(
        default="",
        description=(
            "ARN of the shared agent-images ECR repository the per-org ECR-push roles may push to; the "
            "Resource in each role's inline policy"
        ),
    )
    PROJECT_ECR_REPOSITORY: str = Field(
        default="",
        description="Target ECR repository URI of the built agent image (repository variable ECR_REPOSITORY)",
    )
    PROJECT_BUILD_DONE_URL: str = Field(
        default="",
        description=(
            "Optional build-ready webhook URL (repository variable PLATFORM_BUILD_DONE_URL); empty omits it"
        ),
    )
    AGP_API_URL: str = Field(
        default="",
        description=(
            "Public AGP API base URL including stage and prefix, written as the AGP_API_URL repository "
            "variable; the scaffold's trigger job POSTs to ${AGP_API_URL}/builds/runtime. Empty omits it and "
            "the trigger job cannot reach the platform."
        ),
    )
    CODEBUILD_PROJECT_NAME: str = Field(
        default="",
        description=(
            "CodeBuild project the build-trigger endpoint calls StartBuild on; sourced from "
            "module.codebuild.project_name"
        ),
    )
    GITHUB_OIDC_ISSUER: str = Field(
        default="https://token.actions.githubusercontent.com",
        description="Expected iss claim of a GitHub Actions OIDC token",
    )
    GITHUB_OIDC_AUDIENCE: str = Field(
        default="agp-runtime-build",
        description=(
            "Expected aud claim of a GitHub Actions OIDC token; the calling workflow must request exactly "
            "this audience through its id-token permission. A fixed AGP audience, not GitHub's per-org "
            "default, so a token minted for any other purpose is rejected."
        ),
    )
    RUNTIME_MODULE_BUCKET: str = Field(
        default="",
        description=(
            "S3 bucket holding the zipped agentcore_runtime Terraform module that terraform apply uploads "
            "and the template rollout pushes into the org as the agp-runtime-infra repository; the module is "
            "not shipped in the backend image. Empty leaves the infra rollout without a source, which raises "
            "RolloutError."
        ),
    )
    RUNTIME_MODULE_KEY: str = Field(default="", description="S3 key of the zipped agentcore_runtime module")
    S3_BUCKET_NAME: str = Field(
        default="", description="S3 bucket for project archives; empty defers to PROJECT_ARCHIVES_BUCKET"
    )
    PROJECT_ARCHIVES_BUCKET: str = Field(default="", description="Project archives S3 bucket")

    AGENT_REGISTRY_ID: str = Field(
        default="",
        description=(
            "Optional override: the AWS Agent Registry registryId. Empty, the default, resolves it from "
            "AGENT_REGISTRY_NAME on first use (core.registry_resolver)."
        ),
    )
    AGENT_REGISTRY_NAME: str = Field(
        default="agp-agents",
        description=(
            "AWS Agent Registry name for agent records, resolved to a registryId at runtime; must match the "
            "agent_registry_name tfvar"
        ),
    )
    AGENT_REGISTRY_REGION: str = Field(
        default="us-east-1", description="Region hosting the agent registry (Preview: not eu-central-1)"
    )

    MCP_REGISTRY_ID: str = Field(
        default="",
        description=(
            "Optional override: the registryId of the MCP-server registry. Empty, the default, resolves it "
            "from MCP_REGISTRY_NAME on first use."
        ),
    )
    MCP_REGISTRY_NAME: str = Field(
        default="agp-mcp-servers",
        description=(
            "AWS Agent Registry name for MCP-server records, resolved to a registryId at runtime; must match "
            "the mcp_registry_name tfvar"
        ),
    )
    MCP_REGISTRY_REGION: str = Field(
        default="us-east-1", description="Region hosting the MCP registry (Preview: not eu-central-1)"
    )
    CEDAR_POLICY_ENGINE_PREFIX: str = Field(
        default="agp-cedar-", description="Name prefix for per-gateway AgentCore Cedar Policy Engines"
    )

    JWT_LEEWAY_SECONDS: int = Field(
        default=60,
        description=(
            "Clock-skew tolerance both inbound JWT validators (Entra user tokens and GitHub Actions OIDC "
            "tokens) pass to PyJWT. PyJWT applies leeway to exp, nbf and iat alike, so 60 accepts a token up "
            "to 60 s past its expiry; 0 restores zero tolerance at the cost of blanket 401s from a skewed "
            "container clock."
        ),
    )

    AUTH_PROVIDER: str = Field(
        default="entra",
        description=(
            "Auth provider; Entra is the only one. Retained so the per-call == 'entra' guards keep their "
            "meaning; the dev-auth bypass is selected by USE_DEV_AUTH in the local shape, not by this value."
        ),
    )
    ENTRA_TENANT_ID: str = Field(
        default="",
        description="Microsoft Entra tenant ID (GUID); builds the OIDC discovery URL and the JWKS endpoint",
    )
    ENTRA_AUDIENCE: str = Field(
        default="api://agp",
        description="Expected aud claim in inbound user JWTs: the identifier URI the SPA app exposes",
    )
    ENTRA_SPA_CLIENT_ID: str = Field(
        default="",
        description=(
            "SPA app registration client ID (GUID), accepted as an alternative aud value. Entra's v2.0 "
            "access tokens for the SPA's own exposed scope sometimes carry aud=<client-id GUID> instead of "
            "the identifier URI, so both forms are accepted. Optional; blank accepts only ENTRA_AUDIENCE."
        ),
    )
    ENTRA_BACKEND_CLIENT_ID: str = Field(
        default="",
        description="Confidential Entra app registration client_id used for outbound Microsoft Graph calls",
    )
    ENTRA_BACKEND_CLIENT_SECRET: str = Field(
        default="",
        description=(
            "Confidential client secret. In local dev, set it in backend/.env (see backend/.env.example). In "
            "cloud, leave it out of any file: the ECS task definition injects it from Secrets Manager "
            "through its secrets/valueFrom block, sourced from entra_backend_client_secret in "
            "infrastructure/secrets.auto.tfvars."
        ),
    )
    ENTRA_BACKEND_CLIENT_SECRET_ARN: str = Field(
        default="",
        description=(
            "ARN of the Secrets Manager secret holding ENTRA_BACKEND_CLIENT_SECRET, for a runtime that must "
            "fetch the secret itself. The Terraform tree does not set it, because ECS injects the value "
            "directly. Ignored when ENTRA_BACKEND_CLIENT_SECRET is set."
        ),
    )

    ENTRA_ROLE_ADMIN: str = Field(
        default="Platform.Admin",
        description=(
            "App role value in the inbound token's roles claim that maps to Role.ADMIN. The roles are "
            "defined on the backend app registration, the token's audience, and assigned on its service "
            "principal."
        ),
    )
    ENTRA_ROLE_OPERATOR: str = Field(
        default="Platform.Operator", description="App role value that maps to Role.OPERATOR"
    )
    ENTRA_ROLE_VIEWER: str = Field(
        default="Platform.Viewer", description="App role value that maps to Role.VIEWER"
    )

    ENTRA_PLATFORM_APP_CLIENT_ID: str = Field(
        default="",
        description=(
            "Client id (GUID) of the app registration whose service principal holds the Platform.* role "
            "assignments the Users admin tab manages. Blank selects ENTRA_BACKEND_CLIENT_ID (the "
            "token-audience app), then ENTRA_SPA_CLIENT_ID as a last resort."
        ),
    )

    ENTRA_LOGIN_BASE: str = Field(
        default="https://login.microsoftonline.com",
        description="Entra OAuth2 authority base; overridable for tests and sovereign clouds",
    )
    GRAPH_API_BASE: str = Field(
        default="https://graph.microsoft.com/v1.0",
        description="Microsoft Graph base URL; overridable for tests and sovereign clouds",
    )
    AGENT_APP_AUDIENCE_PREFIX: str = Field(
        default="api://agp-agent-",
        description="Identifier-URI prefix for per-agent Entra app registrations; the agent id is appended",
    )
    MCP_APP_AUDIENCE_PREFIX: str = Field(
        default="api://agp-mcp-", description="Identifier-URI prefix for per-MCP Entra app registrations"
    )
    AGENT_CRED_PROVIDER_PREFIX: str = Field(
        default="agp-agent-obo-",
        description="Name prefix for per-agent AgentCore OAuth2 credential providers",
    )

    API_PREFIX: str = "/api/v1"
    ROOT_PATH: str = Field(default="", description="Root path of the API, e.g. /dev for an API Gateway stage")
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://localhost:5174", "http://localhost:3000"]

    CONTROL_PLANE_VPC_ID: str = Field(
        default="", description="Control plane VPC ID for foundation stack reuse"
    )

    LANGFUSE_HOST: str = Field(
        default="",
        description="Langfuse base URL (e.g. https://<cloudfront>); empty means Langfuse is not configured",
    )
    LANGFUSE_ADMIN_SECRET_NAME: str = Field(
        default="", description="Secrets Manager name of the seed-org admin login the provisioner uses"
    )

    AGENT_TEMPLATES_DIR: str = Field(
        default="",
        description=(
            "Agent-template scaffold directory; empty resolves to ../agent-templates beside the backend tree"
        ),
    )

    LOG_LEVEL: str = Field(default="INFO", description="Root log level; DEBUG on resolves it to DEBUG")

    def aws_selectors(self) -> list[str]:
        """The settings selecting AWS or the directory over a double: a ``*_TABLE_NAME``, ``*_BUCKET_NAME``,
        ``*_BUCKET`` or ``*_LOG_GROUP`` that is set, and an ``*_IN_MEMORY`` flag that is off."""
        selectors = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if name.endswith(("_TABLE_NAME", "_BUCKET_NAME", "_BUCKET", "_LOG_GROUP")) and value:
                selectors.append(name)
            elif name.endswith("_IN_MEMORY") and not value:
                selectors.append(name)
        return selectors

    @property
    def dev_auth_honoured(self) -> bool:
        """Dev-auth headers stand in for a token only with ``USE_DEV_AUTH`` on and every store a double."""
        return self.USE_DEV_AUTH and not self.aws_selectors()

    class Config:
        env_file = os.environ.get("ENV_FILE", ".env") or None
        case_sensitive = True
        extra = "ignore"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if self.DEBUG:
            self.LOG_LEVEL = "DEBUG"
        if not self.AGENT_TEMPLATES_DIR:
            backend_dir = Path(__file__).parent.parent.parent
            self.AGENT_TEMPLATES_DIR = str((backend_dir.parent / "agent-templates").resolve())


settings = Settings()
