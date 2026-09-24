# 03 — Agent specifications

Each agent is an independent, separately testable unit: a versioned prompt template, a declared
input bundle, a declared output schema, and a model/effort setting. Build and test them in this
order; none of them needs the orchestrator to exist.

## Shared base

```python
class Agent(ABC):
    role: str
    prompt_version: str                   # "redteam/v3" — stamped into the trace
    output_model: type[BaseModel]
    model: str = "claude-opus-5"
    effort: Literal["low","medium","high","xhigh","max"] = "high"

    async def run(self, bundle: InputBundle) -> BaseModel:
        ...  # render → call → parse → validate → (one repair retry) → emit trace
```

The base class owns four behaviours so no agent reimplements them:

1. **Prompt rendering** from a `.md` template with frontmatter-declared version.
2. **Cache-friendly request layout** — stable system prompt and rubric first with
   `cache_control: {"type": "ephemeral"}`, volatile per-round content last. Order is
   `tools → system → messages`; a byte change anywhere in the prefix invalidates everything after
   it, so nothing volatile (no timestamps, no run ids, no unsorted `json.dumps`) may appear in the
   prefix. Verify with `usage.cache_read_input_tokens` — if it's 0 across rounds, something is
   invalidating.
3. **One repair retry** on schema-validation failure: re-send with the validation error appended.
   Second failure → `status: "errored"`, not a crash.
4. **Usage accounting** into the trace event.

Default model everywhere is `claude-opus-5` with `thinking: {"type": "adaptive"}`. Cheaper routing
is a *measured experiment*, not a default — see [10](10-cost-and-limits.md) § Model routing.

## 3.1 Coder

| | |
|---|---|
| **Model / effort** | `claude-opus-5`, effort `high` (`xhigh` on round ≥ 2 — later rounds are the hard ones) |
| **Input** | original source; `GroundingReport(target="original")`; failing test / traceback if supplied; `ArbiterNote.consolidated_critique` + `priority_order` (round ≥ 2); list of its own previously rejected diffs |
| **Output** | `Patch` |

**Why unified diffs, not full-file rewrites.** Three reasons, and the third is the real one:
critics review a smaller surface; humans can read the change in the trace viewer; and a diff makes
*scope creep mechanically visible*. A model asked to "fix the SQL injection" will frequently
reformat the file, rename variables, and add type hints. In full-file mode that is invisible; in
diff mode it's ten extra hunks and the Arbiter can call it out. Add a policy check on
`hunks_touched` vs `addresses` count and flag gratuitous churn.

**Diff reliability is the main practical risk.** Models miscount line numbers. Mitigations, in
order of how much they help:

1. Number the input lines in the prompt (`  41| def parse(...)`). Cheap, large effect.
2. Accept a *search/replace block* format as an alternative to line-numbered unified diff, and
   synthesise the real diff with `difflib` on our side. Anchoring on unique context strings is far
   more robust than on line numbers. **Recommended primary format** — make unified diff the
   fallback, not the other way round.
3. `VALIDATE` returns a precise mechanical error and the Coder retries (2 attempts, not counted
   against debate rounds).
4. Reject patches touching more than `max_hunks` (default 8) — a signal the model is rewriting
   rather than fixing.

**Prompt shape:** role and constraints in the cached system block; the rubric for what counts as a
minimal fix; then the volatile bundle. Explicitly license pushback via
`deliberately_unaddressed` — a Coder with no way to say "that finding is wrong" will damage the
code to satisfy a false positive.

**Unit test without the orchestrator:** 6 fixture files with known bugs → assert the patch applies,
the file parses, and the target line range is touched.

## 3.2 Red-team (security)

| | |
|---|---|
| **Model / effort** | `claude-opus-5`, effort `high` |
| **Input** | patched source (line-numbered); `GroundingReport(target="patched")` — bandit + ruff security rules + test results; the baseline report for diffing; the Coder's `rationale` and `addresses` |
| **Output** | `Critique(dimension=SECURITY)` |
| **Grounding** | `bandit -f json`; `ruff` with the `S` (flake8-bandit) ruleset; `pytest` result |

**The job is re-rating, not relaying.** A linter says "B608: possible SQL injection at line 41."
That is a *fact*. The agent's contribution is the judgement the linter cannot make: is that string
actually reachable from untrusted input in this file, what is the blast radius, and is the remedy
worse than the disease. Every `Issue` must therefore cite the finding *and* say something the
finding does not.

Make this measurable, not aspirational. Two metrics, collected per run and reported in the eval:

- **Re-rating rate** — fraction of issues where `Issue.severity` differs from the mapped
  `tool_severity`. Near 0% means you built a linter wrapper with extra steps.
- **Novel-issue rate** — fraction of grounded issues whose evidence is `CODE_SPAN`/`TEST_FAILURE`
  rather than `TOOL_FINDING`, i.e. things no tool flagged.

**Guard against inflation:** the prompt must state that `clean` is a legitimate and expected verdict
on a correct patch, and the rubric must define severity in terms of exploitability and reachability
with concrete examples of what is *not* an issue at each level. Balance the canary set
([07](07-evaluation.md)) with genuinely clean patches so the metric can catch a critic that never
says clean.

**Do not let it execute anything.** The Red-team reads tool output; it never gets a shell. All
execution goes through `sandbox.py` ([05](05-execution-sandbox.md)) on the orchestrator's schedule.

## 3.3 Profiler (performance & complexity)

