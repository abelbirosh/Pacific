"""Boots a real economy (exchange, services, agent host) on free ports for tests."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from pacific.config import Config

TASKS = Path(__file__).resolve().parent.parent / "tasks"

# Contracts retry checks that can't run; in tests the waits are short.
os.environ.setdefault("PACIFIC_VERIFY_BACKOFF", "0.1,0.2,0.4")


@contextmanager
def boot(home: Path, seed: int = 1, brain: str = "scripted"):
    """Yields (config, secrets, admin client) for a fresh economy served on a background thread."""
    from pacific.seed import genesis
    from pacific.server import free_port, running

    config = Config(home=home, exchange_port=free_port(), services_port=free_port(), agents_port=free_port())
    genesis(config, seed=seed, brain=brain)
    secrets = config.secrets()
    with running(config), httpx.Client(base_url=config.exchange_url, timeout=300,
                                       headers={"Authorization": f"Bearer {secrets['admin_key']}"}) as admin:
        yield config, secrets, admin


def run_task(admin: httpx.Client, spec: dict) -> dict:
    task = admin.post("/v1/tasks", json=spec).raise_for_status().json()
    return admin.post(f"/v1/tasks/{task['id']}/run").raise_for_status().json()


def agent_with(secrets: dict, skill: str | None = None, without: str | None = None) -> tuple[str, dict]:
    for aid, a in secrets["agents"].items():
        if (skill is None or skill in a["skills"]) and (without is None or without not in a["skills"]):
            return aid, a
    pytest.skip(f"no agent with={skill} without={without} for this seed")


def load(name: str) -> dict:
    return json.loads((TASKS / name).read_text())


def settle_contracts(admin: httpx.Client, timeout: float = 30) -> None:
    """Wait for every contract still working or verifying to settle."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        open_ = [c for status in ("working", "verifying")
                 for c in admin.get("/v1/contracts", params={"status": status}).json()["contracts"]]
        if not open_:
            return
        time.sleep(0.05)
    raise AssertionError(f"contracts still open after {timeout}s: {[c['id'] for c in open_]}")


def contract(admin: httpx.Client, contract_id: str) -> dict:
    settle_contracts(admin)
    return admin.get(f"/v1/contracts/{contract_id}").raise_for_status().json()


def assert_books_balance(admin: httpx.Client):
    """Every ledger invariant holds, and nothing is left on hold or pending once work has stopped."""
    settle_contracts(admin)
    report = admin.get("/v1/admin/invariants", params={"quiescent": True}).json()
    assert report["ok"], report["violations"]
