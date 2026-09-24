# 023 — SSRF, and a sort that never changes

**The security issue.** `urlopen` on a user-supplied URL is two defects wearing one coat.
It is SSRF — the URL can name `127.0.0.1`, a private range, or a cloud metadata endpoint —
*and* `urlopen` honours `file://` and `ftp://`, so `file:///etc/passwd` is a local file read
with no network involved at all. `bandit` B310 names the second one ("audit url open for
permitted schemes") and says nothing about the first.

That split is the interesting part. A critic that repeats the tool has covered the scheme
question and missed the more likely exploitation; a critic that says "restrict the scheme
**and** resolve the host and reject private ranges" has said something the finding does not.
`high`, because either half is a serious defect with no additional condition needed.

**The performance issue.** `sorted(...)` inside the owner loop, over data the loop does not
modify. Hoisting it is the whole fix. This is a different shape from the other performance
cases in the set — not a wrong data structure (002, 020) and not redundant I/O (018), but
invariant work repeated, like 017 without the regex cache to hide it.

**Why they are independent.** Different functions, no shared state. The fix for either is
untouched by the fix for the other, which is what separates this category from 003, 008 and
009 — and makes it the control that shows whether an arm only reports trade-offs when there
is one.

**What would make this case wrong.** If `largest_per_owner` mutated `avatars`, the sort would
not be invariant and the hoist would be incorrect — that would make it a subtler case and a
worse one, because the declared fix would be wrong. It does not, and the test pins the
behaviour.

**Contamination.** SSRF and `file://` via `urlopen` are both well published.
