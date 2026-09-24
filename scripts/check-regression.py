#!/usr/bin/env python3
"""Compare a sweep against a committed baseline and fail on a real regression.

docs/07-evaluation.md § CI integration: the nightly run "fails on regression beyond a
threshold in M4". This is that check, and the threshold is the whole design problem.

## Why a threshold at all, and why it is in cases

M4 is a count over ~8 dev cases. The arms are LLMs, so a rerun of the identical commit will
not give the identical number — one case flipping is normal noise and six percentage points
at once. A nightly that fails on any decrease would be red most mornings and would be muted
within a week, which is worse than not having it.

So the gate is: **fail when M4 drops by more than `--tolerance` cases** (default 1). That is
deliberately blunt. It catches "the Coder prompt broke and half the patches stopped applying"
and ignores "one case went the other way", which is the only distinction a count this small
can support. Expressing it in cases rather than percent is the same honesty docs/07 asks of
the results table.

A drop *within* tolerance is still printed, because two consecutive tolerated drops are a
trend and nobody would see it otherwise.

## What it does not do

It does not gate on M1 or M2. Both depend on the judge, and until the judge has a published
kappa a change in either is not attributable to the tribunal — it could be the judge. Gating on
an unvalidated instrument would make the nightly fail for reasons nobody can act on.

    scripts/check-regression.py --baseline eval/results/<ts>/raw.jsonl \\
                               --current  eval/results/<new>/raw.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load(path: Path) -> tuple[dict, list[dict]]:
    """Return `(header, score rows)` from a `raw.jsonl`."""
    header: dict = {}
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("kind") == "header":
            header = row
        elif row.get("kind") == "score":
            rows.append(row)
    return header, rows


def m4_by_arm(rows: list[dict]) -> dict[str, tuple[int, int]]:
    """`{arm: (patches correct, runs)}`."""
    out: dict[str, list[int]] = {}
    for row in rows:
        entry = out.setdefault(row["arm"], [0, 0])
        entry[0] += int(bool(row.get("fix_correct")))
        entry[1] += 1
    return {arm: (correct, total) for arm, (correct, total) in out.items()}


def compare(baseline: Path, current: Path, tolerance: int) -> int:
    base_header, base_rows = load(baseline)
    head_header, head_rows = load(current)

    base = m4_by_arm(base_rows)
    head = m4_by_arm(head_rows)

    print(f"baseline {baseline}  ({base_header.get('timestamp', 'unknown')})")
    print(f"current  {current}  ({head_header.get('timestamp', 'unknown')})\n")

    # A prompt change is the most likely cause of a real M4 move, so name it before the
    # numbers: a drop with a prompt version change is a different investigation from a drop
    # without one.
    changed = [
        f"{role}: {base_header.get('prompt_versions', {}).get(role)} -> {stamp}"
        for role, stamp in head_header.get("prompt_versions", {}).items()
        if base_header.get("prompt_versions", {}).get(role) != stamp
    ]
    if changed:
        print("prompt versions changed since the baseline:")
        for line in changed:
            print(f"  {line}")
        print()

    failures = 0
    for arm in sorted(set(base) | set(head)):
        if arm not in base:
            print(f"  {arm}: new arm, {head[arm][0]}/{head[arm][1]} — nothing to compare")
            continue
        if arm not in head:
            print(f"  {arm}: MISSING from the current run (was {base[arm][0]}/{base[arm][1]})")
            failures += 1
            continue
        was, was_of = base[arm]
        now, now_of = head[arm]
        delta = now - was
        note = ""
        if was_of != now_of:
            # A different denominator makes the counts incomparable, and silently comparing
            # them anyway is how a shrinking benchmark looks like an improving system.
            print(
                f"  {arm}: {was}/{was_of} -> {now}/{now_of}  DIFFERENT CASE COUNT — "
                "not comparable"
            )
            failures += 1
            continue
        if delta < -tolerance:
            note = f"  REGRESSION (worse by {-delta}, tolerance {tolerance})"
            failures += 1
        elif delta < 0:
            note = f"  down {-delta}, within tolerance — watch it"
        elif delta > 0:
            note = f"  up {delta}"
        print(f"  {arm}: M4 {was}/{was_of} -> {now}/{now_of}{note}")

    print()
    if failures:
        print(f"{failures} regression(s). Failing.")
        return 1
    print("no M4 regression beyond tolerance.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument(
        "--tolerance",
        type=int,
        default=1,
        help="Cases M4 may drop by without failing. In cases, not percent.",
    )
    args = parser.parse_args(argv)
    for path in (args.baseline, args.current):
        if not path.is_file():
            print(f"no such results file: {path}", file=sys.stderr)
            return 2
    return compare(args.baseline, args.current, args.tolerance)


if __name__ == "__main__":
    raise SystemExit(main())
