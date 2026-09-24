"""Cross-checking a critique against the grounding report it claims to cite.

`Issue.evidence` having `min_length=1` makes "cite something" a validation error. It does not
make the citation *true*: a model can emit `ref: "bandit-B602"` when the finding id is
`e5376668fa`, and the schema is perfectly happy. That gap is where a grounded critic quietly
becomes an ungrounded one.

This module closes it. Every ref is resolved against the real `GroundingReport`, and a critique
that cites something that does not exist is **rejected and repaired**, not accepted with a
footnote. The failure messages are written for the model to act on, because they are fed back
verbatim as the repair prompt.

Two rules here come directly from observed behaviour rather than from the spec:

* **`tools_consulted` must be tool names.** The first live Nemotron run filled it with *finding
  ids* -- schema-valid and meaningless.
* **Measurement refs must resolve to a citable measurement.** docs/03-agents.md § 3.3 says an
  `inconclusive` measurement "MUST NOT be cited ... the prompt says so and the schema validator
  enforces it". The schema alone cannot: it needs the report.
"""

from __future__ import annotations

import re

from tribunal.agents.bundle import (
    DIMENSION_TOOLS,
    AffirmationBundle,
    ArbiterBundle,
    CoderBundle,
    CritiqueBundle,
    PostmortemBundle,
)
from tribunal.contracts import (
    ArbiterNote,
    ConflictAffirmation,
    Critique,
    EvidenceKind,
    Issue,
    PatchProposal,
    PostmortemNote,
    Severity,
)


class EvidenceError(ValueError):
    """A critique's evidence does not resolve against the grounding report.

    A `ValueError` so `LLMClient.call`'s `post_validate` hook turns it into one repair retry,
    counted alongside schema failures.
    """


def validate_critique(critique: Critique, bundle: CritiqueBundle) -> None:
    """Raise `EvidenceError` describing every problem, or return None.

    All problems are collected before raising. Reporting them one at a time would spend a
    repair retry per defect, and the repair budget is one.
    """
    problems: list[str] = []
    problems += _check_dimension(critique, bundle)
    problems += _check_round(critique, bundle)
    problems += _check_tools_consulted(critique, bundle)
    problems += _check_dismissed_not_reraised(critique, bundle)
    problems += _check_verdict_matches_severity(critique)
    for index, issue in enumerate(critique.issues):
        problems += _check_issue(issue, index, critique, bundle)
    if problems:
        raise EvidenceError("\n".join(problems))


# -- critique-level -------------------------------------------------------------------------


def _check_dimension(critique: Critique, bundle: CritiqueBundle) -> list[str]:
    if critique.dimension is not bundle.dimension:
        return [
            f"dimension must be \"{bundle.dimension.value}\" for this critic, got "
            f"\"{critique.dimension.value}\". You assess one dimension only."
        ]
    return []


def _check_round(critique: Critique, bundle: CritiqueBundle) -> list[str]:
    if critique.round != bundle.round:
        return [f"round must be {bundle.round}, got {critique.round}."]
    return []


def _check_tools_consulted(critique: Critique, bundle: CritiqueBundle) -> list[str]:
    """`tools_consulted` must be tool names, and tools that actually produced something."""
    allowed = DIMENSION_TOOLS[bundle.dimension]
    # Tools that *ran*, not tools that produced findings. A critic on a clean file has
    # nothing to cite and must still be able to say which tools it consulted -- requiring a
    # finding here made `tools_consulted` unsatisfiable on exactly the canary case that
    # detects critique inflation.
    ran = set(bundle.patched_report.tools_succeeded) & allowed
    finding_ids = {f.id for f in bundle.patched_report.findings}

    problems = []
    for name in critique.tools_consulted:
        if name in finding_ids:
            problems.append(
                f"tools_consulted contains {name!r}, which is a finding id, not a tool name. "
                f"Use tool names from: {sorted(allowed & ran) or sorted(allowed)}."
            )
        elif name not in allowed:
            problems.append(
                f"tools_consulted contains {name!r}, which is not a grounding tool for the "
                f"{bundle.dimension.value} dimension. Allowed: {sorted(allowed)}."
            )
        elif name not in ran and ran:
            problems.append(
                f"tools_consulted claims {name!r} but that tool produced no output this "
                f"round. Tools that ran: {sorted(ran)}."
            )
    return problems


