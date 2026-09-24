"""Record/replay store, keyed by request hash.

This is what makes docs/09-roadmap.md's "the test suite makes **zero** API calls" true across
four providers instead of one, and it is what `tribunal replay` rests on (criterion S6).

The key includes the **provider** and the **adapted schema**, not just the prompt. Both matter:

* Without the provider, a cassette recorded against Anthropic could be served to a Gemini
  request, and the test suite would claim to cover a provider it never exercised.
* Without the adapted schema, changing a dialect rule (inlining `$defs`, forcing `required`)
  would silently reuse responses recorded against the old schema -- so a schema regression
  would be invisible in exactly the tests meant to catch it.

`prompt_version` is *not* in the key; it is recorded alongside. A prompt edit should invalidate
the cassette through the `system`/`user` text it actually changed, so a version bump with no
text change correctly keeps its recording.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from tribunal.contracts import Usage
from tribunal.llm.base import LLMRequest, LLMResponse, StructureMode


class CassetteMiss(KeyError):
    """No recording for this request.

    In `replay` mode this is fatal and says so loudly: silently falling through to the network
    would turn "zero API calls" into "zero API calls until someone edits a prompt".
    """


def request_hash(request: LLMRequest) -> str:
    payload = {
        "provider": request.provider.value,
        "model": request.model,
        "system": request.system,
        "user": request.user,
        "schema": request.schema_,
        "schema_name": request.schema_name,
        "max_tokens": request.max_tokens,
        "effort": request.effort,
        "cache_system": request.cache_system,
    }
    # sort_keys is load-bearing: an unsorted dump makes the hash depend on dict insertion
    # order, which is the same class of silent-invalidator bug as a timestamp in a prompt.
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class CassetteStore:
    directory: Path

    def path_for(self, request: LLMRequest) -> Path:
        # Provider in the filename as well as the hash, so a directory listing is readable
        # and `git log` shows which provider a recording belongs to.
        return self.directory / request.provider.value / f"{request_hash(request)}.json"

    def has(self, request: LLMRequest) -> bool:
        return self.path_for(request).is_file()

    def load(self, request: LLMRequest) -> LLMResponse:
        path = self.path_for(request)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CassetteMiss(
                f"no cassette for {request.provider.value}/{request.model} "
                f"({request_hash(request)}). Re-record with "
                f"TRIBUNAL_LLM__MODE=record, or check whether a prompt or schema changed."
            ) from exc
        response = raw["response"]
        return LLMResponse(
            data=response["data"],
            raw_text=response["raw_text"],
            structure=StructureMode(response["structure"]),
            usage=Usage.model_validate(response["usage"]),
            stop_reason=response["stop_reason"],
            reasoning=response["reasoning"],
            replayed=True,
        )

    def save(self, request: LLMRequest, response: LLMResponse) -> Path:
        path = self.path_for(request)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "hash": request_hash(request),
            # The request is stored for human readability and for debugging a miss. It is not
            # read back on replay -- the hash is the contract.
            "request": {
                "provider": request.provider.value,
                "model": request.model,
                "schema_name": request.schema_name,
                "effort": request.effort,
                "max_tokens": request.max_tokens,
                "system": request.system,
                "user": request.user,
            },
            "response": {
                "data": response.data,
                "raw_text": response.raw_text,
                "structure": response.structure.value,
                "usage": response.usage.model_dump(mode="json"),
                "stop_reason": response.stop_reason,
                "reasoning": response.reasoning,
            },
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def count(self) -> dict[str, int]:
        """Recordings per provider, for `tribunal providers`."""
        if not self.directory.is_dir():
            return {}
        return {
            sub.name: len(list(sub.glob("*.json")))
            for sub in sorted(self.directory.iterdir())
            if sub.is_dir()
        }
