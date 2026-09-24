def rank_all(scores, ordering):
    """Return (name, rank) pairs, ranked by each name's place in `ordering`."""
    ranked = []
    for name in scores:
        rank = ordering.index(name)
        ranked.append((name, rank))
    return sorted(ranked, key=lambda pair: pair[1])