def _check_verdict_matches_severity(critique: Critique) -> list[str]:
    """A `high` issue with a non-blocking verdict is mis-calibrated.

    docs/02-contracts.md states one direction of this guard in the schema -- `"block"` requires
    a `high` -- on the grounds that "a critic that blocks on low-severity findings is
    mis-calibrated and the parse fails loudly". The converse is equally mis-calibrated, and the
    first live Profiler run did exactly it: two `high` issues under `verdict: "concerns"`.

    Enforced here rather than in `contracts.py` because the docs only specify one direction,
    and unilaterally tightening a documented schema is a bigger change than adding a critic
    calibration check. Recorded as a proposed contract tightening in
    docs/13-implementation-notes.md.
    """
    highs = [issue.id for issue in critique.issues if issue.severity is Severity.HIGH]
    if highs and critique.verdict != "block":
        return [
            f"verdict is {critique.verdict!r} but issues {highs} are severity \"high\". "
            "Either the verdict is \"block\", or those issues are not actually high — decide "
            "which and make them agree."
        ]
    return []


def _check_dismissed_not_reraised(critique: Critique, bundle: CritiqueBundle) -> list[str]:
    """The Arbiter's dismissals are permanent; re-raising one burns a round."""
    dismissed = set(bundle.dismissed_issue_ids)
    repeats = [issue.id for issue in critique.issues if issue.id in dismissed]
    if repeats:
        return [
            f"issues {repeats} were already dismissed as false positives in an earlier round "
            "and must not be raised again."
        ]
    return []


# -- issue-level ----------------------------------------------------------------------------


def _check_issue(
    issue: Issue, index: int, critique: Critique, bundle: CritiqueBundle
) -> list[str]:
    where = f"issues[{index}] ({issue.id})"
    problems = []
    if issue.dimension is not bundle.dimension:
        problems.append(
            f"{where}: dimension must be \"{bundle.dimension.value}\", got "
            f"\"{issue.dimension.value}\"."
        )
    problems += _check_suggested_direction(issue, where)
    for slot, evidence in enumerate(issue.evidence):
        problems += _check_evidence(evidence, f"{where}.evidence[{slot}]", bundle)
    return problems


def _check_suggested_direction(issue: Issue, where: str) -> list[str]:
    """`suggested_direction` forbids diffs. Letting critics write patches collapses the roles
    and produces three competing diffs with no owner (docs/02-contracts.md)."""
    text = issue.suggested_direction
    if not text:
        return []
    markers = [m for m in ("@@", "\n+++ ", "\n--- ", "```") if m in text]
    if markers:
        return [
            f"{where}: suggested_direction contains a diff or code block ({markers[0]!r}). "
            "Describe what to change in one sentence; the Coder owns diffs."
        ]
    if text.count("\n") >= 3:
        return [
            f"{where}: suggested_direction spans {text.count(chr(10)) + 1} lines. It must be "
            "one sentence describing what to change, not an implementation."
        ]
    return []


