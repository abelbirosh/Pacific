"""Idempotent requests: a retry is answered, never redone (inspired by PAP's message IDs, §7.3).

A caller sends `Idempotency-Key: <any unique string>` with a paid call, a transfer or a task
creation, and reuses it when retrying the same request.

Exchange (`Store`): the first request with a key is processed and, once its outcome is final, its
response is stored. A retry with the same key and the same request gets that response back
(header `Idempotent-Replayed: true`) with no new hold and no new charge. The same key with a
different request is refused with `idempotency_key_conflict`; one that arrives while the first is
still running gets `request_in_progress`. Outcomes that may succeed on a retry (a provider's 5xx,
a timeout, a refusal raised before anything ran) aren't stored, so the key can be retried.

Providers (`ReplayCache`): the exchange puts a stable id derived from the account and key in the
grant it signs (claim `idem`). A provider keeps its response to that id, so when the exchange
retries a call whose first response was lost (the provider did the work, then the reply failed or
timed out), the provider answers from its record instead of doing the work again, and the
exchange charges for it once. A copy that arrives while the first is still running waits for it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict

from fastapi.responses import JSONResponse

from .db import Database
from .errors import PacificError

HEADER = "Idempotency-Key"
REPLAYED = "Idempotent-Replayed"
MAX_KEY = 255


def fingerprint(*parts) -> str:
    """A hash of everything that makes two requests the same request."""
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def provider_id(account_id: str, key: str) -> str:
    """The id providers see: stable per (account, key), and reveals neither."""
    return hashlib.sha256(f"{account_id}\0{key}".encode()).hexdigest()[:32]


class Store:
    def __init__(self, db: Database, stale_after_s: float):
        self.db, self.stale_after_s = db, stale_after_s

    async def run(self, account_id: str, key: str | None, request_fingerprint: str, handler) -> JSONResponse:
        """Run `handler()` -> (status, body, final) at most once per (account, key)."""
        if key is None:
            status, payload, _ = await handler()
            return JSONResponse(payload, status_code=status)
        if not 0 < len(key) <= MAX_KEY:
            raise PacificError("invalid_request", f"{HEADER} must be 1-{MAX_KEY} characters")
        replay = self._begin(account_id, key, request_fingerprint)
        if replay is not None:
            return replay
        try:
            status, payload, final = await handler()
        except BaseException:
            self._forget(account_id, key)
            raise
        if final:
            with self.db.tx() as conn:
                conn.execute("UPDATE idempotency SET state='done', status_code=?, response=? WHERE account_id=? AND key=?",
                             (status, json.dumps(payload), account_id, key))
        else:
            self._forget(account_id, key)
        return JSONResponse(payload, status_code=status)

    def _begin(self, account_id: str, key: str, request_fingerprint: str) -> JSONResponse | None:
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT *, (julianday('now') - julianday(created_at)) * 86400 AS age FROM idempotency "
                "WHERE account_id=? AND key=?", (account_id, key)).fetchone()
            if row is None or (row["state"] == "pending" and row["age"] > self.stale_after_s):
                conn.execute("INSERT OR REPLACE INTO idempotency(account_id, key, fingerprint) VALUES (?,?,?)",
                             (account_id, key, request_fingerprint))
                return None
        if row["fingerprint"] != request_fingerprint:
            raise PacificError("idempotency_key_conflict", f"{HEADER} {key!r} was already used for a different request")
        if row["state"] == "pending":
            raise PacificError("request_in_progress", f"a request with {HEADER} {key!r} is still running",
                               headers={"Retry-After": "1"})
        return JSONResponse(json.loads(row["response"]), status_code=row["status_code"], headers={REPLAYED: "true"})

    def _forget(self, account_id: str, key: str):
        with self.db.tx() as conn:
            conn.execute("DELETE FROM idempotency WHERE account_id=? AND key=? AND state='pending'", (account_id, key))


class ReplayCache:
    """A provider's record of its responses to idempotent requests, in memory and bounded."""

    def __init__(self, ttl_s: float = 3600, max_entries: int = 50_000):
        self.ttl_s, self.max_entries = ttl_s, max_entries
        self.done: OrderedDict[tuple, tuple[float, int, list, bytes]] = OrderedDict()
        self.inflight: dict[tuple, asyncio.Future] = {}
        self.replays = 0

    async def claim(self, key: tuple) -> tuple[int, list, bytes] | None:
        """The stored response for `key`, or None if the caller should do the work (and must then
        call `complete`). A copy of a request that's still running waits for it."""
        while True:
            hit = self.done.get(key)
            if hit and hit[0] > time.monotonic():
                self.replays += 1
                return hit[1:]
            pending = self.inflight.get(key)
            if pending is None:
                self.inflight[key] = asyncio.get_running_loop().create_future()
                return None
            await asyncio.shield(pending)

    def complete(self, key: tuple, response: tuple[int, list, bytes] | None):
        """Record the response (None when it failed and shouldn't be replayed) and wake any waiters."""
        if response is not None:
            self.done[key] = (time.monotonic() + self.ttl_s, *response)
            self.done.move_to_end(key)
            while len(self.done) > self.max_entries:
                self.done.popitem(last=False)
        pending = self.inflight.pop(key, None)
        if pending is not None and not pending.done():
            pending.set_result(None)
