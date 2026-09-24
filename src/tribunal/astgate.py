"""Pre-execution AST scan.

Two jobs, and it is important to keep them distinct:

1. **Grounding.** Every hit becomes a `GroundingFinding` with `tool: "astgate"`. A hit does
   *not* block the run and is *not* an `Issue` -- whether it matters is the Red-team's call.
   That division of labour is the thing the whole system is built on.
2. **Execution refusal.** If a `FORBIDDEN_MODULES` import is present and execution was
   requested, `sandbox` refuses to run the file at all.

**This is a hygiene tripwire, not a security boundary.** It is trivially bypassed by
`getattr(__builtins__, "e" + "val")` or an `importlib` string, and it must be documented as
such (docs/05-execution-sandbox.md § Pre-execution static gate). The real boundary is the
container in Layer 3; this catches the realistic cases -- a careless model and an opportunistic
snippet -- and it is free.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Literal

from tribunal.contracts import GroundingFinding, finding_id

TOOL = "astgate"

#: Direct dynamic-execution builtins.
FORBIDDEN_CALLS = frozenset({"eval", "exec", "compile", "__import__"})

#: Imports that make the run either unsandboxable at Layer 2 (network) or capable of escaping
#: the resource limits (ctypes, multiprocessing) or of executing arbitrary bytes (pickle,
#: marshal). Present + execution requested => refuse.
FORBIDDEN_MODULES = frozenset(
    {
        "socket",
        "http",
        "urllib",
        "requests",
        "ftplib",
        "smtplib",
        "ctypes",
        "multiprocessing",
        "pickle",
        "marshal",
        "shutil",
    }
)

#: (module, attribute) pairs worth flagging. "*" matches any attribute of that module.
SUSPICIOUS: frozenset[tuple[str, str]] = frozenset(
    {
        ("subprocess", "*"),
        ("os", "system"),
        ("os", "popen"),
        ("os", "remove"),
        ("os", "unlink"),
        ("os", "rmdir"),
        ("os", "execv"),
        ("os", "spawnv"),
    }
)

#: Why network modules are called out separately: they are the difference between "this run is
#: contained" and "this run can exfiltrate or fetch stage two". Layer 2 cannot block network.
NETWORK_MODULES = frozenset({"socket", "http", "urllib", "requests", "ftplib", "smtplib"})

Kind = Literal["forbidden_call", "forbidden_import", "suspicious_call", "shell_true"]


@dataclass(frozen=True)
class GateHit:
    kind: Kind
    rule: str  # stable identifier, e.g. "forbidden-import:socket"
    target: str  # "eval" | "socket" | "os.system"
    line: int
    end_line: int
    message: str
    #: True when this hit alone justifies refusing to execute the file.
    blocks_execution: bool


class _Scanner(ast.NodeVisitor):
    """Resolves the small amount of aliasing needed to make the checks useful.

    `import subprocess as sp; sp.run(...)` and `from os import system; system(...)` are both
    common enough in model output that missing them would make the gate decorative.
    """

    def __init__(self) -> None:
        self.hits: list[GateHit] = []
        #: local binding -> module root, e.g. {"sp": "subprocess"}
        self.module_aliases: dict[str, str] = {}
        #: local binding -> (module root, attribute), e.g. {"system": ("os", "system")}
        self.name_imports: dict[str, tuple[str, str]] = {}

    # -- imports ------------------------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            self.module_aliases[alias.asname or root] = root
            self._check_module(root, alias.name, node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module is None:  # `from . import x` -- relative, out of scope for v1
            self.generic_visit(node)
            return
        root = node.module.split(".")[0]
        for alias in node.names:
            if alias.name == "*":
                continue  # `from x import *` binds names we cannot resolve statically
            self.name_imports[alias.asname or alias.name] = (root, alias.name)
        self._check_module(root, node.module, node)
        self.generic_visit(node)

    def _check_module(self, root: str, full: str, node: ast.stmt) -> None:
        if root not in FORBIDDEN_MODULES:
            return
        why = "network access" if root in NETWORK_MODULES else "sandbox escape or code execution"
        self.hits.append(
            GateHit(
                kind="forbidden_import",
                rule=f"forbidden-import:{root}",
                target=full,
                line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                message=f"imports {full!r}, which the sandbox denies ({why})",
                blocks_execution=True,
            )
        )

    # -- calls --------------------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        self._check_shell_true(node)
        func = node.func
        if isinstance(func, ast.Name):
            self._check_bare_name(func.id, node)
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            root = self.module_aliases.get(func.value.id, func.value.id)
            self._check_qualified(root, func.attr, node)
        self.generic_visit(node)

    def _check_bare_name(self, name: str, node: ast.Call) -> None:
        if name in FORBIDDEN_CALLS:
            self.hits.append(
                GateHit(
                    kind="forbidden_call",
                    rule=f"forbidden-call:{name}",
                    target=name,
                    line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    message=f"calls {name}(), which executes code built at runtime",
                    blocks_execution=False,
                )
            )
            return
        # `from os import system` then a bare `system(...)`
        imported = self.name_imports.get(name)
        if imported is not None:
            self._check_qualified(imported[0], imported[1], node, display=name)

    def _check_qualified(
        self, module: str, attr: str, node: ast.Call, display: str | None = None
    ) -> None:
        if (module, attr) not in SUSPICIOUS and (module, "*") not in SUSPICIOUS:
            return
        shown = display or f"{module}.{attr}"
        self.hits.append(
            GateHit(
                kind="suspicious_call",
                rule=f"suspicious-call:{module}.{attr}",
                target=shown,
                line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                message=f"calls {shown}(), which starts a process or mutates the filesystem",
                blocks_execution=False,
            )
        )

    def _check_shell_true(self, node: ast.Call) -> None:
        """`shell=True` anywhere. Called out explicitly because it is the single construct the
        threat model names as routine model output (docs/05 § Why this document exists)."""
        for kw in node.keywords:
            if (
                kw.arg == "shell"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
            ):
                self.hits.append(
                    GateHit(
                        kind="shell_true",
                        rule="shell-true",
                        target="shell=True",
                        line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                        message=(
                            "passes shell=True, making the command string a "
                            "shell injection sink"
                        ),
                        blocks_execution=False,
                    )
                )


def scan(source: str, filename: str = "target.py") -> list[GateHit]:
    """Scan source for gate hits. Returns [] when the source does not parse.

    A syntax error is not the gate's problem to report -- `patch.py` and the `input_unusable`
    policy row own that -- so it returns empty rather than raising.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []
    scanner = _Scanner()
    scanner.visit(tree)
    return sorted(scanner.hits, key=lambda h: (h.line, h.rule))


