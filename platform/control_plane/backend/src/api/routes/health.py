"""Health and ping routes, registered without ``API_PREFIX`` and without authentication.

A failed probe puts a fixed literal in the body and the exception in the log; nothing from the
exception reaches the anonymous caller (``tests/test_health_no_internal_disclosure.py``). The one
``S3Service`` is built on first use; tests patch ``_s3_service``.
"""

import logging
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter

from models.health import HealthChecks, HealthResponse, PingResponse
from services.s3_service import S3Service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

_s3: Optional[S3Service] = None


def _s3_service() -> S3Service:
    global _s3
    if _s3 is None:
        _s3 = S3Service()
    return _s3


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """The API's own status plus an S3 probe."""
    status: Literal["healthy", "degraded"] = "healthy"
    try:
        if _s3_service().check_bucket_accessible():
            s3 = "accessible"
        else:
            status, s3 = "degraded", "not accessible"
    except Exception:
        logger.exception("S3 health check failed")
        status, s3 = "degraded", "error: s3 probe failed"
    return HealthResponse(status=status, timestamp=datetime.utcnow().isoformat(), checks=HealthChecks(s3=s3))


@router.get("/ping", response_model=PingResponse)
async def ping():
    return PingResponse(ping="pong", timestamp=datetime.utcnow().isoformat())
