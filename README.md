# tribunal

### Adversarial code review that can decline to agree.

> A Coder proposes a patch, a Red-team and a Profiler critique it independently and in
> parallel, a deterministic policy layer accepts or rejects, and when the critics want
> incompatible things the system emits an explicit **trade-off with justification** instead
> of manufacturing consensus.

*Why the name:* a tribunal hears opposing cases and rules by **procedure**, not by the
panel's mood. That is the whole design — two critics that never see each other's work, and a
twelve-row decision table that owns the verdict. The models supply findings; they do not
decide. And like a real tribunal, this one is allowed to return *"these two cannot be
reconciled, and here is what each costs"* rather than forcing a ruling nobody believes.

**Status: the loop runs end to end, and everything through the optional IDE integration is
built.** Schemas, grounding, sandbox and patch validation are tested. The LLM client speaks
to **Anthropic, OpenAI, Gemini and NVIDIA NIM** behind one interface. The **Red-team**,
**Profiler** and **Coder** run standalone, live-verified against NVIDIA Nemotron and
replayable from cassettes with no credential. The **orchestrator** drives them through the
**deterministic decision layer** and the **state machine**, writes the **trace**, and
`tribunal replay` rebuilds the report from it byte for byte. The **Arbiter** sits on both
sides of the decision — affirming a same-span conflict before it, writing the note after —
so `TRADEOFF` can be reached from a single round and the Coder's pushback gets adjudicated.
The **Postmortem** writes the run up from the trace and cannot quietly drop an open issue
while doing it. `tribunal view` renders any trace as a **single offline HTML file**, the
tribunal ships as a **pair of container images** that keep the credential and the code execution
apart, and an **MCP server** makes the real tribunal callable from an editor. **`tribunal code`** puts the whole thing behind an interactive terminal agent that reads, edits and runs commands — and has the tribunal itself as one of its tools.

The **evaluation harness** is built: 24 hand-written cases, four arms, a scored sweep, an
LLM judge with a κ-validation worksheet, and CI that gates on all of it. What it does not
have is *numbers* — every remaining step needs an API key or a Docker daemon, and those are
listed in [`RUNBOOK.md`](RUNBOOK.md) rather than estimated here.

**1,800+ tests, zero API calls by default.** New here? **[`start.md`](start.md)** is the
guided tour: install, first run, and what the system is doing while it runs. Full plan in
[`docs/`](docs/README.md); every place the build disagreed with the plan is in
[`docs/13-implementation-notes.md`](docs/13-implementation-notes.md); everything *not* done,
and whether that was a constraint or a choice, is in
[`REMAINING.md`](REMAINING.md).

```console
$ tribunal code                            # the interactive coding agent, in this directory
$ tribunal "why does test_fetch fail?"     # same thing, seeded with a prompt
$ tribunal doctor                          # what the grounding layer can actually do here
$ tribunal providers --prices              # what each LLM backend can guarantee, and its cost
$ tribunal ground examples/sql_injection.py --test examples/test_fetch.py --allow-exec
$ tribunal critique examples/sql_injection.py --config examples/nim.toml
$ tribunal propose examples/sql_injection.py --config examples/nim.toml
$ tribunal run examples/sql_injection.py --config examples/nim.toml   # the whole loop
$ tribunal replay traces/01JB….jsonl                                  # no key, no calls
$ tribunal view traces/01JB….jsonl                                    # one HTML file
$ tribunal-mcp                                                        # stdio MCP server
```

## In a terminal

Everything above this line is a pipeline: hand it a file, get a verdict. `tribunal code` is
the other shape — a prompt, a transcript, and an agent that reads, edits, greps and runs
commands in the directory you are standing in until the work is done.

```console
$ tribunal code                                  # interactive
$ tribunal "the auth test started failing"       # not a subcommand, so: a prompt
$ tribunal code --print "add a test for parse_row"
```

