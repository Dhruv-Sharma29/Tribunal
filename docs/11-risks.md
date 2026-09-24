# 11 — Risks and failure modes

Two kinds of risk here: **thesis risks** (the system works but doesn't demonstrate what it claims)
and **delivery risks** (it doesn't get finished). The first kind is more dangerous, because the
project can look complete while being hollow.

## Thesis risks

### R1 — Sycophantic critics *(high likelihood, fatal to the thesis)*

Agreement is the trained default. A critic handed a patch and asked "any problems?" will often say
no, especially in later rounds after the Coder has written a confident rationale. If both critics go
clean on round 1 every time, there is no debate — just an expensive pipeline.

**Detection:** the 3 *seeded-bad* canary cases ([07](07-evaluation.md)) contain an obvious HIGH
vulnerability that any competent critic must flag. Track per-critic yield (issues per critique,
grounded fraction) in `traces/summary.jsonl`. A critic scoring < 3/3 on seeded-bad is broken.

**Mitigation:** critics never see the Coder's confident rationale framed as justification — they see
the diff and the tool output, with the rationale presented as a *claim to check*, not context to
accept. Grounding helps most: it is much harder to overlook a finding you were handed verbatim.

### R2 — Critique inflation *(high likelihood, the mirror image)*

Over-correcting R1 produces a critic that invents issues because it was told to find them. This is
worse than sycophancy because it looks like the system working: rounds happen, pressure is high,
the demo is exciting, and every finding is noise.

**Detection:** the 3 *clean* canary cases. Any HIGH on correct, idiomatic code is a false positive.
M3 in the eval measures exactly this.

**Mitigation:** `clean` must be stated in the prompt as a legitimate and expected verdict; severity
defined by exploitability and reachability with explicit non-examples; `Issue.grounded` weighting in
`pressure` so ungrounded findings can't dominate; and the Coder's `deliberately_unaddressed`
channel plus the Arbiter's `dismissed` list, which gives false positives a route out of the loop
instead of forcing the code to be contorted to satisfy them.

**R1 and R2 are one calibration problem with two tails.** Both canary groups are needed; either
alone can be gamed by over-correcting toward the other.

### R3 — "Isn't this just a linter wrapper?" *(certain to be asked)*

If every `Issue` cites a `bandit` finding with the same severity the tool assigned, the LLM layer is
decoration.

**Mitigation, and it must be measured:** `tool_severity` is kept separate from `Issue.severity` in
the schema precisely so **re-rating rate** (M8) and **novel-issue rate** are computable. Report
both. "34% of security issues were re-rated relative to the tool's own severity, and 22% cited code
spans no tool flagged" is a direct, numeric answer to the question. Collect these from the first
Red-team run — retrofitting them means re-running everything.

### R4 — Manufactured disagreement *(medium likelihood)*

`TRADEOFF` is the headline feature, which creates pressure to emit it. A spurious trade-off ships a
bad patch with an elegant justification attached — the worst possible output.

**Mitigation:** conflict detection is deliberately conservative and requires *grounded* issues on
both sides, with detector 1 (cross-round oscillation) needing two rounds of evidence
([04](04-arbitration.md)). Falling through to `REJECT` is the default; most disagreements are not
conflicts. The 3 conflicting eval cases measure whether `TRADEOFF` fires when it should (M5), and
the 15 non-conflicting cases measure whether it fires when it shouldn't.

### R5 — Oscillation and contradictory instructions *(medium)*

Critic A's remedy breaks critic B's constraint; round 2 undoes round 1; repeat.

**Mitigation:** the Arbiter *synthesises* a single consolidated instruction set rather than
concatenating critiques — this is prevention. The oscillation guard (diff-hash repetition) is
detection, and `no_progress` is the backstop. All three exist because synthesis will sometimes fail.

### R6 — Trace as afterthought *(medium, and self-inflicted)*

The differentiator is "visible disagreement, logged transparently." If the trace is bolted on in the
last week it will be a log file, not an artifact, and the pitch collapses.

**Mitigation:** the trace is Phase 3, and replay/eval both depend on it, so it cannot be deferred
without breaking later phases. That coupling is intentional.

## Delivery risks

### R7 — Diff generation unreliability *(high likelihood, moderate impact)*

Models miscount line numbers. Left unaddressed this shows up as a high `VALIDATE` failure rate and
burns rounds.

**Mitigation:** search/replace blocks anchored on unique context strings as the primary format,
with `difflib` synthesising the real diff on our side; line-numbered input; precise mechanical
errors on failure; patch retries counted separately from debate rounds. Measure the round-1 patch
apply rate from Phase 1 — if it's below ~85%, fix the format before building anything on top.

### R8 — Eval case authoring is slower than expected *(high likelihood)*

24 cases with real labels, runnable tests, and provenance notes is a full day of unglamorous work
that is easy to defer until it's blocking.

**Mitigation:** start in week 3 in half-hour slices ([09](09-roadmap.md) § Pacing); cut line
reduces to 16 cases while keeping all canaries and conflicts.

### R9 — Scope creep into multi-file *(medium)*

"It'd be easy to support two files" is how the timeline dies. Patch validation, import graphs, and
cross-file critique context all get substantially harder.

**Mitigation:** it's an explicit non-goal in the [charter](00-charter.md) with a written rationale.
Point at it and move on.

### R10 — API spend surprise *(low, now that it's costed)*

**Mitigation:** [10](10-cost-and-limits.md) costs it up front (~$120–200 total); per-run budget caps
with `ESCALATE` on breach; cassette-backed CI making zero API calls; replay making judge and report
iteration free.

### R11 — Tool-version drift silently invalidates eval numbers *(low likelihood, high annoyance)*

`bandit` and `ruff` rule sets change between releases. An unpinned Docker image means yesterday's
numbers aren't comparable to today's.

**Mitigation:** pin tool versions in the image, print them in `doctor`, record them in the trace
header and in every eval result set.

### R12 — Phase 7 half-built *(medium)*

A partially working VS Code extension is worse than no extension: it invites a question with no
good answer.

**Mitigation:** Phase 7 is explicitly optional and ordered cheapest-first
([08](08-packaging.md)); the cut line says skip all three if Phases 0–6 aren't polished.

## Claim hygiene

Things not to say, and what to say instead. Each of these is a question an interviewer will ask,
and the honest version is the stronger answer.

| Don't claim | Do claim |
|---|---|
| "Works in 12 IDEs" | "MCP server runs the real tribunal in any MCP host; the `AGENTS.md` rules file shapes the host's own agent and does not invoke my code" |
| "Sandboxed execution" | "Network-less read-only container with dropped caps under `--sandbox=docker`; rlimits and a scrubbed env under `--sandbox=subprocess`, where network is **not** blocked" |
| "Agents reach consensus" | "Agents reach consensus, escalate, or emit an explicit trade-off — and the decision is a deterministic policy over structured critiques, not an LLM judgement" |
| "87.4% accuracy" | "14/16 patches applied and passed the case test vs. 11/16 for the tools-in-prompt baseline; n=16, a one-case difference is not significant" |
| "Production observability" | "JSONL traces with full replay; optional OTel exporter using GenAI semantic conventions" |
| "Novel multi-agent architecture" | "A debate/arbitration pattern with the decision layer deliberately kept out of the LLM — here's why that matters, and here's the failure it prevents" |

## The question to have a real answer for

> *"Why not just one call to a strong model with the linter output in the prompt?"*

That is baseline `B1`, and it exists in the eval precisely because it's the honest challenge. Have
the measured answer ready, whatever it turns out to be. If the recall gap is small, the defensible
claims are: the tribunal detects that *its own fix* introduced a problem (a single call has no mechanism
for this — M2), and it can surface an irreconcilable trade-off rather than silently picking a side
(M5). If the gap is large, say why you think so. If the tribunal doesn't win, say that too, and say what
it would take — "25× the cost for 5 points of recall means use this on security-sensitive diffs, not
on everything" is a better answer than a flattering number, and it's the one that sounds like
someone who has shipped things.
