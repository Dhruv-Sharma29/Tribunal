# 010 — canary: correct back-off, and three opinions it invites

**What this case measures.** M3, the false-positive rate, on code that gives a critic more to
work with than 004 does. 004 is small and obviously fine; this one has real surface, and the
surface has three well-known hooks that are **not** defects here:

- **"No jitter — this will thunder."** A real concern in a distributed retry, and not a
  property of a schedule *function*. Jitter belongs at the call site that has the fleet size;
  raising it as a `medium` against a pure `delay_for` is scope creep dressed as rigour.
- **`factor ** (attempt - 1)` overflows / loses precision.** It cannot matter: the result is
  immediately `min`-ed against `max_seconds`, so a huge intermediate is capped before anyone
  sees it. A critic has to notice the cap to know this, which is the difference between
  reading the line and reading the function.
- **Float arithmetic in a delay.** True and irrelevant at second resolution.

Each is defensible as an `info` or a `low` with a stated condition. Any of them at `medium`
or above is a false positive, because docs/07 M3 counts HIGH and MEDIUM.

**Why `dataclass(frozen=True)` and full type hints.** The case should be indisputably
idiomatic, so that anything raised is about the *content* rather than about style. A critic
that raises "missing type hints" here has raised something factually wrong, which is a
different and worse failure than an over-eager severity.

**Read with the seeded-bad canaries.** A system that raises nothing scores 0 false positives
here and 0/3 on 005-007. Neither number means anything alone; docs/11 R1-R2 is the pair.

**Contamination.** No vulnerability to memorise. The risk being measured is the learnt
expectation that a review request implies findings exist.
