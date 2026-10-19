"""Random tasks whose correct result is known in advance, so the exchange's checks can grade them.

Each generator returns a task spec (title, description, skill, budget, inputs, checks) plus an
estimate of what a competent agent would spend. Budgets are then drawn relative to that estimate:
mostly comfortable, sometimes generous, and sometimes too tight to finish, the way real owners
budget. Everything is drawn from one seeded RNG, so a workload can be regenerated exactly.

With `impossible_rate`, some database and code_run tasks get an acceptance check no honest work
can meet: more rows than the seed provides (a seller can see that and decline), or a total that
doesn't match the data, or program output that never appears (a seller can't see those coming,
so its work fails the checks and the premium is refunded). They're marked impossible and left out
of the pass rate.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

TABLES = ["customers", "orders", "invoices", "events", "tickets", "products", "shipments", "sessions"]
COLUMNS = [
    ("name", "TEXT", lambda r, i: f"name-{i}-{r.randint(100, 999)}"),
    ("email", "TEXT", lambda r, i: f"user{i}@example.com"),
    ("qty", "INTEGER", lambda r, i: r.randint(1, 20)),
    ("status", "TEXT", lambda r, i: r.choice(["new", "paid", "shipped", "void"])),
    ("created_at", "TEXT", lambda r, i: f"2026-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}"),
]

TOPICS = ["rate limiting", "password hashing", "connection pooling", "database migrations", "feature flags",
          "retry with backoff", "structured logging", "cache invalidation", "websocket scaling", "idempotency keys",
          "blue green deploys", "sqlite write ahead log", "jwt rotation", "circuit breakers", "pagination cursors"]
STACKS = ["fastapi", "postgres", "python", "redis", "kubernetes", "nginx", "node", "sqlite", "django", "go"]

# Planted issues: (file content, rules the report must mention, makes the verdict FAIL)
VULNS = [
    ("import subprocess\n\ndef convert(name):\n    return subprocess.check_output('convert ' + name, shell=True)\n", ["B602"], True),
    ("import hashlib\n\ndef digest(pw):\n    return hashlib.md5(pw.encode()).hexdigest()\n", ["B324"], True),
    ('AWS_KEY = "AKIA{key}"\n\ndef client():\n    return AWS_KEY\n', ["aws-access-key"], True),
    ("import pickle\n\ndef load(blob):\n    return pickle.loads(blob)\n", ["B301"], False),
    ("def calc(expr):\n    return eval(expr)\n", ["B307"], False),
    ("import yaml\n\ndef read(text):\n    return yaml.load(text)\n", ["B506"], False),
]
CLEAN = [
    "def add(a, b):\n    return a + b\n",
    "from dataclasses import dataclass\n\n@dataclass\nclass Point:\n    x: int\n    y: int\n",
    "def chunks(xs, n):\n    return [xs[i:i + n] for i in range(0, len(xs), n)]\n",
]

BUDGET_CLASSES = (("tight", 0.15, (0.5, 1.0)), ("normal", 0.6, (1.4, 2.5)), ("generous", 0.25, (3.0, 6.0)))


@dataclass
class Generated:
    spec: dict
    estimate: float
    kind: str
    budget_class: str = "normal"
    meta: dict = field(default_factory=dict)


class Workload:
    def __init__(self, seed: int | None = None, mix: dict[str, float] | None = None, research: bool = True,
                 delegate_rate: float = 0.1, pipeline_rate: float = 0.08, impossible_rate: float = 0.0):
        self.rng = random.Random(seed)
        self.mix = mix or {"database": 0.25, "code_run": 0.3, "security_audit": 0.2, "research": 0.25}
        if not research:
            self.mix = {k: v for k, v in self.mix.items() if k != "research"}
        self.delegate_rate, self.pipeline_rate, self.impossible_rate = delegate_rate, pipeline_rate, impossible_rate
        self.n = 0

    def next(self) -> Generated:
        self.n += 1
        if self.rng.random() < self.pipeline_rate:
            g = self._pipeline()
        else:
            g = self._one(self._pick_kind())
            # draw only when asked for, so workloads without impossible tasks stay exactly as they were
            if self.impossible_rate and g.kind in ("database", "code_run") and self.rng.random() < self.impossible_rate:
                self._make_impossible(g)
        cls, _, (lo, hi) = self._pick(BUDGET_CLASSES)
        g.budget_class = cls
        g.spec["budget"] = round(max(0.3, g.estimate * self.rng.uniform(lo, hi)), 2)
        if g.kind != "pipeline" and self.rng.random() < self.delegate_rate:
            g.spec["inputs"]["delegate"] = True
            g.spec["budget"] = round(g.spec["budget"] * 1.6 + 2, 2)  # room for a seller's fee and markup
        g.spec["title"] = f"#{self.n} {g.spec['title']}"
        return g

    def _pick(self, options):
        x, acc = self.rng.random() * sum(o[1] for o in options), 0.0
        for o in options:
            acc += o[1]
            if x <= acc:
                return o
        return options[-1]

    def _pick_kind(self) -> str:
        return self._pick([(k, w) for k, w in self.mix.items()])[0]

    def _one(self, kind: str) -> Generated:
        return getattr(self, f"_{kind}")()

    # ---- generators -------------------------------------------------------------------------

    def _database(self) -> Generated:
        r = self.rng
        table, db = r.choice(TABLES), f"db{r.randint(1, 999)}"
        cols = [("id", "INTEGER PRIMARY KEY", None)] + r.sample(COLUMNS, r.randint(1, 3)) + [
            ("amount_cents", "INTEGER NOT NULL", lambda r, i: r.randint(100, 50_000))]
        n = r.choice([r.randint(3, 40), r.randint(40, 400)])
        rows = [{c: gen(r, i) for c, _, gen in cols if gen} for i in range(n)]
        ddl = f"CREATE TABLE {table} (" + ", ".join(f"{c} {t}" for c, t, _ in cols) + ");"
        total = sum(row["amount_cents"] for row in rows)
        return Generated({
            "title": f"{table} database", "skill": "database",
            "description": f"Create the {table} database and seed it with {n} rows.",
            "inputs": {"db": db, "schema_sql": ddl, "seed": {table: rows}},
            "checks": [
                {"type": "db_columns", "db": db, "table": table, "columns": [c for c, _, _ in cols]},
                {"type": "db_min_rows", "db": db, "table": table, "min": n},
                {"type": "db_query_equals", "db": db, "sql": f"SELECT SUM(amount_cents) FROM {table}", "expected": total},
                {"type": "used_service", "service": "ocean_db"},
            ],
        }, estimate=5.5 + 0.0005 * n, kind="database", meta={"rows": n})

    def _code_run(self) -> Generated:
        r = self.rng
        kind = r.choices(["sum", "fib", "primes", "words", "slow", "crash"], [3, 3, 2, 2, 1, 0.5])[0]
        if kind == "sum":
            n = r.randint(10, 100_000)
            code, out = f"print('sum of squares', sum(i * i for i in range({n})))\n", f"sum of squares {sum(i * i for i in range(n))}"
        elif kind == "fib":
            k = r.randint(10, 300)
            a, b = 0, 1
            for _ in range(k):
                a, b = b, a + b
            code = f"a, b = 0, 1\nfor _ in range({k}):\n    a, b = b, a + b\nprint('fib', a)\n"
            out = f"fib {a}"
        elif kind == "primes":
            n = r.randint(1_000, 200_000)
            sieve = bytearray([1]) * n
            sieve[0:2] = b"\x00\x00"
            for i in range(2, int(n ** 0.5) + 1):
                if sieve[i]:
                    sieve[i * i::i] = bytearray(len(sieve[i * i::i]))
            code = (f"n = {n}\nsieve = bytearray([1]) * n\nsieve[0:2] = b'\\x00\\x00'\n"
                    "for i in range(2, int(n ** 0.5) + 1):\n    if sieve[i]:\n"
                    "        sieve[i*i::i] = bytearray(len(sieve[i*i::i]))\nprint('primes', sum(sieve))\n")
            out = f"primes {sum(sieve)}"
        elif kind == "words":
            words = [r.choice(["wave", "tide", "reef", "kelp", "gull", "surf"]) for _ in range(r.randint(20, 200))]
            top = max(sorted(set(words)), key=words.count)
            code = (f"from collections import Counter\nwords = {' '.join(words)!r}.split()\n"
                    "c = Counter(words)\nbest = max(sorted(c), key=c.__getitem__)\nprint('top', best, c[best])\n")
            out = f"top {top} {words.count(top)}"
        elif kind == "slow":
            s = round(r.uniform(0.5, 4), 1)
            code, out = f"import time\ntime.sleep({s})\nprint('slept', {s})\n", f"slept {s}"
        else:
            code, out = "raise SystemExit('the program failed on purpose')\n", "never printed"
        key = f"runs/{kind}-{r.randint(1, 9999)}.txt"
        return Generated({
            "title": f"run {kind}", "skill": "code_run",
            "description": f"Run a Python program ({kind}) and save its output.",
            "inputs": {"code": code, "output_key": key},
            "checks": [{"type": "storage_contains", "key": key, "text": out},
                       {"type": "used_service", "service": "ocean_sandbox"}],
        }, estimate=1.0 + (2.0 if kind == "slow" else 0), kind="code_run",
            meta={"program": kind, "impossible": kind == "crash"})

    def _security_audit(self) -> Generated:
        r = self.rng
        planted = r.sample(VULNS, r.choice([0, 1, 1, 2, 3]))
        files, rules, fail = {}, [], False
        for i, (content, ids, high) in enumerate(planted):
            key = "".join(r.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567") for _ in range(16))
            files[f"app/mod{i}.py"] = content.replace("{key}", key)
            rules += ids
            fail |= high
        for i, content in enumerate(r.sample(CLEAN, r.randint(1, len(CLEAN)))):
            files[f"app/util{i}.py"] = content
        key = f"security/audit-{r.randint(1, 9999)}.md"
        verdict = "FAIL: high-severity issues found" if fail else "PASS: no high-severity issues"
        return Generated({
            "title": f"audit {len(files)} files", "skill": "security_audit",
            "description": "Security audit this code for vulnerabilities and leaked secrets.",
            "inputs": {"files": files, "output_key": key},
            "checks": [{"type": "storage_contains", "key": key, "text": ["Verdict", *rules]},
                       {"type": "output_contains", "text": verdict},
                       {"type": "used_service", "service": "ocean_shield"}],
        }, estimate=2.5, kind="security_audit", meta={"planted": rules})

    def _research(self) -> Generated:
        r = self.rng
        query = f"{r.choice(STACKS)} {r.choice(TOPICS)}"
        key = f"research/{query.replace(' ', '-')}-{r.randint(1, 999)}.md"
        return Generated({
            "title": f"research {query}", "skill": "research",
            "description": f"Research {query} and write a sourced brief.",
            "inputs": {"query": query, "output_key": key},
            "checks": [{"type": "storage_contains", "key": key, "text": ["# Research", "## Sources"]},
                       {"type": "used_service", "service": ["ocean_exa", "ocean_tavily", "ocean_brave"]}],
        }, estimate=r.choice([1.0, 4.0, 7.5]), kind="research")

    def _make_impossible(self, g: Generated) -> None:
        r, checks = self.rng, g.spec["checks"]
        if g.kind == "database":
            how = r.choice(["more_rows", "wrong_total"])
            for c in checks:
                if how == "more_rows" and c["type"] == "db_min_rows":
                    c["min"] += r.randint(1, 20)
                elif how == "wrong_total" and c["type"] == "db_query_equals":
                    c["expected"] += r.randint(1, 999)
        else:
            how = "wrong_output"
            for c in checks:
                if c["type"] == "storage_contains":
                    c["text"] = f"{c['text']} (and then some)"
        g.meta.update(impossible=True, impossible_how=how)

    def _pipeline(self) -> Generated:
        kinds = [k for k in self.mix if k in ("database", "code_run", "security_audit", "research")]
        parts = [self._one(self.rng.choice(kinds)) for _ in range(self.rng.randint(2, 3))]
        steps = [{"skill": p.spec["skill"], "description": p.spec["description"], "inputs": p.spec["inputs"],
                  "budget": round(p.estimate * 1.5, 2)} for p in parts]
        return Generated({
            "title": "pipeline: " + " + ".join(p.kind for p in parts), "skill": None,
            "description": "Do these steps in order: " + "; ".join(p.spec["description"] for p in parts),
            "inputs": {"steps": steps},
            "checks": [c for p in parts for c in p.spec["checks"] if c["type"] != "output_contains"],
        }, estimate=sum(p.estimate for p in parts), kind="pipeline", meta={"steps": [p.kind for p in parts]})
