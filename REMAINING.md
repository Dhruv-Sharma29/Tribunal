# What is not done, and why

A single honest ledger of everything outstanding. It exists because "remaining work" and
"work I chose not to do" and "work I could not do here" are three different claims, and a
checklist that mixes them tells a reader nothing.

Four categories, in descending order of how much they should bother you:

- **[A] Blocked on environment** — built, tested against a scripted provider, never *run*.
  Needs an API key or a Docker daemon. This machine has neither.
- **[B] Deliberately not built** — a judgment call, with the reasoning, so you can overrule it.
- **[C] Found and left** — I knew about it and did not fix it. **Now empty.**
- **[D] Worth building next** — nothing is blocking these and nothing is wrong with them
  missing. They are ranked, with what each one buys. Added when `tribunal code` landed,
  because a surface that new has obvious next moves and a ledger that only records
  *blocked* and *rejected* has nowhere to put them.

**Nothing offline is unbuilt, and nothing offline is knowingly broken.** Every piece of
machinery that can be written and tested without a credential is written and tested: 1,800+
tests, ruff clean, zero API calls by default. What remains is almost entirely *execution*, not construction — which is a
comfortable position to be in, and also the position in which it is easiest to overstate how
finished something is. Hence this file.

[`RUNBOOK.md`](RUNBOOK.md) is the companion: it has the exact commands, in order. This file
says what each one is *for* and what it would cost to skip.

---

## [A] Blocked on environment

### A1. Verify the container images — closes **S7**

```bash
./scripts/verify-docker.sh              # build + checks needing no credential
./scripts/verify-docker.sh --with-llm   # also one real run
```

