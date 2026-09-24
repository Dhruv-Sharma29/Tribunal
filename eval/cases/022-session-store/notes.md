# 022 — a temp-file race and a hash rebuilt per entry

**The security issue.** `tempfile.mktemp` was deprecated for a reason: it returns a *name*,
and between the return and the open, anything on the box can create that path — classically a
symlink to something the process may write. The prefix contains the session id, so the name is
guessable rather than merely racy, which turns a timing window into a targeted one.
`tempfile.mkstemp` creates the file atomically and hands back a descriptor.

`medium` rather than `high`: it needs a local attacker, which is a real additional condition.
The rubric asks for that condition to be named, and a critic that rates it `high` without
naming one is over-rating.

**The performance issue.** `fingerprint` builds a new sha256 object, concatenates two strings
and hex-encodes, once per entry. One `hashlib.sha256()` updated in a loop does the same job
with one allocation and one encode. The cost is real and linear-with-a-large-constant rather
than a complexity bug, which is why it is `medium`.

**The trap in the obvious fix.** Switching to an incremental hash changes the empty-list
behaviour: `fingerprint([])` currently returns `""`, and a rewrite that returns
`hashlib.sha256().hexdigest()` returns the hash of nothing instead. That is a silent change to
a value other code may be comparing against. `test_empty_entries_have_an_empty_fingerprint`
pins it, and it is recorded as a forbidden regression.

It is also not the same fingerprint function afterwards — chaining hex digests and updating
one hash produce different values. Whether that matters depends on whether any fingerprint is
persisted, which the file does not say. A critic that raises the question is doing better than
the case does; a patch that changes the values silently is the kind of thing M2 exists for.

**Why the two are independent.** Different functions, no shared state, and neither fix touches
the other.

**Contamination.** `mktemp` is a documented deprecation with a documented reason.
