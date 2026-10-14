"""An agent's toolbox: free internal tools plus market tools that spend wave dollars.

Every tool has a JSON schema so an LLM brain can be handed `toolbox.specs()` directly; the
scripted brain calls the same tools by name. Each invocation is recorded in the trace.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..services.search.web import best_passages
from .client import ExchangeClient, MarketError


@dataclass
class Tool:
    name: str
    description: str
    params: dict
    fn: Callable[..., Any]
    costs_money: bool = False


@dataclass
class TraceEvent:
    tool: str
    args: dict
    ok: bool
    summary: str
    cost: float = 0.0
    ms: int = 0


def _brief(args: dict) -> dict:
    """Keep traces readable: truncate long string arguments."""
    def cut(v):
        if isinstance(v, str) and len(v) > 160:
            return v[:160] + f"... ({len(v)} chars)"
        if isinstance(v, dict):
            return {k: cut(x) for k, x in list(v.items())[:12]}
        if isinstance(v, list):
            return [cut(x) for x in v[:8]]
        return v
    return cut(args)


class Toolbox:
    def __init__(self, client: ExchangeClient, notes_path: Path, agent_id: str, chain: list[str]):
        self.client = client
        self.agent_id = agent_id
        self.chain = chain  # agents already in this delegation chain (never hire them again)
        self.notes_path = notes_path
        self.trace: list[TraceEvent] = []
        self.tools: dict[str, Tool] = {}
        self._register_builtin()

    # ---- invocation -------------------------------------------------------------------------

    def specs(self) -> list[dict]:
        return [{"name": t.name, "description": t.description, "input_schema": t.params} for t in self.tools.values()]

    def call(self, name: str, **args) -> Any:
        tool = self.tools[name]
        t0 = time.monotonic()
        try:
            out = tool.fn(**args)
        except MarketError as e:
            self.trace.append(TraceEvent(name, _brief(args), False, str(e)[:300], 0, int((time.monotonic() - t0) * 1000)))
            raise
        cost = getattr(out, "cost", 0.0) if tool.costs_money else 0.0
        summary = self._summarize(name, out)
        self.trace.append(TraceEvent(name, _brief(args), True, summary, cost, int((time.monotonic() - t0) * 1000)))
        return out

    @staticmethod
    def _summarize(name: str, out: Any) -> str:
        if hasattr(out, "call_id"):
            tail = f" error: {out.error}" if out.error else ""
            return f"{out.service}/{out.endpoint} cost {out.cost:.4f} WD{tail}"
        text = out if isinstance(out, str) else json.dumps(out, default=str)
        return text[:200]

    def register(self, tool: Tool):
        self.tools[tool.name] = tool

    # ---- built-ins --------------------------------------------------------------------------

    def _register_builtin(self):
        c = self.client
        obj = lambda props, req=(): {"type": "object", "properties": props, "required": list(req)}

        self.register(Tool(
            "budget_status", "How many wave dollars this job has left to spend (free).", obj({}),
            lambda: c.job() or c.me(),
        ))
        self.register(Tool(
            "list_services", "Browse the marketplace: platform services and agents for hire, with prices (free).",
            obj({"kind": {"type": "string", "enum": ["platform", "agent"]}, "skill": {"type": "string"}}),
            lambda kind=None, skill=None: c.services(kind, skill),
        ))
        self.register(Tool(
            "quote", "Worst-case cost of a call before making it (free).",
            obj({"service": {"type": "string"}, "endpoint": {"type": "string"}, "args": {"type": "object"}},
                ["service", "endpoint", "args"]),
            lambda service, endpoint, args: c.quote(service, endpoint, args),
        ))
        self.register(Tool(
            "call_service", "Buy one call to a platform service endpoint. Charged per its price plan.",
            obj({"service": {"type": "string"}, "endpoint": {"type": "string"}, "args": {"type": "object"},
                 "max_cost": {"type": "number", "description": "refuse if worst case exceeds this"}},
                ["service", "endpoint", "args"]),
            lambda service, endpoint, args, max_cost=None: c.call(service, endpoint, args, max_cost),
            costs_money=True,
        ))
        self.register(Tool(
            "hire_agent", "Delegate a subtask to another agent's service. You pay its fee plus what it spends.",
            obj({"service": {"type": "string"}, "description": {"type": "string"},
                 "budget": {"type": "number"}, "inputs": {"type": "object"}},
                ["service", "description", "budget"]),
            self._hire, costs_money=True,
        ))
        self.register(Tool(
            "notes_write", "Save a private note that persists across tasks (free).",
            obj({"key": {"type": "string"}, "text": {"type": "string"}}, ["key", "text"]),
            self._notes_write,
        ))
        self.register(Tool(
            "notes_read", "Read private notes; omit key to list them (free).",
            obj({"key": {"type": "string"}}),
            self._notes_read,
        ))
        self.register(Tool(
            "extract_passages", "Pick the passages of a text most relevant to a query, locally (free).",
            obj({"text": {"type": "string"}, "query": {"type": "string"}, "k": {"type": "integer"}}, ["text", "query"]),
            lambda text, query, k=3: best_passages(text, query, k),
        ))

    def _hire(self, service: str, description: str, budget: float, inputs: dict | None = None):
        inputs = {**(inputs or {}), "_chain": self.chain + [self.agent_id]}
        return self.client.call(service, "delegate", {"description": description, "budget": budget, "inputs": inputs})

    def _notes(self) -> dict:
        return json.loads(self.notes_path.read_text()) if self.notes_path.exists() else {}

    def _notes_write(self, key: str, text: str) -> str:
        notes = self._notes()
        notes[key] = text
        self.notes_path.parent.mkdir(parents=True, exist_ok=True)
        self.notes_path.write_text(json.dumps(notes, indent=2))
        return f"saved note {key!r}"

    def _notes_read(self, key: str | None = None):
        notes = self._notes()
        return notes.get(key) if key else sorted(notes)
