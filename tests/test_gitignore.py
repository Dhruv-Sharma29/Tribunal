"""`.gitignore`, checked against a real git index.

One rule in here is load-bearing and easy to get wrong in a way nothing reports.

A bare `traces/` matches a directory of that name at **any depth**, so it silently ignores
`eval/results/<ts>/traces/` as well as the scratch directory at the repo root. Those inner
traces are not scratch output: `tribunal eval --replay` re-scores a sweep *from its traces*,
RUNBOOK step 3 says to commit the results directory, and the nightly's regression gate reads
a promoted one. Get it wrong and you produce a repository where `--replay` reports "no traces
to re-score" against a results directory that is visibly present -- with nothing anywhere
saying git dropped the files.

That is this project's signature failure re-cast as a config line: a rule that is correct for
one consumer and silently wrong for another (docs/13 §§ 65, 72). So it is asserted against
`git check-ignore` in a throwaway repository rather than reasoned about from the pattern
syntax, which is exactly the kind of reasoning that produced the bug.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GITIGNORE = ROOT / ".gitignore"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git on PATH")

#: Generated, reproducible, or secret. None of it belongs in a push.
MUST_IGNORE = [
    "traces/01ABC.jsonl",          # a local run
    "traces/01ABC.html",           # what `tribunal view` writes beside it
    ".venv/bin/python",
    "__pycache__/x.pyc",
    "src/tribunal/__pycache__/cli.cpython-312.pyc",
    ".pytest_cache/CACHEDIR.TAG",
    ".ruff_cache/content",
    ".env",
    ".env.local",
    "tribunal.egg-info/PKG-INFO",
]

#: Evidence. Every one of these is read by something, and a repository missing any of them
#: is broken in a way that only shows up later.
MUST_TRACK = [
    # `--replay` reads these. The whole reason the ignore rule is anchored.
    "eval/results/20260924T031500Z/traces/01ABC.jsonl",
    "eval/results/20260924T031500Z/raw.jsonl",
    "eval/results/20260924T031500Z/summary.md",
    "eval/results/20260924T031500Z/report.html",
    # nightly.yml's regression gate.
    "eval/results/baseline/raw.jsonl",
    # heldout.yml refuses to start without this.
    "eval/judge-kappa.json",
    "eval/judge-labels.jsonl",
    # What lets the suite run with no credential at all.
    "tests/cassettes/nim/abc123.json",
    # The benchmark, the fixtures, the source.
    "eval/cases/001-shell-injection-report/before.py",
    "tests/fixtures/traces/accept.jsonl",
    "src/tribunal/cli.py",
    "examples/nim.toml",
    "constraints.txt",
]


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    """A throwaway repo holding only this project's `.gitignore` and empty decoy files."""
    path = tmp_path_factory.mktemp("gitignore")
    subprocess.run(["git", "init", "-q", "."], cwd=path, check=True)
    shutil.copy(GITIGNORE, path / ".gitignore")
    for relative in MUST_IGNORE + MUST_TRACK:
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    return path


def ignored(repo: Path, relative: str) -> bool:
    return subprocess.run(
        ["git", "check-ignore", "-q", relative], cwd=repo
    ).returncode == 0


@pytest.mark.parametrize("relative", MUST_IGNORE)
def test_generated_and_secret_files_are_ignored(repo, relative):
    assert ignored(repo, relative), f"{relative} would be committed"


@pytest.mark.parametrize("relative", MUST_TRACK)
def test_evidence_is_not_ignored(repo, relative):
    assert not ignored(repo, relative), (
        f"{relative} would be silently dropped from the repository"
    )


def test_the_traces_rule_is_anchored_to_the_repository_root():
    """The specific mistake, asserted on the pattern as well as the behaviour.

    The behavioural tests above would also pass if someone replaced the rule with something
    accidentally equivalent; this one says what the file is required to contain, so the next
    person to edit it reads the reason rather than rediscovering it.
    """
    body = GITIGNORE.read_text(encoding="utf-8")
    assert "\n/traces/" in body, "the traces rule must be anchored with a leading slash"
    assert "\ntraces/" not in body, (
        "a bare `traces/` also ignores eval/results/<ts>/traces/ -- see this module's docstring"
    )


def test_nothing_credential_shaped_is_committable(repo):
    """`.env` is the one file that reliably holds a real key. The config file deliberately
    does not: `providers.*.key_env` names an environment variable rather than carrying a
    secret, which is why `examples/nim.toml` is tracked."""
    assert ignored(repo, ".env")
    assert not ignored(repo, "examples/nim.toml")
    assert "key_env" in (ROOT / "examples" / "nim.toml").read_text(encoding="utf-8")
