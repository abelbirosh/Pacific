"""Contracts: premiums wait in escrow and move only on the verified result of the acceptance checks."""

import threading
import time

import httpx
import pytest

from pacific.exchange.contracts import Contracts
from pacific.faults import write_faults
from pacific.promises import SKILL_MILESTONES, SKILL_PROMISES
from support import agent_with, assert_books_balance, contract, load, settle_contracts


def as_agent(config, agent: dict) -> httpx.Client:
    return httpx.Client(base_url=config.exchange_url, timeout=300, headers={"Authorization": f"Bearer {agent['key']}"})


def parties(secrets, skill: str):
    sid, seller = agent_with(secrets, skill)
    bid, buyer = next((i, a) for i, a in secrets["agents"].items() if i != sid)
    return (sid, seller), (bid, buyer), next(o for o in seller["offerings"] if o.endswith("." + skill))


def balances(admin) -> dict[str, float]:
    return {a["id"]: a["balance"] for a in admin.get("/v1/admin/accounts").json()["accounts"]}


def escrow_balance(admin) -> float:
    return sum(a["balance"] for a in admin.get("/v1/admin/accounts", params={"kind": "escrow"}).json()["accounts"])


def db_hire(db: str, checks: list, tier: str = "standard", budget: float = 8) -> dict:
    spec = load("database.json")
    return {"description": spec["description"], "budget": budget, "inputs": {**spec["inputs"], "db": db},
            "promise": tier, "checks": checks}


USERS_5 = lambda db: {"type": "db_min_rows", "db": db, "table": "users", "min": 5}  # noqa: E731


def test_same_owner_hires_are_not_contracts():
    applies = Contracts.applies
    agent = lambda i, owner: {"id": i, "owner_id": owner}  # noqa: E731
    assert applies(None, agent("a1", "p1"), agent("a2", "p2"))
    assert not applies(None, agent("a1", "p1"), agent("a2", "p1"))
    assert not applies(None, {"id": "p1", "owner_id": None}, agent("a2", "p1"))  # a person hiring their own agent


def test_passing_checks_release_the_premium(economy):
    config, secrets, admin = economy
    (sid, _), (bid, buyer), service = parties(secrets, "database")
    before = balances(admin)
    with as_agent(config, buyer) as http:
        receipt = http.post(f"/v1/call/{service}/delegate", json=db_hire("c_pass", [
            USERS_5("c_pass"), {"type": "db_columns", "db": "c_pass", "table": "users", "columns": ["id", "email"]}],
        )).raise_for_status().json()
    assert receipt["status"] == "ok" and receipt["contract"]["status"] == "verifying"
    held = receipt["contract"]["premium_in_escrow"]["wd"]
    assert held > 0
    c = contract(admin, receipt["contract"]["id"])
    assert c["status"] == "passed" and c["claim"] == "completed" and c["attempts"] == 1
    assert c["premium"]["wd"] == held and c["released"]["wd"] == held and c["refunded"]["wd"] == 0
    after = balances(admin)
    spent = c["spent"]["wd"]
    assert after[bid] == pytest.approx(before[bid] - spent - held, abs=1e-6)  # resources plus the premium
    assert after[sid] == pytest.approx(before[sid] + held, abs=1e-6)  # its platform spend repaid, plus the premium
    assert escrow_balance(admin) == 0
    assert_books_balance(admin)


def test_failing_checks_refund_the_premium(economy):
    config, secrets, admin = economy
    (sid, _), (bid, buyer), service = parties(secrets, "database")
    before = balances(admin)
    checks = [{"type": "db_columns", "db": "c_fail", "table": "users", "columns": ["id", "nickname"]}]
    with as_agent(config, buyer) as http:
        receipt = http.post(f"/v1/call/{service}/delegate", json=db_hire("c_fail", checks)).raise_for_status().json()
    assert receipt["status"] == "ok"  # the seller says it's done; the checks say otherwise
    c = contract(admin, receipt["contract"]["id"])
    assert c["status"] == "failed" and c["released"]["wd"] == 0 and c["refunded"] == c["premium"]
    assert "nickname" in c["verdict"]["results"][0]["detail"]
    after = balances(admin)
    assert after[bid] == pytest.approx(before[bid] - c["spent"]["wd"], abs=1e-6)  # resources only
    assert after[sid] == pytest.approx(before[sid], abs=1e-6)  # repaid at cost, no premium
    assert_books_balance(admin)


