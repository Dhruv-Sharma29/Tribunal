"""The Red-team and Profiler critics.

Runs against a scripted fake provider, so zero API calls. The live path is in
`tests/test_llm_live.py`.

Most of the value here is in `validation.py`: `Issue.evidence` having `min_length=1` makes
"cite something" a validation error, but it does not make the citation *true*. These tests pin
the checks that close that gap, because a critic citing a finding id that does not exist is an
ungrounded critic wearing a costume.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_llm_client import FakeProvider
from tribunal.agents import CritiqueBundle, Profiler, RedTeam
from tribunal.agents.base import Agent, PrefixNotStable
from tribunal.agents.bundle import DIMENSION_TOOLS
from tribunal.agents.identity import canonicalise_issue_ids, finding_rules, slug
from tribunal.agents.prompts import PromptError, available_roles, load
from tribunal.agents.validation import EvidenceError, critic_metrics, validate_critique
from tribunal.config import AgentConfig, AgentsConfig, LLMConfig, Settings
from tribunal.contracts import (
    Critique,
    Dimension,
    Evidence,
    EvidenceKind,
    GroundingFinding,
    GroundingReport,
    Issue,
    Patch,
    PerfMeasurement,
    Severity,
    TestResult,
    UnaddressedIssue,
    finding_id,
)
from tribunal.llm.base import ProviderName
from tribunal.llm.client import LLMClient

SOURCE = '''import subprocess


def run_report(name):
    subprocess.run("generate " + name, shell=True)


def accumulate(rows):
    out = []
    for row in rows:
        out = out + [row]
    return out
'''

B602 = finding_id("bandit", "B602", "target.py", 5)
S602 = finding_id("ruff", "S602", "target.py", 5)
RADON = finding_id("radon", "CC-SUMMARY", "target.py", None)
RUF005 = finding_id("ruff", "RUF005", "target.py", 11)


def finding(id_, tool, rule, line, severity="HIGH", raw=None) -> GroundingFinding:
    return GroundingFinding(
        id=id_, tool=tool, rule=rule, file="target.py", line=line, end_line=line,
        message=f"{tool} {rule} at {line}", tool_severity=severity, raw=raw or {},
    )


def report(
    target="patched", round_=1, measurements=(), tests=None, errors=None, tools_run=None
) -> GroundingReport:
    return GroundingReport(
        target=target,
        round=round_ if target == "patched" else None,
        findings=[
            finding(B602, "bandit", "B602", 5, "HIGH"),
            finding(S602, "ruff", "S602", 5, "error"),
            finding(RUF005, "ruff", "RUF005", 11, "error"),
            finding(RADON, "radon", "CC-SUMMARY", None, None,
                    raw={"blocks": [{"name": "accumulate", "complexity": 3, "rank": "A"}]}),
        ],
        measurements=list(measurements),
        tests=tests,
        tool_errors=errors or {},
        tools_run=(
            tools_run
            if tools_run is not None
            # Mirrors what the suite records: the static tools always, plus pytest and perf
            # when they actually ran.
            else sorted(
                {"astgate", "bandit", "radon", "ruff"}
                | ({"pytest"} if tests else set())
                | ({"perf"} if measurements else set())
            )
        ),
    )


def bundle(dimension=Dimension.SECURITY, **kw) -> CritiqueBundle:
    defaults = dict(
        round=1,
        dimension=dimension,
        filename="target.py",
        patched_source=SOURCE,
        patched_report=report(),
        baseline_report=report(target="original"),
    )
    return CritiqueBundle(**{**defaults, **kw})


def evidence(kind=EvidenceKind.TOOL_FINDING, ref=B602) -> Evidence:
    return Evidence(kind=kind, ref=ref, excerpt="bandit B602 at 5")


def issue(**kw) -> Issue:
    defaults = dict(
        id="SEC-1", dimension=Dimension.SECURITY, severity=Severity.HIGH,
        title="shell=True on concatenated input",
        explanation="name reaches the shell unescaped",
        evidence=[evidence()], confidence=0.9, introduced_by_patch=True,
        suggested_direction="pass an argv list",
    )
    return Issue(**{**defaults, **kw})


def critique(**kw) -> Critique:
    defaults = dict(
        dimension=Dimension.SECURITY, round=1, verdict="block", issues=[issue()],
        tools_consulted=["bandit", "ruff"], summary="One command injection.",
    )
    return Critique(**{**defaults, **kw})


# -- prompts ---------------------------------------------------------------------------------


def test_both_critic_prompts_exist_and_are_versioned():
    assert set(available_roles()) >= {"redteam", "profiler"}
    for role, dimension in (("redteam", "security"), ("profiler", "performance")):
        prompt = load(role)
        assert prompt.stamp == f"{role}/v1"
        assert prompt.dimension == dimension
        assert prompt.changed != "unknown"
        assert prompt.note  # a version with no rationale is a version you cannot interpret


def test_a_prompt_declaring_the_wrong_role_is_rejected(tmp_path):
    """A mismatch here mis-stamps every trace event for that agent."""
    (tmp_path / "redteam.md").write_text("---\nrole: profiler\nversion: v9\n---\nbody\n")
    with pytest.raises(PromptError, match="declares role"):
        load("redteam", tmp_path)


def test_a_prompt_without_frontmatter_is_rejected(tmp_path):
    (tmp_path / "x.md").write_text("just a prompt body\n")
    with pytest.raises(PromptError, match="no frontmatter"):
        load("x", tmp_path)


def test_a_prompt_missing_a_version_is_rejected(tmp_path):
    (tmp_path / "x.md").write_text("---\nrole: x\n---\nbody\n")
    with pytest.raises(PromptError, match="missing `version`"):
        load("x", tmp_path)


def test_an_empty_prompt_body_is_rejected(tmp_path):
    (tmp_path / "x.md").write_text("---\nrole: x\nversion: v1\n---\n\n")
    with pytest.raises(PromptError, match="body is empty"):
        load("x", tmp_path)


def test_a_folded_note_is_joined(tmp_path):
    (tmp_path / "x.md").write_text(
        '---\nrole: x\nversion: v1\nnote: "first line\n  second line"\n---\nbody\n'
    )
    assert "second line" in load("x", tmp_path).note


def test_the_prompts_state_that_clean_is_expected():
    """docs/11-risks.md R2: the single most important anti-inflation clause."""
    for role in ("redteam", "profiler"):
        text = load(role).text
        assert "legitimate and expected verdict" in text
        assert "Inventing" in text


def test_the_prompts_forbid_writing_diffs():
    for role in ("redteam", "profiler"):
        assert "Never write a diff" in load(role).text


def test_the_profiler_prompt_forbids_uncited_numbers():
    """The measurement-honesty rule is the whole point of that agent's prompt."""
    text = load("profiler").text
    assert "MUST NOT cite" in text
    assert "Never state a percentage" in text


