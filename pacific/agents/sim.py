"""The simulated brain: the scripted skill routines, driven by a persona and a seeded RNG.

`ScriptedBrain` makes the same choice every time. `SimBrain` makes the choices a market of
imperfect LLM agents would: which tier or seller to buy from, whether to hire out work it could
do itself, how much budget to hand over, when to quote first, when to guard a call with a max
cost. It also fumbles arguments, retries transient failures, abandons tasks, tips sellers and,
as the adversary, probes the exchange's defences.

Every draw comes from one RNG seeded by `inputs._seed` (random if absent), so a run can be
replayed. `inputs._persona` overrides the agent's own persona for one task. What the brain did is
returned in `artifacts["sim"]`; a probe the exchange failed to refuse is also flagged in the output.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import httpx

from .brain import MIN_USEFUL_BUDGET, ROUTINES, AgentContext, BrainResult, ScriptedBrain, Task, infer_skill
from .client import MarketError
from .personas import PERSONAS, Persona

MAX_CHAIN = 6  # never hire deeper than this, whatever the persona
TRANSIENT = {429, 500, 502, 503, 504}
PAID = ("call_service", "hire_agent")
DELIBERATE = ("call_service", "hire_agent", "list_services", "quote")  # tool calls an LLM would pause before


class GaveUp(Exception):
    """The persona abandons the task before its next paid call."""


@dataclass
class SimStats:
    persona: str
    seed: int
    decisions: list[str] = field(default_factory=list)
    probes: list[dict] = field(default_factory=list)
    paid_calls: int = 0
    quotes: int = 0
    fumbles: int = 0
    retries: int = 0
    guard_402s: int = 0
    hires: int = 0
    tips: float = 0.0
    gave_up: bool = False

    def violations(self) -> list[dict]:
        return [p for p in self.probes if not p["ok"] and not p.get("known")]

    def as_dict(self) -> dict:
        return {**asdict(self), "violations": self.violations()}


def think_scale(override=None) -> float:
    """How long personas pause before decisions: inputs._think_scale, else PACIFIC_THINK_SCALE, else 1."""
    try:
        return max(0.0, float(override if override is not None else os.environ.get("PACIFIC_THINK_SCALE", "1")))
    except ValueError:
        return 1.0


class PersonaTools:
    """Wraps a Toolbox so every routine's tool calls pick up the persona's habits."""

    def __init__(self, inner, persona: Persona, rng: random.Random, stats: SimStats, give_up_after: int | None,
                 think_scale: float = 1.0):
        self.inner, self.p, self.rng, self.stats = inner, persona, rng, stats
        self.give_up_after, self.think_scale = give_up_after, think_scale

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def call(self, name: str, **args):
        if name in DELIBERATE:
            self._think()
        if name not in PAID:
            return self._attempt(name, args)
        if self.give_up_after is not None and self.stats.paid_calls >= self.give_up_after:
            self.stats.gave_up = True
            raise GaveUp(f"gave up after {self.stats.paid_calls} paid calls")
        self.stats.paid_calls += 1
        if name == "call_service":
            if self.rng.random() < self.p.sloppy:
                self._fumble(args)
            guard = self.rng.random() < self.p.max_cost_guard
            if guard or self.rng.random() < self.p.quote_first:
                quote = self._attempt("quote", {k: args[k] for k in ("service", "endpoint", "args")})
                self.stats.quotes += 1
                if guard:
                    args = {**args, "max_cost": round(quote * self.rng.uniform(*self.p.guard_slack), 6)}
        try:
            return self._attempt(name, args)
        except MarketError as e:
            if e.status == 402 and "max_cost" in args and "exceeds your max" in str(e.detail):
                # Guarded too tightly; a real agent reads the error and tries again unguarded.
                self.stats.guard_402s += 1
                return self._attempt(name, {k: v for k, v in args.items() if k != "max_cost"})
            raise

    def _think(self):
        pause = self.rng.expovariate(1 / self.p.think_s) if self.p.think_s > 0 else 0.0
        if pause and self.think_scale:
            time.sleep(min(10.0, pause * self.think_scale))

    def _attempt(self, name: str, args: dict):
        for i in range(self.p.retries + 1):
            last = i == self.p.retries
            try:
                return self.inner.call(name, **args)
            except MarketError as e:
                if e.status not in TRANSIENT or last:
                    raise
            except httpx.TransportError as e:
                if last:
                    raise MarketError(503, f"transport error: {type(e).__name__}: {e}") from e
            self.stats.retries += 1
            time.sleep(min(2.0, 0.05 * 2 ** i) * (0.5 + self.rng.random()))

    def _fumble(self, args: dict):
        """Send a malformed version of the call first, the way a model gets an argument wrong."""
        service, endpoint, body = args["service"], args["endpoint"], dict(args["args"])
        kind = self.rng.choice(["endpoint", "service", "missing", "type"])
        if kind == "endpoint":
            endpoint += "s"
        elif kind == "service":
            service = service.replace("ocean_", "ocean-")
        elif kind == "missing" and body:
            body.pop(self.rng.choice(sorted(body)))
        elif body:
            body[self.rng.choice(sorted(body))] = {"oops": True}
        self.stats.fumbles += 1
        try:
            self.inner.call("call_service", service=service, endpoint=endpoint, args=body)
            self.stats.decisions.append(f"fumble ({kind}) on {args['service']}/{args['endpoint']} went through")
        except MarketError as e:
            self.stats.decisions.append(f"fumble ({kind}) on {args['service']}/{args['endpoint']} -> {e.status}")
        except httpx.TransportError as e:
            self.stats.decisions.append(f"fumble ({kind}) on {args['service']}/{args['endpoint']} -> {type(e).__name__}")


class SimBrain(ScriptedBrain):
    name = "sim"

    def __init__(self, persona: str | None = None):
        if persona is not None and persona not in PERSONAS:
            raise ValueError(f"unknown persona {persona!r}; available: {sorted(PERSONAS)}")
        self.default_persona = persona
        self.stats: SimStats | None = None

    def run(self, task: Task, ctx: AgentContext) -> BrainResult:
        if self.stats is not None:  # a step of a multi-step task
            return self._solve(task, ctx)
        name = task.inputs.get("_persona") or self.default_persona or "careful"
        if name not in PERSONAS:
            return BrainResult("failed", f"unknown persona {name!r}")
        seed = task.inputs.get("_seed")
        seed = int(seed) if seed is not None else random.SystemRandom().randrange(2**31)
        self.persona, self.rng = PERSONAS[name], random.Random(seed)
        self.stats = SimStats(name, seed)
        give_up = self.rng.randint(0, 2) if self.rng.random() < self.persona.abandon else None
        ctx = AgentContext(ctx.agent_id, ctx.agent_name, ctx.skills,
                           PersonaTools(ctx.tools, self.persona, self.rng, self.stats, give_up,
                                        think_scale(task.inputs.get("_think_scale"))))
        try:
            self._browse(ctx)
            if self.persona.mischief:
                Probes(ctx, self.rng, self.stats).run(self.persona.mischief)
            result = self._solve(task, ctx)
        except GaveUp as e:
            result = BrainResult("failed", f"Gave up partway through: {e}.")
        except MarketError as e:
            result = BrainResult("failed", f"Stopped: {e}")
        self._remember(task, ctx, result)
        if self.stats.violations():
            result.output += "\n\nPROBE VIOLATIONS: " + "; ".join(
                f"{v['probe']} got {v['status']} (charged {v['charged']})" for v in self.stats.violations())
        result.artifacts = {**result.artifacts, "sim": self.stats.as_dict()}
        return result

    # ---- deciding -------------------------------------------------------------------------

    def _solve(self, task: Task, ctx: AgentContext) -> BrainResult:
        steps = task.inputs.get("steps")
        if steps:
            return self._run_steps(task, steps, ctx)
        skill = task.skill or infer_skill(task.description)
        if skill is None:
            return BrainResult("failed", f"Can't tell what kind of task this is: {task.description!r}")
        depth = len(task.inputs.get("_chain", []))
        if depth < MAX_CHAIN and (
            skill not in ctx.skills or task.inputs.get("delegate")
            or (depth < self.persona.max_depth and self.rng.random() < self.persona.delegate)
        ):
            hired = self._try_delegate(task, skill, ctx)
            if hired is not None:
                return hired
        task = self._season(task, skill)
        try:
            return ROUTINES[skill](task, ctx)
        except MarketError as e:
            reason = "ran out of budget" if e.cannot_afford else "a service call failed"
            return BrainResult("failed", f"Stopped: {reason}: {e.detail}")

    def _season(self, task: Task, skill: str) -> Task:
        """Fill in what the task leaves open the way this persona would."""
        p, inputs = self.persona, dict(task.inputs)
        if skill == "research":
            if "depth" not in inputs:
                tier = p.tier if p.tier != "random" else self.rng.choice(["best", "middle", "cheapest"])
                inputs["depth"] = {"best": "auto", "middle": "standard", "cheapest": "quick"}[tier]
            if "num_results" not in inputs:
                inputs["num_results"] = self.rng.randint(*p.results)
            self.stats.decisions.append(f"research depth={inputs['depth']} n={inputs['num_results']}")
        elif skill == "code_run" and "timeout_s" not in inputs:
            inputs["timeout_s"] = self.rng.randint(*p.timeout_s)
            self.stats.decisions.append(f"sandbox timeout={inputs['timeout_s']}s")
        return replace(task, inputs=inputs)

    def _try_delegate(self, task: Task, skill: str, ctx: AgentContext) -> BrainResult | None:
        p, rng = self.persona, self.rng
        chain = list(task.inputs.get("_chain", []))
        rogue = p.mischief > 0 and rng.random() < p.mischief  # ignores the chain: hires its own buyers
        offers = [s for s in ctx.tools.call("list_services", kind="agent", skill=skill)
                  if s["provider_id"] != ctx.agent_id and (rogue or s["provider_id"] not in chain)]
        if not offers:
            self.stats.decisions.append(f"no seller for {skill}")
            return None
        price = lambda s: (s["endpoints"][0]["plan"]["per_call"], s["endpoints"][0]["plan"]["markup_pct"])
        if p.hire == "cheapest":
            offer = min(offers, key=price)
        elif p.hire == "priciest":
            offer = max(offers, key=price)
        else:
            offer = rng.choice(sorted(offers, key=lambda s: s["name"]))
        fee, markup = price(offer)
        left = min(ctx.remaining(), task.budget)
        sub_budget = round((left - fee) / (1 + markup / 100) * rng.uniform(*p.hire_share), 4)
        if sub_budget < MIN_USEFUL_BUDGET.get(skill, 1.0):
            self.stats.decisions.append(f"{offer['name']} unaffordable with {left:.2f} WD left")
            return None
        inputs = {k: v for k, v in task.inputs.items() if not k.startswith("_") and k != "delegate"}
        inputs["_seed"] = rng.randrange(2**31)
        if "_think_scale" in task.inputs:
            inputs["_think_scale"] = task.inputs["_think_scale"]
        cycle = " (cycle)" if offer["provider_id"] in chain else ""
        self.stats.decisions.append(f"hire {offer['name']}{cycle} budget={sub_budget} depth={len(chain) + 1}")
        try:
            receipt = ctx.tools.call("hire_agent", service=offer["name"], description=task.description,
                                     budget=sub_budget, inputs=inputs)
        except MarketError as e:
            self.stats.decisions.append(f"hire {offer['name']} refused: {e.status}")
            return None
        self.stats.hires += 1
        result = receipt.result or {}
        status = "completed" if receipt.raw["status"] == "ok" else "failed"
        if status == "completed" and p.tip and rng.random() < p.tip:
            amount = round(max(0.01, receipt.cost * rng.uniform(0.02, 0.1)), 4)
            try:
                ctx.tools.call("transfer", to=offer["provider_id"], amount=amount, memo=f"tip for {offer['name']}")
                self.stats.tips = round(self.stats.tips + amount, 6)
            except MarketError as e:
                self.stats.decisions.append(f"tip refused: {e.status}")
        return BrainResult(
            status,
            f"Hired {offer['provider']} ({offer['name']}) for {receipt.cost:.4f} WD.\n\n{result.get('output', receipt.error)}",
            {"hired": offer["name"], "hire_cost": receipt.cost},
        )

    def _browse(self, ctx: AgentContext):
        if self.rng.random() >= self.persona.browse:
            return
        ctx.tools.call("list_services", kind=self.rng.choice([None, "platform", "agent"]))
        ctx.tools.call("budget_status")
        if self.rng.random() < 0.5:
            ctx.tools.call("notes_read")

    def _remember(self, task: Task, ctx: AgentContext, result: BrainResult):
        if self.rng.random() < 0.5:
            try:
                ctx.tools.call("notes_write", key=f"task:{task.title[:60]}", text=f"{result.status} as {self.persona.name}")
            except (OSError, ValueError) as e:
                self.stats.decisions.append(f"notes write failed: {type(e).__name__}")


# ---- probing the exchange -----------------------------------------------------------------------


class Probes:
    """Things an adversarial agent tries. Each must be refused and must not move the job's spend.

    A refusal with a different status than expected (say 402 because the hold was checked before
    the input, or an upstream 503) is recorded as `unexpected` but is not a violation. A violation
    is a probe the exchange accepted, charged for, or crashed on. `known` marks a documented
    weakness (reported, not a violation)."""

    def __init__(self, ctx: AgentContext, rng: random.Random, stats: SimStats):
        self.ctx, self.rng, self.stats = ctx, rng, stats
        self.client = ctx.tools.client
        self.job = self.client.job_id

    def run(self, rate: float):
        agents = self.client.services(kind="agent")
        mine = [s for s in agents if s["provider_id"] == self.ctx.agent_id]
        others = [s for s in agents if s["provider_id"] != self.ctx.agent_id]
        other = self.rng.choice(others) if others else None
        hire = lambda svc, body: lambda: self._post(f"/v1/call/{svc['name']}/delegate", json=body)
        probes = [
            ("max_cost_exceeded", {402}, lambda: self._post("/v1/call/ocean_storage/list", json={},
                                                            headers={"X-Pacific-Max-Cost": "0.000001"})),
            ("unknown_endpoint", {404}, lambda: self._post("/v1/call/ocean_storage/teleport", json={})),
            ("malformed_body", {400}, lambda: self._post("/v1/call/ocean_storage/list", content=b"{not json")),
            ("storage_traversal", {400}, lambda: self._post("/v1/call/ocean_storage/get", json={"key": "../../../secrets.json"})),
            ("db_traversal", {400}, lambda: self._post("/v1/call/ocean_db/create", json={"name": "../escape"})),
            ("foreign_job", {400, 403, 404}, lambda: self._post(
                "/v1/call/ocean_storage/list", json={}, headers={"X-Pacific-Job": f"job_{self.rng.randrange(16**12):012x}"})),
            ("admin_audit", {403}, lambda: self.client.raw("GET", "/v1/admin/audit")),
            ("admin_mint", {403}, lambda: self.client.raw("POST", "/v1/admin/mint",
                                                          json={"to": self.ctx.agent_id, "amount": 1000})),
            ("negative_transfer", {422}, lambda: self.client.raw("POST", "/v1/transfer", json={"to": "treasury", "amount": -5})),
            ("overdraw_transfer", {402}, lambda: self.client.raw("POST", "/v1/transfer",
                                                                 json={"to": other["provider_id"] if other else "treasury",
                                                                       "amount": 10**9})),
        ]
        if mine:
            probes.append(("self_dealing", {400}, hire(mine[0], {"description": "x", "budget": 1})))
        if other:
            probes += [
                ("negative_budget_hire", {400}, hire(other, {"description": "x", "budget": -5})),
                ("infinite_budget_hire", {400, 402, 422}, lambda: self._post(
                    f"/v1/call/{other['name']}/delegate", content=b'{"description": "x", "budget": 1e999}',
                    headers={"Content-Type": "application/json"})),
                ("over_budget_hire", {402}, hire(other, {"description": "x", "budget": self.ctx.remaining() * 10 + 100})),
                ("assign_task_to_others", {403}, lambda: self.client.raw("POST", "/v1/tasks", json={
                    "agent": other["provider_id"], "title": "x", "description": "x", "budget": 1})),
            ]
            tiers = other["endpoints"][0].get("promises") or []
            if tiers:
                tier = tiers[0]
                probes += [
                    ("unpromisable_checks", {400}, hire(other, {"description": "x", "budget": 1, "promise": tier["tier"],
                                                                "checks": [{"type": "output_contains", "text": "done"}]})),
                    ("over_cap_hire", {400}, hire(other, {"description": "x", "budget": tier["cost_cap"] + 1,
                                                          "promise": tier["tier"]})),
                    ("rewrite_others_promise", {403}, lambda: self.client.raw(
                        "PUT", f"/v1/services/{other['name']}/promises/{tier['tier']}",
                        json={k: tier[k] for k in ("fee", "markup_pct", "cost_cap", "turnaround_s", "deliverables", "checks")}
                        | {"fee": 0})),
                ]
        if self.job:
            parent = (self.client.job() or {}).get("parent_job_id")
            if parent:
                probes += [
                    ("spend_parent_job", {403}, lambda: self._post("/v1/call/ocean_storage/list", json={},
                                                                   headers={"X-Pacific-Job": parent})),
                    ("read_parent_job", {403}, lambda: self.client.raw("GET", f"/v1/jobs/{parent}")),
                ]
            if self.ctx.remaining() > 2:
                probes.append(("sandbox_reads_host", None, self._sandbox_read))
        for name, expected, fn in probes:
            if self.rng.random() < rate:
                self._run(name, expected, fn)

    def _post(self, path: str, headers: dict | None = None, **kw) -> httpx.Response:
        h = {"X-Pacific-Job": self.job} if self.job else {}
        return self.client.raw("POST", path, headers={**h, **(headers or {})}, **kw)

    def _spent(self) -> float:
        return self.client.job()["spent"]["wd"] if self.job else 0.0

    def _run(self, name: str, expected: set[int] | None, fn):
        try:
            before = self._spent()
            r, extra = fn() if expected is None else (fn(), {})
            charged = round(self._spent() - before, 6)
        except (httpx.HTTPError, MarketError) as e:
            self.stats.probes.append({"probe": name, "expected": sorted(expected or []), "status": 0,
                                      "ok": False, "charged": 0.0, "detail": f"{type(e).__name__}: {e}"})
            return
        if expected is None:  # a known weakness: the response body says whether it held
            entry = {"probe": name, "expected": [], "status": r.status_code, "known": True, "charged": charged, **extra}
        else:
            crashed = r.status_code >= 500 and not _is_json(r)  # the exchange itself fell over
            entry = {"probe": name, "expected": sorted(expected), "status": r.status_code, "charged": charged,
                     "ok": r.status_code >= 400 and not crashed and charged == 0,
                     "unexpected": r.status_code not in expected}
            if not entry["ok"] or entry["unexpected"]:
                entry["detail"] = r.text[:300]
        self.stats.probes.append(entry)

    def _sandbox_read(self):
        """The sandbox is resource-limited, not isolated: can its code read the exchange's secrets?"""
        home = Path(self.ctx.tools.notes_path).parents[2]
        code = f"try:\n    print('LEAK' if 'admin_key' in open({str(home / 'secrets.json')!r}).read() else 'SAFE')\nexcept Exception as e:\n    print('SAFE', type(e).__name__)\n"
        r = self._post("/v1/call/ocean_sandbox/run", json={"code": code, "timeout_s": 2})
        stdout = (r.json().get("result") or {}).get("stdout", "") if r.is_success else ""
        return r, {"ok": "LEAK" not in stdout, "detail": stdout.strip()[:80]}


def _is_json(r: httpx.Response) -> bool:
    try:
        r.json()
        return True
    except ValueError:
        return False
