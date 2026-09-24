"""The trace layer on its own: writer, reader, and the report builder.

`tests/test_orchestrator.py` proves these work together over real runs. This file pins the
behaviours that only show up when something has gone *wrong* with a trace -- a gap, a truncated
file, a future schema -- because those are the cases a passing end-to-end run can never reach,
and they are the ones docs/06-observability.md is most specific about.
"""

from __future__ import annotations

import json

import pytest

from tribunal.config import PolicyConfig, Settings
from tribunal.contracts import (
    SCHEMA_VERSION,
    Conflict,
    Decision,
    Dimension,
    Usage,
    Verdict,
)
from tribunal.trace import events
from tribunal.trace import reader as trace_reader
from tribunal.trace import report as report_builder
from tribunal.trace.reader import Trace, TraceError, UnsupportedSchema
from tribunal.trace.writer import TraceWriter, append_summary, trace_file

RUN = "01JBTESTTESTTESTTESTTESTTE"


def header(**overrides) -> dict:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "input_file": "t.py",
        "input_sha256": "abc",
        "config": {"price_table_version": "2026-01"},
        "prompt_versions": {"coder": "coder/v3"},
        "argv": [],
        "trace_level": "default",
    }
    payload.update(overrides)
    return {
        "seq": 0, "ts": "2026-01-01T00:00:00Z", "run_id": RUN, "round": None,
        "kind": "run_start", "actor": "orchestrator", "payload": payload,
        "usage": None, "duration_ms": None,
    }


def line(seq: int, kind: str = "state_enter", run_id: str = RUN, **payload) -> dict:
    return {
        "seq": seq, "ts": "2026-01-01T00:00:00Z", "run_id": run_id, "round": None,
        "kind": kind, "actor": "orchestrator",
        "payload": payload or {"state": "GROUND", "from_state": "INIT"},
        "usage": None, "duration_ms": None,
    }


def jsonl(*objects: dict) -> list[str]:
    return [json.dumps(o) for o in objects]


# -- writer -------------------------------------------------------------------------------


def test_the_sequence_counter_is_assigned_in_exactly_one_place():
    """`emit` takes the *builder*, not a built event, so the counter cannot be read, used and
    then not incremented -- the one way a gap-free counter goes wrong."""
    writer = TraceWriter(RUN)
    for state in ("GROUND", "PROPOSE", "VALIDATE"):
        writer.emit(events.state_enter, state=state, from_state="INIT", round=None)

    assert [e.seq for e in writer.events] == [0, 1, 2]
    assert writer.next_seq == 3
    assert {e.run_id for e in writer.events} == {RUN}


def test_every_line_is_flushed_as_it_is_written(tmp_path):
    """A run killed halfway must still leave an interpretable trace -- which is when you most
    want one. So the assertion is made while the handle is still open."""
    path = tmp_path / "t.jsonl"
    writer = TraceWriter(RUN, path).open()
    writer.emit(events.state_enter, state="GROUND", from_state="INIT", round=None)

    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    writer.emit(events.state_enter, state="PROPOSE", from_state="GROUND", round=None)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
    writer.close()


def test_a_subscriber_sees_the_same_stream_as_the_file(tmp_path):
    """docs/06 principle 4: the CLI progress renderer is a subscriber over the one event
    stream, not a second code path that prints."""
    seen = []
    writer = TraceWriter(RUN, tmp_path / "t.jsonl").open()
    writer.subscribe(seen.append)
    writer.emit(events.state_enter, state="GROUND", from_state="INIT", round=None)
    writer.emit(events.state_exit, state="GROUND", round=None, duration_ms=5)
    writer.close()

    assert [e.kind for e in seen] == ["state_enter", "state_exit"]
    assert seen == writer.events
    assert len((tmp_path / "t.jsonl").read_text().splitlines()) == 2


def test_the_context_manager_names_the_file_after_the_run(tmp_path):
    with trace_file(RUN, tmp_path) as writer:
        writer.emit(events.state_enter, state="GROUND", from_state="INIT", round=None)
    assert (tmp_path / f"{RUN}.jsonl").is_file()


def test_the_writer_works_with_no_file_at_all():
    writer = TraceWriter(RUN).open()
    writer.emit(events.state_enter, state="GROUND", from_state="INIT", round=None)
    writer.close()
    assert len(writer.events) == 1


def test_the_summary_file_grows_one_line_per_run(tmp_path):
    append_summary(tmp_path / "traces", {"run_id": "a", "outcome": "accept"})
    path = append_summary(tmp_path / "traces", {"run_id": "b", "outcome": "tradeoff"})
    rows = [json.loads(row) for row in path.read_text().splitlines()]
    assert [r["run_id"] for r in rows] == ["a", "b"]


