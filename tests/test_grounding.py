"""Golden-file tests: fixture .py -> expected normalised findings, per tool.

**What the golden files record, and what they deliberately do not.** Each golden file holds
`(tool, rule, file, line, end_line, tool_severity)` per finding. Messages and `raw` payloads
are excluded: they churn with every tool release, and a suite that breaks on a bandit patch
release teaches you to run `--update-golden` without reading the diff, which is worse than
having no golden test. Rule ids and line numbers are the parts a critic's `Evidence` actually
cites, so those are what is pinned.

Regenerate deliberately with `TRIBUNAL_UPDATE_GOLDEN=1 pytest tests/test_grounding.py`, and
read the diff.
"""

from __future__ import annotations

import json
import os

import pytest

from tests.conftest import GOLDEN, fixture_source
from tribunal.config import GroundingConfig, Settings
from tribunal.contracts import GroundingFinding
from tribunal.grounding.radon_t import SUMMARY_RULE
from tribunal.grounding.suite import GroundingSuite

FIXTURE_NAMES = ["vulnerable.py", "clean.py", "complex.py"]


def signature(finding: GroundingFinding) -> list:
    return [
        finding.tool,
        finding.rule,
        finding.file,
        finding.line,
        finding.end_line,
        finding.tool_severity,
    ]


async def ground(name: str, settings: Settings) -> list[GroundingFinding]:
    run = await GroundingSuite(settings).run(fixture_source(name), logical_name=name)
    assert run.report.tool_errors == {}, f"a grounding tool failed: {run.report.tool_errors}"
    # Every configured tool must be recorded as having run, whether or not it found anything.
    assert set(run.report.tools_run) >= set(settings.grounding.static_tools)
    return sorted(run.report.findings, key=lambda f: (f.tool, f.rule, f.line or 0))


@pytest.mark.parametrize("name", FIXTURE_NAMES)
async def test_findings_match_golden(name, settings):
    findings = await ground(name, settings)
    actual = [signature(f) for f in findings]
    path = GOLDEN / f"{name}.json"

    if os.environ.get("TRIBUNAL_UPDATE_GOLDEN"):
        GOLDEN.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(actual, indent=2) + "\n", encoding="utf-8")
        pytest.skip(f"golden file rewritten: {path.name}")

    expected = json.loads(path.read_text(encoding="utf-8"))
    assert actual == expected


@pytest.mark.parametrize("name", FIXTURE_NAMES)
async def test_finding_ids_are_stable_across_runs(name, settings):
    """Identity must not depend on the scratch directory.

    Tools run against a copy in a throwaway directory, so if the on-disk path reached
    `finding_id`, every id would change between the baseline and the patched pass -- silently
    breaking issue dismissal, the oscillation guard, and conflict detector 1.
    """
    first = [f.id for f in await ground(name, settings)]
    second = [f.id for f in await ground(name, settings)]
    assert first == second
    assert len(set(first)) == len(first), "finding ids collided within one report"


@pytest.mark.parametrize("name", FIXTURE_NAMES)
async def test_no_finding_leaks_an_absolute_path(name, settings):
    """Every finding's file is the logical name, whatever the tool reported."""
    for finding in await ground(name, settings):
        assert finding.file == name
        assert "/tmp" not in json.dumps(finding.raw)


async def test_the_canary_fixture_is_clean(settings):
    """A grounded critic cannot invent an issue on this file, because there is nothing to cite.

    The only finding permitted is radon's complexity summary, which is a measurement and not a
    defect -- it carries no `tool_severity` precisely so it cannot be read as one.
    """
    findings = await ground("clean.py", settings)
    defects = [f for f in findings if f.rule != SUMMARY_RULE]
    assert defects == [], f"the canary fixture is not clean: {[f.rule for f in defects]}"
    assert [f.tool_severity for f in findings] == [None]


async def test_vulnerable_fixture_is_seen_by_every_security_source(settings):
    """The three security sources must agree that shell=True is there.

    Agreement across independent tools is what makes the Red-team's citation strong; if only
    one tool sees it, the critic is relying on one vendor's rule set.
    """
    findings = await ground("vulnerable.py", settings)
    shell_true_lines = {f.line for f in findings if f.rule in ("B602", "S602", "shell-true")}
    assert len(shell_true_lines) == 1, "the three tools disagree about where shell=True is"
    tools = {f.tool for f in findings if f.rule in ("B602", "S602", "shell-true")}
    assert tools == {"bandit", "ruff", "astgate"}


