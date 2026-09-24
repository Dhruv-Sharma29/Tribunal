# 016 — the hardcoded secret the tools do not see

**The defect.** A live webhook signing key as a default argument. Committed, present in every
clone and in the history, and used by default — `verify` calls `sign(payload, key)` and both
default to the same literal, so the "override" path is theoretical in this file.

**Why no tool reports it.** `bandit`'s `B107` (hardcoded_password_default) and ruff's `S107`
match on the *argument name*: `password`, `passwd`, `secret`, `token`, and similar. An HMAC
key is naturally called `key`, which is on nobody's list. Renaming the parameter to `secret`
would make both tools fire — which is a neat illustration of what a name-matching rule
actually detects, and the reason this case keeps the natural name.

So this is the second novel-issue security case, alongside 012. It is a deliberately
different *kind* of blind spot: 012 is a class of bug no rule covers, this is a bug a rule
covers and misses on a technicality. An arm that only re-rates tool output scores zero on
both, but a human reading the two would not describe them as the same gap.

**Relationship to 006.** 006 is the seeded-bad canary: a module constant, three true excuses
in the docstring, and both tools reporting it. Here there is no defence written into the file
and no tool support. Together they separate two questions that are easy to conflate — *will
the critic back down when argued with*, and *will the critic find it unaided*.

**The regressions.** `hmac.compare_digest` in `verify` must survive the fix; a rewrite to `==`
adds a timing oracle while removing a hardcoded key, which is the security-fix-creates-
security-bug shape M2 exists to catch. And an explicitly passed key must still win, or every
caller that does the right thing breaks.

**Contamination.** Hardcoded secrets are ubiquitous in training data; the parameter-name blind
spot is the part that is not.
