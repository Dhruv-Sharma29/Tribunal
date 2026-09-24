"""`report.html` — the third artifact in a results directory, beside `raw.jsonl` and
`summary.md`.

docs/07-evaluation.md § Runner asks for all three. They are not redundant: `raw.jsonl` is
what a re-score reads, `summary.md` is what goes in a commit comment, and this is the one a
person actually reads.

## No JavaScript at all

The trace viewer is JS-driven because a trace is large, interactive and full of
model-written source that has to be rendered inertly. A results report is none of those: the
data is a few dozen counts, the only interaction is expanding a per-case detail, and
`<details>` does that natively. So this is rendered in Python and ships with no script tag,
which removes the entire class of problem `viewer.js` has to defend against.

Escaping still matters — case ids, model names and error strings all reach the page — so
every interpolation goes through `_esc`, and there is exactly one function that emits markup
from untrusted text.

## What it shows that `summary.md` cannot

A **per-case matrix**: one row per case, one column per arm, and in each cell how many of
that case's known issues that arm caught and whether its patch was correct. The aggregate
table answers "which arm is better"; the matrix answers "on what", which is the question
that tells you whether a two-point difference is a real capability or one lucky case. With
n=16 those are easy to confuse and the aggregate alone invites confusing them.

Each case also expands to show *how* each known issue was matched — `rule`, `line_range`,
`judge` or nothing. A recall number whose provenance is one click away is much harder to
overstate.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from tribunal import __version__
from tribunal.eval.case import EvalCase
from tribunal.eval.scoring import Scorecard, resolution_caveat, totals

if TYPE_CHECKING:  # pragma: no cover
    from tribunal.eval.runner import SweepResult

VIEWER_CSS = Path(__file__).resolve().parent.parent / "viewer" / "assets" / "viewer.css"
REPORT_CSS = Path(__file__).resolve().parent / "assets" / "report.css"


def _esc(value: object) -> str:
    """The only place markup is produced from text. Everything interpolated goes here."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render(result: SweepResult, cases: dict[str, EvalCase] | None = None) -> str:
    """The whole report as one self-contained HTML string."""
    head = result.header
    arms = [arm for arm in head.get("arms", []) if any(c.arm == arm for c in result.cards)]
    by_arm = totals(result.cards)
    n = len({card.case_id for card in result.cards})

    body = [
        f"<h1>tribunal eval — {_esc(head.get('timestamp', ''))}</h1>",
        _provenance(head),
        _judge_banner(result),
        _partial_banner(head, n),
        "<h2>Results</h2>",
        _results_table(arms, by_arm),
        f"<p class=\"meta\">{_esc(resolution_caveat(n))}</p>",
        "<h2>Per case</h2>",
        _matrix(result, arms, cases or {}),
        _errors(result),
        _caveats(),
    ]
    return _document(head, "\n".join(block for block in body if block))


def _document(head: dict, body: str) -> str:
    css = VIEWER_CSS.read_text(encoding="utf-8") + "\n" + REPORT_CSS.read_text(encoding="utf-8")
    title = f"tribunal eval · {head.get('timestamp', '')}"
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_esc(title)}</title>\n"
        f"<style>\n{css}\n</style>\n"
        "</head>\n<body>\n<main class=\"wrap\">\n"
        f"{body}\n"
        "</main>\n</body>\n</html>\n"
    )


# -- the header that makes two sweeps comparable ----------------------------------------


def _provenance(head: dict) -> str:
    """docs/07: "Header every result set with model ids, prompt versions, config,
    `Severity.weight` values, and the price table version. Without that, two sweeps are not
    comparable." Rendered first, because a number read without it is not evidence."""
    models = " · ".join(
        f"{_esc(role)}={_esc(model)}" for role, model in sorted(head.get("models", {}).items())
    )
    prompts = " ".join(
        f"{_esc(role)}@{_esc(stamp.split('/')[-1])}"
        for role, stamp in sorted(head.get("prompt_versions", {}).items())
    )
    weights = " ".join(
        f"{_esc(k)}{_esc(v)}" for k, v in head.get("severity_weights", {}).items()
    )
    rows = [
        f"<b>models</b> {models}",
        f"<b>prompts</b> {prompts}",
        f"<b>severity weights</b> {weights}",
        f"<b>prices</b> {_esc(head.get('price_table_version', '?'))}"
        f" · <b>schema</b> {_esc(head.get('schema_version', '?'))}"
        f" · <b>tribunal</b> {_esc(__version__)}",
        f"<b>splits</b> {_esc(', '.join(head.get('splits', [])))}"
        f" · <b>cases</b> {len(head.get('cases', []))}"
        f" · <b>arms</b> {_esc(', '.join(head.get('arms', [])))}",
    ]
    if head.get("replayed_from"):
        rows.append(f"<b>re-scored from</b> {_esc(head['replayed_from'])}")
    return '<div class="meta">' + "<br>".join(rows) + "</div>"


