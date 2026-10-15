"""Drives realistic traffic through a running economy and grades how it held up.

Tasks arrive as a Poisson process, are assigned by their owners to random agents, and run with
at most `concurrency` in flight. While they run, a monitor polls the exchange's invariants; when
traffic stops, the run waits for everything to settle and checks once more with nothing in flight.
The report breaks results down by persona, task kind and budget class.
"""

from __future__ import annotations

import asyncio
import random
import re
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field

import httpx

from ..exchange.events import TERMINAL
from .workload import Workload


@dataclass
class TrafficConfig:
    tasks: int = 50                  # stop after this many arrivals (0: only the duration limits it)
    duration_s: float = 0           # stop arrivals after this long (0: only the task count limits it)
    rate: float = 2.0                # mean arrivals per second
    concurrency: int = 8             # tasks in flight at once
    seed: int = 1
    think_scale: float = 0.0         # persona pause before decisions (1 = realistic)
    personas: list[str] | None = None  # override every task's persona with one of these
    research: bool = True            # include research tasks (needs offline search or the internet)
    check_every_s: float = 2.0
    task_timeout_s: float = 600
    cancel_rate: float = 0.0         # share of tasks an impatient owner cancels partway through


@dataclass
class TaskRecord:
    n: int
    kind: str
    budget_class: str
    budget: float
    agent: str
    persona: str
    impossible: bool = False
    id: str | None = None
    status: str = "pending"
    queued_s: float = 0.0
    latency_s: float = 0.0
    spent: float = 0.0
    depth: int = 0
    error: str | None = None
    duplicated: bool = False  # a row count came out higher than seeded: some work ran twice
    cancel_after_s: float | None = None  # the owner cancels this long after starting it, if still running
    cancelled: bool = False
    first_event_s: float | None = None   # time from run to the first event that isn't a state change
    sim: dict = field(default_factory=dict)


async def run_traffic(exchange_url: str, secrets: dict, cfg: TrafficConfig, progress=None) -> dict:
    rng = random.Random(cfg.seed)
    workload = Workload(seed=cfg.seed, research=cfg.research)
    agents = secrets["agents"]
    owner_key = {aid: secrets["people"][a["owner"]]["key"] for aid, a in agents.items()}
    records: list[TaskRecord] = []
    violations: dict[str, dict] = {}
    gate = asyncio.Semaphore(cfg.concurrency)
    limits = httpx.Limits(max_connections=cfg.concurrency * 4 + 8, max_keepalive_connections=cfg.concurrency * 2)
    admin = httpx.AsyncClient(base_url=exchange_url, timeout=60, limits=limits,
                              headers={"Authorization": f"Bearer {secrets['admin_key']}"})
    http = httpx.AsyncClient(base_url=exchange_url, timeout=cfg.task_timeout_s, limits=limits)
    t_start = time.monotonic()

    def note(found: list[dict], when: float):
        for v in found:
            violations.setdefault(f"{v['invariant']}: {v['detail']}", {**v, "first_seen_s": round(when, 2)})

    async def one(rec: TaskRecord, spec: dict):
        arrived = time.monotonic()
        async with gate:
            rec.queued_s = time.monotonic() - arrived
            t0 = time.monotonic()
            auth = {"Authorization": f"Bearer {owner_key[spec['agent']]}"}

            async def send(method: str, path: str, **kw) -> httpx.Response:
                for _ in range(6):  # owners honour rate limits too
                    r = await http.request(method, path, headers=auth, **kw)
                    if r.status_code != 429:
                        return r
                    await asyncio.sleep(min(10.0, float(r.headers.get("Retry-After", 1))))
                return r

            post = lambda path, **kw: send("POST", path, **kw)

            async def follow() -> dict:
                """Long-poll the task's events until it finishes, cancelling it if this owner is impatient."""
                cursor, status = None, "running"
                cancel_at = t0 + rec.cancel_after_s if rec.cancel_after_s is not None else None
                while status not in TERMINAL:
                    if time.monotonic() - t0 > cfg.task_timeout_s:
                        raise httpx.ReadTimeout(f"task still {status} after {cfg.task_timeout_s}s")
                    if cancel_at is not None and time.monotonic() >= cancel_at:
                        await post(f"/v1/tasks/{rec.id}/cancel")
                        rec.cancelled, cancel_at = True, None
                    wait = 10.0 if cancel_at is None else max(0.0, min(10.0, cancel_at - time.monotonic()))
                    page = (await send("GET", f"/v1/tasks/{rec.id}/events",
                                       params={"wait": wait, **({"cursor": cursor} if cursor else {})})).json()
                    cursor, status = page["cursor"], page["status"]
                    if rec.first_event_s is None and any(e["type"] != "state" for e in page["events"]):
                        rec.first_event_s = time.monotonic() - t0
                return (await send("GET", f"/v1/tasks/{rec.id}")).json()

            try:
                created = await post("/v1/tasks", json=spec)
                if created.status_code != 200:
                    rec.status, rec.error = f"http_{created.status_code}", created.text[:300]
                    return
                rec.id = created.json()["id"]
                ran = await post(f"/v1/tasks/{rec.id}/run")
                if ran.status_code != 202:
                    rec.status, rec.error = f"http_{ran.status_code}", ran.text[:300]
                    return
                report = await follow()
                rec.status, rec.spent = report["status"], report["spent"]["wd"]
                rec.depth = max((c["depth"] for c in report["calls"]), default=0)
                rec.sim = report.get("artifacts", {}).get("sim", {})
                rec.duplicated = any(_ran_twice(c) for c in report.get("checks", []))
                if rec.status != "passed":
                    rec.error = (report.get("output") or "")[:300]
            except httpx.HTTPError as e:
                rec.status, rec.error = "http_error", f"{type(e).__name__}: {e}"[:300]
            finally:
                rec.latency_s = time.monotonic() - t0
                if progress:
                    progress(rec)

    async def monitor(stop: asyncio.Event):
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=cfg.check_every_s)
            except asyncio.TimeoutError:
                pass
            try:
                r = await admin.get("/v1/admin/invariants")
                note(r.json()["violations"], time.monotonic() - t_start)
            except (httpx.HTTPError, ValueError, KeyError) as e:
                note([{"invariant": "monitor", "detail": f"invariant check failed: {type(e).__name__}: {e}"}],
                     time.monotonic() - t_start)

    stop = asyncio.Event()
    watcher = asyncio.create_task(monitor(stop))
    running: list[asyncio.Task] = []
    try:
        while True:
            elapsed = time.monotonic() - t_start
            if (cfg.tasks and len(records) >= cfg.tasks) or (cfg.duration_s and elapsed >= cfg.duration_s):
                break
            if not cfg.tasks and not cfg.duration_s:
                break
            g = workload.next()
            aid = rng.choice(sorted(agents))
            persona = rng.choice(cfg.personas) if cfg.personas else agents[aid]["brain"].partition(":")[2] or agents[aid]["brain"]
            spec = {**g.spec, "agent": aid}
            spec["inputs"] = {**spec["inputs"], "_seed": rng.randrange(2**31), "_think_scale": cfg.think_scale}
            if cfg.personas:
                spec["inputs"]["_persona"] = persona
            rec = TaskRecord(len(records) + 1, g.kind, g.budget_class, spec["budget"], agents[aid]["name"], persona,
                             impossible=bool(g.meta.get("impossible")))
            if rng.random() < cfg.cancel_rate:
                rec.cancel_after_s = rng.uniform(0.2, 3.0)
            records.append(rec)
            running.append(asyncio.create_task(one(rec, spec)))
            await asyncio.sleep(rng.expovariate(cfg.rate) if cfg.rate > 0 else 0)
        await asyncio.gather(*running)
    finally:
        stop.set()
        await watcher
    wall = time.monotonic() - t_start
    try:
        final = (await admin.get("/v1/admin/invariants", params={"quiescent": True})).json()
        note(final["violations"], wall)
        audit = (await admin.get("/v1/admin/audit")).json()
    finally:
        await admin.aclose()
        await http.aclose()
    return summarize(records, list(violations.values()), wall, cfg, audit, final.get("counts", {}))


