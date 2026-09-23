# File: ananta/src/ananta/core/bootstrap_manager.py
import logging

from ananta.core.plugins.plugin_manager import PluginManager
from ananta.services.action_event_bus import EventBus
from ananta.services.blob_storage_service import BlobStorageService
from ananta.services.state_service import StateService


class BootstrapManager:
    """Phase 1: Creates core services in bootstrap mode with zero dependencies"""

    def create_bootstrap_services(self) -> dict[str, object]:
        """Create all services in bootstrap mode - NO plugin dependencies"""
        return {
            "state_service": StateService(plugin_manager=None),
            "blob_storage_service": BlobStorageService(plugin_manager=None),
            "event_bus": EventBus(plugin_manager=None),
        }

    def create_plugin_manager(
        self, _services: dict[str, object]
    ) -> PluginManager:  # Reserved for interface compatibility
        """Create plugin manager with bootstrap services"""
        return PluginManager()

    def _create_bootstrap_logger(self) -> logging.Logger:
        return logging.getLogger("bootstrap")
