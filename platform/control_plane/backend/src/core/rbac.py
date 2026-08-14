"""Role-Based Access Control middleware with Entra JWT validation.

Entra ID is the sole real auth provider. A local dev-auth bypass (gated on
USE_DEV_AUTH/DEBUG) maps x-user-email / x-user-role headers to a Role for local
development; it is decoupled from any provider setting and never applies when
dev-auth is off (missing/invalid Entra tokens then return 401, no fallback).
"""

import logging
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Mapping, Optional

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

class Role(IntEnum):
    VIEWER = 0
    OPERATOR = 1
    ADMIN = 2

ROLE_MAP = {"viewer": Role.VIEWER, "operator": Role.OPERATOR, "admin": Role.ADMIN}


def _role_from_dev_headers(request: Request) -> Role:
    """Map dev-auth request headers to a Role (local development only).

    Mirrors the legacy header logic: an explicit ``x-user-email`` selects a
    role (demo → VIEWER, admin/dev → ADMIN); otherwise an ``x-user-role`` header
    is honored; with neither header present the caller defaults to ADMIN so a
    bare local request still works.
    """
    user_email = request.headers.get("x-user-email")
    if user_email:
        user_email = user_email.lower()
        if user_email == "demo@example.com":
            return Role.VIEWER
        if user_email in ("admin@example.com", "dev@example.com"):
            return Role.ADMIN
    role_str = request.headers.get("x-user-role", "admin").lower()
    return ROLE_MAP.get(role_str, Role.ADMIN)


def _role_from_entra_claims(claims: dict, settings_fresh) -> Role:
    """Map the Entra `roles` claim to our Role enum. Pick the highest if multiple.

    Shared by `_extract_role` and `current_principal` so the entra role mapping
    lives in exactly one place. No matching role → least-privilege VIEWER default.
    """
    roles_claim = claims.get("roles") or []
    if settings_fresh.ENTRA_ROLE_ADMIN in roles_claim:
        return Role.ADMIN
    if settings_fresh.ENTRA_ROLE_OPERATOR in roles_claim:
        return Role.OPERATOR
    if settings_fresh.ENTRA_ROLE_VIEWER in roles_claim:
        return Role.VIEWER
    return Role.VIEWER


def _extract_role(request: Request) -> Role:
    """
    Extract the caller's Role from the Authorization header.

    Precedence:
      - dev-auth (USE_DEV_AUTH or DEBUG) → map x-user-email / x-user-role headers
        to a Role for local development (no token required).
      - else → validate via core.security_entra.verify_entra_token; map the
        `roles` claim. Missing or invalid tokens always return 401 — no fallback.
        This matches the prototype's "real tokens, real validation" stance.
    """
    auth = request.headers.get("Authorization", "")

    # Settings() fresh inside the function so monkeypatched env vars in tests
    # are honored (the module-level `settings` singleton is frozen at first import).
    # The Entra branch depends on per-call instantiation for test mutability.
    from core.config import Settings as _Settings
    settings_fresh = _Settings()

    # === Dev-auth branch (local development only) ===
    # Wins when explicitly enabled, regardless of provider. Header-driven role
    # selection; no token validation. Decoupled from the removed Cognito branch.
    if settings_fresh.USE_DEV_AUTH or settings_fresh.DEBUG:
        return _role_from_dev_headers(request)

    # === Entra branch ===
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authorization token")
    token = auth.split(" ", 1)[1]

    from core.security_entra import verify_entra_token

    # verify_entra_token raises HTTPException(401) on any validation failure.
    claims = verify_entra_token(token)

    # Map the `roles` claim to our Role enum. Pick the highest if multiple.
    return _role_from_entra_claims(claims, settings_fresh)


def require_role(min_role: Role):
    async def checker(request: Request):
        user_role = _extract_role(request)
        if user_role < min_role:
            raise HTTPException(status_code=403, detail=f"Requires {min_role.name.lower()} role or higher")
        return user_role
    return checker


def principal_email(claims: Mapping[str, Any]) -> Optional[str]:
    """THE email precedence for Entra claims: ``preferred_username`` → ``email`` → ``upn``.

    One order, one place. Two orders used to run at once — this function is what
    `current_principal` (which fills `created_by` / `granted_by`) and `GET /users/me`
    (which fills what the interface shows) both call, so the address attributed to a
    caller's writes is the address that caller is shown.

    `preferred_username` leads because it always carries a value; `email` is the
    authoritative deliverable-mailbox claim but is an *optional* claim that has to be
    configured on the app registration, so it is frequently absent.

    Returns None when the token carries none of the three (a service principal, say).
    That None is deliberate: `"unknown"` belongs to presentation, so /users/me applies
    it at the call site and it can never be persisted into an audit field. Route code
    pairs the None with the caller's oid (``created_by=principal.email or principal.oid``).

    The write side never moved: `current_principal` has picked this order all along, so
    no `created_by` row changes meaning and no back-compat or migration question arises.
    `/users/me`, the read side, is what moved onto it — and what it now shows a caller
    can be a UPN-style identifier rather than a deliverable mailbox.
    """
    return (
        claims.get("preferred_username")
        or claims.get("email")
        or claims.get("upn")
    ) or None


@dataclass
class Principal:
    """The validated caller identity, richer than the bare Role.

    Used by routes (via Depends(current_principal)) to populate `created_by`
    and to default sponsor fields. Under Entra these come from the validated
    token claims; in dev-auth they come from the x-user-email header.
    """
    oid: Optional[str]          # Entra objectId; may be None in dev
    email: Optional[str]        # preferred_username / email / upn / x-user-email
    role: Role
    raw_claims: dict            # the validated claims (empty dict in dev shortcuts)


def current_principal(request: Request) -> Principal:
    """FastAPI dependency returning the caller identity.

    Reuses the SAME precedence as `_extract_role`:
      - dev-auth (USE_DEV_AUTH/DEBUG) → role + identity from the x-user-email
        header (no token validation; empty raw_claims).
      - else (Entra) → verify_entra_token (raises HTTPException(401) on failure),
        then maps the `roles` claim via `_role_from_entra_claims`.

    Used as: `principal: Principal = Depends(current_principal)`.
    """
    auth = request.headers.get("Authorization", "")

    # Fresh Settings() so monkeypatched env vars in tests are honored
    # (mirrors the pattern in _extract_role).
    from core.config import Settings as _Settings
    settings_fresh = _Settings()

    # === Dev-auth branch (local development only) ===
    if settings_fresh.USE_DEV_AUTH or settings_fresh.DEBUG:
        role = _role_from_dev_headers(request)
        email = request.headers.get("x-user-email")
        if email:
            email = email.lower()
        return Principal(oid=None, email=email, role=role, raw_claims={})

    # === Entra branch ===
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authorization token")
    token = auth.split(" ", 1)[1]

    from core.security_entra import verify_entra_token

    # verify_entra_token raises HTTPException(401) on any validation failure.
    claims = verify_entra_token(token)

    oid = claims.get("oid")
    email = principal_email(claims)
    role = _role_from_entra_claims(claims, settings_fresh)
    return Principal(oid=oid, email=email, role=role, raw_claims=claims)
