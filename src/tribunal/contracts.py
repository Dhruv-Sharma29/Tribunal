"""All schemas for tribunal. Single source of truth.

Everything that crosses an agent boundary, a tool boundary, or lands in the trace is defined
here. Deliberately one module rather than a package: the system's whole vocabulary should be
readable in one pass (see docs/01-architecture.md § Repo layout).

Design rules enforced here (docs/02-contracts.md § Design rules):

1. Critics share one output shape (`Critique`), so policy treats dimensions uniformly.
2. Every `Issue` MUST carry at least one `Evidence`. `EvidenceKind.REASONING` is legal but
   marks the issue ungrounded, which halves its policy weight.
3. `Confidence` is quantised to 0.05 and validated strictly -- a model that emits 0.07 gets a
   repair retry rather than silently introducing false precision into cassette hashes.
4. `Severity.weight` is defined exactly once, in code, and never re-derived in a prompt.
5. No field is optional unless absence is semantically distinct from a default.
6. `SCHEMA_VERSION` gates trace compatibility.
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Bumped only on a breaking change to any schema below; the trace reader refuses unknown
# majors rather than rendering garbage (docs/02-contracts.md § Schema evolution).
SCHEMA_VERSION = "1.0"

CONFIDENCE_QUANTUM = 0.05

Confidence = Annotated[float, Field(ge=0.0, le=1.0, multiple_of=CONFIDENCE_QUANTUM)]


def quantise_confidence(value: float) -> float:
    """Snap a confidence to the nearest legal quantum.

    Not used on the validation path -- `Confidence` rejects off-grid values so the failure is
    visible as a parse retry. This exists for the agent layer's repair pass and for tests.
    """
    clamped = min(1.0, max(0.0, value))
    return round(round(clamped / CONFIDENCE_QUANTUM) * CONFIDENCE_QUANTUM, 2)


class Severity(StrEnum):
    """Severity levels and their fixed policy weights.

    `StrEnum` rather than `(str, Enum)`: members of the latter format as `Severity.HIGH` in an
    f-string, and these values get interpolated into prompts and trace payloads, where the
    wanted text is `high`.
    """


    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def weight(self) -> int:
        """The ONLY place these numbers exist.

        Changing them does not bump `SCHEMA_VERSION`, but it does invalidate every published
        eval number -- freeze before the held-out sweep (docs/09-roadmap.md).
        """
        return {"info": 0, "low": 1, "medium": 4, "high": 16}[self.value]


class Dimension(StrEnum):
    SECURITY = "security"
    PERFORMANCE = "performance"
    CORRECTNESS = "correctness"  # emitted by VALIDATE / pytest, not by a critic


#: Short, human-legible prefix per dimension, used in `Issue.id` so a trace or a viewer
#: screenshot reads as `SEC-3f9a1c2b04` rather than as an opaque hash.
DIMENSION_PREFIX = {
    Dimension.SECURITY: "SEC",
    Dimension.PERFORMANCE: "PERF",
    Dimension.CORRECTNESS: "CORR",
}


class EvidenceKind(StrEnum):
    TOOL_FINDING = "tool_finding"  # cites a GroundingFinding.id
    MEASUREMENT = "measurement"  # cites a PerfMeasurement.label
    TEST_FAILURE = "test_failure"  # cites a pytest node id
    CODE_SPAN = "code_span"  # cites lines in the patched file
    REASONING = "reasoning"  # ungrounded -- allowed, penalised


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EvidenceKind
    ref: str = Field(min_length=1)  # finding id / node id / "patched.py:L41-L47"
    excerpt: str = Field(max_length=600)  # verbatim, for the trace viewer

    @property
    def is_grounded(self) -> bool:
        return self.kind is not EvidenceKind.REASONING

    @property
    def span(self) -> tuple[int, int] | None:
        """Parse a `file.py:L41-L47` / `file.py:41` style ref into a 1-based inclusive span.

        Used by conflict detector 2 (docs/04-arbitration.md) to test whether two critics are
        pointing at the same lines. Returns None when the ref carries no line information.
        """
        match = re.search(r":L?(\d+)(?:\s*-\s*L?(\d+))?\s*$", self.ref)
        if match is None:
            return None
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        return (start, end) if start <= end else (end, start)


def canonical_issue_id(dimension: Dimension, rule: str, ref: str) -> str:
    """Derive a stable `Issue.id` from the triple that identifies the finding.

    Stability across rounds is load-bearing: conflict detector 1 recognises an irreconcilable
    trade-off by seeing the *same* id reappear after being fixed, and `dismissed` ids must stay
    dismissed for the rest of the run.

    10 hex digits, not 4. The short form reads better in a demo, but docs/04-arbitration.md
    § Failure-mode checklist calls out id collision across rounds as a real bug, and a 16-bit
    space is too small to dismiss it.
    """
    digest = hashlib.sha1(f"{dimension.value}|{rule}|{ref}".encode()).hexdigest()[:10]
    return f"{DIMENSION_PREFIX[dimension]}-{digest}"


class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)  # canonical_issue_id(dimension, rule, ref)
    dimension: Dimension
    severity: Severity
    title: str = Field(max_length=120)  # one line, no hedging
    explanation: str = Field(max_length=1200)
    evidence: list[Evidence] = Field(min_length=1)
    confidence: Confidence
    introduced_by_patch: bool  # regression vs. pre-existing -- drives the eval metric
    suggested_direction: str | None  # what to change, NOT a diff. The Coder owns diffs.

    @property
    def grounded(self) -> bool:
        return any(e.is_grounded for e in self.evidence)

    @property
    def score(self) -> float:
        """Policy weight. Ungrounded issues are halved, never zeroed.

        Halving rather than zeroing is deliberate: an ungrounded HIGH at confidence 1.0 still
        scores 8.0 and can block, but it cannot dominate a grounded finding.
        """
        return self.severity.weight * self.confidence * (1.0 if self.grounded else 0.5)

    @property
    def spans(self) -> list[tuple[int, int]]:
        return [s for s in (e.span for e in self.evidence) if s is not None]


CriticVerdict = Literal["block", "concerns", "clean"]


class Critique(BaseModel):
    """One critic, one round. Red-team and Profiler emit the same shape."""

    model_config = ConfigDict(extra="forbid")

    dimension: Dimension
    round: int = Field(ge=1)
    verdict: CriticVerdict
    issues: list[Issue]
    positive_notes: list[str] = Field(max_length=3, default_factory=list)
    tools_consulted: list[str] = Field(min_length=1)  # grounding tools actually seen
    summary: str = Field(max_length=500)

    @model_validator(mode="after")
    def _verdict_is_self_consistent(self) -> Critique:
        """Two calibration guards that live in the schema rather than in a prompt.

        A critic that blocks on a LOW is mis-calibrated, and a critic that reports `clean`
        alongside issues is incoherent. Both fail loudly into a repair retry.
        """
        if self.verdict == "block" and not any(i.severity is Severity.HIGH for i in self.issues):
            raise ValueError("verdict 'block' requires at least one HIGH-severity issue")
        if self.verdict == "clean" and self.issues:
            raise ValueError(
                f"verdict 'clean' requires an empty issue list, got {len(self.issues)}"
            )
        if self.verdict == "concerns" and not self.issues:
            raise ValueError("verdict 'concerns' requires at least one issue")
        return self

    @property
    def open_issue_ids(self) -> list[str]:
        return [i.id for i in self.issues]


class UnaddressedIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_id: str = Field(min_length=1)
    reason: str = Field(max_length=300)


class SearchReplaceEdit(BaseModel):
    """One anchored edit: find `search` exactly once, put `replace` there instead."""

    model_config = ConfigDict(extra="forbid")

    search: str  # verbatim from the source, including indentation, unique in the file
    replace: str  # may be empty, to delete


class PatchProposal(BaseModel):
    """The Coder's wire format, before `patch.py` normalises it.

    docs/02-contracts.md declares the Coder's output as `Patch` (a unified diff), while
    docs/03-agents.md makes search/replace blocks the *recommended primary* format because
    models miscount line numbers. Both are right about different layers, so the split is
    explicit: the model emits a `PatchProposal` in whichever format it can get right, and
    `patch.synthesise_patch` renders the canonical unified diff. Everything downstream -- the
    trace, the viewer, the oscillation hash -- only ever sees `Patch`.

    Exactly one of `edits` / `diff` is populated. Both empty is legal and means "nothing to
    fix", which policy handles as Coder pushback rather than as a malformed patch.
    """

    model_config = ConfigDict(extra="forbid")

    round: int = Field(ge=1)
    edits: list[SearchReplaceEdit]  # preferred
    diff: str  # fallback: a unified diff, used only when `edits` is empty
    rationale: str = Field(max_length=1000)
    addresses: list[str]
    deliberately_unaddressed: list[UnaddressedIssue]

    @model_validator(mode="after")
    def _one_format_only(self) -> PatchProposal:
        if self.edits and self.diff.strip():
            raise ValueError(
                "supply either edits or diff, not both; two representations of one change "
                "cannot be reconciled if they disagree"
            )
        return self

    @property
    def is_empty(self) -> bool:
        return not self.edits and not self.diff.strip()


class Patch(BaseModel):
    """The Coder's output, canonicalised: always a unified diff."""

    model_config = ConfigDict(extra="forbid")

    round: int = Field(ge=1)
    diff: str  # unified diff, one file. May be empty -- see policy's empty-diff handling.
    rationale: str = Field(max_length=1000)
    addresses: list[str]  # Issue.ids this patch intends to resolve
    deliberately_unaddressed: list[UnaddressedIssue]
    # Not decoration: this is the Coder's only legitimate way to push back on a false
    # positive, which is what stops it contorting the code to satisfy a bogus finding.


