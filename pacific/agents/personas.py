"""Behaviour profiles for the simulated brain. Each knob is a probability or a range the brain
draws from with its seeded RNG, so one persona produces varied but reproducible traffic.

The mix is meant to look like a market of imperfect LLM agents: some shop around and quote
everything, some overspend, some fumble arguments and retry, some quit halfway, a few poke at
the exchange's defences.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Persona:
    name: str
    blurb: str
    browse: float = 0.3                   # check catalog / budget / notes before starting
    quote_first: float = 0.0              # quote a paid call before making it
    max_cost_guard: float = 0.0           # send X-Pacific-Max-Cost with a paid call
    guard_slack: tuple[float, float] = (1.05, 1.3)  # × the quote; below 1.0 means a self-inflicted 402
    tier: str = "best"                    # research tier: best | cheapest | random
    results: tuple[int, int] = (3, 6)     # research result count range
    timeout_s: tuple[int, int] = (3, 12)  # sandbox timeout range when the task doesn't fix one
    delegate: float = 0.0                 # hire out a skill it could do itself
    hire: str = "cheapest"                # pick seller and tier by: cheapest | random | priciest | ranked (track record)
    hire_share: tuple[float, float] = (0.6, 0.95)  # share of what's left handed to a hired agent
    max_depth: int = 2                    # won't hire when already this deep in a delegation chain
    retries: int = 1                      # retries on transient failures (5xx, 429, transport)
    sloppy: float = 0.0                   # a paid call is preceded by a malformed attempt
    abandon: float = 0.0                  # gives up after 0-2 paid calls
    tip: float = 0.0                      # tips a hired agent from its wallet after a success
    mischief: float = 0.0                 # runs each probe of the exchange's defences; ignores chains
    think_s: float = 0.0                  # mean pause before a decision (× PACIFIC_THINK_SCALE)
    shirk: float = 0.0                    # when hired: claims done having delivered nothing, or half the job


PERSONAS: dict[str, Persona] = {p.name: p for p in (
    Persona("careful", "Shops around, quotes everything, hires by track record, guards spend with a max cost.",
            browse=0.9, quote_first=0.9, max_cost_guard=0.6, guard_slack=(1.02, 1.2), tier="best",
            delegate=0.05, hire="ranked", retries=2, think_s=0.8),
    Persona("frugal", "Always the cheapest tier and seller, small budgets for hires.",
            browse=0.5, quote_first=0.6, tier="cheapest", results=(2, 4), timeout_s=(2, 5),
            delegate=0.1, hire="cheapest", hire_share=(0.4, 0.7), think_s=0.5),
    Persona("spendthrift", "Buys the premium tier, hires the priciest seller, never quotes.",
            browse=0.1, tier="best", results=(6, 10), timeout_s=(15, 30), delegate=0.3,
            hire="priciest", hire_share=(0.85, 0.98), retries=0, think_s=0.3),
    Persona("delegator", "Prefers hiring other agents and builds deep chains; tips good work.",
            browse=0.6, quote_first=0.3, tier="random", delegate=0.75, hire="random",
            max_depth=4, tip=0.4, think_s=0.6),
    Persona("sloppy", "Fumbles arguments, misjudges max cost, retries a lot, sometimes quits.",
            browse=0.2, quote_first=0.4, max_cost_guard=0.5, guard_slack=(0.7, 1.15), tier="random",
            delegate=0.15, hire="random", retries=3, sloppy=0.5, abandon=0.1, think_s=1.0),
    Persona("quitter", "Starts strong and abandons a third of its tasks halfway.",
            browse=0.3, tier="random", delegate=0.1, retries=0, abandon=0.35, think_s=0.4),
    Persona("adversary", "Probes the exchange: overspends, hijacks jobs, self-deals, loops chains.",
            browse=0.5, quote_first=0.2, tier="random", delegate=0.25, hire="random", max_depth=6,
            sloppy=0.2, mischief=0.6, think_s=0.3),
    Persona("shirker", "Takes hires, then claims done having delivered nothing or only half the job.",
            browse=0.2, tier="random", delegate=0.1, hire="random", shirk=0.6, think_s=0.3),
)}

# Plain `--brain sim` deals these out. The shirker is opt-in (`--brain sim:shirker,careful,...`), and
# keeping it out leaves seeded worlds exactly as they were before it existed.
DEFAULT_MIX = tuple(p for p in sorted(PERSONAS) if p != "shirker")
