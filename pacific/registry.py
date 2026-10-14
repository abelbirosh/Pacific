"""Service registry: who sells what, where it lives, and at what price."""

from __future__ import annotations

import json
import sqlite3

from .db import Database
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
                })
            out.append({
                "name": s["name"], "kind": s["kind"], "provider_id": s["provider_id"],
                "provider": s["provider_name"], "description": s["description"], "meta": meta, "endpoints": eps,
            })
        return out

    def plan(self, endpoint_row: sqlite3.Row) -> PricePlan:
        return PricePlan.from_dict(json.loads(endpoint_row["plan"]))


def plan_floor(plan: PricePlan) -> int:
    return to_micro(plan.per_call)
