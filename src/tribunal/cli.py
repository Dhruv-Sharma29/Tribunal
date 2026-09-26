"""Typer CLI.

Two commands work without an LLM at all: `doctor`, which reports what the grounding and
sandbox layers can actually do in this environment, and `ground`, which runs the grounding
suite and prints the normalised findings. `replay` needs no credential either -- it rebuilds a
report from a recorded trace, and `view` renders one as a self-contained HTML file. Only
`postmortem` calls a provider without running the loop.

`doctor` is not a courtesy command. Every critic claim is grounded in a tool result, so a
missing `bandit` silently degrades the Red-team from "grounded" to "opinion" -- which is the
one failure mode the project exists to avoid. It needs to be one command to check.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from tribunal import __version__, viewer
from tribunal.agents import (
    Arbiter,
    Coder,
    CoderBundle,
    CritiqueBundle,
    Postmortem,
    Profiler,
    RedTeam,
)
from tribunal.config import PRICE_TABLE_VERSION, PRICES, Settings
from tribunal.contracts import SCHEMA_VERSION, GroundingReport
from tribunal.eval import arms as arms_mod
from tribunal.eval import case as eval_cases
from tribunal.eval import labels, runner
from tribunal.eval.judge import Judge
from tribunal.grounding.suite import GroundingSuite
from tribunal.llm import registry
from tribunal.llm.base import ProviderName
from tribunal.llm.cassette import CassetteStore
from tribunal.llm.client import LLMClient
from tribunal.orchestrator import Orchestrator
from tribunal.progress import ProgressRenderer
from tribunal.sandbox import Sandbox
from tribunal.trace import reader as trace_reader
from tribunal.trace import report as report_builder

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="An adversarial multi-agent code reviewer with a deterministic decision layer.",
)
console = Console()


class ExitCode(enum.IntEnum):
    """The CLI's contract with shell scripts and CI.

    `0`-`4` describe a *run outcome*, and every command that reports a run exits with the
    one for the run it reported -- `run` for the run it just did, `replay` for the one in
    the trace. A command that reports no run exits `0` or `USAGE`.

    `USAGE` is `64`, not the `5` docs/08 first proposed. 64 is `EX_USAGE` from `sysexits.h`,
    which keeps "you invoked this wrongly" out of the range reserved for outcomes -- adding
    a sixth outcome later would otherwise collide with it, and a CI script keyed on `5`
    would silently start reading a real result as a usage error.
    `scripts/check-exit-codes.sh` verifies every row of this table.
    """

    ACCEPT = 0
    TRADEOFF = 1
    ESCALATE = 2
    FAILED = 3
    BUDGET_EXHAUSTED = 4
    #: Not a run outcome: the invocation itself was wrong. `EX_USAGE` from sysexits.h.
    USAGE = 64


@app.command()
def doctor() -> None:
    """Report tool versions and what the sandbox can actually enforce here."""
    settings = Settings.load()
    console.print(
        f"[bold]tribunal[/] {__version__}  "
        f"schema {SCHEMA_VERSION}  prices {PRICE_TABLE_VERSION}  "
        f"python {sys.version.split()[0]}"
    )

    table = Table("tool", "version", "status", title="grounding", title_justify="left")
    missing: list[str] = []
    for name in settings.grounding.static_tools:
        if name == "astgate":
            table.add_row(name, __version__, "[green]built in[/]")
            continue
        version = _module_version(name)
        if version is None:
            missing.append(name)
            table.add_row(name, "-", "[red]not installed[/]")
        else:
            table.add_row(name, version, "[green]ok[/]")
    for name in ("pytest",):
        version = _module_version(name)
        table.add_row(
            name,
            version or "-",
            "[green]ok (needs --allow-exec)[/]" if version else "[yellow]not installed[/]",
        )
    console.print(table)

    sandbox = Sandbox(settings.sandbox)
    caps = Table("property", "value", title="sandbox", title_justify="left")
    caps.add_row(
        "execution",
        "[green]enabled[/]"
        if settings.sandbox.allow_exec
        else "disabled by default (--allow-exec)",
    )
    caps.add_row("mode", settings.sandbox.mode)
    caps.add_row(
        "network blocked",
        "[green]yes[/]"
        if sandbox.network_is_blocked
        else "[yellow]NO -- use --sandbox=docker for untrusted input[/]",
    )
    caps.add_row(
        "cpu / memory / wall",
        f"{settings.sandbox.cpu_seconds}s"
        f" / {settings.sandbox.address_space_bytes >> 20}MB"
        f" / {settings.sandbox.wall_timeout_seconds}s",
    )
    docker = _docker_version()
    caps.add_row("docker CLI", docker or "[yellow]not found[/]")
    console.print(caps)

    keys = Table("provider", "credential", "status", title="llm providers", title_justify="left")
    usable = 0
    for name, provider in registry.build_all(settings.providers).items():
        reason = provider.available()
        if reason is None:
            usable += 1
        keys.add_row(
            name.value,
            provider.key_env,
            "[green]ready[/]" if reason is None else f"[yellow]{reason}[/]",
        )
    console.print(keys)
    if usable == 0:
        console.print(
            "[yellow]No LLM provider is configured.[/] Grounding, patch validation and the "
            "sandbox all work without one; the agents do not. `tribunal providers` explains "
            "what each backend can guarantee."
        )

    if missing:
        console.print(
            f"[red]Missing grounding tools: {', '.join(missing)}.[/] Critics would fall back to "
            "ungrounded reasoning, which policy halves in weight but cannot repair. "
            "Install with: pip install -e '.'"
        )
        raise typer.Exit(ExitCode.USAGE)
    console.print("[green]All configured grounding tools are available.[/]")


@app.command()
def ground(
    file: Annotated[Path, typer.Argument(help="Single Python file to analyse.")],
    test: Annotated[
        Path | None, typer.Option(help="pytest file used as the correctness oracle.")
    ] = None,
    allow_exec: Annotated[
        bool, typer.Option("--allow-exec", help="Permit execution. Required for --test.")
    ] = False,
    sandbox_mode: Annotated[
        str, typer.Option("--sandbox", help="subprocess | docker")
    ] = "subprocess",
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit the GroundingReport as JSON.")
    ] = False,
) -> None:
    """Run the grounding suite on a file and print the normalised findings."""
    if not file.is_file():
        console.print(f"[red]no such file: {file}[/]")
        raise typer.Exit(ExitCode.USAGE)
    if test is not None and not allow_exec:
        # The unsafe path requires a flag whose name says what it does.
        console.print(
            "[red]--test requires --allow-exec[/]: running a test file imports and executes it, "
            "including any module-level code in the target."
        )
        raise typer.Exit(ExitCode.USAGE)

    settings = Settings.load()
    settings = settings.model_copy(
        update={
            "sandbox": settings.sandbox.model_copy(
                update={"allow_exec": allow_exec, "mode": sandbox_mode}
            )
        }
    )
    source = file.read_text(encoding="utf-8")
    test_source = test.read_text(encoding="utf-8") if test is not None else None
    run = asyncio.run(
        GroundingSuite(settings).run(
            source,
            logical_name=file.name,
            test_source=test_source,
            test_filename=test.name if test else "test_case.py",
        )
    )
    if as_json:
        console.print_json(run.report.model_dump_json())
        return
    _print_report(run.report, file.name)


@app.command()
def providers(
    show_prices: Annotated[
        bool, typer.Option("--prices", help="Include the pinned price table.")
    ] = False,
) -> None:
    """Report what each LLM backend can guarantee, and what it costs.

    The `structure` column is the one that matters. tribunal's premise is that critiques are
    schema-validated structures, so a backend that can only be *asked* for JSON is a different
    proposition from one that constrains decoding -- and mixing them in an eval without
    recording which is which makes the parse-retry rate uninterpretable.
    """
    settings = Settings.load()
    built = registry.build_all(settings.providers)

    table = Table(
        "provider", "default model", "structure", "cache", "effort", "credential", "status",
        title="llm providers", title_justify="left",
    )
    for name, provider in built.items():
        caps = provider.capabilities(provider.default_model)
        reason = provider.available()
        cache = (
            "explicit" if caps.explicit_prompt_cache
            else "reported" if caps.reports_cache_tokens
            else "[dim]none[/]"
        )
        table.add_row(
            name.value,
            provider.default_model,
            _structure_cell(caps.structure),
            cache,
            "yes" if caps.controllable_effort else "[dim]no[/]",
            provider.key_env,
            "[green]ready[/]" if reason is None else f"[yellow]{reason}[/]",
        )
    console.print(table)

    provenance = Table(
        "provider", "wire shape verified against", title="provenance", title_justify="left"
    )
    for name, provider in built.items():
        provenance.add_row(name.value, provider.contract_source)
    console.print(provenance)

    cassettes = CassetteStore(settings.llm.cassette_dir).count()
    console.print(
        f"cassettes in {settings.llm.cassette_dir}: "
        + (", ".join(f"{k}={v}" for k, v in cassettes.items()) if cassettes else "none recorded")
        + f"  |  llm mode: {settings.llm.mode}"
    )

    agents = Table(
        "agent", "provider", "model", "effort", title="agent routing", title_justify="left"
    )
    for role in ("coder", "redteam", "profiler", "arbiter", "postmortem"):
        agent = getattr(settings.agents, role)
        agents.add_row(
            role,
            agent.resolved_provider().value,
            agent.model,
            agent.effort + (f" -> {agent.effort_late_round}" if agent.effort_late_round else ""),
        )
    console.print(agents)

    if show_prices:
        prices = Table(
            "model", "provider", "in $/Mtok", "out $/Mtok", "cached in", "notes",
            title=f"price table {PRICE_TABLE_VERSION}", title_justify="left",
        )
        for model, price in sorted(PRICES.items(), key=lambda kv: (kv[1].provider.value, kv[0])):
            notes = []
            if price.billing != "token_metered":
                notes.append("not token-metered")
            if price.intro_until:
                expired = "EXPIRED " if price.intro_expired() else ""
                notes.append(f"{expired}intro until {price.intro_until}")
            prices.add_row(
                model, price.provider.value,
                f"{price.input_per_mtok:g}", f"{price.output_per_mtok:g}",
                f"{price.cached_input:g}", ", ".join(notes) or "-",
            )
        console.print(prices)


@app.command()
def critique(
    file: Annotated[Path, typer.Argument(help="Single Python file to review.")],
    test: Annotated[
        Path | None, typer.Option(help="pytest file used as the correctness oracle.")
    ] = None,
    allow_exec: Annotated[
        bool, typer.Option("--allow-exec", help="Permit execution. Required for --test.")
    ] = False,
    config: Annotated[
        Path | None, typer.Option("--config", help="TOML config, e.g. examples/nim.toml")
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Override both critics' model.")
    ] = None,
    only: Annotated[
        str | None, typer.Option("--only", help="Run one critic: redteam | profiler")
    ] = None,
    round_: Annotated[int, typer.Option("--round", help="Debate round number.")] = 1,
) -> None:
    """Ground a file, then run the Red-team and Profiler critics on it, independently.

    This is the debate's CRITIQUE state without the surrounding loop: no Coder, no policy, no
    Arbiter. The two critics get identical input and cannot see each other's output -- which is
    the property that makes their agreement informative (docs/01-architecture.md § Rule 2).
    """
    if not file.is_file():
        console.print(f"[red]no such file: {file}[/]")
        raise typer.Exit(ExitCode.USAGE)
    if test is not None and not allow_exec:
        console.print(
            "[red]--test requires --allow-exec[/]: running a test file imports and executes "
            "it, including any module-level code in the target."
        )
        raise typer.Exit(ExitCode.USAGE)

    settings = Settings.load(config)
    settings = settings.model_copy(
        update={
            "sandbox": settings.sandbox.model_copy(update={"allow_exec": allow_exec}),
        }
    )
    if model is not None:
        settings = settings.model_copy(
            update={
                "agents": settings.agents.model_copy(
                    update={
                        "redteam": settings.agents.redteam.model_copy(update={"model": model}),
                        "profiler": settings.agents.profiler.model_copy(update={"model": model}),
                    }
                )
            }
        )

    roles = ["redteam", "profiler"] if only is None else [only]
    for role in roles:
        if role not in ("redteam", "profiler"):
            console.print(f"[red]--only must be redteam or profiler, got {role!r}[/]")
            raise typer.Exit(ExitCode.USAGE)
        agent_config = getattr(settings.agents, role)
        provider = registry.build(agent_config.resolved_provider(), settings.providers)
        reason = provider.available()
        if reason is not None:
            ready = [
                name.value
                for name, p in registry.build_all(settings.providers).items()
                if p.available() is None
            ]
            console.print(
                f"[red]{role} is routed to {provider.name.value} "
                f"({agent_config.model}), which is not usable: {reason}[/]\n"
                f"Providers ready now: {ready or 'none'}. "
                "Route with --model, or --config examples/nim.toml."
            )
            raise typer.Exit(ExitCode.USAGE)

    source = file.read_text(encoding="utf-8")
    test_source = test.read_text(encoding="utf-8") if test is not None else None
    asyncio.run(
        _run_critics(settings, file, source, test_source, test, roles, round_)
    )


async def _run_critics(
    settings: Settings,
    file: Path,
    source: str,
    test_source: str | None,
    test: Path | None,
    roles: list[str],
    round_: int,
) -> None:
    suite = GroundingSuite(settings)
    console.print(f"[bold]grounding[/] {file.name}")
    baseline = await suite.run(
        source,
        logical_name=file.name,
        test_source=test_source,
        test_filename=test.name if test else "test_case.py",
    )
    console.print(
        f"  {len(baseline.report.findings)} finding(s) from "
        f"{', '.join(baseline.report.tools_run)}"
    )

    # Standalone review: the "patched" file is the input itself, so the baseline is also the
    # patched report. In a real round these differ and `NEW since baseline` becomes meaningful.
    patched = baseline.report.model_copy(update={"target": "patched", "round": round_})

    classes = {"redteam": RedTeam, "profiler": Profiler}
    agents = [classes[role](settings) for role in roles]
    bundles = [
        CritiqueBundle(
            round=round_,
            dimension=agent.dimension,
            filename=file.name,
            patched_source=source,
            patched_report=patched,
            baseline_report=baseline.report,
        )
        for agent in agents
    ]

    console.print(
        f"[bold]critique[/] {' + '.join(a.role for a in agents)}"
        + ("  [dim]<- parallel[/]" if len(agents) > 1 else "")
    )
    # The only genuine parallelism in the system. return_exceptions so one critic failing does
    # not lose the other's work -- a failed dimension is `unassessed`, not `clean`.
    results = await asyncio.gather(
        *(agent.run(bundle) for agent, bundle in zip(agents, bundles, strict=True)),
        return_exceptions=True,
    )

    unassessed = []
    for agent, result in zip(agents, results, strict=True):
        if isinstance(result, BaseException):
            unassessed.append(agent.dimension)
            console.print(
                f"[red]{agent.role} errored[/] -> dimension "
                f"{agent.dimension.value} is UNASSESSED, which is not the same as clean\n"
                f"  {type(result).__name__}: {str(result)[:400]}"
            )
            continue
        _print_critique(result)

    if unassessed:
        console.print(
            f"[yellow]unassessed dimensions: "
            f"{', '.join(d.value for d in unassessed)}[/] — policy may not ACCEPT with these "
            "outstanding (docs/04-arbitration.md rows 5/6)."
        )


def _print_critique(run: Any) -> None:
    critique = run.value
    colour = {"block": "red", "concerns": "yellow", "clean": "green"}[critique.verdict]
    console.print(
        f"\n[bold]{run.role}[/] [{colour}]{critique.verdict.upper()}[/]  "
        f"{run.provider.value}/{run.model}  {run.prompt_version}  "
        f"{run.duration_ms}ms  ${run.cost_usd:.4f}"
    )
    console.print(f"  {critique.summary}")

    if critique.issues:
        table = Table(
            "id", "sev", "conf", "score", "grounded", "title",
            title=f"{critique.dimension.value} issues", title_justify="left",
        )
        for issue in critique.issues:
            table.add_row(
                issue.id[:16],
                issue.severity.value,
                f"{issue.confidence:g}",
                f"{issue.score:g}",
                "yes" if issue.grounded else "[yellow]no[/]",
                issue.title[:60],
            )
        console.print(table)
        for issue in critique.issues:
            refs = ", ".join(f"{e.kind.value}:{e.ref}" for e in issue.evidence)
            console.print(f"  [dim]{issue.id[:16]}[/] cites {refs}")
            if issue.suggested_direction:
                console.print(f"    -> {issue.suggested_direction}")
    for note in critique.positive_notes:
        console.print(f"  [green]+[/] {note}")

    m = run.metrics
    severe = m.get("severe_findings", 0)
    coverage = (
        f"severe={m.get('severe_findings_cited')}/{severe}" if severe else "severe=n/a"
    )
    console.print(
        f"  [dim]metrics:[/] grounded={m.get('grounded')}/{m.get('issues')} "
        f"re-rated={m.get('rerated_of_comparable')} "
        f"novel={m.get('novel_issue_rate')} {coverage} pressure={m.get('pressure')} "
        f"| structure={run.response.structure.value} "
        f"parse_retries={run.outcome.repair_retries} "
        f"local_repairs={len(run.outcome.local_repairs)}"
    )
    uncited = m.get("uncited_severe_findings") or []
    if uncited:
        # The R1 (sycophancy / under-reporting) signal, surfaced rather than buried.
        console.print(
            f"  [yellow]did not cite {len(uncited)} high-severity tool finding(s): "
            f"{', '.join(uncited)}[/]"
        )


@app.command()
def propose(
    file: Annotated[Path, typer.Argument(help="Single Python file to patch.")],
    test: Annotated[
        Path | None, typer.Option(help="pytest file used as the correctness oracle.")
    ] = None,
    allow_exec: Annotated[
        bool, typer.Option("--allow-exec", help="Permit execution. Required for --test.")
    ] = False,
    config: Annotated[
        Path | None, typer.Option("--config", help="TOML config, e.g. examples/nim.toml")
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Override the Coder's model.")
    ] = None,
    issue: Annotated[
        str | None,
        typer.Option("--issue", help="Hand the Coder a concern no tool flagged: 'Lx-Ly: text'"),
    ] = None,
    attempts: Annotated[
        int | None, typer.Option("--attempts", help="VALIDATE retries (default 2).")
    ] = None,
    write: Annotated[
        Path | None, typer.Option("--write", help="Write the patched source here.")
    ] = None,
) -> None:
    """Ground a file, then ask the Coder for a minimal patch and run it through VALIDATE.

    This is PROPOSE -> VALIDATE from docs/01-architecture.md without the surrounding loop: no
    critics, no policy, no Arbiter. A patch that does not apply is bounced straight back to the
    Coder with a mechanical error, on a budget counted separately from debate rounds.
    """
    if not file.is_file():
        console.print(f"[red]no such file: {file}[/]")
        raise typer.Exit(ExitCode.USAGE)
    if test is not None and not allow_exec:
        console.print(
            "[red]--test requires --allow-exec[/]: running a test file imports and executes "
            "it, including any module-level code in the target."
        )
        raise typer.Exit(ExitCode.USAGE)

    settings = Settings.load(config)
    settings = settings.model_copy(
        update={"sandbox": settings.sandbox.model_copy(update={"allow_exec": allow_exec})}
    )
    if model is not None:
        settings = settings.model_copy(
            update={
                "agents": settings.agents.model_copy(
                    update={"coder": settings.agents.coder.model_copy(update={"model": model})}
                )
            }
        )

    coder_config = settings.agents.coder
    provider = registry.build(coder_config.resolved_provider(), settings.providers)
    reason = provider.available()
    if reason is not None:
        ready = [
            name.value
            for name, p in registry.build_all(settings.providers).items()
            if p.available() is None
        ]
        console.print(
            f"[red]the Coder is routed to {provider.name.value} ({coder_config.model}), "
            f"which is not usable: {reason}[/]\n"
            f"Providers ready now: {ready or 'none'}. "
            "Route with --model, or --config examples/nim.toml."
        )
        raise typer.Exit(ExitCode.USAGE)

    asyncio.run(_run_coder(settings, file, test, issue, attempts, write))


def _parse_issue(spec: str, filename: str) -> Any:
    """Parse `L4-L7: description` into a supplied issue."""
    from tribunal.agents.bundle import synthetic_issue
    from tribunal.contracts import Dimension

    span, _, text = spec.partition(":")
    if not text.strip():
        raise typer.BadParameter("--issue must look like 'L4-L7: what is wrong'")
    return synthetic_issue(
        dimension=Dimension.CORRECTNESS,
        title=text.strip()[:110],
        explanation=text.strip(),
        ref=f"{filename}:{span.strip()}",
        suggested_direction=None,
    )


async def _run_coder(
    settings: Settings,
    file: Path,
    test: Path | None,
    issue_spec: str | None,
    attempts: int | None,
    write: Path | None,
) -> None:
    source = file.read_text(encoding="utf-8")
    test_source = test.read_text(encoding="utf-8") if test is not None else None

    console.print(f"[bold]grounding[/] {file.name}")
    grounded = await GroundingSuite(settings).run(
        source,
        logical_name=file.name,
        test_source=test_source,
        test_filename=test.name if test else "test_case.py",
    )
    console.print(
        f"  {len(grounded.report.findings)} finding(s) from "
        f"{', '.join(grounded.report.tools_run)}"
    )

    open_issues = (_parse_issue(issue_spec, file.name),) if issue_spec else ()
    bundle = CoderBundle(
        round=1,
        filename=file.name,
        source=source,
        report=grounded.report,
        max_hunks=settings.policy.max_hunks,
        failing_test=test_source,
        open_issues=open_issues,
    )

    coder = Coder(settings)
    console.print(
        f"[bold]propose[/] {coder.provider.value}/{coder.model}  {coder.prompt_version}"
    )
    result = await coder.propose(bundle, max_attempts=attempts)

    for index, attempt in enumerate(result.attempts, start=1):
        state = "[green]applied[/]" if attempt.ok else "[red]bounced[/]"
        console.print(
            f"  attempt {index}/{result.patch_attempts} {state} "
            f"hunks={attempt.validation.hunks} "
            f"edits={attempt.run.metrics['edits']} "
            f"{attempt.run.duration_ms}ms ${attempt.run.cost_usd:.4f}"
        )
        if not attempt.ok:
            console.print(f"    [yellow]{attempt.validation.failure_reason}[/]")

    accepted = result.accepted
    if accepted is None:
        console.print(
            f"\n[red]no patch applied after {result.patch_attempts} attempt(s)[/] — "
            "VALIDATE never passed, so no critic ever sees this. "
            f"Last error: {result.failure_reason}"
        )
        raise typer.Exit(ExitCode.FAILED)

    patch = accepted.patch
    console.print(f"\n[bold]rationale[/] {patch.rationale}")
    if patch.addresses:
        console.print(f"[bold]addresses[/] {', '.join(patch.addresses)}")
    for declined in patch.deliberately_unaddressed:
        # The Coder's pushback channel. The Arbiter adjudicates these explicitly, so they are
        # printed rather than buried.
        console.print(f"[bold]declined[/] {declined.issue_id}: {declined.reason}")
    if patch.diff:
        console.print(Syntax(patch.diff, "diff", theme="ansi_dark", line_numbers=False))
    else:
        console.print("[yellow]empty diff — the Coder claims nothing needs fixing[/]")

    console.print(
        f"[dim]hunks={accepted.validation.hunks}/{settings.policy.max_hunks} "
        f"parses={accepted.validation.parse_ok} "
        f"patch_attempts={result.patch_attempts} "
        f"parse_retries={accepted.run.outcome.repair_retries} "
        f"total=${result.cost_usd:.4f}[/]"
    )

    if write is not None and accepted.patched_source is not None:
        write.write_text(accepted.patched_source, encoding="utf-8")
        console.print(f"[green]wrote[/] {write}")


EXIT_FOR_OUTCOME = {
    "accept": ExitCode.ACCEPT,
    "tradeoff": ExitCode.TRADEOFF,
    "escalate": ExitCode.ESCALATE,
    "reject": ExitCode.ESCALATE,
}


def exit_code_for(report: Any) -> ExitCode:
    """The documented five, all of them reachable.

    Mapping `Decision` alone cannot produce `FAILED` or `BUDGET_EXHAUSTED`: policy returns
    `ESCALATE` for an unusable input *and* for a budget breach, so two of the five
    documented codes were unreachable and a CI script keyed on either would never have
    fired (docs/13 § 55).

    Order matters. A run that produced nothing reviewable is `FAILED` whatever else is true
    of it, and a budget breach is reported as such even though its decision is an
    escalation -- "ran out of money" is a different thing for a pipeline to react to than
    "the critics could not agree".
    """
    if report.terminal_state == "FAILED":
        return ExitCode.FAILED
    if report.rule_fired == "budget_exhausted":
        return ExitCode.BUDGET_EXHAUSTED
    return EXIT_FOR_OUTCOME.get(report.outcome.value, ExitCode.FAILED)


@app.command()
def run(
    file: Annotated[Path, typer.Argument(help="Single Python file to review.")],
    test: Annotated[
        Path | None, typer.Option(help="pytest file used as the correctness oracle.")
    ] = None,
    allow_exec: Annotated[
        bool, typer.Option("--allow-exec", help="Permit execution. Required for --test.")
    ] = False,
    config: Annotated[
        Path | None, typer.Option("--config", help="TOML config, e.g. examples/nim.toml")
    ] = None,
    trace_dir: Annotated[
        Path | None, typer.Option("--trace-dir", help="Where to write the JSONL trace.")
    ] = None,
    error: Annotated[
        str | None,
        typer.Option(
            "--error",
            help="A traceback to seed the Coder with. A path, or `-` for stdin.",
        ),
    ] = None,
    no_arbiter: Annotated[
        bool,
        typer.Option(
            "--no-arbiter",
            help="Template the consolidation instead of calling the Arbiter. Cheaper; "
            "detector 2 cannot fire and no pushback is adjudicated.",
        ),
    ] = False,
    no_postmortem: Annotated[
        bool,
        typer.Option(
            "--no-postmortem",
            help="Skip the write-up. The structured report is unaffected; "
            "`tribunal postmortem <trace>` can add one later.",
        ),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="No live progress. The final report still prints."),
    ] = False,
) -> None:
    """Run the full debate loop: ground, propose, validate, critique, arbitrate.

    Exit codes are the shell contract:

      0  accept                     the patch was accepted
      1  tradeoff                   the critics want incompatible things; a human chooses
      2  escalate OR reject         nothing was accepted
      3  failed                     the input was not reviewable at all
      4  budget exhausted           the cost or time cap stopped the run
      64 usage error                the command was invoked wrongly

    `2` covers two outcomes because for a caller they mean the same thing: no patch
    shipped. To tell them apart, `tribunal replay` on the trace prints the `rule_fired`
    that produced it.
    """
    if not file.is_file():
        console.print(f"[red]no such file: {file}[/]")
        raise typer.Exit(ExitCode.USAGE)
    if test is not None and not allow_exec:
        console.print("[red]--test requires --allow-exec[/]: running a test file executes it.")
        raise typer.Exit(ExitCode.USAGE)

    traceback_text: str | None = None
    if error is not None:
        # `-` is stdin, so a failing run can be piped straight in:
        #   pytest 2>&1 | tribunal run thing.py --error -
        if error == "-":
            traceback_text = sys.stdin.read()
        elif Path(error).is_file():
            traceback_text = Path(error).read_text(encoding="utf-8")
        else:
            console.print(f"[red]no such traceback file: {error}[/] (use `-` for stdin)")
            raise typer.Exit(ExitCode.USAGE)
        if not traceback_text.strip():
            console.print("[red]--error was given but the traceback is empty[/]")
            raise typer.Exit(ExitCode.USAGE)

    settings = Settings.load(config)
    settings = settings.model_copy(
        update={"sandbox": settings.sandbox.model_copy(update={"allow_exec": allow_exec})}
    )
    seats = [
        ("coder", settings.agents.coder),
        ("redteam", settings.agents.redteam),
        ("profiler", settings.agents.profiler),
    ]
    if not no_arbiter:
        seats += [
            ("arbiter", settings.agents.arbiter),
            ("arbiter_affirm", settings.agents.arbiter_affirm),
        ]
    if not no_postmortem:
        seats.append(("postmortem", settings.agents.postmortem))
    unusable = [
        f"{role}: {registry.build(cfg.resolved_provider(), settings.providers).available()}"
        for role, cfg in seats
        if registry.build(cfg.resolved_provider(), settings.providers).available() is not None
    ]
    if unusable:
        console.print("[red]cannot run — an agent has no usable provider:[/]")
        for line in unusable:
            console.print(f"  {line}")
        console.print("Route with --config examples/nim.toml.")
        raise typer.Exit(ExitCode.USAGE)

    source = file.read_text(encoding="utf-8")
    test_source = test.read_text(encoding="utf-8") if test is not None else None

    # Built here rather than inside the orchestrator so `--no-arbiter` is a construction
    # choice with one place to look, and so the templated path stays exercisable. One client
    # for both, so the Arbiter's spend lands in the same budget the critics are checked
    # against.
    client = LLMClient(settings)
    arbiter = None if no_arbiter else Arbiter(settings, client)
    writer_up = None if no_postmortem else Postmortem(settings, client)

    # A renderer over the same events the trace gets, not a second code path: with `-q` it is
    # simply absent, and the run is byte-identical (docs/06 principle 4).
    renderer = None if quiet else ProgressRenderer(
        console=console, max_rounds=settings.policy.max_rounds
    )
    orchestrator = Orchestrator(
        settings,
        client,
        arbiter=arbiter,
        postmortem=writer_up,
        subscribers=[renderer] if renderer else [],
    )

    with contextlib.ExitStack() as stack:
        if renderer is not None:
            stack.enter_context(renderer)
        result = asyncio.run(
            orchestrator.run(
                source,
                filename=file.name,
                test_source=test_source,
                traceback=traceback_text,
                argv=sys.argv[1:],
                trace_dir=trace_dir,
            )
        )
    _print_run(result)
    raise typer.Exit(exit_code_for(result.report))


def _print_run(result: Any) -> None:
    report = result.report
    colour = {"accept": "green", "tradeoff": "yellow", "escalate": "red", "reject": "red"}[
        report.outcome.value
    ]

    table = Table("round", "decision", "rule_fired", "pressure",
                  title="the debate", title_justify="left")
    for event in result.trace.of_kind("policy_decision"):
        p = event.payload
        table.add_row(f"r{p['round']}", p["decision"], p["rule_fired"], f"{p['pressure']:g}")
    console.print(table)

    console.print(
        f"[bold][{colour}]{report.outcome.value.upper()}[/][/] via [bold]{report.rule_fired}[/]  "
        f"{report.rounds_used} round(s)  ${report.total_cost_usd:.4f}  "
        f"{report.wall_seconds:.1f}s  terminal={result.terminal_state.value}"
    )
    if report.unassessed_dimensions:
        console.print(
            f"[yellow]unassessed: {', '.join(d.value for d in report.unassessed_dimensions)}[/] "
            "— these dimensions were never checked, which is not the same as clean."
        )
    if report.conflict:
        c = report.conflict
        console.print(f"\n[bold]trade-off[/] ({c.axis}, detector: {c.detector})")
        console.print(f"  standing objection: {c.right_issue} — {c.right_remedy_cost}")
        console.print(f"  cost of the alternative: {c.left_remedy_cost}")
        # docs/04 § What TRADEOFF actually emits: the axis and the two costs are the record,
        # but the recommended default and what reverses it are what a reader acts on.
        if report.tradeoff_justification:
            console.print(f"\n{report.tradeoff_justification}")
        if report.recommended_default:
            console.print(f"\n[bold]recommended default:[/] {report.recommended_default}")

    if report.issues:
        fates = Table("issue", "dim", "sev", "first seen", "fate",
                      title="issues", title_justify="left")
        for fate in report.issues:
            fates.add_row(fate.issue.id, fate.issue.dimension.value, fate.issue.severity.value,
                          f"r{fate.first_seen_round}", fate.status)
        console.print(fates)

    if report.accepted_diff:
        console.print(Syntax(report.accepted_diff, "diff", theme="ansi_dark"))
    else:
        console.print(f"[yellow]no patch accepted[/]: {report.no_patch_reason}")
    if report.narrative:
        console.print(Markdown(report.narrative))
    if result.trace_path:
        console.print(f"[dim]trace: {result.trace_path}[/]")


@app.command()
def code(
    prompt: Annotated[
        list[str] | None,
        typer.Argument(
            help="What you want done. Starts an interactive session seeded with it; "
            "with --print, runs it once and exits."
        ),
    ] = None,
    print_: Annotated[
        bool,
        typer.Option("--print", "-p", help="One shot: run the prompt, print the reply, exit."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Approve every edit and command without asking. Read what this means in "
            "code/approval.py before using it outside a container.",
        ),
    ] = False,
    plan: Annotated[
        bool,
        typer.Option(
            "--plan", help="Refuse every edit and command; read, reason and propose only."
        ),
    ] = False,
    cwd: Annotated[
        Path | None,
        typer.Option("--cwd", help="Workspace root. Nothing outside it can be read or written."),
    ] = None,
    config: Annotated[
        Path | None, typer.Option("--config", help="TOML config, e.g. examples/nim.toml")
    ] = None,
    model: Annotated[str | None, typer.Option("--model", help="Override the model.")] = None,
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="Override the provider for a model not in the table."),
    ] = None,
    max_steps: Annotated[
        int | None, typer.Option("--max-steps", help="Tool calls per turn. Default 40.")
    ] = None,
    max_usd: Annotated[
        float | None, typer.Option("--max-usd", help="Spend cap for the session. Default 5.")
    ] = None,
    log: Annotated[
        Path | None,
        typer.Option("--log", help="Append every step to this file as JSON lines."),
    ] = None,
) -> None:
    """Work in this directory with a coding agent: read, edit, run, and call the tribunal.

    The agent holds one tool per step -- read, list, grep, glob, write, edit, bash, review --
    and `review` is the one no other coding agent has: it hands a file to the full
    adversarial loop rather than to a second opinion from the same model.

    Approval, by mode:

      ask (default)   reads run free; edits and commands ask, and `always` is remembered
      plan            every edit and command is refused; the agent proposes instead
      auto (--yes)    nothing is asked

    `--print` defaults to plan mode, because a non-interactive run has nobody to ask. Add
    `--yes` to let it change things.
    """
    from tribunal.code.approval import ApprovalMode, Approver
    from tribunal.code.repl import Repl
    from tribunal.code.session import CodeSession
    from tribunal.code.tools import Workspace, WorkspaceError

    if yes and plan:
        console.print("[red]--yes and --plan ask for opposite things[/]")
        raise typer.Exit(ExitCode.USAGE)

    request = " ".join(prompt).strip() if prompt else ""
    if print_ and not request:
        console.print("[red]--print needs a prompt[/]")
        raise typer.Exit(ExitCode.USAGE)

    settings = Settings.load(config)
    agent_config = settings.agents.code
    if model is not None:
        agent_config = agent_config.model_copy(update={"model": model})
    if provider is not None:
        try:
            agent_config = agent_config.model_copy(update={"provider": ProviderName(provider)})
        except ValueError:
            console.print(f"[red]unknown provider {provider!r}[/]")
            raise typer.Exit(ExitCode.USAGE) from None
    updates: dict[str, Any] = {}
    if max_steps is not None:
        updates["max_steps"] = max_steps
    if max_usd is not None:
        updates["max_usd"] = max_usd
    settings = settings.model_copy(
        update={
            "agents": settings.agents.model_copy(update={"code": agent_config}),
            "code": settings.code.model_copy(update=updates),
        }
    )

    unusable = registry.build(
        agent_config.resolved_provider(), settings.providers
    ).available()
    if unusable:
        console.print(f"[red]cannot start:[/] {unusable}")
        console.print("Route with --config examples/nim.toml, or --provider/--model.")
        raise typer.Exit(ExitCode.USAGE)

    try:
        workspace = Workspace(cwd or Path.cwd())
    except WorkspaceError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(ExitCode.USAGE) from None

    mode = ApprovalMode.AUTO if yes else ApprovalMode.PLAN if plan else ApprovalMode.ASK
    if print_ and not yes:
        # Nobody is there to answer a prompt, and silently editing a tree because the run
        # happened to be non-interactive is the behaviour this whole module is careful not
        # to have. Said out loud rather than discovered from a refusal.
        mode = ApprovalMode.PLAN
    session = CodeSession(
        settings=settings,
        workspace=workspace,
        client=LLMClient(settings),
        approver=Approver(mode=mode),
        log_path=log,
    )

    if print_:
        if mode is ApprovalMode.PLAN:
            console.print(
                "[dim]plan mode: nothing will be edited or executed. Add --yes to allow it.[/]"
            )
        answer = asyncio.run(session.ask(request))
        # `Text`, not a markup string: the reply is model-authored, and Rich would read a
        # `list[int]` in it as a style tag.
        console.print(Text(answer.message))
        if answer.stopped_by:
            console.print(f"[yellow]stopped: {answer.stopped_by}[/]", style="yellow")
            raise typer.Exit(ExitCode.FAILED)
        raise typer.Exit(ExitCode.ACCEPT)

    raise typer.Exit(Repl(session, console).run(request or None))


@app.command("eval")
def evaluate(
    split: Annotated[
        str | None, typer.Option("--split", help="dev | heldout. Default: dev.")
    ] = None,
    arms: Annotated[
        str | None,
        typer.Option("--arms", help="Comma-separated, e.g. B1,B3. Default: all four."),
    ] = None,
    smoke: Annotated[
        bool,
        typer.Option(
            "--smoke",
            help="4 cases from cassettes, zero API calls. The CI gate.",
        ),
    ] = False,
    cases_dir: Annotated[
        Path, typer.Option("--cases", help="Where the case directories live.")
    ] = Path("eval/cases"),
    results_root: Annotated[
        Path, typer.Option("--results", help="Parent for the timestamped results dir.")
    ] = Path("eval/results"),
    concurrency: Annotated[
        int, typer.Option("--concurrency", "-j", help="Parallel (case, arm) runs.")
    ] = runner.DEFAULT_CONCURRENCY,
    config: Annotated[
        Path | None, typer.Option("--config", help="TOML config, e.g. examples/nim.toml")
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Load and validate the cases, then stop."),
    ] = False,
    replay_dir: Annotated[
        Path | None,
        typer.Option(
            "--replay",
            help="Re-score a previous results directory from its traces, without "
            "re-running the debate.",
        ),
    ] = None,
    with_judge: Annotated[
        bool,
        typer.Option(
            "--judge/--no-judge",
            help="Run the LLM judge for M1's description fallback and M2. Costs money; "
            "off by default, and its kappa must be published before a held-out sweep.",
        ),
    ] = False,
) -> None:
    """Run the benchmark and write a results directory.

    The results directory is the artifact: a score in a README with no run behind it is not
    evidence. Every run writes a trace, so a later `--replay` can re-score without paying
    for the debate again.

    `--smoke` is the CI gate and makes no API calls.

    `--replay DIR` re-scores a recorded sweep from its traces. Without `--judge` that is
    free and offline; with it, only the judge runs, which is what makes iterating on the
    judge's prompt cost one call per issue instead of a whole sweep.
    """
    try:
        suite = eval_cases.load_suite(cases_dir)
    except eval_cases.CaseError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(ExitCode.USAGE) from exc

    wanted_split = split or ("dev" if not smoke else None)
    selected = [c for c in suite if wanted_split is None or c.meta.split == wanted_split]
    if smoke:
        # A fixed, sorted slice so the gate is the same four cases on every commit.
        selected = selected[: runner.SMOKE_CASES]
    if not selected:
        console.print(f"[red]no cases matched split={wanted_split!r} in {cases_dir}[/]")
        raise typer.Exit(ExitCode.USAGE)

    chosen = [a.strip().upper() for a in arms.split(",")] if arms else list(arms_mod.ARM_IDS)
    unknown = [a for a in chosen if a not in arms_mod.ARMS]
    if unknown:
        console.print(f"[red]unknown arm(s) {unknown}[/]; known: {list(arms_mod.ARM_IDS)}")
        raise typer.Exit(ExitCode.USAGE)

    gaps = eval_cases.composition_gaps(suite)
    if gaps:
        console.print(
            f"[yellow]the benchmark is incomplete[/]: still needed {gaps}. "
            "Results from a partial set are not comparable with a full sweep."
        )

    settings = Settings.load(config)
    judge = None
    if with_judge:
        provider = registry.build(
            settings.agents.judge.resolved_provider(), settings.providers
        )
        if (reason := provider.available()) is not None:
            console.print(f"[red]judge: {reason}[/]")
            raise typer.Exit(ExitCode.USAGE)

    if replay_dir is not None:
        if not (replay_dir / "traces").is_dir():
            console.print(f"[red]no traces under {replay_dir / 'traces'}[/]")
            raise typer.Exit(ExitCode.USAGE)
        if with_judge:
            judge = Judge(settings)
        result = asyncio.run(
            runner.rescore(replay_dir, suite, settings, judge, concurrency)
        )
        console.print(Markdown(runner.summary(result)))
        console.print(f"[dim]re-scored from {replay_dir}, no debate re-run[/]")
        return

    if smoke:
        # Replay-only: a cassette miss raises rather than silently costing money, which is
        # what makes "zero API calls" a property of the command and not of the environment.
        settings = settings.model_copy(
            update={"llm": settings.llm.model_copy(update={"mode": "replay"})}
        )


    if dry_run:
        table = Table("case", "category", "split", "known issues",
                      title="benchmark", title_justify="left")
        for case in selected:
            table.add_row(case.id, case.meta.category, case.meta.split,
                          str(len(case.known_issues)))
        console.print(table)
        console.print(f"[dim]{len(selected)} case(s) x {len(chosen)} arm(s)[/]")
        return

    # A smoke run is a gate, not a measurement: it writes its traces to a temp directory so
    # CI does not litter `eval/results/` with directories nobody will ever read.
    stack = contextlib.ExitStack()
    if smoke:
        directory = Path(stack.enter_context(tempfile.TemporaryDirectory())) / "smoke"
    else:
        directory = runner.results_dir_for(results_root)
    console.print(
        f"running {len(selected)} case(s) x {len(chosen)} arm(s) "
        f"-> [bold]{directory}[/]  [dim]j={concurrency}[/]"
    )

    def progress(case: Any, run: Any) -> None:
        mark = "[red]error[/]" if run.error else (
            "[green]patched[/]" if run.accepted_diff else "[yellow]no patch[/]"
        )
        console.print(f"  {run.arm} {case.id}  {mark}")

    if with_judge:
        judge = Judge(settings)
    with stack:
        result = asyncio.run(
            runner.sweep(
                selected, chosen, settings,
                results_dir=directory, concurrency=concurrency, on_done=progress,
                judge=judge,
            )
        )
        console.print(Markdown(runner.summary(result)))
        if not smoke:
            console.print(f"[dim]raw: {directory / 'raw.jsonl'}[/]")

    if result.errors and all("CassetteMiss" in (r.error or "") for r in result.errors):
        # The gate cannot run, and saying so beats a stack of provider errors. It fails
        # rather than skipping: a CI gate that passes when it cannot do its job is worse
        # than one that is red.
        console.print(
            f"\n[red]this gate is unarmed[/]: no cassette matches these requests in "
            f"{settings.llm.cassette_dir}.\n"
            "Record them once against a real provider, then commit them:\n"
            "  TRIBUNAL_LLM__MODE=record NVIDIA_API_KEY=... \\\n"
            "    tribunal eval --split dev --config examples/nim.toml\n"
            "Until then `--smoke` fails on purpose."
        )
        raise typer.Exit(ExitCode.FAILED)
    if result.errors:
        raise typer.Exit(ExitCode.FAILED)


@app.command("judge-labels")
def judge_labels(
    results: Annotated[Path, typer.Argument(help="A results directory from a dev sweep.")],
    out: Annotated[
        Path, typer.Option("--out", help="Where to write the worksheet.")
    ] = Path("eval/judge-labels.jsonl"),
    sample: Annotated[int, typer.Option("--n", help="How many pairs to label.")] = 30,
    seed: Annotated[int, typer.Option("--seed", help="Sampling seed.")] = 0,
    cases_dir: Annotated[Path, typer.Option("--cases")] = Path("eval/cases"),
) -> None:
    """Emit a hand-labelling worksheet from a recorded sweep. Zero API calls.

    docs/07 § LLM-as-judge: 30 pairs from dev-split runs, hand-labelled, before the judge is
    trusted with anything. The rows are sampled seeded from real traces rather than chosen,
    because a kappa over pairs picked for being interesting is not a kappa over the judge's
    actual workload.

    Label every row, then run `tribunal judge-kappa`.
    """
    suite = {case.id: case for case in eval_cases.load_suite(cases_dir)}
    pairs = runner.judge_pairs(results, suite)
    if not pairs:
        console.print(f"[red]no unresolved issues in {results}[/] — nothing to label.")
        raise typer.Exit(ExitCode.USAGE)

    rows = labels.worksheet(pairs, sample=sample, seed=seed)
    labels.write_worksheet(rows, out)
    console.print(
        f"[green]wrote[/] {out}  [dim]{len(rows)} of {len(pairs)} pair(s)[/]\n"
        "Fill in `hand_label` on every row — a candidate key, or \"none\" — "
        "[bold]before[/] running the judge. Seeing its answer first is anchoring."
    )


@app.command("judge-kappa")
def judge_kappa(
    labels_path: Annotated[
        Path, typer.Argument(help="A filled-in worksheet from `judge-labels`.")
    ] = Path("eval/judge-labels.jsonl"),
    results: Annotated[
        Path | None,
        typer.Option("--results", help="The sweep the worksheet came from."),
    ] = None,
    out: Annotated[
        Path, typer.Option("--out", help="Where to write the published kappa.")
    ] = Path("eval/judge-kappa.json"),
    cases_dir: Annotated[Path, typer.Option("--cases")] = Path("eval/cases"),
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Run the judge over a labelled worksheet and publish Cohen's kappa.

    This calls the judge once per row. docs/07: "Publishing 'judge agreement with my hand
    labels: κ = 0.74 (n = 30)' is worth more than any headline number in the table, because
    it's the sentence that tells a reader the other numbers mean something."

    Writes `eval/judge-kappa.json`, which the held-out workflow refuses to run without.
    """
    if not labels_path.is_file():
        console.print(f"[red]no worksheet at {labels_path}[/] — run `judge-labels` first.")
        raise typer.Exit(ExitCode.USAGE)
    rows = labels.read_worksheet(labels_path)
    missing = labels.unlabelled(rows)
    if missing:
        console.print(
            f"[red]{len(missing)} of {len(rows)} row(s) have no hand_label.[/] "
            "A kappa over a partly-labelled worksheet is not a kappa."
        )
        raise typer.Exit(ExitCode.USAGE)

    settings = Settings.load(config)
    provider = registry.build(
        settings.agents.judge.resolved_provider(), settings.providers
    )
    if (reason := provider.available()) is not None:
        console.print(f"[red]judge: {reason}[/]")
        raise typer.Exit(ExitCode.USAGE)

    suite = {case.id: case for case in eval_cases.load_suite(cases_dir)}
    patched = runner.patched_sources(results, suite) if results else {}
    questions = labels.questions_for(rows, suite, patched)
    judge = Judge(settings)

    async def ask_all() -> dict[str, Any]:
        answers = await asyncio.gather(
            *(judge.run(q) for _id, q in questions), return_exceptions=True
        )
        out_: dict[str, Any] = {}
        for (issue_id, _q), answer in zip(questions, answers, strict=True):
            if isinstance(answer, BaseException):
                console.print(f"[yellow]judge failed on {issue_id}: {answer}[/]")
                continue
            out_[issue_id] = answer.value
        return out_

    verdicts = asyncio.run(ask_all())
    result = labels.kappa(rows, verdicts)
    labels.write_kappa(out, result, settings.agents.judge.model, len(rows))

    console.print(result.render())
    console.print(f"[dim]written to {out}[/]")
    if result.disagreements:
        table = Table("row", "hand label", "judge", title="disagreements",
                      title_justify="left")
        for index, hand, judged in result.disagreements:
            table.add_row(index, hand, judged)
        console.print(table)
    if not result.usable:
        # docs/07: fix the rubric before running anything on held-out.
        raise typer.Exit(ExitCode.FAILED)


