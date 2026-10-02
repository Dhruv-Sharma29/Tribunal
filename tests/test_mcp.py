"""The MCP server, exercised through the SDK rather than around it.

The thing an MCP server is for is being called by a host, and the two ways that goes wrong
are invisible to a test that only imports the module: the tools are never registered, or
their schemas do not describe the arguments a host would send. So these tests go through
`server.list_tools()` and `server.call_tool(...)` — the same two entry points a host uses.

`review_code` runs the **real** orchestrator over a scripted provider, so the path under test
is the production one end to end: grounding, both critics, the policy layer, the trace. No
API call is made and none of these are marked live.

docs/08's sketch is written against `mcp` 1.x and does not import on 2.x. That is recorded in
docs/13 § 63; the version floor is asserted below so the next person to `pip install -U` gets
a test failure rather than a host that silently stops listing tools.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_llm_client import FakeProvider
from tests.test_orchestrator import (
    ARGV_LINE,
    RETURN_LINE,
    SHELL_LINE,
    SOURCE,
    clean,
    critique,
    issue,
    note,
    proposal,
    settings_for,
    span_issue_id,
    write_up,
)
from tribunal.config import ProviderName
from tribunal.contracts import Decision
from tribunal.llm.client import LLMClient
from tribunal.mcp_server import TOOL_NAMES, _reject, _render_report, _settings_for
from tribunal.trace import reader as trace_reader
from tribunal.trace import report as report_builder
from tribunal.trace.writer import traces_in

mcp = pytest.importorskip("mcp", reason="the MCP server is an optional extra")
pytestmark = pytest.mark.asyncio


# -- harness --------------------------------------------------------------------------------


def server_for(tmp_path: Path, script: list, **overrides):
    """A server whose every agent is one scripted provider, and whose traces land in tmp."""
    from tribunal.mcp_server import build_server

    settings = settings_for(tmp_path, trace_dir=tmp_path / "traces", **overrides)
    fake = FakeProvider()
    fake.script = list(script)
    client = LLMClient(settings)
    client.providers[ProviderName.NIM] = fake
    return build_server(settings, client=client)


#: A two-round run that ends in ACCEPT. The Arbiter and the Postmortem are both seated on
#: the MCP path -- an editor user gets the full tribunal, not a cheaper one -- so the script has
#: to feed them too: a note for round 1's reject, and a write-up at the end. Script
#: exhaustion here means a seat this module fills is one the script forgot, which is the
#: harness bug this project has hit more often than any product bug.
ACCEPTS = [
    proposal(1, SHELL_LINE, ARGV_LINE),
    critique("security", 1, "block", [issue("security", "high", "L5-L5")], "bandit"),
    clean("performance", 1),
    note(1, "reject", critique_text="fix the shell call"),
    proposal(2, RETURN_LINE, "    return str(name)"),
    clean("security", 2),
    clean("performance", 2),
    write_up([(1, "replaced the shell call", "reject"), (2, "coerced the return", "accept")]),
]

#: The same run stopped after one round. It terminates REJECT, not ESCALATE -- running out
#: of *rounds* is a rejected final patch, while ESCALATE is reserved for the cost and time
#: budget -- and the security issue is still open. The write-up has to *name* that issue:
#: `validate_postmortem` rejects a narrative that omits a still-open finding, which is the
#: check that stops a bounded run from reading like a clean one.
OPEN_ISSUE = span_issue_id("security", "L5-L5")
ONE_ROUND = [
    proposal(1, SHELL_LINE, ARGV_LINE),
    critique("security", 1, "block", [issue("security", "high", "L5-L5")], "bandit"),
    clean("performance", 1),
    note(1, "reject", critique_text="fix the shell call"),
    write_up(
        [(1, "replaced the shell call", "reject")],
        outcome="reject",
        caveats=[f"{OPEN_ISSUE} is still open: the round budget ran out before a fix landed"],
    ),
]


def target(tmp_path: Path) -> Path:
    path = tmp_path / "t.py"
    path.write_text(SOURCE, encoding="utf-8")
    return path


def text_of(result) -> str:
    """A `CallToolResult`'s text, joined. Tools here return markdown, so this is all of it."""
    return "\n".join(
        block.text for block in result.content if getattr(block, "text", None)
    )


