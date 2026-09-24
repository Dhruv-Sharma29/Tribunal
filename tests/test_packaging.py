"""The container images, checked without a docker daemon.

`scripts/verify-docker.sh` is the real proof of criterion S7 — it builds both images and runs
them — but it needs docker, which CI for a library-shaped project may not have and a laptop
may not have running. These tests cover the properties that are *statements in the files
themselves*, so a regression in the packaging fails the ordinary suite rather than waiting for
someone to remember the script.

The properties worth pinning are the ones that are invisible when broken:

* the two images stay separate, because that separation is the credential boundary;
* no credential is reachable from a build;
* the pinned tool versions do not drift away from what an eval number was attributed to.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

# PyYAML is not a declared dependency, but `bandit` is, and it requires PyYAML -- so it is
# always present wherever the grounding layer is.
yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = (ROOT / "Dockerfile").read_text(encoding="utf-8")
SANDBOX = (ROOT / "Dockerfile.sandbox").read_text(encoding="utf-8")
COMPOSE_TEXT = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
COMPOSE = yaml.safe_load(COMPOSE_TEXT)
CONSTRAINTS_TEXT = (ROOT / "constraints.txt").read_text(encoding="utf-8")
IGNORE = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()

#: Anything that looks like a credential, in either a file or an image layer.
CREDENTIAL = re.compile(r"(sk-ant-|nvapi-|sk-proj-|AIza[0-9A-Za-z_-]{10})")


def directives(dockerfile: str, name: str) -> list[str]:
    """Every `NAME ...` line, comments and continuations folded out."""
    joined = dockerfile.replace("\\\n", " ")
    out = []
    for line in joined.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.upper().startswith(name.upper() + " "):
            out.append(stripped[len(name) + 1:].strip())
    return out


def constraints() -> dict[str, str]:
    pins = {}
    for line in CONSTRAINTS_TEXT.splitlines():
        line = line.split("#")[0].strip()
        if "==" in line:
            name, version = line.split("==", 1)
            pins[name.strip().lower()] = version.strip()
    return pins


# -- the two images stay apart -----------------------------------------------------------------


def test_the_sandbox_image_contains_no_crew_code():
    """The separation is the credential boundary, not packaging tidiness: one image may hold
    an API key, the other may execute untrusted code, and neither may be both."""
    assert "COPY src/" not in SANDBOX
    assert "tribunal" not in SANDBOX.replace("tribunal", "")
    # It installs named packages from the index, never the local project.
    for run in directives(SANDBOX, "RUN"):
        for install in re.findall(r"pip install ([^&|;]*)", run):
            targets = [a for a in install.split() if not a.startswith("-")]
            assert "." not in targets, f"the sandbox image installs the project: {install}"


def test_the_sandbox_image_installs_only_an_interpreter_and_pytest():
    installs = " ".join(directives(SANDBOX, "RUN"))
    for forbidden in ("bandit", "ruff", "radon", "anthropic", "openai"):
        assert forbidden not in installs, f"the sandbox image installs {forbidden}"
    assert "pytest" in installs


def test_both_images_run_unprivileged():
    for name, text in (("runtime", RUNTIME), ("sandbox", SANDBOX)):
        users = directives(text, "USER")
        assert users, f"the {name} image never drops privileges"
        assert users[-1].startswith("65534"), f"the {name} image runs as {users[-1]}"


def test_the_runtime_image_defaults_to_the_subprocess_sandbox():
    """Inside a container the tribunal is already namespaced away from the host. Defaulting to
    `--sandbox=docker` would require the host socket, which is a bigger exposure than the one
    it closes."""
    assert "TRIBUNAL_SANDBOX__MODE=subprocess" in RUNTIME


def test_the_documented_env_vars_are_the_ones_settings_reads():
    """A typo here is silent: the container would just use the defaults."""
    from tribunal.config import Settings

    found = re.findall(r"(TRIBUNAL_[A-Z_]+)=(\S+)", RUNTIME)
    assert found, "the runtime image sets no tribunal configuration"
    for key, value in found:
        # Round-trip each one through pydantic-settings rather than trusting the name. A
        # typo would leave the container silently on the defaults.
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv(key, value)
            snapshot = str(Settings().model_dump())
        assert value.strip('"') in snapshot, f"{key} had no effect on Settings"


# -- credentials -------------------------------------------------------------------------------


def test_no_credential_is_reachable_from_a_build():
    for name, text in (
        ("Dockerfile", RUNTIME),
        ("Dockerfile.sandbox", SANDBOX),
        ("docker-compose.yml", COMPOSE_TEXT),
    ):
        assert not CREDENTIAL.search(text), f"{name} contains something credential-shaped"


def test_no_api_key_is_a_build_arg():
    """A build arg lands in the image history, and image history is published with the
    image."""
    for arg in directives(RUNTIME, "ARG"):
        assert "KEY" not in arg.upper() and "TOKEN" not in arg.upper()


def test_compose_passes_credentials_through_without_values():
    """`- ANTHROPIC_API_KEY` takes the host's value; `- ANTHROPIC_API_KEY=...` would commit
    one to the repository."""
    env = COMPOSE["services"]["tribunal"]["environment"]
    keys = [e for e in env if "API_KEY" in e or "TOKEN" in e]
    assert keys, "no credential is passed through at all"
    for entry in keys:
        assert "=" not in entry, f"{entry} carries a value in the compose file"


def test_dockerignore_excludes_env_files():
    """Not in any build today. A later `COPY . .` would put one in a published layer."""
    assert ".env" in IGNORE


# -- version pinning ------------------------------------------------------------------------


def test_every_grounding_tool_is_pinned_exactly():
    """docs/08 § Docker: an unpinned image silently invalidates the eval numbers, because a
    finding that appears from a newer rule set is indistinguishable from one the tribunal earned.
    """
    from tribunal.config import GroundingConfig

    pins = constraints()
    for tool in GroundingConfig().static_tools:
        if tool == "astgate":
            continue  # ours, versioned with the package
        assert tool in pins, f"{tool} is not pinned in constraints.txt"
        assert re.fullmatch(r"\d+(\.\d+)*", pins[tool]), f"{tool} pin is not exact"


def test_every_runtime_dependency_is_pinned():
    """A dependency that drifts is a dependency the image's behaviour is not attributable to."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pins = constraints()
    for spec in project["project"]["dependencies"]:
        name = re.split(r"[<>=!~\[]", spec)[0].strip().lower()
        assert name in pins, f"{name} is in pyproject but not pinned in constraints.txt"


