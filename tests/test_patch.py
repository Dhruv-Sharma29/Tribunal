"""VALIDATE is a cheap deterministic gate, so it gets the hardest tests in Phase 1.

docs/03-agents.md calls diff reliability "the main practical risk", and docs/01 says VALIDATE
is where a large share of early-round retries get absorbed at zero token cost. Both only hold
if the gate's error messages are precise enough for the Coder to act on without a critique.
"""

from __future__ import annotations

import pytest

from tribunal import patch
from tribunal.contracts import PatchProposal, SearchReplaceEdit, UnaddressedIssue

ORIGINAL = '''import sqlite3


def fetch(conn, user_id):
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = '%s'" % user_id)
    return cur.fetchall()


def audit(conn, user_id):
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = '%s'" % user_id)
    return cur.fetchall()
'''

SAFE_FETCH = '''def fetch(conn, user_id):
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))'''

UNSAFE_FETCH = '''def fetch(conn, user_id):
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = '%s'" % user_id)'''


def proposal(*, edits=(), diff="", round=1, addresses=("SEC-1",), unaddressed=()) -> PatchProposal:
    return PatchProposal(
        round=round,
        edits=list(edits),
        diff=diff,
        rationale="parameterise the query",
        addresses=list(addresses),
        deliberately_unaddressed=list(unaddressed),
    )


def edit(search: str, replace: str) -> SearchReplaceEdit:
    return SearchReplaceEdit(search=search, replace=replace)


# -- The happy path --------------------------------------------------------------------------


def test_search_replace_round_trips_into_an_applied_parsing_file():
    """Phase 1 acceptance criterion, verbatim."""
    p, v = patch.validate(ORIGINAL, proposal(edits=[edit(UNSAFE_FETCH, SAFE_FETCH)]))
    assert v.ok
    assert v.applied and v.parse_ok
    assert v.hunks == 1
    assert "cur.execute(\"SELECT * FROM users WHERE id = ?\", (user_id,))" in v.patched_source
    # audit() is untouched: the anchor was specific enough.
    assert v.patched_source.count("'%s'\" % user_id") == 1
    assert p.diff.startswith("--- a/target.py")


def test_the_synthesised_diff_reapplies_to_the_same_result():
    """The canonical form must be faithful: `Patch.diff` is what the trace, the viewer and the
    oscillation hash all see, so it has to reproduce the edit it came from."""
    _, direct = patch.validate(ORIGINAL, proposal(edits=[edit(UNSAFE_FETCH, SAFE_FETCH)]))
    _, replayed = patch.validate(ORIGINAL, proposal(diff=_diff_of(direct)))
    assert replayed.ok
    assert replayed.patched_source == direct.patched_source


def _diff_of(validation) -> str:
    return patch.make_unified_diff(ORIGINAL, validation.patched_source)


def test_a_deletion_edit_is_supported():
    _, v = patch.validate(ORIGINAL, proposal(edits=[edit("import sqlite3\n\n\n", "")]))
    assert v.ok
    assert "import sqlite3" not in v.patched_source


# -- Bad anchors: the mechanical error path --------------------------------------------------


def test_a_missing_anchor_names_the_line_it_looked_for():
    _, v = patch.validate(ORIGINAL, proposal(edits=[edit("def nonexistent():", "x")]))
    assert not v.ok
    assert not v.applied
    assert v.patched_source is None
    assert "not found" in v.failure_reason
    assert "def nonexistent():" in v.failure_reason
    assert "indentation" in v.failure_reason  # tells the Coder how to fix it


def test_an_ambiguous_anchor_is_reported_rather_than_applied_to_the_first_match():
    """`cur = conn.cursor()` appears twice. Editing the first one silently would change code
    the Coder did not intend to touch."""
    _, v = patch.validate(ORIGINAL, proposal(edits=[edit("    cur = conn.cursor()", "    pass")]))
    assert not v.ok
    assert "matches 2 places" in v.failure_reason
    assert "more surrounding context" in v.failure_reason


