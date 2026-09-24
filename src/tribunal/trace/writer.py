"""Append-only JSONL trace writer, with a subscriber hook for the live CLI renderer.

docs/06-observability.md principle 2: one JSON object per line, append-only. Crash-safe,
greppable, streamable, diffable between runs, and readable with `jq` when the viewer breaks.
Each line is flushed as it is written, so a run killed halfway still leaves an interpretable
trace up to the point it died -- which is when you most want one.

Principle 4 asks for "structured, never `print`", with the human-facing CLI progress as *a
separate renderer over the same event stream, not a second code path*. That is what
`subscribe` provides: the writer owns the single stream, and the `rich` progress renderer is a
subscriber. The doc suggests `structlog` for this; a subscriber list does the same job for
events that are already Pydantic models, without the dependency, and the property that matters
-- one stream, many renderers -- is preserved either way.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from tribunal.contracts import TraceEvent

Subscriber = Callable[[TraceEvent], None]


class TraceWriter:
    """Owns the sequence counter and the file handle.

    `seq` is monotonic and gap-free by construction -- it is assigned here and nowhere else.
    That is what lets `reader.py` treat a gap as evidence that events were lost rather than as
    a numbering convention it has to guess at.
    """

    def __init__(self, run_id: str, path: Path | None = None, keep_events: bool = True) -> None:
        self.run_id = run_id
        self.path = path
        self._handle: TextIO | None = None
        self._seq = 0
        self._subscribers: list[Subscriber] = []
        #: Kept in memory so the report can be built without re-reading the file. `replay`
        #: builds the same report from the file, and a test asserts the two agree.
        self.events: list[TraceEvent] = [] if keep_events else []
        self._keep = keep_events

    # -- lifecycle ----------------------------------------------------------------------

    def open(self) -> TraceWriter:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a", encoding="utf-8")
        return self

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> TraceWriter:
        return self.open()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- writing ------------------------------------------------------------------------

    @property
    def next_seq(self) -> int:
        return self._seq

    def emit(self, build: Callable[..., TraceEvent], **kwargs: object) -> TraceEvent:
        """Build an event with the next sequence number and write it.

        Takes the builder rather than a built event so the counter cannot be read, used and
        then not incremented -- the one way a gap-free counter goes wrong.
        """
        event = build(self._seq, self.run_id, **kwargs)  # type: ignore[arg-type]
        self._seq += 1
        self._write(event)
        return event

    def _write(self, event: TraceEvent) -> None:
        if self._keep:
            self.events.append(event)
        if self._handle is not None:
            self._handle.write(event.model_dump_json(exclude_none=False) + "\n")
            # Flushed per line: a killed run still leaves an interpretable trace.
            self._handle.flush()
        for subscriber in self._subscribers:
            subscriber(event)

    # -- renderers ----------------------------------------------------------------------

    def subscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.append(subscriber)


@contextmanager
def trace_file(run_id: str, directory: Path) -> Iterator[TraceWriter]:
    """`traces/<run_id>.jsonl`, the filename docs/06 § Correlation specifies."""
    writer = TraceWriter(run_id, directory / f"{run_id}.jsonl")
    try:
        yield writer.open()
    finally:
        writer.close()


#: The one file in a trace directory that is not a trace. Named here because three
#: separate consumers had each re-derived "skip summary.jsonl" by literal, and a fourth
#: that forgot would read the aggregate row as a run and report the directory as corrupt.
SUMMARY_NAME = "summary.jsonl"


def traces_in(directory: Path) -> list[Path]:
    """Every per-run trace in `directory`, sorted. Excludes the aggregate summary."""
    if not directory.is_dir():
        return []
    return [p for p in sorted(directory.glob("*.jsonl")) if p.name != SUMMARY_NAME]


def append_summary(directory: Path, row: dict) -> Path:
    """One line per run in `traces/summary.jsonl`.

    docs/06 § Metrics worth aggregating: this is what turns single runs into a dataset, and
    the reason threshold tuning in Phase 5 can be done against real pressure trajectories
    rather than intuition.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / SUMMARY_NAME
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    return path