# -- reader -------------------------------------------------------------------------------


def test_a_trace_round_trips_through_the_file(tmp_path):
    path = tmp_path / "t.jsonl"
    writer = TraceWriter(RUN, path).open()
    writer.emit(
        events.run_start, input_file="t.py", input_sha256="abc",
        config_snapshot={}, prompt_versions={}, argv=[], trace_level="default",
    )
    writer.emit(
        events.run_end, terminal_state="DONE", outcome="accept", rule_fired="accept",
        rounds_used=1, total_cost_usd=0.01, total_tokens=10, wall_seconds=1.0,
    )
    writer.close()

    trace = trace_reader.read(path, strict=True)
    assert trace.run_id == RUN
    assert [e.kind for e in trace.events] == ["run_start", "run_end"]
    assert trace.header.payload["schema_version"] == SCHEMA_VERSION


def test_blank_lines_are_skipped_rather_than_fatal():
    trace = trace_reader.parse(iter(jsonl(header()) + ["", "   "]))
    assert len(trace.events) == 1


def test_an_empty_file_is_not_a_trace():
    with pytest.raises(TraceError, match="not even a run_start header"):
        trace_reader.parse(iter([]))


def test_a_header_that_is_not_first_is_refused():
    """A trace is only self-describing if its header is first."""
    with pytest.raises(TraceError, match="must be run_start"):
        trace_reader.parse(iter(jsonl(line(0), header())))


def test_a_malformed_line_names_its_line_number():
    with pytest.raises(TraceError, match="line 2 is not a valid trace event"):
        trace_reader.parse(iter(jsonl(header()) + ["{not json"]))


def test_a_future_schema_major_is_refused_rather_than_half_rendered():
    """docs/02 § Schema evolution: "the viewer refuses unknown majors rather than rendering
    garbage". The version it could not read is named, because the next question is always
    which build wrote it."""
    with pytest.raises(UnsupportedSchema, match="schema 2.0"):
        trace_reader.parse(iter(jsonl(header(schema_version="2.0"))))


def test_an_older_minor_is_a_warning_not_a_refusal():
    trace = trace_reader.parse(iter(jsonl(header(schema_version="1.9"))))
    assert any("1.9" in w for w in trace.warnings)
    assert len(trace.events) == 1


def test_a_sequence_gap_is_reported_as_lost_events_not_renumbered():
    """The writer assigns `seq` and nothing else does, so a gap means events were lost. A
    warning rather than an exception: a truncated trace is still worth reading."""
    trace = trace_reader.parse(iter(jsonl(header(), line(3))))
    assert any("2 event(s) lost" in w for w in trace.warnings)
    # Counting resumes from what was found, so one gap does not warn about every later event.
    assert len([w for w in trace.warnings if "sequence gap" in w]) == 1


def test_a_file_mixing_two_runs_is_flagged():
    trace = trace_reader.parse(iter(jsonl(header(), line(1, run_id="OTHER"))))
    assert any("more than one run_id" in w for w in trace.warnings)


def test_a_trace_with_no_run_end_says_the_run_was_killed():
    trace = trace_reader.parse(iter(jsonl(header())))
    assert any("killed or is still in flight" in w for w in trace.warnings)


def test_strict_turns_every_warning_into_an_error():
    with pytest.raises(TraceError, match="killed or is still in flight"):
        trace_reader.parse(iter(jsonl(header())), strict=True)


def test_of_kind_and_by_actor_select_what_the_report_builder_needs():
    trace = trace_reader.parse(iter(jsonl(header(), line(1), line(2, kind="state_exit"))))
    assert len(trace.of_kind("state_enter", "state_exit")) == 2
    assert len(trace.by_actor("orchestrator")) == 3
    assert trace.of_kind("policy_decision") == []


# -- the report builder ---------------------------------------------------------------------


def verdict(**overrides) -> Verdict:
    defaults = dict(
        round=1, decision=Decision.ACCEPT, rule_fired="accept", pressure=0.0,
        pressure_history=[0.0], open_issues=[], unassessed_dimensions=[], conflict=None,
    )
    return Verdict(**{**defaults, **overrides})


def trace_of(*built) -> Trace:
    writer = TraceWriter(RUN)
    writer.emit(
        events.run_start, input_file="t.py", input_sha256="abc",
        config_snapshot={}, prompt_versions={}, argv=[], trace_level="default",
    )
    for build, kwargs in built:
        writer.emit(build, **kwargs)
    writer.emit(
        events.run_end, terminal_state="DONE", outcome="accept", rule_fired="accept",
        rounds_used=1, total_cost_usd=0.0, total_tokens=0, wall_seconds=2.5,
    )
    return Trace(events=list(writer.events))


