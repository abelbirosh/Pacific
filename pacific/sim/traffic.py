"""Drives realistic traffic through a running economy and grades how it held up.

Tasks arrive as a Poisson process, are assigned by their owners to random agents, and run with
at most `concurrency` in flight. While they run, a monitor polls the exchange's invariants; when
traffic stops, the run waits for everything to settle and checks once more with nothing in flight.
The report breaks results down by persona, task kind and budget class, and follows the money
through contracts: premiums released and refunded, what is left in escrow (nothing, once settled),
how each tier and payment model did, why hires were refused, and every seller's track record.
"""

from __future__ import annotations

import asyncio
import random
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field

import httpx

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
    delegate_rate: float = 0.1       # share of single-skill tasks marked for hiring another agent
    check_every_s: float = 2.0
    task_timeout_s: float = 600
    settle_timeout_s: float = 600    # how long to wait for contracts to finish verifying


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
    sim: dict = field(default_factory=dict)


async def run_traffic(exchange_url: str, secrets: dict, cfg: TrafficConfig, progress=None) -> dict:
    rng = random.Random(cfg.seed)
    workload = Workload(seed=cfg.seed, research=cfg.research, delegate_rate=cfg.delegate_rate)
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
            try:
                created = await http.post("/v1/tasks", json=spec, headers=auth)
                if created.status_code != 200:
                    rec.status, rec.error = f"http_{created.status_code}", created.text[:300]
                    return
                rec.id = created.json()["id"]
                ran = await http.post(f"/v1/tasks/{rec.id}/run", headers=auth)
                if ran.status_code != 200:
                    rec.status, rec.error = f"http_{ran.status_code}", ran.text[:300]
                    return
                report = ran.json()
                rec.status, rec.spent = report["status"], report["spent"]["wd"]
                rec.depth = max((c["depth"] for c in report["calls"]), default=0)
                rec.sim = report.get("artifacts", {}).get("sim", {})
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
            records.append(rec)
            running.append(asyncio.create_task(one(rec, spec)))
            await asyncio.sleep(rng.expovariate(cfg.rate) if cfg.rate > 0 else 0)
        await asyncio.gather(*running)
    finally:
        stop.set()
        await watcher
    wall = time.monotonic() - t_start
    try:
        settle_s = await settle(admin, cfg.settle_timeout_s)
        final = (await admin.get("/v1/admin/invariants", params={"quiescent": True})).json()
        note(final["violations"], time.monotonic() - t_start)
        audit = (await admin.get("/v1/admin/audit")).json()
        book = (await admin.get("/v1/contracts", params={"limit": 1_000_000})).json()
        ranks = (await admin.get("/v1/rankings")).json()["rankings"]
    finally:
        await admin.aclose()
        await http.aclose()
    report = summarize(records, list(violations.values()), wall, cfg, audit, final.get("counts", {}))
    report["contracts"] = contract_report(book, ranks, records, settle_s)
    return report


