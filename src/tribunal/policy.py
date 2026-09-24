"""The deterministic decision layer. Pure functions only -- no I/O, no LLM, no clock.

This is Rule 1 of docs/01-architecture.md: **the decision layer contains no LLM.** The Arbiter
agent synthesises and explains; it does not decide. That is what makes charter criterion S3
achievable at all -- the outcome is reproducible from the trace, exhaustively unit-testable,
and free. It also removes the classic multi-agent failure where a supervisor LLM is talked into
accepting a bad patch by a confident worker.

Two structural choices follow from that:

**The decision table is a table.** docs/04-arbitration.md specifies twelve ordered rows,
first match wins, and the matched row's name goes into `Verdict.rule_fired`. It is written here
as an ordered tuple of `Rule` objects rather than a chain of `if`s, so "one test per row plus a
no-fall-through test" is mechanical rather than aspirational, and so a new row cannot be added
without appearing in the table.

**Nothing here reads the world.** `decide()` takes a value and returns a value. `Spend` is
passed in rather than measured, test results are passed in rather than run. That is what makes
`tribunal replay` reproduce a verdict byte-for-byte from a trace with zero API calls
(criterion S6).

## `rule_fired` is the point

Every terminal state in every trace is explainable by one string. That is the single most
useful field in the system for debugging and for demoing, and it exists because the table has
names rather than because something logs them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from functools import cached_property

from tribunal.config import BudgetConfig, PolicyConfig
from tribunal.contracts import (
    Conflict,
    Critique,
    Decision,
    Dimension,
    Issue,
    Severity,
    TestResult,
    Verdict,
)

#: Dimensions a critic is responsible for. A dimension not represented in this round's
#: critiques -- and not reported as errored -- was never assessed at all, which is a different
#: thing from a critic that ran and found nothing.
CRITIC_DIMENSIONS = (Dimension.SECURITY, Dimension.PERFORMANCE)


@dataclass(frozen=True)
class Spend:
    """Budget consumed so far. Measured by the orchestrator, passed in as a value."""

    usd: float = 0.0
    tokens: int = 0
    wall_seconds: float = 0.0
    rounds: int = 0

    def exceeds(self, budget: BudgetConfig) -> str | None:
        """Which cap was breached, or None. Named so `rule_fired` can carry the reason."""
        if self.usd >= budget.max_usd:
            return f"usd {self.usd:.2f} >= {budget.max_usd:.2f}"
        if self.tokens >= budget.max_tokens:
            return f"tokens {self.tokens} >= {budget.max_tokens}"
        if self.wall_seconds >= budget.max_wall_seconds:
            return f"wall {self.wall_seconds:.0f}s >= {budget.max_wall_seconds}s"
        return None


@dataclass(frozen=True)
class PolicyInput:
    """Everything the decision needs about the run so far.

    Frozen, and built from values the trace already carries, so a replay reconstructs it
    exactly. Anything that would require calling out -- running a test, pricing a token,
    reading a clock -- is a field here, not a call inside `decide`.
    """

    round: int
    #: Critiques that parsed and validated this round.
    critiques: tuple[Critique, ...] = ()
    #: Dimensions whose critic failed. NOT the same as a critic that found nothing.
    errored_dimensions: tuple[Dimension, ...] = ()
    #: False when the *original* file does not parse.
    input_parses: bool = True
    #: Whether this round produced a patch that applied and parsed.
    patch_applied: bool = True
    #: True when VALIDATE burned every attempt without a patch applying.
    patch_attempts_exhausted: bool = False
    #: True when the Coder deliberately proposed no change.
    patch_is_empty: bool = False
    #: `patch.normalised_diff_sha256` of this round's diff.
    diff_hash: str | None = None
    #: Normalised hashes from *earlier* rounds only.
    previous_diff_hashes: tuple[str, ...] = ()
    #: Issue ids the Arbiter has agreed are false positives. Permanently out of the sum.
    dismissed_issue_ids: frozenset[str] = frozenset()
    #: Pressure from *previous* rounds, oldest first. Empty on round 1.
    pressure_history: tuple[float, ...] = ()
    #: The user's test on the original file, and on the patched file.
    baseline_tests: TestResult | None = None
    patched_tests: TestResult | None = None
    spend: Spend = Spend()
    #: A conflict that has been *detected and affirmed*. Policy does not detect it here;
    #: `detect_conflict` does, and detector 2 additionally requires the Arbiter to affirm.
    conflict: Conflict | None = None


@dataclass
class Context:
    """Derived values, computed once and shared by every rule.

    A dataclass rather than locals so a rule's condition stays a one-line predicate and the
    table reads as a table.
    """

    state: PolicyInput
    config: PolicyConfig
    budget: BudgetConfig

    @cached_property
    def open_issues(self) -> tuple[Issue, ...]:
        """Issues that count, after dismissals and duplicate collapsing."""
        live = [
            issue
            for critique in self.state.critiques
            for issue in critique.issues
            if issue.id not in self.state.dismissed_issue_ids
        ]
        return tuple(_collapse_duplicates(live))

    @cached_property
    def pressure(self) -> float:
        return round(sum(issue.score for issue in self.open_issues), 6)

    @cached_property
    def unassessed(self) -> tuple[Dimension, ...]:
        """Dimensions with no usable critique this round.

        Both an errored critic and a dimension that simply never ran. A naive implementation
        sums the surviving critiques, gets low pressure, and ships -- docs/04-arbitration.md
        calls that "the single most likely real bug in any parallel-critic system".
        """
        assessed = {critique.dimension for critique in self.state.critiques}
        missing = [d for d in CRITIC_DIMENSIONS if d not in assessed]
        return tuple(dict.fromkeys([*self.state.errored_dimensions, *missing]))

    @cached_property
    def rounds_remain(self) -> bool:
        return self.state.round < self.config.max_rounds

    @cached_property
    def full_history(self) -> tuple[float, ...]:
        return (*self.state.pressure_history, self.pressure)


# --------------------------------------------------------------------------------------------
# Duplicate collapsing
# --------------------------------------------------------------------------------------------


def _collapse_duplicates(issues: Iterable[Issue]) -> list[Issue]:
    """Collapse issues that are the same defect reported twice within one dimension.

    **An addition to docs/04-arbitration.md's pressure formula, with a measured reason.** The
    first live Red-team run emitted the same `shell=True` defect as two issues -- one citing
    bandit's finding, one citing ruff's -- and pressure came out at 45.6 on a file with one
    bug. Two tools agreeing is the *strongest* evidence available, so letting it double the
    score inverts the signal.

    Deliberately conservative, in two ways:

    * **Within a dimension only.** The Red-team and the Profiler both flagging the same line is
      genuine cross-dimension disagreement -- very possibly the conflict that produces a
      TRADEOFF -- and collapsing that would delete the project's headline feature.
    * **Same evidence, not same topic.** Two issues collapse only when they cite a common
      grounding ref. Similar titles are not enough; a critic can legitimately raise two
      distinct concerns about one line.

    The survivor is the higher-scoring of the pair, so collapsing never lowers the ceiling on
    a genuine finding.
    """
    survivors: list[Issue] = []
    for issue in sorted(issues, key=lambda i: (-i.score, i.id)):
        refs = {e.ref for e in issue.evidence if e.is_grounded}
        duplicate = any(
            other.dimension is issue.dimension
            and refs & {e.ref for e in other.evidence if e.is_grounded}
            for other in survivors
        )
        if not duplicate:
            survivors.append(issue)
    return sorted(survivors, key=lambda i: i.id)


def duplicates_in(critiques: Sequence[Critique]) -> list[str]:
    """Issue ids that duplicate-collapsing removes. For the trace, so it is visible."""
    live = [issue for critique in critiques for issue in critique.issues]
    kept = {issue.id for issue in _collapse_duplicates(live)}
    return sorted(issue.id for issue in live if issue.id not in kept)


def pressure(
    critiques: Sequence[Critique], dismissed: frozenset[str] = frozenset()
) -> float:
    """Sum of `Issue.score` over open issues. The scalar the whole loop turns on.

    Three properties matter (docs/04-arbitration.md § Pressure): monotone in severity, so a
    HIGH counts 16x a LOW; grounding-weighted, so an ungrounded issue counts half and can
    block but cannot dominate; and dismissal-aware, so a false positive the Arbiter threw out
    leaves the sum permanently.
    """
    live = [
        issue
        for critique in critiques
        for issue in critique.issues
        if issue.id not in dismissed
    ]
    return round(sum(issue.score for issue in _collapse_duplicates(live)), 6)


# --------------------------------------------------------------------------------------------
# Row conditions
# --------------------------------------------------------------------------------------------


def _input_unusable(ctx: Context) -> bool:
    return not ctx.state.input_parses or ctx.state.patch_attempts_exhausted


def _correctness_regression(ctx: Context) -> bool:
    """Passed before, fails after. Correctness dominates everything below it.

    The condition is deliberately *not* "a test is failing now": a test that was already
    failing is the bug we were asked to fix, and failing again is a different signal handled
    as a normal CORRECTNESS issue.
    """
    return bool(regressed_tests(ctx.state.baseline_tests, ctx.state.patched_tests))


def _budget_exhausted(ctx: Context) -> bool:
    return ctx.state.spend.exceeds(ctx.budget) is not None


def _oscillation(ctx: Context) -> bool:
    """The same normalised diff proposed in an earlier round: A -> B -> A."""
    return (
        ctx.state.diff_hash is not None
        and ctx.state.diff_hash in ctx.state.previous_diff_hashes
    )


def _unassessed_dimension(ctx: Context) -> bool:
    return bool(ctx.unassessed) and ctx.rounds_remain


def _unassessed_terminal(ctx: Context) -> bool:
    return bool(ctx.unassessed)


def _hard_block(ctx: Context) -> bool:
    """Asymmetric by design: a security HIGH auto-rejects, a performance HIGH does not.

    An exploitable vulnerability has unbounded downside and a wrong rejection costs one more
    round; a performance regression is recoverable and often intentional. Asymmetric
    thresholds are a statement of values, and stating them in config beats pretending the
    system is neutral.
    """
    return any(
        issue.dimension is rule.dimension
        and issue.severity is rule.severity
        and issue.confidence >= rule.min_confidence
        for issue in ctx.open_issues
        for rule in ctx.config.hard_block
    )


def _irreconcilable(ctx: Context) -> bool:
    """A detected, affirmed conflict whose two sides are both grounded and both still open.

    Re-checked here rather than trusted: a spurious TRADEOFF ships a bad patch with an elegant
    justification attached, which docs/11-risks.md R4 calls the worst possible output.
    """
    conflict = ctx.state.conflict
    if conflict is None:
        return False
    by_id = {issue.id: issue for issue in ctx.open_issues}
    left, right = by_id.get(conflict.left_issue), by_id.get(conflict.right_issue)
    return left is not None and right is not None and left.grounded and right.grounded


def _no_progress(ctx: Context) -> bool:
    """Two consecutive rounds without meaningful improvement.

    Two, not one: the Coder fixes the HIGH and the critics, now unblocked, find MEDIUMs they
    had not reached, so pressure can legitimately rise in round 2.

    The subtle bug docs/04 warns about is treating a missing previous value as 0 and
    escalating on round 1. There is no previous value on round 1, so there is nothing to
    compare and the rule cannot fire -- expressed as a length check, not a default.
    """
    history = ctx.full_history
    if len(history) < 3:
        return False
    epsilon = ctx.config.no_progress_epsilon
    recent = history[-3:]
    # strict=False on purpose: `recent[1:]` is one shorter than `recent`, which is the whole
    # point of pairing a window with its own tail.
    pairs = zip(recent, recent[1:], strict=False)
    return all(later > earlier - epsilon for earlier, later in pairs)


def _rounds_exhausted(ctx: Context) -> bool:
    return ctx.state.round >= ctx.config.max_rounds and ctx.pressure > ctx.config.accept_threshold


def _pressure_over_threshold(ctx: Context) -> bool:
    """The accept threshold is not zero, on purpose.

    4.0 admits one full-confidence MEDIUM, or four LOWs. A system that requires zero open
    issues never terminates on real code, and pretending otherwise produces a demo that only
    works on toys. Open issues at accept time are reported, not hidden.
    """
    return ctx.pressure > ctx.config.accept_threshold


# --------------------------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    name: str
    decision: Decision
    condition: Callable[[Context], bool]
    why: str


#: docs/04-arbitration.md § The decision table. Evaluated top to bottom, **first match wins**,
#: and the matched row's `name` becomes `Verdict.rule_fired`. Order is the specification: row 2
#: sits above row 7 because a patch that fixes a security hole and breaks the tests is not a
#: trade-off, it is broken.
DECISION_TABLE: tuple[Rule, ...] = (
    Rule("input_unusable", Decision.ESCALATE, _input_unusable,
         "the original file does not parse, or no diff ever applied"),
    Rule("correctness_regression", Decision.REJECT, _correctness_regression,
         "a test that passed on the original fails on the patched file"),
    Rule("budget_exhausted", Decision.ESCALATE, _budget_exhausted,
         "a token, dollar or wall-clock cap was reached"),
    Rule("oscillation", Decision.ESCALATE, _oscillation,
         "this exact change was already proposed in an earlier round"),
    Rule("unassessed_dimension", Decision.REJECT, _unassessed_dimension,
         "a critic did not report, and rounds remain to re-run it"),
    Rule("unassessed_terminal", Decision.ESCALATE, _unassessed_terminal,
         "a critic did not report on the final round"),
    Rule("hard_block_security", Decision.REJECT, _hard_block,
         "an open high-severity security issue above the confidence floor"),
    Rule("irreconcilable", Decision.TRADEOFF, _irreconcilable,
         "two grounded issues whose remedies exclude each other"),
    Rule("no_progress", Decision.ESCALATE, _no_progress,
         "two consecutive rounds without meaningful improvement"),
    Rule("rounds_exhausted", Decision.ESCALATE, _rounds_exhausted,
         "the round budget is spent and pressure is still above threshold"),
    Rule("pressure_over_threshold", Decision.REJECT, _pressure_over_threshold,
         "open issues outweigh the accept threshold"),
    #: The catch-all. Its condition is unconditionally true, which is what makes
    #: fall-through structurally impossible rather than merely untested.
    Rule("accept", Decision.ACCEPT, lambda _ctx: True,
         "nothing above matched"),
)


def decide(
    state: PolicyInput,
    config: PolicyConfig | None = None,
    budget: BudgetConfig | None = None,
) -> Verdict:
    """The verdict. Pure: same input, same output, no clock and no network."""
    ctx = Context(state=state, config=config or PolicyConfig(), budget=budget or BudgetConfig())
    rule = next(rule for rule in DECISION_TABLE if rule.condition(ctx))

    conflict = state.conflict if rule.decision is Decision.TRADEOFF else None
    unassessed = list(ctx.unassessed)
    if rule.decision is Decision.ACCEPT and unassessed:  # pragma: no cover - unreachable
        # Rows 5 and 6 catch every unassessed case before `accept` can be reached, and the
        # `Verdict` schema refuses the combination besides. Belt and braces, because this is
        # the failure the whole distinction exists to prevent.
        raise AssertionError(f"accept reached with unassessed dimensions: {unassessed}")

    return Verdict(
        round=state.round,
        decision=rule.decision,
        rule_fired=rule.name,
        pressure=ctx.pressure,
        pressure_history=list(ctx.full_history),
        open_issues=[issue.id for issue in ctx.open_issues],
        unassessed_dimensions=unassessed,
        conflict=conflict,
    )


def explain(verdict: Verdict) -> str:
    """One line a human can read, built from the table rather than from a second copy."""
    rule = next((r for r in DECISION_TABLE if r.name == verdict.rule_fired), None)
    why = rule.why if rule else "unknown rule"
    return (
        f"r{verdict.round} {verdict.decision.value} {verdict.rule_fired} "
        f"pressure={verdict.pressure:g} — {why}"
    )


# --------------------------------------------------------------------------------------------
# Correctness oracle
# --------------------------------------------------------------------------------------------


def regressed_tests(before: TestResult | None, after: TestResult | None) -> list[str]:
    """Tests that passed before the patch and fail after it. Row 2's exact condition.

    Returns [] when either side did not run: an absent oracle is not evidence of a regression.
    """
    if before is None or after is None or not before.ran or not after.ran:
        return []
    was_failing = set(before.failed_node_ids)
    return sorted(node for node in after.failed_node_ids if node not in was_failing)


# --------------------------------------------------------------------------------------------
# Conflict detection
# --------------------------------------------------------------------------------------------

#: A conflict needs both sides at least this severe. A trade-off over two LOWs is not a
#: trade-off, it is noise with a ceremony attached.
CONFLICT_MIN_SEVERITY = Severity.MEDIUM


@dataclass(frozen=True)
class RoundRecord:
    """What detector 1 needs to remember about a past round."""

    round: int
    issue_ids: frozenset[str]
    #: Complexity or measured-latency regression observed beyond noise in that round.
    regression_observed: bool = False


def _axis_for(left: Issue, right: Issue) -> str:
    dimensions = {left.dimension, right.dimension}
    if dimensions == {Dimension.SECURITY, Dimension.PERFORMANCE}:
        return "security_vs_performance"
    if Dimension.PERFORMANCE in dimensions:
        return "clarity_vs_performance"
    return "other"


def _eligible(issue: Issue) -> bool:
    return issue.grounded and issue.severity.weight >= CONFLICT_MIN_SEVERITY.weight


def same_span_candidates(critiques: Sequence[Critique]) -> list[tuple[Issue, Issue]]:
    """Every cross-dimension pair detector 2 would consider, cheapest filter first.

    Split out from `detect_same_span_conflict` so the Arbiter can be asked about a *specific*
    pair rather than about the round in general. The mechanical half of the detector --
    grounded, MEDIUM+, overlapping spans, opposite dimensions -- is free and deterministic,
    and it is what keeps the affirmation call rare: no candidate, no call.

    Ordered by `(left.id, right.id)` so the pair the Arbiter is asked about does not depend on
    which critic happened to return first.
    """
    eligible = [i for c in critiques for i in c.issues if _eligible(i)]
    pairs = [
        (left, right)
        for left in eligible
        for right in eligible
        if left.id < right.id
        and left.dimension is not right.dimension
        and _spans_overlap(left, right)
    ]
    return sorted(pairs, key=lambda pair: (pair[0].id, pair[1].id))


def same_span_conflict(
    left: Issue,
    right: Issue,
    left_remedy_cost: str | None = None,
    right_remedy_cost: str | None = None,
) -> Conflict:
    """Build the `Conflict` for an affirmed pair.

    The remedy costs default to each issue's `suggested_direction`, which is what a run with
    no Arbiter can say. An affirming Arbiter supplies something better -- docs/04 wants
    "+2 branches, p50 +18%", not a restatement of the fix.
    """
    return Conflict(
        left_issue=left.id,
        right_issue=right.id,
        left_remedy_cost=left_remedy_cost or left.suggested_direction or "(no direction given)",
        right_remedy_cost=(
            right_remedy_cost or right.suggested_direction or "(no direction given)"
        ),
        axis=_axis_for(left, right),  # type: ignore[arg-type]
        detector="same_span",
    )


def detect_same_span_conflict(
    critiques: Sequence[Critique], arbiter_affirms: bool
) -> Conflict | None:
    """Detector 2: opposing issues on overlapping lines, in one round.

    Available from round 1 and correspondingly weak, so it requires the Arbiter to affirm that
    the two `suggested_direction`s really are opposing. A non-affirmation falls through to row
    9 or 11 -- which is the safe default, because docs/11-risks.md R4 makes a spurious
    TRADEOFF the worst output the system can produce.

    `arbiter_affirms` is a single gate over the whole round, which is all this pure function
    can express. The orchestrator affirms pair by pair via `same_span_candidates`; this
    signature stays because the policy tests exercise the detector without an LLM in reach.
    """
    if not arbiter_affirms:
        return None
    candidates = same_span_candidates(critiques)
    return same_span_conflict(*candidates[0]) if candidates else None


def detect_oscillation_conflict(
    critiques: Sequence[Critique], history: Sequence[RoundRecord]
) -> Conflict | None:
    """Detector 1: an issue returns after being fixed, across a regression.

    Round *r* fixes a security issue; round *r*'s grounding shows a regression beyond noise;
    round *r+1* fixes the regression and the security issue comes back with the same
    `Issue.id`. Two rounds of evidence that the remedies exclude each other -- the strongest
    signal available, and the reason `Issue.id` is assigned by us rather than by the model.
    """
    if len(history) < 2:
        return None
    current = {i.id: i for c in critiques for i in c.issues if _eligible(i)}
    previous, before_that = history[-1], history[-2]
    for issue_id, issue in current.items():
        returned = issue_id in before_that.issue_ids and issue_id not in previous.issue_ids
        if not (returned and previous.regression_observed):
            continue
        counterpart = next(
            (
                other
                for other in current.values()
                if other.dimension is not issue.dimension and _eligible(other)
            ),
            None,
        )
        if counterpart is None:
            continue
        left, right = sorted((issue, counterpart), key=lambda i: i.id)
        return Conflict(
            left_issue=left.id,
            right_issue=right.id,
            left_remedy_cost=left.suggested_direction or "(no direction given)",
            right_remedy_cost=right.suggested_direction or "(no direction given)",
            axis=_axis_for(left, right),  # type: ignore[arg-type]
            detector="oscillation",
        )
    return None


def _spans_overlap(left: Issue, right: Issue) -> bool:
    return any(
        a_start <= b_end and b_start <= a_end
        for a_start, a_end in left.spans
        for b_start, b_end in right.spans
    )
