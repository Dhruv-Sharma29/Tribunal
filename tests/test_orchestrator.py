"""The orchestrator, end to end, with no API key and no network.

This file exists to close the last four boxes of docs/09-roadmap.md Phase 3, and each of them
is a *run*, not a unit:

* a full run emits a gap-free trace with `run_start`/`run_end` and one `policy_decision` per
  round;
* `tribunal replay` reproduces the report from that trace byte-identically *(criterion S6)*;
* at least one run terminates in each of `ACCEPT`, `REJECT -> ACCEPT`, `ESCALATE` and
  `TRADEOFF` *(criterion S4)*;
* a single critic failure yields `unassessed`, never `ACCEPT` -- the half of that claim the
  policy tests could not make, because it is about the parallel span rather than the table.

The runs are real: the real `GroundingSuite` over a real file, the real `policy.decide`, the
real `fsm.Machine`, the real `patch.py`. Only the provider is scripted, because it is the only
part that costs money. That is also why the outcomes are steered by what the critics *say*
rather than by patching the policy layer -- a test that stubs `decide` proves the orchestrator
can call a mock.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_llm_client import FakeProvider
from tribunal.agents import Arbiter, Postmortem
from tribunal.config import (
    AgentConfig,
    AgentsConfig,
    BudgetConfig,
    LLMConfig,
    PolicyConfig,
    Settings,
)
from tribunal.contracts import Decision, Dimension, canonical_issue_id
from tribunal.fsm import State
from tribunal.llm.base import ProviderName
from tribunal.llm.client import LLMClient
from tribunal.orchestrator import Orchestrator, synthesise_instructions
from tribunal.trace import reader as trace_reader
from tribunal.trace import report as report_builder

SOURCE = '''import subprocess


def generate(name):
    subprocess.run("makereport " + name, shell=True)
    return name
'''

#: The two anchors the scripted Coder edits. Distinct, so a second round can propose a
#: genuinely different diff and not trip the oscillation guard by accident.
SHELL_LINE = '    subprocess.run("makereport " + name, shell=True)'
ARGV_LINE = '    subprocess.run(["makereport", name])'
RETURN_LINE = "    return name"
#: A third anchor, for the runs that need three rounds. Anchors must come from the original
#: source: the Coder is handed `source`, not `current_source`, every round.
DEF_LINE = "def generate(name):"


# -- scripting -------------------------------------------------------------------------------


def settings_for(tmp_path: Path, **overrides: Any) -> Settings:
    """Every agent routed to the one provider the test replaces with a double."""
    def agent() -> AgentConfig:
        return AgentConfig(model="fake-1", provider=ProviderName.NIM)

    return Settings(
        llm=LLMConfig(cassette_dir=tmp_path / "cassettes"),
        agents=AgentsConfig(
            coder=agent(), redteam=agent(), profiler=agent(),
            arbiter=agent(), arbiter_affirm=agent(), postmortem=agent(),
        ),
        **overrides,
    )


def proposal(round_: int, search: str, replace: str) -> dict:
    return {
        "round": round_,
        "edits": [{"search": search, "replace": replace}],
        "diff": "",
        "rationale": "fix the thing",
        "addresses": [],
        "deliberately_unaddressed": [],
    }


def issue(dimension: str, severity: str, span: str, confidence: float = 0.9) -> dict:
    """A `code_span` issue: grounded, and resolvable without knowing what bandit found.

    `tool_finding` would be more realistic, but it would couple every test here to the exact
    finding ids a particular bandit release produces. A span is checked against the real file
    just as strictly -- `validation._check_code_span` rejects one outside it.
    """
    return {
        "id": "model-supplied",  # rewritten by `canonicalise_issue_ids`
        "dimension": dimension,
        "severity": severity,
        "title": f"a {severity} {dimension} issue",
        "explanation": "it is bad",
        "confidence": confidence,
        "introduced_by_patch": False,
        "suggested_direction": f"change the {dimension} thing",
        "evidence": [{"kind": "code_span", "ref": f"t.py:{span}", "excerpt": "..."}],
    }


def critique(dimension: str, round_: int, verdict: str, issues: list[dict], tool: str) -> dict:
    return {
        "dimension": dimension,
        "round": round_,
        "verdict": verdict,
        "issues": issues,
        "positive_notes": [],
        "tools_consulted": [tool],
        "summary": "a summary",
    }


def clean(dimension: str, round_: int) -> dict:
    tool = "bandit" if dimension == "security" else "radon"
    return critique(dimension, round_, "clean", [], tool)


def drive(
    tmp_path: Path,
    script: list[Any],
    *,
    with_arbiter: bool = False,
    with_postmortem: bool = False,
    benchmarks: list[Any] | None = None,
    provider: FakeProvider | None = None,
    **overrides: Any,
):
    """One run against a scripted provider. Returns the `RunResult`.

    The script is consumed in call order: Coder first, then the two critics, then -- when an
    Arbiter is seated -- one affirmation per same-span candidate pair and one note per
    reject or tradeoff. `asyncio.gather` starts its coroutines in argument order and the
    double does not await before popping, so that order is deterministic here, which it would
    not be against a real provider and is why nothing outside this file depends on it.

    `with_arbiter` builds the real agent over the same scripted provider rather than a stub.
    The affirmation is an input to `policy.decide`, so a stub here would be a mock in the
    decision path -- the one place this file refuses to put one.
    """
    settings = settings_for(tmp_path, **overrides)
    fake = provider or FakeProvider()
    fake.script = list(script)
    client = LLMClient(settings)
    client.providers[ProviderName.NIM] = fake
    # One client for both seats: the orchestrator rejects an Arbiter with its own, because
    # that Arbiter's spend would be missing from every budget check.
    arbiter = Arbiter(settings, client) if with_arbiter else None
    writer_up = Postmortem(settings, client) if with_postmortem else None
    orchestrator = Orchestrator(settings, client, arbiter=arbiter, postmortem=writer_up)
    return asyncio.run(
        orchestrator.run(
            SOURCE, filename="t.py", benchmarks=benchmarks,
            trace_dir=tmp_path / "traces",
        )
    )


def span_issue_id(dimension: str, span: str) -> str:
    """The canonical id `issue()` will end up with, derived the way production derives it.

    `canonicalise_issue_ids` rewrites whatever the model said, so a script that has to *name*
    an issue -- an affirmation, a dismissal -- cannot use the model-supplied id. Recomputing
    it here rather than hard-coding the hex keeps the test honest if the derivation changes.
    """
    return canonical_issue_id(Dimension(dimension), "code_span", f"t.py:{span}")


def same_span_pair(security_span: str, performance_span: str) -> tuple[str, str]:
    """The pair as `same_span_candidates` will order it: by id, not by dimension.

    Sorting is what makes the question the Arbiter is asked independent of which critic
    returned first, and `validate_affirmation` rejects an answer that swaps the two -- so a
    script that guesses the order is a script that tests the repair path by accident.
    """
    left, right = sorted(
        [span_issue_id("security", security_span), span_issue_id("performance", performance_span)]
    )
    return left, right


def affirmation(left: str, right: str, opposing: bool = True) -> dict:
    return {
        "left_issue": left,
        "right_issue": right,
        "opposing": opposing,
        "left_remedy_cost": "+2 branches",
        "right_remedy_cost": "unmeasured",
        "reasoning": "the check the security fix needs is the allocation the profiler flagged",
    }


def note(
    round_: int,
    decision: str,
    *,
    critique_text: str | None = None,
    priority_order: list[str] | None = None,
    dismissed: list[dict] | None = None,
    justification: str | None = None,
    recommended: str | None = None,
) -> dict:
    return {
        "round": round_,
        "decision_echo": decision,
        "consolidated_critique": critique_text,
        "priority_order": priority_order or [],
        "dismissed": dismissed or [],
        "tradeoff_justification": justification,
        "recommended_default": recommended,
    }


# -- criterion S4: one run per terminal outcome ----------------------------------------------


def test_a_clean_patch_is_accepted(tmp_path):
    result = drive(tmp_path, [
        proposal(1, SHELL_LINE, ARGV_LINE),
        clean("security", 1),
        clean("performance", 1),
    ])
    assert result.outcome is Decision.ACCEPT
    assert result.report.rule_fired == "accept"
    assert result.terminal_state is State.DONE
    assert result.report.rounds_used == 1
    assert ARGV_LINE in result.report.accepted_diff
    assert result.report.no_patch_reason is None


def test_a_hard_blocked_patch_is_rejected_then_accepted_next_round(tmp_path):
    """The REJECT -> ACCEPT path, which is the loop the whole system is built around.

    Round 1 carries an open high-severity security issue above the confidence floor, so row 7
    fires; round 2 is clean, so the catch-all does. Two `policy_decision` events, two different
    diffs, one accept.
    """
    result = drive(tmp_path, [
        proposal(1, SHELL_LINE, ARGV_LINE),
        critique("security", 1, "block", [issue("security", "high", "L5-L5")], "bandit"),
        clean("performance", 1),
        proposal(2, RETURN_LINE, "    return str(name)"),
        clean("security", 2),
        clean("performance", 2),
    ])
    assert result.outcome is Decision.ACCEPT
    assert result.report.rounds_used == 2

    decisions = [e.payload for e in result.trace.of_kind("policy_decision")]
    assert [(d["round"], d["decision"], d["rule_fired"]) for d in decisions] == [
        (1, "reject", "hard_block_security"),
        (2, "accept", "accept"),
    ]
    # The rejected round's issue is reported as fixed rather than dropped: the charter's
    # output contract is issues *found*, not only issues outstanding.
    assert [(f.issue.dimension, f.status) for f in result.report.issues] == [
        (Dimension.SECURITY, "fixed")
    ]


def test_an_unreported_dimension_on_the_final_round_escalates(tmp_path):
    """Row 6. With one round configured there is no round left in which to re-run the critic,
    so the run escalates rather than accepting a file nobody checked for performance."""
    result = drive(
        tmp_path,
        [
            proposal(1, SHELL_LINE, ARGV_LINE),
            clean("security", 1),
            RuntimeError("the profiler fell over"),
        ],
        policy=PolicyConfig(max_rounds=1),
    )
    assert result.outcome is Decision.ESCALATE
    assert result.report.rule_fired == "unassessed_terminal"
    assert result.report.unassessed_dimensions == [Dimension.PERFORMANCE]
    assert result.terminal_state is State.DONE


def test_two_grounded_issues_on_one_span_end_in_a_tradeoff(tmp_path):
    """Detector 2, affirmed. Both sides medium: a security HIGH would hit row 7 first, and a
    trade-off over two LOWs is noise with a ceremony attached (`CONFLICT_MIN_SEVERITY`)."""
    left, right = same_span_pair("L5-L5", "L5-L6")
    result = drive(
        tmp_path,
        [
            proposal(1, SHELL_LINE, ARGV_LINE),
            critique("security", 1, "concerns", [issue("security", "medium", "L5-L5")],
                     "bandit"),
            critique("performance", 1, "concerns", [issue("performance", "medium", "L5-L6")],
                     "radon"),
            affirmation(left, right, opposing=True),
            note(1, "tradeoff", justification="both cost something",
                 recommended="ship the validated version unless this is the hot path"),
        ],
        with_arbiter=True,
    )
    assert result.outcome is Decision.TRADEOFF
    assert result.report.rule_fired == "irreconcilable"
    assert result.report.conflict is not None
    assert result.report.conflict.detector == "same_span"
    assert result.report.conflict.axis == "security_vs_performance"
    # The Arbiter's costs replace the critics' restated directions -- docs/04 wants
    # "+2 branches", not "change the security thing".
    assert result.report.conflict.left_remedy_cost == "+2 branches"
    # A TRADEOFF ships a patch -- that is the point of the state.
    assert result.report.accepted_diff is not None
    assert {f.status for f in result.report.issues} == {"accepted_tradeoff"}
    # docs/04 § What TRADEOFF actually emits: the axis and the costs are the record, the
    # recommended default and its reversal condition are what a reader acts on.
    assert result.report.recommended_default is not None
    assert "hot path" in result.report.recommended_default


def test_the_same_span_detector_cannot_fire_without_an_arbiter(tmp_path):
    """The identical input with nobody to affirm the opposition must not produce a TRADEOFF.

    docs/11-risks.md R4: a spurious trade-off is the worst output the system can produce, so
    the unaffirmed case falls through to an ordinary rejection.
    """
    result = drive(tmp_path, [
        proposal(1, SHELL_LINE, ARGV_LINE),
        critique("security", 1, "concerns", [issue("security", "medium", "L5-L5")], "bandit"),
        critique("performance", 1, "concerns", [issue("performance", "medium", "L5-L6")],
                 "radon"),
        proposal(2, RETURN_LINE, "    return str(name)"),
        clean("security", 2),
        clean("performance", 2),
    ])
    assert result.outcome is Decision.ACCEPT
    assert result.report.conflict is None
    first = result.trace.of_kind("policy_decision")[0].payload
    assert (first["decision"], first["rule_fired"]) == ("reject", "pressure_over_threshold")


def test_an_arbiter_that_declines_to_affirm_produces_an_ordinary_rejection(tmp_path):
    """The other half of the previous test, and the one that matters more.

    "No Arbiter, so no TRADEOFF" is true by construction. "An Arbiter looked at the pair and
    said no" is the case that decides whether detector 2 is a detector or a rubber stamp.
    """
    left, right = same_span_pair("L5-L5", "L5-L6")
    result = drive(
        tmp_path,
        [
            proposal(1, SHELL_LINE, ARGV_LINE),
            critique("security", 1, "concerns", [issue("security", "medium", "L5-L5")],
                     "bandit"),
            critique("performance", 1, "concerns", [issue("performance", "medium", "L5-L6")],
                     "radon"),
            affirmation(left, right, opposing=False),
            note(1, "reject", critique_text="fix the security one first"),
            proposal(2, RETURN_LINE, "    return str(name)"),
            clean("security", 2),
            clean("performance", 2),
        ],
        with_arbiter=True,
    )
    assert result.outcome is Decision.ACCEPT
    assert result.report.conflict is None
    first = result.trace.of_kind("policy_decision")[0].payload
    assert (first["decision"], first["rule_fired"]) == ("reject", "pressure_over_threshold")
    # The refusal is on the record: an affirmation nobody can see afterwards is an LLM in
    # the decision path with no audit trail.
    answers = [
        e for e in result.trace.by_actor("arbiter_affirm") if e.kind == "llm_response"
    ]
    assert [e.payload["parsed"]["opposing"] for e in answers] == [False]


def test_no_affirmation_is_asked_for_when_no_pair_overlaps(tmp_path):
    """The mechanical half of detector 2 is free, and it is what keeps the call rare."""
    result = drive(
        tmp_path,
        [
            proposal(1, SHELL_LINE, ARGV_LINE),
            clean("security", 1),
            clean("performance", 1),
        ],
        with_arbiter=True,
    )
    assert result.outcome is Decision.ACCEPT
    assert result.trace.by_actor("arbiter_affirm") == []
    # ACCEPT needs no prose from the Arbiter either -- the Postmortem writes that up.
    assert result.trace.by_actor("arbiter") == []


def test_an_arbiter_holding_its_own_client_is_refused(tmp_path):
    """Its spend would be missing from every budget check, and the symptom -- a cap that
    never fires -- looks like nothing at all."""
    settings = settings_for(tmp_path)
    with pytest.raises(ValueError, match="share the orchestrator's LLMClient"):
        Orchestrator(settings, LLMClient(settings), arbiter=Arbiter(settings))


# -- the Coder's pushback, adjudicated --------------------------------------------------------


def test_a_dismissed_issue_leaves_the_pressure_sum_and_the_critics_cannot_re_raise_it(tmp_path):
    """The capability the templated stand-in cannot have, end to end over three rounds.

    Round 1 raises a security issue. Round 2 the Coder pushes back on it and the Arbiter
    agrees. Round 3 must then see it as settled: out of `PolicyInput.dismissed_issue_ids`,
    and named to the critics as something they may not raise again.
    """
    sec = span_issue_id("security", "L5-L5")
    pushback = [{"issue_id": sec, "reason": "the value is a module constant"}]
    round_two = proposal(2, RETURN_LINE, "    return str(name)")
    round_two["deliberately_unaddressed"] = pushback

    fake = FakeProvider()
    result = drive(
        tmp_path,
        [
            proposal(1, SHELL_LINE, ARGV_LINE),
            critique("security", 1, "block", [issue("security", "high", "L5-L5")], "bandit"),
            clean("performance", 1),
            note(1, "reject", critique_text="fix the shell call", priority_order=[sec]),
            round_two,
            critique("security", 2, "block", [issue("security", "high", "L5-L5")], "bandit"),
            clean("performance", 2),
            note(2, "reject", critique_text="agreed, it is a false positive",
                 dismissed=[{"issue_id": sec, "reason": "unreachable from untrusted input"}]),
            # A third anchor from the *original* source: the Coder re-patches the original
            # every round, so a line an earlier round produced is not there to anchor on.
            proposal(3, DEF_LINE, "def generate(name: str):"),
            clean("security", 3),
            clean("performance", 3),
        ],
        with_arbiter=True,
        provider=fake,
    )
    assert result.outcome is Decision.ACCEPT
    assert result.report.rounds_used == 3

    # The fate the report records, which `replay` reproduces without an Arbiter in reach.
    assert [(f.issue.id, f.status) for f in result.report.issues] == [(sec, "dismissed")]

    # And the instruction reached the critics, who would otherwise burn round 3 re-raising it.
    round_three = [r for r in fake.seen if "Round 3" in r.user and "Assess" in r.user]
    assert round_three, "the critics were never given a round 3 bundle"
    assert all("do NOT raise these again" in r.user and sec in r.user for r in round_three)


def test_the_templated_arbiter_dismisses_nothing_even_when_pushed(tmp_path):
    """Adjudication is a judgement call. A template that guessed would silently drop real
    issues out of the pressure sum, which is the one thing a stand-in must not do."""
    sec = span_issue_id("security", "L5-L5")
    round_two = proposal(2, RETURN_LINE, "    return str(name)")
    round_two["deliberately_unaddressed"] = [{"issue_id": sec, "reason": "false positive"}]

    result = drive(tmp_path, [
        proposal(1, SHELL_LINE, ARGV_LINE),
        critique("security", 1, "block", [issue("security", "high", "L5-L5")], "bandit"),
        clean("performance", 1),
        round_two,
        clean("security", 2),
        clean("performance", 2),
    ])
    assert result.outcome is Decision.ACCEPT
    assert [f.status for f in result.report.issues] == ["fixed"]  # not "dismissed"
    notes = result.trace.by_actor("arbiter")
    assert all(n.payload["parsed"]["dismissed"] == [] for n in notes)


# -- the write-up -----------------------------------------------------------------------------


def write_up(
    rounds: list[tuple[int, str, str]],
    outcome: str = "accept",
    caveats: list[str] | None = None,
    disagreement: str | None = None,
) -> dict:
    return {
        "outcome_echo": outcome,
        "headline": "a shell call concatenated a parameter",
        "rounds": [
            {"round": r, "what_changed": changed, "outcome": result}
            for r, changed, result in rounds
        ],
        "disagreement": disagreement,
        "what_i_would_not_trust": caveats or ["the review was single-file"],
        "human_should_check": [],
    }


def test_the_postmortem_writes_the_narrative_into_the_trace(tmp_path):
    """The POSTMORTEM state stops being a no-op transition and starts producing something."""
    result = drive(
        tmp_path,
        [
            proposal(1, SHELL_LINE, ARGV_LINE),
            clean("security", 1),
            clean("performance", 1),
            write_up([(1, "replaced the shell string with an argv list", "accepted")]),
        ],
        with_postmortem=True,
    )
    assert result.outcome is Decision.ACCEPT
    assert result.terminal_state is State.DONE
    notes = [e for e in result.trace.by_actor("postmortem") if e.kind == "llm_response"]
    assert len(notes) == 1
    # Written while the machine is in POSTMORTEM, before the WRITTEN transition.
    states = [e.payload["state"] for e in result.trace.of_kind("state_enter")]
    assert states[-2:] == ["POSTMORTEM", "DONE"]

    assert result.report.narrative is not None
    assert result.report.narrative.rstrip().endswith("- the review was single-file")


def test_a_run_without_a_postmortem_has_no_narrative(tmp_path):
    result = drive(tmp_path, [
        proposal(1, SHELL_LINE, ARGV_LINE),
        clean("security", 1),
        clean("performance", 1),
    ])
    assert result.outcome is Decision.ACCEPT
    assert result.report.narrative is None
    assert result.trace.by_actor("postmortem") == []


def test_an_escalated_run_is_still_written_up(tmp_path):
    """docs/04 § Budget enforcement: "an exceeded budget still produces a trace, a report,
    and the best patch seen so far". The write-up is part of not losing the work, and an
    escalation is when a human most needs to be told what happened."""
    result = drive(
        tmp_path,
        [
            proposal(1, SHELL_LINE, ARGV_LINE),
            clean("security", 1),
            RuntimeError("the profiler fell over"),
            write_up(
                [(1, "replaced the shell string", "escalated: nobody checked performance")],
                outcome="escalate",
                caveats=["performance was never assessed — that is not the same as clean"],
            ),
        ],
        with_postmortem=True,
        policy=PolicyConfig(max_rounds=1),
    )
    assert result.outcome is Decision.ESCALATE
    assert result.report.narrative is not None
    assert "performance" in result.report.narrative


def test_a_failed_run_reaches_no_postmortem_and_says_nothing(tmp_path):
    """FAILED is terminal directly from VALIDATE — there is no POSTMORTEM edge out of it, so
    a run that never landed a patch has no narrative rather than an empty one."""
    result = drive(
        tmp_path,
        [
            proposal(1, "a line that is not in the file", "x"),
            proposal(1, "still not in the file", "y"),
        ],
        with_postmortem=True,
    )
    assert result.terminal_state is State.FAILED
    assert result.report.narrative is None
    assert result.trace.by_actor("postmortem") == []


def test_the_narrative_survives_a_replay_byte_identically(tmp_path):
    """The reason the model writes content and not markdown: `render_narrative` runs on the
    read side, so the live path and `replay` cannot drift."""
    result = drive(
        tmp_path,
        [
            proposal(1, SHELL_LINE, ARGV_LINE),
            clean("security", 1),
            clean("performance", 1),
            write_up([(1, "replaced the shell string with an argv list", "accepted")]),
        ],
        with_postmortem=True,
    )
    from_file = report_builder.build(trace_reader.read(result.trace_path))
    assert from_file.narrative == result.report.narrative
    assert from_file.model_dump_json() == result.report.model_dump_json()


def test_the_postmortem_bundle_rebuilds_from_a_trace_on_disk(tmp_path):
    """`tribunal postmortem <trace>` has to compact a file with the identical code the
    orchestrator uses mid-run, or the re-runnable path is the untested one."""
    result = drive(tmp_path, [
        proposal(1, SHELL_LINE, ARGV_LINE),
        critique("security", 1, "block", [issue("security", "high", "L5-L5")], "bandit"),
        clean("performance", 1),
        proposal(2, RETURN_LINE, "    return str(name)"),
        clean("security", 2),
        clean("performance", 2),
    ])
    bundle = report_builder.postmortem_bundle(trace_reader.read(result.trace_path))
    assert bundle.outcome is Decision.ACCEPT
    assert [d.round for d in bundle.rounds] == [1, 2]
    assert [d.rule_fired for d in bundle.rounds] == ["hard_block_security", "accept"]
    # The Arbiter's instruction for round 1 is part of the story of what round 2 did.
    assert bundle.rounds[0].consolidated_critique
    assert bundle.fixed_issues and not bundle.open_issues


def test_a_postmortem_holding_its_own_client_is_refused(tmp_path):
    settings = settings_for(tmp_path)
    with pytest.raises(ValueError, match="Postmortem must share"):
        Orchestrator(settings, LLMClient(settings), postmortem=Postmortem(settings))


# -- criterion S1's other half: a critic failure is never an accept ---------------------------


def test_a_failed_critic_is_recorded_and_its_dimension_left_unassessed(tmp_path):
    """One critic raising must not lose the other's work, and must not read as clean.

    The policy tests prove rows 5/6 refuse the combination. What they cannot show is that the
    surviving critic's critique still reaches the trace, because that is a property of
    `asyncio.gather(return_exceptions=True)` rather than of the table.
    """
    result = drive(tmp_path, [
        proposal(1, SHELL_LINE, ARGV_LINE),
        clean("security", 1),
        RuntimeError("the profiler fell over"),
        proposal(2, RETURN_LINE, "    return str(name)"),
        clean("security", 2),
        clean("performance", 2),
    ])
    errors = result.trace.of_kind("error")
    assert [e.actor for e in errors] == ["profiler"]
    assert errors[0].payload["exception"] == "RuntimeError"
    assert errors[0].payload["recovered"] is True

    first = result.trace.of_kind("policy_decision")[0].payload
    assert (first["decision"], first["rule_fired"]) == ("reject", "unassessed_dimension")
    assert first["unassessed_dimensions"] == ["performance"]
    # The surviving critic's work is still in the trace for round 1.
    round_one = [e for e in result.trace.of_kind("llm_response") if e.round == 1]
    # coder, the surviving critic, and the templated consolidation. No profiler.
    assert [e.actor for e in round_one] == ["coder", "redteam", "arbiter"]
    # ...and the run recovers rather than aborting.
    assert result.outcome is Decision.ACCEPT


def test_both_critics_failing_still_produces_a_report(tmp_path):
    result = drive(
        tmp_path,
        [
            proposal(1, SHELL_LINE, ARGV_LINE),
            RuntimeError("redteam down"),
            RuntimeError("profiler down"),
        ],
        policy=PolicyConfig(max_rounds=1),
    )
    assert result.outcome is Decision.ESCALATE
    assert result.report.unassessed_dimensions == [Dimension.SECURITY, Dimension.PERFORMANCE]
    assert result.report.accepted_diff is None
    assert "best patch" in result.report.no_patch_reason


def test_the_two_critics_really_do_overlap_in_wall_clock(tmp_path):
    """The parallelism claim, checked the way docs/06 says the viewer must draw it.

    Each critic's `llm_response` carries its own `duration_ms`; the CRITIQUE `state_exit`
    carries the span. Run sequentially the span would be at least the sum. This is the
    assertion that would have caught an `await` in the wrong place.
    """
    class SlowProvider(FakeProvider):
        async def complete(self, request, output_model, timeout):
            if output_model.__name__ == "Critique":
                await asyncio.sleep(0.25)
            return await super().complete(request, output_model, timeout)

    result = drive(
        tmp_path,
        [proposal(1, SHELL_LINE, ARGV_LINE), clean("security", 1), clean("performance", 1)],
        provider=SlowProvider(),
    )
    critics = [
        e.duration_ms
        for e in result.trace.of_kind("llm_response")
        if e.actor in ("redteam", "profiler")
    ]
    span = next(
        e.duration_ms
        for e in result.trace.of_kind("state_exit")
        if e.payload["state"] == State.CRITIQUE.value
    )
    assert len(critics) == 2 and all(d >= 250 for d in critics)
    assert span < sum(critics), (
        f"CRITIQUE took {span}ms but the two critics report {critics}ms — they ran in sequence"
    )


# -- the trace itself -------------------------------------------------------------------------


def test_a_full_run_emits_a_gap_free_self_describing_trace(tmp_path):
    result = drive(tmp_path, [
        proposal(1, SHELL_LINE, ARGV_LINE),
        critique("security", 1, "block", [issue("security", "high", "L5-L5")], "bandit"),
        clean("performance", 1),
        proposal(2, RETURN_LINE, "    return str(name)"),
        clean("security", 2),
        clean("performance", 2),
    ])
    # `strict=True` turns the reader's warnings into an error: a gap, a missing run_end, a
    # header that is not first, or events from two runs in one file all fail here.
    trace = trace_reader.read(result.trace_path, strict=True)

    assert [e.seq for e in trace.events] == list(range(len(trace.events)))
    assert trace.events[0].kind == "run_start"
    assert trace.events[-1].kind == "run_end"
    assert len({e.run_id for e in trace.events}) == 1

    header = trace.events[0].payload
    assert header["input_file"] == "t.py"
    assert header["config"]["price_table_version"]
    # Prompt versions cannot be retrofitted onto traces that already exist.
    assert {"coder", "redteam", "profiler"} <= set(header["prompt_versions"])

    # One policy decision per round, in order, and nothing outside a round.
    rounds = [e.payload["round"] for e in trace.of_kind("policy_decision")]
    assert rounds == [1, 2] == sorted(rounds)

    end = trace.events[-1].payload
    assert end["terminal_state"] == "DONE"
    assert end["rounds_used"] == 2
    assert end["outcome"] == "accept"


def test_the_state_path_is_the_one_the_architecture_diagram_draws(tmp_path):
    result = drive(tmp_path, [
        proposal(1, SHELL_LINE, ARGV_LINE),
        clean("security", 1),
        clean("performance", 1),
    ])
    entered = [e.payload["state"] for e in result.trace.of_kind("state_enter")]
    assert entered == [
        "GROUND", "PROPOSE", "VALIDATE", "CRITIQUE", "ARBITRATE", "POSTMORTEM", "DONE",
    ]
    # Every state_enter is preceded by the matching state_exit, so the waterfall has no holes.
    paired = [
        (e.kind, e.payload["state"])
        for e in result.trace.of_kind("state_enter", "state_exit")
    ]
    assert [kind for kind, _ in paired] == ["state_exit", "state_enter"] * 7


def test_a_bounced_patch_appears_as_a_validate_to_propose_edge(tmp_path):
    """The re-anchoring loop is modelled as real edges, not collapsed into one transition.

    A trace that showed one PROPOSE would claim the patch applied first time when it took two,
    and the VALIDATE -> PROPOSE edge docs/01 credits with absorbing early-round retries at zero
    critic cost would never appear in a waterfall.
    """
    result = drive(tmp_path, [
        proposal(1, "    this anchor is not in the file", "irrelevant"),
        proposal(1, SHELL_LINE, ARGV_LINE),
        clean("security", 1),
        clean("performance", 1),
    ])
    entered = [e.payload["state"] for e in result.trace.of_kind("state_enter")]
    assert entered.count("PROPOSE") == 2
    assert entered.count("VALIDATE") == 2

    attempts = [e.payload for e in result.trace.of_kind("patch_validate")]
    assert [a["attempt"] for a in attempts] == [1, 2]
    assert [a["applied"] for a in attempts] == [False, True]
    assert attempts[0]["failure_reason"]
    # The bounce costs an LLM call but not a debate round.
    assert result.report.rounds_used == 1
    assert result.outcome is Decision.ACCEPT


def test_a_patch_that_never_applies_ends_in_failed(tmp_path):
    """VALIDATE burning every attempt is row 1's second clause, and it still decides.

    Breaking straight out of the loop instead would leave the run with no `policy_decision` at
    all, and `run_end` naming `input_unusable` while the report said `no_decision_recorded` --
    two answers to one question, from the same run.
    """
    result = drive(tmp_path, [
        proposal(1, "    nowhere near the file", "a"),
        proposal(1, "    also not in the file", "b"),
    ])
    assert result.terminal_state is State.FAILED
    assert result.outcome is Decision.ESCALATE
    assert result.report.rule_fired == "input_unusable"

    decisions = result.trace.of_kind("policy_decision")
    assert [e.payload["rule_fired"] for e in decisions] == ["input_unusable"]
    end = result.trace.events[-1].payload
    assert (end["rule_fired"], end["outcome"]) == ("input_unusable", "escalate")
    assert end["rounds_used"] == result.report.rounds_used == 1
    assert end["terminal_state"] == "FAILED"

    # Neither critic was called: a patch that does not apply costs nothing to review.
    assert result.trace.by_actor("redteam") == []
    # The run is still a readable trace, which is the point of the state.
    assert trace_reader.read(result.trace_path, strict=True).events[-1].kind == "run_end"


def test_the_templated_arbiter_is_labelled_as_such(tmp_path):
    """With no Arbiter agent, the consolidated critique is synthesised. A reader must be able
    to tell that apart from a model's judgement, so the trace says which."""
    result = drive(tmp_path, [
        proposal(1, SHELL_LINE, ARGV_LINE),
        critique("security", 1, "block", [issue("security", "high", "L5-L5")], "bandit"),
        clean("performance", 1),
        proposal(2, RETURN_LINE, "    return str(name)"),
        clean("security", 2),
        clean("performance", 2),
    ])
    notes = result.trace.by_actor("arbiter")
    assert len(notes) == 1  # only the rejected round needs consolidating
    assert notes[0].payload["parsed"]["synthesised_by"] == "template"
    assert notes[0].usage is None  # it cost nothing, and the trace must not imply it did

    text = notes[0].payload["parsed"]["consolidated_critique"]
    assert "hard_block_security" in text and "security/high" in text


