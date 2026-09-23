"""Typed data models for default_knowledge_plugin."""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import (
    DEFAULT_CHUNK_MAX_CHARS,
    DEFAULT_CHUNK_OVERLAP_CHARS,
    DEFAULT_EXCLUDE_PATTERNS,
    DEFAULT_INCLUDE_PATTERNS,
    WritePosture,
)


@dataclass(frozen=True, slots=True)
class Manifest:
    """Indexing configuration for a knowledge base.

    Resolved from (in order): manifest.yaml in KB directory,
    address book custom entry, or defaults.

    ``default_knowledge_layer`` is an optional manifest-level fallback
    used when a specific article omits its ``Article Layer:`` line. It
    is a convenience for homogeneous knowledge bases (e.g., the
    process registry — every article is Layer 1) and is not a
    substitute for per-article annotation in mixed knowledge bases.

    ``write_posture`` gates the file-ops write verbs for this KB (W4).
    ``require_metadata_block`` makes ``create_file`` validate the §4
    metadata block before writing (W4). ``archive_subdir`` names the
    relative subdirectory the ``archive_file`` lifecycle verb moves
    retired docs into; when unset the verb fail-louds (W11).
    """

    name: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    process_keys: list[str] = field(default_factory=list)
    include_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_INCLUDE_PATTERNS))
    exclude_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_PATTERNS))
    chunking_strategy: str = "headers"
    max_chars: int = DEFAULT_CHUNK_MAX_CHARS
    overlap_chars: int = DEFAULT_CHUNK_OVERLAP_CHARS
    default_knowledge_layer: int | None = None
    write_posture: WritePosture = WritePosture.FULL
    require_metadata_block: bool = False
    archive_subdir: str | None = None


@dataclass(frozen=True, slots=True)
class ArticleMetadata:
    """Per-article metadata extracted from the markdown preamble.

    Replaces the previous tuple-return shape of
    ``_extract_article_metadata``. Adding new metadata fields here is
    cheaper than threading another return tuple component.

    ``knowledge_layer`` is None when the article has no explicit
    ``Article Layer:`` line and no manifest default applies. The
    indexer emits ``knowledge:layer:unlabeled`` for chunks derived
    from such articles.
    """

    role: str = "reference"
    tags: list[str] = field(default_factory=list)
    knowledge_layer: int | None = None


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    """Chunk content with source metadata."""

    content: str
    relative_doc_path: str
    chunk_index: int
