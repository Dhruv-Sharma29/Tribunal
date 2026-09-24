# 02 — Contracts

Everything the agents exchange is a validated Pydantic model. Nothing crosses an agent boundary as
free text except prose fields explicitly marked as such.

These schemas are the highest-leverage part of the design: the moment `Issue` has a fixed shape with
a required `evidence` field, "ground your critiques" stops being a prompt instruction and becomes a
validation error.

## Design rules

1. **Critics share one output shape.** Red-team and Profiler both emit `Critique`. The policy layer
   therefore treats them uniformly and adding a third critic dimension later costs one enum value,
   not a new code path.
2. **Every `Issue` MUST carry `evidence`.** Either a grounding-tool finding id, a line span in the
   patched file, or a reproduction command. An issue with `evidence.kind == "reasoning"` is legal
   but is flagged `ungrounded` and down-weighted by policy.
3. **Confidence is a decimal in `[0, 1]`, quantised to 0.05.** Free-running floats invite false
   precision and make cassette hashes noisy.
4. **Severity is an enum with fixed weights**, defined once, in code. Never re-derived in a prompt.
5. **No schema field is optional unless absence is semantically distinct from a default.** `null`
   must always mean something.
6. **Schemas are versioned.** `schema_version` on the trace header. A change that breaks old traces
   bumps it; the viewer refuses unknown majors rather than rendering garbage.

## Core models

```python
# src/tribunal/contracts.py  (illustrative — authoritative version lives in code)
from __future__ import annotations
from enum import Enum
from typing import Annotated, Literal
from pydantic import BaseModel, Field, ConfigDict

Confidence = Annotated[float, Field(ge=0.0, le=1.0, multiple_of=0.05)]


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def weight(self) -> int:              # the ONLY place these numbers exist
        return {"info": 0, "low": 1, "medium": 4, "high": 16}[self.value]


class Dimension(str, Enum):
    SECURITY = "security"
    PERFORMANCE = "performance"
    CORRECTNESS = "correctness"           # emitted by VALIDATE / pytest, not by a critic


class EvidenceKind(str, Enum):
    TOOL_FINDING = "tool_finding"         # cites a GroundingFinding.id
    MEASUREMENT = "measurement"           # cites a PerfMeasurement
    TEST_FAILURE = "test_failure"         # cites a pytest node id
    CODE_SPAN = "code_span"               # cites lines in the patched file
    REASONING = "reasoning"               # ungrounded — allowed, penalised


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: EvidenceKind
    ref: str                              # finding id / node id / "patched.py:L41-L47"
    excerpt: str = Field(max_length=600)  # verbatim, for the trace viewer

    @property
    def is_grounded(self) -> bool:
        return self.kind is not EvidenceKind.REASONING
```

### `Issue` — the atom

```python
class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str                               # stable: sha1(dimension|rule|ref)[:10]
    dimension: Dimension
    severity: Severity
    title: str = Field(max_length=120)    # one line, no hedging
    explanation: str = Field(max_length=1200)
    evidence: list[Evidence] = Field(min_length=1)
    confidence: Confidence
    introduced_by_patch: bool             # regression vs. pre-existing — drives metrics
    suggested_direction: str | None        # what to change, NOT a diff. Coder owns diffs.

    @property
    def grounded(self) -> bool:
        return any(e.is_grounded for e in self.evidence)

    @property
    def score(self) -> float:
        """Policy weight. Ungrounded issues are halved, never zeroed."""
        return self.severity.weight * self.confidence * (1.0 if self.grounded else 0.5)
```

`introduced_by_patch` is the field that makes the regression metric in
[07](07-evaluation.md) computable. Without it you cannot distinguish "the critic noticed a
pre-existing smell" from "the Coder broke something," and those have opposite implications.

`suggested_direction` deliberately forbids diffs. Letting critics write patches collapses the roles
and produces three competing diffs with no owner.

### `Critique` — one critic, one round

