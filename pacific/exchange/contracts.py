"""Contracts: a hire under a promise, with the premium in escrow until the work is verified.

A hire becomes a contract when it names a promise tier and the buyer and seller have different
owners (an owner hiring their own agent has nothing to guarantee). Its life:

    open        before any money is held, every acceptance check is run once and must fail;
                then the call's hold reserves the fee plus the markup on the full budget
    working     the seller works, up to the promise's turnaround; at the deadline the exchange
                stops waiting and verifies whatever exists
    returned    the buyer pays the seller's actual spend at cost, as for any hire. If the seller
                says it is done (or the deadline hit), the premium (fee + markup on the actual
                spend) moves into escrow and the contract is `verifying`. If it says it failed,
                or declines, no premium moves; a decline before any spend costs nobody anything
    verifying   the acceptance checks run against the work. A check that can't reach its state
                is retried after each wait in PACIFIC_VERIFY_BACKOFF (default 10, 60, 300 s)
    settled     passed (premium released to the seller), partial (progress payment: the share of
                the milestones that passed), failed (premium refunded), or unverified (checks
                still couldn't run after every retry: refunded automatically, and recorded apart
                from failures, since an outage isn't the seller's doing)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from .. import promises
from ..db import Database
from ..ids import new_id
from ..ledger import Ledger
from ..registry import Registry
from .checks import CheckContext, run_checks

log = logging.getLogger("pacific.contracts")

DEFAULT_BACKOFF = (10.0, 60.0, 300.0)
PRECHECK_BACKOFF = (0.2, 1.0)  # the buyer is waiting, so the must-fail run only retries briefly
SETTLED = ("passed", "partial", "failed", "unverified", "declined")


class ContractError(Exception):
    def __init__(self, status: int, detail):
        super().__init__(detail)
        self.status_code, self.detail = status, detail


def backoff_from_env() -> tuple[float, ...]:
    raw = os.environ.get("PACIFIC_VERIFY_BACKOFF")
    if not raw:
        return DEFAULT_BACKOFF
    return tuple(max(0.0, float(x)) for x in raw.split(",") if x.strip())


def owner_of(account) -> str:
    return account["owner_id"] or account["id"]


class Contracts:
    def __init__(self, db: Database, ledger: Ledger, registry: Registry, services_url: str, gateway_secret: str,
                 backoff: tuple[float, ...] | None = None):
        self.db, self.ledger, self.registry = db, ledger, registry
        self.services_url, self.gateway_secret = services_url, gateway_secret
        self.backoff = backoff_from_env() if backoff is None else backoff
        self._inflight: dict[str, asyncio.Task] = {}

    def applies(self, buyer, seller) -> bool:
        return owner_of(buyer) != owner_of(seller)

    # ---- opening ----------------------------------------------------------------------------

    async def precheck(self, workspace: str, checks: list[dict]) -> None:
        """Every acceptance check must fail before the work starts (limits are exempt). Checks that
        can't run are retried briefly; if they still can't, the hire is refused with a 503."""
        to_run = [(i, c) for i, c in enumerate(checks) if c["type"] not in promises.LIMITS]
        if not to_run:
            return
        ctx = CheckContext(self.services_url, self.gateway_secret, workspace, "", {}, set())
        for wait in (0.0,) + PRECHECK_BACKOFF:
            if wait:
                await asyncio.sleep(wait)
            results = await run_checks(ctx, [c for _, c in to_run])
            if not any(r.get("error") for r in results):
                break
        if errored := [(i, r) for (i, _), r in zip(to_run, results) if r.get("error")]:
            raise ContractError(503, f"couldn't confirm the acceptance checks start unmet (check {errored[0][0]}: "
                                     f"{errored[0][1]['detail']}); try again")
        if passing := [(i, r) for (i, _), r in zip(to_run, results) if r["passed"]]:
            raise ContractError(409, {"message": "acceptance checks already pass before any work, so they can't "
                                                 "show the seller delivered",
                                      "errors": [f"check {i}: {r['detail']}" for i, r in passing]})

    def open(self, conn, call_id: str, promise: dict, buyer_id: str, seller_id: str, buyer_job_id: str | None,
             child_job_id: str, checks: list[dict], budget: int) -> dict:
        contract_id = new_id("ctr")
        deadline = time.time() + promise["turnaround_s"]
        conn.execute(
            "INSERT INTO contracts(id, call_id, promise_id, buyer_id, seller_id, buyer_job_id, child_job_id, checks, "
            "budget, deadline_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (contract_id, call_id, promise["id"], buyer_id, seller_id, buyer_job_id, child_job_id, json.dumps(checks),
             budget, deadline),
        )
        return {"id": contract_id, "deadline_at": deadline}

    # ---- the seller's work comes back ---------------------------------------------------------

    def returned(self, conn, contract_id: str, claim: str, spent: int, premium: int) -> str:
        """Record what came back. `premium` is what was just moved into escrow (0 if none)."""
        if claim in ("completed", "timeout"):
            status = "verifying"
        elif claim == "declined" and spent == 0:
            status = "declined"
        else:
            status = "failed"
        conn.execute(
            "UPDATE contracts SET status = ?, claim = ?, spent = ?, premium = ?, returned_at = ?, "
            "settled_at = CASE WHEN ? = 'verifying' THEN NULL ELSE strftime('%Y-%m-%dT%H:%M:%fZ','now') END "
            "WHERE id = ? AND status = 'working'",
            (status, claim, spent, premium, time.time(), status, contract_id),
        )
        return status

    # ---- verification -----------------------------------------------------------------------

    def start_verifying(self, contract_id: str) -> None:
        if contract_id not in self._inflight:
            task = asyncio.get_running_loop().create_task(self.verify(contract_id))
            self._inflight[contract_id] = task
            task.add_done_callback(lambda _: self._inflight.pop(contract_id, None))

    async def sweep_forever(self, every_s: float = 2.0) -> None:
        """Pick up contracts left verifying (for instance by a restart) and verify them."""
        while True:
            try:
                for row in self.db.all("SELECT id FROM contracts WHERE status = 'verifying'"):
                    self.start_verifying(row["id"])
            except Exception:  # never let the sweeper die
                log.exception("contract sweep failed")
            await asyncio.sleep(every_s)

    async def verify(self, contract_id: str) -> None:
        c = self.db.one("SELECT * FROM contracts WHERE id = ?", (contract_id,))
        if c is None or c["status"] != "verifying":
            return
        promise = self.registry.promise_by_id(c["promise_id"])
        checks = json.loads(c["checks"])
        waits = (0.0,) + tuple(self.backoff)
        results: list[dict] = []
        for wait in waits:
            if wait:
                await asyncio.sleep(wait)
            results = await run_checks(self._context(c), checks)
            with self.db.tx() as conn:
                conn.execute("UPDATE contracts SET attempts = attempts + 1 WHERE id = ?", (contract_id,))
            if not any(r.get("error") for r in results):
                break
        self.settle(contract_id, promise, checks, results)

    def _context(self, c) -> CheckContext:
        job = self.ledger.job_status(c["child_job_id"])
        used = {r["service"] for r in self.db.all(
            "WITH RECURSIVE sub(id) AS (SELECT ? UNION ALL SELECT j.id FROM jobs j JOIN sub ON j.parent_job_id = sub.id) "
            "SELECT DISTINCT service FROM calls WHERE job_id IN (SELECT id FROM sub) AND status = 'ok'",
            (c["child_job_id"],))}
        return CheckContext(self.services_url, self.gateway_secret, job["root_job_id"], "", job, used)

    def settle(self, contract_id: str, promise: dict, checks: list[dict], results: list[dict]) -> None:
        if any(r.get("error") for r in results):
            share, why, status = 0.0, {"unverified": "checks still couldn't run after every retry"}, "unverified"
        else:
            share, why = promises.payout_share(promise, checks, results)
            status = "passed" if share >= 1 else "partial" if share > 0 else "failed"
        with self.db.tx() as conn:
            c = conn.execute("SELECT * FROM contracts WHERE id = ? AND status = 'verifying'", (contract_id,)).fetchone()
            if c is None:
                return  # someone else settled it
            release = int(c["premium"] * share)
            refund = c["premium"] - release
            self.ledger.escrow_payout(conn, self.ledger.escrow_account(conn), c["seller_id"], c["buyer_id"],
                                      c["buyer_job_id"], release, refund, f"contract {contract_id} {status}",
                                      c["call_id"])
            conn.execute(
                "UPDATE contracts SET status = ?, released = ?, refunded = ?, verdict = ?, "
                "settled_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (status, release, refund, json.dumps({"results": results, "payout": why, "share": share}), contract_id),
            )

    # ---- reading ----------------------------------------------------------------------------

    def describe(self, row) -> dict:
        promise = self.registry.promise_by_id(row["promise_id"])
        return {
            "id": row["id"], "call_id": row["call_id"], "status": row["status"], "claim": row["claim"],
            "service": promise["service"], "tier": promise["tier"], "version": promise["version"],
            "payment": promise["payment"], "buyer_id": row["buyer_id"], "seller_id": row["seller_id"],
            "buyer_job_id": row["buyer_job_id"], "child_job_id": row["child_job_id"],
            "checks": json.loads(row["checks"]), "budget": row["budget"], "spent": row["spent"],
            "premium": row["premium"], "released": row["released"], "refunded": row["refunded"],
            "attempts": row["attempts"], "verdict": json.loads(row["verdict"]) if row["verdict"] else None,
            "created_at": row["created_at"], "deadline_at": row["deadline_at"], "returned_at": row["returned_at"],
            "settled_at": row["settled_at"],
        }
