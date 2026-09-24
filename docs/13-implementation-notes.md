# 13 — Implementation notes: where the code departs from the plan

The docs are the design record, so the places where building Phase 1 proved a doc wrong belong
in them rather than in a commit message. Each entry below is a change the code makes against
what [00](00-charter.md)–[12](12-learning-checklist.md) specify, with the reason.

Nothing here changes the thesis or the scope. Entries 1–7 came out of Phase 1; entries 8–11 came
out of building the multi-provider LLM layer ([14](14-providers.md)), where three vendor
documentation claims turned out not to match the live API.

## Bugs in the reference snippets

### 1. `os.setsid()` in `preexec_fn` alongside `start_new_session=True` kills the child

[05](05-execution-sandbox.md) § Layer 2 shows both. `subprocess` runs `setsid()` in the child
*before* `preexec_fn`, so the second call fails with `EPERM` and the child dies before `exec`:

```
SubprocessError: Exception occurred in preexec_fn.
```

`sandbox.py` passes `start_new_session=True` only, and `_limits()` sets rlimits and nothing
else. The doc's underlying point — kill the process *group*, because `proc.kill()` leaves
grandchildren running — is correct and is verified by
`test_grandchild_does_not_outlive_the_kill`. That test asserts on the process *state* in
`/proc`, not on pid existence: a killed grandchild whose parent is already gone becomes a
zombie, and `os.kill(pid, 0)` succeeds on a zombie.

### 2. `useradd -u 65534` fails on the base image

[05](05-execution-sandbox.md) § Layer 3's `Dockerfile.sandbox` runs
`useradd -u 65534 -m runner`. uid 65534 is already `nobody` in Debian, so the build fails with
`UID 65534 is not unique`. `Dockerfile.sandbox` reuses the existing account and creates a home
directory for it, which `--read-only` otherwise makes unwritable.

## Corrections driven by a downstream requirement

### 3. No `-x` on the pytest oracle

[05](05-execution-sandbox.md) § What the Profiler is allowed to run specifies
`pytest <test> -x -q --timeout=20`. But decision-table row 2 is *"the user's test passed on the
original and fails on the patched"* — a comparison of the **sets** of passing node ids across
two runs. `-x` stops at the first failure, so that set is truncated: a patch that breaks test B
becomes indistinguishable from one that breaks A and B, and a pre-existing failure early in the
file masks everything behind it. `pytest_t.py` drops `-x` and reads `--junit-xml` for node ids,
which counts alone cannot give. `regression_node_ids()` then implements row 2 exactly, excluding
tests that were *already* failing — the bug we were asked to fix failing again is a different
signal.

### 4. Output is captured to a file, not through a pipe

[05](05-execution-sandbox.md) § Layer 2 pipes stdout/stderr and calls `proc.communicate()`. That
buffers the child's output in the **parent's** memory, outside every rlimit just applied — so a
`while True: print('x')` in the target exhausts the host's RAM while the sandbox's 512 MB cap
looks like it is doing something. Child output goes to files in the scratch directory, where
`RLIMIT_FSIZE` bounds it, and only a capped prefix is read back.

### 5. `radon` always emits a complexity summary

[01](01-architecture.md) and the config in [04](04-arbitration.md) imply per-function complexity
findings above a rank threshold. But the Profiler's characteristic citation is a *delta* —
"cyclomatic complexity 7 → 12" is the cost line in [04](04-arbitration.md)'s worked trade-off.
7 is rank B, below the default `C` threshold, so with per-function findings alone the *before*
side of that comparison would not exist in the baseline report. `radon_t.py` therefore always
emits one `CC-SUMMARY` finding carrying every function's complexity in `raw`, at `line: None`
and with `tool_severity: None` so it cannot be read as a defect.

### 6. The Coder's output format is split in two

[02](02-contracts.md) declares the Coder's output as `Patch` with a unified `diff`.
[03](03-agents.md) makes search/replace blocks the *recommended primary* format, because models
miscount line numbers. Both are right about different layers, so `contracts.py` carries both:
the model emits a `PatchProposal` (`edits` **or** `diff`, never both), and
`patch.synthesise_patch` renders the canonical unified diff. Everything downstream — the trace,
the viewer, the oscillation hash — only ever sees `Patch`, exactly as [02](02-contracts.md)
intends.

Two supporting details fell out of building it:

- **`difflib` glues body lines together** when a source has no trailing newline, producing
  `-y = 2+y = 3` and an unparseable diff. `make_unified_diff` normalises both sides before
  diffing and `apply_unified_diff` restores the original's convention.
- **Hunk relocation is bounded.** A hunk whose `@@` line numbers are wrong is moved if its
  pre-image appears *exactly once*; two candidates is a refusal naming both. Fuzzy application
  is how a patch tool corrupts a file.

## An honest downgrade

### 7. Layer 2 does not confine filesystem writes

[05](05-execution-sandbox.md) § Tests for this layer lists

| Write to `$CWD/../evil.txt` | confined to scratch dir; scratch removed after |

as a row that passes. It does not pass under `--sandbox=subprocess`. `cwd` is the scratch
directory, so `open('../evil.txt', 'w')` writes to its parent; rlimits bound the *size* of a
write, not its location, and `preexec_fn` cannot create a mount namespace without privileges.
Only `--sandbox=docker` (`--read-only` plus a single `noexec` tmpfs) enforces it.

`test_relative_path_traversal_is_confined` is therefore a **strict xfail with a reason**, the
same treatment [05](05-execution-sandbox.md) prescribes for the network row. A suite that
documents its own limitation is better than one that looks complete. What does hold at Layer 2:
`HOME` and `TMPDIR` are redirected into the scratch directory, so `~/.ssh/id_rsa` is
unreachable and a write to `~/.bashrc` lands somewhere we delete — both tested.

The README claim in [05](05-execution-sandbox.md) § README claim needs one clause added when
it is written: under `--sandbox=subprocess`, **filesystem writes outside the scratch directory
are not blocked either**.

## Deferred, not dropped

- `RLIMIT_NPROC` is set but does not bind for uid 0: a process with `CAP_SYS_RESOURCE` bypasses
  the fork limit. The fork-bomb test skips with that reason when running as root rather than
  passing vacuously. `--sandbox=docker`'s `--pids-limit` enforces it for any uid.
- The `astgate` false positive on a local variable shadowing a module name (`os = FakeShell()`
  then `os.system(...)`) is tested and left in place. Fixing it needs scope tracking; the cost
  of a spurious *advisory* finding is one Red-team dismissal, and the system already has a
  mechanism for that.


---

## Corrections found while building the provider layer

These came from probing live endpoints and introspecting SDKs rather than from reading prose, and
in each case the prose was wrong. Full context in [14](14-providers.md).

### 8. Anthropic model ids must not carry a date suffix

The first version of `config.py` pinned `claude-haiku-4-5-20251001` and aliased the plain
`claude-haiku-4-5` to it. That is backwards: the API **rejects** date-suffixed ids, and
[10](10-cost-and-limits.md)'s own price table uses the plain form. The canonical id is
`claude-haiku-4-5`; the dated string survives only as an alias. `tests/test_config.py` previously
asserted the wrong direction and has been corrected — a test that encodes a bug is worse than no
test.

### 9. NVIDIA's `thinking_token_budget` is self-hosted only

NVIDIA's model documentation presents `thinking_token_budget` (and `reasoning_budget`) as the way
to control reasoning depth. Sending it to `integrate.api.nvidia.com` is a hard
`400 Unsupported parameter(s)`. The hosted endpoint takes OpenAI's `reasoning_effort` instead, so
the budget parameter is gated on a non-default `base_url`.

### 10. NVIDIA's reasoning is *not* inline on the hosted endpoint

The docs describe the chain of thought as "before the final answer, visible in `content`". Measured:
the hosted endpoint returns reasoning in a separate `reasoning_content` field and leaves `content`
as clean JSON. Both shapes are handled — `reasoning_content` is preferred, and `llm/extract.py`
strips an inline `<think>` block otherwise — but `Capabilities.inline_reasoning` is claimed only
for a self-hosted `base_url`.

### 11. Gemini's structured-output shape is nested, and camelCase

The Gemini structured-output guide shows a flat
`response_format={"type": "text", "mime_type": ..., "schema": ...}`. Introspecting
`google.genai.types.ResponseFormat` shows the real shape nests a `TextResponseFormat` under a
`text` key with the field spelled **`jsonSchema`**. Following the prose would have sent a schema
the API silently ignores, producing responses that parse as JSON while not matching the contract —
the hardest kind of failure to attribute later.

## A constraint the contracts doc assumes is enforceable, and is not

[02](02-contracts.md) design rule 3 quantises `Confidence` to 0.05 so that "free-running floats
[do not] invite false precision". **No provider's constrained decoding enforces `multipleOf`** —
not Anthropic, not OpenAI's strict mode, not Gemini. The rule is a *validation* guard, not a
*generation* guard, and a model returning `0.93` yields a payload that looks valid and is not.

Rather than spend a repair retry on quantisation noise — which would swamp the parse-retry rate
that [09](09-roadmap.md) makes the prompt-health signal — `llm/client.py` applies
`contracts.quantise_confidence` locally first and reports it as a `local_repair`, separately from
`repair_retries`. `llm/schema.py` reports every dropped constraint per field so the caller always
knows which guarantees are validation-only.

## Observed critic behaviour worth designing against

The first live Nemotron run produced a schema-valid `Critique` that was semantically wrong in three
ways — all three are the failure modes [11](11-risks.md) predicts, and all three were fixed by
tightening the prompt rather than the schema:

