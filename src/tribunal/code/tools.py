"""The tools, and the workspace they are confined to.

Three properties are worth stating up front, because they are what makes this surface
different from the rest of the project.

**The workspace is a boundary, not a suggestion.** Every path the model supplies is resolved
and checked against the session root before anything opens it. `../../.ssh/id_rsa` is a
refusal that the model sees and can react to, not an exception that ends the session, and
not something an approval prompt can be talked into.

**`bash` is not sandboxed, and nothing here pretends otherwise.** `sandbox.py` scrubs the
environment down to four variables and puts `PATH` at `/usr/bin:/bin`, which is correct for
executing a model's patch of an untrusted input file and useless for a developer who wants
`pytest` and `git` to work in their own repository. So commands run with the user's real
environment, and the protection is the approval prompt in `approval.py` plus a timeout and an
output cap. A reader deciding whether to pass `--yes` should be deciding against that
sentence, not against the word "sandbox" used loosely.

**Every failure is an observation.** A missing file, a bad regex, an ambiguous edit anchor
and a refused path all come back as `Observation(ok=False)` with a message written for the
model to act on. Raising would end the turn and throw away the transcript; returning lets the
next step be the recovery, which is the behaviour that makes an agent feel competent rather
than brittle.
"""

from __future__ import annotations

import fnmatch
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tribunal.code.actions import Step, ToolName
from tribunal.config import Settings


class WorkspaceError(ValueError):
    """A path escaped the session root, or the root itself is not usable."""


@dataclass
class Observation:
    """What a tool did, in the two registers the session needs.

    `text` is what the model sees on the next turn; `summary` is the one line the terminal
    shows. They differ because a 6,000-character test log is the right thing to reason over
    and the wrong thing to scroll past.
    """

    ok: bool
    text: str
    summary: str
    #: Bytes of output that were dropped by the cap, so neither reader silently believes they
    #: saw all of it.
    elided_chars: int = 0
    #: The structured half, for tools whose result has a shape worth rendering rather than
    #: printing. Only `review` fills it in: an outcome, a rule and a list of issue fates are
    #: the one output in this system designed to be read at a glance, and flattening that to
    #: text for the terminal would throw the design away. The model still gets `text`.
    data: dict[str, Any] | None = None


@dataclass
class Snapshot:
    """One file as it was before a tool changed it. `before is None` means it did not exist."""

    path: Path
    before: str | None
    step: str