Eight tools, one per step — `read`, `list`, `grep`, `glob`, `write`, `edit`, `bash`, and
**`review`**, which is the one that only exists here. `review` hands a file to the *whole
tribunal*: two independent critics, grounded in bandit/ruff/radon/astgate output, and the
twelve-row decision table. Not a second opinion from the same model that just wrote the
code — an adversarial one, with a procedure that is allowed to say the critics want
incompatible things. Its patch is reported, never applied, because `TRADEOFF` has no
sensible automatic action.

Reads run unprompted; edits and commands ask, and `always` is remembered per *program*, so
approving `pytest` once does not approve `curl` later. `--plan` refuses every mutation and
makes the agent propose instead; `--yes` asks nothing. `--print` defaults to plan mode,
because a non-interactive run has nobody to answer the question.

**What has and has not been run.** The tool loop, the workspace boundary, approval and the
whole NIM wire path are exercised end to end — against a local endpoint speaking the real
OpenAI protocol, which covers the SDK, `reasoning_effort`, the `json_schema` response format,
the `<think>` recovery and the 503 retry. What has *not* happened is a real model choosing
the steps: no live provider has ever emitted a `Step`. Whether a given model reliably fills
a flat one-tool-per-turn schema is an open question, and the session log records
`parse_retries` per step so that it is answerable the first time someone runs it rather than
a matter of impression. See [`REMAINING.md`](REMAINING.md) § A11.

**Three things it deliberately does not do.** One tool call per step rather than several in
parallel, and no streaming: both follow from building on the existing structured-output
layer instead of a second message-threaded one, which is what makes this work identically on
Anthropic, OpenAI, Gemini and a self-hosted NIM, with cassettes and cost accounting already
in place. And `bash` is **not sandboxed** — the sandbox scrubs the environment down to four
variables, which is correct for executing a model's patch of an untrusted file and useless
for a developer who wants their own `pytest` to work. The real boundaries are the workspace
root check, which is structural and cannot be approved away, and a human reading the command.
`--yes` gives the second one up.

## The two critics

Identical input, no shared channel — independence is what makes their agreement informative.

```console
$ tribunal critique examples/sql_injection.py --config examples/nim.toml
grounding sql_injection.py
  13 finding(s) from astgate, bandit, radon, ruff
critique redteam + profiler  <- parallel

redteam BLOCK     nim/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning  redteam/v1
  SEC-db281a7ea1  high  conf=0.95  score=15.2  cites tool_finding:ca35733e24
  metrics: grounded=1/1 re-rated=0/1 novel=0.0 severe=1/2 pressure=15.2

profiler CONCERNS nim/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning  profiler/v1
  PERF-…          high  conf=0.95  score=15.2  cites code_span:sql_injection.py:L17-L24
  metrics: grounded=2/2 re-rated=0/0 novel=1.0 pressure=30.4
```

**Grounding is enforced, not requested.** `Issue.evidence` having `min_length=1` makes "cite
something" a validation error; it does not make the citation *true*. Every ref is resolved
against the real `GroundingReport` — a finding id that does not exist, an `inconclusive`
measurement, a line span outside the file, or a `tools_consulted` entry that is secretly a
finding id all get rejected and repaired.

The `re-rated`, `novel` and `severe` numbers are the answer to *"isn't this just a linter
wrapper?"*, collected from the first run because they cannot be retrofitted.

## The decision layer

**No LLM decides anything.** `policy.py` is a pure function over structured critiques, written
as the twelve-row table docs/04 specifies — first match wins, and the matched row's name becomes
`Verdict.rule_fired`. The Arbiter writes the prose; it cannot change the verdict.

Driving three rounds of hand-made critiques through the real `policy.py` and `fsm.py`:

