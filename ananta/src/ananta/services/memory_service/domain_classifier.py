"""Domain classification for tool uses based on process_key patterns.

This module classifies process keys into domains to enable domain-based
filtering of tool use memories. The classification is based on prefix
patterns in the process_key string.

Example:
    >>> classify_domain("service_interface::state_service::read_state")
    ToolUseDomain.STATE
    >>> classify_domain("plugin::audio_processing_plugin::generate_audio")
    ToolUseDomain.AUDIO
"""

from ananta.services.memory_service.tool_use_types import ToolUseDomain

# Process key prefix to domain mapping
# Order matters: more specific patterns should come before general ones
_DOMAIN_PATTERNS: tuple[tuple[str, ToolUseDomain], ...] = (
    # Service interfaces
    ("service_interface::state_service", ToolUseDomain.STATE),
    ("service_interface::discovery_service", ToolUseDomain.DISCOVERY),
    ("service_interface::memory_service", ToolUseDomain.MEMORY),
    ("service_interface::inference_service", ToolUseDomain.INFERENCE),
    ("service_interface::io_interface_service", ToolUseDomain.IO),
    ("service_interface::blob_storage_service", ToolUseDomain.BLOB_STORAGE),
    ("service_interface::scheduling_service", ToolUseDomain.SCHEDULING),
    # Plugins by category
    ("plugin::audio", ToolUseDomain.AUDIO),
    ("plugin::cosyvoice", ToolUseDomain.AUDIO),
    ("plugin::ssml", ToolUseDomain.AUDIO),
    ("plugin::file", ToolUseDomain.FILE_SYSTEM),
    ("plugin::network", ToolUseDomain.NETWORK),
    ("plugin::database", ToolUseDomain.DATABASE),
    ("plugin::postgres", ToolUseDomain.DATABASE),
    ("plugin::pgvector", ToolUseDomain.DATABASE),
    ("plugin::discord", ToolUseDomain.IO),
    ("plugin::signal", ToolUseDomain.IO),
    ("plugin::slack", ToolUseDomain.IO),
    ("plugin::telegram", ToolUseDomain.IO),
    ("plugin::jsonrpc", ToolUseDomain.IO),
    ("plugin::rest", ToolUseDomain.IO),
    ("plugin::vscode", ToolUseDomain.IO),
    ("plugin::console", ToolUseDomain.IO),
    ("plugin::default_inference", ToolUseDomain.INFERENCE),
    ("plugin::openai", ToolUseDomain.INFERENCE),
    ("plugin::claude", ToolUseDomain.INFERENCE),
    ("plugin::codex", ToolUseDomain.INFERENCE),
    ("plugin::actr_memory", ToolUseDomain.MEMORY),
    ("plugin::default_blob", ToolUseDomain.BLOB_STORAGE),
    ("plugin::default_scheduling", ToolUseDomain.SCHEDULING),
    ("plugin::default_vault", ToolUseDomain.STATE),
    ("plugin::default_address", ToolUseDomain.STATE),
    ("plugin::comfyui", ToolUseDomain.OTHER),
)


def classify_domain(process_key: str) -> ToolUseDomain:
    """Classify a process_key into a domain.

    Args:
        process_key: Full process key (e.g., service_interface::state_service::read_state)

    Returns:
        ToolUseDomain classification based on prefix matching.
        Returns ToolUseDomain.OTHER if no pattern matches.
    """
    process_key_lower = process_key.lower()

    for pattern, domain in _DOMAIN_PATTERNS:
        if process_key_lower.startswith(pattern):
            return domain

    return ToolUseDomain.OTHER