```python
class Critique(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimension: Dimension
    round: int
    verdict: Literal["block", "concerns", "clean"]
    issues: list[Issue]
    positive_notes: list[str] = Field(max_length=3, default_factory=list)
    tools_consulted: list[str]            # MUST be non-empty; names of grounding tools seen
    summary: str = Field(max_length=500)
```

Two guards live here rather than in a prompt:

- `verdict == "block"` requires at least one `HIGH` issue — a self-consistency check. A critic
  that blocks on low-severity findings is mis-calibrated and the parse fails loudly.
- `verdict == "clean"` requires `issues == []`.

Validation failures are retried once with the error message appended to the conversation, then
recorded as `errored` ([04](04-arbitration.md) § Unassessed dimensions).

### `Patch` — the Coder's output

```python
class Patch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    round: int
    diff: str                             # unified diff, one file
    rationale: str = Field(max_length=1000)
    addresses: list[str]                  # Issue.ids this patch intends to resolve
    deliberately_unaddressed: list[UnaddressedIssue]


class UnaddressedIssue(BaseModel):
    issue_id: str
    reason: str = Field(max_length=300)
```

`deliberately_unaddressed` is not decoration. It gives the Coder a legitimate way to push back —
"this issue is a false positive because the input is already validated at line 12" — which prevents
the pathological behaviour where the Coder contorts the code to satisfy a bogus finding. The Arbiter
adjudicates these explicitly.

### `Verdict` — the policy layer's output

```python
class Decision(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    TRADEOFF = "tradeoff"
    ESCALATE = "escalate"


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    round: int
    decision: Decision
    rule_fired: str                       # e.g. "hard_block_high_security"
    pressure: float                       # Σ Issue.score over open issues
    pressure_history: list[float]
    open_issues: list[str]
    unassessed_dimensions: list[Dimension]
    conflict: Conflict | None              # set iff decision is TRADEOFF
```

`rule_fired` is the single most useful field in the whole system for debugging and for demoing: it
names the exact decision-table row that produced the outcome. Every terminal state in a trace can
be explained by one string.

### `Conflict` and `ArbiterNote`

```python
class Conflict(BaseModel):
    """Two grounded issues whose remedies are mutually exclusive."""
    left_issue: str
    right_issue: str
    left_remedy_cost: str                 # e.g. "+2 branches, +18% p50 latency (measured)"
    right_remedy_cost: str
    axis: Literal["security_vs_performance", "clarity_vs_performance", "other"]


class ArbiterNote(BaseModel):
    """LLM prose. Generated AFTER policy has decided. Never changes the decision."""
    model_config = ConfigDict(extra="forbid")
    round: int
    decision_echo: Decision               # must equal Verdict.decision — checked, not trusted
    consolidated_critique: str | None      # set iff REJECT. Single coherent instruction set.
    priority_order: list[str]              # Issue.ids, most important first
    dismissed: list[UnaddressedIssue]      # Coder pushbacks the Arbiter agrees with
    tradeoff_justification: str | None     # set iff TRADEOFF
    recommended_default: str | None        # set iff TRADEOFF: which side to ship, and why
```

`decision_echo` is validated against the policy's decision. If the Arbiter writes prose for the
wrong branch, that is a bug we want to see, not paper over.

The `consolidated_critique` requirement — one synthesised instruction set, not concatenated critic
output — matters because contradictory raw critiques cause the Coder to oscillate. Round 2 patches
that undo round 1 patches are the classic symptom, and the oscillation guard in
[04](04-arbitration.md) catches it but synthesis is what prevents it.

## Grounding models

```python
class GroundingFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str                               # sha1(tool|rule|file|line)[:10]
    tool: str                             # "bandit" | "ruff" | "radon" | "pytest" | "perf"
    rule: str                             # "B602", "S608", "C901", …
    file: str
    line: int | None
    end_line: int | None
    message: str
    tool_severity: str | None              # tool's own rating, NOT our Severity enum
    raw: dict                             # verbatim tool JSON, for the trace


class PerfMeasurement(BaseModel):
    label: str                            # "timeit:parse_records:n=10000"
    before_ns: int | None
    after_ns: int | None
    repeats: int
    stdev_ns: int | None
    verdict: Literal["faster", "slower", "inconclusive", "unmeasurable"]


class GroundingReport(BaseModel):
    target: Literal["original", "patched"]
    round: int | None                      # None for the baseline
    findings: list[GroundingFinding]
    measurements: list[PerfMeasurement]
    tests: TestResult | None
    tool_errors: dict[str, str]            # tool name → error, for tools that crashed
```

