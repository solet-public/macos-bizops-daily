from typing import Protocol

from ananta.core.config.config_types import PluginOperationalConfig


class PluginInterface(Protocol):
    """Protocol defining the plugin interface.

    Plugins get APP_HOME from self.orchestrator_ref.APP_HOME in prepare_for_readiness()
    and store it as self._app_home. This is NOT passed as a method parameter.
    """

    name: str
    _current_action_name: str

    def get_default_config(self) -> PluginOperationalConfig: ...

    async def execute(
        self,
        action_name: str,
        parameters: dict[str, object],
    ) -> dict[str, object]: ...

    def validate_action_parameters(
        self, params: dict[str, object], required_params: list[str] | None = None
    ) -> dict[str, object]: ...
