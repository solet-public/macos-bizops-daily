"""L1 driver adapter — implements the ``L1Scanner`` Protocol for ``VettingDriver``.

Wraps the synchronous scanner pipeline (:func:`runner.run_all`) as the async
deterministic layer the Stream-O driver injects. The heavy subprocess work runs
in a worker thread so it never blocks the driver's event loop. ``VettingDriver``
takes an ``L1Scanner`` by construction and never imports this module, so there is
no import cycle with ``driver.py``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from .driver import L1Output
from .run_record import RunTarget
from .runner import run_all
from .targets import TargetTree


@dataclass(frozen=True, slots=True)
class L1DeterministicScanner:
    """Concrete deterministic layer. ``root`` is the checkout to scan; a target
    whose ``repo`` is an absolute path overrides it, else ``root`` is used."""

    root: Path

    def _resolve_root(self, target: RunTarget) -> Path:
        candidate = Path(target.repo)
        return candidate if candidate.is_absolute() else self.root

    async def scan(self, run_id: str, target: RunTarget) -> L1Output:
        """Run every deterministic scanner, returning candidate findings + coverage."""
        root = self._resolve_root(target)
        tree = await asyncio.to_thread(TargetTree.from_git, root)
        findings, coverage, report_data = await asyncio.to_thread(run_all, tree, run_id)
        return L1Output(findings=findings, coverage=coverage, report_data=report_data)
