"""The two grounding tools that execute: the pytest oracle and the timeit benchmark.

Both are gated by Layer 1, both run through `sandbox.py`, and both must degrade into a schema
value rather than an exception when they cannot run -- `TestResult.ran=False` and
`PerfMeasurement.verdict="unmeasurable"`. That is what lets the default (no `--allow-exec`)
configuration still produce a complete `GroundingReport`.
"""

from __future__ import annotations

import pytest

from tribunal.config import SandboxConfig
from tribunal.contracts import TestResult
from tribunal.grounding.perf_t import Benchmark, PerfTool, classify, unmeasurable
from tribunal.grounding.pytest_t import PytestTool, regression_node_ids
from tribunal.grounding.suite import GroundingSuite
from tribunal.sandbox import Sandbox, scratch_dir

TARGET = "def double(n):\n    return n * 2\n"
TARGET_BROKEN = "def double(n):\n    return n * 3\n"
TESTS = (
    "from target import double\n"
    "\n"
    "def test_two():\n"
    "    assert double(2) == 4\n"
    "\n"
    "def test_three():\n"
    "    assert double(3) == 6\n"
)

SLOW = (
    "def accumulate(rows):\n    out = []\n"
    "    for r in rows:\n        out = out + [r]\n    return out\n"
)
FAST = (
    "def accumulate(rows):\n    out = []\n"
    "    for r in rows:\n        out.append(r)\n    return out\n"
)
BENCH = Benchmark(
    label="timeit:accumulate:n=2000",
    expression="accumulate(_rows)",
    setup={"_rows": "list(range(2000))"},
    repeats=5,
)


def result(ids: list[str]) -> TestResult:
    return TestResult(
        ran=True,
        exit_code=1 if ids else 0,
        passed=2 - len(ids),
        failed=len(ids),
        errors=0,
        skipped=0,
        failed_node_ids=ids,
        duration_ms=10,
        timed_out=False,
        unavailable_reason=None,
    )


# -- The pytest oracle -----------------------------------------------------------------------


def test_the_oracle_runs_and_reports_node_ids(exec_sandbox):
    with scratch_dir({"target.py": TARGET, "test_target.py": TESTS}) as d:
        got = PytestTool(sandbox=exec_sandbox).run(d, "test_target.py", TARGET)
    assert got.ran
    assert got.all_passed
    assert (got.passed, got.failed, got.errors) == (2, 0, 0)


def test_the_oracle_reports_which_tests_failed(exec_sandbox):
    with scratch_dir({"target.py": TARGET_BROKEN, "test_target.py": TESTS}) as d:
        got = PytestTool(sandbox=exec_sandbox).run(d, "test_target.py", TARGET_BROKEN)
    assert got.ran
    assert not got.all_passed
    assert got.failed == 2
    # Node ids, not just counts: policy row 2 compares the *sets* of passing tests.
    assert sorted(got.failed_node_ids) == ["test_target.py::test_three", "test_target.py::test_two"]


def test_dropping_x_is_what_makes_the_full_failure_set_visible(exec_sandbox):
    """With `-x` the run would stop at `test_two` and `test_three` would never be reported --
    so a patch that breaks one test would be indistinguishable from one that breaks both."""
    with scratch_dir({"target.py": TARGET_BROKEN, "test_target.py": TESTS}) as d:
        got = PytestTool(sandbox=exec_sandbox).run(d, "test_target.py", TARGET_BROKEN)
    assert len(got.failed_node_ids) == 2


def test_the_oracle_refuses_a_target_that_imports_a_denied_module(exec_sandbox):
    hostile = "import socket\n\ndef double(n):\n    return n * 2\n"
    with scratch_dir({"target.py": hostile, "test_target.py": TESTS}) as d:
        got = PytestTool(sandbox=exec_sandbox).run(d, "test_target.py", hostile)
    assert not got.ran
    assert "refused" in got.unavailable_reason


def test_no_collected_tests_is_not_a_pass(exec_sandbox):
    """pytest exits 5 when it collects nothing. Reading that as "0 failed, all good" would
    turn a broken test file into a green correctness oracle."""
    with scratch_dir({"target.py": TARGET, "test_target.py": "# no tests here\n"}) as d:
        got = PytestTool(sandbox=exec_sandbox).run(d, "test_target.py", TARGET)
    assert not got.ran
    assert not got.all_passed
    assert "no tests were collected" in got.unavailable_reason