def test_the_pins_satisfy_the_declared_ranges():
    """Catches the drift where constraints.txt is bumped past what pyproject allows, which
    pip resolves by silently ignoring the constraint."""
    from packaging.requirements import Requirement

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pins = constraints()
    for spec in project["project"]["dependencies"]:
        requirement = Requirement(spec)
        pinned = pins[requirement.name.lower()]
        assert requirement.specifier.contains(pinned), (
            f"constraints.txt pins {requirement.name}=={pinned}, which pyproject's "
            f"{requirement.specifier} does not allow"
        )


def test_the_build_installs_with_the_constraints_file():
    """A pin nobody applies is a comment."""
    installs = " ".join(directives(RUNTIME, "RUN"))
    assert "-c constraints.txt" in installs
    assert "COPY pyproject.toml constraints.txt" in RUNTIME


# -- compose ------------------------------------------------------------------------------------


def test_compose_defines_both_images():
    assert set(COMPOSE["services"]) == {"tribunal", "tribunal-sandbox"}


def test_the_code_under_review_is_mounted_read_only():
    """The tribunal proposes diffs; it never writes to the input. `:ro` makes that mechanical."""
    volumes = COMPOSE["services"]["tribunal"]["volumes"]
    code = [v for v in volumes if v.split(":")[1] == "/code"]
    assert code, "no /code mount"
    assert code[0].endswith(":ro")


def test_traces_are_the_only_writable_mount():
    volumes = COMPOSE["services"]["tribunal"]["volumes"]
    writable = [v for v in volumes if not v.endswith(":ro")]
    assert [v.split(":")[1] for v in writable] == ["/traces"]


