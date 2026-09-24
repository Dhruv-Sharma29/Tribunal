"""The Postmortem: the bundle it reads, the checks on what it writes, and the layout.

Runs against a scripted fake provider, so zero API calls.

The agent itself is the least interesting part — it summarises. What is worth pinning is the
boundary around it, because a write-up is the artifact a human actually reads and a confident
one is believed:

* it cannot describe a run that did not happen (`outcome_echo`, round coverage);
* it cannot omit an open issue or an unassessed dimension, which is the difference between an
  accepted patch and a *conditionally* accepted one;
* the layout is ours, so "the final section is always what I would not trust" is structural
  rather than something the model is asked to remember.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_llm_client import FakeProvider
from tribunal.agents import Postmortem, PostmortemBundle, RoundDigest
from tribunal.agents.prompts import available_roles, load
from tribunal.agents.validation import EvidenceError, validate_postmortem
from tribunal.config import AgentConfig, AgentsConfig, LLMConfig, Settings
from tribunal.contracts import (
    Conflict,
    Decision,
    Dimension,
    Evidence,
    EvidenceKind,
    Issue,
    PostmortemNote,
    RoundSummary,
    Severity,
)
from tribunal.llm.base import ProviderName
from tribunal.llm.client import LLMClient
from tribunal.trace.report import render_narrative

SEC = "SEC-1111111111"
PERF = "PERF-222222222"


def issue(id_: str = SEC, dimension: Dimension = Dimension.SECURITY) -> Issue:
    return Issue(
        id=id_,
        dimension=dimension,
        severity=Severity.HIGH,
        title="shell=True on concatenated input",
        explanation="name reaches the shell unescaped",
        evidence=[Evidence(kind=EvidenceKind.CODE_SPAN, ref="t.py:L5-L5", excerpt="...")],
        confidence=0.9,
        introduced_by_patch=False,
        suggested_direction="pass an argv list",
    )


def digest(round_: int = 1, decision: str = "accept") -> RoundDigest:
    return RoundDigest(
        round=round_,
        decision=decision,
        rule_fired="accept" if decision == "accept" else "hard_block_security",
        pressure=0.0 if decision == "accept" else 15.2,
        rationale="swapped the shell call for an argv list",
        issue_ids=(SEC,),
    )


def bundle(**kw: Any) -> PostmortemBundle:
    defaults = dict(
        run_id="01JB",
        filename="t.py",
        outcome=Decision.ACCEPT,
        rule_fired="accept",
        rounds=(digest(),),
        accepted_diff="--- a\n+++ b\n@@ -5 +5 @@\n-old\n+new\n",
        no_patch_reason=None,
        open_issues=(),
        fixed_issues=(issue(),),
        dismissed_issues=(),
        unassessed_dimensions=(),
    )
    return PostmortemBundle(**{**defaults, **kw})


def note(**kw: Any) -> PostmortemNote:
    defaults = dict(
        outcome_echo=Decision.ACCEPT,
        headline="A shell call built a command by concatenating a parameter.",
        rounds=[
            RoundSummary(
                round=1,
                what_changed="replaced the shell string with an argv list",
                outcome="both critics came back clean, so the patch was accepted",
            )
        ],
        disagreement=None,
        what_i_would_not_trust=["the review was single-file; callers were not examined"],
        human_should_check=[],
    )
    return PostmortemNote(**{**defaults, **kw})


# -- the prompt -------------------------------------------------------------------------------


def test_the_postmortem_prompt_exists_and_is_versioned():
    assert "postmortem" in available_roles()
    prompt = load("postmortem")
    assert prompt.stamp == "postmortem/v1"
    assert prompt.changed != "unknown"
    assert prompt.note


def test_the_prompt_forbids_re_judging_the_outcome():
    """A summariser handed a decision it disagrees with will hedge it rather than report it."""
    text = load("postmortem").text
    assert "not re-judge" in text or "Do not re-judge" in text
    assert "summarising, not reviewing" in text


def test_the_prompt_states_that_uncertainty_is_never_empty():
    text = load("postmortem").text
    assert "never empty" in text
    assert "worse than no review tool" in text


# -- the bundle -------------------------------------------------------------------------------


def test_the_bundle_excludes_prompts_and_carries_decisions():
    """docs/03-agents.md § 3.5: "compacted ... not raw prompts". The exclusion is the point —
    what happened is the sequence of decisions, not the conversation."""
    text = bundle().render()
    assert "round 1: accept via accept" in text
    assert "swapped the shell call for an argv list" in text
    assert "### The accepted patch" in text


def test_open_issues_and_unassessed_dimensions_must_be_mentioned():
    must = bundle(
        open_issues=(issue(),),
        fixed_issues=(),
        unassessed_dimensions=(Dimension.PERFORMANCE,),
    ).must_mention()
    assert set(must) == {SEC, "performance"}


def test_a_tradeoffs_standing_objection_counts_as_open():
    """A trade-off ships the patch with the objection standing. A write-up that treats those
    issues as resolved is describing a different run."""
    conflicted = bundle(
        outcome=Decision.TRADEOFF,
        open_issues=(issue(), issue(PERF, Dimension.PERFORMANCE)),
        fixed_issues=(),
        conflict=Conflict(
            left_issue=SEC, right_issue=PERF, left_remedy_cost="+2 branches",
            right_remedy_cost="p50 +18%", axis="security_vs_performance",
            detector="same_span",
        ),
        tradeoff_justification="ship the validated version",
    )
    assert set(conflicted.must_mention()) == {SEC, PERF}
    text = conflicted.render()
    assert "### The trade-off" in text
    assert "ship the validated version" in text


def test_the_uncertainty_block_names_what_must_be_carried_over():
    text = bundle(
        open_issues=(issue(),),
        fixed_issues=(),
        unassessed_dimensions=(Dimension.PERFORMANCE,),
        uncitable_measurements=("timeit:parse:n=1000",),
    ).render()
    assert "UNASSESSED dimensions: performance" in text
    assert "not the same as clean" in text
    assert "timeit:parse:n=1000" in text


def test_a_clean_run_still_asks_for_caveats():
    """The one case where a model would happily write nothing, and the one where an empty
    section would read as "nothing to worry about"."""
    text = bundle().render()
    assert "Nothing mechanical is outstanding" in text
    assert "careful about anyway" in text


def test_a_run_with_no_patch_says_why():
    text = bundle(accepted_diff=None, no_patch_reason="escalated: rounds_exhausted").render()
    assert "### No patch was accepted" in text
    assert "rounds_exhausted" in text


# -- validation -------------------------------------------------------------------------------


def test_a_narrative_that_re_judges_the_outcome_is_rejected():
    with pytest.raises(EvidenceError, match="not revisiting it"):
        validate_postmortem(note(outcome_echo=Decision.REJECT), bundle())


def test_the_rounds_must_match_the_run():
    with pytest.raises(EvidenceError, match=r"rounds must describe exactly"):
        validate_postmortem(note(), bundle(rounds=(digest(1), digest(2, "reject"))))


def test_an_open_issue_cannot_be_left_out_of_the_write_up():
    """An accepted patch with an open issue is a conditional pass. A write-up that omits the
    condition teaches the reader to stop looking."""
    with pytest.raises(EvidenceError, match="never mentions"):
        validate_postmortem(note(), bundle(open_issues=(issue(),), fixed_issues=()))


def test_an_unassessed_dimension_cannot_be_left_out():
    with pytest.raises(EvidenceError, match="performance"):
        validate_postmortem(note(), bundle(unassessed_dimensions=(Dimension.PERFORMANCE,)))


def test_mentioning_the_open_issue_anywhere_in_the_write_up_satisfies_the_check():
    """Deliberately not "in what_i_would_not_trust specifically": a narrative that explains
    the open issue in the round summary and lists it in the caveats is good writing, and a
    checker that insisted on one exact field would fight it."""
    validate_postmortem(
        note(what_i_would_not_trust=[f"{SEC} is still open: the argv fix does not cover the "
                                     "second call site"]),
        bundle(open_issues=(issue(),), fixed_issues=()),
    )


def test_a_field_opening_with_a_heading_is_rejected():
    """The layout is ours; a field starting with `##` renders a heading inside a heading."""
    with pytest.raises(EvidenceError, match="markdown heading"):
        validate_postmortem(note(headline="## What happened\nit was bad"), bundle())


def test_the_uncertainty_list_cannot_be_empty():
    """A schema rule rather than a validator, because it is true of every run."""
    with pytest.raises(ValueError, match="what_i_would_not_trust"):
        note(what_i_would_not_trust=[])


# -- the layout -------------------------------------------------------------------------------


def test_the_write_up_always_ends_on_what_i_would_not_trust():
    """docs/03-agents.md § 3.5 makes this structural. Rendering it here rather than asking
    the model for it is what makes "always" true rather than usually true."""
    markdown = render_narrative(
        note(
            disagreement="the profiler wanted the branch gone; the red-team wanted it checked",
            human_should_check=["the call volume of generate()"],
        )
    )
    sections = [line for line in markdown.splitlines() if line.startswith("## ")]
    assert sections[-1] == "## What I would not trust"
    assert sections == [
        "## What happened",
        "## Round by round",
        "## Where the critics disagreed",
        "## What a human should check",
        "## What I would not trust",
    ]


def test_absent_optional_sections_are_omitted_rather_than_left_empty():
    markdown = render_narrative(note())
    assert "## Where the critics disagreed" not in markdown
    assert "## What a human should check" not in markdown
    assert "## What I would not trust" in markdown


def test_the_rendered_write_up_carries_every_round():
    markdown = render_narrative(
        note(
            rounds=[
                RoundSummary(round=1, what_changed="first", outcome="rejected"),
                RoundSummary(round=2, what_changed="second", outcome="accepted"),
            ]
        )
    )
    assert "**Round 1.** first" in markdown
    assert "**Round 2.** second" in markdown


# -- end to end through a fake provider ---------------------------------------------------------


def postmortem_client(tmp_path: Path, script: list[Any]) -> tuple[Postmortem, FakeProvider]:
    settings = Settings(
        llm=LLMConfig(cassette_dir=tmp_path),
        agents=AgentsConfig(
            postmortem=AgentConfig(model="fake-1", provider=ProviderName.NIM, max_tokens=4000)
        ),
    )
    fake = FakeProvider(script=script)
    client = LLMClient(settings)
    client.providers[ProviderName.NIM] = fake
    return Postmortem(settings, client), fake


def note_payload(**kw: Any) -> dict:
    base = json.loads(note().model_dump_json())
    base.update(kw)
    return base


async def test_the_postmortem_runs_end_to_end(tmp_path):
    agent, _ = postmortem_client(tmp_path, [note_payload()])
    run = await agent.run(bundle())
    assert run.role == "postmortem"
    assert run.prompt_version == "postmortem/v1"
    assert run.outcome.repair_retries == 0
    assert run.metrics["caveats"] == 1
    assert run.metrics["named_a_disagreement"] is False


async def test_omitting_an_open_issue_costs_one_repair_retry_and_then_lands(tmp_path):
    complete = note_payload(
        what_i_would_not_trust=[f"{SEC} is still open after the final round"]
    )
    agent, _ = postmortem_client(tmp_path, [note_payload(), complete])
    run = await agent.run(bundle(open_issues=(issue(),), fixed_issues=()))
    assert run.outcome.repair_retries == 1
    assert SEC in run.value.what_i_would_not_trust[0]


async def test_the_postmortem_runs_at_medium_effort():
    """Summarisation from a trace, not reasoning (docs/03 § 3.5). It reads the longest input
    in the system, so the effort dial is the one that matters for its cost."""
    assert Postmortem(Settings()).effort_for(1) == "medium"
