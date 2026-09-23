"""Provider Abstraction - Base classes and provider management.

This package provides the provider abstraction layer including:
- ProviderManager: Provider resolution and lifecycle management
- BaseProvider: Base class for all providers
- BaseToolProvider: Base class for tool providers

The providers package handles provider abstraction, allowing flexible
implementation of different provider types (LLM, tools, etc.).
"""

from ananta.core.providers.base_provider import BaseProvider
from ananta.core.providers.base_tool_provider import BaseToolProvider, ToolSchema
from ananta.core.providers.provider_manager import (
    OrchestratorProtocol,
    PluginManagerProtocol,
    ProviderManager,
)

__all__ = [
    # Base Classes
    "BaseProvider",
    "BaseToolProvider",
    "ToolSchema",
    # Provider Management
    "ProviderManager",
    "OrchestratorProtocol",
    "PluginManagerProtocol",
]
