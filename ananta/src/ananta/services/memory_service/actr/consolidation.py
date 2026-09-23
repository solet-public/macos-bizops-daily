"""Memory Consolidation Logic.

Handles clustering of weak memories and summarization into semantic memories.
This mimics the memory consolidation process in biological memory systems.
"""

from typing import Any

from .constants import MIN_CLUSTER_SIZE


def cluster_memories(
    memories: list[dict[str, Any]],
    similarity_threshold: float = 0.7,
) -> list[list[dict[str, Any]]]:
    """Cluster memories by semantic similarity.

    Uses a simple approach for MVP:
    1. Group by session_id if available
    2. Group by common tags
    3. Remaining memories form a single cluster

    For production, consider using proper clustering (K-means, HDBSCAN, etc.)
    with actual embedding similarity.

    Args:
        memories: List of memory dicts to cluster
        similarity_threshold: Minimum similarity for clustering (future use)

    Returns:
        List of clusters, where each cluster is a list of memory dicts
    """
    if not memories:
        return []

    clusters: list[list[dict[str, Any]]] = []
    no_session = _cluster_by_session(memories, clusters)
    _cluster_by_tags(no_session, clusters)

    return clusters


def _cluster_by_session(
    memories: list[dict[str, Any]], clusters: list[list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Group memories by session_id.

    Args:
        memories: All memories to group
        clusters: Output list to append valid clusters to

    Returns:
        Memories without sessions or in too-small session groups
    """
    by_session: dict[str, list[dict[str, Any]]] = {}
    no_session: list[dict[str, Any]] = []

    for memory in memories:
        session_id = memory.get("session_id")
        if session_id:
            by_session.setdefault(session_id, []).append(memory)
        else:
            no_session.append(memory)

    for session_memories in by_session.values():
        if len(session_memories) >= MIN_CLUSTER_SIZE:
            clusters.append(session_memories)
        else:
            no_session.extend(session_memories)

    return no_session


def _cluster_by_tags(memories: list[dict[str, Any]], clusters: list[list[dict[str, Any]]]) -> None:
    """Group memories by primary tag.

    Args:
        memories: Memories without session grouping
        clusters: Output list to append valid clusters to
    """
    if not memories:
        return

    by_tag: dict[str, list[dict[str, Any]]] = {}
    untagged: list[dict[str, Any]] = []

    for memory in memories:
        tags = memory.get("tags", [])
        if tags:
            by_tag.setdefault(tags[0], []).append(memory)
        else:
            untagged.append(memory)

    for tag_memories in by_tag.values():
        if len(tag_memories) >= MIN_CLUSTER_SIZE:
            clusters.append(tag_memories)
        else:
            untagged.extend(tag_memories)

    if len(untagged) >= MIN_CLUSTER_SIZE:
        clusters.append(untagged)


def generate_summary(
    contents: list[str],
    inference_service: Any,
    max_tokens: int = 500,
) -> str:
    """Generate a summary of multiple memory contents.

    Uses the inference service to create a consolidated summary.

    Args:
        contents: List of memory content strings
        inference_service: Service for LLM inference
        max_tokens: Maximum tokens in summary

    Returns:
        Summary string
    """
    combined = "\n\n---\n\n".join(contents)

    # Truncate if too long to avoid context limits
    max_input_chars = 8000  # Roughly 2000 tokens
    if len(combined) > max_input_chars:
        combined = combined[:max_input_chars] + "\n\n[... truncated ...]"

    prompt = f"""Summarize the following related pieces of information into a single,
coherent summary. Preserve key facts, insights, and any important details.
Be concise but complete. Do not add information that isn't present in the original.

CONTENT TO SUMMARIZE:
{combined}

SUMMARY:"""

    try:
        result = inference_service.process(
            {
                "prompt": prompt,
                "max_tokens": max_tokens,
                "temperature": 0.3,  # Low temperature for factual summary
            }
        )

        summary = str(result.get("response", "")).strip()

        if not summary:
            # Fallback: just concatenate truncated versions
            summary = " | ".join([c[:100] + "..." for c in contents[:5]])

        return summary

    except Exception as e:
        # Fallback on error
        return f"[Consolidation failed: {e}] " + " | ".join([c[:50] for c in contents[:3]])


def generate_summary_sync(
    contents: list[str],
    summarizer_func: Any | None = None,
) -> str:
    """Generate a summary without requiring inference_service.

    Provides a simple extractive summary as fallback when no LLM is available.

    Args:
        contents: List of memory content strings
        summarizer_func: Optional custom summarization function

    Returns:
        Summary string
    """
    if summarizer_func:
        return str(summarizer_func(contents))

    # Simple extractive summary: take first sentence of each
    sentences = []
    for content in contents:
        # Find first sentence
        for end_char in [".", "!", "?"]:
            idx = content.find(end_char)
            if idx > 0 and idx < 200:
                sentences.append(content[: idx + 1].strip())
                break
        else:
            # No sentence boundary found, take first 100 chars
            sentences.append(content[:100].strip() + "...")

    return " ".join(sentences[:5])


def should_consolidate(
    memory: dict[str, Any],
    strength: float,
    threshold: float,
    min_age_days: int,
    created_at_parsed: Any,  # datetime
    current_time: Any,  # datetime
) -> bool:
    """Determine if a memory should be consolidated.

    Args:
        memory: Memory dict
        strength: Current activation strength
        threshold: Strength threshold for consolidation
        min_age_days: Minimum age in days
        created_at_parsed: Parsed creation timestamp
        current_time: Current time

    Returns:
        True if memory should be consolidated
    """
    from datetime import timedelta

    # Check strength threshold
    if strength >= threshold:
        return False

    # Check age
    age = current_time - created_at_parsed
    if age < timedelta(days=min_age_days):
        return False

    # Check status
    if memory.get("status") != "active":
        return False

    return True