# -- the cacheable prefix --------------------------------------------------------------------


def test_both_critics_have_a_stable_system_prefix(settings):
    for cls in (RedTeam, Profiler):
        agent = cls(settings)
        assert agent.system_prompt() == load(agent.role).text


def test_a_round_number_in_the_prefix_is_rejected():
    """Anything per-round in the cached prefix invalidates the whole cache, and docs/06 warns
    you will not notice until the bill arrives."""
    with pytest.raises(PrefixNotStable, match="Round 2"):
        Agent._check_prefix_is_stable("rubric\nRound 2 is the hard one")


def test_a_finding_id_in_the_prefix_is_rejected():
    with pytest.raises(PrefixNotStable):
        Agent._check_prefix_is_stable("cite e5376668fa like this")


def test_an_english_word_from_the_hex_alphabet_is_not_flagged():
    """The heuristic requires a digit, so prose is not mistaken for an identifier."""
    Agent._check_prefix_is_stable("the facade of a deadbeef cafe is decafe")


def test_the_changed_date_in_frontmatter_does_not_trip_the_guard(settings):
    """`changed: 2026-09-16` is stable metadata, not a timestamp."""
    RedTeam(settings)  # would raise at construction


# -- the rendered bundle ---------------------------------------------------------------------


def test_the_source_is_line_numbered():
    """A `code_span` ref is only checkable if the model could see which line was which."""
    rendered = bundle().render()
    assert "   5|     subprocess.run" in rendered


