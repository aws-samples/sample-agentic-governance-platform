"""Microsoft Entra ID JWT validator: the inbound JWT entry point ``core.rbac`` calls.

``verify_entra_token`` verifies one JWT against the tenant's JWKS with the expected issuer, the
accepted audiences and the shared clock-skew leeway, and returns the claims. It does no role mapping
(``rbac.py``) and no user-info shaping (the ``/users/me`` route). PyJWT's ``PyJWKClient`` caches the
JWKS per process. Every failure is an ``HTTPException(401)`` whose detail is safe to show a client.
``Settings()`` is read per call so a monkeypatched environment is honoured.
"""

import logging
from typing import Dict

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)


def _expected_issuer() -> str:
    """The issuer claim Entra mints for v2.0 tokens in the configured tenant."""
    from core.config import Settings

    settings = Settings()
    if not settings.ENTRA_TENANT_ID:
        raise RuntimeError(
            "ENTRA_TENANT_ID is not configured. "
            "Set it in the environment: in cloud it is rendered into the ECS "
            "task definition from `entra_tenant_id` in "
            "infrastructure/secrets.auto.tfvars; for local dev see "
            "backend/.env.example."
        )
    return f"https://login.microsoftonline.com/{settings.ENTRA_TENANT_ID}/v2.0"


def _jwks_url() -> str:
    """The OIDC JWKS endpoint of the configured tenant."""
    from core.config import Settings

    settings = Settings()
    return f"https://login.microsoftonline.com/{settings.ENTRA_TENANT_ID}/discovery/v2.0/keys"


_jwk_client = None


def _get_jwk_client():
    """The process-wide ``PyJWKClient`` for the configured tenant."""
    global _jwk_client
    if _jwk_client is None:
        from jwt import PyJWKClient

        _jwk_client = PyJWKClient(_jwks_url(), cache_keys=True)
    return _jwk_client


def reset_jwk_client_cache() -> None:
    """Drop the cached client so a new tenant id is picked up (tests)."""
    global _jwk_client
    _jwk_client = None


def _accepted_audiences(settings) -> list[str]:
    """``ENTRA_AUDIENCE`` plus the SPA and backend client ids when set, deduplicated in order.

    Entra's v2.0 access tokens for an app's own exposed scope carry ``aud=<that app's client-id GUID>``
    rather than the identifier URI, so both forms are accepted; PyJWT passes when any value matches.
    """
    audiences: list[str] = [settings.ENTRA_AUDIENCE]
    if settings.ENTRA_SPA_CLIENT_ID:
        audiences.append(settings.ENTRA_SPA_CLIENT_ID)
    if settings.ENTRA_BACKEND_CLIENT_ID:
        audiences.append(settings.ENTRA_BACKEND_CLIENT_ID)
    return list(dict.fromkeys(audiences))


def verify_entra_token(token: str) -> Dict:
    """Verify an inbound Entra JWT (no ``Bearer`` prefix) and return its claims.

    Raises ``HTTPException(401)`` on any validation failure and ``RuntimeError`` when
    ``ENTRA_TENANT_ID`` is not configured.
    """
    import jwt as pyjwt

    from core.config import Settings

    settings = Settings()
    expected_issuer = _expected_issuer()

    try:
        signing_key = _get_jwk_client().get_signing_key_from_jwt(token)
    except Exception as exc:
        logger.warning("Entra JWKS lookup failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: signing key lookup failed",
        ) from exc

    accepted_audiences = _accepted_audiences(settings)
    try:
        claims = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=accepted_audiences,
            issuer=expected_issuer,
            leeway=settings.JWT_LEEWAY_SECONDS,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_nbf": True,
                "verify_iss": True,
                "verify_aud": True,
            },
        )
    except pyjwt.ExpiredSignatureError as exc:
        logger.warning("[security_entra] token rejected: expired")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
        ) from exc
    except pyjwt.InvalidIssuerError as exc:
        presented_issuer = _peek_unverified(token, "iss")
        logger.warning(
            "[security_entra] token rejected: bad issuer (expected %s, got %s)",
            expected_issuer,
            presented_issuer,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid issuer (expected {expected_issuer})",
        ) from exc
    except pyjwt.InvalidAudienceError as exc:
        presented_audience = _peek_unverified(token, "aud")
        logger.warning(
            "[security_entra] token rejected: bad audience (accepted %s, got %s)",
            accepted_audiences,
            presented_audience,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid audience (accepted {accepted_audiences})",
        ) from exc
    except pyjwt.InvalidSignatureError as exc:
        logger.warning("[security_entra] token rejected: invalid signature")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token signature",
        ) from exc
    except pyjwt.PyJWTError as exc:
        logger.warning("[security_entra] token rejected: %s: %s", type(exc).__name__, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {type(exc).__name__}",
        ) from exc

    return claims


def _peek_unverified(token: str, claim: str):
    """One claim of the payload, decoded WITHOUT verification for the rejection log after PyJWT
    refused the token; never a value to decide on. ``"<unparseable>"`` when PyJWT cannot read it."""
    import jwt as pyjwt

    try:
        return pyjwt.decode(token, options={"verify_signature": False}).get(claim)
    except pyjwt.PyJWTError:
        return "<unparseable>"