def test_an_empty_search_block_is_refused():
    _, v = patch.validate(ORIGINAL, proposal(edits=[edit("", "x = 1")]))
    assert not v.ok
    assert "empty search block" in v.failure_reason


def test_the_failing_edit_is_identified_by_index():
    """With several edits, "it failed" is useless; the Coder needs to know which one."""
    edits = [edit(UNSAFE_FETCH, SAFE_FETCH), edit("def missing():", "x")]
    _, v = patch.validate(ORIGINAL, proposal(edits=edits))
    assert v.failure_reason.startswith("edit 2:")


# -- Unified diffs ---------------------------------------------------------------------------


def test_a_well_formed_unified_diff_applies():
    diff = patch.make_unified_diff(ORIGINAL, ORIGINAL.replace(UNSAFE_FETCH, SAFE_FETCH))
    _, v = patch.validate(ORIGINAL, proposal(diff=diff))
    assert v.ok
    assert "(user_id,)" in v.patched_source


def test_a_hunk_with_wrong_line_numbers_is_relocated():
    """Models miscount line numbers; that is the single most common diff failure. A hunk whose
    context appears exactly once elsewhere is moved rather than rejected."""
    good = patch.make_unified_diff(ORIGINAL, ORIGINAL.replace(UNSAFE_FETCH, SAFE_FETCH))
    misnumbered = "\n".join(
        "@@ -91,7 +91,7 @@" if line.startswith("@@") else line for line in good.splitlines()
    ) + "\n"
    _, v = patch.validate(ORIGINAL, proposal(diff=misnumbered))
    assert v.ok
    assert v.patched_source == ORIGINAL.replace(UNSAFE_FETCH, SAFE_FETCH)


def test_relocation_refuses_to_guess_when_the_context_is_ambiguous():
    """Fuzzy application is how a patch tool corrupts a file. Two candidate sites plus a wrong
    line number is a refusal, not a coin flip."""
    diff = (
        "--- a/target.py\n"
        "+++ b/target.py\n"
        "@@ -900,1 +900,1 @@\n"
        "-    cur = conn.cursor()\n"
        "+    cur = conn.cursor()  # noqa\n"
    )
    _, v = patch.validate(ORIGINAL, proposal(diff=diff))
    assert not v.ok
    assert "matches 2 places" in v.failure_reason
    assert "lines 5, 11" in v.failure_reason  # names both candidates


def test_a_hunk_whose_context_is_absent_says_so_precisely():
    diff = (
        "--- a/target.py\n"
        "+++ b/target.py\n"
        "@@ -4,2 +4,2 @@\n"
        "-def fetch(conn, user_ident):\n"
        "+def fetch(conn, user_id):\n"
    )
    _, v = patch.validate(ORIGINAL, proposal(diff=diff))
    assert not v.ok
    assert "hunk 1 failed to apply at line 4" in v.failure_reason
    assert "def fetch(conn, user_ident):" in v.failure_reason


def test_a_diff_with_no_hunks_is_rejected():
    _, v = patch.validate(ORIGINAL, proposal(diff="I rewrote the file, trust me.\n"))
    assert not v.ok
    assert "not a unified diff" in v.failure_reason


def test_an_unrecognised_body_line_is_rejected():
    diff = "--- a/t.py\n+++ b/t.py\n@@ -1,1 +1,1 @@\n?import sqlite3\n"
    _, v = patch.validate(ORIGINAL, proposal(diff=diff))
    assert not v.ok
    assert "must start with" in v.failure_reason


def test_blank_context_lines_are_tolerated():
    """Some models emit a zero-length line where a single space was meant. Rejecting that
    would burn a retry on whitespace."""
    good = patch.make_unified_diff(ORIGINAL, ORIGINAL.replace(UNSAFE_FETCH, SAFE_FETCH))
    stripped = "\n".join(line.rstrip() if line == " " else line for line in good.splitlines())
    _, v = patch.validate(ORIGINAL, proposal(diff=stripped + "\n"))
    assert v.ok


