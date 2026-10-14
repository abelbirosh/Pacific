from pacific.money import to_micro
from pacific.pricing import PricePlan


def test_flat():
    p = PricePlan(0.5)
    assert p.max_cost({}) == p.cost({}, units=99) == to_micro(0.5)


def test_metered_cap_and_param():
    p = PricePlan(1.0, "page", 0.5, max_units=5, units_param="max_results")
    assert p.max_cost({}) == to_micro(3.5)
    assert p.max_cost({"max_results": 2}) == to_micro(2.0)
    assert p.cost({"max_results": 2}, units=4) == to_micro(2.0)  # billed units capped by the request
    assert p.cost({}, units=3) == to_micro(2.5)


def test_units_from_request_size():
    put = PricePlan(0.05, "kb", 0.01, max_units=2048, units_from="kb:content")
    assert put.max_cost({"content": "x" * 3000}) == to_micro(0.08)
    scan = PricePlan(2.0, "kloc", 1.5, max_units=50, units_from="kloc:files")
    assert scan.max_cost({"files": {"a.py": "x\n" * 250}}) == to_micro(2.0 + 1.5 * 0.3)
    many = PricePlan(0.2, "row", 0.0005, max_units=10_000, units_from="count:rows")
    assert many.max_cost({"rows": [[1]] * 100}) == to_micro(0.25)


def test_delegation():
    p = PricePlan(2.0, kind="delegation", markup_pct=10)
    assert p.max_cost({"budget": 10}) == to_micro(13.0)
    assert p.cost({"budget": 10}, passthrough=to_micro(4)) == to_micro(6.4)
