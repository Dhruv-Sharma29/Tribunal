"""Building the `Report` from a trace. Pure, and the reason replay is exact.

Charter criterion S6 -- "any run replays from its trace with zero API calls, reproducing the
report byte-identically" -- is usually attempted by re-running the pipeline against recorded
responses and hoping nothing drifts. That is a property you have to keep re-earning.

Here it is true *by construction*: the orchestrator does not build a report of its own. It
writes events, then calls `build`. `tribunal replay` reads the file and calls the same
`build`. Same function, same input, therefore same bytes. There is no second code path that
could diverge, which is the only kind of reproducibility claim worth making.

The second payoff is the one docs/10 § Replay calls out: because `policy.decide` is pure and
the *whole* critique is in the trace, `redecide` can ask "what would this run have concluded
under a different `accept_threshold`?" against real recorded critiques, retroactively, for
free. That is how thresholds get tuned on evidence instead of intuition.
"""

from __future__ import annotations

from dataclasses import dataclass

from tribunal.agents.bundle import PostmortemBundle, RoundDigest
from tribunal.config import PolicyConfig, Settings
from tribunal.contracts import (
    Conflict,
    Critique,
    Decision,
    Dimension,
    Issue,
    PostmortemNote,
    Report,
    ReportIssue,
    Verdict,
)
from tribunal.policy import PolicyInput, Spend, decide
from tribunal.trace.reader import Trace


def _verdicts(trace: Trace) -> list[Verdict]:
    """Reconstruct each round's `Verdict` from its `policy_decision` payload.

    The payload is the verdict's fields **flat**, plus diagnostics like
    `collapsed_duplicates`. Flat because the README's one-liner is
    `jq '.payload.rule_fired'` and nesting it under a key would break every published example.
    `Verdict` forbids extra fields, so the read side filters to the declared ones rather than
    the write side giving up greppability.
    """
    fields = set(Verdict.model_fields)
    return [
        Verdict.model_validate({k: v for k, v in e.payload.items() if k in fields})
        for e in trace.of_kind("policy_decision")
    ]


def _critiques_by_round(trace: Trace) -> dict[int, list[Critique]]:
    rounds: dict[int, list[Critique]] = {}
    for event in trace.of_kind("llm_response"):
        if event.actor not in ("redteam", "profiler"):
            continue
        critique = Critique.model_validate(event.payload["parsed"])
        rounds.setdefault(critique.round, []).append(critique)
    return rounds


def _accepted_diff(trace: Trace, final: Verdict | None) -> tuple[str | None, str | None]:
    """The diff that shipped, or why nothing did.

    A TRADEOFF ships a patch too -- that is the whole point of the state -- so the accepted
    diff is the last one that passed VALIDATE in the deciding round, for both ACCEPT and
    TRADEOFF.
    """
    if final is None:
        return None, "the run produced no policy decision"
    if final.decision in (Decision.REJECT, Decision.ESCALATE):
        reason = {
            Decision.REJECT: "the final round was rejected and the round budget ran out",
            Decision.ESCALATE: f"escalated: {final.rule_fired}",
        }[final.decision]
        best = _last_applied_diff(trace, final.round)
        # An escalated run still reports its best patch; it just does not claim it was
        # accepted. Losing the work is not a legitimate outcome (docs/04 § Budget enforcement).
        if best:
            return None, f"{reason} (best patch of {len(best)} chars is in the trace)"
        return None, reason
    diff = _last_applied_diff(trace, final.round)
    if diff is None:
        return None, "no patch applied in the deciding round"
    return diff, None


def _last_applied_diff(trace: Trace, round: int) -> str | None:
    applied = [
        e.payload
        for e in trace.of_kind("patch_validate")
        if e.round == round and e.payload.get("applied") and e.payload.get("parse_ok")
    ]
    return applied[-1]["diff"] if applied else None


