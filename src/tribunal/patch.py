"""Patch synthesis, application, and the deterministic VALIDATE gate.

`VALIDATE` exists to stop the system spending two LLM calls to discover that a diff had the
wrong line numbers (docs/01-architecture.md § Why VALIDATE exists). It is pure mechanism: the
patch applies or it does not, the result parses or it does not, and the failure message is
mechanical enough for the Coder to act on without another critique round.

Two input formats, in the order docs/03-agents.md recommends:

1. **Search/replace blocks (primary).** Anchoring on unique context strings is far more robust
   than anchoring on line numbers, which models miscount. We synthesise the real unified diff
   with `difflib` on our side.
2. **Unified diff (fallback).** Applied by `apply_unified_diff` below, which deliberately
   tolerates wrong `@@` line numbers by relocating a hunk whose context appears exactly once
   elsewhere -- and reports precisely when it cannot.

`Patch.diff` is always a unified diff, whichever format the model used, so everything
downstream (the trace, the viewer, the oscillation hash) has one representation to deal with.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import re
from dataclasses import dataclass

from tribunal.contracts import (
    Patch,
    PatchProposal,
    PatchValidation,
    SearchReplaceEdit,
)

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(ValueError):
    """A mechanical failure to apply. The message is written for the Coder to read."""


# --------------------------------------------------------------------------------------------
# Search/replace blocks
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Applied:
    source: str
    hunks: int


def apply_search_replace(original: str, proposal: PatchProposal) -> tuple[str, list[str]]:
    """Apply each edit's `search` -> `replace`, requiring a unique anchor.

    Returns `(patched_source, repairs)`. Uniqueness is the whole point of the format: a
    `search` block matching twice means the model's anchor was ambiguous, and applying it to
    the first match would silently edit the wrong place. That is reported, not guessed at.
    """
    source = original
    repairs: list[str] = []
    for index, edit in enumerate(proposal.edits, start=1):
        if edit.search == "":
            raise PatchError(f"edit {index}: empty search block; use a unique anchor")

        search, replace, repair = _resolve_anchor(source, edit, index)
        if repair:
            repairs.append(repair)
        source = source.replace(search, replace, 1)
    return source, repairs


def _resolve_anchor(
    source: str, edit: SearchReplaceEdit, index: int
) -> tuple[str, str, str | None]:
    r"""Find `edit.search` in `source`, repairing a literal-backslash-n anchor if needed.

    **Why this repair exists.** Models routinely emit the two characters ``\`` and ``n`` where
    the file has a real newline -- double-escaping inside the JSON string value. Measured on
    NVIDIA Nemotron across the Coder fixtures: it broke 2 of 6 patches, once as "search block
    not found" and once as a replacement that applied and then would not parse.

    **Why it is conditional.** Source code legitimately contains a literal backslash-n -- a
    fixture here has ``",".join(...) + "\n"`` in it. Unescaping unconditionally would corrupt
    exactly that case. So the escaped form is tried *only* when the verbatim anchor does not
    match, and accepted only when the unescaped form matches exactly once. Same discipline as
    hunk relocation: repair when the answer is unambiguous, refuse when it is not.

    The repair is returned, not swallowed, so it lands in the trace as a prompt-health signal
    rather than hiding a systematic escaping fault behind a green result.
    """
    count = source.count(edit.search)
    if count == 1:
        return edit.search, edit.replace, None
    if count > 1:
        raise PatchError(
            f"edit {index}: search block matches {count} places; the anchor is ambiguous. "
            f"Include more surrounding context so it matches exactly once."
        )

    unescaped = _unescape(edit.search)
    if unescaped != edit.search and source.count(unescaped) == 1:
        return (
            unescaped,
            _unescape(edit.replace),
            f"edit {index}: repaired an escaped-newline anchor into real newlines",
        )

    raise PatchError(
        f"edit {index}: search block not found. Its first line was "
        f"{_first_line(edit.search)!r}. Copy the anchor verbatim from the source, "
        f"including indentation. If your text spans several lines, the newlines must be real "
        f"newlines in the JSON string, not the two characters backslash-n."
    )


def _unescape(text: str) -> str:
    r"""Turn the two-character sequence ``\n`` into a newline. Nothing else.

    Deliberately not ``codecs.decode(..., "unicode_escape")``: that also rewrites ``\x``,
    ``\u`` and ``\\``, which would mangle a regex or a Windows path living in the source.

    It also deliberately does **not** handle ``\t``, which an earlier version did. A Windows
    path such as ``C:\tools`` contains the two characters ``\`` and ``t``, so tab-unescaping
    rewrites it -- and unlike the newline case there was never a measured failure behind tab
    handling. Repair only what has actually been observed to break.
    """
    return text.replace("\\n", "\n")


def _first_line(text: str) -> str:
    return text.splitlines()[0] if text.splitlines() else ""


# --------------------------------------------------------------------------------------------
# Unified diff
# --------------------------------------------------------------------------------------------


def make_unified_diff(original: str, patched: str, filename: str = "target.py") -> str:
    """Render the canonical unified diff for a pair of sources. Empty string if identical.

    Both sides are given a trailing newline before diffing. `difflib.unified_diff` emits each
    body line as `marker + line`, so when the final line has no newline of its own the last
    two body lines are concatenated into one -- `-y = 2+y = 3` -- and the resulting diff is
    unparseable. `apply_unified_diff` restores the original's convention afterwards, so the
    round trip is still faithful for a file with no trailing newline.
    """
    if original == patched:
        return ""
    diff = difflib.unified_diff(
        _with_final_newline(original).splitlines(keepends=True),
        _with_final_newline(patched).splitlines(keepends=True),
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        n=3,
    )
    return "".join(diff)


def _with_final_newline(text: str) -> str:
    return text if text == "" or text.endswith("\n") else text + "\n"


@dataclass(frozen=True)
class _Hunk:
    index: int
    old_start: int  # 1-based, as stated in the @@ header
    pre: list[str]  # lines the hunk expects to find (context + removals)
    post: list[str]  # lines it leaves behind (context + additions)


def parse_unified_diff(diff: str) -> list[_Hunk]:
    """Parse hunks out of a one-file unified diff.

    Tolerant of the two things models get wrong about the format: a zero-length line where a
    single space was meant for an empty context line, and a missing count on `@@ -41 +41 @@`.
    """
    hunks: list[_Hunk] = []
    current: _Hunk | None = None
    pre: list[str] = []
    post: list[str] = []

    def flush() -> None:
        nonlocal current, pre, post
        if current is not None:
            hunks.append(
                _Hunk(index=current.index, old_start=current.old_start, pre=pre, post=post)
            )
        current, pre, post = None, [], []

    for raw in diff.splitlines():
        header = HUNK_HEADER.match(raw)
        if header:
            flush()
            current = _Hunk(
                index=len(hunks) + 1, old_start=int(header.group(1)), pre=[], post=[]
            )
            continue
        if current is None:
            continue  # ---/+++ headers, "diff --git", and any preamble
        if raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        marker, _, body = raw[:1], None, raw[1:]
        if marker == "-":
            pre.append(body)
        elif marker == "+":
            post.append(body)
        elif marker in (" ", ""):
            pre.append(body)
            post.append(body)
        else:
            raise PatchError(
                f"hunk {current.index}: unrecognised diff line {raw[:40]!r}; "
                "every body line must start with ' ', '-', or '+'"
            )
    flush()
    if not hunks:
        raise PatchError("no @@ hunk headers found; the diff is not a unified diff")
    return hunks


def apply_unified_diff(original: str, diff: str) -> str:
    """Apply a unified diff, relocating hunks whose stated line numbers are wrong.

    Relocation is bounded and honest: a hunk's pre-image must appear *exactly once* in the
    remaining source for the hunk to move. Fuzzy matching is deliberately not implemented --
    silently applying an approximate hunk is how a patch tool corrupts a file.
    """
    lines = original.splitlines(keepends=True)
    result: list[str] = []
    cursor = 0  # index into `lines` of the first not-yet-consumed line

    for hunk in parse_unified_diff(diff):
        pre = [line + "\n" for line in hunk.pre]
        at = _locate(lines, pre, hunk, cursor)
        result.extend(lines[cursor:at])
        result.extend(line + "\n" for line in hunk.post)
        cursor = at + len(pre)
    result.extend(lines[cursor:])

    patched = "".join(result)
    # splitkeepends/rejoin normalises a missing trailing newline; restore the original's
    # convention so the diff of the diff stays clean.
    if not original.endswith("\n") and patched.endswith("\n"):
        patched = patched[:-1]
    return patched


def _locate(lines: list[str], pre: list[str], hunk: _Hunk, cursor: int) -> int:
    """Find where `pre` sits in `lines`, at or after `cursor`."""
    stated = hunk.old_start - 1
    if not pre:
        # Pure-insertion hunk with no context: nothing to anchor on, so trust the header.
        if not cursor <= stated <= len(lines):
            raise PatchError(
                f"hunk {hunk.index} failed to apply at line {hunk.old_start}: "
                f"insertion point is outside the file (file has {len(lines)} lines)"
            )
        return stated
    if stated >= cursor and _matches(lines, pre, stated):
        return stated
    matches = [i for i in range(cursor, len(lines) - len(pre) + 1) if _matches(lines, pre, i)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise PatchError(
            f"hunk {hunk.index} failed to apply at line {hunk.old_start}: context not found "
            f"anywhere in the file. Its first anchor line is {_anchor(pre)!r}; line "
            f"{hunk.old_start} of the file is {_actual(lines, stated)!r}. Copy context lines "
            "verbatim from the source."
        )
    raise PatchError(
        f"hunk {hunk.index} failed to apply at line {hunk.old_start}: its context matches "
        f"{len(matches)} places (lines {', '.join(str(m + 1) for m in matches)}) and the stated "
        f"line number is not one of them. Add more context lines."
    )


def _matches(lines: list[str], pre: list[str], at: int) -> bool:
    if at < 0 or at + len(pre) > len(lines):
        return False
    return all(lines[at + i].rstrip("\n") == pre[i].rstrip("\n") for i in range(len(pre)))


def _anchor(pre: list[str]) -> str:
    """The first non-blank line of a hunk's pre-image.

    Quoting `pre[0]` verbatim is useless when the hunk opens on a blank context line, which is
    common: the Coder would be told its context should have been ''.
    """
    for line in pre:
        stripped = line.rstrip("\n")
        if stripped.strip():
            return stripped
    return pre[0].rstrip("\n") if pre else ""


def _actual(lines: list[str], index: int) -> str:
    if 0 <= index < len(lines):
        return lines[index].rstrip("\n")
    return "<past end of file>"


# --------------------------------------------------------------------------------------------
# Hashing
# --------------------------------------------------------------------------------------------


def diff_sha256(diff: str) -> str:
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()


def normalised_diff_sha256(diff: str) -> str:
    """Hash for the oscillation guard (decision table row 4).

    Strips file headers, `@@` line numbers, and trailing whitespace, so that "the same logical
    change" hashes the same even when it lands at a different offset. Without the
    normalisation, an A->B->A cycle whose hunks shift by one line would go undetected -- which
    is precisely the case the guard exists for.
    """
    kept: list[str] = []
    for raw in diff.splitlines():
        if raw.startswith(("--- ", "+++ ", "diff --git", "index ")):
            continue
        if HUNK_HEADER.match(raw):
            kept.append("@@")
            continue
        kept.append(raw.rstrip())
    return hashlib.sha256("\n".join(kept).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------------
# The VALIDATE gate
# --------------------------------------------------------------------------------------------


def synthesise_patch(
    original: str, proposal: PatchProposal, filename: str = "target.py"
) -> tuple[Patch, str | None, str | None]:
    """Normalise a `PatchProposal` into a `Patch` carrying a unified diff.

    Returns `(patch, patched_source, error)`. On failure `patched_source` is None, `error` is
    the mechanical message, and `patch.diff` is whatever we could render -- the Coder's retry
    needs to see its own attempt.
    """
    try:
        if proposal.edits:
            patched, repairs = apply_search_replace(original, proposal)
            diff = make_unified_diff(original, patched, filename)
        elif not proposal.is_empty:
            # `is_empty`, not `proposal.diff`: a whitespace-only diff field is the model saying
            # nothing, and treating it as a malformed diff would spend a retry on a blank.
            diff = proposal.diff
            patched = apply_unified_diff(original, diff)
        else:
            # An empty proposal is the Coder claiming there is nothing to fix. That is a
            # legitimate position, handled by policy, not an application failure.
            return _patch_of(proposal, ""), original, None
    except PatchError as exc:
        return _patch_of(proposal, proposal.diff or ""), None, str(exc)
    return _patch_of(proposal, diff), patched, None


def _patch_of(proposal: PatchProposal, diff: str) -> Patch:
    return Patch(
        round=proposal.round,
        diff=diff,
        rationale=proposal.rationale,
        addresses=proposal.addresses,
        deliberately_unaddressed=proposal.deliberately_unaddressed,
    )


def validate(
    original: str,
    proposal: PatchProposal,
    filename: str = "target.py",
    max_hunks: int | None = None,
) -> tuple[Patch, PatchValidation]:
    """The deterministic gate. No LLM, no critics: the patch applies and parses, or it bounces.

    Order matters. Application before parsing before churn: a patch that does not apply has no
    source to parse, and a patch that does not parse should be reported as a syntax error
    rather than as gratuitous churn.
    """
    patch, patched, error = synthesise_patch(original, proposal, filename)
    hunk_count = _count_hunks(patch.diff)

    if error is not None:
        return patch, PatchValidation(
            applied=False,
            parse_ok=False,
            hunks=hunk_count,
            diff_sha256=diff_sha256(patch.diff),
            patched_source=None,
            failure_reason=error,
        )

    assert patched is not None  # noqa: S101 - error is None implies patched is set
    try:
        ast.parse(patched, filename=filename)
    except SyntaxError as exc:
        return patch, PatchValidation(
            applied=True,
            parse_ok=False,
            hunks=hunk_count,
            diff_sha256=diff_sha256(patch.diff),
            patched_source=patched,
            failure_reason=(
                f"patched file does not parse: {exc.msg} at line {exc.lineno}. "
                "The hunks applied, so the edit itself is malformed Python."
            ),
        )

    if max_hunks is not None and hunk_count > max_hunks:
        # Not a correctness failure: a signal the Coder is rewriting rather than fixing
        # (docs/03-agents.md § Coder, mitigation 4).
        return patch, PatchValidation(
            applied=True,
            parse_ok=True,
            hunks=hunk_count,
            diff_sha256=diff_sha256(patch.diff),
            patched_source=patched,
            failure_reason=(
                f"patch touches {hunk_count} hunks, over the limit of {max_hunks}. Make the "
                f"minimal change that addresses {len(patch.addresses)} issue(s); do not "
                "reformat, rename, or add type hints."
            ),
        )

    return patch, PatchValidation(
        applied=True,
        parse_ok=True,
        hunks=hunk_count,
        diff_sha256=diff_sha256(patch.diff),
        patched_source=patched,
        failure_reason=None,
    )


def _count_hunks(diff: str) -> int:
    return sum(1 for line in diff.splitlines() if HUNK_HEADER.match(line))
