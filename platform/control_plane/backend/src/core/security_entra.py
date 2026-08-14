"""
Microsoft Entra ID JWT validator.

This is the inbound JWT path for AUTH_PROVIDER=entra. It does NOT do role
mapping (that's rbac.py). It does NOT do user-info shaping (that's the
/users/me route). It does ONE thing: take a JWT string, verify it against
the Entra tenant's JWKS with the correct issuer + audience + expiry, and
return the claims dict.

JWKS is cached per-process by PyJWT's PyJWKClient (which has its own LRU
cache). On any failure, raises HTTPException(401, ...) with a diagnostic
message safe to surface to the client (no secrets).

The live caller is core/rbac.py (`current_role` / `current_principal`), which calls
verify_entra_token directly — this is the ONLY inbound-JWT entry point. This module's
shape is a leftover of the multi-provider era: it mirrored a Cognito validator so an
import-time dispatcher could swap them. Every other participant in that arrangement has
since been deleted (the dispatcher, its dev-auth bypass and its HTTPBearer facade went in
E36/T14, the Cognito validator earlier), so nothing dispatches to this module any more.

Settings instantiation: this module reads `core.config.Settings()` per call
(not the module-level singleton). Per-request configuration must be readable
from monkeypatched env vars in tests; the singleton is frozen at first import.
See per-epic plan §"Convention discovered in T2".
"""

import logging
from typing import Dict

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)


def _expected_issuer() -> str:
    """The exact issuer claim Entra mints for v2.0 tokens in our tenant."""
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
    """The OIDC JWKS endpoint for our tenant."""
    from core.config import Settings

    settings = Settings()
    return (
        f"https://login.microsoftonline.com/{settings.ENTRA_TENANT_ID}"
        "/discovery/v2.0/keys"
    )


# Lazy singleton — PyJWKClient does its own caching once instantiated.
_jwk_client = None


def _get_jwk_client():
    """Return a singleton PyJWKClient for the configured tenant."""
    global _jwk_client
    if _jwk_client is None:
        from jwt import PyJWKClient

        _jwk_client = PyJWKClient(
            _jwks_url(),
            # PyJWKClient's built-in caching. `cache_keys=True` wraps get_signing_key in an
            # lru_cache(maxsize=16) that NEVER expires; the JWK-SET cache is a separate thing
            # whose `lifespan` defaults to 300 SECONDS (jwt/jwks_client.py:21), not ~1h.
            cache_keys=True,
        )
    return _jwk_client


def reset_jwk_client_cache() -> None:
    """Test helper — drop the cached client so a new tenant ID is picked up."""
    global _jwk_client
    _jwk_client = None


def verify_entra_token(token: str) -> Dict:
    """
    Verify an inbound Entra JWT and return its claims.

    Args:
        token: the raw JWT string (no 'Bearer ' prefix).

    Returns:
        Decoded claims dict.

    Raises:
        HTTPException(401): on any validation failure.
        RuntimeError: if ENTRA_TENANT_ID is not configured.
    """
    import jwt as pyjwt

    from core.config import Settings

    settings = Settings()

    expected_issuer = _expected_issuer()  # raises RuntimeError if tenant unconfigured

    try:
        signing_key = _get_jwk_client().get_signing_key_from_jwt(token)
    except Exception as exc:
        # PyJWKClient errors out if the token has no kid, the JWKS endpoint
        # is unreachable, or no key matches the kid.
        logger.warning("Entra JWKS lookup failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: signing key lookup failed",
        ) from exc

    # Accept the configured audience URI plus the relevant client-ID GUIDs.
    # Microsoft's v2.0 access tokens for an app's own exposed scope carry
    # aud=<that app's client-id GUID> rather than the URI form, so we accept:
    #   - ENTRA_AUDIENCE: the canonical URI (e.g. api://agp)
    #   - ENTRA_SPA_CLIENT_ID: legacy SPA-scope tokens (safe additive transition)
    #   - ENTRA_BACKEND_CLIENT_ID: tokens for the backend confidential app's own
    #     exposed scope (api://agp/Access.Default) carry
    #     aud=<backend-client-GUID>, which is the current frontend path.
    # PyJWT accepts a list and passes if ANY value matches the token's aud claim.
    accepted_audiences: list[str] = [settings.ENTRA_AUDIENCE]
    if settings.ENTRA_SPA_CLIENT_ID:
        accepted_audiences.append(settings.ENTRA_SPA_CLIENT_ID)
    if settings.ENTRA_BACKEND_CLIENT_ID:
        accepted_audiences.append(settings.ENTRA_BACKEND_CLIENT_ID)
    # De-dup defensively in case any values coincide (preserves order).
    accepted_audiences = list(dict.fromkeys(accepted_audiences))

    try:
        claims = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=accepted_audiences,
            issuer=expected_issuer,
            # Clock-skew tolerance (E36/T17). Zero leeway turned a container clock a
            # few seconds fast into blanket 401s. Applies to `exp` AND `nbf`/`iat`.
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
    except pyjwt.ExpiredSignatureError:
        logger.warning("[security_entra] token rejected: expired")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
        )
    except pyjwt.InvalidIssuerError as exc:
        logger.warning("[security_entra] token rejected: bad issuer (expected %s, got %s)",
                       expected_issuer, _peek_unverified(token, "iss"))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid issuer (expected {expected_issuer})",
        ) from exc
    except pyjwt.InvalidAudienceError as exc:
        logger.warning("[security_entra] token rejected: bad audience (accepted %s, got %s)",
                       accepted_audiences, _peek_unverified(token, "aud"))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid audience (accepted {accepted_audiences})",
        ) from exc
    except pyjwt.InvalidSignatureError:
        logger.warning("[security_entra] token rejected: invalid signature")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token signature",
        )
    except pyjwt.PyJWTError as exc:
        logger.warning("[security_entra] token rejected: %s: %s", type(exc).__name__, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {type(exc).__name__}",
        ) from exc

    return claims


def _peek_unverified(token: str, claim: str):
    """Decode a JWT payload WITHOUT verification to extract a single claim for
    logging. Only used in error paths after PyJWT rejected the token. NEVER
    trust this value for any decision."""
    try:
        import jwt as pyjwt
        return pyjwt.decode(token, options={"verify_signature": False}).get(claim)
    except Exception:
        return "<unparseable>"
