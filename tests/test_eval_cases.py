"""The benchmark case format, the loader's guards, and the mechanical scorer.

Two kinds of test. The first checks the loader rejects malformed cases — every rule here is
one docs/07 states in prose, and the reason to enforce it is that a broken case does not fail
at authoring time, it fails at *scoring* time, as a known issue nobody could ever catch that
lands in the results as a miss.

The second runs the **real grounding suite** over the committed cases and checks that every
`rule` locator actually fires on the file it points into. That is the check that cannot be
done by inspection: `B602` is either what `bandit` emits for this code or it is not, and
finding out during the held-out sweep is finding out too late.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from tribunal.config import Settings
from tribunal.contracts import (
    Critique,
    Decision,
    Dimension,
    Evidence,
    EvidenceKind,
    GroundingFinding,
    Issue,
    Report,
    Severity,
    finding_id,
)
from tribunal.eval import (
    CaseError,
    composition_gaps,
    load_case,
    load_suite,
    match_known_issue,
    re_rating,
    resolution_caveat,
    score,
    totals,
)
from tribunal.eval.case import TARGET_COMPOSITION
from tribunal.grounding.suite import GroundingSuite

CASES = Path(__file__).resolve().parent.parent / "eval" / "cases"


# -- the committed set ------------------------------------------------------------------------


def test_every_committed_case_loads():
    suite = load_suite(CASES)
    assert suite, "no cases are committed yet"
    for case in suite:
        assert case.id == case.directory.name
        assert case.notes


def test_the_composition_gap_is_reported_rather_than_asserted():
    """The set is built up over days, so a hard assertion would mean a red suite for a week.
    What must not happen is losing track of the distance to docs/07's target."""
    gaps = composition_gaps(load_suite(CASES))
    assert set(gaps) <= set(TARGET_COMPOSITION)
    remaining = sum(gaps.values())
    assert remaining == sum(TARGET_COMPOSITION.values()) - len(load_suite(CASES))


def test_the_splits_are_recorded():
    for case in load_suite(CASES):
        assert case.meta.split in ("dev", "heldout")


def test_no_heldout_case_is_opened_during_development():
    """docs/07: "Keep the 16 held-out cases **unopened** during development." Nothing
    enforces that but a habit, so this at least makes the count visible."""
    suite = load_suite(CASES)
    heldout = [c.id for c in suite if c.meta.split == "heldout"]
    assert len(heldout) + len([c for c in suite if c.meta.split == "dev"]) == len(suite)


# -- the grounding the locators depend on -------------------------------------------------------


def ground(source: str, filename: str) -> list[GroundingFinding]:
    run = asyncio.run(GroundingSuite(Settings()).run(source, logical_name=filename))
    return list(run.report.findings)


@pytest.mark.parametrize(
    "case", load_suite(CASES), ids=lambda c: c.id
)
def test_every_rule_locator_fires_on_its_own_case(case):
    """A `rule` locator naming a code the tools do not emit for this file is a known issue
    nothing can match — it scores as a miss against every arm, and it looks like a finding
    about the tribunal rather than about the case."""
    wanted = {
        known.locator.value
        for known in case.known_issues
        if known.locator.kind == "rule"
    }
    if not wanted:
        pytest.skip("no rule locators in this case")
    found = {finding.rule for finding in ground(case.source, case.filename)}
    missing = wanted - found
    assert not missing, (
        f"{case.id}: locator rule(s) {sorted(missing)} are not emitted by the grounding "
        f"suite on before.py. It reported: {sorted(found)}"
    )


@pytest.mark.parametrize("case", load_suite(CASES), ids=lambda c: c.id)
def test_a_canary_clean_case_really_is_clean(case):
    """If the tools themselves flag a canary, the case is not measuring critic inflation —
    it is measuring whether the critic repeats a linter, which is a different question."""
    if case.meta.category != "canary_clean":
        pytest.skip("not a canary")
    findings = [f for f in ground(case.source, case.filename) if f.tool != "radon"]
    assert not findings, (
        f"{case.id} is canary_clean but the tools flag "
        f"{[(f.tool, f.rule) for f in findings]}"
    )


# -- loader guards ------------------------------------------------------------------------------


