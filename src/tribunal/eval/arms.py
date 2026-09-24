"""The four arms, and why three of them do not go through the policy layer.

docs/07-evaluation.md § Baselines:

| ID | Baseline | Isolates |
|---|---|---|
| `B0` | single call, no tool output | the floor |
| `B1` | single call + the grounding output | **does the debate beat the linters?** |
| `B2` | Coder -> one critic -> Coder, no arbiter | does arbitration beat one critique? |
| `B3` | the full tribunal | |

## `ArmRun`, rather than making every arm produce a `Report`

`Report` is the tribunal's output, and its `outcome` comes from a `policy_decision` in the trace.
Making the baselines emit one would mean choosing between two bad options: fake a verdict
(ACCEPT with no critics, which the `Verdict` schema only permits by claiming no dimension was
unassessed -- untrue), or run the real table over zero critiques, which fires rows 5/6 and
escalates **every** baseline run. The second is worse than it sounds: it would make B0-B2 look
like they were carefully declining to ship, when in fact they have no mechanism to decline
anything.

docs/07's own results table already says M5 is `n/a` for B0-B2. So the arms return an
`ArmRun` -- the fields scoring actually needs -- and `outcome` is `None` for an arm with no
representation for one. B3's `ArmRun` is built from its real `Report`, so nothing about the
tribunal's path changes.

## A deliberate deviation that favours the baselines

docs/07 describes B0 as a single call saying "fix the bug in this file". These arms use the
**real Coder prompt and the real VALIDATE loop** instead, differing only in what they are
shown and who reviews them. That makes the baselines stronger than the spec asks for, which
is the direction to err: the question is whether *the debate* adds anything, and a baseline
handicapped by a worse prompt would answer a different, flattering question.

Every arm writes a trace, so `--replay` re-scores any of them without re-running.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ulid import ULID

from tribunal.agents import Arbiter, Coder, CoderBundle, CritiqueBundle, Postmortem, RedTeam
from tribunal.config import Settings
from tribunal.contracts import (
    Critique,
    Decision,
    Dimension,
    GroundingFinding,
    GroundingReport,
)
from tribunal.eval.case import EvalCase
from tribunal.grounding.suite import GroundingSuite
from tribunal.llm.client import LLMClient
from tribunal.orchestrator import Orchestrator
from tribunal.patch import normalised_diff_sha256
from tribunal.trace import events
from tribunal.trace.writer import TraceWriter

#: The arms docs/07 § Baselines defines. `B1` is the honest comparison and `B3` is the tribunal;
#: the cut line (docs/09) drops `B0` and `B2` first if time runs out.
ARM_IDS = ("B0", "B1", "B2", "B3")

#: docs/09 § The cut line, item 3: "keep only `B1` vs `B3`. `B1` is the comparison that
#: matters." Named here so the runner's default can be changed in one place.
CUT_LINE_ARMS = ("B1", "B3")


@dataclass
class ArmRun:
    """One arm's result on one case — exactly what scoring needs, and nothing else."""

    arm: str
    case_id: str
    accepted_diff: str | None
    patched_source: str | None
    critiques: list[Critique] = field(default_factory=list)
    findings: list[GroundingFinding] = field(default_factory=list)
    #: None for an arm with no representation for an outcome. docs/07 reports M5 as a
    #: capability difference rather than as a score of zero.
    outcome: Decision | None = None
    rounds_used: int = 1
    cost_usd: float = 0.0
    trace_path: Path | None = None
    error: str | None = None


class Arm(Protocol):
    async def __call__(
        self, case: EvalCase, settings: Settings, client: LLMClient, trace_dir: Path
    ) -> ArmRun: ...


# -- shared plumbing ----------------------------------------------------------------------


def _writer(case: EvalCase, arm: str, trace_dir: Path, settings: Settings) -> TraceWriter:
    run_id = str(ULID())
    writer = TraceWriter(run_id, trace_dir / f"{arm}-{case.id}-{run_id}.jsonl").open()
    writer.emit(
        events.run_start,
        input_file=case.filename,
        input_sha256=hashlib.sha256(case.source.encode("utf-8")).hexdigest(),
        config_snapshot=settings.snapshot(),
        prompt_versions=_prompt_versions(),
        argv=[f"eval:{arm}", case.id],
        trace_level=settings.trace_level,
    )
    return writer


