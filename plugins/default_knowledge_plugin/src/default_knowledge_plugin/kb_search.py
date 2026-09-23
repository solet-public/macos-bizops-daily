"""Search and recall helpers for the default knowledge plugin.

Covers: result formatting, tiered recall, diversity fill, deduplication.
No plugin instance — services are passed as explicit parameters.
"""

from __future__ import annotations

import logging
from typing import Any

from .constants import (
    PLUGIN_NAME,
    SEARCH_EXCLUDED_KB_NAMES,
    TABLE_KNOWLEDGE_INSTALL,
    TAG_DOMAIN_OFFICIAL,
    TAG_PREFIX_DOC,
    TAG_PREFIX_KB_ID,
    TAG_PREFIX_SCOPE,
    Scope,
    process_key_tag,
)
from .kb_indexing import _SOURCE_LINE_PREFIX, extract_article_metadata, extract_article_title
from .layer_filter import LayerConstraint

logger = logging.getLogger(__name__)

_DOCUMENT_DEDUP_HEADROOM = 5
_SEARCH_MIN_TOP_K = 12
_TITLE_CHUNK_SCORE_THRESHOLD = 0.85
_KB_DIVERSITY_SCORE_RATIO = 0.35
_DIVERSITY_RECALL_DEPTH = 50

_DOCUMENT_METADATA_KEYS = ("article_role", "article_tags", "title")


# ---------------------------------------------------------------------------
# Document title extraction from indexed chunk content
# ---------------------------------------------------------------------------

def extract_document_title(content: str) -> str:
    """Extract parent document title from the ``Source:`` metadata line."""
    for line in content.split("\n", 5):
        stripped = line.strip()
        if stripped.startswith(_SOURCE_LINE_PREFIX):
            after_prefix = stripped[len(_SOURCE_LINE_PREFIX):]
            if " — " in after_prefix:
                return after_prefix.split(" — ", 1)[1].strip()
    return ""


def is_title_chunk(content: str) -> bool:
    """Return True if content begins with a top-level markdown header."""
    for line in content.split("\n", 10):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith((_SOURCE_LINE_PREFIX, "Article Role:", "Article Tags:")):
            continue
        return stripped.startswith("# ") and not stripped.startswith("## ")
    return False


def propagate_document_metadata(
    title_chunk: dict[str, Any], target: dict[str, Any],
) -> None:
    """Copy document-level metadata from title chunk to a non-title representative."""
    for key in _DOCUMENT_METADATA_KEYS:
        if key in title_chunk:
            target[key] = title_chunk[key]


