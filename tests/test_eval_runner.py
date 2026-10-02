"""The arms and the sweep, driven against a scripted provider. Zero API calls.

The sweep is the part of the eval that costs ~$25 to get wrong, and the failures that matter
are the quiet ones: a cost column that is really a running total, a known issue nothing could
match, a crashed case that takes the other twenty-three with it. Each of those is a test here.

Every arm runs for real — the real Coder, the real VALIDATE loop, the real grounding suite,
the real orchestrator for B3. Only the provider is scripted, which is the same boundary
`tests/test_orchestrator.py` draws.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.test_llm_client import FakeProvider
from tribunal.cli import app
from tribunal.config import AgentConfig, AgentsConfig, LLMConfig, Settings
from tribunal.contracts import Decision
from tribunal.eval import load_suite
from tribunal.eval.arms import ARM_IDS, ARMS, findings_in
from tribunal.eval.runner import results_dir_for, summary, sweep
from tribunal.llm.base import ProviderName
from tribunal.llm.client import LLMClient
from tribunal.trace import reader as trace_reader

CASES = Path(__file__).resolve().parent.parent / "eval" / "cases"
SUITE = {case.id: case for case in load_suite(CASES)}
INJECTION = SUITE["001-shell-injection-report"]
CANARY = SUITE["004-normalise-names"]

SHELL_LINE = next(
    line for line in INJECTION.source.splitlines() if "subprocess.run" in line
)
ARGV_LINE = (
    '    subprocess.run(["makereport", report_name], '
    'stdout=open(destination, "wb"))'
)


def settings_for(tmp_path: Path) -> Settings:
    def agent() -> AgentConfig:
        return AgentConfig(model="fake-1", provider=ProviderName.NIM)

    return Settings(
        llm=LLMConfig(cassette_dir=tmp_path / "cassettes"),
        agents=AgentsConfig(
            coder=agent(), redteam=agent(), profiler=agent(),
            arbiter=agent(), arbiter_affirm=agent(), postmortem=agent(),
        ),
    )


def proposal(search: str = SHELL_LINE, replace: str = ARGV_LINE, round_: int = 1) -> dict:
    return {
        "round": round_, "edits": [{"search": search, "replace": replace}], "diff": "",
        "rationale": "pass an argv list", "addresses": [], "deliberately_unaddressed": [],
    }


def critique(dimension: str, round_: int, issues: list[dict] | None = None) -> dict:
    issues = issues or []
    # `clean` with issues present is a schema error -- the three verdicts are not
    # interchangeable, and a helper that got this wrong spent a scripted response on a
    # repair retry and then failed the run two calls later with an unrelated message.
    if any(i["severity"] == "high" for i in issues):
        verdict = "block"
    elif issues:
        verdict = "concerns"
    else:
        verdict = "clean"
    return {
        "dimension": dimension, "round": round_,
        "verdict": verdict,
        "issues": issues, "positive_notes": [],
        "tools_consulted": ["bandit" if dimension == "security" else "radon"],
        "summary": "a summary",
    }


def grounded_issue(finding_id_: str, severity: str = "high") -> dict:
    """An issue citing a real grounding finding — the shape M1's rule locator matches."""
    return {
        "id": "model-supplied", "dimension": "security", "severity": severity,
        "title": "shell injection", "explanation": "reaches the shell",
        "confidence": 0.9, "introduced_by_patch": False,
        "suggested_direction": "pass an argv list",
        "evidence": [{"kind": "tool_finding", "ref": finding_id_, "excerpt": "B602"}],
    }


def arbiter_note(round_: int, decision: str = "reject") -> dict:
    return {
        "round": round_, "decision_echo": decision,
        "consolidated_critique": "replace the shell call with an argv list"
        if decision == "reject" else None,
        "priority_order": [], "dismissed": [],
        "tradeoff_justification": None, "recommended_default": None,
    }


def write_up(rounds: int = 1) -> dict:
    """The Postmortem requires one entry per round that produced a decision, so a helper
    that hard-coded a single round would fail on any run that went twice — which is what
    `validate_postmortem` is for, and what it caught here."""
    return {
        "outcome_echo": "accept", "headline": "a shell call concatenated a parameter",
        "rounds": [
            {"round": n, "what_changed": "a change", "outcome": "an outcome"}
            for n in range(1, rounds + 1)
        ],
        "disagreement": None, "what_i_would_not_trust": ["single-file review"],
        "human_should_check": [],
    }


