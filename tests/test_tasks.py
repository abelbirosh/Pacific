"""Async task runs: 202 at once, an event log to follow (cursor, long-poll, SSE), cancel, and
recovery from a restart mid-run."""

import json
import time

import httpx
from fastapi.testclient import TestClient

from pacific.config import Config
from pacific.db import Database
from pacific.ledger import Ledger
from pacific.money import to_micro
from pacific.server import free_port
from support import load

SLOW = {"code": "import time\ntime.sleep(3)\nprint('done')\n", "timeout_s": 6, "output_key": "runs/slow.txt"}


def start(admin, spec) -> str:
    task = admin.post("/v1/tasks", json=spec).raise_for_status().json()
    r = admin.post(f"/v1/tasks/{task['id']}/run")
    assert r.status_code == 202 and r.json()["status"] == "running"
    return task["id"]


def follow(admin, task_id, cursor=None, until=30) -> list[dict]:
    seen, deadline = [], time.time() + until
    while time.time() < deadline:
        page = admin.get(f"/v1/tasks/{task_id}/events", params={"wait": 10, **({"cursor": cursor} if cursor else {})}).json()
        seen += page["events"]
        cursor = page["cursor"]
        if page["status"] in ("passed", "failed", "error", "cancelled") and not page["has_more"]:
            return seen
    raise AssertionError(f"task {task_id} didn't finish")


def test_run_returns_at_once_and_events_tell_the_story(economy):
    _, secrets, admin = economy
    aid = next(iter(secrets["agents"]))
    task_id = start(admin, {**load("database.json"), "agent": aid})
    events = follow(admin, task_id)
    assert [e["status"] for e in events if e["type"] == "state"] == ["running", "checking", "passed"]
    calls = [e for e in events if e["type"] == "call"]
    assert {c["service"] for c in calls} == {"ocean_db"} and all(c["depth"] == 0 for c in calls)
    assert [e["type"] for e in events if e["type"] == "check"] == ["check"] * 4
    report = admin.get(f"/v1/tasks/{task_id}").json()
    assert round(sum(c["cost"] for c in calls), 6) == report["spent"]["wd"]

    # Resuming from any cursor returns exactly the events after it.
    middle = events[len(events) // 2]["id"]
    rest = admin.get(f"/v1/tasks/{task_id}/events", params={"cursor": middle}).json()["events"]
    assert [e["id"] for e in rest] == [e["id"] for e in events[len(events) // 2 + 1:]]


def test_events_stream_and_resume_with_last_event_id(economy):
    _, secrets, admin = economy
    aid = next(iter(secrets["agents"]))
    task_id = start(admin, {**load("code_run.json"), "agent": aid})
    follow(admin, task_id)

    def stream(headers):
        with admin.stream("GET", f"/v1/tasks/{task_id}/events", headers={"Accept": "text/event-stream", **headers}) as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            ids = [line[4:] for line in r.iter_lines() if line.startswith("id: ")]
        return ids

    everything = stream({})
    assert len(everything) >= 5
    assert stream({"Last-Event-ID": everything[1]}) == everything[2:]


def test_cancel_stops_a_task_and_leaves_nothing_behind(economy):
    _, secrets, admin = economy
    aid = next(a for a, v in secrets["agents"].items() if "code_run" in v["skills"])
    task_id = start(admin, {**load("code_run.json"), "agent": aid, "inputs": SLOW, "checks": []})
    time.sleep(0.7)
    assert admin.post(f"/v1/tasks/{task_id}/cancel").json()["status"] == "cancelled"
    events = follow(admin, task_id)
    assert [e["status"] for e in events if e["type"] == "state"] == ["running", "cancelled"]
    assert admin.get(f"/v1/tasks/{task_id}").json()["status"] == "cancelled"
    deadline = time.time() + 15  # the sandbox run in flight finishes and settles
    while not admin.get("/v1/admin/invariants", params={"quiescent": True}).json()["ok"]:
        assert time.time() < deadline, admin.get("/v1/admin/invariants", params={"quiescent": True}).json()
        time.sleep(0.3)


def test_tasks_are_visible_to_their_agent_and_owner_only(economy):
    config, secrets, admin = economy
    (aid, a), (other, b) = list(secrets["agents"].items())[:2]
    task_id = start(admin, {**load("code_run.json"), "agent": aid})
    follow(admin, task_id)
    as_key = lambda key: httpx.Client(base_url=config.exchange_url, headers={"Authorization": f"Bearer {key}"})
    owner = secrets["people"][a["owner"]]["key"]
    assert as_key(owner).get(f"/v1/tasks/{task_id}").status_code == 200
    assert as_key(a["key"]).get(f"/v1/tasks/{task_id}/events").status_code == 200
    for key in (b["key"], secrets["people"][b["owner"]]["key"]):
        assert as_key(key).get(f"/v1/tasks/{task_id}").json()["error"] == "forbidden"
        assert as_key(key).get(f"/v1/tasks/{task_id}/events").status_code == 403
    assert as_key(a["key"]).post(f"/v1/tasks/{task_id}/cancel").status_code == 403  # only the owner may cancel


def test_a_bad_cursor_is_refused(economy):
    _, secrets, admin = economy
    task_id = start(admin, {**load("code_run.json"), "agent": next(iter(secrets["agents"]))})
    for cursor in ("nope", "evt_999999"):
        r = admin.get(f"/v1/tasks/{task_id}/events", params={"cursor": cursor})
        assert (r.status_code, r.json()["error"]) == (400, "invalid_cursor")
    follow(admin, task_id)


def test_a_restart_fails_interrupted_tasks_and_releases_their_holds(tmp_path):
    from pacific.exchange.app import create_app
    from pacific.seed import genesis

    config = Config(home=tmp_path / "home", exchange_port=free_port(), services_port=free_port(), agents_port=free_port())
    genesis(config, seed=4)
    secrets = config.secrets()
    auth = {"Authorization": f"Bearer {secrets['admin_key']}"}
    aid = next(iter(secrets["agents"]))
    before = TestClient(create_app(config), headers=auth)
    task = before.post("/v1/tasks", json={**load("code_run.json"), "agent": aid}).json()

    # Simulate dying mid-run: the task running, a hold on its job, a call still pending.
    db = Database(config.ledger_path)
    with db.tx() as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task["id"],))
        Ledger(db).place_hold(conn, aid, to_micro(1), task["workspace"])
        conn.execute("INSERT INTO calls(id, caller_id, service, endpoint, job_id, held) VALUES "
                     "('call_x', ?, 'ocean_sandbox', 'run', ?, ?)", (aid, task["workspace"], to_micro(1)))

    after = TestClient(create_app(config), headers=auth)  # startup recovers
    report = after.get(f"/v1/tasks/{task['id']}").json()
    assert report["status"] == "error" and "restarted" in report["output"]
    last = after.get(f"/v1/tasks/{task['id']}/events").json()["events"][-1]
    assert (last["type"], last["status"]) == ("state", "error")
    check = after.get("/v1/admin/invariants", params={"quiescent": True}).json()
    assert check["ok"], json.dumps(check["violations"])
