from decimal import Decimal

from before import largest, rollup

ENTRIES = [
    {"account": "4000", "amount": "10.05"},
    {"account": "4000", "amount": "0.10"},
    {"account": "5000", "amount": "3.00"},
]


def test_totals_by_account():
    assert rollup(ENTRIES) == {"4000": Decimal("10.15"), "5000": Decimal("3.00")}


def test_amounts_stay_exact():
    entries = [{"account": "a", "amount": "0.1"}] * 3
    assert rollup(entries)["a"] == Decimal("0.3")


def test_largest_is_ordered_and_limited():
    assert largest(rollup(ENTRIES), limit=1) == [("4000", Decimal("10.15"))]
