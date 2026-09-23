"""Genesis-time helper for the root ``root_manifest.yaml``.

The MINT seed ships the minting solet's ``root_manifest.yaml``
verbatim (``assemble`` copies it from the committed ref), so a seed-born
clone's ``solet_name:`` field still names the MINTING solet,
not the newborn. This module's :func:`seed_for_newborn` rewrites that
single line at genesis so the newborn's manifest accurately identifies
itself — the seed axis's counterpart to ``macos_midwife_plugin``'s
``root_manifest_seed`` (own-copy per this plugin's convention, never a
cross-plugin import).

All other sections (``universal:`` / ``platform_managed:`` /
``sanctioned:`` / ``overrides:`` / ``diagnostic:``) ride through
verbatim; the operator edits the newborn's manifest if minting-tree
sanctioned drift does not apply.

Self-ship note: this module ships in every seed and the §4.2 content
validator scans it, so no identity literal appears here — the minting
name is only ever a runtime value.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

ROOT_MANIFEST_FILENAME: Final[str] = "root_manifest.yaml"
# [ \t]* (not \s*) so the match NEVER crosses the line's own newline, and the
# replacement supplies no newline of its own — the rewrite is byte-exact on
# the one line (the macOS original's \s* + appended-\n shape injected a blank
# line after the name; caught by this plugin's smoke, fixed in both copies
# 2026-07-12).
_SOLET_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^(solet_name:[ \t]*)\S+[ \t]*$", re.MULTILINE,
)


class RootManifestSeedError(RuntimeError):
    """Raised when the newborn manifest cannot be seeded."""


def seed_for_newborn(newborn_root: Path, solet_name: str) -> Path:
    """Rewrite ``<newborn_root>/root_manifest.yaml`` to name the newborn.

    Returns the manifest path.  Raises :class:`RootManifestSeedError`
    when the file is missing or carries no ``solet_name:`` line —
    both are fail-loud states: every seed ships the file (it is in the
    seed_manifest ``copy:`` allowlist) and the schema requires the line.
    Idempotent: a second run rewrites the line to the same value.
    """
    manifest_path = newborn_root / ROOT_MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise RootManifestSeedError(
            f"newborn root_manifest.yaml not present at {manifest_path}; "
            "the seed's copy allowlist ships it — a missing file means the "
            "clone is not a complete seed tree."
        )
    text = manifest_path.read_text(encoding="utf-8")
    rewritten, count = _SOLET_NAME_RE.subn(
        lambda m: f"{m.group(1)}{solet_name}", text, count=1,
    )
    if count != 1:
        raise RootManifestSeedError(
            f"root_manifest.yaml at {manifest_path} carries no "
            "`solet_name:` line; manifest schema violation."
        )
    manifest_path.write_text(rewritten, encoding="utf-8")
    return manifest_path


__all__ = [
    "ROOT_MANIFEST_FILENAME",
    "RootManifestSeedError",
    "seed_for_newborn",
]
