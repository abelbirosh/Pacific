"""The persona-driven sim brain against a real economy: every persona finishes or fails cleanly,
runs replay from their seed, chains form, quitters leave nothing on hold, and every probe the
adversary sends is refused without charge."""

import pytest

from pacific.agents.personas import PERSONAS
from support import agent_with, assert_books_balance, boot, load, run_task


@pytest.fixture(scope="module", autouse=True)
def no_thinking():
    mp = pytest.MonkeyPatch()
    mp.setenv("PACIFIC_THINK_SCALE", "0")
    yield
    mp.undo()


@pytest.fixture(scope="module")
def market(tmp_path_factory):
    with boot(tmp_path_factory.mktemp("sim") / "home", seed=3, brain="sim") as env:
        yield env


@pytest.fixture(scope="module")
def delegators(tmp_path_factory):
    with boot(tmp_path_factory.mktemp("chains") / "home", seed=5, brain="sim:delegator") as env:
        yield env


def sim_task(name: str, agent: str, persona: str, seed: int, **overrides) -> dict:
    spec = {**load(name), "agent": agent, **overrides}
    spec["inputs"] = {**spec["inputs"], "_persona": persona, "_seed": seed}
    return spec


def check_report(report: dict, persona: str):
    assert report["status"] in ("passed", "failed"), report["output"]
    assert report["spent"]["wd"] <= report["budget"]["wd"]
    sim = report["artifacts"]["sim"]
    assert sim["persona"] == persona
    assert not sim["violations"], sim["violations"]
    assert not any("-> internal_error" in d for d in sim["decisions"]), sim["decisions"]
    return sim


def test_genesis_deals_out_personas(market):
    _, secrets, _ = market
    brains = [a["brain"] for a in secrets["agents"].values()]
    assert all(b.startswith("sim:") and b[4:] in PERSONAS for b in brains)
    assert len(set(brains)) == len(brains)


@pytest.mark.parametrize("persona", sorted(PERSONAS))
@pytest.mark.parametrize("task", ["database.json", "code_run.json"])
def test_every_persona_finishes_or_fails_cleanly(market, persona, task):
    _, secrets, admin = market
    aid, _ = agent_with(secrets)
    for seed in range(3):
        check_report(run_task(admin, sim_task(task, aid, persona, seed)), persona)
        assert_books_balance(admin)


def test_same_seed_replays_the_same_decisions(market):
    _, secrets, admin = market
    aid, _ = agent_with(secrets, skill="code_run")
    runs = [run_task(admin, sim_task("code_run.json", aid, "sloppy", seed=42)) for _ in range(2)]
    a, b = (r["artifacts"]["sim"] for r in runs)
    assert a["decisions"] == b["decisions"]
    assert [(t["tool"], t["ok"]) for t in runs[0]["trace"]] == [(t["tool"], t["ok"]) for t in runs[1]["trace"]]


def test_careful_agents_pass_the_reference_tasks(market):
    _, secrets, admin = market
    aid, _ = agent_with(secrets, skill="database")
    for seed in range(3):
        report = run_task(admin, sim_task("database.json", aid, "careful", seed))
        assert report["status"] == "passed", (report["output"], report["checks"])


def test_sloppy_agents_fumble_and_recover(market):
    _, secrets, admin = market
    aid, _ = agent_with(secrets, skill="database")
    sims = [check_report(run_task(admin, sim_task("database.json", aid, "sloppy", s)), "sloppy") for s in range(6)]
    assert sum(s["fumbles"] for s in sims) > 0
    assert_books_balance(admin)


def test_quitters_leave_nothing_on_hold(market):
    _, secrets, admin = market
    aid, _ = agent_with(secrets)
    sims = [check_report(run_task(admin, sim_task("database.json", aid, "quitter", s)), "quitter") for s in range(12)]
    assert any(s["gave_up"] for s in sims)
    assert_books_balance(admin)


def test_adversary_probes_are_all_refused(market):
    _, secrets, admin = market
    aid, _ = agent_with(secrets)
    probed = set()
    for seed in range(6):
        sim = check_report(run_task(admin, sim_task("code_run.json", aid, "adversary", seed, budget=10)), "adversary")
        probed |= {p["probe"] for p in sim["probes"]}
    assert len(probed) >= 10, probed
    assert_books_balance(admin)


def test_delegators_build_chains(delegators):
    _, secrets, admin = delegators
    aid, _ = agent_with(secrets)
    depths = []
    for seed in range(6):
        report = run_task(admin, sim_task("database.json", aid, "delegator", seed, budget=40))
        check_report(report, "delegator")
        depths.append(max((c["depth"] for c in report["calls"]), default=0))
    assert max(depths) >= 2, depths
    assert_books_balance(admin)
