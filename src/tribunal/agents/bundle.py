"""The input bundle a critic is given, and how it is rendered into a prompt.

Not in `contracts.py` on purpose. docs/02-contracts.md's rule is that every schema *crossing an
agent boundary* lives there; this is an input the orchestrator assembles for one agent, never
something two agents exchange. Keeping it here stops `contracts.py` -- whose job is to show the
system's whole vocabulary on one screen -- from accumulating plumbing.

Two rendering decisions matter:

**Line-numbered source.** docs/03-agents.md § Coder lists this first among diff-reliability
mitigations ("cheap, large effect"), and it matters just as much for critics: a `code_span`
evidence ref is only checkable if the model could see which line was which.

**The Coder's rationale is rendered as a claim to check.** R1 in docs/11-risks.md is that critics
go sycophantic when handed a confident justification as context. The rationale is therefore
labelled as an unverified claim, not as background.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from tribunal.contracts import (
    Conflict,
    Critique,
    Decision,
    Dimension,
    Evidence,
    EvidenceKind,
    GroundingFinding,
    GroundingReport,
    Issue,
    Patch,
    Severity,
    UnaddressedIssue,
    Verdict,
)

#: Which grounding tools each dimension is allowed to cite. A critic citing a tool outside its
#: dimension is either confused or padding, and `validation.py` rejects it.
DIMENSION_TOOLS: dict[Dimension, frozenset[str]] = {
    Dimension.SECURITY: frozenset({"bandit", "ruff", "astgate", "pytest"}),
    Dimension.PERFORMANCE: frozenset({"radon", "ruff", "perf", "pytest"}),
}

#: Rule prefixes that belong to the security dimension within `ruff`'s broader rule set.
SECURITY_RUFF_PREFIXES = ("S",)
PERF_RUFF_PREFIXES = ("C90", "PERF", "RUF", "SIM", "B")


@dataclass(frozen=True)
class CritiqueBundle:
    """Everything a critic sees for one round."""

    round: int
    dimension: Dimension
    filename: str
    patched_source: str
    patched_report: GroundingReport
    baseline_report: GroundingReport
    #: None on a synthetic/standalone critique of an unpatched file.
    patch: Patch | None = None
    #: The Arbiter's synthesis from the previous round. The only channel between critics.
    consolidated_critique: str | None = None
    #: Issue ids the Arbiter has already dismissed; re-raising one wastes a round.
    dismissed_issue_ids: tuple[str, ...] = field(default_factory=tuple)

    # -- what this dimension may cite ---------------------------------------------------

    def relevant_findings(self) -> list[GroundingFinding]:
        """Findings this dimension is allowed to cite, in line order.

        Filtering by dimension is not tidiness. Handing the Profiler a wall of `bandit` output
        invites it to raise security issues in the performance dimension, which the policy
        layer then has to attribute to the wrong critic.
        """
        allowed = DIMENSION_TOOLS[self.dimension]
        out = []
        for finding in self.patched_report.findings:
            if finding.tool not in allowed:
                continue
            if finding.tool == "ruff" and not self._ruff_matches(finding.rule):
                continue
            out.append(finding)
        return sorted(out, key=lambda f: (f.line or 0, f.tool, f.rule))

    def _ruff_matches(self, rule: str) -> bool:
        prefixes = (
            SECURITY_RUFF_PREFIXES
            if self.dimension is Dimension.SECURITY
            else PERF_RUFF_PREFIXES
        )
        return rule.startswith(prefixes)

    def citable_measurements(self) -> list:
        return [m for m in self.patched_report.measurements if m.is_citable]

    def uncitable_measurements(self) -> list:
        return [m for m in self.patched_report.measurements if not m.is_citable]

    # -- rendering ----------------------------------------------------------------------

    def render(self) -> str:
        """The volatile half of the request. Nothing here may leak into the cached prefix."""
        blocks = [
            f"Round {self.round}. Assess the patched file for "
            f"{self.dimension.value} issues.",
            "",
            self._source_block(),
            self._findings_block(),
        ]
        if self.dimension is Dimension.PERFORMANCE:
            blocks.append(self._measurements_block())
            blocks.append(self._complexity_block())
        blocks.append(self._tests_block())
        blocks.append(self._tool_errors_block())
        if self.patch is not None:
            blocks.append(self._patch_block())
        if self.consolidated_critique:
            blocks.append(self._prior_round_block())
        blocks.append(
            f"Emit the Critique with dimension=\"{self.dimension.value}\" and "
            f"round={self.round}."
        )
        return "\n".join(block for block in blocks if block)

    def _source_block(self) -> str:
        numbered = "\n".join(
            f"{index:>4}| {line}"
            for index, line in enumerate(self.patched_source.splitlines(), start=1)
        )
        return f"### {self.filename} (patched)\n{numbered}\n"

    def tools_available(self) -> list[str]:
        """Tools that ran successfully and are citable in this dimension."""
        allowed = DIMENSION_TOOLS[self.dimension]
        return sorted(set(self.patched_report.tools_succeeded) & allowed)

    def _findings_block(self) -> str:
        findings = self.relevant_findings()
        ran = self.tools_available()
        silent = self.patched_report.tools_reporting_nothing(DIMENSION_TOOLS[self.dimension])
        if not findings:
            return (
                f"### Grounding findings for {self.dimension.value} on {self.filename}\n"
                f"(none — these tools ran and reported nothing in this dimension: "
                f"{', '.join(ran) or 'no tool for this dimension ran'})\n"
                "A tool running clean is a real result. List the tools you consulted in "
                "tools_consulted even when they found nothing.\n"
            )
        baseline_ids = {f.id for f in self.baseline_report.findings}
        lines = []
        for f in findings:
            origin = "pre-existing" if f.id in baseline_ids else "NEW since baseline"
            location = f"line {f.line}" if f.line is not None else "whole file"
            lines.append(
                f"- id={f.id} tool={f.tool} rule={f.rule} "
                f"tool_severity={f.tool_severity or 'n/a'} {location} [{origin}]\n"
                f"  {f.message}"
            )
        block = (
            f"### Grounding findings for {self.dimension.value} on {self.filename}\n"
            + "\n".join(lines)
            + "\n"
        )
        if silent:
            block += f"Also ran and reported nothing here: {', '.join(silent)}\n"
        return block

    def _measurements_block(self) -> str:
        citable = self.citable_measurements()
        uncitable = self.uncitable_measurements()
        if not citable and not uncitable:
            return (
                "### Measurements\n"
                "(none — no benchmark was run. Do not state any timing or percentage.)\n"
            )
        lines = ["### Measurements"]
        for m in citable:
            lines.append(
                f"- CITABLE label={m.label} verdict={m.verdict} "
                f"before={m.before_ns}ns after={m.after_ns}ns "
                f"stdev={m.stdev_ns}ns repeats={m.repeats}"
            )
        for m in uncitable:
            # Listed, not hidden. The critic must be able to say "performance could not be
            # measured" -- but it must also be unable to mistake this for a result.
            lines.append(
                f"- NOT CITABLE label={m.label} verdict={m.verdict} "
                "(you MUST NOT cite this as measurement evidence)"
            )
        return "\n".join(lines) + "\n"

    def _complexity_block(self) -> str:
        """Before/after cyclomatic complexity, the one claim always well-founded."""
        before = _complexity_table(self.baseline_report)
        after = _complexity_table(self.patched_report)
        if not before and not after:
            return ""
        names = sorted(set(before) | set(after))
        rows = []
        for name in names:
            was, now = before.get(name), after.get(name)
            if was == now:
                continue
            was_text = str(was) if was is not None else "absent"
            now_text = str(now) if now is not None else "removed"
            rows.append(f"- {name}: complexity {was_text} -> {now_text}")
        if not rows:
            return "### Complexity delta (radon)\n(unchanged by this patch)\n"
        return "### Complexity delta (radon)\n" + "\n".join(rows) + "\n"

    def _tests_block(self) -> str:
        tests = self.patched_report.tests
        if tests is None:
            return "### Tests\n(no test was supplied)\n"
        if not tests.ran:
            return f"### Tests\n(not run: {tests.unavailable_reason})\n"
        if tests.timed_out:
            return "### Tests\n(the suite timed out — treat as an unknown, not a pass)\n"
        head = (
            f"### Tests\npassed={tests.passed} failed={tests.failed} "
            f"errors={tests.errors} skipped={tests.skipped}"
        )
        if tests.failed_node_ids:
            head += "\nfailing node ids:\n" + "\n".join(
                f"  - {node}" for node in tests.failed_node_ids
            )
        return head + "\n"

    def _tool_errors_block(self) -> str:
        if not self.patched_report.tool_errors:
            return ""
        lines = "\n".join(
            f"- {tool}: {error}" for tool, error in sorted(self.patched_report.tool_errors.items())
        )
        return (
            "### Tools that did not run\n"
            "These dimensions are UNASSESSED, which is not the same as clean. Do not infer "
            "absence of defects from absence of findings here.\n" + lines + "\n"
        )

    def _patch_block(self) -> str:
        assert self.patch is not None  # noqa: S101
        addresses = ", ".join(self.patch.addresses) or "(nothing specific)"
        pushback = "\n".join(
            f"  - {u.issue_id}: {u.reason}" for u in self.patch.deliberately_unaddressed
        )
        block = (
            "### The Coder's claim — UNVERIFIED, treat as a claim to check\n"
            f"It says it addressed: {addresses}\n"
            f"Its stated rationale: {self.patch.rationale}\n"
        )
        if pushback:
            block += f"It declined to address, with reasons:\n{pushback}\n"
        block += (
            "Verify these claims against the source and the findings above. A confident "
            "rationale is not evidence.\n\n"
            f"### The diff under review\n{self.patch.diff or '(empty diff)'}\n"
        )
        return block

    def _prior_round_block(self) -> str:
        block = (
            "### Consolidated critique from the previous round (from the Arbiter)\n"
            f"{self.consolidated_critique}\n"
        )
        if self.dismissed_issue_ids:
            block += (
                "\nAlready dismissed as false positives — do NOT raise these again:\n"
                + "\n".join(f"  - {issue_id}" for issue_id in self.dismissed_issue_ids)
                + "\n"
            )
        return block


def _complexity_table(report: GroundingReport) -> dict[str, int]:
    """Pull the per-function complexity table out of radon's summary finding."""
    for finding in report.by_tool("radon"):
        blocks = finding.raw.get("blocks")
        if isinstance(blocks, list):
            return {
                row["name"]: row["complexity"]
                for row in blocks
                if isinstance(row, dict) and row.get("name") and "complexity" in row
            }
    return {}