async def call(server, name: str, **arguments):
    return await server.call_tool(name, arguments)


def only_trace(tmp_path: Path) -> Path:
    """`traces_in`, not a bare glob -- `summary.jsonl` shares the directory. Using the
    product's own helper here is deliberate: a test that re-derived the rule would keep
    passing after the rule changed."""
    traces = traces_in(tmp_path / "traces")
    assert len(traces) == 1, [p.name for p in traces]
    return traces[0]


# -- registration ---------------------------------------------------------------------------


async def test_the_host_sees_both_tools(tmp_path):
    """The failure this catches: a decorator that silently registers nothing. A host shows
    an empty tool list and the user concludes the server is broken, with no error anywhere."""
    tools = await server_for(tmp_path, []).list_tools()
    assert {tool.name for tool in tools} == set(TOOL_NAMES)


async def test_every_tool_describes_itself(tmp_path):
    """The description is the entire basis on which a model decides to call a tool. An
    undocumented one is registered and unreachable."""
    for tool in await server_for(tmp_path, []).list_tools():
        assert tool.description and len(tool.description) > 40, tool.name


async def test_review_codes_schema_matches_its_signature(tmp_path):
    """Asserted against `input_schema` — 2.x's spelling. On 1.x this attribute does not
    exist, which is the drift docs/08 marked `[verify]` and this pins."""
    tool = next(t for t in await server_for(tmp_path, []).list_tools() if t.name == "review_code")
    schema = tool.input_schema
    assert set(schema["required"]) == {"file_path"}
    assert set(schema["properties"]) == {
        "file_path", "test_path", "max_rounds", "allow_exec",
    }


async def test_allow_exec_defaults_to_false_in_the_advertised_schema(tmp_path):
    """docs/08: an editor-triggered tool that executes code is a worse default than the same
    thing typed into a terminal. The default a host sees is the one that matters — a model
    filling in arguments reads the schema, not this module's docstring."""
    tool = next(t for t in await server_for(tmp_path, []).list_tools() if t.name == "review_code")
    assert tool.input_schema["properties"]["allow_exec"]["default"] is False


async def test_max_rounds_defaults_to_two(tmp_path):
    """docs/08's patience budget, not a quality claim. Pinned because it is the kind of
    default that gets "harmonised" with the CLI's 3 by someone tidying up."""
    tool = next(t for t in await server_for(tmp_path, []).list_tools() if t.name == "review_code")
    assert tool.input_schema["properties"]["max_rounds"]["default"] == 2


# -- review_code, through the whole tribunal ------------------------------------------------------


async def test_review_code_runs_the_real_loop_and_reports_what_happened(tmp_path):
    server = server_for(tmp_path, ACCEPTS)
    body = text_of(await call(server, "review_code", file_path=str(target(tmp_path))))

    assert body.startswith("## ACCEPT")
    assert "rule fired: `" in body
    assert "rounds: 2" in body
    # The security issue round 1 raised, and the fact that it was fixed.
    assert "security" in body
    assert "```diff" in body


async def test_review_code_writes_a_trace_and_says_where(tmp_path):
    """The trace is the deliverable the CLI and the viewer both read. A tool result that
    does not name it strands the run somewhere the user cannot get back to."""
    server = server_for(tmp_path, ACCEPTS)
    body = text_of(await call(server, "review_code", file_path=str(target(tmp_path))))

    assert only_trace(tmp_path).name in body
    assert "get_trace" in body


async def test_the_trace_records_the_mcp_entry_point(tmp_path):
    """`argv` is how a trace says where a run came from. Without it a sweep over a traces
    directory cannot tell an editor-triggered run from a benchmark arm — which is precisely
    the confusion that made `--replay` skip every B3 run (docs/13 § 52)."""
    server = server_for(tmp_path, ACCEPTS)
    await call(server, "review_code", file_path=str(target(tmp_path)))

    trace = trace_reader.read(only_trace(tmp_path))
    start = next(e for e in trace.events if e.kind == "run_start")
    assert start.payload["argv"][0] == "mcp:review_code"


