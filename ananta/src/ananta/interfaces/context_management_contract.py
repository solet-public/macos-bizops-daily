"""Context Management Contract - Plugin interface for context management."""

from abc import ABC, abstractmethod

from ananta.core.domain.types import ActionResult
from ananta.error_handling import PluginError
from ananta.services.context_management.compaction_types import (
    CompactionRequest,
    WarmingRequest,
)
from ananta.services.context_management.config import ContextManagementConfig


class ContextManagementContract(ABC):
    """Contract for plugins that support context management.

    All plugins implementing this interface must provide:
    - get_context_management_config(): Returns the plugin's context management configuration

    Plugins that declare supports_compaction=True must also implement:
    - generate_compaction_summary(): Generates a summary from messages

    Plugins that declare warming_enabled=True must also implement:
    - warm_cache(): Warms the KV cache with context

    Plugins that declare supports_clear=True must also implement:
    - clear_context(): Clears the context stream for a given context_id
    """

    @abstractmethod
    def get_context_management_config(self) -> ContextManagementConfig:
        """Get context management configuration. Required."""
        ...

    def generate_compaction_summary(self, request: CompactionRequest) -> str:
        """Generate summary from messages. Required if supports_compaction=True.

        Use request.max_tokens and request.temperature for inference.

        Args:
            request: The compaction request with messages and config.

        Returns:
            Generated summary text.

        Raises:
            PluginError: If summary generation fails.
        """
        raise PluginError(
            message="generate_compaction_summary not implemented but supports_compaction=True",
            error_code="plugin.compaction.not_implemented",
        )

    def warm_cache(self, request: WarmingRequest) -> bool:
        """Warm KV cache with context. Required if warming_enabled=True.

        Use request.max_tokens and request.temperature for inference.
        MUST raise on failure - no silent fallbacks.

        Args:
            request: The warming request with messages and config.

        Returns:
            True if warming succeeded.

        Raises:
            PluginError: If warming fails.
        """
        raise PluginError(
            message="warm_cache not implemented but warming_enabled=True",
            error_code="plugin.warming.not_implemented",
        )

    def clear_context(self, context_id: str, reason: str) -> ActionResult:
        """Clear all events and snapshots. Required if supports_clear=True.

        Args:
            context_id: The context to clear.
            reason: Why the context is being cleared.

        Returns:
            ActionResult indicating success or failure.

        Raises:
            PluginError: If clear fails.
        """
        raise PluginError(
            message="clear_context not implemented but supports_clear=True",
            error_code="plugin.clear.not_implemented",
        )