WRITE_UP = write_up()


def run_sweep(
    tmp_path: Path, cases, arms: list[str], script: list[Any], **kw: Any
):
    settings = settings_for(tmp_path)
    fake = FakeProvider()
    fake.script = list(script)
    client = LLMClient(settings)
    client.providers[ProviderName.NIM] = fake
    return asyncio.run(
        sweep(cases, arms, settings, client, results_dir=tmp_path / "out", **kw)
    ), fake


# -- the arms ---------------------------------------------------------------------------


def test_b0_is_shown_no_tool_output(tmp_path):
    """The floor. If B0 saw the findings it would be B1, and the two columns would be
    measuring the same thing."""
    _, fake = run_sweep(tmp_path, [INJECTION], ["B0"], [proposal()])
    prompt = fake.seen[0].user
    assert "B602" not in prompt
    assert "none — these tools ran clean" in prompt or "(none" in prompt


def test_b1_is_shown_the_tool_output(tmp_path):
    """docs/07: B1 is "the baseline that makes the eval credible", and what makes it that
    is having everything B3 has except the debate."""
    _, fake = run_sweep(tmp_path, [INJECTION], ["B1"], [proposal()])
    prompt = fake.seen[0].user
    assert "B602" in prompt


def test_every_arm_produces_a_scorecard(tmp_path):
    result, _ = run_sweep(
        tmp_path, [INJECTION], ["B0", "B1"], [proposal(), proposal()]
    )
    assert {card.arm for card in result.cards} == {"B0", "B1"}
    assert all(card.case_id == INJECTION.id for card in result.cards)


def test_a_baseline_has_no_outcome_to_score(tmp_path):
    """docs/07's results table says M5 is `n/a` for B0-B2. Forcing them through the tribunal's
    Verdict vocabulary would either fake a verdict or escalate every run."""
    result, _ = run_sweep(tmp_path, [INJECTION], ["B0"], [proposal()])
    card = result.cards[0]
    assert card.actual_outcome is None
    assert card.outcome_correct is None


def test_the_crew_arm_does_have_an_outcome(tmp_path):
    result, _ = run_sweep(
        tmp_path, [INJECTION], ["B3"],
        [proposal(), critique("security", 1), critique("performance", 1), WRITE_UP],
    )
    assert result.cards[0].actual_outcome is Decision.ACCEPT


# -- the two bugs a sweep hides ------------------------------------------------------------


def test_each_arm_reports_its_own_cost_not_the_running_total(tmp_path):
    """One `LLMClient` is shared across the sweep, so `client.total_cost_usd` is the total
    for everything run so far. Serially that makes each arm look like it cost what every
    earlier arm cost too; concurrently the number depends on scheduling. M7 is published.
    """
    result, _ = run_sweep(
        tmp_path, [INJECTION], ["B0", "B1"], [proposal(), proposal()]
    )
    by_arm = {card.arm: card.cost_usd for card in result.cards}
    # B1 grounds but the extra work is tool time, not tokens: both arms make exactly one
    # LLM call, so their costs must be equal rather than cumulative.
    assert by_arm["B0"] == by_arm["B1"] > 0
    assert by_arm["B1"] < sum(by_arm.values())


def test_the_crew_can_match_a_rule_locator_from_its_own_trace(tmp_path):
    """`tool_run` used to record only `findings_count`, so a critique's `tool_finding` ref
    could not be resolved back to a rule — and every `rule` locator would have scored as a
    miss against B3 specifically. The same shape as docs/13 § 32, found the same way.

    Round 1 deliberately patches something *else*, so the injection survives into the
    patched grounding and the Red-team has a live finding to cite. A critic citing a finding
    its own patch removed is rejected by `validation.py`, which is correct and is what an
    earlier version of this test tripped over.
    """
    import asyncio as _asyncio

    from tribunal.config import Settings as _Settings
    from tribunal.grounding.suite import GroundingSuite

    ground = _asyncio.run(
        GroundingSuite(_Settings()).run(INJECTION.source, logical_name="before.py")
    )
    b602 = next(f for f in ground.report.findings if f.rule == "B602")

    result, _ = run_sweep(
        tmp_path, [INJECTION], ["B3"],
        [
            # Round 1 touches the return, not the shell call, so B602 is still there for
            # the critic to cite.
            proposal(search="    return destination", replace="    return str(destination)"),
            critique("security", 1, [grounded_issue(b602.id)]),
            critique("performance", 1),
            arbiter_note(1),
            # Round 2 actually fixes it.
            proposal(round_=2),
            critique("security", 2),
            critique("performance", 2),
            write_up(rounds=2),
        ],
    )
    assert not result.errors, result.errors
    card = result.cards[0]
    assert card.known_caught == 1, card.unmatched_known
    assert card.matches[0].how == "rule"
    assert card.rounds_used == 2


