import hashlib
import tempfile


def session_path(session_id):
    """Return the on-disk path for a session's scratch file."""
    return tempfile.mktemp(prefix=f"session-{session_id}-")


def fingerprint(entries):
    """A stable fingerprint for a list of session entries."""
    digest = ""
    for entry in entries:
        digest = hashlib.sha256((digest + entry).encode("utf-8")).hexdigest()
    return digest
