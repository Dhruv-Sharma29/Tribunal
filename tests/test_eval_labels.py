"""The judge-validation workflow: worksheet, kappa, and the file the held-out gate needs.

Every step here is offline. The only part that needs a credential is asking the judge, and
that is deliberately the *last* step: the worksheet is generated and hand-labelled before a
model is involved, because seeing the judge's answer first is anchoring and nothing
downstream can detect that it happened.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from tests.test_eval_runner import (
    INJECTION,
    arbiter_note,
    critique,
    proposal,
    run_sweep,
    span_issue,
    write_up,
)
from tribunal.cli import ExitCode, app
from tribunal.contracts import JudgeVerdict
from tribunal.eval import load_suite
from tribunal.eval.labels import (
    NO_MATCH,
    LabelRow,
    kappa,
    questions_for,
    read_worksheet,
    unlabelled,
    worksheet,
    write_kappa,
    write_worksheet,
)
from tribunal.eval.runner import judge_pairs, patched_sources

CASES = Path(__file__).resolve().parent.parent / "eval" / "cases"
SUITE = {case.id: case for case in load_suite(CASES)}


def verdict(matched: str | None) -> JudgeVerdict:
    return JudgeVerdict(
        matched_known_issue=matched, is_real_issue=matched is not None,
        is_regression=False, reasoning="r", confidence=0.8,
    )


def row(issue_id: str, hand: str | None = "shell_injection") -> LabelRow:
    return LabelRow(
        case_id=INJECTION.id, arm="B3", issue_id=issue_id, title="t",
        explanation="e", candidates=("shell_injection",), hand_label=hand,
    )


# -- the worksheet ----------------------------------------------------------------------


def test_the_worksheet_is_deterministic_for_a_given_seed():
    """A worksheet that changed between invocations would let someone re-roll until the
    labels were easy, which is the same failure as choosing them by hand."""
    pairs = [
        (INJECTION, "B3", _issue(f"SEC-{n}"), ("shell_injection",)) for n in range(50)
    ]
    first = [r.issue_id for r in worksheet(pairs, sample=10, seed=7)]
    again = [r.issue_id for r in worksheet(pairs, sample=10, seed=7)]
    other = [r.issue_id for r in worksheet(pairs, sample=10, seed=8)]
    assert first == again
    assert first != other


def _issue(id_: str):
    from tribunal.contracts import Dimension, Evidence, EvidenceKind, Issue, Severity

    return Issue(
        id=id_, dimension=Dimension.SECURITY, severity=Severity.HIGH, title="t",
        explanation="e",
        evidence=[Evidence(kind=EvidenceKind.CODE_SPAN, ref="before.py:L7-L7", excerpt="")],
        confidence=0.9, introduced_by_patch=False, suggested_direction=None,
    )


def test_a_small_pool_is_taken_whole():
    pairs = [(INJECTION, "B3", _issue("SEC-1"), ())]
    assert len(worksheet(pairs, sample=30)) == 1


def test_the_worksheet_carries_no_judge_verdict(tmp_path):
    """The human labels first. A worksheet that showed the judge's answer would measure
    agreement with an anchor rather than agreement with a judge."""
    path = write_worksheet([row("SEC-1", hand=None)], tmp_path / "w.jsonl")
    body = path.read_text(encoding="utf-8")
    assert "matched_known_issue" not in body
    assert "is_real_issue" not in body
    assert '"hand_label": null' in body


def test_the_worksheet_offers_none_as_an_explicit_choice(tmp_path):
    """Blank means unlabelled. "none" means the labeller decided it matches nothing, and
    those are different states."""
    path = write_worksheet([row("SEC-1", hand=None)], tmp_path / "w.jsonl")
    written = json.loads(
        [ln for ln in path.read_text().splitlines() if "_comment" not in ln][0]
    )
    assert NO_MATCH in written["candidates"]


def test_a_worksheet_round_trips(tmp_path):
    path = write_worksheet([row("SEC-1"), row("SEC-2", hand=NO_MATCH)], tmp_path / "w.jsonl")
    back = read_worksheet(path)
    assert [r.issue_id for r in back] == ["SEC-1", "SEC-2"]
    assert [r.hand_label for r in back] == ["shell_injection", NO_MATCH]


def test_unlabelled_rows_are_detectable(tmp_path):
    rows = [row("SEC-1"), row("SEC-2", hand=None), row("SEC-3", hand="  ")]
    assert [r.issue_id for r in unlabelled(rows)] == ["SEC-2", "SEC-3"]


# -- kappa ------------------------------------------------------------------------------


def test_kappa_compares_hand_labels_to_judge_verdicts():
    rows = [row("SEC-1"), row("SEC-2", hand=NO_MATCH)]
    verdicts = {"SEC-1": verdict("shell_injection"), "SEC-2": verdict(None)}
    assert kappa(rows, verdicts).observed == 1.0


def test_a_question_the_judge_never_answered_is_dropped_not_counted_wrong():
    """An unanswered question is absent, not wrong. Counting it would let a flaky provider
    depress the number the whole eval rests on."""
    rows = [row("SEC-1"), row("SEC-2")]
    result = kappa(rows, {"SEC-1": verdict("shell_injection")})
    assert result.n == 1


def test_the_published_file_records_what_produced_the_number(tmp_path):
    """A kappa with no model, no date and no n beside it is a number someone will still be
    quoting after the rubric has changed twice."""
    rows = [row("SEC-1"), row("SEC-2", hand=NO_MATCH)]
    verdicts = {"SEC-1": verdict("shell_injection"), "SEC-2": verdict(None)}
    path = write_kappa(tmp_path / "k.json", kappa(rows, verdicts), "a-model", len(rows))
    data = json.loads(path.read_text())
    for field in ("kappa", "n", "judge_model", "computed", "usable", "threshold"):
        assert field in data, field
    assert data["threshold"] == 0.6


def test_the_published_file_is_the_shape_the_heldout_gate_reads(tmp_path):
    """`heldout.yml` reads `kappa` out of this file and refuses below 0.6. A shape change
    here silently disarms that gate."""
    path = write_kappa(tmp_path / "k.json", kappa([], {}), "m", 0)
    assert isinstance(json.loads(path.read_text())["kappa"], float)


# -- questions rebuilt for validation ------------------------------------------------------


def test_validation_asks_the_same_shape_of_question_a_sweep_does():
    """Through `blind()`, like every other judge call. A kappa measured on differently
    shaped prompts would be a kappa for a judge nobody uses."""
    built = questions_for([row("SEC-1")], SUITE, {})
    assert len(built) == 1
    issue_id, question = built[0]
    assert issue_id == "SEC-1"
    text = question.render()
    assert "shell_injection" in text
    for arm in ("B0", "B1", "B2", "B3"):
        assert arm not in text, "the arm leaked into a validation question"


# -- end to end from a recorded sweep --------------------------------------------------------


def test_pairs_come_from_the_judges_real_workload(tmp_path):
    """Exactly the pairs `judge_cards` would generate, so a sample of them is a sample of
    what the judge actually sees."""
    run_sweep(tmp_path, [INJECTION], ["B3"], [
        proposal(search="    return destination", replace="    return str(destination)"),
        critique("security", 1, [span_issue("L7-L7")]),
        critique("performance", 1),
        arbiter_note(1),
        proposal(round_=2),
        critique("security", 2),
        critique("performance", 2),
        write_up(rounds=2),
    ])
    pairs = judge_pairs(tmp_path / "out", SUITE)
    assert pairs, "no unresolved issues to label"
    case, arm, issue, candidates = pairs[0]
    assert case.id == INJECTION.id
    assert arm == "B3"
    assert "shell_injection" in candidates


def test_patched_sources_are_recovered_for_the_validation_run(tmp_path):
    run_sweep(tmp_path, [INJECTION], ["B1"], [proposal()])
    sources = patched_sources(tmp_path / "out", SUITE)
    assert INJECTION.id in sources
    assert "shell=True" not in sources[INJECTION.id]


# -- the CLI ----------------------------------------------------------------------------------


def test_judge_labels_writes_a_worksheet(tmp_path):
    run_sweep(tmp_path, [INJECTION], ["B3"], [
        proposal(search="    return destination", replace="    return str(destination)"),
        critique("security", 1, [span_issue("L7-L7")]),
        critique("performance", 1),
        arbiter_note(1),
        proposal(round_=2),
        critique("security", 2),
        critique("performance", 2),
        write_up(rounds=2),
    ])
    out = tmp_path / "labels.jsonl"
    result = CliRunner().invoke(
        app, ["judge-labels", str(tmp_path / "out"), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert out.is_file()
    assert "before" in result.output  # tells the labeller to label first
    assert read_worksheet(out)


def test_judge_labels_says_so_when_there_is_nothing_to_label(tmp_path):
    run_sweep(tmp_path, [INJECTION], ["B0"], [proposal()])
    result = CliRunner().invoke(
        app, ["judge-labels", str(tmp_path / "out"), "--out", str(tmp_path / "l.jsonl")]
    )
    assert result.exit_code == ExitCode.USAGE
    assert "nothing to label" in result.output


def test_judge_kappa_refuses_a_partly_labelled_worksheet(tmp_path):
    """A kappa over a partly-labelled worksheet is not a kappa."""
    path = write_worksheet([row("SEC-1"), row("SEC-2", hand=None)], tmp_path / "w.jsonl")
    result = CliRunner().invoke(app, ["judge-kappa", str(path)])
    assert result.exit_code == ExitCode.USAGE
    assert "no hand_label" in result.output


def test_judge_kappa_refuses_a_missing_worksheet(tmp_path):
    result = CliRunner().invoke(app, ["judge-kappa", str(tmp_path / "nope.jsonl")])
    assert result.exit_code == ExitCode.USAGE
    assert "run `judge-labels` first" in result.output
