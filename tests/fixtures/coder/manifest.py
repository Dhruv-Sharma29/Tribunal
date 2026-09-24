"""What each Coder fixture contains, and what a correct patch must touch.

docs/03-agents.md § 3.1 makes this the Coder's acceptance criterion: "6 fixture files with
known bugs -> assert the patch applies, the file parses, and the target line range is touched."

`target_lines` is the range a correct fix has to change. It is deliberately narrow: a patch that
applies and parses but rewrites an unrelated function has not fixed the bug, and asserting only
"it applies" would pass that. `rule` is the grounding finding a fix should resolve, so the test
can check `addresses` names something real rather than anything at all.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CoderFixture:
    filename: str
    #: 1-based inclusive range a correct patch must touch.
    target_lines: tuple[int, int]
    #: The tool rule a fix should clear, for the addresses check. `None` means no static tool
    #: flags this defect -- see 005 below.
    rule: str | None
    dimension: str
    summary: str

    @property
    def needs_supplied_issue(self) -> bool:
        """True when the Coder can only learn about the defect from a critic.

        No static tool flags loop string concatenation, so 005 has an empty finding list. That
        makes it the only fixture that exercises the round >= 2 path, where `addresses` names
        an `Issue.id` from the consolidated critique rather than a `GroundingFinding.id` --
        and it is the case where `addressable_ids()` would otherwise be empty.
        """
        return self.rule is None


FIXTURES: tuple[CoderFixture, ...] = (
    CoderFixture(
        "001-shell-injection.py", (6, 6), "B602", "security",
        "subprocess with shell=True on a concatenated argument",
    ),
    CoderFixture(
        "002-sql-injection.py", (4, 5), "B608", "security",
        "SQL built with %-formatting from a parameter",
    ),
    CoderFixture(
        "003-eval-config.py", (4, 5), "B307", "security",
        "eval() on caller-supplied text",
    ),
    CoderFixture(
        "004-quadratic-accumulate.py", (5, 8), "RUF005", "performance",
        "list rebuilt with `out = out + [row]` inside a loop",
    ),
    # No ruff or bandit rule covers loop string concatenation, so this fixture arrives with an
    # empty finding list. Kept deliberately: it is the Profiler's characteristic case (a
    # structural claim with no tool finding behind it) and the only fixture that drives the
    # round >= 2 Coder path.
    CoderFixture(
        "005-string-concat.py", (4, 7), None, "performance",
        "string rebuilt by concatenation inside a loop; no static tool flags it",
    ),
    CoderFixture(
        "006-weak-hash.py", (6, 6), "B324", "security",
        "md5 used to fingerprint a secret",
    ),
)
