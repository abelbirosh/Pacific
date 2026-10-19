"""Service registry: who sells what, where it lives, and at what price."""

from __future__ import annotations

import json
import sqlite3

from . import promises
from .db import Database
from .ids import new_id
from .money import to_micro
from .pricing import PricePlan

# Skills an agent can sell to other agents as task delegation. The request schema is the same
# for all of them: describe the subtask and set the budget the hired agent may spend on it.
DELEGATION_SCHEMA = {
    "type": "object",
    "required": ["description", "budget"],
    "properties": {
        "description": {"type": "string", "description": "What the hired agent should do."},
        "budget": {"type": "number", "description": "Max wave dollars the hired agent may spend on platform services."},
        "inputs": {"type": "object", "description": "Structured inputs for the subtask."},
    },
}


def register_service(conn: sqlite3.Connection, name: str, kind: str, provider_id: str, description: str,
                     base_url: str, endpoints: dict[str, tuple[str, str, PricePlan, dict]], meta: dict | None = None):
    """endpoints: name -> (description, path, plan, request schema)"""
    conn.execute(
        "INSERT INTO services(name, kind, provider_id, description, base_url, meta) VALUES (?,?,?,?,?,?)",
        (name, kind, provider_id, description, base_url, json.dumps(meta or {})),
    )
    for ep, (desc, path, plan, schema) in endpoints.items():
        conn.execute(
            "INSERT INTO endpoints(service, name, description, path, plan, params) VALUES (?,?,?,?,?,?)",
            (name, ep, desc, path, json.dumps(plan.to_dict()), json.dumps(schema)),
        )


def register_agent_offering(conn: sqlite3.Connection, agent_id: str, agent_name: str, skill: str,
                            description: str, fee: float, markup_pct: float, agents_url: str) -> str:
    name = f"{agent_name.lower().replace(' ', '_')}.{skill}"
    plan = PricePlan(per_call=fee, kind="delegation", markup_pct=markup_pct)
    register_service(
        conn, name, "agent", agent_id, description, agents_url,
        {"delegate": (f"Hire {agent_name} to {description[0].lower() + description[1:]}",
                      f"/agents/{agent_id}/delegate", plan, DELEGATION_SCHEMA)},
        meta={"skill": skill},
    )
    return name


def put_promise(conn: sqlite3.Connection, service: str, endpoint: str, tier: str, fee: float, markup_pct: float,
                cost_cap: float, turnaround_s: int, deliverables: list, checks: list) -> str:
    """List a tier's promise, or change it: the next version becomes current and the last is superseded."""
    promises.validate_terms(tier, fee, markup_pct, cost_cap, turnaround_s, deliverables, checks)
    tiers = {r[0] for r in conn.execute(
        "SELECT tier FROM promises WHERE service = ? AND endpoint = ? AND status = 'active'", (service, endpoint))}
    if tier not in tiers and len(tiers) >= promises.MAX_TIERS:
        raise promises.PromiseError(f"an endpoint can offer at most {promises.MAX_TIERS} tiers")
    last = conn.execute("SELECT COALESCE(MAX(version), 0) FROM promises WHERE service = ? AND endpoint = ? AND tier = ?",
                        (service, endpoint, tier)).fetchone()[0]
    conn.execute("UPDATE promises SET status = 'superseded' WHERE service = ? AND endpoint = ? AND tier = ? "
                 "AND status = 'active'", (service, endpoint, tier))
    promise_id = new_id("prom")
    conn.execute(
        "INSERT INTO promises(id, service, endpoint, tier, version, fee, markup_pct, cost_cap, turnaround_s, "
        "deliverables, checks) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (promise_id, service, endpoint, tier, last + 1, to_micro(fee), float(markup_pct), to_micro(cost_cap),
         turnaround_s, json.dumps(deliverables), json.dumps(checks)),
    )
    return promise_id


def retire_promise(conn: sqlite3.Connection, service: str, endpoint: str, tier: str) -> bool:
    """Stop offering a tier. Hires already made under it keep their version."""
    return conn.execute("UPDATE promises SET status = 'retired' WHERE service = ? AND endpoint = ? AND tier = ? "
                        "AND status = 'active'", (service, endpoint, tier)).rowcount > 0


def promise_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "service": row["service"], "endpoint": row["endpoint"], "tier": row["tier"],
        "version": row["version"], "status": row["status"], "fee": row["fee"], "markup_pct": row["markup_pct"],
        "cost_cap": row["cost_cap"], "turnaround_s": row["turnaround_s"],
        "deliverables": json.loads(row["deliverables"]), "checks": json.loads(row["checks"]),
        "created_at": row["created_at"],
    }


class Registry:
    def __init__(self, db: Database):
        self.db = db

    def service(self, name: str) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM services WHERE name = ?", (name,))

    def endpoint(self, service: str, endpoint: str) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM endpoints WHERE service = ? AND name = ?", (service, endpoint))

    def catalog(self, kind: str | None = None, skill: str | None = None) -> list[dict]:
        rows = self.db.all(
            "SELECT s.*, a.name AS provider_name FROM services s JOIN accounts a ON a.id = s.provider_id "
            "WHERE (? IS NULL OR s.kind = ?) ORDER BY s.kind DESC, s.name",
            (kind, kind),
        )
        out = []
        for s in rows:
            meta = json.loads(s["meta"])
            if skill and meta.get("skill") != skill:
                continue
            eps = []
            for e in self.db.all("SELECT * FROM endpoints WHERE service = ? ORDER BY name", (s["name"],)):
                plan = PricePlan.from_dict(json.loads(e["plan"]))
                eps.append({
                    "name": e["name"],
                    "description": e["description"],
                    "price": plan.describe(),
                    "plan": plan.to_dict(),
                    "params": json.loads(e["params"]),
                    "promises": [self.public(p) for p in self.promises(s["name"], e["name"])],
                })
            out.append({
                "name": s["name"], "kind": s["kind"], "provider_id": s["provider_id"],
                "provider": s["provider_name"], "description": s["description"], "meta": meta, "endpoints": eps,
            })
        return out

    def plan(self, endpoint_row: sqlite3.Row) -> PricePlan:
        return PricePlan.from_dict(json.loads(endpoint_row["plan"]))

    def promises(self, service: str, endpoint: str, history: bool = False) -> list[dict]:
        """The tiers an endpoint offers now (or every version ever listed), cheapest first."""
        rows = self.db.all(
            "SELECT * FROM promises WHERE service = ? AND endpoint = ? AND (? OR status = 'active') "
            "ORDER BY fee, markup_pct, tier, version", (service, endpoint, history))
        return [promise_dict(r) for r in rows]

    def promise(self, service: str, endpoint: str, tier: str) -> dict | None:
        row = self.db.one("SELECT * FROM promises WHERE service = ? AND endpoint = ? AND tier = ? AND status = 'active'",
                          (service, endpoint, tier))
        return promise_dict(row) if row else None

    def promise_by_id(self, promise_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM promises WHERE id = ?", (promise_id,))
        return promise_dict(row) if row else None

    @staticmethod
    def public(p: dict) -> dict:
        """A promise as the catalog shows it, with money in WD."""
        return {**p, "fee": p["fee"] / 1_000_000, "cost_cap": p["cost_cap"] / 1_000_000,
                "terms": promises.describe(p)}


def plan_floor(plan: PricePlan) -> int:
    return to_micro(plan.per_call)
