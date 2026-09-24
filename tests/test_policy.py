"""The deterministic decision layer.

docs/09-roadmap.md Phase 3 sets the bar: **one test per row of the decision table, plus a test
asserting no input falls through**, plus a test for every row of docs/04-arbitration.md's
failure-mode checklist. The table is the specification, so these tests are the specification
restated in a form that fails when the code drifts from it.

Everything here is pure. No API key, no clock, no sandbox — which is the whole point of Rule 1
(docs/01-architecture.md): the verdict is reproducible from the trace.
"""

from __future__ import annotations

import pytest

from tribunal.config import BudgetConfig, HardBlockRule, PolicyConfig
from tribunal.contracts import (
    Conflict,
    Critique,
    Decision,
    Dimension,
    Evidence,
    EvidenceKind,
    Issue,
    Severity,
    TestResult,
)
from tribunal.policy import (
    DECISION_TABLE,
    Context,
    PolicyInput,
    RoundRecord,
    Spend,
    decide,
    detect_oscillation_conflict,
    detect_same_span_conflict,
    duplicates_in,
    explain,
    pressure,
    regressed_tests,
)

CONFIG = PolicyConfig()
BUDGET = BudgetConfig()


def evidence(kind=EvidenceKind.TOOL_FINDING, ref="finding-1") -> Evidence:
    return Evidence(kind=kind, ref=ref, excerpt="x")


def issue(
    id="SEC-1",
    dimension=Dimension.SECURITY,
    severity=Severity.HIGH,
    confidence=0.9,
    evidence_=None,
    direction="do the thing",
) -> Issue:
    # Evidence defaults to a ref derived from the id. Without that, two "different" issues in
    # one test would cite the same finding and duplicate-collapsing would correctly merge
    # them -- which is right behaviour, and a confusing way to write a test about something
    # else.
    return Issue(
        id=id,
        dimension=dimension,
        severity=severity,
        title="t",
        explanation="e",
        evidence=evidence_ or [evidence(ref=f"finding-for-{id}")],
        confidence=confidence,
        introduced_by_patch=True,
        suggested_direction=direction,
    )


def critique(dimension=Dimension.SECURITY, issues=None, round=1, verdict=None) -> Critique:
    issues = [] if issues is None else issues
    if verdict is None:
        if not issues:
            verdict = "clean"
        elif any(i.severity is Severity.HIGH for i in issues):
            verdict = "block"
        else:
            verdict = "concerns"
    return Critique(
        dimension=dimension,
        round=round,
        verdict=verdict,
        issues=issues,
        tools_consulted=["bandit"],
        summary="s",
    )


def both_clean(round=1) -> tuple[Critique, ...]:
    return (
        critique(Dimension.SECURITY, round=round),
        critique(Dimension.PERFORMANCE, round=round),
    )


def state(**kw) -> PolicyInput:
    """A round where both critics reported clean. Every rule test perturbs one thing."""
    defaults = dict(round=1, critiques=both_clean())
    return PolicyInput(**{**defaults, **kw})


def oracle(failed=(), ran=True) -> TestResult:
    return TestResult(
        ran=ran,
        exit_code=1 if failed else 0,
        passed=3 - len(failed),
        failed=len(failed),
        errors=0,
        skipped=0,
        failed_node_ids=list(failed),
        duration_ms=5,
        timed_out=False,
        unavailable_reason=None if ran else "not run",
    )


# -- the table itself ------------------------------------------------------------------------


def test_the_table_matches_the_documented_specification():
    """Row order *is* the specification: row 2 sits above row 7 because a patch that fixes a
    security hole and breaks the tests is not a trade-off, it is broken."""
    assert [r.name for r in DECISION_TABLE] == [
        "input_unusable",
        "correctness_regression",
        "budget_exhausted",
        "oscillation",
        "unassessed_dimension",
        "unassessed_terminal",
        "hard_block_security",
        "irreconcilable",
        "no_progress",
        "rounds_exhausted",
        "pressure_over_threshold",
        "accept",
    ]


def test_every_row_has_a_reason_string():
    """`explain()` reads the table rather than keeping a second copy that can drift."""
    for rule in DECISION_TABLE:
        assert rule.why