@dataclass(frozen=True)
class CoderBundle:
    """Everything the Coder sees for one patch attempt.

    ## What `addresses` can name, and why it depends on the round

    `Patch.addresses` is documented as "`Issue.id`s this patch intends to resolve"
    (docs/02-contracts.md). On **round 1 there are no `Issue`s** -- the critics have not run
    yet, and the Coder is working from the grounding baseline and an optional failing test. So
    the only ids in existence are `GroundingFinding.id`s.

    The docs do not resolve this. Rather than leave `addresses` empty on round 1 -- which would
    delete the churn check docs/03-agents.md asks for (`hunks_touched` vs `addresses` count) --
    the field accepts any id present in `addressable_ids()`: grounding finding ids on round 1,
    plus open `Issue` ids from round 2 onward. `validation.py` checks against that union, so a
    made-up id is still rejected.

    ## The validation-error field

    `last_validation_error` carries the mechanical bounce message from `patch.validate` back
    into the next attempt's prompt. Those attempts are counted **separately** from debate
    rounds (docs/03-agents.md mitigation 3), which is why they are a field here rather than
    part of the LLM client's schema-repair budget.
    """

    round: int
    filename: str
    source: str
    report: GroundingReport
    max_hunks: int = 8
    failing_test: str | None = None
    traceback: str | None = None
    #: The Arbiter's synthesis. Round >= 2 only; the single channel between the critics.
    consolidated_critique: str | None = None
    priority_order: tuple[str, ...] = field(default_factory=tuple)
    open_issues: tuple[Issue, ...] = field(default_factory=tuple)
    #: Its own diffs that VALIDATE or the policy layer already rejected, with the reason.
    rejected_diffs: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    dismissed_issue_ids: tuple[str, ...] = field(default_factory=tuple)
    last_validation_error: str | None = None
    last_rejected_diff: str | None = None

    def addressable_ids(self) -> set[str]:
        return {f.id for f in self.report.findings} | {i.id for i in self.open_issues}

    def with_validation_error(self, reason: str, diff: str) -> CoderBundle:
        """The next attempt, carrying the mechanical error from the failed one."""
        return replace(
            self,
            last_validation_error=reason,
            last_rejected_diff=diff,
            rejected_diffs=(*self.rejected_diffs, (diff, reason)),
        )

    # -- rendering ----------------------------------------------------------------------

    def render(self) -> str:
        blocks = [
            f"Round {self.round}. Propose a minimal patch for {self.filename}.",
            "",
            self._source_block(),
            self._findings_block(),
            self._test_block(),
            self._traceback_block(),
            self._critique_block(),
            self._issues_block(),
            self._rejected_block(),
            self._retry_block(),
            self._closing(),
        ]
        return "\n".join(block for block in blocks if block)

    def _source_block(self) -> str:
        numbered = "\n".join(
            f"{index:>4}| {line}"
            for index, line in enumerate(self.source.splitlines(), start=1)
        )
        return f"### {self.filename} (current source)\n{numbered}\n"

    def _findings_block(self) -> str:
        findings = sorted(self.report.findings, key=lambda f: (f.line or 0, f.tool, f.rule))
        if not findings:
            ran = ", ".join(self.report.tools_succeeded) or "none"
            return f"### Grounding findings\n(none — these tools ran clean: {ran})\n"
        lines = []
        for f in findings:
            location = f"line {f.line}" if f.line is not None else "whole file"
            lines.append(
                f"- id={f.id} tool={f.tool} rule={f.rule} "
                f"tool_severity={f.tool_severity or 'n/a'} {location}\n  {f.message}"
            )
        return "### Grounding findings on the current source\n" + "\n".join(lines) + "\n"

    def _test_block(self) -> str:
        if self.failing_test is None:
            tests = self.report.tests
            if tests is not None and tests.ran and tests.failed_node_ids:
                nodes = "\n".join(f"  - {n}" for n in tests.failed_node_ids)
                return f"### Failing tests\n{nodes}\n"
            return ""
        return (
            "### The test that must pass\n"
            "This is the correctness oracle. A patch that fixes a finding and breaks this is "
            "not a trade-off, it is broken.\n"
            f"```python\n{self.failing_test}\n```\n"
        )

    def _traceback_block(self) -> str:
        if not self.traceback:
            return ""
        return f"### Traceback\n```\n{self.traceback}\n```\n"

    def _critique_block(self) -> str:
        if not self.consolidated_critique:
            return ""
        block = (
            "### Consolidated critique (from the Arbiter)\n"
            "This is a single synthesised instruction set, not two critics concatenated. "
            "Follow it in order.\n"
            f"{self.consolidated_critique}\n"
        )
        if self.priority_order:
            ordered = "\n".join(
                f"  {n}. {issue_id}" for n, issue_id in enumerate(self.priority_order, 1)
            )
            block += f"\nPriority order, most important first:\n{ordered}\n"
        if self.dismissed_issue_ids:
            block += (
                "\nAlready dismissed as false positives — do NOT change the code to satisfy "
                "these:\n" + "\n".join(f"  - {i}" for i in self.dismissed_issue_ids) + "\n"
            )
        return block

    def _issues_block(self) -> str:
        if not self.open_issues:
            return ""
        lines = []
        for issue in self.open_issues:
            refs = ", ".join(f"{e.kind.value}:{e.ref}" for e in issue.evidence)
            lines.append(
                f"- id={issue.id} [{issue.dimension.value}/{issue.severity.value}] "
                f"confidence={issue.confidence}\n"
                f"  {issue.title}\n"
                f"  {issue.explanation}\n"
                f"  evidence: {refs}\n"
                f"  suggested direction: {issue.suggested_direction or '(none given)'}"
            )
        return "### Open issues\n" + "\n".join(lines) + "\n"

    def _rejected_block(self) -> str:
        """Its own rejected diffs. Without these the Coder re-proposes the same thing."""
        earlier = self.rejected_diffs[:-1] if self.last_rejected_diff else self.rejected_diffs
        if not earlier:
            return ""
        lines = []
        for diff, reason in earlier:
            lines.append(f"- rejected because: {reason}\n```diff\n{diff or '(empty)'}\n```")
        return (
            "### Your earlier attempts that were rejected\n"
            "Do not propose these again.\n" + "\n".join(lines) + "\n"
        )

    def _retry_block(self) -> str:
        if not self.last_validation_error:
            return ""
        return (
            "### YOUR LAST ATTEMPT DID NOT APPLY\n"
            "This is a mechanical failure, not a disagreement about the fix. The error below "
            "is exact. Re-anchor your edit and try again; do not change your approach.\n"
            f"  {self.last_validation_error}\n"
            f"\nWhat you sent:\n```diff\n{self.last_rejected_diff or '(empty)'}\n```\n"
        )

    def _closing(self) -> str:
        addressable = sorted(self.addressable_ids())
        preview = ", ".join(addressable[:12]) + (" ..." if len(addressable) > 12 else "")
        return (
            f"Emit a PatchProposal with round={self.round}. Prefer `edits` (search/replace "
            f"blocks); leave `diff` as an empty string. Keep the patch under "
            f"{self.max_hunks} hunks.\n"
            f"Ids you may put in `addresses` or `deliberately_unaddressed`: {preview or '(none)'}"
        )


