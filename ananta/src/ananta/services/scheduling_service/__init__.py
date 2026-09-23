"""Scheduling Service.

Provides service-interface access to the default scheduling plugin.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol, runtime_checkable

from ananta.constants import DEFAULT_SCHEDULING_PLUGIN
from ananta.core.domain.types import ActionResult
from ananta.core.plugins.plugin_manager import PluginManager
from ananta.error_handling import FrameworkError
from ananta.interfaces.bootstrappable_service_interface import BootstrappableServiceInterface

logger = logging.getLogger(__name__)


@runtime_checkable
class SchedulingPluginProtocol(Protocol):
    """Protocol describing the scheduling plugin contract used by the service."""

    def is_ready(self) -> bool: ...
    def get_readiness_error(self) -> str | None: ...

    def create_cron_schedule(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult: ...

    def execute_in_seconds(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult: ...

    def clear_scheduled_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult: ...

    def clear_scheduled_actions_by_tag(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult: ...

    def get_schedules_by_tag(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult: ...

    def ensure_global_heartbeat(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult: ...


class SchedulingService(BootstrappableServiceInterface):
    """Service wrapper that exposes scheduling operations via service interface."""

    def __init__(
        self,
        plugin_manager: PluginManager | None = None,
        scheduling_plugin_name: str | None = None,
        app_home: str = "",
    ) -> None:
        if plugin_manager is None:
            raise FrameworkError("SchedulingService requires plugin_manager")
        if not app_home:
            raise FrameworkError("SchedulingService requires app_home for plugin execution")

        self._plugin_manager: PluginManager = plugin_manager
        self.plugin_manager = plugin_manager
        self.app_home = app_home
        self._plugin_name = (
            scheduling_plugin_name
            or os.environ.get("ANANTA_SCHEDULING_PLUGIN")
            or DEFAULT_SCHEDULING_PLUGIN
        )
        self._plugin: SchedulingPluginProtocol | None = None

        super().__init__(plugin_manager)

    def _init_bootstrap(self) -> None:
        raise FrameworkError("SchedulingService does not support bootstrap mode")

    def _init_plugin(self) -> None:
        logger.debug("SchedulingService initialized with plugin '%s'", self._plugin_name)

    def _capture_bootstrap_state(self) -> dict[str, object]:
        """Scheduling service never runs in bootstrap mode."""
        return {}

    def _restore_bootstrap_data(self, data: dict[str, object]) -> None:
        """No-op; scheduling service does not persist bootstrap data."""
        if data:
            pass

    def _get_plugin_instance(self) -> SchedulingPluginProtocol:
        if self._plugin is None:
            plugin_obj = self._plugin_manager.get_plugin(self._plugin_name)
            if not isinstance(plugin_obj, SchedulingPluginProtocol):
                raise FrameworkError(
                    f"Scheduling plugin '{self._plugin_name}' does not implement the required protocol"
                )

            provider_hook = getattr(plugin_obj, "set_as_active_provider", None)
            if callable(provider_hook):
                try:
                    provider_hook("SchedulingServiceInterface")
                except Exception:
                    pass

            self._plugin = plugin_obj
        return self._plugin

    def _ensure_ready(self) -> SchedulingPluginProtocol:
        plugin = self._get_plugin_instance()
        if not plugin.is_ready():
            error = plugin.readiness_error or "Unknown readiness error"
            raise FrameworkError(f"Scheduling plugin '{self._plugin_name}' not ready: {error}")
        return plugin

    @staticmethod
    def _normalize_state(state: dict[str, Any] | None) -> dict[str, Any]:
        if not state:
            return {}
        normalized = dict(state)
        if "session_id" not in normalized and "current_session_id" in normalized:
            normalized["session_id"] = normalized["current_session_id"]
        return normalized

    @staticmethod
    def _merge_memory_tag_into_tags(
        memory_tag: str, tags: list[str] | str | None
    ) -> list[str] | str:
        if tags is None:
            return [memory_tag]
        if isinstance(tags, list):
            return tags if memory_tag in tags else [*tags, memory_tag]
        existing = {t.strip() for t in tags.split(",") if t.strip()}
        return tags if memory_tag in existing else f"{tags},{memory_tag}"

    def create_cron_schedule(
        self,
        cron_expression: str,
        actions: list[dict[str, Any]] | None = None,
        action_definitions: list[dict[str, Any]] | None = None,
        memory_tag: str | None = None,
        label: str | None = None,
        tags: list[str] | str | None = None,
        state: dict[str, Any] | None = None,
    ) -> ActionResult:
        params: dict[str, Any] = {"cron_expression": cron_expression}

        if memory_tag is not None:
            params["memory_tag"] = memory_tag
            tags = self._merge_memory_tag_into_tags(memory_tag, tags)
        if actions is not None:
            params["actions"] = actions
        if action_definitions is not None:
            params["action_definitions"] = action_definitions
        if label is not None:
            params["label"] = label
        if tags is not None:
            params["tags"] = tags

        plugin = self._ensure_ready()
        return plugin.create_cron_schedule(
            params=params,
            state=self._normalize_state(state),
        )

    def execute_in_seconds(
        self,
        seconds: int,
        actions: list[dict[str, Any]] | None = None,
        action_definitions: list[dict[str, Any]] | None = None,
        memory_tag: str | None = None,
        content: str | None = None,
        label: str | None = None,
        tags: list[str] | str | None = None,
        state: dict[str, Any] | None = None,
    ) -> ActionResult:
        params: dict[str, Any] = {"seconds": seconds}

        if memory_tag is not None:
            params["memory_tag"] = memory_tag
            tags = self._merge_memory_tag_into_tags(memory_tag, tags)
        if content is not None:
            params["content"] = content
        if actions is not None:
            params["actions"] = actions
        if action_definitions is not None:
            params["action_definitions"] = action_definitions
        if label is not None:
            params["label"] = label
        if tags is not None:
            params["tags"] = tags

        plugin = self._ensure_ready()
        return plugin.execute_in_seconds(
            params=params,
            state=self._normalize_state(state),
        )

    def clear_scheduled_action(
        self,
        schedule_id: str,
        state: dict[str, Any] | None = None,
    ) -> ActionResult:
        plugin = self._ensure_ready()
        return plugin.clear_scheduled_action(
            params={"schedule_id": schedule_id},
            state=self._normalize_state(state),
        )

    def clear_scheduled_actions_by_tag(
        self,
        tag: str,
        state: dict[str, Any] | None = None,
    ) -> ActionResult:
        plugin = self._ensure_ready()
        return plugin.clear_scheduled_actions_by_tag(
            params={"tag": tag},
            state=self._normalize_state(state),
        )

    def get_schedules_by_tag(
        self,
        tag: str,
        state: dict[str, Any] | None = None,
    ) -> ActionResult:
        plugin = self._ensure_ready()
        return plugin.get_schedules_by_tag(
            params={"tag": tag},
            state=self._normalize_state(state),
        )

    def ensure_global_heartbeat(
        self,
        cadence_minutes: int | None = None,
        tag: str | None = None,
        memory_tag: str | None = None,
        state: dict[str, Any] | None = None,
    ) -> ActionResult:
        params: dict[str, Any] = {}
        if cadence_minutes is not None:
            params["cadence_minutes"] = cadence_minutes
        if tag is not None:
            params["tag"] = tag
        if memory_tag is not None:
            params["memory_tag"] = memory_tag
        plugin = self._ensure_ready()
        return plugin.ensure_global_heartbeat(
            params=params,
            state=self._normalize_state(state),
        )