class Decision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    TRADEOFF = "tradeoff"
    ESCALATE = "escalate"


ConflictAxis = Literal["security_vs_performance", "clarity_vs_performance", "other"]


class Conflict(BaseModel):
    """Two grounded issues whose remedies are mutually exclusive."""

    model_config = ConfigDict(extra="forbid")

    left_issue: str = Field(min_length=1)
    right_issue: str = Field(min_length=1)
    left_remedy_cost: str  # e.g. "+2 branches, +18% p50 latency (measured)"
    right_remedy_cost: str
    axis: ConflictAxis
    detector: Literal["oscillation", "same_span"]


class Verdict(BaseModel):
    """The policy layer's output. Produced by a pure function, before any Arbiter call."""

    model_config = ConfigDict(extra="forbid")

    round: int = Field(ge=1)
    decision: Decision
    rule_fired: str = Field(min_length=1)  # names the decision-table row that produced this
    pressure: float  # sum of Issue.score over open issues
    pressure_history: list[float]
    open_issues: list[str]
    unassessed_dimensions: list[Dimension]
    conflict: Conflict | None  # set iff decision is TRADEOFF

    @model_validator(mode="after")
    def _decision_invariants(self) -> Verdict:
        if (self.decision is Decision.TRADEOFF) != (self.conflict is not None):
            raise ValueError("Verdict.conflict must be set iff decision is TRADEOFF")
        # docs/04-arbitration.md rows 5/6: unassessed is not the same as clean, and must never
        # be able to produce an accept. This is the single most likely real bug in any
        # parallel-critic design, so it is an invariant rather than a code-path discipline.
        if self.decision is Decision.ACCEPT and self.unassessed_dimensions:
            raise ValueError(
                "ACCEPT cannot coexist with unassessed dimensions: "
                f"{[d.value for d in self.unassessed_dimensions]}"
            )
        return self


