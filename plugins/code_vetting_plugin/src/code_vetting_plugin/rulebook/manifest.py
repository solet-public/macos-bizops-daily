"""manifest.py — the shared three-level hashing for the assembled rulebook (W3-C C1).

ONE module, THREE callers: the assembler (writes the manifest), the loader (verifies the
whole-artifact hash fail-loud at every load), and ``rulebook_sync`` (W3C-1b — re-derives the
per-source hashes to catch drift). Sharing this module is precisely what prevents the
assembler and the checker from computing hashes differently — the anti-drift guarantee.

Three levels (Q1): per-SOURCE content hash (each pinned anchor + its hash — ``rulebook_sync``'s
drift probe), per-TIER-section hash (localizes a diff for the repair step), and the whole-artifact
hash (the load-time fail-loud integrity check). All hashes are sha256 over a CANONICAL JSON
serialization (sorted keys, no whitespace) so a value's hash is independent of dict ordering.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

_MANIFEST_KEY = "manifest"
_CONTENT_KEY = "content"
_ARTIFACT_HASH_KEY = "artifact_hash"


def canonical_hash(value: Any) -> str:  # noqa: ANN401 — hashes any JSON-serializable rulebook fragment
    """sha256 over the canonical JSON of ``value`` — dict-order-independent, whitespace-independent."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_manifest(content: dict[str, Any], sources: list[dict[str, Any]], tier_sections: dict[str, Any]) -> dict[str, Any]:
    """The three-level manifest for one assembled artifact.

    ``sources`` is ``[{"anchor": str, "content": <json>}, ...]`` — each pinned source (a doc
    section, the in-code directive/DNF sets, a guidance article). ``tier_sections`` maps a tier
    name to the content subset carrying that tier. Only hashes are stored, never the source text.
    """
    return {
        "sources": [{"anchor": source["anchor"], "hash": canonical_hash(source["content"])} for source in sources],
        "tiers": {tier: canonical_hash(section) for tier, section in tier_sections.items()},
        _ARTIFACT_HASH_KEY: canonical_hash(content),
    }


def build_artifact(content: dict[str, Any], sources: list[dict[str, Any]], tier_sections: dict[str, Any]) -> dict[str, Any]:
    """The full on-disk artifact: ``{content, manifest}``."""
    return {_CONTENT_KEY: content, _MANIFEST_KEY: build_manifest(content, sources, tier_sections)}


def verify_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    """Return the artifact ``content`` after checking its whole-artifact hash; FAIL LOUD on mismatch.

    Every runtime load runs this: a tampered/corrupt/hand-edited artifact whose content no longer
    matches the pinned ``artifact_hash`` raises rather than seeding skeptics against a broken moat.
    """
    content = artifact.get(_CONTENT_KEY)
    manifest = artifact.get(_MANIFEST_KEY)
    if not isinstance(content, dict) or not isinstance(manifest, dict):
        raise ValueError("assembled rulebook is malformed — missing 'content' or 'manifest'")
    expected = manifest.get(_ARTIFACT_HASH_KEY)
    actual = canonical_hash(content)
    if actual != expected:
        raise ValueError(
            f"assembled rulebook hash mismatch — artifact content does not match its pinned hash "
            f"(expected {expected}, got {actual}); the rulebook is corrupt or was hand-edited. "
            "Re-run the assembler and commit the regenerated artifact."
        )
    return content
