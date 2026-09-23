"""Source resolution, manifest parsing, chunking, and indexing helpers.

Covers the full path from KB directory → indexed memory chunks.
No plugin instance — services are passed as explicit parameters.
"""

from __future__ import annotations

import logging
from datetime import UTC
from pathlib import Path
from typing import Any

import yaml
from ananta.services.memory_service.actr.constants import (
    EMBEDDING_MAX_CHARS,
)

from .chunking import chunk_by_headers as _chunk_by_headers
from .chunking import chunk_title_block as _chunk_title_block
from .constants import (
    DOC_HISTORY_DIRNAME,
    PLUGIN_NAME,
    TAG_DOMAIN_OFFICIAL,
    TAG_LAYER_UNLABELED,
    TAG_PREFIX_SCOPE,
    Scope,
    WritePosture,
    document_tag,
    kb_id_tag,
    knowledge_layer_tag,
    manifest_tag,
    process_key_tag,
)
from .models import ArticleMetadata, Manifest

logger = logging.getLogger(__name__)

_MANIFEST_FILE = "manifest.yaml"
_SOURCE_LINE_PREFIX = "Source: "


# ---------------------------------------------------------------------------
# Article metadata extraction (moved from class @staticmethod)
# ---------------------------------------------------------------------------

def extract_article_title(content: str) -> str:
    """Extract the first markdown header as the article/chunk title."""
    for line in content.split("\n")[:5]:
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def extract_article_metadata(content: str) -> ArticleMetadata:
    """Extract Article Role, Article Tags, and Article Layer from content."""
    role = "reference"
    article_tags: list[str] = []
    layer: int | None = None
    for line in content.split("\n")[:30]:
        stripped = line.strip()
        if stripped.startswith("Article Role:"):
            role = stripped[len("Article Role:"):].strip()
        elif stripped.startswith("Article Tags:"):
            raw = stripped[len("Article Tags:"):].strip()
            article_tags = [t.strip() for t in raw.split(",") if t.strip()]
        elif stripped.startswith("Article Layer:"):
            value_str = stripped[len("Article Layer:"):].strip()
            try:
                value = int(value_str)
            except ValueError as exc:
                raise ValueError(
                    f"Article Layer must parse as an integer; got {value_str!r}"
                ) from exc
            if value < 1:
                raise ValueError(
                    f"Article Layer must be a positive integer; got {value!r}"
                )
            layer = value
    return ArticleMetadata(role=role, tags=article_tags, knowledge_layer=layer)


# ---------------------------------------------------------------------------
# Source line / chunk preamble builders
# ---------------------------------------------------------------------------

def build_source_line(kb_name: str, doc_title: str) -> str:
    """Build a ``Source:`` metadata line for embedding enrichment."""
    if doc_title:
        return f"{_SOURCE_LINE_PREFIX}{kb_name} — {doc_title}"
    return f"{_SOURCE_LINE_PREFIX}{kb_name}"


