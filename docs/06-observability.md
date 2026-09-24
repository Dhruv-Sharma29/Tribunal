# 06 — Observability: the transparent trace

The pitch is "visible disagreement, logged as a transparent trace." If the trace is an afterthought,
the differentiation collapses. Budget real time here — and note that the trace is not only a demo
artifact: **replay** ([10](10-cost-and-limits.md)) and the **eval harness**
([07](07-evaluation.md)) both consume it, so building it early makes the later phases cheaper.

## Principles

1. **The trace is the source of truth, not a log.** The report, the eval metrics, and the replay all
   derive from it. If something isn't in the trace, it didn't happen.
2. **Append-only, one JSON object per line.** Crash-safe, greppable, streamable, diffable between
   runs, and readable with `jq` when the viewer breaks.
3. **Self-describing.** The header carries schema version, config, model ids and prompt versions. A
   trace from three weeks ago must be interpretable without the code that produced it.
4. **Structured, never `print`.** `structlog` with a JSONL renderer. Human-facing CLI progress is a
   separate renderer over the same event stream — not a second code path.
5. **Redact by default.** Source code goes in the trace (that's the point); secrets and full prompt
   bodies are configurable. `--trace-level=full` includes rendered prompts; the default stores the
   prompt *version* plus an input hash. Traces get shared in GIFs and issue reports.

## Event stream

Schema in [02](02-contracts.md) § Trace event schema. The event kinds and their payloads:

| `kind` | `actor` | Payload highlights |
|---|---|---|
| `run_start` | orchestrator | `schema_version`, config snapshot, model ids, prompt versions, input sha256, CLI argv |
| `state_enter` / `state_exit` | orchestrator | `state`, `round`, `from_state` |
| `llm_request` | agent role | `model`, `effort`, `prompt_version`, `input_hash`, `cache_breakpoints` |
| `llm_response` | agent role | parsed output, `stop_reason`, `usage`, `request_id`, `parse_retries` |
| `tool_run` | grounding tool | `tool`, argv, exit code, `findings_count`, `duration_ms`, truncated stdout |
| `patch_validate` | code | `applied`, `parse_ok`, `hunks`, failure reason, `diff_sha256` |
| `policy_decision` | policy | full `Verdict` incl. `rule_fired`, `pressure`, `pressure_history` |
| `budget_check` | orchestrator | spent vs. cap for usd/tokens/seconds/rounds |
| `error` | any | exception type, message, whether recovered |
| `run_end` | orchestrator | terminal state, totals, final `Report` reference |

`llm_response.usage.cost_usd` is computed at write time from a pinned price table
(`config.PRICES`), so the trace stays interpretable after prices change. Record the table version in
the header.

### Two events that earn their keep

**`policy_decision`** is the money event. It contains `rule_fired`, which means any outcome in any
trace is explainable by one grep:

```bash
jq -r 'select(.kind=="policy_decision")
       | "r\(.round) \(.payload.decision) \(.payload.rule_fired) pressure=\(.payload.pressure)"' \
   trace.jsonl
# r1 reject   hard_block_security      pressure=21.6
# r2 reject   pressure_over_threshold  pressure=7.2
# r3 tradeoff irreconcilable           pressure=5.6
```

That three-line output *is* the project's thesis, printable. Put it in the README.

**`llm_request.cache_breakpoints`** plus `llm_response.usage.cache_read_input_tokens` makes cache
effectiveness observable. A zero cache-read across rounds means a silent prefix invalidator, and you
will not notice it any other way until the bill arrives.

## Correlation

- `run_id` — ULID, in every event and in the trace filename (`traces/<run_id>.jsonl`).
- `seq` — monotonic, gap-free. A gap means events were lost; the reader warns.
- `round` — `None` outside the loop.
- `request_id` — Anthropic's `response._request_id`. Log it; it's what you quote when escalating an
  API problem, and it's unrecoverable after the fact.

## The viewer

A single self-contained HTML file written next to the trace. No server, no build step, no
dependencies — `tribunal view trace.jsonl` writes `trace.html` and opens it. It must work when
double-clicked from a file manager, because that's how a reviewer will open it.

Layout:

```
┌──────────────────────────────────────────────────────────────────────┐
│ run 01JB…  sql_injection.py   3 rounds   TRADEOFF   $0.71   4m12s    │
│ [Timeline] [Debate] [Diffs] [Grounding] [Cost]                       │
├──────────────────────────────────────────────────────────────────────┤
│ ROUND 1                                                              │
│  ┌ Coder ─────────────────────────────┐                              │
│  │ +parameterised query  (3 hunks)    │  addresses: SEC-a31f         │
│  └────────────────────────────────────┘                              │
│  ┌ Red-team ──────────┐  ┌ Profiler ──────────┐   ← side by side,    │
│  │ BLOCK              │  │ CONCERNS           │     drawn in parallel│
│  │ ● HIGH  SEC-b10c   │  │ ● MED   PERF-77c2  │                      │
│  │   bandit:B602 ⎘    │  │   radon 7→12 ⎘     │                      │
│  └────────────────────┘  └────────────────────┘                      │
│  ╔ POLICY ═══════════════════════════════════════════════════════╗   │
│  ║ REJECT · rule: hard_block_security · pressure 21.6            ║   │
│  ╚═══════════════════════════════════════════════════════════════╝   │
│  ┌ Arbiter ─────────────────────────────────────────────────────┐    │
│  │ Consolidated: 1) drop shell=True … 2) keep the param query … │    │
│  │ Dismissed: PERF-91aa (Coder was right — cold path)           │    │
│  └──────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘
```

Design requirements, in priority order:

1. **The two critics render side by side.** The visual claim is independence; a vertical list reads
   as a pipeline. This is the single most important rendering decision in the file.
2. **The policy decision is visually dominant** — boxed, distinct, with `rule_fired` shown. It's the
   thing a viewer should notice within two seconds.
3. **Evidence is expandable.** Every issue's `evidence.excerpt` is one click away. That is what
   turns "the agent said so" into "the agent cited `bandit:B602` at line 41, here it is."
4. **Pressure sparkline in the header.** The convergence story in 40 pixels.
5. **A diff view per round** with the previous round's patch for comparison — makes oscillation
   visible.
6. **Cost tab** — per-agent, per-round token and dollar breakdown from `usage`.

Implementation: embed the JSONL as a `<script type="application/json">` blob and render with
vanilla JS. No CDN — the file must work offline, and the demo GIF must not depend on a network
round-trip.

**Skip Streamlit.** It needs a running server, a Python env, and a port, which kills the "email
someone an HTML file" property. If an interactive dashboard is wanted later, `textual` for a TUI is
a better fit than a web app, but the single HTML file is the deliverable.

## CLI progress rendering

Same event stream, human renderer. Runs take minutes ([10](10-cost-and-limits.md)), so silence is
not acceptable UX:

```
● grounding      bandit 2 · ruff 5 · radon cc=7                        1.2s
● round 1/3
  ├ coder        3 hunks · addresses SEC-a31f                        24.1s
  ├ validate     applied · parses · tests run                         0.4s
  ├ critique     redteam BLOCK(1 high) │ profiler CONCERNS(1 med)    31.8s   ⟵ parallel
  ├ policy       REJECT · hard_block_security · pressure 21.6          0ms
  └ arbiter      consolidated 2 instructions · dismissed 1           18.9s
● round 2/3 …
```

Showing the two critics on one line with a `⟵ parallel` marker communicates the concurrency for
free. `rich` handles this; `structlog` feeds it.

## Metrics worth aggregating

Written to `traces/summary.jsonl`, one line per run. This is what turns single runs into a dataset:

- Outcome distribution (`ACCEPT` / `TRADEOFF` / `ESCALATE` / `FAILED`) and `rule_fired` histogram.
- Rounds-to-terminal distribution.
- Per-agent p50/p95 latency and token cost.
- Cache hit ratio per agent.
- Pressure trajectories (for tuning `accept_threshold` and `no_progress_epsilon` against real data
  rather than guessing — do this before freezing thresholds in Phase 6).
- Schema-parse retry rate per agent — the health signal for prompt quality.
- Critic yield: issues per critique, grounded fraction, re-rating rate ([03](03-agents.md)).

## OpenTelemetry: worth it, cheaply

A minimal OTel exporter mapping `state_enter/state_exit` → spans and `llm_request/llm_response` →
child spans with `gen_ai.*` semantic-convention attributes (`gen_ai.request.model`,
`gen_ai.usage.input_tokens`, …). ~80 lines, behind `--otel`, off by default and with the SDK as an
optional extra so the core install stays light.

Worth doing because a waterfall view makes the parallel critique span visually obvious, and because
"emits OTel traces with GenAI semantic conventions" is a real MLOps line rather than a claimed one.
Not worth doing before the JSONL trace and viewer are solid — the JSONL is the load-bearing
artifact; OTel is the credential. **[verify]** the current `gen_ai.*` convention names against the
OTel spec when implementing; they have churned.