@app.command()
def view(
    trace_path: Annotated[Path, typer.Argument(help="A trace JSONL file.")],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Where to write the HTML. Default: next to the trace."),
    ] = None,
    open_it: Annotated[
        bool, typer.Option("--open/--no-open", help="Open the file in a browser when done.")
    ] = True,
) -> None:
    """Render a trace as a single self-contained HTML file. Zero API calls.

    The output has no server, no build step and no CDN: it opens by double-click from a file
    manager with the network unplugged, which is how a reviewer will actually look at it.
    """
    if not trace_path.is_file():
        console.print(f"[red]no such trace: {trace_path}[/]")
        raise typer.Exit(ExitCode.USAGE)
    try:
        trace = trace_reader.read(trace_path)
    except trace_reader.TraceError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(ExitCode.FAILED) from exc

    for warning in trace.warnings:
        console.print(f"[yellow]warning: {warning}[/]")

    destination = output or trace_path.with_suffix(".html")
    viewer.write(trace, destination)
    size = destination.stat().st_size
    console.print(
        f"[green]wrote[/] {destination}  "
        f"[dim]{len(trace.events)} events · {size / 1024:.0f} KB · works offline[/]"
    )
    if open_it:
        # `webbrowser` is stdlib and a no-op failure on a headless box, which is the common
        # case in CI -- so a failure to open is reported, not raised.
        import webbrowser

        if not webbrowser.open(destination.resolve().as_uri()):
            console.print("[dim]could not open a browser; the file is ready above[/]")