def write_case(root: Path, meta: dict, source: str = "x = 1\n", notes: str = "why") -> Path:
    directory = root / meta["id"]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "meta.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
    (directory / "before.py").write_text(source, encoding="utf-8")
    (directory / "notes.md").write_text(notes, encoding="utf-8")
    return directory


def meta(**overrides) -> dict:
    base = {
        "id": "900-example",
        "category": "security_only",
        "known_issues": [
            {
                "key": "k",
                "dimension": "security",
                "expected_severity": "high",
                "locator": {"kind": "rule", "value": "B602"},
                "description": "a defect",
            }
        ],
        "expected_outcome": "accept",
        "provenance": "hand-written",
        "split": "dev",
    }
    base.update(overrides)
    return base


def test_a_wellformed_case_loads(tmp_path):
    case = load_case(write_case(tmp_path, meta()))
    assert case.id == "900-example"
    assert case.meta.category == "security_only"


def test_an_id_that_is_not_the_directory_name_is_rejected(tmp_path):
    directory = write_case(tmp_path, meta())
    (directory / "meta.yaml").write_text(
        yaml.safe_dump(meta(id="something-else")), encoding="utf-8"
    )
    with pytest.raises(CaseError, match="not the directory name"):
        load_case(directory)


def test_a_canary_clean_case_may_not_declare_known_issues(tmp_path):
    with pytest.raises(CaseError, match="nothing to find"):
        load_case(write_case(tmp_path, meta(id="901-c", category="canary_clean")))


def test_a_non_canary_case_must_declare_a_known_issue(tmp_path):
    """Otherwise M1 can neither pass nor fail on it, and it silently pads the denominator."""
    with pytest.raises(CaseError, match="no known issues"):
        load_case(write_case(tmp_path, meta(id="902-e", known_issues=[])))


def test_a_conflicting_case_must_expect_a_tradeoff(tmp_path):
    """docs/07 M5: on the conflicting cases B0-B2 structurally cannot produce the right
    answer. One expecting `accept` would hand them the point."""
    both = meta()["known_issues"] + [
        {
            "key": "p",
            "dimension": "performance",
            "expected_severity": "medium",
            "locator": {"kind": "line_range", "value": [1, 1], "anchor": "x = 1"},
            "description": "slow",
        }
    ]
    with pytest.raises(CaseError, match="must be tradeoff"):
        load_case(write_case(
            tmp_path, meta(id="903-x", category="conflicting", known_issues=both)
        ))


def test_a_conflicting_case_needs_one_issue_per_dimension(tmp_path):
    with pytest.raises(CaseError, match="needs one of each"):
        load_case(write_case(tmp_path, meta(
            id="904-x", category="conflicting", expected_outcome="tradeoff"
        )))


def test_a_seeded_bad_canary_must_carry_a_high_security_issue(tmp_path):
    """Without one, a sycophantic critic passes the case that exists to catch it."""
    mild = [{
        "key": "k", "dimension": "security", "expected_severity": "low",
        "locator": {"kind": "rule", "value": "B602"}, "description": "meh",
    }]
    with pytest.raises(CaseError, match="no high-severity security issue"):
        load_case(write_case(
            tmp_path, meta(id="905-s", category="canary_seeded_bad", known_issues=mild)
        ))


def test_a_typod_rule_code_is_rejected(tmp_path):
    bad = [{
        "key": "k", "dimension": "security", "expected_severity": "high",
        "locator": {"kind": "rule", "value": "shell=True"}, "description": "d",
    }]
    with pytest.raises(CaseError, match="does not look like a tool rule code"):
        load_case(write_case(tmp_path, meta(id="906-r", known_issues=bad)))


def test_a_line_range_past_the_end_of_the_file_is_rejected(tmp_path):
    """Invisible on inspection, impossible to match, and it reads in the results as a miss."""
    out_of_range = [{
        "key": "k", "dimension": "performance", "expected_severity": "medium",
        "locator": {"kind": "line_range", "value": [40, 90], "anchor": "x = 1"},
        "description": "d",
    }]
    with pytest.raises(CaseError, match="before.py has 1"):
        load_case(write_case(
            tmp_path, meta(id="907-l", category="performance_only",
                           known_issues=out_of_range)
        ))