def test_findings_are_recoverable_from_a_written_trace(tmp_path):
    """The narrower version of the same claim, against the file on disk."""
    result, _ = run_sweep(tmp_path, [INJECTION], ["B1"], [proposal()])
    traces = sorted((tmp_path / "out" / "traces").glob("*.jsonl"))
    assert traces
    findings = findings_in(trace_reader.read(traces[0]))
    assert "B602" in {f.rule for f in findings}


# -- the sweep --------------------------------------------------------------------------


def test_one_case_failing_does_not_lose_the_others(tmp_path):
    """A sweep is ~$25 and ~30 minutes. An exception on case 11 of 24 must not discard the
    first ten."""
    result, _ = run_sweep(
        tmp_path, [INJECTION, CANARY], ["B0"],
        [proposal(), RuntimeError("the provider fell over"), RuntimeError("again")],
    )
    assert len(result.errors) == 1
    assert len(result.cards) == 1
    assert "RuntimeError" in result.errors[0].error


def test_errors_are_counted_separately_rather_than_averaged_in(tmp_path):
    """A crashed run and a run that produced a bad patch are different facts."""
    result, _ = run_sweep(
        tmp_path, [INJECTION, CANARY], ["B0"],
        [proposal(), RuntimeError("boom"), RuntimeError("boom")],
    )
    text = summary(result)
    assert "1 run(s) errored and are excluded" in text
    m4 = next(line for line in text.splitlines() if "M4 patch" in line)
    # One case scored, one errored: M4 is 1/1, not 1/2. The crash is reported, not averaged.
    assert m4.split("|")[2].strip() == "1/1"


def test_an_unknown_arm_is_rejected_before_anything_runs(tmp_path):
    settings = settings_for(tmp_path)
    with pytest.raises(ValueError, match="unknown arm"):
        asyncio.run(sweep([INJECTION], ["B9"], settings))


def test_concurrency_is_bounded(tmp_path):
    """docs/07 § Runner: a semaphore, default 4. Unbounded would rate-limit a real sweep
    and make the wall-clock numbers meaningless."""
    live = 0
    peak = 0
    original = ARMS["B0"]

    async def counting(case, settings, client, trace_dir):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        try:
            return await original(case, settings, client, trace_dir)
        finally:
            live -= 1

    ARMS["B0"] = counting
    try:
        run_sweep(
            tmp_path, list(SUITE.values()), ["B0"], [proposal()] * 12, concurrency=2
        )
    finally:
        ARMS["B0"] = original
    assert peak <= 2


# -- the artifact -------------------------------------------------------------------------


def test_the_results_directory_is_written_with_a_header(tmp_path):
    """docs/07: "a score in a README with no run behind it is not evidence", and "without
    [the header], two sweeps are not comparable"."""
    result, _ = run_sweep(tmp_path, [INJECTION], ["B0"], [proposal()])
    directory = result.directory
    assert (directory / "summary.md").is_file()
    rows = [
        json.loads(line)
        for line in (directory / "raw.jsonl").read_text().splitlines()
    ]
    head = rows[0]
    assert head["kind"] == "header"
    for required in (
        "prompt_versions", "models", "efforts", "severity_weights",
        "price_table_version", "schema_version", "policy", "splits",
    ):
        assert required in head, f"the header omits {required}"
    assert head["severity_weights"] == {"info": 0, "low": 1, "medium": 4, "high": 16}


def test_raw_rows_carry_what_a_rescore_needs(tmp_path):
    result, _ = run_sweep(tmp_path, [INJECTION], ["B0"], [proposal()])
    rows = [
        json.loads(line)
        for line in (result.directory / "raw.jsonl").read_text().splitlines()
    ]
    row = next(r for r in rows if r["kind"] == "score")
    assert row["case"] == INJECTION.id
    assert "unmatched_known" in row and "matched" in row
    assert "cost_usd" in row and "rounds_used" in row


