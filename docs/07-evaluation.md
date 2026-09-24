# 07 — Evaluation

Most portfolio multi-agent projects have zero evaluation. Any rigorous eval story puts this ahead of
nearly all of them — and a *small honest* eval beats a large impressive-looking one, because the
thing being demonstrated is judgement, not the score.

## What question the eval answers

Not "is the tribunal good." Specifically:

> **Does adversarial critique with grounded evidence catch issues that a single grounded LLM call
> misses, and at what cost in false positives, rounds, and dollars?**

That framing forces the right baselines. The interesting comparison is not tribunal-vs-naive-LLM (which
the tribunal wins trivially because it has linters); it is tribunal-vs-*same-tools-single-call*, which
isolates the contribution of the debate itself from the contribution of the grounding.

## Baselines — four, not one

| ID | Baseline | Isolates |
|---|---|---|
| `B0` | Single Opus 5 call: "fix the bug in this file" | The floor |
| `B1` | Single call + grounding-tool output in the prompt | **Does the debate add anything beyond the linters?** ← the honest comparison |
| `B2` | Coder → single combined critic → Coder, one round, no arbiter | Does *role separation + arbitration* add anything beyond one critique pass? |
| `B3` | Full tribunal | |

`B1` is the baseline that makes the eval credible, and it is the one most likely to be
uncomfortable. If `B3` only marginally beats `B1`, that is a genuine finding and reporting it is a
much stronger signal than a suspiciously large win. The fallback claim, if the recall gap is
narrow, is the regression and trade-off metrics — `B1` has no mechanism to detect that its own fix
introduced a problem, and no mechanism to surface a conflict. Measure that explicitly (M2, M5)
rather than hoping the recall number carries the story.

## Benchmark set

**Size: 24 cases**, split 8 dev / 16 held-out. Small enough to hand-build with real known-issue
labels; large enough for the numbers to mean something at the resolution being claimed.

Be honest about resolution: with n=16, a difference of one case is ~6 points. Report counts
(`14/16`), not percentages to one decimal, and report a Wilson interval or at minimum state
"n=16, differences under ~3 cases are not distinguishable." Nothing damages an eval's credibility
faster than `87.4%` from 16 samples.

### Composition

| Category | Cases | Notes |
|---|---|---|
| Security only | 6 | injection, `shell=True`, path traversal, unsafe deserialisation, hardcoded secret, weak crypto |
| Performance only | 5 | O(n²) membership test, repeated recompilation in a loop, string concat in a loop, redundant I/O, unnecessary deep copy |
| Both, independent | 4 | one security + one performance issue, non-interacting |
| **Conflicting** | 3 | the security fix genuinely costs performance — the `TRADEOFF` path |
| **Canary: clean** | 3 | correct, idiomatic code. Any HIGH issue raised is a **false positive**. Catches inflation. |
| **Canary: seeded-bad patch** | 3 | the input already contains an obvious HIGH vuln that any critic must catch. Catches sycophancy. |

The two canary groups (6 of 24) are the sycophancy/inflation instrumentation from
[11](11-risks.md) § R1–R2 built into the benchmark rather than bolted on. A critic that scores 0/3
on clean cases or 3/3 false-positive-free but 0/3 on seeded-bad is broken in a way that a recall
number alone would hide.

### Case format

```
eval/cases/007-sql-injection-in-loop/
├── before.py          # the input
├── test_case.py       # pytest; SHOULD pass on before.py unless the bug is a test failure
├── meta.yaml
└── notes.md           # provenance: where this pattern came from, why it's realistic
```

```yaml
# meta.yaml
id: 007-sql-injection-in-loop
category: both_independent
known_issues:
  - key: sqli
    dimension: security
    expected_severity: high
    locator: {kind: rule, value: "B608"}        # machine-checkable
    description: "f-string interpolation into a SELECT inside the fetch loop"
  - key: recompile
    dimension: performance
    expected_severity: medium
    locator: {kind: line_range, value: [18, 22]}
    description: "re.compile inside the loop body"
expected_outcome: accept        # accept | tradeoff | escalate
forbidden_regressions:
  - "must not remove the retry logic"
runnable_benchmark: true
provenance: "adapted from a pattern in OWASP examples; hand-written"
split: heldout
```

`locator` is what makes M1 automatable: a known issue counts as caught if any reported `Issue`
cites a `GroundingFinding` with that rule, **or** has evidence whose line span intersects the range,
**or** the LLM judge matches its description (in that order of preference — prefer the mechanical
match, fall back to the judge).

### Provenance and contamination

Write `notes.md` for every case. Two reasons: it forces you to justify that the case is realistic
rather than a puzzle, and it records whether the case came from a public source.

State the contamination caveat plainly in the results section: **these are hand-written cases in
well-known vulnerability classes; the models have very likely seen similar code.** That is fine for
comparing *systems on identical inputs* — which is what this eval does — and not fine for claiming
absolute capability. Say it; a reviewer who spots an unacknowledged contamination risk discounts
everything else.

Keep the 16 held-out cases **unopened** during development. Tune on the 8 dev cases, then run
held-out once per milestone, and report every held-out run you did — not the best one. If you tune
against held-out results, say so and demote them to dev.

## Metrics

| ID | Metric | Definition | Automatable? |
|---|---|---|---|
| M1 | **Known-issue recall** | caught known issues / total known issues | yes, via `locator` |
| M2 | **Regression rate** | runs where the final patch introduced a new real issue (`introduced_by_patch == true`, judge-confirmed) / runs | judge |
| M3 | **False-positive rate** | HIGH/MEDIUM issues on canary-clean cases / issues reported on those cases | yes |
| M4 | **Fix correctness** | final patch applies, parses, and the case test passes | yes, fully mechanical |
| M5 | **Outcome accuracy** | terminal state matches `expected_outcome` | yes |
| M6 | Rounds-to-terminal | distribution, not mean | yes |
| M7 | Cost per run | USD, p50 and p95 | yes |
| M8 | Re-rating rate | issues whose severity differs from the tool's ([03](03-agents.md)) | yes |

