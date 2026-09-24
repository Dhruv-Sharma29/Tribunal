# tribunal — Documentation

An adversarial multi-agent code-review system. A **Coder** proposes a patch, a **Red-team** and a
**Profiler** critique it independently and in parallel, an **Arbiter** either rejects with a
synthesised critique or declares consensus (or an explicit trade-off), and a **Postmortem** writes
the whole thing up from a replayable trace.

These docs are the plan and the design record. They are written to be read in order the first time
and used as reference afterwards.

## Reading order

| # | Doc | What it settles |
|---|---|---|
| 00 | [Charter](00-charter.md) | Problem, scope, non-goals, what "done" means |
| 01 | [Architecture](01-architecture.md) | Components, the state machine, repo layout |
| 02 | [Contracts](02-contracts.md) | Every schema: issues, critiques, verdicts, trace events |
| 03 | [Agents](03-agents.md) | Per-agent spec — prompt, grounding, model, output |
| 04 | [Arbitration](04-arbitration.md) | Deterministic policy + LLM arbiter, stopping conditions |
| 05 | [Execution sandbox](05-execution-sandbox.md) | Running model-written code without getting owned |
| 06 | [Observability](06-observability.md) | Trace format, viewer, metrics |
| 07 | [Evaluation](07-evaluation.md) | Benchmark, baselines, metrics, judge validation |
| 08 | [Packaging](08-packaging.md) | CLI, Docker, MCP server, IDE options |
| 09 | [Roadmap](09-roadmap.md) | Phases, acceptance criteria, weekly pacing, cut line |
| 10 | [Cost & limits](10-cost-and-limits.md) | Token/dollar/latency budgets, caching, replay |
| 11 | [Risks](11-risks.md) | Risk register and the failure modes to design against |
| 12 | [Learning checklist](12-learning-checklist.md) | Skills to pick up, in build order |
| 13 | [Implementation notes](13-implementation-notes.md) | Where the code departs from these docs, and why |
| 14 | [Providers](14-providers.md) | The multi-provider LLM layer: four structure guarantees, four schema dialects |

## The one-paragraph version

Most multi-agent demos are sequential pipelines wearing a costume: agent A's output becomes agent
B's input, everyone agrees, and nothing is ever rejected. This project is built around the opposite
assumption — that **critique is only meaningful if rejection is possible and disagreement can be
terminal**. Concretely that means three things the demos skip: critics are grounded in real tool
output (`bandit`, `ruff`, `radon`, `pytest`, `timeit`) and must cite evidence per issue; the
accept/reject decision is a *deterministic policy* over structured critiques, not an LLM vibe; and
when the Red-team and the Profiler want incompatible things, the system is allowed to emit a
**trade-off with justification** instead of manufacturing consensus.

## Status

**Phase 1 complete, plus the multi-provider LLM layer.** Contracts, grounding, sandbox and
`patch.py` are built and tested. The LLM client now speaks to Anthropic, OpenAI, Gemini and
NVIDIA NIM behind one interface ([14](14-providers.md)) — the agents themselves are still to
come. `tribunal doctor`, `tribunal providers` and `tribunal ground <file>` work today. See [09-roadmap.md](09-roadmap.md) for what is next and
[13-implementation-notes.md](13-implementation-notes.md) for the seven places building Phase 1
proved one of these documents wrong.

## Conventions used in these docs

- **MUST / SHOULD / MAY** are load-bearing. MUST items are acceptance criteria.
- Anything marked **[verify]** is a claim I have not confirmed against primary sources; confirm
  before relying on it (especially third-party tooling version numbers).
- Code snippets are illustrative shapes, not committed source. The authoritative schemas live in
  [02-contracts.md](02-contracts.md) and, once built, in `src/tribunal/contracts.py`.
