"""The production host-Python resolver behind ``RuntimeSeams.resolve_base_python`` (Step 7 section 4.1).

``update_runtime_plan`` must never import the create-flow adapter module (the
Step-5 reachability smoke enforces it), so the one production default that
does live here: the same fixed-path resolver the create flow uses, exposed as a
closed vector a fixture can replace without touching the host.
"""

from __future__ import annotations

from pathlib import Path

from .adapters import resolve_long_lived_python

__all__ = ["resolve_base_python"]


def resolve_base_python() -> Path | None:
    """Python 3.13 outside the Manager keg, or ``None`` after every candidate was asked."""
    return resolve_long_lived_python()
