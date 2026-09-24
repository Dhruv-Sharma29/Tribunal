# 017 — recompilation in a loop, and the cache that hides it

**The defect.** `re.compile` on a constant pattern, inside the loop. The fix is a module-level
constant.

**Why this one has a subtlety worth measuring.** `re` keeps an internal cache of up to 512
compiled patterns keyed on the pattern string, so in isolation this costs a dict lookup per
iteration and a microbenchmark shows almost nothing. The cost appears when the cache is
evicted — in a process using many patterns, or after `re.purge()` — and then it is a full
compile per line.

That makes it a good test of whether a critic's explanation is *right* rather than merely
correctly-flagged. "Compiling a regex in a loop is slow" is the relayed version. "The `re`
cache absorbs this until it is evicted, so the cost is latent rather than absent" is the
judgement a tool cannot make, and it is the difference M8 tries to capture.

**It also means the benchmark may show no improvement**, which is exactly the situation the
`unmeasurable` / `inconclusive` verdicts exist for. The Profiler is forbidden from citing an
inconclusive measurement as evidence (`validation.py` enforces it against the real report), so
this case is a live test of whether that holds when a measurement exists but says nothing.
`runnable_benchmark: true` for that reason.

**No tool reports it.** `ruff`'s `PERF` rules do not cover hoisting invariants out of loops,
so the locator is a line range and an arm that only re-rates tool output scores zero.

**Contamination.** Textbook. The cache interaction is the part that has to be reasoned about.
