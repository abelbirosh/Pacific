"""Per-account rate limits on the exchange's API: a token bucket per caller.

Over the limit, a request gets 429 `rate_limited` with a Retry-After header, and the caller must
wait at least that long (PAP 4.3). The caller is identified by its credential, so no database
lookup is needed; requests without one share a bucket per client address. The admin key is exempt.

PACIFIC_RATE_PER_S (default 100) refills each bucket; PACIFIC_RATE_BURST (default 200) caps it.
Set PACIFIC_RATE_PER_S=0 to turn limits off.
"""

from __future__ import annotations

import hashlib
import math
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .errors import body


class TokenBuckets:
    def __init__(self, rate: float, burst: float):
        self.rate, self.burst = rate, burst
        self.buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last refill)
        self.limited = 0

    def take(self, key: str) -> float | None:
        """Spend one token. Returns None if allowed, else seconds until one is available."""
        now = time.monotonic()
        tokens, last = self.buckets.get(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if tokens >= 1:
            self.buckets[key] = (tokens - 1, now)
            return None
        self.buckets[key] = (tokens, now)
        self.limited += 1
        return (1 - tokens) / self.rate

    def prune(self, idle_s: float = 300):
        now = time.monotonic()
        self.buckets = {k: v for k, v in self.buckets.items() if now - v[1] < idle_s}


def limits_from_env() -> tuple[float, float]:
    rate = float(os.environ.get("PACIFIC_RATE_PER_S", 100))
    return rate, float(os.environ.get("PACIFIC_RATE_BURST", max(1.0, rate * 2)))


def install(app: FastAPI, admin_key: str, rate: float, burst: float) -> TokenBuckets | None:
    if rate <= 0:
        return None
    buckets = TokenBuckets(rate, burst)
    admin_auth = f"Bearer {admin_key}"
    seen = {"n": 0}

    @app.middleware("http")
    async def rate_limit(request: Request, call_next):
        if not request.url.path.startswith("/v1/"):
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if auth == admin_auth:
            return await call_next(request)
        key = hashlib.sha256(auth.encode()).hexdigest() if auth else f"addr:{request.client.host if request.client else '?'}"
        wait = buckets.take(key)
        seen["n"] += 1
        if seen["n"] % 10_000 == 0:
            buckets.prune()
        if wait is not None:
            retry = max(1, math.ceil(wait))
            return JSONResponse(body("rate_limited", f"over {buckets.rate:g} requests/s; retry in {retry}s",
                                     retry_after=retry), status_code=429, headers={"Retry-After": str(retry)})
        return await call_next(request)

    app.state.rate_limits = buckets
    return buckets