# ---- reporting ----------------------------------------------------------------------------------


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def _group(records: list[TaskRecord]) -> dict:
    gradable = [r for r in records if not r.impossible and r.status != "cancelled"]
    lat = [r.latency_s for r in records]
    sims = [r.sim for r in records if r.sim]
    return {
        "n": len(records),
        "pass_rate": round(sum(r.status == "passed" for r in gradable) / len(gradable), 3) if gradable else None,
        "statuses": dict(Counter(r.status for r in records)),
        "latency_p50_s": round(_pct(lat, 50), 3),
        "latency_p95_s": round(_pct(lat, 95), 3),
        "latency_max_s": round(max(lat, default=0), 3),
        "mean_spend": round(statistics.fmean([r.spent for r in records]), 4) if records else 0,
        "budget_used": round(statistics.fmean([r.spent / r.budget for r in records if r.budget]), 3) if records else 0,
        "max_depth": max((r.depth for r in records), default=0),
        "quotes": sum(s.get("quotes", 0) for s in sims),
        "fumbles": sum(s.get("fumbles", 0) for s in sims),
        "retries": sum(s.get("retries", 0) for s in sims),
        "hires": sum(s.get("hires", 0) for s in sims),
        "gave_up": sum(bool(s.get("gave_up")) for s in sims),
        "probes": sum(len(s.get("probes", [])) for s in sims),
        "rate_limited": sum(s.get("errors", {}).get("rate_limited", 0) for s in sims),
        "replays": sum(s.get("replays", 0) + s.get("provider_replays", 0) for s in sims),
        "key_drops": sum(s.get("key_drops", 0) for s in sims),
        "duplicated": sum(r.duplicated for r in records),
        "cancelled": sum(r.status == "cancelled" for r in records),
        "first_event_p50_s": round(_pct([r.first_event_s for r in records if r.first_event_s is not None], 50), 3),
    }