# -- one test per row --------------------------------------------------------------------------


def test_row_1_input_unusable_when_the_original_does_not_parse():
    verdict = decide(state(input_parses=False), CONFIG, BUDGET)
    assert (verdict.decision, verdict.rule_fired) == (Decision.ESCALATE, "input_unusable")


def test_row_1_input_unusable_when_no_diff_ever_applied():
    verdict = decide(state(patch_attempts_exhausted=True), CONFIG, BUDGET)
    assert verdict.rule_fired == "input_unusable"


def test_row_2_correctness_regression():
    verdict = decide(
        state(baseline_tests=oracle(), patched_tests=oracle(["t.py::test_a"])),
        CONFIG,
        BUDGET,
    )
    assert (verdict.decision, verdict.rule_fired) == (Decision.REJECT, "correctness_regression")


def test_row_3_budget_exhausted():
    verdict = decide(state(spend=Spend(usd=2.0)), CONFIG, BUDGET)
    assert (verdict.decision, verdict.rule_fired) == (Decision.ESCALATE, "budget_exhausted")


@pytest.mark.parametrize(
    "spend",
    [Spend(usd=2.0), Spend(tokens=400_000), Spend(wall_seconds=600)],
    ids=["usd", "tokens", "wall"],
)
def test_row_3_every_cap_triggers(spend):
    assert decide(state(spend=spend), CONFIG, BUDGET).rule_fired == "budget_exhausted"


def test_row_4_oscillation():
    verdict = decide(
        state(round=2, diff_hash="abc", previous_diff_hashes=("abc",), pressure_history=(1.0,)),
        CONFIG,
        BUDGET,
    )
    assert (verdict.decision, verdict.rule_fired) == (Decision.ESCALATE, "oscillation")


def test_row_5_unassessed_dimension_while_rounds_remain():
    """An errored critic leaves its dimension unassessed, which is not the same as clean."""
    verdict = decide(
        state(
            round=1,
            critiques=(critique(Dimension.SECURITY),),
            errored_dimensions=(Dimension.PERFORMANCE,),
        ),
        CONFIG,
        BUDGET,
    )
    assert (verdict.decision, verdict.rule_fired) == (Decision.REJECT, "unassessed_dimension")
    assert verdict.unassessed_dimensions == [Dimension.PERFORMANCE]


def test_row_6_unassessed_on_the_final_round_escalates():
    verdict = decide(
        state(
            round=3,
            critiques=(critique(Dimension.SECURITY, round=3),),
            errored_dimensions=(Dimension.PERFORMANCE,),
            pressure_history=(0.0, 0.0),
        ),
        CONFIG,
        BUDGET,
    )
    assert (verdict.decision, verdict.rule_fired) == (Decision.ESCALATE, "unassessed_terminal")


def test_row_7_hard_block_security():
    verdict = decide(
        state(
            critiques=(
                critique(Dimension.SECURITY, [issue(severity=Severity.HIGH, confidence=0.9)]),
                critique(Dimension.PERFORMANCE),
            )
        ),
        CONFIG,
        BUDGET,
    )
    assert (verdict.decision, verdict.rule_fired) == (Decision.REJECT, "hard_block_security")


def test_row_7_is_asymmetric_a_performance_high_does_not_hard_block():
    """Stated as a value in config, not pretended away: an exploitable vulnerability has
    unbounded downside; a performance regression is recoverable and often intentional."""
    verdict = decide(
        state(
            critiques=(
                critique(Dimension.SECURITY),
                critique(
                    Dimension.PERFORMANCE,
                    [issue(id="PERF-1", dimension=Dimension.PERFORMANCE, severity=Severity.HIGH)],
                ),
            )
        ),
        CONFIG,
        BUDGET,
    )
    assert verdict.rule_fired != "hard_block_security"
    assert verdict.decision is Decision.REJECT  # still rejected, via pressure


def test_row_7_respects_the_confidence_floor():
    low_confidence = issue(severity=Severity.HIGH, confidence=0.55)
    verdict = decide(
        state(
            critiques=(
                critique(Dimension.SECURITY, [low_confidence]),
                critique(Dimension.PERFORMANCE),
            )
        ),
        CONFIG,
        BUDGET,
    )
    assert verdict.rule_fired != "hard_block_security"