def test_each_dimension_sees_only_its_own_findings():
    """Handing the Profiler a wall of bandit output invites security issues in the performance
    dimension, which policy then attributes to the wrong critic."""
    security = {f.rule for f in bundle(Dimension.SECURITY).relevant_findings()}
    performance = {f.rule for f in bundle(Dimension.PERFORMANCE).relevant_findings()}
    assert "B602" in security and "S602" in security
    assert "B602" not in performance
    assert "RUF005" in performance and "CC-SUMMARY" in performance
    assert "RUF005" not in security  # ruff's non-security rules stay out of the Red-team's view


def test_new_findings_are_distinguished_from_pre_existing_ones():
    """`introduced_by_patch` is only answerable if the critic can tell the difference."""
    baseline = GroundingReport(
        target="original", round=None,
        findings=[finding(B602, "bandit", "B602", 5)],
        measurements=[], tests=None, tool_errors={},
        tools_run=["astgate", "bandit", "radon", "ruff"],
    )
    rendered = bundle(baseline_report=baseline).render()
    assert f"id={B602}" in rendered
    assert "pre-existing" in rendered
    assert "NEW since baseline" in rendered  # S602 is not in the baseline


def test_the_coders_rationale_is_framed_as_a_claim_to_check():
    """docs/11-risks.md R1: a critic handed a confident justification as context goes
    sycophantic."""
    patch = Patch(
        round=1, diff="@@ -1 +1 @@", rationale="Fully validated upstream, this is safe.",
        addresses=["SEC-1"],
        deliberately_unaddressed=[UnaddressedIssue(issue_id="SEC-2", reason="false positive")],
    )
    rendered = bundle(patch=patch).render()
    assert "UNVERIFIED, treat as a claim to check" in rendered
    assert "A confident rationale is not evidence" in rendered
    assert "false positive" in rendered


def test_uncitable_measurements_are_shown_but_marked():
    """The critic must be able to say "performance could not be measured" without being able
    to mistake it for a result."""
    measurements = [
        PerfMeasurement(label="timeit:a", before_ns=1, after_ns=2, repeats=9, stdev_ns=0,
                        verdict="slower"),
        PerfMeasurement(label="timeit:b", before_ns=None, after_ns=None, repeats=0,
                        stdev_ns=None, verdict="unmeasurable"),
    ]
    rendered = bundle(
        Dimension.PERFORMANCE, patched_report=report(measurements=measurements)
    ).render()
    assert "CITABLE label=timeit:a" in rendered
    assert "NOT CITABLE label=timeit:b" in rendered
    assert "MUST NOT cite this" in rendered


def test_with_no_measurements_the_profiler_is_told_not_to_invent_one():
    rendered = bundle(Dimension.PERFORMANCE).render()
    assert "Do not state any timing or percentage" in rendered


def test_the_complexity_delta_is_rendered_for_the_profiler():
    """"complexity 7 -> 14 while fixing a low issue" is the one claim always well-founded."""
    before = GroundingReport(
        target="original", round=None,
        findings=[finding(RADON, "radon", "CC-SUMMARY", None, None,
                          raw={"blocks": [{"name": "accumulate", "complexity": 7, "rank": "B"}]})],
        measurements=[], tests=None, tool_errors={},
        tools_run=["astgate", "bandit", "radon", "ruff"],
    )
    after = report()  # complexity 3
    rendered = bundle(
        Dimension.PERFORMANCE, patched_report=after, baseline_report=before
    ).render()
    assert "accumulate: complexity 7 -> 3" in rendered


