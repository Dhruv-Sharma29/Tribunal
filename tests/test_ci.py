"""The CI workflows, and the one property of them that cannot be checked after the fact.

docs/07 § CI integration: "The API key lives in GitHub Actions secrets and is available only
to the nightly workflow, never to PR workflows from forks."

That is a single-word failure. `pull_request` runs a fork's code *without* the repository's
secrets; `pull_request_target` runs it *with* them, in the base repo's context. Swapping one
for the other looks like a triggering fix and hands the key to anyone who can open a pull
request — and nothing about the resulting workflow run says so. So it is asserted here.

The rest is drift: a workflow that stops running the exit-code script, a gate that quietly
becomes advisory, a sweep with no timeout.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
CI = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
NIGHTLY = yaml.safe_load((WORKFLOWS / "nightly.yml").read_text(encoding="utf-8"))
HELDOUT = yaml.safe_load((WORKFLOWS / "heldout.yml").read_text(encoding="utf-8"))
ALL = {"ci.yml": CI, "nightly.yml": NIGHTLY, "heldout.yml": HELDOUT}


def triggers(workflow: dict) -> dict:
    """PyYAML parses the bare key `on:` as the boolean True, which is a genuinely confusing
    hour to lose."""
    return workflow.get("on") or workflow.get(True) or {}


def steps(workflow: dict) -> list[dict]:
    return [step for job in workflow["jobs"].values() for step in job["steps"]]


def text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


# -- the credential boundary ---------------------------------------------------------------


def test_the_pr_gate_references_no_secret():
    """If it cannot name a secret, it cannot leak one."""
    assert "secrets." not in text("ci.yml")


def test_no_workflow_is_triggered_by_pull_request_target():
    """`pull_request_target` runs fork code with the base repository's secrets. There is no
    use for it here, and it is one word away from `pull_request`.

    Asserted against the parsed triggers rather than the file text, because the text
    mentions it — in the comment explaining why it is not used, which is exactly where you
    would want it mentioned.
    """
    for name, workflow in ALL.items():
        assert "pull_request_target" not in triggers(workflow), name


def test_the_pr_gate_runs_on_pull_request():
    assert "pull_request" in triggers(CI)


def test_only_the_credentialed_workflows_are_unreachable_from_a_fork():
    """`schedule` only ever fires on the default branch of this repository, and
    `workflow_dispatch` needs write access. Neither can be caused by a pull request."""
    for name, workflow in (("nightly.yml", NIGHTLY), ("heldout.yml", HELDOUT)):
        assert "secrets." in text(name), f"{name} should be the one holding the key"
        fired_by = set(triggers(workflow))
        assert fired_by <= {"schedule", "workflow_dispatch"}, f"{name}: {fired_by}"


def test_every_workflow_declares_least_privilege():
    for name, workflow in ALL.items():
        assert "permissions" in workflow, f"{name} does not declare permissions"
        assert workflow["permissions"].get("contents") == "read", name


# -- the PR gate does what docs/07 asks -----------------------------------------------------


def test_the_pr_gate_runs_lint_tests_and_the_exit_code_contract():
    commands = " ".join(step.get("run", "") for step in steps(CI))
    assert "ruff check" in commands
    assert "python -m pytest -q" in commands
    assert "./scripts/check-exit-codes.sh" in commands


def test_the_pr_smoke_gate_runs_integration_and_committed_recordings():
    """The full eval recordings do not exist yet; CI must exercise the available evidence."""
    commands = " ".join(step.get("run", "") for step in CI["jobs"]["smoke"]["steps"])
    assert "python -m pytest" in commands
    assert "tests/test_eval_runner.py" in commands
    assert "tests/test_coder.py" in commands
    assert "tests/test_llm_live.py" in commands
    assert '-m "not live"' in commands
    assert "[dev,all-providers]" in commands


def test_the_pr_gate_validates_every_benchmark_case():
    """`--dry-run` runs the real grounding suite over all 24: a `rule` locator naming a
    code the tools no longer emit fails here rather than becoming a silent miss in a
    results table months later."""
    commands = " ".join(step.get("run", "") for step in steps(CI))
    assert "eval --dry-run" in commands


def test_no_pr_job_is_advisory():
    """`continue-on-error` turns a gate into a notification. If a check is not worth
    failing on, it is not worth running on every pull request."""
    for job in CI["jobs"].values():
        assert not job.get("continue-on-error")
        for step in job["steps"]:
            assert not step.get("continue-on-error")


def test_ci_installs_against_the_pinned_constraints():
    """Otherwise a new `bandit` release turns a green suite red on a commit that changed
    nothing, and the golden files get "fixed" to match a tool nobody chose."""
    for step in steps(CI):
        if "pip install" in step.get("run", ""):
            assert "-c constraints.txt" in step["run"], step["run"]


def test_ci_tests_the_python_floor_that_pyproject_declares():
    import tomllib

    project = tomllib.loads(
        (WORKFLOWS.parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    floor = project["project"]["requires-python"].lstrip(">=")
    versions = CI["jobs"]["test"]["strategy"]["matrix"]["python"]
    assert floor in versions, f"CI never runs the declared floor {floor}"


# -- the nightly ------------------------------------------------------------------------------


def test_the_nightly_runs_doctor_before_spending_money():
    """A sweep on a box with no `bandit` silently degrades every critic from grounded to
    opinion and produces numbers that look real."""
    commands = [step.get("run", "") for step in steps(NIGHTLY)]
    doctor = next(i for i, c in enumerate(commands) if "tribunal doctor" in c)
    sweep = next(i for i, c in enumerate(commands) if "eval --split dev" in c)
    assert doctor < sweep


def test_the_nightly_checks_only_m4_for_regressions():
    """M1 and M2 depend on the judge; until its kappa is published a move in either is not
    attributable to the tribunal. Gating on an unvalidated instrument fails for reasons nobody
    can act on."""
    assert "check-regression.py" in text("nightly.yml")
    assert "M4" in text("nightly.yml")


def test_the_nightly_rejects_a_missing_credential_before_the_sweep(tmp_path):
    nightly_steps = steps(NIGHTLY)
    guard = next(s for s in nightly_steps if s.get("name") == "check provider credential")
    sweep = next(s for s in nightly_steps if s.get("name") == "sweep")
    assert nightly_steps.index(guard) < nightly_steps.index(sweep)
    summary = tmp_path / "step-summary.md"
    result = subprocess.run(
        ["bash", "-e", "-c", guard["run"]],
        cwd=tmp_path,
        env={**os.environ, "ANTHROPIC_API_KEY": "", "GITHUB_STEP_SUMMARY": str(summary)},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "::error::" in result.stdout
    assert "New repository secret" in result.stdout
    assert "ANTHROPIC_API_KEY" in summary.read_text()
    assert not (tmp_path / "eval" / "results").exists()


@pytest.mark.parametrize("layout", ["absent", "baseline", "partial", "complete"])
def test_the_nightly_publishes_only_a_completed_sweep_summary(tmp_path, layout):
    """The always() step must also work after installation or the sweep fails."""
    if layout != "absent":
        baseline = tmp_path / "eval" / "results" / "baseline"
        baseline.mkdir(parents=True)
        (baseline / "summary.md").write_text("old baseline")
    if layout in {"partial", "complete"}:
        partial = tmp_path / "eval" / "results" / "20261003T000000Z"
        partial.mkdir()
    if layout == "complete":
        for timestamp in ("20261001T000000Z", "20261002T000000Z"):
            directory = tmp_path / "eval" / "results" / timestamp
            directory.mkdir()
            (directory / "summary.md").write_text(timestamp)
    summary = tmp_path / "step-summary.md"
    summary.write_text("existing diagnostics\n")
    publish = next(s for s in steps(NIGHTLY) if s.get("name") == "publish the run summary")
    # Run the embedded Python with the same interpreter as the tests.
    script = publish["run"].split("\n", 1)[1].removesuffix("PY\n")
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_STEP_SUMMARY": str(summary)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    expected = "existing diagnostics\n"
    if layout == "complete":
        expected += "20261002T000000Z"
    assert summary.read_text() == expected
    assert NIGHTLY["permissions"] == {"contents": "read"}


def test_long_running_workflows_have_a_timeout():
    """A sweep that hangs must not burn a six-hour runner."""
    for name, workflow in (("nightly.yml", NIGHTLY), ("heldout.yml", HELDOUT)):
        for job in workflow["jobs"].values():
            assert job.get("timeout-minutes"), name


def test_results_are_uploaded_even_when_the_sweep_fails():
    """A failed sweep is still evidence, and it is the run you most want to look at."""
    for workflow in (NIGHTLY, HELDOUT):
        upload = next(
            s for s in steps(workflow) if "upload-artifact" in str(s.get("uses", ""))
        )
        assert upload.get("if") == "always()"


# -- the held-out sweep -----------------------------------------------------------------------


def test_the_heldout_sweep_is_manual_only():
    """docs/07: "run held-out once per milestone, and report every held-out run you did —
    not the best one." A scheduled held-out sweep accumulates attempts nobody reports,
    which is the failure the split exists to prevent."""
    assert set(triggers(HELDOUT)) == {"workflow_dispatch"}


def test_the_heldout_sweep_records_why_it_was_run():
    assert "reason" in triggers(HELDOUT)["workflow_dispatch"]["inputs"]
    assert triggers(HELDOUT)["workflow_dispatch"]["inputs"]["reason"]["required"] is True


def test_the_heldout_sweep_refuses_an_unvalidated_judge():
    """docs/07: "If κ < 0.6, fix the judge rubric before running anything on held-out." The
    held-out split can be spent once per milestone; spending it on numbers whose judge
    nobody has measured wastes the one thing that cannot be re-run."""
    body = text("heldout.yml")
    assert "judge-kappa.json" in body
    assert "0.6" in body
    commands = [s.get("run", "") for s in steps(HELDOUT)]
    guard = next(i for i, c in enumerate(commands) if "judge-kappa.json" in c)
    sweep = next(i for i, c in enumerate(commands) if "eval --split heldout" in c)
    assert guard < sweep


def test_the_heldout_sweep_defaults_to_the_cut_line_arms():
    """docs/09 § The cut line: "keep only B1 vs B3. B1 is the comparison that matters"."""
    assert triggers(HELDOUT)["workflow_dispatch"]["inputs"]["arms"]["default"] == "B1,B3"