def _issue_fates(
    trace: Trace, final: Verdict | None
) -> list[ReportIssue]:
    """Every issue the run saw, with what became of it.

    The four fates are the charter's output contract: issues found, issues fixed, issues
    accepted as a trade-off, and the ones still open at accept time -- which are *reported*,
    not hidden, because the accept threshold is not zero.
    """
    by_round = _critiques_by_round(trace)
    if not by_round:
        return []
    first_seen: dict[str, int] = {}
    latest: dict[str, Issue] = {}
    for round_ in sorted(by_round):
        for critique in by_round[round_]:
            for issue in critique.issues:
                first_seen.setdefault(issue.id, round_)
                latest[issue.id] = issue

    open_now = set(final.open_issues) if final else set()
    conflicted = (
        {final.conflict.left_issue, final.conflict.right_issue}
        if final and final.conflict
        else set()
    )
    dismissed = _dismissed_ids(trace)

    fates: list[ReportIssue] = []
    for issue_id, issue in latest.items():
        if issue_id in dismissed:
            status = "dismissed"
        elif issue_id in conflicted:
            status = "accepted_tradeoff"
        elif issue_id in open_now:
            status = "open"
        else:
            status = "fixed"
        fates.append(
            ReportIssue(issue=issue, first_seen_round=first_seen[issue_id], status=status)
        )
    return sorted(fates, key=lambda f: (f.first_seen_round, f.issue.id))


def _dismissed_ids(trace: Trace) -> set[str]:
    out: set[str] = set()
    for note in _arbiter_notes(trace):
        for entry in note.get("dismissed", []) or []:
            out.add(entry["issue_id"])
    return out


def _arbiter_notes(trace: Trace) -> list[dict]:
    """Every Arbiter note in the trace, templated ones included, in order.

    Keyed on the actor rather than on a `synthesised_by` marker: a reader wanting to tell the
    two apart looks at that field, but a reader wanting *what the Arbiter said* wants both.
    """
    return [
        event.payload.get("parsed", {}) or {}
        for event in trace.of_kind("llm_response")
        if event.actor == "arbiter"
    ]


def _tradeoff_prose(trace: Trace) -> tuple[str | None, str | None]:
    """The justification for the trade-off this run ended in, if it ended in one.

    Taken from the last note that carries one rather than the last note outright: a run that
    reaches TRADEOFF has one note per earlier REJECT in front of it, and those are
    instruction sets, not justifications.
    """
    for note in reversed(_arbiter_notes(trace)):
        if note.get("tradeoff_justification"):
            return note.get("tradeoff_justification"), note.get("recommended_default")
    return None, None


def totals(trace: Trace) -> tuple[float, int]:
    cost = sum(e.usage.cost_usd for e in trace.events if e.usage is not None)
    tokens = sum(
        e.usage.input_tokens + e.usage.output_tokens for e in trace.events if e.usage is not None
    )
    return round(cost, 6), tokens


def _narrative(trace: Trace) -> str | None:
    """The Postmortem's write-up, laid out here rather than by the model.

    Rendering on the read side is what keeps criterion S6 true with an LLM in the loop: the
    trace holds the *content*, both the live path and `replay` render it with this function,
    and the two cannot drift. It is also what makes docs/03-agents.md § 3.5's structural
    promise -- the write-up always ends on "what I would not trust" -- hold on every run
    rather than whenever the model remembers to put it last.
    """
    notes = [
        event.payload.get("parsed", {}) or {}
        for event in trace.of_kind("llm_response")
        if event.actor == "postmortem"
    ]
    if not notes:
        return None
    return render_narrative(PostmortemNote.model_validate(notes[-1]))


def render_narrative(note: PostmortemNote) -> str:
    """Lay a `PostmortemNote` out as markdown. Pure, and the only place the layout lives."""
    lines = [f"## What happened\n\n{note.headline}"]

    if note.rounds:
        lines.append("\n## Round by round\n")
        for summary in note.rounds:
            lines.append(
                f"**Round {summary.round}.** {summary.what_changed}\n\n"
                f"{summary.outcome}\n"
            )

    if note.disagreement:
        lines.append(f"\n## Where the critics disagreed\n\n{note.disagreement}")

    if note.human_should_check:
        lines.append("\n## What a human should check\n")
        lines += [f"- {item}" for item in note.human_should_check]

    # Always last, and always present: the schema makes the list non-empty and
    # `validation.py` makes it complete.
    lines.append("\n## What I would not trust\n")
    lines += [f"- {item}" for item in note.what_i_would_not_trust]
    return "\n".join(lines).strip() + "\n"


