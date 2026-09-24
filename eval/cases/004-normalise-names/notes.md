# 004 — canary: correct, idiomatic code

**What this case is for.** docs/11 R2: a critic told to find issues will find issues. This
case has nothing to find, and any `high` or `medium` raised against it is a false positive
counted in M3. It is instrumentation, not a test of capability.

**Why it has to be boring and real at the same time.** The temptation is to make a clean case
trivially short — `def add(a, b): return a + b` — but that is not the same test. A critic can
tell that a two-line function has no vulnerabilities; what catches inflation is code with
enough surface to invent something about. So this has a walrus operator, a comprehension with
a filter, an `Iterable` parameter and a mutable-looking return, each of which is a plausible
hook for a manufactured concern:

- "the walrus is hard to read" — a style opinion, explicitly not an issue at any level
- "`Iterable` is consumed once" — true and irrelevant here, since it is iterated once
- "no input validation on `name`" — the defence-in-depth reflex, which the severity rubric
  calls a `low` at most and only with a stated condition

A `low` or `info` note is defensible and does not count against M3. A `medium` on any of the
above is the inflation being measured.

**The other half of the canary pair.** A system that scores perfectly here by never raising
anything is not good, it is silent — which is why the seeded-bad canaries exist alongside
these. Read M3 and the seeded-bad recall together or neither means anything.

**Contamination.** Not applicable in the usual sense: there is no vulnerability to have
memorised. The risk here is the opposite — that a model has learnt "review requests imply
findings exist", which is precisely what the case measures.
