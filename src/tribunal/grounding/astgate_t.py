"""The AST gate, wrapped as a grounding tool.

It has no subprocess, so it does not subclass `GroundingTool`; the suite calls it directly.
Present as a separate module so that the suite's tool list reads uniformly and so that the
gate's findings are attributed to `tool: "astgate"` in the trace like any other source.
"""

from __future__ import annotations

import time

from tribunal import astgate
from tribunal.grounding.base import GroundingTarget, ToolOutcome


class AstGateTool:
    name = astgate.TOOL
    executes_target = False

    def argv(self, target: GroundingTarget) -> list[str]:
        return ["<in-process ast scan>", target.logical_name]

    async def run(self, target: GroundingTarget, timeout: int = 60) -> ToolOutcome:
        started = time.monotonic()
        findings = astgate.findings(target.source, target.logical_name)
        return ToolOutcome(
            tool=self.name,
            argv=self.argv(target),
            exit_code=0,
            findings=findings,
            duration_ms=int((time.monotonic() - started) * 1000),
            stdout_excerpt=f"{len(findings)} hit(s)",
        )
