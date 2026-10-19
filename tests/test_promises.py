"""Endpoint promises: what a seller can promise, how buyers' checks are held to it, and versioning."""

import json

import httpx
import pytest

from pacific.promises import SKILL_PROMISES, PromiseError, validate_checks, validate_terms
from support import agent_with, assert_books_balance, load

STANDARD_DB = SKILL_PROMISES["database"]["standard"][1]
THOROUGH_DB = SKILL_PROMISES["database"]["thorough"][1]
INPUTS = {"db": "shop"}


# ---- what a seller can promise -------------------------------------------------------------------


def terms(**over):
    base = dict(tier="standard", fee=1.0, markup_pct=10, cost_cap=10, turnaround_s=60,
                deliverables=[{"name": "db", "description": "the database"}], checks=STANDARD_DB)
    return {**base, **over}


def test_genesis_promises_are_valid():
    for skill, tiers in SKILL_PROMISES.items():
        for tier, (deliverables, checks, cost_cap, turnaround_s) in tiers.items():
            validate_terms(tier, 1.0, 10, cost_cap, turnaround_s, deliverables, checks)


@pytest.mark.parametrize("over, message", [
    ({"checks": [{"type": "output_contains", "text": {"str": {}}}]}, "seller's own report"),
    ({"checks": [{"type": "db_vibes", "db": {"input": "db"}}]}, "unknown check type"),
    ({"checks": [{"type": "db_min_rows", "db": {"input": "db"}, "table": {"str": {}}}]}, "missing ['min']"),
    ({"checks": [{"type": "db_min_rows", "db": "shop", "table": {"str": {}}, "min": {"int": {}}}]}, "constraint object"),
    ({"checks": [{"type": "db_table_exists", "db": {"input": "db"}, "table": {"str": {}}, "rows": {"int": {}}}]},
     "no fields ['rows']"),
    ({"checks": [{"type": "db_table_exists", "db": {"input": "db"}, "table": {"str": {}, "optional": True}}]},
     "can't be optional"),
    ({"checks": [{"type": "db_min_rows", "db": {"input": "db"}, "table": {"str": {}}, "min": {"int": {"min": 9, "max": 1}}}]},
     "min > max"),
    ({"checks": []}, "1-12 check templates"),
    ({"tier": "Gold Tier"}, "tier must be"),
    ({"cost_cap": 0}, "cost_cap must be positive"),
    ({"fee": float("inf")}, "fee must be"),
    ({"markup_pct": 900}, "markup_pct must be"),
    ({"turnaround_s": 0}, "turnaround_s"),
    ({"deliverables": []}, "deliverables"),
])
def test_bad_promises_are_refused(over, message):
    with pytest.raises(PromiseError, match=message.replace("[", r"\[").replace("]", r"\]")):
        validate_terms(**terms(**over))


# ---- holding a buyer's checks to the promise ---------------------------------------------------


def test_checks_within_the_templates_pass():
    checks = [{"type": "db_min_rows", "db": "shop", "table": "users", "min": 5},
              {"type": "db_columns", "db": "shop", "table": "users", "columns": ["id", "email"]},
              {"type": "db_table_exists", "db": "shop", "table": "orders"}]
    assert validate_checks(STANDARD_DB, checks, INPUTS) == []
    assert validate_checks(STANDARD_DB, [], INPUTS) == []


@pytest.mark.parametrize("check, message", [
    ({"type": "db_min_rows", "db": "other", "table": "users", "min": 5}, "must equal inputs.db"),
    ({"type": "db_min_rows", "db": "shop", "table": "users", "min": 500}, "must be in [0, 100]"),
    ({"type": "db_min_rows", "db": "shop", "table": "users", "min": True}, "must be an integer"),
    ({"type": "db_min_rows", "db": "shop", "table": "users"}, "`min` is required"),
    ({"type": "db_min_rows", "db": "shop", "table": "users", "min": 1, "where": "x"}, "aren't allowed"),
    ({"type": "db_columns", "db": "shop", "table": "users", "columns": ["c"] * 21}, "1-20 strings"),
    ({"type": "db_query_equals", "db": "shop", "sql": "SELECT 1", "expected": 1}, "allows no 'db_query_equals'"),
    ({"type": "output_contains", "text": "done"}, "allows no 'output_contains'"),
    ("not a check", "must be an object"),
])
def test_checks_outside_the_templates_are_refused(check, message):
    errors = validate_checks(STANDARD_DB, [check], INPUTS)
    assert len(errors) == 1 and message in errors[0], errors


def test_input_bound_fields_need_the_input():
    errors = validate_checks(STANDARD_DB, [{"type": "db_table_exists", "db": "shop", "table": "t"}], {})
    assert "which isn't set" in errors[0]