```
  r1 reject    hard_block_security      pressure=14.4
  r2 reject    pressure_over_threshold  pressure=7.2
  r3 tradeoff  irreconcilable           pressure=6.4

INIT -> GROUND -> PROPOSE -> VALIDATE -> CRITIQUE -> ARBITRATE   (x3)
     -> TRADEOFF -> POSTMORTEM -> DONE
```

That is the whole thesis, and it cost nothing to produce: no API key, no clock, no network.

`tribunal replay` reproduces the report from a trace **byte-identically**, and not by
re-running the pipeline against recorded responses and hoping nothing drifts. The orchestrator
does not build a report of its own: it writes events and calls `trace/report.build`. `replay`
reads the file and calls the same function. There is no second code path that could diverge,
which is the only kind of reproducibility claim worth making — criterion S6 by construction.

**Fall-through is structurally impossible**, not merely untested: the last row's condition is
unconditionally `True`. **Criterion S1** — a run always terminates in a defined state — is a
property test over 300 random critique sequences driving the real policy layer, bounded by
`max_rounds x patch_attempts + 2` proposals. And the transition table is a table, so an
undefined `(state, trigger)` pair raises with both names instead of silently falling through to
the last `elif`.

## The Arbiter, on both sides of the decision

Its two jobs land in different places in the round, and that is not a detail — it is what
keeps an LLM out of the decision layer while still letting one contribute to it.

```
same_span_candidates  ->  arbiter.affirms()  ->  decide()  ->  arbiter.run()
(mechanical, free)        (classification)      (the only    (prose about a decision
                                                 decision)    it cannot change)
```

**Before.** `TRADEOFF` from a single round needs somebody to say that two remedies on the same
lines genuinely exclude each other. The mechanical half — grounded, `MEDIUM`+, overlapping
spans, opposite dimensions — is a free filter, and on almost every round it returns nothing and
no call is made. Only a real candidate pair buys a classification, at `medium` effort, and the
answer is an *input* to the table rather than a decision. A `false` falls through to an
ordinary rejection, because [R4](docs/11-risks.md) makes a spurious trade-off the worst output
the system can produce.

**After.** One coherent instruction set with the critics' contradictions already resolved —
synthesis rather than concatenation, which is *prevention* for the oscillation the policy layer
only detects. `decision_echo` is checked against the real verdict, so prose that drifted is a
repair retry, not a recorded opinion.

**And the one lever it has.** The Coder can decline to fix something and say why; the Arbiter
rules on it. A dismissal is permanent and removes the issue from `pressure` for the rest of the
run — which is exactly why it is the most tightly scoped field in the system. It may only name
ids the Coder actually contested. Without that check the Arbiter cannot change the verdict but
*can* empty the sum that produced it, which is the same thing with extra steps.