def _check_evidence(evidence, where: str, bundle: CritiqueBundle) -> list[str]:
    kind = evidence.kind
    ref = evidence.ref
    report = bundle.patched_report

    if kind is EvidenceKind.REASONING:
        return []  # legal, and halved in weight by policy

    if kind is EvidenceKind.TOOL_FINDING:
        finding = report.finding(ref)
        if finding is None:
            known = [f.id for f in bundle.relevant_findings()]
            return [
                f"{where}: ref {ref!r} is not a grounding finding id. Use an id from the "
                f"findings list: {known or '(none were reported)'}. The rule code and the "
                "tool name are not ids."
            ]
        allowed = DIMENSION_TOOLS[bundle.dimension]
        if finding.tool not in allowed:
            return [
                f"{where}: finding {ref} comes from {finding.tool!r}, which is not a "
                f"{bundle.dimension.value} tool. Allowed: {sorted(allowed)}."
            ]
        return []

    if kind is EvidenceKind.MEASUREMENT:
        labels = {m.label: m for m in report.measurements}
        measurement = labels.get(ref)
        if measurement is None:
            citable = [m.label for m in bundle.citable_measurements()]
            # Built outside the f-string: a line break inside a replacement field is a
            # syntax error before Python 3.12, and this project targets 3.11+.
            options = ", ".join(citable) if citable else (
                "(none — do not cite a measurement, and do not state a timing or percentage)"
            )
            return [
                f"{where}: ref {ref!r} is not a measurement label. "
                f"Citable measurements: {options}."
            ]
        if not measurement.is_citable:
            return [
                f"{where}: measurement {ref!r} has verdict {measurement.verdict!r} and MUST "
                "NOT be cited as evidence. An inconclusive or unmeasurable result is neither "
                "evidence of a change nor evidence of no change. Use a code_span with an "
                "asymptotic argument, or drop the issue."
            ]
        return []

    if kind is EvidenceKind.TEST_FAILURE:
        tests = report.tests
        if tests is None or not tests.ran:
            return [
                f"{where}: ref {ref!r} cites a test failure, but no test result is available "
                "this round."
            ]
        if ref not in tests.failed_node_ids:
            return [
                f"{where}: ref {ref!r} is not a failing test. Failing node ids: "
                f"{tests.failed_node_ids or '(none — every test passed)'}."
            ]
        return []

    if kind is EvidenceKind.CODE_SPAN:
        return _check_code_span(ref, where, bundle)

    return []  # pragma: no cover - EvidenceKind is exhaustive above


def _check_code_span(ref: str, where: str, bundle: CritiqueBundle) -> list[str]:
    total = len(bundle.patched_source.splitlines())
    parsed = _span_of(ref)
    if parsed is None:
        return [
            f"{where}: ref {ref!r} is not a line span. Use "
            f"\"{bundle.filename}:L<start>-L<end>\"."
        ]
    filename, (start, end) = parsed
    problems = []
    if filename and filename != bundle.filename:
        problems.append(
            f"{where}: ref names file {filename!r}, but the file under review is "
            f"{bundle.filename!r}. This review is single-file."
        )
    if start < 1 or end > total:
        problems.append(
            f"{where}: span L{start}-L{end} is outside {bundle.filename}, which has {total} "
            "lines."
        )
    return problems


def _span_of(ref: str) -> tuple[str, tuple[int, int]] | None:
    from tribunal.contracts import Evidence

    probe = Evidence(kind=EvidenceKind.CODE_SPAN, ref=ref, excerpt="")
    span = probe.span
    if span is None:
        return None
    filename = ref.rsplit(":", 1)[0] if ":" in ref else ""
    return filename, span


# -- the Arbiter ----------------------------------------------------------------------------


def validate_arbiter_note(note: ArbiterNote, bundle: ArbiterBundle) -> None:
    """Raise `EvidenceError` describing every problem, or return None.

    The schema already pins the shape: `consolidated_critique` iff REJECT, the two trade-off
    fields iff TRADEOFF. What it cannot see is the *verdict* the note is supposed to be about,
    and that is where the two failures that matter live -- prose that drifted from the
    decision, and a dismissal of something nobody contested.
    """
    problems: list[str] = []
    problems += _check_echo(note, bundle)
    problems += _check_note_round(note, bundle)
    problems += _check_priority_order(note, bundle)
    problems += _check_dismissals(note, bundle)
    problems += _check_no_diff_in_prose(note)
    if problems:
        raise EvidenceError("\n".join(problems))


