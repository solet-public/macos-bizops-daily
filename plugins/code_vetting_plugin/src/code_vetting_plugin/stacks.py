"""stacks.py — language-stack detection for a target tree (R7-1).

The deterministic scanner roster is multi-ecosystem: some scanners are Python-only
(the platform gate wrappers), some are TypeScript/JavaScript-only (tsc, eslint), and
some are multi-stack (semgrep, osv). R7 gates the stack-specific scanners on what the
target actually IS, so a TS scanner never fires on the platform's own Python worktree and a
Python gate never fires on a foreign React-Native tree — each records an honest
``not_applicable`` instead (ruling C; the roster never forks).

Detection is **enumeration-driven** (over ``tree.tracked``, never a second disk probe),
deterministic, and deliberately dumb: a stack is present iff the tree carries its
source suffixes or its canonical manifest. The detected set is recorded on the run and
rendered as a ``- **Stacks:** …`` report-header line, so a reader of any report can see
what the engine believed the target was (the FT-1 ``Enumeration:``-line pattern).
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .targets import TargetTree

# The root TypeScript-config basename — a secondary TYPESCRIPT signal beyond `.ts`/`.tsx`.
_TSCONFIG_NAME = "tsconfig.json"


class Stack(StrEnum):
    """A language ecosystem a target may carry (R7-1). Multiple may be present at once."""

    PYTHON = "python"
    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"


def detect_stacks(tree: TargetTree) -> frozenset[Stack]:
    """The set of language stacks the enumerated tree carries (deterministic, dumb).

    ``PYTHON``: any ``*.py`` or any Python dependency manifest (reuses the FT-1.1 helper).
    ``TYPESCRIPT``: any ``*.ts``/``*.tsx`` (``.d.ts`` counts) or a root ``tsconfig.json``.
    ``JAVASCRIPT``: any ``*.js``/``*.jsx``/``*.mjs``/``*.cjs``.
    """
    detected: set[Stack] = set()
    if tree.python_files() or tree.python_dependency_manifests():
        detected.add(Stack.PYTHON)
    if tree.typescript_files() or any(Path(rel).name == _TSCONFIG_NAME for rel in tree.all_files()):
        detected.add(Stack.TYPESCRIPT)
    if tree.javascript_files():
        detected.add(Stack.JAVASCRIPT)
    return frozenset(detected)


def render_stacks(stacks: frozenset[Stack]) -> str:
    """Deterministic comma-joined label of a stack set for the report header (empty → ``none``)."""
    return ", ".join(sorted(stack.value for stack in stacks)) if stacks else "none"
