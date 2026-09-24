"""pytest -> TestResult, through the sandbox.

This is the correctness oracle policy row 2 turns on, and it is the most dangerous tool in the
suite: pytest *collects* by importing, so module-level code in the target or the test file runs
before a single assertion is evaluated. Nothing here executes without `--allow-exec`, and
everything runs inside `sandbox.Sandbox`.

Two deviations from the flag list in docs/05-execution-sandbox.md, both deliberate:

* **No `-x`.** Policy row 2 is "the user's test *passed before and fails after*", which is a
  comparison of the *sets* of passing node ids across two runs. `-x` truncates that set at the
  first failure, so a patch that breaks test B would be indistinguishable from one that breaks
  test A and B, and a pre-existing failure early in the file would mask everything behind it --
  the exact distinction row 2 depends on.
* **`--junit-xml` instead of parsing `-q` stdout.** Terse stdout gives counts; JUnit XML gives
  node ids, which is what the set comparison and `Evidence(kind=TEST_FAILURE)` need. It is
  built into pytest, so it adds no plugin dependency to the sandbox image.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from tribunal.contracts import SandboxResult, TestResult
from tribunal.sandbox import ExecutionNotPermitted, Sandbox

TOOL = "pytest"
REPORT_NAME = "junit.xml"

#: pytest's documented exit codes. 0 = all passed, 1 = tests failed; everything else means the
#: run did not happen, which is `ran=False`, not `failed`.
EXIT_OK = 0
EXIT_TESTS_FAILED = 1
EXIT_MEANINGS = {
    2: "pytest was interrupted",
    3: "internal pytest error",
    4: "pytest usage error (bad flags)",
    5: "no tests were collected",
}


@dataclass(frozen=True)
class PytestTool:
    sandbox: Sandbox
    per_test_timeout: int = 20

    def unavailable(self, reason: str) -> TestResult:
        return TestResult(
            ran=False,
            exit_code=None,
            passed=0,
            failed=0,
            errors=0,
            skipped=0,
            failed_node_ids=[],
            duration_ms=None,
            timed_out=False,
            unavailable_reason=reason,
        )

    def argv(self, test_filename: str) -> list[str]:
        return [
            self.sandbox.python_for_sandbox(),
            "-m",
            "pytest",
            test_filename,
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",  # do not write .pytest_cache into the scratch dir
            f"--junit-xml={REPORT_NAME}",
            f"--timeout={self.per_test_timeout}",
        ]

    def run(self, workdir: Path, test_filename: str, target_source: str) -> TestResult:
        """Run the user's tests against whatever is staged in `workdir`.

        `target_source` is gated by `astgate` first: if it imports a denied module, we refuse
        to execute rather than run it and hope.
        """
        try:
            result = self.sandbox.run(
                self.argv(test_filename),
                workdir,
                source_to_gate=target_source,
            )
        except ExecutionNotPermitted as exc:
            return self.unavailable(f"not run: {exc}")
        if result.refused_reason is not None:
            return self.unavailable(result.refused_reason)
        return self.parse(result, workdir)

    def parse(self, result: SandboxResult, workdir: Path) -> TestResult:
        if result.timed_out:
            # A timeout is a real outcome, not an absence of one: the FSM continues and policy
            # sees a test suite that did not complete.
            return TestResult(
                ran=True,
                exit_code=None,
                passed=0,
                failed=0,
                errors=0,
                skipped=0,
                failed_node_ids=[],
                duration_ms=result.duration_ms,
                timed_out=True,
                unavailable_reason=None,
            )
        if result.exit_code not in (EXIT_OK, EXIT_TESTS_FAILED):
            meaning = EXIT_MEANINGS.get(result.exit_code or -1, f"exit {result.exit_code}")
            tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
            return self.unavailable(f"{meaning}: {' / '.join(tail)[:300]}")

        report = workdir / REPORT_NAME
        if not report.is_file():
            return self.unavailable("pytest produced no junit report")
        counts, failed_ids = _parse_junit(report.read_text(encoding="utf-8"))
        return TestResult(
            ran=True,
            exit_code=result.exit_code,
            passed=counts["passed"],
            failed=counts["failed"],
            errors=counts["errors"],
            skipped=counts["skipped"],
            failed_node_ids=failed_ids,
            duration_ms=result.duration_ms,
            timed_out=False,
            unavailable_reason=None,
        )


def _parse_junit(xml: str) -> tuple[dict[str, int], list[str]]:
    root = ET.fromstring(xml)  # noqa: S314 - produced by pytest in our own scratch dir
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    failed_ids: list[str] = []
    for suite in suites:
        for case in suite.iter("testcase"):
            node_id = _node_id(case)
            if case.find("failure") is not None:
                counts["failed"] += 1
                failed_ids.append(node_id)
            elif case.find("error") is not None:
                counts["errors"] += 1
                failed_ids.append(node_id)
            elif case.find("skipped") is not None:
                counts["skipped"] += 1
            else:
                counts["passed"] += 1
    return counts, failed_ids


def _node_id(case: ET.Element) -> str:
    """Rebuild the pytest node id JUnit XML splits across attributes.

    JUnit's `classname` is dotted (`test_fetch.TestUser`) while a pytest node id is
    `test_fetch.py::TestUser::test_x`. Reconstructing it matters because that id is what an
    `Evidence(kind=TEST_FAILURE)` ref must resolve against.
    """
    classname = case.get("classname", "")
    name = case.get("name", "?")
    if not classname:
        return name
    parts = classname.split(".")
    module, rest = parts[0], parts[1:]
    return "::".join([f"{module}.py", *rest, name])


def regression_node_ids(before: TestResult, after: TestResult) -> list[str]:
    """Tests that passed before the patch and fail after it -- policy row 2's exact condition.

    A test that was *already* failing (the bug we were asked to fix) failing again is a
    different signal, handled as a normal CORRECTNESS issue, so it is excluded here.
    """
    if not (before.ran and after.ran):
        return []
    was_failing = set(before.failed_node_ids)
    return sorted(node for node in after.failed_node_ids if node not in was_failing)
