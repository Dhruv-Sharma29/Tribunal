"""The single-file trace viewer.

docs/06-observability.md § The viewer: "A single self-contained HTML file written next to the
trace. No server, no build step, no dependencies ... It must work when double-clicked from a
file manager, because that's how a reviewer will open it."

Everything follows from that one sentence. The CSS and JS are inlined rather than linked, the
trace is embedded as a `<script type="application/json">` blob rather than fetched (a `fetch`
of a sibling file is blocked by CORS under `file://`, which is the failure that makes people
reach for a dev server), and there is no CDN — a webfont or a charting library would make the
demo depend on a network round-trip at exactly the wrong moment.

## Why the data is escaped rather than trusted

The blob carries model-written source, diffs and critique prose. A trace containing the four
characters `</script>` would otherwise close the tag early and turn everything after it into
markup — which, for a file people email each other, is a code-execution path rather than a
rendering bug. `_embed` escapes `<`, `>` and `&` to their `\\u00XX` forms: still valid JSON,
inert inside a script tag, and it cannot be defeated by clever casing or whitespace the way a
`</script>`-specific replacement can.

The JS side holds up the other end: it never assigns to `innerHTML`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tribunal import __version__
from tribunal.contracts import SCHEMA_VERSION
from tribunal.trace.reader import Trace

ASSETS = Path(__file__).resolve().parent / "assets"

#: The shell's placeholders. Matched in one pass so a substitution cannot introduce
#: another placeholder.
PLACEHOLDER = re.compile(r"__(?:TITLE|CSS|WARNINGS|FOOTER|DATA|JS)__")


def _asset(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def _embed(events: list[dict]) -> str:
    """Serialise the events for a `<script type="application/json">` blob.

    `ensure_ascii=False` keeps the file readable and small; the three escaped characters are
    the only ones that can break out of the tag.
    """
    blob = json.dumps(events, ensure_ascii=False, separators=(",", ":"))
    return blob.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render(trace: Trace, title: str | None = None) -> str:
    """Build the whole viewer as one string of HTML."""
    events = [event.model_dump(mode="json") for event in trace.events]
    header = trace.events[0].payload if trace.events else {}
    name = title or header.get("input_file") or trace.run_id or "trace"

    warnings = ""
    if trace.warnings:
        # A truncated trace is still worth reading (docs/06), but the reader must be told --
        # a gap in `seq` means events were lost, and a viewer that hid that would be
        # presenting an incomplete run as a complete one.
        items = "".join(f"<div class=\"warn\">{_text(w)}</div>" for w in trace.warnings)
        warnings = items

    footer = (
        f"tribunal {__version__} · schema {SCHEMA_VERSION} · "
        f"{len(events)} events · rendered offline, no network required"
    )

    fills = {
        "__TITLE__": _text(f"tribunal · {name}"),
        "__CSS__": _asset("viewer.css"),
        "__WARNINGS__": warnings,
        "__FOOTER__": _text(footer),
        "__DATA__": _embed(events),
        "__JS__": _asset("viewer.js"),
    }
    # One pass, so nothing substituted in is scanned for the next placeholder. Chained
    # `.replace()` calls would let a trace reviewing a file that happens to contain the text
    # `__JS__` have the viewer script spliced into the JSON blob -- obscure, but the kind of
    # thing that only ever shows up in a demo.
    return PLACEHOLDER.sub(lambda m: fills[m.group(0)], _asset("shell.html"))


def _text(value: str) -> str:
    """Escape a string for HTML text content. Used only for the few server-rendered bits."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def write(trace: Trace, destination: Path, title: str | None = None) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render(trace, title), encoding="utf-8")
    return destination
