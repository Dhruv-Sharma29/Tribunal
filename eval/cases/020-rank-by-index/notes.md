# 020 — the quadratic lookup that does not look like one

**The defect.** `ordering.index(name)` scans the list from the start, per name. Building
`{name: position for position, name in enumerate(ordering)}` once turns O(n·m) into O(n+m).

**Why a second quadratic-lookup case after 002.** 002's defect is `x in some_list`, which
reads as a membership test and is widely taught as the thing to convert to a set. `.index()`
reads as a *lookup* — the same mental category as `dict[key]` — and the linear scan is a
property of the receiver's type rather than of the call site. They are the same complexity
bug and they are not equally visible, so a benchmark that includes only the first cannot tell
whether an arm recognised a pattern or understood a cost.

**What a good critique says beyond the complexity.** `ordering.index` raises `ValueError` for
a name not in `ordering`, and the dict version raises `KeyError` — or silently returns a
default if someone writes `.get(name, -1)` while "optimising". The failure mode changes, and
the fix should keep it. Nothing in the case tests that, because nothing in the case documents
what should happen for an unknown name; a critic that raises the question is doing better
than the case does.

**Severity.** `medium`. It is quadratic and it is in a ranking function, but both inputs here
are plausibly small; a critic that says `high` without a stated size assumption is
over-rating, and the rubric asks for the condition to be named.

**No tool reports it**, so the locator is a line range.

**Contamination.** Standard. The `.index()` framing is slightly less standard than the `in`
one, which is the point.
