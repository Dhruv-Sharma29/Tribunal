"""Execution isolation for model-written and user-supplied code.

The system takes code from an untrusted source, asks an LLM to modify it, and then wants to
execute the result. That is a remote-code-execution pipeline with a friendly CLI unless the
boundary is real (docs/05-execution-sandbox.md).

Three layers:

* **Layer 1 -- never execute by default.** `SandboxConfig.allow_exec` is False; every entry
  point raises `ExecutionNotPermitted` until `--allow-exec` is passed. Static grounding
  (bandit, ruff, radon, astgate) never executes the target, so the default path is safe and
  the Profiler degrades to `unmeasurable`, which the schema already supports.
* **Layer 2 -- `mode="subprocess"`.** rlimits, a scrubbed environment, a throwaway scratch
  directory with `HOME`/`TMPDIR` redirected into it, and process-*group* kill on timeout.
  **Network is not blocked at this layer** and that limitation is surfaced, not hidden.
* **Layer 3 -- `mode="docker"`.** `--network none --read-only` tmpfs `noexec`, unprivileged
  uid, all capabilities dropped, pid/memory/cpu caps. The sandbox image contains no tribunal code
  and no API key, so the key never exists in a namespace that runs model-written code.

Two details the reference sketch in docs/05 gets wrong, corrected here:

1. It calls `os.setsid()` inside `preexec_fn` *and* passes `start_new_session=True`. The child
   is already a session leader by then, so the second `setsid()` fails and the child dies
   before `exec` with `SubprocessError: Exception occurred in preexec_fn`. Verified. Only
   `start_new_session=True` is used below; `preexec_fn` sets rlimits only.
2. It captures stdout/stderr through pipes, so a runaway `print` loop is buffered in the
   *parent's* memory -- outside every limit we just set. Output is redirected to files in the
   scratch directory instead, where `RLIMIT_FSIZE` bounds it.
"""

from __future__ import annotations

import contextlib
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from tribunal import astgate
from tribunal.config import SandboxConfig
from tribunal.contracts import SandboxResult


class SandboxError(RuntimeError):
    """Base class for sandbox refusals."""


class ExecutionNotPermitted(SandboxError):
    """Layer 1 tripped: execution was requested without `--allow-exec`."""


class ExecutionRefused(SandboxError):
    """The static gate refused this source (denied module import)."""


#: Environment variables that may cross into the sandbox. Allowlist, not denylist -- a new
#: credential-bearing variable in the parent environment must not silently be inherited.
ALLOW_ENV = frozenset({"LANG", "LC_ALL", "LC_CTYPE", "TZ"})

#: Backstop only. If one of these ever survives the allowlist above, `_build_env` raises
#: rather than running, because a leak here is the failure the threat model is about.
DENY_ENV_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "GITHUB_",
    "OPENAI_",
    "SSH_",
    "GOOGLE_",
    "GCP_",
    # Added when the multi-provider LLM layer landed. The allowlist above already meant
    # these never reached a sandboxed process, but a backstop that does not cover every
    # provider the tribunal can authenticate to is a backstop with a hole in it -- and the
    # parametrised test walks exactly this tuple.
    "NVIDIA_",
    "NIM_",
    "GEMINI_",
)
DENY_ENV_EXACT = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "NVIDIA_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    }
)


def _is_denied(name: str) -> bool:
    return name in DENY_ENV_EXACT or name.startswith(DENY_ENV_PREFIXES)


def _build_env(workdir: Path) -> dict[str, str]:
    """A minimal environment. `HOME` and `TMPDIR` point into the scratch directory so that a
    test writing to `~/.bashrc` lands in a directory we delete."""
    env = {k: v for k, v in os.environ.items() if k in ALLOW_ENV and not _is_denied(k)}
    env |= {
        "HOME": str(workdir),
        "TMPDIR": str(workdir),
        "TMP": str(workdir),
        "TEMP": str(workdir),
        "PYTHONPATH": str(workdir),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",  # deterministic, so repeated runs are comparable
        "PATH": "/usr/bin:/bin",
        "NO_COLOR": "1",
    }
    leaked = sorted(k for k in env if _is_denied(k))
    if leaked:  # pragma: no cover -- defensive; the allowlist makes this unreachable
        raise SandboxError(f"environment scrub failed, would have leaked: {leaked}")
    return env


