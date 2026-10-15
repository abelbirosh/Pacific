"""Task events: an ordered log per task, read with a cursor (inspired by PAP §7.5–7.7).

Running a task returns at once (202); what happens next is appended here as it happens:

    state   status changes: running, checking, then passed | failed | error | cancelled
    hire    a call anywhere in the task's job tree hired an agent (seller, budget, depth)
    call    a call anywhere in the task's job tree settled (service, cost, status, depth)
    output  the agent's final output
    check   one completion check's result

`GET /v1/tasks/{id}/events?cursor=&wait=` returns the events after `cursor`, holding the request
up to `wait` seconds until there is one; with `Accept: text/event-stream` it streams them, resuming
from `Last-Event-ID`. Event ids are cursors; clients must treat them as opaque.
"""

from __future__ import annotations

import asyncio
import json
import re

from ..db import Database
from ..errors import PacificError

TERMINAL = ("passed", "failed", "error", "cancelled")
PAGE = 100
_ID = re.compile(r"^evt_(\d{1,12})$")


def event_id(seq: int) -> str:
    return f"evt_{seq:06d}"


class TaskEvents:
    def __init__(self, db: Database):
        self.db = db
        self.signals: dict[str, asyncio.Event] = {}  # task -> set when it gets a new event

    def emit(self, task_id: str, kind: str, **data) -> str:
        with self.db.tx() as conn:
            seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM task_events WHERE task_id = ?",
                               (task_id,)).fetchone()[0]
            conn.execute("INSERT INTO task_events(task_id, seq, type, data) VALUES (?,?,?,?)",
                         (task_id, seq, kind, json.dumps(data, default=str)))
        signal = self.signals.pop(task_id, None)
        if signal is not None:
            signal.set()
        return event_id(seq)

    def read(self, task_id: str, cursor: str | None) -> tuple[list[dict], str | None, bool]:
        """Events after `cursor` (from the start without one): (events, new cursor, has_more)."""
        after = self._seq(task_id, cursor)
        rows = self.db.all("SELECT * FROM task_events WHERE task_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                           (task_id, after, PAGE + 1))
        events = [{"id": event_id(r["seq"]), "type": r["type"], "created_at": r["created_at"], **json.loads(r["data"])}
                  for r in rows[:PAGE]]
        return events, events[-1]["id"] if events else cursor, len(rows) > PAGE

    async def wait(self, task_id: str, timeout: float) -> None:
        signal = self.signals.setdefault(task_id, asyncio.Event())
        try:
            await asyncio.wait_for(signal.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    def _seq(self, task_id: str, cursor: str | None) -> int:
        if not cursor:
            return 0
        m = _ID.match(cursor)
        last = self.db.one("SELECT COALESCE(MAX(seq), 0) AS n FROM task_events WHERE task_id = ?", (task_id,))["n"]
        if m is None or int(m.group(1)) > last:
            raise PacificError("invalid_cursor", f"unknown cursor {cursor!r} for task {task_id}")
        return int(m.group(1))
