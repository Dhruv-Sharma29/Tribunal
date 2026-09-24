"""Scoring a run against a case. Mechanical first, and the judge only where it is unavoidable.

docs/07-evaluation.md § Metrics: "Where mechanical checking is possible (M3, M4, M5, M6, M7,
M8), **do not use the judge at all.** Use the judge only where it's unavoidable." This module
is that boundary, drawn in code: nothing here makes an API call, and every number it produces
is reproducible from a trace.

| Metric | What it needs | Here? |
|---|---|---|
| M1 known-issue recall | `locator`, with a judge fallback for `description` | mechanical part |
| M2 regression rate | the judge (is a new issue real?) | no |
| M3 false positives on canaries | the reported issues | yes |
| M4 patch applies, parses, test passes | the report | yes |
| M5 outcome accuracy | the report | yes |
| M6 rounds to terminal | the report | yes |
| M7 cost | the report | yes |
| M8 re-rating rate | the critiques and the findings they cite | yes |

**M4 leads the results table** because it is the one number with no interpretation in it
(docs/07: "14/16 patches apply, parse, and pass the test, vs 9/16 for B1" is unarguable).

## What "caught" means, and why the order matters

A known issue counts as caught if a reported `Issue`:

1. cites a `GroundingFinding` whose rule is the locator's rule, or
2. has evidence whose line span intersects the locator's range, or
3. is matched to the issue's description by the judge.

Strictly in that order. The mechanical matches are free, deterministic and re-runnable from a
trace; the judge costs money and has to be validated before it can be trusted at all. An
implementation that asked the judge first would be both more expensive and less reproducible,
and would make the recall number depend on the judge's mood on the day.

`Scorecard.unmatched_known` and `.unclaimed_issues` are what the judge pass is given: the
known issues nothing mechanical could match, and the reported issues nothing claimed.
Everything else is settled before a judge is involved.

`score()` never calls anything. `apply_verdicts()` folds judge answers into a card that has
already been scored mechanically, which is what makes `--replay` able to re-judge a recorded
sweep without re-running the debate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tribunal.contracts import (
    Critique,
    Decision,
    EvidenceKind,
    GroundingFinding,
    Issue,
    JudgeVerdict,
    Report,
    Severity,
)
from tribunal.eval.case import EvalCase, KnownIssue

#: How a known issue was recognised. `none` means nothing mechanical matched it, which is
#: the judge's input rather than a verdict.
MatchKind = str  # "rule" | "line_range" | "none"


@dataclass(frozen=True)
class Match:
    """One known issue, and what (if anything) found it."""

    key: str
    how: MatchKind
    issue_id: str | None = None

    @property
    def caught(self) -> bool:
        return self.how != "none"

    @property
    def mechanical(self) -> bool:
        """Whether this was settled without a judge. Reported alongside M1 so a reader can
        see how much of the recall number rests on the judge's reliability."""
        return self.how in ("rule", "line_range")


@dataclass
class Scorecard:
    """Everything one (case, arm) pair contributes to the results table.

    Deliberately counts rather than rates. docs/07: "Report counts (`14/16`), not percentages
    to one decimal ... Nothing damages an eval's credibility faster than `87.4%` from 16
    samples." The aggregation keeps them as counts too; only the report renderer divides.
    """

    case_id: str
    arm: str
    matches: list[Match] = field(default_factory=list)
    #: M4, three separate booleans rather than one: a patch that applies but fails the test
    #: is a different failure from one that never applied, and collapsing them hides which.
    patch_applied: bool = False
    patch_parses: bool = False
    test_passes: bool | None = None
    #: M5
    expected_outcome: Decision | None = None
    actual_outcome: Decision | None = None
    #: M3 -- only meaningful on a canary-clean case.
    false_positives: int = 0
    reported_issues: int = 0
    #: M6, M7
    rounds_used: int = 0
    cost_usd: float = 0.0
    #: M8
    re_rated: int = 0
    comparable_to_tool: int = 0
    #: M2. None until the judge has run -- distinct from 0, which would claim the run
    #: introduced nothing.
    regressions: int | None = None
    #: Issues nobody could match mechanically. The judge's queue, not a verdict.
    unmatched_known: list[str] = field(default_factory=list)
    unclaimed_issues: list[str] = field(default_factory=list)

    @property
    def known_total(self) -> int:
        return len(self.matches)

    @property
    def known_caught(self) -> int:
        return sum(1 for m in self.matches if m.caught)

    @property
    def known_caught_mechanically(self) -> int:
        """The part of M1 that owes nothing to the judge."""
        return sum(1 for m in self.matches if m.mechanical)

    @property
    def fix_correct(self) -> bool:
        """M4. `test_passes is None` means the case ships no test, so the two mechanical
        checks that exist are the whole answer."""
        return self.patch_applied and self.patch_parses and self.test_passes is not False

    @property
    def outcome_correct(self) -> bool | None:
        """M5. None when the arm has no representation for the expected outcome at all --
        docs/07: report that as a capability difference, not as a score of zero."""
        if self.expected_outcome is None or self.actual_outcome is None:
            return None
        return self.expected_outcome is self.actual_outcome