def test_a_tool_that_did_not_run_is_flagged_as_unassessed():
    """`unassessed` is not `clean`, and the critic must not infer absence of defects."""
    rendered = bundle(patched_report=report(errors={"bandit": "timed out after 60s"})).render()
    assert "UNASSESSED" in rendered
    assert "timed out" in rendered


def test_dismissed_issues_are_listed_so_they_are_not_reraised():
    rendered = bundle(
        consolidated_critique="Fix the shell call.", dismissed_issue_ids=("SEC-old",)
    ).render()
    assert "do NOT raise these again" in rendered
    assert "SEC-old" in rendered


# -- evidence validation: the teeth ----------------------------------------------------------


def test_a_valid_critique_passes():
    validate_critique(critique(), bundle())


def test_a_finding_id_that_does_not_exist_is_rejected():
    """The gap the schema cannot close: `ref` is a string, and any string satisfies it."""
    bad = critique(issues=[issue(evidence=[evidence(ref="bandit-B602")])])
    with pytest.raises(EvidenceError, match="not a grounding finding id"):
        validate_critique(bad, bundle())


def test_the_rejection_lists_the_ids_that_would_have_worked():
    """The message is fed back verbatim as the repair prompt, so it has to be actionable."""
    bad = critique(issues=[issue(evidence=[evidence(ref="nope")])])
    with pytest.raises(EvidenceError) as caught:
        validate_critique(bad, bundle())
    assert B602 in str(caught.value)


def test_citing_another_dimensions_tool_is_rejected():
    bad = critique(issues=[issue(evidence=[evidence(ref=RADON)])])
    with pytest.raises(EvidenceError, match="not a security tool"):
        validate_critique(bad, bundle())


def test_an_inconclusive_measurement_cannot_be_cited():
    """docs/03-agents.md § 3.3 requires this to be enforced, and the schema alone cannot: it
    needs the report."""
    inconclusive = PerfMeasurement(
        label="timeit:a", before_ns=100, after_ns=104, repeats=9, stdev_ns=30,
        verdict="inconclusive",
    )
    perf_bundle = bundle(Dimension.PERFORMANCE, patched_report=report(measurements=[inconclusive]))
    bad = critique(
        dimension=Dimension.PERFORMANCE,
        issues=[issue(dimension=Dimension.PERFORMANCE,
                      evidence=[evidence(kind=EvidenceKind.MEASUREMENT, ref="timeit:a")])],
    )
    with pytest.raises(EvidenceError, match="MUST NOT be cited"):
        validate_critique(bad, perf_bundle)


def test_an_unmeasurable_measurement_cannot_be_cited():
    unmeasurable = PerfMeasurement(
        label="timeit:a", before_ns=None, after_ns=None, repeats=0, stdev_ns=None,
        verdict="unmeasurable",
    )
    perf_bundle = bundle(Dimension.PERFORMANCE, patched_report=report(measurements=[unmeasurable]))
    bad = critique(
        dimension=Dimension.PERFORMANCE,
        issues=[issue(dimension=Dimension.PERFORMANCE,
                      evidence=[evidence(kind=EvidenceKind.MEASUREMENT, ref="timeit:a")])],
    )
    with pytest.raises(EvidenceError, match="MUST NOT be cited"):
        validate_critique(bad, perf_bundle)


def test_a_citable_measurement_is_accepted():
    good = PerfMeasurement(
        label="timeit:a", before_ns=100, after_ns=900, repeats=9, stdev_ns=2, verdict="slower",
    )
    perf_bundle = bundle(Dimension.PERFORMANCE, patched_report=report(measurements=[good]))
    ok = critique(
        dimension=Dimension.PERFORMANCE,
        issues=[issue(dimension=Dimension.PERFORMANCE,
                      evidence=[evidence(kind=EvidenceKind.MEASUREMENT, ref="timeit:a")])],
        tools_consulted=["perf"],
    )
    validate_critique(ok, perf_bundle)