**Needs:** a Docker daemon. **Unblocks:** charter criterion **S7** ("one command runs the
whole thing on a clean machine"), and the last box in Phase 4.

The two-image split — credential in one, code execution in the other — is a security
boundary, and the script checks it as one: the sandbox image holds no tribunal code, neither
image has anything credential-shaped in its layer history, both run unprivileged, pinned
tool versions match `constraints.txt`.

**Expect it to find something.** An unbuilt Dockerfile is a hypothesis. The script's shell
syntax is checked and its two grep assertions were validated against real `tribunal doctor`
output including a negative control; everything else is unexercised. I would not be surprised
by a first-run failure and neither should you be.

### A2. Record the cassettes — unblocks the `smoke` CI gate

**Needs:** an API key. **Unblocks:** `tribunal eval --smoke`, which is the per-PR CI gate.

Four prompts have live tests and no recordings — `arbiter/v1`, `arbiter_affirm/v1`,
`postmortem/v1`, `judge/v1` — and the eval's own requests have none either. So `--smoke`
**fails today, on purpose**, with the recording command in its error message. A gate that
passes when it cannot do its job is worse than one that is red.

This is the one place the roadmap's own advice was not followed: *"record cassettes as you
build each agent, not in a batch at the end."* The cost is exactly what it predicted. If you
do one thing from this file, do this one — it is the cheapest and it turns a red CI gate
green.

> The committed cassettes were recorded on `nemotron-3-nano-omni-30b-a3b-reasoning` and the
> default is now `llama-3.3-nemotron-super-49b-v1.5`. `tests/test_coder.py` pins the old
> model **on purpose** (docs/13 § 58). Do not "fix" that by following the default — the pin
> is what stopped a silent 33→39 jump in skipped tests from reading as a pass.

### A3. Sweep the dev split

**Needs:** an API key, ≈ **$22–28** for a full 24-case sweep (docs/10); the dev split is 8
cases × 4 arms, so proportionally less. **Unblocks:** A4, A5, A6 and every number below.

### A4. Validate the judge — gates everything held-out

**Needs:** A3, plus **a human labelling 30 rows by hand**. `tribunal judge-labels` writes
the worksheet offline; the labels have to be filled in *before* `judge-kappa` runs, because
labelling after seeing the judge's answer measures agreement with an anchor and nothing
downstream can detect that it happened.

Exits non-zero below **κ = 0.6**. `heldout.yml` refuses to start without the resulting file.

Publishing this number is worth more than any headline result — it is the sentence that tells
a reader whether the other numbers mean anything.

### A5. Freeze the thresholds

**Needs:** A3. `accept_threshold`, `no_progress_epsilon`, `Severity.weight`, tuned on the
**dev split only**, then stopped. Changing them after publishing invalidates the published
number, and nobody wants to re-run a $25 sweep.

Re-examine **case 009** here. Its own `notes.md` flags it: if a reviewer can name a clean fix
satisfying both concerns, it belongs in `both_independent`, and a spurious conflicting case
inflates **M5** — the metric the project leads with.

### A6. Promote a baseline

**Needs:** A3. Arms `nightly.yml`'s regression gate, which currently skips the check because
the baseline is absent.

### A7. The held-out sweep — closes **S5**

**Needs:** A4, A5, A6. Manual-only, requires a stated reason, **once per milestone**.

docs/07: *report every held-out run you did, not the best one.* If you tune anything against
held-out results, say so and demote them to dev. The split is spendable exactly once; that is
the whole reason it exists.

### A8. Register the MCP server in a real host

**Needs:** a key and an editor. Independent of A3–A7. **RUNBOOK step 8.**

Narrower than it was. The stdio transport is now exercised offline —
`tests/test_mcp_stdio.py` launches the server as a subprocess, completes a real
`initialize` / `tools/list` handshake, and asserts the first byte on stdout is protocol
rather than a banner. I had listed this whole item as blocked; most of it was not, it was
just untested. What genuinely needs a host and a key: `review_code` end to end from an
editor, the per-host config schema (docs/08's `[verify]`, and docs/13 § 63 is what happened
last time it moved), and whether a multi-minute tool call is tolerable in a given host's UI.

### A9. The three Phase 6 artefacts

| | needs |
|---|---|
| Eval results table, with n, caveats and the cost row | A3 or A7 |
| Demo GIF of a run ending in **TRADEOFF** (the accept path is boring) | a real run |
| CI badge | the repo pushed to GitHub — **this is not a git repo yet** |

The README deliberately contains **no results table** today, and its Design decisions section
says so rather than leaving a gap where one should be.

### A10. Acceptance criteria that cannot be closed from here

| | status |
|---|---|
| **S3** decision reproducible from the trace, no LLM call | **done** — `policy.py` is pure, covered |
| **S6** any run replays byte-identically | **done** — by construction; one `report.build`, both paths call it |
| **S1** every run terminates in a defined state | tests pass; the *sweep* half needs A3 |
| **S2** every issue carries checkable evidence | schema enforces it; the `%` ungrounded needs A3 |
| **S4** the system can reject | fixtures cover ESCALATE and TRADEOFF; the outcome *distribution* needs A3 |
| **S5** tribunal beats a single-call baseline | needs A7 |
| **S7** one command on a clean machine | needs A1 |
| **S8** a reader can explain the disagreement handling after reading the README | **needs two people.** The charter says *"Ask two people. Actually do this."* I am not two people, and I wrote the README, which disqualifies me twice. |

### A11. Run the coding agent against a real model

**Needs:** an API key. **Unblocks:** the only open question about `tribunal code`.

The tool loop, the workspace boundary, approval, undo and the whole NIM wire path are
exercised end to end — against a local endpoint speaking the real OpenAI protocol, which
covers the SDK, `reasoning_effort`, the `json_schema` response format, `reasoning_content`,
the inline `<think>` recovery and a 503 retry. A two-bug fixture went from `2 failed` to
`2 passed` with the agent driving.

What has never happened: **a real model emitting a `Step`.** Every step in every test was
scripted. The open question is narrow and measurable — can a given model reliably fill a
flat, one-tool-per-turn schema, and does it pick sensible tools — and it is exactly the
condition this file warns about at the bottom: a code path that has never met a real
provider.

`--log session.jsonl` records `parse_retries`, `structure` and `duration_ms` per step, so
the answer is a number from the first run rather than an impression. Two failure shapes to
watch for specifically:

- a rising `parse_retries` on the smaller models, which would mean the flat schema is too
  wide for them and the tool set should shrink;
- `structure: extracted` on every row, which would mean the JSON is arriving wrapped in
  prose and being recovered — working, but a round-trip more expensive than it looks.

```bash
NVIDIA_API_KEY=… tribunal code --config examples/nim.toml --log /tmp/session.jsonl \
  --print --yes "fix the failing test in cart.py"
```

---

## [B] Deliberately not built

Both are Phase 7, both optional, and I would make the same call again — but they are calls,
not blockers, so overrule them freely.

### B1. `AGENTS.md`

**Skipped.** Its entire content would be per-host conventions: which hosts read a root
`AGENTS.md`, how precedence works between nested ones, where each host looks. I cannot check
any of that from here — no network — so the file would be `[verify]` markers end to end.

The MCP server's own experience is the argument. docs/08's code sketch was written against
`mcp` 1.x and did not import on the installed 2.2.0. A document that *guesses* at host
conventions and is read as authoritative is worse than no document, because the guess is
invisible. docs/08 § *MCP server* already covers registering the server in a way that does
not depend on guessing.

### B2. VS Code extension

**Skipped**, on docs/08's own reasoning: it is *"the same capability as the MCP server with
more maintenance."* The MCP server already makes the real tribunal callable from VS Code, Cursor,
Windsurf and Claude Code. An extension would add a webview and a publishing pipeline and no
new capability.

---

## [C] Found and left

**Empty.** The one entry here — `run --help` omitting `reject` from its exit-code table — is
fixed.

<details>
<summary>What it was, and how it closed</summary>

`src/tribunal/cli.py` documented the shell contract as *"0 accept, 1 tradeoff, 2 escalate,
3 failed, 4 budget exhausted"*. `EXIT_FOR_OUTCOME` maps **both `reject` and `escalate` to
`2`**, so a run whose final patch was rejected exited with a code the help text named as
something else. Found while writing `start.md`, fixed in the guide, recorded in docs/13 § 67,
and then left in the source.

The help text now lists all six codes, says that `2` covers two outcomes and why, and points
at `tribunal replay` for telling them apart. `replay` — which exits with the code for the
run *in the trace* — now says so in its own help, which it never did.

`tests/test_start_doc.py::test_the_one_unfixed_item_is_still_unfixed` is what closed the
loop. It was written to fail **in both directions**: while the section existed it asserted
the gap was real, and the moment the help text started documenting `reject` it failed with
*"REMAINING.md § C1 is stale, delete it"*. That is the only reason this section is accurate
right now rather than three edits behind.

</details>

---

## [D] Worth building next

Ranked by value per unit of work. None is blocked; none is a defect. Everything here is
about `tribunal code`, because that is the newest surface and the rest of the system has
had its rough edges filed off by 68 numbered findings in docs/13.

### D1. Cache the transcript prefix — the biggest cost win available

Today `LLMRequest` has one cacheable region: `system`. That was the right shape for a
critic, whose volatile half is one round's findings. It is the wrong shape for a coding
agent, whose volatile half is a transcript that only ever grows — so **every turn pays full
input price for every earlier step**, and a forty-step session pays for its own history
forty times. On Anthropic the fix is a second `cache_control` breakpoint at the end of the
frozen part of the transcript; the request already knows which part is frozen, because
`_render_transcript` is the only thing that appends to it. Expect the dominant cost of a
long session to drop by most of itself.

### D2. Resume a session

`--log` already writes every step as JSON. Reading it back into `entries` is most of a
`tribunal code --continue`, and it turns an interrupted session from a loss into a pause.

### D3. Honour `.gitignore`

`grep`, `glob` and `list` skip a static ignore list (`code.ignore_dirs`). A project's own
`.gitignore` is the better list and it is already on disk. Today a generated `dist/` that
is not in the static list costs a context window; worse, a gitignored `secrets.env` is
greppable when the project has already said it should not be.

### D4. A token budget, not just a dollar one

`code.max_usd` cannot stop a runaway session on NIM, because NIM is `not_token_metered` and
every call costs exactly $0.00. The step cap is the only real guard there. `BudgetConfig`
already has `max_tokens` and the client already totals them; the code session should check
the same way.

### D5. `review --test`

The agent's `review` tool runs the tribunal with static grounding only. The CLI's `run`
accepts `--test` with `--allow-exec` and gets a pytest oracle out of it, which is what
turns the Profiler from `unmeasurable` into measured. The tool should be able to pass one.

### D6. Write a real trace, so `tribunal view` works on a session

A coding session emits a JSONL log that only this project's own eyes can read. The trace
layer already has a writer, a reader, and an offline HTML viewer. A session is not a review
and should not pretend to be one, but "render what happened as one HTML file" is a solved
problem here being solved twice.

### D7. Parallel tool calls

One tool call per step is a consequence of the structured-output design (`code/actions.py`
explains why that design was chosen). Reading four files takes four round trips. A
`steps: list[Step]` variant for read-only tools only would cut the latency of the survey
phase without letting two writes race.

### D8. POSIX assumptions

`os.killpg`, `start_new_session` and `readline` are all POSIX. On Windows the timeout path
in `tools._bash` would raise rather than kill. Either guard them or declare the platform in
`pyproject.toml`; today it is neither.


## What this file is not

It is not a list of known bugs. Every bug found during the build was fixed and recorded in
[`docs/13-implementation-notes.md`](docs/13-implementation-notes.md) — 68 numbered findings,
most of them cases where the build disagreed with the plan and the plan was wrong.

The dominant failure mode there is worth repeating, because it is the thing most likely to be
true of the parts nobody has run yet: **a consumer written against a field nothing ever
populates.** Seven instances, every one with passing tests. Section A of this file is a list
of code paths that have never executed against a real provider, which is precisely the
condition in which that bug survives. Expect A1–A3 to find something. That is what running
them is for.
