"""Properties the economy must hold at every instant, checked on one consistent snapshot.

`audit` only checks the supply. These go further, so they can be polled while traffic runs:

supply               total supply = sum of balances = sum of mints
negative_balance     no account below zero
overcommitted        no account has more on hold than it holds
job_over_budget      no job's spend plus active holds exceeds its budget
job_books            a job's `spent` equals the charges and escrow deposits booked against it, less
                     the escrow refunds it got back
call_overcharged     no call costs more than the hold placed for it
call_books           a settled call's cost equals the charges and escrow deposits booked for it
delegation_bill      a hire costs fee + seller's spend at cost-plus (or just the spend if it
                     failed), capped by the hold, at the terms of the promise version it was made
                     under if any. Checked once the child job is closed, so a charge that lands
                     on the child job after the buyer was billed shows up here
                     Under a contract the premium part goes to escrow instead of the seller
promise_current      each endpoint tier has at most one current promise version
hire_within_cap      a hire under a promise never gives the seller more budget than its cost cap
escrow_books         the escrow account holds exactly the premiums of contracts not yet paid out
contract_payout      a settled contract paid out exactly its premium (released + refunded); an open
                     one has paid out nothing
release_on_verdict   only a passed contract releases its whole premium, only a partial one part of
                     it, and failed, unverified or declined contracts release nothing
workspace            every storage / database directory belongs to a root job or an account

The exchange adds `unhandled_error` for any exception it didn't handle (it should refuse bad
input with a 4xx, never fall over on it).

With `quiescent=True` (no traffic in flight) also:

holds_released       no active holds
calls_settled        no call still pending
tasks_finished       no task still running
contracts_settled    no contract still working or verifying, so escrow is empty
"""

from __future__ import annotations

import json
from pathlib import Path

from . import promises
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
            "SELECT j.id, j.spent, COALESCE(SUM(CASE t.kind WHEN 'escrow_refund' THEN -t.amount ELSE t.amount END), 0) "
            "AS charged FROM jobs j LEFT JOIN transactions t ON t.job_id = j.id "
            "AND t.kind IN ('charge', 'escrow', 'escrow_refund') GROUP BY j.id HAVING charged != j.spent"),
            lambda r: f"{r['id']} spent {r['spent']} but charges total {r['charged']}")
        flag("call_overcharged", q("SELECT id, cost, held FROM calls WHERE cost > held"),
             lambda r: f"{r['id']} cost {r['cost']} > hold {r['held']}")
        flag("call_books", q(
            "SELECT c.id, c.cost, COALESCE(SUM(t.amount), 0) AS charged FROM calls c "
            "LEFT JOIN transactions t ON t.call_id = c.id AND t.kind IN ('charge', 'escrow') "
            "WHERE c.status != 'pending' GROUP BY c.id HAVING charged != c.cost"),
            lambda r: f"{r['id']} cost {r['cost']} but charges total {r['charged']}")

        for c in q(
            "SELECT c.id, c.status, c.cost, c.held, e.plan, j.spent AS child_spent, p.fee, p.markup_pct, "
            "ct.id AS contract_id, ct.claim "
            "FROM calls c JOIN jobs j ON j.id = c.child_job_id "
            "JOIN endpoints e ON e.service = c.service AND e.name = c.endpoint "
            "LEFT JOIN promises p ON p.id = c.promise_id LEFT JOIN contracts ct ON ct.call_id = c.id "
            "WHERE c.status != 'pending' AND j.status = 'closed'"
        ):
            plan = promises.plan(c) if c["fee"] is not None else PricePlan.from_dict(json.loads(c["plan"]))
            billed_for = c["child_spent"]
            if c["contract_id"]:  # resources at cost now, and the premium into escrow if the work came back
                earns = c["claim"] in ("completed", "timeout")
                expected = plan.cost({}, passthrough=billed_for) if earns else billed_for
            else:
                expected = plan.cost({}, passthrough=billed_for) if c["status"] == "ok" else billed_for
            expected = min(expected, c["held"])
            if c["cost"] != expected:
                out.append({"invariant": "delegation_bill", "detail": f"{c['id']} ({c['status']}) cost {c['cost']}, "
                            f"expected {expected} from the seller's spend of {billed_for}"})

        flag("promise_current", q(
            "SELECT service, endpoint, tier, COUNT(*) AS n FROM promises WHERE status = 'active' "
            "GROUP BY service, endpoint, tier HAVING n > 1"),
            lambda r: f"{r['service']}/{r['endpoint']} tier {r['tier']} has {r['n']} current versions")
        flag("hire_within_cap", q(
            "SELECT c.id, j.budget, p.cost_cap FROM calls c JOIN jobs j ON j.id = c.child_job_id "
            "JOIN promises p ON p.id = c.promise_id WHERE j.budget > p.cost_cap"),
            lambda r: f"{r['id']} gave the seller {r['budget']}, over the promise's cost cap of {r['cost_cap']}")

        escrow = q("SELECT COALESCE(SUM(balance), 0) FROM accounts WHERE kind = 'escrow'")[0][0]
        owed = q("SELECT COALESCE(SUM(premium - released - refunded), 0) FROM contracts")[0][0]
        if escrow != owed:
            out.append({"invariant": "escrow_books", "detail": f"escrow holds {escrow} but open premiums total {owed}"})
        flag("contract_payout", q(
            "SELECT id, status, premium, released, refunded FROM contracts WHERE "
            "(status IN ('passed', 'partial', 'failed', 'unverified', 'declined') AND released + refunded != premium) "
            "OR (status IN ('working', 'verifying') AND released + refunded != 0)"),
            lambda r: f"{r['id']} ({r['status']}) premium {r['premium']}, released {r['released']}, refunded {r['refunded']}")
        flag("release_on_verdict", q(
            "SELECT id, status, premium, released FROM contracts WHERE "
            "(status = 'passed' AND released != premium) OR (status = 'partial' AND NOT (released > 0 AND released < premium)) "
            "OR (status IN ('failed', 'unverified', 'declined') AND released != 0)"),
            lambda r: f"{r['id']} ({r['status']}) released {r['released']} of {r['premium']}")

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
            flag("contracts_settled", q("SELECT id, status FROM contracts WHERE status IN ('working', 'verifying')"),
                 lambda r: f"{r['id']} still {r['status']}")

        counts = {k: q(f"SELECT COUNT(*) FROM {k}")[0][0]
                  for k in ("accounts", "transactions", "jobs", "calls", "tasks", "promises", "contracts")}

    return {"ok": not out, "quiescent": quiescent, "violations": out, "counts": counts}
