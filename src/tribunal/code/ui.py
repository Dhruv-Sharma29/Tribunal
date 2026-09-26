"""The terminal's look: glyphs, colour, and the four things a session has to show well.

A coding agent's interface has one job that a general CLI does not: the user is watching a
process they did not author, deciding in real time whether to let it continue. Everything
below follows from that.

**One line per action, indented result underneath.** A step is `⏺ Edit(cart.py)` with its
outcome on a `⎿` continuation line. The eye can scan a column of `⏺` and see the shape of
what happened without reading a word of it, which is what makes a forty-step session
reviewable at all. Results are summarised, not dumped -- except failures, which get their
first lines, because that is the moment a person actually wants the text.

**The wait is accounted for.** A blocked terminal with no output is indistinguishable from a
hang. The spinner carries elapsed seconds, the step number and the running spend, so the
decision "is this stuck" is never a guess.

**Approval is a dialog, not a y/n.** It shows the *effect* -- a real unified diff against the
file on disk, not the search/replace pair the model happened to emit -- and it offers the
third answer that matters: no, *and here is what to do instead*. A refusal that carries a
sentence of direction is worth several rounds of the agent guessing why it was declined.

**A verdict is not a tool result.** When `review` returns, the tribunal has produced an
outcome, a rule that fired, and a list of issues with fates. Rendering that as a wall of text
throws away the one output in this system that was designed to be read at a glance, so it
gets a panel with the verdict in the colour of its outcome.

## Colour

Amber for the system's own voice, because the tribunal's furniture is brass; verdict colours
are the same three used by `tribunal run` (`accept` green, `tradeoff` yellow, `reject` and
`escalate` red), so a verdict means the same thing in both surfaces. Everything secondary is
grey and nothing relies on colour alone -- each verdict carries its word, each result its
summary. The palette sets no background, so it stays legible on light and dark terminals.
"""

from __future__ import annotations

import itertools
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from tribunal.code.actions import Step, ToolName
from tribunal.code.tools import Observation

THEME = Theme(
    {
        "t.accent": "bold #d08a4a",
        "t.rule": "#7a6a58",
        "t.dim": "grey50",
        "t.tool": "bold #5fafd7",
        "t.ok": "green",
        "t.warn": "yellow",
        "t.bad": "red",
        "t.thought": "italic grey62",
        "t.add": "green",
        "t.del": "red",
        "t.hunk": "cyan",
        # The three verdict colours, identical to `tribunal run`'s.
        "t.accept": "bold green",
        "t.tradeoff": "bold yellow",
        "t.reject": "bold red",
        "t.escalate": "bold red",
    }
)

#: Rotated while the model is thinking. Tribunal's own vocabulary rather than a generic
#: "Loading": the verb is a small, free reminder of what the thing in front of you is.
VERBS = (
    "Deliberating",
    "Examining",
    "Weighing",
    "Considering",
    "Reviewing",
    "Cross-checking",
)

SPINNER_FRAMES = ("✳", "✻", "✽", "✻")


