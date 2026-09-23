"""Context Management Configuration.

Plugin configuration for context management behavior.
ALL fields are required. Plugin MUST fail to load if any field missing.
ALL fields MUST be used by platform logic.

Compaction is purely char-count based (no event count limits).
"""

from dataclasses import dataclass

from ananta.services.context_management.types import ContextIdSource, ContextMode


@dataclass(frozen=True, slots=True)
class ContextManagementConfig:
    """Plugin configuration for context management.

    ALL fields are required. Plugin MUST fail to load if any field missing.
    ALL fields MUST be used by platform logic.

    Field usage map (v21 - char-count only):
    - context_mode: Determines platform vs delegated handling
    - context_id_source: How to resolve context_id
    - context_id_address_key: Key for address_book source (None if not address_book)
    - supports_compaction: Enables compaction feature
    - supports_clear: Enables clear feature
    - auto_compact: Triggers compaction automatically
    - warming_enabled: Enables cache warming after compaction
    - max_char_count: Hard limit - fail if exceeded
    - soft_max_char_count: Trigger compaction
    - target_char_count: Compaction plan - summary budget calculation
    - precache_char_count: Warming - char limit for warming
    - warm_max_tokens: Warming - inference max_tokens
    - warm_temperature: Warming - inference temperature
    - summary_temperature: Compaction - inference temperature
    - chars_per_token: Compaction - convert chars to tokens
    - min_summary_tokens: Compaction - minimum token budget for summary
    - discovery_intent_max_tokens: Discovery - intent analysis max tokens
    - discovery_intent_temperature: Discovery - intent analysis temperature
    - discovery_min_similarity_threshold: Discovery - vector search similarity threshold
    - attachment_scan_limit: ContextStage - max memory records to scan for attachments
    """

    # Core settings
    context_mode: ContextMode
    context_id_source: ContextIdSource
    context_id_address_key: str | None

    # Capability flags
    supports_compaction: bool
    supports_clear: bool
    auto_compact: bool
    warming_enabled: bool

    # Hard threshold (fail if exceeded) - char-based only
    max_char_count: int

    # Soft threshold (trigger compaction) - char-based only
    soft_max_char_count: int

    # Compaction target (summary budget calculation) - char-based only
    target_char_count: int

    # Cache warming
    precache_char_count: int
    warm_max_tokens: int
    warm_temperature: float

    # Summary generation
    summary_temperature: float
    chars_per_token: int
    min_summary_tokens: int

    # Discovery intent analysis
    discovery_intent_max_tokens: int
    discovery_intent_temperature: float

    # Discovery similarity threshold
    discovery_min_similarity_threshold: float

    # Attachment scanning for context stage (limits memory records, not context events)
    attachment_scan_limit: int

    # Model context window budget (tokens).  The guard in
    # ``inference_transaction`` uses this to reject or trim prompts
    # that would overflow the model and produce 0 output tokens.
    model_context_tokens: int


# INF-03 declared-vacant provider (2026-07-04): the ONE defined config for the
# no-provider state. A vacant inference_service has no plugin to supply this
# config, but the boot-time consumers (DiscoveryService threshold, the
# ContextService/cold-context briefing pipeline) are provider-INDEPENDENT and
# must keep working. Provenance: char/threshold/budget values copied verbatim
# from default_inference_plugin's shipped config (profile/config/plugins/
# default_inference_plugin.json, 2026-07-04) — the de-facto platform values;
# the inference-capability flags (compaction/warming/auto-compact) are False
# because those are provider operations a vacant service cannot perform.
# Real providers ALWAYS supply their own config; this constant is returned
# ONLY for the declared-vacant state and never merges with a provider's.
VACANT_PROVIDER_CONTEXT_CONFIG = ContextManagementConfig(
    context_mode=ContextMode.PLATFORM,
    context_id_source=ContextIdSource.PLUGIN_ROOT,
    context_id_address_key=None,
    supports_compaction=False,
    supports_clear=True,
    auto_compact=False,
    warming_enabled=False,
    max_char_count=262144,
    soft_max_char_count=60000,
    target_char_count=20000,
    precache_char_count=10000,
    warm_max_tokens=1,
    warm_temperature=0.0,
    summary_temperature=0.1,
    chars_per_token=4,
    min_summary_tokens=500,
    discovery_intent_max_tokens=400,
    discovery_intent_temperature=0.0,
    discovery_min_similarity_threshold=0.5,
    attachment_scan_limit=10,
    model_context_tokens=32768,
)
