"""Price plans. Every endpoint has one; the exchange uses it to reserve and then charge.

metered:     cost = per_call + per_unit * units      (units reported by the provider)
delegation:  cost = per_call (fee) + passthrough * (1 + markup_pct/100)
             where passthrough is what the hired agent actually spent on its subtask.

Before a call the exchange places a hold for the worst case (`max_cost`), then charges the
actual cost, which can never exceed the hold.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from .money import ceil_micro, fmt, to_micro


@dataclass(frozen=True)
class PricePlan:
    per_call: float
    unit: str | None = None
    per_unit: float = 0.0
    max_units: float = 0.0  # hard cap on billable units per call
    units_param: str | None = None  # request field that lowers the cap (e.g. "max_results")
    units_from: str | None = None  # size the cap from the request: "kb:content", "kloc:files", "count:urls"
    kind: str = "metered"  # metered | delegation
    markup_pct: float = 0.0

    def unit_cap(self, body: dict) -> float:
        cap = self.max_units
        if self.units_param and isinstance(body.get(self.units_param), (int, float)):
            cap = min(cap, max(0.0, float(body[self.units_param])))
        if self.units_from:
            measured = measure(self.units_from, body)
            if measured is not None:
                cap = min(cap, measured)
        return cap

    def max_cost(self, body: dict) -> int:
        if self.kind == "delegation":
            budget = to_micro(body.get("budget", 0))
            return to_micro(self.per_call) + ceil_micro(budget / 1_000_000 * (1 + self.markup_pct / 100))
        return to_micro(self.per_call) + ceil_micro(self.per_unit * self.unit_cap(body))

    def cost(self, body: dict, units: float = 0.0, passthrough: int = 0) -> int:
        if self.kind == "delegation":
            return to_micro(self.per_call) + ceil_micro(passthrough / 1_000_000 * (1 + self.markup_pct / 100))
        billable = min(max(0.0, units), self.unit_cap(body))
        return to_micro(self.per_call) + ceil_micro(self.per_unit * billable)

    def describe(self) -> str:
        base = fmt(to_micro(self.per_call)) + (" fee" if self.kind == "delegation" else "/call")
        if self.kind == "delegation":
            return f"{base} + subtask spend at cost +{self.markup_pct:g}%"
        if self.unit and self.per_unit:
            return f"{base} + {fmt(to_micro(self.per_unit))}/{self.unit} (max {self.max_units:g} {self.unit}s)"
        return base

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PricePlan":
        return cls(**d)


def measure(spec: str, body: dict) -> float | None:
    """Estimate billable units from the request itself, so holds track the real job size."""
    how, _, field = spec.partition(":")
    value = body.get(field)
    if value is None:
        return None
    if how == "count":
        return float(len(value))
    if how == "kb":
        return float(max(1, math.ceil(len(str(value).encode()) / 1024)))
    if how == "lines":
        return float(len([l for l in str(value).splitlines() if l.strip()]))
    if how == "kloc":
        texts = value.values() if isinstance(value, dict) else [str(value)]
        lines = sum(t.count("\n") + 1 for t in texts)
        return max(0.1, math.ceil(lines / 100) / 10)
    raise ValueError(f"unknown units_from {spec!r}")
