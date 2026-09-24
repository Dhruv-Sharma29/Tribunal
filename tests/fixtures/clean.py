"""Canary fixture: a correct, unremarkable module.

A critic that finds issues here is manufacturing them, which is the
"critique inflation" failure mode in docs/11-risks.md. The grounding
layer must report nothing on it, so that a critic's issues on this
file cannot be grounded in a tool finding.
"""

from __future__ import annotations


def normalise(name: str) -> str:
    """Strip surrounding whitespace and lowercase a display name."""
    return name.strip().lower()


def total(amounts: list[int]) -> int:
    """Sum a list of amounts."""
    return sum(amounts)
