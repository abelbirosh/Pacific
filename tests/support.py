"""Boots a real economy (exchange, services, agent host) on free ports for tests."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from pacific.config import Config

TASKS = Path(__file__).resolve().parent.parent / "tasks"


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


def run_task(admin: httpx.Client, spec: dict, wait: float = 300) -> dict:
    """Create and run a task, waiting for its report."""
    task = admin.post("/v1/tasks", json=spec).raise_for_status().json()
    r = admin.post(f"/v1/tasks/{task['id']}/run", params={"wait": wait}).raise_for_status()
    assert r.status_code == 200, f"task {task['id']} didn't finish within {wait}s"
    return r.json()


def agent_with(secrets: dict, skill: str | None = None, without: str | None = None) -> tuple[str, dict]:
    for aid, a in secrets["agents"].items():
        if (skill is None or skill in a["skills"]) and (without is None or without not in a["skills"]):
            return aid, a
    pytest.skip(f"no agent with={skill} without={without} for this seed")


def load(name: str) -> dict:
    return json.loads((TASKS / name).read_text())


def assert_books_balance(admin: httpx.Client):
    """Every ledger invariant holds, and nothing is left on hold or pending once work has stopped."""
    report = admin.get("/v1/admin/invariants", params={"quiescent": True}).json()
    assert report["ok"], report["violations"]
