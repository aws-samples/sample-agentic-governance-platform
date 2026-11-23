"""The live status of a Databricks-hosted agent's app, in the closed ``RuntimeStatus`` union.

Functions over a ``DatabricksIdentityService`` passed as ``svc``, whose collaborators are read at call
time. ``DatabricksIdentityService.runtime_status`` delegates here.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from models.agent import (
    UNKNOWN_STAGE,
    Agent,
    RuntimeStatus,
    RuntimeStatusValue,
    is_databricks_governed_agent,
)
from services.agent_identity_service import ProvisioningError
from services.databricks_workspace_service import DatabricksError

if TYPE_CHECKING:
    from services.databricks_identity_service import DatabricksIdentityService

logger = logging.getLogger(__name__)

# Databricks App ``status.state`` to the closed union ``models.agent.RUNTIME_STATUSES``: an explicit
# dict, so a state Databricks adds later degrades to ``unknown`` instead of being absorbed by a
# heuristic. The non-serving states (``STOPPED``, ``STOPPING``, ``DELETING``, ``UNAVAILABLE``) are
# ``unknown``, not ``failed``: switching an app off is not a fault.
_APP_STATE_TO_RUNTIME_STATUS: dict[str, RuntimeStatusValue] = {
    "RUNNING": "ready",
    "ACTIVE": "ready",
    "DEPLOYING": "creating",
    "STARTING": "creating",
    "UPDATING": "updating",
    "CRASHED": "failed",
    "ERROR": "failed",
    "STOPPED": "unknown",
    "STOPPING": "unknown",
    "DELETING": "unknown",
    "UNAVAILABLE": "unknown",
}

# The containers an app record may carry its state in.
_APP_STATUS_KEYS = ("status", "app_status")

# What may be echoed into a status ``detail``: a code-shaped value only, since ``detail`` reaches
# a UI.
_SAFE_STATUS_TOKEN = re.compile(r"^[A-Za-z_]{1,64}$")


def _safe_token(value: object) -> str:
    """``value`` if it is a bare code-shaped string, else ``""`` (never a partial echo)."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    return candidate if _SAFE_STATUS_TOKEN.match(candidate.replace("_", "")) else ""


def _app_state(app: dict) -> str:
    """The raw ``state`` string from whichever status container the record carries."""
    for key in _APP_STATUS_KEYS:
        container = app.get(key)
        if isinstance(container, dict):
            state = container.get("state")
            if isinstance(state, str) and state.strip():
                return state.strip()
    return ""


def _status(
    agent: Agent,
    status: RuntimeStatusValue,
    resolved_stage: str,
    checked_at: str,
    detail: str | None = None,
) -> RuntimeStatus:
    """One runtime-status answer. ``runtime_arn`` and ``image_tag`` stay ``None``: the former is
    an ARN field the delete cascade parses as one, the latter has no Databricks Apps equivalent."""
    return RuntimeStatus(
        agent_id=agent.id,
        stage=resolved_stage or UNKNOWN_STAGE,
        status=status,
        runtime_arn=None,
        checked_at=checked_at,
        detail=detail,
    )


def runtime_status(
    svc: DatabricksIdentityService, agent: Agent, tenant=None, *, stage: str | None = None
) -> RuntimeStatus:
    """One Databricks agent's live app status in the closed union. Never raises.

    ``not_deployed`` is a positive claim made only from evidence: no ``runtime_handle``, a
    stage the tenant does not have, or every workspace answered and none listed the app.
    ``unknown`` is "nobody could look" or a state the union cannot express; ``failed`` is only
    ``CRASHED`` or ``ERROR``. A sweep in which any workspace failed to answer is ``unknown``,
    never ``not_deployed``. Sync like its AgentCore sibling (the route runs it off the event
    loop), so the async client runs on a fresh loop per call.

    Which workspace hosts the app is answered by which listing returns it, since an app URL
    carries no workspace identity: stage-less probes the tenant's workspaces in sorted order
    and reports the one that listed the app; an explicit ``stage`` probes that one only. An
    empty successful listing counts as absence (``apps list`` visibility follows app
    permissions; the connect-time ``can_discover`` flag is where a missing grant surfaces).
    """
    checked_at = datetime.now(timezone.utc).isoformat()
    reported_stage = stage or UNKNOWN_STAGE

    if not is_databricks_governed_agent(agent):
        if not (agent.runtime_handle or "").strip():
            return _status(
                agent, "not_deployed", reported_stage, checked_at, "no runtime has been deployed yet"
            )
        return _status(
            agent,
            "unknown",
            reported_stage,
            checked_at,
            "runtime status could not be read (the agent has no governed runtime identity)",
        )

    tenant = _tenant_for_status(svc, agent, tenant)
    if tenant is None:
        return _status(
            agent,
            "unknown",
            reported_stage,
            checked_at,
            "runtime status could not be read (the agent's tenant is unavailable)",
        )

    stages = getattr(tenant, "stages", None) or {}
    if stage is not None and stage not in stages:
        return _status(agent, "not_deployed", stage, checked_at, "this stage has no Databricks workspace")
    candidates = (
        [(stage, stages[stage])] if stage is not None else sorted(stages.items(), key=lambda kv: kv[0])
    )
    if not candidates:
        return _status(
            agent,
            "unknown",
            reported_stage,
            checked_at,
            "runtime status could not be read (the tenant has no Databricks workspace)",
        )
    return _sweep_workspaces(svc, agent, candidates, stage, checked_at)