# -- M1: did the run find what the case says is there? -----------------------------------


def match_known_issue(
    known: KnownIssue, issues: list[Issue], findings: list[GroundingFinding]
) -> Match:
    """Mechanical match only. `how == "none"` hands the question to the judge."""
    by_id = {finding.id: finding for finding in findings}

    if known.locator.kind == "rule":
        rule = str(known.locator.value)
        for issue in issues:
            for evidence in issue.evidence:
                if evidence.kind is not EvidenceKind.TOOL_FINDING:
                    continue
                finding = by_id.get(evidence.ref)
                if finding is not None and finding.rule == rule:
                    return Match(key=known.key, how="rule", issue_id=issue.id)

    if known.locator.kind == "line_range":
        start, end = known.locator.value  # type: ignore[misc]
        for issue in issues:
            if any(s <= end and start <= e for s, e in issue.spans):
                return Match(key=known.key, how="line_range", issue_id=issue.id)
            # A tool finding carries a span the issue itself does not.
            for evidence in issue.evidence:
                finding = by_id.get(evidence.ref)
                span = finding.span if finding is not None else None
                if span is not None and span[0] <= end and start <= span[1]:
                    return Match(key=known.key, how="line_range", issue_id=issue.id)

    return Match(key=known.key, how="none")


# -- M8: is this a linter wrapper? ---------------------------------------------------------

#: Copied from `agents/validation.py`, which computes the same divergence per critique for the
#: trace. Imported rather than redefined so the eval and the live metric cannot disagree.
def _tool_equivalent(finding: GroundingFinding) -> Severity | None:
    from tribunal.agents.validation import TOOL_SEVERITY_EQUIVALENT

    if finding.tool_severity is None:
        return None
    return TOOL_SEVERITY_EQUIVALENT.get(finding.tool_severity)


def re_rating(issues: list[Issue], findings: list[GroundingFinding]) -> tuple[int, int]:
    """`(re-rated, comparable)`. docs/03: the answer to "isn't this just a linter wrapper?"

    Only issues citing a tool finding that *has* a severity of its own are comparable — a
    `code_span` issue has nothing to diverge from, and counting it either way would make the
    rate a function of how often critics find novel issues rather than of how often they
    disagree with a tool.
    """
    by_id = {finding.id: finding for finding in findings}
    re_rated = comparable = 0
    for issue in issues:
        for evidence in issue.evidence:
            finding = by_id.get(evidence.ref)
            if finding is None:
                continue
            equivalent = _tool_equivalent(finding)
            if equivalent is None:
                continue
            comparable += 1
            if equivalent is not issue.severity:
                re_rated += 1
            break
    return re_rated, comparable


# -- putting a scorecard together ------------------------------------------------------------

#: docs/07 M3: "HIGH/MEDIUM issues on canary-clean cases". A `low` note on correct code is
#: defensible; a `medium` is the inflation the canary exists to catch.
FALSE_POSITIVE_FLOOR = Severity.MEDIUM


def score(
    case: EvalCase,
    arm: str,
    report: Report,
    critiques: list[Critique],
    findings: list[GroundingFinding],
    test_passes: bool | None = None,
) -> Scorecard:
    """One run, scored mechanically. No API calls, reproducible from a trace."""
    issues = [issue for critique in critiques for issue in critique.issues]
    matches = [match_known_issue(known, issues, findings) for known in case.known_issues]
    claimed = {match.issue_id for match in matches if match.issue_id is not None}
    re_rated, comparable = re_rating(issues, findings)

    card = Scorecard(
        case_id=case.id,
        arm=arm,
        matches=matches,
        patch_applied=report.accepted_diff is not None,
        patch_parses=report.accepted_diff is not None,
        test_passes=test_passes,
        expected_outcome=case.meta.expected_outcome,
        actual_outcome=report.outcome,
        reported_issues=len(issues),
        rounds_used=report.rounds_used,
        cost_usd=report.total_cost_usd,
        re_rated=re_rated,
        comparable_to_tool=comparable,
        unmatched_known=[m.key for m in matches if not m.caught],
        unclaimed_issues=[i.id for i in issues if i.id not in claimed],
    )
    if case.meta.category == "canary_clean":
        card.false_positives = sum(
            1 for issue in issues if issue.severity.weight >= FALSE_POSITIVE_FLOOR.weight
        )
    return card


