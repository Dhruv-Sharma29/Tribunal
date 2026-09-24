#!/usr/bin/env bash
# Promote a sweep to the baseline the nightly compares against.
#
# `nightly.yml` looks for `eval/results/baseline/raw.jsonl` and skips the regression check
# when it is absent. Nothing produced that file until this script existed — a consumer with
# no producer, which is the failure this project has now found six times.
#
#   ./scripts/promote-baseline.sh eval/results/20261018T031500Z
#
# Promote deliberately, and only after the thresholds are frozen: every later nightly is
# measured against whatever is here, so promoting a lucky sweep quietly raises the bar and
# promoting a bad one quietly lowers it.

set -euo pipefail
cd "$(dirname "$0")/.."

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <results directory>" >&2
  exit 64
fi

src=$1
[[ -f "$src/raw.jsonl" ]] || { echo "no raw.jsonl in $src" >&2; exit 64; }

# Refuse a sweep that errored: a baseline is a claim about what the system does, and a run
# with missing cases understates it in a way every later comparison inherits.
if grep -q '"kind": *"error"' "$src/raw.jsonl"; then
  echo "$src contains errored runs — fix them and re-sweep before promoting." >&2
  exit 1
fi

dest=eval/results/baseline
rm -rf "$dest"
mkdir -p "$dest"
cp "$src/raw.jsonl" "$src/summary.md" "$dest/"
[[ -f "$src/report.html" ]] && cp "$src/report.html" "$dest/"

# The provenance of a baseline matters more than the numbers in it.
python - "$src" "$dest" <<'PY'
import json, sys, pathlib
src, dest = sys.argv[1], sys.argv[2]
header = next(
    json.loads(line)
    for line in pathlib.Path(src, "raw.jsonl").read_text().splitlines()
    if line.strip() and json.loads(line).get("kind") == "header"
)
pathlib.Path(dest, "PROVENANCE.json").write_text(
    json.dumps({"promoted_from": src, "sweep": header}, indent=2, sort_keys=True) + "\n"
)
print(f"baseline <- {src}  ({header.get('timestamp')})")
PY

echo "commit eval/results/baseline/ — the nightly compares every run against it."