def _prompt_versions() -> dict[str, str]:
    from tribunal.agents import prompts

    return {role: prompts.load(role).stamp for role in prompts.available_roles()}


def _end(writer: TraceWriter, run: ArmRun, started: float, client: LLMClient) -> ArmRun:
    """Close the trace, and take this run's cost *from its own events*.

    Not from `client.total_cost_usd`: one `LLMClient` is shared across the sweep, so that
    counter is the running total for everything run so far. Serially it makes each arm look
    like it cost what every earlier arm cost as well; concurrently it is worse than wrong,
    because the arms interleave and the number depends on scheduling. M7 is a published
    metric, so it comes from the only place that is per-run — this writer's own usage.
    """
    cost, tokens = _usage_of(writer)
    writer.emit(
        events.run_end,
        terminal_state="DONE" if run.accepted_diff else "FAILED",
        outcome=(run.outcome.value if run.outcome else "escalate"),
        rule_fired=f"arm:{run.arm}",
        rounds_used=run.rounds_used,
        total_cost_usd=cost,
        total_tokens=tokens,
        wall_seconds=time.monotonic() - started,
    )
    run.cost_usd = cost
    run.trace_path = writer.path
    writer.close()
    return run


def _usage_of(writer: TraceWriter) -> tuple[float, int]:
    cost = sum(e.usage.cost_usd for e in writer.events if e.usage is not None)
    tokens = sum(
        e.usage.input_tokens + e.usage.output_tokens
        for e in writer.events
        if e.usage is not None
    )
    return round(cost, 6), tokens


async def _ground(
    writer: TraceWriter, case: EvalCase, settings: Settings
) -> GroundingReport:
    run = await GroundingSuite(settings).run(
        case.source,
        logical_name=case.filename,
        test_source=case.test_source,
    )
    for outcome in run.outcomes:
        writer.emit(
            events.tool_run,
            round=None,
            tool=outcome.tool,
            argv=outcome.argv,
            exit_code=outcome.exit_code,
            findings_count=len(outcome.findings),
            duration_ms=outcome.duration_ms,
            stdout_excerpt=outcome.stdout_excerpt,
            error=outcome.error,
            findings=[f.model_dump(mode="json") for f in outcome.findings],
        )
    return run.report


def _emit_agent(writer: TraceWriter, agent_run, round_: int) -> None:
    writer.emit(
        events.llm_response,
        actor=agent_run.role,
        round=round_,
        parsed=agent_run.value.model_dump(mode="json"),
        stop_reason=agent_run.response.stop_reason,
        structure=agent_run.response.structure.value,
        parse_retries=agent_run.outcome.repair_retries,
        local_repairs=agent_run.outcome.local_repairs,
        transient_retries=agent_run.outcome.transient_retries,
        request_hash=agent_run.outcome.request_hash,
        replayed=agent_run.response.replayed,
        usage=agent_run.response.usage,
        duration_ms=agent_run.duration_ms,
        metrics=agent_run.metrics,
    )


async def _propose(
    writer: TraceWriter,
    case: EvalCase,
    settings: Settings,
    client: LLMClient,
    report: GroundingReport,
    round_: int = 1,
    consolidated: str | None = None,
):
    """One PROPOSE -> VALIDATE pass, traced. Shared by every arm that writes a patch."""
    bundle = CoderBundle(
        round=round_,
        filename=case.filename,
        source=case.source,
        report=report,
        max_hunks=settings.policy.max_hunks,
        failing_test=case.test_source,
        consolidated_critique=consolidated,
    )
    result = await Coder(settings, client).propose(bundle)
    for index, attempt in enumerate(result.attempts, start=1):
        _emit_agent(writer, attempt.run, round_)
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