1. **`tools_consulted` contained finding ids, not tool names.** Schema-valid, meaningless. A
   cross-field check against the `GroundingReport`'s actual tool list would catch this in code
   rather than in prose — worth adding when the Red-team agent lands.
2. **Every issue was rated `high` at confidence 0.95**, including a finding `bandit` rated MEDIUM.
   Critique inflation ([11](11-risks.md) R2), and a direct argument for the re-rating divergence
   metric [02](02-contracts.md) calls for.
3. **The same `shell=True` defect was emitted twice**, once per reporting tool, which
   double-counted pressure to 45.6 on a file with one bug. **The policy layer must deduplicate
   issues citing overlapping spans before summing pressure**, or two tools agreeing inflates the
   score exactly when the evidence is strongest. This is a Phase 3 requirement that
   [04](04-arbitration.md) § Pressure does not currently state.

With the prompt instructed to use tool names, to emit one issue per distinct defect citing multiple
findings as evidence, and to re-rate in context, the same model produced the correct single-issue
critique with both findings as evidence on the first attempt.


---

## Corrections found while building the two critics

### 12. `GroundingReport` could not express "the tool ran and found nothing"

`tools_run` was a *property* derived from `{f.tool for f in findings} | tool_errors`. So a tool
that ran cleanly was indistinguishable from a tool that never ran — both produced no findings and
no error.

That is the same confusion the policy layer refuses to make about critics (`unassessed` is not
`clean`, [04](04-arbitration.md) rows 5/6), and it broke the Red-team on the **clean canary
fixture**: only `radon` reported anything, `radon` is not a security tool, so no security tool had
"produced output", and `tools_consulted` — which [02](02-contracts.md) requires to be non-empty —
became unsatisfiable. The critic correctly emitted `[]` and failed validation twice.

`tools_run` is now a required field on `GroundingReport`, populated by the suite from the actual
tool outcomes, with `tools_succeeded` and `tools_reporting_nothing()` derived from it. "bandit and
ruff ran and found nothing" is the whole substance of a critique on a clean file, and it is now
representable. Both prompts say so explicitly.

### 13. Model-supplied `Issue.id`s are unusable

Observed on the first live run of each critic: the Red-team reused the **grounding finding id** as
the issue id, and the Profiler emitted `"1"` and `"2"`. Both are schema-valid, and both break the
three places `Issue.id` is load-bearing — dismissal persistence, conflict detector 1 (which
recognises a trade-off by seeing *the same id* return), and the regression metric.

[04](04-arbitration.md) § Failure-mode checklist names this exactly. Ids are now **assigned by
us** in `agents/identity.py` from `contracts.canonical_issue_id(dimension, rule, ref)` over the
issue's primary anchor, preferring a tool finding's rule code because it survives line numbers
shifting in later rounds. The model's own id is kept in the trace so a reader can see the rename.

The mapping is a **list of pairs, not a dict**: a model emitting `"1"` twice would collapse two
renames into one, and that is precisely the input this exists for.

### 14. A `high` issue under a non-blocking verdict

[02](02-contracts.md) puts one direction of this guard in the schema — `"block"` requires a
`high`, because "a critic that blocks on low-severity findings is mis-calibrated and the parse
fails loudly". The converse is equally mis-calibrated, and the first live Profiler run did exactly
it: two `high` issues under `verdict: "concerns"`.

Enforced in `agents/validation.py` rather than in `contracts.py`, because the docs specify only
one direction and unilaterally tightening a documented schema is a bigger change than adding a
calibration check. **Proposed contract tightening:** make it a biconditional in `Critique`.

### 15. `classify()` ignored the measurement-quality rule

[03](03-agents.md) § 3.3 specifies: "If `stdev_ns > 0.15 × mean`, the measurement is
`inconclusive`". My `classify()` only compared the *delta* against pooled noise, so a 40%
regression measured on a machine whose timings varied by 30% would have been reported as `slower`.
`MAX_NOISE_RATIO` and `MIN_REPEATS` now gate the verdict before the delta is considered — a large
difference measured badly is still measured badly.

## Observed critic behaviour, measured rather than assumed

Four recorded runs on NVIDIA Nemotron (2 fixtures × 2 critics), all validating on the first
attempt:

| Fixture | Critic | Verdict | Issues | Severe findings cited |
|---|---|---|---|---|
| `vulnerable.py` | redteam | `block` | 1 | **1 of 2** |
| `vulnerable.py` | profiler | `clean` | 0 | n/a |
| `clean.py` | redteam | `clean` | 0 | n/a |
| `clean.py` | profiler | `clean` | 0 | n/a |

**Both critics pass the clean canary** — no manufactured issues, which is [11](11-risks.md) R2.

**The Red-team under-reports on the seeded-bad case.** Its `summary` named four high-severity
defects — command injection, `eval`, SQL injection, `os.system` — and it emitted **one** issue.
That is R1 (the sycophancy tail), and the prose summary is not mechanically checkable, but
coverage of the tool's own HIGH findings is. `critic_metrics` now reports `severe_findings_cited`
and `uncited_severe_findings`, and the CLI surfaces them. It is a **metric, not a rejection**: a
critic legitimately declining a finding as unreachable is the entire point of re-rating, so a
pattern of silent omission belongs in the eval, not in the validator.

**Re-rating rate on that run was 0/1 and novel-issue rate 0.0** — i.e. close to linter-wrapper
behaviour on that file. That is exactly the number [11](11-risks.md) R3 says to publish, and it is
collected from the first run rather than retrofitted.


---

## Corrections found while building the Coder

### 16. Three retry budgets, and why none of them may be merged

[03](03-agents.md) § 3.1 mitigation 3 says VALIDATE "returns a precise mechanical error and the
Coder retries (2 attempts, **not counted against debate rounds**)". That is a *third* budget, and
by the time the Coder existed the system already had two:

| Budget | Counts | Spent when | Owner |
|---|---|---|---|
| `repair_retries` (1) | extra LLM calls | the output violates the schema | `LLMClient` |
| `patch_attempts_per_round` (2) | extra LLM calls | the patch does not apply or parse | `Coder.propose` |
| `max_rounds` (3) | debate rounds | the policy layer rejects the patch | orchestrator |

Merging any two hides a different problem. If VALIDATE bounces counted as schema repairs, the
repair-retry rate — which [09](09-roadmap.md) makes the prompt-health signal — would be dominated
by line-number arithmetic, and a real prompt regression would disappear into it. So
`validate_patch_proposal` checks only what is visible *without applying the patch* (ids that do
not exist, an id in both pushback fields, a copied line-number gutter), and the apply/parse check
runs in its own loop.

### 17. `Patch.addresses` has nothing to name on round 1

[02](02-contracts.md) defines `addresses` as "`Issue.id`s this patch intends to resolve". But on
**round 1 there are no `Issue`s** — the critics have not run. The only ids in existence are
`GroundingFinding.id`s.

The docs do not resolve this. Leaving the field empty on round 1 would delete the churn check
[03](03-agents.md) asks for (`hunks_touched` vs `addresses` count), so `CoderBundle.addressable_ids()`
is the union: finding ids on round 1, plus open issue ids from round 2. A made-up id is still
rejected. **Proposed contract clarification:** say so in [02](02-contracts.md).

### 18. The model emits `\n` as two characters

The single largest cause of Coder failure, measured: **3 of 6 fixtures failed on prompt v1**, and
two of those three were the same bug. The model double-escapes newlines inside the JSON string
value, so `search` arrives containing a backslash and an `n` where the file has a real newline.
It surfaces as two unrelated-looking errors — "search block not found", and a replacement that
applies and then will not parse — which sends the retry looking in the wrong place.

`patch.py` now repairs it, **conditionally**: the escaped form is tried only when the verbatim
anchor does not match, and accepted only when the unescaped form matches exactly once. That
condition is load-bearing, because source code legitimately contains a literal backslash-n —
fixture `005-string-concat.py` has `",".join(...) + "\n"` in it, and unconditional unescaping
would corrupt precisely that case. Same discipline as hunk relocation: repair when the answer is
unambiguous, refuse when it is not. The repair is reported rather than swallowed, so a systematic
escaping fault cannot hide behind a green result.

`_unescape` deliberately does not use `codecs.decode(..., "unicode_escape")`, which would also
rewrite `\x`, `\u` and `\\` and mangle a Windows path or a regex in the source.

### 19. Overlapping edits produce a misleading error

Observed on the SQL-injection fixture: the Coder emitted one edit covering both the query line and
the return line, then a second edit covering the return line alone. The first applied; the second
then matched text the first had already rewritten; the result did not parse and was reported as
"the edit itself is malformed Python" — true, but useless, because the actual fault was that the
two edits were incoherent *with each other*.

`validation.py` now checks edit spans against the original source before application, so the error
names the real problem. Prompt v2 states the rule.

### 20. The model writes anchors as if they were regex patterns

The last fixture to fail did so for a different reason again, and the raw wire text was the only
way to tell. Where the source has `",".join(...)`, the model sent `\(",\)\.join(...)`: regex
escaping on `.` and `(`, and an opening double-quote substituted with `\(`.

Worth stating precisely, because it changes what the right fix is: **this is the model's
serialisation, not our parsing.** The escapes are present in the raw response body before
anything of ours touches it.

It is also *not* recoverable by de-escaping. `\(",\)` does not de-escape to `","` — a quote
character has been replaced, so information is gone. Adding a "turn `\(` back into a quote"
repair to `patch.py` would be encoding one model's quirk into the general patch layer, and it
would fire on source that legitimately contains `\(` (any regex in a string literal). So the
fix is prompt-side only: v4 states that `search` is matched by exact string comparison, is not
a regular expression, and must escape nothing.

