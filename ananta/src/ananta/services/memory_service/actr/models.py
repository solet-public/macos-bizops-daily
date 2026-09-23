"""ACT-R Memory System Data Models.

TypedDict definitions for memory records and memorization tracking.
"""

from typing import TypedDict


class Memory(TypedDict, total=False):
    """A single memory unit in the ACT-R system.

    Attributes:
        id: Unique identifier (UUID)
        content: The actual memory text
        created_at: ISO timestamp of creation
        retrieval_times: List of ISO timestamps when memory was retrieved
        strength: Computed ACT-R activation level
        retrieval_count: Total number of retrievals
        memory_type: 'episodic', 'semantic_l1', or 'semantic_l2'
        status: 'active' or 'archived'
        source_memory_ids: For semantic memories, IDs of source episodic memories
        source_file: For ingested docs, path to source file
        source_lines: Line range in source file (start, end)
        session_id: Session that created this memory
        tags: User-defined tags for organization
    """

    # Identity
    id: str

    # Content
    content: str

    # Temporal
    created_at: str
    retrieval_times: list[str]

    # ACT-R
    strength: float
    retrieval_count: int

    # Lifecycle
    memory_type: str  # 'episodic' | 'semantic_l1' | 'semantic_l2'
    status: str  # 'active' | 'archived'

    # Lineage
    source_memory_ids: list[str]
    source_file: str | None
    source_lines: tuple[int, int] | None

    # Organization
    session_id: str | None
    tags: list[str]


class MemorizationRecord(TypedDict, total=False):
    """Tracks intentional memorization of a memory via spaced repetition.

    Attributes:
        memory_id: ID of the memory being memorized
        started_at: ISO timestamp when memorization began
        review_count: Number of completed reviews
        last_review_at: ISO timestamp of last review
        next_review_at: ISO timestamp when next review is due
        interval_days: Current interval between reviews
        status: 'active', 'paused', 'completed', or 'orphaned'
    """

    memory_id: str
    started_at: str
    review_count: int
    last_review_at: str | None
    next_review_at: str
    interval_days: float
    status: str  # 'active' | 'paused' | 'completed' | 'orphaned'


class MemorySearchResult(TypedDict):
    """A memory with search-related metadata.

    Used in recall results to provide scoring information.
    """

    id: str
    content: str
    memory_type: str
    strength: float
    similarity: float
    final_score: float
    created_at: str
    retrieval_count: int
    tags: list[str]


class ConsolidationAction(TypedDict):
    """Record of a consolidation action taken.

    Used in consolidate() results to describe what was done.
    """

    action: str  # 'consolidated' | 'would_consolidate'
    source_count: int
    new_memory_id: str | None
    summary_preview: str
