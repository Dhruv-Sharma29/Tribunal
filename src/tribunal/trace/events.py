"""Event builders for the JSONL trace.

docs/06-observability.md principle 1: **the trace is the source of truth, not a log.** The
report, the eval metrics and `tribunal replay` all derive from it. So an event carries the
*data*, not a rendered sentence about the data -- `policy_decision` holds the whole `Verdict`,
and `llm_response` holds the parsed output, because a later reader has to be able to re-decide
from them (docs/10 § Replay: "change `accept_threshold` and ask what this run would have
decided").

**Redaction is a level, not a policy scattered through the code.** At the default level an
`llm_request` records the prompt *version* and a hash of the rendered input; at
`--trace-level=full` it records the prompt bodies too. Traces get pasted into issue reports and
demo GIFs, so the safe level is the default one.

One builder per `kind`. They exist so that the payload shape for a kind is defined in exactly
one place -- docs/02-contracts.md says the payload is "kind-specific, schema-checked per kind",
and a dict literal at each call site is how that stops being true.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from tribunal.contracts import SCHEMA_VERSION, TraceEvent, Usage, Verdict

#: Payload keys every `llm_request` carries at the default trace level.
SAFE_REQUEST_KEYS = ("model", "provider", "effort", "prompt_version", "input_hash")


#: Bound before any builder shadows the name. Every builder takes a `round` parameter -- the
#: trace field is called `round` -- so calling the builtin inside one raises
#: `TypeError: 'int' object is not callable`. Aliasing it once is less fragile than remembering
#: not to use it.
_round = round


def now_rfc3339() -> str:
    """RFC 3339, UTC, second precision.

    Second precision on purpose: the timestamp is for ordering and for the latency waterfall,
    and `duration_ms` carries the precision. A microsecond field would be one more thing that
    differs between two runs of the same input.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _event(
    kind: str,
    actor: str,
    payload: dict[str, Any],
    seq: int,
    run_id: str,
    round: int | None = None,
    usage: Usage | None = None,
    duration_ms: int | None = None,
    ts: str | None = None,
) -> TraceEvent:
    return TraceEvent(
        seq=seq,
        ts=ts or now_rfc3339(),
        run_id=run_id,
        round=round,
        kind=kind,  # type: ignore[arg-type]
        actor=actor,
        payload=payload,
        usage=usage,
        duration_ms=duration_ms,
    )


def run_start(
    seq: int,
    run_id: str,
    *,
    input_file: str,
    input_sha256: str,
    config_snapshot: dict[str, Any],
    prompt_versions: dict[str, str],
    argv: list[str],
    trace_level: str,
) -> TraceEvent:
    """The header. Always `seq == 0`, always the first line.

    Carries everything needed to interpret the rest without the code that produced it: schema
    version, the config snapshot (which includes the price-table version), the model ids and
    the prompt versions. Without the prompt versions a later reader cannot tell whether a
    score moved because of a prompt change or a code change, and they cannot be retrofitted.
    """
    return _event(
        "run_start",
        "orchestrator",
        {
            "schema_version": SCHEMA_VERSION,
            "input_file": input_file,
            "input_sha256": input_sha256,
            "config": config_snapshot,
            "prompt_versions": prompt_versions,
            "argv": argv,
            "trace_level": trace_level,
        },
        seq,
        run_id,
    )


def state_enter(
    seq: int, run_id: str, state: str, from_state: str | None, round: int | None
) -> TraceEvent:
    payload = {"state": state, "from_state": from_state}
    return _event("state_enter", "orchestrator", payload, seq, run_id, round)


def state_exit(
    seq: int, run_id: str, state: str, round: int | None, duration_ms: int
) -> TraceEvent:
    return _event(
        "state_exit", "orchestrator", {"state": state}, seq, run_id, round,
        duration_ms=duration_ms,
    )


def llm_request(
    seq: int,
    run_id: str,
    *,
    actor: str,
    round: int | None,
    model: str,
    provider: str,
    effort: str,
    prompt_version: str,
    system: str,
    user: str,
    cache_breakpoints: int,
    full: bool,
) -> TraceEvent:
    payload: dict[str, Any] = {
        "model": model,
        "provider": provider,
        "effort": effort,
        "prompt_version": prompt_version,
        "input_hash": text_hash(system + user),
        # Zero cache reads across rounds means a silent prefix invalidator, and docs/06 warns
        # you will not notice any other way until the bill arrives. Recording the breakpoint
        # count is what makes the ratio interpretable.
        "cache_breakpoints": cache_breakpoints,
    }
    if full:
        payload["system"] = system
        payload["user"] = user
    return _event("llm_request", actor, payload, seq, run_id, round)