def test_checks_that_already_pass_are_refused(economy):
    config, secrets, admin = economy
    (_, _), (bid, buyer), service = parties(secrets, "database")
    with as_agent(config, buyer) as http:  # the buyer builds the table itself, then asks to be shown it
        http.post("/v1/call/ocean_db/create", json={"name": "c_pre"}).raise_for_status()
        http.post("/v1/call/ocean_db/execute", json={"db": "c_pre", "sql": "CREATE TABLE users (id INTEGER)"}).raise_for_status()
        before = balances(admin)
        r = http.post(f"/v1/call/{service}/delegate", json=db_hire("c_pre", [
            {"type": "db_table_exists", "db": "c_pre", "table": "users"}, USERS_5("c_pre")]))
    assert r.status_code == 409, r.json()
    errors = r.json()["detail"]["errors"]
    assert len(errors) == 1 and errors[0].startswith("check 0:")  # only the table check already passes
    assert balances(admin) == before
    assert_books_balance(admin)


def test_a_seller_can_decline_before_spending(economy):
    config, secrets, admin = economy
    (sid, _), (bid, buyer), service = parties(secrets, "database")
    before = balances(admin)
    impossible = [{"type": "db_min_rows", "db": "c_decline", "table": "users", "min": 50}]  # the seed has 5
    with as_agent(config, buyer) as http:
        receipt = http.post(f"/v1/call/{service}/delegate", json=db_hire("c_decline", impossible)).raise_for_status().json()
    assert receipt["status"] == "failed" and receipt["cost"]["wd"] == 0
    assert receipt["contract"]["status"] == "declined" and "50 rows" in receipt["result"]["output"]
    c = contract(admin, receipt["contract"]["id"])
    assert c["status"] == "declined" and c["premium"]["wd"] == 0 and c["attempts"] == 0
    assert balances(admin) == before
    assert_books_balance(admin)


def test_checks_that_cannot_run_are_refunded_as_unverified(economy):
    config, secrets, admin = economy
    (sid, _), (bid, buyer), service = parties(secrets, "code_run")
    key = "runs/unverified.txt"
    body = {"description": "Run a slow program", "budget": 3, "promise": "standard",
            "inputs": {"code": "import time\ntime.sleep(1.5)\nprint('slept')\n", "output_key": key},
            "checks": [{"type": "storage_contains", "key": key, "text": "slept"}]}
    out = {}

    def hire():
        with as_agent(config, buyer) as http:
            out["receipt"] = http.post(f"/v1/call/{service}/delegate", json=body).raise_for_status().json()

    t = threading.Thread(target=hire)
    t.start()
    time.sleep(0.7)  # the hire has passed its pre-work check; now storage reads go down
    write_faults(config.home, [{"match": "/ocean_storage/get", "error_rate": 1.0}])
    try:
        t.join(60)
        c = contract(admin, out["receipt"]["contract"]["id"])
    finally:
        write_faults(config.home, "none")
    assert out["receipt"]["status"] == "ok"
    assert c["status"] == "unverified" and c["attempts"] == 4  # the first run and three retries
    assert c["released"]["wd"] == 0 and c["refunded"] == c["premium"] and c["premium"]["wd"] > 0
    assert_books_balance(admin)