async def test_max_rounds_is_actually_applied(tmp_path):
    """A parameter a host can set and the run ignores is worse than no parameter: the user
    thinks they bounded the cost."""
    settings = _settings_for(settings_for(tmp_path), max_rounds=1, allow_exec=False)
    assert settings.policy.max_rounds == 1

    server = server_for(tmp_path, ONE_ROUND)
    body = text_of(
        await call(server, "review_code", file_path=str(target(tmp_path)), max_rounds=1)
    )
    assert "rounds: 1" in body


async def test_allow_exec_reaches_the_sandbox(tmp_path):
    assert _settings_for(settings_for(tmp_path), 2, True).sandbox.allow_exec is True
    assert _settings_for(settings_for(tmp_path), 2, False).sandbox.allow_exec is False


async def test_an_unusable_provider_is_one_sentence_not_a_traceback(tmp_path, monkeypatch):
    """Over stdio a raised exception is a tool error whose text the host may not show. The
    most likely single failure — the key is set in the user's shell but not in the
    environment the host launched the server from — must say exactly that."""
    from tribunal.mcp_server import build_server

    settings = settings_for(tmp_path, trace_dir=tmp_path / "traces")
    # A developer's real credential must not turn this offline failure test into an API call.
    for provider in settings.providers.model_dump().values():
        monkeypatch.delenv(provider["key_env"], raising=False)
    client = LLMClient(settings)
    body = text_of(
        await call(
            build_server(settings, client=client),
            "review_code",
            file_path=str(target(tmp_path)),
        )
    )
    assert "no usable model provider" in body.lower()
    assert "environment the MCP server starts in" in body


# -- refusing badly-formed calls --------------------------------------------------------------


async def test_a_missing_file_is_a_message_not_a_crash(tmp_path):
    body = text_of(
        await call(server_for(tmp_path, []), "review_code", file_path=str(tmp_path / "nope.py"))
    )
    assert "No such file" in body


async def test_a_non_python_file_is_refused_with_the_reason(tmp_path):
    other = tmp_path / "notes.md"
    other.write_text("hello", encoding="utf-8")
    body = text_of(await call(server_for(tmp_path, []), "review_code", file_path=str(other)))
    assert "not a Python file" in body


async def test_a_test_path_without_allow_exec_is_refused(tmp_path):
    """Naming a test file is asking for it to be executed. Accepting it silently — and
    running the suite with `allow_exec` off, so pytest never runs — would report a clean
    performance dimension nobody measured."""
    tests = tmp_path / "test_t.py"
    tests.write_text("def test_x():\n    assert True\n", encoding="utf-8")
    body = text_of(
        await call(
            server_for(tmp_path, []),
            "review_code",
            file_path=str(target(tmp_path)),
            test_path=str(tests),
        )
    )
    assert "allow_exec" in body
    assert "executes it" in body


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"test_path": "/nope/test_x.py", "allow_exec": True}, "No such test file"),
        ({"max_rounds": 0}, "at least 1"),
    ],
)
async def test_reject_covers_the_remaining_argument_errors(tmp_path, kwargs, expected):
    path = target(tmp_path)
    assert expected in _reject(
        path,
        kwargs.get("test_path"),
        kwargs.get("allow_exec", False),
        kwargs.get("max_rounds", 2),
    )


async def test_a_well_formed_call_is_not_rejected(tmp_path):
    assert _reject(target(tmp_path), None, False, 2) is None


# -- get_trace ---------------------------------------------------------------------------------


async def test_get_trace_reads_back_a_run_by_id(tmp_path):
    server = server_for(tmp_path, ACCEPTS)
    first = text_of(await call(server, "review_code", file_path=str(target(tmp_path))))
    run_id = only_trace(tmp_path).stem

    second = text_of(await call(server, "get_trace", run_id=run_id))
    # Same function renders both, from the same trace, so they agree by construction --
    # criterion S6 applied to the MCP path rather than re-implemented for it.
    assert second == first


