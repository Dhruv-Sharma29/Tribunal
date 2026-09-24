# 003 — parameterisation versus one round trip

**Why this one is `conflicting` and not `both_independent`.** That distinction is the whole
value of the case, and it is easy to get wrong: two issues on one line are usually *not* a
conflict (see `arbiter_affirm.md`, which spends most of its length on that non-example).

Here the remedies genuinely exclude each other at the line they share:

- The security fix is a parameterised query — `WHERE sku = ?` — executed per sku, with the
  sku validated at the boundary.
- The performance fix is to stop querying per sku: one `WHERE sku IN (...)` round trip for
  the whole batch.

A parameterised `IN` clause needs a placeholder list built from the batch length, which is
the construction people get wrong and which reintroduces string building into the query. So
applying either remedy makes the other harder, and there is no third option that is
straightforwardly better. The docstring says this is the request hot path, which is the fact
that makes the trade-off undecidable *from the file alone* — and therefore a human's call.

**What each arm can do with it.** B0–B2 have no representation for "both concerns stand", so
they must pick one silently. B3 can reach `TRADEOFF` via detector 2: the two issues are
grounded, both MEDIUM+, opposite dimensions, overlapping spans — a same-span candidate the
Arbiter is asked to affirm. docs/07 M5 says to report that as a capability difference rather
than as points, and this case is why.

**What would make this case wrong.** If a reviewer can name a fix that satisfies both cleanly,
the case is mislabelled and belongs in `both_independent`. Recheck that before the held-out
run; a spurious conflicting case would inflate exactly the metric the project leads with.

**Contamination.** SQL injection is the most-published vulnerability class there is. The
*conflict* is the part unlikely to be memorised, which is the part being measured.
