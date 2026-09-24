"""Live CLI progress: the second renderer over the one event stream.

docs/06-observability.md principle 4 is the whole design: "Structured, never `print`. ...
Human-facing CLI progress is a separate renderer over the same event stream — not a second
code path." So nothing here is called by the orchestrator. This is a `TraceWriter` subscriber,
it sees exactly the events the JSONL file gets, and if it were deleted the trace would be
byte-identical.

That constraint is worth more than it looks. A progress display built from `print` calls
sprinkled through the orchestrator drifts from the trace the moment someone adds a step and
forgets one of the two — and then the terminal and the file disagree about what happened.
Here they cannot: a step that shows up in the terminal is a step that was recorded, because
they are the same event.

## Why a run needs this at all

Runs take minutes (docs/10). Four minutes of silence reads as a hang, and the thing a watcher
most wants to see — the two critics running *at the same time* — is invisible unless something
draws it. Rendering both on one line with a `parallel` marker communicates the concurrency for
free, which is the shape docs/06 § CLI progress rendering asks for:

    ● grounding      bandit 2 · ruff 5 · radon 1                        1.2s
    ● round 1/3
      ├ coder        1 hunk · addresses SEC-a31f                       24.1s
      ├ validate     applied · parses                                   0.4s
      ├ critique     redteam BLOCK(1 high) │ profiler CONCERNS(1 med)  31.8s  <- parallel
      ├ policy       REJECT · hard_block_security · pressure 21.6         0ms
      └ arbiter      consolidated · dismissed 1                        18.9s

## Two outputs, one renderer

On a terminal this is a `rich.Live` that rewrites itself in place. Redirected to a file or a
CI log it would be thousands of frames of ANSI, so the same state machine instead prints each
step once as it completes. Chosen on `console.is_terminal` rather than on a flag, because the
right behaviour is not something a user should have to know to ask for.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.text import Text

from tribunal.contracts import TraceEvent

#: Width of the step-name column, so the details line up down the page.
LABEL_WIDTH = 10

#: States that are a step a watcher cares about. `INIT` and the terminal states are
#: bookkeeping -- showing them would pad the display with rows that never say anything.
STEP_FOR_STATE = {
    "GROUND": "grounding",
    "PROPOSE": "coder",
    "VALIDATE": "validate",
    "CRITIQUE": "critique",
    "ARBITRATE": "policy",
    "POSTMORTEM": "write-up",
}

DECISION_STYLE = {
    "accept": "green",
    "reject": "red",
    "tradeoff": "yellow",
    "escalate": "red",
}

VERDICT_SHORT = {"block": "BLOCK", "concerns": "CONCERNS", "clean": "clean"}


def _ms(value: float | None) -> str:
    if value is None:
        return ""
    if value < 1000:
        return f"{int(value)}ms"
    return f"{value / 1000:.1f}s"


@dataclass
class Step:
    """One line. `detail` accumulates as the events that describe it arrive."""

    label: str
    round: int | None
    detail: str = ""
    duration_ms: int | None = None
    done: bool = False
    note: str = ""
    style: str = ""

    def render(self, last_in_round: bool) -> Text:
        prefix = "● " if self.round is None else ("  └ " if last_in_round else "  ├ ")
        line = Text(prefix, style="dim")
        line.append(self.label.ljust(LABEL_WIDTH), style="bold" if self.done else "dim")
        line.append(" ")
        line.append(self.detail or ("…" if not self.done else ""), style=self.style or "")
        # The duration is right-of-detail rather than right-aligned to the terminal: a
        # narrow window would otherwise push it onto its own line and break the column.
        if self.duration_ms is not None:
            line.append(f"  {_ms(self.duration_ms)}", style="dim")
        if self.note:
            line.append(f"  {self.note}", style="cyan")
        # A round header has no detail and no duration, so the padded label would leave a
        # trailing run of spaces on the line.
        line.rstrip()
        return line


@dataclass
class ProgressRenderer:
    """A `TraceWriter` subscriber that draws the run as it happens.

    Stateful and single-threaded: the writer calls it inline from the orchestrator's own
    task, so there is no ordering to reason about beyond the order events are emitted in.
    """

    console: Console
    max_rounds: int = 3
    steps: list[Step] = field(default_factory=list)
    _live: Live | None = None
    _printed: int = 0
    #: Set once the run ends, so the final frame can say how it went rather than leaving the
    #: last step looking like it is still running.
    _summary: Text | None = None

    # -- lifecycle ------------------------------------------------------------------------

    def __enter__(self) -> ProgressRenderer:
        if self.console.is_terminal:
            self._live = Live(
                self._renderable(),
                console=self.console,
                # Four refreshes a second is enough for a run measured in minutes, and low
                # enough not to fight the event loop for the terminal.
                refresh_per_second=4,
                transient=False,
            )
            self._live.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._live is not None:
            self._live.update(self._renderable())
            self._live.__exit__(*exc)
            self._live = None
            return
        # The streaming path holds the last row back until it knows whether another follows
        # in its round; nothing follows now, so flush it and then the summary.
        self._flush_from(len(self.steps) - 1)
        if self._summary is not None:
            self.console.print(self._summary)

    # -- the subscriber ---------------------------------------------------------------------

    def __call__(self, event: TraceEvent) -> None:
        handler = getattr(self, f"_on_{event.kind}", None)
        if handler is not None:
            handler(event)
        self._refresh()

    # -- events ---------------------------------------------------------------------------
    #
    # Steps are created by the events that *describe* them -- the Coder's response, the
    # patch validation, the policy decision -- and never by state transitions. A state
    # event's `round` is the round in progress when the transition was emitted, not the
    # round being entered: GROUND -> PROPOSE for round 1 carries `round=None`, and the
    # REJECT edge into round 2 carries `round=1`. Both are correct for a trace, and both
    # are wrong for a display keyed on them. Content events carry the round they belong to.
    #
    # State exits are still used, but only for their durations, matched to the most recent
    # step of that name rather than by round.

    def _on_tool_run(self, event: TraceEvent) -> None:
        label = "grounding" if event.round is None else "re-ground"
        step = self._step(label, event.round)
        tool = event.payload.get("tool", event.actor)
        if event.payload.get("error"):
            step.detail = _join(step.detail, f"{tool} failed")
            step.style = "yellow"
        else:
            step.detail = _join(
                step.detail, f"{tool} {event.payload.get('findings_count', 0)}"
            )
        step.done = True
        step.duration_ms = (step.duration_ms or 0) + (event.duration_ms or 0)

    def _on_patch_validate(self, event: TraceEvent) -> None:
        step = self._step("validate", event.round)
        payload = event.payload
        attempt = payload.get("attempt", 1)
        if payload.get("applied") and payload.get("parse_ok"):
            # The attempt count stays visible when it is not 1. A re-anchoring reuses this
            # row rather than stacking a new one -- a diff stumble is not a debate round --
            # but a patch that took three goes should not read as one that took one.
            prefix = "" if attempt == 1 else f"{attempt} attempts · "
            step.detail = f"{prefix}applied · parses · {payload.get('hunks', 0)} hunk(s)"
            step.style = ""
        else:
            # The mechanical bounce, which is the cheap half of the loop and worth seeing:
            # a watcher should be able to tell a re-anchoring from a debate round.
            step.detail = _one_line(
                f"attempt {attempt} bounced: "
                f"{payload.get('failure_reason') or 'did not apply'}"
            )
            step.style = "yellow"
        step.done = True

    def _on_llm_response(self, event: TraceEvent) -> None:
        parsed = event.payload.get("parsed") or {}
        handler = {
            "coder": self._coder,
            "redteam": self._critic,
            "profiler": self._critic,
            "arbiter": self._arbiter,
            "arbiter_affirm": self._affirm,
            "postmortem": self._postmortem,
        }.get(event.actor)
        if handler is not None:
            handler(event, parsed)

    def _coder(self, event: TraceEvent, parsed: dict) -> None:
        step = self._step("coder", event.round)
        addresses = parsed.get("addresses") or []
        bits = [f"{len(parsed.get('edits') or [])} edit(s)"]
        if addresses:
            bits.append("addresses " + ", ".join(addresses[:2]))
        if parsed.get("deliberately_unaddressed"):
            bits.append(f"pushed back on {len(parsed['deliberately_unaddressed'])}")
        # Re-anchoring attempts reuse the row: a diff stumble is not a debate round, and
        # stacking them would make a cheap retry look like expensive progress.
        step.detail = " · ".join(bits)
        step.done = True
        step.duration_ms = (step.duration_ms or 0) + (event.duration_ms or 0)

    def _critic(self, event: TraceEvent, parsed: dict) -> None:
        """Both critics land on one line, which is what makes the parallelism visible."""
        step = self._step("critique", event.round)
        verdict = VERDICT_SHORT.get(parsed.get("verdict", ""), parsed.get("verdict", "?"))
        issues = parsed.get("issues") or []
        count = f"({len(issues)} {_worst_severity(issues)})" if issues else ""
        step.detail = _join(step.detail, f"{event.actor} {verdict}{count}", " │ ")
        step.note = "⟵ parallel"
        step.done = True

    def _arbiter(self, event: TraceEvent, parsed: dict) -> None:
        step = self._step("arbiter", event.round)
        bits = []
        if parsed.get("synthesised_by") == "template":
            bits.append("templated")
        if parsed.get("consolidated_critique"):
            bits.append("consolidated")
        if parsed.get("tradeoff_justification"):
            bits.append("justified the trade-off")
        if parsed.get("dismissed"):
            bits.append(f"dismissed {len(parsed['dismissed'])}")
        step.detail = " · ".join(bits) or "nothing to consolidate"
        step.done = True
        step.duration_ms = event.duration_ms

    def _affirm(self, event: TraceEvent, parsed: dict) -> None:
        # Its own row rather than folded into the policy line: it is an input to the
        # decision and it costs a call, so hiding it would understate both.
        step = self._step("affirm", event.round)
        step.detail = _join(
            step.detail,
            "conflict affirmed" if parsed.get("opposing") else "conflict declined",
        )
        step.done = True
        step.duration_ms = event.duration_ms

    def _postmortem(self, event: TraceEvent, parsed: dict) -> None:
        step = self._step("write-up", event.round)
        caveats = len(parsed.get("what_i_would_not_trust") or [])
        step.detail = f"{caveats} caveat(s) recorded"
        step.done = True
        step.duration_ms = event.duration_ms

    def _on_policy_decision(self, event: TraceEvent) -> None:
        payload = event.payload
        step = self._step("policy", payload.get("round"))
        decision = payload.get("decision", "")
        step.detail = (
            f"{decision.upper()} · {payload.get('rule_fired')} · "
            f"pressure {payload.get('pressure', 0):g}"
        )
        step.style = DECISION_STYLE.get(decision, "")
        step.done = True
        step.duration_ms = 0

    def _on_error(self, event: TraceEvent) -> None:
        step = self._step("critique", event.round)
        step.detail = _join(step.detail, f"{event.actor} FAILED", " │ ")
        step.style = "red"
        step.done = True

    def _on_state_exit(self, event: TraceEvent) -> None:
        """Durations only, and only where the content event could not supply one.

        Matched to the most recent step of that name rather than by round, because the
        round on a state event is the one in progress rather than the one being entered.
        """
        label = STEP_FOR_STATE.get(event.payload.get("state", ""))
        if label is None:
            return
        for step in reversed(self.steps):
            if step.label == label:
                # CRITIQUE is the exception worth overwriting: the span is the wall-clock
                # the two critics shared, and it is the number that shows they overlapped.
                if label == "critique" or step.duration_ms is None:
                    step.duration_ms = event.duration_ms
                return

    def _on_budget_check(self, event: TraceEvent) -> None:
        if not event.payload.get("breached"):
            return
        step = self._step("budget", event.round)
        step.detail = _one_line(str(event.payload["breached"]))
        step.style = "yellow"
        step.done = True

    def _on_run_end(self, event: TraceEvent) -> None:
        payload = event.payload
        outcome = payload.get("outcome", "")
        summary = Text("● ", style="dim")
        summary.append("done".ljust(LABEL_WIDTH), style="bold")
        summary.append(" ")
        summary.append(outcome.upper(), style=DECISION_STYLE.get(outcome, "bold"))
        summary.append(
            f" · {payload.get('rule_fired')} · {payload.get('rounds_used')} round(s)"
            f" · ${payload.get('total_cost_usd', 0):.4f}"
            f" · {payload.get('wall_seconds', 0):.1f}s",
            style="dim",
        )
        self._summary = summary

    # -- drawing ----------------------------------------------------------------------------

    def _step(self, label: str, round_: int | None) -> Step:
        """The row for this (label, round), created on first sight.

        Creating rows lazily from content is what keeps a step in the round it belongs to,
        and it means a step that never happens -- no Arbiter, no write-up -- simply has no
        row rather than an empty one that looks stalled.
        """
        for step in reversed(self.steps):
            if step.label == label and step.round == round_:
                return step
        if round_ is not None:
            self._round_header(round_)
        step = Step(label=label, round=round_)
        self.steps.append(step)
        return step

    def _round_header(self, round_: int) -> None:
        label = f"round {round_}/{self.max_rounds}"
        if any(s.label == label for s in self.steps):
            return
        self.steps.append(Step(label=label, round=None, done=True))

    def _renderable(self) -> RenderableType:
        lines = [
            step.render(last_in_round=self._is_last_in_round(index))
            for index, step in enumerate(self.steps)
        ]
        if self._summary is not None:
            lines.append(self._summary)
        return Group(*lines)

    def _is_last_in_round(self, index: int) -> bool:
        step = self.steps[index]
        if step.round is None:
            return False
        return not any(s.round == step.round for s in self.steps[index + 1:])

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._renderable())
        else:
            # All but the last row. The last one is still accumulating -- `grounding` gains
            # a tool per event and `critique` gains its second critic -- and a printed line
            # cannot be taken back. It goes out when the next row appears, or on exit.
            self._flush_from(len(self.steps) - 2)

    def _flush_from(self, upto: int) -> None:
        """Non-terminal output: print each completed step once, in order.

        Thousands of ANSI frames in a CI log is worse than no progress at all, and a log
        that shows the same run the terminal did is the point of having one renderer.

        A row is only printed once the *next* one exists, because until then there is no way
        to know whether it is the last of its round -- which is the difference between `├`
        and `└`. The final row is flushed on exit.
        """
        if self._live is not None:
            return
        while self._printed <= upto and self._printed < len(self.steps):
            index = self._printed
            self._printed += 1
            step = self.steps[index]
            if step.done or step.detail:
                self.console.print(step.render(last_in_round=self._is_last_in_round(index)))

#: A row is one line. `patch.validate`'s bounce message is a paragraph written for the model
#: to act on -- valuable in the trace, ruinous in a table.
DETAIL_WIDTH = 64


def _one_line(text: str, width: int = DETAIL_WIDTH) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _join(existing: str, addition: str, separator: str = " · ") -> str:
    return f"{existing}{separator}{addition}" if existing else addition


def _worst_severity(issues: list[dict]) -> str:
    order = ["info", "low", "medium", "high"]
    worst = max(
        (issue.get("severity", "info") for issue in issues),
        key=lambda s: order.index(s) if s in order else 0,
        default="info",
    )
    return worst
