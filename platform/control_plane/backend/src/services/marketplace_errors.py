"""The marketplace's one error type: ``kind`` selects the fixed wire outcome in ``kinds``; the two copy
constants are the details the table and the service share."""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from core.errors import AppError, Outcome

_REAPPROVAL_ERROR = (
    "Publication written, but the record lost its approval and could not be re-approved "
    "automatically; re-approve the record, then approve this request again."
)
_DATABRICKS_PUBLISH_UNSUPPORTED = "Databricks-governed agents cannot be published to the marketplace yet"


class MarketplaceError(AppError):
    """A marketplace operation was refused or failed; ``kind`` selects the wire.

    Every kind has a fixed detail, so the message never reaches a client. The text persisted on a
    FAILED subscription or a PENDING publish request is one of the marketplace modules' fixed literals,
    never an upstream exception's text, because the registry and Graph errors it stands in for can carry
    ids, ARNs and provider messages.
    """

    domain: ClassVar[str] = "marketplace"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "not_found": Outcome(404, "Marketplace product or subscription not found"),
        "conflict": Outcome(409, "Marketplace operation conflicts with the current state"),
        "bad_request": Outcome(422, "Invalid marketplace request"),
        "grant_failed": Outcome(502, "Failed to apply the agent grant; see backend logs and retry"),
        "agent_not_approved": Outcome(
            409, "Product must be approved before it can be published to the marketplace"
        ),
        "publish_conflict": Outcome(409, "A marketplace publish request for this product is already pending"),
        "illegal_publish_state": Outcome(409, "Publish request is not pending"),
        "identity_not_provisioned": Outcome(409, "identity is not provisioned"),
        "databricks_publish_unsupported": Outcome(409, _DATABRICKS_PUBLISH_UNSUPPORTED),
        "publish_unsupported_platform": Outcome(
            409, "This agent's platform does not offer marketplace publication yet"
        ),
        "publish_write_failed": Outcome(
            502, "Failed to write the marketplace publication; see backend logs and retry"
        ),
        "publish_needs_reapproval": Outcome(502, _REAPPROVAL_ERROR),
        "invalid_product_type": Outcome(422, "Invalid marketplace product type"),
        "agent_id_required": Outcome(422, "agent_id is required for an MCP subscription"),
    }

    def __init__(self, message: str, *, kind: str = "bad_request") -> None:
        super().__init__(message, kind)