| | |
|---|---|
| **Model / effort** | `claude-opus-5`, effort `high` |
| **Input** | patched source; `GroundingReport(target="patched")` — radon complexity, perf measurements, test results; the baseline for before/after comparison |
| **Output** | `Critique(dimension=PERFORMANCE)` |
| **Grounding** | `radon cc`/`mi` (always available); `timeit` before/after when a benchmark exists; `cProfile` on the user's test when one is supplied |

**The measurement honesty problem.** Most single-file snippets have no runnable benchmark, and
micro-benchmarks on a loaded machine are noise. This is where a performance critic usually starts
hallucinating percentages. Rules:

- `timeit` runs only when the case supplies a `benchmark()` callable or the user's test is fast
  enough to time. Minimum 5 repeats; report `stdev`.
- If `stdev_ns > 0.15 × mean`, the measurement is `inconclusive` and MUST NOT be cited as a
  `MEASUREMENT` evidence — the prompt says so and the schema validator enforces it.
- With no runnable benchmark, `verdict: "unmeasurable"` and the critic falls back to *asymptotic*
  and *complexity* claims only, cited as `CODE_SPAN` — "this nests a `list.index()` inside a loop
  over the same list, O(n²)". Structural claims are checkable by a reader; invented percentages are
  not.
- Severity for performance is defined by *asymptotic class change* and *measured regression beyond
  noise*, never by aesthetics. "This could be a comprehension" is `INFO` at most.

**Why `radon` is the right static proxy:** cyclomatic complexity and maintainability index are
cheap, always available, and diffable against the baseline, which lets the Profiler make the one
claim that is always well-founded — "this patch increased complexity from 7 to 14 while fixing a
`LOW` issue." That is a real trade-off input for the Arbiter.

## 3.4 Arbiter

| | |
|---|---|
| **Model / effort** | `claude-opus-5`, effort `xhigh` — the hardest reasoning in the system |
| **Input** | `Verdict` from `policy.decide()` (**the decision is already made**); both `Critique`s; the `Patch`; `pressure_history`; the Coder's `deliberately_unaddressed` claims |
| **Output** | `ArbiterNote` |

The Arbiter has three jobs and none of them is deciding:

1. **On REJECT — synthesise.** Produce *one* coherent instruction set with a priority order, not
   concatenated critic output. Resolve contradictions between critics here; if they can't be
   resolved, that's a `Conflict` and policy will have said `TRADEOFF` instead.
2. **On TRADEOFF — justify.** State the axis, both costs, the recommended default, and the
   condition under which the other side wins ("prefer the validated-input version unless this runs
   in the hot path of the ingest loop, in which case ship the fast version behind an explicit
   trust boundary"). This is the highest-value output in the whole system.
3. **Adjudicate pushback.** For each `deliberately_unaddressed` claim, agree (→ `dismissed`) or
   disagree (→ keep in `priority_order`). Dismissed issues are excluded from `pressure` on the
   next round, which is how false positives stop blocking progress.

**Why the decision is upstream.** Handing the accept/reject call to an LLM re-introduces exactly
the failure the project is about: a confident Coder rationale talks the supervisor into shipping a
bad patch. Making the Arbiter a *writer* over a decision it cannot change is the single cleanest
design choice here. The `decision_echo` field catches it if the prose drifts from the decision.

## 3.5 Postmortem

| | |
|---|---|
| **Model / effort** | `claude-opus-5`, effort `medium` — summarisation, not reasoning |
| **Input** | the full trace (compacted: policy decisions, verdicts, arbiter notes, final patch — not raw prompts) |
| **Output** | `Report` (markdown body + structured fields) |

Writes the narrative: what was wrong, what each round changed, where the critics disagreed and how
it resolved, what a human should still check. Because it reads the trace rather than the
conversation, it can be re-run on an old trace for free in replay mode — useful for iterating on
report wording without re-running the debate.

Structure the report so the final section is always **"what I would not trust"** — open issues,
unassessed dimensions, inconclusive measurements. A review tool that never expresses uncertainty is
worse than no review tool.

## Prompt versioning

Every prompt is `src/tribunal/llm/prompts/<role>.md` with frontmatter:

```markdown
---
role: redteam
version: v3
model_default: claude-opus-5
effort_default: high
changed: 2026-09-15
note: "Added explicit 'clean is expected' clause after canary yield hit 100%"
---
```

`prompt_version` goes into every trace event and into the eval report header. Without this you
cannot tell whether a score moved because of a prompt change or a code change — which makes
[07](07-evaluation.md) meaningless. Stamp it from day one; it costs nothing and is impossible to
retrofit.

## Build-and-test order

1. **Grounding tools first.** They have no LLM dependency and every critic depends on their output
   shape. Golden-file tests: fixture `.py` → expected normalised `GroundingFinding` list.
2. **Red-team second.** It's the most self-contained critic and it validates the whole
   grounding→critique pattern. Three fixtures: known-vulnerable, clean, and a false-positive trap.
3. **Profiler third.** Reuses the pattern; exercises the `unmeasurable` path.
4. **Coder fourth.** Needs `patch.py` to exist first; test the diff round-trip hardest.
5. **Arbiter fifth.** Needs `policy.py` and real `Critique` fixtures — record cassettes from steps
   2–4 and feed them in.
6. **Postmortem last.** Needs a real trace file to read.

CI runs all of these against recorded cassettes (`tests/cassettes/`) so the test suite makes **zero
API calls**. Live-API tests are a separate, manually-triggered marker (`pytest -m live`).
