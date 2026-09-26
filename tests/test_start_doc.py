"""`start.md` is checked against the code it describes.

A getting-started guide is the documentation most likely to be read and least likely to be
re-read by its author. It goes stale in a specific, quiet way: a flag is renamed, a command
grows an argument, an exit code shifts, and the file keeps saying what used to be true. The
README in this repo carried a stale test count and a stale phase list for most of the
project, which is the evidence that nobody notices on their own.

So every checkable claim in it is checked here: the commands exist, the flags exist on the
commands they are shown under, the exit-code table matches `exit_code_for`, the arm table
matches `eval/arms.py`, and the links resolve. Prose is not checked and cannot be — but the
parts that rot fastest are all mechanical, and those are the parts below.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tribunal.cli import EXIT_FOR_OUTCOME, ExitCode, app
from tribunal.eval.arms import ARM_IDS
from tribunal.policy import DECISION_TABLE

ROOT = Path(__file__).resolve().parent.parent
START = ROOT / "start.md"
TEXT = START.read_text(encoding="utf-8")


def commands() -> dict[str, object]:
    """Every command the CLI exposes, as Click sees it.

    Via `typer.main.get_command` rather than `app.registered_commands`, because the Click
    layer is the surface a user actually types at -- and because this module compiles with
    `from __future__ import annotations`, which turns every `Annotated[...]` into a string
    and makes signature introspection quietly return nothing. It did: the first version of
    this helper found no options at all and every flag assertion passed vacuously.
    """
    import typer.main

    return dict(typer.main.get_command(app).commands)


def options_of(name: str) -> set[str]:
    """Every long option string the command accepts, including `--no-` counterparts."""
    found = {"--help"}
    for param in commands()[name].params:
        found.update(opt for opt in param.opts if opt.startswith("--"))
        found.update(opt for opt in param.secondary_opts if opt.startswith("--"))
    return found


# -- the commands and flags it tells people to type ---------------------------------------


def test_the_readme_does_not_overstate_the_test_count():
    """Stated as a floor, deliberately.

    The README carried an exact "1393 tests" for most of the project and was wrong within a
    day of being written, because an exact count means every commit that adds a test must
    also edit the README, and none of them did. A floor stays true as tests are added and
    fails loudly if the suite ever shrinks past it -- which is the only direction worth
    being told about.
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    claimed = int(re.search(r"\*\*([\d,]+)\+ tests", readme).group(1).replace(",", ""))

    # Collected for real rather than hard-coded here, because a hard-coded number in the
    # test that guards a hard-coded number in the README is the same bug one level down.
    actual = _collected()
    assert claimed <= actual, f"README claims {claimed}+ tests; only {actual} collected"
    assert claimed >= actual - 400, (
        f"README claims {claimed}+ but there are {actual}: the floor has drifted far enough "
        "below the truth to be worth raising"
    )


def test_start_md_exists_and_is_linked_from_the_readme():
    """A start guide nobody is pointed at is a file in a directory."""
    assert START.is_file()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "start.md" in readme, "README should point at start.md"


#: Only *invocations*, not prose. The old pattern was a bare `<name> ([a-z-]+)`, which was
#: safe only while the project was called `debug-crew` and so could never appear in a
#: sentence. Renaming to `tribunal` made the name an ordinary English word, and the loose
#: pattern immediately started reporting "the tribunal cannot" and "the tribunal proposes"
#: as missing subcommands. A match now has to sit at the start of a line, after a shell
#: prompt, or inside backticks.
INVOCATION = re.compile(r"(?:^|\$ |`)tribunal ([a-z-]+)", re.M)


@pytest.mark.parametrize("command", sorted(
    set(INVOCATION.findall(TEXT)) - {"mcp", "sandbox"}
))
def test_every_command_it_mentions_exists(command):
    assert command in commands(), f"start.md tells the reader to run `tribunal {command}`"


def test_every_flag_it_documents_for_run_exists():
    """The flag block under § 3 is the one most likely to rot: `run` has grown four options
    over the project and each was documented by hand."""
    documented = set(re.findall(r"^(--[a-z][a-z-]*)", _section("## 3. Run the loop"), re.M))
    missing = documented - options_of("run")
    assert not missing, f"start.md documents {missing} on `run`, which does not accept them"


@pytest.mark.parametrize("command", ["eval", "replay", "view", "ground", "code"])
def test_flags_shown_for_other_commands_exist(command):
    """Flags appear inline for these, so they are pulled out of the code blocks that name
    the command rather than out of a dedicated block."""
    shown = set()
    for line in TEXT.splitlines():
        if INVOCATION.search(line) and f"tribunal {command} " in line:
            shown.update(re.findall(r"(--[a-z-]+)", line))
    missing = shown - options_of(command)
    assert not missing, f"start.md shows {missing} on `{command}`"


