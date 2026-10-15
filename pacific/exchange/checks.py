"""Programmatic completion checks. Each check inspects real state and returns pass/fail.

A task lists checks like {"type": "db_min_rows", "db": "app", "table": "users", "min": 5}.
State checks read the task's workspace directly from the platform services (free of charge),
with a grant the exchange signs for each read.
"""

from __future__ import annotations

import json
import re
from typing import Awaitable, Callable

import httpx

from ..grants import Signer
from ..money import fmt, to_micro


class CheckContext:
    def __init__(self, services_url: str, signer: Signer, workspace: str, output: str,
                 job: dict, services_used: set[str]):
        self.services_url, self.signer, self.workspace = services_url, signer, workspace
        self.output = output or ""
        self.job = job
        self.services_used = services_used

    async def service(self, path: str, body: dict) -> tuple[int, dict]:
        raw = json.dumps(body).encode()
        async with httpx.AsyncClient(timeout=60) as http:
            r = await http.post(self.services_url + path, content=raw,
                                headers=self.signer.headers(path, raw, ws=self.workspace, call="check"))
        return r.status_code, r.json()


CheckFn = Callable[[CheckContext, dict], Awaitable[tuple[bool, str]]]
CHECKS: dict[str, CheckFn] = {}


def check(name: str):
    def deco(fn: CheckFn) -> CheckFn:
        CHECKS[name] = fn
        return fn
    return deco


@check("output_contains")
async def _output_contains(ctx, c):
    found = c["text"].lower() in ctx.output.lower()
    return found, f"output {'contains' if found else 'does not contain'} {c['text']!r}"


@check("output_matches")
async def _output_matches(ctx, c):
    found = re.search(c["pattern"], ctx.output, re.I | re.M) is not None
    return found, f"output {'matches' if found else 'does not match'} /{c['pattern']}/"


@check("storage_exists")
async def _storage_exists(ctx, c):
    status, _ = await ctx.service("/ocean_storage/get", {"key": c["key"]})
    return status == 200, f"object {c['key']!r} {'exists' if status == 200 else 'is missing'}"


@check("storage_contains")
async def _storage_contains(ctx, c):
    status, body = await ctx.service("/ocean_storage/get", {"key": c["key"]})
    if status != 200:
        return False, f"object {c['key']!r} is missing"
    texts = c["text"] if isinstance(c["text"], list) else [c["text"]]
    content = body["result"]["content"].lower()
    missing = [t for t in texts if t.lower() not in content]
    return not missing, f"{c['key']!r} missing {missing}" if missing else f"{c['key']!r} contains {texts}"


@check("storage_min_bytes")
async def _storage_min_bytes(ctx, c):
    status, body = await ctx.service("/ocean_storage/get", {"key": c["key"]})
    if status != 200:
        return False, f"object {c['key']!r} is missing"
    n = body["result"]["bytes"]
    return n >= c["min"], f"{c['key']!r} is {n} bytes (need >= {c['min']})"


@check("db_table_exists")
async def _db_table_exists(ctx, c):
    status, body = await ctx.service("/ocean_db/schema", {"db": c["db"]})
    if status != 200:
        return False, f"database {c['db']!r} is missing"
    ok = c["table"] in body["result"]["tables"]
    return ok, f"table {c['db']}.{c['table']} {'exists' if ok else 'is missing'}"


@check("db_columns")
async def _db_columns(ctx, c):
    status, body = await ctx.service("/ocean_db/schema", {"db": c["db"]})
    table = body.get("result", {}).get("tables", {}).get(c["table"]) if status == 200 else None
    if table is None:
        return False, f"table {c['db']}.{c['table']} is missing"
    have = {col["name"].lower() for col in table["columns"]}
    missing = [col for col in c["columns"] if col.lower() not in have]
    return not missing, f"missing columns {missing}" if missing else f"has columns {c['columns']}"


@check("db_min_rows")
async def _db_min_rows(ctx, c):
    status, body = await ctx.service("/ocean_db/schema", {"db": c["db"]})
    table = body.get("result", {}).get("tables", {}).get(c["table"]) if status == 200 else None
    if table is None:
        return False, f"table {c['db']}.{c['table']} is missing"
    return table["rows"] >= c["min"], f"{c['db']}.{c['table']} has {table['rows']} rows (need >= {c['min']})"


@check("db_query_equals")
async def _db_query_equals(ctx, c):
    status, body = await ctx.service("/ocean_db/query", {"db": c["db"], "sql": c["sql"]})
    if status != 200:
        return False, f"query failed: {body.get('detail')}"
    rows = body["result"]["rows"]
    got = rows[0][0] if rows and len(rows[0]) == 1 and not isinstance(c["expected"], list) else rows
    return got == c["expected"], f"query returned {got!r} (expected {c['expected']!r})"


@check("security_scan_clean")
async def _security_scan_clean(ctx, c):
    """Re-scan the stored file(s) and require no findings at or above `max_severity`."""
    files = {}
    for key in c["keys"]:
        status, body = await ctx.service("/ocean_storage/get", {"key": key})
        if status != 200:
            return False, f"object {key!r} is missing"
        files[key] = body["result"]["content"]
    status, body = await ctx.service("/ocean_shield/scan_code", {"files": files})
    if status != 200:
        return False, f"scan failed: {body.get('detail')}"
    counts = body["result"]["counts"]
    worst = c.get("max_severity", "HIGH")
    bad = sum(counts[s] for s in ("HIGH", "MEDIUM", "LOW")[: ("HIGH", "MEDIUM", "LOW").index(worst) + 1])
    return bad == 0, f"scan counts {counts} (no {worst}+ allowed)"


@check("max_spend")
async def _max_spend(ctx, c):
    limit = to_micro(c["amount"])
    return ctx.job["spent"] <= limit, f"spent {fmt(ctx.job['spent'])} (limit {fmt(limit)})"


@check("used_service")
async def _used_service(ctx, c):
    names = c["service"] if isinstance(c["service"], list) else [c["service"]]
    hit = [n for n in names if any(u == n or u.startswith(n) for u in ctx.services_used)]
    return bool(hit), f"used {sorted(ctx.services_used)}; needed one of {names}"


async def run_checks(ctx: CheckContext, checks: list[dict]) -> list[dict]:
    results = []
    for c in checks:
        fn = CHECKS.get(c.get("type"))
        if fn is None:
            results.append({"check": c, "passed": False, "detail": f"unknown check type {c.get('type')!r}"})
            continue
        try:
            passed, detail = await fn(ctx, c)
        except Exception as e:
            passed, detail = False, f"check errored: {type(e).__name__}: {e}"
        results.append({"check": c, "passed": passed, "detail": detail})
    return results
