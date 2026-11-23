"""Teardown of the platform-managed identity an MCP record owns.

Shared by ``DELETE /mcp-servers/{id}`` and the repository delete cascade. The collaborators are
resolved through the route modules' lazy singletons, imported inside the functions:
``McpCedarService`` imports the registry service, so a module-level edge here would close the
import cycle that keeps the cascade out of ``McpServerRegistryService.delete``.
Decision: docs/adr/backend-mcp-teardown-authentication-before-authorization.md
"""

import logging
from typing import List

from models.mcp_server import McpServer
from models.repository import RepoDeleteItemResult
from services.tenant_credentials import StageUnresolvedError, TenantCredentialsError

logger = logging.getLogger(__name__)

ENGINE_KEPT_REASON = (
    "kept because the identity teardown failed; the gateway stays policy-enforced until a retry"
)


async def _teardown_item(item: str, start) -> RepoDeleteItemResult:
    """Run one best-effort teardown leg and map success or failure to a report line-item.

    The async twin of ``services.project_runtime_teardown._run_step``, deliberately the same vocabulary and
    model: ``outcome`` is ``deleted``, ``failed`` or ``skipped`` and ``reason`` is safe, either one of the
    two prefixed reasons (``assume_role_failed:`` means the account is known and could not be
    entered; ``stage_unresolved:`` means the owning account cannot be told, two different operator
    actions) or ``type(err).__name__``, never a token, a Graph body or an AWS message.

    ``start`` is a zero-argument callable returning the awaitable, not the awaitable itself, so
    that resolving the lazy service singleton happens inside the guard too: a cascade whose contract
    is "nothing escapes" must not be able to fail on building a collaborator. A leg may return a
    non-empty string to mean "deliberately did nothing, for this safe reason", which becomes
    ``skipped`` with that reason.
    """
    try:
        note = await start()
    except TenantCredentialsError as err:
        logger.exception("[teardown] step '%s' could not assume the tenant role", item)
        return RepoDeleteItemResult(item=item, outcome="failed", reason=f"assume_role_failed: {err.message}")
    except StageUnresolvedError as err:
        logger.exception("[teardown] step '%s' could not resolve the owning account", item)
        return RepoDeleteItemResult(item=item, outcome="failed", reason=f"stage_unresolved: {err.message}")
    except Exception as err:
        logger.exception("[teardown] step '%s' failed", item)
        return RepoDeleteItemResult(item=item, outcome="failed", reason=type(err).__name__)
    if isinstance(note, str) and note:
        return RepoDeleteItemResult(item=item, outcome="skipped", reason=note)
    return RepoDeleteItemResult(item=item, outcome="deleted")


async def teardown_mcp_identity(mcp: McpServer) -> List[RepoDeleteItemResult]:
    """Tear down the platform-managed identity an MCP record owns, two legs in this order.

    1. ``identity``: delete the MCP's Entra application, which cascades its service principal and
       every consent and app-role assignment granted on it, through the idempotent
       ``GraphService.delete_agent_app`` (resolves appId to objectId, falls back to the SP id,
       no-ops on blank ids, swallows a 404).
    2. ``policy_engine``: detach the Cedar policy engine from the gateway and delete it
       (``McpCedarService.delete_policy_engine``).

    Authentication before authorization is the security property: nothing deletes the gateway
    (the platform did not create it), so a sequence that dies in the middle must leave a gateway
    that can no longer mint a token, not one serving every tool call with Cedar stripped. For the
    same reason a failed identity leg skips the engine leg and reports it ``skipped`` with
    ``ENGINE_KEPT_REASON``; the retry is "delete again". Already-minted tokens stay valid to their
    expiry.

    Each leg is reported ``absent`` when the record carries nothing for it, ``skipped`` when the
    live gateway carries an engine this record does not own (never deleted), and ``failed`` with a
    stack trace in the log when it fails; a failure never blocks the record delete, because both
    legs are idempotent and the ids stay on the record until it goes. The ``[teardown]`` log line
    per item is the only audit trail once the record is gone: the route's response model is the
    record, not the report.
    """
    items: List[RepoDeleteItemResult] = []

    if mcp.entra_app_id or mcp.entra_sp_id:
        from api.routes.mcp_server_grants import get_mcp_graph_service

        items.append(
            await _teardown_item(
                "identity",
                lambda: get_mcp_graph_service().delete_agent_app(
                    entra_app_id=mcp.entra_app_id, entra_sp_id=mcp.entra_sp_id
                ),
            )
        )
    else:
        items.append(
            RepoDeleteItemResult(
                item="identity", outcome="absent", reason="no Entra app on record — nothing to remove"
            )
        )

    if items[0].outcome == "failed":
        items.append(RepoDeleteItemResult(item="policy_engine", outcome="skipped", reason=ENGINE_KEPT_REASON))
    elif mcp.cedar_policy_engine_id or mcp.gateway_id:
        from api.routes.mcp_cedar import get_cedar_service

        items.append(
            await _teardown_item("policy_engine", lambda: get_cedar_service().delete_policy_engine(mcp))
        )
    else:
        items.append(
            RepoDeleteItemResult(
                item="policy_engine",
                outcome="absent",
                reason="no policy engine on record — nothing to remove",
            )
        )

    for entry in items:
        logger.info(
            "[teardown] mcp_server=%s item=%s outcome=%s reason=%s",
            mcp.id,
            entry.item,
            entry.outcome,
            entry.reason,
        )
    return items
