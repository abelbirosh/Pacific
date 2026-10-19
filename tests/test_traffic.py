"""Concurrent traffic from every persona, with faults injected, against a real economy.

The short run is part of the normal suite. PACIFIC_SOAK=<tasks> runs a longer one with chaos
faults (e.g. PACIFIC_SOAK=500 uv run pytest tests/test_traffic.py -k soak -s).
"""

import asyncio
import os

import pytest

from pacific.faults import write_faults
from pacific.sim.traffic import TrafficConfig, format_report, run_traffic
from pacific.sim.workload import Workload
from support import boot


@pytest.fixture(scope="module")
def market(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("PACIFIC_SEARCH", "offline")
    mp.setenv("PACIFIC_PLATFORM_TIMEOUT", "5")
    mp.setenv("PACIFIC_DELEGATION_TIMEOUT", "60")  # a deadlock fails the run instead of hanging it
    with boot(tmp_path_factory.mktemp("traffic") / "home", seed=11, brain="sim") as env:
        yield env
    mp.undo()


def drive(market, profile: str, limit_s: float = 240, **cfg) -> dict:
    config, secrets, _ = market
    write_faults(config.home, profile, seed=cfg.get("seed", 1))
    try:
        run = run_traffic(config.exchange_url, secrets, TrafficConfig(**cfg))
        report = asyncio.run(asyncio.wait_for(run, timeout=limit_s))
    except asyncio.TimeoutError:
        pytest.fail(f"traffic didn't finish within {limit_s}s: something is stuck (deadlock?)")
    finally:
        write_faults(config.home, "none")
    print(format_report(report))
    return report


def assert_healthy(report: dict):
    assert not report["invariant_violations"], report["invariant_violations"]
    assert not report["probe_violations"], report["probe_violations"]
    assert not report["crash_count"], report["crashes"]
    assert report["audit"]["ok"]


def test_workload_is_reproducible():
    a, b = Workload(seed=4), Workload(seed=4)
    for _ in range(30):
        assert a.next().spec == b.next().spec


def test_concurrent_traffic_with_faults(market):
    report = drive(market, "harsh", tasks=60, concurrency=48, rate=40, seed=2,
                   personas=["careful", "frugal", "spendthrift", "delegator", "sloppy", "quitter", "adversary"])
    assert_healthy(report)
    assert report["overall"]["statuses"].get("passed", 0) > 15
    assert set(report["by_persona"]) == {"careful", "frugal", "spendthrift", "delegator", "sloppy", "quitter", "adversary"}
    assert report["overall"]["retries"] > 0  # faults were hit and survived
    # Agents hired out work. Chains two or more deep are rarer under contracts, since a hired agent gets at
    # most its tier's cost cap to spend and often can't afford to hire again; test_sim_brain's
    # test_delegators_build_chains checks deep chains deterministically.
    assert report["overall"]["max_depth"] >= 1
    assert report["contracts"]["contracts"] > 0 and report["contracts"]["in_escrow_wd"] == 0


def test_deep_delegation_does_not_starve_agent_threads(market):
    """More delegating tasks in flight than the agent host once had threads (it deadlocked)."""
    report = drive(market, "none", limit_s=90, tasks=70, concurrency=64, rate=200, seed=3, personas=["delegator"])
    assert_healthy(report)
    assert report["overall"]["latency_max_s"] < 60


@pytest.mark.skipif(not os.environ.get("PACIFIC_SOAK"), reason="set PACIFIC_SOAK=<tasks> for a long chaos run")
def test_soak(market):
    tasks = int(os.environ["PACIFIC_SOAK"])
    report = drive(market, "chaos", limit_s=60 + tasks * 2, tasks=tasks, concurrency=48, rate=20, seed=5)
    assert_healthy(report)
