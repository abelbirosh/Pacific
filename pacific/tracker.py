"""Track records: how each promise tier has done on its contracts, and a ranking from them.

For every (service, endpoint, tier), over all its versions:

    hires        contracts opened
    settled      contracts verified: passed + partial + failed
    success      premium earned per settled contract (a partial counts its share), smoothed
    accuracy     acceptance checks passed out of checks verified, smoothed
    on_time      settled contracts delivered before the turnaround deadline, smoothed
    budget_use   the seller's spend as a share of the budget it was given
    unverified   contracts whose checks never ran (outages); kept out of success, since an outage
                 isn't the seller's doing
    declined     hires the seller turned down before spending anything

Smoothing is Laplace's (k + 1) / (n + 2): a tier with no history scores 0.5, and one lucky hire
can't outrank a long record.

    score = 0.6 success + 0.3 accuracy + 0.1 on_time
"""

from __future__ import annotations

import json

from .db import Database
from .money import to_wd

WEIGHTS = {"success": 0.6, "accuracy": 0.3, "on_time": 0.1}


def _smooth(k: float, n: float) -> float:
    return (k + 1) / (n + 2)


def records(db: Database) -> dict[tuple[str, str, str], dict]:
    rows = db.all(
        "SELECT p.service, p.endpoint, p.tier, c.status, c.claim, c.premium, c.released, c.refunded, c.spent, "
        "c.budget, c.verdict FROM contracts c JOIN promises p ON p.id = c.promise_id")
    acc: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        a = acc.setdefault((r["service"], r["endpoint"], r["tier"]), {
            "hires": 0, "passed": 0, "partial": 0, "failed": 0, "unverified": 0, "declined": 0, "open": 0,
            "earned": 0.0, "checks_passed": 0, "checks_run": 0, "on_time": 0, "use": [],
            "released": 0, "refunded": 0})
        a["hires"] += 1
        status = r["status"]
        a[status if status in ("passed", "partial", "failed", "unverified", "declined") else "open"] += 1
        a["released"] += r["released"]
        a["refunded"] += r["refunded"]
        if r["budget"]:
            a["use"].append(r["spent"] / r["budget"])
        if status in ("passed", "partial", "failed"):
            verdict = json.loads(r["verdict"]) if r["verdict"] else {}
            a["earned"] += verdict.get("share", 1.0 if status == "passed" else 0.0)
            results = verdict.get("results", [])
            a["checks_run"] += len(results)
            a["checks_passed"] += sum(bool(x.get("passed")) for x in results)
            a["on_time"] += r["claim"] != "timeout"
    out = {}
    for key, a in acc.items():
        settled = a["passed"] + a["partial"] + a["failed"]
        stats = {
            "success": round(_smooth(a["earned"], settled), 4),
            "accuracy": round(_smooth(a["checks_passed"], a["checks_run"]), 4),
            "on_time": round(_smooth(a["on_time"], settled), 4),
        }
        out[key] = {
            "hires": a["hires"], "settled": settled, "passed": a["passed"], "partial": a["partial"],
            "failed": a["failed"], "unverified": a["unverified"], "declined": a["declined"], "open": a["open"],
            **stats,
            "budget_use": round(sum(a["use"]) / len(a["use"]), 4) if a["use"] else None,
            "released_wd": to_wd(a["released"]), "refunded_wd": to_wd(a["refunded"]),
            "score": round(sum(WEIGHTS[k] * stats[k] for k in WEIGHTS), 4),
        }
    return out


def blank() -> dict:
    """The record of a tier with no contracts yet."""
    return {"hires": 0, "settled": 0, "passed": 0, "partial": 0, "failed": 0, "unverified": 0, "declined": 0,
            "open": 0, "success": 0.5, "accuracy": 0.5, "on_time": 0.5, "budget_use": None,
            "released_wd": 0.0, "refunded_wd": 0.0, "score": 0.5}


def rankings(db: Database, skill: str | None = None) -> list[dict]:
    recs = records(db)
    rows = db.all(
        "SELECT p.service, p.endpoint, p.tier, p.version, p.fee, p.markup_pct, p.payment, s.meta, a.name AS provider "
        "FROM promises p JOIN services s ON s.name = p.service JOIN accounts a ON a.id = s.provider_id "
        "WHERE p.status = 'active'")
    out = []
    for r in rows:
        meta = json.loads(r["meta"])
        if skill and meta.get("skill") != skill:
            continue
        out.append({"service": r["service"], "endpoint": r["endpoint"], "tier": r["tier"], "version": r["version"],
                    "provider": r["provider"], "skill": meta.get("skill"), "payment": r["payment"],
                    "fee": to_wd(r["fee"]), "markup_pct": r["markup_pct"],
                    **recs.get((r["service"], r["endpoint"], r["tier"]), blank())})
    out.sort(key=lambda x: (-x["score"], -x["settled"], x["fee"], x["service"], x["tier"]))
    for i, x in enumerate(out, 1):
        x["rank"] = i
    return out


def summary(db: Database) -> dict:
    """Totals across every contract: how many settled each way and where the premiums went."""
    row = db.one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(premium), 0) AS premium, COALESCE(SUM(released), 0) AS released, "
        "COALESCE(SUM(refunded), 0) AS refunded FROM contracts")
    statuses = {r["status"]: r["n"] for r in db.all("SELECT status, COUNT(*) AS n FROM contracts GROUP BY status")}
    escrow = db.one("SELECT COALESCE(SUM(balance), 0) AS b FROM accounts WHERE kind = 'escrow'")["b"]
    return {"contracts": row["n"], "statuses": statuses, "premium_wd": to_wd(row["premium"]),
            "released_wd": to_wd(row["released"]), "refunded_wd": to_wd(row["refunded"]),
            "in_escrow_wd": to_wd(escrow)}
