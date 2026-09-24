"""The eval judge: its blinding, its guards, and Cohen's kappa.

Three things here are load-bearing in a way the rest of the eval is not.

**The blinding cannot be checked after the fact.** Nothing in a results table shows whether
the judge knew which arm it was scoring, so if it leaks there is no way to notice from the
numbers — every column just shifts. It is enforced by the type (`JudgeQuestion` has no arm
field) and asserted against the rendered prompt.

**The kappa is published.** docs/07 says that sentence is worth more than any headline
number, which makes a wrong kappa worse than a missing one. It is twenty lines of arithmetic
and it is tested against hand-worked values.

**The judge must not be able to match something nobody offered.** A verdict naming a key that
was not a candidate cannot be attributed to anything, and would silently inflate M1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_llm_client import FakeProvider
from tribunal.agents.prompts import load
from tribunal.config import AgentConfig, AgentsConfig, LLMConfig, Settings
from tribunal.contracts import (
    Dimension,
    Evidence,
    EvidenceKind,
    Issue,
    JudgeVerdict,
    Severity,
)
from tribunal.eval import Judge, agreement, blind, load_suite, score
from tribunal.eval.judge import BLIND_PREFIX, JudgeQuestion
from tribunal.eval.scoring import apply_verdicts
from tribunal.llm.base import ProviderName
from tribunal.llm.client import LLMClient

CASES = Path(__file__).resolve().parent.parent / "eval" / "cases"
SUITE = {case.id: case for case in load_suite(CASES)}
INJECTION = SUITE["001-shell-injection-report"]
PERF = SUITE["002-quadratic-membership"]


def issue(
    id_: str = "SEC-1",
    dimension: Dimension = Dimension.SECURITY,
    severity: Severity = Severity.HIGH,
    title: str = "shell injection via report_name",
    introduced: bool = False,
) -> Issue:
    return Issue(
        id=id_, dimension=dimension, severity=severity, title=title,
        explanation="report_name reaches the shell unescaped",
        evidence=[Evidence(kind=EvidenceKind.CODE_SPAN, ref="before.py:L7-L7",
                           excerpt="subprocess.run(...)")],
        confidence=0.9, introduced_by_patch=introduced,
        suggested_direction="pass an argv list",
    )


PATCHED = INJECTION.source.replace("shell=True", "shell=False")


def question(**kw: Any) -> JudgeQuestion:
    defaults = dict(
        case=INJECTION,
        label=f"{BLIND_PREFIX}1",
        issue=issue(),
        patched_source=PATCHED,
        candidates=tuple(INJECTION.known_issues),
    )
    return JudgeQuestion(**{**defaults, **kw})


def verdict(**kw: Any) -> JudgeVerdict:
    defaults = dict(
        matched_known_issue="shell_injection", is_real_issue=True, is_regression=False,
        reasoning="same defect: the concatenated name reaches the shell", confidence=0.9,
    )
    return JudgeVerdict(**{**defaults, **kw})


# -- the blinding ---------------------------------------------------------------------------


def test_a_question_has_nowhere_to_put_the_arm():
    """Structural, not a prompt instruction. A judge that knows which column is "the tribunal"
    will flatter it, and no results table would show that it had."""
    assert "arm" not in JudgeQuestion.__dataclass_fields__


def test_no_arm_identifier_reaches_the_rendered_prompt():
    text = question().render()
    for arm in ("B0", "B1", "B2", "B3", "tribunal", "baseline"):
        assert arm not in text, f"{arm!r} leaked into the judge's prompt"


def test_the_real_issue_id_is_replaced_with_a_positional_label():
    """The id is the handle the scorer attributes a verdict back with. It is arm-independent
    today; passing it through would make it a leak the moment anything arm-specific entered
    the derivation."""
    questions = blind(INJECTION, [issue(id_="SEC-abc123"), issue(id_="SEC-def456")], PATCHED)
    assert [q.label for q in questions] == [f"{BLIND_PREFIX}1", f"{BLIND_PREFIX}2"]
    for q in questions:
        assert "SEC-abc123" not in q.render()
        assert "SEC-def456" not in q.render()


def test_one_question_per_issue_rather_than_a_batch():
    """docs/07: "No batch scoring — batching invites the judge to score relative to the
    other items in the batch"."""
    questions = blind(INJECTION, [issue(), issue(id_="SEC-2")], PATCHED)
    assert len(questions) == 2
    first = questions[0].render()
    assert first.count("### The reported issue") == 1


