"""Minimal EDGE plugin used by the ER-12 install_plugin_from_path smoke.

One ``@platform_process`` verb (``test_verb``) returning the constant
``{"alive": True}``. Modeled on ``demo_canary_plugin``: zero state, zero
service interfaces, declared ``field_sensitivities`` on both EDGE
return-field paths so the platform's plugin_registration_validator
accepts the plugin at discovery time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ananta.core.actions.action_metadata import (
    ContextHandling,
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.domain.enums import ActionStatus, ProcessorPolicyCategory
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
)

_PLUGIN_NAME = "fresh_install_smoke_plugin"
_RESULT_TYPE = "fresh_install_smoke_test_verb_result"



def _test_verb_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="ER-12 smoke verb payload.",
        properties={
            "alive": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                description="Always true; proves the verb dispatched.",
            ),
            "plugin_name": ParameterMetadata(
                type=ParameterType.STRING,
                description="Always 'fresh_install_smoke_plugin'.",
            ),
        },
    )


class FreshInstallSmokePlugin(PluginBase, EdgeProcessProvider):
    """Stateless EDGE plugin used by the ER-12 install_plugin_from_path smoke."""

    name: str = _PLUGIN_NAME

    def __init__(self) -> None:
        super().__init__()
        self.name = _PLUGIN_NAME

    def prepare_for_readiness(self) -> None:
        """Stateless; mark ready so list_plugins shows status=ready."""
        self.set_ready()

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        """Declare the EDGE process metadata required by plugin_registration_validator."""
        return {
            "test_verb": EdgeProcessDefinition(
                name="test_verb",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type=_RESULT_TYPE,
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
        }

    @platform_process(
        name="test_verb",
        is_discoverable=True,
        context_handling=ContextHandling.NONE,
        parameters={},
        output_type="object",
        output_description="ER-12 smoke verb payload: alive + plugin_name.",
        return_value_schema=_test_verb_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type=_RESULT_TYPE,
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    def test_verb(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the platform envelope around ``{alive: True, plugin_name: ...}``."""
        del params, state
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {"alive": True, "plugin_name": _PLUGIN_NAME},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }
