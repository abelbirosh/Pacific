"""The exchange's agent-facing protocol (inspired by the Personal Agent Protocol draft): a
discovery document, a machine-readable code on every refusal, and per-account rate limits."""

import httpx
import pytest

from pacific.errors import CODES
from support import boot, load


@pytest.fixture(scope="module")
def limited(tmp_path_factory):
    """An economy with a tight rate limit (5/s, burst 5)."""
    mp = pytest.MonkeyPatch()
    mp.setenv("PACIFIC_RATE_PER_S", "5")
    mp.setenv("PACIFIC_RATE_BURST", "5")
    with boot(tmp_path_factory.mktemp("limited") / "home", seed=2) as env:
        yield env
    mp.undo()


def as_agent(config, secrets, i=0) -> httpx.Client:
    aid, a = list(secrets["agents"].items())[i]
    return httpx.Client(base_url=config.exchange_url, headers={"Authorization": f"Bearer {a['key']}"})


def test_discovery_document(economy):
    config, _, _ = economy
    doc = httpx.get(f"{config.exchange_url}/.well-known/pacific.json").json()
    assert doc["protocol_version"] == "0.1"
    assert {"me", "services", "quote", "call", "jobs", "transfer", "tasks"} <= set(doc["endpoints"])
    assert doc["apis"][0] == {"type": "openapi", "url": "/openapi.json", "description": "The exchange's full HTTP API"}
    assert set(doc["errors"]) == set(CODES)
    assert doc["rate_limits"]["per_s"] > 0 and doc["extensions"] == {}


def test_every_refusal_carries_a_known_code(economy):
    config, secrets, admin = economy
    agent = as_agent(config, secrets)
    (aid, a), (other, b) = list(secrets["agents"].items())[:2]
    task = admin.post("/v1/tasks", json={**load("code_run.json"), "agent": aid}).json()
    job = {"X-Pacific-Job": task["workspace"]}
    cases = [
        (httpx.get(f"{config.exchange_url}/v1/me"), 401, "invalid_token"),
        (agent.get("/v1/admin/audit"), 403, "forbidden"),
        (agent.post("/v1/call/ocean_storage/teleport", json={}), 404, "not_found"),
        (agent.post("/v1/call/ocean_storage/list", content=b"{nope"), 400, "invalid_request"),
        (agent.post("/v1/call/ocean_storage/list", json={}, headers={**job, "X-Pacific-Max-Cost": "0.000001"}),
         402, "max_cost_exceeded"),
        (agent.post("/v1/call/ocean_db/execute_many", json={"db": "x", "sql": "x", "rows": 5}, headers=job), 400, "unpriceable"),
        (agent.post("/v1/transfer", json={"to": other, "amount": 10**9}), 402, "insufficient_funds"),
        (agent.post("/v1/transfer", json={"to": other, "amount": -1}), 422, "invalid_request"),
        (agent.post("/v1/call/ocean_storage/list", json={}, headers={"X-Pacific-Job": "job_000000000000"}), 404, "not_found"),
        (agent.post(f"/v1/tasks/{task['id']}/run"), 403, "forbidden"),
        (agent.post(f"/v1/call/{b['offerings'][0]}/delegate", json={"description": "x", "budget": 20}, headers=job),
         402, "budget_exceeded"),
        (agent.post("/v1/call/ocean_storage/get", json={"key": "../x"}, headers=job), 400, "invalid_request"),
    ]
    for r, status, code in cases:
        assert (r.status_code, r.json().get("error")) == (status, code), r.text
        assert r.json()["error"] in CODES and r.json()["detail"]


def test_rate_limit_answers_429_with_retry_after(limited):
    config, secrets, admin = limited
    agent = as_agent(config, secrets)
    statuses = [agent.get("/v1/me") for _ in range(12)]
    refused = [r for r in statuses if r.status_code == 429]
    assert refused and sum(r.status_code == 200 for r in statuses) <= 6
    assert refused[0].json()["error"] == "rate_limited" and int(refused[0].headers["Retry-After"]) >= 1
    assert as_agent(config, secrets, 1).get("/v1/me").status_code == 200  # buckets are per account
    assert all(admin.get("/v1/admin/audit").status_code == 200 for _ in range(12))  # admin is exempt