# -- what the question shows --------------------------------------------------------------


def test_the_question_shows_both_files_so_a_regression_is_answerable():
    text = question().render()
    assert "### The original file" in text
    assert "### The patched file" in text
    assert "shell=True" in text  # the original
    assert "shell=False" in text  # the patched


def test_an_unpatched_run_says_so_rather_than_showing_the_file_twice():
    """Nothing can be a regression when no patch applied, and the prompt should not invite
    the judge to hunt for one."""
    text = question(patched_source=INJECTION.source).render()
    assert "no patch was applied" in text
    assert "nothing can be a regression" in text


def test_only_still_unmatched_known_issues_are_offered():
    """Offering one already matched mechanically invites a second, contradictory match that
    would double-count the same defect."""
    text = question(candidates=()).render()
    assert "none are still unmatched" in text
    assert "must be null" in text


def test_the_closing_line_lists_the_only_legal_keys():
    text = question().render()
    assert "shell_injection" in text.rsplit("Emit a JudgeVerdict", 1)[1]


# -- guards ---------------------------------------------------------------------------------


def test_a_match_against_a_key_nobody_offered_is_rejected(tmp_path):
    judge, _ = judge_with(tmp_path, [])
    with pytest.raises(ValueError, match="not one of this case's unmatched keys"):
        judge.post_validate(verdict(matched_known_issue="invented"), question(candidates=()))


def test_a_regression_on_an_unpatched_run_is_rejected(tmp_path):
    judge, _ = judge_with(tmp_path, [])
    with pytest.raises(ValueError, match="no patch was applied"):
        judge.post_validate(
            verdict(matched_known_issue=None, is_regression=True),
            question(patched_source=INJECTION.source, candidates=()),
        )


def test_a_regression_that_is_not_a_real_issue_is_a_schema_error():
    """A patch cannot introduce a problem that is not a problem."""
    with pytest.raises(ValueError, match="cannot introduce a problem"):
        JudgeVerdict(
            matched_known_issue=None, is_real_issue=False, is_regression=True,
            reasoning="x", confidence=0.5,
        )


def test_matching_a_declared_issue_while_calling_it_unreal_is_a_schema_error():
    with pytest.raises(ValueError, match="contradiction"):
        JudgeVerdict(
            matched_known_issue="shell_injection", is_real_issue=False, is_regression=False,
            reasoning="x", confidence=0.5,
        )


# -- the prompt ------------------------------------------------------------------------------


def test_the_judge_prompt_is_versioned():
    prompt = load("judge")
    assert prompt.stamp == "judge/v1"
    assert prompt.changed != "unknown"
    assert prompt.note


def test_the_prompt_tells_the_judge_it_cannot_know_who_wrote_the_issue():
    text = load("judge").text
    assert "You do not know who wrote this" in text


def test_the_prompt_says_null_is_the_common_answer():
    """A judge asked to match things matches things. The default has to be stated."""
    text = load("judge").text
    assert "the common answer" in text
    assert "not a failure" in text


def test_the_judge_is_not_given_a_cheaper_model():
    """docs/07: "Do not use a cheaper model for the judge — judging is harder than
    critiquing, and a weak judge silently caps the eval's resolution"."""
    settings = Settings()
    assert settings.agents.judge.model == settings.agents.redteam.model
    assert settings.agents.judge.effort == "high"


# -- folding verdicts into a scorecard ------------------------------------------------------


