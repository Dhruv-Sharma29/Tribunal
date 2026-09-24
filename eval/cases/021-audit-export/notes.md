# 021 — an authorisation check that disappears under -O, and a quadratic CSV

**Why `both_independent` and not `conflicting`.** The two defects live in different functions
and their fixes do not interact at all: replacing the `assert` with an explicit raise touches
`check_permission`, and joining a list touches `export_audit`'s loop. Neither makes the other
harder. That is the distinction this category exists to test, and it is the control group for
the three conflicting cases — if an arm reports a trade-off here, it is manufacturing one.

**The security issue.** `assert` for authorisation is the sharpest example of a defect whose
severity is entirely contextual. The statement is correct, tested, and reads fine. Under
`python -O` — which is how plenty of production images run — the whole line is removed at
compile time and `export_audit` hands the audit log to anyone. `bandit` reports B101 on every
`assert` in a codebase and rates it low; the rubric in `redteam.md` reserves `high` for
authentication bypass, which this is. Another clean M8 re-rating, in a different direction
from 014's.

**The performance issue.** `body = body + ...` per entry. Deliberately declared `low`, not
`medium`: it is quadratic, but an audit export is not a hot path and the constant is tiny.
This is the case that tests whether a critic can rate a real defect *down* — the mirror of
014, where the interesting move is rating up. An arm that reports both issues at `high`
has found both and calibrated neither.

**The regressions.** A fix that turns the `assert` into a bare `if` with no raise, or that
builds the CSV with the header appended last, breaks the tests. Both are recorded.

**Contamination.** `assert` for auth is a known anti-pattern; the pairing with an unrelated
performance defect is the design.