@dataclass
class Workspace:
    """The session root, plus the undo log.

    The undo log is not a convenience feature. An agent that can write files needs a way to
    say "that was wrong" that does not depend on the repository being a git checkout with a
    clean tree, because the case where a user most wants to reach for it is the case where
    those assumptions are least likely to hold.
    """

    root: Path
    snapshots: list[Snapshot] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        if not self.root.is_dir():
            raise WorkspaceError(f"not a directory: {self.root}")

    def resolve(self, raw: str) -> Path:
        """Resolve a model-supplied path, refusing anything outside the root.

        `Path.resolve()` before the check, so `a/../../etc/passwd` and a symlink pointing out
        of the tree are both caught -- a string-prefix test on the unresolved path catches
        neither.
        """
        candidate = (self.root / raw.strip()).expanduser()
        resolved = candidate.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise WorkspaceError(
                f"{raw!r} resolves outside the session workspace ({self.root}). "
                "Only paths inside it can be read or written."
            )
        return resolved

    def display(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:  # pragma: no cover - resolve() already prevents this
            return str(path)

    def record(self, path: Path, step: str) -> None:
        self.snapshots.append(
            Snapshot(
                path=path,
                before=path.read_text(encoding="utf-8", errors="replace")
                if path.is_file()
                else None,
                step=step,
            )
        )

    def undo_last(self) -> str | None:
        """Restore the most recent change. Returns a description, or None if there is none."""
        if not self.snapshots:
            return None
        snapshot = self.snapshots.pop()
        if snapshot.before is None:
            snapshot.path.unlink(missing_ok=True)
            return f"removed {self.display(snapshot.path)} (it did not exist before)"
        snapshot.path.write_text(snapshot.before, encoding="utf-8")
        return f"restored {self.display(snapshot.path)}"


@dataclass
class ToolContext:
    """Everything a tool may reach. Passed explicitly so a tool cannot acquire more."""

    workspace: Workspace
    settings: Settings
    #: Only `review` needs one. None means the tool reports that it is unavailable rather
    #: than constructing a second client with a second budget behind the session's back.
    client: object | None = None


# ------------------------------------------------------------------------------------------
# Dispatch
# ------------------------------------------------------------------------------------------


async def execute(step: Step, context: ToolContext) -> Observation:
    """Run one step. Never raises for anything the model can cause.

    Async only because of `review`, which drives the orchestrator's own async loop. The
    other tools are ordinary blocking calls and are not worth a thread: nothing else is
    running while a step executes, by construction -- one tool call per turn.
    """
    handler = {
        ToolName.READ: _read,
        ToolName.LIST: _list,
        ToolName.GREP: _grep,
        ToolName.GLOB: _glob,
        ToolName.WRITE: _write,
        ToolName.EDIT: _edit,
        ToolName.BASH: _bash,
    }.get(step.tool)
    if step.tool is ToolName.REVIEW:
        try:
            return await _review(step, context)
        except WorkspaceError as exc:
            return Observation(False, f"refused: {exc}", "refused: outside the workspace")
    if handler is None:  # `done` is handled by the session, not here.
        return Observation(False, f"{step.tool.value} is not an executable tool", "not a tool")
    try:
        return handler(step, context)
    except WorkspaceError as exc:
        return Observation(False, f"refused: {exc}", "refused: outside the workspace")
    except OSError as exc:
        # A permission error, a directory where a file was expected, a broken symlink. The
        # model can route around all of them if it is told which one happened.
        return Observation(False, f"failed: {exc}", f"failed: {type(exc).__name__}")


# ------------------------------------------------------------------------------------------
# Reading
# ------------------------------------------------------------------------------------------


def _read(step: Step, context: ToolContext) -> Observation:
    path = context.workspace.resolve(step.path)
    if path.is_dir():
        return _list(step, context)
    if not path.is_file():
        return Observation(
            False,
            f"no such file: {context.workspace.display(path)}. "
            "Use `glob` or `list` if you are not sure of the path.",
            "no such file",
        )
    if path.stat().st_size > context.settings.code.max_file_bytes:
        return Observation(
            False,
            f"{context.workspace.display(path)} is larger than the "
            f"{context.settings.code.max_file_bytes} byte read limit. "
            "Use `grep` to find the part you need, then `read` with `start_line`.",
            "file too large",
        )

    text = path.read_text(encoding="utf-8", errors="replace")
    if "\x00" in text[:4096]:
        return Observation(
            False, f"{context.workspace.display(path)} looks binary", "binary file"
        )

    lines = text.splitlines()
    start = max(step.start_line - 1, 0)
    count = step.max_lines or context.settings.code.max_read_lines
    window = lines[start : start + count]
    numbered = "\n".join(f"{start + i + 1:>6}\t{line}" for i, line in enumerate(window))

    tail = start + len(window)
    note = ""
    if tail < len(lines):
        note = (
            f"\n\n[{len(lines) - tail} more line(s). "
            f"Read on with start_line={tail + 1}.]"
        )
    body = f"{context.workspace.display(path)} (lines {start + 1}-{tail} of {len(lines)}):\n"
    return Observation(
        True,
        body + numbered + note,
        f"read {context.workspace.display(path)} ({len(window)} lines)",
    )


def _list(step: Step, context: ToolContext) -> Observation:
    root = context.workspace.resolve(step.path or ".")
    if not root.is_dir():
        return Observation(False, f"not a directory: {step.path}", "not a directory")

    ignore = set(context.settings.code.ignore_dirs)
    limit = context.settings.code.max_glob_results
    rows: list[str] = []
    truncated = False

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in ignore and not d.startswith("."))
        here = Path(dirpath)
        depth = len(here.relative_to(root).parts)
        if depth > 3:
            dirnames[:] = []
            continue
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            if len(rows) >= limit:
                truncated = True
                break
            rows.append(str((here / name).relative_to(root)))
        if truncated:
            break

    note = (
        f"\n[stopped at {limit} entries; narrow with `path` or `glob`]" if truncated else ""
    )
    listing = "\n".join(rows) or "(no files)"
    return Observation(
        True,
        f"{context.workspace.display(root)}/ contains:\n{listing}{note}",
        f"list {context.workspace.display(root)} ({len(rows)} files)",
    )


def _grep(step: Step, context: ToolContext) -> Observation:
    try:
        pattern = re.compile(step.pattern)
    except re.error as exc:
        return Observation(
            False,
            f"{step.pattern!r} is not a valid regular expression: {exc}",
            "bad pattern",
        )

    root = context.workspace.resolve(step.path or ".")
    limit = context.settings.code.max_grep_matches
    hits: list[str] = []
    scanned = 0
    for path in _walk_files(root, context.settings):
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"{context.workspace.display(path)}:{number}: {line.strip()[:200]}")
                if len(hits) >= limit:
                    break
        if len(hits) >= limit:
            break

    if not hits:
        return Observation(
            True,
            f"no match for {step.pattern!r} in {scanned} file(s) under "
            f"{context.workspace.display(root)}",
            "no matches",
        )
    note = f"\n[stopped at {limit} matches]" if len(hits) >= limit else ""
    return Observation(
        True,
        "\n".join(hits) + note,
        f"grep {step.pattern!r} ({len(hits)} match(es))",
    )