def test_a_code_span_outside_the_file_is_rejected():
    bad = critique(
        issues=[issue(evidence=[evidence(kind=EvidenceKind.CODE_SPAN, ref="target.py:L900-L910")])]
    )
    with pytest.raises(EvidenceError, match="outside target.py"):
        validate_critique(bad, bundle())


def test_a_code_span_naming_another_file_is_rejected():
    """Single-file review: a span in another file cannot be checked."""
    bad = critique(
        issues=[issue(evidence=[evidence(kind=EvidenceKind.CODE_SPAN, ref="other.py:L1-L2")])]
    )
    with pytest.raises(EvidenceError, match="single-file"):
        validate_critique(bad, bundle())


def test_a_valid_code_span_is_accepted_and_counts_as_novel():
    """Novel issues are the ones that prove the critic is not a linter wrapper."""
    ok = critique(
        issues=[issue(evidence=[evidence(kind=EvidenceKind.CODE_SPAN, ref="target.py:L8-L12")])]
    )
    validate_critique(ok, bundle())
    assert critic_metrics(ok, bundle())["novel_issue_rate"] == 1.0


def test_a_test_failure_ref_must_be_a_failing_node():
    tests = TestResult(
        ran=True, exit_code=1, passed=1, failed=1, errors=0, skipped=0,
        failed_node_ids=["test_t.py::test_a"], duration_ms=5, timed_out=False,
        unavailable_reason=None,
    )
    ok = critique(
        issues=[issue(evidence=[evidence(kind=EvidenceKind.TEST_FAILURE,
                                         ref="test_t.py::test_a")])],
        tools_consulted=["pytest"],
    )
    validate_critique(ok, bundle(patched_report=report(tests=tests)))

    bad = critique(
        issues=[issue(evidence=[evidence(kind=EvidenceKind.TEST_FAILURE,
                                         ref="test_t.py::test_passing")])],
        tools_consulted=["pytest"],
    )
    with pytest.raises(EvidenceError, match="not a failing test"):
        validate_critique(bad, bundle(patched_report=report(tests=tests)))


def test_reasoning_evidence_is_always_accepted():
    """Legal and penalised, not forbidden: a critic must be able to raise something no tool
    can see."""
    ok = critique(
        issues=[issue(evidence=[evidence(kind=EvidenceKind.REASONING, ref="no tool sees this")])]
    )
    validate_critique(ok, bundle())


def test_tools_consulted_must_not_contain_finding_ids():
    """Observed on the first live run: schema-valid and meaningless."""
    bad = critique(tools_consulted=[B602, S602])
    with pytest.raises(EvidenceError, match="is a finding id, not a tool name"):
        validate_critique(bad, bundle())


def test_tools_consulted_must_be_tools_for_this_dimension():
    bad = critique(tools_consulted=["radon"])
    with pytest.raises(EvidenceError, match="not a grounding tool for the security"):
        validate_critique(bad, bundle())


def test_tools_consulted_must_have_actually_run():
    bad = critique(tools_consulted=["pytest"])
    with pytest.raises(EvidenceError, match="produced no output this round"):
        validate_critique(bad, bundle())


def test_the_wrong_dimension_is_rejected():
    bad = critique(dimension=Dimension.PERFORMANCE)
    with pytest.raises(EvidenceError, match="dimension must be"):
        validate_critique(bad, bundle(Dimension.SECURITY))


def test_the_wrong_round_is_rejected():
    with pytest.raises(EvidenceError, match="round must be 3"):
        validate_critique(critique(), bundle(round=3))


def test_a_dismissed_issue_cannot_be_reraised():
    """The Arbiter's dismissals are permanent; re-raising one burns a round.

    Canonicalisation runs first here, mirroring `Critic.post_validate`: the dismissed list
    holds canonical ids, so comparing against a model-supplied id would never match.
    """
    c = critique()
    mapping = canonicalise_issue_ids(c, finding_rules(report()))
    canonical = mapping[0][1]
    with pytest.raises(EvidenceError, match="already dismissed"):
        validate_critique(c, bundle(dismissed_issue_ids=(canonical,)))