def pick_document_representative(
    doc_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Select the best chunk to represent a document in search results."""
    if len(doc_results) == 1:
        return doc_results[0]
    doc_results.sort(key=lambda r: float(r.get("score", 0.0)), reverse=True)
    best = doc_results[0]
    best_score = float(best.get("score", 0.0))
    if is_title_chunk(best.get("content", "")) or best_score <= 0:
        return best
    title_chunk: dict[str, Any] | None = None
    for candidate in doc_results:
        if is_title_chunk(candidate.get("content", "")):
            candidate_score = float(candidate.get("score", 0.0))
            if candidate_score / best_score >= _TITLE_CHUNK_SCORE_THRESHOLD:
                return candidate
            title_chunk = candidate
            break
    if title_chunk is not None:
        propagate_document_metadata(title_chunk, best)
    return best


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------

def extract_provenance(mem_tags: list[str]) -> tuple[str, str]:
    """Extract (kb_name, file_path) from memory tags."""
    kb_name = ""
    file_path = ""
    for tag in mem_tags:
        if tag.startswith(TAG_PREFIX_KB_ID):
            kb_name = tag[len(TAG_PREFIX_KB_ID):]
        elif tag.startswith(TAG_PREFIX_DOC):
            file_path = tag[len(TAG_PREFIX_DOC):]
    return kb_name, file_path


def format_search_result(mem: dict[str, Any], tier: str) -> dict[str, Any]:
    """Format a memory recall result into a search result with provenance."""
    mem_tags: list[str] = mem.get("tags", [])
    kb_name, file_path = extract_provenance(mem_tags)
    content = mem.get("content", "")
    article_metadata = extract_article_metadata(content)
    title = extract_document_title(content) or extract_article_title(content)
    result: dict[str, Any] = {
        "content": content,
        "knowledge_base": kb_name,
        "file_path": file_path,
        "score": mem.get("final_score", mem.get("similarity", 0.0)),
        "tier": tier,
        "memory_id": str(mem.get("id", "")),
        "article_role": article_metadata.role,
        "article_tags": article_metadata.tags,
        "knowledge_layer": article_metadata.knowledge_layer,
    }
    if title:
        result["title"] = title
    return result


# ---------------------------------------------------------------------------
# Filtering, scoring, deduplication
# ---------------------------------------------------------------------------

def filter_recall_memories(
    memories: list[dict[str, Any]],
    active_set: set[str],
    seen_ids: set[str],
    top_k: int,
    tier: str,
) -> list[dict[str, Any]]:
    """Filter recalled memories to active KBs, dedup, and format. Mutates seen_ids."""
    results: list[dict[str, Any]] = []
    for mem in memories:
        if len(results) >= top_k:
            break
        mid = str(mem.get("id", ""))
        if not mid or mid in seen_ids:
            continue
        kb_name, _ = extract_provenance(mem.get("tags", []))
        if kb_name not in active_set:
            continue
        seen_ids.add(mid)
        results.append(format_search_result(mem, tier))
    return results


def apply_min_score(
    results: list[dict[str, Any]], min_score: float | None,
) -> list[dict[str, Any]]:
    """Filter out semantic-tier results below min_score. Tag matches always kept."""
    if min_score is None:
        return results
    return [
        r for r in results
        if r.get("tier") != "semantic" or r.get("score", 0.0) >= min_score
    ]


def deduplicate_by_document(
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep one representative chunk per source document."""
    doc_groups: dict[str, list[dict[str, Any]]] = {}
    no_doc: list[dict[str, Any]] = []
    for result in results:
        doc = result.get("file_path", "")
        if doc:
            doc_groups.setdefault(doc, []).append(result)
        else:
            no_doc.append(result)

    representatives: list[dict[str, Any]] = list(no_doc)
    for doc_results in doc_groups.values():
        representatives.append(pick_document_representative(doc_results))

    representatives.sort(key=lambda r: float(r.get("score", 0.0)), reverse=True)
    return representatives


# ---------------------------------------------------------------------------
# Recall helpers
# ---------------------------------------------------------------------------

def recall_with_layer_or(
    query: str,
    base_tags: list[str],
    top_k: int,
    seen_ids: set[str],
    layer_constraint: LayerConstraint | None,
    memory_service: Any,
) -> list[dict[str, Any]]:
    """Run recall(s) and merge by memory id (OR-via-N-recalls for layer constraints)."""
    if layer_constraint is None or not layer_constraint.active:
        recall_result = memory_service.recall(
            query=query,
            top_k=top_k * 3,
            tags=base_tags,
            exclude_ids=list(seen_ids) if seen_ids else None,
            score_by_similarity=True,
        )
        return list(recall_result.get("memories", []))

    merged: dict[str, dict[str, Any]] = {}
    for layer_tag in layer_constraint.tag_alternatives():
        recall_result = memory_service.recall(
            query=query,
            top_k=top_k * 3,
            tags=[*base_tags, layer_tag],
            exclude_ids=list(seen_ids) if seen_ids else None,
            score_by_similarity=True,
        )
        for mem in recall_result.get("memories", []):
            mid = str(mem.get("id", ""))
            if not mid:
                continue
            existing = merged.get(mid)
            if existing is None:
                merged[mid] = mem
                continue
            new_score = float(mem.get("final_score", mem.get("similarity", 0.0)))
            old_score = float(existing.get("final_score", existing.get("similarity", 0.0)))
            if new_score > old_score:
                merged[mid] = mem

    return sorted(
        merged.values(),
        key=lambda m: float(m.get("final_score", m.get("similarity", 0.0))),
        reverse=True,
    )


def recall_from_active_kbs(
    query: str,
    active_names: list[str],
    top_k: int,
    seen_ids: set[str],
    memory_service: Any,
    extra_tags: list[str] | None = None,
    layer_constraint: LayerConstraint | None = None,
) -> list[dict[str, Any]]:
    """Recall memories across active KBs with optional tag and layer filters."""
    tier = "process_key" if extra_tags else "semantic"
    active_set = set(active_names)

    if extra_tags:
        base_tags = list(extra_tags)
    else:
        kb_scope_tag = f"{TAG_PREFIX_SCOPE}{Scope.WORKSPACE.value}"
        base_tags = [TAG_DOMAIN_OFFICIAL, kb_scope_tag]

    merged_memories = recall_with_layer_or(
        query=query,
        base_tags=base_tags,
        top_k=top_k,
        seen_ids=seen_ids,
        layer_constraint=layer_constraint,
        memory_service=memory_service,
    )

    return filter_recall_memories(merged_memories, active_set, seen_ids, top_k, tier)


def recall_diversity_hits(
    query: str,
    target_kbs: set[str],
    seen_ids: set[str],
    score_floor: float,
    memory_service: Any,
    layer_constraint: LayerConstraint | None = None,
) -> list[dict[str, Any]]:
    """Recall best unseen result from each target KB above score_floor."""
    kb_scope = f"{TAG_PREFIX_SCOPE}{Scope.WORKSPACE.value}"
    base_tags = [TAG_DOMAIN_OFFICIAL, kb_scope]
    merged_memories = recall_with_layer_or(
        query=query,
        base_tags=base_tags,
        top_k=_DIVERSITY_RECALL_DEPTH // 3,
        seen_ids=seen_ids,
        layer_constraint=layer_constraint,
        memory_service=memory_service,
    )

    remaining = set(target_kbs)
    hits: list[dict[str, Any]] = []
    for mem in merged_memories:
        if not remaining:
            break
        score = float(mem.get("final_score", mem.get("similarity", 0.0)))
        kb_name, _ = extract_provenance(mem.get("tags", []))
        if score < score_floor:
            if kb_name in remaining:
                logger.info(
                    "KB_DIVERSITY: rejected %s (score=%.3f < floor=%.3f)",
                    kb_name, score, score_floor,
                )
            break
        mid = str(mem.get("id", ""))
        if not mid or mid in seen_ids:
            continue
        if kb_name not in remaining:
            continue
        seen_ids.add(mid)
        remaining.discard(kb_name)
        hits.append(format_search_result(mem, "diversity"))
    return hits


def fill_kb_diversity(
    query: str,
    results: list[dict[str, Any]],
    searchable: list[str],
    seen_ids: set[str],
    memory_service: Any,
    layer_constraint: LayerConstraint | None = None,
) -> list[dict[str, Any]]:
    """Add one result per underrepresented KB if it meets the score floor."""
    if not results or len(searchable) < 2:
        return results

    represented = {r.get("knowledge_base", "") for r in results}
    underrepresented = {n for n in searchable if n not in represented}
    if not underrepresented:
        return results

    best_score = max(float(r.get("score", 0.0)) for r in results)
    score_floor = best_score * _KB_DIVERSITY_SCORE_RATIO
    logger.info(
        "KB_DIVERSITY: represented=%s underrepresented=%d best=%.3f floor=%.3f",
        sorted(represented), len(underrepresented), best_score, score_floor,
    )

    diversity_hits = recall_diversity_hits(
        query, underrepresented, seen_ids, score_floor,
        memory_service=memory_service, layer_constraint=layer_constraint,
    )
    logger.info(
        "KB_DIVERSITY: added %d hits from %s",
        len(diversity_hits),
        [h.get("knowledge_base", "?") for h in diversity_hits],
    )
    results.extend(diversity_hits)
    results.sort(key=lambda r: float(r.get("score", 0.0)), reverse=True)
    return results


def collect_tiered_results(
    query: str,
    active_names: list[str],
    top_k: int,
    process_key: str | None,
    tags: list[str] | None,
    memory_service: Any,
    *,
    honor_exclusions: bool,
    layer_constraint: LayerConstraint | None = None,
) -> list[dict[str, Any]]:
    """Run tiered search: process_key → tag → semantic → KB diversity.

    ``honor_exclusions`` is a REQUIRED keyword-only parameter with no default
    (Architect Q1 condition C2): a default would silently decide default-scope
    exclusion policy for a future call site. When True, ``SEARCH_EXCLUDED_KB_NAMES``
    are dropped from the semantic (Tier 3) AND diversity tiers (both read the
    ``searchable`` list); Tiers 1–2 always include every active KB. When False
    (an explicit ``name=`` scope), the exclusion is bypassed entirely, so a
    name-scoped semantic query of an excluded KB returns results instead of
    being empty-by-construction.
    """
    results: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    if process_key:
        pk_tag = process_key_tag(process_key)
        results.extend(recall_from_active_kbs(
            query, active_names, top_k, seen_ids, memory_service,
            extra_tags=[pk_tag], layer_constraint=layer_constraint,
        ))

    if tags and len(results) < top_k:
        tier2 = recall_from_active_kbs(
            query, active_names, top_k - len(results), seen_ids, memory_service,
            extra_tags=tags, layer_constraint=layer_constraint,
        )
        for r in tier2:
            r["tier"] = "tag"
        results.extend(tier2)

    if honor_exclusions:
        searchable = [n for n in active_names if n not in SEARCH_EXCLUDED_KB_NAMES]
    else:
        searchable = list(active_names)

    if len(results) < top_k and searchable:
        semantic_budget = max(top_k * 4, top_k - len(results) + _DOCUMENT_DEDUP_HEADROOM)
        results.extend(recall_from_active_kbs(
            query, searchable, semantic_budget, seen_ids, memory_service,
            layer_constraint=layer_constraint,
        ))

    results = deduplicate_by_document(results)
    results = results[:top_k]

    results = fill_kb_diversity(
        query, results, searchable, seen_ids,
        memory_service=memory_service, layer_constraint=layer_constraint,
    )
    return results


def get_active_names(state_service: Any) -> list[str]:
    """Get names of all active knowledge bases."""
    result = state_service.read_state(
        namespace=PLUGIN_NAME,
        query={"table": TABLE_KNOWLEDGE_INSTALL, "filters": {"is_active": 1}},
    )
    rows = result.get("data", {}).get("records", [])
    return [str(row["name"]) for row in rows if isinstance(row, dict)]
