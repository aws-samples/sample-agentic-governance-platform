"""Environment-variable configuration for the agent (defaults match repo idiom)."""

import os


class Config:
    def __init__(self):
        # The BARE Bedrock model id, exactly as the platform's registry stores it and as the
        # strands template reads it — keeping one spelling across the catalog is what lets the
        # registry inject MODEL_ID into any runtime without knowing the framework.
        self.MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-6")
        self.AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
        # Langfuse observability. The platform provisions one Langfuse project + key PER AGENT and
        # stores the {public_key, secret_key} pair in Secrets Manager; runtime provisioning injects
        # the NON-SECRET host + secret NAME here (never the key values). The agent reads the pair
        # from Secrets Manager at import — NEVER a hardcoded literal. Empty (unconfigured) ⇒
        # telemetry is simply disabled and the agent still runs.
        self.LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "")
        self.LANGFUSE_SECRET_NAME = os.environ.get("LANGFUSE_SECRET_NAME", "")

    @property
    def CREWAI_MODEL(self) -> str:
        """``MODEL_ID`` in the form CrewAI wants: a LiteLLM ``<provider>/<model>`` string.

        CrewAI routes every call through LiteLLM, which picks the provider from this prefix — a
        bare ``us.anthropic.claude-...`` is not resolvable there and fails at call time, not at
        construction. The prefix is added HERE rather than baked into MODEL_ID so the injected
        env value stays framework-neutral (see above). Idempotent: an operator who sets
        ``MODEL_ID=bedrock/...`` by hand does not get ``bedrock/bedrock/...``.
        """
        model_id = self.MODEL_ID
        return model_id if "/" in model_id else f"bedrock/{model_id}"


config = Config()