def test_the_docker_socket_is_not_mounted():
    """docs/08: docker-out-of-docker "grants the container host-root-equivalent access.
    Document that explicitly and make it opt-in." Commented out is opt-in; a live mount here
    would make the dangerous configuration the default one."""
    volumes = COMPOSE["services"]["tribunal"]["volumes"]
    assert not any("docker.sock" in v for v in volumes)
    # ...and the trade-off is explained where someone would go to enable it.
    assert "host-root-equivalent" in COMPOSE_TEXT


def test_the_sandbox_service_does_not_pretend_to_configure_isolation():
    """`--network none` and the rest are applied per-run by `Sandbox._docker_argv`, where the
    sandbox tests can assert them. A compose entry that looked like it set them would be the
    more dangerous kind of documentation."""
    sandbox = COMPOSE["services"]["tribunal-sandbox"]
    assert "network_mode" not in sandbox
    assert "cap_drop" not in sandbox
    assert sandbox["image"] == "tribunal-sandbox:latest"


def test_the_compose_image_name_is_the_one_the_sandbox_looks_for():
    """A rename here means `--sandbox=docker` fails at run time with "image not found"."""
    from tribunal.config import SandboxConfig

    assert COMPOSE["services"]["tribunal-sandbox"]["image"] == SandboxConfig().docker_image


# -- the verification script ----------------------------------------------------------------


def test_the_verification_script_is_executable_and_checks_s7():
    script = ROOT / "scripts" / "verify-docker.sh"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    assert "criterion S7" in text.lower() or "S7" in text


# -- the syntax a build would catch, checked without one -------------------------------------

KNOWN_INSTRUCTIONS = {
    "FROM", "RUN", "COPY", "ADD", "ENV", "ARG", "USER", "WORKDIR", "VOLUME",
    "ENTRYPOINT", "CMD", "LABEL", "EXPOSE", "HEALTHCHECK", "SHELL", "STOPSIGNAL", "ONBUILD",
}


def instruction_lines(text: str):
    """Yield `(lineno, text, continued)` for real instruction lines only.

    Comments are stripped before continuations are resolved — which is exactly the rule that
    makes a comment *inside* a continuation ambiguous, so they are dropped here too.
    """
    continued = False
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            yield number, line, continued  # a comment does not end a continuation
            continue
        yield number, line, continued
        continued = line.endswith("\\")


@pytest.mark.parametrize("name", ["Dockerfile", "Dockerfile.sandbox"])
def test_no_comment_hides_inside_a_line_continuation(name):
    """BuildKit strips them; older builders fold them into the instruction. Either way it is
    a silent difference between build environments, and this project cannot run a build in
    its own test suite to find out which it got."""
    text = (ROOT / name).read_text(encoding="utf-8")
    offenders = [
        (number, line)
        for number, line, continued in instruction_lines(text)
        if continued and line.startswith("#")
    ]
    assert not offenders, f"{name} has a comment inside a continuation: {offenders}"


@pytest.mark.parametrize("name", ["Dockerfile", "Dockerfile.sandbox"])
def test_every_instruction_is_one_docker_understands(name):
    """A typo'd instruction fails the build on someone else's machine, which is the one
    machine criterion S7 is about."""
    text = (ROOT / name).read_text(encoding="utf-8")
    unknown = [
        (number, line.split()[0])
        for number, line, continued in instruction_lines(text)
        if line and not continued and not line.startswith("#")
        and line.split()[0].upper() not in KNOWN_INSTRUCTIONS
    ]
    assert not unknown, f"{name} has unknown instructions: {unknown}"


def test_the_runtime_image_installs_the_project_before_it_is_entered():
    """The dependency layer is installed from metadata alone so it caches across source
    edits; the source install that follows must therefore actually happen, or the image ships
    the stub `__init__.py` the cache trick copies in."""
    runs = directives(RUNTIME, "RUN")
    assert any("--no-deps" in run and "install" in run for run in runs), (
        "the build never reinstalls the project over the dependency-cache stub"
    )
    assert RUNTIME.index("COPY src/ src/") < RUNTIME.index("FROM python:3.11-slim AS runtime")


def test_the_entrypoint_is_a_console_script_the_project_declares():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    entrypoint = directives(RUNTIME, "ENTRYPOINT")[-1]
    name = re.findall(r'"([^"]+)"', entrypoint)[0]
    assert name in project["project"]["scripts"], f"{name} is not a declared console script"
