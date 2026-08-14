"""Environment-variable configuration for the agent (defaults match repo idiom)."""
import os


class Config:
    def __init__(self):
        self.MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-6")
        self.AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
        # Langfuse observability (E26/T10). The platform provisions one Langfuse project + key
        # PER AGENT and stores the {public_key, secret_key} pair in Secrets Manager; the
        # runtime-provisioning injects the NON-SECRET host + secret NAME here (never the key
        # values). The agent reads the pair from Secrets Manager at import — NEVER a hardcoded
        # literal. Empty (unconfigured) ⇒ telemetry is simply disabled and the agent still runs.
        self.LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "")
        self.LANGFUSE_SECRET_NAME = os.environ.get("LANGFUSE_SECRET_NAME", "")
        # MCP wiring. The platform injects these when an MCP grant is approved for this agent;
        # ALL of them are empty on an agent with no grants, and then the agent runs prompt-only
        # (see src/main.py -> _mcp_tools). MCP_SERVERS is a JSON list of
        # {"id","audience","gateway_url","label"} — one entry per granted MCP server; MCP_AUDIENCE
        # /MCP_GATEWAY_URL are the single-MCP-era keys kept only as a back-compat fallback for a
        # runtime provisioned before MCP_SERVERS existed (parsed in src/mcp_runtime.py).
        # CREDENTIAL_PROVIDER_NAME names the AgentCore Identity credential provider whose vaulted
        # client secret performs the On-Behalf-Of exchange — a NAME, never a secret value.
        self.MCP_SERVERS = os.environ.get("MCP_SERVERS", "")
        self.MCP_AUDIENCE = os.environ.get("MCP_AUDIENCE", "")
        self.MCP_GATEWAY_URL = os.environ.get("MCP_GATEWAY_URL", "")
        self.CREDENTIAL_PROVIDER_NAME = os.environ.get("CREDENTIAL_PROVIDER_NAME", "")


config = Config()
