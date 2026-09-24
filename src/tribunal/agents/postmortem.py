"""The Postmortem: writes the run up, from the trace rather than from the conversation.

## Why it reads the trace

docs/03-agents.md § 3.5 makes the input "the full trace (compacted ... **not raw prompts**)",
and the consequence it names is the useful one: "it can be re-run on an old trace for free in
replay mode -- useful for iterating on report wording without re-running the debate". That is
`tribunal postmortem <trace>`, and it works because this agent has no dependency on the run
that produced the events.

## What it is not allowed to produce

docs/03-agents.md gives its output as "a `Report` (markdown body + structured fields)". Taken
literally that would put a second `Report`-builder next to `trace/report.build`, and criterion
S6 -- the report replays byte-identically -- rests on there being exactly one. So the agent
emits a `PostmortemNote`: the *content*, which goes into the trace like any other agent output,
and `trace/report.render_narrative` lays it out. `build` then picks the narrative up from the
trace on both the live and the replay path, and S6 survives intact.

The layout being ours rather than the model's is also what makes docs/03's structural promise
-- "the final section is always **what I would not trust**" -- true on every run instead of
true whenever the model remembers.
"""

from __future__ import annotations

from tribunal.agents.base import Agent
from tribunal.agents.bundle import PostmortemBundle
from tribunal.contracts import PostmortemNote


class Postmortem(Agent[PostmortemNote]):
    role = "postmortem"
    output_model = PostmortemNote

    def render_user(self, bundle: PostmortemBundle) -> str:
        return bundle.render()

    def post_validate(self, value: PostmortemNote, bundle: PostmortemBundle) -> None:
        from tribunal.agents.validation import validate_postmortem

        validate_postmortem(value, bundle)

    def metrics(self, value: PostmortemNote, bundle: PostmortemBundle) -> dict[str, object]:
        return {
            "rounds_described": len(value.rounds),
            "caveats": len(value.what_i_would_not_trust),
            "checks_for_a_human": len(value.human_should_check),
            "named_a_disagreement": value.disagreement is not None,
        }
