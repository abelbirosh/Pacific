"""Endpoint promises: what a seller commits to deliver, for what premium, judged by which checks.

An endpoint offers one or more *tiers* ("standard", "thorough"...). Each tier's promise has:

    deliverables   what the buyer gets, in words
    checks         check templates: the acceptance checks a buyer may set, and how far
    fee, markup    the premium (paid on top of the seller's actual spend)
    cost_cap       the most the seller will spend on platform services for one hire
    turnaround_s   how long the seller expects one hire to take

Promises are never edited in place. Changing one adds a new version and supersedes the old,
so a hire made against a version keeps exactly the terms it was quoted.

A check template is a check whose fields are constraints instead of values:

    {"type": "storage_contains", "key": {"input": "output_key"}, "text": {"strs": {"max_items": 3}}}

lets a buyer require that the object at the hire's `inputs.output_key` contains up to three
strings of its choosing. Constraints:

    {"input": name}                          the hire's inputs[name], exactly
    {"eq": value}                            exactly this value
    {"str": {"max_len": n}}                  any string up to n characters (default 200)
    {"strs": {"max_items": n, "max_len": m}} a string or a list of up to n strings
    {"int": {"min": a, "max": b}}            an integer in [a, b]
    {"num": {"min": a, "max": b}}            a number in [a, b]
    {"one_of": [v1, v2, ...]}                one of these values
    {"json": {"max_len": n}}                 any JSON value up to n characters encoded

Add "optional": true to a constraint to let the buyer leave that field out. A buyer's check is
valid when it matches some template of its type field by field, with no extra fields.

Only checks that read real state can be promised. `output_contains` and `output_matches` read
the seller's own report, so they can never decide whether a seller has delivered.
"""

from __future__ import annotations

import json
import math
import re

from .money import fmt
from .pricing import PricePlan

# check type -> (required fields, optional fields); only checks that inspect real state
STATE_CHECKS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "storage_exists": (("key",), ()),
    "storage_contains": (("key", "text"), ()),
    "storage_min_bytes": (("key", "min"), ()),
    "db_table_exists": (("db", "table"), ()),
    "db_columns": (("db", "table", "columns"), ()),
    "db_min_rows": (("db", "table", "min"), ()),
    "db_query_equals": (("db", "sql", "expected"), ()),
    "security_scan_clean": (("keys",), ("max_severity",)),
    "max_spend": (("amount",), ()),
    "used_service": (("service",), ()),
}
SELF_REPORTED = {"output_contains", "output_matches"}

CONSTRAINTS = ("input", "eq", "str", "strs", "int", "num", "one_of", "json")
TIER = re.compile(r"^[a-z][a-z0-9_]{1,23}$")
MAX_TIERS = 5
MAX_TEMPLATES = 12
MAX_TURNAROUND_S = 7 * 24 * 3600


class PromiseError(ValueError):
    pass


# ---- validating a seller's promise ----------------------------------------------------------------


def validate_terms(tier: str, fee: float, markup_pct: float, cost_cap: float, turnaround_s: int,
                   deliverables: list, checks: list) -> None:
    """Raise PromiseError unless these are terms an endpoint can promise."""
    if not TIER.match(tier):
        raise PromiseError("tier must be 2-24 lowercase letters, digits or _, starting with a letter")
    for name, v, lo, hi in (("fee", fee, 0, 1e6), ("markup_pct", markup_pct, 0, 500), ("cost_cap", cost_cap, 0, 1e6)):
        if not _number(v) or not lo <= v <= hi:
            raise PromiseError(f"{name} must be a number in [{lo:g}, {hi:g}]")
    if cost_cap <= 0:
        raise PromiseError("cost_cap must be positive")
    if not isinstance(turnaround_s, int) or isinstance(turnaround_s, bool) or not 1 <= turnaround_s <= MAX_TURNAROUND_S:
        raise PromiseError(f"turnaround_s must be a whole number of seconds in [1, {MAX_TURNAROUND_S}]")
    if not isinstance(deliverables, list) or not 1 <= len(deliverables) <= 10:
        raise PromiseError("deliverables must list 1-10 items")
    for d in deliverables:
        if not (isinstance(d, dict) and set(d) == {"name", "description"}
                and all(isinstance(d[k], str) and 1 <= len(d[k]) <= 300 for k in d)):
            raise PromiseError("each deliverable is {name, description}, both non-empty strings up to 300 chars")
    if not isinstance(checks, list) or not 1 <= len(checks) <= MAX_TEMPLATES:
        raise PromiseError(f"checks must list 1-{MAX_TEMPLATES} check templates")
    for t in checks:
        _validate_template(t)


