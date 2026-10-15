"""Properties the economy must hold at every instant, checked on one consistent snapshot.

`audit` only checks the supply. These go further, so they can be polled while traffic runs:

supply               total supply = sum of balances = sum of mints
negative_balance     no account below zero
overcommitted        no account has more on hold than it holds
job_over_budget      no job's spend plus active holds exceeds its budget
job_books            a job's `spent` equals the charges booked against it
call_overcharged     no call costs more than the hold placed for it
call_books           a settled call's cost equals the charges booked for it
delegation_bill      a hire costs fee + seller's spend at cost-plus (or just the spend if it
                     failed), capped by the hold. Checked once the child job is closed, so a
                     charge that lands on the child job after the buyer was billed shows up here
workspace            every storage / database directory belongs to a root job or an account
idempotent_once      no Idempotency-Key was charged for more than once

The exchange adds `unhandled_error` for any exception it didn't handle (it should refuse bad
input with a 4xx, never fall over on it).

With `quiescent=True` (no traffic in flight) also:

holds_released       no active holds
calls_settled        no call still pending
tasks_finished       no task still running
jobs_closed          a finished or cancelled task leaves no job in its tree open
events_contiguous    each task's events are numbered 1..n with no gaps
events_match_spend   a finished task's top-level `call` events add up to its root job's spend
"""

from __future__ import annotations

import json
from pathlib import Path

from .db import Database
from .pricing import PricePlan