def test_row_8_irreconcilable():
    left = issue(id="SEC-1", severity=Severity.HIGH, confidence=0.5)
    right = issue(id="PERF-1", dimension=Dimension.PERFORMANCE, severity=Severity.MEDIUM)
    conflict = Conflict(
        left_issue="SEC-1", right_issue="PERF-1",
        left_remedy_cost="+2 branches", right_remedy_cost="reintroduces the injection",
        axis="security_vs_performance", detector="oscillation",
    )
    verdict = decide(
        state(
            critiques=(
                critique(Dimension.SECURITY, [left]),
                critique(Dimension.PERFORMANCE, [right]),
            ),
            conflict=conflict,
        ),
        CONFIG,
        BUDGET,
    )
    assert (verdict.decision, verdict.rule_fired) == (Decision.TRADEOFF, "irreconcilable")
    assert verdict.conflict == conflict


def test_row_8_requires_both_sides_to_still_be_open():
    """A conflict whose side the Arbiter has since dismissed is not a conflict."""
    left = issue(id="SEC-1", severity=Severity.MEDIUM)
    right = issue(id="PERF-1", dimension=Dimension.PERFORMANCE, severity=Severity.MEDIUM)
    conflict = Conflict(
        left_issue="SEC-1", right_issue="PERF-1", left_remedy_cost="a", right_remedy_cost="b",
        axis="security_vs_performance", detector="same_span",
    )
    verdict = decide(
        state(
            critiques=(
                critique(Dimension.SECURITY, [left]),
                critique(Dimension.PERFORMANCE, [right]),
            ),
            conflict=conflict,
            dismissed_issue_ids=frozenset({"SEC-1"}),
        ),
        CONFIG,
        BUDGET,
    )
    assert verdict.decision is not Decision.TRADEOFF


def test_row_8_requires_both_sides_grounded():
    """docs/11-risks.md R4: a spurious trade-off ships a bad patch with an elegant
    justification attached — the worst possible output."""
    ungrounded = issue(
        id="PERF-1", dimension=Dimension.PERFORMANCE, severity=Severity.MEDIUM,
        evidence_=[evidence(kind=EvidenceKind.REASONING, ref="hunch")],
    )
    conflict = Conflict(
        left_issue="PERF-1", right_issue="SEC-1", left_remedy_cost="a", right_remedy_cost="b",
        axis="security_vs_performance", detector="same_span",
    )
    verdict = decide(
        state(
            critiques=(
                critique(Dimension.SECURITY, [issue(id="SEC-1", severity=Severity.MEDIUM)]),
                critique(Dimension.PERFORMANCE, [ungrounded]),
            ),
            conflict=conflict,
        ),
        CONFIG,
        BUDGET,
    )
    assert verdict.decision is not Decision.TRADEOFF


def test_row_9_no_progress_after_two_non_improving_rounds():
    medium = issue(id="SEC-1", severity=Severity.MEDIUM, confidence=1.0)  # score 4.0
    verdict = decide(
        state(
            round=3,
            critiques=(
                critique(Dimension.SECURITY, [medium], round=3),
                critique(Dimension.PERFORMANCE, round=3),
            ),
            pressure_history=(4.0, 4.0),
        ),
        PolicyConfig(max_rounds=5),
        BUDGET,
    )
    assert (verdict.decision, verdict.rule_fired) == (Decision.ESCALATE, "no_progress")


def test_row_10_rounds_exhausted():
    """Two MEDIUMs, not a HIGH: a security HIGH would fire row 7 first, which is the table
    working as specified rather than this row failing."""
    two_mediums = [
        issue(id="SEC-1", severity=Severity.MEDIUM, confidence=1.0),
        issue(id="SEC-2", severity=Severity.MEDIUM, confidence=1.0),
    ]
    verdict = decide(
        state(
            round=3,
            critiques=(
                critique(Dimension.SECURITY, two_mediums, round=3),
                critique(Dimension.PERFORMANCE, round=3),
            ),
            pressure_history=(20.0, 12.0),  # improving, so no_progress must not fire
        ),
        CONFIG,
        BUDGET,
    )
    assert verdict.pressure == 8.0
    assert (verdict.decision, verdict.rule_fired) == (Decision.ESCALATE, "rounds_exhausted")


