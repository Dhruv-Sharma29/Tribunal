# 09 — Roadmap

## What changed from the original plan, and why

| Change | Reason |
|---|---|
| Phases 0 and 1 merged and cut to ~4 days | The throwaway two-agent script and the scoping decision are the same afternoon's work; a full week on foundations before writing real code is a week spent reading |
| **Sandbox promoted into Phase 2** | Executing model-written code is on the critical path for the Profiler, and it's a security boundary, not a polish item ([05](05-execution-sandbox.md)) |
| **Contracts promoted before agents** | Every agent, the policy, the trace, and the eval depend on the schemas. Writing them second means rewriting three things |
| **Trace moved earlier (into Phase 3)** | Replay and the eval harness both consume the trace. Building it after the debate loop means debugging the loop blind, then retrofitting |
| **Policy layer split out from the Arbiter** | Makes the decision testable, free, and reproducible ([04](04-arbitration.md)) |
| Original weeks 3–4 double-booked | Phases 3 and 4 both claimed week 4. Re-paced with explicit buffer |
| Added a **cut line** | 6–8 weeks part-time with a new-to-you API is optimistic. Decide in advance what gets dropped, rather than discovering it in week 7 |
| IDE integration reordered: MCP first | It's the cheapest option that runs the real system ([08](08-packaging.md)) |

Total realistic estimate: **7–9 weeks part-time** for Phases 0–6, with Phase 7 optional. The
original 6–8 was achievable only by under-counting the sandbox, the trace, and eval case authoring
(which is slow, manual work — budget a full day for 24 cases with real labels).

---

## Phase 0 — Foundations and scoping (≈4 days)

**Learn:** Anthropic tool use / multi-turn loops; structured outputs with `messages.parse()` and
Pydantic; orchestration patterns (sequential vs. supervisor/worker vs. debate — you want the third);
FSMs as explicit transition tables rather than nested `if`.

**Build:** a throwaway script where agent A proposes a one-line fix and agent B critiques it as
schema-validated JSON. No framework, no CLI, no orchestrator. Print the exchange.

**Status: superseded, not skipped.** The throwaway script was never written, because the
real system overtook it before there was a reason to. Each box below is closed by something
load-bearing rather than by an exercise, and is marked with what closed it — an unticked box
next to a working system is debt nobody can act on.

**Done when:**
- [x] ~~The script runs two agents~~ — the Red-team and the Profiler run in parallel through
      `LLMClient`, and *every* agent output is Pydantic-validated with a repair retry on
      failure. String parsing appears nowhere; `llm/extract.py` recovers JSON from prose and
      reports `StructureMode.EXTRACTED` so the eval can attribute a parse-retry rate.
- [x] ~~Explain why `messages.parse()` replaced the prefill trick~~ — written up in
      [14](14-providers.md) and enforced in `llm/schema.py`, which adapts one schema to four
      providers' dialects and records what each can *guarantee*
      (`native_strict` / `native_schema` / `native_json` / `extracted`) rather than assuming.
- [x] ~~`cache_read_input_tokens > 0` on the second call~~ — the mechanism is built and
      observable: `agents/base.py` splits the cacheable prefix from the volatile remainder
      and `_check_prefix_is_stable` **rejects** a system prompt containing a round number, a
      timestamp or a hex id at construction. The viewer's cost tab warns on a zero cache-read
      run. The *number* needs a live sweep — see `RUNBOOK.md`.
- [x] ~~Confirm or amend the charter's scope table~~ — confirmed, and the reasoning is in the
      README's Design decisions § 3 with the cost it turned out to carry: of 29 declared eval
      defects, 13 of the 14 with a mechanical rule locator are security, because `bandit` is a
      security linter and ruff's `PERF` rules are narrow ([13](13-implementation-notes.md)
      § 54).

---

## Phase 1 — Contracts, grounding, sandbox (≈1.5 weeks)

The unglamorous phase that everything else rests on. No agents yet.

**Build:**
- `contracts.py` — every schema in [02](02-contracts.md).
- `grounding/` — `bandit`, `ruff`, `radon` runners normalising to `GroundingFinding`.
- `sandbox.py` — subprocess isolation + rlimits; `Dockerfile.sandbox`.
- `patch.py` — search/replace-block → unified diff via `difflib`, apply, `ast.parse` check.
- `grounding/pytest_t.py`, `perf_t.py` — through the sandbox.

