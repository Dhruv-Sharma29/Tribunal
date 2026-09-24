# 018 — redundant I/O, and the fix that changes behaviour

**The defect.** The template is read inside the loop. One `open`, one `read` and one `close`
per row, for a file that cannot change during the call.

**The interesting part is the fix.** Hoisting the read above the loop is correct and obvious
— and it changes what happens when `rows` is empty. Currently an empty list reads nothing;
hoisted, the file is read (and a missing file raises) even with no work to do. Whether that
matters depends on the caller, which is the kind of thing a critic should *say* rather than
silently decide.

`test_no_rows_reads_nothing` pins the current behaviour, so a naive hoist fails the case. The
fix that passes is a lazy read — hoist it but guard on `rows`, or read on first use. That is
a small amount of real design rather than a mechanical transformation, which is what makes
this worth more than "don't do I/O in a loop".

**Why the severity is `medium` and not `high`.** The cost is real and linear in the number of
rows, but it is I/O against a file that will be in the page cache after the first read. A
critic rating this `high` is over-weighting; one rating it `low` is under-weighting a syscall
per row in a rendering loop. The interesting answers are the ones that say which.

**No tool reports it.** Like every performance case in this set, the locator is a line range.

**Contamination.** Common enough to be a stock example. The empty-rows edge is the part that
is not.