def test_a_trace_with_no_decision_reports_that_rather_than_guessing():
    report = report_builder.build(trace_of())
    assert report.outcome is Decision.ESCALATE
    assert report.rule_fired == "no_decision_recorded"
    assert report.rounds_used == 0
    assert report.accepted_diff is None
    assert report.wall_seconds == 2.5


def test_the_verdict_is_reconstructed_from_a_flat_payload():
    """`policy_decision` holds the verdict's fields flat, because the README's one-liner is
    `jq '.payload.rule_fired'`. `Verdict` forbids extra fields, so the read side filters --
    the write side does not give up greppability to make parsing easier."""
    original = verdict(decision=Decision.REJECT, rule_fired="hard_block_security",
                       pressure=15.2, pressure_history=[15.2], open_issues=["SEC-1"])
    trace = trace_of((events.policy_decision, {"verdict": original, "duplicates": ["SEC-2"]}))

    payload = trace.of_kind("policy_decision")[0].payload
    assert payload["rule_fired"] == "hard_block_security"  # flat, greppable
    assert payload["collapsed_duplicates"] == ["SEC-2"]  # and not a Verdict field

    report = report_builder.build(trace)
    assert report.rule_fired == "hard_block_security"
    assert report.outcome is Decision.REJECT
    assert report.pressure_history == [15.2]


def test_costs_are_summed_from_the_usage_on_each_event():
    def usage(cost: float) -> Usage:
        return Usage(model="m", input_tokens=100, output_tokens=50, cost_usd=cost,
                     request_id="r")

    trace = trace_of(*[
        (events.llm_response, {
            "actor": actor, "round": 1, "parsed": {}, "stop_reason": "stop",
            "structure": "native_json", "parse_retries": 0, "local_repairs": [],
            "transient_retries": 0, "request_hash": "h", "replayed": False,
            "usage": usage(cost), "duration_ms": 10,
        })
        for actor, cost in (("coder", 0.001), ("redteam", 0.002))
    ])
    assert report_builder.totals(trace) == (0.003, 300)


def test_an_escalated_run_still_points_at_its_best_patch():
    """docs/04 § Budget enforcement: losing the work is not a legitimate outcome."""
    trace = trace_of(
        (events.patch_validate, {
            "round": 1, "attempt": 1, "applied": True, "parse_ok": True, "hunks": 1,
            "diff_sha256": "a", "normalised_sha256": "n", "failure_reason": None,
            "diff": "--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n",
        }),
        (events.policy_decision, {
            "verdict": verdict(decision=Decision.ESCALATE, rule_fired="budget_exhausted"),
            "duplicates": [],
        }),
    )
    report = report_builder.build(trace)
    assert report.accepted_diff is None
    assert "escalated: budget_exhausted" in report.no_patch_reason
    assert "best patch" in report.no_patch_reason


def test_a_patch_that_never_parsed_is_not_offered_as_the_best_one():
    trace = trace_of(
        (events.patch_validate, {
            "round": 1, "attempt": 1, "applied": True, "parse_ok": False, "hunks": 1,
            "diff_sha256": "a", "normalised_sha256": "n", "failure_reason": "syntax error",
            "diff": "--- a\n+++ b\n",
        }),
        (events.policy_decision, {
            "verdict": verdict(decision=Decision.ACCEPT), "duplicates": [],
        }),
    )
    report = report_builder.build(trace)
    assert report.accepted_diff is None
    assert report.no_patch_reason == "no patch applied in the deciding round"


def test_the_conflicted_pair_is_reported_as_accepted_not_as_open():
    conflict = Conflict(
        left_issue="PERF-1", right_issue="SEC-1",
        left_remedy_cost="slower", right_remedy_cost="unsafe",
        axis="security_vs_performance", detector="same_span",
    )
    decision = verdict(decision=Decision.TRADEOFF, rule_fired="irreconcilable",
                       open_issues=["SEC-1", "PERF-1"], conflict=conflict)
    trace = trace_of(
        (events.llm_response, {
            "actor": "redteam", "round": 1, "stop_reason": "stop", "structure": "native_json",
            "parse_retries": 0, "local_repairs": [], "transient_retries": 0,
            "request_hash": "h", "replayed": False, "usage": None, "duration_ms": 1,
            "parsed": {
                "dimension": "security", "round": 1, "verdict": "concerns",
                "positive_notes": [], "tools_consulted": ["bandit"], "summary": "s",
                "issues": [{
                    "id": "SEC-1", "dimension": "security", "severity": "medium",
                    "title": "t", "explanation": "e", "confidence": 0.9,
                    "introduced_by_patch": False, "suggested_direction": "d",
                    "evidence": [{"kind": "code_span", "ref": "t.py:L1-L2", "excerpt": "x"}],
                }],
            },
        }),
        (events.policy_decision, {"verdict": decision, "duplicates": []}),
    )
    report = report_builder.build(trace)
    assert [(f.issue.id, f.status) for f in report.issues] == [("SEC-1", "accepted_tradeoff")]
    assert report.conflict == conflict