async def test_tool_severity_is_never_mapped_onto_our_severity(settings):
    """bandit's HIGH stays bandit's HIGH.

    Mapping it onto our `Severity` would make the critic redundant -- the point is that the
    agent re-rates in context. Keeping both is what lets the eval measure the divergence, which
    is the answer to "isn't this just a linter wrapper?".
    """
    findings = await ground("vulnerable.py", settings)
    ratings = {f.tool: {f2.tool_severity for f2 in findings if f2.tool == f.tool} for f in findings}
    assert ratings["bandit"] <= {"LOW", "MEDIUM", "HIGH"}
    assert ratings["ruff"] <= {"error", "warning", None}
    assert ratings["astgate"] <= {"advisory", "blocking"}


async def test_radon_summary_is_present_even_when_nothing_is_complex(settings):
    """The Profiler's characteristic citation is a delta ("complexity 7 -> 12").

    Rank B is below the reporting threshold, so without the always-emitted summary the
    "before" side of that comparison would not exist in the baseline report.
    """
    findings = await ground("clean.py", settings)
    summary = next(f for f in findings if f.rule == SUMMARY_RULE)
    assert summary.line is None
    assert summary.raw["blocks"], "the summary carries no per-function table"
    assert all("complexity" in row for row in summary.raw["blocks"])


async def test_radon_min_rank_filters_per_function_findings_only(settings):
    lenient = Settings(grounding=GroundingConfig(radon_min_rank="A"))
    strict = Settings(grounding=GroundingConfig(radon_min_rank="F"))
    many = [f for f in await ground("complex.py", lenient) if f.tool == "radon"]
    few = [f for f in await ground("complex.py", strict) if f.tool == "radon"]
    assert len(many) > len(few)
    # Whatever the threshold, the summary survives.
    assert any(f.rule == SUMMARY_RULE for f in many)
    assert [f.rule for f in few] == [SUMMARY_RULE]


async def test_a_missing_tool_is_an_error_not_a_clean_report(settings, monkeypatch):
    """`unassessed` is not `clean` -- the same discipline the policy layer applies to critics.

    A tool that cannot run must leave a visible hole in `tool_errors`, because a critic with no
    evidence for its dimension is not a critic that found nothing.
    """
    from tribunal.grounding.bandit_t import BanditTool

    monkeypatch.setattr(
        BanditTool, "argv", lambda self, target: ["/nonexistent/bandit", str(target.path.name)]
    )
    run = await GroundingSuite(settings).run(fixture_source("vulnerable.py"), logical_name="v.py")
    assert "bandit" in run.report.tool_errors
    assert "not installed" in run.report.tool_errors["bandit"]
    # The other three tools' work survives one tool's failure.
    assert {f.tool for f in run.report.findings} == {"ruff", "radon", "astgate"}


async def test_a_tool_timeout_is_recorded_not_raised(monkeypatch):
    """A hung tool must not hang the round, and must not look like a clean result."""
    from tribunal.grounding.ruff_t import RuffTool

    monkeypatch.setattr(RuffTool, "argv", lambda self, target: ["/bin/sleep", "20"])
    impatient = Settings(grounding=GroundingConfig(tool_timeout_seconds=1))
    run = await GroundingSuite(impatient).run(
        fixture_source("vulnerable.py"), logical_name="v.py"
    )
    assert "timed out after 1s" in run.report.tool_errors["ruff"]
    # The concurrently-running tools still delivered their work.
    assert {f.tool for f in run.report.findings} == {"bandit", "radon", "astgate"}


async def test_report_round_and_target_must_agree(settings):
    """`round=None` means the baseline. A patched report without a round is a bug we want to
    see, not coerce."""
    from pydantic import ValidationError

    from tribunal.contracts import GroundingReport

    with pytest.raises(ValidationError, match="round must be None"):
        GroundingReport(
            target="patched", round=None, findings=[], measurements=[], tests=None,
            tool_errors={}, tools_run=[],
        )
    with pytest.raises(ValidationError, match="round must be None"):
        GroundingReport(
            target="original", round=2, findings=[], measurements=[], tests=None,
            tool_errors={}, tools_run=[],
        )
