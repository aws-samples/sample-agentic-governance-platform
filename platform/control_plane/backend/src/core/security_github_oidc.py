"""GitHub Actions OIDC JWT validator: the inbound trust boundary of ``POST /builds/runtime``.

A GitHub Action, after pushing the agent image, calls the build-trigger endpoint with a GitHub-minted
OIDC token. ``verify_github_oidc_token`` verifies it against GitHub's JWKS with the expected issuer,
audience, expiry and the shared clock-skew leeway, and returns the typed claims; it is not the Entra
path and runs no Entra RBAC. The ``repository_owner`` against connected-org check happens at the
route. ``Settings()`` is read per call so a monkeypatched environment is honoured.
"""

import logging
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field

from models.base import WireModel

logger = logging.getLogger(__name__)


class GitHubActionsClaims(WireModel):
    """The validated subset of a GitHub Actions OIDC token the route acts on; extra claims are ignored."""

    repository: str = Field(description="<owner>/<repo>")
    repository_owner: str = Field(
        description="The org or user login the route matches against the resolved connection's org"
    )
    sub: Optional[str] = None
    ref: Optional[str] = None
    sha: Optional[str] = None
    workflow: Optional[str] = None
    actor: Optional[str] = Field(default=None, description="The GitHub login that triggered the run")
    event_name: Optional[str] = Field(default=None, description="push, pull_request, ...")


def _jwks_url() -> str:
    """The GitHub Actions OIDC JWKS endpoint, derived from the configured issuer."""
    from core.config import Settings

    settings = Settings()
    return f"{settings.GITHUB_OIDC_ISSUER.rstrip('/')}/.well-known/jwks"


def _expected_issuer() -> str:
    from core.config import Settings

    return Settings().GITHUB_OIDC_ISSUER.rstrip("/")


def _expected_audience() -> str:
    from core.config import Settings

    return Settings().GITHUB_OIDC_AUDIENCE


_jwk_client = None


def _get_jwk_client():
    """The process-wide ``PyJWKClient`` for the GitHub OIDC JWKS endpoint."""
    global _jwk_client
    if _jwk_client is None:
        from jwt import PyJWKClient

        _jwk_client = PyJWKClient(_jwks_url(), cache_keys=True)
    return _jwk_client


def reset_jwk_client_cache() -> None:
    """Drop the cached client so a new issuer is picked up (tests)."""
    global _jwk_client
    _jwk_client = None


def verify_github_oidc_token(token: str) -> GitHubActionsClaims:
    """Verify an inbound GitHub Actions OIDC JWT (no ``Bearer`` prefix) and return the typed claims.

    Raises ``HTTPException(401)`` on any validation failure: bad issuer, audience, signature or expiry,
    or a token without the ``repository`` and ``repository_owner`` claims GitHub mints on every
    Actions token.
    """
    import jwt as pyjwt

    from core.config import Settings

    settings = Settings()

    expected_issuer = _expected_issuer()
    expected_audience = _expected_audience()

    try:
        signing_key = _get_jwk_client().get_signing_key_from_jwt(token)
    except Exception as exc:
        logger.warning("GitHub OIDC JWKS lookup failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: signing key lookup failed",
        ) from exc

    try:
        claims = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=expected_audience,
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
        logger.warning("[github_oidc] token rejected: expired")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
        ) from exc
    except pyjwt.InvalidIssuerError as exc:
        logger.warning("[github_oidc] token rejected: bad issuer (expected %s)", expected_issuer)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid issuer (expected {expected_issuer})",
        ) from exc
    except pyjwt.InvalidAudienceError as exc:
        logger.warning("[github_oidc] token rejected: bad audience (expected %s)", expected_audience)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid audience (expected {expected_audience})",
        ) from exc
    except pyjwt.InvalidSignatureError as exc:
        logger.warning("[github_oidc] token rejected: invalid signature")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token signature",
        ) from exc
    except pyjwt.PyJWTError as exc:
        logger.warning("[github_oidc] token rejected: %s: %s", type(exc).__name__, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {type(exc).__name__}",
        ) from exc

    if not claims.get("repository") or not claims.get("repository_owner"):
        logger.warning("[github_oidc] token rejected: missing repository/repository_owner claim")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: missing repository claims",
        )

    return GitHubActionsClaims(
        repository=claims["repository"],
        repository_owner=claims["repository_owner"],
        sub=claims.get("sub"),
        ref=claims.get("ref"),
        sha=claims.get("sha"),
        workflow=claims.get("workflow"),
        actor=claims.get("actor"),
        event_name=claims.get("event_name"),
    )


_security = HTTPBearer()


def verify_github_oidc(
    credentials: HTTPAuthorizationCredentials = Depends(_security),
) -> GitHubActionsClaims:
    """FastAPI dependency: validate the Bearer GitHub OIDC token into ``GitHubActionsClaims``.

    The only ``HTTPBearer`` security scheme on a live route (``core/rbac.py`` reads the header itself),
    and a separate auth path that never routes through Entra RBAC.
    """
    return verify_github_oidc_token(credentials.credentials)