# -- the tables ------------------------------------------------------------------------------


def test_the_exit_code_table_matches_the_cli():
    """The code block in § 3 is the shell contract. A wrong row here sends someone's CI
    down the wrong branch, which is worse than no table."""
    body = _section("**The exit code")
    documented: dict[int, str] = {}
    for code, meaning in re.findall(r"^\| (\d+) \| (.+?) \|", body, re.M):
        documented[int(code)] = meaning

    # Every outcome must appear against the code it actually produces. `reject` and
    # `escalate` share code 2, and the first version of this table listed only `escalate` --
    # so a rejected final patch exited with a code the guide described as something else.
    for outcome, code in EXIT_FOR_OUTCOME.items():
        assert outcome in documented[int(code)], f"{outcome} exits {int(code)}"
    assert "budget" in documented[int(ExitCode.BUDGET_EXHAUSTED)].lower()
    assert "reviewable" in documented[int(ExitCode.FAILED)]
    assert "usage" in documented[int(ExitCode.USAGE)].lower()
    assert set(documented) == {int(code) for code in ExitCode}


def test_the_arm_table_matches_the_eval_harness():
    documented = set(re.findall(r"^\| `(B\d)` \|", _section("## 9. The benchmark"), re.M))
    assert documented == set(ARM_IDS)


def test_the_claimed_size_of_the_decision_table_is_right():
    """start.md says "twelve-row table" in prose. Spelled out, so this is the only place it
    can be checked -- and a thirteenth rule is exactly the kind of change that would not
    prompt anyone to reread a guide."""
    words = {12: "twelve", 11: "eleven", 13: "thirteen", 14: "fourteen"}
    assert f"{words[len(DECISION_TABLE)]}-row table" in TEXT, (
        f"the table has {len(DECISION_TABLE)} rows; start.md says otherwise"
    )


def test_the_case_count_matches_the_cases_on_disk():
    count = len([p for p in (ROOT / "eval" / "cases").iterdir() if p.is_dir()])
    assert f"{count} hand-written cases" in TEXT


def test_the_irreconcilable_rule_is_where_the_guide_says_it_is():
    """§ 5 tells the reader they can go and read row 8 to check a `tradeoff` verdict. If the
    table is reordered, that instruction sends them to the wrong row -- and the whole point
    of the paragraph is that the claim is checkable."""
    assert "row 8" in TEXT
    assert DECISION_TABLE[7].name == "irreconcilable"


# -- links and the MCP surface ------------------------------------------------------------------


@pytest.mark.parametrize("target", sorted(set(
    t for t in re.findall(r"\]\(([^)#]+)\)", TEXT) if not t.startswith("http")
)))
def test_every_relative_link_resolves(target):
    assert (ROOT / target).exists(), target


def test_the_mcp_tool_signature_matches_the_server():
    """§ 7 prints `review_code`'s signature. Typing it out by hand is how a guide ends up
    describing a parameter that was renamed a month earlier."""
    pytest.importorskip("mcp")
    from tribunal.mcp_server import TOOL_NAMES, build_server

    documented = re.search(r"`review_code\(([^)]*)\)`", TEXT)
    assert documented, "start.md should show review_code's signature"
    named = {p.split("=")[0].strip() for p in documented.group(1).split(",")}

    schema = next(
        t for t in _tools(build_server()) if t.name == "review_code"
    ).input_schema
    assert named == set(schema["properties"])
    for tool_name in TOOL_NAMES:
        assert f"`{tool_name}(" in TEXT


def _tools(server):
    import asyncio

    return asyncio.run(server.list_tools())


def _section(heading: str) -> str:
    """The text from `heading` to the next `## ` heading."""
    start = TEXT.index(heading)
    rest = TEXT[start + len(heading):]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def test_the_case_fixtures_are_excluded_from_lint_without_disarming_grounding():
    """`eval/cases` is excluded from the project's ruff config, because the cases contain
    planted defects and `ruff check . --fix` would delete the thing each case tests.

    The check that matters is the second one: the grounding suite shells out to ruff with
    `--isolated`, so the exclude cannot remove a finding from a case. If that flag were ever
    dropped, every case would silently lose its ruff findings and the benchmark would keep
    reporting scores -- lower ones, for no visible reason.
    """
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ruff = config["tool"]["ruff"]
    assert "eval/cases" in ruff.get("extend-exclude", [])
    # `exclude` replaces ruff's defaults (`.venv` among them); `extend-exclude` adds to them.
    assert "exclude" not in ruff

    sample = ROOT / "eval" / "cases" / "003-validated-query-in-hot-loop" / "before.py"
    assert "--isolated" in _ruff_argv(sample), (
        "grounding must ignore the project's ruff config, or the exclude above would "
        "silently empty every benchmark case's ruff findings"
    )