def test_every_run_leaves_a_readable_trace(tmp_path):
    """The property that makes `--replay` possible: judge-prompt iteration is free after
    the first sweep only if the traces are there and parse."""
    run_sweep(tmp_path, [INJECTION], ["B0", "B1"], [proposal(), proposal()])
    traces = sorted((tmp_path / "out" / "traces").glob("*.jsonl"))
    assert len(traces) == 2
    for path in traces:
        trace = trace_reader.read(path)
        assert trace.events[0].kind == "run_start"
        assert trace.events[-1].kind == "run_end"


def test_the_summary_is_counts_and_carries_the_caveats(tmp_path):
    result, _ = run_sweep(tmp_path, [INJECTION], ["B0", "B1"], [proposal(), proposal()])
    text = summary(result)
    assert "M4 patch applies+passes" in text
    assert text.index("M4 patch") < text.index("M1 known-issue")  # mechanical first
    assert "n=1" in text
    assert "likely resemble training data" in text
    assert "judge: not run" in text
    assert "Nothing in this table depends on a judge" in text
    assert "%" not in text  # counts, never rates


def test_the_results_directory_is_timestamped(tmp_path):
    first = results_dir_for(tmp_path, when=1_700_000_000)
    assert first.name == "20231114T221320Z"


# -- the CLI ------------------------------------------------------------------------------


def test_eval_dry_run_lists_the_cases_without_running_them():
    outcome = CliRunner().invoke(app, ["eval", "--dry-run"])
    assert outcome.exit_code == 0, outcome.output
    assert "001-shell-injection-report" in outcome.output
    assert "case(s) x" in outcome.output


def test_the_committed_benchmark_is_complete():
    """24 cases in docs/07's exact composition. Until it was, every sweep printed a warning;
    this asserts the warning has stopped being true rather than stopped being printed."""
    outcome = CliRunner().invoke(app, ["eval", "--dry-run"])
    assert outcome.exit_code == 0, outcome.output
    assert "the benchmark is incomplete" not in outcome.output


def test_eval_warns_when_the_benchmark_is_partial(tmp_path):
    """A sweep over a partial set is a real number about a different benchmark, and the
    loudest place to say so is the command that produces it. Isolated against its own case
    directory so it keeps testing that as the real set changes."""
    import shutil

    cases = tmp_path / "cases"
    cases.mkdir()
    shutil.copytree(CASES / INJECTION.id, cases / INJECTION.id)
    outcome = CliRunner().invoke(app, ["eval", "--dry-run", "--cases", str(cases)])
    assert "the benchmark is incomplete" in outcome.output


def test_eval_rejects_an_unknown_arm():
    outcome = CliRunner().invoke(app, ["eval", "--dry-run", "--arms", "B1,B9"])
    assert outcome.exit_code == 64
    assert "unknown arm" in outcome.output


def test_eval_rejects_a_split_with_no_cases(tmp_path):
    """Isolated against its own case directory rather than the committed set: the real set
    grows, and a test that assumed "nothing is held out yet" would pass until it silently
    stopped testing anything."""
    import shutil

    cases = tmp_path / "cases"
    cases.mkdir()
    shutil.copytree(CASES / INJECTION.id, cases / INJECTION.id)  # a dev case
    outcome = CliRunner().invoke(
        app, ["eval", "--dry-run", "--split", "heldout", "--cases", str(cases)]
    )
    assert outcome.exit_code == 64
    assert "no cases matched" in outcome.output


def test_smoke_selects_a_fixed_slice_and_forbids_api_calls(tmp_path):
    """`--smoke` replays 4 cases with zero API calls. Replay mode is
    set by the command rather than by the environment, so a cassette miss raises instead of
    quietly costing money on someone's laptop."""
    outcome = CliRunner().invoke(
        app, ["eval", "--smoke", "--dry-run", "--arms", "B1"]
    )
    assert outcome.exit_code == 0, outcome.output
    assert outcome.output.count("dev") <= 4


