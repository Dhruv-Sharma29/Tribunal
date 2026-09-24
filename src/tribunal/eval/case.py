"""The benchmark case format, and the guards that make hand-authoring it safe.

docs/07-evaluation.md § Case format specifies a directory per case: `before.py`, `test_case.py`,
`meta.yaml`, `notes.md`. This module is that format as a schema, plus a loader that refuses a
case it cannot score.

## Why the loader is strict

24 cases is a day of slow manual work, and every one of them is a claim about what the right
answer is. A case with a typo'd rule code, a line range past the end of the file, or a
`conflicting` label that expects `accept` does not fail loudly at authoring time — it fails
quietly at *scoring* time, as a known issue nobody could ever catch, and it lands in the
results table as evidence that the tribunal missed something.

So the rules docs/07 states in prose are enforced here instead: a canary-clean case has no
known issues, a conflicting case expects `tradeoff`, a seeded-bad case carries a high-severity
security issue for the critics to catch, every locator resolves against the file it points
into, and `notes.md` is not empty. Authoring a broken case should be a loud failure in the
test suite, not a silent point against an arm.

The one rule that is not in docs/07: `before.py` must parse. The orchestrator now escalates an
unparseable input before the Coder is called ([13](../../../docs/13-implementation-notes.md)
§ 44), so such a case would score every arm identically regardless of what it was testing.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tribunal.contracts import Decision, Dimension, Severity

#: docs/07 § Composition. The counts it asks for are checked over a whole suite by
#: `Benchmark.check_composition`, not here -- one case cannot know what the set looks like.
Category = Literal[
    "security_only",
    "performance_only",
    "both_independent",
    "conflicting",
    "canary_clean",
    "canary_seeded_bad",
]

Split = Literal["dev", "heldout"]

#: The two mechanical locator kinds, preferred in this order, plus the judge fallback.
#: docs/07: "prefer the mechanical match, fall back to the judge".
LocatorKind = Literal["rule", "line_range", "description"]

#: A tool rule code: `B608`, `S608`, `C901`, `PERF401`. Deliberately narrow -- a typo here is
#: a known issue that can never be caught, which reads in the results as a miss.
RULE_CODE = re.compile(r"^[A-Z]{1,4}\d{2,4}$")


class CaseError(ValueError):
    """A case on disk is malformed. Raised at load time, never at scoring time."""


class Locator(BaseModel):
    """How a known issue is recognised in a `Critique`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: LocatorKind
    value: str | tuple[int, int]
    #: Required on a `line_range`: the code those lines are expected to contain, whitespace
    #: collapsed. A rule locator is checkable against the tools -- either they emit that code
    #: on this file or they do not. A line range is not: every number inside the file is
    #: structurally valid, so an off-by-one lands on the line *below* the defect and scores
    #: as a miss against every arm. Authoring case 002 made exactly that mistake. Stating the
    #: text makes the case self-checking, and makes an edit to `before.py` that shifts the
    #: lines a loud failure rather than a silent one.
    anchor: str | None = None

    @model_validator(mode="after")
    def _value_matches_kind(self) -> Locator:
        if self.kind == "line_range":
            if not isinstance(self.value, tuple):
                raise ValueError("a line_range locator's value must be [start, end]")
            start, end = self.value
            if start < 1 or end < start:
                raise ValueError(f"line_range {self.value} is not a 1-based inclusive span")
            if not (self.anchor or "").strip():
                raise ValueError(
                    f"line_range {self.value} has no `anchor`. State the code those lines "
                    "are meant to contain — nothing else can tell an off-by-one from a "
                    "deliberate choice."
                )
        elif self.anchor is not None:
            raise ValueError(f"a {self.kind} locator does not take an anchor")
        else:
            if not isinstance(self.value, str) or not self.value.strip():
                raise ValueError(f"a {self.kind} locator's value must be a non-empty string")
            if self.kind == "rule" and not RULE_CODE.match(self.value):
                raise ValueError(
                    f"{self.value!r} does not look like a tool rule code (B608, S608, C901). "
                    "A typo here is a known issue nothing can ever match, which scores as a "
                    "miss against every arm."
                )
        return self