async def test_get_trace_makes_no_model_call(tmp_path):
    """An empty script: if `get_trace` touched a provider, `FakeProvider` would raise on
    exhaustion rather than return."""
    server = server_for(tmp_path, ACCEPTS)
    await call(server, "review_code", file_path=str(target(tmp_path)))
    run_id = only_trace(tmp_path).stem
    assert "ACCEPT" in text_of(await call(server, "get_trace", run_id=run_id))


async def test_an_unknown_run_id_lists_what_is_there(tmp_path):
    """The likely cause is a typo or a truncated ULID, and the fix is a nearby id."""
    server = server_for(tmp_path, ACCEPTS)
    await call(server, "review_code", file_path=str(target(tmp_path)))
    real = only_trace(tmp_path).stem

    body = text_of(await call(server, "get_trace", run_id="01NOPE"))
    assert "No trace for run id" in body
    assert real in body


async def test_a_corrupt_trace_is_reported_as_such(tmp_path):
    (tmp_path / "traces").mkdir(parents=True)
    (tmp_path / "traces" / "broken.jsonl").write_text("{not json\n", encoding="utf-8")
    body = text_of(await call(server_for(tmp_path, []), "get_trace", run_id="broken"))
    assert "not a readable trace" in body


# -- the rendering contract ---------------------------------------------------------------------


def report_from(name: str):
    path = Path(__file__).parent / "fixtures" / "traces" / f"{name}.jsonl"
    return report_builder.build(trace_reader.read(path))


@pytest.mark.parametrize("name", ["accept", "tradeoff", "escalate", "failed", "budget"])
async def test_every_outcome_renders_without_raising(name):
    """The fixture traces cover all five terminal shapes. A renderer that crashes on
    TRADEOFF would be a tool that works until the one outcome worth reading."""
    body = _render_report(report_from(name), Path("traces/x.jsonl"))
    assert body.startswith("## ")
    assert "tribunal" in body


async def test_a_tradeoff_renders_both_costs_and_the_recommendation():
    """docs/04: TRADEOFF exists so the two costs reach a human. A summary that says
    "tradeoff" and stops has thrown away the only part that was hard to produce."""
    body = _render_report(report_from("tradeoff"), None)
    report = report_from("tradeoff")
    assert report.outcome is Decision.TRADEOFF
    assert report.conflict is not None
    assert report.conflict.left_remedy_cost in body
    assert report.conflict.right_remedy_cost in body
    assert "incompatible" in body


async def test_a_run_with_no_patch_says_why():
    body = _render_report(report_from("escalate"), None)
    assert "No patch was accepted" in body
    assert report_from("escalate").no_patch_reason in body


@pytest.mark.parametrize("name, expected", [("escalate", ["performance"]),
                                            ("failed", ["security", "performance"])])
async def test_unassessed_dimensions_are_called_out_not_omitted(name, expected):
    """The project's central claim about honesty: unassessed is not clean. A summary that
    drops the field reports silence as safety.

    Named fixtures rather than a skip-if-empty guard: a conditional skip here would go green
    the day the fixtures stopped having an unassessed dimension, which is the one change
    that would make this test worth running.
    """
    report = report_from(name)
    assert [d.value for d in report.unassessed_dimensions] == expected
    body = _render_report(report, None)
    for dimension in expected:
        assert dimension in body
    assert "unassessed" in body
    assert "Not the same as clean" in body


async def test_the_summary_is_markdown_not_json():
    """docs/08: a wall of JSON is unreadable in most host panels."""
    body = _render_report(report_from("accept"), Path("traces/x.jsonl"))
    with pytest.raises(json.JSONDecodeError):
        json.loads(body)
    assert body.lstrip().startswith("## ")


async def test_trace_warnings_reach_the_reader():
    """A trace with no `run_end` still builds a report. Rendering it as though the run
    finished is the silent-degradation failure this project keeps finding in itself."""
    body = _render_report(report_from("accept"), None, warnings=["no run_end: killed"])
    assert "no run_end: killed" in body
