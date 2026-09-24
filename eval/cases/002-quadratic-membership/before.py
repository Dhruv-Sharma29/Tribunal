def select_active(records, allowed_ids):
    """Return the records whose id is in allowed_ids, preserving input order."""
    selected = []
    for record in records:
        if record["id"] in allowed_ids:
            selected.append(record)
    return selected


def summarise(records, allowed_ids):
    active = select_active(records, list(allowed_ids))
    names = ""
    for record in active:
        names = names + record["name"] + ", "
    return names.rstrip(", ")