def test_a_multi_hunk_diff_applies_in_order():
    patched = ORIGINAL.replace("import sqlite3", "import sqlite3  # stdlib").replace(
        UNSAFE_FETCH, SAFE_FETCH
    )
    diff = patch.make_unified_diff(ORIGINAL, patched)
    _, v = patch.validate(ORIGINAL, proposal(diff=diff))
    assert v.ok
    assert v.patched_source == patched


# -- Parsing and churn -----------------------------------------------------------------------


def test_a_patch_that_applies_but_does_not_parse_is_caught():
    unbalanced = edit(
        "    return cur.fetchall()\n\n\ndef audit",
        "    return cur.fetchall(\n\n\ndef audit",
    )
    _, v = patch.validate(ORIGINAL, proposal(edits=[unbalanced]))
    assert v.applied
    assert not v.parse_ok
    assert not v.ok
    assert "does not parse" in v.failure_reason
    assert "The hunks applied" in v.failure_reason  # distinguishes it from an anchor failure


def test_the_churn_guard_bounces_a_patch_that_applies_and_parses():
    """Scope creep is mechanically visible in diff mode: a model asked to fix one thing and
    returning thirty hunks is rewriting the file."""
    p, v = patch.validate(
        ORIGINAL, proposal(edits=[edit(UNSAFE_FETCH, SAFE_FETCH)]), max_hunks=0
    )
    assert v.applied and v.parse_ok
    assert not v.ok  # applied, parses, still rejected
    assert "over the limit of 0" in v.failure_reason
    assert v.patched_source is not None  # the work is preserved for the trace


def test_a_patch_within_the_churn_limit_passes():
    _, v = patch.validate(ORIGINAL, proposal(edits=[edit(UNSAFE_FETCH, SAFE_FETCH)]), max_hunks=8)
    assert v.ok


# -- The empty patch -------------------------------------------------------------------------


def test_an_empty_proposal_is_not_an_application_failure():
    """"Nothing to fix" is a legitimate Coder position; policy decides whether it is right."""
    pushback = UnaddressedIssue(issue_id="SEC-1", reason="already validated upstream")
    p, v = patch.validate(ORIGINAL, proposal(unaddressed=[pushback]))
    assert v.ok
    assert p.diff == ""
    assert v.hunks == 0
    assert v.patched_source == ORIGINAL
    assert p.deliberately_unaddressed[0].reason == "already validated upstream"


def test_an_edit_that_changes_nothing_produces_an_empty_diff():
    _, v = patch.validate(ORIGINAL, proposal(edits=[edit(UNSAFE_FETCH, UNSAFE_FETCH)]))
    assert v.ok
    assert v.patched_source == ORIGINAL


# -- Hashing ---------------------------------------------------------------------------------


def test_the_oscillation_hash_ignores_line_number_drift():
    """Row 4 of the decision table hashes the *normalised* diff. Without normalisation an
    A->B->A cycle whose hunks shifted by one line would go undetected -- exactly the case the
    guard exists for."""
    good = patch.make_unified_diff(ORIGINAL, ORIGINAL.replace(UNSAFE_FETCH, SAFE_FETCH))
    drifted = "\n".join(
        "@@ -41,7 +41,7 @@" if line.startswith("@@") else line for line in good.splitlines()
    ) + "\n"
    assert patch.diff_sha256(good) != patch.diff_sha256(drifted)
    assert patch.normalised_diff_sha256(good) == patch.normalised_diff_sha256(drifted)


def test_the_oscillation_hash_still_separates_different_changes():
    a = patch.make_unified_diff(ORIGINAL, ORIGINAL.replace(UNSAFE_FETCH, SAFE_FETCH))
    b = patch.make_unified_diff(ORIGINAL, ORIGINAL.replace("import sqlite3", "import sqlite3  # x"))
    assert patch.normalised_diff_sha256(a) != patch.normalised_diff_sha256(b)


def test_validation_records_the_diff_hash_even_on_failure():
    _, v = patch.validate(ORIGINAL, proposal(edits=[edit("def missing():", "x")]))
    assert len(v.diff_sha256) == 64


# -- Trailing-newline convention -------------------------------------------------------------


