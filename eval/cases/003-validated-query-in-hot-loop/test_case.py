import sqlite3

import before


def setup_module(_):
    before._CONNECTION = sqlite3.connect(":memory:")
    before._CONNECTION.execute("CREATE TABLE catalogue (sku TEXT, price REAL)")
    before._CONNECTION.execute("INSERT INTO catalogue VALUES ('abc', 1.5)")


def test_returns_prices_for_known_skus():
    assert before.lookup_prices(["abc"]) == {"abc": 1.5}


def test_skips_unknown_skus():
    assert before.lookup_prices(["nope"]) == {}
