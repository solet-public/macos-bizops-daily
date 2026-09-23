"""ACT-R Memory System Constants.

These constants control the behavior of the ACT-R memory system including
decay rates, consolidation thresholds, and spaced repetition intervals.
"""

# ─────────────────────────────────────────────────────────────────────────────
# ACT-R Activation Parameters
# ─────────────────────────────────────────────────────────────────────────────

# Standard ACT-R decay factor (d parameter)
# Higher values = faster decay
# 0.5 is the standard value from ACT-R literature
DECAY_FACTOR: float = 0.5

# Minimum age in hours to avoid division by zero in activation calculation
MIN_AGE_HOURS: float = 0.1

# ─────────────────────────────────────────────────────────────────────────────
# Retrieval Parameters
# ─────────────────────────────────────────────────────────────────────────────

# Default number of results to return from recall
# Experiments show top_k=5 achieves same hit rate as 10 with better precision (80% vs 65%)
# and half the tokens, improving efficiency from 0.122 to 0.266
DEFAULT_TOP_K: int = 5

# Number of top results that get their retrieval recorded
# (strengthening those memories)
RETRIEVAL_BOOST_TOP_N: int = 5

# ─────────────────────────────────────────────────────────────────────────────
# Consolidation Thresholds
# ─────────────────────────────────────────────────────────────────────────────

# Episodic memories weaker than this are candidates for consolidation
# ln(1) = 0, so -1.5 means memories that have decayed significantly
EPISODIC_CONSOLIDATION_THRESHOLD: float = -1.5

# Semantic L1 memories weaker than this become L2 candidates
SEMANTIC_L1_CONSOLIDATION_THRESHOLD: float = -2.5

# Semantic L2 memories weaker than this get archived
SEMANTIC_L2_ARCHIVE_THRESHOLD: float = -3.5

# Minimum age in days before a memory can be consolidated
MIN_AGE_FOR_CONSOLIDATION_DAYS: int = 7

# Minimum cluster size for consolidation (don't summarize singletons)
MIN_CLUSTER_SIZE: int = 3

# ─────────────────────────────────────────────────────────────────────────────
# Document Chunking Parameters
# ─────────────────────────────────────────────────────────────────────────────

# Hard limit for embedding models (chars)
# Empirically tested with nomic-embed-text v1.5 at 2048 token context:
#   512 chars → 75% Top1 retrieval accuracy
#  1024 chars → 75% Top1
#  2000 chars → 83% Top1
#  3000 chars → 100% Top1, 100% Top5, 87.5% keyword recall (best)
#  4000 chars → 100% Top1 but keyword recall slightly decreases
# 3000 chars uses ~750 tokens (~37% of 2048 context) — well within capacity.
# Matches the default max_chars in knowledge base manifests.
# Test: plugins/default_inference_plugin/research/prompt_replay/test_knowledge_base_chunk_size.py
EMBEDDING_MAX_CHARS: int = 3000

# Approximate chars per token for size estimation
CHARS_PER_TOKEN: int = 4

# Target chunk size in tokens
# Actual char limit = (DEFAULT_CHUNK_SIZE - DEFAULT_CHUNK_OVERLAP) * CHARS_PER_TOKEN
# This leaves room for overlap to be prepended to next chunk
DEFAULT_CHUNK_SIZE: int = 256

# Token overlap between chunks for context continuity
DEFAULT_CHUNK_OVERLAP: int = 64

# ─────────────────────────────────────────────────────────────────────────────
# Memorization (Spaced Repetition) Parameters
# ─────────────────────────────────────────────────────────────────────────────

# Initial interval before first review (in days)
INITIAL_INTERVAL_DAYS: float = 1.0

# Multiplier for interval after each successful review
# 2.0 means: 1 day -> 2 days -> 4 days -> 8 days -> ...
INTERVAL_MULTIPLIER: float = 2.0

# Maximum interval between reviews (cap to ensure periodic reinforcement)
MAX_INTERVAL_DAYS: float = 30.0

# Number of successful reviews before memorization is "complete"
# After this many reviews, the memory is considered well-learned
MEMORIZATION_COMPLETE_REVIEWS: int = 7

# ─────────────────────────────────────────────────────────────────────────────
# Vector Storage Namespaces
# ─────────────────────────────────────────────────────────────────────────────

# Namespace for memory vectors in vector_service
# Uses the pgvector plugin's namespace where the embeddings table is created
VECTOR_NAMESPACE: str = "pgvector_service_plugin"

# NOTE: Memory storage now uses relational tables in the actr_memory_plugin namespace
# Schema defined in ananta/services/memory_service/schema.py
# Tables: actr_memory_plugin__memory, actr_memory_plugin__memorization