def llm_response(
    seq: int,
    run_id: str,
    *,
    actor: str,
    round: int | None,
    parsed: dict[str, Any],
    stop_reason: str | None,
    structure: str,
    parse_retries: int,
    local_repairs: list[str],
    transient_retries: int,
    request_hash: str,
    replayed: bool,
    usage: Usage | None,
    duration_ms: int,
    metrics: dict[str, Any] | None = None,
) -> TraceEvent:
    """The parsed output goes in whole, not summarised.

    That is what makes retroactive re-deciding possible: a reader with the critiques can run
    `policy.decide` again under a different threshold and ask what this run *would* have
    decided, for free.
    """
    return _event(
        "llm_response",
        actor,
        {
            "parsed": parsed,
            "stop_reason": stop_reason,
            "structure": structure,
            "parse_retries": parse_retries,
            "local_repairs": local_repairs,
            "transient_retries": transient_retries,
            "request_hash": request_hash,
            "replayed": replayed,
            "metrics": metrics or {},
        },
        seq,
        run_id,
        round,
        usage=usage,
        duration_ms=duration_ms,
    )


def tool_run(
    seq: int,
    run_id: str,
    *,
    round: int | None,
    tool: str,
    argv: list[str],
    exit_code: int | None,
    findings_count: int,
    duration_ms: int,
    stdout_excerpt: str,
    error: str | None,
    measurements: list[dict[str, Any]] | None = None,
    findings: list[dict[str, Any]] | None = None,
) -> TraceEvent:
    """One tool's run.

    `measurements` carries `PerfMeasurement`s verbatim, and only `perf` supplies any. They go
    in the trace rather than only in the in-memory `GroundingReport` because an `inconclusive`
    or `unmeasurable` benchmark is something the write-up has to disclose -- it is not
    evidence of no change -- and a reader working from the file has no other way to learn one
    happened. Omitted rather than empty for every other tool, so existing payloads are
    unchanged.
    """
    payload = {
        "tool": tool,
        "argv": argv,
        "exit_code": exit_code,
        "findings_count": findings_count,
        "stdout_excerpt": stdout_excerpt[:2000],
        "error": error,
    }
    if findings:
        payload["findings"] = findings
    if measurements:
        payload["measurements"] = measurements
    return _event(
        "tool_run",
        tool,
        payload,
        seq,
        run_id,
        round,
        duration_ms=duration_ms,
    )


def patch_validate(
    seq: int,
    run_id: str,
    *,
    round: int,
    attempt: int,
    applied: bool,
    parse_ok: bool,
    hunks: int,
    diff_sha256: str,
    normalised_sha256: str,
    failure_reason: str | None,
    diff: str,
) -> TraceEvent:
    return _event(
        "patch_validate",
        "code",
        {
            "attempt": attempt,
            "applied": applied,
            "parse_ok": parse_ok,
            "hunks": hunks,
            "diff_sha256": diff_sha256,
            # The oscillation guard hashes the *normalised* diff, so the guard is only
            # auditable from a trace if the normalised hash is in it.
            "normalised_sha256": normalised_sha256,
            "failure_reason": failure_reason,
            "diff": diff,
        },
        seq,
        run_id,
        round,
    )


def policy_decision(
    seq: int, run_id: str, verdict: Verdict, duplicates: list[str]
) -> TraceEvent:
    """The money event.

    Any outcome in any trace is explainable by one grep, because `rule_fired` names the exact
    decision-table row that produced it. `duplicates` records what pressure collapsing dropped,
    so a number the eval depends on is never silently different from the sum of the issues.
    """
    return _event(
        "policy_decision",
        "policy",
        {**verdict.model_dump(mode="json"), "collapsed_duplicates": duplicates},
        seq,
        run_id,
        verdict.round,
    )


def budget_check(
    seq: int,
    run_id: str,
    *,
    round: int | None,
    state: str,
    usd: float,
    tokens: int,
    wall_seconds: float,
    caps: dict[str, Any],
    breached: str | None,
) -> TraceEvent:
    return _event(
        "budget_check",
        "orchestrator",
        {
            "state": state,
            "spent": {
                "usd": round_usd(usd),
                "tokens": tokens,
                "wall_seconds": _round(wall_seconds, 1),
            },
            "caps": caps,
            "breached": breached,
        },
        seq,
        run_id,
        round,
    )


def error(
    seq: int,
    run_id: str,
    *,
    actor: str,
    round: int | None,
    exception: str,
    message: str,
    recovered: bool,
) -> TraceEvent:
    return _event(
        "error",
        actor,
        {"exception": exception, "message": message[:2000], "recovered": recovered},
        seq,
        run_id,
        round,
    )


def run_end(
    seq: int,
    run_id: str,
    *,
    terminal_state: str,
    outcome: str,
    rule_fired: str,
    rounds_used: int,
    total_cost_usd: float,
    total_tokens: int,
    wall_seconds: float,
) -> TraceEvent:
    return _event(
        "run_end",
        "orchestrator",
        {
            "terminal_state": terminal_state,
            "outcome": outcome,
            "rule_fired": rule_fired,
            "rounds_used": rounds_used,
            "total_cost_usd": round_usd(total_cost_usd),
            "total_tokens": total_tokens,
            "wall_seconds": _round(wall_seconds, 1),
        },
        seq,
        run_id,
    )


def round_usd(value: float) -> float:
    return _round(value, 6)
