"""
Loader for the Entra backend client secret.

Priority:
  1. settings.ENTRA_BACKEND_CLIENT_SECRET (set directly — local dev path).
  2. settings.ENTRA_BACKEND_CLIENT_SECRET_ARN → fetch from AWS Secrets Manager (cloud path).
  3. Both blank → raise RuntimeError.

The result is cached in a module-level variable; the secret does not rotate
within a process lifetime, and re-fetching on every call would slow startup
and consume AWS API quota.

This loader is the only place boto3 is involved in inbound auth. The inbound
JWT validator (security_entra.py) does not need the backend secret — it only
needs the tenant's public JWKS. The secret is needed for outbound Microsoft
Graph calls (E5+). We surface it now to make the deploy story honest.
"""

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Module-level cache (set on first successful load).
_cached_secret: Optional[str] = None


def load_entra_backend_client_secret() -> str:
    """
    Return the Entra backend confidential-client secret.

    Raises:
        RuntimeError: if neither the literal secret nor an ARN is configured.
    """
    global _cached_secret
    if _cached_secret is not None:
        return _cached_secret

    # Build a fresh Settings instance inside the function so monkeypatched env
    # vars in tests are honored (the module-level `settings` singleton is frozen
    # at first import).
    from core.config import Settings

    settings = Settings()

    # Path 1: literal secret already in the environment — local dev via backend/.env,
    # and in ECS too, where the task definition injects it natively from Secrets
    # Manager (`secrets`/`valueFrom`). This is the normal path in both places.
    if settings.ENTRA_BACKEND_CLIENT_SECRET:
        logger.info("[secrets_loader] using literal ENTRA_BACKEND_CLIENT_SECRET from environment")
        _cached_secret = settings.ENTRA_BACKEND_CLIENT_SECRET
        return _cached_secret

    # Path 2: fetch from AWS Secrets Manager ourselves — the fallback for a runtime
    # that is handed only an ARN.
    if settings.ENTRA_BACKEND_CLIENT_SECRET_ARN:
        import boto3  # imported lazily so local dev can avoid the boto3 startup cost.

        logger.info(
            "[secrets_loader] loading ENTRA_BACKEND_CLIENT_SECRET from Secrets Manager %s",
            settings.ENTRA_BACKEND_CLIENT_SECRET_ARN,
        )
        client = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
        response = client.get_secret_value(
            SecretId=settings.ENTRA_BACKEND_CLIENT_SECRET_ARN
        )
        secret_string = response["SecretString"]

        # Some teams store secrets as JSON `{"secret": "...", ...}` — handle gracefully.
        try:
            parsed = json.loads(secret_string)
            if isinstance(parsed, dict):
                # Try common keys; fall back to the first value if none match.
                for key in ("secret", "value", "password", "ENTRA_BACKEND_CLIENT_SECRET"):
                    if key in parsed:
                        _cached_secret = parsed[key]
                        return _cached_secret
                # JSON dict with no known key → not what we expected, but don't fail —
                # caller may have stored raw JSON intentionally. Use the first value.
                _cached_secret = next(iter(parsed.values())) if parsed else secret_string
                return _cached_secret
        except (json.JSONDecodeError, ValueError):
            # Not JSON — treat the entire SecretString as the secret value.
            pass

        _cached_secret = secret_string
        return _cached_secret

    # Path 3: misconfigured.
    raise RuntimeError(
        "ENTRA_BACKEND_CLIENT_SECRET is not configured. "
        "Set ENTRA_BACKEND_CLIENT_SECRET (local dev) or ENTRA_BACKEND_CLIENT_SECRET_ARN "
        "(cloud, fetched via boto3 from AWS Secrets Manager). "
        "See backend/.env.example for local dev setup."
    )


def reset_cache() -> None:
    """Test helper — clear the module-level cache."""
    global _cached_secret
    _cached_secret = None
