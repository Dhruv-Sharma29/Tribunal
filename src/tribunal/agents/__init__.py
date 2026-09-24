"""tribunal agents. Each is a versioned prompt, an output schema, and a model setting."""

from tribunal.agents.arbiter import Arbiter, ConflictAffirmer
from tribunal.agents.base import Agent, AgentRun, Critic
from tribunal.agents.bundle import (
    AffirmationBundle,
    ArbiterBundle,
    CoderBundle,
    CritiqueBundle,
    PostmortemBundle,
    RoundDigest,
)
from tribunal.agents.coder import Coder, CoderAttempt, CoderResult
from tribunal.agents.postmortem import Postmortem
from tribunal.agents.profiler import Profiler
from tribunal.agents.redteam import RedTeam

__all__ = [
    "AffirmationBundle",
    "Agent",
    "AgentRun",
    "Arbiter",
    "ArbiterBundle",
    "Coder",
    "CoderAttempt",
    "CoderBundle",
    "CoderResult",
    "ConflictAffirmer",
    "Critic",
    "CritiqueBundle",
    "Postmortem",
    "PostmortemBundle",
    "Profiler",
    "RedTeam",
    "RoundDigest",
]
