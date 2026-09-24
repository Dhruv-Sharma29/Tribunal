"""The FSM driver: owns rounds, budgets and trace emission.

Everything decision-shaped lives elsewhere. `policy.py` decides, `fsm.py` says where a decision
leads, the agents produce structured output, and this module does the one thing none of them
can: sequence them, count what they spend, and write down what happened.

Three properties it is responsible for keeping true:

**The two critics run in parallel and neither can lose the other's work.** One
`asyncio.gather(..., return_exceptions=True)`. A critic that fails is recorded as errored and
its dimension is `unassessed` -- which is *not* clean, and which policy rows 5/6 refuse to let
reach an ACCEPT.

**The budget is checked at every state entry, and a breach escalates rather than aborts.**
docs/04 § Budget enforcement: "an exceeded budget still produces a trace, a report, and the
best patch seen so far. 'Ran out of money' is a legitimate outcome to report; losing the work
is not."

**The report is not built here.** It is derived from the trace by `trace/report.py`, so
`tribunal replay` runs the identical function over the identical events. Criterion S6 is then
true by construction rather than by vigilance.

## The Arbiter is optional, on purpose

docs/09 § The cut line contemplates template-rendering in place of the Arbiter agent, and that
path is kept working rather than left to rot. When no Arbiter is passed the orchestrator
synthesises the consolidated critique deterministically and records `synthesised_by:
"template"` in the trace, so a reader can always tell which produced it. The loop therefore
runs end to end -- through ACCEPT, REJECT, ESCALATE and TRADEOFF -- with no API key at all,
which is what keeps the Phase 3 acceptance criteria testable for free.

The difference the real Arbiter makes is not prose quality. It is two capabilities the template
cannot have: **detector 2 can fire**, because there is somebody to affirm that two same-span
remedies are actually opposed; and **the Coder's pushback gets adjudicated**, so a false
positive can leave the pressure sum instead of blocking every remaining round.

## Where the Arbiter sits relative to the decision

Twice per round, on opposite sides of `policy.decide`, and the order is load-bearing:

    same_span_candidates  ->  arbiter.affirms()  ->  decide()  ->  arbiter.run()
    (mechanical, free)        (classification)      (the only    (prose about a decision
                                                     decision)    it cannot change)

The affirmation is an input to the decision; the note is written about it. Neither is the
decision. `ArbiterNote.decision_echo` is checked against the verdict, and the affirmation is
checked against the pair it was asked about.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ulid import ULID

from tribunal.agents import (
    Arbiter,
    ArbiterBundle,
    Coder,
    CoderBundle,
    CritiqueBundle,
    Postmortem,
    Profiler,
    RedTeam,
)
from tribunal.agents.base import AgentRun
from tribunal.config import Settings
from tribunal.contracts import (
    Conflict,
    Critique,
    Decision,
    Dimension,
    GroundingReport,
    Patch,
    Report,
    TestResult,
    Verdict,
)
from tribunal.fsm import Machine, State, Trigger, trigger_for_decision
from tribunal.grounding.perf_t import Benchmark
from tribunal.grounding.suite import GroundingSuite
from tribunal.llm.client import LLMClient
from tribunal.patch import normalised_diff_sha256
from tribunal.policy import (
    PolicyInput,
    RoundRecord,
    Spend,
    decide,
    detect_oscillation_conflict,
    duplicates_in,
    same_span_candidates,
    same_span_conflict,
)
from tribunal.trace import events
from tribunal.trace import report as report_builder
from tribunal.trace.reader import Trace
from tribunal.trace.writer import Subscriber, TraceWriter, append_summary

CRITIC_CLASSES = {Dimension.SECURITY: RedTeam, Dimension.PERFORMANCE: Profiler}


@dataclass
class RoundOutcome:
    """What one debate round produced. Kept so the next round can be assembled."""

    round: int
    critiques: tuple[Critique, ...] = ()
    errored: tuple[Dimension, ...] = ()
    verdict: Verdict | None = None
    patched_source: str | None = None
    diff: str = ""
    normalised_hash: str | None = None
    consolidated_critique: str | None = None
    #: Ids the Arbiter agreed were false positives *this* round. Cumulative dismissals are
    #: gathered across `history`, because a dismissal is permanent for the rest of the run.
    dismissed: tuple[str, ...] = ()
    priority_order: tuple[str, ...] = ()


@dataclass
class RunResult:
    report: Report
    trace: Trace
    machine: Machine
    trace_path: Path | None = None

    @property
    def outcome(self) -> Decision:
        return self.report.outcome

    @property
    def terminal_state(self) -> State:
        return self.machine.state


class Orchestrator:
    """Drives one run from INIT to a terminal state."""

    def __init__(
        self,
        settings: Settings,
        client: LLMClient | None = None,
        arbiter: Arbiter | None = None,
        postmortem: Postmortem | None = None,
        subscribers: Sequence[Subscriber] = (),
    ) -> None:
        self.settings = settings
        self.client = client or LLMClient(settings)
        #: Extra renderers over the one event stream. Nothing in the loop calls them, and a
        #: run with none behaves identically -- which is the property that keeps the trace
        #: and any display of it from drifting apart.
        self.subscribers = list(subscribers)
        #: None means the templated stand-in: no affirmation, so detector 2 cannot fire, and
        #: no adjudication, so nothing is ever dismissed. Injected rather than built from
        #: settings so a caller can run the whole loop without the Arbiter's token cost --
        #: which is what the orchestrator tests do, and what `--no-arbiter` exposes.
        self.arbiter = arbiter
        #: None means no narrative: `Report.narrative` stays None and the structured report
        #: stands alone, which is what the cut line contemplates (docs/09 § The cut line,
        #: item 5). Nothing else in the run depends on it.
        self.postmortem = postmortem
        for name, seat in (("Arbiter", arbiter), ("Postmortem", postmortem)):
            # Budget enforcement reads `self.client.total_cost_usd`, so a seat holding a
            # second client would spend real money invisibly -- and the budget row would keep
            # reporting that the run was well inside its cap. Caught at construction because
            # the symptom (a cap that never fires) looks like nothing at all.
            if seat is not None and seat.client is not self.client:
                raise ValueError(
                    f"the {name} must share the orchestrator's LLMClient, or its spend is "
                    "missing from every budget check. Build one client and pass it to both."
                )
        self.suite = GroundingSuite(settings)
        self._entered_at = time.monotonic()

    # -- budget --------------------------------------------------------------------------

    def _spend(self, started: float, rounds: int) -> Spend:
        return Spend(
            usd=self.client.total_cost_usd,
            tokens=self.client.total_tokens,
            wall_seconds=time.monotonic() - started,
            rounds=rounds,
        )

    def _check_budget(self, writer: TraceWriter, state: State, round_: int | None,
                      spend: Spend) -> str | None:
        breached = spend.exceeds(self.settings.budget)
        writer.emit(
            events.budget_check,
            round=round_,
            state=state.value,
            usd=spend.usd,
            tokens=spend.tokens,
            wall_seconds=spend.wall_seconds,
            caps=self.settings.budget.model_dump(mode="json"),
            breached=breached,
        )
        return breached

    # -- the run -------------------------------------------------------------------------

    async def run(
        self,
        source: str,
        filename: str,
        test_source: str | None = None,
        benchmarks: Sequence[Benchmark] | None = None,
        argv: Sequence[str] | None = None,
        trace_dir: Path | None = None,
        traceback: str | None = None,
    ) -> RunResult:
        run_id = str(ULID())
        started = time.monotonic()
        directory = trace_dir if trace_dir is not None else self.settings.trace_dir
        path = directory / f"{run_id}.jsonl" if directory is not None else None
        writer = TraceWriter(run_id, path).open()
        for subscriber in self.subscribers:
            # The live CLI renderer attaches here. It sees exactly the events the file gets,
            # which is what stops the terminal and the trace from disagreeing about what
            # happened (docs/06 principle 4).
            writer.subscribe(subscriber)
        try:
            return await self._drive(
                writer, source, filename, test_source, benchmarks, argv, started,
                directory, traceback,
            )
        finally:
            writer.close()

    async def _drive(
        self,
        writer: TraceWriter,
        source: str,
        filename: str,
        test_source: str | None,
        benchmarks: Sequence[Benchmark] | None,
        argv: Sequence[str] | None,
        started: float,
        directory: Path | None,
        traceback: str | None = None,
    ) -> RunResult:
        writer.emit(
            events.run_start,
            input_file=filename,
            input_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            config_snapshot=self.settings.snapshot(),
            prompt_versions=self._prompt_versions(),
            argv=list(argv or []),
            trace_level=self.settings.trace_level,
        )

        machine = Machine()
        self._entered_at = time.monotonic()
        machine = self._enter(writer, machine, Trigger.READY, None)

        # -- GROUND: the baseline every later comparison is made against ------------------
        baseline = await self._ground(writer, source, filename, test_source, None, None)

        history: list[RoundOutcome] = []
        if not _parses(source):
            # Row 1's first clause, which nothing used to reach. There is nothing to propose
            # a patch *against*, so the run stops here rather than spending a Coder call and
            # two critics discovering it. The decision is ESCALATE and the terminal state is
            # FAILED -- the same pairing the attempts-exhausted path already produces, and
            # what docs/04 row 1 means by "ESCALATE (-> FAILED if nothing reviewable)".
            verdict = decide(
                PolicyInput(round=1, input_parses=False, patch_applied=False,
                            spend=self._spend(started, 0)),
                self.settings.policy,
                self.settings.budget,
            )
            writer.emit(events.policy_decision, verdict=verdict, duplicates=[])
            machine = self._enter(writer, machine, Trigger.INPUT_UNUSABLE, None)
            return self._finish(writer, machine, verdict, history, started, directory)

        machine = self._enter(writer, machine, Trigger.GROUNDED, None)
        records: list[RoundRecord] = []
        pressure_history: list[float] = []
        diff_hashes: list[str] = []
        current_source = source
        last_verdict: Verdict | None = None
        forced_escalation: str | None = None

        while not machine.is_terminal:
            # The machine increments `round` when it *enters* PROPOSE, and the loop body
            # begins with the machine already there — via GROUNDED the first time and via a
            # REJECT afterwards. Reading `machine.round + 1` here double-counted, which the
            # first end-to-end run caught immediately: round 1 was labelled round 2.
            round_ = machine.round
            spend = self._spend(started, len(history))
            breach = self._check_budget(writer, State.PROPOSE, round_, spend)

            # -- PROPOSE + VALIDATE ------------------------------------------------------
            outcome = RoundOutcome(round=round_)
            coder_result = await self._propose(
                writer, source, filename, baseline, test_source, history, round_, traceback
            )
            machine = self._walk_validate(writer, machine, coder_result, round_)

            accepted = coder_result.accepted if coder_result else None
            if accepted is None:
                # VALIDATE burned every attempt. Row 1's second clause --
                # `patch_attempts_exhausted` -- exists for exactly this, and it is the only
                # place that sets it. Deciding here rather than breaking straight out is what
                # keeps `run_end.rule_fired` and `Report.rule_fired` the same string: without
                # it the trace names the row and the report says "no_decision_recorded",
                # which is two answers to one question.
                verdict = decide(
                    PolicyInput(
                        round=round_,
                        patch_applied=False,
                        patch_attempts_exhausted=True,
                        pressure_history=tuple(pressure_history),
                        spend=self._spend(started, len(history)),
                    ),
                    self.settings.policy,
                    self.settings.budget,
                )
                writer.emit(events.policy_decision, verdict=verdict, duplicates=[])
                outcome.verdict = verdict
                last_verdict = verdict
                history.append(outcome)
                break

            current_source = accepted.patched_source or source
            outcome.diff = accepted.patch.diff
            outcome.normalised_hash = normalised_diff_sha256(accepted.patch.diff)
            outcome.patched_source = current_source

            # -- CRITIQUE: the only genuine parallelism ---------------------------------
            patched = await self._ground(
                writer, current_source, filename, test_source, round_, source, benchmarks
            )
            critiques, errored = await self._critique(
                writer, filename, current_source, patched, baseline, history, round_,
                accepted.patch,
            )
            outcome.critiques, outcome.errored = critiques, errored
            machine = self._enter(writer, machine, Trigger.CRITIQUED, round_)

            # -- ARBITRATE: policy first, prose second ----------------------------------
            conflict = await self._detect_conflict(
                writer, critiques, records, round_, filename, current_source
            )
            verdict = decide(
                PolicyInput(
                    round=round_,
                    critiques=critiques,
                    errored_dimensions=errored,
                    patch_applied=True,
                    patch_is_empty=not accepted.patch.diff.strip(),
                    diff_hash=outcome.normalised_hash,
                    previous_diff_hashes=tuple(diff_hashes),
                    dismissed_issue_ids=frozenset(_dismissed_so_far(history)),
                    pressure_history=tuple(pressure_history),
                    baseline_tests=baseline.tests,
                    patched_tests=patched.tests,
                    spend=self._spend(started, len(history)),
                    conflict=conflict,
                ),
                self.settings.policy,
                self.settings.budget,
            )
            if breach and verdict.decision is not Decision.ESCALATE:
                # A breach detected at state entry must not be overtaken by a later row. The
                # run still writes a report and keeps the best patch -- that is the point.
                forced_escalation = f"budget breached at PROPOSE: {breach}"
            writer.emit(events.policy_decision, verdict=verdict,
                        duplicates=duplicates_in(critiques))

            outcome.verdict = verdict
            last_verdict = verdict
            pressure_history.append(verdict.pressure)
            if outcome.normalised_hash:
                diff_hashes.append(outcome.normalised_hash)
            records.append(
                RoundRecord(
                    round=round_,
                    issue_ids=frozenset(verdict.open_issues),
                    regression_observed=_regression_observed(baseline, patched),
                )
            )

            # The note is written on REJECT (the Coder needs one instruction set) and on
            # TRADEOFF (the justification is the whole point of that terminal state).
            # ACCEPT and ESCALATE need no prose from it -- the Postmortem writes those up.
            if verdict.decision in (Decision.REJECT, Decision.TRADEOFF):
                await self._consolidate(
                    writer, outcome, critiques, verdict, accepted.patch, filename, history
                )
            history.append(outcome)

            decision = Decision.ESCALATE if forced_escalation else verdict.decision
            machine = self._enter(
                writer,
                machine,
                trigger_for_decision(
                    decision, rounds_remain=round_ < self.settings.policy.max_rounds
                ),
                round_,
            )
            if machine.state in (State.ESCALATE, State.TRADEOFF):
                machine = self._enter(writer, machine, Trigger.RECORDED, round_)
            if machine.state is State.POSTMORTEM:
                await self._write_up(writer, filename, round_)
                machine = self._enter(writer, machine, Trigger.WRITTEN, round_)

        return self._finish(writer, machine, last_verdict, history, started, directory)

    def _finish(
        self,
        writer: TraceWriter,
        machine: Machine,
        verdict: Verdict | None,
        history: Sequence[RoundOutcome],
        started: float,
        directory: Path | None,
    ) -> RunResult:
        """`run_end`, then the report derived from the events just written.

        One exit for every way a run can stop, so the unusable-input path cannot grow its own
        slightly different `run_end` -- which is how `rule_fired` came to disagree with the
        report once already (docs/13 § 24).
        """
        writer.emit(
            events.run_end,
            terminal_state=machine.state.value,
            outcome=(verdict.decision.value if verdict else "escalate"),
            rule_fired=(verdict.rule_fired if verdict else "input_unusable"),
            # Derived from the verdict, not from `len(history)`, so it agrees with
            # `Report.rounds_used` -- which `trace/report.build` computes as the highest
            # round that produced a decision. They are equal on every path that runs a
            # round; they diverged on the unusable-input path, where no round happens but
            # `Verdict.round` is 1 because the schema forbids 0. One source, no drift
            # (docs/13 § 24 is what happens otherwise).
            rounds_used=(verdict.round if verdict else len(history)),
            total_cost_usd=self.client.total_cost_usd,
            total_tokens=self.client.total_tokens,
            wall_seconds=time.monotonic() - started,
        )
        trace = Trace(events=list(writer.events))
        report = report_builder.build(trace)
        if directory is not None:
            append_summary(directory, report_builder.summary_row(report, trace, self.settings))
        return RunResult(report=report, trace=trace, machine=machine, trace_path=writer.path)

    # -- states ---------------------------------------------------------------------------

    def _walk_validate(
        self, writer: TraceWriter, machine: Machine, result, round_: int
    ) -> Machine:
        """Replay the Coder's attempts as real PROPOSE -> VALIDATE transitions.

        `Coder.propose` owns its own retry loop so the agent is testable standalone, but the
        FSM models each bounce as an edge. Collapsing them into one transition would leave the
        trace claiming a patch applied first time when it took three goes — and the
        VALIDATE -> PROPOSE edge, which docs/01 says absorbs a large share of early-round
        retries at zero token cost, would never appear in a waterfall.
        """
        attempts = result.attempts if result else []
        for index, attempt in enumerate(attempts):
            machine = self._enter(writer, machine, Trigger.PROPOSED, round_)
            if attempt.ok:
                return self._enter(writer, machine, Trigger.PATCH_OK, round_)
            last = index == len(attempts) - 1
            machine = self._enter(
                writer,
                machine,
                Trigger.PATCH_REJECTED_EXHAUSTED if last else Trigger.PATCH_REJECTED_RETRIES_LEFT,
                round_,
            )
        return machine

    def _enter(
        self, writer: TraceWriter, machine: Machine, trigger: Trigger, round_: int | None
    ) -> Machine:
        """Leave the current state, then enter the next, emitting both.

        The pair is what gives the trace a waterfall. It is also how the parallelism claim
        becomes *checkable* rather than asserted: the CRITIQUE span is wall-clock, each
        critic's `llm_response` carries its own `duration_ms`, and if the two critics ran in
        sequence their durations would sum to the span. They do not
        (docs/06 § The viewer, requirement: "drawn in parallel").
        """
        previous = machine.state
        now = time.monotonic()
        writer.emit(
            events.state_exit,
            state=previous.value,
            round=round_,
            duration_ms=int((now - self._entered_at) * 1000),
        )
        self._entered_at = now
        moved = machine.step(trigger)
        writer.emit(
            events.state_enter,
            state=moved.state.value,
            from_state=previous.value,
            round=round_,
        )
        return moved

    async def _ground(
        self,
        writer: TraceWriter,
        source: str,
        filename: str,
        test_source: str | None,
        round_: int | None,
        original: str | None,
        benchmarks: Sequence[Benchmark] | None = None,
    ) -> GroundingReport:
        run = await self.suite.run(
            source,
            target="original" if round_ is None else "patched",
            round=round_,
            logical_name=filename,
            test_source=test_source,
            benchmarks=list(benchmarks) if benchmarks else None,
            original_source=original,
        )
        for tool_outcome in run.outcomes:
            writer.emit(
                events.tool_run,
                round=round_,
                tool=tool_outcome.tool,
                argv=tool_outcome.argv,
                exit_code=tool_outcome.exit_code,
                findings_count=len(tool_outcome.findings),
                duration_ms=tool_outcome.duration_ms,
                stdout_excerpt=tool_outcome.stdout_excerpt,
                error=tool_outcome.error,
                findings=[f.model_dump(mode="json") for f in tool_outcome.findings],
            )
        if run.report.measurements:
            # `perf` produces no `ToolOutcome` -- its results live on the report rather than
            # on a tool run -- so without this the measurements never reach the trace, and
            # anything reading the file back (the write-up, the viewer) cannot know a
            # benchmark was attempted at all.
            writer.emit(
                events.tool_run,
                round=round_,
                tool="perf",
                argv=[],
                exit_code=0,
                findings_count=0,
                duration_ms=0,
                stdout_excerpt="",
                error=None,
                measurements=[
                    m.model_dump(mode="json") for m in run.report.measurements
                ],
            )
        return run.report

    async def _propose(
        self,
        writer: TraceWriter,
        source: str,
        filename: str,
        baseline: GroundingReport,
        test_source: str | None,
        history: list[RoundOutcome],
        round_: int,
        traceback: str | None = None,
    ):
        previous = history[-1] if history else None
        bundle = CoderBundle(
            round=round_,
            filename=filename,
            source=source,
            report=baseline,
            max_hunks=self.settings.policy.max_hunks,
            failing_test=test_source,
            # docs/03 § 3.1 lists "failing test / traceback if supplied" among the Coder's
            # inputs, and `CoderBundle._traceback_block` has always rendered it. Nothing
            # fed it until `--error` existed -- the seventh consumer-with-no-producer in
            # this codebase, and the only one that was a missing feature rather than a bug.
            traceback=traceback,
            consolidated_critique=previous.consolidated_critique if previous else None,
            priority_order=previous.priority_order if previous else (),
            dismissed_issue_ids=_dismissed_so_far(history),
            open_issues=tuple(
                issue
                for outcome in history
                for critique in outcome.critiques
                for issue in critique.issues
            ),
            rejected_diffs=tuple(
                (outcome.diff, outcome.verdict.rule_fired)
                for outcome in history
                if outcome.verdict and outcome.verdict.decision is Decision.REJECT
            ),
        )
        coder = Coder(self.settings, self.client)
        result = await coder.propose(bundle)
        for index, attempt in enumerate(result.attempts, start=1):
            self._emit_agent(writer, attempt.run, round_)
            writer.emit(
                events.patch_validate,
                round=round_,
                attempt=index,
                applied=attempt.validation.applied,
                parse_ok=attempt.validation.parse_ok,
                hunks=attempt.validation.hunks,
                diff_sha256=attempt.validation.diff_sha256,
                normalised_sha256=normalised_diff_sha256(attempt.patch.diff),
                failure_reason=attempt.validation.failure_reason,
                diff=attempt.patch.diff,
            )
        return result

    async def _critique(
        self,
        writer: TraceWriter,
        filename: str,
        patched_source: str,
        patched: GroundingReport,
        baseline: GroundingReport,
        history: list[RoundOutcome],
        round_: int,
        patch: Patch,
    ) -> tuple[tuple[Critique, ...], tuple[Dimension, ...]]:
        previous = history[-1] if history else None
        agents = [cls(self.settings, self.client) for cls in CRITIC_CLASSES.values()]
        bundles = [
            CritiqueBundle(
                round=round_,
                dimension=agent.dimension,
                filename=filename,
                patched_source=patched_source,
                patched_report=patched,
                baseline_report=baseline,
                # docs/11-risks.md R1: critics go sycophantic when handed a confident
                # justification as context, so the bundle renders the Coder's rationale and
                # its pushback as *claims to check*. Omitting the patch here left that
                # mitigation rendering nothing on every real run.
                patch=patch,
                consolidated_critique=previous.consolidated_critique if previous else None,
                dismissed_issue_ids=_dismissed_so_far(history),
            )
            for agent in agents
        ]
        # `return_exceptions=True`: one critic failing must not lose the other's work.
        results = await asyncio.gather(
            *(agent.run(bundle) for agent, bundle in zip(agents, bundles, strict=True)),
            return_exceptions=True,
        )

        critiques: list[Critique] = []
        errored: list[Dimension] = []
        for agent, result in zip(agents, results, strict=True):
            if isinstance(result, BaseException):
                errored.append(agent.dimension)
                writer.emit(
                    events.error,
                    actor=agent.role,
                    round=round_,
                    exception=type(result).__name__,
                    message=str(result),
                    # Recovered in the sense that the run continues; the *dimension* is not.
                    recovered=True,
                )
                continue
            self._emit_agent(writer, result, round_)
            critiques.append(result.value)
        return tuple(critiques), tuple(errored)

    async def _detect_conflict(
        self,
        writer: TraceWriter,
        critiques: Sequence[Critique],
        records: Sequence[RoundRecord],
        round_: int,
        filename: str,
        source: str,
    ) -> Conflict | None:
        """Detector 1, then detector 2 -- the second of which costs a call, sometimes.

        Detector 1 is free and stronger, so it goes first and short-circuits. Detector 2's
        mechanical half is also free: `same_span_candidates` is a filter over issues already
        in hand, and on the overwhelmingly common round it returns nothing and the Arbiter is
        never asked. Only a genuine same-span, cross-dimension, grounded, MEDIUM+ pair buys a
        classification call, and the first pair affirmed wins.

        With no Arbiter there is nobody to affirm, so detector 2 cannot fire -- the safe
        default, because docs/11-risks.md R4 makes a spurious TRADEOFF the worst output the
        system can produce.
        """
        oscillating = detect_oscillation_conflict(critiques, records)
        if oscillating is not None:
            return oscillating
        if self.arbiter is None:
            return None
        for left, right in same_span_candidates(critiques):
            run = await self.arbiter.affirms(left, right, round_, filename, source)
            self._emit_agent(writer, run, round_)
            if run.value.opposing:
                return same_span_conflict(
                    left,
                    right,
                    left_remedy_cost=run.value.left_remedy_cost,
                    right_remedy_cost=run.value.right_remedy_cost,
                )
        return None

    async def _consolidate(
        self,
        writer: TraceWriter,
        outcome: RoundOutcome,
        critiques: Sequence[Critique],
        verdict: Verdict,
        patch: Patch,
        filename: str,
        history: Sequence[RoundOutcome],
    ) -> None:
        """The Arbiter's note, or the template that stands in for it.

        Synthesis rather than concatenation is *prevention* for the oscillation the guard in
        policy merely detects (docs/02 § ArbiterNote): contradictory raw critiques make the
        Coder undo round 1 in round 2.

        Writes its results onto `outcome` rather than returning them, because there are three
        of them now -- the instruction set, the dismissals and the priority order -- and the
        next round needs all three.
        """
        round_ = verdict.round
        if self.arbiter is None:
            outcome.consolidated_critique = self._templated_note(writer, critiques, verdict)
            return

        bundle = ArbiterBundle(
            round=round_,
            filename=filename,
            verdict=verdict,
            critiques=tuple(critiques),
            patch=patch,
            already_dismissed=_dismissed_so_far(history),
        )
        run = await self.arbiter.run(bundle, round_)
        self._emit_agent(writer, run, round_)
        note = run.value
        outcome.consolidated_critique = note.consolidated_critique
        outcome.dismissed = tuple(entry.issue_id for entry in note.dismissed)
        outcome.priority_order = tuple(note.priority_order)

    def _templated_note(
        self, writer: TraceWriter, critiques: Sequence[Critique], verdict: Verdict
    ) -> str:
        """The cut-line fallback, labelled as such in the trace.

        Deliberately cannot dismiss anything: adjudicating the Coder's pushback is a judgement
        call, and a template that guessed at it would silently drop real issues out of the
        pressure sum.

        The payload follows `ArbiterNote`'s branch rule -- prose in the field the decision
        calls for, null in the others -- plus the `synthesised_by` marker that says it is not
        one. A payload that contradicted its own schema would be a trap for whoever writes
        the viewer.
        """
        rejecting = verdict.decision is Decision.REJECT
        text = (
            synthesise_instructions(critiques, verdict)
            if rejecting
            else describe_tradeoff(critiques, verdict)
        )
        writer.emit(
            events.llm_response,
            actor="arbiter",
            round=verdict.round,
            parsed={
                "round": verdict.round,
                "decision_echo": verdict.decision.value,
                "consolidated_critique": text if rejecting else None,
                "priority_order": verdict.open_issues,
                "dismissed": [],
                "tradeoff_justification": None if rejecting else text,
                "recommended_default": None if rejecting else TEMPLATED_DEFAULT,
                "synthesised_by": "template",
            },
            stop_reason=None,
            structure="native_strict",
            parse_retries=0,
            local_repairs=[],
            transient_retries=0,
            request_hash="",
            replayed=False,
            usage=None,
            duration_ms=0,
        )
        return text

    async def _write_up(self, writer: TraceWriter, filename: str, round_: int) -> None:
        """The narrative, written from the events this run has already emitted.

        Skipped silently when no Postmortem is seated: `Report.narrative` is then None and
        the structured report stands on its own, which is what every run did before this
        agent existed.

        Deliberately after the last budget check. docs/04 § Budget enforcement: "an exceeded
        budget still produces a trace, a report, and the best patch seen so far". Refusing to
        write up a run because the money ran out loses the work at exactly the moment a human
        most needs to be told what happened.
        """
        if self.postmortem is None:
            return
        bundle = report_builder.postmortem_bundle(Trace(events=list(writer.events)), filename)
        run = await self.postmortem.run(bundle, round_)
        self._emit_agent(writer, run, round_)

    def _emit_agent(self, writer: TraceWriter, run: AgentRun, round_: int) -> None:
        full = self.settings.trace_level == "full"
        writer.emit(
            events.llm_request,
            actor=run.role,
            round=round_,
            model=run.model,
            provider=run.provider.value,
            effort=run.effort,
            prompt_version=run.prompt_version,
            system=run.outcome.response.raw_text if full else "",
            user="" if not full else "",
            cache_breakpoints=1,
            full=full,
        )
        writer.emit(
            events.llm_response,
            actor=run.role,
            round=round_,
            parsed=run.value.model_dump(mode="json"),
            stop_reason=run.response.stop_reason,
            structure=run.response.structure.value,
            parse_retries=run.outcome.repair_retries,
            local_repairs=run.outcome.local_repairs,
            transient_retries=run.outcome.transient_retries,
            request_hash=run.outcome.request_hash,
            replayed=run.response.replayed,
            usage=run.response.usage,
            duration_ms=run.duration_ms,
            metrics=run.metrics,
        )

    def _prompt_versions(self) -> dict[str, str]:
        from tribunal.agents import prompts

        return {
            role: prompts.load(role).stamp
            for role in prompts.available_roles()
        }


#: What the templated path can honestly say about which side to ship: nothing. Naming a
#: winner is a judgement, and docs/04 wants the *condition* that reverses it -- which no
#: template can derive. Saying so is better than a confident-sounding default nobody chose.
TEMPLATED_DEFAULT = (
    "No recommendation: this run had no Arbiter, and choosing a side is a judgement rather "
    "than a lookup. Both costs are stated above; a human decides."
)


def _dismissed_so_far(history: Sequence[RoundOutcome]) -> tuple[str, ...]:
    """Every id the Arbiter has dismissed, oldest first.

    A dismissal is permanent (docs/02 § ArbiterNote), so this accumulates across the run
    rather than resetting each round. It feeds three places at once: `PolicyInput`, where the
    issue leaves the pressure sum; the critics' bundles, where re-raising it is a validation
    error; and the next `ArbiterBundle`, so a settled question is not re-adjudicated.
    """
    seen: dict[str, None] = {}
    for outcome in history:
        for issue_id in outcome.dismissed:
            seen.setdefault(issue_id, None)
    return tuple(seen)


def describe_tradeoff(critiques: Sequence[Critique], verdict: Verdict) -> str:
    """Deterministic stand-in for the Arbiter's trade-off justification.

    States the axis, both sides and both costs, and stops there. docs/04 § What TRADEOFF
    actually emits also wants the recommended default and the condition that reverses it;
    those are judgements, so the template declines them explicitly rather than inventing one.
    """
    conflict = verdict.conflict
    if conflict is None:  # pragma: no cover - only reachable on a TRADEOFF, which sets it
        return "A trade-off was recorded without a conflict, which should not happen."
    titles = {i.id: i.title for c in critiques for i in c.issues}
    left, right = conflict.left_issue, conflict.right_issue
    return "\n".join(
        [
            f"Irreconcilable on axis {conflict.axis} (detector: {conflict.detector}).",
            f"  {left}: {titles.get(left, '(unknown issue)')}",
            f"    remedy costs: {conflict.left_remedy_cost}",
            f"  {right}: {titles.get(right, '(unknown issue)')}",
            f"    remedy costs: {conflict.right_remedy_cost}",
            "Both objections stand. Neither was dropped to manufacture agreement.",
        ]
    )


def synthesise_instructions(critiques: Sequence[Critique], verdict: Verdict) -> str:
    """Deterministic stand-in for the Arbiter's consolidated critique.

    Ordered by policy weight, so the Coder is told to fix the thing that is actually keeping
    pressure high rather than whichever critic spoke first. docs/09 § The cut line explicitly
    permits template-rendering in place of an LLM agent; this is that, and the trace records
    `synthesised_by: "template"` so nobody mistakes it for an Arbiter's judgement.
    """
    open_ids = set(verdict.open_issues)
    issues = sorted(
        (i for c in critiques for i in c.issues if i.id in open_ids),
        key=lambda i: -i.score,
    )
    if not issues:
        return "No open issues. Re-propose only if the patch failed to apply."
    lines = [
        f"Fix these in order. Pressure is {verdict.pressure:g}; "
        f"rule fired was {verdict.rule_fired}.",
    ]
    for index, issue in enumerate(issues, start=1):
        refs = ", ".join(f"{e.kind.value}:{e.ref}" for e in issue.evidence)
        lines.append(
            f"{index}. [{issue.id}] {issue.dimension.value}/{issue.severity.value} — "
            f"{issue.title}\n   evidence: {refs}\n   direction: "
            f"{issue.suggested_direction or '(none given)'}"
        )
    return "\n".join(lines)


def _regression_observed(baseline: GroundingReport, patched: GroundingReport) -> bool:
    """Whether this round's grounding shows a measured or structural regression.

    Detector 1 needs this: an issue returning is only evidence of a trade-off if the
    intervening round actually cost something.
    """
    if any(m.verdict == "slower" for m in patched.measurements):
        return True
    return _total_complexity(patched) > _total_complexity(baseline)


def _total_complexity(report: GroundingReport) -> int:
    for finding in report.by_tool("radon"):
        total = finding.raw.get("total_complexity")
        if isinstance(total, int):
            return total
    return 0


def _parses(source: str) -> bool:
    """Whether the *original* file is Python at all.

    Checked here rather than inferred from a grounding tool error: `bandit` and `ruff` both
    report a syntax error, but they report a great many other things too, and "the input is
    not reviewable" is a different claim from "a tool complained".
    """
    try:
        ast.parse(source)
    except SyntaxError:
        return False
    return True


def failed_tests(result: TestResult | None) -> list[str]:
    return list(result.failed_node_ids) if result and result.ran else []
