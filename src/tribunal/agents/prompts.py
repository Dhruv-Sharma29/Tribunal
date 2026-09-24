"""Prompt templates with frontmatter-declared versions.

`prompt_version` goes into every trace event and every eval report header. Without it you
cannot tell whether a score moved because of a prompt change or a code change, which makes
[07](../../docs/07-evaluation.md) meaningless -- and it cannot be retrofitted onto traces that
already exist (docs/09-roadmap.md § Hard-won ordering advice).

The frontmatter is parsed with a deliberately small hand-rolled reader rather than a YAML
dependency. The fields are a flat `key: value` map by construction, and a prompt file is not a
place where arbitrary YAML evaluation should be possible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "llm" / "prompts"

FRONTMATTER = re.compile(r"\A---\s*\n(?P<body>.*?)\n---\s*\n(?P<text>.*)\Z", re.DOTALL)
#: Continuation lines in a folded value start with whitespace.
KEY_VALUE = re.compile(r"^(?P<key>[a-z_][a-z0-9_]*):\s*(?P<value>.*)$")


class PromptError(ValueError):
    """A prompt file is missing, malformed, or missing a required field."""


@dataclass(frozen=True)
class Prompt:
    role: str
    version: str
    text: str
    model_default: str
    effort_default: str
    dimension: str | None
    changed: str
    note: str
    path: Path

    @property
    def stamp(self) -> str:
        """The value that goes into the trace, e.g. `redteam/v1`."""
        return f"{self.role}/{self.version}"


def _parse_frontmatter(raw: str, path: Path) -> tuple[dict[str, str], str]:
    match = FRONTMATTER.match(raw)
    if match is None:
        raise PromptError(
            f"{path.name}: no frontmatter block. Every prompt must open with a `---` fenced "
            "block declaring at least `role` and `version`."
        )
    fields: dict[str, str] = {}
    key: str | None = None
    for line in match.group("body").splitlines():
        if not line.strip():
            continue
        if line[:1].isspace() and key is not None:
            # A folded continuation line, so a long `note` can wrap.
            fields[key] = f"{fields[key]} {line.strip()}".strip()
            continue
        kv = KEY_VALUE.match(line)
        if kv is None:
            raise PromptError(f"{path.name}: cannot parse frontmatter line {line!r}")
        key = kv.group("key")
        fields[key] = kv.group("value").strip().strip('"')
    return fields, match.group("text").strip()


@cache
def load(role: str, directory: Path | None = None) -> Prompt:
    """Load and cache a role's prompt.

    Cached because the rendered system prompt is the cacheable request prefix: re-reading the
    file per call would be harmless, but re-deriving it invites someone to interpolate
    something volatile into it later.
    """
    base = directory or PROMPT_DIR
    path = base / f"{role}.md"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        available = sorted(p.stem for p in base.glob("*.md")) if base.is_dir() else []
        raise PromptError(
            f"no prompt for role {role!r} at {path}; available: {available}"
        ) from exc

    fields, text = _parse_frontmatter(raw, path)
    for required in ("role", "version"):
        if required not in fields:
            raise PromptError(f"{path.name}: frontmatter is missing `{required}`")
    if fields["role"] != role:
        raise PromptError(
            f"{path.name}: frontmatter declares role {fields['role']!r} but the file is named "
            f"{role}.md. A mismatch here mis-stamps every trace event."
        )
    if not text:
        raise PromptError(f"{path.name}: frontmatter present but the prompt body is empty")

    return Prompt(
        role=fields["role"],
        version=fields["version"],
        text=text,
        model_default=fields.get("model_default", "claude-opus-5"),
        effort_default=fields.get("effort_default", "high"),
        dimension=fields.get("dimension"),
        changed=fields.get("changed", "unknown"),
        note=fields.get("note", ""),
        path=path,
    )


def available_roles(directory: Path | None = None) -> list[str]:
    base = directory or PROMPT_DIR
    return sorted(p.stem for p in base.glob("*.md")) if base.is_dir() else []
