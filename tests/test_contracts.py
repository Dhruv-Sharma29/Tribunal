"""The schemas are the enforcement mechanism, so the guards get tested like code.

"Ground your critiques" is a prompt instruction until `Issue.evidence` has `min_length=1`, at
which point it is a validation error. These tests pin the guards that turn design rules from
docs/02-contracts.md into failures.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tribunal.contracts import (
    ArbiterNote,
    Conflict,
    Critique,
    Decision,
    Dimension,
    Evidence,
    EvidenceKind,
    Issue,
    PatchProposal,
    PerfMeasurement,
    SearchReplaceEdit,
    Severity,
    TestResult,
    UnaddressedIssue,
    Verdict,
    canonical_issue_id,
    quantise_confidence,
)


def evidence(kind=EvidenceKind.TOOL_FINDING, ref="abc1234567") -> Evidence:
    return Evidence(kind=kind, ref=ref, excerpt="bandit B602 at line 34")


def issue(**kwargs) -> Issue:
    defaults = dict(
        id="SEC-0000000001",
        dimension=Dimension.SECURITY,
        severity=Severity.HIGH,
        title="shell=True on attacker-controlled input",
        explanation="The command string is concatenated from a request parameter.",
        evidence=[evidence()],
        confidence=0.8,
        introduced_by_patch=True,
        suggested_direction="pass a list of args and drop shell=True",
    )
    return Issue(**{**defaults, **kwargs})


def critique(**kwargs) -> Critique:
    defaults = dict(
        dimension=Dimension.SECURITY,
        round=1,
        verdict="block",
        issues=[issue()],
        tools_consulted=["bandit", "ruff"],
        summary="One high-severity command injection.",
    )
    return Critique(**{**defaults, **kwargs})


# -- Severity weights ------------------------------------------------------------------------


def test_severity_weights_are_defined_once():
    assert [s.weight for s in Severity] == [0, 1, 4, 16]


def test_a_high_outweighs_four_lows():
    """Monotone in severity: trading one HIGH for four LOWs must reduce pressure."""
    assert Severity.HIGH.weight > 4 * Severity.LOW.weight


# -- Evidence and grounding ------------------------------------------------------------------


def test_an_issue_cannot_exist_without_evidence():
    with pytest.raises(ValidationError, match="evidence"):
        issue(evidence=[])


def test_reasoning_evidence_is_legal_but_ungrounded():
    """Ungrounded is allowed and penalised, not forbidden: a critic must be able to raise
    something a tool cannot see, it just cannot dominate on it."""
    ungrounded = issue(evidence=[evidence(kind=EvidenceKind.REASONING, ref="n/a")])
    assert not ungrounded.grounded
    assert ungrounded.score == pytest.approx(16 * 0.8 * 0.5)


def test_an_ungrounded_high_can_still_block_but_cannot_dominate():
    """docs/04 § Pressure: an ungrounded HIGH at confidence 1.0 scores 8.0 -- above the 4.0
    accept threshold, below a grounded HIGH."""
    ungrounded = issue(confidence=1.0, evidence=[evidence(kind=EvidenceKind.REASONING, ref="x")])
    grounded = issue(confidence=1.0)
    assert ungrounded.score == 8.0
    assert grounded.score == 16.0


def test_mixed_evidence_counts_as_grounded():
    mixed = issue(evidence=[evidence(kind=EvidenceKind.REASONING, ref="x"), evidence()])
    assert mixed.grounded


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("patched.py:L41-L47", (41, 47)),
        ("patched.py:41", (41, 41)),
        ("target.py:L7", (7, 7)),
        ("bandit:B602", None),  # a finding id, not a span
        ("abc1234567", None),
    ],
)
def test_evidence_span_parsing(ref, expected):
    """Conflict detector 2 needs to know whether two critics point at the same lines."""
    assert evidence(ref=ref).span == expected


def test_evidence_is_frozen():
    with pytest.raises(ValidationError):
        evidence().ref = "mutated"


# -- Confidence quantisation -----------------------------------------------------------------


@pytest.mark.parametrize("value", [0.0, 0.05, 0.15, 0.6, 0.85, 1.0])
def test_legal_confidences_are_accepted(value):
    assert issue(confidence=value).confidence == value


@pytest.mark.parametrize("value", [0.07, 0.123, 0.99])
def test_off_grid_confidence_is_rejected_loudly(value):
    """Rejected rather than snapped: the failure surfaces as a parse retry, and the retry rate
    is the prompt-health signal the eval tracks."""
    with pytest.raises(ValidationError, match="multiple"):
        issue(confidence=value)


@pytest.mark.parametrize(
    ("raw", "snapped"),
    [(0.07, 0.05), (0.123, 0.1), (0.99, 1.0), (-1, 0.0), (2, 1.0)],
)
def test_quantise_is_available_for_the_repair_pass(raw, snapped):
    assert quantise_confidence(raw) == snapped


# -- Critique self-consistency ---------------------------------------------------------------


def test_block_requires_a_high_severity_issue():
    """A critic that blocks on a LOW is mis-calibrated; the parse fails rather than the policy
    inheriting a bad signal."""
    with pytest.raises(ValidationError, match="at least one HIGH"):
        critique(issues=[issue(severity=Severity.LOW)])


def test_clean_requires_an_empty_issue_list():
    with pytest.raises(ValidationError, match="empty issue list"):
        critique(verdict="clean")


def test_concerns_requires_at_least_one_issue():
    with pytest.raises(ValidationError, match="at least one issue"):
        critique(verdict="concerns", issues=[])


def test_a_clean_critique_is_valid():
    assert critique(verdict="clean", issues=[]).issues == []


def test_tools_consulted_cannot_be_empty():
    """An empty list would mean a critique with no grounding at all."""
    with pytest.raises(ValidationError):
        critique(tools_consulted=[])


def test_positive_notes_are_capped():
    with pytest.raises(ValidationError):
        critique(positive_notes=["a", "b", "c", "d"])


# -- Issue identity --------------------------------------------------------------------------


def test_issue_ids_are_stable_and_prefixed():
    first = canonical_issue_id(Dimension.SECURITY, "B602", "vulnerable.py:8")
    assert first == canonical_issue_id(Dimension.SECURITY, "B602", "vulnerable.py:8")
    assert first.startswith("SEC-")
    assert canonical_issue_id(Dimension.PERFORMANCE, "CC-C", "x.py:4").startswith("PERF-")


def test_different_content_gets_a_different_id():
    """docs/04 § Failure-mode checklist: an id collision across rounds with different content
    is a real bug, so the derivation must include enough context to separate them."""
    ids = {
        canonical_issue_id(Dimension.SECURITY, "B602", "a.py:8"),
        canonical_issue_id(Dimension.SECURITY, "B608", "a.py:8"),
        canonical_issue_id(Dimension.SECURITY, "B602", "a.py:9"),
        canonical_issue_id(Dimension.PERFORMANCE, "B602", "a.py:8"),
    }
    assert len(ids) == 4


# -- Verdict invariants ----------------------------------------------------------------------


def verdict(**kwargs) -> Verdict:
    defaults = dict(
        round=1,
        decision=Decision.ACCEPT,
        rule_fired="accept",
        pressure=1.0,
        pressure_history=[1.0],
        open_issues=[],
        unassessed_dimensions=[],
        conflict=None,
    )
    return Verdict(**{**defaults, **kwargs})


def test_accept_can_never_coexist_with_an_unassessed_dimension():
    """The single most likely real bug in any parallel-critic design: a critic times out, the
    surviving critiques sum to low pressure, and the system ships. Made unrepresentable."""
    with pytest.raises(ValidationError, match="unassessed"):
        verdict(decision=Decision.ACCEPT, unassessed_dimensions=[Dimension.PERFORMANCE])


def test_a_reject_may_carry_unassessed_dimensions():
    assert verdict(
        decision=Decision.REJECT,
        rule_fired="unassessed_dimension",
        unassessed_dimensions=[Dimension.PERFORMANCE],
    ).decision is Decision.REJECT


def test_conflict_is_set_exactly_when_the_decision_is_tradeoff():
    conflict = Conflict(
        left_issue="SEC-1",
        right_issue="PERF-1",
        left_remedy_cost="+2 branches",
        right_remedy_cost="reintroduces the injection",
        axis="security_vs_performance",
        detector="oscillation",
    )
    with pytest.raises(ValidationError, match="iff decision is TRADEOFF"):
        verdict(decision=Decision.TRADEOFF, rule_fired="irreconcilable", conflict=None)
    with pytest.raises(ValidationError, match="iff decision is TRADEOFF"):
        verdict(decision=Decision.REJECT, conflict=conflict)
    assert verdict(decision=Decision.TRADEOFF, rule_fired="irreconcilable", conflict=conflict)


# -- ArbiterNote: prose follows the decision, never leads it ---------------------------------


def note(**kwargs) -> ArbiterNote:
    defaults = dict(
        round=1,
        decision_echo=Decision.REJECT,
        consolidated_critique="1) drop shell=True 2) keep the parameterised query",
        priority_order=["SEC-1"],
        dismissed=[],
        tradeoff_justification=None,
        recommended_default=None,
    )
    return ArbiterNote(**{**defaults, **kwargs})


def test_reject_prose_requires_a_consolidated_critique():
    with pytest.raises(ValidationError, match="consolidated_critique"):
        note(consolidated_critique=None)


def test_accept_prose_must_not_carry_a_consolidated_critique():
    with pytest.raises(ValidationError, match="consolidated_critique"):
        note(decision_echo=Decision.ACCEPT)


def test_tradeoff_prose_requires_a_justification_and_a_default():
    with pytest.raises(ValidationError, match="tradeoff_justification"):
        note(decision_echo=Decision.TRADEOFF, consolidated_critique=None)
    with pytest.raises(ValidationError, match="recommended_default"):
        note(
            decision_echo=Decision.TRADEOFF,
            consolidated_critique=None,
            tradeoff_justification="security wins here",
        )
    assert note(
        decision_echo=Decision.TRADEOFF,
        consolidated_critique=None,
        tradeoff_justification="security wins here",
        recommended_default="ship the validated version",
    )


# -- Measurements ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("verdict_", "citable"),
    [("faster", True), ("slower", True), ("inconclusive", False), ("unmeasurable", False)],
)
def test_only_a_real_measurement_is_citable(verdict_, citable):
    """The Profiler must refuse to cite an inconclusive measurement as evidence, so the
    predicate lives in the schema rather than in the prompt."""
    m = PerfMeasurement(
        label="timeit:x", before_ns=1, after_ns=2, repeats=9, stdev_ns=0, verdict=verdict_
    )
    assert m.is_citable is citable


def test_test_result_reason_is_present_exactly_when_it_did_not_run():
    with pytest.raises(ValidationError, match="unavailable_reason"):
        TestResult(
            ran=False, exit_code=None, passed=0, failed=0, errors=0, skipped=0,
            failed_node_ids=[], duration_ms=None, timed_out=False, unavailable_reason=None,
        )
    with pytest.raises(ValidationError, match="unavailable_reason"):
        TestResult(
            ran=True, exit_code=0, passed=1, failed=0, errors=0, skipped=0,
            failed_node_ids=[], duration_ms=5, timed_out=False, unavailable_reason="huh",
        )


def test_a_timed_out_suite_is_not_all_passed():
    timed_out = TestResult(
        ran=True, exit_code=None, passed=0, failed=0, errors=0, skipped=0,
        failed_node_ids=[], duration_ms=30000, timed_out=True, unavailable_reason=None,
    )
    assert not timed_out.all_passed


# -- PatchProposal ---------------------------------------------------------------------------


def proposal(**kwargs) -> PatchProposal:
    defaults = dict(
        round=1, edits=[], diff="", rationale="fix it", addresses=["SEC-1"],
        deliberately_unaddressed=[],
    )
    return PatchProposal(**{**defaults, **kwargs})


def test_two_representations_of_one_change_are_rejected():
    with pytest.raises(ValidationError, match="not both"):
        proposal(edits=[SearchReplaceEdit(search="a", replace="b")], diff="@@ -1 +1 @@")


def test_an_empty_proposal_is_legal_pushback():
    """The Coder claiming there is nothing to fix is a position policy handles, not a malformed
    patch."""
    assert proposal().is_empty


def test_pushback_carries_a_reason():
    p = proposal(
        deliberately_unaddressed=[
            UnaddressedIssue(issue_id="SEC-1", reason="input is validated at line 12")
        ]
    )
    assert p.deliberately_unaddressed[0].reason


def test_every_model_forbids_unknown_fields():
    """`extra="forbid"` is what makes the generated JSON schema usable for structured outputs
    (`additionalProperties: false` plus a complete `required` list)."""
    with pytest.raises(ValidationError, match="Extra inputs"):
        issue(severity_v2="high")
