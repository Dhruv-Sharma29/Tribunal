"""Reading a trace back, and refusing to misinterpret one.

Two checks that exist because the alternative is silent nonsense:

* **Schema major.** docs/02-contracts.md § Schema evolution: "the viewer refuses unknown majors
  rather than rendering garbage". Same rule here -- a trace written by a future schema is
  rejected with its version named, not partially parsed.
* **Gap-free `seq`.** The writer assigns sequence numbers and nothing else does, so a gap means
  events were *lost*. Reported as a warning rather than an exception: a truncated trace is
  still worth reading, and a run killed mid-flight is exactly when you want to read one.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from tribunal.contracts import SCHEMA_VERSION, TraceEvent


class TraceError(ValueError):
    """The file is not a readable trace."""


class UnsupportedSchema(TraceError):
    """Written by a schema major this build does not understand."""


def _major(version: str) -> int:
    return int(version.split(".")[0])


@dataclass
class Trace:
    """A parsed trace, plus whatever was wrong with it."""

    events: list[TraceEvent]
    warnings: list[str] = field(default_factory=list)

    @property
    def header(self) -> TraceEvent:
        return self.events[0]

    @property
    def run_id(self) -> str:
        return self.header.run_id

    def of_kind(self, *kinds: str) -> list[TraceEvent]:
        return [e for e in self.events if e.kind in kinds]

    def by_actor(self, actor: str) -> list[TraceEvent]:
        return [e for e in self.events if e.actor == actor]


def parse(lines: Iterator[str], strict: bool = False) -> Trace:
    """Parse JSONL into events, validating the header and the sequence.

    `strict` turns warnings into errors, for the tests that assert a freshly written trace is
    perfect.
    """
    events: list[TraceEvent] = []
    warnings: list[str] = []

    for number, line in enumerate(lines, start=1):
        text = line.strip()
        if not text:
            continue
        try:
            events.append(TraceEvent.model_validate(json.loads(text)))
        except (ValueError, TypeError) as exc:
            raise TraceError(f"line {number} is not a valid trace event: {exc}") from exc

    if not events:
        raise TraceError("empty trace: not even a run_start header")
    header = events[0]
    if header.kind != "run_start":
        raise TraceError(
            f"the first event must be run_start, got {header.kind!r}. A trace is only "
            "self-describing if its header is first."
        )

    written_with = header.payload.get("schema_version", "0.0")
    if _major(written_with) > _major(SCHEMA_VERSION):
        raise UnsupportedSchema(
            f"trace was written with schema {written_with}, this build understands "
            f"{SCHEMA_VERSION}. Refusing to render it rather than showing you garbage."
        )
    if written_with != SCHEMA_VERSION:
        warnings.append(f"schema {written_with} differs from this build's {SCHEMA_VERSION}")

    expected = 0
    for event in events:
        if event.seq != expected:
            warnings.append(
                f"sequence gap: expected seq {expected}, found {event.seq} — "
                f"{event.seq - expected} event(s) lost"
            )
            expected = event.seq
        expected += 1

    if {e.run_id for e in events} != {header.run_id}:
        warnings.append("the file mixes events from more than one run_id")
    if not any(e.kind == "run_end" for e in events):
        warnings.append("no run_end: the run was killed or is still in flight")

    if strict and warnings:
        raise TraceError("; ".join(warnings))
    return Trace(events=events, warnings=warnings)


def read(path: Path, strict: bool = False) -> Trace:
    with path.open(encoding="utf-8") as handle:
        return parse(handle, strict=strict)