def _check_echo(note: ArbiterNote, bundle: ArbiterBundle) -> list[str]:
    """The one check docs/03-agents.md names by hand: "catches it if the prose drifts"."""
    actual = bundle.verdict.decision
    if note.decision_echo is actual:
        return []
    return [
        f"decision_echo is \"{note.decision_echo.value}\" but the decision was "
        f"\"{actual.value}\". You are writing up a decision that has already been made by the "
        "policy layer; you cannot change it. Echo it exactly, and write the prose that "
        f"\"{actual.value}\" requires."
    ]


def _check_note_round(note: ArbiterNote, bundle: ArbiterBundle) -> list[str]:
    if note.round != bundle.round:
        return [f"round must be {bundle.round}, got {note.round}."]
    return []


def _check_priority_order(note: ArbiterNote, bundle: ArbiterBundle) -> list[str]:
    """Priority is an ordering of the open issues, not a place to introduce new ones."""
    open_ids = [issue.id for issue in bundle.open_issues()]
    problems = []
    unknown = [i for i in note.priority_order if i not in open_ids]
    if unknown:
        problems.append(
            f"priority_order contains {unknown}, which are not open issues this round. "
            f"Allowed ids: {open_ids or '(none — leave priority_order empty)'}."
        )
    duplicates = sorted({i for i in note.priority_order if note.priority_order.count(i) > 1})
    if duplicates:
        problems.append(
            f"priority_order lists {duplicates} more than once. An order is a sequence of "
            "distinct ids."
        )
    return problems


def _check_dismissals(note: ArbiterNote, bundle: ArbiterBundle) -> list[str]:
    """A dismissal is permanent and removes pressure, so its scope is the tightest in the
    system: only what the Coder actually contested, and never something also prioritised."""
    adjudicable = bundle.adjudicable_ids()
    dismissed_ids = [entry.issue_id for entry in note.dismissed]
    problems = []

    ungrounded = [i for i in dismissed_ids if i not in adjudicable]
    if ungrounded:
        contested = sorted(adjudicable)
        problems.append(
            f"dismissed contains {ungrounded}, which the Coder did not push back on. You may "
            "only dismiss an issue whose finding the Coder contested — dismissing an "
            "uncontested issue lowers the pressure the policy layer decided on, which is not "
            f"yours to change. Contested this round: {contested or '(nothing)'}."
        )
    both = sorted(set(dismissed_ids) & set(note.priority_order))
    if both:
        problems.append(
            f"{both} appear in both dismissed and priority_order. Dismissing an issue means "
            "the Coder must not act on it; prioritising it means the opposite. Choose one."
        )
    repeats = sorted({i for i in dismissed_ids if dismissed_ids.count(i) > 1})
    if repeats:
        problems.append(f"dismissed names {repeats} more than once.")
    return problems


def _check_no_diff_in_prose(note: ArbiterNote) -> list[str]:
    """The Coder owns diffs. An Arbiter that writes one produces a second patch with no
    owner, which is the same collapse `_check_suggested_direction` prevents for critics."""
    problems = []
    for field, text in (
        ("consolidated_critique", note.consolidated_critique),
        ("tradeoff_justification", note.tradeoff_justification),
        ("recommended_default", note.recommended_default),
    ):
        if not text:
            continue
        marker = next((m for m in ("@@ -", "\n+++ ", "\n--- ", "```") if m in text), None)
        if marker is not None:
            problems.append(
                f"{field} contains a diff or code block ({marker!r}). Describe the change in "
                "words; the Coder writes the patch."
            )
    return problems


def validate_affirmation(
    affirmation: ConflictAffirmation, bundle: AffirmationBundle
) -> None:
    """The affirmation must be about the pair that was asked about.

    A model answering a *different* pair is not a hypothetical: the two ids are opaque hex
    and the two issues are adjacent in the prompt, which is exactly the setup in which one
    gets substituted for the other. Since this answer feeds `policy.decide`, a swapped pair
    would produce a `Conflict` naming issues nobody classified.
    """
    expected = (bundle.left.id, bundle.right.id)
    got = (affirmation.left_issue, affirmation.right_issue)
    if got == expected:
        return
    swapped = " (you swapped them)" if got == (expected[1], expected[0]) else ""
    raise EvidenceError(
        f"this question is about left_issue=\"{expected[0]}\" and "
        f"right_issue=\"{expected[1]}\", but you answered about {got}{swapped}. Copy the two "
        "ids exactly as given, in that order."
    )