def _glob(step: Step, context: ToolContext) -> Observation:
    root = context.workspace.resolve(step.path or ".")
    limit = context.settings.code.max_glob_results
    pattern = step.pattern.strip()
    matches: list[str] = []
    for path in _walk_files(root, context.settings):
        relative = context.workspace.display(path)
        if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(path.name, pattern):
            matches.append(relative)
            if len(matches) >= limit:
                break
    if not matches:
        return Observation(True, f"nothing matches {pattern!r}", "no matches")
    return Observation(
        True, "\n".join(sorted(matches)), f"glob {pattern} ({len(matches)} file(s))"
    )


def _walk_files(root: Path, settings: Settings):
    """Every readable, plausibly-textual file under `root`, ignore list applied."""
    ignore = set(settings.code.ignore_dirs)
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore]
        for name in sorted(filenames):
            path = Path(dirpath) / name
            try:
                if path.stat().st_size > settings.code.max_file_bytes:
                    continue
            except OSError:
                continue
            yield path


# ------------------------------------------------------------------------------------------
# Writing
# ------------------------------------------------------------------------------------------


def _write(step: Step, context: ToolContext) -> Observation:
    path = context.workspace.resolve(step.path)
    if path.is_dir():
        return Observation(False, f"{step.path} is a directory", "is a directory")
    existed = path.is_file()
    context.workspace.record(path, f"write {context.workspace.display(path)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(step.content, encoding="utf-8")
    verb = "overwrote" if existed else "created"
    lines = len(step.content.splitlines())
    return Observation(
        True,
        f"{verb} {context.workspace.display(path)} ({lines} line(s))",
        f"{verb} {context.workspace.display(path)}",
    )


def _edit(step: Step, context: ToolContext) -> Observation:
    """Replace one literal, unambiguous occurrence.

    The same contract the Coder works under (`patch.apply_search_replace`, and the rubric in
    `llm/prompts/coder.md`): the anchor is *literal text*, not a pattern, and it must match
    exactly once. Two matches is a refusal rather than a guess, because the wrong one of two
    identical-looking regions is the edit a reviewer is least likely to catch.
    """
    path = context.workspace.resolve(step.path)
    if not path.is_file():
        return Observation(
            False,
            f"no such file: {context.workspace.display(path)}. `write` creates a new one.",
            "no such file",
        )
    original = path.read_text(encoding="utf-8", errors="replace")
    occurrences = original.count(step.search)

    if occurrences == 0:
        hint = ""
        squashed = re.sub(r"\s+", " ", step.search.strip())
        if squashed and re.sub(r"\s+", " ", original).count(squashed):
            hint = (
                " The text is present but the whitespace differs -- copy the anchor verbatim "
                "from a `read`, including indentation."
            )
        return Observation(
            False,
            f"the search text does not occur in {context.workspace.display(path)}.{hint}",
            "anchor not found",
        )
    if occurrences > 1:
        return Observation(
            False,
            f"the search text occurs {occurrences} times in "
            f"{context.workspace.display(path)}; it must be unique. Include more surrounding "
            "lines until it is.",
            f"anchor matches {occurrences} times",
        )

    context.workspace.record(path, f"edit {context.workspace.display(path)}")
    path.write_text(original.replace(step.search, step.replace, 1), encoding="utf-8")
    delta = len(step.replace.splitlines()) - len(step.search.splitlines())
    return Observation(
        True,
        f"edited {context.workspace.display(path)} (net {delta:+d} line(s))",
        f"edited {context.workspace.display(path)}",
    )


# ------------------------------------------------------------------------------------------
# Executing
# ------------------------------------------------------------------------------------------


def _bash(step: Step, context: ToolContext) -> Observation:
    timeout = context.settings.code.command_timeout_seconds
    started = time.monotonic()
    # `start_new_session` so the timeout can kill the whole process *group*. Without it a
    # command that backgrounds something survives the kill and holds the pipe open, which
    # looks to the session like a hang with no output -- the exact failure `sandbox.py`
    # documents at its own timeout path.
    process = subprocess.Popen(  # noqa: S602 - shell is the point; see the module docstring
        step.command,
        shell=True,
        cwd=context.workspace.root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        output = process.communicate(timeout=timeout)[0] or ""
        code = process.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill_group(process)
        output = process.communicate()[0] or ""
        code = None
        timed_out = True

    elapsed = time.monotonic() - started
    body, elided = _cap(output, context.settings.code.max_output_chars)
    if timed_out:
        return Observation(
            False,
            f"$ {step.command}\n[killed after {timeout}s]\n{body}",
            f"timed out after {timeout}s",
            elided,
        )
    status = "ok" if code == 0 else f"exit {code}"
    return Observation(
        code == 0,
        f"$ {step.command}\n[{status}, {elapsed:.1f}s]\n{body or '(no output)'}",
        f"{step.command[:60]} — {status}",
        elided,
    )


def _kill_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # pragma: no cover - race on exit
        process.kill()


def _cap(text: str, limit: int) -> tuple[str, int]:
    """Keep the head and the tail. The middle of a long log is the least informative part --
    a traceback's first frames and its last line are what the next step needs."""
    if len(text) <= limit:
        return text, 0
    head = int(limit * 0.4)
    tail = limit - head
    dropped = len(text) - limit
    return f"{text[:head]}\n[… {dropped} characters elided …]\n{text[-tail:]}", dropped


# ------------------------------------------------------------------------------------------
# The tribunal itself, as a tool
# ------------------------------------------------------------------------------------------


async def _review(step: Step, context: ToolContext) -> Observation:
    """Run the full adversarial loop on one Python file.

    This is the reason the coding agent lives in this repository rather than being a generic
    wrapper: the thing it can call that nothing else can is two independent critics and a
    deterministic decision table. It is also the most expensive tool by an order of
    magnitude, which is why `approval.py` gates it even in modes where `bash` runs freely.

    The accepted diff is *reported, not applied*. Applying it would mean a patch reaching the
    working tree without the user seeing the verdict that produced it, and `TRADEOFF` -- the
    outcome this project exists to be able to emit -- has no sensible automatic action.
    """
    from tribunal.orchestrator import Orchestrator

    if context.client is None:
        return Observation(
            False,
            "review is unavailable in this session (no LLM client)",
            "review unavailable",
        )
    path = context.workspace.resolve(step.path)
    if not path.is_file():
        return Observation(False, f"no such file: {step.path}", "no such file")
    if path.suffix != ".py":
        return Observation(
            False,
            "the tribunal grounds its critics in Python tooling (bandit, ruff, radon, "
            f"astgate), so it only reviews .py files; {path.name} is not one.",
            "not a Python file",
        )

    orchestrator = Orchestrator(context.settings, context.client)
    result = await orchestrator.run(
        path.read_text(encoding="utf-8"),
        filename=path.name,
        argv=["code", "review", context.workspace.display(path)],
    )
    report = result.report
    lines = [
        f"tribunal on {context.workspace.display(path)}: "
        f"{report.outcome.value.upper()} via {report.rule_fired} "
        f"({report.rounds_used} round(s), ${report.total_cost_usd:.4f})",
    ]
    for fate in report.issues:
        lines.append(
            f"  {fate.issue.id}  {fate.issue.dimension.value}/{fate.issue.severity.value}  "
            f"{fate.status}  {fate.issue.title}"
        )
    if report.conflict:
        lines.append(f"  trade-off on {report.conflict.axis}: {report.conflict.right_issue}")
    if report.accepted_diff:
        lines.append("\nthe accepted patch (NOT applied -- apply it with `edit` if you agree):")
        lines.append(report.accepted_diff)
    else:
        lines.append(f"\nno patch accepted: {report.no_patch_reason}")

    body, elided = _cap("\n".join(lines), context.settings.code.max_output_chars * 2)
    return Observation(
        report.outcome.value == "accept",
        body,
        f"tribunal: {report.outcome.value} via {report.rule_fired}",
        elided,
        data={
            "target": context.workspace.display(path),
            "outcome": report.outcome.value,
            "rule_fired": report.rule_fired,
            "rounds": report.rounds_used,
            "cost_usd": report.total_cost_usd,
            "issues": [
                {
                    "id": fate.issue.id,
                    "dimension": fate.issue.dimension.value,
                    "severity": fate.issue.severity.value,
                    "status": fate.status,
                    "title": fate.issue.title,
                }
                for fate in report.issues
            ],
            "conflict": (
                f"{report.conflict.axis}: {report.conflict.right_issue}"
                if report.conflict
                else None
            ),
            "diff": report.accepted_diff or None,
            "no_patch_reason": report.no_patch_reason,
        },
    )