@dataclass(frozen=True)
class ArbiterBundle:
    """Everything the Arbiter sees, after the decision has already been made.

    The ordering is the point. `verdict` is not a suggestion and not a draft: it came out of
    `policy.decide` before this bundle was built, and the render leads with it precisely so
    the model cannot mistake its job for deciding. docs/03-agents.md § 3.4: the Arbiter is a
    *writer* over a decision it cannot change, and `ArbiterNote.decision_echo` is the check
    that catches prose that drifted anyway.

    Both critiques go in whole, including the one that said `clean`. A synthesis that only
    sees the complaints cannot tell "the other critic looked and found nothing" from "the
    other critic never ran" -- and those have opposite implications for what the Coder should
    do next.
    """

    round: int
    filename: str
    verdict: Verdict
    critiques: tuple[Critique, ...]
    patch: Patch | None = None
    #: Ids dismissed in *earlier* rounds. Already-dismissed issues are not re-adjudicated.
    already_dismissed: tuple[str, ...] = field(default_factory=tuple)

    def open_issues(self) -> list[Issue]:
        """The issues policy counted as open, in the order the verdict names them."""
        by_id = {issue.id: issue for c in self.critiques for issue in c.issues}
        return [by_id[i] for i in self.verdict.open_issues if i in by_id]

    def pushbacks(self) -> list[UnaddressedIssue]:
        """The Coder's claims that a finding is wrong. Each needs an answer."""
        if self.patch is None:
            return []
        return [
            claim
            for claim in self.patch.deliberately_unaddressed
            if claim.issue_id not in self.already_dismissed
        ]

    def adjudicable_ids(self) -> set[str]:
        """Ids the Arbiter may put in `dismissed`: the pushbacks, and nothing else.

        Dismissal is permanent and removes an issue from `pressure` for the rest of the run.
        Letting the Arbiter dismiss an issue nobody contested would hand it a way to lower the
        pressure it was told not to decide about -- the decision, re-entered through the side
        door.
        """
        return {claim.issue_id for claim in self.pushbacks()}

    # -- rendering ----------------------------------------------------------------------

    def render(self) -> str:
        blocks = [
            self._decision_block(),
            self._issues_block(),
            self._pushback_block(),
            self._patch_block(),
            self._pressure_block(),
            self._closing(),
        ]
        return "\n".join(block for block in blocks if block)

    def _decision_block(self) -> str:
        conflict = ""
        if self.verdict.conflict is not None:
            c = self.verdict.conflict
            conflict = (
                f"\nThe conflict, as detected: {c.left_issue} vs {c.right_issue} on axis "
                f"{c.axis} (detector: {c.detector}).\n"
                f"  {c.left_issue} remedy costs: {c.left_remedy_cost}\n"
                f"  {c.right_issue} remedy costs: {c.right_remedy_cost}\n"
            )
        return (
            f"Round {self.round}, reviewing {self.filename}.\n\n"
            "### THE DECISION IS ALREADY MADE\n"
            f"decision: {self.verdict.decision.value}\n"
            f"rule fired: {self.verdict.rule_fired}\n"
            f"pressure: {self.verdict.pressure:g}\n"
            "You are writing this decision up. You cannot change it, and nothing you write "
            "will be read as changing it. Echo it back in `decision_echo`.\n"
            f"{conflict}"
        )

    def _issues_block(self) -> str:
        issues = self.open_issues()
        if not issues:
            return (
                "### Open issues\n(none — no issue is holding this patch up.)\n"
            )
        lines = []
        for issue in issues:
            refs = ", ".join(f"{e.kind.value}:{e.ref}" for e in issue.evidence)
            lines.append(
                f"- id={issue.id} [{issue.dimension.value}/{issue.severity.value}] "
                f"confidence={issue.confidence} score={issue.score:g}\n"
                f"  {issue.title}\n"
                f"  {issue.explanation}\n"
                f"  evidence: {refs}\n"
                f"  the critic's suggested direction: "
                f"{issue.suggested_direction or '(none given)'}"
            )
        block = "### Open issues, from both critics\n" + "\n".join(lines) + "\n"
        clean = [c.dimension.value for c in self.critiques if c.verdict == "clean"]
        if clean:
            block += (
                f"These critics looked and found nothing: {', '.join(sorted(clean))}. That is "
                "a result, not a gap.\n"
            )
        if self.verdict.unassessed_dimensions:
            names = ", ".join(d.value for d in self.verdict.unassessed_dimensions)
            block += (
                f"UNASSESSED — nobody checked these dimensions this round: {names}. Do not "
                "describe them as clean.\n"
            )
        return block

    def _pushback_block(self) -> str:
        claims = self.pushbacks()
        if not claims:
            return ""
        lines = "\n".join(f"  - {c.issue_id}: {c.reason}" for c in claims)
        return (
            "### The Coder's pushback — adjudicate each one\n"
            "It declined to address these, with its reasons. Agree (put the id in "
            "`dismissed` with your own reason) or disagree (leave it in `priority_order`). "
            "A dismissal is permanent for the rest of the run and removes the issue from the "
            "pressure sum, so agree only when the finding is genuinely wrong — not when the "
            "fix is merely awkward.\n"
            f"{lines}\n"
        )

    def _patch_block(self) -> str:
        if self.patch is None:
            return ""
        return (
            "### The patch under discussion\n"
            f"Its stated rationale: {self.patch.rationale}\n"
            f"```diff\n{self.patch.diff or '(empty diff)'}\n```\n"
        )

    def _pressure_block(self) -> str:
        history = self.verdict.pressure_history
        if not history:
            return ""
        trail = " -> ".join(f"{value:g}" for value in [*history, self.verdict.pressure])
        return (
            f"### Pressure across rounds\n{trail}\n"
            "Flat or rising pressure means the previous round's instructions did not land. "
            "If so, say what was unclear about them and be more specific this time.\n"
        )

    def _closing(self) -> str:
        decision = self.verdict.decision
        ids = ", ".join(i.id for i in self.open_issues()) or "(none)"
        required = {
            Decision.REJECT: (
                "Write `consolidated_critique`: ONE instruction set for the Coder, ordered, "
                "with the contradictions between the critics already resolved. Leave "
                "`tradeoff_justification` and `recommended_default` null."
            ),
            Decision.TRADEOFF: (
                "Write `tradeoff_justification` and `recommended_default`: which side ships, "
                "what it costs, and the condition under which the other side wins. Leave "
                "`consolidated_critique` null."
            ),
        }.get(
            decision,
            "Leave `consolidated_critique`, `tradeoff_justification` and "
            "`recommended_default` null; this decision needs none of them.",
        )
        return (
            f"Emit an ArbiterNote with round={self.round} and "
            f"decision_echo=\"{decision.value}\".\n"
            f"{required}\n"
            f"`priority_order` may contain only these ids: {ids}"
        )