def _renders(sample: str) -> bool:
    """Whether this terminal's encoding can print `sample`. Not every one can."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        sample.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


@dataclass(frozen=True)
class Glyphs:
    """Unicode where the terminal supports it, ASCII where it does not."""

    action: str = "⏺"
    result: str = "⎿"
    prompt: str = "›"
    scales: str = "⚖"
    spinner: tuple[str, ...] = SPINNER_FRAMES

    @classmethod
    def detect(cls) -> Glyphs:
        if _renders("⏺⎿›⚖✻"):
            return cls()
        return cls(action="*", result="\\_", prompt=">", scales="", spinner=("-", "\\", "|", "/"))


GLYPHS = Glyphs.detect()

#: How each tool is named in the action line. Capitalised like a function call, because that
#: is what it is, and because `Bash(...)` scans differently from prose.
TOOL_LABEL = {
    ToolName.READ: "Read",
    ToolName.LIST: "List",
    ToolName.GREP: "Grep",
    ToolName.GLOB: "Glob",
    ToolName.WRITE: "Write",
    ToolName.EDIT: "Edit",
    ToolName.BASH: "Bash",
    ToolName.REVIEW: "Tribunal",
    ToolName.DONE: "Done",
}


def console(**kwargs) -> Console:
    return Console(theme=THEME, **kwargs)


# ------------------------------------------------------------------------------------------
# The session frame
# ------------------------------------------------------------------------------------------


def banner(version: str, model: str, mode: str, root: Path, width: int = 76) -> RenderableType:
    """The opening box: what you are talking to, where, and under what rules."""
    title = Text.assemble(
        (f"{GLYPHS.scales}  " if GLYPHS.scales else "", "t.accent"),
        ("tribunal code", "t.accent"),
        (f"  {version}", "t.dim"),
    )
    where = Text.assemble(
        (model, "t.tool"),
        ("  ·  ", "t.dim"),
        (mode, "t.warn" if mode != "ask" else "t.dim"),
        ("  ·  ", "t.dim"),
        (_short_path(root), "t.dim"),
    )
    hint = Text(
        "/help for commands · /exit to leave · ctrl-c interrupts a turn", style="t.dim"
    )
    return Panel(
        Group(title, Text(), where, hint),
        border_style="t.rule",
        padding=(0, 2),
        width=width,
    )


def prompt_text() -> str:
    """The input prompt. Plain, because `readline` owns this line.

    A drawn input box is the one piece of Claude Code's look that is not worth copying here:
    it needs the terminal in raw mode, and that costs the user their own readline
    configuration -- history, reverse search, word motion, everything they have muscle memory
    for. A visible accent glyph and a blank line above it carry the same "your turn" signal.
    """
    return f"{GLYPHS.prompt} "


def thought_line(text: str) -> Text:
    return Text(text, style="t.thought")


def action_line(step: Step) -> Text:
    """`⏺ Edit(cart.py)` -- the scannable column."""
    return Text.assemble(
        (f"{GLYPHS.action} ", "t.accent"),
        (TOOL_LABEL[step.tool], "t.tool"),
        ("(", "t.dim"),
        (_argument(step), "default"),
        (")", "t.dim"),
    )


def result_line(observation: Observation) -> Text:
    style = "t.dim" if observation.ok else "t.warn"
    return Text.assemble(
        (f"  {GLYPHS.result} ", "t.dim"), (observation.summary, style)
    )


def failure_excerpt(observation: Observation, lines: int = 10) -> RenderableType:
    """The first lines of a failed step, indented under its result.

    `Padding` rather than a prefix per line: a prefix indents the source lines and leaves
    every *wrapped* continuation hard against the margin, which breaks the column the
    `⎿` rendering exists to create.
    """
    from rich.padding import Padding

    body = "\n".join(observation.text.splitlines()[:lines])
    return Padding(Text(body, style="t.dim"), (0, 0, 0, 5))


def status_line(steps: int, calls: int, cost: float, seconds: float | None = None) -> Text:
    parts = [
        (f"  {steps} step(s)", "t.dim"),
        ("  ·  ", "t.rule"),
        (f"{calls} call(s)", "t.dim"),
        ("  ·  ", "t.rule"),
        (f"${cost:.4f}", "t.dim"),
    ]
    if seconds is not None:
        parts += [("  ·  ", "t.rule"), (f"{seconds:.1f}s", "t.dim")]
    return Text.assemble(*parts)


def reply(message: str) -> RenderableType:
    """The agent's answer. Bordered, so it is never confused with a tool result."""
    return Panel(Text(message), border_style="t.rule", padding=(0, 1))


# ------------------------------------------------------------------------------------------
# Waiting
# ------------------------------------------------------------------------------------------


