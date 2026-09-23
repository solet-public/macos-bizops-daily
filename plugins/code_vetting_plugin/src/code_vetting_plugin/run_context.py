"""run_context.py — shared run-context helpers for the vetting CLIs.

Small read-only helpers both the Wave-1 sample renderer and the end-to-end
pipeline need: the repo root, the current git ref (read-only git), the real
tracked-debt allowlist totals, and a wall-clock stamp. Factored here so exactly
one definition exists.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

_ALLOWLIST_GATES: dict[str, str] = {
    "god_class": "god_class_allowlist.txt",
    "radon_cc": "radon_cc_allowlist.txt",
    "radon_mi": "radon_mi_allowlist.txt",
    "sql_access": "sql_access_allowlist.txt",
    "service_interface_ast": "service_interface_ast_allowlist.txt",
    "return_shape": "return_shape_allowlist.txt",
}


def repo_root() -> Path:
    """The platform checkout root — the single definition; CLIs and the gate wrapper delegate here.

    ``…/plugins/code_vetting_plugin/src/code_vetting_plugin/run_context.py`` → parents[4].
    """
    return Path(__file__).resolve().parents[4]


def git_head(root: Path) -> str:
    """Short HEAD sha via read-only git — the real target ref."""
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _count_entries(path: Path) -> int:
    """Non-blank, non-comment lines in an allowlist file (its tracked-debt count)."""
    text = path.read_text(encoding="utf-8")
    return sum(1 for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#"))


def allowlist_totals(root: Path) -> dict[str, int]:
    """Current per-gate allowlist entry counts — the real tracked-debt snapshot."""
    gates_dir = root / "quality_gates"
    return {gate: _count_entries(gates_dir / filename) for gate, filename in _ALLOWLIST_GATES.items()}


def system_clock() -> str:
    """Wall-clock ISO-8601 timestamp (UTC) — the caller-supplied run clock."""
    return datetime.now(UTC).isoformat()
