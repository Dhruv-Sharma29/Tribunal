"""The Arbiter: the note it writes, and the affirmation detector 2 needs from it.

Runs against a scripted fake provider, so zero API calls.

The Arbiter is the one agent whose *output is not the interesting part*. Its prose is
downstream of a decision already made, and the tests that matter are the ones proving it
stays there: that a note disagreeing with the verdict is rejected rather than recorded, that
it cannot dismiss an issue nobody contested, and that an affirmation about the wrong pair
never reaches `policy.decide`.

Those three are not stylistic. Each is a route by which an LLM could re-enter the decision
layer that docs/01-architecture.md § Rule 1 keeps it out of.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_llm_client import FakeProvider
from tribunal.agents import AffirmationBundle, Arbiter, ArbiterBundle, ConflictAffirmer
from tribunal.agents.prompts import available_roles, load
from tribunal.agents.validation import (
    EvidenceError,
    validate_affirmation,
    validate_arbiter_note,
)
from tribunal.config import AgentConfig, AgentsConfig, LLMConfig, Settings
from tribunal.contracts import (
    ArbiterNote,
    Conflict,
    Critique,
    Decision,
    Dimension,
    Evidence,
    EvidenceKind,
    Issue,
    Patch,
    Severity,
    UnaddressedIssue,
    Verdict,
)
from tribunal.llm.base import ProviderName
from tribunal.llm.client import LLMClient

SOURCE = '''import subprocess


def run_report(name):
    subprocess.run("generate " + name, shell=True)
    return name
'''

SEC = "SEC-1111111111"
PERF = "PERF-222222222"


# -- fixtures ---------------------------------------------------------------------------------


def issue(
    id_: str = SEC,
    dimension: Dimension = Dimension.SECURITY,
    severity: Severity = Severity.HIGH,
    span: str = "target.py:L5-L5",
    direction: str = "pass an argv list",
) -> Issue:
    return Issue(
        id=id_,
        dimension=dimension,
        severity=severity,
        title=f"a {severity.value} {dimension.value} issue",
        explanation="it is bad, for reasons the tool cannot see",
        evidence=[Evidence(kind=EvidenceKind.CODE_SPAN, ref=span, excerpt="...")],
        confidence=0.9,
        introduced_by_patch=False,
        suggested_direction=direction,
    )


def critique(dimension=Dimension.SECURITY, issues=None, verdict="block") -> Critique:
    return Critique(
        dimension=dimension,
        round=1,
        verdict=verdict,
        issues=[issue()] if issues is None else issues,
        tools_consulted=["bandit"] if dimension is Dimension.SECURITY else ["radon"],
        summary="a summary",
    )


def verdict(
    decision: Decision = Decision.REJECT,
    open_issues: list[str] | None = None,
    conflict: Conflict | None = None,
    **kw: Any,
) -> Verdict:
    defaults = dict(
        round=1,
        decision=decision,
        rule_fired="hard_block_security",
        pressure=15.2,
        pressure_history=[],
        open_issues=[SEC] if open_issues is None else open_issues,
        unassessed_dimensions=[],
        conflict=conflict,
    )
    return Verdict(**{**defaults, **kw})


def patch(unaddressed: list[UnaddressedIssue] | None = None) -> Patch:
    return Patch(
        round=1,
        diff="--- a/target.py\n+++ b/target.py\n@@ -5 +5 @@\n-old\n+new\n",
        rationale="swapped the shell call for an argv list",
        addresses=[SEC],
        deliberately_unaddressed=unaddressed or [],
    )


def bundle(**kw: Any) -> ArbiterBundle:
    defaults = dict(
        round=1,
        filename="target.py",
        verdict=verdict(),
        critiques=(critique(),),
        patch=patch(),
    )
    return ArbiterBundle(**{**defaults, **kw})


def note(**kw: Any) -> ArbiterNote:
    defaults = dict(
        round=1,
        decision_echo=Decision.REJECT,
        consolidated_critique="Fix the shell call first.",
        priority_order=[SEC],
        dismissed=[],
        tradeoff_justification=None,
        recommended_default=None,
    )
    return ArbiterNote(**{**defaults, **kw})


def unopposed_pair() -> tuple[Issue, Issue]:
    """Two real defects on one line whose remedies do not exclude each other.

    Shared with the live test, because "does this model affirm anything put in front of it?"
    is the question detector 2 lives or dies on and the fixture should not drift between the
    offline and live halves of the answer.
    """
    left = issue(
        id_=SEC,
        span="target.py:L7-L7",
        direction="pass an argv list instead of shell=True",
    )
    right = issue(
        id_=PERF,
        dimension=Dimension.PERFORMANCE,
        severity=Severity.MEDIUM,
        span="target.py:L7-L7",
        direction="append to the list instead of rebuilding it with +",
    )
    return left, right


def affirmation_bundle(**kw: Any) -> AffirmationBundle:
    defaults = dict(
        round=1,
        filename="target.py",
        source=SOURCE,
        left=issue(),
        right=issue(id_=PERF, dimension=Dimension.PERFORMANCE, severity=Severity.MEDIUM,
                    span="target.py:L5-L6", direction="hoist the call out of the loop"),
    )
    return AffirmationBundle(**{**defaults, **kw})


# -- prompts ----------------------------------------------------------------------------------


def test_both_arbiter_prompts_exist_and_are_versioned():
    assert set(available_roles()) >= {"arbiter", "arbiter_affirm"}
    for role in ("arbiter", "arbiter_affirm"):
        prompt = load(role)
        assert prompt.stamp == f"{role}/v1"
        assert prompt.changed != "unknown"
        assert prompt.note


def test_the_affirmation_is_a_separate_prompt_version_from_the_note():
    """Folding them into one role would make a wording change to the synthesis read, in the
    eval, as a change in conflict sensitivity."""
    assert load("arbiter").stamp != load("arbiter_affirm").stamp


def test_the_note_prompt_states_that_the_arbiter_does_not_decide():
    text = load("arbiter").text
    assert "You do not decide anything" in text
    assert "already been made" in text


def test_the_affirmation_prompt_leads_with_the_non_examples():
    """docs/11-risks.md R4: a spurious TRADEOFF is the worst output the system can produce,
    and the first thing a model does with "are these in tension?" is say yes."""
    text = load("arbiter_affirm").text
    assert "usually is" in text
    assert "burden of proof is on `true`" in text


# -- the bundle -------------------------------------------------------------------------------


def test_the_render_leads_with_the_decision_and_says_it_is_fixed():
    text = bundle().render()
    assert "THE DECISION IS ALREADY MADE" in text
    assert "decision: reject" in text
    assert "rule fired: hard_block_security" in text
    assert "cannot change it" in text


def test_a_clean_critic_is_shown_as_a_result_rather_than_omitted():
    """A synthesis that only sees complaints cannot tell "looked, found nothing" from
    "never ran", and those mean opposite things for the next round."""
    text = bundle(
        critiques=(critique(), critique(dimension=Dimension.PERFORMANCE, issues=[],
                                        verdict="clean")),
    ).render()
    assert "looked and found nothing: performance" in text


def test_an_unassessed_dimension_is_never_described_as_clean():
    text = bundle(
        verdict=verdict(unassessed_dimensions=[Dimension.PERFORMANCE]),
    ).render()
    assert "UNASSESSED" in text
    assert "Do not describe them as clean" in text


def test_the_tradeoff_render_asks_for_the_justification_and_nothing_else():
    conflict = Conflict(
        left_issue=SEC, right_issue=PERF, left_remedy_cost="+2 branches",
        right_remedy_cost="p50 +18%", axis="security_vs_performance", detector="same_span",
    )
    text = bundle(
        verdict=verdict(decision=Decision.TRADEOFF, rule_fired="irreconcilable",
                        conflict=conflict, open_issues=[SEC]),
    ).render()
    assert "tradeoff_justification" in text and "recommended_default" in text
    assert "Leave `consolidated_critique` null" in text
    assert "+2 branches" in text


def test_only_the_coders_pushback_is_adjudicable():
    contested = bundle(patch=patch([UnaddressedIssue(issue_id=SEC, reason="false positive")]))
    assert contested.adjudicable_ids() == {SEC}
    assert bundle().adjudicable_ids() == set()


def test_an_already_dismissed_issue_is_not_re_adjudicated():
    """A dismissal is permanent, so asking about it again wastes a round and invites the
    model to reverse itself."""
    reopened = bundle(
        patch=patch([UnaddressedIssue(issue_id=SEC, reason="false positive")]),
        already_dismissed=(SEC,),
    )
    assert reopened.pushbacks() == []
    assert "pushback" not in reopened.render()


def test_the_affirmation_render_shows_only_the_overlapping_lines():
    """The question is about two remedies, not about the file. Sending the whole source
    invites an answer to a bigger question than the one asked."""
    text = affirmation_bundle().render()
    assert "subprocess.run" in text
    assert "import subprocess" not in text  # line 1, outside the span + 2 lines of context
    assert f'left_issue="{SEC}"' in text and f'right_issue="{PERF}"' in text


# -- validation: the three routes back into the decision ---------------------------------------


def test_a_note_that_disagrees_with_the_verdict_is_rejected():
    """docs/03-agents.md § 3.4: "`decision_echo` catches it if the prose drifts"."""
    with pytest.raises(EvidenceError, match="already been made"):
        validate_arbiter_note(
            note(decision_echo=Decision.ACCEPT, consolidated_critique=None), bundle()
        )


def test_a_note_for_the_wrong_round_is_rejected():
    with pytest.raises(EvidenceError, match="round must be 1"):
        validate_arbiter_note(note(round=2), bundle())


def test_the_arbiter_cannot_dismiss_an_issue_nobody_contested():
    """The decision, re-entering through the side door: a dismissal removes the issue from
    the pressure sum the policy layer just decided on."""
    with pytest.raises(EvidenceError, match="did not push back"):
        validate_arbiter_note(
            note(dismissed=[UnaddressedIssue(issue_id=SEC, reason="I disagree")]), bundle()
        )


def test_the_arbiter_can_dismiss_what_the_coder_contested():
    contested = bundle(patch=patch([UnaddressedIssue(issue_id=SEC, reason="unreachable")]))
    validate_arbiter_note(
        note(priority_order=[], dismissed=[UnaddressedIssue(issue_id=SEC, reason="agreed")]),
        contested,
    )


def test_an_issue_cannot_be_dismissed_and_prioritised_at_once():
    contested = bundle(patch=patch([UnaddressedIssue(issue_id=SEC, reason="unreachable")]))
    with pytest.raises(EvidenceError, match="Choose one"):
        validate_arbiter_note(
            note(priority_order=[SEC],
                 dismissed=[UnaddressedIssue(issue_id=SEC, reason="agreed")]),
            contested,
        )


def test_priority_order_cannot_name_an_issue_that_is_not_open():
    with pytest.raises(EvidenceError, match="not open issues"):
        validate_arbiter_note(note(priority_order=[SEC, "SEC-invented"]), bundle())


def test_priority_order_cannot_repeat_an_issue():
    with pytest.raises(EvidenceError, match="more than once"):
        validate_arbiter_note(note(priority_order=[SEC, SEC]), bundle())


def test_the_arbiter_may_not_write_a_diff():
    """Same collapse `_check_suggested_direction` prevents for critics: a second patch with
    no owner."""
    with pytest.raises(EvidenceError, match="Describe the change in words"):
        validate_arbiter_note(
            note(consolidated_critique="do this:\n```diff\n-a\n+b\n```"), bundle()
        )


def test_every_problem_with_a_note_is_reported_at_once():
    """The repair budget is one, so reporting problems one at a time spends it on the first."""
    with pytest.raises(EvidenceError) as caught:
        validate_arbiter_note(
            note(round=3, priority_order=["SEC-invented"],
                 dismissed=[UnaddressedIssue(issue_id=SEC, reason="no")]),
            bundle(),
        )
    assert len(str(caught.value).splitlines()) >= 3


def test_an_affirmation_about_a_different_pair_is_rejected():
    """The two ids are opaque hex and adjacent in the prompt -- exactly the setup in which
    one gets substituted for the other, and this answer feeds `policy.decide`."""
    from tribunal.contracts import ConflictAffirmation

    answer = ConflictAffirmation(
        left_issue=PERF, right_issue=SEC, opposing=True,
        left_remedy_cost="x", right_remedy_cost="y", reasoning="they fight",
    )
    with pytest.raises(EvidenceError, match="you swapped them"):
        validate_affirmation(answer, affirmation_bundle())


# -- end to end through a fake provider ---------------------------------------------------------


def arbiter_client(tmp_path: Path, script: list[Any]) -> tuple[Arbiter, FakeProvider]:
    def agent() -> AgentConfig:
        return AgentConfig(model="fake-1", provider=ProviderName.NIM, max_tokens=4000)

    settings = Settings(
        llm=LLMConfig(cassette_dir=tmp_path),
        agents=AgentsConfig(arbiter=agent(), arbiter_affirm=agent()),
    )
    fake = FakeProvider(script=script)
    client = LLMClient(settings)
    client.providers[ProviderName.NIM] = fake
    return Arbiter(settings, client), fake


def note_payload(**kw: Any) -> dict:
    base = json.loads(note().model_dump_json())
    base.update(kw)
    return base


async def test_the_arbiter_runs_end_to_end(tmp_path):
    arbiter, _ = arbiter_client(tmp_path, [note_payload()])
    run = await arbiter.run(bundle())
    assert run.role == "arbiter"
    assert run.prompt_version == "arbiter/v1"
    assert run.value.decision_echo is Decision.REJECT
    assert run.outcome.repair_retries == 0
    # Mirrors the templated path's marker, so a trace reader can tell them apart without
    # knowing whether an Arbiter was configured.
    assert run.metrics["synthesised_by"] == "arbiter"


async def test_a_drifting_note_costs_one_repair_retry_and_then_lands(tmp_path):
    arbiter, _ = arbiter_client(
        tmp_path,
        [
            note_payload(decision_echo="accept", consolidated_critique=None),
            note_payload(),
        ],
    )
    run = await arbiter.run(bundle())
    assert run.outcome.repair_retries == 1
    assert run.value.decision_echo is Decision.REJECT


async def test_the_affirmer_runs_at_its_own_effort_and_version(tmp_path):
    """A yes/no classification must not inherit the synthesis budget: it fires once per
    same-span candidate pair, which is more often than the note does."""
    settings = Settings()
    arbiter = Arbiter(settings)
    assert arbiter.effort_for(1) == "xhigh"
    assert arbiter.affirmer.effort_for(1) == "medium"
    assert arbiter.affirmer.prompt_version == "arbiter_affirm/v1"


async def test_the_affirmer_shares_the_arbiters_client(tmp_path):
    """Two clients would mean two cost totals, and the budget would never see half the spend."""
    arbiter, _ = arbiter_client(tmp_path, [])
    assert arbiter.affirmer.client is arbiter.client


async def test_affirms_returns_the_whole_run_so_the_trace_can_record_it(tmp_path):
    arbiter, _ = arbiter_client(
        tmp_path,
        [{
            "left_issue": SEC, "right_issue": PERF, "opposing": True,
            "left_remedy_cost": "+2 branches", "right_remedy_cost": "p50 +18%",
            "reasoning": "the check the security fix needs is inside the measured loop",
        }],
    )
    left = issue()
    right = issue(id_=PERF, dimension=Dimension.PERFORMANCE, severity=Severity.MEDIUM,
                  span="target.py:L5-L6")
    run = await arbiter.affirms(left, right, 1, "target.py", SOURCE)
    assert run.role == "arbiter_affirm"
    assert run.value.opposing is True
    assert run.metrics["opposing"] is True
    # An LLM in the decision path with no record of what it said is the thing this prevents.
    assert run.trace_payload()["metrics"]["left_issue"] == SEC


async def test_a_swapped_affirmation_is_repaired_rather_than_believed(tmp_path):
    swapped = {
        "left_issue": PERF, "right_issue": SEC, "opposing": True,
        "left_remedy_cost": "x", "right_remedy_cost": "y", "reasoning": "they fight",
    }
    correct = {**swapped, "left_issue": SEC, "right_issue": PERF}
    arbiter, _ = arbiter_client(tmp_path, [swapped, correct])
    left = issue()
    right = issue(id_=PERF, dimension=Dimension.PERFORMANCE, severity=Severity.MEDIUM,
                  span="target.py:L5-L6")
    run = await arbiter.affirms(left, right, 1, "target.py", SOURCE)
    assert run.outcome.repair_retries == 1
    assert (run.value.left_issue, run.value.right_issue) == (SEC, PERF)


def test_the_affirmer_is_usable_on_its_own(tmp_path):
    """Built as its own agent, not a method, so the eval can swap its model independently."""
    settings = Settings()
    assert ConflictAffirmer(settings).role == "arbiter_affirm"