def findings(source: str, filename: str = "target.py") -> list[GroundingFinding]:
    """Normalise gate hits into `GroundingFinding`s the critics can cite."""
    out = []
    for hit in scan(source, filename):
        out.append(
            GroundingFinding(
                id=finding_id(TOOL, hit.rule, filename, hit.line),
                tool=TOOL,
                rule=hit.rule,
                file=filename,
                line=hit.line,
                end_line=hit.end_line,
                message=hit.message,
                # The gate's own rating, deliberately not mapped onto our Severity enum: the
                # critic must re-rate in context, and the eval measures how often it diverges.
                tool_severity="blocking" if hit.blocks_execution else "advisory",
                raw={
                    "kind": hit.kind,
                    "target": hit.target,
                    "blocks_execution": hit.blocks_execution,
                },
            )
        )
    return out


def execution_refusal(source: str, filename: str = "target.py") -> str | None:
    """Return a refusal reason if this source must not be executed, else None.

    Goes into `GroundingReport.tool_errors["sandbox"]` so the refusal is visible in the trace
    rather than looking like a tool that silently produced nothing.
    """
    blocking = [h for h in scan(source, filename) if h.blocks_execution]
    if not blocking:
        return None
    modules = sorted({h.target.split(".")[0] for h in blocking})
    network = [m for m in modules if m in NETWORK_MODULES]
    label = "network module import" if network else "denied module import"
    return f"refused: {label} ({', '.join(modules)})"


def assert_single_call_expression(expression: str) -> ast.Call:
    """Validate a model-supplied benchmark entry point before it goes into the timeit runner.

    The runner template is ours; only this expression comes from the model, and it is inserted
    as a literal string. Requiring exactly one `Call` expression -- not a statement list -- is
    what stops `foo(); import os; os.system("...")` from riding along
    (docs/05-execution-sandbox.md § What the Profiler is allowed to run).
    """
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"benchmark expression does not parse: {exc.msg}") from exc
    if not isinstance(tree.body, ast.Call):
        raise ValueError(
            f"benchmark expression must be a single call, got {type(tree.body).__name__}"
        )
    return tree.body
