# 10 — Cost, latency, caching, and replay

Run the numbers before building. A five-agent multi-round system on a frontier model is not free,
and discovering the per-run cost during the eval sweep is an unpleasant way to learn it.

## Price table (as of 2026-09-15)

| Model | ID | Context | Input $/MTok | Output $/MTok |
|---|---|---|---|---|
| Claude Opus 5 | `claude-opus-5` | 1M | $5.00 | $25.00 |
| Claude Sonnet 5 | `claude-sonnet-5` | 1M | $2.00 | $10.00 |
| Claude Haiku 4.5 | `claude-haiku-4-5` | 200K | $1.00 | $5.00 |

Cache reads bill at ~0.1× input; cache writes at ~1.25×. Thinking tokens bill as **output**, which
is the term that dominates here — adaptive thinking at effort `high` is the single largest cost
driver in the system.

Pin these in `config.PRICES` with a version stamp and compute `cost_usd` at trace-write time
([06](06-observability.md)), so old traces stay interpretable after prices change.

## Per-run cost estimate

Rough token accounting for one debate round on a 200-line file, all agents on `claude-opus-5`:

| Agent | Input | Output (incl. thinking) | Cost |
|---|---|---|---|
| Coder | ~4K | ~1.5K | $0.058 |
| Red-team | ~6K | ~1.2K | $0.060 |
| Profiler | ~6K | ~1.2K | $0.060 |
| Arbiter | ~8K | ~1.0K | $0.065 |
| **Round total** | ~24K | ~4.9K | **≈ $0.24** |

| Scenario | Cost |
|---|---|
| 1 round, accept | ~$0.24 + postmortem $0.10 ≈ **$0.34** |
| 3 rounds, escalate | ~$0.72 + postmortem ≈ **$0.82** |
| With prompt caching on stable prefixes | **≈ $0.15–0.55** |
| Full eval sweep: 24 cases × (B3 + B1 + judge) | **≈ $22–28** |
| Full eval, all four arms | **≈ $30–35** |

Budget defaults in [04](04-arbitration.md) (`max_usd = 2.00`) sit ~2.5× above the worst expected
run, which leaves room for a pathological case without letting a bug run away.

**Plan for roughly $120–200 of API spend across the project**, dominated by eval sweeps and by
prompt iteration in Phase 2. That is the number to know before starting, and `--replay` is what
keeps it from being 3×.

## Latency

| Call | p50 |
|---|---|
| Coder (effort high, adaptive thinking) | 20–35s |
| Critic (parallel, so max of two) | 25–40s |
| Arbiter (effort xhigh) | 20–40s |
| Grounding suite | 1–3s |
| **Round** | **~75–110s** |
| 3-round run + postmortem | **~4–6 min** |

Consequences to design for, not discover:

- **Streaming progress is mandatory**, not nice-to-have ([06](06-observability.md) § CLI progress).
  Four minutes of silence reads as a hang.
- **Parallel critique halves the critique phase**, and it's visible in the trace waterfall — the
  concurrency has an observable payoff, which is worth demonstrating.
- **Set client timeouts generously.** SDK default is 10 minutes; per-agent, use ~180s with
  `max_retries=2`, and prefer `.stream()` + `.get_final_message()` for anything with large
  `max_tokens` to avoid HTTP timeouts.
- **Eval sweeps need concurrency** (semaphore of 4): 24 cases × 4 arms serially is ~2 hours.

## Prompt caching

The largest free win. System prompts and rubrics are large and identical across rounds; per-round
content is small.

Request layout, in render order (`tools → system → messages`):

```
[system]  role + rubric + severity definitions          ← cache_control: ephemeral
[system]  original source file (line-numbered)          ← cache_control: ephemeral
[msg 1]   grounding report for this round               ← volatile
[msg 2]   consolidated critique / patch                 ← volatile
```

Rules that actually matter:

