"""Idempotency-Key: a retried request is answered, never redone or charged twice."""

import asyncio
import threading

import httpx

from pacific.faults import write_faults
from pacific.idempotency import ReplayCache
from support import load


def setup(economy, budget=40):
    config, secrets, admin = economy
    aid, a = list(secrets["agents"].items())[0]
    task = admin.post("/v1/tasks", json={**load("code_run.json"), "agent": aid, "budget": budget}).json()
    client = httpx.Client(base_url=config.exchange_url, timeout=60,
                          headers={"Authorization": f"Bearer {a['key']}", "X-Pacific-Job": task["workspace"]})
    return config, admin, client, task


def call(client, service, endpoint, body, key=None):
    return client.post(f"/v1/call/{service}/{endpoint}", json=body, headers={"Idempotency-Key": key} if key else {})


def charges(admin, task, key):
    return [c["cost"] for c in admin.get(f"/v1/tasks/{task['id']}").json()["calls"] if c["idempotency_key"] == key]


def rows_in(client, db):
    return call(client, "ocean_db", "query", {"db": db, "sql": "SELECT COUNT(*) FROM t"}).json()["result"]["rows"][0][0]


def lose_reply(config, send):
    """Send a request whose provider does the work and then answers 500, as if the reply was lost."""
    write_faults(config.home, [{"match": "/ocean_db/execute_many", "after_rate": 1.0}])
    try:
        return send()
    finally:
        write_faults(config.home, "none")


def test_a_lost_reply_is_replayed_not_redone(economy):
    config, admin, client, task = setup(economy)
    for db in ("keyed", "unkeyed"):
        assert call(client, "ocean_db", "create", {"name": db}).status_code == 200
        call(client, "ocean_db", "execute", {"db": db, "sql": "CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)"})
    seed = lambda db: {"db": db, "sql": "INSERT INTO t(v) VALUES (?)", "rows": [[i] for i in range(5)]}

    lost = lose_reply(config, lambda: call(client, "ocean_db", "execute_many", seed("keyed"), key="seed-1"))
    assert lost.status_code == 500 and lost.json()["error"] == "provider_failed" and lost.json()["cost"]["wd"] == 0
    retry = call(client, "ocean_db", "execute_many", seed("keyed"), key="seed-1")
    assert retry.status_code == 200 and retry.json()["provider_replayed"]
    assert rows_in(client, "keyed") == 5  # the work ran once...
    assert [c > 0 for c in charges(admin, task, "seed-1")] == [False, True]  # ...and was paid for once

    # Without a key the retry can't be recognised: the rows go in twice (what keys prevent).
    lose_reply(config, lambda: call(client, "ocean_db", "execute_many", seed("unkeyed")))
    call(client, "ocean_db", "execute_many", seed("unkeyed"))
    assert rows_in(client, "unkeyed") == 10
    assert admin.get("/v1/admin/invariants").json()["ok"]


def test_a_retry_gets_the_original_response_and_one_charge(economy):
    _, admin, client, task = setup(economy)
    body = {"key": "notes/a.txt", "content": "hello " * 400}
    first = call(client, "ocean_storage", "put", body, key="put-1")
    again = call(client, "ocean_storage", "put", body, key="put-1")
    assert first.status_code == again.status_code == 200
    assert again.headers["Idempotent-Replayed"] == "true" and again.json() == first.json()
    assert len(charges(admin, task, "put-1")) == 1


def test_reusing_a_key_for_a_different_request_is_refused(economy):
    _, admin, client, task = setup(economy)
    assert call(client, "ocean_storage", "list", {}, key="list-1").status_code == 200
    other = call(client, "ocean_storage", "list", {"prefix": "x"}, key="list-1")
    assert (other.status_code, other.json()["error"]) == (409, "idempotency_key_conflict")
    assert len(charges(admin, task, "list-1")) == 1


def test_a_concurrent_copy_is_told_to_wait(economy):
    _, _, client, _ = setup(economy)
    body = {"code": "import time\ntime.sleep(1.5)\nprint('done')\n", "timeout_s": 5}
    results = []
    threads = [threading.Thread(target=lambda: results.append(call(client, "ocean_sandbox", "run", body, key="slow-1")))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted((r.status_code, r.json().get("error")) for r in results) == [(200, None), (409, "request_in_progress")]


def test_transfers_are_idempotent(economy):
    config, secrets, _ = economy
    (a, ka), (b, _) = list(secrets["agents"].items())[:2]
    payer = httpx.Client(base_url=config.exchange_url, headers={"Authorization": f"Bearer {ka['key']}"})
    before = payer.get("/v1/me").json()["balance"]["micro"]
    for _ in range(3):
        r = payer.post("/v1/transfer", json={"to": b, "amount": 1.5}, headers={"Idempotency-Key": "tip-1"})
        assert r.status_code == 200
    assert before - payer.get("/v1/me").json()["balance"]["micro"] == 1_500_000


def test_a_provider_makes_a_copy_wait_for_the_original():
    async def scenario():
        cache, key, order = ReplayCache(), ("ws", "id"), []
        assert await cache.claim(key) is None  # the original does the work

        async def copy():
            order.append(("copy got", await cache.claim(key)))

        waiter = asyncio.create_task(copy())
        await asyncio.sleep(0.05)
        assert not waiter.done()  # the copy waits rather than redoing it
        cache.complete(key, (200, [], b"result"))
        await waiter
        return order

    assert asyncio.run(scenario()) == [("copy got", (200, [], b"result"))]
