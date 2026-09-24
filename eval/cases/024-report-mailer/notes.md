# 024 — a PATH-resolved mailer, and the one performance defect a tool reports

**Why this case matters disproportionately.** Every other performance issue in the benchmark
has a line-range locator, because ruff's `PERF` rules do not cover wrong data structures,
invariant work in loops, redundant I/O or deepcopy. Here `RUF005` fires on
`values = values + [x]`, so this is the **only** case where M1's mechanical half reaches the
performance dimension at all.

That is worth stating in the results rather than leaving implicit: performance recall across
this benchmark is almost entirely judged against line ranges and, where those fail, against
the judge. The security dimension is much better grounded. Two numbers computed the same way
are not equally well evidenced, and a reader deserves to know which is which.

**The security issue.** `subprocess.call(["sendmail", ...])` with no absolute path. The argv
list means there is no shell and no injection; the defect is resolution — `sendmail` is
whatever PATH finds first, and PATH is influenced by the service unit, the container
entrypoint, or a compromised earlier directory. `medium`, because it needs an attacker who
can already influence the environment, and the rubric asks for that condition to be stated.

There is a third defect nobody declared: `open(body_path, "rb")` is never closed. It is real,
it is minor, and it is deliberately not in `known_issues` — a critic that reports it is
finding something true that the case did not declare, which is the novel-issue behaviour
docs/11 R3 measures and which the judge records as `is_real_issue` with no match. A case where
the *right* answer includes something undeclared is a useful thing to have in the set.

**Why the two declared issues are independent.** Different functions, and the fixes share
nothing.

**Contamination.** Both patterns are common; the interesting property here is the grounding
asymmetry described above, which is a fact about the tools rather than about the models.
