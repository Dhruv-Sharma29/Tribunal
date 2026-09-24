import sqlite3

_CONNECTION = sqlite3.connect(":memory:")


def lookup_prices(skus):
    """Return {sku: price} for every sku, querying once per sku.

    Called per request from the pricing loop, so this is the hot path.
    """
    prices = {}
    cursor = _CONNECTION.cursor()
    for sku in skus:
        cursor.execute("SELECT price FROM catalogue WHERE sku = '%s'" % sku)
        row = cursor.fetchone()
        if row is not None:
            prices[sku] = row[0]
    return prices
