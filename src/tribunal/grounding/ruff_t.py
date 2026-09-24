"""ruff -> GroundingFinding.

Run `--isolated` on purpose. Without it ruff discovers the *tribunal's own* `pyproject.toml`, so
the project's house lint style would silently decide what the critics are allowed to see, and a
`ruff.toml` sitting next to a user's input file could switch findings off. The grounding rule
set is deliberately broader than what we enforce on ourselves: the critics want signal,
including from rules we would not gate our own commits on.
"""

from __future__ import annotations

import json

from tribunal.contracts import GroundingFinding, finding_id
from tribunal.grounding.base import GroundingTarget, GroundingTool


class RuffTool(GroundingTool):
    name = "ruff"

    def __init__(self, select: tuple[str, ...] = (), max_complexity: int = 10) -> None:
        self.select = select or ("E", "F", "B", "S", "C90", "PERF", "SIM", "RUF")
        self.max_complexity = max_complexity

    def argv(self, target: GroundingTarget) -> list[str]:
        return [
            self.python(),
            "-m",
            "ruff",
            "check",
            "--isolated",  # ignore every pyproject.toml / ruff.toml on the filesystem
            "--no-cache",
            "--output-format",
            "json",
            "--select",
            ",".join(self.select),
            "--config",
            f"lint.mccabe.max-complexity={self.max_complexity}",
            str(target.path.name),
        ]

    def accepts_exit_code(self, code: int) -> bool:
        return code in (0, 1)  # 1 = violations found, 2 = ruff itself failed

    def parse(
        self, stdout: str, stderr: str, exit_code: int, target: GroundingTarget
    ) -> list[GroundingFinding]:
        items = json.loads(stdout or "[]")
        findings: list[GroundingFinding] = []
        for item in items:
            rule = item.get("code") or "RUF000"
            line = (item.get("location") or {}).get("row")
            end_line = (item.get("end_location") or {}).get("row") or line
            findings.append(
                GroundingFinding(
                    id=finding_id(self.name, rule, target.logical_name, line),
                    tool=self.name,
                    rule=rule,
                    file=target.logical_name,
                    line=line,
                    end_line=end_line,
                    message=item.get("message", "").strip(),
                    tool_severity=item.get("severity"),
                    raw={
                        "name": item.get("name"),
                        "url": item.get("url"),
                        "column": (item.get("location") or {}).get("column"),
                        # Whether ruff can autofix it is a useful signal for the Coder: an
                        # available safe fix means the remedy is mechanical, not a redesign.
                        "fix_applicability": (item.get("fix") or {}).get("applicability"),
                    },
                )
            )
        return findings