def test_redecide_on_a_trace_with_no_decisions_is_empty():
    assert report_builder.redecide(trace_of(), PolicyConfig()) == []


def test_summary_row_carries_the_prompt_health_signal():
    trace = trace_of(
        (events.llm_response, {
            "actor": "coder", "round": 1, "parsed": {}, "stop_reason": "stop",
            "structure": "native_json", "parse_retries": 2, "local_repairs": ["a"],
            "transient_retries": 0, "request_hash": "h", "replayed": False,
            "usage": Usage(model="m", input_tokens=900, output_tokens=50, cost_usd=0.01,
                           request_id="r", cache_read_input_tokens=100),
            "duration_ms": 10,
        }),
        (events.policy_decision, {"verdict": verdict(), "duplicates": []}),
    )
    row = report_builder.summary_row(report_builder.build(trace), trace, Settings())

    assert row["parse_retries"] == 2
    assert row["cache_read_ratio"] == 0.1  # 100 / (100 + 900)
    assert row["schema_version"] == SCHEMA_VERSION
    assert row["outcome"] == "accept"
    assert row["issues"] == {"fixed": 0, "open": 0, "accepted_tradeoff": 0, "dismissed": 0}


def test_summary_row_does_not_divide_by_zero_on_a_run_that_made_no_calls():
    trace = trace_of((events.policy_decision, {"verdict": verdict(), "duplicates": []}))
    assert report_builder.summary_row(report_builder.build(trace), trace, Settings())[
        "cache_read_ratio"
    ] == 0.0


# -- the event builders themselves -----------------------------------------------------------


def test_timestamps_are_second_precision_so_two_runs_differ_less():
    assert events.now_rfc3339().endswith("Z")
    assert len(events.now_rfc3339()) == len("2026-01-01T00:00:00Z")


def test_prompt_bodies_are_recorded_only_at_the_full_trace_level():
    """Traces get pasted into issue reports and demo GIFs, so the safe level is the default."""
    default = events.llm_request(
        0, RUN, actor="coder", round=1, model="m", provider="nim", effort="high",
        prompt_version="coder/v3", system="SYSTEM", user="USER", cache_breakpoints=1,
        full=False,
    )
    assert "system" not in default.payload
    assert default.payload["input_hash"] == events.text_hash("SYSTEMUSER")
    assert set(events.SAFE_REQUEST_KEYS) <= set(default.payload)

    full = events.llm_request(
        0, RUN, actor="coder", round=1, model="m", provider="nim", effort="high",
        prompt_version="coder/v3", system="SYSTEM", user="USER", cache_breakpoints=1,
        full=True,
    )
    assert full.payload["system"] == "SYSTEM"


def test_a_long_tool_excerpt_is_truncated_rather_than_dropped():
    event = events.tool_run(
        0, RUN, round=1, tool="bandit", argv=["bandit"], exit_code=0, findings_count=0,
        duration_ms=1, stdout_excerpt="x" * 5000, error=None,
    )
    assert len(event.payload["stdout_excerpt"]) == 2000


def test_a_policy_decision_carries_its_round_without_being_told():
    event = events.policy_decision(0, RUN, verdict(round=3), duplicates=[])
    assert event.round == 3
    assert event.payload["unassessed_dimensions"] == []


def test_the_budget_check_rounds_what_it_records():
    event = events.budget_check(
        0, RUN, round=1, state="PROPOSE", usd=0.1234567891, tokens=10,
        wall_seconds=1.2345, caps={"max_usd": 2.0}, breached=None,
    )
    assert event.payload["spent"] == {"usd": 0.123457, "tokens": 10, "wall_seconds": 1.2}


def test_an_error_message_is_capped():
    event = events.error(
        0, RUN, actor="profiler", round=1, exception="RuntimeError",
        message="y" * 5000, recovered=True,
    )
    assert len(event.payload["message"]) == 2000
    assert event.payload["recovered"] is True


def test_unassessed_dimensions_survive_the_round_trip_as_enums():
    trace = trace_of((events.policy_decision, {
        "verdict": verdict(decision=Decision.ESCALATE, rule_fired="unassessed_terminal",
                           unassessed_dimensions=[Dimension.PERFORMANCE]),
        "duplicates": [],
    }))
    assert report_builder.build(trace).unassessed_dimensions == [Dimension.PERFORMANCE]