def summarize(records: list[TaskRecord], violations: list[dict], wall: float, cfg: TrafficConfig,
              audit: dict, counts: dict) -> dict:
    by = lambda key: {k: _group(v) for k, v in sorted(_bucket(records, key).items())}
    probe_violations = [{"task": r.n, **v} for r in records for v in r.sim.get("violations", [])]
    probes_run = Counter(p["probe"] for r in records for p in r.sim.get("probes", []))
    unexpected = Counter(f"{p['probe']}->{p['status']}" for r in records for p in r.sim.get("probes", [])
                         if p.get("unexpected"))
    crashed = [asdict(r) for r in records if is_crash(r)]
    injected = sum(r.status == "error" and not is_crash(r) for r in records)
    return {
        "config": asdict(cfg),
        "wall_s": round(wall, 2),
        "throughput_per_s": round(len(records) / wall, 3) if wall else 0,
        "overall": _group(records),
        "by_persona": by("persona"),
        "by_kind": by("kind"),
        "by_budget": by("budget_class"),
        "invariant_violations": violations,
        "probe_violations": probe_violations,
        "probes_run": dict(sorted(probes_run.items())),
        "error_codes": dict(sum((Counter(r.sim.get("errors", {})) for r in records), Counter()).most_common()),
        "probe_refusals_with_other_status": dict(unexpected),
        "crashes": crashed[:20],
        "crash_count": len(crashed),
        "injected_task_errors": injected,
        "audit": audit,
        "ledger_counts": counts,
        "ok": not violations and not probe_violations and not crashed,
    }


def _ran_twice(check: dict) -> bool:
    """A failed exact row-count check that came out high means some insert ran twice."""
    c = check.get("check", {})
    if check.get("passed") or c.get("type") != "db_query_equals" or not str(c.get("sql", "")).startswith("SELECT COUNT(*)"):
        return False
    m = re.match(r"query returned (\d+) ", check.get("detail", ""))
    return bool(m) and int(m.group(1)) > c["expected"]


def is_crash(r: TaskRecord) -> bool:
    """A failure of the system itself, as opposed to one the run injected or a task that just failed."""
    err = r.error or ""
    return (r.status.startswith("http") or "agent crashed" in err
            or (r.status == "error" and "injected fault" not in err))


def _bucket(records, key):
    out = defaultdict(list)
    for r in records:
        out[getattr(r, key)].append(r)
    return out


def format_report(rep: dict) -> str:
    cols = ["n", "pass", "p50 s", "p95 s", "spend", "used", "depth", "quote", "fumbl", "retry", "429", "replay",
            "dup", "hires", "quit", "probe"]
    row = lambda name, g: [name, str(g["n"]), "-" if g["pass_rate"] is None else f"{g['pass_rate']:.0%}",
                           f"{g['latency_p50_s']:.2f}", f"{g['latency_p95_s']:.2f}", f"{g['mean_spend']:.2f}",
                           f"{g['budget_used']:.0%}", str(g["max_depth"]), str(g["quotes"]), str(g["fumbles"]),
                           str(g["retries"]), str(g["rate_limited"]), str(g["replays"]), str(g["duplicated"]),
                           str(g["hires"]), str(g["gave_up"]), str(g["probes"])]

    def table(title, groups):
        rows = [row(k, g) for k, g in groups.items()]
        widths = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate([title] + cols)]
        line = lambda vals: "  ".join(v.rjust(w) if i else v.ljust(w) for i, (v, w) in enumerate(zip(vals, widths)))
        return "\n".join([line([title] + cols), line(["-" * w for w in widths]), *map(line, rows)])

    o = rep["overall"]
    out = [
        f"{o['n']} tasks in {rep['wall_s']}s ({rep['throughput_per_s']}/s)   statuses {o['statuses']}   "
        f"first event p50 {o['first_event_p50_s']}s",
        f"audit ok: {rep['audit'].get('ok')}   ledger {rep['ledger_counts']}   "
        f"tasks failed by injected agent-host faults: {rep['injected_task_errors']}",
        "",
        table("persona", rep["by_persona"]), "",
        table("kind", rep["by_kind"]), "",
        table("budget", rep["by_budget"]), "",
        f"refusals seen by agents (incl. retried): {rep['error_codes'] or 'none'}",
    ]
    if rep["invariant_violations"]:
        out.append(f"INVARIANT VIOLATIONS ({len(rep['invariant_violations'])}):")
        out += [f"  [{v['first_seen_s']}s] {v['invariant']}: {v['detail']}" for v in rep["invariant_violations"][:25]]
    if rep["probe_violations"]:
        out.append(f"PROBE VIOLATIONS ({len(rep['probe_violations'])}):")
        out += [f"  task {v['task']}: {v['probe']} -> {v['status']} (charged {v['charged']}) {v.get('detail', '')[:120]}"
                for v in rep["probe_violations"][:25]]
    if rep["crashes"]:
        out.append(f"CRASHES / TRANSPORT ERRORS ({rep['crash_count']}, {len(rep['crashes'])} shown):")
        out += [f"  #{c['n']} {c['kind']} {c['status']}: {(c['error'] or '')[:160]}" for c in rep["crashes"]]
    out.append("RESULT: " + ("OK" if rep["ok"] else "PROBLEMS FOUND"))
    return "\n".join(out)