def _tenant_for_status(svc: DatabricksIdentityService, agent: Agent, tenant):
    """The agent's tenant for a status read, or ``None`` when it cannot be resolved for any
    reason: a record with no tenant raises ``ProvisioningError`` here, a deleted tenant raises
    the tenant service's own error, and a read surface answers ``unknown`` to both."""
    try:
        return svc._resolve_tenant(agent, tenant)
    except Exception:
        logger.warning(
            "[databricks_identity] runtime-status read could not resolve the tenant of agent %s",
            agent.id,
            exc_info=True,
        )
        return None


def _sweep_workspaces(
    svc: DatabricksIdentityService, agent: Agent, candidates: list, stage: str | None, checked_at: str
) -> RuntimeStatus:
    """Probe every candidate workspace and answer from the first that lists the app. All are
    attempted, so one workspace behind a rotated credential does not hide a healthy agent on
    another. Every workspace answered and none listed the app is ``not_deployed``; anything
    less is ``unknown`` naming the error kinds."""
    handle = (agent.runtime_handle or "").strip()
    listed_stages: list[str] = []
    error_kinds: list[str] = []

    for name, workspace_stage in candidates:
        apps, error_kind = _probe_workspace(svc, agent, workspace_stage)
        if apps is None:
            error_kinds.append(error_kind)
            continue
        listed_stages.append(name)
        app = _match_app(apps, handle)
        if app is None:
            continue
        state = _app_state(app)
        status = _APP_STATE_TO_RUNTIME_STATUS.get(state.upper(), "unknown")
        detail = None
        if status == "unknown":
            safe_state = _safe_token(state)
            detail = (
                f"the app is {safe_state.lower()}"
                if safe_state
                else "the app reported no recognizable status"
            )
        return _status(agent, status, name, checked_at, detail)

    if listed_stages and not error_kinds:
        return _status(
            agent,
            "not_deployed",
            stage or (listed_stages[0] if len(listed_stages) == 1 else UNKNOWN_STAGE),
            checked_at,
            "no Databricks workspace on this tenant lists this app",
        )
    kinds = sorted(set(error_kinds))
    return _status(
        agent,
        "unknown",
        stage or UNKNOWN_STAGE,
        checked_at,
        f"runtime status could not be read ({', '.join(kinds)})",
    )


def _probe_workspace(
    svc: DatabricksIdentityService, agent: Agent, workspace_stage
) -> tuple[list[dict] | None, str]:
    """One workspace's app listing as ``(apps, "")``, or ``(None, kind)`` with a safe error
    kind: ``no_workspace_url``, the Databricks kind, ``credential_unavailable`` for a stored
    credential that cannot be read, or ``probe_failed`` for any other fault, logged with its
    traceback because a read surface has no honest way to raise."""
    workspace_url = (getattr(workspace_stage, "workspace_url", "") or "").strip()
    if not workspace_url:
        return None, "no_workspace_url"
    try:
        return asyncio.run(_list_apps(svc, workspace_stage, workspace_url)), ""
    except DatabricksError as err:
        return None, _safe_token(err.kind) or "unreadable"
    except ProvisioningError:
        return None, "credential_unavailable"
    except Exception:
        logger.warning(
            "[databricks_identity] runtime-status read failed for agent %s",
            agent.id,
            exc_info=True,
        )
        return None, "probe_failed"


async def _list_apps(svc: DatabricksIdentityService, stage, workspace_url: str) -> list[dict]:
    """Mint a workspace token for ``stage`` and list its apps (one loop, one client)."""
    token = await svc._workspace_token(stage)
    return await svc._databricks.list_apps(workspace_url, token)


def _match_app(apps, handle: str) -> Optional[dict]:
    """The app record whose ``url`` matches this agent's handle, else ``None``. Compared with
    trailing slashes and case normalised, so a cosmetic difference between what discovery wrote
    and what the listing returns does not read as "the app is gone"."""
    if not handle:
        return None
    target = handle.rstrip("/").lower()
    for app in apps:
        if not isinstance(app, dict) or not app.get("name"):
            continue
        url = str(app.get("url") or "").rstrip("/").lower()
        if url and url == target:
            return app
    return None
