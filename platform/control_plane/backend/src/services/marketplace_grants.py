"""The Entra grant a subscription confers, applied on approval and torn down on revoke.

Functions over a ``MarketplaceService`` passed as ``svc``, whose registries, injected grant functions and
store are read at call time. An MCP product confers the agent-to-MCP app-role assignment, an agent product
the user-to-agent one; a failed grant persists the subscription FAILED with one of this module's literals.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from models.marketplace import ProductType, Subscription, SubscriptionStatus
from services.agent_mcp_grant import GrantNotFoundError, GrantRevokeError
from services.agent_user_grant import UserGrantError, UserGrantNotFoundError
from services.marketplace_errors import MarketplaceError

if TYPE_CHECKING:
    from services.marketplace_service import MarketplaceService

logger = logging.getLogger(__name__)

_GRANT_FAILED_ERROR = "Grant application failed; see backend logs and retry."
_GRANT_FAILED_RAISE = "Grant application failed"
_GRANT_UNRESOLVED = "Could not resolve agent or MCP for the grant"
_AGENT_NOT_PROVISIONED = "agent identity is not provisioned"
_AGENT_NOT_PUBLISHED = "agent product is no longer published"
_MCP_NOT_PUBLISHED = "MCP product is no longer published"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def revoke_agent_grant(svc: MarketplaceService, sub: Subscription) -> None:
    """Delete the subscriber's Invoker assignment on the product's agent when the row holds an
    assignment id and the agent still resolves; otherwise Graph holds nothing to revoke."""
    agent = svc._agent_registry.get(sub.product_id)
    if not sub.grant_assignment_id or agent is None:
        return
    try:
        await svc._agent_revoke_fn(agent, sub.grant_assignment_id)
    except UserGrantNotFoundError:
        return
    except UserGrantError:
        logger.exception(
            "[marketplace] user-grant revoke failed for subscription %s (agent %s, assignment %s)",
            sub.id,
            sub.product_id,
            sub.grant_assignment_id,
        )
        raise MarketplaceError("Grant revoke failed", kind="grant_failed") from None


async def revoke_mcp_grant(svc: MarketplaceService, sub: Subscription) -> None:
    """Revoke the agent's app-role assignment on the MCP server when the row holds an assignment
    id and the server still resolves; otherwise Graph holds nothing to revoke."""
    mcp = svc._mcp_registry.get(sub.product_id)
    if not sub.grant_assignment_id or mcp is None:
        return
    try:
        await svc._revoke_fn(mcp, sub.grant_assignment_id)
    except GrantNotFoundError:
        return
    except GrantRevokeError:
        logger.exception(
            "[marketplace] revoke failed for subscription %s (MCP %s, assignment %s)",
            sub.id,
            sub.product_id,
            sub.grant_assignment_id,
        )
        raise MarketplaceError("Grant revoke failed", kind="grant_failed") from None


def _grant_failure(
    svc: MarketplaceService, sub: Subscription, *, decided_by: str, error: str, message: str
) -> MarketplaceError:
    """Persist the subscription FAILED with ``error`` (a literal of this module) and return the
    ``grant_failed`` error to raise; the row stays visible and retryable. The write is conditioned
    on the status the caller's guard read, which ``sub`` still carries on entry."""
    was = sub.status.value
    sub.status = SubscriptionStatus.FAILED
    sub.error = error
    sub.decided_by = decided_by
    sub.decided_at = _now()
    sub.updated_at = _now()
    svc._rows.save(sub, after={was})
    return MarketplaceError(message, kind="grant_failed")


def _grant_applied(
    svc: MarketplaceService, sub: Subscription, *, decided_by: str, assignment_id: str
) -> Subscription:
    was = sub.status.value
    sub.status = SubscriptionStatus.APPROVED
    sub.grant_assignment_id = assignment_id
    sub.error = None
    sub.decided_by = decided_by
    sub.decided_at = _now()
    sub.updated_at = _now()
    svc._rows.save(sub, after={was})
    return sub


async def apply_grant(svc: MarketplaceService, sub: Subscription, *, decided_by: str) -> Subscription:
    """Grant the subscribing agent on the MCP server: resolve both records, re-assert that the
    product is still offered (a row outlives its birth gate; revoke deliberately does not re-gate),
    await ``grant_fn`` and persist APPROVED with the assignment id, or FAILED and ``grant_failed``."""
    agent = svc._agent_registry.get(sub.agent_id) if sub.agent_id else None
    mcp = svc._mcp_registry.get(sub.product_id)
    if agent is None or mcp is None:
        raise _grant_failure(
            svc, sub, decided_by=decided_by, error=_GRANT_UNRESOLVED, message=_GRANT_UNRESOLVED
        )
    if not svc._product_is_offered(mcp, ProductType.MCP, svc._rows.load_listings()):
        raise _grant_failure(
            svc, sub, decided_by=decided_by, error=_MCP_NOT_PUBLISHED, message=_MCP_NOT_PUBLISHED
        )

    try:
        assignment_id = await svc._grant_fn(agent, mcp)
    except Exception:
        logger.exception(
            "[marketplace] grant failed for subscription %s (agent %s -> MCP %s)",
            sub.id,
            sub.agent_id,
            sub.product_id,
        )
        raise _grant_failure(
            svc, sub, decided_by=decided_by, error=_GRANT_FAILED_ERROR, message=_GRANT_FAILED_RAISE
        ) from None
    return _grant_applied(svc, sub, decided_by=decided_by, assignment_id=assignment_id)


async def apply_agent_grant(svc: MarketplaceService, sub: Subscription, *, decided_by: str) -> Subscription:
    """Grant the subscriber on the product's agent: the agent must exist with a provisioned
    identity, an SP id and an Invoker role id, and the product must still be offered; then await
    ``agent_grant_fn`` and persist APPROVED with the assignment id, or FAILED and ``grant_failed``."""
    agent = svc._agent_registry.get(sub.product_id)
    if (
        agent is None
        or getattr(agent, "identity_status", None) != "provisioned"
        or not getattr(agent, "entra_sp_id", None)
        or not getattr(agent, "invoker_role_id", None)
    ):
        raise _grant_failure(
            svc, sub, decided_by=decided_by, error=_AGENT_NOT_PROVISIONED, message=_AGENT_NOT_PROVISIONED
        )
    if not svc._product_is_offered(agent, ProductType.AGENT, svc._rows.load_listings()):
        raise _grant_failure(
            svc, sub, decided_by=decided_by, error=_AGENT_NOT_PUBLISHED, message=_AGENT_NOT_PUBLISHED
        )

    try:
        assignment_id = await svc._agent_grant_fn(agent, sub.requester_oid)
    except Exception:
        logger.exception(
            "[marketplace] user grant failed for subscription %s (user -> agent %s)",
            sub.id,
            sub.product_id,
        )
        raise _grant_failure(
            svc, sub, decided_by=decided_by, error=_GRANT_FAILED_ERROR, message=_GRANT_FAILED_RAISE
        ) from None
    return _grant_applied(svc, sub, decided_by=decided_by, assignment_id=assignment_id)