def test_an_unparseable_input_is_rejected(tmp_path):
    """The orchestrator escalates before the Coder runs, so every arm would score the same
    on it regardless of what it was meant to test."""
    with pytest.raises(CaseError, match="does not parse"):
        load_case(write_case(tmp_path, meta(id="908-p"), source="def f(:\n"))


def test_empty_notes_are_rejected(tmp_path):
    with pytest.raises(CaseError, match="notes.md is empty"):
        load_case(write_case(tmp_path, meta(id="909-n"), notes="   \n"))


def test_duplicate_known_issue_keys_are_rejected(tmp_path):
    twice = meta()["known_issues"] * 2
    with pytest.raises(CaseError, match="duplicate known_issue keys"):
        load_case(write_case(tmp_path, meta(id="910-d", known_issues=twice)))


def test_a_missing_file_names_which_one(tmp_path):
    directory = write_case(tmp_path, meta(id="911-m"))
    (directory / "notes.md").unlink()
    with pytest.raises(CaseError, match="missing notes.md"):
        load_case(directory)


# -- scoring --------------------------------------------------------------------------------

B602 = finding_id("bandit", "B602", "before.py", 7)


def finding(rule: str = "B602", line: int = 7, tool: str = "bandit") -> GroundingFinding:
    return GroundingFinding(
        id=finding_id(tool, rule, "before.py", line), tool=tool, rule=rule,
        file="before.py", line=line, end_line=line, message=f"{tool} {rule}",
        tool_severity="HIGH", raw={},
    )


def issue(
    id_: str = "SEC-1", kind: EvidenceKind = EvidenceKind.TOOL_FINDING,
    ref: str = B602, severity: Severity = Severity.HIGH,
    dimension: Dimension = Dimension.SECURITY,
) -> Issue:
    return Issue(
        id=id_, dimension=dimension, severity=severity, title="t", explanation="e",
        evidence=[Evidence(kind=kind, ref=ref, excerpt="x")], confidence=0.9,
        introduced_by_patch=False, suggested_direction="d",
    )


def test_a_rule_locator_matches_an_issue_citing_that_finding():
    case = load_case(CASES / "001-shell-injection-report")
    known = case.issue("shell_injection")
    match = match_known_issue(known, [issue()], [finding()])
    assert match.caught and match.how == "rule"
    assert match.issue_id == "SEC-1"


def test_a_rule_locator_does_not_match_a_different_rule():
    case = load_case(CASES / "001-shell-injection-report")
    match = match_known_issue(
        case.issue("shell_injection"),
        [issue(ref=finding_id("bandit", "B105", "before.py", 7))],
        [finding(rule="B105")],
    )
    assert not match.caught


def test_a_line_range_locator_matches_an_overlapping_span():
    case = load_case(CASES / "002-quadratic-membership")
    known = case.issue("list_membership")  # lines 5-5
    match = match_known_issue(
        known,
        [issue(kind=EvidenceKind.CODE_SPAN, ref="before.py:L4-L6",
               dimension=Dimension.PERFORMANCE, severity=Severity.MEDIUM)],
        [],
    )
    assert match.caught and match.how == "line_range"


def test_a_line_range_locator_does_not_match_a_distant_span():
    case = load_case(CASES / "002-quadratic-membership")
    match = match_known_issue(
        case.issue("list_membership"),
        [issue(kind=EvidenceKind.CODE_SPAN, ref="before.py:L40-L41",
               dimension=Dimension.PERFORMANCE, severity=Severity.MEDIUM)],
        [],
    )
    assert not match.caught and match.how == "none"


def test_a_line_range_locator_matches_through_a_cited_findings_span():
    """The issue's own evidence may be a tool finding with no span in the ref, while the
    finding it names knows exactly which line it is about."""
    case = load_case(CASES / "002-quadratic-membership")
    hit = finding(rule="PERF401", line=5, tool="ruff")
    match = match_known_issue(
        case.issue("list_membership"), [issue(ref=hit.id)], [hit]
    )
    assert match.caught and match.how == "line_range"