**Done when:**
- [x] `tests/test_sandbox.py` passes every row of the table in [05](05-execution-sandbox.md),
      including the documented `xfail` for network under `--sandbox=subprocess` — **plus a
      second `xfail` for filesystem confinement, which that table wrongly claims holds at
      Layer 2** ([13](13-implementation-notes.md) § 7).
- [x] Golden-file tests: fixture `.py` → expected normalised findings, for each tool.
      Goldens pin `(tool, rule, file, line, end_line, tool_severity)` and deliberately omit
      messages, which churn with every tool release.
- [x] `patch.py` round-trips a search/replace block into an applied, parsing file, and returns a
      precise mechanical error on a bad anchor.
- [x] `tribunal doctor` reports tool versions, and exits non-zero when one is missing — a
      missing tool silently degrades a critic from grounded to opinion.

---

## Phase 2 — Agents, independently (≈2 weeks)

Build in the order given in [03](03-agents.md) § Build-and-test order. Each agent is testable
alone; no orchestrator exists yet.

**Status: all five agents are built** — Red-team, Profiler, Coder, Arbiter and Postmortem.
The first three are live-tested against NVIDIA NIM and reproducible from cassettes with no
credential; the Arbiter and the Postmortem are tested offline against a scripted provider,
and their cassettes are **not yet recorded** — their live tests exist and skip without a
credential.

The Arbiter is two agents behind one seat, because its two jobs sit on opposite sides of the
decision: `arbiter_affirm/v1` classifies a same-span pair *before* `policy.decide` (detector 2
cannot fire without it) and `arbiter/v1` writes the note *after*. See
[13](13-implementation-notes.md) § 26. Its dismissals are restricted to issues the Coder
actually contested (§ 27) — an unconstrained `dismissed` would let it empty the pressure sum
the policy layer just decided on, which is Rule 1 bypassed through the side door.

The Postmortem emits a `PostmortemNote` rather than a `Report`: a second report-builder next
to `trace/report.build` would end criterion S6, which is true only because there is exactly
one. The note goes into the trace, `render_narrative` lays it out on the read side, and
`Report.narrative` is derived like every other field ([13](13-implementation-notes.md) § 30).
Rendering the layout is also what makes docs/03's "the final section is always *what I would
not trust*" structural rather than something a model is asked to remember.

**Done when:**
- [x] **Red-team, Profiler and Coder** run standalone from a fixture bundle
      (`tribunal critique`, `tribunal propose`). The **Arbiter** and the **Postmortem**
      run inside the loop (`tribunal run`; `--no-arbiter` and `--no-postmortem` fall back
      to the cut-line behaviour), and `tribunal postmortem <trace>` re-writes a recorded
      run's narrative for one call.
- [x] The Coder's own criterion from [03](03-agents.md) § 3.1: six fixture files with known
      bugs, asserting the patch applies, the file parses, **and the target line range is
      touched** — the third clause matters, because the first two pass a patch that rewrote an
      unrelated function.
- [x] Every agent's output validates on first attempt on the dev fixtures — 4/4 recorded runs
      at `repair_retries=0`. Tracked on `AgentRun.trace_payload()` from the first run, since it
      cannot be retrofitted. *n is far too small to claim the ≥ 90% figure; that needs the
      Phase 5 benchmark.*
- [x] The Profiler emits `unmeasurable` when no benchmark exists, and **cannot** cite an
      `inconclusive` measurement: `agents/validation.py` rejects the ref against the real
      report, which the schema alone cannot do.
- [x] Cassettes recorded into `tests/cassettes/`; the test suite makes **zero** API calls
      (`-m "not live"` is the default). **Outstanding: the Arbiter's two prompts and the
      Postmortem's have live tests but no recordings yet** — the one place this phase's own
      ordering advice ("record cassettes as you build each agent") is not yet satisfied.
- [x] `prompt_version` frontmatter on every prompt built so far, enforced at load time —
      a file whose frontmatter role disagrees with its filename is rejected rather than
      mis-stamping every trace event. The affirmation carries its own version rather than
      sharing the Arbiter's, so the eval can tell a synthesis rewording from a change in
      conflict sensitivity.

---

## Phase 3 — Policy, FSM, orchestrator, trace (≈2 weeks)

The core. Do the trace in this phase, not later.

**Build:** `policy.py` (pure), `fsm.py` (pure), `orchestrator.py`, `trace/` (events, writer,
reader), parallel critique via `asyncio.gather`, budget enforcement.

**Status: complete.** `policy.py` and `fsm.py` are pure and exhaustively tested; the
orchestrator drives them, the trace is written and read back, and `replay` rebuilds the report
from it. Every box below is closed against runs that make **zero** API calls — the provider is
a scripted double, everything else in the loop is the real thing.

