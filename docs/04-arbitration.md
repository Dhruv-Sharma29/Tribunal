# 04 — Arbitration, stopping conditions, and disagreement

This is the architecturally interesting part. Everything else in the system is competent
engineering; this is the bit that isn't in the other demos.

## The split

| | Policy (`policy.py`) | Arbiter agent |
|---|---|---|
| Decides accept/reject/tradeoff/escalate | **yes** | no |
| Pure function of `(critiques, patch, history, config)` | yes | no |
| Costs money | no | yes |
| Unit-testable exhaustively | yes | no |
| Explains the decision in prose | no | yes |
| Synthesises the next instruction set | no | yes |

`policy.decide(...) -> Verdict` is called first. Its `Verdict.decision` selects which prose branch
the Arbiter writes, and `ArbiterNote.decision_echo` is validated against it.

## Pressure: the scalar the whole loop turns on

```python
def pressure(critiques: list[Critique], dismissed: set[str]) -> float:
    return sum(
        issue.score                        # severity.weight × confidence × grounding factor
        for c in critiques
        for issue in c.issues
        if issue.id not in dismissed
    )
```

One number, computed the same way every round, recorded in `pressure_history`. It is what makes
"are we making progress?" answerable instead of vibes. Three properties matter:

- **Monotone in severity** — a HIGH counts 16× a LOW, so a patch that trades one HIGH for four LOWs
  reduces pressure, which is usually correct.
- **Grounding-weighted** — ungrounded issues count half. They can still block (an ungrounded HIGH at
  confidence 1.0 scores 8.0, well above the accept threshold) but they cannot dominate.
- **Dismissal-aware** — issues the Arbiter agreed were false positives leave the sum permanently.

## The decision table

Evaluated top to bottom; **first match wins**; the matched row's name goes into
`Verdict.rule_fired`. This table is the specification — `tests/test_policy.py` has one test per row
plus a test asserting no input falls through.

| # | Rule name | Condition | Decision |
|---|---|---|---|
| 1 | `input_unusable` | original file doesn't parse, or no diff ever applied after retries | `ESCALATE` (→ `FAILED` if nothing reviewable) |
| 2 | `correctness_regression` | user-supplied test passed on original and fails on patched | `REJECT` |
| 3 | `budget_exhausted` | tokens/USD/wall-clock cap hit | `ESCALATE` |
| 4 | `oscillation` | `sha256(normalised_diff)` seen in an earlier round | `ESCALATE` |
| 5 | `unassessed_dimension` | any critic `errored` this round **and** rounds remain | `REJECT` (re-run) |
| 6 | `unassessed_terminal` | any critic `errored` on the final round | `ESCALATE` |
| 7 | `hard_block_security` | any open `SECURITY` issue with `severity == HIGH` and `confidence ≥ 0.6` | `REJECT` |
| 8 | `irreconcilable` | a `Conflict` is detected (see below) and both sides grounded | `TRADEOFF` |
| 9 | `no_progress` | `pressure[r] > pressure[r-1] − ε` for two consecutive rounds (ε = 2.0) | `ESCALATE` |
| 10 | `rounds_exhausted` | `round == max_rounds` (default 3) and `pressure > accept_threshold` | `ESCALATE` |
| 11 | `pressure_over_threshold` | `pressure > accept_threshold` (default 4.0) | `REJECT` |
| 12 | `accept` | otherwise | `ACCEPT` |

### Notes on individual rows

**Row 2 before everything else.** Correctness dominates. A patch that fixes a security hole and
breaks the tests is not a trade-off, it's broken. Note the condition is *passed before, fails
after* — a test that was already failing (the bug we were asked to fix) failing again is a
different signal, handled as a normal `CORRECTNESS` issue.

**Row 5/6 — unassessed ≠ clean.** If the Profiler call times out, the performance dimension was not
assessed. A naive implementation sums the surviving critiques, gets low pressure, and ships. That is
a silent correctness bug in the orchestrator, and it is the single most likely real bug in any
parallel-critic system. `unassessed_dimensions` is carried on the `Verdict` and can never coexist
with `ACCEPT`.

**Row 7 — asymmetric by design.** Security HIGH auto-rejects; performance HIGH does not. Justified:
an exploitable vulnerability has unbounded downside and a wrong rejection costs one more round,
whereas a performance regression is recoverable and often intentional. Asymmetric thresholds are a
*statement of values*, and stating them explicitly in config is better engineering than pretending
the system is neutral. Config:

```toml
[policy]
max_rounds = 3
patch_attempts_per_round = 2
accept_threshold = 4.0            # pressure below this accepts
no_progress_epsilon = 2.0
hard_block = [{ dimension = "security", severity = "high", min_confidence = 0.6 }]
```

**Row 9 — `no_progress` needs two consecutive rounds.** One bad round is normal: the Coder fixes
the HIGH and the critics, now unblocked, find MEDIUMs they hadn't reached. Pressure can legitimately
rise in round 2. Requiring two consecutive non-improving rounds avoids bailing out on that.