def test_the_synthesised_instructions_are_ordered_by_policy_weight():
    """Ordered by score, so the Coder is told to fix what is keeping pressure high rather than
    whichever critic spoke first."""
    from tribunal.contracts import Critique, Verdict

    low, high = issue("security", "low", "L5-L5"), issue("security", "high", "L6-L6")
    low["id"], high["id"] = "SEC-low", "SEC-high"
    parsed = Critique.model_validate(critique("security", 1, "block", [low, high], "bandit"))
    verdict = Verdict(
        round=1, decision=Decision.REJECT, rule_fired="hard_block_security",
        pressure=15.0, pressure_history=[15.0], open_issues=["SEC-low", "SEC-high"],
        unassessed_dimensions=[], conflict=None,
    )
    text = synthesise_instructions([parsed], verdict)
    assert text.index("SEC-high") < text.index("SEC-low")


def test_with_no_open_issues_the_instructions_say_so():
    from tribunal.contracts import Verdict

    verdict = Verdict(
        round=1, decision=Decision.REJECT, rule_fired="pressure_over_threshold",
        pressure=0.0, pressure_history=[0.0], open_issues=[],
        unassessed_dimensions=[], conflict=None,
    )
    assert "No open issues" in synthesise_instructions([], verdict)


# -- criterion S6: replay ---------------------------------------------------------------------