class Thinking:
    """A spinner that says how long it has been going, and what it is costing.

    Its own thread, because the turn blocks the main one inside the event loop. Silent on a
    non-terminal: a status line written to a pipe is noise in a log and noise in a test.
    """

    def __init__(self, console: Console, step: int, cost: float = 0.0) -> None:
        self.console = console
        self.step = step
        self.cost = cost
        self.started = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._live = None

    def __enter__(self) -> Thinking:
        if not self.console.is_terminal:
            return self
        from rich.live import Live

        self._live = Live(
            self._render(next(iter(GLYPHS.spinner))),
            console=self.console,
            refresh_per_second=8,
            transient=True,
        )
        self._live.start()
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._live is not None:
            self._live.stop()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def _animate(self) -> None:
        frames = itertools.cycle(GLYPHS.spinner)
        verb = VERBS[(self.step - 1) % len(VERBS)]
        while not self._stop.wait(0.125):
            if self._live is not None:
                self._live.update(self._render(next(frames), verb))

    def _render(self, frame: str, verb: str | None = None) -> Text:
        verb = verb or VERBS[(self.step - 1) % len(VERBS)]
        parts = [
            (f"{frame} ", "t.accent"),
            (f"{verb}… ", "t.accent"),
            (f"({self.elapsed:.0f}s", "t.dim"),
            (f" · step {self.step}", "t.dim"),
        ]
        if self.cost:
            parts.append((f" · ${self.cost:.4f}", "t.dim"))
        parts.append((" · ctrl-c to interrupt)", "t.dim"))
        return Text.assemble(*parts)


# ------------------------------------------------------------------------------------------
# Approval
# ------------------------------------------------------------------------------------------


def approval_panel(step: Step, key: str, notes: list[str], body: RenderableType) -> Panel:
    """The dialog. Numbered answers, because typing `2` is faster than spelling `always`."""
    options = Table.grid(padding=(0, 1))
    options.add_column(style="t.accent", no_wrap=True)
    options.add_column()
    options.add_row("1.", Text("yes"))
    options.add_row("2.", Text.assemble(("yes, and stop asking for ", "default"), (key, "t.tool")))
    options.add_row("3.", Text("no, and tell it what to do instead"))

    rows: list[RenderableType] = [body, Text()]
    for note in notes:
        rows.append(Text(f"! {note}", style="t.warn"))
    if notes:
        rows.append(Text())
    rows.append(options)

    return Panel(
        Group(*rows),
        title=Text(f" {TOOL_LABEL[step.tool].lower()} ", style="t.warn"),
        title_align="left",
        border_style="t.warn",
        padding=(0, 1),
    )


def edit_diff(step: Step, current: str | None) -> RenderableType:
    """A real unified diff of the edit, against the file as it is on disk.

    Not the search/replace pair: those are the model's *instructions*, and what a person
    needs to approve is the *effect*. Falls back to the raw blocks when the anchor does not
    resolve -- which is itself worth seeing, because it means the edit is about to be
    refused.
    """
    from tribunal.patch import make_unified_diff

    if current is not None and current.count(step.search) == 1:
        patched = current.replace(step.search, step.replace, 1)
        return diff_text(make_unified_diff(current, patched, step.path))

    blocks = Text()
    for line in step.search.splitlines():
        blocks.append(f"- {line}\n", style="t.del")
    for line in step.replace.splitlines():
        blocks.append(f"+ {line}\n", style="t.add")
    return blocks


def diff_text(diff: str, limit: int = 40) -> Text:
    """Colour a unified diff without Rich's `diff` lexer, which does not mark hunks."""
    out = Text()
    for index, line in enumerate(diff.splitlines()):
        if index >= limit:
            out.append(f"  … {len(diff.splitlines()) - limit} more line(s)\n", style="t.dim")
            break
        if line.startswith("@@"):
            out.append(line + "\n", style="t.hunk")
        elif line.startswith("+++") or line.startswith("---"):
            out.append(line + "\n", style="t.dim")
        elif line.startswith("+"):
            out.append(line + "\n", style="t.add")
        elif line.startswith("-"):
            out.append(line + "\n", style="t.del")
        else:
            out.append(line + "\n")
    out.rstrip()
    return out


def write_preview(step: Step, lines: int = 20) -> RenderableType:
    from rich.syntax import Syntax

    body = "\n".join(step.content.splitlines()[:lines])
    more = len(step.content.splitlines()) - lines
    syntax = Syntax(body, _lexer(step.path), theme="ansi_dark", word_wrap=True)
    if more > 0:
        return Group(syntax, Text(f"… {more} more line(s)", style="t.dim"))
    return syntax


# ------------------------------------------------------------------------------------------
# The verdict
# ------------------------------------------------------------------------------------------


