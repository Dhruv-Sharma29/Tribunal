"""Monthly roll-up of ledger entries."""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from decimal import Decimal


def rollup(entries: Iterable[Mapping[str, object]]) -> dict[str, Decimal]:
    """Total the `amount` of each entry by its `account`.

    Amounts are `Decimal` because these are money; summing floats here would drift by a
    penny or two a month and the reconciliation would fail for reasons nobody could
    find.
    """
    totals: defaultdict[str, Decimal] = defaultdict(Decimal)
    for entry in entries:
        account = str(entry["account"])
        totals[account] += Decimal(str(entry["amount"]))
    return dict(totals)


def largest(totals: Mapping[str, Decimal], limit: int = 5) -> list[tuple[str, Decimal]]:
    """The `limit` accounts with the biggest totals, largest first."""
    return sorted(totals.items(), key=lambda item: item[1], reverse=True)[:limit]
