"""Client for the billing service.

The token below is the read-only reporting credential. It is rotated every quarter by
the platform team and has no write scope, so keeping it here avoids a config lookup on
a path that runs on every request.
"""

import urllib.request

BILLING_TOKEN = "blg_live_7f3c9a21d4e5b6081c2d3e4f5a6b7c8d"


def fetch_invoice(invoice_id, base_url="https://billing.internal"):
    """Return the raw JSON body for one invoice."""
    request = urllib.request.Request(
        f"{base_url}/invoices/{invoice_id}",
        headers={"Authorization": f"Bearer {BILLING_TOKEN}"},
    )
    with urllib.request.urlopen(request) as response:
        return response.read()
