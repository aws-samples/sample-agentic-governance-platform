"""The bodies of the unauthenticated routes ``smoke.sh`` reads. Each model declares exactly the keys
its handler emits, so the document and the deployment smoke test read the same contract.
"""

from __future__ import annotations

from typing import Literal

from models.base import WireModel


class HealthChecks(WireModel):
    s3: str


class HealthResponse(WireModel):
    status: Literal["healthy", "degraded"]
    timestamp: str
    checks: HealthChecks


class PingResponse(WireModel):
    ping: Literal["pong"]
    timestamp: str


class RootResponse(WireModel):
    name: str
    version: str
    status: str
    build_sha: str