@pytest.mark.parametrize("script_name", ["accept", "reject_then_accept", "tradeoff"])
def test_the_report_replays_from_the_trace_byte_identically(tmp_path, script_name):
    """Criterion S6, and the reason it is worth asserting on more than the happy path.

    True by construction -- the orchestrator writes events and calls `report.build`, and this
    calls the same `build` over the file -- but "by construction" is a claim about today's
    code. The test is what keeps a second report-building code path from appearing.
    """
    scripts = {
        "accept": ([
            proposal(1, SHELL_LINE, ARGV_LINE),
            clean("security", 1),
            clean("performance", 1),
        ], False),
        "reject_then_accept": ([
            proposal(1, SHELL_LINE, ARGV_LINE),
            critique("security", 1, "block", [issue("security", "high", "L5-L5")], "bandit"),
            clean("performance", 1),
            proposal(2, RETURN_LINE, "    return str(name)"),
            clean("security", 2),
            clean("performance", 2),
        ], False),
        "tradeoff": ([
            proposal(1, SHELL_LINE, ARGV_LINE),
            critique("security", 1, "concerns", [issue("security", "medium", "L5-L5")],
                     "bandit"),
            critique("performance", 1, "concerns", [issue("performance", "medium", "L5-L6")],
                     "radon"),
            affirmation(*same_span_pair("L5-L5", "L5-L6")),
            note(1, "tradeoff", justification="both cost something",
                 recommended="ship the validated version"),
        ], True),
    }
    script, with_arbiter = scripts[script_name]
    result = drive(tmp_path, script, with_arbiter=with_arbiter)

    from_file = report_builder.build(trace_reader.read(result.trace_path))
    assert from_file.model_dump_json() == result.report.model_dump_json()