def test_row_11_pressure_over_threshold():
    medium = issue(id="SEC-1", severity=Severity.MEDIUM, confidence=1.0)  # 4.0
    low = issue(id="SEC-2", severity=Severity.LOW, confidence=1.0)  # 1.0 -> total 5.0
    verdict = decide(
        state(
            critiques=(
                critique(Dimension.SECURITY, [medium, low]),
                critique(Dimension.PERFORMANCE),
            )
        ),
        CONFIG,
        BUDGET,
    )
    assert (verdict.decision, verdict.rule_fired) == (Decision.REJECT, "pressure_over_threshold")
    assert verdict.pressure == 5.0


def test_row_12_accept():
    verdict = decide(state(), CONFIG, BUDGET)
    assert (verdict.decision, verdict.rule_fired) == (Decision.ACCEPT, "accept")
    assert verdict.pressure == 0.0


def test_row_12_accepts_one_full_confidence_medium():
    """The accept threshold is 4.0, not zero. A system requiring zero open issues never
    terminates on real code."""
    medium = issue(id="SEC-1", severity=Severity.MEDIUM, confidence=1.0)
    verdict = decide(
        state(critiques=(critique(Dimension.SECURITY, [medium]), critique(Dimension.PERFORMANCE))),
        CONFIG,
        BUDGET,
    )
    assert verdict.decision is Decision.ACCEPT
    assert verdict.open_issues == ["SEC-1"]  # reported, not hidden


# -- no fall-through ---------------------------------------------------------------------------


def test_no_input_can_fall_through_the_table():
    """The last row's condition is unconditionally true, so fall-through is structurally
    impossible rather than merely untested."""
    ctx = Context(state=state(), config=CONFIG, budget=BUDGET)
    assert DECISION_TABLE[-1].condition(ctx) is True
    assert DECISION_TABLE[-1].name == "accept"


@pytest.mark.parametrize("round_", [1, 2, 3, 4])
@pytest.mark.parametrize("parses", [True, False])
@pytest.mark.parametrize("errored", [(), (Dimension.PERFORMANCE,)])
@pytest.mark.parametrize("severity", list(Severity))
def test_every_combination_produces_a_named_rule(round_, parses, errored, severity):
    """A crude cartesian sweep. Any input at all yields a decision and a rule name — charter
    criterion S1 at the policy layer."""
    verdict = decide(
        PolicyInput(
            round=round_,
            critiques=(
                critique(Dimension.SECURITY, [issue(severity=severity)], round=round_),
                critique(Dimension.PERFORMANCE, round=round_),
            ),
            errored_dimensions=errored,
            input_parses=parses,
            pressure_history=tuple(5.0 for _ in range(round_ - 1)),
        ),
        CONFIG,
        BUDGET,
    )
    assert verdict.rule_fired in {r.name for r in DECISION_TABLE}
    assert verdict.decision in set(Decision)


# -- ordering ------------------------------------------------------------------------------------


def test_correctness_beats_a_security_hard_block():
    """Row 2 above row 7. A patch that fixes a security hole and breaks the tests is broken,
    not a trade-off."""
    verdict = decide(
        state(
            critiques=(critique(Dimension.SECURITY, [issue()]), critique(Dimension.PERFORMANCE)),
            baseline_tests=oracle(),
            patched_tests=oracle(["t.py::test_a"]),
        ),
        CONFIG,
        BUDGET,
    )
    assert verdict.rule_fired == "correctness_regression"


def test_unassessed_beats_a_hard_block():
    """Rows 5/6 above row 7: you cannot conclude anything about a dimension nobody assessed."""
    verdict = decide(
        state(
            critiques=(critique(Dimension.SECURITY, [issue()]),),
            errored_dimensions=(Dimension.PERFORMANCE,),
        ),
        CONFIG,
        BUDGET,
    )
    assert verdict.rule_fired == "unassessed_dimension"