class KnownIssue(BaseModel):
    """One defect the case asserts is present, and how to tell whether it was found."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1)
    dimension: Dimension
    expected_severity: Severity
    locator: Locator
    description: str = Field(min_length=1, max_length=400)


class CaseMeta(BaseModel):
    """`meta.yaml`, exactly."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    category: Category
    known_issues: list[KnownIssue] = Field(default_factory=list)
    expected_outcome: Decision
    forbidden_regressions: list[str] = Field(default_factory=list)
    runnable_benchmark: bool = False
    provenance: str = Field(min_length=1)
    split: Split

    @model_validator(mode="after")
    def _category_invariants(self) -> CaseMeta:
        keys = [issue.key for issue in self.known_issues]
        if len(keys) != len(set(keys)):
            raise ValueError(f"duplicate known_issue keys in {self.id}: {keys}")

        if self.category == "canary_clean":
            # docs/07: "correct, idiomatic code. Any HIGH issue raised is a false positive."
            # A clean case with a known issue is not a canary, it is a mislabelled case, and
            # it would poison M3 -- the metric that detects critique inflation.
            if self.known_issues:
                raise ValueError(
                    f"{self.id} is canary_clean but declares known issues. The whole point "
                    "of the category is that there is nothing to find."
                )
            if self.expected_outcome is not Decision.ACCEPT:
                raise ValueError(f"{self.id} is canary_clean, so it must expect accept")
        elif not self.known_issues:
            raise ValueError(
                f"{self.id} is {self.category} but declares no known issues, so M1 recall "
                "can neither pass nor fail on it."
            )

        if self.category == "conflicting" and self.expected_outcome is not Decision.TRADEOFF:
            # docs/07 § Metrics, M5: on the conflicting cases B0-B2 structurally cannot
            # produce the right answer. A conflicting case expecting `accept` would hand
            # them the point.
            raise ValueError(
                f"{self.id} is conflicting, so its expected_outcome must be tradeoff — that "
                "is the capability the category exists to measure."
            )
        if self.category == "conflicting":
            dimensions = {issue.dimension for issue in self.known_issues}
            if dimensions != {Dimension.SECURITY, Dimension.PERFORMANCE}:
                raise ValueError(
                    f"{self.id} is conflicting but its known issues are all "
                    f"{sorted(d.value for d in dimensions)}. A trade-off needs one of each."
                )

        if self.category == "canary_seeded_bad":
            # docs/07: "the input already contains an obvious HIGH vuln that any critic must
            # catch. Catches sycophancy." Without a HIGH, the case cannot detect it.
            severe = [
                issue
                for issue in self.known_issues
                if issue.expected_severity is Severity.HIGH
                and issue.dimension is Dimension.SECURITY
            ]
            if not severe:
                raise ValueError(
                    f"{self.id} is canary_seeded_bad but declares no high-severity security "
                    "issue, so a sycophantic critic would pass it."
                )
        return self


@dataclass(frozen=True)
class EvalCase:
    """A loaded case: its metadata and the files beside it."""

    meta: CaseMeta
    directory: Path
    source: str
    notes: str
    test_source: str | None = None

    @property
    def id(self) -> str:
        return self.meta.id

    @property
    def filename(self) -> str:
        return "before.py"

    @property
    def known_issues(self) -> list[KnownIssue]:
        return list(self.meta.known_issues)

    def located_lines(self, known: KnownIssue) -> list[str]:
        """The source lines a `line_range` locator points at, for review and for pinning.

        A `rule` locator is checkable -- either the tools emit that code on this file or
        they do not. A `line_range` is not: any number inside the file is structurally
        valid, and an off-by-one lands on the `return` below the defect and scores as a miss
        against every arm. Nothing can verify the *intent*, so the next best thing is to
        make what it points at visible, and to let a test pin it so an edit to `before.py`
        that shifts the lines fails loudly instead of silently.
        """
        if known.locator.kind != "line_range":
            return []
        start, end = known.locator.value  # type: ignore[misc]
        return self.source.splitlines()[start - 1 : end]

    def issue(self, key: str) -> KnownIssue:
        for known in self.meta.known_issues:
            if known.key == key:
                return known
        raise KeyError(key)


# -- loading ----------------------------------------------------------------------------------