#: An empty baseline, for the arm that is shown no tool output at all. Every tool "ran" in
#: the sense that B0 is not being handicapped by a missing suite -- it is being shown nothing,
#: which is the thing B0 isolates.
def _blind_report() -> GroundingReport:
    return GroundingReport(
        target="original", round=None, findings=[], measurements=[], tests=None,
        tool_errors={}, tools_run=[],
    )


# -- the arms ------------------------------------------------------------------------------


async def run_b0(
    case: EvalCase, settings: Settings, client: LLMClient, trace_dir: Path
) -> ArmRun:
    """The floor: propose a patch having been shown no tool output."""
    started = time.monotonic()
    writer = _writer(case, "B0", trace_dir, settings)
    result = await _propose(writer, case, settings, client, _blind_report())
    accepted = result.accepted
    run = ArmRun(
        arm="B0",
        case_id=case.id,
        accepted_diff=accepted.patch.diff if accepted else None,
        patched_source=accepted.patched_source if accepted else None,
        findings=[],
    )
    return _end(writer, run, started, client)


async def run_b1(
    case: EvalCase, settings: Settings, client: LLMClient, trace_dir: Path
) -> ArmRun:
    """The honest comparison: the same call, plus the grounding output in the prompt.

    Everything B3 has except the debate. If B3 only marginally beats this, that is the
    finding, and docs/07 says to report it rather than to look for a kinder baseline.
    """
    started = time.monotonic()
    writer = _writer(case, "B1", trace_dir, settings)
    report = await _ground(writer, case, settings)
    result = await _propose(writer, case, settings, client, report)
    accepted = result.accepted
    run = ArmRun(
        arm="B1",
        case_id=case.id,
        accepted_diff=accepted.patch.diff if accepted else None,
        patched_source=accepted.patched_source if accepted else None,
        findings=list(report.findings),
    )
    return _end(writer, run, started, client)


async def run_b2(
    case: EvalCase, settings: Settings, client: LLMClient, trace_dir: Path
) -> ArmRun:
    """Coder -> one combined critic -> Coder. One round, no arbiter, no policy layer.

    The critic is the Red-team over the patched file. Not a merged security+performance
    critic: there is no such prompt, and inventing one for the baseline would make B2 a
    measurement of a prompt written specially to lose. Using a real critic asks the sharper
    question -- does *arbitration and a second dimension* add anything over one good
    critique pass.
    """
    started = time.monotonic()
    writer = _writer(case, "B2", trace_dir, settings)
    report = await _ground(writer, case, settings)
    first = await _propose(writer, case, settings, client, report)
    accepted = first.accepted
    if accepted is None:
        run = ArmRun(arm="B2", case_id=case.id, accepted_diff=None, patched_source=None,
                     findings=list(report.findings))
        return _end(writer, run, started, client)

    patched = await GroundingSuite(settings).run(
        accepted.patched_source or case.source,
        target="patched", round=1, logical_name=case.filename,
        test_source=case.test_source, original_source=case.source,
    )
    critic = RedTeam(settings, client)
    critique_run = await critic.run(
        CritiqueBundle(
            round=1, dimension=Dimension.SECURITY, filename=case.filename,
            patched_source=accepted.patched_source or case.source,
            patched_report=patched.report, baseline_report=report,
            patch=accepted.patch,
        )
    )
    _emit_agent(writer, critique_run, 1)

    second = await _propose(
        writer, case, settings, client, report, round_=2,
        consolidated=critique_run.value.summary,
    )
    final = second.accepted or accepted
    run = ArmRun(
        arm="B2",
        case_id=case.id,
        accepted_diff=final.patch.diff,
        patched_source=final.patched_source,
        critiques=[critique_run.value],
        findings=list(patched.report.findings),
        rounds_used=2,
    )
    return _end(writer, run, started, client)


