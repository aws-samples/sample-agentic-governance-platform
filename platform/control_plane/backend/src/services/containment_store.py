"""Containment stores, on ``core.state_store.StateStore``.

- ``ContainmentStore`` — the MUTABLE table (``CONTAINMENT_TABLE_NAME``). One partition per agent,
  ``pk="AGENT#<id>"`` with ``sk`` = ``STATE`` | ``MONITOR`` | ``SIGNAL#<span_end>#<span_id>``.
  The monitored-agent index is ``pk="MONITORED", sk=<agent_id>`` (deleted on disable) and the
  monitor lease is ``pk="LEASE", sk=<name>``. The ``STATE`` row is written as a transition from the
  ``(state, job)`` values the caller expects, so two writers racing the same row cannot both land;
  the ``MONITOR`` row's ``enabled`` is the route's and ``cursor_ms`` the loop's.
- ``AuditStore`` — the APPEND-ONLY table (``AUDIT_TABLE_NAME``): ``pk="AGENT#<id>",
  sk="EVENT#<iso>#<uuid4 hex>"``. Every write is ``put_new``; it never updates or deletes.

Signals and leases carry a ``ttl`` attribute (epoch s). Empty table name ⇒ in-memory, matching
the ``*_TABLE_NAME`` convention.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Collection, Mapping
from typing import Any, Optional

from core.config import settings
from core.state_store import StateStore
from models.containment import AuditEvent, ContainmentState, Signal

SIGNAL_TTL_S = 7 * 24 * 3600
MONITORED_PK = "MONITORED"
LEASE_PK = "LEASE"


def _pk(agent_id: str) -> str:
    return f"AGENT#{agent_id}"


class ContainmentStore:
    def __init__(self, *, table_name: str | None = None, clock: Callable[[], float] | None = None):
        name = settings.CONTAINMENT_TABLE_NAME if table_name is None else table_name
        self._clock = clock or time.time
        self._store = StateStore(name, clock=self._clock)

    def get_state(self, agent_id: str) -> ContainmentState | None:
        row = self._get(_pk(agent_id), "STATE")
        return ContainmentState.model_validate_json(row["doc"]) if row else None

    def put_state(self, state: ContainmentState, expected: Mapping[str, Collection[Any]]) -> bool:
        """Write ``state`` when the row's attributes are in ``expected``; False when another write won."""
        return self._store.transition(
            {
                "pk": _pk(state.agent_id),
                "sk": "STATE",
                "state": state.state,
                "job": state.job,
                "doc": state.model_dump_json(),
            },
            expected,
        )

    def is_contained(self, agent_id: str) -> bool:
        s = self.get_state(agent_id)
        return s is not None and s.state == "CONTAINED"

    def get_monitor(self, agent_id: str) -> dict:
        row = self._get(_pk(agent_id), "MONITOR") or {}
        return {"enabled": bool(row.get("enabled", False)), "cursor_ms": int(row.get("cursor_ms", 0))}

    def set_monitor(self, agent_id: str, *, enabled: bool, cursor_ms: int | None = None) -> dict:
        """``cursor_ms=None`` leaves the cursor to the monitor loop."""
        attrs: dict[str, Any] = {"enabled": enabled}
        if cursor_ms is not None:
            attrs["cursor_ms"] = int(cursor_ms)
        self._store.set_owned((_pk(agent_id), "MONITOR"), attrs)
        if enabled:
            self._store.put_new({"pk": MONITORED_PK, "sk": agent_id})
        else:
            self._delete(MONITORED_PK, agent_id)
        return self.get_monitor(agent_id)

    def save_cursor(self, agent_id: str, cursor_ms: int) -> None:
        """Advance ONLY ``cursor_ms`` — never ``enabled`` — so a monitor tick cannot undo a user
        turning monitoring off mid-tick. No-op when the MONITOR row is missing."""
        self._store.update_existing((_pk(agent_id), "MONITOR"), {"cursor_ms": int(cursor_ms)})

    def list_monitored(self) -> list[str]:
        return [r["sk"] for r in self._rows_under(MONITORED_PK, "")]

    def put_signal(self, sig: Signal) -> bool:
        """False when this span was already signalled (the key is built from the span)."""
        return self._store.put_new(
            {
                "pk": _pk(sig.agent_id),
                "sk": f"SIGNAL#{sig.span_end}#{sig.span_id}",
                "span_id": sig.span_id,
                "label": sig.label,
                "doc": sig.model_dump_json(),
                "ttl": int(self._clock()) + SIGNAL_TTL_S,
            }
        )

    def list_signals(self, agent_id: str, limit: int = 20) -> list[Signal]:
        rows = self._rows_under(_pk(agent_id), "SIGNAL#", forward=False, limit=limit)
        return [Signal.model_validate_json(r["doc"]) for r in rows]

    def acquire_lease(self, name: str, holder: str, ttl_s: int = 30) -> bool:
        """True when ``holder`` now holds ``name`` until now+ttl_s: the lease was free, expired,
        or already ``holder``'s (a renewal)."""
        return self._store.acquire_lease((LEASE_PK, name), holder, ttl_s)

    def _get(self, pk: str, sk: str) -> dict | None:
        return self._store.get((pk, sk), consistent=True)

    def _delete(self, pk: str, sk: str) -> None:
        self._store.delete((pk, sk))

    def _rows_under(
        self, pk: str, prefix: str, *, forward: bool = True, limit: int | None = None
    ) -> list[dict]:
        return self._store.query(pk, sk_prefix=prefix, forward=forward, limit=limit, consistent=True)


class AuditStore:
    """Append-only: ``put_new`` and ``query``. The sort key carries a uuid suffix so two events in
    the same millisecond land on distinct rows."""

    def __init__(self, *, table_name: str | None = None):
        name = settings.AUDIT_TABLE_NAME if table_name is None else table_name
        self._store = StateStore(name)

    def append(self, event: AuditEvent) -> None:
        sk = f"EVENT#{event.at}#{uuid.uuid4().hex}"
        item = {
            "pk": _pk(event.agent_id),
            "sk": sk,
            "at": event.at,
            "action": event.action,
            "result": event.result,
            "actor_name": event.actor_name,
            "doc": event.model_dump_json(),
        }
        if event.step is not None:
            item["step"] = event.step
        if not self._store.put_new(item):
            raise RuntimeError(f"audit row {sk} for agent {event.agent_id} already exists")

    def list(self, agent_id: str, limit: int = 100) -> list[AuditEvent]:
        """The newest ``limit`` events, returned oldest first."""
        rows = self._store.query(_pk(agent_id), sk_prefix="EVENT#", forward=False, limit=limit)
        return [AuditEvent.model_validate_json(r["doc"]) for r in reversed(rows)]


_containment_store: Optional[ContainmentStore] = None
_audit_store: Optional[AuditStore] = None


def get_containment_store() -> ContainmentStore:
    global _containment_store
    if _containment_store is None:
        _containment_store = ContainmentStore()
    return _containment_store


def get_audit_store() -> AuditStore:
    global _audit_store
    if _audit_store is None:
        _audit_store = AuditStore()
    return _audit_store