def test_a_thorough_tier_allows_more():
    check = {"type": "db_query_equals", "db": "shop", "sql": "SELECT SUM(total_cents) FROM orders", "expected": 6749}
    assert validate_checks(THOROUGH_DB, [check], INPUTS) == []
    assert validate_checks(THOROUGH_DB, [{"type": "db_min_rows", "db": "shop", "table": "u", "min": 5000}], INPUTS) == []


def test_optional_fields():
    templates = [{"type": "security_scan_clean", "keys": {"strs": {}}, "max_severity": {"one_of": ["HIGH"], "optional": True}}]
    assert validate_checks(templates, [{"type": "security_scan_clean", "keys": ["a.py"]}], {}) == []
    assert validate_checks(templates, [{"type": "security_scan_clean", "keys": "a.py", "max_severity": "HIGH"}], {}) == []
    assert validate_checks(templates, [{"type": "security_scan_clean", "keys": "a.py", "max_severity": "LOW"}], {})


# ---- promises on the live exchange ---------------------------------------------------------------


def as_agent(config, agent: dict) -> httpx.Client:
    return httpx.Client(base_url=config.exchange_url, timeout=300, headers={"Authorization": f"Bearer {agent['key']}"})


def seller_and_buyer(secrets, skill="database"):
    sid, seller = agent_with(secrets, skill)
    bid, buyer = next((i, a) for i, a in secrets["agents"].items() if i != sid)
    return (sid, seller), (bid, buyer), next(o for o in seller["offerings"] if o.endswith("." + skill))


def hire_body(db: str, **terms):
    spec = load("database.json")
    return {"description": spec["description"], "budget": 8, "inputs": {**spec["inputs"], "db": db}, **terms}


def test_catalog_lists_tiers_cheapest_first(economy):
    _, _, admin = economy
    for s in admin.get("/v1/services", params={"kind": "agent"}).json()["services"]:
        ep = s["endpoints"][0]
        tiers = [p["tier"] for p in ep["promises"]]
        assert tiers == ["standard", "thorough"], s["name"]
        standard, thorough = ep["promises"]
        assert standard["fee"] == ep["plan"]["per_call"] and standard["markup_pct"] == ep["plan"]["markup_pct"]
        assert thorough["fee"] > standard["fee"] and thorough["cost_cap"] > standard["cost_cap"]
        assert standard["version"] == 1 and standard["status"] == "active" and "standard v1" in standard["terms"]
    platform = admin.get("/v1/services", params={"kind": "platform"}).json()["services"]
    assert all(e["promises"] == [] for s in platform for e in s["endpoints"])