@app.command()
def postmortem(
    trace_path: Annotated[Path, typer.Argument(help="A trace JSONL file.")],
    config: Annotated[
        Path | None, typer.Option("--config", help="TOML config, e.g. examples/nim.toml")
    ] = None,
) -> None:
    """Write (or rewrite) the narrative for a recorded run. One LLM call, no debate.

    The Postmortem reads the trace rather than the conversation, so a run's write-up can be
    regenerated months later for the price of a single call — which is how the report's
    wording gets iterated on without paying for the debate again (docs/03 § 3.5).

    Unlike `replay`, this does call a provider. `replay` never does, and that stays true.
    """
    if not trace_path.is_file():
        console.print(f"[red]no such trace: {trace_path}[/]")
        raise typer.Exit(ExitCode.USAGE)
    settings = Settings.load(config)
    provider = registry.build(
        settings.agents.postmortem.resolved_provider(), settings.providers
    )
    if (reason := provider.available()) is not None:
        console.print(f"[red]postmortem: {reason}[/]")
        console.print("Route with --config examples/nim.toml.")
        raise typer.Exit(ExitCode.USAGE)
    try:
        trace = trace_reader.read(trace_path)
    except trace_reader.TraceError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(ExitCode.FAILED) from exc

    bundle = report_builder.postmortem_bundle(trace)
    run = asyncio.run(Postmortem(settings).run(bundle, 1))
    console.print(Markdown(report_builder.render_narrative(run.value)))
    console.print(
        f"[dim]{run.prompt_version}  {run.model}  ${run.cost_usd:.4f}  "
        f"{run.duration_ms}ms[/]"
    )
    # Not appended to the trace on purpose: a trace is the record of one run, and a second
    # `run_end` or a late event would break the gap-free `seq` the reader validates. The
    # write-up is printed; re-running the command is cheap.