**This is what `StructureMode` is for.** NVIDIA NIM is `native_json` — JSON mode with no schema
enforcement — and this is the class of defect that distinguishes it from the `native_strict`
backends, where decoding is constrained. The layer records the mode on every response precisely
so a failure like this is attributable to the provider rather than to the prompt.

### A narrowing, found by its own test

`_unescape` originally handled `\t` as well as `\n`. Its own test caught that this rewrites
`C:\tools` — two characters a Windows path in the source legitimately contains. Unlike the
newline case there was never a measured failure behind tab handling, so it is gone. The rule the
repair layer follows: **repair only what has actually been observed to break.**

### Prompt versions earned, not assumed

The Coder prompt reached **v4** during this phase, each bump traceable to a measured failure
rather than to taste:

| Version | Change | Because |
|---|---|---|
| v1 | initial | — |
| v2 | newline-escaping rule; one-edit-per-region rule | 3 of 6 fixtures failed |
| v3 | `addresses` and `deliberately_unaddressed` are mutually exclusive | the quadratic-accumulate fixture put one id in both and did not recover on the repair retry |
| v4 | `search` is literal text, not a pattern | the string-concat fixture emitted regex escapes and a quote substitution in its anchor |

This is why `prompt_version` is stamped from the first run and cannot be retrofitted
([09](09-roadmap.md) § Hard-won ordering advice): every one of those bumps invalidates the
cassettes recorded against the previous version, and without the stamp there would be no way to
tell which prompt a recorded response came from.


## The Coder's six fixtures, measured

docs/03-agents.md § 3.1's acceptance criterion, against NVIDIA Nemotron with prompt v4. All
three clauses are asserted, and the third is the one that matters: "applies and parses" alone
would pass a patch that rewrote an unrelated function.

| Fixture | Applies | Parses | Touches target | Attempts |
|---|---|---|---|---|
| `001-shell-injection.py` | yes | yes | yes | 1 |
| `002-sql-injection.py` | yes | yes | yes | 2 |
| `003-eval-config.py` | yes | yes | yes | 2 |
| `004-quadratic-accumulate.py` | yes | yes | yes | 1 |
| `005-string-concat.py` | **no** | — | — | 2 |
| `006-weak-hash.py` | yes | yes | yes | 1 |

**5 of 6.** The progression is the interesting part: v1 scored 3/6, v2 scored 4/6, v4 scores
5/6, and each bump came from reading the raw wire text of a specific failure rather than from
rewriting the prompt on instinct.

`005` is left as a non-strict `xfail` with the measured reason, the same treatment as the
sandbox's network row. It is the only fixture whose defect sits inside a line dense with quote
characters, and it is failing on a provider-side serialisation defect that no prompt line has
cleared — which is exactly the difference `StructureMode` was built to make attributable.

**Three of the five passing fixtures needed two attempts**, i.e. the first patch did not apply
and the mechanical error fixed it. That is the VALIDATE loop doing the job docs/01-architecture.md
§ Why VALIDATE exists claims for it — absorbing diff-formatting stumbles at zero critic cost —
and it is a strong argument for keeping `patch_attempts_per_round` separate from the debate
budget.


---

## Additions made while building the policy layer and the FSM

### 21. Pressure collapses duplicate issues within a dimension

[04](04-arbitration.md) § Pressure sums `Issue.score` over open issues, with no de-duplication.
Measured against that: the first live Red-team run emitted one `shell=True` defect as **two**
issues — one citing bandit's finding, one citing ruff's — and pressure came out at **45.6 on a
file with one bug**. Two independent tools agreeing is the *strongest* evidence the system can
have, so letting it double the score inverts the signal.

`policy.pressure` therefore collapses issues that share a grounding ref. Deliberately
conservative in two ways, because the alternative failure is worse:

* **Within a dimension only.** The Red-team and the Profiler both flagging the same line is
  genuine cross-dimension disagreement — very possibly the conflict that produces a `TRADEOFF`.
  Collapsing that would delete the project's headline feature.
* **Same evidence, not same topic.** Two issues collapse only when they cite a common ref.
  Similar titles are not enough; a critic can legitimately raise two distinct concerns about one
  line.

The survivor is the higher-scoring of the pair, and `policy.duplicates_in` reports what was
dropped so the collapse is visible in the trace rather than silently changing a number the eval
depends on. **Proposed amendment to [04](04-arbitration.md) § Pressure.**

### 22. A `REJECT` on the final round has to escalate

The decision table's `rounds_exhausted` row escalates when the round budget is spent *and*
pressure is above threshold. But a `REJECT` can arrive on the final round from a different row
— `correctness_regression`, most obviously — and the FSM's `ARBITRATE --reject--> PROPOSE` edge
would then loop past `max_rounds`.

`fsm.trigger_for_decision` takes `rounds_remain` and converts a terminal-round `REJECT` into an
`ESCALATE`. It lives in the FSM rather than the policy table because it is a fact about the
machine's budget, not about the merits of the patch — and putting it in the table would have
meant a thirteenth row that duplicates row 10's condition with a different consequence.

### 23. A dimension nobody assessed is unassessed, not clean

[04](04-arbitration.md) rows 5/6 say "any critic `errored`". Implemented literally, a round
where a critic was never *invoked* — as opposed to invoked and failed — produces no error and no
critique, sums to zero pressure, and accepts.

`Context.unassessed` therefore counts both: dimensions reported as errored, *and* dimensions
with no critique at all. This is the same bug class as note 12 (`GroundingReport` could not
express "the tool ran and found nothing"), arriving one layer up.

### A bug the tests found in the rule they were testing

`_no_progress` compares a three-round window against its own tail, and the first version zipped
them with `strict=True` — which raises, because a window and its tail differ in length by one.
It only surfaced on the specific input that reaches a third round with history, which is exactly
what "one test per row" is for.

## Corrections found while building the orchestrator and the trace

### 24. A run that never lands a patch has to decide, not just stop

The decision table's row 1, `input_unusable`, has two clauses: the original file does not parse,
**or** `patch_attempts_exhausted`. The orchestrator set neither. When VALIDATE burned every
re-anchoring attempt it broke out of the loop, and the run ended with no `policy_decision` at
all — so `run_end` named `input_unusable` (its fallback for "no verdict") while
`trace/report.py` named `no_decision_recorded` (its own). One run, two answers to
"why did this stop", differing by which half of the trace you read.

The fix is to run the table on the way out, with `patch_attempts_exhausted=True`, and emit the
`policy_decision` like any other round. Row 1 fires, both names agree, and the round appears in
`rounds_used` instead of vanishing. It also means the second clause of row 1 is now reachable
from a real run rather than only from `tests/test_policy.py`.

The FSM is untouched: VALIDATE exhausting still goes to `FAILED`, and a decision recorded while
the machine sits in `FAILED` is correct — the table explains the outcome, the machine says where
the run stopped. They are answering different questions.

### 25. `budget_check.breached` cannot fire before the policy row does

The orchestrator checks the budget on entry to `PROPOSE` and keeps a `forced_escalation` so
that "a breach detected at state entry must not be overtaken by a later row". With the template
Arbiter it never fires, and this is not a bug: both the check and row 3 call
`Spend.exceeds(budget)` on the same counter, and `decide` sees it a full round's spending later
than the state-entry check does. Anything the entry check would catch, row 3 has already caught.

It becomes reachable when an **Arbiter agent** is configured, because that spends tokens
*between* round N's decision and round N+1's `PROPOSE` — which is the gap the guard was written
for. Left in place and documented rather than deleted, and `tests/test_orchestrator.py` asserts
the row-3 path instead of the flag, so the test does not quietly pass for the wrong reason.

### The one thing the fake provider cannot prove

`test_the_two_critics_really_do_overlap_in_wall_clock` sleeps inside the double and asserts the
`CRITIQUE` span is shorter than the sum of the two critics' `duration_ms`. Without the sleep the
assertion is vacuous — both durations round to 0ms and any span "beats" them. That is the shape
of every parallelism test worth writing: the claim is about wall-clock, so the test has to spend
some.

## Corrections found while building the Arbiter

### 26. The Arbiter has to run twice, on opposite sides of the decision

docs/03-agents.md § 3.4 lists the Arbiter's input as "`Verdict` from `policy.decide()` (**the
decision is already made**)", and docs/04-arbitration.md § Conflict detection makes detector 2
conditional on "their `suggested_direction`s [being] classified as opposing **by the Arbiter**".
Those two cannot both be one call. The affirmation is an *input* to `decide`; the note is
written *about* its output. A single agent invocation would have to be upstream and downstream
of the same function.

So the Arbiter is two agents behind one seat: `ConflictAffirmer` (`arbiter_affirm/v1`,
`ConflictAffirmation`, effort `medium`) before the decision, and `Arbiter` (`arbiter/v1`,
`ArbiterNote`, effort `xhigh`) after it. The split is not only mechanical:

* **Separate `prompt_version`s.** Folding the classification into `arbiter/vN` would make a
  wording change to the *synthesis* read, in the eval, as a change in conflict sensitivity.
  Those are the two numbers this project most needs to keep apart.
* **Separate effort.** The affirmation fires once per same-span candidate pair, more often
  than the note does, and answers a yes/no question. Giving it the synthesis budget would make
  the cheapest question in the system its most expensive call.

`policy.detect_same_span_conflict(critiques, arbiter_affirms: bool)` survives unchanged for the
pure tests, but the orchestrator no longer uses it: a single round-wide boolean cannot express
"the Arbiter said yes about *this* pair". `same_span_candidates()` and `same_span_conflict()`
are split out so a specific pair can be asked about, and the mechanical filter stays free —
no candidate, no call.

### 27. An Arbiter that can dismiss anything can decide

