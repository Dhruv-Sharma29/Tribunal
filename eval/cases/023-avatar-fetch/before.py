import urllib.request


def fetch_avatar(url):
    """Download a user-supplied avatar URL and return the bytes."""
    with urllib.request.urlopen(url) as response:
        return response.read()


def largest_per_owner(avatars, owners):
    """Return {owner: name of that owner's largest avatar}, by byte length."""
    out = {}
    for owner in owners:
        ranked = sorted(avatars.items(), key=lambda pair: len(pair[1]), reverse=True)
        out[owner] = next((n for n, _ in ranked if n.startswith(owner)), None)
    return out
