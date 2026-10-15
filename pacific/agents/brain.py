"""Agent brains. A brain gets a task and a toolbox and decides what to buy.

`Brain` is the plug point: implement `run(task, ctx)` and add it to BRAINS. An LLM brain would
hand `ctx.tools.specs()` to the model as its tools, loop on tool calls via `ctx.tools.call`,
and use `ctx.remaining()` / the task budget to decide when to stop.

`ScriptedBrain` is the stub: deterministic skill routines that make budget-aware choices
(which search tier it can afford, whether to hire another agent) so the economy can be
exercised end to end without a model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .client import MarketError
from .tools import Toolbox

SKILLS = ("research", "database", "security_audit", "code_run")

SKILL_DESCRIPTIONS = {
    "research": "Research a topic on the web and deliver a sourced markdown brief",
    "database": "Design, create and seed a SQL database from a schema",
    "security_audit": "Audit source code and dependencies for vulnerabilities and leaked secrets",
    "code_run": "Run a Python program and report its output",
}

_KEYWORDS = {
    "security_audit": ("security", "vulnerab", "audit", "scan", "cve", "secret"),
    "database": ("database", "table", "schema", "sql", "seed"),
    "code_run": ("run ", "execute", "script", "program", "compute"),
    "research": ("research", "search", "find", "look up", "compare", "brief"),
}


@dataclass
class Task:
    title: str
    description: str
    budget: float
    skill: str | None = None
    inputs: dict = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        return cls(
            title=d.get("title") or d.get("description", "")[:60],
            description=d.get("description", ""),
            budget=float(d.get("budget", 0)),
            skill=d.get("skill"),
            inputs=d.get("inputs") or {},
            checks=d.get("checks") or [],
        )


@dataclass
class BrainResult:
    status: str  # completed | failed
    output: str
    artifacts: dict = field(default_factory=dict)


@dataclass
class AgentContext:
    agent_id: str
    agent_name: str
    skills: list[str]
    tools: Toolbox

    def remaining(self) -> float:
        return self.tools.call("budget_status")["remaining"]["wd"] if self.tools.client.job_id else float("inf")


class Brain(Protocol):
    name: str

    def run(self, task: Task, ctx: AgentContext) -> BrainResult: ...


# ---- the scripted stub --------------------------------------------------------------------------


class ScriptedBrain:
    name = "scripted"

    def run(self, task: Task, ctx: AgentContext) -> BrainResult:
        steps = task.inputs.get("steps")
        if steps:
            return self._run_steps(task, steps, ctx)
        skill = task.skill or infer_skill(task.description)
        if skill is None:
            return BrainResult("failed", f"Can't tell what kind of task this is: {task.description!r}")
        if skill not in ctx.skills or task.inputs.get("delegate"):
            hired = self._try_delegate(task, skill, ctx)
            if hired is not None:
                return hired
        try:
            return ROUTINES[skill](task, ctx)
        except MarketError as e:
            reason = "ran out of budget" if e.cannot_afford else "a service call failed"
            return BrainResult("failed", f"Stopped: {reason}: {e.detail}")

    def _run_steps(self, task: Task, steps: list[dict], ctx: AgentContext) -> BrainResult:
        outputs, ok = [], True
        for i, step in enumerate(steps, 1):
            left = ctx.remaining()
            share = left / (len(steps) - i + 1)
            sub = Task(
                title=f"{task.title} / step {i}",
                description=step.get("description", ""),
                budget=min(left, step.get("budget", share)),
                skill=step.get("skill"),
                inputs={**step.get("inputs", {}), "_chain": task.inputs.get("_chain", [])},
            )
            res = self.run(sub, ctx)
            outputs.append(f"## Step {i}: {sub.description}\n{res.output}")
            if res.status != "completed":
                ok = False
                break
        return BrainResult("completed" if ok else "failed", "\n\n".join(outputs))

    def _try_delegate(self, task: Task, skill: str, ctx: AgentContext) -> BrainResult | None:
        """Hire the cheapest agent selling this skill, if one is affordable and not already in the chain."""
        chain = set(task.inputs.get("_chain", [])) | {ctx.agent_id}
        offers = []
        for s in ctx.tools.call("list_services", kind="agent", skill=skill):
            if s["provider_id"] in chain:
                continue
            plan = s["endpoints"][0]["plan"]
            offers.append((plan["per_call"], plan["markup_pct"], s))
        if not offers:
            return None
        fee, markup, offer = min(offers, key=lambda o: (o[0], o[1]))
        left = min(ctx.remaining(), task.budget)
        sub_budget = round((left - fee) / (1 + markup / 100) * 0.95, 4)  # keep a little slack
        if sub_budget < MIN_USEFUL_BUDGET.get(skill, 1.0):
            return None
        inputs = {k: v for k, v in task.inputs.items() if k not in ("delegate", "_chain")}
        try:
            receipt = ctx.tools.call("hire_agent", service=offer["name"], description=task.description,
                                     budget=sub_budget, inputs=inputs)
        except MarketError:
            return None
        result = receipt.result or {}
        status = "completed" if receipt.raw["status"] == "ok" else "failed"
        return BrainResult(
            status,
            f"Hired {offer['provider']} ({offer['name']}) for {receipt.cost:.4f} WD.\n\n{result.get('output', receipt.error)}",
        )


def infer_skill(description: str) -> str | None:
    text = description.lower()
    for skill, words in _KEYWORDS.items():
        if any(w in text for w in words):
            return skill
    return None


MIN_USEFUL_BUDGET = {"research": 1.0, "database": 6.0, "security_audit": 3.0, "code_run": 1.0}


# ---- skill routines -----------------------------------------------------------------------------


def fit_to_budget(ctx: AgentContext, service: str, endpoint: str, args: dict, param: str, budget: float) -> dict | None:
    """Shrink a linear size parameter (timeout, result count...) until the worst case fits the budget."""
    if ctx.tools.call("quote", service=service, endpoint=endpoint, args=args) <= budget:
        return args
    base = ctx.tools.call("quote", service=service, endpoint=endpoint, args={**args, param: 0})
    per = ctx.tools.call("quote", service=service, endpoint=endpoint, args={**args, param: 1}) - base
    if base > budget or per <= 0:
        return None
    fitted = int((budget - base) / per)
    return {**args, param: fitted} if fitted >= 1 else None


def _store(ctx: AgentContext, key: str | None, content: str) -> str | None:
    if not key:
        return None
    ctx.tools.call("call_service", service="ocean_storage", endpoint="put", args={"key": key, "content": content})
    return key


def research(task: Task, ctx: AgentContext) -> BrainResult:
    query = task.inputs.get("query") or task.description
    depth = task.inputs.get("depth", "auto")
    n = int(task.inputs.get("num_results", 5))
    reserve = 0.2 if task.inputs.get("output_key") else 0.0  # keep enough to store the report
    left = min(ctx.remaining(), task.budget) - reserve

    # Pick the best tier the budget allows: quote each and take the first that fits.
    tiers = [
        ("ocean_exa", "search", {"query": query, "num_results": n, "contents": True}),
        ("ocean_tavily", "search", {"query": query, "max_results": min(n, 5)}),
        ("ocean_brave", "search", {"query": query, "num_results": n}),
    ]
    if depth == "quick":
        tiers = tiers[2:]
    elif depth == "standard":
        tiers = tiers[1:]
    for service, endpoint, args in tiers:
        if ctx.tools.call("quote", service=service, endpoint=endpoint, args=args) <= left:
            break
    else:
        return BrainResult("failed", f"Can't afford any search tier with {left:.2f} WD left.")

    receipt = ctx.tools.call("call_service", service=service, endpoint=endpoint, args=args)
    result = receipt.result
    lines = [f"# Research: {query}", "", f"_Searched with {service} for {receipt.cost:.2f} WD._", ""]
    if result.get("answer"):
        lines += ["## Summary", result["answer"], ""]
    lines.append("## Sources")
    for i, r in enumerate(result["results"], 1):
        lines.append(f"{i}. [{r['title']}]({r['url']}) ({r['source']})")
        body = r.get("highlights") or ([r["content"]] if r.get("content") else [r.get("snippet", "")])
        for passage in body[:2]:
            if passage:
                lines.append(f"   > {passage[:300]}")
    report = "\n".join(lines)
    stored = _store(ctx, task.inputs.get("output_key"), report)
    ctx.tools.call("notes_write", key=f"research:{query[:60]}", text=f"{len(result['results'])} sources via {service}")
    tail = f"\n\nSaved to storage key `{stored}`." if stored else ""
    return BrainResult("completed", report + tail, {"report_key": stored, "tier": service})


def database(task: Task, ctx: AgentContext) -> BrainResult:
    db = task.inputs.get("db", "app")
    schema_sql = task.inputs.get("schema_sql")
    if not schema_sql:
        return BrainResult("failed", "database tasks need inputs.schema_sql")
    call = lambda ep, args: ctx.tools.call("call_service", service="ocean_db", endpoint=ep, args=args)
    try:
        call("create", {"name": db})
    except MarketError as e:
        if e.status != 409:  # already exists is fine
            raise
    call("execute", {"db": db, "sql": schema_sql})
    seeded = []
    for table, rows in (task.inputs.get("seed") or {}).items():
        if not rows:
            continue
        cols = list(rows[0])
        sql = f'INSERT INTO "{table}" ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})'
        call("execute_many", {"db": db, "sql": sql, "rows": [[r.get(c) for c in cols] for r in rows]})
        seeded.append(f"{table}: {len(rows)} rows")
    schema = call("schema", {"db": db}).result
    summary = "\n".join(
        f"- {t}: {info['rows']} rows, columns {', '.join(c['name'] for c in info['columns'])}"
        for t, info in schema["tables"].items()
    )
    return BrainResult("completed", f"Database `{db}` ready.\n{summary}", {"db": db, "schema": schema})


def security_audit(task: Task, ctx: AgentContext) -> BrainResult:
    files = dict(task.inputs.get("files") or {})
    for key in task.inputs.get("storage_keys", []):
        files[key] = ctx.tools.call("call_service", service="ocean_storage", endpoint="get", args={"key": key}).result["content"]
    lines, worst_ok = [f"# Security audit: {task.title}", ""], True
    if files:
        scan = ctx.tools.call("call_service", service="ocean_shield", endpoint="scan_code", args={"files": files}).result
        worst_ok &= scan["passed"]
        lines += [f"## Code scan ({scan['files']} files, {scan['lines']} lines)", f"Counts: {scan['counts']}", ""]
        lines += [f"- **{f['severity']}** `{f['file']}:{f['line']}` {f['rule']}: {f['message']}" for f in scan["findings"]]
        lines.append("")
    if task.inputs.get("requirements"):
        deps = ctx.tools.call("call_service", service="ocean_shield", endpoint="audit_deps",
                              args={"requirements": task.inputs["requirements"]}).result
        worst_ok &= deps["passed"]
        lines += [f"## Dependencies ({deps['packages_checked']} checked)", f"Counts: {deps['counts']}", ""]
        for f in deps["findings"]:
            fixed = f" (fixed in {', '.join(f['fixed_in'])})" if f.get("fixed_in") else ""
            lines.append(f"- **{f['severity']}** {f['package']} {f.get('id', f['rule'])}: {f['message']}{fixed}")
    if len(lines) == 2:
        return BrainResult("failed", "security audits need inputs.files, inputs.storage_keys or inputs.requirements")
    verdict = "PASS: no high-severity issues" if worst_ok else "FAIL: high-severity issues found"
    lines.insert(1, f"**Verdict: {verdict}**")
    report = "\n".join(lines)
    stored = _store(ctx, task.inputs.get("output_key"), report)
    return BrainResult("completed", report, {"report_key": stored, "passed": worst_ok})


def code_run(task: Task, ctx: AgentContext) -> BrainResult:
    code = task.inputs.get("code")
    if not code:
        return BrainResult("failed", "code_run tasks need inputs.code")
    args = {"code": code, "timeout_s": task.inputs.get("timeout_s", 10), "stdin": task.inputs.get("stdin", "")}
    reserve = 0.1 if task.inputs.get("output_key") else 0.0
    args = fit_to_budget(ctx, "ocean_sandbox", "run", args, "timeout_s", min(ctx.remaining(), task.budget) - reserve)
    if args is None:
        return BrainResult("failed", "Not enough budget for even a 1-second sandbox run.")
    run = ctx.tools.call("call_service", service="ocean_sandbox", endpoint="run", args=args).result
    _store(ctx, task.inputs.get("output_key"), run["stdout"])
    status = "completed" if run["exit_code"] == 0 and not run["timed_out"] else "failed"
    out = f"exit {run['exit_code']} in {run['duration_s']}s\n--- stdout ---\n{run['stdout']}"
    if run["stderr"]:
        out += f"\n--- stderr ---\n{run['stderr']}"
    return BrainResult(status, out, {"run": run})


ROUTINES: dict[str, Callable[[Task, AgentContext], BrainResult]] = {
    "research": research,
    "database": database,
    "security_audit": security_audit,
    "code_run": code_run,
}

BRAINS: dict[str, Callable[..., Brain]] = {"scripted": ScriptedBrain}


def make_brain(name: str) -> Brain:
    """`name` is a brain, optionally with an argument: "scripted", "sim", "sim:frugal"."""
    base, _, arg = name.partition(":")
    if base not in BRAINS:
        raise ValueError(f"unknown brain {name!r}; available: {sorted(BRAINS)}")
    return BRAINS[base](arg) if arg else BRAINS[base]()


def describe_result(result: BrainResult) -> dict:
    return {"status": result.status, "output": result.output, "artifacts": json.loads(json.dumps(result.artifacts, default=str))}


from .sim import SimBrain  # noqa: E402  (sim builds on the routines above)

BRAINS["sim"] = SimBrain
