"""Evidence that the execution boundary is real.

One test per row of the table in docs/05-execution-sandbox.md § Tests for this layer. Two rows
are `xfail` with a reason rather than deleted: a suite that documents its own limitation is
better than one that looks complete (docs/05 § Tests for this layer).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from tests.conftest import running_as_root
from tribunal.config import SandboxConfig
from tribunal.sandbox import (
    DENY_ENV_PREFIXES,
    ExecutionNotPermitted,
    Sandbox,
    scratch_dir,
)


def run_py(sandbox: Sandbox, workdir: Path, code: str, **kwargs):
    return sandbox.run([sandbox.python_for_sandbox(), "-c", code], workdir, **kwargs)


# -- Layer 1 --------------------------------------------------------------------------------


def test_execution_is_denied_by_default():
    """Layer 1. The default is the safe one; the unsafe one needs a flag that says so."""
    with scratch_dir() as d, pytest.raises(ExecutionNotPermitted, match="--allow-exec"):
        Sandbox(SandboxConfig()).run(["/bin/echo", "hi"], d)


def test_static_grounding_needs_no_execution():
    """The corollary: the safe-by-default path is not a degraded path.

    bandit/ruff/radon/astgate analyse the file without running it, so the default
    configuration still produces grounded critiques.
    """
    assert Sandbox(SandboxConfig()).config.allow_exec is False


# -- Resource limits ------------------------------------------------------------------------


def test_cpu_burner_is_killed_and_the_run_continues(exec_sandbox):
    with scratch_dir() as d:
        started = time.monotonic()
        result = run_py(exec_sandbox, d, "while True: pass")
        elapsed = time.monotonic() - started
        assert not result.ok
        assert elapsed < exec_sandbox.config.wall_timeout_seconds + 2
        # The run continues: the sandbox returns a result rather than raising.
        assert run_py(exec_sandbox, d, "print('still here')").stdout.strip() == "still here"


def test_memory_hog_does_not_take_the_host_with_it(exec_sandbox):
    with scratch_dir() as d:
        result = run_py(exec_sandbox, d, "x = [0] * 10**10")
        assert not result.ok
        assert "MemoryError" in result.stderr or result.exit_code != 0


@running_as_root
def test_fork_bomb_hits_the_process_ceiling(exec_sandbox):
    code = (
        "import os\n"
        "for _ in range(500):\n"
        "    try:\n"
        "        if os.fork() == 0:\n"
        "            os._exit(0)\n"
        "    except OSError:\n"
        "        print('BLOCKED'); break\n"
    )
    with scratch_dir() as d:
        result = run_py(exec_sandbox, d, code, timeout=10)
        assert "BLOCKED" in result.stdout or result.timed_out


def test_timeout_sets_timed_out_and_the_fsm_can_continue(exec_sandbox):
    with scratch_dir() as d:
        result = run_py(exec_sandbox, d, "import time; time.sleep(60)", timeout=2)
        assert result.timed_out
        assert result.exit_code is None  # killed before it could report one
        assert not result.ok
        assert result.duration_ms >= 1900


def test_grandchild_does_not_outlive_the_kill(exec_sandbox):
    """`proc.kill()` leaves grandchildren running; `os.killpg` is the correct pair for
    `start_new_session=True`. Asserts on process *state*, because a pid whose process has
    become a zombie still answers `os.kill(pid, 0)`."""
    code = (
        "import pathlib, subprocess, time\n"
        "child = subprocess.Popen(['sleep', '90'])\n"
        "pathlib.Path('gc.pid').write_text(str(child.pid))\n"
        "time.sleep(90)\n"
    )
    with scratch_dir() as d:
        result = run_py(exec_sandbox, d, code, timeout=3)
        assert result.timed_out
        pid = int((d / "gc.pid").read_text())
        assert _process_state(pid) in ("gone", "Z"), (
            f"grandchild {pid} is still in state {_process_state(pid)}; killpg did not reach it"
        )


def _process_state(pid: int) -> str:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return "gone"
    return stat.rsplit(")", 1)[1].split()[0]


# -- Environment and filesystem -------------------------------------------------------------


def test_api_key_is_not_reachable(exec_sandbox, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-be-visible")
    with scratch_dir() as d:
        result = run_py(
            exec_sandbox, d, "import os; print(os.environ['ANTHROPIC_API_KEY'])"
        )
        assert not result.ok
        assert "KeyError" in result.stderr
        assert "sk-should-never-be-visible" not in result.stdout + result.stderr


@pytest.mark.parametrize("prefix", DENY_ENV_PREFIXES)
def test_no_credential_shaped_variable_survives(exec_sandbox, monkeypatch, prefix):
    name = f"{prefix}SECRET"
    monkeypatch.setenv(name, "leaked")
    with scratch_dir() as d:
        result = run_py(exec_sandbox, d, f"import os; print(os.environ.get({name!r}, 'ABSENT'))")
        assert result.stdout.strip() == "ABSENT"


def test_home_is_redirected_so_ssh_keys_are_unreachable(exec_sandbox):
    code = (
        "import os\n"
        "print('HOME', os.environ['HOME'])\n"
        "try:\n"
        "    open(os.path.expanduser('~/.ssh/id_rsa'))\n"
        "    print('READ')\n"
        "except FileNotFoundError:\n"
        "    print('DENIED')\n"
    )
    with scratch_dir() as d:
        result = run_py(exec_sandbox, d, code)
        assert "DENIED" in result.stdout
        assert str(d) in result.stdout  # HOME points into the throwaway scratch dir


def test_a_write_to_home_lands_in_the_scratch_dir(exec_sandbox):
    code = (
        "import os, pathlib\n"
        "pathlib.Path(os.path.expanduser('~/.bashrc')).write_text('pwned')\n"
    )
    with scratch_dir() as d:
        assert run_py(exec_sandbox, d, code).ok
        assert (d / ".bashrc").read_text() == "pwned"


def test_the_scratch_directory_is_removed(exec_sandbox):
    with scratch_dir({"t.py": "open('side-effect.txt','w').write('x')"}) as d:
        assert run_py(exec_sandbox, d, "open('side-effect.txt','w').write('x')").ok
        assert (d / "side-effect.txt").is_file()
    assert not d.exists()


def test_scratch_inputs_cannot_escape_via_filename():
    with (
        pytest.raises(Exception, match="escapes the scratch directory"),
        scratch_dir({"../escaped.py": "x = 1"}),
    ):
        pass


@pytest.mark.xfail(
    reason=(
        "Relative-path traversal is NOT confined under --sandbox=subprocess. cwd is the "
        "scratch dir, so open('../evil.txt') writes outside it; rlimits bound the size of a "
        "write, not its location, and preexec_fn cannot create a mount namespace without "
        "privileges. docs/05's test table claims this row holds at Layer 2 -- it does not. "
        "Only --sandbox=docker (--read-only plus a single noexec tmpfs) enforces it."
    ),
    strict=True,
)
def test_relative_path_traversal_is_confined(exec_sandbox):
    with scratch_dir() as d:
        run_py(exec_sandbox, d, "open('../escaped.txt','w').write('x')")
        escaped = d.parent / "escaped.txt"
        try:
            assert not escaped.exists()
        finally:
            escaped.unlink(missing_ok=True)


@pytest.mark.xfail(
    reason=(
        "Network is not blocked under --sandbox=subprocess: preexec_fn cannot create a "
        "network namespace without privileges. This is the main reason to prefer "
        "--sandbox=docker, which passes --network none."
    ),
    strict=False,  # non-strict: it also 'passes' on a host with no outbound route at all
)
def test_network_is_blocked(exec_sandbox):
    code = (
        "import socket\n"
        "socket.create_connection(('1.1.1.1', 80), timeout=4).close()\n"
        "print('CONNECTED')\n"
    )
    with scratch_dir() as d:
        # source_to_gate is not passed here on purpose: this row is about what the *isolation
        # layer* blocks, not about what the AST gate refuses to start.
        result = run_py(exec_sandbox, d, code, timeout=8)
        assert "CONNECTED" not in result.stdout


def test_ast_gate_refuses_to_execute_a_network_import(exec_sandbox):
    """What actually protects the subprocess layer: refusing to start.

    A tripwire, not a boundary -- it is bypassable -- but it catches the realistic cases, and
    the refusal is recorded rather than silent.
    """
    with scratch_dir() as d:
        result = run_py(
            exec_sandbox, d, "print('hi')", source_to_gate="import socket\nsocket.socket()\n"
        )
        assert result.refused_reason == "refused: network module import (socket)"
        assert result.exit_code is None
        assert not result.ok


# -- Output handling ------------------------------------------------------------------------


def test_runaway_output_is_truncated_not_buffered_into_the_parent(exec_sandbox):
    """Output goes to a file bounded by RLIMIT_FSIZE, not through a pipe into our own heap."""
    with scratch_dir() as d:
        result = run_py(
            exec_sandbox,
            d,
            "import sys\nfor _ in range(200000): sys.stdout.write('x' * 100)\n",
            timeout=15,
        )
        assert result.truncated
        assert len(result.stdout) <= exec_sandbox.config.output_capture_bytes + 100


def test_docker_mode_reports_network_blocked():
    assert Sandbox(SandboxConfig(mode="docker")).network_is_blocked is True
    assert Sandbox(SandboxConfig(mode="subprocess")).network_is_blocked is False


def test_docker_argv_carries_every_isolation_flag():
    """The docker argv is the security claim, so assert on it directly rather than needing a
    docker daemon in CI."""
    sandbox = Sandbox(SandboxConfig(mode="docker", allow_exec=True))
    argv = sandbox._docker_argv(["python", "runner.py"], Path("/tmp/scratch"))
    joined = " ".join(argv)
    for flag in (
        "--network none",
        "--read-only",
        "noexec",
        "--user 65534:65534",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--pids-limit",
        "--memory",
        "--cpus",
    ):
        assert flag in joined, f"missing isolation flag: {flag}"
    assert "/tmp/scratch:/work/in:ro" in joined


def test_docker_mode_fails_loudly_without_the_docker_cli(monkeypatch):
    sandbox = Sandbox(SandboxConfig(mode="docker", allow_exec=True))

    def boom(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", boom)
    with scratch_dir() as d, pytest.raises(Exception, match="requires the docker CLI"):
        sandbox.run(["python", "-c", "pass"], d)
