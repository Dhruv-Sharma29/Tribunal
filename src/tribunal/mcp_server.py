"""MCP server: the real tribunal, callable from an editor.

docs/08-packaging.md § MCP server. It is a thin adapter over `Orchestrator.run`, which is the
whole argument for doing it at all — the editor gets grounding, parallel critique, the policy
layer and a trace, rather than a model roleplaying a reviewer with a rubric.

## The SDK moved, and docs/08's sketch is v1

docs/08 sketches `from mcp.server.fastmcp import FastMCP` and marks the area `[verify]`
because "these have moved more than once". They have: `mcp` 2.x renamed `FastMCP` to
`MCPServer` (`mcp.server.mcpserver`), and `Tool.inputSchema` is now `Tool.input_schema`. The
v1 import raises `ModuleNotFoundError` with a migration note rather than failing obscurely,
which is a kind thing for a library to do and does not make the sketch runnable.

This module is written against **mcp 2.2.0**, verified by probing the installed package, and
the dependency is pinned `>=2.0` so a v1 install fails at resolution rather than at import.

## Two deliberate differences from the CLI

**`allow_exec` defaults off and is per-call.** docs/08: "An editor-triggered tool that
executes code on the developer's machine is a worse default than the same thing typed
deliberately into a terminal." A CLI user typed `--allow-exec`; an editor user may not know a
tool call happened at all. The Profiler degrades to `unmeasurable` without it, which the
schema already supports.

**`max_rounds` defaults to 2, not 3.** docs/08's recommendation, and the reason is patience
rather than quality: a tool call that returns in four minutes reads as a hang in most host
panels. The caller can raise it.

## What a tool returns

Markdown, not JSON. docs/08: "A tool result that's a wall of JSON is unreadable in most host
panels." The summary carries the outcome, the rule that fired, the issues and their fates,
the diff and the trace path; a caller that wants structure reads the trace, whose path is in
the summary, or calls `get_trace`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tribunal import __version__
from tribunal.config import Settings
from tribunal.contracts import Report
from tribunal.trace import reader as trace_reader
from tribunal.trace import report as report_builder

#: Kept small on purpose. docs/08 names exactly these two, and a third would be a third
#: thing to keep working against a host contract that has already moved once.
TOOL_NAMES = ("review_code", "get_trace")

INSTRUCTIONS = """\
Runs an adversarial code review on a single Python file: a Coder proposes a patch, a
security critic and a performance critic assess it independently and in parallel, and a
deterministic policy layer decides. Every critique is grounded in real tool output
(bandit, ruff, radon, pytest).

Use `review_code` for a file the user wants reviewed or fixed. It takes minutes, not
seconds. Use `get_trace` to read back a previous review by its run id.

