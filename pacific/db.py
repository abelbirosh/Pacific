"""SQLite access for the exchange's ledger, registry, jobs, calls and tasks."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- kind: treasury | platform | service | escrow | person | agent
CREATE TABLE IF NOT EXISTS accounts (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    name       TEXT NOT NULL,
    owner_id   TEXT REFERENCES accounts(id),
    key_hash   TEXT UNIQUE,
    balance    INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    profile    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Append-only. Every movement of wave dollars is one row.
CREATE TABLE IF NOT EXISTS transactions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    kind       TEXT NOT NULL,              -- mint | transfer | charge | escrow | escrow_release | escrow_refund
    from_id    TEXT REFERENCES accounts(id),
    to_id      TEXT NOT NULL REFERENCES accounts(id),
    amount     INTEGER NOT NULL CHECK (amount > 0),
    memo       TEXT NOT NULL DEFAULT '',
    call_id    TEXT,
    job_id     TEXT
);

-- Reserved funds: counted against available balance (and job budget) until settled.
CREATE TABLE IF NOT EXISTS holds (
    id         TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    job_id     TEXT,
    amount     INTEGER NOT NULL CHECK (amount >= 0),
    status     TEXT NOT NULL DEFAULT 'active', -- active | settled
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- kind: platform (served by the services host) | agent (task delegation to an agent)
CREATE TABLE IF NOT EXISTS services (
    name        TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    provider_id TEXT NOT NULL REFERENCES accounts(id),
    description TEXT NOT NULL,
    base_url    TEXT NOT NULL,
    meta        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS endpoints (
    service     TEXT NOT NULL REFERENCES services(name),
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    path        TEXT NOT NULL,
    plan        TEXT NOT NULL,             -- PricePlan JSON
    params      TEXT NOT NULL DEFAULT '{}',-- JSON schema of the request body
    PRIMARY KEY (service, name)
);

-- What an endpoint promises, per tier. Terms are never edited: a change adds the next version and
-- supersedes the last, so a hire keeps exactly the terms it was quoted. Money is micro-WD.
CREATE TABLE IF NOT EXISTS promises (
    id           TEXT PRIMARY KEY,
    service      TEXT NOT NULL,
    endpoint     TEXT NOT NULL,
    tier         TEXT NOT NULL,
    version      INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active', -- active | superseded | retired
    fee          INTEGER NOT NULL CHECK (fee >= 0),
    markup_pct   REAL NOT NULL,
    cost_cap     INTEGER NOT NULL CHECK (cost_cap > 0),
    turnaround_s INTEGER NOT NULL,
    deliverables TEXT NOT NULL,             -- JSON [{name, description}]
    checks       TEXT NOT NULL,             -- JSON check templates
    payment      TEXT NOT NULL DEFAULT 'completion', -- completion | progress
    milestones   TEXT NOT NULL DEFAULT '[]',         -- JSON [{name, share_pct, description, checks}] (progress)
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (service, endpoint, tier, version),
    FOREIGN KEY (service, endpoint) REFERENCES endpoints(service, name)
);

-- A job is a spending scope with a budget. Delegation creates child jobs.
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL REFERENCES accounts(id),
    budget        INTEGER NOT NULL,
    spent         INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'open', -- open | closed
    parent_job_id TEXT REFERENCES jobs(id),
    root_job_id   TEXT NOT NULL,
    task_id       TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS calls (
    id             TEXT PRIMARY KEY,
    caller_id      TEXT NOT NULL REFERENCES accounts(id),
    service        TEXT NOT NULL,
    endpoint       TEXT NOT NULL,
    job_id         TEXT,
    child_job_id   TEXT,
    held           INTEGER NOT NULL DEFAULT 0,
    cost           INTEGER NOT NULL DEFAULT 0,
    units          REAL NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'pending', -- pending | ok | failed | rejected
    promise_id     TEXT REFERENCES promises(id), -- the promise version a hire was made under
    acceptance     TEXT,                    -- JSON: the buyer's acceptance checks for that hire
    error          TEXT,
    latency_ms     INTEGER,
    started_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- A hire under a promise between accounts with different owners. The premium waits in escrow
-- until the acceptance checks are verified, then is released, refunded, or split by milestone.
CREATE TABLE IF NOT EXISTS contracts (
    id           TEXT PRIMARY KEY,
    call_id      TEXT NOT NULL UNIQUE REFERENCES calls(id),
    promise_id   TEXT NOT NULL REFERENCES promises(id),
    buyer_id     TEXT NOT NULL REFERENCES accounts(id),
    seller_id    TEXT NOT NULL REFERENCES accounts(id),
    buyer_job_id TEXT REFERENCES jobs(id),
    child_job_id TEXT REFERENCES jobs(id),
    checks       TEXT NOT NULL,             -- JSON acceptance checks
    status       TEXT NOT NULL DEFAULT 'working',
                 -- working | verifying | passed | partial | failed | unverified | declined
    claim        TEXT,                      -- what the seller said: completed | failed | declined | timeout | error
    premium      INTEGER NOT NULL DEFAULT 0, -- moved into escrow
    released     INTEGER NOT NULL DEFAULT 0, -- paid out of escrow to the seller
    refunded     INTEGER NOT NULL DEFAULT 0, -- paid out of escrow back to the buyer
    attempts     INTEGER NOT NULL DEFAULT 0, -- verification runs
    verdict      TEXT,                      -- JSON: check results, milestones paid
    spent        INTEGER NOT NULL DEFAULT 0, -- the seller's spend on platform services
    budget       INTEGER NOT NULL DEFAULT 0, -- what the buyer let the seller spend
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    deadline_at  REAL NOT NULL,             -- unix time: the promise's turnaround after the hire
    returned_at  REAL,                      -- unix time the seller's work came back (or the deadline hit)
    settled_at   TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL REFERENCES accounts(id),
    title       TEXT NOT NULL,
    spec        TEXT NOT NULL,             -- full task JSON (description, inputs, checks...)
    budget      INTEGER NOT NULL,
    job_id      TEXT NOT NULL REFERENCES jobs(id),
    status      TEXT NOT NULL DEFAULT 'pending', -- pending | running | passed | failed | error
    output      TEXT,
    trace       TEXT,
    checks      TEXT,
    artifacts   TEXT,                      -- what the brain chose to report (JSON)
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tx_call ON transactions(call_id);
CREATE INDEX IF NOT EXISTS idx_tx_job ON transactions(job_id);
CREATE INDEX IF NOT EXISTS idx_calls_job ON calls(job_id);
CREATE INDEX IF NOT EXISTS idx_holds_active ON holds(account_id, status);
CREATE INDEX IF NOT EXISTS idx_promises_current ON promises(service, endpoint, tier, status);
CREATE INDEX IF NOT EXISTS idx_contracts_status ON contracts(status);
CREATE INDEX IF NOT EXISTS idx_contracts_promise ON contracts(promise_id);
"""


class Database:
    """One writer at a time; each `tx()` block is a single IMMEDIATE transaction."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        """Add columns introduced after a ledger was created."""
        for table, column, decl in (("tasks", "artifacts", "TEXT"),
                                    ("calls", "promise_id", "TEXT REFERENCES promises(id)"),
                                    ("calls", "acceptance", "TEXT"),
                                    ("promises", "payment", "TEXT NOT NULL DEFAULT 'completion'"),
                                    ("promises", "milestones", "TEXT NOT NULL DEFAULT '[]'")):
            cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    @contextmanager
    def tx(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    @contextmanager
    def snapshot(self):
        """A read-only view that no write can interleave with."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            finally:
                self._conn.execute("COMMIT")

    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def all(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self):
        self._conn.close()