# -- the Postmortem -------------------------------------------------------------------------


def validate_postmortem(note: PostmortemNote, bundle: PostmortemBundle) -> None:
    """Raise `EvidenceError` describing every problem, or return None.

    Two of these three checks are about the same failure from different angles: a write-up
    that reads as more confident than the run was. An accepted patch with an open issue is a
    conditional pass, and a narrative that omits the condition is worse than no narrative --
    it teaches the reader to stop looking.
    """
    problems: list[str] = []
    problems += _check_outcome_echo(note, bundle)
    problems += _check_rounds_match(note, bundle)
    problems += _check_uncertainty_is_accounted_for(note, bundle)
    problems += _check_no_headings(note)
    if problems:
        raise EvidenceError("\n".join(problems))


def _check_outcome_echo(note: PostmortemNote, bundle: PostmortemBundle) -> list[str]:
    if note.outcome_echo is bundle.outcome:
        return []
    return [
        f"outcome_echo is \"{note.outcome_echo.value}\" but the run ended in "
        f"\"{bundle.outcome.value}\". You are recounting a decision, not revisiting it."
    ]


def _check_rounds_match(note: PostmortemNote, bundle: PostmortemBundle) -> list[str]:
    """One entry per round, same numbers. A missing round is a gap in the story; an extra
    one is invented."""
    actual = [digest.round for digest in bundle.rounds]
    described = [summary.round for summary in note.rounds]
    if described == actual:
        return []
    return [
        f"rounds must describe exactly the rounds in the input, {actual}, in order — got "
        f"{described}."
    ]


def _check_uncertainty_is_accounted_for(
    note: PostmortemNote, bundle: PostmortemBundle
) -> list[str]:
    """Every open issue and unassessed dimension must appear somewhere in the write-up.

    docs/03-agents.md § 3.5 makes "what I would not trust" the section the report always ends
    on. The schema can force it to be non-empty; only the trace knows whether it is
    *complete*, which is the property that matters.
    """
    haystack = " ".join(
        [
            note.headline,
            note.disagreement or "",
            *note.what_i_would_not_trust,
            *note.human_should_check,
            *(summary.what_changed for summary in note.rounds),
            *(summary.outcome for summary in note.rounds),
        ]
    )
    missing = [fact for fact in bundle.must_mention() if fact not in haystack]
    if not missing:
        return []
    return [
        f"the write-up never mentions {missing}. Every issue still open at the end, and every "
        "dimension nobody assessed, has to appear in what_i_would_not_trust — an accepted "
        "patch with an open issue is a conditional pass, and a write-up that omits the "
        "condition misleads the reader into not looking."
    ]


def _check_no_headings(note: PostmortemNote) -> list[str]:
    """The layout is ours. A field opening with `##` renders a heading inside a heading."""
    offenders = [
        name
        for name, text in (
            ("headline", note.headline),
            ("disagreement", note.disagreement),
            *((f"what_i_would_not_trust[{i}]", t)
              for i, t in enumerate(note.what_i_would_not_trust)),
            *((f"human_should_check[{i}]", t)
              for i, t in enumerate(note.human_should_check)),
        )
        if text and text.lstrip().startswith("#")
    ]
    if not offenders:
        return []
    return [
        f"{offenders} start with a markdown heading. The write-up is laid out for you; supply "
        "the content only."
    ]


# -- metrics ---------------------------------------------------------------------------------

