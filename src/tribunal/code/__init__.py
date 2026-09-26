"""`tribunal code`: the interactive terminal agent.

Everything else in this project is a *pipeline* -- you hand it a file and it hands you a
verdict. This package is the other shape: a prompt, a transcript, and a loop that reads,
edits and runs things until the work is done, which is what a developer actually sits in
front of. The two are not rivals. The coding agent has the tribunal as one of its tools, so
"write the fix" and "have two adversarial critics try to tear the fix apart" are one session
rather than two commands.

It is built on the existing LLM layer rather than beside it: one structured call per step,
through `LLMClient`, which means cassettes, repair retries, backoff, four providers and cost
accounting all already work here and none of it is reimplemented.
"""

from __future__ import annotations

from tribunal.code.actions import REQUIRED_FIELDS, Step, ToolName
from tribunal.code.approval import Approval, ApprovalMode, Approver
from tribunal.code.session import CodeSession, SessionLimit, Turn
from tribunal.code.tools import Observation, Workspace, execute

__all__ = [
    "REQUIRED_FIELDS",
    "Approval",
    "ApprovalMode",
    "Approver",
    "CodeSession",
    "Observation",
    "SessionLimit",
    "Step",
    "ToolName",
    "Turn",
    "Workspace",
    "execute",
]
