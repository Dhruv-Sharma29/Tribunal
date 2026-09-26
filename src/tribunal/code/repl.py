"""The terminal session: input, live step rendering, approval dialogs, slash commands.

Everything here is presentation and input. No decision that affects a run lives in this
module -- the loop is `session.py`, permission is `approval.py`, and the look is `ui.py` --
so a different front end drives the same session object with none of this. The split is why
`ui.py` can be unit-tested without a terminal and why this file is mostly wiring.

Three choices worth explaining.

**`input()` with `readline`, not a raw-mode TUI.** Importing `readline` gives line editing,
history and reverse search from the user's own configuration for the cost of one import. A
drawn input box would need raw mode, which takes all of that away -- and scrollback with it,
which is where the output of the command you just approved lives.

**Model-authored text is printed as `Text`, never as markup.** Every string that came from
the model reaches the console wrapped, because Rich reads `[dim]` in a string as a style
tag. A reply mentioning `list[int]` would otherwise render as an unclosed tag at best and
raise mid-session at worst, and "the agent crashed while telling you what it did" is a bad
way to lose a turn.

**Results are summarised; failures are not.** The model gets the whole observation. The
terminal gets one line, plus the first lines of the text when the step *failed*, because
that is the moment a person actually wants to read it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path

from rich.console import Console
from rich.text import Text

from tribunal import __version__
from tribunal.code import ui
from tribunal.code.actions import Step, ToolName
from tribunal.code.approval import ApprovalMode, Approver
from tribunal.code.session import Answer, CodeSession, Hooks
from tribunal.code.tools import Observation, ToolContext, execute

HISTORY_FILE = Path.home() / ".tribunal" / "code-history"

COMMANDS = {
    "/help": "this",
    "/exit": "end the session (Ctrl-D too)",
    "/clear": "forget the conversation; the files stay as they are",
    "/cost": "what this session has spent, and what was approved",
    "/mode": "show or set approval: ask | plan | auto",
    "/undo": "revert the most recent write or edit",
    "/review <file>": "run the full tribunal on a file, outside the agent loop",
    "/diff": "what has changed in this session",
    "/model": "the model, provider and effort behind this session",
}

TOOLS = {
    "read list grep glob": "look around; these never ask",
    "write edit": "change a file; shown as a diff before it happens",
    "bash": "run a command here, with your environment",
    "review": "hand a file to the whole tribunal — two critics and the decision table",
}


class Repl:
    """One interactive session at a terminal."""

    def __init__(self, session: CodeSession, console: Console | None = None) -> None:
        self.session = session
        # Pushed rather than required: the CLI hands us its own plain `Console`, and a
        # front end that did not know about `ui.THEME` would otherwise die on the first
        # styled string it printed.
        self.console = console or ui.console()
        self.console.push_theme(ui.THEME)
        self.session.hooks = Hooks(
            on_thinking=self._thinking,
            on_step=self._render_step,
            on_observation=self._render_observation,
            on_note=lambda text: self.console.print(Text(text, style="t.dim")),
        )
        self.session.approver.prompter = self._ask_approval

    # -- entry point --------------------------------------------------------------------

    def run(self, first: str | None = None) -> int:
        """Loop until the user leaves. Returns the process exit code."""
        self.console.print(
            ui.banner(
                __version__,
                f"{self.session.settings.agents.code.resolved_provider().value}/"
                f"{self.session.settings.agents.code.model}",
                self.session.approver.mode.value,
                self.session.workspace.root,
            )
        )
        self._load_history()
        pending = first
        try:
            while True:
                if pending is None:
                    try:
                        line = input(f"\n{ui.prompt_text()}").strip()
                    except EOFError:
                        self.console.print()
                        break
                    except KeyboardInterrupt:
                        # At the prompt, Ctrl-C clears the line rather than ending the
                        # session -- leaving is `/exit` or Ctrl-D, which cannot happen while
                        # reaching for Ctrl-C to stop a runaway turn.
                        self.console.print(Text("(Ctrl-D or /exit to leave)", style="t.dim"))
                        continue
                else:
                    self.console.print(f"\n{ui.prompt_text()}{pending}")
                    line, pending = pending, None

                if not line:
                    continue
                if line.startswith("/"):
                    if self._command(line):
                        break
                    continue
                self._turn(line)
        finally:
            self._save_history()
            self._farewell()
        return 0

    def _turn(self, request: str) -> None:
        started = time.monotonic()
        try:
            answer = asyncio.run(self.session.ask(request))
        except KeyboardInterrupt:
            self.console.print(
                Text("\ninterrupted — the steps so far are kept. What next?", style="t.warn")
            )
            return
        self._render_answer(answer, time.monotonic() - started)

    # -- rendering ----------------------------------------------------------------------

    def _thinking(self, number: int, cost: float) -> ui.Thinking:
        return ui.Thinking(self.console, number, cost)

    def _render_step(self, step: Step) -> None:
        if step.tool is ToolName.DONE:
            return
        self.console.print()
        if step.thought:
            self.console.print(ui.thought_line(step.thought))
        self.console.print(ui.action_line(step))

    def _render_observation(self, step: Step, observation: Observation) -> None:
        if observation.data:
            self.console.print(ui.verdict_panel(observation.data))
            return
        self.console.print(ui.result_line(observation))
        if not observation.ok:
            self.console.print(ui.failure_excerpt(observation))

    def _render_answer(self, answer: Answer, seconds: float = 0.0) -> None:
        self.console.print()
        if answer.message:
            self.console.print(ui.reply(answer.message))
        if answer.stopped_by:
            self.console.print(Text(f"  stopped: {answer.stopped_by}", style="t.warn"))
        self.console.print(
            ui.status_line(
                len(answer.turns), self.session.client.calls, self.session.cost_usd, seconds
            )
        )

    # -- approval -----------------------------------------------------------------------

    def _ask_approval(self, step: Step, key: str) -> str:
        """Show the effect, then ask. Returns yes / always / no, with optional direction.

        The third answer is the one worth having: a refusal that carries a sentence of
        direction saves the rounds the agent would otherwise spend guessing why it was
        declined. That sentence is appended to the refusal the model sees.
        """
        self.console.print()
        self.console.print(
            ui.approval_panel(
                step,
                key,
                self.session.approver.notes(step, self.session.workspace.root),
                self._effect(step),
            )
        )
        try:
            answer = input(f"  {ui.prompt_text()}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            self.console.print(Text("  no", style="t.warn"))
            return "no"

        self.console.print()
        if answer in {"1", "y", "yes", ""}:
            return "yes"
        if answer in {"2", "a", "always"}:
            return "always"
        if answer in {"never"}:
            return "never"
        # Anything else is a refusal, and anything longer than "3" or "n" is direction the
        # user typed instead of picking an option. Both reach the model.
        if answer not in {"3", "n", "no"}:
            return f"no: {answer}"
        try:
            note = input("  what should it do instead? (enter to skip) ").strip()
        except (EOFError, KeyboardInterrupt):
            note = ""
        return f"no: {note}" if note else "no"

    def _effect(self, step: Step):
        """What this step will actually do, in the form a person can judge."""
        if step.tool is ToolName.BASH:
            return Text(step.command)
        if step.tool is ToolName.WRITE:
            return ui.write_preview(step)
        if step.tool is ToolName.EDIT:
            path = self.session.workspace.root / step.path
            current = (
                path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
            )
            return ui.edit_diff(step, current)
        return Text(step.describe())

    # -- slash commands -----------------------------------------------------------------

    def _command(self, line: str) -> bool:
        """Handle a slash command. Returns True when the session should end."""
        name, _, argument = line.partition(" ")
        argument = argument.strip()

        if name in {"/exit", "/quit"}:
            return True
        if name == "/help":
            self.console.print(ui.help_panel(COMMANDS, TOOLS))
        elif name == "/clear":
            self.session.entries.clear()
            self.console.print(
                Text("  conversation cleared; the working tree is untouched", style="t.dim")
            )
        elif name == "/cost":
            self._cost()
        elif name == "/mode":
            self._mode(argument)
        elif name == "/undo":
            restored = self.session.workspace.undo_last()
            self.console.print(Text(f"  {restored or 'nothing to undo'}", style="t.dim"))
        elif name == "/diff":
            self._diff()
        elif name == "/review":
            self._review(argument)
        elif name == "/model":
            config = self.session.settings.agents.code
            self.console.print(
                Text(
                    f"  {config.resolved_provider().value}/{config.model}  "
                    f"effort={config.effort}  max_tokens={config.max_tokens}",
                    style="t.dim",
                )
            )
        else:
            self.console.print(Text(f"  unknown command {name} — try /help", style="t.warn"))
        return False

    def _cost(self) -> None:
        self.console.print(
            ui.status_line(
                self.session.steps_taken, self.session.client.calls, self.session.cost_usd
            )
        )
        approved = sorted({key for key, allowed in self.session.approver.log if allowed})
        if approved:
            self.console.print(Text(f"  approved: {', '.join(approved)}", style="t.dim"))

    def _mode(self, argument: str) -> None:
        if not argument:
            self.console.print(
                Text(f"  mode is {self.session.approver.mode.value}", style="t.dim")
            )
            return
        try:
            self.session.approver.mode = ApprovalMode(argument)
        except ValueError:
            self.console.print(Text("  mode is one of: ask, plan, auto", style="t.warn"))
            return
        self.console.print(
            Text(f"  mode is now {self.session.approver.mode.value}", style="t.dim")
        )

    def _diff(self) -> None:
        """Every file this session changed, as a diff against how it started.

        Against the *first* snapshot of each file, not the previous one: what a reader wants
        at the end of a session is the net change, not a replay of the edits.
        """
        from tribunal.patch import make_unified_diff

        first: dict[Path, str | None] = {}
        for snapshot in self.session.workspace.snapshots:
            first.setdefault(snapshot.path, snapshot.before)
        if not first:
            self.console.print(Text("  nothing changed in this session", style="t.dim"))
            return
        for path, before in first.items():
            now = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
            diff = make_unified_diff(before or "", now, self.session.workspace.display(path))
            self.console.print(ui.diff_text(diff) if diff.strip() else Text())

    def _review(self, argument: str) -> None:
        if not argument:
            self.console.print(Text("  usage: /review <file.py>", style="t.warn"))
            return
        step = Step(tool=ToolName.REVIEW, path=argument, thought="requested by the user")
        context = ToolContext(
            self.session.workspace, self.session.settings, self.session.client
        )
        with ui.Thinking(self.console, self.session.steps_taken + 1, self.session.cost_usd):
            observation = asyncio.run(execute(step, context))
        self._render_observation(step, observation)

    # -- history ------------------------------------------------------------------------

    def _load_history(self) -> None:
        with contextlib.suppress(ImportError, OSError):
            import readline

            HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            if HISTORY_FILE.is_file():
                readline.read_history_file(HISTORY_FILE)
            readline.set_history_length(1000)

    def _save_history(self) -> None:
        with contextlib.suppress(ImportError, OSError):
            import readline

            HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            readline.write_history_file(HISTORY_FILE)
            os.chmod(HISTORY_FILE, 0o600)

    def _farewell(self) -> None:
        parts = []
        if self.session.cost_usd:
            parts.append(f"${self.session.cost_usd:.4f}")
        changed = sorted(
            {self.session.workspace.display(s.path) for s in self.session.workspace.snapshots}
        )
        if changed:
            parts.append(f"changed: {', '.join(changed)}")
        if parts:
            self.console.print(Text("  " + "  ·  ".join(parts), style="t.dim"))


def build_approver(mode: ApprovalMode) -> Approver:
    return Approver(mode=mode)
