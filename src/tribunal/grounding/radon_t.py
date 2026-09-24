"""radon -> GroundingFinding (cyclomatic complexity).

Two things worth knowing about this runner.

**Only rank C and worse becomes a per-function finding.** radon ranks every function, and a
clean file yields dozens of rank-A entries. Feeding those to a critic is pure noise, and noise
is what turns a grounded critic into a rubber stamp. radon's ranks: A = 1-5, B = 6-10,
C = 11-20, D = 21-30, E = 31-40, F = 41+.

**But the full table is always emitted as one summary finding.** The Profiler's characteristic
citation is a *delta* -- "cyclomatic complexity 7 -> 12" is the cost line in the worked
trade-off example in docs/04-arbitration.md. A delta from 7 to 12 crosses the B/C boundary, so
if only rank-C-and-worse functions were reported, the "before" side of that comparison would
not exist in the baseline report. The summary finding carries every function's complexity in
`raw`, at `line: None`, so the comparison is always available.
"""

from __future__ import annotations

import json

from tribunal.contracts import GroundingFinding, finding_id
from tribunal.grounding.base import GroundingTarget, GroundingTool

#: Worst-to-best, so `RANKS.index(...)` orders them.
RANKS = ("F", "E", "D", "C", "B", "A")

SUMMARY_RULE = "CC-SUMMARY"


class RadonTool(GroundingTool):
    name = "radon"

    def __init__(self, min_rank: str = "C") -> None:
        if min_rank not in RANKS:
            raise ValueError(f"min_rank must be one of {RANKS}, got {min_rank!r}")
        self.min_rank = min_rank

    def argv(self, target: GroundingTarget) -> list[str]:
        return [self.python(), "-m", "radon", "cc", "-j", "-s", str(target.path.name)]

    def accepts_exit_code(self, code: int) -> bool:
        return code == 0

    def parse(
        self, stdout: str, stderr: str, exit_code: int, target: GroundingTarget
    ) -> list[GroundingFinding]:
        payload = json.loads(stdout or "{}")
        blocks = self._flatten(payload, target)
        findings = [
            self._finding(block, target)
            for block in blocks
            if RANKS.index(block["rank"]) <= RANKS.index(self.min_rank)
        ]
        findings.append(self._summary(blocks, target))
        return findings

    def _flatten(self, payload: dict, target: GroundingTarget) -> list[dict]:
        """radon keys results by the path it was given and nests closures one level down."""
        out: list[dict] = []
        for key, entries in payload.items():
            if isinstance(entries, dict) and "error" in entries:
                raise RuntimeError(f"radon failed on {key}: {entries['error']}")
            for entry in entries:
                out.append(entry)
                for closure in entry.get("closures") or []:
                    out.append(closure)
        return out

    def _finding(self, block: dict, target: GroundingTarget) -> GroundingFinding:
        name = block.get("name", "<anonymous>")
        rule = f"CC-{block['rank']}"
        line = block.get("lineno")
        return GroundingFinding(
            id=finding_id(self.name, f"{rule}:{name}", target.logical_name, line),
            tool=self.name,
            rule=rule,
            file=target.logical_name,
            line=line,
            end_line=block.get("endline"),
            message=(
                f"{block.get('type', 'function')} {name!r} has cyclomatic complexity "
                f"{block['complexity']} (rank {block['rank']})"
            ),
            tool_severity=block["rank"],
            raw={
                k: block[k]
                for k in ("name", "type", "rank", "complexity", "lineno", "endline")
                if k in block
            },
        )

    def _summary(self, blocks: list[dict], target: GroundingTarget) -> GroundingFinding:
        table = sorted(
            (
                {"name": b.get("name"), "complexity": b["complexity"], "rank": b["rank"],
                 "line": b.get("lineno")}
                for b in blocks
            ),
            key=lambda row: (-row["complexity"], row["name"] or ""),
        )
        worst = table[0] if table else None
        total = sum(row["complexity"] for row in table)
        message = (
            f"{len(table)} block(s) measured; total complexity {total}; worst is "
            f"{worst['name']!r} at {worst['complexity']} (rank {worst['rank']})"
            if worst
            else "no measurable blocks"
        )
        return GroundingFinding(
            id=finding_id(self.name, SUMMARY_RULE, target.logical_name, None),
            tool=self.name,
            rule=SUMMARY_RULE,
            file=target.logical_name,
            line=None,
            end_line=None,
            message=message,
            # Not a defect, so it carries no rating. A critic citing this is citing a
            # measurement, which is what the before/after comparison needs.
            tool_severity=None,
            raw={"blocks": table, "total_complexity": total},
        )
