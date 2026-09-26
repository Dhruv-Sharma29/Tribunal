"""The coding agent's wire format: one tool call per turn, as a flat structured object.

## Why not native tool-calling

The obvious implementation is the provider's own tool-use API. This one does not use it, for
a reason that is specific to this codebase rather than a general preference: `llm/base.py`
has exactly one request shape -- system, user, a JSON schema, one answer -- and everything
valuable hangs off it. Cassettes are keyed by that request, `tribunal replay` depends on
them, the repair retry, the jittered backoff and the cost accounting all live in
`LLMClient`, and four providers already implement it. Adding a second, message-threaded,
tool-calling path would mean four more provider implementations and a second set of
everything above, with NIM and Gemini getting the weaker half.

So a step is a *structured output*, and the loop in `session.py` is the tool runner. The cost
is real and worth stating: one tool call per turn rather than several in parallel, and no
streaming. The benefit is that `tribunal code` works on every provider the tribunal already
speaks to, including a self-hosted NIM, and a session can be recorded and replayed with no
credential like everything else here.

## Why the fields are flat

`args: dict[str, Any]` is the natural shape and the one that cannot be used. OpenAI's strict
mode forces `additionalProperties: false` and requires every key to be listed (see
`llm/schema.py`), which makes an open dict either unrepresentable or unconstrained depending
on the provider -- the worst kind of difference, because it fails silently on the weaker one.
A flat object with a fixed key set adapts identically to all four dialects.

The consequence is that most fields are empty on most steps, and that "which fields does
this tool actually need" cannot be expressed in the schema either. `REQUIRED_FIELDS` is that
rule, enforced in a validator, which turns a malformed call into `LLMClient`'s ordinary
repair retry with the specific missing field named -- the same mechanism a malformed
critique gets, and no new failure path.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tribunal.llm.client import register_output_model


class ToolName(StrEnum):
    """Every tool. Read-only ones first; the four below `EDIT` change something or cost money.

    Kept small deliberately. A tool the model uses wrongly once per session is worse than a
    tool that does not exist, because the recovery costs a whole turn -- and `bash` already
    covers the long tail at the price of one approval prompt.
    """

    READ = "read"
    LIST = "list"
    GREP = "grep"
    GLOB = "glob"
    WRITE = "write"
    EDIT = "edit"
    BASH = "bash"
    #: Hand a Python file to the real adversarial loop: ground, propose, critique, decide.
    REVIEW = "review"
    #: Not a tool so much as the exit condition: the turn is over and this is the answer.
    DONE = "done"


#: Tools that change the working tree, spend money, or execute something. `approval.py` gates
#: exactly this set; everything else runs unprompted because a read cannot surprise anyone.
MUTATING = frozenset({ToolName.WRITE, ToolName.EDIT, ToolName.BASH, ToolName.REVIEW})

#: What each tool cannot work without. Checked in a validator rather than documented in the
#: prompt and hoped for.
REQUIRED_FIELDS: dict[ToolName, tuple[str, ...]] = {
    ToolName.READ: ("path",),
    ToolName.LIST: (),
    ToolName.GREP: ("pattern",),
    ToolName.GLOB: ("pattern",),
    ToolName.WRITE: ("path", "content"),
    ToolName.EDIT: ("path", "search"),
    ToolName.BASH: ("command",),
    ToolName.REVIEW: ("path",),
    ToolName.DONE: ("message",),
}


@register_output_model
class Step(BaseModel):
    """One turn of the loop: what the model is thinking, and the single thing it wants to do.

    Registered as an output model so the Anthropic provider can pass the class to
    `messages.parse` and get constrained decoding rather than a hopeful instruction.
    """

    model_config = ConfigDict(extra="forbid")

    #: One or two sentences, shown to the user above the tool line. This is the only channel
    #: the agent has for saying *why*, so it is not optional in practice even though an empty
    #: string validates -- a step with no thought reads as the tool doing something on its own.
    thought: str = Field(default="", max_length=2_000)
    tool: ToolName

    #: `read`, `write`, `edit`, `review`, and the subtree root for `list`/`grep`/`glob`.
    path: str = ""
    #: A regular expression for `grep`, a glob for `glob`.
    pattern: str = ""
    #: `edit`: literal text to find, which must occur exactly once. Not a pattern.
    search: str = ""
    #: `edit`: what replaces it. Empty means delete the matched text.
    replace: str = ""
    #: `write`: the whole new contents of the file.
    content: str = ""
    #: `bash`: the command line, run through the shell in the workspace root.
    command: str = ""
    #: `done`: the answer to the user.
    message: str = ""
    #: `read`: 1-based first line. 0 means the start of the file.
    start_line: int = Field(default=0, ge=0)
    #: `read`: how many lines. 0 means the configured default.
    max_lines: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _required_fields_are_present(self) -> Step:
        missing = [
            field
            for field in REQUIRED_FIELDS[self.tool]
            # `content` may legitimately be the empty string -- writing an empty file is a
            # real operation -- so emptiness is only a failure for the fields where it cannot
            # mean anything. `search` is one of those: the empty string matches everywhere.
            if not str(getattr(self, field)).strip()
            and not (self.tool is ToolName.WRITE and field == "content")
        ]
        if missing:
            raise ValueError(
                f"tool {self.tool.value!r} requires {', '.join(missing)}; "
                f"re-emit the step with {'them' if len(missing) > 1 else 'it'} filled in"
            )
        return self

    def describe(self) -> str:
        """A one-line rendering of the call, for the terminal and for the transcript.

        The same string in both places on purpose: what the user watched scroll past is
        exactly what the model is shown on the next turn, so a session is never explained
        two different ways.
        """
        if self.tool is ToolName.READ:
            span = f" from line {self.start_line}" if self.start_line else ""
            return f"read {self.path}{span}"
        if self.tool is ToolName.LIST:
            return f"list {self.path or '.'}"
        if self.tool is ToolName.GREP:
            return f"grep {self.pattern!r}" + (f" in {self.path}" if self.path else "")
        if self.tool is ToolName.GLOB:
            return f"glob {self.pattern}" + (f" in {self.path}" if self.path else "")
        if self.tool is ToolName.WRITE:
            return f"write {self.path} ({len(self.content.splitlines())} lines)"
        if self.tool is ToolName.EDIT:
            return f"edit {self.path} ({_first_line(self.search)})"
        if self.tool is ToolName.BASH:
            return f"bash {self.command}"
        if self.tool is ToolName.REVIEW:
            return f"review {self.path} (the full tribunal)"
        return "done"


def _first_line(text: str, limit: int = 48) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"
