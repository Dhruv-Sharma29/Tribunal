"""The evaluation harness (docs/07-evaluation.md).

Split so the parts that need a credential are separable from the parts that do not: `case`
and `scoring` are pure, and every mechanical metric is reproducible from a committed trace
with no API call. The judge, the arms and the runner build on top of them.
"""

from tribunal.eval.case import (
    CaseError,
    CaseMeta,
    EvalCase,
    KnownIssue,
    Locator,
    composition_gaps,
    load_case,
    load_suite,
)
from tribunal.eval.judge import (
    Agreement,
    Judge,
    JudgeQuestion,
    agreement,
    blind,
)
from tribunal.eval.scoring import (
    ArmTotals,
    Match,
    Scorecard,
    match_known_issue,
    re_rating,
    resolution_caveat,
    score,
    totals,
)

__all__ = [
    "Agreement",
    "ArmTotals",
    "CaseError",
    "CaseMeta",
    "EvalCase",
    "Judge",
    "JudgeQuestion",
    "KnownIssue",
    "Locator",
    "Match",
    "Scorecard",
    "agreement",
    "blind",
    "composition_gaps",
    "load_case",
    "load_suite",
    "match_known_issue",
    "re_rating",
    "resolution_caveat",
    "score",
    "totals",
]
