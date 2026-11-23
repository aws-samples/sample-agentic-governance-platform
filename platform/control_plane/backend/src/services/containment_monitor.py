"""The misuse monitor — every 10 s, scan each monitored agent for FAIL signals.

One lease (30 s) per tick, so only one ECS task scans. Per agent: ``source.scan`` from the saved
cursor, write each new signal (+ one ``signal`` audit row), then advance the cursor with
``save_cursor`` (never ``set_monitor``: a tick must not undo a user turning monitoring off). A
failing agent is logged and skipped; the loop never raises.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from datetime import UTC, datetime, timedelta

import anyio

from models.containment import AuditEvent
from services.containment_store import get_audit_store, get_containment_store

logger = logging.getLogger(__name__)

TICK_S = 10
LEASE_NAME = "containment-monitor"
LEASE_TTL_S = 30
MONITOR_ACTOR = ("agp-monitor", "AGP monitor")


async def monitor_tick(*, store, source, agents_loader, holder: str, audit=None) -> int:
    """One pass over the monitored agents; returns the number of NEW signals written."""
    if not store.acquire_lease(LEASE_NAME, holder, ttl_s=LEASE_TTL_S):
        return 0
    audit = audit or get_audit_store()
    written = 0
    last: datetime | None = None
    for agent_id in store.list_monitored():
        try:
            agent = agents_loader(agent_id)
            if agent is None:
                continue
            cursor = store.get_monitor(agent_id)["cursor_ms"]
            signals, _sessions, new_cursor = await anyio.to_thread.run_sync(source.scan, agent, cursor)
            for sig in signals:
                if store.put_signal(sig):
                    written += 1
                    now = datetime.now(UTC)
                    last = now if last is None or now > last else last + timedelta(microseconds=1)
                    audit.append(
                        AuditEvent(
                            agent_id=agent_id,
                            at=last.isoformat(timespec="microseconds"),
                            actor_oid=MONITOR_ACTOR[0],
                            actor_name=MONITOR_ACTOR[1],
                            action="signal",
                            result="ok",
                            detail=sig.explanation,
                            native_ref=sig.span_id,
                        )
                    )
            store.save_cursor(agent_id, new_cursor)
        except Exception:
            logger.exception("[monitor] scan failed for agent %s", agent_id)
    return written


def _default_deps() -> dict:
    """Production wiring: the agents registry, the AgentCore eval source, one holder per task."""
    from api.routes.agents import get_service as get_agents
    from services.containment_native import make_client_factory
    from services.containment_service import agent_stage_cfg
    from services.containment_signals import AgentCoreEvalSource

    factory = make_client_factory(agent_stage_cfg)
    return {
        "store": get_containment_store(),
        "source": AgentCoreEvalSource(
            logs_client_factory=factory,
            agentcore_client_factory=factory,
            control_client_factory=factory,
        ),
        "agents_loader": get_agents().get,
        "holder": f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}",
    }


async def run_monitor_forever(stop_event: asyncio.Event) -> None:
    """The startup hook: tick every ``TICK_S`` until ``stop_event`` is set. Never raises."""
    deps = None
    while not stop_event.is_set():
        try:
            deps = deps or _default_deps()
            await monitor_tick(**deps)
        except Exception:
            logger.exception("[monitor] tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TICK_S)
        except asyncio.TimeoutError:
            pass
