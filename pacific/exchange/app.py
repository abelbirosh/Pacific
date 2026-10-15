"""The Pacific exchange: identity, the service catalog, metered billing, budgets and tasks.

Every purchase goes through `POST /v1/call/{service}/{endpoint}`:

    1. price the request's worst case from the endpoint's plan and place a hold
       (fails with 402 if the caller's balance or job budget can't cover it)
    2. forward to the provider: the platform services host, or an agent (delegation)
    3. compute the actual cost from reported usage and settle: charge provider, release rest

Delegation opens a child job for the hired agent, with the buyer's stated budget. The hired
agent pays for its own platform calls under that job, and the buyer is then charged the
agent's fee plus that spend at cost-plus. All resources created anywhere in one task tree
land in the root job's workspace, so checks can find them.
"""


import json
import math
import os
import secrets as pysecrets
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, Field

from ..config import Config
from .. import idempotency, invariants, ratelimit
from ..db import Database
from ..errors import CODES, PacificError, body as error_body, code_for
from ..grants import Signer
from ..ids import new_id
from ..ledger import Ledger, LedgerError
from ..money import fmt, to_micro, to_wd
from ..registry import Registry, register_agent_offering
from .checks import CheckContext, run_checks

PROTOCOL_VERSION = "0.1"
PLATFORM_TIMEOUT = 120
DELEGATION_TIMEOUT = 900


def jsonable_errors(exc: RequestValidationError) -> list[dict]:
    return [{k: v for k, v in e.items() if k in ("loc", "msg", "type")} for e in exc.errors()]


