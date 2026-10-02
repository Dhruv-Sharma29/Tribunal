"""CLI behaviour, focused on the two things a user can get wrong.

`doctor` is the command that catches a silently degraded install, and `ground --test` is the
command that must refuse to execute without an explicit flag.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from tribunal.cli import ExitCode, app
from tribunal.contracts import Conflict, Decision, Dimension, Usage, Verdict
from tribunal.fsm import Machine, State
from tribunal.orchestrator import RunResult
from tribunal.trace import events
from tribunal.trace import report as report_builder
from tribunal.trace.reader import Trace
from tribunal.trace.writer import TraceWriter

runner = CliRunner()


def test_exit_codes_match_the_documented_contract():
    """Shell scripts and CI depend on these, so they are pinned."""
    assert (ExitCode.ACCEPT, ExitCode.TRADEOFF, ExitCode.ESCALATE) == (0, 1, 2)
    assert (ExitCode.FAILED, ExitCode.BUDGET_EXHAUSTED) == (3, 4)


def test_doctor_reports_tool_versions():
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == ExitCode.ACCEPT
    for tool in ("bandit", "ruff", "radon", "astgate", "pytest"):
        assert tool in result.output


def test_doctor_is_honest_about_network_isolation():
    """The README claim is "network access is not blocked" under subprocess. `doctor` has to
    say the same thing, or the weaker layer looks like the stronger one."""
    result = runner.invoke(app, ["doctor"])
    assert "network blocked" in result.output
    assert "docker" in result.output


def test_doctor_fails_when_a_grounding_tool_is_missing(monkeypatch):
    """A missing tool degrades a critic from grounded to opinion, which is the failure mode the
    project exists to avoid. It must be an error, not a warning buried in output."""
    import tribunal.cli as cli

    monkeypatch.setattr(
        cli, "_module_version", lambda module: None if module == "bandit" else "1.0"
    )
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == ExitCode.USAGE
    assert "Missing grounding tools" in result.output


def test_ground_prints_normalised_findings(tmp_path):
    target = tmp_path / "vuln.py"
    target.write_text("import subprocess\nsubprocess.run('ls', shell=True)\n", encoding="utf-8")
    result = runner.invoke(app, ["ground", str(target)])
    assert result.exit_code == ExitCode.ACCEPT
    assert "B602" in result.output
    assert "shell-true" in result.output


def test_ground_emits_json_on_request(tmp_path):
    import json

    target = tmp_path / "vuln.py"
    target.write_text("import subprocess\nsubprocess.run('ls', shell=True)\n", encoding="utf-8")
    result = runner.invoke(app, ["ground", str(target), "--json"])
    assert result.exit_code == ExitCode.ACCEPT
    payload = json.loads(result.output)
    assert payload["target"] == "original"
    assert payload["round"] is None
    assert any(f["rule"] == "B602" for f in payload["findings"])


def test_ground_refuses_a_test_file_without_allow_exec(tmp_path):
    target = tmp_path / "t.py"
    target.write_text("x = 1\n", encoding="utf-8")
    test = tmp_path / "test_t.py"
    test.write_text("def test_x(): assert True\n", encoding="utf-8")
    result = runner.invoke(app, ["ground", str(target), "--test", str(test)])
    assert result.exit_code == ExitCode.USAGE
    assert "--allow-exec" in result.output
    assert "module-level code" in result.output  # says *why*, not just that it refused


def test_ground_runs_the_oracle_with_allow_exec(tmp_path):
    target = tmp_path / "t.py"
    target.write_text("def double(n):\n    return n * 2\n", encoding="utf-8")
    test = tmp_path / "test_t.py"
    test.write_text(
        "from t import double\n\ndef test_x():\n    assert double(2) == 4\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["ground", str(target), "--test", str(test), "--allow-exec"])
    assert result.exit_code == ExitCode.ACCEPT
    assert "1 passed" in result.output


def test_ground_rejects_a_missing_file():
    result = runner.invoke(app, ["ground", "/nonexistent/file.py"])
    assert result.exit_code == ExitCode.USAGE
    assert "no such file" in result.output


# -- `run` and `replay` ------------------------------------------------------------------------
#
# `_print_run` is only reachable through `tribunal run`, so nothing below stubs the renderer:
# the point is to execute it. The orchestrator itself is replaced, because a real run needs a
# credential and the loop is covered end to end in `tests/test_orchestrator.py`.

RUN_ID = "01JBCLITESTCLITESTCLITESTC"


def _verdict(**overrides) -> Verdict:
    defaults = dict(
        round=1, decision=Decision.ACCEPT, rule_fired="accept", pressure=0.0,
        pressure_history=[0.0], open_issues=[], unassessed_dimensions=[], conflict=None,
    )
    return Verdict(**{**defaults, **overrides})


def _trace(*extra, path: Path | None = None) -> Trace:
    writer = TraceWriter(RUN_ID, path).open()
    writer.emit(
        events.run_start, input_file="t.py", input_sha256="abc",
        config_snapshot={"price_table_version": "2026-01"}, prompt_versions={"coder": "c/v1"},
        argv=[], trace_level="default",
    )
    writer.emit(
        events.patch_validate, round=1, attempt=1, applied=True, parse_ok=True, hunks=1,
        diff_sha256="a", normalised_sha256="n", failure_reason=None,
        diff="--- a/t.py\n+++ b/t.py\n@@ -1 +1 @@\n-old\n+new\n",
    )
    for build, kwargs in extra:
        writer.emit(build, **kwargs)
    writer.emit(
        events.run_end, terminal_state="DONE", outcome="accept", rule_fired="accept",
        rounds_used=1, total_cost_usd=0.01, total_tokens=150, wall_seconds=3.0,
    )
    writer.close()
    return Trace(events=list(writer.events))


def _install_fake_run(monkeypatch, result: RunResult) -> None:
    import tribunal.cli as cli

    class FakeOrchestrator:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, *_args, **_kwargs):
            return result

    monkeypatch.setattr(cli, "Orchestrator", FakeOrchestrator)
    # Routing is checked before the run starts, and there is no credential in a test env.
    monkeypatch.setattr(cli.registry, "build", lambda *_a, **_k: _AlwaysAvailable())


class _AlwaysAvailable:
    def available(self):
        return None


def _result(trace: Trace, state: State = State.DONE, path: Path | None = None) -> RunResult:
    return RunResult(
        report=report_builder.build(trace), trace=trace,
        machine=Machine(state=state), trace_path=path,
    )


def test_run_rejects_a_missing_file():
    result = runner.invoke(app, ["run", "does-not-exist.py"])
    assert result.exit_code == ExitCode.USAGE
    assert "no such file" in result.output


def test_run_refuses_a_test_oracle_without_allow_exec(tmp_path):
    """Same rule as `ground`: naming a test file is asking to execute it."""
    target = tmp_path / "t.py"
    target.write_text("x = 1\n", encoding="utf-8")
    oracle = tmp_path / "test_t.py"
    oracle.write_text("def test_x():\n    assert True\n", encoding="utf-8")

    result = runner.invoke(app, ["run", str(target), "--test", str(oracle)])
    assert result.exit_code == ExitCode.USAGE
    assert "--test requires --allow-exec" in result.output


def test_run_refuses_to_start_when_an_agent_has_no_usable_provider(tmp_path, monkeypatch):
    """A missing credential must fail before the first tool runs, naming the agent. Finding
    out three minutes into a run that the Profiler was never going to work is the failure this
    prevents."""
    import tribunal.cli as cli

    class Unavailable:
        def available(self):
            return "NVIDIA_API_KEY is not set"

    monkeypatch.setattr(cli.registry, "build", lambda *_a, **_k: Unavailable())
    target = tmp_path / "t.py"
    target.write_text("x = 1\n", encoding="utf-8")

    result = runner.invoke(app, ["run", str(target)])
    assert result.exit_code == ExitCode.USAGE
    assert "cannot run" in result.output
    assert "NVIDIA_API_KEY is not set" in result.output
    assert "profiler" in result.output


def test_run_prints_the_debate_and_exits_zero_on_accept(tmp_path, monkeypatch):
    trace = _trace((events.policy_decision, {"verdict": _verdict(), "duplicates": []}))
    trace_path = tmp_path / f"{RUN_ID}.jsonl"
    _install_fake_run(monkeypatch, _result(trace, path=trace_path))

    target = tmp_path / "t.py"
    target.write_text("x = 1\n", encoding="utf-8")
    result = runner.invoke(app, ["run", str(target)])

    assert result.exit_code == ExitCode.ACCEPT
    assert "ACCEPT" in result.output
    assert "accept" in result.output  # the rule, which is what makes it explainable
    assert "+new" in result.output  # the diff that shipped
    assert str(RUN_ID) in result.output  # the trace to go and read


def test_run_exits_one_on_a_tradeoff_and_prints_the_standing_objection(tmp_path, monkeypatch):
    """The trade-off render is the one docs/06 cares most about: ship this, here is the
    objection that stands, here is what the alternative would have cost."""
    conflict = Conflict(
        left_issue="PERF-aaaaaaaaaa", right_issue="SEC-bbbbbbbbbb",
        left_remedy_cost="+18% p50 latency", right_remedy_cost="keeps the injection open",
        axis="security_vs_performance", detector="same_span",
    )
    trace = _trace((events.policy_decision, {
        "verdict": _verdict(decision=Decision.TRADEOFF, rule_fired="irreconcilable",
                            pressure=6.4, pressure_history=[6.4],
                            open_issues=["SEC-bbbbbbbbbb", "PERF-aaaaaaaaaa"],
                            conflict=conflict),
        "duplicates": [],
    }))
    _install_fake_run(monkeypatch, _result(trace))

    target = tmp_path / "t.py"
    target.write_text("x = 1\n", encoding="utf-8")
    result = runner.invoke(app, ["run", str(target)])

    assert result.exit_code == ExitCode.TRADEOFF
    assert "TRADEOFF" in result.output
    assert "irreconcilable" in result.output
    assert "same_span" in result.output
    assert "keeps the injection open" in result.output
    assert "+18% p50 latency" in result.output


def test_run_says_an_unassessed_dimension_is_not_the_same_as_clean(tmp_path, monkeypatch):
    trace = _trace((events.policy_decision, {
        "verdict": _verdict(decision=Decision.ESCALATE, rule_fired="unassessed_terminal",
                            unassessed_dimensions=[Dimension.PERFORMANCE]),
        "duplicates": [],
    }))
    _install_fake_run(monkeypatch, _result(trace))

    target = tmp_path / "t.py"
    target.write_text("x = 1\n", encoding="utf-8")
    result = runner.invoke(app, ["run", str(target)])

    assert result.exit_code == ExitCode.ESCALATE
    assert "unassessed: performance" in result.output
    # Rich wraps this sentence according to the runner's terminal width.
    assert "not the same as clean" in " ".join(result.output.split())
    assert "no patch accepted" in result.output


def test_a_final_reject_exits_as_an_escalation(tmp_path, monkeypatch):
    """A run whose last word is REJECT has run out of rounds, which is an escalation to a
    human, not a clean failure. The shell contract is what CI depends on, so the mapping is
    asserted rather than assumed."""
    trace = _trace((events.policy_decision, {
        "verdict": _verdict(decision=Decision.REJECT, rule_fired="hard_block_security",
                            pressure=21.6, pressure_history=[21.6],
                            open_issues=["SEC-cccccccccc"]),
        "duplicates": [],
    }))
    _install_fake_run(monkeypatch, _result(trace, state=State.FAILED))

    target = tmp_path / "t.py"
    target.write_text("x = 1\n", encoding="utf-8")
    result = runner.invoke(app, ["run", str(target)])

    assert result.exit_code == ExitCode.ESCALATE
    assert "terminal=FAILED" in result.output
    assert "hard_block_security" in result.output


def test_replay_rebuilds_the_report_with_no_api_calls(tmp_path):
    """Criterion S6 at the command level. The trace on disk is the only input."""
    path = tmp_path / f"{RUN_ID}.jsonl"
    _trace(
        (events.policy_decision, {
            "verdict": _verdict(decision=Decision.REJECT, rule_fired="hard_block_security",
                                pressure=21.6, pressure_history=[21.6],
                                open_issues=["SEC-dddddddddd"]),
            "duplicates": [],
        }),
        path=path,
    )
    result = runner.invoke(app, ["replay", str(path)])

    # `replay` reports a run, so it exits with that run's outcome code. The recorded
    # decision here is a REJECT that ran out of rounds, i.e. an escalation.
    assert result.exit_code == ExitCode.ESCALATE
    assert RUN_ID in result.output
    assert "hard_block_security" in result.output
    assert "21.6" in result.output


def test_replay_can_ask_what_a_different_threshold_would_have_decided(tmp_path):
    path = tmp_path / f"{RUN_ID}.jsonl"
    _trace(
        (events.llm_response, {
            "actor": "profiler", "round": 1, "stop_reason": "stop",
            "structure": "native_json", "parse_retries": 0, "local_repairs": [],
            "transient_retries": 0, "request_hash": "h", "replayed": False,
            "usage": Usage(model="m", input_tokens=10, output_tokens=5, cost_usd=0.001,
                           request_id="r"),
            "duration_ms": 1,
            "parsed": {
                "dimension": "performance", "round": 1, "verdict": "concerns",
                "positive_notes": [], "tools_consulted": ["radon"], "summary": "s",
                "issues": [{
                    "id": "PERF-eeeeeeeeee", "dimension": "performance", "severity": "medium",
                    "title": "quadratic append", "explanation": "e", "confidence": 1.0,
                    "introduced_by_patch": False, "suggested_direction": "use a list",
                    "evidence": [{"kind": "code_span", "ref": "t.py:L1-L2", "excerpt": "x"}],
                }],
            },
        }),
        (events.policy_decision, {
            "verdict": _verdict(decision=Decision.REJECT, rule_fired="pressure_over_threshold",
                                pressure=4.0, pressure_history=[4.0],
                                open_issues=["PERF-eeeeeeeeee"]),
            "duplicates": [],
        }),
        path=path,
    )
    result = runner.invoke(app, ["replay", str(path), "--accept-threshold", "100"])

    # `replay` reports a run, so it exits with that run's outcome code. The recorded
    # decision here is a REJECT that ran out of rounds, i.e. an escalation.
    assert result.exit_code == ExitCode.ESCALATE
    assert "re-decided at accept_threshold=100" in result.output
    assert "yes" in result.output  # the decision changed


def test_replay_can_print_the_whole_report_as_json(tmp_path):
    path = tmp_path / f"{RUN_ID}.jsonl"
    _trace((events.policy_decision, {"verdict": _verdict(), "duplicates": []}), path=path)
    result = runner.invoke(app, ["replay", str(path), "--report"])

    # This trace records an ACCEPT, so replaying it exits 0 — the same contract as the
    # tests above, reaching the other answer.
    assert result.exit_code == ExitCode.ACCEPT
    body = result.output[result.output.index("{"):]
    assert json.loads(body)["rule_fired"] == "accept"


def test_replay_warns_about_a_truncated_trace_rather_than_refusing_it(tmp_path):
    """A run killed mid-flight is exactly when you want to read its trace."""
    path = tmp_path / "partial.jsonl"
    writer = TraceWriter(RUN_ID, path).open()
    writer.emit(
        events.run_start, input_file="t.py", input_sha256="abc", config_snapshot={},
        prompt_versions={}, argv=[], trace_level="default",
    )
    writer.close()

    result = runner.invoke(app, ["replay", str(path)])
    # `replay` reports a run, so it exits with that run's outcome code. The recorded
    # decision here is a REJECT that ran out of rounds, i.e. an escalation.
    assert result.exit_code == ExitCode.ESCALATE
    assert "warning:" in result.output
    assert "killed or is still in flight" in result.output


def test_replay_rejects_a_missing_file():
    result = runner.invoke(app, ["replay", "nope.jsonl"])
    assert result.exit_code == ExitCode.USAGE
    assert "no such trace" in result.output


def test_replay_refuses_a_file_that_is_not_a_trace(tmp_path):
    path = tmp_path / "junk.jsonl"
    path.write_text("not json at all\n", encoding="utf-8")
    result = runner.invoke(app, ["replay", str(path)])
    assert result.exit_code == ExitCode.FAILED
    assert "not a valid trace event" in result.output


def test_replay_refuses_a_trace_from_a_future_schema(tmp_path):
    path = tmp_path / "future.jsonl"
    header = {
        "seq": 0, "ts": "2026-01-01T00:00:00Z", "run_id": RUN_ID, "round": None,
        "kind": "run_start", "actor": "orchestrator", "usage": None, "duration_ms": None,
        "payload": {"schema_version": "9.0", "input_file": "t.py", "input_sha256": "a",
                    "config": {}, "prompt_versions": {}, "argv": [], "trace_level": "default"},
    }
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")

    result = runner.invoke(app, ["replay", str(path)])
    assert result.exit_code == ExitCode.FAILED
    assert "schema 9.0" in result.output
    assert "rather than showing you garbage" in result.output


# -- the exit-code contract ------------------------------------------------------------------
#
# `scripts/check-exit-codes.sh` is the shell-level proof docs/09 Phase 4 asks for. These run
# the same checks in-process so a regression fails the ordinary suite rather than waiting for
# someone to remember the script.

TRACES = Path(__file__).parent / "fixtures" / "traces"


def test_every_documented_exit_code_is_reachable():
    """Mapping `Decision` alone could only ever produce 0-2: policy returns ESCALATE for an
    unusable input *and* for a budget breach, so `FAILED` and `BUDGET_EXHAUSTED` were
    documented and unreachable, and a pipeline keyed on either would never have fired."""
    expected = {
        "accept": ExitCode.ACCEPT,
        "tradeoff": ExitCode.TRADEOFF,
        "escalate": ExitCode.ESCALATE,
        "failed": ExitCode.FAILED,
        "budget": ExitCode.BUDGET_EXHAUSTED,
    }
    for name, code in expected.items():
        result = runner.invoke(app, ["replay", str(TRACES / f"{name}.jsonl")])
        assert result.exit_code == code, f"{name}: {result.output}"
    assert set(expected.values()) == {0, 1, 2, 3, 4}


def test_a_failed_run_outranks_its_decision():
    """A run that produced nothing reviewable is FAILED whatever its decision says. The
    `failed` fixture's decision is `escalate`, which is why order matters in
    `exit_code_for`."""
    from tribunal.trace import reader as trace_reader

    report = report_builder.build(trace_reader.read(TRACES / "failed.jsonl"))
    assert report.outcome is Decision.ESCALATE
    assert report.terminal_state == "FAILED"


def test_a_budget_breach_is_distinguishable_from_an_ordinary_escalation():
    """"Ran out of money" is a different thing for a pipeline to react to than "the critics
    could not agree", and both are `Decision.ESCALATE`."""
    from tribunal.trace import reader as trace_reader

    budget = report_builder.build(trace_reader.read(TRACES / "budget.jsonl"))
    plain = report_builder.build(trace_reader.read(TRACES / "escalate.jsonl"))
    assert budget.outcome is plain.outcome is Decision.ESCALATE
    assert budget.rule_fired == "budget_exhausted"
    assert plain.rule_fired != "budget_exhausted"


