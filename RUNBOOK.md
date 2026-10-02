# Runbook — the steps that need a credential or a daemon

Everything in this project runs offline except the steps below. Each one is
machinery-complete and has been tested against a scripted provider; none has been *run*,
because the machine it was built on has no API key and no container runtime. This file is
the exact sequence, in order, with what each step unblocks.

Run them on a machine with `NVIDIA_API_KEY` (or `ANTHROPIC_API_KEY`) and Docker.

Steps 1–7 map one-to-one onto [`REMAINING.md`](REMAINING.md) § A1–A7, which says what each
one is *for* and what it costs to skip. This file is the commands; that file is the
reasoning. Step 8 is independent of the rest and can be done at any point.

**Do step 2 first if you only have time for one.** It supplies the missing full-evaluation
recordings. PR CI already runs scripted integration and the committed agent recordings.

---

## 1. Verify the container images — closes criterion S7

```bash
./scripts/verify-docker.sh              # build + the checks that need no credential
./scripts/verify-docker.sh --with-llm   # also one real run
```

Builds both images and checks the properties that make the two-image split a security
boundary rather than packaging tidiness: the sandbox image holds no tribunal code, neither image
has anything credential-shaped in its layer history, both run unprivileged, and the pinned
tool versions match `constraints.txt`.

**Expect it to find something.** It has never been run, and an unbuilt Dockerfile is a
hypothesis. Its shell syntax is checked and its two grep assertions were validated against
real `tribunal doctor` output, including a negative control; everything else is
unexercised.

Then tick the `[~]` box in `docs/09-roadmap.md` Phase 4.

---

## 2. Record the cassettes — enables full evaluation replay

```bash
NVIDIA_API_KEY=... pytest -m live            # the agents' own live tests
TRIBUNAL_LLM__MODE=record NVIDIA_API_KEY=... \
  tribunal eval --split dev --config examples/nim.toml
git add tests/cassettes && git commit
```

Four prompts have live tests and no recordings: `arbiter/v1`, `arbiter_affirm/v1`,
`postmortem/v1`, `judge/v1`. The eval's own requests have none either, which is why
`tribunal eval --smoke` fails today — deliberately, with the recording command in its
error message. PR CI runs the scripted evaluation integration and the committed agent replay
tests instead. Missing full-evaluation recordings still fail `eval --smoke`; they are never
replaced by fabricated responses or a live-network fallback.

This is the one place the roadmap's own advice ("record cassettes as you build each agent,
not in a batch at the end") was not followed, and the cost is exactly what it predicted.

**Note:** the committed cassettes were recorded on
`nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`, and the default is now
`llama-3.3-nemotron-super-49b-v1.5`. `tests/test_coder.py` pins the old model on purpose —
see docs/13 § 58 for why, and do not "fix" it by following the default.

---

## 3. Sweep the dev split

For the scheduled GitHub Actions run, add an `ANTHROPIC_API_KEY` repository secret under
**Settings → Secrets and variables → Actions → New repository secret**. The workflow uses
the default Anthropic agent models; exporting a key on your laptop does not configure
GitHub's runner, and `NVIDIA_API_KEY` does not authenticate Anthropic models. Nightly checks
this before starting a sweep and publishes results in the Actions run summary.

```bash
tribunal eval --split dev --arms B0,B1,B2,B3 --config examples/nim.toml
```

8 cases × 4 arms. Writes `eval/results/<ts>/{raw.jsonl,summary.md,report.html,traces/}`.
No judge yet, so M1 counts mechanical locator matches only and M2 is absent — the report
says so in a banner.

---

## 4. Validate the judge — gates everything held-out

```bash
tribunal judge-labels eval/results/<ts>       # offline; writes eval/judge-labels.jsonl
# ... fill in `hand_label` on all 30 rows, BEFORE the next command ...
tribunal judge-kappa --results eval/results/<ts>
```

The worksheet is sampled seeded from the judge's real workload rather than chosen, and it
deliberately carries no judge verdict: labelling after seeing one measures agreement with an
anchor, and nothing downstream can detect that it happened.

`judge-kappa` writes `eval/judge-kappa.json` and **exits non-zero below κ = 0.6**. docs/07:
fix the rubric before running anything held-out. `heldout.yml` refuses to start without this
file.

Publishing this number is worth more than any headline result — it is the sentence that tells
a reader the other numbers mean something.

---

## 5. Freeze the thresholds

Tune `accept_threshold`, `no_progress_epsilon` and `Severity.weight` on the **dev split
only**, then stop. Every sweep header records all three, so a later reader can tell which
configuration produced which number — but changing them after a published result invalidates
it, and nobody wants to re-run a $25 sweep.

Re-examine **case 009** at this point. Its own `notes.md` flags it: if a reviewer can name a
clean fix satisfying both concerns, it belongs in `both_independent`, and a spurious
conflicting case inflates M5 — the metric the project leads with on capability.

---

## 6. Promote a baseline — arms the nightly's regression gate

```bash
./scripts/promote-baseline.sh eval/results/<ts>
git add eval/results/baseline && git commit
```

`nightly.yml` compares each run's M4 against this and skips the check when it is absent. The
script refuses a sweep containing errored runs: a baseline is a claim about what the system
does, and one with missing cases understates it in a way every later comparison inherits.

---

## 7. The held-out sweep — criterion S5

```bash
gh workflow run heldout.yml -f arms=B1,B3 -f reason="milestone 1"
```

Manual only, and it requires a stated reason. docs/07: run it once per milestone and **report
every held-out run you did, not the best one**. If you tune anything against held-out
results, say so and demote them to dev.

Commit the results directory. A score in a README with no run behind it is not evidence.

---

## 8. Register the MCP server in a real host

*Independent of steps 1–7. Needs a key and an editor, not a sweep.*

```bash
pip install -e '.[mcp]'
tribunal-mcp                    # stdio; a host launches this, you do not
```

Then register `tribunal-mcp` as a local stdio MCP server in the host's config and call
`review_code` on a real file.

What is already covered, so you know what you are actually checking: the tools are driven
in-process against a scripted provider (34 tests), and `tests/test_mcp_stdio.py` launches
the server as a subprocess and speaks real JSON-RPC to it — `initialize`, `tools/list`, and
an assertion that the first byte on stdout is protocol rather than a banner. Neither needs a
credential.

So the **transport is exercised**. What is not:

- `review_code` end to end against a live provider, from a host rather than a test.
- The per-host config file location and schema. docs/08 marks this `[verify]` and it has
  moved more than once — docs/13 § 63 is what happened last time.
- Whether a multi-minute tool call is tolerable in a given host's UI. `max_rounds` defaults
  to 2 for exactly this reason; if a host still times out, that is the number to lower.

---

## What this unblocks in the README

The eval results table and the demo GIF are the last two Phase 6 items, and both need a real
run. The README deliberately contains no results table today — see its Design decisions
section, which says so.

[`REMAINING.md`](REMAINING.md) tracks the same work as a ledger, including the two Phase 7
items that were **deliberately skipped** rather than blocked (`AGENTS.md`, the VS Code
extension) — those are judgment calls and are meant to be overruled if you disagree.
