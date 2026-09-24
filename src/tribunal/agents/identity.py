"""Canonical `Issue.id` assignment.

Models do not produce stable identifiers. Observed on the first live run of both critics:

* the Red-team reused the *grounding finding id* as the issue id, so two distinct issues citing
  the same finding would collide;
* the Profiler emitted `"1"` and `"2"`.

Both are schema-valid and both break the policy layer, because `Issue.id` is load-bearing in
three places that all assume it is stable and content-derived:

1. **Dismissal.** An issue the Arbiter agreed was a false positive must stay dismissed for the
   rest of the run (docs/04-arbitration.md § Pressure, "dismissal-aware").
2. **Conflict detector 1.** An irreconcilable trade-off is recognised by seeing *the same
   `Issue.id`* return after being fixed. With `"1"`/`"2"` every round looks like a repeat.
3. **The regression metric.** `introduced_by_patch` is only meaningful if an issue can be
   tracked across rounds.

docs/04-arbitration.md § Failure-mode checklist names this exactly: "An `Issue.id` collides
across rounds with different content -> id derivation must include enough context; test it."

So ids are **assigned by us**, not accepted from the model, using
`contracts.canonical_issue_id(dimension, rule, ref)` over the issue's primary anchor. The
model's own id is kept in the returned mapping so a trace can show what it said.
"""

from __future__ import annotations

import re

from tribunal.contracts import Critique, Dimension, EvidenceKind, Issue, canonical_issue_id

_SLUG = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    return _SLUG.sub("-", text.lower()).strip("-")[:60]


def _primary_anchor(issue: Issue, finding_rule: dict[str, str]) -> tuple[str, str]:
    """The `(rule, ref)` pair that identifies what this issue is *about*.

    Prefers grounded evidence, and among grounded evidence prefers a tool finding: a finding's
    rule code is the most stable description of a defect available, and it survives the line
    numbers shifting as later rounds patch the file. A `code_span` anchor is inherently less
    stable, which is a real limitation of tracking novel issues across rounds -- stated here
    rather than hidden.
    """
    order = (
        EvidenceKind.TOOL_FINDING,
        EvidenceKind.MEASUREMENT,
        EvidenceKind.TEST_FAILURE,
        EvidenceKind.CODE_SPAN,
        EvidenceKind.REASONING,
    )
    for kind in order:
        for evidence in issue.evidence:
            if evidence.kind is not kind:
                continue
            if kind is EvidenceKind.TOOL_FINDING:
                rule = finding_rule.get(evidence.ref, "unknown-finding")
                return rule, evidence.ref
            return kind.value, evidence.ref
    return "ungrounded", slug(issue.title)  # unreachable: evidence has min_length=1


def canonicalise_issue_ids(
    critique: Critique, finding_rule: dict[str, str] | None = None
) -> list[tuple[str, str]]:
    """Rewrite every `Issue.id` in place. Returns `[(model_id, canonical_id)]` in issue order.

    A list of pairs rather than a `{model_id: canonical_id}` dict, because the dict is lossy in
    exactly the case this function exists for: a model that emits `"1"` twice, or reuses one
    finding id for two issues, would collapse two entries into one and the trace would lose a
    rename. Observed live, so not hypothetical.

    Two issues that share an anchor -- the same finding cited by genuinely different concerns --
    are separated by folding the title into the derivation. Without that, the second would
    silently overwrite the first everywhere an id is used as a key.
    """
    rules = finding_rule or {}
    mapping: list[tuple[str, str]] = []
    assigned: set[str] = set()

    for issue in critique.issues:
        rule, ref = _primary_anchor(issue, rules)
        candidate = canonical_issue_id(issue.dimension, rule, ref)
        if candidate in assigned:
            candidate = canonical_issue_id(issue.dimension, f"{rule}|{slug(issue.title)}", ref)
        # Still colliding means two issues with the same anchor *and* the same title, which is
        # a duplicate the critic should not have emitted. Suffix rather than lose one.
        suffix = 2
        base = candidate
        while candidate in assigned:
            candidate = canonical_issue_id(issue.dimension, f"{rule}|{suffix}", ref)
            suffix += 1
            if suffix > 16:  # pragma: no cover - defensive
                candidate = f"{base}-{suffix}"
                break
        mapping.append((issue.id, candidate))
        assigned.add(candidate)
        issue.id = candidate
    return mapping


def finding_rules(report) -> dict[str, str]:
    """`{finding_id: "tool:rule"}`, the stable description an issue id is derived from."""
    return {f.id: f"{f.tool}:{f.rule}" for f in report.findings}


def prefix_for(dimension: Dimension) -> str:
    from tribunal.contracts import DIMENSION_PREFIX

    return DIMENSION_PREFIX[dimension]