def _judge_banner(result: SweepResult) -> str:
    """Three states, stated plainly. An unvalidated judge undermines M1's non-mechanical
    share and all of M2, and a reader cannot tell from the numbers."""
    judged = any(card.regressions is not None for card in result.cards)
    kappa = result.header.get("judge_kappa")
    if not judged:
        return (
            '<div class="banner good"><b>No judge was involved.</b>'
            "M1 counts mechanical locator matches only, and M2 is absent. Every number "
            "below is reproducible from the traces with no model in the loop.</div>"
        )
    if kappa is None:
        return (
            '<div class="banner bad"><b>The judge has not been validated.</b>'
            "No Cohen&#39;s &kappa; against hand labels has been published for it, so M1&#39;s "
            "non-mechanical share and all of M2 rest on an unmeasured instrument. "
            "docs/07 asks for this before a held-out sweep, not after.</div>"
        )
    return (
        f'<div class="banner good"><b>Judge: '
        f"{_esc(result.header.get('judge_model', 'unknown'))}.</b>"
        f"&kappa; against hand labels = {_esc(kappa)}.</div>"
    )


def _partial_banner(head: dict, n: int) -> str:
    """A sweep over a subset is a real number about a different benchmark."""
    from tribunal.eval.case import TARGET_COMPOSITION

    target = sum(TARGET_COMPOSITION.values())
    if n >= target:
        return ""
    return (
        f'<div class="banner"><b>Partial benchmark: {n} of {target} cases.</b>'
        "These numbers describe the cases that ran, not the benchmark. They are not "
        "comparable with a full sweep.</div>"
    )


# -- the aggregate table ----------------------------------------------------------------


def _results_table(arms: list[str], by_arm: dict) -> str:
    def cells(render_one) -> str:
        return "".join(f'<td class="num">{render_one(by_arm[a])}</td>' for a in arms)

    def fraction(top: int, bottom: int) -> str:
        if not bottom:
            return '<span class="na">&mdash;</span>'
        return f'<span class="count">{top}/{bottom}</span>'

    rows = [
        # M4 leads, because it is the one number with no interpretation in it.
        ('<td class="metric lead">M4 patch applies, parses, passes</td>',
         lambda t: fraction(t.fix_correct, t.runs), "lead"),
        ('<td class="metric">M1 known-issue recall</td>',
         lambda t: fraction(t.known_caught, t.known_total), ""),
        ('<td class="sub">of which mechanical</td>',
         lambda t: fraction(t.known_caught_mechanically, t.known_total), ""),
        ('<td class="metric">M2 regressions introduced</td>',
         lambda t: (f'<span class="count">{t.regressions}</span>'
                    if t.regressions is not None else '<span class="na">not judged</span>'), ""),
        ('<td class="metric">M3 false positives (canary)</td>',
         lambda t: f'<span class="count">{t.false_positives}</span>', ""),
        ('<td class="metric">M5 outcome accuracy</td>',
         lambda t: (fraction(t.outcome_correct, t.outcome_scored)
                    if t.outcome_scored else '<span class="na">n/a</span>'), ""),
        ('<td class="metric">M6 rounds, p50</td>',
         lambda t: f'<span class="count">{t.rounds_p50:g}</span>', ""),
        ('<td class="metric">M7 cost p50 / run</td>',
         lambda t: f'<span class="count">${t.cost_p50:.4f}</span>', ""),
        ('<td class="metric">M7 cost p95 / run</td>',
         lambda t: f'<span class="count">${t.cost_p95:.4f}</span>', ""),
        ('<td class="metric">M8 re-rated / comparable</td>',
         lambda t: fraction(t.re_rated, t.comparable_to_tool), ""),
    ]
    header = "".join(f'<th class="arm num">{_esc(a)}</th>' for a in arms)
    lines = [
        '<div class="scroll"><table class="results">',
        f"<thead><tr><th>Metric</th>{header}</tr></thead><tbody>",
    ]
    for label, render_one, cls in rows:
        lines.append(f'<tr class="{cls}">{label}{cells(render_one)}</tr>')
    lines.append("</tbody></table></div>")
    return "\n".join(lines)


# -- the per-case matrix ------------------------------------------------------------------


