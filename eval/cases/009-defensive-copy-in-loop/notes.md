# 009 — the copy that fixes the aliasing is the copy the loop cannot afford

**Two defects, one remedy, opposite signs.**

`settings = DEFAULTS` binds the module-level dict; `settings.update(overrides)` mutates it.
So a caller who passes `{"scopes": ["read", "write"]}` silently upgrades the scopes of every
later call in the process — a privilege leak across requests, which is why this is `high` and
security rather than a correctness nit. Then `payload = settings` aliases that same dict into
every request, so all of them share `order_id`.

The fix is copying: `dict(DEFAULTS)` per call and a fresh dict per order. The performance
issue *is* that copy. The docstring says this runs once per order across tens of millions of
rows in a nightly batch, which is the fact that makes a per-order allocation expensive enough
to argue about.

**Why it is not simply resolvable.** There is a tempting third option — build the constant
part once and only copy the two per-order fields into a small dict — and it is genuinely
better. If a reviewer concludes it is *straightforwardly* better and cheap, this case is
mislabelled and belongs in `both_independent`. The judgement that keeps it here: the payload
shape is the caller's contract, so a request that is no longer the merged settings dict is a
behaviour change, and the honest answer depends on what the consumer expects — which is not
in this file.

**That is the recheck docs/07 implies and this set should do before the held-out sweep.** A
spurious conflicting case inflates M5, the metric the project leads with on capability. Of
the three conflicting cases, 008 is the one that cannot dissolve; this one is the one to
re-examine.

**Locators are line ranges, not rules.** No tool in the suite reports either defect: `ruff`
does not flag the module-dict mutation (it is not a mutable *default argument*), and nothing
flags a copy that is absent. That makes this a novel-issue case in both dimensions — the
strongest form of the "isn't this just a linter wrapper?" question, since an arm that only
relays tool output scores zero here.

**The test that pins the aliasing.** `test_each_request_keeps_its_own_order_id` fails on the
original file. That is deliberate and allowed — docs/07 says the test "SHOULD pass on
before.py **unless the bug is a test failure**", and here it is.

**Contamination.** The mutable-shared-default pattern is well known. The tension with a
stated batch size is not.
