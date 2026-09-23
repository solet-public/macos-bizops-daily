"""Slice D — genesis attempt-marker writer.

Own-copy (minimal) of `macos_midwife_plugin/manifest_writer.py`'s
pattern: an audit-trail JSON recording what genesis did and when, at
`<target>/profile/data/github_midwife/attempt.json` (the
`profile/data/` root, not the solet root, so the boot-time
root-strictness check does not trip on a stray top-level file — same
reasoning as the macos_midwife precedent).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .constants import MANIFEST_MARKER_PATH


class ManifestMarkerError(RuntimeError):
    """Raised when the marker file cannot be written."""


def build_marker_payload(
    *, name: str, profile_name: str, steps: list[dict[str, Any]], status: str,
) -> dict[str, Any]:
    """Assemble the JSON payload that lands in the marker file."""
    return {
        "schema_version": 1,
        "solet_name": name,
        "profile_name": profile_name,
        "status": status,
        "steps": [dict(step) for step in steps],
        "written_at": datetime.now(UTC).isoformat(),
    }


def write_marker(target: Path, payload: dict[str, Any]) -> Path:
    """Write the marker JSON; return its absolute path.

    Atomicity: writes to `<filename>.tmp` first then renames — a crash
    mid-write leaves the prior marker (if any) intact.
    """
    final_path = target / MANIFEST_MARKER_PATH
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = final_path.with_suffix(final_path.suffix + ".tmp")
    try:
        tmp_path.write_text(json.dumps(payload, indent=2) + "\n")
        tmp_path.replace(final_path)
    except OSError as exc:
        raise ManifestMarkerError(
            f"manifest marker write failed at {final_path}: {exc}"
        ) from exc
    return final_path