def _matrix(result: SweepResult, arms: list[str], cases: dict[str, EvalCase]) -> str:
    """One row per case, one column per arm. The question the aggregate cannot answer."""
    by_key: dict[tuple[str, str], Scorecard] = {
        (card.case_id, card.arm): card for card in result.cards
    }
    case_ids = sorted({card.case_id for card in result.cards})
    header = "".join(f'<th class="arm num">{_esc(a)}</th>' for a in arms)
    lines = [
        '<div class="scroll"><table class="matrix">',
        f"<thead><tr><th>Case</th><th>Category</th>{header}</tr></thead><tbody>",
    ]
    for case_id in case_ids:
        case = cases.get(case_id)
        category = case.meta.category if case else ""
        cells = "".join(_cell(by_key.get((case_id, arm))) for arm in arms)
        lines.append(
            f'<tr><td class="case">{_esc(case_id)}</td>'
            f'<td class="cat">{_esc(category)}</td>{cells}</tr>'
        )
    lines.append("</tbody></table></div>")

    details = [_case_detail(case_ids, by_key, arms, cases)]
    return "\n".join(lines + details)


def _cell(card: Scorecard | None) -> str:
    if card is None:
        return '<td class="cell"><span class="pill na">—</span></td>'
    if card.known_total == 0:
        # A canary-clean case has nothing to catch; what matters is what it invented.
        shade = "all" if card.false_positives == 0 else "none"
        label = "clean" if card.false_positives == 0 else f"+{card.false_positives}"
    elif card.known_caught == card.known_total:
        shade, label = "all", f"{card.known_caught}/{card.known_total}"
    elif card.known_caught == 0:
        shade, label = "none", f"{card.known_caught}/{card.known_total}"
    else:
        shade, label = "some", f"{card.known_caught}/{card.known_total}"
    fix = ('<span class="fix yes">&#10003;</span>' if card.fix_correct
           else '<span class="fix no">&#10007;</span>')
    return f'<td class="cell"><span class="pill {shade}">{label}</span>{fix}</td>'


def _case_detail(
    case_ids: list[str],
    by_key: dict[tuple[str, str], Scorecard],
    arms: list[str],
    cases: dict[str, EvalCase],
) -> str:
    """How each known issue was matched, per arm. A recall number whose provenance is one
    click away is much harder to overstate."""
    out = ["<h2>How each issue was matched</h2>"]
    for case_id in case_ids:
        case = cases.get(case_id)
        rows = []
        for arm in arms:
            card = by_key.get((case_id, arm))
            if card is None:
                continue
            if not card.matches:
                rows.append(f"<b>{_esc(arm)}</b> nothing declared to catch")
                continue
            marks = " ".join(
                f'{_esc(m.key)}:<span class="how {m.how}">{_esc(m.how)}</span>'
                for m in card.matches
            )
            rows.append(f"<b>{_esc(arm)}</b> {marks}")
        locators = ""
        if case is not None:
            locators = " · ".join(
                f"{_esc(k.key)}={_esc(k.locator.kind)}" for k in case.known_issues
            )
        out.append(
            f'<details class="case"><summary>{_esc(case_id)}</summary>'
            f'<div class="body"><div class="meta">locators: {locators or "none"}</div>'
            + "<br>".join(rows)
            + "</div></details>"
        )
    return "\n".join(out)


def _errors(result: SweepResult) -> str:
    """Counted separately, never averaged in: a crashed run and a bad patch are different
    facts, and a report that blends them hides the first."""
    if not result.errors:
        return ""
    items = "".join(
        f"<li><code>{_esc(run.arm)}</code> on <code>{_esc(run.case_id)}</code>: "
        f"{_esc(run.error)}</li>"
        for run in result.errors
    )
    return (
        f'<div class="banner bad"><b>{len(result.errors)} run(s) errored and are excluded '
        f"from every number above.</b><ul>{items}</ul></div>"
    )


def _caveats() -> str:
    return (
        '<div class="caveats"><h2>Caveats</h2>'
        "<p><b>Contamination.</b> The cases are hand-written in well-known vulnerability "
        "classes, and the models have very likely seen similar code. That is fine for "
        "comparing systems on identical inputs, which is what this measures, and not fine "
        "for claiming absolute capability.</p>"
        "<p><b>M1 is two metrics wearing one name.</b> Of the declared defects, almost every "
        "one with a mechanical <code>rule</code> locator is a security issue: "
        "<code>bandit</code> is a security linter and <code>ruff</code>&#39;s performance "
        "rules are narrow. Performance recall therefore rests on line ranges and, where "
        "those fail, on the judge. The two dimensions are not equally well evidenced.</p>"
        "<p><b>Counts, not rates.</b> Every figure above is a count over a small n. A "
        "one-case difference is several points and is not a result.</p></div>"
    )