def test_smoke_with_missing_recordings_fails_without_calling_a_provider(tmp_path, monkeypatch):
    from tribunal.llm.registry import build_all

    settings = Settings(llm=LLMConfig(mode="live", cassette_dir=tmp_path / "cassettes"))
    monkeypatch.setattr(Settings, "load", lambda *args, **kwargs: settings)
    calls = []

    async def forbid_network(*args, **kwargs):
        calls.append(args)
        raise AssertionError("a replay must never call a live provider")

    for provider in build_all(settings.providers).values():
        monkeypatch.setattr(type(provider), "complete", forbid_network)
    outcome = CliRunner().invoke(app, ["eval", "--smoke", "--arms", "B1,B3"])

    assert outcome.exit_code == 3, outcome.output
    assert "this gate is unarmed" in outcome.output
    assert "CassetteMiss" in outcome.output
    assert calls == []
    assert not (tmp_path / "cassettes").exists()


def test_the_arm_ids_match_the_documented_baselines():
    assert ARM_IDS == ("B0", "B1", "B2", "B3")
    assert set(ARMS) == set(ARM_IDS)


# -- the judge pass, and replay -------------------------------------------------------------


def judged_settings(tmp_path: Path) -> Any:
    """The scripted provider, with the judge routed at it too."""
    from tribunal.config import AgentConfig

    settings = settings_for(tmp_path)
    return settings.model_copy(
        update={
            "agents": settings.agents.model_copy(
                update={
                    "judge": AgentConfig(
                        model="fake-1", provider=ProviderName.NIM, max_tokens=4000
                    )
                }
            )
        }
    )


def judge_payload(
    matched: str | None = None, real: bool = True, regression: bool = False
) -> dict:
    from tribunal.contracts import JudgeVerdict

    return json.loads(
        JudgeVerdict(
            matched_known_issue=matched, is_real_issue=real, is_regression=regression,
            reasoning="because of the mechanism described", confidence=0.85,
        ).model_dump_json()
    )


def span_issue(span: str, severity: str = "high") -> dict:
    """An issue with a code_span nothing in 001 has a locator for, so it stays unclaimed."""
    return {
        "id": "model-supplied", "dimension": "security", "severity": severity,
        "title": "an unlocated concern", "explanation": "something is wrong here",
        "confidence": 0.9, "introduced_by_patch": False,
        "suggested_direction": "change it",
        "evidence": [{"kind": "code_span", "ref": f"before.py:{span}", "excerpt": "..."}],
    }


def run_judged(tmp_path: Path, arms: list[str], script: list[Any], **kw: Any):
    from tribunal.eval.judge import Judge

    settings = judged_settings(tmp_path)
    fake = FakeProvider()
    fake.script = list(script)
    client = LLMClient(settings)
    client.providers[ProviderName.NIM] = fake
    return asyncio.run(
        sweep([INJECTION], arms, settings, client, results_dir=tmp_path / "out",
              judge=Judge(settings, client), **kw)
    ), fake


def test_the_judge_catches_what_no_locator_could(tmp_path):
    """M1's description fallback, end to end: the critic reports the injection with a code
    span, the case locates it by `rule`, nothing matches mechanically, and the judge
    settles it."""
    result, _ = run_judged(
        tmp_path, ["B3"],
        [
            proposal(search="    return destination", replace="    return str(destination)"),
            critique("security", 1, [span_issue("L7-L7")]),
            critique("performance", 1),
            arbiter_note(1),
            proposal(round_=2),
            critique("security", 2),
            critique("performance", 2),
            write_up(rounds=2),
            judge_payload(matched="shell_injection"),
        ],
    )
    assert not result.errors, result.errors
    card = result.cards[0]
    assert card.known_caught == 1
    assert card.known_caught_mechanically == 0
    assert card.matches[0].how == "judge"


def test_the_judge_is_not_asked_about_what_a_locator_already_settled(tmp_path):
    """Re-asking would let a paid, non-deterministic answer overturn a free one — and would
    cost a call per issue on every sweep for nothing."""
    import asyncio as _asyncio

    from tribunal.config import Settings as _Settings
    from tribunal.grounding.suite import GroundingSuite

    ground = _asyncio.run(
        GroundingSuite(_Settings()).run(INJECTION.source, logical_name="before.py")
    )
    b602 = next(f for f in ground.report.findings if f.rule == "B602")

    result, fake = run_judged(
        tmp_path, ["B3"],
        [
            proposal(search="    return destination", replace="    return str(destination)"),
            critique("security", 1, [grounded_issue(b602.id)]),
            critique("performance", 1),
            arbiter_note(1),
            proposal(round_=2),
            critique("security", 2),
            critique("performance", 2),
            write_up(rounds=2),
            # No judge payload scripted: asking would raise IndexError.
        ],
    )
    assert not result.errors, result.errors
    card = result.cards[0]
    assert card.matches[0].how == "rule"
    assert card.regressions == 0  # judged, and there was nothing to ask about


