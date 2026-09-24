from __future__ import annotations

import os
from pathlib import Path

import pytest

from tribunal.config import SandboxConfig, Settings
from tribunal.sandbox import Sandbox

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden"


@pytest.fixture
def exec_sandbox() -> Sandbox:
    """A sandbox with execution enabled and short limits, so tests fail fast."""
    return Sandbox(
        SandboxConfig(allow_exec=True, cpu_seconds=3, wall_timeout_seconds=6)
    )


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def exec_settings() -> Settings:
    return Settings(sandbox=SandboxConfig(allow_exec=True, cpu_seconds=5, wall_timeout_seconds=20))


def fixture_source(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


running_as_root = pytest.mark.skipif(
    os.geteuid() == 0,
    reason=(
        "RLIMIT_NPROC does not apply to root: a process with CAP_SYS_RESOURCE bypasses the "
        "fork limit entirely. The check is real for the normal non-root case, and "
        "--sandbox=docker enforces it with --pids-limit regardless of uid."
    ),
)
