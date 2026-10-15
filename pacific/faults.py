"""Fault injection for the platform services host and the agent host (for testing).

Rules live in `<home>/faults.json` and are re-read whenever the file changes, so a traffic run
can turn faults on, off or up while the servers keep running:

    {"seed": 1, "rules": [
      {"match": "/ocean_db/", "latency_ms": [20, 200], "error_rate": 0.05, "status": 503,
       "hang_rate": 0.01, "hang_s": 12, "after_rate": 0.02}
    ]}

The first rule whose `match` is a prefix of (or contained in) the request path applies.

latency_ms   extra delay before handling, drawn uniformly from the range
error_rate   refuse before doing any work, with `status` (default 503)
hang_rate    stall for `hang_s` first: past the exchange's provider timeout when it's lowered
             (`pacific sim --boot` sets PACIFIC_PLATFORM_TIMEOUT=10), otherwise just very slow
after_rate   do the work, then answer 500 anyway: the side effect happened but the caller
             can't know it did (what a retry does next is the interesting part)
"""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

PROFILES: dict[str, list[dict]] = {
    "none": [],
    "mild": [
        {"match": "/ocean_", "latency_ms": [5, 60], "error_rate": 0.02, "after_rate": 0.005},
        {"match": "/agents/", "latency_ms": [0, 20], "error_rate": 0.01},
    ],
    "harsh": [
        {"match": "/ocean_", "latency_ms": [20, 300], "error_rate": 0.08, "hang_rate": 0.01, "hang_s": 12,
         "after_rate": 0.02},
        {"match": "/agents/", "latency_ms": [0, 80], "error_rate": 0.03, "after_rate": 0.01},
    ],
    "chaos": [
        {"match": "/ocean_", "latency_ms": [20, 500], "error_rate": 0.2, "status": 500, "hang_rate": 0.03,
         "hang_s": 12, "after_rate": 0.05},
        {"match": "/agents/", "latency_ms": [0, 150], "error_rate": 0.08, "after_rate": 0.03},
    ],
}


def faults_path(home: Path) -> Path:
    return home / "faults.json"


def write_faults(home: Path, profile: str | list[dict], seed: int | None = None) -> list[dict]:
    """Switch the running servers to a named profile (or explicit rules)."""
    rules = PROFILES[profile] if isinstance(profile, str) else profile
    path = faults_path(home)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"seed": seed, "rules": rules}, indent=2))
    tmp.replace(path)
    return rules


class FaultInjector:
    def __init__(self, path: Path):
        self.path = path
        self.rules: list[dict] = []
        self.rng = random.Random()
        self._stamp = None
        self.injected: dict[str, int] = {}

    def _reload(self):
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self.rules, self._stamp = [], None
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return  # half-written; keep the old rules until the next change
        self.rules, self._stamp = data.get("rules", []), stamp
        self.rng = random.Random(data.get("seed"))

    def rule_for(self, path: str) -> dict | None:
        self._reload()
        return next((r for r in self.rules if path.startswith(r["match"]) or r["match"] in path), None)

    def _count(self, kind: str):
        self.injected[kind] = self.injected.get(kind, 0) + 1


def install(app: FastAPI, path: Path) -> FaultInjector:
    injector = FaultInjector(path)

    @app.middleware("http")
    async def inject_faults(request: Request, call_next):
        rule = injector.rule_for(request.url.path)
        if rule is None:
            return await call_next(request)
        rng = injector.rng
        if rule.get("latency_ms"):
            await asyncio.sleep(rng.uniform(*rule["latency_ms"]) / 1000)
        if rng.random() < rule.get("hang_rate", 0):
            injector._count("hang")
            await asyncio.sleep(rule.get("hang_s", 3))
        if rng.random() < rule.get("error_rate", 0):
            injector._count("error")
            return JSONResponse({"detail": "injected fault: service unavailable"}, status_code=rule.get("status", 503))
        after = rng.random() < rule.get("after_rate", 0)
        response = await call_next(request)
        if after:
            injector._count("after")
            return JSONResponse({"detail": "injected fault: failed after doing the work"}, status_code=500)
        return response

    app.state.faults = injector
    return injector