def test_a_high_issue_with_a_non_blocking_verdict_is_rejected():
    """The mirror of the guard docs/02 states. The first live Profiler run did exactly this."""
    bad = critique(verdict="concerns")
    with pytest.raises(EvidenceError, match='issues .* are severity "high"'):
        validate_critique(bad, bundle())


def test_a_suggested_direction_containing_a_diff_is_rejected():
    """Letting critics write patches collapses the roles into three competing diffs."""
    bad = critique(issues=[issue(suggested_direction="@@ -5,1 +5,1 @@\n-bad\n+good")])
    with pytest.raises(EvidenceError, match="contains a diff or code block"):
        validate_critique(bad, bundle())


def test_a_multi_line_suggested_direction_is_rejected():
    bad = critique(issues=[issue(suggested_direction="do this\nthen this\nthen that\nand that")])
    with pytest.raises(EvidenceError, match="must be one sentence"):
        validate_critique(bad, bundle())


def test_all_problems_are_reported_at_once():
    """The repair budget is one, so reporting failures one at a time would waste it."""
    bad = critique(
        verdict="concerns",
        tools_consulted=["radon"],
        issues=[issue(evidence=[evidence(ref="nonexistent")])],
    )
    with pytest.raises(EvidenceError) as caught:
        validate_critique(bad, bundle())
    message = str(caught.value)
    assert "not a grounding finding id" in message
    assert "not a grounding tool" in message
    assert "severity" in message


# -- identity ----------------------------------------------------------------------------------


def test_model_supplied_ids_are_replaced_with_canonical_ones():
    """Observed live: the Red-team reused the finding id, the Profiler emitted "1" and "2"."""
    c = critique(
        issues=[
            issue(id=B602, title="sql injection"),
            issue(id="1", title="a different concern about the same finding"),
        ]
    )
    mapping = canonicalise_issue_ids(c, finding_rules(report()))
    assert [old for old, _ in mapping] == [B602, "1"]
    assert all(new.startswith("SEC-") for _, new in mapping)
    assert len({new for _, new in mapping}) == 2  # no collision


def test_duplicate_model_ids_do_not_collapse_a_rename():
    """The reason the mapping is a list of pairs and not a dict. A model emitting "1" twice
    must still yield two recorded renames."""
    c = critique(
        issues=[
            issue(id="1", title="first"),
            issue(id="1", title="second", evidence=[evidence(ref=S602)]),
        ]
    )
    mapping = canonicalise_issue_ids(c, finding_rules(report()))
    assert len(mapping) == 2
    assert [old for old, _ in mapping] == ["1", "1"]
    assert len({new for _, new in mapping}) == 2


def test_ids_are_stable_for_the_same_content():
    """Conflict detector 1 recognises a trade-off by seeing the same id return."""
    first = canonicalise_issue_ids(critique(), finding_rules(report()))
    second = canonicalise_issue_ids(critique(), finding_rules(report()))
    assert first == second
    assert first[0][1].startswith("SEC-")


def test_ids_separate_issues_with_different_anchors():
    c = critique(
        issues=[
            issue(evidence=[evidence(ref=B602)]),
            issue(evidence=[evidence(ref=S602)], title="the ruff view"),
        ]
    )
    ids = [new for _, new in canonicalise_issue_ids(c, finding_rules(report()))]
    assert len(set(ids)) == 2


def test_a_tool_finding_anchor_is_preferred_over_a_code_span():
    """A finding's rule code survives line numbers shifting as later rounds patch the file."""
    with_span_first = issue(
        evidence=[
            evidence(kind=EvidenceKind.CODE_SPAN, ref="target.py:L5-L5"),
            evidence(ref=B602),
        ]
    )
    only_finding = issue(evidence=[evidence(ref=B602)])
    a = canonicalise_issue_ids(critique(issues=[with_span_first]), finding_rules(report()))
    b = canonicalise_issue_ids(critique(issues=[only_finding]), finding_rules(report()))
    assert [new for _, new in a] == [new for _, new in b]


