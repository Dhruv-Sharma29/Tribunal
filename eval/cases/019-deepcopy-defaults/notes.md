# 019 — the correct version of 009's bug, and only slow

**The defect.** `copy.deepcopy` per job. It is not wrong — it is the reason the two
correctness tests pass — it is just far more machinery than this structure needs. `deepcopy`
walks arbitrary graphs and maintains a memo dict to handle cycles and shared references; the
template is a two-level dict of literals.

**Why it is paired with 009 deliberately.** 009 is the same shape with the copy *missing*: a
module-level dict mutated in place, aliased into every result, and a genuine security issue.
This is the same code written correctly. A benchmark containing only the broken version
teaches a critic that `deepcopy` in a loop means "look for aliasing"; containing both asks
whether it can tell the two apart.

That matters for M3-adjacent behaviour even though this is not a canary: a critic that raises
a *security* issue here has misread correct defensive code as the bug it is preventing, which
is a false positive of the most misleading kind.

**The fix, and why both regressions are pinned.** The defensible fixes are an explicit
`{"headers": {"accept": "application/json"}, "retries": 3, "tags": []}` per job, or
`copy.copy` with the nested values rebuilt. The tempting wrong fix is plain `copy.copy`,
which shares `headers` and `tags` between every job — `test_jobs_do_not_share_nested_state`
fails, and so does the template-mutation test. Both are recorded, because a patch that trades
a performance issue for an aliasing bug is exactly the M2 regression shape.

**No tool reports it.** Line-range locator, like every performance case here.

**Contamination.** `deepcopy` cost is well documented; the pairing with 009 is the design.