def test_replay_can_redecide_a_recorded_run_under_a_different_threshold(tmp_path):
    """The payoff of putting whole critiques in the trace: threshold tuning on real runs, for
    free, retroactively (docs/10 § Replay)."""
    result = drive(tmp_path, [
        proposal(1, SHELL_LINE, ARGV_LINE),
        clean("security", 1),
        critique("performance", 1, "block", [issue("performance", "high", "L5-L5")], "radon"),
        proposal(2, RETURN_LINE, "    return str(name)"),
        clean("security", 2),
        clean("performance", 2),
    ])
    assert result.trace.of_kind("policy_decision")[0].payload["rule_fired"] == (
        "pressure_over_threshold"
    )

    trace = trace_reader.read(result.trace_path)
    generous = report_builder.redecide(trace, PolicyConfig(accept_threshold=100.0))
    assert generous[0].changed
    assert generous[0].hypothetical.decision is Decision.ACCEPT

    unchanged = report_builder.redecide(trace, PolicyConfig())
    assert not any(row.changed for row in unchanged)


# -- budget ------------------------------------------------------------------------------------


def test_a_breached_budget_escalates_without_losing_the_work(tmp_path):
    """docs/04 § Budget enforcement: "an exceeded budget still produces a trace, a report, and
    the best patch seen so far. 'Ran out of money' is a legitimate outcome to report; losing
    the work is not."

    Row 3 is what fires, not the orchestrator's own state-entry check: both read the same
    `Spend`, and `decide` sees it a full round's spending later than the PROPOSE check does.
    The state-entry check only overtakes a *later* row once an Arbiter agent spends between
    the two, which is why `forced_escalation` is written the way it is and why nothing here
    asserts on `budget_check.breached`.
    """
    result = drive(
        tmp_path,
        [
            proposal(1, SHELL_LINE, ARGV_LINE),
            critique("security", 1, "block", [issue("security", "high", "L5-L5")], "bandit"),
            clean("performance", 1),
            proposal(2, RETURN_LINE, "    return str(name)"),
            clean("security", 2),
            clean("performance", 2),
        ],
        budget=BudgetConfig(max_usd=0.004),
    )
    assert result.outcome is Decision.ESCALATE
    assert result.report.rule_fired == "budget_exhausted"
    assert result.terminal_state is State.DONE
    assert result.report.rounds_used == 2

    # The work survives: round 2's patch applied, and the report says where to find it rather
    # than silently dropping it.
    assert result.report.accepted_diff is None
    assert "best patch" in result.report.no_patch_reason
    assert any(
        e.payload["applied"] and e.round == 2
        for e in result.trace.of_kind("patch_validate")
    )


