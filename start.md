# Start here

A guide to running `tribunal` and to what it is doing while it runs. The
[README](README.md) argues for the design; this file gets you to a result.

Everything below runs with **no API key** unless a step says otherwise. That is deliberate:
the grounding layer, the sandbox, patch validation, replay and the viewer are all real work
that happens before any model is called, and being able to exercise them without a
credential is what makes the rest testable.

---

## 1. Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
```

Add a provider only when you want the agents to run:

```bash
pip install -e '.[nim]'            # or [anthropic] / [openai] / [gemini] / [all-providers]
pip install -e '.[mcp]'            # the editor integration, § 7
```

Then check what this machine can actually do:

```bash
tribunal doctor
```

```
tribunal 0.1.0  schema 1.0  prices 2026-09-16  python 3.12.3
┏━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ tool    ┃ version ┃ status                  ┃
┡━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ bandit  │ 1.9.4   │ ok                      │
│ ruff    │ 0.16.7  │ ok                      │
│ radon   │ 6.0.1   │ ok                      │
│ astgate │ 0.1.0   │ built in                │
│ pytest  │ 9.1.1   │ ok (needs --allow-exec) │
└─────────┴─────────┴─────────────────────────┘
```

**Read the sandbox block it prints underneath.** It tells you whether network is blocked and
whether Docker is available — not what the config *asks for*, but what this machine will
actually enforce. A missing tool degrades a critic from grounded to opinionated, and `doctor`
is the only place that difference is visible before a run.

---

## 2. See the grounding, before any model is involved

This is the layer everything else rests on, and it is worth looking at on its own.

```bash
tribunal ground examples/sql_injection.py
```

```
┏━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ line ┃ tool    ┃ rule              ┃ tool severity ┃ message                             ┃
┡━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ -    │ radon   │ CC-SUMMARY        │ -             │ 5 block(s) measured; total          │
│      │         │                   │               │ complexity 14; worst is 'audit' at  │
│      │         │                   │               │ 6 (rank B)                          │
│ 13   │ bandit  │ B608              │ MEDIUM        │ Possible SQL injection vector       │
│      │         │                   │               │ through string-based query          │
│      │         │                   │               │ construction.                       │
│ 13   │ ruff    │ S608              │ error         │ Possible SQL injection vector …     │
│ 23   │ ruff    │ RUF005            │ error         │ Consider `[*out, row]` instead of   │
│      │         │                   │               │ concatenation                       │
│ 34   │ bandit  │ B602              │ HIGH          │ subprocess call with shell=True     │
│ 34   │ astgate │ shell-true        │ advisory      │ passes shell=True, making the       │
│      │         │                   │               │ command string a shell sink         │
└──────┴─────────┴───────────────────┴───────────────┴─────────────────────────────────────┘
13 finding(s) from astgate, bandit, radon, ruff
```

(Trimmed — the real table has 13 rows.) Note lines 13 and 34: **bandit and ruff both flag
each one**, under different rule ids. Deduplicating those into a single "issue" is
`grounding`'s job, and the reason a critic cites a *finding id* rather than a line number.

Four tools, one normalised shape, each finding addressable by a stable id. A critic may only
raise an issue it can tie to one of these rows or to a measurement it actually took. That
constraint is the whole reason the output is worth reading — it is also why the system
reviews *single-file Python* and not your repository. The grounding is the scope.

`--json` gives the machine-readable form. `--test <file> --allow-exec` adds pytest as a
correctness oracle; see § 6 before you pass that flag.

---

## 3. Run the loop

Needs a provider. The cheapest is NVIDIA NIM's hosted developer endpoint:

```bash
export NVIDIA_API_KEY=nvapi-…            # from https://build.nvidia.com
tribunal run examples/sql_injection.py --config examples/nim.toml
```

You will see a live progress panel and then a report. What happened, in order:

| | |
|---|---|
| **GROUND** | the four tools above run on the original file |
| **PROPOSE** | the Coder returns a *minimal* patch as search/replace edits, not a rewrite |
| **VALIDATE** | the patch is applied and re-grounded. A patch that does not parse never reaches a critic |
| **CRITIQUE** | Red-team and Profiler run **in parallel**, neither seeing the other's output |
| **DECIDE** | a pure function — no model — maps the critiques to accept / reject / trade-off / escalate |
| **ARBITRATE** | on a reject, one consolidated instruction set for the next round. On a trade-off, the justification |
| **POSTMORTEM** | the run written up from its own trace |

Useful flags:

```
--test <file> --allow-exec   run pytest as the oracle (see § 6)
--error <path|->             seed the Coder with a traceback; `-` reads stdin
--no-arbiter                 template the consolidation instead of calling the Arbiter — cheaper
--no-postmortem              skip the narrative; the structured report is unaffected
-q                           no live panel; the final report still prints
--trace-dir <dir>            where the JSONL trace lands (default: traces/)
```

**The exit code is the contract**, so this composes with a shell:

| code | meaning |
|---|---|
| 0 | `accept` — the patch was accepted |
| 1 | `tradeoff` — the critics want incompatible things; a human chooses |
| 2 | `escalate` or `reject` — nothing was accepted |
| 3 | `failed` — the input was not reviewable at all |
| 4 | budget exhausted — the cost or time cap stopped the run |
| 64 | usage error; you invoked the command wrongly |

Three of these are worth saying plainly.

**`1` is not a failure.** It is the system declining to manufacture agreement, which is the
behaviour it exists to have. Decide deliberately whether your pipeline treats a declared
trade-off as a stop; it is a distinct code so that you can.

**`2` covers both `escalate` and `reject`**, because for a caller they mean the same thing —
no patch shipped. If you need to tell them apart, `tribunal replay` on the trace prints the
`rule_fired` that produced it. `rounds_exhausted` and `pressure_over_threshold` are different
stories with the same exit code.

**`64` is `EX_USAGE` from `sysexits.h`**, not `5`, so "you invoked this wrongly" stays out of
the range reserved for run outcomes. A sixth outcome later would have collided.

---

## 4. Read the trace

Every run writes one JSONL file, and **that file is the source of truth** — the report you
saw printed was derived from it, by the same function that derives it here:

```bash
tribunal replay traces/01JB….jsonl          # no key, no calls, byte-identical report
tribunal view   traces/01JB….jsonl          # one self-contained HTML file
```

```
$ tribunal replay tests/fixtures/traces/tradeoff.jsonl
01M36RXVMQFAM4XDEGEJ7RS6QA  t.py  tradeoff via irreconcilable  1 round(s)  $0.0050
┏━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━┓
┃ round ┃ decision ┃ rule_fired     ┃ pressure ┃
┡━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━┩
│ r1    │ tradeoff │ irreconcilable │ 7.2      │
└───────┴──────────┴────────────────┴──────────┘
```

Replay reproducing the report exactly is not a feature that was added — there is only one
`report.build`, and both paths call it. It cannot drift.

Two things worth doing here:

- **`--accept-threshold`** re-decides a recorded run under a different threshold, so you can
  ask "would this have been accepted at 0.7?" without spending anything.
- **`view`** renders the whole debate offline: both critics side by side, what each one
  cited, where they disagreed, and the pressure trajectory per round. There is no network
  request in the file. It is the fastest way to understand a run you did not watch.

The fixtures in `tests/fixtures/traces/` cover all five terminal shapes (`accept`,
`tradeoff`, `escalate`, `failed`, `budget`) and need no credential — a good place to start.

---

## 5. How the decision actually gets made

The part most worth understanding, because it is the part that is not a model.

`policy.decide()` is a **pure function**: grounded findings and both critiques in, a
`Verdict` out. A twelve-row table, first match wins, and the row that matched is recorded as
`rule_fired` in the trace. Nothing about the outcome is a model's opinion about what should
happen — the models supply *findings*, and the table supplies the *decision*.

So when a run says `tradeoff via irreconcilable`, that is checkable. You can read row 8 of
the table, read the inputs off the trace, and confirm it. The same run against a different
model reaches the same verdict from the same findings.

Two detectors decide whether a genuine conflict exists:

1. **Oscillation** — round *n* undoes round *n−1*. A loop, caught structurally.
2. **Same-span** — both critics land on the same lines wanting opposite things. This one is
   *conditional on the Arbiter affirming* that the two remedies really are opposed, because
   two findings on one line are usually just two findings on one line. The affirmation is an
   input to the table, not a decision by it.

When either fires, the run ends in `TRADEOFF`: both remedies, both costs, a recommended
default, and what would reverse it. That is the terminal state the whole design is for.

---

## 6. Executing model-written code

`--allow-exec` is off by default, everywhere, including over MCP.

With it off, pytest never runs and the Profiler reports `unmeasurable` rather than guessing.
That is a real reduction in what the system can tell you, and it is still the default,
because turning it on means executing code a model wrote moments ago.

When you do turn it on, the sandbox applies CPU, memory and wall limits and blocks the
obvious escapes. **It does not block network in subprocess mode** — `doctor` says so
plainly. For anything you did not write yourself, use the container:

```bash
docker compose build
docker compose run --rm tribunal run /code/examples/sql_injection.py \
    --config /code/examples/nim.toml