def create_app(config: Config, db: Database | None = None) -> FastAPI:
    secrets = config.secrets()
    platform_timeout = float(os.environ.get("PACIFIC_PLATFORM_TIMEOUT", PLATFORM_TIMEOUT))
    delegation_timeout = float(os.environ.get("PACIFIC_DELEGATION_TIMEOUT", DELEGATION_TIMEOUT))
    db = db or Database(config.ledger_path)
    signer = Signer.load(config.home)  # signs a grant for every request forwarded to a provider
    idem = idempotency.Store(db, stale_after_s=delegation_timeout + 60)
    ledger, registry = Ledger(db), Registry(db)
    # No connection cap: a delegated call holds its connection while the hired agent makes calls
    # of its own, so a capped pool can fill with callers waiting on callees that can't connect.
    http = httpx.AsyncClient(limits=httpx.Limits(max_connections=None, max_keepalive_connections=200))

    @asynccontextmanager
    async def lifespan(_):
        yield
        await http.aclose()

    app = FastAPI(title="Pacific exchange", lifespan=lifespan)

    # Every refusal carries a machine-readable `error` code (see errors.py).
    @app.exception_handler(LedgerError)
    async def ledger_error(_, exc: LedgerError):
        return JSONResponse(error_body(exc.code, str(exc)), status_code=exc.status_code)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_, exc: StarletteHTTPException):
        code = getattr(exc, "code", None) or code_for(exc.status_code)
        return JSONResponse(error_body(code, exc.detail, **getattr(exc, "extra", {})), status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def invalid_body(_, exc: RequestValidationError):
        return JSONResponse(error_body("invalid_request", jsonable_errors(exc)), status_code=422)

    # The exchange should refuse bad input, never fall over on it. Unhandled exceptions are kept
    # (most recent 100) and reported by /v1/admin/invariants.
    unhandled: list[dict] = []

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        unhandled.append({"path": request.url.path, "error": f"{type(exc).__name__}: {exc}"[:300]})
        del unhandled[:-100]
        return JSONResponse(error_body("internal_error", "internal error"), status_code=500)

    rate, burst = ratelimit.limits_from_env()
    ratelimit.install(app, secrets["admin_key"], rate, burst)

    # ---- discovery ----------------------------------------------------------------------------

    @app.get("/.well-known/pacific.json")
    def discovery():
        """What an agent needs to work with this exchange (inspired by PAP's poppy.json)."""
        return {
            "protocol_version": PROTOCOL_VERSION,
            "organization": {"name": "Pacific Exchange", "url": config.exchange_url},
            "auth": {"schemes": ["bearer"]},
            "endpoints": {
                "me": "/v1/me", "services": "/v1/services", "quote": "/v1/quote/{service}/{endpoint}",
                "call": "/v1/call/{service}/{endpoint}", "jobs": "/v1/jobs/{job_id}", "transfer": "/v1/transfer",
                "offer": "/v1/services", "tasks": "/v1/tasks", "run_task": "/v1/tasks/{task_id}/run",
            },
            "apis": [{"type": "openapi", "url": "/openapi.json", "description": "The exchange's full HTTP API"}],
            "headers": {"job": "X-Pacific-Job", "max_cost": "X-Pacific-Max-Cost"},
            "errors": {code: {"status": status, "meaning": meaning} for code, (status, meaning) in CODES.items()},
            "rate_limits": {"per_s": rate, "burst": burst} if rate > 0 else None,
            "extensions": {},
        }

    # ---- auth -------------------------------------------------------------------------------

    def bearer(authorization: str = Header(default="")) -> str:
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer API key")
        return authorization.removeprefix("Bearer ").strip()

    def is_admin(key: str) -> bool:
        return pysecrets.compare_digest(key, secrets["admin_key"])

    def account(key: str = Depends(bearer)):
        row = ledger.account_by_key(key)
        if row is None:
            raise HTTPException(401, "unknown API key")
        return row

    def admin(key: str = Depends(bearer)):
        if not is_admin(key):
            raise HTTPException(403, "admin key required")

    def admin_or_account(key: str = Depends(bearer)):
        """None for admin, otherwise the account row."""
        if is_admin(key):
            return None
        return account(key)

    def money(micro: int) -> dict:
        return {"micro": micro, "wd": to_wd(micro), "display": fmt(micro)}

    # ---- identity & catalog -----------------------------------------------------------------

    @app.get("/v1/me")
    def me(acct=Depends(account)):
        with db.tx() as conn:
            available = ledger.available(conn, acct["id"])
        return {
            "id": acct["id"], "kind": acct["kind"], "name": acct["name"], "owner_id": acct["owner_id"],
            "profile": json.loads(acct["profile"]), "balance": money(acct["balance"]), "available": money(available),
        }

    @app.get("/v1/services")
    def services(kind: str | None = None, skill: str | None = None):
        return {"services": registry.catalog(kind, skill)}

    @app.get("/v1/services/{name}")
    def service(name: str):
        match = [s for s in registry.catalog() if s["name"] == name]
        if not match:
            raise HTTPException(404, f"no service {name!r}")
        return match[0]

    class Offering(BaseModel):
        skill: str = Field(pattern=r"^[a-z_]{2,32}$")
        description: str = Field(min_length=5, max_length=300)
        fee: float = Field(ge=0)
        markup_pct: float = Field(ge=0, le=500)

    @app.post("/v1/services")
    def offer(req: Offering, acct=Depends(account)):
        """An agent lists a skill it will perform for other agents (task delegation)."""
        if acct["kind"] != "agent":
            raise HTTPException(403, "only agents can list delegation services")
        with db.tx() as conn:
            name = register_agent_offering(conn, acct["id"], acct["name"], req.skill, req.description,
                                           req.fee, req.markup_pct, config.agents_url)
        return {"service": name}

    def _price(plan, body: dict) -> int:
        try:
            return plan.max_cost(body)
        except Exception as e:  # malformed input in a field the plan measures
            raise PacificError("unpriceable", f"can't price this request: {type(e).__name__}: {e}")

    @app.post("/v1/quote/{service}/{endpoint}")
    async def quote(service: str, endpoint: str, request: Request):
        ep = registry.endpoint(service, endpoint)
        if ep is None:
            raise HTTPException(404, f"no endpoint {service}/{endpoint}")
        body = await _json(request)
        plan = registry.plan(ep)
        return {"max_cost": money(_price(plan, body)), "floor": money(to_micro(plan.per_call)), "price": plan.describe()}

    # ---- money movement ---------------------------------------------------------------------

    class Transfer(BaseModel):
        to: str
        amount: float = Field(gt=0)
        memo: str = ""

    @app.post("/v1/transfer")
    async def transfer(req: Transfer, acct=Depends(account), idempotency_key: str | None = Header(default=None)):
        async def handler():
            dest = ledger.resolve(req.to)
            if dest is None:
                raise HTTPException(404, f"no account {req.to!r}")
            with db.tx() as conn:
                ledger.transfer(conn, acct["id"], dest["id"], to_micro(req.amount), req.memo)
            return 200, {"ok": True, "to": dest["id"], "amount": money(to_micro(req.amount))}, True

        return await idem.run(acct["id"], idempotency_key, idempotency.fingerprint("transfer", req.model_dump()), handler)

    @app.get("/v1/jobs/{job_id}")
    def job(job_id: str, who=Depends(admin_or_account)):
        status = ledger.job_status(job_id)
        if who is not None and status["account_id"] != who["id"]:
            raise HTTPException(403, "not your job")
        return {**status, **{k: money(status[k]) for k in ("budget", "spent", "held", "remaining")}}

    # ---- the metered call -------------------------------------------------------------------

    async def _json(request: Request) -> dict:
        raw = await request.body()
        if not raw:
            return {}
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            raise HTTPException(400, "body must be JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be a JSON object")
        return body

    @app.post("/v1/call/{service}/{endpoint}")
    async def call(
        service: str,
        endpoint: str,
        request: Request,
        acct=Depends(account),
        x_pacific_job: str | None = Header(default=None),
        x_pacific_max_cost: float | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ):
        body = await _json(request)
        return await idem.run(
            acct["id"], idempotency_key,
            idempotency.fingerprint("call", service, endpoint, x_pacific_job, x_pacific_max_cost, body),
            lambda: _call(service, endpoint, body, acct, x_pacific_job, x_pacific_max_cost, idempotency_key),
        )

    async def _call(service: str, endpoint: str, body: dict, acct, x_pacific_job: str | None,
                    x_pacific_max_cost: float | None, idempotency_key: str | None) -> tuple[int, dict, bool]:
        """One metered call. Returns (HTTP status, body, final): `final` is False for outcomes
        a retry could change (the provider failed or timed out), which aren't kept for replay."""
        svc, ep = registry.service(service), registry.endpoint(service, endpoint)
        if svc is None or ep is None:
            raise HTTPException(404, f"no endpoint {service}/{endpoint}")
        if svc["provider_id"] == acct["id"]:
            raise HTTPException(400, "an agent cannot buy its own service")
        plan = registry.plan(ep)
        idem_claim = idempotency.provider_id(acct["id"], idempotency_key) if idempotency_key else None

        job = None
        if x_pacific_job:
            job = ledger.job_status(x_pacific_job)
            if job["account_id"] != acct["id"]:
                raise HTTPException(403, "that job belongs to another account")
        budget = body.get("budget")
        if svc["kind"] == "agent" and not (isinstance(budget, (int, float)) and not isinstance(budget, bool)
                                           and math.isfinite(budget) and budget > 0):
            raise HTTPException(400, "delegation needs a positive `budget` for the hired agent")

        hold_amount = _price(plan, body)
        if x_pacific_max_cost is not None and hold_amount > to_micro(x_pacific_max_cost):
            raise PacificError("max_cost_exceeded",
                               f"worst-case cost {fmt(hold_amount)} exceeds your max {x_pacific_max_cost} WD")

        call_id = new_id("call")
        with db.tx() as conn:
            hold_id = ledger.place_hold(conn, acct["id"], hold_amount, x_pacific_job)
            conn.execute(
                "INSERT INTO calls(id, caller_id, service, endpoint, job_id, held, idempotency_key) VALUES (?,?,?,?,?,?,?)",
                (call_id, acct["id"], service, endpoint, x_pacific_job, hold_amount, idempotency_key),
            )

        workspace = job["root_job_id"] if job else acct["id"]
        t0 = time.monotonic()
        cost, units, status, error, payload, http_status = 0, 0.0, "failed", None, None, 502
        child, provider_replayed = None, False
        try:
            if svc["kind"] == "platform":
                raw = json.dumps(body).encode()
                resp = await http.post(
                    svc["base_url"] + ep["path"], content=raw, timeout=platform_timeout,
                    headers=signer.headers(ep["path"], raw, ws=workspace, call=call_id, sub=acct["id"], idem=idem_claim),
                )
                http_status = resp.status_code
                provider_replayed = resp.headers.get(idempotency.REPLAYED) == "true"
                data = resp.json()
                if resp.is_success:
                    units = float(data.get("usage", {}).get("units", 0))
                    cost, status, payload = plan.cost(body, units=units), "ok", data["result"]
                else:
                    error = data.get("detail", resp.text)
            else:
                with db.tx() as conn:
                    child = ledger.open_job(conn, svc["provider_id"], to_micro(body["budget"]), parent_job_id=x_pacific_job)
                    conn.execute("UPDATE calls SET child_job_id = ? WHERE id = ?", (child, call_id))
                raw = json.dumps({"task": body, "job_id": child, "buyer_id": acct["id"],
                                  "skill": json.loads(svc["meta"]).get("skill")}).encode()
                resp = await http.post(
                    svc["base_url"] + ep["path"], content=raw, timeout=delegation_timeout,
                    headers=signer.headers(ep["path"], raw, ws=workspace, call=call_id, sub=acct["id"], idem=idem_claim),
                )
                with db.tx() as conn:
                    conn.execute("UPDATE jobs SET status = 'closed' WHERE id = ?", (child,))
                passthrough = ledger.job_status(child)["spent"]
                units = to_wd(passthrough)
                http_status = resp.status_code
                data = resp.json()
                if resp.is_success and data.get("status") == "completed":
                    cost, status, payload = plan.cost(body, passthrough=passthrough), "ok", data
                else:
                    # The hired agent failed: no fee or markup, but the buyer still covers what
                    # was genuinely spent on its behalf.
                    cost, payload = passthrough, data
                    error = data.get("detail") or data.get("output") or "delegated task failed"
                    http_status = 200 if resp.is_success else http_status
        except Exception as e:  # whatever went wrong, the hold below must still be settled
            error = f"provider unreachable or bad response: {type(e).__name__}: {e}"
            if child:  # whatever the hired agent already spent is still owed at cost
                with db.tx() as conn:
                    conn.execute("UPDATE jobs SET status = 'closed' WHERE id = ?", (child,))
                cost = ledger.job_status(child)["spent"]
        latency = int((time.monotonic() - t0) * 1000)

        with db.tx() as conn:
            charged = ledger.settle_hold(conn, hold_id, svc["provider_id"], cost,
                                         f"{service}/{endpoint}", call_id)
            conn.execute(
                "UPDATE calls SET status=?, cost=?, units=?, error=?, latency_ms=? WHERE id=?",
                (status, charged, units, None if error is None else str(error)[:2000], latency, call_id),
            )
        receipt = {
            "call_id": call_id, "service": service, "endpoint": endpoint, "status": status,
            "cost": money(charged), "units": units, "latency_ms": latency,
            "job": _job_brief(x_pacific_job), "provider_replayed": provider_replayed,
        }
        if status != "ok" and svc["kind"] == "platform":
            http_status = http_status if http_status >= 400 else 502
            # A provider's 5xx is the provider failing, never the exchange (internal_error is ours).
            code = "provider_failed" if http_status >= 500 else code_for(http_status)
            return http_status, {**receipt, "error": code, "detail": error}, http_status < 500
        return 200, {**receipt, "result": payload, "error": error}, True

    def _job_brief(job_id: str | None) -> dict | None:
        if not job_id:
            return None
        j = ledger.job_status(job_id)
        return {"id": job_id, "budget": to_wd(j["budget"]), "spent": to_wd(j["spent"]), "remaining": to_wd(j["remaining"])}

    # ---- tasks ------------------------------------------------------------------------------

    class TaskIn(BaseModel):
        agent: str
        title: str
        description: str
        budget: float = Field(gt=0)
        skill: str | None = None
        inputs: dict = Field(default_factory=dict)
        checks: list[dict] = Field(default_factory=list)

    @app.post("/v1/tasks")
    async def create_task(req: TaskIn, who=Depends(admin_or_account), idempotency_key: str | None = Header(default=None)):
        async def handler():
            return 200, _create_task(req, who), True

        return await idem.run("admin" if who is None else who["id"], idempotency_key,
                              idempotency.fingerprint("task", req.model_dump()), handler)

    def _create_task(req: TaskIn, who) -> dict:
        agent = ledger.resolve(req.agent)
        if agent is None or agent["kind"] != "agent":
            raise HTTPException(404, f"no agent {req.agent!r}")
        if who is not None and agent["owner_id"] != who["id"]:
            raise HTTPException(403, "you can only assign tasks to agents you own")
        task_id = new_id("task")
        with db.tx() as conn:
            job_id = ledger.open_job(conn, agent["id"], to_micro(req.budget), task_id=task_id)
            conn.execute(
                "INSERT INTO tasks(id, agent_id, title, spec, budget, job_id) VALUES (?,?,?,?,?,?)",
                (task_id, agent["id"], req.title, req.model_dump_json(), to_micro(req.budget), job_id),
            )
        return task_report(task_id)

    @app.post("/v1/tasks/{task_id}/run")
    async def run_task(task_id: str, who=Depends(admin_or_account)):
        task = db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if task is None:
            raise HTTPException(404, "no such task")
        if who is not None and ledger.account(task["agent_id"])["owner_id"] != who["id"]:
            raise HTTPException(403, "not your agent's task")
        if task["status"] != "pending":
            raise HTTPException(409, f"task is already {task['status']}")
        with db.tx() as conn:
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        spec = json.loads(task["spec"])
        try:
            path = f"/agents/{task['agent_id']}/run"
            raw = json.dumps({"task": spec, "job_id": task["job_id"]}).encode()
            resp = await http.post(config.agents_url + path, content=raw, timeout=delegation_timeout,
                                   headers=signer.headers(path, raw, ws=task["job_id"], call=task_id))
            result = resp.json() if resp.is_success else {"status": "error", "output": resp.text}
        except httpx.HTTPError as e:
            result = {"status": "error", "output": f"agent host unreachable: {type(e).__name__}: {e}"}
        with db.tx() as conn:
            conn.execute("UPDATE jobs SET status='closed' WHERE id=?", (task["job_id"],))

        job = ledger.job_status(task["job_id"])
        used = {r["service"] for r in db.all(
            "SELECT DISTINCT c.service FROM calls c JOIN jobs j ON j.id = c.job_id "
            "WHERE j.root_job_id = ? AND c.status = 'ok'", (job["root_job_id"],))}
        ctx = CheckContext(config.services_url, signer, job["root_job_id"],
                           result.get("output", ""), job, used)
        checks = await run_checks(ctx, spec.get("checks", []))
        if result.get("status") == "error":
            final = "error"
        else:
            final = "passed" if all(c["passed"] for c in checks) and result.get("status") == "completed" else "failed"
        with db.tx() as conn:
            conn.execute(
                "UPDATE tasks SET status=?, output=?, trace=?, checks=?, artifacts=?, "
                "finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                (final, result.get("output"), json.dumps(result.get("trace", [])), json.dumps(checks),
                 json.dumps(result.get("artifacts") or {}), task_id),
            )
        return task_report(task_id)

    def task_report(task_id: str) -> dict:
        task = db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if task is None:
            raise HTTPException(404, "no such task")
        job = ledger.job_status(task["job_id"])
        jobs = {j["id"]: dict(j) for j in db.all("SELECT * FROM jobs WHERE root_job_id = ?", (job["root_job_id"],))}
        names = {a["id"]: a["name"] for a in db.all("SELECT id, name FROM accounts")}
        calls = [
            {**dict(c), "caller": names.get(c["caller_id"]), "depth": _depth(jobs, c["job_id"]), "cost_wd": to_wd(c["cost"])}
            for c in db.all(
                "SELECT * FROM calls WHERE job_id IN (%s) ORDER BY started_at" % ",".join("?" * len(jobs)), tuple(jobs))
        ]
        by_service: dict[str, float] = {}
        for c in calls:
            if c["depth"] == 0:
                by_service[c["service"]] = round(by_service.get(c["service"], 0) + c["cost_wd"], 6)
        return {
            "id": task["id"], "title": task["title"], "agent_id": task["agent_id"], "agent": names[task["agent_id"]],
            "status": task["status"], "budget": money(task["budget"]), "spent": money(job["spent"]),
            "remaining": money(job["budget"] - job["spent"]), "workspace": job["root_job_id"],
            "spend_by_service": by_service, "checks": json.loads(task["checks"] or "[]"),
            "output": task["output"], "trace": json.loads(task["trace"] or "[]"), "calls": calls,
            "artifacts": json.loads(task["artifacts"] or "{}"),
            "spec": json.loads(task["spec"]), "created_at": task["created_at"], "finished_at": task["finished_at"],
        }

    def _depth(jobs: dict, job_id: str | None) -> int:
        d = 0
        while job_id and jobs.get(job_id, {}).get("parent_job_id"):
            job_id, d = jobs[job_id]["parent_job_id"], d + 1
        return d

    @app.get("/v1/tasks/{task_id}")
    def get_task(task_id: str, _=Depends(admin_or_account)):
        return task_report(task_id)

    @app.get("/v1/tasks")
    def list_tasks(_=Depends(admin), limit: int = 50):
        rows = db.all(
            "SELECT t.id, t.title, t.status, t.budget, j.spent, a.name AS agent, t.created_at FROM tasks t "
            "JOIN jobs j ON j.id = t.job_id JOIN accounts a ON a.id = t.agent_id ORDER BY t.created_at DESC LIMIT ?",
            (limit,),
        )
        return {"tasks": [{**dict(r), "budget": to_wd(r["budget"]), "spent": to_wd(r["spent"])} for r in rows]}

    # ---- admin ------------------------------------------------------------------------------

    @app.get("/v1/admin/accounts", dependencies=[Depends(admin)])
    def accounts(kind: str | None = None):
        rows = db.all(
            "SELECT a.*, o.name AS owner_name, "
            "(SELECT COALESCE(SUM(amount),0) FROM holds h WHERE h.account_id=a.id AND h.status='active') AS held "
            "FROM accounts a LEFT JOIN accounts o ON o.id = a.owner_id "
            "WHERE (? IS NULL OR a.kind = ?) ORDER BY a.kind, a.name",
            (kind, kind),
        )
        return {"accounts": [
            {"id": r["id"], "kind": r["kind"], "name": r["name"], "owner": r["owner_name"],
             "balance": to_wd(r["balance"]), "held": to_wd(r["held"]), "profile": json.loads(r["profile"])}
            for r in rows
        ]}

    @app.get("/v1/admin/transactions", dependencies=[Depends(admin)])
    def transactions(limit: int = 50, account: str | None = None):
        rows = db.all(
            "SELECT t.*, f.name AS from_name, d.name AS to_name FROM transactions t "
            "LEFT JOIN accounts f ON f.id = t.from_id JOIN accounts d ON d.id = t.to_id "
            "WHERE (? IS NULL OR t.from_id = ? OR t.to_id = ?) ORDER BY t.id DESC LIMIT ?",
            (account, account, account, limit),
        )
        return {"transactions": [{**dict(r), "amount": to_wd(r["amount"])} for r in rows]}

    @app.get("/v1/admin/audit", dependencies=[Depends(admin)])
    def audit():
        a = ledger.audit()
        return {**a, **{k: to_wd(a[k]) for k in ("total_supply", "sum_of_balances", "sum_of_mints", "active_holds")}}

    @app.get("/v1/admin/invariants", dependencies=[Depends(admin)])
    def check_invariants(quiescent: bool = False):
        report = invariants.check(db, config.data_dir, quiescent=quiescent)
        crashes = [{"invariant": "unhandled_error", "detail": f"{u['path']}: {u['error']}"} for u in unhandled]
        return {**report, "ok": report["ok"] and not crashes, "violations": report["violations"] + crashes}

    class Mint(BaseModel):
        to: str
        amount: float = Field(gt=0)
        memo: str = "admin mint"

    @app.post("/v1/admin/mint", dependencies=[Depends(admin)])
    def mint(req: Mint):
        dest = ledger.resolve(req.to)
        if dest is None:
            raise HTTPException(404, f"no account {req.to!r}")
        with db.tx() as conn:
            ledger.mint(conn, dest["id"], to_micro(req.amount), req.memo)
        return {"ok": True, "total_supply": to_wd(ledger.total_supply())}

    class AdminTransfer(Transfer):
        source: str = Field(alias="from")

    @app.post("/v1/admin/transfer", dependencies=[Depends(admin)])
    def admin_transfer(req: AdminTransfer):
        src, dest = ledger.resolve(req.source), ledger.resolve(req.to)
        if src is None or dest is None:
            raise HTTPException(404, "unknown account")
        with db.tx() as conn:
            ledger.transfer(conn, src["id"], dest["id"], to_micro(req.amount), req.memo)
        return {"ok": True}

    return app