def test_m2_counts_a_regression_the_judge_confirms(tmp_path):
    """B2 rather than B3: it has a critic (so there are issues to judge) and no policy
    layer or write-up, so the script is the three calls the test is actually about."""
    result, _ = run_judged(
        tmp_path, ["B2"],
        [
            proposal(),
            critique("security", 1, [span_issue("L7-L7", severity="medium")]),
            proposal(round_=2, search="    return destination",
                     replace="    return str(destination)"),
            judge_payload(matched=None, real=True, regression=True),
        ],
    )
    assert not result.errors, result.errors
    assert result.cards[0].regressions == 1


def test_a_judged_sweep_says_it_was_not_validated(tmp_path):
    """A table with judge-derived rows and no kappa beside them claims more than it has
    earned, so the absence is printed rather than omitted."""
    result, _ = run_judged(
        tmp_path, ["B1"],
        [proposal(search="    return destination", replace="    return str(destination)")],
    )
    text = summary(result)
    assert "NOT VALIDATED" in text
    assert "M2 regressions introduced" in text


def test_an_unjudged_sweep_says_nothing_depends_on_a_judge(tmp_path):
    result, _ = run_sweep(tmp_path, [INJECTION], ["B1"], [proposal()])
    text = summary(result)
    assert "judge: not run" in text
    assert "M2 regressions introduced | —" in text.replace("  ", " ") or "—" in text


# -- replay -------------------------------------------------------------------------------


def test_replay_reproduces_the_mechanical_scores_with_no_calls(tmp_path):
    """docs/07: "`--replay` re-scores from traces without re-running the debate". The
    mechanical metrics are recomputed rather than read back, so a change to `scoring.py` is
    visible on old sweeps — a stored number would hide it."""
    from tribunal.eval.runner import rescore

    first, _ = run_sweep(tmp_path, [INJECTION], ["B0", "B1"], [proposal(), proposal()])
    settings = settings_for(tmp_path)
    again = asyncio.run(rescore(tmp_path / "out", [INJECTION], settings))

    assert {c.arm for c in again.cards} == {"B0", "B1"}
    before = {(c.arm, c.known_caught, c.fix_correct) for c in first.cards}
    after = {(c.arm, c.known_caught, c.fix_correct) for c in again.cards}
    assert before == after
    assert again.header["replayed_from"].endswith("out")


def test_replay_re_judges_without_re_running_the_debate(tmp_path):
    """The property worth the whole implementation: iterating on the judge's prompt costs
    one call per issue, not a sweep."""
    from tribunal.eval.judge import Judge
    from tribunal.eval.runner import rescore

    run_judged(
        tmp_path, ["B3"],
        [
            proposal(search="    return destination", replace="    return str(destination)"),
            critique("security", 1, [span_issue("L7-L7")]),
            critique("performance", 1),
            arbiter_note(1),
            proposal(round_=2),
            critique("security", 2),
            critique("performance", 2),
            write_up(rounds=2),
            judge_payload(matched=None, real=False),
        ],
    )
    # Second pass: only the judge is scripted, so any coder or critic call would raise.
    settings = judged_settings(tmp_path)
    fake = FakeProvider()
    fake.script = [judge_payload(matched="shell_injection")]
    client = LLMClient(settings)
    client.providers[ProviderName.NIM] = fake
    again = asyncio.run(
        rescore(tmp_path / "out", [INJECTION], settings, Judge(settings, client))
    )
    assert len(fake.seen) == 1, "re-scoring re-ran something other than the judge"
    assert again.cards[0].known_caught == 1
    assert again.cards[0].matches[0].how == "judge"


