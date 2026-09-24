# 015 — command injection again, on purpose

**Why a second command-injection case.** 001 is `subprocess.run(..., shell=True)` with one
tainted parameter. This is `os.system` with two, in a context the docstring marks as
privileged. A benchmark with exactly one instance per vulnerability class cannot distinguish
"the system catches command injection" from "the system caught *that* command injection", and
with n=16 held-out cases a single lucky or unlucky instance moves M1 by six points.

**What differs beyond the API.** Two parameters are interpolated, and the fix has to handle
both. A patch that converts to `subprocess.run(["tar", "czf", destination, directory])` is
correct; one that quotes `directory` and leaves `label` in the destination path is a partial
fix that still lets a crafted label escape — `label="x; rm -rf /"` puts the semicolon in
`destination`, which is also concatenated in. Whether a critic notices the *second*
injectable parameter is the thing worth measuring here, and it is not something a locator can
express: `B605` fires on the call either way.

That is a good argument for the judge's description fallback existing, and a reminder that
M1 recall on this case is a floor rather than the whole answer.

**The regression to watch.** `/var/backups/{label}.tar.gz` is the documented return value and
the test pins it. A fix that sanitises `label` by stripping characters changes the path a
caller gets back for a legitimate label containing a dot or a dash.

**Contamination.** `os.system` with concatenation is among the most published snippets in
existence.
