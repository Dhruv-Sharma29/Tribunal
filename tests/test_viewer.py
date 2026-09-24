"""The single-file trace viewer.

Two kinds of test here, and the second is the one that matters.

The Python half checks the properties the file must have on disk: no unsubstituted
placeholders, nothing fetched over the network, and a trace full of hostile text embedded
inertly. Those are cheap and they catch the bugs that make the file unopenable.

The JS half actually **runs the viewer's script** against a minimal DOM (`viewer_dom.js`) and
asserts on what it rendered. Without it, a runtime error in the first line of `renderHeader`
produces a blank page and a green test suite — which is exactly the failure mode a viewer has,
because nothing else in the system consumes it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.test_orchestrator import (
    ARGV_LINE,
    RETURN_LINE,
    SHELL_LINE,
    affirmation,
    clean,
    critique,
    drive,
    issue,
    note,
    proposal,
    same_span_pair,
    write_up,
)
from tribunal import viewer
from tribunal.cli import app
from tribunal.trace import reader as trace_reader

DOM_DRIVER = Path(__file__).parent / "viewer_dom.js"
needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="the DOM harness needs node on PATH"
)


# -- traces to render -------------------------------------------------------------------------


def accepted_run(tmp_path):
    return drive(tmp_path, [
        proposal(1, SHELL_LINE, ARGV_LINE),
        critique("security", 1, "block", [issue("security", "high", "L5-L5")], "bandit"),
        clean("performance", 1),
        proposal(2, RETURN_LINE, "    return str(name)"),
        clean("security", 2),
        clean("performance", 2),
    ])


def tradeoff_run(tmp_path):
    """The run worth looking at: two critics, a conflict, an Arbiter and a write-up."""
    left, right = same_span_pair("L5-L5", "L5-L6")
    return drive(
        tmp_path,
        [
            proposal(1, SHELL_LINE, ARGV_LINE),
            critique("security", 1, "concerns", [issue("security", "medium", "L5-L5")],
                     "bandit"),
            critique("performance", 1, "concerns", [issue("performance", "medium", "L5-L6")],
                     "radon"),
            affirmation(left, right, opposing=True),
            note(1, "tradeoff", justification="the check sits inside the measured loop",
                 recommended="ship the validated version unless this is the hot path"),
            write_up(
                [(1, "replaced the shell string with an argv list", "both objections stand")],
                outcome="tradeoff",
                caveats=[f"{left} and {right} are unresolved by construction"],
                disagreement="the red-team wanted a check where the profiler wanted none",
            ),
        ],
        with_arbiter=True,
        with_postmortem=True,
    )


def render(tmp_path, result) -> str:
    return viewer.render(trace_reader.read(result.trace_path))


# -- the file on disk ---------------------------------------------------------------------------


def test_every_placeholder_is_substituted(tmp_path):
    html = render(tmp_path, accepted_run(tmp_path))
    assert "__CSS__" not in html and "__JS__" not in html and "__DATA__" not in html
    assert html.startswith("<!doctype html>")


def test_the_file_fetches_nothing(tmp_path):
    """docs/06: no CDN, no server, no build step — "the demo GIF must not depend on a network
    round-trip". A `<link>` or an `http://` in here is that dependency."""
    html = render(tmp_path, tradeoff_run(tmp_path))
    # The *data* may legitimately contain a URL — a trace records the provider base_url in
    # its config snapshot — so the claim is about the viewer's own markup. Cut the blob out
    # first, or this passes and fails on what the run happened to talk to.
    start = html.index('id="trace-data">')
    chrome = html[:start] + html[html.index("</script>", start):]
    # The SVG namespace is an identifier, not a request, and is the one URL-shaped string
    # allowed. Removed explicitly so it cannot mask a real one.
    assert chrome.count("http://www.w3.org/2000/svg") >= 1
    chrome = chrome.replace("http://www.w3.org/2000/svg", "")
    for forbidden in ("http://", "https://", "<link", "@import", "fetch(", "XMLHttpRequest"):
        assert forbidden not in chrome, f"the viewer reaches for {forbidden}"


def test_the_css_and_the_script_are_inline(tmp_path):
    html = render(tmp_path, accepted_run(tmp_path))
    assert "<style>" in html
    assert '<script type="application/json" id="trace-data">' in html


def test_a_trace_containing_a_script_tag_cannot_break_out(tmp_path):
    """A trace carries model-written source. `</script>` in a diff would otherwise close the
    blob early and turn the rest of the run into markup — in a file people email each other.
    """
    events = [e.model_dump(mode="json") for e in trace_reader.read(
        accepted_run(tmp_path).trace_path
    ).events]
    events[0]["payload"]["input_file"] = "</script><img src=x onerror=alert(1)>.py"
    blob = viewer._embed(events)
    assert "</script>" not in blob
    assert "<" not in blob and ">" not in blob
    assert json.loads(blob.replace("\\u003c", "<").replace("\\u003e", ">")
                      .replace("\\u0026", "&"))[0]["payload"]["input_file"].startswith(
        "</script>"
    )


