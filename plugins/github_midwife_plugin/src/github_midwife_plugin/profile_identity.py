"""The release profile a solet was born from, read from its sealed ``PROVENANCE.json`` (iss_eb626338).

``manifest.yaml``'s ``profile_name`` is a label, not an identity: templates renamed it
five times, and ``apply_manifest`` rewrites it to ``local``.  ``PROVENANCE.json``'s bundle
is sealed into the release and travels with merges, so genesis uses it to pick the
template and the existing-install plugin transitions use it to decide which profile a
solet is.  This module is import-light on purpose: the update adapter runs it without
the environment genesis needs (vault constants raise at import without ``SOLET_NAME``).
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = [
    "PROFILE_TEMPLATE_BY_BUNDLE",
    "PROVENANCE_FILENAME",
    "ProvenanceError",
    "declared_bundle_name",
    "declared_profile",
]

PROVENANCE_FILENAME = "PROVENANCE.json"
PROFILE_TEMPLATE_BY_BUNDLE = {
    "macos_free_minimal": "macos-free-solet",
    "macos-bizops": "macos-bizops",
    "macos_samantha": "macos-samantha-solet",
}


class ProvenanceError(ValueError):
    """``PROVENANCE.json`` is present but names no profile this release knows."""


def declared_bundle_name(clone_root: Path) -> str | None:
    """The sealed bundle name; ``None`` when there is no provenance file or it declares no bundle."""
    provenance_path = clone_root / PROVENANCE_FILENAME
    if not provenance_path.is_file():
        return None
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProvenanceError(f"{PROVENANCE_FILENAME} is not valid JSON: {exc}") from exc
    if not isinstance(provenance, dict):
        raise ProvenanceError(f"{PROVENANCE_FILENAME} must contain a JSON object")

    bundle = provenance.get("bundle")
    if not isinstance(bundle, dict):
        return None
    bundle_name = bundle.get("name")
    return bundle_name.strip() if isinstance(bundle_name, str) and bundle_name.strip() else None


def declared_profile(clone_root: Path) -> str | None:
    """The release profile the sealed bundle selects; ``None`` when no bundle is declared, loud when it is unknown."""
    bundle_name = declared_bundle_name(clone_root)
    if bundle_name is None:
        return None
    profile = PROFILE_TEMPLATE_BY_BUNDLE.get(bundle_name)
    if profile is None:
        raise ProvenanceError(f"{PROVENANCE_FILENAME} declares unknown bundle {bundle_name!r}")
    return profile
