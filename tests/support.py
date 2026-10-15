"""Boots a real economy (exchange, services, agent host) on free ports for tests."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn

from pacific.config import Config

TASKS = Path(__file__).resolve().parent.parent / "tasks"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def boot(home: Path, seed: int = 1, brain: str = "scripted"):
    """Yields (config, secrets, admin client) for a fresh economy served on background threads."""
    from pacific.agents.host import create_app as agents_app
    from pacific.exchange.app import create_app as exchange_app
    from pacific.seed import genesis
    from pacific.services.app import create_app as services_app

    config = Config(home=home, exchange_port=free_port(), services_port=free_port(), agents_port=free_port())
    genesis(config, seed=seed, brain=brain)
    secrets = config.secrets()
    servers = [
        uvicorn.Server(uvicorn.Config(exchange_app(config), port=config.exchange_port, log_level="error")),
        uvicorn.Server(uvicorn.Config(services_app(config.data_dir, secrets["gateway_secret"]),
                                      port=config.services_port, log_level="error")),
        uvicorn.Server(uvicorn.Config(agents_app(config), port=config.agents_port, log_level="error")),
    ]

    async def serve_all():
        await asyncio.gather(*(s.serve() for s in servers))

    thread = threading.Thread(target=lambda: asyncio.run(serve_all()), daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not all(s.started for s in servers):
        assert time.time() < deadline, "servers did not start"
        time.sleep(0.05)
    admin = httpx.Client(base_url=config.exchange_url, timeout=300,
                         headers={"Authorization": f"Bearer {secrets['admin_key']}"})
    try:
        yield config, secrets, admin
    finally:
        admin.close()
        for s in servers:
            s.should_exit = True
        thread.join(timeout=5)


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


def assert_books_balance(admin: httpx.Client):
    """Supply = balances = mints, and nothing is left on hold once work has stopped."""
    audit = admin.get("/v1/admin/audit").json()
    assert audit["ok"], audit
    assert audit["active_holds"] == 0, audit
