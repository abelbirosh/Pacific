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

-- kind: treasury | platform | service | person | agent
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
    kind       TEXT NOT NULL,              -- mint | transfer | charge
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
    error          TEXT,
    latency_ms     INTEGER,
    started_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
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
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(tasks)")}
        if "artifacts" not in cols:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN artifacts TEXT")

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
