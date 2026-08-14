"""GitHub Actions OIDC JWT validator — a SEPARATE inbound trust boundary.

This is the auth path for the E22/T6 build-trigger endpoint: a GitHub Action, after
pushing the agent image, calls `POST /builds/runtime` with a GitHub-minted OIDC token.
This module verifies that token — it is NOT the Entra path and does NOT do Entra RBAC
(`require_role`/`current_principal`). It does ONE thing: take a JWT, verify it against
GitHub's OIDC JWKS with the correct issuer + audience + expiry, and return the claims.

Shape mirrors `core/security_entra.py` deliberately (lazy cached `PyJWKClient`,
`_jwks_url()`/`_expected_issuer()`/`_expected_audience()` reading `Settings()` per-call,
`reset_jwk_client_cache()` test helper, the typed-exception→HTTP-401 chain). The
`repository_owner`→connected-org authorization check is NOT here — it happens at the
route (defense-in-depth against the resolved connection's org). This validator only
proves the token is a genuine, current, correctly-audienced GitHub Actions token.

Settings are read per-call (not the module singleton) so tests' monkeypatched env is
honored — same convention as security_entra.
"""

import logging
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class GitHubActionsClaims(BaseModel):
    """The validated subset of a GitHub Actions OIDC token we act on.

    Extra claims are ignored. `repository_owner` is the GitHub org login the route
    matches against the resolved connection's org (defense-in-depth)."""

    repository: str  # "<owner>/<repo>"
    repository_owner: str  # the org/user login
    sub: Optional[str] = None
    ref: Optional[str] = None
    sha: Optional[str] = None
    workflow: Optional[str] = None
    actor: Optional[str] = None  # the GitHub login that triggered the run
    event_name: Optional[str] = None  # "push" | "pull_request" | ...


def _jwks_url() -> str:
    """The GitHub Actions OIDC JWKS endpoint (derived from the configured issuer)."""
    from core.config import Settings

    settings = Settings()
    return f"{settings.GITHUB_OIDC_ISSUER.rstrip('/')}/.well-known/jwks"


def _expected_issuer() -> str:
    from core.config import Settings

    return Settings().GITHUB_OIDC_ISSUER.rstrip("/")


def _expected_audience() -> str:
    from core.config import Settings

    return Settings().GITHUB_OIDC_AUDIENCE


# Lazy singleton — PyJWKClient does its own caching once instantiated.
_jwk_client = None


def _get_jwk_client():
    """Return a singleton PyJWKClient for the GitHub OIDC JWKS endpoint."""
    global _jwk_client
    if _jwk_client is None:
        from jwt import PyJWKClient

        _jwk_client = PyJWKClient(
            _jwks_url(),
            cache_keys=True,  # PyJWKClient's built-in LRU cache (default ttl ~1h)
        )
    return _jwk_client


def reset_jwk_client_cache() -> None:
    """Test helper — drop the cached client so a new issuer is picked up."""
    global _jwk_client
    _jwk_client = None


def verify_github_oidc_token(token: str) -> GitHubActionsClaims:
    """Verify an inbound GitHub Actions OIDC JWT and return the typed claims.

    Args:
        token: the raw JWT string (no 'Bearer ' prefix).

    Returns:
        GitHubActionsClaims.

    Raises:
        HTTPException(401): on any validation failure (bad issuer/audience/signature/
            expiry, or missing required claims).
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
            # Same shared clock-skew tolerance as the Entra path (E36/T17); skew here
            # breaks deploys, not logins. Applies to `exp` AND `nbf`/`iat`.
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
        logger.warning("[github_oidc] token rejected: expired")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
        )
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
    except pyjwt.InvalidSignatureError:
        logger.warning("[github_oidc] token rejected: invalid signature")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token signature",
        )
    except pyjwt.PyJWTError as exc:
        logger.warning("[github_oidc] token rejected: %s: %s", type(exc).__name__, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {type(exc).__name__}",
        ) from exc

    # Required identity claims. GitHub always mints these on an Actions OIDC token; a
    # token missing them is not a usable Actions token → reject rather than 500 later.
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
    """FastAPI dependency: validate the Bearer GitHub OIDC token → GitHubActionsClaims.

    Injects the bearer token via `Depends(HTTPBearer())` — note that the Entra path does NOT
    (`core/rbac.py` reads the header itself), so this is the only `HTTPBearer` security scheme
    on a live route. It is also a SEPARATE auth path: it never routes through Entra RBAC."""
    return verify_github_oidc_token(credentials.credentials)