**Done when:**
- [x] `tests/test_policy.py` has one test per row of the decision table in
      [04](04-arbitration.md) **plus** a no-fall-through test. Fall-through is *structurally*
      impossible: the last row's condition is unconditionally true, and a test asserts that.
- [x] `tests/test_fsm.py` property test: random critique sequences always reach a terminal state
      within `max_rounds × patch_attempts + 2`. *(criterion S1)* 300 seeds, driving the real
      policy layer rather than a stub. Bound counted in **proposals**, not state edges — one
      debate round legitimately contains several PROPOSE entries, one per re-anchoring.
- [x] Every failure-mode row in [04](04-arbitration.md) § Failure-mode checklist has a test.
- [x] A single critic failure yields `unassessed`, never `ACCEPT` — proved at the policy layer
      twice over (rows 5/6, and the `Verdict` schema refuses the combination), and now over a
      real run: the failing critic is recorded as `error`, the *surviving* critic's critique
      still reaches the trace, and the run recovers in the next round.
- [x] A full run emits a gap-free trace with `run_start`/`run_end` and a `policy_decision` per
      round. Asserted with the reader in `strict=True`, which turns a gap, a missing `run_end`,
      a misplaced header or two run ids in one file into a failure rather than a warning.
- [x] `tribunal replay` reproduces the report from a trace with zero API calls. *(criterion S6)*
      True by construction — one `report.build`, called by both paths — and asserted
      byte-identically on three different outcomes, so a second report-building code path
      cannot appear unnoticed.
- [x] At least one end-to-end run terminates in each of `ACCEPT`, `REJECT→ACCEPT`, `ESCALATE`, and
      `TRADEOFF` on hand-made inputs. *(criterion S4)* Plus `FAILED`, which the criterion does
      not name and which was emitting two different `rule_fired` strings for one run
      ([13](13-implementation-notes.md) § 24).

---

## Phase 4 — Viewer and CLI polish (≈1 week)

**Build:** single-file HTML viewer ([06](06-observability.md)), `rich` CLI progress renderer,
`Typer` surface complete, exit codes, `doctor`, Dockerfiles, docker-compose.

**Status: the viewer is done.** Five tabs, no CDN, no server, no build step; the CSS and JS
are inlined and the trace is embedded as an escaped JSON blob rather than fetched, because a
`fetch` of a sibling file is blocked under `file://`. The tests **run the viewer's own script**
against a small DOM shim in node — nothing else in the system consumes the viewer, so a
runtime error would otherwise be a blank page and a green suite
([13](13-implementation-notes.md) §§ 36–37).

**Done when:**
- [x] `tribunal view trace.jsonl` produces an HTML file that works **offline**, opened by
      double-click, with critics rendered side by side and `rule_fired` visually dominant.
      Both asserted against the rendered DOM, not against the stylesheet.
- [~] `docker run … tribunal run examples/sql_injection.py` works on a clean machine.
      *(criterion S7)* — **written, not yet verified.** `Dockerfile` (multi-stage, unprivileged,
      pinned via `constraints.txt`), `docker-compose.yml` and `scripts/verify-docker.sh` exist;
      the script builds both images and checks the separation, the pins, the mounts and the
      uid. It has **not been run**, because the machine this was built on has no docker
      daemon. Run it before claiming S7. The static half — the properties that are statements
      in the files themselves — is covered by `tests/test_packaging.py` and passes.
- [x] Exit codes verified in a shell script — `scripts/check-exit-codes.sh`, fifteen checks,
      offline against committed trace fixtures. Writing it found that **two of the five
      documented outcome codes were unreachable** ([13](13-implementation-notes.md) § 55).
- [x] Evidence excerpts expandable in the viewer — one `<details>` per issue, every
      `evidence.excerpt` one click away.
- [x] Beyond the four above, because the trace already carried them: a pressure sparkline,
      per-round diffs shown beside the previous round's, a grounding tab that marks
      uncitable measurements as such, and a cost tab that calls out a zero cache-read run.
- [x] `rich` live progress renderer for the run itself (docs/06 § CLI progress rendering).
      A `TraceWriter` subscriber, not a second code path: `-q` removes it and the run is
      unchanged. Two outputs from one state machine — a `Live` on a terminal, one line per
      step when redirected — chosen on `console.is_terminal`
      ([13](13-implementation-notes.md) §§ 42–43).

