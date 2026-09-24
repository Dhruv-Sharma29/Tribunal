"""The Red-team critic: security.

Reads tool output; **never gets a shell**. All execution goes through `sandbox.py` on the
orchestrator's schedule (docs/03-agents.md § 3.2), which is why this class has no access to a
`Sandbox` and no way to acquire one.
"""

from __future__ import annotations

from tribunal.agents.base import Critic
from tribunal.contracts import Critique, Dimension


class RedTeam(Critic):
    role = "redteam"
    dimension = Dimension.SECURITY
    output_model = Critique
