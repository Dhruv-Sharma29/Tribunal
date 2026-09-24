"""Token fingerprinting with a fast hash."""

import hashlib


def fingerprint(token):
    return hashlib.md5(token.encode()).hexdigest()
