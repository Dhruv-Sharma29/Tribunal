# 008 — the security property is the cost

**Why this is the cleanest conflicting case in the set.** Most "conflicts" dissolve under
examination: two issues on one line usually have independent fixes. Here they cannot, because
a password KDF's security property **is** its cost. Strengthening the derivation means
increasing the work per login; reducing the work per login means weakening the derivation.
There is no third option that improves both — that is what "irreconcilable" means, and it is
rare enough that most of the 3 conflicting cases have to be constructed carefully.

- **Security remedy:** replace the md5 loop with `hashlib.scrypt` or PBKDF2-HMAC-SHA256 at a
  modern cost. Slower by design, by roughly an order of magnitude at defensible parameters.
- **Performance remedy:** the comment says the iteration count is tuned to a 40ms p99 login
  budget. Any strengthening spends that budget, and the budget is real.

**What resolves it, and why the file cannot.** The genuine answer depends on facts not in
this file: the login rate, whether hashing can move off the request path, whether the p99
budget is a product requirement or an artefact. That is exactly the shape docs/04 asks a
`TRADEOFF` to emit — ship one side, record the standing objection, name the condition that
reverses it, and say a human decides because nobody in the run knows the login volume.

**What each arm can do.** B0–B2 must silently pick a side; they have no representation for
"both concerns stand". B3 can reach `TRADEOFF` if detector 2 fires: both issues grounded,
both MEDIUM+, opposite dimensions, overlapping spans at the derivation loop. docs/07 M5 says
to report that as a capability difference rather than as points.

**The regression to watch.** `verify` uses `hmac.compare_digest`, and a rewrite that returns
`derived == expected` reintroduces a timing oracle while "fixing" the hash. That is a
security fix that creates a security bug, which is precisely what M2 measures — and it is
recorded in `forbidden_regressions` so it is not only the judge's job to notice.

**Why `split: heldout`.** The first case written for the held-out half. Authoring it means
reading it, which is unavoidable; the discipline docs/07 asks for is that no threshold and no
prompt is tuned against its *results*.

**Contamination.** md5-for-passwords is heavily published. The tension with a stated latency
budget is the part that has to be reasoned about.
