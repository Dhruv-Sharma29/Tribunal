# 12 — Learning checklist

Ordered by when you need it, not by topic. Each item names the phase that forces you to learn it,
because learning ahead of the need is how the foundations week turns into three.

## Phase 0 — before any real code

| Skill | Why | Done when |
|---|---|---|
| Multi-turn tool use with the Anthropic API | Backbone of every agent; you need the request/response/loop shape in your fingers | You've written a manual `while stop_reason == "tool_use"` loop once, by hand |
| Structured outputs (`messages.parse()` + Pydantic) | The Arbiter parses critiques programmatically instead of making an LLM call to interpret an LLM call | A schema-validated `Critique` comes back and a deliberately malformed one raises |
| Prompt caching basics | Largest free cost win; the layout constraint shapes every prompt you write | `usage.cache_read_input_tokens > 0` on a second call |
| Adaptive thinking + `effort` | `budget_tokens` is gone on current models; effort is the quality/cost dial | You can say what `effort: xhigh` changes and when it's worth it |
| Debate/voting vs. sequential vs. supervisor patterns | You're building the third; knowing the other two is how you defend the choice | You can name the failure mode each pattern has |
| FSMs as explicit transition tables | "Stopping conditions" in interview terms; nested `if` cannot be exhaustively tested | You've written a transition table with guards and a no-fall-through test |

**API details that will bite you if you skip them:**

- Assistant **prefill is rejected** on current models — the "start the reply with `{`" trick is
  gone; structured outputs replace it.
- `output_config` holds both `effort` and `format`; the old top-level `output_format=` on
  `messages.create()` is deprecated (`messages.parse(output_format=Model)` is the Pydantic path).
- JSON-schema mode needs `additionalProperties: false` and a complete `required` list —
  `ConfigDict(extra="forbid")` with non-optional fields gives you both.
- Return **all** `tool_result` blocks in a **single** user message; splitting them trains the model
  out of parallel tool calls.
- Parse tool inputs with `json.loads`, never string matching — escaping varies.
- Thinking tokens bill as **output**. This is the dominant cost term.
- Structured output format is incompatible with citations.
- Log `response._request_id` — it's what you quote when escalating, and it's unrecoverable later.

## Phase 1 — contracts, grounding, sandbox

| Skill | Why | Done when |
|---|---|---|
| Pydantic v2: `ConfigDict`, `Annotated` constraints, validators | The schemas are the system's vocabulary and its guardrails | `extra="forbid"` models generate valid JSON schemas end to end |
| `bandit` JSON output + rule ids | Security grounding | Normalises to `GroundingFinding` with a stable id |
| `ruff` (esp. the `S` / flake8-bandit ruleset) | Bug patterns + security overlap | Same |
| `radon` cc/mi | The always-available static proxy for complexity | Before/after diffable |
| `difflib` — `SequenceMatcher`, `unified_diff` | Synthesising and validating patches | Search/replace block → applied, parsing file |
| `ast` — `parse`, `walk`, `NodeVisitor` | Patch validation and the pre-exec static gate | Forbidden-call scan works |
| POSIX process isolation: `resource.setrlimit`, `os.setsid`, `killpg` | You are executing model-written code; this is a security boundary | Every row of the [05](05-execution-sandbox.md) test table passes |
| Docker `--network none`, `--read-only`, `--cap-drop`, tmpfs `noexec` | The credible isolation layer | Network test fails inside the sandbox container |

The sandbox work is the part most likely to teach you something you didn't know you didn't know.
`proc.kill()` not killing grandchildren, `RLIMIT_DATA` missing `mmap`, `preexec_fn` being unsafe in
threaded parents — these are the kind of specifics that read as real experience.

## Phase 2 — agents

| Skill | Why | Done when |
|---|---|---|
| Prompt engineering for schema compliance | Repair-retry rate is your prompt-health metric | ≥ 90% first-attempt validation on dev fixtures |
| Cache-friendly prompt layout | Stable prefix, volatile suffix, `tools → system → messages` order | Cache reads non-zero from round 2 |
| Record/replay testing (cassettes) | CI with zero API calls and deterministic tests | `pytest` passes with no network |
| Grounding + reasoning composition | The "LLM + deterministic signal" pattern production roles care about | Critics cite evidence and re-rate severity |

