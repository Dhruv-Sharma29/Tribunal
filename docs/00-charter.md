# 00 — Charter

## The problem

An LLM asked to fix a bug will produce a plausible patch and stop. It will not tell you that the fix
introduced a subprocess call with `shell=True`, or that it turned an O(n) loop into an O(n²) one, or
that it silently changed behaviour on the empty-input case. Single-pass code generation has no
adversary.

Human code review supplies that adversary, and the interesting part of human review is not the
finding — it is the **negotiation**: a security reviewer and a performance reviewer want different
things, someone has to decide, and the decision has a rationale that outlives the PR.

tribunal models that. Not "more agents = better," but specifically: *an adversary with real
evidence, a decision procedure that can say no, and a written record of why.*

## What it does

**Input:** a single Python file (≤ ~400 lines), plus optionally a failing `pytest` test or an error
traceback.

**Output:**
1. A validated patch (unified diff) that applies cleanly and parses, or an explicit "no accepted
   patch" with the reason.
2. A structured report: issues found, issues fixed, issues accepted-as-trade-off, rounds used.
3. A complete JSONL trace of every agent call and state transition, and a single-file HTML viewer
   that renders the debate as a thread.

## Scope for v1

### In
- Single-file Python 3.11+ modules and scripts.
- Pure-ish code: standard library plus a small allowlist of third-party imports available in the
  sandbox image.
- Optional runnable `pytest` test supplied by the user as the correctness oracle.
- Two critic dimensions only: security (Red-team) and performance/complexity (Profiler).

### Out (deliberately, not from running out of time)
- Multi-file / cross-module refactors. Grounding tools work fine, but the *patch validation* story
  gets much harder (import graphs, circular changes) and it would eat the whole timeline.
- Non-Python languages. The grounding layer is the differentiator and it is language-specific;
  adding a second language means a second grounding layer for zero architectural gain.
- Code that needs network, a database, or a running service. The sandbox denies network by design
  ([05](05-execution-sandbox.md)); supporting services means fixtures, containers, and secrets
  handling that are orthogonal to the thesis.
- Repo-wide agents / codebase Q&A. Different product.
- A hosted web service. CLI + MCP server is the delivery surface ([08](08-packaging.md)).

### Why this scoping is the right answer in an interview
The narrowing is chosen so that **every critic claim can be grounded in a tool result**. Single-file
Python is exactly the domain where `bandit`, `ruff`, `radon`, `pytest` and `timeit` all work with no
setup. Widen the scope and the critics degrade into unfalsifiable opinion — which is precisely the
failure mode the project exists to avoid. That is a design constraint derived from the thesis, not a
concession.

## Non-goals

- Beating a human reviewer.
- Being fast. A run takes minutes ([10](10-cost-and-limits.md)); that is acceptable and stated up
  front.
- Framework neutrality. No LangChain / CrewAI / AutoGen dependency. The orchestration *is* the
  project; delegating it to a framework deletes the part worth showing. Their ideas are worth
  stealing (see [01](01-architecture.md) § Prior art), their abstractions are not worth inheriting.

## Success criteria

The project is done when all of these hold. These are the same items as the Phase acceptance
criteria in [09](09-roadmap.md), gathered here as the contract.

| # | Criterion | How it's checked |
|---|---|---|
| S1 | A run on any benchmark case terminates in a defined state, always | `tests/test_fsm.py` exhaustive-guard test + full eval sweep with zero `UNKNOWN` outcomes |
| S2 | Every emitted issue carries machine-checkable evidence or is flagged `ungrounded` | Schema validation in `contracts.py`; eval reports `%` ungrounded |
| S3 | The accept/reject decision is reproducible from the trace alone, with no LLM call | `policy.py` is pure; `tests/test_policy.py` covers the decision table |
| S4 | The system can reject. At least one benchmark case ends in `ESCALATE` and at least one in `TRADEOFF` | Eval outcome distribution |
| S5 | Tribunal beats a single-call baseline on known-issue recall, with the number published honestly | [07](07-evaluation.md) results table, held-out split |
| S6 | Any run replays from its trace with zero API calls | `tribunal replay <trace.jsonl>` reproduces the report byte-identically |
| S7 | One command runs the whole thing on a clean machine | `docker run … tribunal run examples/sql_injection.py` |
| S8 | A reader who has never seen the code can explain the disagreement handling after reading the README | Ask two people. Actually do this. |

## The honest risk to the thesis

Adversarial LLM setups fail in two opposite directions: **sycophancy** (the critic finds nothing,
because agreeing is the trained default) and **critique inflation** (the critic manufactures issues,
because it was told to find issues). Both make the debate theatre. The mitigations — canary cases,
evidence requirements, per-critic yield metrics — are in [11](11-risks.md) § R1–R2 and are a
first-class part of the build, not a polish item.
