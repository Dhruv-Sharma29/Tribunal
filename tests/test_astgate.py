"""The AST gate.

Tested at the level its docstring claims and no higher: it catches the realistic cases -- a
careless model and an opportunistic snippet -- and it is trivially bypassable. The bypass is
tested too, so the suite states the limitation rather than implying a boundary that is not
there.
"""

from __future__ import annotations

import pytest

from tribunal import astgate


def rules(source: str) -> set[str]:
    return {hit.rule for hit in astgate.scan(source)}


# -- Dynamic execution -----------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(astgate.FORBIDDEN_CALLS))
def test_every_dynamic_execution_builtin_is_flagged(name):
    assert f"forbidden-call:{name}" in rules(f"x = {name}('1 + 1')\n")


def test_a_forbidden_call_does_not_block_execution():
    """`eval` is a finding for the Red-team to rate, not a reason to refuse to run the file.
    Refusing would take the correctness oracle away from every file that uses it."""
    hits = astgate.scan("eval('1')\n")
    assert hits and not any(h.blocks_execution for h in hits)
    assert astgate.execution_refusal("eval('1')\n") is None


# -- Imports ---------------------------------------------------------------------------------


@pytest.mark.parametrize("module", sorted(astgate.FORBIDDEN_MODULES))
def test_every_denied_module_import_blocks_execution(module):
    assert f"forbidden-import:{module}" in rules(f"import {module}\n")
    assert astgate.execution_refusal(f"import {module}\n") is not None


def test_a_submodule_import_is_attributed_to_its_root():
    assert "forbidden-import:http" in rules("import http.client\n")
    assert "forbidden-import:urllib" in rules("from urllib import request\n")


def test_network_and_non_network_refusals_are_distinguished():
    """The refusal string goes into `tool_errors["sandbox"]`, so it has to say which risk
    tripped -- network exfiltration and sandbox escape are different conversations."""
    assert "network module import" in astgate.execution_refusal("import socket\n")
    assert "denied module import" in astgate.execution_refusal("import ctypes\n")


def test_a_relative_import_is_ignored():
    """Single-file scope: `from . import x` has no root module to check, and guessing would
    produce a finding with nothing behind it."""
    assert rules("from . import helpers\n") == set()


def test_a_star_import_binds_names_we_cannot_resolve():
    """The import itself is still flagged; the names it brings in are not tracked, and
    pretending otherwise would be a false sense of coverage."""
    assert "forbidden-import:socket" in rules("from socket import *\n")


# -- Aliasing --------------------------------------------------------------------------------


def test_a_module_alias_is_resolved():
    """`import subprocess as sp` is common in model output; missing it would make the gate
    decorative."""
    assert "suspicious-call:subprocess.run" in rules("import subprocess as sp\nsp.run(['ls'])\n")


def test_a_from_import_binding_is_resolved():
    assert "suspicious-call:os.system" in rules("from os import system\nsystem('ls')\n")


def test_a_renamed_from_import_is_resolved():
    assert "suspicious-call:os.system" in rules("from os import system as sh\nsh('ls')\n")


def test_an_unrelated_method_named_run_is_not_flagged():
    """A wildcard on `subprocess` must not turn every `.run()` in the file into a finding."""
    assert rules("class Job:\n    def run(self): pass\n\nJob().run()\n") == set()


def test_a_local_variable_shadowing_a_module_name_is_a_known_false_positive():
    """Documented, not fixed. Resolving this needs scope tracking, and the cost of a spurious
    advisory finding is one Red-team dismissal -- the system has a mechanism for that."""
    assert "suspicious-call:os.system" in rules("os = FakeShell()\nos.system('ls')\n")


# -- shell=True ------------------------------------------------------------------------------


def test_shell_true_is_flagged_wherever_it_appears():
    """Called out separately because the threat model names it as routine model output."""
    assert "shell-true" in rules("import subprocess\nsubprocess.run('ls', shell=True)\n")
    assert "shell-true" in rules("run(cmd, shell=True)\n")


def test_shell_false_is_not_flagged():
    assert "shell-true" not in rules("import subprocess\nsubprocess.run(['ls'], shell=False)\n")


def test_a_non_literal_shell_argument_is_not_flagged():
    """`shell=flag` is not provably True, and a gate that guesses produces findings a critic
    cannot cite."""
    assert "shell-true" not in rules("run(cmd, shell=flag)\n")


# -- Findings --------------------------------------------------------------------------------


def test_findings_are_normalised_and_stable():
    source = "import socket\nimport subprocess\nsubprocess.run('x', shell=True)\n"
    first = astgate.findings(source, "a.py")
    second = astgate.findings(source, "a.py")
    assert [f.id for f in first] == [f.id for f in second]
    assert all(f.tool == "astgate" for f in first)
    assert all(f.file == "a.py" for f in first)
    assert len({f.id for f in first}) == len(first)


def test_a_findings_severity_is_the_gates_own_rating():
    findings = {f.rule: f for f in astgate.findings("import socket\neval('1')\n")}
    assert findings["forbidden-import:socket"].tool_severity == "blocking"
    assert findings["forbidden-call:eval"].tool_severity == "advisory"


def test_hits_are_ordered_by_line():
    source = "import subprocess\nsubprocess.run('a', shell=True)\nimport socket\n"
    lines = [h.line for h in astgate.scan(source)]
    assert lines == sorted(lines)


# -- Limitations, stated ---------------------------------------------------------------------


def test_the_gate_is_bypassable_and_that_is_documented_not_fixed():
    """A hygiene tripwire, not a security boundary. The real boundary is the container.

    If this test ever starts failing because someone "hardened" the gate, the fix is to delete
    the hardening: a gate that looks complete invites relying on it.
    """
    assert rules("getattr(__builtins__, 'ev' + 'al')('1+1')\n") == set()
    assert rules("__import__('imp' + 'ortlib').import_module('socket')\n") != set()  # eval-ish
    assert astgate.execution_refusal("m = 'soc' + 'ket'\n__import__(m)\n") is None


def test_a_file_that_does_not_parse_yields_no_hits():
    """A syntax error belongs to `patch.py` and the `input_unusable` policy row, not here."""
    assert astgate.scan("def broken(\n") == []
    assert astgate.findings("def broken(\n") == []


# -- The benchmark expression gate -----------------------------------------------------------


def test_a_single_call_expression_is_accepted():
    call = astgate.assert_single_call_expression("parse_records(rows, strict=True)")
    assert call.func.id == "parse_records"


@pytest.mark.parametrize(
    "expression",
    [
        "parse(rows); import os; os.system('id')",  # statement list
        "import os",
        "1 + 1",
        "rows",
        "[f(x) for x in rows]",
        "",
        "not python (",
    ],
)
def test_anything_other_than_one_call_is_refused(expression):
    """The timeit runner template is ours; this is the only hole the model reaches through, so
    it accepts exactly one shape."""
    with pytest.raises(ValueError):
        astgate.assert_single_call_expression(expression)