@dataclass(frozen=True)
class AffirmationBundle:
    """One question, asked of the Arbiter before policy decides: are these two opposed?

    docs/04-arbitration.md § Conflict detection makes detector 2 "weaker — so it requires the
    Arbiter to affirm the conflict, and a non-affirmation falls through to row 9/11". The
    mechanical half (grounded, MEDIUM+, overlapping spans, opposite dimensions) is already
    true of the pair by the time this is built; the only open question is whether the two
    remedies genuinely exclude each other.

    Deliberately narrow. It does not see the verdict, the patch history or the pressure,
    because none of those bear on the question and all of them invite the model to answer a
    bigger one.
    """

    round: int
    filename: str
    source: str
    left: Issue
    right: Issue

    def render(self) -> str:
        return "\n".join(
            [
                f"Round {self.round}. Two issues in {self.filename} cite overlapping lines.",
                "",
                self._span_block(),
                self._issue_block("A", self.left),
                self._issue_block("B", self.right),
                (
                    f"Answer for exactly this pair: left_issue=\"{self.left.id}\", "
                    f"right_issue=\"{self.right.id}\"."
                ),
            ]
        )

    def _span_block(self) -> str:
        """The overlapping lines, and only those. The rest of the file is not the question."""
        spans = [*self.left.spans, *self.right.spans]
        if not spans:
            return ""
        lines = self.source.splitlines()
        start = max(1, min(s for s, _ in spans) - 2)
        end = min(len(lines), max(e for _, e in spans) + 2)
        numbered = "\n".join(
            f"{index:>4}| {lines[index - 1]}" for index in range(start, end + 1)
        )
        return f"### {self.filename} lines {start}-{end}\n{numbered}\n"

    def _issue_block(self, label: str, issue: Issue) -> str:
        return (
            f"### Issue {label}: {issue.id} [{issue.dimension.value}/"
            f"{issue.severity.value}]\n"
            f"{issue.title}\n{issue.explanation}\n"
            f"proposed remedy: {issue.suggested_direction or '(none given)'}\n"
        )


