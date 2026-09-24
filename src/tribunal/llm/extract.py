"""Recovering a JSON object from text that is not only a JSON object.

Needed because structured output is not universally enforceable:

* **NVIDIA Nemotron reasoning models put the chain of thought and the final answer in the
  same `content` field.** There is no separate `reasoning_content` to ignore -- the JSON we
  want is preceded by prose. Verified against NVIDIA's model documentation, which describes
  reasoning as "chain-of-thought before the final answer, visible in `content`".
* Any provider in JSON mode (rather than strict schema mode) may wrap its answer in a
  markdown fence or add a sentence of preamble.

The order is deliberate: try to parse the whole thing first, and only fall back to recovery.
A response that *was* clean JSON must be reported as such, because the difference between
`native_json` and `extracted` is exactly the signal the eval needs to attribute a rising
parse-retry rate to a provider rather than to prompt drift.
"""

from __future__ import annotations

import json
import re
from typing import Any

from tribunal.llm.base import ProviderBadResponse

#: Nemotron and several open reasoning models delimit the chain of thought this way. Matched
#: non-greedily and tolerantly of a missing closing tag, which happens when the response was
#: cut off by a token limit mid-thought.
THINK_BLOCK = re.compile(r"<think>.*?(?:</think>|\Z)", re.DOTALL | re.IGNORECASE)

FENCE = re.compile(r"```(?:json)?\s*(?P<body>.*?)```", re.DOTALL | re.IGNORECASE)


def split_reasoning(text: str) -> tuple[str, str | None]:
    """Separate inline `<think>` reasoning from the answer.

    The reasoning is returned rather than discarded: it goes into the trace as the response's
    `reasoning` field, which keeps the debate viewer honest about *why* a critic said what it
    said even on providers that do not expose a reasoning summary of their own.
    """
    blocks = THINK_BLOCK.findall(text)
    if not blocks:
        return text, None
    answer = THINK_BLOCK.sub("", text).strip()
    reasoning = "\n".join(
        b.replace("<think>", "").replace("</think>", "").strip() for b in blocks
    ).strip()
    return answer, reasoning or None


def find_json_object(text: str) -> str | None:
    """Return the first balanced top-level JSON object in `text`, or None.

    Brace counting has to respect string literals and escapes: a `"{"` inside an
    `Issue.explanation` would otherwise unbalance the count and truncate the object. A regex
    cannot do this, which is why this is a scanner.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None  # unbalanced: almost always a max_tokens truncation


def parse_json_payload(text: str) -> tuple[dict[str, Any], bool, str | None]:
    """Parse a provider's text into a JSON object.

    Returns `(data, needed_recovery, reasoning)`. `needed_recovery` is False only when the
    whole response body was already a JSON object, which is what lets the caller distinguish
    `native_json` from `extracted`.
    """
    stripped = text.strip()
    if not stripped:
        raise ProviderBadResponse("provider returned an empty response body")

    # 1. The clean case.
    try:
        data = json.loads(stripped)
    except ValueError:
        pass
    else:
        if isinstance(data, dict):
            return data, False, None
        raise ProviderBadResponse(
            f"expected a JSON object, got {type(data).__name__}: {stripped[:120]!r}"
        )

    # 2. Inline chain of thought.
    answer, reasoning = split_reasoning(stripped)

    # 3. A markdown fence around the answer.
    fenced = FENCE.search(answer)
    candidates = [fenced.group("body").strip()] if fenced else []
    # 4. Brace scan, as the last resort.
    scanned = find_json_object(answer)
    if scanned:
        candidates.append(scanned)

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data, True, reasoning

    detail = answer[:200].replace("\n", " ")
    if "{" in answer and find_json_object(answer) is None:
        raise ProviderBadResponse(
            "response contains an unterminated JSON object, which usually means it hit "
            f"max_tokens mid-object: ...{answer[-120:]!r}"
        )
    raise ProviderBadResponse(f"no JSON object found in response: {detail!r}")
