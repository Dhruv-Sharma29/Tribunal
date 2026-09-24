"""The Profiler critic: performance and complexity.

Like the Red-team it never executes anything itself. The measurements it cites were produced by
`grounding/perf_t.py` through the sandbox, before this agent ran, and the citability rule is
enforced by `validation.py` rather than trusted to the prompt.
"""

from __future__ import annotations

from tribunal.agents.base import Critic
from tribunal.contracts import Critique, Dimension


class Profiler(Critic):
    role = "profiler"
    dimension = Dimension.PERFORMANCE
    output_model = Critique