def build_chunk_preamble(
    source_line: str,
    article_role: str,
    article_tags: list[str],
    knowledge_layer: int | None = None,
) -> str:
    """Build the metadata preamble prepended to every chunk."""
    parts = [source_line]
    if knowledge_layer is not None:
        parts.append(f"Article Layer: {knowledge_layer}")
    if article_role and article_role != "reference":
        parts.append(f"Article Role: {article_role}")
    if article_tags:
        parts.append(f"Article Tags: {', '.join(article_tags)}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Source resolution + manifest
# ---------------------------------------------------------------------------

def resolve_from_address_book(
    name: str, address_book_service: Any,
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """Resolve source URL, token, and indexing config from the address book."""
    from ananta.core.domain.enums import ActionStatus

    if address_book_service is None:
        return None, None, None

    resolved = address_book_service.resolve_with_secrets(name)
    if resolved.get("action_status") != ActionStatus.COMPLETED.value:
        return None, None, None

    data = resolved.get("data", {})
    entries = data.get("entries", []) if isinstance(data, dict) else []

    url: str | None = None
    token: str | None = None
    indexing_config: dict[str, Any] | None = None

    for entry in entries:
        field_type = entry.get("field_type", "")
        if field_type == "url":
            url = entry.get("value")
        elif field_type == "token":
            token = entry.get("value")
        elif field_type == "custom":
            desc = entry.get("description", "")
            if "indexing configuration" in desc.lower():
                import json
                raw = entry.get("value", "{}")
                indexing_config = json.loads(raw) if isinstance(raw, str) else raw

    return url, token, indexing_config


def resolve_source(
    name: str,
    source: str | None,
    kb_root: Path | None,
    address_book_service: Any,
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """Resolve source URL, token, and address book config."""
    kb_dir = kb_root / name if kb_root else None

    if source:
        return source, None, None

    if kb_dir and kb_dir.exists():
        return None, None, None

    url, token, indexing_config = resolve_from_address_book(name, address_book_service)
    if url:
        return url, token, indexing_config

    raise FileNotFoundError(
        f"Knowledge base '{name}' not found. No local directory at "
        f"{kb_dir}, no source URL provided, and no address book entry."
    )


def _apply_content_config(kwargs: dict[str, Any], content: dict[str, Any]) -> None:
    """Apply the ``content.patterns`` + ``content.chunking`` manifest block."""
    patterns = content.get("patterns", {})
    chunking = content.get("chunking", {})
    if patterns.get("include"):
        kwargs["include_patterns"] = patterns["include"]
    if patterns.get("exclude"):
        kwargs["exclude_patterns"] = patterns["exclude"]
    if chunking.get("strategy"):
        kwargs["chunking_strategy"] = chunking["strategy"]
    if chunking.get("max_chars"):
        kwargs["max_chars"] = int(chunking["max_chars"])
    if chunking.get("overlap_chars"):
        kwargs["overlap_chars"] = int(chunking["overlap_chars"])


def _parse_default_knowledge_layer(data: dict[str, Any]) -> int | None:
    """Parse + validate the top-level ``default_knowledge_layer`` (fast-fail)."""
    raw_default_layer = data.get("default_knowledge_layer")
    if raw_default_layer is None:
        return None
    try:
        value = int(raw_default_layer)
        if value < 1:
            raise ValueError(
                f"default_knowledge_layer must be a positive integer; got {value!r}"
            )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"manifest {data.get('name', '<unknown>')!r} has an "
            f"invalid default_knowledge_layer: {exc}"
        ) from exc
    return value


def _parse_write_posture(data: dict[str, Any]) -> WritePosture | None:
    """Parse + validate the top-level ``write_posture`` enum (fast-fail)."""
    raw_posture = data.get("write_posture")
    if raw_posture is None:
        return None
    try:
        return WritePosture(raw_posture)
    except ValueError as exc:
        valid = ", ".join(p.value for p in WritePosture)
        raise ValueError(
            f"manifest {data.get('name', '<unknown>')!r} has an invalid "
            f"write_posture {raw_posture!r}; expected one of: {valid}"
        ) from exc


def parse_manifest(data: dict[str, Any]) -> Manifest:
    """Parse manifest dict into Manifest dataclass."""
    kwargs: dict[str, Any] = {
        "name": data.get("name", ""),
        "description": data.get("description", ""),
        "tags": data.get("tags", []),
        "process_keys": data.get("process_keys", []),
    }
    _apply_content_config(kwargs, data.get("content", {}))

    layer = _parse_default_knowledge_layer(data)
    if layer is not None:
        kwargs["default_knowledge_layer"] = layer
    posture = _parse_write_posture(data)
    if posture is not None:
        kwargs["write_posture"] = posture
    if data.get("require_metadata_block") is not None:
        kwargs["require_metadata_block"] = bool(data["require_metadata_block"])
    if data.get("archive_subdir"):
        kwargs["archive_subdir"] = str(data["archive_subdir"])

    return Manifest(**kwargs)


def resolve_manifest(
    kb_dir: Path, name: str, indexing_config: dict[str, Any] | None
) -> Manifest:
    """Resolve manifest from directory file, address book, or defaults."""
    manifest_path = kb_dir / _MANIFEST_FILE
    if manifest_path.exists():
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("name", name)
        return parse_manifest(raw)

    if indexing_config:
        indexing_config.setdefault("name", name)
        return parse_manifest(indexing_config)

    return Manifest(name=name)


# ---------------------------------------------------------------------------
# File collection + classification
# ---------------------------------------------------------------------------

def classify_source_type(kb_dir: Path) -> str:
    """Classify source type: symlink, git, or local."""
    if kb_dir.is_symlink():
        return "symlink"
    if (kb_dir / ".git").exists():
        return "git"
    return "local"


def collect_files(kb_dir: Path, manifest: Manifest) -> list[Path]:
    """Collect files matching manifest patterns."""
    matched: set[Path] = set()
    for pattern in manifest.include_patterns:
        matched.update(kb_dir.glob(pattern))
        matched.update(kb_dir.rglob(pattern))

    filtered: list[Path] = []
    for path in sorted(matched):
        if not path.is_file():
            continue
        rel_path = path.relative_to(kb_dir)
        relative = str(rel_path)
        if relative.startswith("processes/"):
            continue
        # Code-level exclusion of the W12 snapshot sidecar. A manifest
        # ``**/.doc_history/**`` glob is NOT trusted for deep snapshot nesting
        # (e.g. ``.doc_history/archive/foo.md/<ts>.md``) because ``Path.match``
        # does not reliably cover it; the parts check is authoritative.
        if DOC_HISTORY_DIRNAME in rel_path.parts:
            continue
        excluded = False
        for exc_pattern in manifest.exclude_patterns:
            if path.match(exc_pattern):
                excluded = True
                break
            if Path(relative).match(exc_pattern):
                excluded = True
                break
        if not excluded:
            filtered.append(path)

    return filtered


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_file(content: str, manifest: Manifest, relative_path: str) -> list[str]:
    """Split content into chunks per manifest strategy.

    ``headers`` uses the header-aware recursive chunker; ``title_block`` emits
    exactly one name/summary discovery chunk carrying the KB-relative read
    path (workbench). An unknown strategy is a fast-fail ``ValueError`` naming
    the manifest and strategy — the prior silent ``chunk_fixed_size`` fallback
    is gone (no live manifest declared it), so a registered KB can never
    silently full-content-chunk on a strategy typo.
    """
    if manifest.chunking_strategy == "headers":
        return _chunk_by_headers(content, manifest.max_chars)
    if manifest.chunking_strategy == "title_block":
        return _chunk_title_block(content, relative_path, manifest.max_chars)
    raise ValueError(
        f"Unknown chunking strategy {manifest.chunking_strategy!r} in manifest "
        f"{manifest.name!r}; expected 'headers' or 'title_block'."
    )


# ---------------------------------------------------------------------------
# Tag building
# ---------------------------------------------------------------------------

def build_chunk_tags(
    name: str,
    relative_path: str,
    manifest: Manifest,
    article_metadata: ArticleMetadata,
) -> list[str]:
    """Build tag set for a chunk."""
    tags = [
        TAG_DOMAIN_OFFICIAL,
        kb_id_tag(name),
        f"{TAG_PREFIX_SCOPE}{Scope.WORKSPACE.value}",
        document_tag(relative_path),
    ]
    for pk in manifest.process_keys:
        tags.append(process_key_tag(pk))
    for t in manifest.tags:
        tags.append(manifest_tag(t))
    effective_layer = (
        article_metadata.knowledge_layer
        if article_metadata.knowledge_layer is not None
        else manifest.default_knowledge_layer
    )
    if effective_layer is not None:
        tags.append(knowledge_layer_tag(effective_layer))
    else:
        tags.append(TAG_LAYER_UNLABELED)
    return tags


# ---------------------------------------------------------------------------
# Indexing and memory management
# ---------------------------------------------------------------------------

def index_files(
    kb_dir: Path,
    name: str,
    manifest: Manifest,
    memory_service: Any,
    files: list[Path] | None = None,
) -> tuple[list[str], int]:
    """Index files into memory. Returns (memory_ids, chunk_count)."""
    if files is None:
        files = collect_files(kb_dir, manifest)

    memory_ids: list[str] = []
    chunk_count = 0

    for file_path in files:
        relative = str(file_path.relative_to(kb_dir))
        content = file_path.read_text(encoding="utf-8", errors="replace")
        doc_title = extract_article_title(content)
        source_line = build_source_line(name, doc_title)
        article_metadata = extract_article_metadata(content)
        effective_layer = (
            article_metadata.knowledge_layer
            if article_metadata.knowledge_layer is not None
            else manifest.default_knowledge_layer
        )
        preamble = build_chunk_preamble(
            source_line,
            article_metadata.role,
            article_metadata.tags,
            knowledge_layer=effective_layer,
        )
        preamble_budget = len(preamble) + 1
        adjusted = Manifest(
            name=manifest.name,
            max_chars=max(manifest.max_chars - preamble_budget, manifest.max_chars // 2),
            chunking_strategy=manifest.chunking_strategy,
            overlap_chars=manifest.overlap_chars,
            default_knowledge_layer=manifest.default_knowledge_layer,
        )
        chunks = chunk_file(content, adjusted, relative)

        for chunk_content in chunks:
            enriched = f"{preamble}\n{chunk_content}"
            if len(enriched) > EMBEDDING_MAX_CHARS:
                raise ValueError(
                    f"Knowledge base chunk exceeds EMBEDDING_MAX_CHARS "
                    f"({len(enriched)} > {EMBEDDING_MAX_CHARS}). "
                    f"Add sub-headers to break up long sections in "
                    f"{name}/{relative}"
                )
            tags = build_chunk_tags(name, relative, manifest, article_metadata)
            result = memory_service.remember(content=enriched, tags=tags)
            mid = result.get("memory_id") or result.get("id")
            if mid:
                memory_ids.append(str(mid))
            chunk_count += 1

    return memory_ids, chunk_count


def delete_kb_chunks(memory_ids: list[str], memory_service: Any) -> None:
    """Hard-delete KB chunk rows through the OWNING memory service.

    Per W5.P §2.4: KB chunks are system-managed indexed text. No
    cognitive reinforcement / focus semantics apply (those are user-facing
    actr_memory concerns). Hard-delete is correct; soft-archive via
    user-facing forget() was residue.

    KB chunks are actr_memory rows; per the 2026-06-21 SQL-lockdown cohort
    (Cross-Plugin Data Access) they are deleted through the owner verb
    ``service_interface::memory_service::delete_memories_by_ids``, never via raw
    SQL on the foreign ``actr_memory_plugin__memory`` namespace. That verb
    cascades VECTOR-FIRST and then hard-deletes the records, so it SUBSUMES the
    prior best-effort kb-side ``vector_service.delete_by_external_ids`` call.
    Cross-plugin transactional atomicity is RELINQUISHED (a service call cannot
    join the caller's state transaction — W5.P §3.1 Option A); the residual
    orphan window is regenerable index, swept by the operator-fired
    ``purge_orphaned_chunks`` / ``cleanup_orphaned_vectors`` verbs.
    """
    if not memory_ids:
        return
    memory_service.delete_memories_by_ids(list(memory_ids))


def delete_kb_chunks_for_file(
    name: str, relative_path: str, memory_service: Any,
) -> list[str]:
    """Deterministic per-file hard-delete — replaces ``forget_chunks_for_file``.

    Per W5.P §7.6 (C4 fix): the original `forget_chunks_for_file` used
    ``memory_service.recall(query=f"file:{path}", top_k=1000)`` which is
    relevance-truncated. Files with >1000 chunks leaked orphans. This
    replacement resolves the exact chunk-id set deterministically.

    FIND through the OWNING ``service_interface::memory_service::get_memories_by_tag``
    (cohort D1, no foreign-namespace SQL): query the NARROWER ``doc_tag`` then
    AND-filter on ``kb_tag`` list-membership to scope to THIS KB. ``document_tag``
    is path-only, so the same relative path in another KB shares the doc_tag —
    the ``kb_tag`` filter is what prevents cross-KB deletion. ``include_archived=True``
    matches the prior status-agnostic ``is_deleted = 0`` SELECT it replaces, and
    EXACT list-membership replaces the old ``tags::text LIKE`` substring match
    (removing the prefix false-match risk). Scan is bounded by the chunks
    sharing this doc_tag.

    Returns the IDs of the chunk rows that were deleted (so callers can
    reconcile install-record `memory_ids` lists).
    """
    kb_tag = kb_id_tag(name)
    doc_tag = document_tag(relative_path)
    tag_result = memory_service.get_memories_by_tag(
        tag=doc_tag, include_archived=True,
    )
    memories = tag_result.get("memories", [])
    chunk_ids: list[str] = [
        str(memory["id"])
        for memory in memories
        if isinstance(memory, dict)
        and memory.get("id")
        and kb_tag in (memory.get("tags") or [])
    ]
    if chunk_ids:
        delete_kb_chunks(chunk_ids, memory_service)
    return chunk_ids


def is_source_stale(name: str, record: dict[str, Any], kb_root: Path | None) -> bool:
    """Check if any ingestable source file has been modified since last install.

    Walks only the files that ``collect_files`` would actually ingest — same
    manifest include/exclude contract — so build artifacts (``__pycache__``,
    ``.pyc``), VCS metadata (``.git/``), archives, and any other non-content
    files cannot trigger spurious re-indexes when their mtimes drift.
    """
    if kb_root is None:
        return False

    indexed_at = record.get("indexed_at")
    if not indexed_at:
        logger.info("%s: is_source_stale('%s'): no indexed_at, stale", PLUGIN_NAME, name)
        return True

    indexed_ts = parse_indexed_timestamp(indexed_at)
    if indexed_ts is None:
        logger.info("%s: is_source_stale('%s'): invalid indexed_at", PLUGIN_NAME, name)
        return True

    kb_dir = kb_root / name
    if not kb_dir.is_dir():
        return False

    manifest = resolve_manifest(kb_dir, name, None)
    for file_path in collect_files(kb_dir, manifest):
        if file_path.stat().st_mtime > indexed_ts:
            logger.info(
                "%s: is_source_stale('%s'): %s modified after install",
                PLUGIN_NAME, name, file_path.name,
            )
            return True

    return False


def parse_indexed_timestamp(indexed_at: str) -> float | None:
    """Parse an ISO-format indexed_at string into a UTC epoch timestamp."""
    from datetime import datetime

    try:
        indexed_dt = datetime.fromisoformat(indexed_at)
        if indexed_dt.tzinfo is None:
            indexed_dt = indexed_dt.replace(tzinfo=UTC)
        return indexed_dt.timestamp()
    except (ValueError, TypeError):
        return None


def parse_memory_ids(raw: Any) -> list[str]:
    """Parse memory_ids from install record. Handles both JSON strings and lists."""
    import json

    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        parsed: list[str] = json.loads(raw)
        return parsed
    return []


def now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    from datetime import datetime

    return datetime.now(UTC).isoformat()