`tool_severity` is kept separate from our `Severity` on purpose. Mapping `bandit`'s HIGH straight
onto our HIGH would make the critic redundant — the whole point is that the agent *re-rates*
severity in context (is the input actually attacker-controlled?). Keeping both lets the eval measure
how often the agent's rating diverges from the tool's, which is a direct measurement of whether the
agent is adding value over the linter. **That number is the answer to "isn't this just a linter
wrapper?"** — collect it from day one.

`verdict: "unmeasurable"` exists because most snippets have no runnable benchmark; the Profiler must
be able to say so rather than inventing a number.

## Trace event schema

JSONL, one object per line, append-only. See [06](06-observability.md) for usage.

```python
class TraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: int                              # monotonic, gap-free
    ts: str                               # RFC 3339, UTC
    run_id: str
    round: int | None
    kind: Literal[
        "run_start", "state_enter", "state_exit",
        "llm_request", "llm_response",
        "tool_run", "patch_validate",
        "policy_decision", "budget_check",
        "error", "run_end",
    ]
    actor: str                            # "coder" | "redteam" | "policy" | "orchestrator" | …
    payload: dict                         # kind-specific, schema-checked per kind
    usage: Usage | None
    duration_ms: int | None


class Usage(BaseModel):
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: float                       # computed at write time from the price table
    request_id: str | None                # Anthropic request-id, for support escalation
```

The first line of every trace file is a header event (`kind: "run_start"`) carrying
`schema_version`, the config snapshot, model ids, prompt versions, and the input file hash. That
header is what makes a trace self-describing and a replay honest — you can see whether the run you
are looking at used the same prompts as the run you are comparing it to.

## Getting the model to honour these schemas

Use structured outputs, not prompt-and-pray. With the Anthropic Python SDK:

```python
resp = await client.messages.parse(
    model="claude-opus-5",
    max_tokens=16000,
    thinking={"type": "adaptive"},
    output_config={"effort": "high"},
    system=[{"type": "text", "text": REDTEAM_SYSTEM,
             "cache_control": {"type": "ephemeral"}}],   # stable prefix → cache it
    messages=[{"role": "user", "content": critique_request}],
    output_format=Critique,                              # Pydantic model
)
critique: Critique = resp.parsed_output
```

Notes that will save debugging time:

- `messages.parse()` with `output_format=<PydanticModel>` returns a validated instance on
  `.parsed_output`. The raw-schema equivalent is
  `output_config={"format": {"type": "json_schema", "schema": {...}}}` on `messages.create()`;
  the old top-level `output_format=` kwarg on `create()` is deprecated.
- JSON-schema mode needs `additionalProperties: false` and a complete `required` list — which
  `extra="forbid"` plus non-optional fields gives you for free. Keep `ConfigDict(extra="forbid")`
  on every model.
- Assistant **prefill is rejected** on current models, so the old "start the reply with `{`" trick
  is gone. Structured outputs replace it.
- Structured output format is **incompatible with citations**; irrelevant here but worth knowing.
- Nested `Enum` + `Annotated` constraints generate valid schemas, but deeply recursive models do
  not. Keep the tree shallow — it already is.
- Parse tool inputs with `json.loads`, never string matching; escaping varies by model.

## Schema evolution

Round-tripping matters for the eval: a mid-project schema change invalidates recorded cassettes and
old traces. Policy:

- Additive optional fields → no version bump, old traces still render.
- Renamed/removed/retyped fields → bump `schema_version` major, and write a one-function migration
  in `trace/reader.py`. Do not silently coerce.
- `Severity.weight` changes → **not** a schema change but it *does* invalidate every published eval
  number. Re-run the sweep and say so in the results table. Freeze these weights before Phase 6.