def test_a_hard_block_beats_a_conflict():
    """Row 7 above row 8: an exploitable HIGH is not something to trade away."""
    conflict = Conflict(
        left_issue="SEC-1", right_issue="PERF-1", left_remedy_cost="a", right_remedy_cost="b",
        axis="security_vs_performance", detector="same_span",
    )
    verdict = decide(
        state(
            critiques=(
                critique(Dimension.SECURITY, [issue(id="SEC-1", severity=Severity.HIGH)]),
                critique(
                    Dimension.PERFORMANCE,
                    [issue(id="PERF-1", dimension=Dimension.PERFORMANCE, severity=Severity.MEDIUM)],
                ),
            ),
            conflict=conflict,
        ),
        CONFIG,
        BUDGET,
    )
    assert verdict.rule_fired == "hard_block_security"


def test_budget_beats_everything_below_it():
    verdict = decide(
        state(
            critiques=(critique(Dimension.SECURITY, [issue()]), critique(Dimension.PERFORMANCE)),
            spend=Spend(usd=5.0),
        ),
        CONFIG,
        BUDGET,
    )
    assert verdict.rule_fired == "budget_exhausted"


# -- pressure --------------------------------------------------------------------------------


def test_pressure_is_monotone_in_severity():
    high = pressure([critique(Dimension.SECURITY, [issue(severity=Severity.HIGH, confidence=1.0)])])
    lows = pressure(
        [
            critique(
                Dimension.SECURITY,
                [
                    issue(id=f"SEC-{n}", severity=Severity.LOW, confidence=1.0,
                          evidence_=[evidence(ref=f"f{n}")])
                    for n in range(4)
                ],
                verdict="concerns",
            )
        ]
    )
    assert high > lows  # one HIGH outweighs four LOWs


def test_an_ungrounded_issue_counts_half():
    grounded = pressure([critique(Dimension.SECURITY, [issue(confidence=1.0)])])
    ungrounded = pressure(
        [
            critique(
                Dimension.SECURITY,
                [issue(confidence=1.0, evidence_=[evidence(kind=EvidenceKind.REASONING, ref="x")])],
            )
        ]
    )
    assert ungrounded == grounded / 2
    assert ungrounded > CONFIG.accept_threshold  # can still block


def test_dismissed_issues_leave_the_sum_permanently():
    c = [critique(Dimension.SECURITY, [issue(confidence=1.0)])]
    assert pressure(c) == 16.0
    assert pressure(c, frozenset({"SEC-1"})) == 0.0


# -- duplicate collapsing ----------------------------------------------------------------------


def test_the_same_defect_reported_twice_counts_once():
    """Measured: the first live Red-team run emitted one `shell=True` defect as two issues —
    one citing bandit, one citing ruff — and pressure came out at 45.6 on a one-bug file. Two
    tools agreeing is the strongest evidence available; letting it double the score inverts
    the signal."""
    shared = evidence(ref="bandit-B602")
    duplicated = critique(
        Dimension.SECURITY,
        [
            issue(id="SEC-1", confidence=1.0, evidence_=[shared]),
            issue(id="SEC-2", confidence=1.0, evidence_=[shared]),
        ],
    )
    assert pressure([duplicated]) == 16.0
    assert duplicates_in([duplicated]) == ["SEC-2"]


def test_cross_dimension_agreement_is_not_a_duplicate():
    """The Red-team and the Profiler flagging the same line is genuine disagreement — very
    possibly the conflict that produces a TRADEOFF. Collapsing it would delete the headline
    feature."""
    shared = evidence(ref="line-41")
    both = [
        critique(Dimension.SECURITY, [issue(id="SEC-1", confidence=1.0, evidence_=[shared])]),
        critique(
            Dimension.PERFORMANCE,
            [
                issue(
                    id="PERF-1", dimension=Dimension.PERFORMANCE, confidence=1.0,
                    evidence_=[shared],
                )
            ],
        ),
    ]
    assert pressure(both) == 32.0
    assert duplicates_in(both) == []


def test_two_distinct_concerns_about_one_line_are_not_duplicates():
    """Collapsing is by shared evidence, not by topic: a critic can legitimately raise two
    different concerns about the same region."""
    pair = critique(
        Dimension.SECURITY,
        [
            issue(id="SEC-1", confidence=1.0, evidence_=[evidence(ref="f1")]),
            issue(id="SEC-2", confidence=1.0, evidence_=[evidence(ref="f2")]),
        ],
    )
    assert pressure([pair]) == 32.0