def scorecard(case=INJECTION, issues=None):
    from tests.test_eval_cases import report

    return score(case, "B3", report(), [critique_of(issues or [])], [])


def critique_of(issues):
    from tribunal.contracts import Critique

    return Critique(
        dimension=Dimension.SECURITY, round=1,
        verdict="block" if any(i.severity is Severity.HIGH for i in issues) else "clean",
        issues=issues, tools_consulted=["bandit"], summary="s",
    )


def test_the_judge_can_catch_what_no_locator_matched():
    """M1's description fallback: the whole reason the judge exists for M1 at all."""
    card = scorecard(issues=[issue(id_="SEC-1")])
    assert card.known_caught == 0
    assert card.unmatched_known == ["shell_injection"]

    apply_verdicts(card, {f"{BLIND_PREFIX}1": ("SEC-1", verdict())})
    assert card.known_caught == 1
    assert card.matches[0].how == "judge"
    assert card.unmatched_known == []


def test_a_judge_match_is_visibly_not_a_mechanical_one():
    """The results table reports the mechanical share separately, so a reader can see how
    much of the recall number rests on the judge's reliability."""
    card = scorecard(issues=[issue(id_="SEC-1")])
    apply_verdicts(card, {f"{BLIND_PREFIX}1": ("SEC-1", verdict())})
    assert card.known_caught == 1
    assert card.known_caught_mechanically == 0
    assert not card.matches[0].mechanical


def test_a_mechanical_match_is_never_overwritten_by_the_judge():
    """Preference order is rule, then line range, then the judge — and re-judging a recorded
    sweep must not be able to change what was already settled deterministically."""
    from tribunal.contracts import GroundingFinding, finding_id

    b602 = GroundingFinding(
        id=finding_id("bandit", "B602", "before.py", 7), tool="bandit", rule="B602",
        file="before.py", line=7, end_line=7, message="m", tool_severity="HIGH", raw={},
    )
    cited = Issue(
        id="SEC-1", dimension=Dimension.SECURITY, severity=Severity.HIGH, title="t",
        explanation="e",
        evidence=[Evidence(kind=EvidenceKind.TOOL_FINDING, ref=b602.id, excerpt="x")],
        confidence=0.9, introduced_by_patch=False, suggested_direction="d",
    )
    from tests.test_eval_cases import report

    card = score(INJECTION, "B3", report(), [critique_of([cited])], [b602])
    assert card.matches[0].how == "rule"

    apply_verdicts(card, {f"{BLIND_PREFIX}1": ("SEC-999", verdict())})
    assert card.matches[0].how == "rule"
    assert card.matches[0].issue_id == "SEC-1"


def test_m2_counts_only_real_regressions():
    card = scorecard(issues=[issue(id_="SEC-1")])
    apply_verdicts(card, {
        "a": ("SEC-1", verdict(matched_known_issue=None, is_regression=True)),
        "b": ("SEC-2", verdict(matched_known_issue=None, is_regression=False)),
    })
    assert card.regressions == 1


def test_m2_is_none_until_the_judge_has_run():
    """Distinct from 0, which would claim the run introduced nothing."""
    assert scorecard().regressions is None


def test_re_judging_cannot_catch_a_known_issue_twice():
    card = scorecard(issues=[issue(id_="SEC-1")])
    apply_verdicts(card, {
        "a": ("SEC-1", verdict()),
        "b": ("SEC-2", verdict()),
    })
    assert card.known_caught == 1
    assert card.known_total == 1


# -- Cohen's kappa ----------------------------------------------------------------------------


def test_perfect_agreement_on_two_categories_is_one():
    assert agreement([("a", "a"), ("b", "b"), ("a", "a"), ("b", "b")]).kappa == 1.0


def test_chance_level_agreement_is_zero():
    """Two raters each saying `a` half the time, agreeing exactly as often as chance
    predicts: observed 0.5, expected 0.5, kappa 0."""
    labels = [("a", "a"), ("a", "b"), ("b", "a"), ("b", "b")]
    result = agreement(labels)
    assert result.observed == 0.5
    assert result.expected == 0.5
    assert result.kappa == 0.0