def test_hire_under_a_tier_is_billed_by_it(economy):
    config, secrets, admin = economy
    _, (_, buyer), service = seller_and_buyer(secrets)
    promise = admin.get(f"/v1/services/{service}/promises").json()["promises"][1]  # thorough
    checks = [{"type": "db_min_rows", "db": "tiered", "table": "users", "min": 5},
              {"type": "db_query_equals", "db": "tiered", "sql": "SELECT SUM(total_cents) FROM orders", "expected": 6749}]
    body = hire_body("tiered", promise="thorough", promise_version=promise["version"], checks=checks)
    with as_agent(config, buyer) as http:
        quote = http.post(f"/v1/quote/{service}/delegate", json=body).json()
        assert quote["promise"]["id"] == promise["id"]
        fee = round(promise["fee"] * 1e6)
        assert quote["max_cost"]["micro"] == fee + -(-8_000_000 * (100 + promise["markup_pct"]) // 100)
        r = http.post(f"/v1/call/{service}/delegate", json=body)
    receipt = r.raise_for_status().json()
    assert receipt["status"] == "ok", receipt
    assert receipt["promise"] == {"id": promise["id"], "tier": "thorough", "version": promise["version"]}
    assert receipt["cost"]["micro"] > fee  # the thorough fee, plus the seller's spend at its markup
    assert_books_balance(admin)


@pytest.mark.parametrize("terms, status, message", [
    ({"promise": "platinum"}, 404, "offers no tier 'platinum'"),
    ({"promise": "standard", "promise_version": 99}, 409, "not 99"),
    ({"promise": "standard", "checks": [{"type": "db_min_rows", "db": "elsewhere", "table": "users", "min": 1}]},
     400, "doesn't allow"),
    ({"promise": "standard", "checks": [{"type": "output_contains", "text": "done"}]}, 400, "doesn't allow"),
    ({"promise": "standard", "budget": 1000}, 400, "over the standard promise's cost cap"),
    ({"checks": []}, 400, "need a `promise` tier"),
    ({"promise": 7}, 400, "names a tier"),
])
def test_bad_hires_are_refused_before_money_moves(economy, terms, status, message):
    config, secrets, admin = economy
    _, (bid, buyer), service = seller_and_buyer(secrets)
    before = admin.get("/v1/admin/accounts").json()["accounts"]
    with as_agent(config, buyer) as http:
        r = http.post(f"/v1/call/{service}/delegate", json={**hire_body("refused"), **terms})
    assert r.status_code == status and message in str(r.json()["detail"]), r.json()
    assert admin.get("/v1/admin/accounts").json()["accounts"] == before


def test_platform_services_take_no_promise(economy):
    config, secrets, _ = economy
    _, buyer = next(iter(secrets["agents"].items()))
    with as_agent(config, buyer) as http:
        r = http.post("/v1/call/ocean_storage/list", json={"promise": "standard"})
    assert r.status_code == 400 and "don't offer promises" in r.json()["detail"]


def test_changing_a_promise_makes_a_new_version(economy):
    config, secrets, admin = economy
    (_, seller), (_, buyer), service = seller_and_buyer(secrets)
    v1 = admin.get(f"/v1/services/{service}/promises").json()["promises"][0]
    with as_agent(config, buyer) as http:
        first = http.post(f"/v1/call/{service}/delegate",
                          json=hire_body("before_change", promise="standard", promise_version=v1["version"]))
    assert first.raise_for_status().json()["promise"]["id"] == v1["id"]

    change = {"fee": v1["fee"] + 1, "markup_pct": v1["markup_pct"], "cost_cap": v1["cost_cap"],
              "turnaround_s": 60, "deliverables": v1["deliverables"], "checks": v1["checks"]}
    with as_agent(config, buyer) as http:  # only the provider can change it
        assert http.put(f"/v1/services/{service}/promises/standard", json=change).status_code == 403
    with as_agent(config, seller) as http:
        v2 = http.put(f"/v1/services/{service}/promises/standard", json=change).raise_for_status().json()
        bad = http.put(f"/v1/services/{service}/promises/standard", json={**change, "checks": [{"type": "output_contains"}]})
    assert bad.status_code == 400 and "seller's own report" in bad.json()["detail"]
    assert v2["version"] == v1["version"] + 1 and v2["fee"] == v1["fee"] + 1 and v2["id"] != v1["id"]

    history = admin.get(f"/v1/services/{service}/promises", params={"history": True}).json()["promises"]
    old = next(p for p in history if p["id"] == v1["id"])
    assert old["status"] == "superseded" and old["fee"] == v1["fee"]  # its terms never change
    current = [p for p in admin.get(f"/v1/services/{service}/promises").json()["promises"] if p["tier"] == "standard"]
    assert [p["id"] for p in current] == [v2["id"]]

    with as_agent(config, buyer) as http:  # a buyer quoted v1 is told the terms moved
        stale = http.post(f"/v1/call/{service}/delegate",
                          json=hire_body("stale", promise="standard", promise_version=v1["version"]))
    assert stale.status_code == 409
    assert_books_balance(admin)  # the first hire is still billed at v1's terms


def test_retiring_a_tier(economy):
    config, secrets, admin = economy
    (_, seller), (_, buyer), service = seller_and_buyer(secrets, "code_run")
    with as_agent(config, seller) as http:
        assert http.delete(f"/v1/services/{service}/promises/thorough").raise_for_status().json()["retired"] == "thorough"
        assert http.delete(f"/v1/services/{service}/promises/thorough").status_code == 404
    tiers = [p["tier"] for p in admin.get(f"/v1/services/{service}/promises").json()["promises"]]
    assert tiers == ["standard"]
    with as_agent(config, buyer) as http:
        r = http.post(f"/v1/call/{service}/delegate",
                      json={"description": "run it", "budget": 2, "inputs": {"code": "print(1)"}, "promise": "thorough"})
    assert r.status_code == 404


def test_scripted_brain_hires_under_the_cheapest_tier(economy):
    _, secrets, admin = economy
    buyer, _ = agent_with(secrets)
    spec = {**load("database.json"), "agent": buyer, "budget": 20}
    spec["inputs"] = {**spec["inputs"], "delegate": True, "db": "brain_tier"}
    spec["checks"] = [{"type": "db_min_rows", "db": "brain_tier", "table": "users", "min": 5},
                      {"type": "used_service", "service": "ocean_db"}]
    task = admin.post("/v1/tasks", json=spec).raise_for_status().json()
    report = admin.post(f"/v1/tasks/{task['id']}/run").raise_for_status().json()
    assert report["status"] == "passed", report["output"]
    hire = next(c for c in report["calls"] if c["depth"] == 0)
    assert hire["promise_id"] and "tier)" in report["output"]
    # only the check the tier allows goes along; used_service isn't in the standard database templates
    assert json.loads(hire["acceptance"]) == [spec["checks"][0]]
    assert_books_balance(admin)