def test_collapsing_keeps_the_higher_scoring_issue():
    shared = evidence(ref="shared")
    pair = critique(
        Dimension.SECURITY,
        [
            issue(id="SEC-low", severity=Severity.LOW, confidence=1.0, evidence_=[shared]),
            issue(id="SEC-high", severity=Severity.HIGH, confidence=1.0, evidence_=[shared]),
        ],
    )
    assert pressure([pair]) == 16.0
    assert duplicates_in([pair]) == ["SEC-low"]


# -- the correctness oracle ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ((), ("t::b",), ["t::b"]),
        (("t::a",), ("t::a",), []),  # already failing: the bug we were asked to fix
        (("t::a",), ("t::a", "t::b"), ["t::b"]),
        (("t::a",), (), []),
    ],
)
def test_only_new_failures_count_as_a_regression(before, after, expected):
    assert regressed_tests(oracle(before), oracle(after)) == expected


def test_an_absent_oracle_is_not_evidence_of_a_regression():
    assert regressed_tests(None, oracle(["t::a"])) == []
    assert regressed_tests(oracle(), None) == []
    assert regressed_tests(oracle(ran=False), oracle(["t::a"])) == []


# -- docs/04 § Failure-mode checklist -------------------------------------------------------------


def test_both_critics_clean_on_round_one_accepts_immediately():
    """Not a pointless round 2."""
    verdict = decide(state(), CONFIG, BUDGET)
    assert verdict.decision is Decision.ACCEPT
    assert verdict.round == 1


def test_both_critics_errored_escalates_and_never_accepts():
    for round_ in (1, 3):
        verdict = decide(
            PolicyInput(
                round=round_,
                critiques=(),
                errored_dimensions=(Dimension.SECURITY, Dimension.PERFORMANCE),
                pressure_history=tuple(0.0 for _ in range(round_ - 1)),
            ),
            CONFIG,
            BUDGET,
        )
        assert verdict.decision is not Decision.ACCEPT
        assert set(verdict.unassessed_dimensions) == {Dimension.SECURITY, Dimension.PERFORMANCE}


def test_a_missing_critique_counts_as_unassessed_even_without_an_error():
    """A dimension that simply never ran is as unassessed as one that crashed."""
    verdict = decide(
        PolicyInput(round=1, critiques=(critique(Dimension.SECURITY),)), CONFIG, BUDGET
    )
    assert verdict.unassessed_dimensions == [Dimension.PERFORMANCE]
    assert verdict.decision is not Decision.ACCEPT


def test_an_empty_diff_with_low_pressure_accepts():
    """The Coder claiming nothing to fix is a legitimate position."""
    verdict = decide(state(patch_is_empty=True), CONFIG, BUDGET)
    assert verdict.decision is Decision.ACCEPT


def test_an_empty_diff_with_high_pressure_rejects():
    """docs/04 § Failure-mode checklist: "if the pressure was above threshold, that's a
    REJECT with the Coder's reasoning recorded as pushback"."""
    verdict = decide(
        state(
            patch_is_empty=True,
            critiques=(
                critique(Dimension.SECURITY, [issue(severity=Severity.MEDIUM, confidence=1.0)]),
                critique(
                    Dimension.PERFORMANCE,
                    [
                        issue(
                            id="PERF-1", dimension=Dimension.PERFORMANCE,
                            severity=Severity.MEDIUM, confidence=1.0,
                        )
                    ],
                ),
            ),
        ),
        CONFIG,
        BUDGET,
    )
    assert verdict.decision is Decision.REJECT
    assert verdict.rule_fired == "pressure_over_threshold"


def test_no_progress_cannot_fire_on_round_one():
    """The obvious bug is an index error; the subtle one is treating a missing previous value
    as 0 and escalating. There is no previous value on round 1, so there is nothing to
    compare — expressed as a length check, not a default."""
    verdict = decide(
        state(
            critiques=(
                critique(Dimension.SECURITY, [issue(severity=Severity.MEDIUM, confidence=1.0)]),
                critique(Dimension.PERFORMANCE),
            ),
            pressure_history=(),
        ),
        CONFIG,
        BUDGET,
    )
    assert verdict.rule_fired != "no_progress"
    assert verdict.pressure_history == [4.0]