def test_replay_refuses_a_trace_whose_diff_no_longer_applies(tmp_path):
    """The case's `before.py` was edited after the sweep. Judging the recorded issues
    against the original would attribute regressions to the wrong file."""
    from tribunal.eval.arms import replay_run
    from tribunal.trace import reader as trace_reader

    run_sweep(tmp_path, [INJECTION], ["B1"], [proposal()])
    trace_path = next((tmp_path / "out" / "traces").glob("*.jsonl"))

    from dataclasses import replace as dc_replace

    moved = dc_replace(INJECTION, source="# an entirely different file\n")
    run = replay_run(trace_reader.read(trace_path), moved, "B1")
    assert run.error is not None
    assert "no longer applies" in run.error


def test_replay_needs_traces_to_re_score(tmp_path):
    from tribunal.eval.runner import rescore

    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match="no traces"):
        asyncio.run(rescore(tmp_path / "empty", [INJECTION], settings_for(tmp_path)))


def test_a_recorded_run_identifies_itself_rather_than_relying_on_its_filename(tmp_path):
    """B3 runs through the real `Orchestrator`, which names its trace `<run_id>.jsonl` and
    knows nothing about arms. A filename parse silently skipped every tribunal run and
    re-scored only the baselines — the one shape of bug a results table never reveals."""
    from tribunal.eval.runner import _identify
    from tribunal.trace import reader as trace_reader

    run_sweep(tmp_path, [INJECTION], ["B3"], [
        proposal(), critique("security", 1), critique("performance", 1), write_up(),
    ])
    traces = [
        p for p in sorted((tmp_path / "out" / "traces").glob("*.jsonl"))
        if p.name != "summary.jsonl"
    ]
    assert traces
    assert _identify(trace_reader.read(traces[0])) == ("B3", INJECTION.id)


def test_a_trace_that_is_not_an_eval_run_is_skipped(tmp_path):
    """A `traces/` directory collects strays — an ordinary `tribunal run`, a copied file.
    Re-scoring one against a case it was never about would invent a row."""
    from tribunal.eval.runner import _identify
    from tribunal.trace import events
    from tribunal.trace import reader as trace_reader
    from tribunal.trace.writer import TraceWriter

    path = tmp_path / "plain.jsonl"
    with TraceWriter("01PLAIN", path) as writer:
        writer.emit(
            events.run_start, input_file="t.py", input_sha256="x", config_snapshot={},
            prompt_versions={}, argv=["run", "t.py"], trace_level="default",
        )
    assert _identify(trace_reader.read(path)) is None


def test_the_orchestrators_own_summary_file_is_not_read_as_a_trace(tmp_path):
    """`Orchestrator` writes `summary.jsonl` into the same directory it writes traces to.
    It is a different shape entirely, and re-scoring choked on it."""
    from tribunal.eval.runner import rescore

    run_sweep(tmp_path, [INJECTION], ["B3"], [
        proposal(), critique("security", 1), critique("performance", 1), write_up(),
    ])
    assert (tmp_path / "out" / "traces" / "summary.jsonl").is_file()
    again = asyncio.run(rescore(tmp_path / "out", [INJECTION], settings_for(tmp_path)))
    assert len(again.cards) == 1


def test_eval_replay_on_a_directory_with_no_traces_is_a_usage_error(tmp_path):
    (tmp_path / "nope").mkdir()
    outcome = CliRunner().invoke(app, ["eval", "--replay", str(tmp_path / "nope")])
    assert outcome.exit_code == 64
    assert "no traces under" in outcome.output


# -- report.html ----------------------------------------------------------------------------


def report_for(tmp_path: Path, cases, arms: list[str], script: list[Any], **kw: Any) -> str:
    from tribunal.eval.judge import Judge

    settings = judged_settings(tmp_path)
    fake = FakeProvider()
    fake.script = list(script)
    client = LLMClient(settings)
    client.providers[ProviderName.NIM] = fake
    judge = Judge(settings, client) if kw.pop("judged", False) else None
    asyncio.run(
        sweep(cases, arms, settings, client, results_dir=tmp_path / "out",
              judge=judge, **kw)
    )
    return (tmp_path / "out" / "report.html").read_text(encoding="utf-8")


def test_the_results_directory_has_all_three_artifacts(tmp_path):
    """docs/07 § Runner names raw.jsonl, summary.md and report.html. They are not
    redundant: one is re-scored, one goes in a commit comment, one is read."""
    run_sweep(tmp_path, [INJECTION], ["B0"], [proposal()])
    for name in ("raw.jsonl", "summary.md", "report.html"):
        assert (tmp_path / "out" / name).is_file(), name


