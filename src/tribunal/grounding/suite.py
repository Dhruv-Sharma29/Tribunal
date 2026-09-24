"""Run the grounding suite and assemble a `GroundingReport`.

`GROUND` runs this once on the original file to establish the baseline every later comparison
is made against, then once per round on the patched file.

**Why the static tools are concurrent and the sandboxed ones are not.** The four static tools
are independent processes over the same file, so they run under `asyncio.gather`. The sandboxed
tools run *synchronously from the event loop*, on purpose, for two reasons that point the same
way:

* `sandbox.py` uses `preexec_fn`, which is unsafe in a multi-threaded parent. Moving the
  sandbox onto `asyncio.to_thread` to "keep the loop responsive" would make the parent
  multi-threaded at exactly the moment it forks. That is the subtle breakage flagged in
  docs/05-execution-sandbox.md, and it would show up as intermittent, unattributable child
  failures.
* Benchmarks must not run concurrently with anything, including each other. Two timed
  workloads sharing a CPU measure the scheduler, not the code.

**Tool failures are recorded, never swallowed.** A crashed tool lands in
`GroundingReport.tool_errors` and its dimension is short of evidence -- the same "unassessed is
not the same as clean" discipline the policy layer applies to critics.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from tribunal.config import Settings
from tribunal.contracts import GroundingFinding, GroundingReport, PerfMeasurement, TestResult
from tribunal.grounding.astgate_t import AstGateTool
from tribunal.grounding.bandit_t import BanditTool
from tribunal.grounding.base import GroundingTarget, ToolOutcome
from tribunal.grounding.perf_t import Benchmark, PerfTool
from tribunal.grounding.pytest_t import PytestTool
from tribunal.grounding.radon_t import RadonTool
from tribunal.grounding.ruff_t import RuffTool
from tribunal.sandbox import Sandbox, scratch_dir


@dataclass
class GroundingRun:
    """A report plus the per-tool detail the trace needs for `tool_run` events."""

    report: GroundingReport
    outcomes: list[ToolOutcome] = field(default_factory=list)

    @property
    def findings(self) -> list[GroundingFinding]:
        return self.report.findings


@dataclass
class GroundingSuite:
    settings: Settings

    def __post_init__(self) -> None:
        cfg = self.settings.grounding
        available = {
            "bandit": BanditTool(),
            "ruff": RuffTool(select=cfg.ruff_select, max_complexity=cfg.ruff_max_complexity),
            "radon": RadonTool(min_rank=cfg.radon_min_rank),
            "astgate": AstGateTool(),
        }
        unknown = set(cfg.static_tools) - set(available)
        if unknown:
            raise ValueError(f"unknown static grounding tools configured: {sorted(unknown)}")
        self.static_tools = [available[name] for name in cfg.static_tools]
        self.sandbox = Sandbox(self.settings.sandbox)

    async def run(
        self,
        source: str,
        *,
        target: str = "original",
        round: int | None = None,
        logical_name: str = "target.py",
        test_source: str | None = None,
        test_filename: str = "test_case.py",
        benchmarks: list[Benchmark] | None = None,
        original_source: str | None = None,
    ) -> GroundingRun:
        """Ground one version of the file.

        `logical_name` must be the input file's real basename. The user's test file imports
        the module by that name, and grounding-finding ids are derived from it -- so it stays
        constant across the baseline and every patched pass, while the scratch directory it
        lives in does not.

        `original_source` is required to produce a before/after `PerfMeasurement`; without it
        the benchmarks are reported `unmeasurable` rather than compared against nothing.
        """
        outcomes: list[ToolOutcome] = []
        findings: list[GroundingFinding] = []
        tool_errors: dict[str, str] = {}

        with scratch_dir() as workdir:
            staged = GroundingTarget.stage(source, workdir, logical_name)
            outcomes.extend(await self._run_static(staged))

            tests = self._run_tests(workdir, source, test_source, test_filename)
            measurements = self._run_benchmarks(
                source, original_source, benchmarks, logical_name
            )

        for outcome in outcomes:
            if outcome.error is not None:
                tool_errors[outcome.tool] = outcome.error
            findings.extend(outcome.findings)

        # `sandbox` is not a tool, but a refusal to execute belongs in `tool_errors` so the
        # trace shows why there are no test or benchmark results.
        if tests is not None and not tests.ran and tests.unavailable_reason:
            tool_errors["pytest"] = tests.unavailable_reason

        ran = [outcome.tool for outcome in outcomes]
        if tests is not None and tests.ran:
            ran.append("pytest")
        if any(m.verdict in ("faster", "slower", "inconclusive") for m in measurements):
            ran.append("perf")

        report = GroundingReport(
            target=target,  # type: ignore[arg-type]
            round=round,
            findings=findings,
            measurements=measurements,
            tests=tests,
            tool_errors=tool_errors,
            tools_run=sorted(set(ran)),
        )
        return GroundingRun(report=report, outcomes=outcomes)

    # -- static -----------------------------------------------------------------------

    async def _run_static(self, staged: GroundingTarget) -> list[ToolOutcome]:
        timeout = self.settings.grounding.tool_timeout_seconds
        results = await asyncio.gather(
            *(tool.run(staged, timeout=timeout) for tool in self.static_tools),
            return_exceptions=True,
        )
        outcomes: list[ToolOutcome] = []
        for tool, result in zip(self.static_tools, results, strict=True):
            if isinstance(result, BaseException):
                # `GroundingTool.run` already converts tool failures into outcomes, so this
                # branch means our own code raised. Record it and keep the other tools' work.
                outcomes.append(
                    ToolOutcome(
                        tool=tool.name,
                        argv=[],
                        exit_code=None,
                        error=f"{type(result).__name__}: {result}",
                    )
                )
            else:
                outcomes.append(result)
        return outcomes

    # -- sandboxed --------------------------------------------------------------------

    def _run_tests(
        self,
        workdir: Path,
        source: str,
        test_source: str | None,
        test_filename: str,
    ) -> TestResult | None:
        """None means "no test was supplied", which is different from "the test did not run"."""
        if test_source is None:
            return None
        pytest_tool = PytestTool(sandbox=self.sandbox)
        if not self.settings.sandbox.allow_exec:
            return pytest_tool.unavailable(
                "not run: a test was supplied but execution requires --allow-exec"
            )
        (workdir / test_filename).write_text(test_source, encoding="utf-8")
        return pytest_tool.run(workdir, test_filename, source)

    def _run_benchmarks(
        self,
        source: str,
        original_source: str | None,
        benchmarks: list[Benchmark] | None,
        logical_name: str,
    ) -> list[PerfMeasurement]:
        from tribunal.grounding.perf_t import unmeasurable

        if not benchmarks:
            return []
        if not self.settings.sandbox.allow_exec:
            return [unmeasurable(b.label, "execution requires --allow-exec") for b in benchmarks]
        if original_source is None:
            return [
                unmeasurable(b.label, "no baseline source to compare against")
                for b in benchmarks
            ]
        perf = PerfTool(sandbox=self.sandbox)
        return [perf.compare(original_source, source, b, logical_name) for b in benchmarks]
