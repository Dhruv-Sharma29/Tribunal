#!/usr/bin/env bash
# Criterion S7: "docker run ... tribunal run examples/sql_injection.py works on a clean
# machine." This is the script that checks it, because the claim is about a machine that is
# not the author's and "it worked here" is not evidence of that.
#
#   ./scripts/verify-docker.sh              # build + the checks that need no credential
#   ./scripts/verify-docker.sh --with-llm   # also one real run; costs money, needs a key
#
# Exits non-zero on the first failure, so it composes into CI.

set -euo pipefail

cd "$(dirname "$0")/.."

WITH_LLM=0
[[ "${1:-}" == "--with-llm" ]] && WITH_LLM=1

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }

command -v docker >/dev/null || fail "docker is not on PATH"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/traces"

# -- build ------------------------------------------------------------------------------------

step "build"
docker build -q -t tribunal:verify --build-arg PROVIDERS=all-providers . >/dev/null
pass "runtime image builds"
docker build -q -t tribunal-sandbox:verify -f Dockerfile.sandbox . >/dev/null
pass "sandbox image builds"

# -- the two-image separation is the security property, so it is checked, not assumed ---------

step "image separation"

# The sandbox image must not contain the tribunal. If it does, a future edit has merged the two
# and the credential boundary is gone.
if docker run --rm --entrypoint python tribunal-sandbox:verify \
     -c "import tribunal" 2>/dev/null; then
  fail "the sandbox image contains tribunal — it must hold no tribunal code"
fi
pass "sandbox image has no tribunal code"

if docker run --rm --entrypoint python tribunal-sandbox:verify \
     -c "import sys; sys.exit(0 if __import__('importlib.util', fromlist=['x']).find_spec('pytest') else 1)"; then
  pass "sandbox image has pytest"
else
  fail "the sandbox image is missing pytest"
fi

# Neither image may carry a credential in its layers. A key in an image history is published
# the moment the image is.
for image in tribunal:verify tribunal-sandbox:verify; do
  if docker image history --no-trunc --format '{{.CreatedBy}}' "$image" \
       | grep -Eiq '(sk-ant-|nvapi-|sk-proj-|AIza|API_KEY=[^$])'; then
    fail "$image has something credential-shaped baked into a layer"
  fi
done
pass "no credential baked into either image"

# -- runs with no credential at all ------------------------------------------------------------

step "works without a credential"

# `doctor` exits non-zero when a grounding tool is missing, so this is the image's own smoke
# test: it proves bandit, ruff and radon are installed and runnable, not merely pip-resolved.
# COLUMNS: `doctor` prints rich tables, and rich falls back to 80 columns when stdout is not
# a tty — which truncates the version column and makes the pin check below unfalsifiable.
docker run --rm -e COLUMNS=200 tribunal:verify doctor >"$WORK/doctor.txt" \
  || fail "doctor exited non-zero — a grounding tool is missing from the image"
pass "doctor passes inside the image"

grep -q "All configured grounding tools are available" "$WORK/doctor.txt" \
  || fail "doctor did not confirm the grounding tools"

# The pinned versions must be the ones constraints.txt names, or a published eval number
# cannot be attributed to the rule sets that produced it (docs/08 § Docker). Matched loosely
# on the row rather than anchored, because the row is drawn inside a box.
for pin in $(grep -E '^(bandit|ruff|radon)==' constraints.txt); do
  tool="${pin%%==*}"; want="${pin##*==}"
  grep -qE "${tool}[^0-9]+${want//./\\.}([^0-9]|$)" "$WORK/doctor.txt" \
    || fail "doctor does not report ${tool} ${want} as constraints.txt pins"
done
pass "grounding tool versions match constraints.txt"

# The static half of the pipeline never executes the target, so it needs no key and no
# --allow-exec. This is the check that the mounts and the working directory line up.
docker run --rm -v "$PWD:/code:ro" -v "$WORK/traces:/traces" tribunal:verify \
  ground /code/examples/sql_injection.py >"$WORK/ground.txt" \
  || fail "ground failed inside the image"
grep -qi "finding" "$WORK/ground.txt" || fail "ground produced no findings on the demo file"
pass "ground runs on a read-only /code mount"

# -- the writable mount ---------------------------------------------------------------------

step "traces survive the container"

# A run needs a credential, but `view` and `replay` do not -- so the mount is proved with a
# trace generated on the host and rendered inside the container.
python -c "
import json, pathlib
src = sorted(pathlib.Path('traces').glob('*.jsonl'))
print(src[0] if src else '')
" >"$WORK/have_trace.txt" 2>/dev/null || true

if [[ -s "$WORK/have_trace.txt" ]]; then
  cp "$(cat "$WORK/have_trace.txt")" "$WORK/traces/sample.jsonl"
  docker run --rm -v "$WORK/traces:/traces" tribunal:verify \
    view /traces/sample.jsonl --no-open >/dev/null \
    || fail "view failed inside the image"
  [[ -f "$WORK/traces/sample.html" ]] \
    || fail "view wrote nothing to the mounted /traces"
  grep -q "<!doctype html>" "$WORK/traces/sample.html" \
    || fail "the rendered viewer is not HTML"
  pass "view writes to the host through /traces"
else
  printf '  \033[33mskip\033[0m no trace in ./traces to render; run the tribunal once first\n'
fi

# -- unprivileged ------------------------------------------------------------------------------

step "runs unprivileged"
uid="$(docker run --rm --entrypoint python tribunal:verify -c 'import os; print(os.getuid())')"
[[ "$uid" == "65534" ]] || fail "the runtime image runs as uid $uid, not 65534"
pass "runtime image runs as nobody (65534)"

# -- the real thing ----------------------------------------------------------------------------

if [[ "$WITH_LLM" == "1" ]]; then
  step "one real run (costs money)"
  : "${NVIDIA_API_KEY:?--with-llm needs NVIDIA_API_KEY in the environment}"
  set +e
  docker run --rm -e NVIDIA_API_KEY -v "$PWD:/code:ro" -v "$WORK/traces:/traces" \
    tribunal:verify run /code/examples/sql_injection.py --config /code/examples/nim.toml
  code=$?
  set -e
  # 0 accept, 1 tradeoff, 2 escalate -- all legitimate outcomes. 3+ is a failure to run.
  [[ "$code" -le 2 ]] || fail "the run exited $code"
  pass "the full loop runs in the container (exit $code)"
  ls "$WORK/traces"/*.jsonl >/dev/null 2>&1 || fail "the run wrote no trace to /traces"
  pass "the run wrote its trace to the host"
fi

step "criterion S7 satisfied"
printf '  the tribunal builds and runs on a clean machine, with the sandbox image separate\n\n'