def test_an_unmatched_known_issue_is_queued_for_the_judge_not_scored_as_missed():
    """docs/07 orders the three match kinds: rule, then line range, then the judge. Nothing
    mechanical matching is the judge's input, and the scorer says so rather than deciding."""
    case = load_case(CASES / "001-shell-injection-report")
    card = score(case, "B3", report(), [critique([])], [finding()])
    assert card.unmatched_known == ["shell_injection"]
    assert card.known_caught == 0


def critique(issues: list[Issue], dimension: Dimension = Dimension.SECURITY) -> Critique:
    return Critique(
        dimension=dimension, round=1,
        verdict="block" if any(i.severity is Severity.HIGH for i in issues) else "clean",
        issues=issues, tools_consulted=["bandit"], summary="s",
    )


def report(outcome: Decision = Decision.ACCEPT, diff: str | None = "--- a\n+++ b\n") -> Report:
    return Report(
        run_id="01T", input_file="before.py", input_sha256="x", outcome=outcome,
        rule_fired="accept", rounds_used=1, accepted_diff=diff,
        no_patch_reason=None if diff else "nothing applied", issues=[], conflict=None,
        pressure_history=[0.0], unassessed_dimensions=[], total_cost_usd=0.42,
        total_tokens=100, wall_seconds=1.0,
    )


def test_m4_needs_the_patch_to_apply_and_the_test_to_pass():
    case = load_case(CASES / "001-shell-injection-report")
    assert score(case, "B3", report(), [], [], test_passes=True).fix_correct
    assert not score(case, "B3", report(), [], [], test_passes=False).fix_correct
    assert not score(case, "B3", report(diff=None), [], [], test_passes=True).fix_correct


def test_a_case_with_no_test_is_scored_on_the_two_checks_that_exist():
    case = load_case(CASES / "001-shell-injection-report")
    assert score(case, "B3", report(), [], [], test_passes=None).fix_correct


def test_m5_compares_the_outcome_to_what_the_case_expects():
    conflicting = load_case(CASES / "003-validated-query-in-hot-loop")
    assert score(conflicting, "B3", report(Decision.TRADEOFF), [], []).outcome_correct
    assert not score(conflicting, "B3", report(Decision.ACCEPT), [], []).outcome_correct


def test_m3_counts_only_medium_and_above_on_a_canary():
    """docs/07 M3. A `low` note on correct code is defensible; a `medium` is inflation."""
    canary = load_case(CASES / "004-normalise-names")
    noisy = critique([
        issue(id_="SEC-1", severity=Severity.HIGH),
        issue(id_="SEC-2", severity=Severity.MEDIUM),
        issue(id_="SEC-3", severity=Severity.LOW),
        issue(id_="SEC-4", severity=Severity.INFO),
    ])
    card = score(canary, "B3", report(), [noisy], [finding()])
    assert card.false_positives == 2
    assert card.reported_issues == 4


def test_m3_is_not_counted_on_a_case_that_has_real_issues():
    """A false positive is only definable where the right answer is "nothing"."""
    real = load_case(CASES / "001-shell-injection-report")
    card = score(real, "B3", report(), [critique([issue()])], [finding()])
    assert card.false_positives == 0


def test_m8_counts_only_issues_that_had_a_tool_rating_to_diverge_from():
    """A `code_span` issue has nothing to disagree with, and counting it would make the
    re-rating rate a function of how often critics find novel issues instead."""
    tool_backed = issue(severity=Severity.LOW)  # bandit said HIGH
    novel = issue(id_="SEC-2", kind=EvidenceKind.CODE_SPAN, ref="before.py:L1-L2")
    re_rated, comparable = re_rating([tool_backed, novel], [finding()])
    assert (re_rated, comparable) == (1, 1)


def test_totals_stay_counts_rather_than_rates():
    """docs/07: "Report counts (14/16), not percentages to one decimal."""
    case = load_case(CASES / "001-shell-injection-report")
    cards = [
        score(case, "B3", report(), [critique([issue()])], [finding()], test_passes=True),
        score(case, "B3", report(diff=None), [critique([])], [finding()]),
    ]
    arm = totals(cards)["B3"]
    assert (arm.runs, arm.fix_correct) == (2, 1)
    assert (arm.known_caught, arm.known_total) == (1, 2)
    assert arm.cost_p50 == 0.42


