"""The loop: prompt in, tool calls out, until the agent says it is done.

One turn is: render the transcript, ask for a `Step`, check it against the approver, execute
it, append the result. Repeat until the model emits `done` or a budget stops it. That is the
whole control flow, and keeping it that small is deliberate -- every interesting decision in
this package is somewhere the loop can point at (`actions.py` for the schema, `approval.py`
for permission, `tools.py` for effects) rather than inside the loop itself.

## The transcript is the memory

There is no message history on the wire: `LLMRequest` is a system prefix and one user block
(see `actions.py` for why that is not being changed). So the session keeps the transcript
itself and renders it into the user block each turn, which has two consequences worth naming.

The good one: what the user watched scroll past and what the model is shown next turn are
generated from the same objects, so a session cannot be explained two different ways.

The awkward one: the transcript grows, and a long session would eventually exceed the
context. `_render_transcript` therefore elides from the *middle* once it passes
`code.max_history_chars`, keeping the original request and the recent steps -- the two ends
a recovery actually needs. It says so in the text it emits, because a model that cannot tell
it has forgotten something will confidently re-derive it.

## The system prefix stays cacheable

`Agent._check_prefix_is_stable` runs at construction, and it is doing real work here rather
than guarding a theoretical mistake. The obvious implementation puts the working directory,
the file tree and the step counter in the system prompt, and every one of those would
invalidate the prompt cache on the turn it changed -- which on this surface is most turns,
because the tree changes whenever the agent writes a file. They all live in the user block.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tribunal.agents.base import Agent, AgentRun
from tribunal.code.actions import Step, ToolName
from tribunal.code.approval import Approval, ApprovalMode, Approver
from tribunal.code.tools import Observation, ToolContext, Workspace, execute
from tribunal.config import Settings
from tribunal.llm.base import ProviderError
from tribunal.llm.client import LLMClient, SchemaRepairFailed


class CodeAgent(Agent[Step]):
    """The `code` seat. A versioned prompt and one output schema, like every other agent."""

    role = "code"
    output_model = Step

    def render_user(self, bundle: SessionContext) -> str:
        return bundle.render()


@dataclass
class SessionContext:
    """The volatile half of the request: where we are, what was asked, what has happened."""

    root: Path
    mode: ApprovalMode
    tree: str
    request: str
    transcript: str
    step_number: int
    max_steps: int

    def render(self) -> str:
        remaining = self.max_steps - self.step_number + 1
        budget = (
            f"This is step {self.step_number} of at most {self.max_steps}. "
            f"{remaining} step(s) remain; if you run out, the turn ends with whatever you "
            "have said, so emit `done` while you still can."
        )
        return (
            f"<workspace root=\"{self.root}\" mode=\"{self.mode.value}\">\n"
            f"{self.tree}\n"
            "</workspace>\n\n"
            f"<request>\n{self.request}\n</request>\n\n"
            f"<transcript>\n{self.transcript or '(nothing yet -- this is the first step)'}\n"
            "</transcript>\n\n"
            f"{budget}\n"
            "Emit the next step."
        )


@dataclass
class Turn:
    """One executed step and what came back."""

    step: Step
    observation: Observation
    approval: Approval
    cost_usd: float
    duration_ms: int


@dataclass
class Answer:
    """The result of one user prompt: the agent's reply and everything it did to get there."""

    message: str
    turns: list[Turn]
    cost_usd: float
    #: `None` when the agent finished on its own; otherwise the limit that stopped it.
    stopped_by: str | None = None

    @property
    def complete(self) -> bool:
        return self.stopped_by is None


