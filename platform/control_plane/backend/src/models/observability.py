"""The response model of ``GET /observability/settings``.

``ObservabilitySettings`` tells the frontend whether Langfuse is configured and which host it is:
``configured`` is ``bool(LANGFUSE_HOST)`` and ``langfuse_host`` is that host or ``None`` when unset,
so a page renders its not-configured state from ``configured`` alone. No key value is on the wire;
the per-agent key pair stays in Secrets Manager and ``services.langfuse_metrics_service`` reads it.
"""

from __future__ import annotations

from models.base import WireModel


class ObservabilitySettings(WireModel):
    langfuse_host: str | None
    configured: bool
