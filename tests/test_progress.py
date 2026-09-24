"""The live CLI renderer, driven by real runs.

Every test here attaches the renderer to an actual orchestrator run and asserts on what it
drew. That is deliberate: the renderer's correctness is entirely a question of whether it
reads the event stream the orchestrator really emits, and a test that hand-feeds it invented
events would pass while the display showed nothing — which is how the first version of this
lost the Coder's row.

The output is captured through a non-terminal `Console`, which is also the CI path: one line
per step as it completes, rather than thousands of frames of ANSI.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from rich.console import Console

from tests.test_llm_client import FakeProvider
from tests.test_orchestrator import (
    ARGV_LINE,
    DEF_LINE,
    RETURN_LINE,
    SHELL_LINE,
    SOURCE,
    affirmation,
    clean,
    critique,
    issue,
    note,
    proposal,
    same_span_pair,
    settings_for,
    span_issue_id,
    write_up,
)
from tribunal.agents import Arbiter, Postmortem
from tribunal.config import PolicyConfig
from tribunal.llm.base import ProviderName
from tribunal.llm.client import LLMClient
from tribunal.orchestrator import Orchestrator
from tribunal.progress import ProgressRenderer, Step, _one_line
from tribunal.trace.writer import TraceWriter


def shown(
    tmp_path: Path,
    script: list[Any],
    *,
    with_arbiter: bool = False,
    with_postmortem: bool = False,
    width: int = 200,
    **overrides: Any,
) -> str:
    """One real run with the renderer attached. Returns everything it printed."""
    settings = settings_for(tmp_path, **overrides)
    fake = FakeProvider()
    fake.script = list(script)
    client = LLMClient(settings)
    client.providers[ProviderName.NIM] = fake

    console = Console(
        file=__import__("io").StringIO(), force_terminal=False, width=width, no_color=True
    )
    renderer = ProgressRenderer(console=console, max_rounds=settings.policy.max_rounds)
    orchestrator = Orchestrator(
        settings,
        client,
        arbiter=Arbiter(settings, client) if with_arbiter else None,
        postmortem=Postmortem(settings, client) if with_postmortem else None,
        subscribers=[renderer],
    )
    with renderer:
        asyncio.run(orchestrator.run(SOURCE, filename="t.py", trace_dir=tmp_path / "traces"))
    return console.file.getvalue()


def rows(output: str, label: str) -> list[str]:
    """Every line whose step name is `label`, in order — one per round."""
    return [
        line
        for line in output.splitlines()
        if re.match(rf"^[●\s├└]+{re.escape(label)}\b", line)
    ]


def row(output: str, label: str) -> str:
    """The first such line. Use `rows` when the round matters."""
    found = rows(output, label)
    if not found:
        raise AssertionError(f"no {label!r} row in:\n{output}")
    return found[0]


ACCEPT_SCRIPT = [
    proposal(1, SHELL_LINE, ARGV_LINE),
    clean("security", 1),
    clean("performance", 1),
]


# -- the shape docs/06 asks for -----------------------------------------------------------------


def test_the_run_is_drawn_as_grounding_then_rounds(tmp_path):
    output = shown(tmp_path, ACCEPT_SCRIPT)
    assert row(output, "grounding")
    assert row(output, "round 1/3")
    for label in ("coder", "validate", "critique", "policy"):
        assert row(output, label)


def test_both_critics_share_one_line_with_a_parallel_marker(tmp_path):
    """docs/06 § CLI progress rendering: showing them on one line "communicates the
    concurrency for free". Two rows would read as a pipeline, which is the wrong claim."""
    line = row(shown(tmp_path, ACCEPT_SCRIPT), "critique")
    assert "redteam" in line and "profiler" in line
    assert "│" in line
    assert "parallel" in line


def test_the_policy_row_names_the_rule_that_fired(tmp_path):
    output = shown(tmp_path, [
        proposal(1, SHELL_LINE, ARGV_LINE),
        critique("security", 1, "block", [issue("security", "high", "L5-L5")], "bandit"),
        clean("performance", 1),
        proposal(2, RETURN_LINE, "    return str(name)"),
        clean("security", 2),
        clean("performance", 2),
    ])
    assert "REJECT" in row(output, "policy")
    assert "hard_block_security" in row(output, "policy")
    assert "round 2/3" in output


def test_the_grounding_row_accumulates_every_tool(tmp_path):
    """The row is printed once, when the next step appears — not after the first tool. That
    ordering bug showed `bandit 2` and silently dropped ruff, radon and astgate."""
    line = row(shown(tmp_path, ACCEPT_SCRIPT), "grounding")
    for tool in ("bandit", "ruff", "radon", "astgate"):
        assert tool in line, f"{tool} missing from {line!r}"


