# 05 — Execution sandbox

## Why this document exists

The original plan said "run `cProfile` or `timeit` on before/after versions." Read that again: the
system takes code from an untrusted source (a user's buggy file), asks an LLM to modify it, and then
**executes the result on the developer's machine**. Add `pytest`, which imports and runs arbitrary
module-level code, and you have a remote-code-execution pipeline with a friendly CLI.

This is not hypothetical paranoia. Two realistic paths:

1. A user runs `tribunal run downloaded_snippet.py` on a file with a malicious
   `os.system(...)` at module scope. `pytest` collection executes it. Nothing the LLM does is
   involved.
2. The Coder, fixing a "slow file read," writes a patch using `subprocess` — models produce
   `shell=True` regularly — and the Profiler's benchmark run executes it.

A code-review tool that can be owned by the code it reviews is not shippable, and "I noticed the
execution boundary and designed for it" is a stronger interview signal than any amount of
orchestration cleverness. Do this in Phase 2, not as polish.

## Threat model

| | |
|---|---|
| **Untrusted inputs** | the user's source file; the Coder's diff; the user's test file |
| **Trusted** | our own code, prompts, config, the grounding tools' binaries |
| **Assets to protect** | the developer's filesystem and credentials (`~/.ssh`, `~/.aws`, `ANTHROPIC_API_KEY`), the host network, the host's CPU/RAM, the trace store |
| **Out of scope** | a determined attacker with kernel exploits; malicious *grounding tool* binaries; protecting against the user attacking their own machine deliberately |
| **Assumed adversary** | opportunistic malicious snippet, or a careless/confused model. Not a targeted APT. |

Stating the out-of-scope items matters: a sandbox that claims to stop everything is a sandbox
nobody believes.

## Defence in depth

Three layers. Layer 1 is mandatory, layers 2–3 are what makes the claim credible.

### Layer 1 — never execute by default

`GROUND` runs **static** tools only (`bandit`, `ruff`, `radon`) unless execution is explicitly
enabled. Static analysis on a file does not run it.

```
tribunal run file.py                    # static grounding only. safe by default.
tribunal run file.py --test t.py        # requires --allow-exec, else hard error
tribunal run file.py --test t.py --allow-exec
tribunal run file.py --test t.py --allow-exec --sandbox=docker   # recommended
```

The default is the safe one, and the unsafe one requires an explicit flag whose name says what it
does. The Profiler degrades gracefully to `unmeasurable` ([03](03-agents.md) § 3.3), which the
schema already supports — the design absorbs "no execution" without special-casing.

### Layer 2 — process isolation (`--sandbox=subprocess`, default when exec is allowed)

For users who won't run Docker. Weaker, and documented as such.

```python
# src/tribunal/sandbox.py
import os, resource, subprocess, tempfile, sys

DENY_ENV = ("ANTHROPIC_API_KEY", "AWS_", "GITHUB_", "OPENAI_", "SSH_", "GOOGLE_")

def _limits() -> None:                     # runs in the child, post-fork, pre-exec
    resource.setrlimit(resource.RLIMIT_CPU,   (10, 10))            # 10s CPU
    resource.setrlimit(resource.RLIMIT_AS,    (512<<20, 512<<20))  # 512 MB address space
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))            # fork bomb ceiling
    resource.setrlimit(resource.RLIMIT_FSIZE, (16<<20, 16<<20))    # 16 MB writes
    resource.setrlimit(resource.RLIMIT_CORE,  (0, 0))              # no core dumps
    os.setsid()                                                     # own process group

def run_in_sandbox(argv: list[str], cwd: str, timeout: int = 30) -> SandboxResult:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(DENY_ENV) and k not in DENY_ENV}
    env |= {"PYTHONDONTWRITEBYTECODE": "1", "HOME": cwd, "TMPDIR": cwd,
            "PYTHONPATH": cwd, "PATH": "/usr/bin:/bin"}
    proc = subprocess.Popen(
        argv, cwd=cwd, env=env, preexec_fn=_limits,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)   # kill the GROUP, not just the child
        return SandboxResult(timed_out=True, ...)
    ...
```

Details that are easy to get wrong:

- **Kill the process group.** `proc.kill()` leaves grandchildren alive. `os.setsid()` in the child
  plus `os.killpg` is the correct pair.
- **Scrub the environment, don't inherit it.** Allowlist, not denylist, for anything that matters;
  the `DENY_ENV` prefix scan above is a backstop, and `HOME`/`TMPDIR` are redirected into the
  scratch dir so a write to `~/.bashrc` lands in a temp directory.
- **Fresh `tempfile.mkdtemp()` per run**, copied inputs only, deleted after. Never execute in the
  user's working directory — a test that writes files must not touch their repo.
- **`RLIMIT_AS` over `RLIMIT_DATA`** — the latter misses `mmap`.
- **No network isolation at this layer.** `preexec_fn` cannot create a network namespace without
  privileges. State this limitation in the CLI's help text; it's the main reason to prefer Docker.
- `preexec_fn` is unsafe in multi-threaded parents. The orchestrator is `asyncio`-based
  (single-threaded), so this is fine — but note it, because moving to a thread pool later would
  break it subtly.

### Layer 3 — container isolation (`--sandbox=docker`, recommended)

```dockerfile
# Dockerfile.sandbox — no tribunal code, no API key, no network
FROM python:3.11-slim
RUN useradd -u 65534 -m runner && pip install --no-cache-dir pytest
USER runner
WORKDIR /work
ENTRYPOINT ["python", "-I"]
```

```
docker run --rm \
  --network none \
  --read-only --tmpfs /work:rw,size=64m,noexec \
  --user 65534:65534 \
  --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 128 --memory 512m --cpus 1 \
  -v "$SCRATCH:/work/in:ro" \
  tribunal-sandbox:latest /work/in/runner.py
```

`--network none` is the flag that actually matters — it removes exfiltration of anything the
sandbox might read, and it removes "download stage 2" entirely. The tribunal image and the sandbox
image are **separate** so the API key never exists in a namespace that runs model-written code.

`noexec` on the tmpfs blocks the write-then-execute pattern. `--read-only` plus a single writable
tmpfs means nothing survives the run.

## What the Profiler is allowed to run

Only two things, both wrapped:

1. `pytest <user_test> -x -q --timeout=20` — correctness oracle and `cProfile` target.
2. A generated `runner.py` that `timeit`s an explicitly declared entry point.

Never the target module directly, never an interactive interpreter, never anything the model wrote
free-form. The `timeit` runner is *our* template with the model-supplied call expression inserted as
a literal string, and the insertion point is validated by `ast.parse` to be a single call
expression — not a statement list.

## Pre-execution static gate

Before any execution, a cheap AST scan on the patched file. This is a hygiene tripwire, not a
security boundary (it is trivially bypassable by `getattr`), and must be documented as such — but it
catches the realistic cases and it gives the Red-team a high-quality `TOOL_FINDING` for free:

```python
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}
FORBIDDEN_MODULES = {"socket", "http", "urllib", "requests", "ftplib", "smtplib",
                     "ctypes", "multiprocessing", "pickle", "marshal", "shutil"}
SUSPICIOUS = {("subprocess", "*"), ("os", "system"), ("os", "popen"), ("os", "remove")}
```

A hit does not block the run; it becomes a `GroundingFinding` with `tool: "astgate"`. Whether it's
an `Issue` is the Red-team's call — which is exactly the division of labour the whole system is
built on. If it's in `FORBIDDEN_MODULES` and execution was requested, refuse to execute and record
`tool_errors["sandbox"] = "refused: network module import"`.

## Tests for this layer

`tests/test_sandbox.py` — these run in CI and are the evidence that the boundary is real:

| Test | Asserts |
|---|---|
| CPU burner (`while True: pass`) | killed by `RLIMIT_CPU`, run continues |
| Memory hog (`[0]*10**10`) | `MemoryError` or kill, no host OOM |
| Fork bomb | `RLIMIT_NPROC` holds, host stays responsive |
| `open(os.path.expanduser("~/.ssh/id_rsa"))` | `FileNotFoundError` (HOME redirected) |
| `os.environ["ANTHROPIC_API_KEY"]` | `KeyError` |
| Write to `$CWD/../evil.txt` | confined to scratch dir; scratch removed after |
| Grandchild process outlives parent | killed by `killpg` |
| `socket.create_connection(("1.1.1.1", 80))` | fails under `--sandbox=docker`; **documented as passing under `--sandbox=subprocess`** |
| Timeout path | `SandboxResult.timed_out` set, trace event emitted, FSM continues |

That last docker-vs-subprocess row is the honest one. Write the failing-under-subprocess case as an
`xfail` with a reason string rather than deleting it — the test suite documenting its own
limitation is better than a suite that looks complete.

## README claim

Write exactly this, and nothing stronger:

> Code the tribunal writes is executed only with `--allow-exec`. With `--sandbox=docker` (recommended)
> execution happens in a network-less, read-only container as an unprivileged user with dropped
> capabilities and CPU/memory/PID limits. With `--sandbox=subprocess` the same resource limits and a
> scrubbed environment apply, **but network access is not blocked** — use Docker if the input is
> untrusted. Static grounding (`bandit`, `ruff`, `radon`) never executes the target.

## Supported execution platform

Layer 2 (`--sandbox=subprocess --allow-exec`) requires Linux, including in CI. macOS rejects the address-space limit used here, and Windows does not provide this POSIX execution boundary. Run the Linux container or use Docker mode with a running Docker engine on macOS. Static grounding and execution-disabled review do not require the subprocess sandbox. Sandbox resource-limit tests must run on Linux; a macOS failure is not a reason to remove these limits.
