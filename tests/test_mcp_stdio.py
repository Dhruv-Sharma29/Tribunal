"""The MCP server over a real pipe, as a host speaks to it.

Separate from `test_mcp.py` because these are **synchronous**: they drive a subprocess and
read its stdout, so the async fixtures and the module-level `asyncio` mark next door do not
apply and pytest-asyncio warns when they are left on.

Separate also because they test a different thing. `test_mcp.py` calls `server.call_tool()`
in-process, which never touches `main()`, the stdio transport, or the module's import path
under `-m`. That left the one path a host actually uses as the only unexercised one -- and it
is where a stray `print`, a slow import, or a crash at startup turns into a server that
connects and lists no tools, with no error anywhere.

No credential is needed: `initialize` and `tools/list` are protocol, not model calls.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tribunal.mcp_server import TOOL_NAMES

pytest.importorskip("mcp", reason="the MCP server is an optional extra")

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT = 30


def start():
    return subprocess.Popen(
        [sys.executable, "-m", "tribunal.mcp_server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, cwd=ROOT,
    )


def send(proc, message: dict) -> None:
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "test", "version": "0"}},
}


def test_the_server_answers_a_real_stdio_handshake():
    proc = start()
    try:
        send(proc, INITIALIZE)
        initialised = json.loads(proc.stdout.readline())
        assert initialised["result"]["capabilities"]["tools"] is not None

        send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = json.loads(proc.stdout.readline())
    finally:
        proc.terminate()
        proc.wait(timeout=TIMEOUT)

    assert {tool["name"] for tool in listed["result"]["tools"]} == set(TOOL_NAMES)


def test_nothing_but_protocol_reaches_stdout():
    """The host owns stdout. A banner, a warning or a stray print corrupts the stream, and
    the symptom is a server that connects and then behaves as though it has no tools --
    which is why `tribunal-mcp` is its own entry point rather than a CLI subcommand."""
    proc = start()
    try:
        send(proc, INITIALIZE)
        first = proc.stdout.readline()
    finally:
        proc.terminate()
        proc.wait(timeout=TIMEOUT)

    # The very first byte on the pipe must be the JSON-RPC reply, not a greeting.
    assert first.startswith("{"), f"stdout opened with non-protocol output: {first[:200]!r}"
    assert json.loads(first)["id"] == 1


def test_the_advertised_entry_point_matches_the_module_that_was_just_driven():
    """`pyproject` promises `tribunal-mcp`; the tests above drive `-m
    tribunal.mcp_server`. They must be the same code, or this file tests a path nobody
    runs."""
    import tomllib

    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["tribunal-mcp"] == "tribunal.mcp_server:main"