def test_every_state_entry_records_what_had_been_spent(tmp_path):
    result = drive(tmp_path, [
        proposal(1, SHELL_LINE, ARGV_LINE),
        clean("security", 1),
        clean("performance", 1),
    ])
    checks = result.trace.of_kind("budget_check")
    assert [e.payload["state"] for e in checks] == ["PROPOSE"]
    assert checks[0].payload["caps"]["max_rounds"] == 3


# -- the summary dataset -----------------------------------------------------------------------


def test_each_run_appends_one_row_to_the_summary_dataset(tmp_path):
    """docs/06 § Metrics worth aggregating: this is what turns single runs into a dataset."""
    for _ in range(2):
        result = drive(tmp_path, [
            proposal(1, SHELL_LINE, ARGV_LINE),
            clean("security", 1),
            clean("performance", 1),
        ])

    rows = [
        json.loads(line)
        for line in (tmp_path / "traces" / "summary.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 2
    assert {r["run_id"] for r in rows} == {r["run_id"] for r in rows}  # distinct ULIDs
    assert rows[-1]["outcome"] == "accept"
    assert rows[-1]["rule_fired"] == "accept"
    assert rows[-1]["issues"] == {
        "fixed": 0, "open": 0, "accepted_tradeoff": 0, "dismissed": 0
    }
    assert rows[-1]["prompt_versions"]["coder"]
    assert rows[-1]["input_sha256"] == result.report.input_sha256


def test_the_trace_lands_in_the_configured_directory_by_default(tmp_path):
    """`run(trace_dir=...)` is an override; with none given the run writes where the settings
    say, and the report is the same object either way."""
    settings = settings_for(tmp_path, trace_dir=tmp_path / "configured")
    fake = FakeProvider(script=[
        proposal(1, SHELL_LINE, ARGV_LINE),
        clean("security", 1),
        clean("performance", 1),
    ])
    client = LLMClient(settings)
    client.providers[ProviderName.NIM] = fake
    result = asyncio.run(Orchestrator(settings, client).run(SOURCE, filename="t.py"))

    assert result.trace_path.parent == tmp_path / "configured"
    assert result.trace_path.name == f"{result.report.run_id}.jsonl"
    assert result.outcome is Decision.ACCEPT


def test_an_unmeasurable_benchmark_reaches_the_write_ups_caveats(tmp_path):
    """`perf` produces no ToolOutcome, so without an explicit event its results never reach
    the trace — and "no benchmark could be run" is exactly the kind of thing the write-up is
    required to disclose. Execution is off, so every benchmark is `unmeasurable` here, which
    is the case that matters: it is silent unless something reports it.
    """
    from tribunal.grounding.perf_t import Benchmark

    result = drive(
        tmp_path,
        [
            proposal(1, SHELL_LINE, ARGV_LINE),
            clean("security", 1),
            clean("performance", 1),
        ],
        benchmarks=[Benchmark(label="timeit:generate", expression="generate('x')")],
    )
    perf = [e for e in result.trace.of_kind("tool_run") if e.payload["tool"] == "perf"]
    assert perf, "the perf results never reached the trace"
    assert perf[0].payload["measurements"][0]["verdict"] == "unmeasurable"

    bundle = report_builder.postmortem_bundle(trace_reader.read(result.trace_path))
    # `unmeasurable()` folds the reason into the label, so the caveat carries *why* there is
    # no number rather than only that there isn't one.
    assert bundle.uncitable_measurements == (
        "timeit:generate (execution requires --allow-exec)",
    )
    assert "execution requires --allow-exec" in bundle.render()
    assert "not evidence of no change" in bundle.render()


# -- guards that nothing used to reach --------------------------------------------------------


def test_the_critics_are_shown_the_coders_rationale_as_a_claim_to_check(tmp_path):
    """docs/11-risks.md R1: critics go sycophantic when handed a confident justification as
    context, so `CritiqueBundle` renders the rationale and the pushback as claims to verify.

    The orchestrator never passed the patch, so that mitigation rendered nothing on every
    real run — present in the bundle, tested in isolation, dead in production.
    """
    sec = span_issue_id("security", "L5-L5")
    second = proposal(2, RETURN_LINE, "    return str(name)")
    second["rationale"] = "coerced the name to a string"
    # Round 1 has no `Issue` ids yet, so pushback is only expressible from round 2.
    second["deliberately_unaddressed"] = [{"issue_id": sec, "reason": "module constant"}]

    fake = FakeProvider()
    drive(tmp_path, [
        proposal(1, SHELL_LINE, ARGV_LINE),
        critique("security", 1, "block", [issue("security", "high", "L5-L5")], "bandit"),
        clean("performance", 1),
        second,
        clean("security", 2),
        clean("performance", 2),
    ], provider=fake)

    prompts = [r.user for r in fake.seen if "Assess the patched file" in r.user]
    assert len(prompts) == 4, "two critics, two rounds"
    for prompt in prompts:
        assert "UNVERIFIED" in prompt
        assert "### The diff under review" in prompt
    round_two = [p for p in prompts if "Round 2" in p]
    for prompt in round_two:
        assert "coerced the name to a string" in prompt
        assert "It declined to address" in prompt
        assert sec in prompt


def test_an_input_that_does_not_parse_stops_before_the_coder_is_called(tmp_path):
    """Row 1's *first* clause. `PolicyInput.input_parses` existed, the rule read it, and
    nothing ever set it — so a file that is not Python went round the whole loop.

    docs/01 § States: FAILED means "the system could not produce anything reviewable (e.g.
    input doesn't parse)". There is nothing to patch, so nothing is spent finding out.
    """
    settings = settings_for(tmp_path)
    fake = FakeProvider()
    fake.script = []  # any LLM call at all would raise IndexError
    client = LLMClient(settings)
    client.providers[ProviderName.NIM] = fake

    broken = "def generate(name:\n    this is not python\n"
    result = asyncio.run(
        Orchestrator(settings, client).run(
            broken, filename="t.py", trace_dir=tmp_path / "traces"
        )
    )
    assert fake.seen == [], "an unreviewable input still cost an LLM call"
    assert result.outcome is Decision.ESCALATE
    assert result.report.rule_fired == "input_unusable"
    assert result.terminal_state is State.FAILED
    assert result.report.accepted_diff is None
    # No round ran. `Verdict.round` is 1 only because the schema forbids 0, and `run_end`
    # now derives its count from the verdict so the two cannot disagree.
    assert result.report.rounds_used == 1


def test_the_unusable_input_run_is_still_a_readable_trace(tmp_path):
    """Every exit goes through one `_finish`, so the short path cannot grow a `run_end` that
    disagrees with the report — which is how `rule_fired` diverged once already (docs/13 §24).
    """
    settings = settings_for(tmp_path)
    client = LLMClient(settings)
    client.providers[ProviderName.NIM] = FakeProvider()
    result = asyncio.run(
        Orchestrator(settings, client).run(
            "def f(:\n", filename="t.py", trace_dir=tmp_path / "traces"
        )
    )
    trace = trace_reader.read(result.trace_path, strict=True)
    assert trace.events[-1].kind == "run_end"
    end = trace.events[-1].payload
    assert (end["rule_fired"], end["outcome"]) == ("input_unusable", "escalate")
    assert end["terminal_state"] == "FAILED"
    # And it replays, like any other run.
    assert report_builder.build(trace).model_dump_json() == result.report.model_dump_json()


def test_a_parsing_input_is_unaffected(tmp_path):
    """The guard must not fire on the ordinary case — a syntax check that rejected working
    files would be a far worse bug than the dead one it replaces."""
    result = drive(tmp_path, [
        proposal(1, SHELL_LINE, ARGV_LINE),
        clean("security", 1),
        clean("performance", 1),
    ])
    assert result.outcome is Decision.ACCEPT
    assert result.report.rounds_used == 1


def test_run_end_and_the_report_always_agree_on_the_round_count(tmp_path):
    """Two counters for one question is how `rule_fired` diverged once already. Asserted
    across the paths that count differently: a clean accept, a rejected-then-accepted run,
    and the unusable input where no round runs at all."""
    runs = [
        drive(tmp_path / "a", [
            proposal(1, SHELL_LINE, ARGV_LINE), clean("security", 1), clean("performance", 1),
        ]),
        drive(tmp_path / "b", [
            proposal(1, SHELL_LINE, ARGV_LINE),
            critique("security", 1, "block", [issue("security", "high", "L5-L5")], "bandit"),
            clean("performance", 1),
            proposal(2, RETURN_LINE, "    return str(name)"),
            clean("security", 2), clean("performance", 2),
        ]),
    ]
    settings = settings_for(tmp_path / "c")
    client = LLMClient(settings)
    client.providers[ProviderName.NIM] = FakeProvider()
    runs.append(asyncio.run(Orchestrator(settings, client).run(
        "def f(:\n", filename="t.py", trace_dir=tmp_path / "c" / "traces"
    )))

    for result in runs:
        end = result.trace.of_kind("run_end")[-1].payload
        assert end["rounds_used"] == result.report.rounds_used
        assert end["rule_fired"] == result.report.rule_fired
        assert end["outcome"] == result.report.outcome.value


def test_a_supplied_traceback_reaches_the_coder(tmp_path):
    """docs/03 § 3.1 lists "failing test / traceback if supplied" among the Coder's inputs
    and `CoderBundle._traceback_block` has always rendered it — but nothing fed it until
    `--error` existed. The seventh consumer-with-no-producer in this codebase, and the only
    one that was a missing feature rather than a bug."""
    settings = settings_for(tmp_path)
    fake = FakeProvider()
    fake.script = [proposal(1, SHELL_LINE, ARGV_LINE), clean("security", 1),
                   clean("performance", 1)]
    client = LLMClient(settings)
    client.providers[ProviderName.NIM] = fake

    crash = 'Traceback (most recent call last):\n  File "t.py", line 5\nValueError: nope\n'
    asyncio.run(
        Orchestrator(settings, client).run(
            SOURCE, filename="t.py", traceback=crash, trace_dir=tmp_path / "traces"
        )
    )
    assert "### Traceback" in fake.seen[0].user
    assert "ValueError: nope" in fake.seen[0].user


def test_a_run_without_a_traceback_renders_no_traceback_block(tmp_path):
    fake = FakeProvider()
    drive(tmp_path, [
        proposal(1, SHELL_LINE, ARGV_LINE), clean("security", 1), clean("performance", 1),
    ], provider=fake)
    assert "### Traceback" not in fake.seen[0].user