def _ruff_argv(path: Path) -> list[str]:
    from tribunal.grounding.base import GroundingTarget
    from tribunal.grounding.ruff_t import RuffTool

    return RuffTool().argv(
        GroundingTarget(path=path, logical_name=path.name, source=path.read_text())
    )


# -- REMAINING.md ------------------------------------------------------------------------------

REMAINING = ROOT / "REMAINING.md"


def test_remaining_md_exists_and_is_linked():
    """An unlinked ledger is a file in a directory."""
    assert REMAINING.is_file()
    assert "REMAINING.md" in (ROOT / "README.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("target", sorted(set(
    t for t in re.findall(r"\]\(([^)#]+)\)", REMAINING.read_text(encoding="utf-8"))
    if not t.startswith("http")
)))
def test_every_link_in_remaining_resolves(target):
    assert (ROOT / target).exists(), target


def test_the_one_unfixed_item_is_still_unfixed():
    """REMAINING.md § C1 says `run --help` omits `reject` from its exit-code table.

    A stale entry in a ledger of outstanding work is worse than a missing one: it sends a
    reader to look at something that is already fine, and it makes every other entry less
    believable. So this fails in *both* directions -- if C1 gets fixed, this test says to
    delete the section rather than leaving it to rot.
    """
    text = REMAINING.read_text(encoding="utf-8")
    help_text = " ".join((commands()["run"].help or "").split())
    describes_reject = "reject" in help_text.lower()

    if "C1." in text:
        assert not describes_reject, (
            "`run --help` now documents `reject` -- REMAINING.md § C1 is stale, delete it"
        )
        assert "0 accept, 1 tradeoff, 2 escalate" in help_text, (
            "REMAINING.md § C1 quotes the help text; the quote no longer matches"
        )
        # The premise of the entry: the two outcomes really do share an exit code.
        assert EXIT_FOR_OUTCOME["reject"] == EXIT_FOR_OUTCOME["escalate"]


def test_remaining_accounts_for_every_open_roadmap_criterion():
    """§ A10 lists the charter's acceptance criteria. If the charter grows an S9, the ledger
    should not quietly keep claiming it covers them all."""
    charter = (ROOT / "docs" / "00-charter.md").read_text(encoding="utf-8")
    declared = set(re.findall(r"^\| (S\d+) \|", charter, re.M))
    covered = set(re.findall(r"\*\*(S\d+)\*\*", REMAINING.read_text(encoding="utf-8")))
    assert declared, "no criteria found in the charter -- the table shape changed"
    assert declared <= covered, f"REMAINING.md § A10 omits {sorted(declared - covered)}"


# -- do.txt ------------------------------------------------------------------------------------

DO = ROOT / "do.txt"


def test_do_txt_does_not_overstate_the_test_count():
    """Same floor rule as the README, for the same reason: an exact count obliges every
    commit that adds a test to edit a plan file, which is a rule nobody follows."""
    claimed = int(re.search(r"([\d,]+)\+ tests green", DO.read_text(encoding="utf-8"))
                  .group(1).replace(",", ""))
    actual = _collected()
    assert claimed <= actual, f"do.txt claims {claimed}+ tests; only {actual} collected"
    assert claimed >= actual - 400, "the floor has drifted far below the truth"


def test_do_txt_and_remaining_agree_about_what_is_left():
    """The two files describe the same outstanding work from different angles, and do.txt's
    header sends the reader to the other one. A disagreement between them is worse than
    either being wrong alone -- the reader cannot tell which to believe.

    Only the structural claims are checked: whether section [C] is empty, and how many [A]
    items there are. Prose is not checkable and is not checked.
    """
    do = DO.read_text(encoding="utf-8")
    remaining = REMAINING.read_text(encoding="utf-8")

    c_is_empty = "[C] Found and left -- EMPTY" in do
    assert c_is_empty == ("**Empty.**" in remaining), (
        "do.txt and REMAINING.md disagree about whether section [C] still has an entry"
    )

    blocked = {m for m in re.findall(r"^### (A\d+)\.", remaining, re.M)}
    assert f"[A] Blocked on environment -- {len(blocked)} items" in do, (
        f"REMAINING.md has {len(blocked)} blocked items; do.txt says otherwise"
    )


def _collected() -> int:
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-header"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    found = re.search(r"(\d+)/?\d* tests collected", proc.stdout)
    if not found:  # pragma: no cover - collection is broken; other tests will say so
        pytest.skip(f"could not collect: {proc.stdout[-400:]}")
    return int(found.group(1))