@app.command()
def replay(
    trace_path: Annotated[Path, typer.Argument(help="A trace JSONL file.")],
    threshold: Annotated[
        float | None,
        typer.Option("--accept-threshold", help="Re-decide under a different threshold."),
    ] = None,
    show_report: Annotated[
        bool, typer.Option("--report", help="Print the full report as JSON.")
    ] = False,
) -> None:
    """Re-derive the report from a recorded trace. Zero API calls.

    Exits with the code for the run *in the trace*, on the same contract `run` documents —
    so a recorded run can be re-checked in CI without re-running it.

    The orchestrator does not build a report of its own — it writes events and calls the same
    builder this command calls. Criterion S6 is therefore true by construction rather than by
    vigilance.

    With --accept-threshold, re-runs the decision table over the *recorded critiques* and
    reports what the run would have concluded. That is how thresholds get tuned on evidence
    instead of intuition (docs/10 § Replay).
    """
    if not trace_path.is_file():
        console.print(f"[red]no such trace: {trace_path}[/]")
        raise typer.Exit(ExitCode.USAGE)
    try:
        trace = trace_reader.read(trace_path)
    except trace_reader.TraceError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(ExitCode.FAILED) from exc

    for warning in trace.warnings:
        console.print(f"[yellow]warning: {warning}[/]")

    report = report_builder.build(trace)
    console.print(
        f"[bold]{report.run_id}[/]  {report.input_file}  "
        f"{report.outcome.value} via {report.rule_fired}  "
        f"{report.rounds_used} round(s)  ${report.total_cost_usd:.4f}"
    )
    table = Table("round", "decision", "rule_fired", "pressure",
                  title="recorded decisions", title_justify="left")
    for event in trace.of_kind("policy_decision"):
        p = event.payload
        table.add_row(f"r{p['round']}", p["decision"], p["rule_fired"], f"{p['pressure']:g}")
    console.print(table)

    if threshold is not None:
        settings = Settings.load()
        hypothetical = settings.policy.model_copy(update={"accept_threshold": threshold})
        rows = report_builder.redecide(trace, hypothetical)
        what_if = Table(
            "round", "recorded", "would be", "changed",
            title=f"re-decided at accept_threshold={threshold:g}", title_justify="left",
        )
        for row in rows:
            what_if.add_row(
                f"r{row.round}",
                f"{row.recorded.decision.value} ({row.recorded.rule_fired})",
                f"{row.hypothetical.decision.value} ({row.hypothetical.rule_fired})",
                "[yellow]yes[/]" if row.changed else "no",
            )
        console.print(what_if)

    if show_report:
        console.print_json(report.model_dump_json())

    # The exit code describes the run being reported, whichever command reports it. Without
    # this, `replay` exited 0 on an escalated run -- so the shell contract was testable only
    # by spending money, and re-checking a committed trace in CI could not fail.
    raise typer.Exit(exit_code_for(report))


