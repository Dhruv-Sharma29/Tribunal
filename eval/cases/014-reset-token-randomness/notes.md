# 014 — the case where the tool's severity is wrong in the interesting direction

**The defect.** `random` is a Mersenne Twister seeded from the clock. Its state is 624 32-bit
words and is fully recoverable from enough consecutive outputs; for a 32-character token from
a 62-character alphabet, a handful of observed tokens is plenty. A predictable
password-reset token is account takeover without a password.

**Why this case earns its place next to the other security ones.** Both `bandit` (B311) and
`ruff` (S311) report it, and both report it as *low* — the rule is "pseudo-random generators
should not be used for security/cryptographic purposes", and the tool cannot know whether
this one is. The file says it is: the function is called `make_reset_token` and its docstring
says single-use password reset.

That gap is exactly the M8 re-rating metric, and this is the cleanest instance of it in the
set: the tool's rating and the correct rating differ by two levels, in the direction that
matters, and the information needed to close the gap is in the file. A critic that relays
`low` has relayed. A critic that says `high` **and explains that the context is a reset
token** has done the job the project claims.

**The fix is one import.** `secrets.choice` is a drop-in. There is no trade-off, no cost, and
no reason for a round of debate — which makes this a good case for watching whether an arm
spends rounds on something that should take one.

**Contamination.** Published everywhere. The measurement here is not "does it know `random`
is weak" — it is whether the severity moves.
