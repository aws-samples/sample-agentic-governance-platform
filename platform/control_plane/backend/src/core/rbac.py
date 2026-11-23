"""Role-based access control: the FastAPI dependencies that turn a request into a ``Role`` or a
``Principal``.

Entra ID is the only real auth provider. The dev-auth bypass maps the ``x-user-email`` / ``x-user-role``
headers to a role only while ``Settings.dev_auth_honoured`` holds: ``USE_DEV_AUTH`` on and every store a
double, the local launcher's shape. Otherwise a missing or invalid token is a 401 and nothing else is
consulted, ``DEBUG`` included. ``Settings()`` is read per call so a monkeypatched environment is honoured.
"""

import logging
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, ClassVar, Mapping, Optional

from fastapi import HTTPException, Request

from core.errors import AppError, Outcome

logger = logging.getLogger(__name__)


class Role(IntEnum):
    VIEWER = 0
    OPERATOR = 1
    ADMIN = 2


ROLE_MAP = {"viewer": Role.VIEWER, "operator": Role.OPERATOR, "admin": Role.ADMIN}


class AuthError(AppError):
    """The caller's role is below the route's minimum. The message is the detail and names the
    required role alone, never the caller's token or claims."""

    domain: ClassVar[str] = "auth"
    kinds: ClassVar[Mapping[str, Outcome]] = {"role_required": Outcome(403, None)}


def _role_from_dev_headers(request: Request) -> Role:
    """The dev-auth role: ``demo@example.com`` is a viewer, ``admin@`` or ``dev@example.com`` an admin,
    otherwise the ``x-user-role`` header, and ADMIN when neither header is present."""
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
    """The highest ``Role`` named in the token's ``roles`` claim; VIEWER when none matches."""
    roles_claim = claims.get("roles") or []
    if settings_fresh.ENTRA_ROLE_ADMIN in roles_claim:
        return Role.ADMIN
    if settings_fresh.ENTRA_ROLE_OPERATOR in roles_claim:
        return Role.OPERATOR
    if settings_fresh.ENTRA_ROLE_VIEWER in roles_claim:
        return Role.VIEWER
    return Role.VIEWER


def _extract_role(request: Request) -> Role:
    """The caller's ``Role``: from the dev-auth headers while ``Settings.dev_auth_honoured`` holds, else
    from the validated Entra token's ``roles`` claim. A missing or invalid token is a 401."""
    auth = request.headers.get("Authorization", "")

    from core.config import Settings as _Settings

    settings_fresh = _Settings()
    if settings_fresh.dev_auth_honoured:
        return _role_from_dev_headers(request)

    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authorization token")
    token = auth.split(" ", 1)[1]

    from core.security_entra import verify_entra_token

    claims = verify_entra_token(token)
    return _role_from_entra_claims(claims, settings_fresh)


def require_role(min_role: Role):
    async def checker(request: Request):
        user_role = _extract_role(request)
        if user_role < min_role:
            raise AuthError(f"Requires {min_role.name.lower()} role or higher", kind="role_required")
        return user_role

    return checker


def principal_email(claims: Mapping[str, Any]) -> Optional[str]:
    """The email precedence for Entra claims: ``preferred_username``, then ``email``, then ``upn``.

    The one order both ``current_principal`` (which fills ``created_by`` / ``granted_by``) and
    ``GET /users/me`` (which fills what the interface shows) use, so the address attributed to a
    caller's writes is the address that caller is shown. ``preferred_username`` leads because it always
    carries a value; ``email`` is the deliverable-mailbox claim but is optional and has to be configured
    on the app registration, so it is frequently absent.

    Returns None when the token carries none of the three (a service principal, say). ``"unknown"``
    belongs to presentation, so ``/users/me`` applies it at the call site and it can never be persisted
    into an audit field; route code pairs the None with the caller's oid.
    """
    return (claims.get("preferred_username") or claims.get("email") or claims.get("upn")) or None


@dataclass
class Principal:
    """The validated caller identity routes read through ``Depends(current_principal)``.

    ``oid`` is the Entra object id (None in dev-auth); ``email`` follows ``principal_email`` under Entra
    and is the ``x-user-email`` header in dev-auth; ``raw_claims`` is the validated claims dict (empty
    in dev-auth).
    """

    oid: Optional[str]
    email: Optional[str]
    role: Role
    raw_claims: dict


def current_principal(request: Request) -> Principal:
    """FastAPI dependency returning the caller identity under the same precedence as ``_extract_role``:
    the dev-auth headers while ``Settings.dev_auth_honoured`` holds, else the validated Entra token (401
    on failure)."""
    auth = request.headers.get("Authorization", "")

    from core.config import Settings as _Settings

    settings_fresh = _Settings()
    if settings_fresh.dev_auth_honoured:
        role = _role_from_dev_headers(request)
        email = request.headers.get("x-user-email")
        if email:
            email = email.lower()
        return Principal(oid=None, email=email, role=role, raw_claims={})

    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authorization token")
    token = auth.split(" ", 1)[1]

    from core.security_entra import verify_entra_token

    claims = verify_entra_token(token)
    oid = claims.get("oid")
    email = principal_email(claims)
    role = _role_from_entra_claims(claims, settings_fresh)
    return Principal(oid=oid, email=email, role=role, raw_claims=claims)