def _validate_template(t) -> None:
    if not isinstance(t, dict) or "type" not in t:
        raise PromiseError("each check template is an object with a `type`")
    kind = t["type"]
    if kind in SELF_REPORTED:
        raise PromiseError(f"{kind} reads the seller's own report, so it can't be promised")
    if kind not in STATE_CHECKS:
        raise PromiseError(f"unknown check type {kind!r}; promisable: {sorted(STATE_CHECKS)}")
    required, optional = STATE_CHECKS[kind]
    fields = set(t) - {"type"}
    if missing := [f for f in required if f not in fields]:
        raise PromiseError(f"{kind} template is missing {missing}")
    if extra := sorted(fields - set(required) - set(optional)):
        raise PromiseError(f"{kind} has no fields {extra}")
    for f in fields:
        _validate_constraint(kind, f, t[f], optional_ok=True)
        if f in required and t[f].get("optional"):
            raise PromiseError(f"{kind}.{f} is required by the check, so it can't be optional")


def _validate_constraint(kind: str, field: str, c, optional_ok: bool) -> None:
    where = f"{kind}.{field}"
    if not isinstance(c, dict):
        raise PromiseError(f"{where} must be a constraint object, one of {list(CONSTRAINTS)}")
    keys = set(c) - {"optional"}
    if len(keys) != 1 or (op := next(iter(keys))) not in CONSTRAINTS:
        raise PromiseError(f"{where} must have exactly one of {list(CONSTRAINTS)}")
    if "optional" in c and not (optional_ok and isinstance(c["optional"], bool)):
        raise PromiseError(f"{where}.optional must be true or false")
    arg = c[op]
    if op == "input":
        if not (isinstance(arg, str) and re.match(r"^[A-Za-z][A-Za-z0-9_]{0,63}$", arg)):
            raise PromiseError(f"{where}.input must name an input field")
    elif op == "one_of":
        if not (isinstance(arg, list) and 1 <= len(arg) <= 50):
            raise PromiseError(f"{where}.one_of must list 1-50 values")
    elif op in ("str", "strs", "json", "int", "num"):
        allowed = {"str": {"max_len"}, "strs": {"max_items", "max_len"}, "json": {"max_len"},
                   "int": {"min", "max"}, "num": {"min", "max"}}[op]
        if not isinstance(arg, dict) or set(arg) - allowed or not all(_number(v) for v in arg.values()):
            raise PromiseError(f"{where}.{op} takes numbers for {sorted(allowed)}")
        if op in ("int", "num") and "min" in arg and "max" in arg and arg["min"] > arg["max"]:
            raise PromiseError(f"{where}.{op} has min > max")
    if len(json.dumps(c)) > 4000:
        raise PromiseError(f"{where} is too large")


# ---- validating a buyer's acceptance checks against a promise ------------------------------------


def validate_checks(templates: list[dict], checks, inputs: dict) -> list[str]:
    """Errors for each acceptance check that no template of the promise allows (empty: all valid)."""
    if not isinstance(checks, list):
        return ["checks must be a list"]
    if len(checks) > 3 * MAX_TEMPLATES:
        return [f"at most {3 * MAX_TEMPLATES} acceptance checks"]
    errors = []
    for i, check in enumerate(checks):
        if not isinstance(check, dict) or "type" not in check:
            errors.append(f"check {i}: must be an object with a `type`")
            continue
        candidates = [t for t in templates if t["type"] == check["type"]]
        if not candidates:
            errors.append(f"check {i}: this promise allows no {check['type']!r} checks")
            continue
        why = [_mismatch(t, check, inputs) for t in candidates]
        if all(why):
            errors.append(f"check {i} ({check['type']}): {why[0]}")
    return errors


def _mismatch(template: dict, check: dict, inputs: dict) -> str | None:
    fields = set(check) - {"type"}
    if extra := sorted(fields - set(template)):
        return f"fields {extra} aren't allowed"
    for f, c in template.items():
        if f == "type":
            continue
        if f not in check:
            if not c.get("optional"):
                return f"`{f}` is required"
            continue
        if why := _violates(c, check[f], inputs):
            return f"`{f}` {why}"
    return None


def _violates(c: dict, v, inputs: dict) -> str | None:
    op = next(k for k in c if k != "optional")
    arg = c[op]
    if op == "input":
        if arg not in inputs:
            return f"must be the hire's inputs.{arg}, which isn't set"
        return None if v == inputs[arg] else f"must equal inputs.{arg} ({inputs[arg]!r})"
    if op == "eq":
        return None if v == arg else f"must be {arg!r}"
    if op == "one_of":
        return None if v in arg else f"must be one of {arg}"
    if op == "str":
        return None if isinstance(v, str) and len(v) <= arg.get("max_len", 200) else \
            f"must be a string up to {arg.get('max_len', 200)} chars"
    if op == "strs":
        items = [v] if isinstance(v, str) else v
        n, m = arg.get("max_items", 5), arg.get("max_len", 200)
        ok = isinstance(items, list) and 1 <= len(items) <= n and all(isinstance(s, str) and len(s) <= m for s in items)
        return None if ok else f"must be a string or 1-{n} strings, each up to {m} chars"
    if op in ("int", "num"):
        is_type = isinstance(v, int) if op == "int" else _number(v)
        if isinstance(v, bool) or not is_type:
            return f"must be {'an integer' if op == 'int' else 'a number'}"
        lo, hi = arg.get("min", -math.inf), arg.get("max", math.inf)
        return None if lo <= v <= hi else f"must be in [{lo:g}, {hi:g}]"
    if op == "json":
        try:
            size = len(json.dumps(v))
        except (TypeError, ValueError):
            return "must be JSON"
        return None if size <= arg.get("max_len", 2000) else f"must encode to at most {arg.get('max_len', 2000)} chars"
    return f"has an unknown constraint {op!r}"


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