def test_a_file_without_a_trailing_newline_keeps_that_convention():
    original = "x = 1\ny = 2"
    diff = patch.make_unified_diff(original, "x = 1\ny = 3")
    assert patch.apply_unified_diff(original, diff) == "x = 1\ny = 3"


@pytest.mark.parametrize("bad", ["", "   \n"])
def test_a_blank_diff_is_treated_as_no_change(bad):
    _, v = patch.validate(ORIGINAL, proposal(diff=bad))
    assert v.ok
    assert v.patched_source == ORIGINAL


# -- the escaped-newline anchor repair -------------------------------------------------------


def test_an_escaped_newline_anchor_is_repaired():
    """Models double-escape newlines inside the JSON string value: `search` arrives with the
    two characters backslash-n where the file has a real newline. Measured on NVIDIA Nemotron,
    it broke 2 of 6 Coder fixtures."""
    edit = SearchReplaceEdit(
        search="def fetch(conn, user_id):\\n    cur = conn.cursor()",
        replace="def fetch(conn, user_id):\\n    cur = conn.cursor()  # noqa",
    )
    patched, repairs = patch.apply_search_replace(ORIGINAL, proposal(edits=[edit]))
    assert repairs == ["edit 1: repaired an escaped-newline anchor into real newlines"]
    assert "# noqa" in patched


def test_the_repair_is_reported_rather_than_swallowed():
    """A systematic escaping fault must not hide behind a green result -- the repair is the
    prompt-health signal that says the model is getting this wrong."""
    edit = SearchReplaceEdit(search="import sqlite3\\n", replace="import sqlite3  # stdlib\\n")
    _, repairs = patch.apply_search_replace(ORIGINAL, proposal(edits=[edit]))
    assert repairs


def test_a_source_containing_a_real_literal_backslash_n_is_not_corrupted():
    """The condition that makes the repair safe. This source genuinely contains backslash-n;
    unescaping unconditionally would rewrite the program."""
    source = 'def render(rows):\n    return ",".join(rows) + "\\n"\n'
    assert "\\n" in source  # a literal backslash followed by n, inside a string literal
    edit = SearchReplaceEdit(
        search='    return ",".join(rows) + "\\n"',
        replace='    return "\\n".join(rows)',
    )
    patched, repairs = patch.apply_search_replace(source, proposal(edits=[edit]))
    assert repairs == []  # the verbatim anchor matched, so nothing was repaired
    assert patched == 'def render(rows):\n    return "\\n".join(rows)\n'


def test_the_repair_refuses_when_the_unescaped_form_is_ambiguous():
    """Same discipline as hunk relocation: repair when the answer is unambiguous, refuse when
    it is not."""
    source = "a = 1\nb = 2\na = 1\nb = 2\n"
    edit = SearchReplaceEdit(search="a = 1\\nb = 2", replace="a = 9")
    _, validation = patch.validate(source, proposal(edits=[edit]))
    assert not validation.ok
    assert "not found" in validation.failure_reason


def test_the_anchor_error_explains_the_escaping_trap():
    """The message is the repair prompt, so it has to name the likely cause."""
    edit = SearchReplaceEdit(search="def totally_absent():\\n    pass", replace="x")
    _, validation = patch.validate(ORIGINAL, proposal(edits=[edit]))
    assert not validation.ok
    assert "real newlines" in validation.failure_reason


def test_unescape_touches_only_newlines():
    """Narrow on purpose.

    An earlier version also unescaped backslash-t, and this test caught it mangling
    ``C:\\tools`` -- two characters that a Windows path in the source legitimately contains.
    There was never a measured failure behind tab handling, so it is gone: only repair what has
    actually been observed to break.
    """
    from tribunal.patch import _unescape

    assert _unescape(r"C:\path\to\file") == r"C:\path\to\file"
    assert _unescape(r"re.compile(r'\d+')") == r"re.compile(r'\d+')"
    assert _unescape(r"a\tb") == r"a\tb"
    assert _unescape(r"a\nb") == "a\nb"