#: How a tool's own rating maps onto ours, used *only* to measure divergence -- never to set a
#: severity. docs/02-contracts.md keeps `tool_severity` separate precisely so this comparison
#: is possible; using it as a default would make the critic redundant.
TOOL_SEVERITY_EQUIVALENT: dict[str, Severity] = {
    "HIGH": Severity.HIGH,
    "MEDIUM": Severity.MEDIUM,
    "LOW": Severity.LOW,
    "error": Severity.MEDIUM,
    "warning": Severity.LOW,
    "blocking": Severity.HIGH,
    "advisory": Severity.LOW,
    "F": Severity.HIGH,
    "E": Severity.HIGH,
    "D": Severity.MEDIUM,
    "C": Severity.MEDIUM,
    "B": Severity.LOW,
    "A": Severity.INFO,
}


def critic_metrics(critique: Critique, bundle: CritiqueBundle) -> dict[str, float | int]:
    """The numbers that answer "isn't this just a linter wrapper?" (docs/11-risks.md R3).

    Collected from the first run, because retrofitting them means re-running everything.
    """
    issues = critique.issues
    grounded = [i for i in issues if i.grounded]
    novel = [
        i
        for i in grounded
        if all(
            e.kind in (EvidenceKind.CODE_SPAN, EvidenceKind.TEST_FAILURE)
            for e in i.evidence
            if e.is_grounded
        )
    ]

    rerated = 0
    comparable = 0
    for issue in issues:
        for evidence in issue.evidence:
            if evidence.kind is not EvidenceKind.TOOL_FINDING:
                continue
            finding = bundle.patched_report.finding(evidence.ref)
            if finding is None or finding.tool_severity is None:
                continue
            equivalent = TOOL_SEVERITY_EQUIVALENT.get(finding.tool_severity)
            if equivalent is None:
                continue
            comparable += 1
            if issue.severity is not equivalent:
                rerated += 1
            break

    # docs/11-risks.md R1: "Track per-critic yield ... A critic scoring < 3/3 on seeded-bad is
    # broken." Observed on the first live seeded-bad run: the Red-team's summary named four
    # high-severity defects and emitted one issue. The summary is prose and not checkable, but
    # coverage of the tool's own high-severity findings is, and it is the number that detects
    # under-reporting. A metric, not a hard rejection: a critic legitimately declining a
    # finding as unreachable is the whole point of re-rating, and the eval -- not the
    # validator -- is where a pattern of silent omission should surface.
    cited_refs = {e.ref for i in issues for e in i.evidence}
    severe = [
        f
        for f in bundle.relevant_findings()
        if TOOL_SEVERITY_EQUIVALENT.get(f.tool_severity or "") is Severity.HIGH
    ]
    uncited_severe = [f.id for f in severe if f.id not in cited_refs]

    return {
        "issues": len(issues),
        "grounded": len(grounded),
        "grounded_fraction": round(len(grounded) / len(issues), 3) if issues else 1.0,
        # Yield against the tool's own high-severity findings.
        "severe_findings": len(severe),
        "severe_findings_cited": len(severe) - len(uncited_severe),
        "uncited_severe_findings": uncited_severe,
        # Near 0% means a linter wrapper with extra steps.
        "rerating_rate": round(rerated / comparable, 3) if comparable else 0.0,
        "rerated_of_comparable": f"{rerated}/{comparable}",
        # Issues no tool flagged at all.
        "novel_issue_rate": round(len(novel) / len(issues), 3) if issues else 0.0,
        "pressure": round(sum(i.score for i in issues), 2),
        "highest_severity": max((i.severity for i in issues), default=None),
    }


# --------------------------------------------------------------------------------------------
# The Coder
# --------------------------------------------------------------------------------------------


class ProposalError(ValueError):
    """A patch proposal is internally incoherent or names ids that do not exist.

    Distinct from a VALIDATE bounce: these are problems visible without applying the patch,
    so they are repaired on the schema-repair budget rather than consuming a patch attempt.
    """


