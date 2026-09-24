"""The Arbiter: writes up a decision it cannot change, and answers detector 2's one question.

## Two calls, on opposite sides of the decision

docs/03-agents.md § 3.4 gives the Arbiter three jobs — synthesise on `reject`, justify on
`tradeoff`, adjudicate the Coder's pushback — and all three happen *after* `policy.decide`.
docs/04-arbitration.md § Conflict detection gives it a fourth that happens *before*: detector 2
only fires when the Arbiter affirms that two same-span issues really are opposed.

That ordering cannot be collapsed into one call. The affirmation is an **input** to the
decision, and the note is written **about** the decision; a single call would have to be both
upstream and downstream of the same function. So there are two prompts, two schemas and two
config entries:

| Call | When | Output | Effort |
|---|---|---|---|
| `affirms()` | before `decide`, per same-span pair | `ConflictAffirmation` | `medium` |
| `run()` | after `decide`, on reject and tradeoff | `ArbiterNote` | `xhigh` |

The affirmation runs at a lower effort on purpose. It is a yes/no classification over two
issues and a dozen lines of source, it fires more often than the note does, and giving it the
synthesis budget would make the cheapest question in the system the most expensive call.

## Why the affirmation cannot dismiss, and the note cannot decide

Both agents are writers with exactly one lever each, and both levers are checked rather than
trusted. `decision_echo` must equal the verdict it was handed. `dismissed` may only name ids
the Coder actually pushed back on — otherwise the Arbiter could drop the pressure it was told
not to decide about, which is the decision re-entering through the side door. `validation.py`
enforces both, and a violation is one repair retry, not a warning.
"""

from __future__ import annotations

from tribunal.agents.base import Agent, AgentRun
from tribunal.agents.bundle import AffirmationBundle, ArbiterBundle
from tribunal.config import Settings
from tribunal.contracts import ArbiterNote, ConflictAffirmation, Issue
from tribunal.llm.client import LLMClient


class ConflictAffirmer(Agent[ConflictAffirmation]):
    """Detector 2's second half: are these two remedies mutually exclusive?

    Its own role, and so its own `prompt_version`, because the eval has to be able to
    attribute a change in the trade-off rate to the prompt that produced it. Folding this
    into `arbiter/vN` would make a wording change to the synthesis look like a change in
    conflict sensitivity.
    """

    role = "arbiter_affirm"
    output_model = ConflictAffirmation

    def render_user(self, bundle: AffirmationBundle) -> str:
        return bundle.render()

    def post_validate(self, value: ConflictAffirmation, bundle: AffirmationBundle) -> None:
        from tribunal.agents.validation import validate_affirmation

        validate_affirmation(value, bundle)

    def metrics(
        self, value: ConflictAffirmation, bundle: AffirmationBundle
    ) -> dict[str, object]:
        return {
            "opposing": value.opposing,
            "left_issue": value.left_issue,
            "right_issue": value.right_issue,
        }


class Arbiter(Agent[ArbiterNote]):
    """Writes the note, and owns the affirmer so the orchestrator has one seat to fill."""

    role = "arbiter"
    output_model = ArbiterNote

    def __init__(self, settings: Settings, client: LLMClient | None = None) -> None:
        super().__init__(settings, client)
        # Shares the client, so both calls land in one cost total and one cassette dir.
        self.affirmer = ConflictAffirmer(settings, self.client)

    def render_user(self, bundle: ArbiterBundle) -> str:
        return bundle.render()

    def post_validate(self, value: ArbiterNote, bundle: ArbiterBundle) -> None:
        from tribunal.agents.validation import validate_arbiter_note

        validate_arbiter_note(value, bundle)

    def metrics(self, value: ArbiterNote, bundle: ArbiterBundle) -> dict[str, object]:
        """`synthesised_by` mirrors the templated path's marker, so a trace reader can tell
        which produced a consolidation without knowing whether an Arbiter was configured."""
        return {
            "synthesised_by": "arbiter",
            "dismissed_count": len(value.dismissed),
            "pushbacks_seen": len(bundle.pushbacks()),
            "prioritised": len(value.priority_order),
        }

    async def affirms(
        self,
        left: Issue,
        right: Issue,
        round_: int,
        filename: str,
        source: str,
    ) -> AgentRun[ConflictAffirmation]:
        """Detector 2's question about one specific pair.

        Returns the whole run rather than the bool: the orchestrator writes it to the trace,
        and an affirmation that nobody can see afterwards is an LLM in the decision path with
        no record of what it said.
        """
        bundle = AffirmationBundle(
            round=round_, filename=filename, source=source, left=left, right=right
        )
        return await self.affirmer.run(bundle, round_)