def _structure_cell(structure: Any) -> str:
    colour = {
        "native_strict": "green",
        "native_schema": "cyan",
        "native_json": "yellow",
        "extracted": "red",
    }[structure.value]
    return f"[{colour}]{structure.value}[/]"


def _print_report(report: GroundingReport, filename: str) -> None:
    table = Table("line", "tool", "rule", "tool severity", "message",
                  title=f"grounding: {filename}", title_justify="left")
    for f in sorted(report.findings, key=lambda f: (f.line or 0, f.tool, f.rule)):
        table.add_row(
            str(f.line) if f.line else "-",
            f.tool,
            f.rule,
            # The tool's own rating, not ours. The critic re-rates in context; the divergence
            # between the two is the measurement that answers "isn't this just a linter?".
            f.tool_severity or "-",
            f.message[:78],
        )
    console.print(table)
    if report.tests is not None:
        t = report.tests
        state = (
            f"[green]{t.passed} passed[/]" if t.all_passed
            else f"[red]{t.failed} failed, {t.errors} errors[/]" if t.ran
            else f"[yellow]not run: {t.unavailable_reason}[/]"
        )
        console.print(f"tests: {state}")
    for measurement in report.measurements:
        console.print(f"perf: {measurement.label} -> {measurement.verdict}")
    for tool, error in report.tool_errors.items():
        console.print(f"[yellow]{tool}: {error}[/]")
    console.print(f"{len(report.findings)} finding(s) from {', '.join(report.tools_run)}")