## Phase 3 — orchestration

| Skill | Why | Done when |
|---|---|---|
| `asyncio`: `gather(..., return_exceptions=True)`, `wait_for`, semaphores | Parallel critique, per-call timeouts, eval concurrency | A critic timing out doesn't lose the other's work |
| `AsyncAnthropic`, `.stream()` + `.get_final_message()` | Concurrent calls; streaming avoids HTTP timeouts at high `max_tokens` | Both critics visibly overlap in the trace |
| Pure-function design for testability | `policy.py`/`fsm.py` with no I/O is what makes replay and exhaustive tests possible | Decision table fully unit-tested without an API key |
| Property-based testing (`hypothesis`) | Termination is a property, not a set of examples | Random critique sequences always terminate |
| `structlog` with a JSONL renderer | Structured logging instead of ad-hoc `print` | Gap-free trace, two renderers over one stream |
| Idempotent, replayable steps | Debugging your own system, and criterion S6 | `replay` reproduces the report exactly |

## Phase 4 — surfaces

| Skill | Why | Done when |
|---|---|---|
| `Typer` | CLI; quick from a FastAPI background — same author, same type-hint idiom | Subcommands, flags, meaningful exit codes |
| `rich` live rendering | Four-minute runs need progress | Parallel critique visible in the terminal |
| Vanilla JS + embedded JSON | Single-file offline viewer; no server, no CDN | Opens by double-click with no network |
| Multi-stage Docker builds | Two images, one without the API key | Clean-machine one-command demo |

## Phase 5 — evaluation

| Skill | Why | Done when |
|---|---|---|
| Benchmark design: splits, canaries, provenance | The single biggest differentiator vs. other portfolios | 24 cases, dev/held-out, canaries and conflicts included |
| LLM-as-judge, and its validation | Needed for recall and regression scoring | κ against 30 hand labels, published |
| Blind/randomised scoring | A judge that knows which arm is yours will flatter it | Arm labels stripped before judging |
| Honest small-n reporting | Counts with intervals, not decimals from 16 samples | Results table states n and the resolution |
| GitHub Actions with secrets and cost control | Cassette smoke on PRs, live nightly on `main` | Fork PRs cannot reach the key |

## Phase 7 (optional)

| Skill | Why |
|---|---|
| `mcp` Python SDK — tools, stdio server, host registration | The cheapest way to make the **real** tribunal callable in an editor |
| `AGENTS.md` / rules conventions | Shapes the host's own agent only — know the difference before claiming it |
| VS Code Extension API (TS): commands, webviews | Only if you want a dedicated UI artifact |

**[verify]** at implementation time: per-host MCP config locations and schemas, which hosts natively
read a root `AGENTS.md` and how precedence works, and OTel `gen_ai.*` semantic-convention names.
All three have churned; don't put a version number on a resume you haven't checked this month.

## Interview-ready talking points, mapped to what you built

| Question | Where the answer lives |
|---|---|
| "How did you scope this?" | [00](00-charter.md) § Scope — narrowing derived from the grounding constraint, not from time pressure |
| "How do you stop infinite loops?" | [04](04-arbitration.md) — five independent guards, plus a property test proving termination |
| "What if the agents disagree?" | [04](04-arbitration.md) § Conflict detection — `TRADEOFF` as a first-class terminal state with a justification and a named human decision |
| "Isn't this a linter wrapper?" | [11](11-risks.md) § R3 — re-rating rate and novel-issue rate, measured |
| "How do you know it works?" | [07](07-evaluation.md) — four baselines, held-out split, validated judge, published cost |
| "Why no framework?" | [01](01-architecture.md) § Prior art — what was taken from each, what was left, and why 200 lines of FSM beat the escape hatches |
| "You execute model-written code?" | [05](05-execution-sandbox.md) — threat model, three layers, and the tests that prove the boundary |
| "What would you do differently?" | Have a real answer. Candidates: measure `B1` earlier; freeze severity weights sooner; the docker-socket trade-off |