@dataclass(frozen=True)
class RoundDigest:
    """One round, flattened out of the trace for the Postmortem to read."""

    round: int
    decision: str
    rule_fired: str
    pressure: float
    rationale: str | None
    issue_ids: tuple[str, ...]
    consolidated_critique: str | None = None


@dataclass(frozen=True)
class PostmortemBundle:
    """The run, compacted, for the agent that writes it up.

    docs/03-agents.md § 3.5 is specific about the input: "the full trace (compacted: policy
    decisions, verdicts, arbiter notes, final patch -- **not raw prompts**)". The exclusion is
    the interesting half. A postmortem handed the raw prompts would summarise the
    *conversation*, which is both enormous and the wrong object: what happened is the sequence
    of decisions, and that is what a reader wants recounted.

    It is also what makes the agent re-runnable. Reading the trace rather than the exchange
    means `tribunal postmortem <trace>` can rewrite an old run's narrative for one call,
    which is how the report's wording gets iterated on without paying for the debate again.

    Built from a `Trace` rather than from the orchestrator's locals for the same reason: the
    version that runs inside a live run and the version that runs over a file months later
    must be the same code, or the second one is untested.
    """

    run_id: str
    filename: str
    outcome: Decision
    rule_fired: str
    rounds: tuple[RoundDigest, ...]
    accepted_diff: str | None
    no_patch_reason: str | None
    open_issues: tuple[Issue, ...]
    fixed_issues: tuple[Issue, ...]
    dismissed_issues: tuple[Issue, ...]
    unassessed_dimensions: tuple[Dimension, ...]
    conflict: Conflict | None = None
    tradeoff_justification: str | None = None
    #: Measurements the Profiler was forbidden to cite. They belong in "what I would not
    #: trust" precisely because they are not evidence of anything.
    uncitable_measurements: tuple[str, ...] = field(default_factory=tuple)

    # -- what the note must account for -------------------------------------------------

    def must_mention(self) -> tuple[str, ...]:
        """Facts the write-up cannot omit: every open issue, every unassessed dimension.

        Not a style rule. An accepted patch with an open `medium` and a performance
        dimension nobody checked is a *conditional* pass, and a narrative that reads as an
        unconditional one is actively misleading -- worse than no narrative, because a reader
        stops looking.
        """
        return tuple(
            [issue.id for issue in self.open_issues]
            + [dimension.value for dimension in self.unassessed_dimensions]
        )

    # -- rendering ----------------------------------------------------------------------

    def render(self) -> str:
        blocks = [
            self._outcome_block(),
            self._rounds_block(),
            self._issues_block(),
            self._conflict_block(),
            self._diff_block(),
            self._uncertainty_block(),
            self._closing(),
        ]
        return "\n".join(block for block in blocks if block)

    def _outcome_block(self) -> str:
        return (
            f"Write up this run of {self.filename}.\n\n"
            "### Outcome, already decided\n"
            f"outcome: {self.outcome.value}\n"
            f"rule fired: {self.rule_fired}\n"
            f"rounds: {len(self.rounds)}\n"
            "Echo the outcome in `outcome_echo`. You are recounting what happened, not "
            "re-judging it.\n"
        )

    def _rounds_block(self) -> str:
        if not self.rounds:
            return "### Rounds\n(none — the run ended before a round completed.)\n"
        lines = []
        for digest in self.rounds:
            lines.append(
                f"- round {digest.round}: {digest.decision} via {digest.rule_fired}, "
                f"pressure {digest.pressure:g}\n"
                f"  what the patch did: {digest.rationale or '(no rationale recorded)'}\n"
                f"  issues raised: {', '.join(digest.issue_ids) or '(none)'}"
            )
            if digest.consolidated_critique:
                lines.append(
                    f"  the Arbiter told the Coder: {digest.consolidated_critique}"
                )
        return "### What each round did\n" + "\n".join(lines) + "\n"

    def _issues_block(self) -> str:
        sections = [
            ("Still open at the end", self.open_issues),
            ("Fixed during the run", self.fixed_issues),
            ("Dismissed as false positives", self.dismissed_issues),
        ]
        lines = []
        for title, issues in sections:
            if not issues:
                continue
            lines.append(f"{title}:")
            lines += [
                f"  - {i.id} [{i.dimension.value}/{i.severity.value}] {i.title}"
                for i in issues
            ]
        if not lines:
            return "### Issues\n(none was raised at any point.)\n"
        return "### Issues\n" + "\n".join(lines) + "\n"

    def _conflict_block(self) -> str:
        if self.conflict is None:
            return ""
        block = (
            "### The trade-off\n"
            f"{self.conflict.left_issue} vs {self.conflict.right_issue} on axis "
            f"{self.conflict.axis}, detected by {self.conflict.detector}.\n"
            f"  {self.conflict.left_issue} costs: {self.conflict.left_remedy_cost}\n"
            f"  {self.conflict.right_issue} costs: {self.conflict.right_remedy_cost}\n"
        )
        if self.tradeoff_justification:
            block += f"The Arbiter's justification: {self.tradeoff_justification}\n"
        return block

    def _diff_block(self) -> str:
        if self.accepted_diff is None:
            return f"### No patch was accepted\nWhy: {self.no_patch_reason}\n"
        return f"### The accepted patch\n```diff\n{self.accepted_diff}\n```\n"

    def _uncertainty_block(self) -> str:
        """Handed over explicitly, because the closing section depends on it being complete."""
        lines = []
        if self.unassessed_dimensions:
            names = ", ".join(d.value for d in self.unassessed_dimensions)
            lines.append(
                f"- UNASSESSED dimensions: {names}. Nobody checked these. This is not the "
                "same as clean, and it must appear in what_i_would_not_trust."
            )
        if self.open_issues:
            ids = ", ".join(i.id for i in self.open_issues)
            lines.append(
                f"- Issues still open at the end: {ids}. Each must appear in "
                "what_i_would_not_trust."
            )
        if self.uncitable_measurements:
            labels = ", ".join(self.uncitable_measurements)
            lines.append(
                f"- Measurements that produced no usable result: {labels}. An inconclusive "
                "or unmeasurable benchmark is not evidence of no change."
            )
        if not lines:
            return (
                "### Known limits of this run\n"
                "Nothing mechanical is outstanding: no open issue, no unassessed dimension, "
                "no inconclusive measurement. Say what a reader should still be careful "
                "about anyway — the review was single-file, and the tools have blind spots.\n"
            )
        return "### Known limits of this run\n" + "\n".join(lines) + "\n"

    def _closing(self) -> str:
        return (
            f"Emit a PostmortemNote with outcome_echo=\"{self.outcome.value}\" and one "
            f"`rounds` entry per round above ({len(self.rounds)})."
        )


def synthetic_issue(
    dimension: Dimension,
    title: str,
    explanation: str,
    ref: str,
    severity: Severity = Severity.MEDIUM,
    confidence: float = 0.8,
    suggested_direction: str | None = None,
) -> Issue:
    """Build an `Issue` outside the critic path.

    Needed in two places that are not shortcuts: the Coder fixtures for defects no static tool
    flags, and `tribunal propose --issue`, which lets a human hand the Coder a concern
    directly. The id is canonical from the start, so it behaves like a critic's issue
    everywhere downstream.
    """
    from tribunal.contracts import canonical_issue_id

    return Issue(
        id=canonical_issue_id(dimension, "supplied", ref),
        dimension=dimension,
        severity=severity,
        title=title,
        explanation=explanation,
        evidence=[Evidence(kind=EvidenceKind.CODE_SPAN, ref=ref, excerpt="")],
        confidence=confidence,
        introduced_by_patch=False,
        suggested_direction=suggested_direction,
    )
