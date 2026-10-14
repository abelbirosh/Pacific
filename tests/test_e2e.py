"""Boots the real exchange, services and agent host on free ports and drives tasks through them.

Network-free by default (database, sandbox, storage, delegation). Set PACIFIC_NET_TESTS=1 to
also exercise the live web search tiers and OSV dependency audits.
"""

import asyncio
import json
import os
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from pacific.config import Config


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def economy(tmp_path_factory):
    from pacific.agents.host import create_app as agents_app
    from pacific.exchange.app import create_app as exchange_app
    from pacific.seed import genesis
    from pacific.services.app import create_app as services_app

    config = Config(home=tmp_path_factory.mktemp("pacific") / "home", exchange_port=free_port(),
                    services_port=free_port(), agents_port=free_port())
    genesis(config, seed=1)
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
    yield config, secrets, admin
    for s in servers:
        s.should_exit = True
    thread.join(timeout=5)


def run_task(admin, spec: dict) -> dict:
    task = admin.post("/v1/tasks", json=spec).raise_for_status().json()
    return admin.post(f"/v1/tasks/{task['id']}/run").raise_for_status().json()


def agent_with(secrets, skill=None, without=None):
    for aid, a in secrets["agents"].items():
        if (skill is None or skill in a["skills"]) and (without is None or without not in a["skills"]):
            return aid, a
    pytest.skip(f"no agent with={skill} without={without} for this seed")


def load(name: str) -> dict:
    return json.loads(open(os.path.join(os.path.dirname(__file__), "..", "tasks", name)).read())


def test_genesis_shape(economy):
    _, secrets, admin = economy
    assert len(secrets["agents"]) == 5
    for a in secrets["agents"].values():
        assert 2 <= len(a["skills"]) <= 4 and a["owner"] in secrets["people"]
    assert admin.get("/v1/admin/audit").json()["ok"]
    names = {s["name"] for s in admin.get("/v1/services").json()["services"]}
    assert {"ocean_exa", "ocean_tavily", "ocean_brave", "ocean_db", "ocean_shield", "ocean_sandbox"} <= names


def test_database_task_passes_checks(economy):
    _, secrets, admin = economy
    aid, _ = agent_with(secrets)
    report = run_task(admin, {**load("database.json"), "agent": aid})
    assert report["status"] == "passed", report["checks"]
    assert 0 < report["spent"]["wd"] <= 10


def test_code_run_fits_small_budget(economy):
    _, secrets, admin = economy
    aid, _ = agent_with(secrets)
    report = run_task(admin, {**load("code_run.json"), "agent": aid, "budget": 2})
    assert report["status"] == "passed", report["output"]
    assert report["spent"]["wd"] <= 2


def test_budget_too_small_fails_cleanly(economy):
    _, secrets, admin = economy
    aid, _ = agent_with(secrets)
    report = run_task(admin, {**load("database.json"), "agent": aid, "budget": 3})
    assert report["status"] == "failed"
    assert report["spent"]["wd"] <= 3
    assert admin.get("/v1/admin/audit").json()["ok"]


def test_delegation_charges_fee_plus_cost(economy):
    _, secrets, admin = economy
    buyer, _ = agent_with(secrets)
    spec = {**load("database.json"), "agent": buyer, "budget": 20}
    spec["inputs"] = {**spec["inputs"], "delegate": True, "db": "delegated"}
    spec["checks"] = [{"type": "db_min_rows", "db": "delegated", "table": "users", "min": 5}]
    report = run_task(admin, spec)
    assert report["status"] == "passed", report["output"]
    top = [c for c in report["calls"] if c["depth"] == 0]
    sub = [c for c in report["calls"] if c["depth"] == 1]
    assert len(top) == 1 and top[0]["service"].endswith(".database") and sub
    seller_spend = sum(c["cost"] for c in sub)
    plan = next(s for s in admin.get("/v1/services").json()["services"] if s["name"] == top[0]["service"])["endpoints"][0]["plan"]
    expected = round(plan["per_call"] * 1e6) + -(-seller_spend * (100 + plan["markup_pct"]) // 100)
    assert abs(top[0]["cost"] - expected) <= 1
    assert admin.get("/v1/admin/audit").json()["ok"]


def test_cannot_use_someone_elses_job(economy):
    config, secrets, admin = economy
    (a1, s1), (a2, s2) = list(secrets["agents"].items())[:2]
    task = admin.post("/v1/tasks", json={**load("code_run.json"), "agent": a1}).json()
    r = httpx.post(f"{config.exchange_url}/v1/call/ocean_storage/list", json={},
                   headers={"Authorization": f"Bearer {s2['key']}", "X-Pacific-Job": task["workspace"]})
    assert r.status_code == 403


def test_services_reject_direct_calls(economy):
    config, _, _ = economy
    r = httpx.post(f"{config.services_url}/ocean_db/list", headers={"X-Pacific-Workspace": "x"})
    assert r.status_code == 401


@pytest.mark.skipif(not os.environ.get("PACIFIC_NET_TESTS"), reason="needs internet")
def test_research_and_security_live(economy):
    _, secrets, admin = economy
    aid, _ = agent_with(secrets)
    assert run_task(admin, {**load("research.json"), "agent": aid})["status"] == "passed"
    assert run_task(admin, {**load("research_cheap.json"), "agent": aid})["status"] == "passed"
    assert run_task(admin, {**load("security.json"), "agent": aid})["status"] == "passed"