def test_the_escaped_blob_is_still_valid_json(tmp_path):
    html = render(tmp_path, tradeoff_run(tmp_path))
    start = html.index('id="trace-data">') + len('id="trace-data">')
    blob = html[start:html.index("</script>", start)]
    events = json.loads(blob)
    assert events[0]["kind"] == "run_start"
    assert any(e["kind"] == "policy_decision" for e in events)


def test_trace_content_cannot_be_read_as_a_placeholder(tmp_path):
    """Chained `.replace()` calls would let a reviewed file whose text contains `__JS__` have
    the viewer script spliced into the JSON blob. One pass, so it cannot."""
    events = [e.model_dump(mode="json") for e in trace_reader.read(
        accepted_run(tmp_path).trace_path
    ).events]
    events[0]["payload"]["input_file"] = "__JS__ __CSS__ __DATA__.py"
    blob = viewer._embed(events)
    assert "__JS__" in blob  # survived into the data...
    html = viewer.PLACEHOLDER.sub(lambda m: {"__DATA__": blob}.get(m.group(0), ""),
                                  "__DATA__ __JS__")
    assert html == blob + " "  # ...and was not re-scanned


def test_a_warning_on_the_trace_reaches_the_reader(tmp_path):
    """A gap in `seq` means events were lost. A viewer that hid that would present an
    incomplete run as a complete one."""
    result = accepted_run(tmp_path)
    trace = trace_reader.read(result.trace_path)
    trace.warnings.append("seq gap between 4 and 6")
    html = viewer.render(trace)
    assert "seq gap between 4 and 6" in html


def test_write_puts_the_file_where_it_is_asked(tmp_path):
    result = accepted_run(tmp_path)
    out = tmp_path / "nested" / "trace.html"
    assert viewer.write(trace_reader.read(result.trace_path), out) == out
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")


# -- the CLI --------------------------------------------------------------------------------------


def test_view_writes_html_next_to_the_trace(tmp_path):
    result = accepted_run(tmp_path)
    outcome = CliRunner().invoke(app, ["view", str(result.trace_path), "--no-open"])
    assert outcome.exit_code == 0, outcome.output
    assert result.trace_path.with_suffix(".html").is_file()
    assert "works offline" in outcome.output


def test_view_on_a_missing_trace_is_a_usage_error(tmp_path):
    outcome = CliRunner().invoke(app, ["view", str(tmp_path / "nope.jsonl"), "--no-open"])
    assert outcome.exit_code == 64


def test_view_makes_no_api_calls(tmp_path, monkeypatch):
    """Rendering is pure. The command must work on a machine with no credential at all."""
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    result = accepted_run(tmp_path)
    outcome = CliRunner().invoke(
        app, ["view", str(result.trace_path), "-o", str(tmp_path / "x.html"), "--no-open"]
    )
    assert outcome.exit_code == 0, outcome.output


# -- the viewer actually running --------------------------------------------------------------