```

`/code` is mounted **read-only** — the tribunal proposes diffs, it never writes to your input,
and the mount makes that mechanical rather than a promise. `/traces` is the only writable
mount, so traces and rendered HTML land on the host and outlive the container.

Two images, deliberately: the credential lives in one, the untrusted code runs in the other,
and the one that runs the code has no network and no key.

---

## 7. From an editor (MCP)

```bash
pip install -e '.[mcp]'      # needs mcp >= 2.0
tribunal-mcp               # stdio server; a host launches this
```

Register `tribunal-mcp` as a local stdio MCP server in your host's config. Two tools:

- **`review_code(file_path, test_path=None, max_rounds=2, allow_exec=False)`** — the full
  tribunal, returning a markdown summary: outcome, the rule that fired, every issue and its
  fate, the diff, and the trace path.
- **`get_trace(run_id)`** — reads a previous review back. No model calls.

Three differences from the CLI, each on purpose:

- `max_rounds` defaults to **2**, not 3. A tool call that returns in four minutes reads as a
  hang in most panels. Raise it when you want the third round.
- `allow_exec` is off, and passing a `test_path` without it is **refused** rather than
  quietly honoured — accepting it would run the tribunal with pytest disabled and report a
  performance dimension nobody measured.
- Both tools return markdown, not JSON, because a wall of JSON is unreadable in a host panel.

---

## 8. The benchmark

24 hand-written cases in `eval/cases/`, each with a planted defect, a test, and a `meta.yaml`
declaring what a correct review must find.

```bash
tribunal eval --dry-run     # validate all 24 against the live grounding. No API calls.
tribunal eval --smoke       # 4 cases from full eval recordings, when available. No API calls.
tribunal eval --split dev --arms B1,B3 --config examples/nim.toml   # costs money
```

Four arms, so the question is not "does it work" but **which part is doing the work**:

| | | isolates |
|---|---|---|
| `B0` | one call, no tool output | the floor |
| `B1` | one call **+ the grounding output** | does the debate beat the linters? |
| `B2` | Coder → one critic → Coder, no arbiter | does arbitration beat one critique? |
| `B3` | the full tribunal | |

`B1` is the honest comparison and the one to look at first: if the tribunal cannot beat a single
model that was handed the same bandit and ruff output, the debate is decoration. The
baselines also use the **real** Coder prompt and the real VALIDATE loop rather than a
deliberately weak one — a handicapped baseline answers a flattering question.

`--replay <results-dir>` re-scores a previous sweep from its traces without re-running
anything.

`--split heldout` exists and is **manual-only, once per milestone**. If you run it three
times and report the best one, the split has stopped meaning anything.

---

## 9. Where to read next

| | |
|---|---|
| [`README.md`](README.md) | why it is built this way, and § *What I would not trust* |
| [`docs/00-charter.md`](docs/00-charter.md) | scope, and what is deliberately out of it |
| [`docs/04-arbitration.md`](docs/04-arbitration.md) | the decision table, row by row |
| [`docs/13-implementation-notes.md`](docs/13-implementation-notes.md) | every place the build disagreed with the plan, and which one was wrong |
| [`RUNBOOK.md`](RUNBOOK.md) | the steps that need a credential or a daemon, for whoever has one |
| [`do.txt`](do.txt) | what is built, what is skipped, and why |

If you read one thing beyond this file, make it the README's **What I would not trust**
section, or docs/13. The implementation notes are largely a record of the same bug found
seven times — a consumer written against a field nothing ever populated, every instance with
passing tests — and that is the most transferable thing here.

---

## Troubleshooting

**`No LLM provider is configured`** — expected with no key. §§ 2, 4 and 8's `--dry-run` /
`--smoke` all still work. Set the key in the shell that runs the command; over MCP, set it in
the environment the *host* launches the server from, which is usually not your shell.

**A critic says `unmeasurable`** — the Profiler could not run the code, almost always because
`--allow-exec` is off. That is the honest answer, not a failure.

**A dimension is listed as `unassessed`** — nobody checked it, and that is *not* the same as
clean. A critic that crashed or timed out leaves its dimension unassessed rather than
quietly clean, and policy rows 5 and 6 refuse to accept a patch on that basis. It is
surfaced in the report, the viewer, the MCP summary and the write-up, because the failure
this guards against is silence being read as safety.

**Exit code 1 in CI** — a trade-off, not a crash. Decide whether your pipeline should treat a
declared trade-off as a stop; the code is distinct from `2` and `3` so you can choose.

**`ModuleNotFoundError: No module named 'mcp.server.fastmcp'`** — you have `mcp` 1.x. The
server needs 2.0+; `pip install -e '.[mcp]'` pins it.