def test_the_shell_script_covers_every_code_the_enum_declares():
    """The script is the artifact docs/09 asks for; this keeps it from drifting behind a
    new code."""
    script = (Path(__file__).parent.parent / "scripts" / "check-exit-codes.sh").read_text()
    for code in ExitCode:
        assert f"check {int(code)} " in script, f"the script never checks exit {int(code)}"


# -- `--error`, the traceback that seeds the Coder -------------------------------------------


def test_run_rejects_a_missing_traceback_file(tmp_path):
    target = tmp_path / "t.py"
    target.write_text("x = 1\n", encoding="utf-8")
    result = runner.invoke(app, ["run", str(target), "--error", str(tmp_path / "nope.txt")])
    assert result.exit_code == ExitCode.USAGE
    assert "no such traceback file" in result.output
    assert "`-` for stdin" in result.output


def test_run_rejects_an_empty_traceback(tmp_path):
    """`--error` given and empty is a mistake worth naming: it silently produces the same
    run as not passing it."""
    target = tmp_path / "t.py"
    target.write_text("x = 1\n", encoding="utf-8")
    blank = tmp_path / "blank.txt"
    blank.write_text("   \n", encoding="utf-8")
    result = runner.invoke(app, ["run", str(target), "--error", str(blank)])
    assert result.exit_code == ExitCode.USAGE
    assert "traceback is empty" in result.output


def test_run_reads_a_traceback_from_stdin(tmp_path, monkeypatch):
    """docs/08 specifies `--error PATH|-`, so a failing run pipes straight in:
    `pytest 2>&1 | tribunal run thing.py --error -`."""
    target = tmp_path / "t.py"
    target.write_text("x = 1\n", encoding="utf-8")

    seen = {}

    class FakeOrchestrator:
        def __init__(self, *_a, **_k):
            pass

        async def run(self, *_a, **kwargs):
            seen["traceback"] = kwargs.get("traceback")
            return _result(_trace())

    import tribunal.cli as cli

    monkeypatch.setattr(cli, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(cli.registry, "build", lambda *_a, **_k: _AlwaysAvailable())
    runner.invoke(app, ["run", str(target), "--error", "-"], input="ValueError: boom\n")
    assert seen["traceback"] == "ValueError: boom\n"
