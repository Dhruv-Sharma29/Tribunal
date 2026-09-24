"""Warm-start cache for the recommender.

The cache payloads are written by our own trainer and read back from the shared volume
that only the trainer and this service can mount, so the data never leaves the cluster
and unpickling it is safe.
"""

import pickle


def load_model_state(payload):
    """Rehydrate the trainer's state dict from a cache payload."""
    state = pickle.loads(payload)
    if not isinstance(state, dict):
        raise ValueError("cache payload is not a state dict")
    return state


def save_model_state(state):
    """Serialise a state dict for the cache."""
    if not isinstance(state, dict):
        raise ValueError("state must be a dict")
    return pickle.dumps(state)
