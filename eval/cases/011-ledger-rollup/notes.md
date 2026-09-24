# 011 — canary: the one aimed at the Profiler

**Why a second clean case, and why this one.** 004 and 010 bait a security critic. Neither
gives the Profiler much to be wrong about, and M3 is a false-positive rate over *both*
critics — a canary set that only tests one dimension measures half of what it claims to.

This case is written so the performance-flavoured observations are **true and wrong to act
on**:

- **`Decimal` is far slower than `float`.** Correct, and switching is a correctness bug: the
  docstring says these are money, and `test_amounts_stay_exact` fails on floats. A patch that
  "optimises" this is the clearest possible M2 regression, and the forbidden regression names
  it.
- **`sorted(...)[:limit]` is O(n log n) where `heapq.nlargest` is O(n log k).** Also correct,
  also asymptotically better — and meaningless on a monthly roll-up whose accounts number in
  the hundreds. A `low` noting it is defensible. A `medium` is the inflation being measured:
  severity is about impact, and there is none here.
- **`str(entry["amount"])` allocates.** True of every conversion in Python.

**The subtlety worth stating.** This case is *deliberately near the line*. `heapq.nlargest`
genuinely is a better default, so a reviewer could argue a `low` is right — and a `low` costs
nothing under M3, which counts MEDIUM and above. That is the calibration being tested: the
question is not whether a critic notices, it is whether it can notice something and correctly
decline to escalate it.

**Why it carries a benchmark.** `runnable_benchmark: true`: `rollup` is a pure function over a
list, so a before/after timing is meaningful if the Profiler wants one. It is the one clean
case where a measurement is available, which makes it the case where "cited an inconclusive
measurement" could show up — the validator forbids it, and a canary is a good place to find
out whether that holds under a live model.

**Contamination.** Money-in-Decimal is standard advice and widely published, which is part of
the point: a critic that has learnt "Decimal for money" should be *more* likely to leave this
alone, not less.