`--no-arbiter` runs the templated stand-in from
[the cut line](docs/09-roadmap.md#the-cut-line) instead: cheaper, detector 2 cannot fire, and
nothing is ever dismissed. The trace records `synthesised_by` either way, so a reader can
always tell which produced a consolidation.

## The Coder

`PROPOSE -> VALIDATE` without the loop around it. A patch that does not apply is bounced straight
back with a mechanical error, on a budget counted separately from debate rounds.

```console
$ tribunal propose tests/fixtures/coder/001-shell-injection.py --config examples/nim.toml
propose nim/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning  coder/v3
  attempt 1/1 applied hunks=1 edits=1

rationale Replace shell=True subprocess call with list arguments to prevent shell injection.
addresses b68c1a40a1, a0209ee6a3
-    subprocess.run("makereport " + report_name, shell=True)
+    subprocess.run(["makereport", report_name])
```

It emits **search/replace blocks**, not diffs, and we synthesise the unified diff with `difflib`.
Anchoring on a unique context string is far more reliable than line numbers, which models
miscount — and the diff is still what everything downstream sees, so scope creep stays
mechanically visible.

**Three retry budgets, deliberately not merged:** one schema repair, two VALIDATE re-anchorings,
three debate rounds. Merging any two hides a different problem — if VALIDATE bounces counted as
schema repairs, the repair-retry rate would be dominated by line-number arithmetic and a real
prompt regression would vanish into it.

Against six fixtures with known bugs, asserting the patch applies, parses **and touches the
declared target lines**: **5 of 6** on Nemotron (v1 of the prompt scored 3). Three of the five
needed a second attempt, which is the VALIDATE loop absorbing a diff stumble at zero critic
cost. The sixth is a documented provider-side serialisation defect, not silently dropped —
see [`docs/13-implementation-notes.md`](docs/13-implementation-notes.md).

## The write-up, and what it is not allowed to leave out

The Postmortem reads the **trace**, not the conversation — the decisions, the patches and the
fate of every issue, with the prompts left out. That is what makes it re-runnable:
`tribunal postmortem traces/01JB….jsonl` rewrites an old run's narrative for one call, which
is how the report's wording gets iterated on without paying for the debate again.

It supplies content; the layout is ours. So the write-up always ends on the same section:

```markdown
## What I would not trust

- SEC-db281a7ea1 is still open: the argv fix does not cover the second call site
- performance was never assessed — the Profiler errored, which is not the same as clean
- timeit:parse_records (execution requires --allow-exec) — no benchmark ran, so the
  performance claim rests on an asymptotic argument rather than a measurement
```

**Non-empty is a schema rule. Complete is checked against the trace.** Every issue still open
at the end, and every dimension nobody assessed, must appear somewhere in the write-up, or it
is rejected and repaired. An accepted patch with an open issue is a *conditional* pass, and a
narrative that reads as an unconditional one is worse than no narrative — it is the artifact a
human actually reads, and it teaches them to stop looking.

`Report.narrative` is derived from the trace like every other field, so
[criterion S6](docs/00-charter.md) survives having an LLM in the loop: the model writes the
content, `replay` re-renders it, and the bytes match.

## The viewer

`tribunal view trace.jsonl` writes one HTML file beside the trace and opens it. No server, no
build step, no CDN: it works double-clicked from a file manager with the network unplugged,
which is how a reviewer actually opens it and how the demo GIF gets recorded.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 01JB…  sql_injection.py  TRADEOFF  1 round  $0.71  4m12s  ╱╲_ 21→7   │
│ [Timeline] [Debate] [Diffs] [Grounding] [Cost]                       │
├──────────────────────────────────────────────────────────────────────┤
│ ┌ Red-team ──────────┐  ┌ Profiler ──────────┐   ← side by side      │
│ │ CONCERNS           │  │ CONCERNS           │                       │
│ │ ▸ MED SEC-db281a7  │  │ ▸ MED PERF-1ad674b │   ← click for the     │
│ └────────────────────┘  └────────────────────┘     cited evidence    │
│ ╔══════════════════════════════════════════════════════════════════╗ │
│ ║ TRADEOFF   irreconcilable   pressure 7.2                         ║ │
│ ╚══════════════════════════════════════════════════════════════════╝ │
└──────────────────────────────────────────────────────────────────────┘
```

**The critics are columns, not a list.** That is the first requirement in
[docs/06](docs/06-observability.md) and it is the whole visual argument: a vertical stack reads
as a pipeline, and the claim being made is independence.

**The trace is untrusted input here.** It carries model-written source and diffs, and the
output is a file people email each other — so `</script>` in a diff is escaped out of the JSON
blob, and the script never assigns to `innerHTML`. The test harness enforces the second by
making `innerHTML` throw.

Nothing downstream consumes a viewer, so a runtime error in it is a blank page and a green test
suite. The tests therefore **run the viewer's script** in node against a small DOM shim and
assert on what it drew. That found three real bugs on its first run, including a sparkline that
drew its last point twice — see [docs/13](docs/13-implementation-notes.md) § 37.

## Providers

Critiques are schema-validated structures, so the difference that matters between backends is
**how strongly each can guarantee the output matches a schema** — recorded on every response and
in every cassette, because otherwise a rising parse-retry rate is unattributable.

| Provider | Default model | Guarantee | Credential |
|---|---|---|---|
| Anthropic | `claude-opus-5` | `native_strict` — decoding constrained to our schema | `ANTHROPIC_API_KEY` |
| OpenAI | `gpt-6-astra` | `native_strict` | `OPENAI_API_KEY` |
| Gemini | `gemini-3.8-flash` | `native_schema` — documented JSON Schema subset | `GEMINI_API_KEY` |
| NVIDIA NIM | `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` | `native_json` — JSON mode, no schema enforcement | `NVIDIA_API_KEY` |

An agent names a model and the provider follows, so the two cannot disagree:

```toml
[agents.profiler]
model = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
```

See [`docs/14-providers.md`](docs/14-providers.md) for the schema-dialect adaptation, the three
vendor-documentation claims that did not survive contact with the live APIs, and why
`multipleOf` is a validation guard rather than a generation guard.

## What makes this different from a sequential agent demo

1. **Critics are grounded.** Every issue must cite a `bandit` / `ruff` / `radon` / `pytest` /
   `timeit` result, a code span, or a reproduction. Ungrounded claims are flagged and down-weighted.
2. **The decision layer contains no LLM.** `policy.py` is a pure function over structured critiques
   with a documented decision table, so every outcome is reproducible, free, and unit-testable. The
   Arbiter agent writes the prose; it cannot change the verdict.
3. **Critics never see each other.** Independence within a round is what makes agreement
   informative. The only channel between them is the Arbiter's synthesis in the next round.
4. **Disagreement can be terminal.** `TRADEOFF` is a first-class end state: ship this, here's the
   standing objection, here's the recommended default, here's who needs to decide.
5. **The trace is the artifact.** JSONL, self-describing, and fully replayable — the report, the
   eval metrics, and threshold tuning all derive from it with zero API calls.

The whole thesis in three lines of `jq`:

```console
$ jq -r 'select(.kind=="policy_decision")
         | "r\(.round) \(.payload.decision) \(.payload.rule_fired) pressure=\(.payload.pressure)"' \
     traces/01JB….jsonl
r1 reject   hard_block_security      pressure=21.6
r2 reject   pressure_over_threshold  pressure=7.2
r3 tradeoff irreconcilable           pressure=5.6
```

## The whole loop

A run takes minutes, so it draws itself while it works — the same events the trace records,
rendered for a human rather than for `jq`:

```
● grounding  bandit 2 · ruff 5 · radon 1 · astgate 2                        1.2s
● round 1/3
  ├ coder      2 edit(s) · addresses SEC-db281a7ea1                        24.1s
  ├ validate   applied · parses · 1 hunk(s)                                 0.4s
  ├ critique   redteam BLOCK(1 high) │ profiler CONCERNS(1 medium)         31.8s  ⟵ parallel
  ├ policy     REJECT · hard_block_security · pressure 21.6                  0ms
  └ arbiter    consolidated · dismissed 1                                  18.9s
● round 2/3 …
```

Both critics on one line with a `parallel` marker is the concurrency claim, made visible for
free. The renderer is a subscriber on the trace writer rather than anything the orchestrator
calls — `-q` removes it and the recorded run is identical, which is what stops the terminal
and the trace from ever disagreeing about what happened.

```console
$ tribunal run examples/sql_injection.py --test examples/test_fetch.py --allow-exec \
      --config examples/nim.toml
$ tribunal run examples/sql_injection.py --no-arbiter     # templated, cheaper, no TRADEOFF
$ tribunal replay traces/01JB….jsonl --accept-threshold 8
$ tribunal postmortem traces/01JB….jsonl                  # rewrite the narrative, 1 call
$ tribunal view traces/01JB….jsonl                        # self-contained HTML
```

Exit codes: `0` accept · `1` tradeoff · `2` escalate · `3` failed · `4` budget exhausted ·
`64` usage. **`replay` exits with the code of the run it is reporting**, so a recorded run can
be re-checked in CI, and `scripts/check-exit-codes.sh` verifies all six offline.

`replay` needs no credential and makes no calls, unconditionally — which is what makes it
usable in CI. `--accept-threshold` re-runs the decision table over the *recorded* critiques and
reports what the run would have concluded instead, which is how a threshold gets tuned on
evidence rather than on intuition. `postmortem` is the one trace command that does call a
provider, which is why it is a separate command rather than a flag on `replay`.

## Running it in Docker

Two images, and the split is a security property rather than packaging tidiness: one holds the
API key and can reach the network, the other executes model-written code and has neither.
Nothing is both.

| Image | Contains | API key | Network |
|---|---|---|---|
| `tribunal` | tribunal, grounding tools, prompts | yes | yes (API only) |
| `tribunal-sandbox` | python + pytest, nothing else | **no** | **none** |

```console
$ docker compose build
$ docker compose run --rm tribunal doctor
$ docker compose run --rm tribunal run /code/examples/sql_injection.py \
      --config /code/examples/nim.toml
```

`/code` is mounted read-only — the tribunal proposes diffs, it never writes to the input — and
`/traces` is the only writable mount, so traces and rendered HTML land on the host and outlive
the container. Credentials are passed through from the environment by name; never as a build
arg (it lands in the image history) and never as a CLI flag (it lands in shell history *and*
in `run_start.payload.argv` in the trace).

**The container defaults to `--sandbox=subprocess`, on purpose.** Using `--sandbox=docker` from
inside the tribunal's own container means mounting the host docker socket, and anything that can
talk to that socket can start a privileged container mounting `/`. The tribunal in a container is
already namespaced away from the host, so that trade is a loss. The socket mount is present in
`docker-compose.yml`, commented out, with the reason next to it.

Tool versions are pinned in `constraints.txt` and printed by `doctor`, because a finding that
appeared from a rebuilt image with a newer `ruff` is indistinguishable from one the tribunal
earned — and that would quietly invalidate every published eval number.

`scripts/verify-docker.sh` builds both images and checks all of the above, including that
neither image has anything credential-shaped in its layer history.

## Executing model-written code

Code the tribunal writes is executed only with `--allow-exec`. With `--sandbox=docker` (recommended)
execution happens in a network-less, read-only container as an unprivileged user with dropped
capabilities and CPU/memory/PID limits. With `--sandbox=subprocess` the same resource limits and
a scrubbed environment apply, **but neither network access nor filesystem writes outside the
scratch directory are blocked** — use Docker if the input is untrusted. Static grounding
(`bandit`, `ruff`, `radon`) never executes the target.

## Design decisions

Five choices that shaped the rest, with what each one cost.

### 1. The decision layer contains no LLM

`policy.py` is a pure function over structured critiques: a twelve-row table, first match
wins, the matched row's name becomes `Verdict.rule_fired`. The Arbiter is an LLM and it
writes the prose — on a rejection, one coherent instruction set; on a trade-off, the
justification — but it is called *after* the decision and cannot change it.

**Why.** Handing accept/reject to a model re-introduces exactly the failure this project is
about: a confident Coder rationale talking a supervisor into shipping a bad patch. Making the
decision a pure function also makes it free, reproducible, and exhaustively testable — the
table has one test per row plus a no-fall-through test, and fall-through is *structurally*
impossible because the last row's condition is unconditionally `True`.

**What that costs, and where it leaked.** A writer with no lever is easy; a writer with one
lever is where the care goes. The Arbiter has exactly one — it can mark a Coder's pushback as
a false positive, which permanently removes that issue from the pressure sum. Unconstrained,
that is the decision re-entering through the side door: it cannot change the verdict, but it
can empty the sum that produced it and let the next round accept. So `dismissed` may only
name issues the Coder actually contested, and `decision_echo` is checked against the real
verdict rather than trusted. Both are repair retries, not warnings.

There is one place an LLM legitimately feeds the decision, and the ordering is what keeps it
honest: trade-off detector 2 asks the Arbiter whether two same-span remedies are genuinely
opposed, *before* `decide()` runs. That answer is an **input** to the table, the same way a
critique is. It is not the decision, and a `false` simply falls through to an ordinary
rejection.

### 2. Disagreement can be terminal

Every toy multi-agent system assumes convergence: loop until the agents agree, and if they
do not, loop harder. Security and performance are genuinely in tension, and forcing consensus
means one critic's concern gets silently dropped — with no record of which one or why.

So `TRADEOFF` is a first-class terminal state. Concretely, on a case where a SQL query is
built by string interpolation inside a per-request loop:

- the security fix is a parameterised query, executed per sku;
- the performance fix is one `IN`-clause round trip for the whole batch;
- a parameterised `IN` clause needs a placeholder list built from the batch length, which
  reintroduces string building into the query.

Applying either remedy makes the other harder. There is no third option that is
straightforwardly better, and the fact that decides it — whether this is really the hot path
— is not in the file. So the run ships the patch, records the standing objection with both
costs, states which side to ship by default *and the condition that reverses it*, and says a
human decides because nobody in the run knows the call volume.

**Detection is deliberately conservative**, because a spurious trade-off is the worst output
the system can produce: it ships a mediocre patch with a confident justification attached and
looks exactly like a good outcome. Two independent detectors, both requiring grounded,
`MEDIUM`+ issues. The stronger one needs two rounds of evidence — an issue returns after
being fixed, across a measured regression. The weaker one is available in round 1 and
therefore requires the Arbiter to affirm the opposition; its prompt spends most of its length
on the *non*-examples, because the first thing a model does with "are these in tension?" is
say yes. A missed trade-off costs one extra round. A false one costs the claim.

### 3. Scope follows from the grounding constraint, not from the calendar

Single-file Python, two critic dimensions, standard library plus an allowlist. Multi-file
refactors, other languages and networked code are non-goals.

This is not "what fitted in the time". Every critic claim has to cite a `bandit` / `ruff` /
`radon` / `pytest` / `timeit` result, a code span, or a reproduction — and that constraint
picks the scope for you. Cross-file dataflow has no tool in this suite that can ground it, so
a multi-file critic would be reasoning without evidence while wearing the same schema as one
that has it. The narrow version is the one where the central claim stays true.

**The honest cost, found by building the benchmark.** Of the 29 defects the 24 cases declare,
14 have a mechanical rule locator and 13 of those are security. `bandit` is a security linter
by definition and ruff's `PERF` rules do not fire on a wrong data structure, invariant work in
a loop, redundant I/O or an unnecessary `deepcopy`. So the performance dimension is almost
entirely ungrounded: its critics cite code spans, and its recall rests on line ranges and the
judge. M1 is two metrics wearing one name, and the results table reports the mechanical share
on its own row so the two are not read as equally well evidenced.

### 4. The sandbox defaults to not running anything

Three layers. Layer 1: execution is off unless `--allow-exec` is passed. Layer 2
(`--sandbox=subprocess`): rlimits, a scrubbed environment, process-group kill. Layer 3
(`--sandbox=docker`, recommended): network-less, read-only, unprivileged, dropped
capabilities, CPU/memory/PID limits.

**The trade-off is that Layer 2 is weaker than it looks, and the tests say so.**
`tests/test_sandbox.py` carries two `xfail`s with reasons: network is not blocked under
subprocess, and neither is filesystem containment — `cwd` is the scratch directory, so
`open('../evil.txt')` writes outside it, and `preexec_fn` cannot create a mount namespace
without privileges. docs/05's own test table claimed the second row held at Layer 2. It does
not. A suite that documents its own limitation is worth more than one that looks complete.

Running the tribunal *inside* a container inverts the usual advice: `--sandbox=docker` from in
there needs the host docker socket, and anything that can talk to that socket can start a
privileged container mounting `/`. The tribunal in a container is already namespaced away from
the host, so the image ships `TRIBUNAL_SANDBOX__MODE=subprocess` and the socket mount sits
commented out in `docker-compose.yml` with the reason attached.

### 5. What I would do differently

**Write the consumer before the producer, or neither.** Five separate times a field was read
by code that worked, tested fine, and was fed by nothing: `perf` measurements never reached
the trace; the critics were never shown the Coder's rationale, so the R1 sycophancy mitigation
rendered nothing on every real run; `PolicyInput.input_parses` had no FSM edge to fire
through; `tool_run` recorded a findings *count*, so the tribunal arm — and only the tribunal arm —
would have scored zero on every rule locator; and exit codes 3 and 4 were documented and
unreachable. Each was found by writing the thing that consumed it. None would have been found
by a test, because every one of them had tests that passed. If I started again I would treat
"a field with no producer" as a build error rather than a code-review question.

**Check what the tools actually cover before choosing the dimensions.** The grounding
asymmetry in § 3 is a fact about `bandit` and `ruff` that ten minutes of checking would have
surfaced on day one. It does not invalidate the performance dimension — an arm that only
relays tool output scores near zero there, which is a *stronger* answer to "isn't this just a
linter wrapper?" than the re-rating rate — but I would have designed the metric knowing it,
rather than discovering it while writing case 20 of 24.

**Record cassettes as each agent is built.** The roadmap says this explicitly and I did it for
the first three agents and not the last three. The consequence is that the per-PR smoke gate
fails today, on purpose, because it cannot run.

**Be more suspicious of skips than of failures.** Changing the default NIM model silently
turned the Coder's entire acceptance criterion into six skipped tests. The run still said
"passed"; the only signal was a skip count moving from 33 to 39. A cassette miss is now a
failure, and the replay suite pins the model it recorded against rather than following a
mutable default.

**Measure `B1` earlier.** The whole eval turns on tribunal-versus-same-tools-single-call, and that
number is the one most likely to be uncomfortable. The harness is built and the benchmark is
complete; the sweep has not been run, so this README contains no results table — and a score
with no run behind it is not evidence.

## Docs

Start at [`docs/README.md`](docs/README.md) for the reading order. Highlights:

- [Charter](docs/00-charter.md) — scope, non-goals, success criteria
- [Architecture](docs/01-architecture.md) — the state machine and the two rules it's built on
- [Arbitration](docs/04-arbitration.md) — decision table, stopping conditions, conflict detection
- [Execution sandbox](docs/05-execution-sandbox.md) — running model-written code safely
- [Evaluation](docs/07-evaluation.md) — four baselines, canary cases, validated judge
- [Roadmap](docs/09-roadmap.md) — phases with acceptance criteria, and the cut line

## Scope (v1)

**The tribunal** — `run`, `critique`, `propose`, `review`, the MCP server — is single-file
Python, standard library plus an allowlist, optional `pytest` oracle. Two critic dimensions:
security and performance. Multi-file refactors, other languages, and networked code are
explicit non-goals — see the [charter](docs/00-charter.md) for why the narrowing follows from
the grounding constraint rather than from the timeline.

**`tribunal code`** is not bound by that, and the difference is not an inconsistency. The
narrowing exists because every critic claim has to be grounded in a real tool result, and
bandit, ruff, radon and astgate are Python tools. The coding agent makes no grounded claims
— it reads, edits and runs what you tell it to, in whatever language the directory happens
to contain. The one place the two meet is its `review` tool, which inherits the narrow scope
exactly: it refuses anything that is not a `.py` file rather than pretending to assess it.