def test_progress_payment_pays_the_milestones_that_passed(economy):
    config, secrets, admin = economy
    (_, seller), (bid, buyer), service = parties(secrets, "database")
    deliverables, templates, cost_cap, turnaround = SKILL_PROMISES["database"]["thorough"]
    tier = {"fee": 2.0, "markup_pct": 20, "cost_cap": cost_cap, "turnaround_s": turnaround,
            "deliverables": deliverables, "checks": templates, "payment": "progress",
            "milestones": SKILL_MILESTONES["database"]}
    with as_agent(config, seller) as http:
        http.put(f"/v1/services/{service}/promises/staged", json=tier).raise_for_status()
        bad = http.put(f"/v1/services/{service}/promises/staged2",
                       json={**tier, "milestones": [{**m, "share_pct": 10} for m in tier["milestones"]]})
    assert bad.status_code == 400 and "add up to 100" in bad.json()["detail"]

    schema = {"type": "db_columns", "db": "c_prog", "table": "users", "columns": ["id", "name", "email"]}
    wrong_total = {"type": "db_query_equals", "db": "c_prog", "sql": "SELECT SUM(total_cents) FROM orders", "expected": 1}
    with as_agent(config, buyer) as http:
        uncovered = http.post(f"/v1/call/{service}/delegate", json=db_hire("c_prog", [schema], "staged"))
        receipt = http.post(f"/v1/call/{service}/delegate",
                            json=db_hire("c_prog", [schema, wrong_total], "staged")).raise_for_status().json()
    assert uncovered.status_code == 400 and "milestone data has no acceptance check" in str(uncovered.json()["detail"])
    c = contract(admin, receipt["contract"]["id"])
    assert c["status"] == "partial" and c["payment"] == "progress"
    premium = round(c["premium"]["wd"] * 1e6)
    assert round(c["released"]["wd"] * 1e6) == int(premium * 0.4)  # the schema milestone's 40%
    assert c["verdict"]["payout"] == {"schema": {"share_pct": 40, "passed": True},
                                      "data": {"share_pct": 60, "passed": False}}
    assert_books_balance(admin)


def test_at_the_deadline_whatever_exists_is_verified(economy):
    config, secrets, admin = economy
    (_, seller), (bid, buyer), service = parties(secrets, "code_run")
    deliverables, templates, cost_cap, _ = SKILL_PROMISES["code_run"]["standard"]
    with as_agent(config, seller) as http:
        http.put(f"/v1/services/{service}/promises/rush", json={
            "fee": 1.0, "markup_pct": 10, "cost_cap": cost_cap, "turnaround_s": 1,
            "deliverables": deliverables, "checks": templates}).raise_for_status()
    key = "runs/late.txt"
    body = {"description": "Run a program that takes too long", "budget": 3, "promise": "rush",
            "inputs": {"code": "import time\ntime.sleep(2.5)\nprint('slept')\n", "output_key": key},
            "checks": [{"type": "storage_contains", "key": key, "text": "slept"}]}
    with as_agent(config, buyer) as http:
        receipt = http.post(f"/v1/call/{service}/delegate", json=body).raise_for_status().json()
    assert receipt["contract"]["claim"] == "timeout" and receipt["status"] == "failed"
    assert receipt["latency_ms"] >= 2000  # waited for the sandbox run already under way to bill the job
    c = contract(admin, receipt["contract"]["id"])
    assert c["status"] == "failed" and c["refunded"] == c["premium"] and c["spent"]["wd"] > 0
    assert_books_balance(admin)  # the late sandbox charge landed before the buyer was billed


def test_rankings_follow_the_track_record(economy):
    _, secrets, admin = economy
    (_, _), _, service = parties(secrets, "database")
    settle_contracts(admin)
    ranks = admin.get("/v1/rankings", params={"skill": "database"}).json()["rankings"]
    assert [r["rank"] for r in ranks] == list(range(1, len(ranks) + 1))
    assert all(r["skill"] == "database" for r in ranks)
    standard = next(r for r in ranks if r["service"] == service and r["tier"] == "standard")
    assert standard["passed"] >= 1 and standard["failed"] >= 1 and standard["declined"] >= 1
    assert 0 < standard["success"] < 1 and standard["settled"] == standard["passed"] + standard["partial"] + standard["failed"]
    untried = [r for r in ranks if r["hires"] == 0]
    assert all(r["score"] == 0.5 for r in untried)
    catalog = next(s for s in admin.get("/v1/services", params={"kind": "agent"}).json()["services"] if s["name"] == service)
    record = next(p for p in catalog["endpoints"][0]["promises"] if p["tier"] == "standard")["record"]
    assert record["score"] == standard["score"] and record["hires"] == standard["hires"]