def test_slug_is_stable_and_bounded():
    assert slug("SQL Injection via %-formatting!") == "sql-injection-via-formatting"
    assert len(slug("x" * 200)) <= 60


# -- metrics -----------------------------------------------------------------------------------


def test_rerating_rate_is_computed_against_the_tools_own_severity():
    """docs/11-risks.md R3: near 0% means a linter wrapper with extra steps."""
    relayed = critique(issues=[issue(severity=Severity.HIGH)])  # bandit also said HIGH
    assert critic_metrics(relayed, bundle())["rerating_rate"] == 0.0

    rerated = critique(verdict="concerns", issues=[issue(severity=Severity.LOW)])
    assert critic_metrics(rerated, bundle())["rerating_rate"] == 1.0


def test_novel_issue_rate_counts_issues_no_tool_flagged():
    c = critique(
        issues=[
            issue(evidence=[evidence(ref=B602)]),
            issue(title="unflagged",
                  evidence=[evidence(kind=EvidenceKind.CODE_SPAN, ref="target.py:L8-L12")]),
        ]
    )
    assert critic_metrics(c, bundle())["novel_issue_rate"] == 0.5


def test_metrics_on_a_clean_critique_do_not_divide_by_zero():
    clean = critique(verdict="clean", issues=[])
    metrics = critic_metrics(clean, bundle())
    assert metrics["issues"] == 0
    assert metrics["grounded_fraction"] == 1.0
    assert metrics["pressure"] == 0.0
    assert metrics["highest_severity"] is None


def test_pressure_matches_the_policy_formula():
    c = critique(issues=[issue(severity=Severity.HIGH, confidence=0.9)])
    assert critic_metrics(c, bundle())["pressure"] == pytest.approx(16 * 0.9)


# -- end to end through a fake provider --------------------------------------------------------


def critic_client(tmp_path: Path, script: list[Any]) -> tuple[LLMClient, FakeProvider]:
    settings = Settings(
        llm=LLMConfig(cassette_dir=tmp_path),
        agents=AgentsConfig(
            redteam=AgentConfig(model="fake-1", provider=ProviderName.NIM, max_tokens=4000),
            profiler=AgentConfig(model="fake-1", provider=ProviderName.NIM, max_tokens=4000),
        ),
    )
    fake = FakeProvider(script=script)
    client = LLMClient(settings)
    client.providers[ProviderName.NIM] = fake
    return client, fake, settings


def payload(**kw) -> dict:
    base = json.loads(critique().model_dump_json())
    base.update(kw)
    return base


async def test_the_redteam_runs_end_to_end(tmp_path):
    client, fake, settings = critic_client(tmp_path, [payload()])
    agent = RedTeam(settings, client)
    run = await agent.run(bundle())
    assert run.role == "redteam"
    assert run.prompt_version == "redteam/v1"
    assert run.value.verdict == "block"
    assert run.metrics["grounded"] == 1
    assert run.outcome.repair_retries == 0
    # The model's id was replaced by a canonical one.
    assert run.value.issues[0].id.startswith("SEC-")


async def test_the_profiler_runs_end_to_end(tmp_path):
    perf = payload(
        dimension="performance",
        verdict="concerns",
        issues=[
            json.loads(
                issue(
                    severity=Severity.MEDIUM,
                    dimension=Dimension.PERFORMANCE,
                    title="quadratic accumulation",
                    evidence=[evidence(kind=EvidenceKind.CODE_SPAN, ref="target.py:L8-L12")],
                ).model_dump_json()
            )
        ],
        tools_consulted=["radon"],
    )
    client, fake, settings = critic_client(tmp_path, [perf])
    run = await Profiler(settings, client).run(bundle(Dimension.PERFORMANCE))
    assert run.value.dimension is Dimension.PERFORMANCE
    assert run.value.issues[0].id.startswith("PERF-")
    assert run.metrics["novel_issue_rate"] == 1.0