def postmortem_bundle(trace: Trace, filename: str | None = None) -> PostmortemBundle:
    """Compact a trace into the Postmortem's input.

    Lives here, beside `build`, because it reads the same events by the same rules -- and
    because `tribunal postmortem <trace>` has to compact a trace off disk with the identical
    code the orchestrator uses mid-run, or the re-runnable path is the untested one.
    """
    report = build(trace)
    verdicts = _verdicts(trace)
    by_round = _critiques_by_round(trace)
    rationales = _rationales_by_round(trace)
    consolidations = _consolidations_by_round(trace)

    rounds = tuple(
        RoundDigest(
            round=verdict.round,
            decision=verdict.decision.value,
            rule_fired=verdict.rule_fired,
            pressure=verdict.pressure,
            rationale=rationales.get(verdict.round),
            issue_ids=tuple(
                issue.id
                for critique in by_round.get(verdict.round, [])
                for issue in critique.issues
            ),
            consolidated_critique=consolidations.get(verdict.round),
        )
        for verdict in verdicts
    )
    fates = {status: [f.issue for f in report.issues if f.status == status]
             for status in ("open", "fixed", "dismissed", "accepted_tradeoff")}

    return PostmortemBundle(
        run_id=report.run_id,
        filename=filename or report.input_file,
        outcome=report.outcome,
        rule_fired=report.rule_fired,
        rounds=rounds,
        accepted_diff=report.accepted_diff,
        no_patch_reason=report.no_patch_reason,
        # A trade-off ships the patch with the objection standing, so those issues are open
        # in every sense a reader cares about -- and the narrative must account for them.
        open_issues=tuple(fates["open"] + fates["accepted_tradeoff"]),
        fixed_issues=tuple(fates["fixed"]),
        dismissed_issues=tuple(fates["dismissed"]),
        unassessed_dimensions=tuple(report.unassessed_dimensions),
        conflict=report.conflict,
        tradeoff_justification=report.tradeoff_justification,
        uncitable_measurements=_uncitable_measurements(trace),
    )


def _rationales_by_round(trace: Trace) -> dict[int, str]:
    """The Coder's rationale for the patch that was accepted in each round."""
    out: dict[int, str] = {}
    for event in trace.of_kind("llm_response"):
        if event.actor == "coder" and event.round is not None:
            out[event.round] = event.payload.get("parsed", {}).get("rationale", "")
    return out


def _consolidations_by_round(trace: Trace) -> dict[int, str]:
    out: dict[int, str] = {}
    for note in _arbiter_notes(trace):
        text = note.get("consolidated_critique")
        if text and isinstance(note.get("round"), int):
            out[note["round"]] = text
    return out


def _uncitable_measurements(trace: Trace) -> tuple[str, ...]:
    """Benchmark labels that produced no usable verdict, newest round last.

    An `inconclusive` or `unmeasurable` result is not evidence of no change, which is exactly
    why it belongs in the write-up's caveats rather than being quietly dropped.
    """
    labels: dict[str, None] = {}
    for event in trace.of_kind("tool_run"):
        if event.payload.get("tool") != "perf":
            continue
        for measurement in event.payload.get("measurements", []) or []:
            if measurement.get("verdict") in ("inconclusive", "unmeasurable"):
                labels.setdefault(measurement["label"], None)
    return tuple(labels)


def build(trace: Trace) -> Report:
    """The run's `Report`, derived entirely from recorded events."""
    header = trace.header
    verdicts = _verdicts(trace)
    final = verdicts[-1] if verdicts else None
    ends = trace.of_kind("run_end")
    end = ends[-1] if ends else None

    diff, no_patch_reason = _accepted_diff(trace, final)
    cost, tokens = totals(trace)
    justification, recommended = _tradeoff_prose(trace)

    return Report(
        run_id=header.run_id,
        input_file=header.payload["input_file"],
        input_sha256=header.payload["input_sha256"],
        outcome=final.decision if final else Decision.ESCALATE,
        rule_fired=final.rule_fired if final else "no_decision_recorded",
        terminal_state=(end.payload.get("terminal_state", "DONE") if end else "DONE"),
        rounds_used=max((v.round for v in verdicts), default=0),
        accepted_diff=diff,
        no_patch_reason=no_patch_reason,
        issues=_issue_fates(trace, final),
        conflict=final.conflict if final else None,
        tradeoff_justification=justification,
        recommended_default=recommended,
        narrative=_narrative(trace),
        pressure_history=final.pressure_history if final else [],
        unassessed_dimensions=final.unassessed_dimensions if final else [],
        total_cost_usd=cost,
        total_tokens=tokens,
        wall_seconds=end.payload["wall_seconds"] if end else 0.0,
    )