def test_a_module_level_side_effect_runs_inside_the_sandbox(exec_sandbox):
    """pytest collects by importing, so module-level code executes before any assertion.
    This is the threat the sandbox exists for; the point of the test is that it is contained."""
    hostile = "import os\nos.environ['MARKER'] = 'x'\n\ndef double(n):\n    return n * 2\n"
    with scratch_dir({"target.py": hostile, "test_target.py": TESTS}) as d:
        got = PytestTool(sandbox=exec_sandbox).run(d, "test_target.py", hostile)
    assert got.ran and got.all_passed
    assert "MARKER" not in __import__("os").environ  # nothing leaked into our process


def test_the_oracle_is_unavailable_without_allow_exec():
    tool = PytestTool(sandbox=Sandbox(SandboxConfig()))
    with scratch_dir({"target.py": TARGET, "test_target.py": TESTS}) as d:
        got = tool.run(d, "test_target.py", TARGET)
    assert not got.ran
    assert "--allow-exec" in got.unavailable_reason


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ([], ["t::b"], ["t::b"]),  # a real regression
        (["t::a"], ["t::a"], []),  # already failing: the bug we were asked to fix
        (["t::a"], ["t::a", "t::b"], ["t::b"]),  # fixed nothing, broke something
        (["t::a"], [], []),  # fixed it
        (["t::a"], ["t::b"], ["t::b"]),  # fixed a, broke b
    ],
)
def test_regression_detection_isolates_new_failures(before, after, expected):
    """Policy row 2 is "passed before and fails after", not "is failing now"."""
    assert regression_node_ids(result(before), result(after)) == expected


def test_regression_detection_needs_both_sides_to_have_run():
    never_ran = PytestTool(sandbox=Sandbox(SandboxConfig())).unavailable("not run")
    assert regression_node_ids(never_ran, result(["t::b"])) == []


# -- The timeit benchmark --------------------------------------------------------------------


def test_a_real_speedup_is_measured(exec_sandbox):
    got = PerfTool(sandbox=exec_sandbox).compare(SLOW, FAST, BENCH)
    assert got.verdict == "faster"
    assert got.is_citable
    assert got.before_ns > got.after_ns
    assert got.repeats == BENCH.repeats


def test_a_real_regression_is_measured(exec_sandbox):
    got = PerfTool(sandbox=exec_sandbox).compare(FAST, SLOW, BENCH)
    assert got.verdict == "slower"
    assert got.is_citable


def test_identical_code_is_inconclusive_not_faster(exec_sandbox):
    """Noise is not evidence. A Profiler allowed to call a 2% move a regression would
    manufacture a finding on every round."""
    got = PerfTool(sandbox=exec_sandbox).compare(FAST, FAST, BENCH)
    assert got.verdict == "inconclusive"
    assert not got.is_citable


@pytest.mark.parametrize("after_ns", [500_000, 2_000_000])
def test_identical_code_is_inconclusive_despite_timing_drift(monkeypatch, after_ns):
    timings = iter([(1_000_000, 100), (after_ns, 100)])
    calls = []

    def time_once(self, source, benchmark, logical_name):
        calls.append((source, benchmark, logical_name))
        return next(timings)

    monkeypatch.setattr(PerfTool, "time_once", time_once)
    got = PerfTool(sandbox=Sandbox(SandboxConfig())).compare(FAST, FAST, BENCH, "accumulator.py")
    assert calls == [(FAST, BENCH, "accumulator.py")] * 2
    assert (got.before_ns, got.after_ns, got.repeats) == (1_000_000, after_ns, BENCH.repeats)
    assert got.verdict == "inconclusive"
    assert not got.is_citable


@pytest.mark.parametrize(
    "timings", [["benchmark failed"], [(1_000_000, 100), "benchmark failed"]]
)
def test_identical_code_preserves_measurement_errors(monkeypatch, timings):
    results = iter(timings)
    monkeypatch.setattr(PerfTool, "time_once", lambda *args: next(results))
    got = PerfTool(sandbox=Sandbox(SandboxConfig())).compare(FAST, FAST, BENCH)
    assert got.verdict == "unmeasurable"
    assert "benchmark failed" in got.label
    assert not got.is_citable


