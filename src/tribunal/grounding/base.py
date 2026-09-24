"""The grounding tool interface.

Grounding is the differentiator: every critic claim must cite a real tool result, which is what
keeps the critics from degrading into unfalsifiable opinion (docs/00-charter.md § Why this
scoping is the right answer). This module owns the two things that makes possible:

1. **Normalisation.** Five tools with five output formats become one `GroundingFinding` list,
   so `Evidence(kind=TOOL_FINDING, ref=<finding id>)` is checkable by code rather than by
   reading.
2. **Stable identity.** `GroundingFinding.id` is `sha1(tool|rule|file|line)`, and tools are
   run against a *copy* in a throwaway scratch directory. If the on-disk path leaked into the
   id, every finding id would change between runs and between the baseline and the patched
   pass -- which would silently break issue dismissal, the oscillation guard, and conflict
   detector 1. So the tool's reported path is always rewritten to `GroundingTarget.logical_name`
   before the id is computed.

Static tools (`bandit`, `ruff`, `radon`, `astgate`) never execute the target, so they run as
plain subprocesses and need no sandbox. Only `pytest_t` and `perf_t` go through `sandbox.py`.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from tribunal.contracts import GroundingFinding

#: Cap on captured tool output. Tool stdout goes into the trace, and a linter on a pathological
#: file can emit megabytes.
OUTPUT_CAP = 256 * 1024


@dataclass(frozen=True)
class GroundingTarget:
    """A source file staged for analysis.

    `logical_name` is the only path that is ever allowed into a finding, an evidence ref, or
    the trace. `path` is a scratch-directory artefact and changes every run.
    """

    logical_name: str
    path: Path
    source: str

    @classmethod
    def stage(
        cls, source: str, directory: Path, logical_name: str = "target.py"
    ) -> GroundingTarget:
        path = directory / logical_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        return cls(logical_name=logical_name, path=path, source=source)


@dataclass
class ToolOutcome:
    """One tool's run. Carries enough to emit a `tool_run` trace event verbatim."""

    tool: str
    argv: list[str]
    exit_code: int | None
    findings: list[GroundingFinding] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0
    stdout_excerpt: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None


class ToolUnavailable(RuntimeError):
    """The tool's binary or module is not importable in this environment."""


class GroundingTool(ABC):
    """Run a tool, normalise its output, never raise into the orchestrator.

    A tool that crashes must not take the round down with it: the failure becomes
    `ToolOutcome.error`, which the suite folds into `GroundingReport.tool_errors`. A missing
    tool result is visible in the trace rather than looking like a clean file -- the same
    "unassessed is not clean" discipline the policy layer applies to critics.
    """

    name: str
    #: Static tools analyse the file without running it, so Layer 1 does not apply to them.
    executes_target: bool = False

    @abstractmethod
    def argv(self, target: GroundingTarget) -> list[str]:
        """The command to run. Prefer `python -m <tool>` over a PATH lookup."""

    @abstractmethod
    def parse(
        self, stdout: str, stderr: str, exit_code: int, target: GroundingTarget
    ) -> list[GroundingFinding]:
        """Normalise the tool's output. May raise; `run` converts that to `ToolOutcome.error`."""

    def accepts_exit_code(self, code: int) -> bool:
        """Linters exit non-zero when they find things. Only some codes mean 'I crashed'."""
        return code in (0, 1)

    async def run(self, target: GroundingTarget, timeout: int = 60) -> ToolOutcome:
        argv = self.argv(target)
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(target.path.parent),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env(),
            )
        except FileNotFoundError as exc:
            return ToolOutcome(
                tool=self.name,
                argv=argv,
                exit_code=None,
                error=f"not installed: {exc.filename}",
                duration_ms=_elapsed(started),
            )
        try:
            raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolOutcome(
                tool=self.name,
                argv=argv,
                exit_code=None,
                error=f"timed out after {timeout}s",
                duration_ms=_elapsed(started),
            )

        stdout = _decode(raw_out)
        stderr = _decode(raw_err)
        code = proc.returncode if proc.returncode is not None else -1
        outcome = ToolOutcome(
            tool=self.name,
            argv=argv,
            exit_code=code,
            duration_ms=_elapsed(started),
            stdout_excerpt=stdout[:2000],
        )
        if not self.accepts_exit_code(code):
            outcome.error = f"exit {code}: {(stderr or stdout).strip()[:400]}"
            return outcome
        try:
            outcome.findings = self.parse(stdout, stderr, code, target)
        except Exception as exc:  # noqa: BLE001 - a parse bug must not fail the round
            outcome.error = f"could not parse {self.name} output: {type(exc).__name__}: {exc}"
        return outcome

    # -- helpers for subclasses ----------------------------------------------------------

    @staticmethod
    def _env() -> dict[str, str]:
        """Tool subprocesses inherit the environment minus anything secret-shaped.

        These are trusted binaries, so this is hygiene rather than a boundary -- but tool
        stdout lands in the trace, and traces get pasted into issue reports.
        """
        from tribunal.sandbox import _is_denied

        env = {k: v for k, v in os.environ.items() if not _is_denied(k)}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["NO_COLOR"] = "1"
        return env

    @staticmethod
    def python() -> str:
        return sys.executable


def _decode(raw: bytes) -> str:
    return raw[:OUTPUT_CAP].decode("utf-8", errors="replace")


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