@dataclass
class Hooks:
    """Where the terminal plugs in. All optional; the tests pass none.

    `on_thinking` brackets the model call rather than reporting it afterwards. A terminal
    blocked with no output is indistinguishable from a hung one, and the loop is the only
    thing that knows a call has started.
    """

    on_thinking: Callable[[int, float], AbstractContextManager] | None = None
    on_step: Callable[[Step], None] | None = None
    on_observation: Callable[[Step, Observation], None] | None = None
    on_note: Callable[[str], None] | None = None

    def thinking(self, number: int, cost: float) -> AbstractContextManager:
        if self.on_thinking:
            return self.on_thinking(number, cost)
        return contextlib.nullcontext()

    def step(self, step: Step) -> None:
        if self.on_step:
            self.on_step(step)

    def observation(self, step: Step, observation: Observation) -> None:
        if self.on_observation:
            self.on_observation(step, observation)

    def note(self, text: str) -> None:
        if self.on_note:
            self.on_note(text)


class SessionLimit(RuntimeError):
    """A budget stopped the turn. Carries the name so the caller can say which."""

    def __init__(self, name: str, detail: str) -> None:
        super().__init__(detail)
        self.name = name


@dataclass
class CodeSession:
    """One conversation against one workspace."""

    settings: Settings
    workspace: Workspace
    client: LLMClient = None  # type: ignore[assignment]
    approver: Approver = field(default_factory=Approver)
    hooks: Hooks = field(default_factory=Hooks)
    #: Appended as JSON lines, one per step, when set. Not a `trace/` trace: those describe a
    #: review and are replayable into a report, and a coding session is neither.
    log_path: Path | None = None

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = LLMClient(self.settings)
        self.agent = CodeAgent(self.settings, self.client)
        self.entries: list[tuple[str, Any]] = []
        self._cost_at_start = self.client.total_cost_usd

    # -- accounting ---------------------------------------------------------------------

    @property
    def cost_usd(self) -> float:
        return self.client.total_cost_usd - self._cost_at_start

    @property
    def steps_taken(self) -> int:
        return sum(1 for kind, _ in self.entries if kind == "turn")

    # -- the loop -----------------------------------------------------------------------

    async def ask(self, request: str) -> Answer:
        """Run one user prompt to completion, or to a budget."""
        self.entries.append(("user", request))
        tree = self._tree()
        turns: list[Turn] = []
        cost_before = self.cost_usd
        stopped: str | None = None
        message = ""

        for number in range(1, self.settings.code.max_steps + 1):
            try:
                self._check_budget()
            except SessionLimit as limit:
                stopped, message = limit.name, str(limit)
                break

            context = SessionContext(
                root=self.workspace.root,
                mode=self.approver.mode,
                tree=tree,
                request=request,
                transcript=self._render_transcript(),
                step_number=number,
                max_steps=self.settings.code.max_steps,
            )
            try:
                with self.hooks.thinking(number, self.cost_usd):
                    run = await self.agent.run(context, round_=1)
            except SchemaRepairFailed as exc:
                stopped, message = "schema", f"the model could not emit a usable step: {exc}"
                break
            except ProviderError as exc:
                stopped, message = "provider", f"the provider failed: {exc}"
                break

            step: Step = run.value
            self.hooks.step(step)

            if step.tool is ToolName.DONE:
                self.entries.append(("turn", Turn(step, _finished(), Approval(True),
                                                  run.cost_usd, run.duration_ms)))
                self._log(step, _finished(), run)
                message = step.message
                break

            approval = self.approver.check(step)
            if approval.allowed:
                started = time.monotonic()
                observation = await execute(
                    step, ToolContext(self.workspace, self.settings, self.client)
                )
                duration = int((time.monotonic() - started) * 1000)
            else:
                observation = Observation(False, approval.reason, "declined")
                duration = 0

            turn = Turn(step, observation, approval, run.cost_usd, duration)
            turns.append(turn)
            self.entries.append(("turn", turn))
            self.hooks.observation(step, observation)
            self._log(step, observation, run)
        else:
            stopped = "steps"
            message = (
                f"stopped after {self.settings.code.max_steps} steps without finishing. "
                "The work so far is in the transcript; say what to do next, or raise the "
                "limit with --max-steps."
            )

        return Answer(
            message=message,
            turns=turns,
            cost_usd=self.cost_usd - cost_before,
            stopped_by=stopped,
        )

    def _check_budget(self) -> None:
        cap = self.settings.code.max_usd
        if self.cost_usd >= cap:
            raise SessionLimit(
                "cost",
                f"the session has spent ${self.cost_usd:.4f}, at or over its ${cap:.2f} cap. "
                "Raise it with --max-usd, or start a new session.",
            )

    # -- rendering ----------------------------------------------------------------------

    def _render_transcript(self) -> str:
        """The whole session as text, elided from the middle if it is too long."""
        blocks: list[str] = []
        for kind, entry in self.entries:
            if kind == "user":
                blocks.append(f"[the user said]\n{entry}")
                continue
            turn: Turn = entry
            body = turn.observation.text
            cap = self.settings.code.max_output_chars
            if len(body) > cap:
                body = body[:cap] + f"\n[… {len(body) - cap} characters elided …]"
            status = "ok" if turn.observation.ok else "FAILED"
            blocks.append(
                f"[step] {turn.step.describe()}\n"
                f"thought: {turn.step.thought}\n"
                f"result ({status}):\n{body}"
            )

        limit = self.settings.code.max_history_chars
        joined = "\n\n".join(blocks)
        if len(joined) <= limit:
            return joined

        # Keep the first block -- almost always the original request -- and as many recent
        # blocks as fit. Dropping the oldest first would lose the task itself, which is the
        # one thing a recovery cannot re-derive.
        head, rest = blocks[0], blocks[1:]
        kept: list[str] = []
        size = len(head)
        for block in reversed(rest):
            if size + len(block) > limit:
                break
            kept.insert(0, block)
            size += len(block)
        dropped = len(rest) - len(kept)
        marker = f"[… {dropped} earlier step(s) elided to fit the context …]"
        return "\n\n".join([head, marker, *kept])

    def _tree(self) -> str:
        """A shallow listing of the workspace, for orientation only.

        Deliberately not the whole tree: on any real repository that is thousands of lines
        of context spent on directories the agent will never open. `glob` and `grep` are how
        it finds things; this is how it knows what kind of project it is in.
        """
        ignore = set(self.settings.code.ignore_dirs)
        rows: list[str] = []
        try:
            children = sorted(
                self.workspace.root.iterdir(), key=lambda p: (p.is_file(), p.name)
            )
        except OSError as exc:  # pragma: no cover - the root was checked at construction
            return f"(cannot list the workspace: {exc})"
        for child in children:
            if child.name.startswith(".") or child.name in ignore:
                continue
            if child.is_dir():
                try:
                    inner = [
                        p.name
                        for p in sorted(child.iterdir())
                        if not p.name.startswith(".") and p.name not in ignore
                    ]
                except OSError:
                    inner = []
                preview = ", ".join(inner[:8]) + ("…" if len(inner) > 8 else "")
                rows.append(f"  {child.name}/  ({preview})" if preview else f"  {child.name}/")
            else:
                rows.append(f"  {child.name}")
            if len(rows) >= 60:
                rows.append("  …")
                break
        return "\n".join(rows) or "  (empty)"

    # -- session log --------------------------------------------------------------------

    def _log(self, step: Step, observation: Observation, run: AgentRun | None = None) -> None:
        """One JSON line per step.

        `parse_retries` and `structure` are in here for the same reason `AgentRun` carries
        them everywhere else: docs/09-roadmap.md makes the schema-parse retry rate the
        prompt-health signal, and it cannot be recovered after the fact. The first version
        of this method logged the cost and threw the rest away, which would have left the
        one measurable question about this surface — can a given model actually fill the
        flat step schema — unanswerable from a session log.
        """
        if self.log_path is None:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "step": step.model_dump(mode="json"),
            "ok": observation.ok,
            "summary": observation.summary,
            "observation": observation.text,
            "cost_usd": round(run.cost_usd if run else 0.0, 6),
            "parse_retries": run.outcome.repair_retries if run else None,
            "local_repairs": run.outcome.local_repairs if run else [],
            "structure": run.response.structure.value if run else None,
            "duration_ms": run.duration_ms if run else None,
        }
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _finished() -> Observation:
    return Observation(True, "the agent reported the turn finished", "done")
