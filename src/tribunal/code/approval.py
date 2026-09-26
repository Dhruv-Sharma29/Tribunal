"""Who is allowed to change what, and who gets asked first.

Three modes, and the honest description of each:

* **`ask`** (the default) -- read-only tools run unprompted; `write`, `edit`, `bash` and
  `review` stop and ask. "Always" is remembered for the rest of the session, scoped to the
  tool and, for `bash`, to the *program* rather than the whole shell: approving `pytest`
  once does not approve `curl` later.
* **`plan`** -- every mutating tool is refused, and the refusal is handed to the model as an
  observation, so it keeps working and explains the change instead of making it. This is the
  mode for "what would you do here", and it is the default for `--print`, because a
  non-interactive run has nobody to answer a prompt.
* **`auto`** (`--yes`) -- nothing is asked. For a run you are watching, or a container you
  are willing to lose.

## What this is not

It is not a security boundary, and the one place it looks like one is the denylist below.
That list catches the catastrophic *typo* -- the `rm -rf /` that was meant to be scoped, the
`dd` with the wrong `of=`. It cannot stop a model that is trying to do harm, because `bash`
is a shell and every denylist over a shell is one `$(echo cm0K | base64 -d)` from useless.
The real boundaries in this session are the workspace root check in `tools.py`, which is
structural, and a human reading the command in the prompt. Anyone relying on more than that
should be running `tribunal code` in a container.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from tribunal.code.actions import MUTATING, Step, ToolName


class ApprovalMode(StrEnum):
    ASK = "ask"
    PLAN = "plan"
    AUTO = "auto"


@dataclass(frozen=True)
class Approval:
    """The answer, plus the sentence the model is shown when the answer is no."""

    allowed: bool
    reason: str = ""


#: Commands refused in every mode, `auto` included. See the module docstring: these are
#: typos, not threats. Each one is a command whose *intended* form is almost always narrower.
CATASTROPHIC = (
    re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\s+(-[a-zA-Z]+\s+)*(/|~)\s*$"),
    re.compile(r"\bmkfs(\.\w+)?\b"),
    re.compile(r"\bdd\b[^|;]*\bof=/dev/(sd|nvme|hd|disk)"),
    re.compile(r":\(\)\s*\{.*\};\s*:"),  # the fork bomb
    re.compile(r"\bchmod\s+(-[a-zA-Z]+\s+)*777\s+/\s*$"),
    re.compile(r"\bshutdown\b|\breboot\b|\bhalt\b"),
)

#: Words that make a `bash` approval prompt worth reading twice. Not refused -- shown.
NOTABLE = ("curl", "wget", "ssh", "scp", "git push", "npm publish", "pip install", "sudo")

#: What a prompter returns: `yes`, `always`, `never`, `no`, or `no: <what to do instead>`.
#: That last shape is the one worth having -- a bare refusal makes the agent guess why, and
#: guessing costs rounds. A plain callable rather than a protocol class: the terminal
#: supplies one, the tests supply a lambda, and nothing else ever will.
Prompter = Callable[[Step, str], str]


@dataclass
class Approver:
    mode: ApprovalMode = ApprovalMode.ASK
    #: None in `plan` and `auto`, where nothing is ever asked.
    prompter: Prompter | None = None
    remembered: set[str] = field(default_factory=set)
    refused: set[str] = field(default_factory=set)
    #: Every decision, in order, so `/cost` and the end-of-session summary can say what was
    #: actually approved rather than what was requested.
    log: list[tuple[str, bool]] = field(default_factory=list)

    def check(self, step: Step) -> Approval:
        if step.tool not in MUTATING:
            return Approval(True)

        blocked = _catastrophic(step)
        if blocked is not None:
            self.log.append((self.key(step), False))
            return Approval(
                False,
                f"refused outright: {blocked}. This is blocked in every mode, including "
                "--yes. If you genuinely need it, run it yourself.",
            )

        key = self.key(step)
        if self.mode is ApprovalMode.AUTO or key in self.remembered:
            self.log.append((key, True))
            return Approval(True)
        if self.mode is ApprovalMode.PLAN:
            self.log.append((key, False))
            return Approval(
                False,
                "this session is in plan mode, so nothing is changed or executed. Carry on "
                "reading and reasoning, and use `done` to describe the change you would "
                "make -- including the exact edits -- so the user can apply it.",
            )
        if key in self.refused:
            self.log.append((key, False))
            return Approval(
                False, f"the user already refused {key} in this session; do not ask again."
            )

        answer = (self.prompter(step, key) if self.prompter else "no").strip()
        lowered = answer.lower()
        allowed = lowered.startswith(("y", "a"))
        if lowered.startswith("a"):
            self.remembered.add(key)
        if lowered.startswith("never"):
            self.refused.add(key)
        self.log.append((key, allowed))
        if allowed:
            return Approval(True)

        direction = answer.split(":", 1)[1].strip() if ":" in answer else ""
        reason = (
            "the user declined this action. Do not retry it; either find another way or "
            "use `done` to explain what you need."
        )
        if direction:
            # Passed through verbatim, like a `post_validate` message in `llm/client.py`:
            # it is written for the model to act on, and paraphrasing it here would be this
            # module inventing an instruction the user did not give.
            reason += f" They said what to do instead: {direction}"
        return Approval(False, reason)

    @staticmethod
    def key(step: Step) -> str:
        """What an "always" answer is remembered against.

        For `bash` that is the program name, not the command line: remembering the whole
        line would be useless (no two `pytest` invocations match) and remembering just
        `bash` would be dangerous (one approved `ls` would approve every later command).
        The program is the unit a user actually has an opinion about.
        """
        if step.tool is not ToolName.BASH:
            return step.tool.value
        try:
            parts = shlex.split(step.command)
        except ValueError:  # unbalanced quotes; the shell will complain soon enough
            parts = step.command.split()
        program = next((p for p in parts if "=" not in p), "") if parts else ""
        return f"bash:{program or '?'}"

    def notes(self, step: Step, root: Path | None = None) -> list[str]:
        """Things the prompt should say out loud before the user types `y`.

        `root` is how the write note tells "creates a file" from "replaces one": without it
        the prompt warned about an overwrite for every `write`, including new files. A
        warning that fires when nothing is at stake is the fastest way to teach someone to
        stop reading warnings.
        """
        out: list[str] = []
        if step.tool is ToolName.BASH:
            lowered = step.command.lower()
            out += [f"this command runs `{word}`" for word in NOTABLE if word in lowered]
        if step.tool is ToolName.WRITE and root is not None and (root / step.path).is_file():
            out.append("this replaces the whole file — its current contents are lost")
        if step.tool is ToolName.REVIEW:
            out.append("the full tribunal makes several model calls and is the priciest tool")
        return out


def _catastrophic(step: Step) -> str | None:
    if step.tool is not ToolName.BASH:
        return None
    for pattern in CATASTROPHIC:
        match = pattern.search(step.command)
        if match:
            return match.group(0).strip()
    return None
