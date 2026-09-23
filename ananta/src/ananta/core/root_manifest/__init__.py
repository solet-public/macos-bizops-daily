"""Per-solet root-manifest drift discipline.

Three consumers read ``<solet_root>/root_manifest.yaml``:

* :mod:`ananta.core.root_manifest.pre_commit` — BLOCKING at ``git commit``.
* :mod:`ananta.core.root_manifest.cutover` — BLOCKING at blue-green cutover.
* :mod:`ananta.core.root_manifest.diagnostic` — NEVER BLOCKING; logs only.

All three pivot on :func:`classify_root_entries`, which yields a
:class:`Classification` carrying every drift dimension.  The strictness
escalation (BLOCKING vs INFO) is the consumer's interpretation; the
classifier returns observed facts.

Design: ``workbench/2026-06-16_root_manifest_yaml_design.md``.
"""

from .classifier import classify_root_entries, load_manifest
from .types import (
    MANIFEST_FILENAME,
    Classification,
    Manifest,
    SunsetOverdue,
)

__all__ = [
    "MANIFEST_FILENAME",
    "Classification",
    "Manifest",
    "SunsetOverdue",
    "classify_root_entries",
    "load_manifest",
]
