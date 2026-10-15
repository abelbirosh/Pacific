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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..auth import Credentials, DPoPAuth, dpop_proof, sign_jwt
from .brain import MIN_USEFUL_BUDGET, ROUTINES, AgentContext, BrainResult, ScriptedBrain, Task, infer_skill
from .client import MarketError
from .personas import PERSONAS, Persona

MAX_CHAIN = 6  # never hire deeper than this, whatever the persona
TRANSIENT = {"rate_limited", "provider_failed", "request_in_progress", "internal_error"}  # worth retrying
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
    replays: int = 0           # retries the exchange answered from its record
    provider_replays: int = 0  # retries a provider answered from its record (work done once)
    key_drops: int = 0         # retries sent with a fresh key, so they could repeat the work
    token_renewals: int = 0    # session tokens replaced after the exchange called them invalid
    errors: dict[str, int] = field(default_factory=dict)  # refusal codes seen, retries included
    hires: int = 0
    tips: float = 0.0
    gave_up: bool = False

    def violations(self) -> list[dict]:
        return [p for p in self.probes if not p["ok"]]

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
        if name in (*PAID, "transfer") and not args.get("idempotency_key"):
            args = {**args, "idempotency_key": self._new_key()}
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
            if e.code == "max_cost_exceeded" and "max_cost" in args:
                # Guarded too tightly; a real agent reads the error and tries again unguarded.
                self.stats.guard_402s += 1
                return self._attempt(name, {k: v for k, v in args.items() if k != "max_cost"})
            raise

    def _new_key(self) -> str:
        """Unique for the account across runs (the job id), reproducible within one (the seed)."""
        return f"{self.inner.client.job_id or 'wallet'}-{self.rng.getrandbits(64):016x}"

    def _think(self):
        pause = self.rng.expovariate(1 / self.p.think_s) if self.p.think_s > 0 else 0.0
        if pause and self.think_scale:
            time.sleep(min(10.0, pause * self.think_scale))

    def _attempt(self, name: str, args: dict):
        for i in range(self.p.retries + 1):
            last = i == self.p.retries
            wait = min(2.0, 0.05 * 2 ** i) * (0.5 + self.rng.random())
            if i and args.get("idempotency_key") and self.rng.random() < self.p.sloppy:
                # A sloppy agent retries with a fresh key, so the exchange can't tell it's a retry.
                args = {**args, "idempotency_key": self._new_key()}
                self.stats.key_drops += 1
            try:
                out = self.inner.call(name, **args)
                if getattr(out, "replayed", False):
                    self.stats.replays += 1
                if isinstance(getattr(out, "raw", None), dict) and out.raw.get("provider_replayed"):
                    self.stats.provider_replays += 1
                return out
            except MarketError as e:
                self.stats.errors[e.code] = self.stats.errors.get(e.code, 0) + 1
                if not (e.code in TRANSIENT or e.status >= 500) or last:
                    raise
                # A sloppy agent ignores Retry-After half the time and comes straight back.
                if e.retry_after and e.code in ("rate_limited", "request_in_progress") and self.rng.random() >= self.p.sloppy:
                    wait = min(10.0, e.retry_after)
            except httpx.TransportError as e:
                if last:
                    raise MarketError(503, f"transport error: {type(e).__name__}: {e}") from e
            self.stats.retries += 1
            time.sleep(wait)

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
            self.stats.errors[e.code] = self.stats.errors.get(e.code, 0) + 1
            self.stats.decisions.append(f"fumble ({kind}) on {args['service']}/{args['endpoint']} -> {e.code}")
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
        self.stats.token_renewals = getattr(ctx.tools.client.auth, "renewals", 0)
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
        ctx.tools.call("discover")
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

    Expectations are error codes (see pacific/errors.py). A refusal with a different code than
    expected (say `budget_exceeded` because the hold was checked before the input, or an upstream
    `provider_failed`) is recorded as `unexpected` but is not a violation. A violation is a probe
    the exchange accepted, charged for, or crashed on. The sandbox escape is a paid
    call that is meant to run; its violation is the program getting out."""

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
        traversal = {"invalid_request", "budget_exceeded", "insufficient_funds"}
        probes = [
            ("max_cost_exceeded", {"max_cost_exceeded"}, lambda: self._post(
                "/v1/call/ocean_storage/list", json={}, headers={"X-Pacific-Max-Cost": "0.000001"})),
            ("unknown_endpoint", {"not_found"}, lambda: self._post("/v1/call/ocean_storage/teleport", json={})),
            ("malformed_body", {"invalid_request"}, lambda: self._post("/v1/call/ocean_storage/list", content=b"{not json")),
            ("storage_traversal", traversal, lambda: self._post("/v1/call/ocean_storage/get",
                                                                json={"key": "../../../secrets.json"})),
            ("db_traversal", traversal, lambda: self._post("/v1/call/ocean_db/create", json={"name": "../escape"})),
            ("foreign_job", {"not_found", "forbidden"}, lambda: self._post(
                "/v1/call/ocean_storage/list", json={}, headers={"X-Pacific-Job": f"job_{self.rng.randrange(16**12):012x}"})),
            ("admin_audit", {"forbidden"}, lambda: self.client.raw("GET", "/v1/admin/audit")),
            ("admin_mint", {"forbidden"}, lambda: self.client.raw("POST", "/v1/admin/mint",
                                                                  json={"to": self.ctx.agent_id, "amount": 1000})),
            ("negative_transfer", {"invalid_request"}, lambda: self.client.raw("POST", "/v1/transfer",
                                                                               json={"to": "treasury", "amount": -5})),
            ("overdraw_transfer", {"insufficient_funds", "insufficient_scope"}, lambda: self.client.raw(
                "POST", "/v1/transfer", json={"to": other["provider_id"] if other else "treasury", "amount": 10**9})),
        ]
        if self.client.discovery().get("rate_limits"):
            probes.append(("flood", None, self._flood))
        if isinstance(self.client.auth, DPoPAuth) and self.client.auth.token:
            probes += self._token_probes()
        if mine:
            probes.append(("self_dealing", {"invalid_request"}, hire(mine[0], {"description": "x", "budget": 1})))
        if other:
            probes += [
                ("negative_budget_hire", {"invalid_request"}, hire(other, {"description": "x", "budget": -5})),
                ("infinite_budget_hire", {"invalid_request", "unpriceable"}, lambda: self._post(
                    f"/v1/call/{other['name']}/delegate", content=b'{"description": "x", "budget": 1e999}',
                    headers={"Content-Type": "application/json"})),
                ("over_budget_hire", {"budget_exceeded", "insufficient_funds"}, hire(
                    other, {"description": "x", "budget": self.ctx.remaining() * 10 + 100})),
                ("assign_task_to_others", {"forbidden", "insufficient_scope"}, lambda: self.client.raw("POST", "/v1/tasks", json={
                    "agent": other["provider_id"], "title": "x", "description": "x", "budget": 1})),
            ]
        if self.job:
            parent = (self.client.job() or {}).get("parent_job_id")
            if parent:
                probes += [
                    ("spend_parent_job", {"forbidden"}, lambda: self._post("/v1/call/ocean_storage/list", json={},
                                                                           headers={"X-Pacific-Job": parent})),
                    ("read_parent_job", {"forbidden"}, lambda: self.client.raw("GET", f"/v1/jobs/{parent}")),
                ]
            if self.ctx.remaining() > 2:
                probes.append(("sandbox_escape", None, self._sandbox_escape))
            probes.append(("key_reuse", None, self._key_reuse))
        for name, expected, fn in probes:
            if self.rng.random() < rate:
                self._run(name, expected, fn)

    def _flood(self):
        """Hammer a free endpoint from 16 threads, well past the advertised burst. The exchange
        must never accept more than burst + rate x elapsed (a busy exchange may serve slower than
        the limit, in which case nothing needs refusing), and must not fall over."""
        limits = self.client.discovery()["rate_limits"]
        t0 = time.monotonic()
        with ThreadPoolExecutor(16) as pool:
            responses = list(pool.map(lambda _: self.client.raw("GET", "/v1/me"), range(int(limits["burst"] * 1.5) + 50)))
        elapsed = time.monotonic() - t0
        accepted = sum(r.status_code == 200 for r in responses)
        limited = [r for r in responses if r.status_code == 429]
        bound = limits["burst"] + limits["per_s"] * elapsed + 5
        crashed = any(r.status_code >= 500 for r in responses)
        if limited:  # back off as told, so the rest of the task isn't refused too
            time.sleep(min(10.0, float(limited[0].headers.get("Retry-After", 1))))
        r = limited[0] if limited else responses[-1]
        return r, {"ok": accepted <= bound and not crashed and all(_code(x) == "rate_limited" for x in limited),
                   "detail": f"{accepted} accepted, {len(limited)} limited in {elapsed:.2f}s (allowed {bound:.0f})"}

    def _token_probes(self) -> list:
        """Attacks on the session-token layer. The probe sends these with a bare client, crafting
        the Authorization and DPoP headers itself."""
        auth: DPoPAuth = self.client.auth
        token, key, me = auth.token, auth.credentials.key, auth.credentials.account_id
        base = self.client.base_url
        bare = httpx.Client(base_url=base, timeout=30)
        url = lambda path: base.copy_with(path=path, query=None)
        signed = lambda method, path, proof_key=key, for_path=None: {
            "Authorization": f"DPoP {token}", "DPoP": dpop_proof(proof_key, method, url(for_path or path), token)}
        token_url = url("/v1/auth/token")

        def token_request(assertion: str) -> httpx.Response:
            body = {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
            return bare.post("/v1/auth/token", json=body, headers={"DPoP": dpop_proof(key, "POST", token_url)})

        def replay():
            headers = signed("GET", "/v1/me")  # the same proof, sent twice (waiting out any 429)
            first = self._patiently(lambda: bare.get("/v1/me", headers=headers))
            second = self._patiently(lambda: bare.get("/v1/me", headers=headers))
            if 429 in (first.status_code, second.status_code):
                return second, {"ok": True, "skipped": "rate limited"}
            return second, {"ok": first.status_code == 200 and _code(second) == "invalid_dpop_proof",
                            "detail": f"first {first.status_code}, replay -> {_code(second)}"}

        def assertion_replay():
            assertion = auth.credentials.assertion(str(token_url))
            first, second = token_request(assertion), token_request(assertion)
            return second, {"ok": first.status_code == 200 and _code(second) == "invalid_grant",
                            "detail": f"first {first.status_code}, replay -> {_code(second)}"}

        now = int(time.time())
        expired = sign_jwt(key, {"iss": me, "sub": me, "aud": str(token_url), "iat": now - 600, "exp": now - 300,
                                 "jti": f"x{self.rng.getrandbits(64):x}"})
        others = [s["provider_id"] for s in self.client.services(kind="agent") if s["provider_id"] != me]
        forged = (Credentials(others[0], key).assertion(str(token_url)) if others else None)
        elsewhere = (self.client.job() or {}).get("parent_job_id") or f"job_{self.rng.getrandbits(48):012x}"
        probes = [
            ("dpop_replay", None, replay),
            ("token_without_proof", {"invalid_dpop_proof"}, lambda: bare.get("/v1/me", headers={"Authorization": f"DPoP {token}"})),
            ("proof_for_other_url", {"invalid_dpop_proof"}, lambda: bare.get(
                f"/v1/jobs/{self.job}", headers=signed("GET", f"/v1/jobs/{self.job}", for_path="/v1/me"))),
            ("stolen_token", {"invalid_dpop_proof"}, lambda: bare.get(
                "/v1/me", headers=signed("GET", "/v1/me", proof_key=Ed25519PrivateKey.generate()))),
            ("token_outside_its_job", {"forbidden", "not_found"}, lambda: bare.post(
                "/v1/call/ocean_storage/list", json={}, headers={**signed("POST", "/v1/call/ocean_storage/list"),
                                                                 "X-Pacific-Job": elsewhere})),
            ("out_of_scope", {"insufficient_scope"}, lambda: bare.post(
                "/v1/services", json={"skill": "research", "description": "cheap research", "fee": 0, "markup_pct": 0},
                headers=signed("POST", "/v1/services"))),
            ("expired_assertion", {"invalid_grant"}, lambda: token_request(expired)),
            ("assertion_replay", None, assertion_replay),
        ]
        if forged:
            probes.append(("forged_assertion", {"invalid_client"}, lambda: token_request(forged)))
        return probes

    def _key_reuse(self):
        """Pay for one cheap call under an Idempotency-Key, then reuse the key for a different call
        (must be refused) and for the same call (must be replayed). Only the first may be charged."""
        key = {"Idempotency-Key": f"probe-{self.client.job_id}-{self.rng.getrandbits(48):012x}"}
        before = self._spent()
        post = lambda body: self._patiently(lambda: self._post("/v1/call/ocean_storage/list", json=body, headers=key))
        first = post({})
        if first.status_code != 200:  # nothing final was stored (a fault): no verdict
            return first, {"ok": True, "skipped": f"first call got {first.status_code}"}
        other, again = post({"prefix": "x"}), post({})
        if 429 in (other.status_code, again.status_code):
            return other, {"ok": True, "skipped": "rate limited"}
        charged = round(self._spent() - before, 6)
        paid = first.json()["cost"]["wd"]
        ok = (_code(other) == "idempotency_key_conflict" and again.headers.get("Idempotent-Replayed") == "true"
              and again.json() == first.json() and abs(charged - paid) < 1e-9)
        return other, {"ok": ok, "detail": f"reuse -> {_code(other)}, retry replayed="
                                           f"{again.headers.get('Idempotent-Replayed')}, charged {charged} for {paid}"}

    @staticmethod
    def _patiently(send, tries: int = 4) -> httpx.Response:
        """Send, waiting out Retry-After on a 429 (our own floods drain the bucket)."""
        for _ in range(tries):
            r = send()
            if r.status_code != 429:
                return r
            time.sleep(min(10.0, float(r.headers.get("Retry-After", 1))))
        return r

    def _post(self, path: str, headers: dict | None = None, **kw) -> httpx.Response:
        h = {"X-Pacific-Job": self.job} if self.job else {}
        return self.client.raw("POST", path, headers={**h, **(headers or {})}, **kw)

    def _spent(self) -> float:
        if not self.job:
            return 0.0
        for _ in range(3):
            try:
                return self.client.job()["spent"]["wd"]
            except MarketError as e:
                if e.code != "rate_limited":
                    raise
                time.sleep(min(10.0, e.retry_after or 1))
        return self.client.job()["spent"]["wd"]

    def _run(self, name: str, expected: set[int] | None, fn):
        try:
            before = self._spent()
            r, extra = fn() if expected is None else (fn(), {})
            charged = round(self._spent() - before, 6)
        except MarketError as e:
            if e.code == "rate_limited":  # our own bookkeeping was throttled: no verdict either way
                self.stats.probes.append({"probe": name, "skipped": "rate_limited", "ok": True, "status": 429})
                return
            self.stats.probes.append({"probe": name, "expected": sorted(expected or []), "status": 0,
                                      "ok": False, "charged": 0.0, "detail": f"{type(e).__name__}: {e}"})
            return
        except httpx.HTTPError as e:
            self.stats.probes.append({"probe": name, "expected": sorted(expected or []), "status": 0,
                                      "ok": False, "charged": 0.0, "detail": f"{type(e).__name__}: {e}"})
            return
        if expected is None:  # a probe that judges its own outcome (sandbox escape, flood)
            entry = {"probe": name, "expected": [], "status": r.status_code, "charged": charged, **extra}
        else:
            code = _code(r)
            crashed = code == "internal_error" or (r.status_code >= 500 and code is None)  # the exchange fell over
            entry = {"probe": name, "expected": sorted(expected), "status": r.status_code, "code": code,
                     "charged": charged, "ok": r.status_code >= 400 and not crashed and charged == 0,
                     "unexpected": code not in expected}
            if not entry["ok"] or entry["unexpected"]:
                entry["detail"] = r.text[:300]
        self.stats.probes.append(entry)

    def _sandbox_escape(self):
        """Pay for a sandbox run whose code tries to get out: read the exchange's secrets and
        keys, list the host, reach the exchange over the network, start another program."""
        home = Path(self.ctx.tools.notes_path).parents[2]
        url = self.client.base_url
        code = (
            "import os, socket, subprocess\n"
            f"home = {str(home)!r}\n"
            f"for name in ('secrets.json', 'exchange_key.pem', 'keys/{self.ctx.agent_id}.pem'):\n"
            "    try:\n"
            "        open(os.path.join(home, name)).read(16); print('LEAK read', name)\n"
            "    except Exception:\n"
            "        pass\n"
            "try:\n"
            "    os.listdir(os.path.join(home, 'keys')); print('LEAK list keys')\n"
            "except Exception:\n"
            "    pass\n"
            "try:\n"
            "    os.listdir(home); print('LEAK list home')\n"
            "except Exception:\n"
            "    pass\n"
            "try:\n"
            f"    socket.create_connection(({url.host!r}, {url.port}), timeout=2); print('LEAK network')\n"
            "except Exception:\n"
            "    pass\n"
            "try:\n"
            "    subprocess.run(['/bin/echo'], capture_output=True); print('LEAK exec')\n"
            "except Exception:\n"
            "    pass\n"
            "print('done')\n"
        )
        r = self._post("/v1/call/ocean_sandbox/run", json={"code": code, "timeout_s": 3})
        stdout = (r.json().get("result") or {}).get("stdout", "") if r.is_success else ""
        leaks = [line for line in stdout.splitlines() if line.startswith("LEAK")]
        return r, {"ok": not leaks, "detail": "; ".join(leaks) or stdout.strip()[:80]}


def _code(r: httpx.Response) -> str | None:
    try:
        data = r.json()
    except ValueError:
        return None
    return data.get("error") if isinstance(data, dict) else None