`ArbiterNote.dismissed` removes issues from `pressure` permanently. Nothing in the schema says
*which* issues may go there, and nothing needed to while the field was unreachable. Once the
agent exists, an unconstrained `dismissed` is a complete bypass of Rule 1: the Arbiter cannot
change `Verdict.decision`, but it can empty the sum that produced it and let the next round
accept.

`validation.py` therefore restricts dismissal to ids the **Coder actually pushed back on**
(`Patch.deliberately_unaddressed`). That is the channel docs/03-agents.md § 3.4 job 3
describes — "for each `deliberately_unaddressed` claim, agree or disagree" — and nothing wider
was ever intended. Dismissing an uncontested issue is now a repair retry with a message saying
why, alongside three smaller checks: an id cannot be both dismissed and prioritised,
`priority_order` cannot name a non-open issue, and the prose cannot contain a diff.

### 28. `Report` could not carry the sentence the TRADEOFF state exists for

docs/04-arbitration.md § What TRADEOFF actually emits specifies a recommended default and the
condition that reverses it ("ship the validated version; reverse this if this is the ingest hot
path"). `Conflict` carries the two issue ids, the axis and the two remedy costs — the *record* —
but there was nowhere for the *recommendation*, so the highest-value output in the system had
no field to land in and `tribunal run` could not print it.

`Report.tradeoff_justification` and `Report.recommended_default` are added, optional, and
derived from the last Arbiter note in the trace that carries one — not the last note outright,
since a run reaching TRADEOFF has one note per earlier REJECT in front of it, and those are
instruction sets. Optional rather than required-on-TRADEOFF because the templated path has no
recommendation to make, and says so rather than inventing one.

### 29. An Arbiter with its own client spends money the budget cannot see

`Orchestrator._check_budget` reads `self.client.total_cost_usd`. An `Arbiter` constructed as
`Arbiter(settings)` builds its own `LLMClient`, so its spend — the most expensive call in the
round, at `xhigh` — would be missing from every budget check. The symptom is a cap that never
fires, which looks exactly like a run comfortably inside its budget.

Rejected at construction rather than documented: `Orchestrator.__init__` raises if the Arbiter
does not share its client. Caught the same way `_check_prefix_is_stable` catches a volatile
cache prefix — both are silent-until-the-bill-arrives failures, and neither is visible in a
code review of the line that causes it.

### A test that was passing for the wrong reason

`test_two_grounded_issues_on_one_span_end_in_a_tradeoff` passed `arbiter=object()`, with the
comment "affirmation is all detector 2 needs from it". That was true of the *placeholder* — the
orchestrator only checked `is not None` — and it meant the one test asserting a TRADEOFF never
exercised an affirmation at all. With a real Arbiter driven by the same scripted provider, the
test now needs the affirmation in its script, and the far more interesting case becomes
writable: an Arbiter that **declines** to affirm. "No Arbiter, so no TRADEOFF" is true by
construction; "an Arbiter looked at the pair and said no" is what decides whether detector 2 is
a detector or a rubber stamp.

### The ordering nobody would guess from the docs

`same_span_candidates` sorts pairs by `(left.id, right.id)`, so which pair the Arbiter is asked
about does not depend on which critic returned first. Since ids are `SEC-`/`PERF-` prefixed,
sorting means the **performance** issue is `left` whenever both dimensions are present — the
opposite of the reading order in every example in docs/04. It is correct, `_axis_for` is
order-insensitive, and `validate_affirmation` rejects an answer that swaps the pair; but a test
that hard-codes `(security, performance)` tests the repair path by accident, which cost a
debugging round here.

## Corrections found while building the Postmortem

### 30. The Postmortem cannot output a `Report` without breaking criterion S6

docs/03-agents.md § 3.5 gives its output as "a `Report` (markdown body + structured fields)".
Taken literally that is a second `Report`-builder sitting next to `trace/report.build`, and
criterion S6 — the report replays byte-identically — is true *by construction* precisely
because there is exactly one. An LLM in that position makes the claim unmakeable: two calls,
two reports, no reproducibility.

The resolution keeps both. The agent emits a **`PostmortemNote`**: the content, written into
the trace like any other agent output. `trace/report.render_narrative` lays it out, and
`build` picks it up from the trace on the live path and the replay path alike. Same function,
same events, same bytes — S6 survives, and `Report.narrative` is just another derived field.

It also buys the thing that mattered more than the reproducibility argument. docs/03 asks that
"the final section is always **what I would not trust**". Asking a model to remember a
structural rule on every run is how the rule eventually gets forgotten, and you find out from
the one report that omitted it. Rendering the layout here makes "always" true.

### 31. "Never empty" is a schema rule; "complete" needs the trace

`what_i_would_not_trust` is `min_length=1`, which stops the section vanishing. It does not stop
the far more likely failure: a write-up that lists a generic caveat while omitting the issue
that is *actually* still open. An accepted patch with an open issue is a **conditional** pass,
and a narrative that reads as unconditional is worse than no narrative — it is the one artifact
a human reads, and it teaches them to stop looking.

`validation.py` therefore checks the write-up *accounts for* every open issue id and every
unassessed dimension the trace recorded. Deliberately checked across the whole note rather than
against `what_i_would_not_trust` specifically: a narrative that explains an open issue in the
round summary and lists it in the caveats is good writing, and a checker insisting on one exact
field would fight it.

A trade-off's standing objection counts as open for this purpose. It has shipped, but it is
unresolved by construction, and a write-up that reports it as fixed is describing a different
run.

### 32. `perf` results never reached the trace at all

`PostmortemBundle` wants inconclusive and unmeasurable benchmarks, because those belong in the
caveats — an `inconclusive` result is not evidence of no change. Reading them back turned out
to be impossible: measurements live on the in-memory `GroundingReport`, `tool_run` events are
emitted per `ToolOutcome`, and **`perf` produces no `ToolOutcome`**. Nothing was lost at
runtime, because the critics are handed the report directly; but anything reading the file
afterwards — the write-up, and later the viewer — could not tell a benchmark had been attempted.

The fix is a `perf` `tool_run` event carrying the measurements verbatim, emitted by the
orchestrator when the report has any. `measurements` is omitted rather than empty on every
other tool, so existing payloads are byte-unchanged.

Worth noting how this surfaced: the code that consumed the field was written first, passed its
tests against hand-built bundles, and was quietly dead on every real run. The test that caught
it drives a real `GroundingSuite` with `allow_exec=False`, where every benchmark comes back
`unmeasurable` — the case that is silent unless something reports it.

### 33. `tribunal postmortem` rather than `replay --postmortem`

docs/03-agents.md § 3.5 promises the write-up "can be re-run on an old trace for free in replay
mode". Putting that behind a `replay` flag would have cost something not worth trading: the
README's flat claim that `replay` needs no credential and makes no calls. A flag that sometimes
makes an API call turns an unconditional guarantee into a conditional one, and the guarantee is
load-bearing — it is what makes replay usable in CI.

So the capability gets its own command. `replay` still never calls a provider; `postmortem`
says in its help that it does. Both compact the trace with the same `postmortem_bundle`, so
the re-runnable path is not the untested one.

The re-generated write-up is printed, not appended to the trace: a trace is the record of one
run, and a late event after `run_end` would break the gap-free `seq` the reader validates.

## Corrections found while building the viewer

### 34. The trace is untrusted input, and the viewer is the place that forgets it

Everywhere else in the system the trace is *our* data. The viewer is the first component that
turns it into markup, and at that moment it stops being data and becomes a document someone
opens — often by double-clicking a file a colleague emailed them.

Two escapes are therefore load-bearing, in opposite directions:

* **Out of the JSON blob.** A trace carries model-written source and diffs. The four characters
  `</script>` anywhere in one would close the embedding tag early and turn the rest of the run
  into markup. `_embed` escapes `<`, `>` and `&` to `\uXXXX` — still valid JSON, inert in a
  script tag, and not defeatable by casing or whitespace the way a `</script>`-specific
  replacement is.
* **Into the DOM.** The JS never assigns to `innerHTML`; every string goes through
  `textContent`. The test harness enforces this by making `innerHTML` *throw* on access, so a
  future edit that reaches for it fails rather than passing on the author's machine.

Neither is hypothetical for a tool whose input is a file with a known vulnerability in it.

### 35. Chained `.replace()` lets the payload become a placeholder

The shell is filled by substituting `__CSS__`, `__DATA__`, `__JS__` and so on. Done as chained
`.replace()` calls, each substitution's *output* is scanned by the next one — so a trace
reviewing a file that happens to contain the text `__JS__` would get the viewer's entire script
spliced into the middle of the JSON blob.

Obscure, and exactly the class of thing that only ever appears in a demo. One `re.sub` pass with
a dict fixes it permanently: nothing substituted in is ever re-scanned.

### 36. A viewer is the one component nothing else consumes

Every other module in this project is checked by something downstream of it — a bad
`GroundingReport` breaks a critic, a bad `Verdict` breaks the FSM. Nothing consumes the viewer,
so a runtime error on its first line produces a blank page and a green test suite.

So the tests **run the viewer's own script**, in node, against a ~150-line DOM shim
(`tests/viewer_dom.js`), and assert on what it drew. Deliberately a shim rather than jsdom: the
viewer's whole claim is that it needs no dependencies, and a shim small enough to read is also
a statement of exactly which DOM surface it is allowed to use. Anything it reaches for that the
shim lacks fails loudly instead of working in one browser.

It earned its keep immediately — every finding below came from running it, not from reading the
code.

### 37. Three bugs the DOM harness found on first run

**The pressure sparkline drew its last point twice.** `Verdict.pressure_history` is the *full*
history including the current round (`policy.Context.full_history`), not the rounds before it.
The viewer appended `final.pressure` on top, so every trajectory ended in a spurious flat
segment — the exact visual the sparkline exists to show honestly.

**Every 0ms call vanished from the timeline.** The filter was `e.duration_ms` rather than
`e.duration_ms != null`, so a 0ms step was falsy and dropped. That silently removed the
templated Arbiter, the fast critic, *and* — because it left only one critic in the round — the
`⟵ parallel` annotation that is the whole point of the timeline tab.

**An errored critic showed its exception instead of its meaning.** The card printed
`RuntimeError: the profiler fell over` and nothing else. The message that matters is that
nobody assessed the dimension, which is *not the same as clean*; the exception is the detail
underneath it. Both now, in that order.

### A requirement that is a CSS rule

docs/06 ranks "the two critics render side by side" as requirement 1, above the policy box,
and calls it "the single most important rendering decision in the file". In the end it is
`grid-template-columns: 1fr 1fr` — but the test asserts both critic cards are inside the one
`.critics` container rather than asserting on the CSS, because the claim is about what a reader
sees and a stacked pair would still satisfy a property check on the stylesheet.

## Corrections found while building the Docker packaging

### 38. The images are a boundary, so the tests check it rather than the README

docs/08 § Docker states the separation as a table: one image has the API key and the network,
the other executes untrusted code and has neither. Written as two Dockerfiles, that is a
convention — and a convention survives exactly until someone consolidates them "to reduce
duplication".

`tests/test_packaging.py` therefore asserts the properties directly: the sandbox image
installs no tribunal code and no grounding tools, neither image runs as root, no build arg is
credential-shaped, and `docker-compose.yml` passes keys through by name with no values.
`scripts/verify-docker.sh` checks the same claims against the *built* images, where it can
also grep the layer history for anything key-shaped.

Two of those pass today only because the files say so. That is the point: they will fail the
moment the files stop saying so.

### 39. A comment inside a line continuation is a build that differs by builder

The runtime image originally explained its two `TRIBUNAL_*` variables with comments inside a
continued `ENV`. BuildKit strips whole-line comments before resolving continuations; older
builders fold them into the instruction. Either way it is a silent difference between build
environments, which is precisely the failure criterion S7 is about — "works on a clean
machine" means a machine whose builder you did not choose.

Split into separate `ENV` instructions, and `tests/test_packaging.py` now refuses the pattern
in either Dockerfile. This project cannot run a build in its own test suite, so the syntax it
can check statically, it checks.

### 40. `constraints.txt`, and why `pyproject.toml` keeps its ranges

docs/08: "Pin tool versions in the image ... an unpinned image silently invalidates the eval
numbers." The subtlety is *where* to pin. Pinning in `pyproject.toml` would make the package
hostile to install alongside anything else; leaving the image unpinned makes a published
number unattributable, because a finding that appeared from a rebuilt base image with a newer
`ruff` is indistinguishable from one the tribunal earned.

So the ranges stay in `pyproject.toml` and the exact versions live in `constraints.txt`, which
only the images apply. Three tests keep the two from drifting apart: every runtime dependency
is pinned, every pin satisfies the range `pyproject.toml` declares, and the build actually
passes `-c constraints.txt` — a pin nobody applies is a comment.

### 41. The container defaults to the *weaker* sandbox, deliberately

`--sandbox=docker` is the recommended mode on a host. From inside the tribunal's own container it
requires mounting the host docker socket, and anything that can talk to that socket can start
a privileged container mounting `/` — host-root-equivalent access, granted to the process
whose whole job is running model-written code.

The tribunal inside a container is *already* namespaced away from the host, so the exposure that
buys is smaller than the exposure it costs. The image therefore ships
`TRIBUNAL_SANDBOX__MODE=subprocess`, the compose file has the socket mount present but
commented out with the reason attached, and a test asserts it stays commented out. docs/08
asks for this to be documented and opt-in; making the dangerous configuration require an edit
is the strongest form of that.

### A verification script is not a verified build

`scripts/verify-docker.sh` was written against a machine with no docker daemon, so it has
never run. Its shell syntax is checked and its two grep-based assertions were validated
against real `tribunal doctor` output captured locally — including a negative control, since
a version check that cannot fail is worse than none. Everything else in it is unexercised.

Recorded here rather than quietly ticked on the roadmap: the roadmap's S7 box stays open with
a note, because "the script exists" and "the images build" are different claims and only one
of them has evidence.

## Corrections found while building the progress renderer

### 42. A state event's `round` is the round in progress, not the round being entered

The obvious way to draw a run is off `state_enter`: PROPOSE means the Coder is working,
CRITIQUE means the critics are. Built that way, the display put every round-1 step under no
round at all and labelled round 2's header "round 1/3".

Both numbers are correct in the trace and wrong for a display keyed on them:

* `GROUND -> PROPOSE` for round 1 is emitted before the loop starts, with `round=None`;
* the `REJECT` edge into round 2 is emitted from inside round 1, with `round=1`.

There is also an ordering trap underneath it. `_propose` runs the Coder and *then* replays
the attempts as PROPOSE/VALIDATE transitions, so the Coder's `llm_response` arrives **before**
the state it belongs to. A display that creates rows on state entry drops it entirely, which
is exactly what happened — the Coder had no line at all in the first version.

The fix is to build rows from the events that *describe* a step: the agent's response, the
patch validation, the policy decision. Those carry the round they belong to, and they arrive
when the thing they describe actually happened. State exits are still read, but only for
their durations, matched to the most recent row of that name.

### 43. A printed line cannot be taken back

The renderer has two outputs from one state machine: a `rich.Live` on a terminal, and one
line per step when redirected (thousands of ANSI frames in a CI log is worse than no progress
at all). The streaming half has a constraint the live half does not — a row must be complete
before it is printed.

It was not. Rows were flushed after every event, so `grounding` printed the moment `bandit`
reported and never gained `ruff`, `radon` or `astgate`; `critique` printed on the first critic
and lost the second, which is precisely the line whose whole job is showing two critics at
once.

A row is now held until the next one is created — which is also when its `├` or `└` becomes
knowable — and the last is flushed on exit. The live path is unaffected, which is why this
was invisible until the tests captured a non-terminal console.

### A display that must not be able to lie

Two details are there because the alternative is a terminal that reads better than the run
went. A critic that errored shows `profiler FAILED` rather than being absent, because an
absent critic reads as a clean one. A templated consolidation says `templated`, because the
terminal must not imply a model wrote what a template did.

And the renderer is a `TraceWriter` subscriber rather than anything the orchestrator calls,
so a step that appears in the terminal is a step that was recorded — the two cannot drift.
`tests/test_progress.py` asserts a run without the renderer produces the same event stream:
a recorded run must not be a function of whether someone was watching it.

## A sweep for guards nothing reached

§ 32 found a consumer written against a field nothing ever populated: the Postmortem's
uncitable-measurement caveat, dead on every real run because `perf` results never reached the
trace. That is a failure mode with no symptom — the code has tests, the tests pass against
hand-built inputs, and the feature simply never happens.

So before Phase 5 measures anything, every field that something *reads* was checked against
whether anything in production *writes* it. Most hits were model-supplied fields, which is
what "nothing writes it" correctly looks like for a schema the LLM fills. Three were real.

### 44. Row 1's first clause had no edge to fire through

`PolicyInput.input_parses` exists, `_input_unusable` reads it, and the orchestrator never set
it. A file that is not Python went through grounding, a Coder call, two critics and a policy
decision before anything noticed — spending the most expensive part of a run on an input
docs/04 row 1 says to reject immediately.

Setting the flag was not enough, because there was nowhere to go. docs/01's diagram draws
`VALIDATE --> FAILED` (row 1's *second* clause: no diff ever applied) and no edge out of
`GROUND` at all — while the same document's state table says FAILED means "the system could
not produce anything reviewable (**e.g. input doesn't parse**)". The prose described a
transition the diagram omitted.

`GROUND --> FAILED` is now in both, plus `Trigger.INPUT_UNUSABLE`. The run emits a real
`policy_decision` on the way out, so the outcome is explained by the same table as every
other outcome rather than by a special case.

### 45. The sycophancy mitigation rendered nothing on every real run

docs/11-risks.md R1 is that critics go sycophantic when handed a confident justification as
context. `CritiqueBundle` takes that seriously: `_patch_block` renders the Coder's rationale
under "**UNVERIFIED — treat as a claim to check**", lists its pushback, and shows the diff.

The orchestrator never passed `patch=`. The field defaults to `None`, the block renders
nothing, and every critic in every real run was reviewing a patched file with no idea what
the Coder claimed to have done or what it had declined to fix. Tested in isolation, dead in
production — the same shape as § 32, found by the same sweep.

This one matters for the eval specifically: R1 is a *measured* risk, and measuring critic
sycophancy against a system that never shows critics the rationale would have measured
nothing and reported a number.

### 46. Two counters for one question, again

Fixing § 44 immediately reproduced § 24 in miniature. `run_end.rounds_used` was `len(history)`
and `Report.rounds_used` is the highest round that produced a decision. Equal on every path
that runs a round — and 0 versus 1 on the new one, where no round happens but `Verdict.round`
is 1 because the schema forbids 0.

`run_end` now derives the count from the verdict, so the two agree by construction, and a test
asserts `rounds_used`, `rule_fired` and `outcome` match across the report and the trace on all
three shapes of run.

### Still unfed, and not a bug

`CoderBundle.traceback` is rendered and never set: docs/08's `--error PATH|-`, which seeds the
Coder with a failing traceback, is specified and unbuilt. Left alone rather than quietly
wired, because it is a missing feature with a documented interface rather than a guard that
silently does nothing. Recorded in `do.txt` so it does not become one.

## Corrections found while building the eval harness

### 47. A baseline forced through the tribunal's vocabulary measures the wrong thing

`Report.outcome` comes from a `policy_decision` in the trace, so making B0-B2 produce a
`Report` means either faking a `Verdict` — ACCEPT with no critics, which the schema only
permits by claiming no dimension was unassessed, which is untrue — or running the real table
over zero critiques, which fires rows 5/6 and **escalates every baseline run**.

The second is worse than it sounds. It would make B0-B2 look like they were carefully
declining to ship, when in fact they have no mechanism to decline anything, and M5 would
read as a score of zero rather than as the absence of a capability. docs/07's own results
table already writes `n/a` there.

So the arms return an `ArmRun` with `outcome: Decision | None`, and B3's is built from its
real `Report`. Nothing about the tribunal's path changes, and the baselines stop being described
in a vocabulary they do not have.

The related deviation, recorded because it favours the baselines: docs/07 describes B0 as a
call saying "fix the bug in this file", and these arms use the **real Coder prompt and the
real VALIDATE loop**, differing only in what they are shown and who reviews them. The
question is whether the *debate* adds anything; a baseline handicapped by a worse prompt
would answer a flattering version of it.

### 48. M7 was the shared client's running total

`_end` took each arm's cost from `client.total_cost_usd`. One `LLMClient` is shared across a
sweep, so that is the total for everything run so far: serially, each arm appeared to cost
what every earlier arm cost as well; concurrently — which is the default, with a semaphore of
4 — the number depends on scheduling and is not a cost at all.

Cost now comes from the run's own trace, summed over its own `usage` events. The same
property the eval needs for `--replay` turns out to be the only correct source for M7: the
writer is per-run, the client is not.

### 49. `tool_run` recorded a count, and M1 needs the findings

A critique cites a grounding finding by **id**. M1's `rule` locator asks "did any reported
issue cite a finding whose rule is `B602`" — which needs the id-to-rule mapping, and
`tool_run` carried only `findings_count`.

The consequence was arm-specific and would have been very hard to spot in a results table:
B0-B2 hold their findings in memory and score fine, while **B3** — the only arm whose
findings have to come back out of a trace — would have scored zero on every `rule` locator.
A tribunal that looks worse than a single call at finding the thing a linter already flagged is
exactly the kind of result that gets believed.

Found the same way as § 32 and § 45: by writing the consumer first. `tool_run` now carries
the normalised findings, which the viewer's grounding tab wanted anyway.

### Three bugs the tests found in the tests

Worth recording because each one is the harness being *correct*:

* A critic cited a finding its own patch had removed. `validation.py` rejected it — the
  refs resolve against the **patched** report, not the baseline. The fix was to write a more
  realistic script, where round 1 patches something else and the injection survives for the
  critic to cite.
* A scripted Postmortem described one round on a run that took two, and
  `validate_postmortem` rejected it. That check exists so a write-up cannot skip a round.
* A scripted Arbiter was missing entirely from a run whose round 1 rejected.

None of these needed a product change. A harness whose own fixtures keep tripping the
product's validators is a harness testing the right thing.

## Corrections found while wiring the judge and replay

### 50. Blinding the judge is the one thing no results table can reveal

docs/07 asks that the judge be "blind to system identity", on the grounds that "a judge that
knows which arm is 'the tribunal' will flatter it". What makes this different from the other
requirements is that **there is no way to detect a violation afterwards**. A leaked arm id
shifts every column a little; the table stays internally consistent and looks exactly like a
table produced by a blind judge.

So it is enforced by the type. `JudgeQuestion` has no `arm` field — there is nowhere to put
one — and `blind()` additionally replaces the real `Issue.id` with a positional label. The id
is arm-independent today, which is precisely why passing it through would be the kind of leak
that appears later, when something arm-specific enters the derivation and nobody re-checks.

Two tests assert no arm identifier reaches the rendered question or the request the client
actually sends.

### 51. Kappa is published, so it is computed here rather than imported

`agreement()` is twenty lines of arithmetic that could have been `sklearn.metrics.cohen_kappa_score`.
docs/07 makes publishing that number worth more than any headline result — "the sentence that
tells a reader the other numbers mean something" — which inverts the usual build-or-borrow
calculation: a published statistic whose computation a reader cannot inspect is worth less
than the twenty lines cost.

Two edge cases have deliberate answers rather than exceptions. Perfect agreement on a single
category gives chance agreement of 1.0 and kappa of 0/0; it reports 1.0 and lets `n` and the
category count tell the reader the statistic is not saying much. An **empty** label set
reports 0.0, not 1.0 — a judge nobody has checked must not read as perfectly agreeing.

### 52. A filename cannot identify a tribunal run

`--replay` re-scores a sweep from its traces, which means matching each trace back to its
(arm, case). The first implementation parsed the filename, `B1-<case id>-<run id>.jsonl`,
with a fiddly prefix match because case ids contain dashes.

It silently skipped **every B3 run**. The tribunal arm goes through the real `Orchestrator`, which
names its own trace `<run_id>.jsonl` and knows nothing about arms — so a replay re-scored only
the baselines and produced a table with the tribunal column simply absent. Nothing about that
output says "the tribunal was not re-scored"; it looks like a sweep that did not include B3.

Traces are self-describing (docs/06 principle 3), and every arm already writes
`argv=["eval:<arm>", "<case id>"]` into its header. `_identify` reads that. A trace whose argv
does not start with `eval:` is skipped, so an ordinary `tribunal run` output sitting in the
directory cannot invent a row.

### 53. The orchestrator writes `summary.jsonl` next to its traces

`Orchestrator.run` appends a row to `<trace_dir>/summary.jsonl` — the per-run dataset docs/06
asks for. The eval points `trace_dir` at `results/<ts>/traces/`, so the sweep's own trace
directory acquires a file that is not a trace, and `--replay` choked on the first one it read.

Excluded by name, and any other unreadable file is skipped rather than failing the re-score: a
directory people copy traces into will collect strays, and a replay that dies on one of them
is a replay nobody runs.

### Three fixture bugs, and one that was worth the noise

The scripted-provider fixtures tripped the product's validators three more times — a critique
with `verdict: "clean"` and issues in it, a write-up whose `outcome_echo` disagreed with the
run, and a script one entry short because a repair retry had consumed it. All three were the
harness being wrong.

The first is worth recording: the helper chose `"block"` when any issue was `high` and
`"clean"` otherwise, so a `medium`-only critique came out as `clean`-with-issues. The schema
caught it, the repair retry ate the next scripted response, and the run then failed two calls
later with a message about a `PostmortemNote`. The distance between the cause and the symptom
is the argument for the schema guard existing at all.

## What authoring 24 cases revealed about the grounding

### 54. The performance dimension is almost entirely ungrounded

Writing the benchmark out in full made a property visible that no amount of reading the tool
docs had: **of 29 declared defects, 14 have a `rule` locator and 13 of those 14 are
security.** The one exception is `RUF005` in case 024.

That is not a gap in the cases. It is what the tools cover. `bandit` is a security linter by
definition, and `ruff`'s `PERF` rules are narrow — they do not fire on a wrong data structure
(002, 020), invariant work hoisted out of a loop (017, 023), redundant I/O (018), or an
unnecessary `deepcopy` (019). Every one of those ends up with a line-range locator, and the
critics reviewing them have no tool findings to cite either, so their evidence is
`code_span` and their issues are novel by construction.

Three consequences worth stating before any number is published:

* **M1 is two metrics wearing one name.** Security recall is largely settled by mechanical
  rule matches; performance recall rests on line ranges and, where those fail, on the judge.
  The results table reports the mechanical share on its own row for exactly this reason,
  and the two dimensions should not be read as equally well evidenced.
* **The "isn't this just a linter wrapper?" question answers itself on this half.** An arm
  that only re-rates tool output scores near zero on the performance cases. That is a
  stronger demonstration than the re-rating rate, and it was not designed in — it is a
  consequence of what `bandit` and `ruff` are.
* **The Profiler is the agent most exposed to ungrounded reasoning**, and `Issue.evidence`
  with `min_length=1` does not save it: a `code_span` is grounded by the schema's definition
  while being, in substance, the critic pointing at a line it has opinions about. docs/11 R3
  is measured against the security half far more meaningfully than against this one.

### Cases written to test the harness as much as the tribunal

Several cases exist to make a *pair* of numbers interpretable rather than to add recall:

* **009 and 019** are the same shape with and without the defect — a module-level structure
  copied per item. 009 omits the copy and is a security bug; 019 performs it and is merely
  slow. An arm that reports a security issue on 019 has misread correct defensive code as
  the bug it prevents.
* **006 and 016** are both hardcoded credentials and separate two questions that are easy to
  conflate: will the critic back down when the file argues with it (006, with three true
  excuses and both tools reporting), and will it find the thing unaided (016, where the
  parameter is called `key` and `bandit`'s name-matching rule therefore misses it).
* **014 and 021** are both re-rating tests pointing in opposite directions: `random` for a
  reset token should be rated *up* from the tool's `low`, and a quadratic CSV build in an
  audit export should be rated *down* to `low`. An arm that reports everything at `high` has
  found both and calibrated neither.
* **024 declares two defects and contains three.** The unclosed file handle is real, minor
  and deliberately undeclared, so the right answer includes something the case does not
  claim — which is what `is_real_issue` with no match is for.

## Corrections found while verifying the exit codes

### 55. Two of the five documented exit codes were unreachable

`run` derived its exit code from `Decision` alone. Policy returns `Decision.ESCALATE` for an
unusable input *and* for a budget breach, so `FAILED` (3) and `BUDGET_EXHAUSTED` (4) could
never be produced — both documented in docs/08 and in the README, both dead. A pipeline
keyed on exit 4 to page someone about cost would never have fired, and a run that produced
nothing reviewable was indistinguishable from one where the critics simply could not agree.

The fix needed a fact the `Report` did not carry. `TerminalState` was declared in
`contracts.py` and used nowhere — a third dead definition found the same way as § 44 and
§ 45, by writing the consumer. `Report.terminal_state` now carries it, derived from
`run_end`, and `exit_code_for` checks in order: FAILED terminal first, then the
`budget_exhausted` rule, then the outcome.

The ordering is the substance. A run that produced nothing reviewable is `FAILED` whatever
its decision says, and "ran out of money" is a different thing for a pipeline to react to
than "the critics could not agree" even though policy calls both an escalation.

### 56. `replay` exited 0 on a failed run, which made the contract untestable

docs/09 asks for the exit codes to be "verified in a shell script". They could not be:
codes 0-4 describe run outcomes, `run` is the only command that produced them, and a run
costs money. A shell script that spends money is a shell script nobody puts in CI, so the
contract would have stayed unverified.

`replay` now exits with the outcome of the run it is reporting. That is the right semantics
independently — the exit code describes the run being reported, whichever command reports it
— and it makes the whole matrix checkable offline from five committed trace fixtures. It also
gives CI something it did not have: re-checking a recorded run and failing on its outcome.

`scripts/check-exit-codes.sh` is fifteen checks and takes about a second. Unlike
`scripts/verify-docker.sh`, this one has been run.

### The `5` in docs/08, resolved

docs/08 specified `5` for a usage error; the code has always used `64`. Keeping `64`:
it is `EX_USAGE` from `sysexits.h`, and it leaves the 0-9 range to run outcomes. Adding a
sixth outcome later would collide with `5`, and a pipeline keyed on it would silently begin
reading a real result as a usage error. docs/08 amended rather than the code.

### 57. The results report needs no JavaScript, and that is the interesting part

The trace viewer is JS-driven for three reasons: a trace is large, it is interactive, and it
is full of model-written source that has to be rendered inertly. `report.html` has none of
those properties — a few dozen counts, one expandable detail per case, and no untrusted code
in it. So it is rendered in Python with no script tag at all, which removes the entire class
of problem `viewer.js` has to defend against (§ 34) rather than defending against it twice.

Escaping still applies: case ids, model names and error strings all reach the page, and
there is exactly one function that emits markup from text.

The content decision worth recording is the **per-case matrix**. `summary.md` can carry the
aggregate table; it cannot usefully carry one row per case and one column per arm. That view
is what separates "B3 beats B1 by two" from "B3 beats B1 on the two conflicting cases and is
level everywhere else" — and at n=16, where one case is six points, those are the same number
and completely different findings.

## Corrections found while building CI and changing the NIM default

### 58. Changing the default model silently disarmed the Coder's acceptance criterion

The NIM default moved from `nemotron-3-nano-omni-30b-a3b-reasoning` (30B *A3B*, ~3B active
per token) to `llama-3.3-nemotron-super-49b-v1.5` (dense 49B). Every judgement this system
asks for — re-rating a finding in context, deciding whether two remedies genuinely conflict,
adjudicating a Coder's pushback — is the kind that scales with active parameters, so the
larger model is the right default when the output is a number someone will quote. The nano
model stays priced and is the better pick for a smoke run.

The switch broke something invisible. `tests/test_coder.py` builds its settings from
`examples/nim.toml`, which now names the new model; cassettes are keyed on a request hash
that includes the model; so every lookup missed. And a miss was handled like this:

```python
except Exception as exc:  # a cassette miss is a skip, not a failure
    if "no cassette" in str(exc):
        pytest.skip(...)
```

The suite went from asserting docs/03 § 3.1's acceptance criterion — six fixtures, patch
applies, parses, and touches the declared lines — to asserting nothing, and the only trace
of it was the skip count moving from 33 to 39 in a run that still said "passed".

Two changes, and the second matters more than the first:

* The cassette suite **pins the model it recorded against**. A replay test is about a
  recorded exchange, not about whatever the example config routes to this month.
* A cassette miss is now a **failure**. With the model pinned, the only things that can
  cause one are a prompt or schema change, which is precisely what the suite exists to
  notice. A skip was the wrong shape for it: skips are how a test stops testing without
  anyone deciding that it should.

The generalisation: any replay fixture keyed on a mutable default disarms itself the day
the default moves, quietly. Worth checking wherever cassettes are used.

**The measurements do not transfer.** Everything docs/13 records about "Nemotron" — the
Coder's 5/6, the quote-mangling in dense replacements (§§ 18, 20), the first-run
`tools_consulted` confusion — was measured on the nano model and is labelled as such. None
of it has been re-measured on the 49B, and a default change is not a measurement.

### 59. A CI gate that cannot run should be red, not green

`--smoke` is the per-PR gate, and it replays from cassettes. There are none for the eval's
requests, so it cannot run. Three options: skip it (a gate that passes when it cannot do its
job), mark the job `continue-on-error` (a gate that is a notification), or let it fail.

It fails, and it says why:

```
this gate is unarmed: no cassette matches these requests in tests/cassettes.
Record them once against a real provider, then commit them:
  TRIBUNAL_LLM__MODE=record NVIDIA_API_KEY=... tribunal eval --split dev ...
Until then `--smoke` fails on purpose.
```

Two smaller fixes came with it. The first attempt at the diagnostic checked whether the
cassette directory was empty — it is not, it holds the Phase 2 recordings, so the check
never fired and the failure stayed a wall of `CassetteMiss`. The real signal is *every* run
failing with a miss. And `--smoke` was writing a results directory into `eval/results/`; a
gate is not a measurement, so it writes to a temp directory and CI stops littering.

### The security property in the CI configuration

`pull_request` runs a fork's code without the repository's secrets. `pull_request_target`
runs it *with* them, in the base repo's context. Swapping one for the other looks like a
triggering fix, hands the API key to anyone who can open a pull request, and produces a
workflow run that looks entirely normal.

So `ci.yml` references no secret at all, and `tests/test_ci.py` asserts it — along with the
converse, that the two credentialed workflows can only be reached by `schedule` (default
branch only) and `workflow_dispatch` (write access only). It is the one property of a CI
configuration that cannot be checked by looking at a run afterwards.

## Closing the housekeeping

### 60. The § 58 sweep found nothing else, and the guard matters more than the sweep

§ 58 generalised: any replay fixture keyed on a mutable default disarms itself the day the
default moves, quietly. The sweep found exactly two modules that read the committed
cassettes.

`tests/test_coder.py` was the instance, and is fixed. `tests/test_llm_live.py` is **immune by
construction**: its replay tests build each request from the *recorded* request — model,
system, user, max_tokens, effort all come out of the cassette — so there is no default to
follow. That is the shape to copy, and it is worth naming: a replay test that reconstructs
its request from the recording cannot drift, and one that reconstructs it from configuration
always can.

A one-time sweep does not stop it recurring, so two guards went in beside the fix. The pinned
`CASSETTE_MODEL` must appear among the committed cassettes, and every fixture must have a
cassette that mentions it. Both fail by name rather than by turning into skips, which was the
original sin.

### 61. `--error PATH|-`, the seventh consumer with no producer

`CoderBundle.traceback` is rendered by `_traceback_block`, listed in docs/03 § 3.1 among the
Coder's inputs, and specified in docs/08's CLI table as `--error PATH|-`. Nothing set it.

Unlike §§ 32, 44, 45, 49 and 55, this one was a *missing feature* rather than a dead guard —
the consumer was correct and waiting. Built rather than cut: it is specified in two documents,
the rendering already existed, and a traceback is the most natural way to hand the Coder a
starting point. `-` reads stdin, so `pytest 2>&1 | tribunal run thing.py --error -` works.

One detail found while wiring it: the validation belongs **before** the provider check. A
user who typos the path should be told about the typo, not about credentials — the same
ordering `--test requires --allow-exec` already used, and the opposite of what the first
version did.

### 62. Unticked boxes next to a working system are debt nobody can act on

docs/09's Phase 0 asked for a throwaway two-agent script. It was never written: the real
system overtook it before there was a reason to. Four boxes therefore sat unticked for the
whole project next to a system that does all four things, which reads as incomplete work
rather than as a plan that was outgrown.

They are now struck through, each annotated with what actually closed it — parallel critics
with Pydantic validation everywhere, the four-provider schema dialect work in
`llm/schema.py`, the cache-prefix stability check that *rejects* a volatile system prompt at
construction. One is honestly still open: the cache-read number needs a live sweep, and
saying so is better than ticking it.

## Corrections found while building the MCP server

### 63. The `[verify]` marker was right, and the sketch it guarded was already wrong

docs/08-packaging.md sketches the server as `from mcp.server.fastmcp import FastMCP` and
annotates the section: "**[verify]** the exact config file location and schema per host at
implementation time — these have moved more than once."

They had. The installed SDK is `mcp` **2.2.0**, where `FastMCP` is `MCPServer` in
`mcp.server.mcpserver`, and `Tool.inputSchema` is `Tool.input_schema`. The v1 import raises
a `ModuleNotFoundError` carrying a migration URL, which is a decent thing for a library to
do and still leaves every line of the sketch unrunnable.

Worth being precise about what the marker bought. It did not prevent the drift and it could
not have. What it did was make the drift *expected*: the hour spent probing the installed
package before writing anything was budgeted rather than surprising, and no part of the
design had to be rethought because the disagreement was confined to two import lines. A
design note that says "this will be stale, check it" is doing its job when it turns out to
be stale.

The dependency is pinned `mcp>=2.0` rather than left open, so an environment holding 1.x
fails at resolution. Unpinned, it would fail at import — inside a host's stdio pipe, where
the user sees a server that starts and lists no tools.

### 64. Availability was checked against providers the run would not use

The first version asked `registry.build(...)` for each agent's provider and called
`available()` on the result. The run then built its own `LLMClient`, which built its own
providers. Two sets of objects, and only the second set ever made a call.

Nothing was wrong with the *answer* in production, because both sets are built from the same
config. It was wrong as a structure: a pre-flight check that does not interrogate the thing
that will do the work is a check that can agree with nothing. It also made the server
untestable — a scripted provider seated in a client was invisible to the check, so the one
path worth testing (the full tribunal, no API call) was unreachable through the tool.

It now asks `client.providers`, and `build_server` takes the client. The seam is the same
one `Orchestrator` already offers, for the same reason.

### 65. Three consumers had each re-derived "skip summary.jsonl"

`traces/summary.jsonl` is the aggregate row `append_summary` writes beside the per-run
traces. Two places in `eval/runner.py` excluded it by string literal. The MCP server's
`get_trace` was about to become the third, and its first version did not — so a bare glob
offered `summary` in the "recent runs" list, pointing the caller at a file that parses as
neither a trace nor a run id.

Replaced with `trace.writer.traces_in(directory)`, next to the constant that names the file.
The rule now lives once, beside the function that creates the exception to it.

This is the same shape as the bug class that dominates these notes, rotated: not a consumer
with no producer, but *one producer with several consumers that each re-implemented its
contract*. Both fail the same way — a change at the source that the copies do not hear
about — and both are invisible to tests, because every copy is individually correct on the
day it is written.

### 66. Four fixture bugs, all of them the product's validators working

Getting one scripted run through the MCP path took four tries, and every failure was the
test harness tripping a check that exists for a reason:

- `ArbiterNote` with `consolidated_critique=None` on a REJECT — the schema requires the
  instruction set to exist exactly when the Coder needs one.
- A Postmortem write-up echoing `escalate` for a run that ended REJECT. Running out of
  *rounds* is a rejected final patch; ESCALATE is the cost and time budget. Conflating them
  is precisely what `_check_outcome_echo` is for.
- A write-up omitting the still-open security issue, caught by
  `_check_uncertainty_is_accounted_for`.
- A test helper globbing `*.jsonl` and finding `summary.jsonl` — § 65, from the other side.

None were product bugs. The pattern is now familiar enough to state as a rule: when a
scripted run dies of provider exhaustion, the fixture is lying about the run, and the
validator that caught it is the one worth reading first.

## Writing the getting-started guide, and testing it

### 67. Four things in the docs were wrong, and a test found all four

`start.md` describes how to run the system. Writing it meant restating a lot of things the
code already knows, so `tests/test_start_doc.py` checks the restatements: commands exist,
flags exist on the commands they are shown under, the tables match their sources, the links
resolve, and the MCP signature matches the server's advertised schema.

It failed on its first run, four times:

- **The baseline arms were mislabelled.** The guide said "B1 sequential, B2 no-grounding".
  `eval/arms.py` says B1 is one call *plus the grounding output* and B2 is Coder → one
  critic → Coder. B1 is the comparison the whole eval rests on, described as the wrong
  thing.
- **The exit-code table omitted `reject`.** `EXIT_FOR_OUTCOME` maps both `reject` and
  `escalate` to `2`; the table listed only `escalate`. A run that ended with a rejected
  final patch exited with a code the guide named as something else. `run --help` had the
  same gap and was left that way for a day — see § 69.
- **The Docker invocation named a service that does not exist** (`tribunal`, not `tribunal`)
  and mount paths that do not exist (`/work`, not `/code` and `/traces`).
- **The sample grounding output was from a different file** than the command above it.

Every one of these was written in the same sitting as the code it describes, by someone who
had just read that code. That is the argument for the test: care does not survive
restatement, and none of these are catchable by reading.

The README's own status paragraph, meanwhile, had been claiming "Phases 1–3 done ... what
remains is the evaluation harness" and "1393 tests" for most of the project. Now corrected —
and the test count is stated as a **floor** (`1,600+`), checked against a real collection
rather than a second hard-coded number. An exact count obliges every commit that adds a test
to edit the README, which is a rule nobody follows and therefore a number that is always
wrong.

### 68. `exclude` in a ruff config replaces the defaults; `extend-exclude` adds to them

`ruff check src tests` was always clean, but the obvious `ruff check .` reported seven
errors in `eval/cases/` — which is the benchmark working as designed, since every case is a
planted defect. Worse, five were marked `[*] fixable`, so a passing `--fix` would have
deleted the thing each case exists to test.

The fix is an exclude. The first attempt used `exclude = ["eval/cases"]` and turned
`ruff check .` into a 37 MB lint of the entire virtualenv, because `exclude` *replaces*
ruff's built-in list (which is what was hiding `.venv`). `extend-exclude` is the additive
one.

The check worth keeping is the second half of that test. `grounding/ruff_t.py` runs ruff
with `--isolated`, so no `pyproject.toml` on the filesystem can affect a case's findings —
which is the only reason this exclude is safe. Drop that flag and every benchmark case
silently loses its ruff findings while the sweep goes on reporting scores. Lower ones, for
no visible reason. The test asserts both halves together, because either alone is fine and
the combination is the hazard.

### 69. The ledger's test is what closed the last item in it

§ 67 ends by noting that `run --help` carried the same missing-`reject` row as the guide,
and then does not fix it. `REMAINING.md` § C was written around that one entry.

The entry came with `test_the_one_unfixed_item_is_still_unfixed`, which asserts in **both**
directions: while the section exists the gap must be real, and the moment `run --help`
starts documenting `reject` the test fails with *"REMAINING.md § C1 is stale, delete it"*.
Fixing the help text turned the ledger red, which is the only reason the ledger is accurate
now rather than three edits behind.

That is the shape worth keeping. A file describing outstanding work rots in a way that is
worse than an ordinary stale doc: a resolved entry sends a reader to inspect something
already fine, and every remaining entry becomes less believable for sitting next to it. The
fix is not discipline — it is a test that fails when the world catches up with the document.

The help text now lists all six codes and says why `2` covers two outcomes, and `replay`
gained the one sentence it never had: it exits with the code for the run *in the trace*, on
the same contract, so a recorded run can be re-checked in CI without re-running it. That
behaviour was always true and documented nowhere.

## The rename

### 70. `debug-crew` named the wrong half of the system

The project was called `debug-crew` until this point. Both halves of that name were wrong:
*debugging* is the task, not the thesis, and the system does review, patching and
adjudication rather than debugging; *crew* is generic agent-framework vocabulary for a
project whose central argument is that **the agents do not decide anything**.

Renamed to **`tribunal`**. A tribunal hears opposing cases and rules by procedure rather
than by the panel's mood, which is exactly a twelve-row decision table with `rule_fired`
recorded in the trace, and a tribunal may return *unresolved* — the `TRADEOFF` state. It was
the only candidate that named both headline properties at once. `counterpoint` was the other
and the close runner-up; `quorum` was rejected outright for implying consensus, which is the
opposite of the argument.

Done now because there is no git history and no user, so it was the cheapest it would ever
be. 122 files rewritten, plus 42 more for the prose word *crew* on its own.

### 71. What the rename could have broken, and why it did not

Two things were worth checking before touching anything, both of them cases where the
project's name is *data* rather than code:

**Cassettes.** `request_hash` covers provider, model, system prompt, user prompt, schema,
`max_tokens`, effort and the cache flag. No prompt mentions the project by name — checked,
not assumed — so every recorded response still keys to the same hash. Had a single prompt
said "debug-crew", the rename would have silently invalidated every cassette, and the
symptom would have been `--smoke` failing for a reason with no visible connection to the
change.

**Recorded traces.** `tests/fixtures/traces/*.jsonl` carry the old name in `argv`, in the
sandbox image name and in the `debug-crew-sbx-` scratch-directory prefix. Those are fixtures
under our control and were rewritten with everything else, so they continue to match what
the code now produces.

Ordering was the other hazard: `debug-crew` is a prefix of `debug-crew-sandbox` and
`debug-crew-mcp`, so a naive replace would have produced `tribunal-sandbox` only by accident
and `tribunalsandbox` by bad luck. Longest pattern first, every time.

### 72. Renaming to an ordinary English word broke a test that had never been right

`tests/test_start_doc.py` checks that every command `start.md` tells the reader to run
actually exists, by finding `<name> ([a-z-]+)` in the text. That pattern was never
*correct* — it matched any word following the project name — but it was *safe*, because
`debug-crew` is not a word and could not appear in a sentence.

`tribunal` is a word. The moment the rename landed, the test reported `tribunal cannot`,
`tribunal proposes` and `tribunal with` as missing subcommands. The fix is to match only
invocations: start of line, after a shell prompt, or inside backticks.

The general point is worth keeping. A test can pass for years on a property of the *data*
rather than a property of the *check* — here, "the project name never occurs in prose" —
and nothing distinguishes the two until that property quietly stops holding. This is the
same shape as § 65's duplicated `summary.jsonl` rule and § 58's fixture keyed on a mutable
default: correct today, load-bearing on an assumption nobody wrote down.

Three other things the rename surfaced, none of them bugs: `ruff` re-sorted thirteen import
blocks, because `tribunal` sorts after `tests` where `debug_crew` sorted before; the
virtualenv had the old absolute path baked into every shebang, `pyvenv.cfg` and the editable
install's `.pth`, so moving the directory broke it until those were repointed; and a stale
`debug_crew-0.1.0.dist-info` sat alongside the new one until it was removed, which would
have left two editable installs racing to provide the same package.