class ConflictAffirmation(BaseModel):
    """The Arbiter's answer to detector 2's one question, asked BEFORE policy decides.

    docs/04-arbitration.md makes detector 2 conditional on the Arbiter classifying two
    `suggested_direction`s as genuinely opposing. That classification is an *input* to
    `policy.decide`, not a decision: the table still owns the outcome, and a `False` here
    simply falls through to row 9 or 11. Keeping it a separate, tiny schema rather than a
    field on `ArbiterNote` is what preserves the ordering -- the note is written after the
    decision, and a note cannot be an input to the thing that produces it.
    """

    model_config = ConfigDict(extra="forbid")

    left_issue: str = Field(min_length=1)
    right_issue: str = Field(min_length=1)
    #: True only when fixing either issue makes the other worse. Two issues on one line that
    #: happen to both need fixing are not a conflict.
    opposing: bool
    #: What each remedy costs, in the units docs/04 asks for ("+2 branches, p50 +18%").
    #: Ignored when `opposing` is False.
    left_remedy_cost: str = Field(max_length=300)
    right_remedy_cost: str = Field(max_length=300)
    reasoning: str = Field(max_length=600)


class ArbiterNote(BaseModel):
    """LLM prose, generated AFTER policy has decided. Never changes the decision."""

    model_config = ConfigDict(extra="forbid")

    round: int = Field(ge=1)
    decision_echo: Decision  # must equal Verdict.decision -- checked, not trusted
    consolidated_critique: str | None  # set iff REJECT. One coherent instruction set.
    priority_order: list[str]  # Issue.ids, most important first
    dismissed: list[UnaddressedIssue]  # Coder pushbacks the Arbiter agrees with
    tradeoff_justification: str | None  # set iff TRADEOFF
    recommended_default: str | None  # set iff TRADEOFF: which side to ship, and why

    @model_validator(mode="after")
    def _prose_matches_branch(self) -> ArbiterNote:
        if (self.decision_echo is Decision.REJECT) != (self.consolidated_critique is not None):
            raise ValueError("consolidated_critique must be set iff decision_echo is REJECT")
        is_tradeoff = self.decision_echo is Decision.TRADEOFF
        if is_tradeoff != (self.tradeoff_justification is not None):
            raise ValueError("tradeoff_justification must be set iff decision_echo is TRADEOFF")
        if is_tradeoff != (self.recommended_default is not None):
            raise ValueError("recommended_default must be set iff decision_echo is TRADEOFF")
        return self


