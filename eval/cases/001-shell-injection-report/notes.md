# 001 — shell injection in a report generator

**The defect.** `report_name` reaches `subprocess.run(..., shell=True)` by concatenation. A
name of `q3; rm -rf /` runs both commands. `bandit` flags the call as B602 and `ruff` as S602,
so the finding is mechanically locatable and the locator uses the rule rather than a line
range — line numbers move the moment the Coder touches the file.

**Why it is realistic rather than a puzzle.** The function has a real job (it returns the path
it wrote) and the shell is being used for the redirection, which is the actual reason people
reach for `shell=True` here. A fix that drops the redirect or the return value is a regression,
which is what `forbidden_regressions` records.

**The interesting failure.** The naive fix is to quote the name. That is weaker than passing an
argv list and moving the redirect into Python, and a critic that accepts quoting without
comment is one worth catching.

**Contamination.** A well-known vulnerability class; the models have very likely seen
near-identical code. Fine for comparing systems on identical inputs, not for claiming absolute
capability — see docs/07 § Provenance and contamination.
