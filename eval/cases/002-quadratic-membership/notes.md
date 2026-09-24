# 002 — quadratic membership test and string concatenation in a loop

**The defects.** Two, both performance, both independent of each other.
`record["id"] in allowed_ids` scans a list per record; `names = names + ...` rebuilds a string
per record.

**Why line-range locators here rather than rules.** Neither defect has a reliable rule code in
this suite. `ruff`'s `PERF401`-family rules do not fire on the membership test at all, so a
`rule` locator would never match and would score as a miss against every arm. The
line-range locator says what it means: *an issue pointing at this line*.

This makes 002 the case that tests whether the Profiler finds something no tool reported —
the "novel issue" half of the linter-wrapper question (docs/11 R3). If an arm only ever
repeats tool output, it scores zero here.

**Why it is realistic.** `select_active` is called with `list(allowed_ids)` by `summarise`,
which is how the list ends up there in real code: someone normalised an argument and made the
inner loop quadratic without touching the inner loop. The fix has to notice that.

**The regression to watch.** Converting to a set is correct, but sorting by `allowed_ids`
order, or returning a set, breaks the documented ordering — hence the first forbidden
regression, and the test that pins it.

**Contamination.** Textbook patterns; certainly resemble training data. See docs/07 §
Provenance and contamination.