**M4 is the metric to lead with in the results table**, because it's fully mechanical — no judge, no
interpretation. "14/16 patches apply, parse, and pass the test, vs 9/16 for B1" is unarguable. M1
and M2 need the judge and therefore need the judge validated. Order the table so the mechanical
metrics come first.

M5 is the metric nobody else has, because nobody else has a `TRADEOFF` state. On the 3 conflicting
cases, `B0`–`B2` structurally cannot produce the right answer — they have no representation for it.
That is not a scoring trick, it's the thesis: report it as a capability difference, not as points.

## LLM-as-judge, and validating it

Needed for M1 (description fallback) and M2 (is a newly reported issue real?).

```python
class JudgeVerdict(BaseModel):
    matched_known_issue: str | None       # meta.yaml key, or None
    is_real_issue: bool
    is_regression: bool
    reasoning: str = Field(max_length=500)
    confidence: Confidence
```

Rules that keep it honest:

- **Judge model:** `claude-opus-5` at effort `high`. Do not use a cheaper model for the judge —
  judging is harder than critiquing, and a weak judge silently caps the eval's resolution.
- **Blind to system identity.** The judge never learns whether it's scoring `B0` or `B3`. Shuffle
  and strip labels. A judge that knows which arm is "the tribunal" will flatter it.
- **One issue at a time**, with the case's `meta.yaml` and the patched code. No batch scoring —
  batching invites the judge to score relative to the other items in the batch.
- **Structured output**, schema-validated, same machinery as the agents.

**Validate the judge before trusting it.** Hand-label 30 issue/outcome pairs drawn from dev-split
runs, compute agreement (Cohen's κ), and publish it. If κ < 0.6, fix the judge rubric before
running anything on held-out. Publishing "judge agreement with my hand labels: κ = 0.74 (n = 30)"
is worth more than any headline number in the table, because it's the sentence that tells a reader
the other numbers mean something.

Where mechanical checking is possible (M3, M4, M5, M6, M7, M8), **do not use the judge at all.**
Use the judge only where it's unavoidable.

## Runner

```
tribunal eval --split dev                     # 8 cases, all four arms
tribunal eval --split heldout --arms B1,B3    # the headline run
tribunal eval --replay runs/2026-09-20/       # re-score from traces, zero API calls
tribunal eval --smoke                         # 4 cases, CI, cassette-replayed
```

Requirements:

- Cases run concurrently with a semaphore (default 4) — the wall-clock difference between serial
  and parallel on 24 cases × 4 arms is ~2 hours vs ~30 minutes.
- Every run writes a full trace. `--replay` re-scores from traces without re-running the debate,
  which means **judge-prompt iteration is free** after the first sweep. This single property is
  worth the whole replay implementation ([10](10-cost-and-limits.md)).
- Results land in `eval/results/<timestamp>/{raw.jsonl, summary.md, report.html}` and are
  committed. The results directory is the artifact; a score in a README with no run behind it is
  not evidence.
- Header every result set with model ids, prompt versions, config, `Severity.weight` values, and
  the price table version. Without that, two sweeps are not comparable.

## Results table format

Freeze this shape now so the numbers drop in:

```markdown
### Held-out results (n=16, 2026-10-18)
models: coder/critics/arbiter = claude-opus-5 (effort high/high/xhigh) · prompts coder@v4 redteam@v3 profiler@v3 arbiter@v2
judge: claude-opus-5, κ vs. hand labels = 0.74 (n=30) · severity weights info0/low1/med4/high16

| Metric                      | B0 single | B1 +tools | B2 one-critic | B3 tribunal |
|-----------------------------|-----------|-----------|---------------|---------|
| M4 patch applies+passes     |  9/16     | 11/16     | 13/16         | 14/16   |
| M1 known-issue recall       | 11/27     | 19/27     | 22/27         | 24/27   |
| M2 regressions introduced   |  5/16     |  4/16     |  2/16         |  1/16   |
| M3 false positives (canary) |    —      |  2        |  4            |  3      |
| M5 outcome accuracy         |  n/a      |  n/a      |  n/a          | 13/16   |
| M7 cost p50 / run           | $0.04     | $0.05     | $0.31         | $0.78   |
| M6 rounds p50               |  1        |  1        |  2            |  2      |

n=16; a one-case difference is ~6pp and is not significant. Cases are hand-written in
well-known vulnerability classes and likely resemble training data — see § Contamination.
```

Numbers above are **placeholders showing the shape**, not predictions. Note the row that costs
25× more — reporting that honestly, next to the recall gain, is the analysis an interviewer is
actually looking for. If `B3` costs 25× `B1` for 5 points of recall, the correct conclusion is
"use the tribunal on security-sensitive diffs, not on everything," and saying so demonstrates more
engineering judgement than the score does.

## CI integration

Full sweeps are ~$25 and ~30 minutes ([10](10-cost-and-limits.md)) — do not run them per-commit.

- **Per PR:** unit tests + `--smoke` (4 cases, cassette-replayed, zero API calls, < 60s).
- **Nightly on `main`:** dev split, live, posts the summary as a commit comment; fails on
  regression beyond a threshold in M4.
- **Per milestone, manual:** held-out sweep. Commit the results directory.

The API key lives in GitHub Actions secrets and is available only to the nightly workflow, never to
PR workflows from forks.
