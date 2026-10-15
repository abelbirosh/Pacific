"""Boots the real exchange, services and agent host on free ports and drives tasks through them.

Network-free by default (database, sandbox, storage, delegation). Set PACIFIC_NET_TESTS=1 to
also exercise the live web search tiers and OSV dependency audits.
"""

import os

import httpx
import pytest

from support import agent_with, load, run_task


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


def test_unpriceable_requests_are_refused_not_crashed(economy):
    config, secrets, admin = economy
    (buyer, b), (seller, _) = list(secrets["agents"].items())[:2]
    offering = secrets["agents"][seller]["offerings"][0]
    task = admin.post("/v1/tasks", json={**load("code_run.json"), "agent": buyer}).json()
    post = lambda path, **kw: httpx.post(f"{config.exchange_url}{path}", **kw, headers={
        "Authorization": f"Bearer {b['key']}", "X-Pacific-Job": task["workspace"], "Content-Type": "application/json"})
    for budget in (b"1e999", b"true", b'"5"', b"-1"):
        r = post(f"/v1/call/{offering}/delegate", content=b'{"description": "x", "budget": ' + budget + b"}")
        assert r.status_code == 400, (budget, r.text)
    r = post("/v1/call/ocean_db/execute_many", json={"db": "x", "sql": "SELECT 1", "rows": 5})
    assert r.status_code == 400, r.text
    assert admin.get("/v1/admin/audit").json()["active_holds"] == 0