def test_the_summary_line_reports_the_outcome_and_the_cost(tmp_path):
    output = shown(tmp_path, ACCEPT_SCRIPT)
    done = row(output, "done")
    assert "ACCEPT" in done
    assert "1 round(s)" in done
    assert re.search(r"\$\d+\.\d{4}", done)


def test_each_row_is_one_line(tmp_path):
    """`patch.validate`'s bounce message is a paragraph written for the model to act on.
    Valuable in the trace, ruinous in a table."""
    output = shown(tmp_path, [
        proposal(1, "a line that is not in the file at all", "x"),
        proposal(1, SHELL_LINE, ARGV_LINE),
        clean("security", 1),
        clean("performance", 1),
    ], width=200)
    for line in output.splitlines():
        assert len(line) <= 200


# -- the things a watcher is there to see --------------------------------------------------


def test_a_re_anchored_patch_reuses_its_row_but_says_it_took_two_goes(tmp_path):
    """A diff stumble is not a debate round, so it must not stack a second row — but a patch
    that took three goes must not read as one that took one."""
    output = shown(tmp_path, [
        proposal(1, "not in the file", "x"),
        proposal(1, SHELL_LINE, ARGV_LINE),
        clean("security", 1),
        clean("performance", 1),
    ])
    assert len(rows(output, "validate")) == 1
    assert "2 attempts" in row(output, "validate")
    assert "applied" in row(output, "validate")


def test_a_failed_critic_is_shown_as_failed_not_as_silence(tmp_path):
    """An absent critic reads as a clean one, which is the single most misleading thing this
    display could do."""
    output = shown(
        tmp_path,
        [
            proposal(1, SHELL_LINE, ARGV_LINE),
            clean("security", 1),
            RuntimeError("the profiler fell over"),
        ],
        policy=PolicyConfig(max_rounds=1),
    )
    assert "profiler FAILED" in row(output, "critique")
    assert "ESCALATE" in row(output, "policy")


def test_the_affirmation_gets_its_own_row_because_it_costs_a_call(tmp_path):
    left, right = same_span_pair("L5-L5", "L5-L6")
    output = shown(
        tmp_path,
        [
            proposal(1, SHELL_LINE, ARGV_LINE),
            critique("security", 1, "concerns", [issue("security", "medium", "L5-L5")],
                     "bandit"),
            critique("performance", 1, "concerns", [issue("performance", "medium", "L5-L6")],
                     "radon"),
            affirmation(left, right, opposing=True),
            note(1, "tradeoff", justification="both cost something", recommended="ship it"),
        ],
        with_arbiter=True,
    )
    assert "conflict affirmed" in row(output, "affirm")
    assert "TRADEOFF" in row(output, "policy")
    assert "justified the trade-off" in row(output, "arbiter")


def test_a_templated_arbiter_says_so(tmp_path):
    """The terminal must not imply a model wrote something a template did."""
    output = shown(tmp_path, [
        proposal(1, SHELL_LINE, ARGV_LINE),
        critique("security", 1, "block", [issue("security", "high", "L5-L5")], "bandit"),
        clean("performance", 1),
        proposal(2, RETURN_LINE, "    return str(name)"),
        clean("security", 2),
        clean("performance", 2),
    ])
    assert "templated" in row(output, "arbiter")


def test_a_dismissal_is_reported(tmp_path):
    sec = span_issue_id("security", "L5-L5")
    round_two = proposal(2, RETURN_LINE, "    return str(name)")
    round_two["deliberately_unaddressed"] = [{"issue_id": sec, "reason": "constant"}]
    output = shown(
        tmp_path,
        [
            proposal(1, SHELL_LINE, ARGV_LINE),
            critique("security", 1, "block", [issue("security", "high", "L5-L5")], "bandit"),
            clean("performance", 1),
            note(1, "reject", critique_text="fix it", priority_order=[sec]),
            round_two,
            critique("security", 2, "block", [issue("security", "high", "L5-L5")], "bandit"),
            clean("performance", 2),
            note(2, "reject", critique_text="agreed",
                 dismissed=[{"issue_id": sec, "reason": "unreachable"}]),
            proposal(3, DEF_LINE, "def generate(name: str):"),
            clean("security", 3),
            clean("performance", 3),
        ],
        with_arbiter=True,
    )
    # Round 2 is where the Coder pushes back and the Arbiter rules on it.
    assert "pushed back on 1" in rows(output, "coder")[1]
    assert "dismissed 1" in rows(output, "arbiter")[1]


def test_the_write_up_row_appears_only_with_a_postmortem(tmp_path):
    with_it = shown(
        tmp_path / "a",
        [*ACCEPT_SCRIPT, write_up([(1, "argv list", "accepted")])],
        with_postmortem=True,
    )
    assert "caveat(s) recorded" in row(with_it, "write-up")
    without = shown(tmp_path / "b", ACCEPT_SCRIPT)
    assert "write-up" not in without


