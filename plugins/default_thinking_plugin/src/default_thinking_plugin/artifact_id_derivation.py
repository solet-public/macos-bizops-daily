"""Deterministic artifact ID derivation.

The platform derives all artifact IDs from structured metadata so the
thinking model never authors identifiers. This eliminates the class of
errors where the model copies an example ID from a plan template instead
of reading the actual artifact in scope.

Naming contract (canonical, fixture-aligned):

    wmf-{genre}-composition-{NNN}-{seq}     Work Manifest
    brf-{genre}-composition-{NNN}-{seq}     Brief
    cdg-{genre}-composition-{NNN}-{seq}     Composition Design Document
    psp-{genre}-composition-{NNN}-{seq}     Pipeline Spec
    wbs-{genre}-composition-{NNN}-{seq}-phase{N}   WBS

``genre`` is the user-stated genre slug (already dash-separated).
``NNN`` is the zero-padded composition number (3 digits).
``seq`` is the manifest sequence within a composition (always ``001``
today; reserved for future re-attempts).
"""

from __future__ import annotations

import re
from typing import Final

# ── Field-extraction regexes ───────────────────────────────────────────
#
# Both the directive (freeform key=value list) and the brief content
# (markdown bullets) carry the same canonical fields. A single pattern
# tolerates either by allowing optional leading bullet markers and either
# ``:`` or ``=`` separators. Anchors are line-relative; callers normalize
# the U+2424 line separator (used by the model in JSON-string arguments)
# to ``\n`` before matching.

_COMPOSITION_NUMBER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?im)^\s*[-*]?\s*composition[_ ]number\s*[:=]\s*(\d+)\s*$"
)
_GENRE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?im)^\s*[-*]?\s*genre\s*[:=]\s*([a-z0-9][a-z0-9-]*)\s*$"
)


class ArtifactIdDerivationError(ValueError):
    """Raised when required metadata for ID derivation is missing or invalid."""


def _normalize_line_separators(text: str) -> str:
    """Convert U+2424 to newline so line-anchored patterns match.

    The thinking model is instructed to use U+2424 as the line separator
    in JSON-string arguments (so JSON parsers don't mangle real newlines).
    Other parts of the platform normalize this for plan content; we do
    the same for directives consumed here.
    """
    return text.replace("\u2424", "\n")


def parse_composition_metadata(text: str) -> tuple[int, str]:
    """Extract ``(composition_number, genre)`` from a directive or brief content.

    Raises ``ArtifactIdDerivationError`` if either field is missing.
    """
    text = _normalize_line_separators(text)
    cn_match = _COMPOSITION_NUMBER_RE.search(text)
    if cn_match is None:
        raise ArtifactIdDerivationError(
            "Missing required field 'composition_number' "
            "(expected line like 'composition_number: 20')"
        )
    genre_match = _GENRE_RE.search(text)
    if genre_match is None:
        raise ArtifactIdDerivationError(
            "Missing required field 'genre' "
            "(expected line like 'genre: <genre-name>')"
        )
    return int(cn_match.group(1)), genre_match.group(1).lower()


_PREFIX_BY_ARTIFACT_TYPE: Final[dict[str, str]] = {
    "brief": "brf",
    "composition_design": "cdg",
    "pipeline_spec": "psp",
}


def derive_authored_artifact_id(manifest_id: str, artifact_type: str) -> str:
    """Build a child artifact id by swapping the ``wmf-`` prefix.

    The artifact_type-to-prefix mapping is centralized here so a renamed
    artifact type cannot silently produce an inconsistent id.
    """
    prefix = _PREFIX_BY_ARTIFACT_TYPE.get(artifact_type)
    if prefix is None:
        raise ArtifactIdDerivationError(
            f"Unknown artifact_type for id derivation: {artifact_type!r}"
        )
    if not manifest_id.startswith("wmf-"):
        raise ArtifactIdDerivationError(
            f"manifest_id must start with 'wmf-': {manifest_id!r}"
        )
    suffix = manifest_id.removeprefix("wmf-")
    return f"{prefix}-{suffix}"


def derive_brief_id(directive: str) -> str:
    """Derive the brief id from the directive's structured fields.

    The brief is the first artifact and has no upstream id to inherit
    from, so its identifier comes from the directive that initiated
    its authoring (per the plan-step contract: the directive must
    include ``composition_number=N`` and ``genre=<slug>``).
    """
    composition_number, genre = parse_composition_metadata(directive)
    return f"brf-{genre}-composition-{composition_number:03d}-001"