def load_case(directory: Path) -> EvalCase:
    """Read one case directory, or raise `CaseError` naming what is wrong with it."""
    meta_path = directory / "meta.yaml"
    source_path = directory / "before.py"
    notes_path = directory / "notes.md"

    for required in (meta_path, source_path, notes_path):
        if not required.is_file():
            raise CaseError(f"{directory.name}: missing {required.name}")

    try:
        raw = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CaseError(f"{directory.name}: meta.yaml is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise CaseError(f"{directory.name}: meta.yaml must be a mapping")

    try:
        meta = CaseMeta.model_validate(_normalise(raw))
    except ValueError as exc:
        raise CaseError(f"{directory.name}: {exc}") from exc

    if meta.id != directory.name:
        # The id is what appears in every results row. A mismatch means a row nobody can
        # trace back to a directory.
        raise CaseError(
            f"{directory.name}: meta.yaml declares id {meta.id!r}, which is not the "
            "directory name"
        )

    source = source_path.read_text(encoding="utf-8")
    try:
        ast.parse(source)
    except SyntaxError as exc:
        raise CaseError(
            f"{meta.id}: before.py does not parse ({exc.msg} at line {exc.lineno}). The "
            "orchestrator escalates an unparseable input before the Coder runs, so every "
            "arm would score identically on this case whatever it was meant to test."
        ) from exc

    notes = notes_path.read_text(encoding="utf-8").strip()
    if not notes:
        raise CaseError(
            f"{meta.id}: notes.md is empty. docs/07 § Provenance: it forces you to justify "
            "the case is realistic, and it records whether it came from a public source."
        )

    _check_locators(meta, source)

    test_path = directory / "test_case.py"
    return EvalCase(
        meta=meta,
        directory=directory,
        source=source,
        notes=notes,
        test_source=test_path.read_text(encoding="utf-8") if test_path.is_file() else None,
    )


def _normalise(raw: dict[str, Any]) -> dict[str, Any]:
    """YAML gives lists where the schema wants tuples; nothing else is massaged."""
    out = dict(raw)
    for issue in out.get("known_issues") or []:
        locator = issue.get("locator")
        if isinstance(locator, dict) and isinstance(locator.get("value"), list):
            locator["value"] = tuple(locator["value"])
    return out


def _check_locators(meta: CaseMeta, source: str) -> None:
    """Every locator must be able to match something in the file it points into.

    A `line_range` past the end of `before.py` is the failure this exists for: it is
    invisible on inspection, it can never match, and it shows up in the results as every arm
    missing an issue that was never findable.
    """
    lines = source.splitlines()
    total = len(lines)
    for known in meta.known_issues:
        if known.locator.kind != "line_range":
            continue
        start, end = known.locator.value  # type: ignore[misc]
        if end > total:
            raise CaseError(
                f"{meta.id}: known issue {known.key!r} points at lines {start}-{end}, but "
                f"before.py has {total}. Nothing could ever match it."
            )
        # Catches the off-by-one that lands on a blank line or a comment. It cannot catch
        # one that lands on the wrong *statement* -- only a human reading `notes.md` can --
        # which is why `located_lines` exists and why the suite pins what each one points at.
        body = [
            line for line in lines[start - 1 : end]
            if line.strip() and not line.strip().startswith("#")
        ]
        if not body:
            raise CaseError(
                f"{meta.id}: known issue {known.key!r} points at lines {start}-{end}, which "
                "are blank or comments. A locator has to point at code."
            )
        actual = " ".join(line.strip() for line in lines[start - 1 : end])
        wanted = " ".join((known.locator.anchor or "").split())
        if actual != wanted:
            raise CaseError(
                f"{meta.id}: known issue {known.key!r} points at lines {start}-{end}, which "
                f"contain {actual!r}, but its anchor says {wanted!r}. Either the line "
                "numbers are off or before.py has shifted under them."
            )


def load_suite(root: Path) -> list[EvalCase]:
    """Every case under `root`, sorted by id. Raises on the first malformed one."""
    if not root.is_dir():
        raise CaseError(f"no case directory at {root}")
    cases = [
        load_case(child)
        for child in sorted(root.iterdir())
        if child.is_dir() and (child / "meta.yaml").is_file()
    ]
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise CaseError(f"duplicate case ids under {root}")
    return cases


# -- the shape of the whole set -----------------------------------------------------------

#: docs/07 § Composition, and the cut line's reduced set (docs/09: "24 -> 16 cases (5 dev /
#: 11 held-out), keeping **all 3 conflicting and all 6 canary**"). Stated as the full target;
#: `composition_gaps` reports the distance to it rather than asserting, because the set is
#: built up over days and a hard assertion would mean a red suite for a week.
TARGET_COMPOSITION: dict[str, int] = {
    "security_only": 6,
    "performance_only": 5,
    "both_independent": 4,
    "conflicting": 3,
    "canary_clean": 3,
    "canary_seeded_bad": 3,
}

#: The categories the cut line says never to trim. A set missing one of these cannot answer
#: the questions the eval exists for.
NEVER_CUT = ("conflicting", "canary_clean", "canary_seeded_bad")


def composition_gaps(cases: list[EvalCase]) -> dict[str, int]:
    """`{category: how many more are needed}`, empty when the target is met."""
    have: dict[str, int] = dict.fromkeys(TARGET_COMPOSITION, 0)
    for case in cases:
        have[case.meta.category] = have.get(case.meta.category, 0) + 1
    return {
        category: want - have.get(category, 0)
        for category, want in TARGET_COMPOSITION.items()
        if have.get(category, 0) < want
    }
