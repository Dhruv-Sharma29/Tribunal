# 14 — Multi-provider LLM layer

## Why this is not just "swap the base URL"

[03](03-agents.md) and [10](10-cost-and-limits.md) assume one provider: `claude-opus-5`
everywhere, one price table, one structured-output mechanism. Supporting NVIDIA NIM, Gemini and
OpenAI alongside it is a real architectural change rather than a configuration one, for one
reason:

> tribunal's premise is that critiques are **schema-validated structures**, not prose. The
> interesting difference between providers is therefore not their prompt format — it is **how
> strongly each can guarantee the output matches a schema.**

Four different guarantees, and the layer records which one produced every response:

| `StructureMode` | Guarantee | Providers |
|---|---|---|
| `native_strict` | Decoding constrained to our exact schema. Output *cannot* violate it. | Anthropic `messages.parse`, OpenAI `strict: true` |
| `native_schema` | Schema sent and honoured, but only a documented *subset* of JSON Schema | Gemini |
| `native_json` | "Emit JSON." Shape is the model's problem. | NVIDIA NIM |
| `extracted` | JSON recovered from prose or from an inline chain of thought | any fallback path |

**Why recording it matters.** [09](09-roadmap.md) Phase 2 makes the schema-parse retry rate the
prompt-health signal, and [07](07-evaluation.md) attributes score changes to prompt versions. If
a run mixes providers and does not record the mode, a rising retry rate is unattributable — it
could equally mean "the prompt drifted" or "this backend cannot constrain output". `StructureMode`
on every response and in every cassette keeps those two separable.

## Layout

```
src/tribunal/llm/
├── base.py         # LLMRequest / LLMResponse / Provider ABC / StructureMode / Capabilities
├── schema.py       # JSON Schema dialect adaptation, per provider
├── extract.py      # recovering JSON from reasoning output
├── cassette.py     # record/replay, keyed by the full request
├── client.py       # retries, repair, accounting — the provider-agnostic surface
├── registry.py     # construction and discovery, credential-free
├── anthropic_p.py  openai_p.py  gemini_p.py  nim_p.py
```

`config.py` gains `providers` (connection settings, **never credentials** — only the *name* of
the env var, so a config snapshot is safe in a trace header), `llm` (cassette mode, repair
budget), and a provider-qualified `PRICES` table.

## Schema dialects: the part that fails silently

Sending an unadapted schema does not error. The provider **ignores** it, and the failure surfaces
much later as an unexplained parse-retry rate. So adaptation is explicit and reports what it gave
up.

| Provider | `$ref` | `additionalProperties` | `required` | Value constraints |
|---|---|---|---|---|
| Anthropic | kept | as emitted | as emitted | not enforced by decoding |
| OpenAI `strict` | kept | **forced `false`** everywhere | **must list every key** | dropped |
| Gemini | **inlined** (`$ref` undocumented) | **stripped** (unsupported) | as emitted | dropped |
| NIM | kept | as emitted | every key | dropped (schema is a hint) |

Two consequences worth stating plainly:

**`Critique.positive_notes` has a default, so Pydantic omits it from `required`** — which OpenAI's
strict mode rejects. Forcing it in is not the semantic change it looks like: optionality is
expressed by a nullable union, not by omission, so the model must now emit `[]` explicitly. That
is a better outcome than it silently dropping the key.

**No provider enforces `multipleOf`.** [02](02-contracts.md) design rule 3 quantises `Confidence`
to 0.05, and *no current structured-output implementation constrains it*. The rule is therefore a
**validation** guard, not a **generation** guard. A model picking `0.93` produces a payload that
looks valid and is not. Burning a repair retry on that would swamp the prompt-health signal with
quantisation noise, so `client.py` applies `contracts.quantise_confidence` locally first, for free,
and counts it as a `local_repair` rather than a `repair_retry`.

## Cassettes

Keyed by a hash over provider, model, system, user, **adapted schema**, schema name, max_tokens,
effort. Both of the unobvious inclusions are load-bearing:

- **Provider** — otherwise a recording made against Anthropic could serve a Gemini request, and
  the suite would claim coverage of a backend it never exercised.
- **Adapted schema** — otherwise changing a dialect rule silently reuses responses recorded
  against the old schema, making a schema regression invisible in exactly the tests meant to
  catch it.

`prompt_version` is deliberately *not* in the key. A prompt edit invalidates the cassette through
the text it actually changed, so a version bump with no text change correctly keeps its recording.

A miss in `replay` mode is **fatal**. Falling through to the network is how "zero API calls in CI"
quietly stops being true.

## NVIDIA NIM, and what measuring it changed

The Nemotron models are the most interesting integration, and three assumptions taken from the
documentation turned out to be wrong when probed against the live hosted endpoint.