async def test_an_ungrounded_citation_costs_one_repair_retry(tmp_path):
    """The evidence check shares the schema check's retry budget and counter."""
    broken = payload(
        issues=[json.loads(issue(evidence=[evidence(ref="invented-id")]).model_dump_json())]
    )
    client, fake, settings = critic_client(tmp_path, [broken, payload()])
    run = await RedTeam(settings, client).run(bundle())
    assert run.outcome.repair_retries == 1
    assert "not a grounding finding id" in fake.seen[1].user


async def test_a_critic_that_cannot_ground_its_claims_fails_the_dimension(tmp_path):
    """Better an errored critic -- which policy reads as `unassessed` -- than an accepted
    critique nobody can check."""
    from tribunal.llm.client import SchemaRepairFailed

    broken = payload(
        issues=[json.loads(issue(evidence=[evidence(ref="invented")]).model_dump_json())]
    )
    client, fake, settings = critic_client(tmp_path, [broken, broken])
    with pytest.raises(SchemaRepairFailed):
        await RedTeam(settings, client).run(bundle())


async def test_a_critic_refuses_a_bundle_for_the_other_dimension(tmp_path):
    """A mis-routed bundle is an orchestrator bug. Fail before spending a call on it."""
    client, fake, settings = critic_client(tmp_path, [payload()])
    with pytest.raises(ValueError, match="assesses security"):
        await RedTeam(settings, client).run(bundle(Dimension.PERFORMANCE))
    assert fake.seen == []  # no API call was made


async def test_the_trace_payload_carries_the_prompt_health_signals(tmp_path):
    """docs/09-roadmap.md makes the repair-retry rate the prompt-health signal, and it cannot
    be recovered after the fact."""
    client, fake, settings = critic_client(tmp_path, [payload()])
    run = await RedTeam(settings, client).run(bundle())
    trace = run.trace_payload()
    assert trace["prompt_version"] == "redteam/v1"
    assert trace["provider"] == "nim"
    assert trace["structure"] == "native_json"
    assert trace["structure_guaranteed"] is False
    assert trace["parse_retries"] == 0
    assert "dropped_constraints" in trace
    assert trace["metrics"]["rerating_rate"] == 0.0


async def test_the_two_critics_never_see_each_others_output(tmp_path):
    """docs/01-architecture.md Rule 2. The bundle has no field for it, which is the point."""
    fields = set(CritiqueBundle.__dataclass_fields__)
    assert "other_critique" not in fields
    assert "redteam_critique" not in fields
    # The only channel is the Arbiter's synthesis from the *previous* round.
    assert "consolidated_critique" in fields


def test_every_dimension_has_a_declared_tool_allowlist():
    assert set(DIMENSION_TOOLS) >= {Dimension.SECURITY, Dimension.PERFORMANCE}
    assert DIMENSION_TOOLS[Dimension.SECURITY] & {"bandit", "ruff"}
    assert "radon" in DIMENSION_TOOLS[Dimension.PERFORMANCE]


def test_uncited_severe_findings_are_counted():
    """docs/11-risks.md R1. Observed live: the Red-team's summary named four high-severity
    defects on the seeded-bad fixture and emitted one issue. The summary is prose and not
    checkable; coverage of the tool's own HIGH findings is."""
    only_one = critique(issues=[issue(evidence=[evidence(ref=B602)])])
    metrics = critic_metrics(only_one, bundle())
    assert metrics["severe_findings"] == 1  # only bandit's B602 maps to HIGH
    assert metrics["severe_findings_cited"] == 1
    assert metrics["uncited_severe_findings"] == []

    ignored = critique(issues=[issue(evidence=[evidence(ref=S602)])])
    assert critic_metrics(ignored, bundle())["uncited_severe_findings"] == [B602]


def test_a_clean_critique_that_ignores_a_severe_finding_is_visible():
    """`clean` is legitimate, but silently dropping a HIGH finding should show up in the
    yield metric rather than nowhere."""
    clean = critique(verdict="clean", issues=[])
    metrics = critic_metrics(clean, bundle())
    assert metrics["uncited_severe_findings"] == [B602]
