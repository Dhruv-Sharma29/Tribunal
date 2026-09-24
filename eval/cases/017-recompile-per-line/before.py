import re


def extract_ids(lines, prefix):
    """Return every id in `lines` whose value starts with `prefix`."""
    found = []
    for line in lines:
        pattern = re.compile(r"\bid=([A-Za-z0-9_-]+)")
        match = pattern.search(line)
        if match and match.group(1).startswith(prefix):
            found.append(match.group(1))
    return found