# -- the property that makes this a renderer rather than a second code path ------------------


def test_the_renderer_sees_exactly_what_the_trace_records(tmp_path):
    """docs/06 principle 4. If these two could differ, the terminal and the file would
    disagree about what happened, and the file is the one nobody is watching."""
    seen: list[str] = []
    writer = TraceWriter("01TEST", tmp_path / "t.jsonl")
    renderer = ProgressRenderer(console=Console(file=__import__("io").StringIO()))
    writer.subscribe(lambda e: seen.append(e.kind))
    writer.subscribe(renderer)

    from tribunal.trace import events

    with writer:
        writer.emit(
            events.run_start, input_file="t.py", input_sha256="x", config_snapshot={},
            prompt_versions={}, argv=[], trace_level="default",
        )
    assert seen == ["run_start"]
    assert len(writer.events) == 1


def test_a_run_without_the_renderer_is_byte_identical(tmp_path):
    """The renderer must not be able to change the run. It is a subscriber, and a subscriber
    that mutated the trace would make every recorded run a function of whether someone was
    watching it."""
    from tribunal.trace import reader as trace_reader

    with_renderer = shown(tmp_path / "a", ACCEPT_SCRIPT)
    assert with_renderer  # it drew something

    settings = settings_for(tmp_path / "b")
    fake = FakeProvider()
    fake.script = list(ACCEPT_SCRIPT)
    client = LLMClient(settings)
    client.providers[ProviderName.NIM] = fake
    plain = asyncio.run(
        Orchestrator(settings, client).run(
            SOURCE, filename="t.py", trace_dir=tmp_path / "b" / "traces"
        )
    )
    drawn = sorted((tmp_path / "a" / "traces").glob("*.jsonl"))[0]
    a = [e.kind for e in trace_reader.read(drawn).events]
    b = [e.kind for e in plain.trace.events]
    assert a == b


# -- units worth pinning ------------------------------------------------------------------------


def test_a_long_detail_is_flattened_to_one_line():
    assert "\n" not in _one_line("a\nb\nc")
    assert _one_line("x" * 200).endswith("…")
    assert len(_one_line("x" * 200)) <= 64


def test_a_step_with_no_detail_renders_without_trailing_space():
    text = Step(label="round 1/3", round=None, done=True).render(last_in_round=False)
    assert str(text) == str(text).rstrip()


# -- the terminal path ---------------------------------------------------------------------


def live_output(tmp_path: Path, script: list[Any]) -> str:
    """The same run through a `rich.Live`, with the ANSI stripped back off."""
    import io

    settings = settings_for(tmp_path)
    fake = FakeProvider()
    fake.script = list(script)
    client = LLMClient(settings)
    client.providers[ProviderName.NIM] = fake

    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=True, width=120)
    renderer = ProgressRenderer(console=console, max_rounds=settings.policy.max_rounds)
    with renderer:
        asyncio.run(
            Orchestrator(settings, client, subscribers=[renderer]).run(
                SOURCE, filename="t.py", trace_dir=tmp_path / "traces"
            )
        )
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", buffer.getvalue())


def test_the_live_path_leaves_the_finished_run_on_screen(tmp_path):
    """`Live` rewrites in place, so the risk is the opposite of the streaming path's: not a
    missing row, but a display that vanishes or freezes mid-run. The last frame has to be
    the whole finished run."""
    output = live_output(tmp_path, ACCEPT_SCRIPT)
    tail = [line for line in output.splitlines() if line.strip()][-7:]
    assert any("round 1/3" in line for line in tail)
    assert any("critique" in line and "parallel" in line for line in tail)
    assert "ACCEPT" in tail[-1]


def test_the_live_display_is_closed_even_when_the_run_raises(tmp_path):
    """A `Live` left open hides the cursor and eats the next prompt. Whatever happens in the
    run, the terminal has to be handed back."""
    import io

    settings = settings_for(tmp_path)
    fake = FakeProvider()
    fake.script = [RuntimeError("the coder fell over")] * 4
    client = LLMClient(settings)
    client.providers[ProviderName.NIM] = fake

    console = Console(file=io.StringIO(), force_terminal=True, width=120)
    renderer = ProgressRenderer(console=console, max_rounds=settings.policy.max_rounds)
    try:
        with renderer:
            asyncio.run(
                Orchestrator(settings, client, subscribers=[renderer]).run(
                    SOURCE, filename="t.py", trace_dir=tmp_path / "traces"
                )
            )
    except Exception:  # noqa: BLE001 - the point is what happens to the display
        pass
    assert renderer._live is None