# --------------------------------------------------------------------------------------------
# Grounding
# --------------------------------------------------------------------------------------------


class GroundingFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)  # finding_id(tool, rule, file, line)
    tool: str  # "bandit" | "ruff" | "radon" | "astgate" | "pytest" | "perf"
    rule: str  # "B602", "S608", "C901", ...
    file: str
    line: int | None
    end_line: int | None
    message: str
    tool_severity: str | None  # the tool's own rating, NOT our Severity enum
    raw: dict  # verbatim tool JSON, for the trace

    @property
    def span(self) -> tuple[int, int] | None:
        if self.line is None:
            return None
        return (self.line, self.end_line if self.end_line is not None else self.line)


def finding_id(tool: str, rule: str, file: str, line: int | None) -> str:
    """sha1(tool|rule|file|line)[:10]. Stable across runs for the same finding."""
    return hashlib.sha1(f"{tool}|{rule}|{file}|{line}".encode()).hexdigest()[:10]


PerfVerdict = Literal["faster", "slower", "inconclusive", "unmeasurable"]


class PerfMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str  # "timeit:parse_records:n=10000"
    before_ns: int | None
    after_ns: int | None
    repeats: int = Field(ge=0)
    stdev_ns: int | None
    verdict: PerfVerdict
    # "unmeasurable" exists because most snippets have no runnable benchmark; the Profiler
    # must be able to say so rather than inventing a number.

    @property
    def is_citable(self) -> bool:
        """An inconclusive or unmeasurable result is not evidence.

        docs/09-roadmap.md Phase 2 makes "refuses to cite an inconclusive measurement" an
        acceptance criterion, so the predicate lives here rather than in the prompt.
        """
        return self.verdict in ("faster", "slower")


class TestResult(BaseModel):
    """Outcome of the user-supplied pytest oracle. The correctness signal policy row 2 needs."""

    model_config = ConfigDict(extra="forbid")

    #: Tells pytest not to try collecting this as a test class. Without it, every test module
    #: that imports the name emits a PytestCollectionWarning.
    __test__ = False

    ran: bool  # False when execution was not permitted or the sandbox refused
    exit_code: int | None
    passed: int
    failed: int
    errors: int
    skipped: int
    failed_node_ids: list[str]
    duration_ms: int | None
    timed_out: bool
    unavailable_reason: str | None  # set iff ran is False

    @model_validator(mode="after")
    def _reason_iff_not_ran(self) -> TestResult:
        if self.ran == (self.unavailable_reason is not None):
            raise ValueError("unavailable_reason must be set iff ran is False")
        return self

    @property
    def all_passed(self) -> bool:
        return self.ran and self.failed == 0 and self.errors == 0 and not self.timed_out


class GroundingReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: Literal["original", "patched"]
    round: int | None  # None for the baseline
    findings: list[GroundingFinding]
    measurements: list[PerfMeasurement]
    tests: TestResult | None
    tool_errors: dict[str, str]  # tool name -> error, for tools that crashed
    #: Every tool that actually executed, whether or not it reported anything.
    #:
    #: Derived from the findings it would be wrong: a tool that ran and found nothing would be
    #: indistinguishable from a tool that never ran. That is the same confusion the policy
    #: layer refuses to make about critics -- `unassessed` is not `clean` -- and "bandit ran
    #: and found nothing" is the most useful thing that can be said about a clean file.
    tools_run: list[str]

    @model_validator(mode="after")
    def _round_matches_target(self) -> GroundingReport:
        if (self.target == "original") != (self.round is None):
            raise ValueError("round must be None for the 'original' baseline, and set otherwise")
        return self

    def by_tool(self, tool: str) -> list[GroundingFinding]:
        return [f for f in self.findings if f.tool == tool]

    def finding(self, finding_id_: str) -> GroundingFinding | None:
        return next((f for f in self.findings if f.id == finding_id_), None)

    @property
    def tools_succeeded(self) -> list[str]:
        """Tools that ran without crashing. These are the ones a critic may cite."""
        return sorted(set(self.tools_run) - set(self.tool_errors))

    def tools_reporting_nothing(self, among: set[str]) -> list[str]:
        """Tools that ran, did not crash, and found nothing in the given dimension."""
        produced = {f.tool for f in self.findings}
        return sorted((set(self.tools_succeeded) & among) - produced)


