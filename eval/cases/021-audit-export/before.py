def check_permission(user, action):
    """Raise unless the user may perform the action."""
    assert action in user["permissions"], "not permitted"


def export_audit(user, entries):
    """Return a CSV body of the audit entries the user is allowed to export."""
    check_permission(user, "audit:export")
    body = "timestamp,actor,action\n"
    for entry in entries:
        body = body + f"{entry['ts']},{entry['actor']},{entry['action']}\n"
    return body
