"""The eval judge, and the blinding that makes its answers usable.

docs/07-evaluation.md § LLM-as-judge. Four rules, and three of them are structural here
rather than asked for in the prompt:

**Blind to system identity.** "A judge that knows which arm is 'the tribunal' will flatter it."
`JudgeQuestion` has no field for the arm, `blind()` strips it from the issue id, and a test
asserts no arm id reaches the rendered prompt. A prompt instruction not to look would be a
request; a schema with nowhere to put it is a guarantee.

**One issue at a time.** "No batch scoring — batching invites the judge to score relative to
the other items in the batch." So the unit is one question, and the runner's job is to ask
many of them, not to build one big prompt.

**Only where mechanical checking is impossible.** `scoring.py` settles M3-M8 and the `rule`
and `line_range` halves of M1 without ever constructing a `JudgeQuestion`. The judge sees the
leftovers: known issues nothing matched, and reported issues nobody claimed.

**Validated before it is trusted.** `agreement()` computes Cohen's kappa against hand labels,
and docs/07 makes publishing that number worth more than any headline result: it is the
sentence that tells a reader the other numbers mean something. Below 0.6 the rubric gets
fixed before anything held-out runs.

## Why issue ids are rewritten

An `Issue.id` is `SEC-<sha1 of dimension|rule|ref>`, which is arm-independent — but the id is
*also* the handle the scorer uses to attribute a verdict back to a run. Passing it through
would be harmless today and would silently become a leak the moment anything arm-specific
enters the derivation. `blind()` replaces it with a positional label and keeps the mapping on
our side, so the judge cannot see an identifier it has no use for.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from tribunal.agents.base import Agent
from tribunal.contracts import Issue, JudgeVerdict
from tribunal.eval.case import EvalCase, KnownIssue

#: What the judge is shown instead of an `Issue.id`. Positional and meaningless on purpose.
BLIND_PREFIX = "reported-issue-"


@dataclass(frozen=True)
class JudgeQuestion:
    """One issue, one case, no arm.

    There is deliberately no `arm` field. docs/07's blinding requirement is the one that
    cannot be checked after the fact — nothing in a results table shows whether the judge
    knew — so it is enforced by the type rather than by discipline.
    """

    case: EvalCase
    label: str
    issue: Issue
    patched_source: str
    #: Only the declared defects that are still candidates. Ones already matched
    #: mechanically are excluded: offering them again invites a second, contradictory match.
    candidates: tuple[KnownIssue, ...] = ()

    # -- rendering --------------------------------------------------------------------

    def render(self) -> str:
        blocks = [
            f"Case `{self.case.id}`. Score one reported issue.",
            "",
            self._declared_block(),
            self._original_block(),
            self._patched_block(),
            self._issue_block(),
            self._closing(),
        ]
        return "\n".join(block for block in blocks if block)

    def _declared_block(self) -> str:
        if not self.candidates:
            return (
                "### Defects this case declares\n"
                "(none are still unmatched — so `matched_known_issue` must be null. The "
                "question is whether this issue is real, and whether the patch caused it.)\n"
            )
        lines = [
            f"- key={known.key} [{known.dimension.value}/{known.expected_severity.value}]\n"
            f"  {known.description}"
            for known in self.candidates
        ]
        return (
            "### Defects this case declares, still unmatched\n"
            + "\n".join(lines)
            + "\nMatch only if the reported issue is the *same defect*. `null` is the "
            "common answer.\n"
        )

    def _original_block(self) -> str:
        return f"### The original file\n{_numbered(self.case.source)}\n"

    def _patched_block(self) -> str:
        if self.patched_source == self.case.source:
            return (
                "### The patched file\n(no patch was applied — the original above is what "
                "would ship, so nothing can be a regression.)\n"
            )
        return f"### The patched file\n{_numbered(self.patched_source)}\n"

    def _issue_block(self) -> str:
        issue = self.issue
        refs = ", ".join(f"{e.kind.value}:{e.ref}" for e in issue.evidence)
        excerpts = "\n".join(f"    {e.excerpt}" for e in issue.evidence if e.excerpt)
        block = (
            f"### The reported issue ({self.label})\n"
            f"dimension: {issue.dimension.value}\n"
            f"claimed severity: {issue.severity.value}\n"
            f"claimed to be introduced by the patch: {issue.introduced_by_patch}\n"
            f"title: {issue.title}\n"
            f"explanation: {issue.explanation}\n"
            f"cited evidence: {refs}\n"
        )
        if excerpts:
            block += f"evidence excerpts:\n{excerpts}\n"
        return block

    def _closing(self) -> str:
        keys = ", ".join(k.key for k in self.candidates) or "(none)"
        return (
            "Emit a JudgeVerdict. `matched_known_issue` must be one of these keys or null: "
            f"{keys}."
        )


def _numbered(source: str) -> str:
    return "\n".join(
        f"{index:>4}| {line}" for index, line in enumerate(source.splitlines(), start=1)
    )


def blind(
    case: EvalCase,
    issues: Sequence[Issue],
    patched_source: str | None,
    candidates: Sequence[KnownIssue] = (),
) -> list[JudgeQuestion]:
    """One question per issue, with the arm and the real ids stripped.

    Labels are positional (`reported-issue-1`, ...) within one arm's run, so nothing about
    ordering across arms leaks either. The caller keeps the label-to-id mapping; the judge
    never sees an id it cannot use.
    """
    return [
        JudgeQuestion(
            case=case,
            label=f"{BLIND_PREFIX}{index}",
            issue=issue,
            patched_source=patched_source or case.source,
            candidates=tuple(candidates),
        )
        for index, issue in enumerate(issues, start=1)
    ]


class Judge(Agent[JudgeVerdict]):
    """`claude-opus-5` at `high`, and docs/07 says not to economise here.

    "Judging is harder than critiquing, and a weak judge silently caps the eval's
    resolution." The failure is invisible in the results: every arm is scored by the same
    weak judge, so the columns stay internally consistent and the whole table is just
    quieter than the truth.
    """

    role = "judge"
    output_model = JudgeVerdict

    def render_user(self, bundle: JudgeQuestion) -> str:
        return bundle.render()

    def post_validate(self, value: JudgeVerdict, bundle: JudgeQuestion) -> None:
        keys = {known.key for known in bundle.candidates}
        if value.matched_known_issue is not None and value.matched_known_issue not in keys:
            raise ValueError(
                f"matched_known_issue is {value.matched_known_issue!r}, which is not one of "
                f"this case's unmatched keys {sorted(keys) or '(none)'}. A match against a "
                "key that is not offered cannot be attributed to anything."
            )
        if bundle.patched_source == bundle.case.source and value.is_regression:
            raise ValueError(
                "is_regression is true but no patch was applied — the file the issue is "
                "about is the original, so nothing about it can have been introduced."
            )

    def metrics(self, value: JudgeVerdict, bundle: JudgeQuestion) -> dict[str, object]:
        return {
            "matched": value.matched_known_issue is not None,
            "is_real_issue": value.is_real_issue,
            "is_regression": value.is_regression,
            "confidence": value.confidence,
        }


# -- validating the judge -------------------------------------------------------------------


@dataclass(frozen=True)
class Agreement:
    """Cohen's kappa against hand labels, with the counts it was computed from."""

    kappa: float
    n: int
    observed: float
    expected: float
    disagreements: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """docs/07: "If κ < 0.6, fix the judge rubric before running anything on held-out."""
        return self.kappa >= 0.6

    def render(self) -> str:
        verdict = "usable" if self.usable else "NOT USABLE — fix the rubric first"
        return (
            f"judge agreement with hand labels: κ = {self.kappa:.2f} (n = {self.n}), "
            f"observed {self.observed:.2f}, expected by chance {self.expected:.2f} — "
            f"{verdict}"
        )