def test_a_hand_worked_value():
    """observed = 3/4, expected = (3/4 * 2/4) + (1/4 * 2/4) = 0.5, kappa = 0.25/0.5 = 0.5."""
    labels = [("a", "a"), ("a", "a"), ("a", "b"), ("b", "b")]
    result = agreement(labels)
    assert result.observed == 0.75
    assert result.expected == 0.5
    assert result.kappa == 0.5


def test_total_disagreement_is_negative():
    assert agreement([("a", "b"), ("b", "a")]).kappa < 0


def test_one_category_everywhere_reports_one_rather_than_dividing_by_zero():
    """The degenerate case: chance agreement is also 1.0. Reporting 1.0 and letting `n` and
    the category count speak is better than a ZeroDivisionError in a published statistic."""
    result = agreement([("a", "a")] * 5)
    assert result.kappa == 1.0
    assert result.n == 5


def test_the_usable_threshold_is_the_one_docs_07_states():
    """"If κ < 0.6, fix the judge rubric before running anything on held-out"."""
    assert not agreement([("a", "a"), ("a", "b"), ("b", "a"), ("b", "b")]).usable
    assert agreement([("a", "a")] * 9 + [("a", "b"), ("b", "b")] * 3).usable is not None


def test_disagreements_are_kept_so_the_rubric_can_be_fixed():
    result = agreement([("a", "a"), ("a", "b"), ("b", "c")])
    assert [(hand, judged) for _, hand, judged in result.disagreements] == [
        ("a", "b"), ("b", "c")
    ]


def test_the_rendered_line_states_n_and_the_verdict():
    text = agreement([("a", "a")] * 10 + [("a", "b")]).render()
    assert "κ =" in text and "n = 11" in text


def test_an_empty_label_set_is_not_a_kappa_of_one():
    """A judge nobody has checked must not report as perfectly agreeing."""
    result = agreement([])
    assert result.kappa == 0.0
    assert not result.usable


# -- end to end through a fake provider ---------------------------------------------------


def judge_with(tmp_path: Path, script: list[Any]) -> tuple[Judge, FakeProvider]:
    settings = Settings(
        llm=LLMConfig(cassette_dir=tmp_path),
        agents=AgentsConfig(
            judge=AgentConfig(model="fake-1", provider=ProviderName.NIM, max_tokens=4000)
        ),
    )
    fake = FakeProvider(script=script)
    client = LLMClient(settings)
    client.providers[ProviderName.NIM] = fake
    return Judge(settings, client), fake


async def test_the_judge_runs_end_to_end(tmp_path):
    payload = json.loads(verdict().model_dump_json())
    judge, _ = judge_with(tmp_path, [payload])
    run = await judge.run(question())
    assert run.role == "judge"
    assert run.prompt_version == "judge/v1"
    assert run.value.matched_known_issue == "shell_injection"
    assert run.metrics["matched"] is True
    assert run.outcome.repair_retries == 0


async def test_an_invented_key_costs_one_repair_retry_and_then_lands(tmp_path):
    bad = json.loads(verdict(matched_known_issue="not_a_key").model_dump_json())
    good = json.loads(verdict().model_dump_json())
    judge, _ = judge_with(tmp_path, [bad, good])
    run = await judge.run(question())
    assert run.outcome.repair_retries == 1
    assert run.value.matched_known_issue == "shell_injection"


async def test_the_prompt_the_judge_actually_receives_carries_no_arm(tmp_path):
    """The end-to-end version: not the rendered string in isolation, but what the client
    sends."""
    judge, fake = judge_with(tmp_path, [json.loads(verdict().model_dump_json())])
    await judge.run(question())
    sent = fake.seen[0]
    for arm in ("B0", "B1", "B2", "B3"):
        assert arm not in sent.user
        assert arm not in sent.system