# --------------------------------------------------------------------------------------------
# Sandbox and patch validation
# --------------------------------------------------------------------------------------------


class SandboxResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: list[str]
    exit_code: int | None  # None iff killed before reporting (timeout, signal)
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    truncated: bool  # output exceeded the capture cap
    refused_reason: str | None  # set when the static gate refused to execute at all

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.refused_reason is None


class PatchValidation(BaseModel):
    """VALIDATE's deterministic gate result. No LLM, no critics."""

    model_config = ConfigDict(extra="forbid")

    applied: bool
    parse_ok: bool
    hunks: int
    diff_sha256: str
    patched_source: str | None  # None iff not applied
    failure_reason: str | None  # precise and mechanical: "hunk 2 failed to apply at line 47"

    @model_validator(mode="after")
    def _failure_reason_accounts_for_every_bounce(self) -> PatchValidation:
        """A bounce always carries a reason; a clean pass may still carry one.

        The asymmetry is deliberate. The churn guard (docs/03-agents.md § Coder, mitigation 4)
        bounces a patch that applied *and* parsed, because touching 30 hunks to fix one issue
        is a rewrite, not a fix. So `failure_reason` means "why VALIDATE bounced it", not
        "the mechanics failed".
        """
        if not (self.applied and self.parse_ok) and self.failure_reason is None:
            raise ValueError("failure_reason must be set when the patch did not apply and parse")
        if self.applied != (self.patched_source is not None):
            raise ValueError("patched_source must be set iff applied")
        return self

    @property
    def ok(self) -> bool:
        return self.applied and self.parse_ok and self.failure_reason is None


# --------------------------------------------------------------------------------------------
# Trace
# --------------------------------------------------------------------------------------------


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(ge=0.0)  # computed at write time from the pinned price table
    request_id: str | None  # Anthropic request-id; unrecoverable after the fact, so log it


TraceEventKind = Literal[
    "run_start",
    "state_enter",
    "state_exit",
    "llm_request",
    "llm_response",
    "tool_run",
    "patch_validate",
    "policy_decision",
    "budget_check",
    "error",
    "run_end",
]


class TraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=0)  # monotonic, gap-free. A gap means events were lost.
    ts: str  # RFC 3339, UTC
    run_id: str
    round: int | None  # None outside the loop
    kind: TraceEventKind
    actor: str  # "coder" | "redteam" | "policy" | "orchestrator" | ...
    payload: dict
    usage: Usage | None
    duration_ms: int | None


# --------------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------------


TerminalState = Literal["DONE", "FAILED"]


class RoundSummary(BaseModel):
    """One round, as the Postmortem describes it."""

    model_config = ConfigDict(extra="forbid")

    round: int = Field(ge=1)
    what_changed: str = Field(min_length=1, max_length=600)
    #: What the round concluded, in words. The `rule_fired` string is already in the trace;
    #: this is the sentence that makes it mean something to a reader.
    outcome: str = Field(min_length=1, max_length=400)


class PostmortemNote(BaseModel):
    """The Postmortem's content. The *markdown* is rendered from it, not emitted by it.

    docs/03-agents.md § 3.5 asks for "a `Report` (markdown body + structured fields)" whose
    "final section is always **what I would not trust**". Asking a model to remember a
    structural rule on every run is how the rule eventually gets forgotten, so the model
    supplies the content and `trace/report.render_narrative` lays it out. The guarantee then
    holds by construction, and the write-up reads the same on every run -- which matters
    because a reader learns where to look.

    Structured fields it does NOT carry: the outcome, the rule fired, the accepted diff, the
    issue fates, the costs. Those are derived from the trace by `trace/report.build` and would
    be a second, disagreeing source of truth here. `outcome_echo` is the exception, and only
    so a narrative that drifted from the decision can be rejected.
    """

    model_config = ConfigDict(extra="forbid")

    outcome_echo: Decision  # must equal Report.outcome -- checked, not trusted
    #: One sentence on what was actually wrong with the input file.
    headline: str = Field(min_length=1, max_length=400)
    rounds: list[RoundSummary]
    #: Where the critics disagreed and how it resolved. None when they did not.
    disagreement: str | None = Field(default=None, max_length=1000)
    #: docs/03-agents.md § 3.5: "A review tool that never expresses uncertainty is worse than
    #: no review tool." Non-empty is a schema rule, and `validation.py` additionally checks it
    #: accounts for the open issues and unassessed dimensions the trace actually recorded.
    what_i_would_not_trust: list[str] = Field(min_length=1)
    human_should_check: list[str]