def agreement(
    labels: Sequence[tuple[str, str]], names: Sequence[str] | None = None
) -> Agreement:
    """Cohen's kappa over `(hand_label, judge_label)` pairs.

    Implemented here rather than pulled in from `sklearn`, for two reasons that are the same
    reason: it is twenty lines, and the number is the one docs/07 says to *publish*. A
    published statistic whose computation nobody can read is worth less than the twenty
    lines cost.

    Labels are arbitrary strings — for M1 they are known-issue keys or `"none"`, for M2 they
    are `"regression"` / `"not"`. Any categorical pair works.
    """
    n = len(labels)
    if n == 0:
        return Agreement(kappa=0.0, n=0, observed=0.0, expected=0.0)

    categories = sorted(set(names or []) | {label for pair in labels for label in pair})
    observed = sum(1 for hand, judged in labels if hand == judged) / n

    expected = 0.0
    for category in categories:
        hand_rate = sum(1 for hand, _ in labels if hand == category) / n
        judge_rate = sum(1 for _, judged in labels if judged == category) / n
        expected += hand_rate * judge_rate

    # Perfect agreement on a single category is the degenerate case: chance agreement is
    # also 1.0 and kappa is 0/0. Report 1.0 rather than a division error, and let `n` and
    # the category count tell a reader the statistic is not saying much.
    kappa = 1.0 if expected >= 1.0 else (observed - expected) / (1.0 - expected)
    return Agreement(
        kappa=round(kappa, 4),
        n=n,
        observed=round(observed, 4),
        expected=round(expected, 4),
        disagreements=[
            (str(index), hand, judged)
            for index, (hand, judged) in enumerate(labels)
            if hand != judged
        ],
    )
