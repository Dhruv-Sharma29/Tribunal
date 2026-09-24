"""Validating the judge: the worksheet, and the kappa it produces.

docs/07-evaluation.md § LLM-as-judge: "Hand-label 30 issue/outcome pairs drawn from
dev-split runs, compute agreement (Cohen's κ), and publish it. If κ < 0.6, fix the judge
rubric before running anything on held-out."

`judge.agreement()` does the arithmetic. This module is everything around it — which is the
part that decides whether the number means anything:

**The worksheet is generated offline, from recorded runs.** Sampling the pairs by hand
invites sampling the interesting ones, and a kappa computed over pairs chosen for being
interesting is not a kappa over the judge's actual workload. `worksheet()` takes them from
real dev-split traces, seeded, so the same results directory always yields the same 30.

**The human labels before the judge runs.** The worksheet has an empty `hand_label` and
carries no judge verdict, because seeing one first is anchoring and there is no way to
detect it afterwards. `kappa()` is a separate step over a filled-in worksheet.

**`judge-kappa.json` is the file `heldout.yml` refuses to run without.** It was a consumer
with no producer until this module existed, which is the failure mode the README's design
notes are mostly about.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tribunal.contracts import JudgeVerdict
from tribunal.eval.case import EvalCase
from tribunal.eval.judge import Agreement, JudgeQuestion, agreement, blind

#: docs/07 asks for 30. Enough for a kappa to mean something at this resolution, few enough
#: to hand-label in a sitting — which matters, because a worksheet nobody finishes produces
#: no number at all.
DEFAULT_SAMPLE = 30

#: What a hand label says when the reported issue matches none of the case's declared
#: defects. Spelled out rather than left blank so an unfilled row is distinguishable from a
#: deliberate "none".
NO_MATCH = "none"


@dataclass(frozen=True)
class LabelRow:
    """One pair to label, and the label once someone has."""

    case_id: str
    arm: str
    issue_id: str
    title: str
    explanation: str
    candidates: tuple[str, ...]
    hand_label: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "case": self.case_id,
            # The arm is recorded for provenance, and is *not* shown to the judge —
            # `JudgeQuestion` has no field for it.
            "arm": self.arm,
            "issue": self.issue_id,
            "title": self.title,
            "explanation": self.explanation,
            "candidates": [*self.candidates, NO_MATCH],
            "hand_label": self.hand_label,
        }

    @classmethod
    def from_json(cls, row: dict[str, Any]) -> LabelRow:
        return cls(
            case_id=row["case"], arm=row["arm"], issue_id=row["issue"],
            title=row.get("title", ""), explanation=row.get("explanation", ""),
            candidates=tuple(c for c in row.get("candidates", []) if c != NO_MATCH),
            hand_label=row.get("hand_label"),
        )


def worksheet(
    pairs: list[tuple[EvalCase, str, Any, tuple[str, ...]]],
    sample: int = DEFAULT_SAMPLE,
    seed: int = 0,
) -> list[LabelRow]:
    """Sample `(case, arm, issue, candidate keys)` down to a labelling worksheet.

    Seeded and sorted first, so the same results directory always yields the same rows. A
    worksheet that changed between invocations would let someone re-roll until the labels
    were easy, which is the same failure as choosing them by hand.
    """
    rows = [
        LabelRow(
            case_id=case.id, arm=arm, issue_id=issue.id, title=issue.title,
            explanation=issue.explanation, candidates=candidates,
        )
        for case, arm, issue, candidates in pairs
    ]
    rows.sort(key=lambda r: (r.case_id, r.arm, r.issue_id))
    if len(rows) <= sample:
        return rows
    return sorted(
        random.Random(seed).sample(rows, sample),
        key=lambda r: (r.case_id, r.arm, r.issue_id),
    )


def write_worksheet(rows: list[LabelRow], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps({
                "_comment":
                    "Fill in hand_label on every row: a candidate key, or \"none\". Label "
                    "before running the judge — seeing its answer first is anchoring, and "
                    "nothing downstream can detect that you did.",
                "_rows": len(rows),
            })
            + "\n"
        )
        for row in rows:
            handle.write(json.dumps(row.to_json(), sort_keys=True) + "\n")
    return path


def read_worksheet(path: Path) -> list[LabelRow]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        if "_comment" in raw:
            continue
        rows.append(LabelRow.from_json(raw))
    return rows


def unlabelled(rows: list[LabelRow]) -> list[LabelRow]:
    return [row for row in rows if not (row.hand_label or "").strip()]


def kappa(rows: list[LabelRow], verdicts: dict[str, JudgeVerdict]) -> Agreement:
    """Cohen's kappa over `(hand label, judge label)`, keyed by issue id.

    A row the judge did not answer is dropped rather than counted as a disagreement: an
    unanswered question is absent, not wrong, and counting it would let a flaky provider
    depress the number the whole eval rests on.
    """
    pairs: list[tuple[str, str]] = []
    for row in rows:
        verdict = verdicts.get(row.issue_id)
        if verdict is None or not row.hand_label:
            continue
        pairs.append((row.hand_label, verdict.matched_known_issue or NO_MATCH))
    return agreement(pairs)


def questions_for(
    rows: list[LabelRow], cases: dict[str, EvalCase], patched: dict[str, str]
) -> list[tuple[str, JudgeQuestion]]:
    """Rebuild the judge's question for each labelled row, keyed by issue id.

    Goes through `blind()` like every other judge call, so the validation run asks exactly
    the questions a scoring run would. A kappa measured on differently-shaped prompts would
    be a kappa for a judge nobody uses.
    """
    from tribunal.contracts import Dimension, Evidence, EvidenceKind, Issue, Severity

    out: list[tuple[str, JudgeQuestion]] = []
    for row in rows:
        case = cases.get(row.case_id)
        if case is None:
            continue
        issue = Issue(
            id=row.issue_id,
            # The worksheet stores what the judge is shown, and the judge is shown the
            # title, the explanation and the evidence — not the dimension or the severity,
            # which are the reporter's claims rather than facts about the defect.
            dimension=Dimension.SECURITY,
            severity=Severity.MEDIUM,
            title=row.title,
            explanation=row.explanation,
            evidence=[Evidence(kind=EvidenceKind.REASONING, ref="recorded", excerpt="")],
            confidence=0.5,
            introduced_by_patch=False,
            suggested_direction=None,
        )
        candidates = [case.issue(key) for key in row.candidates if _has(case, key)]
        question = blind(case, [issue], patched.get(row.case_id), candidates)[0]
        out.append((row.issue_id, question))
    return out


def _has(case: EvalCase, key: str) -> bool:
    try:
        case.issue(key)
    except KeyError:
        return False
    return True


def write_kappa(path: Path, result: Agreement, model: str, rows: int) -> Path:
    """The file `heldout.yml` refuses to run without.

    It records the number *and* what produced it. A kappa with no model, no date and no n
    beside it is a number someone will still be quoting after the rubric has changed twice.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "kappa": result.kappa,
                "n": result.n,
                "labelled_rows": rows,
                "observed_agreement": result.observed,
                "expected_by_chance": result.expected,
                "judge_model": model,
                "computed": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "usable": result.usable,
                "threshold": 0.6,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path
