### Eval results (n=0, 2026-09-26T10:15:42Z)

models: arbiter=claude-opus-5 · coder=claude-opus-5 · postmortem=claude-opus-5 · profiler=claude-opus-5 · redteam=claude-opus-5
prompts: arbiter@v1 arbiter_affirm@v1 code@v1 coder@v4 judge@v1 postmortem@v1 profiler@v1 redteam@v1
severity weights: info0 low1 medium4 high16 · prices 2026-09-16 · splits dev

judge: not run — M1 counts mechanical matches only, and M2 is absent. Nothing in this table depends on a judge.

| Metric                      |  |
|-----------------------------||
| M4 patch applies+passes     |  |
| M1 known-issue recall       |  |
|    of which mechanical      |  |
| M2 regressions introduced   |  |
| M5 outcome accuracy         |  |
| M3 false positives (canary) |  |
| M8 re-rated / comparable    |  |
| M6 rounds p50               |  |
| M7 cost p50 / run           |  |
| M7 cost p95 / run           |  |

no runs

**32 run(s) errored and are excluded:**
- `B0` on `001-shell-injection-report`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B1` on `001-shell-injection-report`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B2` on `001-shell-injection-report`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B3` on `001-shell-injection-report`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B0` on `002-quadratic-membership`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B1` on `002-quadratic-membership`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B2` on `002-quadratic-membership`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B3` on `002-quadratic-membership`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B0` on `003-validated-query-in-hot-loop`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B1` on `003-validated-query-in-hot-loop`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B2` on `003-validated-query-in-hot-loop`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B3` on `003-validated-query-in-hot-loop`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B0` on `004-normalise-names`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B1` on `004-normalise-names`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B2` on `004-normalise-names`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B3` on `004-normalise-names`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B0` on `005-eval-trusted-config`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B1` on `005-eval-trusted-config`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B2` on `005-eval-trusted-config`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B3` on `005-eval-trusted-config`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B0` on `006-rotated-api-key`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B1` on `006-rotated-api-key`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B2` on `006-rotated-api-key`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B3` on `006-rotated-api-key`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B0` on `007-internal-pickle-cache`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B1` on `007-internal-pickle-cache`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B2` on `007-internal-pickle-cache`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B3` on `007-internal-pickle-cache`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B0` on `012-archive-extract-path`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B1` on `012-archive-extract-path`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B2` on `012-archive-extract-path`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set
- `B3` on `012-archive-extract-path`: ProviderUnavailable: anthropic: ANTHROPIC_API_KEY is not set

Cases are hand-written in well-known vulnerability classes and likely resemble training data — see docs/07 § Provenance and contamination. That is fine for comparing systems on identical inputs and not fine for claiming absolute capability.