@contextlib.contextmanager
def scratch_dir(inputs: dict[str, str] | None = None) -> Iterator[Path]:
    """A fresh directory per run, populated with copies only, removed afterwards.

    Never execute in the user's working directory: a test that writes files must not be able
    to touch their repo.
    """
    path = Path(tempfile.mkdtemp(prefix="tribunal-sbx-"))
    try:
        for name, content in (inputs or {}).items():
            target = path / name
            if not target.resolve().is_relative_to(path.resolve()):
                raise SandboxError(f"input filename escapes the scratch directory: {name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@dataclass(frozen=True)
class Sandbox:
    config: SandboxConfig

    # -- public entry points ------------------------------------------------------------

    def run(
        self,
        argv: Sequence[str],
        workdir: Path,
        timeout: int | None = None,
        source_to_gate: str | None = None,
    ) -> SandboxResult:
        """Execute `argv` under the configured isolation layer.

        `source_to_gate`, when given, is scanned by `astgate` first and execution is refused
        outright if it imports a denied module. The refusal is returned as a `SandboxResult`
        with `refused_reason` set rather than raised, so it lands in the trace as an outcome.
        """
        if not self.config.allow_exec:
            raise ExecutionNotPermitted(
                "execution requires --allow-exec; static grounding does not execute the target"
            )
        if source_to_gate is not None:
            reason = astgate.execution_refusal(source_to_gate)
            if reason is not None:
                return SandboxResult(
                    argv=list(argv),
                    exit_code=None,
                    stdout="",
                    stderr="",
                    duration_ms=0,
                    timed_out=False,
                    truncated=False,
                    refused_reason=reason,
                )
        limit = timeout if timeout is not None else self.config.wall_timeout_seconds
        if self.config.mode == "docker":
            return self._run_docker(argv, workdir, limit)
        return self._run_subprocess(argv, workdir, limit)

    def python_for_sandbox(self) -> str:
        """The interpreter to invoke inside the sandbox.

        Subprocess mode reuses the tribunal's own interpreter, because that is where `pytest` and
        `pytest-timeout` are installed. Docker mode must use the image's `python`: the tribunal
        venv does not exist in that namespace, and pointing at a host path would be the one
        mistake that reintroduces host filesystem coupling into the isolated layer.
        """
        return sys.executable if self.config.mode == "subprocess" else "python"

    @property
    def network_is_blocked(self) -> bool:
        """Honest capability report, used by `doctor` and by the CLI's help text."""
        return self.config.mode == "docker"

    # -- Layer 2 ------------------------------------------------------------------------

    def _limits(self) -> None:
        """Runs in the child, post-fork, pre-exec.

        `preexec_fn` is unsafe in a multi-threaded parent. The orchestrator is asyncio-based
        and single-threaded, so this is fine -- but moving the orchestrator onto a thread pool
        would break it subtly, which is why it is written down here.
        """
        cfg = self.config
        resource.setrlimit(resource.RLIMIT_CPU, (cfg.cpu_seconds, cfg.cpu_seconds))
        # RLIMIT_AS, not RLIMIT_DATA: the latter misses mmap.
        resource.setrlimit(resource.RLIMIT_AS, (cfg.address_space_bytes,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (cfg.max_file_bytes,) * 2)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        with contextlib.suppress(ValueError, OSError):
            # RLIMIT_NPROC is per-UID, not per-process, and root bypasses it entirely. Best
            # effort: it is a fork-bomb ceiling for the common non-root case, and the docker
            # layer's --pids-limit is the version that actually holds.
            resource.setrlimit(resource.RLIMIT_NPROC, (cfg.max_processes,) * 2)

    def _run_subprocess(self, argv: Sequence[str], workdir: Path, timeout: int) -> SandboxResult:
        out_path = workdir / ".sbx-stdout"
        err_path = workdir / ".sbx-stderr"
        started = time.monotonic()
        timed_out = False
        with out_path.open("wb") as out_f, err_path.open("wb") as err_f:
            proc = subprocess.Popen(  # noqa: S603 - argv is ours; the code it runs is not
                list(argv),
                cwd=str(workdir),
                env=_build_env(workdir),
                preexec_fn=self._limits,  # noqa: PLC2801
                stdin=subprocess.DEVNULL,
                stdout=out_f,
                stderr=err_f,
                start_new_session=True,  # child becomes its own process-group leader
                close_fds=True,
            )
            pgid = self._pgid_of(proc)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill_group(proc, pgid)
        exit_code = None if timed_out else proc.returncode
        stdout, out_trunc = self._read_capped(out_path)
        stderr, err_trunc = self._read_capped(err_path)
        return SandboxResult(
            argv=list(argv),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=timed_out,
            truncated=out_trunc or err_trunc,
            refused_reason=None,
        )

    @staticmethod
    def _pgid_of(proc: subprocess.Popen[bytes]) -> int:
        """Capture the group id while the child is certainly alive.

        Reading it after the process is reaped raises `ProcessLookupError`, and falling back to
        `proc.pid` would then target a recycled pid.
        """
        try:
            return os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError):
            return proc.pid

    @staticmethod
    def _kill_group(proc: subprocess.Popen[bytes], pgid: int) -> None:
        """Kill the GROUP, not just the child.

        `proc.kill()` leaves grandchildren running -- verified: a `sleep` spawned by the child
        survives `proc.kill()` and is reaped by `killpg`. `os.setsid` (via
        `start_new_session`) plus `os.killpg` is the correct pair.
        """
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        if proc.returncode is None:  # pragma: no cover -- SIGKILL is not catchable
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            proc.wait()

    def _read_capped(self, path: Path) -> tuple[str, bool]:
        cap = self.config.output_capture_bytes
        try:
            with path.open("rb") as fh:
                data = fh.read(cap + 1)
        except FileNotFoundError:
            return "", False
        truncated = len(data) > cap
        text = data[:cap].decode("utf-8", errors="replace")
        if truncated:
            text += f"\n... [truncated at {cap} bytes]"
        return text, truncated

    # -- Layer 3 ------------------------------------------------------------------------

    def _docker_argv(self, argv: Sequence[str], workdir: Path) -> list[str]:
        cfg = self.config
        return [
            "docker",
            "run",
            "--rm",
            # The flag that actually matters: no exfiltration, and no "download stage 2".
            "--network",
            "none",
            "--read-only",
            # noexec blocks the write-then-execute pattern; a single writable tmpfs means
            # nothing survives the run.
            "--tmpfs",
            "/work:rw,size=64m,noexec",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(cfg.docker_pids_limit),
            "--memory",
            cfg.docker_memory,
            "--cpus",
            cfg.docker_cpus,
            "-v",
            f"{workdir}:/work/in:ro",
            "-w",
            "/work",
            "--entrypoint",
            "",
            cfg.docker_image,
            *argv,
        ]

    def _run_docker(self, argv: Sequence[str], workdir: Path, timeout: int) -> SandboxResult:
        full = self._docker_argv(argv, workdir)
        started = time.monotonic()
        try:
            completed = subprocess.run(  # noqa: S603 - argv is ours
                full,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=_build_env(workdir),
            )
        except subprocess.TimeoutExpired as exc:
            # `docker run --rm` tears the container down when the client is killed, so there is
            # no process group to chase here.
            return SandboxResult(
                argv=full,
                exit_code=None,
                stdout=_decode(exc.stdout),
                stderr=_decode(exc.stderr),
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=True,
                truncated=False,
                refused_reason=None,
            )
        except FileNotFoundError as exc:
            raise SandboxError(
                "mode='docker' requires the docker CLI on PATH; "
                "use --sandbox=subprocess (weaker: no network isolation)"
            ) from exc
        cap = self.config.output_capture_bytes
        stdout, out_trunc = _cap_text(completed.stdout, cap)
        stderr, err_trunc = _cap_text(completed.stderr, cap)
        return SandboxResult(
            argv=full,
            exit_code=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=False,
            truncated=out_trunc or err_trunc,
            refused_reason=None,
        )


def _decode(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else value.decode("utf-8", errors="replace")


def _cap_text(value: str | None, cap: int) -> tuple[str, bool]:
    text = value or ""
    if len(text.encode("utf-8", errors="replace")) <= cap:
        return text, False
    return text[:cap] + f"\n... [truncated at {cap} bytes]", True