def rendered(tmp_path, result) -> dict:
    """Run the viewer's own script against a minimal DOM and return what it drew."""
    path = tmp_path / "view.html"
    viewer.write(trace_reader.read(result.trace_path), path)
    proc = subprocess.run(
        ["node", str(DOM_DRIVER), str(path)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@needs_node
def test_every_tab_renders_something(tmp_path):
    """One broken tab must not be able to hide behind the default tab working."""
    out = rendered(tmp_path, tradeoff_run(tmp_path))
    assert out["tabs"] == ["Timeline", "Debate", "Diffs", "Grounding", "Cost"]
    for name, html in out["panels"].items():
        assert len(html) > 120, f"the {name} tab rendered nothing: {html}"


@needs_node
def test_the_header_states_the_outcome_and_the_run(tmp_path):
    out = rendered(tmp_path, tradeoff_run(tmp_path))
    assert 'class="outcome tradeoff">TRADEOFF' in out["header"]
    assert "1 round(s)" in out["header"]


@needs_node
def test_the_two_critics_render_side_by_side(tmp_path):
    """Requirement 1 of docs/06 § The viewer, and the single most important rendering
    decision in the file: the visual claim is independence, and a vertical list reads as a
    pipeline."""
    debate = rendered(tmp_path, tradeoff_run(tmp_path))["panels"]["Debate"]
    assert '<div class="critics">' in debate
    security = debate.index('class="card critic security"')
    performance = debate.index('class="card critic performance"')
    grid = debate.index('<div class="critics">')
    # Both critics inside the one grid container, which is what makes them columns.
    assert grid < security < performance


@needs_node
def test_the_policy_decision_is_its_own_dominant_block(tmp_path):
    """Requirement 2: the thing a viewer should notice within two seconds."""
    debate = rendered(tmp_path, tradeoff_run(tmp_path))["panels"]["Debate"]
    assert '<div class="policy tradeoff">' in debate
    assert '<span class="decision">TRADEOFF</span>' in debate
    assert '<span class="rule">irreconcilable</span>' in debate


@needs_node
def test_evidence_is_one_click_away(tmp_path):
    """Requirement 3: what turns "the agent said so" into "the agent cited this, here it is"."""
    debate = rendered(tmp_path, tradeoff_run(tmp_path))["panels"]["Debate"]
    assert '<details class="issue">' in debate
    assert '<div class="evidence">' in debate
    assert '<span class="ref">t.py:L5-L5</span>' in debate


@needs_node
def test_the_write_up_and_its_caveats_reach_the_timeline(tmp_path):
    timeline = rendered(tmp_path, tradeoff_run(tmp_path))["panels"]["Timeline"]
    assert "What I would not trust" in timeline
    assert "unresolved by construction" in timeline


@needs_node
def test_an_unassessed_dimension_is_not_drawn_as_a_missing_card(tmp_path):
    """The absence of a critic card must not read as the absence of a problem."""
    result = drive(
        tmp_path,
        [
            proposal(1, SHELL_LINE, ARGV_LINE),
            clean("security", 1),
            RuntimeError("the profiler fell over"),
        ],
        policy=__import__(
            "tribunal.config", fromlist=["PolicyConfig"]
        ).PolicyConfig(max_rounds=1),
    )
    debate = rendered(tmp_path, result)["panels"]["Debate"]
    assert "UNASSESSED" in debate
    assert "not the same as clean" in debate
    assert "the profiler fell over" in debate


@needs_node
def test_the_previous_rounds_diff_is_shown_beside_this_one(tmp_path):
    """Requirement 5: A→B→A cycling is obvious when the two are adjacent, invisible when
    they are not."""
    diffs = rendered(tmp_path, accepted_run(tmp_path))["panels"]["Diffs"]
    assert '<div class="diffpair">' in diffs
    assert "round 1 (previous)" in diffs


@needs_node
def test_the_cost_tab_totals_every_priced_call(tmp_path):
    cost = rendered(tmp_path, tradeoff_run(tmp_path))["panels"]["Cost"]
    assert "<tfoot>" in cost
    for actor in ("coder", "redteam", "profiler", "arbiter", "postmortem"):
        assert ">" + actor + "<" in cost


@needs_node
def test_a_zero_cache_read_run_is_called_out(tmp_path):
    """docs/06: a zero cache-read means a silent prefix invalidator, "and you will not notice
    it any other way until the bill arrives"."""
    cost = rendered(tmp_path, accepted_run(tmp_path))["panels"]["Cost"]
    assert "volatile leaked into the cached prompt prefix" in cost


@needs_node
def test_hostile_trace_content_is_rendered_as_text(tmp_path):
    """The end-to-end version of the escaping test: a `<script>` in the *data* must come out
    of the DOM as characters, never as a node."""
    result = accepted_run(tmp_path)
    trace = trace_reader.read(result.trace_path)
    trace.events[0].payload["input_file"] = "<script>alert(1)</script>.py"
    path = tmp_path / "hostile.html"
    path.write_text(viewer.render(trace), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(DOM_DRIVER), str(path)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    # The DOM harness escapes text on the way out, so a string the viewer *rendered* and an
    # element the viewer *built* look different here. This is the former.
    assert "&lt;script&gt;alert(1)&lt;/script&gt;.py" in out["header"]
    assert "<script>" not in out["header"]


@needs_node
def test_the_viewer_never_touches_innerhtml(tmp_path):
    """Enforced by the shim, which throws on access. The trace holds model-written code, so
    building markup from it by string concatenation is a code-execution path."""
    source = (viewer.ASSETS / "viewer.js").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)  # the header comment names it
    code = "\n".join(line for line in code.splitlines() if not line.strip().startswith("//"))
    assert "innerHTML" not in code
    rendered(tmp_path, tradeoff_run(tmp_path))  # the shim throws on access, so this proves it


@needs_node
def test_a_truncated_trace_still_renders(tmp_path):
    """docs/06: a run killed mid-flight is exactly when you want to read its trace."""
    result = accepted_run(tmp_path)
    lines = result.trace_path.read_text(encoding="utf-8").splitlines()
    cut = tmp_path / "cut.jsonl"
    cut.write_text("\n".join(lines[:9]) + "\n", encoding="utf-8")

    path = tmp_path / "cut.html"
    viewer.write(trace_reader.read(cut), path)
    proc = subprocess.run(
        ["node", str(DOM_DRIVER), str(path)], capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["panels"]["Debate"]  # no policy decision yet, but it still draws