# -- applying the judge -------------------------------------------------------------------


def apply_verdicts(
    card: Scorecard, verdicts: dict[str, tuple[str, JudgeVerdict]]
) -> Scorecard:
    """Fold judge answers into a mechanically-scored card. Pure; no calls.

    `verdicts` maps a blind label to `(issue_id, verdict)`. Two things happen:

    **M1's description fallback.** A known issue nothing matched mechanically is caught if
    the judge says a reported issue is that issue. Recorded as `how="judge"`, so the
    mechanical share of the recall number stays visible -- docs/07 prefers the mechanical
    match and a table that hid which was which would be claiming more precision than it has.

    **M2.** Regressions are counted from the judge's `is_regression`, which is the only
    question here nothing mechanical can answer.

    Separated from `score` rather than folded into it because the whole point of `--replay`
    is that this step can be re-run over recorded runs when the judge's prompt changes,
    without paying for the debate again.
    """
    matched: dict[str, tuple[str, JudgeVerdict]] = {}
    for _label, (issue_id, verdict) in verdicts.items():
        key = verdict.matched_known_issue
        if key is None:
            continue
        # First verdict wins, so re-running the judge with a different prompt cannot make a
        # known issue "caught twice" and quietly change the denominator.
        matched.setdefault(key, (issue_id, verdict))

    card.matches = [
        Match(key=m.key, how="judge", issue_id=matched[m.key][0])
        if not m.caught and m.key in matched
        else m
        for m in card.matches
    ]
    card.unmatched_known = [m.key for m in card.matches if not m.caught]
    card.regressions = sum(
        1 for _label, (_id, verdict) in verdicts.items()
        if verdict.is_real_issue and verdict.is_regression
    )
    return card


# -- aggregation ------------------------------------------------------------------------------


@dataclass
class ArmTotals:
    """One column of the results table. Counts, never rates."""

    arm: str
    runs: int = 0
    fix_correct: int = 0
    known_caught: int = 0
    known_total: int = 0
    outcome_correct: int = 0
    outcome_scored: int = 0
    false_positives: int = 0
    re_rated: int = 0
    comparable_to_tool: int = 0
    #: M1's mechanical share, so a reader can see how much rests on the judge.
    known_caught_mechanically: int = 0
    #: M2. None while no card has been judged; 0 means "judged, and none found".
    regressions: int | None = None
    costs: list[float] = field(default_factory=list)
    rounds: list[int] = field(default_factory=list)

    def percentile(self, values: list[float] | list[int], fraction: float) -> float:
        """Nearest-rank, because with n=16 an interpolated percentile is a fiction."""
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, round(fraction * len(ordered)) - 1))
        return float(ordered[index])

    @property
    def cost_p50(self) -> float:
        return self.percentile(self.costs, 0.5)

    @property
    def cost_p95(self) -> float:
        return self.percentile(self.costs, 0.95)

    @property
    def rounds_p50(self) -> float:
        return self.percentile(self.rounds, 0.5)


def totals(cards: list[Scorecard]) -> dict[str, ArmTotals]:
    out: dict[str, ArmTotals] = {}
    for card in cards:
        arm = out.setdefault(card.arm, ArmTotals(arm=card.arm))
        arm.runs += 1
        arm.fix_correct += int(card.fix_correct)
        arm.known_caught += card.known_caught
        arm.known_total += card.known_total
        correct = card.outcome_correct
        if correct is not None:
            arm.outcome_scored += 1
            arm.outcome_correct += int(correct)
        arm.false_positives += card.false_positives
        arm.re_rated += card.re_rated
        arm.comparable_to_tool += card.comparable_to_tool
        arm.known_caught_mechanically += card.known_caught_mechanically
        if card.regressions is not None:
            arm.regressions = (arm.regressions or 0) + card.regressions
        arm.costs.append(card.cost_usd)
        arm.rounds.append(card.rounds_used)
    return out


#: docs/07: "with n=16, a difference of one case is ~6 points ... state 'n=16, differences
#: under ~3 cases are not distinguishable.'" Stated as a function so the results renderer
#: cannot forget it.
def resolution_caveat(n: int) -> str:
    if n == 0:
        return "no runs"
    points = 100.0 / n
    return (
        f"n={n}; one case is ~{points:.0f}pp, so differences under ~3 cases are not "
        "distinguishable."
    )
