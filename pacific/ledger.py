"""The wave-dollar ledger.

Invariant: the sum of every account balance equals the global supply (`meta.total_supply`).
Money only enters through `mint` (which raises the supply) and otherwise only moves between
accounts. Holds reserve funds without moving them, so a caller can never commit more than it has.

Escrow is a real account (kind `escrow`). A hire under a promise moves its premium out of the
buyer's hold into escrow, where it waits for the contract's verdict and then moves exactly once
more: released to the seller, refunded to the buyer, or split between them. A refund also gives
the buyer's job back that much of its budget.
"""

from __future__ import annotations

import json
import sqlite3

from .db import Database
from .ids import hash_key, new_api_key, new_id
from .money import fmt


class LedgerError(Exception):
    status_code = 400


class InsufficientFunds(LedgerError):
    status_code = 402


class BudgetExceeded(LedgerError):
    status_code = 402


class Ledger:
    def __init__(self, db: Database):
        self.db = db

    # ---- supply -----------------------------------------------------------------------------

    def total_supply(self) -> int:
        row = self.db.one("SELECT value FROM meta WHERE key='total_supply'")
        return int(row["value"]) if row else 0

    def mint(self, conn: sqlite3.Connection, to_id: str, amount: int, memo: str) -> None:
        if amount <= 0:
            raise LedgerError("mint amount must be positive")
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('total_supply', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + ?",
            (str(amount), amount),
        )
        conn.execute("UPDATE accounts SET balance = balance + ? WHERE id = ?", (amount, to_id))
        conn.execute(
            "INSERT INTO transactions(kind, from_id, to_id, amount, memo) VALUES ('mint', NULL, ?, ?, ?)",
            (to_id, amount, memo),
        )

    def audit(self) -> dict:
        supply = self.total_supply()
        circulating = self.db.one("SELECT COALESCE(SUM(balance), 0) AS s FROM accounts")["s"]
        minted = self.db.one("SELECT COALESCE(SUM(amount), 0) AS s FROM transactions WHERE kind='mint'")["s"]
        held = self.db.one("SELECT COALESCE(SUM(amount), 0) AS s FROM holds WHERE status='active'")["s"]
        return {
            "total_supply": supply,
            "sum_of_balances": circulating,
            "sum_of_mints": minted,
            "active_holds": held,
            "ok": supply == circulating == minted,
        }

    # ---- accounts ---------------------------------------------------------------------------

    def create_account(
        self,
        conn: sqlite3.Connection,
        kind: str,
        name: str,
        owner_id: str | None = None,
        profile: dict | None = None,
        with_key: bool = True,
    ) -> tuple[str, str | None]:
        account_id = new_id(kind[:4])
        key = new_api_key() if with_key else None
        conn.execute(
            "INSERT INTO accounts(id, kind, name, owner_id, key_hash, profile) VALUES (?,?,?,?,?,?)",
            (account_id, kind, name, owner_id, hash_key(key) if key else None, json.dumps(profile or {})),
        )
        return account_id, key

    def account(self, account_id: str) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM accounts WHERE id = ?", (account_id,))

    def account_by_key(self, key: str) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM accounts WHERE key_hash = ?", (hash_key(key),))

    def resolve(self, ref: str) -> sqlite3.Row | None:
        """Look an account up by id or (case-insensitive) name."""
        return self.db.one(
            "SELECT * FROM accounts WHERE id = ? OR lower(name) = lower(?) ORDER BY id = ? DESC LIMIT 1",
            (ref, ref, ref),
        )

    @staticmethod
    def held(conn: sqlite3.Connection, account_id: str) -> int:
        return conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM holds WHERE account_id = ? AND status = 'active'",
            (account_id,),
        ).fetchone()[0]

    def available(self, conn: sqlite3.Connection, account_id: str) -> int:
        balance = conn.execute("SELECT balance FROM accounts WHERE id = ?", (account_id,)).fetchone()[0]
        return balance - self.held(conn, account_id)

    # ---- movement ---------------------------------------------------------------------------

    def transfer(
        self,
        conn: sqlite3.Connection,
        from_id: str,
        to_id: str,
        amount: int,
        memo: str = "",
        kind: str = "transfer",
        call_id: str | None = None,
        job_id: str | None = None,
        ignore_holds: bool = False,
    ) -> None:
        if amount <= 0:
            raise LedgerError("transfer amount must be positive")
        if from_id == to_id:
            raise LedgerError("cannot transfer to self")
        if not ignore_holds and self.available(conn, from_id) < amount:
            raise InsufficientFunds(f"{from_id} has {fmt(self.available(conn, from_id))} available, needs {fmt(amount)}")
        conn.execute("UPDATE accounts SET balance = balance - ? WHERE id = ?", (amount, from_id))
        conn.execute("UPDATE accounts SET balance = balance + ? WHERE id = ?", (amount, to_id))
        conn.execute(
            "INSERT INTO transactions(kind, from_id, to_id, amount, memo, call_id, job_id) VALUES (?,?,?,?,?,?,?)",
            (kind, from_id, to_id, amount, memo, call_id, job_id),
        )

    # ---- holds & jobs -----------------------------------------------------------------------

    def place_hold(self, conn: sqlite3.Connection, account_id: str, amount: int, job_id: str | None) -> str:
        available = self.available(conn, account_id)
        if available < amount:
            raise InsufficientFunds(f"needs {fmt(amount)} but only {fmt(available)} is available")
        if job_id:
            remaining = self.job_remaining(conn, job_id)
            if remaining < amount:
                raise BudgetExceeded(f"needs up to {fmt(amount)} but job {job_id} has {fmt(remaining)} of budget left")
        hold_id = new_id("hold")
        conn.execute(
            "INSERT INTO holds(id, account_id, job_id, amount) VALUES (?,?,?,?)",
            (hold_id, account_id, job_id, amount),
        )
        return hold_id

    def settle_hold(
        self,
        conn: sqlite3.Connection,
        hold_id: str,
        to_id: str | None,
        amount: int,
        memo: str,
        call_id: str | None,
    ) -> int:
        """Release a hold and charge up to its amount to `to_id`. Returns what was charged."""
        return self.settle_hold_split(conn, hold_id, [(to_id, amount, "charge")], memo, call_id)

    def settle_hold_split(
        self,
        conn: sqlite3.Connection,
        hold_id: str,
        parts: list[tuple[str | None, int, str]],
        memo: str,
        call_id: str | None,
    ) -> int:
        """Release a hold and pay it out in parts, (to_id, amount, kind) in order, each capped by what
        is left of the hold. Kind is `charge` or `escrow`; both count as the job's spend. Returns the total."""
        hold = conn.execute("SELECT * FROM holds WHERE id = ? AND status = 'active'", (hold_id,)).fetchone()
        if hold is None:
            raise LedgerError(f"hold {hold_id} is not active")
        conn.execute("UPDATE holds SET status = 'settled' WHERE id = ?", (hold_id,))
        left, total = hold["amount"], 0
        for to_id, amount, kind in parts:
            paid = max(0, min(amount, left))
            if paid and to_id:
                # The hold already guaranteed these funds, so skip the available-balance check.
                self.transfer(conn, hold["account_id"], to_id, paid, memo, kind, call_id, hold["job_id"],
                              ignore_holds=True)
                left, total = left - paid, total + paid
        if total and hold["job_id"]:
            conn.execute("UPDATE jobs SET spent = spent + ? WHERE id = ?", (total, hold["job_id"]))
        return total

    # ---- escrow -----------------------------------------------------------------------------

    def escrow_account(self, conn: sqlite3.Connection) -> str:
        row = conn.execute("SELECT id FROM accounts WHERE kind = 'escrow'").fetchone()
        if row:
            return row[0]
        platform = conn.execute("SELECT id FROM accounts WHERE kind = 'platform'").fetchone()
        account_id, _ = self.create_account(conn, "escrow", "Pacific Escrow", owner_id=platform[0] if platform else None,
                                            with_key=False)
        return account_id

    def escrow_payout(self, conn: sqlite3.Connection, escrow_id: str, seller_id: str, buyer_id: str,
                      buyer_job_id: str | None, release: int, refund: int, memo: str, call_id: str) -> None:
        """Move a contract's premium out of escrow: `release` to the seller, `refund` to the buyer.

        The refund also gives the buyer's job back that much spend, unless that job is itself a hire
        that has already been billed to its own buyer: its spend is what that bill was worked out
        from, so a late refund just goes to the buyer's wallet."""
        if release:
            self.transfer(conn, escrow_id, seller_id, release, memo, "escrow_release", call_id, buyer_job_id)
        if refund:
            billed = buyer_job_id and conn.execute(
                "SELECT 1 FROM calls WHERE child_job_id = ? AND status != 'pending'", (buyer_job_id,)).fetchone()
            job = None if billed else buyer_job_id
            self.transfer(conn, escrow_id, buyer_id, refund, memo, "escrow_refund", call_id, job)
            if job:
                conn.execute("UPDATE jobs SET spent = spent - ? WHERE id = ?", (refund, job))

    def open_job(
        self,
        conn: sqlite3.Connection,
        account_id: str,
        budget: int,
        parent_job_id: str | None = None,
        task_id: str | None = None,
    ) -> str:
        job_id = new_id("job")
        root = job_id
        if parent_job_id:
            root = conn.execute("SELECT root_job_id FROM jobs WHERE id = ?", (parent_job_id,)).fetchone()[0]
        conn.execute(
            "INSERT INTO jobs(id, account_id, budget, parent_job_id, root_job_id, task_id) VALUES (?,?,?,?,?,?)",
            (job_id, account_id, budget, parent_job_id, root, task_id),
        )
        return job_id

    @staticmethod
    def job_remaining(conn: sqlite3.Connection, job_id: str) -> int:
        job = conn.execute("SELECT budget, spent, status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            raise LedgerError(f"unknown job {job_id}")
        if job["status"] != "open":
            raise BudgetExceeded(f"job {job_id} is closed")
        held = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM holds WHERE job_id = ? AND status = 'active'", (job_id,)
        ).fetchone()[0]
        return job["budget"] - job["spent"] - held

    def job_status(self, job_id: str) -> dict:
        job = self.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if job is None:
            raise LedgerError(f"unknown job {job_id}")
        held = self.db.one(
            "SELECT COALESCE(SUM(amount), 0) AS h FROM holds WHERE job_id = ? AND status = 'active'", (job_id,)
        )["h"]
        return {
            "id": job["id"],
            "account_id": job["account_id"],
            "budget": job["budget"],
            "spent": job["spent"],
            "held": held,
            "remaining": job["budget"] - job["spent"] - held,
            "status": job["status"],
            "parent_job_id": job["parent_job_id"],
            "root_job_id": job["root_job_id"],
        }