async def settle(admin: httpx.AsyncClient, timeout_s: float) -> float:
    """Wait until no contract is still working or verifying; returns how long that took."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        try:
            open_ = 0
            for status in ("working", "verifying"):
                open_ += len((await admin.get("/v1/contracts", params={"status": status})).json()["contracts"])
        except (httpx.HTTPError, ValueError, KeyError):
            open_ = 1
        if not open_:
            break
        await asyncio.sleep(0.25)
    return round(time.monotonic() - t0, 2)


def contract_report(book: dict, ranks: list[dict], records: list["TaskRecord"], settle_s: float) -> dict:
    contracts = book["contracts"]
    by: dict[str, dict] = {}
    for c in contracts:
        for key in (f"tier:{c['tier']}", f"payment:{c['payment']}"):
            g = by.setdefault(key, {"n": 0, "statuses": Counter(), "premium": 0.0, "released": 0.0, "refunded": 0.0})
            g["n"] += 1
            g["statuses"][c["status"]] += 1
            g["premium"] += c["premium"]["wd"]
            g["released"] += c["released"]["wd"]
            g["refunded"] += c["refunded"]["wd"]
    groups = {}
    for key, g in sorted(by.items()):
        settled = sum(g["statuses"][s] for s in ("passed", "partial", "failed"))
        groups[key] = {"n": g["n"], "statuses": dict(g["statuses"]),
                       "pass_rate": round(g["statuses"]["passed"] / settled, 3) if settled else None,
                       "premium_wd": round(g["premium"], 4), "released_wd": round(g["released"], 4),
                       "refunded_wd": round(g["refunded"], 4)}
    refused = Counter()
    for r in records:
        for status, n in (r.sim.get("refused_hires") or {}).items():
            refused[status] += n
    attempts = Counter(c["attempts"] for c in contracts if c["status"] != "declined")
    return {
        **book["summary"],
        "settle_wait_s": settle_s,
        "by_group": groups,
        "verification_attempts": dict(sorted(attempts.items())),
        "refused_hires_by_status": dict(refused),
        "rankings": [{k: r[k] for k in ("rank", "service", "tier", "payment", "provider", "hires", "settled",
                                        "passed", "partial", "failed", "unverified", "declined", "success",
                                        "accuracy", "on_time", "score")} for r in ranks],
    }


# ---- reporting ----------------------------------------------------------------------------------


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def _group(records: list[TaskRecord]) -> dict:
    gradable = [r for r in records if not r.impossible]
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
    }


def summarize(records: list[TaskRecord], violations: list[dict], wall: float, cfg: TrafficConfig,
              audit: dict, counts: dict) -> dict:
    by = lambda key: {k: _group(v) for k, v in sorted(_bucket(records, key).items())}
    probe_violations = [{"task": r.n, **v} for r in records for v in r.sim.get("violations", [])]
    known = Counter(p["probe"] for r in records for p in r.sim.get("probes", []) if p.get("known") and not p.get("ok"))
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
        "known_weaknesses": dict(known),
        "probe_refusals_with_other_status": dict(unexpected),
        "crashes": crashed[:20],
        "crash_count": len(crashed),
        "injected_task_errors": injected,
        "audit": audit,
        "ledger_counts": counts,
        "ok": not violations and not probe_violations and not crashed,
    }


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
    cols = ["n", "pass", "p50 s", "p95 s", "spend", "used", "depth", "quote", "fumbl", "retry", "hires", "quit", "probe"]
    row = lambda name, g: [name, str(g["n"]), "-" if g["pass_rate"] is None else f"{g['pass_rate']:.0%}",
                           f"{g['latency_p50_s']:.2f}", f"{g['latency_p95_s']:.2f}", f"{g['mean_spend']:.2f}",
                           f"{g['budget_used']:.0%}", str(g["max_depth"]), str(g["quotes"]), str(g["fumbles"]),
                           str(g["retries"]), str(g["hires"]), str(g["gave_up"]), str(g["probes"])]

    def table(title, groups):
        rows = [row(k, g) for k, g in groups.items()]
        widths = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate([title] + cols)]
        line = lambda vals: "  ".join(v.rjust(w) if i else v.ljust(w) for i, (v, w) in enumerate(zip(vals, widths)))
        return "\n".join([line([title] + cols), line(["-" * w for w in widths]), *map(line, rows)])

    o = rep["overall"]
    out = [
        f"{o['n']} tasks in {rep['wall_s']}s ({rep['throughput_per_s']}/s)   statuses {o['statuses']}",
        f"audit ok: {rep['audit'].get('ok')}   ledger {rep['ledger_counts']}   "
        f"tasks failed by injected agent-host faults: {rep['injected_task_errors']}",
        "",
        table("persona", rep["by_persona"]), "",
        table("kind", rep["by_kind"]), "",
        table("budget", rep["by_budget"]), "",
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
    if rep["known_weaknesses"]:
        out.append(f"known weaknesses confirmed: {rep['known_weaknesses']}")
    if rep.get("contracts"):
        out += ["", *format_contracts(rep["contracts"])]
    out.append("RESULT: " + ("OK" if rep["ok"] else "PROBLEMS FOUND"))
    return "\n".join(out)


def format_contracts(c: dict) -> list[str]:
    out = [
        f"contracts: {c['contracts']}   {c['statuses']}   settled {c['settle_wait_s']}s after traffic stopped",
        f"premiums: {c['premium_wd']:.4f} WD into escrow, {c['released_wd']:.4f} released to sellers, "
        f"{c['refunded_wd']:.4f} refunded to buyers, {c['in_escrow_wd']:.4f} still in escrow",
        f"verification runs per contract: {c['verification_attempts']}   refused hires by status: "
        f"{c['refused_hires_by_status'] or '{}'}",
    ]
    for key, g in c["by_group"].items():
        rate = "-" if g["pass_rate"] is None else f"{g['pass_rate']:.0%}"
        out.append(f"  {key:<20} n={g['n']:<4} pass {rate:>4}   released {g['released_wd']:8.4f}   "
                   f"refunded {g['refunded_wd']:8.4f}   {g['statuses']}")
    ranked = [r for r in c["rankings"] if r["hires"]]
    if ranked:
        out.append("rankings (tiers with hires):")
        for r in ranked[:15]:
            out.append(f"  #{r['rank']:<3} {r['service'] + '/' + r['tier']:<32} score {r['score']:.3f}  "
                       f"success {r['success']:.2f} accuracy {r['accuracy']:.2f} on time {r['on_time']:.2f}  "
                       f"{r['passed']}/{r['partial']}/{r['failed']} pass/partial/fail, {r['unverified']} unverified, "
                       f"{r['declined']} declined")
    return out