---

## Phase 5 — Evaluation (≈1.5 weeks)

**Build:** 24 cases with `meta.yaml` labels and `notes.md` provenance; the four baseline arms; the
judge with schema output; the runner with concurrency and `--replay`; the report generator.

**Done when:**
- [ ] 24 cases, 8 dev / 16 held-out, including 3 conflicting and 6 canary.
- [ ] Judge validated against 30 hand labels; κ published. **Do this before the held-out run.**
- [ ] Thresholds (`accept_threshold`, `no_progress_epsilon`, severity weights) tuned on the **dev
      split only**, then frozen and recorded.
- [ ] `--smoke` runs in CI from cassettes in under 60 seconds, zero API calls.
- [ ] One held-out sweep across all four arms; results committed to `eval/results/<ts>/`.
      *(criterion S5)*
- [ ] Nightly GitHub Actions workflow on the dev split with the key in secrets, not exposed to fork
      PRs.

---

## Phase 6 — Docs, demo, polish (≈1 week)

**Build:** README (problem, architecture diagram, `jq` one-liner output, eval table, demo GIF); a
**Design decisions** section covering the policy/Arbiter split, the disagreement handling, the
scoping rationale, and the sandbox trade-offs; `asciinema`/GIF recording; GitHub Actions badge.

**Done when:**
- [ ] README leads with the problem and the `policy_decision` `jq` output, not with a feature list.
- [ ] Design-decisions section covers all four items above, including what you'd do differently.
- [ ] Two people who've never seen the code can explain the trade-off handling after reading it.
      *(criterion S8)*
- [ ] Demo GIF shows a real run that ends in `TRADEOFF` — the accept path is the boring one.
- [ ] Eval results table present with n, caveats, and the cost row.

---

## Phase 7 — IDE integration (optional, ≈1–2 weeks)

Order: **MCP server → `AGENTS.md` → VS Code extension.** See [08](08-packaging.md). Skip entirely
if Phases 0–6 aren't polished; there is no partial credit for a half-built extension.

---

## Pacing

| Week | Primary | Secondary |
|---|---|---|
| 1 | Phase 0 (4d) → start Phase 1 | |
| 2 | Phase 1: grounding + sandbox | |
| 3 | Phase 2: grounding tests, Red-team, Profiler | start eval case authoring (background task) |
| 4 | Phase 2: Coder, patch.py, Arbiter, Postmortem | more eval cases |
| 5 | Phase 3: contracts→policy→FSM→orchestrator | |
| 6 | Phase 3: trace, replay, parallel critique, tests | |
| 7 | Phase 4: viewer + CLI + Docker | |
| 8 | Phase 5: eval runner, judge, validation | |
| 9 | Phase 5 held-out sweep → Phase 6 docs/demo | |
| +  | Phase 7 if it's earned | |

**Authoring eval cases is the one task that parallelises against coding.** It needs no code, it's
slow, and it's tempting to defer — start it in week 3 in half-hour slices.

## The cut line

Decide now, not in week 8. In order of what gets dropped first:

1. OpenTelemetry exporter → drop. The JSONL trace carries the story.
2. `--sandbox=docker` → drop to subprocess-only, with the limitation documented in the README.
3. Baselines `B0` and `B2` → keep only `B1` vs `B3`. `B1` is the comparison that matters.
4. Benchmark 24 → 16 cases (5 dev / 11 held-out), keeping **all 3 conflicting and all 6 canary**.
   Cut from the single-dimension categories; the canaries and conflicts are the instrumentation.
5. Postmortem agent → template-render the report from the trace instead. Loses the least.
6. Trade-off detector 2 (same-span) → keep only detector 1 (cross-round oscillation). Slower to
   trigger but stronger evidence.

**Never cut:** the policy layer, the trace, the `TRADEOFF` state, the canary cases, the sandbox's
Layer-1 default-off-execution. Those five are the project. Everything else is presentation.

## Hard-won ordering advice

- **Freeze `Severity.weight` and the thresholds before Phase 5.** Changing them invalidates every
  published number and you will not want to re-run a $25 sweep.
- **Record cassettes as you build each agent**, not in a batch at the end. Retro-recording means
  re-running everything and the prompts will have drifted.
- **Write `notes.md` for an eval case at the moment you write the case.** Reconstructing provenance
  three weeks later is guesswork, and guessed provenance is worse than none.
- **Stamp `prompt_version` from the very first agent.** It cannot be retrofitted onto traces that
  already exist, and without it the eval can't attribute a score change.