async def run_b3(
    case: EvalCase, settings: Settings, client: LLMClient, trace_dir: Path
) -> ArmRun:
    """The full tribunal, through the real orchestrator. Nothing here is eval-specific."""
    orchestrator = Orchestrator(
        settings,
        client,
        arbiter=Arbiter(settings, client),
        postmortem=Postmortem(settings, client),
    )
    result = await orchestrator.run(
        case.source,
        filename=case.filename,
        test_source=case.test_source,
        argv=["eval:B3", case.id],
        trace_dir=trace_dir,
    )
    critiques = [
        Critique.model_validate(event.payload["parsed"])
        for event in result.trace.of_kind("llm_response")
        if event.actor in ("redteam", "profiler")
    ]
    findings = findings_in(result.trace)
    return ArmRun(
        arm="B3",
        case_id=case.id,
        accepted_diff=result.report.accepted_diff,
        patched_source=None,
        critiques=critiques,
        findings=findings,
        outcome=result.report.outcome,
        rounds_used=result.report.rounds_used,
        cost_usd=result.report.total_cost_usd,
        trace_path=result.trace_path,
    )


def findings_in(trace) -> list[GroundingFinding]:
    """Every grounding finding the trace recorded, de-duplicated by id.

    A critique cites a finding by id; M1's `rule` locator needs the rule behind that id.
    `tool_run` used to carry only `findings_count`, so this was unrecoverable from a trace
    and the eval would have scored every rule locator as a miss against the tribunal -- the same
    shape as docs/13 § 32, found the same way, by writing the consumer first.
    """
    seen: dict[str, GroundingFinding] = {}
    for event in trace.of_kind("tool_run"):
        for raw in event.payload.get("findings", []) or []:
            finding = GroundingFinding.model_validate(raw)
            seen.setdefault(finding.id, finding)
    return list(seen.values())


ARMS: dict[str, Arm] = {"B0": run_b0, "B1": run_b1, "B2": run_b2, "B3": run_b3}


# -- reading a run back off disk -------------------------------------------------------------


def critiques_in(trace) -> list[Critique]:
    return [
        Critique.model_validate(event.payload["parsed"])
        for event in trace.of_kind("llm_response")
        if event.actor in ("redteam", "profiler")
    ]


def accepted_diff_in(trace) -> str | None:
    applied = [
        e.payload
        for e in trace.of_kind("patch_validate")
        if e.payload.get("applied") and e.payload.get("parse_ok")
    ]
    return applied[-1]["diff"] if applied else None


def replay_run(trace, case: EvalCase, arm: str) -> ArmRun:
    """Rebuild an `ArmRun` from a recorded trace, with no calls and no provider.

    This is what makes `--replay` worth the implementation: docs/07 wants judge-prompt
    iteration to be free after the first sweep, and it is only free if a run can be
    reconstructed faithfully rather than approximately.

    The patched source is recovered by re-applying the accepted diff to the case's original,
    rather than being stored: a diff is already in the trace, `patch.apply_unified_diff` is
    the same function that produced the patched file in the first place, and a second copy
    of the source would be a second thing that could disagree with it.
    """
    from tribunal.patch import PatchError, apply_unified_diff

    diff = accepted_diff_in(trace)
    patched = None
    if diff:
        try:
            patched = apply_unified_diff(case.source, diff)
        except PatchError:
            # A diff that no longer applies means the case's `before.py` was edited after
            # the sweep. Surfaced as an error rather than silently judged against the
            # original, which would attribute regressions to the wrong file.
            return ArmRun(
                arm=arm, case_id=case.id, accepted_diff=diff, patched_source=None,
                error="the recorded diff no longer applies to before.py — the case changed "
                      "after this sweep, so it cannot be re-scored",
            )

    decisions = trace.of_kind("policy_decision")
    outcome = None
    if decisions:
        outcome = Decision(decisions[-1].payload["decision"])
    ends = trace.of_kind("run_end")
    return ArmRun(
        arm=arm,
        case_id=case.id,
        accepted_diff=diff,
        patched_source=patched,
        critiques=critiques_in(trace),
        findings=findings_in(trace),
        outcome=outcome,
        rounds_used=(ends[-1].payload.get("rounds_used", 1) if ends else 1),
        cost_usd=(ends[-1].payload.get("total_cost_usd", 0.0) if ends else 0.0),
        trace_path=None,
    )