# --------------------------------------------------------------------------------------------
# Retroactive re-deciding
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Redecision:
    """What a recorded round would have decided under a different configuration."""

    round: int
    recorded: Verdict
    hypothetical: Verdict

    @property
    def changed(self) -> bool:
        return (self.recorded.decision, self.recorded.rule_fired) != (
            self.hypothetical.decision,
            self.hypothetical.rule_fired,
        )


def redecide(trace: Trace, config: PolicyConfig) -> list[Redecision]:
    """Re-run the decision table over recorded critiques under a different config.

    docs/10 § Replay: "change `accept_threshold` and ask what this run would have decided --
    against real recorded critiques, retroactively, for free. That is how you tune thresholds
    on evidence rather than intuition."

    Only the inputs the trace actually carries are reconstructed. Budget spend is taken from
    the recorded `budget_check` events, so a re-decision cannot accidentally conclude something
    the original run could not have.
    """
    by_round = _critiques_by_round(trace)
    recorded = {v.round: v for v in _verdicts(trace)}
    history: list[float] = []
    hashes: list[str] = []
    out: list[Redecision] = []

    for round_ in sorted(recorded):
        critiques = tuple(by_round.get(round_, ()))
        assessed = {c.dimension for c in critiques}
        errored = tuple(
            d for d in (Dimension.SECURITY, Dimension.PERFORMANCE) if d not in assessed
        )
        this_hash = _normalised_hash(trace, round_)
        original = recorded[round_]
        hypothetical = decide(
            PolicyInput(
                round=round_,
                critiques=critiques,
                errored_dimensions=errored,
                diff_hash=this_hash,
                previous_diff_hashes=tuple(hashes),
                pressure_history=tuple(history),
                dismissed_issue_ids=frozenset(_dismissed_ids(trace)),
                conflict=_conflict_of(original),
                spend=_spend_at(trace, round_),
            ),
            config,
        )
        out.append(Redecision(round=round_, recorded=original, hypothetical=hypothetical))
        history.append(hypothetical.pressure)
        if this_hash:
            hashes.append(this_hash)
    return out


def _conflict_of(verdict: Verdict) -> Conflict | None:
    return verdict.conflict


def _normalised_hash(trace: Trace, round: int) -> str | None:
    applied = [
        e.payload
        for e in trace.of_kind("patch_validate")
        if e.round == round and e.payload.get("applied")
    ]
    return applied[-1].get("normalised_sha256") if applied else None


def _spend_at(trace: Trace, round: int) -> Spend:
    checks = [e for e in trace.of_kind("budget_check") if (e.round or 0) <= round]
    if not checks:
        return Spend()
    spent = checks[-1].payload["spent"]
    return Spend(
        usd=spent["usd"], tokens=spent["tokens"], wall_seconds=spent["wall_seconds"], rounds=round
    )


def summary_row(report: Report, trace: Trace, settings: Settings) -> dict:
    """One line for `traces/summary.jsonl`. The dataset docs/06 § Metrics asks for."""
    responses = trace.of_kind("llm_response")
    retries = sum(e.payload.get("parse_retries", 0) for e in responses)
    cache_reads = sum(
        e.usage.cache_read_input_tokens for e in responses if e.usage is not None
    )
    cache_eligible = sum(
        e.usage.cache_read_input_tokens + e.usage.input_tokens
        for e in responses
        if e.usage is not None
    )
    return {
        "run_id": report.run_id,
        "input_file": report.input_file,
        "input_sha256": report.input_sha256,
        "outcome": report.outcome.value,
        "rule_fired": report.rule_fired,
        "rounds_used": report.rounds_used,
        "pressure_history": report.pressure_history,
        "unassessed": [d.value for d in report.unassessed_dimensions],
        "issues": {
            status: sum(1 for i in report.issues if i.status == status)
            for status in ("fixed", "open", "accepted_tradeoff", "dismissed")
        },
        "total_cost_usd": report.total_cost_usd,
        "total_tokens": report.total_tokens,
        "wall_seconds": report.wall_seconds,
        # The prompt-health signal. Tracked from the first run because it cannot be
        # reconstructed afterwards.
        "parse_retries": retries,
        "cache_read_ratio": round(cache_reads / cache_eligible, 3) if cache_eligible else 0.0,
        "prompt_versions": trace.header.payload.get("prompt_versions", {}),
        "price_table_version": trace.header.payload.get("config", {}).get(
            "price_table_version", "unknown"
        ),
        "schema_version": trace.header.payload.get("schema_version"),
    }