- **Caching is a prefix match.** One byte changed anywhere in the prefix invalidates everything
  after it. Nothing volatile may appear before the last breakpoint — no timestamps, no `run_id`, no
  unsorted `json.dumps` (always `sort_keys=True`), no changing tool list.
- Max 4 breakpoints per request; minimum cacheable prefix is model-dependent (512–4096 tokens), so
  short prefixes silently don't cache.
- **Verify, don't assume:** assert `usage.cache_read_input_tokens > 0` from round 2 onward in tests.
  A silent invalidator costs money with no error message. Surface the ratio in the trace summary.
- Default TTL is 5 minutes, which comfortably covers a multi-round run. `ttl: "1h"` is available but
  unnecessary here.

Expect ~40–60% input-token reduction across a 3-round run with the layout above.

## Replay and the response cache

Two distinct mechanisms, both essential.

**Response cache (cassettes).** `llm/cassette.py` keys on
`sha256(model | effort | system | messages | output_schema | prompt_version)` → recorded response.

- CI and `pytest` run entirely from cassettes: **zero API calls, zero cost, deterministic**.
- During prompt iteration, unchanged agents are served from cache while you change one — which is
  what makes Phase 2 affordable.
- Bypass with `--no-cache`.
- Cache is *content-addressed*, so a prompt edit is automatically a cache miss. No manual
  invalidation, no stale-cassette class of bug.

**Trace replay.** `tribunal replay trace.jsonl` re-derives the report and the metrics from a
recorded trace with no API calls at all, because the policy layer is pure
([01](01-architecture.md) Rule 1) and every LLM output is in the trace.

This is worth more than it sounds:

- Iterate on the **judge** prompt and re-score a whole eval sweep for free ([07](07-evaluation.md)).
- Iterate on the **report wording** for free.
- Change `accept_threshold` and ask "what would this run have decided?" — against real recorded
  critiques, retroactively, for free. That is how you tune thresholds on evidence rather than
  intuition, and it's the concrete payoff of keeping `policy.py` pure.
- Debug a bad outcome without paying to reproduce it.

Replay is also criterion S6 and the answer to "how do you know this is reproducible."

## Model routing: measure, don't assume

Default is `claude-opus-5` everywhere. Cost-driven downgrades are a **measured experiment**, run on
the dev split against the frozen benchmark — not a default chosen on price.

The hypothesis worth testing, in order of expected value:

1. **Lower effort before a smaller model.** Dropping the critics to effort `medium` on
   `claude-opus-5` is often a better cost/quality trade than moving to a smaller model, and it keeps
   one cache namespace. Test this first.
2. **Critics → `claude-sonnet-5`.** Critiquing against supplied tool evidence is the most
   constrained task in the system, so it's the most plausible candidate. Round cost drops
   ~$0.24 → ~$0.17.
3. **Postmortem → `claude-sonnet-5`.** Summarisation from a trace; low risk.
4. **Arbiter and Coder stay on Opus 5.** Synthesis and patch generation are the hardest tasks and
   the ones where a regression is least visible in the metrics.

Caveat to note in the write-up: **caches are model-scoped.** A mixed-model tribunal forfeits cache reuse
across the models it mixes, so the saving is smaller than the sticker price suggests. Measure cost
*per completed task*, not per request — a cheaper critic that triggers an extra round is not
cheaper.

Report whatever you find, including "no measurable quality difference, 30% cheaper" or "measurably
worse, not worth it." Either is a good result; a routing table adopted without measurement is not.

## Context window

At 1M tokens on all three models, context is not a constraint for single-file review. No compaction,
no context editing, no chunking. Worth a sentence in the write-up — knowing which problems you
*don't* have is part of the design.

## Budget enforcement recap

```toml
[budget]
max_usd = 2.00
max_tokens = 400_000
max_wall_seconds = 600
max_rounds = 3
```

Checked at every state entry, emitting `budget_check`. Breach → `ESCALATE`, never abort: the run
still produces a trace, a report, and the best patch so far ([04](04-arbitration.md) § Budget
enforcement).
