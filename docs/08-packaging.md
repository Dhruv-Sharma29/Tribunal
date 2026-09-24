# 08 — Packaging and integration surfaces

## CLI (`Typer`)

The primary surface. `Typer` is FastAPI's sibling by the same author — type hints become the CLI,
so it's an afternoon to pick up from a FastAPI background.

```
tribunal run FILE [options]
  --test PATH               pytest file used as the correctness oracle
  --error PATH|-            traceback to seed the Coder
  --max-rounds INT          default 3
  --max-usd FLOAT           default 2.00
  --allow-exec              permit executing the target (off by default)
  --sandbox {docker,subprocess}   default docker if available, else subprocess
  --model TEXT              default claude-opus-5
  --effort {low,…,max}      default high
  --trace PATH              default traces/<run_id>.jsonl
  --trace-level {default,full}
  --view                    write and open trace.html when done
  --json                    machine-readable report on stdout
  --no-cache                bypass the response cache
  -q / -v

tribunal replay TRACE           re-derive the report from a trace, zero API calls
tribunal view TRACE             write + open the single-file HTML viewer
tribunal eval [...]             see 07-evaluation.md
tribunal doctor                 check tools, docker, credentials, print versions
```

Two details that make it feel finished:

- **Exit codes are meaningful**, so it composes into CI: `0` accept, `1` tradeoff, `2` escalate,
  `3` failed, `4` budget exhausted, `64` config/usage error. A code-review tool that always exits 0
  is not usable in a pipeline.

  `64` rather than the `5` this document first proposed: it is `EX_USAGE` from `sysexits.h`,
  and it keeps "you invoked this wrongly" out of the range reserved for run outcomes. A sixth
  outcome added later would otherwise collide with it, and a pipeline keyed on `5` would
  silently start reading a real result as a usage error. Every row is verified offline by
  `scripts/check-exit-codes.sh` ([13](13-implementation-notes.md) § 55).
- **`doctor`** is the first thing anyone runs when it breaks. Check: `bandit`/`ruff`/`radon` on
  PATH with versions, docker availability, credential resolution, prompt versions, price-table
  version. Cheap to write, disproportionate payoff in "someone else ran my project successfully."

### Credentials

Resolve through the SDK's own chain — don't reimplement it. A bare `AsyncAnthropic()` picks up
`ANTHROPIC_API_KEY`, or `ANTHROPIC_AUTH_TOKEN`, or an `ant auth login` profile. Never take an API
key as a CLI flag (it lands in shell history and in `run_start.payload.argv` in the trace).

## Docker

Two images, and the separation is a security property, not packaging tidiness
([05](05-execution-sandbox.md)):

| Image | Contains | Has API key | Network |
|---|---|---|---|
| `tribunal` | tribunal code, grounding tools, prompts | yes | yes (API only) |
| `tribunal-sandbox` | python + pytest, nothing else | **no** | **none** |

```bash
docker run --rm \
  -e ANTHROPIC_API_KEY \
  -v "$PWD:/code:ro" -v "$PWD/traces:/traces" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  ghcr.io/<user>/tribunal:latest \
  run /code/examples/sql_injection.py --view
```

Mounting the docker socket to let the container spawn the sandbox container is
docker-out-of-docker; it grants the container host-root-equivalent access. **Document that
explicitly** and make it opt-in: without the socket mount, the containerised tribunal falls back to
`--sandbox=subprocess` inside its own container (which is already isolated from the host), and
that's the recommended configuration. Noticing this trade-off is worth more than hiding it.

Pin tool versions in the image and print them in `doctor` — `bandit` and `ruff` rule sets change
between releases, and an unpinned image silently invalidates the eval numbers.

## MCP server

The cheapest way to make the **real** tribunal callable from inside an editor, and the correct first
choice if Phase 7 happens at all.

> **Built. This sketch was written against `mcp` 1.x and does not run.** The SDK renamed
> `FastMCP` to `MCPServer` (`mcp.server.mcpserver`) and `Tool.inputSchema` to
> `Tool.input_schema` in 2.x, which is exactly the drift the `[verify]` note below
> anticipated. `src/tribunal/mcp_server.py` is the working version; the shape below is
> kept because the *design* it describes survived unchanged, and because a sketch that was
> right about everything except the import is the most honest thing to leave here.

```python
# The v1 sketch, superseded. See src/tribunal/mcp_server.py for the 2.x version.
from mcp.server.fastmcp import FastMCP          # 2.x: from mcp.server.mcpserver import MCPServer

mcp = FastMCP("tribunal")                     # 2.x: MCPServer(name=..., instructions=...)

@mcp.tool()
async def review_code(
    file_path: str,
    test_path: str | None = None,
    max_rounds: int = 3,                        # built as 2, per the recommendation below
) -> dict:                                      # built as `str`: markdown, per the last note below
    """Run the adversarial tribunal on a Python file.

    Returns the final patch, the structured report, and the trace path.
    """
    report = await orchestrate(...)
    return report.model_dump()

@mcp.tool()
async def get_trace(run_id: str) -> str:
    """Return the debate trace for a previous review, as readable markdown."""
```