| Claim in the docs | Measured on `integrate.api.nvidia.com` |
|---|---|
| Reasoning appears inline in `content`, before the answer | **False.** Reasoning comes back in a separate `reasoning_content` field (1–3 KB); `content` is clean JSON |
| `thinking_token_budget` controls reasoning depth | **Rejected**: `400 Unsupported parameter(s)`. Self-hosted NIM only — use `reasoning_effort` |
| JSON output supported, schema validation unclear | `response_format: {"type": "json_schema", ...}` **is accepted** |

So `inline_reasoning` is claimed only when `base_url` is *not* the hosted endpoint, and
`thinking_token_budget` is gated the same way. The `<think>`-stripping path in `extract.py` stays —
a self-hosted build may match the prose docs — but it is not the hosted default.

**`503 ResourceExhausted` is routine, not exceptional.** The shared hosted endpoint returns
`Worker local total request limit reached (16/16)` under trivial concurrency. It maps to
`ProviderTransientError` and is retried with jittered backoff; without that, a debate round fails
for a reason that has nothing to do with the code under review. This is also a caution for
[01](01-architecture.md) § Concurrency: the two critics run in parallel, and on a shared endpoint
that parallelism is itself a source of 503s.

**It is not token-metered.** The hosted developer tier is credit-based and self-hosted NIM has no
per-token price, so `PRICES` marks it `not_token_metered` — a distinct state from "we forgot to pin
a price", and the reason `price_for` can keep raising on an unknown model.

## Gemini: the wire shape the prose docs got wrong

The structured-output guide shows a flat
`response_format={"type": "text", "mime_type": ..., "schema": ...}`. Introspecting
`google.genai.types.ResponseFormat` shows the real shape nests a `TextResponseFormat` under a
`text` key, with the field spelled **`jsonSchema`** in camelCase. Following the prose would have
produced a silently ignored schema — the worst failure available here, since the response would
still parse as JSON while not matching the contract.

`client.aio.interactions` is used rather than the sync surface wrapped in `asyncio.to_thread`, and
that is a **correctness requirement, not a style preference**: [05](05-execution-sandbox.md) notes
that `preexec_fn` is unsafe in a multi-threaded parent. A thread-pool call anywhere in the LLM
layer leaves those threads alive for the rest of the run, so the next sandboxed `pytest` would fork
from a multi-threaded process. The grounding suite already runs sandboxed tools synchronously to
preserve that guarantee; the LLM layer must not undo it.

## Routing agents

An agent names a model; the provider follows from `PRICES`, so pointing the Profiler at Nemotron is
a one-line change and the two fields cannot disagree:

```toml
[agents.profiler]
model = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"

[agents.redteam]
model = "gpt-6-astra"
```

`tribunal providers` prints the resolved routing, each backend's guarantees, where its wire shape
was verified, and the price table.

**This does not make model routing a default.** [10](10-cost-and-limits.md) § Model routing is
explicit that cost-driven downgrades are a *measured experiment* on the dev split, not a choice
made on price. What this layer adds is the ability to run that experiment — and one more
independent variable the eval must hold fixed or report.

## Effort mapping

The project's effort levels are `low | medium | high | xhigh | max`. Only Anthropic has all five.
OpenAI and NIM have no `xhigh`/`max`, so those clamp to `high` — the Arbiter runs at `xhigh` by
default, and clamping beats both a 400 and silently dropping the parameter. Gemini's Interactions
API exposes no effort control at all, which `Capabilities.controllable_effort` reports as `False`.

## What is still unverified

Honesty about coverage, since `providers` prints it:

| Provider | Status |
|---|---|
| NIM | **Live-tested.** 3 live tests pass, including the canary and the reasoning-separation case. One cassette committed. |
| Anthropic | Wire shape from the SDK and the bundled `claude-api` skill. Not live-tested here (no key). |
| OpenAI | Wire shape from the official structured-outputs guide. Not live-tested. |
| Gemini | Wire shape from SDK introspection. **Not live-tested**, and the Interactions API is new enough that the usage-field names are read defensively. |

`pytest -m live` exercises whichever backends have a credential; the default run is `-m "not live"`
and makes zero API calls.

## Security note

Credentials never enter config, a trace, or a cassette — only the *name* of the environment
variable does. `sandbox.py`'s environment scrub gained `NVIDIA_`, `NIM_` and `GEMINI_` prefixes
when this layer landed: the allowlist already meant no key reached a sandboxed process, but a
backstop that does not cover every provider the tribunal can authenticate to is a backstop with a hole
in it. `tests/test_llm_client.py` asserts no cassette contains key material, because cassettes are
committed.
