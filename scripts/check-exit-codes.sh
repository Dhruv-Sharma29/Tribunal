#!/usr/bin/env bash
# The CLI's contract with shell scripts and CI, verified from a shell script.
#
# docs/09-roadmap.md Phase 4: "Exit codes verified in a shell script." docs/08: "A
# code-review tool that always exits 0 is not usable in a pipeline" -- so the codes are a
# contract, and a contract nobody executes is a comment.
#
# Every check here runs offline against committed trace fixtures. That is possible because
# `replay` exits with the outcome of the run it is reporting, which is also the property
# that lets CI re-check a recorded run. Before that, verifying the outcome codes meant
# spending money, so in practice nobody would have.
#
#   ./scripts/check-exit-codes.sh

set -uo pipefail
cd "$(dirname "$0")/.."

CLI=${CLI:-"python -m tribunal.cli"}
TRACES=tests/fixtures/traces
failures=0

check() {
  local want=$1 desc=$2; shift 2
  "$@" >/dev/null 2>&1
  local got=$?
  if [[ "$got" == "$want" ]]; then
    printf '  \033[32mok\033[0m   %-42s exit %s\n' "$desc" "$got"
  else
    printf '  \033[31mFAIL\033[0m %-42s wanted %s, got %s\n' "$desc" "$want" "$got"
    failures=$((failures + 1))
  fi
}

printf '\n\033[1mrun outcomes (replayed from committed traces, no API calls)\033[0m\n'
check 0  "accept"                           $CLI replay $TRACES/accept.jsonl
check 1  "tradeoff"                         $CLI replay $TRACES/tradeoff.jsonl
check 2  "escalate"                         $CLI replay $TRACES/escalate.jsonl
check 3  "failed (nothing reviewable)"      $CLI replay $TRACES/failed.jsonl
check 4  "budget exhausted"                 $CLI replay $TRACES/budget.jsonl

printf '\n\033[1musage errors (64 = EX_USAGE)\033[0m\n'
check 64 "run: no such file"                $CLI run does-not-exist.py
check 64 "run: --test without --allow-exec" $CLI run examples/sql_injection.py --test examples/test_fetch.py
check 64 "ground: no such file"             $CLI ground does-not-exist.py
check 64 "view: no such trace"              $CLI view does-not-exist.jsonl --no-open
check 64 "replay: no such trace"            $CLI replay does-not-exist.jsonl
check 64 "postmortem: no such trace"        $CLI postmortem does-not-exist.jsonl
check 64 "eval: unknown arm"                $CLI eval --dry-run --arms B9

printf '\n\033[1mcommands that report no run\033[0m\n'
check 0  "doctor"                           $CLI doctor
check 0  "ground on a real file"            $CLI ground examples/sql_injection.py
check 0  "eval --dry-run"                   $CLI eval --dry-run

printf '\n'
if [[ "$failures" -gt 0 ]]; then
  printf '\033[31m%s check(s) failed\033[0m\n\n' "$failures"
  exit 1
fi
printf '\033[32mevery documented exit code is reachable and correct\033[0m\n\n'