def test_a_benchmark_that_cannot_run_is_unmeasurable(exec_sandbox):
    bogus = Benchmark(label="x", expression="nope(1)")
    got = PerfTool(sandbox=exec_sandbox).compare(SLOW, FAST, bogus)
    assert got.verdict == "unmeasurable"
    assert not got.is_citable
    assert "NameError" in got.label  # the reason is preserved for the trace


def test_an_injected_statement_list_never_reaches_the_runner(exec_sandbox):
    """The runner template is ours and the expression is validated as a single call, so this
    fails at the gate rather than inside the sandbox."""
    hostile = Benchmark(label="x", expression="accumulate(_rows); __import__('os').system('id')")
    got = PerfTool(sandbox=exec_sandbox).compare(SLOW, FAST, hostile)
    assert got.verdict == "unmeasurable"
    assert "does not parse" in got.label


def test_a_setup_statement_is_refused(exec_sandbox):
    """Setup accepts expressions only; there is no field through which a statement fits."""
    hostile = Benchmark(label="x", expression="accumulate(_rows)", setup={"_rows": "import os"})
    got = PerfTool(sandbox=exec_sandbox).compare(SLOW, FAST, hostile)
    assert got.verdict == "unmeasurable"
    assert "must be a single expression" in got.label


def test_a_setup_name_must_be_an_identifier(exec_sandbox):
    hostile = Benchmark(label="x", expression="accumulate(_rows)", setup={"a; b": "1"})
    assert PerfTool(sandbox=exec_sandbox).compare(SLOW, FAST, hostile).verdict == "unmeasurable"


@pytest.mark.parametrize("original", [SLOW, FAST])
def test_benchmarks_are_unmeasurable_without_allow_exec(original):
    got = PerfTool(sandbox=Sandbox(SandboxConfig())).compare(original, FAST, BENCH)
    assert got.verdict == "unmeasurable"
    assert "--allow-exec" in got.label


def test_the_noise_gate_uses_both_a_relative_and_an_absolute_floor():
    """Either gate alone is wrong: a purely relative one calls a 4% move on a noisy
    measurement real; a purely noise-based one calls a 1% move on a very stable one real."""
    stable = classify(BENCH, (1_000_000, 100), (1_020_000, 100))  # 2%, tiny noise
    assert stable.verdict == "inconclusive"
    noisy = classify(BENCH, (1_000_000, 400_000), (1_300_000, 400_000))  # 30%, huge noise
    assert noisy.verdict == "inconclusive"
    clear = classify(BENCH, (1_000_000, 1_000), (1_300_000, 1_000))
    assert clear.verdict == "slower"


def test_unmeasurable_carries_no_numbers():
    m = unmeasurable("timeit:x", "no benchmark declared")
    assert (m.before_ns, m.after_ns, m.repeats, m.stdev_ns) == (None, None, 0, None)


# -- End to end through the suite ------------------------------------------------------------


async def test_the_suite_produces_a_complete_report_with_execution(exec_settings):
    run = await GroundingSuite(exec_settings).run(
        SLOW,
        logical_name="target.py",
        test_source=(
            "from target import accumulate\n\n"
            "def test_it():\n    assert accumulate([1]) == [1]\n"
        ),
        test_filename="test_target.py",
        benchmarks=[BENCH],
        original_source=FAST,
    )
    assert run.report.tests.all_passed
    assert run.report.measurements[0].verdict == "slower"
    assert run.report.tool_errors == {}


async def test_the_default_configuration_still_produces_a_complete_report(settings):
    """Safe by default is not a degraded path: static grounding is unaffected, and the
    execution-dependent fields carry an explicit reason instead of a silence."""
    run = await GroundingSuite(settings).run(
        SLOW,
        logical_name="target.py",
        test_source="def test_it():\n    assert True\n",
        benchmarks=[BENCH],
        original_source=FAST,
    )
    assert run.report.findings  # static tools ran
    assert not run.report.tests.ran
    assert run.report.measurements[0].verdict == "unmeasurable"
    assert "--allow-exec" in run.report.tool_errors["pytest"]