# ---- pricing ------------------------------------------------------------------------------------


def plan(promise: dict) -> PricePlan:
    """The price plan for a hire under this promise: the tier's fee plus spend at cost-plus."""
    return PricePlan(per_call=promise["fee"] / 1_000_000, kind="delegation", markup_pct=promise["markup_pct"])


def describe(promise: dict) -> str:
    return (f"{promise['tier']} v{promise['version']}: {fmt(promise['fee'])} fee + spend at cost "
            f"+{promise['markup_pct']:g}%, spend up to {fmt(promise['cost_cap'])}, ~{promise['turnaround_s']}s")


# ---- what each skill promises at genesis --------------------------------------------------------

_KEY = {"input": "output_key"}
_DB = {"input": "db"}

# skill -> tier -> (deliverables, check templates, cost cap WD, turnaround s)
SKILL_PROMISES: dict[str, dict[str, tuple[list[dict], list[dict], float, int]]] = {
    "research": {
        "standard": (
            [{"name": "brief", "description": "A markdown research brief with a ## Sources section, saved at inputs.output_key"}],
            [{"type": "storage_contains", "key": _KEY, "text": {"strs": {"max_items": 2, "max_len": 60}}},
             {"type": "storage_min_bytes", "key": _KEY, "min": {"int": {"min": 1, "max": 2000}}}],
            10, 300),
        "thorough": (
            [{"name": "brief", "description": "A markdown research brief with a ## Sources section, saved at inputs.output_key"},
             {"name": "sources", "description": "Sources read in full from a ranked search tier"}],
            [{"type": "storage_contains", "key": _KEY, "text": {"strs": {"max_items": 6, "max_len": 80}}},
             {"type": "storage_min_bytes", "key": _KEY, "min": {"int": {"min": 1, "max": 8000}}},
             {"type": "used_service", "service": {"one_of": ["ocean_exa", "ocean_tavily", ["ocean_exa", "ocean_tavily"]]}}],
            25, 900),
    },
    "database": {
        "standard": (
            [{"name": "database", "description": "The database inputs.db, created from inputs.schema_sql and seeded"}],
            [{"type": "db_table_exists", "db": _DB, "table": {"str": {"max_len": 64}}},
             {"type": "db_columns", "db": _DB, "table": {"str": {"max_len": 64}},
              "columns": {"strs": {"max_items": 20, "max_len": 64}}},
             {"type": "db_min_rows", "db": _DB, "table": {"str": {"max_len": 64}}, "min": {"int": {"min": 0, "max": 100}}}],
            15, 300),
        "thorough": (
            [{"name": "database", "description": "The database inputs.db, created from inputs.schema_sql and seeded"},
             {"name": "verified data", "description": "Seeded data that answers the buyer's own queries correctly"}],
            [{"type": "db_table_exists", "db": _DB, "table": {"str": {"max_len": 64}}},
             {"type": "db_columns", "db": _DB, "table": {"str": {"max_len": 64}},
              "columns": {"strs": {"max_items": 40, "max_len": 64}}},
             {"type": "db_min_rows", "db": _DB, "table": {"str": {"max_len": 64}}, "min": {"int": {"min": 0, "max": 10000}}},
             {"type": "db_query_equals", "db": _DB, "sql": {"str": {"max_len": 500}}, "expected": {"json": {"max_len": 2000}}}],
            30, 900),
    },
    "security_audit": {
        "standard": (
            [{"name": "report", "description": "A markdown audit report with a verdict, saved at inputs.output_key"}],
            [{"type": "storage_contains", "key": _KEY, "text": {"strs": {"max_items": 3, "max_len": 60}}}],
            10, 300),
        "thorough": (
            [{"name": "report", "description": "A markdown audit report with a verdict and every finding, saved at inputs.output_key"}],
            [{"type": "storage_contains", "key": _KEY, "text": {"strs": {"max_items": 10, "max_len": 80}}},
             {"type": "storage_min_bytes", "key": _KEY, "min": {"int": {"min": 1, "max": 4000}}},
             {"type": "used_service", "service": {"eq": "ocean_shield"}}],
            25, 900),
    },
    "code_run": {
        "standard": (
            [{"name": "stdout", "description": "The program's standard output, saved at inputs.output_key"}],
            [{"type": "storage_contains", "key": _KEY, "text": {"strs": {"max_items": 3, "max_len": 200}}}],
            5, 120),
        "thorough": (
            [{"name": "stdout", "description": "The program's standard output, saved at inputs.output_key"}],
            [{"type": "storage_contains", "key": _KEY, "text": {"strs": {"max_items": 10, "max_len": 500}}},
             {"type": "storage_min_bytes", "key": _KEY, "min": {"int": {"min": 1, "max": 100000}}}],
            15, 600),
    },
}