def test_no_progress_needs_two_consecutive_non_improving_rounds():
    """One bad round is normal: the Coder fixes the HIGH and the critics, now unblocked, find
    MEDIUMs they had not reached."""
    rising_once = decide(
        state(
            round=2,
            critiques=(
                critique(
                    Dimension.SECURITY, [issue(severity=Severity.MEDIUM, confidence=1.0)], round=2
                ),
                critique(Dimension.PERFORMANCE, round=2),
            ),
            pressure_history=(20.0,),
        ),
        PolicyConfig(max_rounds=5),
        BUDGET,
    )
    assert rising_once.rule_fired != "no_progress"


def test_a_verdict_can_never_accept_with_an_unassessed_dimension():
    """Enforced twice: rows 5/6 catch it, and the `Verdict` schema refuses the combination."""
    verdict = decide(
        state(
            critiques=(critique(Dimension.SECURITY),),
            errored_dimensions=(Dimension.PERFORMANCE,),
        ),
        CONFIG,
        BUDGET,
    )
    assert not (verdict.decision is Decision.ACCEPT and verdict.unassessed_dimensions)


# -- conflict detection ---------------------------------------------------------------------------


def span(ref: str) -> Evidence:
    return Evidence(kind=EvidenceKind.CODE_SPAN, ref=ref, excerpt="")


def test_detector_2_finds_opposing_issues_on_overlapping_spans():
    left = issue(id="SEC-1", severity=Severity.MEDIUM, evidence_=[span("t.py:L10-L20")])
    right = issue(
        id="PERF-1", dimension=Dimension.PERFORMANCE, severity=Severity.MEDIUM,
        evidence_=[span("t.py:L15-L25")],
    )
    critiques = [critique(Dimension.SECURITY, [left]), critique(Dimension.PERFORMANCE, [right])]
    conflict = detect_same_span_conflict(critiques, arbiter_affirms=True)
    assert conflict is not None
    assert {conflict.left_issue, conflict.right_issue} == {"SEC-1", "PERF-1"}
    assert conflict.axis == "security_vs_performance"
    assert conflict.detector == "same_span"


def test_detector_2_requires_the_arbiter_to_affirm():
    """Weak evidence, so it needs corroboration. A non-affirmation falls through to row 9/11,
    which is the safe default."""
    left = issue(id="SEC-1", severity=Severity.MEDIUM, evidence_=[span("t.py:L10-L20")])
    right = issue(id="PERF-1", dimension=Dimension.PERFORMANCE, severity=Severity.MEDIUM,
                  evidence_=[span("t.py:L15-L25")])
    critiques = [critique(Dimension.SECURITY, [left]), critique(Dimension.PERFORMANCE, [right])]
    assert detect_same_span_conflict(critiques, arbiter_affirms=False) is None


def test_detector_2_ignores_non_overlapping_spans():
    left = issue(id="SEC-1", severity=Severity.MEDIUM, evidence_=[span("t.py:L1-L5")])
    right = issue(id="PERF-1", dimension=Dimension.PERFORMANCE, severity=Severity.MEDIUM,
                  evidence_=[span("t.py:L90-L95")])
    critiques = [critique(Dimension.SECURITY, [left]), critique(Dimension.PERFORMANCE, [right])]
    assert detect_same_span_conflict(critiques, arbiter_affirms=True) is None


def test_detector_2_ignores_low_severity_and_ungrounded_issues():
    """A trade-off over two LOWs is noise with a ceremony attached."""
    low_left = issue(id="SEC-1", severity=Severity.LOW, evidence_=[span("t.py:L10-L20")])
    right = issue(id="PERF-1", dimension=Dimension.PERFORMANCE, severity=Severity.MEDIUM,
                  evidence_=[span("t.py:L15-L25")])
    low = [critique(Dimension.SECURITY, [low_left]), critique(Dimension.PERFORMANCE, [right])]
    assert detect_same_span_conflict(low, arbiter_affirms=True) is None

    hunch = issue(id="SEC-2", severity=Severity.MEDIUM,
                  evidence_=[evidence(kind=EvidenceKind.REASONING, ref="x")])
    ungrounded = [critique(Dimension.SECURITY, [hunch]), critique(Dimension.PERFORMANCE, [right])]
    assert detect_same_span_conflict(ungrounded, arbiter_affirms=True) is None


