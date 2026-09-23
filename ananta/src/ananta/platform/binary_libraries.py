"""Canonical path resolver for the platform's binary library tree.

Per the storage architecture design §1.3, binary libraries are
content-addressable, shared, immutable assets fetched at install time
(``asset_manager`` / ``plugin.yaml`` ``assets:`` block locally; AWS
Mountpoint for S3 in the cloud). They live under ``<repo>/binary_libraries/``
on disk, or ``/app/binary_libraries/`` inside the solet container
where the Mountpoint S3 bind-mount lands. Pre-Task-#57 plugin code
chained ``Path(__file__).parent.parent.parent`` from per-plugin source
files to reach the historical in-plugin paths (``instruments/``,
``impulse_responses/``, ``sound_effects/``); Task #56 removed those
in-plugin directories from git + the CodeBuild context, so plugin
code must now read from the canonical location directly.

This module is the only place that computes the binary-library root.
Plugins that need samples / soundfonts / impulse responses / sound
effects call :func:`path_for` with their plugin name and an optional
library subdirectory. The helper does NOT verify that the resulting
path exists — callers handle absence (e.g., asset_manager hasn't
fetched yet, or the library is optional).
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

# Resolve the repo root from this file's location. Layout:
#   <repo>/ananta/src/ananta/platform/binary_libraries.py
# parents[0]=platform, [1]=ananta, [2]=src, [3]=ananta(outer), [4]=<repo>.
# The cloud container mirrors this exactly under /app/, so the same math
# resolves to /app/ at runtime.
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[4]
_BINARY_LIBRARIES_ROOT: Final[Path] = _REPO_ROOT / "binary_libraries"


def path_for(plugin_name: str, library_subdir: str = "") -> Path:
    """Return the canonical filesystem location for a plugin's binary library tree.

    Args:
        plugin_name: The plugin's directory name (e.g.
            ``"musical_synthesis_plugin"``).
        library_subdir: Optional subdirectory beneath the plugin's binary
            library root (e.g. ``"musescore_general"``). Defaults to the
            empty string, which returns the plugin's root.

    Returns:
        ``<repo>/binary_libraries/<plugin_name>[/<library_subdir>]`` as a
        :class:`pathlib.Path`. Existence is the caller's concern.
    """
    base = _BINARY_LIBRARIES_ROOT / plugin_name
    if library_subdir:
        return base / library_subdir
    return base


__all__ = ["path_for"]