def test_the_resolution_caveat_is_stated_in_cases_not_decimals():
    text = resolution_caveat(16)
    assert "n=16" in text
    assert "~6pp" in text
    assert "under ~3 cases" in text


def test_a_locator_pointing_only_at_comments_is_rejected(tmp_path):
    """Reachable even with a correct anchor: the range quotes the comment faithfully and
    still points at nothing a critic could cite."""
    commented = [{
        "key": "k", "dimension": "performance", "expected_severity": "medium",
        "locator": {"kind": "line_range", "value": [2, 2], "anchor": "# the slow bit"},
        "description": "d",
    }]
    with pytest.raises(CaseError, match="blank or comments"):
        load_case(write_case(
            tmp_path,
            meta(id="912-b", category="performance_only", known_issues=commented),
            source="x = 1\n# the slow bit\ny = 2\n",
        ))


@pytest.mark.parametrize("case", load_suite(CASES), ids=lambda c: c.id)
def test_no_case_carries_formatting_noise(case):
    """A `before.py` that trips E501 hands every critic a finding about line length.

    Incidental *real* findings are welcome — they are what makes a case realistic, and a
    critic choosing not to raise one is information. Formatting complaints are not that:
    they inflate the reported-issue count on every arm equally, they crowd the findings
    block the critics are shown, and on a canary they are indistinguishable from the
    inflation M3 exists to measure. Authoring 005-007 introduced three of them.
    """
    formatting = [
        f.rule
        for f in ground(case.source, case.filename)
        if f.tool == "ruff" and f.rule.startswith("E")
    ]
    assert not formatting, (
        f"{case.id}: before.py trips {sorted(set(formatting))}. Wrap the lines — this is "
        "noise in every arm's findings, not a defect worth measuring."
    )


# -- anchors: what makes a line_range locator checkable --------------------------------------


@pytest.mark.parametrize("case", load_suite(CASES), ids=lambda c: c.id)
def test_every_committed_line_range_locator_states_what_it_points_at(case):
    """The loader verifies the anchor against the file, so this only has to prove the
    committed cases actually carry one — a locator with no anchor cannot be loaded at all."""
    for known in case.known_issues:
        if known.locator.kind == "line_range":
            assert known.locator.anchor
            text = " ".join(line.strip() for line in case.located_lines(known))
            assert text == " ".join(known.locator.anchor.split())


def test_a_line_range_without_an_anchor_is_rejected(tmp_path):
    """Authoring case 002 pointed at line 15 — the `return` — when the concatenation was on
    14. Every number inside the file is structurally valid, so nothing but the text can tell
    an off-by-one from a deliberate choice."""
    no_anchor = [{
        "key": "k", "dimension": "performance", "expected_severity": "medium",
        "locator": {"kind": "line_range", "value": [1, 1]}, "description": "d",
    }]
    with pytest.raises(CaseError, match="has no `anchor`"):
        load_case(write_case(
            tmp_path, meta(id="913-a", category="performance_only", known_issues=no_anchor)
        ))


def test_an_anchor_that_does_not_match_the_lines_is_rejected(tmp_path):
    """The failure mode this exists for: `before.py` is edited, the lines shift, and the
    locator silently starts pointing at the statement below the defect."""
    wrong = [{
        "key": "k", "dimension": "performance", "expected_severity": "medium",
        "locator": {"kind": "line_range", "value": [1, 1], "anchor": "y = 2"},
        "description": "d",
    }]
    with pytest.raises(CaseError, match="but its anchor says"):
        load_case(write_case(
            tmp_path,
            meta(id="914-a", category="performance_only", known_issues=wrong),
            source="x = 1\ny = 2\n",
        ))


def test_a_rule_locator_may_not_carry_an_anchor(tmp_path):
    """A rule locator is checked against the tools. An anchor there would be a second,
    unverified claim about the same thing."""
    both = [{
        "key": "k", "dimension": "security", "expected_severity": "high",
        "locator": {"kind": "rule", "value": "B602", "anchor": "x = 1"},
        "description": "d",
    }]
    with pytest.raises(CaseError, match="does not take an anchor"):
        load_case(write_case(tmp_path, meta(id="915-a", known_issues=both)))