def test_detector_2_ignores_two_issues_from_the_same_critic():
    """A conflict is between dimensions. One critic disagreeing with itself is incoherence,
    not a trade-off."""
    a = issue(id="SEC-1", severity=Severity.MEDIUM, evidence_=[span("t.py:L10-L20")])
    b = issue(id="SEC-2", severity=Severity.MEDIUM, evidence_=[span("t.py:L15-L25")])
    same = [critique(Dimension.SECURITY, [a, b]), critique(Dimension.PERFORMANCE)]
    assert detect_same_span_conflict(same, arbiter_affirms=True) is None


def test_detector_1_needs_two_rounds_of_evidence():
    current = [critique(Dimension.SECURITY, [issue(id="SEC-1", severity=Severity.MEDIUM)])]
    assert detect_oscillation_conflict(current, []) is None
    assert detect_oscillation_conflict(current, [RoundRecord(1, frozenset({"SEC-1"}))]) is None


def test_detector_1_fires_when_a_fixed_issue_returns_across_a_regression():
    """Round r fixes the security issue and regresses performance; round r+1 fixes the
    performance regression and the security issue comes back with the same id. Two rounds of
    evidence that the remedies exclude each other."""
    returning = issue(id="SEC-1", severity=Severity.HIGH)
    counterpart = issue(id="PERF-1", dimension=Dimension.PERFORMANCE, severity=Severity.MEDIUM,
                        evidence_=[evidence(ref="perf-finding")])
    current = [
        critique(Dimension.SECURITY, [returning]),
        critique(Dimension.PERFORMANCE, [counterpart]),
    ]
    history = [
        RoundRecord(1, frozenset({"SEC-1"})),
        RoundRecord(2, frozenset({"PERF-1"}), regression_observed=True),
    ]
    conflict = detect_oscillation_conflict(current, history)
    assert conflict is not None
    assert conflict.detector == "oscillation"
    assert {conflict.left_issue, conflict.right_issue} == {"SEC-1", "PERF-1"}


def test_detector_1_requires_a_measured_regression_in_the_intervening_round():
    current = [
        critique(Dimension.SECURITY, [issue(id="SEC-1", severity=Severity.HIGH)]),
        critique(Dimension.PERFORMANCE, [issue(id="PERF-1", dimension=Dimension.PERFORMANCE,
                                               severity=Severity.MEDIUM,
                                               evidence_=[evidence(ref="p")])]),
    ]
    history = [
        RoundRecord(1, frozenset({"SEC-1"})),
        RoundRecord(2, frozenset({"PERF-1"}), regression_observed=False),
    ]
    assert detect_oscillation_conflict(current, history) is None


# -- explain ---------------------------------------------------------------------------------------


def test_explain_is_the_jq_one_liner_in_prose():
    verdict = decide(state(input_parses=False), CONFIG, BUDGET)
    line = explain(verdict)
    assert "escalate" in line
    assert "input_unusable" in line
    assert "does not parse" in line


def test_a_custom_hard_block_rule_is_honoured():
    """The asymmetry is config, not a constant buried in the code."""
    perf_blocks = PolicyConfig(
        hard_block=(
            HardBlockRule(
                dimension=Dimension.PERFORMANCE, severity=Severity.HIGH, min_confidence=0.6
            ),
        )
    )
    verdict = decide(
        state(
            critiques=(
                critique(Dimension.SECURITY),
                critique(
                    Dimension.PERFORMANCE,
                    [
                        issue(
                            id="PERF-1", dimension=Dimension.PERFORMANCE, severity=Severity.HIGH
                        )
                    ],
                ),
            )
        ),
        perf_blocks,
        BUDGET,
    )
    assert verdict.rule_fired == "hard_block_security"
