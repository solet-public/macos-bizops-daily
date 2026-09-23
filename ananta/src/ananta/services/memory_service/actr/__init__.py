"""ACT-R Memory System Components.

This module provides the cognitive architecture components for the long-term
memory system based on ACT-R (Adaptive Control of Thought-Rational).

Components:
- activation: ACT-R base-level activation (strength) computation
- chunking: Document chunking for ingestion
- consolidation: Memory clustering and summarization
- constants: System constants and thresholds
- models: TypedDict definitions for Memory and MemorizationRecord
"""

from .activation import compute_strength
from .chunking import chunk_by_turns, chunk_code_file, chunk_document
from .constants import (
    DECAY_FACTOR,
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_TOP_K,
    EPISODIC_CONSOLIDATION_THRESHOLD,
    INITIAL_INTERVAL_DAYS,
    INTERVAL_MULTIPLIER,
    MAX_INTERVAL_DAYS,
    MEMORIZATION_COMPLETE_REVIEWS,
    MIN_AGE_FOR_CONSOLIDATION_DAYS,
    MIN_AGE_HOURS,
    MIN_CLUSTER_SIZE,
    RETRIEVAL_BOOST_TOP_N,
    SEMANTIC_L1_CONSOLIDATION_THRESHOLD,
    SEMANTIC_L2_ARCHIVE_THRESHOLD,
)
from .models import MemorizationRecord, Memory

__all__ = [
    # Activation
    "compute_strength",
    # Chunking
    "chunk_code_file",
    "chunk_document",
    "chunk_by_turns",
    # Constants
    "DECAY_FACTOR",
    "MIN_AGE_HOURS",
    "DEFAULT_TOP_K",
    "RETRIEVAL_BOOST_TOP_N",
    "EPISODIC_CONSOLIDATION_THRESHOLD",
    "SEMANTIC_L1_CONSOLIDATION_THRESHOLD",
    "SEMANTIC_L2_ARCHIVE_THRESHOLD",
    "MIN_AGE_FOR_CONSOLIDATION_DAYS",
    "MIN_CLUSTER_SIZE",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_CHUNK_OVERLAP",
    "INITIAL_INTERVAL_DAYS",
    "INTERVAL_MULTIPLIER",
    "MAX_INTERVAL_DAYS",
    "MEMORIZATION_COMPLETE_REVIEWS",
    # Models
    "Memory",
    "MemorizationRecord",
]
