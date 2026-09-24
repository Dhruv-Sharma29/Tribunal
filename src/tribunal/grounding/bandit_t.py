"""bandit -> GroundingFinding.

`tool_severity` carries bandit's own HIGH/MEDIUM/LOW verbatim and is deliberately *not* mapped
onto our `Severity` enum. Mapping bandit's HIGH straight onto ours would make the Red-team
redundant: the point is that the agent re-rates severity in context (is this input actually
attacker-controlled?). Keeping both lets the eval measure how often the agent diverges from the
tool, which is the answer to "isn't this just a linter wrapper?" (docs/02-contracts.md
§ Grounding models).
"""

from __future__ import annotations

import json

from tribunal.contracts import GroundingFinding, finding_id
from tribunal.grounding.base import GroundingTarget, GroundingTool


class BanditTool(GroundingTool):
    name = "bandit"

    def argv(self, target: GroundingTarget) -> list[str]:
        return [
            self.python(),
            "-m",
            "bandit",
            "--format",
            "json",
            "--quiet",
            # No config discovery: the tribunal's own pyproject must not change what the critics
            # see, and a user's stray .bandit file must not silence findings.
            "--exit-zero",
            str(target.path.name),
        ]

    def accepts_exit_code(self, code: int) -> bool:
        return code == 0  # --exit-zero, so anything else is a real failure

    def parse(
        self, stdout: str, stderr: str, exit_code: int, target: GroundingTarget
    ) -> list[GroundingFinding]:
        payload = json.loads(stdout or "{}")
        findings: list[GroundingFinding] = []
        for item in payload.get("results", []):
            line = item.get("line_number")
            line_range = item.get("line_range") or ([line] if line else [])
            rule = item.get("test_id", "B000")
            findings.append(
                GroundingFinding(
                    id=finding_id(self.name, rule, target.logical_name, line),
                    tool=self.name,
                    rule=rule,
                    file=target.logical_name,
                    line=line,
                    end_line=max(line_range) if line_range else line,
                    message=item.get("issue_text", "").strip(),
                    tool_severity=item.get("issue_severity"),
                    raw={
                        "test_name": item.get("test_name"),
                        "issue_severity": item.get("issue_severity"),
                        "issue_confidence": item.get("issue_confidence"),
                        "cwe": (item.get("issue_cwe") or {}).get("id"),
                        "more_info": item.get("more_info"),
                        "code": item.get("code"),
                    },
                )
            )
        # bandit reports its own crashes in-band; surface them instead of dropping them.
        errors = payload.get("errors") or []
        if errors:
            raise RuntimeError(f"bandit reported errors: {errors}")
        return findings
