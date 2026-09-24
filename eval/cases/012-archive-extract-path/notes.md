# 012 — Zip Slip, and nothing in the suite reports it

**The defect.** `os.path.join(destination_dir, member_name)` is not containment.
`os.path.join("/out", "../../etc/cron.d/x")` is `/out/../../etc/cron.d/x`, and
`os.path.join("/out", "/etc/passwd")` is `/etc/passwd` — join discards the left operand
entirely when the right is absolute. Combined with `makedirs(..., exist_ok=True)` the
function will happily create the intervening directories on the way out of the destination.

**Why it is here.** `bandit` has no path-traversal check and ruff's `S` rules do not cover
it, so **the grounding suite reports nothing at all on this file**. That makes 012 one of the
cases that answers docs/11 R3 — "isn't this just a linter wrapper?" — because an arm that
only re-rates tool output scores zero on it. The locator is a line range for the same reason:
a `rule` locator would name a code no tool emits, which the loader now refuses.

**The fix, and the two regressions to avoid.** Containment is
`os.path.realpath(target).startswith(os.path.realpath(destination_dir) + os.sep)`, checked
*after* resolution — a check on the raw `member_name` for `".."` misses symlinks and absolute
paths. The two recorded regressions are the shortcuts: refusing nested members entirely
(breaks the test) and returning None instead of the path (breaks the caller's contract).

**What a shallow fix looks like.** `member_name.replace("..", "")` is the reflex, and it is
wrong twice: it does not handle an absolute path, and `....//` survives it. A critic that
accepts that fix is worth catching, though that judgement belongs to the judge rather than to
a locator.

**Contamination.** Zip Slip is well published, including the exact `os.path.join` shape. As
with every case here, that is fine for comparing systems on identical inputs — see docs/07 §
Provenance and contamination.