Why it's cheap: `orchestrate()` already exists for the CLI, so the MCP server is a thin adapter over
one function — genuinely a few dozen lines, not a rewrite. No FastAPI service is required for this
(the earlier plan assumed one); a stdio MCP server is simpler and has fewer moving parts. Add HTTP
only if a non-MCP client needs it.

Design notes specific to this tool:

- A run takes minutes. Return promptly with a `run_id` and expose `get_trace` for polling, or return
  the full report and accept the wait — pick one deliberately and document it. Recommendation: full
  report with a conservative `max_rounds` default of 2 for the MCP path, since editor tool calls
  have less patience than a CLI user.
- `--allow-exec` defaults **off** over MCP. An editor-triggered tool that executes code on the
  developer's machine is a worse default than the same thing typed deliberately into a terminal.
- Return the trace path *and* a short rendered summary. A tool result that's a wall of JSON is
  unreadable in most host panels.

All three were built as written. The third is the one that changed the signature: both tools
return **markdown**, not `dict`, so nothing renders as a JSON blob in a host panel. A caller
that wants structure reads the trace, whose path is in every summary. `get_trace` re-derives
its report from the trace with the same `report.build` the live run used, so the two cannot
disagree — criterion S6 applied to the MCP path rather than re-implemented for it.

One addition the sketch did not anticipate: naming a `test_path` without `allow_exec` is
**refused** rather than quietly honoured. Accepting it would run the tribunal with pytest
disabled and report a performance dimension nobody measured.

Hosts register a local MCP server via JSON config pointing at the server command. Claude Code,
Cursor, and Windsurf support MCP natively; **[verify]** the exact config file location and schema
per host at implementation time — these have moved more than once. (They did: see
docs/13 § 63. The verification cost an hour and changed two import lines, which is roughly
what a marker like this is for.)

Install and register:

```bash
pip install -e '.[mcp]'      # needs mcp >= 2.0; 1.x fails at resolution, not at import
tribunal-mcp               # stdio server, for a host to launch
```

`tribunal-mcp` is a separate entry point rather than a `tribunal mcp` subcommand: the
host owns the process's stdout, so anything else the CLI might print there corrupts the
protocol stream.

## VS Code extension

A real extension (TypeScript, VS Code Extension API: a command + a webview panel) that calls the
tribunal and renders the trace side-by-side with the diff.

Do this **only** if you specifically want a polished UI artifact, and only after Phases 0–6 are
solid. It is a second codebase in a second language, it works in exactly one editor, and it gives
the same *capability* as the MCP server with more maintenance. The webview can reuse the viewer's
rendering code, which is the one genuine synergy.

## A rules/`AGENTS.md` package — what it is and isn't

Distributing a rules file (`AGENTS.md`, `.cursor/rules/`, and each host's equivalent) makes the
*host editor's own model* follow your review standards. It does not invoke this project's code: no
`bandit` grounding, no parallel critique, no policy layer, no trace. The host's model roleplays a
red-team reviewer using your rubric.

That is a legitimate and useful thing to ship, and it's nearly free once the rubrics exist — the
severity definitions written for [03](03-agents.md) are exactly the content. But be precise about
the claim. "Ships review rules that any AGENTS.md-aware editor will follow" is honest. "Works in
12 editors" implying the tribunal runs in 12 editors is not, and it's the kind of overreach an
interviewer will probe.

The genuinely useful version: an `AGENTS.md` in this repo that documents how to *call the MCP tool*
and what the tribunal's severity rubric means — so the host's agent knows when to reach for the real
thing.

**[verify]** before citing any specifics: which hosts natively read a root `AGENTS.md`, precedence
rules when a host also has its own convention file, and version numbers for any of it. This area
changes fast and stale version claims on a resume are a liability. `ponytail`
(github.com/DietrichGebert/ponytail) was suggested as a reference architecture for multi-host
*distribution* — check it still exists and does what you think before depending on the pattern.

## Recommended order for Phase 7

1. **MCP server** — cheapest, runs the real system, broadest host coverage.
2. **`AGENTS.md`** documenting the MCP tool + the rubric — nearly free on top of (1).
3. **VS Code extension** — only for a dedicated UI artifact.

If time is tight, **skip all three.** A polished CLI + trace viewer + README with a recorded demo
GIF and an honest eval table is a complete, interview-ready artifact. Half-finished IDE support is
worse than none, because it invites exactly the question you can't answer well.