**Row 11 vs row 12 — the accept threshold is not zero.** `accept_threshold = 4.0` accepts a patch
carrying up to one MEDIUM at full confidence, or four LOWs. A system that requires zero open issues
never terminates on real code, and pretending otherwise produces a demo that only works on toys.
Open issues at accept time are reported, not hidden — that's what the Postmortem's "what I would
not trust" section is for.

## Conflict detection (row 8)

A `TRADEOFF` is not "the critics both complained." It is a specific, detectable situation: **the
remedy for issue A causes issue B, and vice versa.** Detection is deliberately conservative — false
`TRADEOFF`s are worse than missed ones, because a spurious trade-off ships a bad patch with a nice
justification attached.

Two independent detectors, both required to be grounded:

**Detector 1 — measured oscillation across rounds.** Round *r* fixes a security issue; round *r*'s
grounding shows a complexity or measured-latency regression beyond noise; round *r+1* fixes the
regression and the security issue returns (same `Issue.id`). Two rounds of evidence that the
remedies exclude each other. Strongest signal, needs ≥ 2 rounds.

**Detector 2 — same-span opposing issues in one round.** Both critics cite overlapping line spans
(`CODE_SPAN`/`TOOL_FINDING` ranges intersect), both are grounded, both are `MEDIUM`+, and their
`suggested_direction`s are classified as opposing by the Arbiter. Available in round 1, weaker —
so it requires the Arbiter to affirm the conflict, and a non-affirmation falls through to row 9/11.

Everything else — critics complaining about unrelated things, one critic clean, ungrounded
objections — is a normal `REJECT`. Most disagreements are not conflicts.

### What TRADEOFF actually emits

The accepted patch, plus a first-class record of the unresolved objection:

```
DECISION: trade-off accepted (rule: irreconcilable, axis: security_vs_performance)

Shipping: parameterised query with input validation (issue SEC-a31f resolved)
Cost:     +2 branches, cyclomatic complexity 7 → 12; measured p50 +18% (±4%, 9 repeats)
Objection standing: PERF-77c2 (medium, confidence 0.8, grounded in timeit measurement)

Recommended default: ship the validated version.
Reverse this if: this function is in the ingest hot path (>10k calls/s), in which case
move validation to the trust boundary at the caller and keep the fast path here.
Human decision required: yes — nobody in this run knows the call volume.
```

**Why this is the best talking point in the project.** Every toy multi-agent system assumes
convergence: loop until the agents agree, and if they don't, loop harder. But security and
performance are *genuinely* in tension, and forcing consensus means one critic's concern gets
silently dropped — with no record of which one or why. Modelling "agree to disagree" as a
first-class terminal state, with a written justification and an explicit "who needs to decide
this," is the difference between a system that pretends to be an engineer and one that behaves like
a good one: it knows what it doesn't know and escalates with context.

## Stopping conditions, summarised

Five independent ways the loop terminates. Any one of them alone is insufficient.

| Guard | Catches | Row |
|---|---|---|
| Round cap | The generic runaway | 10 |
| Diminishing returns | Slow non-convergence that would burn the full budget for nothing | 9 |
| Oscillation | A→B→A patch cycling, the classic contradictory-critique symptom | 4 |
| Budget cap | Cost blowout regardless of round count (a pathological round can be 5× normal) | 3 |
| Irreconcilable | The case where continuing is *wrong*, not just expensive | 8 |

`tests/test_fsm.py` MUST include a property test: over randomly generated critique sequences, every
run reaches a terminal state within `max_rounds × patch_attempts + 2` transitions. That test is the
proof of criterion S1.

## Budget enforcement

Checked at every `state_enter`, emitting a `budget_check` trace event:

```python
@dataclass(frozen=True)
class Budget:
    max_usd: float = 2.00
    max_tokens: int = 400_000
    max_wall_seconds: int = 600
    max_rounds: int = 3
```

On breach, the FSM transitions to `ESCALATE` — never a hard abort. An exceeded budget still
produces a trace, a report, and the best patch seen so far. "Ran out of money" is a legitimate
outcome to report; losing the work is not.

## Failure-mode checklist for this layer

Things to actively test, because each has bitten someone:

- Both critics return `clean` on round 1 → must `ACCEPT` immediately, not run a pointless round 2.
- Both critics `errored` → `ESCALATE`, never `ACCEPT`.
- Coder returns an empty diff (claims nothing to fix) → treat as a patch attempt failure; if the
  pressure was above threshold, that's a `REJECT` with the Coder's reasoning recorded as pushback.
- Arbiter's `decision_echo` mismatches the policy verdict → raise, don't coerce.
- An `Issue.id` collides across rounds with different content → id derivation must include enough
  context; test it.
- `pressure_history` with a single element on round 1 → `no_progress` must not fire (index error is
  the obvious bug; the subtle one is treating a missing previous value as 0 and escalating).