def test_the_report_needs_no_javascript_and_no_network(tmp_path):
    """A results report has no interaction beyond expanding a detail, which `<details>`
    does natively — so it ships without the entire class of problem `viewer.js` defends
    against."""
    html = report_for(tmp_path, [INJECTION], ["B0"], [proposal()])
    assert "<script" not in html
    assert "http://" not in html and "https://" not in html
    assert "<details" in html


def test_the_report_leads_with_the_mechanical_metric(tmp_path):
    """docs/07: "M4 is the metric to lead with in the results table, because it's fully
    mechanical — no judge, no interpretation"."""
    html = report_for(tmp_path, [INJECTION], ["B0"], [proposal()])
    assert html.index("M4 patch applies") < html.index("M1 known-issue recall")
    assert html.index("M1 known-issue recall") < html.index("M2 regressions")


def test_the_report_shows_m1s_mechanical_share_separately(tmp_path):
    html = report_for(tmp_path, [INJECTION], ["B0"], [proposal()])
    assert "of which mechanical" in html


def test_the_report_carries_the_provenance_header(tmp_path):
    """"Without that, two sweeps are not comparable"."""
    html = report_for(tmp_path, [INJECTION], ["B0"], [proposal()])
    for field in ("models", "prompts", "severity weights", "prices", "schema", "splits"):
        assert field in html, field


def test_an_unvalidated_judge_is_called_out_prominently(tmp_path):
    """A reader cannot tell from the numbers, so the report has to say it."""
    html = report_for(
        tmp_path, [INJECTION], ["B1"],
        [proposal(search="    return destination", replace="    return str(destination)")],
        judged=True,
    )
    assert "has not been validated" in html
    assert "banner bad" in html


def test_a_run_with_no_judge_says_nothing_depends_on_one(tmp_path):
    html = report_for(tmp_path, [INJECTION], ["B0"], [proposal()])
    assert "No judge was involved" in html
    assert "reproducible from the traces" in html


def test_a_partial_benchmark_is_labelled_as_one(tmp_path):
    """A sweep over a subset is a real number about a different benchmark."""
    html = report_for(tmp_path, [INJECTION], ["B0"], [proposal()])
    assert "Partial benchmark: 1 of 24" in html


def test_the_per_case_matrix_shows_every_case_and_arm(tmp_path):
    """The aggregate answers "which arm is better"; the matrix answers "on what", which is
    how you tell a capability from one lucky case at n=16."""
    html = report_for(
        tmp_path, [INJECTION, CANARY], ["B0", "B1"],
        [proposal(), proposal(), proposal(search="x", replace="y"),
         proposal(search="x", replace="y")],
    )
    assert 'class="matrix"' in html
    assert INJECTION.id in html and CANARY.id in html
    assert "security_only" in html and "canary_clean" in html


def test_the_detail_shows_how_each_issue_was_matched(tmp_path):
    """A recall number whose provenance is one click away is harder to overstate."""
    html = report_for(tmp_path, [INJECTION], ["B0"], [proposal()])
    assert "How each issue was matched" in html
    assert "shell_injection" in html
    assert "locators:" in html


def test_errored_runs_are_shown_and_excluded(tmp_path):
    html = report_for(
        tmp_path, [INJECTION, CANARY], ["B0"],
        [proposal(), RuntimeError("the provider fell over"), RuntimeError("again")],
    )
    assert "errored and are excluded" in html
    assert "the provider fell over" in html


def test_the_report_states_the_contamination_and_grounding_caveats(tmp_path):
    """Both are properties of the benchmark that a reader cannot infer from the table, and
    docs/07 says to state the first plainly."""
    html = report_for(tmp_path, [INJECTION], ["B0"], [proposal()])
    assert "Contamination" in html
    assert "two metrics wearing one name" in html
    assert "Counts, not rates" in html


def test_hostile_text_in_a_result_is_escaped(tmp_path):
    """Case ids, model names and error strings all reach the page."""
    from tribunal.eval import report_html

    result, _ = run_sweep(tmp_path, [INJECTION], ["B0"], [RuntimeError("<script>x</script>")])
    html = report_html.render(result, {INJECTION.id: INJECTION})
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