The tribunal never executes the file unless `allow_exec=true` is passed explicitly.\
"""


def build_server(settings: Settings | None = None, client: Any = None) -> Any:
    """Construct the server. Importing `mcp` lazily keeps the core package installable
    without it, the same way the provider SDKs are optional.

    `client` is the seam the tests use: one `LLMClient` with a scripted provider seated
    drives both tools through the real `Orchestrator` with no API call. It is the same
    injection point `Orchestrator` already offers, for the same reason — a server whose only
    test was "does it start" would be a thin adapter nobody had ever run.
    """
    try:
        from mcp.server.mcpserver import MCPServer
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on the environment
        raise ModuleNotFoundError(
            "the MCP server needs the `mcp` SDK: pip install 'tribunal[mcp]'. "
            "Note it must be mcp >= 2.0 — 1.x exposes FastMCP rather than MCPServer."
        ) from exc

    base = settings or Settings.load()
    shared = client
    server = MCPServer(
        name="tribunal",
        title="tribunal",
        description="Adversarial code review with a deterministic decision layer.",
        instructions=INSTRUCTIONS,
        website_url="https://github.com/meghagarg/tribunal",
    )

    @server.tool()
    async def review_code(
        file_path: str,
        test_path: str | None = None,
        max_rounds: int = 2,
        allow_exec: bool = False,
    ) -> str:
        """Review and patch a single Python file with the full tribunal.

        Returns a markdown summary: the outcome, the policy rule that produced it, every
        issue found and what became of it, the accepted diff, and the path to the trace.

        Takes minutes. `allow_exec` is off by default and running the file — including any
        test file — requires passing it explicitly.
        """
        target = Path(file_path)
        problem = _reject(target, test_path, allow_exec, max_rounds)
        if problem:
            return problem

        settings_ = _settings_for(base, max_rounds, allow_exec)
        from tribunal.llm.client import LLMClient

        # One client for the availability check and the run, so the check cannot pass
        # against a provider the run will not use.
        llm = shared or LLMClient(settings_)
        unusable = _unusable_agents(settings_, llm)
        if unusable:
            return (
                "**Cannot run — no usable model provider.**\n\n"
                + "\n".join(f"- {line}" for line in unusable)
                + "\n\nSet the provider's API key in the environment the MCP server "
                "starts in, not in the tool call."
            )

        result = await _run(settings_, llm, target, test_path)
        return _render_report(result.report, result.trace_path)

    @server.tool()
    async def get_trace(run_id: str) -> str:
        """Read back a previous review by its run id, as markdown. Makes no model calls.

        The report is re-derived from the trace by the same function that produced it
        during the run, so this cannot drift from what the review actually concluded.
        """
        from tribunal.trace.writer import traces_in

        directory = base.trace_dir
        candidates = traces_in(directory)
        path = directory / f"{run_id}.jsonl"
        # `traces_in`, not a bare glob: `summary.jsonl` sits in the same directory and is
        # an aggregate, not a run. Offering it as a "recent run" would send the caller to
        # a file that parses as neither.
        if path not in candidates:
            available = [p.stem for p in candidates][-5:]
            return (
                f"No trace for run id `{run_id}` under `{directory}`."
                + (f" Recent runs: {', '.join(available)}." if available else "")
            )
        try:
            trace = trace_reader.read(path)
        except trace_reader.TraceError as exc:
            return f"`{path}` is not a readable trace: {exc}"
        return _render_report(report_builder.build(trace), path, trace.warnings)

    return server


# -- the parts worth testing without an SDK -------------------------------------------------


def _reject(
    target: Path, test_path: str | None, allow_exec: bool, max_rounds: int
) -> str | None:
    """Argument problems, phrased for a model to act on rather than for a shell.

    A tool result is read by an assistant that will try again, so each message says what to
    do differently. `Exit(USAGE)` has no meaning here.
    """
    if not target.is_file():
        return f"No such file: `{target}`. Pass a path to a single Python file."
    if target.suffix != ".py":
        return (
            f"`{target}` is not a Python file. The tribunal reviews single-file Python only — "
            "see the charter's scope table."
        )
    if test_path and not allow_exec:
        # The same rule the CLI enforces: naming a test file is asking to execute it.
        return (
            "A `test_path` was given but `allow_exec` is false. Running a test file "
            "imports and executes it, including any module-level code in the target. Pass "
            "`allow_exec=true` only if the user has asked for the tests to be run."
        )
    if test_path and not Path(test_path).is_file():
        return f"No such test file: `{test_path}`."
    if max_rounds < 1:
        return "`max_rounds` must be at least 1."
    return None


def _settings_for(base: Settings, max_rounds: int, allow_exec: bool) -> Settings:
    return base.model_copy(
        update={
            "policy": base.policy.model_copy(update={"max_rounds": max_rounds}),
            "sandbox": base.sandbox.model_copy(update={"allow_exec": allow_exec}),
        }
    )


def _unusable_agents(settings: Settings, client: Any) -> list[str]:
    """Checked before the run, so a missing key is one clear sentence rather than a
    traceback after the grounding suite has already run — which over stdio would reach the
    user as a tool error with a stack trace in it, if it reached them at all.

    Asks `client.providers`, not the registry: those are the provider objects the run will
    use, and a check against separately-built ones is a second source of truth that can
    agree with nothing.
    """
    out = []
    for role in ("coder", "redteam", "profiler"):
        config = getattr(settings.agents, role)
        provider = client.providers[config.resolved_provider()]
        reason = provider.available()
        if reason is not None:
            out.append(f"{role}: {reason}")
    return out


async def _run(settings: Settings, client: Any, target: Path, test_path: str | None):
    from tribunal.agents import Arbiter, Postmortem
    from tribunal.orchestrator import Orchestrator

    orchestrator = Orchestrator(
        settings,
        client,
        arbiter=Arbiter(settings, client),
        postmortem=Postmortem(settings, client),
    )
    return await orchestrator.run(
        target.read_text(encoding="utf-8"),
        filename=target.name,
        test_source=(
            Path(test_path).read_text(encoding="utf-8") if test_path else None
        ),
        argv=["mcp:review_code", target.name],
    )


OUTCOME_NOTE = {
    "accept": "The patch was accepted.",
    "tradeoff": (
        "**The critics wanted incompatible things.** The patch ships with the other "
        "concern recorded rather than dropped — a human decides."
    ),
    "escalate": "**The run stopped without accepting a patch.** See the rule below.",
    "reject": "The final round was rejected.",
}


def _render_report(report: Report, trace_path: Path | None, warnings=()) -> str:
    """Markdown, because a wall of JSON is unreadable in a host panel (docs/08)."""
    lines = [
        f"## {report.outcome.value.upper()} — `{report.input_file}`",
        "",
        OUTCOME_NOTE.get(report.outcome.value, ""),
        "",
        f"- rule fired: `{report.rule_fired}`",
        f"- rounds: {report.rounds_used}",
        f"- cost: ${report.total_cost_usd:.4f} · {report.wall_seconds:.0f}s",
        f"- run id: `{report.run_id}`",
    ]
    if report.unassessed_dimensions:
        names = ", ".join(d.value for d in report.unassessed_dimensions)
        lines.append(
            f"- **unassessed: {names}** — nobody checked these. Not the same as clean."
        )
    for warning in warnings:
        lines.append(f"- ⚠ {warning}")

    if report.issues:
        lines += ["", "### Issues", ""]
        for fate in report.issues:
            issue = fate.issue
            lines.append(
                f"- `{issue.id}` **{issue.severity.value}** "
                f"{issue.dimension.value} — {issue.title} *({fate.status})*"
            )

    if report.conflict:
        conflict = report.conflict
        lines += [
            "",
            "### The trade-off",
            "",
            f"`{conflict.left_issue}` vs `{conflict.right_issue}` "
            f"on axis *{conflict.axis}*.",
            f"- {conflict.left_issue} costs: {conflict.left_remedy_cost}",
            f"- {conflict.right_issue} costs: {conflict.right_remedy_cost}",
        ]
        if report.recommended_default:
            lines += ["", f"**Recommended default:** {report.recommended_default}"]

    if report.accepted_diff:
        lines += ["", "### Patch", "", "```diff", report.accepted_diff.rstrip(), "```"]
    else:
        lines += ["", f"**No patch was accepted.** {report.no_patch_reason}"]

    if report.narrative:
        lines += ["", report.narrative.rstrip()]

    if trace_path:
        lines += [
            "",
            f"Trace: `{trace_path}` — `tribunal view {trace_path}` renders it, "
            f"or call `get_trace` with run id `{report.run_id}`.",
        ]
    lines += ["", f"<sub>tribunal {__version__}</sub>"]
    return "\n".join(line for line in lines if line is not None)


def main() -> None:  # pragma: no cover - the stdio loop
    """`tribunal-mcp`, or `python -m tribunal.mcp_server`.

    stdio because that is what hosts register, and because it has no port, no TLS and no
    second process to supervise.
    """
    build_server().run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
