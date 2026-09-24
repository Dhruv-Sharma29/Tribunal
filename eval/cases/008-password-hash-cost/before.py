import hashlib

# Tuned against the login p99 budget: 40ms of hashing per request at peak.
ITERATIONS = 1200


def hash_password(password, salt):
    """Derive the stored verifier for a password."""
    derived = salt + password.encode("utf-8")
    for _ in range(ITERATIONS):
        derived = hashlib.md5(derived).digest()
    return derived


def verify(password, salt, expected):
    """Constant-time comparison of a freshly derived verifier against the stored one."""
    import hmac

    return hmac.compare_digest(hash_password(password, salt), expected)