def check(db: Database, data_dir: Path | None = None, quiescent: bool = False) -> dict:
    out: list[dict] = []

    def flag(name: str, rows, describe):
        for r in rows:
            out.append({"invariant": name, "detail": describe(r)})

    with db.snapshot() as conn:
        q = lambda sql, p=(): conn.execute(sql, p).fetchall()
        supply = int((q("SELECT value FROM meta WHERE key='total_supply'") or [[0]])[0][0])
        balances = q("SELECT COALESCE(SUM(balance), 0) FROM accounts")[0][0]
        mints = q("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE kind='mint'")[0][0]
        if not supply == balances == mints:
            out.append({"invariant": "supply", "detail": f"supply {supply} balances {balances} mints {mints}"})

        flag("negative_balance", q("SELECT id, balance FROM accounts WHERE balance < 0"),
             lambda r: f"{r['id']} balance {r['balance']}")
        flag("overcommitted", q(
            "SELECT a.id, a.balance, SUM(h.amount) AS held FROM accounts a "
            "JOIN holds h ON h.account_id = a.id AND h.status = 'active' GROUP BY a.id HAVING held > a.balance"),
            lambda r: f"{r['id']} holds {r['held']} on a balance of {r['balance']}")
        flag("job_over_budget", q(
            "SELECT j.id, j.budget, j.spent, COALESCE(SUM(h.amount), 0) AS held FROM jobs j "
            "LEFT JOIN holds h ON h.job_id = j.id AND h.status = 'active' GROUP BY j.id "
            "HAVING j.spent + held > j.budget"),
            lambda r: f"{r['id']} spent {r['spent']} + held {r['held']} > budget {r['budget']}")
        flag("job_books", q(
            "SELECT j.id, j.spent, COALESCE(SUM(t.amount), 0) AS charged FROM jobs j "
            "LEFT JOIN transactions t ON t.job_id = j.id AND t.kind = 'charge' GROUP BY j.id HAVING charged != j.spent"),
            lambda r: f"{r['id']} spent {r['spent']} but charges total {r['charged']}")
        flag("call_overcharged", q("SELECT id, cost, held FROM calls WHERE cost > held"),
             lambda r: f"{r['id']} cost {r['cost']} > hold {r['held']}")
        flag("call_books", q(
            "SELECT c.id, c.cost, COALESCE(SUM(t.amount), 0) AS charged FROM calls c "
            "LEFT JOIN transactions t ON t.call_id = c.id AND t.kind = 'charge' "
            "WHERE c.status != 'pending' GROUP BY c.id HAVING charged != c.cost"),
            lambda r: f"{r['id']} cost {r['cost']} but charges total {r['charged']}")

        for c in q(
            "SELECT c.id, c.status, c.cost, c.held, e.plan, j.spent AS child_spent "
            "FROM calls c JOIN jobs j ON j.id = c.child_job_id "
            "JOIN endpoints e ON e.service = c.service AND e.name = c.endpoint "
            "WHERE c.status != 'pending' AND j.status = 'closed'"
        ):
            plan = PricePlan.from_dict(json.loads(c["plan"]))
            billed_for = c["child_spent"]
            expected = plan.cost({}, passthrough=billed_for) if c["status"] == "ok" else billed_for
            expected = min(expected, c["held"])
            if c["cost"] != expected:
                out.append({"invariant": "delegation_bill", "detail": f"{c['id']} ({c['status']}) cost {c['cost']}, "
                            f"expected {expected} from the seller's spend of {billed_for}"})

        flag("idempotent_once", q(
            "SELECT caller_id, idempotency_key, COUNT(*) AS n FROM calls WHERE idempotency_key IS NOT NULL "
            "AND cost > 0 GROUP BY caller_id, idempotency_key HAVING n > 1"),
            lambda r: f"{r['caller_id']} was charged {r['n']} times for key {r['idempotency_key']!r}")

        if data_dir is not None:
            owners = {r[0] for r in q("SELECT DISTINCT root_job_id FROM jobs")} | {r[0] for r in q("SELECT id FROM accounts")}
            for kind in ("storage", "db"):
                root = data_dir / kind
                if root.is_dir():
                    for ws in root.iterdir():
                        if ws.name not in owners:
                            out.append({"invariant": "workspace", "detail": f"{kind}/{ws.name} has no owning job or account"})

        if quiescent:
            flag("holds_released", q("SELECT id, account_id, job_id, amount FROM holds WHERE status = 'active'"),
                 lambda r: f"{r['id']} {r['amount']} on {r['account_id']} (job {r['job_id']})")
            flag("calls_settled", q("SELECT id, service, endpoint FROM calls WHERE status = 'pending'"),
                 lambda r: f"{r['id']} {r['service']}/{r['endpoint']}")
            flag("tasks_finished", q("SELECT id, title FROM tasks WHERE status = 'running'"),
                 lambda r: f"{r['id']} {r['title']}")
            flag("jobs_closed", q(
                "SELECT j.id, t.id AS task, t.status FROM jobs j JOIN tasks t ON t.job_id = j.root_job_id "
                "WHERE j.status = 'open' AND t.status NOT IN ('pending', 'running')"),
                lambda r: f"job {r['id']} still open though task {r['task']} is {r['status']}")
            flag("events_contiguous", q(
                "SELECT task_id, COUNT(*) AS n, MAX(seq) AS last FROM task_events GROUP BY task_id HAVING n != last"),
                lambda r: f"task {r['task_id']} has {r['n']} events numbered up to {r['last']}")
            for r in q("SELECT t.id, j.spent, t.status FROM tasks t JOIN jobs j ON j.id = t.job_id "
                       "WHERE t.status IN ('passed', 'failed', 'error', 'cancelled')"):
                logged = sum(round(json.loads(e[0])["cost"] * 1_000_000) for e in q(
                    "SELECT data FROM task_events WHERE task_id = ? AND type = 'call' "
                    "AND json_extract(data, '$.depth') = 0", (r["id"],)))
                if logged != r["spent"]:
                    out.append({"invariant": "events_match_spend",
                                "detail": f"task {r['id']} ({r['status']}): call events total {logged}, job spent {r['spent']}"})

        counts = {k: q(f"SELECT COUNT(*) FROM {k}")[0][0] for k in ("accounts", "transactions", "jobs", "calls", "tasks")}

    return {"ok": not out, "quiescent": quiescent, "violations": out, "counts": counts}