def validate_patch_proposal(proposal: PatchProposal, bundle: CoderBundle) -> None:
    """Checks that need no patch application, collected and raised together.

    The apply/parse check deliberately lives elsewhere: docs/03-agents.md counts VALIDATE
    retries separately from every other budget, so mixing them in here would make a bad anchor
    look like a schema failure and corrupt the prompt-health signal.
    """
    problems: list[str] = []

    if proposal.round != bundle.round:
        problems.append(f"round must be {bundle.round}, got {proposal.round}.")

    allowed = bundle.addressable_ids()
    for label, ids in (
        ("addresses", proposal.addresses),
        (
            "deliberately_unaddressed",
            [u.issue_id for u in proposal.deliberately_unaddressed],
        ),
    ):
        unknown = [i for i in ids if i not in allowed]
        if unknown:
            preview = sorted(allowed)[:12]
            problems.append(
                f"{label} names ids that do not exist: {unknown}. Use ids from the allowed "
                f"list, e.g. {preview or '(the list is empty — leave the field empty)'}."
            )

    both = set(proposal.addresses) & {u.issue_id for u in proposal.deliberately_unaddressed}
    if both:
        problems.append(
            f"ids {sorted(both)} appear in both addresses and deliberately_unaddressed. "
            "Either you fixed it or you declined it; it cannot be both."
        )

    dismissed = set(bundle.dismissed_issue_ids) & set(proposal.addresses)
    if dismissed:
        # Changing code to satisfy a finding the Arbiter already threw out is the exact
        # pathology `deliberately_unaddressed` exists to prevent.
        problems.append(
            f"ids {sorted(dismissed)} were already dismissed as false positives. Do not "
            "change the code to satisfy them."
        )

    problems += _check_edits(proposal, bundle)

    if problems:
        raise ProposalError("\n".join(problems))


def _check_edits(proposal: PatchProposal, bundle: CoderBundle) -> list[str]:
    problems = _check_edits_do_not_overlap(proposal, bundle)
    for index, edit in enumerate(proposal.edits, start=1):
        if not edit.search.strip():
            problems.append(
                f"edit {index}: search block is empty or whitespace. It must be a verbatim, "
                "uniquely-matching excerpt of the source."
            )
            continue
        if _looks_line_numbered(edit.search) or _looks_line_numbered(edit.replace):
            # The source is shown as `  41| code`. Copying the gutter in is a common and
            # otherwise baffling failure: the anchor never matches and the error says only
            # "not found".
            problems.append(
                f"edit {index}: the search or replace text still contains the line-number "
                'gutter (e.g. "  41| "). Those numbers are display only — strip them.'
            )
        if edit.search == edit.replace:
            problems.append(f"edit {index}: search and replace are identical; it is a no-op.")
    return problems


def _check_edits_do_not_overlap(
    proposal: PatchProposal, bundle: CoderBundle
) -> list[str]:
    """Two edits must not touch the same region of the source.

    Observed on the SQL-injection fixture: the Coder emitted one edit replacing both the query
    line *and* the return line, then a second edit replacing the return line on its own. The
    first applied, the second then matched text the first had already rewritten, and the
    result did not parse -- reported as "the edit itself is malformed Python", which sends the
    retry looking in the wrong place entirely.

    Checked before application, against the *original* source, so the error names the real
    problem: the edits are incoherent with each other, not individually wrong.
    """
    spans: list[tuple[int, int, int]] = []
    for index, edit in enumerate(proposal.edits, start=1):
        if not edit.search:
            continue
        at = bundle.source.find(edit.search)
        if at == -1:
            continue  # a missing anchor is `patch.py`'s error to report, with its own repair
        spans.append((at, at + len(edit.search), index))

    problems = []
    spans.sort()
    for (_start, end, first), (next_start, _, second) in zip(spans, spans[1:], strict=False):
        if next_start < end:
            problems.append(
                f"edits {first} and {second} both cover the same lines of the source. Each "
                "edit must anchor on a distinct region; combine them into one block instead."
            )
    return problems


_GUTTER = re.compile(r"^\s*\d+\|", re.MULTILINE)


def _looks_line_numbered(text: str) -> bool:
    return bool(_GUTTER.search(text))
