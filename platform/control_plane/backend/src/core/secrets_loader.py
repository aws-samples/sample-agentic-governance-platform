"""Loader for the Entra backend client secret, the credential behind outbound Microsoft Graph calls.

``load_entra_backend_client_secret`` returns ``settings.ENTRA_BACKEND_CLIENT_SECRET`` when it is set
(local dev through ``backend/.env``, and ECS, whose task definition injects it from Secrets Manager);
otherwise it fetches ``settings.ENTRA_BACKEND_CLIENT_SECRET_ARN`` from Secrets Manager itself,
unwrapping a JSON object body by the first of the keys in ``_JSON_SECRET_KEYS``, else by its first
value; with neither setting configured it raises ``RuntimeError``. The value is cached for the
process, since the secret does not rotate within one lifetime. ``Settings()`` is read per call so a
monkeypatched environment is honoured, and the Secrets Manager client is built, through
``core.aws_clients``, only when the ARN path runs.
"""

import json
import logging
from typing import Optional

from core import aws_clients

logger = logging.getLogger(__name__)

_cached_secret: Optional[str] = None

_JSON_SECRET_KEYS = ("secret", "value", "password", "ENTRA_BACKEND_CLIENT_SECRET")


def _unwrap(secret_string: str) -> str:
    try:
        parsed = json.loads(secret_string)
    except ValueError:
        return secret_string
    if not isinstance(parsed, dict) or not parsed:
        return secret_string
    for key in _JSON_SECRET_KEYS:
        if key in parsed:
            return parsed[key]
    return next(iter(parsed.values()))


def load_entra_backend_client_secret() -> str:
    """Return the Entra backend confidential-client secret.

    Raises ``RuntimeError`` when neither the literal setting nor its Secrets Manager ARN is configured.
    """
    global _cached_secret
    if _cached_secret is not None:
        return _cached_secret

    from core.config import Settings

    settings = Settings()

    if settings.ENTRA_BACKEND_CLIENT_SECRET:
        logger.info("[secrets_loader] using literal ENTRA_BACKEND_CLIENT_SECRET from environment")
        secret: str = settings.ENTRA_BACKEND_CLIENT_SECRET
        _cached_secret = secret
        return secret

    if settings.ENTRA_BACKEND_CLIENT_SECRET_ARN:
        logger.info(
            "[secrets_loader] loading ENTRA_BACKEND_CLIENT_SECRET from Secrets Manager %s",
            settings.ENTRA_BACKEND_CLIENT_SECRET_ARN,
        )
        client = aws_clients.client("secretsmanager", region=settings.AWS_REGION)
        response = client.get_secret_value(SecretId=settings.ENTRA_BACKEND_CLIENT_SECRET_ARN)
        secret = _unwrap(response["SecretString"])
        _cached_secret = secret
        return secret

    raise RuntimeError(
        "ENTRA_BACKEND_CLIENT_SECRET is not configured. "
        "Set ENTRA_BACKEND_CLIENT_SECRET (local dev) or ENTRA_BACKEND_CLIENT_SECRET_ARN "
        "(cloud, fetched via boto3 from AWS Secrets Manager). "
        "See backend/.env.example for local dev setup."
    )


def reset_cache() -> None:
    """Clear the module-level cache (tests)."""
    global _cached_secret
    _cached_secret = None
