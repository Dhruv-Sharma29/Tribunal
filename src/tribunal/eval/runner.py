"""Running the benchmark, and writing the results directory that is the actual artifact.

docs/07-evaluation.md § Runner. Three requirements shape everything here:

**Concurrency.** "the wall-clock difference between serial and parallel on 24 cases x 4 arms
is ~2 hours vs ~30 minutes." A semaphore, default 4, over `(case, arm)` pairs.

**Every run writes a trace**, so `--replay` re-scores without re-running the debate — which
makes judge-prompt iteration free after the first sweep. That is the property worth the whole
replay implementation, and it is why the arms all write traces even though only B3 needs one
for its own report.

**The results directory is the artifact.** "a score in a README with no run behind it is not
evidence." `raw.jsonl` is one line per (case, arm) with everything a re-score needs;
`summary.md` is the table; both carry a header naming the models, prompt versions, severity
weights and price-table version, "without that, two sweeps are not comparable".

## One case failing is not the sweep failing

A sweep is ~$25 and ~30 minutes. An exception on case 11 of 24 must not discard the first ten
— so an arm that raises is recorded as an errored `ArmRun` and the sweep continues. The error
lands in `raw.jsonl` and is counted separately in the summary, because a crashed run and a run
that produced a bad patch are different facts and averaging them together would hide the first.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tribunal.config import PRICE_TABLE_VERSION, Settings
from tribunal.contracts import SCHEMA_VERSION, Severity
from tribunal.eval import report_html
from tribunal.eval.arms import ARM_IDS, ARMS, ArmRun, replay_run
from tribunal.eval.case import EvalCase
from tribunal.eval.judge import Judge, blind
from tribunal.eval.scoring import (
    Scorecard,
    apply_verdicts,
    resolution_caveat,
    score,
    totals,
)
from tribunal.llm.client import LLMClient

#: docs/07 § Runner: "Cases run concurrently with a semaphore (default 4)".
DEFAULT_CONCURRENCY = 4

#: `--smoke` is the CI gate: "4 cases, cassette-replayed, zero API calls, < 60s".
SMOKE_CASES = 4


@dataclass
class SweepResult:
    cards: list[Scorecard]
    runs: list[ArmRun]
    directory: Path | None
    header: dict

    @property
    def errors(self) -> list[ArmRun]:
        return [run for run in self.runs if run.error is not None]


def header(settings: Settings, cases: Sequence[EvalCase], arms: Sequence[str]) -> dict:
    """What makes two sweeps comparable. docs/07: without it, they are not."""
    from tribunal.agents import prompts

    return {
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "schema_version": SCHEMA_VERSION,
        "price_table_version": PRICE_TABLE_VERSION,
        "arms": list(arms),
        "cases": [case.id for case in cases],
        "splits": sorted({case.meta.split for case in cases}),
        "prompt_versions": {
            role: prompts.load(role).stamp for role in prompts.available_roles()
        },
        "models": {
            role: getattr(settings.agents, role).model
            for role in ("coder", "redteam", "profiler", "arbiter", "postmortem")
        },
        "efforts": {
            role: getattr(settings.agents, role).effort
            for role in ("coder", "redteam", "profiler", "arbiter", "postmortem")
        },
        # Frozen before Phase 5 per docs/09's ordering advice: changing them invalidates
        # every published number, so a result set that does not record them is unreadable.
        "severity_weights": {s.value: s.weight for s in Severity},
        "policy": settings.policy.model_dump(mode="json"),
    }


async def run_one(
    case: EvalCase,
    arm: str,
    settings: Settings,
    client: LLMClient,
    trace_dir: Path,
) -> ArmRun:
    """One (case, arm). An exception becomes an errored run, never a lost sweep."""
    try:
        return await ARMS[arm](case, settings, client, trace_dir)
    except Exception as exc:  # noqa: BLE001 - one case must not end the sweep
        return ArmRun(
            arm=arm,
            case_id=case.id,
            accepted_diff=None,
            patched_source=None,
            error=f"{type(exc).__name__}: {exc}",
        )


async def sweep(
    cases: Sequence[EvalCase],
    arms: Sequence[str],
    settings: Settings,
    client: LLMClient | None = None,
    results_dir: Path | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    on_done: object = None,
    judge: Judge | None = None,
) -> SweepResult:
    """Every (case, arm) pair, concurrently, scored mechanically."""
    unknown = [arm for arm in arms if arm not in ARMS]
    if unknown:
        raise ValueError(f"unknown arm(s) {unknown}; known: {list(ARM_IDS)}")

    client = client or LLMClient(settings)
    traces = (results_dir / "traces") if results_dir else Path(".")
    if results_dir is not None:
        traces.mkdir(parents=True, exist_ok=True)

    limit = asyncio.Semaphore(concurrency)
    pairs = [(case, arm) for case in cases for arm in arms]

    async def one(case: EvalCase, arm: str) -> tuple[EvalCase, ArmRun]:
        async with limit:
            run = await run_one(case, arm, settings, client, traces)
        if callable(on_done):
            on_done(case, run)
        return case, run

    finished = await asyncio.gather(*(one(case, arm) for case, arm in pairs))

    triples = [
        (case, run, score(
            case, run.arm, _as_report(run), run.critiques, run.findings, test_passes=None
        ))
        for case, run in finished
        if run.error is None
    ]
    if judge is not None:
        # After the mechanical pass, never instead of it: the judge only ever sees what
        # locators could not settle.
        await judge_cards(triples, judge, concurrency)

    head = header(settings, cases, arms)
    if judge is not None:
        head["judge_model"] = settings.agents.judge.model
    result = SweepResult(
        cards=[card for _, _, card in triples],
        runs=[run for _, run in finished],
        directory=results_dir,
        header=head,
    )
    if results_dir is not None:
        write_results(result, {case.id: case for case in cases})
    return result


def _as_report(run: ArmRun):
    """Adapt an `ArmRun` to the shape `score` reads.

    A thin object rather than a real `Report`: a baseline has no `Verdict` behind it, and
    constructing one would mean inventing a `rule_fired` for a run that has no policy layer.
    """

    @dataclass
    class _View:
        accepted_diff: str | None
        outcome: object
        rounds_used: int
        total_cost_usd: float

    return _View(
        accepted_diff=run.accepted_diff,
        outcome=run.outcome,
        rounds_used=run.rounds_used,
        total_cost_usd=run.cost_usd,
    )


# -- the judge pass ---------------------------------------------------------------------


async def judge_cards(
    pairs: Sequence[tuple[EvalCase, ArmRun, Scorecard]],
    judge: Judge,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> None:
    """Ask the judge only about what mechanical scoring could not settle.

    Two things are left over after `score`: known issues no locator matched (M1's
    description fallback) and reported issues that claimed nothing (M2 — is it real, and did
    the patch introduce it?). Both are answered by asking about the *unclaimed issues*, with
    the *unmatched known issues* as the candidate keys.

    Nothing else is asked. An issue that already matched a locator is settled deterministically
    and re-asking would let the judge overturn a free, reproducible answer with a paid one;
    a known issue that matched is not offered as a candidate, so it cannot be matched twice.
    """
    limit = asyncio.Semaphore(concurrency)

    async def one(case: EvalCase, run: ArmRun, card: Scorecard) -> None:
        by_id = {i.id: i for c in run.critiques for i in c.issues}
        unclaimed = [by_id[i] for i in card.unclaimed_issues if i in by_id]
        if not unclaimed:
            # Still a judged card: M2 of zero is a real answer, and leaving `regressions`
            # as None would read as "not judged" in the summary.
            card.regressions = 0
            return
        candidates = [case.issue(key) for key in card.unmatched_known]
        questions = blind(case, unclaimed, run.patched_source, candidates)

        async def ask(question, issue_id: str):
            async with limit:
                return issue_id, await judge.run(question)

        answered = await asyncio.gather(
            *(ask(q, issue.id) for q, issue in zip(questions, unclaimed, strict=True)),
            return_exceptions=True,
        )
        verdicts = {}
        for index, result in enumerate(answered):
            if isinstance(result, BaseException):
                # One refusal must not discard the others, and must not be scored as a
                # verdict either -- an unanswered question is absent, not negative.
                continue
            issue_id, run_ = result
            verdicts[f"q{index}"] = (issue_id, run_.value)
        apply_verdicts(card, verdicts)

    await asyncio.gather(*(one(case, run, card) for case, run, card in pairs))


# -- replay -------------------------------------------------------------------------------


async def rescore(
    directory: Path,
    cases: Sequence[EvalCase],
    settings: Settings,
    judge: Judge | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> SweepResult:
    """Re-score a recorded sweep from its traces. Zero API calls unless a judge is given.

    docs/07 § Runner: "`--replay` re-scores from traces without re-running the debate, which
    means **judge-prompt iteration is free** after the first sweep. This single property is
    worth the whole replay implementation."

    The mechanical metrics are recomputed rather than read back from `raw.jsonl`, which is
    the point: a change to `scoring.py` should be visible on old sweeps without paying for
    them again, and a stored number would hide it.
    """
    from tribunal.trace import reader as trace_reader
    from tribunal.trace.writer import traces_in

    # `summary.jsonl` is written into the same directory by the orchestrator's own per-run
    # bookkeeping, and it is a different shape entirely. `traces_in` excludes it; anything
    # else unreadable is skipped below rather than failing the whole re-score, because a
    # directory people copy traces into will collect strays.
    traces = traces_in(directory / "traces")
    if not traces:
        raise ValueError(f"no traces under {directory / 'traces'} to re-score")
    by_id = {case.id: case for case in cases}

    runs: list[ArmRun] = []
    triples: list[tuple[EvalCase, ArmRun, Scorecard]] = []
    for path in traces:
        try:
            trace = trace_reader.read(path)
        except trace_reader.TraceError:
            continue
        identity = _identify(trace)
        if identity is None:
            continue
        arm, case_id = identity
        case = by_id.get(case_id)
        if case is None:
            continue
        run = replay_run(trace, case, arm)
        run.trace_path = path
        runs.append(run)
        if run.error is None:
            triples.append((case, run, score(
                case, arm, _as_report(run), run.critiques, run.findings
            )))

    if judge is not None:
        await judge_cards(triples, judge, concurrency)

    scored = [card for _, _, card in triples]
    head = header(settings, [c for c, _, _ in triples] or list(cases),
                  sorted({r.arm for r in runs}))
    head["replayed_from"] = str(directory)
    if judge is not None:
        head["judge_model"] = settings.agents.judge.model
    return SweepResult(cards=scored, runs=runs, directory=None, header=head)


def _recorded_runs(directory: Path, cases: dict[str, EvalCase]):
    """Yield `(case, ArmRun)` for every eval trace in a results directory."""
    from tribunal.trace import reader as trace_reader
    from tribunal.trace.writer import traces_in

    for path in traces_in(directory / "traces"):
        try:
            trace = trace_reader.read(path)
        except trace_reader.TraceError:
            continue
        identity = _identify(trace)
        if identity is None:
            continue
        arm, case_id = identity
        case = cases.get(case_id)
        if case is not None:
            yield case, replay_run(trace, case, arm)


def judge_pairs(directory: Path, cases: dict[str, EvalCase]):
    """Every `(case, arm, issue, candidate keys)` the judge would be asked about.

    Exactly the pairs `judge_cards` would generate — the unclaimed issues, with the
    unmatched known issues as candidates — so a kappa measured over a sample of these is a
    kappa over the judge's real workload rather than over questions invented for it.
    """
    pairs = []
    for case, run in _recorded_runs(directory, cases):
        if run.error is not None:
            continue
        card = score(case, run.arm, _as_report(run), run.critiques, run.findings)
        by_id = {i.id: i for c in run.critiques for i in c.issues}
        candidates = tuple(card.unmatched_known)
        for issue_id in card.unclaimed_issues:
            if issue_id in by_id:
                pairs.append((case, run.arm, by_id[issue_id], candidates))
    return pairs


def patched_sources(directory: Path, cases: dict[str, EvalCase]) -> dict[str, str]:
    """`{case id: the patched file}`, so a kappa run asks about the same code a sweep did."""
    out: dict[str, str] = {}
    for case, run in _recorded_runs(directory, cases):
        if run.patched_source:
            out.setdefault(case.id, run.patched_source)
    return out


def _identify(trace) -> tuple[str, str] | None:
    """Which arm and which case a recorded trace is, from the trace itself.

    Read out of `run_start.argv`, which every arm writes as `["eval:<arm>", "<case id>"]`,
    rather than parsed out of the filename. The filename cannot carry it: `B3` runs through
    the real `Orchestrator`, which names its own trace `<run_id>.jsonl` and knows nothing
    about arms — so a filename parse silently skipped every tribunal run and re-scored only the
    baselines, which is the one shape of bug a results table would never reveal.

    docs/06 principle 3: the trace is self-describing. This is what that is for.
    """
    header = trace.events[0] if trace.events else None
    argv = (header.payload.get("argv") if header else None) or []
    if len(argv) < 2 or not str(argv[0]).startswith("eval:"):
        return None
    return str(argv[0]).removeprefix("eval:"), str(argv[1])


# -- the artifact ------------------------------------------------------------------------


def write_results(
    result: SweepResult, cases: dict[str, EvalCase] | None = None
) -> Path:
    """`raw.jsonl` and `summary.md`, both headered. The directory is the evidence."""
    directory = result.directory
    assert directory is not None  # noqa: S101 - callers check
    directory.mkdir(parents=True, exist_ok=True)

    with (directory / "raw.jsonl").open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": "header", **result.header}) + "\n")
        for card in result.cards:
            handle.write(json.dumps(_card_row(card), sort_keys=True) + "\n")
        for run in result.errors:
            handle.write(
                json.dumps(
                    {"kind": "error", "case": run.case_id, "arm": run.arm,
                     "error": run.error},
                    sort_keys=True,
                )
                + "\n"
            )
    (directory / "summary.md").write_text(summary(result), encoding="utf-8")
    (directory / "report.html").write_text(
        report_html.render(result, cases), encoding="utf-8"
    )
    return directory


def _card_row(card: Scorecard) -> dict:
    return {
        "kind": "score",
        "case": card.case_id,
        "arm": card.arm,
        "known_caught": card.known_caught,
        "known_total": card.known_total,
        "matched": [{"key": m.key, "how": m.how, "issue": m.issue_id} for m in card.matches],
        "unmatched_known": card.unmatched_known,
        "unclaimed_issues": card.unclaimed_issues,
        "fix_correct": card.fix_correct,
        "patch_applied": card.patch_applied,
        "test_passes": card.test_passes,
        "expected_outcome": (
            card.expected_outcome.value if card.expected_outcome else None
        ),
        "actual_outcome": card.actual_outcome.value if card.actual_outcome else None,
        "outcome_correct": card.outcome_correct,
        "false_positives": card.false_positives,
        "reported_issues": card.reported_issues,
        "rounds_used": card.rounds_used,
        "cost_usd": round(card.cost_usd, 6),
        "re_rated": card.re_rated,
        "comparable_to_tool": card.comparable_to_tool,
    }


def _judge_line(result: SweepResult) -> str:
    """Say plainly whether a judge was involved, and whether it was validated.

    docs/07: publishing the judge's kappa "is worth more than any headline number in the
    table, because it's the sentence that tells a reader the other numbers mean something".
    The converse holds too — a table with judge-derived rows and no kappa beside them is
    quietly claiming more than it has earned, so the absence is stated rather than omitted.
    """
    judged = any(card.regressions is not None for card in result.cards)
    kappa = result.header.get("judge_kappa")
    if not judged:
        return (
            "judge: not run — M1 counts mechanical matches only, and M2 is absent. "
            "Nothing in this table depends on a judge."
        )
    if kappa is None:
        return (
            "judge: run but **NOT VALIDATED** — no kappa against hand labels. M1's "
            "non-mechanical share and all of M2 rest on an unmeasured judge; docs/07 says "
            "to validate before a held-out sweep, not after."
        )
    return f"judge: {result.header.get('judge_model', 'unknown')}, κ vs. hand labels = {kappa}"


def summary(result: SweepResult) -> str:
    """The results table, in the shape docs/07 § Results table format froze.

    Mechanical metrics first, counts not percentages, and the resolution caveat printed
    under the table rather than left to the reader to work out.
    """
    by_arm = totals(result.cards)
    arms = [arm for arm in result.header["arms"] if arm in by_arm]
    head = result.header
    n = len({card.case_id for card in result.cards})

    lines = [
        f"### Eval results (n={n}, {head['timestamp']})",
        "",
        "models: "
        + " · ".join(f"{role}={model}" for role, model in sorted(head["models"].items())),
        "prompts: "
        + " ".join(f"{role}@{stamp.split('/')[-1]}"
                   for role, stamp in sorted(head["prompt_versions"].items())),
        "severity weights: "
        + " ".join(f"{k}{v}" for k, v in head["severity_weights"].items())
        + f" · prices {head['price_table_version']} · splits "
        + ",".join(head["splits"]),
        "",
        _judge_line(result),
        "",
    ]

    rows: list[tuple[str, list[str]]] = [
        ("M4 patch applies+passes",
         [f"{by_arm[a].fix_correct}/{by_arm[a].runs}" for a in arms]),
        ("M1 known-issue recall",
         [f"{by_arm[a].known_caught}/{by_arm[a].known_total}" for a in arms]),
        ("   of which mechanical",
         [f"{by_arm[a].known_caught_mechanically}/{by_arm[a].known_total}" for a in arms]),
        ("M2 regressions introduced",
         [str(by_arm[a].regressions) if by_arm[a].regressions is not None else "—"
          for a in arms]),
        ("M5 outcome accuracy",
         [f"{by_arm[a].outcome_correct}/{by_arm[a].outcome_scored}"
          if by_arm[a].outcome_scored else "n/a" for a in arms]),
        ("M3 false positives (canary)",
         [str(by_arm[a].false_positives) for a in arms]),
        ("M8 re-rated / comparable",
         [f"{by_arm[a].re_rated}/{by_arm[a].comparable_to_tool}" for a in arms]),
        ("M6 rounds p50", [f"{by_arm[a].rounds_p50:g}" for a in arms]),
        ("M7 cost p50 / run", [f"${by_arm[a].cost_p50:.4f}" for a in arms]),
        ("M7 cost p95 / run", [f"${by_arm[a].cost_p95:.4f}" for a in arms]),
    ]

    width = max(len(label) for label, _ in rows)
    lines.append("| " + "Metric".ljust(width) + " | " + " | ".join(arms) + " |")
    lines.append("|" + "-" * (width + 2) + "|" + "|".join("-" * 6 for _ in arms) + "|")
    for label, values in rows:
        lines.append("| " + label.ljust(width) + " | " + " | ".join(values) + " |")

    lines += ["", resolution_caveat(n)]
    if result.errors:
        # Counted separately, never averaged in: a crashed run and a bad patch are
        # different facts.
        lines.append("")
        lines.append(f"**{len(result.errors)} run(s) errored and are excluded:**")
        lines += [f"- `{r.arm}` on `{r.case_id}`: {r.error}" for r in result.errors]
    lines += [
        "",
        "Cases are hand-written in well-known vulnerability classes and likely resemble "
        "training data — see docs/07 § Provenance and contamination. That is fine for "
        "comparing systems on identical inputs and not fine for claiming absolute "
        "capability.",
    ]
    return "\n".join(lines) + "\n"


def results_dir_for(root: Path, when: float | None = None) -> Path:
    stamp = datetime.fromtimestamp(when or time.time(), UTC).strftime("%Y%m%dT%H%M%SZ")
    return root / stamp
