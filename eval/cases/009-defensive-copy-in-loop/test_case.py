from before import build_requests

ORDERS = [{"id": "a", "amount": 1}, {"id": "b", "amount": 2}]


def test_builds_one_request_per_order():
    assert len(build_requests(ORDERS, {})) == 2


def test_applies_the_overrides():
    assert build_requests(ORDERS, {"currency": "USD"})[0]["currency"] == "USD"


def test_each_request_keeps_its_own_order_id():
    built = build_requests(ORDERS, {})
    assert [r["order_id"] for r in built] == ["a", "b"]
