"""Environment-variable configuration for the agent (defaults match repo idiom)."""

import os


class Config:
    def __init__(self):
        self.MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-6")
        self.AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
        # Langfuse observability. The platform provisions one Langfuse project + key PER AGENT
        # and stores the {public_key, secret_key} pair in Secrets Manager; runtime-provisioning
        # injects the NON-SECRET host + secret NAME here (never the key values). The agent reads
        # the pair from Secrets Manager at import — NEVER a hardcoded literal. Empty
        # (unconfigured) ⇒ telemetry is simply disabled and the agent still runs.
        self.LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "")
        self.LANGFUSE_SECRET_NAME = os.environ.get("LANGFUSE_SECRET_NAME", "")


config = Config()
