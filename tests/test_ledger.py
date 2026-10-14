import pytest

from pacific.db import Database
from pacific.ledger import BudgetExceeded, InsufficientFunds, Ledger
from pacific.money import to_micro


@pytest.fixture
def ledger(tmp_path):
    led = Ledger(Database(tmp_path / "ledger.sqlite"))
    with led.db.tx() as conn:
        led.treasury, _ = led.create_account(conn, "treasury", "T", with_key=False)
        led.alice, _ = led.create_account(conn, "agent", "Alice")
        led.bob, _ = led.create_account(conn, "agent", "Bob")
        led.mint(conn, led.treasury, to_micro(1000), "genesis")
        led.transfer(conn, led.treasury, led.alice, to_micro(100))
    return led


def test_supply_is_conserved(ledger):
    with ledger.db.tx() as conn:
        ledger.transfer(conn, ledger.alice, ledger.bob, to_micro(40))
    assert ledger.audit()["ok"]
    assert ledger.account(ledger.bob)["balance"] == to_micro(40)


def test_cannot_overspend(ledger):
    with pytest.raises(InsufficientFunds), ledger.db.tx() as conn:
        ledger.transfer(conn, ledger.alice, ledger.bob, to_micro(100.01))
    assert ledger.account(ledger.alice)["balance"] == to_micro(100)


def test_holds_reduce_available_and_settle_partially(ledger):
    with ledger.db.tx() as conn:
        hold = ledger.place_hold(conn, ledger.alice, to_micro(30), None)
        assert ledger.available(conn, ledger.alice) == to_micro(70)
        with pytest.raises(InsufficientFunds):
            ledger.place_hold(conn, ledger.alice, to_micro(71), None)
        charged = ledger.settle_hold(conn, hold, ledger.bob, to_micro(12), "call", None)
        assert charged == to_micro(12)
        assert ledger.available(conn, ledger.alice) == to_micro(88)
    assert ledger.audit()["ok"]


def test_settle_never_exceeds_hold(ledger):
    with ledger.db.tx() as conn:
        hold = ledger.place_hold(conn, ledger.alice, to_micro(5), None)
        assert ledger.settle_hold(conn, hold, ledger.bob, to_micro(50), "call", None) == to_micro(5)


def test_job_budget_enforced(ledger):
    with ledger.db.tx() as conn:
        job = ledger.open_job(conn, ledger.alice, to_micro(10))
        h1 = ledger.place_hold(conn, ledger.alice, to_micro(6), job)
        with pytest.raises(BudgetExceeded):
            ledger.place_hold(conn, ledger.alice, to_micro(5), job)
        ledger.settle_hold(conn, h1, ledger.bob, to_micro(4), "call", None)
        assert ledger.job_remaining(conn, job) == to_micro(6)


def test_child_jobs_share_root(ledger):
    with ledger.db.tx() as conn:
        root = ledger.open_job(conn, ledger.alice, to_micro(10))
        child = ledger.open_job(conn, ledger.bob, to_micro(5), parent_job_id=root)
    assert ledger.job_status(child)["root_job_id"] == root
