"""`tribunal code`: the schema, the workspace boundary, approval, and the loop.

The properties pinned here are the ones whose breakage is silent. A path escaping the
workspace, an approval that is remembered too widely, an `edit` that picks one of two
identical anchors, a transcript that drops the original request when it elides -- none of
these fail loudly. They produce a session that looks like it worked.

Every test runs against `FakeProvider`, so the whole file makes zero API calls.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.test_llm_client import FakeProvider
from tribunal.cli import ExitCode, app, command_names
from tribunal.code.actions import MUTATING, REQUIRED_FIELDS, Step, ToolName
from tribunal.code.approval import ApprovalMode, Approver
from tribunal.code.session import CodeSession
from tribunal.code.tools import ToolContext, Workspace, WorkspaceError, execute
from tribunal.config import AgentConfig, AgentsConfig, CodeConfig, LLMConfig, Settings
from tribunal.llm.base import ProviderName
from tribunal.llm.client import LLMClient

runner = CliRunner()


# ------------------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------------------


def settings_for(tmp_path: Path, **code: object) -> Settings:
    return Settings(
        llm=LLMConfig(cassette_dir=tmp_path / "cassettes"),
        agents=AgentsConfig(
            code=AgentConfig(model="fake-1", provider=ProviderName.NIM, max_tokens=4_000)
        ),
        code=CodeConfig(**code),
    )


def context_for(tmp_path: Path, **code: object) -> ToolContext:
    return ToolContext(Workspace(tmp_path), settings_for(tmp_path, **code))


def run(step: Step, context: ToolContext):
    return asyncio.run(execute(step, context))


def session_for(root: Path, script: list, mode: ApprovalMode = ApprovalMode.AUTO, **code):
    settings = settings_for(root, **code)
    client = LLMClient(settings)
    client.providers[ProviderName.NIM] = FakeProvider(script=list(script))
    return CodeSession(
        settings=settings,
        workspace=Workspace(root),
        client=client,
        approver=Approver(mode=mode),
    )


def done(message: str = "finished") -> dict:
    return {"tool": "done", "message": message, "thought": "that is the lot"}


# ------------------------------------------------------------------------------------------
# The step schema
# ------------------------------------------------------------------------------------------


def test_every_tool_declares_what_it_needs():
    """A tool added to the enum without a row here would accept any step at all."""
    assert set(REQUIRED_FIELDS) == set(ToolName)


@pytest.mark.parametrize(
    ("payload", "missing"),
    [
        ({"tool": "read"}, "path"),
        ({"tool": "grep"}, "pattern"),
        ({"tool": "edit", "path": "a.py"}, "search"),
        ({"tool": "bash"}, "command"),
        ({"tool": "done"}, "message"),
    ],
)
def test_a_step_missing_its_required_field_is_a_repairable_error(payload, missing):
    """It has to raise `ValueError` inside validation, which is what `LLMClient` turns into
    one repair retry naming the field -- not a crash, and not a silently empty call."""
    with pytest.raises(ValueError, match=missing):
        Step.model_validate(payload)


def test_writing_an_empty_file_is_a_legal_step():
    """`content` is the one required field whose empty value means something."""
    assert Step.model_validate({"tool": "write", "path": "empty.txt", "content": ""})


def test_the_mutating_set_is_exactly_what_can_change_or_cost():
    assert {ToolName.WRITE, ToolName.EDIT, ToolName.BASH, ToolName.REVIEW} == MUTATING


def test_the_prompt_documents_every_tool():
    """The tool reference lives in the prompt and the tool list lives in the enum. This is
    what keeps them from drifting apart, which would be invisible until a model used a tool
    it had never been told about."""
    from tribunal.agents.prompts import load

    text = load("code").text
    for tool in ToolName:
        assert f"`{tool.value}`" in text, f"{tool.value} is not documented in code.md"


def test_the_code_agent_keeps_a_cacheable_prefix(tmp_path):
    """Construction runs `_check_prefix_is_stable`. The working directory and the file tree
    belong in the user block; this is the guard that says so."""
    from tribunal.code.session import CodeAgent

    settings = settings_for(tmp_path)
    assert CodeAgent(settings, LLMClient(settings)).prompt_version == "code/v1"


# ------------------------------------------------------------------------------------------
# The workspace boundary
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("escape", ["../outside.txt", "/etc/passwd", "a/../../outside.txt"])
def test_a_path_outside_the_workspace_is_refused(tmp_path, escape):
    (tmp_path.parent / "outside.txt").write_text("secret", encoding="utf-8")
    workspace = Workspace(tmp_path)
    with pytest.raises(WorkspaceError):
        workspace.resolve(escape)


def test_a_symlink_out_of_the_tree_is_refused_too(tmp_path):
    """A string-prefix check on the unresolved path would let this through."""
    (tmp_path.parent / "target.txt").write_text("secret", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(tmp_path.parent / "target.txt")
    with pytest.raises(WorkspaceError):
        Workspace(tmp_path).resolve("link.txt")


def test_an_escaping_path_comes_back_as_an_observation_not_an_exception(tmp_path):
    """The model has to be able to recover from its own bad path."""
    observation = run(Step(tool=ToolName.READ, path="../../etc/passwd"), context_for(tmp_path))
    assert not observation.ok
    assert "outside the session workspace" in observation.text


# ------------------------------------------------------------------------------------------
# Reading tools
# ------------------------------------------------------------------------------------------


def test_read_numbers_lines_and_says_where_to_continue(tmp_path):
    (tmp_path / "a.py").write_text("\n".join(f"line {i}" for i in range(1, 11)), "utf-8")
    observation = run(
        Step(tool=ToolName.READ, path="a.py", max_lines=4), context_for(tmp_path)
    )
    assert "     1\tline 1" in observation.text
    assert "6 more line(s)" in observation.text
    assert "start_line=5" in observation.text


def test_read_of_a_missing_file_suggests_the_way_out(tmp_path):
    observation = run(Step(tool=ToolName.READ, path="nope.py"), context_for(tmp_path))
    assert not observation.ok
    assert "glob" in observation.text


def test_grep_reports_file_and_line(tmp_path):
    (tmp_path / "a.py").write_text("import os\ndef f():\n    return os.getcwd()\n", "utf-8")
    observation = run(Step(tool=ToolName.GREP, pattern=r"def\s+\w+"), context_for(tmp_path))
    assert "a.py:2: def f():" in observation.text


def test_grep_with_a_broken_pattern_says_so(tmp_path):
    observation = run(Step(tool=ToolName.GREP, pattern="(unclosed"), context_for(tmp_path))
    assert not observation.ok
    assert "not a valid regular expression" in observation.text


def test_the_ignore_list_keeps_a_virtualenv_out_of_the_context(tmp_path):
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "big.py").write_text("needle", encoding="utf-8")
    (tmp_path / "mine.py").write_text("needle", encoding="utf-8")
    observation = run(Step(tool=ToolName.GREP, pattern="needle"), context_for(tmp_path))
    assert "mine.py" in observation.text
    assert ".venv" not in observation.text


def test_glob_matches_on_the_relative_path_and_the_bare_name(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "thing_test.py").write_text("", encoding="utf-8")
    observation = run(Step(tool=ToolName.GLOB, pattern="*_test.py"), context_for(tmp_path))
    assert "src/thing_test.py" in observation.text


# ------------------------------------------------------------------------------------------
# Writing tools
# ------------------------------------------------------------------------------------------


def test_write_creates_the_file_and_records_an_undo(tmp_path):
    context = context_for(tmp_path)
    run(Step(tool=ToolName.WRITE, path="new/thing.py", content="x = 1\n"), context)
    assert (tmp_path / "new" / "thing.py").read_text(encoding="utf-8") == "x = 1\n"
    assert context.workspace.undo_last() == "removed new/thing.py (it did not exist before)"
    assert not (tmp_path / "new" / "thing.py").exists()


def test_undo_restores_the_previous_contents(tmp_path):
    (tmp_path / "a.py").write_text("before\n", encoding="utf-8")
    context = context_for(tmp_path)
    run(Step(tool=ToolName.WRITE, path="a.py", content="after\n"), context)
    context.workspace.undo_last()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "before\n"


def test_edit_replaces_a_unique_anchor(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    observation = run(
        Step(tool=ToolName.EDIT, path="a.py", search="return 1", replace="return 2"),
        context_for(tmp_path),
    )
    assert observation.ok
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "def f():\n    return 2\n"


def test_edit_refuses_an_ambiguous_anchor_rather_than_guessing(tmp_path):
    """Two identical regions and a silent choice between them is the edit least likely to be
    noticed in review, so it is a refusal with the count in it."""
    (tmp_path / "a.py").write_text("x = 1\ny = 1\n", encoding="utf-8")
    observation = run(
        Step(tool=ToolName.EDIT, path="a.py", search="= 1", replace="= 2"),
        context_for(tmp_path),
    )
    assert not observation.ok
    assert "occurs 2 times" in observation.text
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\ny = 1\n"


def test_edit_names_whitespace_when_that_is_the_real_problem(tmp_path):
    """A model that retyped the anchor instead of copying it gets told which mistake it
    made, because 'not found' sends it looking for the wrong thing."""
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    observation = run(
        Step(tool=ToolName.EDIT, path="a.py", search="def f():\n  return 1", replace="x"),
        context_for(tmp_path),
    )
    assert not observation.ok
    assert "whitespace differs" in observation.text


# ------------------------------------------------------------------------------------------
# bash
# ------------------------------------------------------------------------------------------


def test_bash_runs_in_the_workspace_and_reports_the_exit_code(tmp_path):
    (tmp_path / "marker.txt").write_text("", encoding="utf-8")
    observation = run(Step(tool=ToolName.BASH, command="ls"), context_for(tmp_path))
    assert observation.ok
    assert "marker.txt" in observation.text


def test_a_failing_command_is_not_ok_but_is_still_an_observation(tmp_path):
    observation = run(Step(tool=ToolName.BASH, command="exit 3"), context_for(tmp_path))
    assert not observation.ok
    assert "exit 3" in observation.summary


def test_a_hanging_command_is_killed_at_the_timeout(tmp_path):
    observation = run(
        Step(tool=ToolName.BASH, command="sleep 30"),
        context_for(tmp_path, command_timeout_seconds=1),
    )
    assert not observation.ok
    assert "killed after 1s" in observation.text


def test_long_output_keeps_both_ends(tmp_path):
    """The head and the tail of a log are the informative parts; the middle is not."""
    command = "python3 -c 'print(\"HEAD\"); print(\"x\" * 20000); print(\"TAIL\")'"
    observation = run(
        Step(tool=ToolName.BASH, command=command),
        context_for(tmp_path, max_output_chars=2_000),
    )
    assert "HEAD" in observation.text
    assert "TAIL" in observation.text
    assert observation.elided_chars > 0


# ------------------------------------------------------------------------------------------
# Approval
# ------------------------------------------------------------------------------------------


def test_read_only_tools_never_ask():
    approver = Approver(mode=ApprovalMode.ASK, prompter=lambda step, key: pytest.fail("asked"))
    assert approver.check(Step(tool=ToolName.READ, path="a.py")).allowed


def test_plan_mode_refuses_and_tells_the_agent_what_to_do_instead():
    approval = Approver(mode=ApprovalMode.PLAN).check(
        Step(tool=ToolName.WRITE, path="a.py", content="x")
    )
    assert not approval.allowed
    assert "plan mode" in approval.reason
    assert "done" in approval.reason


def test_always_is_remembered_against_the_program_not_the_command_line():
    """Approving `pytest` once must not approve `curl` later, and must not require a fresh
    prompt for every `pytest` invocation either."""
    asked = []
    approver = Approver(
        mode=ApprovalMode.ASK, prompter=lambda step, key: asked.append(key) or "always"
    )
    assert approver.check(Step(tool=ToolName.BASH, command="pytest -k one")).allowed
    assert approver.check(Step(tool=ToolName.BASH, command="pytest -k two")).allowed
    assert asked == ["bash:pytest"]
    approver.prompter = lambda step, key: asked.append(key) or "no"
    assert not approver.check(Step(tool=ToolName.BASH, command="curl example.com")).allowed
    assert asked == ["bash:pytest", "bash:curl"]


def test_never_is_remembered_and_stops_the_asking():
    approver = Approver(mode=ApprovalMode.ASK, prompter=lambda step, key: "never")
    step = Step(tool=ToolName.BASH, command="git push")
    assert not approver.check(step).allowed
    approver.prompter = lambda step, key: pytest.fail("asked again after never")
    assert not approver.check(step).allowed


@pytest.mark.parametrize(
    "command", ["rm -rf /", "mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/dev/sda", "reboot"]
)
def test_the_catastrophic_list_holds_even_under_yes(command):
    """`--yes` means "do not ask me", not "do anything". These are typos, not requests."""
    approval = Approver(mode=ApprovalMode.AUTO).check(Step(tool=ToolName.BASH, command=command))
    assert not approval.allowed
    assert "every mode" in approval.reason


def test_a_scoped_rm_is_not_caught_by_the_denylist():
    """A denylist that catches ordinary work gets disabled, and then it catches nothing."""
    approval = Approver(mode=ApprovalMode.AUTO).check(
        Step(tool=ToolName.BASH, command="rm -rf build/")
    )
    assert approval.allowed


# ------------------------------------------------------------------------------------------
# The loop
# ------------------------------------------------------------------------------------------


def test_a_session_runs_steps_until_the_agent_says_done(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    session = session_for(
        tmp_path,
        [
            {"tool": "read", "path": "a.py", "thought": "look first"},
            {"tool": "edit", "path": "a.py", "search": "x = 1", "replace": "x = 2"},
            done("set x to 2 in a.py"),
        ],
    )
    answer = asyncio.run(session.ask("make x two"))
    assert answer.complete
    assert answer.message == "set x to 2 in a.py"
    assert [turn.step.tool for turn in answer.turns] == [ToolName.READ, ToolName.EDIT]
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 2\n"


def test_the_transcript_carries_the_previous_result_into_the_next_request(tmp_path):
    """There is no message history on the wire, so this is the only memory the agent has."""
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    session = session_for(
        tmp_path, [{"tool": "read", "path": "a.py", "thought": "look"}, done()]
    )
    asyncio.run(session.ask("what is in a.py"))
    second = session.client.providers[ProviderName.NIM].seen[1]
    assert "needle" in second.user
    assert "what is in a.py" in second.user


def test_a_refused_step_is_fed_back_rather_than_ending_the_turn(tmp_path):
    session = session_for(
        tmp_path,
        [{"tool": "bash", "command": "make release"}, done("described it instead")],
        mode=ApprovalMode.PLAN,
    )
    answer = asyncio.run(session.ask("ship it"))
    assert answer.complete
    assert not answer.turns[0].approval.allowed
    assert "plan mode" in session.client.providers[ProviderName.NIM].seen[1].user


def test_the_step_budget_stops_a_loop_without_losing_the_work(tmp_path):
    session = session_for(
        tmp_path, [{"tool": "list"}] * 3, mode=ApprovalMode.AUTO, max_steps=3
    )
    answer = asyncio.run(session.ask("go forever"))
    assert answer.stopped_by == "steps"
    assert not answer.complete
    assert "--max-steps" in answer.message
    assert session.steps_taken == 3


def test_the_spend_cap_stops_the_next_step(tmp_path):
    """`FakeProvider` charges a thousandth of a dollar per call, so two calls exceed this."""
    session = session_for(tmp_path, [{"tool": "list"}] * 5, max_usd=0.0015)
    answer = asyncio.run(session.ask("keep going"))
    assert answer.stopped_by == "cost"
    assert "--max-usd" in answer.message


def test_a_model_that_cannot_emit_a_step_ends_the_turn_politely(tmp_path):
    """Two attempts (the call plus one repair retry) both naming a tool that does not
    exist. The session stops with a message; it does not raise into the terminal."""
    session = session_for(tmp_path, [{"tool": "teleport"}, {"tool": "teleport"}])
    answer = asyncio.run(session.ask("do something"))
    assert answer.stopped_by == "schema"
    assert session.workspace.snapshots == []


def test_a_provider_failure_ends_the_turn_rather_than_the_session(tmp_path):
    from tribunal.llm.base import ProviderRefused

    session = session_for(tmp_path, [ProviderRefused("declined on policy grounds")])
    answer = asyncio.run(session.ask("do something"))
    assert answer.stopped_by == "provider"
    assert "declined on policy grounds" in answer.message


def test_the_transcript_elides_from_the_middle_and_keeps_the_request(tmp_path):
    """Dropping the oldest entry first would lose the task itself, which is the one thing a
    recovery cannot re-derive."""
    (tmp_path / "a.py").write_text("x\n" * 400, encoding="utf-8")
    script = [{"tool": "read", "path": "a.py"} for _ in range(6)] + [done()]
    session = session_for(tmp_path, script, max_history_chars=1_200, max_output_chars=600)
    asyncio.run(session.ask("the original request"))
    last = session.client.providers[ProviderName.NIM].seen[-1].user
    assert "the original request" in last
    assert "earlier step(s) elided" in last


def test_the_session_log_records_every_step_as_json(tmp_path):
    log = tmp_path / "logs" / "session.jsonl"
    session = session_for(tmp_path, [{"tool": "list"}, done("ok")])
    session.log_path = log
    asyncio.run(session.ask("look around"))
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [record["step"]["tool"] for record in records] == ["list", "done"]


def test_the_session_log_keeps_the_prompt_health_signal(tmp_path):
    """docs/09 makes the schema-parse retry rate the prompt-health metric, and it cannot be
    recovered after the fact. A log with only the cost in it cannot answer the one
    measurable question about this surface: can this model fill the step schema?"""
    log = tmp_path / "session.jsonl"
    # A first payload the schema rejects, then a good one: exactly one repair retry.
    session = session_for(tmp_path, [{"tool": "read"}, {"tool": "list"}, done("ok")])
    session.log_path = log
    asyncio.run(session.ask("look around"))
    first = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert first["parse_retries"] == 1
    assert first["structure"] == "native_json"
    assert first["duration_ms"] is not None


def test_the_write_note_does_not_warn_about_overwriting_a_new_file(tmp_path):
    """A warning that fires when nothing is at stake teaches people to stop reading
    warnings, which is the opposite of what an approval prompt is for."""
    approver = Approver(mode=ApprovalMode.ASK)
    new_file = Step(tool=ToolName.WRITE, path="brand_new.py", content="x = 1")
    assert approver.notes(new_file, tmp_path) == []

    (tmp_path / "existing.py").write_text("old\n", encoding="utf-8")
    existing = Step(tool=ToolName.WRITE, path="existing.py", content="x = 1")
    assert "current contents are lost" in approver.notes(existing, tmp_path)[0]


# ------------------------------------------------------------------------------------------
# The command line
# ------------------------------------------------------------------------------------------


def argv_after_main(monkeypatch, argv: list[str]) -> list[str]:
    """What `main` hands to Click, given what the user typed."""
    import sys

    from tribunal import cli

    names = command_names()
    seen: list[list[str]] = []
    monkeypatch.setattr(cli, "command_names", lambda: names)
    monkeypatch.setattr(cli, "app", lambda: seen.append(list(sys.argv)))
    monkeypatch.setattr(sys, "argv", ["tribunal", *argv])
    cli.main()
    return seen[0][1:]


def test_a_bare_prompt_is_rewritten_to_the_code_command(monkeypatch):
    """`tribunal "fix the test"` has to mean something, and it must not change what any
    existing invocation means."""
    assert argv_after_main(monkeypatch, ["why does this fail?"]) == [
        "code",
        "why does this fail?",
    ]


def test_a_flag_that_belongs_to_code_is_rewritten_too(monkeypatch):
    assert argv_after_main(monkeypatch, ["-p", "add a test"]) == ["code", "-p", "add a test"]


@pytest.mark.parametrize("argv", [["doctor", "--help"], ["run", "a.py"], ["--help"]])
def test_an_existing_invocation_keeps_its_meaning(monkeypatch, argv):
    assert argv_after_main(monkeypatch, argv) == argv


def test_code_is_a_real_command_so_the_rewrite_cannot_recurse():
    assert "code" in command_names()


def test_print_without_a_prompt_is_a_usage_error():
    result = runner.invoke(app, ["code", "--print"])
    assert result.exit_code == ExitCode.USAGE


def test_yes_and_plan_together_are_a_usage_error():
    result = runner.invoke(app, ["code", "--yes", "--plan", "-p", "hello"])
    assert result.exit_code == ExitCode.USAGE


# ------------------------------------------------------------------------------------------
# The terminal
# ------------------------------------------------------------------------------------------


def repl_for(tmp_path, capture):
    from rich.console import Console

    from tribunal.code.repl import Repl

    return Repl(session_for(tmp_path, [done()]), Console(file=capture, width=100))


def test_model_text_containing_brackets_is_printed_literally(tmp_path):
    """Rich reads `[dim]` in a string as a style tag. A reply mentioning `list[int]` must
    not be re-styled, swallowed, or -- with an unclosed tag -- raise while reporting the
    work that was just done."""
    import io

    from tribunal.code.session import Answer

    capture = io.StringIO()
    repl = repl_for(tmp_path, capture)
    repl._render_answer(Answer(message="changed list[int] to Sequence[int]", turns=[],
                               cost_usd=0.0))
    assert "list[int]" in capture.getvalue()


def test_a_failing_step_shows_its_text_verbatim(tmp_path):
    import io

    from tribunal.code.tools import Observation

    capture = io.StringIO()
    repl = repl_for(tmp_path, capture)
    step = Step(tool=ToolName.BASH, command="pytest -k 'x[1]'", thought="run it")
    repl._render_step(step)
    repl._render_observation(step, Observation(False, "KeyError: 'x[1]'", "exit 1"))
    output = capture.getvalue()
    assert "pytest -k 'x[1]'" in output
    assert "KeyError: 'x[1]'" in output


def test_every_tool_has_a_label_in_the_terminal():
    """A tool with no label would raise a KeyError while rendering the step that used it --
    at the worst moment, after the work was already done."""
    from tribunal.code.ui import TOOL_LABEL

    assert set(TOOL_LABEL) == set(ToolName)


def test_an_edit_is_previewed_as_a_diff_against_the_file_on_disk(tmp_path):
    """What a person approves is the effect, not the search/replace pair the model emitted.
    The two differ: the pair has no context lines and no line numbers."""
    from tribunal.code.ui import edit_diff

    current = "def f():\n    return 1\n"
    step = Step(tool=ToolName.EDIT, path="a.py", search="return 1", replace="return 2")
    rendered = edit_diff(step, current).plain
    assert "@@" in rendered
    assert "-    return 1" in rendered
    assert "+    return 2" in rendered


def test_an_unresolvable_anchor_still_previews_something(tmp_path):
    """It is about to be refused, and seeing why is the point."""
    from tribunal.code.ui import edit_diff

    step = Step(tool=ToolName.EDIT, path="a.py", search="not here", replace="x")
    rendered = edit_diff(step, "def f():\n    pass\n").plain
    assert "- not here" in rendered
    assert "+ x" in rendered


def test_a_verdict_renders_as_a_verdict_not_as_text(tmp_path):
    import io

    from rich.console import Console

    from tribunal.code.ui import THEME, verdict_panel

    capture = io.StringIO()
    Console(file=capture, width=100, theme=THEME).print(
        verdict_panel(
            {
                "target": "cart.py",
                "outcome": "tradeoff",
                "rule_fired": "irreconcilable",
                "rounds": 2,
                "cost_usd": 0.0412,
                "issues": [
                    {"id": "SEC-1", "dimension": "security", "severity": "high",
                     "status": "standing", "title": "shell injection"}
                ],
                "conflict": "latency vs input validation",
                "diff": None,
                "no_patch_reason": "the critics want incompatible things",
            }
        )
    )
    output = capture.getvalue()
    assert "TRADEOFF" in output
    assert "irreconcilable" in output
    assert "shell injection" in output
    assert "latency vs input validation" in output


def test_the_review_tool_hands_the_terminal_a_structured_result():
    """`Observation.data` is what makes the panel above possible. Only `review` fills it."""
    from tribunal.code.tools import Observation

    assert Observation(True, "text", "summary").data is None


def test_a_refusal_can_carry_direction_to_the_model():
    """The dialog offers "no, and tell it what to do instead". If that sentence stopped at
    the terminal, the option would be a lie."""
    approver = Approver(
        mode=ApprovalMode.ASK, prompter=lambda step, key: "no: run the unit tests instead"
    )
    approval = approver.check(Step(tool=ToolName.BASH, command="make release"))
    assert not approval.allowed
    assert "run the unit tests instead" in approval.reason


def test_the_spinner_writes_nothing_to_a_pipe():
    """A status line in a log file is noise, and in a test it is a failing assertion."""
    import io

    from rich.console import Console

    from tribunal.code.ui import THEME, Thinking

    capture = io.StringIO()
    with Thinking(Console(file=capture, width=80, theme=THEME), step=1, cost=0.0):
        pass
    assert capture.getvalue() == ""


def test_the_banner_stays_on_one_line_for_a_deep_path(tmp_path):
    from tribunal.code.ui import _short_path

    deep = Path("/var/lib/one/two/three/four/five/six/seven/eight/nine/ten/project")
    assert len(_short_path(deep)) <= 46
    assert "project" in _short_path(deep)
