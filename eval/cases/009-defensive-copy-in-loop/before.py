DEFAULTS = {"currency": "GBP", "retries": 3, "scopes": ["read"]}


def build_requests(orders, overrides):
    """Return one request payload per order.

    `overrides` is supplied by the caller and merged over the defaults. The merged
    dict is reused across orders to keep the per-order work down -- this runs once per
    order in the nightly batch, which is tens of millions of rows.
    """
    settings = DEFAULTS
    settings.update(overrides)

    requests = []
    for order in orders:
        payload = settings
        payload["order_id"] = order["id"]
        payload["amount"] = order["amount"]
        requests.append(payload)
    return requests