def verdict_panel(data: dict) -> RenderableType:
    """What `review` returns, rendered as the tribunal's own output rather than as text.

    `data` is the structured half of the observation: outcome, the rule that fired, the
    issues and their fates, and the diff if one was accepted.
    """
    outcome = str(data.get("outcome", "unknown"))
    style = {
        "accept": "t.accept",
        "tradeoff": "t.tradeoff",
        "reject": "t.reject",
        "escalate": "t.escalate",
    }.get(outcome, "t.dim")

    header = Text.assemble(
        (outcome.upper(), style),
        ("  via  ", "t.dim"),
        (str(data.get("rule_fired", "?")), "default"),
    )
    meta = Text.assemble(
        (f"{data.get('rounds', 0)} round(s)", "t.dim"),
        ("  ·  ", "t.rule"),
        (f"${data.get('cost_usd', 0.0):.4f}", "t.dim"),
        ("  ·  ", "t.rule"),
        (str(data.get("target", "")), "t.dim"),
    )

    rows: list[RenderableType] = [header, meta]
    issues = data.get("issues") or []
    if issues:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="t.dim", no_wrap=True)
        table.add_column(no_wrap=True)
        table.add_column(style="t.dim", no_wrap=True)
        table.add_column()
        for issue in issues[:12]:
            table.add_row(
                issue.get("id", ""),
                Text(
                    f"{issue.get('dimension', '')}/{issue.get('severity', '')}",
                    style=_severity_style(issue.get("severity", "")),
                ),
                issue.get("status", ""),
                issue.get("title", ""),
            )
        rows += [Text(), table]

    if data.get("conflict"):
        rows += [Text(), Text(f"trade-off: {data['conflict']}", style="t.tradeoff")]
    if data.get("diff"):
        rows += [
            Text(),
            Text("the accepted patch — reported, not applied:", style="t.dim"),
            diff_text(str(data["diff"])),
        ]
    elif data.get("no_patch_reason"):
        rows += [Text(), Text(f"no patch: {data['no_patch_reason']}", style="t.dim")]

    return Panel(Group(*rows), border_style=style, padding=(0, 1),
                 title=Text(" tribunal ", style=style), title_align="left")


def _severity_style(severity: str) -> str:
    return {"critical": "t.bad", "high": "t.bad", "medium": "t.warn"}.get(severity, "t.dim")


# ------------------------------------------------------------------------------------------
# Help
# ------------------------------------------------------------------------------------------


def help_panel(commands: dict[str, str], tools: dict[str, str]) -> RenderableType:
    left = Table.grid(padding=(0, 2))
    left.add_column(style="t.accent", no_wrap=True)
    left.add_column(style="t.dim")
    for name, description in commands.items():
        left.add_row(name, description)

    right = Table.grid(padding=(0, 2))
    right.add_column(style="t.tool", no_wrap=True)
    right.add_column(style="t.dim")
    for name, description in tools.items():
        right.add_row(name, description)

    return Group(
        Text("commands", style="t.accent"),
        left,
        Text(),
        Text("what it can do", style="t.accent"),
        right,
    )


def _argument(step: Step) -> str:
    if step.tool is ToolName.BASH:
        return step.command if len(step.command) <= 72 else step.command[:71] + "…"
    if step.tool in {ToolName.GREP, ToolName.GLOB}:
        where = f", {step.path}" if step.path else ""
        return f"{step.pattern}{where}"
    if step.tool is ToolName.READ and step.start_line:
        return f"{step.path}:{step.start_line}"
    if step.tool is ToolName.LIST:
        return step.path or "."
    return step.path


def _short_path(path: Path, keep: int = 3) -> str:
    """`~/proj/src` where possible, `…/deep/nested/dir` otherwise.

    A full path can be longer than the banner, and a banner that wraps onto three lines to
    tell you where you are is worse at telling you where you are.
    """
    try:
        shortened = "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        shortened = str(path)
    if len(shortened) <= 46:
        return shortened
    parts = Path(shortened).parts[-keep:]
    return "…/" + "/".join(parts)


def _lexer(path: str) -> str:
    return {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".md": "markdown",
        ".toml": "toml",
        ".json": "json",
        ".sh": "bash",
        ".yml": "yaml",
        ".yaml": "yaml",
        ".rs": "rust",
        ".go": "go",
    }.get(Path(path).suffix, "text")
