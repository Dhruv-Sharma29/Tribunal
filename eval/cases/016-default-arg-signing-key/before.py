import hmac
import hashlib


def sign(payload, key="s3cr3t-webhook-signing-key-2024"):
    """Return the hex HMAC-SHA256 signature for an outbound webhook payload."""
    return hmac.new(key.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify(payload, signature, key="s3cr3t-webhook-signing-key-2024"):
    """Constant-time check of an inbound signature."""
    return hmac.compare_digest(sign(payload, key), signature)