def _module_version(module: str) -> str | None:
    try:
        from importlib.metadata import version

        return version(module)
    except Exception:  # noqa: BLE001 - absence is the answer we want, not a traceback
        return None


def _docker_version() -> str | None:
    try:
        out = subprocess.run(  # noqa: S603
            ["docker", "--version"], capture_output=True, text=True, timeout=5, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def command_names() -> set[str]:
    """Every subcommand Click will accept, as typed.

    Derived from the built Click group rather than from `app.registered_commands`, because a
    command's name can come from the decorator, from the function name, or from Typer's
    underscore-to-dash rewriting, and only the built group knows which applied. `test_cli`
    asserts this against the real group so a renamed command cannot quietly become a prompt.
    """
    import typer.main

    return set(typer.main.get_command(app).commands)


def main() -> None:
    """The `tribunal` entry point: a subcommand, or a bare prompt for the coding agent.

        tribunal doctor                 # a subcommand, unchanged
        tribunal "why does this fail?"  # rewritten to `tribunal code "..."`
        tribunal -p "add a test"        # rewritten too; -p belongs to `code`

    A shim rather than a Typer feature, because Click cannot have both a default command and
    a set of named ones: a group either dispatches on the first argument or it does not. The
    rewrite is therefore deliberately timid -- it fires only when the first argument is not a
    known command and not a help flag, so no existing invocation can change meaning, and
    `tribunal doc` (a typo for `doctor`) still gets Click's "no such command" rather than
    being sent to a model as a prompt.
    """
    argv = sys.argv[1:]
    first = argv[0] if argv else ""
    if argv and first not in command_names() and first not in {"--help", "-h", "--version"}:
        sys.argv = [sys.argv[0], "code", *argv]
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