class JudgeVerdict(BaseModel):
    """The eval judge's answer about ONE reported issue, on ONE case.

    docs/07-evaluation.md § LLM-as-judge. Needed for exactly two things: M1's description
    fallback, when no `rule` or `line_range` locator matched, and M2, which asks whether a
    newly reported issue is real and was introduced by the patch. Everywhere mechanical
    checking is possible the judge is not consulted at all -- docs/07 is explicit that using
    it more widely caps the eval's resolution at the judge's own reliability.

    It never learns which arm produced the issue. A judge that knows which column is "the
    tribunal" will flatter it, and there is no way to detect that after the fact from the
    numbers, which is why the blinding is structural rather than a prompt instruction.
    """

    model_config = ConfigDict(extra="forbid")

    #: A `meta.yaml` known-issue key, or None for "this is not one of the declared issues".
    #: None is the common answer and is not a failure: a critic finding something true that
    #: the case did not declare is the novel-issue behaviour the project claims.
    matched_known_issue: str | None
    #: Whether the issue is a real defect at all, independent of whether the case declared
    #: it. This is what separates a novel finding from a false positive.
    is_real_issue: bool
    #: Whether the *patch* introduced it. M2. Only meaningful when `is_real_issue`.
    is_regression: bool
    reasoning: str = Field(min_length=1, max_length=500)
    confidence: Confidence

    @model_validator(mode="after")
    def _regression_implies_real(self) -> JudgeVerdict:
        if self.is_regression and not self.is_real_issue:
            raise ValueError(
                "is_regression without is_real_issue: a patch cannot introduce a problem "
                "that is not a problem. Decide which one is wrong."
            )
        if self.matched_known_issue is not None and not self.is_real_issue:
            raise ValueError(
                "matched_known_issue with is_real_issue=False: the case declares that issue "
                "is present, so matching it and calling it unreal is a contradiction."
            )
        return self


class ReportIssue(BaseModel):
    """An issue as it stood at the end of the run, with its fate."""

    model_config = ConfigDict(extra="forbid")

    issue: Issue
    first_seen_round: int
    status: Literal["fixed", "open", "accepted_tradeoff", "dismissed"]


class Report(BaseModel):
    """The run's structured output. Derived from the trace, so `replay` reproduces it exactly."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    schema_version: str = SCHEMA_VERSION
    input_file: str
    input_sha256: str
    outcome: Decision
    rule_fired: str
    #: `FAILED` means the run produced nothing reviewable (docs/01 § States). Distinct from
    #: the `Decision`, which is `escalate` for both an unusable input and a budget breach --
    #: so without this field exit codes 3 and 4 were unreachable. docs/13 § 55.
    terminal_state: TerminalState = "DONE"
    rounds_used: int
    accepted_diff: str | None  # None iff no patch was accepted
    no_patch_reason: str | None  # set iff accepted_diff is None
    issues: list[ReportIssue]
    conflict: Conflict | None
    #: The Arbiter's prose for a TRADEOFF, lifted from the trace. `Conflict` names *which* two
    #: issues collide; docs/04 § What TRADEOFF actually emits wants the sentence that says
    #: which side to ship and what would reverse that, and there is nowhere else to put it.
    #: None on a templated run, which is why it is optional rather than required on TRADEOFF.
    tradeoff_justification: str | None = None
    recommended_default: str | None = None
    #: The Postmortem's write-up, rendered from its `PostmortemNote` in the trace. None on a
    #: run with no Postmortem seated, and on a FAILED run, which never reaches that state.
    narrative: str | None = None
    pressure_history: list[float]
    unassessed_dimensions: list[Dimension]
    total_cost_usd: float
    total_tokens: int
    wall_seconds: float

    @model_validator(mode="after")
    def _reason_iff_no_patch(self) -> Report:
        if (self.accepted_diff is None) != (self.no_patch_reason is not None):
            raise ValueError("no_patch_reason must be set iff accepted_diff is None")
        return self
